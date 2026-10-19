"""Measured loudness dynamics: the short-term loudness envelope of a file (or a window of it) and its swing.

    envelope(cfg, path, t0=0, t1=None)  -> {t: [...], S: [...], M: [...], I, LRA, TP}   (0.1 s steps, ebur128)
    swing(env, lo=10, hi=95)            -> p95 - p10 of momentary (400 ms) loudness (LU), gated at -70 LUFS
    window_stats(cfg, path, t0, t1)     -> {I, TP, swing, M_p10, M_p95, M_max, S_max, LRA}

"Swing" is the punch number the factory measures everywhere (claims check, QA swing_vs_reference, the
reference intro envelope): how far the loud moments (hits, burns) sit above the quiet ones (the lines). It
is p95 - p10 of the 400 ms momentary loudness inside the window (the "short-term" swing of the real-run
report: 7.2 LU on v5 vs 12.3 on the reference's 10.4 s cold open), so one stray peak can't fake it and a
level change (integrated) alone can't move it. The 3 s EBU short-term value is too slow for a 10 s cold
open whose hits last 0.3-0.8 s (it reads ~2.7 LU on both), so it is kept in the envelope but not used.

The window is cut BEFORE measuring (-ss/-t on the input), so the short-term values at the window's start
never include audio from before it.
"""
import re
import subprocess

import numpy as np

GATE = -70.0
_ROW = re.compile(r"t:\s*([\d.]+)\s+TARGET:\S*\s+LUFS\s+M:\s*(-?[\d.]+|-inf|nan)\s+S:\s*(-?[\d.]+|-inf|nan)")


def _f(s):
    try:
        v = float(s)
    except ValueError:
        return -120.0
    return v if np.isfinite(v) else -120.0


def _bin(cfg):
    return getattr(cfg, "ffmpeg", None) or "ffmpeg"


def envelope(cfg, path, t0=0.0, t1=None) -> dict:
    """ebur128 framelog over [t0, t1] of the file's first audio stream."""
    cmd = [_bin(cfg), "-hide_banner", "-nostdin", "-loglevel", "info"]
    if t0 and t0 > 0:
        cmd += ["-ss", f"{t0:.3f}"]
    if t1 is not None:
        cmd += ["-t", f"{max(0.05, t1 - (t0 or 0)):.3f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=info", "-f", "null", "-"]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
    log = r.stderr.decode(errors="ignore")
    ts, ms, ss = [], [], []
    for m in _ROW.finditer(log):
        ts.append(round(float(m.group(1)) + (t0 or 0), 3))
        ms.append(_f(m.group(2)))
        ss.append(_f(m.group(3)))
    summ = log[log.rfind("Summary:"):] if "Summary:" in log else ""

    def grab(rx):
        m = re.search(rx, summ, re.S)
        return _f(m.group(1)) if m else None
    return {"t": ts, "M": ms, "S": ss, "I": grab(r"I:\s*(-?[\d.]+|-inf)\s*LUFS"), "LRA": grab(r"LRA:\s*(-?[\d.]+)\s*LU"),
            "TP": grab(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)"), "t0": t0 or 0.0, "t1": t1}


def swing(env, lo=10, hi=95, key="M", skip_s=0.0) -> float | None:
    """p_hi - p_lo of the gated momentary loudness (LU; key="S" for EBU short-term). skip_s drops the first
    seconds."""
    t = np.asarray(env.get("t") or [], float)
    v = np.asarray(env.get(key) or [], float)
    if len(v) == 0:
        return None
    if skip_s:
        v = v[t >= (env.get("t0") or 0) + skip_s]
    v = v[v > GATE]
    if len(v) < 5:
        return None
    return round(float(np.percentile(v, hi) - np.percentile(v, lo)), 2)


def window_stats(cfg, path, t0=0.0, t1=None) -> dict:
    env = envelope(cfg, path, t0, t1)
    s = np.asarray([x for x in env["S"] if x > GATE], float)
    m = np.asarray([x for x in env["M"] if x > GATE], float)
    return {"I": env["I"], "TP": env["TP"], "LRA": env["LRA"], "swing": swing(env),
            "M_p10": round(float(np.percentile(m, 10)), 2) if len(m) else None,
            "M_p95": round(float(np.percentile(m, 95)), 2) if len(m) else None,
            "S_max": round(float(s.max()), 2) if len(s) else None,
            "M_max": round(float(m.max()), 2) if len(m) else None, "n": len(env["t"])}


def resample(env, step=0.1, key="M") -> list:
    """[(t, value)] every `step` seconds (the gain-shaping curve a renderer follows)."""
    t = np.asarray(env.get("t") or [], float)
    v = np.asarray(env.get(key) or [], float)
    if len(t) == 0:
        return []
    out, k = [], t[0]
    while k <= t[-1] + 1e-6:
        i = int(np.argmin(np.abs(t - k)))
        out.append((round(float(k), 3), round(float(v[i]), 2)))
        k += step
    return out
