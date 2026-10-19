"""Craft upgrades, measured on synthetic footage: room tone + crossfaded joins, the grade order, the
cold-open template, music structure, flash + hit on the cut, J-cuts, caption size, frame-rate conform,
push-ins, speed ramps and pacing. Every number here is measured from rendered output (or from the
filter strings / buses where the property is structural)."""
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pytest

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]
FPS = 25
# talk with a steady 1 kHz "voice" tone: the J-cut lead is measured on it
TT_WORDS = [(0.5, 2.4, "the biggest thing we did was this."), (2.6, 4.2, "then we went outside"),
            (6.5, 8.4, "later that night it rained"), (8.6, 10.4, "and that was the whole day.")]
# talk whose pauses were gated by a denoiser: phrases 1 and 3 sit on a ~-42 dBFS floor, 2 and 4 ~12.5 dB lower
# (the rough cut measured a 12.3 dB median step at talk joins)
GT_WORDS = [(0.4, 2.6, "first we walked to the market"), (3.4, 5.6, "it was busy and loud there"),
            (6.4, 8.6, "then we found the little cafe"), (9.4, 11.6, "and stayed until it closed.")]


def _ff(*a):
    subprocess.run(FF + [str(x) for x in a], check=True)


def _transcript(phr):
    segs = []
    for t0, t1, text in phr:
        ws = text.split()
        step = (t1 - t0) / len(ws)
        segs.append({"start": t0, "end": t1, "text": text,
                     "words": [{"start": round(t0 + i * step, 3), "end": round(t0 + (i + 0.85) * step, 3),
                                "word": " " + w, "probability": 0.99} for i, w in enumerate(ws)]})
    return {"language": "en", "model": "hand-written", "segments": segs}


def make_library(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    p = {}
    plate = "color=c=0x2a3440:s=1280x720:r=25:d={d}"
    # tt: steady 1 kHz tone at -20 dBFS + a little pink noise
    p["tt"] = root / "tt.mp4"
    _ff("-f", "lavfi", "-i", plate.format(d=12), "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=12",
        "-f", "lavfi", "-i", "sine=f=1000:d=12:sample_rate=48000", "-f", "lavfi", "-i", "anoisesrc=d=12:c=pink:r=48000:a=0.004",
        "-filter_complex", "[0:v][1:v]overlay=x='200+100*sin(t)':y=200[v];[2:a]volume=0.14[s];[s][3:a]amix=inputs=2:normalize=0,"
        "aformat=channel_layouts=stereo[a]", "-map", "[v]", "-map", "[a]", *X264, "-c:a", "aac", "-b:a", "160k", "-shortest", p["tt"])
    # gt: speech-like bursts over floors that alternate between -42 and -66 dBFS
    floor = "+".join(f"between(t,{a - 0.4},{b + 0.4})*{0.0110 if k % 2 == 0 else 0.0026}" for k, (a, b, _) in enumerate(GT_WORDS))
    speech = "(" + "+".join(f"between(t,{a},{b})" for a, b, _ in GT_WORDS) + ")*gt(sin(2*PI*2.7*t),-0.3)"   # words + gaps
    p["gt"] = root / "gt.mp4"
    _ff("-f", "lavfi", "-i", plate.format(d=12.5), "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=12.5",
        "-f", "lavfi", "-i", f"anoisesrc=d=12.5:c=white:r=48000:a=1.0",
        "-f", "lavfi", "-i", f"aevalsrc='0.35*sin(2*PI*190*t)*(0.55+0.45*sin(2*PI*5*t))*({speech})':d=12.5:s=48000",
        "-filter_complex", f"[2:a]lowpass=f=4000,volume='({floor})':eval=frame[n];[n][3:a]amix=inputs=2:normalize=0,"
        "aformat=channel_layouts=stereo[a];[0:v][1:v]overlay=x='300+80*sin(t)':y=240[v]",
        "-map", "[v]", "-map", "[a]", *X264, "-c:a", "aac", "-b:a", "160k", "-shortest", p["gt"])
    for k, src in {"b1": "testsrc2=s=1280x720:r=25:d=8,hue=h=60", "b2": "testsrc2=s=1280x720:r=25:d=8,hue=h=200,negate"}.items():
        p[k] = root / f"{k}.mp4"
        _ff("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", "sine=f=330:d=8", *X264, "-c:a", "aac", "-shortest", p[k])
    p["ntsc"] = root / "ntsc.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24000/1001:d=8", "-f", "lavfi", "-i", "sine=f=500:d=8",
        *X264, "-r", "24000/1001", "-c:a", "aac", "-shortest", p["ntsc"])
    p["sq"] = root / "sq.mp4"
    _ff("-f", "lavfi", "-i", "color=c=0x303030:s=1280x720:r=25:d=6,drawbox=x=540:y=260:w=200:h=200:color=white:t=fill,noise=alls=8:allf=t",
        *X264, p["sq"])
    p["mv"] = root / "mv.mp4"
    _ff("-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25:d=8", "-f", "lavfi", "-i", "color=c=white:s=40x120:r=25:d=8",
        "-filter_complex", "[0:v][1:v]overlay=x='40+100*t':y=300,noise=alls=8:allf=t[v]", "-map", "[v]", *X264, p["mv"])
    p["song"] = root / "click120.wav"
    _ff("-f", "lavfi", "-i", "aevalsrc='(0.6+0.4*eq(mod(floor(t*2),4),0))*sin(2*PI*1000*t)*exp(-60*mod(t,0.5))':d=40:s=48000",
        "-f", "lavfi", "-i", "sine=f=110:d=40", "-filter_complex",
        "[0:a][1:a]amix=inputs=2:weights=0.3 0.5,aformat=channel_layouts=stereo", "-c:a", "pcm_s16le", p["song"])
    return p


