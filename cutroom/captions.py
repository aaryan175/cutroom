"""Caption rendering: one small RGBA PNG per cue, burned in by the segment renderer.

House rules (from real review rounds, keep them):
  * clean white text, NO outline / stroke / keyline; at most a very soft, wide, low-opacity shadow
  * one style everywhere (font, size, position), max 2 lines, short cues (asr.cues splits them)
  * a cue that would sit on a face moves to the top for that cue only

No fonts ship with cutroom. `find_font()` looks for a free/system sans in the usual places
(Inter, Helvetica Neue, Arial, DejaVu Sans); set style.captions.font or config caption_font to choose.
"""
import hashlib
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIRS = [
    "~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
    "~/.local/share/fonts", "~/.fonts", "/usr/share/fonts", "/usr/local/share/fonts",
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"),
]
# (file-name pattern, preferred face styles inside a collection / variable font)
FONT_PREFS = [
    (r"^Inter(-|_)?(SemiBold|Medium|Bold)?\.(ttf|otf)$", ("SemiBold", "Medium", "Bold")),
    (r"^Inter.*\.(ttf|otf)$", ("SemiBold", "Medium", "Bold")),
    (r"^HelveticaNeue\.ttc$", ("Medium", "Bold")),
    (r"^Helvetica\.ttc$", ("Bold",)),
    (r"^Arial( Bold)?\.ttf$|^arialbd\.ttf$", ("Bold",)),
    (r"^DejaVuSans-Bold\.ttf$", ("Bold",)),
    (r"^DejaVuSans\.ttf$", ("Book",)),
    (r"^LiberationSans-Bold\.ttf$", ("Bold",)),
]
POSITIONS = {"bottom": 0.885, "lower": 0.78, "top": 0.105}      # baseline-ish centre of the text block (fraction of H)

_FONT_CACHE = {}


def _walk_fonts():
    seen = set()
    for d in FONT_DIRS:
        p = Path(os.path.expanduser(d))
        if not p.is_dir():
            continue
        for dp, _, fs in os.walk(p):
            for f in fs:
                if f.lower().endswith((".ttf", ".otf", ".ttc")) and f not in seen:
                    seen.add(f)
                    yield f, os.path.join(dp, f)


def find_font(preferred: str = "") -> tuple:
    """(path, face_index) of the caption/graphics font. `preferred` = explicit path (index 0)."""
    if preferred:
        p = os.path.expanduser(preferred)
        if not os.path.exists(p):
            raise FileNotFoundError(f"caption font not found: {preferred}")
        return p, _best_face(p, ("SemiBold", "Medium", "Bold"))
    if "auto" in _FONT_CACHE:
        return _FONT_CACHE["auto"]
    files = list(_walk_fonts())
    for pat, styles in FONT_PREFS:
        rx = re.compile(pat, re.I)
        for name, path in files:
            if rx.search(name):
                _FONT_CACHE["auto"] = (path, _best_face(path, styles))
                return _FONT_CACHE["auto"]
    _FONT_CACHE["auto"] = ("", 0)       # Pillow's built-in bitmap font as a last resort
    return _FONT_CACHE["auto"]


def _best_face(path, styles) -> int:
    if not path.lower().endswith(".ttc"):
        return 0
    found = {}
    for i in range(32):
        try:
            f = ImageFont.truetype(path, 20, index=i)
        except OSError:
            break
        found[f.getname()[1]] = i
    for s in styles:
        if s in found:
            return found[s]
    return 0


