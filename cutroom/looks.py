"""Looks: named colour filters applied on top of the house grade (docs/PLAN_FORMAT.md "Looks").

A look is an ffmpeg filter chain that runs AFTER the base grade (LUT / log curve / saturation) and the
exposure correction, so it behaves the same on log and rec709 footage. Built-in looks are defined as
parameters (curves, colour balance, eq, vignette) and scaled by an intensity 0-1, so "50 %" is literally
half of every adjustment. Any `.cube` file the creator drops in `<work>/luts/` is listed as a look too
(`lut:<filename>`); its intensity is applied by blending the LUT with the identity cube (content-named
copies in `cache/luts/`). Nothing copyrighted is bundled: the built-ins are plain filter maths.

Plan fields:
  style.grade.look   {"name": "warm_film", "intensity": 0.8}     the whole edit
  segment.look       {"name": "bw", "intensity": 1.0}             one segment (wins over the plan's;
                                                                   {"name": "natural"} turns it off)
A bare string ("warm_film") means intensity 1.0.

Also here: frame previews of every look (one ffmpeg call renders the whole gallery from a single
decoded frame), colour stats of a picture (the same luma / contrast / sat / warmth the reference engine
measures) and `match()`, which picks the look + intensity closest to a reference's colour.
"""
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import numpy as np

# ------------------------------------------------------------------------------------------------- built-ins
# p keys: curves {all|r|g|b: [[x, y], ...]}, cb (colorbalance: rs gs bs rm gm bm rh gh bh, -1..1),
# eq {contrast, brightness, gamma, saturation}, vignette (angle in radians, 0 = none).
# css: the instant browser approximation for draft playback {brightness, contrast, saturate, sepia, hue}.
BUILTIN = [
    {"name": "natural", "label": "Natural", "desc": "The house grade, nothing added.",
     "p": {}, "css": {}},
    {"name": "warm_film", "label": "Warm Film", "desc": "Gentle warmth and a soft film curve.",
     "p": {"curves": {"all": [[0, .035], [.25, .235], [.75, .775], [1, .975]]},
           "cb": {"rs": .05, "bs": -.05, "rm": .05, "bm": -.06, "rh": .03, "bh": -.04},
           "eq": {"saturation": .95}},
     "css": {"sepia": .22, "saturate": .98, "contrast": 1.02, "brightness": 1.0, "hue": -4}},
    {"name": "cool_clean", "label": "Cool Clean", "desc": "Crisp, slightly cool, clean whites.",
     "p": {"cb": {"rs": -.03, "bs": .05, "rm": -.03, "bm": .04, "rh": -.01, "bh": .02},
           "eq": {"contrast": 1.06, "brightness": .01, "saturation": .96}},
     "css": {"hue": 8, "contrast": 1.06, "brightness": 1.02, "saturate": .92}},
    {"name": "golden_hour", "label": "Golden Hour", "desc": "Warm amber light, like late sun.",
     "p": {"cb": {"rs": .03, "bs": -.06, "rm": .1, "gm": .03, "bm": -.1, "rh": .06, "gh": .02, "bh": -.08},
           "eq": {"saturation": 1.12, "contrast": 1.03}},
     "css": {"sepia": .35, "saturate": 1.25, "contrast": 1.03, "brightness": 1.03, "hue": -8}},
    {"name": "teal_orange", "label": "Teal & Orange", "desc": "Teal shadows, warm skin. The blockbuster look.",
     "p": {"cb": {"rs": -.1, "gs": .02, "bs": .12, "rh": .1, "gh": .02, "bh": -.1},
           "eq": {"saturation": 1.1, "contrast": 1.06}},
     "css": {"sepia": .18, "saturate": 1.3, "contrast": 1.08, "hue": -6}},
    {"name": "moody_matte", "label": "Moody Matte", "desc": "Lifted blacks, muted colour, soft contrast.",
     "p": {"curves": {"all": [[0, .09], [.5, .47], [1, .92]]},
           "cb": {"bs": .03, "rs": -.01}, "eq": {"saturation": .78}, "vignette": .45},
     "css": {"contrast": .82, "saturate": .78, "brightness": .97}},
    {"name": "punchy", "label": "Punchy Contrast", "desc": "Deep blacks, bright highlights, rich colour.",
     "p": {"curves": {"all": [[0, 0], [.25, .2], [.75, .82], [1, 1]]}, "eq": {"saturation": 1.22}},
     "css": {"contrast": 1.22, "saturate": 1.28}},
    {"name": "soft_pastel", "label": "Soft Pastel", "desc": "Light, airy and low contrast.",
     "p": {"curves": {"all": [[0, .1], [.5, .55], [1, .97]]},
           "cb": {"rh": .02, "bh": .03, "gm": -.01}, "eq": {"saturation": .85}},
     "css": {"contrast": .84, "saturate": .85, "brightness": 1.07}},
    # mono = Rec.709 luma written to R = G = B (an RGB channel mix). Not eq=saturation=0: ffmpeg's eq writes
    # U = V = 127 for that, a measurable green cast (Lab a -3.2, b +2.7 on a neutral chart).
    {"name": "bw", "label": "Black & White", "desc": "Monochrome with a little extra contrast.",
     "p": {"mono": 1.0, "eq": {"contrast": 1.1}},
     "css": {"saturate": 0.0, "contrast": 1.1}},
    # generated looks: a 3D LUT built from a measured recipe (GEN_RECIPES below), not shipped data
    {"name": "bw_contrast", "label": "B&W Contrast", "gen": "bw_contrast", "reference_look": True,
     "desc": "Neutral low-key black and white: crushed toe, silver highlights that stop at 85 % (no paper white).",
     "css": {"saturate": 0.0, "contrast": 1.25, "brightness": 0.82}},
    {"name": "teal_night", "label": "Teal Night", "gen": "teal_night", "reference_look": True, "neutralize": True,
     "desc": "One cyan tint over every level, a 4/255 cyan black floor, skin pulled toward grey-cyan. "
             "White balance is neutralised first.",
     "css": {"saturate": 0.55, "contrast": 1.15, "brightness": 0.8, "hue": 170, "sepia": 0.2}},
    {"name": "vintage_fade", "label": "Vintage Fade", "desc": "Faded, warm, old-print colour.",
     "p": {"curves": {"r": [[0, .08], [.5, .53], [1, .96]], "g": [[0, .06], [.5, .5], [1, .93]],
                      "b": [[0, .12], [.5, .46], [1, .84]]},
           "eq": {"saturation": .74}, "vignette": .35},
     "css": {"sepia": .4, "contrast": .86, "saturate": .78, "brightness": 1.02}},
    {"name": "night_cinematic", "label": "Night Cinematic", "desc": "Dark, cool and dramatic. Night footage only.",
     "night": True,
     "p": {"cb": {"rs": -.05, "bs": .12, "bm": .06, "rh": .02},
           "eq": {"brightness": -.04, "gamma": .9, "contrast": 1.1, "saturation": .82}, "vignette": .5},
     "css": {"brightness": .82, "contrast": 1.12, "saturate": .8, "hue": 14}},
]
BUILTIN_BY_NAME = {b["name"]: b for b in BUILTIN}

