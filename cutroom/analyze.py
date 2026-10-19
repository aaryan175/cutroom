"""Per-clip analysis on the proxy: shot boundaries + 2-second quality windows.

One ffmpeg decode pass per clip (rawvideo pipe, 12.5 fps, 480 px wide) feeds:
  shots      PySceneDetect ContentDetector (on a 240 px copy)
  shake      LK optical flow -> RANSAC similarity transform -> camera translation (dx, dy) per frame;
             shake = mean |change of camera velocity| (px @ 480w), motion = mean |velocity|
  exposure   mean luma, contrast = luma std, sharpness = variance of Laplacian
  faces      max Haar-cascade frontal faces, sampled every ~0.5 s
"""
import json
import math
import multiprocessing
import os
import re
import statistics
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np

from . import db, governor

FPS = 12.5
WIDTH = 480
WINDOW = 2.0
FACE_EVERY = 6                   # frames (~0.48 s at 12.5 fps)
LOG_SAT_MAX = 45.0               # mean HSV saturation (0-255) below which 10-bit footage is treated as log
LOG_CONTRAST_MAX = 50.0

PHASH_N = 5                      # perceptual hashes per clip (10/30/50/70/90 % of the clip)
_cascade = None
_smile = None


def _face_cascade():
    """OpenCV's bundled frontal-face Haar cascade, or None (OpenCV 5 no longer ships it)."""
    global _cascade
    if _cascade is None:
        import cv2
        try:
            _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if _cascade.empty():
                _cascade = False
        except AttributeError:
            _cascade = False
    return _cascade or None


def _smile_cascade():
    global _smile
    if _smile is None:
        import cv2
        try:
            _smile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_smile.xml")
            if _smile.empty():
                _smile = False
        except AttributeError:
            _smile = False
    return _smile or None


