# -*- coding: utf-8 -*-
"""生成应用图标（托盘 / 窗口 / favicon），无外部素材依赖。"""
import os
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "assets", "icon.ico")
PNG = os.path.join(BASE, "assets", "icon.png")

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角方底：品牌蓝竖向渐变
grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    r = int(88 + (37 - 88) * t)
    g = int(148 + (99 - 148) * t)
    b = int(255 + (235 - 255) * t)
    gd.line([(0, y), (S, y)], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.235), fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
# 顶部高光
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gdr = ImageDraw.Draw(glow)
gdr.ellipse([-S * 0.25, -S * 0.55, S * 1.05, S * 0.42], fill=(255, 255, 255, 46))
img.alpha_composite(Image.composite(glow, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

d = ImageDraw.Draw(img)
# 三根柱状图
bars = [(0.245, 0.545, 0.375, 0.775), (0.435, 0.415, 0.565, 0.775), (0.625, 0.285, 0.755, 0.775)]
for x0, y0, x1, y1 in bars:
    d.rounded_rectangle(
        [S * x0, S * y0, S * x1, S * y1],
        radius=int(S * 0.028),
        fill=(255, 255, 255, 246),
    )
# 底部基线
d.rounded_rectangle([S * 0.245, S * 0.822, S * 0.755, S * 0.862], radius=int(S * 0.02), fill=(255, 255, 255, 120))
# 右侧强调点
d.ellipse([S * 0.655, S * 0.215, S * 0.775, S * 0.335], fill=(34, 211, 238, 255))

img.save(PNG, "PNG")
sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(OUT, format="ICO", sizes=sizes)
print("icon written:", OUT, os.path.getsize(OUT), "bytes")
