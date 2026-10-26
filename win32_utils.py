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
    """把主窗位置限制在**整块屏幕可见**的范围内。

    以前是"至少留 160px 可抓"，结果窗口能被拖到屏幕外大半：
    再点右下角小窗唤回主窗时，窗口虽然显示出来了，却在工作区之外，
    看起来就是"打不开 / 卡住"。现在整窗都不许出屏。
    """
    vl, vt, vr, vb = virtual_screen()
    max_x = max(vl, vr - int(w))
    max_y = max(vt, vb - int(h))
    x = int(min(max(vl, x), max_x))
    y = int(min(max(vt, y), max_y))
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


WM_GETMINMAXINFO = 0x0024
WS_MAXIMIZE = 0x01000000


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT), ("ptMaxPosition", POINT),
                ("ptMinTrackSize", POINT), ("ptMaxTrackSize", POINT)]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]


def _frame_pad():
    """WS_THICKFRAME 那圈不可见边框的厚度（含 padded border）。"""
    try:
        return (user32.GetSystemMetrics(32) + user32.GetSystemMetrics(92),   # SM_CXSIZEFRAME + SM_CXPADDEDBORDER
                user32.GetSystemMetrics(33) + user32.GetSystemMetrics(92))
    except Exception:
        return (8, 8)


def _snap_wndproc(hwnd, msg, wparam, lparam):
    """窗口过程：压平标题栏，并让最大化正好落在工作区里。"""
    old = _SNAP_OLD.get(hwnd)
    try:
        if msg == WM_NCCALCSIZE and wparam:
            # 非最大化：客户区 = 整个窗口（没有标题栏）。
            # 最大化：系统给的建议矩形是"工作区 + 一圈边框"，不内缩就会盖住任务栏。
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            if style & WS_MAXIMIZE:
                p = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
                bx, by = _frame_pad()
                p.rgrc[0].left += bx
                p.rgrc[0].top += by
                p.rgrc[0].right -= bx
                p.rgrc[0].bottom -= by
            return 0
        if msg == WM_GETMINMAXINFO:
            # 只把最大化尺寸限定到工作区；边框补偿交给上面的 NCCALCSIZE
            if old:
                user32.CallWindowProcW(old, hwnd, msg, wparam, lparam)
            else:
                user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            mon = user32.MonitorFromWindow(hwnd, 2)      # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                m, wa = mi.rcMonitor, mi.rcWork
                info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
                info.ptMaxPosition.x = wa.left - m.left
                info.ptMaxPosition.y = wa.top - m.top
                info.ptMaxSize.x = wa.right - wa.left
                info.ptMaxSize.y = wa.bottom - wa.top
            return 0
    except Exception:
        pass
    try:
        if old:
            return user32.CallWindowProcW(old, hwnd, msg, wparam, lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    except Exception:
        return 0


# ---------------- 系统关机 ----------------

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
SM_SHUTTINGDOWN = 0x2000

_shutdown_proc = None          # WNDPROC 必须常驻，否则被 GC 后窗口消息直接崩
_shutdown_old = {}             # hwnd -> 原子类化前的窗口过程
_shutdown_cb = {}              # hwnd -> 关机回调


def shutting_down():
    """系统是否正在走关机/注销流程。"""
    try:
        return bool(user32.GetSystemMetrics(SM_SHUTTINGDOWN))
    except Exception:
        return False


def _shutdown_wndproc(hwnd, msg, wparam, lparam):
    if msg in (WM_QUERYENDSESSION, WM_ENDSESSION):
        cb = _shutdown_cb.get(hwnd)
        if cb:
            try:
                cb()
            except Exception as e:
                _log("shutdown callback error: %s" % e)
        # 一律放行：我们是"收起到托盘"的程序，没有任何需要保存的状态，
        # 拦下关机只会让 Windows 弹「此应用阻止关机」。
        return 1
    old = _shutdown_old.get(hwnd)
    try:
        if old:
            # argtypes 里声明的是 c_void_p，必须显式包一层，直接传 int 会 TypeError
            return user32.CallWindowProcW(ctypes.c_void_p(old), hwnd, msg, wparam, lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    except Exception:
        return 0


def watch_shutdown(hwnd, on_ending):
    """让窗口在系统关机时立刻放行，并在放行前回调 on_ending。

    背景：关闭按钮被做成"收起托盘"，pywebview 的 closing 回调会拦下 WM_CLOSE。
    关机时系统先发 WM_QUERYENDSESSION，这条消息走的是窗口过程而不是 pywebview 的
    closing 事件，子类化后就能在这里抢先应答 —— 否则 Windows 等 5 秒仍收不到答复，
    就会弹「此应用阻止关机」。
    """
    global _shutdown_proc
    if not hwnd:
        return False
    try:
        if hwnd in _shutdown_old:
            _shutdown_cb[hwnd] = on_ending      # 回调可替换，不必重复子类化
            return True
        try:
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            setter = user32.SetWindowLongPtrW
        except Exception:
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            setter = user32.SetWindowLongW
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                           wintypes.WPARAM, wintypes.LPARAM]
        if _shutdown_proc is None:
            _shutdown_proc = WNDPROC(_shutdown_wndproc)
        old = setter(hwnd, GWL_WNDPROC, ctypes.cast(_shutdown_proc, ctypes.c_void_p))
        if not old:
            return False
        _shutdown_old[hwnd] = old
        _shutdown_cb[hwnd] = on_ending
        _log("watch_shutdown: hooked hwnd=%s" % hwnd)
        return True
    except Exception as e:
        _log("watch_shutdown error: %s" % e)
        return False


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
