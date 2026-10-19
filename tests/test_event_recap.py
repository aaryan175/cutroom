"""Event recap mode on synthetic footage: stage / crowd / wide classification, crowd-energy peaks,
soundbite scoring (with the crowd's response after a line) and the structure of the `cutroom recap`
draft, which must pass `cutroom plan check`.

Stand-ins (lavfi, no real footage):
  crowd  a moving cellular-automaton pattern (lots of local motion in a busy frame) with a loud
         applause-like noise burst (clap transients) in the middle of the clip
  stage  a coloured spotlight drifting over a dark stage, with a harmonic tone shaped into syllables
         (a speech stand-in); one line is followed by applause, the other by near silence
  wide   a frozen busy frame (no local motion) with a quiet room rumble
  song   a 120 BPM click track that gets denser at 20 s (the drop)
"""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "96k", "-shortest"]
D = 12
CLAPS = ("(random(0)*2-1)*(0.12+0.88*min(1,lt(mod(t*9.7,1),0.12)+lt(mod(t*6.1+0.3,1),0.1)"
         "+lt(mod(t*13.3+0.6,1),0.08)))")
SYL = ("0.45*(sin(2*PI*170*t)+0.5*sin(2*PI*340*t)+0.25*sin(2*PI*510*t))*pow(max(0,sin(2*PI*3.7*t)),2)"
       "*(0.6+0.4*sin(2*PI*0.4*t))")
LINE_A = (0.4, 2.8, "We never stop chasing this dream together!")     # followed by applause
LINE_B = (6.4, 8.8, "next we look at the slide about logistics.")     # followed by quiet
LINE_C = (0.4, 2.8, "Tonight you become the first of many!")          # the second stage clip's big line


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


