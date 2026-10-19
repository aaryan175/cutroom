"""QA, review board and match-cut miner on synthetic ffmpeg lavfi fixtures (no footage needed)."""
import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cutroom import board, db, matchcuts, qa  # noqa: E402
from cutroom.config import Config, init_project  # noqa: E402

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
SZ = "640x360"


def _ff(*args):
    subprocess.run(FF + [str(a) for a in args], check=True)


@pytest.fixture(scope="module")
def defect_video(tmp_path_factory):
    """12.04 s: moving 0-3 | black 3-4 | frozen bars 4-6 | moving 6-8 | 1 white frame at 8.0 |
    moving 8.04-12.04; audio tone with digital silence 9.5-10.5."""
    d = tmp_path_factory.mktemp("qa")
    out = d / "defects_v2.mp4"
    _ff("-f", "lavfi", "-i", f"testsrc2=size={SZ}:rate=25:duration=3",
        "-f", "lavfi", "-i", f"color=black:size={SZ}:rate=25:duration=1",
        "-f", "lavfi", "-i", f"smptebars=size={SZ}:rate=25:duration=2",
        "-f", "lavfi", "-i", f"mandelbrot=size={SZ}:rate=25",
        "-f", "lavfi", "-i", f"color=white:size={SZ}:rate=25:duration=0.04",
        "-f", "lavfi", "-i", f"testsrc2=size={SZ}:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=12.04",
        "-filter_complex",
        "[3:v]trim=duration=2,setpts=PTS-STARTPTS[m];[5:v]hue=h=120,setpts=PTS-STARTPTS[t2];"
        "[0:v][1:v][2:v][m][4:v][t2]concat=n=6:v=1:a=0,format=yuv420p[v];"
        "[6:a]volume=0.3,volume=enable='between(t,9.5,10.5)':volume=0[a]",
        "-map", "[v]", "-map", "[a]", *X264, "-c:a", "aac", "-b:a", "96k", out)
    return out


def _hit(check, t, tol, sev=("fail",)):
    return any(i["severity"] in sev and i["t"] is not None and abs(i["t"] - t) <= tol for i in check["issues"])


def test_qa_flags_each_defect(defect_video, tmp_path):
    cfg = Config(work=str(tmp_path))
    rep = qa.run(cfg, defect_video, out_dir=tmp_path / "out")
    c = {x["id"]: x for x in rep["checks"]}
    assert c["black"]["status"] == "fail" and _hit(c["black"], 3.0, 0.05)
    assert c["frozen"]["status"] == "fail" and _hit(c["frozen"], 4.0, 0.1)
    assert c["silence_holes"]["status"] == "fail" and _hit(c["silence_holes"], 9.6, 0.4)
    assert c["flash_frames"]["status"] == "fail" and _hit(c["flash_frames"], 8.0, 0.045)
    assert c["av_length"]["status"] == "pass"
    assert c["loudness"]["value"]["integrated_lufs"] < -20          # quiet tone -> loudness fail
    s = rep["summary"]
    assert s["status"] == "fail" and s["fail"] >= 4
    assert s["sheets"] and all(Path(p).exists() for p in s["sheets"])
    assert Path(s["report"]).exists() and Path(s["html"]).exists()
    assert json.loads(Path(s["report"]).read_text())["summary"]["fail"] == s["fail"]
    # evidence frames are written for the picture issues
    assert c["black"]["issues"][0]["evidence"] and Path(c["black"]["issues"][0]["evidence"][0]).exists()
    # shots are detected at the real cuts
    starts = [round(x["start"], 2) for x in rep["shots"]]
    for t in (3.0, 4.0, 6.0, 8.0, 8.04):
        assert any(abs(t - x) < 0.045 for x in starts), (t, starts)


