# -*- coding: utf-8 -*-
"""
应用路径解析 —— 把「只读资源」与「可写数据」分开。

为什么需要这份文件（安装版必须）
--------------------------------
开发态程序放在项目目录，`account.json / session.bin / logs / .cache` 直接写在旁边没问题。
打包成安装版后程序位于 `C:\\Program Files\\...`，普通用户对该目录**没有写权限**，
一旦往那里写文件就会抛 PermissionError，表现为"同步失败 / 日志写不进去"。

因此约定：
    资源（index.html / assets / icon）  → app_dir()    （源码目录，打包后为 exe 所在目录）
    可写数据（缓存 / 会话 / 配置 / 日志）→ data_dir()   （安装版 = %APPDATA%\\WorkBuddyPoints）

开发态 data_dir() 仍返回项目目录，避免影响已有调试数据与既有用户配置。
"""
import os
import shutil
import sys

APP_NAME = "WorkBuddyPoints"
_LOCK_FILE = ".portable"


def is_frozen():
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """资源根目录：源码运行时是项目目录；打包后是 exe 所在目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir():
    """静态资源目录（onefile 模式下资源被解到 _MEIPASS）。"""
    return getattr(sys, "_MEIPASS", app_dir())


def _roaming():
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return base


def data_dir():
    """可写数据目录。

    - 源码运行：项目目录（保持原有行为，方便排查）
    - 安装版：%APPDATA%\\WorkBuddyPoints（一定有写权限、随用户漫游）
    - 便携版：若 exe 同级放了 `.portable` 文件，则用 exe 同级 `data/`（U 盘场景）
    """
    if not is_frozen():
        return app_dir()
    home = app_dir()
    if os.path.exists(os.path.join(home, _LOCK_FILE)):
        d = os.path.join(home, "data")
    else:
        d = os.path.join(_roaming(), APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def path(*parts, **kw):
    """资源路径：path('assets', 'icon.ico')。"""
    base = bundle_dir() if kw.get("bundle") else app_dir()
    return os.path.join(base, *parts)


def data_path(*parts):
    """可写路径：data_path('logs', 'app.log')，父目录自动创建。"""
    p = os.path.join(data_dir(), *parts)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return p


def migrate_legacy(names=()):
    """把旧版本写在程序目录的数据搬到 data_dir（仅当新位置不存在时）。

    安装升级时用户既有的登录态/额度配置不会丢，这是"安装即用"的一部分。
    """
    if os.path.abspath(data_dir()) == os.path.abspath(app_dir()):
        return []
    moved = []
    for name in names:
        src = os.path.join(app_dir(), name)
        dst = os.path.join(data_dir(), name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.makedirs(os.path.dirname(dst) or data_dir(), exist_ok=True)
                shutil.copy2(src, dst)
                moved.append(name)
            except Exception:
                pass
    return moved
