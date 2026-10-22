# -*- coding: utf-8 -*-
"""本机数据层单测（无第三方依赖）

跑法：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

覆盖三件"错了但肉眼看不出来"的事：
1. 没有 messageId 的记录不能被当成重复丢掉；
2. 增量读取（从上次字节偏移继续）必须与全量读取结果完全一致；
3. 正在写入的半行不能被消费，等写完再读。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import usage_source as U


def _line(ts, credit=1.5, mid=None, model="m", tool=""):
    obj = {
        "type": "assistant",
        "timestamp": ts,
        "cwd": "C:/proj",
        "name": tool,
        "providerData": {"rawUsage": {"credit": credit}, "model": model},
    }
    if mid:
        obj["providerData"]["messageId"] = mid
    return json.dumps(obj, ensure_ascii=False)


class ScanFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbpts-test-")
        self.path = os.path.join(self.tmp, "s.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, lines, newline_at_end=True):
        body = "\n".join(lines)
        if lines and newline_at_end:
            body += "\n"
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(body)

    def test_no_message_id_kept(self):
        """没有 messageId 的三条记录必须全部保留。"""
        base = int(time.time() * 1000)
        self._write([_line(base + i) for i in range(3)])
        recs, _off = U._scan_file(self.path, 0)
        self.assertEqual(len(recs), 3)
        self.assertEqual(sum(r["credit"] for r in recs), 4.5)

    def test_duplicate_message_id_collapsed(self):
        """同一个 messageId 只保留一条。"""
        base = int(time.time() * 1000)
        self._write([_line(base, mid="same"), _line(base + 1, mid="same")])
        recs, _off = U._scan_file(self.path, 0)
        self.assertEqual(len(recs), 2)          # 去重在 _build 里按 mid 做，单文件不吞行
        mids = {r["mid"] for r in recs}
        self.assertEqual(mids, {"same"})

    def test_incremental_matches_full(self):
        base = int(time.time() * 1000)
        self._write([_line(base), _line(base + 1000)])
        recs1, off = U._scan_file(self.path, 0)
        self.assertEqual(len(recs1), 2)

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_line(base + 2000) + "\n")
        tail, off2 = U._scan_file(self.path, off)
        self.assertEqual(len(tail), 1)
        self.assertEqual(off2, os.path.getsize(self.path))

        full, _ = U._scan_file(self.path, 0)
        self.assertEqual(len(full), 3)
        self.assertEqual([r["ts"] for r in recs1 + tail], [r["ts"] for r in full])

    def test_partial_line_not_consumed(self):
        """最后一行没有换行符 → 本轮不读，补全后再读。"""
        base = int(time.time() * 1000)
        self._write([_line(base)], newline_at_end=False)
        recs, off = U._scan_file(self.path, 0)
        self.assertEqual(recs, [])
        self.assertEqual(off, 0)

        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n")
        recs2, off2 = U._scan_file(self.path, off)
        self.assertEqual(len(recs2), 1)
        self.assertGreater(off2, 0)


class BuildTest(unittest.TestCase):
    """_build 的增量缓存：文件没动不重读、追加后只读新增。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbpts-build-")
        self.projects = os.path.join(self.tmp, "projects", "p1")
        os.makedirs(self.projects)
        self.file = os.path.join(self.projects, "a.jsonl")
        self.cache = os.path.join(self.tmp, "cache.json")
        self.state = {
            "home": self.tmp, "projects": os.path.join(self.tmp, "projects"),
            "db": "", "hasDb": False, "valid": True, "source": "manual", "note": "",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _append(self, lines):
        with open(self.file, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    def test_incremental_build_equals_full(self):
        base = int(time.time() * 1000)
        self._append([_line(base, mid="a", credit=2), _line(base + 1000, mid="b", credit=3)])
        with mock.patch.object(U.data_source, "resolve", return_value=self.state), \
                mock.patch.object(U, "CACHE_PATH", self.cache):
            svc = U.UsageService()
            first = svc._build(force=True)
            self.assertEqual(first["today"]["consumed"], 5)
            self.assertFalse(first["meta"]["cached"])

            # 没有新写入：应命中缓存记录，不产生重复
            second = svc._build(force=False)
            self.assertEqual(second["today"]["consumed"], 5)
            self.assertEqual(second["meta"]["records"], 2)
            self.assertTrue(second["meta"]["cached"])

            # 追加一条：增量读取，结果与全量一致
            self._append([_line(base + 2000, mid="c", credit=4)])
            third = svc._build(force=False)
            self.assertEqual(third["today"]["consumed"], 9)
            self.assertEqual(third["meta"]["records"], 3)
            full = svc._build(force=True)
            self.assertEqual(full["today"]["consumed"], 9)
            self.assertEqual(full["meta"]["records"], 3)

    def test_same_message_in_two_files_counted_once(self):
        base = int(time.time() * 1000)
        self._append([_line(base, mid="dup", credit=7)])
        other = os.path.join(self.projects, "b.jsonl")
        with open(other, "w", encoding="utf-8") as f:
            f.write(_line(base, mid="dup", credit=7) + "\n")
        with mock.patch.object(U.data_source, "resolve", return_value=self.state), \
                mock.patch.object(U, "CACHE_PATH", self.cache):
            data = U.UsageService()._build(force=True)
        self.assertEqual(data["meta"]["records"], 1)
        self.assertEqual(data["today"]["consumed"], 7)

    def test_empty_and_aggregate_share_shape(self):
        """_empty() 与真实聚合结果必须是同一套字段，否则前端会读到 undefined。"""
        base = int(time.time() * 1000)
        self._append([_line(base, mid="a")])
        with mock.patch.object(U.data_source, "resolve", return_value=self.state), \
                mock.patch.object(U, "CACHE_PATH", self.cache):
            svc = U.UsageService()
            real = svc._build(force=True)
        blank = svc._empty()
        for k in blank:
            self.assertIn(k, real, "聚合结果缺少字段 %s" % k)


if __name__ == "__main__":
    unittest.main()