def craft_plan(lib):
    return {
        "title": "Craft", "version": 1,
        "style": {"captions": {"position": "bottom"}, "voice": {"clean": False}, "loudness": {"I": -14, "TP": -1.5}},
        "cold_open": {"dur": 5.0, "lines": [{"from": "talk/1"}], "shots": "auto", "asl": 0.45},
        "sections": [
            {"name": "open", "music": {"song": str(lib["song"]), "offset": 2.0, "gain_db": -12},
             "segments": [
                 {"type": "broll", "clip": "b1.mp4", "in": 0.5, "out": 2.5, "audio": "natural"},
                 {"type": "broll", "clip": "b2.mp4", "in": 0.5, "out": 2.5, "audio": "natural",
                  "transition": {"kind": "flash", "frames": 4, "sfx": "hit", "gain_db": -3}},
                 {"type": "montage", "beat_sync": True, "shots": [{"clip": "b1.mp4", "in": 4.0}, {"clip": "b2.mp4", "in": 5.0}]},
             ]},
            {"name": "talk",
             "segments": [
                 {"type": "broll", "clip": "b2.mp4", "in": 3.0, "out": 4.6, "audio": "natural"},
                 {"type": "talk", "clip": "tt.mp4", "in": 0.5, "out": 10.8, "lead_in": 0.4, "room_tone": False},
                 {"type": "broll", "clip": "b1.mp4", "in": 5.0, "out": 6.5, "audio": "natural"},
                 {"type": "talk", "clip": "gt.mp4", "in": 0.2, "out": 12.0},
             ]},
            {"name": "pics",
             "segments": [
                 {"type": "broll", "clip": "sq.mp4", "in": 0.5, "out": 3.5, "push": {"to": 1.06, "ease": "linear"}},
                 {"type": "broll", "clip": "ntsc.mp4", "in": 0.5, "out": 2.5, "conform": "dup"},
                 {"type": "broll", "clip": "ntsc.mp4", "in": 4.0, "out": 6.0},
                 {"type": "broll", "clip": "mv.mp4", "in": 0.5, "out": 3.0, "ramp": {"to": 3.0, "over": 0.8}},
             ]},
        ],
    }