# ------------------------------------------------------------------------------------------------- generated looks
# Recipes measured on a stylised brand-film reference (cutroom-refs forensic "picture" report): display-referred
# Rec.709 in -> out. Only the numbers are kept here; the .cube is generated from them on first use and cached by
# content (cache/luts/gen_<sha>.cube), so nothing third-party is bundled.
GEN_RECIPES = {
    # neutral mono (Rec.709 luma -> R=G=B) through a low-key curve: crushed toe, darker mids, white ceiling 0.85
    "bw_contrast": {"kind": "mono",
                    "curve": [[0, 0], [0.04, 0.004], [0.10, 0.022], [0.20, 0.068], [0.35, 0.17], [0.50, 0.30],
                              [0.65, 0.45], [0.80, 0.62], [0.90, 0.73], [1.0, 0.85]]},
    # one cyan hue at every level: luma curve with a 4/255 floor and a 0.94 ceiling, Lab chroma x0.70 (x0.35 near
    # the skin/orange axis, Lab hue 60 +- 30 deg), plus a cyan (a, b) tint by OUTPUT L
    "teal_night": {"kind": "tint",
                   "curve": [[0, 0.016], [0.05, 0.022], [0.15, 0.045], [0.30, 0.12], [0.50, 0.29], [0.70, 0.52],
                             [0.85, 0.72], [1.0, 0.94]],
                   "chroma": 0.70, "skin_chroma": 0.35, "skin_hue": 60.0, "skin_width": 30.0,
                   "tint": [[0, 0, 0], [10, -3.0, -2.2], [22, -6.8, -3.7], [45, -12.4, -6.5], [70, -16.1, -9.8],
                            [90, -11.2, -7.6], [100, -4.0, -3.0]]},
}
_Y709 = np.array([0.2126, 0.7152, 0.0722])
_M709 = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
_WP = _M709 @ np.ones(3)


def _lin(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _enc(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def _lab_f(t):
    return np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16 / 116)


def _lab_finv(t):
    return np.where(t > 0.206893, t ** 3, (t - 16 / 116) / 7.787)


def rgb_to_lab(rgb):
    """Display RGB (0-1, sRGB/709 primaries, D65) -> CIE Lab, rows of 3."""
    xyz = _lin(np.asarray(rgb, np.float64)) @ _M709.T / _WP
    fx, fy, fz = _lab_f(xyz[:, 0]), _lab_f(xyz[:, 1]), _lab_f(xyz[:, 2])
    return np.stack([116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)], 1)


