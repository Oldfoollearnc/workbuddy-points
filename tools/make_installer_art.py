# -*- coding: utf-8 -*-
"""
生成 Inno Setup 安装向导用的品牌图。

为什么用脚本而不是切图：
- 向导图对尺寸敏感（modern 样式：左图 164x314、右上 55x55），手改尺寸容易糊；
- 4 倍超采样后再缩小，中文字形在 100%/125%/150% DPI 下都锐利；
- 版本/文案调整时重跑一次即可，不需要美术资源。

产物（installer/art/）：
    wizard.png / wizard@2x.png         左侧竖图 164x314 / 328x628
    wizard-small.png / @2x            右上小图 55x55 / 110x110

用法：.venv\\Scripts\\python.exe tools\\make_installer_art.py
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "installer", "art")
ICON = os.path.join(ROOT, "assets", "icon.png")

SS = 4                      # 超采样倍数
W, H = 164, 314             # modern 样式左侧图尺寸（逻辑像素）

# 配色取自 assets/css/tokens.css 亮色主题，保证安装器与程序同一套观感
WHITE = (255, 255, 255)
INK = (12, 26, 48)          # --text-1  #0c1a30
INK_2 = (71, 85, 105)       # 次级文字
INK_3 = (148, 163, 184)     # 弱化文字
BRAND = (37, 99, 235)       # --brand   #2563eb
LINE = (226, 232, 240)      # 分隔线

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def font(name, size, fallback=None):
    for cand in (name, fallback):
        if not cand:
            continue
        p = cand if os.path.isabs(cand) else os.path.join(FONT_DIR, cand)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def px(v):
    """逻辑像素 -> 超采样像素"""
    return int(round(v * SS))


def draw_wizard():
    img = Image.new("RGB", (W * SS, H * SS), WHITE)
    d = ImageDraw.Draw(img)

    # 顶部 3px 品牌色带：给白底一个明确的起点，不喧哗
    d.rectangle([0, 0, W * SS, px(3)], fill=BRAND)

    # 图标 52x52，圆角已在 png 内
    icon_px = px(52)
    icon = Image.open(ICON).convert("RGBA").resize((icon_px, icon_px), Image.LANCZOS)
    ix, iy = px(24), px(40)
    img.paste(icon, (ix, iy), icon)

    f_en = font("seguisb.ttf", px(22), "segoeuib.ttf")
    f_zh = font("msyhbd.ttc", px(19), "msyh.ttc")
    f_body = font("msyh.ttc", px(10.5))
    f_foot = font("msyh.ttc", px(8.5))

    y = iy + icon_px + px(18)
    d.text((px(24), y), "WorkBuddy", font=f_en, fill=INK)
    y += px(30)
    d.text((px(24), y), "积分观测台", font=f_zh, fill=INK)

    # 品牌短横线
    y += px(36)
    d.rectangle([px(24), y, px(24) + px(26), y + px(2)], fill=BRAND)

    # 三行卖点，等距排列
    y += px(16)
    for line in ("本机额度实时可见", "云端逐笔可对账", "数据只留本机"):
        d.text((px(24), y), line, font=f_body, fill=INK_2)
        y += px(19)

    # 底部说明
    fy = H * SS - px(38)
    d.line([px(24), fy - px(10), W * SS - px(24), fy - px(10)], fill=LINE, width=max(1, px(0.6)))
    d.text((px(24), fy), "升级安装保留登录状态", font=f_foot, fill=(100, 116, 139))

    return img


def draw_small():
    img = Image.new("RGBA", (55 * SS, 55 * SS), (0, 0, 0, 0))
    icon_px = px(44)
    icon = Image.open(ICON).convert("RGBA").resize((icon_px, icon_px), Image.LANCZOS)
    img.paste(icon, ((55 * SS - icon_px) // 2, (55 * SS - icon_px) // 2), icon)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    big = draw_wizard()
    big.resize((W, H), Image.LANCZOS).save(os.path.join(OUT, "wizard.png"))
    big.save(os.path.join(OUT, "wizard@2x.png"))

    small = draw_small()
    small.resize((55, 55), Image.LANCZOS).save(os.path.join(OUT, "wizard-small.png"))
    small.save(os.path.join(OUT, "wizard-small@2x.png"))

    for name in ("wizard.png", "wizard@2x.png", "wizard-small.png", "wizard-small@2x.png"):
        p = os.path.join(OUT, name)
        print("%-18s %6.1f KB" % (name, os.path.getsize(p) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
