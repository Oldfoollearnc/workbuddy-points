# -*- coding: utf-8 -*-
"""
WorkBuddy 积分观测台 · Windows 桌面启动器

形态
----
1. 启动即打开主界面（窗口先上屏，内容随后加载），同时常驻系统托盘；
2. 单击托盘图标 → 屏幕右下角弹出快捷面板（无边框、置顶、固定尺寸，严格贴边），
   已打开时再点只抖一下，不会自动关闭；
3. 面板「展开完整面板」→ 全尺寸主界面，主界面保留「收起为小窗」；
4. 关闭任一窗口均收起到托盘，托盘菜单「退出」才结束进程。

数据
----
由 usage_source.UsageService 读取本机 WorkBuddy 会话记录，经 js_api 提供给界面。

运行
----
python desktop.py            # 打开主界面（默认）
python desktop.py --show     # 启动只弹小窗
python desktop.py --main     # 同默认（显式写法）
"""
import csv
import ctypes
import faulthandler
import json
import datetime as dt
import os
import re
import sys
import threading
import time
import traceback
from ctypes import wintypes

# 本机无独立显卡：关闭 WebView2 的 GPU 合成，静态界面 CPU 更稳、显存占用更低。
# 注意这里**不带** --remote-debugging-port —— 那等于给同机任意进程开一个读取
# WebView2 Cookie 的后门（Network.getAllCookies + Runtime.evaluate）。
# 开发态确实要 CDP（tools/live.py 截图 / 导出 session.bin）时，显式设 WB_CDP=1，
# 由 main() 在启动前补参数；正常使用永远不打开。
_ENV_ARGS = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
_BASE_ARGS = "--disable-gpu --disable-gpu-compositing"
os.environ.setdefault(_ENV_ARGS, _BASE_ARGS)

import webview

import app_paths
import autostart
import data_source
import diagnostics
import logutil

from win32_utils import (      # noqa: E402  Win32 底层工具（拆分自本文件，行为未变）
    RECT, POINT, MONITORINFO,
    dpi_scale, work_area, virtual_screen, clamp_main_pos,
    set_app_id, find_hwnd, on_taskbar, round_corners, strip_nonclient,
    enable_snap_support, MONITORINFO,
    single_instance, signal_show,
    SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE,
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED,
    GWL_STYLE, KEEP_VISIBLE_X, KEEP_VISIBLE_Y,
)
import update_check
import usage_source
from account_service import AccountService, CDP_PORT, cdp_enabled
from checkin_service import CheckinService
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
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.GetCapture.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long

# 同一个前端脚本只记录一次失败，避免启动阶段刷屏
_js_fail_seen = set()

LOGIN_TITLE = "WorkBuddy 账号登录"
WM_SETICON = 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_SHOWWINDOW = 0x0040
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2

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
    logutil.log(LOG_PATH, msg)


