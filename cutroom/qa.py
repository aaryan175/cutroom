"""Machine QA for a rendered video: measure everything a reviewer would catch, then hand the operator
contact sheets to look at before delivering.

    report = qa.run(cfg, "renders/trip_v3_preview.mp4")        # also writes trip_v3_preview.qa.json/.qa.html
    print(report["summary"])                                   # + trip_v3_preview.qa/ (sheets + evidence)

Two ffmpeg decodes run in parallel:
  video   one pass at 480 px wide (every frame): blackdetect + freezedetect inside the graph, and a raw
          pipe that feeds frame differences (cut detection), luma, sharpness, LK camera shake and a small
          JPEG cache used for faces, evidence frames and contact sheets
  audio   one pass: ebur128 (integrated loudness, true peak, momentary loudness timeline) and a
          16 kHz mono copy for silence holes and the music-under-speech floor

The renderer's sidecar (`<stem>.json` next to the video, see docs/PLAN_FORMAT.md) makes QA much sharper:
planned cuts are checked against the decoded cuts, repeats are checked by source clip, captions and
overlays are checked against faces, and intentional stills / black / titles are not flagged. Without a
sidecar QA falls back to detected shots only. Accepted sidecar shape (aliases are tolerated):

  {"fps": 25, "width": 1920, "height": 1080, "version": 3,
   "segments": [{"start": 0.0, "end": 1.24, "kind": "montage", "section": "cold open",
                 "clip": "<clip id>", "path": "/abs/source.mov", "in": 12.0, "out": 13.24,
                 "captions": [{"start": 0.1, "end": 1.0, "text": "...", "box": [x, y, w, h]}],
                 "overlays": [{"start": 0.3, "end": 2.8, "kind": "callout", "position": "top-right",
                               "box": [x, y, w, h]}]}],
   "captions": [...], "overlays": [...]}                       # optional top-level lists, same fields

Times are timeline seconds; boxes are fractions of the frame. A caption/overlay may instead give
"at"/"dur" relative to its segment start.

Every check yields pass / warn / fail with timestamps and evidence frame paths. Thresholds follow
docs/EDITING_RULES.md (rule numbers in the check table below).
"""
import html as _html
import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

# -- thresholds (rule numbers from docs/EDITING_RULES.md) -------------------------------------------
DECODE_W = 480                 # analysis width (same as analyze.py, so shake numbers are comparable)
SHAKE_FPS = 12.5               # analyze.py measures camera shake at 12.5 fps
LUFS_RANGE = (-16.0, -13.0)    # integrated loudness target for web delivery
LUFS_SLACK = 1.5               # within this many LU outside the range = warn, beyond = fail
TP_MAX = -1.5                  # rule 82
HOLE_WIN, HOLE_DB = 0.25, -65.0  # rule 80
UNDER_SPEECH_DB = -40.0        # rule 70: anything continuous under talk louder than this
DARK_LUMA = 35.0               # rules 49/90/99
SHARP_FLOOR = 20.0             # variance of Laplacian at 480 px (analyze.auto_quality uses the same floor)
SHAKE_WARN, SHAKE_FAIL = 8.0, 14.0   # analyze.py scale: <3 steady, >8 shaky; 14 = the rejected clip in rule 84
INVISIBLE_CUT = 20.0           # rule 31: mean abs luma diff across a cut (0-255)
NEAR_DUP_CORR = 0.8            # rule 92
REPEAT_WINDOW = 10.0           # rule 92
MIN_MONTAGE_SHOT = 0.3
FLOOR_STEP_MEDIAN, FLOOR_STEP_P90 = 3.0, 8.0   # room tone across talk joins (reference: 2.2 / 4.9 dB)
TALK_MIN_WINDOW_DB = -55.0      # quietest 250 ms inside talk (reference -39; a gated cut sits at -65..-73)
BLACK_P1_RANGE = (8.0, 35.0)    # median black point (p1 luma, 0-255): not crushed and not milky. Reference 8,
                                # the approved LUT-only house grade 29-41 on daylight; v2's "insane" grade 2-4
BLACK_P1_MILKY = BLACK_P1_RANGE[1]
BLACK_P1_SHOT = 45.0            # one shot's black point this high is visibly lifted
BLACK_P1_CRUSH_SHOT = 4.0       # one daylight shot's black point this low is crushed
GRADE_LUMA_TOL = 15.0           # grade_bounds: rendered vs LUT-only (same source frame), luma 0-255
GRADE_P1_DROP = 12.0            #   p1 may fall at most this far below the LUT-only black point
GRADE_SAT_GAIN = 1.15           #   saturation at most +15 % over LUT-only
SMALL_FACE_H = 0.08             # face detections smaller than this (frame height) inside a caption band are text, not faces
DUP_RATE = 0.8                  # duplicated frames per second in a moving shot (24 -> 25 judder ~1/s)
CAPTION_PCT = (3.3, 4.7)        # caption FONT px as % of frame height. Reference: 43 px at 1080 = 3.98 %
                                # (its visible text height measures ~2.96 %; don't compare font size to text height)
CAPTION_REF_PCT = 3.98
SFX_TOL_S = 0.005               # transition sound transient vs the cut frame
EMPTY_MOTION = 0.6             # px/frame camera motion below which a face-less shot may be an empty plate
EMPTY_ACTIVITY = 1.5           # mean abs frame diff (64x36, 0-255) below which nothing moves in frame
SHEET_COLS, SHEET_ROWS = 4, 6  # 24 tiles per sheet
MAX_EVIDENCE = 400
JPEG_Q = 72

INTENTIONAL_STILL = {"still", "title", "black", "card", "freeze"}
INTENTIONAL_BLACK = {"black", "title", "fade", "card"}
TALK_KINDS = {"talk", "speech", "aroll", "a-roll"}
MONTAGE_KINDS = {"montage", "mont", "teaser", "tease"}

SEV_ORDER = {"pass": 0, "info": 0, "warn": 1, "fail": 2}


# ==================================================================================================
# small helpers
# ==================================================================================================

def fmt_t(t) -> str:
    t = max(0.0, float(t or 0))
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _ffbin(cfg, name):
    return getattr(cfg, name, None) or name


def _font(size):
    from PIL import ImageFont
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf", "/Library/Fonts/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


_cascade = None


def _faces_detector():
    """OpenCV's bundled frontal-face Haar cascade (None if this OpenCV build lacks it)."""
    global _cascade
    if _cascade is None:
        import cv2
        try:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            _cascade = c if not c.empty() else False
        except AttributeError:
            _cascade = False
    return _cascade or None


def detect_faces(bgr) -> list:
    """Face boxes as fractions [x, y, w, h] of the frame."""
    import cv2
    cas = _faces_detector()
    if cas is None or bgr is None:
        return []
    h, w = bgr.shape[:2]
    g = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    m = max(18, int(min(w, h) * 0.07))
    boxes = cas.detectMultiScale(g, scaleFactor=1.15, minNeighbors=6, minSize=(m, m))
    return [[x / w, y / h, bw / w, bh / h] for (x, y, bw, bh) in (boxes if len(boxes) else [])]


_hog = None


def detect_people(bgr) -> list:
    """Person boxes (fractions) from OpenCV's HOG people detector at 960 px. Coarse: it misses seated
    or partial people sometimes, so it only ever clears an "empty plate" warning, never raises one."""
    import cv2
    global _hog
    if bgr is None:
        return []
    if _hog is None:
        _hog = cv2.HOGDescriptor()
        _hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    h, w = bgr.shape[:2]
    s = 960.0 / max(w, h)
    im = cv2.resize(bgr, (int(w * s), int(h * s))) if abs(s - 1) > 0.05 else bgr
    boxes, _ = _hog.detectMultiScale(im, winStride=(8, 8), padding=(16, 16), scale=1.05)
    H, Wd = im.shape[:2]
    return [[x / Wd, y / H, bw / Wd, bh / H] for (x, y, bw, bh) in (boxes if len(boxes) else [])]


def head_box(f):
    """Grow a Haar face box (brows to chin) to the whole head: hair above, beard/jaw below, ears at the sides."""
    x, y, w, h = f
    return [x - 0.18 * w, y - 0.35 * h, w * 1.36, h * 1.75]


def overlap(a, b) -> float:
    """Intersection area of two [x, y, w, h] boxes as a fraction of box a."""
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-9, a[2] * a[3])


CAPTION_BANDS = {"bottom": [0.08, 0.76, 0.84, 0.17], "lower": [0.08, 0.64, 0.84, 0.17],
                 "top": [0.08, 0.05, 0.84, 0.16], "center": [0.1, 0.42, 0.8, 0.16]}
OVERLAY_BOXES = {"top-left": [0.03, 0.04, 0.36, 0.24], "top-right": [0.61, 0.04, 0.36, 0.24],
                 "bottom-left": [0.03, 0.66, 0.36, 0.26], "bottom-right": [0.61, 0.66, 0.36, 0.26],
                 "center": [0.25, 0.32, 0.5, 0.36], "lower_third": [0.04, 0.70, 0.55, 0.18]}


# ==================================================================================================
# sidecar / plan
# ==================================================================================================

def _norm_items(items, seg_start=0.0, default_kind=""):
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        s = _first(it, "start", "t0", "timeline_start")
        e = _first(it, "end", "t1", "timeline_end")
        if s is None and it.get("at") is not None:
            s = seg_start + float(it["at"])
        if s is None:
            continue
        s = float(s)
        if e is None:
            e = s + float(it.get("dur", 1.0))
        out.append({"start": s, "end": float(e), "text": str(it.get("text", "")), "box": it.get("box"),
                    "kind": it.get("kind", default_kind), "position": it.get("position")})
    return out


