"""Vision: the detectors that look at pictures, and the AI "brain" pass that checks them.

Two layers. The fast detectors run locally (no cloud) and propose; the AI agent (Claude Code / Codex via
agent.py) looks at contact strips of every proposal and decides. Every AI claim cites frame numbers that
were on the images it saw, so each claim can be checked; claims that cite nothing it was shown are dropped.

Faces
    FaceDetector: OpenCV's YuNet DNN detector (`cv2.FaceDetectorYN`, model face_detection_yunet_2023mar.onnx
    from the opencv_zoo repo). Finds side-on, small and partly covered faces that the Haar cascade misses, with
    far fewer false positives in caption bands. The model (~230 KB) is downloaded once into a user cache dir
    and checked against a pinned SHA-256; it is never in git. Offline or on a failed download the old Haar
    cascade is used, and `backend` says which one ran.
        CUTROOM_MODEL_DIR   cache dir (default ~/Library/Caches/cutroom/models, or $XDG_CACHE_HOME/cutroom/models)
        CUTROOM_OFFLINE=1   never download (YuNet only if the model is already cached)
        CUTROOM_FACES=haar  force the Haar cascade

Shots
    ShotFeatures (fed one frame at a time by reference.scan_video) + detect_shots(): a combined detector.
    A boundary needs a picture jump (64x36 content diff, luma histogram, HSV colour histogram, edge change
    ratio) that is not explained by camera motion (phase-correlation motion-compensated diff, so a fast pan is
    not a cut). Flash / burn suppression: a run of at most 3 frames whose light or overlay differs from both
    neighbours is judged by block-wise structure similarity (robust to a light leak covering half the frame):
    same scene before and after = an effect, not a cut; different scenes = ONE cut, placed on the first frame
    that looks like the incoming shot (a two-frame burn hides the cut between its two frames). Gradual
    transitions (dissolves, fades) are found by a blend fit over a +-6 frame window.

Brain
    brain_reference(cfg, rid): contact strips around every candidate event in the opening (cuts, rejected
    cuts, flashes, devices) + one tile per shot for the whole video, with the detector numbers, go to the
    agent in one structured-output run. The reply (BRAIN_SCHEMA) is validated, citations are checked, and
    the result is cached by content hash (same pictures + prompt + model = no second call). The cost cap is
    the agent's own --max-budget-usd (Claude) plus an image budget, default $1 per reference.
    merge_brain(an, brain) folds the verdicts into an analysis: final cuts, shot type / subject / camera /
    framing per shot, AI-read devices and on-screen text, sections and the "why it works" notes.

    Library clips: clip_context() gives the tagging prompt the detector numbers (YuNet faces per sampled time,
    in-clip cut candidates); the overnight tagger asks for shot type / subject / framing / faces / text and
    cites sheet tile times (`seen_at`), checked by check_clip_citations(). No extra AI call per clip.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

# ==================================================================================================
# faces
# ==================================================================================================
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
FACE_SCORE = 0.62              # YuNet confidence floor (measured: real faces 0.75-0.95, caption text < 0.5)
FACE_NMS = 0.3
FACE_MIN_SIDE = 640            # frames are scaled so the long side is at least this (YuNet sees faces >= ~10 px)
FACE_MAX_SIDE = 1280           # ... and at most this (big faces > ~300 px are found on the downscaled pass)

_model_lock = threading.Lock()
_model_state = {"path": None, "tried": False, "error": None}


def model_dir() -> Path:
    if os.environ.get("CUTROOM_MODEL_DIR"):
        return Path(os.environ["CUTROOM_MODEL_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "cutroom" / "models"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "cutroom" / "models"


def _sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def ensure_model(download=None, url=YUNET_URL, sha256=YUNET_SHA256, timeout=20.0):
    """Path of the verified YuNet model, downloading it once if allowed. None when unavailable.
    A file with the wrong checksum is never used (and is replaced on the next download)."""
    if download is None:
        download = os.environ.get("CUTROOM_OFFLINE", "") not in ("1", "true", "yes")
    p = model_dir() / YUNET_FILE
    with _model_lock:
        if p.exists():
            if _sha256(p) == sha256:
                return p
            _model_state["error"] = "cached model failed its checksum"
        if not download or (_model_state["tried"] and _model_state.get("url") == url):
            return None
        _model_state["tried"], _model_state["url"] = True, url
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".part{os.getpid()}")
            with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:   # noqa: S310 - fixed https URL
                shutil.copyfileobj(r, f)
            if _sha256(tmp) != sha256:
                tmp.unlink(missing_ok=True)
                _model_state["error"] = "downloaded model failed its checksum"
                return None
            os.replace(tmp, p)
            _model_state["error"] = None
            return p
        except Exception as e:  # noqa: BLE001 - offline, proxy, disk: fall back to Haar
            _model_state["error"] = f"{type(e).__name__}: {e}"
            return None


def model_status() -> dict:
    p = model_dir() / YUNET_FILE
    return {"path": str(p), "present": p.exists(), "error": _model_state.get("error")}


class FaceDetector:
    """detect(bgr) -> [{"box": [x, y, w, h] fractions, "score": float|None, "kp": [[x, y] x5]|None}].
    backend: 'yunet' or 'haar' (whatever actually loaded)."""

    def __init__(self, backend="auto", score=FACE_SCORE, model_path=None):
        import cv2
        self.cv2 = cv2
        self.score = score
        self.backend = None
        self._yn = None
        self._haar = None
        want = (os.environ.get("CUTROOM_FACES") or backend or "auto").lower()
        if want in ("auto", "yunet"):
            mp = Path(model_path) if model_path else ensure_model()
            if mp and Path(mp).exists() and hasattr(cv2, "FaceDetectorYN"):
                try:
                    self._yn = cv2.FaceDetectorYN.create(str(mp), "", (320, 320), score, FACE_NMS, 5000)
                    self.backend = "yunet"
                except Exception:  # noqa: BLE001 - a corrupt/incompatible model: fall back
                    self._yn = None
        if self._yn is None:
            try:
                c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
                if not c.empty():
                    self._haar = c
                    self.backend = "haar"
            except AttributeError:
                pass

    @property
    def available(self):
        return self.backend is not None

    def _yunet(self, bgr):
        cv2 = self.cv2
        h, w = bgr.shape[:2]
        long = max(w, h)
        s = 1.0
        if long < FACE_MIN_SIDE:
            s = FACE_MIN_SIDE / long
        elif long > FACE_MAX_SIDE:
            s = FACE_MAX_SIDE / long
        im = bgr if abs(s - 1) < 1e-3 else cv2.resize(bgr, (int(round(w * s)), int(round(h * s))),
                                                      interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        H, W = im.shape[:2]
        self._yn.setInputSize((W, H))
        _, res = self._yn.detect(im)
        out = []
        for r in (res if res is not None else []):
            x, y, bw, bh = (float(v) for v in r[:4])
            kp = r[4:14].reshape(5, 2)
            sc = float(r[14])
            if sc < self.score or bw < 6 or bh < 6:
                continue
            # landmark sanity: both eyes above the mouth corners (rejects text strokes and patterns)
            eye_y, mouth_y = kp[:2, 1].mean(), kp[3:5, 1].mean()
            if not eye_y < mouth_y:
                continue
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(W), x + bw), min(float(H), y + bh)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            out.append({"box": [x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H], "score": round(sc, 3),
                        "kp": [[round(float(a) / W, 4), round(float(b) / H, 4)] for a, b in kp]})
        return out

    def _haar_detect(self, bgr):
        cv2 = self.cv2
        h, w = bgr.shape[:2]
        g = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        m = max(18, int(min(w, h) * 0.07))
        boxes = self._haar.detectMultiScale(g, scaleFactor=1.15, minNeighbors=6, minSize=(m, m))
        return [{"box": [x / w, y / h, bw / w, bh / h], "score": None, "kp": None}
                for (x, y, bw, bh) in (boxes if len(boxes) else [])]

    def detect(self, bgr):
        if bgr is None or not self.available:
            return []
        return self._yunet(bgr) if self._yn is not None else self._haar_detect(bgr)


_detectors = {}


def face_detector(backend="auto") -> FaceDetector:
    """One shared detector per backend per process (thread-safe enough for cutroom's use: YuNet calls are
    serialised by a lock)."""
    key = (os.environ.get("CUTROOM_FACES") or backend, os.environ.get("CUTROOM_MODEL_DIR"))
    d = _detectors.get(key)
    if d is None:
        d = _detectors[key] = FaceDetector(backend)
        d._lock = threading.Lock()
    return d


def detect_faces(bgr, backend="auto") -> list:
    """Face dicts for one BGR frame (shared detector)."""
    d = face_detector(backend)
    with d._lock:
        return d.detect(bgr)


def face_summary(faces) -> dict:
    """count, largest face height (share of frame), its centre and a position word."""
    if not faces:
        return {"count": 0, "size": 0.0, "x": None, "y": None, "position": None}
    big = max(faces, key=lambda f: f["box"][3])
    x, y, w, h = big["box"]
    cx, cy = x + w / 2, y + h / 2
    col = "left" if cx < 0.38 else ("right" if cx > 0.62 else "centre")
    row = "top" if cy < 0.3 else ("low" if cy > 0.7 else "")
    return {"count": len(faces), "size": round(h, 3), "x": round(cx, 3), "y": round(cy, 3),
            "position": (row + "-" + col) if row else col}


def face_size_word(h) -> str:
    """Shot size from the largest face height (share of the frame height)."""
    if not h:
        return "none"
    if h >= 0.30:
        return "close"
    if h >= 0.12:
        return "medium"
    return "wide"


# ==================================================================================================
# shots: features (one frame at a time) + the combined detector
# ==================================================================================================
SAME_LAGS = 5
SHOT_FEATS = ("chd", "ecr", "mcd", "shift", "dluma") + tuple(f"same{L}" for L in range(1, SAME_LAGS + 1))
CUT_MAFD = 8.0                # 64x36 mean abs diff (0-255) for a hard cut, relative to the local median too
ANOMALY_CHD = 0.10            # colour-histogram jump that marks a light / overlay event (a jump cut stays below)
ANOMALY_LUMA = 6.0
SAME = 0.5                    # "same picture" score at or above this
FLASH_MAX = 3                 # a light / overlay run this short is never a shot of its own when the scene holds
GRAD_K = 6                    # half window of the gradual-transition (dissolve / fade) fit
ORB_W = 320


def block_match(a, b, bw=8, bh=6):
    """Share of blocks (of 64x36 grey frames) that look alike: mean and contrast within 8 levels. Blocks flat
    and equal in both (a black matte, a clear sky) are left out, so an inset's border never makes two different
    pictures 'the same'; blocks under an overlay simply fail, so the rest still carries a covered picture."""
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    h, w = a.shape
    A = a[:h // bh * bh, :w // bw * bw].reshape(h // bh, bh, w // bw, bw)
    B = b[:h // bh * bh, :w // bw * bw].reshape(h // bh, bh, w // bw, bw)
    ma, mb = A.mean(axis=(1, 3)), B.mean(axis=(1, 3))
    sa, sb = A.std(axis=(1, 3)), B.std(axis=(1, 3))
    flat_same = (sa < 3) & (sb < 3) & (np.abs(ma - mb) < 6)
    ok = (np.abs(ma - mb) < 8) & (np.abs(sa - sb) < 8)
    live = ~flat_same
    if live.sum() < 6:
        return 1.0 if flat_same.mean() > 0.9 else 0.0
    return float((ok & live).sum() / live.sum())


class _Orb:
    """ORB keypoints + a RANSAC homography: how much of picture b is picture a, moved and/or partly covered.
    Overlays, burns and light leaks leave the uncovered part matching; a different shot does not."""

    def __init__(self, cv2):
        self.cv2 = cv2
        self.orb = cv2.ORB_create(nfeatures=400, fastThreshold=12, edgeThreshold=15, patchSize=21)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def describe(self, grey):
        kp, des = self.orb.detectAndCompute(grey, None)
        return (np.float32([k.pt for k in kp]) if kp else np.zeros((0, 2), np.float32)), des

    def same(self, fa, fb, ga, gb):
        """0-1: the larger of (a) the share of the smaller keypoint set explained by one homography and
        (b) the share of textured 8x6 blocks (64x36 grey) whose level and contrast match."""
        return max(self._orb_score(fa, fb), block_match(ga, gb))

    def _orb_score(self, fa, fb):
        (pa, da), (pb, db) = fa, fb
        if da is None or db is None or len(pa) < 25 or len(pb) < 25:
            return 0.0
        m = self.bf.match(da, db)
        if len(m) < 8:
            return 0.0
        src = np.float32([pa[x.queryIdx] for x in m])
        dst = np.float32([pb[x.trainIdx] for x in m])
        H, mask = self.cv2.findHomography(src, dst, self.cv2.RANSAC, 4.0)
        if H is None or mask is None:
            return 0.0
        inl = int(mask.sum())
        # a degenerate fit (only a few points on a static matte corner / logo) is not the same picture
        if inl < 12:
            return 0.0
        spread = np.ptp(dst[mask.ravel() > 0], axis=0)
        if spread[0] < 0.15 * ORB_W and spread[1] < 0.15 * ORB_W * 9 / 16:
            return 0.0
        return float(min(1.0, inl / (0.45 * min(len(pa), len(pb)))))


class ShotFeatures:
    """Per-frame shot-boundary features on top of reference.scan_video's own (mafd, hd, luma, g32):
        chd     HSV colour histogram distance to the previous frame (0-1)
        ecr     edge change ratio (0-1): share of edges that appear or vanish
        mcd     motion-compensated 128x72 diff: the content diff after undoing the best global shift
        dluma   luma change vs the previous frame
        sameL   "same picture" score (ORB + RANSAC homography) to the frame L back, L = 1..5; computed only
                where the picture jumps (NaN elsewhere: nothing to decide)"""

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.orb = _Orb(cv2)
        self.ring = []          # last SAME_LAGS frames: [grey320, g32, orb-or-None]
        self.prev = None
        self.out = {k: [] for k in SHOT_FEATS}

    def _orb_of(self, item):
        if item[2] is None:
            item[2] = self.orb.describe(item[0])
        return item[2]

    def feed(self, bgr, grey=None):
        cv2 = self.cv2
        if grey is None:
            grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h, w = grey.shape
        g = grey if w == ORB_W else cv2.resize(grey, (ORB_W, max(2, int(round(ORB_W * h / w)))), interpolation=cv2.INTER_AREA)
        g32 = cv2.resize(grey, (32, 18), interpolation=cv2.INTER_AREA)
        g64 = cv2.resize(grey, (64, 36), interpolation=cv2.INTER_AREA)
        g128 = cv2.resize(grey, (128, 72), interpolation=cv2.INTER_AREA).astype(np.float32)
        small = cv2.resize(bgr, (96, 54), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [12, 4], [0, 180, 0, 256]).ravel()
        hist = hist / max(1.0, hist.sum())
        e = cv2.Canny(cv2.resize(grey, (160, 90), interpolation=cv2.INTER_AREA), 60, 140)
        ed = cv2.dilate(e, np.ones((3, 3), np.uint8))
        lum = float(g32.mean())
        o = self.out
        jump = False
        if self.prev is None:
            for k in ("chd", "ecr", "mcd", "shift", "dluma"):
                o[k].append(0.0)
        else:
            ph, pe, ped, pg, pl, p32 = self.prev
            chd = float(0.5 * np.abs(hist - ph).sum())
            o["chd"].append(chd)
            ne, pn = float((e > 0).sum()), float((pe > 0).sum())
            if ne < 30 and pn < 30:
                o["ecr"].append(0.0)
            else:
                ein = float(((e > 0) & (ped == 0)).sum()) / max(ne, 1.0)
                eout = float(((pe > 0) & (ed == 0)).sum()) / max(pn, 1.0)
                o["ecr"].append(max(ein, eout))
            try:
                win = cv2.createHanningWindow((128, 72), cv2.CV_32F)
                (dx, dy), _ = cv2.phaseCorrelate(pg.copy(), g128.copy(), win)   # it may window its inputs in place
            except Exception:  # noqa: BLE001
                dx = dy = 0.0
            if abs(dx) > 40 or abs(dy) > 24:
                dx = dy = 0.0
            M = np.float32([[1, 0, -dx], [0, 1, -dy]])
            warped = cv2.warpAffine(g128, M, (128, 72), borderMode=cv2.BORDER_REPLICATE)
            m = 6
            o["mcd"].append(float(np.abs(warped[m:-m, m:-m] - pg[m:-m, m:-m]).mean()))
            o["shift"].append(float(np.hypot(dx, dy)))
            o["dluma"].append(lum - pl)
            d32 = float(np.abs(g32.astype(np.int16) - p32.astype(np.int16)).mean())
            jump = d32 >= 3.5 or chd >= ANOMALY_CHD or abs(lum - pl) >= ANOMALY_LUMA
        cur = [g, g64, None]
        for L in range(1, SAME_LAGS + 1):
            if jump and len(self.ring) >= L:
                it = self.ring[-L]
                o[f"same{L}"].append(self.orb.same(self._orb_of(it), self._orb_of(cur), it[1], g64))
            else:
                o[f"same{L}"].append(np.nan)
        self.ring.append(cur)
        if len(self.ring) > SAME_LAGS:
            self.ring.pop(0)
        self.prev = (hist, e, ed, g128, lum, g32)

    def arrays(self, n=None):
        return {k: np.asarray(v[:n] if n else v, np.float32) for k, v in self.out.items()}


def same_pic(arr, a, b):
    """'Same picture' score of frames a and b (stored when computed at scan time, else a grey-level estimate)."""
    if a > b:
        a, b = b, a
    L = b - a
    if L == 0:
        return 1.0
    if 1 <= L <= SAME_LAGS and b < len(arr["same1"]):
        v = float(arr[f"same{L}"][b])
        if not np.isnan(v):
            return v
    g = arr["g32"]
    return block_match(g[a], g[b], 4, 3)


def _local_med(x, i, r=12):
    lo, hi = max(1, i - r), min(len(x), i + r + 1)
    nb = np.concatenate([x[lo:max(lo, i - 1)], x[min(hi, i + 2):hi]])
    return float(np.median(nb)) if len(nb) else 0.0


def boundary_score(arr, i) -> dict:
    """The evidence for a boundary between frame i-1 and frame i (all detector numbers, for the brain)."""
    mafd, hd = arr["mafd"], arr["hd"]
    med = _local_med(mafd, i)
    out = {"frame": int(i), "mafd": round(float(mafd[i]), 2), "mafd_med": round(med, 2), "hd": round(float(hd[i]), 3),
           "luma": round(float(arr["luma"][i]), 1), "luma_prev": round(float(arr["luma"][i - 1]), 1)}
    if "chd" in arr:
        out.update({"chd": round(float(arr["chd"][i]), 3), "ecr": round(float(arr["ecr"][i]), 3),
                    "mcd": round(float(arr["mcd"][i]), 2), "same": round(same_pic(arr, i - 1, i), 3)})
    return out


def _is_jump(arr, i) -> bool:
    """A picture jump between frames i-1 and i that camera motion does not explain."""
    mafd, hd = arr["mafd"], arr["hd"]
    d = float(mafd[i])
    med = _local_med(mafd, i)
    rel = d >= CUT_MAFD and d >= 3.0 * (med + 1.5)
    hist = float(hd[i]) >= 0.42 and d >= 6.0 and d >= 2.0 * (med + 1.5)
    col = float(arr["chd"][i]) >= 0.5 and d >= 5.0 and d >= 2.0 * (med + 1.5)
    edge = float(arr["ecr"][i]) >= 0.55 and d >= 5.0 and d >= 2.5 * (med + 1.5)
    if not (rel or hist or col or edge):
        return False
    # a fast pan / whip: the picture moved (a real global shift) and, shifted back, it matches
    mc = float(arr["mcd"][i])
    sh = float(arr["shift"][i])
    if sh >= 1.5 and mc < 0.4 * d and same_pic(arr, i - 1, i) >= SAME and float(hd[i]) < 0.3:
        # ... and the camera was already moving (a jump cut's shift appears on one frame only)
        nbs = max(float(arr["shift"][i - 1]) if i > 1 else 0.0, float(arr["shift"][i + 1]) if i + 1 < len(mafd) else 0.0)
        if nbs >= 0.5 * sh:
            return False
    # continuous motion or a sweeping light (a runner, confetti, a leak crossing the frame): the neighbours jump
    # as much and the picture still matches itself; a cut is a single step to a different picture
    if _continuous(arr, i):
        return False
    return True


def _continuous(arr, i) -> bool:
    mafd = arr["mafd"]
    d = float(mafd[i])
    nb = max(float(mafd[i - 1]) if i > 1 else 0.0, float(mafd[i + 1]) if i + 1 < len(mafd) else 0.0)
    return d < 1.8 * nb and same_pic(arr, i - 1, i) >= 0.3


def _soft_ok(arr, i) -> bool:
    """A scene-detector candidate (PySceneDetect) the jump test missed: kept only when the frame clearly stands out
    from its neighbourhood, is not continuous motion, and the structure says the picture changed."""
    d = float(arr["mafd"][i])
    return (d >= 4.0 and d >= 2.0 * (_local_med(arr["mafd"], i) + 1.5) and not _continuous(arr, i)
            and same_pic(arr, i - 1, i) < SAME)


def _anomaly(arr, i) -> bool:
    """A light / colour / overlay event on frame i (vs i-1): what a flash, burn or wipe looks like."""
    return float(arr["chd"][i]) >= ANOMALY_CHD or abs(float(arr["dluma"][i])) >= ANOMALY_LUMA


def _gradual(arr, fps, hard):
    """Dissolves and fades: frames where the picture k frames before and after differ a lot, no hard cut sits
    inside, and the middle frame is a blend of the two ends (not a pan, whose middle is neither). A run of
    qualifying frames is one transition, placed where the blend is half-way."""
    g = arr["g32"].astype(np.float32)
    n = len(g)
    k = GRAD_K
    hs = np.array(sorted(hard) + [10 ** 9])

    def fit_at(i):
        a, b = i - k, i + k
        j = int(np.searchsorted(hs, a + 1))
        if hs[j] <= b:
            return None
        A, B, M = g[a], g[b], g[i]
        dab = float(np.abs(A - B).mean())
        if dab < 15 or float(np.max(arr["mafd"][a + 1:b + 1])) >= CUT_MAFD * 1.5:
            return None
        D = A - B
        den = float((D * D).sum())
        alpha = float(np.clip(((M - B) * D).sum() / max(den, 1e-6), 0, 1))
        fit = float(np.abs(M - (alpha * A + (1 - alpha) * B)).mean())
        dam, dbm = float(np.abs(M - A).mean()), float(np.abs(M - B).mean())
        steps = arr["mafd"][a + 1:b + 1]
        steady = float(np.max(steps)) < 3.5 * (float(np.median(steps)) + 0.5)
        moving = float(np.median(arr["mcd"][a + 1:b + 1])) < 0.6 * float(np.median(steps)) if "mcd" in arr else False
        different = block_match(A, B, 4, 3) < 0.35          # a flare fading on one picture is not a transition
        if 0.2 < alpha < 0.8 and fit < 0.15 * dab and min(dam, dbm) > 0.25 * dab and steady and not moving and different:
            return {"alpha": alpha, "fit": fit / dab, "dark": min(float(arr["luma"][a]), float(arr["luma"][b])) < 16}
        return None
    out = []
    i = k
    while i < n - k:
        f = fit_at(i)
        if not f:
            i += 1
            continue
        run = [(i, f)]
        j = i + 1
        while j < n - k:
            fj = fit_at(j)
            if not fj:
                break
            run.append((j, fj))
            j += 1
        c, fc = min(run, key=lambda x: abs(x[1]["alpha"] - 0.5))
        out.append({"f0": run[0][0] - k, "f1": run[-1][0] + k + 1, "frame": c, "t": round(c / fps, 4),
                    "kind": "fade" if fc["dark"] else "dissolve", "fit": round(fc["fit"], 3), "alpha": round(fc["alpha"], 2)})
        i = j + k
    return out


def detect_shots(arr, fps, extra_candidates=()) -> dict:
    """The combined detector on reference.scan_video arrays. Returns
    {"cuts": [first frame of every new shot], "flashes": [{f0, frames, t, kind}], "gradual": [...],
     "rejected": [{frame, why, ...numbers}]}.

    1. candidates: picture jumps (content / histogram / colour / edge) not explained by camera motion
    2. flash return: the picture before comes back within FLASH_MAX frames -> the frames between are an effect
    3. overlay start / end: a light or colour event whose frame is still the same picture as the frame before
       (a burn's outgoing frame, a wipe over the same shot, the clean frame after a burn) -> not a cut
    4. black runs bound shots; dissolves / fades add a boundary at their middle"""
    n = int(arr["n"][0])
    luma = arr["luma"]
    cand = [i for i in range(1, n) if _is_jump(arr, i)]
    pys = set(int(c) for c in list(arr.get("content_cuts", [])) + list(arr.get("adaptive_cuts", [])) + list(extra_candidates))
    for c in sorted(pys):
        if 0 < c < n and not any(abs(c - x) <= 2 for x in cand) and _soft_ok(arr, c):
            cand.append(c)
    cand = sorted(set(cand))
    cset = set(cand)
    rejected, flashes = [], []
    drop = set()
    for c in cand:
        if c in drop:
            continue
        # 2. flash return
        back = next((k for k in range(1, FLASH_MAX + 1) if c + k < n and same_pic(arr, c - 1, c + k) >= SAME
                     and same_pic(arr, c - 1, c) < SAME), None)
        if back:
            inside = [x for x in cand if c <= x <= c + back]
            for x in inside:
                drop.add(x)
                rejected.append({**boundary_score(arr, x), "why": f"the picture before comes back after a {back}-frame "
                                                                   f"flash/effect (frames {c}-{c + back - 1})"})
            bright = float(luma[c:c + back].mean()) - max(float(luma[c - 1]), float(luma[min(n - 1, c + back)]))
            flashes.append({"f0": c, "frames": back, "t": round(c / fps, 4),
                            "kind": "white" if bright > 40 else ("flash" if bright > 8 else "effect")})
            continue
        # 3. overlay / light event on the same picture
        if _anomaly(arr, c) and same_pic(arr, c - 1, c) >= SAME:
            drop.add(c)
            rejected.append({**boundary_score(arr, c), "why": "light/overlay change on the same picture (burn, wipe, flash edge)"})
            continue
    cuts = [c for c in cand if c not in drop]
    # two "cuts" one frame apart around a light / overlay event: one of them is the overlay's other edge (a
    # scribble or leak that leaves one frame after the cut). Keep the bigger change, drop the one whose frame
    # still shows its neighbour's picture.
    ks = set(cuts)
    for c in list(cuts):
        if c in ks and (c - 1) in ks and _anomaly(arr, c) and _anomaly(arr, c - 1):
            sa, sb = same_pic(arr, c - 2, c - 1), same_pic(arr, c - 1, c)
            loser, s_ = (c, sb) if sb >= sa else (c - 1, sa)
            if s_ >= 0.25:
                ks.discard(loser)
                drop.add(loser)
                rejected.append({**boundary_score(arr, loser), "why": "overlay edge one frame from a cut (same picture as its neighbour)"})
    cuts = sorted(ks)
    # a one-frame white / graphic flash frame between two cuts: on one shot (the picture after it is the picture
    # before it) it is an effect, not two cuts; between two different shots it stays a 1-frame flash shot (the cut
    # map keeps it, so a rebuild can place the flash)
    ks = set(cuts)
    for c in cuts:
        if c in ks and (c + 1) in ks and c + 1 < n:
            lc = float(luma[c])
            if lc >= 180 or lc - max(float(luma[c - 1]), float(luma[c + 1])) >= 40:
                if same_pic(arr, c - 1, c + 1) >= 0.3:
                    ks.discard(c)
                    ks.discard(c + 1)
                    for x in (c, c + 1):
                        rejected.append({**boundary_score(arr, x), "why": "same picture either side of a one-frame flash"})
                flashes.append({"f0": c, "frames": 1, "t": round(c / fps, 4), "kind": "white"})
    cuts = sorted(ks)
    # burns: an anomaly frame right before or after a kept cut = the two-frame light-leak device
    for c in cuts:
        a_side = c - 1 >= 1 and (c - 1) not in ks and _anomaly(arr, c - 1)
        b_side = c + 1 < n and (c + 1) not in ks and _anomaly(arr, c + 1)
        if a_side or b_side:
            f0 = c - 1 if a_side else c
            f1 = c + 1 if b_side else c
            bright = float(luma[f0:f1].mean()) - max(float(luma[max(0, f0 - 1)]), float(luma[min(n - 1, f1)]))
            flashes.append({"f0": f0, "frames": f1 - f0, "t": round(f0 / fps, 4),
                            "kind": "burn" if bright > 8 or _anomaly(arr, c) else "wipe", "cut": c})
    # 4. black runs: both edges are boundaries
    cs = set(cuts)
    dark = (luma < 14) & (arr["std"] < 6)
    s = None
    for i in range(n + 1):
        v = bool(dark[i]) if i < n else False
        if v and s is None:
            s = i
        elif not v and s is not None:
            if i - s >= 2:
                for e in (s, i):
                    if 0 < e < n and not any(abs(e - c) <= 2 for c in cs):
                        cs.add(e)
            s = None
    gradual = _gradual(arr, fps, cs)
    for gr in gradual:
        if not any(abs(gr["frame"] - c) <= GRAD_K for c in cs):
            cs.add(gr["frame"])
    flashes.sort(key=lambda f: f["f0"])
    # picture events that are not cuts (light, colour or overlay changes): what the brain pass should look at
    final = sorted(c for c in cs if 0 < c < n)
    near = set(f for c in final for f in range(c - 2, c + 3)) | set(f for fl in flashes for f in range(fl["f0"] - 1, fl["f0"] + fl["frames"] + 2))
    effects = []
    for i in range(1, n):
        if i in near or not (_anomaly(arr, i) and float(arr["mafd"][i]) >= 5.0):
            continue
        if effects and i - effects[-1]["f1"] <= 3:
            effects[-1]["f1"] = i + 1
            continue
        effects.append({"f0": i, "f1": i + 1, "t": round(i / fps, 4)})
    return {"cuts": final, "flashes": flashes, "gradual": gradual, "rejected": rejected, "effects": effects}


def shot_features_from_frames(frames_bgr):
    """Test helper: the full scan feature set for a list of BGR frames (any size)."""
    import cv2
    sf = ShotFeatures()
    L = {k: [] for k in ("luma", "std", "mafd", "hd")}
    g32 = []
    prev_small = prev_hist = None
    for fr in frames_bgr:
        grey = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        m, s = cv2.meanStdDev(grey)
        L["luma"].append(float(m[0, 0]))
        L["std"].append(float(s[0, 0]))
        small = cv2.resize(grey, (64, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
        hist = np.bincount((small.ravel() >> 4).astype(np.int64), minlength=16).astype(np.float32) / small.size
        L["mafd"].append(float(np.abs(small - prev_small).mean()) if prev_small is not None else 0.0)
        L["hd"].append(float(0.5 * np.abs(hist - prev_hist).sum()) if prev_hist is not None else 0.0)
        g32.append(cv2.resize(grey, (32, 18), interpolation=cv2.INTER_AREA))
        sf.feed(fr, grey)
        prev_small, prev_hist = small, hist
    arr = {k: np.asarray(v, np.float32) for k, v in L.items()}
    arr.update(sf.arrays())
    arr["g32"] = np.asarray(g32, np.uint8)
    arr["n"] = np.asarray([len(frames_bgr)])
    arr["content_cuts"] = np.asarray([], np.int64)
    arr["adaptive_cuts"] = np.asarray([], np.int64)
    return arr


# ==================================================================================================
# a small JSON-schema validator (the subset the brain schemas use; no extra dependency)
# ==================================================================================================
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def schema_errors(obj, schema, path="$", out=None, limit=50):
    """List of 'path: problem' strings; empty = valid. Supports type (incl. lists), properties, required,
    additionalProperties: false, items, enum, minimum, maximum, minItems, maxItems, maxLength."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        ok = False
        for x in ts:
            if x == "integer":
                ok = ok or (isinstance(obj, int) and not isinstance(obj, bool))
            elif x == "number":
                ok = ok or (isinstance(obj, (int, float)) and not isinstance(obj, bool))
            elif x in _TYPES:
                ok = ok or isinstance(obj, _TYPES[x])
        if not ok:
            out.append(f"{path}: expected {t}, got {type(obj).__name__}")
            return out
    if "enum" in schema and obj not in schema["enum"]:
        out.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            out.append(f"{path}: {obj} < {schema['minimum']}")
        if "maximum" in schema and obj > schema["maximum"]:
            out.append(f"{path}: {obj} > {schema['maximum']}")
    if isinstance(obj, str) and "maxLength" in schema and len(obj) > schema["maxLength"]:
        out.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(obj, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in obj:
                out.append(f"{path}: missing '{k}'")
        if schema.get("additionalProperties") is False:
            for k in obj:
                if k not in props:
                    out.append(f"{path}: unexpected '{k}'")
        for k, v in obj.items():
            if k in props:
                schema_errors(v, props[k], f"{path}.{k}", out, limit)
    if isinstance(obj, list):
        if "minItems" in schema and len(obj) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, v in enumerate(obj):
                schema_errors(v, schema["items"], f"{path}[{i}]", out, limit)
    return out


# ==================================================================================================
# the brain pass: schema
# ==================================================================================================
SHOT_TYPES = ["wide", "medium", "close", "insert", "pov", "drone", "screen", "archive", "graphic", "black"]
SUBJECTS = ["creator", "speaker", "crowd", "people", "object", "place", "text", "none"]
CAMERA_MOVES = ["static", "handheld", "pan", "tilt", "push_in", "pull_out", "tracking", "zoom", "whip", "drone_move",
                "unknown"]
FRAMING = ["good", "ok", "poor"]
DEVICE_TYPES = ["card", "burn", "flash", "inset", "gate", "letterbox", "typed_title", "title", "scribble", "shape_wipe",
                "glitch", "pip", "film_frame", "overlay", "speed_ramp", "punch_in", "split_screen", "other"]
TEXT_ROLES = ["card", "title", "caption", "lower_third", "overlay", "sign", "other"]
CUT_VERDICTS = ["cut", "not_a_cut", "gradual"]

_frames_arr = {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 1, "maxItems": 12}
BRAIN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["events", "missed_cuts", "shots", "devices", "text_on_screen", "sections", "why_it_works", "summary"],
    "properties": {
        "events": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "verdict", "frame", "reason", "frames"],
            "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": CUT_VERDICTS},
                           "frame": {"type": ["integer", "null"]}, "reason": {"type": "string"},
                           "frames": _frames_arr}}},
        "missed_cuts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["frame", "reason", "frames"],
            "properties": {"frame": {"type": "integer"}, "reason": {"type": "string"}, "frames": _frames_arr}}},
        "shots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "type", "subject", "camera", "framing", "faces", "text", "note", "frames"],
            "properties": {"id": {"type": "string"}, "type": {"type": "string", "enum": SHOT_TYPES},
                           "subject": {"type": "string", "enum": SUBJECTS}, "camera": {"type": "string", "enum": CAMERA_MOVES},
                           "framing": {"type": "string", "enum": FRAMING}, "faces": {"type": ["integer", "null"], "minimum": 0},
                           "text": {"type": ["string", "null"]}, "note": {"type": ["string", "null"]},
                           "frames": _frames_arr}}},
        "devices": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "f0", "f1", "detail", "frames"],
            "properties": {"kind": {"type": "string", "enum": DEVICE_TYPES}, "f0": {"type": "integer", "minimum": 0},
                           "f1": {"type": "integer", "minimum": 0}, "detail": {"type": "string"},
                           "frames": _frames_arr}}},
        "text_on_screen": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["f0", "f1", "text", "role", "frames"],
            "properties": {"f0": {"type": "integer", "minimum": 0}, "f1": {"type": "integer", "minimum": 0},
                           "text": {"type": "string"}, "role": {"type": "string", "enum": TEXT_ROLES},
                           "frames": _frames_arr}}},
        "sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "f0", "f1", "purpose", "frames"],
            "properties": {"name": {"type": "string"}, "f0": {"type": "integer", "minimum": 0},
                           "f1": {"type": "integer", "minimum": 0}, "purpose": {"type": "string"},
                           "frames": _frames_arr}}},
        "why_it_works": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False, "required": ["point", "frames"],
            "properties": {"point": {"type": "string"}, "frames": _frames_arr}}},
        "summary": {"type": "string"},
    },
}


