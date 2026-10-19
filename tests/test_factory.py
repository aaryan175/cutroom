"""The factory (docs/FACTORY.md): bus, run_role against fake agents, the governor's agent slot, intake mapping,
lock check, lint, lessons, the runner end to end (fake agent, real render on synthetic footage), studio routes.
No real agent is ever called: CUTROOM_CLAUDE_BIN / CUTROOM_CODEX_BIN point at tests/fixtures/fake_agent.py."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("CUTROOM_GLOBAL_DIR", tempfile.mkdtemp(prefix="cutroom-test-global-"))

from cutroom import agent  # noqa: E402
from cutroom.config import Config  # noqa: E402
from cutroom.factory import brain, bus, card, fconfig, lessons, lint, lockcheck, mapnotes  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def fake_bin(tmp_path, name="claude"):
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\nexec {sys.executable} {FIX / 'fake_agent.py'} \"$@\"\n")
    p.chmod(0o755)
    return p


def script(tmp_path, d, name="script.json"):
    p = tmp_path / name
    p.write_text(json.dumps(d))
    return p


@pytest.fixture()
def cfg(tmp_path):
    c = Config(work=str(tmp_path / "work"))
    (tmp_path / "work" / "plans").mkdir(parents=True)
    return c


# ============================================================================================== step 1: bus
def test_bus_events_fold_and_closed_states(cfg):
    o = bus.create(cfg, "revise", "trip", comments=["m1"], text="hi")
    assert bus.ID_RE.match(o["id"]) and bus.load_order(cfg, o["id"])["kind"] == "revise"
    with pytest.raises(bus.BusError):
        bus.append(cfg, o["id"], "finished")                      # not in the closed set
    with pytest.raises(bus.BusError):
        bus.append(cfg, o["id"], "blocked")                       # needs a why
    bus.append(cfg, o["id"], "picked_up", phase="intake")
    bus.append(cfg, o["id"], "progress", phase="edit", text="editing", cost_total=0.12)
    st = bus.status(cfg, o["id"])
    assert st["state"] == "running" and st["phase"] == "edit" and st["cost_usd"] == 0.12 and st["picked"]
    bus.append(cfg, o["id"], "needs_you", why="budget", questions=[{"id": "q1"}])
    st = bus.status(cfg, o["id"])
    assert st["state"] == "needs_you" and st["why"] == "budget" and st["questions"] == [{"id": "q1"}]
    ev = bus.events(cfg, o["id"])
    assert all(e["at"].endswith("Z") and e["t"] for e in ev)       # stamped by code
    with pytest.raises(bus.BusError):
        bus.create(cfg, "publish", "trip")
    # order.json is written once
    assert json.loads((bus.order_dir(cfg, o["id"]) / "order.json").read_text())["comments"] == ["m1"]


def test_bus_debounce_folds_one_burst(cfg):
    a = bus.submit(cfg, "revise", "trip", comments=["m1"], debounce_s=90)
    b = bus.submit(cfg, "revise", "trip", comments=["m2", "m3"], debounce_s=90)
    c = bus.submit(cfg, "revise", "other", comments=["x"], debounce_s=90)
    assert b["folded_into"] == a["order"]["id"] and "folded_into" not in c
    ids, _ = bus.order_comments(cfg, a["order"]["id"])
    assert ids == ["m1", "m2", "m3"]
    st = bus.status(cfg, b["folded"])
    assert st["state"] == "picked_up" and st["folded_into"] == a["order"]["id"]
    sa = bus.status(cfg, a["order"]["id"])
    assert sa["state"] == "queued" and sa["not_before"]           # waits for the window to close
    # once picked up, a new note queues as the NEXT order (never folded into a running one)
    bus.append(cfg, a["order"]["id"], "picked_up")
    d = bus.submit(cfg, "revise", "trip", comments=["m4"], debounce_s=90)
    assert "folded_into" not in d
    # a reject is not debounced
    r = bus.submit(cfg, "reject", "trip", text="no", debounce_s=90)
    assert not bus.status(cfg, r["order"]["id"])["not_before"]


def test_bus_version_reservation_never_collides(cfg):
    pd = Path(cfg.work) / "plans"
    (pd / "trip_v3.json").write_text("{}")
    (pd / "trip_v10.json").write_text("{}")
    got = []
    th = [threading.Thread(target=lambda: got.append(bus.reserve_version(cfg, "trip"))) for _ in range(6)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert sorted(got) == list(range(11, 17))
    assert bus.reserve_version(cfg, "other") == 1
    assert bus.latest_version(cfg, "trip") == ("trip_v10.json", 10)


def test_config_defaults_and_override(cfg):
    fc = fconfig.load(cfg)
    assert fc["debounce_s"] == 90 and fc["budgets"]["cost_usd"] == 5.0 and fc["auto_apply_extensions"] is False
    fc = fconfig.save(cfg, {"budgets": {"cost_usd": 2.5}, "models": {"editor": "opus"}})
    assert fc["budgets"]["cost_usd"] == 2.5 and fc["budgets"]["role_runs"] == 6 and fc["models"]["editor"] == "opus"


# ============================================================================================== step 2: run_role
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"], "properties": {"summary": {"type": "string"}}}


@pytest.mark.parametrize("which", ["claude", "codex"])
def test_run_role_modes(tmp_path, monkeypatch, which):
    b = fake_bin(tmp_path, which)
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN" if which == "claude" else "CUTROOM_CODEX_BIN", str(b))
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    sc = script(tmp_path, {"critic": [{"result": {"summary": "fine"}, "cost": 0.07}, {"mode": "refuse"}, {"mode": "limit"},
                                      {"mode": "quote_limit"}, {"mode": "noresult"}, {"mode": "timeout"}]})
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", str(sc))
    ag = agent.get(which)
    run = lambda **kw: agent.run_role("critic", "look", agent=ag, tools=["Read", "Bash(cutroom frames:*)"],  # noqa: E731
                                      schema=SCHEMA, cwd=tmp_path, out_dir=tmp_path / f"r{len(list(tmp_path.glob('r*')))}", **kw)
    r = run(timeout=30)
    assert r["ok"] and r["result_json"]["summary"] == "fine", r
    if which == "claude":
        assert r["cost_usd"] == 0.07
        a = json.loads(log.read_text().splitlines()[0])["argv"]
        assert "--json-schema" in a and "--no-session-persistence" in a and a[a.index("--tools") + 1] == "Bash"
        assert "Bash(cutroom frames:*)" in a and "--max-budget-usd" not in a
    else:
        assert r["cost_usd"] > 0 and r["usage"]["output_tokens"] == 300
        a = json.loads(log.read_text().splitlines()[0])["argv"]
        assert a[0] == "exec" and "--output-schema" in a and "-o" in a and a[a.index("-s") + 1] == "read-only"
    assert Path(r["stream_path"]).exists() and (Path(r["stream_path"]).parent / "result.json").exists()
    r = run(timeout=30)
    assert r["refused"] and not r["ok"] and not r["limited"]
    r = run(timeout=30)
    assert r["limited"] and not r["refused"]
    r = run(timeout=30)                                           # the MODEL quoting "usage limit" is not a limit
    assert r["ok"] and not r["limited"] and "Usage limit" in r["text"]
    r = run(timeout=30)
    assert not r["ok"] and r["error"] and not r["refused"] and not r["limited"]
    t0 = time.time()
    r = run(timeout=3)
    assert r["timeout"] and time.time() - t0 < 15
    pid = int((Path(r["stream_path"]).parent / "pid").read_text())
    time.sleep(0.5)
    from cutroom.governor import pid_alive, _zombie
    assert not pid_alive(pid) or _zombie(pid)


def test_run_role_stop_and_env(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, "claude")
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", str(b))
    sc = script(tmp_path, {"editor": {"mode": "timeout"}})
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", str(sc))
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    ev = threading.Event()
    threading.Timer(1.0, ev.set).start()
    r = agent.run_role("editor", "x", agent=agent.get("claude"), tools=["Read"], schema=SCHEMA, cwd=tmp_path,
                       out_dir=tmp_path / "r", timeout=60, stop_check=ev.is_set)
    assert r["stopped"] and not r["ok"]
    assert json.loads(log.read_text().splitlines()[0])["role"] == "editor"     # CUTROOM_FACTORY_ROLE was set


def test_classify_only_harness_lines():
    assert agent.classify_failure("Invalid API key · Please run /login") == "refused"
    assert agent.classify_failure("Your organization has disabled Claude subscription access for Claude Code") == "refused"
    assert agent.classify_failure("Claude AI usage limit reached|123") == "limited"
    assert agent.classify_failure("API Error: 529 overloaded") == "limited"
    assert agent.classify_failure("render failed") is None


def test_cli_refuses_heavy_commands_inside_a_role(tmp_path):
    env = dict(os.environ, CUTROOM_FACTORY_ROLE="editor", CUTROOM_WORK=str(tmp_path))
    for cmd in (["render", "x.json"], ["transcribe"], ["export", "x.mp4"]):
        r = subprocess.run([sys.executable, "-m", "cutroom.cli", *cmd], capture_output=True, text=True, env=env,
                           cwd=str(Path(__file__).resolve().parents[1]))
        assert r.returncode == 2 and "factory" in (r.stderr + r.stdout).lower(), (cmd, r.stderr)


# ============================================================================================== step 3: governor agent slot
def test_governor_agent_slot_and_watch(tmp_path, monkeypatch):
    from cutroom import governor
    monkeypatch.setenv("CUTROOM_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("CUTROOM_AGENT_SLOTS", "1")
    g = governor.Governor(Config(work=str(tmp_path)))
    assert g.agent_slots == 1 and g.limits()["agent_slots"] == 1 and "agent" in governor.KINDS
    with g.try_slot("agent", label="a") as ok1:
        assert ok1
        got = []
        th = threading.Thread(target=lambda: got.append(g.try_slot("agent").__enter__()))
        th.start()
        th.join()
        assert got == [False]                                       # another thread: busy, answers at once
        assert g.busy_slots("agent")[0]["label"] == "a"
        assert any(s["kind"] == "agent" and s["busy"] for s in g.status()["slots"])
    with g.try_slot("agent") as ok2:
        assert ok2
    # the agent slot holds even with the governor disabled (like ASR)
    monkeypatch.setenv("CUTROOM_GOVERNOR", "0")
    g2 = governor.Governor(Config(work=str(tmp_path)))
    with g2.try_slot("agent") as a:
        res = []
        th = threading.Thread(target=lambda: res.append(g2.try_slot("agent").__enter__()))
        th.start()
        th.join()
        assert a and res == [False]
    monkeypatch.delenv("CUTROOM_GOVERNOR")
    # memory cap: an agent process that grows past agent_mem_cap_gb is stopped (its whole group)
    monkeypatch.setenv("CUTROOM_AGENT_MEM_CAP_GB", "0.15")
    g3 = governor.Governor(Config(work=str(tmp_path)))
    p = subprocess.Popen([sys.executable, "-c", "import time; x = bytearray(400 * 1024 * 1024); time.sleep(30)"],
                         start_new_session=True)
    with g3.watching(p, ["python"], kind="agent") as w:
        p.wait(timeout=25)
    assert w["killed"] and "memory" in w["killed"]


def test_governor_emergency_stops_agents_first(tmp_path, monkeypatch):
    from cutroom import governor
    monkeypatch.setenv("CUTROOM_LOCK_DIR", str(tmp_path / "locks"))
    g = governor.Governor(Config(work=str(tmp_path)))
    big = subprocess.Popen([sys.executable, "-c", "import time; x = bytearray(150*1024*1024); time.sleep(30)"])
    ag = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    time.sleep(1.0)
    g._register(big.pid, ["ffmpeg"], 4.0, kind="heavy")
    g._register(ag.pid, ["claude"], 3.0, kind="agent")
    monkeypatch.setattr(governor, "free_ram", lambda max_age=1.5: 0)
    try:
        why = g.emergency_check()
        assert why and ag.wait(timeout=10) is not None
        assert big.poll() is None                                   # the bigger non-agent child survives
    finally:
        for p in (big, ag):
            p.kill()
        g._unregister(big.pid)
        g._unregister(ag.pid)


# ============================================================================================== a small real project
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


@pytest.fixture(autouse=True)
def _factory_gates_clear(request):
    """Tests share one module project (fproj): a test that fails between pause() and resume() must not leave
    PAUSED / backoff.json behind for the next one (every later order would sit queued until its timeout)."""
    yield
    if "fproj" in request.fixturenames:
        from cutroom.factory import runner as R
        R.resume(request.getfixturevalue("fproj")["cfg"])


@pytest.fixture(scope="module")
def fproj(tmp_path_factory):
    from conftest import make_library
    from cutroom import db, ingest
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("factory")
    lib = make_library(base / "footage")
    cfg = init_project(base / "work", [base / "footage"])
    cfg.workers = 2
    cfg.out_width, cfg.out_height, cfg.preview_height = 640, 360, 180
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    ingest.make_sheets(cfg, con)
    ids = {k: db.clip_id(v) for k, v in lib.items()}
    cfg.p("transcripts", f"{ids['steady']}.json").write_text(json.dumps(_transcript()))
    db.upsert(con, "transcripts", {"clip_id": ids["steady"], "path": "", "language": "en", "n_words": 14, "speech_s": 5.0,
                                   "text": " ".join(p[2] for p in PHRASES), "model": "hand", "updated": time.time()},
              key=("clip_id",))
    con.commit()
    return {"cfg": cfg, "con": con, "ids": ids, "base": base}


def base_plan(ids, title="Trip"):
    return {"title": title, "version": 1, "style": {"captions": {"enabled": False}},
            "sections": [{"name": "morning", "segments": [
                {"type": "talk", "clip": ids["steady"], "in": 0.2, "out": 7.9},
                {"type": "broll", "clip": ids["scenes"], "in": 0.5, "out": 2.5},
                {"type": "broll", "clip": ids["dark"], "in": 1.0, "out": 2.5}]},
                {"name": "evening", "segments": [
                    {"type": "black", "dur": 0.4},
                    {"type": "broll", "clip": ids["scenes"], "in": 4.0, "out": 6.0}]}]}


def _marker(cfg, plan, t, text, t_end=None, mid=None):
    from cutroom import studio
    m = {"id": mid or f"m{int(t * 1000)}", "plan": plan, "t": t, "text": text, "status": "open", "kind": "note",
         "author": "you", "thread": [], "created": time.time()}
    if t_end is not None:
        m["t_end"] = t_end
    studio.markers_update(cfg, lambda d: d.append(m))
    return m


# ============================================================================================== step 4: intake + brain
def test_mapnotes_pieces_words_flags_supersede(fproj):
    from cutroom.render import resolve_timeline
    cfg, con, ids = fproj["cfg"], fproj["con"], fproj["ids"]
    plan = base_plan(ids)
    tl = resolve_timeline(cfg, con, plan)
    seg = {(r["sec"], r["seg"]): r for r in tl["segments"]}
    cut = seg[(0, 1)]["start"]                                   # talk -> first b-roll
    m1 = {"id": "c1", "t": cut - 0.3, "text": "remove this, I don't need it"}
    n1 = mapnotes.map_comment(cfg, con, tl, plan, m1, 1)
    assert n1["segments"][:2] == ["0.0", "0.1"] and "remove" in n1["flags"]          # window covers 2 pieces
    talk = next(p for p in n1["pieces"] if p["ref"] == "0.0" and p["track"] == "V1")
    assert talk["src_in"] < talk["src_out"] and talk["words"]                         # source span + its words
    assert n1["window"] == [round(m1["t"] - 1, 3), round(m1["t"] + 1, 3)]
    m2 = {"id": "c2", "t": 0.3, "t_end": seg[(1, 1)]["start"] + 0.2, "text": "it was better before"}
    n2 = mapnotes.map_comment(cfg, con, tl, plan, m2, 2)
    assert set(n2["segments"]) >= {"0.0", "0.1", "0.2", "1.0", "1.1"} and "restore" in n2["flags"]
    assert mapnotes.flags_of("I like this edit") == ["praise"]
    assert "praise" not in mapnotes.flags_of("this is not good")
    assert "conditional_approval" in mapnotes.flags_of("approve but change the last clip")
    # a note on a segment with an active ruling supersedes it (the newest note wins)
    k = brain.seg_key(plan["sections"][0]["segments"][1])
    d = brain.add_decision(cfg, "trip", "b-roll after the talk", "keep the scenes shot", "comment", segments=[k])
    n3 = mapnotes.map_comment(cfg, con, tl, plan, {"id": "c3", "t": seg[(0, 1)]["start"] + 0.5, "text": "swap it"}, 3,
                              decisions=brain.decisions(cfg, "trip"))
    assert n3["supersedes"] == d["id"]
    t = mapnotes.text_note("make the intro faster", 4)
    assert t["t"] is None and t["segments"] == []


def test_brain_decisions_locks_grades(fproj, tmp_path):
    cfg = Config(work=str(tmp_path))
    a = brain.add_decision(cfg, "p", "music under talk", "on", "comment")
    b = brain.add_decision(cfg, "p", "Music under talk", "no music under talk", "comment")
    c = brain.add_decision(cfg, "p", "callouts", "off", "verdict")
    brain.add_decision(cfg, "p", "callouts again", "on", "answer", supersedes=c["id"])
    act = {r["id"] for r in brain.decisions(cfg, "p")}
    assert b["id"] in act and a["id"] not in act and c["id"] not in act        # newest wins; superseded hidden
    assert len(brain.decisions(cfg, "p", active_only=False)) == 4
    brain.add_locks(cfg, "p", ["k1", "k2"], why="praised")
    assert set(brain.locks(cfg, "p")) == {"k1", "k2"}
    brain.add_grade(cfg, "C1", 1, 2, grade="BROLL")
    brain.add_grade(cfg, "C1", 1, 2, grade=None, ident="NOTME")
    g = brain.grades(cfg)
    assert g == [{"clip": "C1", "t0": 1.0, "t1": 2.0, "grade": "BROLL", "ident": "NOTME"}]
    with pytest.raises(ValueError):
        brain.add_grade(cfg, "C1", grade="MEH")
    brain.write_file(cfg, "p", "STORY.md", "# beats\n")
    assert brain.read_file(cfg, "p", "STORY.md").startswith("# beats")
    with pytest.raises(ValueError):
        brain.write_file(cfg, "p", "ref_pairs.json", {"x": 1})


# ============================================================================================== step 5: machine QA
def test_plan_lock_rules(fproj):
    ids = fproj["ids"]
    base = base_plan(ids)
    cand = json.loads(json.dumps(base))
    cand["sections"][0]["segments"].pop(1)                       # delete 0.1
    assert lockcheck.plan_lock(base, cand, allowed_refs={"0.1"})["status"] == "pass"
    r = lockcheck.plan_lock(base, cand, allowed_refs={"0.2"})
    assert r["status"] == "fail" and "0.1" in r["issues"][0]["text"]
    ins = json.loads(json.dumps(base))
    ins["sections"][1]["segments"].insert(1, {"type": "black", "dur": 0.3})
    assert lockcheck.plan_lock(base, ins, allowed_refs={"1.0"})["status"] == "pass"     # next to a noted segment
    assert lockcheck.plan_lock(base, ins, allowed_refs={"0.0"})["status"] == "fail"     # far from any
    sty = json.loads(json.dumps(base))
    sty["style"]["music_under_speech"] = "duck"
    assert lockcheck.plan_lock(base, sty)["status"] == "fail"
    assert lockcheck.plan_lock(base, sty, allowed_global=True)["status"] == "pass"
    key = brain.seg_key(base["sections"][0]["segments"][1])
    r = lockcheck.plan_lock(base, cand, allowed_refs={"0.1"}, locked_keys={key}, allowed_locked_refs=set())
    assert r["status"] == "fail" and "locked" in r["issues"][0]["text"]                  # praised: hands off
    same = lockcheck.plan_lock(base, json.loads(json.dumps(base)))
    assert same["status"] == "pass" and len(same["pairs"]) == 5


def test_lint_checks(fproj, tmp_path):
    from cutroom.render import resolve_timeline
    cfg, con, ids = fproj["cfg"], fproj["con"], fproj["ids"]
    plan = base_plan(ids)
    tl = resolve_timeline(cfg, con, plan)
    c2 = Config(work=str(tmp_path))
    checks = [{"id": "C1", "type": "banned_clip", "params": {"clip": ids["dark"]}},
              {"id": "C2", "type": "banned_span", "params": {"clip": ids["scenes"], "t0": 10, "t1": 11}},
              {"id": "C3", "type": "max_count", "params": {"kind": "overlay", "max": 0}},
              {"id": "C4", "type": "no_music_under_speech", "params": {}},
              {"id": "C5", "type": "qa_metric", "params": {"check": "black", "worst": "pass"}}]
    res = {r["id"]: r for r in lint.run(c2, plan, tl, {"summary": {"checks": {"black": "warn"}}}, con=con, checks=checks)}
    assert res["C1"]["status"] == "fail" and res["C1"]["t"] is not None
    assert res["C2"]["status"] == "pass" and res["C3"]["status"] == "pass" and res["C4"]["status"] == "pass"
    assert res["C5"]["status"] == "fail"
    assert lint.validate_check({"type": "banned_span", "params": {"clip": "x"}})
    lint.add_check(c2, {"type": "banned_clip", "params": {"clip": ids["dark"]}, "lesson": "P0001"})
    brain.add_grade(c2, ids["scenes"], 0, 1, grade="NEVER")
    res2 = lint.run(c2, plan, tl, None, con=con)
    assert any(r["status"] == "fail" and r["lesson"] == "P0001" for r in res2)
    assert any(r["id"].startswith("grade:") and r["status"] == "fail" for r in res2)


def test_cards_and_questions():
    qs = card.clean_questions([{"text": "Which shot? " * 60, "options": ["the wide one", "the close one (recommended)"]},
                               {"text": "Keep it?", "options": []}])
    assert all(len(q["line"]) <= 320 for q in qs) and qs[1]["options"] == ["yes", "no"]
    assert card.question_problem({"text": "x", "options": ["a"]})
    assert card.question_problem({"text": "ok?", "options": ["a", "b"]}) is None
    assert card.theme(24, [{"change": "Removed the walk at 7:19."}, {"change": "Swapped the 8:23 shot"}]).startswith(
        "v24: removed the walk at 7:19; swapped")


# ============================================================================================== step 6: runner, S path
def use_fake(tmp_path, monkeypatch, scen=None, which="claude"):
    b = fake_bin(tmp_path, which)
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN" if which == "claude" else "CUTROOM_CODEX_BIN", str(b))
    monkeypatch.setenv("CUTROOM_AGENT", which)
    monkeypatch.setenv("CUTROOM_FACTORY_AGENT", which)
    monkeypatch.setenv("CUTROOM_FACTORY_DEBOUNCE_S", "0")
    log = tmp_path / "agent_calls.jsonl"
    log.unlink(missing_ok=True)
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    import shutil
    shutil.rmtree(tmp_path / "script.json.calls", ignore_errors=True)
    sc = script(tmp_path, scen or {})
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", str(sc))
    agent.clear_cache()
    return log, sc


def calls(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def drain(r, timeout=600):
    """Tick the runner until nothing is queued or running (a test-speed loop instead of the 2 s tick)."""
    from cutroom.factory import runner as R
    t_end = time.time() + timeout
    while time.time() < t_end:
        r.tick()
        if r.driver is not None:
            r.driver.join(timeout=max(1, t_end - time.time()))
            continue
        busy = [o for o in R.list_view(r.cfg) if o["state"] in ("queued", "picked_up", "running") and not o["folded_into"]
                and not (o["not_before"] and bus.iso_to_t(o["not_before"]) > time.time())]
        if not busy:
            return
        time.sleep(0.2)
    raise AssertionError("runner did not settle")


def render_base(cfg, plan_file):
    from cutroom.factory import gates
    r = subprocess.run(gates.cli("render", plan_file, "--preview"), cwd=cfg.work, env=gates.sub_env(cfg),
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def new_plan(cfg, ids, slug, title=None, render=True, plan=None):
    pf = Path(cfg.work) / "plans" / f"{slug}_v1.json"
    pf.write_text(json.dumps(plan or base_plan(ids, title or slug)))
    if render:
        render_base(cfg, pf)
    return pf


def test_runner_s_path_end_to_end(fproj, tmp_path, monkeypatch):
    from cutroom import studio
    from cutroom.factory import runner as R
    from cutroom.render import resolve_timeline
    cfg, con, ids = fproj["cfg"], fproj["con"], fproj["ids"]
    log, _ = use_fake(tmp_path, monkeypatch, {"editor": {"edits": [{"op": "delete", "sec": 0, "seg": 2}]}})
    new_plan(cfg, ids, "trip", "trip")
    tl = resolve_timeline(cfg, con, base_plan(ids, "trip"))
    s02 = next(r for r in tl["segments"] if (r["sec"], r["seg"]) == (0, 2))
    m = _marker(cfg, "trip", round((s02["start"] + s02["end"]) / 2, 2), "remove this dark clip", mid="cm1")
    res = R.submit(cfg, "revise", "trip", comment_ids=["cm1"])
    oid = res["order"]["id"]
    assert studio.markers_load(cfg)[-1]["factory"]["order"] == oid           # the "in factory" pill
    r = R.FactoryRunner(cfg)
    assert r.acquire()
    try:
        drain(r)
    finally:
        r.release()
    d = R.order_detail(cfg, oid)
    st = d["status"]
    assert st["state"] == "ready", (st, (bus.order_dir(cfg, oid) / "error.txt").read_text()
                                     if (bus.order_dir(cfg, oid) / "error.txt").exists() else d["card"])
    assert st["version"] == 2
    v2 = json.loads((Path(cfg.work) / "plans" / "trip_v2.json").read_text())
    assert v2["version"] == 2 and len(v2["sections"][0]["segments"]) == 2 and v2["sections"][1] == base_plan(ids, "trip")["sections"][1]
    card_ = d["card"]
    assert card_["passed"] and card_["rows"][0]["note"] == "n1" and card_["rows"][0]["chip"].startswith("[[t:")
    assert any("lock check" in c for c in card_["checked"]) and any("render lock" in c for c in card_["checked"])
    assert (Path(cfg.work) / "renders" / "trip_v2_preview.mp4").exists()
    assert (Path(cfg.work) / "renders" / "trip_v2_preview.changed.jpg").exists()
    assert (Path(cfg.work) / "renders" / "trip_v2_preview.card.json").exists()
    vm = json.loads((Path(cfg.work) / "plans" / ".history" / "trip" / "versions.json").read_text())
    assert vm["trip_v2.json"]["by"] == "factory" and vm["trip_v2.json"]["order"] == oid
    mk = next(x for x in studio.markers_load(cfg) if x["id"] == "cm1")
    assert mk["status"] == "done" and "[[t:" in mk["thread"][-1]["text"] and mk["thread"][-1]["author"] == "ai"
    roles_run = [c["role"] for c in calls(log)]
    assert roles_run[:3] == ["producer", "editor", "critic"]                  # the critic runs on a removal
    assert [p["state"] for p in d["phases"] if p["name"] in ("fix", "render2", "qa2")] == ["skipped"] * 3
    assert brain.notes_ledger(cfg, "trip")[-1]["version"] == 2
    assert json.loads((bus.order_dir(cfg, oid) / "result.json").read_text())["outcome"] == "ready"
    lt = R.light(cfg)
    assert lt["light"] == "needs_you" and "trip_v2" in lt["review"]          # waiting for a verdict
    ev = bus.events(cfg, oid)
    assert any(e["state"] == "progress" and e.get("role") for e in ev)      # team activity lines
    # the editor never wrote a version or the draft; roles never ran render
    assert not any("render" in " ".join(c["argv"]) and c["role"] for c in calls(log) if "Bash(cutroom render" in c["argv"])


def test_runner_restart_resumes_at_the_interrupted_phase(fproj, tmp_path, monkeypatch):
    import signal
    from cutroom.factory import gates
    from cutroom.factory import runner as R
    cfg, ids = fproj["cfg"], fproj["ids"]
    log, _ = use_fake(tmp_path, monkeypatch, {"editor": {"edits": [{"op": "delete", "sec": 1, "seg": 0}]}})
    plan = base_plan(ids, "crash")
    plan["sections"][1]["segments"] += [{"type": "broll", "clip": ids["scenes"], "in": 0.2 + i * 0.6, "out": 0.7 + i * 0.6}
                                        for i in range(8)]
    new_plan(cfg, ids, "crash", plan=plan, render=False)
    _marker(cfg, "crash", 0.2, "the black bit: remove it", mid="cr1")
    oid = R.submit(cfg, "revise", "crash", comment_ids=["cr1"])["order"]["id"]
    env = gates.sub_env(cfg)
    p = subprocess.Popen(gates.cli("factory", "run"), cwd=cfg.work, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    killed_at = None
    try:
        t_end = time.time() + 300
        while time.time() < t_end:
            st = bus.status(cfg, oid)
            if st["phase"] in ("render", "qa") and st["state"] == "running":
                killed_at = st["phase"]
                break
            if st["state"] in bus.TERMINAL:
                break
            time.sleep(0.1)
    finally:
        os.killpg(p.pid, signal.SIGKILL)                                     # a hard crash: no cleanup at all
        p.wait()
    assert killed_at, bus.status(cfg, oid)
    assert bus.status(cfg, oid)["state"] == "running"                        # left mid-phase
    n_calls = len(calls(log))
    r = R.FactoryRunner(cfg)
    assert r.acquire()
    try:
        r._recover_orphans()
        drain(r)
    finally:
        r.release()
    st = bus.status(cfg, oid)
    assert st["state"] in ("ready", "needs_you") and st["version"], st
    later = [c["role"] for c in calls(log)][n_calls:]
    assert "producer" not in later and "editor" not in later                 # finished phases were not re-run
    idx = phases_index("revise", killed_at)
    inj = json.loads((bus.order_dir(cfg, oid) / "phases" / f"{idx:02d}_{killed_at}" / "in.json").read_text())
    assert inj["attempts"] == 2
    assert (Path(cfg.work) / "plans" / f"crash_v{st['version']}.json").exists()


def phases_index(kind, name):
    from cutroom.factory import phases
    return phases.PLANS[kind].index(name)


def test_runner_gates_refusal_limit_timeout(fproj, tmp_path, monkeypatch):
    from cutroom.factory import runner as R
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "gates", render=False)
    _marker(cfg, "gates", 0.5, "trim the start", mid="g1")
    # 1) login refused: needs_you + backoff; nothing runs until Resume
    log, sc = use_fake(tmp_path, monkeypatch, {"producer": [{"mode": "refuse"}, {}]})
    oid = R.submit(cfg, "revise", "gates", comment_ids=["g1"])["order"]["id"]
    r = R.FactoryRunner(cfg)
    assert r.acquire()
    try:
        drain(r, 120)
        st = bus.status(cfg, oid)
        assert st["state"] == "needs_you" and st["why"] == "account-refused" and st["questions"][0]["kind"] == "login"
        assert R.backoff(cfg) and R.light(cfg)["light"] == "blocked" and R.light(cfg)["login"]
        o2 = R.submit(cfg, "revise", "gates", text="another note")["order"]["id"]
        r.tick()
        assert r.driver is None and r.gate.startswith("backoff")              # everything paused
        assert R.resume(cfg) == 1 and not R.backoff(cfg)
        R.stop(cfg, o2)
        drain(r, 300)
        assert bus.status(cfg, oid)["state"] in ("ready", "needs_you")
        assert [c["role"] for c in calls(log)].count("producer") == 2         # exactly one retry, after Resume
        # 2) usage limit: requeued with not_before, at most 3 attempts, then blocked
        log, sc = use_fake(tmp_path, monkeypatch, {"producer": {"mode": "limit"}})
        monkeypatch.setattr(R.fconfig, "load", lambda c, _l=R.fconfig.load: {**_l(c), "limit_backoff_s": 0.2})
        _marker(cfg, "gates", 1.0, "trim again", mid="g2")
        o3 = R.submit(cfg, "revise", "gates", comment_ids=["g2"])["order"]["id"]
        for _ in range(4):
            drain(r, 60)
            time.sleep(0.3)
        st = bus.status(cfg, o3)
        assert st["state"] == "blocked" and "usage limit" in st["why"], st
        assert [c["role"] for c in calls(log)].count("producer") == 4         # first try + 3 retries, no loop
        # Retry is manual and only from blocked
        assert R.retry(cfg, o3)["state"] == "queued"
        R.stop(cfg, o3)
        # 3) a role over its wall clock: killed, order blocked, nothing relaunched
        log, sc = use_fake(tmp_path, monkeypatch, {"producer": {"mode": "timeout"}})
        monkeypatch.setattr(R.fconfig, "load", lambda c, _l=R.fconfig.load: {**_l(c), "walls": {**_l(c)["walls"], "producer": 2}})
        _marker(cfg, "gates", 1.5, "and again", mid="g3")
        o4 = R.submit(cfg, "revise", "gates", comment_ids=["g3"])["order"]["id"]
        drain(r, 60)
        st = bus.status(cfg, o4)
        assert st["state"] == "blocked" and "wall clock" in st["why"]
        assert [c["role"] for c in calls(log)].count("producer") == 1
        # PAUSED gate: gates are checked only when an order is pickable, so queue one first
        _marker(cfg, "gates", 2.0, "one more", mid="g4")
        o5 = R.submit(cfg, "revise", "gates", comment_ids=["g4"], debounce_s=0)["order"]["id"]
        R.pause(cfg)
        try:
            r.tick()
            assert r.gate == "paused" and r.driver is None and bus.status(cfg, o5)["state"] == "queued"
            assert R.light(cfg)["light"] == "blocked"
        finally:
            R.resume(cfg)
            R.stop(cfg, o5)
    finally:
        R.resume(cfg)
        r.release()


def test_runner_budget_cap_pauses_the_order(fproj, tmp_path, monkeypatch):
    from cutroom.factory import runner as R
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "budget", render=False)
    _marker(cfg, "budget", 0.5, "trim the start", mid="b1")
    log, _ = use_fake(tmp_path, monkeypatch, {"producer": {"cost": 0.8}})
    monkeypatch.setenv("CUTROOM_FACTORY_COST_USD", "0.5")
    oid = R.submit(cfg, "revise", "budget", comment_ids=["b1"])["order"]["id"]
    r = R.FactoryRunner(cfg)
    assert r.acquire()
    try:
        drain(r, 120)
        st = bus.status(cfg, oid)
        assert st["state"] == "needs_you" and "budget" in st["why"] and st["questions"][0]["kind"] == "budget"
        assert [c["role"] for c in calls(log)] == ["producer"]               # stopped at the next phase boundary
        monkeypatch.delenv("CUTROOM_FACTORY_COST_USD")
        R.answer(cfg, oid, st["questions"][0]["id"], 1)
        drain(r, 300)
        assert bus.status(cfg, oid)["state"] in ("ready", "needs_you") and "editor" in [c["role"] for c in calls(log)]
    finally:
        r.release()


# ============================================================================================== step 7: studio wiring
def _req(port, method, path, body=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data or b"null")
    except ValueError:
        return r.status, data


class _SSE:
    def __init__(self, port):
        import http.client
        import queue
        self.q = queue.Queue()
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        self.c.request("GET", "/api/events")
        self.r = self.c.getresponse()
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

    def wait(self, pred, timeout=300):
        import queue
        t_end = time.time() + timeout
        seen = []
        while time.time() < t_end:
            try:
                ev, d = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.append((ev, d))
            if pred(ev, d):
                return ev, d
        raise AssertionError(f"event not seen; last: {seen[-5:]}")

    def close(self):
        try:
            self.c.sock.shutdown(2)
        except Exception:  # noqa: BLE001
            pass
        self.c.close()


def test_studio_factory_routes_end_to_end(fproj, tmp_path, monkeypatch):
    from cutroom import governor, studio
    cfg, ids = fproj["cfg"], fproj["ids"]
    log, _ = use_fake(tmp_path, monkeypatch, {"editor": {"edits": [{"op": "delete", "sec": 1, "seg": 0}]}})
    monkeypatch.setenv("CUTROOM_FACTORY_DEBOUNCE_S", "2")
    new_plan(cfg, ids, "studiof", render=False)
    _marker(cfg, "studiof", 0.3, "remove the black bit", mid="s1")
    srv, st = studio.make_server(cfg, 0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    port = srv.server_address[1]
    sse = _SSE(port)
    try:
        assert st.factory.owner and st.factory.link_base.endswith(str(port))
        code, s0 = _req(port, "GET", "/api/factory/state")
        assert code == 200 and not s0["paused"] and not s0["backoff"] and s0["config"]["budgets"]["cost_usd"] == 5.0
        assert s0["light"] in ("idle", "needs_you", "blocked") and (s0["light"] != "blocked" or s0["blocked"])
        code, o = _req(port, "POST", "/api/factory/orders", {"plan": "studiof", "comment_ids": ["s1"]})
        assert code == 200 and o["status"]["state"] == "queued"
        _marker(cfg, "studiof", 0.6, "and tighten here", mid="s2")
        code, o2 = _req(port, "POST", "/api/factory/orders", {"plan": "studiof", "comment_ids": ["s2"]})
        assert o2["folded_into"] == o["id"]                                   # two sends, one order
        code, _x = _req(port, "POST", "/api/factory/orders", {"plan": "studiof", "kind": "publish"})
        assert code == 400
        sse.wait(lambda e, d: e == "factory" and d.get("order") == o["id"] and d.get("state") in ("ready", "needs_you"),
                 timeout=600)
        sse.wait(lambda e, d: e == "factory_state", timeout=30)
        code, d = _req(port, "GET", f"/api/factory/orders/{o['id']}")
        assert d["status"]["state"] in ("ready", "needs_you") and d["status"]["version"] == 2
        assert [p["step"] for p in d["phases"]][:2] == ["Intake", "Producer"] and d["roles"][0]["role"] == "producer-1"
        code, lg = _req(port, "GET", f"/api/factory/orders/{o['id']}/role/producer-1/log")
        assert code == 200 and lg["lines"] and "prompt" in lg
        code, jobs = _req(port, "GET", "/api/jobs")
        assert any(j["title"].startswith("factory: studiof_v2") for j in jobs["jobs"])       # render went through Jobs
        code, vs = _req(port, "GET", "/api/factory/versions?plan=studiof")
        assert vs["versions"][0]["version"] == 2 and vs["versions"][0]["card"] and vs["versions"][0]["render_url"]
        code, rv = _req(port, "GET", "/api/review")
        assert any(i["plan"] == "studiof" and i["version"] == 2 for i in rv["items"])
        # verdicts
        code, e = _req(port, "POST", "/api/factory/verdict", {"plan": "studiof", "version": 2, "disposition": "REJECT"})
        assert code == 400                                                    # reason required
        code, a = _req(port, "POST", "/api/factory/verdict", {"plan": "studiof", "version": 2, "disposition": "APPROVE"})
        assert code == 200 and a["approve_order"] and "order" not in a
        sse.wait(lambda e, d: e == "factory" and d.get("order") == a["approve_order"] and d.get("state") == "done", 600)
        assert (Path(cfg.work) / "renders" / "studiof_v2.mp4").exists()     # full quality, from masters
        code, vs = _req(port, "GET", "/api/factory/versions?plan=studiof")
        assert vs["versions"][0]["approved"] and vs["versions"][0]["final_render"] == "studiof_v2.mp4"
        assert len(brain.locks(cfg, "studiof")) >= 2                          # approved segments are locked
        # grades, brain, lessons, extension stub
        code, g = _req(port, "POST", "/api/grades", {"clip": ids["dark"], "t0": 0, "t1": 2, "grade": "NEVER"})
        assert code == 200 and _req(port, "GET", "/api/grades")[1]["grades"]
        assert _req(port, "POST", "/api/grades", {"clip": ids["dark"], "grade": "BAD"})[0] == 400
        code, b = _req(port, "PUT", "/api/factory/brain/studiof/STORY.md", {"value": "# Story\n1. morning\n"})
        assert code == 200 and _req(port, "GET", "/api/factory/brain/studiof")[1]["files"]["STORY.md"].startswith("# Story")
        code, les = _req(port, "POST", "/api/factory/lessons", {"rule": "never use the dark clip", "tags": ["broll"],
                                                                "check": {"type": "banned_clip", "params": {"clip": ids["dark"]}}})
        assert code == 200 and les["check"]
        code, ll = _req(port, "GET", "/api/factory/lessons")
        assert any(x["id"] == les["id"] for x in ll["lessons"]) and ll["checks"]
        assert _req(port, "PATCH", "/api/factory/lessons", {"id": les["id"], "status": "superseded"})[1]["status"] == "superseded"
        assert _req(port, "POST", f"/api/factory/apply-extension/{o['id']}")[0] == 501
        # pause / resume
        p1 = _req(port, "POST", "/api/factory/pause")[1]
        assert p1["light"] == "blocked" and p1["paused"] and p1["text"] == "paused"
        p2 = _req(port, "POST", "/api/factory/resume")[1]
        assert not p2["paused"] and p2.get("text") != "paused"
        # the chat refuses while another AI run holds the agent slot
        g = governor.for_cfg(cfg)
        with g.try_slot("agent", label="factory editor") as held:
            assert held
            code, body = 0, None
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request("POST", "/api/chat", body=json.dumps({"message": "hi"}).encode(), headers={"Content-Type": "application/json"})
            txt = c.getresponse().read().decode()
            c.close()
            assert "busy" in txt and "factory editor" in txt
    finally:
        sse.close()
        st.close()
        srv.shutdown()
        srv.server_close()


# ============================================================================================== steps 8-11: critic, verdicts,
# forensics, lessons
def _drive_one(cfg, slug, comment_ids=None, text="", kind="revise", **kw):
    from cutroom.factory import runner as R
    oid = R.submit(cfg, kind, slug, comment_ids=comment_ids, text=text, **kw)["order"]["id"]
    r = R.FactoryRunner(cfg)
    assert r.acquire()
    try:
        drain(r)
    finally:
        r.release()
    return oid, R.order_detail(cfg, oid)


def test_critic_hard_finding_needs_a_proof(fproj, tmp_path, monkeypatch):
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "crit")                                              # with a base render
    hard = {"findings": [{"id": "F1", "sev": "HARD", "t": 1.0, "note": "n1", "rule": None, "text": "jump in the words",
                          "evidence": "frame"}]}
    # 1) HARD finding + a proof from the fix round -> ready, finding closed
    log, _ = use_fake(tmp_path, monkeypatch, {"critic": {"result": hard}, "editor": {"edits": [{"op": "delete", "sec": 0, "seg": 2}]}})
    from cutroom.render import resolve_timeline
    tl = resolve_timeline(cfg, fproj["con"], base_plan(ids, "crit"))
    r02 = next(r for r in tl["segments"] if (r["sec"], r["seg"]) == (0, 2))
    _marker(cfg, "crit", round((r02["start"] + r02["end"]) / 2, 2), "remove the dark clip", mid="k1")
    oid, d = _drive_one(cfg, "crit", ["k1"])
    roles_ = [c["role"] for c in calls(log)]
    assert roles_[:4] == ["producer", "editor", "critic", "fix"] and roles_.count("critic") == 1    # one critic, one fix
    assert d["status"]["state"] == "ready", d["card"]["not_passed"]
    f = d["card"]["critic"]["findings"][0]
    assert f["id"] == "F1" and f["fix"]
    assert "lessons" in roles_                                             # HARD finding harvested + triaged
    assert any(i["kind"] == "critic_hard" for i in lessons.load(cfg)["inbox"])
    # the critic never saw the editor's ledger reasoning
    crit = next(c for c in calls(log) if c["role"] == "critic")
    assert "stub change for" not in crit["prompt_head"] and "Your first-round ledger" not in crit["prompt_head"]
    # 2) no proof -> the version is visible but "not passed", needs you
    log, _ = use_fake(tmp_path, monkeypatch, {"critic": {"result": hard}, "fix": {"result": {"proofs": []}}})
    _marker(cfg, "crit", 0.7, "trim the start", mid="k2")
    oid, d = _drive_one(cfg, "crit", ["k2"])
    assert d["status"]["state"] == "needs_you" and "critic F1" in d["status"]["why"]
    assert d["card"] and not d["card"]["passed"] and (Path(cfg.work) / "plans" / f"crit_v{d['status']['version']}.json").exists()
    assert d["status"]["questions"][0]["kind"] == "notpassed"
    # 3) a read-only role that writes a file: the phase fails as HARD
    log, _ = use_fake(tmp_path, monkeypatch, {"critic": {"write": {"plans/crit_v99.json": "{}"}}})
    _marker(cfg, "crit", 0.9, "remove it", mid="k3")
    oid, d = _drive_one(cfg, "crit", ["k3"])
    assert d["status"]["state"] == "blocked" and "modified files" in d["status"]["why"]
    (Path(cfg.work) / "plans" / "crit_v99.json").unlink()
    # 4) a pure caption/text tweak skips the critic
    log, _ = use_fake(tmp_path, monkeypatch, {"producer": {"result": {"changes": [
        {"note": "n1", "action": "caption_text", "segments": ["0.0"], "summary": "fix the words", "tags": ["captions"]}]}}})
    _marker(cfg, "crit", 1.1, "the caption says the wrong word", mid="k4")
    oid, d = _drive_one(cfg, "crit", ["k4"])
    assert "critic" not in [c["role"] for c in calls(log)]
    assert next(p for p in d["phases"] if p["name"] == "critic")["skipped"] == "pure text / caption tweak"


def test_verdicts_conditional_reject_notes(fproj, tmp_path):
    from cutroom.factory import runner as R
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "verd", render=False)
    with pytest.raises(KeyError):
        R.verdict(cfg, "verd", 9, "APPROVE")
    a = R.verdict(cfg, "verd", 1, "APPROVE", text="approve but change the last clip")
    assert a["approve_order"] and a["order"]                                    # conditional: approve + a revise
    assert bus.load_order(cfg, a["order"])["text"] == "approve but change the last clip"
    with pytest.raises(ValueError):
        R.verdict(cfg, "verd", 1, "REJECT")
    rj = R.verdict(cfg, "verd", 1, "REJECT", text="the intro is worse than before")
    o = bus.load_order(cfg, rj["order"])
    assert o["kind"] == "reject" and o["priority"] == 1 and o["base_file"] == "verd_v1.json"
    assert any(i["kind"] == "reject" for i in lessons.load(cfg)["inbox"])
    with pytest.raises(ValueError):
        R.verdict(cfg, "verd", 1, "NOTES")
    with pytest.raises(ValueError):
        R.verdict(cfg, "verd", 1, "PUBLISH")
    # priority: the reject goes before the earlier revise
    r = R.FactoryRunner(cfg)
    assert r.pick() == rj["order"] or bus.load_order(cfg, r.pick())["priority"] == 1
    for x in (a["order"], a["approve_order"], rj["order"]):
        R.stop(cfg, x)


def test_team_sizing_and_forensics(fproj, tmp_path, monkeypatch):
    from cutroom.factory import phases
    n = [{"flags": ["restore"]}]
    assert phases.size_team({"kind": "revise"}, n, {"team": {"size": "S"}})["size"] == "M"
    assert phases.size_team({"kind": "revise"}, [{"flags": []}], {"changes": [{"action": "new_capability"}], "team": {"size": "S"}})["size"] == "L"
    assert phases.size_team({"kind": "request"}, [{"flags": []}], {"team": {"size": "S"}})["size"] == "L"
    assert phases.size_team({"kind": "revise"}, [{"flags": []}] * 13, {"team": {"size": "S"}})["size"] == "M"
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "fore", render=False)
    log, _ = use_fake(tmp_path, monkeypatch, {"producer": {"result": {"team": {"size": "L", "forensics": True, "extend": [],
                                                                                "why": "match the reference"}}}})
    _marker(cfg, "fore", 0.5, "make the start like the reference", mid="f1")
    oid, d = _drive_one(cfg, "fore", ["f1"], ref="ref123")
    assert "forensics" in [c["role"] for c in calls(log)]
    assert brain.read_file(cfg, "fore", "REFERENCE.md").startswith("# Reference")
    assert brain.read_file(cfg, "fore", "ref_pairs.json")[0]["label"] == "start"


def test_extend_is_a_suborder_stub(fproj, tmp_path, monkeypatch):
    from cutroom.factory import runner as R
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "ext", render=False)
    log, _ = use_fake(tmp_path, monkeypatch, {"producer": {"result": {"team": {"size": "S", "forensics": False,
                                                                               "extend": ["word-pop captions"], "why": "x"}}}})
    _marker(cfg, "ext", 0.5, "make the words pop one by one", mid="e1")
    oid, d = _drive_one(cfg, "ext", ["e1"])
    sub = next(o for o in R.list_view(cfg, plan="ext") if o["kind"] == "extend")
    assert sub["parent"] == oid and sub["state"] == "needs_you" and "word-pop" in sub["why"]
    assert d["status"]["state"] in ("ready", "needs_you")                     # the parent was not blocked
    assert R.answer(cfg, sub["id"], "q1", 2)["label"] == "skip it"
    assert bus.status(cfg, sub["id"])["state"] == "done"


def test_lessons_loop(tmp_path):
    cfg = Config(work=str(tmp_path))
    rules = lessons.parse_rules((Path(__file__).resolve().parents[1] / "docs" / "EDITING_RULES.md").read_text())
    assert len(rules) > 100 and rules[0]["id"] == "R001" and rules[0]["tags"] == ["story"] and rules[0]["why"]
    lessons.ensure_global_seed()
    assert len(lessons.load(cfg, "global")["lessons"]) == len(rules)
    new = lessons.harvest(cfg, [{"source": "verdict:x", "kind": "reject", "text": "the dark clip again", "tags": ["broll"]}], order="o1")
    again = lessons.harvest(cfg, [{"source": "verdict:x", "kind": "reject", "text": "the dark clip again"}], order="o1")
    assert len(new) == 1 and again == []                                       # idempotent
    sha0 = lessons.lessons_sha(cfg)
    done = lessons.apply_ops(cfg, [{"op": "add", "inbox": new[0]["id"], "rule": "Never use clip C9", "symptom": "dark",
                                    "tags": ["broll"], "check_json": json.dumps({"type": "banned_clip", "params": {"clip": "C9"}})}])
    lid = done[0]["lesson"]
    assert done[0]["check"] and lint.load_checks(cfg, "project")[0]["params"]["clip"] == "C9"
    assert lessons.lessons_sha(cfg) != sha0
    b = lessons.brief(cfg, "slug", tags={"broll"})
    assert f"[{lid}]" in b["text"] and "machine check" in b["text"] and b["n_global"] > 0
    assert all(r.startswith(("P", "R")) for r in b["rules"]) and len(b["rules"]) <= 60
    cap = lessons.brief(cfg, "slug", tags=set(), cap=10)
    assert len(cap["rules"]) == 10
    brain.add_decision(cfg, "slug", "music", "no music under talk", "comment")
    b2 = lessons.brief(cfg, "slug", tags={"captions"})
    assert "no music under talk" in b2["text"] and f"[{lid}]" in b2["text"]  # project lessons always; rulings first
    assert not any(r.startswith("R") and "broll" in str(r) for r in b2["rules"])
    lessons.supersede(cfg, lid)
    assert f"[{lid}]" not in lessons.brief(cfg, "slug", tags={"broll"})["text"]
    n2 = lessons.harvest(cfg, [{"source": "x", "kind": "notes", "text": "one-off"}])
    lessons.apply_ops(cfg, [{"op": "none", "inbox": n2[0]["id"], "reason": "taste"}])
    assert lessons.inbox(cfg) == []


# ============================================================================================== step 12: variants
def test_variants_keep_folds_into_next_version(fproj, tmp_path, monkeypatch):
    from cutroom.factory import runner as R
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "vari", render=False)
    log, _ = use_fake(tmp_path, monkeypatch, {"variants": {"variant_edits": [
        [{"op": "set", "sec": 0, "seg": 1, "fields": {"in": 3.0, "out": 5.0}}],
        [{"op": "set", "sec": 0, "seg": 1, "fields": {"clip": ids["steady"], "in": 3.0, "out": 5.0}}]]}})
    _marker(cfg, "vari", 9.0, "try a few options for this shot", mid="v1")
    oid, d = _drive_one(cfg, "vari", ["v1"], kind="variants", variants=2)
    assert d["status"]["state"] == "ready", d["status"]
    cd = d["card"]
    assert len(cd["variants"]) == 2 and all(v["render"] for v in cd["variants"])
    assert all((Path(cfg.work) / "renders" / v["render"]).exists() for v in cd["variants"])
    res = R.variant_verdict(cfg, oid, [{"variant": 2, "verdict": "KEEP"}, {"variant": 1, "verdict": "KILL"}])
    keep = next(x for x in res if x["verdict"] == "KEEP")["order"]
    assert bus.load_order(cfg, keep)["adopt"].endswith("var2.json")
    r = R.FactoryRunner(cfg)
    assert r.acquire()
    try:
        drain(r)
    finally:
        r.release()
    st = bus.status(cfg, keep)
    assert st["state"] in ("ready", "needs_you") and st["version"]
    v = json.loads((Path(cfg.work) / "plans" / f"vari_v{st['version']}.json").read_text())
    assert v["sections"][0]["segments"][1]["clip"] == ids["steady"]          # the kept option is in the version


# ============================================================================================== step 14: acceptance (stub replay)
REPLAY_NOTES = [   # the 11 operator notes of the v023 round (redacted), as comments on a rebuilt v022
    (0, 0, "Add the 'everyone's so supportive' bit back, it was better before"),
    (0, 1, "I've been saying it's hot too much, I was a bit repetitive"),
    (0, 2, "The 4-bedroom, 5-bathroom animation, you can get rid of"),
    (1, 0, "The caption should say 'the sexiest view with the pool at the top'"),
    (1, 1, "remove the 600 animation and change it to say 'I even bought the second account'"),
    (1, 2, "A lot of silence after I say it's pretty nice and lively"),
    (1, 3, "No need for the slow-mo bit"),
    (2, 0, "That clip at 7:19, the person walking to the left: just remove that clip"),
    (2, 1, "I say recovery time and then there's a clip of me"),
    (2, 2, "The clip at 8:23 is horrible, I hate the lighting on it"),
    (2, 3, "Remove the account ban animation"),
]


def replay_plan(ids):
    seg = lambda c, a, b: {"type": "broll", "clip": ids[c], "in": a, "out": b}      # noqa: E731
    return {"title": "Replay", "version": 22, "style": {"captions": {"enabled": False}}, "sections": [
        {"name": "intro", "segments": [seg("steady", 0.2, 2.2), seg("scenes", 0.3, 1.8), seg("dark", 1, 2.5)]},
        {"name": "villa", "segments": [seg("steady", 3, 4.5), seg("scenes", 3.2, 4.4), seg("steady", 5, 6.5),
                                       {**seg("scenes", 6.1, 7.5), "speed": 0.5}]},
        {"name": "night", "segments": [seg("shaky", 0.5, 2), seg("steady", 6.6, 7.8), seg("dark", 3, 4.5), seg("scenes", 4.5, 5.6)]}]}


def test_acceptance_replay_v022_to_v023_stub(fproj, tmp_path, monkeypatch):
    """Acceptance (build step 14) against the stub agent: 11 notes on a rebuilt v022; 9 resolved the way the real
    v023 did, 2 become questions; the lock check shows nothing else changed; wall time well under 45 min.
    The real replay (real footage, real agent) is described in docs/FACTORY.md."""
    from cutroom.factory import runner as R
    from cutroom.render import resolve_timeline
    cfg, con, ids = fproj["cfg"], fproj["con"], fproj["ids"]
    plan = replay_plan(ids)
    pf = Path(cfg.work) / "plans" / "replay_v22.json"
    pf.write_text(json.dumps(plan))
    render_base(cfg, pf)
    tl = resolve_timeline(cfg, con, plan)
    mid = {(r["sec"], r["seg"]): (r["start"] + r["end"]) / 2 for r in tl["segments"]}
    for i, (s, g, text) in enumerate(REPLAY_NOTES):
        _marker(cfg, "replay", round(mid[(s, g)], 2), text, mid=f"r{i + 1:02d}")
    # what the real v023 did (render_v5's v023 layer), as plan edits on v022; two notes were asked back
    asked = {"n9", "n2"}
    edits = [{"op": "delete", "sec": 2, "seg": 3}, {"op": "delete", "sec": 2, "seg": 2}, {"op": "delete", "sec": 2, "seg": 0},
             {"op": "set", "sec": 1, "seg": 3, "fields": {"speed": None}}, {"op": "set", "sec": 1, "seg": 2, "fields": {"out": 6.2}},
             {"op": "set", "sec": 1, "seg": 1, "fields": {"in": 3.4}}, {"op": "set", "sec": 1, "seg": 0, "fields": {"out": 4.3}},
             {"op": "delete", "sec": 0, "seg": 2}, {"op": "set", "sec": 0, "seg": 0, "fields": {"in": 0.4}}]
    note_seg = {f"n{i + 1}": f"{s}.{g}" for i, (s, g, _t) in enumerate(REPLAY_NOTES)}
    ledger = [{"note": n, "change": f"done like v023 ({REPLAY_NOTES[int(n[1:]) - 1][2][:40]})", "segments": [note_seg[n]],
               "t_out": round(mid[tuple(map(int, note_seg[n].split(".")))], 2), "proof": "frame at the spot"}
              for n in note_seg if n not in asked]
    changes = [{"note": n, "action": "remove" if "remove" in REPLAY_NOTES[int(n[1:]) - 1][2].lower() or "rid" in
                REPLAY_NOTES[int(n[1:]) - 1][2] else "trim", "segments": [note_seg[n]], "summary": "as v023", "tags": []}
               for n in note_seg if n not in asked]
    questions = [{"note": "n2", "text": "Which 'hot' lines go?", "options": ["the 2nd and 4th", "all but the first"]},
                 {"note": "n9", "text": "Recovery time: which clip of you?", "options": ["the pool one", "the bed one"]}]
    log, _ = use_fake(tmp_path, monkeypatch, {
        "producer": {"result": {"changes": changes, "questions": questions,
                                "team": {"size": "M", "forensics": False, "extend": [], "why": "craft"}}},
        "editor": {"edits": edits, "result": {"ledger": ledger}}})
    t0 = time.time()
    oid, d = _drive_one(cfg, "replay", [f"r{i + 1:02d}" for i in range(11)])
    wall = time.time() - t0
    cd = d["card"]
    resolved = {r["note"] for r in cd["rows"]}
    assert len(resolved) >= 9 and len(cd["questions"]) >= 2, (resolved, cd["questions"])
    assert {q["note"] for q in cd["questions"] if q.get("note")} >= asked
    qa = next(p for p in d["phases"] if p["name"] == "qa")
    lock_fails = [x for x in cd["not_passed"] if "lock" in x]
    assert not lock_fails, lock_fails                                          # nothing un-noted changed
    assert wall < 45 * 60
    v = json.loads((Path(cfg.work) / "plans" / f"replay_v{d['status']['version']}.json").read_text())
    assert d["status"]["version"] == 23 and len(v["sections"][2]["segments"]) == 1
    (tmp_path / "replay_result.json").write_text(json.dumps({"wall_s": wall, "resolved": sorted(resolved),
                                                              "questions": [q["line"] for q in cd["questions"]],
                                                              "passed": cd["passed"], "not_passed": cd["not_passed"],
                                                              "qa_phase": qa}, indent=1))
    print(f"\nreplay: {len(resolved)}/11 resolved, {len(cd['questions'])} questions, {wall:.0f}s, passed={cd['passed']}")


def test_film_provenance_reaches_sidecar_normalize():
    from cutroom import film
    f = film.normalize({"rolloff": {"knee": 0.82, "ceiling": 0.94}, "from_reference": "9fe6442b1b", "measured": "x"})
    assert f["from_reference"] == "9fe6442b1b" and f["rolloff"]["ceiling"] == 0.94 and "measured" not in f
    assert "from_reference" not in film.normalize({"rolloff": {"knee": 0.82, "ceiling": 0.94}})
    assert film.normalize({"from_reference": "r"}) is None          # provenance alone is no effect
    assert film.chain(f, 1920, 1080)                                   # still renders


def test_render_lock_excused_only_by_asked_style_change():
    hard = [{"sev": "HARD", "ref": "1.2", "text": "untouched segment 1.2 looks different", "t": 49.0}]
    ex = lockcheck.excuse_global(hard, ["cold_open", "style"], True)
    assert ex[0]["sev"] == "warn" and "style" in ex[0]["text"] and hard[0]["sev"] == "HARD"
    assert lockcheck.excuse_global(hard, ["style"], False)[0]["sev"] == "HARD"       # not asked for
    assert lockcheck.excuse_global(hard, ["cold_open"], True)[0]["sev"] == "HARD"    # cold open can't touch body frames


def test_role_path_has_cutroom_command(tmp_path, monkeypatch):
    from cutroom.factory import runner as R
    cfg = Config(work=str(tmp_path))
    fake = tmp_path / "py" / "bin"
    fake.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(fake / "python3"))
    d = Path(R.cli_bindir(cfg))
    assert d == tmp_path / "factory" / ".bin" and os.access(d / "cutroom", os.X_OK)
    assert f'"{fake / "python3"}" -m cutroom.cli' in (d / "cutroom").read_text()
    (fake / "cutroom").write_text("#!/bin/sh\n")                         # a venv install: use it as is
    assert R.cli_bindir(cfg) == str(fake)


def test_fix_round_drops_answered_unplaced_questions():
    from cutroom.factory import phases
    pr = {"questions": [{"text": "producer q", "note": None}]}
    ed = {"unplaced": [{"note": "n2", "text": "title can't move"}, {"note": "n3", "text": "still open"}]}
    fx = {"ledger": [{"note": "n2", "change": "swapped the held shot"}, {"note": "n1", "change": "x"}]}
    assert [q["text"] for q in phases.round_questions(pr, ed, fx)] == ["producer q", "still open"]
    assert len(phases.round_questions(pr, ed, None)) == 3                 # no fix round: all stay


# ============================================================================================== hardening after the first real run
def test_lessons_only_from_the_creator_and_supersede_delete(tmp_path):
    """QA bugs taught false lessons in the real run ("never add a film look" from a broken rule-38 check). A
    lesson comes only from the creator's words or a finding the creator confirmed; delete / supersede retire
    the lesson's check too."""
    cfg = Config(work=str(tmp_path))
    rows = lessons.harvest(cfg, [
        {"source": "qa:o1", "kind": "machine_fail", "text": "film: not measured on the creator's reference"},
        {"source": "critic:o1", "kind": "critic_hard", "text": "the same setup three times in 5 s"},
        {"source": "comment:m1", "kind": "note", "text": "never put the title on my shirt"},
        {"source": "verdict:x_v2", "kind": "reject", "text": "too quiet, no punch"}], order="o1")
    st = {r["kind"]: r["status"] for r in rows}
    assert st == {"machine_fail": "unconfirmed", "critic_hard": "unconfirmed", "note": "open", "reject": "open"}
    assert {r["provenance"]["kind"] for r in rows} == {"machine", "critic", "creator_note", "creator_reject"}
    assert [r["kind"] for r in lessons.inbox(cfg, order="o1")] == ["note", "reject"]   # the keeper never sees the rest
    m_id, c_id, n_id = rows[0]["id"], rows[1]["id"], rows[2]["id"]
    # a lesson-keeper that tries anyway is refused by code
    done = lessons.apply_ops(cfg, [{"op": "add", "inbox": m_id, "rule": "Never add a film look",
                                    "check_json": {"type": "qa_metric", "params": {"check": "devices_vs_reference"}}}])
    assert done[0]["op"] == "refused" and not lessons.load(cfg)["lessons"] and not lint.load_checks(cfg, "project")
    # the creator's own note can become a lesson (with a check), with provenance on the lesson
    d = lessons.apply_ops(cfg, [{"op": "add", "inbox": n_id, "rule": "Titles never sit on the creator",
                                 "check_json": {"type": "qa_metric", "params": {"check": "text_vs_faces"}}}])
    les = lessons.load(cfg)["lessons"][0]
    assert d[0]["op"] == "add" and les["provenance"]["kind"] == "creator_note" and les["provenance"]["creator"]
    # the creator confirms the critic finding -> eligible
    lessons.confirm_item(cfg, c_id, text="yes: never repeat a setup within 10 s")
    assert c_id in [r["id"] for r in lessons.inbox(cfg)]
    d2 = lessons.apply_ops(cfg, [{"op": "supersede", "inbox": c_id, "lesson": les["id"], "rule": "Never repeat a setup within 10 s"}])
    new = d2[0]["lesson"]
    allc = bus.read_json(lint.checks_path(cfg), {})["checks"]
    assert next(c for c in allc if c["lesson"] == les["id"])["status"] == "retired"      # superseded lesson's check off
    assert not lint.load_checks(cfg, "project")
    assert next(x for x in lessons.load(cfg)["lessons"] if x["id"] == new)["provenance"]["confirmed"]
    # delete: gone from the law and the brief, check retired, kept in `deleted`; ids never reused
    l3 = lessons.add_lesson(cfg, "Never add a film look", check={"type": "qa_metric", "params": {"check": "devices_vs_reference"}})
    assert l3["provenance"]["kind"] == "operator" and lint.load_checks(cfg, "project")
    rec = lessons.delete(cfg, l3["id"], reason="taught by a QA bug")
    assert rec["status"] == "deleted" and not lint.load_checks(cfg, "project")
    assert f"[{l3['id']}]" not in lessons.brief(cfg, "s")["text"]
    assert lessons.load(cfg)["deleted"][0]["id"] == l3["id"]
    assert lessons.add_lesson(cfg, "another")["id"] not in {x["id"] for x in lessons.load(cfg)["lessons"][:-1]} | {l3["id"]}
    lessons.supersede(cfg, new, reason="replaced by hand")
    with pytest.raises(KeyError):
        lessons.supersede(cfg, new, by="P9999")
    lessons.dismiss_item(cfg, m_id, "a QA bug")
    assert not lessons.unconfirmed(cfg)[0]["id"] == m_id if lessons.unconfirmed(cfg) else True


