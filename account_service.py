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
import json
import os
import threading
import time

import app_paths
import dpapi

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
    const from = ds(new Date(now.getTime() - 29 * 86400000));
    // 逐页取全（此前只取第 1 页，导致日历只剩两天数据）
    let page = 1, all = [], total = 0;
    do {
      const r = await post('/billing/meter/get-user-request-usage', {
        startTime: from + ' 00:00:00', endTime: today + ' 23:59:59',
        pageNum: page, pageSize: 200 });
      const dd = (r.json && r.json.data) || {};
      total = dd.total || 0;
      all = all.concat(dd.data || []);
      page += 1;
    } while (all.length < total && page <= 5);
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


def _log(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(PROBE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


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
        self.state = {
            "status": "disconnected",   # disconnected | logining | connected | expired | error
            "user": None,
            "cloud": None,
            "packages": [],
            "daily": [],
            "cloudWeek": None,
            "cloudModels": [],
            "lastSync": None,
            "lastError": None,
            "interval": 60,
            "origin": "",
        }
        self._load_cache()

    # ---------------- 缓存 ----------------

    def _load_cache(self):
        try:
            with open(CACHE, encoding="utf-8") as f:
                d = json.load(f)
            for k in ("status", "user", "cloud", "packages", "daily", "cloudWeek", "cloudModels", "lastSync", "interval", "origin"):
                if k in d:
                    self.state[k] = d[k]
            self.interval_min = 60 if d.get("interval") is None else max(0, int(d.get("interval")))
        except Exception:
            pass

    def _save_cache(self):
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump({k: self.state[k] for k in
                           ("status", "user", "cloud", "packages", "daily", "cloudWeek", "cloudModels", "lastSync", "interval", "origin")},
                          f, ensure_ascii=False, indent=2)
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
        if websocket is None:
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
        """把登录 Cookie 加密存到本地，供下次启动回填。"""
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

    def try_restore(self):
        """启动时调用：有存档就回填并尝试同步。"""
        if not os.path.exists(COOKIE_FILE):
            return False
        time.sleep(2.0)
        self._nav(ORIGINS[0] + "/app", wait=3.5)
        if not self.restore_session():
            return False
        self._nav(ORIGINS[0] + PROFILE_PATH, wait=4.5)
        res = self.sync()
        _log(f"try_restore sync ok={res.get('ok')}")
        return bool(res.get("ok"))

    # ---------------- 同步 ----------------

    def sync(self):
        if not self.win:
            return {"ok": False, "error": "no window"}
        if self._syncing:
            return {"ok": False, "state": self.snapshot(), "busy": True}
        self._syncing = True
        try:
            bill = None
            last_err = ""
            used_origin = ""
            for origin in ORIGINS:
                for attempt in range(2):
                    # 会话按页面加载刷新：用页面内跳转刷新对应域的 profile 页
                    need_nav = (attempt > 0) or (self._current_path() != PROFILE_PATH)
                    if need_nav:
                        self._nav(origin + PROFILE_PATH, wait=4.5)
                    else:
                        time.sleep(2.0)

                    self.win.evaluate_js("window.__wbBill=null;")
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

            mapped = self._map(bill)
            with self.lock:
                self.state.update({
                    "status": "connected",
                    "cloud": mapped["cloud"],
                    "packages": mapped["packages"],
                    "daily": mapped["daily"],
                    "cloudWeek": mapped.get("cloudWeek"),
                    "cloudModels": mapped.get("cloudModels") or [],
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
            with self.lock:
                self.state["status"] = "expired"
                self.state["lastError"] = str(e)[:120]
            self._save_cache()
            _log(f"sync failed: {e}")
            return {"ok": False, "error": str(e), "state": self.snapshot()}
        finally:
            self._syncing = False

    def _map(self, bill):
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

        rows = ((((bill.get("usage") or {}).get("json") or {}).get("data") or {}).get("data")) or []
        daily = {}
        for r in rows:
            t = str(r.get("requestTime") or "")[:10]
            if not t:
                continue
            rec = daily.setdefault(t, {"points": 0.0, "calls": 0})
            rec["points"] = round(rec["points"] + _num(r.get("credit")), 2)
            rec["calls"] += 1

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
                    "uid": a0.get("uid") or "",
                    "uin": str(a0.get("uin") or ""),
                    "accountType": a0.get("type") or "",
                }
        except Exception:
            pass

        # 云端按天 × 模型（用于近 7 天堆叠，优先于本地估算）
        by_day_model = {}
        for r in rows:
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
        for r in rows:
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

        return {
            "user": dict({"plan": sm.get("SubscriptionPackageName") or "",
                          "paid": bool(sm.get("IsPaidUser"))}, **ident),
            "cloudWeek": {"models": cloud_models, "days": cloud_week},
            "cloudModels": cloud_models_detail,
            "cloud": {
                "available": round(available, 2),
                "total": round(total, 2),
                "used": round(used, 2),
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
        with self.lock:
            self.state.update({"status": "disconnected", "user": None, "cloud": None,
                               "packages": [], "daily": [], "cloudWeek": None, "cloudModels": [],
                               "lastSync": None, "lastError": None})
        try:
            if os.path.exists(CACHE):
                os.remove(CACHE)
        except Exception:
            pass
        return {"ok": True}

    def probe(self):
        return self.sync()

    def snapshot(self):
        with self.lock:
            d = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                 for k, v in self.state.items()}
        d["busy"] = self._syncing
        return d
