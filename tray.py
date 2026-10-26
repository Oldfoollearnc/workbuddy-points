# -*- coding: utf-8 -*-
"""
Windows 系统托盘（原生 Win32 实现）

为什么不用第三方托盘库：本应用要求「单击托盘图标即弹出快捷面板」，
而多数跨平台托盘库只暴露双击/右键菜单。这里直接走 Shell_NotifyIconW，
可以精确拿到 WM_LBUTTONUP / WM_RBUTTONUP。

右键菜单
--------
系统菜单（TrackPopupMenu）的外框、圆角、阴影都不可控，怎么自绘都有一股
"XP 时代"的味道。所以这里自己建一个 WS_POPUP 小窗口来画菜单：
圆角窗口区域 + 系统投影 + 自绘 hover 胶囊，配色跟随应用主题。
"""
import ctypes
import os
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_MENU_CMD = WM_APP + 2
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_MOUSEMOVE = 0x0200
WM_KEYDOWN = 0x0100
WM_ACTIVATE = 0x0006
WM_NCHITTEST = 0x0084
WM_KILLFOCUS = 0x0008
WM_SETCURSOR = 0x0020
IDC_ARROW = 32512

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_NONE, NIIF_INFO, NIIF_WARNING = 0, 1, 2
NIIF_INFO = 0x1
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x10, 0x40, 0x8000
MF_STRING, MF_SEPARATOR = 0, 0x800
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_BOTTOMALIGN = 0x100, 0x2, 0x20

TRANSPARENT_BK = 1
DT_SINGLELINE, DT_VCENTER, DT_LEFT, DT_CENTER = 0x20, 0x4, 0x0, 0x1
NULL_BRUSH, NULL_PEN = 5, 8
CS_VREDRAW, CS_HREDRAW, CS_DROPSHADOW = 0x0001, 0x0002, 0x00020000
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x00000080, 0x00000008
SW_SHOW = 5
HTCLIENT = 1
VK_ESCAPE = 0x1B

ID_OPEN_POPUP, ID_OPEN_MAIN, ID_REFRESH, ID_EXIT = 1, 2, 3, 4
ID_SYNC, ID_AUTOSTART, ID_SETTINGS = 5, 6, 7
ID_UPDATE = 8

# 菜单尺寸（逻辑像素；进程 DPI 无关，和界面 1:1 对应）
MENU_W = 158        # 比原来小一圈：9 行压到 178px 高（原来 204）
ITEM_H = 26
MENU_PAD = 5
SEP_H = 6
MENU_RADIUS = 12

# 配色跟随程序主题
PALETTE = {
    "light": {"bg": "#FFFFFF", "text": "#122036", "hover_bg": "#EAF1FE",
              "hover_text": "#1D4ED8", "sep": "#E7EEF9", "border": "#DCE5F4"},
    "dark": {"bg": "#101A2B", "text": "#E9EEF8", "hover_bg": "#1B2942",
             "hover_text": "#74A6FF", "sep": "#22304A", "border": "#2A3A56"},
}


def _rgb(hexstr):
    """#RRGGBB → COLORREF(0x00BBGGRR)"""
    h = (hexstr or "#000000").lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", RECT),
        ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# GetModuleHandleW 必须声明返回值：默认按 c_int 处理会在 64 位下截断模块句柄，
# 传给 RegisterClassW 后 Windows 拿到一个非法 HINSTANCE，创建窗口时直接 access violation。
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = ctypes.c_void_p
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.SetCursor.argtypes = [ctypes.c_void_p]
user32.SetCursor.restype = ctypes.c_void_p
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(RECT), wintypes.UINT]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.CreatePen.restype = ctypes.c_void_p
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int]
gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p


