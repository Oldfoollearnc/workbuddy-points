# -*- coding: utf-8 -*-
"""第二轮审计回归测试：配置容错、DB 兜底、周期起点、httpOnly Cookie 清理。"""
import datetime as dt
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import account_service as A
import usage_source as U


class ConfigRobustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbpts-cfg-")
        self.path = os.path.join(self.tmp, "points_config.json")
        self.p = mock.patch.object(U, "CONFIG_PATH", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bad_cover_days_keeps_quota(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"total": 1000, "cycle_start": "2026-10-01", "cover_days": "bad"}')
        cfg = U._load_config()
        self.assertEqual(cfg["total"], 1000.0)
        self.assertEqual(cfg["cycle_start"], "2026-10-01")
        self.assertEqual(cfg["cover_days"], 0)


class DbFallbackTest(unittest.TestCase):
    def test_full_date_rows_are_counted(self):
        svc = U.UsageService.__new__(U.UsageService)
        svc.source = {"home": "C:/x", "db": "C:/x/workbuddy.db", "note": ""}
        today = dt.date.today().isoformat()
        data = svc._from_db({"daily": [{"date": today, "points": 88.0, "calls": 12}]})
        self.assertEqual(data["today"]["consumed"], 88.0)
        self.assertEqual(data["today"]["requests"], 12)
        self.assertEqual(data["daily"][-1]["consumed"], 88.0)


class CycleStartTest(unittest.TestCase):
    def test_exhausted_package_start_is_kept(self):
        svc = A.AccountService.__new__(A.AccountService)
        row = {"requestTime": dt.date.today().isoformat() + " 10:00:00",
               "requestId": "r1", "credit": 1.0, "model": "m"}
        bill = {
            "summary": {"json": {"data": {"TotalCredits": 10, "CreditGroups": []}}},
            "free": {"json": {"data": {"Accounts": [
                {"CycleStartTime": "2026-10-01 00:00:00", "CycleCapacityRemainPrecise": 0},
                {"CycleStartTime": "2026-10-20 00:00:00", "CycleCapacityRemainPrecise": 5},
            ]}}},
            "paid": {"json": {"data": {"Accounts": []}}},
            "usage": {"json": {"data": {"data": [row], "total": 1}}},
            "identity": {"json": {"data": {"accounts": []}}},
        }
        out = A.AccountService._map(svc, bill)
        self.assertEqual(out["cloud"]["cycleStartText"], "2026-10-01")


class PurgeCookiesTest(unittest.TestCase):
    def test_purges_only_login_domains(self):
        tmp = tempfile.mkdtemp(prefix="wbpts-cookie-")
        db = os.path.join(tmp, "Cookies")
        con = sqlite3.connect(db)
        con.execute("create table cookies (host_key text, name text)")
        con.executemany("insert into cookies values (?,?)", [
            ("www.workbuddy.cn", "session"),
            (".workbuddy.cn", "KEYCLOAK_SESSION"),
            ("www.codebuddy.cn", "x"),
            ("example.com", "keep"),
        ])
        con.commit()
        con.close()
        marker = os.path.join(tmp, "logout.pending")
        with open(marker, "w") as f:
            f.write("1")
        with mock.patch.object(A, "COOKIE_DB", db), \
                mock.patch.object(A, "LOGOUT_MARKER", marker):
            n = A.purge_pending_cookies()
            self.assertEqual(n, 3)
            self.assertFalse(os.path.exists(marker))
        con = sqlite3.connect(db)
        left = con.execute("select host_key from cookies").fetchall()
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(left, [("example.com",)])


if __name__ == "__main__":
    unittest.main()
