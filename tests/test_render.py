"""End-to-end render test on a synthetic library (lavfi clips, a speech-like talk track with a
hand-written transcript, a 120 BPM click song). Renders a plan that uses every segment type and
overlay kind, as a preview and as a final, and checks timing, beat cuts, captions, audio and sidecar."""
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p"]
FPS = 25
# talk clip: four phrases; the 2.3 s pause between phrase 2 and 3 is a jump cut auto_cover must hide
PHRASES = [(0.5, 2.4, "so this is the first thing we did"),
           (2.6, 4.2, "then we went outside"),
           (6.5, 8.4, "later that night it rained"),
           (8.6, 10.4, "and that was the whole day.")]


def _ff(*a):
    subprocess.run(FF + [str(x) for x in a], check=True)


def _speech_track(path: Path, dur: float):
    """Phrases placed at known times over -45 dBFS room tone. Uses macOS `say` when available,
    otherwise speech-like modulated tone bursts."""
    tmp = path.parent / "_phr"
    tmp.mkdir(exist_ok=True)
    inputs, filters = [], []
    use_say = shutil.which("say") is not None
    for k, (t0, t1, text) in enumerate(PHRASES):
        f = tmp / f"p{k}.wav"
        if use_say:
            aiff = tmp / f"p{k}.aiff"
            subprocess.run(["say", "-o", str(aiff), text], check=True)
            _ff("-i", aiff, "-af", f"atempo=1.0,apad=whole_dur={t1 - t0},atrim=0:{t1 - t0}", "-ar", 48000, "-ac", 1, f)
        else:
            _ff("-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*180*t)*(0.6+0.4*sin(2*PI*4*t))':d={t1 - t0}:s=48000", f)
        inputs += ["-i", f]
        filters.append(f"[{k + 1}:a]adelay={int(t0 * 1000)}|{int(t0 * 1000)},volume=0.9[s{k}]")
    mix = "".join(f"[s{k}]" for k in range(len(PHRASES)))
    _ff("-f", "lavfi", "-i", f"anoisesrc=d={dur}:c=pink:r=48000:a=0.006", *inputs,
        "-filter_complex", ";".join(filters) + f";[0:a]{mix}amix=inputs={len(PHRASES) + 1}:normalize=0,"
        f"atrim=0:{dur},aformat=channel_layouts=stereo[a]", "-map", "[a]", "-c:a", "pcm_s16le", path)
    shutil.rmtree(tmp)


def _transcript(dur):
    segs = []
    for t0, t1, text in PHRASES:
        ws = text.split()
        step = (t1 - t0) / len(ws)
        segs.append({"start": t0, "end": t1, "text": text,
                     "words": [{"start": round(t0 + i * step, 3), "end": round(t0 + (i + 0.85) * step, 3),
                                "word": " " + w, "probability": 0.99} for i, w in enumerate(ws)]})
    return {"language": "en", "model": "hand-written", "segments": segs}


def make_library(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    p = {}
    # talk: dark plate + small moving test pattern, so white pixels in the caption band come from captions
    speech = root / "_speech.wav"
    _speech_track(speech, 12)
    p["talk"] = root / "talk.mp4"
    _ff("-f", "lavfi", "-i", "color=c=0x2a3440:s=1920x1080:r=25:d=12", "-f", "lavfi",
        "-i", "testsrc2=s=480x270:r=25:d=12", "-i", speech,
        "-filter_complex", "[0:v][1:v]overlay=x='200+100*sin(t)':y=200[v]", "-map", "[v]", "-map", "2:a",
        *X264, "-c:a", "aac", "-b:a", "128k", "-shortest", p["talk"])
    speech.unlink()
    looks = {"b1": "testsrc2=s=1920x1080:r=25:d=8,hue=h=60",
             "b2": "testsrc2=s=1920x1080:r=25:d=8,hue=h=200,negate",
             "b3": "smptehdbars=s=1920x1080:r=25:d=8,noise=alls=12:allf=t",
             "b4": "mandelbrot=s=1920x1080:r=25,trim=duration=8"}
    for k, src in looks.items():
        p[k] = root / f"{k}.mp4"
        _ff("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", "sine=f=330:d=8", *X264, "-c:a", "aac", "-shortest", p[k])
    tmp = root / "_v.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=6", *X264, tmp)
    p["vert"] = root / "vert.mov"
    _ff("-display_rotation", "90", "-i", tmp, "-c", "copy", p["vert"])
    tmp.unlink()
    # 120 BPM click song, 40 s, accent every 4th beat
    p["song"] = root / "click120.wav"
    _ff("-f", "lavfi", "-i", "aevalsrc='(0.6+0.4*eq(mod(floor(t*2),4),0))*sin(2*PI*1000*t)*exp(-60*mod(t,0.5))':d=40:s=48000",
        "-f", "lavfi", "-i", "sine=f=110:d=40", "-filter_complex", "[0:a][1:a]amix=inputs=2:weights=1 0.15,aformat=channel_layouts=stereo",
        "-c:a", "pcm_s16le", p["song"])
    return p


