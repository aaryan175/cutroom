"""Render engine: edit plan (JSON) -> finished video.

Pipeline
  1. load + normalize + validate the plan (PlanError lists every problem)
  2. expand sections into a flat timeline of ATOMS on an integer frame grid
       talk      -> one atom per spoken piece (asr.tighten removes pauses)
       broll     -> one atom (optional voiceover from another clip, captioned)
       montage   -> one atom per shot; with beat_sync the cut points are beats of the section's song
       matchcut  -> A atom ending exactly at a.t + B atom starting exactly at b.t (free ends beat-snapped)
       title / black / still
     Segment layers are then mapped onto atoms: overlays + cutaways on talk are anchored to SOURCE
     time, so a graphic or b-roll shot runs continuously across the jump cuts inside it.
  3. probe each "unit" once (3 graded frames): faces (crop=face, caption/card placement) and mean luma
     (bounded exposure correction), plus dialogue loudness for talk units
  4. render overlay sequences (one headless-Chrome session, or Pillow) and caption PNGs
  5. render every atom to a cached intermediate (frame-exact video, sample-exact PCM audio). The cache
     key hashes the full ffmpeg argv + identity of every input file + the code version, so any change to
     grade, LUT, overlay content, captions, font, encoder or code re-renders exactly what it touches.
  6. concat (demuxer, stream copy) -> dialogue bus; music bed (ducked with ramps) + SFX -> mix ->
     programme loudness (measured gain + oversampled limiter, re-measured) -> final H.264/AAC encode
     + sidecar JSON timeline (<out>.json) that QA and the review board read.

Plan fields this renderer understands beyond the plan.py schema (all optional):
  style.loudness            {"I": -14, "TP": -1.5, "LRA": 11}   programme target
  section.music             + "mode" (duck|mute|full), "fade_in", "fade_out"; gain_db defaults to -8
  any segment / shot        "fit": "cover"|"blur" (blur = vertical source on a blurred fill), "stabilize"
  talk                      "auto_cover": true + "cover": [{"clip", "in"?}]  b-roll laid across every
                            join where tighten removed > 1.5 s (hides the jump cut)
  broll                     "voiceover": {"clip", "in", "out"}  dialogue (cleaned, captioned) from
                            another clip over the b-roll picture
  matchcut                  "b.align": {"scale" >= 1, "dx", "dy"} (fractions of the frame) to match
                            framing; "beat_sync" (default true when the section has music) snaps the
                            free ends to beats, the match frames never move; "audio"
  montage shot              "in" may be omitted (steadiest window is used), "speed"
  overlays on talk          "at" is seconds after the segment's "in" in SOURCE time, so the card stays
                            on its moment after re-tightening; "dur" is output time (runs across cuts)
  sfx name                  a built-in (music.SFX) or a path to a 48 kHz wav
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import captions as CAP
from . import cards as CD
from . import db
from . import film as FM
from . import governor
from . import looks as LK
from . import media
from . import overlays as OV
from . import plan as P

SR = 48000
_SRC_FILES = ("render.py", "captions.py", "overlays.py", "media.py", "cards.py", "film.py")
CODE_VERSION = hashlib.sha1(b"".join(Path(__file__).with_name(f).read_bytes() for f in _SRC_FILES
                                     if Path(__file__).with_name(f).exists())).hexdigest()[:12]

HOUSE_TAIL = "colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv420p:dither=fsb"
GENERIC_LOG = "format=gbrp,curves=all='0.09/0 0.25/0.13 0.41/0.4 0.6/0.68 0.8/0.9 1/1',format=yuv420p,eq=saturation=1.35"
LUMA_TARGET, LUMA_DEADBAND, EQ_BOUND = 118.0, 10.0, 0.08
SLOG_STOP = 0.075                        # S-Log3 code-value offset per stop of exposure (log domain, pre-LUT)
DIALOG_LUFS, NATURAL_LUFS, MIX_LUFS = -16.0, -20.0, -26.0
NATURAL_UNDER_MUSIC_LUFS, MONTAGE_NATURAL_DB = -24.0, -6.0
DEFAULT_MUSIC_GAIN = -8.0
FLASH_K_OUT, FLASH_K_IN = (0.30, 0.70), (0.80, 0.15)        # mix-to-white schedule (reference burns, measured)
PROGRAMME_OVER_LU = 2.0                  # dialogue-anchored loudness: the programme may sit this far over the target
JOIN_COVER_S = 1.5                       # auto_cover: joins where tighten removed more than this


def _jobs(cfg):
    env = os.environ.get("CUTROOM_MAX_JOBS")
    if env:
        return max(1, int(env))
    return max(1, min(cfg.n_workers(), 3))


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "untitled"


def _sha(obj, n=16):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:n]


def _file_id(path):
    try:
        st = os.stat(path)
        return [str(path), st.st_size, int(st.st_mtime)]
    except OSError:
        return [str(path), None, None]


def _file_sha1(path, cap=None):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class Ctx:
    """Everything one render needs; shared read-mostly across worker threads."""

    def __init__(self, cfg, con, plan, preview):
        self.cfg, self.con, self.plan, self.preview = cfg, con, plan, preview
        o = plan["output"]
        self.fps = float(o["fps"])
        if preview:
            self.H = int(cfg.preview_height)
            self.W = int(round(o["width"] * self.H / o["height"] / 2)) * 2
        else:
            self.W, self.H = int(o["width"]), int(o["height"])
        self.s = self.H / 1080.0
        self.style = plan["style"]
        self.cache = cfg.p("cache", "render")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.warnings = []
        self._warned = set()
        self.lock = threading.Lock()
        self._info = {}
        self._pregraded = {}
        cs = self.style["captions"]
        self.font_pi = CAP.find_font(cs.get("font") or cfg.caption_font or "")
        if not self.font_pi[0]:
            self.warn("no caption font found; using Pillow's built-in font (set style.captions.font)")
        self.lut = self._resolve_lut(self.style["grade"].get("lut") or cfg.lut)
        self.film = FM.normalize(self.style.get("film"))
        self.co_boost = 0.0

    def warn(self, msg, once_key=None):
        with self.lock:
            k = once_key or msg
            if k in self._warned:
                return
            self._warned.add(k)
            self.warnings.append(msg)
        print(f"  ! {msg}", flush=True)

    def _resolve_lut(self, lut):
        if not lut:
            return ""
        p = Path(os.path.expanduser(lut))
        if not p.is_absolute():
            p = Path(self.cfg.work) / p
        if not p.exists():
            raise P.PlanError(f"LUT not found: {lut}")
        d = self.cfg.p("cache", "luts")
        d.mkdir(parents=True, exist_ok=True)
        link = d / f"{_file_sha1(p)[:16]}.cube"           # content-named: safe in filtergraphs + cache keys
        if not link.exists():
            shutil.copyfile(p, link)
        return str(link)

    # -- sources ---------------------------------------------------------------------------
    def song(self, ref):
        return song(self, ref)

    def clip(self, ref):
        r = db.get_clip(self.con, ref)
        return dict(r)

    def source(self, row):
        """Input file for a clip: original master for finals, proxy for previews."""
        if self.preview and row.get("proxy") and os.path.exists(row["proxy"]):
            return row["proxy"]
        return row["path"]

    def info(self, path):
        with self.lock:
            if path in self._info:
                return self._info[path]
        pr = media.probe(self.cfg, path)
        s = media.summarize_probe(pr)
        v = next((x for x in pr.get("streams", []) if x.get("codec_type") == "video"), {})
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        if abs(int(s.get("rotation") or 0)) % 180 == 90:
            w, h = h, w
        fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        try:
            from fractions import Fraction
            fpsq = Fraction(fr) if Fraction(fr) > 0 else Fraction(v.get("r_frame_rate") or "0/1")
        except (ValueError, ZeroDivisionError):
            fpsq = None
        out = {"w": w, "h": h, "range": v.get("color_range") or "", "duration": s["duration"],
               "has_audio": bool(s["has_audio"]), "pix_fmt": s.get("pix_fmt", ""),
               "fps_q": (fpsq.numerator, fpsq.denominator) if fpsq else None}
        with self.lock:
            self._info[path] = out
        return out

    def src_fps(self, row, src):
        """(num, den) frame rate of the file a piece is cut from, or None."""
        q = self.info(src).get("fps_q")
        if q and q[0] > 0:
            return q
        f = float(row.get("fps") or 0) if row else 0.0
        return _fps_q(f) if f > 0 else None

    def pregraded(self, row, src):
        """True when a preview proxy already has the log->709 LUT baked in (ingest bakes cfg.lut into
        proxies of log clips). Decided from the picture: graded proxies are not desaturated."""
        if src == row["path"] or not row.get("is_log"):
            return False
        with self.lock:
            if src in self._pregraded:
                return self._pregraded[src]
        tmp = self.cache / f"sat_{_sha(_file_id(src))}.raw"
        if not tmp.exists():
            t = max(0.0, float(row.get("duration") or 1) / 2)
            media.ffmpeg(self.cfg, ["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-vf", "scale=96:54,format=rgb24",
                                    "-f", "rawvideo", tmp])
        a = np.frombuffer(tmp.read_bytes(), np.uint8).reshape(-1, 3).astype(np.float32)
        sat = float(np.mean((a.max(1) - a.min(1)) / np.maximum(a.max(1), 1) * 255))
        res = sat > 45
        with self.lock:
            self._pregraded[src] = res
        return res


# ============================================================================== picture chains
def _range_of(info):
    return "full" if info.get("range") in ("pc", "jpeg") else "tv"


def _fps_q(f):
    """Float frame rate -> exact (num, den) for the NTSC rates."""
    for num, den in ((24000, 1001), (30000, 1001), (60000, 1001), (48000, 1001)):
        if abs(f - num / den) < 0.005:
            return (num, den)
    from fractions import Fraction
    q = Fraction(f).limit_denominator(1001)
    return (q.numerator, q.denominator)


def finish_chain(ctx):
    """The house finishing curve: one fixed string for every shot (black point ~10/255, soft highlight
    shoulder, gentle vibrance). Applied after the LUT; never per shot (rule 94)."""
    fin = ctx.style["grade"].get("finish") or {}
    if fin is False or not fin.get("enabled", True):
        return []
    b, sh = float(fin.get("black", 0.028)), float(fin.get("shoulder", 0.96))
    out = [f"curves=master='0/0 0.06/{b:.4f} 0.18/0.15 0.5/0.5 0.82/0.84 0.94/0.925 1/{sh:.4f}'"]
    v = float(fin.get("vibrance", 0.18) or 0)
    if abs(v) > 1e-3:
        out.append(f"vibrance=intensity={v:.3f}")
    return out


def log_exposure(stops, enable=None):
    """Exposure in the LOG domain (before the LUT): an offset of SLOG_STOP code values per stop."""
    o = SLOG_STOP * abs(float(stops))
    en = f":enable='{enable}'" if enable else ""
    if stops > 0:
        return f"curves=all='0/{o:.4f} {1 - o:.4f}/1'{en}"
    return f"curves=all='{o:.4f}/0 1/{1 - o:.4f}'{en}"


def display_exposure(stops, enable=None):
    """Exposure on display-referred (Rec.709) pictures: a gain with the black point pinned at 0
    (never eq=brightness, which lifts blacks)."""
    g = 2 ** (float(stops) / 2.4)
    en = f":enable='{enable}'" if enable else ""
    if g >= 1:
        return f"curves=all='0/0 {min(1.0, 1 / g):.4f}/1'{en}"
    return f"curves=all='0/0 1/{g:.4f}'{en}"


def grade_chain(ctx, row, src, W, H, stops=0.0, pre_fx=(), probe=False):
    """Filters from a cropped picture to graded yuv420p WxH (scale is part of the chain: the LUT
    always runs at output size). House grade v2:
        exposure (log domain, before the LUT; bounded, half the measured error) -> LUT -> finish curve
    The finish is identical on every shot; a shot's only individual value is its exposure in stops.
    `pre_fx` = [(frame, stops)] extra exposure pushes on single frames (flash transitions).
    probe=True gives the LUT-only chain the exposure is measured on."""
    info = ctx.info(src)
    rng = _range_of(info)
    sat = float(ctx.style["grade"].get("saturation", 1.0) or 1.0)
    is_log = bool(row.get("is_log")) if row else False
    pre = ctx.pregraded(row, src) if row else False
    log_domain = is_log and not pre
    expo = log_exposure if log_domain else display_exposure
    ops = []
    if not probe:
        if abs(float(stops)) > 0.01:
            ops.append(expo(stops))
        ops += [expo(st, f"eq(n,{int(fr)})") for fr, st in (pre_fx or ())]
    if log_domain and not ctx.lut:
        ctx.warn("log footage without a LUT: using a generic log->709 curve (set config lut or style.grade.lut)",
                 once_key="nolut")
        f = [f"scale={W}:{H}:flags=lanczos:in_range={rng}:out_range=tv:in_color_matrix=bt709:out_color_matrix=bt709",
             "format=yuv420p"] + (["format=gbrp"] + ops if ops else []) + [GENERIC_LOG]
        if not probe:
            fin = finish_chain(ctx)
            if fin:
                f += ["format=gbrp"] + fin + ["format=yuv420p"]
        tag = "generic-log"
    else:
        # every other path runs in full-range RGB with an explicit BT.709 matrix both ways
        f = [f"scale={W}:{H}:flags=lanczos:in_range={rng}:out_range=full:in_color_matrix=bt709", "format=gbrp16le"] + ops
        if log_domain:
            f += ["format=gbrpf32le", f"lut3d=file='{ctx.lut}':interp=tetrahedral"]
            tag = "lut"
        else:
            tag = "pregraded" if pre else "rec709"
        if not probe:
            f += finish_chain(ctx)
        f += ["format=gbrp16le", HOUSE_TAIL]
    if abs(sat - 1.0) > 1e-3 and not probe:
        f.append(f"eq=saturation={sat:.3f}")
    return f, tag


def crop_rect(dw, dh, W, H, crop="center", zoom=1.0, faces=(), align=None):
    """Source crop window (fractions x, y, w, h of the display frame) with output aspect W/H."""
    A = W / H
    if isinstance(crop, (list, tuple)) and len(crop) == 4:
        rx, ry, rw, rh = (float(v) for v in crop)
    else:
        rx, ry, rw, rh = 0.0, 0.0, 1.0, 1.0
    rwp, rhp = rw * dw, rh * dh
    if rwp / rhp > A:
        cw, ch = rhp * A, rhp
    else:
        cw, ch = rwp, rwp / A
    cx, cy = (rx + rw / 2) * dw, (ry + rh / 2) * dh
    zoom = max(1.0, float(zoom or 1.0))
    if align:
        zoom *= max(1.0, float(align.get("scale", 1.0) or 1.0))
    cw, ch = cw / zoom, ch / zoom
    if crop == "face" and faces:
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        cx = (fx + fw / 2) * dw
        cy = (fy + fh / 2) * dh + ch * 0.1           # face slightly above centre
    if align:
        cx -= float(align.get("dx", 0) or 0) * cw
        cy -= float(align.get("dy", 0) or 0) * ch
    x0 = min(max(0.0, cx - cw / 2), dw - cw)
    y0 = min(max(0.0, cy - ch / 2), dh - ch)
    return (x0 / dw, y0 / dh, cw / dw, ch / dh)


def _crop_px(rect, info):
    dw, dh = info["w"], info["h"]
    x, y, w, h = rect
    cw = max(2, int(round(w * dw / 2)) * 2)
    ch = max(2, int(round(h * dh / 2)) * 2)
    cx = min(max(0, int(round(x * dw))), dw - cw)
    cy = min(max(0, int(round(y * dh))), dh - ch)
    return f"crop={cw}:{ch}:{cx}:{cy}"


def look_chain(ctx, look):
    """Filters of a look (cutroom/looks.py) to append after the base grade; [] for none."""
    if not look:
        return []
    return LK.chain(ctx.cfg, look)


def push_chain(push, W, H):
    """Slow digital push-in / pull-out (a slider stand-in) as a sub-pixel `perspective` warp evaluated per
    frame - the smoothest of the measured methods (zoompan rounds to whole pixels and jitters; scale+crop
    stair-steps). push = {"to", "ease", "x", "y", "off_f", "tot_f"}: zoom follows the SEGMENT's time, so a
    push runs continuously across the jump cuts inside a talk segment."""
    if not push:
        return []
    to = float(push.get("to", 1.06))
    off, tot = int(push.get("off_f", 0)), max(1, int(push.get("tot_f", 1)) - 1)
    P_ = f"clip(({off}+in)/{tot},0,1)"
    ease = push.get("ease", "inout")
    E = {"linear": P_, "in": f"pow({P_},2)", "out": f"(1-pow(1-{P_},2))"}.get(ease, f"(pow({P_},2)*(3-2*{P_}))")
    z0, z1 = (1.0, to) if to >= 1 else (1.0 / to, 1.0)
    Z = f"({z0:.6f}+{z1 - z0:.6f}*{E})"
    fx, fy = float(push.get("x", 0.5)), float(push.get("y", 0.5))
    X0, Y0 = f"(W-W/{Z})*{fx:.4f}", f"(H-H/{Z})*{fy:.4f}"
    X1, Y1 = f"({X0}+W/{Z})", f"({Y0}+H/{Z})"
    return ["format=gbrp", f"perspective=x0='{X0}':y0='{Y0}':x1='{X1}':y1='{Y0}':x2='{X0}':y2='{Y1}':x3='{X1}':y3='{Y1}'"
            ":interpolation=cubic:eval=frame", "format=yuv420p"]


def film_parts(ctx, label, W, H):
    """Film/lens character (style.film) as graph chains from [IN] to [OUT]; None when off."""
    fl = getattr(ctx, "film", None)
    if not fl:
        return None
    ch = FM.chain(fl, W, H)
    return FM.expand(ch, label, W, H) if ch else None


def picture(ctx, row, src, W, H, rect, stops=0.0, fit="cover", stabilize=False, label="p", look=None,
            pre_fx=(), push=None):
    """See _picture; adds the plan's film/lens character (style.film) after the grade and look."""
    ch = _picture(ctx, row, src, W, H, rect, stops, fit, stabilize, label, look, pre_fx, push)
    fp = film_parts(ctx, label, W, H)
    if not fp:
        return ch
    mid = f"[{label}film]"
    return [c.replace("[OUT]", mid) for c in ch] + [c.replace("[IN]", mid, 1) for c in fp]


def _picture(ctx, row, src, W, H, rect, stops=0.0, fit="cover", stabilize=False, label="p", look=None,
             pre_fx=(), push=None):
    """Filtergraph fragment list turning an already-timed input into graded WxH yuv420p.
    The chain is: crop -> grade_chain (log exposure -> LUT -> house finish / saturation) -> look
    (style.grade.look or segment.look) -> stabilise -> push-in. Everything is part of the filter text, so
    it is in the atom's cache key. Returns (list_of_chains_with_IN_and_OUT_placeholders)."""
    info = ctx.info(src)
    g, _ = grade_chain(ctx, row, src, W, H, stops, pre_fx)
    eqs = look_chain(ctx, look)
    stab = []
    if stabilize:
        stab = [f"deshake=rx=48:ry=48:edge=mirror", f"scale={int(W * 1.06) // 2 * 2}:-2", f"crop={W}:{H}"]
    mv = push_chain(push, W, H)
    vertical = info["h"] > info["w"] and W > H
    if fit == "blur" and vertical:
        fw = int(round(H * info["w"] / info["h"] / 2)) * 2
        g_fg, _ = grade_chain(ctx, row, src, fw, H, stops, pre_fx)
        return [f"[IN]split[{label}a][{label}b]",
                f"[{label}a]{','.join(g_fg + eqs)}[{label}f]",
                f"[{label}b]{','.join(g + eqs)},gblur=sigma={36 * ctx.s:.1f},eq=brightness=-0.08:saturation=0.9[{label}g]",
                f"[{label}g][{label}f]overlay=(W-w)/2:0{(',' + ','.join(stab + mv)) if stab or mv else ''},setsar=1,format=yuv420p[OUT]"]
    return [f"[IN]{_crop_px(rect, info)},{','.join(g + eqs + stab + mv)},setsar=1,format=yuv420p[OUT]"]


# ============================================================================== probes
_CASCADE = threading.local()        # cv2 cascades are not thread-safe: one per worker thread


def _faces_in(gray):
    import cv2
    if not hasattr(_CASCADE, "c"):
        _CASCADE.c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h, w = gray.shape
    eqd = cv2.equalizeHist(gray)
    m = max(20, int(min(w, h) * 0.08))
    found = _CASCADE.c.detectMultiScale(eqd, scaleFactor=1.15, minNeighbors=6, minSize=(m, m))
    return [(x / w, y / h, fw / w, fh / h) for x, y, fw, fh in found]


def probe_unit(ctx, row, src, t0, t1):
    """3 graded frames across [t0, t1] -> faces (fractions of the display frame) + small luma grids."""
    info = ctx.info(src)
    pw = 640
    ph = max(2, int(round(pw * info["h"] / max(1, info["w"]) / 2)) * 2)
    gf, tag = grade_chain(ctx, row, src, pw, ph, probe=True)
    span = max(0.0, t1 - t0)
    times = [t0 + span * f for f in (0.15, 0.5, 0.85)]
    key = _sha(["probe3", _file_id(src), [round(t, 3) for t in times], gf, CODE_VERSION])
    jp = ctx.cache / f"probe_{key}.json"
    if jp.exists():
        return json.loads(jp.read_text())
    raw = ctx.cache / f"probe_{key}.raw"
    args, fc = [], []
    for k, t in enumerate(times):
        args += ["-ss", f"{max(0.0, min(t, info['duration'] - 0.1)):.3f}", "-i", src]
        fc.append(f"[{k}:v]trim=end_frame=1,setpts=PTS-STARTPTS,{','.join(gf)},"
                  f"scale=in_range=tv:in_color_matrix=bt709:out_range=full,format=rgb24[g{k}]")
    fc.append(f"{''.join(f'[g{k}]' for k in range(len(times)))}concat=n={len(times)}:v=1:a=0[o]")
    media.ffmpeg(ctx.cfg, args + ["-filter_complex", ";".join(fc), "-map", "[o]", "-f", "rawvideo", "-pix_fmt", "rgb24", raw])
    a = np.frombuffer(raw.read_bytes(), np.uint8)
    nfr = len(a) // (pw * ph * 3)
    rgb = a[: nfr * pw * ph * 3].reshape(nfr, ph, pw, 3)
    yf = rgb.astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    frames = np.clip(yf, 0, 255).astype(np.uint8)
    mx, mn = rgb.max(axis=3).astype(np.float32), rgb.min(axis=3).astype(np.float32)
    lut_stats = {"luma": round(float(np.median(yf)), 1), "p1": round(float(np.percentile(yf, 1)), 1),
                 "p99": round(float(np.percentile(yf, 99)), 1),
                 "sat": round(float(np.mean(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)) * 255), 1)}
    faces = []
    for fr in frames:
        faces += _faces_in(np.ascontiguousarray(fr))
    import cv2
    grids = [cv2.resize(fr, (64, 36), interpolation=cv2.INTER_AREA).astype(float).tolist() for fr in frames]
    res = {"faces": [list(map(float, f)) for f in faces], "grids": grids, "grade": tag, "lut_stats": lut_stats}
    jp.write_text(json.dumps(res))
    raw.unlink(missing_ok=True)
    return res


