"""Multicam / dual-system audio sync (cutroom/sync.py) and the NLE hand-off (cutroom/nle.py).

Synthetic shoot (tests/multicam_fixture.py): camA and camB hear the same room (different pictures, mic colour and
noise), camB starts 3.37 s after camA; an audio-only recorder rolls first and its clock runs 150 ppm fast. Sync must
recover every offset to within one frame and the drift to within a few ppm. The NLE export must round-trip frame
for frame against render.resolve_timeline at 25 and at 23.976 fps."""
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

import multicam_fixture as MF
from cutroom import db, ingest, nle, sync
from cutroom.config import init_project

FPS = 25.0
FRAME = 1.0 / FPS


@pytest.fixture(scope="module")
def shoot(tmp_path_factory):
    base = tmp_path_factory.mktemp("multicam")
    clips = MF.make_shoot(base / "footage")
    # an unrelated camera rolling at the same time (different room): must NOT join the group
    other = base / "footage" / "C_CAM"
    other.mkdir()
    x = MF.room_sound(seed=99, dur=40.0)
    MF._write_wav(base / "_o.wav", x)
    MF._ff("-f", "lavfi", "-i", "mandelbrot=size=320x180:rate=25", "-i", base / "_o.wav", "-t", "40",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-metadata", "creation_time=2026-05-01T10:00:20Z", other / "C0201.MP4")
    clips["other"] = other / "C0201.MP4"
    song = base / "music" / "song.wav"
    song.parent.mkdir()
    MF._write_wav(song, MF.room_sound(seed=3, dur=40.0))
    cfg = init_project(base / "work", [base / "footage"], [base / "music"])
    cfg.workers, cfg.min_free_disk_gb = 2, 0.0
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    stats = sync.detect(cfg, con, echo=None)
    ids = {k: db.clip_id(v) for k, v in clips.items()}
    return {"cfg": cfg, "con": con, "clips": clips, "ids": ids, "stats": stats, "song": song}


def _group(shoot):
    gs = sync.groups(shoot["con"])
    assert len(gs) == 1, [[m["rel"] for m in g["members"]] for g in gs]
    return gs[0]


# ============================================================================================ sync
def test_sync_recovers_offsets_and_drift(shoot):
    g = _group(shoot)
    by = {m["clip_id"]: m for m in g["members"]}
    ids, truth = shoot["ids"], MF.truth()
    assert set(by) == {ids["camA"], ids["camB"], ids["rec"]}            # the unrelated camera stayed out
    err = {k: abs(by[ids[k]]["offset"] - truth[k]) for k in ("camA", "camB", "rec")}
    print("sync offset error (ms):", {k: round(v * 1000, 3) for k, v in err.items()},
          "drift ppm:", by[ids["rec"]]["drift_ppm"], "truth", round(truth["rec_drift_ppm"], 2))
    assert max(err.values()) < FRAME, err                                 # within one frame (it is ~0.1 ms)
    assert max(err.values()) < 0.002                                      # and in fact well under a millisecond
    assert abs(by[ids["rec"]]["drift_ppm"] - truth["rec_drift_ppm"]) < 10
    assert abs(by[ids["camB"]]["drift_ppm"]) < 2 and by[ids["camA"]]["drift_ppm"] == 0
    assert g["loop_err_ms"] < 1.0                                        # all three pairs agree
    # the recorder ranks first for sound; cameras are the angles
    assert g["audio_sources"][0] == by[ids["rec"]]["angle"]
    assert sorted(g["angles"]) == sorted([by[ids["camA"]]["angle"], by[ids["camB"]]["angle"]])
    # drift matters at the far end of the take: the event at recorder time 65 s is where the model says
    m = by[ids["rec"]]
    t_true = 65.0                                                         # recorder clock seconds
    g_true = (MF.REC_START + t_true / (1 + MF.REC_DRIFT)) - MF.REC_START  # real seconds since the recorder started
    assert abs(sync.to_group(m, t_true) - g_true) < FRAME / 4


def test_sync_rejects_unrelated_and_reports_why(shoot):
    con, ids = shoot["con"], shoot["ids"]
    rows = con.execute("SELECT * FROM sync_pairs WHERE a=? OR b=?", (ids["other"], ids["other"])).fetchall()
    assert rows, "the unrelated camera overlapped in time, so it must have been tried"
    assert all(r["ok"] == 0 for r in rows)
    assert all("low confidence" in (r["why"] or "") or "silent" in (r["why"] or "") for r in rows)
    assert shoot["stats"]["accepted"] == 3


def test_sync_devices_metadata_and_naming(shoot):
    con = shoot["con"]
    devs = {d["code"]: d for d in sync.devices(con)}
    rec = next(d for d in devs.values() if d["kind"] == "recorder")
    assert rec["model"] == "ZOOM H6"                                     # from the BWF originator
    meta = con.execute("SELECT * FROM clip_media WHERE clip_id=?", (shoot["ids"]["rec"],)).fetchone()
    assert meta["start_src"] == "bwf" and meta["tc_seconds"] == pytest.approx(12 * 3600 + 2)
    cam = next(d for d in devs.values() if d["kind"] == "camera" and "a_cam" in d["key"])
    sync.name_device(con, cam["code"], name="Cam A wide")
    g = _group(shoot)
    assert any(m["device_name"] == "Cam A wide" for m in g["members"])
    # cached: a second detect measures nothing new
    st = sync.detect(shoot["cfg"], con, echo=None)
    assert st["measured"] == 0 and st["groups"] == 1


def test_sync_explicit_clips_without_timestamps(shoot, tmp_path):
    """--clips aligns a hand-picked set even when capture times say nothing (here: just camB vs the recorder)."""
    cfg, con, ids = shoot["cfg"], shoot["con"], shoot["ids"]
    rows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM clips WHERE id IN (?,?)", (ids["camB"], ids["rec"]))}
    res = sync.align_pair(cfg, rows[ids["rec"]], rows[ids["camB"]])
    assert res["ok"] and abs(res["lag"] - (MF.CAM_B_START - MF.REC_START)) < 0.002


# ============================================================================================ plans
def _mc_plan(gid, extra=()):
    return {"title": "Multicam", "version": 1, "output": {"width": 640, "height": 360, "fps": 25},
            "style": {"captions": {"enabled": False}, "voice": {"clean": False}, "grade": {"lut": ""}},
            "sections": [{"name": "talk", "segments": [
                {"type": "talk", "multicam": {"group": gid, "angle": "camB"}, "in": 20.0, "out": 24.0, "tighten": False},
                {"type": "talk", "multicam": {"group": gid, "angle": "camA"}, "in": 24.0, "out": 28.0, "tighten": False},
                *extra]}]}


def test_multicam_segment_resolves_to_angle_and_recorder(shoot):
    from cutroom import plan as P
    from cutroom import render as R
    cfg, con, ids = shoot["cfg"], shoot["con"], shoot["ids"]
    g = _group(shoot)
    plan = _mc_plan(g["id"])
    assert P.validate(P.normalize(plan, cfg), con) == []
    res = R.resolve_timeline(cfg, con, plan, preview=False)
    v1 = [p for p in res["pieces"] if p["track"] == "V1"]
    a1 = [p for p in res["pieces"] if p["track"] == "A1"]
    by = {m["clip_id"]: m for m in g["members"]}
    assert v1[0]["clip"] == ids["camB"] and v1[1]["clip"] == ids["camA"]
    assert v1[0]["in"] == pytest.approx(sync.from_group(by[ids["camB"]], 20.0), abs=1e-3)
    assert v1[1]["in"] == pytest.approx(sync.from_group(by[ids["camA"]], 24.0), abs=1e-3)
    assert all(p["clip"] == ids["rec"] for p in a1)                     # the recorder is the sound
    assert a1[0]["in"] == pytest.approx(sync.from_group(by[ids["rec"]], 20.0), abs=2e-3)
    # out of coverage is an error, with the angle named
    bad = _mc_plan(g["id"])
    bad["sections"][0]["segments"][0].update(**{"in": 0.5, "out": 2.0})     # camB starts at group 6.37 s
    errs = P.validate(P.normalize(bad, cfg), con)
    assert errs and "camB" in errs[0] or "cam" in errs[0]
    # switching angle keeps the moment: clip-relative in/out move by the offset difference
    seg = {"type": "talk", "clip": ids["camB"], "in": 10.0, "out": 12.0, "multicam": {"group": g["id"], "angle": "camB"}}
    sw = sync.switch_angle(con, seg, "camA")
    assert sw["clip"] == ids["camA"]
    assert sw["in"] == pytest.approx(10.0 + (by[ids["camB"]]["offset"] - by[ids["camA"]]["offset"]), abs=1e-3)
    assert sync.map_time(con, g["id"], sw["in"], frm="camA", to="camB") == pytest.approx(10.0, abs=1e-3)


def test_multicam_render_uses_recorder_audio_in_sync(shoot):
    """End to end: the rendered sound IS the recorder, lined up with the picture's moment to within a few ms."""
    from cutroom import render as R
    from cutroom.asr import load_audio
    cfg, con, ids = shoot["cfg"], shoot["con"], shoot["ids"]
    g = _group(shoot)
    p = cfg.p("plans", "mc_v1.json")
    p.write_text(json.dumps(_mc_plan(g["id"])))
    out = R.render_plan(cfg, con, p, preview=True)
    y = load_audio(cfg, out, sr=sync.SR)
    rec = next(m for m in g["members"] if m["clip_id"] == ids["rec"])
    xr = load_audio(cfg, shoot["clips"]["rec"], sr=sync.SR)
    errs = []
    for t_tl, g_t in ((1.0, 21.0), (5.0, 25.0), (6.5, 26.5)):
        tr = sync.from_group(rec, g_t)                                    # where the recorder heard that moment
        M = int(0.06 * sync.SR)
        W = int(1.5 * sync.SR)
        xs = y[int(t_tl * sync.SR): int(t_tl * sync.SR) + W].astype(np.float64)
        j0 = int(round(tr * sync.SR)) - M
        ys = xr[j0: j0 + W + 2 * M].astype(np.float64)
        s, sharp = sync.gcc_phat_lag(xs, ys, M)
        errs.append((s - M) / sync.SR)
        assert sharp > 8
    print("render vs recorder (ms):", [round(e * 1000, 2) for e in errs])
    assert max(abs(e) for e in errs) < 0.01                               # 10 ms; a frame is 40


# ============================================================================================ NLE
def _nle_plan(gid, song):
    pl = _mc_plan(gid, extra=[
        {"type": "broll", "clip": "C0001", "in": 30.0, "out": 31.6, "speed": 1.6, "audio": "natural",
         "overlays": [{"kind": "callout", "text": "Main stage", "at": 0.2, "dur": 1.0, "position": "top-left"}]},
        {"type": "still", "clip": "C0101", "t": 12.0, "dur": 1.0},
        {"type": "title", "text": "Thanks", "dur": 1.2}])
    pl["sections"].append({"name": "music", "music": {"song": str(song), "offset": 3.0, "gain_db": -6},
                           "segments": [{"type": "broll", "clip": "C0101", "in": 30.0, "out": 33.0, "audio": "natural"},
                                        {"type": "talk", "multicam": {"group": gid, "angle": "camB"}, "in": 40.0,
                                         "out": 42.0, "tighten": False}]})
    pl["style"]["music_under_speech"] = "duck"
    return pl


def test_nle_export_round_trip_25(shoot, monkeypatch):
    monkeypatch.setenv("CUTROOM_GFX", "pil")
    cfg, con, ids = shoot["cfg"], shoot["con"], shoot["ids"]
    g = _group(shoot)
    p = cfg.p("plans", "nle25_v1.json")
    p.write_text(json.dumps(_nle_plan(g["id"], shoot["song"])))
    man = nle.export(cfg, con, p, fmts=["all"], graphics=True, make_zip=True, echo=lambda *_: None)
    d = Path(man["dir"])
    for f in ("nle25_v1.xml", "nle25_v1.fcpxml", "nle25_v1_V1.edl", "captions.srt", "README_PREMIERE.txt", "nle.json"):
        assert (d / f).exists() or f == "captions.srt", f
    assert Path(man["zip"]).exists()
    assert nle.lint_xmeml(d / "nle25_v1.xml") == [] and nle.lint_fcpxml(d / "nle25_v1.fcpxml") == []
    rep = nle.verify(cfg, con, d)
    print(json.dumps({k: (v["ok"], v["matched"], v["expected"], v["problems"][:3]) for k, v in rep["checks"].items()}))
    assert rep["ok"], rep["checks"]
    assert rep["checks"]["premiere_xml"]["matched"] == rep["checks"]["premiere_xml"]["expected"] >= 8
    # specifics: sequence rate, masters (not proxies), the recorder on a dialogue track, speed, graphics, music keys
    px = nle.parse_xmeml(d / "nle25_v1.xml")
    seq = px["sequences"][0]
    assert (seq["tb"], seq["ntsc"], seq["width"], seq["height"]) == (25, False, 640, 360)
    v1 = seq["video"][0]
    assert v1[0]["file"] == str(shoot["clips"]["camB"]) and v1[0]["start"] == 0
    by = {m["clip_id"]: m for m in g["members"]}
    assert v1[0]["in"] == round(sync.from_group(by[ids["camB"]], 20.0) * 25)
    assert any(c["speed"] == pytest.approx(1.6) for c in v1)
    aud = [c for tr in seq["audio"] for c in tr]
    assert any(c["file"] == str(shoot["clips"]["rec"]) for c in aud)
    assert len(seq["video"][2]) == 2                                     # callout + title card on V3 (ProRes 4444)
    gm = man["graphics_media"]
    assert all(Path(x["path"]).exists() for x in gm)
    pf = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt",
                         "-of", "csv=p=0", gm[0]["path"]], capture_output=True, text=True).stdout
    assert "prores" in pf and "yuva444" in pf
    root = ET.parse(d / "nle25_v1.xml").getroot()
    keys = [k for e in root.iter("effect") if e.findtext("effectid") == "audiolevels" for k in e.iter("keyframe")]
    assert len(keys) >= 4                                                # the music ducks under the multicam talk
    # the multicam source sequence: every member stacked at its offset
    mc = next(s for s in px["sequences"] if s["name"].startswith("MC "))
    starts = sorted(c["start"] for tr in mc["video"] for c in tr)
    assert starts == sorted(round(by[c]["offset"] * 25) for c in (ids["camA"], ids["camB"]))
    srt = (d / "captions.srt").read_text() if (d / "captions.srt").exists() else ""
    assert "-->" in srt or not man["captions"]
    readme = (d / "README_PREMIERE.txt").read_text()
    assert "File > Import" in readme and "Link Media" in readme and "MULTICAM" in readme