@pytest.fixture(scope="module")
def rproj(tmp_path_factory):
    from cutroom import db, ingest
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("render")
    lib = make_library(base / "footage")
    cfg = init_project(base / "work", [base / "footage"])
    cfg.workers = 3
    cfg.out_width, cfg.out_height = 960, 540
    cfg.preview_height = 360
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    ids = {k: db.clip_id(v) for k, v in lib.items() if k != "song"}
    cfg.p("transcripts", f"{ids['talk']}.json").write_text(json.dumps(_transcript(12)))
    plan = {
        "title": "Render Test", "version": 2,
        "style": {"captions": {"position": "bottom"}, "voice": {"clean": True, "atten_lim_db": 20},
                  "loudness": {"I": -14, "TP": -1.5}},
        "sections": [
            {"name": "open", "music": {"song": str(lib["song"]), "offset": 2.0, "gain_db": -6},
             "segments": [
                 {"type": "montage", "beat_sync": True, "beats_per_cut": 2, "min_shot": 0.4,
                  "shots": [{"clip": "b1.mp4", "in": 1.0}, {"clip": "b2.mp4", "in": 0.5}, {"clip": "b3.mp4", "in": 2.0},
                            {"clip": "b4.mp4"}],
                  "overlays": [{"kind": "label", "text": "Day one", "at": 0.2, "dur": 2.0, "position": "top-right"}],
                  "sfx": [{"name": "whoosh", "at": 0.0, "gain_db": -12}]},
                 {"type": "matchcut", "a": {"clip": "b1.mp4", "t": 4.0}, "b": {"clip": "b3.mp4", "t": 3.0,
                                                                           "align": {"scale": 1.1, "dx": 0.02, "dy": 0}},
                  "pre": 0.9, "post": 1.1},
                 {"type": "title", "text": "The Day", "sub": "a test", "dur": 1.6, "bg": "blur:b2.mp4@2.0"},
             ]},
            {"name": "talk",
             "segments": [
                 {"type": "talk", "clip": "talk.mp4", "in": 0.3, "out": 10.8, "auto_cover": True,
                  "cover": [{"clip": "b4.mp4", "in": 1.0}],
                  "cutaways": [{"clip": "b2.mp4", "in": 3.0, "at": 0.4, "dur": 1.0}],
                  "overlays": [{"kind": "callout", "text": "6:40 am", "at": 2.4, "dur": 2.0, "position": "auto"},
                               {"kind": "lower_third", "text": "The workshop", "sub": "Day two", "at": 6.4, "dur": 3.0},
                               {"kind": "title", "text": "Later", "at": 8.4, "dur": 1.2, "position": "center"}]},
                 {"type": "broll", "clip": "b3.mp4", "in": 1.0, "out": 2.0, "speed": 0.5, "audio": "natural",
                  "stabilize": True},
                 {"type": "broll", "clip": "vert.mov", "in": 0.5, "out": 2.5, "fit": "blur", "crop": "face"},
             ]},
            {"name": "end", "music": {"song": str(lib["song"]), "offset": 10.0},
             "segments": [
                 {"type": "still", "clip": "b1.mp4", "t": 5.0, "dur": 1.2, "zoom": 1.1},
                 {"type": "black", "dur": 0.6},
             ]},
        ],
    }
    pp = cfg.p("plans", "test.json")
    pp.write_text(json.dumps(plan, indent=1))
    return {"cfg": cfg, "con": con, "lib": lib, "ids": ids, "plan": pp}