def load_sidecar(path) -> dict:
    """Normalise the renderer's resolved-timeline JSON (tolerates key aliases). None if missing/invalid."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    segs_raw = raw if isinstance(raw, list) else _first(raw, "segments", "timeline", "pieces", "items", default=[])
    meta = raw if isinstance(raw, dict) else {}
    segs, caps, ovs = [], [], []
    t = 0.0
    for i, s in enumerate(segs_raw or []):
        if not isinstance(s, dict):
            continue
        start = _first(s, "start", "t0", "timeline_start", "tl_start")
        start = float(start) if start is not None else t
        end = _first(s, "end", "t1", "timeline_end", "tl_end")
        end = float(end) if end is not None else start + float(_first(s, "dur", "duration", default=0))
        t = end
        seg = {"i": i, "start": start, "end": end,
               "kind": str(_first(s, "type", "kind", default="")).lower(),
               "section": _first(s, "section", "chapter", default=""),
               "clip": _first(s, "clip_id", "clip", "source_id", "src_id", "id"),
               "path": _first(s, "path", "source", "src", "source_path"),
               "in": _first(s, "in", "src_in", "source_in"), "out": _first(s, "out", "src_out", "source_out"),
               "audio": _first(s, "audio", default="")}
        if seg["clip"] is None and seg["path"]:
            seg["clip"] = seg["path"]
        segs.append(seg)
        caps += _norm_items(s.get("captions"), start, "caption")
        ovs += _norm_items(s.get("overlays"), start, "overlay")
    caps += _norm_items(meta.get("captions"), 0.0, "caption")
    ovs += _norm_items(meta.get("overlays"), 0.0, "overlay")
    segs.sort(key=lambda x: x["start"])
    return {"path": str(p), "fps": _first(meta, "fps"), "width": _first(meta, "width"),
            "height": _first(meta, "height"), "version": _first(meta, "version"), "title": _first(meta, "title"),
            "caption_position": _first(meta.get("style", {}).get("captions", {}) if isinstance(meta.get("style"), dict) else {},
                                       "position", default=_first(meta, "caption_position")),
            "segments": segs, "captions": caps, "overlays": ovs}


# ==================================================================================================
# decoding
# ==================================================================================================

def _probe(cfg, video) -> dict:
    r = subprocess.run([_ffbin(cfg, "ffprobe"), "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", "-count_packets", str(video)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffprobe failed on {video}: {r.stderr[-400:]}")
    return json.loads(r.stdout or "{}")


def _rate(s):
    num, _, den = (s or "0/1").partition("/")
    try:
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except ValueError:
        return 0.0


class _VideoScan:
    """One decode pass at DECODE_W wide. Runs blackdetect/freezedetect in the same ffmpeg graph."""

    def __init__(self, cfg, video, w, h, fps, workdir):
        self.cfg, self.video, self.w, self.h, self.fps = cfg, str(video), w, h, fps
        self.err_path = Path(workdir) / "_video.log"
        self.error = None

    def run(self):
        import cv2
        cv2.setNumThreads(2)
        w, h, fps = self.w, self.h, self.fps
        freeze_d = 2.0 / fps + 1e-3            # >= 3 identical frames
        graph = (f"[0:v:0]scale={w}:{h}:flags=area,format=yuv420p,split=2[o][d];"
                 f"[d]blackdetect=d={1.0 / fps:.4f}:pix_th=0.10:pic_th=0.98,"
                 f"freezedetect=n=-60dB:d={freeze_d:.4f},nullsink")
        cmd = [_ffbin(self.cfg, "ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "info", "-i", self.video,
               "-filter_complex", graph, "-map", "[o]", "-an", "-fps_mode", "passthrough",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        with open(self.err_path, "w") as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, bufsize=w * h * 3 * 2)
            self._consume(proc, cv2)
            proc.stdout.close()
            proc.wait()
        self.log = self.err_path.read_text(errors="ignore")
        if proc.returncode and self.n == 0:
            self.error = f"video decode failed: {self.log[-400:]}"

    def _consume(self, proc, cv2):
        w, h = self.w, self.h
        fs = w * h * 3
        stride = max(1, int(round(self.fps / SHAKE_FPS)))
        g64, luma, mafd, sharp, vel, jpegs, p1 = [], [], [], [], [], [], []
        prev_g, prev_full, prev_shake = None, None, None
        i = 0
        while True:
            buf = proc.stdout.read(fs)
            if len(buf) < fs:
                break
            fr = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            grey = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(grey, (64, 36), interpolation=cv2.INTER_AREA)
            g64.append(small)
            luma.append(float(grey.mean()))
            p1.append(float(np.percentile(grey[::3, ::3], 1)))
            mafd.append(float(np.abs(small.astype(np.int16) - prev_g).mean()) if prev_g is not None else 0.0)
            sharp.append(float(cv2.Laplacian(grey, cv2.CV_32F).var()) if i % 3 == 0 else np.nan)
            v = (np.nan, np.nan)
            if i % stride == 0:
                if prev_shake is not None:
                    s = _camera_shift(cv2, prev_shake, grey)
                    if s is not None:
                        v = s
                prev_shake = grey
            vel.append(v)
            ok, jb = cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
            jpegs.append(jb.tobytes() if ok else b"")
            prev_g = small.astype(np.int16)
            i += 1
        self.n = i
        self.g64 = np.array(g64, np.uint8) if g64 else np.zeros((0, 36, 64), np.uint8)
        self.luma = np.array(luma, np.float32)
        self.p1 = np.array(p1, np.float32)
        self.mafd = np.array(mafd, np.float32)
        self.sharp = np.array(sharp, np.float32)
        self.vel = np.array(vel, np.float32).reshape(-1, 2)
        self.jpegs = jpegs
        self.stride = stride

    def frame(self, i):
        import cv2
        i = int(min(max(i, 0), self.n - 1))
        b = self.jpegs[i]
        return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR) if b else None

    def spans(self, key_start, key_end):
        """Parse blackdetect / freezedetect spans from the ffmpeg log."""
        out = []
        if key_start == "black":
            for m in re.finditer(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", self.log):
                out.append((float(m.group(1)), float(m.group(2))))
            # a black run that lasts to EOF prints no line in some builds: catch it from luma
            return out
        starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", self.log)]
        ends = [float(x) for x in re.findall(r"freeze_end:\s*([\d.]+)", self.log)]
        for k, s in enumerate(starts):
            e = ends[k] if k < len(ends) else self.n / self.fps
            out.append((s, e))
        return out


def _camera_shift(cv2, prev, cur):
    """Global translation (dx, dy) prev -> cur via LK + RANSAC similarity (same as analyze.py), or None."""
    pts = cv2.goodFeaturesToTrack(prev, maxCorners=120, qualityLevel=0.01, minDistance=12, blockSize=7)
    if pts is None or len(pts) < 12:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, winSize=(21, 21), maxLevel=3)
    ok = st.reshape(-1) == 1
    if ok.sum() < 10:
        return None
    m, inl = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if m is None or inl is None or inl.sum() < max(8, 0.3 * ok.sum()):
        return None
    return float(m[0, 2]), float(m[1, 2])


class _AudioScan:
    """One pass: ebur128 (with true peak) on the full-rate audio + a 16 kHz mono copy for numpy checks."""

    SR = 16000

    def __init__(self, cfg, video, workdir):
        self.cfg, self.video = cfg, str(video)
        self.err_path = Path(workdir) / "_audio.log"
        self.error = None
        self.audio = np.zeros(0, np.float32)

    def run(self):
        graph = ("[0:a:0]asplit=2[m][s];[m]ebur128=peak=true:framelog=info[e];[e]anullsink;"
                 f"[s]aresample={self.SR},aformat=sample_fmts=flt:channel_layouts=mono[o]")
        cmd = [_ffbin(self.cfg, "ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "info", "-i", self.video,
               "-filter_complex", graph, "-map", "[o]", "-f", "f32le", "-"]
        with open(self.err_path, "w") as errf:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=errf)
        self.log = self.err_path.read_text(errors="ignore")
        self.audio = np.frombuffer(r.stdout, np.float32).copy()
        if r.returncode and not len(self.audio):
            self.error = f"audio decode failed: {self.log[-400:]}"
        self._parse()

    def _parse(self):
        log = self.log
        self.integrated = self.lra = self.true_peak = None
        summ = log[log.rfind("Summary:"):] if "Summary:" in log else ""
        m = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summ)
        if m:
            self.integrated = float(m.group(1)) if m.group(1) != "-inf" else -120.0
        m = re.search(r"LRA:\s*(-?[\d.]+)\s*LU", summ)
        if m:
            self.lra = float(m.group(1))
        m = re.search(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)", summ, re.S)
        if m:
            self.true_peak = float(m.group(1)) if m.group(1) != "-inf" else -120.0
        tm = []
        for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET:[^M]*M:\s*(-?[\d.]+|-inf)", log):
            tm.append((float(m.group(1)), float(m.group(2)) if m.group(2) != "-inf" else -120.0))
        self.momentary = np.array(tm, np.float32).reshape(-1, 2)


# ==================================================================================================
# analysis
# ==================================================================================================

def detect_cuts(g64: np.ndarray, mafd: np.ndarray) -> list:
    """Frame indices that start a new shot (hard cuts) from 64x36 frame differences.
    A cut is a difference spike well above the local median, or a big jump in the luma histogram."""
    n = len(mafd)
    if n < 2:
        return []
    hists = np.stack([np.bincount((g.ravel() >> 4), minlength=16) for g in g64]).astype(np.float32)
    hists /= max(1.0, float(g64[0].size))
    hd = np.zeros(n, np.float32)
    hd[1:] = 0.5 * np.abs(hists[1:] - hists[:-1]).sum(1)
    cuts = []
    for i in range(1, n):
        lo, hi = max(1, i - 12), min(n, i + 13)
        nb = np.concatenate([mafd[lo:max(lo, i - 1)], mafd[min(hi, i + 2):hi]])
        med = float(np.median(nb)) if len(nb) else 0.0
        d = float(mafd[i])
        if (d >= 8.0 and d >= 3.5 * (med + 1.5)) or (hd[i] >= 0.45 and d >= 6.0 and d >= 2.0 * (med + 1.5)):
            cuts.append(i)
    return cuts


def _diff_across(g64, i) -> float:
    """Mean abs luma difference (0-255) between frame i-1 and frame i."""
    if i <= 0 or i >= len(g64):
        return 0.0
    return float(np.abs(g64[i].astype(np.int16) - g64[i - 1].astype(np.int16)).mean())


def _corr(a, b) -> float:
    a = a.astype(np.float64).ravel(); b = b.astype(np.float64).ravel()
    a = a - a.mean(); b = b - b.mean()
    d = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    return float(np.sum(a * b)) / d if d > 1e-6 else (1.0 if np.allclose(a, b) else 0.0)


def _planned_shots(side, fps, n):
    """Shots from the sidecar: consecutive segments that continue the same source are merged."""
    shots = []
    for s in side["segments"]:
        f0, f1 = int(round(s["start"] * fps)), int(round(s["end"] * fps))
        f0, f1 = max(0, min(f0, n)), max(0, min(f1, n))
        if f1 <= f0:
            continue
        if shots:
            p = shots[-1]
            cont = (p["clip"] is not None and p["clip"] == s["clip"] and p["kind"] == s["kind"]
                    and s["in"] is not None and p["out"] is not None
                    and abs(float(s["in"]) - float(p["out"])) <= 1.0 / fps + 1e-3)
            if cont and abs(f0 - p["f1"]) <= 1:
                p["f1"] = f1; p["out"] = s["out"]; p["segs"].append(s["i"])
                continue
        shots.append({"f0": f0, "f1": f1, "clip": s["clip"], "kind": s["kind"], "section": s["section"],
                      "in": s["in"], "out": s["out"], "path": s["path"], "segs": [s["i"]], "audio": s["audio"]})
    return shots


class _Report:
    def __init__(self, qa_dir, vs, fps):
        self.checks = []
        self.qa_dir = Path(qa_dir)
        self.ev_dir = self.qa_dir / "evidence"
        self.vs, self.fps = vs, fps
        self.n_ev = 0
        self._written = {}

    def evidence(self, frame, tag=""):
        """Write the cached frame as evidence; returns its path (or None past the cap)."""
        vs = self.vs
        if vs is None or vs.n == 0 or frame is None:
            return None
        frame = int(min(max(frame, 0), vs.n - 1))
        if frame in self._written:
            return self._written[frame]
        if self.n_ev >= MAX_EVIDENCE:
            return None
        self.ev_dir.mkdir(parents=True, exist_ok=True)
        p = self.ev_dir / f"f{frame:06d}_{fmt_t(frame / self.fps).replace(':', 'm')}s{('_' + tag) if tag else ''}.jpg"
        p.write_bytes(vs.jpegs[frame])
        self.n_ev += 1
        self._written[frame] = str(p)
        return str(p)

    def add(self, cid, category, title, issues=None, value=None, detail="", status=None, rule=None):
        issues = issues or []
        st = status or max((i["severity"] for i in issues), key=lambda s: SEV_ORDER[s], default="pass")
        c = {"id": cid, "category": category, "title": title, "status": st, "value": value, "detail": detail,
             "rule": rule, "issues": issues}
        self.checks.append(c)
        return c

    def issue(self, severity, msg, t=None, t_end=None, frames=(), tag=""):
        fps = self.fps
        ev = [p for p in (self.evidence(f, tag) for f in frames) if p]
        frame = frames[0] if frames else (int(round(t * fps)) if t is not None else None)
        return {"severity": severity, "t": None if t is None else round(float(t), 3),
                "t_end": None if t_end is None else round(float(t_end), 3),
                "tc": fmt_t(t) if t is not None else "", "frame": frame, "msg": msg, "evidence": ev}


def _merge_runs(idx, gap=1):
    runs = []
    for i in idx:
        if runs and i - runs[-1][1] <= gap:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return runs


def _within(t0, t1, spans, pad=0.0):
    return any(t0 >= a - pad and t1 <= b + pad for a, b in spans)


def _overlaps(t0, t1, spans):
    return any(t0 < b and t1 > a for a, b in spans)


# ==================================================================================================
# main entry
# ==================================================================================================

def run(cfg, video, plan_path=None, out_dir=None, sidecar_path=None) -> dict:
    """QA a rendered video. Writes <stem>.qa.json, <stem>.qa.html and <stem>.qa/ (contact sheets +
    evidence frames) into `out_dir` (default: next to the video). Returns the report dict."""
    t_start = time.time()
    video = Path(video).resolve()
    if not video.exists():
        raise FileNotFoundError(video)
    out_dir = Path(out_dir).resolve() if out_dir else video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    qa_dir = out_dir / f"{stem}.qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    for old in list(qa_dir.glob("sheet_*.jpg")) + list((qa_dir / "evidence").glob("*.jpg")):
        old.unlink()

    side = load_sidecar(sidecar_path or video.with_suffix(".json"))
    try:
        raw_side = json.loads(Path(sidecar_path or video.with_suffix(".json")).read_text()) if side else {}
        raw_side = raw_side if isinstance(raw_side, dict) else {}
    except (OSError, ValueError):
        raw_side = {}
    plan = None
    if plan_path:
        try:
            from . import plan as P
            plan = P.normalize(P.load(plan_path), cfg)
        except Exception as e:  # noqa: BLE001 - a broken plan must not stop QA
            plan = {"_error": str(e)}

    info = _probe(cfg, video)
    vst = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    ast = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if vst is None:
        raise RuntimeError(f"{video} has no video stream")
    W, H = int(vst.get("width") or 0), int(vst.get("height") or 0)
    rot = 0
    for sd in vst.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        W, H = H, W
    fps = _rate(vst.get("avg_frame_rate")) or _rate(vst.get("r_frame_rate")) or 25.0
    dw = DECODE_W if W >= H else int(round(DECODE_W * W / max(H, 1) / 2) * 2)
    dh = max(2, int(round(dw * H / max(W, 1) / 2)) * 2)

    vs = _VideoScan(cfg, video, dw, dh, fps, qa_dir)
    au = _AudioScan(cfg, video, qa_dir) if ast is not None else None
    th = [threading.Thread(target=vs.run, daemon=True)]
    if au:
        th.append(threading.Thread(target=au.run, daemon=True))
    for t in th:
        t.start()
    for t in th:
        t.join()
    if vs.error:
        raise RuntimeError(vs.error)
    n = vs.n
    rep = _Report(qa_dir, vs, fps)

    # ---------------------------------------------------------------- technical
    fmt = info.get("format", {})
    vdur = float(vst.get("duration") or 0) or (n / fps)
    adur = float(ast.get("duration") or 0) if ast else 0.0
    duration = float(fmt.get("duration") or vdur)
    exp = {}
    if side:
        exp.update({k: side[k] for k in ("fps", "width", "height") if side.get(k)})
    if plan and plan.get("output"):
        exp = {**plan["output"], **exp}
    iss = []
    if exp.get("width") and exp.get("height") and (int(exp["width"]), int(exp["height"])) != (W, H):
        iss.append(rep.issue("warn", f"resolution {W}x{H}, plan says {exp['width']}x{exp['height']} (fine for a preview)"))
    if exp.get("fps") and abs(float(exp["fps"]) - fps) > 0.01:
        iss.append(rep.issue("fail", f"frame rate {fps:.3f}, plan says {exp['fps']}"))
    if _rate(vst.get("r_frame_rate")) and abs(_rate(vst.get("r_frame_rate")) - fps) > max(0.05, 0.002 * fps):
        iss.append(rep.issue("warn", f"variable frame rate: avg {fps:.3f} vs r {_rate(vst.get('r_frame_rate')):.3f}"))
    if vst.get("pix_fmt") not in ("yuv420p", "yuvj420p", None):
        iss.append(rep.issue("warn", f"pix_fmt {vst.get('pix_fmt')} (yuv420p plays everywhere)"))
    if rot:
        iss.append(rep.issue("warn", f"display rotation {rot} degrees in the container"))
    rep.add("streams", "technical", "Streams, resolution, frame rate", iss,
            value={"width": W, "height": H, "fps": round(fps, 3), "codec": vst.get("codec_name"),
                   "pix_fmt": vst.get("pix_fmt"), "audio": ast.get("codec_name") if ast else None,
                   "sample_rate": int(ast.get("sample_rate") or 0) if ast else None,
                   "channels": ast.get("channels") if ast else None, "duration": round(duration, 3),
                   "frames_decoded": n})

    iss = []
    nb = int(vst.get("nb_read_packets") or vst.get("nb_frames") or 0)
    if ast is None:
        iss.append(rep.issue("fail", "no audio stream"))
    else:
        gap = abs(vdur - adur)
        if gap > 1.0 / fps + 1e-3:
            iss.append(rep.issue("fail", f"audio {adur:.3f}s vs video {vdur:.3f}s: off by {gap * fps:.1f} frames",
                                 t=min(vdur, adur)))
    if abs(n - vdur * fps) > 1.5:
        iss.append(rep.issue("warn", f"decoded {n} frames but duration implies {vdur * fps:.1f}"))
    if side and side["segments"]:
        tl = max(s["end"] for s in side["segments"])
        if abs(tl - vdur) > 1.0 / fps + 1e-3:
            iss.append(rep.issue("fail", f"timeline is {tl:.3f}s but the file is {vdur:.3f}s "
                                         f"({(vdur - tl) * fps:+.1f} frames)"))
    rep.add("av_length", "technical", "Audio/video length (rule 81, 112)", iss,
            value={"video_s": round(vdur, 3), "audio_s": round(adur, 3), "frames": n, "packets": nb})

    # ---------------------------------------------------------------- loudness
    talk_spans, montage_spans, still_spans, black_ok = [], [], [], []
    framed = [(float(x["start"]), float(x["end"]), x["frame"]) for x in (raw_side.get("segments") or []) if x.get("frame")]
    if side:
        for s in side["segments"]:
            k = s["kind"]
            if k in TALK_KINDS:
                talk_spans.append((s["start"], s["end"]))
            if k in MONTAGE_KINDS:
                montage_spans.append((s["start"], s["end"]))
            if k in INTENTIONAL_STILL:
                still_spans.append((s["start"], s["end"]))
            if k in INTENTIONAL_BLACK:
                black_ok.append((s["start"], s["end"]))
    if au and au.error is None:
        iss = []
        I, TP = au.integrated, au.true_peak
        if I is None:
            iss.append(rep.issue("warn", "ebur128 summary missing"))
        else:
            lo, hi = LUFS_RANGE
            mb = ((raw_side.get("loudness") or {}).get("measured_before") or {})
            dlg = None
            if mb.get("anchor") == "dialogue" and (mb.get("dialogue") or {}).get("I") is not None:
                # dialogue-anchored: the DIALOGUE sits on the target; music sections may lift the
                # programme up to 2 LU over it (render.PROGRAMME_OVER_LU)
                dlg = round(float(mb["dialogue"]["I"]) + float(mb.get("gain_db") or 0.0), 1)
                hi += 2.0
            off = lo - I if I < lo else (I - hi if I > hi else 0.0)
            if off > 0:
                iss.append(rep.issue("fail" if off > LUFS_SLACK else "warn",
                                     f"integrated loudness {I:.1f} LUFS, target {lo:g}..{hi:g}"
                                     + (f" (dialogue-anchored; dialogue {dlg:.1f})" if dlg is not None else "")))
        if TP is not None and TP > TP_MAX:
            iss.append(rep.issue("fail", f"true peak {TP:.1f} dBTP > {TP_MAX:g} (lower the gain or limit)"))
        mom = au.momentary
        value = {"integrated_lufs": I, "true_peak_dbtp": TP, "lra": au.lra}
        if I is not None and dlg is not None:
            value["dialogue_lufs"] = dlg

        def span_lufs(spans):
            if not spans or not len(mom):
                return None
            sel = np.zeros(len(mom), bool)
            for a, b in spans:
                sel |= (mom[:, 0] >= a + 0.4) & (mom[:, 0] <= b)
            v = mom[sel, 1]
            v = v[v > -70]
            return round(float(10 * np.log10(np.mean(10 ** (v / 10)))), 1) if len(v) else None

        value["talk_lufs"] = span_lufs(talk_spans)
        value["montage_lufs"] = span_lufs(montage_spans)
        rep.add("loudness", "technical", "Loudness and true peak (rule 82)", iss, value=value)
    elif ast is not None:
        rep.add("loudness", "technical", "Loudness and true peak (rule 82)",
                [rep.issue("fail", au.error if au else "audio not decoded")])

    # ---------------------------------------------------------------- audio: silence holes, under-speech
    if au and len(au.audio):
        sr = au.SR
        win = int(sr * HOLE_WIN)
        k = len(au.audio) // win
        rms = 20 * np.log10(np.sqrt(np.mean(au.audio[:k * win].astype(np.float64).reshape(k, win) ** 2, 1)) + 1e-12)
        holes = np.where(rms < HOLE_DB)[0]
        iss = []
        for a, b in _merge_runs(holes.tolist()):
            t0, t1 = a * HOLE_WIN, (b + 1) * HOLE_WIN
            floor = float(rms[a:b + 1].min())
            intentional = _within(t0, t1, black_ok, pad=0.3)
            at_edge = t0 < 0.3 or t1 > duration - 0.3
            in_talk = _overlaps(t0, t1, talk_spans)
            sev = "warn" if (intentional or at_edge) else "fail"
            where = " inside a talking segment" if in_talk else (" (intentional black/title)" if intentional else "")
            iss.append(rep.issue(sev, f"digital-silence hole {t1 - t0:.2f}s at {floor:.0f} dBFS{where}: fill with room tone",
                                 t=t0, t_end=t1, frames=[int(t0 * fps)], tag="silence"))
        rep.add("silence_holes", "audio", f"Silence holes (< {HOLE_DB:g} dBFS over {HOLE_WIN}s, rule 80)", iss,
                value={"holes": len(iss), "windows_below": int(len(holes))})

        # what sits under speech: the quiet floor between words in talk spans
        iss, floors = [], []
        if talk_spans:
            hop = int(sr * 0.05)
            for a, b in talk_spans:
                seg = au.audio[int(a * sr):int(b * sr)]
                m = len(seg) // hop
                if m < 10:
                    continue
                fr = seg[:m * hop].reshape(m, hop).astype(np.float64)
                r = 20 * np.log10(np.sqrt(np.mean(fr ** 2, 1)) + 1e-12)
                q = r <= np.percentile(r, 15)
                floor = float(np.percentile(r, 10))
                spec = np.abs(np.fft.rfft(fr[q] * np.hanning(hop), axis=1)) + 1e-9
                flat = float(np.mean(np.exp(np.mean(np.log(spec), 1)) / np.mean(spec, 1)))
                floors.append({"t0": round(a, 2), "t1": round(b, 2), "floor_db": round(floor, 1),
                               "flatness": round(flat, 3)})
                if floor > UNDER_SPEECH_DB:
                    kind = "music likely" if flat < 0.25 else "noise or music"
                    iss.append(rep.issue("warn", f"floor between words {floor:.0f} dBFS ({kind}, flatness {flat:.2f}) "
                                                 f"under talk {fmt_t(a)}-{fmt_t(b)} (rule 70: <= {UNDER_SPEECH_DB:g})",
                                         t=a, t_end=b))
            rep.add("under_speech", "audio", "Music/noise under speech (rule 70, 71)", iss,
                    value={"spans": floors[:200]})
        else:
            rep.add("under_speech", "audio", "Music/noise under speech (rule 70, 71)", [], status="info",
                    detail="no talk spans (needs the render sidecar)")

    # ---------------------------------------------------------------- picture: black / frozen
    black = vs.spans("black", None)
    blk_lum = _merge_runs(np.where(vs.luma < 12)[0].tolist())
    for a, b in blk_lum:     # catch black runs ffmpeg did not report (e.g. at EOF)
        t0, t1 = a / fps, (b + 1) / fps
        if not _overlaps(t0, t1, black):
            black.append((t0, t1))
    iss = []
    for t0, t1 in sorted(black):
        intentional = _within(t0, t1, black_ok, pad=1.0 / fps)
        edge = t0 < 1.0 / fps or t1 > duration - 2.0 / fps
        sev = "info" if intentional else ("warn" if edge else "fail")
        if sev == "info":
            continue
        iss.append(rep.issue(sev, f"black or near-black picture {t1 - t0:.2f}s ({(t1 - t0) * fps:.0f} frames)" +
                             (" at the head/tail" if edge else ""), t=t0, t_end=t1,
                             frames=[int(round(t0 * fps))], tag="black"))
    rep.add("black", "picture", "Black frames (blackdetect)", iss, value={"spans": [[round(a, 3), round(b, 3)] for a, b in black]})

    frozen = vs.spans("freeze", None)
    iss = []
    keep = [(t0, t1) for t0, t1 in frozen
            if not _within(t0, t1, [(a - 2 / fps, b + 2 / fps) for a, b in black])
            and not _within(t0, t1, still_spans, pad=1.0 / fps)]
    # group freezes separated by < 0.3 s: a run of short freezes is stutter (duplicated frames from a
    # frame-rate conversion, a slowed clip or a near-still), one long freeze is a held/ran-out frame
    groups = []
    for t0, t1 in sorted(keep):
        if groups and t0 - groups[-1][-1][1] < 0.3:
            groups[-1].append((t0, t1))
        else:
            groups.append([(t0, t1)])
    for g in groups:
        t0, t1 = g[0][0], g[-1][1]
        longest = max(b - a for a, b in g) * fps
        frozen_fr = sum(b - a for a, b in g) * fps
        f0 = int(round(t0 * fps))
        ev = [f0, min(n - 1, int(round(t1 * fps)) - 1)]
        if len(g) == 1:
            sev = "fail" if longest >= 8 else "warn"
            msg = (f"frozen picture {t1 - t0:.2f}s ({longest:.0f} identical frames): held frame or the source ran out "
                   f"(rule 33)")
        else:
            sev = "fail" if (t1 - t0) >= 0.5 or longest >= 8 else "warn"
            msg = (f"stutter {t1 - t0:.2f}s: {len(g)} freezes, {frozen_fr:.0f} repeated frames (low-fps or slowed "
                   f"source, or a near-still; rules 33, 34)")
        iss.append(rep.issue(sev, msg, t=t0, t_end=t1, frames=ev, tag="frozen"))
    rep.add("frozen", "picture", "Frozen frames (freezedetect, >= 3 identical)", iss,
            value={"spans": [[round(a, 3), round(b, 3)] for a, b in frozen]})

    # ---------------------------------------------------------------- cuts
    tr_frames = [int(t.get("frame", round(float(t["t"]) * fps))) for t in (raw_side.get("transitions") or [])]
    teaser_t = [(x.get("start"), x.get("end")) for x in (raw_side.get("segments") or []) if x.get("teaser")]

    def near_tr(f, r=4):
        return any(abs(f - x) <= r for x in tr_frames)

    def in_teaser(f):
        return bool(teaser_t) and _within(f / fps, f / fps, teaser_t, pad=1 / fps)
    det = detect_cuts(vs.g64, vs.mafd)
    det_set = set(det)
    planned = []
    shots_plan = _planned_shots(side, fps, n) if side and side["segments"] else None
    if shots_plan:
        planned = [s["f0"] for s in shots_plan[1:]]
    iss_cut, iss_inv = [], []
    if planned:
        for k, f in enumerate(planned):
            prev_kind, kind = shots_plan[k]["kind"], shots_plan[k + 1]["kind"]
            near = [d for d in det if abs(d - f) <= 3]
            diff = _diff_across(vs.g64, f)
            if f in det_set:
                continue
            if near:
                off = min(near, key=lambda d: abs(d - f)) - f
                sev = "warn" if abs(off) == 1 else "fail"
                if in_teaser(f) or near_tr(f):
                    continue          # micro-cuts 3 frames apart / a flash ramp: the detector can't separate them
                iss_cut.append(rep.issue(sev, f"cut lands {off:+d} frame(s) from the planned frame {f} (rule 112)",
                                         t=f / fps, frames=[f - 1, f], tag="cut"))
            elif diff < INVISIBLE_CUT:
                sev = "fail" if (kind in MONTAGE_KINDS or prev_kind in MONTAGE_KINDS) else (
                    "info" if kind in TALK_KINDS and prev_kind in TALK_KINDS else "warn")
                if sev != "info":
                    iss_inv.append(rep.issue(sev, f"invisible cut: luma diff {diff:.1f} < {INVISIBLE_CUT:g} "
                                                  f"({prev_kind or '?'} -> {kind or '?'}, rule 31)",
                                             t=f / fps, frames=[f - 1, f], tag="invisible"))
            else:
                iss_cut.append(rep.issue("warn", f"planned cut at frame {f} not detected (diff {diff:.1f}); "
                                                 f"dissolve or very similar shots?", t=f / fps, frames=[f - 1, f], tag="cut"))
        pset = set(planned)
        for d in det:
            if near_tr(d) or in_teaser(d):
                continue              # a flash's light ramp, or a cutaway switch inside a teaser line
            if not any(abs(d - p) <= 3 for p in pset):
                iss_cut.append(rep.issue("warn", "unplanned cut inside a segment (cut in the source, or a timing slip)",
                                         t=d / fps, frames=[d - 1, d], tag="cut"))
        rep.add("cut_timing", "picture", "Cuts on the delivered file vs the plan (rule 112)", iss_cut,
                value={"planned": len(planned), "detected": len(det),
                       "exact": sum(1 for f in planned if f in det_set)})
    else:
        rep.add("cut_timing", "picture", "Cuts on the delivered file vs the plan (rule 112)", [], status="info",
                value={"detected": len(det)}, detail="no sidecar: cuts detected from the picture only")

    # shots = planned shots if we have them, otherwise detected shots
    if shots_plan:
        shots = shots_plan
    else:
        b = [0] + det + [n]
        shots = [{"f0": b[i], "f1": b[i + 1], "clip": None, "kind": "", "section": "", "in": None, "out": None,
                  "path": None, "segs": [], "audio": ""} for i in range(len(b) - 1) if b[i + 1] > b[i]]

    # invisible cuts on detected-only cuts, not needed (a detected cut is visible by definition);
    # without a plan look for near-identical neighbouring shots instead (jump between same set-up)
    if not shots_plan:
        for k in range(1, len(shots)):
            f = shots[k]["f0"]
            diff = _diff_across(vs.g64, f)
            if diff < INVISIBLE_CUT:
                iss_inv.append(rep.issue("warn", f"weak cut: luma diff {diff:.1f} < {INVISIBLE_CUT:g} (rule 31)",
                                         t=f / fps, frames=[f - 1, f], tag="invisible"))
    rep.add("invisible_cuts", "picture", f"Invisible cuts (luma diff < {INVISIBLE_CUT:g}, rule 31)", iss_inv)

    # flash frames: 1-frame shots
    iss = []
    for k, s in enumerate(shots):
        if s["f1"] - s["f0"] == 1 and 0 < s["f0"] and s["f1"] < n:
            if s["kind"] in ("flash",) or near_tr(s["f0"]):
                continue
            iss.append(rep.issue("fail", "1-frame shot (flash frame)", t=s["f0"] / fps,
                                 frames=[s["f0"] - 1, s["f0"], s["f1"]], tag="flash"))
    # also catch detected 1-frame shots when the plan thinks the span is continuous
    if shots_plan:
        for a, b2 in zip(det, det[1:]):
            if near_tr(a):
                continue              # a planned flash: its light frames are not a 1-frame shot
            if b2 - a == 1 and not any(abs(a - x["f0"]) <= 0 and x["f1"] - x["f0"] == 1 for x in shots):
                iss.append(rep.issue("fail", "1-frame flash on the delivered file", t=a / fps,
                                     frames=[a - 1, a, b2], tag="flash"))
    rep.add("flash_frames", "picture", "Flash frames (1-frame shots)", iss)

    # ---------------------------------------------------------------- per-shot measurements
    vel = vs.vel
    for k, s in enumerate(shots):
        f0, f1 = s["f0"], s["f1"]
        s["idx"] = k
        s["start"], s["end"] = round(f0 / fps, 3), round(f1 / fps, 3)
        s["dur"] = round((f1 - f0) / fps, 3)
        mids = sorted({f0, (f0 + f1 - 1) // 2, f1 - 1})
        s["sample_frames"] = mids
        faces = [detect_faces(vs.frame(f)) for f in mids]
        s["faces_per_frame"] = [len(x) for x in faces]
        s["faces"] = max(s["faces_per_frame"]) if faces else 0
        s["face_boxes"] = {str(f): b for f, b in zip(mids, faces)}
        s["luma"] = round(float(vs.luma[f0:f1].mean()), 1)
        sh = vs.sharp[f0:f1]
        sh = sh[~np.isnan(sh)]
        s["sharpness"] = round(float(np.median(sh)), 1) if len(sh) else None
        v = vel[f0 + 1:f1]
        v = v[~np.isnan(v[:, 0])] if len(v) else v
        if len(v) >= 3:
            dv = np.diff(v, axis=0)
            s["shake"] = round(float(np.mean(np.hypot(dv[:, 0], dv[:, 1]))), 2)
            s["motion"] = round(float(np.mean(np.hypot(v[:, 0], v[:, 1]))), 2)
        else:
            s["shake"] = s["motion"] = None
        s["activity"] = round(float(vs.mafd[f0 + 1:f1].mean()), 2) if f1 - f0 > 1 else 0.0

    def shot_issue(sev, s, msg, tag):
        return rep.issue(sev, f"shot {s['idx'] + 1} ({s['dur']:.2f}s): {msg}", t=s["start"], t_end=s["end"],
                         frames=[s["sample_frames"][len(s["sample_frames"]) // 2]], tag=tag)

    real = [s for s in shots if not _within(s["start"], s["end"], black_ok + still_spans, pad=1 / fps)
            and not _within(s["start"], s["end"], [(a - 1 / fps, b + 1 / fps) for a, b in black])]
    iss = []
    for s in real:
        if s["shake"] is not None and s["dur"] >= 0.5 and s["shake"] > SHAKE_WARN:
            iss.append(shot_issue("fail" if s["shake"] > SHAKE_FAIL else "warn", s,
                                  f"shaky (shake {s['shake']:.1f}; < 3 steady, > {SHAKE_WARN:g} shaky; rule 84)", "shake"))
    rep.add("shake", "shots", "Camera shake per shot (rule 84)", iss,
            value={"median": _nanmed([s["shake"] for s in real])})
    iss = []
    for s in real:
        fr = next((w for a, b, w in framed if a - 1e-3 <= s["start"] < b), None)
        luma = s["luma"]
        if fr:                                   # an inset / gate: judge the picture inside its window
            img = vs.frame(s["sample_frames"][len(s["sample_frames"]) // 2])
            if img is not None:
                hh, ww = img.shape[:2]
                x0, y0, x1, y1 = fr["window"]
                win = img[int(y0 * hh):int(y1 * hh), int(x0 * ww):int(x1 * ww)]
                if win.size:
                    luma = float(win.mean())
        if luma < DARK_LUMA:
            iss.append(shot_issue("warn", s, f"dark (mean luma {luma:.0f} < {DARK_LUMA:g}; rule 90)", "dark"))
        if s["sharpness"] is not None and s["sharpness"] < SHARP_FLOOR and s["dur"] >= 0.3:
            iss.append(shot_issue("warn", s, f"soft (Laplacian variance {s['sharpness']:.0f} < {SHARP_FLOOR:g}; rule 90)", "soft"))
    rep.add("exposure_sharpness", "shots", "Dark or soft shots (rule 90)", iss)

    iss = []
    for s in real:
        if s["kind"] in TALK_KINDS or s["dur"] < 0.4:
            continue
        if s["faces"] == 0 and (s["motion"] or 0) < EMPTY_MOTION and s["activity"] < EMPTY_ACTIVITY:
            s["people"] = max(len(detect_people(vs.frame(f))) for f in s["sample_frames"])
            if s["people"] == 0:
                iss.append(shot_issue("warn", s, "no face, no person found and almost no motion: possibly an empty "
                                                 "plate, check it (rule 24)", "empty"))
    rep.add("empty_shots", "shots", "Possibly empty plates (rule 24)", iss)

    # picture must fill the frame: no plates, picture-in-picture or letterbox bars (rule 37)
    iss, bordered = [], {}
    for s in real:
        if any(a - 1e-3 <= s["start"] < b for a, b, _ in framed):
            continue                             # a planned gate / inset / letterbox (the reference's frames, rule 38)
        g = vs.g64[s["sample_frames"][len(s["sample_frames"]) // 2]].astype(np.float32)
        ring = np.concatenate([g[:3].ravel(), g[-3:].ravel(), g[:, :4].ravel(), g[:, -4:].ravel()])
        inner = g[8:-8, 10:-10]
        bars_tb = g[:3].mean() < 14 and g[-3:].mean() < 14 and inner.mean() > 40
        bars_lr = g[:, :4].mean() < 14 and g[:, -4:].mean() < 14 and inner.mean() > 40
        if (ring.mean() < 14 and inner.mean() > 40) or bars_tb or bars_lr:
            bordered[s["idx"]] = ("border all round (plate / picture-in-picture)" if ring.mean() < 14 else
                                  "letterbox bars" if bars_tb else "pillarbox bars")
    runs = _merge_runs(sorted(bordered))          # consecutive bordered shots = one finding
    for a, b in runs:
        sa, sb = shots[a], shots[b]
        what = bordered[a]
        n_sh = b - a + 1
        iss.append(rep.issue("warn", f"picture does not fill the frame: {what}" +
                             (f" on {n_sh} consecutive shots" if n_sh > 1 else "") + " (rules 37, 96)",
                             t=sa["start"], t_end=sb["end"], frames=[sa["sample_frames"][len(sa["sample_frames"]) // 2]],
                             tag="border"))
    rep.add("full_frame", "picture", "Picture fills the frame (rules 37, 96)", iss)

    # ---------------------------------------------------------------- repeats
    iss = []
    if shots_plan:
        nt = [s for s in shots if s["clip"] is not None and s["kind"] not in TALK_KINDS
              and s["kind"] not in ("title", "black")]
        for i, a in enumerate(nt):
            for b in nt[i + 1:]:
                if b["clip"] != a["clip"]:
                    continue
                gap = b["start"] - a["end"]
                same_win = (a["in"] is not None and b["in"] is not None and a["out"] is not None and b["out"] is not None
                            and float(a["in"]) < float(b["out"]) and float(b["in"]) < float(a["out"]))
                if same_win:
                    iss.append(rep.issue("fail", f"same source window reused: shots {a['idx'] + 1} and {b['idx'] + 1} "
                                                 f"(clip {a['clip']} {a['in']}-{a['out']}; rule 23)",
                                         t=b["start"], frames=[a["sample_frames"][1 if len(a['sample_frames']) > 1 else 0],
                                                               b["sample_frames"][1 if len(b['sample_frames']) > 1 else 0]],
                                         tag="repeat"))
                elif gap < REPEAT_WINDOW:
                    iss.append(rep.issue("warn", f"same clip {gap:.1f}s apart: shots {a['idx'] + 1} and {b['idx'] + 1} (rule 92)",
                                         t=b["start"], frames=[a["sample_frames"][0], b["sample_frames"][0]], tag="repeat"))
                elif a["section"] and a["section"] == b["section"]:
                    iss.append(rep.issue("warn", f"same clip twice in section '{a['section']}': shots {a['idx'] + 1} "
                                                 f"and {b['idx'] + 1} (rule 92)", t=b["start"],
                                         frames=[a["sample_frames"][0], b["sample_frames"][0]], tag="repeat"))
    # near-duplicate set-ups by picture (works with or without a sidecar)
    for i, a in enumerate(shots):
        if a["idx"] in bordered:
            continue                  # a dark plate around the picture dominates the correlation
        ma = vs.g64[a["sample_frames"][len(a["sample_frames"]) // 2]][4:-4, 6:-6]
        for b in shots[i + 2:]:
            if b["start"] - a["end"] > REPEAT_WINDOW:
                break
            if a["kind"] in TALK_KINDS and b["kind"] in TALK_KINDS:
                continue
            if not shots_plan and a["faces"] and b["faces"] and a["dur"] >= 1.5 and b["dur"] >= 1.5:
                continue          # talking head returning after a cutaway
            if shots_plan and a["clip"] is not None and a["clip"] == b["clip"]:
                continue          # already reported by clip id
            if _within(a["start"], a["end"], black_ok + still_spans + black) or \
               _within(b["start"], b["end"], black_ok + still_spans + black):
                continue
            if b["idx"] in bordered:
                continue
            mb = vs.g64[b["sample_frames"][len(b["sample_frames"]) // 2]][4:-4, 6:-6]
            c = _corr(ma, mb)
            if c >= NEAR_DUP_CORR and float(ma.std()) > 8:
                iss.append(rep.issue("warn", f"near-duplicate set-up: shots {a['idx'] + 1} and {b['idx'] + 1} "
                                             f"correlate {c:.2f} within {b['start'] - a['end']:.1f}s (rule 92)",
                                     t=b["start"], frames=[a["sample_frames"][len(a['sample_frames']) // 2],
                                                           b["sample_frames"][len(b['sample_frames']) // 2]], tag="dup"))
    # short montage shots
    for s in shots:
        if teaser_t and _within(s["start"], s["end"], teaser_t, pad=1 / fps):
            continue                  # cold-open micro-cuts are short on purpose (reference teaser: median 0.16 s)
        is_mont = s["kind"] in MONTAGE_KINDS or (not shots_plan)
        if is_mont and 1.0 / fps < (s["f1"] - s["f0"]) / fps < MIN_MONTAGE_SHOT and 0 < s["f0"] and s["f1"] < n:
            iss.append(shot_issue("warn", s, f"shorter than {MIN_MONTAGE_SHOT}s", "short"))
    rep.add("repeats", "shots", "Repeats, near-duplicates, very short shots (rules 23, 30, 92)", iss)

    # ---------------------------------------------------------------- captions / overlays vs faces
    if side and (side["captions"] or side["overlays"]):
        iss = []
        cap_pos = side.get("caption_position") or ((plan or {}).get("style", {}).get("captions", {}).get("position")
                                                   if isinstance(plan, dict) else None) or "bottom"
        checked = 0
        items = [("caption", c) for c in side["captions"]] + [("overlay", o) for o in side["overlays"]]
        for kind, it in items:
            if it.get("box"):
                box = [float(x) for x in it["box"]]
            elif kind == "caption":
                box = CAPTION_BANDS.get(it.get("position") or cap_pos, CAPTION_BANDS["bottom"])
            else:
                key = it.get("position") or ("lower_third" if it.get("kind") == "lower_third" else "center")
                box = OVERLAY_BOXES.get(key, OVERLAY_BOXES["center"])
            f0, f1 = int(round(it["start"] * fps)), max(int(round(it["start"] * fps)) + 1, int(round(it["end"] * fps)))
            hits = []
            for f in sorted({f0 + 1, (f0 + f1) // 2, f1 - 2}):
                if not 0 <= f < n:
                    continue
                checked += 1
                for fb in detect_faces(vs.frame(f)):
                    if kind == "caption" and fb[3] < SMALL_FACE_H and overlap(fb, box) > 0.5:
                        continue                 # a "face" this small inside the caption band is the caption text
                    ov = overlap(head_box(fb), box)
                    if ov > 0.02:
                        hits.append((f, ov))
            if hits:
                f, ov = max(hits, key=lambda x: x[1])
                label = it.get("text") or it.get("kind") or kind
                iss.append(rep.issue("fail", f"{kind} '{label[:40]}' overlaps a face/head ({ov * 100:.0f}% of the head box; "
                                             f"rules 55, 64)", t=it["start"], t_end=it["end"], frames=[f], tag="capface"))
        rep.add("text_vs_faces", "captions", "Captions and graphics clear of faces (rules 55, 64)", iss,
                value={"items": len(items), "frames_checked": checked})
    else:
        rep.add("text_vs_faces", "captions", "Captions and graphics clear of faces (rules 55, 64)", [], status="info",
                detail="no caption/overlay timing (needs the render sidecar)")

    # ---------------------------------------------------------------- craft checks (reference-measured)
    _craft_checks(rep, vs, au, shots, real, fps, n, H, duration, raw_side, side, talk_spans, black_ok, still_spans,
                  frozen, cfg)
    _reference_checks(rep, vs, fps, n, raw_side, framed)

    # ---------------------------------------------------------------- contact sheets
    issue_frames = {}
    for c in rep.checks:
        for i in c["issues"]:
            if i.get("frame") is not None and i["severity"] in ("fail", "warn"):
                issue_frames.setdefault(int(i["frame"]), i["severity"])
    sheets = _contact_sheets(vs, shots, fps, qa_dir, stem, issue_frames)

    # ---------------------------------------------------------------- summary + write
    fails = [(c, i) for c in rep.checks for i in c["issues"] if i["severity"] == "fail"]
    warns = [(c, i) for c in rep.checks for i in c["issues"] if i["severity"] == "warn"]
    top = [f"[{i['tc'] or '--:--.-'}] {i['severity'].upper()} {c['id']}: {i['msg']}"
           for c, i in sorted(fails, key=lambda x: (x[1]["t"] if x[1]["t"] is not None else -1))[:12]]
    top += [f"[{i['tc'] or '--:--.-'}] WARN {c['id']}: {i['msg']}"
            for c, i in sorted(warns, key=lambda x: (x[1]["t"] if x[1]["t"] is not None else -1))[:max(0, 15 - len(top))]]
    json_path = out_dir / f"{stem}.qa.json"
    html_path = out_dir / f"{stem}.qa.html"
    runtime = round(time.time() - t_start, 1)
    summary = {"status": "fail" if fails else ("warn" if warns else "pass"), "fail": len(fails), "warn": len(warns),
               "checks": {c["id"]: c["status"] for c in rep.checks},
               "shots": len(shots), "cuts_detected": len(det), "duration": round(duration, 3),
               "loudness_lufs": au.integrated if au else None, "true_peak_dbtp": au.true_peak if au else None,
               "top_issues": top, "sheets": [str(p) for p in sheets], "report": str(json_path), "html": str(html_path),
               "runtime_s": runtime, "sidecar": side["path"] if side else None}
    report = {"video": str(video), "version": side.get("version") if side else _version_of(stem),
              "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "duration": round(duration, 3), "fps": round(fps, 3),
              "width": W, "height": H, "sidecar": side["path"] if side else None,
              "plan": str(plan_path) if plan_path else None, "runtime_s": runtime, "summary": summary,
              "checks": rep.checks,
              "shots": [{k: v for k, v in s.items() if k not in ("face_boxes",)} for s in shots],
              "cuts_detected": [round(f / fps, 3) for f in det]}
    tmp = json_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1, default=_json_default))
    os.replace(tmp, json_path)
    html_path.write_text(_html_page(report, out_dir))
    for p in (vs.err_path, getattr(au, "err_path", None)):
        try:
            if p:
                Path(p).unlink()
        except OSError:
            pass
    return report


# ==================================================================================================
# craft checks: room tone, black level, duplicate frames, flash + hit alignment, caption size
# ==================================================================================================

def _floor_db(rms10, t0, t1):
    a, b = max(0, int(round(t0 * 100))), min(len(rms10), int(round(t1 * 100)))
    if b - a < 5:
        return None
    return float(np.percentile(rms10[a:b], 10))


def floor_steps(audio, sr, joins, win=0.6):
    """Noise-floor step (dB) across each join: 10th percentile of 10 ms RMS in `win` s before vs after."""
    h = int(sr * 0.01)
    k = len(audio) // h
    if k == 0:
        return [], np.zeros(0)
    rms10 = 20 * np.log10(np.sqrt(np.mean(audio[:k * h].astype(np.float64).reshape(k, h) ** 2, 1)) + 1e-12)
    out = []
    for t in joins:
        f0, f1 = _floor_db(rms10, t - win, t - 0.02), _floor_db(rms10, t + 0.02, t + win)
        if f0 is None or f1 is None:
            continue
        out.append({"t": round(float(t), 3), "before": round(f0, 1), "after": round(f1, 1), "step": round(f1 - f0, 1)})
    return out, rms10


def _env_1ms(x, sr):
    """5 ms RMS envelope at a 1 ms hop."""
    hop, w = max(1, sr // 1000), max(1, int(sr * 0.005))
    sq = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    idx = np.arange(0, max(0, len(x) - w), hop)
    return np.sqrt((sq[idx + w] - sq[idx]) / w)


def sfx_offset(audio, sr, t_expected_start, sfx_wav, search=0.06):
    """Where a sound effect actually starts in the mix relative to where it should: cross-correlate the
    5 ms envelopes of the effect and of the mix (+-search s). Returns the offset in seconds, or None."""
    import soundfile as sf
    try:
        x, xsr = sf.read(str(sfx_wav), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001
        return None
    x = x.mean(1)
    if xsr != sr:
        idx = np.arange(0, len(x), xsr / sr).astype(int)
        x = x[idx[idx < len(x)]]
    full = _env_1ms(x, sr)
    if not len(full):
        return None
    # match 150 ms around the effect's own transient (a whoosh-into-impact is quiet for its first 0.35 s)
    k0 = max(0, int(np.argmax(full)) - 40)
    ex = full[k0:k0 + 150]
    t_expected_start = t_expected_start + k0 / 1000.0
    a0 = int((t_expected_start - search) * sr)
    if a0 < 0 or a0 + int((2 * search + 0.2) * sr) > len(audio) or not len(ex):
        return None
    em = _env_1ms(audio[a0:a0 + int((2 * search + 0.2) * sr)], sr)
    lags = np.arange(0, int(2 * search * 1000) + 1)
    sc = [float(np.dot(em[l:l + len(ex)], ex)) if l + len(ex) <= len(em) else -1 for l in lags]
    best = int(lags[int(np.argmax(sc))])
    return round((best / 1000.0) - search, 4)


def _craft_checks(rep, vs, au, shots, real, fps, n, H, duration, raw, side, talk_spans, black_ok, still_spans,
                  frozen, cfg):
    # ---- room tone continuity across talk -> talk joins
    joins = raw.get("talk_joins")
    if joins is None and side:
        segs = side["segments"]
        joins = [b["start"] for a, b in zip(segs, segs[1:]) if a["kind"] in TALK_KINDS and b["kind"] in TALK_KINDS
                 and abs(a["end"] - b["start"]) < 1e-3]
    if au and len(au.audio) and joins:
        st, rms10 = floor_steps(au.audio, au.SR, joins)
        iss = []
        absd = [abs(x["step"]) for x in st]
        med = float(np.median(absd)) if absd else None
        p90 = float(np.percentile(absd, 90)) if absd else None
        if med is not None and (med > FLOOR_STEP_MEDIAN or p90 > FLOOR_STEP_P90):
            worst = max(st, key=lambda x: abs(x["step"]))
            iss.append(rep.issue("warn", f"room tone steps at talk joins: median {med:.1f} dB, p90 {p90:.1f} dB "
                                         f"(target <= {FLOOR_STEP_MEDIAN:g} / {FLOOR_STEP_P90:g}; reference 2.2 / 4.9). "
                                         f"Worst {worst['step']:+.1f} dB", t=worst["t"],
                                 frames=[int(worst["t"] * fps)], tag="tone"))
        mins = []
        for a, b in talk_spans:
            i0, i1 = int(a * 100) + 3, int(b * 100) - 3
            if i1 - i0 >= 25:
                w = rms10[i0:i1]
                k = len(w) // 25
                w25 = 10 * np.log10(np.mean(10 ** (w[:k * 25].reshape(k, 25) / 10), 1) + 1e-12)
                mins.append(float(w25.min()))
        mn = min(mins) if mins else None
        if mn is not None and mn < TALK_MIN_WINDOW_DB:
            iss.append(rep.issue("warn", f"quietest 250 ms inside talk is {mn:.0f} dBFS (< {TALK_MIN_WINDOW_DB:g}): a hole "
                                         "under the dialogue; room tone should fill it"))
        rep.add("room_tone_steps", "audio", "Room tone continuous across talk joins", iss,
                value={"joins": len(st), "median_abs_db": None if med is None else round(med, 1),
                       "p90_abs_db": None if p90 is None else round(p90, 1),
                       "over_6db": sum(1 for x in absd if x > 6), "min_250ms_talk_db": None if mn is None else round(mn, 1),
                       "steps": st[:200], "room_tone_beds": len(raw.get("room_tone") or [])})
    else:
        rep.add("room_tone_steps", "audio", "Room tone continuous across talk joins", [], status="info",
                detail="no talk-to-talk joins")

    # ---- black level (milky blacks)
    def is_black_ok(s):
        return _within(s["start"], s["end"], black_ok + still_spans, pad=1 / fps)
    framed_t = [(float(x["start"]), float(x["end"])) for x in (raw.get("segments") or []) if x.get("frame")]
    iss, rows = [], []
    for s in real:
        if is_black_ok(s) or s["f1"] - s["f0"] < 3 or _within(s["start"], s["end"], framed_t, pad=1 / fps):
            continue                             # a gate / inset border is not the picture's black point
        a, b = s["f0"] + 1, s["f1"] - 1                  # skip boundary frames (flashes, dips)
        p1 = float(np.median(vs.p1[a:b])) if b > a else float(vs.p1[s["f0"]])
        s["black_p1"] = round(p1, 1)
        night = s["luma"] < 50
        rows.append((s, p1, night))
        if not night and p1 > BLACK_P1_SHOT:
            iss.append(rep.issue("warn", f"shot {s['idx'] + 1}: lifted blacks (p1 luma {p1:.0f}); exposure belongs "
                                         "before the LUT, never a post-LUT brightness lift",
                                 t=s["start"], t_end=s["end"], frames=[(s["f0"] + s["f1"]) // 2], tag="black"))
        if not night and s["luma"] > 90 and p1 < BLACK_P1_CRUSH_SHOT:
            iss.append(rep.issue("warn", f"shot {s['idx'] + 1}: crushed blacks on a daylight shot (p1 luma {p1:.1f}); "
                                         "the house grade is the LUT only (rule 120)",
                                 t=s["start"], t_end=s["end"], frames=[(s["f0"] + s["f1"]) // 2], tag="black"))
    vals = [p for _, p, night in rows if not night]
    med = float(np.median(vals)) if vals else None
    lo, hi = BLACK_P1_RANGE
    if med is not None and med > hi:
        iss.insert(0, rep.issue("warn", f"milky blacks: median black point p1 {med:.1f} (> {hi:g}; reference 8, "
                                        "LUT-only daylight 29-41)"))
    elif med is not None and med < lo:
        iss.insert(0, rep.issue("warn", f"crushed blacks: median black point p1 {med:.1f} (< {lo:g}); a curve, look or "
                                        "exposure pull is crushing the toe (rule 120)"))
    rep.add("black_level", "picture", f"Black level (median p1 {lo:g}-{hi:g}: not crushed, not milky)", iss[:40],
            value={"median_p1": None if med is None else round(med, 1),
                   "p10_p1": round(float(np.percentile(vals, 10)), 1) if vals else None,
                   "p90_p1": round(float(np.percentile(vals, 90)), 1) if vals else None, "shots": len(vals),
                   "reference_p1": 8.5, "range": list(BLACK_P1_RANGE)})

    # ---- duplicate frames (frame-rate judder from 23.976 / 29.97 sources)
    iss, total_d, per = [], 0, []
    fz = [(a, b) for a, b in frozen]
    det = detect_cuts(vs.g64, vs.mafd)
    trf = [int(t.get("frame", round(float(t["t"]) * fps))) for t in (raw.get("transitions") or [])]
    for s in real:
        if is_black_ok(s) or s["f1"] - s["f0"] < 6:
            continue
        # judge each continuous run separately: a cutaway switch inside the piece is a different picture
        bounds = [s["f0"]] + [d for d in det if s["f0"] + 2 < d < s["f1"] - 2] + [s["f1"]]
        idx = []
        for a, b in zip(bounds, bounds[1:]):
            m = vs.mafd[a + 1:b]
            if len(m) < 5:
                continue
            med_m = float(np.median(m))
            if med_m < 1.0:
                continue                                 # (almost) static content: a repeat is invisible
            idx += [a + 1 + i for i, v in enumerate(m) if v < 0.15 * med_m]
        idx = [i for i in idx if not _overlaps(i / fps, (i + 1) / fps, fz) and not any(abs(i - x) <= 3 for x in trf)]
        if not idx:
            continue
        rate = len(idx) / max(1e-6, s["dur"])
        total_d += len(idx)
        per.append({"shot": s["idx"] + 1, "dups": len(idx), "per_s": round(rate, 2), "clip": s.get("clip")})
        if len(idx) >= 2 and rate >= DUP_RATE:
            iss.append(rep.issue("warn", f"shot {s['idx'] + 1}: {len(idx)} duplicated frames ({rate:.1f}/s) - a "
                                         "frame-rate mismatch (23.976 -> 25?) pushed through fps; conform it",
                                 t=s["start"], t_end=s["end"], frames=idx[:2], tag="dup"))
    rep.add("duplicate_frames", "picture", "Duplicated frames in moving shots (frame-rate conform)", iss,
            value={"duplicates": total_d, "shots": per[:100]})

    # ---- flash / transition peak on the cut frame, transition sound on the cut
    trs = raw.get("transitions") or []
    sfx_ev = {e.get("on"): e for e in (raw.get("sfx") or []) if e.get("on")}
    iss, rows = [], []
    for k, tr in enumerate(trs):
        f = int(tr.get("frame", round(float(tr["t"]) * fps)))
        row = {"t": tr["t"], "kind": tr["kind"]}
        if tr["kind"] in ("flash", "dip_white") and 3 <= f < n - 3:
            w = vs.luma[f - 3:f + 4]
            pk = f - 3 + int(np.argmax(w))
            row["peak_frame_offset"] = pk - f
            row["luma"] = [round(float(x), 1) for x in w]
            if abs(pk - f) > 1:
                iss.append(rep.issue("fail", f"flash at {fmt_t(tr['t'])} peaks {pk - f:+d} frames off the cut",
                                     t=tr["t"], frames=[f - 1, f, pk], tag="flash"))
        elif tr["kind"] == "dip" and 3 <= f < n - 3:
            w = vs.luma[f - 3:f + 4]
            row["peak_frame_offset"] = int(np.argmin(w)) - 3
        ev = sfx_ev.get(f"transition:{k}")
        other = [e for e in (raw.get("sfx") or []) if e is not ev and e.get("align") == "peak" and ev is not None
                 and abs(float(e["t"]) - float(ev["t"])) < 0.06]
        if ev and other:
            row["sfx_offset_ms"] = None            # two transients on one frame (a riser into a burn): not separable
            row["note"] = f"shares the cut with {other[0]['name']}"
        elif ev and au and len(au.audio):
            path = ev["name"]
            if not os.path.exists(str(path)):
                try:
                    from . import music
                    path = music.sfx(ev["name"], cfg)
                except Exception:  # noqa: BLE001
                    path = None
            pk_s = float(ev.get("peak_s") or 0.0)
            off = sfx_offset(au.audio, au.SR, float(ev["t"]) - pk_s, path) if path else None
            row["sfx_offset_ms"] = None if off is None else round(off * 1000, 1)
            if off is not None and abs(off) > SFX_TOL_S:
                sev = "warn" if abs(off) <= 1.0 / fps else "fail"
                iss.append(rep.issue(sev, f"{ev['name']} transient lands {off * 1000:+.0f} ms from the cut at "
                                          f"{fmt_t(tr['t'])} (tolerance {SFX_TOL_S * 1000:.0f} ms)", t=tr["t"]))
        rows.append(row)
    orphans = [e for e in (raw.get("sfx") or []) if str(e.get("on", "")).startswith("transition:")
               and int(str(e["on"]).split(":")[1]) >= len(trs)]
    for e in orphans:
        iss.append(rep.issue("fail", f"sound {e['name']} at {fmt_t(e['t'])} is bound to a transition that no longer exists "
                                     "(rule 39)", t=e["t"]))
    rep.add("flash_sfx_alignment", "picture", "Flash peak on the cut frame, hit transient on the cut (rules 39, 83)", iss,
            value={"transitions": rows}, status=None if trs else "info")

    # ---- caption size
    cs = raw.get("captions_style") or {}
    caps = [c for x in (raw.get("segments") or []) for c in (x.get("captions") or [])]
    px = cs.get("px") or next((c.get("px") for c in caps if c.get("px")), None)
    if px and H:
        pct = 100.0 * float(px) / float(H)
        iss = []
        if not CAPTION_PCT[0] <= pct <= CAPTION_PCT[1]:
            iss.append(rep.issue("warn", f"caption type {px} px = {pct:.2f}% of frame height ({pct * 10.8:.0f} px at 1080); "
                                         f"reference {CAPTION_REF_PCT}% (~43 px at 1080)"))
        rep.add("caption_size", "captions", "Caption size vs reference (~43 px at 1080)", iss,
                value={"px": px, "pct_of_height": round(pct, 2), "px_at_1080": round(pct * 10.8, 1),
                       "reference_pct": CAPTION_REF_PCT, "captions": len(caps)})
    else:
        rep.add("caption_size", "captions", "Caption size vs reference (~43 px at 1080)", [], status="info",
                detail="no caption size in the sidecar")


DEVICE_KINDS = ("burn", "card", "inset", "gate", "letterbox", "typed_title", "film")


def device_rule_issues(raw):
    """Rule 38 (the reference wins): every device in the render (burns, cards, inset / gate / letterbox frames,
    typed titles, film character) must be one the creator's reference measurably has. [(severity, message)]."""
    used = [d for d in (raw.get("devices") or []) if d in DEVICE_KINDS]
    if not used:
        return []
    ref = raw.get("reference") or {}
    have = set(ref.get("devices") or [])
    if "film" in used and (raw.get("film") or {}).get("from_reference"):
        have.add("film")
    out = []
    for d in used:
        if d not in have:
            who = f"reference {ref['id']}" if ref.get("id") else "no reference"
            out.append(("fail", f"{d}: not measured on the creator's reference ({who}); home-made decorative effects "
                                "stay banned (rule 38)"))
    return out


