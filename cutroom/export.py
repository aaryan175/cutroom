"""Deliverables: one edit, every platform.

    cutroom export RENDER.mp4|PLAN.json --preset youtube|reels|tiktok|shorts|square|feed|all
                   [--cutdown 15,30,60] [--thumbs] [--no-captions] [--keep-burned] [--fps N] [--trim]
    cutroom cutdown PLAN.json --len 30            build plans/<slug>_cut30.json only (the AI can refine it)

What it makes, in renders/exports/<render stem>/:
  <stem>_<preset>.mp4      platform presets: resolution, frame rate, bitrate/CRF, max duration, loudness
                           target (two-pass loudnorm, verified with ebur128) and caption safe areas
  9:16 / 4:5 / 1:1         auto-reframed from the 16:9 render: faces are detected (YuNet; Haar cascades when
                           YuNet is unavailable) on every sampled frame, each shot (from the render sidecar)
                           gets its own smoothed crop path, a static crop when the subject barely moves, and
                           the crop is clamped so a detected head is never cut. Shots with no face follow
                           the picture's saliency, else the centre.
                           Title cards and shots carrying an overlay are letterboxed over a blurred copy.
                           Burned-in captions would be cut by the crop, so a caption-free "clean feed" of
                           the plan is rendered once (cached pieces make this cheap) and the captions are
                           re-set for the vertical frame inside the platform's safe area.
  <stem>.srt / .vtt        captions at the exact heard times from the sidecar (or the transcripts)
  <stem>.chapters.txt      YouTube chapter lines from the plan's sections
  thumbs/thumb_0N.jpg      thumbnail candidates (sharp, face visible, eyes open, smile, exposure, thirds)
  thumbs/sheet.jpg         contact sheet of the candidates with their scores
  export.json              manifest: every output, its measured size/fps/duration/loudness, warnings

Cutdowns (`--cutdown 30`): a new plan `<slug>_cut30.json` keeps the hook (the opening of the first
section), picks the strongest lines (transcript stakes words + highlight score + length) in story order and
ends on the payoff (the last line), fitted to the length within 10 % using the renderer's own timeline
resolution; then it is rendered and exported like any other edit. A plan-level cold_open is kept with its
teaser lines resolved to the full edit's clip/in/out (the cut renames the sections its `from` refs name) and
its auto shot pool frozen; it is dropped when it would take over half the cut.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import db, governor, media

# --------------------------------------------------------------------------------------------- presets
# Safe areas are fractions of the frame kept free of text (top, bottom, left, right): platform UI
# (captions, buttons, account name) sits there. Max durations are what each surface accepts as that
# format at the time of writing; override with --max-dur when a platform changes its rules.
PRESETS = {
    "youtube": {"label": "YouTube 16:9", "aspect": (16, 9), "width": 1920, "height": 1080, "fps": None,
                "crf": 18, "profile": "high", "vbitrate": None, "abitrate": "320k", "I": -14.0, "TP": -1.5,
                "max_dur": None, "safe": (0.0, 0.0, 0.0, 0.0)},
    "youtube_4k": {"label": "YouTube 4K", "aspect": (16, 9), "width": 3840, "height": 2160, "fps": None,
                   "crf": 18, "profile": "high", "vbitrate": None, "abitrate": "320k", "I": -14.0, "TP": -1.5,
                   "max_dur": None, "safe": (0.0, 0.0, 0.0, 0.0)},
    "reels": {"label": "Instagram Reels 9:16", "aspect": (9, 16), "width": 1080, "height": 1920, "fps": None,
              "crf": None, "profile": "high", "vbitrate": "12M", "abitrate": "256k", "I": -14.0, "TP": -1.0,
              "max_dur": 180.0, "safe": (0.12, 0.24, 0.06, 0.12)},
    "tiktok": {"label": "TikTok 9:16", "aspect": (9, 16), "width": 1080, "height": 1920, "fps": None,
               "crf": None, "profile": "high", "vbitrate": "12M", "abitrate": "256k", "I": -14.0, "TP": -1.0,
               "max_dur": 600.0, "safe": (0.09, 0.26, 0.06, 0.14)},
    "shorts": {"label": "YouTube Shorts 9:16", "aspect": (9, 16), "width": 1080, "height": 1920, "fps": None,
               "crf": None, "profile": "high", "vbitrate": "12M", "abitrate": "256k", "I": -14.0, "TP": -1.0,
               "max_dur": 60.0, "safe": (0.08, 0.2, 0.06, 0.12)},
    "square": {"label": "Square 1:1", "aspect": (1, 1), "width": 1080, "height": 1080, "fps": None,
               "crf": None, "profile": "high", "vbitrate": "8M", "abitrate": "256k", "I": -14.0, "TP": -1.0,
               "max_dur": 60.0, "safe": (0.05, 0.1, 0.05, 0.05)},
    "feed": {"label": "Feed 4:5", "aspect": (4, 5), "width": 1080, "height": 1350, "fps": None,
             "crf": None, "profile": "high", "vbitrate": "8M", "abitrate": "256k", "I": -14.0, "TP": -1.0,
             "max_dur": 60.0, "safe": (0.05, 0.12, 0.05, 0.05)},
}
ALL = ["youtube", "reels", "tiktok", "shorts", "square"]
ACCEPTED_FPS = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)


def preset_names(spec) -> list:
    out = []
    for x in (spec if isinstance(spec, (list, tuple)) else [spec]):
        for p in str(x).split(","):
            p = p.strip().lower()
            if not p:
                continue
            if p == "all":
                out += [n for n in ALL if n not in out]
            elif p in PRESETS:
                if p not in out:
                    out.append(p)
            else:
                raise ValueError(f"unknown preset {p!r}; presets: {', '.join(PRESETS)}, all")
    return out


def _log(echo, msg):
    if echo:
        echo(msg)


# --------------------------------------------------------------------------------------------- probing
def video_info(cfg, path) -> dict:
    info = media.probe(cfg, path)
    s = media.summarize_probe(info)
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    if abs(int(s.get("rotation") or 0)) % 180 == 90:
        w, h = h, w
    return {"w": w, "h": h, "fps": float(s.get("fps") or 25.0), "duration": float(s.get("duration") or 0),
            "has_audio": bool(s.get("has_audio")), "codec": s.get("codec")}


def load_sidecar(video) -> dict | None:
    p = Path(str(video)[:-4] + ".json") if str(video).endswith(".mp4") else None
    if p and p.exists():
        try:
            d = json.loads(p.read_text())
            if d.get("schema", "").startswith("cutroom-render"):
                return d
        except (OSError, ValueError):
            pass
    return None


def out_dir(cfg, video) -> Path:
    d = cfg.p("renders", "exports", Path(video).stem)
    d.mkdir(parents=True, exist_ok=True)
    return d


def sidecar_plan(cfg, sidecar) -> Path | None:
    """The plan a render came from. A project folder that was copied or moved keeps absolute paths in its
    sidecars, so a plan of the same name inside this project wins."""
    p = Path((sidecar or {}).get("plan") or "")
    if not p.name:
        return None
    local = cfg.p("plans", p.name)
    if local.exists():
        return local
    return p if p.exists() else None


def plan_sha(plan) -> str:
    """Canonical content hash of a plan dict (key order and whitespace don't count; the version number does)."""
    return hashlib.sha1(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                        .encode()).hexdigest()[:16]


def find_render(cfg, plan_path, preview=None, version=None, names=(), fresh=True) -> Path | None:
    """The newest render of this plan, by sidecar.

    A render counts when its sidecar's plan is this file (or `names`: render stems like "<slug>_v4" whose
    sidecar points at a factory candidate), it is a WHOLE render (no `only`: a cold-open-only render is never
    a base), its version matches the plan's (`version`, default: the plan file's own "version"), and it is
    fresh: same content hash when the sidecar has one, else not older than the plan file.
    preview=True/False demands that mode (the factory compares preview with preview: a final render as the
    base of a preview made every PSNR and QA comparison wrong); None prefers final, then preview."""
    plan_path = Path(plan_path)
    want = str(plan_path.resolve())
    name = plan_path.name
    try:
        pobj = json.loads(plan_path.read_text())
    except (OSError, ValueError):
        pobj = None
    if version is None and isinstance(pobj, dict):
        version = pobj.get("version")
    psha = plan_sha(pobj) if isinstance(pobj, dict) else None
    try:
        pmt = plan_path.stat().st_mtime
    except OSError:
        pmt = 0
    stems = tuple(f"{n}_" for n in names or ())
    best = None
    for sc in cfg.p("renders").glob("*.json"):
        if sc.name.endswith((".qa.json", ".card.json")):
            continue
        try:
            d = json.loads(sc.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("only"):
            continue
        mp4 = sc.with_suffix(".mp4")
        by_name = bool(stems) and mp4.name.startswith(stems) and not re.match(r".*_v\d+_var\d+", mp4.stem)
        if d.get("plan") != want and Path(d.get("plan") or "").name != name and not by_name:
            continue
        if not mp4.exists():
            continue
        if preview is not None and bool(d.get("preview")) != bool(preview):
            continue
        if version is not None and d.get("version") is not None and str(d.get("version")) != str(version):
            continue
        if fresh:
            if d.get("plan_sha") and psha:
                if d["plan_sha"] != psha and not by_name:
                    continue
            elif not by_name and mp4.stat().st_mtime < pmt - 1:
                continue
        key = (0 if d.get("preview") else 1, mp4.stat().st_mtime)
        if best is None or key > best[0]:
            best = (key, mp4)
    return best[1] if best else None


def _cli(cfg, *args, echo=None, timeout=7200) -> str:
    """Run a cutroom CLI command for this project in a subprocess (renders use their own process pool)."""
    env = dict(os.environ)
    env["CUTROOM_WORK"] = str(cfg.work)
    repo = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    r = subprocess.run([sys.executable, "-m", "cutroom.cli", *[str(a) for a in args]], cwd=str(cfg.work), env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"cutroom {' '.join(map(str, args))[:200]} failed:\n{(r.stderr or r.stdout)[-1500:]}")
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def render_plan(cfg, plan_path, preview=False, out=None, echo=print) -> Path:
    args = ["render", plan_path] + (["--preview"] if preview else []) + (["--out", out] if out else [])
    _log(echo, f"export: rendering {Path(plan_path).name}{' (preview)' if preview else ''}")
    return Path(_cli(cfg, *args))


# --------------------------------------------------------------------------------------------- frames
def sample_frames(cfg, video, every=0.2, width=640, gray=True):
    """Decode `video` at 1/every fps, `width` wide. Returns (times, frames uint8 HxW or HxWx3), (w, h)."""
    vi = video_info(cfg, video)
    h = max(2, int(round(width * vi["h"] / max(vi["w"], 1) / 2)) * 2)
    pix = "gray" if gray else "rgb24"
    cmd = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(video), "-an",
           "-vf", f"fps={1 / every:.6f}:round=near,scale={width}:{h}", "-pix_fmt", pix, "-f", "rawvideo", "-"]
    g = governor.active()
    if g is not None:
        r = g.run(cmd, kind=None, timeout=3600, text=False)
        if getattr(r, "killed", None):
            raise media.GovernorKilled(f"stopped by resource governor: {r.killed}")
    else:
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
    if r.returncode:
        raise media.FFError((r.stderr or b"").decode(errors="ignore")[-500:])
    ch = 1 if gray else 3
    fs = width * h * ch
    n = len(r.stdout) // fs
    frames = [np.frombuffer(r.stdout[i * fs:(i + 1) * fs], np.uint8).reshape((h, width) if gray else (h, width, 3))
              for i in range(n)]
    times = [round(i * every, 4) for i in range(n)]
    return times, frames, (width, h)


_CASCADES = {}


def _cascade(name):
    import cv2
    if name not in _CASCADES:
        c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        _CASCADES[name] = None if c.empty() else c
    return _CASCADES[name]


def detect_faces(img) -> list:
    """Faces as (x, y, w, h) fractions of the frame (greyscale or BGR): the YuNet DNN detector in vision.py
    (side-on, small and partly covered faces, few false positives in caption bands). When YuNet is not
    available (offline with no cached model, CUTROOM_FACES=haar, or a failing model) the Haar cascades below
    run instead. Tests replace this with a synthetic detector."""
    import cv2
    try:
        from . import vision
        det = vision.face_detector()
        if det.backend == "yunet":
            bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img
            return [tuple(f["box"]) for f in vision.detect_faces(bgr)]
    except Exception:  # noqa: BLE001 - never fail an export on the face model: use the cascades
        pass
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return detect_faces_haar(gray)


def detect_faces_haar(gray) -> list:
    """Frontal + profile (both directions) Haar cascades on an equalised greyscale frame, overlapping boxes
    merged: the fallback when YuNet is unavailable."""
    import cv2
    h, w = gray.shape[:2]
    eqd = cv2.equalizeHist(gray)
    m = max(18, int(min(w, h) * 0.06))
    boxes = []
    fr = _cascade("haarcascade_frontalface_default.xml")
    if fr is not None:
        boxes += [tuple(b) for b in fr.detectMultiScale(eqd, scaleFactor=1.12, minNeighbors=6, minSize=(m, m))]
    pr = _cascade("haarcascade_profileface.xml")
    if pr is not None:
        boxes += [tuple(b) for b in pr.detectMultiScale(eqd, scaleFactor=1.12, minNeighbors=7, minSize=(m, m))]
        flip = cv2.flip(eqd, 1)
        boxes += [(w - x - bw, y, bw, bh) for x, y, bw, bh in
                  pr.detectMultiScale(flip, scaleFactor=1.12, minNeighbors=7, minSize=(m, m))]
    out = []
    for b in sorted(boxes, key=lambda b: -b[2] * b[3]):
        if any(_iou(b, o) > 0.3 for o in out):
            continue
        out.append(b)
    return [(x / w, y / h, bw / w, bh / h) for x, y, bw, bh in out]


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter + 1e-9)


