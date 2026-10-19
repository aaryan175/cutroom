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
from . import db
from . import media
from . import overlays as OV
from . import plan as P

SR = 48000
_SRC_FILES = ("render.py", "captions.py", "overlays.py", "media.py")
CODE_VERSION = hashlib.sha1(b"".join(Path(__file__).with_name(f).read_bytes() for f in _SRC_FILES
                                     if Path(__file__).with_name(f).exists())).hexdigest()[:12]

HOUSE_TAIL = "colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv420p:dither=fsb"
GENERIC_LOG = "format=gbrp,curves=all='0.09/0 0.25/0.13 0.41/0.4 0.6/0.68 0.8/0.9 1/1',format=yuv420p,eq=saturation=1.35"
LUMA_TARGET, LUMA_DEADBAND, EQ_BOUND = 118.0, 10.0, 0.08
DIALOG_LUFS, NATURAL_LUFS, MIX_LUFS = -16.0, -20.0, -26.0
DEFAULT_MUSIC_GAIN = -8.0
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
        out = {"w": w, "h": h, "range": v.get("color_range") or "", "duration": s["duration"],
               "has_audio": bool(s["has_audio"]), "pix_fmt": s.get("pix_fmt", "")}
        with self.lock:
            self._info[path] = out
        return out

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


def grade_chain(ctx, row, src, W, H):
    """Filters from a cropped picture to graded yuv420p WxH (scale is part of the chain: the LUT
    always runs at output size). Same chain for every shot of the same kind - one house look."""
    info = ctx.info(src)
    rng = _range_of(info)
    sat = float(ctx.style["grade"].get("saturation", 1.0) or 1.0)
    is_log = bool(row.get("is_log")) if row else False
    pre = ctx.pregraded(row, src) if row else False
    if is_log and ctx.lut and not pre:
        f = [f"scale={W}:{H}:flags=lanczos:in_range={rng}:out_range=full:in_color_matrix=bt709",
             "format=gbrpf32le", f"lut3d=file='{ctx.lut}':interp=tetrahedral", "format=gbrp16le", HOUSE_TAIL]
        tag = "lut"
    else:
        f = [f"scale={W}:{H}:flags=lanczos:in_range={rng}:out_range=tv:in_color_matrix=bt709:out_color_matrix=bt709",
             "format=yuv420p"]
        tag = "pregraded" if pre else "rec709"
        if is_log and not pre:
            f.append(GENERIC_LOG)
            tag = "generic-log"
            ctx.warn("log footage without a LUT: using a generic log->709 curve (set config lut or style.grade.lut)",
                     once_key="nolut")
    if abs(sat - 1.0) > 1e-3:
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