def _warm_pct(img, side):
    """Share of burn-coloured pixels: red glow (side "out") or orange disc (side "in"); img is BGR."""
    b, g, r = (img[..., k].astype(np.int16) for k in range(3))
    if side == "out":
        m = (r > 170) & (g < 110) & (b < 110) & (r - g > 80)
    else:
        m = (r > 200) & (g > 70) & (g < 240) & (b < 130) & (r - b > 100)
    return float(m.mean())


def _sat(img):
    a = img.astype(np.float32)
    mx, mn = a.max(axis=2), a.min(axis=2)
    return float(np.mean(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)) * 255)


def _reference_checks(rep, vs, fps, n, raw, framed):
    """Checks for the reference-measured devices and the house grade: rule 38 device provenance, burn on the
    cut, the card when the reference opens on one, and the grade within bounds of the LUT-only picture."""
    if not raw:
        return
    # ---- rule 38: devices must come from the reference
    iss = [rep.issue(sev, msg) for sev, msg in device_rule_issues(raw)]
    rep.add("devices_vs_reference", "picture", "Effects measured on the reference only (rule 38)", iss,
            value={"used": raw.get("devices") or [], "reference": (raw.get("reference") or {}).get("devices")},
            status=None if raw.get("devices") else "info")
    # ---- burns: outgoing glow on the last frame before the cut, incoming disc on the cut frame
    iss, rows = [], []
    for tr in raw.get("transitions") or []:
        if tr.get("kind") != "burn":
            continue
        f = int(tr.get("frame", round(float(tr["t"]) * fps)))
        if not 3 <= f < n - 3:
            continue
        fo, fi = int(tr.get("frames_out", 1)), int(tr.get("frames_in", 1))
        pa = [_warm_pct(vs.frame(x), "out") for x in range(f - fo - 1, f + 1)]
        pb = [_warm_pct(vs.frame(x), "in") for x in range(f - 1, f + fi + 1)]
        lum = [float(vs.luma[x]) for x in range(f - 3, f + 3)]
        a_on, a_before = pa[-2], pa[0]                 # glow on frame f-1, none on f-fo-1
        b_on, b_after = pb[1], pb[-1]                  # disc on frame f, none after f+fi
        row = {"t": tr["t"], "frame": f, "glow_pct": round(a_on, 3), "disc_pct": round(b_on, 3),
               "luma": [round(x, 1) for x in lum], "jump": round(max(lum[2], lum[3]) - (lum[0] + lum[5]) / 2, 1)}
        rows.append(row)
        soft = float(tr.get("strength", 1.0)) < 0.8         # a soft burn (a card's exit) only warms the frame
        if not soft and not (b_on > 0.08 and b_on > 2 * b_after):
            iss.append(rep.issue("fail", f"burn at {fmt_t(tr['t'])}: no incoming disc on the cut frame "
                                         f"({b_on * 100:.0f}% orange; rule 123)", t=tr["t"], frames=[f - 1, f], tag="burn"))
        if float(tr.get("strength", 1.0)) >= 0.8 and not (a_on > 0.04 and a_on > 2 * max(a_before, 0.005)):
            iss.append(rep.issue("warn", f"burn at {fmt_t(tr['t'])}: weak outgoing glow on the frame before the cut "
                                         f"({a_on * 100:.0f}% red)", t=tr["t"], frames=[f - 1, f], tag="burn"))
    rep.add("burn_timing", "picture", "Film burns: glow before the cut, disc on the cut (rule 123)", iss,
            value={"burns": rows}, status=None if rows else "info")
    # ---- the card, when the reference opens on one
    ref = raw.get("reference") or {}
    cards = [x for x in raw.get("segments") or [] if x.get("kind") == "card" or x.get("card")]
    iss = []
    if ref.get("card"):
        if not cards or float(cards[0]["start"]) > 0.05:
            iss.append(rep.issue("fail", "the reference opens on a title card (\"COMING UP\"); this render does not "
                                         "(rule 45)", t=0.0))
        else:
            c = cards[0]
            f = min(n - 1, int(round((float(c["start"]) + float(c["end"])) / 2 * fps)))
            img = vs.frame(f)
            b, g, r = (img[..., k].astype(np.int16) for k in range(3))
            ink = float(((r > 180) & (g > 180) & (b < 110)).mean())
            if ink < 0.004:
                iss.append(rep.issue("fail", f"card at 0:00 shows almost no yellow text ({ink * 100:.2f}% of pixels)",
                                     t=float(c["start"]), frames=[f], tag="card"))
    rep.add("card_present", "picture", "Title card when the reference has one", iss,
            value={"reference_card": bool(ref.get("card")), "cards": len(cards)}, status=None if ref.get("card") else "info")
    # ---- grade within bounds of the LUT-only picture of the same source
    iss, rows = [], []
    for x in raw.get("segments") or []:
        ls = x.get("lut_stats")
        if not ls or x.get("frame") or x.get("kind") != "clip" or float(x["end"]) - float(x["start"]) < 0.3 \
                or x.get("push") or x.get("cutaways"):
            continue                             # a push or a cutaway shows another picture than the probe
        crop = x.get("crop") or [0, 0, 1, 1]
        if crop[2] < 0.9:                       # a tight crop sees a different part of the frame than the probe
            continue
        f = int(round((float(x["start"]) + float(x["end"])) / 2 * fps))
        img = vs.frame(f)
        if img is None:
            continue
        g = img[..., ::-1].astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        hh = g.shape[0]
        body = g[: int(hh * 0.8)]                # outside the caption band
        st = {"luma": float(np.median(body)), "p1": float(np.percentile(body, 1)), "sat": _sat(img[: int(hh * 0.8)])}
        row = {"t": round(float(x["start"]), 2), "clip": x.get("clip_rel"), "render": {k: round(v, 1) for k, v in st.items()},
               "lut_only": ls}
        rows.append(row)
        why = []
        if abs(st["luma"] - ls["luma"]) > GRADE_LUMA_TOL:
            why.append(f"luma {st['luma']:.0f} vs {ls['luma']:.0f}")
        if st["p1"] < min(ls["p1"], max(ls["p1"] - GRADE_P1_DROP, BLACK_P1_RANGE[0])) - 0.5:
            why.append(f"black point {st['p1']:.0f} vs {ls['p1']:.0f} (crushed)")
        if st["sat"] > ls["sat"] * GRADE_SAT_GAIN + 4:
            why.append(f"saturation {st['sat']:.0f} vs {ls['sat']:.0f}")
        if why:
            iss.append(rep.issue("warn", f"{x.get('clip_rel') or 'shot'} at {fmt_t(x['start'])}: grade outside the LUT-only "
                                         f"bounds ({'; '.join(why)}; rule 94/120)", t=float(x["start"]), frames=[f], tag="grade"))
    rep.add("grade_bounds", "picture", "Grade within bounds of the LUT-only house grade (rules 94, 120)", iss[:30],
            value={"shots": rows[:60], "tolerance": {"luma": GRADE_LUMA_TOL, "p1_drop": GRADE_P1_DROP,
                                                     "sat_gain": GRADE_SAT_GAIN}},
            status=None if rows else "info")