def make_event_library(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    p = {}
    for k, seed in (("crowd1", 1), ("crowd2", 7), ("crowd3", 23)):
        p[k] = root / f"{k}.mp4"
        _ff("-f", "lavfi", "-i", f"life=s=320x180:mold=10:rate=25:ratio=0.45:seed={seed}:death_color=#505050:"
                                 f"life_color=#d8c8b0,scale=1280:720:flags=neighbor,trim=duration={D}",
            "-f", "lavfi", "-i", f"aevalsrc='if(between(t,4,8),{CLAPS},0.02*(random(0)*2-1))':s=48000:d={D}",
            *X264, *AAC, p[k])
    spot = ("geq=lum='if(lt(hypot(X-640-140*sin(T*0.6),Y-320),150),235,if(lt(hypot(X-640-140*sin(T*0.6),Y-320),230),"
            "55,14))':cb='if(lt(hypot(X-640-140*sin(T*0.6),Y-320),150),80,128)':"
            "cr='if(lt(hypot(X-640-140*sin(T*0.6),Y-320),150),205,128)'")
    talk = (f"if(between(t,{LINE_A[0]},{LINE_A[1]})+between(t,{LINE_B[0]},{LINE_B[1]}),{SYL},0)"
            f"+if(between(t,3.1,5.9),{CLAPS},0.004*(random(0)*2-1))")
    for k in ("stage1", "stage2"):
        p[k] = root / f"{k}.mp4"
        _ff("-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=25:d={D},{spot}",
            "-f", "lavfi", "-i", f"aevalsrc='{talk}':s=48000:d={D}", *X264, *AAC, p[k])
    for k, seed in (("wide1", 3), ("wide2", 11)):
        p[k] = root / f"{k}.mp4"
        _ff("-f", "lavfi", "-i", f"life=s=640x360:mold=10:rate=25:ratio=0.3:seed={seed}:death_color=#283040:"
                                 f"life_color=#c0b080,trim=start_frame=40:end_frame=41,loop=loop=-1:size=1,"
                                 f"trim=duration={D},setpts=N/25/TB,scale=1280:720:flags=neighbor",
            "-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.03:d={D}", *X264, *AAC, p[k])
    mus = root.parent / "music"
    mus.mkdir(exist_ok=True)
    p["song"] = mus / "build120.wav"
    _ff("-f", "lavfi", "-i", "aevalsrc='(0.5+0.5*eq(mod(floor(t*2),4),0))*(sin(2*PI*80*t)*exp(-25*mod(t,0.5))"
                             "+0.6*sin(2*PI*1500*t)*exp(-90*mod(t,0.5)))"
                             "+gt(t,20)*0.35*(random(0)*2-1)*exp(-40*mod(t+0.25,0.5))':d=60:s=44100",
        "-c:a", "pcm_s16le", p["song"])
    return p


@pytest.fixture(scope="module")
def eproj(tmp_path_factory):
    from cutroom import analyze, asr, db, ingest, music
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("event")
    lib = make_event_library(base / "footage")
    cfg = init_project(base / "work", [base / "footage"], [base / "music"])
    cfg.workers = 2
    cfg.out_width, cfg.out_height = 1280, 720
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    analyze.analyze_all(cfg, con)
    music.index_songs(cfg, con)
    ids = {k: db.clip_id(v) for k, v in lib.items() if k != "song"}
    for k, first in (("stage1", LINE_A), ("stage2", LINE_C)):    # hand-written: no speech-to-text in tests
        tr = _transcript([first, LINE_B])
        asr._store(cfg, con, ids[k], "en", "hand-written", tr["segments"], 14, 4.8)
    analyze.events_all(cfg, con)
    return {"cfg": cfg, "con": con, "lib": lib, "ids": ids}


def test_event_scores_pure():
    from cutroom.analyze import event_scores
    pic = [{"t0": 0.0, "t1": 2.0, "faces": 0, "face_h": 0.0, "local_motion": 14.0, "edges": 0.14, "luma": 110,
            "dark": 0.0, "lights": 0.0},                                         # busy + moving: crowd
           {"t0": 2.0, "t1": 4.0, "faces": 1, "face_h": 0.22, "local_motion": 1.0, "edges": 0.05, "luma": 90,
            "dark": 0.3, "lights": 0.2},                                         # one big face: stage
           {"t0": 4.0, "t1": 6.0, "faces": 0, "face_h": 0.0, "local_motion": 0.5, "edges": 0.13, "luma": 80,
            "dark": 0.2, "lights": 0.0}]                                         # busy, still: wide
    aud = [{"t0": 0.0, "loud": -14.0, "flux": 1.0, "flat": 0.6, "onsets": 9.0, "mod": 0.2},
           {"t0": 2.0, "loud": -22.0, "flux": 0.5, "flat": 0.1, "onsets": 2.0, "mod": 0.6},
           {"t0": 4.0, "loud": -45.0, "flux": 0.1, "flat": 0.3, "onsets": 0.0, "mod": 0.1}]
    w = event_scores(pic, aud)
    assert [x["kind"] for x in w] == ["crowd", "stage", "wide"]
    assert w[0]["energy"] > w[1]["energy"] > w[2]["energy"]
    assert w[0]["peak"] == 1 and w[2]["peak"] == 0


def test_soundbite_response_scoring():
    """Same line twice: the one the room answers with applause, said louder and higher, ranks first."""
    from cutroom.analyze import score_soundbites
    sr = 16000
    t = np.arange(int(sr * 14)) / sr
    rng = np.random.default_rng(0)

    def voice(f0, amp):
        return amp * (np.sin(2 * np.pi * f0 * t) + 0.5 * np.sin(4 * np.pi * f0 * t)) * np.maximum(0, np.sin(2 * np.pi * 3.7 * t)) ** 2
    y = 0.003 * rng.standard_normal(len(t))
    y = y + np.where((t > 0.5) & (t < 2.5), voice(220, 0.5), 0) + np.where((t > 7.5) & (t < 9.5), voice(150, 0.15), 0)
    claps = rng.standard_normal(len(t)) * (0.1 + 0.9 * ((np.mod(t * 9.7, 1) < 0.12) | (np.mod(t * 6.1, 1) < 0.1)))
    y = (y + np.where((t > 2.7) & (t < 6.0), 0.5 * claps, 0)).astype(np.float32)
    words = []
    for a, b in ((0.5, 2.5), (7.5, 9.5)):
        ws = "you will never forget tonight!".split()
        step = (b - a) / len(ws)
        words += [(a + i * step, a + (i + 0.85) * step, w) for i, w in enumerate(ws)]
    rows = score_soundbites(words, y, sr)
    assert len(rows) == 2
    best, other = rows
    assert best["t0"] < 3 and best["response"] > other["response"] + 0.2
    assert best["loud"] > other["loud"] and best["pitch"] > other["pitch"]


def test_classification(eproj):
    con, ids = eproj["con"], eproj["ids"]
    kinds = {k: con.execute("SELECT kind FROM event_clips WHERE clip_id=?", (cid,)).fetchone()["kind"]
             for k, cid in ids.items()}
    assert kinds == {"crowd1": "crowd", "crowd2": "crowd", "crowd3": "crowd", "stage1": "stage", "stage2": "stage",
                     "wide1": "wide", "wide2": "wide"}, kinds


def test_energy_peaks(eproj):
    con, ids = eproj["con"], eproj["ids"]
    for k in ("crowd1", "crowd2"):
        r = con.execute("SELECT energy, energy_t FROM event_clips WHERE clip_id=?", (ids[k],)).fetchone()
        assert 4.0 <= r["energy_t"] < 8.0, (k, r["energy_t"])          # inside the applause burst
        peaks = [w["t0"] for w in con.execute("SELECT t0 FROM event_windows WHERE clip_id=? AND peak=1", (ids[k],))]
        assert peaks and all(4.0 <= t < 8.0 for t in peaks), peaks
        quiet = con.execute("SELECT MAX(energy) FROM event_windows WHERE clip_id=? AND t0 < 4", (ids[k],)).fetchone()[0]
        assert r["energy"] > quiet + 0.2
    crowd_peak = con.execute("SELECT energy FROM event_clips WHERE clip_id=?", (ids["crowd1"],)).fetchone()[0]
    wide_peak = con.execute("SELECT energy FROM event_clips WHERE clip_id=?", (ids["wide1"],)).fetchone()[0]
    assert crowd_peak > wide_peak + 0.3


def test_soundbites_found(eproj):
    con, ids = eproj["con"], eproj["ids"]
    rows = con.execute("SELECT * FROM soundbites WHERE clip_id=? ORDER BY score DESC", (ids["stage1"],)).fetchall()
    assert len(rows) == 2
    assert rows[0]["t0"] == pytest.approx(LINE_A[0], abs=0.05)               # the line the room answered
    assert rows[0]["response"] > rows[1]["response"] + 0.2


def test_search_kind_and_soundbites(eproj, capsys, monkeypatch):
    from cutroom import cli
    monkeypatch.chdir(eproj["cfg"].work)
    cli.main(["search", "--kind", "crowd", "--json"])
    res = json.loads(capsys.readouterr().out)
    assert {x["id"] for x in res} == {eproj["ids"][k] for k in ("crowd1", "crowd2", "crowd3")}
    assert all(x["event"]["score"] > 0.35 for x in res)
    cli.main(["search", "--kind", "wide"])
    out = capsys.readouterr().out
    assert eproj["ids"]["wide1"] in out and "[wide " in out
    cli.main(["search", "--soundbites", "dream"])
    out = capsys.readouterr().out
    assert "chasing this dream" in out and "logistics" not in out


def test_recap_plan_structure(eproj, capsys, monkeypatch):
    from cutroom import cli, music, plan as P, recap, render
    cfg, con, ids = eproj["cfg"], eproj["con"], eproj["ids"]
    path, rep, errs = recap.write(cfg, con, "Recap Test", length=40)
    assert not errs, errs
    plan = json.loads(Path(path).read_text())
    assert path.name == "recap-test_v1.json" and plan["version"] == 1
    names = [s["name"] for s in plan["sections"]]
    assert names == ["cold open", "build", "climax"]
    cold, build, climax = plan["sections"]
    # cold open: the best soundbite (the line the room cheered), then the crowd reacting
    assert cold["segments"][0]["type"] == "talk" and cold["segments"][0]["clip"] in (ids["stage1"], ids["stage2"])
    assert cold["segments"][0]["in"] <= LINE_A[0] and cold["segments"][0]["out"] >= LINE_A[1]
    assert cold["segments"][1]["type"] == "broll" and cold["segments"][1]["audio"] == "natural"
    crowd_ids = {ids[k] for k in ("crowd1", "crowd2", "crowd3")}
    assert cold["segments"][1]["clip"] in crowd_ids
    assert cold["music"]["mode"] == "under"
    # build: beat-synced montages, music ducked under the soundbites, J-cut talk then a crowd reaction
    assert build["music"]["mode"] == "duck" and build["transition"]["kind"] == "flash"
    assert build["segments"][0]["type"] == "montage" and build["segments"][0]["beat_sync"]
    talks = [k for k, s in enumerate(build["segments"]) if s["type"] == "talk"]
    assert talks
    for k in talks:
        assert build["segments"][k]["lead_in"] == pytest.approx(0.3)
        assert build["segments"][k + 1]["type"] == "broll" and build["segments"][k + 1]["clip"] in crowd_ids
    shot_kinds = {sh["clip"] for s in build["segments"] if s["type"] == "montage" for sh in s["shots"]}
    assert shot_kinds & crowd_ids and shot_kinds & {ids["wide1"], ids["wide2"]}
    # climax: a flash onto the drop, fast montage, the biggest peak held, the end card last
    assert climax["transition"]["kind"] == "flash"
    si = music.song_info(cfg, con, rep["song"])
    assert climax["music"]["offset"] in [round(d, 3) for d in si["drops"]] or climax["music"]["offset"] > 0
    assert climax["segments"][0]["type"] == "montage"
    held = climax["segments"][-2]
    assert held["type"] == "broll" and held["audio"] == "natural" and held["clip"] in crowd_ids
    top = con.execute("SELECT MAX(energy) FROM event_windows WHERE kind IN ('crowd','stage')").fetchone()[0]
    assert con.execute("SELECT MAX(energy) FROM event_windows WHERE clip_id=? AND t0 <= ? AND t1 > ?",
                       (held["clip"], held["in"], held["in"])).fetchone()[0] == pytest.approx(top)
    # the same line on two cameras is used once
    texts = [b["text"] for b in rep["soundbites"]]
    assert len(texts) == len(set(texts))
    assert climax["segments"][-1]["type"] == "title" and climax["music"]["end"] == {"fade": 1.5}
    # flashes: on downbeats, never more than the limit in any 10 s
    res = render.resolve_timeline(cfg, con, plan, preview=True)
    segrow = {(r["sec"], r["seg"]): r for r in res["segments"]}
    flashes = [segrow[(si_, gi)]["start"] for si_, sec in enumerate(plan["sections"])
               for gi, s in enumerate(sec["segments"])
               if (s.get("transition") or (sec.get("transition") if gi == 0 else None) or {}).get("kind") == "flash"]
    assert len(flashes) >= 2
    for a in flashes:
        assert sum(1 for x in flashes if a <= x < a + 10.0) <= recap.RECAP_DEFAULTS["flash_max_per_10s"]
    downs = np.asarray(si["downbeats"], float)
    secs = {s["index"]: s for s in res["sections"]}
    for k, s in enumerate(build["segments"]):
        if k and (s.get("transition") or {}).get("kind") == "flash":
            st = build["music"]["offset"] + segrow[(1, k)]["start"] - secs[1]["start"]
            assert float(np.min(np.abs(downs - st))) <= recap.RECAP_DEFAULTS["downbeat_tol"] + 1e-6
    # ramps only on montage shots, never on talk
    for sec in plan["sections"]:
        for s in sec["segments"]:
            assert "ramp" not in s
    # length near the target, and it passes `cutroom plan check`
    assert abs(res["duration"] - 40) <= 0.25 * 40, res["duration"]
    assert not P.validate(P.normalize(plan, cfg), con)
    monkeypatch.chdir(cfg.work)
    with pytest.raises(SystemExit) as ex:
        cli.main(["plan", "check", str(path)])
    assert ex.value.code == 0, capsys.readouterr().out
    # a second run never overwrites: it writes v2
    p2, _, _ = recap.write(cfg, con, "Recap Test", length=40)
    assert p2.name == "recap-test_v2.json"


def test_recap_cli_needs_events(tmp_path, monkeypatch):
    from cutroom import cli
    from cutroom.config import init_project
    cfg = init_project(tmp_path / "w", [tmp_path / "lib"])
    (tmp_path / "lib").mkdir(exist_ok=True)
    monkeypatch.chdir(cfg.work)
    with pytest.raises(SystemExit) as ex:
        cli.main(["recap", "x", "--len", "30"])
    assert "detect events" in str(ex.value.code)