def _co_tl():
    """A resolved timeline with a generated cold open (section -1) like the real run: card, strobe blocks,
    teaser lines, the held end shot with its typed title, then the body."""
    P = []

    def add(sec, seg, typ, kind, a, b, track="V1", clip="c1"):
        P.append({"track": track, "sec": sec, "seg": seg, "type": typ, "kind": kind, "start": a, "end": b,
                  "clip": clip if kind == "clip" else None, "in": 0.0 if kind == "clip" else None, "speed": 1.0})
    add(-1, 0, "card", "card", 0.0, 0.84)
    add(-1, 1, "montage", "clip", 0.84, 1.44)
    add(-1, 2, "talk", "clip", 1.44, 2.6)
    add(-1, 2, "talk", "voice", 1.44, 2.6, track="A1")
    add(-1, 3, "montage", "clip", 2.6, 3.4)
    add(-1, 4, "talk", "clip", 3.4, 5.6)
    add(-1, 5, "broll", "clip", 7.4, 10.1)
    add(-1, 5, "overlay", "typed_title", 7.4, 10.1, track="V3")
    add(0, 0, "talk", "clip", 10.1, 20.0)
    return {"pieces": P, "duration": 20.0}


def test_cold_open_notes_map_to_its_pieces(monkeypatch):
    """The real run: notes on the intro mapped to '0 segments'. Generated cold-open pieces, cards and the typed
    title now map to cold_open.* refs, and the lock check lets a cold-open note change plan['cold_open']."""
    tl = _co_tl()
    plan = {"sections": [{"name": "body", "segments": [{"type": "talk", "clip": "c1", "in": 0, "out": 10}]}],
            "cold_open": {"lines": [{}, {}]}}
    refs = mapnotes.cold_open_refs(tl)
    assert refs[(-1, 0)] == "cold_open.card" and refs[(-1, 2)] == "cold_open.lines[0]" and refs[(-1, 4)] == "cold_open.lines[1]"
    assert refs[(-1, 1)] == "cold_open.shots" and refs[(-1, 5)] == "cold_open.end_shot"
    assert mapnotes.cold_open_span(tl) == [0.0, 10.1]
    monkeypatch.setattr(mapnotes, "_words_in", lambda *a, **k: "")
    n = mapnotes.map_comment(None, None, tl, plan, {"id": "m", "t": 0.3, "text": "louder like the reference"}, 1)
    assert "cold_open.card" in n["segments"] and "cold_open" in n["segments"] and n["cold_open"]
    n4 = mapnotes.map_comment(None, None, tl, plan, {"id": "m4", "t": 8.9, "text": "move the title off the sun"}, 4)
    assert {"cold_open.end_shot", "cold_open.title"} <= set(n4["segments"]) and "0.0" not in n4["segments"]
    nb = mapnotes.map_comment(None, None, tl, plan, {"id": "mb", "t": 14.0, "text": "cut this"}, 5)
    assert nb["segments"] == ["0.0"] and not nb["cold_open"]
    base = {**plan, "cold_open": {"music": {"lift_db": 12}}}
    cand = {**plan, "cold_open": {"music": {"lift_db": 20}}}
    assert lockcheck.plan_lock(base, cand)["status"] == "fail"
    lk = lockcheck.plan_lock(base, cand, allowed_top={"cold_open"})
    assert lk["status"] == "pass" and "a note on it asked for it" in lk["issues"][0]["text"]