def _work_area():
    r = RECT()
    try:
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class Tray:
    """系统托盘图标：左键单击 → 快捷面板；右键 → 自绘菜单。"""

    def __init__(self, icon_path, on_click=None, on_open_main=None, on_refresh=None, on_exit=None,
                 on_sync=None, on_autostart=None, autostart_state=None, on_settings=None,
                 on_update=None,
                 tooltip="WorkBuddy 积分观测台 · 单击查看今日积分", logger=None):
        self.log = logger or (lambda msg: None)
        self.icon_path = icon_path
        self.on_click = on_click
        self.on_open_main = on_open_main
        self.on_refresh = on_refresh
        self.on_exit = on_exit
        self.on_sync = on_sync
        self.on_autostart = on_autostart
        self.autostart_state = autostart_state      # 返回 bool，用来画勾
        self.on_settings = on_settings
        self.on_update = on_update
        self.tooltip = tooltip[:120]

        self.hwnd = None
        self.hicon = None
        self._nid = None
        self._proc = WNDPROC(self._wndproc)
        self._thread = None
        self._cls = "PointsInsightTrayWnd"

        # 菜单状态
        self.theme = "light"
        self._menu_hwnd = None
        self._menu_proc = WNDPROC(self._menu_wndproc)
        self._menu_items = []
        self._menu_hit = []          # [(cmd, y0, y1)]
        self._menu_hover = -1
        self._menu_w = MENU_W
        self._menu_h = 0
        self._font = None
        self._arrow = None

    # ---------- 主题 / 字体 ----------

    def balloon(self, title, text, level="info"):
        """在托盘图标上弹一条系统气泡。

        只在主窗和小窗都不可见时才用 —— 系统气泡的样式改不了，
        能弹在界面里就弹界面里的 toast。
        """
        if not self.hwnd:
            return False
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = (title or "")[:63]
            nid.szInfo = (text or "")[:255]
            nid.dwInfoFlags = NIIF_WARNING if level == "warn" else NIIF_INFO
            ok = bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
            if not ok:
                self.log("balloon: Shell_NotifyIconW 返回 False")
            return ok
        except Exception as e:
            self.log(f"balloon error: {e}")
            return False

    def set_theme(self, theme):
        self.theme = "dark" if theme == "dark" else "light"

    def _pal(self):
        return PALETTE.get(self.theme, PALETTE["light"])

    def _ensure_cursor(self):
        """菜单窗口必须显式给一个箭头光标。

        以前窗口类没设 hCursor、也没处理 WM_SETCURSOR，鼠标移到菜单上会一直显示
        系统上一次留下的"后台运行"指针（箭头旁边转圈），看起来像程序卡死了。
        """
        if not self._arrow:
            self._arrow = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        return self._arrow

    def _ensure_font(self):
        if self._font:
            return self._font
        # 进程 DPI 无关：-17 ≈ 12.7px，和界面 12.5px 字号对齐
        self._font = gdi32.CreateFontW(
            -14, 0, 0, 0, 400, 0, 0, 0, 1, 4, 0, 5, 0, "Microsoft YaHei UI")
        if not self._font:
            self._font = gdi32.CreateFontW(
                -14, 0, 0, 0, 400, 0, 0, 0, 1, 4, 0, 5, 0, "Segoe UI")
        return self._font

    # ---------- 生命周期 ----------

    def start(self):
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._run_inner()
        except Exception:
            import traceback
            self.log("tray fatal\n" + traceback.format_exc())

    def _run_inner(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self._cls
        user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, self._cls, "PointsInsightTray", 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not self.hwnd:
            raise OSError("创建托盘消息窗口失败 err=%s" % ctypes.get_last_error())
        self.log(f"tray hwnd={self.hwnd}")

        # 图标
        if self.icon_path and os.path.exists(self.icon_path):
            self.hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not self.hicon:
            self.hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tooltip
        self._nid = nid

        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError("注册托盘图标失败 err=%s" % ctypes.get_last_error())
        self.log("tray icon registered")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def set_tip(self, text):
        """改托盘悬浮提示（今天用了多少 / 还能用多少），不重建图标。"""
        text = (text or "")[:120]
        if text == self.tooltip:
            return
        self.tooltip = text
        nid = getattr(self, "_nid", None)
        if not nid or not self.hwnd:
            return
        try:
            nid.szTip = text
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception as e:
            self.log(f"set_tip failed: {e}")

    def stop(self):
        try:
            if self._nid:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._menu_hwnd:
                user32.DestroyWindow(self._menu_hwnd)
                self._menu_hwnd = None
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass

    # ---------- 托盘消息 ----------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONUP:
                    self._safe(self.on_click)
                elif lparam in (WM_RBUTTONUP,):
                    self._popup_menu()
                elif lparam == WM_LBUTTONDBLCLK:
                    # 双击已经产生两次 WM_LBUTTONUP（开一次、关一次），这里再开主界面
                    # 会闪一下；主界面只走小窗里的「展开完整面板」或右键菜单。
                    return 0
                return 0
            if msg == WM_MENU_CMD:
                self._dispatch(int(wparam or 0))
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------- 菜单 ----------

    def _menu_layout(self):
        """算出每个可见项的位置；切换/分隔线都参与布局。"""
        self._menu_hit = []
        y = MENU_PAD
        for mid, label, _accent in self._menu_items:
            if label is None:
                self._menu_hit.append((0, y, y + SEP_H))
                y += SEP_H
            else:
                self._menu_hit.append((mid, y, y + ITEM_H))
                y += ITEM_H
        self._menu_h = y + MENU_PAD

    def _ensure_menu_window(self):
        if self._menu_hwnd:
            return self._menu_hwnd
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW | CS_DROPSHADOW
        wc.lpfnWndProc = ctypes.cast(self._menu_proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.hCursor = self._ensure_cursor()
        wc.lpszClassName = "PointsInsightMenuWnd"
        user32.RegisterClassW(ctypes.byref(wc))       # 已注册会失败，忽略
        hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_TOPMOST, "PointsInsightMenuWnd", "",
            WS_POPUP, 0, 0, MENU_W, 200, None, None, hinst, None)
        if not hwnd:
            self.log("create menu window failed: %s" % ctypes.get_last_error())
            return None
        # 圆角窗口区域：系统接管这块 HRGN，之后不能再删
        user32.SetWindowRgn(hwnd, gdi32.CreateRoundRectRgn(
            0, 0, self._menu_w + 1, self._menu_h + 1, MENU_RADIUS, MENU_RADIUS), True)
        self._menu_hwnd = hwnd
        return hwnd

    def _popup_menu(self):
        auto_on = False
        try:
            auto_on = bool(self.autostart_state and self.autostart_state())
        except Exception:
            auto_on = False
        self._menu_items = [
            # 只留真正会用到的四个：自启 / 更新 / 设置 / 退出。
            # 打开面板、刷新、同步都能在界面里一步点到，塞进这里只是噪音。
            (ID_AUTOSTART, ("✓ 开机自启" if auto_on else "开机自启"), "#64748B"),
            (ID_UPDATE, "检查更新", "#F59E0B"),
            (ID_SETTINGS, "设置", "#0EA5E9"),
            (0, None, None),
            (ID_EXIT, "退出", "#DC2626"),
        ]
        self._menu_layout()
        hwnd = self._ensure_menu_window()
        if not hwnd:
            self._popup_menu_native()      # 兜底：自建窗口失败才用系统菜单
            return
        try:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            w, h = self._menu_w, self._menu_h
            wl, wt, wr, wb = _work_area()
            x = max(wl + 6, min(pt.x - w + 22, wr - w - 6))
            y = pt.y - h - 8
            if y < wt + 6:
                y = pt.y + 8
            y = max(wt + 6, min(y, wb - h - 6))
            self._menu_hover = -1
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, w, h, 0x0040)   # SWP_SHOWWINDOW
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.InvalidateRect(hwnd, None, True)
        except Exception as e:
            self.log(f"menu show error: {e}")

    def _hide_menu(self):
        if self._menu_hwnd:
            user32.ShowWindow(self._menu_hwnd, 0)
            self._menu_hover = -1

    def _menu_wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                self._paint_menu(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1                      # 全部由 _paint_menu 画，避免闪烁
            if msg == WM_MOUSEMOVE:
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                idx = self._hit(y)
                if idx != self._menu_hover:
                    self._menu_hover = idx
                    user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_LBUTTONUP:
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                idx = self._hit(y)
                cmd = self._menu_hit[idx][0] if idx >= 0 else 0
                self._hide_menu()
                if cmd:
                    user32.PostMessageW(self.hwnd, WM_MENU_CMD, cmd, 0)
                return 0
            if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
                self._hide_menu()
                return 0
            if msg == WM_KILLFOCUS:
                self._hide_menu()
                return 0
            if msg == WM_ACTIVATE:
                if (int(wparam) & 0xFFFF) == 0:      # WA_INACTIVE
                    self._hide_menu()
                return 0
            if msg == WM_SETCURSOR:
                cur = self._ensure_cursor()
                if cur:
                    user32.SetCursor(cur)
                    return 1
            if msg == WM_NCHITTEST:
                return HTCLIENT
        except Exception as e:
            self.log(f"menu wndproc error: {e}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _hit(self, y):
        for i, (_cmd, y0, y1) in enumerate(self._menu_hit):
            if y0 <= y < y1:
                return i
        return -1

    def _paint_menu(self, hwnd):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        if not hdc:
            return
        pal = self._pal()
        w, hh = self._menu_w, self._menu_h
        try:
            whole = RECT(0, 0, w, hh)
            bg = gdi32.CreateSolidBrush(_rgb(pal["bg"]))
            user32.FillRect(hdc, ctypes.byref(whole), bg)
            gdi32.DeleteObject(bg)

            # 1px 描边：窗口有圆角区域，边框跟着圆弧走
            pen = gdi32.CreatePen(0, 1, _rgb(pal["border"]))
            old_pen = gdi32.SelectObject(hdc, pen)
            old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
            gdi32.RoundRect(hdc, 0, 0, w, hh, MENU_RADIUS, MENU_RADIUS)
            gdi32.SelectObject(hdc, old_brush)
            gdi32.SelectObject(hdc, old_pen)
            gdi32.DeleteObject(pen)

            null_pen = gdi32.GetStockObject(NULL_PEN)
            font = self._ensure_font()
            old_font = gdi32.SelectObject(hdc, font) if font else None
            gdi32.SetBkMode(hdc, TRANSPARENT_BK)

            for i, (mid, label, accent) in enumerate(self._menu_items):
                y0, y1 = self._menu_hit[i][1], self._menu_hit[i][2]
                if label is None:
                    # 分隔线：短一点、淡一点，比系统那条通栏线克制
                    sp = gdi32.CreatePen(0, 1, _rgb(pal["sep"]))
                    op = gdi32.SelectObject(hdc, sp)
                    cy = (y0 + y1) // 2
                    gdi32.MoveToEx(hdc, 14, cy, None)
                    gdi32.LineTo(hdc, w - 14, cy)
                    gdi32.SelectObject(hdc, op)
                    gdi32.DeleteObject(sp)
                    continue

                hovered = (i == self._menu_hover)
                if hovered:
                    hb = gdi32.CreateSolidBrush(_rgb(pal["hover_bg"]))
                    ob = gdi32.SelectObject(hdc, hb)
                    op = gdi32.SelectObject(hdc, null_pen)
                    gdi32.RoundRect(hdc, 6, y0 + 2, w - 6, y1 - 2, 9, 9)
                    gdi32.SelectObject(hdc, ob)
                    gdi32.SelectObject(hdc, op)
                    gdi32.DeleteObject(hb)

                if hovered:
                    color = pal["hover_text"]
                elif mid == ID_EXIT:
                    color = accent
                else:
                    color = pal["text"]
                gdi32.SetTextColor(hdc, _rgb(color))
                # 文字整行居中：菜单本来就不宽，左对齐会显得左边重、右边空
                tr = RECT(0, y0, w, y1)
                user32.DrawTextW(hdc, label, -1, ctypes.byref(tr),
                                 DT_SINGLELINE | DT_VCENTER | DT_CENTER)
            if old_font:
                gdi32.SelectObject(hdc, old_font)
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _popup_menu_native(self):
        """兜底：自建菜单窗口创建失败时，退回系统菜单（能用就行）。"""
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, MF_STRING, ID_AUTOSTART, "开机自启（切换）")
        user32.AppendMenuW(menu, MF_STRING, ID_UPDATE, "检查更新")
        user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "设置")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
            pt.x, pt.y, 0, self.hwnd, None,
        )
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        self._dispatch(cmd)

    def _dispatch(self, cmd):
        # 菜单只剩四项：自启 / 更新 / 设置 / 退出。
        # 打开面板、刷新、同步都改成在界面里做，这里不再保留死分支。
        if cmd == ID_AUTOSTART:
            self._safe(self.on_autostart)
        elif cmd == ID_SETTINGS:
            self._safe(self.on_settings)
        elif cmd == ID_UPDATE:
            self._safe(self.on_update)
        elif cmd == ID_EXIT:
            self._safe(self.on_exit)

    @staticmethod
    def _safe(fn):
        if not fn:
            return
        try:
            fn()
        except Exception:
            pass

    # ---------- 气泡提示 ----------

    def notify(self, title, message):
        if not self._nid:
            return
        try:
            nid = self._nid
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = title[:60]
            nid.szInfo = message[:250]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        except Exception:
            pass