def _csv_cell(v):
    """CSV 单元格转义：Excel 会把 = + - @ 开头的值当公式执行（DDE / 外链）。

    模型名、用途都来自服务端，不能当可信输入；前置单引号让它按文本显示。
    """
    s = "" if v is None else v
    if isinstance(s, str) and s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def read_version():
    """读取 version.json（每次提交由 .githooks/commit-msg 按提交信息递增）。"""
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
# 第二次双击图标时不再弹"程序已在运行"——那是在教用户去找托盘图标。
# 正常逻辑应该是：点图标 = 我要看界面，那就把已经跑着的那个窗口拿到眼前。
SHOW_EVENT = "Local\\WorkBuddyPointsInsightShow"
def start_show_watcher(app):
    """第一个实例：驻守一个线程，等第二个实例敲门。"""
    try:
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        ev = kernel32.CreateEventW(None, False, False, SHOW_EVENT)   # 自动复位
        if not ev:
            log("show watcher: CreateEventW returned null")
            return False
    except Exception as e:
        log(f"show watcher create failed: {e}")
        return False

    def loop():
        while True:
            try:
                if kernel32.WaitForSingleObject(ev, 1000) == 0:
                    log("second instance detected -> bring main window up")
                    try:
                        if "--show" in sys.argv:
                            app.show_popup()
                        else:
                            app.show_main()
                    except Exception as e:
                        log(f"show from watcher error: {e}")
            except Exception as e:
                log(f"show watcher loop error: {e}")
                return

    threading.Thread(target=loop, daemon=True).start()
    return True


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
        # 可见性事件：两个监视线程只在对应窗口可见时才采样；
        # 隐藏时阻塞在 wait() 上，而不是整夜 22Hz / 3Hz 空转。
        self._popup_visible = threading.Event()
        self._main_visible = threading.Event()
        self.usage = usage_source.UsageService()
        self.account = AccountService()
        self.checkin = CheckinService(self.account)

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
        # 目标客户区尺寸：加回系统边框后要按它补回来，否则内容会被吃掉 16×39
        self.main_size = (main_w, main_h)

        self.pop_x = int(avail_w - pw - MARGIN)
        self.pop_y = int(avail_h - ph - MARGIN)
        api = Api(self)

        # 主窗必须**第一个**创建：窗口是按创建顺序初始化的，主界面先建好就能先出画面，
        # 小窗/登录窗晚几百毫秒无所谓（它们本来就是隐藏的）。
        # hidden=False：不等页面加载完，先把窗口框放上屏（底色是界面同款浅蓝），
        # 首帧由 index.html 里的启动遮罩接管，视觉上就是"窗口立刻出现 → 内容随后加载"。
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
            # --tray（开机自启）时窗口一开始就藏着，否则会先闪一下再收进托盘
            hidden=("--tray" in sys.argv),
            background_color="#EEF3FC",
        )
        self.main.events.closing += self._on_main_closing

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

        # 账号登录窗：这个窗口加载的是**远端页面**（workbuddy.cn），
        # 绝不能把桌面 Api 暴露给它 —— 否则页面上的任何脚本（含第三方脚本、跳转页）
        # 都能直接调 get_usage / choose_data_dir / account_sync / win_*。
        # 取数与导航全部由 Python 侧 evaluate_js 完成，这里不需要 js_api。
        self.login_win = webview.create_window(
            "WorkBuddy 账号登录",
            "about:blank",
            js_api=None,
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
            self._main_visible.clear()
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
            self._popup_visible.set()      # 唤醒"点外收起"监听
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
            # 小窗不可见时不采样：以前是整生命周期 45ms 空转，白烧 CPU。
            if not self._popup_visible.wait(2.0):
                prev_down = None
                continue
            time.sleep(0.045)
            try:
                hwnd = user32.FindWindowW(None, POPUP_TITLE)
                if not hwnd or not user32.IsWindowVisible(hwnd):
                    prev_down = None
                    time.sleep(0.25)      # 事件还没清掉的短窗口，退化成低频
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
            self._popup_visible.clear()    # 隐藏后监听线程阻塞，不再 22Hz 空转
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

    def _window_visible(self, title):
        hwnd = find_hwnd(title)
        return bool(hwnd and user32.IsWindowVisible(hwnd))

    def notify(self, msg, level="info"):
        """把一条结果按"用户此刻能看见什么"投递出去。

        顺序：小窗可见 → 弹在小窗（避免和托盘气泡两个东西叠在右下角）；
              主窗可见 → 弹在主窗；
              都不在 → 托盘气泡（系统样式，改不了，但至少不丢消息）。
        """
        icon = "i-alert" if level == "warn" else "i-check"
        try:
            payload = "window.App && App.toast && App.toast(%s, %s)" % (
                json.dumps(msg, ensure_ascii=False), json.dumps(icon))
            if self.popup and self._window_visible(POPUP_TITLE):
                self._safe_js(self.popup, payload)
                return "popup"
            if self.main and self._window_visible(MAIN_TITLE):
                self._safe_js(self.main, payload)
                return "main"
            if self.tray:
                self.tray.balloon("WorkBuddy 积分观测台", msg, level)
                return "tray"
        except Exception as e:
            log("notify error: %s" % e)
        return "none"

    def _pin_temporarily(self, hwnd, seconds=4.0):
        """临时置顶：窗口刚出来时压住其它程序，几秒后自动放开。

        不做成常驻置顶 —— 那会挡住用户真正在用的窗口；4 秒足够看到界面出来。
        """
        try:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            return

        def _unpin():
            time.sleep(seconds)
            try:
                user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            except Exception:
                pass

        threading.Thread(target=_unpin, daemon=True).start()

    def show_main(self):
        # 小窗让位（同时把小窗状态簿记成"已收起"）
        self.hide_popup()
        hwnd = find_hwnd(MAIN_TITLE)
        was_visible = bool(hwnd and user32.IsWindowVisible(hwnd))
        try:
            self.main.show()
            self.main.restore()
        except Exception as e:
            log(f"main.show error: {e}")
        self._main_visible.set()          # 唤醒"系统最小化 → 收成小窗"监听
        if hwnd:
            user32.ShowWindow(hwnd, 5)
            if not getattr(self, "resize_frame_on", False):
                # 贴边/分屏/系统最大化那套已经全部去掉：只要纯客户区无边框窗口。
                strip_nonclient(hwnd)
                # pywebview 的 width/height 是外框尺寸，剥掉边框后客户区会小一圈，补回来
                size = getattr(self, "main_size", None)
                if size:
                    user32.SetWindowPos(hwnd, None, 0, 0,
                                        int(size[0] * self.scale), int(size[1] * self.scale),
                                        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
                self.resize_frame_on = True
            self._ensure_on_screen(hwnd)
            if not was_visible:
                # 1px 抖动只在窗口本来隐藏时需要（强制出一帧）；
                # 启动时窗口已经是可见的，再抖一次反而会闪。
                self._nudge(hwnd)
            try:
                user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            # 启动/展开时先置顶几秒：刚打开就被别的窗口盖住，看起来像"没打开"
            self._pin_temporarily(hwnd)
            self._safe_js(self.main,
                          "window.App && (App.beforeShow(), App.playEnter('main'))")
        threading.Timer(0.2, lambda: self._safe_js(self.main, "App.refreshLayout()")).start()
        threading.Timer(0.3, set_window_icons).start()

    def _ensure_on_screen(self, hwnd):
        """唤回主窗时兜一次：只有几乎整块都在屏幕外，才拉回工作区居中。

        拖动过程中不做任何限制（要的就是自由拖动）；
        但拖出去以后点右下角小窗唤回，必须能完整看见，所以在这里补一次。
        """
        try:
            rc = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if w <= 0 or h <= 0:
                return
            vl, vt, vr, vb = virtual_screen()
            ix = max(0, min(rc.right, vr) - max(rc.left, vl))
            iy = max(0, min(rc.bottom, vb) - max(rc.top, vt))
            visible = (ix * iy) / float(w * h)
            if visible >= 0.3:
                return                      # 还能看见三成，就不动它
            wa = work_area()
            nx = wa[0] + max(0, ((wa[2] - wa[0]) - w) // 2)
            ny = wa[1] + max(0, ((wa[3] - wa[1]) - h) // 2)
            user32.SetWindowPos(hwnd, None, nx, ny, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            log(f"ensure_on_screen: visible={visible:.0%} -> ({nx},{ny})")
        except Exception as e:
            log(f"ensure_on_screen error: {e}")

    def hide_main(self):
        self._main_visible.clear()
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
        """开始拖动主窗：只做拖动，跟手为准。

        一路试过系统拖动（想白拿 Snap 预览）、贴边吸附、四角分屏、范围钳制，
        结果都是"看着花哨、用着不跟手"。现在只保留最朴素的一条：
        GetCursorPos 跟着光标 SetWindowPos，鼠标在哪窗口就在哪，不做任何吸附和限制。
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
                # 不做任何范围限制：拖到哪儿就是哪儿，鼠标和窗口严格 1:1。
                # 以前在这里 clamp，窗口一到边界就停住、鼠标继续走，看着就是"拖不动/不跟手"。
                x, y = int(pt.x - off_x), int(pt.y - off_y)
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
            # Win+↑ 是系统最大化：这时 _maxed 仍是 False，必须先认 IsZoomed，
            # 否则会把"满屏矩形"当成普通尺寸记进 _normal_rect，之后再也还原不回去。
            if user32.IsZoomed(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                self._maxed = False
                log("win_max -> restore from system zoom")
                threading.Timer(0.12, lambda: self._safe_js(self.main, "App.refreshLayout()")).start()
                return {"ok": True, "maximized": False}
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
        """执行前端脚本，失败不再静默。

        以前这里 except 之后什么都不做，结果"图表动画被关掉""钩子没生效"这类问题
        在日志里一点痕迹都没有，只能靠肉眼发现。现在第一次失败会落一条日志
        （同一个脚本只记一次，避免启动阶段刷屏）。
        """
        try:
            if win:
                win.evaluate_js(code)
        except Exception as e:
            key = str(code)[:60]
            if key not in _js_fail_seen:
                _js_fail_seen.add(key)
                log(f"js eval failed: {e} | {key}")

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
            # 主窗不可见时不需要监听（隐藏 = 已经在托盘里了），阻塞等待下次显示。
            if not self._main_visible.wait(2.0):
                continue
            time.sleep(0.35)
            try:
                if not hwnd or not user32.IsWindow(hwnd):
                    hwnd = user32.FindWindowW(None, MAIN_TITLE)
                    if not hwnd:
                        time.sleep(0.2)
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
            self.checkin.stop_watch()   # 先让看猫轮询退出，别在窗口销毁后再伸手
        except Exception:
            pass
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

    # ---------- 每日签到 / 派猫猫旅行 ----------

    def get_checkin(self):
        return self._desk.checkin.snapshot()

    def checkin_run(self):
        """手动重试（界面上"重试"按钮用）。同步执行，前端 await 时转圈。"""
        opt = self._desk.checkin.options
        return self._desk.checkin.run(travel_auto=bool(opt.get("travel", True)))

    def checkin_options(self, auto=None, travel=None):
        return self._desk.checkin.set_options(auto=auto, travel=travel)

    def get_checkin_tasks(self):
        """成长计划任务进度（活动卡第四格用，带 30 分钟缓存）。"""
        return self._desk.checkin.tasks()

    def export_ledger_csv(self):
        """把云端逐笔流水导出成 CSV（utf-8-sig，Excel 直接打开不乱码）。"""
        try:
            rows = self._desk.account.state.get("usageRows") or []
            if not rows:
                return {"ok": False, "error": "还没有可导出的流水，先同步一次云端"}
            win = self._desk.main or self._desk.popup
            name = "WorkBuddy-流水-%s.csv" % dt.datetime.now().strftime("%Y%m%d-%H%M")
            picked = win.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
            if not picked:
                return {"ok": False, "cancelled": True}
            path = picked[0] if isinstance(picked, (list, tuple)) else picked
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["时间", "模型", "用途", "积分"])
                for r in rows:
                    w.writerow([_csv_cell(r.get("t")), _csv_cell(r.get("model")),
                                _csv_cell(r.get("purpose")), r.get("credit") or 0])
            log("export_ledger_csv -> %s (%d 行)" % (path, len(rows)))
            return {"ok": True, "path": path, "count": len(rows)}
        except Exception as e:
            log(f"export_ledger_csv error: {e}")
            return {"ok": False, "error": str(e)[:160]}

    def export_rows_csv(self, name="汇总", head_json="[]", rows_json="[]"):
        """导出前端算好的汇总表。

        为什么让前端算：抽成"按模型 / 按月"两种口径后，聚合规则必须跟页面上的筛选
        （今日 / 近 7 天 / 只看某个模型）完全一致；在 Python 再算一遍就会出现
        "表里和屏幕上对不上"。这里只负责落盘，并把行数卡在 5 万以内。
        """
        try:
            # 先把体积卡住再解析：一个超长 IPC 字符串足以把主进程内存顶上去
            if len(head_json or "") > 64 * 1024 or len(rows_json or "") > 4 * 1024 * 1024:
                return {"ok": False, "error": "导出内容过大，请缩小筛选范围"}
            head = json.loads(head_json or "[]")
            rows = json.loads(rows_json or "[]")
            if not head or not rows:
                return {"ok": False, "error": "没有可导出的内容"}

            # Excel 会把 = + - @ 开头的单元格当公式执行（DDE / 外链）。
            # 模型名、用途都来自服务端，不能信，统一前置单引号当文本。
            cell = _csv_cell

            win = self._desk.main or self._desk.popup
            safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', "", str(name or "汇总"))[:24] or "汇总"
            fn = "%s-%s.csv" % (safe_name, dt.datetime.now().strftime("%Y%m%d-%H%M"))
            picked = win.create_file_dialog(webview.SAVE_DIALOG, save_filename=fn)
            if not picked:
                return {"ok": False, "cancelled": True}
            path = picked[0] if isinstance(picked, (list, tuple)) else picked
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow([cell(x) for x in head][:20])
                n = 0
                for r in rows[:50000]:
                    if not isinstance(r, (list, tuple)):
                        continue          # dict 会被写成键名，宁可跳过也不产一张错表
                    w.writerow([cell(v) for v in list(r)[:20]])
                    n += 1
            log("export_rows_csv -> %s (%d 行)" % (path, n))
            return {"ok": True, "path": path, "count": n}
        except Exception as e:
            log(f"export_rows_csv error: {e}")
            return {"ok": False, "error": str(e)[:160]}

    # ---------- 维护：清缓存 / 清日志 ----------

    def maintenance_info(self):
        """设置页「维护」区：先让人看到占多大，再决定要不要清。"""
        cache_bytes = 0
        try:
            cache_bytes = self._desk.usage.cache_size()
        except Exception:
            pass
        return {
            "cacheBytes": cache_bytes,
            "logBytes": logutil.size(LOG_PATH),
            "cachePath": app_paths.data_path(".cache", "usage.json"),
            "logPath": LOG_PATH,
        }

    def clear_usage_cache(self):
        """删掉解析缓存；下次刷新会全量重建（本机记录本身不受影响）。"""
        try:
            removed = self._desk.usage.clear_cache()
            self._desk.refresh_all()      # 立刻重建，别让界面等下一轮定时刷新
            log("clear_usage_cache -> %s" % removed)
            return {"ok": True, "removed": bool(removed)}
        except Exception as e:
            log(f"clear_usage_cache error: {e}")
            return {"ok": False, "error": str(e)[:160]}

    def clear_logs(self):
        try:
            ok = logutil.clear(LOG_PATH)
            log("clear_logs -> %s" % ok)
            return {"ok": bool(ok)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def export_diagnostics(self):
        """把排障要用的东西打成 zip（不含手机号 / cookie / 登录凭证）。"""
        try:
            win = self._desk.main or self._desk.popup
            picked = win.create_file_dialog(webview.FOLDER_DIALOG)
            if not picked:
                return {"ok": False, "cancelled": True}
            dest = picked[0] if isinstance(picked, (list, tuple)) else picked

            def _g(fn, default=None):
                try:
                    return fn()
                except Exception:
                    return default

            acc = _g(self._desk.account.snapshot, {}) or {}
            state = {
                "账号状态": acc.get("status"),
                "上次同步": acc.get("lastSync"),
                "同步间隔(分钟)": acc.get("interval"),
                "云端可用积分": (acc.get("cloud") or {}).get("available"),
                "云端本周期已用": (acc.get("cloud") or {}).get("used"),
                "签到状态": _g(lambda: (self._desk.checkin.snapshot().get("result") or {}).get("checkin")),
                "旅行状态": _g(lambda: ((self._desk.checkin.snapshot().get("result") or {}).get("travel") or {}).get("state")),
                "开机自启": _g(autostart.is_enabled),
                "本地数据目录": _g(lambda: (self._desk.usage.snapshot() or {}).get("home")),
            }
            path = diagnostics.build(
                dest,
                version=read_version(),
                log_path=LOG_PATH,
                data_dir=app_paths.data_dir(),
                app_state=state,
            )
            log("export_diagnostics -> %s" % path)
            return {"ok": True, "path": path}
        except Exception as e:
            log(f"export_diagnostics error: {e}")
            return {"ok": False, "error": str(e)[:200]}

    def test_notify(self):
        """内部自测用：走一遍真实分流并返回投递到了哪里。

        界面上已经没有入口（验证完就撤了），保留接口是为了以后排查时
        能直接从前端 console 调 window.pywebview.api.test_notify()。
        """
        try:
            where = self._desk.notify("这是一条测试通知：能看到就说明分流正常", "info")
            return {"ok": True, "where": where}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    def open_url(self, url):
        """用系统默认浏览器打开链接（只允许 http/https，避免被页面里塞奇怪协议）。"""
        try:
            u = str(url or "").strip()
            if not u.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "只支持 http/https"}
            os.startfile(u)          # noqa: S606  Windows 专用
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    _update_cache = {"at": 0.0, "data": None}

    def check_update(self):
        """查 GitHub 最新版（结果缓存一天，失败静默）。"""
        now = time.time()
        c = Api._update_cache
        if c["data"] and now - c["at"] < 86400:
            return c["data"]
        try:
            r = update_check.check(read_version())
        except Exception as e:
            return {"ok": False, "error": str(e)[:120], "hasUpdate": False}
        if r.get("ok"):
            Api._update_cache = {"at": now, "data": r}
        return r

    def get_autostart(self):
        """开机自启：当前是否开启 + 会写进注册表的命令（供设置页展示）。"""
        return {"on": autostart.is_enabled(), "command": autostart.launch_command()}

    def set_autostart(self, on):
        try:
            cmd = autostart.set_enabled(bool(on))
            return {"ok": True, "on": autostart.is_enabled(), "command": cmd}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

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
            data = json.loads(payload or "{}")     # 只接受合法 JSON，避免把任意文本注进页面
            if not isinstance(data, dict):
                return False
            js = "window.App && App.applyPrefs(%s)" % json.dumps(data, ensure_ascii=False)
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
            home = (data_source.resolve() or {}).get("home") \
                or os.path.join(os.path.expanduser("~"), ".workbuddy")
            os.startfile(home)
            return True
        except Exception as e:
            log(f"open_data_dir error: {e}")
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
        # 点图标 = 想看界面：直接把已经跑着的那个喊到前面，不再弹窗教用户找托盘
        if signal_show():
            log("another instance detected, asked it to show main; exiting")
        else:
            log("another instance detected but signal failed; showing alert")
            alert("WorkBuddy 积分观测台",
                  "程序已在运行，但没能把窗口叫到前面。\n"
                  "请在右下角通知区找到托盘图标（蓝色柱状图标），单击即可打开。")
        return

    app = Desktop()
    app.build()
    start_show_watcher(app)      # 被再次双击时，负责把主窗亮出来
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

    def _tray_autostart():
        """托盘菜单里切换开机自启，成功后弹个气泡说明。"""
        try:
            on = not autostart.is_enabled()
            autostart.set_enabled(on)
            msg = "已开启开机自启（静默到托盘）" if on else "已关闭开机自启"
            log("tray autostart -> %s" % on)
            for w in (app.main, app.popup):
                app._safe_js(w, "window.App && App.toast && App.toast('%s')" % msg)
        except Exception as e:
            log(f"tray autostart error: {e}")

    def _open_settings():
        """托盘菜单「设置」：先把主窗叫出来，再切到设置页。"""
        try:
            app.show_main()
            threading.Timer(
                0.35, lambda: app._safe_js(app.main, "window.App && App.go && App.go('settings')")
            ).start()
        except Exception as e:
            log(f"tray settings error: {e}")

    def _tray_update():
        """托盘「检查更新」：手动点的就要给结果，不能像设置页那样静默。"""
        try:
            Api._update_cache = {"at": 0.0, "data": None}     # 手动点绕过一天缓存
            r = update_check.check(read_version())
            if not r.get("ok"):
                app.notify("检查更新失败：%s" % (r.get("error") or "网络不通"), "warn")
            elif r.get("hasUpdate"):
                app.notify("发现新版本 %s（当前 v%s）：设置 → 关于 里下载"
                           % (r.get("latest"), read_version()), "info")
            else:
                app.notify("已经是最新版本 v%s" % read_version(), "info")
        except Exception as e:
            log(f"tray update error: {e}")
            app.notify("检查更新失败，请检查网络或代理", "warn")

    tray = Tray(
        ICON,
        on_settings=_open_settings,
        on_click=app.toggle_popup,
        on_open_main=app.open_main,
        on_refresh=app.refresh_all,
        on_sync=lambda: app.account.sync(),
        on_autostart=_tray_autostart,
        autostart_state=autostart.is_enabled,
        on_update=_tray_update,
        on_exit=app.quit,
        logger=log,
    )
    app.tray = tray
    try:
        tray.start()
        log("tray started")
    except Exception as e:
        log(f"tray error: {e}")

    # 启动即出画面：默认直接上主界面（窗口在 build 时已 hidden=False，这里只做收尾），
    # --show 仍可改为"只弹小窗"。用 webview.start(func) 而不是 Timer(1.0) —— 少等 1 秒。
    def _startup_show():
        try:
            if "--tray" in sys.argv:
                # 开机自启：不打扰，只驻留托盘，签到/派猫照常在后台跑
                log("startup hidden (--tray)")
                return
            if "--show" in sys.argv:
                app.show_popup()
            else:
                app.show_main()
            log("startup window shown")
        except Exception as e:
            log(f"startup show error: {e}")

    _last_notify = {"key": ""}

    def _push_checkin():
        """把后台签到/派猫的最新结论推到结果条，并按需发一条通知。"""
        for w in (app.main, app.popup):
            app._safe_js(w, "window.App && App.reloadCheckin && App.reloadCheckin()")

        # 只在"这次确实发生了新事"时通知，避免每轮轮询都弹
        try:
            res = (app.checkin.snapshot() or {}).get("result") or {}
            ck = res.get("checkin") or {}
            tv = res.get("travel") or {}
            msg, level = "", "info"
            if ck.get("error"):
                msg, level = ck["error"], "warn"
            elif ck.get("new"):
                msg = "今日签到成功，+%d 分" % (ck.get("gain") or 0)
            elif tv.get("claimed"):
                msg = "猫猫回来了，领到 %d 分" % (tv.get("claimed") or 0)
            elif tv.get("departed"):
                msg = "猫猫出发去「%s」了，%d 小时后回来" % (
                    tv.get("location") or "旅行", tv.get("durationHours") or 1)
            if not msg:
                return
            key = "%s|%s" % (res.get("date") or "", msg)
            if key == _last_notify["key"]:
                return
            _last_notify["key"] = key
            where = app.notify(msg, level)
            log("notify -> %s: %s" % (where, msg))
        except Exception as e:
            log("push_checkin notify error: %s" % e)

    app.checkin.set_notify(_push_checkin)

    # 启动时尝试回填上次登录态（有存档则无需再次扫码）
    def _auto_restore():
        time.sleep(4)
        restored = False
        try:
            restored = app.account.try_restore()
            log(f"auto-restore: {restored}")
        except Exception as e:
            log(f"auto-restore error: {e}")

        # 签到紧跟在同步之后：此时隐藏窗口已停在 profile 页，页面内 fetch 直接可用，
        # 整个签到 + 派猫也就一两次往返。放到 reloadAccount 的延时循环后面会白等 11 秒。
        try:
            opt = app.checkin.options
            travel_now = None
            if opt.get("auto", True):
                snap = app.checkin.run(travel_auto=bool(opt.get("travel", True)))
                travel_now = ((snap or {}).get("result") or {}).get("travel")
                _push_checkin()
            else:
                log("auto-checkin skipped: 用户已关闭自动签到")
        except Exception as e:
            log(f"auto-checkin error: {e}")

        # 常驻看猫：到点自动领积分，跨天自动签新的一天再派一趟。
        # 每天只能派 1 次，所以真正会丢的只有"到家没领"这一段。
        try:
            app.checkin.start_watch(travel_now)
        except Exception as e:
            log(f"checkin watch start error: {e}")

        if restored:
            for delay in (0, 3, 8):
                time.sleep(delay)
                for w in (app.main, app.popup):
                    app._safe_js(w, "window.App && App.reloadAccount()")
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
    # 开发态才开 CDP（WB_CDP=1）：正式使用时不带调试端口
    if cdp_enabled():
        os.environ[_ENV_ARGS] = ("%s --remote-debugging-port=%d --remote-allow-origins=*"
                                 % (os.environ.get(_ENV_ARGS) or "", CDP_PORT)).strip()
        log(f"CDP enabled on 127.0.0.1:{CDP_PORT} (WB_CDP=1) —— 仅开发态，用完请关")
    try:
        webview.start(_startup_show, debug="--debug" in sys.argv, private_mode=False,
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
