# -*- coding: utf-8 -*-
"""Windows 底层工具：DPI、几何、窗口样式、单实例唤醒。

为什么单独拆出来：
`desktop.py` 原来 1432 行，混着 Win32 调用、窗口编排、托盘、API 桥、启动流程五件事，
改任何一个功能都要在一堆 ctypes 里翻。这里收拢的是**完全不依赖业务对象**的部分：
纯函数，输入句柄/坐标，输出结果或改窗口样式，没有全局状态，也没有回调。

拆分原则：函数名与签名一律不改，`desktop.py` 直接 import 使用，
行为与拆分前逐字一致。
"""
import ctypes
import os
from ctypes import wintypes

import app_paths
import logutil

# ---------------- 日志（与 desktop 同一路径，避免两份 app.log） ----------------
_LOG_PATH = os.path.join(app_paths.data_dir(), "logs", "app.log")


def _log(msg):
    logutil.log(_LOG_PATH, msg)


# ---------------- 常量 ----------------

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

KEEP_VISIBLE_X = 160     # 水平方向至少保留这么宽在屏幕内，避免窗口"跑丢"
KEEP_VISIBLE_Y = 48      # 垂直方向至少保留标题栏高度

GWL_STYLE = -16
GWL_WNDPROC = -4
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WM_NCCALCSIZE = 0x0083

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)
_SNAP_PROC = None
_SNAP_OLD = {}

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9

# 第二次双击图标时给已运行实例的信号
SHOW_EVENT = "Local\\WorkBuddyPointsInsightShow"


# ---------------- 结构 ----------------

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


# ---------------- DLL ----------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [wintypes.POINT]     # 必须用 wintypes.POINT，和调用方一致
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL


# ---------------- 几何 ----------------

def dpi_scale():
    try:
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def work_area():
    r = RECT()
    try:
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen():
    """整个虚拟桌面（多显示器时包含负坐标屏），用于判断窗口是否还在可见范围。"""
    try:
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w > 0 and h > 0:
            return x, y, x + w, y + h
    except Exception:
        pass
    return work_area()


def clamp_main_pos(x, y, w, h):
    """把主窗位置限制在"还能抓得到"的范围里。

    拖到屏幕外不是致命的，但用户找不回来就只能重启 —— 所以水平方向至少留
    KEEP_VISIBLE_X 宽、垂直方向保证标题栏（顶部 KEEP_VISIBLE_Y）始终在屏内。
    """
    vl, vt, vr, vb = virtual_screen()
    x = int(max(vl - w + KEEP_VISIBLE_X, min(x, vr - KEEP_VISIBLE_X)))
    y = int(max(vt, min(y, vb - KEEP_VISIBLE_Y)))
    return x, y


# ---------------- 窗口 ----------------

def set_app_id():
    """让任务栏/开始菜单按应用而不是 python.exe 分组与显示图标。

    注意：SetCurrentProcessExplicitAppUserModelID 由 shell32 导出，kernel32 里没有
    （此前写在 kernel32 上会静默失败，任务栏因此一直显示 Python 图标）。
    """
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        hr = shell32.SetCurrentProcessExplicitAppUserModelID("WorkBuddy.Points.Insight")
        _log(f"set_app_id hr={hr}")
    except Exception as e:
        _log(f"set_app_id error: {e}")


def find_hwnd(title):
    hwnd = user32.FindWindowW(None, title)
    return hwnd or None


def on_taskbar(pt):
    """光标是否落在任务栏/通知区域上。

    托盘图标也算"小窗外面的点击"，但它的语义是"我要看小窗"，不是"点外面收起"。
    这里两条判断：
    1) 窗口类名（Shell_TrayWnd / TrayNotifyWnd / 溢出区…）；
    2) 光标在该显示器的"工作区之外、显示器之内"——任务栏无论放上下左右都能盖住。
    """
    try:
        hwnd = user32.WindowFromPoint(pt)
        if hwnd:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            cls = buf.value or ""
            if cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "TrayNotifyWnd",
                       "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland") \
                    or "Tray" in cls or "Overflow" in cls:
                return True
        mon = user32.MonitorFromPoint(pt, 2)      # MONITOR_DEFAULTTONEAREST
        if mon:
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                in_mon = (mi.rcMonitor.left <= pt.x < mi.rcMonitor.right
                          and mi.rcMonitor.top <= pt.y < mi.rcMonitor.bottom)
                in_work = (mi.rcWork.left <= pt.x < mi.rcWork.right
                           and mi.rcWork.top <= pt.y < mi.rcWork.bottom)
                if in_mon and not in_work:
                    return True
    except Exception:
        pass
    return False


