"""Deliverables (export.py) and scale features (analyze.py detectors, tagging cost control, estimate).
Synthetic footage only: faces are bright boxes found by a stand-in detector, applause is generated noise."""
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from conftest import FF, X264

from cutroom import analyze, db, export, ingest, media
from cutroom.config import Config, init_project


def _ff(*a):
    subprocess.run(FF + [str(x) for x in a], check=True)


def _frame(path, t, w=None):
    """RGB frame of `path` at t seconds (numpy HxWx3)."""
    vi = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    W, H = int(vi[0]), int(vi[1])
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(H, W, 3)


def _white_box_detector(gray):
    """Stand-in face detector: the bright (>235) blob is the face."""
    ys, xs = np.nonzero(gray > 235)
    if len(xs) < 30:
        return []
    h, w = gray.shape
    return [(xs.min() / w, ys.min() / h, (xs.max() - xs.min() + 1) / w, (ys.max() - ys.min() + 1) / h)]


@pytest.fixture
def fast_gov(monkeypatch, tmp_path):
    monkeypatch.setenv("CUTROOM_MIN_FREE_RAM_GB", "0.3")
    return None


def _cfg(tmp_path, roots=()):
    cfg = init_project(tmp_path / "work", list(roots))
    cfg.workers, cfg.max_load, cfg.min_free_disk_gb = 2, 1000.0, 0.0
    cfg.save()
    return cfg


# ================================================================================================ presets

def _talking_video(path, dur=4.0, w=1280, h=720):
    _ff("-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=25:duration={dur}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}:sample_rate=48000",
        "-af", "volume=-26dB", *X264, "-c:a", "aac", "-shortest", path)
    return path


def test_presets_dimensions_duration_loudness(tmp_path, fast_gov):
    cfg = _cfg(tmp_path)
    v = _talking_video(cfg.p("renders", "clip_v1.mp4"))
    m = export.export(cfg, None, v, presets=["youtube", "square"], echo=None)
    outs = {o["name"]: o for o in m["outputs"]}
    assert (outs["youtube"]["width"], outs["youtube"]["height"]) == (1920, 1080)
    assert (outs["square"]["width"], outs["square"]["height"]) == (1080, 1080)
    for o in outs.values():
        assert abs(o["duration"] - 4.0) < 0.15, o
        assert abs(o["fps"] - 25) < 0.01                          # the render's rate is kept (no 25->30 judder)
        assert abs(o["loudness"]["I"] - o["target"]["I"]) < 1.0, o["loudness"]
        assert o["loudness"]["TP"] <= o["target"]["TP"] + 0.6
    assert any("upscaled" in w for w in m["warnings"])            # 720p source -> 1080p master is flagged
    # max duration: skipped with a pointer to cutdowns, or trimmed on request
    m2 = export.export(cfg, None, v, presets=["shorts"], max_dur=2.0, echo=None)
    assert not m2["outputs"] and "cutdown" in m2["warnings"][0]
    m3 = export.export(cfg, None, v, presets=["shorts"], max_dur=2.0, trim=True, echo=None)
    o = m3["outputs"][0]
    assert (o["width"], o["height"]) == (1080, 1920) and abs(o["duration"] - 2.0) < 0.15
    assert export.preset_names("all") == export.ALL
    with pytest.raises(ValueError):
        export.preset_names("vhs")


def test_proxy_settings_and_colour_pipe():
    cfg = Config(work="/tmp/x", lut=__file__, proxy_height=540)      # any existing file stands in for a .cube
    assert media.proxy_bitrate(cfg) == "1200k"
    cfg.proxy_height = 360
    assert media.proxy_bitrate(cfg) == "600k"
    cfg.proxy_bitrate = "900k"
    assert media.proxy_bitrate(cfg) == "900k"
    enc = media.hw_encoder(cfg)
    assert "900k" in enc and "2500k" not in enc
    row = {"is_log": 1, "fps": 25, "id": "x"}
    args = ingest._proxy_args(cfg, row, "in.mov", "out.mp4", enc)
    vf = args[args.index("-vf") + 1]
    assert "in_color_matrix=bt709" in vf and vf.index("in_color_matrix") < vf.index("lut3d")
    assert "colorspace=" in vf and "all=bt709" in vf
    assert args[args.index("-colorspace") + 1] == "bt709"
    assert any("cutroom proxy graded lut=" in str(a) for a in args)
    row["is_log"] = 0
    vf2 = ingest._proxy_args(cfg, row, "in.mov", "out.mp4", enc)
    assert "lut3d" not in " ".join(map(str, vf2))