_HOG = None


def detect_people(gray) -> list:
    """Standing / walking people as (x, y, w, h) fractions (OpenCV HOG people detector), strongest first.
    Used for shots where nobody faces the camera (back views, backlit profiles)."""
    import cv2
    global _HOG
    if _HOG is None:
        _HOG = cv2.HOGDescriptor()
        _HOG.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    h, w = gray.shape[:2]
    rects, weights = _HOG.detectMultiScale(gray, winStride=(8, 8), padding=(8, 8), scale=1.05)
    ws = np.asarray(weights).reshape(-1) if len(rects) else []
    out = [((x / w, y / h, bw / w, bh / h), float(s)) for (x, y, bw, bh), s in zip(rects, ws) if s >= 0.5]
    return [b for b, _ in sorted(out, key=lambda x: -x[1])]


def _saliency_x(gray) -> tuple:
    """(x centre 0-1, confidence 0-1) of the picture's detail energy (edges), for shots without faces."""
    import cv2
    g = cv2.GaussianBlur(gray, (0, 0), 1.5)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    e = np.hypot(gx, gy).sum(axis=0)
    tot = float(e.sum())
    if tot <= 0:
        return 0.5, 0.0
    xs = (np.arange(len(e)) + 0.5) / len(e)
    cx = float((xs * e).sum() / tot)
    spread = float(np.sqrt(((xs - cx) ** 2 * e).sum() / tot))      # 0.29 = uniform
    return cx, max(0.0, min(1.0, (0.29 - spread) / 0.15))


# --------------------------------------------------------------------------------------------- shots
def shot_list(sidecar, duration, times=None, frames=None, overlay_fit=True) -> list:
    """[{t0, t1, mode}] covering the video. Cuts come from the sidecar (segments + cutaways), so a crop path
    never smooths across a cut. mode 'fit' = letterbox (title cards, black, shots carrying an overlay)."""
    cuts = {0.0, round(duration, 4)}
    fits = []
    if sidecar:
        for s in sidecar.get("segments", []):
            cuts.add(round(float(s["start"]), 4))
            cuts.add(round(float(s["end"]), 4))
            for c in s.get("cutaways") or []:
                cuts.add(round(float(c["t0"]), 4))
                cuts.add(round(float(c["t1"]), 4))
            if s.get("kind") in ("title", "black") or s.get("type") in ("title", "black"):
                fits.append((float(s["start"]), float(s["end"])))
            for o in (s.get("overlays") or []) if overlay_fit else []:
                fits.append((float(o["t0"]), float(o["t1"])))
    elif frames:
        prev = None
        for t, f in zip(times, frames):
            hist = np.histogram(f, bins=32, range=(0, 255))[0].astype(np.float32)
            hist /= hist.sum() + 1e-9
            if prev is not None and float(np.abs(hist - prev).sum()) > 0.5:
                cuts.add(round(t, 4))
            prev = hist
    pts = sorted(c for c in cuts if 0 <= c <= duration)
    shots = []
    for a, b in zip(pts, pts[1:]):
        if b - a < 1e-3:
            continue
        mode = "track"
        for f0, f1 in fits:
            if min(b, f1) - max(a, f0) > 0.25 * (b - a) or (f0 <= a + 1e-3 and f1 >= b - 1e-3):
                mode = "fit"
                break
        shots.append({"t0": a, "t1": b, "mode": mode})
    return shots


