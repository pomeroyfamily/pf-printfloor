"""Draw the app icons (home screen / install) into app/static/icons. Run: python tools/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG, TRACK, RING = (20, 25, 31), (52, 61, 72), (29, 111, 209)


def icon(size: int, ring_frac: float) -> Image.Image:
    s = size * 4                                   # draw large, scale down for smooth edges
    im = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(im)
    r = s * ring_frac / 2
    w = int(r * 0.36)
    box = (s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r)
    d.ellipse(box, outline=TRACK, width=w)
    d.arc(box, start=-90, end=-90 + 0.62 * 360, fill=RING, width=w)
    for ang in (-90, -90 + 0.62 * 360):           # round caps
        import math
        cx = s / 2 + (r - w / 2) * math.cos(math.radians(ang))
        cy = s / 2 + (r - w / 2) * math.sin(math.radians(ang))
        d.ellipse((cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2), fill=RING)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (180, 192, 512):
        icon(size, 0.62).save(OUT / f"icon-{size}.png", optimize=True)
    icon(512, 0.5).save(OUT / "icon-maskable-512.png", optimize=True)   # inside the 80% safe zone
    print("wrote", sorted(p.name for p in OUT.iterdir()))