def lab_to_rgb(lab):
    fy = (lab[:, 0] + 16) / 116
    fx, fz = fy + lab[:, 1] / 500, fy - lab[:, 2] / 200
    xyz = np.stack([_lab_finv(fx), _lab_finv(fy), _lab_finv(fz)], 1) * _WP
    return _enc(xyz @ np.linalg.inv(_M709).T)


def gen_transform(name):
    """The colour transform of a generated look: f(rgb rows 0-1) -> rgb rows 0-1."""
    fn = _gen_transform(name)

    def safe(p):
        with np.errstate(all="ignore"):          # Accelerate's matmul raises spurious FP flags on finite inputs
            return np.nan_to_num(fn(np.asarray(p, np.float64)), nan=0.0)
    return safe


def _gen_transform(name):
    r = GEN_RECIPES[name]
    xs, ys = zip(*r["curve"])

    def curve(x):
        return np.interp(x, xs, ys)
    if r["kind"] == "mono":
        def fn(p):
            return np.repeat(curve(p @ _Y709)[:, None], 3, 1)
        return fn
    Ls = [t[0] for t in r["tint"]]

    def fn(p):
        lab = rgb_to_lab(p)
        yo = curve(p @ _Y709)
        Lo = rgb_to_lab(np.repeat(yo[:, None], 3, 1))[:, 0]
        ta = np.interp(Lo, Ls, [t[1] for t in r["tint"]])
        tb = np.interp(Lo, Ls, [t[2] for t in r["tint"]])
        hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1]))
        d = (hue - r["skin_hue"] + 180) % 360 - 180
        k = r["chroma"] - (r["chroma"] - r["skin_chroma"]) * np.exp(-(d / r["skin_width"]) ** 2)
        return lab_to_rgb(np.stack([Lo, lab[:, 1] * k + ta, lab[:, 2] * k + tb], 1))
    return fn


def gen_cube_path(cfg, name, size=33):
    """Content-named generated .cube for a GEN look (cache/luts/gen_<name>_<sha>.cube)."""
    from .colorsci import Cube
    import inspect
    key = _sha([name, GEN_RECIPES[name], size, inspect.getsource(_gen_transform)], 12)    # content: recipe + maths
    d = Path(cfg.p("cache", "luts"))
    out = d / f"gen_{name}_{key}.cube"
    if out.exists() and out.stat().st_size > 0:
        return out
    with _LOCK:
        if not out.exists():
            Cube.from_function(gen_transform(name), size, f"cutroom {name} (generated from a measured recipe)").write(out)
    return out
CB_KEYS = ("rs", "gs", "bs", "rm", "gm", "bm", "rh", "gh", "bh")
_LOCK = threading.Lock()


class LookError(ValueError):
    pass


def _sha(obj, n=16):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:n]


def luts_dir(cfg):
    return Path(cfg.work) / "luts"


def lut_files(cfg):
    d = luts_dir(cfg)
    if not d.is_dir():
        return []
    from .cameras import reserved_lut_names
    hide = reserved_lut_names()                 # camera input / show transforms are not creative looks
    return sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".cube" and not p.name.startswith(".")
                   and p.name.lower() not in hide), key=lambda p: p.name.lower())


def normalize_look(v):
    """A plan look value -> {"name", "intensity"} or None (no look). Raises LookError when malformed."""
    if v is None or v == "" or v is False:
        return None
    if isinstance(v, str):
        v = {"name": v}
    if not isinstance(v, dict) or not isinstance(v.get("name"), str) or not v["name"].strip():
        raise LookError("look must be a name or {name, intensity}")
    name = v["name"].strip()
    k = v.get("intensity", 1.0)
    try:
        k = float(1.0 if k is None else k)
    except (TypeError, ValueError):
        raise LookError("look intensity must be a number from 0 to 1") from None
    if not 0.0 <= k <= 1.0:
        raise LookError("look intensity must be from 0 to 1 (0.8 = 80 %)")
    if name not in BUILTIN_BY_NAME and not name.startswith("lut:"):
        raise LookError(f"unknown look {name!r} (built-ins: {', '.join(BUILTIN_BY_NAME)}; or lut:<file in luts/>)")
    if name.startswith("lut:") and (not name[4:] or "/" in name[4:] or name[4:].startswith(".")):
        raise LookError("lut looks are lut:<file name in the project's luts/ folder>")
    out = {"name": name, "intensity": round(k, 3)}
    nz = v.get("neutralize")
    if nz is not None:
        if not isinstance(nz, bool):
            raise LookError("look neutralize must be true or false (white balance to neutral before the look)")
        out["neutralize"] = nz
    return out


def wants_neutral(lk):
    """True when a look neutralises white balance before it runs: its own `neutralize`, else the built-in's default."""
    if not lk:
        return False
    if lk.get("neutralize") is not None:
        return bool(lk["neutralize"])
    return bool((BUILTIN_BY_NAME.get(lk["name"]) or {}).get("neutralize"))


def validate_look(v, where="look"):
    """Plan validation messages for a look value (schema only; files are checked by check_files)."""
    try:
        normalize_look(v)
        return []
    except LookError as e:
        return [f"{where}: {e}"]


