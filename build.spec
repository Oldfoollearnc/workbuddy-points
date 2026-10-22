# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（onedir）。

为什么用 onedir 而不是 onefile：
- onedir 启动快（无需每次解压），WebView2 资源文件可被浏览器进程直接映射；
- onefile 每次启动要把 ~80MB 解到 %TEMP%，首屏明显变慢，且杀软误报率更高。

产物：dist/WorkBuddyPoints/WorkBuddyPoints.exe
安装包：installer/Output/WorkBuddyPoints-Setup-<version>.exe（Inno Setup 编译）
"""
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ("index.html", "."),
    ("assets", "assets"),
    ("version.json", "."),
]

# pywebview 的 JS 注入文件、pythonnet 的运行时 DLL 都必须带上
for pkg in ("webview", "pythonnet", "clr_loader"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries = locals().get("binaries", []) + b
        hiddenimports = locals().get("hiddenimports", []) + h
    except Exception:
        pass

datas += collect_data_files("webview")


# ---------------------------------------------------------------------------
# 体积裁剪：webview 的 collect_all 会把所有平台后端原样塞进产物
# （gtk/qt/cocoa/cef/mshtml/android，外加 android jar 与 x86/arm64 原生库）。
# 本程序只出 Windows x64，走 winforms + edgechromium，其余全部剔除。
# 实测可省 ~330KB 产物，安装包层面等价于去掉一整块无用解压。
# ---------------------------------------------------------------------------
_DROP = (
    "webview/platforms/gtk.py",
    "webview/platforms/qt.py",
    "webview/platforms/cocoa.py",
    "webview/platforms/cef.py",
    "webview/platforms/mshtml.py",
    "webview/platforms/win32.py",
    "webview/platforms/android/",
    "webview/lib/pywebview-android.jar",
)


def _drop_foreign(items):
    kept = []
    for it in items:
        # datas 输入是 (src, dest)，Analysis 产出的 TOC 是 (name, path, typecode)，
        # 两种都看一遍，避免漏网
        head = " ".join(str(x) for x in it[:2]).replace("\\", "/").lower()
        if any(p in head for p in _DROP):
            continue
        kept.append(it)
    return kept


datas = _drop_foreign(datas)
binaries = _drop_foreign(locals().get("binaries", []))

hiddenimports = locals().get("hiddenimports", []) + [
    "clr",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI 框架用 pywebview，Tk/Qt 一律不要
        "tkinter", "_tkinter", "matplotlib", "numpy", "pandas",
        "PIL", "PIL.ImageQt",
        # 数据分析/测试/文档/包管理，运行时用不到
        # 注意：不要把 distutils 写进 excludes —— PyInstaller 的 hook-distutils
        # 会把它 alias 到自家 vendored 副本，声明排除会直接报
        # "Target module distutils already imported as ExcludedModule"。
        "unittest", "doctest", "pydoc", "pydoc_data", "test",
        "setuptools", "pip", "lib2to3",
    ],
    noarchive=False,
)

# PyInstaller 自带的 hook-webview.py 会在 Analysis 内部把整包再收一遍，
# 所以最终生效的过滤点必须放在 Analysis 之后。
a.datas = _drop_foreign(a.datas)
a.binaries = _drop_foreign(a.binaries)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WorkBuddyPoints",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 托盘程序，不弹黑框
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WorkBuddyPoints",
)