# ================================================================================================ reframe

def test_reframe_keeps_a_moving_face_inside(tmp_path, fast_gov, monkeypatch):
    monkeypatch.setattr(export, "detect_faces", _white_box_detector)
    cfg = _cfg(tmp_path)
    src = cfg.p("renders", "walk_v1.mp4")
    # a 120x150 "face" crossing the frame left -> right over 6 s on a textured background
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=200:duration=6:sample_rate=48000",
        "-f", "lavfi", "-i", "color=c=white:s=120x150:r=25:d=6",
        "-filter_complex", "[0:v]lutyuv=y='val*0.6'[bg];[bg][2:v]overlay=x='120+t*150':y=250[v]",
        "-map", "[v]", "-map", "1:a", *X264, "-c:a", "aac", "-shortest", src)
    m = export.export(cfg, None, src, presets=["reels"], echo=None)
    o = m["outputs"][0]
    assert (o["width"], o["height"]) == (1080, 1920)
    assert o["reframe"] and all(s["how"] in ("face", "fit") for s in o["reframe"])
    scale = 1920 / 720
    for t in (0.3, 1.5, 3.0, 4.5, 5.7):
        fr = _frame(o["file"], t)
        mask = fr.min(axis=2) > 230
        cols = np.nonzero(mask.any(axis=0))[0]
        assert len(cols), f"face lost at {t}s"
        assert cols.min() > 4 and cols.max() < 1080 - 5, (t, cols.min(), cols.max())
        width = cols.max() - cols.min() + 1
        assert abs(width - 120 * scale) < 0.12 * 120 * scale, (t, width)        # the whole face, not a slice


def test_reframe_static_shot_holds_still_and_fit_for_titles():
    times = [i * 0.2 for i in range(30)]
    face = (0.6, 0.3, 0.08, 0.2)
    faces = [[face] for _ in times]
    sal = [(0.5, 0.0)] * len(times)
    shots = [{"t0": 0.0, "t1": 4.0, "mode": "track"}, {"t0": 4.0, "t1": 6.0, "mode": "fit"}]
    out = export.plan_crops(times, faces, sal, shots, cw=0.32)
    xs = [x for _, x in out[0]["path"]]
    assert max(xs) - min(xs) < 1e-6 and out[0]["how"] == "face"          # a still subject: a locked crop
    assert abs(xs[0] - (0.6 + 0.04)) < 0.02
    assert out[1]["how"] == "fit"
    # no face, flat picture: centre
    out2 = export.plan_crops(times, [[] for _ in times], sal, shots[:1], cw=0.32)
    assert out2[0]["how"] == "centre" and out2[0]["path"][0][1] == 0.5


# ================================================================================================ captions / chapters

SIDE = {"schema": "cutroom-render-v1", "sections": [
    {"name": "cold open", "start": 0.0, "end": 12.0}, {"name": "villa", "start": 12.0, "end": 40.5},
    {"name": "evening", "start": 40.5, "end": 75.0}],
    "segments": [{"start": 12.0, "end": 14.0, "kind": "clip", "captions": [
        {"t0": 12.08, "t1": 12.9, "text": "yeah, living here"}, {"t0": 12.92, "t1": 13.96, "text": "is insane."}]},
        {"start": 14.0, "end": 16.0, "kind": "clip", "captions": [{"t0": 3723.5, "t1": 3724.25, "text": "late line"}]}]}


