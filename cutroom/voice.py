"""Dialogue audio: denoise with a ceiling, the render-time voice chain, hole detection and room tone.

The attenuation CEILING matters: full-strength denoise gates pauses to digital silence and thins
voices in busy rooms. A ceiling (20 dB default, 12-15 dB for loud rooms) keeps natural room tone.
"""
import os
import tempfile
from pathlib import Path

import numpy as np

from . import db
from .asr import load_audio

OUT_SR = 48000

VOICE_CHAIN = ("highpass=f=85,"
               "equalizer=f=250:t=q:w=1.2:g=-2.5,"
               "equalizer=f=3200:t=q:w=1.0:g=2.5,"
               "equalizer=f=9000:t=q:w=1.5:g=1.5,"
               "acompressor=threshold=-22dB:ratio=3:attack=6:release=90:makeup=2")
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
FALLBACK_DENOISE = "highpass=f=80,afftdn=nr=12:nf=-40:tn=1"


def voice_chain() -> str:
    """ffmpeg -af string for dialogue at render time. Follow with LOUDNORM at the segment level."""
    return VOICE_CHAIN


def _clip_path(cfg, cid):
    con = db.connect(cfg.db_path)
    try:
        r = con.execute("SELECT path FROM clips WHERE id=?", (cid,)).fetchone()
        if not r:
            r = db.get_clip(con, cid)
        return r["path"]
    finally:
        con.close()


def has_deepfilter() -> bool:
    try:
        import df.enhance  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


_DF = {}


def _deepfilter(in_wav, out_wav, atten_lim_db):
    import torch  # noqa: F401  (DeepFilterNet depends on torch)
    from df.enhance import enhance, init_df, load_audio as df_load, save_audio
    if "m" not in _DF:
        _DF["m"] = init_df()[:2]
    model, state = _DF["m"]
    audio, _ = df_load(str(in_wav), sr=state.sr())
    enh = enhance(model, state, audio, atten_lim_db=atten_lim_db)
    save_audio(str(out_wav), enh, state.sr())


def clean(cfg, cid, atten_lim_db=20, force=False, src=None) -> str:
    """Denoised dialogue for a clip at <work>/voice/<id>_<atten>.wav (48 kHz stereo 16-bit PCM).
    DeepFilterNet with an attenuation ceiling when installed, else an ffmpeg afftdn chain.
    `src` overrides the source file (defaults to the clip's original)."""
    from .media import ffmpeg
    atten = int(round(atten_lim_db)) if atten_lim_db is not None else 0
    out = cfg.p("voice", f"{cid}_{atten}.wav")
    if out.exists() and out.stat().st_size > 44 and not force:
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    src = src or _clip_path(cfg, cid)
    tmp_out = out.with_name(out.stem + ".tmp.wav")
    done = False
    if has_deepfilter():
        with tempfile.TemporaryDirectory(dir=str(cfg.p("voice"))) as td:
            mono = Path(td, "in.wav")
            enh = Path(td, "enh.wav")
            ffmpeg(cfg, ["-i", src, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", OUT_SR, "-c:a", "pcm_f32le", mono])
            try:
                _deepfilter(mono, enh, atten_lim_db)
                ffmpeg(cfg, ["-i", enh, "-ac", "2", "-ar", OUT_SR, "-c:a", "pcm_s16le", tmp_out])
                done = True
            except Exception as e:  # noqa: BLE001
                print(f"  deepfilter failed ({type(e).__name__}: {e}); using ffmpeg denoise")
    if not done:
        ffmpeg(cfg, ["-i", src, "-vn", "-map", "0:a:0", "-af", FALLBACK_DENOISE,
                     "-ac", "2", "-ar", OUT_SR, "-c:a", "pcm_s16le", tmp_out])
    os.replace(tmp_out, out)
    return str(out)


def _rms_db_windows(audio, sr, win):
    n = max(1, int(sr * win))
    k = len(audio) // n
    if k == 0:
        return np.array([])
    fr = audio[: k * n].reshape(k, n).astype(np.float64)
    return 20 * np.log10(np.sqrt(np.mean(fr ** 2, axis=1)) + 1e-12)


class _Bin:
    ffmpeg = "ffmpeg"


def silence_holes(wav_or_video_path, win=0.25, floor_db=-65, cfg=None):
    """[(t, rms_db)] for each `win`-second window whose RMS is below `floor_db` (dropouts, gated
    pauses, digital silence)."""
    audio = load_audio(cfg or _Bin, wav_or_video_path, sr=16000)
    db_ = _rms_db_windows(audio, 16000, win)
    return [(round(i * win, 3), round(float(v), 1)) for i, v in enumerate(db_) if v < floor_db]


def room_tone(cfg, cid, dur, force=False) -> str:
    """WAV (48 kHz stereo) of `dur` seconds of the clip's room tone: the quietest 1.5 s stretch
    without speech (and not digital silence), looped with crossfades."""
    import soundfile as sf
    from . import asr
    dur = float(dur)
    out = cfg.p("voice", f"{cid}_tone_{dur:.2f}.wav")
    if out.exists() and not force:
        return str(out)
    src = _clip_path(cfg, cid)
    seg = 1.5
    a16 = load_audio(cfg, src, sr=16000)
    hop = 0.1
    db_ = _rms_db_windows(a16, 16000, hop)
    speech = [(w[0] - 0.3, w[1] + 0.3) for w in asr.words(cfg, cid)]
    if not speech:
        speech = asr.speech_spans(a16)
    nwin = int(seg / hop)
    best, best_t = None, 0.0
    for i in range(0, max(1, len(db_) - nwin + 1)):
        t0, t1 = i * hop, i * hop + seg
        w = db_[i:i + nwin]
        if len(w) < nwin or w.min() < -90:          # contains digital silence
            continue
        if any(s < t1 and e > t0 for s, e in speech):
            continue
        score = float(np.mean(w)) + float(np.std(w))  # quiet and steady
        if best is None or score < best:
            best, best_t = score, t0
    piece = load_audio(cfg, src, sr=OUT_SR, start=best_t, dur=seg)
    if len(piece) < OUT_SR * 0.2:
        piece = np.zeros(int(OUT_SR * seg), dtype=np.float32)
    xf = min(int(0.25 * OUT_SR), len(piece) // 3)
    n_out = int(round(dur * OUT_SR))
    buf = np.zeros(n_out + len(piece), dtype=np.float32)
    fade_in = np.sqrt(np.linspace(0, 1, xf, dtype=np.float32))
    fade_out = fade_in[::-1]
    pos = 0
    first = True
    while pos < n_out:
        p = piece.copy()
        if not first:
            p[:xf] *= fade_in
        p[-xf:] *= fade_out
        buf[pos:pos + len(p)] += p
        pos += len(p) - xf
        first = False
    y = buf[:n_out]
    edge = min(int(0.01 * OUT_SR), n_out // 2)      # 10 ms de-click at both ends
    if edge:
        y[:edge] *= np.linspace(0, 1, edge, dtype=np.float32)
        y[-edge:] *= np.linspace(1, 0, edge, dtype=np.float32)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), np.stack([y, y], axis=1), OUT_SR, subtype="PCM_16")
    return str(out)
