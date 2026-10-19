"""Music: song index with beat grids, beat-snapped cut lengths, the ducked music bed for a render,
and small procedurally generated sound effects (no third-party assets)."""
import json
import os
import tempfile
import time
from pathlib import Path

import numpy as np

from . import db
from .config import AUDIO_EXTS

SR = 48000
ANALYSIS_SR = 22050
BREAK_GAIN = 10 ** (-30 / 20)      # a music break drops ~30 dB (reference: the bass falls to -45..-52 dB), never to digital silence


# --------------------------------------------------------------------------------------------- index
def _probe_song(cfg, path):
    from .media import probe
    try:
        info = probe(cfg, path)
    except Exception:  # noqa: BLE001
        return 0.0, None
    fmt = info.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    title = tags.get("title")
    if title and tags.get("artist"):
        title = f"{tags['artist']} - {title}"
    return float(fmt.get("duration") or 0), title


def analyze_beats(path, max_s=600):
    """Beat grid for one audio file: {"tempo", "beats", "downbeats", "drops"}."""
    import librosa
    y, sr = librosa.load(str(path), sr=ANALYSIS_SR, mono=True, duration=max_s)
    return beats_from_signal(y, sr)


def beats_from_signal(y, sr, hop=512, env=None):
    """Beat grid for a mono signal already in memory (a song, or the full mix of a reference video).
    `env` = a precomputed onset-strength envelope at this hop. Same result shape as analyze_beats."""
    import librosa
    if env is None:
        env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, bt = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=hop, units="time")
    tempo = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0
    bt = np.asarray(bt, dtype=float)
    # downbeats: every 4th beat, on the phase with the strongest onsets
    down = []
    if len(bt) >= 4:
        fr = np.clip(librosa.time_to_frames(bt, sr=sr, hop_length=hop), 0, len(env) - 1)
        strength = [env[fr[p::4]].sum() for p in range(4)]
        down = bt[int(np.argmax(strength))::4].tolist()
    # drops: biggest jumps in 2 s onset-strength RMS
    drops = []
    fps = sr / hop
    w = max(1, int(2 * fps))
    if len(env) > 2 * w:
        rms = np.sqrt(np.convolve(env ** 2, np.ones(w) / w, mode="same"))
        jump = np.zeros_like(rms)
        jump[w:] = rms[w:] - rms[:-w]
        order = np.argsort(jump)[::-1]
        for i in order:
            t = float(i / fps)
            if jump[i] <= 0 or len(drops) >= 3:
                break
            if all(abs(t - d) > 8 for d in drops):
                drops.append(t)
        # snap drops to the nearest beat
        if len(bt):
            drops = [float(bt[np.argmin(np.abs(bt - d))]) for d in drops]
    return {"tempo": round(tempo, 2), "beats": [round(x, 4) for x in bt.tolist()],
            "downbeats": [round(x, 4) for x in down], "drops": sorted(round(x, 3) for x in drops)}


def _song_files(cfg):
    roots = list(cfg.music_roots or []) + [str(cfg.p("music"))]
    seen = set()
    for r in roots:
        rp = Path(r).expanduser()
        if not rp.exists():
            continue
        for dp, _, fs in os.walk(rp):
            for f in fs:
                if f.startswith(".") or Path(f).suffix.lower() not in AUDIO_EXTS:
                    continue
                p = str(Path(dp, f).resolve())
                if p not in seen:
                    seen.add(p)
                    yield p


def index_songs(cfg, con, force=False):
    """Scan music roots + <work>/music, upsert `songs`, write <work>/music/<id>.beats.json."""
    n = 0
    for path in _song_files(cfg):
        sid = db.clip_id(path)
        bp = cfg.p("music", f"{sid}.beats.json")
        row = con.execute("SELECT * FROM songs WHERE id=?", (sid,)).fetchone()
        if row and bp.exists() and not force:
            continue
        dur, title = _probe_song(cfg, path)
        title = title or Path(path).stem
        try:
            info = analyze_beats(path)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {Path(path).name}: {type(e).__name__}: {e}")
            continue
        info["duration"] = dur
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(info))
        db.upsert(con, "songs", {"id": sid, "path": path, "title": title, "duration": dur,
                                 "tempo": info["tempo"], "beats_path": str(bp), "updated": time.time()})
        con.commit()
        n += 1
        print(f"  {sid}  {info['tempo']:6.1f} bpm  {len(info['beats'])} beats  {title}")
    print(f"songs indexed: {n} new/updated")
    return n


