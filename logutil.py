# -*- coding: utf-8 -*-
"""统一日志：追加写 + 单文件 2MB 轮转（保留一份 .1）。

为什么单独抽出来：
以前 desktop / account_service 各写各的日志函数，都是纯 append、永不轮转，
`app.log` 只能靠手动删。轮转逻辑只有一份，才好保证三处行为一致；
tag 用来区分是哪个模块写的，省掉每个调用点自己拼前缀。
"""
import datetime as dt
import os

LIMIT = 2 * 1024 * 1024     # 单个日志文件上限 2MB


def log(path, msg, tag=""):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            if os.path.getsize(path) > LIMIT:
                os.replace(path, path + ".1")     # 覆盖上一份 .1，最多留两代
        except OSError:
            pass
        head = "[%s]" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if tag:
            head += " [%s]" % tag
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (head, msg))
    except Exception:
        # 日志失败不能反过来影响主流程
        pass


def size(path):
    """日志当前大小（含轮转出来的 .1），给设置页显示用。"""
    total = 0
    for p in (path, path + ".1"):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def clear(path):
    """清空日志（连同 .1）：旧记录一起删掉，排障时不会被历史噪音干扰。

    只截断不删文件：还能继续 append，也不用重新判断目录权限。
    """
    try:
        for p in (path, path + ".1"):
            try:
                os.remove(p)
            except OSError:
                pass
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "w", encoding="utf-8").close()
        return True
    except Exception:
        return False