def picture(ctx, row, src, W, H, rect, eq=None, fit="cover", stabilize=False, label="p"):
    """Filtergraph fragment list turning an already-timed input into graded WxH yuv420p.
    Returns (list_of_chains_with_IN_and_OUT_placeholders)."""
    info = ctx.info(src)
    g, _ = grade_chain(ctx, row, src, W, H)
    eqs = []
    if eq and (abs(eq.get("brightness", 0)) > 1e-3 or abs(eq.get("gamma", 1) - 1) > 1e-3):
        eqs = [f"eq=brightness={eq['brightness']:.4f}:gamma={eq['gamma']:.4f}"]
    stab = []
    if stabilize:
        stab = [f"deshake=rx=48:ry=48:edge=mirror", f"scale={int(W * 1.06) // 2 * 2}:-2", f"crop={W}:{H}"]
    vertical = info["h"] > info["w"] and W > H
    if fit == "blur" and vertical:
        fw = int(round(H * info["w"] / info["h"] / 2)) * 2
        g_fg, _ = grade_chain(ctx, row, src, fw, H)
        return [f"[IN]split[{label}a][{label}b]",
                f"[{label}a]{','.join(g_fg + eqs)}[{label}f]",
                f"[{label}b]{','.join(g + eqs)},gblur=sigma={36 * ctx.s:.1f},eq=brightness=-0.08:saturation=0.9[{label}g]",
                f"[{label}g][{label}f]overlay=(W-w)/2:0{(',' + ','.join(stab)) if stab else ''},setsar=1,format=yuv420p[OUT]"]
    return [f"[IN]{_crop_px(rect, info)},{','.join(g + eqs + stab)},setsar=1,format=yuv420p[OUT]"]


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
    gf, tag = grade_chain(ctx, row, src, pw, ph)
    span = max(0.0, t1 - t0)
    times = [t0 + span * f for f in (0.15, 0.5, 0.85)]
    key = _sha(["probe2", _file_id(src), [round(t, 3) for t in times], gf, CODE_VERSION])
    jp = ctx.cache / f"probe_{key}.json"
    if jp.exists():
        return json.loads(jp.read_text())
    raw = ctx.cache / f"probe_{key}.raw"
    args, fc = [], []
    for k, t in enumerate(times):
        args += ["-ss", f"{max(0.0, min(t, info['duration'] - 0.1)):.3f}", "-i", src]
        fc.append(f"[{k}:v]trim=end_frame=1,setpts=PTS-STARTPTS,{','.join(gf)},format=gray[g{k}]")
    fc.append(f"{''.join(f'[g{k}]' for k in range(len(times)))}concat=n={len(times)}:v=1:a=0[o]")
    media.ffmpeg(ctx.cfg, args + ["-filter_complex", ";".join(fc), "-map", "[o]", "-f", "rawvideo", "-pix_fmt", "gray", raw])
    a = np.frombuffer(raw.read_bytes(), np.uint8)
    nfr = len(a) // (pw * ph)
    frames = a[: nfr * pw * ph].reshape(nfr, ph, pw)
    faces = []
    for fr in frames:
        faces += _faces_in(np.ascontiguousarray(fr))
    import cv2
    grids = [cv2.resize(fr, (64, 36), interpolation=cv2.INTER_AREA).astype(float).tolist() for fr in frames]
    res = {"faces": [list(map(float, f)) for f in faces], "grids": grids, "grade": tag}
    jp.write_text(json.dumps(res))
    raw.unlink(missing_ok=True)
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
          "captions": [], "overlays": [], "cutaways": [], "unit": None}
    return at


