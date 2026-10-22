# -*- coding: utf-8 -*-
"""
WorkBuddy 积分观测台 · Windows 桌面启动器

形态
----
1. 启动后常驻系统托盘，不占用屏幕；
2. 单击托盘图标 → 屏幕右下角弹出快捷面板（无边框、置顶、固定尺寸，严格贴边）；
3. 面板「展开完整面板」→ 全尺寸主界面，主界面保留「收起为小窗」；
4. 关闭任一窗口均收起到托盘，托盘菜单「退出」才结束进程。

数据
----
由 usage_source.UsageService 读取本机 WorkBuddy 会话记录，经 js_api 提供给界面。

运行
----
python desktop.py            # 托盘常驻（默认）
python desktop.py --show     # 启动即弹出小窗
python desktop.py --main     # 启动即打开主界面
"""
import ctypes
import faulthandler
import json
import datetime as dt
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

# 本机无独立显卡：关闭 WebView2 的 GPU 合成，静态界面 CPU 更稳、显存占用更低
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--disable-gpu --disable-gpu-compositing --remote-debugging-port=9333 --remote-allow-origins=*")

import webview

import app_paths
import data_source
import usage_source
from account_service import AccountService
from tray import Tray

# 资源随程序走，数据写到可写目录：安装版在 Program Files 下也能正常读写日志与缓存
BASE = app_paths.bundle_dir()
INDEX = "file:///" + os.path.join(BASE, "index.html").replace("\\", "/")
ICON = os.path.join(BASE, "assets", "icon.ico")
LOG_DIR = os.path.join(app_paths.data_dir(), "logs")
LOG_PATH = os.path.join(LOG_DIR, "app.log")
VERSION_FILE = os.path.join(BASE, "version.json")

POPUP_TITLE = "WorkBuddy 积分观测台"
MAIN_TITLE = "WorkBuddy 积分观测台 · 完整面板"

POP_W, POP_H = 424, 656      # 客户区目标尺寸
MAIN_W, MAIN_H = 1408, 916
MIN_W, MIN_H = 1024, 680     # 自绘缩放的最小尺寸（与 create_window(min_size) 一致）
MARGIN = 8                   # 距工作区右下角（逻辑像素）

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long

LOGIN_TITLE = "WorkBuddy 账号登录"
WM_SETICON = 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
HWND_TOPMOST = -1
SWP_SHOWWINDOW = 0x0040
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9

VK_LBUTTON = 0x01
HWND_TOP = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

DRAG_TICK = 0.008        # 拖动循环轮询间隔（秒）≈120Hz，足够跟手
KEEP_VISIBLE_X = 160     # 水平方向至少保留这么宽在屏幕内，避免窗口"跑丢"
KEEP_VISIBLE_Y = 48      # 垂直方向至少保留标题栏高度

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_CAPTION = 0x00C00000


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


# ---------------- 基础工具 ----------------

def log(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def read_version():
    """读取 version.json（每次 git 提交由 .githooks/pre-commit 自动递增）。"""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("version") or "1.0.0"
    except Exception:
        return "1.0.0"


def alert(title, text):
    """pythonw 无控制台，异常必须以对话框形式让用户看到。"""
    try:
        user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


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


def set_app_id():
    """让任务栏/开始菜单按应用而不是 python.exe 分组与显示图标。

    注意：SetCurrentProcessExplicitAppUserModelID 由 shell32 导出，kernel32 里没有
    （此前写在 kernel32 上会静默失败，任务栏因此一直显示 Python 图标）。
    """
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        hr = shell32.SetCurrentProcessExplicitAppUserModelID("WorkBuddy.Points.Insight")
        log(f"set_app_id hr={hr}")
    except Exception as e:
        log(f"set_app_id error: {e}")


def find_hwnd(title):
    hwnd = user32.FindWindowW(None, title)
    return hwnd or None


user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [wintypes.POINT]     # 注意必须用 wintypes.POINT，和调用方一致
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]


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
        log(f"round_corners error: {e}")
        return False


def strip_nonclient(hwnd):
    """确保窗口没有任何非客户区。

    曾经的实现给无边框窗口补 WS_THICKFRAME 来恢复"拖边缩放"，代价是 Windows 会画一圈
    非客户区边框（深色主题下是 8px 近黑色带），也就是用户看到的"顶部黑边"。
    现在改为前端自绘缩放把手（见 app.js `setupResize`），窗口必须保持纯客户区。
    """
    try:
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        want = style & ~(WS_THICKFRAME | WS_MAXIMIZEBOX | WS_CAPTION)
        if style != want:
            user32.SetWindowLongW(hwnd, GWL_STYLE, want)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            log(f"strip_nonclient: style {hex(style)} -> {hex(want)}")
        # 顺带补圆角：主窗去掉边框后是纯直角，Win11 下用 DWM 恢复系统圆角
        if round_corners(hwnd):
            log("round_corners: ok")
    except Exception as e:
        log(f"strip_nonclient error: {e}")


