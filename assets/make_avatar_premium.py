"""Premium Zolt logo: a geometric "Z" whose diagonal is a lightning bolt.
Renders three colour variants + a preview sheet into assets/.
Run: python assets/make_avatar_premium.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S, OUT = 2048, 1024
C = S // 2
HERE = Path(__file__).resolve().parent

VARIANTS = {
    # name: (bg inner, bg outer, mark top, mark mid, mark bottom, ring, glow)
    "gold": ((26, 32, 58), (6, 8, 18), (255, 236, 176), (244, 186, 72), (196, 128, 34), (232, 190, 110), (245, 180, 60)),
    "laravel": ((40, 16, 18), (8, 5, 7), (255, 170, 120), (255, 76, 52), (204, 34, 28), (255, 110, 80), (255, 70, 45)),
    "indigo": ((250, 250, 255), (226, 230, 245), (120, 120, 255), (82, 70, 230), (52, 36, 160), (110, 100, 230), (110, 90, 255)),
}


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def radial(inner, outer) -> Image.Image:
    n = 512
    img = Image.new("RGB", (n, n))
    px, c = img.load(), n / 2
    for y in range(n):
        for x in range(n):
            t = min(1.0, math.hypot(x - c, y - c * 0.8) / (n * 0.72))
            px[x, y] = lerp(inner, outer, t ** 1.3)
    return img.resize((S, S), Image.BICUBIC).convert("RGBA")


def vgrad(top, mid, bottom, y0, y1) -> Image.Image:
    col = Image.new("RGB", (1, S))
    for y in range(S):
        t = min(1.0, max(0.0, (y - y0) / (y1 - y0)))
        col.putpixel((0, y), lerp(top, mid, t * 2) if t < 0.5 else lerp(mid, bottom, (t - 0.5) * 2))
    return col.resize((S, S)).convert("RGBA")


def z_mask(scale: float = 1.0) -> Image.Image:
    def P(x, y):
        return (C + x * scale, C + y * scale)

    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    r = 34 * scale
    d.rounded_rectangle([*P(-400, -400), *P(400, -272)], radius=r, fill=255)   # top bar
    d.rounded_rectangle([*P(-400, 272), *P(400, 400)], radius=r, fill=255)     # bottom bar
    d.polygon([P(140, -290), P(345, -290), P(85, -18), P(215, -18),            # bolt diagonal
               P(-140, 290), P(-345, 290), P(-85, 18), P(-215, 18)], fill=255)
    return m.filter(ImageFilter.GaussianBlur(1.5))


def solid(color, alpha: Image.Image) -> Image.Image:
    layer = Image.new("RGBA", (S, S), color + (0,))
    layer.putalpha(alpha)
    return layer


def render(name: str) -> Image.Image:
    bg_in, bg_out, top, mid, bot, ring_c, glow_c = VARIANTS[name]
    light = name == "indigo"
    img = radial(bg_in, bg_out)

    # fine outer ring, brighter at the top (like light catching metal)
    ring = Image.new("L", (S, S), 0)
    R = 820
    ImageDraw.Draw(ring).ellipse([C - R, C - R, C + R, C + R], outline=255, width=6)
    fade = Image.linear_gradient("L").resize((S, S)).point(lambda v: int(255 - v * 0.8))
    img.alpha_composite(solid(ring_c, ImageChops.multiply(ring, fade)))

    mark = z_mask(1.22)
    # soft shadow / glow under the mark
    if light:
        shadow = mark.filter(ImageFilter.GaussianBlur(40)).point(lambda v: int(v * 0.35))
        img.alpha_composite(solid((40, 30, 120), ImageChops.offset(shadow, 0, 30)))
    else:
        img.alpha_composite(solid(glow_c, mark.filter(ImageFilter.GaussianBlur(90)).point(lambda v: int(v * 0.55))))

    # metallic gradient fill
    fill = vgrad(top, mid, bot, C - 490, C + 490)
    fill.putalpha(mark)
    img.alpha_composite(fill)

    # bevel: bright top-left edge, darker bottom-right edge
    hi = ImageChops.subtract(mark, ImageChops.offset(mark, 10, 10)).filter(ImageFilter.GaussianBlur(3))
    lo = ImageChops.subtract(mark, ImageChops.offset(mark, -10, -10)).filter(ImageFilter.GaussianBlur(3))
    img.alpha_composite(solid((255, 255, 255), hi.point(lambda v: int(v * 0.7))))
    img.alpha_composite(solid((0, 0, 0), lo.point(lambda v: int(v * 0.35))))

    # diagonal sheen across the upper half of the mark
    sheen = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sheen).polygon([(0, 0), (S, 0), (S, C - 260), (0, C + 120)], fill=60)
    img.alpha_composite(solid((255, 255, 255), ImageChops.multiply(sheen.filter(ImageFilter.GaussianBlur(60)), mark)))

    return img.convert("RGB").resize((OUT, OUT), Image.LANCZOS)


def main() -> None:
    images = {}
    for name in VARIANTS:
        img = render(name)
        img.save(HERE / f"zolt_avatar_{name}.png", optimize=True)
        images[name] = img
    # preview: each variant as Telegram shows it (circle crop)
    sheet = Image.new("RGB", (3 * 420 + 40, 460), (245, 245, 248))
    mask = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 399, 399], fill=255)
    for i, img in enumerate(images.values()):
        sheet.paste(img.resize((400, 400), Image.LANCZOS), (20 + i * 420, 30), mask)
    sheet.save(HERE / "zolt_avatar_preview.png")
    print("saved", ", ".join(f"zolt_avatar_{n}.png" for n in images), "+ preview")


if __name__ == "__main__":
    main()
