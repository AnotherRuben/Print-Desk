"""Profile pictures: turning a colour image into the black dots a thermal
printer can print, the ESC/POS command for it, and stand-in avatars for tests."""
from __future__ import annotations

import hashlib

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

SIZES = {"small": 160, "medium": 256, "large": 352}     # dots; 8 dots = 1 mm on 80mm paper


def _black_share(bw: Image.Image) -> float:
    return 1 - ImageStat.Stat(bw.convert("L")).mean[0] / 255


STYLES = {
    "photo": "Photo - smooth tones, best for faces",
    "crisp": "Crisp - cleaner light areas, a bit more contrast",
    "halftone": "Halftone - newspaper-style dot pattern",
    "classic": "Classic - the first version (bright, high contrast)",
}


def _fit_grey(img: Image.Image, width: int) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGBA")
    bg = Image.new("RGBA", img.size, "white")             # transparent parts become paper
    img = Image.alpha_composite(bg, img).convert("L")
    return ImageOps.fit(img, (width, width), Image.LANCZOS)


def _grey(img: Image.Image, width: int) -> Image.Image:
    """The first version: brightened and contrasty (style "classic")."""
    img = ImageOps.autocontrast(_fit_grey(img, width), cutoff=1)
    img = ImageEnhance.Brightness(img).enhance(1.12)      # thermal dots spread; lighten a little
    return ImageEnhance.Contrast(img).enhance(1.15)


def _prep(img: Image.Image, width: int, lift: float = 0.8, lighten_dark: bool = True) -> Image.Image:
    """Gentle preparation for photos. Thermal dots spread, so mid-tones print darker
    than they should: those are lifted, while light areas (faces) keep some tone
    instead of turning into bare paper, and a little sharpening keeps eyes and mouths."""
    g = _fit_grey(img, width)
    g = ImageOps.autocontrast(g, cutoff=(0.5, 0.2))
    if lighten_dark and ImageStat.Stat(g).mean[0] < 95:  # a really dark picture: lift a bit more
        lift -= 0.12
    g = g.filter(ImageFilter.UnsharpMask(radius=3, percent=110, threshold=2))
    lut = []
    for v in range(256):
        x = (v / 255) ** lift
        if x > 0.9:
            x = 0.9 + (x - 0.9) * 0.6                     # highlights: a light dot here and there
        lut.append(int(255 * min(1.0, x)))
    return g.point(lut)


def _atkinson(grey: Image.Image) -> Image.Image:
    """Atkinson dithering: spreads only 3/4 of the error, so light areas stay clean."""
    return _atkinson_plain(grey)


def _atkinson_plain(grey: Image.Image) -> Image.Image:
    """Plain Python, no numpy needed (and faster for avatar-sized pictures)."""
    w, h = grey.convert("L").size
    px = [float(v) for v in grey.convert("L").tobytes()]
    out = bytearray(w * h)
    for y in range(h):
        base = y * w
        for x in range(w):
            i = base + x
            old = px[i]
            new = 255.0 if old >= 128 else 0.0
            out[i] = 255 if new else 0
            err = (old - new) / 8
            if not err:
                continue
            if x + 1 < w: px[i + 1] += err
            if x + 2 < w: px[i + 2] += err
            if y + 1 < h:
                j = i + w
                if x > 0: px[j - 1] += err
                px[j] += err
                if x + 1 < w: px[j + 1] += err
            if y + 2 < h:
                px[i + 2 * w] += err
    return Image.frombytes("L", (w, h), bytes(out)).convert("1", dither=Image.NONE)


