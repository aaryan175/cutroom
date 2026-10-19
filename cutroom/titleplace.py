"""Where a typed title sits on its held shot: a position chosen by measurement, never by guess.

    title_box(text, size, W, H, font)            -> (w, h) of the title in frame fractions
    contrast(rgb_a, rgb_b)                        -> WCAG contrast ratio 1..21 (yellow on white ~1.1: fails)
    score_frames(frames, box_wh, color, ...)      -> {x, y, ok, score, contrast, why, backing, candidates}
    place(cfg, path, t0, t1, text, ...)           -> score_frames on frames sampled from a clip span
    choose_shot(cfg, cands, text, ..., used=...)  -> the candidate held shot whose quiet/dark area suits the title

Plan use: `"pos": {"x": 0.5, "y": 0.2}` (title centre, frame fractions) or `"pos": "auto"` on a typed_title
overlay, or on `cold_open.title`. `cold_open.end_shot: "auto"` (or {"auto": true, "dur"}) picks the held
shot from the cold-open shot pool with choose_shot. The renderer records the chosen position and why in the
sidecar (typed_titles[].pos / pos_why).

Scoring (per candidate box on a grid of title-sized boxes, over 3-5 frames of the span):
  * detail   mean gradient magnitude inside the box (busy texture reads badly under text)
  * spread   luma standard deviation inside the box (a box half sky, half shirt is a bad backdrop)
  * faces    any overlap with a detected face/head or person box rejects the box (never type over people)
  * sun      a box holding blown highlights (luma > 0.94 over > 2 % of it, or a near-white blob) is rejected
  * contrast WCAG contrast between the title colour and the box's mean colour must be >= 3.0 AND >= 2.0
             against the box's 90th-percentile luminance (bright patches inside a dark box); otherwise
             the box is only usable with a dark backing plate (backing: true, contrast re-scored on it)
  score = detail + 0.6 * spread + 0.15 * |dy from the default line| (lower is better); boxes that pass
  contrast always beat boxes that need a backing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

DEFAULT_Y = 0.505
MIN_CONTRAST = 3.0
MIN_CONTRAST_P90 = 2.0
SUN_LUMA = 0.94
SUN_FRAC = 0.02
BACKING_RGB = (12, 12, 12)


def _lin(c):
    c = np.asarray(c, float) / 255.0
    return np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def rel_lum(rgb) -> float:
    r, g, b = _lin(rgb)
    return float(0.2126 * r + 0.7152 * g + 0.0722 * b)


def contrast(a, b) -> float:
    la, lb = rel_lum(a), rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def title_box(text, size=0.092, W=1920, H=1080, font="") -> tuple:
    """(w, h) fractions of the drawn title (+ a small margin)."""
    from . import cards as CD
    try:
        fp = CD.find_font("title", font or "")
        lay = CD.text_layer(str(text or "TITLE"), fp, float(size) * H * 0.80, (255, 255, 255))
        w, h = lay.width / W, lay.height / H
    except Exception:  # noqa: BLE001 - no font: an estimate from the character count
        w, h = 0.55 * float(size) * len(str(text or "TITLE")) * H / W, float(size) * 1.2
    return min(0.96, w * 1.08), min(0.9, h * 1.15)


def _boxes_hit(box, others, thr=0.02):
    from .qa import overlap
    return any(overlap(box, o) > thr or overlap(o, box) > thr for o in others)


def _region(img, box):
    H, W = img.shape[:2]
    x0, y0 = int(max(0, box[0]) * W), int(max(0, box[1]) * H)
    x1, y1 = int(min(1, box[0] + box[2]) * W), int(min(1, box[1] + box[3]) * H)
    return img[y0:max(y0 + 1, y1), x0:max(x0 + 1, x1)]


def detect_avoid(bgr) -> list:
    """Face/head + person boxes (fractions) to keep the title off."""
    from . import qa
    out = []
    try:
        out += [qa.head_box(f) for f in qa.detect_faces(bgr)]
    except Exception:  # noqa: BLE001
        pass
    try:   # HOG finds a torso or upper body; the person goes on down to the frame edge (seated, cropped)
        out += [[x, y, w, max(h, 1.0 - y)] for x, y, w, h in qa.detect_people(bgr)]
    except Exception:  # noqa: BLE001
        pass
    return out


def score_frames(frames, box_wh, color="#FFCD00", avoid=None, grid=(9, 13), default_y=DEFAULT_Y,
                 detect=True, margin=0.04) -> dict:
    """frames: RGB uint8 arrays (same size). avoid: extra [x, y, w, h] boxes (faces/people), or per-frame
    lists. Returns the best centre (x, y) + why; ok False means only a backed title can read anywhere."""
    import cv2
    from .cards import hex_rgb
    col = hex_rgb(color, (255, 205, 0))
    bw, bh = box_wh
    avoid_all = list(avoid or [])
    stats = []
    for f in frames:
        bgr = cv2.cvtColor(f, cv2.COLOR_RGB2BGR)
        if detect:
            avoid_all += detect_avoid(bgr)
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        stats.append((f, g, np.sqrt(gx * gx + gy * gy)))
    nx, ny = grid
    xs = np.linspace(margin + bw / 2, 1 - margin - bw / 2, nx) if bw < 1 - 2 * margin else np.array([0.5])
    ys = np.linspace(margin + bh / 2, 1 - margin - bh / 2, ny)
    cands = []
    for cy in ys:
        for cx in xs:
            box = [cx - bw / 2, cy - bh / 2, bw, bh]
            why = []
            if _boxes_hit(box, avoid_all):
                why.append("face/person")
            det, spr, mean_rgb, p90, sun = [], [], [], [], 0.0
            for f, g, mag in stats:
                rg, rm, rf = _region(g, box), _region(mag, box), _region(f, box)
                det.append(float(rm.mean()))
                spr.append(float(rg.std()))
                mean_rgb.append(rf.reshape(-1, 3).mean(0))
                p90.append(float(np.percentile(rg, 90)))
                sun = max(sun, float((rg > SUN_LUMA).mean()))
            if sun > SUN_FRAC:
                why.append(f"sun/blown highlights ({sun:.0%} of the box)")
            mrgb = np.mean(mean_rgb, 0)
            c_mean = contrast(col, mrgb)
            p90v = float(np.mean(p90)) * 255
            c_p90 = contrast(col, (p90v, p90v, p90v))
            readable = c_mean >= MIN_CONTRAST and c_p90 >= MIN_CONTRAST_P90
            score = float(np.mean(det)) + 0.6 * float(np.mean(spr)) + 0.15 * abs(cy - default_y)
            cands.append({"x": round(float(cx), 3), "y": round(float(cy), 3), "score": round(score, 4),
                          "detail": round(float(np.mean(det)), 4), "spread": round(float(np.mean(spr)), 4),
                          "contrast": c_mean, "contrast_p90": c_p90, "readable": readable,
                          "rejected": why, "mean_rgb": [int(v) for v in mrgb]})
    usable = [c for c in cands if not c["rejected"]]
    good = sorted([c for c in usable if c["readable"]], key=lambda c: c["score"])
    if good:
        b = good[0]
        why = (f"quietest readable area: detail {b['detail']:.3f}, spread {b['spread']:.3f}, contrast "
               f"{b['contrast']:.1f}:1 vs mean {tuple(b['mean_rgb'])}")
        return {"x": b["x"], "y": b["y"], "ok": True, "backing": False, "score": b["score"], "contrast": b["contrast"],
                "why": why, "n_candidates": len(cands), "n_rejected": len(cands) - len(usable), "candidates": cands}
    pool = sorted(usable or cands, key=lambda c: c["score"])
    b = pool[0]
    return {"x": b["x"], "y": b["y"], "ok": False, "backing": True, "score": b["score"],
            "contrast": contrast(col, BACKING_RGB),
            "why": (f"no area gives the title contrast >= {MIN_CONTRAST}:1 (best {b['contrast']:.1f}:1)"
                    + ("" if usable else "; every area touches a face or the sun") + "; drawn on a dark backing"),
            "n_candidates": len(cands), "n_rejected": len(cands) - len(usable), "candidates": cands}


def sample_frames(cfg, path, t0, t1, n=4, width=480) -> list:
    """n RGB frames evenly spread over [t0, t1] of a file."""
    ff = getattr(cfg, "ffmpeg", None) or "ffmpeg"
    out = []
    span = max(0.0, float(t1) - float(t0))
    for k in range(n):
        t = float(t0) + span * (k + 0.5) / n
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v",
               "1", "-vf", f"scale={width}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode or not r.stdout:
            continue
        from . import media
        try:
            s = media.summarize_probe(media.probe(cfg, str(path)))
            w, h = int(s.get("width") or 0), int(s.get("height") or 0)
            rot = abs(int(s.get("rotation") or 0)) % 180 == 90
            if rot:
                w, h = h, w
            hh = int(round(width * h / w / 2) * 2) if w and h else 0
        except Exception:  # noqa: BLE001
            hh = 0
        if not hh or len(r.stdout) != width * hh * 3:
            hh = len(r.stdout) // (width * 3)
        if hh <= 0:
            continue
        out.append(np.frombuffer(r.stdout[:width * hh * 3], np.uint8).reshape(hh, width, 3))
    return out


def place(cfg, path, t0, t1, text, size=0.092, color="#FFCD00", font="", out_wh=(1920, 1080), zoom=1.0,
          n=4, detect=True) -> dict:
    """Best title centre on the clip span (source frame -> output frame by a centre crop/scale; a push-in
    up to `zoom` grows the box so the title stays inside the chosen area through the push)."""
    frames = sample_frames(cfg, path, t0, t1, n=n)
    if not frames:
        return {"x": 0.5, "y": DEFAULT_Y, "ok": False, "backing": False, "why": "no frames could be read", "auto": False}
    bw, bh = title_box(text, size, out_wh[0], out_wh[1], font)
    z = max(1.0, float(zoom))
    r = score_frames(frames, (min(0.96, bw * z), min(0.9, bh * z)), color, detect=detect)
    r["box_wh"] = [round(bw, 4), round(bh, 4)]
    r["auto"] = True
    return r


def resolve_pos(pos, default_y=DEFAULT_Y):
    """{x, y} | "auto" | None -> (x, y) when explicit, else None."""
    if isinstance(pos, dict) and pos.get("x") is not None and pos.get("y") is not None:
        return float(pos["x"]), float(pos["y"])
    return None


def choose_shot(cfg, cands, text, size=0.092, color="#FFCD00", dur=2.7, used=None, path_of=None, n=3) -> dict:
    """cands: [{"clip", "in", "duration"?}] (+ "path" or path_of(clip) -> file); with "duration" a candidate
    whose window is taken slides to the clip's first free window. used: {clip: [(a, b)...]} source windows
    shown within ~10 s of the title (the repeats rule): a candidate overlapping one is skipped. Returns the
    winner {clip, in, dur, x, y, why, ranking: [...]}; readable-without-backing beats backed, then the
    lowest area score."""
    used = used or {}
    rows = []
    for c in cands:
        a = float(c.get("in") or 0.0)
        b = a + float(dur)

        def clashes(a_, b_):
            return [w for w in used.get(c["clip"], []) if a_ < w[1] + 0.2 and b_ > w[0] - 0.2]
        clash = clashes(a, b)
        D = float(c.get("duration") or 0)
        if clash and D:                       # slide to the first free window later in the clip
            t = a
            while clashes(t, t + float(dur)) and t + float(dur) <= D - 0.05:
                t += 0.25
            if not clashes(t, t + float(dur)) and t + float(dur) <= D - 0.05:
                a, b, clash = round(t, 3), round(t + float(dur), 3), []
        if clash:
            rows.append({"clip": c["clip"], "in": a, "skipped": f"source window used nearby {clash[0]}"})
            continue
        p = c.get("path") or (path_of(c["clip"]) if path_of else None)
        if not p or not Path(p).exists():
            rows.append({"clip": c["clip"], "in": a, "skipped": "no file"})
            continue
        r = place(cfg, p, a, b, text, size, color, n=n)
        rows.append({"clip": c["clip"], "in": a, "x": r["x"], "y": r["y"], "ok": r.get("ok"), "score": r.get("score"),
                     "contrast": r.get("contrast"), "why": r.get("why")})
    live = [r for r in rows if not r.get("skipped") and r.get("score") is not None]
    if not live:
        return {"clip": None, "why": "no candidate held shot is free and readable", "ranking": rows}
    live.sort(key=lambda r: (not r["ok"], r["score"]))
    w = live[0]
    return {"clip": w["clip"], "in": w["in"], "dur": float(dur), "x": w["x"], "y": w["y"], "ok": w["ok"],
            "why": f"{w['clip']} @ {w['in']:.2f}: {w['why']}", "ranking": rows}


def debug_image(frame, res, out_path):
    """Save the frame with every rejected box (red), readable boxes (thin green) and the pick (thick)."""
    import cv2
    img = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR).copy()
    H, W = img.shape[:2]
    bw, bh = res.get("box_wh") or [0.3, 0.1]
    for c in res.get("candidates") or []:
        x0, y0 = int((c["x"] - bw / 2) * W), int((c["y"] - bh / 2) * H)
        x1, y1 = int((c["x"] + bw / 2) * W), int((c["y"] + bh / 2) * H)
        if c["rejected"]:
            cv2.circle(img, (int(c["x"] * W), int(c["y"] * H)), 2, (0, 0, 255), -1)
        elif c["readable"]:
            cv2.circle(img, (int(c["x"] * W), int(c["y"] * H)), 2, (0, 255, 0), -1)
    x0, y0 = int((res["x"] - bw / 2) * W), int((res["y"] - bh / 2) * H)
    x1, y1 = int((res["x"] + bw / 2) * W), int((res["y"] + bh / 2) * H)
    cv2.rectangle(img, (x0, y0), (x1, y1), (0, 255, 255) if res.get("ok") else (0, 128, 255), 2)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), img)
    return str(out_path)
