"""生成 8cava 图标：圆形雷达极坐标底 + 白色折线 + 离散白块。

用法: python scripts/make_icon.py   （输出 icon.ico + icon.png 到 8cava/ 根目录）
"""
import math
import os
import random

from PIL import Image, ImageDraw

SIZE = 256
CX = CY = 128
R = 112                                   # 雷达屏半径

RADAR_BG = (70, 58, 92, 255)              # 略偏白的紫 #463A5C
GRID = (155, 132, 214, 130)               # 淡紫 #9B84D6（半透明）
CROSS = (155, 132, 214, 210)              # 十字线稍亮
LINE = (255, 255, 255, 255)               # 折线白
GLOW = (155, 132, 214, 170)               # 折线光晕淡紫
BLOCK = (255, 255, 255, 255)              # 离散白块

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 1. 圆形雷达屏底
d.ellipse([CX - R, CY - R, CX + R, CY + R], fill=RADAR_BG)

# 2. 极坐标网格：同心圆 + 放射线 + 十字
for r in (28, 56, 84, 112):
    d.ellipse([CX - r, CY - r, CX + r, CY + r], outline=GRID, width=1)
for ang in range(0, 360, 45):
    x = CX + R * math.cos(math.radians(ang))
    y = CY + R * math.sin(math.radians(ang))
    d.line([CX, CY, x, y], fill=GRID, width=1)
d.line([CX - R, CY, CX + R, CY], fill=CROSS, width=1)
d.line([CX, CY - R, CX, CY + R], fill=CROSS, width=1)

# 3. 离散白块（大小不一，落在雷达屏内）
random.seed(7)
placed = 0
while placed < 18:
    x = random.uniform(CX - R + 18, CX + R - 18)
    y = random.uniform(CY - R + 18, CY + R - 18)
    if (x - CX) ** 2 + (y - CY) ** 2 > (R - 16) ** 2:
        continue
    s = random.choice([2, 3, 4, 5, 6])
    d.rectangle([x, y, x + s, y + s], fill=BLOCK)
    placed += 1

# 4. 折线波形（白，带淡紫光晕；不对称 + 更粗）
pts = []
for i in range(22):
    x = CX - R + 24 + i * ((2 * R - 48) / 21.0)
    t = i / 21.0
    y = (CY
         + 22 * math.sin(t * math.pi * 2.7 + 0.8)
         + 14 * math.sin(t * math.pi * 5.3 + 2.1)
         + 7 * math.sin(t * math.pi * 11.0 + 4.2))
    y = max(CY - R + 26, min(CY + R - 26, y))
    pts.append((x, y))
d.line(pts, fill=GLOW, width=20, joint="curve")
d.line(pts, fill=LINE, width=14, joint="curve")

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
img.save(os.path.join(root, "icon.png"))
img.save(os.path.join(root, "icon.ico"), format="ICO",
         sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("icon.ico + icon.png ->", root)
