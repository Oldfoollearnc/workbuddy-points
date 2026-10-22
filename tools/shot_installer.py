# -*- coding: utf-8 -*-
"""
安装向导截图（验收用，不参与打包）。

做法：启动安装包 → 找到顶层窗口 → 全屏抓图后按窗口矩形裁剪 →
若卡在"已安装版本"确认框，先按 Enter 关掉再到欢迎页截一张 → 关闭进程。

用法：
    python tools/shot_installer.py installer/Output/WorkBuddyPoints-Setup-1.7.1.exe logs/shots
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

from PIL import ImageGrab

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def visible_windows(keyword):
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if keyword.lower() in buf.value.lower():
            found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def rect_of(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def shot(hwnd, path):
    l, t, r, b = rect_of(hwnd)
    pad = 8
    img = ImageGrab.grab(bbox=(l - pad, t - pad, r + pad, b + pad), all_screens=True)
    img.save(path)
    print("saved", path, img.size)
    return path


def press_enter():
    VK_RETURN = 0x0D
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, 2, 0)


def main():
    if len(sys.argv) < 2:
        print("usage: shot_installer.py <setup.exe> [outdir]")
        return 2
    exe = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else "logs/shots")
    os.makedirs(out, exist_ok=True)

    print("elevated:", bool(ctypes.windll.shell32.IsUserAnAdmin()))
    p = subprocess.Popen([exe])

    deadline = time.time() + 25
    wins = []
    while time.time() < deadline:
        wins = visible_windows("WorkBuddy")
        if wins:
            break
        time.sleep(0.4)

    if not wins:
        print("未找到安装向导窗口")
        p.kill()
        return 1

    hwnd, title = wins[-1]
    print("window:", hex(hwnd), repr(title))
    time.sleep(1.2)
    shot(hwnd, os.path.join(out, "01-first.png"))

    # 有旧版本时会先弹确认框：回车选"是"（继续安装），再到欢迎页
    press_enter()
    time.sleep(1.5)
    wins = visible_windows("WorkBuddy")
    if wins:
        hwnd, title = wins[-1]
        print("after enter:", hex(hwnd), repr(title))
        shot(hwnd, os.path.join(out, "02-welcome.png"))

    # 不真装：直接结束安装进程
    subprocess.run(["taskkill", "/PID", str(p.pid), "/F"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
