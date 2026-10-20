"""Resource governor (slots across processes, memory-cap kills, disk floor) and the overnight pipeline
(dry run end to end on the synthetic library; Claude reply parsing with canned output, no real calls)."""
import json
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from cutroom import governor, overnight
from cutroom.config import Config, init_project

REPO = Path(__file__).resolve().parents[1]


def _gov(tmp_path, **kw):
    cfg = Config(work=str(tmp_path), max_load=1000.0, min_free_ram_gb=0.01, min_free_disk_gb=0.0, **kw)
    g = governor.Governor(cfg)
    g.poll_s = 0.05
    return g


@pytest.fixture(autouse=True)
def _no_hook():
    yield
    governor.reset()


HOLD = """
import json, sys, time
sys.path.insert(0, sys.argv[2])
from cutroom.config import Config
from cutroom import governor
g = governor.Governor(Config(work=sys.argv[1], max_load=1000.0, min_free_ram_gb=0.01, min_free_disk_gb=0.0))
g.poll_s = 0.05
with g.slot("asr", label="test"):
    t0 = time.time()
    time.sleep(1.2)
    t1 = time.time()
print(json.dumps([t0, t1]))
"""


def test_asr_slot_is_exclusive_across_processes(tmp_path):
    procs = [subprocess.Popen([sys.executable, "-c", HOLD, str(tmp_path), str(REPO)], stdout=subprocess.PIPE, text=True)
             for _ in range(2)]
    g = _gov(tmp_path)
    seen_holder = None
    for _ in range(100):                                  # the status shows who holds the slot
        busy = [s for s in g.status()["slots"] if s["kind"] == "asr" and s["busy"]]
        if busy:
            seen_holder = busy[0]
            break
        time.sleep(0.05)
    spans = sorted(json.loads(p.communicate(timeout=60)[0]) for p in procs)
    assert all(p.returncode == 0 for p in procs)
    assert spans[1][0] >= spans[0][1] - 0.01, f"ASR slots overlapped: {spans}"
    assert seen_holder and seen_holder["pid"] in [p.pid for p in procs] and seen_holder["label"] == "test"
    # released: the slot is free again and no waiter files are left behind
    assert not any(s["busy"] for s in g.status()["slots"])
    assert g.status()["waiting"] == []


def test_heavy_slots_bound_concurrency_and_throttle(tmp_path):
    g = _gov(tmp_path, heavy_slots=2)
    live, peak, mx = [0], [0], threading.Lock()

    def job():
        with g.slot("heavy"):
            with mx:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.3)
            with mx:
                live[0] -= 1
    ts = [threading.Thread(target=job) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 2
    # high load throttles to ONE slot, never zero
    g.max_load = -1.0
    assert g.allowed("heavy") == 1 and any("load" in r for r in g.throttle_reasons())
    with g.slot("heavy"):
        with g.slot("heavy"):                             # re-entrant in the same thread
            pass


def test_memory_cap_kills_child_with_reason(tmp_path):
    g = _gov(tmp_path)
    t0 = time.time()
    r = g.run([sys.executable, "-c", "import time; x = b'\\x01' * (300 << 20); time.sleep(30)"],
              kind=None, mem_cap_gb=0.1)
    assert r.killed and "memory footprint" in r.killed and "cap 0.10 GB" in r.killed
    assert r.returncode != 0 and time.time() - t0 < 15
    assert r.peak_gb > 0.1
    assert g.children() == []                             # registry cleaned up
    ok = g.run([sys.executable, "-c", "print('hi')"], kind="heavy", mem_cap_gb=0.5)
    assert ok.killed is None and ok.returncode == 0 and ok.stdout.strip() == "hi"


def test_media_hook_raises_governor_killed(tmp_path):
    from cutroom import media
    g = _gov(tmp_path, max_proc_mem_gb=0.1)
    media.set_run_hook(g.media_hook)
    with pytest.raises(media.GovernorKilled):
        media.run([sys.executable, "-c", "import time; x = b'\\x01' * (300 << 20); time.sleep(30)"])
    assert media.run([sys.executable, "-c", "print(1)"]).stdout.strip() == "1"


def test_footprint_and_watch_self(tmp_path):
    fp = governor.footprint()
    assert fp and fp > 5 * 1024 ** 2
    assert governor.footprint(999999) is None
    g = _gov(tmp_path)
    with pytest.raises(governor.MemoryGuardError):
        g.watch_self(0.001)()
    assert g.check_self(1000) > 0
    st = g.status()
    for k in ("load", "ram_free_gb", "disk_free_gb", "slots", "throttled", "limits", "children"):
        assert k in st


def test_disk_floor_pauses_instead_of_failing(tmp_path, monkeypatch):
    g = _gov(tmp_path)
    g.min_free_disk_gb = 20
    DU = namedtuple("DU", "total used free")
    free = {"gb": 5.0}
    calls = []

    def fake(path):
        calls.append(path)
        return DU(100 * governor.GB, 0, free["gb"] * governor.GB)
    monkeypatch.setattr(governor.shutil, "disk_usage", fake)
    assert not g.disk_ok() and any("disk" in b for b in g.blockers("heavy"))
    got = threading.Event()

    def writer():
        with g.slot("heavy", label="proxy"):
            g.wait_for_disk(poll=0.05)
            got.set()
    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.4)
    assert not got.is_set(), "a write started below the disk floor"
    assert g.last_wait and "disk" in g.last_wait[1]
    free["gb"] = 50.0
    t.join(timeout=5)
    assert got.is_set() and g.disk_ok()


