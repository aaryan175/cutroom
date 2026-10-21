"""Brand-film toolkit (a stylised cinematic reference rebuilt on vlog footage): generated looks (bw_contrast,
teal_night) + the bw neutrality fix + white balance before a tint + per-shot / per-cutaway looks + reference look
mode, the 5:3 pillarbox, sound automation (low-pass, gain ramps, a near-silence hole, frequency-dependent duck,
under ramp), the sub riser / sub drop with end alignment and offsets, an audio-only voice memo kept raw, the
whole-programme loudness-envelope match, image end cards, exposure ramps, slow motion past 4x (real frames or
optical flow), 0.2 s burst shots, and the voice-over story hints. Synthetic footage only."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]
W, H, FPS = 640, 360, 25


def _ff(*a):
    subprocess.run(FF + [str(x) for x in a], check=True)


def _frames(path, w=W, h=H, pix="rgb24"):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", pix, "-"],
                       capture_output=True, check=True)
    ch = 3 if pix == "rgb24" else 1
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, ch)


def _audio(path, sr=48000):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32)


def _db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)) + 1e-12)


@pytest.fixture(scope="module")
def bf(tmp_path_factory):
    from PIL import Image, ImageDraw
    from cutroom import db, ingest, sync
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("brandfilm")
    lib = base / "footage"
    lib.mkdir()
    # a: warm daylight-ish picture (golden cast) with pink-noise ambience
    _ff("-f", "lavfi", "-i", "testsrc2=size=960x540:rate=25:duration=8", "-f", "lavfi",
        "-i", "anoisesrc=c=pink:d=8:a=0.3:r=48000",
        "-vf", "colorbalance=rs=0.15:gs=0.05:bs=-0.15:rm=0.12:bm=-0.12", *X264, "-c:a", "aac", "-shortest", lib / "a.mp4")
    # b: 100 fps source (real slow motion up to 4x in a 25p edit)
    _ff("-f", "lavfi", "-i", "testsrc2=size=960x540:rate=100:duration=3", *X264, lib / "b100.mp4")
    # c: plain 25p motion for optical-flow slow motion
    _ff("-f", "lavfi", "-i", "testsrc2=size=960x540:rate=25:duration=4", *X264, lib / "c.mp4")
    # memo: an audio-only voice memo (tones + hiss, mono, phone-ish)
    _ff("-f", "lavfi", "-i", "sine=f=520:d=20:r=48000", "-f", "lavfi", "-i", "anoisesrc=c=white:a=0.01:d=20:r=48000",
        "-filter_complex", "[0:a]volume='if(lt(mod(t,1.5),1.1),0.5,0.0)':eval=frame[s];[s][1:a]amix=inputs=2:normalize=0[o]",
        "-map", "[o]", "-ac", "1", "-c:a", "aac", "-b:a", "160k", lib / "memo.m4a")
    cfg = init_project(base / "work", [lib])
    cfg.workers = 2
    cfg.out_width, cfg.out_height, cfg.preview_height = W, H, H
    cfg.out_fps = FPS
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    new = sync.register_audio(cfg, con, echo=None)
    ids = {k: db.clip_id(lib / f) for k, f in (("a", "a.mp4"), ("b", "b100.mp4"), ("c", "c.mp4"), ("memo", "memo.m4a"))}
    # the memo's words (a transcript the way asr writes it): two sentences
    words = [(0.1, 0.4, "One"), (0.45, 0.8, "thing."), (1.5, 1.8, "You"), (1.85, 2.3, "love"), (2.35, 2.6, "it,"),
             (3.0, 3.3, "I'm"), (3.35, 3.6, "so"), (3.65, 4.1, "proud.")]
    tp = cfg.p("transcripts", f"{ids['memo']}.json")
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps({"segments": [{"start": 0.1, "end": 4.1, "text": " ".join(w for _, _, w in words),
                                            "words": [{"start": s, "end": e, "word": w} for s, e, w in words]}]}))
    logo = base / "work" / "logo.png"
    im = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((40, 20, 360, 180), fill=(240, 240, 240, 255))
    im.save(logo)
    return {"cfg": cfg, "con": con, "ids": ids, "base": base, "new_audio": new, "logo": logo}


def _render(bf, name, plan):
    from cutroom import render as R
    pf = bf["cfg"].p("plans", f"{name}_v1.json")
    pf.parent.mkdir(parents=True, exist_ok=True)
    plan = {"title": name, "version": 1, "output": {"width": W, "height": H, "fps": FPS},
            **{k: v for k, v in plan.items() if k not in ("title", "version")}}
    plan.setdefault("style", {}).setdefault("captions", {"enabled": False})
    pf.write_text(json.dumps(plan))
    out = R.render_plan(bf["cfg"], bf["con"], pf, preview=True)
    return out, json.loads(Path(out[:-4] + ".json").read_text())


# ------------------------------------------------------------------------------------------------ looks
def test_bw_writes_neutral_chroma(bf, tmp_path):
    """bw = luma to R = G = B: U and V stay 128 (eq=saturation=0 wrote 127 = a green cast)."""
    from cutroom import looks
    ch = looks.chain(bf["cfg"], "bw")
    assert not any("saturation=0" in c for c in ch) and any("colorchannelmixer" in c for c in ch)
    out = tmp_path / "bw.yuv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:d=0.04", "-vf", ",".join(["format=yuv420p", *ch, "format=yuv420p"]),
        "-frames:v", "1", "-f", "rawvideo", out)
    a = np.frombuffer(out.read_bytes(), np.uint8)
    y = 320 * 180
    u, v = a[y:y + y // 4].astype(float), a[y + y // 4:].astype(float)
    assert abs(u.mean() - 128) < 0.6 and abs(v.mean() - 128) < 0.6, (u.mean(), v.mean())
    assert np.abs(u - 128).max() <= 1 and np.abs(v - 128).max() <= 1


def test_generated_looks_hit_the_recipe(bf):
    """The generated cubes reproduce the measured targets on a neutral ramp."""
    from cutroom import looks
    from cutroom.colorsci import Cube
    g = np.linspace(0, 1, 11)
    grey = np.stack([g, g, g], 1)
    bw = Cube.read(looks.gen_cube_path(bf["cfg"], "bw_contrast")).apply(grey) * 255
    assert np.allclose(bw[:, 0], bw[:, 1], atol=0.5) and np.allclose(bw[:, 1], bw[:, 2], atol=0.5)
    assert bw[0, 0] < 1 and 214 <= bw[-1, 0] <= 219            # crushed black, 0.85 white ceiling (217)
    assert bw[5, 0] < 0.36 * 255                                 # low-key mids (0.5 -> 0.30)
    teal = Cube.read(looks.gen_cube_path(bf["cfg"], "teal_night")).apply(grey) * 255
    r, gg, b = teal[5]
    assert 0.55 < r / gg < 0.72 and 0.98 < b / gg < 1.10, teal[5]   # cyan tint: R/G ~0.6, B/G ~1.04
    assert 3 <= teal[0].max() <= 8                               # the 4/255 cyan floor
    # intensity blends toward identity
    ch = looks.chain(bf["cfg"], {"name": "teal_night", "intensity": 0.5})
    assert any("lut3d" in c for c in ch)


def test_white_balance_before_teal(bf):
    from cutroom import looks
    g = looks.wb_gains([200, 150, 100])                        # a warm mid-tone mean
    assert g[0] < 1 < g[2] and g[1] == 1.0
    ch = looks.chain(bf["cfg"], "teal_night", wb=g)
    i_wb = next(i for i, c in enumerate(ch) if c.startswith("colorchannelmixer"))
    i_lut = next(i for i, c in enumerate(ch) if c.startswith("lut3d"))
    assert i_wb < i_lut
    assert not any(c.startswith("colorchannelmixer") for c in looks.chain(bf["cfg"], "bw_contrast", wb=g))
    assert not any(c.startswith("colorchannelmixer") for c in looks.chain(bf["cfg"], {"name": "teal_night", "neutralize": False}, wb=g))


def test_reference_look_mode_bypasses_guards():
    from cutroom import looks
    base = {"luma": 120, "contrast": 50, "sat": 80, "warmth": 20, "p1": 20}
    night = {"luma": 60, "contrast": 45, "sat": 60, "warmth": -10, "p1": 2}
    target = {"luma": 55, "contrast": 45, "sat": 60, "warmth": -10, "p1": 3}
    g = looks.match(base, {"night_cinematic": night}, target)
    assert g["name"] == "natural" and g["mode"] == "guarded"            # daylight: no night look, blacks guarded
    r = looks.match(base, {"night_cinematic": night}, target, reference=True)
    assert r["name"] == "night_cinematic" and r["intensity"] > 0.8 and r["mode"] == "reference"
    assert looks.reference_mode({"p1": 3}) and not looks.reference_mode({"p1": 30})
    an = {"shots": [{"i": 0, "start": 0, "end": 1, "dur": 1, "luma": 0, "sat": 0, "warmth": 0},
                    {"i": 1, "start": 1, "end": 5, "dur": 4, "luma": 30, "sat": 0.4, "warmth": 0},
                    {"i": 2, "start": 5, "end": 7, "dur": 2, "luma": 35, "sat": 90, "warmth": -18},
                    {"i": 3, "start": 7, "end": 8, "dur": 1, "luma": 20, "sat": 120, "warmth": 4}],
          "colour": {"overall": {"luma": 30}}}
    res = looks.reference_looks(an)
    assert [s["look"] for s in res["shots"]] == [None, "bw_contrast", "teal_night", "teal_night"]
    assert res["shares"]["mono"] == 0.5 and res["teal_film"] and res["reference_mode"]


def test_per_shot_and_cutaway_looks_and_pillarbox(bf):
    """Looks switch per montage shot and per cutaway; teal shots are white-balanced first; a 5:3 pillarbox."""
    ids = bf["ids"]
    plan = {"style": {"grade": {"look": "bw_contrast"}},
            "sections": [{"name": "s", "frame": {"kind": "pillarbox", "aspect": 5 / 3}, "segments": [
                {"type": "montage", "beat_sync": False, "shots": [
                    {"clip": ids["a"], "in": 1.0, "dur": 1.0},
                    {"clip": ids["a"], "in": 3.0, "dur": 1.0, "look": "teal_night"}]},
                {"type": "broll", "clip": ids["a"], "in": 4.0, "out": 6.0, "look": "teal_night",
                 "cutaways": [{"clip": ids["a"], "in": 1.0, "at": 0.8, "dur": 0.6, "look": "bw_contrast"}]}]}]}
    out, side = _render(bf, "pershot", plan)
    fr = _frames(out)
    sat = lambda f: float((fr[f].max(2).astype(int) - fr[f].min(2)).mean())   # noqa: E731
    assert sat(12) < 3 and sat(37) > 8                         # shot 1 mono, shot 2 tinted
    assert sat(50 + 30) < 3 and sat(50 + 5) > 8 and sat(50 + 45) > 8         # the cutaway is mono inside a teal shot
    # teal: neutralised, then one cyan hue (B and G over R) even though the clip is warm
    mid = fr[37][H // 4:3 * H // 4, W // 4:3 * W // 4].reshape(-1, 3).astype(float).mean(0)
    assert mid[2] > mid[0] and mid[1] > mid[0], mid
    seg = next(s for s in side["segments"] if s["i"] == 1)
    assert seg["grade"]["look"]["name"] == "teal_night" and seg["grade"]["look"].get("wb")
    # pillarbox: 5:3 picture inside 16:9 -> 20 px black bars at 640 wide, picture to the edge of the window
    bar = round(W * (1 - (5 / 3) / (16 / 9)) / 2)
    f = fr[37].astype(int)
    assert f[:, :bar - 2].max() < 20 and f[:, W - bar + 2:].max() < 20
    assert f[:, bar + 3:bar + 40].mean() > 20 and "pillarbox" in side["devices"]


# ------------------------------------------------------------------------------------------------ sound
def test_lowpass_automation_and_band_split():
    from cutroom import music as M
    sr = 48000
    rng = np.random.default_rng(1)
    x = rng.standard_normal((sr * 2, 2)).astype(np.float32) * 0.1
    hz = M.curve(len(x), [[0, 1000], [1.0, 1000], [1.2, 18000]])
    y = M.lowpass_auto(x, hz)

    def hf(a):
        sp = np.abs(np.fft.rfft(a[:, 0]))
        f = np.fft.rfftfreq(len(a), 1 / sr)
        return float(sp[f > 4000].mean() / sp[(f > 200) & (f < 600)].mean())
    assert hf(y[:sr // 2]) < 0.05 and hf(y[-sr // 2:]) > 0.6     # muffled, then open (dry)
    assert np.allclose(y[-sr // 4:], x[-sr // 4:], atol=1e-5)
    s, m, h = M.band_split(x)
    assert np.allclose(s + m + h, x, atol=1e-5)                    # an undecked bed is bit-for-bit the same
    assert M.auto_keys({"from": -14, "to": 0}, 10) == [[0.0, -14.0], [10.0, 0.0]]
    assert M.auto_keys([[2, 1], [0, 3]]) == [[0.0, 3.0], [2.0, 1.0]]


def test_frequency_dependent_duck_and_under_ramp(bf, tmp_path):
    from cutroom import music as M
    song = tmp_path / "pad.wav"
    _ff("-f", "lavfi", "-i", "sine=f=60:d=6:r=48000", "-f", "lavfi", "-i", "sine=f=1000:d=6:r=48000",
        "-filter_complex", "[0][1]amix=inputs=2:normalize=0,volume=0.3", "-ac", "2", song)
    sec = {"song": str(song), "offset": 0, "start": 0, "dur": 6, "gain_db": 0, "mode": "duck",
           "speech": [(2.0, 4.0)], "duck_bands": {"sub": -25, "mid": -14}, "start_edge": "cut", "end_edge": "cut"}
    out = tmp_path / "bed.wav"
    M.music_bed(bf["cfg"], None, [sec], out)
    import soundfile as sf
    a = sf.read(str(out))[0][:, 0]
    sr = 48000

    def band(x, lo, hi):
        sp = np.abs(np.fft.rfft(x))
        f = np.fft.rfftfreq(len(x), 1 / sr)
        return 20 * np.log10(sp[(f > lo) & (f < hi)].max())
    free, duck = a[int(0.5 * sr):int(1.5 * sr)], a[int(2.6 * sr):int(3.6 * sr)]
    assert abs((band(free, 40, 80) - band(duck, 40, 80)) - 25) < 2.5
    assert abs((band(free, 800, 1200) - band(duck, 800, 1200)) - 14) < 2.0
    # gain automation (an under ramp -14 -> 0 over the section)
    sec2 = {**sec, "speech": [], "duck_bands": None, "gain_keys": [[0, -14], [6, 0]]}
    M.music_bed(bf["cfg"], None, [sec2], out)
    b = sf.read(str(out))[0][:, 0]
    assert 12 < _db(b[-sr // 2:]) - _db(b[:sr // 2]) < 15


def test_sfx_riser_end_alignment_drop_and_offset(bf):
    from cutroom import music as M
    from cutroom import render as R

    class C:
        cfg = bf["cfg"]

        def warn(self, *a, **k):
            pass
    n = 48000 * 8
    ev = [{"name": "sub_riser", "t": 5.0, "gain_db": 0, "align": "end", "dur": 4}]
    buf = R.sfx_track(C(), ev, n)[:, 0]
    env = np.array([_db(buf[i:i + 480]) for i in range(0, n - 480, 480)])
    k = int(5.0 * 100)
    assert _db(buf[int(4.5 * 48000):int(5.0 * 48000)]) > _db(buf[int(1.5 * 48000):int(2.0 * 48000)]) + 20   # climbs
    assert env[k + 1] < -100 and env[k - 1] > -20     # and stops dead on it
    assert M.sfx_align("sub_riser") == "end" and M.sfx_align("sub_drop") == "peak"
    ev = [{"name": "sub_drop", "t": 3.0, "gain_db": 0, "align": "peak", "offset": -0.14}]
    buf = R.sfx_track(C(), ev, n)[:, 0]
    pk = np.argmax(np.abs(buf)) / 48000
    assert abs(pk - 2.86) < 0.03                       # the drop's peak leads the mark by 0.14 s
    sp = np.abs(np.fft.rfft(buf))
    f = np.fft.rfftfreq(len(buf), 1 / 48000)
    assert f[np.argmax(sp)] < 70                       # it is sub (60 -> 30 Hz)
    p = M.sfx("sub_riser", bf["cfg"], dur=6)
    assert Path(p).name.startswith("sub_riser_") and p != M.sfx("sub_riser", bf["cfg"], dur=5)


def test_sound_automation_voice_memo_end_card(bf):
    """A muffled walk-in, a near-silence hole with a riser that stops on the voice, a raw audio-only memo as the
    voice-over, then a centred logo card on black."""
    ids = bf["ids"]
    plan = {"style": {"captions": {"enabled": False}, "voice": {"clean": True}},
            "sections": [
                {"name": "noise", "segments": [{"type": "broll", "clip": ids["a"], "in": 0.5, "out": 2.5, "audio": "natural",
                                                "sound": {"lowpass": 1200}}]},
                {"name": "silence", "segments": [{"type": "broll", "clip": ids["a"], "in": 3.0, "out": 5.0, "audio": "natural",
                                                  "sound": {"gain_db": -30},
                                                  "sfx": [{"name": "sub_riser", "on": "end", "dur": 1.5, "gain_db": -6}]}]},
                {"name": "voice", "segments": [{"type": "broll", "clip": ids["a"], "in": 5.0, "out": 7.0,
                                                "voiceover": {"clip": ids["memo"], "in": 0.0, "out": 1.0, "clean": False}}]},
                {"name": "end", "segments": [{"type": "image", "file": str(bf["logo"]), "dur": 1.0, "size": 0.4}]}]}
    out, side = _render(bf, "sound", plan)
    a = _audio(out)
    sr = 48000
    noise, hole = a[int(0.3 * sr):int(1.7 * sr)], a[int(2.3 * sr):int(3.0 * sr)]
    assert _db(hole) < _db(noise) - 18                      # the hole sits ~30 dB under the walk-in
    sp = np.abs(np.fft.rfft(noise))
    f = np.fft.rfftfreq(len(noise), 1 / sr)
    assert sp[f > 5000].mean() < 0.05 * sp[(f > 200) & (f < 800)].mean()     # low-passed at 1.2 kHz
    vo = next(s for s in side["segments"] if s["section"] == "voice")
    assert vo["audio"] == "voice"
    # the memo is the raw file (no de-noise), and the riser ends on the voice's cut
    ev = next(e for e in side["sfx"] if e["name"] == "sub_riser")
    assert ev["align"] == "end" and abs(ev["t"] - vo["start"]) < 1e-3
    # end card: logo centred, black around, the picture hard-cuts into it
    fr = _frames(out)
    card = next(s for s in side["segments"] if s["kind"] == "image")
    f0 = int(round(card["start"] * FPS))
    img = fr[f0 + 3].astype(int)
    assert img[H // 2, W // 2].min() > 200 and img[5, 5].max() < 20 and img[H // 2, 20].max() < 20
    assert fr[f0 - 1].astype(int).mean() > 40                # no fade: the frame before is the picture


def test_registered_audio_only_memo(bf):
    row = bf["con"].execute("SELECT status, width, has_audio FROM clips WHERE id=?", (bf["ids"]["memo"],)).fetchone()
    assert row["status"] == "audio" and row["width"] == 0 and row["has_audio"] == 1


def test_programme_dynamics_follows_reference(bf, monkeypatch, tmp_path):
    """style.dynamics match: the finished mix takes the reference's envelope (quiet first half, loud second)."""
    from cutroom import render as R
    t = np.round(np.arange(0.1, 6.0, 0.1), 3)
    env = {"t": t.tolist(), "M": [(-38.0 if x < 3 else -14.0) for x in t], "I": -16.0, "swing": 24.0}
    monkeypatch.setattr(R, "_ref_envelope", lambda cfg, rid: env)
    ids = bf["ids"]
    plan = {"reference": "fake", "style": {"dynamics": {"match": "reference"}},
            "sections": [{"name": "a", "segments": [{"type": "broll", "clip": ids["a"], "in": 0.5, "out": 6.5, "audio": "natural"}]}]}
    monkeypatch.setattr(R, "plan_reference_info", lambda ctx, rid: {"id": rid, "devices": []})
    out, side = _render(bf, "pdyn", plan)
    a = _audio(out)
    sr = 48000
    assert _db(a[int(3.6 * sr):int(5.6 * sr)]) - _db(a[int(0.6 * sr):int(2.6 * sr)]) > 14
    pd = side["programme_dynamics"]
    assert pd["status"] == "ok" and pd["result"]["rms_err_lu"] < 4


