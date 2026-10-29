# -*- coding: utf-8 -*-
"""云端口径单测：去重、截断、对账、退出登录。

跑法：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

这些逻辑决定界面上每个数字，出错时不会报异常、只会"悄悄算错"，
所以必须有断言兜住。
"""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import account_service as A


def _bill(rows, used=10.0):
    return {
        "ok": True,
        "summary": {"json": {"data": {
            "TotalCredits": 100,
            "CreditGroups": [{
                "Kind": "plan", "Name": "套餐包",
                "CycleTotalCapacity": 100,
                "CycleRemainCapacity": 100 - used,
                "CycleUsedCapacity": used,
            }],
            "Packages": [{"PackageCode": "c1"}],
            "SubscriptionPackageName": "专业版",
            "IsPaidUser": True,
        }}},
        "usage": {"json": {"data": {"data": rows, "total": len(rows)}}},
        "free": {"json": {"data": {"Accounts": []}}},
        "paid": {"json": {"data": {"Accounts": []}}},
        "identity": {"data": {"accounts": [{"nickname": "n", "uid": "u", "avatarUrl": "", "phoneNumber": ""}]}},
    }


def _row(t, rid, credit=1.0, model="m", purpose="agent", client=""):
    return {"requestTime": t, "requestId": rid, "credit": credit,
            "model": model, "agentPurpose": purpose, "client": client}


class MapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbpts-map-")
        self.cache = os.path.join(self.tmp, "account.json")
        self.cookie = os.path.join(self.tmp, "session.bin")
        self.patches = [
            mock.patch.object(A, "CACHE", self.cache),
            mock.patch.object(A, "COOKIE_FILE", self.cookie),
        ]
        for p in self.patches:
            p.start()
        self.svc = A.AccountService()
        self.today = dt.date.today().strftime("%Y-%m-%d")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_content_different_request_id_collapsed(self):
        """云端会给同一笔扣费换 requestId 重复返回，按内容去重后只能算一次。"""
        rows = [
            _row(self.today + " 10:00:00", "r1", 2.0),
            _row(self.today + " 10:00:00", "r2", 2.0),
        ]
        out = self.svc._map(_bill(rows, used=2.0))
        self.assertEqual(len(out["usageRows"]), 1)
        self.assertEqual(out["daily"][0]["points"], 2.0)
        self.assertEqual(out["daily"][0]["calls"], 1)

    def test_daily_uses_all_rows_not_truncated_list(self):
        """超过 1500 条时：明细列表可以截断，但每日合计必须按全量算。"""
        base = dt.datetime.combine(dt.date.today(), dt.time(0, 0, 0))
        rows = []
        for i in range(1600):
            t = base + dt.timedelta(seconds=i * 30)
            rows.append(_row(t.strftime("%Y-%m-%d %H:%M:%S"), "r%d" % i, 0.5))
        out = self.svc._map(_bill(rows, used=10.0))

        self.assertEqual(len(out["usageRows"]), 1500)          # 明细上限
        self.assertAlmostEqual(out["daily"][0]["points"], 800.0, places=2)
        self.assertEqual(out["daily"][0]["calls"], 1600)       # 统计不漏
        self.assertAlmostEqual(out["cloud"]["rowsSum"], 800.0, places=2)

    def test_reconcile_exposed(self):
        """逐笔合计与账号自报"已用"的差额必须写进返回值，而不是假装一致。"""
        rows = [_row(self.today + " 11:00:00", "r1", 12.0)]
        out = self.svc._map(_bill(rows, used=10.0))
        cloud = out["cloud"]
        self.assertAlmostEqual(cloud["rowsSum"], 12.0, places=2)
        self.assertAlmostEqual(cloud["drift"], 2.0, places=2)
        self.assertAlmostEqual(cloud["driftPct"], 0.2, places=4)

    def test_prev_rows_merged_without_duplicates(self):
        """增量拉取只覆盖最近几天，旧记录必须从缓存里并回来且不重复。"""
        old_t = (dt.date.today() - dt.timedelta(days=5)).strftime("%Y-%m-%d") + " 09:00:00"
        prev = [{"id": "old1", "t": old_t, "credit": 3.0, "model": "m", "purpose": "agent"}]
        patch_old = (dt.date.today() - dt.timedelta(days=4)).strftime("%Y-%m-%d") + " 09:00:00"
        rows = [
            _row(old_t, "old1", 3.0),          # 与缓存同一条
            _row(patch_old, "new1", 5.0),      # 新拉的
        ]
        out = self.svc._map(_bill(rows, used=8.0), prev_rows=prev)
        self.assertEqual(len(out["usageRows"]), 2)
        self.assertAlmostEqual(out["cloud"]["rowsSum"], 8.0, places=2)

    def test_reconcile_only_counts_current_cycle(self):
        """对账必须裁到本周期：周期开始前的消耗算进来会让差额虚高 4 倍。"""
        start = (dt.date.today() - dt.timedelta(days=2)).strftime("%Y-%m-%d")
        before = (dt.date.today() - dt.timedelta(days=6)).strftime("%Y-%m-%d") + " 10:00:00"
        bill = _bill([_row(before, "old", 20.0),
                      _row(self.today + " 10:00:00", "new", 5.0)], used=5.0)
        bill["free"] = {"json": {"data": {"Accounts": [{
            "CycleStartTime": start + " 00:00:00", "CapacityRemain": 100}]}}}
        out = self.svc._map(bill)
        self.assertAlmostEqual(out["cloud"]["rowsSum"], 5.0, places=2, msg="周期外的 20 分不应计入")
        self.assertAlmostEqual(out["cloud"]["drift"], 0.0, places=2)
        self.assertEqual(out["cloud"]["cycleRows"], 1)

    def test_count_kind_detects_buckets(self):
        """云端按 5 分钟聚合时，条数是"笔"不是"次"，前端要据此换单位。"""
        bucket_rows = [_row(self.today + " 21:00:00", "r1"),
                       _row(self.today + " 20:55:00", "r2")]
        self.assertEqual(self.svc._map(_bill(bucket_rows))["cloud"]["countKind"], "bucket")

        minute_rows = [_row(self.today + " 21:03:00", "r1"),
                       _row(self.today + " 20:47:00", "r2")]
        self.assertEqual(self.svc._map(_bill(minute_rows))["cloud"]["countKind"], "request")

    def test_client_kept_on_rows(self):
        """逐笔的客户端来源必须留住：丢了就永远分不清 WorkBuddy 与 CodeBuddy。"""
        rows = [_row(self.today + " 10:00:00", "r1", 1.0, client="workbuddy"),
                _row(self.today + " 10:05:00", "r2", 2.0, client="codebuddy")]
        out = self.svc._map(_bill(rows, used=3.0))
        got = sorted(r["client"] for r in out["usageRows"])
        self.assertEqual(got, ["codebuddy", "workbuddy"])

    def test_cloud_clients_split(self):
        """按来源聚合：积分/笔数/按天/按模型各算各的，合计仍等于总量。"""
        rows = [
            _row(self.today + " 10:00:00", "r1", 1.0, model="a", client="workbuddy"),
            _row(self.today + " 10:05:00", "r2", 2.0, model="a", client="workbuddy"),
            _row(self.today + " 10:10:00", "r3", 4.0, model="b", client="codebuddy"),
        ]
        out = self.svc._map(_bill(rows, used=7.0))
        cc = out["cloudClients"]
        self.assertEqual(sorted(cc), ["codebuddy", "workbuddy"])
        self.assertAlmostEqual(cc["workbuddy"]["points"], 3.0, places=2)
        self.assertEqual(cc["workbuddy"]["calls"], 2)
        self.assertAlmostEqual(cc["codebuddy"]["points"], 4.0, places=2)
        self.assertEqual(cc["workbuddy"]["days"][0]["date"], self.today)
        self.assertAlmostEqual(cc["codebuddy"]["models"][0]["points"], 4.0, places=2)
        self.assertAlmostEqual(sum(v["points"] for v in cc.values()),
                               out["cloud"]["rowsSum"], places=2)

    def test_rows_without_client_keep_amount(self):
        """旧缓存里的行没有 client：归到未标注，金额一条都不能少（否则历史合计变小）。"""
        old_t = (dt.date.today() - dt.timedelta(days=5)).strftime("%Y-%m-%d") + " 09:00:00"
        prev = [{"id": "old1", "t": old_t, "credit": 3.0, "model": "m", "purpose": "agent"}]
        out = self.svc._map(_bill([_row(self.today + " 10:00:00", "r1", 5.0, client="codebuddy")],
                                  used=8.0), prev_rows=prev)
        self.assertAlmostEqual(out["cloud"]["rowsSum"], 8.0, places=2)
        self.assertAlmostEqual(out["cloudClients"][""]["points"], 3.0, places=2)
        self.assertAlmostEqual(out["cloudClients"]["codebuddy"]["points"], 5.0, places=2)

    def test_old_rows_get_client_from_new_pull(self):
        """旧缓存行没有来源：同一条记录重新拉回来时必须把 client 补上，而不是被去重丢掉。"""
        t = self.today + " 10:00:00"
        prev = [{"id": "old", "t": t, "credit": 3.0, "model": "m", "purpose": "agent"}]
        out = self.svc._map(_bill([_row(t, "old", 3.0, client="CodeBuddyIDE")], used=3.0),
                            prev_rows=prev)
        self.assertEqual(len(out["usageRows"]), 1, "同一条记录不该变成两条")
        self.assertEqual(out["usageRows"][0]["client"], "CodeBuddyIDE")
        self.assertAlmostEqual(out["cloud"]["rowsSum"], 3.0, places=2)

    def test_usage_since_full_window_when_client_missing(self):
        """缓存里还有没标来源的行 → 下次同步补拉完整窗口，否则历史永远补不上 client。"""
        import datetime as _dt
        full = (_dt.date.today() - _dt.timedelta(days=29)).isoformat()
        recent = _dt.date.today().isoformat()
        self.svc.state["daily"] = [{"date": recent, "points": 1, "calls": 1}]
        self.svc.state["usageRows"] = [{"id": "x", "t": recent + " 10:00:00",
                                        "credit": 1.0, "model": "m", "purpose": "agent"}]
        self.assertEqual(self.svc._usage_since(), full)

        self.svc.state["usageRows"][0]["client"] = "WorkBuddy"
        self.assertTrue(self.svc._usage_since() > full, "全都标了来源就回到增量窗口")

    def test_probe_logs_client_values(self):
        """探针只写结构（字段名与条数），不写单笔金额。"""
        rows = [_row(self.today + " 10:00:00", "r1", 9.99, client="codebuddy")]
        with mock.patch.object(A, "_log") as lg:
            A._probe_sources(_bill(rows))
        text = "\n".join(str(c.args[0]) for c in lg.call_args_list)
        self.assertIn("codebuddy", text)
        self.assertIn("requestTime", text)
        self.assertNotIn("9.99", text)

    def test_logout_really_disconnects(self):
        """退出登录必须删掉会话存档并记住"不要自动回填"。"""
        with open(self.cookie, "wb") as f:
            f.write(b"ciphertext")
        self.svc.win = None
        self.svc.logout()

        self.assertFalse(os.path.exists(self.cookie), "session.bin 应被删除")
        with open(self.cache, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertTrue(saved.get("autoLogin") is False, "应持久化 autoLogin=False")
        self.assertFalse(self.svc.try_restore(), "已退出登录时不应自动回填")


if __name__ == "__main__":
    unittest.main()