def test_srt_vtt_timing_matches_sidecar(tmp_path):
    cues = export.caption_cues(None, None, SIDE)
    srt = export.write_srt(cues, tmp_path / "a.srt")
    vtt = export.write_vtt(cues, tmp_path / "a.vtt")
    txt = Path(srt).read_text()
    stamps = re.findall(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)", txt)
    got = [(int(a) * 3600 + int(b) * 60 + int(c) + int(d) / 1000, int(e) * 3600 + int(f) * 60 + int(g) + int(h) / 1000)
           for a, b, c, d, e, f, g, h in stamps]
    want = [(c["t0"], c["t1"]) for s in SIDE["segments"] for c in s["captions"]]
    assert len(got) == len(want) and all(abs(x - y) < 0.0011 for g_, w_ in zip(got, want) for x, y in zip(g_, w_))
    assert "is insane." in txt and txt.startswith("1\n")
    v = Path(vtt).read_text()
    assert v.startswith("WEBVTT") and "01:02:03.500 --> 01:02:04.250" in v
    text, warn = export.chapters(SIDE)
    assert text.splitlines() == ["0:00 Cold open", "0:12 Villa", "0:40 Evening"] and not warn
    _, warn2 = export.chapters({"sections": SIDE["sections"][:2]})
    assert any("3" in w for w in warn2)


# ================================================================================================ thumbnails

def _tex(seed, blur=False):
    rng = np.random.default_rng(seed)
    img = (rng.random((270, 480, 3)) * 255).astype(np.uint8)
    img = (img.astype(np.float32) * 0.5 + 64).astype(np.uint8)
    if blur:
        import cv2
        img = cv2.GaussianBlur(img, (0, 0), 6)
    return img


def test_thumbnail_scoring_prefers_sharp_face_frames():
    face = [(0.55, 0.2, 0.18, 0.32)]                     # 32 % of the height, near the right third
    sharp_face = export.score_frame(_tex(1), faces=face)
    blur_face = export.score_frame(_tex(1, blur=True), faces=face)
    sharp_none = export.score_frame(_tex(2), faces=[])
    assert sharp_face["total"] > blur_face["total"] and sharp_face["total"] > sharp_none["total"]
    assert sharp_face["sharp"] > blur_face["sharp"] + 0.2
    scored = [(1.0, blur_face), (3.0, sharp_none), (5.0, sharp_face), (5.5, sharp_face)]
    picks = export.pick_thumbnails(scored, n=2, min_gap=2.0)
    assert picks[0][0] == 5.0 and all(abs(a[0] - b[0]) >= 2.0 for a in picks for b in picks if a is not b)


def test_thumbnails_end_to_end(tmp_path, fast_gov, monkeypatch):
    monkeypatch.setattr(export, "detect_faces", _white_box_detector)
    cfg = _cfg(tmp_path)
    src = cfg.p("renders", "thumbs_v1.mp4")
    # 0-4 s: soft picture with a face; 4-8 s: sharp picture with a face (the winners must come from here)
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=8",
        "-vf", "drawbox=x=700:y=200:w=200:h=240:color=white:t=fill,gblur=sigma=8:enable='lt(t,4)'",
        *X264, src)
    out = export.thumbnails(cfg, None, src, None, tmp_path / "thumbs", n=3, echo=None)
    assert len(out) == 3 and (tmp_path / "thumbs" / "sheet.jpg").exists()
    assert out[0]["t"] >= 4.0 and out[0]["faces"] == 1
    assert all(Path(o["file"]).stat().st_size > 1000 for o in out)


# ================================================================================================ cutdowns

LINES = [(0.2, 1.8, "we drove three hundred miles today."), (2.2, 3.6, "the view was insane."),
         (4.0, 5.6, "then the car broke down."), (6.0, 7.6, "best day ever honestly.")]


def _write_transcript(cfg, con, cid, lines):
    segs = []
    for t0, t1, text in lines:
        ws = text.split()
        step = (t1 - t0) / len(ws)
        segs.append({"start": t0, "end": t1, "text": text,
                     "words": [{"start": round(t0 + i * step, 3), "end": round(t0 + (i + 0.85) * step, 3),
                                "word": " " + w, "probability": 0.99} for i, w in enumerate(ws)]})
    cfg.p("transcripts", f"{cid}.json").write_text(json.dumps({"language": "en", "model": "hand", "segments": segs}))
    db.upsert(con, "transcripts", {"clip_id": cid, "path": "", "language": "en", "n_words": 20, "speech_s": 6.0,
                                   "text": " ".join(x[2] for x in lines), "model": "hand", "updated": time.time()},
              key=("clip_id",))
    con.commit()