def test_find_render_same_version_same_mode_whole_and_fresh(tmp_path):
    """The real run compared a v3 FINAL render (picked over a stale preview) with v4 previews. The base is the
    plan version's render in the same mode, never a cold-open-only partial, never one older than the plan."""
    from cutroom import export
    from cutroom.factory import phases
    cfg = Config(work=str(tmp_path))
    pd, rd = tmp_path / "plans", tmp_path / "renders"
    pd.mkdir()
    rd.mkdir()
    plan = {"title": "x", "version": 3, "sections": []}
    pf = pd / "trip_v3.json"
    pf.write_text(json.dumps(plan))

    def side(name, preview, only=None, version=3, plan_path=pf, sha=True, age=0):
        mp4 = rd / f"{name}.mp4"
        mp4.write_bytes(b"x")
        d = {"schema": "cutroom-render-v1", "plan": str(plan_path.resolve()), "preview": preview, "only": only,
             "version": version, "plan_sha": export.plan_sha(plan) if sha else "stale000"}
        (rd / f"{name}.json").write_text(json.dumps(d))
        t = time.time() - age
        os.utime(mp4, (t, t))
        return mp4
    final = side("trip-a_v3", False, age=10)
    side("trip-a_v3_cold-open", True, only="cold_open", age=5)            # partial: never a base
    side("trip-a_v3_stale_preview", True, sha=False, age=1)                # made before the plan changed
    assert phases.find_render(cfg, pf) is None                             # no fresh whole preview -> none
    assert export.find_render(cfg, pf) == final                            # export: final preferred, partial skipped
    good = side("trip-a_v3_preview", True, age=20)
    side("trip-a_v2_preview", True, version=2, age=0)                     # wrong version
    assert phases.find_render(cfg, pf) == good
    assert export.find_render(cfg, pf, preview=False) == final
    # a factory version: its render's sidecar points at the order's candidate; found by name
    cand = tmp_path / "candidate.json"
    cand.write_text(json.dumps({**plan, "version": 4}))
    pf4 = pd / "trip_v4.json"
    pf4.write_text(json.dumps({**plan, "version": 4}))
    v4 = side("trip_v4_preview", True, version=4, plan_path=cand, sha=False)
    side("trip_v4_var1_preview", True, version=4, plan_path=cand, sha=False)
    assert phases.find_render(cfg, pf4) == v4