def test_qa_uses_sidecar(defect_video, tmp_path):
    """With the render sidecar: intentional black/still pass, planned cuts are matched exactly, a
    reused source window fails, and the flash stays a fail."""
    v = tmp_path / "side_v3.mp4"
    shutil.copy(defect_video, v)
    side = {"version": 3, "fps": 25, "width": 640, "height": 360, "segments": [
        {"start": 0.0, "end": 3.0, "kind": "broll", "clip": "c1", "in": 10.0, "out": 13.0, "section": "a"},
        {"start": 3.0, "end": 4.0, "kind": "black"},
        {"start": 4.0, "end": 6.0, "kind": "still", "clip": "c2", "in": 1.0, "out": 1.0},
        {"start": 6.0, "end": 8.0, "kind": "montage", "clip": "c3", "in": 0.0, "out": 2.0, "section": "a"},
        {"start": 8.0, "end": 8.04, "kind": "montage", "clip": "c4", "in": 5.0, "out": 5.04, "section": "a"},
        {"start": 8.04, "end": 12.04, "kind": "broll", "clip": "c1", "in": 11.0, "out": 15.0, "section": "a"}],
        "captions": [{"start": 0.5, "end": 2.0, "text": "hello there"}]}
    v.with_suffix(".json").write_text(json.dumps(side))
    rep = qa.run(Config(work=str(tmp_path)), v, out_dir=tmp_path / "out")
    c = {x["id"]: x for x in rep["checks"]}
    assert rep["sidecar"]
    assert c["black"]["status"] == "pass"
    assert c["frozen"]["status"] == "pass"
    assert c["cut_timing"]["value"]["planned"] == 5 and c["cut_timing"]["value"]["exact"] == 5
    assert c["flash_frames"]["status"] == "fail"
    assert any("same source window" in i["msg"] for i in c["repeats"]["issues"])
    assert c["text_vs_faces"]["status"] in ("pass", "fail") and c["text_vs_faces"]["value"]["items"] == 1


def test_board_range_notes(defect_video, tmp_path, capsys):
    cfg = init_project(tmp_path / "work", [tmp_path])
    dst = cfg.p("renders", "demo_v2.mp4")
    shutil.copy(defect_video, dst)
    (cfg.p("renders") / "reference").mkdir()
    shutil.copy(defect_video, cfg.p("renders", "reference", "ref.mp4"))
    size = dst.stat().st_size
    data = dst.read_bytes()
    srv = board.make_server(cfg, 0, "127.0.0.1")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + "/r/demo_v2.mp4", headers={"Range": "bytes=100-199"}))
        assert r.status == 206
        assert r.headers["Content-Range"] == f"bytes 100-199/{size}"
        assert r.headers["Accept-Ranges"] == "bytes"
        assert r.read() == data[100:200]
        r = urllib.request.urlopen(urllib.request.Request(base + "/r/demo_v2.mp4/", headers={"Range": "bytes=-10"}))
        assert r.status == 206 and r.read() == data[-10:]                 # suffix range + trailing slash
        r = urllib.request.urlopen(base + "/r/demo_v2.mp4")
        assert r.status == 200 and int(r.headers["Content-Length"]) == size
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/r/demo_v2.mp4", headers={"Range": f"bytes={size + 5}-"}))
        assert e.value.code == 416
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/r/../cutroom.json")
        assert e.value.code == 404
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "demo_v2.mp4" in page and "<video" in page and "reference/ref.mp4" in page
        cmp_page = urllib.request.urlopen(base + "/compare?a=demo_v2.mp4&b=reference/ref.mp4").read().decode()
        assert cmp_page.count("<video") == 2
        body = json.dumps({"render": "demo_v2.mp4", "t": 12.34, "text": "swap this shot"}).encode()
        r = urllib.request.urlopen(urllib.request.Request(base + "/note", data=body, method="POST",
                                                          headers={"Content-Type": "application/json"}))
        note = json.loads(r.read())["note"]
        assert note["version"] == 2 and note["t"] == 12.34
        notes = json.loads(urllib.request.urlopen(base + "/api/notes?render=demo_v2.mp4").read())["notes"]
        assert len(notes) == 1
    finally:
        srv.shutdown()
        srv.server_close()
    board.add_note(cfg, "demo_v3.mp4", 3.0, "tighter intro")
    capsys.readouterr()
    board.print_notes(cfg)
    out = capsys.readouterr().out
    assert "[00:12.3] swap this shot" in out and "[00:03.0] tighter intro" in out
    assert out.index("demo_v3.mp4") < out.index("demo_v2.mp4")          # newest version first
    board.print_notes(cfg, since=3)
    out = capsys.readouterr().out
    assert "tighter intro" in out and "swap this shot" not in out


