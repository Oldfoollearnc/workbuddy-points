# -*- coding: utf-8 -*-
"""
WorkBuddy 账号积分 · 联网取数服务（最终版）

实测结论（2026-10-21）
----------------------
1. 网页登录拿到的是"控制台会话"，`/v2/*` 永远 401（要客户端 OAuth Bearer）；
   但 `billing/meter/*` 接受该会话，**前提是页面刚加载过**（会话按页面刷新）。
2. 正确姿势：把登录窗导航到 `www.workbuddy.cn/profile/plans-usage` → 等加载完成
   → 在页面内 POST 四个只读接口，拿回余额/分组/积分包/逐笔明细。
3. 登录检测用 GET `/console/accounts`（200 即已登录），不能用 `/v2/accounts`。

数据接口
--------
- POST /billing/meter/get-user-resource-summary          {}                        → TotalCredits / CreditGroups / 套餐名
- POST /billing/meter/get-user-resource-free-packages     {PackageCodes,...}         → 赠送/奖励积分包（含有效期）
- POST /billing/meter/get-user-resource-paid-packages     {PackageCodes,...}         → 付费积分包
- POST /billing/meter/get-user-request-usage              {startTime,endTime,...}    → 云端逐笔扣费明细

只读、不写、不下单；登录态只存在应用自己的 WebView2 profile。
"""
import datetime as dt
import hashlib
import json
import os
import threading
import time

import app_paths
import fsutil
import dpapi
import logutil

try:
    import urllib.request
    import websocket  # websocket-client，pywebview 依赖里已带
except Exception:  # pragma: no cover
    websocket = None

# 凭证与缓存一律写在可写数据目录（安装版 = %APPDATA%\WorkBuddyPoints），
# 绝不放程序目录：Program Files 无写权限，且凭证不该跟着安装目录走。
BASE = app_paths.data_dir()
LOG_DIR = os.path.join(BASE, "logs")
PROBE_LOG = os.path.join(LOG_DIR, "api-probe.log")
CACHE = os.path.join(BASE, "account.json")
COOKIE_FILE = os.path.join(BASE, "session.bin")
CDP_PORT = 9333
COOKIE_DOMAINS = ("workbuddy.cn", "codebuddy.cn", "qq.com", "tencent.com")

LOGIN_URL = ("https://www.workbuddy.cn/login?platform=agents&state=0"
             "&redirect_uri=https%3A%2F%2Fwww.workbuddy.cn%2Fapp")
ORIGINS = ["https://www.workbuddy.cn", "https://www.codebuddy.cn"]
PROFILE_PATH = "/profile/plans-usage"
PROFILE_URL = ORIGINS[0] + PROFILE_PATH
CONSOLE_CHECK = "/console/accounts"

SYNC_JS = r"""
(async () => {
  const post = async (p, body) => {
    const r = await fetch(p, { method: 'POST', credentials: 'include',
      headers: { 'content-type': 'application/json', accept: 'application/json', 'x-client-platform': 'web' },
      body: JSON.stringify(body || {}) });
    let j = null;
    try { j = await r.json(); } catch (e) {}
    return { status: r.status, json: j };
  };
  const pad = n => String(n).padStart(2, '0');
  const ds = d => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  const now = new Date();
  const today = ds(now);
  const out = { at: Date.now(), href: location.href };
  try {
    out.summary = await post('/billing/meter/get-user-resource-summary', {});
    const sm = (out.summary.json && out.summary.json.data) || {};
    const codes = (sm.Packages || []).map(p => p.PackageCode).filter(Boolean);
    out.free = await post('/billing/meter/get-user-resource-free-packages', {
      PageNumber: 1, PageSize: 200, Status: [0],
      SlicePeriodStartTime: today + ' 00:00:00', SlicePeriodEndTime: today + ' 23:59:59',
      PackageCodes: codes });
    out.paid = await post('/billing/meter/get-user-resource-paid-packages', {
      PageNumber: 1, PageSize: 20, Status: [0, 3], PackageCodes: codes, NeedRenewInfo: true });
    // 增量扫描：起始日由 Python 根据缓存里最新一天推算（见 _usage_since）
    const from = (window.__wbSince) || ds(new Date(now.getTime() - 29 * 86400000));
    // 逐页取全（此前只取第 1 页，导致日历只剩两天数据）。
    // 每页 500、最多 8 页：单日调用量再大也能一次拉完，总量仍很小（只读、按天窗口）。
    let page = 1, all = [], total = 0;
    do {
      const r = await post('/billing/meter/get-user-request-usage', {
        startTime: from + ' 00:00:00', endTime: today + ' 23:59:59',
        pageNum: page, pageSize: 500 });
      const dd = (r.json && r.json.data) || {};
      total = dd.total || 0;
      all = all.concat(dd.data || []);
      page += 1;
    } while (all.length < total && page <= 8);
    out.usage = { status: 200, json: { data: { data: all, total: total } } };
    out.usageTotal = total;
    try {
      const ra = await fetch('/console/accounts', { credentials: 'include', headers: { accept: 'application/json' } });
      out.identity = await ra.json();
    } catch (e) { out.identity = null; }
    out.ok = out.summary.status === 200 && !!(out.summary.json && out.summary.json.data);
  } catch (e) {
    out.error = String(e).slice(0, 200);
    out.ok = false;
  }
  window.__wbBill = out;
})();
"""