# ------------------------------------------------------------------------------------------------ picture
def test_exposure_ramp_burst_and_slowmo(bf):
    ids = bf["ids"]
    plan = {"style": {"grade": {"normalize_exposure": False}},
            "sections": [{"name": "s", "segments": [
                {"type": "broll", "clip": ids["a"], "in": 1.0, "out": 3.0, "audio": "music",
                 "exposure_stops": {"from": -1.5, "to": 0.5}},
                {"type": "montage", "beat_sync": False, "min_shot": 0.2, "audio": "music",
                 "shots": [{"clip": ids["a"], "in": float(k), "dur": 0.2} for k in range(4)]},
                {"type": "broll", "clip": ids["b"], "in": 0.5, "out": 1.0, "slowmo": 4, "audio": "natural"},
                {"type": "broll", "clip": ids["c"], "in": 1.0, "out": 1.4, "slowmo": {"factor": 8}}]}]}
    out, side = _render(bf, "pic", plan)
    fr = _frames(out)
    lum = fr.astype(float).mean(axis=(1, 2, 3))
    assert lum[45] > lum[3] + 25 and lum[25] > lum[3]          # the ramp brightens across the one shot
    burst = [s for s in side["segments"] if s["type"] == "montage"]
    assert [s["frames"] for s in burst] == [5, 5, 5, 5]
    sm4 = next(s for s in side["segments"] if (s.get("slowmo") or {}).get("factor") == 4)
    assert sm4["slowmo"]["method"] == "frames" and sm4["frames"] == 50 and sm4["audio"] == "none"
    f0 = int(round(sm4["start"] * FPS))
    d = [np.abs(fr[i + 1].astype(int) - fr[i]).mean() for i in range(f0, f0 + 20)]
    assert min(d) > 0.3                                          # every frame a real new 100p frame: no duplicates
    sm8 = next(s for s in side["segments"] if (s.get("slowmo") or {}).get("factor") == 8)
    assert sm8["slowmo"]["method"] == "flow" and sm8["frames"] == 80
    assert any("optical-flow" in w for w in side["warnings"])
    f0 = int(round(sm8["start"] * FPS))
    d = [np.abs(fr[i + 1].astype(int) - fr[i]).mean() for i in range(f0 + 2, f0 + 30)]
    assert min(d) > 0.05                                         # interpolated, not frozen / duplicated


