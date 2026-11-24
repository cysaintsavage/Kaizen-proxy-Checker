"""
Scanner-style live banner renderer  |  Dev: @Sy_saint
Drop a .ttf font at fonts/font.ttf to use a custom font (optional).
"""
import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

W, H = 900, 540
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    os.path.join(HERE, "fonts", "font.ttf"),
    "DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "consolab.ttf", "consola.ttf", "Menlo.ttc", "arialbd.ttf", "Arial Bold.ttf",
]

GREEN = (0, 230, 118)
RED = (255, 69, 90)
ORANGE = (255, 159, 28)
BLUE = (64, 156, 255)
PURPLE = (178, 102, 255)
CYAN = (0, 229, 255)
WHITE = (240, 246, 255)
MUTED = (110, 128, 160)

# label, stat key, colour, share-base ("done" or "live")
CARDS = [
    ("LIVE", "live", GREEN, "done"),
    ("DEAD", "dead", RED, "done"),
    ("LEAKED", "leaked", ORANGE, "live"),
    ("PUBLIC", "public", BLUE, "live"),
    ("PRIVATE", "private", PURPLE, "live"),
    ("ELITE", "elite", CYAN, "live"),
]


@lru_cache(maxsize=32)
def font(size: int):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def _right(d, x_right, y, text, f, fill):
    d.text((x_right - d.textlength(text, font=f), y), text, font=f, fill=fill)


def render_banner(st: dict) -> bytes:
    total = max(int(st.get("total", 0)), 1)
    done = int(st.get("done", 0))
    frame = int(st.get("frame", 0))
    final = bool(st.get("final"))
    stopped = bool(st.get("stopped"))
    live = int(st.get("live", 0))
    dev = st.get("dev", "Sy_saint")

    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)

    # background gradient + grid
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(7 + int(9 * t), 11 + int(13 * t), 22 + int(28 * t), 255))
    for x in range(0, W, 36):
        d.line([(x, 0), (x, H)], fill=(16, 24, 42, 255))
    for y in range(0, H, 36):
        d.line([(0, y), (W, y)], fill=(16, 24, 42, 255))

    # scanning sweep (translucent overlay)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    if not final:
        sy = (frame * 83) % H
        for i in range(70):
            od.line([(0, sy - i), (W, sy - i)], fill=(0, 255, 150, int(60 * (1 - i / 70))))
        od.line([(0, sy), (W, sy)], fill=(0, 255, 170, 190))
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)

    # header
    d.text((30, 20), "PROXY SCANNER", font=font(38), fill=CYAN)
    d.text((32, 66), f"MODE {str(st.get('mode', 'auto')).upper()}   "
                     f"THREADS {st.get('threads', 0)}   TIMEOUT {st.get('timeout', 0)}s",
           font=font(16), fill=MUTED)

    # status badge
    if final:
        label, col = ("STOPPED", RED) if stopped else ("SCAN COMPLETE", GREEN)
    else:
        label, col = "SCANNING", (GREEN if frame % 2 == 0 else (0, 120, 70))
    d.rounded_rectangle((W - 270, 24, W - 30, 64), radius=10, fill=(13, 19, 36),
                        outline=tuple(int(c * 0.5) for c in col), width=2)
    d.ellipse((W - 254, 38, W - 240, 52), fill=col)
    d.text((W - 228, 33), label, font=font(20), fill=WHITE)

    # progress bar
    bx0, by0, bx1, by1 = 30, 98, 870, 126
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=8, fill=(13, 19, 36), outline=(40, 60, 95), width=2)
    fill_w = int((bx1 - bx0 - 6) * min(done / total, 1))
    for i in range(fill_w):
        k = i / max(fill_w, 1)
        c = (int(GREEN[0] + (CYAN[0] - GREEN[0]) * k), int(GREEN[1] + (CYAN[1] - GREEN[1]) * k),
             int(GREEN[2] + (CYAN[2] - GREEN[2]) * k))
        d.line([(bx0 + 3 + i, by0 + 3), (bx0 + 3 + i, by1 - 3)], fill=c)
    for i in range(1, 10):
        x = bx0 + 3 + int((bx1 - bx0 - 6) * i / 10)
        d.line([(x, by0 + 3), (x, by1 - 3)], fill=(13, 19, 36))
    pct = done / total * 100
    d.text((30, 134), f"{pct:5.1f}%   {done}/{total if st.get('total') else 0} CHECKED", font=font(18), fill=WHITE)
    cur = ">> scan finished" if final else f">> probing {st.get('current', '')}"
    _right(d, 870, 136, cur, font(16), GREEN)

    # stat cards
    for i, (name, key, colr, base) in enumerate(CARDS):
        cx = 30 + (i % 3) * 285
        cy = 168 + (i // 3) * 132
        d.rounded_rectangle((cx, cy, cx + 270, cy + 120), radius=14, fill=(13, 19, 36),
                            outline=tuple(int(c * 0.45) for c in colr), width=2)
        d.rounded_rectangle((cx, cy + 14, cx + 6, cy + 106), radius=3, fill=colr)
        d.text((cx + 24, cy + 14), name, font=font(20), fill=colr)
        d.text((cx + 24, cy + 44), str(int(st.get(key, 0))), font=font(54), fill=WHITE)
        denom = done if base == "done" else live
        share = (int(st.get(key, 0)) / denom * 100) if denom else 0
        _right(d, cx + 254, cy + 16, f"{share:.0f}%", font(18), MUTED)

    # bottom stats
    rate = (live / done * 100) if done else 0
    items = [("AVG PING", f"{int(st.get('avg', 0))} ms"), ("SPEED", f"{st.get('speed', 0):.1f}/s"),
             ("ELAPSED", fmt_time(st.get("elapsed", 0))), ("LIVE RATE", f"{rate:.1f}%")]
    for i, (lab, val) in enumerate(items):
        x = 34 + i * 210
        d.text((x, 438), lab, font=font(14), fill=MUTED)
        d.text((x, 458), val, font=font(24), fill=WHITE)

    # footer
    d.line([(30, 500), (870, 500)], fill=(40, 60, 95), width=1)
    d.text((30, 508), f"DEV: @{dev}", font=font(18), fill=CYAN)
    _right(d, 870, 510, "Advanced Proxy Checker", font(16), MUTED)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()