@pytest.fixture(scope="module")
def ntsc(tmp_path_factory):
    base = tmp_path_factory.mktemp("ntsc")
    lib = base / "footage"
    lib.mkdir()
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for name, src, rate in (("A001.MOV", "testsrc2", "24000/1001"), ("B001.MOV", "smptehdbars", "24000/1001"),
                            ("C001.MP4", "testsrc2", "25")):
        subprocess.run(ff + ["-f", "lavfi", "-i", f"{src}=size=320x180:rate={rate}:duration=12", "-f", "lavfi",
                             "-i", "anoisesrc=d=12:c=pink:a=0.1", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(lib / name)], check=True)
    cfg = init_project(base / "work", [lib])
    cfg.min_free_disk_gb = 0.0
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    return {"cfg": cfg, "con": con}


def test_nle_export_round_trip_23976(ntsc):
    cfg, con = ntsc["cfg"], ntsc["con"]
    plan = {"title": "NTSC", "version": 2, "output": {"width": 640, "height": 360, "fps": 23.976},
            "style": {"captions": {"enabled": False}, "voice": {"clean": False}},
            "sections": [{"name": "a", "segments": [
                {"type": "broll", "clip": "A001", "in": 1.0, "out": 3.5, "audio": "natural"},
                {"type": "talk", "clip": "B001", "in": 2.0, "out": 6.0, "tighten": False},
                {"type": "broll", "clip": "C001", "in": 1.0, "out": 3.0, "audio": "natural"},     # 25p -> speed conform
                {"type": "black", "dur": 0.5},
                {"type": "broll", "clip": "A001", "in": 7.13, "out": 9.0, "audio": "mix"}]}]}
    p = cfg.p("plans", "ntsc_v2.json")
    p.write_text(json.dumps(plan))
    man = nle.export(cfg, con, p, fmts=["premiere", "fcpxml", "edl", "otio"], graphics=False, echo=lambda *_: None)
    d = Path(man["dir"])
    rep = nle.verify(cfg, con, d)
    print(json.dumps({k: (v["ok"], v["matched"], v["expected"], v["problems"][:3]) for k, v in rep["checks"].items()}))
    assert rep["ok"], rep["checks"]
    px = nle.parse_xmeml(d / "ntsc_v2.xml")
    seq = px["sequences"][0]
    assert (seq["tb"], seq["ntsc"]) == (24, True)
    files = {f["path"]: f for f in px["files"].values()}
    assert files[str(next(Path(cfg.library_roots[0]).glob("A001.MOV")))]["ntsc"] is True
    assert files[str(next(Path(cfg.library_roots[0]).glob("C001.MP4")))]["ntsc"] is False
    conf = [c for c in seq["video"][0] if c["file"].endswith("C001.MP4")][0]
    assert conf["speed"] == pytest.approx(23.976 / 25, abs=1e-3)       # 25p b-roll conformed by speed, as rendered
    root = ET.parse(d / "ntsc_v2.fcpxml").getroot()
    seqfmt = root.find("library/event/project/sequence").get("format")
    assert next(f for f in root.iter("format") if f.get("id") == seqfmt).get("frameDuration") == "1001/24000s"
    # a source in point that is not on the frame grid lands on the nearest frame, nothing drifts
    r = Fraction(24000, 1001)
    a2 = [c for c in seq["video"][0] if c["file"].endswith("A001.MOV")][1]
    assert a2["in"] == nle.frames(7.13, r)