CLAIM_TEXTS = [
    ("So the old music swing was 12 dB and the new one is 20 dB, with the floor 6 dB deeper and the hits 2 dB hotter.",
     [("floor", "delta", -6.0), ("hits", "delta", 2.0)]),
    ("The plan check passes, and the runner should confirm the ~11 LU swing, I at -10 and TP at or below -1.5.",
     [("swing", "abs", 11.0)]),
    ("The lift on the no-talk hits goes from 20 to 22 dB (cold_open.music.lift_db), so the hits now sit at dialogue +6 dB, "
     "about -10 LUFS.", [("hits_over_lines", "abs", 6.0), ("hits", "abs", -10.0)]),
    ("Raised the cold open loudness target from -12 to -10 LUFS (cold_open.loudness.I).", []),
    ("C5655 is not used anywhere else within 10s.", [("repeats", "none", 0)]),
]


@pytest.mark.parametrize("text,want", CLAIM_TEXTS)
def test_claims_parse_real_run_sentences(text, want):
    from cutroom.factory import claims
    assert [(c["qty"], c["kind"], c["claimed"]) for c in claims.parse(text)] == want


def _punchy_clip(path, quiet_db=-30, loud_db=-8):
    """10 s: 2 s quiet / 0.5 s loud, repeated - a known swing."""
    expr = f"if(lt(mod(t,2.5),2.0),{10 ** (quiet_db / 20):.5f},{10 ** (loud_db / 20):.5f})*sin(2*PI*440*t)"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=25:d=10",
                    "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d=10", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    "-b:a", "192k", str(path)], check=True)
    return path