def lut_frame_stats(ctx, row, src, t, rect):
    """LUT-only numbers (luma median, p1, p99, sat 0-255) of one source frame inside a crop rect: what the
    house grade alone makes of exactly this picture. QA grade_bounds compares the render against it."""
    info = ctx.info(src)
    pw = 480
    ph = max(2, int(round(pw * 9 / 16 / 2)) * 2)
    gf, tag = grade_chain(ctx, row, src, pw, ph, probe=True)
    key = _sha(["lutstat1", _file_id(src), round(float(t), 3), [round(v, 4) for v in rect], gf, CODE_VERSION])
    jp = ctx.cache / f"lutstat_{key}.json"
    if jp.exists():
        return json.loads(jp.read_text())
    raw = ctx.cache / f"lutstat_{key}.raw"
    media.ffmpeg(ctx.cfg, ["-ss", f"{max(0.0, min(float(t), info['duration'] - 0.1)):.3f}", "-i", src, "-frames:v", "1",
                           "-vf", f"{_crop_px(rect, info)},{','.join(gf)},scale=in_range=tv:in_color_matrix=bt709:out_range=full,"
                           "format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", raw])
    a = np.frombuffer(raw.read_bytes(), np.uint8)
    raw.unlink(missing_ok=True)
    rgb = a[: (len(a) // (pw * ph * 3)) * pw * ph * 3].reshape(-1, ph, pw, 3)[0]
    body = rgb[: int(ph * 0.8)].astype(np.float32)          # outside the caption band, like QA measures
    y = body @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    mx, mn = body.max(axis=2), body.min(axis=2)
    res = {"luma": round(float(np.median(y)), 1), "p1": round(float(np.percentile(y, 1)), 1),
           "p99": round(float(np.percentile(y, 99)), 1),
           "sat": round(float(np.mean(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)) * 255), 1), "t": round(float(t), 3)}
    jp.write_text(json.dumps(res))
    return res


def luma_in(probe, rect):
    if not probe or not probe.get("grids"):
        return None
    x, y, w, h = rect
    vals = []
    for gr in probe["grids"]:
        g = np.asarray(gr)
        x0, x1 = int(x * 64), max(int(x * 64) + 1, int((x + w) * 64))
        y0, y1 = int(y * 36), max(int(y * 36) + 1, int((y + h) * 36))
        vals.append(float(g[y0:y1, x0:x1].mean()))
    return float(np.mean(vals))


def luma_median_in(probe, rect):
    """Median luma (0-255) of the probe frames inside a crop rect (LUT-only chain)."""
    if not probe or not probe.get("grids"):
        return None
    x, y, w, h = rect
    vals = []
    for gr in probe["grids"]:
        g = np.asarray(gr)
        x0, x1 = int(x * 64), max(int(x * 64) + 1, int((x + w) * 64))
        y0, y1 = int(y * 36), max(int(y * 36) + 1, int((y + h) * 36))
        vals.append(g[y0:y1, x0:x1].ravel())
    return float(np.median(np.concatenate(vals)))


def exposure_stops(med, ex=None):
    """Bounded exposure correction in stops from the median luma measured through the LUT only:
    error = log2 of the display-light ratio to the target (gamma 2.4), corrected by `strength` (half) of
    it, clamped to +-max_stops, 0 inside the deadband. Night scenes aim lower so night stays night."""
    if med is None:
        return 0.0
    ex = ex or {}
    target = float(ex.get("target_luma", LUMA_TARGET))
    if med < float(ex.get("night_luma", 60.0)):
        target = float(ex.get("night_target", 70.0))
    err = 2.4 * math.log2(target / max(1.0, float(med)))
    if abs(err) < float(ex.get("deadband_stops", 0.33)):
        return 0.0
    mx = float(ex.get("max_stops", 1.0))
    return round(max(-mx, min(mx, float(ex.get("strength", 0.5)) * err)), 3)


def exposure_eq(mean):
    """Bounded correction toward LUMA_TARGET; no-op inside the deadband."""
    if mean is None:
        return None
    d = LUMA_TARGET - mean
    if abs(d) < LUMA_DEADBAND:
        return {"brightness": 0.0, "gamma": 1.0, "mean": round(mean, 1)}
    d = d - math.copysign(LUMA_DEADBAND, d)
    b = max(-EQ_BOUND, min(EQ_BOUND, 0.5 * d / 255))
    gm = max(1 - EQ_BOUND, min(1 + EQ_BOUND, 1 + 0.9 * d / 255))
    return {"brightness": round(b, 4), "gamma": round(gm, 4), "mean": round(mean, 1)}


def faces_out(faces, rect, W, H):
    """Source-fraction face boxes -> output pixel boxes for a crop rect."""
    x, y, w, h = rect
    out = []
    for fx, fy, fw, fh in faces:
        ox, oy = (fx - x) / w * W, (fy - y) / h * H
        ow, oh = fw / w * W, fh / h * H
        if ox + ow > 0 and oy + oh > 0 and ox < W and oy < H:
            out.append((ox, oy, ow, oh))
    return out


def measure_loudness(ctx, path, t0, dur, chain):
    key = _sha(["lufs", _file_id(path), round(t0, 3), round(dur, 3), chain])
    jp = ctx.cache / f"lufs_{key}.json"
    if jp.exists():
        return json.loads(jp.read_text())["I"]
    r = media.ffmpeg(ctx.cfg, ["-loglevel", "info", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", path, "-vn",
                               "-af", f"aresample={SR},{chain + ',' if chain else ''}loudnorm=print_format=json",
                               "-f", "null", "-"])
    m = re.search(r'"input_i"\s*:\s*"([-\d.inf]+)"', r.stderr or "")
    val = float(m.group(1)) if m and "inf" not in m.group(1) else None
    jp.write_text(json.dumps({"I": val}))
    return val


# ============================================================================== songs
def song(ctx, ref):
    from . import music
    try:
        return music.song_info(ctx.cfg, ctx.con, ref)
    except KeyError:
        p = Path(os.path.expanduser(str(ref)))
        if not p.is_absolute():
            p = Path(ctx.cfg.work) / p
        if not p.exists():
            raise
        jp = ctx.cfg.p("cache", "beats", f"{_sha(_file_id(p))}.json")
        if jp.exists():
            d = json.loads(jp.read_text())
        else:
            d = music.analyze_beats(str(p))
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(json.dumps(d))
        return {"path": str(p), "title": p.stem, **d}


# ============================================================================== cold open (teaser template)
_STAKES = re.compile(r"\b(\d[\d,.]*|biggest|best|worst|insane|crazy|craziest|never|first|last|million|millions|"
                     r"thousand|money|dollars?|free|secret|why|wow|literally|most|only|dream|finally|everything|"
                     r"nobody|everyone|impossible|huge|massive|incredible|unbelievable)\b", re.I)


def _find_section(plan, ref):
    """'<section index or name>/<segment index>' -> (section dict, segment index or None)."""
    sp, _, gp = str(ref).partition("/")
    sp = sp.strip().lower()
    secs = plan.get("sections", [])
    sec = secs[int(sp)] if sp.isdigit() and int(sp) < len(secs) else \
        next((s for s in secs if str(s.get("name", "")).strip().lower() == sp), None)
    if sec is None:
        raise P.PlanError(f"cold_open line: no section {ref!r}")
    return sec, (int(gp) if gp.strip().isdigit() else None)


def teaser_candidates(words, a, b, lo=0.8, hi=2.6):
    """Teaser-line candidates inside [a, b]: [(score, t0, t1, text)], best first. Short claim-like lines
    (numbers, superlatives, stakes words) that start and end on word boundaries, ideally on punctuation."""
    ws = [w for w in words if a <= w[0] and w[1] <= b]
    out = []
    for i in range(len(ws)):
        for j in range(i, len(ws)):
            t0, t1 = ws[i][0], ws[j][1]
            if t1 - t0 > hi:
                break
            if t1 - t0 < lo:
                continue
            if j + 1 < len(ws) and ws[j + 1][0] - t1 < 0.12 and not re.search(r"[.!?,;]$", ws[j][2]):
                continue                       # would cut mid-phrase
            if i > 0 and t0 - ws[i - 1][1] < 0.12 and not re.search(r"[.!?,;]$", ws[i - 1][2]):
                continue
            text = " ".join(w[2] for w in ws[i:j + 1])
            sc = 2.0 * len(_STAKES.findall(text)) + (1.0 if re.search(r"[.!?]$", ws[j][2]) else 0.0) \
                - 0.6 * abs((t1 - t0) - 1.8)
            out.append((round(sc, 3), t0, t1, text))
    out.sort(key=lambda x: -x[0])
    return out


def _resolve_line(ctx, plan, ln):
    if isinstance(ln, str):
        ln = {"from": ln}
    if ln.get("from") is None:
        return ctx.clip(ln["clip"]), float(ln["in"]), float(ln["out"]), ln.get("text", "")
    sec, gi = _find_section(plan, ln["from"])
    segs = sec.get("segments", [])
    cands = [segs[gi]] if gi is not None and gi < len(segs) else [s for s in segs if s.get("type") == "talk"]
    for seg in cands:
        src = seg if seg.get("type") == "talk" else (seg.get("voiceover") or {})
        if not src.get("clip"):
            continue
        row = ctx.clip(src["clip"])
        a = float(ln.get("in", src["in"]))
        b = float(ln.get("out", src["out"]))
        if ln.get("in") is not None and ln.get("out") is not None:
            return row, a, b, ""
        cs = teaser_candidates(_words(ctx, row["id"]), a, b)
        if cs:
            _, t0, t1, text = cs[0]
            return row, max(0.0, t0 - 0.06), t1 + 0.12, text
        return row, a, min(b, a + 2.0), ""
    raise P.PlanError(f"cold_open line {ln.get('from')!r}: no talk segment there")


def _co_rhythm(ctx, co, total, song, offset):
    """Cut times (seconds from the start of the cold open) and the target time of the first word."""
    fps = ctx.fps
    if co.get("from_reference"):
        from . import reference as R
        an = R.load_analysis(ctx.cfg, co["from_reference"])
        s0 = (an.get("sections") or [{}])[0]
        end0 = float(s0.get("end") or 10.0)
        shots = [s for s in an.get("shots", []) if float(s["start"]) < end0 - 1e-6]
        cuts = [float(s["start"]) for s in shots[1:]]
        asl = float(s0.get("asl") or 0.45)
        t = cuts[-1] if cuts else 0.0
        while t + asl < total - 1e-6 and end0 < total:      # longer than the reference: continue its ASL
            t += asl
            if t > end0:
                cuts.append(t)
        cuts = [c for c in cuts if c < total - 1.5 / fps]
        fw = (an.get("structure") or {}).get("time_to_first_word_s") or 1.4
        basis = f"reference {co['from_reference']} cold open ({len(shots)} shots in {end0:.2f}s)"
    else:
        asl = float(co.get("asl", 0.45))
        pat = (0.9, 1.15, 0.75, 1.3, 1.0, 0.8, 1.2, 0.95)       # varied, never metronomic
        cuts, t, k = [], 0.0, 0
        while True:
            t += asl * pat[k % len(pat)]
            k += 1
            if t >= total - 0.2:
                break
            cuts.append(t)
        beats = np.asarray(song.get("beats") or [], float) if song else np.asarray([])
        if len(beats) > 3:                          # pull cuts onto the half-beat grid when one is close
            half = np.sort(np.concatenate([beats, (beats[1:] + beats[:-1]) / 2])) - offset
            cuts = [float(half[np.argmin(np.abs(half - c))]) if np.min(np.abs(half - c)) < 0.08 else c for c in cuts]
        fw = float(co.get("first_word", 1.4))
        basis = f"auto rhythm ASL {asl:.2f}s" + (" snapped to half beats" if len(beats) > 3 else "")
    cuts = sorted({round(round(c * fps) / fps, 4) for c in cuts if 2.5 / fps < c < total - 2.5 / fps})
    out = []
    for c in cuts:                                   # never a shot shorter than 3 frames
        if not out or c - out[-1] >= 3 / fps - 1e-6:
            out.append(c)
    return out, float(fw), basis


def _co_music(ctx, co, plan, ref_style):
    m = dict(co.get("music") or {})
    if not m.get("song"):
        src = next((s.get("music") for s in plan.get("sections", []) if (s.get("music") or {}).get("song")), None)
        if src:
            m = {"song": src["song"], "offset": float(src.get("offset", 0.0)), **{k: v for k, v in m.items() if k != "song"}}
    song = ctx.song(m["song"]) if m.get("song") else None
    if m:
        base = {"under_db": -10.0, "lift_db": 10.0} if ref_style else {}       # reference teaser: 0.22 / 0.70
        m = {**P.MUSIC_DEFAULTS, "gain_db": -8.0, **base, **m, "mode": m.get("mode", "under")}
    return m, song


class _ShotPool:
    """B-roll windows for the cold open: the edit's own clips at windows the edit does NOT show later
    (talk ranges, cutaways, cover clips, b-roll, montage shots, stills and match-cut frames are all marked
    used), never the same clip twice in a row."""

    def __init__(self, ctx, plan, co, lines):
        self.ctx = ctx
        self.used = {}
        cand = []
        for sec in plan.get("sections", []):
            for seg in sec.get("segments", []):
                t = seg.get("type")
                if t == "talk":
                    self.mark(ctx.clip(seg["clip"])["id"], float(seg["in"]), float(seg["out"]))
                    for c in seg.get("cutaways") or []:
                        cand.append((c["clip"], c.get("in")))
                        if c.get("in") is not None:
                            self.mark(ctx.clip(c["clip"])["id"], float(c["in"]), float(c["in"]) + float(c.get("dur", 2.0)))
                    for c in seg.get("cover") or []:
                        ref = c["clip"] if isinstance(c, dict) else c
                        cin = c.get("in") if isinstance(c, dict) else None
                        cand.append((ref, cin))
                        if cin is not None:      # auto_cover takes ~1.6 s per covered join from here on
                            self.mark(ctx.clip(ref)["id"], float(cin), float(cin) + 3.5)
                elif t == "broll":
                    cand.append((seg["clip"], seg.get("in")))
                    self.mark(ctx.clip(seg["clip"])["id"], float(seg["in"]), float(seg["out"]))
                elif t == "montage":
                    for sh in seg.get("shots", []):
                        cand.append((sh["clip"], sh.get("in")))
                        if sh.get("in") is not None:
                            end = float(sh["out"]) if sh.get("out") is not None else float(sh["in"]) + float(seg.get("max_shot", 1.6))
                            self.mark(ctx.clip(sh["clip"])["id"], float(sh["in"]), end)
                elif t == "still":
                    self.mark(ctx.clip(seg["clip"])["id"], float(seg["t"]) - 1.0, float(seg["t"]) + 1.0)
                elif t == "title" and str(seg.get("bg", "")).startswith("blur:"):
                    ref, _, tt = str(seg["bg"])[5:].rpartition("@")
                    self.mark(ctx.clip(ref)["id"], float(tt or 0) - 1.0, float(tt or 0) + 1.0)
                elif t == "matchcut":
                    for side in ("a", "b"):
                        cand.append((seg[side]["clip"], seg[side].get("t")))
                        self.mark(ctx.clip(seg[side]["clip"])["id"], float(seg[side]["t"]) - 1.5, float(seg[side]["t"]) + 1.5)
        if isinstance(co.get("shots"), list) and co["shots"]:
            cand = [(x["clip"], x.get("in")) for x in co["shots"]]      # explicit picks; the edit's windows stay marked
        for row, a, b, _ in lines:
            self.mark(row["id"], a, b)
        es = co.get("end_shot") or {}
        if es.get("clip") is not None:
            self.mark(ctx.clip(es["clip"])["id"], float(es.get("in", 0.0)), float(es.get("in", 0.0)) + float(es.get("dur", 2.7)))
        seen, self.pool = set(), []
        line_ids = {ln[0]["id"] for ln in lines}
        for ref, a in cand:
            r = ctx.clip(ref)
            if r["id"] in seen or r["id"] in line_ids:
                continue
            seen.add(r["id"])
            self.pool.append({"ref": ref, "row": r, "next": float(a) if a is not None else 0.15 * float(r["duration"] or 1)})
        if not self.pool:
            raise P.PlanError("cold_open: no b-roll to cut the teaser from (add broll/montage segments or cold_open.shots)")
        self.k, self.last = 0, None

    def mark(self, cid, a, b):
        self.used.setdefault(cid, []).append((a, b))

    def free_at(self, p, dur):
        """First source time in pool entry p whose [t, t + dur] window is not used (no marking)."""
        D = float(p["row"]["duration"] or 0)
        t = p["next"]
        for _ in range(400):
            if t + dur > D - 0.05:
                t = 0.1
            if not any(t < b2 + 0.2 and t + dur > a2 - 0.2 for a2, b2 in self.used.get(p["row"]["id"], [])):
                return round(t, 3)
            t += 0.25
        return None

    def take(self, dur):
        for _ in range(len(self.pool)):
            p = self.pool[self.k % len(self.pool)]
            self.k += 1
            if p["row"]["id"] != self.last or len(self.pool) == 1:
                break
        D = float(p["row"]["duration"] or 0)
        t = p["next"]
        for _ in range(400):
            if t + dur > D - 0.05:
                t = 0.1
            if not any(t < b2 + 0.2 and t + dur > a2 - 0.2 for a2, b2 in self.used.get(p["row"]["id"], [])):
                break
            t += 0.25
        self.mark(p["row"]["id"], t, t + dur)
        p["next"] = t + dur + 0.4
        self.last = p["row"]["id"]
        return {"clip": p["ref"], "in": round(t, 3)}


def _ref_frame_spec(fr, co_override=None):
    """A measured frame run (reference devices) -> a plan frame dict (co_override wins)."""
    if co_override:
        return {"kind": (fr or {}).get("kind") or co_override.get("kind", "inset"), **co_override}
    if not fr or fr.get("kind") in (None, "full"):
        return None
    x0, y0, x1, y1 = fr["box"]
    if fr["kind"] == "inset":
        return {"kind": "inset", "w": round(x1 - x0, 4), "h": round(y1 - y0, 4), "x": round(x0, 4), "y": round(y0, 4),
                "radius": 5, "bg": fr.get("color", "#000000")}
    out = {"kind": "gate", "w": round(x1 - x0, 4), "h": round(y1 - y0, 4), "x": round(x0, 4), "y": round(y0, 4),
           "radius": 9, "color": fr.get("color", CD.GATE["color"])}
    inner = fr.get("inner")
    if inner:
        ix0, iy0, ix1, iy1 = inner["box"]
        out["inner"] = {"w": round(ix1 - ix0, 4), "h": round(iy1 - iy0, 4), "radius": 12, "color": inner.get("color", "#000000")}
    return out


def reference_teaser(an, fps):
    """The measured structure of a reference's cold open, from its cut map + devices:
    [{"kind": "card"|"block"|"slot"|"end", "frames": n | [n...], "frame": run, "burn_in": burn | None, ...}].
    A 2-frame burn shot is split back into its two halves (last frame of the shot before it, first frame of
    the shot after it). Returns (elements, info)."""
    dev = an.get("devices") or {}
    s0 = (an.get("sections") or [{}])[0]
    t_end = float(s0.get("end") or (an.get("structure") or {}).get("cold_open_s") or 10.0)
    f_end = int(round(t_end * fps))
    end_b = [int(b["frame"]) for b in dev.get("burns") or [] if abs(int(b["frame"]) - f_end) <= 3]
    if end_b:                                    # the teaser ends on its closing burn's cut
        f_end = end_b[0]
        t_end = f_end / fps
    burns = {int(b["frame"]): b for b in dev.get("burns") or [] if int(b["frame"]) < f_end}
    runs = dev.get("frames") or []
    card = dev.get("card")

    def frame_at(f0, f1):
        best, cov = None, 0
        for r in runs:
            c = min(f1, int(r["f1"])) - max(f0, int(r["f0"]))
            if c > cov:
                best, cov = r, c
        return best
    shots = []
    for s in an.get("shots", []):
        f0, f1 = int(s["f0"]), int(s["f1"])
        if f0 >= f_end:
            break
        if s.get("kind") == "flash" and f1 - f0 == 2:          # burn pair: split between neighbours
            if shots:
                shots[-1]["f1"] += 1
            if f0 + 1 < f_end:
                shots.append({"f0": f0 + 1, "f1": min(f1, f_end), "speech": s.get("speech") or 0.0, "_half": True})
            continue
        f1 = min(f1, f_end)
        if shots and shots[-1].get("_half"):
            h = shots.pop()
            f0 = h["f0"]
        shots.append({"f0": f0, "f1": f1, "speech": s.get("speech") or 0.0})
    els = []
    for s in shots:
        n = s["f1"] - s["f0"]
        if n <= 0:
            continue
        burn = next((b for f, b in burns.items() if abs(f - s["f0"]) <= 1), None)
        fr = frame_at(s["f0"], s["f1"])
        if card and s["f0"] <= int(card["f0"]) + 1 and not els:
            els.append({"kind": "card", "frames": n, "card": card, "burn_in": burn})
            continue
        short = n <= 8
        if short and els and els[-1]["kind"] == "block" and not burn:
            els[-1]["frames"].append(n)
            els[-1]["frame_runs"].append(fr)
            continue
        if short:
            els.append({"kind": "block", "frames": [n], "frame_runs": [fr], "burn_in": burn})
        elif els and els[-1]["kind"] == "slot" and not burn:
            els[-1]["frames"] += n                 # a reframe cut inside one dialogue slot
        else:
            els.append({"kind": "slot", "frames": n, "frame": fr, "burn_in": burn, "speech": s["speech"]})
    if card and els and els[0]["kind"] == "card":   # the card's own length (the cut map may split its exit frame)
        d_ = int(card["f1"]) - els[0]["frames"]
        if d_ and len(els) > 1 and els[1]["kind"] == "block" and els[1]["frames"][0] - d_ >= 3:
            els[0]["frames"] += d_
            els[1]["frames"][0] -= d_
    f = shots[0]["f0"] if shots else 0                 # each element's span on the reference (dynamics match)
    for e in els:
        nf = sum(e["frames"]) if isinstance(e["frames"], list) else int(e["frames"])
        e["t0"], e["t1"] = round(f / fps, 4), round((f + nf) / fps, 4)
        f += nf
    # the end shot: the last long element when a burn leads into it (the "teaser ends" hit)
    for e in reversed(els):
        if e["kind"] == "slot":
            if e.get("burn_in"):
                e["kind"] = "end"
            break
    titles = [t for t in dev.get("titles") or [] if float(t["t0"]) < t_end + 0.5]
    end_burn = next((b for b in dev.get("burns") or [] if abs(int(b["frame"]) - f_end) <= 2), None)
    return els, {"t_end": t_end, "titles": titles, "end_burn": end_burn, "card": card}


def build_cold_open(ctx, plan):
    """plan.cold_open -> a generated section (index -1).
    style "teaser": teaser lines from later in the edit, dense micro-cuts between them (rhythm from the
      reference cut map or ~0.45 s auto), music UNDER the voice; optional card / inset / gate / burns /
      end shot / typed title.
    style "reference": rebuilds the reference's measured cold open element by element: card -> inset strobe
      -> lines on the creator's face for the whole line, in the reference's frame (gate) -> blocks in the
      reference's shot lengths and frames -> burns where the reference has them -> the gate dropping on the
      biggest hit -> the typed title on a held end shot -> a burn into the edit."""
    co = plan.get("cold_open")
    if not co:
        return None
    if co.get("end_shot") == "auto":                 # titleplace.choose_shot picks the held shot (below)
        co = {**co, "end_shot": {"auto": True}}
    ref_style = co.get("style") == "reference"
    lines = [_resolve_line(ctx, plan, ln) for ln in (co.get("lines") or [])]
    m, song = _co_music(ctx, co, plan, ref_style)
    pool = _ShotPool(ctx, plan, co, lines)
    cl = co.get("loudness") or {}
    prog_I = float(((plan.get("style") or {}).get("loudness") or {}).get("I", -14.0))
    ctx.co_boost = float(cl.get("I", -12.0)) - prog_I
    if ref_style:
        sec = _build_reference_teaser(ctx, plan, co, lines, pool)
    else:
        sec = _build_classic_teaser(ctx, plan, co, lines, pool, m, song)
    if m:
        sec["music"] = m
        card = next((k for k, s in enumerate(sec["segments"]) if s.get("type") == "card"), None)
        if ref_style and card == 0 and len(sec["segments"]) > 1 and co.get("card_music", False) is False:
            # the reference card rides its riser alone; the music comes in on the cut out of it
            d = float(sec["segments"][0].get("dur", CD.CARD["dur"]))
            sec["music"] = {**m, "breaks": list(m.get("breaks") or []) + [{"before": "cut:1", "dur": round(min(1.0, d), 3)}]}
    return sec


def _line_seg(ctx, co, row, la, lb, text, frame=None):
    face_s = co.get("face_s")
    seg = {"type": "talk", "clip": row["id"], "in": round(la, 3), "out": round(lb, 3), "tighten": False,
           "lead_in": 0, "tail": 0, "captions": co.get("captions", True), "room_tone": False, "cutaways": [],
           "crop": co.get("crop", "center"), "teaser": True, "note": f"teaser line: {text}"[:120]}
    if frame:
        seg["frame"] = frame
    return seg, (float("inf") if face_s is None else float(face_s))


def _auto_end_shot(ctx, co, pool, dur):
    """cold_open.end_shot "auto": the pool's held shot whose quiet / dark area suits the title best
    (titleplace.choose_shot), at a source window not used anywhere in the edit (repeats rule). Cached on
    disk by the candidates + title, so the timeline resolver and the renderer agree."""
    from . import titleplace as TP
    ti = co.get("title") or {}
    ti = {"text": ti} if isinstance(ti, str) else dict(ti)
    text, size = ti.get("text") or "TITLE", float(ti.get("size", CD.TITLE["size"]))
    color = ti.get("color", CD.TITLE["color"])
    cands = []
    for p in pool.pool:
        t = pool.free_at(p, dur)
        if t is not None:
            cands.append({"clip": p["ref"], "in": t, "path": ctx.source(p["row"])})
    key = _sha(json.dumps([[c["clip"], c["in"]] for c in cands] + [text, size, color, round(dur, 3), "v1"]))
    cp = Path(ctx.cache) / "titlepos" / f"shot_{key}.json"
    res = None
    try:
        res = json.loads(cp.read_text())
    except (OSError, ValueError):
        pass
    if res is None:
        res = TP.choose_shot(ctx.cfg, cands, text, size, color, dur=dur)
        res = {k: v for k, v in res.items() if k != "ranking"} | {"ranking": [
            {k: v for k, v in r.items() if k in ("clip", "in", "ok", "score", "contrast", "skipped")} for r in res.get("ranking") or []]}
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(res))
        except OSError:
            pass
    if not res.get("clip"):
        ctx.warn(f"cold_open.end_shot auto: {res.get('why')}; taking the next pool shot")
        return {}
    pool.mark(ctx.clip(res["clip"])["id"], float(res["in"]), float(res["in"]) + dur)
    ctx.end_shot_choice = res
    return {"clip": res["clip"], "in": float(res["in"]), "dur": dur, "title_pos": {"x": res["x"], "y": res["y"]}}


