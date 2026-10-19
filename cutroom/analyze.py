"""Per-clip analysis on the proxy: shot boundaries + 2-second quality windows.

One ffmpeg decode pass per clip (rawvideo pipe, 12.5 fps, 480 px wide) feeds:
  shots      PySceneDetect ContentDetector (on a 240 px copy)
  shake      LK optical flow -> RANSAC similarity transform -> camera translation (dx, dy) per frame;
             shake = mean |change of camera velocity| (px @ 480w), motion = mean |velocity|
  exposure   mean luma, contrast = luma std, sharpness = variance of Laplacian
  faces      max Haar-cascade frontal faces, sampled every ~0.5 s
"""
import os
import statistics
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

from . import db

FPS = 12.5
WIDTH = 480
WINDOW = 2.0
FACE_EVERY = 6                   # frames (~0.48 s at 12.5 fps)
LOG_SAT_MAX = 45.0               # mean HSV saturation (0-255) below which 10-bit footage is treated as log
LOG_CONTRAST_MAX = 50.0

_cascade = None


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
    det = _cut_detector()
    min_face = max(20, int(min(w, h) * 0.07))

    cuts, wins = [], {}
    prev_grey, prev_v, pts = None, (0.0, 0.0), None
    sats = []
    i = 0
    while True:
        buf = proc.stdout.read(fsize)
        if len(buf) < fsize:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        t = i / FPS
        k = int(t // WINDOW)
        win = wins.setdefault(k, {"shake": [], "motion": [], "luma": [], "std": [], "sharp": [], "faces": 0})
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
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
                        "sharpness": mean(x["sharp"]) if x["sharp"] else 0.0, "faces": int(x["faces"])})
    bounds = [0.0] + sorted({round(c, 3) for c in cuts if 0.3 < c < dur - 0.3}) + [round(dur, 3)]
    shots = [{"idx": j, "start": bounds[j], "end": bounds[j + 1]} for j in range(len(bounds) - 1)]
    return {"metrics": metrics, "shots": shots, "saturation": float(np.mean(sats)) if sats else None,
            "frames": i, "seconds": time.time() - t_start}


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
    is_log = row["is_log"]
    # 10-bit footage that is flat and nearly colourless is almost always a log profile
    if not is_log and _is_high_bit(row["pix_fmt"]) and res["saturation"] is not None:
        med_contrast = statistics.median([m["contrast"] for m in res["metrics"]] or [99])
        if res["saturation"] < LOG_SAT_MAX and med_contrast < LOG_CONTRAST_MAX:
            is_log = 1
    con.execute("UPDATE clips SET status='analyzed', is_log=?, error=NULL, updated=? WHERE id=?",
                (is_log, time.time(), cid))


def analyze_all(cfg, con, limit=None, max_jobs=None):
    """Shots + quality metrics for every proxied clip that has not been analyzed yet."""
    rows = con.execute("SELECT * FROM clips WHERE status='proxied' AND proxy IS NOT NULL ORDER BY rel").fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("analyze: nothing to do", flush=True)
        return
    if _face_cascade() is None:
        print("  ! no Haar face cascade in this OpenCV build (install opencv-python-headless<5); faces = 0", flush=True)
    n = max(1, min(cfg.n_workers(), max_jobs) if max_jobs else cfg.n_workers())
    t = time.time()
    total = sum(r["duration"] or 0 for r in rows)
    ok = err = 0
    with ProcessPoolExecutor(n) as ex:
        futs = {ex.submit(analyze_proxy, cfg.ffmpeg, r["proxy"], r["duration"] or 0, cfg.ffprobe): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            try:
                res = f.result()
                _store(con, r["id"], res, r)
                ok += 1
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