def load_font(path_index: tuple, size: int, weight: str = "SemiBold"):
    path, idx = path_index
    if not path:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()
    key = (path, idx, size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = ImageFont.truetype(path, size, index=idx)
    try:                                           # variable fonts (e.g. Inter[opsz,wght])
        names = [n.decode() if isinstance(n, bytes) else n for n in f.get_variation_names()]
        if weight in names:
            f.set_variation_by_name(weight)
        elif "Medium" in names:
            f.set_variation_by_name("Medium")
    except (OSError, AttributeError):
        pass
    _FONT_CACHE[key] = f
    return f


def font_hash(path_index: tuple) -> str:
    path, idx = path_index
    if not path:
        return "builtin"
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{idx}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def apply_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "sentence" and text:
        return text[0].upper() + text[1:]
    return text


def wrap(text: str, font, max_w: int, max_lines: int = 2) -> list:
    """Greedy word wrap into <= max_lines; balances two lines; the last line is ellipsized if needed."""
    words = text.split()
    if not words:
        return []
    w = lambda s: font.getlength(s)          # noqa: E731
    if w(text) <= max_w:
        return [text]
    if max_lines >= 2:                        # best balanced split
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            m = max(w(a), w(b))
            if m <= max_w and (best is None or m < best[0]):
                best = (m, [a, b])
        if best:
            return best[1]
    lines, cur = [], ""
    for word in words:
        nxt = f"{cur} {word}".strip()
        if w(nxt) <= max_w or not cur:
            cur = nxt
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and w(lines[-1] + "…") > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def render_cue(text, out_png, W, H, font_pi, size, case="sentence"):
    """Draw one caption into a tight RGBA PNG (text + soft shadow). Returns (path, w, h)."""
    scale = H / 1080.0
    px = max(10, int(round(size * scale)))
    font = load_font(font_pi, px)
    lines = wrap(apply_case(text, case), font, int(W * 0.78), 2)
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * 1.12)
    tw = int(max(font.getlength(l) for l in lines)) if lines else 1
    pad = int(px * 0.9)                                   # room for the shadow blur
    cw, ch = tw + 2 * pad, lh * max(1, len(lines)) + 2 * pad
    shadow = Image.new("L", (cw, ch), 0)
    sd = ImageDraw.Draw(shadow)
    text_l = Image.new("L", (cw, ch), 0)
    td = ImageDraw.Draw(text_l)
    for i, line in enumerate(lines):
        lw = font.getlength(line)
        x = (cw - lw) / 2
        y = pad + i * lh
        td.text((x, y), line, font=font, fill=255)
        sd.text((x, y + px * 0.06), line, font=font, fill=255)
    # very soft, wide, low-opacity shadow: no edge, no keyline - just lifts the text off bright skies
    shadow = shadow.filter(ImageFilter.GaussianBlur(px * 0.32)).point(lambda v: int(v * 0.38))
    base = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    base.putalpha(shadow)                                  # black, soft
    txt = Image.new("RGBA", (cw, ch), (255, 255, 255, 0))
    txt.putalpha(text_l)                                   # white, anti-aliased, no stroke
    img = Image.alpha_composite(base, txt)
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png, optimize=False, compress_level=1)
    return str(out_png), cw, ch


def cue_png(cache_dir, text, W, H, font_pi, size, case):
    """Cached caption PNG named by a hash of everything that changes its pixels."""
    key = hashlib.sha1(f"{text}|{W}|{H}|{font_hash(font_pi)}|{size}|{case}|{CAPTION_STYLE_VERSION}".encode()).hexdigest()[:16]
    out = Path(cache_dir, f"cap_{key}.png")
    if out.exists():
        with Image.open(out) as im:
            return str(out), im.width, im.height
    return render_cue(text, out, W, H, font_pi, size, case)


def place(position, box_w, box_h, W, H):
    """Top-left (x, y) for a caption box at a named position."""
    cy = POSITIONS.get(position, POSITIONS["bottom"]) * H
    x = int(round((W - box_w) / 2))
    y = int(round(cy - box_h / 2))
    return x, max(0, min(H - box_h, y))


def overlaps_face(x, y, w, h, faces, shrink=0.25):
    """True if the caption box covers any face box (output pixels). The caption box is shrunk a bit
    because its PNG includes transparent shadow padding."""
    sx, sy = w * shrink / 2, h * shrink / 2
    x0, y0, x1, y1 = x + sx, y + sy, x + w - sx, y + h - sy
    for fx, fy, fw, fh in faces:
        if fx < x1 and fx + fw > x0 and fy < y1 and fy + fh > y0:
            return True
    return False


CAPTION_STYLE_VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10] if os.path.exists(__file__) else "0"

if __name__ == "__main__":         # quick look: python -m cutroom.captions "some text" out.png
    pi = find_font()
    print(pi)
    print(render_cue(sys.argv[1] if len(sys.argv) > 1 else "this is a caption test", sys.argv[2] if len(sys.argv) > 2 else "cap.png",
                     1920, 1080, pi, 32))