def test_plan_validation_of_new_fields():
    from cutroom import plan as P
    ok = {"sections": [{"name": "a", "sound": {"gain_db": -30}, "ref_span": [0, 16.2],
                        "music": {"song": "x", "duck_bands": {"sub": -25, "mid": -14}, "under_db": {"from": -14, "to": 0},
                                  "lowpass": {"from": 2200, "to": 4000, "at": 0, "over": 15}},
                        "segments": [{"type": "broll", "clip": "c", "in": 0, "out": 1, "sound": {"lowpass": [[0, 1900], [2, 4500]]},
                                      "exposure_stops": [[0, -1], [3, 0]], "slowmo": {"factor": 8, "method": "flow"},
                                      "sfx": [{"name": "sub_riser", "dur": 6, "on": "end"}, {"name": "sub_drop", "offset": -0.14}],
                                      "voiceover": {"clip": "m", "in": 0, "out": 1, "clean": False}},
                                     {"type": "image", "file": "logo.png", "dur": 2.8},
                                     {"type": "montage", "min_shot": 0.2, "shots": [{"clip": "c", "in": 0, "dur": 0.2, "look": "bw_contrast"}]}],
                        "frame": {"kind": "pillarbox"}}],
          "style": {"dynamics": {"match": "reference"}}}
    assert P.validate(P.normalize(ok)) == []
    bad = json.loads(json.dumps(ok))
    s = bad["sections"][0]
    s["sound"] = {"gain_db": -90, "lowpss": 3}
    s["segments"][0]["slowmo"] = 40
    s["segments"][0]["exposure_stops"] = {"from": -5, "to": 0}
    s["segments"][1]["file"] = ""
    s["segments"][2]["min_shot"] = 0.01
    s["frame"] = {"kind": "pillarbox", "aspect": 2.0}
    s["music"]["duck_bands"] = {"bass": -3}
    errs = " | ".join(P.validate(P.normalize(bad)))
    for frag in ("gain_db must be -60..24", "unknown keys", "slowmo factor", "exposure_stops", "image needs file",
                 "min_shot", "pillarbox aspect", "duck_bands"):
        assert frag in errs, frag


