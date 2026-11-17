"""Draws Zolt's profile picture: a lightning bolt (Zolt = jolt, instant work)
inside code brackets (a developer's agent), with automation nodes orbiting it.
Run: python assets/make_avatar.py  ->  assets/zolt_avatar.png
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 2048          # draw large, downscale for smooth edges
OUT = 1024
C = S // 2
HERE = Path(__file__).resolve().parent


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def radial_bg(inner, outer) -> Image.Image:
    n = 512  # smooth gradient, so compute small and scale up
    img = Image.new("RGB", (n, n))
    px = img.load()
    c = n / 2
    maxd = math.hypot(c, c)
    for y in range(n):
        for x in range(n):
            t = min(1.0, math.hypot(x - c, y - c * 0.85) / maxd * 1.25)
            px[x, y] = lerp(inner, outer, t ** 1.4)
    return img.resize((S, S), Image.BICUBIC)


def linear_fill(size, top, bottom) -> Image.Image:
    grad = Image.new("RGB", (1, 256))
    for y in range(256):
        grad.putpixel((0, y), lerp(top, bottom, y / 255))
    return grad.resize(size)


def glow(mask: Image.Image, color, radius, strength=1.0) -> Image.Image:
    blurred = mask.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        blurred = blurred.point(lambda v: min(255, int(v * strength)))
    layer = Image.new("RGBA", (S, S), color + (0,))
    layer.putalpha(blurred)
    return layer


def main() -> None:
    # Background: deep night blue -> near black (an "always awake" PC at night)
    base = radial_bg((40, 34, 92), (7, 9, 24)).convert("RGBA")

    # --- automation ring: thin orbit with nodes (n8n-style workflow) ---
    ring = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(ring)
    r = 760
    d.ellipse([C - r, C - r, C + r, C + r], outline=255, width=10)
    nodes = []
    for i, deg in enumerate((-60, 10, 120, 200, 250)):
        a = math.radians(deg)
        nx, ny = C + r * math.cos(a), C + r * math.sin(a)
        nr = 44 if i % 2 == 0 else 30
        nodes.append((nx, ny, nr))
        d.ellipse([nx - nr, ny - nr, nx + nr, ny + nr], fill=255)
    base.alpha_composite(glow(ring, (0, 220, 255), 30, 0.8))
    ring_color = Image.new("RGBA", (S, S), (90, 230, 255, 0))
    ring_color.putalpha(ring.point(lambda v: int(v * 0.75)))
    base.alpha_composite(ring_color)

    # --- code brackets  <  /> ---
    br = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(br)
    w = 70
    h = 330
    lx = C - 470
    d.line([(lx + 170, C - h), (lx, C), (lx + 170, C + h)], fill=255, width=w, joint="curve")
    rx = C + 470
    d.line([(rx - 170, C - h), (rx, C), (rx - 170, C + h)], fill=255, width=w, joint="curve")
    for (x, y) in [(lx + 170, C - h), (lx, C), (lx + 170, C + h),
                   (rx - 170, C - h), (rx, C), (rx - 170, C + h)]:
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=255)
    base.alpha_composite(glow(br, (255, 60, 50), 40, 0.9))
    br_fill = linear_fill((S, S), (255, 120, 80), (255, 45, 32)).convert("RGBA")  # Laravel red
    br_fill.putalpha(br)
    base.alpha_composite(br_fill)

    # --- lightning bolt in the middle ---
    bolt_pts = [
        (C + 90, C - 560), (C - 230, C + 60), (C - 20, C + 60),
        (C - 110, C + 560), (C + 240, C - 90), (C + 30, C - 90),
        (C + 150, C - 560),
    ]
    bolt = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bolt).polygon(bolt_pts, fill=255)
    bolt = bolt.filter(ImageFilter.GaussianBlur(2))
    base.alpha_composite(glow(bolt, (255, 200, 40), 110, 1.3))
    base.alpha_composite(glow(bolt, (255, 240, 150), 35, 1.2))
    bolt_fill = linear_fill((S, S), (255, 251, 210), (255, 176, 0)).convert("RGBA")
    bolt_fill.putalpha(bolt)
    base.alpha_composite(bolt_fill)

    # bright highlight on the bolt's left edge
    hi = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hi).polygon([(C + 90, C - 560), (C - 230, C + 60), (C - 160, C + 60),
                                (C + 120, C - 520)], fill=170)
    hi = ImageChops.multiply(hi.filter(ImageFilter.GaussianBlur(6)), bolt)
    white = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    white.putalpha(hi)
    base.alpha_composite(white)

    # small sparks
    sp = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(sp)
    for (x, y, s) in [(C - 330, C - 380, 22), (C + 330, C + 360, 18), (C + 300, C - 420, 14),
                      (C - 300, C + 420, 14)]:
        d.polygon([(x, y - s * 3), (x + s, y), (x, y + s * 3), (x - s, y)], fill=255)
        d.polygon([(x - s * 3, y), (x, y - s), (x + s * 3, y), (x, y + s)], fill=255)
    base.alpha_composite(glow(sp, (255, 230, 120), 14, 1.2))
    spc = Image.new("RGBA", (S, S), (255, 245, 200, 0))
    spc.putalpha(sp)
    base.alpha_composite(spc)

    out = base.convert("RGB").resize((OUT, OUT), Image.LANCZOS)
    out.save(HERE / "zolt_avatar.png", optimize=True)
    print("saved", HERE / "zolt_avatar.png")


if __name__ == "__main__":
    main()
