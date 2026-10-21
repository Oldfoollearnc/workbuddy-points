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

hiddenimports = locals().get("hiddenimports", []) + [
    "clr",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=locals().get("binaries", []),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL.ImageQt"],
    noarchive=False,
)
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
