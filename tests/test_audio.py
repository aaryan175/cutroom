"""Audio tests: beats, snapping, tightening, captions, director cues, music ducking, holes, voice
clean fallback, and (when macOS `say` exists) a tiny-model ASR round trip."""
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from cutroom import asr, db, music, voice
from cutroom.config import init_project

FF = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FF, reason="ffmpeg not on PATH")


def ff(*args):
    subprocess.run([FF, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *map(str, args)], check=True)


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)) + 1e-12)


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    root = tmp_path_factory.mktemp("cr")
    (root / "music").mkdir()
    (root / "footage").mkdir()
    cfg = init_project(root / "work", [root / "footage"], [root / "music"])
    con = db.connect(cfg.db_path)
    return root, cfg, con


@pytest.fixture(scope="module")
def click(work):
    root, _, _ = work
    p = root / "music" / "click120.wav"
    # 120 BPM: a 15 ms 1 kHz burst every 0.5 s, accent on every 4th beat
    ff("-f", "lavfi", "-i",
       "aevalsrc='if(lt(mod(t,0.5),0.015),(0.5+0.4*lt(mod(t,2),0.015))*sin(2*PI*1000*t),0)':d=20:s=22050",
       "-ac", "1", p)
    return p


@pytest.fixture(scope="module")
def tone(work):
    root, _, _ = work
    p = root / "tone.wav"
    ff("-f", "lavfi", "-i", "sine=f=330:d=12:sample_rate=48000", "-af", "volume=0.3", "-ac", "2", p)
    return p


def add_clip(cfg, con, path, has_audio=1):
    path = str(Path(path).resolve())
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, text=True).stdout)
    cid = db.clip_id(path)
    db.upsert(con, "clips", {"id": cid, "path": path, "rel": Path(path).name, "duration": dur,
                             "has_audio": has_audio, "status": "probed", "updated": time.time()})
    con.commit()
    return cid, dur


# ------------------------------------------------------------------------------------------ music
def test_beats_tempo(work, click):
    _, cfg, con = work
    music.index_songs(cfg, con)
    info = music.song_info(cfg, con, "click120")
    assert abs(info["tempo"] - 120) <= 3, info["tempo"]
    b = music.beats(cfg, con, "click120")
    assert len(b) > 30
    assert abs(np.median(np.diff(b)) - 0.5) < 0.03
    assert info["downbeats"] and len(info["downbeats"]) <= len(b) // 4 + 1


def test_snap_cuts_on_beat(work, click):
    _, cfg, con = work
    b = music.beats(cfg, con, "click120")
    start = float(b[2])
    durs = [0.7, 1.3, 0.45, 2.1, 0.9]
    snapped = music.snap_cuts(b, start, durs, min_shot=0.4)
    t = start
    for d0, d in zip(durs, snapped):
        assert d >= 0.4
        t += d
        assert np.min(np.abs(b - t)) < 0.03, "cut not on a beat"
        assert abs(d - d0) <= 0.5 + 0.03, "moved more than one beat"
    grid = music.montage_grid(b, start, 6, beats_per_cut=2)
    assert len(grid) == 6 and all(abs(g - 1.0) < 0.05 for g in grid)


def test_music_bed_ducks(work, tone, tmp_path):
    _, cfg, con = work
    out = tmp_path / "bed.wav"
    music.music_bed(cfg, con, [
        {"song": str(tone), "offset": 1.0, "start": 0.0, "dur": 10.0, "gain_db": 0, "speech": [(4.0, 6.0)],
         "mode": "duck", "duck_db": -18},
        {"song": None, "start": 10.0, "dur": 1.0},
    ], out)
    y, sr = sf.read(str(out))
    assert sr == 48000 and y.shape[1] == 2
    assert abs(len(y) / sr - 11.0) < 0.01
    inside = y[int(4.3 * sr):int(5.7 * sr)]
    outside = y[int(1.0 * sr):int(3.5 * sr)]
    assert rms_db(outside) - rms_db(inside) >= 12
    assert rms_db(y[int(10.2 * sr):]) < -80          # silent section


def test_sfx_generated(work):
    _, cfg, _ = work
    for name, d in (("whoosh", 0.4), ("hit", 0.5), ("tick", 0.02), ("riser", 1.5)):
        p = music.sfx(name, cfg)
        info = sf.info(p)
        assert info.samplerate == 48000 and info.channels == 2
        assert abs(info.duration - d) < 0.02, (name, info.duration)


# ------------------------------------------------------------------------------------------ words
W = [  # (start, end, word): "Hello there. <1 s pause> This is a test of the edit room. Okay cut."
    (0.10, 0.40, "Hello"), (0.45, 0.80, "there."),
    (1.80, 1.95, "This"), (1.98, 2.10, "is"), (2.12, 2.18, "a"), (2.20, 2.50, "test"), (2.52, 2.60, "of"),
    (2.62, 2.70, "the"), (2.72, 2.95, "edit"), (2.98, 3.30, "room."),
    (4.00, 4.30, "Okay"), (4.35, 4.60, "cut."),
]


def test_tighten_splits_at_pause():
    pieces = asr.tighten(W, 0.0, 5.0, gap=0.35, pad=0.08, min_piece=0.8)
    assert len(pieces) >= 2
    assert pieces[0][0] == pytest.approx(0.02) and pieces[0][1] == pytest.approx(0.88)
    assert pieces[1][0] == pytest.approx(1.72)
    for x, y in pieces:
        assert not (x < 1.3 < y), "pause should be removed"
    # tiny trailing fragment gets merged into the previous piece
    merged = asr.tighten(W[:10] + [(3.8, 3.9, "so")], 0.0, 5.0, min_piece=0.8)
    assert merged[-1][1] == pytest.approx(4.08)