def _halftone(grey: Image.Image, n: int = 8) -> Image.Image:
    """Ordered (Bayer) dithering: a regular dot pattern."""
    def matrix(k):
        if k == 1:
            return [[0]]
        m = matrix(k // 2)
        size = len(m)
        out = [[0] * (2 * size) for _ in range(2 * size)]
        for y in range(size):
            for x in range(size):
                v = 4 * m[y][x]
                out[y][x], out[y][x + size] = v, v + 2
                out[y + size][x], out[y + size][x + size] = v + 3, v + 1
        return out
    m = matrix(n)
    px = grey.load()
    bw = Image.new("1", grey.size, 1)
    out = bw.load()
    for y in range(grey.height):
        for x in range(grey.width):
            out[x, y] = 255 if px[x, y] > (m[y % n][x % n] + 0.5) / (n * n) * 255 else 0
    return bw


def _circle(bw: Image.Image, round_crop: bool) -> Image.Image:
    if not round_crop:
        return bw
    w = bw.width
    mask = Image.new("L", bw.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w - 1, w - 1), fill=255)
    out = Image.composite(bw.convert("L"), Image.new("L", bw.size, 255), mask).convert("1", dither=Image.NONE)
    ImageDraw.Draw(out).ellipse((0, 0, w - 1, w - 1), outline=0, width=2)   # keeps light avatars' edge
    return out


def _dither(grey: Image.Image, round_crop: bool) -> Image.Image:
    return _circle(grey.convert("1", dither=Image.FLOYDSTEINBERG), round_crop)


def to_bitmap(img: Image.Image, width: int, round_crop: bool = True,
              lighten_dark: bool = True, max_black: float = 0.55, style: str = "photo") -> Image.Image:
    """Colour picture -> 1-bit dithered picture `width` dots wide, in one of STYLES."""
    if style == "crisp":
        return _circle(_atkinson(_prep(img, width, 0.75, lighten_dark)), round_crop)
    if style == "halftone":
        return _circle(_halftone(_prep(img, width, 0.75, lighten_dark)), round_crop)
    if style == "photo":
        return _dither(_prep(img, width, 0.8, lighten_dark), round_crop)
    # "classic": the first version. With lighten_dark, pictures with a lot of black are
    # brightened step by step until no more than max_black of the circle is black.
    grey = _grey(img, width)
    bw = _dither(grey, round_crop)
    area = 0.785 if round_crop else 1.0                   # a circle covers pi/4 of the square
    if lighten_dark:
        for gamma, floor in ((0.8, 0.12), (0.65, 0.22), (0.5, 0.32), (0.4, 0.42)):
            if _black_share(bw) / area <= max_black:
                break
            lifted = grey.point(lambda v, g=gamma, f=floor: int(255 * (f + (1 - f) * (v / 255) ** g)))
            bw = _dither(lifted, round_crop)
    return bw


MAX_BAND_BYTES = 768                                      # one picture piece stays well under 1 KB


def escpos_raster(bw: Image.Image, band: int | None = None) -> bytes:
    """GS v 0 commands for a 1-bit image (black = printed). Sent in small bands:
    cheap printers with a small receive buffer can drop part of a big one and then
    sit waiting for picture data that never comes."""
    w, h = bw.size
    row_bytes = (w + 7) // 8
    band = band or max(8, MAX_BAND_BYTES // row_bytes)
    # invert: in mode "1" black is 0, the printer wants 1 for a dot
    packed = ImageOps.invert(bw.convert("L")).convert("1").tobytes()
    out = bytearray()
    for top in range(0, h, band):
        rows = min(band, h - top)
        out += b"\x1dv0\x00" + bytes([row_bytes & 0xFF, row_bytes >> 8, rows & 0xFF, rows >> 8])
        out += packed[top * row_bytes:(top + rows) * row_bytes]
    return bytes(out)


def demo_avatar(name: str, size: int = 300) -> Image.Image:
    """A stand-in avatar (coloured circle with an initial) for test mode."""
    h = hashlib.md5(name.lower().encode()).digest()
    bg = (60 + h[0] % 160, 60 + h[1] % 160, 60 + h[2] % 160)
    im = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(im)
    d.ellipse((size * .12, size * .12, size * .88, size * .88), fill=(250, 248, 240))
    letter = (name.strip("_ ") or "?")[0].upper()
    try:
        font = ImageFont.load_default(size=int(size * .45))
    except TypeError:                                     # very old Pillow
        font = ImageFont.load_default()
    d.text((size / 2, size / 2), letter, font=font, fill=(25, 25, 30), anchor="mm")
    return im


# ------------------------------------------------------------------ messages with emotes

FONT_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",                       # Consolas, on every Windows PC
    "C:/Windows/Fonts/cour.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "DejaVuSansMono.ttf",
]
_font_cache = {}