def cdp_enabled():
    """是否允许本进程开 CDP 调试端口。

    默认关闭：`--remote-debugging-port` + `--remote-allow-origins=*` 意味着**同机任意
    进程**都能连上 WebView2 读走全部 Cookie，生产环境没有任何理由常开。
    只有开发期需要 tools/live.py 截图、或要导出 session.bin 时，
    显式设 `WB_CDP=1` 才打开。
    """
    return os.environ.get("WB_CDP") == "1"


def _log(msg):
    logutil.log(PROBE_LOG, msg, "api")


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _date(s):
    if not s:
        return ""
    return str(s)[5:10] if len(str(s)) >= 10 else str(s)


class AccountService:
    def __init__(self):
        self.win = None
        self.lock = threading.Lock()
        self.interval_min = 60
        self._stop = False
        self._last_check = None
        self._syncing = False
        self._sync_lock = threading.Lock()   # 定时轮询与手动同步可能撞车，用锁而不是布尔判断
        self.state = {
            "status": "disconnected",   # disconnected | logining | connected | expired | error
            "user": None,
            "cloud": None,
            "packages": [],
            "daily": [],
            "cloudWeek": None,
            "cloudModels": [],
            "usageRows": [],
            "lastSync": None,
            "lastError": None,
            "interval": 60,
            "origin": "",
            # 用户显式「退出登录」后置 False：下次启动不再自动回填会话。
            # 没有它的话，退出登录只是清了内存，重启后 try_restore 又把账号连回来了。
            "autoLogin": True,
        }
        self._load_cache()

    # ---------------- 缓存 ----------------

    def _load_cache(self):
        try:
            d = fsutil.load_json(CACHE)
            if d is None:
                raise FileNotFoundError(CACHE)
            for k in ("status", "user", "cloud", "packages", "daily", "cloudWeek", "cloudModels", "usageRows", "lastSync", "interval", "origin", "autoLogin"):
                if k in d:
                    self.state[k] = d[k]
            self.interval_min = 60 if d.get("interval") is None else max(0, int(d.get("interval")))
        except Exception:
            pass

    def _save_cache(self):
        try:
            # 原子写：这个文件上百 KB，直接 open(w) 截断再写，别的线程读到半截会解析失败
            fsutil.save_json(CACHE, {k: self.state[k] for k in
                           ("status", "user", "cloud", "packages", "daily", "cloudWeek", "cloudModels", "usageRows", "lastSync", "interval", "origin", "autoLogin")},
                           indent=2)
        except Exception:
            pass

    # ---------------- 窗口 ----------------

    def attach(self, win):
        self.win = win

    def open_login(self):
        if not self.win:
            return {"ok": False, "error": "登录窗口不可用"}
        try:
            self.win.show()
            self._nav(LOGIN_URL, wait=2.0)
            threading.Timer(2.5, self._install_hook).start()
            with self.lock:
                self.state["status"] = "logining"
                self.state["autoLogin"] = True     # 用户主动登录 → 恢复开机自动回填
            threading.Thread(target=self._watch_login, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _install_hook(self):
        """新窗口一律拦在本窗口内，避免回调被外部浏览器接走。"""
        js = """
        (() => {
          if (window.__wbHooked) return;
          window.__wbHooked = true;
          window.open = function (url) { if (url) location.href = String(url); return null; };
          const fix = () => { try { document.querySelectorAll('a[target="_blank"]').forEach(a => a.target = '_self'); } catch (e) {} };
          document.addEventListener('click', e => {
            const a = e.target && e.target.closest ? e.target.closest('a[target="_blank"]') : null;
            if (a && a.href) { e.preventDefault(); location.href = a.href; }
          }, true);
          fix(); setInterval(fix, 1500);
        })();
        """
        try:
            self.win.evaluate_js(js)
        except Exception:
            pass

    def _watch_login(self, timeout=900):
        """不打断用户当前页面：只检测当前页所在域的登录状态；
        同步阶段再自动切到有会话的那个域。"""
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop:
            time.sleep(5)
            self._install_hook()
            st = self._console_status()
            if st != self._last_check:
                self._last_check = st
                _log(f"watch console={st} href={self._href()}")
            if st == 200:
                _log("login detected (billing 200)")
                res = self.sync()
                if res.get("ok"):
                    try:
                        self.win.hide()
                    except Exception:
                        pass
                    return
                _log("sync after login failed, keep window open")
                with self.lock:
                    self.state["status"] = "logining"
        _log("watch timeout")

    def _href(self):
        try:
            return (self.win.evaluate_js("location.href") or "")[:110]
        except Exception:
            return ""

    def _nav(self, url, wait=4.0):
        """优先用 pywebview 原生导航（稳定），失败再退回页面内跳转。"""
        try:
            self.win.load_url(url)
            time.sleep(wait)
            return True
        except Exception as e:
            _log(f"load_url failed: {e}")
            return self._goto(url, wait)

    def _goto(self, url, wait=4.0):
        try:
            self.win.evaluate_js("location.href=%s" % json.dumps(url))
        except Exception as e:
            _log(f"goto error: {e}")
            return False
        time.sleep(wait)
        try:
            for _ in range(20):
                if (self.win.evaluate_js("document.readyState") or "") == "complete":
                    break
                time.sleep(0.4)
        except Exception:
            pass
        return True

    def _on_origin(self, origin):
        """隐藏窗口当前是否已经停在这个域里——同源 fetch 不需要重新加载页面。"""
        try:
            href = self.win.evaluate_js("location.href") or ""
        except Exception:
            return False
        return str(href).startswith(origin)

    def _current_path(self):
        try:
            return self.win.evaluate_js("location.pathname") or ""
        except Exception:
            return ""

    def _console_status(self):
        """真正的登录判定：在当前页面 POST 一次积分汇总。
        返回 200 说明会话可用于 billing 接口（登录页会返回 401）。"""
        if not self.win:
            return -1
        js = ("fetch('/billing/meter/get-user-resource-summary',{method:'POST',credentials:'include',"
              "headers:{'content-type':'application/json',accept:'application/json','x-client-platform':'web'},"
              "body:'{}'}).then(r=>{window.__wbC=r.status;return r.status}).catch(()=>{window.__wbC=-1;return -1})")
        try:
            self.win.evaluate_js(js)
            for _ in range(10):
                time.sleep(0.25)
                v = self.win.evaluate_js("window.__wbC||0")
                if v:
                    return int(v)
        except Exception:
            pass
        return -1

    # ---------------- 登录态持久化（CDP） ----------------

    def _cdp_tabs(self):
        if websocket is None or not cdp_enabled():
            return []
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=3) as r:
                return json.load(r)
        except Exception:
            return []

    def _cdp_ws(self):
        for t in self._cdp_tabs():
            if t.get("type") == "page" and ("workbuddy.cn" in t.get("url", "") or "codebuddy.cn" in t.get("url", "")):
                try:
                    return websocket.create_connection(t["webSocketDebuggerUrl"], timeout=8)
                except Exception:
                    continue
        return None

    def save_session(self):
        """把登录 Cookie 加密存到本地（仅开发态）。

        生产态不开 CDP，拿不到 Cookie 也没关系：WebView2 profile 本身就是持久化的
        （private_mode=False + storage_path），Cookie 由浏览器自己落盘。
        """
        if not cdp_enabled():
            return False
        ws = self._cdp_ws()
        if not ws:
            _log("save_session: no cdp tab")
            return False
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies", "params": {}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == 1:
                    cookies = (r.get("result") or {}).get("cookies") or []
                    break
            keep = [c for c in cookies if any(d in (c.get("domain") or "") for d in COOKIE_DOMAINS)]
            dpapi.save_json(COOKIE_FILE, {"at": time.time(), "cookies": keep})
            _log(f"save_session: {len(keep)} cookies")
            return True
        except Exception as e:
            _log(f"save_session error: {e}")
            return False
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def restore_session(self):
        """启动时回填 Cookie；成功后无需再次扫码。"""
        if not os.path.exists(COOKIE_FILE):
            return False
        try:
            data = dpapi.load_json(COOKIE_FILE)
            cookies = data.get("cookies") or []
        except Exception as e:
            _log(f"restore_session read error: {e}")
            return False
        ws = self._cdp_ws()
        if not ws:
            _log("restore_session: no cdp tab")
            return False
        n = 0
        try:
            for c in cookies:
                params = {k: c.get(k) for k in
                          ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires")}
                params = {k: v for k, v in params.items() if v not in (None, "")}
                if "expires" in params and params["expires"] and params["expires"] < time.time():
                    continue
                ws.send(json.dumps({"id": 100 + n, "method": "Network.setCookie", "params": params}))
                n += 1
            time.sleep(0.6)
            _log(f"restore_session: set {n} cookies")
            return n > 0
        except Exception as e:
            _log(f"restore_session error: {e}")
            return False
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _sync_cooling(self):
        """距上次成功同步是否还没到间隔。

        为什么要有它：以前每次启动都无条件 sync 一遍，一天开五次机就是五次全量拉取
        （汇总 + 免费包 + 付费包 + 多页逐笔 + 身份）。这类接口按频率风控，
        开得越勤越容易被限流，限流之后连手动同步都难。
        用户手动点「同步」不走这里，永远能立刻拉。
        """
        try:
            mins = int(self.interval_min)
        except Exception:
            mins = 60
        if mins <= 0:
            return True          # 设为「仅手动」：启动时一次都不打
        if self.state.get("status") != "connected" or not self.state.get("cloud"):
            return False         # 没有可用缓存，必须同步一次
        last = self.state.get("lastSync")
        if not last:
            return False
        try:
            lt = dt.datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False
        return (dt.datetime.now() - lt).total_seconds() < mins * 60

    def try_restore(self):
        """启动时调用：登录态来自 WebView2 持久化 profile，能同步就同步。

        Cookie 本来就跟 profile 一起落盘，所以不再依赖 CDP 回填；
        只有开发态（WB_CDP=1）才额外把 session.bin 里的 Cookie 灌一次。
        """
        if not self.state.get("autoLogin", True):
            _log("try_restore skipped: 用户已退出登录")
            return False
        time.sleep(2.0)
        # 先导一次页面把会话激活（签到要用同源 fetch，也需要页面在 workbuddy 域）
        self._nav(ORIGINS[0] + "/app", wait=3.5)
        if cdp_enabled() and os.path.exists(COOKIE_FILE):
            self.restore_session()

        if self._sync_cooling():
            _log("try_restore: 距上次同步不足 %s 分钟，直接用缓存，跳过主动同步" % self.interval_min)
            return bool(self.state.get("cloud"))

        self._nav(ORIGINS[0] + PROFILE_PATH, wait=4.5)
        res = self.sync()
        _log(f"try_restore sync ok={res.get('ok')}")
        return bool(res.get("ok"))

    # ---------------- 同步 ----------------

    def sync(self):
        if not self.win:
            return {"ok": False, "error": "no window"}
        if not self._sync_lock.acquire(blocking=False):
            return {"ok": False, "state": self.snapshot(), "busy": True}
        self._syncing = True
        try:
            bill = None
            last_err = ""
            used_origin = ""
            for origin in ORIGINS:
                for attempt in range(2):
                    # 页面已经在这个域里就直接发请求，不再每次同步都整页重载一遍
                    # （一次重载要拉 HTML/JS/CSS，几百 KB，纯属白花）。
                    # 兜底：第一次拿不到数据时，重试会强制导航一次再试。
                    need_nav = (attempt > 0) or (not self._on_origin(origin))
                    if need_nav:
                        self._nav(origin + PROFILE_PATH, wait=4.5)
                    else:
                        time.sleep(1.2)

                    since = self._usage_since()
                    self.win.evaluate_js("window.__wbBill=null; window.__wbSince=%s;" % json.dumps(since))
                    self.win.evaluate_js(SYNC_JS)
                    raw = None
                    for _ in range(30):
                        time.sleep(0.3)
                        try:
                            raw = self.win.evaluate_js("JSON.stringify(window.__wbBill||null)")
                        except Exception:
                            raw = None
                        if raw and raw not in ("null", "undefined"):
                            break
                    bill = json.loads(raw) if raw and raw != "null" else None
                    if bill and bill.get("ok"):
                        used_origin = origin
                        break
                    last_err = "%s: %s" % (origin, (bill or {}).get("error") or (
                        "summary status=%s" % (((bill or {}).get("summary") or {}).get("status"))))
                    _log(f"sync attempt {origin} #{attempt + 1} failed: {last_err}")
                if bill and bill.get("ok"):
                    break

            if not bill or not bill.get("ok"):
                raise RuntimeError(last_err or "接口未返回数据")

            mapped = self._map(bill, self.state.get("usageRows") or [])

            # —— 降级保护 ——
            # 接口字段改名/分页失效时，最伤人的不是报错，而是"看起来同步成功、数字全变 0"。
            # 这里做两件事：明显不合理的新值不覆盖旧值，并打上 stale 标记让界面说明。
            prev = (self.state.get("cloud") or {})
            fresh = mapped["cloud"]
            stale_note = ""
            try:
                new_avail = fresh.get("available") or 0
                old_avail = prev.get("available") or 0
                new_rows = mapped.get("usageRows") or []
                if old_avail > 0 and new_avail <= 0:
                    fresh["available"] = old_avail
                    fresh["total"] = prev.get("total") or fresh.get("total") or 0
                    fresh["used"] = prev.get("used") or 0
                    stale_note = "云端余额字段异常，沿用上次数据"
                elif prev.get("used", 0) > 0 and not new_rows:
                    mapped["usageRows"] = self.state.get("usageRows") or []
                    mapped["daily"] = self.state.get("daily") or []
                    stale_note = "本次没有取到逐笔明细，沿用上次数据"
            except Exception as e:
                _log(f"degrade check error: {e}")
            if stale_note:
                fresh["stale"] = stale_note
                _log("sync degraded: %s" % stale_note)

            with self.lock:
                self.state.update({
                    "status": "connected",
                    "cloud": fresh,
                    "packages": mapped["packages"],
                    "daily": mapped["daily"],
                    "cloudWeek": mapped.get("cloudWeek"),
                    "cloudModels": mapped.get("cloudModels") or [],
                    "usageRows": mapped.get("usageRows") or [],
                    "user": mapped["user"],
                    "lastSync": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "lastError": None,
                    "origin": used_origin,
                })
            self._save_cache()
            threading.Thread(target=self.save_session, daemon=True).start()
            _log(f"sync ok available={mapped['cloud'].get('available')} "
                 f"groups={len(mapped['packages'])} daily={len(mapped['daily'])}")
            return {"ok": True, "state": self.snapshot()}
        except Exception as e:
            msg = str(e)
            # 只有"会话不可用"才算过期。网络抖动 / 接口超时也判成过期的话，
            # 界面会一直显示"登录已失效"，用户只能重新扫码 —— 这是误判，不是事实。
            auth_like = any(k in msg for k in ("401", "403", "登录", "会话"))
            with self.lock:
                self.state["status"] = "expired" if auth_like else "error"
                self.state["lastError"] = msg[:120]
            self._save_cache()
            _log(f"sync failed ({'auth' if auth_like else 'transient'}): {e}")
            return {"ok": False, "error": msg, "state": self.snapshot()}
        finally:
            self._syncing = False
            self._sync_lock.release()

    def _usage_since(self):
        """增量扫描起点（保护账号，避免每次都全量拉 30 天）。

        规则：
        - 有缓存：从"缓存里最新一天 - 2 天"开始（覆盖跨天延迟入账），最多回看 30 天；
        - 缓存断档超过 25 天：说明很久没同步，回退成完整 30 天；
        - 无缓存：完整 30 天。
        已经过去的日期账单不会变，重复拉只会增加请求量，没有信息增量。
        """
        today = dt.date.today()
        full = (today - dt.timedelta(days=29)).isoformat()
        days = [d.get("date") for d in (self.state.get("daily") or []) if d.get("date")]
        if not days:
            return full
        try:
            last = dt.date.fromisoformat(max(days))
        except Exception:
            return full
        if (today - last).days > 25:
            return full
        start = last - dt.timedelta(days=2)
        return max(full, start.isoformat())

    def _map(self, bill, prev_rows=None):
        sm = ((bill.get("summary") or {}).get("json") or {}).get("data") or {}
        available = _num(sm.get("TotalCredits"))
        groups = sm.get("CreditGroups") or []
        packages = []
        for g in groups:
            packages.append({
                "kind": g.get("Kind") or "",
                "name": g.get("Name") or g.get("Kind") or "",
                "total": _num(g.get("CycleTotalCapacity")),
                "remain": _num(g.get("CycleRemainCapacity")),
                "used": _num(g.get("CycleUsedCapacity")),
            })
        total = sum(p["total"] for p in packages) or available
        used = sum(p["used"] for p in packages)

        # 有效期：仍有余额的积分包里最早的到期时间（最先过期的最该关注）
        expiries, starts = [], []
        for src in (bill.get("free"), bill.get("paid")):
            accs = (((src or {}).get("json") or {}).get("data") or {}).get("Accounts") or []
            for a in accs:
                remain = _num(a.get("CycleCapacityRemainPrecise")
                              or a.get("CycleCapacityRemain")
                              or a.get("CapacityRemain") or 0)
                if remain <= 0:
                    continue
                if a.get("CycleEndTime"):
                    expiries.append(str(a["CycleEndTime"]))
                if a.get("CycleStartTime"):
                    starts.append(str(a["CycleStartTime"]))
        expire_at = min(expiries) if expiries else ""
        cycle_start = min(starts)[:10] if starts else ""

        # ---- 逐笔：归一化 + 与缓存合并 ----
        # 增量扫描只拉"上次之后"的窗口，因此必须把新数据并进缓存，否则视图会缺历史。
        #
        # 去重有**两把键**：
        #   1) requestId —— 同一条记录的重复拉取；
        #   2) 内容键（秒级时间 + 模型 + 积分 + 用途）—— 云端会把同一笔请求
        #      在不同次返回里生成不同的 requestId（实测 10-20 整天几乎每笔都成对出现），
        #      只按 id 去重会让日历/趋势的每日合计凭空多出 60%+。
        #      校验：周期内逐笔合计 2369.92，账号自报"已用"1416.39；按内容去重后 1453.96。
        raw_rows = ((((bill.get("usage") or {}).get("json") or {}).get("data") or {}).get("data")) or []
        merged = {}
        seen_content = set()

        def _add(t, credit, model, purpose, rid):
            key = (t, model, round(credit, 4), purpose)
            if key in seen_content:
                return
            seen_content.add(key)
            merged[rid] = {"requestTime": t, "credit": credit,
                           "model": model, "agentPurpose": purpose}

        for r in (prev_rows or []):
            t = str(r.get("t") or r.get("requestTime") or "")[:19]
            if not t:
                continue
            rid = r.get("id") or r.get("requestId") or "%s|%s|%s" % (t, r.get("model"), r.get("credit"))
            _add(t, _num(r.get("credit")), str(r.get("model") or ""),
                 str(r.get("purpose") or r.get("agentPurpose") or ""), rid)
        for r in raw_rows:
            t = str(r.get("requestTime") or "")[:19]
            if not t:
                continue
            rid = str(r.get("requestId") or "%s|%s|%s" % (t, r.get("model"), r.get("credit")))
            _add(t, _num(r.get("credit")), str(r.get("model") or ""),
                 str(r.get("agentPurpose") or ""), rid)

        cutoff = (dt.date.today() - dt.timedelta(days=29)).isoformat()
        rows_all = [v for v in merged.values() if str(v.get("requestTime") or "")[:10] >= cutoff]
        rows_all.sort(key=lambda x: x["requestTime"], reverse=True)

        # 聚合一律用全量。以前先把 rows 截断到 1500 条再算 daily，
        # 重度账号第 30 天那一头会被静默砍掉，日历/趋势与"已用"永远对不上。
        daily = {}
        for r in rows_all:
            t = str(r.get("requestTime") or "")[:10]
            if not t:
                continue
            rec = daily.setdefault(t, {"points": 0.0, "calls": 0})
            rec["points"] = round(rec["points"] + _num(r.get("credit")), 2)
            rec["calls"] += 1

        # 逐笔明细（云端口径）：换一台电脑登录同一账号，看到的也是这一份。
        # 只在这里限量 —— account.json 不能无限长，但统计口径不受这个上限影响。
        usage_rows = []
        for rid, v in merged.items():
            t = v["requestTime"]
            if t[:10] < cutoff:
                continue
            usage_rows.append({"id": rid, "t": t, "credit": round(v["credit"], 4),
                               "model": v["model"], "purpose": v["agentPurpose"]})
        usage_rows.sort(key=lambda x: x["t"], reverse=True)
        usage_rows = usage_rows[:1500]

        # 账号身份（昵称/头像/手机号）
        ident = {}
        try:
            accs = (((bill.get("identity") or {}).get("data") or {}).get("accounts")) or []
            if accs:
                a0 = accs[0]
                ident = {
                    "nickname": a0.get("nickname") or "",
                    "avatar": a0.get("avatarUrl") or "",
                    "phone": a0.get("phoneNumber") or "",
                    # UID / UIN 不落盘：本地任何地方都不展示它们，
                    # 只留一个不可逆哈希用于区分账号（多账号功能会用到这个哈希）。
                    "uidHash": hashlib.sha256(
                        str(a0.get("uid") or "").encode("utf-8")).hexdigest()[:16],
                    "accountType": a0.get("type") or "",
                }
        except Exception:
            pass

        # 云端按天 × 模型（用于近 7 天堆叠，优先于本地估算）
        by_day_model = {}
        for r in rows_all:
            t = str(r.get("requestTime") or "")[:10]
            m = str(r.get("model") or "未知模型")
            if not t:
                continue
            by_day_model.setdefault(t, {})
            by_day_model[t][m] = round(by_day_model[t].get(m, 0) + _num(r.get("credit")), 2)
        model_totals = {}
        for d, mv in by_day_model.items():
            for m, v in mv.items():
                model_totals[m] = round(model_totals.get(m, 0) + v, 2)
        cloud_models = [k for k, _v in sorted(model_totals.items(), key=lambda kv: -kv[1])]

        # 近 30 天 × 模型（含调用次数）：模型使用量卡片的云端口径
        model_stats = {}
        for r in rows_all:
            m = str(r.get("model") or "未知模型")
            st = model_stats.setdefault(m, {"points": 0.0, "calls": 0})
            st["points"] += _num(r.get("credit"))
            st["calls"] += 1
        cloud_models_detail = [{"name": k, "points": round(v["points"], 2), "calls": v["calls"]}
                               for k, v in sorted(model_stats.items(), key=lambda kv: -kv[1]["points"])]
        cloud_week = []
        for i in range(6, -1, -1):
            day = (dt.date.today() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            mv = by_day_model.get(day, {})
            cloud_week.append({
                "date": day[5:],
                "weekday": "日一二三四五六"[(dt.date.today() - dt.timedelta(days=i)).isoweekday() % 7],
                "values": [round(mv.get(m, 0), 2) for m in cloud_models],
                "total": round(sum(mv.values()), 2),
                "isToday": i == 0,
            })

        # 对账：逐笔合计 vs 账号自报"周期已用"，**只在同一个周期内比**。
        # 踩过的坑：29 天窗口里包含周期开始前的消耗，会让差额从 2.4% 虚高到 9.4%。
        cycle_from = (cycle_start or "")[:10]
        rows_cycle = [r for r in rows_all
                      if not cycle_from or str(r.get("requestTime") or "")[:10] >= cycle_from]
        rows_sum = round(sum(_num(r.get("credit")) for r in rows_cycle), 2)
        drift = round(rows_sum - used, 2) if used > 0 else 0.0
        drift_pct = round(drift / used, 4) if used > 0 else 0.0

        # 云端返回的到底是"逐笔调用"还是"聚合后的计费记录"？
        # 实测 10-21 起全部落在 5 分钟整点上（秒=00 且分钟是 5 的倍数），
        # 更早的记录是分钟级 —— 说明服务端已改为按「5 分钟 × 模型」聚合。
        # 判定只看**今天**：KPI 展示的是今天的笔数，历史混粒度不影响今天的标签。
        def _is_bucket(t):
            s = str(t or "")
            try:
                return s[17:19] == "00" and int(s[14:16]) % 5 == 0
            except (ValueError, IndexError):
                return False

        today_s = dt.date.today().isoformat()
        today_rows = [r for r in rows_cycle if str(r.get("requestTime") or "")[:10] == today_s]
        bucket_n = sum(1 for r in today_rows if _is_bucket(r.get("requestTime")))
        count_kind = "bucket" if today_rows and bucket_n / len(today_rows) >= 0.8 else "request"

        return {
            "user": dict({"plan": sm.get("SubscriptionPackageName") or "",
                          "paid": bool(sm.get("IsPaidUser"))}, **ident),
            "cloudWeek": {"models": cloud_models, "days": cloud_week},
            "cloudModels": cloud_models_detail,
            "usageRows": usage_rows,
            "cloud": {
                "available": round(available, 2),
                "total": round(total, 2),
                "used": round(used, 2),
                "rowsSum": rows_sum,
                "drift": drift,
                "driftPct": drift_pct,
                "countKind": count_kind,
                "cycleRows": len(rows_cycle),
                "expireAtText": _date(expire_at),
                "cycleStartText": cycle_start,
                "cycleEndText": "",
                "source": "billing/meter",
            },
            "packages": packages,
            "daily": [{"date": k, "points": v["points"], "calls": v["calls"]}
                      for k, v in sorted(daily.items())][-90:],
        }

    # ---------------- 轮询 / 设置 ----------------

    def start_polling(self):
        def loop():
            while not self._stop:
                time.sleep(30)
                if self.state["status"] != "connected":
                    continue
                if self.interval_min <= 0:        # 0 = 仅手动，不做任何自动请求
                    continue
                last = self.state.get("lastSync")
                due = True
                if last:
                    try:
                        due = (dt.datetime.now() - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() >= self.interval_min * 60
                    except Exception:
                        due = True
                if due:
                    try:
                        self.sync()
                    except Exception:
                        pass
        threading.Thread(target=loop, daemon=True).start()

    def set_interval(self, minutes):
        try:
            iv = max(0, int(minutes))
        except Exception:
            iv = 60
        self.interval_min = iv          # 0 表示"仅手动"，不能被折叠成 60
        with self.lock:
            self.state["interval"] = self.interval_min
        self._save_cache()
        return {"ok": True, "interval": self.interval_min}

    def logout(self):
        """断开账号：清数据、删会话存档、关闭自动回填，并尽力清掉 profile 里的 Cookie。

        以前只清了内存里的 state，session.bin 与 WebView2 profile 里的 Cookie 都还在，
        重启后 try_restore 直接把账号又连回来了 —— 「退出登录」等于没退。
        """
        with self.lock:
            self.state.update({"status": "disconnected", "user": None, "cloud": None,
                               "packages": [], "daily": [], "cloudWeek": None, "cloudModels": [],
                               "lastSync": None, "lastError": None, "autoLogin": False})
            keep = {k: self.state[k] for k in ("interval", "autoLogin")}
        try:
            if os.path.exists(COOKIE_FILE):
                os.remove(COOKIE_FILE)
                _log("logout: session.bin removed")
        except Exception as e:
            _log(f"logout: remove session failed {e}")
        # 缓存只留设置项，账号数据不留在磁盘上
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log(f"logout: write cache failed {e}")
        self._clear_profile_cookies()
        return {"ok": True}

    def _clear_profile_cookies(self):
        """尽力清掉登录窗所在域的 Cookie。

        httpOnly 的 Cookie 由浏览器保护，脚本删不掉（会在过期后自然失效），
        但至少把可删的都删掉、localStorage 清空，避免下次打开登录窗还是已登录态。
        """
        if not self.win:
            return
        js = """
        (() => {
          document.cookie.split(';').forEach(c => {
            const n = (c.split('=')[0] || '').trim();
            if (n) document.cookie = n + '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/';
          });
          try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}
        })();
        """
        try:
            self.win.evaluate_js(js)
            _log("logout: profile cookies cleared (best effort)")
        except Exception as e:
            _log(f"logout: clear cookies failed {e}")

    def probe(self):
        return self.sync()

    def snapshot(self):
        with self.lock:
            d = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                 for k, v in self.state.items()}
        d["busy"] = self._syncing
        return d
