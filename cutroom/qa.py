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
EMPTY_MOTION = 0.6             # px/frame camera motion below which a face-less shot may be an empty plate
EMPTY_ACTIVITY = 1.5           # mean abs frame diff (64x36, 0-255) below which nothing moves in frame
SHEET_COLS, SHEET_ROWS = 4, 6  # 24 tiles per sheet
MAX_EVIDENCE = 400
JPEG_Q = 72

INTENTIONAL_STILL = {"still", "title", "black", "card", "freeze"}
INTENTIONAL_BLACK = {"black", "title", "fade"}
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
        g64, luma, mafd, sharp, vel, jpegs = [], [], [], [], [], []
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
            off = lo - I if I < lo else (I - hi if I > hi else 0.0)
            if off > 0:
                iss.append(rep.issue("fail" if off > LUFS_SLACK else "warn",
                                     f"integrated loudness {I:.1f} LUFS, target {lo:g}..{hi:g}"))
        if TP is not None and TP > TP_MAX:
            iss.append(rep.issue("fail", f"true peak {TP:.1f} dBTP > {TP_MAX:g} (lower the gain or limit)"))
        mom = au.momentary
        value = {"integrated_lufs": I, "true_peak_dbtp": TP, "lra": au.lra}

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
            if s["kind"] in ("flash",):
                continue
            iss.append(rep.issue("fail", "1-frame shot (flash frame)", t=s["f0"] / fps,
                                 frames=[s["f0"] - 1, s["f0"], s["f1"]], tag="flash"))
    # also catch detected 1-frame shots when the plan thinks the span is continuous
    if shots_plan:
        for a, b2 in zip(det, det[1:]):
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
        if s["luma"] < DARK_LUMA:
            iss.append(shot_issue("warn", s, f"dark (mean luma {s['luma']:.0f} < {DARK_LUMA:g}; rule 90)", "dark"))
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

def _contact_sheets(vs, shots, fps, qa_dir, stem, issue_frames) -> list:
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
                         f"  ·  first | mid | last of each shot", fill=(230, 230, 230), font=font)
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