@pytest.fixture
def copied_project(project, tmp_path):
    """The session project's library.db in a private work dir (proxies stay where they are, read-only)."""
    src = project["cfg"]
    cfg = Config(**{**src.__dict__})
    cfg.work = str(tmp_path / "work")
    for d in ("plans", "renders", "transcripts", "cache", "overnight", "proxies", "sheets"):
        cfg.p(d).mkdir(parents=True, exist_ok=True)
    s = db.connect(src.db_path)
    d = db.connect(cfg.db_path)
    s.backup(d)
    s.close()
    cfg.save()
    return cfg, d, project["ids"]


def test_cutdown_plan_is_valid_and_fits(copied_project):
    from cutroom import plan as P, render
    cfg, con, ids = copied_project
    _write_transcript(cfg, con, ids["steady"], LINES)
    _write_transcript(cfg, con, ids["vertical"], [(0.2, 1.6, "first time here."), (2.0, 3.6, "and that is the end.")])
    plan = {"title": "Road trip", "version": 2, "style": {"captions": {"enabled": True}}, "sections": [
        {"name": "cold open", "segments": [
            {"type": "montage", "beat_sync": False, "max_shot": 1.0, "shots": [
                {"clip": ids["scenes"], "in": 0.5}, {"clip": ids["shaky"], "in": 1.0}, {"clip": ids["dark"], "in": 1.0},
                {"clip": ids["scenes"], "in": 4.0}, {"clip": ids["steady"], "in": 2.0}]},
            {"type": "title", "text": "Road trip", "dur": 2.0}]},
        {"name": "day", "segments": [{"type": "talk", "clip": ids["steady"], "in": 0.0, "out": 8.0}]},
        {"name": "end", "segments": [{"type": "talk", "clip": ids["vertical"], "in": 0.0, "out": 4.0},
                                     {"type": "black", "dur": 1.0}]}]}
    pp = cfg.p("plans", "roadtrip_v2.json")
    pp.write_text(json.dumps(plan))
    full = render.resolve_timeline(cfg, con, plan)["duration"]
    for L in (10, 15):
        out = export.build_cutdown(cfg, con, pp, L, echo=None)
        assert out.name == f"roadtrip_cut{L}.json"
        cut = json.loads(out.read_text())
        assert P.validate(P.normalize(cut, cfg), con) == []
        dur = render.resolve_timeline(cfg, con, cut)["duration"]
        assert abs(dur - L) <= 0.1 * L, (L, dur, full)
        segs = [s for sec in cut["sections"] for s in sec["segments"]]
        assert segs[0]["type"] == "montage"                                    # keeps the hook
        talks = [s for s in segs if s["type"] == "talk"]
        assert talks[-1]["clip"] == ids["vertical"] and "end" in talks[-1]["note"]     # ends on the payoff
        assert cut["cutdown"]["from"] == "roadtrip_v2.json"
        # story order: picked lines appear in source order
        keyed = [(0 if s["clip"] == ids["steady"] else 1, s["in"]) for s in talks]
        assert keyed == sorted(keyed)


# ================================================================================================ dedupe