def expand(ctx, sections):
    """Flat atom list on the frame grid + per-segment mapping info + section spans."""
    tl = Timeline(ctx.fps)
    segs, secs = [], []
    cs = ctx.style["captions"]
    for si, sec in sections:
        s_start = tl.t
        mus = sec.get("music") or {}
        sng = song(ctx, mus["song"]) if mus.get("song") else None
        off = float(mus.get("offset", 0.0) or 0.0)
        beats = np.asarray(sng["beats"], float) if sng else np.asarray([])

        def song_t(t):                      # timeline -> song time inside this section
            return off + (t - s_start)

        def tl_t(st):
            return s_start + (st - off)
        sec_rec = {"index": si, "name": sec.get("name", f"section {si}"), "start": s_start, "music": None,
                   "montage_cuts": [], "beats_tl": [float(tl_t(b)) for b in beats] if sng else []}
        for gi, seg in enumerate(sec.get("segments", [])):
            t = seg["type"]
            rec = {"sec": si, "seg": gi, "type": t, "spec": seg, "atoms": [], "src_map": []}
            T0 = tl.t
            if t == "talk":
                row = ctx.clip(seg["clip"])
                pieces = _tighten(ctx, row["id"], float(seg["in"]), float(seg["out"]), float(seg.get("gap", 0.35))) \
                    if seg.get("tighten", True) else [(float(seg["in"]), float(seg["out"]))]
                unit = ("talk", si, gi)
                for a, b in pieces:
                    at = _base_atom(ctx, si, sec, gi, seg, "clip", row)
                    at.update(a=a, unit=unit, audio={"mode": "voice", "clip": row, "a": a})
                    tl.add(at, dur=b - a)
                    rec["src_map"].append((a, a + at["n"] / ctx.fps, at["f0"] / ctx.fps))
                    rec["atoms"].append(at["i"])
                    if cs.get("enabled", True) and seg.get("captions", True):
                        at["cue_src"] = (row["id"], a, a + at["n"] / ctx.fps)
            elif t == "broll":
                row = ctx.clip(seg["clip"])
                at = _base_atom(ctx, si, sec, gi, seg, "clip", row)
                sp = float(seg.get("speed", 1.0) or 1.0)
                d = (float(seg["out"]) - float(seg["in"])) / sp
                vo = seg.get("voiceover")
                if vo:
                    vrow = ctx.clip(vo["clip"])
                    vd = float(vo["out"]) - float(vo["in"])
                    if vd > d:
                        sp = max(0.5, sp * d / vd)
                    d = vd
                    at["audio"] = {"mode": "voice", "clip": vrow, "a": float(vo["in"])}
                    if cs.get("enabled", True) and seg.get("captions", True):
                        at["cue_src"] = (vrow["id"], float(vo["in"]), float(vo["out"]))
                    at["vo_unit"] = ("vo", si, gi)
                else:
                    mode = seg.get("audio", "music")
                    at["audio"] = {"mode": mode if mode in ("natural", "mix") else "none", "clip": row,
                                   "a": float(seg["in"]), "speed": sp}
                at.update(a=float(seg["in"]), speed=sp, unit=("seg", si, gi))
                tl.add(at, dur=d)
                rec["atoms"].append(at["i"])
            elif t == "montage":
                shots = seg["shots"]
                mode = seg.get("audio", "music")
                use_beats = bool(seg.get("beat_sync", True)) and len(beats) > 3
                if seg.get("beat_sync", True) and not use_beats:
                    ctx.warn(f"section '{sec_rec['name']}' montage {gi}: beat_sync needs section music with beats; "
                             "using planned shot lengths")
                ends = None
                if use_beats:
                    from . import music
                    step = float(np.median(np.diff(beats)))
                    bpc = max(1, int(seg.get("beats_per_cut", 1) or 1))
                    while step * bpc < float(seg.get("min_shot", 0.4)) - 1e-6:
                        bpc += 1
                    durs = music.montage_grid(beats, song_t(tl.t), len(shots), bpc)
                    acc, ends = song_t(tl.t), []
                    for d in durs:
                        acc += d
                        ends.append(tl_t(acc))
                for k, sh in enumerate(shots):
                    row = ctx.clip(sh["clip"])
                    at = _base_atom(ctx, si, sec, gi, {**seg, **{x: sh[x] for x in ("crop", "zoom", "fit", "stabilize") if x in sh}}, "clip", row)
                    want = (ends[k] - tl.t) if ends else None
                    if want is None:
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
                    if avail < want * sp:
                        sp = max(0.5, avail / want)
                    at.update(a=a, speed=sp, unit=("shot", si, gi, k),
                              audio={"mode": mode if mode in ("natural", "mix") else "none", "clip": row, "a": a, "speed": sp})
                    if ends:
                        tl.add(at, end_t=ends[k])
                        sec_rec["montage_cuts"].append(round(at["f1"] / ctx.fps, 4))
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
                n_pre = max(1, int(round(pre * ctx.fps)))
                mode = seg.get("audio", "music")
                aa = _base_atom(ctx, si, sec, gi, {**seg, **{x: A[x] for x in ("crop", "zoom") if x in A}}, "clip", ra)
                a_in = float(A["t"]) - n_pre / ctx.fps                 # lock end: last frame is the match frame
                aa.update(a=a_in, unit=("mcA", si, gi),
                          audio={"mode": mode if mode in ("natural", "mix") else "none", "clip": ra, "a": a_in})
                tl.add(aa, dur=n_pre / ctx.fps)
                bb = _base_atom(ctx, si, sec, gi, {**seg, **{x: B[x] for x in ("crop", "zoom") if x in B}}, "clip", rb)
                bb.update(a=float(B["t"]), align=B.get("align"), unit=("mcB", si, gi),   # lock start
                          audio={"mode": mode if mode in ("natural", "mix") else "none", "clip": rb, "a": float(B["t"])})
                tl.add(bb, dur=post)
                rec["atoms"] += [aa["i"], bb["i"]]
                rec["match_t"] = round(bb["f0"] / ctx.fps, 4)
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
            elif t == "still":
                row = ctx.clip(seg["clip"])
                at = _base_atom(ctx, si, sec, gi, seg, "still", row)
                at.update(a=float(seg["t"]), still_zoom=float(seg.get("zoom", 1.08)), unit=("seg", si, gi))
                tl.add(at, dur=float(seg.get("dur", 2.0)))
                rec["atoms"].append(at["i"])
            rec["t0"], rec["t1"] = T0, tl.t
            segs.append(rec)
        sec_rec["end"] = tl.t
        if sng:
            sec_rec["music"] = {"song": sng["path"], "title": sng.get("title"), "offset": off,
                                "gain_db": float(mus.get("gain_db", DEFAULT_MUSIC_GAIN)),
                                "duck_db": float(mus.get("duck_db", -18)),
                                "mode": mus.get("mode") or ctx.style.get("music_under_speech", "mute"),
                                "fade_in": float(mus.get("fade_in", 0.3)), "fade_out": float(mus.get("fade_out", 0.6))}
        secs.append(sec_rec)
    return tl, segs, secs


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
                          "position": o.get("position", "auto"), "dur": total, "first_atom": parts[0][0],
                          "t0": T0, "t1": T0 + total})
            for i, a0, a1, off in parts:
                tl.atoms[i]["overlays"].append({"spec": sid, "t0": a0, "t1": a1, "offset": off})
        cut = [dict(c) for c in seg.get("cutaways", []) or []]
        if talk and seg.get("auto_cover") and len(rec["src_map"]) > 1:
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
        for e in seg.get("sfx", []) or []:
            sfx.append({"name": e["name"], "t": S0 + float(e.get("at", 0.0)), "gain_db": float(e.get("gain_db", -6))})
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
        lj = loud_jobs.setdefault((key, au["mode"]), {"row": au["clip"], "t0": au["a"], "t1": au["a"], "mode": au["mode"]})
        lj["t0"] = min(lj["t0"], au["a"])
        lj["t1"] = max(lj["t1"], au["a"] + at["n"] / ctx.fps * float(au.get("speed", 1.0)))
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
        loud_jobs[k]["gain_db"] = 0.0 if I is None else max(-20.0, min(20.0, target - I))

    norm = bool(ctx.style["grade"].get("normalize_exposure", True))
    cs = ctx.style["captions"]
    fixes = [tuple(f) for f in cs.get("fixes", []) or []]
    cap_dir = ctx.cache / "captions"
    for at in tl.atoms:
        pr = probes.get(at["unit"]) if at["unit"] else None
        faces = pr["faces"] if pr else []
        if at["clip"] and at["kind"] in ("clip", "still", "title"):
            info = ctx.info(at["src"])
            at["rect"] = crop_rect(info["w"], info["h"], ctx.W, ctx.H, at["crop"], at["zoom"], faces, at["align"])
            at["eq"] = exposure_eq(luma_in(pr, at["rect"])) if norm else None
            at["grade"] = pr["grade"] if pr else ""
            at["faces_out"] = faces_out(faces, at["rect"], ctx.W, ctx.H)
        else:
            at["faces_out"] = []
        for c in at["cutaways"]:
            cp = probes.get(c["unit"])
            ci = ctx.info(c["src"])
            c["rect"] = crop_rect(ci["w"], ci["h"], ctx.W, ctx.H, c["crop"], c["zoom"], cp["faces"] if cp else [])
            c["eq"] = exposure_eq(luma_in(cp, c["rect"])) if norm else None
        au = at["audio"]
        if au["mode"] != "none" and au.get("clip") and au["clip"].get("has_audio"):
            lj = loud_jobs[(at.get("vo_unit") or at["unit"], au["mode"])]
            au.update(path=lj["path"], chain=lj["chain"], gain_db=round(lj["gain_db"], 2))
        elif au["mode"] != "none":
            au["mode"] = "none"
        # captions
        if at.get("cue_src"):
            cid, a, b = at["cue_src"]
            dur = at["n"] / ctx.fps
            for t0, t1, text in asr.cues(_words(ctx, cid), a, b, max_words=int(cs.get("max_words", 5)),
                                         max_chars=int(cs.get("max_chars", 30)), fixes=fixes):
                t1 = min(t1, dur)
                if t1 - t0 < 0.1:
                    continue
                png, w, h = CAP.cue_png(cap_dir, text, ctx.W, ctx.H, ctx.font_pi, int(cs.get("size", 54)), cs.get("case", "sentence"))
                pos = cs.get("position", "bottom")
                x, y = CAP.place(pos, w, h, ctx.W, ctx.H)
                busy = at["faces_out"] if not at["cutaways"] else []
                if pos != "top" and CAP.overlaps_face(x, y, w, h, busy):
                    pos = "top"
                    x, y = CAP.place(pos, w, h, ctx.W, ctx.H)
                at["captions"].append({"t0": round(t0, 3), "t1": round(t1, 3), "text": text, "png": png,
                                       "x": x, "y": y, "position": pos})
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
    if kind == "black" or (kind == "title" and not at["title"]["bg"].startswith("blur:")):
        i0 = inp("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fpsr}:d={d + 0.5:.3f}")
        fc.append(f"[{i0}:v]setsar=1,format=yuv420p[b0]")
    elif kind == "title":
        i0 = inp("-ss", f"{at['a']:.3f}", "-i", at["src"])
        files.append(at["src"])
        ch = picture(ctx, at["clip"], at["src"], W, H, at["rect"], at.get("eq"), label="t")
        fc += [c.replace("[IN]", f"[{i0}:v]trim=end_frame=1,setpts=PTS-STARTPTS,").replace("[OUT]", "[tp]") for c in ch]
        fc.append(f"[tp]gblur=sigma={30 * ctx.s:.1f},eq=brightness=-0.12:saturation=0.85,loop=loop={n + 2}:size=1:start=0,"
                  f"setpts=N/({fpsr}*TB),fps={fpsr}[b0]")
    elif kind == "still":
        i0 = inp("-ss", f"{at['a']:.3f}", "-i", at["src"])
        files.append(at["src"])
        W2, H2 = W * 2, H * 2
        ch = picture(ctx, at["clip"], at["src"], W2, H2, at["rect"], at.get("eq"), label="s")
        fc += [c.replace("[IN]", f"[{i0}:v]trim=end_frame=1,setpts=PTS-STARTPTS,").replace("[OUT]", "[sp]") for c in ch]
        z = max(1.0, float(at.get("still_zoom", 1.08)))
        fc.append(f"[sp]loop=loop={n + 2}:size=1:start=0,setpts=N/({fpsr}*TB),"
                  f"zoompan=z='1+({z - 1:.4f})*on/{max(1, n)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fpsr},"
                  f"setsar=1,format=yuv420p[b0]")
    else:
        sp = float(at["speed"] or 1.0)
        i0 = inp("-ss", f"{max(0.0, at['a']):.3f}", "-t", f"{d * sp + 0.5:.3f}", "-i", at["src"])
        files.append(at["src"])
        ch = picture(ctx, at["clip"], at["src"], W, H, at["rect"], at.get("eq"), at.get("fit", "cover"),
                     at.get("stabilize"), label="m")
        head = f"[{i0}:v]setpts=(PTS-STARTPTS)/{sp:.5f},fps={fpsr},"
        fc += [c.replace("[IN]", head).replace("[OUT]", "[b0]") for c in ch]
    last = "b0"
    k = 0
    # cutaways: b-roll picture over the main picture, main audio continues
    for c in at["cutaways"]:
        k += 1
        cd = c["t1"] - c["t0"]
        ci = inp("-ss", f"{max(0.0, c['a']):.3f}", "-t", f"{cd + 0.5:.3f}", "-i", c["src"])
        files.append(c["src"])
        ch = picture(ctx, c["clip"], c["src"], W, H, c["rect"], c.get("eq"), label=f"c{k}")
        fc += [x.replace("[IN]", f"[{ci}:v]setpts=PTS-STARTPTS,fps={fpsr},")
                .replace("[OUT]", f"[cx{k}]") for x in ch]
        fc.append(f"[cx{k}]tpad=stop_mode=clone:stop_duration=1,setpts=PTS+{c['t0']:.4f}/TB[cv{k}]")
        fc.append(f"[{last}][cv{k}]overlay=eof_action=pass:enable='between(t,{c['t0']:.4f},{c['t1'] - 0.5 / fps:.4f})'[x{k}]")
        last = f"x{k}"
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
    fc.append(f"[{last}]format=yuv420p,tpad=stop_mode=clone:stop_duration={d + 1:.3f},trim=end_frame={n},setpts=PTS-STARTPTS[v]")
    return args, fc, files