# -- Claude reply parsing (canned output; the real claude is never called in tests) ---------------

TAGS = [{"id": "abc1234567", "summary": "Walking along a beach at sunset", "setting": "Beach",
         "time_of_day": "Sunset", "people": "creator", "creator_visible": "yes", "talking_to_camera": False,
         "actions": "walking, laughing", "shot": "Wide", "quality": 4.4, "usable": True,
         "best": [{"t": 3.5, "why": "smile"}, {"t": 999, "why": "past the end"}],
         "avoid": [{"t0": 0, "t1": 1.2, "why": "shaky start"}], "topics": ["beach", "travel"],
         "bogus_field": "dropped"},
        {"id": "def7654321", "summary": "Desk with a laptop", "people": "none", "quality": "2"}]


@pytest.mark.parametrize("raw", [
    json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "result": "Here are the tags:\n```json\n" + json.dumps(TAGS, indent=2) + "\n```\nDone.",
                "total_cost_usd": 0.12}),
    "I looked at both sheets.\n\n" + json.dumps(TAGS) + "\n\nLet me know if you need more.",
    "```\n" + json.dumps(TAGS, indent=1).replace('"quality": "2"', '"quality": "2",') + "\n```",
    json.dumps({"tags": TAGS}),
    "Clip 1: " + json.dumps(TAGS[0]) + "\nClip 2: " + json.dumps(TAGS[1]),
])
def test_parse_tag_output_handles_messy_replies(raw):
    got = overnight.parse_tag_output(raw)
    assert [o["id"] for o in got] == ["abc1234567", "def7654321"]


def test_parse_tag_output_garbage():
    assert overnight.parse_tag_output("Sorry, I could not read the images.") == []
    assert overnight.parse_tag_output("") == []


def test_validate_tag_coerces_fields():
    v = overnight.validate_tag(TAGS[0], duration=12.0)
    assert "bogus_field" not in v and "id" not in v
    assert v["time_of_day"] == "golden" and v["setting"] == "Beach" and v["shot"] == "wide"
    assert v["creator_visible"] is True and v["talking_to_camera"] is False and v["usable"] is True
    assert v["actions"] == ["walking", "laughing"] and v["quality"] == 4
    assert v["best"] == [{"t": 3.5, "why": "smile"}]
    assert v["avoid"] == [{"t0": 0.0, "t1": 1.2, "why": "shaky start"}]
    assert overnight.validate_tag({"quality": "9"}, 5)["quality"] == 5
    assert overnight.validate_tag({"nonsense": 1}, 5) == {}


def test_usage_limit_detection():
    assert overnight.LIMIT_PAT.search("Claude AI usage limit reached|1760000000")
    assert overnight.LIMIT_PAT.search("You've hit your limit · resets 3am")
    assert not overnight.LIMIT_PAT.search("all good")


# -- overnight dry run on the synthetic library ------------------------------------------------