def _smooth(vals, sigma):
    if len(vals) < 3 or sigma <= 0:
        return list(vals)
    r = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    v = np.asarray(vals, dtype=float)
    pad = np.r_[np.full(r, v[0]), v, np.full(r, v[-1])]
    return list(np.convolve(pad, k, mode="valid"))


def plan_crops(times, faces, saliency, shots, cw, every=0.2, head_margin=0.35, static_travel=0.04,
               people=None) -> list:
    """Per shot: crop centre path for a crop `cw` wide (fraction of the source width).

    For each sampled frame: the subject is the largest face (or all faces when they fit together); its head
    box is the face widened by `head_margin` each side. Missing samples are filled from neighbours, the
    path is Gaussian-smoothed (about 0.6 s), held static when it would travel less than `static_travel`, and
    finally clamped so the head box stays inside the crop at every sample (when it fits at all) and the
    crop stays inside the frame. Returns shots with "path": [(t, cx)], "how": face|saliency|centre|fit."""
    half = cw / 2
    out = []
    for sh in shots:
        idx = [i for i, t in enumerate(times) if sh["t0"] - 1e-6 <= t < sh["t1"] - 1e-6]
        if sh["mode"] == "fit" or not idx:
            out.append({**sh, "how": "fit" if sh["mode"] == "fit" else "centre",
                        "path": [(sh["t0"], 0.5)], "heads": []})
            continue
        targets, heads = [], []
        for i in idx:
            fs = [f for f in faces[i] if 0.03 <= f[3] <= 0.95]
            if not fs:
                targets.append(None)
                heads.append(None)
                continue
            big = max(fs, key=lambda f: f[2] * f[3])
            keep = [f for f in fs if f[2] * f[3] >= 0.35 * big[2] * big[3]]
            lo = min(f[0] - head_margin * f[2] for f in keep)
            hi = max(f[0] + f[2] * (1 + head_margin) for f in keep)
            if hi - lo > cw:                                   # together they do not fit: the biggest one
                lo, hi = big[0] - head_margin * big[2], big[0] + big[2] * (1 + head_margin)
            targets.append((lo + hi) / 2)
            heads.append((max(0.0, lo), min(1.0, hi)))
        n_face = sum(t is not None for t in targets)
        how = "face"
        if n_face == 0 and people is not None:
            # nobody faces the camera: follow a detected person; their head is the top of the box
            for k, i in enumerate(idx):
                ps = people[i] if i < len(people) else []
                if ps:
                    x, y, w, h = ps[0]
                    targets[k] = x + w / 2
                    heads[k] = (max(0.0, x + 0.2 * w), min(1.0, x + 0.8 * w))
            if any(t is not None for t in targets):
                how = "person"
                n_face = sum(t is not None for t in targets)
        if n_face == 0:
            sal = [saliency[i] for i in idx]
            conf = float(np.mean([c for _, c in sal])) if sal else 0.0
            if conf > 0.25:
                how = "saliency"
                cx = float(np.median([x for x, _ in sal]))
                targets = [cx] * len(idx)
            else:
                how = "centre"
                targets = [0.5] * len(idx)
        # fill gaps from the nearest known sample
        known = [k for k, v in enumerate(targets) if v is not None]
        filled = []
        for k in range(len(targets)):
            if targets[k] is not None:
                filled.append(targets[k])
            else:
                nb = min(known, key=lambda j: abs(j - k))
                filled.append(targets[nb])
        path = _smooth(filled, sigma=max(1.0, 0.6 / every))
        if max(path) - min(path) < static_travel:
            path = [float(np.median(path))] * len(path)
        final = []
        for k, cx in enumerate(path):
            hb = heads[k]
            if hb and hb[1] - hb[0] <= cw:
                cx = min(max(cx, hb[1] - half), hb[0] + half)      # head fully inside
            cx = min(max(cx, half), 1 - half)
            final.append(cx)
        # a second light smoothing, then re-clamp: the clamp has the last word
        final2 = _smooth(final, sigma=1.0) if how == "face" and len(set(final)) > 1 else final
        res = []
        for k, cx in enumerate(final2):
            hb = heads[k]
            if hb and hb[1] - hb[0] <= cw:
                cx = min(max(cx, hb[1] - half), hb[0] + half)
            res.append(round(min(max(cx, half), 1 - half), 5))
        out.append({**sh, "how": how, "path": [(times[i], res[k]) for k, i in enumerate(idx)], "heads": heads,
                    "face_samples": n_face, "samples": len(idx)})
    return out


def crop_x_at(shot, t) -> float:
    path = shot["path"]
    if len(path) == 1 or t <= path[0][0]:
        return path[0][1]
    if t >= path[-1][0]:
        return path[-1][1]
    for (ta, xa), (tb, xb) in zip(path, path[1:]):
        if ta <= t <= tb:
            return xa + (xb - xa) * (t - ta) / max(tb - ta, 1e-9)
    return path[-1][1]


def track(cfg, video, sidecar, aspect, every=0.2, echo=print, overlay_fit=True) -> dict:
    """Face/saliency track of a 16:9 video for a crop of `aspect` (w, h). Returns
    {cw (fraction of width), shots: plan_crops output, duration, fps, src_w, src_h}."""
    vi = video_info(cfg, video)
    cw = min(1.0, vi["h"] * aspect[0] / aspect[1] / vi["w"])
    times, frames, _ = sample_frames(cfg, video, every=every, width=640, gray=True)
    faces = [detect_faces(f) for f in frames]
    faces = _drop_flicker(faces)
    sal = [_saliency_x(f) for f in frames]
    shots = shot_list(sidecar, vi["duration"], times, frames, overlay_fit=overlay_fit)
    people = [[] for _ in frames]
    for sh in shots:                                   # people only where no face was found (cheap enough)
        idx = [i for i, t in enumerate(times) if sh["t0"] - 1e-6 <= t < sh["t1"] - 1e-6]
        if sh["mode"] == "track" and idx and not any(faces[i] for i in idx):
            for i in idx:
                people[i] = detect_people(frames[i])
    planned = plan_crops(times, faces, sal, shots, cw, every=every, people=people)
    n_face = sum(1 for f in faces if f)
    _log(echo, f"export: tracked {len(frames)} frames ({n_face} with a face, "
               f"{sum(1 for p in people if p)} more with a person) over {len(shots)} shot(s)")
    return {"cw": cw, "shots": planned, "duration": vi["duration"], "fps": vi["fps"], "src_w": vi["w"],
            "src_h": vi["h"], "times": times, "faces": faces}


def _drop_flicker(faces) -> list:
    """Remove single-sample detections with no similar box in the neighbouring samples (cascade noise),
    unless the face is big (big faces are rarely false)."""
    out = []
    for i, fs in enumerate(faces):
        keep = []
        for f in fs:
            if f[3] > 0.25:
                keep.append(f)
                continue
            cx = f[0] + f[2] / 2
            near = False
            for j in (i - 1, i + 1):
                if 0 <= j < len(faces):
                    near |= any(abs(g[0] + g[2] / 2 - cx) < 0.08 and abs(g[3] - f[3]) < 0.5 * f[3] for g in faces[j])
            if near:
                keep.append(f)
        out.append(keep)
    return out


# --------------------------------------------------------------------------------------------- captions
def caption_cues(cfg, con, sidecar) -> list:
    """[(t0, t1, text)] in timeline seconds: the render's own caption cues, or (captions off) the cues
    resolved from the transcripts for the plan the render came from."""
    cues = [(float(c["t0"]), float(c["t1"]), c["text"]) for s in (sidecar or {}).get("segments", [])
            for c in s.get("captions") or []]
    if cues or not sidecar or con is None:
        return sorted(cues)
    try:
        from . import render
        plan = json.loads(sidecar_plan(cfg, sidecar).read_text())
        plan.setdefault("style", {}).setdefault("captions", {})["enabled"] = True
        res = render.resolve_timeline(cfg, con, plan, preview=bool(sidecar.get("preview")))
        return sorted((float(c["t0"]), float(c["t1"]), c["text"]) for p in res["pieces"] for c in p.get("captions", []))
    except Exception:  # noqa: BLE001 - no plan / transcripts: no captions file
        return []


def _ts(t, sep=","):
    t = max(0.0, t)
    h, r = divmod(int(round(t * 1000)), 3600000)
    m, r = divmod(r, 60000)
    s, ms = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_srt(cues, path):
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        lines += [str(i), f"{_ts(a)} --> {_ts(b)}", text, ""]
    Path(path).write_text("\n".join(lines))
    return path