def _song_row(con, ref):
    ref = str(ref)
    r = con.execute("SELECT * FROM songs WHERE id=? OR path=?", (ref, ref)).fetchone()
    if r:
        return r
    if os.path.exists(ref):
        r = con.execute("SELECT * FROM songs WHERE path=?", (str(Path(ref).resolve()),)).fetchone()
        if r:
            return r
    rows = con.execute("SELECT * FROM songs WHERE title LIKE ? OR path LIKE ?", (f"%{ref}%", f"%{ref}%")).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise KeyError(f"no song matches {ref!r} (run `cutroom songs index`)")
    raise KeyError(f"{ref!r} is ambiguous: {[x['title'] for x in rows[:8]]}")


def song_info(cfg, con, ref) -> dict:
    r = _song_row(con, ref)
    d = json.loads(Path(r["beats_path"]).read_text())
    return {"id": r["id"], "path": r["path"], "title": r["title"], "duration": r["duration"],
            "tempo": d["tempo"], "beats": d["beats"], "downbeats": d["downbeats"], "drops": d["drops"]}


def beats(cfg, con, ref) -> np.ndarray:
    return np.asarray(song_info(cfg, con, ref)["beats"], dtype=float)


# --------------------------------------------------------------------------------------------- grid
def snap_cuts(beats, start, durations, min_shot=0.4, max_shot=None):
    """Snap planned shot lengths so each cut lands on a beat. `start` is song time at the first
    shot. For each planned length d0 at song time st, take the beat in (st+0.3, st+d0+1.2)
    closest to st+d0; never shorter than min_shot, never longer than max_shot (when given)."""
    b = np.asarray(beats, dtype=float)
    st, out = float(start), []
    for d0 in durations:
        d0 = float(d0)
        if max_shot:
            d0 = min(d0, float(max_shot))
        target = st + d0
        hi = st + d0 + 1.2
        if max_shot:
            hi = min(hi, st + float(max_shot) + 1e-6)
        cand = b[(b > st + max(0.3, min_shot - 1e-6)) & (b < hi)] if len(b) else b
        d = float(cand[np.argmin(np.abs(cand - target))] - st) if len(cand) else d0
        d = max(d, float(min_shot))
        out.append(round(d, 4))
        st += d
    return out


def montage_grid(beats, start, n, beats_per_cut=1):
    """n cut durations on the beat grid starting at song time `start` (first cut ends on the
    beats_per_cut-th beat after start). Extrapolates with the median interval past the last beat."""
    b = np.asarray(beats, dtype=float)
    step = float(np.median(np.diff(b))) if len(b) > 1 else 0.5
    i0 = int(np.searchsorted(b, start - 0.05)) if len(b) else 0
    ends = []
    for k in range(1, n + 1):
        j = i0 + k * beats_per_cut
        if j < len(b):
            ends.append(float(b[j]))
        else:
            base = float(b[-1]) if len(b) else float(start)
            ends.append(base + (j - (len(b) - 1)) * step)
    out, prev = [], float(start)
    for e in ends:
        out.append(round(e - prev, 4))
        prev = e
    return out


# --------------------------------------------------------------------------------------------- bed
def _decode(cfg, path, offset, dur):
    """Decode [offset, offset+dur] of a song to float32 stereo at 48 kHz, zero-padded to dur."""
    import subprocess
    n = int(round(dur * SR))
    cmd = [getattr(cfg, "ffmpeg", "ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{max(0.0, float(offset)):.3f}", "-i", str(path), "-t", f"{dur:.3f}", "-vn",
           "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg decode failed for {path}: {r.stderr.decode(errors='replace')[-400:]}")
    a = np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2)
    if len(a) < n:
        a = np.vstack([a, np.zeros((n - len(a), 2), dtype=np.float32)])
    return a[:n].copy()


