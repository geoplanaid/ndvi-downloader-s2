"""Regenerate ndvi_downloader/icon.png (needs Pillow)."""
import os

from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=96, fill=(46, 125, 50, 255))
# 3x2 grid of monthly NDVI tiles
greens = [(27, 94, 32), (102, 187, 106), (200, 230, 201), (165, 214, 167), (56, 142, 60), (249, 168, 37)]
for i, c in enumerate(greens):
    x0 = 80 + (i % 3) * 120
    y0 = 80 + (i // 3) * 120
    d.rounded_rectangle((x0, y0, x0 + 100, y0 + 100), radius=18, fill=c + (255,))
# download arrow
d.rectangle((231, 320, 281, 400), fill=(255, 255, 255, 255))
d.polygon([(186, 395), (326, 395), (256, 460)], fill=(255, 255, 255, 255))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ndvi_downloader", "icon.png")
img.resize((128, 128), Image.LANCZOS).save(out)
print("saved", os.path.abspath(out))