def _title_pos(ctx, seg, o, a, b):
    """typed_title "pos": {x, y} as given, or "auto" measured on the segment's clip span [a, b] (source s).
    Returns {x, y, backing?, why} or None (default centre line). Cached on disk per clip span + title."""
    pos = o.get("pos")
    if isinstance(pos, dict) and pos.get("x") is not None and pos.get("y") is not None:
        return {"x": float(pos["x"]), "y": float(pos["y"]), "why": "given in the plan"}
    if pos != "auto":
        return None
    if not seg.get("clip"):
        ctx.warn(f"typed_title '{o.get('text', '')}': pos auto needs a clip under it; default position used")
        return None
    from . import titleplace as TP
    row = ctx.clip(seg["clip"])
    zoom = float((seg.get("push") or {}).get("to", 1.0)) if isinstance(seg.get("push"), dict) else 1.0
    size, color = float(o.get("size", CD.TITLE["size"])), o.get("color", CD.TITLE["color"])
    key = _sha(json.dumps([row["id"], round(a, 3), round(b, 3), o.get("text"), size, color, o.get("font"), zoom,
                           ctx.W, ctx.H, "v1"]))
    cp = Path(ctx.cache) / "titlepos" / f"pos_{key}.json"
    try:
        return json.loads(cp.read_text())
    except (OSError, ValueError):
        pass
    r = TP.place(ctx.cfg, ctx.source(row), a, b, o.get("text") or "", size, color, o.get("font") or "",
                 (ctx.W, ctx.H), zoom)
    out = {"x": r["x"], "y": r["y"], "backing": bool(r.get("backing")), "why": "auto: " + str(r.get("why"))}
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(out))
    except OSError:
        pass
    return out


def _end_shot_seg(ctx, co, pool, dur, title_meas=None):
    es = dict(co.get("end_shot") or {})
    if es.get("auto") and es.get("clip") is None:
        es.update(_auto_end_shot(ctx, co, pool, float(es.get("dur", dur))))
    if es.get("clip") is None:
        es.update(pool.take(dur))
    seg = {"type": "broll", "clip": es["clip"], "in": float(es.get("in", 0.0)),
           "out": float(es.get("in", 0.0)) + float(es.get("dur", dur)), "audio": es.get("audio", "natural"),
           "teaser": True, "frame": "full", "note": "cold-open end shot (creator alone, held)"}
    if es.get("push", {"to": 1.08, "ease": "linear"}):
        seg["push"] = es.get("push", {"to": 1.08, "ease": "linear"})
    ti = co.get("title")
    if ti:
        ti = {"text": ti} if isinstance(ti, str) else dict(ti)
        meas = title_meas or {}
        o = {"kind": "typed_title", "text": ti["text"], "at": 0.0, "dur": float(seg["out"] - seg["in"]),
             "start": float(ti.get("start", meas.get("start", 1.12))),
             "char_s": float(ti.get("char_s", meas.get("char_s", CD.TITLE["char_s"]))),
             "color": ti.get("color", meas.get("color", CD.TITLE["color"])),
             "size": float(ti.get("size", meas.get("size", CD.TITLE["size"]))),
             **({"font": ti["font"]} if ti.get("font") else {}),
             **({"pos": es["title_pos"] if ti.get("pos") == "auto" and es.get("title_pos") else ti["pos"]}
                if ti.get("pos") else {}),
             **({"sfx": ti.get("sfx", "hiss")} if ti.get("sfx", "hiss") else {})}
        seg["overlays"] = [o]
    return seg


def _burn(co, strength=1.0, gain_db=-4.0):
    b = co.get("burns", True)
    if not b:
        return None
    base = b if isinstance(b, dict) else {}
    return {"kind": "burn", "frames": 2, "strength": strength, "gain_db": gain_db, **base}


def _block_seg(pool, frames_list, fps, frames=None, tick=True):
    shots = []
    for k, nf in enumerate(frames_list):
        d = max(3, int(nf)) / fps
        sh = {**pool.take(d), "dur": round(d, 4)}
        fr = frames[k] if frames else None
        sh["frame"] = fr if fr else "full"
        shots.append(sh)
    seg = {"type": "montage", "beat_sync": False, "min_shot": 0.04, "max_shot": 30, "audio": "music",
           "shots": shots, "teaser": True, "note": "cold-open strobe"}
    if tick:   # reference: a high-band transient on every strobe image
        at, ev = 0.0, []
        for sh in shots:
            ev.append({"name": "tick", "at": round(at, 4), "gain_db": -20})
            at += sh["dur"]
        seg["sfx"] = ev
    return seg


def _build_reference_teaser(ctx, plan, co, lines, pool):
    from . import reference as R_
    fps = ctx.fps
    an = R_.load_analysis(ctx.cfg, co["from_reference"])
    if not (an.get("devices") or {}).get("version"):
        try:
            an = R_.ensure_devices(ctx.cfg, co["from_reference"])
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"cold_open: reference devices not measured ({e}); using the measured inventory defaults")
    els, info = reference_teaser(an, fps)
    ctx.reference_info = {"id": co["from_reference"], "devices": sorted(set(R_.device_kinds(an))),
                          "card": bool(info.get("card")), "teaser_s": info["t_end"]}
    segs = []
    li = 0
    card_spec = co.get("card", True)
    inset_o = co.get("inset") if isinstance(co.get("inset"), dict) else None
    gate_o = co.get("gate") if isinstance(co.get("gate"), dict) else None
    title_meas = None
    if info["titles"]:
        t = info["titles"][0]
        title_meas = {k: t[k] for k in ("color", "size") if t.get(k)}
        ty = t.get("type_on") or {}
        if ty.get("start_s") is not None:
            title_meas["start"] = float(ty["start_s"])
        if ty.get("char_s"):
            title_meas["char_s"] = float(ty["char_s"])
    dyn_on = bool(dynamics_spec(co))
    prev_e, n_before = None, 0

    def tag_prev():             # dynamics match: the element's last segment follows that element's span
        if dyn_on and prev_e is not None and len(segs) > n_before:
            segs[-1]["_ref_span"] = [prev_e["t0"], prev_e["t1"]]
    for e in els:
        tag_prev()
        prev_e, n_before = e, len(segs)
        burn = _burn(co, strength=0.3 if (segs and segs[-1]["type"] == "card") else 1.0) if e.get("burn_in") else None
        if e["kind"] == "card":
            if card_spec is False:
                continue
            cs = dict(card_spec) if isinstance(card_spec, dict) else {}
            meas = e["card"] or {}
            seg = {"type": "card", "text": cs.get("text", meas.get("text") or CD.CARD["text"]),
                   "dur": round(e["frames"] / fps, 4), "teaser": True,
                   **{k: cs.get(k, meas.get(k)) for k in ("color", "scale_from", "scale_frames") if cs.get(k, meas.get(k)) is not None},
                   **{k: cs[k] for k in ("font", "size", "boil", "sfx") if k in cs}}
            segs.append(seg)
        elif e["kind"] == "block":
            frs = [_ref_frame_spec(r, inset_o if (r or {}).get("kind") == "inset" else (gate_o if (r or {}).get("kind") == "gate" else None))
                   for r in e["frame_runs"]]
            seg = _block_seg(pool, e["frames"], fps, frs, tick=co.get("ticks", True))
            if burn:
                seg["transition"] = burn
            segs.append(seg)
        elif e["kind"] == "slot":
            if li >= len(lines):
                continue
            row, la, lb, text = lines[li]
            li += 1
            fr = _ref_frame_spec(e.get("frame"), gate_o if (e.get("frame") or {}).get("kind") == "gate" else None)
            seg, _ = _line_seg(ctx, co, row, la, lb, text, fr)
            if burn:
                seg["transition"] = burn
            segs.append(seg)
        elif e["kind"] == "end":
            while li < len(lines):           # lines the reference had no slot for go before the end shot
                row, la, lb, text = lines[li]
                li += 1
                seg, _ = _line_seg(ctx, co, row, la, lb, text, _ref_frame_spec({"kind": "gate", "box": [CD.GATE["x"], CD.GATE["y"],
                                   CD.GATE["x"] + CD.GATE["w"], CD.GATE["y"] + CD.GATE["h"]]}, gate_o))
                segs.append(seg)
            seg = _end_shot_seg(ctx, co, pool, float((co.get("end_shot") or {}).get("dur", e["frames"] / fps)), title_meas)
            b = _burn(co, strength=1.0, gain_db=-1.0)          # the biggest hit of the intro
            if b and e.get("burn_in"):
                seg["transition"] = b
            segs.append(seg)
    tag_prev()
    sec = {"name": co.get("name", "cold open"), "segments": segs, "_generated": "cold_open",
           "_basis": f"reference {co['from_reference']} cold open, element by element ({len(els)} elements)",
           "_cuts": [], "_dur": info["t_end"], "frame": None}
    if co.get("end_flash"):
        sec["_end_transition"] = {"kind": "flash", "frames": 4}
    elif info.get("end_burn") and co.get("burns", True) is not False:
        sec["_end_transition"] = _burn(co, strength=0.9)
    return sec


def _build_classic_teaser(ctx, plan, co, lines, pool, m, song):
    fps = ctx.fps
    total = float(co.get("dur", 10.0))
    if co.get("from_reference") and co.get("dur") is None:
        from . import reference as R_
        an = R_.load_analysis(ctx.cfg, co["from_reference"])
        total = float(((an.get("sections") or [{}])[0]).get("end") or total)
    cuts, first_word, basis = _co_rhythm(ctx, co, total, song, float(m.get("offset", 0.0)) if m else 0.0)
    card = co.get("card")
    card_d = 0.0
    if card:
        card_d = round(round(float((card if isinstance(card, dict) else {}).get("dur", CD.CARD["dur"])) * fps) / fps, 4)
    es = co.get("end_shot")
    end_d = float(es.get("dur", 2.7)) if isinstance(es, dict) else 0.0
    body_end = total - end_d
    cuts = [c for c in cuts if card_d + 2.5 / fps < c < body_end - 2.5 / fps]
    inset = co.get("inset")
    inset_fr = (dict(inset, kind="inset") if isinstance(inset, dict) else {"kind": "inset"}) if inset else None
    gate = co.get("gate")
    gate_fr = (dict(gate, kind="gate") if isinstance(gate, dict) else {"kind": "gate"}) if gate else None

    def pieces(a, b):
        bounds = [a] + [c for c in cuts if a + 2.5 / fps < c < b - 2.5 / fps] + [b]
        return [(x, y) for x, y in zip(bounds, bounds[1:]) if y - x > 0.5 / fps]

    def snap(t):
        near = [c for c in cuts if abs(c - t) < 0.35]
        return min(near, key=lambda c: abs(c - t)) if near else round(t * fps) / fps
    segs = []
    if card:
        segs.append({"type": "card", "dur": card_d, "teaser": True, **(card if isinstance(card, dict) else {})})
    sum_lines = sum(b - a for _, a, b, _ in lines)
    first_end = snap(max(card_d + 3 / fps, min(first_word, max(card_d + 0.4, body_end - sum_lines))))
    k = len(lines)
    rest = max(0.0, body_end - first_end - sum_lines)
    gap = rest / max(1, k) if k else rest
    t = first_end
    order = []
    for i, (row, a, b, text) in enumerate(lines):
        order.append(("line", i, t, t + (b - a)))
        t += b - a
        if i < k - 1 and gap >= 3 / fps:
            e = snap(t + gap)
            if e > t + 2.5 / fps:
                order.append(("block", i + 1, t, e))
                t = e
    if body_end - t > 2.5 / fps:
        order.append(("block", k, t, body_end))
    if first_end - card_d > 2.5 / fps:
        order.insert(0, ("block", 0, card_d, first_end))
    use_burns = bool(co.get("burns"))
    flash_next = bool(card) and use_burns
    for kind, i, a, b in order:
        if kind == "block":
            shots = [{**pool.take(y - x), "dur": round(y - x, 4), **({"frame": inset_fr} if inset_fr else {})}
                     for x, y in pieces(a, b)]
            if not shots:
                continue
            seg = {"type": "montage", "beat_sync": False, "min_shot": 0.04, "max_shot": 30, "audio": "music",
                   "shots": shots, "teaser": True, "note": "cold-open micro-cuts"}
            if flash_next or (i == 1 and co.get("flashes", "auto") == "auto"):
                seg["transition"] = _burn(co, 0.55 if flash_next else 1.0) if use_burns else {"kind": "flash", "frames": 4}
                flash_next = False
            segs.append(seg)
        else:
            row, la, lb, text = lines[i]
            seg, face_s = _line_seg(ctx, co, row, la, lb, text, gate_fr)
            seg["cutaways"] = [{**pool.take(y - x), "at": round(x - a, 4), "dur": round(y - x, 4)}
                               for x, y in pieces(a, b) if x >= a + face_s - 1e-6]
            segs.append(seg)
    if es:
        seg = _end_shot_seg(ctx, co, pool, end_d)
        if use_burns:
            seg["transition"] = _burn(co, 1.0, -1.0)
        segs.append(seg)
    sec = {"name": co.get("name", "cold open"), "segments": segs, "_generated": "cold_open",
           "_basis": basis, "_cuts": cuts, "_dur": total}
    if co.get("end_flash", False):
        sec["_end_transition"] = {"kind": "flash", "frames": 4}
    elif use_burns and es:
        sec["_end_transition"] = _burn(co, 0.9)
    return sec


# ============================================================================== expansion
class Timeline:
    def __init__(self, fps):
        self.fps = fps
        self.F = 0
        self.atoms = []

    @property
    def t(self):
        return self.F / self.fps

    def add(self, atom, dur=None, end_t=None):
        f1 = int(round(end_t * self.fps)) if end_t is not None else self.F + int(round(dur * self.fps))
        f1 = max(f1, self.F + 1)
        atom.update(i=len(self.atoms), f0=self.F, f1=f1, n=f1 - self.F)
        self.atoms.append(atom)
        self.F = f1
        return atom


def _words(ctx, cid):
    from . import asr
    try:
        return asr.words(ctx.cfg, cid)
    except Exception as e:  # noqa: BLE001
        ctx.warn(f"no transcript words for {cid}: {e}")
        return []


def _tighten(ctx, cid, a, b, gap):
    from . import asr
    w = _words(ctx, cid)
    if not w:
        return [(a, b)]
    return asr.tighten(w, a, b, gap=gap)


def _base_atom(ctx, sec_i, sec, seg_i, seg, kind, row=None):
    at = {"sec": sec_i, "section": sec.get("name", f"section {sec_i}"), "seg": seg_i, "type": seg["type"],
          "kind": kind, "clip": row, "src": ctx.source(row) if row else None, "a": 0.0, "speed": 1.0,
          "crop": seg.get("crop", "center"), "zoom": seg.get("zoom", 1.0), "fit": seg.get("fit", "cover"),
          "align": None, "stabilize": bool(seg.get("stabilize")), "audio": {"mode": "none"},
          "captions": [], "overlays": [], "cutaways": [], "unit": None,
          "look": LK.effective(ctx.style["grade"].get("look"), seg.get("look")),
          "exposure_stops": seg.get("exposure_stops"), "conform": "none",
          "frame": CD.frame_spec(seg["frame"] if "frame" in seg else sec.get("frame"))}
    return at


def _conform(ctx, row, src, seg, sync):
    """Frame-rate conform for a source that is not at the output rate: (mode, speed factor).
    auto: within 5 % of the output rate, b-roll is sped up (23.976 -> 25 = +4.3 %, every source frame
    shown once; natural audio is atempo'd, pitch kept) and sync talk is frame-blended (lip-sync holds);
    anything else goes through fps (50 -> 25 drops every other frame, no duplicates)."""
    mode = (seg or {}).get("conform") or ctx.style.get("conform") or "auto"
    q = ctx.src_fps(row, src) if row else None
    if not q:
        return "none", 1.0
    f = q[0] / q[1]
    if abs(f - ctx.fps) < 1e-3:
        return "none", 1.0
    if mode == "auto":
        mode = ("blend" if sync else "speed") if abs(f - ctx.fps) / ctx.fps < 0.05 else "dup"
    if mode == "speed":
        return "speed", ctx.fps * q[1] / q[0]
    return mode, 1.0


def _pace_durations(pace, n, base_asl=None):
    """Planned shot lengths for a montage from section.pace: ASL, alternating fast / slow blocks."""
    asl = float(pace.get("asl") or base_asl or 0.8)
    if pace.get("alternate", True):
        pat = np.array([0.7, 0.7, 0.7, 1.45, 1.45])
        pat = pat / pat.mean()
        d = [asl * pat[k % len(pat)] for k in range(n)]
    else:
        d = [asl] * n
    mh = pace.get("max_hold")
    return [min(x, float(mh)) if mh else x for x in d]


def _push_spec(seg, ctx, kind):
    pu = seg.get("push")
    if pu is None and kind == "talk" and ctx.style.get("auto_push"):
        ap = ctx.style["auto_push"]
        return {"to": float(ap.get("to", 1.04)), "ease": "inout", "auto": True, "min_hold": float(ap.get("min_hold", 4.0))}
    if not pu:
        return None
    if not isinstance(pu, dict):
        pu = {"to": float(pu)}
    return {"to": float(pu.get("to", 1.06)), "ease": pu.get("ease", "inout"),
            **({"x": float(pu["x"])} if pu.get("x") is not None else {}),
            **({"y": float(pu["y"])} if pu.get("y") is not None else {})}


def _seg_transition(sec, gi, seg):
    tr = seg.get("transition")
    if tr is None and gi == 0:
        tr = sec.get("transition")
    sp = P.transition_spec(tr)
    return sp if sp and sp["kind"] != "cut" and sp["frames_out"] + sp["frames_in"] > 0 else None


VISUAL_PREV = ("broll", "montage", "matchcut", "still", "card")


