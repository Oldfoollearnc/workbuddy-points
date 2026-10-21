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

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_CAPTION = 0x00C00000


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


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
        threading.Thread(target=self._watch_popup_focus, daemon=True).start()
        log(f"windows created, scale={s}, popup={pw}x{ph}, main={main_w}x{main_h}")

    # ---------- 显隐 ----------

    def show_popup(self):
        if not self.popup:
            return
        # 主窗与小窗互斥：同一时间只展示一个
        try:
            if self.main:
                self.main.hide()
        except Exception:
            pass
        mh = user32.FindWindowW(None, MAIN_TITLE)
        if mh:
            user32.ShowWindow(mh, 0)
        try:
            self.popup.show()
        except Exception as e:
            log(f"popup.show error: {e}")
        hwnd = user32.FindWindowW(None, POPUP_TITLE)
        if hwnd:
            self._align_popup(hwnd)
            user32.ShowWindow(hwnd, 5)
            # 置顶 + 前台：否则会被其他窗口遮住，看起来像"没有打开"
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
            try:
                user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
            except Exception:
                pass
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            self._nudge(hwnd)
            self._popup_shown_at = time.time()
            self._popup_focused = False
            self._fg_at_show = int(user32.GetForegroundWindow() or 0)
        threading.Timer(0.35, self._refresh_popup_layout).start()
        threading.Timer(0.15, set_window_icons).start()

    # ---------- 点空白处收起小窗 ----------

    def _own_windows(self):
        """本进程的窗口句柄集合：这些窗口拿到焦点时不应收起小窗。"""
        out = set()
        for title in (POPUP_TITLE, MAIN_TITLE, LOGIN_TITLE):
            h = user32.FindWindowW(None, title)
            if h:
                out.add(int(h))
        tray = getattr(self, "tray", None)
        if tray is not None and getattr(tray, "hwnd", None):
            out.add(int(tray.hwnd))
        return out

    def _watch_popup_focus(self):
        """点小窗之外的任何地方就收起。

        小窗是独立顶层窗口，点桌面/别的程序不会触发任何 JS 事件，只能轮询前台窗口。
        判定规则（避免"抢不到焦点就自动消失"的误伤）：
        1. 前台是本进程窗口（小窗/主窗/登录窗/托盘）→ 不动；
        2. 前台 == 弹出瞬间的前台（说明我们没抢到焦点，用户还没操作）→ 不动；
        3. 其余情况 = 用户把焦点给了别的窗口 → 收起。
        """
        log("popup focus watcher started")
        while not getattr(self, "quitting", False):
            time.sleep(0.3)
            try:
                hwnd = user32.FindWindowW(None, POPUP_TITLE)
                if not hwnd or not user32.IsWindowVisible(hwnd):
                    continue
                if time.time() - getattr(self, "_popup_shown_at", 0) < 0.85:
                    continue
                fg = int(user32.GetForegroundWindow() or 0)
                if not fg:
                    continue
                if fg in self._own_windows():
                    self._popup_focused = True
                    continue
                if not getattr(self, "_popup_focused", False):
                    continue   # 还没真正获得过焦点：不判断，交给前端 blur 兜底
                log(f"popup lost focus (fg={fg}), hide")
                self.hide_popup()
            except Exception as e:
                log(f"popup focus watcher error: {e}")

    def hide_popup(self):
        try:
            if self.popup:
                self.popup.hide()
        except Exception:
            pass
        hwnd = user32.FindWindowW(None, POPUP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 0)

    def show_main(self):
        try:
            if self.popup:
                self.popup.hide()
        except Exception:
            pass
        ph = user32.FindWindowW(None, POPUP_TITLE)
        if ph:
            user32.ShowWindow(ph, 0)
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
            self._nudge(hwnd)
            try:
                user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        threading.Timer(0.2, lambda: self._safe_js(self.main, "App.refreshLayout()")).start()
        threading.Timer(0.3, set_window_icons).start()

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
        """绝对定位（pywebview 自带的 drag 会把增量叠加，导致窗口越拖越快）。"""
        hwnd = find_hwnd(MAIN_TITLE)
        try:
            if not hwnd:
                return {"ok": False}
            if getattr(self, "_maxed", False):      # 拖动即还原
                self._maxed = False
            user32.SetWindowPos(hwnd, None, int(x), int(y), int(w or 0), int(h or 0),
                                SWP_NOZORDER | SWP_NOACTIVATE | (0 if (w and h) else SWP_NOSIZE))
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def win_min(self):
        """最小化：进入系统最小化，由 _watch_minimize 转成右下角小窗。"""
        hwnd = find_hwnd(MAIN_TITLE)
        try:
            if hwnd:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            elif self.main:
                self.main.minimize()
            return {"ok": True}
        except Exception as e:
            log(f"win_min error: {e}")
            return {"ok": False, "error": str(e)}

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

    def _align_popup(self, hwnd, attempt=0):
        """按客户区把窗口贴到右下角；窗口初始化会异步覆盖尺寸，因此带重试。"""
        try:
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

            if attempt == 0:
                log(f"align: client={cr.right}x{cr.bottom} target={target_w}x{target_h} dx={dx} dy={dy}")
            # pywebview 在窗口显示后会再套用一次自身布局，因此固定重试一段时间
            if attempt < 8:
                threading.Timer(0.15, lambda: self._align_popup(hwnd, attempt + 1)).start()
        except Exception as e:
            log(f"align error: {e}")

    @staticmethod
    def _nudge(hwnd):
        """隐藏期间创建的 WebView2 可能不出帧，轻微改变尺寸强制其重新布局并绘制。"""
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
        if not self.popup:
            return
        try:
            visible = bool(user32.IsWindowVisible(user32.FindWindowW(None, POPUP_TITLE)))
        except Exception:
            visible = False
        log(f"toggle popup, visible={visible}")
        if visible:
            self.hide_popup()
        else:
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
        return True

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
    else:
        def greeting():
            time.sleep(3.0)
            tray.notify("WorkBuddy 积分观测台已在托盘",
                        "单击托盘图标，在屏幕右下角查看今日真实积分用量。")
        threading.Thread(target=greeting, daemon=True).start()

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
