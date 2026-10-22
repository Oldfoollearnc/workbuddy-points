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


def _row(t, rid, credit=1.0, model="m", purpose="agent"):
    return {"requestTime": t, "requestId": rid, "credit": credit,
            "model": model, "agentPurpose": purpose}


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