def check_files(cfg, v, where="look"):
    try:
        lk = normalize_look(v)
    except LookError as e:
        return [f"{where}: {e}"]
    if lk and lk["name"].startswith("lut:") and not (luts_dir(cfg) / lk["name"][4:]).is_file():
        return [f"{where}: no LUT file {lk['name'][4:]!r} in {luts_dir(cfg)}"]
    return []


def effective(plan_look, seg_look):
    """The look that applies to a segment: its own when set (natural = none), else the plan's."""
    try:
        lk = normalize_look(seg_look) if seg_look not in (None, "") else normalize_look(plan_look)
    except LookError:
        return None
    if not lk or lk["name"] == "natural" or lk["intensity"] <= 0.001:
        return None
    return lk


# ------------------------------------------------------------------------------------------------- catalogue
def catalogue(cfg):
    """[{name, label, desc, kind: builtin|lut, css}] for the UI."""
    out = [{"name": b["name"], "label": b["label"], "desc": b["desc"], "kind": "builtin", "css": b["css"]}
           for b in BUILTIN]
    for p in lut_files(cfg):
        try:
            css = _lut_css(p)
        except Exception:  # noqa: BLE001
            css = {}
        out.append({"name": f"lut:{p.name}", "label": p.stem.replace("_", " ").replace("-", " "),
                    "desc": "Your LUT (from the luts folder).", "kind": "lut", "css": css})
    return out


def label_of(name):
    if name in BUILTIN_BY_NAME:
        return BUILTIN_BY_NAME[name]["label"]
    return name[4:].rsplit(".", 1)[0] if name.startswith("lut:") else name


# ------------------------------------------------------------------------------------------------- LUT files
def _read_cube(path):
    size, dmin, dmax, rows = None, [0.0, 0.0, 0.0], [1.0, 1.0, 1.0], []
    for ln in Path(path).read_text(errors="replace").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        u = s.upper()
        if u.startswith("LUT_3D_SIZE"):
            size = int(s.split()[1])
        elif u.startswith("LUT_1D_SIZE"):
            raise LookError(f"{Path(path).name} is a 1D LUT; only 3D .cube LUTs are supported")
        elif u.startswith("DOMAIN_MIN"):
            dmin = [float(x) for x in s.split()[1:4]]
        elif u.startswith("DOMAIN_MAX"):
            dmax = [float(x) for x in s.split()[1:4]]
        elif u.startswith("TITLE") or u[0].isalpha():
            continue
        else:
            parts = s.split()
            if len(parts) >= 3:
                rows.append([float(x) for x in parts[:3]])
    if not size or len(rows) != size ** 3:
        raise LookError(f"{Path(path).name}: not a valid 3D .cube (size {size}, {len(rows)} entries)")
    return size, np.asarray(dmin), np.asarray(dmax), np.asarray(rows, dtype=np.float64)


def _identity(size, dmin, dmax):
    g = np.linspace(0.0, 1.0, size)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")           # red changes fastest in .cube order
    ident = np.stack([r.ravel(), gg.ravel(), b.ravel()], axis=1)
    return dmin + ident * (dmax - dmin)


def _lut_css(path):
    size, dmin, dmax, rows = _read_cube(path)
    ident = _identity(size, dmin, dmax)
    lum = lambda a: a @ np.array([0.2126, 0.7152, 0.0722])  # noqa: E731
    chroma = lambda a: a.max(1) - a.min(1)  # noqa: E731
    b = float(np.clip(lum(rows).mean() / max(1e-6, lum(ident).mean()), 0.6, 1.5))
    s = float(np.clip(chroma(rows).mean() / max(1e-6, chroma(ident).mean()), 0.0, 2.0))
    c = float(np.clip(lum(rows).std() / max(1e-6, lum(ident).std()), 0.6, 1.6))
    warm = float((rows[:, 0] - rows[:, 2]).mean() - (ident[:, 0] - ident[:, 2]).mean())
    return {"brightness": round(b, 3), "saturate": round(s, 3), "contrast": round(c, 3),
            "sepia": round(float(np.clip(warm * 2.5, 0, .5)), 3), "hue": round(float(np.clip(-warm * 40, -20, 20)), 1)}


def _lut_for(cfg, name, k):
    """Content-named copy of a user LUT, blended with identity at intensity k."""
    src = luts_dir(cfg) / name[4:]
    if not src.is_file():
        raise LookError(f"no LUT file {name[4:]!r} in the luts folder")
    h = hashlib.sha1(src.read_bytes()).hexdigest()[:16]
    d = Path(cfg.p("cache", "luts"))
    d.mkdir(parents=True, exist_ok=True)
    kk = round(float(k), 3)
    out = d / (f"{h}.cube" if kk >= 0.999 else f"{h}_k{int(round(kk * 1000)):04d}.cube")
    if out.exists():
        return out
    with _LOCK:
        if out.exists():
            return out
        tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        if kk >= 0.999:
            shutil.copyfile(src, tmp)
        else:
            size, dmin, dmax, rows = _read_cube(src)
            ident = _identity(size, dmin, dmax)
            mix = ident + (rows - ident) * kk
            lines = [f"LUT_3D_SIZE {size}", "DOMAIN_MIN " + " ".join(f"{v:g}" for v in dmin),
                     "DOMAIN_MAX " + " ".join(f"{v:g}" for v in dmax)]
            lines += [f"{a:.6f} {b:.6f} {c:.6f}" for a, b, c in mix]
            tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, out)
    return out


