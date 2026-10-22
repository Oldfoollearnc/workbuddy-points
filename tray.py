# -*- coding: utf-8 -*-
"""
Windows 系统托盘（原生 Win32 实现）

为什么不用第三方托盘库：本应用要求「单击托盘图标即弹出快捷面板」，
而多数跨平台托盘库只暴露双击/右键菜单。这里直接走 Shell_NotifyIconW，
可以精确拿到 WM_LBUTTONUP / WM_RBUTTONUP。
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
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_MEASUREITEM = 0x002C
WM_DRAWITEM = 0x002B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x10, 0x40, 0x8000
MF_STRING, MF_SEPARATOR, MF_OWNERDRAW = 0, 0x800, 0x0100
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_BOTTOMALIGN = 0x100, 0x2, 0x20

ODS_SELECTED = 0x0001
ODT_MENU = 1
MIM_BACKGROUND, MIM_STYLE = 0x00000002, 0x00000010
MNS_NOCHECK = 0x80000000
TRANSPARENT_BK = 1
DT_SINGLELINE, DT_VCENTER, DT_LEFT = 0x20, 0x4, 0x0
NULL_BRUSH, NULL_PEN = 5, 8

ID_OPEN_POPUP, ID_OPEN_MAIN, ID_REFRESH, ID_EXIT = 1, 2, 3, 4

# 菜单配色（跟随程序主题，取的是界面上同一套色）
PALETTE = {
    "light": {"bg": "#FFFFFF", "text": "#122036", "hover_bg": "#EAF1FE",
              "hover_text": "#1D4ED8", "sep": "#E7EEF9"},
    "dark": {"bg": "#101A2B", "text": "#E9EEF8", "hover_bg": "#1B2942",
             "hover_text": "#74A6FF", "sep": "#22304A"},
}


def _rgb(hexstr):
    """#RRGGBB → COLORREF(0x00BBGGRR)"""
    h = (hexstr or "#000000").lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MENUINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.DWORD),
        ("dwStyle", wintypes.DWORD), ("cyMax", wintypes.UINT),
        ("hbrBack", wintypes.HBRUSH), ("dwContextHelpID", wintypes.DWORD),
        ("dwMenuData", ctypes.c_size_t),
    ]


class MEASUREITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT), ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT), ("itemWidth", wintypes.UINT),
        ("itemHeight", wintypes.UINT), ("itemData", ctypes.c_size_t),
    ]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT), ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT), ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT), ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC), ("rcItem", RECT),
        ("itemData", ctypes.c_size_t),
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
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
user32.SetMenuInfo.argtypes = [wintypes.HMENU, ctypes.POINTER(MENUINFO)]
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
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

# ---- 自绘菜单用到的 GDI ----
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(RECT), wintypes.UINT]
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]


