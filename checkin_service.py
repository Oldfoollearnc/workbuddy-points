# -*- coding: utf-8 -*-
"""
每日签到 + 派猫猫旅行（Buddy 加油站）。

为什么放在这个程序里
--------------------
程序本来就常驻、有界面、也已经握着 WorkBuddy 的网页会话（WebView2 profile），
签到和派猫只差两次接口调用。比起让用户再装一个脚本、再配一套定时任务，
把这两件事挂在启动流程上，每天开一次程序就等于领完了。

实现口径
--------
- **全部走本程序自己的网页会话**（`credentials: 'include'`，在 account_service
  的隐藏窗口里 evaluate_js 执行 fetch），不读、不碰、不打印任何 token。
- 三个写接口是唯一的副作用，均已在客户端实际验证：
    POST /billing/meter/daily-checkin         签到（幂等）
    POST /activity/growth/buddy/travel/claim  领取已到家的旅行积分
    POST /activity/growth/buddy/travel/depart 派出猫猫
  其余全是只读 GET/POST。
- **先查后写**：已签到就不发签到请求；已到每日派遣上限就不发派出请求。
  服务端本身也幂等，这里的判断只为少发无谓请求。
- 签到"今天已签到"返回的是 HTTP 400 + code 10001，这是**正常幂等返回**，
  不是失败，不能当错误报给用户。
"""
import datetime as dt
import json
import random
import threading
import time

import app_paths

CACHE = app_paths.data_path("checkin.json")

ORIGIN = "https://www.workbuddy.cn"
PROFILE_PATH = "/profile/plans-usage"

BACKOFF_SEC = (60, 300, 900)   # 失败重试：1 分钟 → 5 分钟 → 15 分钟

STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"
TRAVEL_STATUS = "/activity/growth/buddy/travel/status"
TRAVEL_CONFIG = "/activity/growth/buddy/travel/config"
TRAVEL_CLAIM = "/activity/growth/buddy/travel/claim"
TRAVEL_DEPART = "/activity/growth/buddy/travel/depart"
# 成长计划任务（注意带 /v2 前缀，和旅行那组不是同一族）
GROWTH_TASKS = "/v2/activity/growth/tasks"
GROWTH_PROFILE = "/v2/activity/growth/profile"

# 签到接口用 code 表示业务结果：0 = 成功，10001 = 今天已签到（幂等，不是错误）
CODE_OK = 0
CODE_ALREADY = 10001

_FETCH_JS = r"""
(() => {
  window.__wbCN = '';
  (async () => {
    try {
      const opt = {
        method: %(method)s,
        credentials: 'include',
        headers: {
          'content-type': 'application/json',
          accept: 'application/json',
          'x-client-platform': 'web'
        }
      };
      %(body)s
      const r = await fetch(%(path)s, opt);
      let j = null;
      try { j = await r.json(); } catch (e) {}
      window.__wbCN = JSON.stringify({ status: r.status, json: j });
    } catch (e) {
      window.__wbCN = JSON.stringify({ status: -1, error: String(e).slice(0, 200) });
    }
  })();
})();
"""


def _log(msg):
    try:
        from logutil import log as _l
        _l(msg)
    except Exception:
        pass


