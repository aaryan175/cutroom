"""Film / lens character: measure it on frames, rebuild it as an ffmpeg chain.

The grade decides colour. This module covers what a camera, lens and delivery leave on the picture on
top of colour: grain, softness, vignette falloff, halation and bloom around highlights, chromatic fringing
at the frame edges, highlight roll-off, black lift and gate weave. Two halves:

  measure_frame(rgb) / measure_frames([...]) -> numbers (0-255 units unless named)
      grain_std     std of the fine residual (Y minus a 1.5 px blur) in flat mid-tone areas
      grain_corr    lag-1 autocorrelation of that residual (0 = per-pixel noise, ~0.5+ = clumpy / big grain)
      sharpness     share of gradient energy a 1 px blur removes (higher = crisper; scaled-up proxies are low)
      vignette      corner luma / centre luma (1.0 = flat field)
      halation      red excess in a 3-20 px ring around clipped highlights minus the excess further out
      bloom         luma excess in that ring (glow around highlights, any colour)
      ca            colour-edge mismatch (|grad R - grad B| / |grad G|) at the frame edges / at the centre
      p1, p99, p999, clip_pct     black point, highlight roll-off, share of pixels >= 250
      weave_px      frame-to-frame jitter (px at 1080) after removing the smooth camera path (burst input)

  chain(film, W, H) -> ffmpeg filters that ADD a measured character to a graded picture:
      {"grain": 0-12 (std in 0-255), "grain_size": 1-3 px, "soften": 0-2 (gaussian sigma px at 1080),
       "vignette": 0-0.6 (corner darkening, fraction), "halation": 0-1, "bloom": 0-1, "ca": 0-3 (px at the
       corners), "rolloff": {"knee": 0.8, "ceiling": 0.86}, "black": 0-0.1 (black lift), "seed": int}
      Every key defaults to off. Only what a reference measurably has belongs in a plan (rule 38).

  match(ref_stats, ours_stats) -> film dict that moves ours toward the reference, each value bounded,
      with a "why" per key. Keys the reference does not show stay off.
"""
import math

import numpy as np

FILM_KEYS = ("grain", "grain_size", "soften", "vignette", "halation", "bloom", "ca", "rolloff", "black", "seed")
LIMITS = {"grain": (0.0, 12.0), "grain_size": (1.0, 3.0), "soften": (0.0, 2.0), "vignette": (0.0, 0.6),
          "halation": (0.0, 1.0), "bloom": (0.0, 1.0), "ca": (0.0, 3.0), "black": (0.0, 0.1)}


# ============================================================================== measurement
def _y(rgb):
    a = rgb.astype(np.float32)
    return 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]


def _blur(x, s):
    import cv2
    return cv2.GaussianBlur(x, (0, 0), s)


