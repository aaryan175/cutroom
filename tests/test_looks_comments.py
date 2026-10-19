"""Looks (filter chains, previews, plan validation, render) and review comments (drawing composite,
the one-by-one AI run queue against a fake `claude`, one undo step per comment, stop)."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from test_studio import SSEListener, req

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


@pytest.fixture(scope="module")
def lc(tmp_path_factory):
    from cutroom import db, ingest, studio
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("looks")
    lib = base / "footage"
    lib.mkdir()
    clip = lib / "colour.mp4"
    # warm-ish natural picture: gradients + colour bars, so saturation and luma changes are measurable
    subprocess.run(FF + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=6",
                         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
                         "-vf", "eq=saturation=0.7:brightness=-0.02",
                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", "-shortest", str(clip)], check=True)
    cfg = init_project(base / "work", [lib])
    cfg.workers = 2
    cfg.out_width, cfg.out_height, cfg.preview_height = 640, 360, 180
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    cid = db.clip_id(clip)
    srv, st = studio.make_server(cfg, 0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    yield {"cfg": cfg, "con": con, "cid": cid, "port": srv.server_address[1], "studio": st, "base": base}
    st.close()
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------------------------------- looks
def _stats(path):
    from cutroom import looks
    return looks.image_stats(path)


def test_every_builtin_look_runs_through_ffmpeg(lc, tmp_path):
    from cutroom import looks
    names = [b["name"] for b in looks.BUILTIN]
    src = tmp_path / "frame.png"
    subprocess.run(FF + ["-f", "lavfi", "-i", "testsrc2=size=480x270:d=1", "-frames:v", "1", str(src)], check=True)
    base = None
    for nm in names:
        for k in (0.5, 1.0):
            ch = looks.chain(lc["cfg"], {"name": nm, "intensity": k})
            out = tmp_path / f"{nm}_{k}.jpg"
            subprocess.run(FF + ["-i", str(src), "-vf", ",".join(["format=yuv420p", *(ch or ["null"])]),
                                 "-frames:v", "1", str(out)], check=True)
            assert out.stat().st_size > 1000, nm
        if nm == "natural":
            assert looks.chain(lc["cfg"], {"name": nm, "intensity": 1}) == []
            base = _stats(tmp_path / "natural_1.0.jpg")
    bw = _stats(tmp_path / "bw_1.0.jpg")
    assert bw["sat"] < base["sat"] * 0.25
    night = _stats(tmp_path / "night_cinematic_1.0.jpg")
    assert night["luma"] < base["luma"] - 8
    warm = _stats(tmp_path / "warm_film_1.0.jpg")
    assert warm["warmth"] > base["warmth"] + 3
    # half intensity sits between none and full
    half = _stats(tmp_path / "bw_0.5.jpg")
    assert bw["sat"] < half["sat"] < base["sat"]


def test_look_validation(lc):
    from cutroom import looks, plan as P
    assert looks.normalize_look("bw") == {"name": "bw", "intensity": 1.0}
    assert looks.normalize_look(None) is None
    seg = {"type": "broll", "clip": lc["cid"], "in": 0, "out": 2}
    ok = {"sections": [{"segments": [{**seg, "look": {"name": "golden_hour", "intensity": 0.6}}]}],
          "style": {"grade": {"look": {"name": "teal_orange", "intensity": 0.8}}}}
    assert P.validate(P.normalize(ok)) == []
    bad = {"sections": [{"segments": [{**seg, "look": {"name": "sepia_dream"}}]}],
           "style": {"grade": {"look": {"name": "bw", "intensity": 1.7}}}}
    errs = P.validate(P.normalize(bad))
    assert any("style.grade.look" in e and "intensity" in e for e in errs), errs
    assert any("segment 0 look" in e and "unknown look" in e for e in errs), errs
    assert P.normalize({"sections": []})["style"]["grade"]["look"] is None
    # a lut look needs the file in <work>/luts
    from cutroom.render import extra_validate
    missing = {"sections": [{"segments": [{**seg, "look": "lut:nope.cube"}]}]}
    assert any("no LUT file" in e for e in extra_validate(missing, lc["con"], lc["cfg"]))


def test_lut_look_intensity_blend(lc):
    from cutroom import looks
    d = Path(lc["cfg"].work) / "luts"
    d.mkdir(exist_ok=True)
    n = 5
    g = np.linspace(0, 1, n)
    rows = [f"{min(1, r * 1.2):.4f} {gg:.4f} {b * 0.8:.4f}" for b in g for gg in g for r in g]   # warm LUT
    (d / "Warm Test.cube").write_text(f"LUT_3D_SIZE {n}\n" + "\n".join(rows) + "\n")
    cat = looks.catalogue(lc["cfg"])
    assert any(c["name"] == "lut:Warm Test.cube" and c["kind"] == "lut" for c in cat)
    full = looks._lut_for(lc["cfg"], "lut:Warm Test.cube", 1.0)
    half = looks._lut_for(lc["cfg"], "lut:Warm Test.cube", 0.5)
    assert full != half and half.exists()
    _, _, _, rows_h = looks._read_cube(half)
    assert abs(rows_h[-1][2] - 0.9) < 1e-3              # blue at white: halfway between 1.0 and 0.8
    assert looks.chain(lc["cfg"], {"name": "lut:Warm Test.cube", "intensity": 0.5})[1].startswith(f"lut3d=file='{half}'")


def test_preview_route_jpeg_and_cache(lc):
    S = lc
    q = f"/api/looks/preview?clip={S['cid']}&t=2&look=golden_hour&intensity=80"
    t0 = time.time()
    code, hdr, body = req(S, "GET", q, raw=True)
    first = time.time() - t0
    assert code == 200 and body[:2] == b"\xff\xd8", (code, body[:200])
    files = list(Path(S["cfg"].work, "frames", "looks").glob("*.jpg"))
    t0 = time.time()
    code, _, body2 = req(S, "GET", q, raw=True)
    assert code == 200 and body2 == body
    assert list(Path(S["cfg"].work, "frames", "looks").glob("*.jpg")) == files      # served from cache
    assert time.time() - t0 < max(0.5, first)
    code, sheet = req(S, "GET", f"/api/looks/sheet?clip={S['cid']}&t=2")
    assert code == 200 and len(sheet["items"]) >= 11, sheet
    urls = {it["name"]: it["url"] for it in sheet["items"]}
    stats = {}
    for name in ("natural", "bw", "punchy"):
        code, _, img = req(S, "GET", urls[name], raw=True)
        assert code == 200 and img[:2] == b"\xff\xd8"
        p = Path(S["cfg"].work, "frames", "looks", urls[name].rsplit("/", 1)[1])
        stats[name] = _stats(p)
    assert stats["bw"]["sat"] < stats["natural"]["sat"] * 0.3
    assert stats["punchy"]["sat"] > stats["natural"]["sat"]
    code, e = req(S, "GET", f"/api/looks/preview?clip={S['cid']}&t=2&look=bogus")
    assert code == 400


def test_preview_from_timeline_and_match(lc, monkeypatch):
    S = lc
    req(S, "POST", "/api/plan/new", {"title": "Looks"})
    req(S, "POST", "/api/plan/looks/op", {"op": "insert", "at_t": 0,
                                          "segment": {"type": "broll", "clip": S["cid"], "in": 1, "out": 4}})
    code, sheet = req(S, "GET", "/api/looks/sheet?plan=looks&t=1.0")
    assert code == 200 and sheet["clip"] == S["cid"] and abs(sheet["t"] - 2.0) < 0.05 and sheet["where"]["seg"] == 0
    # match against a fake analysed reference whose colour is black and white
    from cutroom import looks
    rid = "abc123def0"
    d = Path(S["cfg"].work, "refs", rid)
    d.mkdir(parents=True, exist_ok=True)
    nat = _stats(Path(S["cfg"].work, "frames", "looks", sheet["items"][0]["url"].rsplit("/", 1)[1]))
    target = {"luma": nat["luma"], "contrast": nat["contrast"] * 1.08, "sat": 4.0, "warmth": 0.0}
    (d / "meta.json").write_text(json.dumps({"id": rid, "status": "analyzed", "title": "bw ref"}))
    (d / "analysis.json").write_text(json.dumps({"colour": {"overall": target, "by_section": []}, "shots": []}))
    from cutroom import reference as R
    monkeypatch.setattr(R, "resolve_id", lambda cfg, x: rid)
    code, m = req(S, "GET", f"/api/looks/match?ref={rid}&plan=looks&t=1.0")
    assert code == 200, m
    assert m["pick"]["name"] == "bw" and m["pick"]["intensity"] >= 0.8, m
    assert m["preview_url"].startswith("/media/look/") and m["distance"] < m["base_distance"]
    assert looks.label_of("bw") == "Black & White"


def test_render_applies_look(lc):
    """Same plan rendered with and without a look: the look measurably changes the output frames,
    and the timeline exposes it."""
    from cutroom import render
    S = lc
    seg = {"type": "broll", "clip": S["cid"], "in": 1.0, "out": 3.0}
    base = {"title": "lk", "version": 1, "sections": [{"name": "a", "segments": [seg]}],
            "style": {"captions": {"enabled": False}, "grade": {"normalize_exposure": False}}}
    plans = Path(S["cfg"].work, "plans")
    out = {}
    for name, look in (("none", None), ("bw", {"name": "bw", "intensity": 1.0}),
                       ("night", {"name": "night_cinematic", "intensity": 1.0})):
        p = json.loads(json.dumps(base))
        if look:
            p["style"]["grade"]["look"] = look
        pf = plans / f"lk_{name}_v1.json"
        pf.write_text(json.dumps(p))
        mp4 = render.render_plan(S["cfg"], S["con"], pf, preview=True, out=Path(S["cfg"].work, "renders", f"lk_{name}.mp4"))
        fr = Path(S["cfg"].work, "renders", f"lk_{name}.png")
        subprocess.run(FF + ["-ss", "1.0", "-i", str(mp4), "-frames:v", "1", str(fr)], check=True)
        out[name] = _stats(fr)
        side = json.loads(Path(str(mp4)[:-4] + ".json").read_text())
        g = next(x for x in side["timeline"] if x.get("grade")) if isinstance(side.get("timeline"), list) else None
        if g is not None and look:
            assert g["grade"]["look"]["name"] == look["name"]
    assert out["bw"]["sat"] < out["none"]["sat"] * 0.3, out
    assert out["night"]["luma"] < out["none"]["luma"] - 6, out
    # per-segment look wins; natural opts a segment out
    p = json.loads(json.dumps(base))
    p["style"]["grade"]["look"] = {"name": "bw", "intensity": 1}
    p["sections"][0]["segments"].append({**seg, "look": {"name": "natural"}})
    tl = render.resolve_timeline(S["cfg"], S["con"], p)
    v1 = [x for x in tl["pieces"] if x["track"] == "V1"]
    assert v1[0]["look"]["name"] == "bw" and v1[1]["look"] is None and tl["look"]["name"] == "bw"


# ---------------------------------------------------------------------------------------------- comments
def test_drawing_composite(lc):
    S = lc
    req(S, "POST", "/api/plan/new", {"title": "Draw"})
    req(S, "POST", "/api/plan/draw/op", {"op": "insert", "at_t": 0,
                                         "segment": {"type": "broll", "clip": S["cid"], "in": 0, "out": 3}})
    drawing = [{"tool": "circle", "points": [[0.3, 0.3], [0.6, 0.7]], "color": "#ff3b30"},
               {"tool": "arrow", "points": [[0.1, 0.9], [0.28, 0.6]], "color": "#00ff00"},
               {"tool": "pen", "points": [[0.7, 0.2], [0.75, 0.25], [0.8, 0.2]], "color": "#ffffff"},
               {"tool": "box", "points": [[0.05, 0.05], [0.2, 0.2]], "color": "#3c74fb"}]
    code, m = req(S, "POST", "/api/markers", {"plan": "draw", "t": 1.0, "t_end": 2.0, "text": "remove this",
                                              "drawing": drawing})
    assert code == 200, m
    assert m["frame_raw_url"].startswith("/media/marker/") and m["frame_url"].startswith("/media/grab/comment_")
    assert m["t_end"] == 2.0 and m["author"] == "you" and m["thread"] == [] and len(m["drawing"]) == 4
    from PIL import Image
    raw = np.asarray(Image.open(S["studio"].media_url_to_path(m["frame_raw_url"])).convert("RGB")).astype(int)
    ann = np.asarray(Image.open(S["studio"].media_url_to_path(m["frame_url"])).convert("RGB")).astype(int)
    assert raw.shape == ann.shape
    H, W = raw.shape[:2]
    # the circle's left edge (x=0.3, y=0.5) is red now; the centre of the circle is untouched
    y, x = int(0.5 * H), int(0.3 * W)
    px = ann[y - 2:y + 3, x - 3:x + 4].reshape(-1, 3)
    assert any(r > 200 and g < 90 and b < 90 for r, g, b in px), px
    cy, cx = int(0.5 * H), int(0.45 * W)
    assert np.abs(ann[cy, cx] - raw[cy, cx]).max() < 25
    # PATCH a new drawing re-composites from the raw frame
    code, m2 = req(S, "PATCH", f"/api/markers/{m['id']}", {"drawing": drawing[:1]})
    assert code == 200 and m2["frame_url"] != m["frame_url"] and len(m2["drawing"]) == 1
    code, e = req(S, "POST", "/api/markers", {"plan": "draw", "t": 1.0, "drawing": [{"tool": "laser", "points": [[0, 0]]}]})
    assert code == 400
    # thread reply + new statuses
    code, m3 = req(S, "PATCH", f"/api/markers/{m['id']}", {"reply": {"text": "Use the second take"}, "status": "needs_you"})
    assert code == 200 and m3["status"] == "needs_you" and m3["thread"][-1]["text"] == "Use the second take"
    code, e = req(S, "PATCH", f"/api/markers/{m['id']}", {"status": "bogus"})
    assert code == 400
    req(S, "DELETE", f"/api/markers/{m['id']}")


FAKE_AGENT = r'''#!{py}
import json, os, re, subprocess, sys, time
prompt = sys.stdin.read()
log = os.environ["FAKE_LOG"]
mid = re.search(r"comment id `([0-9a-f]+)`", prompt)
mid = mid.group(1) if mid else None
draft = re.search(r"Plan draft open in the editor: `([^`]+)`", prompt).group(1)
with open(log, "a") as f:
    f.write(json.dumps({{"id": mid, "prompt": prompt, "t": time.time()}}) + "\n")
def out(o):
    print(json.dumps(o), flush=True)
out({{"type": "system", "subtype": "init", "session_id": "sess-c", "model": "fake"}})
text = re.search(r'^"(.*)"$', prompt, re.M).group(1)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
if "ambiguous" in text:
    reply = "Needs you: which of the two takes do you mean?"
else:
    for k in range(2):                                   # two separate saves = two watcher steps
        d = json.load(open(draft))
        d.setdefault("notes", []).append(mid + "-" + str(k))
        json.dump(d, open(draft, "w"))
        time.sleep(1.3)
    reply = "Trimmed it, see [[t:1.2]]."
    if "via cli" in text:
        subprocess.run([sys.executable, "-m", "cutroom.cli", "comments", "done", mid, "--reply", "CLI: " + reply], check=True)
out({{"type": "stream_event", "event": {{"type": "content_block_delta", "index": 0, "delta": {{"type": "text_delta", "text": "Looking at it.\n\n" + reply}}}}}})
out({{"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-c", "total_cost_usd": 0,
      "duration_ms": 10, "num_turns": 1, "result": reply}})
'''


def _stub(tmp_path, monkeypatch, sleep=0.0):
    stub = tmp_path / "claude"
    stub.write_text(FAKE_AGENT.format(py=sys.executable))
    stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", str(stub))
    monkeypatch.setenv("CUTROOM_AGENT", "claude")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SLEEP", str(sleep))
    return log


def _wait_idle(S, timeout=90):
    t_end = time.time() + timeout
    while time.time() < t_end:
        code, st = req(S, "GET", "/api/comments/run")
        if not st["running"]:
            return st
        time.sleep(0.3)
    raise AssertionError("comment run did not finish")


def test_comment_run_one_by_one(lc, tmp_path, monkeypatch):
    S = lc
    log = _stub(tmp_path, monkeypatch)
    req(S, "POST", "/api/plan/new", {"title": "Review"})
    req(S, "POST", "/api/plan/review/op", {"op": "insert", "at_t": 0,
                                           "segment": {"type": "broll", "clip": S["cid"], "in": 0, "out": 4}})
    ids = {}
    for t, text in ((2.5, "fix this via cli"), (0.5, "too long, trim the start"), (1.5, "ambiguous: use the other one")):
        code, m = req(S, "POST", "/api/markers", {"plan": "review", "t": t, "text": text,
                                                  "drawing": [{"tool": "box", "points": [[.1, .1], [.4, .4]]}]})
        ids[t] = m["id"]
    code, h0 = req(S, "GET", "/api/plan/review")
    pos0 = h0["history"]["pos"]
    lis = SSEListener(S)
    try:
        code, st = req(S, "POST", "/api/comments/run", {"plan": "review", "all_open": True})
        assert code == 200 and st["running"] and st["total"] == 3, st
        code, again = req(S, "POST", "/api/comments/run", {"plan": "review", "all_open": True})
        assert code == 409
        seen = []
        while len([s for s in seen if s[0] == "result"]) < 3:
            ev, d = lis.wait(lambda e, x: e == "comments" and x.get("type") in ("working", "result"), timeout=60)
            seen.append((d["type"], d["id"], d.get("status")))
    finally:
        lis.close()
    st = _wait_idle(S)
    order = [ids[0.5], ids[1.5], ids[2.5]]
    assert [x[1] for x in seen if x[0] == "working"] == order               # timeline order, one at a time
    assert [x[0] for x in seen] == ["working", "result"] * 3                 # never two in flight
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert [c["id"] for c in calls] == order
    p0 = calls[0]["prompt"]
    assert "Review comment 1 of 3" in p0 and "too long, trim the start" in p0 and "drew a box" in p0
    assert "Look at these images" in p0 and "/studio/grabs/comment_" in p0       # the annotated frame
    assert "Open review comments" not in p0                                     # just this comment
    ms = {m["id"]: m for m in req(S, "GET", "/api/markers?plan=review")[1]["markers"]}
    assert ms[ids[0.5]]["status"] == "done" and "[[t:1.2]]" in ms[ids[0.5]]["thread"][-1]["text"]
    assert ms[ids[0.5]]["thread"][-1]["author"] == "ai"
    assert ms[ids[1.5]]["status"] == "needs_you" and ms[ids[1.5]]["thread"][-1]["text"].startswith("Needs you:")
    assert ms[ids[2.5]]["status"] == "done" and ms[ids[2.5]]["thread"][-1]["text"].startswith("CLI:")
    assert len(ms[ids[2.5]]["thread"]) == 1                                     # CLI reply not doubled
    assert [r["status"] for r in st["results"]] == ["done", "needs_you", "done"]
    # one undo step per comment: two comments edited the draft (two saves each)
    code, h1 = req(S, "GET", "/api/plan/review")
    assert h1["history"]["pos"] == pos0 + 2, (pos0, h1["history"])
    assert h1["draft"]["notes"] == [f"{ids[0.5]}-0", f"{ids[0.5]}-1", f"{ids[2.5]}-0", f"{ids[2.5]}-1"]
    code, u = req(S, "POST", "/api/plan/review/undo")
    assert u["draft"]["notes"] == [f"{ids[0.5]}-0", f"{ids[0.5]}-1"]               # only the last comment's change
    req(S, "POST", "/api/plan/review/redo")


def test_comment_run_stop_after_current(lc, tmp_path, monkeypatch):
    S = lc
    log = _stub(tmp_path, monkeypatch, sleep=1.0)
    req(S, "POST", "/api/plan/new", {"title": "Stopper"})
    req(S, "POST", "/api/plan/stopper/op", {"op": "insert", "at_t": 0,
                                            "segment": {"type": "broll", "clip": S["cid"], "in": 0, "out": 4}})
    ids = [req(S, "POST", "/api/markers", {"plan": "stopper", "t": t, "text": f"note {t}"})[1]["id"] for t in (0.5, 1.0, 1.5)]
    code, st = req(S, "POST", "/api/comments/run", {"plan": "stopper", "ids": ids})
    assert code == 200
    t_end = time.time() + 20
    while time.time() < t_end and not log.exists():
        time.sleep(0.05)
    code, s2 = req(S, "POST", "/api/comments/stop")
    assert s2["stopping"] is True
    st = _wait_idle(S)
    assert [r["id"] for r in st["results"]] == [ids[0]] and st["queue"] == ids[1:]
    ms = {m["id"]: m for m in req(S, "GET", "/api/markers?plan=stopper")[1]["markers"]}
    assert ms[ids[0]]["status"] == "done" and ms[ids[1]]["status"] == "open" and ms[ids[2]]["status"] == "open"
    assert len(log.read_text().splitlines()) == 1


def test_comments_cli(lc, monkeypatch, capsys):
    S = lc
    code, m = req(S, "POST", "/api/markers", {"plan": "review", "t": 3.0, "text": "cli test"})
    from cutroom import cli
    monkeypatch.setenv("CUTROOM_WORK", S["cfg"].work)
    cli.main(["comments", "needs", m["id"], "--reply", "which one?"])
    cli.main(["markers", "list", "--plan", "review", "--all"])
    out = capsys.readouterr().out
    assert "needs_you" in out
    mm = next(x for x in req(S, "GET", "/api/markers?plan=review")[1]["markers"] if x["id"] == m["id"])
    assert mm["status"] == "needs_you" and mm["thread"][-1]["text"] == "Needs you: which one?"
    cli.main(["comments", "done", m["id"], "--reply", "Done [[t:3]]"])
    mm = next(x for x in req(S, "GET", "/api/markers?plan=review")[1]["markers"] if x["id"] == m["id"])
    assert mm["status"] == "done" and len(mm["thread"]) == 2