# ------------------------------------------------------------------------------------------------ story
def test_story_breaks_and_hints(bf):
    from cutroom import asr, story
    ws = asr.words(bf["cfg"], bf["ids"]["memo"])
    bk = story.breaks(ws)
    assert [b["kind"] for b in bk] == ["sentence", "clause"] and abs(bk[0]["t"] - 1.15) < 1e-6
    ids = bf["ids"]

    def vo(a, b):
        return {"type": "broll", "clip": ids["c"], "in": 1, "out": 2, "voiceover": {"clip": ids["memo"], "in": a, "out": b}}
    burst = {"type": "montage", "beat_sync": False, "shots": [{"clip": ids["a"], "in": float(k), "dur": 0.3} for k in range(6)]}
    plan = {"sections": [{"name": "v", "segments": [vo(0.0, 1.15), vo(1.15, 2.0), vo(2.0, 4.2)]},
                         {"name": "leap", "segments": [burst]}]}
    hs = story.hints(plan, bf["con"], bf["cfg"])
    assert any("inside the word 'love'" in h for h in hs)
    assert any("callback" in h for h in hs)
    # a burst that replays 3 earlier windows passes
    plan["sections"].insert(0, {"name": "early", "segments": [{"type": "broll", "clip": ids["a"], "in": float(k), "out": k + 0.5}
                                                              for k in range(3)]})
    assert not any("callback" in h for h in story.hints(plan, bf["con"], bf["cfg"]))