# ==================================================================================================
# the brain pass: images
# ==================================================================================================
STRIP_W, STRIP_H = 200, 112        # one frame in an event strip
STRIP_BEFORE, STRIP_AFTER = 3, 3   # frames shown either side of an event (7 frames per strip)
ROWS_PER_IMAGE = 8                 # event strips per image
SHOT_TILE = (256, 144)
SHOT_COLS, SHOT_ROWS = 6, 5
BRAIN_WINDOW_S = 60.0              # event strips cover the opening (where the devices live)
MAX_EVENTS = 64
EST_TOKENS_PER_IMAGE = 1700        # ~ (w*h)/750 for a 1400x1000 image
DEFAULT_BUDGET_USD = 1.0
DEFAULT_MODEL = "sonnet"           # the brain pass reads pictures and fills a schema: Sonnet-class is plenty; set
                                   # vision_model to "opus" (or "" = the agent's own default) for more judgement
MAX_SHOT_LABELS = 60               # shots the agent labels (all of the opening, then where the type changes)


def _font(cv2):
    return cv2.FONT_HERSHEY_SIMPLEX


def _label(cv2, img, text, colour=(0, 255, 255), scale=0.45):
    cv2.rectangle(img, (0, 0), (img.shape[1], 16), (25, 25, 25), -1)
    cv2.putText(img, text, (3, 12), _font(cv2), scale, colour, 1, cv2.LINE_AA)
    return img