def write_vtt(cues, path):
    lines = ["WEBVTT", ""]
    for a, b, text in cues:
        lines += [f"{_ts(a, '.')} --> {_ts(b, '.')}", text, ""]
    Path(path).write_text("\n".join(lines))
    return path


def chapters(sidecar) -> tuple:
    """(text, warnings) YouTube chapter lines from the sections. YouTube wants the first at 0:00, at least
    three chapters, each at least 10 s long; the warnings say when that is not met."""
    secs = [s for s in (sidecar or {}).get("sections", []) if s.get("end", 0) > s.get("start", 0)]
    warn = []
    lines = []
    for i, s in enumerate(secs):
        t = 0.0 if i == 0 else float(s["start"])
        m, sec = divmod(int(t), 60)
        h, m = divmod(m, 60)
        stamp = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        lines.append(f"{stamp} {str(s.get('name') or f'Part {i + 1}').strip().capitalize()}")
        if s["end"] - s["start"] < 10:
            warn.append(f"chapter '{s.get('name')}' is {s['end'] - s['start']:.1f}s (YouTube needs 10 s)")
    if len(secs) < 3:
        warn.append(f"{len(secs)} chapter(s): YouTube shows chapters only from 3")
    return "\n".join(lines) + ("\n" if lines else ""), warn


def _font(sidecar, size):
    from PIL import ImageFont
    try:
        from . import captions as C
        fp = C.find_font("")
        return C.load_font(fp, size)
    except Exception:  # noqa: BLE001 - captions.py API is not ours: fall back to a plain face
        pass
    f = (sidecar or {}).get("font")
    if f and os.path.exists(f):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    from .ingest import load_font
    return load_font(size)


def _wrap(draw, text, font, max_w, max_lines=2):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return lines


