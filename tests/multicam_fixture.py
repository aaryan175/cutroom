"""Synthetic multicam shoot for the sync / NLE tests: two cameras hearing the same room (different pictures, mics
and noise), offset by a known amount, plus an audio-only recorder whose clock runs fast (drift)."""
import subprocess
from pathlib import Path

import numpy as np

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
SR = 48000

# truth, in seconds of "real" time
CAM_A_START, CAM_A_DUR = 5.0, 60.0
CAM_B_START, CAM_B_DUR = 5.0 + 3.37, 55.0          # camB starts 3.37 s after camA
REC_START, REC_DUR = 2.0, 70.0                     # the recorder rolls first and stops last
REC_DRIFT = 150e-6                                 # its clock runs 150 ppm fast
TOTAL = 75.0


def room_sound(seed=7, dur=TOTAL, sr=SR) -> np.ndarray:
    """Speech-like bursts of band-limited noise and short tones, irregular rhythm (no periodicity)."""
    rng = np.random.default_rng(seed)
    n = int(dur * sr)
    x = np.zeros(n, np.float32)
    t = 0.2
    while t < dur - 0.6:
        d = rng.uniform(0.06, 0.45)
        i0, i1 = int(t * sr), int((t + d) * sr)
        k = i1 - i0
        env = np.sin(np.linspace(0, np.pi, k)) ** 0.7 * rng.uniform(0.15, 0.8)
        if rng.random() < 0.75:
            w = rng.standard_normal(k + 64)
            lo = rng.integers(2, 10)
            w = np.convolve(w, np.ones(lo) / lo, mode="same")[:k]
            x[i0:i1] += (w * env).astype(np.float32)
        else:
            f = rng.uniform(180, 900)
            x[i0:i1] += (np.sin(2 * np.pi * f * np.arange(k) / sr) * env).astype(np.float32)
        t += d + rng.uniform(0.05, 0.7)
    return x / max(1e-6, float(np.abs(x).max())) * 0.7


def _ff(*a):
    subprocess.run(FF + [str(v) for v in a], check=True)


def _write_wav(path, x, sr=SR):
    import soundfile as sf
    sf.write(str(path), np.clip(x, -1, 1).astype(np.float32), sr, subtype="PCM_16")


def make_shoot(root: Path, fps_a="25", fps_b="25", seed=7) -> dict:
    """camA.mp4, camB.mov (different picture, mic colour, noise) and REC/ZOOM0001.WAV (drifting clock)."""
    root.mkdir(parents=True, exist_ok=True)
    s = room_sound(seed)
    rng = np.random.default_rng(seed + 1)
    tt = np.arange(len(s)) / SR

    def cut(start, dur):
        return s[int(start * SR): int(start * SR) + int(dur * SR)]
    a = cut(CAM_A_START, CAM_A_DUR) + 0.02 * rng.standard_normal(int(CAM_A_DUR * SR)).astype(np.float32)
    b = cut(CAM_B_START, CAM_B_DUR)
    b = np.convolve(b, np.ones(6) / 6, mode="same").astype(np.float32) * 0.6      # duller, quieter mic
    b = b + 0.03 * rng.standard_normal(len(b)).astype(np.float32)
    nrec = int(REC_DUR * (1 + REC_DRIFT) * SR)
    true_t = REC_START + np.arange(nrec) / SR / (1 + REC_DRIFT)                   # fast clock: more samples
    rec = np.interp(true_t, tt, s).astype(np.float32) * 0.9 + 0.01 * rng.standard_normal(nrec).astype(np.float32)
    (root / "A_CAM").mkdir(exist_ok=True)
    (root / "B_CAM").mkdir(exist_ok=True)
    (root / "REC").mkdir(exist_ok=True)
    wa, wb = root / "_a.wav", root / "_b.wav"
    _write_wav(wa, a)
    _write_wav(wb, b)
    out = {"camA": root / "A_CAM" / "C0001.MP4", "camB": root / "B_CAM" / "C0101.MOV", "rec": root / "REC" / "ZOOM0001.WAV"}
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={fps_a}:duration={CAM_A_DUR}", "-i", wa,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-metadata", "creation_time=2026-05-01T10:00:05Z", "-shortest", out["camA"])
    _ff("-f", "lavfi", "-i", f"smptehdbars=size=320x180:rate={fps_b}:duration={CAM_B_DUR}", "-i", wb,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-metadata", "creation_time=2026-05-01T10:00:08Z", "-shortest", out["camB"])
    wr = root / "_r.wav"
    _write_wav(wr, rec)
    _ff("-i", wr, "-c:a", "pcm_s16le", "-write_bext", "1", "-metadata", "originator=ZOOM H6",
        "-metadata", "origination_date=2026-05-01", "-metadata", "origination_time=12:00:02",
        "-metadata", "time_reference=" + str(int((12 * 3600 + 2) * SR)), out["rec"])
    for w in (wa, wb, wr):
        w.unlink()
    return out


def truth() -> dict:
    """Expected group offsets (group time = camA's clock, 0 = the recorder's first sample) and drift."""
    g0 = REC_START - CAM_A_START
    return {"camA": CAM_A_START - CAM_A_START - g0, "camB": CAM_B_START - CAM_A_START - g0, "rec": 0.0,
            "rec_drift_ppm": (1.0 / (1.0 + REC_DRIFT) - 1.0) * 1e6}
