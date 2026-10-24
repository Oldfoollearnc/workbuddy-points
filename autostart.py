# -*- coding: utf-8 -*-
"""开机自启动：读写 HKCU\\...\\Run。

与安装器（installer/WorkBuddyPoints.iss）用同一个值名 `WorkBuddyPoints`，
所以安装时勾了「开机自动启动」，装完进设置页看到的就是开着的，不会两套状态打架。

写进注册表的命令一律带 `--tray`：开机时不弹窗，只驻留托盘 + 后台跑签到。
安装器以前的写法漏了这个参数，描述写"最小化到托盘"、实际却会弹出主窗。
"""
import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WorkBuddyPoints"


def launch_command():
    """返回要写进注册表的完整命令行。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：sys.executable 就是 WorkBuddyPoints.exe
        return '"%s" --tray' % sys.executable

    # 源码运行：用 pythonw.exe 拉起 desktop.py，避免每次开机弹一个黑框
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    script = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop.py"))
    return '"%s" "%s" --tray' % (exe, script)


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            try:
                val, _ = winreg.QueryValueEx(k, VALUE_NAME)
                return bool(val)
            except FileNotFoundError:
                return False
    except OSError:
        return False


def enable():
    cmd = launch_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except (FileNotFoundError, OSError):
        pass


def set_enabled(on):
    """开/关自启动，返回当前命令行（关掉时为空串）。"""
    if on:
        return enable()
    disable()
    return ""