def _blend_cube(cfg, src, k):
    """A cube blended with identity at intensity k (content-named, cached)."""
    h = hashlib.sha1(Path(src).read_bytes()).hexdigest()[:16]
    kk = round(float(k), 3)
    out = Path(cfg.p("cache", "luts")) / f"{h}_k{int(round(kk * 1000)):04d}.cube"
    if out.exists():
        return out
    with _LOCK:
        if not out.exists():
            size, dmin, dmax, rows = _read_cube(src)
            ident = _identity(size, dmin, dmax)
            mix = ident + (rows - ident) * kk
            lines = [f"LUT_3D_SIZE {size}", "DOMAIN_MIN " + " ".join(f"{v:g}" for v in dmin),
                     "DOMAIN_MAX " + " ".join(f"{v:g}" for v in dmax)]
            lines += [f"{a:.6f} {b:.6f} {c:.6f}" for a, b, c in mix]
            tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
            tmp.write_text("\n".join(lines) + "\n")
            os.replace(tmp, out)
    return out


# ------------------------------------------------------------------------------------------------- chains
_TO_RGB = "scale=in_range=tv:in_color_matrix=bt709:out_range=full,format=gbrp"
_TO_YUV = "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p"


def _pts(points, k):
    return " ".join(f"{x:g}/{x + (y - x) * k:.4f}" for x, y in points)


def wb_filter(gains):
    """RGB gains (white balance to neutral) as an RGB-domain channel mixer; '' when ~unity."""
    if not gains or all(abs(float(g) - 1.0) < 0.004 for g in gains):
        return ""
    r, g, b = (float(x) for x in gains)
    return f"colorchannelmixer=rr={r:.4f}:gg={g:.4f}:bb={b:.4f}"


def wb_gains(rgb_mean, limit=(0.7, 1.45)):
    """Grey-world gains from the mean (R, G, B) of a picture's mid-tones: green kept, red/blue scaled to it,
    clamped so a scene that IS one colour is not forced grey."""
    r, g, b = (max(1e-3, float(x)) for x in rgb_mean)
    lo, hi = limit
    return [round(min(hi, max(lo, g / r)), 4), 1.0, round(min(hi, max(lo, g / b)), 4)]


def _mono_mixer(k):
    """Rec.709 luma to R = G = B at strength k (0 = untouched, 1 = mono), as a colorchannelmixer."""
    wr, wg, wb_ = (float(x) for x in _Y709)
    vals = {}
    for o, own in (("r", "r"), ("g", "g"), ("b", "b")):
        for i, w in (("r", wr), ("g", wg), ("b", wb_)):
            vals[f"{o}{i}"] = (1 - k) * (1.0 if i == own else 0.0) + k * w
    return "colorchannelmixer=" + ":".join(f"{kk}={v:.5f}" for kk, v in vals.items())


def chain(cfg, look, wb=None):
    """ffmpeg filters (list) that apply a look to a yuv420p rec709 picture and return yuv420p. [] = none.
    `wb` = RGB gains (wb_gains) applied before the look when the look neutralises white balance
    (render.prepare measures them per shot)."""
    lk = normalize_look(look)
    if not lk or lk["name"] == "natural" or lk["intensity"] <= 0.001:
        return []
    k = float(lk["intensity"])
    name = lk["name"]
    pre = wb_filter(wb) if (wb and wants_neutral(lk)) else ""
    if name.startswith("lut:"):
        lut = _lut_for(cfg, name, k)
        return ["scale=in_range=tv:in_color_matrix=bt709:out_range=full,format=gbrpf32le",
                *([pre] if pre else []), f"lut3d=file='{lut}':interp=tetrahedral", "format=gbrp16le", _TO_YUV]
    b = BUILTIN_BY_NAME.get(name)
    if b is None:
        raise LookError(f"unknown look {name!r}")
    if b.get("gen"):
        lut = gen_cube_path(cfg, b["gen"])
        if k < 0.999:
            lut = _blend_cube(cfg, lut, k)
        return ["scale=in_range=tv:in_color_matrix=bt709:out_range=full,format=gbrpf32le",
                *([pre] if pre else []), f"lut3d=file='{lut}':interp=tetrahedral", "format=gbrp16le", _TO_YUV]
    p = b["p"]
    f, rgb = [], [pre] if pre else []
    if p.get("mono"):
        rgb.append(_mono_mixer(float(p["mono"]) * k))
    if p.get("curves"):
        names = {"all": "master", "r": "red", "g": "green", "b": "blue"}
        rgb.append("curves=" + ":".join(f"{names[ch]}='{_pts(pts, k)}'" for ch, pts in p["curves"].items()))
    if p.get("cb"):
        vals = {kk: v * k for kk, v in p["cb"].items() if kk in CB_KEYS and abs(v * k) > 1e-4}
        if vals:
            rgb.append("colorbalance=" + ":".join(f"{kk}={v:.4f}" for kk, v in vals.items()))
    if rgb:
        f += [_TO_RGB, *rgb, _TO_YUV]
    eq = p.get("eq") or {}
    e = []
    for key, neutral, fmt in (("contrast", 1.0, "{:.4f}"), ("brightness", 0.0, "{:.4f}"), ("gamma", 1.0, "{:.4f}"),
                              ("saturation", 1.0, "{:.4f}")):
        if key in eq:
            v = neutral + (float(eq[key]) - neutral) * k
            if abs(v - neutral) > 1e-4:
                e.append(f"{key}={fmt.format(v)}")
    if e:
        f.append("eq=" + ":".join(e))
    if p.get("vignette") and p["vignette"] * k > 0.02:
        f.append(f"vignette=angle={p['vignette'] * k:.4f}")
    if f:
        f.append("format=yuv420p")
    return f