def decode_window(cfg, video, t1, w=STRIP_W, h=STRIP_H, t0=0.0):
    """Every frame of video[t0, t1) as small BGR arrays (one ffmpeg decode)."""
    from . import media  # noqa: F401  (keeps ffmpeg use discoverable; the decode streams raw frames)
    cmd = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if t0:
        cmd += ["-ss", f"{t0:.3f}"]
    cmd += ["-t", f"{t1 - t0:.3f}", "-i", str(video), "-map", "0:v:0", "-an", "-vf", f"scale={w}:{h}:flags=area",
            "-fps_mode", "passthrough", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    nice = shutil.which("nice")
    if nice:
        cmd = [nice, "-n", "10"] + cmd
    r = subprocess.run(cmd, capture_output=True, check=False)
    fs = w * h * 3
    k = len(r.stdout) // fs
    return np.frombuffer(r.stdout[:k * fs], np.uint8).reshape(k, h, w, 3)


def event_strip_images(frames, events, out_dir, prefix="events"):
    """Tile event strips (STRIP_BEFORE..STRIP_AFTER frames around each event, each frame labelled with its
    number, the event's first incoming frame outlined) into images. Returns ([paths], {event_id: [frames]})."""
    import cv2
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(frames)
    rows, shown = [], {}
    for ev in events:
        f = int(ev["frame"])
        idx = [x for x in range(f - STRIP_BEFORE, f + STRIP_AFTER + 1) if 0 <= x < n]
        if not idx:
            continue
        shown[ev["id"]] = idx
        tiles = []
        for x in idx:
            t = frames[x].copy()
            t = cv2.copyMakeBorder(t, 18, 2, 2, 2, cv2.BORDER_CONSTANT,
                                   value=(0, 160, 255) if x == f else (40, 40, 40))
            _label(cv2, t, f"{x}", (0, 255, 255) if x != f else (0, 200, 255))
            tiles.append(t)
        while len(tiles) < STRIP_BEFORE + STRIP_AFTER + 1:
            tiles.append(np.zeros_like(tiles[0]))
        head = np.zeros((22, sum(t.shape[1] for t in tiles), 3), np.uint8)
        cv2.putText(head, f"{ev['id']}  {ev.get('label', '')}"[:150], (4, 16), _font(cv2), 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        rows.append(np.vstack([head, np.hstack(tiles)]))
    paths = []
    for k in range(0, len(rows), ROWS_PER_IMAGE):
        p = out_dir / f"{prefix}_{k // ROWS_PER_IMAGE + 1:02d}.jpg"
        cv2.imwrite(str(p), np.vstack(rows[k:k + ROWS_PER_IMAGE]), [cv2.IMWRITE_JPEG_QUALITY, 80])
        paths.append(p)
    return paths, shown


def shot_sheet_images(ref_dir, shots, out_dir, prefix="shots"):
    """One tile per shot (its middle frame, from the analysis thumbs), labelled 'S<i> f<mid>'.
    Returns ([paths], {shot_id: [mid frame]})."""
    import cv2
    out_dir = Path(out_dir)
    tw, th = SHOT_TILE
    tiles, shown = [], {}
    for s in shots:
        mid = (int(s["f0"]) + int(s["f1"]) - 1) // 2
        img = cv2.imread(str(Path(ref_dir) / s["thumb"])) if s.get("thumb") else None
        if img is None:
            img = np.zeros((th, tw, 3), np.uint8)
        img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        img = cv2.copyMakeBorder(img, 18, 2, 2, 2, cv2.BORDER_CONSTANT, value=(40, 40, 40))
        _label(cv2, img, f"S{s['i']} f{mid} ({s['frames']}fr)")
        tiles.append(img)
        shown[f"S{s['i']}"] = [mid]
    paths = []
    per = SHOT_COLS * SHOT_ROWS
    for k in range(0, len(tiles), per):
        chunk = tiles[k:k + per]
        while len(chunk) % SHOT_COLS:
            chunk.append(np.zeros_like(chunk[0]))
        grid = np.vstack([np.hstack(chunk[r:r + SHOT_COLS]) for r in range(0, len(chunk), SHOT_COLS)])
        p = out_dir / f"{prefix}_{k // per + 1:02d}.jpg"
        cv2.imwrite(str(p), grid, [cv2.IMWRITE_JPEG_QUALITY, 78])
        paths.append(p)
    return paths, shown


# ==================================================================================================
# the brain pass: candidates, prompt, call, checks
# ==================================================================================================

def reference_events(an, arr, fps, window_s=BRAIN_WINDOW_S, max_events=MAX_EVENTS):
    """Candidate events in the opening: every detector cut, every rejected candidate, flashes, gradual
    transitions and device edges. Each: {id, frame, kind, label, numbers}."""
    lim = int(window_s * fps)
    evs = []
    seen = set()

    def add(f, kind, label, numbers=None):
        f = int(f)
        if f <= 0 or f >= lim or (f, kind) in seen:
            return
        seen.add((f, kind))
        evs.append({"frame": f, "kind": kind, "label": label, "numbers": numbers or {}})
    for s in an.get("shots", [])[1:]:
        add(s["f0"], "cut", "detector: CUT here", boundary_score(arr, s["f0"]) if arr is not None else {})
    sh = an.get("shot_detector") or {}
    for r in sh.get("rejected") or []:
        add(r["frame"], "rejected", f"detector: NOT a cut ({r.get('why', '')[:60]})", r)
    for fl in sh.get("flashes") or an.get("flashes") or []:
        add(fl["f0"], "flash", f"detector: {fl.get('frames')}-frame {fl.get('kind')}", fl)
    for g in sh.get("gradual") or []:
        add(g["frame"], "gradual", f"detector: {g['kind']} (gradual)", g)
    for x in sh.get("effects") or []:
        add(x["f0"], "effect", f"detector: light/colour/overlay change over {x['f1'] - x['f0']} frame(s), judged NOT a cut",
            boundary_score(arr, x["f0"]) if arr is not None else {})
    dev = an.get("devices") or {}
    if dev.get("card"):
        add(dev["card"]["f1"], "device", "detector: title card ends", {"card": dev["card"].get("text")})
    for r in dev.get("frames") or []:
        add(r["f0"], "device", f"detector: {r['kind']} frame starts", {"box": r.get("box")})
    for t in dev.get("titles") or []:
        add(t["f0"], "device", "detector: on-screen title starts", {})
    evs.sort(key=lambda e: (e["frame"], e["kind"]))
    # merge events on the same or adjacent frames into one strip
    merged = []
    for e in evs:
        if merged and e["frame"] - merged[-1]["frame"] <= 1:
            m = merged[-1]
            if e["kind"] == "cut" and "cut" not in m["kinds"]:
                m["frame"] = e["frame"]                 # the strip centres on the detector's cut frame
            m["label"] += " | " + e["label"]
            m["kinds"].append(e["kind"])
            m["numbers"] = {**e["numbers"], **m["numbers"]}
            continue
        merged.append({**e, "kinds": [e["kind"]]})
    merged = merged[:max_events]
    for k, e in enumerate(merged, 1):
        e["id"] = f"E{k}"
    return merged


def _num_line(d):
    keep = ("mafd", "mafd_med", "hd", "chd", "ecr", "mcd", "sim", "luma_prev", "luma", "why", "kind", "frames", "card")
    return ", ".join(f"{k}={d[k]}" for k in keep if k in d and d[k] is not None)


def build_reference_prompt(an, events, shown_events, shots_shown, image_paths, fps, window_s=BRAIN_WINDOW_S):
    src = an.get("source") or {}
    dev = an.get("devices") or {}
    lines = []
    for e in events:
        if e["id"] in shown_events:
            lines.append(f"- {e['id']} frame {e['frame']} ({e['frame'] / fps:.2f}s): {e['label']}. Numbers: {_num_line(e['numbers'])}")
    shot_lines = []
    for s in an.get("shots", []):
        sid = f"S{s['i']}"
        if sid in shots_shown:
            shot_lines.append(f"- {sid} frames {s['f0']}-{s['f1'] - 1} ({s['dur']:.2f}s) detector kind={s.get('kind')} "
                              f"faces={s.get('faces')} face_h={s.get('face_h')} speech={s.get('speech')} "
                              f"luma={s.get('luma')} motion={s.get('motion')}")
    devs = json.dumps({k: dev.get(k) for k in ("card", "burns", "frames", "titles") if dev.get(k)}, default=str)[:3000]
    imgs = "\n".join(f"  {Path(p).resolve()}" for p in image_paths)
    return f"""You are a senior film editor breaking down a reference video so another editor can rebuild its style.
Fast detectors have already proposed shot boundaries, flashes and devices. They are often fooled (light-leak
burns, overlays, scribbles, fast pans). YOU decide, from the pictures. Read every image below with the Read tool
before answering (and nothing else):
{imgs}

The video: {src.get('width')}x{src.get('height')} at {fps:g} fps, {src.get('duration')} s, {len(an.get('shots', []))} detector shots.
Frame n is at n/{fps:g} s. Every tile carries its frame number in yellow; the outlined tile in a strip is the
frame the event is about (the detector's first frame of the new shot).

EVENT STRIPS (images named events_*): one row per candidate event, {STRIP_BEFORE} frames before to {STRIP_AFTER} after.
{chr(10).join(lines) or '(none)'}

SHOT SHEETS (images named shots_*): one tile per detector shot, its middle frame.
{chr(10).join(shot_lines)}

Detector devices (numbers): {devs}

Answer in the JSON schema. Rules:
- events: one verdict per event id. verdict "cut" = a real change of shot; give "frame" = the FIRST frame of the
  new shot (move it if the detector is off by a frame, e.g. a two-frame burn hides the cut between its frames).
  "not_a_cut" = same shot continues (flash, burn, overlay, scribble, shape wipe, light change, pan); frame=null.
  "gradual" = dissolve/fade; frame = its middle.
- missed_cuts: real cuts you can see in the strips that no event covers (only frames visible in a strip).
- shots: at most {MAX_SHOT_LABELS} entries: every S-id in the opening {window_s:.0f} s, then only
  shots where the type, subject or camera changes from the shot before. Per entry: type (wide/medium/close/insert/pov/drone/screen/archive/graphic/black),
  subject, camera move, framing quality, number of faces you see, on-screen text (verbatim, null if none), a short note.
- devices: every editing device you SEE, with its first and last frame. Vocabulary: card (full-screen text card),
  burn (light leak / flare carrying a cut), flash (1-2 bright frames on one shot), inset (picture boxed small on a
  plain surround), gate (picture inside a thin dark film-gate border), letterbox (black bars top and bottom, often
  nested inside a gate), typed_title / title (text over picture), scribble (hand-drawn strokes), shape_wipe
  (translucent flat shapes sliding over the picture), film_frame (a frame showing film-strip edges or a sepia/olive
  film flash), glitch, pip (picture-in-picture), overlay (cards, data, captions), speed_ramp, punch_in, split_screen.
  Look at EVERY event strip for these, including ones the detector called "not a cut".
- text_on_screen: verbatim text you can read, with its frames and role.
- sections: the structure (e.g. teaser, intro, a-roll, ...) with frame ranges and what each section is for.
- Keep every reason / detail / note under 25 words and the summary under 120 words.
- why_it_works: up to 6 concrete points on why this edit works (rhythm, contrast, signals), each with evidence.
- EVERY item cites "frames": frame numbers that are printed on tiles you looked at. Never cite a frame you were not
  shown; claims without visible evidence are discarded. Do not name real people. Be concrete and short.
"""


def _hash_files(paths):
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).name.encode())
        h.update(_sha256(p).encode())
    return h.hexdigest()


PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (5.0, 25.0)}   # $ per M tokens in / out (list)


def estimate_cost(n_images, model="", n_events=30, n_shots=MAX_SHOT_LABELS):
    """Rough $ for one brain call. Output dominates: ~90 tokens per event verdict, ~110 per shot label, plus
    devices / sections / notes and some thinking. Input: the images and prompt, re-read from cache each turn.
    An unknown model ("" = the agent's default) is priced as Opus, the expensive case."""
    m = (model or "").lower()
    pin, pout = next((v for k, v in PRICES.items() if k in m), PRICES["opus"])
    tin = n_images * EST_TOKENS_PER_IMAGE + 14000
    tout = 90 * n_events + 110 * min(n_shots, MAX_SHOT_LABELS) + 5000
    return round((tin * pin * 2.5 + tout * pout * 1.15) / 1e6, 3)


def _trim(obj, n=400):
    if isinstance(obj, str):
        return obj[:n]
    if isinstance(obj, list):
        return [_trim(x, n) for x in obj]
    if isinstance(obj, dict):
        return {k: _trim(v, 1500 if k == "summary" else n) for k, v in obj.items()}
    return obj


def salvage_structured(stream_path):
    """The last StructuredOutput the agent wrote in a Claude stream (a run can end on its budget or wall clock
    after the reply was written)."""
    last = None
    try:
        with open(stream_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                for c in (e.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "StructuredOutput":
                        last = c.get("input")
    except OSError:
        return None
    return last if isinstance(last, dict) else None


def check_citations(result, shown_frames, events_shown, shots_shown, n_frames):
    """Drop (and list) every claim that cites no frame the model was shown. Returns (clean, dropped)."""
    shown = set(int(f) for f in shown_frames)
    dropped = []

    def ok_frames(fr):
        return [int(f) for f in fr or [] if int(f) in shown]
    result = _trim(result)
    clean = {k: result.get(k) for k in ("summary",)}
    ev = []
    for e in result.get("events") or []:
        vis = set(events_shown.get(e.get("id"), []))
        good = [f for f in ok_frames(e.get("frames")) if f in vis or not vis]
        if not vis or not good:
            dropped.append({"where": "events", "item": e, "why": "unknown event id or no visible evidence"})
            continue
        if e["verdict"] in ("cut", "gradual") and (e.get("frame") is None or int(e["frame"]) not in vis):
            dropped.append({"where": "events", "item": e, "why": "cut frame not inside its strip"})
            continue
        ev.append({**e, "frames": good})
    clean["events"] = ev
    mc = []
    for m in result.get("missed_cuts") or []:
        good = ok_frames(m.get("frames"))
        if int(m["frame"]) in shown and good:
            mc.append({**m, "frames": good})
        else:
            dropped.append({"where": "missed_cuts", "item": m, "why": "frame not shown"})
    clean["missed_cuts"] = mc
    sh = []
    for s in result.get("shots") or []:
        vis = set(shots_shown.get(s.get("id"), []))
        good = ok_frames(s.get("frames"))
        if s.get("id") not in shots_shown or not good:
            dropped.append({"where": "shots", "item": s, "why": "unknown shot id or no visible evidence"})
            continue
        sh.append({**s, "frames": good, "_mid": sorted(vis)[0] if vis else None})
    clean["shots"] = sh
    for key in ("devices", "text_on_screen", "sections", "why_it_works"):
        items = []
        for d in result.get(key) or []:
            good = ok_frames(d.get("frames"))
            if not good:
                dropped.append({"where": key, "item": d, "why": "no visible evidence"})
                continue
            if "f0" in d and (int(d["f0"]) > int(d["f1"]) or int(d["f1"]) > n_frames):
                dropped.append({"where": key, "item": d, "why": "bad frame range"})
                continue
            items.append({**d, "frames": good})
        clean[key] = items
    return clean, dropped


def final_cuts(detector_cuts, events, clean):
    """Detectors propose, the AI decides: start from the detector cuts, apply each verdict, add confirmed misses."""
    cuts = set(int(c) for c in detector_cuts)
    by_id = {e["id"]: e for e in events}
    changes = []
    for v in clean.get("events") or []:
        e = by_id.get(v["id"])
        if not e:
            continue
        f = int(e["frame"])
        near = [c for c in cuts if abs(c - f) <= 1]
        if v["verdict"] == "not_a_cut":
            for c in near:
                cuts.discard(c)
                changes.append({"frame": c, "change": "removed", "why": v["reason"]})
        else:
            new = int(v["frame"])
            for c in near:
                if c != new:
                    cuts.discard(c)
            if new not in cuts:
                cuts.add(new)
                changes.append({"frame": new, "change": "moved" if near else "added", "why": v["reason"]})
    for m in clean.get("missed_cuts") or []:
        f = int(m["frame"])
        if not any(abs(c - f) <= 1 for c in cuts):
            cuts.add(f)
            changes.append({"frame": f, "change": "added", "why": m["reason"]})
    return sorted(c for c in cuts if c > 0), changes


def _cache_dir(cfg):
    return Path(cfg.work) / "cache" / "vision"


def brain_reference(cfg, rid, budget_usd=None, window_s=BRAIN_WINDOW_S, agent=None, model=None, force=False,
                    echo=print, runner=None):
    """Run the AI brain pass on an analysed reference. Writes refs/<id>/brain.json and returns it.
    `runner` (tests) replaces agent.run_role: runner(prompt, schema=..., images=..., max_budget_usd=...) -> dict."""
    from . import agent as agentmod
    from . import governor
    from . import reference as R
    rid = R.resolve_id(cfg, rid)
    d = R.ref_dir(cfg, rid)
    an = R.load_analysis(cfg, rid)
    budget = float(budget_usd if budget_usd is not None else getattr(cfg, "vision_ref_budget_usd", DEFAULT_BUDGET_USD))
    fps = float(an["source"]["fps"])
    n = int(an["source"]["frames"])
    npz = d / "work" / "scan.npz"
    arr = dict(np.load(npz)) if npz.exists() else None
    video = d / "proxy.mp4"
    if not video.exists():
        video = R.source_path(cfg, rid)
    bd = d / "work" / "brain"
    bd.mkdir(parents=True, exist_ok=True)
    win = min(float(window_s), n / fps)
    events = reference_events(an, arr, fps, win)
    frames = decode_window(cfg, video, win + 0.5)
    for old in bd.glob("*.jpg"):
        old.unlink()
    ev_paths, ev_shown = event_strip_images(frames, events, bd)
    sh_paths, sh_shown = shot_sheet_images(d, an.get("shots", []), bd)
    images = ev_paths + sh_paths
    # image budget: drop shot sheets from the end (late, long a-roll) until the estimate fits
    model = model if model is not None else (getattr(cfg, "vision_model", "") or DEFAULT_MODEL)
    while len(images) > len(ev_paths) and estimate_cost(len(images), model, len(events)) > budget * 0.7:
        images.pop()
    kept_sheets = set(p.name for p in images)
    per = SHOT_COLS * SHOT_ROWS
    shots_shown = {k: v for pos, (k, v) in enumerate(sh_shown.items()) if sh_paths[pos // per].name in kept_sheets}
    prompt = build_reference_prompt(an, events, ev_shown, shots_shown, images, fps, win)
    ag = agent if isinstance(agent, agentmod._Agent) else (agentmod.get(agent) if agent in agentmod.AGENTS
                                                           else agentmod.choose(cfg))
    key = hashlib.sha256(json.dumps([prompt.replace(str(Path(cfg.work).resolve()), "<work>"), BRAIN_SCHEMA,
                                     _hash_files(images), ag.name, model], sort_keys=True).encode()).hexdigest()[:20]
    cdir = _cache_dir(cfg)
    cdir.mkdir(parents=True, exist_ok=True)
    cpath = cdir / f"ref_{key}.json"
    shown_frames = sorted({f for v in ev_shown.values() for f in v} | {f for v in shots_shown.values() for f in v})
    if cpath.exists() and not force:
        res = json.loads(cpath.read_text())
        cost, cached = 0.0, True
        raw = res.get("raw")
        meta = res.get("meta", {})
    else:
        est = estimate_cost(len(images), model, len(events), len(shots_shown))
        echo(f"vision: {len(events)} events, {len(ev_paths)} strip images + {len(images) - len(ev_paths)} shot sheets "
             f"to {ag.name} (estimated ${est:.2f}, cap ${budget:.2f})")
        if est > budget:
            raise RuntimeError(f"brain pass estimated at ${est:.2f}, over the ${budget:.2f} budget "
                               f"(raise vision_ref_budget_usd or --budget)")
        call = runner or (lambda p, **kw: agentmod.run_role("vision", p, cfg=cfg, agent=ag, tools=("Read",),
                                                             gov=governor.for_cfg(cfg), out_dir=bd / "run",
                                                             timeout=1500, model=model, **kw))
        r = call(prompt, schema=BRAIN_SCHEMA, images=[str(p) for p in images], max_budget_usd=budget)
        raw = r.get("result_json")
        if not isinstance(raw, dict) and r.get("stream_path"):
            raw = salvage_structured(r["stream_path"])       # paid for: use it if it validates
            if raw is not None:
                echo(f"vision: the run ended early ({r.get('error')}); using the reply it had already written")
        cost = float(r.get("cost_usd") or 0.0)
        cached = False
        meta = {"agent": ag.name, "model": model, "cost_usd": cost, "seconds": r.get("seconds"), "error": r.get("error")}
        if not isinstance(raw, dict):
            raise RuntimeError(f"brain pass failed: {r.get('error') or 'no result'} (cost ${cost:.3f})")
        errs = schema_errors(raw, BRAIN_SCHEMA)
        if errs:
            meta["schema_errors"] = errs[:20]
            raise RuntimeError("brain reply failed its schema: " + "; ".join(errs[:5]))
        cpath.write_text(json.dumps({"raw": raw, "meta": meta}, indent=1))
    clean, dropped = check_citations(raw, shown_frames, ev_shown, shots_shown, n)
    shown_cuts = [int(s["f0"]) for s in an.get("shots", [])[1:]]
    det_cuts = [int(c) for c in (an.get("shot_detector") or {}).get("cuts") or shown_cuts]
    cuts, changes = final_cuts(shown_cuts, events, clean)
    shot_map = {f"S{s['i']}": s for s in an.get("shots", [])}
    for s in clean["shots"]:
        ds = shot_map.get(s["id"])
        if ds:
            s["f0"], s["f1"] = int(ds["f0"]), int(ds["f1"])
    brain = {"version": 1, "id": rid, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "window_s": win, "fps": fps,
             "agent": meta.get("agent", ag.name), "model": meta.get("model", model), "cost_usd": round(cost, 4),
             "cached": cached, "cache_key": key, "images": [p.name for p in images], "n_events": len(events),
             "events": [{k: e[k] for k in ("id", "frame", "kinds", "label")} for e in events],
             "verdicts": clean["events"], "missed_cuts": clean["missed_cuts"], "cuts": cuts, "cut_changes": changes,
             "detector_cuts": det_cuts,
             "shots": clean["shots"], "devices": clean["devices"], "text_on_screen": clean["text_on_screen"],
             "sections": clean["sections"], "why_it_works": clean["why_it_works"], "summary": clean.get("summary") or "",
             "dropped": dropped, "budget_usd": budget}
    (d / "brain.json").write_text(json.dumps(brain, indent=1, ensure_ascii=False))
    return brain


def deep_analyze(cfg, rid, budget_usd=None, agent=None, model=None, force=False, echo=print, runner=None):
    """`cutroom ref analyze --deep`: detectors, then the brain pass, then the analysis rebuilt on the AI's cuts
    with its breakdown merged in. Returns (analysis, brain)."""
    from . import reference as R
    rid = R.resolve_id(cfg, rid)
    an = R.analyze(cfg, rid, echo=echo)
    brain = brain_reference(cfg, rid, budget_usd=budget_usd, agent=agent, model=model, force=force, echo=echo,
                            runner=runner)
    cur = [int(s["f0"]) for s in an["shots"][1:]]
    if brain["cuts"] != cur:
        echo(f"vision: rebuilding the cut map on the reviewed cuts ({len(brain['cut_changes'])} changes)")
        an = R.analyze(cfg, rid, echo=echo, cuts_override=brain["cuts"])
    else:
        merge_brain(an, brain)
        an["summary"] = R.summary_lines(an)
        R._jsave(R.ref_dir(cfg, rid) / "analysis.json", an)
    return an, brain


def merge_brain(an, brain):
    """Fold a brain pass into an analysis (in place): per-shot AI labels by frame overlap, the AI devices on
    the shots they touch, and an["brain"] (the breakdown the studio and compare read)."""
    if not brain:
        return an
    labels = brain.get("shots") or []
    for s in an.get("shots", []):
        best, ov = None, 0
        for b in labels:
            o = min(s["f1"], b.get("f1", 0)) - max(s["f0"], b.get("f0", 0))
            if o > ov:
                best, ov = b, o
        if best and ov >= 0.5 * (s["f1"] - s["f0"]):
            s["ai"] = {k: best.get(k) for k in ("type", "subject", "camera", "framing", "faces", "text", "note")}
        else:
            s.pop("ai", None)
        tags = set(s.get("devices") or [])
        for dv in brain.get("devices") or []:
            if min(s["f1"], dv["f1"] + 1) - max(s["f0"], dv["f0"]) > 0:
                tags.add(dv["kind"])
        s["devices"] = sorted(tags)
    an["brain"] = {k: brain.get(k) for k in ("version", "created", "agent", "model", "cost_usd", "cached", "window_s",
                                              "cuts", "cut_changes", "devices", "text_on_screen", "sections",
                                              "why_it_works", "summary", "verdicts", "missed_cuts")}
    an["brain"]["n_dropped"] = len(brain.get("dropped") or [])
    return an


def load_brain(cfg, rid):
    from . import reference as R
    p = R.ref_dir(cfg, R.resolve_id(cfg, rid)) / "brain.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def brain_lines(brain) -> list:
    """Plain-language summary lines of a brain pass."""
    if not brain:
        return []
    fps = float(brain.get("fps") or 25)
    out = [f"AI breakdown ({brain.get('agent')}, ${brain.get('cost_usd', 0):.2f}{', cached' if brain.get('cached') else ''}): "
           f"{len(brain.get('cuts') or [])} cuts after review, {len(brain.get('cut_changes') or [])} detector cuts changed"]
    if brain.get("summary"):
        out.append("  " + brain["summary"])
    for d in brain.get("devices") or []:
        out.append(f"  device {d['kind']} {d['f0'] / fps:.2f}-{(d['f1'] + 1) / fps:.2f}s: {d['detail']}")
    for t in brain.get("text_on_screen") or []:
        out.append(f"  text \"{t['text']}\" ({t['role']}) {t['f0'] / fps:.2f}s")
    for w in brain.get("why_it_works") or []:
        out.append(f"  why: {w['point']}")
    return out


# ==================================================================================================
# library clips: detector numbers for the tagging prompt + citation checks
# ==================================================================================================
CLIP_FACE_SAMPLES = 6


def clip_face_samples(cfg, proxy, duration, n=CLIP_FACE_SAMPLES):
    """YuNet faces on n evenly spaced proxy frames: [{t, count, size, position}]."""
    import cv2
    out = []
    dur = float(duration or 0)
    if not proxy or not Path(proxy).exists() or dur <= 0:
        return out
    for k in range(n):
        t = round(dur * (k + 0.5) / n, 2)
        cmd = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(proxy),
               "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
        r = subprocess.run(cmd, capture_output=True, check=False)
        img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR) if r.stdout else None
        if img is None:
            continue
        fs = face_summary(detect_faces(img))
        out.append({"t": t, **{k_: fs[k_] for k_ in ("count", "size", "position")}})
    return out


def clip_context(cfg, con, row) -> dict:
    """Detector numbers for one library clip (cached in the vision_clips table by proxy signature)."""
    ensure_schema(con)
    proxy = row["proxy"] if "proxy" in row.keys() else None
    sig = None
    if proxy and Path(proxy).exists():
        st = Path(proxy).stat()
        sig = f"{st.st_size}:{st.st_mtime_ns}:{face_detector().backend}"
    got = con.execute("SELECT sig, data FROM vision_clips WHERE clip_id=?", (row["id"],)).fetchone()
    if got and got["sig"] == sig and sig:
        return json.loads(got["data"])
    faces = clip_face_samples(cfg, proxy, row["duration"])
    cuts = [round(float(r["start"]), 2) for r in
            con.execute("SELECT start FROM shots WHERE clip_id=? AND start > 0.3 ORDER BY start", (row["id"],))]
    ctx = {"faces": faces, "face_backend": face_detector().backend, "cuts": cuts,
           "max_faces": max([f["count"] for f in faces] or [0]),
           "max_face_size": max([f["size"] for f in faces] or [0.0])}
    con.execute("INSERT OR REPLACE INTO vision_clips (clip_id, sig, data, updated) VALUES (?,?,?,?)",
                (row["id"], sig, json.dumps(ctx), time.time()))
    con.commit()
    return ctx


VISION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vision_clips (clip_id TEXT PRIMARY KEY, sig TEXT, data TEXT, updated REAL);
"""


def ensure_schema(con):
    con.executescript(VISION_SCHEMA_SQL)


def context_line(ctx) -> str:
    """One prompt line with the detector numbers for a clip."""
    if not ctx:
        return ""
    fs = ctx.get("faces") or []
    parts = [f"{f['t']:.1f}s: {f['count']} face{'s' if f['count'] != 1 else ''}"
             + (f" (largest {f['size'] * 100:.0f}% of height, {f['position']})" if f["count"] else "") for f in fs]
    s = f"faces ({ctx.get('face_backend')}): " + ("; ".join(parts) if parts else "not measured")
    if ctx.get("cuts"):
        s += f" · in-clip cut candidates at {', '.join(f'{c:.1f}s' for c in ctx['cuts'][:8])}s (confirm or ignore)"
    return s


def check_clip_citations(tag, duration, every, max_tiles, extra_times=()) -> dict:
    """Keep the vision fields of a tag only when `seen_at` cites times shown on the contact sheet (or the
    detector sample times). Returns the tag with unverified vision fields removed."""
    from .ingest import sheet_times
    shown = list(sheet_times(float(duration or 0), every, max_tiles)) + [float(x) for x in extra_times]
    seen = [float(x) for x in tag.get("seen_at") or [] if isinstance(x, (int, float))]
    good = [t for t in seen if any(abs(t - s) <= max(0.6, every * 0.26) for s in shown)]
    out = dict(tag)
    if good:
        out["seen_at"] = [round(t, 2) for t in good[:8]]
    else:
        out.pop("seen_at", None)
        for k in VISION_TAG_FIELDS:
            out.pop(k, None)
    return out


VISION_TAG_FIELDS = ("subject", "framing", "camera_move", "faces", "text_on_screen")