def test_dedupe_groups_identical_and_near_identical(tmp_path, fast_gov):
    lib = tmp_path / "lib"
    (lib / "a").mkdir(parents=True)
    (lib / "b").mkdir()
    base = lib / "a" / "orig.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=960x540:rate=25:duration=5", *X264, base)
    shutil.copyfile(base, lib / "b" / "copy.mp4")                               # byte-identical copy
    _ff("-i", base, "-vf", "eq=brightness=0.03,noise=alls=4:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "30", "-pix_fmt", "yuv420p", lib / "a" / "reencoded.mp4")       # same shot, re-encoded
    _ff("-f", "lavfi", "-i", "mandelbrot=size=960x540:rate=25", "-t", "5", *X264, lib / "a" / "other.mp4")
    cfg = _cfg(tmp_path, [lib])
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    ingest.make_sheets(cfg, con)
    analyze.analyze_all(cfg, con)
    ids = {p.stem: db.clip_id(p) for p in (lib / "a").glob("*.mp4")} | {"copy": db.clip_id(lib / "b" / "copy.mp4")}
    assert con.execute("SELECT COUNT(*) FROM phash").fetchone()[0] == 4          # hashes come free with analysis
    res = analyze.dedupe(cfg, con, echo=None)
    assert res["groups"] == 1 and res["redundant"] == 2
    g = analyze.dupe_info(con, ids["orig"])
    assert set(g["members"]) == {ids["orig"], ids["copy"], ids["reencoded"]}
    assert analyze.dupe_info(con, ids["other"]) is None
    assert con.execute("SELECT kind FROM dupes WHERE clip_id=?", (ids["copy"],)).fetchone()[0] == "exact"
    # search collapses the group; --near ranks the look-alikes first
    from cutroom import tags

    class A:
        text, limit, json = "", 30, True
        dedupe, highlights, session, near = True, False, None, None
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        rows = tags.search(cfg, con, A)
    got = {r["id"] for r in rows}
    assert ids["other"] in got and len(got & set(g["members"])) == 1
    A.dedupe, A.near = False, ids["orig"]
    with contextlib.redirect_stdout(io.StringIO()):
        near = tags.search(cfg, con, A)
    assert {r["id"] for r in near[:2]} == {ids["copy"], ids["reencoded"]}
    q = tags.query(cfg, con, dedupe=True)
    assert q["total"] == 2
    # tagging cost control: duplicates inherit their representative's tags instead of an AI call
    from cutroom import overnight
    pipe = overnight.Pipeline(cfg, {"tag": False}, echo=False)
    plan = {}
    cands = pipe.tag_candidates(con, plan)
    assert {r["id"] for r in cands} == {g["rep"], ids["other"]} and plan["inherit"] == 2
    db.set_tags(con, g["rep"], {"summary": "test pattern", "quality": 4, "best": [{"t": 1.0, "why": "x"}]})
    assert pipe.inherit_tags(con) == 2
    for m in g["members"]:
        if m != g["rep"]:
            t = db.get_tags(con, m)
            assert t["inherited_from"] == g["rep"] and t["summary"] == "test pattern"


def test_tag_gate_skips_short_black_and_reports_plan(tmp_path):
    cfg = _cfg(tmp_path)
    con = db.connect(cfg.db_path)
    analyze.ensure_scale_schema(con)
    for cid, dur in (("short01", 0.4), ("black01", 6.0), ("good01", 6.0)):
        sheet = cfg.p("sheets", f"{cid}.jpg")
        sheet.write_bytes(b"\xff\xd8\xff\xe0fake")
        db.upsert(con, "clips", {"id": cid, "path": f"/f/{cid}.mp4", "rel": f"{cid}.mp4", "duration": dur,
                                 "status": "analyzed", "sheet": str(sheet), "has_audio": 0, "updated": time.time()})
        luma = 2.0 if cid == "black01" else 120.0
        con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?)", (cid, 0, 2, 1.0, 1.0, luma, 10, 100, 0))
    con.commit()
    from cutroom import overnight
    pipe = overnight.Pipeline(cfg, {"tag": False}, echo=False)
    plan = {}
    cands = pipe.tag_candidates(con, plan)
    assert [r["id"] for r in cands] == ["good01"]
    assert plan["gated"] == 2 and set(plan["gate_reasons"]) == {"too short", "black / lens cap"}
    line = pipe.tag_plan_line(plan, 8)
    assert "1 to send" in line and "about 1 call" in line
    cfg.tag_gate = False
    assert len(pipe.tag_candidates(con)) == 3


# ================================================================================================ highlights

