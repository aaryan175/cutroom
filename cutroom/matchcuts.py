"""Match-cut mining across the whole library (cutroom matchcuts).

A match cut hides a jump in time or place inside a frame that lines up: same structure, same framing,
ideally the camera still, so the change reads as a glitch in time instead of a cut (rules 38, 40).

Pipeline (all on the PROXIES, OpenCV + numpy only):
  1. sample   moments near clip starts/ends, around shot boundaries, and every EVERY_S inside long clips
  2. describe each moment:
       edge     log Sobel gradient magnitude at 64x36, blurred and z-scored: structure only, so a day
                frame and a night frame of the same set-up still correlate (raw luma would invert)
       layout   8-bin gradient-orientation histograms on a 8x4 grid (a coarse HOG), z-scored
       motion   dominant optical-flow vector into the moment (Farneback, 4 fps)
       + luma, sharpness, a 16x9 colour thumbnail and a 64x36 grey thumbnail
     cached per clip in <work>/cache/matchcuts/feat/<clip>.npz
  3. search   chunked matrix products over the unit descriptors (top-k per row, never an n x n matrix;
              ~50k moments stay well under 2 GB), only pairs from DIFFERENT clips of the same orientation
  4. label    daynight (structure matches, luma ratio > 1.6), motion (both moving the same way),
              setup (same place: colour also matches), graphic (lines match across different scenes)
  5. align    ORB + RANSAC similarity on equalised frames (the crop that makes B sit on A), falling back
              to ECC on gradient images; JSON per pair in <work>/cache/matchcuts/
  6. store    matchcuts table (a, ta, b, tb, score, kind); print the best candidates

`preview_pair(cfg, con, a, ta, b, tb)` renders a 3-up still (A | B aligned | 50 % blend) and a 2-second
test clip (1 s of A ending at ta, then 1 s of B from tb) into <work>/cache/matchcuts/previews/.
"""
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import db

FEAT_VERSION = 2
SAMPLE_FPS = 4.0          # decode rate for sampling (motion uses the previous decoded frame)
EVERY_S = 4.0             # one moment every N seconds inside long clips
MAX_PER_CLIP = 80
EDGE_W, EDGE_H = 64, 36
GRID_W, GRID_H, NBINS = 8, 4, 8
W_EDGE, W_LAYOUT = 0.65, 0.35
TOPK = 5
CHUNK = 512
MIN_TEXTURE = 6.0         # grey std below this = flat frame (sky, wall, black): never a candidate
PER_PAIR = 2              # time-distinct candidates kept per clip pair
PER_MOMENT = 3            # a clip moment may appear in at most this many candidates
ALIGN_TOP = 300           # candidates that get an alignment estimate
MAX_JOBS = 3              # concurrent ffmpeg decodes


# ==================================================================================================
# features
# ==================================================================================================

def _zunit(v):
    v = v.astype(np.float32).ravel()
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