def expand(ctx, sections):
    """Flat atom list on the frame grid + per-segment mapping info + section spans."""
    tl = Timeline(ctx.fps)
    fps = ctx.fps
    segs, secs = [], []
    cs = ctx.style["captions"]
    aud = ctx.style.get("audio") or {}
    ctx.transitions = []
    for si, sec in sections:
        s_start = tl.t
        mus = sec.get("music") or {}
        sng = ctx.song(mus["song"]) if mus.get("song") else None
        off = float(mus.get("offset", 0.0) or 0.0)
        beats = np.asarray(sng["beats"], float) if sng else np.asarray([])
        pace = sec.get("pace") or {}
        teaser_sec = sec.get("_generated") == "cold_open"

        def song_t(t):                      # timeline -> song time inside this section
            return off + (t - s_start)

        def tl_t(st):
            return s_start + (st - off)
        sec_rec = {"index": si, "name": sec.get("name", f"section {si}"), "start": s_start, "music": None,
                   "montage_cuts": [], "beats_tl": [float(tl_t(b)) for b in beats] if sng else [],
                   "generated": sec.get("_generated")}
        seglist = sec.get("segments", [])
        for gi, seg in enumerate(seglist):
            t = seg["type"]
            rec = {"sec": si, "seg": gi, "type": t, "spec": seg, "atoms": [], "src_map": [],
                   "transition": _seg_transition(sec, gi, seg), "teaser": bool(seg.get("teaser") or teaser_sec),
                   "sec_pace": pace}
            prev_rec = segs[-1] if segs else None
            T0 = tl.t
            F0 = tl.F
            if t == "talk":
                row = ctx.clip(seg["clip"])
                pieces = _tighten(ctx, row["id"], float(seg["in"]), float(seg["out"]), float(seg.get("gap", 0.35))) \
                    if seg.get("tighten", True) else [(float(seg["in"]), float(seg["out"]))]
                unit = ("talk", si, gi)
                cmode, _ = _conform(ctx, row, ctx.source(row), seg, sync=True)
                # J / L cuts: the voice starts under the previous shot / runs on under the next one
                lead = seg.get("lead_in")
                if lead is None:
                    ok = (prev_rec is not None and prev_rec["type"] in VISUAL_PREV and not rec["teaser"]
                          and not prev_rec.get("teaser") and rec["transition"] is None)
                    lead = float(aud.get("auto_lead_in", 0.3)) if ok else 0.0
                tail = seg.get("tail")
                if tail is None:
                    nxt = seglist[gi + 1] if gi + 1 < len(seglist) else None
                    ok = nxt is not None and nxt.get("type") in ("broll", "montage", "matchcut") and not rec["teaser"] \
                        and _seg_transition(sec, gi + 1, nxt) is None
                    tail = float(aud.get("auto_tail", 0.0)) if ok else 0.0
                if tl.atoms == []:
                    lead = 0.0
                # the picture never moves: the voice's audio window reaches `lead` s before the piece into
                # the source (it plays under the previous shot) and `tail` s past its end (under the next)
                prev_n = tl.atoms[-1]["n"] / fps if tl.atoms else 0.0
                lead = round(max(0.0, min(float(lead), pieces[0][0], max(0.0, prev_n - 2.0 / fps))) * fps) / fps
                tail = round(max(0.0, min(float(tail), 2.0)) * fps) / fps
                rec["lead_in"], rec["tail"] = lead, tail
                for k, (a, b) in enumerate(pieces):
                    pre = lead if k == 0 else 0.0
                    post = tail if k == len(pieces) - 1 else 0.0
                    at = _base_atom(ctx, si, sec, gi, seg, "clip", row)
                    at.update(a=a, unit=unit, conform=cmode,
                              audio={"mode": "voice", "clip": row, "a": a, "pre": pre, "post": post,
                                     "fi": 0.05 if pre else 0.012, "fo": 0.08 if post else 0.015})
                    tl.add(at, dur=b - a)
                    rec["src_map"].append((a, a + at["n"] / fps, at["f0"] / fps))
                    rec["atoms"].append(at["i"])
                    if cs.get("enabled", True) and seg.get("captions", True):
                        at["cue_src"] = (row["id"], a - pre, b + post)
                        at["cue_shift"] = pre
            elif t == "broll":
                row = ctx.clip(seg["clip"])
                at = _base_atom(ctx, si, sec, gi, seg, "clip", row)
                sp = float(seg.get("speed", 1.0) or 1.0)
                d = (float(seg["out"]) - float(seg["in"])) / sp
                vo = seg.get("voiceover")
                cmode, cf = _conform(ctx, row, ctx.source(row), seg, sync=False)
                if vo:
                    vrow = ctx.clip(vo["clip"])
                    vd = float(vo["out"]) - float(vo["in"])
                    if vd > d:
                        sp = max(0.5, sp * d / vd)
                    d = vd
                    at["audio"] = {"mode": "voice", "clip": vrow, "a": float(vo["in"])}
                    if cs.get("enabled", True) and seg.get("captions", True):
                        at["cue_src"] = (vrow["id"], float(vo["in"]), float(vo["out"]))
                        at["cue_shift"] = 0.0
                    at["vo_unit"] = ("vo", si, gi)
                    sp *= cf
                else:
                    sp *= cf
                    mode = seg.get("audio", "natural")
                    at["audio"] = {"mode": mode if mode in ("natural", "mix") else "none", "clip": row,
                                   "a": float(seg["in"]), "speed": sp}
                at.update(a=float(seg["in"]), speed=sp, unit=("seg", si, gi), conform=cmode)
                if seg.get("ramp"):
                    at["ramp"] = {"to": float(seg["ramp"]["to"]), "over": float(seg["ramp"].get("over", 0.4))}
                tl.add(at, dur=d)
                rec["atoms"].append(at["i"])
            elif t == "montage":
                shots = seg["shots"]
                mode = seg.get("audio", "music")
                use_beats = bool(seg.get("beat_sync", True)) and len(beats) > 3
                if seg.get("beat_sync", True) and not use_beats:
                    ctx.warn(f"section '{sec_rec['name']}' montage {gi}: beat_sync needs section music with beats; "
                             "using planned shot lengths")
                pc = {**pace, **(seg.get("pace") or {})}
                ends = None
                if use_beats:
                    from . import music
                    if pc.get("asl"):
                        durs = music.snap_cuts(beats, song_t(tl.t), _pace_durations(pc, len(shots)),
                                               min_shot=float(seg.get("min_shot", 0.4)),
                                               max_shot=float(pc.get("max_hold") or seg.get("max_shot") or 0) or None)
                    else:
                        step = float(np.median(np.diff(beats)))
                        bpc = max(1, int(seg.get("beats_per_cut", 1) or 1))
                        while step * bpc < float(seg.get("min_shot", 0.4)) - 1e-6:
                            bpc += 1
                        durs = music.montage_grid(beats, song_t(tl.t), len(shots), bpc)
                    acc, ends = song_t(tl.t), []
                    for d in durs:
                        acc += d
                        ends.append(tl_t(acc))
                paced = _pace_durations(pc, len(shots)) if (pc.get("asl") and not use_beats) else None
                for k, sh in enumerate(shots):
                    row = ctx.clip(sh["clip"])
                    at = _base_atom(ctx, si, sec, gi, {**seg, **{x: sh[x] for x in ("crop", "zoom", "fit", "stabilize", "frame")
                                                                 if x in sh}}, "clip", row)
                    want = (ends[k] - tl.t) if ends else None
                    if want is None:
                        if sh.get("dur") is not None:
                            want = float(sh["dur"])
                        elif paced:
                            want = paced[k]
                        else:
                            avail = (float(sh["out"]) - float(sh["in"])) if sh.get("out") is not None and sh.get("in") is not None \
                                else float(seg.get("max_shot", 1.6))
                            want = min(max(avail, float(seg.get("min_shot", 0.4))), float(seg.get("max_shot", 1.6)))
                    a = sh.get("in")
                    if a is None:
                        from . import analyze
                        a = analyze.steadiest_window(ctx.con, row["id"], want)[0]
                    a = float(a)
                    out = float(sh["out"]) if sh.get("out") is not None else float(row["duration"] or a + want)
                    avail = max(0.04, out - a)
                    sp = float(sh.get("speed", 1.0) or 1.0)
                    cmode, cf = _conform(ctx, row, ctx.source(row), {**seg, **sh}, sync=False)
                    if avail < want * sp * cf:
                        sp = max(0.5, avail / want / cf)
                    sp *= cf
                    at.update(a=a, speed=sp, unit=("shot", si, gi, k), conform=cmode,
                              audio={"mode": mode if mode in ("natural", "mix") else "none", "clip": row, "a": a, "speed": sp})
                    rp = sh.get("ramp") or seg.get("ramp")
                    if rp:
                        at["ramp"] = {"to": float(rp["to"]), "over": float(rp.get("over", 0.4))}
                    pu = _push_spec(sh, ctx, "shot") or _push_spec(seg, ctx, "montage")
                    if pu:
                        at["push_spec"] = pu
                    if ends:
                        tl.add(at, end_t=ends[k])
                        sec_rec["montage_cuts"].append(round(at["f1"] / fps, 4))
                    else:
                        tl.add(at, dur=want)
                    rec["atoms"].append(at["i"])
            elif t == "matchcut":
                A, B = seg["a"], seg["b"]
                ra, rb = ctx.clip(A["clip"]), ctx.clip(B["clip"])
                pre, post = float(seg.get("pre", 0.8)), float(seg.get("post", 1.4))
                if len(beats) > 3 and seg.get("beat_sync", True):
                    from . import music
                    pre, post = music.snap_cuts(beats, song_t(tl.t), [pre, post], min_shot=0.4)
                pre = min(pre, float(A["t"]))
                n_pre = max(1, int(round(pre * fps)))
                mode = seg.get("audio", "music")
                aa = _base_atom(ctx, si, sec, gi, {**seg, **{x: A[x] for x in ("crop", "zoom") if x in A}}, "clip", ra)
                a_in = float(A["t"]) - n_pre / fps                 # lock end: last frame is the match frame
                ca, _ = _conform(ctx, ra, ctx.source(ra), seg, sync=True)    # blend keeps the match frame exact
                aa.update(a=a_in, unit=("mcA", si, gi), conform=ca,
                          audio={"mode": mode if mode in ("natural", "mix") else "none", "clip": ra, "a": a_in})
                tl.add(aa, dur=n_pre / fps)
                bb = _base_atom(ctx, si, sec, gi, {**seg, **{x: B[x] for x in ("crop", "zoom") if x in B}}, "clip", rb)
                cb, _ = _conform(ctx, rb, ctx.source(rb), seg, sync=True)
                bb.update(a=float(B["t"]), align=B.get("align"), unit=("mcB", si, gi), conform=cb,   # lock start
                          audio={"mode": mode if mode in ("natural", "mix") else "none", "clip": rb, "a": float(B["t"])})
                tl.add(bb, dur=post)
                rec["atoms"] += [aa["i"], bb["i"]]
                rec["match_t"] = round(bb["f0"] / fps, 4)
            elif t == "title":
                at = _base_atom(ctx, si, sec, gi, seg, "title")
                bg = seg.get("bg", "black") or "black"
                at["title"] = {"text": seg["text"], "sub": seg.get("sub", ""), "bg": bg}
                if bg.startswith("blur:"):
                    ref, _, tt = bg[5:].rpartition("@")
                    row = ctx.clip(ref)
                    at.update(clip=row, src=ctx.source(row), a=float(tt or 0), unit=("seg", si, gi))
                tl.add(at, dur=float(seg.get("dur", 2.0)))
                rec["atoms"].append(at["i"])
            elif t == "black":
                at = _base_atom(ctx, si, sec, gi, seg, "black")
                tl.add(at, dur=float(seg.get("dur", 1.0)))
                rec["atoms"].append(at["i"])
            elif t == "card":
                at = _base_atom(ctx, si, sec, gi, seg, "card")
                at["card"] = CD.card_spec({k: seg.get(k) for k in ("text", "color", "size", "font", "scale_from",
                                                                    "scale_frames", "boil", "bg_luma", "y", "soft",
                                                                    "tracking", "max_w")})
                at["frame"] = None
                tl.add(at, dur=float(seg.get("dur", CD.CARD["dur"])))
                rec["atoms"].append(at["i"])
            elif t == "still":
                row = ctx.clip(seg["clip"])
                at = _base_atom(ctx, si, sec, gi, seg, "still", row)
                at.update(a=float(seg["t"]), still_zoom=float(seg.get("zoom", 1.08)), unit=("seg", si, gi))
                tl.add(at, dur=float(seg.get("dur", 2.0)))
                rec["atoms"].append(at["i"])
            rec["t0"], rec["t1"] = T0, tl.t
            rec["f0"], rec["f1"] = F0, tl.F
            # push-in: continuous across the whole segment on talk / broll (montage shots push one by one)
            pu = _push_spec(seg, ctx, t) if t in ("talk", "broll") else None
            if pu and pu.get("auto") and (rec["t1"] - rec["t0"]) < pu["min_hold"]:
                pu = None
            for i in rec["atoms"]:
                at = tl.atoms[i]
                ps = pu or at.pop("push_spec", None)
                if ps:
                    whole = pu is not None
                    at["push"] = {**ps, "off_f": (at["f0"] - F0) if whole else 0,
                                  "tot_f": (tl.F - F0) if whole else at["n"]}
            for i in rec["atoms"]:
                tl.atoms[i]["teaser"] = rec["teaser"]
            segs.append(rec)
        sec_rec["end"] = tl.t
        if sng:
            sec_rec["music"] = {"song": sng["path"], "title": sng.get("title"), "offset": off,
                                "gain_db": float(mus.get("gain_db", DEFAULT_MUSIC_GAIN)),
                                "duck_db": float(mus.get("duck_db", -18)),
                                "mode": mus.get("mode") or ctx.style.get("music_under_speech", "mute"),
                                "start": mus.get("start", "cut"), "end": mus.get("end", "cut"),
                                "montage_gain_db": float(mus.get("montage_gain_db", P.MUSIC_DEFAULTS["montage_gain_db"])),
                                "natural_duck_db": float(mus.get("natural_duck_db", 0.0) or 0.0),
                                "under_db": float(mus.get("under_db", -5.0)),
                                "under_lift_db": float(mus.get("lift_db", 0.0) or 0.0) if (mus.get("mode") == "under") else 0.0,
                                "breaks": list(mus.get("breaks") or []),
                                "fade_in": float(mus.get("fade_in", 0.3)), "fade_out": float(mus.get("fade_out", 0.6))}
            for r in segs:
                if r["sec"] == si:
                    for i in r["atoms"]:
                        tl.atoms[i]["under_music"] = True
        if sec.get("_end_transition"):
            sec_rec["end_transition"] = P.transition_spec(sec["_end_transition"])
        secs.append(sec_rec)
    _finish_expand(ctx, tl, segs, secs)
    return tl, segs, secs


def _finish_expand(ctx, tl, segs, secs):
    """Post-pass over the finished frame grid: transitions on cut frames, crossfade handles at every
    audio join, music breaks before flashes."""
    fps = ctx.fps
    aud = ctx.style.get("audio") or {}
    # a generated cold open hands its closing flash to whatever comes next
    for k, r in enumerate(segs):
        if k and r["transition"] is None:
            prev_sec = next((s for s in secs if s["index"] == segs[k - 1]["sec"]), None)
            if prev_sec and prev_sec.get("end_transition") and r["sec"] != segs[k - 1]["sec"]:
                r["transition"] = prev_sec["end_transition"]
    for r in segs:
        sp = r["transition"]
        if not sp or not r["atoms"]:
            continue
        first = tl.atoms[r["atoms"][0]]
        if first["i"] == 0:
            continue
        prev = tl.atoms[first["i"] - 1]
        sp = dict(sp)
        if sp["kind"] == "burn":
            sp.setdefault("seed", int(first["f0"]))         # each burn differs, the same one on every render
        sp["frames_out"] = min(sp["frames_out"], max(0, prev["n"] - 1))
        sp["frames_in"] = min(sp["frames_in"], max(0, first["n"] - 1))
        first["tr_in"], prev["tr_out"] = sp, sp
        ctx.transitions.append({**sp, "t": round(first["f0"] / fps, 4), "frame": first["f0"], "sec": r["sec"], "seg": r["seg"]})
    # equal-power crossfade handles wherever two pieces of the same kind of sound meet
    h = max(0.0, float(aud.get("join_xfade_s", 0.02))) / 2

    def fam(at):
        m = at["audio"]["mode"]
        return "voice" if m == "voice" else ("amb" if m in ("natural", "mix") else None)
    ctx.talk_joins = []
    for A, B in zip(tl.atoms, tl.atoms[1:]):
        fa, fb = fam(A), fam(B)
        if not fa or fa != fb:
            continue
        if fa == "voice" and A["type"] == "talk" and B["type"] == "talk":
            ctx.talk_joins.append(round(B["f0"] / fps, 4))
        if h <= 0:
            continue
        ua, ub = A["audio"], B["audio"]
        if not ua.get("post"):
            ua["post"], ua["fo"], ua["curve_out"] = h, 2 * h, "qsin"
        hb = min(h, max(0.0, float(ub["a"]) / float(ub.get("speed", 1.0) or 1.0)))
        if not ub.get("pre") and hb > 0:
            ub["pre"], ub["fi"], ub["curve_in"] = hb, 2 * hb, "qsin"
    # music breaks: silence before a flash / whip so the hit lands into nothing
    for tr in ctx.transitions:
        if tr["break"] <= 0:
            continue
        for s in secs:
            if s["music"] and s["start"] + 1e-6 < tr["t"] <= s["end"] + 1e-6:
                s["music"].setdefault("_breaks", []).append((tr["t"], tr["break"]))
    for s in secs:
        m = s["music"]
        if not m:
            continue
        for b in m.get("breaks") or []:
            if b.get("at") is not None:
                t = s["start"] + float(b["at"])
            else:
                ref = str(b["before"])
                gi = int(ref.split(":")[-1]) if ref.split(":")[-1].isdigit() else None
                r = next((x for x in segs if x["sec"] == s["index"] and x["seg"] == gi), None)
                if r is None:
                    continue
                t = r["t0"]
            m.setdefault("_breaks", []).append((round(t, 4), float(b.get("dur", 0.3))))

# ============================================================================== layers on atoms
def src_to_out(rec, s):
    """Map source time inside a talk segment to timeline time (a time in a removed pause maps to the
    start of the next piece)."""
    for a, b, T in rec["src_map"]:
        if s < a:
            return T
        if s <= b:
            return T + (s - a)
    a, b, T = rec["src_map"][-1]
    return T + (b - a)


def _spread(tl, atom_ids, T0, T1, fps):
    """Split a timeline interval over the atoms it touches: [(atom_index, t0_rel, t1_rel, offset_s)]."""
    out, off = [], 0.0
    for i in atom_ids:
        at = tl.atoms[i]
        a0, a1 = at["f0"] / fps, at["f1"] / fps
        x0, x1 = max(a0, T0), min(a1, T1)
        if x1 - x0 > 0.5 / fps:
            out.append((i, x0 - a0, x1 - a0, off))
            off += x1 - x0
    return out, off


def apply_layers(ctx, tl, segs):
    """Overlays, cutaways, auto-cover and SFX of each plan segment -> atoms."""
    fps = ctx.fps
    specs, sfx = [], []
    for rec in segs:
        seg = rec["spec"]
        talk = rec["type"] == "talk"
        S0, S1 = rec["t0"], rec["t1"]
        seg_in = float(seg.get("in", 0) or 0)

        def span(at_, dur_):
            # start anchored to SOURCE time on talk (survives re-tightening); duration is output time,
            # so a card keeps running across the jump cuts inside it
            T0 = src_to_out(rec, seg_in + float(at_)) if talk else S0 + float(at_)
            return T0, T0 + float(dur_)
        for k, o in enumerate(seg.get("overlays", []) or []):
            T0, T1 = span(o.get("at", 0.3), o.get("dur", 2.5))
            T1 = min(T1, S1)
            parts, total = _spread(tl, rec["atoms"], T0, T1, fps)
            if not parts:
                ctx.warn(f"overlay '{o.get('text', '')}' falls outside its segment; skipped")
                continue
            sid = len(specs)
            specs.append({"id": sid, "kind": o["kind"], "text": o.get("text", ""), "sub": o.get("sub", ""),
                          "position": "center" if o["kind"] == "typed_title" else o.get("position", "auto"),
                          "dur": total, "first_atom": parts[0][0], "t0": T0, "t1": T0 + total})
            if o["kind"] == "typed_title":
                specs[-1]["opts"] = {k: o[k] for k in ("color", "size", "font", "start", "char_s", "y") if o.get(k) is not None}
                if o.get("pos"):
                    a_src = (seg_in + float(o.get("at", 0.0))) if talk else (seg_in + (T0 - S0) * float(seg.get("speed") or 1.0))
                    tp = _title_pos(ctx, seg, o, a_src, a_src + (T1 - T0) * float(seg.get("speed") or 1.0))
                    if tp:
                        specs[-1]["opts"].update({"x": round(tp["x"], 4), "y": round(tp["y"], 4)},
                                                 **({"backing": True} if tp.get("backing") else {}))
                        specs[-1]["pos_why"] = tp.get("why")
                        from . import titleplace as TP
                        bw, bh = TP.title_box(o.get("text") or "", float(o.get("size", CD.TITLE["size"])), ctx.W, ctx.H,
                                              o.get("font") or "")
                        specs[-1]["box"] = [round(max(0.0, tp["x"] - bw / 2), 4), round(max(0.0, tp["y"] - bh / 2), 4),
                                            round(bw, 4), round(bh, 4)]
                if o.get("sfx"):
                    sfx.append({"name": o["sfx"], "t": T0 + float(o.get("start", 0.0)) - 0.04,
                                "gain_db": float(o.get("sfx_gain_db", -16)), "on": f"typed:{sid}"})
            for i, a0, a1, off in parts:
                tl.atoms[i]["overlays"].append({"spec": sid, "t0": a0, "t1": a1, "offset": off})
        cut = [dict(c) for c in seg.get("cutaways", []) or []]
        if talk and seg.get("auto_cover") and rec["src_map"]:
            pool = list(seg.get("cover") or [])
            if not pool:
                ctx.warn(f"talk segment {rec['sec']}/{rec['seg']}: auto_cover needs a 'cover' list of clips; skipped")
            used = {}
            for (a, b, T), (a2, b2, T2) in zip(rec["src_map"], rec["src_map"][1:]):
                if not pool or a2 - b <= JOIN_COVER_S:
                    continue
                pre, post = min(0.7, 0.4 * (b - a)), min(0.9, 0.4 * (b2 - a2))
                J = T2
                if any(J - pre < c["_T1"] and J + post > c["_T0"] for c in cut if "_T0" in c):
                    continue
                c = pool[len(used) % len(pool)]
                ref = c["clip"] if isinstance(c, dict) else c
                row = ctx.clip(ref)
                cin = c.get("in") if isinstance(c, dict) else None
                if cin is None:
                    from . import analyze
                    cin = analyze.steadiest_window(ctx.con, row["id"], pre + post)[0]
                cin = float(cin) + used.get(row["id"], 0.0)
                used[row["id"]] = used.get(row["id"], 0.0) + pre + post + 0.5
                cut.append({"clip": ref, "in": cin, "_T0": J - pre, "_T1": J + post, "auto": True})
            # pacing: cap how long the face is held with no visual change (section.pace.max_hold)
            mh = (seg.get("pace") or {}).get("max_hold") or (rec.get("sec_pace") or {}).get("max_hold")
            if mh and pool:
                mh = float(mh)
                dur_c = min(1.8, max(0.8, 0.35 * mh))
                for _ in range(64):
                    spans = sorted([(c["_T0"], c["_T1"]) for c in cut if "_T0" in c] +
                                   [(src_to_out(rec, seg_in + float(c["at"])), src_to_out(rec, seg_in + float(c["at"])) + float(c["dur"]))
                                    for c in cut if "_T0" not in c])
                    free, last = [], S0
                    for a, b in spans:
                        if a > last:
                            free.append((last, a))
                        last = max(last, b)
                    free.append((last, S1))
                    long = [(a, b) for a, b in free if b - a > mh + 0.3]
                    if not long:
                        break
                    a, b = long[0]
                    J = a + mh * 0.6 if (b - a) > 2 * mh else (a + b) / 2 - dur_c / 2
                    c = pool[len(used) % len(pool)]
                    ref = c["clip"] if isinstance(c, dict) else c
                    row = ctx.clip(ref)
                    cin = c.get("in") if isinstance(c, dict) else None
                    if cin is None:
                        from . import analyze
                        cin = analyze.steadiest_window(ctx.con, row["id"], dur_c)[0]
                    cin = float(cin) + used.get(row["id"], 0.0)
                    used[row["id"]] = used.get(row["id"], 0.0) + dur_c + 0.5
                    cut.append({"clip": ref, "in": cin, "_T0": J, "_T1": min(S1, J + dur_c), "auto": True, "pace": True})
        for c in cut:
            if "_T0" in c:
                T0, T1 = c["_T0"], c["_T1"]
            else:
                T0 = src_to_out(rec, seg_in + float(c["at"])) if talk else S0 + float(c["at"])
                T1 = T0 + float(c["dur"])
            T1 = min(T1, S1)
            row = ctx.clip(c["clip"])
            parts, _ = _spread(tl, rec["atoms"], T0, T1, fps)
            for i, a0, a1, off in parts:
                tl.atoms[i]["cutaways"].append({"clip": row, "src": ctx.source(row), "a": float(c["in"]) + off,
                                                "t0": a0, "t1": a1, "auto": bool(c.get("auto")),
                                                "crop": c.get("crop", "center"), "zoom": c.get("zoom", 1.0),
                                                "unit": ("cut", row["id"], round(float(c["in"]) + off, 2))})
        seg_sfx = seg.get("sfx", [])
        if rec["type"] == "card" and "sfx" not in seg:
            seg_sfx = [{"name": "riser", "on": "end", "gain_db": -8.0}]          # the reference card rides a riser
        if isinstance(seg_sfx, str):
            seg_sfx = [{"name": seg_sfx, "on": "end" if rec["type"] == "card" else None}]
        for e in seg_sfx or []:
            if e.get("on") in ("cut", "transition"):
                sfx.append({"name": e["name"], "t": S0, "gain_db": float(e.get("gain_db", -6)), "align": "peak",
                            "on": f"cut:{rec['sec']}/{rec['seg']}"})
            elif e.get("on") == "end":
                sfx.append({"name": e["name"], "t": S1, "gain_db": float(e.get("gain_db", -6)), "align": "peak",
                            "on": f"end:{rec['sec']}/{rec['seg']}"})
            else:
                sfx.append({"name": e["name"], "t": S0 + float(e.get("at", 0.0)), "gain_db": float(e.get("gain_db", -6))})
    # transition sounds: the transient PEAK lands on the cut frame
    for k, tr in enumerate(getattr(ctx, "transitions", []) or []):
        if tr.get("sfx"):
            sfx.append({"name": tr["sfx"], "t": tr["t"], "gain_db": tr["gain_db"], "align": "peak", "on": f"transition:{k}"})
    return specs, sfx


