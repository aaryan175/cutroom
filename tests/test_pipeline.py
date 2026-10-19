import json
import subprocess
from argparse import Namespace

from PIL import Image

from cutroom import analyze, db, ingest, tags


def _row(project, key):
    return project["con"].execute("SELECT * FROM clips WHERE id=?", (project["ids"][key],)).fetchone()


def test_scan_finds_only_videos(project):
    con = project["con"]
    rels = sorted(r["rel"] for r in con.execute("SELECT rel FROM clips"))
    assert len(rels) == 5, rels
    assert not any("._" in r or ".hidden" in r or "tiny" in r or "loop" in r for r in rels)


def test_rescan_keeps_status(project, capsys):
    cfg, con = project["cfg"], project["con"]
    before = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM clips")}
    stats = ingest.scan(cfg, con)
    assert stats["new"] == 0 and stats["changed"] == 0
    assert before == {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM clips")}


def test_probe_fields(project):
    r = _row(project, "steady")
    assert r["width"] == 1920 and r["height"] == 1080 and r["has_audio"] == 1
    assert abs(r["duration"] - 8) < 0.2 and abs(r["fps"] - 25) < 0.1
    v = _row(project, "vertical")
    assert abs(v["rotation"]) == 90
    assert ingest.display_size(v) == (1080, 1920)
    assert _row(project, "dark")["has_audio"] == 0


def test_all_analyzed(project):
    con = project["con"]
    bad = con.execute("SELECT rel, status, error FROM clips WHERE status != 'analyzed'").fetchall()
    assert not bad, [dict(b) for b in bad]


def test_proxies_decode(project):
    cfg = project["cfg"]
    for key in project["ids"]:
        r = _row(project, key)
        info = subprocess.run([cfg.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                               "-show_entries", "stream=width,height,nb_read_frames", "-of", "json", r["proxy"]],
                              capture_output=True, text=True, check=True)
        s = json.loads(info.stdout)["streams"][0]
        assert int(s["nb_read_frames"]) > 50
        if key == "vertical":
            assert s["height"] == cfg.proxy_height and s["width"] < s["height"]
        else:
            assert s["height"] == cfg.proxy_height and s["width"] == 960


def test_sheets_are_jpegs(project):
    for key in project["ids"]:
        r = _row(project, key)
        im = Image.open(r["sheet"])
        im.verify()
        im = Image.open(r["sheet"])
        assert im.format == "JPEG" and im.width <= 1920 and im.height > 150


def test_shake_and_exposure(project):
    con, ids = project["con"], project["ids"]
    steady = analyze.clip_quality(con, ids["steady"])
    shaky = analyze.clip_quality(con, ids["shaky"])
    dark = analyze.clip_quality(con, ids["dark"])
    assert shaky["shake"] > steady["shake"] + 2, (steady, shaky)
    assert steady["shake"] < 2, steady
    assert dark["exposure"] < 40 < steady["exposure"], (dark, steady)
    assert steady["windows"] == 4


def test_scene_cuts(project):
    con, ids = project["con"], project["ids"]
    shots = con.execute("SELECT * FROM shots WHERE clip_id=? ORDER BY idx", (ids["scenes"],)).fetchall()
    assert len(shots) >= 2
    assert any(abs(s["start"] - 3) < 0.5 for s in shots[1:])
    one = con.execute("SELECT COUNT(*) FROM shots WHERE clip_id=?", (ids["steady"],)).fetchone()[0]
    assert one == 1


def test_window_helpers(project):
    con, ids = project["con"], project["ids"]
    w = analyze.window_quality(con, ids["steady"], 1.0, 5.0)
    assert w["windows"] == 3
    t0, t1 = analyze.steadiest_window(con, ids["shaky"], 3.0)
    assert 0 <= t0 < t1 <= 8.01 and abs((t1 - t0) - 3) < 1e-6


def _search_args(**kw):
    base = dict(text="", setting=None, time=None, people=None, shot=None, camera=None, max_shake=None,
                min_quality=None, speech=False, no_speech=False, creator=False, min_dur=None, limit=30, json=True)
    base.update(kw)
    return Namespace(**base)


def test_tags_search(project, capsys):
    cfg, con, ids = project["cfg"], project["con"], project["ids"]
    db.set_tags(con, ids["steady"], {"summary": "test pattern on a rooftop", "setting": "rooftop",
                                     "time_of_day": "day", "quality": 4})
    db.set_tags(con, ids["dark"], {"summary": "dark rooftop corner", "usable": False})
    res = tags.search(cfg, con, _search_args(text="Rooftop pattern"))
    assert [r["id"] for r in res] == [ids["steady"]]
    res = tags.search(cfg, con, _search_args(setting="roof"))
    assert [r["id"] for r in res] == [ids["steady"]]
    res = tags.search(cfg, con, _search_args(text="shaky"))           # path match
    assert res and res[0]["id"] == ids["shaky"] and res[0]["match"] == "path"
    res = tags.search(cfg, con, _search_args(max_shake=2.0))
    assert ids["shaky"] not in [r["id"] for r in res]
    capsys.readouterr()
    tags.search(cfg, con, _search_args(text="rooftop", json=False))
    assert ids["steady"] in capsys.readouterr().out


def test_transcript_hit(project):
    cfg, con, ids = project["cfg"], project["con"], project["ids"]
    data = {"language": "en", "segments": [{"start": 0.5, "end": 3.0, "text": "hello there, coffee time",
            "words": [{"start": 0.5, "end": 0.9, "word": " hello", "probability": 0.9},
                      {"start": 1.0, "end": 1.4, "word": " there,", "probability": 0.9},
                      {"start": 1.6, "end": 2.1, "word": " coffee", "probability": 0.9},
                      {"start": 2.2, "end": 2.6, "word": " time", "probability": 0.9}]}]}
    cfg.p("transcripts", f"{ids['vertical']}.json").write_text(json.dumps(data))
    db.upsert(con, "transcripts", {"clip_id": ids["vertical"], "path": "", "language": "en", "n_words": 4,
                                   "speech_s": 2.1, "text": "hello there, coffee time", "model": "test",
                                   "updated": 0}, key=("clip_id",))
    con.commit()
    res = tags.search(cfg, con, _search_args(text="coffee"))
    assert res[0]["id"] == ids["vertical"] and res[0]["hit_t"] == 1.6


def test_todo_and_describe(project, capsys):
    cfg, con, ids = project["cfg"], project["con"], project["ids"]
    tags.todo(cfg, con, 10)
    out = capsys.readouterr().out
    assert ids["shaky"] in out and "cutroom tags set" in out and ".jpg" in out
    tags.describe(cfg, con, ids["scenes"], frames=2)
    out = capsys.readouterr().out
    assert "shots    " in out and "frames" in out
    frames = [ln.split()[-1] for ln in out.splitlines() if ln.strip().endswith(".jpg") and "frames/" in ln]
    assert len(frames) == 2 and all(Image.open(f).width == 1920 for f in frames)