class Tray:
    """系统托盘图标：左键单击 → 快捷面板开/关；右键菜单 → 主界面、刷新、退出。"""

    def __init__(self, icon_path, on_click=None, on_open_main=None, on_refresh=None, on_exit=None,
                 tooltip="WorkBuddy 积分观测台 · 单击查看今日积分", logger=None):
        self.log = logger or (lambda msg: None)
        self.icon_path = icon_path
        self.on_click = on_click
        self.on_open_main = on_open_main
        self.on_refresh = on_refresh
        self.on_exit = on_exit
        self.tooltip = tooltip[:120]

        self.hwnd = None
        self.hicon = None
        self._nid = None
        self._proc = WNDPROC(self._wndproc)
        self._thread = None
        self._cls = "PointsInsightTrayWnd"

        # 自绘菜单状态
        self.theme = "light"
        self._menu_items = []      # [(id, label, color|None)]
        self._font = None
        self._bg_brush = None
        self._hover_brush = None
        self._accent_brushes = {}

    def set_theme(self, theme):
        self.theme = "dark" if theme == "dark" else "light"

    def _pal(self):
        return PALETTE.get(self.theme, PALETTE["light"])

    def _ensure_font(self):
        if self._font:
            return self._font
        # 进程是 DPI 无关的，这里的 -17 ≈ 12.7px 字号，和界面里的 12.5px 对齐。
        # 参数：高,宽,倾角,角度,字重,斜体,下划线,删除线,字符集(1=DEFAULT),
        #      输出精度(4=TT),裁剪(0),质量(5=CLEARTYPE),字族(0)
        self._font = gdi32.CreateFontW(
            -17, 0, 0, 0, 400, 0, 0, 0, 1, 4, 0, 5, 0, "Microsoft YaHei UI")
        if not self._font:
            self._font = gdi32.CreateFontW(
                -17, 0, 0, 0, 400, 0, 0, 0, 1, 4, 0, 5, 0, "Segoe UI")
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

    def stop(self):
        try:
            if self._nid:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass

    # ---------- 消息处理 ----------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONUP:
                    self._safe(self.on_click)
                elif lparam in (WM_RBUTTONUP,):
                    self._popup_menu()
                elif lparam == WM_LBUTTONDBLCLK:
                    # 双击**不能**再开主界面：一次双击本来就产生了两次 WM_LBUTTONUP
                    # （开一次、关一次），再叠一个 open_main 就会让主界面闪一下又消失，
                    # 用户看到的就是"连点几下，主界面闪出来了"。
                    # 进主界面只走小窗里的「展开完整面板」或右键菜单。
                    return 0
                return 0
            if msg == WM_MEASUREITEM:
                mis = ctypes.cast(ctypes.c_void_p(lparam),
                                  ctypes.POINTER(MEASUREITEMSTRUCT)).contents
                if self._measure_item(mis):
                    return 1
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            if msg == WM_DRAWITEM:
                dis = ctypes.cast(ctypes.c_void_p(lparam),
                                  ctypes.POINTER(DRAWITEMSTRUCT)).contents
                if dis.CtlType == ODT_MENU:
                    try:
                        self._draw_item(dis)
                    except Exception as e:
                        self.log(f"menu draw error: {e}")
                    return 1
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self):
        """自绘右键菜单：跟随程序主题，深色/浅色都是一套自己的观感。"""
        self._menu_items = [
            (ID_OPEN_POPUP, "打开快捷面板", "#2563EB"),
            (ID_OPEN_MAIN, "展开完整主界面", "#06B6D4"),
            (ID_REFRESH, "刷新数据", "#10B981"),
            (0, None, None),
            (ID_EXIT, "退出", "#DC2626"),
        ]
        pal = self._pal()
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        cmd = 0
        try:
            for idx, (mid, label, _accent) in enumerate(self._menu_items):
                if label is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    # itemData 传下标，绘制时按下标取标题/颜色
                    user32.AppendMenuW(menu, MF_OWNERDRAW, mid, ctypes.c_void_p(idx))

            mi = MENUINFO()
            mi.cbSize = ctypes.sizeof(MENUINFO)
            mi.fMask = MIM_BACKGROUND | MIM_STYLE
            mi.dwStyle = MNS_NOCHECK
            mi.hbrBack = gdi32.CreateSolidBrush(_rgb(pal["bg"]))
            user32.SetMenuInfo(menu, ctypes.byref(mi))
            self._bg_brush = mi.hbrBack
            self._hover_brush = gdi32.CreateSolidBrush(_rgb(pal["hover_bg"]))
            self._ensure_font()

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                pt.x, pt.y, 0, self.hwnd, None,
            )
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        except Exception as e:
            self.log(f"menu error: {e}")
        finally:
            for b in (self._bg_brush, self._hover_brush):
                if b:
                    gdi32.DeleteObject(ctypes.c_void_p(b))
            self._bg_brush = self._hover_brush = None
            user32.DestroyMenu(menu)

        if cmd == ID_OPEN_POPUP:
            self._safe(self.on_click)
        elif cmd == ID_OPEN_MAIN:
            self._safe(self.on_open_main)
        elif cmd == ID_REFRESH:
            self._safe(self.on_refresh)
        elif cmd == ID_EXIT:
            self._safe(self.on_exit)

    def _measure_item(self, mis):
        if mis.CtlType != ODT_MENU:
            return False
        mis.itemHeight = 34
        mis.itemWidth = 212
        return True

    def _draw_item(self, dis):
        idx = int(dis.itemData or 0)
        if idx < 0 or idx >= len(self._menu_items):
            return
        mid, label, accent = self._menu_items[idx]
        if not label or not dis.hDC:
            return
        pal = self._pal()
        hdc = dis.hDC
        rc = dis.rcItem
        selected = bool(dis.itemState & ODS_SELECTED)

        if self._bg_brush:
            user32.FillRect(hdc, ctypes.byref(rc), self._bg_brush)

        null_pen = gdi32.GetStockObject(NULL_PEN)
        # 选中态：圆角胶囊底
        if selected and self._hover_brush:
            r = RECT(rc.left + 6, rc.top + 3, rc.right - 6, rc.bottom - 3)
            old_b = gdi32.SelectObject(hdc, self._hover_brush)
            old_p = gdi32.SelectObject(hdc, null_pen)
            gdi32.RoundRect(hdc, r.left, r.top, r.right, r.bottom, 10, 10)
            gdi32.SelectObject(hdc, old_b)
            gdi32.SelectObject(hdc, old_p)

        # 左侧强调圆点
        dot = gdi32.CreateSolidBrush(_rgb(accent or pal["hover_text"]))
        if dot:
            old_b = gdi32.SelectObject(hdc, dot)
            old_p = gdi32.SelectObject(hdc, null_pen)
            cy = (rc.top + rc.bottom) // 2
            gdi32.RoundRect(hdc, rc.left + 16, cy - 3, rc.left + 22, cy + 3, 6, 6)
            gdi32.SelectObject(hdc, old_b)
            gdi32.SelectObject(hdc, old_p)
            gdi32.DeleteObject(dot)

        font = self._ensure_font()
        old_font = gdi32.SelectObject(hdc, font) if font else None
        gdi32.SetBkMode(hdc, TRANSPARENT_BK)
        if selected:
            color = pal["hover_text"]
        elif mid == ID_EXIT:
            color = accent
        else:
            color = pal["text"]
        gdi32.SetTextColor(hdc, _rgb(color))
        tr = RECT(rc.left + 34, rc.top, rc.right - 14, rc.bottom)
        user32.DrawTextW(hdc, label, -1, ctypes.byref(tr),
                         DT_SINGLELINE | DT_VCENTER | DT_LEFT)
        if old_font:
            gdi32.SelectObject(hdc, old_font)

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