def _has_smile(smile_c, grey, face) -> bool:
    """Haar smile cascade on the lower half of a face box (a cheap expression hint, not a classifier)."""
    x, y, w, h = (int(v) for v in face)
    roi = grey[y + h // 2: y + h, x: x + w]
    if roi.size == 0 or w < 24:
        return False
    found = smile_c.detectMultiScale(roi, scaleFactor=1.6, minNeighbors=20, minSize=(max(8, w // 4), max(6, h // 10)))
    return len(found) > 0


def phash_hex(grey) -> str | None:
    """64-bit DCT perceptual hash of a greyscale frame as 16 hex chars; None for flat frames (black,
    lens cap, white) because every flat frame hashes alike."""
    import cv2
    small = cv2.resize(grey, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    if float(small.std()) < 4.0:
        return None
    d = cv2.dct(small)[:8, :8].flatten()
    bits = d > np.median(d[1:])
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return f"{v:016x}"


def _cut_detector():
    from scenedetect import ContentDetector
    return ContentDetector(threshold=27.0, min_scene_len=int(FPS * 0.6))


def _feed_detector(det, i, frame):
    """Feed one frame; returns cut times in seconds. Works with timecode- and int-based APIs."""
    from scenedetect.common import FrameTimecode
    try:
        cuts = det.process_frame(FrameTimecode(i, fps=FPS), frame)
    except TypeError:
        cuts = det.process_frame(i, frame)
    return [_tc_seconds(c) for c in cuts]


def _tc_seconds(c) -> float:
    if isinstance(c, (int, np.integer)):
        return int(c) / FPS
    s = getattr(c, "seconds", None)
    return float(s if s is not None else c.get_seconds())


def _flush_detector(det, i):
    try:
        from scenedetect.common import FrameTimecode
        return [_tc_seconds(c) for c in (det.post_process(FrameTimecode(i, fps=FPS)) or [])]
    except Exception:  # noqa: BLE001 - older/newer APIs differ; trailing cuts are a nicety
        return []


def _camera_shift(cv2, prev, cur, pts):
    """Global translation (dx, dy) from prev to cur plus the tracked points to reuse next frame.
    Translation is None when it can't be trusted (cut, flat frame, too few inliers)."""
    if pts is None or len(pts) < 40:
        pts = cv2.goodFeaturesToTrack(prev, maxCorners=120, qualityLevel=0.01, minDistance=12, blockSize=7)
    if pts is None or len(pts) < 12:
        return None, None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, winSize=(21, 21), maxLevel=3)
    ok = st.reshape(-1) == 1
    if ok.sum() < 10:
        return None, None
    m, inl = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if m is None or inl is None or inl.sum() < max(8, 0.3 * ok.sum()):
        return None, None
    keep = nxt[ok][inl.reshape(-1) == 1].reshape(-1, 1, 2)     # track inliers (background) onward
    return (float(m[0, 2]), float(m[1, 2])), keep


def analyze_proxy(ffmpeg_bin: str, proxy: str, duration: float, ffprobe_bin: str = "ffprobe") -> dict:
    """Decode the proxy once and measure everything. Pure function (runs in a worker process)."""
    import cv2
    cv2.setNumThreads(1)
    t_start = time.time()
    probe = subprocess.run([ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", proxy], capture_output=True, text=True)
    pw, ph = (int(x) for x in probe.stdout.strip().split(",")[:2])
    w = WIDTH
    h = max(2, int(round(w * ph / pw / 2)) * 2)
    sw, sh = w // 2, max(2, h // 2)
    cmd = [ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", proxy, "-an",
           "-vf", f"fps={FPS},scale={w}:{h}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=w * h * 3 * 4)
    fsize = w * h * 3
    cascade = _face_cascade()
    smile_c = _smile_cascade() if cascade is not None else None
    det = _cut_detector()
    n_est = max(1, int(round((duration or 0) * FPS)))
    hash_at = {int((k + 0.5) / PHASH_N * n_est): k for k in range(PHASH_N)} if duration else {}
    hashes = [None] * PHASH_N
    min_face = max(20, int(min(w, h) * 0.07))

    cuts, wins = [], {}
    prev_grey, prev_v, pts = None, (0.0, 0.0), None
    sats = []
    i = 0
    while True:
        buf = proc.stdout.read(fsize)
        if len(buf) < fsize:
            break
        if i % 250 == 0:
            try:
                governor.worker_check("analysis worker")     # no-op outside a governed pool
            except MemoryError:
                proc.kill()
                proc.wait()
                raise
        frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        t = i / FPS
        k = int(t // WINDOW)
        win = wins.setdefault(k, {"shake": [], "motion": [], "luma": [], "std": [], "sharp": [], "faces": 0,
                                  "face_n": 0, "smile_n": 0})
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if i in hash_at:
            hashes[hash_at[i]] = phash_hex(grey)
        small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        cuts += _feed_detector(det, i, small)
        mean, std = cv2.meanStdDev(grey)
        win["luma"].append(float(mean[0, 0]))
        win["std"].append(float(std[0, 0]))
        if i % 3 == 0:
            win["sharp"].append(float(cv2.Laplacian(grey, cv2.CV_32F).var()))
        if i % 12 == 0:
            sats.append(float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 1].mean()))
        if cascade is not None and i % FACE_EVERY == 0:
            faces = cascade.detectMultiScale(grey, scaleFactor=1.2, minNeighbors=5, minSize=(min_face, min_face))
            win["faces"] = max(win["faces"], len(faces))
            if len(faces) and smile_c is not None:
                win["face_n"] += 1
                win["smile_n"] += int(_has_smile(smile_c, grey, max(faces, key=lambda f: f[2] * f[3])))
        if prev_grey is not None:
            if i % 10 == 0:
                pts = None                                  # refresh features regularly
            v, pts = _camera_shift(cv2, prev_grey, grey, pts)
            if v is not None:          # untrusted pairs (cuts, flat frames) keep the previous velocity
                win["motion"].append(float(np.hypot(*v)))
                win["shake"].append(float(np.hypot(v[0] - prev_v[0], v[1] - prev_v[1])))
                prev_v = v
        prev_grey = grey
        i += 1
    proc.stdout.close()
    proc.wait()
    cuts += _flush_detector(det, i)
    if i == 0:
        raise RuntimeError("proxy decoded to zero frames")
    dur = duration or i / FPS

    metrics = []
    for k in sorted(wins):
        x = wins[k]
        t0 = k * WINDOW
        mean = lambda a: float(np.mean(a)) if a else None   # noqa: E731
        metrics.append({"t0": round(t0, 3), "t1": round(min(t0 + WINDOW, dur), 3),
                        "shake": mean(x["shake"]) if x["shake"] else 0.0,
                        "motion": mean(x["motion"]) if x["motion"] else 0.0,
                        "exposure": mean(x["luma"]), "contrast": mean(x["std"]),
                        "sharpness": mean(x["sharp"]) if x["sharp"] else 0.0, "faces": int(x["faces"]),
                        "smile": round(x["smile_n"] / x["face_n"], 3) if x["face_n"] else None})
    bounds = [0.0] + sorted({round(c, 3) for c in cuts if 0.3 < c < dur - 0.3}) + [round(dur, 3)]
    shots = [{"idx": j, "start": bounds[j], "end": bounds[j + 1]} for j in range(len(bounds) - 1)]
    return {"metrics": metrics, "shots": shots, "saturation": float(np.mean(sats)) if sats else None,
            "frames": i, "seconds": time.time() - t_start, "phash": hashes if any(hashes) else None}


def _is_high_bit(pix_fmt) -> bool:
    p = (pix_fmt or "").lower()
    return any(b in p for b in ("10le", "10be", "12le", "12be", "p010", "p210", "p410"))


def _store(con, cid, res, row):
    con.execute("DELETE FROM shots WHERE clip_id=?", (cid,))
    con.execute("DELETE FROM metrics WHERE clip_id=?", (cid,))
    con.executemany("INSERT INTO shots (clip_id, idx, start, end) VALUES (?,?,?,?)",
                    [(cid, s["idx"], s["start"], s["end"]) for s in res["shots"]])
    con.executemany("INSERT INTO metrics (clip_id, t0, t1, shake, motion, exposure, contrast, sharpness, faces) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [(cid, m["t0"], m["t1"], m["shake"], m["motion"], m["exposure"], m["contrast"],
                      m["sharpness"], m["faces"]) for m in res["metrics"]])
    ensure_scale_schema(con)
    if res.get("phash"):
        con.execute("INSERT OR REPLACE INTO phash VALUES (?,?,?,?)", (cid, json.dumps(res["phash"]), "analyze", time.time()))
    con.execute("DELETE FROM win_extra WHERE clip_id=?", (cid,))
    con.executemany("INSERT INTO win_extra (clip_id, t0, smile) VALUES (?,?,?)",
                    [(cid, m["t0"], m.get("smile")) for m in res["metrics"] if m.get("smile") is not None])
    is_log = row["is_log"]
    # 10-bit footage that is flat and nearly colourless is almost always a log profile
    if not is_log and _is_high_bit(row["pix_fmt"]) and res["saturation"] is not None:
        med_contrast = statistics.median([m["contrast"] for m in res["metrics"]] or [99])
        if res["saturation"] < LOG_SAT_MAX and med_contrast < LOG_CONTRAST_MAX:
            is_log = 1
    con.execute("UPDATE clips SET status='analyzed', is_log=?, error=NULL, updated=? WHERE id=?",
                (is_log, time.time(), cid))


def analyze_all(cfg, con, limit=None, max_jobs=None, ids=None):
    """Shots + quality metrics for every proxied clip that has not been analyzed yet (`ids` = only these)."""
    rows = con.execute("SELECT * FROM clips WHERE status='proxied' AND proxy IS NOT NULL ORDER BY rel").fetchall()
    if ids is not None:
        rows = [r for r in rows if r["id"] in ids]
    if limit:
        rows = rows[:limit]
    if not rows:
        print("analyze: nothing to do", flush=True)
        return
    if _face_cascade() is None:
        print("  ! no Haar face cascade in this OpenCV build (install opencv-python-headless<5); faces = 0", flush=True)
    gov = governor.for_cfg(cfg)
    n = max(1, min(cfg.n_workers(), max_jobs) if max_jobs else cfg.n_workers())
    n = min(n, gov.heavy_slots)
    t = time.time()
    total = sum(r["duration"] or 0 for r in rows)
    ok = err = 0
    with make_pool(cfg, n) as ex, ThreadPoolExecutor(n) as tex:
        futs = {tex.submit(analyze_one, cfg, ex, r): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            try:
                res = f.result()
                _store(con, r["id"], res, r)
                ok += 1
            except BrokenProcessPool as e:            # a worker died (e.g. killed): retry next run
                print(f"  ! {r['rel']}: analysis worker died ({e}); left for the next run", flush=True)
                err += 1
            except Exception as e:  # noqa: BLE001
                con.execute("UPDATE clips SET status='error', error=?, updated=? WHERE id=?",
                            (f"analyze: {str(e)[-300:]}", time.time(), r["id"]))
                err += 1
            con.commit()
            if i % 20 == 0:
                print(f"  analyze {i}/{len(rows)}", flush=True)
    dt = time.time() - t
    print(f"analyze: {ok} ok · {err} errors · {dt:.1f}s for {total / 60:.1f} min of footage "
          f"({total / max(dt, 1e-6):.0f}x realtime with {n} workers)", flush=True)


def make_pool(cfg, n):
    """Process pool for analyze_proxy: spawned (never forked, so no slot lock is inherited), niced,
    with the governor's per-process memory cap checked inside each worker."""
    gov = governor.for_cfg(cfg)
    return ProcessPoolExecutor(max(1, n), mp_context=multiprocessing.get_context("spawn"),
                               initializer=governor.worker_init,
                               initargs=(gov.nice if gov.enabled else 0, gov.max_proc_mem_gb if gov.enabled else None))


def analyze_one(cfg, ex, row) -> dict:
    """Analyze one clip in pool `ex` while holding a heavy governor slot. Returns analyze_proxy's dict."""
    gov = governor.for_cfg(cfg)
    with gov.slot("heavy", label=f"analyze {row['id']}"):
        return ex.submit(analyze_proxy, cfg.ffmpeg, row["proxy"], row["duration"] or 0, cfg.ffprobe).result()


# -- queries other modules use ---------------------------------------------------------------

def _med(vals):
    vals = [v for v in vals if v is not None]
    return float(statistics.median(vals)) if vals else None


def clip_quality(con, cid) -> dict:
    """Clip-level summary: median shake/motion/exposure/contrast, min sharpness, max faces."""
    rows = con.execute("SELECT * FROM metrics WHERE clip_id=? ORDER BY t0", (cid,)).fetchall()
    if not rows:
        return {}
    return {"shake": _med([r["shake"] for r in rows]), "motion": _med([r["motion"] for r in rows]),
            "exposure": _med([r["exposure"] for r in rows]), "contrast": _med([r["contrast"] for r in rows]),
            "sharpness": min(r["sharpness"] for r in rows), "faces": max(r["faces"] for r in rows),
            "windows": len(rows)}


def window_quality(con, cid, t0, t1) -> dict:
    """Summary of the 2 s windows overlapping [t0, t1): mean/max shake, mean exposure, min sharpness, max faces."""
    rows = con.execute("SELECT * FROM metrics WHERE clip_id=? AND t1>? AND t0<? ORDER BY t0",
                       (cid, t0, t1)).fetchall()
    if not rows:
        return {}
    return {"shake": float(np.mean([r["shake"] for r in rows])), "shake_max": max(r["shake"] for r in rows),
            "motion": float(np.mean([r["motion"] for r in rows])),
            "exposure": float(np.mean([r["exposure"] for r in rows])),
            "contrast": float(np.mean([r["contrast"] for r in rows])),
            "sharpness": min(r["sharpness"] for r in rows), "faces": max(r["faces"] for r in rows),
            "windows": len(rows)}


def _exposure_penalty(e) -> float:
    if e is None:
        return 0.0
    return max(0.0, 45 - e) / 15 + max(0.0, e - 210) / 15     # too dark / blown out


def steadiest_window(con, cid, dur) -> tuple:
    """(t0, t1) of length `dur` with the least shake and sane exposure."""
    c = con.execute("SELECT duration FROM clips WHERE id=?", (cid,)).fetchone()
    total = float(c["duration"] or 0) if c else 0.0
    if total <= dur or total <= 0:
        return (0.0, round(total, 3))
    rows = con.execute("SELECT t0, t1, shake, exposure FROM metrics WHERE clip_id=? ORDER BY t0", (cid,)).fetchall()
    if not rows:
        return (0.0, round(dur, 3))
    starts = sorted({min(r["t0"], total - dur) for r in rows} | {max(0.0, total - dur)})
    best, best_s = None, 0.0
    for s in starts:
        ws = [r for r in rows if r["t1"] > s and r["t0"] < s + dur]
        if not ws:
            continue
        score = (float(np.mean([r["shake"] for r in ws])) + 0.5 * max(r["shake"] for r in ws)
                 + float(np.mean([_exposure_penalty(r["exposure"]) for r in ws])))
        if best is None or score < best:
            best, best_s = score, s
    return (round(best_s, 3), round(best_s + dur, 3))


def auto_quality(q: dict) -> float:
    """Rough 1-5 usability from metrics (used when the operator hasn't tagged `quality`)."""
    if not q:
        return 0.0
    s = 5.0
    s -= min(2.0, max(0.0, (q.get("shake") or 0) - 3) / 3)
    s -= min(1.5, _exposure_penalty(q.get("exposure")))
    if (q.get("sharpness") or 0) < 20:
        s -= 1.0
    return round(max(1.0, s), 1)


# ==================================================================================================
# Scale detectors: cheap per-library passes that make a 4 TB library searchable and cheaper to tag.
#   dedupe      exact copies (same bytes) + near-duplicates (perceptual hashes of 5 keyframes)
#   highlights  per-2 s window score from audio peaks (applause / laughter / crowd energy), camera
#               motion peaks and faces (+ a smile hint)
#   sessions    capture-time "shoots" and GPS places
#   takes       best take among repeated lines (the later take wins unless it is clearly worse)
# All results live in their own tables (SCALE_SCHEMA); every pass is cheap and safe to re-run.
# People clustering (face embeddings) is not done: it needs an embedding model; see docs/OVERNIGHT.md.
# ==================================================================================================

SCALE_SCHEMA = """
CREATE TABLE IF NOT EXISTS phash (clip_id TEXT PRIMARY KEY, hashes TEXT, src TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS win_extra (clip_id TEXT, t0 REAL, smile REAL, PRIMARY KEY (clip_id, t0));
CREATE TABLE IF NOT EXISTS filesig (clip_id TEXT PRIMARY KEY, size INTEGER, mtime REAL, sig TEXT);
CREATE TABLE IF NOT EXISTS dupes (clip_id TEXT PRIMARY KEY, grp TEXT, rep TEXT, kind TEXT, dist REAL, updated REAL);
CREATE INDEX IF NOT EXISTS dupes_grp ON dupes(grp);
CREATE TABLE IF NOT EXISTS highlights (clip_id TEXT, t0 REAL, t1 REAL, score REAL, audio REAL, motion REAL,
  faces REAL, kind TEXT, PRIMARY KEY (clip_id, t0));
CREATE TABLE IF NOT EXISTS highlight_peaks (clip_id TEXT PRIMARY KEY, score REAL, t REAL, kind TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS clip_geo (clip_id TEXT PRIMARY KEY, lat REAL, lon REAL, alt REAL, ts REAL, ts_src TEXT,
  source TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS sessions (clip_id TEXT PRIMARY KEY, session INTEGER, place INTEGER, ts REAL, updated REAL);
CREATE INDEX IF NOT EXISTS sessions_session ON sessions(session);
CREATE TABLE IF NOT EXISTS takes (grp INTEGER, clip_id TEXT, t0 REAL, t1 REAL, text TEXT, rank INTEGER, score REAL,
  best INTEGER, PRIMARY KEY (clip_id, t0));
CREATE INDEX IF NOT EXISTS takes_grp ON takes(grp);
CREATE TABLE IF NOT EXISTS tag_gate (clip_id TEXT PRIMARY KEY, reason TEXT, updated REAL);
"""


def ensure_scale_schema(con):
    con.executescript(SCALE_SCHEMA)


def _say(echo, msg):
    if echo:
        echo(msg)


# -- perceptual hashes + duplicates ----------------------------------------------------------------

def hamming(a, b) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def phash_dist(ha, hb) -> float | None:
    """Mean Hamming distance (0-64) between two clips' keyframe hashes. Each keyframe is compared with
    the same relative position in the other clip and its neighbours (different lengths, small offsets).
    None when either clip has fewer than 3 usable (non-flat) keyframes."""
    if not ha or not hb:
        return None
    ds = []
    for i, a in enumerate(ha):
        if a is None:
            continue
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(hb) and hb[j] is not None:
                d = hamming(a, hb[j])
                best = d if best is None else min(best, d)
        if best is not None:
            ds.append(best)
    if len(ds) < 3:
        return None
    return float(np.mean(ds))


_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _hash_matrix(hash_lists) -> tuple:
    """(H uint64 n x PHASH_N, M bool mask of usable hashes) from lists of hex hashes / None."""
    n = len(hash_lists)
    H = np.zeros((n, PHASH_N), dtype=np.uint64)
    M = np.zeros((n, PHASH_N), dtype=bool)
    for r, hs in enumerate(hash_lists):
        for k, h in enumerate((hs or [])[:PHASH_N]):
            if h is not None:
                H[r, k] = np.uint64(int(h, 16))
                M[r, k] = True
    return H, M


def dist_block(Ha, Ma, Hb, Mb) -> np.ndarray:
    """phash_dist for every pair of rows of (Ha, Ma) x (Hb, Mb), vectorised; inf where < 3 usable keyframes."""
    a, b = len(Ha), len(Hb)
    D = np.full((a, b, PHASH_N), np.inf, dtype=np.float32)
    for i in range(PHASH_N):
        for j in (i - 1, i, i + 1):
            if not 0 <= j < PHASH_N:
                continue
            x = (Ha[:, i][:, None] ^ Hb[:, j][None, :])
            pc = _POP8[x.view(np.uint8).reshape(a, b, 8)].sum(axis=2).astype(np.float32)
            ok = Ma[:, i][:, None] & Mb[:, j][None, :]
            D[:, :, i] = np.minimum(D[:, :, i], np.where(ok, pc, np.inf))
    fin = np.isfinite(D)
    cnt = fin.sum(axis=2)
    mean = np.where(fin, D, 0).sum(axis=2) / np.maximum(cnt, 1)
    return np.where(cnt >= 3, mean, np.inf)


def clip_phashes(cfg, row) -> list | None:
    """Keyframe hashes for a clip that was analysed before hashes existed (decodes 5 small frames of the proxy)."""
    import cv2
    from . import ingest, media
    proxy, dur = row["proxy"], float(row["duration"] or 0)
    if not proxy or not os.path.exists(proxy) or dur <= 0:
        return None
    info = media.probe(cfg, proxy)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None
    times = [round((k + 0.5) / PHASH_N * dur, 3) for k in range(PHASH_N)]
    frames = ingest.extract_tiles(cfg, proxy, times, dur, int(v["width"]), int(v["height"]), width=64)
    out = [phash_hex(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)) for f in frames]
    return out if any(out) else None


def file_sig(path, size) -> str | None:
    """Cheap content signature for exact-copy detection: size + SHA-1 of the first and last MiB (read-only)."""
    import hashlib
    h = hashlib.sha1(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(1 << 20))
            if size > 2 << 20:
                f.seek(-(1 << 20), os.SEEK_END)
                h.update(f.read(1 << 20))
    except OSError:
        return None
    return h.hexdigest()[:20]


def _ts(row) -> float | None:
    """Capture time (epoch seconds) from clips.shot_at, or None."""
    import datetime as _dt
    s = (row["shot_at"] if "shot_at" in row.keys() else None) or ""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    for cand in (s, s[:19]):
        try:
            d = _dt.datetime.fromisoformat(cand)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    return None


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _rep_score(con, row, tagged) -> tuple:
    q = auto_quality(clip_quality(con, row["id"]))
    return (1 if row["id"] in tagged else 0, q, float(row["duration"] or 0), row["id"])


def dedupe(cfg, con, echo=print, hash_missing=True) -> dict:
    """Group exact copies and near-duplicates. Writes the `dupes` table (one row per member of a group of
    2+, `rep` = the clip to look at / tag; the others inherit its tags). Returns stats."""
    ensure_scale_schema(con)
    t_start = time.time()
    rows = con.execute("SELECT * FROM clips WHERE status IN ('proxied','analyzed') AND proxy IS NOT NULL").fetchall()
    have = {r["clip_id"]: json.loads(r["hashes"]) for r in con.execute("SELECT clip_id, hashes FROM phash")}
    if hash_missing:
        missing = [r for r in rows if r["id"] not in have]
        if missing:
            _say(echo, f"dedupe: hashing {len(missing)} clip(s) analysed before hashes existed")
        gov = governor.for_cfg(cfg)
        for r in missing:
            try:
                with gov.slot("heavy", label=f"phash {r['id']}"):
                    hs = clip_phashes(cfg, r)
            except Exception as e:  # noqa: BLE001 - one unreadable proxy never stops the pass
                _say(echo, f"  ! phash {r['rel']}: {str(e)[-160:]}")
                hs = None
            con.execute("INSERT OR REPLACE INTO phash VALUES (?,?,?,?)", (r["id"], json.dumps(hs), "dedupe", time.time()))
            have[r["id"]] = hs
        con.commit()
    uf = _UF()
    dist = {}
    kinds = {}
    # exact copies: same size first (free), then a 2 MiB content signature
    by_size = {}
    for r in rows:
        if r["size"]:
            by_size.setdefault(int(r["size"]), []).append(r)
    sigs = {r["clip_id"]: (r["size"], r["mtime"], r["sig"]) for r in con.execute("SELECT * FROM filesig")}
    n_exact = 0
    for size, grp in by_size.items():
        if len(grp) < 2:
            continue
        seen = {}
        for r in grp:
            s = sigs.get(r["id"])
            sig = s[2] if s and s[0] == r["size"] and abs((s[1] or 0) - (r["mtime"] or 0)) < 1e-3 else None
            if sig is None:
                sig = file_sig(r["path"], size)
                con.execute("INSERT OR REPLACE INTO filesig VALUES (?,?,?,?)", (r["id"], r["size"], r["mtime"], sig))
            if sig is None:
                continue
            if sig in seen:
                uf.union(seen[sig], r["id"])
                kinds[r["id"]] = "exact"
                kinds[seen[sig]] = "exact"
                dist[r["id"]] = 0.0
                n_exact += 1
            else:
                seen[sig] = r["id"]
    # near-duplicates: same folder, or close in capture time
    thr = float(getattr(cfg, "dedupe_hamming", 6.0) or 6.0)
    win_s = float(getattr(cfg, "dedupe_window_h", 2.0) or 0) * 3600
    usable = [r for r in rows if have.get(r["id"]) and sum(h is not None for h in have[r["id"]]) >= 3]
    buckets = {}
    for r in usable:
        buckets.setdefault(("dir", os.path.dirname(r["rel"] or r["path"])), []).append(r)
    timed = sorted(((t, r) for r in usable if (t := _ts(r)) is not None), key=lambda x: x[0])
    pairs_checked = n_near = 0
    checked = set()

    def link(ra, rb, d):
        nonlocal n_near
        key = (ra["id"], rb["id"]) if ra["id"] < rb["id"] else (rb["id"], ra["id"])
        if key in checked:
            return
        checked.add(key)
        uf.union(ra["id"], rb["id"])
        for x in (ra["id"], rb["id"]):
            dist[x] = min(dist.get(x, 99.0), d)
        n_near += 1

    for grp in buckets.values():                      # same folder: all pairs, 256 rows at a time
        if len(grp) < 2:
            continue
        H, M = _hash_matrix([have[r["id"]] for r in grp])
        for s0 in range(0, len(grp), 256):
            D = dist_block(H[s0:s0 + 256], M[s0:s0 + 256], H, M)
            pairs_checked += D.size
            for i, j in zip(*np.nonzero(D <= thr)):
                if s0 + i < j:
                    link(grp[s0 + i], grp[j], float(D[i, j]))
    if win_s > 0 and len(timed) > 1:                   # close in capture time: a sliding window
        H, M = _hash_matrix([have[r["id"]] for _, r in timed])
        ts = np.array([t for t, _ in timed])
        for i in range(1, len(timed)):
            j0 = int(np.searchsorted(ts, ts[i] - win_s))
            if j0 >= i:
                continue
            D = dist_block(H[i:i + 1], M[i:i + 1], H[j0:i], M[j0:i])[0]
            pairs_checked += len(D)
            for j in np.nonzero(D <= thr)[0]:
                link(timed[j0 + j][1], timed[i][1], float(D[j]))
    groups = {}
    for r in rows:
        if r["id"] in uf.p:
            groups.setdefault(uf.find(r["id"]), []).append(r)
    tagged = {r["clip_id"] for r in con.execute("SELECT clip_id, data FROM tags")
              if "inherited_from" not in (json.loads(r["data"] or "{}") or {})}
    con.execute("DELETE FROM dupes")
    now = time.time()
    n_members = 0
    for root, members in groups.items():
        if len(members) < 2:
            continue
        rep = max(members, key=lambda m: _rep_score(con, m, tagged))
        grp = rep["id"]
        all_exact = all(kinds.get(m["id"]) == "exact" for m in members)
        for m in members:
            con.execute("INSERT INTO dupes VALUES (?,?,?,?,?,?)",
                        (m["id"], grp, rep["id"], "exact" if all_exact or kinds.get(m["id"]) == "exact" else "near",
                         round(dist.get(m["id"], 0.0), 2), now))
            n_members += 1
    con.commit()
    n_groups = sum(1 for m in groups.values() if len(m) >= 2)
    res = {"clips": len(rows), "groups": n_groups, "members": n_members, "redundant": n_members - n_groups,
           "exact_pairs": n_exact, "near_pairs": n_near, "pairs_checked": pairs_checked,
           "seconds": round(time.time() - t_start, 2)}
    _say(echo, f"dedupe: {n_groups} group(s), {n_members - n_groups} redundant clip(s) of {len(rows)} "
               f"({n_exact} exact, {n_near} near pairs; {pairs_checked} pairs compared, {res['seconds']}s)")
    return res


def dupe_info(con, cid) -> dict | None:
    try:
        r = con.execute("SELECT * FROM dupes WHERE clip_id=?", (cid,)).fetchone()
    except Exception:  # noqa: BLE001 - no table yet
        return None
    if not r:
        return None
    members = [x["clip_id"] for x in con.execute("SELECT clip_id FROM dupes WHERE grp=? ORDER BY clip_id", (r["grp"],))]
    return {"group": r["grp"], "rep": r["rep"], "kind": r["kind"], "dist": r["dist"], "members": members}


def near(cfg, con, ref, limit=20) -> list:
    """Clips that look most like `ref` (perceptual-hash distance), with same-shoot clips as a tie-break.
    [{id, dist, same_group, same_session}]"""
    ensure_scale_schema(con)
    from . import db as _db
    cid = _db.get_clip(con, ref)["id"]
    hs = {r["clip_id"]: json.loads(r["hashes"]) for r in con.execute("SELECT clip_id, hashes FROM phash")}
    if not hs.get(cid):
        row = con.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
        hs[cid] = clip_phashes(cfg, row) if row else None
    mine = hs.get(cid)
    sess = {r["clip_id"]: r["session"] for r in con.execute("SELECT clip_id, session FROM sessions")}
    grp = (dupe_info(con, cid) or {}).get("group")
    groups = {r["clip_id"]: r["grp"] for r in con.execute("SELECT clip_id, grp FROM dupes")}
    out = []
    for other, h in hs.items():
        if other == cid:
            continue
        d = phash_dist(mine, h) if mine else None
        same_s = sess.get(cid) is not None and sess.get(other) == sess.get(cid)
        if d is None and not same_s:
            continue
        out.append({"id": other, "dist": None if d is None else round(d, 2),
                    "same_group": bool(grp) and groups.get(other) == grp, "same_session": same_s})
    out.sort(key=lambda x: (not x["same_group"], x["dist"] if x["dist"] is not None else 99, not x["same_session"]))
    return out[:limit]


# -- highlights ------------------------------------------------------------------------------------

HL_WINDOW = WINDOW


def audio_window_features(audio, sr=16000, window=HL_WINDOW) -> list:
    """Per window: loudness (p90 frame dBFS), spectral flux, spectral flatness, onset density (transients/s)
    and 2-8 Hz amplitude-modulation share of the voice band (laughter/cheering rhythm)."""
    n_fft, hop = 1024, 512
    if len(audio) < n_fft:
        return []
    nfr = 1 + (len(audio) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(nfr)[:, None]
    fr = audio[idx] * np.hanning(n_fft)[None, :].astype(np.float32)
    mag = np.abs(np.fft.rfft(fr, axis=1)).astype(np.float32) + 1e-9
    rms_db = 20 * np.log10(np.sqrt(np.mean(fr ** 2, axis=1)) + 1e-9)
    logm = np.log(mag)
    flux = np.r_[0.0, np.maximum(0, np.diff(logm, axis=0)).mean(axis=1)]
    flat = np.exp(logm.mean(axis=1)) / mag.mean(axis=1)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = (freqs > 300) & (freqs < 3000)
    env = mag[:, band].mean(axis=1)
    med = float(np.median(flux))
    mad = float(np.median(np.abs(flux - med))) + 1e-6
    onsets = (flux > med + 4 * mad) & (rms_db > -50)
    fps_fr = sr / hop
    per = int(round(window * fps_fr))
    tfr = (np.arange(nfr) * hop + n_fft / 2) / sr               # frame centre times
    out = []
    for kk in range(int(math.ceil(len(audio) / sr / window))):
        lo, hi = np.searchsorted(tfr, kk * window), np.searchsorted(tfr, (kk + 1) * window)
        sl = slice(int(lo), int(hi))
        if sl.stop - sl.start < per // 4:
            break
        e = env[sl] - env[sl].mean()
        mod = 0.0
        if len(e) >= 16 and np.any(e):
            sp = np.abs(np.fft.rfft(e)) ** 2
            f = np.fft.rfftfreq(len(e), 1 / fps_fr)
            tot = sp[1:].sum() + 1e-12
            mod = float(sp[(f >= 2) & (f <= 8)].sum() / tot)
        out.append({"t0": round(kk * window, 3), "loud": float(np.percentile(rms_db[sl], 90)),
                    "flux": float(flux[sl].mean()), "flat": float(np.median(flat[sl])),
                    "onsets": float(onsets[sl].sum() / window), "mod": mod})
    return out


def _sig(x):
    return 1.0 / (1.0 + math.exp(-x))


def score_windows(audio_feats, metrics, smiles=None) -> list:
    """Combine audio / motion / face evidence into a 0-1 highlight score per 2 s window.
    audio: loud relative to the clip AND loud in absolute terms, transient-dense (claps, laughter, cheers),
    noise-like (flat spectrum: applause, crowd) or rhythmically modulated (laughter). motion: camera-motion
    peaks. faces: a face on screen, more with a smile."""
    smiles = smiles or {}
    A = {round(f["t0"] / HL_WINDOW): f for f in audio_feats}
    M = {round(m["t0"] / HL_WINDOW): m for m in metrics}
    keys = sorted(set(A) | set(M))
    if not keys:
        return []

    def z(vals):
        v = np.array([x for x in vals if x is not None], dtype=float)
        if len(v) < 2:
            return lambda x: 0.0
        mu, sd = float(np.median(v)), float(np.std(v)) + 1e-6
        return lambda x: 0.0 if x is None else (x - mu) / sd
    zl = z([A[k]["loud"] for k in A])
    zf = z([A[k]["flux"] for k in A])
    zo = z([A[k]["onsets"] for k in A])
    zm = z([M[k]["motion"] for k in M])
    out = []
    for k in keys:
        a, m = A.get(k), M.get(k)
        audio = None
        kind = None
        if a:
            level = min(1.0, max(0.0, (a["loud"] + 50) / 30))          # -50 dBFS -> 0, -20 dBFS -> 1
            stand_out = _sig(2 * (zl(a["loud"]) - 0.5))                # louder than the rest of this clip
            # applause / crowd: noise-like (flat) spectrum full of transients; laughter / cheering: dense
            # transients with a 2-8 Hz rhythm. Ordinary speech has the rhythm but fewer, quieter onsets.
            applause = min(1.0, max(0.0, (a["flat"] - 0.3) / 0.25)) * min(1.0, a["onsets"] / 8)
            laughish = min(1.0, max(0.0, (a["mod"] - 0.3) / 0.4)) * min(1.0, a["onsets"] / 7)
            audio = level * (0.3 * stand_out + 0.2 * _sig(zf(a["flux"]) - 1) + 0.2 * _sig(zo(a["onsets"]) - 1)
                             + 0.3 * max(applause, laughish) * stand_out)
            if level > 0.5 and zl(a["loud"]) > 0.5 and applause > 0.6:
                kind = "applause/crowd"
            elif level > 0.5 and zl(a["loud"]) > 1.0 and laughish > 0.6:
                kind = "laughter/cheer"
        motion = None
        face = 0.0
        if m:
            motion = min(1.0, max(0.0, (m.get("motion") or 0) / 15)) * 0.5 + 0.5 * _sig(zm(m.get("motion")) - 1)
            if (m.get("faces") or 0) > 0:
                face = 0.5 + 0.5 * float(smiles.get(round(m["t0"], 3)) or 0)
        parts = [(0.55, audio), (0.25, motion), (0.2, face if m else None)]
        wsum = sum(w for w, v in parts if v is not None)
        score = sum(w * v for w, v in parts if v is not None) / wsum if wsum else 0.0
        if kind is None:
            if motion is not None and motion > 0.75:
                kind = "motion"
            elif face > 0.9:
                kind = "smile"
            elif audio is not None and audio > 0.6 and zl(a["loud"]) > 1.5:
                kind = "loud"
        t0 = k * HL_WINDOW
        out.append({"t0": round(t0, 3), "t1": round(t0 + HL_WINDOW, 3), "score": round(score, 4),
                    "audio": None if audio is None else round(audio, 4),
                    "motion": None if motion is None else round(motion, 4), "faces": round(face, 3), "kind": kind})
    return out


def highlights_clip(cfg, con, row) -> list:
    """Compute and store the highlight windows for one analysed clip. Returns the windows."""
    from . import asr
    ensure_scale_schema(con)
    cid = row["id"]
    feats = []
    if row["has_audio"]:
        src = row["proxy"] if row["proxy"] and os.path.exists(row["proxy"]) else row["path"]
        try:
            feats = audio_window_features(asr.load_audio(cfg, src, timeout=600))
        except Exception:  # noqa: BLE001 - no usable audio: picture-only score
            feats = []
    mets = [dict(r) for r in con.execute("SELECT t0, t1, motion, faces FROM metrics WHERE clip_id=? ORDER BY t0", (cid,))]
    smiles = {round(r["t0"], 3): r["smile"] for r in con.execute("SELECT t0, smile FROM win_extra WHERE clip_id=?", (cid,))}
    dur = float(row["duration"] or 0)
    wins = [w for w in score_windows(feats, mets, smiles) if not dur or w["t0"] < dur]
    con.execute("DELETE FROM highlights WHERE clip_id=?", (cid,))
    con.executemany("INSERT INTO highlights VALUES (?,?,?,?,?,?,?,?)",
                    [(cid, w["t0"], min(w["t1"], dur) if dur else w["t1"], w["score"], w["audio"], w["motion"],
                      w["faces"], w["kind"]) for w in wins])
    best = max(wins, key=lambda w: w["score"], default=None)
    con.execute("INSERT OR REPLACE INTO highlight_peaks VALUES (?,?,?,?,?)",
                (cid, best["score"] if best else 0.0, best["t0"] if best else None, best["kind"] if best else None,
                 time.time()))
    con.commit()
    return wins


def highlights_pending(con) -> list:
    ensure_scale_schema(con)
    return con.execute("SELECT c.* FROM clips c LEFT JOIN highlight_peaks h ON h.clip_id = c.id "
                       "WHERE c.status='analyzed' AND (h.clip_id IS NULL OR h.updated < c.updated)").fetchall()


def highlights_all(cfg, con, echo=print) -> int:
    rows = highlights_pending(con)
    gov = governor.for_cfg(cfg)
    for i, r in enumerate(rows, 1):
        try:
            with gov.slot("heavy", label=f"highlights {r['id']}"):
                highlights_clip(cfg, con, r)
        except Exception as e:  # noqa: BLE001
            _say(echo, f"  ! highlights {r['rel']}: {str(e)[-160:]}")
        if i % 50 == 0:
            _say(echo, f"  highlights {i}/{len(rows)}")
    _say(echo, f"highlights: {len(rows)} clip(s) scored")
    return len(rows)


# -- capture sessions ("shoots") + places ---------------------------------------------------------

def _haversine_m(a, b):
    la1, lo1, la2, lo2 = (math.radians(x) for x in (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))


def clip_geo(cfg, con, row, probe=True) -> dict:
    """GPS + capture time for one clip (cached in clip_geo). GPS from ffprobe tags (ISO 6709) or a camera
    XML sidecar; time from shot_at, else the file's mtime (flagged ts_src='mtime')."""
    from . import ingest, media
    ts, ts_src = _ts(row), "shot_at"                      # always from the current row (cheap)
    if ts is None and row["mtime"]:
        ts, ts_src = float(row["mtime"]), "mtime"
    r = con.execute("SELECT * FROM clip_geo WHERE clip_id=?", (row["id"],)).fetchone()
    if r and (r["updated"] or 0) >= (row["updated"] or 0):          # GPS is cached: one probe per clip
        d = dict(r)
        d.update(ts=ts, ts_src=ts_src if ts is not None else None)
        return d
    g, src = None, None
    if probe:
        try:
            g = media.geo_from_probe(media.probe(cfg, row["path"]))
            src = "tags" if g else None
        except Exception:  # noqa: BLE001
            g = None
        if not g:
            for sc in ingest._sidecars(row["path"]):
                if sc.lower().endswith(".xml") and os.path.getsize(sc) < 512 * 1024:
                    try:
                        g = media.geo_from_xml(open(sc, errors="ignore").read())
                    except OSError:
                        g = None
                    if g:
                        src = "xml"
                        break
    d = {"clip_id": row["id"], "lat": g[0] if g else None, "lon": g[1] if g else None, "alt": g[2] if g else None,
         "ts": ts, "ts_src": ts_src if ts is not None else None, "source": src, "updated": time.time()}
    con.execute("INSERT OR REPLACE INTO clip_geo VALUES (?,?,?,?,?,?,?,?)", tuple(d.values()))
    return d


def sessions(cfg, con, echo=print, probe=True) -> dict:
    """Group clips into shoots (capture-time gap > session_gap_min, or a different GPS place) and places
    (GPS within place_radius_m). Writes the `sessions` table; numbering is chronological from 1."""
    ensure_scale_schema(con)
    rows = con.execute("SELECT * FROM clips WHERE status NOT IN ('new','error')").fetchall()
    geo = [clip_geo(cfg, con, r, probe=probe) for r in rows]
    con.commit()
    radius = float(getattr(cfg, "place_radius_m", 150) or 150)
    gap = float(getattr(cfg, "session_gap_min", 45) or 45) * 60
    places = []                                         # [(lat, lon, n)]
    place_of = {}
    for g in sorted((g for g in geo if g["lat"] is not None), key=lambda g: g["ts"] or 0):
        best = None
        for i, (la, lo, n) in enumerate(places):
            d = _haversine_m((la, lo), (g["lat"], g["lon"]))
            if d <= radius and (best is None or d < best[1]):
                best = (i, d)
        if best is None:
            places.append((g["lat"], g["lon"], 1))
            place_of[g["clip_id"]] = len(places)
        else:
            i = best[0]
            la, lo, n = places[i]
            places[i] = ((la * n + g["lat"]) / (n + 1), (lo * n + g["lon"]) / (n + 1), n + 1)
            place_of[g["clip_id"]] = i + 1
    timed = sorted((g for g in geo if g["ts"] is not None), key=lambda g: g["ts"])
    con.execute("DELETE FROM sessions")
    sid, prev = 0, None
    now = time.time()
    for g in timed:
        pl = place_of.get(g["clip_id"])
        new = prev is None or g["ts"] - prev["ts"] > gap
        if not new and pl and prev.get("_place") and pl != prev["_place"]:
            new = True
        if new:
            sid += 1
        g["_place"] = pl or (prev.get("_place") if prev and not new else None)
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (g["clip_id"], sid, pl, g["ts"], now))
        prev = g
    con.commit()
    res = {"clips": len(rows), "sessions": sid, "places": len(places), "with_gps": len(place_of),
           "untimed": len(geo) - len(timed)}
    _say(echo, f"sessions: {sid} shoot(s), {len(places)} place(s) from GPS ({len(place_of)} clips with GPS, "
               f"{res['untimed']} without a capture time)")
    return res


def session_list(con) -> list:
    """[{session, day, start, end, clips, hours, place, lat, lon}] newest first."""
    import datetime as _dt
    try:
        rows = con.execute("SELECT s.session, MIN(s.ts) AS t0, MAX(s.ts) AS t1, COUNT(*) AS n, "
                           "SUM(COALESCE(c.duration,0)) AS dur, MAX(s.place) AS place, AVG(g.lat) AS lat, AVG(g.lon) AS lon "
                           "FROM sessions s JOIN clips c ON c.id = s.clip_id LEFT JOIN clip_geo g ON g.clip_id = s.clip_id "
                           "GROUP BY s.session ORDER BY t0 DESC").fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        utc = _dt.timezone.utc                 # same clock as clips.shot_at, so it lines up with the Days view
        d0 = _dt.datetime.fromtimestamp(r["t0"], utc) if r["t0"] else None
        out.append({"session": r["session"], "day": d0.strftime("%Y-%m-%d") if d0 else "unknown",
                    "start": d0.strftime("%H:%M") if d0 else None,
                    "end": _dt.datetime.fromtimestamp(r["t1"], utc).strftime("%H:%M") if r["t1"] else None,
                    "clips": r["n"], "hours": round((r["dur"] or 0) / 3600, 2), "place": r["place"],
                    "lat": None if r["lat"] is None else round(r["lat"], 4),
                    "lon": None if r["lon"] is None else round(r["lon"], 4)})
    return out


# -- best takes ------------------------------------------------------------------------------------

def takes(cfg, con, echo=print, threshold=0.7) -> dict:
    """Find lines said more than once (inside a clip or across clips of the same shoot) and rank the takes:
    the later take wins (creators restart a line until it is right) unless it is clearly worse
    (incomplete, shaky, soft, no face). Writes the `takes` table (best=1 marks the take to use)."""
    import difflib
    from . import asr
    ensure_scale_schema(con)
    sess = {r["clip_id"]: (r["session"], r["ts"]) for r in con.execute("SELECT clip_id, session, ts FROM sessions")}
    trs = con.execute("SELECT clip_id FROM transcripts WHERE n_words > 5").fetchall()
    lines = []                                   # (scope, order_key, cid, t0, t1, text, tokens)
    for r in trs:
        cid = r["clip_id"]
        try:
            ws = asr.words(cfg, cid)
        except Exception:  # noqa: BLE001
            continue
        s, ts = sess.get(cid, (None, None))
        scope = f"s{s}" if s is not None else f"c{cid}"
        for t0, t1, text in asr.sentences(ws):
            toks = [asr._norm(x) for x in text.split() if asr._norm(x)]
            if len(toks) >= 4:
                lines.append((scope, ((ts or 0) + t0), cid, t0, t1, text, toks))
    by_scope = {}
    for ln in lines:
        by_scope.setdefault(ln[0], []).append(ln)
    uf = _UF()
    for scope, ls in by_scope.items():
        index = {}
        for i, ln in enumerate(ls):
            for tok in set(ln[6]):
                if tok not in asr.STOPWORDS:
                    index.setdefault(tok, []).append(i)
        for i, ln in enumerate(ls):
            cnt = {}
            for tok in set(ln[6]):
                for j in index.get(tok, ()):
                    if j > i:
                        cnt[j] = cnt.get(j, 0) + 1
            for j, c in cnt.items():
                if c < 2:
                    continue
                if difflib.SequenceMatcher(None, ln[6], ls[j][6]).ratio() >= threshold:
                    uf.union((scope, i), (scope, j))
    groups = {}
    for scope, ls in by_scope.items():
        for i, ln in enumerate(ls):
            if (scope, i) in uf.p:
                groups.setdefault(uf.find((scope, i)), []).append(ln)
    con.execute("DELETE FROM takes")
    gid = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        gid += 1
        members.sort(key=lambda x: x[1])
        longest = max(len(m[6]) for m in members)
        scored = []
        for k, m in enumerate(members):
            q = window_quality(con, m[2], m[3], m[4])
            qual = 0.5
            if q:
                qual = 1.0
                qual -= min(0.5, max(0.0, (q.get("shake") or 0) - 4) / 10)
                qual -= 0.3 if (q.get("sharpness") or 0) < 20 else 0.0
                qual -= 0.2 if not q.get("faces") else 0.0
            order = (k + 1) / len(members)
            complete = len(m[6]) / longest
            score = 0.5 * order + 0.25 * complete + 0.25 * max(0.0, qual)
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        for rank, (score, m) in enumerate(scored, 1):
            con.execute("INSERT OR REPLACE INTO takes VALUES (?,?,?,?,?,?,?,?)",
                        (gid, m[2], round(m[3], 3), round(m[4], 3), m[5][:300], rank, round(score, 3), int(rank == 1)))
    con.commit()
    _say(echo, f"takes: {gid} repeated line(s) ranked across {len(trs)} transcript(s)")
    return {"groups": gid, "lines": len(lines)}


# -- tagging gate ----------------------------------------------------------------------------------

def gate_reason(cfg, row, q) -> str | None:
    """Why a clip is not worth an AI tagging call, or None. Gated clips stay searchable (by file name,
    speech and metrics) and can be tagged later (tag_gate: false)."""
    if not getattr(cfg, "tag_gate", True):
        return None
    dur = float(row["duration"] or 0)
    if dur < float(getattr(cfg, "tag_min_dur", 1.0) or 0):
        return f"too short ({dur:.1f}s)"
    if q:
        dark = float(getattr(cfg, "tag_gate_dark", 6.0) or 0)
        if dark and q.get("exposure") is not None and q["exposure"] < dark:
            return f"black / lens cap (luma {q['exposure']:.0f})"
        sh = float(getattr(cfg, "tag_gate_shake", 12.0) or 0)
        if sh and (q.get("shake") or 0) > sh:
            return f"very shaky (shake {q['shake']:.1f})"
    return None


# ==================================================================================================
# Event recap detectors (live seminar / conference footage): what kind of shot each 2 s window is,
# where the crowd energy peaks, and which spoken lines are soundbites. Every number is a cheap,
# explainable measurement; the kinds are estimates the AI confirms on the frames before it trusts them.
#
#   event_windows   per 2 s window: stage / crowd / wide scores (0-1), kind, energy (0-1), peak flag and
#                   the raw cues they come from (loudness, applause, local motion, faces, face size,
#                   stage-light share, edge density)
#   event_clips     per clip: dominant kind, share of each kind, strongest energy moment
#   soundbites      short spoken lines scored by loudness, pitch, emphasis, wording and the crowd's
#                   response right after (needs a transcript; nothing here runs speech-to-text)
# ==================================================================================================

EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_windows (clip_id TEXT, t0 REAL, t1 REAL, kind TEXT, stage REAL, crowd REAL,
  wide REAL, energy REAL, peak INTEGER, loud REAL, applause REAL, local_motion REAL, faces INTEGER, face_h REAL,
  lights REAL, edges REAL, luma REAL, PRIMARY KEY (clip_id, t0));
CREATE INDEX IF NOT EXISTS event_windows_kind ON event_windows(kind);
CREATE TABLE IF NOT EXISTS event_clips (clip_id TEXT PRIMARY KEY, kind TEXT, stage REAL, crowd REAL, wide REAL,
  energy REAL, energy_t REAL, updated REAL);
CREATE TABLE IF NOT EXISTS soundbites (clip_id TEXT, t0 REAL, t1 REAL, text TEXT, score REAL, loud REAL,
  pitch REAL, emphasis REAL, wording REAL, response REAL, PRIMARY KEY (clip_id, t0));
CREATE INDEX IF NOT EXISTS soundbites_score ON soundbites(score);
"""
EVENT_KINDS = ("stage", "crowd", "wide")
EV_FPS = 4.0                     # picture sampling for the event pass (local motion needs ~4 fps)
EV_W = 320                       # analysis width (faces use 2x this)
EV_KIND_MIN = 0.35               # a kind needs at least this score, and a margin over the runner-up
EV_KIND_MARGIN = 0.08
EV_PEAK = 0.5                    # energy at or above this, and a local maximum in its clip = a peak
BITE_MIN_S, BITE_MAX_S = 0.8, 7.0
BITE_MIN_WORDS, BITE_MAX_WORDS = 3, 18
_QUOTE = re.compile(r"\b(you|your|we|our|together|today|tonight|now|never|always|everything|nothing|believe|"
                    r"dream|change|future|win|life|stand|rise|ready|let's|lets)\b", re.I)


def ensure_event_schema(con):
    con.executescript(EVENT_SCHEMA)


def _clamp01(x) -> float:
    return float(min(1.0, max(0.0, x)))


def event_frame_features(ffmpeg_bin, src, duration, window=WINDOW, fps=EV_FPS, width=EV_W, ffprobe_bin="ffprobe") -> list:
    """One small decode of the proxy. Per 2 s window: faces (count and the largest face's height as a share
    of the frame), local motion (mean absolute frame difference after removing the global camera shift, so
    a pan alone stays low and a jumping crowd stays high), edge density, mean luma, dark share and the
    stage-light share (bright pixels inside a mostly dark frame, or bright saturated colour)."""
    import cv2
    w = int(width)
    probe = subprocess.run([ffprobe_bin or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    try:
        pw, ph = (int(x) for x in probe.stdout.strip().split(",")[:2])
    except ValueError:
        pw, ph = 16, 9
    h = max(2, int(round(w * ph / pw / 2)) * 2)
    fw, fh = 2 * w, 2 * h
    cmd = [ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src), "-an",
           "-vf", f"fps={fps},scale={fw}:{fh}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fsize = fw * fh * 3
    cascade = _face_cascade()
    min_face = max(16, int(fh * 0.05))
    wins = {}
    prev, i = None, 0
    while True:
        buf = proc.stdout.read(fsize)
        if len(buf) < fsize:
            break
        big = np.frombuffer(buf, np.uint8).reshape(fh, fw, 3)
        frame = cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA)
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        t = i / fps
        x = wins.setdefault(int(t // window), {"faces": 0, "face_h": 0.0, "lm": [], "edges": [], "luma": [],
                                                "dark": [], "lights": []})
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        dark = float((grey < 45).mean())
        bright = float((grey > 200).mean())
        colour = float(((hsv[..., 2] > 170) & (hsv[..., 1] > 110)).mean())
        # spotlights: some bright pixels inside a frame that is mostly dark; or strong coloured light
        x["lights"].append(_clamp01(min(bright, 0.3) / 0.08) * _clamp01((dark - 0.35) / 0.3) + 0.5 * _clamp01(colour / 0.1))
        x["dark"].append(dark)
        x["luma"].append(float(grey.mean()))
        x["edges"].append(float((cv2.Canny(grey, 60, 140) > 0).mean()))
        g = grey.astype(np.float32)
        if prev is not None:
            try:
                (dx, dy), _ = cv2.phaseCorrelate(prev, g)
            except cv2.error:
                dx = dy = 0.0
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            warped = cv2.warpAffine(prev, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
            mx = int(min(w // 4, abs(dx) + 2)), int(min(h // 4, abs(dy) + 2))
            d = np.abs(g - warped)[mx[1]:h - mx[1], mx[0]:w - mx[0]]
            x["lm"].append(float(d.mean()) if d.size else 0.0)
        prev = g
        if cascade is not None and i % 2 == 0:
            gb = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gb, scaleFactor=1.15, minNeighbors=5, minSize=(min_face, min_face))
            if len(faces):
                x["faces"] = max(x["faces"], len(faces))
                x["face_h"] = max(x["face_h"], float(max(f[3] for f in faces)) / fh)
        i += 1
    proc.stdout.close()
    proc.wait()
    dur = float(duration or i / fps)
    out = []
    for k in sorted(wins):
        x = wins[k]
        mean = lambda a: float(np.mean(a)) if a else 0.0   # noqa: E731
        out.append({"t0": round(k * window, 3), "t1": round(min((k + 1) * window, dur), 3), "faces": int(x["faces"]),
                    "face_h": round(x["face_h"], 4), "local_motion": round(mean(x["lm"]), 3),
                    "edges": round(mean(x["edges"]), 4), "luma": round(mean(x["luma"]), 1),
                    "dark": round(mean(x["dark"]), 3), "lights": round(mean(x["lights"]), 3)})
    return out


def _applause(a) -> float:
    """Applause / cheering evidence 0-1 from audio_window_features: a noise-like (flat) spectrum full of
    transients, or dense transients with a 2-8 Hz rhythm (cheering, chanting, laughter)."""
    if not a:
        return 0.0
    ap = _clamp01((a["flat"] - 0.3) / 0.25) * _clamp01(a["onsets"] / 8)
    ch = _clamp01((a["mod"] - 0.3) / 0.4) * _clamp01(a["onsets"] / 7)
    return max(ap, ch)


def _speechlike(a) -> float:
    """Voice-like audio 0-1: rhythmic (2-8 Hz modulation), not noise-like, with some level."""
    if not a:
        return 0.0
    return _clamp01((a["mod"] - 0.15) / 0.35) * _clamp01((0.35 - a["flat"]) / 0.25) * _clamp01((a["loud"] + 55) / 20)


def event_scores(pic, audio_feats, metrics=None) -> list:
    """Combine picture, audio and camera evidence per window into stage / crowd / wide scores, a kind and
    a crowd-energy value. Pure function (tests feed it numbers).
      stage  a dominant speaker (one to three faces, the largest >= ~8 % of the frame height) or
             spotlights in a dark frame, with voice-like sound
      crowd  many faces and/or lots of local motion in a busy (edge-dense) frame, applause or cheering
      wide   a busy frame with little local motion and no big face (an establishing view of the room)
      energy loudness (absolute: -50 dBFS -> 0, -15 dBFS -> 1) + applause/cheering + local motion"""
    A = {round(f["t0"] / WINDOW): f for f in (audio_feats or [])}
    M = {round(m["t0"] / WINDOW): m for m in (metrics or [])}
    out = []
    for p in pic:
        k = round(p["t0"] / WINDOW)
        a, m = A.get(k), M.get(k)
        lm = _clamp01((p["local_motion"] - 2.0) / 8.0)
        busy = _clamp01((p["edges"] - 0.03) / 0.09)
        big_face = _clamp01((p["face_h"] - 0.06) / 0.1) if 1 <= p["faces"] <= 3 else 0.0
        many_faces = _clamp01((p["faces"] - 2) / 4)
        app = _applause(a)
        speech = _speechlike(a)
        loud = _clamp01((a["loud"] + 50) / 35) if a else 0.0
        cam = _clamp01(((m or {}).get("motion") or 0) / 12)
        stage = max(big_face, 0.9 * p["lights"]) * (0.7 + 0.3 * speech) * (1 - 0.6 * many_faces) * (1 - 0.5 * app)
        crowd = max(many_faces, 0.85 * lm * (0.4 + 0.6 * busy)) * (0.75 + 0.25 * max(app, loud))
        crowd = max(crowd, 0.6 * app * (0.5 + 0.5 * lm))
        wide = busy * (1 - lm) * (1 - big_face) * (1 - 0.5 * many_faces) * (1 - 0.6 * p["lights"]) * (1 - 0.3 * cam)
        sc = {"stage": stage, "crowd": crowd, "wide": wide}
        best = sorted(sc, key=lambda x: -sc[x])
        kind = best[0] if sc[best[0]] >= EV_KIND_MIN and sc[best[0]] - sc[best[1]] >= EV_KIND_MARGIN else None
        energy = 0.4 * loud + 0.35 * app + 0.25 * lm
        out.append({**p, "stage": round(stage, 3), "crowd": round(crowd, 3), "wide": round(wide, 3), "kind": kind,
                    "energy": round(energy, 3), "loud": None if a is None else round(a["loud"], 1),
                    "applause": round(app, 3)})
    for i, w in enumerate(out):            # peaks: local maxima of energy above EV_PEAK
        nb = [out[j]["energy"] for j in (i - 1, i + 1) if 0 <= j < len(out)]
        w["peak"] = int(w["energy"] >= EV_PEAK and all(w["energy"] >= x for x in nb))
    return out


def _f0_track(y, sr):
    """(f0 Hz per 20 ms frame, voiced mask) via YIN on the voice range; None when librosa is missing."""
    try:
        import librosa
    except ImportError:
        return None, None
    if len(y) < sr // 4:
        return None, None
    hop = int(sr * 0.02)
    f0 = librosa.yin(y.astype(np.float32), fmin=70, fmax=420, sr=sr, frame_length=1024, hop_length=hop)
    n = len(f0)
    rms = np.array([np.sqrt(np.mean(y[i * hop:i * hop + 1024] ** 2) + 1e-12) if i * hop < len(y) else 0.0
                    for i in range(n)])
    voiced = (20 * np.log10(rms + 1e-9) > -45) & (f0 > 72) & (f0 < 415)
    return f0, voiced


def _rms_db(y) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9)) if len(y) else -120.0


def soundbite_candidates(words_list, lo=BITE_MIN_S, hi=BITE_MAX_S) -> list:
    """Sentences (and the clauses of long ones) short enough to quote: [(t0, t1, text, [words])]."""
    from . import asr
    out = []
    for t0, t1, text in asr.sentences(words_list):
        ws = [w for w in words_list if t0 <= w[0] and w[1] <= t1 + 1e-6]
        parts = [ws]
        if t1 - t0 > hi or len(ws) > BITE_MAX_WORDS:             # split long sentences at commas / pauses
            parts, cur = [], []
            for k, w in enumerate(ws):
                cur.append(w)
                nxt = ws[k + 1] if k + 1 < len(ws) else None
                if nxt is None or re.search(r"[,;:]$", w[2]) or nxt[0] - w[1] > 0.35:
                    parts.append(cur)
                    cur = []
        for p in parts:
            if not p:
                continue
            a, b = p[0][0], p[-1][1]
            if lo <= b - a <= hi and BITE_MIN_WORDS <= len(p) <= BITE_MAX_WORDS:
                out.append((a, b, " ".join(w[2] for w in p), p))
    return out


def score_soundbites(words_list, audio, sr=16000, after_s=4.0) -> list:
    """Score quotable lines in one clip. Per line, relative to the clip's own speech:
      loud      line loudness vs the clip's median line (z, squashed to 0-1)
      pitch     median pitch vs the clip's median (semitones) plus pitch range inside the line
      emphasis  loudness swing across the line's words + '!' / ALL-CAPS words
      wording   short, direct, quotable words (you/we/never/dream ... numbers, superlatives)
      response  the crowd right after the line (0-after_s s): louder than the line and applause-like
    score = 0.25 loud + 0.2 pitch + 0.15 emphasis + 0.15 wording + 0.25 response."""
    from .render import _STAKES
    cands = soundbite_candidates(words_list)
    if not cands:
        return []
    n = len(audio)
    rows = []
    f0_all, voiced_all = _f0_track(audio, sr) if audio is not None and len(audio) else (None, None)
    hop = int(sr * 0.02)
    clip_f0 = float(np.median(f0_all[voiced_all])) if f0_all is not None and voiced_all.any() else None
    for t0, t1, text, ws in cands:
        seg = audio[int(t0 * sr):int(t1 * sr)] if n else np.zeros(0, np.float32)
        loud = _rms_db(seg)
        wl = [_rms_db(audio[int(w[0] * sr):int(w[1] * sr)]) for w in ws] if n else []
        swing = float(np.percentile(wl, 90) - np.percentile(wl, 10)) if len(wl) >= 3 else 0.0
        st_med = st_rng = 0.0
        if f0_all is not None and clip_f0:
            i0, i1 = int(t0 * sr / hop), int(t1 * sr / hop) + 1
            f, v = f0_all[i0:i1], voiced_all[i0:i1]
            if v.sum() >= 5:
                semi = 12 * np.log2(f[v] / clip_f0)
                st_med = float(np.median(semi))
                st_rng = float(np.percentile(semi, 90) - np.percentile(semi, 10))
        after = audio[int(t1 * sr) + int(0.15 * sr):int((t1 + after_s) * sr)] if n else np.zeros(0, np.float32)
        resp = 0.0
        if len(after) >= sr:
            af = audio_window_features(after, sr, window=min(2.0, len(after) / sr))
            app = max((_applause(x) for x in af), default=0.0)
            jump = _rms_db(after) - loud
            resp = _clamp01(0.6 * app + 0.4 * _clamp01((jump + 6) / 12))
        caps = sum(1 for w in ws if len(w[2]) > 2 and w[2].strip(".,!?").isupper())
        excl = 1 if "!" in text else 0
        nw = len(ws)
        wording = _clamp01(0.25 * len(_QUOTE.findall(text)) + 0.3 * len(_STAKES.findall(text))
                           + (0.3 if 4 <= nw <= 12 else 0.0) + (0.15 if re.search(r"[.!?]$", text) else 0.0))
        rows.append({"t0": round(t0, 3), "t1": round(t1, 3), "text": text, "_loud": loud, "_swing": swing,
                     "_st": st_med, "_rng": st_rng, "_caps": caps, "_excl": excl, "wording": round(wording, 3),
                     "response": round(resp, 3)})
    med_loud = float(np.median([r["_loud"] for r in rows]))
    for r in rows:
        r["loud"] = round(_sig((r["_loud"] - med_loud) / 3.0), 3)
        r["pitch"] = round(_clamp01(0.5 + r["_st"] / 6.0) * 0.6 + _clamp01(r["_rng"] / 8.0) * 0.4, 3)
        r["emphasis"] = round(_clamp01(r["_swing"] / 15.0) * 0.7 + 0.15 * min(1, r["_caps"]) + 0.15 * r["_excl"], 3)
        r["score"] = round(0.25 * r["loud"] + 0.2 * r["pitch"] + 0.15 * r["emphasis"] + 0.15 * r["wording"]
                           + 0.25 * r["response"], 4)
        for k in [k for k in r if k.startswith("_")]:
            r.pop(k)
    rows.sort(key=lambda r: -r["score"])
    return rows


def events_clip(cfg, con, row) -> dict:
    """Event windows, the clip summary and soundbites for one analysed clip (stored; returns the summary)."""
    from . import asr
    ensure_event_schema(con)
    cid = row["id"]
    src = row["proxy"] if row["proxy"] and os.path.exists(row["proxy"]) else row["path"]
    dur = float(row["duration"] or 0)
    pic = event_frame_features(getattr(cfg, "ffmpeg", "ffmpeg") or "ffmpeg", src, dur,
                               ffprobe_bin=getattr(cfg, "ffprobe", "ffprobe") or "ffprobe")
    audio = None
    feats = []
    if row["has_audio"]:
        try:
            audio = asr.load_audio(cfg, src, timeout=600)
            feats = audio_window_features(audio)
        except Exception:  # noqa: BLE001 - picture-only scores
            audio, feats = None, []
    mets = [dict(r) for r in con.execute("SELECT t0, motion FROM metrics WHERE clip_id=? ORDER BY t0", (cid,))]
    wins = event_scores(pic, feats, mets)
    con.execute("DELETE FROM event_windows WHERE clip_id=?", (cid,))
    con.executemany("INSERT INTO event_windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(cid, w["t0"], w["t1"], w["kind"], w["stage"], w["crowd"], w["wide"], w["energy"], w["peak"],
                      w["loud"], w["applause"], w["local_motion"], w["faces"], w["face_h"], w["lights"], w["edges"],
                      w["luma"]) for w in wins])
    n = max(1, len(wins))
    share = {k: round(sum(1 for w in wins if w["kind"] == k) / n, 3) for k in EVENT_KINDS}
    top = max(share, key=lambda k: share[k])
    kind = top if share[top] >= 0.3 else None
    best = max(wins, key=lambda w: w["energy"], default=None)
    summ = {"kind": kind, **share, "energy": best["energy"] if best else 0.0, "energy_t": best["t0"] if best else None}
    con.execute("INSERT OR REPLACE INTO event_clips VALUES (?,?,?,?,?,?,?,?)",
                (cid, kind, share["stage"], share["crowd"], share["wide"], summ["energy"], summ["energy_t"], time.time()))
    con.execute("DELETE FROM soundbites WHERE clip_id=?", (cid,))
    ws = asr.words(cfg, cid)
    bites = score_soundbites(ws, audio if audio is not None else np.zeros(0, np.float32)) if ws else []
    con.executemany("INSERT OR REPLACE INTO soundbites VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(cid, b["t0"], b["t1"], b["text"][:300], b["score"], b["loud"], b["pitch"], b["emphasis"],
                      b["wording"], b["response"]) for b in bites])
    con.commit()
    summ["soundbites"] = len(bites)
    return summ


def events_pending(con) -> list:
    """Analysed clips without event scores, or whose analysis / transcript is newer than them."""
    ensure_event_schema(con)
    return con.execute(
        "SELECT c.* FROM clips c LEFT JOIN event_clips e ON e.clip_id = c.id LEFT JOIN transcripts tr ON tr.clip_id = c.id "
        "WHERE c.status='analyzed' AND (e.clip_id IS NULL OR e.updated < c.updated OR e.updated < COALESCE(tr.updated, 0))"
    ).fetchall()


def events_all(cfg, con, echo=print) -> int:
    rows = events_pending(con)
    gov = governor.for_cfg(cfg)
    kinds = {}
    for i, r in enumerate(rows, 1):
        try:
            with gov.slot("heavy", label=f"events {r['id']}"):
                s = events_clip(cfg, con, r)
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        except Exception as e:  # noqa: BLE001
            _say(echo, f"  ! events {r['rel']}: {str(e)[-160:]}")
        if i % 50 == 0:
            _say(echo, f"  events {i}/{len(rows)}")
    nb = con.execute("SELECT COUNT(*) FROM soundbites").fetchone()[0]
    _say(echo, f"events: {len(rows)} clip(s) scored ("
               + ", ".join(f"{k or 'mixed'} {v}" for k, v in sorted(kinds.items(), key=lambda x: str(x[0])))
               + f"); {nb} soundbite(s) in the library")
    return len(rows)


def event_pool(con, kind, limit=200, min_score=0.0) -> list:
    """Best windows of one kind across the library: [{clip_id, t0, t1, score, energy, local_motion, face_h,
    faces, peak}], strongest first (crowd by energy, stage by score, wide by score with steady camera)."""
    ensure_event_schema(con)
    order = {"crowd": "w.energy * 0.6 + w.crowd * 0.4", "stage": "w.stage", "wide": "w.wide"}[kind]
    rows = con.execute(
        f"SELECT w.*, ({order}) AS rank_score, COALESCE(m.shake, 0) AS shake FROM event_windows w "
        "LEFT JOIN metrics m ON m.clip_id = w.clip_id AND m.t0 = w.t0 "
        f"WHERE w.kind = ? AND w.{kind} >= ? ORDER BY rank_score DESC LIMIT ?", (kind, float(min_score), int(limit))).fetchall()
    return [dict(r) for r in rows]


def soundbite_list(con, limit=50, text=None) -> list:
    ensure_event_schema(con)
    rows = con.execute("SELECT * FROM soundbites ORDER BY score DESC").fetchall()
    out = []
    words = [w.lower() for w in (text or "").split() if w]
    for r in rows:
        if words and not all(w in (r["text"] or "").lower() for w in words):
            continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out