def _grad(x):
    import cv2
    gx = cv2.Sobel(x, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(x, cv2.CV_32F, 0, 1, ksize=3)
    return np.sqrt(gx * gx + gy * gy)


def _to1080(rgb):
    import cv2
    h = rgb.shape[0]
    if h == 1080:
        return rgb
    w = int(round(rgb.shape[1] * 1080 / h))
    return cv2.resize(rgb, (w, 1080), interpolation=cv2.INTER_AREA if h > 1080 else cv2.INTER_CUBIC)


def measure_frame(rgb, window=None):
    """Film/lens numbers of one RGB uint8 frame (any size; resampled to 1080 lines). `window` =
    (x0, y0, x1, y1) fractions to measure inside (a gated or inset picture), default the whole frame."""
    rgb = _to1080(np.ascontiguousarray(rgb))
    if window:
        H, W = rgb.shape[:2]
        x0, y0, x1, y1 = window
        rgb = rgb[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]
    H, W = rgb.shape[:2]
    Y = _y(rgb)
    out = {}
    # grain: fine residual in flat mid-tones
    res = Y - _blur(Y, 1.5)
    flat = (_grad(_blur(Y, 2.0)) < 6.0) & (Y > 40) & (Y < 215)
    if flat.sum() > 2000:
        r = res[flat]
        out["grain_std"] = round(float(np.std(r)), 3)
        a, b = res[:, :-1], res[:, 1:]
        m = flat[:, :-1] & flat[:, 1:]
        if m.sum() > 2000:
            out["grain_corr"] = round(float(np.corrcoef(a[m], b[m])[0, 1]), 3)
    # sharpness: gradient energy lost to a 1 px blur, on textured pixels only
    g0 = _grad(Y)
    g1 = _grad(_blur(Y, 1.0))
    tex = g0 > np.percentile(g0, 80)
    if tex.sum() > 500:
        out["sharpness"] = round(float(1.0 - g1[tex].mean() / max(1e-6, g0[tex].mean())), 4)
    # vignette: corners vs centre
    bw, bh = int(W * 0.12), int(H * 0.12)
    corners = np.concatenate([Y[:bh, :bw].ravel(), Y[:bh, -bw:].ravel(), Y[-bh:, :bw].ravel(), Y[-bh:, -bw:].ravel()])
    cen = Y[int(H * 0.35):int(H * 0.65), int(W * 0.35):int(W * 0.65)]
    out["vignette"] = round(float(corners.mean() / max(1.0, cen.mean())), 3)
    # halation / bloom: rings around clipped highlights
    hi = (Y >= 240).astype(np.uint8)
    if 50 < hi.sum() < 0.25 * H * W:
        import cv2
        d = cv2.distanceTransform(1 - hi, cv2.DIST_L2, 3)
        near = (d >= 3) & (d <= 20)
        far = (d >= 25) & (d <= 45)
        if near.sum() > 200 and far.sum() > 200:
            red = rgb[..., 0].astype(np.float32) - 0.5 * (rgb[..., 1].astype(np.float32) + rgb[..., 2])
            out["halation"] = round(float(red[near].mean() - red[far].mean()), 2)
            out["bloom"] = round(float(Y[near].mean() - Y[far].mean()), 2)
    # chromatic fringing: colour-edge mismatch at the edges vs the centre
    R, G, B = (rgb[..., k].astype(np.float32) for k in range(3))
    gR, gG, gB = _grad(R), _grad(G), _grad(B)
    yy, xx = np.mgrid[0:H, 0:W]
    rr = np.hypot((xx - W / 2) / (W / 2), (yy - H / 2) / (H / 2))
    strong = gG > np.percentile(gG, 92)
    mis = np.abs(gR - gB) / np.maximum(gG, 1.0)
    o, c = strong & (rr > 0.8), strong & (rr < 0.4)
    if o.sum() > 300 and c.sum() > 300:
        out["ca"] = round(float(mis[o].mean() / max(1e-6, mis[c].mean())), 3)
    # tone ends
    out["p1"] = round(float(np.percentile(Y, 1)), 2)
    out["p99"] = round(float(np.percentile(Y, 99)), 2)
    out["p999"] = round(float(np.percentile(Y, 99.9)), 2)
    out["clip_pct"] = round(float((Y >= 250).mean() * 100), 3)
    out["luma"] = round(float(Y.mean()), 2)
    mx = rgb.max(axis=2).astype(np.float32)
    mn = rgb.min(axis=2).astype(np.float32)
    out["sat"] = round(float(np.mean(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0))), 4)
    return out


