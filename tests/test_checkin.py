# -*- coding: utf-8 -*-
"""签到 / 猫猫的「今日结论」缓存。

这一块以前零测试，改一次就只能靠临时脚本 + 手动开关程序验证；
把几个关键场景固化下来，至少保证"重启后不会把旧状态当成现在"。
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import checkin_service as cs  # noqa: E402


class FakeAccount:
    """_run_inner 只用到 state / win；win=None 表示"没有窗口"，走本地短路时不该碰它。"""

    def __init__(self):
        self.state = {"autoLogin": True, "status": "connected"}
        self.win = None


class TodayCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_cache = cs.CACHE
        cs.CACHE = os.path.join(self.tmp, "checkin.json")
        self.today = dt.date.today().isoformat()

    def tearDown(self):
        cs.CACHE = self.orig_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------

    def write_cache(self, travel, at=None, date=None, claimed_today=0, cache_today=None):
        now = dt.datetime.now()
        at = at or now.strftime("%Y-%m-%d %H:%M:%S")
        date = date or self.today
        data = {
            "at": at,
            "result": {"ok": True, "at": at, "date": date,
                       "checkin": {"done": True, "new": False, "gain": 0},
                       "travel": travel, "checkedIn": True,
                       "week": [True] * 7, "streak": 9, "balance": 900},
            "options": {"auto": True, "travel": True},
            "signedDate": date, "streak": 9, "week": [True] * 7, "balance": 900,
            "claimedDate": date if claimed_today else None,
            "claimedToday": claimed_today,
        }
        if cache_today is not None:
            data["today"] = cache_today
        with open(cs.CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def service(self):
        return cs.CheckinService(FakeAccount())

    # ---------- 场景 ----------

    def test_traveling_keeps_absolute_arrival(self):
        """上次退出时猫还在路上：重启后剩余时间应≈原值，而不是被当成"从现在起算"。"""
        self.write_cache({"state": "traveling", "location": "咖啡馆",
                          "remainingSec": 228 * 60, "limitReached": True,
                          "reward": 6, "progress": .2})
        travel = self.service().snapshot()["result"]["travel"]
        self.assertEqual(travel["state"], "traveling")
        self.assertAlmostEqual(travel["remainingSec"] / 60, 228, delta=2)
        self.assertTrue(travel["limitReached"])

    def test_stale_snapshot_becomes_arrived(self):
        """同一条缓存放到 6 小时后启动：猫早到家了，不能再显示"228 分钟后回来"。"""
        old = (dt.datetime.now() - dt.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        self.write_cache({"state": "traveling", "location": "咖啡馆",
                          "remainingSec": 228 * 60, "limitReached": True, "reward": 6}, at=old)
        travel = self.service().snapshot()["result"]["travel"]
        self.assertEqual(travel["state"], "arrived")
        self.assertEqual(travel["reward"], 6)
        self.assertEqual(travel["remainingSec"], 0)

    def test_done_day_short_circuits_without_network(self):
        """已签 + 已收回：整轮跳过远端（win=None 也能返回，说明没有进页面）。"""
        self.write_cache({"state": "idle", "limitReached": True, "reward": 0,
                          "claimed": 0, "claimedToday": 6}, claimed_today=6)
        svc = self.service()
        out = svc._run_inner(travel_auto=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["reason"], "local_done")
        self.assertEqual(out["travel"]["state"], "idle")
        self.assertEqual(out["travel"]["claimedToday"], 6)

    def test_yesterday_never_leaks_into_today(self):
        """跨天：昨天的"已派满"不能挂到今天头上。"""
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        self.write_cache({"state": "idle", "limitReached": True,
                          "claimedToday": 6, "reward": 0}, date=yday, claimed_today=6)
        travel = self.service().snapshot()["result"]["travel"]
        self.assertEqual(travel["state"], "unknown")

    def test_no_cache_returns_none(self):
        self.assertIsNone(self.service().snapshot()["result"])

    def test_claim_not_counted_twice(self):
        """领取金额按天累计：落盘重载后仍是 6，不会变成 12。"""
        svc = self.service()
        svc._absorb({"checkedIn": True, "streak": 9, "week": [True] * 7, "balance": 900,
                     "checkin": {"done": True},
                     "travel": {"state": "traveling", "location": "咖啡馆", "reward": 6,
                                "limitReached": True, "claimedToday": 0, "departed": True,
                                "remainingSec": 13680, "arriveAt": int(time.time()) + 13680,
                                "departAt": int(time.time())}})
        svc._save_cache()
        again = self.service()
        again._absorb({"checkedIn": True, "travel": {"state": "idle", "limitReached": True,
                                                     "claimed": 6, "claimedToday": 6, "reward": 0}})
        again._save_cache()
        self.assertEqual(self.service().snapshot()["result"]["travel"]["claimedToday"], 6)

    def test_cross_day_resets_today_block(self):
        """today 块按天滚动：昨天的记录不会参与今天的合成。"""
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        svc = self.service()
        svc.state["today"] = {"date": yday, "checkedIn": True, "dispatched": True}
        block = svc._today()
        self.assertEqual(block["date"], self.today)
        self.assertNotIn("checkedIn", block)

    # ---------- 子代理评审补的边界 ----------

    def test_absorb_ignores_previous_day_result(self):
        """23:59 开跑、00:00 之后收尾的那一轮，不能写进"今天"的块。"""
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        svc = self.service()
        svc._absorb({"date": yday, "checkedIn": True, "checkin": {"done": True},
                     "travel": {"state": "idle", "limitReached": True, "claimedToday": 6}})
        block = svc.state.get("today") or {}
        self.assertFalse(block.get("checkedIn"))
        self.assertFalse(block.get("claimed"))

    def test_limit_reached_alone_does_not_short_circuit(self):
        """派出那一刻 limitReached 就是 true：没领到就不能算"今日已完成"。"""
        self.write_cache({"state": "idle", "limitReached": True, "reward": 0,
                          "claimed": 0, "claimedToday": 0})
        out = self.service()._run_inner(travel_auto=True)
        self.assertNotEqual(out.get("reason"), "local_done")
        self.assertEqual(out.get("reason"), "no_window")

    def test_bad_types_do_not_break_snapshot(self):
        """缓存里的数字被写成时间串也要能出快照，不能让接口整体抛错。"""
        self.write_cache({}, cache_today={
            "date": self.today, "checkedIn": True, "dispatched": True,
            "arriveAt": "2026-10-25 10:00:00", "departAt": None,
            "reward": "6", "pending": "abc",
        })
        travel = self.service().snapshot()["result"]["travel"]
        self.assertIn(travel["state"], ("arrived", "idle", "traveling"))
        self.assertEqual(travel["claimedToday"], 6)


if __name__ == "__main__":
    unittest.main()