def test_overnight_dry_run_end_to_end(project, tmp_path, monkeypatch):
    monkeypatch.setenv("CUTROOM_MIN_FREE_RAM_GB", "0.5")
    cfg = init_project(tmp_path / "work", project["cfg"].library_roots)
    cfg.workers, cfg.max_load, cfg.min_free_disk_gb = 2, 1000.0, 0.0
    cfg.save()
    # a plan that uses one clip: it must be processed first
    first = project["ids"]["dark"]
    (cfg.p("plans") / "test_v1.json").write_text(json.dumps({"title": "t", "sections": [
        {"name": "a", "segments": [{"type": "broll", "clip": first, "in": 0, "out": 2}]}]}))
    overnight.note_usage(cfg, [project["ids"]["vertical"]])
    rc = overnight.run(cfg, dry_run=True, model="tiny", echo=False)
    assert rc == 0
    st = overnight.read_state(cfg)
    assert st["finished"] and st["status"] == "finished" and not st["running"]
    for name in overnight.STAGES:
        assert st["stages"][name]["status"] in ("done", "dry-run"), (name, st["stages"][name])
    assert st["stages"]["tag"]["status"] == "dry-run"
    assert st["stages"]["proxies"]["done"] == 5 and st["stages"]["analyze"]["done"] == 5

    from cutroom import db
    con = db.connect(cfg.db_path)
    assert con.execute("SELECT COUNT(*) FROM clips WHERE status='analyzed'").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM clips WHERE sheet IS NOT NULL").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0          # dry run: nothing tagged
    rep = overnight.report_path(cfg).read_text()
    assert "# Overnight report" in rep and "h understood" in rep and "| tag | dry-run |" in rep
    prompt = (cfg.p("overnight") / "tag_prompt_preview.txt").read_text()
    assert "Read tool" in prompt and str(cfg.p("sheets")) in prompt and '"summary"' in prompt
    # recently used clips go first
    order = [ln.split("id: ")[1].strip() for ln in prompt.splitlines() if ln.strip().startswith(tuple("123456789"))
             and "id: " in ln]
    assert order[:2] == [project["ids"]["vertical"], first]
    log = (cfg.p("overnight") / "overnight.log").read_text()
    assert "stage process" in log and "overnight finished" in log
    assert not (cfg.p("overnight") / "overnight.pid").exists()
    # a second run continues from the database: nothing left to do
    assert overnight.run(cfg, dry_run=True, model="tiny", echo=False) == 0
    st = overnight.read_state(cfg)
    assert st["stages"]["proxies"]["total"] == 0 and st["stages"]["analyze"]["total"] == 0
    assert "unchanged" in st["stages"]["matchcuts"].get("note", "")


def test_failures_skip_after_two(tmp_path):
    cfg = Config(work=str(tmp_path))
    assert overnight.record_failure(cfg, "analyze", "c1", "boom") == 1
    assert overnight.record_failure(cfg, "analyze", "c1", "boom") == 2
    assert overnight.load_failures(cfg)["counts"]["analyze:c1"] == 2


def test_stop_and_status_without_run(tmp_path, capsys):
    cfg = Config(work=str(tmp_path))
    assert overnight.stop(cfg) is False
    overnight.print_status(cfg)
    assert "has not run" in capsys.readouterr().out


# -- Codex tagging (a fake `codex` that writes a canned tag array to the -o file) -----------------

FAKE_CODEX_TAG = r'''#!{py}
import json, os, re, sys
argv = sys.argv[1:]
if argv[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read()
log = os.environ["FAKE_CODEX_LOG"]
schema = json.load(open(argv[argv.index("--output-schema") + 1]))
with open(log, "a") as f:
    f.write(json.dumps({{"argv": argv, "prompt": prompt, "schema": schema}}) + "\n")
def out(o):
    print(json.dumps(o), flush=True)
out({{"type": "thread.started", "thread_id": "t-1"}})
out({{"type": "turn.started"}})
if os.environ.get("FAKE_CODEX_MODE") == "limit":
    out({{"type": "error", "message": "You've hit your usage limit. Try again at 3:00 AM."}})
    out({{"type": "turn.failed", "error": {{"message": "You've hit your usage limit. Try again at 3:00 AM."}}}})
    sys.exit(1)
ids = re.findall(r"id: (\S+)", prompt)
tags = [{{"id": i, "summary": "Test pattern on a screen", "setting": "studio", "time_of_day": "indoor",
          "people": "none", "creator_visible": False, "talking_to_camera": None, "actions": None, "shot": "Wide",
          "camera": "locked", "mood": ["calm"], "quality": 4, "usable": True, "best": [{{"t": 2.0, "why": "clean"}}],
          "avoid": None, "topics": ["test"], "notes": None}} for i in ids]
reply = json.dumps({{"tags": tags}})
open(argv[argv.index("-o") + 1], "w").write(reply)
out({{"type": "item.completed", "item": {{"id": "item_0", "type": "agent_message", "text": reply}}}})
out({{"type": "turn.completed", "usage": {{"input_tokens": 5000, "cached_input_tokens": 0, "output_tokens": 300}}}})
'''