def _nanmed(xs):
    xs = [x for x in xs if x is not None]
    return round(float(np.median(xs)), 2) if xs else None


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _version_of(stem):
    m = re.search(r"_v(\d+)", stem)
    return int(m.group(1)) if m else None


# ==================================================================================================
# contact sheets + html
# ==================================================================================================

def _contact_sheets(vs, shots, fps, qa_dir, stem, issue_frames, note="first | mid | last of each shot") -> list:
    """First / mid / last frame of every shot (so both sides of every cut), <= 24 tiles per sheet,
    timestamp + shot number on each tile, red/amber border where a check flagged the frame."""
    from PIL import Image, ImageDraw
    tiles = []
    for s in shots:
        for k, f in enumerate(s["sample_frames"]):
            role = ("first", "mid", "last")[k] if len(s["sample_frames"]) == 3 else ("first", "last")[min(k, 1)]
            if len(s["sample_frames"]) == 1:
                role = "only"
            tiles.append((f, s["idx"], role, s))
    if not tiles or vs.n == 0:
        return []
    font, small = _font(20), _font(15)
    tw, th = vs.w, vs.h
    per = SHEET_COLS * SHEET_ROWS
    out = []
    hdr = 40
    for si in range(0, len(tiles), per):
        chunk = tiles[si:si + per]
        rows = math.ceil(len(chunk) / SHEET_COLS)
        img = Image.new("RGB", (SHEET_COLS * tw + (SHEET_COLS + 1) * 6, hdr + rows * (th + 6) + 6), (14, 14, 16))
        d = ImageDraw.Draw(img)
        a, b = chunk[0][0] / fps, chunk[-1][0] / fps
        d.text((10, 10), f"{stem}  ·  sheet {si // per + 1}/{math.ceil(len(tiles) / per)}  ·  {fmt_t(a)} - {fmt_t(b)}"
                         f"  ·  {note}", fill=(230, 230, 230), font=font)
        for k, (f, idx, role, s) in enumerate(chunk):
            x, y = 6 + (k % SHEET_COLS) * (tw + 6), hdr + (k // SHEET_COLS) * (th + 6)
            fr = vs.frame(f)
            if fr is not None:
                img.paste(Image.fromarray(fr[:, :, ::-1]), (x, y))
            sev = issue_frames.get(f)
            if sev:
                col = (230, 60, 60) if sev == "fail" else (240, 170, 40)
                d.rectangle([x - 3, y - 3, x + tw + 2, y + th + 2], outline=col, width=4)
            label = f"{fmt_t(f / fps)}  f{f}  #{idx + 1} {role}"
            bx = d.textbbox((0, 0), label, font=small)
            d.rectangle([x + 4, y + 4, x + 12 + bx[2], y + 10 + bx[3]], fill=(0, 0, 0))
            d.text((x + 8, y + 5), label, fill=(255, 225, 90), font=small)
            sub = f"{s['dur']:.2f}s" + (f" {s['kind']}" if s.get("kind") else "") + (f" faces {s['faces']}" if s.get("faces") else "")
            bx = d.textbbox((0, 0), sub, font=small)
            d.rectangle([x + 4, y + th - 12 - bx[3], x + 12 + bx[2], y + th - 4], fill=(0, 0, 0))
            d.text((x + 8, y + th - 10 - bx[3]), sub, fill=(200, 200, 200), font=small)
        p = Path(qa_dir) / f"sheet_{si // per + 1:03d}.jpg"
        img.save(p, "JPEG", quality=82)
        out.append(p)
    return out


def _html_page(rep, out_dir) -> str:
    esc = _html.escape
    s = rep["summary"]

    def rel(p):
        try:
            return os.path.relpath(p, out_dir)
        except ValueError:
            return p

    col = {"pass": "#3fb67a", "info": "#6b7a90", "warn": "#e0a23a", "fail": "#e05555"}
    rows = []
    for c in rep["checks"]:
        val = c.get("value")
        vs = ""
        if isinstance(val, dict):
            vs = ", ".join(f"{k}={v}" for k, v in val.items() if not isinstance(v, (list, dict)))
        elif val is not None:
            vs = str(val)
        issues = "".join(
            f"<li><span class='chip' style='background:{col[i['severity']]}'>{i['severity']}</span> "
            f"<b>{esc(i['tc'])}</b> {esc(i['msg'])} " +
            "".join(f"<a href='{esc(rel(e))}'><img src='{esc(rel(e))}' loading='lazy'></a>" for e in i["evidence"][:3]) +
            "</li>" for i in c["issues"][:60])
        more = f"<li>… {len(c['issues']) - 60} more in the JSON</li>" if len(c["issues"]) > 60 else ""
        rows.append(f"<section><h3><span class='chip' style='background:{col.get(c['status'], '#888')}'>{c['status']}</span> "
                    f"{esc(c['title'])}</h3><div class='val'>{esc(vs)} {esc(c.get('detail') or '')}</div>"
                    f"<ul>{issues}{more}</ul></section>")
    sheets = "".join(f"<a href='{esc(rel(p))}'><img class='sheet' src='{esc(rel(p))}' loading='lazy'></a>" for p in s["sheets"])
    top = "".join(f"<li>{esc(t)}</li>" for t in s["top_issues"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QA {esc(Path(rep['video']).name)}</title><style>
body{{background:#111214;color:#e6e6e6;font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px;max-width:1200px;margin:auto}}
h1{{font-size:20px}} h3{{font-size:16px;margin:4px 0}} section{{background:#1a1b1f;border-radius:10px;padding:10px 14px;margin:10px 0}}
.chip{{display:inline-block;padding:1px 8px;border-radius:9px;color:#111;font-size:12px;font-weight:700;text-transform:uppercase}}
ul{{padding-left:18px;margin:6px 0}} li{{margin:4px 0}} li img{{height:64px;border-radius:4px;margin-left:6px;vertical-align:middle}}
.val{{color:#9aa3b2;font-size:13px}} img.sheet{{width:100%;margin:8px 0;border-radius:6px}} a{{color:#8ab4ff}}
</style></head><body>
<h1><span class='chip' style='background:{col[s['status']]}'>{s['status']}</span> {esc(Path(rep['video']).name)}</h1>
<div class='val'>{rep['duration']:.1f}s · {rep['width']}x{rep['height']} @ {rep['fps']:g} fps · {s['shots']} shots ·
{s['fail']} fail · {s['warn']} warn · loudness {s['loudness_lufs']} LUFS · TP {s['true_peak_dbtp']} dBTP · QA {s['runtime_s']}s ·
sidecar: {esc(str(s['sidecar']))}</div>
<section><h3>Top issues</h3><ul>{top or '<li>none</li>'}</ul></section>
{''.join(rows)}
<h2>Contact sheets</h2>{sheets}
</body></html>"""
