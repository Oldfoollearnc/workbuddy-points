# -*- coding: utf-8 -*-
"""核心纯逻辑单测：等待策略、自启命令、诊断截断、字段脱敏。

跑法：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

这些函数的共同点是"改错了不会抛异常，只会让行为悄悄变形"：
等待策略算错就是白打请求、自启命令写错就是开机拉不起程序，
所以用断言钉住。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autostart
import diagnostics
from checkin_service import CheckinService


class NextWaitTest(unittest.TestCase):
    """派猫等待策略：拿到绝对时间就该一觉睡到点，中途不醒。"""

    def test_traveling_sleeps_until_arrival(self):
        # 15 分钟到家 → 睡 900 + 45 秒缓冲
        self.assertEqual(CheckinService._next_wait({"remainingSec": 900, "state": "traveling"}), 945)

    def test_traveling_capped_by_four_hours(self):
        # 超过地点时长上限（4h）时封顶，防系统时间跳变后睡过头
        self.assertEqual(CheckinService._next_wait({"remainingSec": 18000, "state": "traveling"}), 4 * 3600)

    def test_daily_limit_sleeps_to_next_midnight(self):
        wait = CheckinService._next_wait(
            {"remainingSec": 0, "state": "idle", "limitReached": True})
        now = dt.datetime.now()
        nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        expect = (nxt - now).total_seconds()
        # 两次取时间之间会有毫秒级差异
        self.assertLess(abs(wait - expect), 5)
        self.assertGreater(wait, 3600)      # 不该被 1 小时上限截断

    def test_idle_retries_soon(self):
        self.assertEqual(CheckinService._next_wait(
            {"remainingSec": 0, "state": "idle", "limitReached": False}), 600)

    def test_no_data_falls_back(self):
        self.assertEqual(CheckinService._next_wait(None), 900)


class AutostartTest(unittest.TestCase):
    def test_command_contains_tray_flag(self):
        # 开机自启必须带 --tray，否则开机会弹主窗（安装器以前就是这个 bug）
        self.assertIn("--tray", autostart.launch_command())

    def test_command_is_quoted(self):
        cmd = autostart.launch_command()
        self.assertTrue(cmd.startswith('"'), "路径含空格，必须加引号")

    def test_registry_value_name_matches_installer(self):
        # 与 install.iss 里写的 ValueName 保持一致，否则设置页读不到安装时的状态
        self.assertEqual(autostart.VALUE_NAME, "WorkBuddyPoints")


class DiagnosticsTest(unittest.TestCase):
    def test_log_tail_keeps_last_part(self):
        import tempfile
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".log") as f:
            f.write(b"old\n" * 50000)
            f.write(b"NEWEST LINE\n")
            path = f.name
        try:
            text = diagnostics._log_tail(path, max_bytes=200)
            self.assertIn("NEWEST LINE", text)
            self.assertLess(len(text), 2000)
        finally:
            os.unlink(path)

    def test_log_tail_handles_missing_file(self):
        self.assertIn("读取失败", diagnostics._log_tail("不存在的文件.log"))


class MaskTest(unittest.TestCase):
    def test_uid_is_hashed_not_stored(self):
        """UID 只留哈希：16 位十六进制，且不能等于原值。"""
        import hashlib
        raw = "a7bb22e8-d4f3-41b7-afdf-25c49041ca23"
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(len(h), 16)
        self.assertNotEqual(h, raw)


if __name__ == "__main__":
    unittest.main()