def atom_samples(at, fps):
    """Exact sample span of an atom from the global frame grid (no drift over hundreds of cuts)."""
    s0 = int(round(at["f0"] * SR / fps))
    return s0, int(round(at["f1"] * SR / fps)) - s0


def build_audio_cmd(ctx, at):
    """ffmpeg args for an atom's dialogue/natural audio (None when the atom is silent). Audio runs in
    its own process: an audio output in the same graph as the overlay compositing deadlocks
    ffmpeg 8's scheduler intermittently (measured ~15% of runs)."""
    au = at["audio"]
    if au["mode"] == "none":
        return None, []
    d = at["n"] / ctx.fps
    _, S = atom_samples(at, ctx.fps)
    asp = float(au.get("speed", 1.0) or 1.0)
    chain = [f"aresample={SR}", "aformat=sample_fmts=fltp:channel_layouts=stereo"] + _atempo(asp)
    if au.get("chain"):
        chain.append(au["chain"])
    chain.append(f"volume={au.get('gain_db', 0.0):.2f}dB")
    fo = 0.015                                   # short de-click fades: jump cuts stay tight
    chain += ["afade=t=in:d=0.012", f"afade=t=out:st={max(0.0, d - fo):.4f}:d={fo}",
              f"apad=whole_len={S}", f"atrim=end_sample={S}", "asetpts=N/SR/TB"]
    args = ["-ss", f"{max(0.0, au['a']):.3f}", "-t", f"{d * asp + 0.5:.3f}", "-i", au["path"], "-vn",
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
        # stall guard: generous per-second budget, one retry (ffmpeg's scheduler can wedge on rare graphs)
        budget = max(180.0, at["n"] / ctx.fps * (25 if ctx.preview else 90))
        try:
            for attempt in (1, 2):
                try:
                    media.ffmpeg(ctx.cfg, args + ["-filter_complex_script", script, "-map", "[v]", *enc, "-f", "mov", tmp],
                                 timeout=budget)
                    break
                except subprocess.TimeoutExpired:
                    if attempt == 2:
                        raise _atom_err(at, f"ffmpeg stalled twice (> {budget:.0f}s)") from None
                    ctx.warn(f"atom {at['i']}: ffmpeg stalled after {budget:.0f}s; retrying once")
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
        s = int(round(e["t"] * SR))
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


def normalize_mix(ctx, mix_wav, out_wav, n, target):
    """Programme loudness: measure, apply the exact gain, catch peaks with a 4x-oversampled limiter,
    re-measure and correct (limiting costs a little loudness). Deterministic, unlike loudnorm's
    dynamic fallback, which kicks in whenever peaks forbid a linear gain."""
    I_t = float(target["I"])
    ceil_db = float(target["TP"]) - 1.0                 # headroom for AAC inter-sample overshoot
    m0 = _measure(ctx.cfg, mix_wav)
    if not m0:
        ctx.warn("mix is silent or unmeasurable; skipping loudness normalization")
        shutil.copyfile(mix_wav, out_wav)
        return None
    gain = I_t - m0["I"]
    lim = 10 ** (ceil_db / 20)
    m = m0
    for _ in range(3):
        af = (f"aresample=192000,volume={gain:.3f}dB,alimiter=limit={lim:.5f}:attack=1:release=60:level=false,"
              f"aresample={SR},apad=whole_len={n},atrim=end_sample={n}")
        media.ffmpeg(ctx.cfg, ["-i", mix_wav, "-af", af, "-c:a", "pcm_s24le", "-ar", str(SR), out_wav])
        m = _measure(ctx.cfg, out_wav)
        if not m or abs(m["I"] - I_t) < 0.3:
            break
        gain += I_t - m["I"]
    return {"input": m0, "gain_db": round(gain, 2), "output": m}


# ============================================================================== main entry
def _select_sections(plan, only):
    secs = list(enumerate(plan["sections"]))
    if not only:
        return secs
    o = str(only).strip().lower()
    hit = [(i, s) for i, s in secs if str(i) == o or s.get("name", "").lower() == o or slugify(s.get("name", "")) == slugify(o)]
    if not hit:
        raise P.PlanError(f"--only {only!r} matches no section; sections: {[s.get('name') for _, s in secs]}")
    return hit


def extra_validate(plan, con, cfg):
    errs = []
    from . import music
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
            for k, e in enumerate(s.get("sfx", []) or []):
                if e.get("name") not in music.SFX and not os.path.exists(str(e.get("name"))):
                    errs.append(f"{w} sfx {k}: unknown sound {e.get('name')!r} (built-ins: {sorted(music.SFX)} or a wav path)")
    return errs


def render_plan(cfg, con, plan_path, preview=False, out=None, only=None):
    t_start = time.time()
    raw = P.load(plan_path)
    plan = P.normalize(raw, cfg)
    errs = P.validate(plan, con) + extra_validate(plan, con, cfg)
    if errs:
        raise P.PlanError("plan has problems:\n  " + "\n  ".join(errs))
    ctx = Ctx(cfg, con, plan, preview)
    sections = _select_sections(plan, only)
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
        gfx = OV.render_all(specs, ctx.W, ctx.H, ctx.fps, ctx.cache / "gfx", ctx.font_pi, warn=ctx.warn)
        if specs:
            print(f"  graphics: {len(specs)} overlays ({next(iter(gfx.values()))['renderer']}) in {time.time() - t_g:.1f}s", flush=True)
        t_r = time.time()
        futs = [pool.submit(render_atom, ctx, at, gfx) for at in tl.atoms]
        parts = []
        for at, f in zip(tl.atoms, futs):
            parts.append(f.result())
        hits = sum(1 for at in tl.atoms if at.get("cached"))
        print(f"  segments: {len(parts)} ({hits} cached) in {time.time() - t_r:.1f}s", flush=True)

    # ---- concat + audio
    run_dir = ctx.cache / "runs" / _sha([plan_path, preview, only, time.time()], 10)
    run_dir.mkdir(parents=True, exist_ok=True)
    lst = run_dir / "list.txt"
    lst.write_text("".join(f"file '{v}'\n" for v, _ in parts))
    n_samples = int(round(total_f * SR / ctx.fps))
    dialog = np.zeros((n_samples, 2), np.float32)       # dialogue / natural bus, sample-exact placement
    for at, (_, wav) in zip(tl.atoms, parts):
        if wav:
            s0, S = atom_samples(at, ctx.fps)
            dialog[s0:s0 + S] = _read_wav(wav, S)

    from . import music
    speech = [(tl.atoms[i]["f0"] / ctx.fps, tl.atoms[i]["f1"] / ctx.fps) for i in range(len(tl.atoms))
              if tl.atoms[i]["audio"]["mode"] in ("voice", "natural")]
    bed_secs = []
    for s in secs:
        m = s["music"]
        e = {"song": m["song"] if m else None, "offset": m["offset"] if m else 0.0, "start": s["start"],
             "dur": s["end"] - s["start"], "gain_db": m["gain_db"] if m else 0.0,
             "speech": [(a, b) for a, b in speech if b > s["start"] and a < s["end"]],
             "mode": m["mode"] if m else "full", "duck_db": m["duck_db"] if m else -18}
        if m:
            e.update(fade_in=m["fade_in"], fade_out=m["fade_out"])
        bed_secs.append(e)
    bed = run_dir / "music.wav"
    if any(e["song"] for e in bed_secs):
        music.music_bed(cfg, con, bed_secs, bed)
        mus = _read_wav(bed, n_samples)
    else:
        mus = np.zeros((n_samples, 2), np.float32)
    mix = dialog + mus + sfx_track(ctx, sfx, n_samples)
    import soundfile as sf
    mix_wav = run_dir / "mix.wav"
    sf.write(str(mix_wav), mix, SR, subtype="FLOAT")
    target = {"I": -14.0, "TP": -1.5, "LRA": 11, **(plan["style"].get("loudness") or {})}
    norm_wav = run_dir / "mix_norm.wav"
    measured = normalize_mix(ctx, mix_wav, norm_wav, n_samples, target)

    # ---- final encode
    slug = slugify(plan["title"])
    name = f"{slug}_v{plan['version']}" + (f"_{slugify(sections[0][1].get('name', sections[0][0]))}" if only else "") \
        + ("_preview" if preview else "") + ".mp4"
    out = Path(out) if out else cfg.p("renders", name)
    out.parent.mkdir(parents=True, exist_ok=True)
    venc = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] if preview else
            ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high"])
    tmp_out = out.with_name(f".{out.stem}.tmp.mp4")
    media.ffmpeg(cfg, ["-f", "concat", "-safe", "0", "-i", lst, "-i", norm_wav, "-map", "0:v", "-map", "1:a",
                       *venc, "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
                       "-colorspace", "bt709", "-color_range", "tv", "-r", f"{ctx.fps:g}",
                       "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-movflags", "+faststart", tmp_out], timeout=7200)
    loud = _ebur128(cfg, tmp_out)
    if loud["TP"] is not None and loud["TP"] > target["TP"]:
        fix = target["TP"] - loud["TP"] - 0.1
        ctx.warn(f"true peak {loud['TP']} dBTP after encode; lowering programme by {-fix:.2f} dB")
        tmp2 = out.with_name(f".{out.stem}.tmp2.mp4")
        media.ffmpeg(cfg, ["-i", tmp_out, "-i", norm_wav, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                           "-af", f"volume={fix:.2f}dB", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp2])
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
             "grade": {"chain": at.get("grade"), "eq": at.get("eq")} if at.get("clip") else None,
             "crop": [round(v, 4) for v in at["rect"]] if at.get("rect") else None,
             "captions": [{"t0": round(t0 + c["t0"], 3), "t1": round(t0 + c["t1"], 3), "text": c["text"],
                           "position": c["position"]} for c in at["captions"]],
             "overlays": [{"kind": specs[o["spec"]]["kind"], "text": specs[o["spec"]]["text"],
                           "position": specs[o["spec"]]["position"], "t0": round(t0 + o["t0"], 3),
                           "t1": round(t0 + o["t1"], 3), "renderer": gfx[o["spec"]]["renderer"]} for o in at["overlays"]],
             "cutaways": [{"clip": c["clip"]["id"], "clip_rel": c["clip"]["rel"], "in": round(c["a"], 3),
                           "t0": round(t0 + c["t0"], 3), "t1": round(t0 + c["t1"], 3), "auto": c["auto"]}
                          for c in at["cutaways"]],
             "cache_key": at.get("cache_key"), "cached": at.get("cached")}
        if at["kind"] == "title":
            r["title"] = at["title"]
        rows.append(r)
    return {"schema": "cutroom-render-v1", "title": plan["title"], "version": plan["version"],
            "plan": str(Path(plan_path).resolve()), "output": str(out), "preview": ctx.preview, "only": only,
            "width": ctx.W, "height": ctx.H, "fps": fps, "frames": total_f, "duration": round(total_f / fps, 4),
            "code_version": CODE_VERSION, "render_seconds": round(secs_taken, 1),
            "font": ctx.font_pi[0], "lut": ctx.lut,
            "sections": [{"name": s["name"], "index": s["index"], "start": round(s["start"], 4), "end": round(s["end"], 4),
                          "music": s["music"], "montage_cuts": s["montage_cuts"]} for s in secs],
            "matchcuts": [{"section": r["sec"], "seg": r["seg"], "cut_at": r.get("match_t")} for r in segs if r["type"] == "matchcut"],
            "segments": rows, "sfx": sfx,
            "loudness": {"target": target, "measured_before": measured, "final": loud},
            "warnings": ctx.warnings}