def test_nle_rendered_fx_segments_are_frame_exact(shoot, monkeypatch):
    """With a render, cards / freezes come out as ProRes segments whose frame count equals their V4 span."""
    monkeypatch.setenv("CUTROOM_GFX", "pil")
    from cutroom import render as R
    cfg, con = shoot["cfg"], shoot["con"]
    g = _group(shoot)
    pl = _mc_plan(g["id"], extra=[{"type": "card", "text": "COMING UP", "dur": 0.84, "sfx": None},
                                  {"type": "still", "clip": "C0101", "t": 12.0, "dur": 1.0}])
    p = cfg.p("plans", "fx_v1.json")
    p.write_text(json.dumps(pl))
    r = R.render_plan(cfg, con, p, preview=True)
    man = nle.export(cfg, con, p, fmts=["premiere"], render=r, graphics=False, echo=lambda *_: None)
    assert len(man["fx_media"]) >= 1
    for it in man["fx_media"]:
        n = nle._count_frames(cfg, it["path"])
        assert n == it["end"] - it["start"], (it, n)
    rep = nle.verify(cfg, con, man["dir"])
    assert rep["ok"], rep["checks"]


def test_studio_multicam_op(shoot):
    """The Inspector's angle switch is a plan op: clip, in and out follow the group."""
    from cutroom import studio
    import contextlib
    con, ids = shoot["con"], shoot["ids"]
    g = _group(shoot)

    class Pool:
        @contextlib.contextmanager
        def get(self):
            yield con

    class St:
        pool = Pool()
    plan = {"sections": [{"segments": [{"type": "talk", "clip": ids["camB"], "in": 10.0, "out": 12.0,
                                        "multicam": {"group": g["id"], "angle": "camB"}}]}]}
    studio.op_multicam_angle(St(), plan, None, {"op": "multicam_angle", "seg": [0, 0], "angle": "camA", "audio": "camera"})
    seg = plan["sections"][0]["segments"][0]
    assert seg["clip"] == ids["camA"] and seg["multicam"] == {"group": g["id"], "angle": "camA", "audio": "camera"}
    by = {m["clip_id"]: m for m in g["members"]}
    assert seg["in"] == pytest.approx(10.0 + by[ids["camB"]]["offset"] - by[ids["camA"]]["offset"], abs=1e-3)


