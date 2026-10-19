"""Cutroom Studio backend: HTTP API, plan ops (timeline -> source mapping through tightened talk),
undo/redo/commit, markers, SSE, the Claude bridge (against a fake `claude`) and a render job.
Runs the real server on a random port over a small synthetic library."""
import http.client
import json
import queue
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from conftest import make_library

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
# talk clip = "steady" (8 s): two phrases with a 2 s pause that tightening removes
PHRASES = [(0.5, 2.5, "this is the first part of it"), (4.5, 7.5, "and here is the second part okay")]


def _transcript():
    segs = []
    for t0, t1, text in PHRASES:
        ws = text.split()
        step = (t1 - t0) / len(ws)
        segs.append({"start": t0, "end": t1, "text": text,
                     "words": [{"start": round(t0 + i * step, 3), "end": round(t0 + (i + 0.85) * step, 3),
                                "word": " " + w, "probability": 0.99} for i, w in enumerate(ws)]})
    return {"language": "en", "model": "hand-written", "segments": segs}


@pytest.fixture(scope="module")
def studio_srv(tmp_path_factory):
    from cutroom import db, ingest, music, studio
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("studio")
    lib = make_library(base / "footage")
    (base / "songs").mkdir()
    subprocess.run(FF + ["-f", "lavfi", "-i", "aevalsrc='sin(2*PI*1000*t)*exp(-60*mod(t,0.5))':d=20:s=44100",
                         "-c:a", "pcm_s16le", str(base / "songs" / "click120.wav")], check=True)
    cfg = init_project(base / "work", [base / "footage"], [base / "songs"])
    cfg.workers = 2
    cfg.out_width, cfg.out_height, cfg.preview_height = 640, 360, 180
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    ingest.make_sheets(cfg, con)
    music.index_songs(cfg, con)
    ids = {k: db.clip_id(v) for k, v in lib.items()}
    tr = _transcript()
    cfg.p("transcripts", f"{ids['steady']}.json").write_text(json.dumps(tr))
    db.upsert(con, "transcripts", {"clip_id": ids["steady"], "path": "", "language": "en", "n_words": 14,
                                   "speech_s": 5.0, "text": " ".join(p[2] for p in PHRASES), "model": "hand",
                                   "updated": time.time()}, key=("clip_id",))
    con.commit()
    srv, st = studio.make_server(cfg, 0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    yield {"cfg": cfg, "con": con, "ids": ids, "port": srv.server_address[1], "studio": st, "base": base}
    st.close()
    srv.shutdown()
    srv.server_close()


def req(S, method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", S["port"], timeout=120)
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    if raw:
        return r.status, dict(r.getheaders()), payload
    try:
        return r.status, json.loads(payload or b"null")
    except ValueError:
        return r.status, payload


def sse_events(S, method, path, body=None, until=None, timeout=60):
    """Read an SSE stream; returns [(event, data)] until `until(event, data)` or the stream ends."""
    c = http.client.HTTPConnection("127.0.0.1", S["port"], timeout=timeout)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 200, r.read()
    out, ev, data = [], None, []
    t_end = time.time() + timeout
    while time.time() < t_end:
        line = r.fp.readline()
        if not line:
            break
        line = line.decode().rstrip("\n")
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "" and data:
            d = json.loads("\n".join(data))
            out.append((ev, d))
            if until and until(ev, d):
                break
            ev, data = None, []
    c.close()
    return out


class SSEListener:
    """Background reader of /api/events."""

    def __init__(self, S):
        self.q = queue.Queue()
        self.c = http.client.HTTPConnection("127.0.0.1", S["port"], timeout=120)
        self.c.request("GET", "/api/events")
        self.r = self.c.getresponse()
        assert self.r.status == 200
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        ev, data = None, []
        try:
            while True:
                line = self.r.fp.readline()
                if not line:
                    return
                line = line.decode().rstrip("\n")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line == "" and data:
                    self.q.put((ev, json.loads("\n".join(data))))
                    ev, data = None, []
        except Exception:  # noqa: BLE001
            return

    def wait(self, pred, timeout=30):
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                ev, d = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if pred(ev, d):
                return ev, d
        raise AssertionError("event not seen")

    def close(self):
        try:
            self.c.sock.shutdown(2)
        except Exception:  # noqa: BLE001
            pass
        self.c.close()


# ---------------------------------------------------------------------------------------------- library
def test_project_clips_clip(studio_srv):
    S = studio_srv
    code, p = req(S, "GET", "/api/project")
    assert code == 200 and p["status"]["clips"] == 5 and len(p["songs"]) == 1
    code, r = req(S, "GET", "/api/clips?limit=2")
    assert code == 200 and r["total"] == 5 and len(r["rows"]) == 2 and r["rows"][0]["thumb"].startswith("/media/thumb/")
    code, r2 = req(S, "GET", "/api/clips?limit=2&offset=2")
    assert {x["id"] for x in r["rows"]}.isdisjoint({x["id"] for x in r2["rows"]})
    code, r = req(S, "GET", "/api/clips?q=scenes")
    assert [x["id"] for x in r["rows"]] == [S["ids"]["scenes"]]
    code, r = req(S, "GET", "/api/clips?q=second+part&speech=1")
    assert [x["id"] for x in r["rows"]] == [S["ids"]["steady"]] and r["rows"][0]["hit_t"] is not None
    sid = S["ids"]["steady"]
    code, c = req(S, "GET", f"/api/clip/{sid}")
    assert code == 200 and len(c["words"]) == 14 and c["proxy_url"] and c["width"] == 1920
    code, t = req(S, "PUT", f"/api/clip/{sid}/tags", {"setting": "kitchen", "quality": 4})
    assert code == 200 and t["tags"]["setting"] == "kitchen"
    code, r = req(S, "GET", "/api/clips?setting=kitch&min_quality=3")
    assert [x["id"] for x in r["rows"]] == [sid]
    code, e = req(S, "GET", "/api/clip/nope-nope")
    assert code == 404 and "error" in e


def test_media_range_thumb(studio_srv):
    S = studio_srv
    sid = S["ids"]["steady"]
    code, h, body = req(S, "GET", f"/media/proxy/{sid}", headers={"Range": "bytes=0-99"}, raw=True)
    assert code == 206 and len(body) == 100 and h["Content-Range"].startswith("bytes 0-99/")
    size = int(h["Content-Range"].split("/")[1])
    code, h, body = req(S, "GET", f"/media/proxy/{sid}", headers={"Range": f"bytes={size + 10}-"}, raw=True)
    assert code == 416
    code, _, _ = req(S, "GET", "/media/render/../cutroom.json", raw=True)
    assert code == 404
    code, _, _ = req(S, "GET", "/media/render/%2e%2e/cutroom.json", raw=True)
    assert code == 404
    code, h, body = req(S, "GET", f"/media/thumb/{sid}?t=2.0", raw=True)
    assert code == 200 and body[:2] == b"\xff\xd8" and h["Content-Type"] == "image/jpeg"
    assert list(S["cfg"].p("frames", "thumbs").glob(f"{sid}_*.jpg"))
    code, h, body = req(S, "GET", f"/media/frame/{sid}?t=3.0&w=640", raw=True)
    assert code == 200 and body[:2] == b"\xff\xd8"
    code, _, body = req(S, "GET", f"/media/sheet/{sid}", raw=True)
    assert code == 200 and body[:2] == b"\xff\xd8"
    code, _, body = req(S, "GET", "/", raw=True)
    assert code == 200


# ---------------------------------------------------------------------------------------------- plans
def _v1(tl):
    return [p for p in tl["pieces"] if p["track"] == "V1"]


def test_plan_ops_undo_commit(studio_srv):
    S = studio_srv
    ids = S["ids"]
    code, d = req(S, "POST", "/api/plan/new", {"title": "Studio Test"})
    assert code == 200 and d["slug"] == "studio-test"
    slug = d["slug"]
    op = lambda body: req(S, "POST", f"/api/plan/{slug}/op", body)      # noqa: E731
    code, d = op({"op": "insert", "at_t": 0, "segment": {"type": "talk", "clip": ids["steady"], "in": 0.2, "out": 7.9}})
    assert code == 200, d
    code, d = op({"op": "insert", "at_t": 999, "segment": {"type": "broll", "clip": ids["shaky"], "in": 1.0, "out": 3.0}})
    assert code == 200, d
    tl = d["timeline"]
    talk = [p for p in _v1(tl) if p["type"] == "talk"]
    assert len(talk) == 2, "tighten removes the 2 s pause"
    tight = sum(p["end"] - p["start"] for p in talk)
    assert 5.0 < tight < 7.0 and abs(tl["duration"] - (tight + 2.0)) < 0.05
    assert talk[1]["in"] > talk[0]["out"] + 1.5                       # the gap is a jump in source time
    assert d["errors"] == [] and d["history"]["can_undo"]
    # split inside the SECOND tightened piece: the source time must skip the removed pause
    p2 = talk[1]
    t = round(p2["start"] + 1.0, 3)
    expect = p2["in"] + (t - p2["start"])
    code, d = op({"op": "split", "t": t})
    assert code == 200, d
    segs = d["draft"]["sections"][0]["segments"]
    assert len(segs) == 3 and segs[0]["type"] == segs[1]["type"] == "talk"
    assert abs(segs[0]["out"] - expect) < 1e-3 and abs(segs[1]["in"] - expect) < 1e-3
    assert abs(segs[0]["in"] - 0.2) < 1e-6 and abs(segs[1]["out"] - 7.9) < 1e-6
    rows = d["timeline"]["segments"]
    assert abs(rows[0]["end"] - t) < 0.15                             # the cut lands where it was made
    assert abs(d["timeline"]["duration"] - tl["duration"]) < 0.3
    # trim the b-roll out point by 0.5 s
    dur0 = d["timeline"]["duration"]
    code, d = op({"op": "trim", "seg": [0, 2], "edge": "out", "delta": -0.5})
    assert code == 200 and abs(d["draft"]["sections"][0]["segments"][2]["out"] - 2.5) < 1e-6
    assert abs(d["timeline"]["duration"] - (dur0 - 0.5)) < 0.05
    # move the b-roll to the front, then ripple-delete it
    code, d = op({"op": "move", "seg": [0, 2], "to": [0, 0]})
    assert code == 200 and d["draft"]["sections"][0]["segments"][0]["type"] == "broll"
    assert _v1(d["timeline"])[0]["clip"] == ids["shaky"] and _v1(d["timeline"])[0]["start"] == 0
    dur1 = d["timeline"]["duration"]
    code, d = op({"op": "delete", "seg": [0, 0], "ripple": True})
    assert code == 200 and len(d["draft"]["sections"][0]["segments"]) == 2
    assert abs(d["timeline"]["duration"] - (dur1 - 1.5)) < 0.05
    # lift (no ripple) keeps the length
    code, d2 = op({"op": "delete", "seg": [0, 1], "ripple": False})
    assert code == 200 and d2["draft"]["sections"][0]["segments"][1]["type"] == "black"
    assert abs(d2["timeline"]["duration"] - d["timeline"]["duration"]) < 0.05
    # undo / redo
    code, u = req(S, "POST", f"/api/plan/{slug}/undo")
    assert code == 200 and u["draft"]["sections"][0]["segments"][1]["type"] == "talk"
    code, u = req(S, "POST", f"/api/plan/{slug}/undo")
    assert code == 200 and u["draft"]["sections"][0]["segments"][0]["type"] == "broll"
    code, r = req(S, "POST", f"/api/plan/{slug}/redo")
    assert code == 200 and len(r["draft"]["sections"][0]["segments"]) == 2
    # freeze frame at a time inside the first talk segment: still of that source frame, segment split
    tf = round(r["timeline"]["segments"][0]["start"] + 0.8, 3)
    loc_src = next(p for p in _v1(r["timeline"]) if p["start"] <= tf < p["end"])
    expect_src = loc_src["in"] + (tf - loc_src["start"])
    code, f = op({"op": "freeze", "t": tf, "dur": 1.0})
    assert code == 200, f
    segs = f["draft"]["sections"][0]["segments"]
    still = segs[1]
    assert still["type"] == "still" and abs(still["t"] - expect_src) < 1e-3 and still["clip"] == ids["steady"]
    assert abs(f["timeline"]["duration"] - (r["timeline"]["duration"] + 1.0)) < 0.2
    # overlay + cutaway placed by timeline time on talk -> source-relative "at"
    talk_row = f["timeline"]["segments"][2]
    code, o = op({"op": "add_overlay", "seg": [0, 2], "overlay": {"kind": "callout", "text": "hi", "at_t": talk_row["start"] + 0.5}})
    assert code == 200 and o["draft"]["sections"][0]["segments"][2]["overlays"][0]["at"] >= 0
    assert any(p["track"] == "V3" for p in o["timeline"]["pieces"])
    code, o = op({"op": "add_cutaway", "seg": [0, 2], "cutaway": {"clip": ids["dark"], "in": 1.0, "at_t": talk_row["start"] + 0.2, "dur": 0.8}})
    assert code == 200 and any(p["track"] == "V2" for p in o["timeline"]["pieces"])
    code, o = op({"op": "add_section", "name": "end", "music": {"song": "click120", "offset": 6.0}})
    assert code == 200 and len(o["draft"]["sections"]) == 2
    code, o = op({"op": "insert", "at_t": 999, "segment": {"type": "black", "dur": 2.0}})
    assert code == 200 and o["draft"]["sections"][1]["segments"] == [{"type": "black", "dur": 2.0}]
    band = o["timeline"]["sections"][1]
    assert band["music"] and len(band["music"]["beats_in_section"]) >= 3
    # validation: an op that breaks the plan is refused; a forced PUT saves and reports
    code, e = op({"op": "insert", "at_t": 0, "segment": {"type": "broll", "clip": "nope.mp4", "in": 0, "out": 1}})
    assert code == 422 and any("nope.mp4" in x for x in e["errors"])
    bad = json.loads(json.dumps(o["draft"]))
    bad["sections"][0]["segments"].append({"type": "talk", "clip": "nope.mp4", "in": 0, "out": 2})
    code, e = req(S, "PUT", f"/api/plan/{slug}", {"plan": bad})
    assert code == 422 and e["errors"]
    code, e = req(S, "PUT", f"/api/plan/{slug}", {"plan": bad, "force": True})
    assert code == 200 and e["errors"] and e["timeline"]["duration"] > 0
    assert any(p["kind"] == "invalid" for p in _v1(e["timeline"]))
    code, c = req(S, "POST", f"/api/plan/{slug}/commit")
    assert code == 422
    code, u = req(S, "POST", f"/api/plan/{slug}/undo")
    assert code == 200 and not u["errors"]
    code, c = req(S, "POST", f"/api/plan/{slug}/commit")
    assert code == 200 and c["name"] == "studio-test_v1.json"
    assert S["cfg"].p("plans", "studio-test_v1.json").exists()
    code, c = req(S, "POST", f"/api/plan/{slug}/commit")
    assert c["name"] == "studio-test_v2.json"
    code, g = req(S, "GET", f"/api/plan/{slug}")
    assert [v["file"] for v in g["version_files"]] == ["studio-test_v1.json", "studio-test_v2.json"]
    code, p = req(S, "GET", "/api/project")
    assert any(x["slug"] == slug and x["draft"] for x in p["plans"])


def test_resolve_is_fast_and_matches_split_math(studio_srv):
    from cutroom import render
    S = studio_srv
    plan = {"title": "speed", "sections": [{"name": "a", "segments": [
        {"type": "talk", "clip": S["ids"]["steady"], "in": 0.2, "out": 7.9} for _ in range(30)]}]}
    t0 = time.time()
    tl = render.resolve_timeline(S["cfg"], S["con"], plan)
    first = time.time() - t0
    t0 = time.time()
    render.resolve_timeline(S["cfg"], S["con"], plan)
    assert time.time() - t0 < 0.05 and first < 2.0
    loc = render.locate(tl, 10.0)
    p = loc["piece"]
    assert abs(loc["src_t"] - (p["in"] + 10.0 - p["start"])) < 1e-3
    back = render.segment_src_to_timeline(tl, loc["sec"], loc["seg"], loc["src_t"])
    assert abs(back - 10.0) < 0.05


# ---------------------------------------------------------------------------------------------- markers
def test_markers_crud(studio_srv, monkeypatch):
    S = studio_srv
    req(S, "POST", "/api/plan/new", {"title": "Mk"})
    req(S, "POST", "/api/plan/mk/op", {"op": "insert", "at_t": 0,
                                       "segment": {"type": "broll", "clip": S["ids"]["steady"], "in": 1, "out": 4}})
    code, m = req(S, "POST", "/api/markers", {"plan": "mk", "t": 1.5, "text": "too dark here"})
    assert code == 200 and m["status"] == "open" and m["frame_url"], m
    code, _, body = req(S, "GET", m["frame_url"], raw=True)
    assert code == 200 and body[:2] == b"\xff\xd8"
    code, ms = req(S, "GET", "/api/markers?plan=mk")
    assert [x["id"] for x in ms["markers"]] == [m["id"]]
    code, m2 = req(S, "PATCH", f"/api/markers/{m['id']}", {"text": "much too dark"})
    assert code == 200 and m2["text"] == "much too dark"
    # the CLI marks it done (what Claude runs after fixing it)
    from cutroom import cli
    monkeypatch.setenv("CUTROOM_WORK", S["cfg"].work)
    cli.main(["markers", "done", m["id"]])
    code, ms = req(S, "GET", "/api/markers?plan=mk")
    assert ms["markers"][0]["status"] == "done"
    code, _ = req(S, "DELETE", f"/api/markers/{m['id']}")
    assert code == 200
    code, ms = req(S, "GET", "/api/markers?plan=mk")
    assert ms["markers"] == []
    code, g = req(S, "POST", "/api/grab", {"source": "clip", "clip": S["ids"]["dark"], "t": 2.0})
    assert code == 200 and Path(g["path"]).exists() and g["url"].startswith("/media/grab/")
    code, cx = req(S, "POST", "/api/context", {"plan": "mk", "playhead": 1.0})
    ctx = json.loads(S["cfg"].p("studio", "context.json").read_text())
    assert ctx["draft_path"].endswith("plans/mk.draft.json")


# ---------------------------------------------------------------------------------------------- SSE
def test_sse_plan_changed_external(studio_srv):
    S = studio_srv
    req(S, "POST", "/api/plan/new", {"title": "Ext"})
    lis = SSEListener(S)
    try:
        lis.wait(lambda e, d: e == "hello", timeout=10)
        time.sleep(1.2)                          # let the watcher take its baseline
        p = S["cfg"].p("plans", "ext.draft.json")
        d = json.loads(p.read_text())
        d["title"] = "Edited by Claude"
        p.write_text(json.dumps(d))              # an external edit, like Claude's Edit tool
        ev, data = lis.wait(lambda e, x: e == "plan_changed" and x["file"] == "ext.draft.json"
                            and x["source"] == "external", timeout=10)
        assert data["slug"] == "ext" and data["kind"] == "draft" and not data["deleted"]
        # the external edit became an undo step
        code, u = req(S, "POST", "/api/plan/ext/undo")
        assert code == 200 and u["draft"]["title"] == "Ext"
    finally:
        lis.close()


# ---------------------------------------------------------------------------------------------- chat bridge
FAKE_CLAUDE = r'''#!{py}
import json, sys, os
prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}}) + "\n")
lines = [
  {{"type": "system", "subtype": "init", "session_id": "sess-123", "model": "fake"}},
  {{"type": "stream_event", "event": {{"type": "content_block_delta", "index": 0, "delta": {{"type": "text_delta", "text": "Five "}}}}}},
  {{"type": "stream_event", "event": {{"type": "content_block_delta", "index": 0, "delta": {{"type": "text_delta", "text": "clips."}}}}}},
  {{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Five clips."}}]}}}},
  {{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {{"command": "cutroom status"}}}}]}}}},
  {{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu1", "content": "clips 5"}}]}}}},
  {{"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-123", "total_cost_usd": 0.01,
    "duration_ms": 1234, "num_turns": 2, "result": "Five clips."}},
]
for l in lines:
    print(json.dumps(l), flush=True)
'''


def test_chat_bridge_fake_claude(studio_srv, tmp_path, monkeypatch):
    S = studio_srv
    stub = tmp_path / "claude"
    stub.write_text(FAKE_CLAUDE.format(py=sys.executable))
    stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", str(stub))
    monkeypatch.setenv("CUTROOM_AGENT", "claude")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    req(S, "POST", "/api/chat/new")
    code, g = req(S, "POST", "/api/grab", {"source": "clip", "clip": S["ids"]["steady"], "t": 1.0})
    req(S, "POST", "/api/plan/new", {"title": "Chat"})
    req(S, "POST", "/api/plan/chat/op", {"op": "insert", "at_t": 0,
                                         "segment": {"type": "talk", "clip": S["ids"]["steady"], "in": 0.2, "out": 7.9}})
    evs = sse_events(S, "POST", "/api/chat", {"message": "what is here?", "attachments": [g["url"]],
                                             "context": {"plan": "chat", "playhead": 1.0}},
                     until=lambda e, d: d.get("type") in ("done", "error"))
    types = [d["type"] for _, d in evs]
    assert "session" in types and "tool" in types and "tool_result" in types and types[-1] == "done", types
    assert "".join(d["text"] for _, d in evs if d["type"] == "text") == "Five clips."
    tool = next(d for _, d in evs if d["type"] == "tool")
    assert tool["name"] == "Bash" and tool["input"] == "cutroom status"
    assert evs[-1][1]["session_id"] == "sess-123"
    assert json.loads(S["cfg"].p("studio", "chat.json").read_text())["session_id"] == "sess-123"
    call = json.loads(log.read_text().splitlines()[0])
    assert "--resume" not in call["argv"] and "--permission-mode" in call["argv"] and "acceptEdits" in call["argv"]
    assert "Bash(cutroom:*)" in call["argv"] and not any("dangerously" in a for a in call["argv"])
    assert "## What I'm pointing at" in call["prompt"] and "chat.draft.json" in call["prompt"]
    assert "segment [0, 0]" in call["prompt"] and g["path"] in call["prompt"] and "Look at these images" in call["prompt"]
    assert Path(call["cwd"]).resolve() == Path(S["cfg"].work).resolve()
    # second turn resumes the session
    sse_events(S, "POST", "/api/chat", {"message": "and now?"}, until=lambda e, d: d.get("type") in ("done", "error"))
    call2 = json.loads(log.read_text().splitlines()[1])
    i = call2["argv"].index("--resume")
    assert call2["argv"][i + 1] == "sess-123"
    code, h = req(S, "GET", "/api/chat/history")
    assert [m["role"] for m in h["messages"]][-4:] == ["user", "assistant", "user", "assistant"]
    assert h["messages"][-1]["text"] == "Five clips." and h["session_id"] == "sess-123"
    # project CLAUDE.md imports the repo's
    md = Path(S["cfg"].work, "CLAUDE.md").read_text()
    assert "@" + str(Path(__file__).resolve().parents[1] / "CLAUDE.md") in md and "Studio mode" in md
    # ... and AGENTS.md tells Codex to follow it, with the studio sections inlined verbatim
    ag = Path(S["cfg"].work, "AGENTS.md").read_text()
    repo_md = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text()
    assert f"Read `{Path(__file__).resolve().parents[1] / 'CLAUDE.md'}`" in ag and "SKILL.md" in ag
    assert "ARCHITECTURE.md" in ag and "extending-cutroom" in ag
    for heading in ("## Studio mode", "### Talking in the studio: clips, not code"):
        assert heading in ag
    sec = repo_md[repo_md.index("## Studio mode"):]
    assert sec.strip() in ag            # verbatim (Studio mode is the last section of CLAUDE.md)
    # missing binary -> install steps
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", str(tmp_path / "nope"))
    evs = sse_events(S, "POST", "/api/chat", {"message": "hi"}, until=lambda e, d: True)
    assert evs[0][1]["type"] == "error" and "install" in evs[0][1]


FAKE_CODEX = r"""#!{py}
import json, os, sys, time
argv = sys.argv[1:]
log = os.environ.get("FAKE_CODEX_LOG")
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
if argv[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read() if argv and argv[-1] == "-" else ""
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": argv, "prompt": prompt, "cwd": os.getcwd()}}) + "\n")
def out(o):
    print(json.dumps(o), flush=True)
if mode == "gone" and "resume" in argv:
    sys.stderr.write("Error: no rollout found for thread id " + argv[argv.index("resume") + 1] + "\n"); sys.exit(1)
out({{"type": "thread.started", "thread_id": "thr-777"}})
out({{"type": "turn.started"}})
if mode == "fail":
    for i in (1, 2, 3):
        out({{"type": "error", "message": f"Reconnecting... {{i}}/5 (unexpected status 401 Unauthorized: Missing bearer)"}})
    out({{"type": "error", "message": "unexpected status 401 Unauthorized: Missing bearer or basic authentication"}})
    out({{"type": "turn.failed", "error": {{"message": "unexpected status 401 Unauthorized: Missing bearer or basic authentication"}}}})
    sys.exit(1)
out({{"type": "item.completed", "item": {{"id": "item_0", "type": "reasoning", "text": "**Checking status**"}}}})
out({{"type": "item.started", "item": {{"id": "item_1", "type": "command_execution", "command": "bash -lc 'cutroom status'",
     "aggregated_output": "", "exit_code": None, "status": "in_progress"}}}})
out({{"type": "item.completed", "item": {{"id": "item_1", "type": "command_execution", "command": "bash -lc 'cutroom status'",
     "aggregated_output": "clips 5\n", "exit_code": 0, "status": "completed"}}}})
out({{"type": "item.started", "item": {{"id": "item_2", "type": "file_change", "changes": [{{"path": os.getcwd() + "/plans/chat.draft.json", "kind": "update"}}], "status": "in_progress"}}}})
out({{"type": "item.completed", "item": {{"id": "item_2", "type": "file_change", "changes": [{{"path": os.getcwd() + "/plans/chat.draft.json", "kind": "update"}}], "status": "completed"}}}})
out({{"type": "item.some_future_event", "item": {{"id": "x"}}}})
out({{"type": "item.completed", "item": {{"id": "item_9", "type": "todo_list", "items": []}}}})
out({{"type": "item.updated", "item": {{"id": "item_3", "type": "agent_message", "text": "Five"}}}})
out({{"type": "item.updated", "item": {{"id": "item_3", "type": "agent_message", "text": "Five clips"}}}})
out({{"type": "item.completed", "item": {{"id": "item_3", "type": "agent_message", "text": "Five clips."}}}})
out({{"type": "turn.completed", "usage": {{"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 40}}}})
"""


def _codex_stub(tmp_path, monkeypatch, mode="ok"):
    stub = tmp_path / "codex"
    stub.write_text(FAKE_CODEX.format(py=sys.executable))
    stub.chmod(0o755)
    log = tmp_path / "codex_calls.jsonl"
    monkeypatch.setenv("CUTROOM_CODEX_BIN", str(stub))
    monkeypatch.setenv("CUTROOM_AGENT", "codex")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    return log


def _chat(S, msg, **kw):
    return sse_events(S, "POST", "/api/chat", {"message": msg, **kw}, until=lambda e, d: d.get("type") in ("done", "error"))


def test_chat_bridge_fake_codex(studio_srv, tmp_path, monkeypatch):
    S = studio_srv
    log = _codex_stub(tmp_path, monkeypatch)
    req(S, "POST", "/api/chat/new")
    code, g = req(S, "POST", "/api/grab", {"source": "clip", "clip": S["ids"]["steady"], "t": 2.0})
    req(S, "POST", "/api/plan/new", {"title": "Chat"})
    evs = _chat(S, "what is here?", attachments=[g["url"]], context={"plan": "chat", "playhead": 1.0})
    types = [d["type"] for _, d in evs]
    assert types[0] == "session" and evs[0][1]["session_id"] == "thr-777"
    assert types[-1] == "done" and "error" not in types, types
    assert "".join(d["text"] for _, d in evs if d["type"] == "text") == "Five clips."      # deltas, no duplication
    assert [d["text"] for _, d in evs if d["type"] == "text"] == ["Five", " clips", "."]
    tools = [d for _, d in evs if d["type"] == "tool"]
    assert [(t["name"], t["input"]) for t in tools] == [("Bash", "bash -lc 'cutroom status'"), ("Edit", "chat.draft.json")]
    res = [d for _, d in evs if d["type"] == "tool_result"]
    assert len(res) == 2 and res[0]["id"] == "item_1" and res[0]["ok"] and "exit 0" in res[0]["text"]
    assert types.index("tool") < types.index("tool_result") < types.index("text")
    done = evs[-1][1]
    assert done["session_id"] == "thr-777" and done["usage"]["output_tokens"] == 40
    st = json.loads(S["cfg"].p("studio", "chat.json").read_text())
    assert st["session_id"] == "thr-777" and st["agent"] == "codex"
    call = json.loads(log.read_text().splitlines()[0])
    a = call["argv"]
    assert a[0] == "exec" and "resume" not in a and a[-1] == "-" and "--json" in a and "--skip-git-repo-check" in a
    assert a[a.index("-s") + 1] == "workspace-write" and Path(a[a.index("-C") + 1]).resolve() == Path(S["cfg"].work).resolve()
    assert Path(a[a.index("--add-dir") + 1]).resolve() == Path(__file__).resolve().parents[1]
    assert a[a.index("-i") + 1] == g["path"] and not any("dangerously" in x for x in a)
    assert "## What I'm pointing at" in call["prompt"] and Path(call["cwd"]).resolve() == Path(S["cfg"].work).resolve()
    # second turn resumes the thread: exec ... resume <id> ... -
    evs = _chat(S, "and now?")
    assert evs[-1][1]["type"] == "done"
    a2 = json.loads(log.read_text().splitlines()[1])["argv"]
    i = a2.index("resume")
    assert a2[0] == "exec" and a2[i + 1] == "thr-777" and a2[-1] == "-" and "--json" in a2[i:]
    code, h = req(S, "GET", "/api/chat/history")
    assert h["messages"][-1]["text"] == "Five clips." and h["messages"][-1]["agent"] == "codex"
    assert h["messages"][-1]["tools"][0] == {"name": "Bash", "input": "bash -lc 'cutroom status'"}
    # a Claude session id is never handed to Codex
    S["studio"].chat.set_session("claude-sess", "claude")
    _chat(S, "fresh?")
    a3 = json.loads(log.read_text().splitlines()[2])["argv"]
    assert "resume" not in a3


def test_chat_bridge_codex_failure_and_gone_session(studio_srv, tmp_path, monkeypatch):
    S = studio_srv
    log = _codex_stub(tmp_path, monkeypatch, mode="fail")
    req(S, "POST", "/api/chat/new")
    evs = _chat(S, "hi")
    types = [d["type"] for _, d in evs]
    assert types.count("error") == 1 and types[-1] == "error" and "done" not in types, types
    assert "401" in evs[-1][1]["error"] and "Reconnecting" not in evs[-1][1]["error"]
    assert types.count("status") == 3                         # retries surface as status lines only
    # a resumed thread that no longer exists: start a new one and answer anyway
    monkeypatch.setenv("FAKE_CODEX_MODE", "gone")
    S["studio"].chat.set_session("thr-old", "codex")
    evs = _chat(S, "again")
    assert evs[-1][1]["type"] == "done" and any(d["type"] == "warning" for _, d in evs)
    calls = [json.loads(x)["argv"] for x in log.read_text().splitlines()]
    assert "resume" in calls[-2] and "resume" not in calls[-1]
    req(S, "POST", "/api/chat/new")


def test_agent_api_switch(studio_srv, tmp_path, monkeypatch):
    S = studio_srv
    _codex_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("CUTROOM_AGENT")
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", str(tmp_path / "no-claude"))
    code, p = req(S, "GET", "/api/project")
    assert code == 200 and p["agent"]["name"] == "codex" and p["agent"]["logged_in"] is True
    assert p["agent"]["available"] == ["codex"]
    code, a = req(S, "POST", "/api/agent", {"name": "claude"})
    assert code == 200 and a["name"] == "claude" and a["setting"] == "claude"
    assert json.loads(S["cfg"].p("cutroom.json").read_text())["agent"] == "claude"
    evs = _chat(S, "hi")                                     # claude chosen but missing -> install steps
    assert evs[0][1]["type"] == "error" and "install" in evs[0][1] and evs[0][1]["agent"] == "claude"
    code, e = req(S, "POST", "/api/agent", {"name": "gpt"})
    assert code == 400
    code, a = req(S, "POST", "/api/agent", {"name": "auto"})
    assert code == 200 and a["name"] == "codex"


# ---------------------------------------------------------------------------------------------- jobs
def test_render_job(studio_srv):
    S = studio_srv
    req(S, "POST", "/api/plan/new", {"title": "Tiny"})
    req(S, "POST", "/api/plan/tiny/op", {"op": "insert", "at_t": 0,
                                         "segment": {"type": "broll", "clip": S["ids"]["steady"], "in": 1.0, "out": 2.0}})
    req(S, "POST", "/api/plan/tiny/op", {"op": "insert", "at_t": 99, "segment": {"type": "black", "dur": 0.4}})
    code, c = req(S, "POST", "/api/plan/tiny/commit")
    assert code == 200
    code, e = req(S, "POST", "/api/render", {"plan_file": "plans/tiny.draft.json", "preview": True})
    assert code == 409
    lis = SSEListener(S)
    try:
        code, j = req(S, "POST", "/api/render", {"plan_file": f"plans/{c['name']}", "preview": True})
        assert code == 200, j
        ev, d = lis.wait(lambda e, x: e == "render_done" and x["job"] == j["job"], timeout=150)
    finally:
        lis.close()
    assert d["file"] == "tiny_v1_preview.mp4"
    code, job = req(S, "GET", f"/api/jobs/{j['job']}")
    assert job["status"] == "done" and job["progress"] == 1.0 and any("progress" in x for x in job["log_tail"])
    code, h, body = req(S, "GET", d["url"], headers={"Range": "bytes=0-9"}, raw=True)
    assert code == 206 and len(body) == 10
    code, jobs = req(S, "GET", "/api/jobs")
    assert any(x["id"] == j["job"] for x in jobs["jobs"])
    code, gv = req(S, "GET", "/api/governor")
    assert code == 200 and "available" in gv
    code, ov = req(S, "POST", "/api/overnight", {"action": "status"})
    assert code == 200 and ov["available"] and ov["running"] is False
    code, ov = req(S, "POST", "/api/overnight", {"action": "bogus"})
    assert code == 400


# ================================================================================ real-run UI fixes
def test_saved_state_after_factory_release(studio_srv):
    """The real run showed "Draft — unsaved changes since v5" while the draft was v3: factory versions are
    written next to the draft and saved_state compared the draft with the NEWEST file. The draft's base is
    the version it equals (own number first); newer versions are listed, never made the base."""
    S = studio_srv
    code, d = req(S, "POST", "/api/plan/new", {"title": "Saved State"})
    slug = d["slug"]
    req(S, "POST", f"/api/plan/{slug}/op", {"op": "insert", "at_t": 0,
                                           "segment": {"type": "broll", "clip": S["ids"]["steady"], "in": 1.0, "out": 2.0}})
    for _ in range(3):
        code, c = req(S, "POST", f"/api/plan/{slug}/commit")
        assert code == 200, c
    assert c["version"] == 3
    # the factory publishes v4 and v5 (different content), the draft is untouched
    v3 = json.loads(S["cfg"].p("plans", f"{slug}_v3.json").read_text())
    for n in (4, 5):
        fv = json.loads(json.dumps(v3))
        fv["version"] = n
        fv["sections"][0]["segments"][0]["out"] = 2.0 + 0.1 * n
        S["cfg"].p("plans", f"{slug}_v{n}.json").write_text(json.dumps(fv))
    code, g = req(S, "GET", f"/api/plan/{slug}")
    s = g["saved"]
    assert s["version"] == 3 and s["latest"] == f"{slug}_v3.json" and s["dirty"] is False
    assert s["newest_version"] == 5 and [x["version"] for x in s["newer"]] == [4, 5]
    # an edit makes it dirty against v3 (not v5)
    code, e = req(S, "POST", f"/api/plan/{slug}/op", {"op": "insert", "at_t": 99, "segment": {"type": "black", "dur": 0.4}})
    assert e["saved"]["dirty"] is True and e["saved"]["version"] == 3
    # a draft that equals a factory version is "saved as" that version, whatever its own number says
    same = json.loads(S["cfg"].p("plans", f"{slug}_v5.json").read_text())
    same["version"] = 3
    code, e = req(S, "PUT", f"/api/plan/{slug}", {"plan": same})
    assert code == 200 and e["saved"]["version"] == 5 and e["saved"]["dirty"] is False and e["saved"]["newer"] == []


def test_commit_refuses_stale_draft_and_respects_reservations(studio_srv):
    """Save version never writes a draft the page has not seen (expect = draft_sha) and never takes a version
    number a running factory order reserved."""
    from cutroom.factory import bus as fbus
    S = studio_srv
    code, d = req(S, "POST", "/api/plan/new", {"title": "Stale Save"})
    slug = d["slug"]
    code, d = req(S, "POST", f"/api/plan/{slug}/op", {"op": "insert", "at_t": 0,
                                                     "segment": {"type": "broll", "clip": S["ids"]["steady"], "in": 1.0, "out": 2.0}})
    seen = d["draft_sha"]
    assert seen and len(seen) == 16
    # the draft changes on disk behind the page's back (another tab, Claude, Open in draft)
    p = S["cfg"].p("plans", f"{slug}.draft.json")
    dd = json.loads(p.read_text())
    dd["title"] = "changed elsewhere"
    p.write_text(json.dumps(dd))
    code, e = req(S, "POST", f"/api/plan/{slug}/commit", {"expect": seen})
    assert code == 409 and "changed since" in e["error"]
    assert not S["cfg"].p("plans", f"{slug}_v1.json").exists()
    code, g = req(S, "GET", f"/api/plan/{slug}")
    assert g["draft_sha"] != seen and g["draft"]["title"] == "changed elsewhere"
    # a running order reserved v1: the hand save takes v2
    assert fbus.reserve_version(S["cfg"], slug) == 1
    code, c = req(S, "POST", f"/api/plan/{slug}/commit", {"expect": g["draft_sha"]})
    assert code == 200 and c["version"] == 2, c
    assert json.loads(S["cfg"].p("plans", f"{slug}_v2.json").read_text())["title"] == "changed elsewhere"
    assert fbus.reserve_version(S["cfg"], slug) == 3                     # and the factory skips the hand one


def test_factory_lessons_delete_supersede_confirm_routes(studio_srv, tmp_path, monkeypatch):
    from cutroom.factory import lessons, lint
    S = studio_srv
    monkeypatch.setenv("CUTROOM_GLOBAL_DIR", str(tmp_path / "global"))
    cfg = S["cfg"]
    bad = lessons.add_lesson(cfg, "never add a film look", check={"type": "qa_metric",
                                                                  "params": {"check": "devices_vs_reference", "worst": "warn"}})
    good = lessons.add_lesson(cfg, "keep held shots under titles fresh")
    newer = lessons.add_lesson(cfg, "keep held shots fresh within 10 s")
    code, r = req(S, "GET", "/api/factory/lessons")
    row = next(x for x in r["lessons"] if x["id"] == bad["id"])
    assert row["provenance"]["kind"] == "operator" and row["check"]
    # delete: the lesson leaves the list and its learned check retires
    code, d = req(S, "POST", f"/api/factory/lessons/{bad['id']}/delete", {"reason": "taught by a QA bug"})
    assert code == 200 and d["status"] == "deleted" and d["deleted_why"] == "taught by a QA bug"
    code, r = req(S, "GET", "/api/factory/lessons")
    assert bad["id"] not in {x["id"] for x in r["lessons"]} and bad["id"] in {x["id"] for x in r["deleted"]}
    assert row["check"] not in {c["id"] for c in lint.load_checks(cfg)}
    raw = json.loads(lint.checks_path(cfg, "project").read_text())["checks"]
    assert next(c for c in raw if c["id"] == row["check"])["status"] == "retired"
    assert bad["id"] not in lessons.brief(cfg)["rules"]
    # supersede, naming the replacement; self / unknown refused
    code, e = req(S, "POST", f"/api/factory/lessons/{good['id']}/supersede", {"by": good["id"]})
    assert code == 400
    code, e = req(S, "POST", "/api/factory/lessons/P9999/supersede", {})
    assert code == 404
    code, s = req(S, "POST", f"/api/factory/lessons/{good['id']}/supersede", {"by": newer["id"], "reason": "narrower"})
    assert code == 200 and s["status"] == "superseded" and s["superseded_by"] == newer["id"]
    assert good["id"] not in lessons.brief(cfg)["rules"] and newer["id"] in lessons.brief(cfg)["rules"]
    code, e = req(S, "POST", f"/api/factory/lessons/{newer['id']}/delete", {"scope": "nowhere"})
    assert code == 400
    # the old PATCH path still works (and takes a reason)
    code, e = req(S, "PATCH", "/api/factory/lessons", {"id": newer["id"], "status": "superseded", "reason": "x"})
    assert code == 200 and e["status"] == "superseded"
    # a machine finding is unconfirmed until the creator confirms it; dismiss keeps it out for good
    rows = lessons.harvest(cfg, [{"source": "qa:o1", "kind": "machine_fail", "text": "film not measured on the reference"},
                                 {"source": "critic:o1", "kind": "critic_hard", "text": "title sits on the shirt"}])
    code, r = req(S, "GET", "/api/factory/lessons")
    assert {x["id"] for x in r["unconfirmed"]} >= {x["id"] for x in rows}
    code, c = req(S, "POST", f"/api/factory/inbox/{rows[1]['id']}/confirm", {})
    assert code == 200 and c["status"] == "open" and c["provenance"]["confirmed"] is True
    code, x = req(S, "POST", f"/api/factory/inbox/{rows[0]['id']}/dismiss", {"reason": "QA bug"})
    assert code == 200 and x["status"] == "triaged" and x["dismissed"]
    code, r = req(S, "GET", "/api/factory/lessons")
    assert rows[0]["id"] not in {x["id"] for x in r["unconfirmed"]} and rows[1]["id"] in {x["id"] for x in r["inbox"]}


def test_team_rows_show_short_paths():
    from cutroom.studio import role_rows_for_ui, short_paths
    w = "/data/proj/work"
    out = role_rows_for_ui([{"role": "editor-1", "last": f'StructuredOutput {{"candidate": "{w}/factory/orders/o1/candidate.json"}}'},
                            {"role": "critic-1", "last": f"Read {w}/renders/x_v5_preview.mp4", "error": None},
                            {"role": "fix-1", "last": "Bash cutroom frames C5628 --at 18.2", "error": f"no file {w}/plans/a.json"}], w)
    assert out[0]["last"] == "returned its result" and out[0]["last_full"].startswith("StructuredOutput")
    assert out[1]["last"] == "Read renders/x_v5_preview.mp4" and out[1]["last_full"].endswith(".mp4")
    assert out[2]["last"] == "Bash cutroom frames C5628 --at 18.2" and "last_full" not in out[2]
    assert out[2]["error"] == "no file plans/a.json"
    assert short_paths("open /opt/a/b/c/d/e.json now", w) == "open …/d/e.json now"
    home = str(Path.home())
    assert short_paths(f"{home}/x.json", w) == "~/x.json"
    assert "/" not in short_paths(f"{w}", w).strip(".")


def test_question_options_not_double_numbered_or_cut_mid_word():
    from cutroom.factory import card
    real = {"note": "n2", "question": "The cold-open title can't sit over the trees without a code change (typed titles are "
            "always centred), and every other clip is already used within 10s. How should I handle it?",
            "options": ["1. Add an x/y position option for typed titles in the renderer (a code change), then put DAY THREE "
                        "over the left-side trees", "2. Raise the title into the upper frame on the same shot (also needs the "
                        "y pass-through)", "3. Give the title a dark backing or outline so it reads on the shirt",
                        "4. Leave it as it is"]}
    q = card.clean_questions([real])[0]
    assert all(not o[:1].isdigit() for o in q["options"])
    assert " = 1." not in q["line"] and "1 = Add an x/y" in q["line"]
    assert len(q["line"]) <= card.QMAX
    words = set(" ".join(real["options"] + [real["question"]]).replace("(", " ").replace(")", " ").split())
    for piece in q["options"] + [q["text"]]:
        last = piece.rstrip("…").split()[-1].strip(",;:(")
        assert last in words or last.rstrip(",.") in words, f"cut mid-word: {piece!r}"
    assert len(q["text"]) >= 100                                   # the question keeps its sense
    assert q["options_full"][0].startswith("Add an x/y") and q["text_full"].endswith("handle it?")
    again = card.clean_questions([q])[0]                            # cleaning twice keeps the full words
    assert again["options_full"] == q["options_full"] and again["line"] == q["line"]
    assert card.strip_number("1.5 s gap") == "1.5 s gap" and card.strip_number("-6 dB") == "-6 dB"
    assert card.strip_number("(3) Leave it") == "Leave it" and card.strip_number("2 = keep") == "keep"