@pytest.fixture(scope="module")
def cproj(tmp_path_factory):
    from cutroom import db, ingest
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("craft")
    lib = make_library(base / "footage")
    cfg = init_project(base / "work", [base / "footage"])
    cfg.workers = 3
    cfg.out_width, cfg.out_height = 1280, 720
    cfg.preview_height = 360
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ids = {k: db.clip_id(v) for k, v in lib.items() if k != "song"}
    cfg.p("transcripts", f"{ids['tt']}.json").write_text(json.dumps(_transcript(TT_WORDS)))
    cfg.p("transcripts", f"{ids['gt']}.json").write_text(json.dumps(_transcript(GT_WORDS)))
    pp = cfg.p("plans", "craft.json")
    pp.write_text(json.dumps(craft_plan(lib), indent=1))
    return {"cfg": cfg, "con": con, "lib": lib, "ids": ids, "plan": pp}


@pytest.fixture(scope="module")
def craft(cproj):
    from cutroom import render
    out = render.render_plan(cproj["cfg"], cproj["con"], cproj["plan"], preview=True)
    return Path(out), json.loads(Path(out[:-4] + ".json").read_text())


@pytest.fixture(scope="module")
def baseline_talk(cproj):
    """The talk section the old way: butt joins, no room tone."""
    from cutroom import render
    plan = json.loads(cproj["plan"].read_text())
    plan["title"] = "Craft base"
    plan["style"]["audio"] = {"room_tone": False, "join_xfade_s": 0.0}
    pp = cproj["cfg"].p("plans", "craft_base.json")
    pp.write_text(json.dumps(plan))
    out = render.render_plan(cproj["cfg"], cproj["con"], pp, preview=True, only="talk")
    return Path(out), json.loads(Path(out[:-4] + ".json").read_text())


@pytest.fixture(scope="module")
def craft_qa(cproj, craft):
    from cutroom import qa
    return qa.run(cproj["cfg"], craft[0])