def _tag_project(tmp_path, n=3):
    from cutroom import db
    cfg = init_project(tmp_path / "work", [])
    cfg.max_load, cfg.min_free_disk_gb, cfg.min_free_ram_gb, cfg.tag_sleep_s = 1000.0, 0.0, 0.01, 0
    cfg.tag_batch = 20                                     # Codex caps a call at 8 images anyway
    cfg.save()
    con = db.connect(cfg.db_path)
    ids = []
    for i in range(n):
        sheet = cfg.p("sheets", f"c{i}.jpg")
        sheet.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        cid = f"clip{i:06d}"
        db.upsert(con, "clips", {"id": cid, "path": f"/footage/c{i}.mp4", "rel": f"c{i}.mp4", "duration": 8.0,
                                 "width": 1920, "height": 1080, "rotation": 0, "has_audio": 0, "is_log": 0,
                                 "status": "analyzed", "sheet": str(sheet), "updated": time.time()}, key=("id",))
        ids.append(cid)
    con.commit()
    return cfg, con, ids


def _codex_env(tmp_path, monkeypatch, mode="ok"):
    stub = tmp_path / "codex"
    stub.write_text(FAKE_CODEX_TAG.format(py=sys.executable))
    stub.chmod(0o755)
    log = tmp_path / "codex_tag_calls.jsonl"
    monkeypatch.setenv("CUTROOM_CODEX_BIN", str(stub))
    monkeypatch.setenv("CUTROOM_AGENT", "codex")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("CUTROOM_MIN_FREE_RAM_GB", "0.01")
    return log


def test_codex_overnight_tagging(tmp_path, monkeypatch):
    from cutroom import db
    log = _codex_env(tmp_path, monkeypatch)
    cfg, con, ids = _tag_project(tmp_path, n=10)
    pipe = overnight.Pipeline(cfg, {"tag": True}, echo=False)
    assert pipe.agent.name == "codex" and pipe.agent_missing() is None
    assert pipe.tag_loop(until_idle=True) == "done"
    for cid in ids:
        t = db.get_tags(con, cid)
        assert t["tagged_by"] == "codex-overnight" and t["shot"] == "wide" and t["quality"] == 4
        assert t["best"] == [{"t": 2.0, "why": "clean"}] and "avoid" not in t and "notes" not in t
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert [c["argv"].count("-i") for c in calls] == [8, 2]          # batches of at most 8 sheets
    a = calls[0]["argv"]
    assert a[0] == "exec" and a[-1] == "-" and "--ephemeral" in a and "--json" in a and "--skip-git-repo-check" in a
    assert a[a.index("-s") + 1] == "read-only" and "-o" in a and "--output-schema" in a
    imgs = [a[i + 1] for i, x in enumerate(a) if x == "-i"]
    assert all(Path(p).suffix == ".jpg" and Path(p).is_absolute() for p in imgs)
    assert "attached to this message" in calls[0]["prompt"] and "Read tool" not in calls[0]["prompt"]
    sch = calls[0]["schema"]
    item = sch["properties"]["tags"]["items"]
    assert sch["required"] == ["tags"] and set(item["required"]) == set(item["properties"]) and "id" in item["required"]
    assert not list(cfg.p("overnight", "codex").glob("*"))           # temp schema / reply files cleaned up
    st = pipe.stage_mem["tag"]
    assert st["calls"] == 2 and st["usage"]["output_tokens"] == 600 and st["agent"] == "codex"


def test_codex_usage_limit_stops_tagging(tmp_path, monkeypatch):
    from cutroom import db
    _codex_env(tmp_path, monkeypatch, mode="limit")
    cfg, con, ids = _tag_project(tmp_path, n=2)
    pipe = overnight.Pipeline(cfg, {"tag": True}, echo=False)
    assert pipe.tag_loop(until_idle=True) == "usage limit"
    assert all(not db.get_tags(con, c) for c in ids)
    assert "usage limit" in pipe.stage_mem["tag"]["last_error"]