def test_cues_respect_limits():
    cs = asr.cues(W, 0.0, 5.0, max_words=3, max_chars=30)
    assert cs
    for t0, t1, txt in cs:
        assert len(txt.split()) <= 3
        assert t1 - t0 >= 0.15
    for (a0, a1, _), (b0, b1, _) in zip(cs, cs[1:]):
        assert a1 <= b0 - 0.019
    assert cs[0][2] == "Hello there."
    # relative timing + fixes + numeric cleanup
    ws = [(10.0, 10.2, "at"), (10.25, 10.5, "7"), (10.5, 10.8, ".50"), (10.85, 11.0, "teh"), (11.0, 11.3, "end .")]
    c2 = asr.cues(ws, 10.0, 12.0, max_words=6, fixes=[[r"\bteh\b", "the"]])
    assert c2[0][0] == 0.0 and c2[0][2] == "at 7:50 the end."


def test_director_cues_and_repeats():
    dc = asr.director_cues(W)
    assert any(p == "okay cut" for _, _, p in dc)
    assert asr.director_cues([(0, 0.3, "we"), (0.3, 0.5, "cut"), (0.5, 0.7, "onions")]) == []
    ws = [(0, .3, "This"), (.3, .5, "is"), (.5, .8, "the"), (.8, 1.2, "best"), (1.2, 1.6, "coffee."),
          (3, 3.3, "Okay"), (3.3, 3.5, "this"), (3.5, 3.7, "is"), (3.7, 4, "the"), (4, 4.3, "best"), (4.3, 4.7, "coffee.")]
    rp = asr.repeats(ws)
    assert rp and rp[0][1][0] == 3
    ov = asr.overused(ws + [(5, 5.3, "coffee"), (6, 6.3, "coffee")], 0, 10, min_count=3)
    assert ov and ov[0][0] == "coffee" and ov[0][1] == 4


# ------------------------------------------------------------------------------------------ voice
def test_silence_holes(work, tmp_path):
    p = tmp_path / "holes.wav"
    ff("-f", "lavfi", "-i", "aevalsrc='if(between(t,2,3),0,0.3*sin(2*PI*440*t))':d=5:s=16000", p)
    holes = voice.silence_holes(str(p))
    ts = [t for t, _ in holes]
    assert ts and min(ts) >= 1.99 and max(ts) <= 3.0
    assert len(ts) >= 3


def test_voice_clean_fallback(work, tone, monkeypatch):
    _, cfg, con = work
    cid, dur = add_clip(cfg, con, tone)
    monkeypatch.setattr(voice, "has_deepfilter", lambda: False)
    out = voice.clean(cfg, cid, atten_lim_db=15, force=True)
    info = sf.info(out)
    assert out.endswith(f"{cid}_15.wav")
    assert info.samplerate == 48000 and info.channels == 2
    assert abs(info.duration - dur) < 0.05
    assert voice.clean(cfg, cid, atten_lim_db=15) == out   # cached
    assert "acompressor" in voice.voice_chain()


# ------------------------------------------------------------------------------------------ asr
SAY = shutil.which("say")


@pytest.fixture(scope="module")
def speech(work):
    if not SAY:
        pytest.skip("macOS `say` not available")
    root, _, _ = work
    aiff = root / "speech.aiff"
    subprocess.run([SAY, "-o", str(aiff),
                    "Hello there. [[slnc 1200]] This is a test of the edit room. [[slnc 1200]] Okay, cut."], check=True)
    wav = root / "footage" / "speech.wav"
    # 1 s of silence up front + a short tail so VAD/ASR have context
    ff("-i", aiff, "-af", "adelay=1000|1000,apad=pad_dur=1", "-ar", "48000", "-ac", "1", wav)
    return wav


def test_asr_roundtrip(work, speech, tmp_path):
    _, cfg, con = work
    cid, dur = add_clip(cfg, con, speech)
    silent = tmp_path / "silent.wav"
    ff("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.001", "-ar", "48000", silent)
    sid, _ = add_clip(cfg, con, silent)
    res = asr.transcribe_all(cfg, con, model="tiny")
    assert cid in [r["clip_id"] for r in res] and sid not in [r["clip_id"] for r in res]
    row = con.execute("SELECT * FROM transcripts WHERE clip_id=?", (sid,)).fetchone()
    assert row["n_words"] == 0 and row["model"] == "tiny"          # recorded, not retried
    row = con.execute("SELECT * FROM transcripts WHERE clip_id=?", (cid,)).fetchone()
    assert row["n_words"] >= 8, row["text"]
    assert "test" in row["text"].lower()
    ws = asr.words(cfg, cid)
    assert all(a <= b for a, b, _ in ws)
    assert any("cut" in p for _, _, p in asr.director_cues(ws)), ws
    pieces = asr.tighten(ws, 0.0, dur)
    assert len(pieces) >= 2                                         # split at a ~1.2 s pause
    assert asr.transcribe_all(cfg, con, model="tiny") == []         # nothing left to do
    asr.print_transcript(cfg, cid)
    tone_path = voice.room_tone(cfg, cid, 3.0)
    assert abs(sf.info(tone_path).duration - 3.0) < 0.01