def test_studio_routes_open_in_premiere_and_multicam(shoot, monkeypatch):
    """Studio: /api/multicam, group badges on /api/clips and /api/days, and Open in Premiere as a job."""
    import http.client
    import threading
    import time
    from cutroom import studio
    monkeypatch.setenv("CUTROOM_GFX", "pil")
    cfg = shoot["cfg"]
    g = _group(shoot)
    cfg.p("plans", "studio_mc_v1.json").write_text(json.dumps(_mc_plan(g["id"])))
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
        code, mc = req("GET", "/api/multicam")
        assert code == 200 and mc["groups"][0]["id"] == g["id"] and len(mc["groups"][0]["members"]) == 3
        code, rows = req("GET", "/api/clips?limit=50")
        tagged = {r["id"]: r.get("mc") for r in rows["rows"]}
        assert tagged[shoot["ids"]["camA"]]["group"] == g["id"] and tagged[shoot["ids"]["other"]] is None
        code, days = req("GET", "/api/days")
        assert any(d.get("multicam") for d in days)
        assert req("POST", "/api/export_nle", {"formats": ["vhs"], "plan_file": "plans/studio_mc_v1.json"})[0] == 400
        code, j = req("POST", "/api/export_nle", {"plan_file": "plans/studio_mc_v1.json"})
        assert code == 200 and j["job"]
        for _ in range(240):
            jj = req("GET", f"/api/jobs/{j['job']}")[1]
            if jj["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        assert jj["status"] == "done", jj
        assert jj["meta"]["zip"].endswith("_nle.zip") and Path(jj["meta"]["zip"]).exists()
        assert "File > Import" in jj["meta"]["howto"]
        assert any(f["file"].endswith(".xml") for f in jj["meta"]["files"])
    finally:
        st.close()
        srv.shutdown()
        srv.server_close()


def test_sync_timecode_fallback_only_when_jam_synced(shoot):
    """A silent angle can still join by jam-synced time-of-day timecode, but only on devices marked tc_sync."""
    con, ids = shoot["con"], shoot["ids"]
    a, b = sorted((ids["camA"], ids["camB"]))
    try:
        for cid, tc in ((ids["camA"], 36005.0), (ids["camB"], 36008.37)):
            con.execute("UPDATE clip_media SET tc_seconds=? WHERE clip_id=?", (tc, cid))
        res = sync._timecode_fallback(con, a, b, {"ok": 0, "why": "low confidence"})
        assert res["ok"] == 0                                             # not marked: audio decides alone
        con.execute("UPDATE devices SET tc_sync=1 WHERE id IN (SELECT device_id FROM clip_media WHERE clip_id IN (?,?))", (a, b))
        res = sync._timecode_fallback(con, a, b, {"ok": 0, "why": "low confidence"})
        sign = 1 if a == ids["camA"] else -1
        assert res["ok"] == 1 and res["method"] == "timecode" and res["lag"] == pytest.approx(sign * 3.37, abs=1e-6)
    finally:
        con.rollback()