def edge_vec(grey, size=(EDGE_W, EDGE_H)):
    """Illumination-robust structure map: log gradient magnitude, blurred, zero-mean unit vector."""
    import cv2
    g = cv2.GaussianBlur(grey.astype(np.float32), (0, 0), 1.0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    m = np.log1p(np.hypot(gx, gy))
    m = cv2.resize(m, size, interpolation=cv2.INTER_AREA)
    m = cv2.GaussianBlur(m, (0, 0), 0.8)          # tolerate a pixel or two of drift
    return _zunit(m)


def layout_vec(grey, grid=(GRID_W, GRID_H), nb=NBINS):
    """Coarse HOG: per cell, gradient-orientation histogram weighted by log magnitude (sign-free)."""
    import cv2
    g = grey.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    mag = np.log1p(np.hypot(gx, gy))
    ang = (np.arctan2(gy, gx) % np.pi) / np.pi * nb
    b = np.minimum(ang.astype(np.int32), nb - 1)
    h, w = g.shape
    gw, gh = grid if w >= h else grid[::-1]
    ys = (np.arange(h) * gh // h)[:, None]
    xs = (np.arange(w) * gw // w)[None, :]
    cell = (ys * gw + xs) * nb + b
    hist = np.bincount(cell.ravel(), weights=mag.ravel(), minlength=gw * gh * nb).reshape(gw * gh, nb)
    hist /= (np.linalg.norm(hist, axis=1, keepdims=True) + 1e-6)
    return _zunit(hist)


def describe(bgr, prev_grey=None):
    """Feature dict for one frame (bgr at ~160 px wide)."""
    import cv2
    grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    land = grey.shape[1] >= grey.shape[0]
    esz = (EDGE_W, EDGE_H) if land else (EDGE_H, EDGE_W)
    small = cv2.resize(grey, esz, interpolation=cv2.INTER_AREA)
    mv = (0.0, 0.0)
    if prev_grey is not None and prev_grey.shape == grey.shape:
        a = cv2.resize(prev_grey, (grey.shape[1] // 2, grey.shape[0] // 2))
        b = cv2.resize(grey, (grey.shape[1] // 2, grey.shape[0] // 2))
        fl = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 9, 3, 5, 1.1, 0)
        med = np.median(fl.reshape(-1, 2), axis=0) * 2.0
        mv = (float(med[0]), float(med[1]))
    return {"edge": edge_vec(grey, esz), "layout": layout_vec(grey), "grey": small,
            "rgb": cv2.resize(bgr, (16, 9) if land else (9, 16), interpolation=cv2.INTER_AREA),
            "luma": float(grey.mean()), "std": float(small.std()),
            "sharp": float(cv2.Laplacian(grey, cv2.CV_32F).var()), "motion": mv, "land": land}


def sample_times(duration, shots=(), every=EVERY_S, cap=MAX_PER_CLIP) -> list:
    """Moments worth matching: near the start and end, both sides of shot boundaries, every `every` s."""
    d = float(duration or 0)
    if d < 1.0:
        return []
    ts = [0.5, 1.25, d - 1.25, d - 0.5]
    for s in shots or []:
        st = float(s)
        if 0.6 < st < d - 0.6:
            ts += [st - 0.5, st + 0.5]
    if d > 2 * every:
        ts += list(np.arange(every, d - 1.0, every))
    ts = sorted({round(min(max(0.3, t), d - 0.3), 2) for t in ts})
    out = []
    for t in ts:
        if not out or t - out[-1] >= 0.4:
            out.append(t)
    if len(out) > cap:
        idx = np.linspace(0, len(out) - 1, cap).round().astype(int)
        out = [out[i] for i in sorted(set(idx))]
    return out


def _decode_samples(cfg, src, times, width=160):
    """Decode `src` at SAMPLE_FPS and return {t: (bgr, prev_grey)} for the requested times."""
    import cv2
    r = subprocess.run([getattr(cfg, "ffprobe", "ffprobe"), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:stream_side_data=rotation", "-of", "json", str(src)],
                       capture_output=True, text=True)
    st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    pw, ph = int(st.get("width") or 16), int(st.get("height") or 9)
    rot = 0
    for sd in st.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", 0) or 0)
    if abs(rot) in (90, 270):
        pw, ph = ph, pw
    w = width if pw >= ph else int(round(width * pw / ph / 2) * 2)
    h = int(round(w * ph / pw / 2) * 2) if pw >= ph else width
    want = {int(round(t * SAMPLE_FPS)): t for t in times}
    last = max(want) if want else 0
    cmd = [getattr(cfg, "ffmpeg", "ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src), "-an",
           "-vf", f"fps={SAMPLE_FPS},scale={w}:{h}:flags=area", "-frames:v", str(last + 2),
           "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fs = w * h * 3
    out, prev, i = {}, None, 0
    while True:
        buf = proc.stdout.read(fs)
        if len(buf) < fs:
            break
        if i in want or (i + 1) in want:
            fr = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            if i in want:
                out[want[i]] = (fr.copy(), prev)
            prev = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        else:
            prev = None
        i += 1
    proc.stdout.close()
    proc.wait()
    # times past the decoded end (rounding at the tail) reuse the last decoded frame
    return out


def clip_features(cfg, row, shots=()) -> dict:
    """Cached per-clip sample features."""
    cache = Path(cfg.work) / "cache" / "matchcuts" / "feat" / f"{row['id']}.npz"
    src = row["proxy"] if row["proxy"] and os.path.exists(row["proxy"]) else row["path"]
    key = f"{FEAT_VERSION}|{src}|{os.path.getmtime(src) if os.path.exists(src) else 0}|{EVERY_S}"
    if cache.exists():
        try:
            with np.load(cache, allow_pickle=False) as z:
                if str(z["key"]) == key:
                    return {k: z[k] for k in z.files}
        except Exception:  # noqa: BLE001 - stale/corrupt cache: rebuild
            pass
    times = sample_times(row["duration"], shots)
    if not times:
        return None
    got = _decode_samples(cfg, src, times)
    feats = []
    for t in times:
        if t in got:
            bgr, prev = got[t]
            f = describe(bgr, prev)
            f["t"] = t
            feats.append(f)
    if not feats:
        return None
    res = {"key": np.array(key), "t": np.array([f["t"] for f in feats], np.float32),
           "edge": np.stack([f["edge"] for f in feats]).astype(np.float16),
           "layout": np.stack([f["layout"] for f in feats]).astype(np.float16),
           "grey": np.stack([f["grey"] for f in feats]).astype(np.uint8),
           "rgb": np.stack([f["rgb"] for f in feats]).astype(np.uint8),
           "luma": np.array([f["luma"] for f in feats], np.float32),
           "std": np.array([f["std"] for f in feats], np.float32),
           "sharp": np.array([f["sharp"] for f in feats], np.float32),
           "motion": np.array([f["motion"] for f in feats], np.float32).reshape(-1, 2),
           "land": np.array(feats[0]["land"])}
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.stem + ".tmp.npz")
    np.savez_compressed(tmp, **res)
    os.replace(tmp, cache)
    return res


# ==================================================================================================
# search
# ==================================================================================================

def _descriptors(E, L):
    return np.concatenate([np.sqrt(W_EDGE) * E, np.sqrt(W_LAYOUT) * L], axis=1).astype(np.float32)


def search_pairs(D, clip_idx, ok, k=TOPK, floor=0.5, chunk=CHUNK):
    """Top-k partners per row from other clips, by dot product of unit descriptors, in row chunks so
    memory stays at chunk x N floats. Returns {(i, j): score} with i < j."""
    n = len(D)
    out = {}
    cols_ok = ok.copy()
    for s0 in range(0, n, chunk):
        rows = np.arange(s0, min(n, s0 + chunk))
        rows = rows[ok[rows]]
        if not len(rows):
            continue
        with np.errstate(all="ignore"):              # Accelerate BLAS raises spurious FP flags
            S = D[rows] @ D.T                            # chunk x N float32
        S[clip_idx[rows][:, None] == clip_idx[None, :]] = -9
        S[:, ~cols_ok] = -9
        kk = min(k, n - 1)
        if kk <= 0:
            continue
        part = np.argpartition(-S, kk - 1, axis=1)[:, :kk]
        for r, i in enumerate(rows):
            for j in part[r]:
                s = float(S[r, j])
                if s >= floor:
                    key = (int(min(i, j)), int(max(i, j)))
                    if s > out.get(key, -1):
                        out[key] = s
        del S
    return out


def _null_stats(D, clip_idx, ok, n_pairs=20000, seed=0):
    """Median and robust spread of scores between random moments of different clips (the cross-shot null)."""
    rng = np.random.default_rng(seed)
    idx = np.where(ok)[0]
    if len(idx) < 3:
        return 0.0, 1.0
    a = rng.choice(idx, n_pairs)
    b = rng.choice(idx, n_pairs)
    m = clip_idx[a] != clip_idx[b]
    if not m.any():
        return 0.0, 1.0
    with np.errstate(all="ignore"):
        s = np.einsum("ij,ij->i", D[a[m]], D[b[m]])
    med = float(np.median(s))
    mad = float(np.median(np.abs(s - med))) * 1.4826
    return med, max(mad, 0.02)


def classify(fa, fb, edge_corr, layout_corr):
    """(kind, detail) for a structurally matching pair of moments."""
    la, lb = float(fa["luma"]), float(fb["luma"])
    ratio = max(la, lb) / max(1.0, min(la, lb))
    va, vb = np.asarray(fa["motion"], np.float64), np.asarray(fb["motion"], np.float64)
    ma, mb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    cos = float(va @ vb / (ma * mb)) if ma > 1e-6 and mb > 1e-6 else 0.0
    ra, rb = fa["rgb"].astype(np.float64).ravel(), fb["rgb"].astype(np.float64).ravel()
    ra, rb = ra - ra.mean(), rb - rb.mean()
    col = float(ra @ rb / (np.linalg.norm(ra) * np.linalg.norm(rb) + 1e-6))
    pix = float(np.abs(fa["grey"].astype(np.float32) - fb["grey"].astype(np.float32)).mean())
    detail = {"edge": round(edge_corr, 3), "layout": round(layout_corr, 3), "luma_ratio": round(ratio, 2),
              "colour": round(col, 3), "pix": round(pix, 1), "motion_a": [round(x, 2) for x in va.tolist()],
              "motion_b": [round(x, 2) for x in vb.tolist()], "motion_cos": round(cos, 2)}
    if ratio > 1.6:
        kind = "daynight"
    elif ma > 1.5 and mb > 1.5 and cos > 0.9:
        kind = "motion"
    elif col > 0.8:
        kind = "setup"
    else:
        kind = "graphic"
    return kind, detail


# ==================================================================================================
# alignment
# ==================================================================================================

def _grab(cfg, src, t, width=640):
    """One BGR frame (width px wide) at t seconds."""
    cmd = [getattr(cfg, "ffmpeg", "ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{max(0.0, float(t)):.3f}", "-i", str(src), "-frames:v", "1",
           "-vf", f"scale={width}:-2:flags=area", "-pix_fmt", "bgr24", "-f", "image2pipe", "-vcodec", "bmp", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode or not r.stdout:
        return None
    import cv2
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def _crop_for(M, w, h):
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    p = corners @ M[:, :2].T + M[:, 2]
    x0, y0 = p.min(0)
    x1, y1 = p.max(0)
    return float(x0 / w), float(y0 / h), float((x1 - x0) / w), float((y1 - y0) / h)


def _crop_result(M, w, h, method, **extra):
    """Turn an A->B similarity (A coords to B coords) into the crop that makes the two frames line up."""
    import cv2
    s = float(np.sqrt(M[0, 0] ** 2 + M[1, 0] ** 2))
    rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    base = {"ok": True, "method": method, "scale": round(s, 4), "rot": round(rot, 2),
            "tx": round(float(M[0, 2]) / w, 4), "ty": round(float(M[1, 2]) / h, 4), "M": M.tolist(), **extra}
    if abs(rot) > 3:
        return {**base, "ok": False, "why": "rotation"}
    if 0.97 < s < 1.03 and max(abs(M[0, 2]), abs(M[1, 2])) < 0.02 * w:
        return {**base, "crop_side": None, "crop": [0, 0, 1, 1], "zoom": 1.0}
    for side, MM in (("B", M), ("A", cv2.invertAffineTransform(M))):
        x, y, cw, ch = _crop_for(MM, w, h)
        if x >= -0.01 and y >= -0.01 and x + cw <= 1.01 and y + ch <= 1.01 and cw >= 0.5:
            return {**base, "crop_side": side, "crop": [round(max(0, x), 4), round(max(0, y), 4),
                                                         round(min(cw, 1), 4), round(min(ch, 1), 4)],
                    "zoom": round(1 / cw, 3)}
    return {**base, "ok": False, "why": "no-shared-window"}


def _grad(g):
    import cv2
    g = cv2.GaussianBlur(g.astype(np.float32), (0, 0), 1.2)
    m = np.log1p(np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)))
    return ((m - m.mean()) / (m.std() + 1e-6)).astype(np.float32)


def align_frames(fa, fb) -> dict:
    """Similarity transform taking A's pixels onto B's (ORB + RANSAC on equalised grey, ECC fallback),
    plus the crop/zoom that makes the pair line up. Works for day/night pairs (equalised + gradients)."""
    import cv2
    if fa is None or fb is None:
        return {"ok": False, "why": "no-frames"}
    if fa.shape != fb.shape:
        fb = cv2.resize(fb, (fa.shape[1], fa.shape[0]))
    ga = cv2.equalizeHist(cv2.cvtColor(fa, cv2.COLOR_BGR2GRAY))
    gb = cv2.equalizeHist(cv2.cvtColor(fb, cv2.COLOR_BGR2GRAY))
    h, w = ga.shape
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(ga, None)
    kb, dbb = orb.detectAndCompute(gb, None)
    if da is not None and dbb is not None and len(ka) >= 30 and len(kb) >= 30:
        ms = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, dbb, k=2)
        good = [m for m, n2 in (x for x in ms if len(x) == 2) if m.distance < 0.8 * n2.distance]
        if len(good) >= 20:
            pa = np.float32([ka[m.queryIdx].pt for m in good])
            pb = np.float32([kb[m.trainIdx].pt for m in good])
            M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4, maxIters=4000)
            if M is not None and int(inl.sum()) >= 18:
                r = _crop_result(M, w, h, "orb", inliers=int(inl.sum()), matches=len(good))
                if r.get("ok"):
                    return r
    # ECC on gradient maps (illumination robust), half size
    a = _grad(cv2.resize(ga, (w // 2, h // 2)))
    b = _grad(cv2.resize(gb, (w // 2, h // 2)))
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        cc, warp = cv2.findTransformECC(a, b, warp, cv2.MOTION_AFFINE,
                                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5), None, 5)
    except cv2.error:
        return {"ok": False, "why": "ecc-failed"}
    # ECC warp maps B coords -> A coords (template a, input b); invert to A -> B, then back to full size
    M = cv2.invertAffineTransform(warp).astype(np.float64)
    M[:, 2] *= 2.0
    if cc < 0.3:
        return {"ok": False, "why": "ecc-weak", "ecc": round(float(cc), 3)}
    return _crop_result(M, w, h, "ecc", ecc=round(float(cc), 3))


def _align_path(cfg, a, ta, b, tb) -> Path:
    return Path(cfg.work) / "cache" / "matchcuts" / f"{a}_{ta:.2f}__{b}_{tb:.2f}.json"


# ==================================================================================================
# mine
# ==================================================================================================

def mine(cfg, con, limit=None, min_score=0.75, top=25, align_top=ALIGN_TOP, quiet=False) -> list:
    """Mine candidate pairs and store them in the matchcuts table. `limit` = max clips to process."""
    t0 = time.time()
    rows = con.execute("SELECT * FROM clips WHERE status IN ('proxied','analyzed') AND (proxy IS NOT NULL OR path IS NOT NULL) "
                       "AND duration > 1 ORDER BY rel").fetchall()
    if limit:
        rows = rows[:int(limit)]
    if len(rows) < 2:
        if not quiet:
            print("matchcuts: need at least two proxied clips", flush=True)
        return []
    shots = {}
    for r in con.execute("SELECT clip_id, start FROM shots WHERE idx > 0"):
        shots.setdefault(r["clip_id"], []).append(r["start"])

    feats = {}

    def work(r):
        try:
            return r["id"], clip_features(cfg, r, shots.get(r["id"], ()))
        except Exception as e:  # noqa: BLE001 - one broken proxy must not stop the miner
            return r["id"], e

    with ThreadPoolExecutor(max(1, min(MAX_JOBS, cfg.n_workers() if hasattr(cfg, "n_workers") else 2))) as ex:
        for k, (cid, f) in enumerate(ex.map(work, rows), 1):
            if isinstance(f, Exception):
                if not quiet:
                    print(f"  ! {cid}: {f}", flush=True)
            elif f is not None:
                feats[cid] = f
            if not quiet and k % 100 == 0:
                print(f"  features {k}/{len(rows)}", flush=True)
    by_id = {r["id"]: r for r in rows}

    # flatten
    cids = sorted(feats)
    T, CI, LAND = [], [], []
    E, L = [], []
    for ci, cid in enumerate(cids):
        f = feats[cid]
        m = len(f["t"])
        T.append(f["t"]); CI.append(np.full(m, ci)); LAND.append(np.full(m, bool(f["land"])))
        E.append(f["edge"]); L.append(f["layout"])
    if not T:
        return []
    T = np.concatenate(T); CI = np.concatenate(CI); LAND = np.concatenate(LAND)
    offs = np.cumsum([0] + [len(feats[c]["t"]) for c in cids])
    STD = np.concatenate([feats[c]["std"] for c in cids])
    SHARP = np.concatenate([feats[c]["sharp"] for c in cids])
    sharp_floor = float(np.percentile(SHARP, 10)) if len(SHARP) > 20 else 0.0
    ok_all = (STD >= MIN_TEXTURE) & (SHARP >= sharp_floor)

    def moment(i):
        ci = int(CI[i]); f = feats[cids[ci]]; j = i - offs[ci]
        return {"luma": f["luma"][j], "motion": f["motion"][j], "rgb": f["rgb"][j], "grey": f["grey"][j]}

    cands = []
    null = {}
    for land in (True, False):
        idx = np.where(LAND == land)[0]
        if len(idx) < 2:
            continue
        # descriptor matrix for this orientation (edge maps differ in shape per orientation)
        # (float16 sources, one float32 matrix: ~50k moments x 2560 dims = 0.5 GB)
        order = sorted(set(CI[idx].tolist()))
        Eo = np.concatenate([feats[cids[c]]["edge"] for c in order])
        Lo = np.concatenate([feats[cids[c]]["layout"] for c in order])
        D = np.empty((len(idx), Eo.shape[1] + Lo.shape[1]), np.float32)
        D[:, :Eo.shape[1]] = Eo
        D[:, :Eo.shape[1]] *= np.float32(np.sqrt(W_EDGE))
        D[:, Eo.shape[1]:] = Lo
        D[:, Eo.shape[1]:] *= np.float32(np.sqrt(W_LAYOUT))
        D /= (np.linalg.norm(D, axis=1, keepdims=True) + 1e-6)
        ci_o, ok_o = CI[idx], ok_all[idx]
        med, spread = _null_stats(D, ci_o, ok_o)
        null[("landscape" if land else "portrait")] = {"median": round(med, 3), "spread": round(spread, 3)}
        pairs = search_pairs(D, ci_o, ok_o, floor=min(min_score, 0.9) - 0.05)
        for (i, j), s in pairs.items():
            if s < min_score:
                continue
            gi, gj = int(idx[i]), int(idx[j])
            ec = float(np.dot(Eo[i].astype(np.float64), Eo[j])); lc = float(np.dot(Lo[i].astype(np.float64), Lo[j]))
            fa, fb = moment(gi), moment(gj)
            if float(np.abs(fa["grey"].astype(np.float32) - fb["grey"].astype(np.float32)).mean()) < 2.0 \
                    and abs(fa["luma"] - fb["luma"]) < 3:
                continue          # literally the same image (duplicate file / copy): useless as a cut
            kind, det = classify(fa, fb, ec, lc)
            # direction: daynight goes bright -> dark; otherwise earlier library order first
            a_, b_ = (gi, gj)
            if kind == "daynight" and fa["luma"] < fb["luma"]:
                a_, b_ = gj, gi
            det["z"] = round((s - med) / spread, 1)
            # motion that fights itself across the cut reads as a jolt
            va, vb = moment(a_)["motion"], moment(b_)["motion"]
            na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
            score = s
            if na > 1.5 and nb > 1.5 and float(va @ vb) / (na * nb) < -0.3:
                score -= 0.1
                det["motion_conflict"] = True
            cands.append({"a": cids[int(CI[a_])], "ta": round(float(T[a_]), 2), "b": cids[int(CI[b_])],
                          "tb": round(float(T[b_]), 2), "score": round(float(score), 4), "kind": kind, "detail": det})

    # dedupe: <= PER_PAIR time-distinct pairs per clip pair, each moment used <= PER_MOMENT times
    cands.sort(key=lambda c: -c["score"])
    seen, use, final = {}, {}, []
    for c in cands:
        key = tuple(sorted((c["a"], c["b"])))
        lst = seen.setdefault(key, [])
        if len(lst) >= PER_PAIR or any(abs(c["ta"] - x) < 2.0 and abs(c["tb"] - y) < 2.0 for x, y in lst):
            continue
        ka, kb = (c["a"], int(c["ta"] // 2)), (c["b"], int(c["tb"] // 2))
        if use.get(ka, 0) >= PER_MOMENT or use.get(kb, 0) >= PER_MOMENT:
            continue
        lst.append((c["ta"], c["tb"]))
        use[ka] = use.get(ka, 0) + 1
        use[kb] = use.get(kb, 0) + 1
        final.append(c)

    # alignment for the best candidates
    def src_of(cid):
        r = by_id[cid]
        return r["proxy"] if r["proxy"] and os.path.exists(r["proxy"]) else r["path"]

    def do_align(c):
        p = _align_path(cfg, c["a"], c["ta"], c["b"], c["tb"])
        al = align_frames(_grab(cfg, src_of(c["a"]), c["ta"]), _grab(cfg, src_of(c["b"]), c["tb"]))
        c["align"] = {k: v for k, v in al.items() if k != "M"} | {"M": al.get("M")}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**{k: c[k] for k in ("a", "ta", "b", "tb", "score", "kind", "detail")},
                                 "a_path": by_id[c["a"]]["path"], "b_path": by_id[c["b"]]["path"],
                                 "align": al}, indent=1, default=float))
        return c

    with ThreadPoolExecutor(MAX_JOBS) as ex:
        list(ex.map(do_align, final[:align_top]))

    if limit is None:
        con.execute("DELETE FROM matchcuts")
    else:
        ids = list(feats)
        q = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM matchcuts WHERE a IN ({q}) AND b IN ({q})", ids + ids)
    con.executemany("INSERT OR REPLACE INTO matchcuts (a, ta, b, tb, score, kind) VALUES (?,?,?,?,?,?)",
                    [(c["a"], c["ta"], c["b"], c["tb"], c["score"], c["kind"]) for c in final])
    con.commit()

    if not quiet:
        n_mom = int(len(T))
        print(f"matchcuts: {len(cids)} clips · {n_mom} moments · {len(cands)} raw · {len(final)} kept "
              f"(min score {min_score}) · null {null} · {time.time() - t0:.1f}s", flush=True)
        for c in final[:top]:
            al = c.get("align") or {}
            how = (f"zoom {al.get('zoom')} crop {al.get('crop_side') or '-'} ({al.get('method')})" if al.get("ok")
                   else f"no alignment ({al.get('why', 'not tried')})")
            ra, rb = by_id[c["a"]]["rel"] or c["a"], by_id[c["b"]]["rel"] or c["b"]
            print(f"  {c['score']:.3f} {c['kind']:9s} {ra} @ {c['ta']:.2f}s  ->  {rb} @ {c['tb']:.2f}s   "
                  f"z {c['detail'].get('z')} · {how}", flush=True)
        if final:
            c = final[0]
            print(f"  preview: python -c \"from cutroom import matchcuts as m, db; from cutroom.config import Config; "
                  f"c=Config.load(); print(m.preview_pair(c, db.connect(c.db_path), '{c['a']}', {c['ta']}, "
                  f"'{c['b']}', {c['tb']}))\"", flush=True)
    return final


# ==================================================================================================
# preview
# ==================================================================================================

def preview_pair(cfg, con, a, ta, b, tb, pre=1.0, post=1.0, height=720, fps=25) -> dict:
    """2-up/3-up still (A | B aligned | 50 % blend) and a short test clip of the cut (A ends at ta,
    B starts at tb, alignment crop applied) in <work>/cache/matchcuts/previews/."""
    import cv2
    ra, rb = db.get_clip(con, a), db.get_clip(con, b)
    sa = ra["proxy"] if ra["proxy"] and os.path.exists(ra["proxy"]) else ra["path"]
    sb = rb["proxy"] if rb["proxy"] and os.path.exists(rb["proxy"]) else rb["path"]
    ta, tb = float(ta), float(tb)
    out_dir = Path(cfg.work) / "cache" / "matchcuts" / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{ra['id']}_{ta:.2f}__{rb['id']}_{tb:.2f}"
    ap = _align_path(cfg, ra["id"], ta, rb["id"], tb)
    if ap.exists():
        al = json.loads(ap.read_text()).get("align", {})
    else:
        al = align_frames(_grab(cfg, sa, ta), _grab(cfg, sb, tb))
    # last frame of A (just before ta) and first frame of B (at tb)
    fa = _grab(cfg, sa, max(0.0, ta - 1.0 / fps), width=640)
    fb = _grab(cfg, sb, tb, width=640)
    if fa is None or fb is None:
        raise RuntimeError("could not decode the preview frames")
    fb = cv2.resize(fb, (fa.shape[1], fa.shape[0]))
    h, w = fa.shape[:2]

    def apply(img, side_name):
        if not al.get("ok") or al.get("crop_side") != side_name:
            return img
        x, y, cw, ch = al["crop"]
        x0, y0 = int(x * w), int(y * h)
        x1, y1 = max(x0 + 2, int((x + cw) * w)), max(y0 + 2, int((y + ch) * h))
        return cv2.resize(img[y0:y1, x0:x1], (w, h), interpolation=cv2.INTER_CUBIC)

    A2, B2 = apply(fa, "A"), apply(fb, "B")
    blend = cv2.addWeighted(A2, 0.5, B2, 0.5, 0)
    strip = np.hstack([A2, np.zeros((h, 6, 3), np.uint8), B2, np.zeros((h, 6, 3), np.uint8), blend])
    for k, label in enumerate((f"A {ra['id']} @ {ta:.2f}s (last frame)", f"B {rb['id']} @ {tb:.2f}s (first frame)",
                               "50% blend")):
        cv2.putText(strip, label, (10 + k * (w + 6), 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(strip, label, (10 + k * (w + 6), 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 230, 255), 2, cv2.LINE_AA)
    still = out_dir / f"{stem}.jpg"
    cv2.imwrite(str(still), strip, [cv2.IMWRITE_JPEG_QUALITY, 88])

    # test clip: frame-exact lengths, alignment crop on the tighter side
    oh = int(height) // 2 * 2
    ow = int(round(oh * w / h / 2)) * 2
    na, nb = max(1, int(round(pre * fps))), max(1, int(round(post * fps)))

    def chain(side_name, n_frames):
        f = ""
        if al.get("ok") and al.get("crop_side") == side_name:
            x, y, cw, ch = al["crop"]
            f = f"crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{x:.4f}:ih*{y:.4f},"
        return (f"{f}scale={ow}:{oh}:flags=lanczos,setsar=1,fps={fps},format=yuv420p,"
                f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={n_frames},setpts=PTS-STARTPTS")

    clip = out_dir / f"{stem}.mp4"
    cmd = [getattr(cfg, "ffmpeg", "ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-ss", f"{max(0.0, ta - na / fps):.3f}", "-t", f"{na / fps + 0.5:.3f}", "-i", str(sa),
           "-ss", f"{tb:.3f}", "-t", f"{nb / fps + 0.5:.3f}", "-i", str(sb),
           "-filter_complex", f"[0:v]{chain('A', na)}[a];[1:v]{chain('B', nb)}[b];[a][b]concat=n=2:v=1:a=0[v]",
           "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart",
           str(clip)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"preview render failed: {r.stderr[-600:]}")
    return {"still": str(still), "clip": str(clip), "align": {k: v for k, v in al.items() if k != "M"}}