def mono_font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _emote_bw(img: Image.Image, h: int) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGBA")
    w = max(1, round(img.width * h / img.height))
    img = img.resize((w, h), Image.LANCZOS)
    bg = Image.new("RGBA", img.size, "white")
    grey = Image.alpha_composite(bg, img).convert("L")
    grey = ImageOps.autocontrast(grey, cutoff=1)
    return grey.convert("1", dither=Image.FLOYDSTEINBERG)


def render_rich(parts: list[dict], width: int, font_size: int = 22, emote_h: int = 40,
                quote: bool = True) -> Image.Image:
    """A viewer message with emotes as one 1-bit picture `width` dots wide.
    Text is drawn sharp (no dithering), emotes are dithered like the avatars."""
    font = mono_font(font_size)
    space = font.getlength(" ")
    tokens = []                                           # ("word", str) | ("emote", bw image)
    if quote:
        tokens.append(("word", '"'))
    for p in parts:
        if p["type"] == "emote" and p.get("image") is not None:
            tokens.append(("emote", _emote_bw(p["image"], emote_h)))
        else:
            text = p.get("text") if p["type"] == "text" else p.get("name", "")
            for i, word in enumerate((text or "").split(" ")):
                if i:
                    tokens.append(("space", None))
                if word:
                    tokens.append(("word", word))
    if quote:
        tokens.append(("word", '"'))

    def size(t):
        kind, v = t
        if kind == "space":
            return space
        if kind == "emote":
            return v.width
        return font.getlength(v)

    lines, cur, x = [], [], 0.0
    for t in tokens:
        w = size(t)
        if t[0] == "space":
            if cur:
                cur.append(t)
                x += w
            continue
        if x + w > width and cur:
            while cur and cur[-1][0] == "space":
                cur.pop()
            lines.append(cur)
            cur, x = [], 0.0
        if t[0] == "word" and w > width:                  # a single very long word: cut it
            v = t[1]
            while font.getlength(v) > width and len(v) > 1:
                v = v[:-1]
            t, w = ("word", v), font.getlength(v)
        cur.append(t)
        x += w
    if cur:
        lines.append(cur)

    line_h = max(emote_h, font_size + 6) + 4
    img = Image.new("1", (width, max(1, line_h * len(lines))), 1)
    d = ImageDraw.Draw(img)
    for row, line in enumerate(lines):
        y0, x = row * line_h, 0.0
        for kind, v in line:
            if kind == "space":
                x += space
            elif kind == "emote":
                img.paste(v, (int(x), y0 + (line_h - v.height) // 2))
                x += v.width
            else:
                d.text((x, y0 + line_h // 2), v, font=font, fill=0, anchor="lm")
                x += font.getlength(v)
    return img


def demo_emote(name: str, size: int = 56) -> Image.Image:
    """Stand-in emote for test mode: a small coloured face."""
    h = hashlib.md5(name.encode()).digest()
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((2, 2, size - 3, size - 3), fill=(60 + h[0] % 150, 60 + h[1] % 150, 60 + h[2] % 150, 255),
              outline=(20, 20, 20, 255), width=3)
    e = size // 5
    d.ellipse((size * .3 - e / 2, size * .38 - e / 2, size * .3 + e / 2, size * .38 + e / 2), fill=(20, 20, 20, 255))
    d.ellipse((size * .7 - e / 2, size * .38 - e / 2, size * .7 + e / 2, size * .38 + e / 2), fill=(20, 20, 20, 255))
    d.arc((size * .25, size * .4, size * .75, size * .8), 20, 160, fill=(20, 20, 20, 255), width=4)
    return im