@pytest.fixture(scope="module")
def mc_project(tmp_path_factory):
    base = tmp_path_factory.mktemp("mc")
    lib = base / "lib"
    lib.mkdir()
    clips = {"day": lib / "day.mp4", "night": lib / "night.mp4", "other": lib / "other.mp4"}
    _ff("-f", "lavfi", "-i", f"testsrc2=size={SZ}:rate=25:duration=6", *X264, clips["day"])
    # same pattern, different colours, much darker: the "night" take of the same set-up
    _ff("-f", "lavfi", "-i", f"testsrc2=size={SZ}:rate=25:duration=6",
        "-vf", "hue=h=150:s=0.7,lutyuv=y=val*0.3", *X264, clips["night"])
    _ff("-f", "lavfi", "-i", f"mandelbrot=size={SZ}:rate=25", "-t", "6", *X264, clips["other"])
    cfg = init_project(base / "work", [lib])
    con = db.connect(cfg.db_path)
    for k, p in clips.items():
        db.upsert(con, "clips", {"id": db.clip_id(p), "path": str(p), "rel": p.name, "duration": 6.0, "fps": 25.0,
                                 "width": 640, "height": 360, "proxy": str(p), "status": "analyzed"})
    con.commit()
    return cfg, con, {k: db.clip_id(p) for k, p in clips.items()}


def test_matchcuts_finds_daynight_pair(mc_project, capsys):
    cfg, con, ids = mc_project
    res = matchcuts.mine(cfg, con, min_score=0.6)
    out = capsys.readouterr().out
    assert "matchcuts:" in out
    pair = {ids["day"], ids["night"]}
    hits = [c for c in res if {c["a"], c["b"]} == pair]
    assert hits, res
    best = hits[0]
    assert best["kind"] == "daynight" and best["a"] == ids["day"]          # bright side first
    assert best["score"] >= 0.75
    unrelated = [c["score"] for c in res if ids["other"] in (c["a"], c["b"])]
    assert all(best["score"] > s + 0.1 for s in unrelated)
    # the same comparison straight from the cached features: related beats every unrelated pair
    rows = {k: con.execute("SELECT * FROM clips WHERE id=?", (v,)).fetchone() for k, v in ids.items()}
    F = {k: matchcuts.clip_features(cfg, r) for k, r in rows.items()}

    def best_sim(x, y):
        Dx = matchcuts._descriptors(F[x]["edge"].astype(np.float32), F[x]["layout"].astype(np.float32))
        Dy = matchcuts._descriptors(F[y]["edge"].astype(np.float32), F[y]["layout"].astype(np.float32))
        Dx /= np.linalg.norm(Dx, axis=1, keepdims=True)
        Dy /= np.linalg.norm(Dy, axis=1, keepdims=True)
        with np.errstate(all="ignore"):          # Accelerate BLAS raises spurious FP flags
            return float((Dx @ Dy.T).max())

    assert best_sim("day", "night") > max(best_sim("day", "other"), best_sim("night", "other")) + 0.2
    stored = con.execute("SELECT COUNT(*) FROM matchcuts WHERE kind='daynight'").fetchone()[0]
    assert stored >= 1
    al = json.loads(matchcuts._align_path(cfg, best["a"], best["ta"], best["b"], best["tb"]).read_text())
    assert al["align"]["ok"] and abs(al["align"]["scale"] - 1) < 0.05            # same framing
    pv = matchcuts.preview_pair(cfg, con, best["a"], best["ta"], best["b"], best["tb"])
    assert Path(pv["still"]).exists() and Path(pv["clip"]).exists()
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                pv["clip"]], capture_output=True, text=True).stdout)
    assert abs(dur - 2.0) < 0.1