def test_tagging_skipped_when_codex_not_logged_in(tmp_path, monkeypatch):
    stub = tmp_path / "codex"
    stub.write_text(f"#!{sys.executable}\nimport sys\nprint('Not logged in')\nsys.exit(1)\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CUTROOM_CODEX_BIN", str(stub))
    monkeypatch.setenv("CUTROOM_AGENT", "codex")
    cfg, con, ids = _tag_project(tmp_path, n=1)
    pipe = overnight.Pipeline(cfg, {"tag": True}, echo=False)
    assert pipe.agent_missing() == "codex not logged in"


def _waiting_labels(gov, kind="heavy"):
    out = set()
    for f in gov._p("waiting", "x").parent.glob(f"{kind}.*.json"):
        try:
            out.add(json.loads(f.read_text()).get("label"))
        except (OSError, ValueError):
            pass
    return out


def _until(cond, timeout=10.0, what="condition"):
    """Wait for a condition (a synchronisation point, not a guess at timing); fail loudly on timeout."""
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, f"timed out waiting for {what}"
        time.sleep(0.005)


def test_slot_is_fifo_between_threads_of_one_process(tmp_path, monkeypatch):
    """A thread that has been waiting for a slot is never overtaken by one that started waiting later
    (in-process waiters used to race for a freed slot, so queued work could start out of order)."""
    monkeypatch.setenv("CUTROOM_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("CUTROOM_MIN_FREE_RAM_GB", "0.01")
    cfg = init_project(tmp_path / "work", [])
    cfg.heavy_slots, cfg.max_load, cfg.min_free_disk_gb = 1, 1000.0, 0.0
    cfg.save()
    gov = governor.Governor(cfg)
    gov.poll_s = 0.01
    order, lock = [], threading.Lock()

    def take(label):
        with gov.slot("heavy", label=label):
            with lock:
                order.append(label)

    labels = [f"w{i}" for i in range(4)]
    threads = []
    with gov.slot("heavy", label="holder"):
        for lab in labels:                          # each one provably queued before the next arrives
            t = threading.Thread(target=take, args=(lab,))
            t.start()
            threads.append(t)
            _until(lambda: lab in _waiting_labels(gov), what=f"{lab} to queue")
    for t in threads:
        t.join(10)
    assert order == labels, order


def test_stop_takes_effect_between_items_even_for_queued_work(tmp_path, monkeypatch):
    """`overnight --stop` (the STOP file): items queued for a governor slot must not start afterwards;
    only the one already running finishes. Synchronised on events and the governor's own waiter files:
    c0 holds the only heavy slot, the other in-flight items are provably queued for it, then STOP lands."""
    import threading
    monkeypatch.setenv("CUTROOM_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("CUTROOM_MIN_FREE_RAM_GB", "0.01")
    cfg = init_project(tmp_path / "work", [])
    cfg.heavy_slots, cfg.max_load, cfg.min_free_disk_gb = 1, 1000.0, 0.0
    cfg.save()
    pipe = overnight.Pipeline(cfg, {"run_id": "t"}, echo=False)
    pipe.gov.poll_s = 0.01
    pipe.gov.abort_check = pipe.stop_reason
    started, lock = [], threading.Lock()
    c0_in = threading.Event()
    workers = 3

    def fn(r):
        if r["id"] != "c0":
            assert c0_in.wait(10), "c0 never took the slot"
        with pipe.gov.slot("heavy", label=r["id"]):
            with lock:
                started.append(r["id"])
            if r["id"] == "c0":
                c0_in.set()
                # the user stops the night while c0 encodes, once the other in-flight items wait for the slot
                _until(lambda: _waiting_labels(pipe.gov) >= {f"c{i}" for i in range(1, workers)},
                       what="the queued items to wait for the slot")
                (pipe.dir / "STOP").touch()
        return r["id"]
    rows = [{"id": f"c{i}", "duration": 1.0, "rel": f"c{i}"} for i in range(5)]
    pipe.stage_begin("proxies", total=len(rows))
    with pytest.raises(overnight.Stopped):
        pipe.run_items("proxies", rows, fn, lambda r, res: None, lambda r, e: None, workers=workers)
    pipe.gov.abort_check = None
    assert started == ["c0"], started
    assert pipe.stage_mem["proxies"]["done"] == 1 and not pipe.stage_mem["proxies"].get("errors")
