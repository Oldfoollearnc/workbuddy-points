# -*- coding: utf-8 -*-
"""文件写入的小工具。

只解决一件事：**别让"写文件写到一半"被读到**。

`open(path, "w")` 会立刻把目标文件截断成 0 字节，接着 `json.dump` 是分块写的。
本地刷新和定时扫描是两条线程，读的那一刻很可能正好落在中间，于是读到
`{"ver": 5, "home": "C:\\..."` 这样的半截 JSON —— 日志里的
`cache read error: Expecting ',' delimiter` 就是这么产生的。

做法：写到同目录的 `.tmp`，`fsync` 落盘，再 `os.replace` 原子替换。
Windows 上同卷的 `os.replace` 是原子的，读到的要么是旧文件、要么是新文件。
"""
import json
import os
import time


def save_json(path, data, indent=None):
    """原子写 JSON。失败会抛异常，由调用方决定是否吞掉。"""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass          # 某些网络盘不支持 fsync，忽略即可
    # Windows 上如果目标文件正被读（Python 的 open 不带 FILE_SHARE_DELETE），
    # os.replace 会抛 PermissionError。读只持续几毫秒，重试几次就过去了。
    for _ in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.03)
    # 极端情况：一直被占用。退回直接覆盖 —— 宁可冒着被读到半截的风险，
    # 也不能让这次的数据完全不落盘（读端本身有解析失败兜底）。
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    try:
        os.remove(tmp)
    except OSError:
        pass


def load_json(path, default=None, retries=2):
    """读 JSON；文件不存在或损坏都返回 default。

    偶发解析失败多半是"写端正好在替换文件"，隔几十毫秒重读一次通常就好了
    （Windows 上写端口按 `os.replace` 失败时会退回直接覆盖，那一瞬间可能被读端撞上）。
    """
    for attempt in range(retries + 1):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception:
            if attempt >= retries:
                return default
            time.sleep(0.05)