def test_claims_checked_against_the_render(tmp_path):
    from cutroom import dynamics
    from cutroom.factory import claims
    cfg = Config(work=str(tmp_path))
    v = _punchy_clip(tmp_path / "v.mp4")
    base = _punchy_clip(tmp_path / "b.mp4", quiet_db=-24, loud_db=-12)
    sw = dynamics.window_stats(cfg, v)["swing"]
    assert 15 < sw < 26, sw
    note = {"id": "n1", "t": 5.0}
    rows = claims.check(cfg, [{"note": note, "source": "editor n1 change", "text": f"The cold open now has a ~{sw:.0f} LU swing."},
                              {"note": note, "source": "editor n1 proof", "text": "That gives a 40 LU swing."},
                              {"note": note, "source": "editor n1 change", "text": "The quiet floor is 6 dB deeper."},
                              {"note": note, "source": "editor n1 change", "text": "The hits are now 4 dB louder."},
                              {"note": note, "source": "editor n1 change", "text": "No clipping anywhere."}],
                        str(v), None, str(base), None, {})
    by = {(r["text"][:30], r["qty"]): r for r in rows}
    ok = {k[0][:12]: r["ok"] for k, r in by.items()}
    assert ok["The cold ope"] is True and ok["That gives a"] is False
    assert ok["The quiet fl"] is True and ok["The hits are"] is True and ok["No clipping "] is True
    bad = claims.findings(rows)
    assert len(bad) == 1 and bad[0]["sev"] == "HARD" and "40" in bad[0]["text"] and bad[0]["source"] == "claims"
    led = claims.annotate_ledger([{"note": "n1", "change": "x", "proof": "y"}], rows)
    assert "NOT on the render" in led[0]["change"] and "measured:" in led[0]["proof"]