# ------------------------------------------------------------------------------------------------- previews
def _base_chain(cfg, con, row, plan_style, W, H):
    """The project's base grade for a clip at WxH (the same grade_chain a render uses on the master)."""
    from . import plan as P
    from . import render as R

    class _Pv(R._ResolveCtx):
        pass
    plan = P.normalize({"sections": [], "style": plan_style or {}}, cfg)
    ctx = _Pv(cfg, con, plan, False)
    ctx._cam, ctx._cam_con = {}, None                 # camera transforms (cameras.py) apply to look previews too
    lut = ctx.style["grade"].get("lut") or cfg.lut
    ctx.lut = R.Ctx._resolve_lut(ctx, lut) if lut else ""
    from . import cameras as CAM
    f, tag = R.grade_chain(ctx, row, CAM.decode_path(con, row), W, H)
    return f, tag


def _frame_wb(cfg, src, t, base, out_dir, st):
    """Grey-world white-balance gains of one base-graded frame (mid-tones), cached by content."""
    from . import media
    key = _sha(["wb1", src, st.st_size, int(st.st_mtime), round(t, 3), base])
    jp = Path(out_dir) / f"wb_{key}.json"
    if jp.exists():
        return json.loads(jp.read_text())
    raw = Path(out_dir) / f".wb_{key}.{os.getpid()}.raw"
    try:
        media.ffmpeg(cfg, ["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-vf",
                           ",".join(base) + ",scale=160:90,scale=in_range=tv:in_color_matrix=bt709:out_range=full,format=rgb24",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], timeout=120)
        a = np.frombuffer(raw.read_bytes(), np.uint8).reshape(-1, 3).astype(np.float64)
    finally:
        raw.unlink(missing_ok=True)
    g = wb_gains(midtone_mean(a))
    jp.write_text(json.dumps(g))
    return g


def midtone_mean(rgb):
    """Mean (R, G, B) of the mid-tone pixels (luma 20-90 % of range) of rows of 0-255 RGB; all pixels if too few."""
    a = np.asarray(rgb, np.float64).reshape(-1, 3)
    y = a[:, 0] * _Y709[0] + a[:, 1] * _Y709[1] + a[:, 2] * _Y709[2]
    m = (y > 0.2 * 255) & (y < 0.9 * 255)
    if m.sum() < 0.05 * len(a):
        m = np.ones(len(a), bool)
    return a[m].mean(axis=0).tolist()


def frame_size(row, width=480):
    w, h = int(row.get("width") or 16), int(row.get("height") or 9)
    if abs(int(row.get("rotation") or 0)) % 180 == 90:
        w, h = h, w
    H = max(2, int(round(width * h / max(1, w) / 2)) * 2)
    return int(width), H