def _merge_spans(spans, join=0.5):
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in spans if b > a):
        if out and a - out[-1][1] < join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def duck_envelope(n, spans, floor_gain, ramp=0.25, sr=SR):
    """Gain curve of length n: 1.0 outside speech, floor_gain inside, linear ramps of `ramp` s
    that finish at the span edges (music is already down when the first word starts)."""
    t = np.arange(n, dtype=np.float64) / sr
    w = np.zeros(n)
    for a, b in _merge_spans(spans):
        up = np.clip((t - (a - ramp)) / ramp, 0, 1)
        down = np.clip(((b + ramp) - t) / ramp, 0, 1)
        w = np.maximum(w, np.minimum(up, down))
    return (1.0 - (1.0 - floor_gain) * w).astype(np.float32)


def _span_env(n, spans, ramp, sr=SR, centred=True):
    """0..1 membership curve of length n for timeline-relative spans, linear ramps of `ramp` s.
    centred: the ramp is centred on each span edge (lift around cuts); else it finishes at the edge."""
    t = np.arange(n, dtype=np.float64) / sr
    w = np.zeros(n)
    for a, b in _merge_spans(spans, join=2 * ramp + 0.05):
        if centred:
            up = np.clip((t - (a - ramp / 2)) / max(ramp, 1e-6), 0, 1)
            down = np.clip(((b + ramp / 2) - t) / max(ramp, 1e-6), 0, 1)
        else:
            up = np.clip((t - (a - ramp)) / max(ramp, 1e-6), 0, 1)
            down = np.clip(((b + ramp) - t) / max(ramp, 1e-6), 0, 1)
        w = np.maximum(w, np.minimum(up, down))
    return w.astype(np.float32)