def _no_dark_plan(ids, title):
    """base_plan without the clip an earlier test grades NEVER (the module project is shared)."""
    p = base_plan(ids, title)
    p["sections"][0]["segments"] = [s for s in p["sections"][0]["segments"] if s.get("clip") != ids["dark"]]
    return p


def test_false_claim_becomes_a_finding_and_is_rejudged(fproj, tmp_path, monkeypatch):
    """An editor number the render doesn't show becomes a HARD finding; after the fix round it is re-measured
    on the FIXED render: still claimed -> open (not passed); withdrawn -> closed, with the measured number."""
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "claim", plan=_no_dark_plan(ids, "claim"))
    say = {"summary": "Done: the programme now has a 45 LU swing."}
    # 1) the fix round repeats the claim -> still false on the fixed render -> not passed
    use_fake(tmp_path, monkeypatch, {"editor": {"result": say}, "fix": {"result": say}})
    _marker(cfg, "claim", 1.0, "make it punchier", mid="q1")
    oid, d = _drive_one(cfg, "claim", ["q1"])
    cd = d["card"]
    k = next(f for f in cd["critic"]["findings"] if f["id"] == "K1")
    assert k["sev"] == "HARD" and k["status"] == "open" and "45" in k["text"]
    assert d["status"]["state"] == "needs_you" and any(x.startswith("claim K1") for x in cd["not_passed"])
    ver = next(p for p in d["phases"] if p["name"] == "verify")
    assert not ver.get("skipped")
    # 2) the fix round withdraws it -> closed by code, passes
    use_fake(tmp_path, monkeypatch, {"editor": {"result": say}, "fix": {"result": {"summary": "Corrected the swing figure."}}})
    _marker(cfg, "claim", 1.2, "make it punchier again", mid="q2")
    oid, d = _drive_one(cfg, "claim", ["q2"])
    k = next(f for f in d["card"]["critic"]["findings"] if f["id"] == "K1")
    assert k["status"] == "closed" and "withdrew" in k["verified"], k
    assert d["status"]["state"] == "ready", d["card"]["not_passed"]