def previews(cfg, con, clip, t, looks, plan_style=None, width=480, run=None):
    """Render (cached) JPEGs of one frame of `clip` at source time `t` in each look.
    looks: [{name, intensity}] (None / natural = the base grade only). Returns [Path] in order.
    All missing ones come from ONE ffmpeg run (one decode, split into N looks).
    `run(args)` executes ffmpeg (the studio passes one that holds a governor light slot)."""
    from . import db, media
    row = dict(db.get_clip(con, clip))
    dur = float(row.get("duration") or 0)
    t = max(0.0, min(float(t), max(0.0, dur - 0.05)))
    W, H = frame_size(row, width)
    base, tag = _base_chain(cfg, con, row, plan_style, W, H)
    from . import cameras as CAM
    src = CAM.decode_path(con, row)
    st = os.stat(src)
    out_dir = Path(cfg.p("frames", "looks"))
    out_dir.mkdir(parents=True, exist_ok=True)
    items = []
    wb = None
    if any(wants_neutral(normalize_look(lk)) for lk in looks if lk):
        wb = _frame_wb(cfg, src, t, base, out_dir, st)
    for lk in looks:
        lk = normalize_look(lk) or {"name": "natural", "intensity": 1.0}
        ch = chain(cfg, lk, wb=wb)
        key = _sha([src, st.st_size, int(st.st_mtime), round(t, 3), base, ch, W])
        items.append((lk, ch, out_dir / f"{row['id']}_{int(round(t * 1000)):08d}_{key}.jpg"))
    todo = [(lk, ch, p) for lk, ch, p in items if not (p.exists() and p.stat().st_size > 0)]
    if todo:
        tmps = [p.with_name(f".{p.stem}.{os.getpid()}.{threading.get_ident()}.jpg") for _, _, p in todo]
        n = len(todo)
        fc = [f"[0:v]{','.join(base)}" + (f",split={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else "[s0]")]
        for i, (lk, ch, _) in enumerate(todo):
            fc.append(f"[s{i}]{','.join(ch) if ch else 'null'},format=yuvj420p[o{i}]")
        args = ["-ss", f"{t:.3f}", "-i", src, "-filter_complex", ";".join(fc)]
        for i, tmp in enumerate(tmps):
            args += ["-map", f"[o{i}]", "-frames:v", "1", "-q:v", "3", "-update", "1", "-f", "image2", str(tmp)]
        try:
            if run is not None:
                run(args)
            else:
                media.ffmpeg(cfg, args, timeout=120)
            for tmp, (_, _, p) in zip(tmps, todo):
                if tmp.exists() and tmp.stat().st_size > 0:
                    os.replace(tmp, p)
        finally:
            for tmp in tmps:
                tmp.unlink(missing_ok=True)
    missing = [p for _, _, p in items if not p.exists()]
    if missing:
        raise LookError(f"could not render look previews for clip {clip} at {t:.2f}s")
    return [p for _, _, p in items]


# ------------------------------------------------------------------------------------------------- colour stats
def image_stats(path):
    """luma / contrast (std of luma) / sat (HSV S mean) / warmth (mean R - mean B), 0-255 units, like
    reference.py measures a reference's shots."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((240, 240))
    a = np.asarray(im, dtype=np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b              # cv2 BGR2GRAY weights
    mx, mn = a.max(2), a.min(2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6) * 255.0, 0.0)
    return {"luma": round(float(luma.mean()), 2), "contrast": round(float(luma.std()), 2),
            "sat": round(float(sat.mean()), 2), "warmth": round(float(r.mean() - b.mean()), 2),
            "p1": round(float(np.percentile(luma, 1)), 2)}


# weights: one unit of each stat matters about as much as the others for how a look reads
_W = {"luma": 1 / 18.0, "contrast": 1 / 10.0, "sat": 1 / 14.0, "warmth": 1 / 9.0}


def distance(a, b):
    return float(np.sqrt(sum(((a[k] - b[k]) * w) ** 2 for k, w in _W.items())))


NIGHT_SCENE_LUMA = 70.0     # footage brighter than this (mean luma through the LUT) is daylight: no night looks
MATCH_MIN_P1 = 15.0         # a matched look may not push the black point below this (unless the base is lower)
MATCH_MAX_SAT = 1.15        # nor saturation more than +15 % over the LUT-only picture


def is_night_look(name):
    return bool((BUILTIN_BY_NAME.get(name) or {}).get("night"))


def match(base_stats, look_stats, target, scene_luma=None, reference=False):
    """Pick the look and intensity whose result is closest to `target` colour stats.
    look_stats: {name: stats at intensity 1}. Stats move ~linearly with intensity, so each look is a
    line from base (k=0) to its full stats; the best k on that line is found by a fine scan.
    Guards (the creator called a night look on daylight footage "insane"): a night look is never picked
    for daylight footage (`scene_luma`, default the base's own luma, >= NIGHT_SCENE_LUMA), and the
    intensity is capped so the black point stays >= MATCH_MIN_P1 and saturation within +15 % of the
    LUT-only base.
    reference=True ("reference look" mode): the reference is itself a deliberate stylised grade (crushed blacks,
    a night tint on every shot), so the night-on-daylight exclusion and the black-point / saturation caps are
    off. It is used automatically when the reference's own black point is below MATCH_MIN_P1
    (`reference_mode(target)`).
    Returns {name, intensity, distance, base_distance, ranked: [{name, intensity, distance}], mode}."""
    ranked = []
    day = float(scene_luma if scene_luma is not None else base_stats.get("luma", 0)) >= NIGHT_SCENE_LUMA
    for name, st in look_stats.items():
        best = (distance(base_stats, target), 0.0)
        if day and is_night_look(name) and not reference:
            ranked.append({"name": name, "intensity": 0.0, "distance": round(best[0], 3), "excluded": "night look on daylight"})
            continue
        for k in np.linspace(0.2, 1.0, 17):
            s = {key: base_stats[key] + (st[key] - base_stats[key]) * k for key in _W}
            if "p1" in base_stats and "p1" in st and not reference:
                p1k = base_stats["p1"] + (st["p1"] - base_stats["p1"]) * k
                if p1k < min(MATCH_MIN_P1, base_stats["p1"]) - 1e-6:
                    break
            if s["sat"] > base_stats["sat"] * MATCH_MAX_SAT + 1e-6 and not reference:
                break
            d = distance(s, target)
            if d < best[0] - 1e-9:
                best = (d, float(k))
        ranked.append({"name": name, "intensity": round(best[1], 2), "distance": round(best[0], 3)})
    ranked.sort(key=lambda r: (r["distance"], r["name"] != "natural"))
    top = next((r for r in ranked if r["intensity"] > 0 and r["name"] != "natural"), None)
    base_d = distance(base_stats, target)
    if top is None or top["distance"] >= base_d - 0.05:
        top = {"name": "natural", "intensity": 1.0, "distance": round(base_d, 3)}
    return {**top, "base_distance": round(base_d, 3), "ranked": ranked, "mode": "reference" if reference else "guarded"}


def reference_mode(target, ref_p1=None):
    """True when the reference's own picture breaks the match guards on purpose: its black point (p1, 0-255) is
    under MATCH_MIN_P1, i.e. crushed blacks are the look, not a mistake to protect against."""
    p1 = ref_p1 if ref_p1 is not None else (target or {}).get("p1")
    return p1 is not None and float(p1) < MATCH_MIN_P1


# ------------------------------------------------------------------------------------------------- per-shot reference looks
MONO_SAT = 10.0          # a reference shot with mean HSV saturation (0-255) under this reads as black and white
COOL_WARMTH = -1.5       # mean R - B at or under this (0-255) on a coloured shot = a cool / cyan tint


def classify_shot(shot):
    """Look family of one measured reference shot (reference.py shot row: luma, sat, warmth):
    'black' (nothing to grade), 'mono' (black and white), 'cool' (cyan / teal tint), 'warm' or 'natural'."""
    luma, sat, warm = (shot.get(k) for k in ("luma", "sat", "warmth"))
    if luma is not None and float(luma) < 3.0:
        return "black"
    if sat is not None and float(sat) < MONO_SAT:
        return "mono"
    if warm is not None and float(warm) <= COOL_WARMTH:
        return "cool"
    if warm is not None and float(warm) >= 12.0:
        return "warm"
    return "natural"


FAMILY_LOOK = {"mono": "bw_contrast", "cool": "teal_night", "warm": "teal_night", "natural": None, "black": None}


def reference_looks(an):
    """Per-shot look plan for a measured reference (reference.load_analysis): which built-in look each of its
    shots maps to, and the share of runtime per family. A stylised reference switches looks BY SHOT, so the
    rebuild gives each of its segments the look of the reference shot it stands in for.
    'warm' maps to teal_night too: in a teal-graded film the warm shots are the same grade on tungsten /
    practical light (amber mids, cyan highlights), not a third grade."""
    shots = an.get("shots") or []
    dur = float(shots[-1]["end"] if shots else 0) or 1.0
    rows, fam_s = [], {}
    for sh in shots:
        fam = classify_shot(sh)
        d = float(sh.get("dur") or (sh["end"] - sh["start"]))
        fam_s[fam] = fam_s.get(fam, 0.0) + d
        rows.append({"i": sh.get("i"), "start": sh.get("start"), "end": sh.get("end"), "family": fam,
                     "sat": sh.get("sat"), "warmth": sh.get("warmth"), "luma": sh.get("luma")})
    colour = sum(fam_s.get(k, 0.0) for k in ("cool", "warm", "natural"))
    teal_film = colour > 0 and fam_s.get("cool", 0.0) >= 0.5 * colour     # most coloured time is cyan-tinted
    for r in rows:
        r["look"] = ("teal_night" if (teal_film and r["family"] in ("cool", "warm", "natural"))
                     else FAMILY_LOOK.get(r["family"]) if r["family"] != "natural" else None)
    shares = {k: round(v / dur, 3) for k, v in sorted(fam_s.items(), key=lambda kv: -kv[1])}
    ov = (an.get("colour") or {}).get("overall") or {}
    styl = (fam_s.get("mono", 0.0) + (fam_s.get("cool", 0.0) if teal_film else 0.0)) / dur
    if ov.get("p1") is not None:
        rm = reference_mode(ov)
    else:                       # no black point measured: a dark, mostly stylised (mono / tinted) film counts
        rm = bool(ov.get("luma") is not None and float(ov["luma"]) < 60 and styl >= 0.5)
    return {"shots": rows, "shares": shares, "teal_film": teal_film, "stylised_share": round(styl, 3),
            "reference_mode": rm}