# ============================================================================== per-atom preparation
def prepare(ctx, tl, segs, specs, pool):
    """Probes (faces, luma, loudness) per unit, crop rects, exposure eq, caption cues + placement,
    overlay positions. Probes run in the worker pool."""
    from . import asr
    units = {}
    for at in tl.atoms:
        if at["unit"] and at["clip"] and at["kind"] in ("clip", "still", "title"):
            u = units.setdefault(at["unit"], {"row": at["clip"], "src": at["src"], "t0": at["a"], "t1": at["a"]})
            u["t0"] = min(u["t0"], at["a"])
            u["t1"] = max(u["t1"], at["a"] + at["n"] / ctx.fps * at["speed"])
        for c in at["cutaways"]:
            units.setdefault(c["unit"], {"row": c["clip"], "src": c["src"], "t0": c["a"], "t1": c["a"] + (c["t1"] - c["t0"])})
    keys = list(units)
    import cv2  # noqa: F401  - import once here: a first import racing in worker threads leaves cv2 half-initialised
    futs = [pool.submit(probe_unit, ctx, units[k]["row"], units[k]["src"], units[k]["t0"], units[k]["t1"]) for k in keys]
    probes = {k: f.result() for k, f in zip(keys, futs)}

    # dialogue loudness per talk / voiceover unit (static gain per segment: no per-piece pumping)
    vstyle = ctx.style.get("voice") or {}
    loud_jobs = {}
    for at in tl.atoms:
        au = at["audio"]
        if au["mode"] == "none" or not au.get("clip") or not au["clip"].get("has_audio"):
            continue
        key = at.get("vo_unit") or at["unit"]
        asp = float(au.get("speed", 1.0) or 1.0)
        a0 = au["a"] - float(au.get("pre", 0.0)) * asp
        lj = loud_jobs.setdefault((key, au["mode"]), {"row": au["clip"], "t0": a0, "t1": a0, "mode": au["mode"],
                                                      "under_music": bool(at.get("under_music")),
                                                      "montage": at["type"] == "montage",
                                                      "teaser": bool(at.get("teaser"))})
        lj["t0"] = min(lj["t0"], a0)
        lj["t1"] = max(lj["t1"], au["a"] + (at["n"] / ctx.fps + float(au.get("post", 0.0))) * asp)
    voice_src = {}
    for (key, mode), lj in loud_jobs.items():
        row = lj["row"]
        if mode == "voice":
            if row["id"] not in voice_src:
                voice_src[row["id"]] = _voice_source(ctx, row, vstyle)
            lj["path"], lj["chain"] = voice_src[row["id"]]
        else:
            lj["path"], lj["chain"] = row["path"], ""
    lkeys = list(loud_jobs)
    lfuts = [pool.submit(measure_loudness, ctx, loud_jobs[k]["path"], loud_jobs[k]["t0"],
                         max(0.5, loud_jobs[k]["t1"] - loud_jobs[k]["t0"]), loud_jobs[k]["chain"]) for k in lkeys]
    for k, f in zip(lkeys, lfuts):
        I = f.result()
        target = {"voice": DIALOG_LUFS, "natural": NATURAL_LUFS, "mix": MIX_LUFS}[k[1]]
        if k[1] != "voice" and loud_jobs[k]["under_music"]:
            target = min(target, NATURAL_UNDER_MUSIC_LUFS)      # ambience sits under the music, never on top
        if k[1] != "voice" and loud_jobs[k]["montage"]:
            target += MONTAGE_NATURAL_DB
        if k[1] == "voice" and loud_jobs[k].get("teaser"):
            target += float(getattr(ctx, "co_boost", 0.0) or 0.0)   # cold-open lines sit at their own target
        loud_jobs[k]["target"] = target
        loud_jobs[k]["gain_db"] = 0.0 if I is None else max(-20.0, min(20.0, target - I))

    norm = bool(ctx.style["grade"].get("normalize_exposure", True))
    ex = ctx.style["grade"].get("exposure") or {}

    def stops_for(at_or_c, pr, rect):
        if at_or_c.get("exposure_stops") is not None:
            return float(at_or_c["exposure_stops"])
        return exposure_stops(luma_median_in(pr, rect), ex) if norm else 0.0
    cs = ctx.style["captions"]
    fixes = [tuple(f) for f in cs.get("fixes", []) or []]
    cap_dir = ctx.cache / "captions"
    for at in tl.atoms:
        pr = probes.get(at["unit"]) if at["unit"] else None
        faces = pr["faces"] if pr else []
        if at["clip"] and at["kind"] in ("clip", "still", "title"):
            info = ctx.info(at["src"])
            at["rect"] = crop_rect(info["w"], info["h"], ctx.W, ctx.H, at["crop"], at["zoom"], faces, at["align"])
            at["stops"] = stops_for(at, pr, at["rect"])
            at["luma_lut"] = luma_median_in(pr, at["rect"])
            at["grade"] = pr["grade"] if pr else ""
            at["faces_out"] = faces_out(faces, at["rect"], ctx.W, ctx.H)
            # a flash is light, not an overlay: a small log-domain push on its frames (never past ~+1 stop:
            # through the LUT a bigger push turns magenta) + the mix to white at the end of the atom graph
            pfx = []
            if at["kind"] == "clip" and at["grade"] in ("lut", "generic-log"):
                tr = at.get("tr_out")
                if tr and tr["kind"] == "flash" and tr["frames_out"]:
                    fo = tr["frames_out"]
                    pfx += [(at["n"] - fo + j, 1.0 * (j + 1) / fo) for j in range(fo)]
                tr = at.get("tr_in")
                if tr and tr["kind"] == "flash" and tr["frames_in"]:
                    fi = tr["frames_in"]
                    pfx += [(j, 1.0 * (fi - j) / fi) for j in range(fi)]
            at["pre_fx"] = pfx
            if at.get("push") and at["push"].get("x") is None and at["faces_out"] and at["crop"] == "face":
                fx, fy, fw, fh = max(at["faces_out"], key=lambda f: f[2] * f[3])
                at["push"]["x"] = round(min(1.0, max(0.0, (fx + fw / 2) / ctx.W)), 3)
                at["push"]["y"] = round(min(1.0, max(0.0, (fy + fh / 2) / ctx.H)), 3)
        else:
            at["faces_out"] = []
        for c in at["cutaways"]:
            cp = probes.get(c["unit"])
            ci = ctx.info(c["src"])
            c["rect"] = crop_rect(ci["w"], ci["h"], ctx.W, ctx.H, c["crop"], c["zoom"], cp["faces"] if cp else [])
            c["stops"] = exposure_stops(luma_median_in(cp, c["rect"]), ex) if norm else 0.0
        au = at["audio"]
        if au["mode"] != "none" and au.get("clip") and au["clip"].get("has_audio"):
            lj = loud_jobs[(at.get("vo_unit") or at["unit"], au["mode"])]
            au.update(path=lj["path"], chain=lj["chain"], gain_db=round(lj["gain_db"], 2))
            lj = loud_jobs[(at.get("vo_unit") or at["unit"], au["mode"])]
            au["target"] = lj["target"]
        elif au["mode"] != "none":
            au["mode"] = "none"
    # LUT-only numbers of each clip piece's own middle frame (QA grade_bounds)
    jobs = [(at, pool.submit(lut_frame_stats, ctx, at["clip"], at["src"],
                             at["a"] + at["n"] / ctx.fps * float(at["speed"] or 1.0) / 2, at["rect"]))
            for at in tl.atoms if at["kind"] == "clip" and at.get("rect") and at["n"] >= 8]
    for at, fu in jobs:
        try:
            at["lut_stats"] = fu.result()
        except Exception as e:  # noqa: BLE001 - a measurement, never a reason to fail the render
            ctx.warn(f"LUT-only probe failed on atom {at['i']}: {e}", once_key="lutstat")
    # captions follow the HEARD audio (rule 57): with a J/L cut, a cue spoken under the neighbouring
    # shot is drawn on that shot, so text and voice stay together
    size = int(cs.get("size", P.CAPTION_SIZE_1080))
    px = max(10, int(round(size * ctx.H / 1080.0)))

    def put(at, t0, t1, text):
        dur = at["n"] / ctx.fps
        t0, t1 = max(0.0, t0), min(t1, dur)
        if t1 - t0 < 0.1:
            return
        png, w, h = CAP.cue_png(cap_dir, text, ctx.W, ctx.H, ctx.font_pi, size, cs.get("case", "sentence"))
        pos = cs.get("position", "bottom")
        x, y = CAP.place(pos, w, h, ctx.W, ctx.H)
        busy = at["faces_out"] if not at["cutaways"] else []
        if pos != "top" and CAP.overlaps_face(x, y, w, h, busy):
            pos = "top"
            x, y = CAP.place(pos, w, h, ctx.W, ctx.H)
        pad = int(px * 0.9)                      # captions.render_cue pads the PNG for the shadow
        at["captions"].append({"t0": round(t0, 3), "t1": round(t1, 3), "text": text, "png": png,
                               "x": x, "y": y, "position": pos, "px": px, "box_w": w, "box_h": h, "pad": pad})
    for at in tl.atoms:
        if not at.get("cue_src"):
            continue
        cid, a, b = at["cue_src"]
        shift = float(at.get("cue_shift", 0.0))
        dur = at["n"] / ctx.fps
        i = at["i"]
        for t0, t1, text in asr.cues(_words(ctx, cid), a, b, max_words=int(cs.get("max_words", 5)),
                                     max_chars=int(cs.get("max_chars", 30)), fixes=fixes):
            t0, t1 = t0 - shift, t1 - shift
            if t0 < 0 and i > 0:
                pv = tl.atoms[i - 1]
                pd = pv["n"] / ctx.fps
                put(pv, pd + t0, pd + min(t1, 0.0), text)
            if t1 > dur and i + 1 < len(tl.atoms):
                put(tl.atoms[i + 1], max(0.0, t0 - dur), t1 - dur, text)
            put(at, t0, t1, text)
    for at in tl.atoms:
        at["captions"].sort(key=lambda c: c["t0"])
    # overlay placement: auto -> corner away from faces of the first atom it appears on
    cap_bottom = cs.get("position", "bottom") != "top"
    for sp in specs:
        if sp["position"] in (None, "", "auto"):
            sp["position"] = OV.auto_position(sp["kind"], tl.atoms[sp["first_atom"]]["faces_out"], ctx.W, ctx.H, cap_bottom)
        elif sp["kind"] in ("title",) and sp["position"] == "auto":
            sp["position"] = "center"


def _voice_source(ctx, row, vstyle):
    """(audio path, filter chain) for dialogue from a clip."""
    from . import voice
    chain = voice.voice_chain()
    if vstyle and vstyle.get("clean", True):
        try:
            return voice.clean(ctx.cfg, row["id"], vstyle.get("atten_lim_db", 20)), chain
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"voice clean failed for {row['rel']}: {e}; using camera audio")
    return row["path"], chain


# ============================================================================== atom render
def _atempo(speed):
    s, out = float(speed), []
    while s > 2.0:
        out.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        out.append("atempo=0.5"); s /= 0.5
    if abs(s - 1.0) > 1e-3:
        out.append(f"atempo={s:.5f}")
    return out


def _rate_filter(conform, fpsr):
    """Frame-rate stage of a clip head: plain fps (identical rates, speed conform, or far-off rates) or a
    frame blend for sync sources within 5 % of the output rate (no duplicated frames, lip-sync kept)."""
    if conform == "blend":
        return f"framerate=fps={fpsr}:interp_start=0:interp_end=255:scene=8"
    return f"fps={fpsr}"


def ramp_params(at, fps):
    """Speed ramp into the cut on a b-roll / montage piece: base speed until u1, then a linear rise to
    `to` x over the last `over` seconds. Returns the setpts expression (source time -> output time),
    the source seconds consumed and where the motion blur fades in, or None."""
    rp = at.get("ramp")
    if not rp:
        return None
    d = at["n"] / fps
    sp = float(at["speed"] or 1.0)
    over = max(2.0 / fps, min(float(rp.get("over", 0.4)), 0.8 * d))
    to = float(rp["to"])
    u1 = d - over
    k = sp * (to - 1.0) / over
    s1 = sp * u1
    expr = f"if(lt(T,{s1:.6f}),T/{sp:.6f},{u1:.6f}+(-{sp:.6f}+sqrt({sp * sp:.6f}+2*{k:.6f}*(T-{s1:.6f})))/{k:.6f})/TB"
    q = 0.25 * over
    return {"u1": u1, "over": over, "to": to, "k": k, "expr": expr, "src_need": s1 + sp * over + k * over * over / 2,
            "t_blur": s1 + sp * q + k * q * q / 2}


def _k_out(fo, lo, hi):
    return [round(float(x), 3) for x in (np.linspace(lo, hi, fo) if fo > 1 else [0.5 * (lo + hi) + 0.1])]


def _k_in(fi, hi, lo):
    if fi <= 1:
        return [round(0.5 * (hi + lo) + 0.25, 3)]
    return [round(float(hi * (lo / hi) ** (j / (fi - 1))), 3) for j in range(fi)]


def transition_filters(at, n, s=1.0):
    """Light events on an atom's boundary frames, built from the footage itself (never an overlay asset):
    flash = mix toward white (in Y'CbCr, so no colour cast) on the last/first frames, dip = through black,
    dip_white = through white, whip = a temporal mix + horizontal smear on the frames either side of the
    cut. Frame indices are the atom's own (0 = first frame)."""
    out, whip_en = [], []

    def mix(f, K, white):
        y = 235 if white else 16
        return (f"lutyuv=y='val+({y}-val)*{K:.3f}':u='val+(128-val)*{K:.3f}':v='val+(128-val)*{K:.3f}'"
                f":enable='eq(n,{int(f)})'")
    for side in ("tr_out", "tr_in"):
        tr = at.get(side)
        if not tr:
            continue
        kind = tr["kind"]
        fr = tr["frames_out"] if side == "tr_out" else tr["frames_in"]
        if fr <= 0 or kind == "burn":                # burns are light layers (build_atom_cmd), not a mix
            continue
        if kind == "whip":
            whip_en.append(f"gte(n,{n - fr})" if side == "tr_out" else f"lt(n,{fr})")
            continue
        if kind == "flash":
            ks = _k_out(fr, *FLASH_K_OUT) if side == "tr_out" else _k_in(fr, *FLASH_K_IN)
        else:
            ks = _k_out(fr, 0.5, 1.0) if side == "tr_out" else list(reversed(_k_out(fr, 0.5, 1.0)))
        frames = range(n - fr, n) if side == "tr_out" else range(fr)
        out += [mix(f, K, kind in ("flash", "dip_white")) for f, K in zip(frames, ks)]
    if whip_en:
        en = "+".join(whip_en)
        bx = max(4, int(round(24 * s)))
        out = [f"split[ws][wt]", f"[wt]tmix=frames=3,avgblur=sizeX={bx}:sizeY=1[wb]",
               f"[ws][wb]overlay=0:0:enable='{en}'" + "".join("," + x for x in out) + "[v]"]
    return out


def build_atom_cmd(ctx, at, gfx):
    """ffmpeg args (without output) for one atom, plus the list of input files to fingerprint."""
    fps, W, H = ctx.fps, ctx.W, ctx.H
    n = at["n"]
    d = n / fps
    args, fc, files = [], [], []
    nin = 0

    def inp(*a):
        nonlocal nin
        args.extend(a)
        nin += 1
        return nin - 1
    fpsr = f"{fps:g}"
    kind = at["kind"]
    if kind == "card":
        cdir, _ = CD.card_sequence(ctx.cache / "cards", W, H, ctx.fps, n, at["card"], seed=int(at["f0"]))
        i0 = inp("-framerate", fpsr, "-start_number", "0", "-i", os.path.join(cdir, "%04d.png"))
        files.append(cdir)
        fc.append(f"[{i0}:v]scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p,setsar=1[b0]")
    elif kind == "black" or (kind == "title" and not at["title"]["bg"].startswith("blur:")):
        i0 = inp("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fpsr}:d={d + 0.5:.3f}")
        fc.append(f"[{i0}:v]setsar=1,format=yuv420p[b0]")
    elif kind == "title":
        i0 = inp("-ss", f"{at['a']:.3f}", "-i", at["src"])
        files.append(at["src"])
        ch = picture(ctx, at["clip"], at["src"], W, H, at["rect"], at.get("stops", 0.0), label="t", look=at.get("look"))
        fc += [c.replace("[IN]", f"[{i0}:v]trim=end_frame=1,setpts=PTS-STARTPTS,").replace("[OUT]", "[tp]") for c in ch]
        fc.append(f"[tp]gblur=sigma={30 * ctx.s:.1f},eq=brightness=-0.12:saturation=0.85,loop=loop={n + 2}:size=1:start=0,"
                  f"setpts=N/({fpsr}*TB),fps={fpsr}[b0]")
    elif kind == "still":
        i0 = inp("-ss", f"{at['a']:.3f}", "-i", at["src"])
        files.append(at["src"])
        W2, H2 = W * 2, H * 2
        ch = picture(ctx, at["clip"], at["src"], W2, H2, at["rect"], at.get("stops", 0.0), label="s", look=at.get("look"))
        fc += [c.replace("[IN]", f"[{i0}:v]trim=end_frame=1,setpts=PTS-STARTPTS,").replace("[OUT]", "[sp]") for c in ch]
        z = max(1.0, float(at.get("still_zoom", 1.08)))
        fc.append(f"[sp]loop=loop={n + 2}:size=1:start=0,setpts=N/({fpsr}*TB),"
                  f"zoompan=z='1+({z - 1:.4f})*on/{max(1, n)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fpsr},"
                  f"setsar=1,format=yuv420p[b0]")
    else:
        sp = float(at["speed"] or 1.0)
        rp = ramp_params(at, fps)
        need = rp["src_need"] if rp else d * sp
        i0 = inp("-ss", f"{max(0.0, at['a']):.3f}", "-t", f"{need + 0.5:.3f}", "-i", at["src"])
        files.append(at["src"])
        ch = picture(ctx, at["clip"], at["src"], W, H, at["rect"], at.get("stops", 0.0), at.get("fit", "cover"),
                     at.get("stabilize"), label="m", look=at.get("look"), pre_fx=at.get("pre_fx") or (),
                     push=at.get("push"))
        rate = _rate_filter(at.get("conform"), fpsr)
        if rp:
            # speed ramp into the cut: output time -> source time follows a linear speed rise; a 4-frame
            # temporal mix fades in as the speed climbs (motion blur), keyed on SOURCE time (blend's T)
            head = (f"[{i0}:v]setpts=PTS-STARTPTS,split[rs][rt];[rt]tmix=frames=4[rm];"
                    f"[rs][rm]blend=all_expr='A*(1-clip((T-{rp['t_blur']:.4f})/0.3,0,1))+B*clip((T-{rp['t_blur']:.4f})/0.3,0,1)',"
                    f"setpts='{rp['expr']}',{rate},")
        else:
            head = f"[{i0}:v]setpts=(PTS-STARTPTS)/{sp:.8f},{rate},"
        fc += [c.replace("[IN]", head).replace("[OUT]", "[b0]") for c in ch]
    last = "b0"
    k = 0
    # cutaways: b-roll picture over the main picture, main audio continues
    for c in at["cutaways"]:
        k += 1
        cd = c["t1"] - c["t0"]
        ci = inp("-ss", f"{max(0.0, c['a']):.3f}", "-t", f"{cd + 0.5:.3f}", "-i", c["src"])
        files.append(c["src"])
        ch = picture(ctx, c["clip"], c["src"], W, H, c["rect"], c.get("stops", 0.0), label=f"c{k}", look=at.get("look"))
        cmode, _ = _conform(ctx, c["clip"], c["src"], None, sync=True)
        fc += [x.replace("[IN]", f"[{ci}:v]setpts=PTS-STARTPTS,{_rate_filter(cmode, fpsr)},")
                .replace("[OUT]", f"[cx{k}]") for x in ch]
        fc.append(f"[cx{k}]tpad=stop_mode=clone:stop_duration=1,setpts=PTS+{c['t0']:.4f}/TB[cv{k}]")
        fc.append(f"[{last}][cv{k}]overlay=eof_action=pass:enable='between(t,{c['t0']:.4f},{c['t1'] - 0.5 / fps:.4f})'[x{k}]")
        last = f"x{k}"
    # frame (window-box gate / inset / letterbox): the picture is scaled to COVER the window (cropped, never
    # squeezed) and laid on the border colour; a matte with the rounded window goes on top. Hard snap.
    fr = at.get("frame")
    if fr:
        mpng, (bx0, by0, bx1, by1) = CD.matte_png(ctx.cache / "mattes", W, H, fr)
        files.append(mpng)
        f_ = max((bx1 - bx0) / W, (by1 - by0) / H)
        sw, sh = max(2, int(math.ceil(W * f_ / 2)) * 2), max(2, int(math.ceil(H * f_ / 2)) * 2)
        px, py = int(round((bx0 + bx1) / 2 - sw / 2)), int(round((by0 + by1) / 2 - sh / 2))
        col = "0x" + "".join(f"{v:02X}" for v in CD.hex_rgb(fr["color"]))
        bgi = inp("-f", "lavfi", "-i", f"color=c={col}:s={W}x{H}:r={fpsr}:d={d + 0.5:.3f}")
        mi = inp("-i", mpng)
        fc.append(f"[{last}]scale={sw}:{sh}:flags=lanczos,setsar=1[frs]")
        fc.append(f"[{bgi}:v]format=yuv420p[frb]")
        fc.append(f"[frb][frs]overlay={px}:{py}:eof_action=endall:shortest=1[frp]")
        fc.append(f"[frp][{mi}:v]overlay=0:0:eof_action=repeat,format=yuv420p[frm]")
        last = "frm"
    # overlays. Frosted glass: ONE full-length mask stream per atom (black + every card mask laid on
    # it), ONE blur branch, alphamerge, overlay - then the cards. Every branch lives for the whole
    # atom: per-card finite blur/mask branches deadlock ffmpeg's scheduler intermittently.
    glass = [o for o in at["overlays"] if gfx[o["spec"]]["mask"]]
    if glass:
        mb = inp("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fpsr}:d={d + 1:.3f}")
        ml = f"mb{k}"
        fc.append(f"[{mb}:v]format=yuv420p[{ml}]")
        win = []
        for j, o in enumerate(glass):
            g = gfx[o["spec"]]
            off = int(round(o["offset"] * fps))
            mi = inp("-framerate", fpsr, "-start_number", str(off), "-i", os.path.join(g["dir"], "m%04d.png"))
            files.append(g["dir"])
            fc.append(f"[{mi}:v]format=rgba,setpts=PTS-STARTPTS+{o['t0']:.4f}/TB[mq{j}]")
            fc.append(f"[{ml}][mq{j}]overlay=0:0:eof_action=pass:enable='between(t,{o['t0']:.4f},{o['t1']:.4f})'[mm{j}]")
            ml = f"mm{j}"
            win.append(f"between(t,{o['t0']:.4f},{o['t1']:.4f})")
        en = "+".join(win)
        fc.append(f"[{last}]split[gs1][gs2]")
        fc.append(f"[gs2]gblur=sigma={26 * ctx.s:.1f}:enable='{en}',eq=brightness=-0.03:saturation=1.1:enable='{en}',format=yuv420p[gbl]")
        fc.append(f"[gbl][{ml}]alphamerge[gam]")
        fc.append(f"[gs1][gam]overlay=0:0:eof_action=pass:enable='{en}'[glz]")
        last = "glz"
    for o in at["overlays"]:
        k += 1
        g = gfx[o["spec"]]
        off = int(round(o["offset"] * fps))
        t0, t1 = o["t0"], o["t1"]
        gi = inp("-framerate", fpsr, "-start_number", str(off), "-i", os.path.join(g["dir"], "%04d.png"))
        files.append(g["dir"])
        fc.append(f"[{gi}:v]format=rgba,setpts=PTS-STARTPTS+{t0:.4f}/TB[o{k}]")
        fc.append(f"[{last}][o{k}]overlay=0:0:eof_action=pass:enable='between(t,{t0:.4f},{t1 - 0.5 / fps:.4f})'[y{k}]")
        last = f"y{k}"
    # title card graphic
    if kind == "title":
        k += 1
        g = gfx[at["title_spec"]]
        gi = inp("-framerate", fpsr, "-start_number", "0", "-i", os.path.join(g["dir"], "%04d.png"))
        files.append(g["dir"])
        fc.append(f"[{gi}:v]format=rgba[o{k}]")
        fc.append(f"[{last}][o{k}]overlay=0:0:eof_action=pass[y{k}]")
        last = f"y{k}"
    # captions
    for c in at["captions"]:
        k += 1
        # single-frame input + eof_action=repeat; `-loop 1` image inputs + the glass split deadlock ffmpeg 8
        pi = inp("-i", c["png"])
        files.append(c["png"])
        fc.append(f"[{last}][{pi}:v]overlay={c['x']}:{c['y']}:eof_action=repeat:enable='between(t,{c['t0']:.4f},{c['t1']:.4f})'[y{k}]")
        last = f"y{k}"
    # film burns: light frames on the boundary (cut between the outgoing and incoming frame), composited
    # as a normal mix toward the light followed by a screen of the light (cards.composite_burn)
    for side in ("tr_out", "tr_in"):
        tr = at.get(side)
        if not tr or tr["kind"] != "burn":
            continue
        fr_n = tr["frames_out"] if side == "tr_out" else tr["frames_in"]
        for j in range(fr_n):
            fidx = (n - fr_n + j) if side == "tr_out" else j
            if not 0 <= fidx < n:
                continue
            lp, la = CD.burn_png(ctx.cache / "burns", W, H, "out" if side == "tr_out" else "in",
                                 tr.get("palette", "warm"), tr.get("side", "right"), float(tr.get("strength", 1.0)),
                                 int(tr.get("seed", 0)), j, fr_n)
            files += [lp, la]
            ia, il = inp("-i", la), inp("-i", lp)
            en = f"between(t,{(fidx - 0.25) / fps:.4f},{(fidx + 0.25) / fps:.4f})"
            k += 1
            fc.append(f"[{last}][{ia}:v]overlay=0:0:eof_action=repeat:enable='{en}'[bu{k}]")
            fc.append(f"[{il}:v]format=gbrp,scale={W}:{H}[bl{k}]")
            fc.append(f"[bu{k}]format=gbrp[bp{k}]")
            fc.append(f"[bp{k}][bl{k}]blend=all_mode=screen:enable='{en}',format=yuv420p[bb{k}]")
            last = f"bb{k}"
    trx = transition_filters(at, n, ctx.s)
    if trx and trx[0].startswith("split"):
        fc.append(f"[{last}]format=yuv420p,tpad=stop_mode=clone:stop_duration={d + 1:.3f},trim=end_frame={n},setpts=PTS-STARTPTS,"
                  f"{trx[0]}")
        fc += trx[1:]
    else:
        fc.append(f"[{last}]format=yuv420p,tpad=stop_mode=clone:stop_duration={d + 1:.3f},trim=end_frame={n},setpts=PTS-STARTPTS"
                  f"{''.join(',' + x for x in trx)}[v]")
    return args, fc, files