def test_preexisting_qa_fail_never_blocks_closure(fproj, tmp_path, monkeypatch):
    """A QA fail already in the base version is a 'was already there' warning; the critic's HARD finding closes
    when the verify run re-judges it closed on the fixed render."""
    from cutroom.factory import gates
    cfg, ids = fproj["cfg"], fproj["ids"]
    new_plan(cfg, ids, "pre", plan=_no_dark_plan(ids, "pre"))
    rep = {"summary": {"fail": 1, "warn": 0, "status": "fail"},
           "checks": [{"id": "text_vs_faces", "title": "Captions clear of faces", "status": "fail",
                       "issues": [{"severity": "fail", "t": 6.0, "msg": "caption overlaps a face"}]},
                      {"id": "av_length", "status": "pass", "issues": []}]}
    monkeypatch.setattr(gates, "run_qa", lambda *a, **k: (json.loads(json.dumps(rep)), None))
    hard = {"findings": [{"id": "F1", "sev": "HARD", "t": 1.0, "note": "n1", "rule": None, "text": "jump in the words",
                          "evidence": "frame"}]}
    log, _ = use_fake(tmp_path, monkeypatch, {"critic": {"result": hard}})
    _marker(cfg, "pre", 1.0, "tighten the start", mid="p1")
    oid, d = _drive_one(cfg, "pre", ["p1"])
    roles_ = [c["role"] for c in calls(log)]
    assert "verify" in roles_ and roles_.index("verify") > roles_.index("fix")
    f = d["card"]["critic"]["findings"][0]
    assert f["status"] == "closed" and f["verified"]
    assert d["status"]["state"] == "ready", d["card"]["not_passed"]
    assert any("was already in v1" in w for w in d["card"]["warns"] if isinstance(w, str)) or \
        "already in v1" in json.dumps(d["card"])


def test_qa_check_missing_from_base_is_not_a_new_fail():
    from cutroom.factory import gates
    cand = {"checks": [{"id": "swing_vs_reference", "title": "Swing", "status": "fail",
                        "issues": [{"severity": "fail", "t": 0.0, "msg": "swing 7 vs 12"}]},
                       {"id": "black", "title": "Black", "status": "fail", "issues": [{"severity": "fail", "t": 3, "msg": "b"}]}]}
    base = {"checks": [{"id": "black", "title": "Black", "status": "pass", "issues": []}]}
    new, kept = gates.qa_regressions(cand, base)
    assert [x["check"] for x in new] == ["black"] and kept[0]["check"] == "swing_vs_reference" and kept[0]["not_on_base"]