@pytest.fixture(scope="module")
def renders(rproj):
    from cutroom import render
    cfg, con = rproj["cfg"], rproj["con"]
    out = {}
    for prev in (True, False):
        p = render.render_plan(cfg, con, rproj["plan"], preview=prev)
        out[prev] = (Path(p), json.loads(Path(p[:-4] + ".json").read_text()))
    return out


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                        "stream=codec_type,nb_read_frames,duration,width,height:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _gray_frames(path, w=64, h=36):
    r = subprocess.run(FF[:-1] + ["-i", str(path), "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8)
    return a.reshape(-1, h, w).astype(np.float32)


def _audio(path, sr=48000):
    r = subprocess.run(FF[:-1] + ["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32)


@pytest.mark.parametrize("preview", [True, False])
def test_output_and_timing(renders, preview):
    path, side = renders[preview]
    assert path.exists() and path.stat().st_size > 10_000
    info = _probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    fps = side["fps"]
    assert abs(int(v["nb_read_frames"]) - side["frames"]) <= 2
    assert abs(int(v["nb_read_frames"]) / fps - side["duration"]) <= 2 / fps
    assert abs(float(a["duration"]) - float(v["duration"])) <= 2 / fps, (a["duration"], v["duration"])
    assert int(v["height"]) == (360 if preview else 540)
    # loudness hit the programme target
    fin = side["loudness"]["final"]
    assert fin["I"] is not None and abs(fin["I"] + 14) < 0.6, fin
    assert fin["TP"] is not None and fin["TP"] <= -1.4, fin


@pytest.mark.parametrize("preview", [True, False])
def test_sidecar_complete(renders, preview):
    _, side = renders[preview]
    segs = side["segments"]
    assert side["schema"] == "cutroom-render-v1" and side["warnings"] is not None
    assert sum(s["frames"] for s in segs) == side["frames"]
    for a, b in zip(segs, segs[1:]):
        assert abs(a["end"] - b["start"]) < 1e-6
    types = {s["type"] for s in segs}
    assert types == {"talk", "broll", "montage", "matchcut", "title", "black", "still"}
    for s in segs:
        for k in ("start", "end", "frames", "kind", "clip", "in", "out", "captions", "overlays", "cutaways", "cache_key"):
            assert k in s
        if s["kind"] == "clip":
            assert s["clip"] and s["out"] > s["in"]
    talk = [s for s in segs if s["type"] == "talk"]
    assert len(talk) >= 2, "tighten should split the talk segment into pieces"
    assert any(c["auto"] for s in talk for c in s["cutaways"]), "auto_cover should hide the long jump cut"
    assert any(not c["auto"] for s in talk for c in s["cutaways"])
    kinds = {o["kind"] for s in segs for o in s["overlays"]}
    assert kinds == {"callout", "lower_third", "title", "label"}
    callout = next(o for s in segs for o in s["overlays"] if o["kind"] == "callout")
    assert callout["position"] in ("top-left", "top-right")
    # the callout starts before the 2.2 s pause that tighten removed and keeps running across the
    # jump cut: same graphic on consecutive pieces, continuous in time, full requested duration
    co = [o for s in segs for o in s["overlays"] if o["kind"] == "callout"]
    assert len(co) == 2 and abs(co[0]["t1"] - co[1]["t0"]) < 0.05
    assert abs((co[1]["t1"] - co[0]["t0"]) - 2.0) < 0.05
    # matchcut: A ends exactly at a.t, B starts exactly at b.t
    mc = [s for s in segs if s["type"] == "matchcut"]
    assert abs(mc[0]["out"] - 4.0) < 1e-3 and abs(mc[1]["in"] - 3.0) < 1e-3
    assert side["sections"][0]["music"] and side["sections"][1]["music"] is None


def test_montage_cuts_on_beats(renders):
    path, side = renders[True]
    fps = side["fps"]
    sec = side["sections"][0]
    cuts = sec["montage_cuts"][:-1]                 # cuts between shots
    assert len(cuts) == 3
    # beats of the section's song on the timeline (song offset applied)
    from cutroom import music
    info = music.analyze_beats(sec["music"]["song"])
    beats = np.asarray(info["beats"]) - sec["music"]["offset"] + sec["start"]
    for c in cuts:
        assert np.min(np.abs(beats - c)) <= 1.0 / fps + 1e-3, (c, beats[:12])
    # and the picture actually changes there (frame-difference spike within +-1 frame)
    g = _gray_frames(path)
    diff = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))      # diff[k] = change from frame k to k+1
    mont_end = int(round(sec["montage_cuts"][-1] * fps))
    base = np.median(diff[:mont_end])
    for c in cuts:
        f = int(round(c * fps))
        win = diff[max(0, f - 2):f + 1]
        assert win.max() > max(3 * base, 4.0), (c, win, base)


def test_captions_visible(renders):
    path, side = renders[False]
    def clear(s, t):
        return not any(x["t0"] - 0.1 <= t <= x["t1"] + 0.1 for x in s["cutaways"] + s["overlays"])
    cap = next(c for s in side["segments"] if s["type"] == "talk" for c in s["captions"]
               if clear(s, (c["t0"] + c["t1"]) / 2))
    t = (cap["t0"] + cap["t1"]) / 2
    r = subprocess.run(FF[:-1] + ["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                                  "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    H, W = side["height"], side["width"]
    img = np.frombuffer(r.stdout, np.uint8).reshape(H, W, 3)
    band = img[int(H * 0.80):int(H * 0.97)] if cap["position"] == "bottom" else img[:int(H * 0.2)]
    white = (band.min(axis=2) > 225).sum()
    assert white > 150, white          # default caption size is the reference's ~32 px at 1080 (16 px here)
    # no keyline: there must be no ring of near-black pixels hugging the letters
    assert (band.max(axis=2) < 15).sum() < white * 0.2


def test_no_audio_holes_in_talk(renders):
    path, side = renders[False]
    a = _audio(path)
    sr = 48000
    win = int(0.1 * sr)
    for s in side["segments"]:
        if s["type"] != "talk":
            continue
        i0, i1 = int((s["start"] + 0.03) * sr), int((s["end"] - 0.03) * sr)
        x = a[i0:i1]
        k = len(x) // win
        rms = 20 * np.log10(np.sqrt((x[:k * win].reshape(k, win) ** 2).mean(axis=1)) + 1e-12)
        assert rms.min() > -65, (s["start"], rms.min())


def test_cache_reuse(rproj, renders):
    from cutroom import render
    _, side0 = renders[True]
    p = render.render_plan(rproj["cfg"], rproj["con"], rproj["plan"], preview=True)
    side = json.loads(Path(p[:-4] + ".json").read_text())
    assert all(s["cached"] for s in side["segments"])
    assert [s["cache_key"] for s in side["segments"]] == [s["cache_key"] for s in side0["segments"]]


def test_only_section(rproj):
    from cutroom import render
    p = render.render_plan(rproj["cfg"], rproj["con"], rproj["plan"], preview=True, only="end")
    side = json.loads(Path(p[:-4] + ".json").read_text())
    assert p.endswith("_end_preview.mp4") and {s["section"] for s in side["segments"]} == {"end"}


def test_plan_errors_are_loud(rproj):
    from cutroom import render
    from cutroom.plan import PlanError
    bad = rproj["cfg"].p("plans", "bad.json")
    bad.write_text(json.dumps({"sections": [{"name": "x", "segments": [
        {"type": "talk", "clip": "nope.mp4", "in": 0, "out": 2},
        {"type": "broll", "clip": "b1.mp4", "in": 0, "out": 2, "crop": "diagonal"},
        {"type": "montage", "shots": [{"clip": "b1.mp4", "in": 0}], "sfx": [{"name": "boing"}]}]}]}))
    with pytest.raises(PlanError) as e:
        render.render_plan(rproj["cfg"], rproj["con"], bad, preview=True)
    msg = str(e.value)
    assert "nope.mp4" in msg and "crop" in msg and "boing" in msg