def round_corners(hwnd):
    """Win11：给无边框窗口加系统圆角（DWMWCP_ROUND）。Win10 会直接返回错误码，忽略即可。"""
    try:
        pref = ctypes.c_int(2)          # DWMWCP_ROUND
        rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), 33,     # DWMWA_WINDOW_CORNER_PREFERENCE
            ctypes.byref(pref), ctypes.sizeof(pref))
        return rc == 0
    except Exception as e:
        _log(f"round_corners error: {e}")
        return False


def strip_nonclient(hwnd):
    """确保窗口没有任何非客户区。

    曾经的实现给无边框窗口补 WS_THICKFRAME 来恢复"拖边缩放"，代价是 Windows 会画一圈
    非客户区边框（深色主题下是 8px 近黑色带），也就是用户看到的"顶部黑边"。
    现在改为前端自绘缩放把手（见 app.js `setupResize`），窗口必须保持纯客户区。
    """
    try:
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        # 曾试过把 WS_THICKFRAME 加回来 + DwmExtendFrameIntoClientArea 做边缘吸附，
        # 实测两个后果：窗口顶部出现一条深色边框，且拖到边缘依旧不吸附
        # （pywebview 的窗口类没有配 WM_NCCALCSIZE，系统拿到的仍是"有非客户区"的窗口）。
        # 真正要做需要替换窗口过程处理 NCCALCSIZE，风险大于收益，先回到纯客户区。
        want = style & ~(WS_THICKFRAME | WS_MAXIMIZEBOX | WS_CAPTION)
        if style != want:
            user32.SetWindowLongW(hwnd, GWL_STYLE, want)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            _log(f"strip_nonclient: style {hex(style)} -> {hex(want)}")

        # 顺带补圆角：主窗去掉边框后是纯直角，Win11 下用 DWM 恢复系统圆角
        if round_corners(hwnd):
            _log("round_corners: ok")
    except Exception as e:
        _log(f"strip_nonclient error: {e}")


def enable_snap_support(hwnd):
    """让无边框窗口重新获得 Windows 贴边布局（拖边分屏 / Win+方向键 / 双击最大化）。

    为什么必须两件事一起做：
      只加 WS_THICKFRAME，系统会画一圈非客户区（深色主题下就是用户看到的「顶部黑边」），
      而且拖到边缘仍然不吸附 —— 窗口类没处理 WM_NCCALCSIZE，系统仍认为它有非客户区。
      所以这里同时子类化窗口过程，把 WM_NCCALCSIZE 的客户区压成整个窗口：
      样式上它是标准可缩放窗口，视觉上仍然没有标题栏。
    失败返回 False，窗口保持原来的纯客户区行为，不影响启动。
    """
    global _SNAP_PROC
    try:
        if os.environ.get("WB_NO_SNAP"):    # 出问题时的回退开关：置 1 即恢复纯客户区
            return False
        if hwnd in _SNAP_OLD:
            return True
        if _SNAP_PROC is None:
            _SNAP_PROC = WNDPROC(_snap_wndproc)
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                           wintypes.WPARAM, wintypes.LPARAM]

        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        want = (style | WS_THICKFRAME | WS_CAPTION | WS_MAXIMIZEBOX) & ~WS_SYSMENU
        if style != want:
            user32.SetWindowLongW(hwnd, GWL_STYLE, want)
        old = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, ctypes.cast(_SNAP_PROC, ctypes.c_void_p))
        if not old:
            return False
        _SNAP_OLD[hwnd] = old
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        round_corners(hwnd)      # 去掉标题栏后仍是直角，Win11 用 DWM 补回圆角
        _log("snap support on: style %s" % hex(want))
        return True
    except Exception as e:
        _log("enable_snap_support error: %s" % e)
        return False


def _snap_wndproc(hwnd, msg, wparam, lparam):
    """窗口过程：只拦 WM_NCCALCSIZE，其余原样转回原过程。"""
    try:
        if msg == WM_NCCALCSIZE and wparam:
            return 0
    except Exception:
        pass
    old = _SNAP_OLD.get(hwnd)
    try:
        if old:
            return user32.CallWindowProcW(old, hwnd, msg, wparam, lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    except Exception:
        return 0


# ---------------- 单实例 ----------------


def single_instance():
    """同一时刻只允许一个实例，避免两个进程争用 WebView2 用户数据目录。"""
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, "Local\\WorkBuddyPointsInsight")
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return handle, already


def signal_show():
    """第二个实例：给已经跑着的实例发一个"亮一下"的信号。"""
    try:
        kernel32.OpenEventW.restype = wintypes.HANDLE
        kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        h = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)   # EVENT_MODIFY_STATE
        if not h:
            return False
        try:
            kernel32.SetEvent(h)
        finally:
            kernel32.CloseHandle(h)
        return True
    except Exception as e:
        _log(f"signal_show failed: {e}")
        return False