def weave(frames):
    """Frame-to-frame jitter (px at 1080) of a burst of consecutive frames after removing the smooth
    camera path (a 5-frame moving average), via phase correlation of the luma."""
    import cv2
    if len(frames) < 6:
        return None
    ys = [_blur(_y(_to1080(f)), 1.0) for f in frames]
    win = np.hanning(ys[0].shape[0])[:, None] * np.hanning(ys[0].shape[1])[None, :]
    pos = [(0.0, 0.0)]
    for a, b in zip(ys, ys[1:]):
        (dx, dy), _ = cv2.phaseCorrelate(a.astype(np.float64) * win, b.astype(np.float64) * win)
        pos.append((pos[-1][0] + dx, pos[-1][1] + dy))
    p = np.asarray(pos)
    k = 5
    sm = np.vstack([np.convolve(p[:, j], np.ones(k) / k, mode="same") for j in range(2)]).T
    r = (p - sm)[k // 2:-(k // 2)]
    return round(float(np.sqrt((r ** 2).sum(axis=1).mean())), 3)


def summarize(rows):
    """Median of every numeric key over frame rows."""
    keys = sorted({k for r in rows for k in r})
    out = {}
    for k in keys:
        v = [r[k] for r in rows if r.get(k) is not None and isinstance(r[k], (int, float))]
        if v:
            out[k] = round(float(np.median(v)), 4)
    out["frames"] = len(rows)
    return out


def measure_frames(frames, window=None):
    return summarize([measure_frame(f, window) for f in frames])


def grab_frames(cfg, video, times, vf="", height=None):
    """RGB uint8 frames at `times` (seconds), decoded by ffmpeg to temporary PNGs (optional filter first)."""
    import tempfile
    from pathlib import Path
    from PIL import Image
    from . import media
    out = []
    with tempfile.TemporaryDirectory(prefix="cutroom-film-") as td:
        for k, t in enumerate(times):
            chain = ",".join(x for x in (vf, f"scale=-2:{height}:flags=lanczos" if height else "", "format=rgb24") if x)
            p = Path(td) / f"f{k}.png"
            media.ffmpeg(cfg, ["-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1", "-vf", chain, str(p)])
            out.append(np.asarray(Image.open(p).convert("RGB")))
    return out


# ============================================================================== rebuild
def normalize(film):
    """Plan value -> dict with every key (off = 0 / None), or None when everything is off."""
    if not film:
        return None
    f = {k: film.get(k) for k in FILM_KEYS if film.get(k) not in (None, False)}
    if not any(float(f.get(k) or 0) > 1e-4 for k in ("grain", "soften", "vignette", "halation", "bloom", "ca", "black")) \
            and not f.get("rolloff"):
        return None
    if film.get("from_reference"):     # provenance: the sidecar carries it so QA rule 38 can see the match
        f["from_reference"] = film["from_reference"]
    return f


def validate(film, where="style.film"):
    if film in (None, False, {}):
        return []
    if not isinstance(film, dict):
        return [f"{where} must be an object"]
    errs = []
    for k in film:
        if k not in FILM_KEYS and k not in ("why", "from_reference", "measured"):
            errs.append(f"{where}: unknown key {k!r} (known: {', '.join(FILM_KEYS)})")
    for k, (lo, hi) in LIMITS.items():
        v = film.get(k)
        if v is not None:
            try:
                ok = lo <= float(v) <= hi
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errs.append(f"{where}.{k} must be {lo:g}-{hi:g}")
    ro = film.get("rolloff")
    if ro not in (None, False):
        if not isinstance(ro, dict) or not 0.5 <= float(ro.get("knee", 0.8)) < float(ro.get("ceiling", 0.9)) <= 1.0:
            errs.append(f"{where}.rolloff must be {{\"knee\": 0.5-1, \"ceiling\": knee-1}}")
    return errs


def chain(film, W, H):
    """ffmpeg filter list (yuv420p in, yuv420p out) adding the film/lens character. [] when off."""
    f = normalize(film)
    if not f:
        return []
    s = H / 1080.0
    out = []
    rgb = []
    ro = f.get("rolloff")
    blk = float(f.get("black") or 0)
    if ro or blk > 1e-4:
        kn, ce = (float(ro.get("knee", 0.8)), float(ro.get("ceiling", 0.9))) if ro else (1.0, 1.0)
        pts = [(0.0, blk), (0.25, 0.25 + blk * 0.6)]
        if kn < 1.0:
            mid = kn + (1.0 - kn) * 0.5
            pts += [(kn, kn), (mid, kn + (ce - kn) * 0.72), (1.0, ce)]
        else:
            pts += [(1.0, 1.0)]
        rgb.append("curves=master='" + " ".join(f"{x:.4f}/{y:.4f}" for x, y in pts) + "'")
    ca = float(f.get("ca") or 0) * s
    if rgb:
        out += ["format=gbrp", *rgb, "format=yuv420p"]
    sig = float(f.get("soften") or 0) * s
    if sig > 0.05:
        out.append(f"gblur=sigma={sig:.3f}:steps=2")
    if ca > 0.05:
        # lateral chromatic aberration: red scaled up, blue scaled down about the centre (radial fringe)
        kx = ca / (W / 2)
        out.append(f"__CA__{kx:.6f}")
    halo, bloom = float(f.get("halation") or 0), float(f.get("bloom") or 0)
    if halo > 1e-3 or bloom > 1e-3:
        out.append(f"__HALO__{halo:.3f}:{bloom:.3f}")
    v = float(f.get("vignette") or 0)
    if v > 1e-3:
        # vignette angle chosen so the corners fall by about `v` (ffmpeg: corner factor ~ cos^4 of the angle)
        ang = math.acos(max(0.05, (1.0 - v)) ** 0.25)
        out.append(f"vignette=angle={ang:.4f}:mode=forward:eval=init")
    g = float(f.get("grain") or 0)
    if g > 0.05:
        out.append(f"__GRAIN__{g:.3f}:{float(f.get('grain_size') or 1.0):.2f}:{int(f.get('seed') or 0)}")
    return out


def expand(filters, label, W, H):
    """Turn a chain() list into filtergraph text from [IN] to [OUT] (the special __X__ steps need their own
    branches). Returns a list of graph chains, like render.picture()."""
    plain, parts, k = [], [], 0
    cur = "[IN]"
    for f in filters:
        if not f.startswith("__"):
            plain.append(f)
            continue
        k += 1
        nxt = f"[{label}f{k}]"
        pre = (",".join(plain) + ",") if plain else ""
        plain = []
        if f.startswith("__GRAIN__"):
            amt, size, seed = f[len("__GRAIN__"):].split(":")
            size = max(1.0, float(size))
            gw, gh = max(16, int(W / size) // 2 * 2), max(16, int(H / size) // 2 * 2)
            # luma-only grain: noise on a mid-grey layer, overlay-blended (grey 128 is the identity)
            strength = float(amt) * 1.7 * math.sqrt(size)
            parts.append(f"{cur}{pre}format=yuv420p,split[{label}g{k}a][{label}g{k}b]")
            parts.append(f"[{label}g{k}b]scale={gw}:{gh}:flags=area,lutyuv=y=128:u=128:v=128,"
                         f"noise=c0s={strength:.2f}:c0f=t+u:c0_seed={int(seed) + 7},"
                         f"scale={W}:{H}:flags=bicubic,format=yuv420p[{label}g{k}n]")
            parts.append(f"[{label}g{k}a][{label}g{k}n]blend=c0_mode=overlay:c1_expr=A:c2_expr=A,"
                         f"format=yuv420p{nxt}")
        elif f.startswith("__CA__"):
            kx = float(f[len("__CA__"):])
            rw, rh = int(round(W * (1 + kx))) // 2 * 2, int(round(H * (1 + kx))) // 2 * 2
            bw_, bh_ = int(round(W * (1 + 2 * kx))) // 2 * 2, int(round(H * (1 + 2 * kx))) // 2 * 2
            parts.append(f"{cur}{pre}format=gbrp,split=3[{label}c{k}g0][{label}c{k}b][{label}c{k}r]")
            parts.append(f"[{label}c{k}g0]setsar=1[{label}c{k}g]")
            parts.append(f"[{label}c{k}r]scale={bw_}:{bh_}:flags=bicubic,crop={W}:{H},setsar=1[{label}c{k}r2]")
            parts.append(f"[{label}c{k}b]scale={rw}:{rh}:flags=bicubic,crop={W}:{H},setsar=1[{label}c{k}b2]")
            # gbrp planes: 0 = G, 1 = B, 2 = R
            parts.append(f"[{label}c{k}g][{label}c{k}b2][{label}c{k}r2]mergeplanes=map0s=0:map0p=0:map1s=1:map1p=1:"
                         f"map2s=2:map2p=2:format=gbrp,format=yuv420p{nxt}")
        elif f.startswith("__HALO__"):
            halo, bloom = (float(x) for x in f[len("__HALO__"):].split(":"))
            sg = max(4.0, 14.0 * H / 1080.0)
            parts.append(f"{cur}{pre}format=gbrp,split[{label}h{k}a][{label}h{k}b]")
            # highlights only (above ~0.8), blurred, tinted red for halation, screened back on
            rgain, ggain, bgain = 0.9 * halo + 0.5 * bloom, 0.25 * halo + 0.5 * bloom, 0.12 * halo + 0.5 * bloom
            parts.append(f"[{label}h{k}b]curves=all='0/0 0.78/0 1/1',gblur=sigma={sg:.1f},"
                         f"colorchannelmixer=rr={rgain:.3f}:gg={ggain:.3f}:bb={bgain:.3f}[{label}h{k}c]")
            parts.append(f"[{label}h{k}a][{label}h{k}c]blend=all_mode=screen,format=yuv420p{nxt}")
        cur = nxt
    tail = ",".join(plain)
    parts.append(f"{cur}{tail + ',' if tail else ''}format=yuv420p[OUT]" if cur != "[IN]" else
                 f"[IN]{tail + ',' if tail else ''}format=yuv420p[OUT]")
    return parts


# ============================================================================== matching
def match(ref, ours):
    """Film parameters that move `ours` (measured on our LUT-only picture) toward `ref`. Only where the
    reference measurably has more of something; each value is bounded. Returns {film keys..., "why": {}}."""
    film, why = {}, {}
    rg, og = ref.get("grain_std"), ours.get("grain_std")
    if rg is not None and og is not None and rg > og * 1.15 + 0.15:
        add = math.sqrt(max(0.0, rg * rg - og * og))
        film["grain"] = round(min(LIMITS["grain"][1], add), 2)
        rc = ref.get("grain_corr")
        film["grain_size"] = 1.0 if rc is None or rc < 0.25 else (1.5 if rc < 0.45 else 2.0)
        why["grain"] = f"reference grain std {rg:.2f} vs ours {og:.2f}"
    rs, os_ = ref.get("sharpness"), ours.get("sharpness")
    if rs is not None and os_ is not None and os_ > rs + 0.015:
        # empirical: a gaussian of sigma s removes about 0.11*s of the 1-px-blur energy share at 1080
        film["soften"] = round(min(LIMITS["soften"][1], (os_ - rs) / 0.11), 2)
        why["soften"] = f"reference sharpness {rs:.3f} vs ours {os_:.3f} (ours crisper)"
    rv, ov = ref.get("vignette"), ours.get("vignette")
    if rv is not None and ov is not None and rv < ov - 0.04:
        film["vignette"] = round(min(LIMITS["vignette"][1], 1.0 - rv / max(ov, 1e-3)), 3)
        why["vignette"] = f"reference corners/centre {rv:.2f} vs ours {ov:.2f}"
    rh, oh = ref.get("halation"), ours.get("halation")
    if rh is not None and rh > 3.0 and (oh is None or rh > oh + 3.0):
        film["halation"] = round(min(1.0, (rh - (oh or 0.0)) / 20.0), 2)
        why["halation"] = f"reference red ring {rh:.1f} vs ours {oh}"
    rb, ob = ref.get("bloom"), ours.get("bloom")
    if rb is not None and rb > 6.0 and (ob is None or rb > ob + 6.0):
        film["bloom"] = round(min(1.0, (rb - (ob or 0.0)) / 40.0), 2)
        why["bloom"] = f"reference glow {rb:.1f} vs ours {ob}"
    rc_, oc = ref.get("ca"), ours.get("ca")
    if rc_ is not None and oc is not None and rc_ > oc * 1.25 and rc_ > 1.3:
        film["ca"] = round(min(LIMITS["ca"][1], (rc_ - oc) * 1.5), 2)
        why["ca"] = f"reference edge colour mismatch x{rc_:.2f} vs ours x{oc:.2f}"
    # roll-off from the brightest 0.1 % (p99 is content: an indoor shot simply has no bright pixels)
    r999, o999 = ref.get("p999"), ours.get("p999")
    if r999 is not None and o999 is not None and r999 < o999 - 8 and (ref.get("clip_pct") or 0) < 0.5 * (ours.get("clip_pct") or 0):
        ce = round(max(0.8, min(1.0, (r999 + 6) / 255.0)), 3)
        film["rolloff"] = {"knee": round(max(0.6, ce - 0.12), 3), "ceiling": ce}
        why["rolloff"] = f"reference brightest 0.1 % at {r999:.0f} vs ours {o999:.0f} (clipping {ref.get('clip_pct')}% vs {ours.get('clip_pct')}%)"
    r1, o1 = ref.get("p1"), ours.get("p1")
    if r1 is not None and o1 is not None and r1 > o1 + 4:
        film["black"] = round(min(LIMITS["black"][1], (r1 - o1) / 255.0), 3)
        why["black"] = f"reference p1 {r1:.1f} vs ours {o1:.1f}"
    film["why"] = why
    return film