def set_window_icons():
    try:
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    except Exception:
        pass
    for title in (POPUP_TITLE, MAIN_TITLE, LOGIN_TITLE):
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            continue
        round_corners(hwnd)
        for kind, size in ((1, 32), (0, 16)):     # ICON_BIG / ICON_SMALL
            hicon = user32.LoadImageW(None, ICON, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, kind, hicon)


def single_instance():
    """同一时刻只允许一个实例，避免两个进程争用 WebView2 用户数据目录。"""
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, "Local\\WorkBuddyPointsInsight")
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return handle, already


class Desktop:
    def __init__(self):
        self.popup = None
        self.main = None
        self.tray = None
        self.quitting = False
        self.close_to_tray = True
        self.scale = dpi_scale()
        self.pop_size = (POP_W, POP_H)
        # 小窗状态簿记：意图（而非可见性）驱动托盘单击的开/关，避免与"点外部收起"互相打架
        self._popup_intent = False
        self._popup_hidden_at = 0.0
        self._hidden_by_watcher_at = 0.0
        self._popup_shown_at = 0.0
        self._popup_lock = threading.Lock()   # 串行化 show/hide，连点不会互相踩
        self._collapsing = False
        self.usage = usage_source.UsageService()
        self.account = AccountService()

    # ---------- 构建 ----------

    def build(self):
        wa = work_area()
        s = self.scale
        avail_w = (wa[2] - wa[0]) / s
        avail_h = (wa[3] - wa[1]) / s

        pw, ph = POP_W, POP_H
        if avail_h < ph + 80:
            ratio = max(0.6, (avail_h - 80) / ph)
            pw, ph = int(pw * ratio), int(avail_h - 80)
        self.pop_size = (pw, ph)

        main_w = int(min(MAIN_W, avail_w - 60))
        main_h = int(min(MAIN_H, avail_h - 60))
        main_x = int((avail_w - main_w) / 2)
        main_y = int(max(20, (avail_h - main_h) / 2))

        self.pop_x = int(avail_w - pw - MARGIN)
        self.pop_y = int(avail_h - ph - MARGIN)
        api = Api(self)
        # 窗口请求尺寸预留不可见边框，展示前会按客户区精确校正
        self.popup = webview.create_window(
            POPUP_TITLE,
            INDEX + "#popup",
            js_api=api,
            width=pw + 24,
            height=ph + 48,
            x=self.pop_x,
            y=self.pop_y,
            resizable=False,
            frameless=True,
            easy_drag=False,
            on_top=True,
            hidden=True,
            shadow=False,
            background_color="#FFFFFF",
            text_select=False,
            zoomable=False,
        )
        self.main = webview.create_window(
            MAIN_TITLE,
            INDEX + "#main",
            js_api=api,
            width=main_w,
            height=main_h,
            x=main_x,
            y=main_y,
            min_size=(1024, 680),
            frameless=True,          # 自绘标题栏：去掉 Windows 原生标题，避免两套标题重复
            easy_drag=False,
            shadow=False,
            hidden=True,
            background_color="#EEF3FC",
        )
        self.main.events.closing += self._on_main_closing

        # 账号登录窗：常驻在 codebuddy.cn 域内，登录后隐藏，仅供同源取数
        self.login_win = webview.create_window(
            "WorkBuddy 账号登录",
            "about:blank",
            js_api=api,
            width=460,
            height=760,
            min_size=(380, 560),
            hidden=True,
            background_color="#FFFFFF",
        )
        self.login_win.events.closing += self._on_login_closing
        self.account.attach(self.login_win)
        threading.Thread(target=self._watch_minimize, daemon=True).start()
        threading.Thread(target=self._watch_popup_click, daemon=True).start()
        log(f"windows created, scale={s}, popup={pw}x{ph}, main={main_w}x{main_h}")

    # ---------- 显隐 ----------

    def show_popup(self):
        if not self.popup:
            return
        with self._popup_lock:
            # 意图先落地：即使后面 show() 还没返回，下一次单击也能正确判成"该关"
            self._popup_intent = True
            self._popup_shown_at = time.time()
            # 主窗与小窗互斥：同一时间只展示一个
            try:
                if self.main:
                    self.main.hide()
            except Exception:
                pass
            mh = user32.FindWindowW(None, MAIN_TITLE)
            if mh:
                user32.ShowWindow(mh, 0)

            hwnd = user32.FindWindowW(None, POPUP_TITLE)
            # 连击判定：刚收起不到 450ms 又打开 → 不播入场动画，否则一开一关会"闪"
            calm = (self._popup_shown_at - getattr(self, "_popup_hidden_at", 0)) > 0.45
            if hwnd:
                # 关键：尺寸/位置在**隐藏状态**下就校正到位，再显示。
                # 以前是先 show 再 align + 1px 抖动，用户看到的就是"出现→跳一下"的闪烁。
                self._align_popup_once(hwnd)
                self._nudge(hwnd)
            try:
                self.popup.show()
            except Exception as e:
                log(f"popup.show error: {e}")
            if hwnd:
                # 置顶 + 前台：否则会被其他窗口遮住，看起来像"没有打开"
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
                try:
                    user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
                except Exception:
                    pass
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                self._align_popup(hwnd)    # 显示后只校正"确有偏差"的情况，正常情况下不动
                self._safe_js(self.popup, "window.App && App.beforeShow()")
                if calm:
                    self._safe_js(self.popup, "window.App && App.playEnter('popup')")
                else:
                    self._safe_js(self.popup, "window.App && App.burstShake()")
                self._fg_at_show = int(user32.GetForegroundWindow() or 0)
        threading.Timer(0.35, self._refresh_popup_layout).start()
        threading.Timer(0.15, set_window_icons).start()

    # ---------- 点小窗外部收起 ----------

    def _watch_popup_click(self):
        """鼠标左键"松开"时判断落点：不在小窗矩形内就收起。

        为什么不用 GetForegroundWindow / window.blur：
        从托盘单击弹出时，焦点常常闪一下又回到任务栏（Windows 的前台限制），
        按焦点判断会出现"一点开就消失"。而鼠标落点与焦点无关，判定稳定。
        用"松开"而不是"按下"：不影响用户在小窗内拖选，也不会打断外部拖拽。
        """
        log("popup click watcher started")
        # None = 还没采样过；第一次只做基线，避免把"弹出前那一下点击的松开"当成外部点击
        prev_down = None
        while not getattr(self, "quitting", False):
            time.sleep(0.045)
            try:
                hwnd = user32.FindWindowW(None, POPUP_TITLE)
                if not hwnd or not user32.IsWindowVisible(hwnd):
                    prev_down = None
                    continue
                down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)   # VK_LBUTTON
                if prev_down is None:
                    prev_down = down
                    continue
                released = prev_down and not down
                prev_down = down
                if not released:
                    continue
                # 刚弹出的一瞬间，把"打开它的那一下点击"忽略掉，
                # 否则从托盘点开时，松手早于监听采样就会被判成"点了外面"而立刻收起。
                if time.time() - getattr(self, "_popup_shown_at", 0) < 0.35:
                    continue
                pt = wintypes.POINT()
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    continue
                # 点任务栏（托盘图标就在上面）交给 toggle_popup 处理，
                # 这里不能当成"点外面"收起，否则托盘连点会变成"收起→又弹出"的闪烁。
                if on_taskbar(pt):
                    continue
                rc = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rc))
                inside = rc.left <= pt.x < rc.right and rc.top <= pt.y < rc.bottom
                if not inside:
                    log(f"popup outside click ({pt.x},{pt.y}) -> hide")
                    self._hidden_by_watcher_at = time.time()
                    self.hide_popup()
            except Exception as e:
                log(f"popup click watcher error: {e}")

    def hide_popup(self):
        with self._popup_lock:
            try:
                if self.popup:
                    self.popup.hide()
            except Exception:
                pass
            hwnd = user32.FindWindowW(None, POPUP_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 0)
            self._popup_intent = False
            self._popup_hidden_at = time.time()

    def show_main(self):
        # 小窗让位（同时把小窗状态簿记成"已收起"）
        self.hide_popup()
        try:
            self.main.show()
            self.main.restore()
        except Exception as e:
            log(f"main.show error: {e}")
        hwnd = user32.FindWindowW(None, MAIN_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 5)
            if not getattr(self, "resize_frame_on", False):
                strip_nonclient(hwnd)
                self.resize_frame_on = True
            self._ensure_on_screen(hwnd)
            self._nudge(hwnd)
            try:
                user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            self._safe_js(self.main,
                          "window.App && (App.beforeShow(), App.playEnter('main'))")
        threading.Timer(0.2, lambda: self._safe_js(self.main, "App.refreshLayout()")).start()
        threading.Timer(0.3, set_window_icons).start()

    def _ensure_on_screen(self, hwnd):
        """主窗只剩一条边留在屏幕外/完全跑丢时，拉回工作区居中。

        "拖没了只能重启软件"是最伤体验的失败模式：这里给它兜底，
        用户点托盘「打开主界面」就能把窗口找回来。
        """
        try:
            rc = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            w, h = rc.right - rc.left, rc.bottom - rc.top
            vl, vt, vr, vb = virtual_screen()
            lost = (rc.right < vl + 120 or rc.left > vr - 120
                    or rc.bottom < vt + 60 or rc.top > vb - 60)
            if not lost:
                return
            wa = work_area()
            nx = wa[0] + max(0, ((wa[2] - wa[0]) - w) // 2)
            ny = wa[1] + max(0, ((wa[3] - wa[1]) - h) // 2)
            user32.SetWindowPos(hwnd, None, nx, ny, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            log(f"ensure_on_screen: ({rc.left},{rc.top}) -> ({nx},{ny})")
        except Exception as e:
            log(f"ensure_on_screen error: {e}")

    def hide_main(self):
        try:
            if self.main:
                self.main.hide()
        except Exception:
            pass
        hwnd = user32.FindWindowW(None, MAIN_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 0)

    # ---------- 自绘标题栏（最小化 / 最大化 / 关闭） ----------

    def win_state(self):
        hwnd = find_hwnd(MAIN_TITLE)
        maximized = bool(getattr(self, "_maxed", False))
        if hwnd:
            try:
                maximized = maximized or bool(user32.IsZoomed(hwnd))
            except Exception:
                pass
        return {"ok": True, "maximized": maximized, "hwnd": int(hwnd or 0)}

    def win_pos(self):
        """自绘标题栏拖动/缩放用：返回窗口左上角与尺寸（无边框后即客户区）。"""
        hwnd = find_hwnd(MAIN_TITLE)
        try:
            if not hwnd:
                return None
            rc = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            return {"x": rc.left, "y": rc.top, "w": rc.right - rc.left, "h": rc.bottom - rc.top}
        except Exception:
            return None

    def win_resize(self, x, y, w, h):
        """自绘缩放把手：一次调用直接给出新的窗口矩形（含最小尺寸保护）。"""
        hwnd = find_hwnd(MAIN_TITLE)
        try:
            if not hwnd:
                return {"ok": False}
            w = max(MIN_W, int(w))
            h = max(MIN_H, int(h))
            if getattr(self, "_maxed", False):
                self._maxed = False
            user32.SetWindowPos(hwnd, None, int(x), int(y), w, h,
                                SWP_NOZORDER | SWP_NOACTIVATE)
            return {"ok": True, "w": w, "h": h}
        except Exception:
            return {"ok": False}

    def win_move(self, x, y, w=0, h=0):
        """绝对定位（旧路径，保留给没有 win_drag_start 的旧前端）。"""
        hwnd = find_hwnd(MAIN_TITLE)
        try:
            if not hwnd:
                return {"ok": False}
            if getattr(self, "_maxed", False):      # 拖动即还原
                self._maxed = False
            rc = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            x, y = clamp_main_pos(int(x), int(y), rc.right - rc.left, rc.bottom - rc.top)
            user32.SetWindowPos(hwnd, None, x, y, int(w or 0), int(h or 0),
                                SWP_NOZORDER | SWP_NOACTIVATE | (0 if (w and h) else SWP_NOSIZE))
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def win_drag_start(self):
        """开始拖动主窗：原生循环（GetCursorPos + SetWindowPos），立刻返回。

        为什么不在前端算位移（旧实现）：
        1. 光标一旦离开窗口矩形，WebView2 就不再派发 mousemove，窗口停在原地；
           光标再靠近时按"累计位移"补一次，窗口会瞬移 —— 表现就是"鼠标一靠近窗口，
           窗口反而跑到后面/跑不见了"，严重时整窗移出屏幕，只能重启。
        2. 前端 screenX 是 CSS 像素，SetWindowPos 是 Win32 坐标，DPI 缩放不一致时
           窗口会比鼠标跑得快，越拖越偏。
        原生循环里两者在同一坐标空间，且窗口 1:1 跟随光标，不会出现上述问题。
        """
        hwnd = find_hwnd(MAIN_TITLE)
        if not hwnd:
            return {"ok": False}
        if getattr(self, "_dragging", False):
            return {"ok": True, "busy": True}
        self._dragging = True
        threading.Thread(target=self._drag_loop, args=(hwnd,), daemon=True).start()
        return {"ok": True}

    def _drag_loop(self, hwnd):
        pt = wintypes.POINT()
        rc = RECT()
        moved = 0
        deadline = time.time() + 300      # 保险丝：按键状态读不到时不至于永久循环
        try:
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            user32.GetCursorPos(ctypes.byref(pt))
            off_x, off_y = pt.x - rc.left, pt.y - rc.top
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if getattr(self, "_maxed", False):
                self._maxed = False
            # 仅首帧提一次 z-order（等同常规窗口被拖起时的置顶），之后只平移
            user32.SetWindowPos(hwnd, HWND_TOP, rc.left, rc.top, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)
            while user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                if self.quitting or not user32.IsWindow(hwnd) or time.time() > deadline:
                    break
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    break
                x, y = clamp_main_pos(pt.x - off_x, pt.y - off_y, w, h)
                if x != rc.left or y != rc.top:
                    user32.SetWindowPos(hwnd, None, x, y, 0, 0,
                                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                    rc.left, rc.top = x, y
                    moved += 1
                time.sleep(DRAG_TICK)
        except Exception as e:
            log(f"drag loop error: {e}")
        finally:
            self._dragging = False
            if moved:
                log(f"drag end: {moved} steps -> ({rc.left},{rc.top})")
            # 结束态交回前端：解除拖动光标、补一次窗口状态
            self._safe_js(self.main, "window.App && App.__dragEnd()")

    def win_min(self):
        """最小化 = 收成右下角小窗，并播放"主窗收拢 → 小窗浮现"的过渡。

        以前是 ShowWindow(SW_MINIMIZE) 再由监视线程恢复 + 弹小窗，中间会闪一下
        系统的任务栏最小化动画。现在不真的最小化，直接做几何收拢：
        主窗矩形按 ease-out 插值到小窗位置，同时页面内容淡出，最后换小窗入场。
        """
        hwnd = find_hwnd(MAIN_TITLE)
        if not hwnd:
            return {"ok": False}
        if getattr(self, "_collapsing", False):
            return {"ok": True, "busy": True}
        self._collapsing = True
        threading.Thread(target=self._collapse_to_popup, args=(hwnd,), daemon=True).start()
        return {"ok": True}

    def _collapse_to_popup(self, hwnd):
        """只做"内容淡出 → 换小窗"，不再缩放窗口。

        之前的做法是把主窗矩形一路插值到小窗大小，窗口每帧重排，图表和文字全在跳，
        看起来就是"闪来闪去"。现在不碰窗口几何：内容 150ms 淡出，隐藏后小窗自己入场。
        """
        try:
            self._safe_js(self.main, "window.App && App.playExit()")
            time.sleep(0.16)
        except Exception as e:
            log(f"collapse error: {e}")
        finally:
            self._collapsing = False
            self.hide_main()
            self.show_popup()

    def win_max(self):
        """切换最大化/还原：按工作区手工铺满（不盖任务栏），并记住原位置。"""
        hwnd = find_hwnd(MAIN_TITLE)
        try:
            if not hwnd:
                return {"ok": False}
            if getattr(self, "_maxed", False):
                r = getattr(self, "_normal_rect", None)
                if r:
                    user32.SetWindowPos(hwnd, None, r[0], r[1], r[2], r[3],
                                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
                else:
                    user32.ShowWindow(hwnd, SW_RESTORE)
                self._maxed = False
            else:
                rc = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rc))
                self._normal_rect = (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
                wa = work_area()
                user32.SetWindowPos(hwnd, None, wa[0], wa[1],
                                    wa[2] - wa[0], wa[3] - wa[1],
                                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
                self._maxed = True
            log(f"win_max -> maximized={self._maxed} rect={getattr(self, '_normal_rect', None)}")
            threading.Timer(0.12, lambda: self._safe_js(self.main, "App.refreshLayout()")).start()
            return {"ok": True, "maximized": self._maxed}
        except Exception as e:
            log(f"win_max error: {e}")
            return {"ok": False, "error": str(e)}

    def win_close(self):
        """关闭：按设置收起到托盘；关闭了「收起到托盘」则退出。"""
        if not self.close_to_tray:
            threading.Thread(target=self.quit, daemon=True).start()
            return {"ok": True, "quit": True}
        self.hide_main()
        log("win_close -> hide to tray")
        return {"ok": True, "toTray": True}

    def _align_popup_once(self, hwnd):
        """把窗口按客户区贴到右下角，返回 (dx, dy)。"""
        wr, cr, pt = RECT(), RECT(), POINT(0, 0)
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        target_w, target_h = self.pop_size

        if cr.right != target_w or cr.bottom != target_h:
            frame_w = (wr.right - wr.left) - cr.right
            frame_h = (wr.bottom - wr.top) - cr.bottom
            user32.SetWindowPos(hwnd, None, 0, 0,
                                int(target_w * self.scale) + frame_w,
                                int(target_h * self.scale) + frame_h,
                                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
            user32.GetWindowRect(hwnd, ctypes.byref(wr))
            user32.GetClientRect(hwnd, ctypes.byref(cr))

        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        wa = work_area()
        margin = int(MARGIN * self.scale)
        dx = (wa[2] - margin) - (pt.x + cr.right)
        dy = (wa[3] - margin) - (pt.y + cr.bottom)
        if dx or dy:
            user32.SetWindowPos(hwnd, None, wr.left + dx, wr.top + dy, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return dx, dy

    def _align_popup(self, hwnd, attempt=0):
        """显示后再校正一次（pywebview 可能套用自己的布局），只做必要的修正。"""
        try:
            dx, dy = self._align_popup_once(hwnd)
            if attempt == 0:
                log(f"align: target={self.pop_size[0]}x{self.pop_size[1]} dx={dx} dy={dy}")
            if attempt < 8:
                threading.Timer(0.15, lambda: self._align_popup(hwnd, attempt + 1)).start()
        except Exception as e:
            log(f"align error: {e}")

    @staticmethod
    def _nudge(hwnd):
        """隐藏期间创建的 WebView2 可能不出帧，轻微改变尺寸强制其重新布局并绘制。

        注意：这一步必须在窗口**显示之前**做（见 show_popup），
        否则用户会看到一次 1px 的尺寸抖动，观感就是"闪一下"。
        """
        try:
            rc = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            w = rc.right - rc.left
            h = rc.bottom - rc.top
            user32.SetWindowPos(hwnd, None, rc.left, rc.top, w, h - 1, SWP_NOZORDER | SWP_NOACTIVATE)
            user32.SetWindowPos(hwnd, None, rc.left, rc.top, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
            user32.InvalidateRect(hwnd, None, True)
            user32.UpdateWindow(hwnd)
        except Exception as e:
            log(f"nudge error: {e}")

    def _refresh_popup_layout(self):
        self._safe_js(self.popup, "App.refreshLayout()")

    @staticmethod
    def _safe_js(win, code):
        try:
            if win:
                win.evaluate_js(code)
        except Exception:
            pass

    # ---------- 托盘 ----------

    def toggle_popup(self):
        """托盘单击：**只负责打开**。

        已经打开时再点不再关闭（关闭走小窗右上角的 ×，或点小窗以外的空白处），
        只抖一下作为"我在呢"的反馈。这样连点永远不会出现"开了又关、关了又开"的错乱。
        """
        if not self.popup:
            return
        if getattr(self, "_popup_intent", False):
            log("toggle popup: already open -> shake")
            self._safe_js(self.popup, "window.App && App.burstShake()")
            return
        log("toggle popup -> show")
        self.show_popup()

    def open_main(self):
        self.hide_popup()
        self.show_main()

    def expand(self):
        self.hide_popup()
        self.show_main()
        return True

    def collapse(self):
        self.hide_main()
        self.show_popup()
        return True

    def refresh_all(self):
        try:
            self.usage.refresh()
        except Exception as e:
            log(f"refresh error: {e}")
        for win in (self.popup, self.main, getattr(self, "login_win", None)):
            self._safe_js(win, "window.App && App.reloadUsage()")
        return True

    # ---------- 退出 ----------

    def _watch_minimize(self):
        """系统最小化按钮 → 收起为右下角小窗（而不是进任务栏）。"""
        hwnd = None
        while not self.quitting:
            time.sleep(0.35)
            try:
                if not hwnd or not user32.IsWindow(hwnd):
                    hwnd = user32.FindWindowW(None, MAIN_TITLE)
                    if not hwnd:
                        continue
                    if not getattr(self, "min_hint_shown", False):
                        self.min_hint_shown = True
                        log(f"minimize watcher attached hwnd={hwnd}")
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)      # SW_RESTORE，先取消最小化
                    log("minimize detected -> collapse to popup")
                    self.show_popup()
            except Exception:
                hwnd = None

    def _on_login_closing(self):
        if self.quitting:
            return True
        try:
            self.login_win.hide()
        except Exception:
            pass
        return False

    def _on_main_closing(self):
        if self.quitting or not self.close_to_tray:
            return True
        self.hide_main()
        self.show_popup()
        return False

    def quit(self):
        self.quitting = True
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        for win in (self.popup, self.main, getattr(self, "login_win", None)):
            try:
                if win:
                    win.destroy()
            except Exception:
                pass
        time.sleep(0.25)
        os._exit(0)


class Api:
    """前端可调用的桌面能力（属性必须下划线开头，避免 pywebview 递归反射）。"""

    def __init__(self, app: Desktop):
        self._desk = app

    def get_usage(self):
        return self._desk.usage.get()

    def get_account(self):
        return self._desk.account.snapshot()

    def account_login(self):
        return self._desk.account.open_login()

    def account_probe(self):
        return self._desk.account.probe()

    def account_sync(self):
        return self._desk.account.sync()

    def account_set_interval(self, minutes):
        return self._desk.account.set_interval(minutes)

    def account_logout(self):
        return self._desk.account.logout()

    def refresh_usage(self):
        return self._desk.usage.refresh()

    def save_quota(self, total, cycle_start, cycle_end):
        try:
            usage_source.save_config(total, cycle_start, cycle_end)
            return {"ok": True, "data": self._desk.usage.refresh()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_cover_days(self, days):
        """设置统计覆盖天数（0 = 全部），保存后立即重扫，返回新的用量数据。"""
        try:
            cfg = usage_source.save_cover_days(days)
            data = self._desk.usage.refresh()
            log(f"set_cover_days: {cfg.get('cover_days')}")
            return {"ok": True, "coverDays": cfg.get("cover_days", 0), "data": data}
        except Exception as e:
            log(f"set_cover_days error: {e}")
            return {"ok": False, "error": str(e)}

    def expand(self):
        return self._desk.expand()

    def collapse(self):
        return self._desk.collapse()

    def win_min(self):
        return self._desk.win_min()

    def win_max(self):
        return self._desk.win_max()

    def win_close(self):
        return self._desk.win_close()

    def win_state(self):
        return self._desk.win_state()

    def win_pos(self):
        return self._desk.win_pos()

    def win_move(self, x, y, w=0, h=0):
        return self._desk.win_move(x, y, w, h)

    def win_drag_start(self):
        return self._desk.win_drag_start()

    def win_resize(self, x, y, w, h):
        return self._desk.win_resize(x, y, w, h)

    # ---------- 本地数据源（#7） ----------

    def get_version(self):
        return {"version": read_version()}

    def get_datasource(self):
        """返回数据目录解析结果（供设置页展示"当前读的是哪个目录"）。"""
        try:
            return {"ok": True, "state": data_source.resolve()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def choose_data_dir(self):
        """弹出目录选择框，选中后保存并立即重扫本地记录。"""
        try:
            win = self._desk.main or self._desk.popup
            picked = win.create_file_dialog(webview.FOLDER_DIALOG)
            if not picked:
                return {"ok": False, "cancelled": True}
            path = picked[0] if isinstance(picked, (list, tuple)) else picked
            out = data_source.set_home(path)
            self._desk.usage.refresh()
            log(f"choose_data_dir: {path} valid={out['ok']}")
            return {"ok": bool(out["ok"]), "state": out["state"]}
        except Exception as e:
            log(f"choose_data_dir error: {e}")
            return {"ok": False, "error": str(e)}

    def reset_data_dir(self):
        try:
            st = data_source.clear_home()
            self._desk.usage.refresh()
            return {"ok": True, "state": st}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def hide(self):
        self._desk.hide_popup()
        return True

    def sync_theme(self, theme):
        js = "window.App && App.__applyTheme('%s')" % ("dark" if theme == "dark" else "light")
        for win in (self._desk.popup, self._desk.main):
            try:
                if win:
                    win.evaluate_js(js)
            except Exception:
                pass
        # 托盘右键菜单是自绘的，主题要跟着界面一起换
        try:
            if self._desk.tray:
                self._desk.tray.set_theme(theme)
        except Exception:
            pass
        return True

    def sync_prefs(self, payload):
        """把偏好推给另一个窗口。

        小窗与主界面是两个独立 WebView，各自持有一份 prefs；主界面改了
        「工作日折算 / 计入签到」后，小窗不会自己知道，额度数字就会停在旧值。
        这里显式广播一次，另一端合并后立即重算。
        """
        try:
            js = "window.App && App.applyPrefs(%s)" % (payload or "{}")
            for win in (self._desk.popup, self._desk.main):
                self._desk._safe_js(win, js)
            return True
        except Exception as e:
            log(f"sync_prefs error: {e}")
            return False

    def set_close_to_tray(self, flag):
        self._desk.close_to_tray = bool(flag)
        return True

    def open_data_dir(self):
        try:
            os.startfile(os.path.join(os.path.expanduser("~"), ".workbuddy"))
            return True
        except Exception:
            return False


def install_diagnostics():
    """把崩溃信息落到 logs/crash.log：进程"闪退"时至少留下最后现场。

    - faulthandler：段错误 / 访问违例（WebView2、ctypes 调用）这类 Python 层拦不到的崩溃
    - excepthook：主线程未捕获异常
    - threading.excepthook：后台线程异常（同步、轮询都在线程里）
    """
    try:
        crash = os.path.join(LOG_DIR, "crash.log")
        os.makedirs(LOG_DIR, exist_ok=True)
        f = open(crash, "a", encoding="utf-8", buffering=1)
        f.write("\n==== run %s ====\n" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        faulthandler.enable(file=f, all_threads=True)
    except Exception as e:
        log(f"faulthandler init error: {e}")

    def _hook(exc_type, exc, tb):
        log("UNCAUGHT\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    def _thook(args):
        log("THREAD UNCAUGHT (%s)\n%s" % (
            getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))))

    sys.excepthook = _hook
    try:
        threading.excepthook = _thook
    except Exception:
        pass


def main():
    log("=== start ===")
    install_diagnostics()
    set_app_id()
    handle, already = single_instance()
    if already:
        log("another instance detected, exit")
        alert("WorkBuddy 积分观测台",
              "程序已在运行。\n请在右下角通知区找到托盘图标（蓝色柱状图标），单击即可打开快捷面板。")
        return

    app = Desktop()
    app.build()
    app.usage.start()          # 后台读取本机用量
    app.account.start_polling()  # 账号云同步轮询（默认 60 分钟）

    if "--sync" in sys.argv:      # 诊断用：启动后立即同步一次并写日志
        def _one_shot():
            time.sleep(4)
            try:
                r = app.account.sync()
                log("sync(one-shot): " + json.dumps(r, ensure_ascii=False)[:1200])
            except Exception as e:
                log(f"sync(one-shot) error: {e}")
        threading.Thread(target=_one_shot, daemon=True).start()

    tray = Tray(
        ICON,
        on_click=app.toggle_popup,
        on_open_main=app.open_main,
        on_refresh=app.refresh_all,
        on_exit=app.quit,
        logger=log,
    )
    app.tray = tray
    try:
        tray.start()
        log("tray started")
    except Exception as e:
        log(f"tray error: {e}")

    if "--show" in sys.argv:
        threading.Timer(1.0, app.show_popup).start()
    elif "--main" in sys.argv:
        threading.Timer(1.0, app.show_main).start()
    # 默认托盘常驻：不再弹「已在托盘」的气泡提示（用户明确要求去掉启动弹窗）

    # 启动时尝试回填上次登录态（有存档则无需再次扫码）
    def _auto_restore():
        time.sleep(4)
        try:
            ok = app.account.try_restore()
            log(f"auto-restore: {ok}")
            if ok:
                for delay in (0, 3, 8):
                    time.sleep(delay)
                    for w in (app.main, app.popup):
                        app._safe_js(w, "window.App && App.reloadAccount()")
        except Exception as e:
            log(f"auto-restore error: {e}")
    threading.Thread(target=_auto_restore, daemon=True).start()

    def _apply_icons():
        for delay in (1.2, 2.5, 5.0):
            time.sleep(delay)
            set_window_icons()
    threading.Thread(target=_apply_icons, daemon=True).start()

    def _sync_tray_theme():
        """界面主题存在 localStorage 里，Python 不知道 —— 启动后问一次，菜单配色才不会错。"""
        for _ in range(6):
            time.sleep(2.0)
            for w in (app.popup, app.main):
                try:
                    t = w.evaluate_js("(window.App && App.getTheme) ? App.getTheme() : null")
                except Exception:
                    t = None
                if t in ("light", "dark"):
                    try:
                        if app.tray:
                            app.tray.set_theme(t)
                        log(f"tray theme = {t}")
                    except Exception:
                        pass
                    return
    threading.Thread(target=_sync_tray_theme, daemon=True).start()

    log("webview.start")
    try:
        webview.start(debug="--debug" in sys.argv, private_mode=False,
                      storage_path=app_paths.data_path(".webview-profile"),
                      icon=ICON if os.path.exists(ICON) else None)
    except Exception:
        log("webview.start EXCEPTION\n" + traceback.format_exc())
        raise
    # 正常退出也会走到这里：记录退出原因，便于区分"用户退出"与"异常结束"
    log("webview.exit windows=%s quitting=%s" % (len(webview.windows or []), getattr(app, "quitting", None)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        log("FATAL\n" + tb)
        alert("WorkBuddy 积分观测台启动失败",
              "启动时发生错误，详情已写入：\n" + LOG_PATH + "\n\n" + tb[-800:])