class CheckinService:
    """签到与派猫。所有写操作都要求显式开关，默认跟随用户偏好。"""

    def __init__(self, account):
        self.account = account          # AccountService（借用它的隐藏窗口）
        self.lock = threading.Lock()
        self._running = False
        self._watch_started = False
        self._watch_stop = threading.Event()
        self.on_update = None           # 后台跑完一轮后的回调（刷新界面）
        self._fail_streak = 0           # 连续失败次数，用于退避
        self._tasks = {}                # 成长任务缓存 {"at": ts, "data": {...}}
        self.state = {
            "status": "idle",           # idle | running | done | failed
            "result": None,
            "at": None,
            "error": None,
        }
        # 用户偏好落在本地：Python 启动时不需要等界面加载完才知道该不该签到
        self.options = {"auto": True, "travel": True}
        self._load_cache()

    # ---------------- 偏好 / 缓存 ----------------

    def set_options(self, auto=None, travel=None):
        if auto is not None:
            self.options["auto"] = bool(auto)
        if travel is not None:
            self.options["travel"] = bool(travel)
        self._save_cache()
        _log("checkin options: %s" % json.dumps(self.options, ensure_ascii=False))
        return {"ok": True, "options": dict(self.options)}

    def _load_cache(self):
        """缓存"最后一次结果"：签到还没跑完时，界面先把上次的数字显示出来。"""
        try:
            with open(CACHE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("at"):
                self.state["at"] = d.get("at")
                self.state["result"] = d.get("result")
                self.state["status"] = "done" if (d.get("result") or {}).get("ok") else "failed"
            # 记住"哪天签成的"：重启后也能直接跳过当天剩余的签到查询
            for k in ("signedDate", "streak", "week", "balance"):
                if k in d:
                    self.state[k] = d.get(k)
            opt = d.get("options") or {}
            for k in ("auto", "travel"):
                if k in opt:
                    self.options[k] = bool(opt[k])
        except Exception:
            pass

    def _save_cache(self):
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump({"at": self.state["at"], "result": self.state["result"],
                           "options": self.options,
                           "signedDate": self.state.get("signedDate"),
                           "streak": self.state.get("streak"),
                           "week": self.state.get("week"),
                           "balance": self.state.get("balance")},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        return {
            "status": self.state["status"],
            "result": self.state["result"],
            "at": self.state["at"],
            "error": self.state["error"],
            "running": self._running,
        }

    # ---------------- 页面内请求 ----------------

    def _page_ready(self):
        href = self.account._href() or ""
        return ("workbuddy.cn" in href) or ("codebuddy.cn" in href)

    def _reload_page(self, timeout=12.0):
        """强制重新加载页面，用于 401 后的自愈（会话多半只是页面状态坏了）。"""
        return self._ensure_page(timeout=timeout, force=True)

    def _ensure_page(self, timeout=12.0, force=False):
        """确保隐藏窗口停在 workbuddy 的页面里，fetch 才有同源会话。

        导航必须带超时：pywebview 的 load_url 会一直等到加载完成，网络不好或
        会话异常时能卡几分钟，整个签到线程就吊死在那里（实测遇过一次）。
        这里把导航丢进子线程，最多等 timeout 秒，超时就按"页面没就绪"处理。
        """
        if self._page_ready() and not force:
            return True

        done = threading.Event()

        def _nav():
            try:
                self.account.win.load_url(ORIGIN + PROFILE_PATH)
            except Exception as e:
                _log("checkin ensure_page load_url failed: %s" % e)
            finally:
                done.set()

        threading.Thread(target=_nav, daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if done.is_set() or self._page_ready():
                return self._page_ready()
            time.sleep(0.3)
        _log("checkin ensure_page timeout (%.0fs)" % timeout)
        return self._page_ready()

    def _call(self, path, method="POST", body=None, timeout=15.0, retry=1):
        """在页面上下文发一次请求，返回 {'status': int, 'json': dict|None}。

        401/403/5xx 会自动重试一次：服务端偶发风控和一次性拒绝长得一模一样，
        只打一次就下"登录失效"的结论，只会让用户白白去扫一次码。
        """
        resp = self._call_once(path, method, body, timeout)
        if retry > 0 and resp.get("status") in (401, 403, 500, 502, 503, 504):
            time.sleep(1.5)
            again = self._call_once(path, method, body, timeout)
            if again.get("status") == 200:
                return again
            resp = again
        return resp

    def _call_once(self, path, method="POST", body=None, timeout=15.0):
        win = self.account.win
        if not win:
            return {"status": -2, "error": "登录窗口不可用"}
        payload = dict(
            path=json.dumps(path),
            method=json.dumps(method),
            body=("opt.body = %s;" % json.dumps(json.dumps(body or {}))) if method == "POST" else "",
        )
        try:
            win.evaluate_js(_FETCH_JS % payload)
        except Exception as e:
            return {"status": -2, "error": str(e)[:160]}
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.15)
            try:
                raw = win.evaluate_js("window.__wbCN || ''")
            except Exception:
                raw = ""
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    return {"status": -2, "error": "响应解析失败"}
        return {"status": -2, "error": "请求超时"}

    @staticmethod
    def _code(resp):
        j = resp.get("json") or {}
        try:
            return int(j.get("code"))
        except Exception:
            return None

    # ---------------- 成长计划任务（只读，给活动卡用） ----------------

    def tasks(self, ttl=1800):
        """任务完成度与截止提醒。缓存 30 分钟——这组接口容易 401，别每轮都打。"""
        now = time.time()
        with self.lock:
            cached = self._tasks.get("data")
            if cached and now - self._tasks.get("at", 0) < ttl:
                return cached
        data = self._fetch_tasks()
        if data.get("ok"):
            with self.lock:
                self._tasks = {"at": now, "data": data}
        elif cached:
            # 拿不到就沿用上次的，界面上标个"旧"比整块空掉好
            stale = dict(cached)
            stale["stale"] = True
            return stale
        return data

    def _fetch_tasks(self):
        out = {"ok": False, "at": dt.datetime.now().strftime("%H:%M"),
               "completed": 0, "total": 0, "claimedCredit": 0,
               "todoCredit": 0, "dueSoon": [], "error": None}
        if self.account.state.get("autoLogin") is False or not self.account.win:
            out["error"] = "未连接账号"
            return out
        got = self.account._sync_lock.acquire(timeout=8)
        if not got:
            out["error"] = "账号正在同步，稍后再看"
            return out
        try:
            if not self._ensure_page():
                out["error"] = "打不开任务页面"
                return out
            r = self._call(GROWTH_TASKS, "GET")
            if r.get("status") != 200:
                out["error"] = self._http_hint(r, "任务进度读取失败")
                return out
            items = ((r.get("json") or {}).get("data") or {}).get("tasks") or []
            if not isinstance(items, list):
                out["error"] = "任务数据格式异常"
                return out
            today = dt.date.today()
            done, todo, due = [], [], []
            for t in items:
                credit = t.get("reward_credit") or 0
                if (t.get("accept_status") or "") == "claimed":
                    done.append(t)
                    continue
                todo.append(t)
                end = (t.get("valid_end") or "")[:10]
                if not end:
                    continue
                try:
                    left = (dt.date.fromisoformat(end) - today).days
                except Exception:
                    continue
                # 窗口给到 30 天：活动常常一次性挂一批一个月后截止的任务，
                # 只盯 7 天会一整天显示"暂无到期"，其实是有的。
                if 0 <= left <= 30:
                    due.append({"title": t.get("title") or "", "date": end,
                                "days": left, "credit": credit})
            due.sort(key=lambda x: x["days"])
            out.update({
                "ok": True,
                "completed": len(done),
                "total": len(items),
                "claimedCredit": round(sum((t.get("reward_credit") or 0) for t in done), 2),
                "todoCredit": round(sum((t.get("reward_credit") or 0) for t in todo), 2),
                "dueSoon": due[:3],
                "dueCount": len(due),
            })
            # profile 里的 completed 是官方口径，和任务列表对不上时以它为准
            p = self._call(GROWTH_PROFILE, "GET")
            if p.get("status") == 200:
                pd = (p.get("json") or {}).get("data") or {}
                if pd.get("total"):
                    out["completed"] = int(pd.get("completed") or out["completed"])
                    out["total"] = int(pd.get("total"))
            return out
        finally:
            self.account._sync_lock.release()

    # ---------------- 主流程 ----------------

    def set_notify(self, fn):
        """设置"后台跑完一轮"后的回调：让界面刷新结果条。"""
        self.on_update = fn

    def start_watch(self, travel=None):
        """常驻看猫：到点领积分，跨天自动签新的一天并重新派出。

        为什么必须常驻：**每天只能派 1 次**（实测派出后 daily_limit_reached
        立刻置 true，领取后也不重置），所以"派满"就是保证当天确实派出去了；
        但猫到家那 9 分不会自己进账，程序关了就等于白丢一趟。

        travel：启动那次 run() 的结果。传进来就能直接算出下次该醒的时刻，
        不用像以前那样先空等 90 秒——那 90 秒纯粹是躲开启动任务的拍脑袋值。
        """
        with self.lock:
            if self._watch_started:
                return False
            self._watch_started = True
        threading.Thread(target=self._watch_loop, args=(travel,), daemon=True).start()
        _log("checkin watch started (next wake in %ds)" % int(self._next_wait(travel)))
        return True

    def stop_watch(self):
        self._watch_stop.set()

    def _sleep_until(self, seconds):
        """按墙钟睡满 seconds 秒。

        不能直接 wait(seconds)：Windows 休眠时单调时钟会停，醒来后还得再等满剩余时间，
        结果就是"睡眠 8 小时后闹钟又多等 2 小时"。这里每 45 秒醒一次看墙钟，
        发现已经过点就直接走——不发任何请求，开销可忽略。
        """
        target = time.time() + max(0.0, seconds)
        while not self._watch_stop.is_set():
            remain = target - time.time()
            if remain <= 0:
                return False
            self._watch_stop.wait(min(remain, 45))
        return True     # 被 stop 打断

    def _watch_loop(self, travel=None):
        # 首轮不再固定等 90 秒：启动那次已经把旅行状态查回来了，
        # 直接按它算下一次该醒的时刻即可。
        if self._sleep_until(self._next_wait(travel)):
            return
        while not self._watch_stop.is_set():
            travel = None
            ok = True
            try:
                if self.options.get("auto", True):
                    snap = self.run(travel_auto=bool(self.options.get("travel", True)),
                                    lock_timeout=8, quiet=True)
                    res = (snap or {}).get("result") or {}
                    ok = bool(res.get("ok"))
                    travel = res.get("travel")
                    if self.on_update:
                        try:
                            self.on_update()
                        except Exception as e:
                            _log("checkin notify error: %s" % e)
            except Exception as e:
                ok = False
                _log("checkin watch error: %s" % e)

            # 失败时按 1 → 5 → 15 分钟退避，最多三次；成功立刻清零。
            # 不加这个，一次网络抖动就要等到下个自然节点（可能 4 小时后）。
            if ok:
                self._fail_streak = 0
                wait = self._next_wait(travel)
            else:
                self._fail_streak = min(self._fail_streak + 1, len(BACKOFF_SEC))
                wait = BACKOFF_SEC[self._fail_streak - 1]
                _log("checkin retry #%d，%ds 后再试" % (self._fail_streak, wait))
            if self._sleep_until(wait):
                return

    @staticmethod
    def _next_wait(travel):
        """下一轮该睡多久。

        原则：**拿到绝对时间就一觉睡到点，中途不醒**。
        arrive_at / 次日 0:05 都是确定时刻，中途反复确认不会带来任何新信息，
        只会白打请求。4 小时是地点时长上限，拿它当睡眠封顶，
        防止系统休眠或时间跳变把闹钟拖到很远。
        """
        CEIL = 4 * 3600
        if travel:
            left = int(travel.get("remainingSec") or 0)
            if left > 0:
                return max(60, min(left + 45, CEIL))   # 睡到"到家 + 45 秒"
            if travel.get("state") == "idle" and travel.get("limitReached"):
                # 每天只能派 1 次：直接睡到次日 0:05，一次搞定
                now = dt.datetime.now()
                nxt = (now + dt.timedelta(days=1)).replace(
                    hour=0, minute=5, second=0, microsecond=0)
                return max(60.0, (nxt - now).total_seconds())
            if travel.get("state") == "idle":
                return 600.0   # 空闲又能派却没派出去：多半是刚报错，10 分钟后重试
        return 900.0

    def run(self, travel_auto=True, lock_timeout=90, quiet=False):
        """执行一次签到 + 旅行闭环。线程安全，重复调用直接返回当前快照。

        quiet=True 用于后台轮询：结果照常写缓存，但不打印"done"日志刷屏。
        """
        with self.lock:
            if self._running:
                return self._snapshot()
            self._running = True
            self.state["status"] = "running"
            self.state["error"] = None

        try:
            res = self._run_inner(travel_auto, lock_timeout)
        except Exception as e:
            res = {"ok": False, "error": "执行异常：%s" % str(e)[:160],
                   "checkin": None, "travel": None}
            _log("checkin run error: %s" % e)

        with self.lock:
            self._running = False
            self.state["status"] = "done" if res.get("ok") else "failed"
            self.state["result"] = res
            self.state["at"] = res.get("at") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.state["error"] = res.get("error")
            self._save_cache()
        if not quiet:
            _log("checkin done ok=%s new=%s travel=%s" % (
                res.get("ok"),
                (res.get("checkin") or {}).get("new"),
                (res.get("travel") or {}).get("stateText")))
        return self.snapshot()

    def _run_inner(self, travel_auto, lock_timeout=90):
        now = dt.datetime.now()
        res = {
            "ok": False,
            "at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "checkin": None,
            "travel": None,
            "streak": None,
            "balance": None,
            "checkedIn": None,
            "week": [],
            "error": None,
        }

        if self.account.state.get("autoLogin") is False:
            res["error"] = "已退出登录，未执行自动签到"
            res["reason"] = "logged_out"
            return res
        if not self.account.win:
            res["error"] = "登录窗口不可用，无法签到"
            res["reason"] = "no_window"
            return res
        # 没连过账号就没有可用的网页会话：与其让用户看到"页面未就绪"这种技术黑话，
        # 不如直接说清楚该怎么办。
        if self.account.state.get("status") == "disconnected":
            res["error"] = "还没连接云端账号，签到需要先在右上角头像里连接一次"
            res["reason"] = "no_account"
            return res

        # 与定时同步共用一把锁：同步过程中页面正在跳转，此时注入 fetch 会拿到空响应。
        # 拿不到锁说明正好在同步，宁可让用户点一次「重试」，也不要和导航抢页面。
        got = self.account._sync_lock.acquire(timeout=lock_timeout)
        if not got:
            res["error"] = "账号正在同步数据，稍等一下再点重试"
            res["reason"] = "busy"
            return res
        try:
            if not self._ensure_page():
                res["error"] = "打不开签到页面，可能是网络断了或登录状态已失效"
                res["reason"] = "no_page"
                return res

            today_s = now.strftime("%Y-%m-%d")
            if self.state.get("signedDate") == today_s:
                # 今天已经签成功过：签到是一天一次，再问就是白发请求。
                # 直接复用上次的结论，跨天后 signedDate 变了才会再去查。
                res["checkedIn"] = True
                res["streak"] = self.state.get("streak")
                res["balance"] = self.state.get("balance")
                res["week"] = self.state.get("week") or []
                res["checkin"] = {"done": True, "new": False, "gain": 0,
                                  "message": "今天已经签过，没有重复打卡",
                                  "error": None, "skipped": True}
            else:
                st = self._call(STATUS_PATH, "POST")
                if st.get("status") in (401, 403):
                    # 会话多半只是页面状态坏了：重载一次再试，比直接让用户扫码温和得多
                    _log("checkin status 401，重载页面后重试")
                    if self._reload_page():
                        st = self._call(STATUS_PATH, "POST", retry=0)
                if st.get("status") != 200:
                    res["error"] = self._http_hint(st, "读取签到状态失败")
                    res["reason"] = "status_failed"
                    return res
                data = (st.get("json") or {}).get("data") or {}
                res["streak"] = data.get("streak_days")
                res["balance"] = data.get("total_credits")
                res["checkedIn"] = bool(data.get("today_checked_in"))
                res["week"] = [bool(x) for x in (data.get("week_progress") or [])][:7]
                res["checkin"] = self._do_checkin(data)
                # 记下"哪一天签成了"，当天余下的轮询就不必再问签到接口
                if res["checkedIn"]:
                    self.state["signedDate"] = today_s
                    self.state["streak"] = res["streak"]
                    self.state["week"] = res["week"]
                    self.state["balance"] = res["balance"]

            if travel_auto:
                res["travel"] = self._do_travel()
            res["ok"] = True
            # 签到结论优先于旅行：猫猫失败不影响"今天签到成功"这个结论
            if res["checkin"] and res["checkin"].get("error"):
                res["ok"] = False
                res["error"] = res["checkin"]["error"]
            return res
        finally:
            if got:
                self.account._sync_lock.release()

    # ---------------- 签到 ----------------

    def _do_checkin(self, data):
        """返回 {done, new, gain, message, error}。"""
        out = {"done": bool(data.get("today_checked_in")), "new": False,
               "gain": 0, "message": "", "error": None,
               "dailyCredit": data.get("daily_credit") or 100}
        if out["done"]:
            out["message"] = "今天已经签过，没有重复打卡"
            return out

        r = self._call(CHECKIN_PATH, "POST")
        code = self._code(r)
        if r.get("status") == 200 and code in (CODE_OK, None):
            out["done"] = True
            out["new"] = True
            out["gain"] = out["dailyCredit"]
            out["message"] = "签到成功"
            return out
        if code == CODE_ALREADY:
            # 幂等返回：服务端认为已签（多端同时开程序时会遇到），不是错误
            out["done"] = True
            out["message"] = "今天已经签过，没有重复打卡"
            return out
        out["error"] = self._http_hint(r, "签到失败")
        out["message"] = out["error"]
        return out

    def _http_hint(self, resp, prefix):
        st = resp.get("status")
        j = resp.get("json") or {}
        msg = j.get("msg") or j.get("message") or resp.get("error") or ""
        if st in (401, 403):
            # 别一上来就喊"登录失效"：同会话的其他接口往往还是好的，
            # 让用户白扫一次码比报个模糊原因更糟。
            return "请求被服务端拒绝（401），多半是短时风控，点「重试」再来一次；整页都没数据时才需要重新连接账号"
        if st == -2:
            return "%s：%s" % (prefix, msg or "页面无响应")
        if st == 200:
            return "%s：%s" % (prefix, msg or "接口返回异常")
        return "%s（HTTP %s）%s" % (prefix, st, ("：" + str(msg)) if msg else "")

    # ---------------- 派猫猫旅行 ----------------

    def _do_travel(self):
        out = {"state": "unknown", "stateText": "未知", "location": "",
               "remainingSec": 0, "remainingText": "", "reward": 0,
               "claimed": 0, "departed": False, "limitReached": False,
               "error": None}

        r = self._call(TRAVEL_STATUS, "GET")
        if r.get("status") != 200:
            out["error"] = self._http_hint(r, "旅行状态读取失败")
            return out
        d = (r.get("json") or {}).get("data") or {}
        self._fill_travel(out, d)

        # 1) 已到家 → 先领积分（领完状态变 idle，才能接着派）
        if out["state"] == "arrived":
            c = self._call(TRAVEL_CLAIM, "POST", {})
            if c.get("status") == 200:
                out["claimed"] = out["reward"]
                r2 = self._call(TRAVEL_STATUS, "GET")
                if r2.get("status") == 200:
                    self._fill_travel(out, (r2.get("json") or {}).get("data") or {})
            else:
                out["error"] = self._http_hint(c, "领取旅行积分失败")

        # 2) 空闲且今天还能派 → 派出去
        if out["state"] == "idle" and not out["limitReached"] and not out["error"]:
            locs = self._travel_locations()
            if locs:
                pick = random.choice(locs)
                p = self._call(TRAVEL_DEPART, "POST", {"location_id": pick.get("id")})
                if p.get("status") == 200:
                    out["departed"] = True
                    out["location"] = pick.get("name") or ""
                    r3 = self._call(TRAVEL_STATUS, "GET")
                    if r3.get("status") == 200:
                        self._fill_travel(out, (r3.get("json") or {}).get("data") or {})
                else:
                    out["error"] = self._http_hint(p, "派出猫猫失败")
        return out

    def _travel_locations(self):
        r = self._call(TRAVEL_CONFIG, "GET")
        if r.get("status") != 200:
            return []
        d = (r.get("json") or {}).get("data") or {}
        locs = d.get("locations") or d.get("list") or []
        return [x for x in locs if isinstance(x, dict) and x.get("id")]

    @staticmethod
    def _fill_travel(out, d):
        if not d:
            return
        state = d.get("state") or "unknown"
        arrive = d.get("arrive_at")
        server = d.get("server_now")
        # 服务端不会在到点时把 state 翻成 arrived —— 实测到点后仍是 traveling，
        # 只有 claim 成功了才变 idle。所以"能不能领"必须自己比时间，
        # 光等 state=='arrived' 会一直领不到。
        if state == "traveling" and arrive and server and int(server) >= int(arrive):
            state = "arrived"

        out["state"] = state
        out["stateText"] = {"idle": "空闲", "traveling": "旅行中",
                            "arrived": "已到家"}.get(state, "未知")
        loc = d.get("location") or {}
        out["location"] = loc.get("name") or out.get("location") or ""
        out["reward"] = d.get("reward_credit") or 0
        out["limitReached"] = bool(d.get("daily_limit_reached"))
        out["durationHours"] = d.get("duration_hours") or 0

        depart = d.get("depart_at")
        arrive = d.get("arrive_at")
        server = d.get("server_now")
        # 行程进度（0~1）：活动卡上的进度条直接吃这个值，不在前端重算时间
        if depart and arrive and server and int(arrive) > int(depart):
            span = float(int(arrive) - int(depart))
            out["progress"] = max(0.0, min(1.0, (int(server) - int(depart)) / span))
        elif state == "arrived":
            out["progress"] = 1.0
        else:
            out["progress"] = 0.0

        if state == "traveling" and arrive and server:
            left = max(0, int(arrive) - int(server))
            out["remainingSec"] = left
            out["remainingText"] = _dur_text(left)
        elif state == "arrived":
            out["remainingSec"] = 0
            out["remainingText"] = "已到家，可领取"
        else:
            out["remainingSec"] = 0
            out["remainingText"] = ""


def _dur_text(sec):
    """把秒数说成人话：38 分钟后 / 1 小时 12 分钟后。"""
    if sec <= 0:
        return "即将到家"
    m = int(round(sec / 60.0))
    if m < 60:
        return "%d 分钟后" % max(1, m)
    h, m2 = divmod(m, 60)
    return "%d 小时%s后" % (h, (" %d 分钟" % m2) if m2 else "")
