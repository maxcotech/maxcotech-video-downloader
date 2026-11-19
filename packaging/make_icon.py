"""Draws the app icon (blue rounded square, white download arrow) -> app.ico."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(sys.argv[1] if len(sys.argv) > 1 else "app.ico")
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# background: rounded square with a subtle vertical gradient
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
top, bottom = (45, 140, 255), (11, 92, 214)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
img.paste(grad, (0, 0), mask)

white = (255, 255, 255, 255)
# arrow shaft + head
d.rounded_rectangle([442, 210, 582, 560], radius=40, fill=white)
d.polygon([(292, 500), (732, 500), (512, 730)], fill=white)
# tray
d.rounded_rectangle([250, 780, 774, 860], radius=40, fill=white)

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(out, sizes=sizes)
print("wrote", out)