def _edge(n, kind, sr=SR, head=True):
    """Gain ramp at a section edge: "cut" = 8 ms quarter-sine on the exact sample, or {"fade": s}."""
    if isinstance(kind, dict):
        d = float(kind.get("fade", 0.0) or 0.0)
        m = min(n // 2, int(round(d * sr)))
        r = np.linspace(0, 1, m, dtype=np.float32) if m else np.zeros(0, np.float32)
    else:
        m = min(n // 2, int(round(0.008 * sr)))
        r = np.sin(np.linspace(0, np.pi / 2, m, dtype=np.float32)) if m else np.zeros(0, np.float32)
    return r if head else r[::-1]


def k_weighted_lufs(a, sr=SR):
    """Integrated loudness (BS.1770 K-weighting, absolute + relative gate) of a float stereo array."""
    from scipy.signal import lfilter
    x = np.asarray(a, np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if len(x) < sr // 2:
        return None
    # K-weighting biquads for 48 kHz (ITU-R BS.1770-4 table 1 and 2)
    b1, a1 = [1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585]
    b2, a2 = [1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621]
    y = lfilter(b2, a2, lfilter(b1, a1, x, axis=0), axis=0)
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    k = 1 + (len(y) - blk) // hop
    if k < 1:
        return None
    ms = np.array([np.mean(y[i * hop:i * hop + blk] ** 2, axis=0).sum() for i in range(k)])
    lk = -0.691 + 10 * np.log10(ms + 1e-12)
    g = ms[lk > -70]
    if not len(g):
        return None
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g = ms[(lk > -70) & (lk > rel)]
    return float(-0.691 + 10 * np.log10(g.mean())) if len(g) else None


def music_bed(cfg, con, sections_timeline, out_wav):
    """Render the music track for a whole timeline to a 48 kHz stereo float WAV.

    sections_timeline: [{"song": path|id|None, "offset": song s at section start, "start": timeline s,
                         "dur": s, "gain_db": -8, "speech": [(t0, t1) timeline]  -- REAL dialogue only,
                         "mode": "duck"|"mute"|"full"|"under", "duck_db": -18, "start_edge": "cut"|{"fade"},
                         "end_edge": "cut"|{"fade"}, "lift_db": 7, "lift_ramp": 0.06,
                         "breaks": [(t_end, dur)], "under_db": -5, "voice_lufs": -16,
                         "natural": [(t0, t1)], "natural_duck_db": 0}]
    Structure (measured on a reference vlog): songs start and stop hard on the section's cut frames
    (8 ms ramps), sit `lift_db` higher wherever nobody talks (montage/b-roll ~7 LU above talk), drop ~30 dB
    for 0.2-0.4 s before a flash so the hit lands into silence, and duck only under real dialogue.
    "under" = a fixed bed `under_db` below the voice and never ducked (cold-open teaser lines).
    Legacy "fade_in"/"fade_out" seconds are honoured when no start/end edge is given.
    """
    import soundfile as sf
    secs = [s for s in sections_timeline if s.get("dur", 0) > 0]
    total = max((float(s["start"]) + float(s["dur"]) for s in secs), default=0.0)
    n_total = int(round(total * SR))
    bed = np.zeros((n_total, 2), dtype=np.float32)
    report = []
    for s in secs:
        song = s.get("song")
        if not song:
            continue
        path = song
        if not os.path.exists(str(song)) and con is not None:
            path = _song_row(con, song)["path"]
        start, dur = float(s["start"]), float(s["dur"])
        a = _decode(cfg, path, float(s.get("offset", 0.0)), dur)
        n = len(a)
        g_db = float(s.get("gain_db", 0.0))
        mode = s.get("mode", "duck")
        rel = [(t0 - start, t1 - start) for t0, t1 in s.get("speech", []) if t1 > start and t0 < start + dur]
        env = np.ones(n, dtype=np.float32)
        if mode == "under":
            lv = k_weighted_lufs(a)
            if lv is not None:
                g_db = float(s.get("voice_lufs", -16.0)) + float(s.get("under_db", -5.0)) - lv
        else:
            if mode in ("duck", "mute") and rel:
                floor = 0.0 if mode == "mute" else 10 ** (float(s.get("duck_db", -18)) / 20)
                env *= duck_envelope(n, rel, floor)
            lift = float(s.get("lift_db", 0.0) or 0.0)
            if lift > 0:
                # the lift is RELATIVE TO THE DIALOGUE: lifted music lands ~lift LU over the voice target,
                # never louder, whatever the song's mastering level or the section's gain
                lv = k_weighted_lufs(a)
                if lv is not None:
                    cap = float(s.get("voice_lufs", -16.0)) + lift - lv
                    lift = max(0.0, min(g_db + lift, cap) - g_db)
            if lift:
                talk = _span_env(n, rel, float(s.get("lift_ramp", 0.06)), centred=True) if rel else np.zeros(n, np.float32)
                env *= (10 ** (lift / 20)) ** (1.0 - talk)
            nd = float(s.get("natural_duck_db", 0.0) or 0.0)
            nat = [(t0 - start, t1 - start) for t0, t1 in s.get("natural", []) if t1 > start and t0 < start + dur]
            if nd < 0 and nat:
                env *= (10 ** (max(-6.0, nd) / 20)) ** _span_env(n, nat, 0.25, centred=True)
        for t_end, bd in s.get("breaks", []) or []:
            e = float(t_end) - start
            if not 0 < e <= dur + 1e-6:
                continue
            w = _span_env(n, [(max(0.0, e - float(bd)), e)], 0.01, centred=False) if bd > 0 else None
            if w is not None:
                # the gap ends exactly on the event sample: the ramp back up starts at the event
                t = np.arange(n) / SR
                w = np.where(t >= e, np.clip(1 - (t - e) / 0.01, 0, 1), w).astype(np.float32)
                env *= 1.0 - (1.0 - BREAK_GAIN) * w
        se = s.get("start_edge")
        ee = s.get("end_edge")
        if se is None:
            se = {"fade": float(s.get("fade_in", 0.3))}
        if ee is None:
            ee = {"fade": float(s.get("fade_out", 0.6))}
        hr = _edge(n, se, head=True)
        tr = _edge(n, ee, head=False)
        if len(hr):
            env[:len(hr)] *= hr
        if len(tr):
            env[n - len(tr):] *= tr
        g = 10 ** (g_db / 20)
        i0 = int(round(start * SR))
        m = min(n, n_total - i0)
        bed[i0:i0 + m] += a[:m] * (env[:m, None] * g)
        report.append({"start": round(start, 4), "dur": round(dur, 4), "gain_db": round(g_db, 2), "mode": mode})
    peak = float(np.abs(bed).max()) if len(bed) else 0.0
    if peak > 4.0:                                           # absurd overlap; the programme limiter does the rest
        bed *= 4.0 / peak
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), bed, SR, subtype="FLOAT")
    return str(out_wav)


# --------------------------------------------------------------------------------------------- sfx
SFX = {
    # name: (duration, lavfi source(s), filter)
    "whoosh": ("anoisesrc=d=0.4:c=pink:r=48000:a=0.6",
               "highpass=f=300,lowpass=f=5000,afade=t=in:d=0.28:curve=exp,afade=t=out:st=0.28:d=0.12"),
    "hit": ("aevalsrc='0.9*sin(2*PI*(50*t+(90/25)*(1-exp(-25*t))))*exp(-9*t)':d=0.5:s=48000",
            "lowpass=f=400,afade=t=out:st=0.4:d=0.1"),
    "tick": ("aevalsrc='0.8*sin(2*PI*2200*t)*exp(-250*t)':d=0.02:s=48000", "anull"),
    "riser": ("anoisesrc=d=1.5:c=white:r=48000:a=0.4",
              "highpass=f=600,lowpass=f=9000,afade=t=in:d=1.45:curve=exp,afade=t=out:st=1.45:d=0.05"),
}


_PEAKS = {}


def sfx_peak(path) -> float:
    """Seconds from the start of a sound file to its transient peak (argmax of the 5 ms RMS envelope).
    The renderer aligns this instant, not the file start, to the event frame."""
    import soundfile as sf
    st = os.stat(path)
    key = (str(path), st.st_size, int(st.st_mtime))
    if key in _PEAKS:
        return _PEAKS[key]
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    m = np.abs(x).max(axis=1)
    w = max(1, int(sr * 0.005))
    k = len(m) // w
    if k == 0:
        return 0.0
    env = np.sqrt((m[:k * w].reshape(k, w) ** 2).mean(axis=1))
    i = int(np.argmax(env))
    seg = m[i * w:(i + 1) * w]
    pk = (i * w + int(np.argmax(seg))) / sr          # sample-accurate inside the loudest 5 ms block
    _PEAKS[key] = float(pk)
    return float(pk)


def sfx(name, cfg=None) -> str:
    """Path to a generated sound effect (whoosh | hit | tick | riser), 48 kHz stereo WAV, cached
    under <work>/cache/sfx/ (or a temp dir when no project is given)."""
    import subprocess
    if name not in SFX:
        raise KeyError(f"unknown sfx {name!r}; choose from {sorted(SFX)}")
    if cfg is None:
        try:
            from .config import Config
            cfg = Config.load()
        except (SystemExit, Exception):  # noqa: BLE001
            cfg = None
    d = cfg.p("cache", "sfx") if cfg is not None else Path(tempfile.gettempdir(), "cutroom-sfx")
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{name}.wav"
    if out.exists() and out.stat().st_size > 44:
        return str(out)
    src, filt = SFX[name]
    ff = getattr(cfg, "ffmpeg", "ffmpeg") if cfg is not None else "ffmpeg"
    tmp = d / f".{name}.tmp.wav"
    subprocess.run([ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
                    "-af", f"{filt},aformat=channel_layouts=stereo", "-ar", str(SR), "-c:a", "pcm_s16le", str(tmp)],
                   check=True, capture_output=True)
    os.replace(tmp, out)
    return str(out)