def atom_samples(at, fps):
    """Exact sample span of an atom from the global frame grid (no drift over hundreds of cuts)."""
    s0 = int(round(at["f0"] * SR / fps))
    return s0, int(round(at["f1"] * SR / fps)) - s0


def audio_span(at, fps):
    """(pre_samples, total_samples) of an atom's audio: its frame-exact span plus the handle / J-cut lead
    before it and the handle / L-cut tail after it (those parts overlap the neighbours on the bus)."""
    _, S = atom_samples(at, fps)
    au = at["audio"]
    pre = int(round(float(au.get("pre", 0.0) or 0.0) * SR))
    post = int(round(float(au.get("post", 0.0) or 0.0) * SR))
    return pre, S + pre + post


def build_audio_cmd(ctx, at):
    """ffmpeg args for an atom's dialogue/natural audio (None when the atom is silent). Audio runs in
    its own process: an audio output in the same graph as the overlay compositing deadlocks
    ffmpeg 8's scheduler intermittently (measured ~15% of runs).
    The window runs from `pre` seconds before the atom to `post` after it. Joins between two pieces of
    the same sound get equal-power (quarter-sine) crossfades over the handles; a J-cut lead fades in
    under the previous shot and an L-cut tail fades out under the next one."""
    au = at["audio"]
    if au["mode"] == "none":
        return None, []
    fps = ctx.fps
    d = at["n"] / fps
    pre_n, S_tot = audio_span(at, fps)
    pre, post = pre_n / SR, float(au.get("post", 0.0) or 0.0)
    tot = S_tot / SR
    asp = float(au.get("speed", 1.0) or 1.0)
    chain = [f"aresample={SR}", "aformat=sample_fmts=fltp:channel_layouts=stereo"] + _atempo(asp)
    if au.get("chain"):
        chain.append(au["chain"])
    chain.append(f"volume={au.get('gain_db', 0.0):.2f}dB")
    fi, fo = float(au.get("fi", 0.012)), float(au.get("fo", 0.015))
    ci, co = au.get("curve_in", "qsin" if pre else "tri"), au.get("curve_out", "qsin" if post else "tri")
    chain += [f"afade=t=in:d={fi:.4f}:curve={ci}", f"afade=t=out:st={max(0.0, tot - fo):.4f}:d={fo:.4f}:curve={co}"]
    rp = ramp_params(at, fps)
    if rp:                                       # no sync sound on a ramped piece: the ambience leaves with the ramp
        chain.append(f"afade=t=out:st={pre + max(0.0, rp['u1'] - 0.05):.4f}:d={rp['over']:.4f}")
    chain += [f"apad=whole_len={S_tot}", f"atrim=end_sample={S_tot}", "asetpts=N/SR/TB"]
    a0 = au["a"] - pre * asp
    args = ["-ss", f"{max(0.0, a0):.3f}", "-t", f"{tot * asp + 0.5:.3f}", "-i", au["path"], "-vn",
            "-af", ",".join(chain), "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2"]
    return args, [au["path"]]


def _encoder(ctx):
    tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
    if ctx.preview:
        v = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-g", str(int(ctx.fps))]
    else:
        v = ["-c:v", "prores_ks", "-profile:v", "1", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
    return v + tags + ["-an"]


def _input_ids(files):
    ids = []
    for f in files:
        if os.path.isdir(f):
            ids.append([f, "dir"])          # overlay dirs and caption PNGs are content-named
        else:
            ids.append(_file_id(f))
    return ids


def _atom_err(at, e):
    return RuntimeError(f"atom {at['i']} ({at['section']} / {at['type']} {at['kind']}"
                        f"{' ' + at['clip']['rel'] if at.get('clip') else ''} @ {at['a']:.2f}) failed:\n{e}")


def _encode_atom(ctx, at, args, script, enc, tmp, budget):
    # stall guard: generous per-second budget, one retry (ffmpeg's scheduler can wedge on rare graphs)
    for attempt in (1, 2):
        try:
            media.ffmpeg(ctx.cfg, args + ["-filter_complex_script", script, "-map", "[v]", *enc, "-f", "mov", tmp],
                         timeout=budget)
            return
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise _atom_err(at, f"ffmpeg stalled twice (> {budget:.0f}s)") from None
            ctx.warn(f"atom {at['i']}: ffmpeg stalled after {budget:.0f}s; retrying once")


def render_atom(ctx, at, gfx):
    """Render (or reuse) an atom's picture (.mov, video only) and audio (.wav). Returns (mov, wav|None)."""
    segdir = ctx.cache / "segs"
    segdir.mkdir(parents=True, exist_ok=True)
    args, fc, files = build_atom_cmd(ctx, at, gfx)
    enc = _encoder(ctx)
    key = _sha({"code": CODE_VERSION, "args": args, "fc": fc, "enc": enc, "inputs": _input_ids(files),
                "fps": ctx.fps, "W": ctx.W, "H": ctx.H}, 20)
    out = segdir / f"{key}.mov"
    at["cache_key"] = key
    hit_v = out.exists() and out.stat().st_size > 0
    if not hit_v:
        tmp = segdir / f".{key}.tmp.mov"
        script = segdir / f".{key}.fc"
        script.write_text(";\n".join(fc))
        budget = max(180.0, at["n"] / ctx.fps * (25 if ctx.preview else 90))
        gov = governor.for_cfg(ctx.cfg)
        try:
            with gov.slot("heavy", label=f"render atom {at['i']}"):
                gov.wait_for_disk(segdir)
                _encode_atom(ctx, at, args, script, enc, tmp, budget)
        except media.FFError as e:
            raise _atom_err(at, e) from None
        finally:
            script.unlink(missing_ok=True)
        os.replace(tmp, out)
    wav, hit_a = None, True
    aargs, afiles = build_audio_cmd(ctx, at)
    if aargs:
        akey = _sha({"code": CODE_VERSION, "args": aargs, "inputs": _input_ids(afiles)}, 20)
        wav = segdir / f"{akey}.wav"
        at["audio_key"] = akey
        hit_a = wav.exists() and wav.stat().st_size > 44
        if not hit_a:
            tmp = segdir / f".{akey}.tmp.wav"
            try:
                media.ffmpeg(ctx.cfg, aargs + ["-f", "wav", tmp], timeout=1800)
            except media.FFError as e:
                raise _atom_err(at, e) from None
            os.replace(tmp, wav)
    at["cached"] = hit_v and hit_a
    return str(out), (str(wav) if wav else None)


# ============================================================================== audio mix
def _read_wav(path, n):
    import soundfile as sf
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SR:
        raise RuntimeError(f"{path}: expected {SR} Hz, got {sr}")
    if a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    a = a[:, :2]
    if len(a) < n:
        a = np.vstack([a, np.zeros((n - len(a), 2), np.float32)])
    return a[:n]


def sfx_track(ctx, events, n):
    from . import music
    import soundfile as sf
    buf = np.zeros((n, 2), np.float32)
    for e in events:
        p = e["name"]
        if not os.path.exists(str(p)):
            p = music.sfx(e["name"], ctx.cfg)
        x, sr = sf.read(str(p), dtype="float32", always_2d=True)
        if sr != SR:
            ctx.warn(f"sfx {e['name']} is {sr} Hz; skipped (use 48 kHz)")
            continue
        if x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        t = float(e["t"])
        if e.get("align") == "peak":
            pk = music.sfx_peak(p)
            e["peak_s"] = round(pk, 4)
            t -= pk                                    # the transient, not the file start, lands on the frame
        s = int(round(t * SR))
        if s >= n:
            continue
        m = min(len(x), n - max(0, s))
        if s < 0:
            x, s = x[-s:], 0
            m = min(len(x), n)
        buf[s:s + m] += x[:m, :2] * (10 ** (e["gain_db"] / 20))
    return buf


def _loudnorm_json(stderr):
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr or "", re.S)
    return json.loads(m.group(0)) if m else None


def _ebur128(cfg, path):
    r = media.ffmpeg(cfg, ["-loglevel", "info", "-nostats", "-i", path, "-map", "0:a:0",
                           "-af", "ebur128=peak=true", "-f", "null", "-"])
    tail = (r.stderr or "")[-1500:]
    I = re.search(r"I:\s*(-?[\d.]+|-inf) LUFS", tail)
    LRA = re.search(r"LRA:\s*(-?[\d.]+) LU", tail)
    TP = re.search(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf) dBFS", tail, re.S)
    f = lambda m: float(m.group(1)) if m and "inf" not in m.group(1) else None   # noqa: E731
    return {"I": f(I), "LRA": f(LRA), "TP": f(TP)}


