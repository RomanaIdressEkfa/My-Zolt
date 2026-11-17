"""Zolt "Royal" logo: gold Z with a ruby-red lightning diagonal, inside a
luxury-watch style gold bezel on a deep crimson ground.
Run: python assets/make_avatar_royal.py  ->  assets/zolt_avatar_royal.png
"""
from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from make_avatar_premium import C, HERE, OUT, S, radial, solid, vgrad

GOLD = ((255, 240, 190), (232, 182, 82), (168, 108, 30))
RUBY = ((235, 60, 64), (190, 14, 34), (105, 4, 20))


def masks(scale: float):
    def P(x, y):
        return (C + x * scale, C + y * scale)

    bars = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(bars)
    r = 30 * scale
    d.rounded_rectangle([*P(-400, -400), *P(400, -272)], radius=r, fill=255)
    d.rounded_rectangle([*P(-400, 272), *P(400, 400)], radius=r, fill=255)
    bolt = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bolt).polygon(
        [P(140, -272), P(345, -272), P(85, -18), P(215, -18),
         P(-140, 272), P(-345, 272), P(-85, 18), P(-215, 18)], fill=255)
    return bars.filter(ImageFilter.GaussianBlur(1.2)), bolt.filter(ImageFilter.GaussianBlur(1.2))


def metal(colors, alpha, y0, y1):
    layer = vgrad(*colors, y0, y1)
    layer.putalpha(alpha)
    return layer


def bevel(img, mask, strength=0.75):
    hi = ImageChops.subtract(mask, ImageChops.offset(mask, 8, 8)).filter(ImageFilter.GaussianBlur(2.5))
    lo = ImageChops.subtract(mask, ImageChops.offset(mask, -8, -8)).filter(ImageFilter.GaussianBlur(2.5))
    img.alpha_composite(solid((255, 255, 255), hi.point(lambda v: int(v * strength))))
    img.alpha_composite(solid((0, 0, 0), lo.point(lambda v: int(v * 0.4))))


def bezel(img):
    ring = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(ring)
    for R, w in ((880, 10), (842, 3)):
        d.ellipse([C - R, C - R, C + R, C + R], outline=255, width=w)
    for i in range(60):  # watch-style ticks, longer every 5
        a = math.radians(i * 6 - 90)
        r1, r2 = (790, 836) if i % 5 == 0 else (812, 834)
        w = 9 if i % 5 == 0 else 4
        if i == 0:
            continue  # 12 o'clock gets a diamond instead
        d.line([(C + r1 * math.cos(a), C + r1 * math.sin(a)),
                (C + r2 * math.cos(a), C + r2 * math.sin(a))], fill=255, width=w)
    # 12 o'clock diamond marker
    d.polygon([(C, C - 868), (C + 26, C - 818), (C, C - 768), (C - 26, C - 818)], fill=255)
    img.alpha_composite(solid((240, 170, 60), ring.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.35))))
    img.alpha_composite(metal(GOLD, ring, C - 880, C + 880))


def render() -> Image.Image:
    img = radial((70, 10, 22), (8, 2, 5))
    # faint crimson halo behind the mark
    halo = Image.new("L", (S, S), 0)
    ImageDraw.Draw(halo).ellipse([C - 560, C - 560, C + 560, C + 560], fill=110)
    img.alpha_composite(solid((150, 20, 35), halo.filter(ImageFilter.GaussianBlur(160))))
    bezel(img)

    scale = 1.18
    bars, bolt = masks(scale)
    union = ImageChops.lighter(bars, bolt)

    # gold outline around the whole Z ties the red bolt to the gold bars
    outline = ImageChops.subtract(union.filter(ImageFilter.MaxFilter(29)), union)
    img.alpha_composite(solid((0, 0, 0), union.filter(ImageFilter.MaxFilter(41)).filter(ImageFilter.GaussianBlur(30)).point(lambda v: int(v * 0.55))))
    img.alpha_composite(metal(GOLD, outline.filter(ImageFilter.GaussianBlur(1)), C - 500, C + 500))
    bevel(img, outline, 0.6)

    # ruby bolt with inner glow
    img.alpha_composite(solid((255, 40, 50), bolt.filter(ImageFilter.GaussianBlur(70)).point(lambda v: int(v * 0.45))))
    img.alpha_composite(metal(RUBY, bolt, C - 330, C + 330))
    core = ImageChops.multiply(bolt.filter(ImageFilter.MinFilter(31)).filter(ImageFilter.GaussianBlur(18)), bolt)
    img.alpha_composite(solid((255, 150, 130), core.point(lambda v: int(v * 0.18))))
    bevel(img, bolt, 0.55)

    # gold bars, each with its own metallic gradient
    for y0, y1 in ((-400, -272), (272, 400)):
        band = Image.new("L", (S, S), 0)
        ImageDraw.Draw(band).rectangle([0, C + y0 * scale - 4, S, C + y1 * scale + 4], fill=255)
        part = ImageChops.multiply(bars, band)
        img.alpha_composite(metal(GOLD, part, C + y0 * scale, C + y1 * scale))
        bevel(img, part, 0.8)

    # diagonal sheen over the whole mark
    sheen = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sheen).polygon([(0, 0), (S, 0), (S, C - 300), (0, C + 100)], fill=34)
    img.alpha_composite(solid((255, 255, 255), ImageChops.multiply(sheen.filter(ImageFilter.GaussianBlur(50)),
                                                                   union.filter(ImageFilter.MaxFilter(29)))))
    return img.convert("RGB").resize((OUT, OUT), Image.LANCZOS)


def main() -> None:
    img = render()
    img.save(HERE / "zolt_avatar_royal.png", optimize=True)
    prev = Image.new("RGB", (900, 460), (245, 245, 248))
    mask = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 399, 399], fill=255)
    prev.paste(img.resize((400, 400), Image.LANCZOS), (30, 30), mask)
    dark = Image.new("RGB", (430, 460), (24, 25, 30))
    prev.paste(dark, (470, 0))
    prev.paste(img.resize((400, 400), Image.LANCZOS), (485, 30), mask)
    small = img.resize((64, 64), Image.LANCZOS)
    m64 = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(m64).ellipse([0, 0, 63, 63], fill=255)
    prev.paste(small, (400, 380), m64)
    prev.save(HERE / "zolt_avatar_royal_preview.png")
    print("saved zolt_avatar_royal.png + preview")


if __name__ == "__main__":
    main()
