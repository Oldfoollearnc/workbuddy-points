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
import fsutil

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
            # 今日结论：签到过没 / 派出过没 / 收回没 / 收回多少。
            # 单独存一份，重启后第一帧直接照着它渲染，不必等远端。
            "today": {},
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
            for k in ("signedDate", "streak", "week", "balance", "claimedDate", "claimedToday"):
                if k in d:
                    self.state[k] = d.get(k)
            opt = d.get("options") or {}
            for k in ("auto", "travel"):
                if k in opt:
                    self.options[k] = bool(opt[k])
            t = d.get("today")
            if isinstance(t, dict) and t.get("date"):
                self.state["today"] = t
            self._migrate_day_cache(d)
        except Exception:
            pass

    def _save_cache(self):
        try:
            today = self.state.get("today")
            fsutil.save_json(CACHE, {"at": self.state["at"], "result": self.state["result"],
                                 "options": dict(self.options),
                                 # 拷一份再序列化：后台线程可能正在往 today 里写，
                                 # 直接传引用会 json.dump 时 "dict changed size during iteration"
                                 "today": dict(today) if isinstance(today, dict) else {},
                                 "signedDate": self.state.get("signedDate"),
                                 "streak": self.state.get("streak"),
                                 "week": self.state.get("week"),
                                 "balance": self.state.get("balance"),
                                 "claimedDate": self.state.get("claimedDate"),
                                 "claimedToday": self.state.get("claimedToday")}, indent=2)
        except Exception:
            pass

    # ---------------- 今日结论（本地优先） ----------------

    @staticmethod
    def _day_key():
        return dt.date.today().isoformat()

    def _today(self, create=True):
        """取今日结论；跨天自动清零。create=False 时没有就返回空 dict。"""
        today = self._day_key()
        t = self.state.get("today")
        if not isinstance(t, dict) or t.get("date") != today:
            if not create:
                return {}
            t = {"date": today}
            self.state["today"] = t
        return t

    @staticmethod
    def _ts_of(text):
        """'YYYY-MM-DD HH:MM:SS'（本地时间）→ 秒级时间戳；解析不了返回 0。"""
        try:
            return int(time.mktime(time.strptime(str(text), "%Y-%m-%d %H:%M:%S")))
        except Exception:
            return 0

    @staticmethod
    def _int0(v):
        """缓存里的数字可能被写成字符串、带千分位、甚至整段时间文本。

        这里一律降级成 0：宁可少显示一个倒计时，也不能让 snapshot() 抛异常
        —— 那会让前端永远卡在「正在准备签到…」。
        """
        try:
            return int(float(str(v).replace(",", "").strip()))
        except Exception:
            return 0

    def _migrate_day_cache(self, d):
        """把老缓存的散字段（signedDate / claimedDate / result.travel）并进 today 块。

        老版本没存"到达时间"，但存了快照时间 at 和相对剩余秒数，
        两者相加就是绝对到达时刻 —— 用它把"上次看到还剩 228 分钟"还原成"几点几分到"。
        """
        today = self._day_key()
        t = self._today()
        if self.state.get("signedDate") == today or d.get("signedDate") == today:
            t["checkedIn"] = True
            for k in ("streak", "week", "balance"):
                v = self.state.get(k, d.get(k))
                if v is not None:
                    t[k] = v
        if self.state.get("claimedDate") == today or d.get("claimedDate") == today:
            got = self._int0(self.state.get("claimedToday") or d.get("claimedToday"))
            if got > 0:
                t.update(claimed=True, reward=got, dispatched=True, limitReached=True)

        res = self.state.get("result") or {}
        tv = (res.get("travel") or {}) if isinstance(res, dict) else {}
        if isinstance(res, dict) and res.get("date") == today and tv:
            st = tv.get("state")
            if st in ("traveling", "arrived") or tv.get("departed") or tv.get("limitReached"):
                t["dispatched"] = True
            if tv.get("location"):
                t.setdefault("location", tv.get("location"))
            if tv.get("limitReached"):
                t["limitReached"] = True
            if st == "traveling" and not t.get("arriveAt"):
                arrive = self._ts_of(res.get("at")) + self._int0(tv.get("remainingSec"))
                if arrive > 0:
                    t["arriveAt"] = arrive
                    t.setdefault("departAt", self._ts_of(res.get("at")))
            if tv.get("reward"):
                t.setdefault("pending", self._int0(tv.get("reward")))
            if st in ("traveling", "arrived"):
                t.setdefault("dispatchedAt", res.get("at"))

    def _absorb(self, res):
        """把一轮结果沉淀成今日结论：以后启动直接读它，不再问远端。"""
        if not isinstance(res, dict):
            return
        # 一轮可能跨过零点（_ensure_page 最长 12s + 多次请求）。结果里的 date 是
        # 开跑那一刻的日期，而 _today() 取的是此刻的日期 —— 23:59 跑的那一轮
        # 会把"昨天签过"写进今天的块，第二天直接判定"已完成"，整天不签到。
        day = str(res.get("date") or "")[:10]
        if day and day != self._day_key():
            _log("absorb skipped: 本轮属于 %s，已跨天" % day)
            return
        t = self._today()
        c = res.get("checkin") or {}
        tv = res.get("travel") or {}

        if res.get("checkedIn") or c.get("done"):
            t["checkedIn"] = True
            if res.get("streak") is not None:
                t["streak"] = res.get("streak")
            if res.get("week"):
                t["week"] = list(res.get("week") or [])[:7]
            if res.get("balance") is not None:
                t["balance"] = res.get("balance")

        st = tv.get("state")
        if st in ("traveling", "arrived") or tv.get("departed"):
            t["dispatched"] = True
            if tv.get("location"):
                t["location"] = tv.get("location")
            if st == "traveling":
                if tv.get("arriveAt"):
                    t["arriveAt"] = self._int0(tv.get("arriveAt"))
                elif tv.get("remainingSec"):
                    t["arriveAt"] = int(time.time()) + self._int0(tv.get("remainingSec"))
                if tv.get("departAt"):
                    t["departAt"] = self._int0(tv.get("departAt"))
                if tv.get("reward"):
                    t["pending"] = self._int0(tv.get("reward"))
            elif st == "arrived":
                t["arrived"] = True
                if tv.get("reward"):
                    t["pending"] = self._int0(tv.get("reward"))

        if tv.get("limitReached"):
            # daily_limit_reached 只说明"今天名额用掉了"，不等于"我们看见它派出去了"。
            # 之前这里直接置 dispatched=True，于是没派送的机器也显示"今日已派过"，
            # 再叠加 claimed 就变成"今日已完成"，整天不再去问服务端。
            t["limitReached"] = True
            if st in ("traveling", "arrived") or tv.get("departed"):
                t["dispatched"] = True

        got = self._int0(tv.get("claimed"))
        if got > 0 or tv.get("claimedToday"):
            # claimedToday 是当天累计（_note_travel_claim 维护），直接取它，
            # 免得本轮 claimed 与累计值叠加成双倍
            total = self._int0(tv.get("claimedToday"))
            t["claimed"] = True
            t["reward"] = max(self._int0(t.get("reward")) + got, total)
            t["pending"] = 0
            t["limitReached"] = True
            t.pop("arriveAt", None)     # 已收回，倒计时到此为止
            t.pop("arrived", None)      # 待领取标记一并清掉

    def _travel_view(self, t):
        """用今日结论还原一张旅行卡，字段与接口返回保持一致（前端不用改）。"""
        now = int(time.time())
        reward = self._int0(t.get("reward"))          # 今天已领
        pending = self._int0(t.get("pending"))        # 到家待领
        v = {"error": None, "departed": False, "claimed": 0,
             "claimedToday": reward, "limitReached": bool(t.get("limitReached")),
             "location": "", "reward": pending, "durationHours": 0, "progress": 0.0,
             "remainingSec": 0, "remainingText": "", "unknown": False,
             "canClaim": False, "canDepart": False}
        arrive = self._int0(t.get("arriveAt"))
        # 已领取且没有新的在途行程：今天就算完事了。
        # 若本地还记着一趟没回来的（arriveAt 在未来），以在途为准 ——
        # 否则新派出去的猫会被上一条 claimed 盖成"空闲"。
        if t.get("claimed") and not (arrive and now < arrive):
            v.update(state="idle", stateText="空闲", limitReached=True)
            return v

        if arrive and now < arrive:
            left = arrive - now
            v.update(state="traveling", stateText="旅行中", location=t.get("location") or "",
                     remainingSec=left, remainingText=_dur_text(left))
            dep = self._int0(t.get("departAt"))
            if dep and arrive > dep:
                v["progress"] = max(0.0, min(1.0, (now - dep) / float(arrive - dep)))
            return v

        if arrive or pending:
            # 到点了但还没领：显示"可领取"，让用户知道分没丢
            v.update(state="arrived", stateText="已到家", remainingText="已到家，可领取",
                     location=t.get("location") or "", progress=1.0, reward=pending,
                     canClaim=pending > 0)
            return v

        # 只剩"名额已用"这条线索、没有任何行程细节：如实标注，别冒充"已派满"。
        v.update(state="idle", stateText="空闲",
                 limitReached=bool(t.get("limitReached")),
                 unknown=bool(t.get("limitReached") and not t.get("claimed")))
        return v

    def _merge_result(self, res):
        """首帧用：把今日结论盖在当前 result 上，让界面直接显示今天的事实。"""
        t = self._today(create=False)
        today = self._day_key()
        if not t or not (t.get("checkedIn") or t.get("dispatched")):
            # 没有今日结论时，昨天的旅行状态不能挂到今天头上：
            # 否则一开程序就是"今天已派满"，而其实那是昨天的事。
            if isinstance(res, dict) and res.get("date") and res.get("date") != today:
                out = dict(res)
                out["travel"] = {
                    "state": "unknown", "stateText": "未知", "location": "",
                    "remainingSec": 0, "remainingText": "", "reward": 0,
                    "claimed": 0, "claimedToday": 0, "departed": False,
                    "limitReached": False, "error": None, "progress": 0.0
                }
                return out
            return res

        out = dict(res or {})
        out["date"] = t["date"]
        out["ok"] = True
        if t.get("checkedIn"):
            out["checkedIn"] = True
            out["streak"] = t.get("streak", out.get("streak"))
            out["week"] = t.get("week") or out.get("week") or []
            out["balance"] = t.get("balance", out.get("balance"))
            out["checkin"] = {"done": True, "new": False, "gain": 0,
                              "message": "今天已经签过，没有重复打卡",
                              "error": None, "skipped": True}
        if t.get("dispatched"):
            out["travel"] = self._travel_view(t)
            out["error"] = None
        return out

    def snapshot(self):
        with self.lock:
            try:
                return self._snapshot()
            except Exception as e:
                # 合成失败不能把整个接口打死：退回原始 result，界面至少还有东西显示。
                # 以前这里没兜底，一个坏字段就让前端永远停在「正在准备签到…」。
                _log("snapshot 合成失败：%s" % e)
                return {"status": self.state.get("status"), "result": self.state.get("result"),
                        "at": self.state.get("at"), "error": self.state.get("error"),
                        "running": self._running}

    def _snapshot(self):
        return {
            "status": self.state["status"],
            "result": self._merge_result(self.state["result"]),
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
            self._absorb(res)
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

        # 今天该做的都做完了（签过 + 猫已收回）：本地结论就是最终答案。
        # 连页面都不用叫醒，启动那一刻界面就有正确状态。
        # 只有 claimed 才算"收回"：limitReached 在派出那一刻就为 true，
        # 拿它当已完成会漏掉"到家没领"的那 9 分。
        t = self._today(create=False)
        today_done = bool(t.get("checkedIn")) and bool(t.get("claimed"))
        if today_done:
            res.update({
                "ok": True,
                "checkedIn": True,
                "streak": t.get("streak"),
                "balance": t.get("balance"),
                "week": t.get("week") or [],
                "checkin": {"done": True, "new": False, "gain": 0,
                            "message": "今天已经签过，没有重复打卡",
                            "error": None, "skipped": True},
                "travel": self._travel_view(t),
                "reason": "local_done",
            })
            _log("今日已签已收回，直接用本地结论，跳过远端查询")
            return res

        if not self.account.win:
            res["error"] = "登录窗口不可用，无法签到"
            res["reason"] = "no_window"
            return res
        # 没连过账号就没有可用的网页会话：与其让用户看到"页面未就绪"这种技术黑话，
        # 不如直接说清楚该怎么办。
        if self.account.state.get("status") == "disconnected":
            res["error"] = "还没连接账号。点右上角头像 →「连接账号」，扫一次码就能自动签到"
            res["reason"] = "no_account"
            return res

        # 与定时同步共用一把锁：同步过程中页面正在跳转，此时注入 fetch 会拿到空响应。
        # 拿不到锁说明正好在同步，宁可让用户点一次「重试」，也不要和导航抢页面。
        got = self.account._sync_lock.acquire(timeout=lock_timeout)
        if not got:
            res["error"] = "正在同步云端数据，几秒后再点「重试」"
            res["reason"] = "busy"
            return res
        try:
            if not self._ensure_page():
                res["error"] = "打不开签到页面：多半是网络断了。点「重试」再试一次；一直不行就重新连接账号"
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
                if res["checkin"].get("done") and not res["checkedIn"]:
                    # 刚签成功：data 是**签到前**的状态快照，checkedIn / week 还停在旧值。
                    # 早先直接拿它去渲染，界面就自相矛盾：横幅写「今日签到成功 +100 分」，
                    # 左下角卡片和签到面板却写「待签到」、「本周已签 6/7 天」。
                    # 先按本地结果乐观补上今天的点，再回读一次服务端做校准。
                    res["checkedIn"] = True
                    week = list(res["week"] or [])
                    if len(week) == 7:
                        week[now.weekday()] = True      # 点阵是周一为 0，与界面一致
                    res["week"] = week
                    self._refresh_after_checkin(res)
                elif res["checkin"].get("unknown"):
                    # 服务端 200 但没给 code：不能当成功（会把 signedDate 锁死一整天），
                    # 回读一次状态，服务端说签了就算签了。
                    self._refresh_after_checkin(res)
                    if res["checkedIn"]:
                        res["checkin"]["done"] = True
                        res["checkin"]["new"] = False
                        res["checkin"]["message"] = "今天已经签过，没有重复打卡"

                # 记下"哪一天签成了"，当天余下的轮询就不必再问签到接口
                if res["checkedIn"]:
                    self.state["signedDate"] = today_s
                    self.state["streak"] = res["streak"]
                    self.state["week"] = res["week"]
                    self.state["balance"] = res["balance"]

            if travel_auto:
                res["travel"] = self._do_travel()
                self._note_travel_claim(res["travel"])
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
               "gain": 0, "message": "", "error": None, "unknown": False,
               "dailyCredit": data.get("daily_credit") or 100}
        if out["done"]:
            out["message"] = "今天已经签过，没有重复打卡"
            return out

        r = self._call(CHECKIN_PATH, "POST")
        code = self._code(r)
        if r.get("status") == 200 and code == CODE_OK:
            out["done"] = True
            out["new"] = True
            out["gain"] = out["dailyCredit"]
            out["message"] = "签到成功"
            return out
        if r.get("status") == 200 and code is None:
            # 200 但没有 code：字段可能变了，也可能只是响应结构不同。
            # 直接当成功会把 signedDate 写死一整天，宁可回读一次状态再定论。
            out["unknown"] = True
            out["message"] = "签到请求已发出，服务端没有返回结果码"
            return out
        if code == CODE_ALREADY:
            # 幂等返回：服务端认为已签（多端同时开程序时会遇到），不是错误
            out["done"] = True
            out["message"] = "今天已经签过，没有重复打卡"
            return out
        out["error"] = self._http_hint(r, "签到失败")
        out["message"] = out["error"]
        return out

    def _refresh_after_checkin(self, res):
        """签到成功后回读一次状态，把 checkedIn / week / 连签 / 余额校准成服务端值。

        读不到（网络/会话问题）就保留调用方按本地结果推算的值 —— 至少"今天签过了"是对的，
        不会又变回"待签到"。只做加法：服务端如果还没跟上（today_checked_in 仍为 false），
        不把已经成立的签到结论降级。
        """
        try:
            st = self._call(STATUS_PATH, "POST", retry=0)
            if st.get("status") != 200:
                return
            d = (st.get("json") or {}).get("data") or {}
            if d.get("today_checked_in"):
                res["checkedIn"] = True
            wk = [bool(x) for x in (d.get("week_progress") or [])][:7]
            if len(wk) == 7:
                old = res["week"] if len(res["week"] or []) == 7 else [False] * 7
                res["week"] = [a or b for a, b in zip(wk, old)]
            if d.get("streak_days") is not None:
                res["streak"] = d.get("streak_days")
            if d.get("total_credits") is not None:
                res["balance"] = d.get("total_credits")
        except Exception as e:
            _log("checkin 签到后回读状态失败：%s" % e)

    def _http_hint(self, resp, prefix):
        """把接口错误翻译成"用户能懂 + 知道下一步做什么"的话。

        原则：先给结论，再给一个具体动作；不出现 HTTP 状态码当主语，
        技术细节塞在括号里给懂得人看。
        """
        st = resp.get("status")
        j = resp.get("json") or {}
        msg = j.get("msg") or j.get("message") or resp.get("error") or ""
        if st in (401, 403):
            return ("登录状态过期了。程序会自己重载页面再试一次；"
                    "如果还不行，点右上角头像 →「重新连接」扫一次码")
        if st == -2:
            return ("网络没有响应，可能是断网或代理拦截。"
                    "点「重试」再来一次；如果一直这样，检查一下网络")
        if st == 200:
            return "服务端返回了异常内容（%s）" % (msg or "没有给出原因")
        if isinstance(st, int) and st >= 500:
            return ("服务端出错了（%s），这类问题通常是暂时的，"
                    "过几分钟点「重试」再试" % st)
        if st == 429:
            return "请求太频繁被限流了，等几分钟再试"
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
            before = self._int0(out.get("reward"))
            c = self._call(TRAVEL_CLAIM, "POST", {})
            code = self._code(c)
            if c.get("status") == 200 and code == CODE_OK:
                out["claimed"] = before
                self._reload_travel(out)
            elif c.get("status") == 200 and code is None:
                # HTTP 200 但没给结果码：回读状态，奖励归零才算真领到。
                # 只看 status 会把"没领到"记成"已领取"，接着一整天都不再问 —— 分就白丢了。
                self._reload_travel(out)
                if before and not self._int0(out.get("reward")):
                    out["claimed"] = before
                elif before:
                    out["error"] = "领取请求已发出，服务端没有返回结果码"
            else:
                out["error"] = self._http_hint(c, "领取旅行积分失败")

        # 2) 空闲且今天还能派 → 派出去
        if out["state"] == "idle" and not out["limitReached"] and not out["error"]:
            locs = self._travel_locations()
            if locs:
                pick = random.choice(locs)
                p = self._call(TRAVEL_DEPART, "POST", {"location_id": pick.get("id")})
                pcode = self._code(p)
                if p.get("status") == 200 and pcode in (CODE_OK, CODE_ALREADY):
                    out["departed"] = True
                    out["location"] = pick.get("name") or ""
                    self._reload_travel(out)
                elif p.get("status") == 200 and pcode is None:
                    # 没有结果码：回读状态，只有真的在旅行才敢记"已派出"
                    self._reload_travel(out)
                    out["departed"] = out.get("state") in ("traveling", "arrived")
                else:
                    out["error"] = self._http_hint(p, "派出猫猫失败")
        return out

    def _reload_travel(self, out):
        """回读一次旅行状态，覆盖 out 里的行程字段（领取/派出后的校准）。"""
        r = self._call(TRAVEL_STATUS, "GET", retry=0)
        if r.get("status") == 200:
            self._fill_travel(out, (r.get("json") or {}).get("data") or {})

    # ---------------- 立即检查（只读） ----------------

    def probe(self):
        """立即检查：只读，不签到、不领取、不派送。

        为什么需要它：本地 today 结论命中「签过 + 领过」就整天短路，一旦某次判断错了，
        界面会一直显示「今日已完成」。这个入口绕过短路与冷却，把服务端此刻的事实读回来，
        并用它校准本地结论 —— 本地错了就改本地，而不是继续错一整天。
        """
        out = {"ok": False, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "checkin": None, "travel": None, "error": None}
        if self.account.state.get("autoLogin") is False:
            out["error"] = "已退出登录，先连接账号"
            return out
        if not self.account.win:
            out["error"] = "登录窗口不可用"
            return out
        if self.account.state.get("status") == "disconnected":
            out["error"] = "还没连接账号，先扫码连接"
            return out
        if getattr(self.account, "_login_active", False):
            out["error"] = "正在扫码登录，先完成登录再检查"
            return out
        got = self.account._sync_lock.acquire(timeout=20)
        if not got:
            out["error"] = "正在同步云端数据，几秒后再试"
            return out
        try:
            if not self._ensure_page():
                out["error"] = "打不开账号页面：多半是网络断了"
                return out
            out["ok"] = True
            self._read_truth(out)
            self._reconcile(out)
            self._save_cache()      # 校准后的结论同样要落盘，否则重启就丢
            return out
        finally:
            if got:
                self.account._sync_lock.release()

    def _read_truth(self, out):
        """读签到与猫猫的真实状态写进 out。调用方必须已持有 account._sync_lock。"""
        st = self._call(STATUS_PATH, "POST")
        if st.get("status") == 200:
            d = (st.get("json") or {}).get("data") or {}
            out["checkin"] = {
                "done": bool(d.get("today_checked_in")),
                "streak": d.get("streak_days"),
                "balance": d.get("total_credits"),
                "week": [bool(x) for x in (d.get("week_progress") or [])][:7],
                "dailyCredit": d.get("daily_credit") or 100,
                "error": None,
            }
        else:
            out["checkin"] = {"done": None,
                              "error": self._http_hint(st, "读取签到状态失败")}

        tr = self._call(TRAVEL_STATUS, "GET")
        if tr.get("status") == 200:
            tv = {"state": "unknown", "stateText": "未知", "location": "",
                  "remainingSec": 0, "remainingText": "", "reward": 0,
                  "claimed": 0, "claimedToday": 0, "departed": False,
                  "limitReached": False, "error": None, "progress": 0.0,
                  "arriveAt": 0, "departAt": 0}
            self._fill_travel(tv, (tr.get("json") or {}).get("data") or {})
            tv["canClaim"] = tv.get("state") == "arrived" and self._int0(tv.get("reward")) > 0
            tv["canDepart"] = tv.get("state") == "idle" and not tv.get("limitReached")
            out["travel"] = tv
        else:
            out["travel"] = {"state": "unknown", "stateText": "未知",
                             "error": self._http_hint(tr, "读取猫猫状态失败")}

    # ---------------- 按需执行单个动作 ----------------

    def act(self, kind):
        """执行用户点的那一件：checkin（签到）/ claim（领积分）/ travel（派猫猫）。

        与 run() 的区别：只做一件，不顺手把别的也做了。
        检查（probe）之后由用户决定要不要执行，执行完立刻回读真值并校准本地结论。
        """
        kind = str(kind or "").strip().lower()
        if kind not in ("checkin", "claim", "travel"):
            return {"ok": False, "error": "未知动作"}
        out = {"ok": False, "kind": kind, "message": "", "error": None,
               "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if self.account.state.get("autoLogin") is False:
            out["error"] = "已退出登录，先连接账号"
            return out
        if not self.account.win:
            out["error"] = "登录窗口不可用"
            return out
        if self.account.state.get("status") == "disconnected":
            out["error"] = "还没连接账号，先扫码连接"
            return out
        if getattr(self.account, "_login_active", False):
            out["error"] = "正在扫码登录，先完成登录"
            return out
        # 别让用户在"正在签到…"里干等半分钟：超过 10 秒还没轮到就直接说清楚。
        got = self.account._sync_lock.acquire(timeout=10)
        if not got:
            out["error"] = "云端正在同步，稍等几秒再点一次"
            return out
        try:
            if not self._ensure_page():
                out["error"] = "打不开账号页面：多半是网络断了"
                return out
            if kind == "checkin":
                self._act_checkin(out)
            elif kind == "claim":
                self._act_claim(out)
            else:
                self._act_travel(out)
            # 无论成败都回读一次：界面显示的必须是服务端的事实
            truth = {"ok": True, "checkin": None, "travel": None}
            self._read_truth(truth)
            self._reconcile(truth)
            # 手动动作的结论也要落盘。run() 里会存，act() 少这一句，
            # 结果是"刚签完就退出程序"，下次启动又当没签过重新问一遍。
            self._save_cache()
            return out
        finally:
            if got:
                self.account._sync_lock.release()

    def _act_checkin(self, out):
        st = self._call(STATUS_PATH, "POST")
        if st.get("status") != 200:
            out["error"] = self._http_hint(st, "读取签到状态失败")
            return
        d = (st.get("json") or {}).get("data") or {}
        if d.get("today_checked_in"):
            out.update(ok=True, message="今天已经签过，没有重复打卡")
            return
        r = self._call(CHECKIN_PATH, "POST")
        code = self._code(r)
        gain = d.get("daily_credit") or 100
        if r.get("status") == 200 and code == CODE_OK:
            out.update(ok=True, message="签到成功 +%s 分" % gain)
        elif code == CODE_ALREADY:
            out.update(ok=True, message="今天已经签过，没有重复打卡")
        elif r.get("status") == 200 and code is None:
            out.update(ok=True, message="签到请求已发出，正在确认结果")
        else:
            out["error"] = self._http_hint(r, "签到失败")

    def _act_claim(self, out):
        tv = {"state": "unknown", "stateText": "未知", "location": "", "remainingSec": 0,
              "remainingText": "", "reward": 0, "claimed": 0, "departed": False,
              "limitReached": False, "error": None, "progress": 0.0}
        r = self._call(TRAVEL_STATUS, "GET")
        if r.get("status") != 200:
            out["error"] = self._http_hint(r, "读取猫猫状态失败")
            return
        self._fill_travel(tv, (r.get("json") or {}).get("data") or {})
        if tv.get("state") != "arrived":
            out["error"] = "现在没有可领取的积分（猫猫%s）" % (tv.get("stateText") or "状态未知")
            return
        before = self._int0(tv.get("reward"))
        c = self._call(TRAVEL_CLAIM, "POST", {})
        code = self._code(c)
        if c.get("status") == 200 and code == CODE_OK:
            out.update(ok=True, message=("领到 %s 分" % before) if before else "积分已领取")
        elif c.get("status") == 200 and code is None:
            out.update(ok=True, message="领取请求已发出，正在确认结果")
        elif c.get("status") == 200:
            out["error"] = "服务端没有确认领取（code=%s）" % code
        else:
            out["error"] = self._http_hint(c, "领取旅行积分失败")

    def _act_travel(self, out):
        tv = {"state": "unknown", "stateText": "未知", "location": "", "remainingSec": 0,
              "remainingText": "", "reward": 0, "claimed": 0, "departed": False,
              "limitReached": False, "error": None, "progress": 0.0}
        r = self._call(TRAVEL_STATUS, "GET")
        if r.get("status") != 200:
            out["error"] = self._http_hint(r, "读取猫猫状态失败")
            return
        self._fill_travel(tv, (r.get("json") or {}).get("data") or {})
        if tv.get("state") == "traveling":
            out["error"] = "猫猫还在旅行，等它回来再派"
            return
        if tv.get("state") == "arrived":
            out["error"] = "猫猫已到家，先把积分领了才能再派"
            return
        if tv.get("limitReached"):
            out["error"] = "今天已经派过了，每天只能派 1 次"
            return
        locs = self._travel_locations()
        if not locs:
            out["error"] = "没读到目的地列表，稍后再试"
            return
        pick = random.choice(locs)
        p = self._call(TRAVEL_DEPART, "POST", {"location_id": pick.get("id")})
        code = self._code(p)
        if p.get("status") == 200 and code in (CODE_OK, CODE_ALREADY):
            out.update(ok=True, message="猫猫出发去「%s」了" % (pick.get("name") or "旅行"))
        else:
            out["error"] = self._http_hint(p, "派出猫猫失败")

    def _reconcile(self, res):
        """用刚读到的服务端事实校准今日结论。服务端说什么就是什么，不替它圆场。"""
        with self.lock:
            t = self._today()
            c = res.get("checkin") or {}
            if not c.get("error") and c.get("done") is True:
                t["checkedIn"] = True
                for k in ("streak", "week", "balance"):
                    if c.get(k) is not None:
                        t[k] = c[k]
                self.state["signedDate"] = self._day_key()
                self.state["streak"] = t.get("streak")
                self.state["week"] = t.get("week")
                self.state["balance"] = t.get("balance")
            elif not c.get("error") and c.get("done") is False:
                # 服务端明确说没签：本地那条"今天签过"就是错的，撤掉。
                t["checkedIn"] = False
                if self.state.get("signedDate") == self._day_key():
                    self.state["signedDate"] = None

            tv = res.get("travel") or {}
            st = tv.get("state")
            if not tv.get("error") and st in ("traveling", "arrived", "idle"):
                if st == "traveling":
                    t["dispatched"] = True
                    if tv.get("location"):
                        t["location"] = tv["location"]
                    if tv.get("arriveAt"):
                        t["arriveAt"] = self._int0(tv["arriveAt"])
                    if tv.get("departAt"):
                        t["departAt"] = self._int0(tv["departAt"])
                    # 在途时 reward 是"预计带回来多少"，覆盖旧的待领值
                    t["pending"] = self._int0(tv.get("reward"))
                    t.pop("arrived", None)
                elif st == "arrived":
                    t["dispatched"] = True
                    t["arrived"] = True
                    t["pending"] = self._int0(tv.get("reward"))
                    if tv.get("location"):
                        t["location"] = tv["location"]
                    if tv.get("arriveAt"):
                        t["arriveAt"] = self._int0(tv["arriveAt"])
                else:                                   # idle：今天没在旅行
                    if tv.get("limitReached"):
                        t["dispatched"] = True
                        t["limitReached"] = True
                    else:
                        # 服务端说今天还没派 → 清掉本地"已派出/已领取"的旧结论。
                        # 这是"猫猫没派送却显示今日已完成"的正面纠正。
                        for k in ("dispatched", "limitReached", "claimed", "reward",
                                  "pending", "arriveAt", "arrived", "departAt"):
                            t.pop(k, None)

            # 同步刷新 result 里的旅行快照：_merge_result 在某些分支会直接返回旧 result，
            # 只改 today 的话界面还会显示纠正前那一份。
            rs = self.state.get("result")
            if isinstance(rs, dict):
                rs["travel"] = self._travel_view(t)
                rs["date"] = self._day_key()
                if t.get("checkedIn"):
                    rs["checkedIn"] = True
            self._save_cache()

    def _note_travel_claim(self, travel):
        """把"今天领到多少"记进本地状态。

        为什么要另记一笔：`travel["claimed"]` 只代表**本轮**领到多少，下一轮重建 travel 就归零，
        界面上"今天领了多少"随之消失（重启更是彻底看不到）。这里按天累加写进 checkin.json，
        界面就能一直显示，跨天自动清零。
        """
        today = dt.date.today().isoformat()
        if self.state.get("claimedDate") != today:
            self.state["claimedDate"] = today
            self.state["claimedToday"] = 0
        got = self._int0(travel.get("claimed"))
        if got:
            self.state["claimedToday"] = self._int0(self.state.get("claimedToday")) + got
        travel["claimedToday"] = self._int0(self.state.get("claimedToday"))

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
        # 绝对时刻要一起存：只有相对秒数的话，重启后会被当成"从现在起还剩 228 分钟"
        out["arriveAt"] = int(arrive) if arrive else 0
        out["departAt"] = int(depart) if depart else 0


def _dur_text(sec):
    """把秒数说成人话：38 分钟后 / 1 小时 12 分钟后。"""
    if sec <= 0:
        return "即将到家"
    m = int(round(sec / 60.0))
    if m < 60:
        return "%d 分钟后" % max(1, m)
    h, m2 = divmod(m, 60)
    return "%d 小时%s后" % (h, (" %d 分钟" % m2) if m2 else "")