def _measure(cfg, path):
    r = media.ffmpeg(cfg, ["-loglevel", "info", "-i", path, "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    m = _loudnorm_json(r.stderr)
    if not m or "inf" in str(m.get("input_i")):
        return None
    return {"I": float(m["input_i"]), "TP": float(m["input_tp"]), "LRA": float(m["input_lra"])}


def render_graphics(ctx, specs):
    """Overlay graphics: typed titles are drawn here (cards.typed_sequence), everything else by overlays.py."""
    typed = [sp for sp in specs if sp["kind"] == "typed_title"]
    rest = [sp for sp in specs if sp["kind"] != "typed_title"]
    gfx = OV.render_all(rest, ctx.W, ctx.H, ctx.fps, ctx.cache / "gfx", ctx.font_pi, warn=ctx.warn) if rest else {}
    for sp in typed:
        n = max(1, int(round(sp["dur"] * ctx.fps)))
        d, fp = CD.typed_sequence(ctx.cache / "gfx", ctx.W, ctx.H, ctx.fps, n, {"text": sp["text"], **(sp.get("opts") or {})})
        gfx[sp["id"]] = {"dir": d, "n": n, "mask": False, "key": Path(d).name, "renderer": "typed",
                         "font": os.path.basename(fp[0] or "builtin")}
    return gfx


def dynamics_spec(co) -> dict:
    """cold_open.dynamics -> {"match": "reference", ...} or {} (off). Accepts the string "reference"."""
    d = (co or {}).get("dynamics")
    if isinstance(d, str):
        d = {"match": d}
    return dict(d) if isinstance(d, dict) and d.get("match") == "reference" else {}


def _limit_wav(cfg, src, dst, ceil_db):
    """The house true-peak limiter (4x oversampled alimiter, as normalize_mix) on one file."""
    lim = 10 ** (ceil_db / 20)
    media.ffmpeg(cfg, ["-i", str(src), "-af", f"aresample=192000,alimiter=limit={lim:.5f}:attack=1:release=60:level=false,"
                                              f"aresample={SR}", "-c:a", "pcm_f32le", "-ar", str(SR), str(dst)])


def dyn_target_curve(ref_curve, spans, co_end, ref_end, step=0.1):
    """The reference's momentary loudness mapped onto OUR cold open: [(t_ours, M_target)].
    spans: [(our_t0, our_t1, ref_t0, ref_t1)] per element (piecewise-linear time map; outside the knots the
    edges are held by offset); with no spans our [0, co_end] is stretched onto the reference's [0, ref_end]."""
    rc = np.asarray(ref_curve, float).reshape(-1, 2)
    if not len(rc):
        return []
    if spans:
        kx, ky = [], []
        for a, b, ra, rb in sorted(spans):
            if kx and a < kx[-1] - 1e-6:
                continue
            kx += [a, b]
            ky += [ra, rb]
        kx, ky = np.asarray(kx), np.asarray(ky)

        def mp(t):
            if t < kx[0]:
                return ky[0] - (kx[0] - t)
            if t > kx[-1]:
                return ky[-1] + (t - kx[-1])
            return float(np.interp(t, kx, ky))
    else:
        def mp(t):
            return t * ref_end / max(co_end, 1e-6)
    out = []
    t = step
    while t <= co_end + 1e-6:
        out.append((round(t, 3), float(np.interp(mp(t), rc[:, 0], rc[:, 1]))))
        t += step
    return out


def _apply_gain_curve(win, sr, centres, G, co_end, smooth_s=0.012):
    """dB gain knots (at `centres`) -> per-sample gain on the window: linear between knots, back to unity over
    the last 20 ms before the cut into the edit, unity after it, then a 12 ms box smooth so a gain step
    never clicks (a click is a peak the encoder turns into true-peak overshoot)."""
    n = len(win)
    ts = np.arange(n) / sr
    g_db = np.interp(ts, centres, G, left=G[0], right=G[-1])
    g_db[ts > co_end] = 0.0
    m = (ts > co_end - 0.02) & (ts <= co_end)
    if m.any():
        g_db[m] *= np.linspace(1, 0, m.sum())
    k = max(1, int(smooth_s * sr))
    g = 10 ** (g_db / 20)
    g = np.convolve(np.pad(g, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    return (win * g.astype(np.float32)[:, None]).astype(np.float32)


def relimit_overshoots(cfg, wav, encoded, out_wav, tp_target, block_s=0.5, pad_s=0.25):
    """Post-encode true-peak fix that leaves the programme level alone: decode the encoded file 4x oversampled,
    find the blocks whose peak crosses `tp_target`, and replace only those stretches (padded, 30 ms
    crossfades) with the mix limited lower by the worst overshoot + 0.3 dB. Returns {extra_db, spans} or None
    (nothing to fix / undecodable)."""
    import soundfile as sf
    r = subprocess.run([getattr(cfg, "ffmpeg", None) or "ffmpeg", "-v", "error", "-i", str(encoded), "-map", "0:a:0",
                        "-f", "f32le", "-ac", "2", "-ar", str(SR * 4), "-"], capture_output=True)
    a = np.frombuffer(r.stdout, np.float32)
    if not len(a):
        return None
    pk = np.abs(a.reshape(-1, 2)).max(axis=1)
    bl = int(block_s * SR * 4)
    nb = len(pk) // bl + 1
    over = []
    for i in range(nb):
        seg = pk[i * bl:(i + 1) * bl]
        if len(seg):
            db = 20 * np.log10(float(seg.max()) + 1e-12)
            if db > tp_target - 0.05:
                over.append((i * block_s, (i + 1) * block_s, db))
    if not over:
        return None
    extra = max(db for _, _, db in over) - tp_target + 0.3
    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    lim = Path(str(out_wav) + ".lim.wav")
    _limit_wav(cfg, wav, lim, tp_target - 1.0 - extra)
    y = sf.read(str(lim), dtype="float32", always_2d=True)[0]
    lim.unlink(missing_ok=True)
    n = min(len(x), len(y))
    w = np.zeros(n, np.float32)
    for a0, b0, _ in over:
        w[max(0, int((a0 - pad_s) * sr)):min(n, int((b0 + pad_s) * sr))] = 1.0
    k = int(0.03 * sr)
    w = np.convolve(np.pad(w, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid").astype(np.float32)
    out = x.copy()
    out[:n] = x[:n] * (1 - w[:, None]) + y[:n] * w[:, None]
    sf.write(str(out_wav), out, sr, subtype="PCM_24")
    return {"extra_db": round(extra, 2), "spans": [[round(a0, 2), round(b0, 2)] for a0, b0, _ in over]}


def _encoded_tp(cfg, wav, tmp):
    """True peak (dBTP) of a wav after the final AAC encode (192k, as render_plan encodes)."""
    try:
        media.ffmpeg(cfg, ["-i", str(wav), "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), str(tmp)])
        return _ebur128(cfg, str(tmp))["TP"]
    except Exception:  # noqa: BLE001
        return None


def shape_cold_open(ctx, plan, tl, segs, wav, target, iters=5):
    """cold_open.dynamics {"match": "reference"}: after programme normalisation, give the cold open the
    reference's measured momentary-loudness envelope (its swing: lines vs hits vs the held end), element
    by element. The gain curve (0.1 s knots, dB) acts on the summed cold-open stems (voice + music + SFX) and
    is solved by measuring: apply -> limit -> measure (ebur128) -> correct, a few rounds. The hits' peaks
    are caught by the same oversampled limiter ON THE COLD OPEN ONLY, before the final encode, so the
    loud moments are limited locally instead of the whole programme being pulled down afterwards.
    Returns the sidecar record. `wav` is rewritten in place."""
    import soundfile as sf
    from . import dynamics as D
    from . import reference as R
    co = plan.get("cold_open") or {}
    dyn = dynamics_spec(co)
    rid = dyn.get("reference") or co.get("from_reference") or plan.get("reference")
    if not rid:
        ctx.warn("cold_open.dynamics: no reference to match (cold_open.from_reference)")
        return {"status": "skipped", "why": "no reference"}
    fps = ctx.fps
    recs = [r for r in segs if r["sec"] == -1 and r["atoms"]]
    if not recs:
        return {"status": "skipped", "why": "no cold open"}
    co_end = max(tl.atoms[i]["f1"] for r in recs for i in r["atoms"]) / fps
    spans = []
    for r in recs:
        sp = r["spec"].get("_ref_span")
        if sp:
            a = tl.atoms[r["atoms"][0]]["f0"] / fps
            b = max(tl.atoms[i]["f1"] for i in r["atoms"]) / fps
            spans.append((a, b, float(sp[0]), float(sp[1])))
    try:
        ref = R.intro_envelope(ctx.cfg, rid)
    except Exception as e:  # noqa: BLE001
        ctx.warn(f"cold_open.dynamics: reference envelope not measured ({e})")
        return {"status": "skipped", "why": str(e)[:200]}
    off = float(dyn.get("offset_db", 0.0) or 0.0)
    tgt = [(t, m + off) for t, m in dyn_target_curve(ref["curve"], spans, co_end, float(ref["t1"]) - float(ref["t0"]))]
    if not tgt:
        return {"status": "skipped", "why": "empty reference envelope"}
    tt = np.asarray([t for t, _ in tgt])
    tv = np.asarray([v for _, v in tgt])
    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    tail = 0.5
    n_win = min(len(x), int(round((co_end + tail) * sr)))
    win = x[:n_win].copy()
    centres = tt - 0.2                                        # a momentary value measures [t - 0.4, t]
    lo_g, hi_g = float(dyn.get("min_db", -24.0)), float(dyn.get("max_db", 15.0))
    ceil_db = float(target["TP"]) - 1.0
    work = Path(str(wav) + ".dyn")
    work.mkdir(exist_ok=True)
    pre, lim_p = work / "pre.wav", work / "lim.wav"
    hist = []

    def solve(G, ceil, iters_):
        best = None
        for _ in range(iters_):
            sf.write(str(pre), _apply_gain_curve(win, sr, centres, G, co_end), sr, subtype="FLOAT")
            _limit_wav(ctx.cfg, pre, lim_p, ceil)
            env = D.envelope(ctx.cfg, lim_p, 0.0, co_end)
            et = np.asarray(env["t"], float)
            em = np.asarray(env["M"], float)
            ours = np.interp(tt, et, em) if len(et) else np.full(len(tt), -120.0)
            ok = (ours > -60) & (tv > -60)
            err = np.where(ok, tv - ours, 0.0)
            rec = {"iter": len(hist), "ceil_db": round(ceil, 2), "swing": D.swing(env), "I": env.get("I"),
                   "TP": env.get("TP"), "rms_err_lu": round(float(np.sqrt(np.mean(err[ok] ** 2))) if ok.any() else 0.0, 2)}
            hist.append(rec)
            if best is None or rec["rms_err_lu"] < best[0]["rms_err_lu"]:
                best = (dict(rec), sf.read(str(lim_p), dtype="float32", always_2d=True)[0], G.copy())
            if rec["rms_err_lu"] < 0.8:
                break
            G = np.clip(G + 0.8 * err, lo_g, hi_g)
        return best

    rec, yb, Gb = solve(np.zeros(len(tt)), ceil_db, iters)
    # the encoder overshoots sharp hits (AAC inter-sample peaks): measure the cold open through it and, when it
    # would cross the programme ceiling, limit ITS peaks lower and re-solve the envelope at that ceiling, so
    # the hits are limited here and the whole programme is never pulled down after the encode
    lim_extra = 0.0
    for _ in range(2):
        sf.write(str(lim_p), yb, sr, subtype="FLOAT")
        tp = _encoded_tp(ctx.cfg, lim_p, work / "enc.m4a")
        rec["TP_encoded"] = tp
        if tp is None or tp <= float(target["TP"]) - 0.2:
            break
        lim_extra += tp - float(target["TP"]) + 0.3
        rec, yb, Gb = solve(Gb, ceil_db - lim_extra, 3)
        rec["peak_limit_extra_db"] = round(lim_extra, 2)
    yb = yb[:n_win]
    m = min(len(yb), n_win)
    out = x.copy()
    xf = int(0.05 * sr)                                       # splice back inside the unity tail
    s0 = max(0, m - xf)
    out[:s0] = yb[:s0]
    r = np.linspace(1, 0, m - s0, dtype=np.float32)[:, None]
    out[s0:m] = yb[s0:m] * r + x[s0:m] * (1 - r)
    sf.write(str(wav), out, sr, subtype="PCM_24")
    shutil.rmtree(work, ignore_errors=True)
    info = {"status": "ok", "reference": rid, "co_end": round(co_end, 3), "spans": len(spans),
            "ref": {k: ref.get(k) for k in ("t0", "t1", "I", "swing", "M_max")},
            "result": rec, "iterations": hist, "gain_db": {"min": round(float(Gb.min()), 1), "max": round(float(Gb.max()), 1)},
            "curve": [[round(float(c), 2), round(float(g), 1)] for c, g in zip(centres, Gb)]}
    print(f"  cold-open dynamics: swing {rec['swing']} LU (reference {ref.get('swing')}), I {rec['I']} LUFS, "
          f"gain {info['gain_db']['min']}..{info['gain_db']['max']} dB, {len(hist)} pass(es)", flush=True)
    return info


def normalize_mix(ctx, mix_wav, out_wav, n, target, dialog_wav=None):
    """Programme loudness: measure, apply the exact gain, catch peaks with a 4x-oversampled limiter,
    re-measure and correct (limiting costs a little loudness). Deterministic, unlike loudnorm's
    dynamic fallback, which kicks in whenever peaks forbid a linear gain.
    anchor "dialogue" (default when there is dialogue): the gain puts the DIALOGUE at the target, so
    lifted music between lines cannot pull the talking down; the programme may then sit up to 1 LU over
    the target (PROGRAMME_OVER_LU), never more."""
    I_t = float(target["I"])
    ceil_db = float(target["TP"]) - 1.0                 # headroom for AAC inter-sample overshoot
    m0 = _measure(ctx.cfg, mix_wav)
    if not m0:
        ctx.warn("mix is silent or unmeasurable; skipping loudness normalization")
        shutil.copyfile(mix_wav, out_wav)
        return None
    gain = I_t - m0["I"]
    md = None
    if target.get("anchor", "dialogue") == "dialogue" and dialog_wav:
        md = _measure(ctx.cfg, dialog_wav)
        if md:
            # lift the programme so the talking reaches the target (music must not pull it down), up to
            # PROGRAMME_OVER_LU over the programme target; never lower than plain programme normalisation
            gain = max(I_t - m0["I"], min(I_t - md["I"], I_t + PROGRAMME_OVER_LU - m0["I"]))
    I_goal = m0["I"] + gain
    lim = 10 ** (ceil_db / 20)
    m = m0
    for _ in range(3):
        af = (f"aresample=192000,volume={gain:.3f}dB,alimiter=limit={lim:.5f}:attack=1:release=60:level=false,"
              f"aresample={SR},apad=whole_len={n},atrim=end_sample={n}")
        media.ffmpeg(ctx.cfg, ["-i", mix_wav, "-af", af, "-c:a", "pcm_s24le", "-ar", str(SR), out_wav])
        m = _measure(ctx.cfg, out_wav)
        if not m or abs(m["I"] - I_goal) < 0.3:
            break
        gain += I_goal - m["I"]
    return {"input": m0, "dialogue": md, "anchor": "dialogue" if md else "programme", "gain_db": round(gain, 2),
            "output": m}


# ============================================================================== main entry
def _select_sections(plan, only, cold_open=None):
    secs = ([(-1, cold_open)] if cold_open else []) + list(enumerate(plan["sections"]))
    if not only:
        return secs
    o = str(only).strip().lower()
    hit = [(i, s) for i, s in secs if str(i) == o or s.get("name", "").lower() == o or slugify(s.get("name", "")) == slugify(o)
           or (i == -1 and o in ("cold_open", "cold-open", "teaser"))]
    if len(hit) > 1:
        hit = [h for h in hit if h[0] != -1] or hit
    if not hit:
        raise P.PlanError(f"--only {only!r} matches no section; sections: {[s.get('name') for _, s in secs]}")
    return hit


def extra_validate(plan, con, cfg):
    errs = []
    from . import music
    errs += LK.check_files(cfg, ((plan.get("style") or {}).get("grade") or {}).get("look"), "style.grade.look")
    for si, sec in enumerate(plan.get("sections", [])):
        w0 = f"section {si} ({sec.get('name', '')})"
        mus = sec.get("music") or {}
        if mus.get("song"):
            try:
                music.song_info(cfg, con, mus["song"])
            except KeyError as e:
                p = Path(os.path.expanduser(str(mus["song"])))
                if not (p.exists() or (Path(cfg.work) / p).exists()):
                    errs.append(f"{w0} music: {e}")
            except Exception as e:  # noqa: BLE001
                errs.append(f"{w0} music: {e}")
        for gi, s in enumerate(sec.get("segments", [])):
            w = f"{w0} segment {gi}"
            t = s.get("type")
            if s.get("look") is not None:
                errs += LK.check_files(cfg, s["look"], f"{w} look")
            if s.get("fit", "cover") not in ("cover", "blur"):
                errs.append(f"{w}: fit must be 'cover' or 'blur'")
            c = s.get("crop", "center")
            if not (c in ("center", "face") or (isinstance(c, list) and len(c) == 4 and all(0 <= float(v) <= 1 for v in c))):
                errs.append(f"{w}: crop must be 'center', 'face' or [x, y, w, h] fractions")
            if float(s.get("speed", 1.0) or 1.0) <= 0:
                errs.append(f"{w}: speed must be > 0")
            if t in ("title", "black", "still") and float(s.get("dur", 1.0)) <= 0:
                errs.append(f"{w}: dur must be > 0")
            if t == "matchcut":
                al = (s.get("b") or {}).get("align")
                if al is not None and (not isinstance(al, dict) or float(al.get("scale", 1.0)) < 1.0):
                    errs.append(f"{w}: b.align must be {{scale>=1, dx, dy}}")
            if t == "broll" and s.get("voiceover"):
                vo = s["voiceover"]
                if vo.get("in") is None or vo.get("out") is None or float(vo["out"]) <= float(vo["in"]):
                    errs.append(f"{w}: voiceover needs clip, in < out")
                else:
                    try:
                        db.get_clip(con, vo["clip"])
                    except KeyError as e:
                        errs.append(f"{w} voiceover: {e}")
            if t == "title" and str(s.get("bg", "black")).startswith("blur:"):
                ref = str(s["bg"])[5:].rpartition("@")[0]
                try:
                    db.get_clip(con, ref)
                except KeyError as e:
                    errs.append(f"{w} bg: {e}")
            for k, cv in enumerate(s.get("cover", []) or []):
                try:
                    db.get_clip(con, cv["clip"] if isinstance(cv, dict) else cv)
                except KeyError as e:
                    errs.append(f"{w} cover {k}: {e}")
            for k, o in enumerate(s.get("overlays", []) or []):
                if not str(o.get("text", "")).strip():
                    errs.append(f"{w} overlay {k}: text is empty")
                if float(o.get("dur", 2.5)) <= 0:
                    errs.append(f"{w} overlay {k}: dur must be > 0")
            tr = P.transition_spec(s.get("transition")) if not P._validate_transition("", s.get("transition")) else None
            if tr and tr.get("sfx") and tr["sfx"] not in music.SFX and not os.path.exists(str(tr["sfx"])):
                errs.append(f"{w} transition: unknown sound {tr['sfx']!r} (built-ins: {sorted(music.SFX)} or a wav path)")
            for k, e in enumerate(s.get("sfx", []) or []):
                if e.get("name") not in music.SFX and not os.path.exists(str(e.get("name"))):
                    errs.append(f"{w} sfx {k}: unknown sound {e.get('name')!r} (built-ins: {sorted(music.SFX)} or a wav path)")
    return errs


def _rms_db(x, win):
    k = len(x) // win
    if k <= 0:
        return np.zeros(0)
    return 20 * np.log10(np.sqrt((x[:k * win].reshape(k, win).astype(np.float64) ** 2).mean(axis=1)) + 1e-9)


def room_tone_bed(ctx, rec, tl, voice_bus, spans):
    """Room tone under a whole talk segment: the clip's own quietest steady patch (from the CLEANED
    dialogue), looped with equal-power crossfades, laid from bed_fade before the first word-audio to
    bed_fade after the last, at max(the segment's floor, voice - 32 dB) (reference: ambience 30-36 dB under
    the voice, never a digital hole). Returns (start_sample, stereo array, info) or None."""
    from . import voice
    import soundfile as sf
    aud = ctx.style.get("audio") or {}
    first = tl.atoms[rec["atoms"][0]]
    au = first["audio"]
    if au["mode"] != "voice" or not au.get("path"):
        return None
    a, b = spans
    fade = float(aud.get("bed_fade_s", 0.3))
    i0, i1 = int(round(a * SR)), int(round(b * SR))
    seg = voice_bus[i0:i1].mean(axis=1)
    r = _rms_db(seg, int(0.01 * SR))
    r = r[r > -90]
    if len(r) < 20:
        return None
    loud = r[r >= np.percentile(r, 50)]
    voice_db = float(10 * np.log10(np.mean(10 ** (loud / 10))))
    start = max(0.0, a - fade)
    dur = (b + fade) - start
    try:
        path = voice.room_tone(ctx.cfg, au["clip"]["id"], round(dur, 2), src=au["path"])
    except Exception as e:  # noqa: BLE001
        ctx.warn(f"room tone for {au['clip'].get('rel')}: {e}")
        return None
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    if sr != SR or not len(x):
        return None
    x = x[:, :2] if x.shape[1] >= 2 else np.repeat(x, 2, axis=1)
    # measured with the same meter as the floors (20th percentile of 10 ms RMS), so the bed's audible
    # floor - not its average - matches the loudest piece's floor
    rb = _rms_db(x.mean(axis=1), int(0.01 * SR))
    rb = rb[rb > -100]
    if len(rb) < 10:
        return None
    cur = float(np.percentile(rb, 20))
    # the clip's own floor = its quietest steady patch at the gain the dialogue gets on the bus; the bed
    # fills up to max(that floor, voice - 32 dB) and never comes closer than 12 dB to the voice
    # the loudest piece floor sets the bed, so quieter (gated) pieces are filled up to it: no step
    floors = []
    for i in rec["atoms"]:
        at = tl.atoms[i]
        s0, S = atom_samples(at, ctx.fps)
        rr = _rms_db(voice_bus[s0:s0 + S].mean(axis=1), int(0.01 * SR))
        rr = rr[rr > -90]
        if len(rr) >= 30:
            floors.append(float(np.percentile(rr, 20)))    # p20: the floor you hear between words
    floor = max([cur + float(au.get("gain_db", 0.0) or 0.0)] + floors)
    target = min(max(floor, voice_db + float(aud.get("room_tone_under_voice_db", -32.0))), voice_db - 12.0)
    x = x * (10 ** ((target - cur) / 20))
    m = min(len(x), int(round(fade * SR)))
    if m > 1:
        ramp = np.sin(np.linspace(0, np.pi / 2, m, dtype=np.float32))[:, None]
        if start > 0:
            x[:m] *= ramp
        x[-m:] *= ramp[::-1]
    return int(round(start * SR)), x.astype(np.float32), {"t0": round(start, 3), "t1": round(start + len(x) / SR, 3),
                                                           "level_db": round(target, 1), "floor_db": round(floor, 1),
                                                           "voice_db": round(voice_db, 1), "clip": au["clip"]["id"]}


def _add(bus, s0, x):
    n = len(bus)
    a, b = max(0, s0), min(n, s0 + len(x))
    if b > a:
        bus[a:b] += x[a - s0:b - s0]


def build_buses(ctx, cfg, con, tl, segs, secs, parts, n_samples):
    """Dialogue / ambience / room-tone buses and the music bed, sample-exact on the frame grid.
    Pieces are ADDED (they overlap by their crossfade handles and J/L leads), ambience ducks under a
    J/L-cut voice, room tone runs under every talk segment, and only real dialogue ducks the music."""
    from . import music
    fps = ctx.fps
    aud = ctx.style.get("audio") or {}
    voice_bus = np.zeros((n_samples, 2), np.float32)
    nat_bus = np.zeros((n_samples, 2), np.float32)
    voice_spans, nat_spans = [], []
    for at, (_, wav) in zip(tl.atoms, parts):
        if not wav:
            continue
        s0, _ = atom_samples(at, fps)
        pre, tot = audio_span(at, fps)
        x = _read_wav(wav, tot)
        st = s0 - pre
        if at["audio"]["mode"] == "voice":
            _add(voice_bus, st, x)
            voice_spans.append((st / SR, (st + tot) / SR))
        else:
            _add(nat_bus, st, x)
            nat_spans.append((st / SR, (st + tot) / SR))
    vs = music._merge_spans(voice_spans, join=0.05)
    if vs and nat_spans:
        env = music.duck_envelope(n_samples, vs, 10 ** (float(aud.get("jl_duck_db", -12.0)) / 20), ramp=0.3)
        nat_bus *= env[:, None]
    beds = []
    bed_bus = np.zeros((n_samples, 2), np.float32)
    if aud.get("room_tone", True):
        for rec in segs:
            if rec["type"] not in ("talk", "broll") or not rec["atoms"] or rec["spec"].get("room_tone") is False:
                continue
            if rec["type"] == "broll" and not rec["spec"].get("voiceover"):
                continue
            ats = [tl.atoms[i] for i in rec["atoms"]]
            sp_ = [(at["f0"] / fps - float(at["audio"].get("pre", 0) or 0), at["f1"] / fps + float(at["audio"].get("post", 0) or 0))
                   for at in ats if at["audio"]["mode"] == "voice"]
            if not sp_:
                continue
            res = room_tone_bed(ctx, rec, tl, voice_bus, (min(a for a, _ in sp_), max(b for _, b in sp_)))
            if res:
                st, x, info = res
                _add(bed_bus, st, x)
                beds.append(info)
    dialog = voice_bus + nat_bus + bed_bus
    ctx._voice_bus = voice_bus
    # music
    speech = [(a, b) for a, b in vs]
    bed_secs = []
    for s in secs:
        m = s["music"]
        e = {"song": m["song"] if m else None, "offset": m["offset"] if m else 0.0, "start": s["start"],
             "dur": s["end"] - s["start"], "gain_db": m["gain_db"] if m else 0.0,
             "speech": [(a, b) for a, b in speech if b > s["start"] and a < s["end"]],
             "mode": m["mode"] if m else "full", "duck_db": m["duck_db"] if m else -18}
        if m:
            e.update(start_edge=m.get("start", "cut"), end_edge=m.get("end", "cut"),
                     lift_db=float(m.get("under_lift_db", 0.0) or 0.0) if m["mode"] == "under" else m.get("montage_gain_db", 0.0),
                     breaks=m.get("_breaks", []), under_db=m.get("under_db", -5.0), voice_lufs=DIALOG_LUFS,
                     natural=[(a, b) for a, b in nat_spans if b > s["start"] and a < s["end"]],
                     natural_duck_db=m.get("natural_duck_db", 0.0))
        bed_secs.append(e)
    mus = np.zeros((n_samples, 2), np.float32)
    if any(e["song"] for e in bed_secs):
        run_dir = ctx.cache / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        bed = run_dir / f"music_{_sha([time.time(), id(ctx)], 8)}.wav"
        music.music_bed(cfg, con, bed_secs, bed)
        mus = _read_wav(bed, n_samples)
        bed.unlink(missing_ok=True)
    return dialog, mus, {"room_tone": beds, "voice_spans": [[round(a, 3), round(b, 3)] for a, b in vs],
                         "music": [{k: v for k, v in e.items() if k not in ("speech", "natural")} for e in bed_secs if e["song"]]}


def _plan_file_sha(plan_path):
    """Content hash of the plan a render was made from (export.plan_sha): find_render uses it to tell a fresh
    render from one made before the plan file changed."""
    try:
        from .export import plan_sha
        return plan_sha(json.loads(Path(plan_path).read_text()))
    except (OSError, ValueError):
        return None


def render_plan(cfg, con, plan_path, preview=False, out=None, only=None):
    t_start = time.time()
    raw = P.load(plan_path)
    plan = P.normalize(raw, cfg)
    errs = P.validate(plan, con) + extra_validate(plan, con, cfg)
    if errs:
        raise P.PlanError("plan has problems:\n  " + "\n  ".join(errs))
    ctx = Ctx(cfg, con, plan, preview)
    co = build_cold_open(ctx, plan) if plan.get("cold_open") else None
    sections = _select_sections(plan, only, co)
    jobs = _jobs(cfg)
    print(f"render '{plan['title']}' v{plan['version']} · {ctx.W}x{ctx.H}@{ctx.fps:g} · "
          f"{'preview' if preview else 'final'} · {jobs} jobs · code {CODE_VERSION}", flush=True)

    tl, segs, secs = expand(ctx, sections)
    specs, sfx = apply_layers(ctx, tl, segs)
    total_f = tl.F
    print(f"  timeline: {len(tl.atoms)} atoms · {total_f / ctx.fps:.2f}s", flush=True)

    with ThreadPoolExecutor(jobs) as pool:
        prepare(ctx, tl, segs, specs, pool)
        # title cards are overlays too
        for at in tl.atoms:
            if at["kind"] == "title":
                sid = len(specs)
                specs.append({"id": sid, "kind": "title_card", "text": at["title"]["text"], "sub": at["title"]["sub"],
                              "position": "center", "dur": at["n"] / ctx.fps})
                at["title_spec"] = sid
        t_g = time.time()
        gfx = render_graphics(ctx, specs)
        if specs:
            print(f"  graphics: {len(specs)} overlays ({next(iter(gfx.values()))['renderer']}) in {time.time() - t_g:.1f}s", flush=True)
        t_r = time.time()
        futs = [pool.submit(render_atom, ctx, at, gfx) for at in tl.atoms]
        parts = []
        progress = bool(os.environ.get("CUTROOM_PROGRESS"))      # machine-readable lines for the studio
        for at, f in zip(tl.atoms, futs):
            parts.append(f.result())
            if progress:
                print(f"  progress {len(parts)}/{len(futs)}", flush=True)
        hits = sum(1 for at in tl.atoms if at.get("cached"))
        print(f"  segments: {len(parts)} ({hits} cached) in {time.time() - t_r:.1f}s", flush=True)

    # ---- concat + audio
    run_dir = ctx.cache / "runs" / _sha([plan_path, preview, only, time.time()], 10)
    run_dir.mkdir(parents=True, exist_ok=True)
    lst = run_dir / "list.txt"
    lst.write_text("".join(f"file '{v}'\n" for v, _ in parts))
    n_samples = int(round(total_f * SR / ctx.fps))
    dialog, mus, bus_info = build_buses(ctx, cfg, con, tl, segs, secs, parts, n_samples)
    mix = dialog + mus + sfx_track(ctx, sfx, n_samples)
    ctx.bus_info = bus_info
    import soundfile as sf
    mix_wav = run_dir / "mix.wav"
    sf.write(str(mix_wav), mix, SR, subtype="FLOAT")
    target = {"I": -14.0, "TP": -1.5, "LRA": 11, "anchor": "dialogue", **(plan["style"].get("loudness") or {})}
    dialog_wav = None
    vb = getattr(ctx, "_voice_bus", None)
    if vb is not None and float(np.abs(vb).max() if len(vb) else 0.0) > 1e-4:
        dialog_wav = run_dir / "dialog.wav"
        sf.write(str(dialog_wav), vb, SR, subtype="FLOAT")
    ctx._voice_bus = None
    norm_wav = run_dir / "mix_norm.wav"
    measured = normalize_mix(ctx, mix_wav, norm_wav, n_samples, target, dialog_wav)
    ctx.dynamics_info = None
    if co and dynamics_spec(plan.get("cold_open")) and any(r["sec"] == -1 for r in segs):
        ctx.dynamics_info = shape_cold_open(ctx, plan, tl, segs, norm_wav, target)

    # ---- final encode
    slug = slugify(plan["title"])
    name = f"{slug}_v{plan['version']}" + (f"_{slugify(sections[0][1].get('name', sections[0][0]))}" if only else "") \
        + ("_preview" if preview else "") + ".mp4"
    out = Path(out) if out else cfg.p("renders", name)
    out.parent.mkdir(parents=True, exist_ok=True)
    venc = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] if preview else
            ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high"])
    tmp_out = out.with_name(f".{out.stem}.tmp.mp4")
    gov = governor.for_cfg(cfg)
    with gov.slot("heavy", label=f"render final {name}"):
        gov.wait_for_disk(out.parent)
        media.ffmpeg(cfg, ["-f", "concat", "-safe", "0", "-i", lst, "-i", norm_wav, "-map", "0:v", "-map", "1:a",
                           *venc, "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
                           "-colorspace", "bt709", "-color_range", "tv", "-r", f"{ctx.fps:g}",
                           "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-movflags", "+faststart", tmp_out],
                     timeout=7200)
    loud = _ebur128(cfg, tmp_out)
    fix = 0.0
    cur_wav = norm_wav
    for k in range(3):                  # AAC overshoot on sharp transients (hits) can need more than one pass
        if loud["TP"] is None or loud["TP"] <= target["TP"]:
            break
        tmp2 = out.with_name(f".{out.stem}.tmp2.mp4")
        lw = run_dir / f"mix_peaks{k}.wav"
        res = relimit_overshoots(cfg, cur_wav, tmp_out, lw, float(target["TP"])) if k < 2 else None
        if res:
            # only the stretches whose ENCODED peak crosses the ceiling get limited lower; the rest of the
            # programme (and an already-verified cold open) keeps its level and its punch
            ctx.warn(f"true peak {loud['TP']} dBTP after encode; limiting the peaks {res['extra_db']:.2f} dB lower in "
                     f"{len(res['spans'])} spot(s) (programme level kept)")
            cur_wav, af = lw, []
        else:                           # last resort: the old whole-programme drop
            fix += target["TP"] - loud["TP"] - 0.1
            ctx.warn(f"true peak {loud['TP']} dBTP after encode; lowering programme by {-fix:.2f} dB")
            af = ["-af", f"volume={fix:.2f}dB"]
        media.ffmpeg(cfg, ["-i", tmp_out, "-i", cur_wav, "-map", "0:v", "-map", "1:a",
                           "-c:v", "copy", *af, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp2])
        os.replace(tmp2, tmp_out)
        loud = _ebur128(cfg, tmp_out)
    os.replace(tmp_out, out)

    # ---- sidecar
    side = sidecar(ctx, plan, plan_path, tl, segs, secs, specs, gfx, sfx, total_f, loud, target, measured,
                   time.time() - t_start, out, only)
    Path(str(out)[:-4] + ".json").write_text(json.dumps(side, indent=1))
    shutil.rmtree(run_dir, ignore_errors=True)
    print(f"done: {out} · {total_f / ctx.fps:.2f}s · I {loud['I']} LUFS · TP {loud['TP']} dBTP · "
          f"{time.time() - t_start:.1f}s", flush=True)
    return str(out)


def sidecar(ctx, plan, plan_path, tl, segs, secs, specs, gfx, sfx, total_f, loud, target, measured, secs_taken, out, only):
    fps = ctx.fps
    rows = []
    for at in tl.atoms:
        t0 = at["f0"] / fps
        r = {"i": at["i"], "section": at["section"], "seg": at["seg"], "type": at["type"], "kind": at["kind"],
             "start": round(t0, 4), "end": round(at["f1"] / fps, 4), "frames": at["n"],
             "clip": at["clip"]["id"] if at.get("clip") else None, "clip_rel": at["clip"]["rel"] if at.get("clip") else None,
             "source": at.get("src"), "in": round(at["a"], 4) if at.get("clip") else None,
             "out": round(at["a"] + at["n"] / fps * at["speed"], 4) if at.get("clip") else None,
             "speed": round(at["speed"], 4), "audio": at["audio"]["mode"],
             "audio_gain_db": at["audio"].get("gain_db"),
             "audio_pre": round(float(at["audio"].get("pre", 0) or 0), 4),
             "audio_post": round(float(at["audio"].get("post", 0) or 0), 4),
             "grade": {"chain": at.get("grade"), "stops": at.get("stops"), "luma_lut": at.get("luma_lut"),
                       "finish": ",".join(finish_chain(ctx)), "look": at.get("look")} if at.get("clip") else None,
             "conform": at.get("conform"), "push": at.get("push"), "ramp": at.get("ramp"),
             "transition_in": at.get("tr_in"), "teaser": bool(at.get("teaser")),
             "frame": ({"kind": at["frame"]["kind"], "window": [round(v, 4) for v in
                        (at["frame"]["inner"] or at["frame"])["window"]], "color": at["frame"]["color"]}
                       if at.get("frame") else None),
             "card": ({"text": at["card"].get("text"), "color": at["card"].get("color"), "size": at["card"].get("size")}
                      if at.get("card") else None),
             "lut_stats": at.get("lut_stats"),
             "crop": [round(v, 4) for v in at["rect"]] if at.get("rect") else None,
             "captions": [{"t0": round(t0 + c["t0"], 3), "t1": round(t0 + c["t1"], 3), "text": c["text"],
                           "position": c["position"], "px": c.get("px"),
                           "box": [round((c["x"] + c.get("pad", 0)) / ctx.W, 4), round((c["y"] + c.get("pad", 0)) / ctx.H, 4),
                                   round(max(1, c.get("box_w", 0) - 2 * c.get("pad", 0)) / ctx.W, 4),
                                   round(max(1, c.get("box_h", 0) - 2 * c.get("pad", 0)) / ctx.H, 4)]}
                          for c in at["captions"]],
             "overlays": [{"kind": specs[o["spec"]]["kind"], "text": specs[o["spec"]]["text"],
                           "position": specs[o["spec"]]["position"], "t0": round(t0 + o["t0"], 3),
                           "t1": round(t0 + o["t1"], 3), "renderer": gfx[o["spec"]]["renderer"],
                           **({"box": specs[o["spec"]]["box"]} if specs[o["spec"]].get("box") else {})}
                          for o in at["overlays"]],
             "cutaways": [{"clip": c["clip"]["id"], "clip_rel": c["clip"]["rel"], "in": round(c["a"], 3),
                           "t0": round(t0 + c["t0"], 3), "t1": round(t0 + c["t1"], 3), "auto": c["auto"]}
                          for c in at["cutaways"]],
             "cache_key": at.get("cache_key"), "cached": at.get("cached")}
        if at["kind"] == "title":
            r["title"] = at["title"]
        rows.append(r)
    devices = sorted({"burn" for t in getattr(ctx, "transitions", []) if t.get("kind") == "burn"} |
                     {"card" for at in tl.atoms if at["kind"] == "card"} |
                     {at["frame"]["kind"] for at in tl.atoms if at.get("frame")} |
                     {"typed_title" for sp in specs if sp.get("kind") == "typed_title"} |
                     ({"film"} if getattr(ctx, "film", None) else set()))
    return {"schema": "cutroom-render-v1", "title": plan["title"], "version": plan["version"],
            "plan": str(Path(plan_path).resolve()), "plan_sha": _plan_file_sha(plan_path),
            "output": str(out), "preview": ctx.preview, "only": only,
            "width": ctx.W, "height": ctx.H, "fps": fps, "frames": total_f, "duration": round(total_f / fps, 4),
            "code_version": CODE_VERSION, "render_seconds": round(secs_taken, 1),
            "font": ctx.font_pi[0], "lut": ctx.lut,
            "grade_style": {k: ctx.style["grade"].get(k) for k in ("normalize_exposure", "saturation", "look", "finish")},
            "film": getattr(ctx, "film", None), "devices": devices,
            "reference": getattr(ctx, "reference_info", None),
            "typed_titles": [{"text": sp["text"], "t0": round(sp["t0"], 3), "t1": round(sp["t1"], 3),
                              "start": float((sp.get("opts") or {}).get("start", 0.0)),
                              "char_s": float((sp.get("opts") or {}).get("char_s", CD.TITLE["char_s"])),
                              "font": gfx.get(sp["id"], {}).get("font"),
                              "pos": ({"x": sp["opts"]["x"], "y": sp["opts"]["y"]} if "x" in (sp.get("opts") or {}) else None),
                              "backing": bool((sp.get("opts") or {}).get("backing")), "pos_why": sp.get("pos_why")}
                             for sp in specs if sp.get("kind") == "typed_title"],
            "end_shot_choice": getattr(ctx, "end_shot_choice", None),
            "sections": [{"name": s["name"], "index": s["index"], "start": round(s["start"], 4), "end": round(s["end"], 4),
                          "music": {k: v for k, v in s["music"].items() if not k.startswith("_")} if s["music"] else None,
                          "music_breaks": (s["music"] or {}).get("_breaks", []),
                          "montage_cuts": s["montage_cuts"], "generated": s.get("generated")} for s in secs],
            "transitions": getattr(ctx, "transitions", []), "talk_joins": getattr(ctx, "talk_joins", []),
            "room_tone": (getattr(ctx, "bus_info", None) or {}).get("room_tone", []),
            "voice_spans": (getattr(ctx, "bus_info", None) or {}).get("voice_spans", []),
            "music_bed": (getattr(ctx, "bus_info", None) or {}).get("music", []),
            "captions_style": {"size": int(ctx.style["captions"].get("size", P.CAPTION_SIZE_1080)),
                               "px": max(10, int(round(int(ctx.style["captions"].get("size", P.CAPTION_SIZE_1080)) * ctx.H / 1080.0))),
                               "position": ctx.style["captions"].get("position", "bottom")},
            "matchcuts": [{"section": r["sec"], "seg": r["seg"], "cut_at": r.get("match_t")} for r in segs if r["type"] == "matchcut"],
            "segments": rows, "sfx": sfx,
            "loudness": {"target": target, "measured_before": measured, "final": loud},
            "cold_open_dynamics": getattr(ctx, "dynamics_info", None),
            "warnings": ctx.warnings}


# ============================================================================== resolve (no encoding)
# The studio's timeline view. Same expansion as render_plan (Ctx -> expand -> apply_layers), so piece
# times are exactly what a render produces, but nothing is encoded or probed: tightening uses the
# transcripts, beats come from the song index, clip lookups are cached.
_RESOLVE_CACHE = {}
_RESOLVE_LOCK = threading.Lock()
_RESOLVE_MAX = 48
_CLIP_CACHE = {}                         # (db path, ref) -> (time, row dict); 30 s TTL
_CLIP_TTL = 30.0


def _cached_clip(cfg, con, ref):
    key = (str(cfg.db_path), str(ref))
    hit = _CLIP_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CLIP_TTL:
        return dict(hit[1])
    row = dict(db.get_clip(con, ref))
    if len(_CLIP_CACHE) > 20000:
        _CLIP_CACHE.clear()
    _CLIP_CACHE[key] = (now, row)
    return dict(row)


class _ResolveCtx(Ctx):
    """A Ctx that never touches ffmpeg: no LUT copy, no font search, no probes, quiet warnings."""

    def __init__(self, cfg, con, plan, preview):           # noqa: super().__init__ deliberately skipped
        self.cfg, self.con, self.plan, self.preview = cfg, con, plan, preview
        o = plan["output"]
        self.fps = float(o["fps"])
        if preview:
            self.H = int(cfg.preview_height)
            self.W = int(round(o["width"] * self.H / o["height"] / 2)) * 2
        else:
            self.W, self.H = int(o["width"]), int(o["height"])
        self.s = self.H / 1080.0
        self.style = plan["style"]
        self.cache = cfg.p("cache", "render")
        self.warnings, self._warned = [], set()
        self.lock = threading.Lock()
        self._info, self._pregraded = {}, {}
        self.font_pi, self.lut = (None, None), ""
        self.deps = {}                                   # files whose change invalidates the result
        self._songs = {}

    def warn(self, msg, once_key=None):
        k = once_key or msg
        if k not in self._warned:
            self._warned.add(k)
            self.warnings.append(msg)

    def src_fps(self, row, src):                        # no ffprobe here: the library row's rate
        f = float(row.get("fps") or 0) if row else 0.0
        return _fps_q(f) if f > 0 else None

    def clip(self, ref):
        row = _cached_clip(self.cfg, self.con, ref)
        tp = self.cfg.p("transcripts", f"{row['id']}.json")
        self.deps[str(tp)] = _mtime(tp)
        return row

    def source(self, row):
        if self.preview and row.get("proxy"):
            return row["proxy"]
        return row["path"]

    def song(self, ref):
        if ref in self._songs:
            return self._songs[ref]
        from . import music
        try:
            d = music.song_info(self.cfg, self.con, ref)
        except KeyError:
            p = Path(os.path.expanduser(str(ref)))
            if not p.is_absolute():
                p = Path(self.cfg.work) / p
            if not p.exists():
                raise
            jp = self.cfg.p("cache", "beats", f"{_sha(_file_id(p))}.json")
            self.deps[str(jp)] = _mtime(jp)
            if jp.exists():
                d = {"path": str(p), "title": p.stem, **json.loads(jp.read_text())}
            else:                       # never run beat analysis here: that is seconds of librosa
                self.warn(f"song {ref} is not indexed (cutroom songs index); beats unknown until a render")
                d = {"path": str(p), "title": p.stem, "beats": [], "downbeats": [], "drops": [], "tempo": 0}
        self._songs[ref] = d
        return d


def _mtime(p):
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return None


def _seg_label(seg):
    t = seg.get("type")
    if t == "title":
        return seg.get("text", "title")
    if t == "black":
        return "black"
    if t == "card":
        return seg.get("text", "COMING UP")
    ref = seg.get("clip") or (seg.get("a") or {}).get("clip") or ""
    return f"{t} {Path(str(ref)).stem}".strip()


def _invalid_placeholder(seg, err):
    est = P.estimate_duration({"sections": [{"segments": [seg]}]}) if seg.get("type") in P.SEG_TYPES else 1.0
    try:
        est = float(est)
    except Exception:  # noqa: BLE001
        est = 1.0
    return {"type": "black", "dur": max(0.04, min(est if est > 0 else 1.0, 600.0)), "_invalid": str(err)}


def _expand_safe(ctx, plan):
    """expand + apply_layers; segments that cannot be expanded (missing clip, bad times) become
    placeholders of their estimated length so the rest of the timeline still resolves."""
    co = None
    if plan.get("cold_open"):
        try:
            co = build_cold_open(ctx, plan)
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"cold_open: {type(e).__name__}: {e}")
    sections = ([(-1, co)] if co else []) + list(enumerate(plan["sections"]))
    try:
        tl, segs, secs = expand(ctx, sections)
        specs, sfx = apply_layers(ctx, tl, segs)
        return tl, segs, secs, specs, sfx
    except Exception:  # noqa: BLE001  - find the culprits one segment at a time
        pass
    if co:
        try:
            expand(ctx, [(-1, co)])
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"cold_open: {type(e).__name__}: {e}")
            co = None
    fixed = json.loads(json.dumps(plan))
    for si, sec in enumerate(fixed["sections"]):
        mus = sec.get("music") or {}
        if mus.get("song"):
            try:
                ctx.song(mus["song"])
            except Exception as e:  # noqa: BLE001
                ctx.warn(f"section {si} music: {e}")
                sec["music"] = None
        for gi, seg in enumerate(sec.get("segments", [])):
            probe = {**sec, "music": sec.get("music"), "segments": [seg]}
            try:
                t2, s2, _ = expand(ctx, [(si, probe)])
                apply_layers(ctx, t2, s2)
            except Exception as e:  # noqa: BLE001
                ctx.warn(f"section {si} segment {gi}: {type(e).__name__}: {e}")
                sec["segments"][gi] = _invalid_placeholder(seg, e)
    sections = ([(-1, co)] if co else []) + list(enumerate(fixed["sections"]))
    tl, segs, secs = expand(ctx, sections)
    specs, sfx = apply_layers(ctx, tl, segs)
    return tl, segs, secs, specs, sfx


def resolve_timeline(cfg, con, plan_dict, preview=True):
    """Resolve a plan into timeline pieces without encoding anything.

    Returns {pieces, segments, sections, beats, duration, fps, warnings}. Pieces:
      {id, sec, seg, sub, type, kind, track, start, end, clip, clip_rel, in, out, speed, label,
       captions:[{t0, t1, text}], overlays:[{kind, text, t0, t1, position}]}   (times are timeline seconds,
    in/out are source seconds). Tracks: V1 main picture, V2 cutaways, V3 overlays, A1 dialogue/natural,
    A2 music. `segments` has one row per plan segment with its timeline span (and src_map for talk).
    Results are cached by plan hash + the transcripts/beat files they used; treat them as read-only."""
    plan = P.normalize(plan_dict, cfg)
    key = _sha([plan, bool(preview), str(cfg.work)], 24)
    with _RESOLVE_LOCK:
        hit = _RESOLVE_CACHE.get(key)
    if hit and all(_mtime(p) == m for p, m in hit["deps"].items()):
        return hit["res"]
    ctx = _ResolveCtx(cfg, con, plan, preview)
    fps = ctx.fps
    tl, segs, secs, specs, sfx = _expand_safe(ctx, plan)
    from . import asr
    cs = ctx.style["captions"]
    fixes = [tuple(f) for f in cs.get("fixes", []) or []]
    pieces = []
    sub_of = {}
    by_seg = {(r["sec"], r["seg"]): r for r in segs}
    # piece ids hash the segment's content (+ occurrence), so a selection survives moves and edits elsewhere
    seg_key, seen_keys = {}, {}
    for r in segs:
        h = _sha({k: v for k, v in r["spec"].items() if k != "_invalid"}, 8)
        n = seen_keys.get(h, 0)
        seen_keys[h] = n + 1
        seg_key[(r["sec"], r["seg"])] = h if n == 0 else f"{h}-{n}"
    for at in tl.atoms:
        t0, t1 = at["f0"] / fps, at["f1"] / fps
        k = (at["sec"], at["seg"])
        sub = sub_of.get(k, 0)
        sub_of[k] = sub + 1
        spec = by_seg[k]["spec"]
        row = at.get("clip")
        p = {"id": f"V1:{seg_key[k]}.{sub}", "sec": at["sec"], "seg": at["seg"], "sub": sub,
             "type": at["type"], "kind": at["kind"], "track": "V1", "start": round(t0, 4), "end": round(t1, 4),
             "clip": row["id"] if row else None, "clip_rel": row["rel"] if row else None,
             "in": round(at["a"], 4) if row else None,
             "out": round(at["a"] + at["n"] / fps * at["speed"], 4) if row else None,
             "speed": round(float(at["speed"] or 1.0), 4), "label": _seg_label(spec), "captions": [], "overlays": [],
             "look": at.get("look"), "transition_in": at.get("tr_in"), "push": at.get("push"), "ramp": at.get("ramp"),
             "conform": at.get("conform"), "teaser": bool(at.get("teaser")),
             "frame": at["frame"]["kind"] if at.get("frame") else None}
        if at["kind"] == "card":
            p["label"] = at["card"].get("text", "COMING UP")
        if at["sec"] == -1:
            p["generated"] = "cold_open"
        if spec.get("_invalid"):
            p.update(kind="invalid", label=f"invalid: {spec['_invalid']}"[:160])
        if at["kind"] == "title":
            p["label"] = at["title"]["text"]
            p["title"] = at["title"]
        if at["kind"] == "still":
            p["out"] = p["in"]
        if at.get("cue_src") and cs.get("enabled", True):
            cid, a, b = at["cue_src"]
            shift = float(at.get("cue_shift", 0.0))
            for c0, c1, text in asr.cues(asr.words(cfg, cid), a, b, max_words=int(cs.get("max_words", 5)),
                                         max_chars=int(cs.get("max_chars", 30)), fixes=fixes):
                c0, c1 = c0 - shift, c1 - shift          # heard time (J/L cuts can move a cue onto a neighbour)
                if c1 - c0 >= 0.1:
                    p["captions"].append({"t0": round(t0 + c0, 3), "t1": round(t0 + c1, 3), "text": text})
        for o in at["overlays"]:
            sp = specs[o["spec"]]
            p["overlays"].append({"kind": sp["kind"], "text": sp["text"], "sub": sp.get("sub", ""),
                                  "position": sp["position"], "t0": round(t0 + o["t0"], 3), "t1": round(t0 + o["t1"], 3)})
        pieces.append(p)
        # A1: the sound that plays under this atom (dialogue, natural or mixed camera audio)
        au = at["audio"]
        arow = au.get("clip")
        if au["mode"] != "none" and arow and arow.get("has_audio"):
            asp = float(au.get("speed", 1.0) or 1.0)
            pre, post = float(au.get("pre", 0) or 0), float(au.get("post", 0) or 0)
            pieces.append({"id": f"A1:{seg_key[k]}.{sub}", "sec": at["sec"], "seg": at["seg"], "sub": sub,
                           "type": at["type"], "kind": au["mode"], "track": "A1", "start": round(t0 - pre, 4),
                           "end": round(t1 + post, 4), "clip": arow["id"], "clip_rel": arow["rel"],
                           "in": round(au["a"] - pre * asp, 4),
                           "out": round(au["a"] + (at["n"] / fps + post) * asp, 4), "speed": round(asp, 4),
                           "lead_in": round(pre, 4) if pre > 0.03 else 0.0, "tail": round(post, 4) if post > 0.03 else 0.0,
                           "label": Path(arow["rel"] or "").stem, "captions": [], "overlays": [],
                           **({"generated": "cold_open"} if at["sec"] == -1 else {})})
    # V2: cutaways, merged back into one piece per cutaway where the atoms split it
    v2 = []
    for at in tl.atoms:
        T = at["f0"] / fps
        for c in at["cutaways"]:
            s0, s1 = T + c["t0"], T + c["t1"]
            last = v2[-1] if v2 else None
            if (last and last["clip"] == c["clip"]["id"] and abs(last["end"] - s0) < 1.5 / fps
                    and abs(last["out"] - c["a"]) < 1.5 / fps and last["seg"] == at["seg"] and last["sec"] == at["sec"]):
                last["end"] = round(s1, 4)
                last["out"] = round(c["a"] + (c["t1"] - c["t0"]), 4)
                continue
            v2.append({"id": "", "sec": at["sec"], "seg": at["seg"], "sub": 0, "type": "cutaway",
                       "kind": "auto_cover" if c["auto"] else "cutaway", "track": "V2", "start": round(s0, 4),
                       "end": round(s1, 4), "clip": c["clip"]["id"], "clip_rel": c["clip"]["rel"],
                       "in": round(c["a"], 4), "out": round(c["a"] + (c["t1"] - c["t0"]), 4), "speed": 1.0,
                       "label": Path(c["clip"]["rel"] or "").stem, "captions": [], "overlays": [], "auto": c["auto"],
                       "look": at.get("look"), **({"generated": "cold_open"} if at["sec"] == -1 else {})})
    count = {}
    for p in v2:
        k = (p["sec"], p["seg"])
        p["sub"] = count.get(k, 0)
        count[k] = p["sub"] + 1
        p["id"] = f"V2:{seg_key[k]}.{p['sub']}"
    pieces += v2
    # V3: one piece per overlay graphic (title cards stay on V1 as kind "title")
    count = {}
    for sp in specs:
        at = tl.atoms[sp["first_atom"]]
        k = (at["sec"], at["seg"])
        n = count.get(k, 0)
        count[k] = n + 1
        pieces.append({"id": f"V3:{seg_key[k]}.{n}", "sec": k[0], "seg": k[1], "sub": n, "type": "overlay",
                       "kind": sp["kind"], "track": "V3", "start": round(sp["t0"], 4), "end": round(sp["t1"], 4),
                       "clip": None, "clip_rel": None, "in": None, "out": None, "speed": 1.0,
                       "label": sp["text"], "text": sp["text"], "sub_text": sp.get("sub", ""),
                       "position": sp["position"], "captions": [], "overlays": []})
    # A2 + section bands
    sections, beats_all = [], []
    for s in secs:
        m = s["music"]
        band = {"i": s["index"], "index": s["index"], "name": s["name"], "start": round(s["start"], 4),
                "end": round(s["end"], 4), "music": None, "beats": []}
        if s.get("generated"):
            band["generated"] = s["generated"]
        if m:
            bs = [round(b, 4) for b in s["beats_tl"] if s["start"] - 1e-6 <= b <= s["end"] + 1e-6]
            msrc = (plan["sections"][s["index"]].get("music") or {}) if s["index"] >= 0 else {"song": m["song"]}
            sng = ctx._songs.get(msrc.get("song"), {}) or next((v for v in ctx._songs.values() if v.get("path") == m["song"]), {})
            band["music"] = {"song": sng.get("id") or m["song"], "path": m["song"], "title": m.get("title"),
                             "offset": m["offset"], "gain_db": m["gain_db"], "mode": m["mode"],
                             "start": m.get("start"), "end": m.get("end"), "montage_gain_db": m.get("montage_gain_db"),
                             "breaks": m.get("_breaks", []), "beats_in_section": bs}
            band["beats"] = bs
            beats_all += bs
            if s["end"] > s["start"]:
                pieces.append({"id": f"A2:{s['index']}", "sec": s["index"], "seg": None, "sub": 0, "type": "music",
                               "kind": "music", "track": "A2", "start": round(s["start"], 4), "end": round(s["end"], 4),
                               "clip": None, "clip_rel": None, "song": band["music"]["song"],
                               "in": round(m["offset"], 4), "out": round(m["offset"] + s["end"] - s["start"], 4),
                               "speed": 1.0, "label": m.get("title") or Path(m["song"]).stem, "captions": [],
                               "overlays": []})
        sections.append(band)
    segrows = []
    for r in segs:
        spec = r["spec"]
        row = {"sec": r["sec"], "seg": r["seg"], "type": r["type"], "start": round(r["t0"], 4),
               "end": round(r["t1"], 4), "label": _seg_label(spec), "invalid": spec.get("_invalid"),
               "clip": spec.get("clip"), "key": seg_key[(r["sec"], r["seg"])],
               "pieces": [f"V1:{seg_key[(r['sec'], r['seg'])]}.{k}" for k in range(len(r["atoms"]))]}
        if r["src_map"]:
            row["src_map"] = [[round(a, 4), round(b, 4), round(T, 4)] for a, b, T in r["src_map"]]
        if r.get("match_t") is not None:
            row["match_t"] = r["match_t"]
        segrows.append(row)
    res = {"pieces": pieces, "segments": segrows, "sections": sections, "beats": beats_all,
           "duration": round(tl.F / fps, 4), "frames": tl.F, "fps": fps, "width": ctx.W, "height": ctx.H,
           "sfx": [{"name": e["name"], "t": round(e["t"], 3), "gain_db": e["gain_db"], "align": e.get("align", "start"),
                    "on": e.get("on")} for e in sfx],
           "transitions": getattr(ctx, "transitions", []), "talk_joins": getattr(ctx, "talk_joins", []),
           "warnings": ctx.warnings, "hash": key,
           "look": LK.effective(ctx.style["grade"].get("look"), None)}
    with _RESOLVE_LOCK:
        if len(_RESOLVE_CACHE) >= _RESOLVE_MAX:
            _RESOLVE_CACHE.pop(next(iter(_RESOLVE_CACHE)))
        _RESOLVE_CACHE[key] = {"res": res, "deps": dict(ctx.deps)}
    return res


def locate(resolved, t, track="V1"):
    """Timeline time -> what plays there: {piece, sec, seg, seg_start, seg_end, src_t, clip} or None.
    src_t is the SOURCE time (tightened gaps and speed included) for clip pieces, None otherwise.
    A time exactly on a cut belongs to the piece that starts there; t past the end -> last piece."""
    ps = [p for p in resolved["pieces"] if p["track"] == track]
    if not ps:
        return None
    hit = next((p for p in ps if p["start"] - 1e-6 <= t < p["end"] - 1e-6), None)
    if hit is None:
        hit = ps[-1] if t >= ps[-1]["end"] - 1e-6 else ps[0]
    seg = next((s for s in resolved["segments"] if s["sec"] == hit["sec"] and s["seg"] == hit["seg"]), None)
    src = None
    if hit.get("clip") and hit.get("in") is not None:
        tt = min(max(t, hit["start"]), hit["end"])
        src = hit["in"] if hit["kind"] == "still" else round(hit["in"] + (tt - hit["start"]) * float(hit.get("speed") or 1.0), 4)
    return {"piece": hit, "sec": hit["sec"], "seg": hit["seg"], "seg_start": seg["start"] if seg else hit["start"],
            "seg_end": seg["end"] if seg else hit["end"], "src_t": src, "clip": hit.get("clip")}


def segment_src_to_timeline(resolved, sec, seg, s):
    """Inverse of locate for a talk segment: source time -> timeline time (a time in a removed pause
    maps to the start of the next piece)."""
    row = next((x for x in resolved["segments"] if x["sec"] == sec and x["seg"] == seg), None)
    if not row:
        return None
    if row.get("src_map"):
        return src_to_out({"src_map": row["src_map"]}, s)
    return None