def _audio(path, sr=16000):
    r = subprocess.run(FF[:-1] + ["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32)


def _gray(path, w=64, h=36):
    r = subprocess.run(FF[:-1] + ["-i", str(path), "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w).astype(np.float32)


def _band_db(x, sr, f0, f1, t0, t1):
    seg = x[int(t0 * sr):int(t1 * sr)]
    if len(seg) < 64:
        return -120.0
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    fr = np.fft.rfftfreq(len(seg), 1 / sr)
    return float(10 * np.log10(sp[(fr >= f0) & (fr <= f1)].sum() / len(seg) ** 2 + 1e-12))


def _seg(side, sec, typ, k=0):
    rows = [s for s in side["segments"] if s["section"] == sec and s["type"] == typ]
    return rows[k]


# ------------------------------------------------------------------------------------------- P0-1
def test_room_tone_continuous(craft, baseline_talk):
    from cutroom import qa
    (path, side), (bpath, bside) = craft, baseline_talk
    st, _ = qa.floor_steps(_audio(path), 16000, side["talk_joins"])
    bst, _ = qa.floor_steps(_audio(bpath), 16000, bside["talk_joins"])
    med = float(np.median([abs(x["step"]) for x in st]))
    bmed = float(np.median([abs(x["step"]) for x in bst]))
    assert len(st) >= 4 and len(bst) == len(st)
    assert med <= 3.0, st
    assert bmed > 6.0, bst                       # without the bed the gated joins step by > 6 dB
    assert side["room_tone"] and all(b["level_db"] <= b["voice_db"] - 20 for b in side["room_tone"])
    # equal-power crossfades: pieces overlap by the 20 ms handles
    talk = [s for s in side["segments"] if s["type"] == "talk"]
    assert any(abs(s["audio_pre"] - 0.01) < 1e-3 for s in talk) and any(abs(s["audio_post"] - 0.01) < 1e-3 for s in talk)


def test_room_tone_steps_qa(craft_qa):
    c = next(c for c in craft_qa["checks"] if c["id"] == "room_tone_steps")
    assert c["value"]["median_abs_db"] is not None and c["value"]["median_abs_db"] <= 3.0, c["value"]
    assert c["value"]["room_tone_beds"] >= 1


# ------------------------------------------------------------------------------------------- P0-2
def test_grade_order_and_exposure_math():
    from cutroom import render

    class C:
        lut = "/tmp/x.cube"
        style = {"grade": {"saturation": 1.0, "finish": {"enabled": True, "black": 0.028, "shoulder": 0.96, "vibrance": 0.18}}}

        def info(self, src):
            return {"range": "pc", "w": 1920, "h": 1080}

        def pregraded(self, row, src):
            return False

        def warn(self, *a, **k):
            pass
    f, tag = render.grade_chain(C(), {"is_log": 1}, "x", 1280, 720, stops=0.8, pre_fx=[(10, 1.0)])
    s = ",".join(f)
    assert tag == "lut" and "eq=brightness" not in s
    i_exp, i_lut = s.index("curves=all='0/0.0600"), s.index("lut3d")
    i_fin = s.index("curves=master='0/0 0.06/0.0280")
    assert i_exp < i_lut < i_fin                  # exposure in log BEFORE the LUT, one finish after it
    assert s.index(":enable='eq(n,10)'") < i_lut  # flash push also pre-LUT
    probe, _ = render.grade_chain(C(), {"is_log": 1}, "x", 640, 360, probe=True)
    assert not any(x.startswith("curves") for x in probe)     # exposure is measured through the LUT only
    # bounded, half the measured error, deadband, night stays night
    assert render.exposure_stops(118) == 0.0
    assert render.exposure_stops(110) == 0.0                   # inside +-1/3 stop
    assert render.exposure_stops(200) == pytest.approx(0.5 * 2.4 * math.log2(118 / 200), abs=1e-3)
    assert render.exposure_stops(250) == -1.0 or render.exposure_stops(250) > -1.0 - 1e-9
    assert render.exposure_stops(20) == 1.0                    # clamped
    assert render.exposure_stops(50) == pytest.approx(0.5 * 2.4 * math.log2(70 / 50), abs=1e-3)


def test_black_level_after_lut(cproj, tmp_path):
    """A lifted (LC-709-like) LUT: the old post-LUT brightness lift leaves milky blacks; the house finish
    pins them near the reference's ~10/255."""
    from cutroom import render
    cube = tmp_path / "lift.cube"
    n = 9
    rows = [f"{0.06 + 0.88 * r / (n - 1):.5f} {0.06 + 0.88 * g / (n - 1):.5f} {0.06 + 0.88 * b / (n - 1):.5f}"
            for b in range(n) for g in range(n) for r in range(n)]
    cube.write_text(f"LUT_3D_SIZE {n}\n" + "\n".join(rows) + "\n")

    class C:
        lut = str(cube)
        style = {"grade": {"saturation": 1.0, "finish": {"enabled": True}}}

        def info(self, src):
            return {"range": "tv", "w": 1280, "h": 720}

        def pregraded(self, row, src):
            return False

        def warn(self, *a, **k):
            pass
    src = cproj["lib"]["mv"]
    new, _ = render.grade_chain(C(), {"is_log": 1}, str(src), 320, 180, stops=0.0)
    old = [f"scale=320:180:flags=lanczos:in_range=tv:out_range=full:in_color_matrix=bt709", "format=gbrpf32le",
           f"lut3d=file='{cube}':interp=tetrahedral", "format=gbrp16le", render.HOUSE_TAIL,
           "eq=brightness=0.0400:gamma=1.0400"]
    p1 = {}
    for k, ch in (("new", new), ("old", old)):
        r = subprocess.run(FF[:-1] + ["-i", str(src), "-frames:v", "1", "-vf", ",".join(ch) + ",format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, check=True)
        p1[k] = float(np.percentile(np.frombuffer(r.stdout, np.uint8), 1))
    assert p1["old"] >= p1["new"] + 6 and 2 <= p1["new"] <= 13, p1


def test_black_level_qa(craft_qa):
    c = next(c for c in craft_qa["checks"] if c["id"] == "black_level")
    assert c["value"]["median_p1"] is not None and c["value"]["shots"] >= 5


# ------------------------------------------------------------------------------------------- P0-3
def test_cold_open_structure(cproj, craft):
    from cutroom import render
    _, side = craft
    co = [s for s in side["segments"] if s["section"] == "cold open"]
    assert co and abs(co[-1]["end"] - 5.0) <= 0.05
    cuts = sorted({round(s["start"], 3) for s in co} | {round(c["t0"], 3) for s in co for c in s["cutaways"]})
    asl = 5.0 / len(cuts)
    assert 0.3 <= asl <= 0.65, (asl, cuts)
    assert any(s["type"] == "talk" for s in co)                     # a teaser line from later in the edit
    line = next(s for s in co if s["type"] == "talk")
    assert any(s["type"] == "talk" and s["clip"] == line["clip"] for s in side["segments"] if s["section"] == "talk")
    assert side["sections"][0]["generated"] == "cold_open" and side["sections"][0]["music"]["mode"] == "under"
    # a flash out of the cold open into the edit
    first_edit = next(s for s in side["segments"] if s["section"] != "cold open")
    assert any(abs(t["t"] - first_edit["start"]) < 1e-3 and t["kind"] == "flash" for t in side["transitions"])
    res = render.resolve_timeline(cproj["cfg"], cproj["con"], json.loads(cproj["plan"].read_text()))
    gen = [p for p in res["pieces"] if p.get("generated") == "cold_open"]
    assert gen and all(p["sec"] == -1 for p in gen)


def test_cold_open_from_reference(cproj):
    from cutroom import render
    cfg = cproj["cfg"]
    d = cfg.p("refs", "refteaser")
    d.mkdir(parents=True, exist_ok=True)
    starts = [0.0, 0.8, 0.96, 1.12, 1.6, 2.04, 2.2, 2.8, 3.24, 3.4, 4.0]
    (d / "meta.json").write_text(json.dumps({"id": "refteaser", "title": "ref teaser"}))
    (d / "analysis.json").write_text(json.dumps({
        "sections": [{"i": 0, "start": 0.0, "end": 4.6, "asl": 0.42, "name": "cold open"}],
        "shots": [{"i": i, "start": t, "dur": 0.2} for i, t in enumerate(starts)],
        "structure": {"time_to_first_word_s": 1.43}}))
    plan = json.loads(cproj["plan"].read_text())
    plan["cold_open"] = {"from_reference": "refteaser", "lines": [{"from": "talk/1"}], "shots": "auto"}
    res = render.resolve_timeline(cfg, cproj["con"], plan)
    gen = sorted({p["start"] for p in res["pieces"] if p.get("generated") == "cold_open" and p["track"] in ("V1", "V2")})
    end = max(p["end"] for p in res["pieces"] if p.get("generated") == "cold_open")
    assert abs(end - 4.6) < 0.05
    matched = sum(1 for c in starts[1:] if any(abs(c - g) <= 1.0 / FPS + 1e-6 for g in gen))
    assert matched >= len(starts[1:]) - 2, (gen, starts)       # the reference cut map, one to one (line edges excepted)


# ------------------------------------------------------------------------------------------- P0-4
def test_music_bed_structure(tmp_path):
    from cutroom import music
    import soundfile as sf
    sr = 48000
    song = tmp_path / "s.wav"
    t = np.arange(int(sr * 12)) / sr
    sf.write(song, np.stack([0.3 * np.sin(2 * np.pi * 220 * t)] * 2, 1).astype(np.float32), sr)
    out = tmp_path / "bed.wav"

    class Cfg:
        ffmpeg = "ffmpeg"
    music.music_bed(Cfg(), None, [
        {"song": str(song), "offset": 0, "start": 0.0, "dur": 4.0, "gain_db": -10, "mode": "mute",
         "speech": [(2.0, 3.0)], "start_edge": "cut", "end_edge": "cut", "lift_db": 7, "breaks": [(1.5, 0.3)]},
        {"song": str(song), "offset": 0, "start": 4.0, "dur": 4.0, "gain_db": -10, "mode": "under", "under_db": -5,
         "voice_lufs": -16, "speech": [(5.0, 7.0)], "start_edge": "cut", "end_edge": "cut"},
        {"song": str(song), "offset": 0, "start": 8.0, "dur": 3.0, "gain_db": -10, "mode": "mute",
         "speech": [], "natural": [(8.5, 10.5)], "natural_duck_db": 0, "lift_db": 7, "start_edge": "cut", "end_edge": "cut"},
    ], out)
    a, _ = sf.read(out, dtype="float32", always_2d=True)
    a = a[:, 0]

    def lev(t0, t1):
        x = a[int(t0 * sr):int(t1 * sr)]
        return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)
    # hard start: full level 10 ms after the section's first sample (8 ms ramp), no 0.3 s fade
    assert abs(lev(0.01, 0.03) - lev(0.5, 1.0)) < 0.5
    assert abs(lev(3.95, 3.99) - lev(0.5, 1.0)) < 1.0            # hard stop on the cut
    # lift: music +7 dB where nobody talks, silent under talk (mute)
    # (a 0.3-amplitude sine is about -13.5 LUFS, so -10 + 7 stays under the voice + 7 = -9 cap)
    assert abs(lev(0.5, 1.0) - (-10 + 7 + 20 * np.log10(0.3 / np.sqrt(2)))) < 0.6
    assert lev(2.3, 2.7) < -60
    # break: silence for 0.3 s that ends exactly on the event, music back right after
    assert lev(1.22, 1.49) < lev(0.5, 1.0) - 25 and lev(1.22, 1.49) > -90 and lev(1.52, 1.7) > lev(0.5, 1.0) - 1
    # under: never ducked; sits 5 dB under the voice target (K-weighted LUFS)
    assert abs(lev(5.2, 6.8) - lev(4.2, 4.9)) < 0.5
    lufs = music.k_weighted_lufs(np.stack([a[int(4.1 * sr):int(7.9 * sr)]] * 2, 1))
    assert abs(lufs - (-21)) < 1.0, lufs
    # natural sound does not duck the music
    assert abs(lev(9.0, 10.0) - lev(8.1, 8.4)) < 0.5


def test_natural_broll_not_ducked(craft):
    """In a render: the music (110 Hz bed) under a natural-sound b-roll is not ducked by 18 dB, and it is
    lifted the same over b-roll and montage (both non-talk)."""
    path, side = craft
    x = _audio(path, 16000)
    b1 = _seg(side, "open", "broll", 0)
    mont = _seg(side, "open", "montage", 0)
    lb = _band_db(x, 16000, 100, 120, b1["start"] + 0.3, b1["end"] - 0.3)
    lm = _band_db(x, 16000, 100, 120, mont["start"] + 0.1, mont["end"] - 0.1)
    assert abs(lb - lm) < 3.0, (lb, lm)


# ------------------------------------------------------------------------------------------- P0-5
def test_flash_and_hit_on_the_cut(craft, craft_qa):
    path, side = craft
    tr = next(t for t in side["transitions"] if t["sec"] == 0 and t["seg"] == 1)
    g = _gray(path).mean(axis=(1, 2))
    f = tr["frame"]
    pk = f - 3 + int(np.argmax(g[f - 3:f + 4]))
    assert abs(pk - f) <= 1, (g[f - 4:f + 5], f)
    assert g[pk] > g[f - 5] + 40 and g[pk] > g[f + 6] + 40           # a real light event, not a dissolve
    c = next(c for c in craft_qa["checks"] if c["id"] == "flash_sfx_alignment")
    row = next(r for r in c["value"]["transitions"] if abs(r["t"] - tr["t"]) < 1e-3)
    assert row["sfx_offset_ms"] is not None and abs(row["sfx_offset_ms"]) <= 5.0, row
    ev = next(e for e in side["sfx"] if str(e.get("on", "")).startswith("transition:") and abs(e["t"] - tr["t"]) < 1e-3)
    assert ev["align"] == "peak" and ev["peak_s"] > 0


def test_transition_filters_dip_and_whip():
    from cutroom import render
    at = {"tr_out": {"kind": "dip", "frames_out": 2, "frames_in": 2}, "tr_in": None}
    f = render.transition_filters(at, 50)
    assert len(f) == 2 and "eq(n,48)" in f[0] and "(16-val)*1.000" in f[1]
    at = {"tr_in": {"kind": "whip", "frames_out": 3, "frames_in": 3}}
    f = render.transition_filters(at, 50)
    assert f[0].startswith("split") and "tmix=frames=3" in f[1] and "avgblur" in f[1] and "lt(n,3)" in f[2]


# ------------------------------------------------------------------------------------------- P0-6
def test_j_cut_voice_leads_picture(craft):
    path, side = craft
    talk = next(s for s in side["segments"] if s["section"] == "talk" and s["type"] == "talk")
    assert abs(talk["audio_pre"] - 0.4) < 1e-3
    x = _audio(path, 16000)
    sr = 16000
    hop = int(0.005 * sr)
    cut = talk["start"]
    t0 = cut - 1.0
    seg = x[int(t0 * sr):int((cut + 0.6) * sr)]
    k = len(seg) // hop
    env = []
    for i in range(k):
        w = seg[i * hop:i * hop + 4 * hop]
        if len(w) < hop * 4:
            break
        sp = np.abs(np.fft.rfft(w * np.hanning(len(w)))) ** 2
        fr = np.fft.rfftfreq(len(w), 1 / sr)
        env.append(10 * np.log10(sp[(fr > 950) & (fr < 1050)].sum() + 1e-12))
    env = np.array(env)
    steady = np.median(env[-20:])
    onset = t0 + (int(np.argmax(env > steady - 10)) * hop + 2 * hop) / sr
    lead = cut - onset
    assert abs(lead - 0.4) <= 1.0 / FPS + 0.03, (lead, steady)
    # the b-roll's natural sound (330 Hz) ducks under the arriving voice
    under = _band_db(x, sr, 320, 340, cut - 0.25, cut - 0.05)
    before = _band_db(x, sr, 320, 340, cut - 1.2, cut - 0.8)
    assert under < before - 6, (under, before)


# ------------------------------------------------------------------------------------------- P0-7
def test_caption_size(craft, craft_qa):
    _, side = craft
    assert side["captions_style"]["size"] == 43 and side["captions_style"]["px"] == round(43 * 360 / 1080)
    caps = [c for s in side["segments"] for c in s["captions"]]
    assert caps and all(c["px"] == side["captions_style"]["px"] for c in caps)
    c = next(c for c in craft_qa["checks"] if c["id"] == "caption_size")
    assert c["status"] == "pass" and abs(c["value"]["pct_of_height"] - 3.98) < 0.4


# ------------------------------------------------------------------------------------------- P1-8
def test_conform_no_duplicate_frames(craft, craft_qa):
    _, side = craft
    dup, auto = [s for s in side["segments"] if s["section"] == "pics" and s["clip_rel"] == "ntsc.mp4"]
    assert dup["conform"] == "dup" and auto["conform"] == "speed"
    assert abs(auto["speed"] - 25 * 1001 / 24000) < 1e-4
    c = next(c for c in craft_qa["checks"] if c["id"] == "duplicate_frames")
    shots = {r["shot"]: r for r in c["value"]["shots"]}
    sh = {s["idx"] + 1: s for s in craft_qa["shots"]}
    n_dup = next((r["dups"] for k, r in shots.items() if abs(sh[k]["start"] - dup["start"]) < 0.05), 0)
    n_auto = next((r["dups"] for k, r in shots.items() if abs(sh[k]["start"] - auto["start"]) < 0.05), 0)
    assert n_dup >= 1 and n_auto == 0, (shots, dup["start"], auto["start"])


# ------------------------------------------------------------------------------------------- P1-9
def test_push_in_monotonic(craft):
    path, side = craft
    s = next(x for x in side["segments"] if x["clip_rel"] == "sq.mp4" and x["section"] == "pics")
    r = subprocess.run(FF[:-1] + ["-ss", f"{s['start']:.3f}", "-i", str(path), "-frames:v", str(s["frames"]),
                                  "-vf", "format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True)
    H, W = side["height"], side["width"]
    fr = np.frombuffer(r.stdout, np.uint8).reshape(-1, H, W).astype(np.float32)
    side_len = np.sqrt((fr > 128).sum(axis=(1, 2)))
    side_len = side_len[1:-1]
    assert np.all(np.diff(side_len) > -0.6), np.diff(side_len)       # never shrinks (sub-pixel noise allowed)
    assert abs(side_len[-1] / side_len[0] - 1.06) < 0.015, side_len[[0, -1]]


# ------------------------------------------------------------------------------------------- P1-10
def test_speed_ramp(craft):
    from cutroom import render
    path, side = craft
    s = next(x for x in side["segments"] if x["clip_rel"] == "mv.mp4" and x["section"] == "pics")
    assert s["frames"] == round(2.5 * FPS)                           # the ramp keeps the planned length
    r = subprocess.run(FF[:-1] + ["-ss", f"{s['start']:.3f}", "-i", str(path), "-frames:v", str(s["frames"]),
                                  "-vf", "format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True)
    H, W = side["height"], side["width"]
    fr = np.frombuffer(r.stdout, np.uint8).reshape(-1, H, W).astype(np.float32)
    xs = np.arange(W)
    fr = (fr > 128).astype(np.float32)
    cx = np.array([(f.sum(0) * xs).sum() / max(1.0, f.sum()) for f in fr])
    # the box moves 100 px/s of SOURCE time at 1280 wide -> scale to the preview width
    src_t = (cx * 1280 / W - 60) / 100.0
    v = np.diff(src_t) * FPS                                         # source seconds per output second = speed
    assert abs(np.median(v[5:30]) - 1.0) < 0.1, v[5:30]
    assert np.median(v[-6:-1]) > 2.2, v[-10:]
    at = {"n": s["frames"], "speed": 1.0, "ramp": {"to": 3.0, "over": 0.8}}
    rp = render.ramp_params(at, FPS)
    d = s["frames"] / FPS
    assert abs(rp["src_need"] - ((d - 0.8) + 0.8 * 2.0)) < 1e-6        # base part + the linear rise 1 -> 3


# ------------------------------------------------------------------------------------------- P1-11
def test_pacing(cproj):
    from cutroom import render
    d = render._pace_durations({"asl": 0.6, "alternate": True, "max_hold": 0.8}, 10)
    assert abs(np.mean(d) - 0.6) < 0.08 and max(d) <= 0.8 and min(d) < 0.5
    plan = json.loads(cproj["plan"].read_text())
    plan.pop("cold_open")
    sec = plan["sections"][1]
    sec["pace"] = {"max_hold": 2.0}
    sec["segments"][3].update({"auto_cover": True, "cover": [{"clip": "b1.mp4", "in": 0.2}]})
    res = render.resolve_timeline(cproj["cfg"], cproj["con"], plan)
    talk2 = next(r for r in res["segments"] if r["sec"] == 1 and r["seg"] == 3)
    v2 = sorted((p["start"], p["end"]) for p in res["pieces"] if p["track"] == "V2" and p["sec"] == 1 and p["seg"] == 3)
    assert v2
    edges = [talk2["start"]] + [x for a, b in v2 for x in (a, b)] + [talk2["end"]]
    holds = [edges[i + 1] - edges[i] for i in range(0, len(edges), 2)]
    assert max(holds) <= 2.0 + 0.3 + 1e-6, (holds, v2)


def test_plan_validation_of_craft_fields():
    from cutroom import plan as P
    bad = {"sections": [{"name": "a", "transition": {"kind": "burn"}, "music": {"song": "x", "start": "slow"},
                         "pace": {"asl": 0.01},
                         "segments": [{"type": "talk", "clip": "c", "in": 0, "out": 2, "lead_in": 5, "ramp": {"to": 2}},
                                      {"type": "broll", "clip": "c", "in": 0, "out": 2, "push": {"to": 1.5}, "conform": "x"}]}],
           "cold_open": {"dur": 100, "lines": [{"from": "nowhere/0"}]}}
    errs = "\n".join(P.validate(P.normalize(bad)))
    for k in ("transition kind", "music: start", "pace.asl", "lead_in", "ramp applies", "push.to", "conform", "cold_open.dur",
              "names no section"):
        assert k in errs, (k, errs)