def _draw_text(d, xy, text, font, anchor_w=None, align="center", fill=(255, 255, 255, 255), size=40):
    x, y = xy
    tw = d.textlength(text, font=font)
    if align == "center":
        x = x - tw / 2
    d.text((x, y), text, font=font, fill=fill, stroke_width=max(2, size // 18), stroke_fill=(0, 0, 0, 170))
    return tw


def _graphic(items, W, H, safe, sidecar, fonts):
    """One RGBA frame with the active captions and overlays laid out for this frame shape."""
    from PIL import Image, ImageDraw
    top, bottom, left, right = safe
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0, x1 = W * left + W * 0.04, W * (1 - right) - W * 0.04
    cx = (x0 + x1) / 2
    cap_top = H * (1 - bottom)
    for it in items:
        if it["kind"] != "caption":
            continue
        size = fonts["cap"][1]
        lines = _wrap(d, it["text"], fonts["cap"][0], x1 - x0)
        lh = int(size * 1.22)
        y = int(H * (1 - bottom) - len(lines) * lh - size * 0.4)
        cap_top = min(cap_top, y)
        for k, ln in enumerate(lines):
            _draw_text(d, (cx, y + k * lh), ln, fonts["cap"][0], size=size)
    for it in items:
        k = it["kind"]
        if k == "caption":
            continue
        text = str(it.get("text") or "")
        if k == "title":
            f, size = fonts["title"]
            lines = _wrap(d, text, f, x1 - x0, max_lines=3)
            y = int(H * 0.42 - len(lines) * size * 0.6)
            for j, ln in enumerate(lines):
                _draw_text(d, (cx, y + j * int(size * 1.15)), ln, f, size=size)
        elif k == "lower_third":
            f, size = fonts["lt"]
            lines = _wrap(d, text, f, x1 - x0, max_lines=2)
            y = int(cap_top - size * 1.8 - len(lines) * size * 1.2)
            for j, ln in enumerate(lines):
                _draw_text(d, (x0, y + j * int(size * 1.2)), ln, f, align="left", size=size)
        else:                                            # label / callout: small, under the top safe area
            f, size = fonts["label"] if k == "label" else fonts["lt"]
            y = int(H * top + size * 0.6)
            pos = str(it.get("position") or "")
            if pos.endswith("right"):
                tw = d.textlength(text, font=f)
                _draw_text(d, (x1 - tw, y), text, f, align="left", size=size)
            else:
                _draw_text(d, (x0, y), text, f, align="left", size=size)
    return im


def caption_track(cues, W, H, safe, workdir, sidecar=None, size=None, overlays=()) -> Path | None:
    """A concat list of full-frame RGBA PNGs (blank where nothing shows) for one overlay input: captions
    centred just above the bottom safe area, overlay graphics (titles, lower thirds, labels, callouts)
    re-laid out for this frame shape inside the safe area."""
    from PIL import Image
    items = [{"kind": "caption", "t0": float(a), "t1": float(b), "text": t} for a, b, t in (cues or [])]
    items += [{"kind": o.get("kind") or "label", "t0": float(o["t0"]), "t1": float(o["t1"]), "text": o.get("text"),
               "position": o.get("position")} for o in (overlays or [])]
    items = [i for i in items if i["t1"] > i["t0"] + 1e-3]
    if not items:
        return None
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    size = size or max(24, int(round(W * 0.05)))
    fonts = {"cap": (_font(sidecar, size), size), "title": (_font(sidecar, int(W * 0.085)), int(W * 0.085)),
             "lt": (_font(sidecar, int(W * 0.05)), int(W * 0.05)), "label": (_font(sidecar, int(W * 0.036)), int(W * 0.036))}
    blank = workdir / "blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
    bounds = sorted({0.0} | {i["t0"] for i in items} | {i["t1"] for i in items})
    entries, cache = [], {}
    for a, b in zip(bounds, bounds[1:]):
        if b - a < 1e-3:
            continue
        mid = (a + b) / 2
        act = [i for i in items if i["t0"] <= mid < i["t1"]]
        if not act:
            entries.append((blank, b - a))
            continue
        key = tuple((i["kind"], i["text"], i.get("position")) for i in act)
        if key not in cache:
            p = workdir / f"g_{len(cache):04d}.png"
            _graphic(act, W, H, safe, sidecar, fonts).save(p)
            cache[key] = p
        if entries and entries[-1][0] == cache[key]:
            entries[-1] = (cache[key], entries[-1][1] + b - a)
        else:
            entries.append((cache[key], b - a))
    entries.append((blank, 1.0))
    lst = workdir / "graphics.ffconcat"
    body = ["ffconcat version 1.0"]
    for p, d in entries:
        body += [f"file '{p.name}'", f"duration {max(0.001, d):.4f}"]
    body.append(f"file '{blank.name}'")
    lst.write_text("\n".join(body) + "\n")
    return lst


# --------------------------------------------------------------------------------------------- loudness
def measure_loudness(cfg, path, I=-14.0, TP=-1.0, LRA=11.0) -> dict | None:
    """First loudnorm pass: measured_I/TP/LRA/thresh + offset (None without audio)."""
    cmd = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af",
           f"loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json", "-f", "null", "-"]
    r = media.run(cmd, check=False, timeout=3600)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "", re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    try:
        if not math.isfinite(float(d["input_i"])):
            return None
    except (TypeError, ValueError):
        return None
    return d


def loudnorm_filter(meas, I, TP, LRA=11.0) -> str:
    if not meas:
        return "anull"
    return (f"loudnorm=I={I}:TP={TP}:LRA={LRA}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
            f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:"
            f"linear=true:print_format=none,aresample=48000")


def ebur128(cfg, path) -> dict:
    r = media.run([cfg.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
                   "-f", "null", "-"], check=False, timeout=3600)
    tail = (r.stderr or "")[-2500:]
    mi = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", tail)
    mp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    f = lambda m: None if not m or "inf" in m.group(1) else float(m.group(1))   # noqa: E731
    return {"I": f(mi), "TP": f(mp)}


# --------------------------------------------------------------------------------------------- encode
def _fps_for(preset, src_fps, force=None):
    if force:
        return float(force)
    if preset.get("fps"):
        return float(preset["fps"])
    # platforms take 24-60 fps; conforming 25 -> 30 duplicates every 5th frame (visible judder), so keep it
    if any(abs(src_fps - f) < 0.02 for f in ACCEPTED_FPS):
        return src_fps
    return 30.0 if src_fps < 45 else 60.0


def _venc(preset, fps):
    g = int(round(fps * 2))
    v = ["-c:v", "libx264", "-preset", "medium", "-profile:v", preset.get("profile", "high"), "-pix_fmt", "yuv420p",
         "-g", str(g), "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    if preset.get("crf") is not None:
        v += ["-crf", str(preset["crf"])]
    else:
        b = preset["vbitrate"]
        v += ["-b:v", b, "-maxrate", b, "-bufsize", f"{int(float(b.rstrip('Mk')) * 2)}{b[-1]}"]
    return v


def encode_preset(cfg, src, out, preset, track_info=None, cues=None, fps=None, trim=None, loud=None,
                  workdir=None, sidecar=None, overlays=(), echo=print) -> dict:
    """Encode one preset from `src`. Same aspect: scale. Other aspects: tracked crop (sendcmd-driven
    crop x per frame) with letterboxed 'fit' shots, then the caption track. Audio: two-pass loudnorm."""
    vi = video_info(cfg, src)
    W, H = preset["width"], preset["height"]
    out_fps = _fps_for(preset, vi["fps"], fps)
    dur = min(vi["duration"], trim) if trim else vi["duration"]
    workdir = Path(workdir or Path(out).parent / f".{Path(out).stem}")
    workdir.mkdir(parents=True, exist_ok=True)
    same_aspect = abs(vi["w"] / vi["h"] - W / H) < 0.01
    inputs = ["-i", str(src)]
    graph = []
    warnings = []
    if same_aspect:
        graph.append(f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1,fps={out_fps:g}[v]")
        if vi["h"] < H:
            warnings.append(f"{preset['label']}: upscaled from {vi['w']}x{vi['h']} (export a final render for full sharpness)")
    else:
        tr = track_info
        cw_px = max(2, int(round(tr["cw"] * vi["w"] / 2)) * 2)
        ch_px = vi["h"]
        if ch_px < H:
            warnings.append(f"{preset['label']}: the crop is {cw_px}x{ch_px}, upscaled to {W}x{H} "
                            "(export from a final or 4K render for a sharp vertical)")
        cmds = workdir / "crop.cmd"
        fits = [s for s in tr["shots"] if s["how"] == "fit"]
        with open(cmds, "w") as f:
            n = int(math.ceil(dur * vi["fps"])) + 1
            shots = tr["shots"]
            j = 0
            last = None
            for i in range(n):
                t = i / vi["fps"]
                while j + 1 < len(shots) and t >= shots[j]["t1"] - 1e-6:
                    j += 1
                cx = crop_x_at(shots[j], t) if shots else 0.5
                x = int(round(cx * vi["w"] - cw_px / 2))
                x = max(0, min(vi["w"] - cw_px, x))
                if x != last:
                    f.write(f"{t:.4f} crop@track x {x};\n")
                    last = x
        x0 = max(0, min(vi["w"] - cw_px, int(round((crop_x_at(tr["shots"][0], 0) if tr["shots"] else 0.5) * vi["w"] - cw_px / 2))))
        graph.append("[0:v]split=2[a][b]")
        graph.append(f"[a]sendcmd=f='{cmds}',crop@track=w={cw_px}:h={ch_px}:x={x0}:y=0,"
                     f"scale={W}:{H}:flags=lanczos,setsar=1[tracked]")
        if fits:
            en = "+".join(f"between(t,{s['t0']:.4f},{s['t1'] - 1e-3:.4f})" for s in fits)
            fg_h = int(round(W * vi["h"] / vi["w"] / 2)) * 2
            graph.append("[b]split=2[b1][b2]")
            graph.append(f"[b1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                         f"gblur=sigma=40,eq=brightness=-0.08:saturation=0.85[bg]")
            graph.append(f"[b2]scale={W}:{fg_h}:flags=lanczos[fg]")
            graph.append("[bg][fg]overlay=0:(H-h)/2,setsar=1[fit]")
            graph.append(f"[tracked][fit]overlay=0:0:enable='{en}'[pic]")
        else:
            graph.append("[b]nullsink")
            graph.append("[tracked]null[pic]")
        last_label = "pic"
        if cues or overlays:
            lst = caption_track(cues, W, H, preset["safe"], workdir / "captions", sidecar=sidecar, overlays=overlays)
            if lst:
                inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
                graph.append("[1:v]format=rgba[cap]")
                graph.append(f"[{last_label}][cap]overlay=0:0:eof_action=pass:format=auto[capd]")
                last_label = "capd"
        graph.append(f"[{last_label}]fps={out_fps:g},format=yuv420p[v]")
    af = None
    if vi["has_audio"]:
        af = loudnorm_filter(loud, preset["I"], preset["TP"])
    tmp = Path(out).with_name(f".{Path(out).stem}.tmp.mp4")
    args = [*inputs, "-filter_complex", ";".join(graph), "-map", "[v]"]
    if vi["has_audio"]:
        args += ["-map", "0:a:0", "-af", af, "-c:a", "aac", "-b:a", preset["abitrate"], "-ar", "48000"]
    args += _venc(preset, out_fps)
    if trim:
        args += ["-t", f"{dur:.3f}"]
    args += ["-movflags", "+faststart", str(tmp)]
    gov = governor.for_cfg(cfg)
    t0 = time.time()
    with gov.slot("heavy", label=f"export {Path(out).name}"):
        gov.wait_for_disk(Path(out).parent)
        media.ffmpeg(cfg, args, log=cfg.p("renders", "ffmpeg.log"), timeout=max(1800, dur * 20))
    os.replace(tmp, out)
    got = video_info(cfg, out)
    meas = ebur128(cfg, out) if got["has_audio"] else {"I": None, "TP": None}
    res = {"preset": preset["label"], "file": str(out), "width": got["w"], "height": got["h"],
           "fps": round(got["fps"], 3), "duration": round(got["duration"], 3), "loudness": meas,
           "target": {"I": preset["I"], "TP": preset["TP"]}, "seconds": round(time.time() - t0, 1),
           "size_mb": round(os.path.getsize(out) / 1e6, 2), "warnings": warnings}
    if not same_aspect:
        res["reframe"] = [{"t0": round(s["t0"], 3), "t1": round(s["t1"], 3), "how": s["how"],
                           "face_samples": s.get("face_samples", 0), "samples": s.get("samples", 0)}
                          for s in track_info["shots"]]
    shutil.rmtree(workdir, ignore_errors=True)
    return res


# --------------------------------------------------------------------------------------------- clean feed
def clean_feed(cfg, video, sidecar, echo=print) -> Path | None:
    """The same edit without burned-in captions and overlay graphics (rendered once, cached): what reframes
    and thumbnails use. Title cards stay (they are letterboxed whole)."""
    plan_path = sidecar_plan(cfg, sidecar)
    if plan_path is None:
        _log(echo, f"export: plan {sidecar.get('plan')} not found; reframing the captioned render (captions may be cut)")
        return None
    plan = json.loads(plan_path.read_text())
    plan.setdefault("style", {}).setdefault("captions", {})["enabled"] = False
    for sec in plan.get("sections", []):               # graphics are re-laid out for each frame shape
        for seg in sec.get("segments", []):
            if seg.get("overlays"):
                seg["overlays"] = []
    stem = Path(video).stem
    tmp_plan = cfg.p("plans", f".{stem}.clean.json")
    body = json.dumps(plan, indent=1)
    if not tmp_plan.exists() or tmp_plan.read_text() != body:
        tmp_plan.write_text(body)
    out = out_dir(cfg, video) / f"{stem}_clean.mp4"
    if out.exists() and out.stat().st_mtime >= max(tmp_plan.stat().st_mtime, Path(video).stat().st_mtime):
        return out
    args = ["render", tmp_plan, "--out", out] + (["--preview"] if sidecar.get("preview") else []) \
        + (["--only", sidecar["only"]] if sidecar.get("only") else [])
    _log(echo, "export: rendering a caption-free clean feed for the reframes (cached pieces are reused)")
    _cli(cfg, *args)
    vi, vo = video_info(cfg, video), video_info(cfg, out)
    if abs(vi["duration"] - vo["duration"]) > 2.0 / max(vi["fps"], 1):
        _log(echo, f"export: clean feed is {vo['duration']:.2f}s vs {vi['duration']:.2f}s; not using it")
        return None
    return out


# --------------------------------------------------------------------------------------------- thumbnails
def _colourful(rgb) -> float:
    r, g, b = (rgb[..., i].astype(np.float32) for i in range(3))
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def score_frame(rgb, faces=None) -> dict:
    """Thumbnail score parts for one RGB frame (0-1 each) and the total.
    sharp: Laplacian variance (log-scaled) · face: a face 20-45 % of the frame height · eyes: two open eyes
    (Haar eye cascade, which rarely fires on closed eyes) · smile · exposure: mid-tone mean, few clipped or
    crushed pixels · thirds: subject near a rule-of-thirds line · colour: colourfulness."""
    import cv2
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    lap = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    sharp = min(1.0, max(0.0, (math.log10(lap + 1) - 1.0) / 2.0))        # 10 -> 0, 1000 -> 1
    faces = detect_faces(gray) if faces is None else faces
    face = eyes = smile = 0.0
    sub_x, sub_y = None, None
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        face = 1.0 if 0.2 <= fh <= 0.45 else max(0.0, 1 - min(abs(fh - 0.2), abs(fh - 0.45)) / 0.15)
        face = max(face, 0.35)
        sub_x, sub_y = fx + fw / 2, fy + fh * 0.4
        x0, y0, x1, y1 = int(fx * w), int(fy * h), int((fx + fw) * w), int((fy + fh) * h)
        roi = gray[y0:y1, x0:x1]
        if roi.size and roi.shape[0] >= 24:
            ec = _cascade("haarcascade_eye.xml")
            if ec is not None:
                up = roi[: roi.shape[0] // 2 + 2]
                n = len(ec.detectMultiScale(up, scaleFactor=1.1, minNeighbors=6, minSize=(max(6, roi.shape[1] // 10),) * 2))
                eyes = 1.0 if n >= 2 else 0.5 if n == 1 else 0.0
            sc = _cascade("haarcascade_smile.xml")
            if sc is not None:
                low = roi[roi.shape[0] // 2:]
                smile = 1.0 if len(sc.detectMultiScale(low, scaleFactor=1.6, minNeighbors=20,
                                                       minSize=(max(8, roi.shape[1] // 4), max(6, roi.shape[0] // 10)))) else 0.0
    mean = float(gray.mean()) / 255
    clip_hi = float((gray >= 250).mean())
    clip_lo = float((gray <= 5).mean())
    exposure = max(0.0, 1 - abs(mean - 0.5) / 0.35) * (1 - min(1.0, clip_hi * 8)) * (1 - min(1.0, max(0.0, clip_lo - 0.05) * 4))
    if sub_x is None:
        sub_x, _ = _saliency_x(gray)
        sub_y = 0.45
    d = min(math.hypot(sub_x - tx, sub_y - ty) for tx in (1 / 3, 2 / 3, 0.5) for ty in (1 / 3, 0.45))
    thirds = max(0.0, 1 - d / 0.3)
    colour = min(1.0, _colourful(rgb) / 80)
    total = (0.25 * sharp + 0.25 * face + 0.12 * eyes + 0.08 * smile + 0.15 * exposure + 0.08 * thirds
             + 0.07 * colour)
    if not faces:
        total *= 0.8
    return {"total": round(total, 4), "sharp": round(sharp, 3), "face": round(face, 3), "eyes": eyes, "smile": smile,
            "exposure": round(exposure, 3), "thirds": round(thirds, 3), "colour": round(colour, 3),
            "faces": len(faces)}


def _blocked_spans(sidecar, margin=0.25) -> list:
    """Timeline spans thumbnails avoid: title/black segments and the frames right around cuts."""
    spans = []
    for s in (sidecar or {}).get("segments", []):
        if s.get("kind") in ("title", "black"):
            spans.append((s["start"], s["end"]))
        spans.append((s["start"] - margin, s["start"] + margin))
    return spans


def pick_thumbnails(scored, n=6, min_gap=2.0, per_shot=2, shots=None) -> list:
    """Best `n` of [(t, score_dict)] with at least `min_gap` s between picks and at most `per_shot` per shot."""
    picked, used = [], {}
    for t, sc in sorted(scored, key=lambda x: -x[1]["total"]):
        if any(abs(t - p[0]) < min_gap for p in picked):
            continue
        k = None
        if shots:
            k = next((i for i, s in enumerate(shots) if s["t0"] <= t < s["t1"]), None)
            if k is not None and used.get(k, 0) >= per_shot:
                continue
        picked.append((t, sc))
        if k is not None:
            used[k] = used.get(k, 0) + 1
        if len(picked) >= n:
            break
    return picked


def _master_frame(cfg, con, sidecar, t, out) -> bool:
    """Full-resolution frame from the original master behind timeline time t, with the project LUT (log
    footage) through the same BT.709 pipe as proxies/renders. False when it can't be located."""
    from .ingest import PROXY_LUT_TAIL
    if con is None:
        return False
    seg = next((s for s in sidecar.get("segments", []) if s["start"] <= t < s["end"] and s.get("clip")), None)
    if not seg or seg.get("kind") not in ("clip", "still"):
        return False
    clip, src_t = seg["clip"], None
    for c in seg.get("cutaways") or []:
        if c["t0"] <= t < c["t1"]:
            clip, src_t = c["clip"], float(c["in"]) + (t - c["t0"])
    if src_t is None:
        src_t = float(seg["in"]) + (t - seg["start"]) * float(seg.get("speed") or 1.0)
    row = con.execute("SELECT * FROM clips WHERE id=?", (clip,)).fetchone()
    if not row or not os.path.exists(row["path"]):
        return False
    crop = seg.get("crop") if clip == seg["clip"] else None
    vf = []
    if crop and crop != [0.0, 0.0, 1.0, 1.0]:
        x, y, w, h = crop
        vf.append(f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}")
    if cfg.lut and row["is_log"] and os.path.exists(cfg.lut):
        vf += ["scale=iw:ih:in_color_matrix=bt709:out_range=full", "format=gbrpf32le",
               f"lut3d=file='{cfg.lut}':interp=tetrahedral", "format=gbrp16le", PROXY_LUT_TAIL]
    media.ffmpeg(cfg, ["-ss", f"{max(0.0, src_t):.3f}", "-i", row["path"], "-frames:v", "1",
                       *(["-vf", ",".join(vf)] if vf else []), "-q:v", "2", "-update", "1", str(out)])
    return Path(out).exists()


def thumbnails(cfg, con, video, sidecar, dest, n=6, every=0.5, source="auto", echo=print) -> list:
    """Score frames every `every` s, pick `n` diverse winners, write thumb_0N.jpg (full resolution: from the
    masters when the sidecar can locate them, else from `video`) and sheet.jpg."""
    from PIL import Image, ImageDraw
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    times, frames, _ = sample_frames(cfg, video, every=every, width=960, gray=False)
    blocked = _blocked_spans(sidecar)
    vi = video_info(cfg, video)
    shots = shot_list(sidecar, vi["duration"]) if sidecar else None
    scored = []
    for t, f in zip(times, frames):
        if any(a <= t <= b for a, b in blocked) or t > vi["duration"] - 0.3:
            continue
        scored.append((t, score_frame(f)))
    picks = pick_thumbnails(scored, n=n, shots=shots)
    out = []
    for i, (t, sc) in enumerate(picks, 1):
        p = dest / f"thumb_{i:02d}.jpg"
        how = "render"
        if source in ("auto", "master") and sidecar:
            try:
                if _master_frame(cfg, con, sidecar, t, p):
                    how = "master"
            except Exception:  # noqa: BLE001 - fall back to the render frame
                how = "render"
        if how == "render":
            media.ffmpeg(cfg, ["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", "-update", "1", str(p)])
        with Image.open(p) as im:
            size = im.size
        out.append({"file": str(p), "t": t, "from": how, "size": list(size), **sc})
    if out:
        tw, th = 480, 270
        cols = 3
        rows = math.ceil(len(out) / cols)
        sheet = Image.new("RGB", (cols * tw, rows * (th + 34)), (18, 18, 18))
        d = ImageDraw.Draw(sheet)
        from .ingest import load_font
        f = load_font(16)
        for k, o in enumerate(out):
            with Image.open(o["file"]) as im:
                im = im.convert("RGB")
                im.thumbnail((tw, th))
                x, y = (k % cols) * tw, (k // cols) * (th + 34)
                sheet.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
            d.text((x + 6, y + th + 6), f"#{k + 1} {o['t']:.1f}s  score {o['total']:.2f}  sharp {o['sharp']:.2f}  "
                                        f"face {o['face']:.1f} eyes {o['eyes']:.1f}", fill=(230, 230, 230), font=f)
        sheet.save(dest / "sheet.jpg", quality=88)
    _log(echo, f"export: {len(out)} thumbnail candidate(s) -> {dest}")
    return out


# --------------------------------------------------------------------------------------------- cutdowns
STAKES = re.compile(r"\b(\d[\d,.]*|first|last|best|worst|biggest|insane|crazy|never|ever|every|million|"
                    r"thousand|hundred|money|free|secret|actually|finally|literally|love|hate|why|how)\b", re.I)


def _line_units(cfg, con, plan) -> list:
    """Sentences of every talk segment as candidate cutdown lines: {sec, seg, clip, in, out, text, score}."""
    from . import asr
    out = []
    order = 0
    for si, sec in enumerate(plan.get("sections", [])):
        for gi, seg in enumerate(sec.get("segments", [])):
            if seg.get("type") != "talk":
                continue
            try:
                row = db.get_clip(con, seg["clip"])
            except KeyError:
                continue
            a, b = float(seg.get("in", 0)), float(seg.get("out") or row["duration"] or 0)
            ws = [w for w in asr.words(cfg, row["id"]) if w[0] >= a - 0.05 and w[1] <= b + 0.05]
            for t0, t1, text in asr.sentences(ws):
                dur = t1 - t0
                if dur < 0.6:
                    continue
                stakes = len(STAKES.findall(text))
                hl = con.execute("SELECT MAX(score) FROM highlights WHERE clip_id=? AND t1>? AND t0<?",
                                 (row["id"], t0, t1)).fetchone()[0] if _has_table(con, "highlights") else None
                length_fit = 1.0 if 1.2 <= dur <= 8 else 0.5
                words = len(text.split())
                score = 0.35 * min(1.0, stakes / 2) + 0.3 * float(hl or 0) + 0.2 * length_fit + 0.15 * min(1.0, words / 10)
                if re.search(r"\b(cut|rolling|take two|again)\b", text.lower()) and words <= 4:
                    continue                                   # director cues
                out.append({"sec": si, "seg": gi, "clip": row["id"], "in": round(max(a, t0 - 0.1), 3),
                            "out": round(min(b, t1 + 0.15), 3), "text": text, "score": round(score, 3),
                            "order": order, "dur": dur, "spec": seg})
                order += 1
    return out


def _has_table(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _resolve(cfg, con, plan) -> dict:
    from . import render
    return render.resolve_timeline(cfg, con, plan, preview=True)


def _plan_duration(cfg, con, plan) -> float:
    return float(_resolve(cfg, con, plan)["duration"])


def _cutdown_cold_open(cfg, con, plan, echo=None) -> dict | None:
    """The plan's cold_open made independent of the full edit's sections, for a cutdown whose sections are
    rebuilt (the hook + "story"): every `{"from": "<section>/<seg>"}` line is resolved against the FULL plan
    into the same concrete {clip, in, out, text} the full edit uses, and `shots: "auto"` is frozen to the full
    edit's b-roll pool. A line that cannot be resolved (or is not a 0.3-6 s line) is dropped; with no line left
    the cold open is dropped. Returns the rewritten cold_open, or None."""
    from . import plan as P, render
    co = json.loads(json.dumps(plan.get("cold_open") or {}))
    if not co:
        return None
    try:
        full = P.normalize(plan, cfg)
        ctx = render._ResolveCtx(cfg, con, full, True)
    except Exception as e:  # noqa: BLE001
        _log(echo, f"cutdown: dropping the cold open (the full plan does not resolve: {e})")
        return None
    raw = co.get("lines") or []
    lines, resolved = [], []
    for k, ln in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(ln, str):
            ln = {"from": ln}
        if not isinstance(ln, dict):
            continue
        try:
            row, a, b, text = render._resolve_line(ctx, full, ln)
        except Exception as e:  # noqa: BLE001
            _log(echo, f"cutdown: cold open line {k} dropped ({e})")
            continue
        if ln.get("from") is None:
            lines.append(ln)
        elif 0.3 <= b - a <= 6:
            lines.append({"clip": row["id"], "in": round(a, 3), "out": round(b, 3),
                          "text": text or ln.get("text", "")})
        else:
            _log(echo, f"cutdown: cold open line {k} dropped ({ln['from']!r} resolves to {b - a:.1f}s, "
                       "not a teaser line)")
            continue
        resolved.append((row, a, b, text))
    if raw and not lines:
        _log(echo, "cutdown: dropping the cold open (none of its lines survive the cut)")
        return None
    co["lines"] = lines
    if not (co.get("music") or {}).get("song"):      # the teaser's music falls back to the FULL edit's first song
        src = next((s.get("music") for s in full.get("sections", []) if (s.get("music") or {}).get("song")), None)
        if src:
            co["music"] = {**(co.get("music") or {}), "song": src["song"], "offset": float(src.get("offset", 0.0))}
    if co.get("shots", "auto") == "auto":
        try:
            pco = {**co, "end_shot": {"auto": True}} if co.get("end_shot") == "auto" else co
            pool = render._ShotPool(ctx, full, pco, resolved)
            co["shots"] = [{"clip": p["row"]["id"], "in": round(float(p["next"]), 3)} for p in pool.pool]
        except Exception as e:  # noqa: BLE001 - keep "auto": it may still find b-roll in the cut's own sections
            _log(echo, f"cutdown: cold open shots stay auto ({e})")
    return co


def _trim_seg(seg, budget):
    """A copy of a picture segment shortened toward `budget` seconds (montage: fewer shots)."""
    seg = json.loads(json.dumps(seg))
    t = seg.get("type")
    if t == "montage":
        per = max(0.4, float(seg.get("max_shot", 1.3) or 1.3))
        seg["shots"] = seg.get("shots", [])[:max(2, int(math.ceil(budget / per)))]
    elif t == "title":
        seg["dur"] = min(float(seg.get("dur", 2.0)), 1.8)
    elif t == "broll" and seg.get("out") is not None:
        seg["out"] = round(min(float(seg["out"]), float(seg.get("in", 0)) + max(1.5, budget)), 3)
    elif t == "matchcut":
        seg["pre"], seg["post"] = min(float(seg.get("pre", 0.8)), 0.8), min(float(seg.get("post", 1.4)), 1.2)
    return seg


def build_cutdown(cfg, con, plan_path, length, echo=print) -> Path:
    """Write plans/<slug>_cut<len>.json: hook + strongest lines in story order + payoff, within 10 % of
    `length` seconds (checked with the renderer's timeline resolution). Edits without dialogue keep their
    picture segments in order, shortened, until the length is reached."""
    from . import plan as P
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text())
    L = float(length)
    secs = plan.get("sections", [])
    if not secs:
        raise ValueError("plan has no sections")
    lines = _line_units(cfg, con, plan)
    payoff = lines[-1] if lines else None
    first = secs[0]
    meta = {k: v for k, v in first.items() if k != "segments"}
    hook_budget = max(3.0, min(0.3 * L, 12.0))
    co = _cutdown_cold_open(cfg, con, plan, echo) if plan.get("cold_open") else None

    def make(sections, note, cold_open=True):
        p = {k: v for k, v in plan.items() if k not in ("sections", "cutdown", "note", "cold_open")}
        if co and cold_open:
            p["cold_open"] = co
        p.update(title=f"{plan.get('title', 'untitled')} ({int(L)} s cut)", version=1, sections=sections, note=note)
        return p

    if co:                                          # the teaser must build on its own and leave room for the cut
        probe = [{**meta, "segments": [{"type": "black", "dur": 0.5}]}]
        tl = _resolve(cfg, con, make(probe, ""))
        bad = [w for w in tl["warnings"] if str(w).startswith("cold_open")]
        co_s = float(tl["duration"]) - _plan_duration(cfg, con, make(probe, "", cold_open=False))
        if bad or co_s > 0.5 * L:
            why = bad[0] if bad else f"{co_s:.1f}s is over half of {L:.0f}s"
            _log(echo, f"cutdown: dropping the cold open ({why})")
            co = None

    # the hook: the opening of the first section up to its first talk segment (the cold open)
    hook_segs = []
    for seg in first.get("segments", []):
        if seg.get("type") == "talk":
            break
        if seg.get("type") == "black":
            continue
        hook_segs.append(_trim_seg(seg, hook_budget))
        if _plan_duration(cfg, con, make([{**meta, "segments": hook_segs}], "")) >= hook_budget:
            break
    hook_line = None
    if not hook_segs and lines:                      # no cold open: the strongest line opens the cut
        hook_line = max(lines[:-1] or lines, key=lambda u: u["score"])
        hook_segs = [_talk_from(hook_line)]
    if not lines:                                     # no dialogue: picture segments in order, shortened
        segs = list(hook_segs)
        for sec in secs[1:] if hook_segs else secs:
            for seg in sec.get("segments", []):
                if seg.get("type") == "black":
                    continue
                segs.append(_trim_seg(seg, max(2.0, L / 6)))
                if _plan_duration(cfg, con, make([{**meta, "segments": segs}], "")) >= L * 0.95:
                    break
            else:
                continue
            break
        segs.append({"type": "black", "dur": 0.5})
        cur = make([{**meta, "segments": segs}], f"cutdown of {plan_path.name} to {int(L)} s (no dialogue: "
                                                 "opening picture segments, shortened). Refine freely.")
        dur = _plan_duration(cfg, con, cur)
        while dur > L * 1.1 and len(segs) > 2:
            segs.pop(-2)
            dur = _plan_duration(cfg, con, cur)
        picked = []
    else:
        picked = []
        pool = sorted([u for u in lines if u is not payoff and u is not hook_line], key=lambda u: -u["score"])

        def build(units):
            body = [_talk_from(u) for u in sorted(units, key=lambda u: u["order"])]
            if payoff is not None and payoff is not hook_line:
                body.append(_talk_from(payoff))
            body.append({"type": "black", "dur": 0.5})
            return make([{**meta, "name": first.get("name", "hook"), "segments": hook_segs},
                          {"name": "story", "segments": body}],
                         f"cutdown of {plan_path.name} to {int(L)} s: hook from '{first.get('name', 'the opening')}', "
                         f"{len(units)} strongest line(s) in story order, ending on the payoff line. Refine freely.")
        cur = build(picked)
        dur = _plan_duration(cfg, con, cur)
        for u in pool:                                 # add the strongest lines that still fit
            if dur >= L * 0.97:
                break
            trial = build(picked + [u])
            d = _plan_duration(cfg, con, trial)
            if d <= L * 1.05:
                picked.append(u)
                cur, dur = trial, d
        while dur > L * 1.1 and picked:               # too long: drop the weakest
            picked.remove(min(picked, key=lambda u: u["score"]))
            cur = build(picked)
            dur = _plan_duration(cfg, con, cur)
        while dur > L * 1.1 and len(hook_segs) > 1:   # still too long: shorten the hook
            hook_segs.pop()
            cur = build(picked)
            dur = _plan_duration(cfg, con, cur)
    if dur < L * 0.9:
        _log(echo, f"cutdown: only {dur:.1f}s of material for a {L:.0f}s cut")
    errs = P.validate(P.normalize(cur, cfg), con)
    if errs:
        raise P.PlanError("cutdown plan has problems:\n  " + "\n  ".join(errs))
    slug = re.sub(r"_v\d+$", "", plan_path.stem)
    out = plan_path.with_name(f"{slug}_cut{int(L)}.json")
    cur["cutdown"] = {"from": plan_path.name, "length": L, "duration": round(dur, 2),
                      "lines": [{"clip": u["clip"], "in": u["in"], "out": u["out"], "text": u["text"],
                                 "score": u["score"]} for u in sorted(picked, key=lambda u: u["order"])],
                      "payoff": payoff["text"] if payoff else None}
    out.write_text(json.dumps(cur, indent=1))
    _log(echo, f"cutdown: {out.name} · {dur:.1f}s for {L:.0f}s · {len(picked)} line(s) + hook"
               + (" + payoff" if payoff else ""))
    return out


def _talk_from(u) -> dict:
    spec = u["spec"]
    seg = {"type": "talk", "clip": u["clip"], "in": u["in"], "out": u["out"], "note": u["text"][:120]}
    for k in ("crop", "captions", "tighten", "gap", "look", "zoom"):
        if k in spec:
            seg[k] = spec[k]
    return seg


# --------------------------------------------------------------------------------------------- driver
def export(cfg, con, target, presets=("youtube",), cutdowns=(), thumbs=False, captions=True, keep_burned=False,
           fps=None, trim=False, preview=None, max_dur=None, echo=print) -> dict:
    """Export a render (or a plan's newest render) to platform presets + captions/chapters (+ thumbnails),
    and optionally cutdowns. Returns the manifest (also written to export.json)."""
    target = Path(target)
    if not target.is_absolute() and not target.exists():
        target = Path(cfg.work) / target
    if target.suffix == ".json":
        video = find_render(cfg, target)
        if video is None:
            video = render_plan(cfg, target, preview=bool(preview), echo=echo)
    else:
        video = target
    if not video.exists():
        raise FileNotFoundError(f"no render {video}")
    names = preset_names(presets)
    manifest = {"source": str(video), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "outputs": [], "warnings": []}
    manifest.update(_export_one(cfg, con, video, names, thumbs, captions, keep_burned, fps, trim, max_dur, echo))
    plan_path = sidecar_plan(cfg, load_sidecar(video)) or (target if target.suffix == ".json" else None)
    for L in cutdowns or ():
        if not plan_path or not Path(plan_path).exists():
            manifest["warnings"].append(f"cutdown {L}s: the plan behind {video.name} was not found")
            continue
        cp = build_cutdown(cfg, con, plan_path, L, echo=echo)
        side = load_sidecar(video) or {}
        rv = render_plan(cfg, cp, preview=bool(side.get("preview")) if preview is None else bool(preview), echo=echo)
        sub = _export_one(cfg, con, rv, [n for n in names if n != "youtube_4k"] or ["reels"], False, captions,
                          keep_burned, fps, trim, max_dur, echo)
        manifest.setdefault("cutdowns", []).append({"length": L, "plan": str(cp), "render": str(rv), **sub})
    d = out_dir(cfg, video)
    (d / "export.json").write_text(json.dumps(manifest, indent=1))
    manifest["dir"] = str(d)
    return manifest


def _export_one(cfg, con, video, names, thumbs, captions, keep_burned, fps, trim, max_dur, echo) -> dict:
    side = load_sidecar(video)
    d = out_dir(cfg, video)
    stem = video.stem
    vi = video_info(cfg, video)
    res = {"outputs": [], "warnings": [], "dir": str(d)}
    cues = caption_cues(cfg, con, side) if captions else []
    if cues:
        res["srt"] = str(write_srt(cues, d / f"{stem}.srt"))
        res["vtt"] = str(write_vtt(cues, d / f"{stem}.vtt"))
    if side:
        txt, warn = chapters(side)
        if txt:
            (d / f"{stem}.chapters.txt").write_text(txt)
            res["chapters"] = str(d / f"{stem}.chapters.txt")
            res["warnings"] += warn
    burned = bool(side and any(s.get("captions") or s.get("overlays") for s in side.get("segments", [])))
    overlays = [o for s in (side or {}).get("segments", []) for o in s.get("overlays") or []]
    need_reframe = [n for n in names if abs(vi["w"] / max(vi["h"], 1) - PRESETS[n]["width"] / PRESETS[n]["height"]) > 0.01]
    picture = video
    if need_reframe and burned and not keep_burned:
        cf = clean_feed(cfg, video, side, echo=echo)
        if cf:
            picture = cf
        else:
            res["warnings"].append("reframes use the captioned render: burned captions may be cut at the sides")
    loud_cache, tracks = {}, {}
    for n in names:
        pr = dict(PRESETS[n])
        if max_dur:
            pr["max_dur"] = float(max_dur)
        if pr["max_dur"] and vi["duration"] > pr["max_dur"] + 0.05 and not trim:
            msg = (f"{n}: {vi['duration']:.1f}s is longer than {pr['max_dur']:.0f}s; skipped "
                   f"(use --cutdown {int(min(pr['max_dur'], 60))} or --trim)")
            res["warnings"].append(msg)
            _log(echo, f"export: {msg}")
            continue
        out = d / f"{stem}_{n}.mp4"
        reframe = n in need_reframe
        src = picture if reframe else video
        key = (str(src), pr["I"], pr["TP"])
        if key not in loud_cache:
            loud_cache[key] = measure_loudness(cfg, src, pr["I"], pr["TP"]) if vi["has_audio"] else None
        tr = None
        if reframe:
            ak = (pr["width"], pr["height"], str(src))
            if ak not in tracks:
                tracks[ak] = track(cfg, src, side, (pr["width"], pr["height"]), echo=echo,
                                   overlay_fit=picture == video)

            tr = tracks[ak]
        _log(echo, f"export: {n} -> {out.name}")
        clean = reframe and picture != video
        r = encode_preset(cfg, src, out, pr, track_info=tr, cues=cues if (clean and captions) else None,
                          fps=fps, trim=pr["max_dur"] if trim and pr["max_dur"] else None, loud=loud_cache[key],
                          sidecar=side, overlays=overlays if clean else (), echo=echo)
        r["name"] = n
        res["outputs"].append(r)
        res["warnings"] += r.get("warnings", [])
    if thumbs:
        res["thumbnails"] = thumbnails(cfg, con, picture, side, d / "thumbs", echo=echo)
        res["thumb_sheet"] = str(d / "thumbs" / "sheet.jpg")
    return res