def _wav(path, sig, sr=48000):
    pcm = (np.clip(sig, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def test_highlight_score_peaks_on_applause_burst(tmp_path, fast_gov):
    sr, dur = 48000, 14.0
    rng = np.random.default_rng(7)
    t = np.arange(int(sr * dur)) / sr
    sig = 0.004 * rng.standard_normal(len(t))                              # room tone
    sig += 0.03 * np.sin(2 * np.pi * 180 * t) * (np.sin(2 * np.pi * 3 * t) > 0)   # someone talking-ish
    a, b = int(8.0 * sr), int(10.0 * sr)                                    # applause: 8-10 s
    n = b - a
    claps = np.zeros(n)
    for c in rng.integers(0, n - 2000, 60):
        claps[c:c + 1500] += rng.standard_normal(1500) * np.exp(-np.arange(1500) / 200)
    sig[a:b] += 0.35 * claps + 0.08 * rng.standard_normal(n)
    lib = tmp_path / "lib"
    lib.mkdir()
    _wav(tmp_path / "a.wav", sig, sr)
    _ff("-f", "lavfi", "-i", f"testsrc2=size=960x540:rate=25:duration={dur}", "-i", tmp_path / "a.wav",
        *X264, "-c:a", "aac", "-b:a", "128k", "-shortest", lib / "show.mp4")
    cfg = _cfg(tmp_path, [lib])
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    analyze.analyze_all(cfg, con)
    row = con.execute("SELECT * FROM clips").fetchone()
    wins = analyze.highlights_clip(cfg, con, row)
    best = max(wins, key=lambda w: w["score"])
    assert best["t0"] == 8.0, [(w["t0"], w["score"]) for w in wins]
    others = [w["score"] for w in wins if w["t0"] != 8.0]
    assert best["score"] > max(others) + 0.1
    assert best["kind"] == "applause/crowd"
    peak = con.execute("SELECT * FROM highlight_peaks").fetchone()
    assert peak["t"] == 8.0
    from cutroom import tags
    q = tags.query(cfg, con, highlights=True)
    assert q["total"] == 1 and q["rows"][0]["highlight"]["t"] == 8.0


# ================================================================================================ sessions / takes

def test_sessions_places_and_best_takes(copied_project):
    cfg, con, ids = copied_project
    analyze.ensure_scale_schema(con)
    # three clips shot 10 min apart at one place, one clip 3 h later somewhere else
    stamp = {ids["steady"]: "2026-07-10T08:00:00Z", ids["shaky"]: "2026-07-10T08:10:00Z",
             ids["dark"]: "2026-07-10T08:20:00Z", ids["scenes"]: "2026-07-10T11:30:00Z", ids["vertical"]: None}
    for cid, s in stamp.items():
        con.execute("UPDATE clips SET shot_at=?, updated=? WHERE id=?", (s, time.time(), cid))
    con.commit()
    geo = {ids["steady"]: (-8.65, 115.13), ids["shaky"]: (-8.6505, 115.1302), ids["scenes"]: (-8.40, 115.20)}
    for cid, (la, lo) in geo.items():
        con.execute("INSERT OR REPLACE INTO clip_geo VALUES (?,?,?,?,?,?,?,?)",
                    (cid, la, lo, None, None, None, "tags", time.time() + 10))
    con.commit()
    res = analyze.sessions(cfg, con, echo=None, probe=False)
    s = {r["clip_id"]: (r["session"], r["place"]) for r in con.execute("SELECT * FROM sessions")}
    assert s[ids["steady"]][0] == s[ids["shaky"]][0] == s[ids["dark"]][0] != s[ids["scenes"]][0]
    assert s[ids["steady"]][1] == s[ids["shaky"]][1] and s[ids["scenes"]][1] != s[ids["steady"]][1]
    assert res["places"] == 2
    days = __import__("cutroom.tags", fromlist=["days"]).days(con)
    assert any(len(d["sessions"]) == 2 for d in days)
    # a line said twice inside a shoot: the later, complete take wins
    _write_transcript(cfg, con, ids["steady"], [(0.2, 1.8, "this is the best beach on the island"),
                                                (3.0, 4.6, "this is the best beach on the whole island")])
    _write_transcript(cfg, con, ids["shaky"], [(1.0, 2.0, "okay so this is the best beach on the island")])
    analyze.takes(cfg, con, echo=None)
    rows = con.execute("SELECT * FROM takes ORDER BY rank").fetchall()
    assert len({r["grp"] for r in rows}) == 1 and len(rows) == 3
    assert rows[0]["clip_id"] == ids["shaky"] and rows[0]["best"] == 1     # the last take of the shoot


# ================================================================================================ estimate

def test_estimate_is_sane(copied_project, tmp_path):
    from cutroom import estimate
    cfg, con, ids = copied_project
    (cfg.p("overnight") / "rates.json").write_text(json.dumps({
        "proxies": [{"x_realtime": 1.6, "model": None}], "analyze": [{"x_realtime": 12.0}],
        "transcribe": [{"x_realtime": 2.0, "model": "medium"}]}))
    res = estimate.estimate(cfg, con, tb=4)
    lib, proj = res["this_library"], res["projection"]
    assert res["facts"]["clips"] == 5 and lib["hours"] > 0
    assert proj["hours"] > 1000 * lib["hours"]
    st = {r[0]: r for r in proj["stages"]}
    assert abs(st["proxies"][2] - proj["hours"] / 1.6) < 1e-6 and "measured" in st["proxies"][3]
    assert all(r[2] >= 0 for r in proj["stages"])
    d = proj["disk_gb"]
    assert d["proxies"] > 0 and d["sheets"] > 0 and d["database"] > 0
    lines = []
    estimate.print_estimate(res, echo=lines.append)
    out = "\n".join(lines)
    assert "projection for 4 TB" in out and "AI tagging" in out and "disk:" in out
    # nothing measured: everything is labelled as assumed
    (cfg.p("overnight") / "rates.json").unlink()
    res2 = estimate.estimate(cfg, con, hours=100)
    assert all("assumed" in r[3] or "VAD" in r[3] for r in res2["projection"]["stages"] if r[0] != "sheets")


def test_geo_parsing():
    assert media.parse_iso6709("+37.7749-122.4194+010.000/") == (37.7749, -122.4194, 10.0)
    assert media.parse_iso6709("garbage") is None
    xml = ('<Item name="Latitude" value="35:39.5"/><Item name="LatitudeRef" value="N"/>'
           '<Item name="Longitude" value="139:44.0"/><Item name="LongitudeRef" value="E"/>')
    la, lo, _ = media.geo_from_xml(xml)
    assert abs(la - 35.6583) < 1e-3 and abs(lo - 139.7333) < 1e-3
    assert media.geo_from_probe({"format": {"tags": {"com.apple.quicktime.location.ISO6709": "-33.8688+151.2093/"}}})[:2] \
        == (-33.8688, 151.2093)


# ================================================================================================ studio route

def test_studio_export_route_and_clip_params(tmp_path):
    import http.client
    import threading
    from cutroom import studio
    cfg = _cfg(tmp_path)
    srv, st = studio.make_server(cfg, 0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    port = srv.server_address[1]

    def req(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"null")
    try:
        assert req("POST", "/api/export", {"video": "renders/nope.mp4", "preset": "reels"})[0] == 404
        _talking_video(cfg.p("renders", "x_v1.mp4"), dur=1.0, w=320, h=180)
        assert req("POST", "/api/export", {"video": "renders/x_v1.mp4", "preset": "vhs"})[0] == 400
        code, j = req("POST", "/api/export", {"video": "renders/x_v1.mp4", "preset": ["square"]})
        assert code == 200 and j["job"]
        for _ in range(240):
            jj = req("GET", f"/api/jobs/{j['job']}")[1]
            if jj["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        assert jj["status"] == "done", jj
        assert any(f["file"].endswith("x_v1_square.mp4") for f in jj["meta"]["files"])
        code, r = req("GET", "/api/clips?highlights=1&dedupe=1&session=3")
        assert code == 200 and r["total"] == 0
    finally:
        st.close()
        srv.shutdown()
        srv.server_close()


# ================================================================================================ remote ASR

def test_asr_remote_command_template(tmp_path):
    from cutroom import asr
    fake = tmp_path / "fake_asr.py"
    fake.write_text(
        "import json, sys, wave\n"
        "audio, out, model = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "n = wave.open(audio).getnframes()\n"
        "json.dump({'language': 'en', 'segments': [{'start': 0.0, 'end': n / 16000, 'text': model,\n"
        "           'words': [{'start': 0.0, 'end': 0.5, 'word': ' ' + model, 'probability': 1.0}]}]}, open(out, 'w'))\n")
    cfg = Config(work=str(tmp_path), asr_remote=f"{sys.executable} {fake} {{audio}} {{out}} {{model}}")
    lang, segs = asr._run_model(cfg, np.zeros(16000 * 2, np.float32), "small")
    assert lang == "en" and segs[0]["text"] == "small" and abs(segs[0]["end"] - 2.0) < 1e-6
    assert not list((tmp_path / "transcripts" / "remote").glob("*"))           # temp WAV/JSON cleaned up
    cfg.asr_remote = f"{sys.executable} -c 'import sys; sys.exit(3)' {{audio}}"
    with pytest.raises(RuntimeError):
        asr._run_model(cfg, np.zeros(1600, np.float32), "small")
