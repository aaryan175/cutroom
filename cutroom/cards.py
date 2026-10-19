"""Procedural graphics measured from a creator's reference: film burns, frame mattes (window-box gate,
inset, letterbox), the "COMING UP" card and the typed title. Everything is generated here from numbers
(no bundled assets, no fonts shipped): the shapes, colours, sizes and timings below are the ones measured
frame by frame on the reference intro (forensic inventory, 25 fps, 1080p).

  burn_layer(W, H, side, ...)      one RGBA light frame of the two-frame film burn
      side "out" = last frame of the outgoing shot: a red/orange glow rising from the lower left
                   (centroid ~0.25, 0.7; 17-23 % of pixels saturated red; +25-30 luma)
      side "in"  = first frame of the incoming shot: a large orange-yellow disc entering from the right
                   (centroid ~0.8, 0.46; 29-42 % orange) with a magenta/blue fringe on its leading edge
                   and a blown-white core
  matte(W, H, frame)               RGBA border with a transparent rounded window (+ an optional inner matte)
  card_sequence(dir, ...)          the boiling near-black grunge card with the scaling yellow text
  typed_sequence(dir, ...)         a title typed on one character at a time (transparent PNGs)

All randomness is seeded, so a frame is identical on every render (cache keys stay stable).
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

CODE_VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10]

# measured on the reference (events.json)
GATE = {"w": 0.973, "h": 0.906, "x": 25 / 1920, "y": 45 / 1080, "radius": 9, "feather": 3, "color": "#0F0100"}
INSET = {"scale": 0.62, "radius": 5, "feather": 1, "bg": "#000000"}
CARD = {"text": "COMING UP", "color": "#FBFF09", "size": 0.142, "max_w": 0.70, "scale_from": 0.873,
        "scale_frames": 10, "boil": 2.5, "bg_luma": 7.0, "dur": 0.84}
TITLE = {"color": "#FFCD00", "size": 0.092, "char_s": 0.07}

# display faces in order of preference (file-name patterns, face styles); nothing is bundled
CARD_FONTS = [(r"^Cooper.*Black.*\.(ttf|otf)$", ("Black", "Regular")), (r"^Cooper.*\.(ttf|otf|ttc)$", ("Black", "Bold")),
              (r"^Recoleta.*Black.*\.(ttf|otf)$", ("Black",)), (r"^Fraunces.*\.(ttf|otf)$", ("Black", "Bold")),
              (r"^SuperClarendon\.ttc$", ("Bold", "Black")), (r"^Rockwell\.ttc$", ("Bold",)),
              (r"^Georgia Bold\.ttf$", ("Bold", "Regular")), (r"^DejaVuSerif-Bold\.ttf$", ("Bold",))]
TITLE_FONTS = [(r"^BebasNeue-Regular\.(ttf|otf)$", ("Regular",)), (r"^BebasNeue.*\.(ttf|otf)$", ("Regular",)),
               (r"^Anton-Regular\.ttf$", ("Regular",)), (r"^LeagueGothic.*\.ttf$", ("Regular",)),
               (r"^Oswald.*\.ttf$", ("Bold", "SemiBold")), (r"^Antonio.*\.ttf$", ("Bold",)),
               (r"^Impact\.ttf$", ("Regular",)), (r"^Futura\.ttc$", ("Condensed ExtraBold",)),
               (r"^Avenir Next Condensed\.ttc$", ("Bold", "Demi Bold")), (r"^DejaVuSans-Bold\.ttf$", ("Bold",))]
_FONTS = {}


def hex_rgb(s, default=(0, 0, 0)):
    s = str(s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return default
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def valid_color(s):
    return bool(re.fullmatch(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", str(s or "").strip()))


# ============================================================================== fonts
def _face_index(path, styles):
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


def find_font(kind="card", preferred=""):
    """(path, face index) of an installed display face. `preferred` = a path, or a file/family name
    fragment ("Cooper Black", "Bebas"). Falls back down the preference list, then to the caption font."""
    from . import captions
    key = (kind, preferred)
    if key in _FONTS:
        return _FONTS[key]
    files = list(captions._walk_fonts())
    if preferred:
        p = os.path.expanduser(preferred)
        if os.path.exists(p):
            _FONTS[key] = (p, _face_index(p, ("Black", "Bold", "Regular")))
            return _FONTS[key]
        frag = re.sub(r"[\s_-]+", "", preferred.lower())
        for name, path in files:
            if frag in re.sub(r"[\s_-]+", "", name.lower()):
                _FONTS[key] = (path, _face_index(path, ("Black", "Bold", "Regular")))
                return _FONTS[key]
    for pat, styles in (CARD_FONTS if kind == "card" else TITLE_FONTS):
        rx = re.compile(pat, re.I)
        for name, path in files:
            if rx.search(name):
                _FONTS[key] = (path, _face_index(path, styles))
                return _FONTS[key]
    _FONTS[key] = captions.find_font("")
    return _FONTS[key]


def _font(fp, size):
    path, idx = fp
    if not path:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()
    return ImageFont.truetype(path, size, index=idx)


def _font_id(fp):
    path, idx = fp
    if not path:
        return "builtin"
    st = os.stat(path)
    return f"{os.path.basename(path)}|{idx}|{st.st_size}"


def _draw_tracked(draw, xy, text, f, tracking):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=f, fill=255)
        x += f.getlength(ch) + tracking


def _tracked_len(text, f, tracking):
    return sum(f.getlength(c) for c in text) + tracking * max(0, len(text) - 1)


def text_layer(text, fp, cap_px, color, soft=0.0, max_w=None, tracking=0.0):
    """RGBA image of `text` whose capital height is `cap_px` (shrunk to fit max_w). soft > 0 rounds the
    corners (blur + re-threshold of the alpha), the soft-serif look of a heavy display face."""
    probe = _font(fp, 200)
    bb = probe.getbbox("H")
    cap200 = max(1, bb[3] - bb[1])
    size = max(8, int(round(200 * cap_px / cap200)))
    f = _font(fp, size)
    l, t, r, b = f.getbbox(text)
    if max_w and r - l > max_w:
        size = max(8, int(size * max_w / (r - l)))
        f = _font(fp, size)
        l, t, r, b = f.getbbox(text)
    pad = max(4, size // 10)
    tr = float(tracking) * size
    if abs(tr) > 0.01:
        wid = int(math.ceil(_tracked_len(text, f, tr) - l + 2))
        if max_w and wid > max_w:
            size = max(8, int(size * max_w / wid))
            f = _font(fp, size)
            l, t, r, b = f.getbbox(text)
            tr = float(tracking) * size
            wid = int(math.ceil(_tracked_len(text, f, tr) - l + 2))
        im = Image.new("L", (wid + 2 * pad, b - t + 2 * pad), 0)
        _draw_tracked(ImageDraw.Draw(im), (pad - l, pad - t), text, f, tr)
    else:
        im = Image.new("L", (r - l + 2 * pad, b - t + 2 * pad), 0)
        ImageDraw.Draw(im).text((pad - l, pad - t), text, font=f, fill=255)
    if soft > 0:
        rad = max(0.6, soft * size / 100.0)
        a = np.asarray(im.filter(ImageFilter.GaussianBlur(rad)), np.float32)
        a = np.clip((a - 118.0) * 3.2 + 128, 0, 255)          # re-threshold: rounded corners, soft edge
        im = Image.fromarray(a.astype(np.uint8))
    rgba = Image.new("RGBA", im.size, color + (0,))
    rgba.putalpha(im)
    return rgba


# ============================================================================== noise helpers
def _value_noise(rng, H, W, cells):
    import cv2
    g = rng.random((max(2, int(H / cells)) + 1, max(2, int(W / cells)) + 1)).astype(np.float32)
    return cv2.resize(g, (W, H), interpolation=cv2.INTER_CUBIC)


def grunge(W, H, seed, luma=7.0):
    """Near-black crumpled-paper texture, RGB uint8: a large-scale height field lit from the upper left
    (folds and creases), plus fine grain. Mean luma ~`luma` (the reference card measures ~6-7 between the
    letters), values ~0-40."""
    import cv2
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
    s = H / 1080.0
    h = 1.0 * _value_noise(rng, H, W, 560 * s) + 0.40 * _value_noise(rng, H, W, 230 * s)
    # crumple: fold the height field (abs of a centred field makes ridge lines)
    h = np.abs(h - np.median(h)) + 0.25 * h
    gx = cv2.Sobel(h, cv2.CV_32F, 1, 0, ksize=5)
    gy = cv2.Sobel(h, cv2.CV_32F, 0, 1, ksize=5)
    shade = -(0.7 * gx + 0.7 * gy)
    shade = shade / max(1e-6, np.percentile(np.abs(shade), 99))
    lit = np.clip(shade, 0, 1) ** 1.4
    tone = _value_noise(rng, H, W, 600 * s)                      # broad uneven exposure of the sheet
    fine = rng.normal(0, 1, (H, W)).astype(np.float32)
    y = 2.0 + 3.0 * tone + 24.0 * lit + 2.2 * fine
    y = y * (float(luma) / max(1e-3, float(y.mean())))
    y = np.clip(y, 0, 60)
    return np.stack([y, y, y * 0.98], axis=2).astype(np.uint8)


# ============================================================================== burns
PALETTES = {
    # (outgoing glow: deep, mid), (incoming disc: edge, body, core), fringe
    "warm": {"glow": ((235, 12, 8), (255, 42, 16)), "disc": ((255, 96, 20), (255, 168, 30), (255, 246, 215)),
             "fringe": ((235, 60, 190), (90, 80, 255))},
    "red": {"glow": ((190, 8, 12), (255, 50, 30)), "disc": ((240, 40, 30), (255, 110, 50), (255, 220, 190)),
            "fringe": ((220, 40, 160), (80, 60, 240))},
    "gold": {"glow": ((230, 70, 10), (255, 140, 30)), "disc": ((255, 140, 20), (255, 200, 50), (255, 245, 200)),
             "fringe": ((240, 90, 200), (110, 110, 255))},
}


def burn_layer(W, H, side, palette="warm", from_side="right", strength=1.0, seed=0, k=0, n=1):
    """One frame of film-burn LIGHT as RGB uint8 on black (composite with a screen blend: black = no
    change, 255 = blown). side "out" (outgoing glow, lower left) or "in" (incoming disc, from the right);
    k/n index the frame inside a multi-frame burn (the light grows on the way out, recedes on the way in)."""
    pal = PALETTES.get(palette, PALETTES["warm"])
    rng = np.random.default_rng((int(seed) * 7919 + k * 31 + (0 if side == "out" else 17)) & 0xFFFFFFFF)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    x = (xx + 0.5) / W
    y = (yy + 0.5) / H
    asp = W / H
    if from_side == "left":
        x = 1.0 - x
    lo_n = _value_noise(rng, H, W, 420 * H / 1080.0) - 0.5          # broad, smooth irregularity only
    md_n = _value_noise(rng, H, W, 150 * H / 1080.0) - 0.5
    grow = (k + 1) / max(1, n) if side == "out" else 1.0 - k / max(1, n)
    if side == "out":
        # red/orange glow rising from the lower left; the hottest part blows toward white
        cx, cy = 0.08 + rng.uniform(-0.04, 0.04), 0.98 + rng.uniform(-0.03, 0.03)
        d = np.sqrt(((x - cx) * asp / 1.55) ** 2 + ((y - cy) / 1.15) ** 2)
        r = (0.56 + 0.22 * grow) * (1 + 0.30 * lo_n + 0.08 * md_n)
        a = np.clip(1.0 - d / r, 0, 1)
        I = np.clip(a * 1.9, 0, 1) ** 0.8
        deep, mid = (np.array(c, np.float32) for c in pal["glow"])
        hot = np.clip((a - 0.50) / 0.30 + 0.6 * md_n, 0, 1)[..., None] ** 1.5 * 0.85
        col = deep * (1 - np.clip(a * 1.4, 0, 1)[..., None]) + mid * np.clip(a * 1.4, 0, 1)[..., None]
        col = col * (1 - hot) + np.array([255, 205, 160], np.float32) * hot
        light = col * I[..., None]
        # a faint violet haze high on the same side
        hz = np.clip(1.0 - np.sqrt(((x - 0.02) * asp / 1.7) ** 2 + ((y - 0.02) / 0.75) ** 2) / 0.7, 0, 1) ** 2
        light += np.array(pal["fringe"][1], np.float32) * 0.30 * hz[..., None] * (1 - I[..., None])
    else:
        # the left arc of a huge disc centred off-frame right: orange edge, yellow body, white-hot core,
        # magenta then blue fringe on the leading edge; the far side thins so the picture shows through
        cx, cy = 1.27 + rng.uniform(-0.05, 0.04), 0.46 + rng.uniform(-0.06, 0.06)
        R = 0.65 + rng.uniform(-0.05, 0.04) + 0.03 * (grow - 1)
        d = np.sqrt(((x - cx) * asp) ** 2 + (y - cy) ** 2) / asp
        edge = R - d + 0.018 * lo_n
        inside = np.clip(edge / 0.012 + 0.5, 0, 1)
        depth = np.clip(edge / 0.42, 0, 1)
        e_c, b_c, c_c = (np.array(c, np.float32) for c in pal["disc"])
        t1 = np.clip(depth * 2.0, 0, 1)[..., None]
        core = np.clip((depth - 0.42) / 0.30 + 0.35 * md_n, 0, 1)[..., None] ** 1.4 * 0.95
        body = e_c * (1 - t1) + b_c * t1
        body = body * (1 - core) + c_c * core
        thin = 1.0 - 0.6 * np.clip((depth - 0.72) / 0.28, 0, 1)            # far side lets the picture through
        light = body * (inside * thin)[..., None]
        mag, blu = (np.array(c, np.float32) for c in pal["fringe"])
        ring = np.exp(-((edge + 0.010) / 0.011) ** 2) * (1 - inside)
        ring2 = np.exp(-((edge + 0.032) / 0.016) ** 2) * (1 - inside)
        light += mag * (0.9 * ring)[..., None] + blu * (0.45 * ring2)[..., None]
        spill = np.clip(1.0 + edge / 0.25, 0, 1) ** 3 * (1 - inside) * 0.10
        light += e_c * spill[..., None]
    if from_side == "left":
        light = light[:, ::-1]
    return np.clip(light * float(strength), 0, 255).astype(np.uint8)


BURN_OPACITY = 0.7          # normal-mix share of the light (by its brightest channel) before the screen


def burn_alpha(light, opacity=BURN_OPACITY):
    """RGBA version of a light frame for the normal-mix pass: alpha = brightest channel x opacity."""
    a = (light.max(axis=2).astype(np.float32) / 255.0 * float(opacity) * 255.0).astype(np.uint8)
    return np.dstack([light, a])


def composite_burn(picture, light, opacity=BURN_OPACITY):
    """Reference implementation of the render's burn composite (float RGB 0-255 in and out): a normal mix
    toward the light, then a screen of the light (the ffmpeg graph does the same two steps)."""
    P = picture.astype(np.float32)
    L = light.astype(np.float32)
    m = L.max(axis=2, keepdims=True) / 255.0 * float(opacity)
    Q = P * (1 - m) + L * m
    return 255.0 - (255.0 - Q) * (255.0 - L) / 255.0


def burn_png(cache_dir, W, H, side, palette="warm", from_side="right", strength=1.0, seed=0, k=0, n=1):
    """(light RGB png, light RGBA png) for one burn frame, cached by content."""
    key = hashlib.sha1(json.dumps([CODE_VERSION, W, H, side, palette, from_side, round(float(strength), 3),
                                   int(seed), k, n]).encode()).hexdigest()[:16]
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"burn_{key}.png"
    pa = d / f"burn_{key}_a.png"
    if not (p.exists() and pa.exists()):
        L = burn_layer(W, H, side, palette, from_side, strength, seed, k, n)
        for path, im in ((p, Image.fromarray(L, "RGB")), (pa, Image.fromarray(burn_alpha(L), "RGBA"))):
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp.png")
            im.save(tmp)
            os.replace(tmp, path)
    return str(p), str(pa)


# ============================================================================== frames (gate / inset / letterbox)
def frame_spec(fr):
    """Normalise a plan `frame` value -> {kind, window (x0, y0, x1, y1 fractions), radius, feather, color,
    inner: {...} | None} or None (full frame)."""
    if not fr or fr in ("full", "none") or (isinstance(fr, dict) and fr.get("kind") in (None, "full", "none")):
        return None
    if isinstance(fr, str):
        fr = {"kind": fr}
    kind = fr["kind"]
    if kind == "gate":
        w, h = float(fr.get("w", GATE["w"])), float(fr.get("h", GATE["h"]))
        x0 = float(fr["x"]) if fr.get("x") is not None else (GATE["x"] if abs(w - GATE["w"]) < 1e-6 else (1 - w) / 2)
        y0 = float(fr["y"]) if fr.get("y") is not None else (GATE["y"] if abs(h - GATE["h"]) < 1e-6 else (1 - h) * 0.45)
        out = {"kind": "gate", "window": (x0, y0, x0 + w, y0 + h), "radius": float(fr.get("radius", GATE["radius"])),
               "feather": float(fr.get("feather", GATE["feather"])), "color": fr.get("color", GATE["color"])}
    elif kind == "inset":
        sc = float(fr.get("scale", INSET["scale"]))
        w, h = float(fr.get("w", sc)), float(fr.get("h", sc))
        x0 = float(fr["x"]) if fr.get("x") is not None else (1 - w) / 2
        y0 = float(fr["y"]) if fr.get("y") is not None else (1 - h) / 2
        out = {"kind": "inset", "window": (x0, y0, x0 + w, y0 + h), "radius": float(fr.get("radius", INSET["radius"])),
               "feather": float(fr.get("feather", INSET["feather"])), "color": fr.get("bg", fr.get("color", INSET["bg"]))}
    elif kind == "letterbox":
        asp = float(fr.get("aspect", 2.46))
        h = min(1.0, (16 / 9) / asp)
        out = {"kind": "letterbox", "window": (0.0, (1 - h) / 2, 1.0, (1 + h) / 2), "radius": float(fr.get("radius", 0)),
               "feather": float(fr.get("feather", 0)), "color": fr.get("color", "#000000")}
    else:
        raise ValueError(f"unknown frame kind {kind!r}")
    inner = fr.get("inner")
    out["inner"] = None
    if inner:
        x0, y0, x1, y1 = out["window"]
        ow, oh = x1 - x0, y1 - y0
        if inner.get("aspect"):
            iw = float(inner.get("w", ow * 0.95))
            ih = min(oh, iw * (16 / 9) / float(inner["aspect"]))
        else:
            iw, ih = float(inner.get("w", 0.77)), float(inner.get("h", 0.74))
        cx, cy = x0 + ow / 2, y0 + oh / 2
        out["inner"] = {"window": (cx - iw / 2, cy - ih / 2, cx + iw / 2, cy + ih / 2),
                        "radius": float(inner.get("radius", 15)), "feather": float(inner.get("feather", 2)),
                        "color": inner.get("color", "#000000")}
    return out


def _round_mask(W, H, win, radius, feather):
    """Float alpha 1 inside a rounded window (pixel radius at 1080, scaled), feathered edge."""
    s = H / 1080.0
    x0, y0, x1, y1 = win[0] * W, win[1] * H, win[2] * W, win[3] * H
    r = max(0.0, radius * s)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32) + 0.5
    dx = np.maximum(np.maximum(x0 + r - xx, xx - (x1 - r)), 0)
    dy = np.maximum(np.maximum(y0 + r - yy, yy - (y1 - r)), 0)
    dist = np.sqrt(dx * dx + dy * dy) - r                         # > 0 outside the rounded rect
    inside_rect = (xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)
    dist = np.where(inside_rect, dist, np.maximum(dist, 0.5 + np.maximum(np.maximum(x0 - xx, xx - x1),
                                                                         np.maximum(y0 - yy, yy - y1))))
    f = max(0.75, feather * s)
    return np.clip(0.5 - dist / f, 0, 1)


def matte_png(cache_dir, W, H, spec):
    """RGBA matte for a frame spec: border colour outside the window, (inner matte colour between the
    window and the inner window), transparent where the picture shows. Returns (png path, picture window
    in pixels (x0, y0, x1, y1))."""
    key = hashlib.sha1(json.dumps([CODE_VERSION, W, H, spec], default=str).encode()).hexdigest()[:16]
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"matte_{key}.png"
    pic = spec["inner"]["window"] if spec.get("inner") else spec["window"]
    box = (int(round(pic[0] * W)), int(round(pic[1] * H)), int(round(pic[2] * W)), int(round(pic[3] * H)))
    if not p.exists():
        a_out = _round_mask(W, H, spec["window"], spec["radius"], spec["feather"])
        col = np.array(hex_rgb(spec["color"]), np.float32)
        rgb = np.broadcast_to(col, (H, W, 3)).copy()
        alpha = 1.0 - a_out
        if spec.get("inner"):
            inn = spec["inner"]
            a_in = _round_mask(W, H, inn["window"], inn["radius"], inn["feather"])
            icol = np.array(hex_rgb(inn["color"]), np.float32)
            ring = a_out * (1 - a_in)                         # inside the gate, outside the picture
            rgb = rgb * (1 - a_out[..., None]) + icol * a_out[..., None]
            alpha = alpha + ring
        out = np.dstack([rgb, np.clip(alpha, 0, 1) * 255]).astype(np.uint8)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp.png")
        Image.fromarray(out, "RGBA").save(tmp)
        os.replace(tmp, p)
    return str(p), box


# ============================================================================== card + typed title
def card_spec(seg):
    c = {**CARD, **{k: v for k, v in (seg or {}).items() if v is not None}}
    return c


def card_sequence(cache_dir, W, H, fps, n, spec, seed=0):
    """Full-frame RGB PNG sequence (0000.png ...) of the card: boiling grunge + scaling text.
    Returns the directory."""
    fp = find_font("card", spec.get("font") or "")
    key = hashlib.sha1(json.dumps([CODE_VERSION, W, H, fps, n, {k: spec.get(k) for k in sorted(spec)}, _font_id(fp),
                                   int(seed)], default=str).encode()).hexdigest()[:16]
    d = Path(cache_dir) / f"card_{key}"
    if (d / f"{n - 1:04d}.png").exists():
        return str(d), fp
    tmp = Path(str(d) + f".{os.getpid()}.tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    col = hex_rgb(spec.get("color"), (251, 255, 9))
    layer = text_layer(str(spec.get("text") or CARD["text"]), fp, float(spec.get("size", CARD["size"])) * H, col,
                       soft=float(spec.get("soft", 1.6)), max_w=float(spec.get("max_w", CARD["max_w"])) * W,
                       tracking=float(spec.get("tracking", -0.035)))
    sf = float(spec.get("scale_from", CARD["scale_from"]))
    nf = max(1, int(spec.get("scale_frames", CARD["scale_frames"])))
    boil = max(1.0, float(spec.get("boil", CARD["boil"])))
    cy = float(spec.get("y", 0.486))
    last_bg, last_k = None, None
    for f in range(n):
        k = int(f // boil)
        if k != last_k:
            last_bg, last_k = grunge(W, H, seed * 1000 + k, float(spec.get("bg_luma", CARD["bg_luma"]))), k
        x = min(1.0, f / nf)
        sc = sf + (1.0 - sf) * (1 - (1 - x) ** 2)
        lw, lh = max(1, int(round(layer.width * sc))), max(1, int(round(layer.height * sc)))
        L = layer.resize((lw, lh), Image.LANCZOS)
        im = Image.fromarray(last_bg, "RGB")
        im.paste(L, (int(round(W / 2 - lw / 2)), int(round(H * cy - lh / 2))), L)
        im.save(tmp / f"{f:04d}.png", compress_level=1)
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    os.replace(tmp, d)
    return str(d), fp


def typed_sequence(cache_dir, W, H, fps, n, spec, offset=0):
    """Transparent RGBA PNG sequence of a typed title: nothing until `start` s, then one more character
    every `char_s` s (left to right, the full line's layout fixed from the start), then hold."""
    fp = find_font("title", spec.get("font") or "")
    text = str(spec.get("text", ""))
    key = hashlib.sha1(json.dumps([CODE_VERSION, W, H, fps, n, {k: spec.get(k) for k in sorted(spec)}, _font_id(fp)],
                                  default=str).encode()).hexdigest()[:16]
    d = Path(cache_dir) / f"typed_{key}"
    if (d / f"{n - 1:04d}.png").exists():
        return str(d), fp
    tmp = Path(str(d) + f".{os.getpid()}.tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    col = hex_rgb(spec.get("color"), hex_rgb(TITLE["color"]))
    cap = float(spec.get("size", TITLE["size"])) * H * 0.80        # size = text height incl. ascent; caps ~80 %
    full = text_layer(text, fp, cap, col)
    # character boundaries: the width of each prefix, so the line never re-flows while typing
    probe = _font(fp, 200)
    bb = probe.getbbox("H")
    size = max(8, int(round(200 * cap / max(1, bb[3] - bb[1]))))
    f = _font(fp, size)
    l0 = f.getbbox(text)[0]
    pad = max(4, size // 10)
    xs = [pad + (f.getlength(text[:i + 1]) - l0) for i in range(len(text))]
    x0 = int(round(W * float(spec.get("x", 0.5)) - full.width / 2))
    y0 = int(round(H * float(spec.get("y", 0.505)) - full.height / 2))
    if spec.get("x") is not None:                 # an explicit/auto position never pushes the title off frame
        x0 = max(0, min(W - full.width, x0))
        y0 = max(0, min(H - full.height, y0))
    start, cs = float(spec.get("start", 0.0)), max(0.005, float(spec.get("char_s", TITLE["char_s"])))
    blank = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    plate = blank
    if spec.get("backing"):                       # titleplace found no readable area: a dark plate behind it
        from PIL import ImageDraw
        pad_b = int(round(full.height * 0.18))
        plate = blank.copy()
        ImageDraw.Draw(plate).rounded_rectangle(
            (x0 - pad_b, y0 - pad_b // 2, x0 + full.width + pad_b, y0 + full.height + pad_b // 2),
            radius=max(2, pad_b // 2), fill=(12, 12, 12, 190))
    prev = None
    for i in range(n):
        t = (i + offset) / fps
        shown = 0 if t < start - 1e-6 else min(len(text), int((t - start) / cs + 1e-6) + 1)
        p = tmp / f"{i:04d}.png"
        if shown == prev and i > 0:
            os.link(tmp / f"{i - 1:04d}.png", p)
            continue
        im = (plate if shown else blank).copy()
        if shown:
            cut = int(round(xs[shown - 1])) + 2 if shown < len(text) else full.width
            im.paste(full.crop((0, 0, min(full.width, cut), full.height)), (x0, y0),
                     full.crop((0, 0, min(full.width, cut), full.height)))
        im.save(p, compress_level=1)
        prev = shown
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    os.replace(tmp, d)
    return str(d), fp


def typed_char_times(text, start, char_s):
    """Seconds (from the overlay start) at which each character appears; spaces ride with the next one."""
    out, t = [], float(start)
    for ch in text:
        out.append(round(t, 4))
        t += float(char_s)
    return out
