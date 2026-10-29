# -*- coding: utf-8 -*-
"""
生成 Inno Setup 安装向导用的品牌图。

为什么用脚本而不是切图：
- 向导图对尺寸敏感（modern 样式：左图 164x314、右上 55x55），手改尺寸容易糊；
- 4 倍超采样后再缩小，中文字形在 100%/125%/150% DPI 下都锐利；
- 版本/文案调整时重跑一次即可，不需要美术资源。

这一版的设计：左图改成整幅深蓝渐变（带两圈光晕 + 斜向高光），文字白系，
比原来白底 + 三行小字更接近现代安装器的观感；右上角小图保留图标但带透明通道
（配合 .iss 里的 WizardSmallImageAlphaFormat=defined，深色模式下不会出现白底方块）。

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

# 与 assets/css/tokens.css 的亮色主题同源：品牌蓝为主，往深蓝压出层次
TOP = (13, 35, 82)          # 顶部：近黑的深蓝
BOT = (37, 99, 235)         # 底部：--brand
WHITE = (255, 255, 255)

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


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def draw_wizard():
    img = Image.new("RGBA", (W * SS, H * SS), TOP + (255,))
    d = ImageDraw.Draw(img, "RGBA")

    # 竖向渐变：每行一条，超采样下也没有可见条带
    for y in range(H * SS):
        t = y / float(H * SS - 1)
        # 前 2/3 慢一点过渡，底部快速提亮，视觉重心落在文字区
        t = t ** 0.85
        d.line([(0, y), (W * SS, y)], fill=lerp(TOP, BOT, t) + (255,))

    # 右上角光晕：两圈半透明白，营造"聚光"
    cx, cy = px(150), px(20)
    for r, a in ((px(96), 26), (px(64), 30), (px(38), 34)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, a))

    # 左下斜向高光带
    d.polygon([(0, px(196)), (W * SS, px(132)), (W * SS, px(168)), (0, px(232))],
              fill=(255, 255, 255, 14))

    # 图标放在半透明圆角方块里，避免深底上图标边缘发灰
    icon_px = px(56)
    tile = px(64)
    ix, iy = px(22), px(38)
    d.rounded_rectangle([ix - px(4), iy - px(4), ix - px(4) + tile, iy - px(4) + tile],
                        radius=px(18), fill=(255, 255, 255, 34))
    icon = Image.open(ICON).convert("RGBA").resize((icon_px, icon_px), Image.LANCZOS)
    img.paste(icon, (ix, iy), icon)

    f_en = font("seguisb.ttf", px(21), "segoeuib.ttf")
    f_zh = font("msyhbd.ttc", px(18), "msyh.ttc")
    f_tag = font("msyh.ttc", px(9))
    f_body = font("msyh.ttc", px(9.5))
    f_foot = font("msyh.ttc", px(8))

    y = iy + tile + px(16)
    d.text((px(22), y), "WorkBuddy", font=f_en, fill=WHITE)
    y += px(27)
    d.text((px(22), y), "积分观测台", font=f_zh, fill=(235, 242, 255))

    # 品牌短横线（白，半透明）
    y += px(30)
    d.rounded_rectangle([px(22), y, px(22) + px(28), y + px(2.4)],
                        radius=px(1.2), fill=(255, 255, 255, 210))

    y += px(15)
    d.text((px(22), y), "看得见每一分花在哪", font=f_tag, fill=(210, 225, 250))

    # 三条卖点：左侧小白点 + 文案
    y += px(30)
    for line in ("本机额度实时可见", "云端逐笔可对账", "数据只留本机"):
        d.ellipse([px(22), y + px(4), px(22) + px(3.2), y + px(7.2)], fill=(255, 255, 255, 230))
        d.text((px(30), y), line, font=f_body, fill=(222, 233, 250))
        y += px(20)

    # 底部说明
    fy = H * SS - px(34)
    d.line([px(22), fy - px(12), W * SS - px(22), fy - px(12)], fill=(255, 255, 255, 60),
           width=max(1, px(0.6)))
    d.text((px(22), fy), "升级安装保留登录状态", font=f_foot, fill=(200, 216, 240))

    return img


def draw_small():
    """右上角小图：品牌色圆角块 + 图标。

    Inno 对「小图」没有 alpha 指令，透明 PNG 会被合到白底上，深色向导里就是一块白方块；
    所以这里直接画成不透明的圆角方块，浅色/深色都成立。
    """
    size = 55 * SS
    img = Image.new("RGB", (size, size), TOP)
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=lerp(TOP, BOT, y / float(size - 1)))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=px(13), fill=255)
    out = Image.new("RGB", (size, size), (255, 255, 255))
    out.paste(img, (0, 0), mask)
    icon_px = px(38)
    icon = Image.open(ICON).convert("RGBA").resize((icon_px, icon_px), Image.LANCZOS)
    out.paste(icon, ((size - icon_px) // 2, (size - icon_px) // 2), icon)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    big = draw_wizard()
    big.resize((W, H), Image.LANCZOS).save(os.path.join(OUT, "wizard.png"))
    big.save(os.path.join(OUT, "wizard@2x.png"))       # 328x628，2x DPI

    small = draw_small()
    small.resize((55, 55), Image.LANCZOS).save(os.path.join(OUT, "wizard-small.png"))
    small.save(os.path.join(OUT, "wizard-small@2x.png"))   # 保留 RGBA

    for name in ("wizard.png", "wizard@2x.png", "wizard-small.png", "wizard-small@2x.png"):
        p = os.path.join(OUT, name)
        print("%-18s %6.1f KB" % (name, os.path.getsize(p) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
