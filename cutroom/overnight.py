"""Overnight understanding: index, transcribe, analyse, mine match cuts and have the AI agent (Claude Code
or Codex, see agent.py) tag every clip,
unattended, resumable, inside the resource governor's limits.

    cutroom overnight [--until 07:00] [--no-tag] [--tag-limit N] [--model small|medium] [--dry-run]
    cutroom overnight --status | --stop

Pipeline (each stage resumes from the database; finished work is never redone):
    scan -> probe -> proxies -> sheets -> analyze -> transcribe (speech first) -> songs -> matchcuts -> tag
Clips the creator touched recently (searches, `cutroom clip`, plans, studio markers) go first, then
longer clips / clips with more speech. Tagging starts as soon as analysis is done and runs next to
transcription (it costs the machine almost nothing: the agent only looks at the contact-sheet JPEGs).
Claude Code opens the sheets with its Read tool; Codex gets them attached (-i, at most 8 per call) and
answers in a JSON schema (--output-schema, read back from -o). Usage limits on either stop the stage cleanly.

Process model
    supervisor (this command)   keeps the Mac awake (caffeinate -i), writes state.json, watches the
                                stage process's memory, restarts it after a crash with back-off, and
                                gives up on a clip that was in flight during two crashes.
    stage process (child)       runs the stages, niced, through governor slots.

Files in <work>/overnight/: state.json (live progress), overnight.log, overnight.pid, REPORT.md
(the morning report), failures.json, inflight.json, STOP (graceful-stop request).

Studio API: start_background(cfg, ...) -> pid · stop(cfg) · read_state(cfg) · report_path(cfg) ·
note_usage(cfg, clip_ids) (call when the user searches/opens clips so they are processed first).
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from . import agent as agentmod
from . import db, governor

STAGES = ["scan", "probe", "proxies", "sheets", "analyze", "transcribe", "songs", "matchcuts", "tag"]
EXIT_DONE, EXIT_STOPPED, EXIT_MEMORY = 0, 3, 4
MAX_RESTARTS = 20
MAX_CLIP_FAILURES = 2
TAGGER = "claude-overnight"                     # tagged_by for Claude Code; Codex writes "codex-overnight"
TAGGERS = {"claude": "claude-overnight", "codex": "codex-overnight"}
REPO = Path(__file__).resolve().parents[1]


# ==================================================================================================
# files + shared state
# ==================================================================================================

def odir(cfg) -> Path:
    d = cfg.p("overnight")
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_path(cfg) -> Path:
    return odir(cfg) / "REPORT.md"


def _jload(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _jsave(path, data):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


@contextlib.contextmanager
def _flocked(path):
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def merge_state(cfg, updates: dict, stage_updates: dict = None) -> dict:
    """Read-modify-write state.json under a lock (the supervisor and the stage process both write)."""
    d = odir(cfg)
    with _flocked(d / ".state.lock"):
        st = _jload(d / "state.json", {})
        st.update(updates)
        if stage_updates:
            stages = st.setdefault("stages", {})
            for name, fields in stage_updates.items():
                stages.setdefault(name, {}).update(fields)
        st["updated"] = time.time()
        _jsave(d / "state.json", st)
        return st


def read_state(cfg) -> dict:
    """state.json plus `running` (supervisor alive?)."""
    st = _jload(odir(cfg) / "state.json", {})
    pid = _read_pid(cfg)
    st["running"] = bool(pid and governor.pid_alive(pid))
    return st


def _read_pid(cfg):
    try:
        return int((odir(cfg) / "overnight.pid").read_text().strip())
    except (OSError, ValueError):
        return None


class _Log:
    def __init__(self, cfg, echo=True):
        self.path = odir(cfg) / "overnight.log"
        self.echo = echo
        self.mx = threading.Lock()

    def __call__(self, msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        with self.mx:
            with open(self.path, "a") as f:
                f.write(line + "\n")
            if self.echo:
                print(line, flush=True)


# ==================================================================================================
# priority + failures
# ==================================================================================================

def note_usage(cfg, ids):
    """Record that the user searched for / opened these clips (they are processed first overnight)."""
    ids = [str(i) for i in ids if i][:50]
    if not ids:
        return
    try:
        with open(odir(cfg) / "usage.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "ids": ids}) + "\n")
    except OSError:
        pass


def _walk_clip_refs(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "clip" and isinstance(v, str):
                out.append(v)
            else:
                _walk_clip_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_clip_refs(v, out)


def recent_clip_ids(cfg, con, max_plans=30) -> list:
    """Clip ids the user touched recently, most recent first: usage log, plans, studio context/markers."""
    events = []                                       # (time, [refs])
    up = odir(cfg) / "usage.jsonl"
    if up.exists():
        try:
            for line in up.read_text().splitlines()[-2000:]:
                d = json.loads(line)
                events.append((d.get("t", 0), d.get("ids", [])))
        except (OSError, ValueError):
            pass
    plans = sorted(cfg.p("plans").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_plans]
    for p in plans:
        refs = []
        _walk_clip_refs(_jload(p, {}), refs)
        events.append((p.stat().st_mtime, refs))
    for name in ("context.json", "markers.json"):
        p = cfg.p("studio", name)
        if p.exists():
            refs = []
            _walk_clip_refs(_jload(p, {}), refs)
            events.append((p.stat().st_mtime, refs))
    out, seen = [], set()
    for _, refs in sorted(events, key=lambda e: -e[0]):
        for ref in refs:
            if ref in seen:
                continue
            seen.add(ref)
            try:
                cid = db.get_clip(con, ref)["id"]
            except (KeyError, sqlite_error()):
                continue
            if cid not in out:
                out.append(cid)
    return out


def sqlite_error():
    import sqlite3
    return sqlite3.Error


def load_failures(cfg) -> dict:
    return _jload(odir(cfg) / "failures.json", {"counts": {}, "reasons": {}})


def record_failure(cfg, stage, cid, reason) -> int:
    d = odir(cfg)
    with _flocked(d / ".failures.lock"):
        f = _jload(d / "failures.json", {"counts": {}, "reasons": {}})
        key = f"{stage}:{cid}"
        f["counts"][key] = f["counts"].get(key, 0) + 1
        f["reasons"][key] = str(reason)[-300:]
        _jsave(d / "failures.json", f)
        return f["counts"][key]


# ==================================================================================================
# AI tagging: prompt, call, parse, validate
# ==================================================================================================

ENUM_MAP = {
    "time_of_day": {"day": "day", "daytime": "day", "morning": "day", "afternoon": "day", "midday": "day",
                    "golden": "golden", "golden hour": "golden", "sunset": "golden", "sunrise": "golden",
                    "dusk": "golden", "evening": "golden", "night": "night", "nighttime": "night",
                    "indoor": "indoor", "indoors": "indoor", "interior": "indoor"},
}
BOOL_FIELDS = ("creator_visible", "talking_to_camera", "usable")
LIST_FIELDS = ("actions", "mood", "topics")
STR_FIELDS = {"summary": 300, "setting": 60, "people": 80, "shot": 30, "camera": 30, "notes": 500, "time_of_day": 20}
LIMIT_PAT = agentmod.LIMIT_PAT


def claude_bin() -> str | None:
    """The real `claude` executable (shell aliases are invisible to subprocesses, so PATH lookup
    finds the binary itself). CUTROOM_CLAUDE_BIN / CUTROOM_CLAUDE override."""
    return agentmod.find_claude()


def _metrics_line(con, cid) -> str:
    from . import analyze
    q = analyze.clip_quality(con, cid)
    if not q:
        return "not analyzed"
    return (f"shake {q['shake']:.1f} (steady<3, shaky>8) · exposure {q['exposure']:.0f}/255 · "
            f"sharpness {q['sharpness']:.0f} · faces up to {q['faces']} · auto-quality {analyze.auto_quality(q)}/5")


def build_tag_prompt(cfg, con, rows, agent="claude") -> str:
    """The tagging prompt. Claude Code opens each sheet with its Read tool; Codex gets the sheets attached
    as images (same order as the clips) and must answer in the {"tags": [...]} JSON schema."""
    from .ingest import display_size
    fields = "\n".join(f'  "{k}": {v}' for k, v in db.TAG_FIELDS.items())
    lines = []
    for n, r in enumerate(rows, 1):
        tr = con.execute("SELECT text, n_words FROM transcripts WHERE clip_id=?", (r["id"],)).fetchone()
        if tr and (tr["n_words"] or 0) > 0:
            speech = '"' + " ".join((tr["text"] or "").split())[:400] + '"'
        elif tr:
            speech = "(no speech)"
        else:
            speech = "(not transcribed yet)" if r["has_audio"] else "(no audio track)"
        w, h = display_size(r)
        lines.append(f"{n}. id: {r['id']}\n   file: {r['rel']}\n   duration: {float(r['duration'] or 0):.1f}s · "
                     f"{w}x{h}{' · LOG footage (flat colours are expected)' if r['is_log'] else ''}\n"
                     f"   metrics: {_metrics_line(con, r['id'])}\n   speech: {speech}\n"
                     f"   contact sheet: {Path(r['sheet']).resolve()}")
    if agent == "codex":
        look = (f"The {len(rows)} contact-sheet images attached to this message are the clips below, in the same order\n"
                "(image 1 = clip 1). Look at each one carefully. A sheet is a grid of frames sampled across the whole\n"
                "clip; each tile is labelled with its timestamp (seconds) in yellow, and the header shows the file name\n"
                "and format. Do not run commands or open other files.")
        reply = ('Then reply with ONLY a JSON object {"tags": [...]}: one object per clip, same order, each with\n'
                 '"id" (the clip id) plus these fields (use null for anything you cannot tell rather than guess):')
    else:
        look = ("For EACH clip below, use the Read tool to open its contact-sheet JPEG and look at it carefully. A sheet is a\n"
                "grid of frames sampled across the whole clip; each tile is labelled with its timestamp (seconds) in yellow,\n"
                "and the header shows the file name and format. Read only the sheet files listed here, nothing else.")
        reply = ("Then reply with ONLY a JSON array (no prose, no markdown fences): one object per clip, same order, each with\n"
                 '"id" (the clip id) plus these fields (all optional; leave a field out rather than guess):')
    return f"""You are tagging clips from a creator's personal video library so an editor can find shots later.

{look}

{reply}
{fields}

Rules:
- Describe what is visible in the frames (and said in the speech snippet). Never invent names of people or places.
- "people": use 'creator' for the person the footage is about (usually the one holding the camera / talking to
  it / appearing across many clips), otherwise 'friend', 'strangers', 'crowd' or 'none'.
- "best" / "avoid" times come from the tile labels (seconds from clip start).
- "quality" 1-5 reflects focus, exposure, framing and shake; the metrics line is a hint, the frames decide.
- "usable": false only for accidental / broken / unflattering clips or ones where strangers are the subject.
- Keep strings short and concrete; lists of 1-6 lowercase words.

Clips:
{chr(10).join(lines)}
"""


def _strip_fences(text: str) -> str:
    m = re.findall(r"```(?:json|JSON)?\s*\n?(.*?)```", text, re.S)
    return "\n".join(m) if m else text


def _loads_lenient(s):
    try:
        return json.loads(s)
    except ValueError:
        return json.loads(re.sub(r",\s*([\]}])", r"\1", s))       # trailing commas


def parse_tag_output(text) -> list:
    """Extract the list of tag objects from Claude's reply (envelope JSON, fenced, or mixed with prose)."""
    if isinstance(text, (list, dict)):
        obj = text
    else:
        text = (text or "").strip()
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
    if isinstance(obj, dict) and "result" in obj and isinstance(obj.get("result"), str):
        return parse_tag_output(obj["result"])             # claude --output-format json envelope
    if obj is not None:
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
        if isinstance(obj, dict):
            for k in ("tags", "clips", "results", "items"):
                if isinstance(obj.get(k), list):
                    return [o for o in obj[k] if isinstance(o, dict)]
            return [obj] if "id" in obj else []
    body = _strip_fences(text)
    for cand in (body, text):
        try:
            v = _loads_lenient(cand.strip())
            if isinstance(v, list):
                return [o for o in v if isinstance(o, dict)]
        except ValueError:
            pass
    dec = json.JSONDecoder()
    best = []
    for src in (body, text):
        for m in re.finditer(r"\[", src):
            frag = src[m.start():]
            for attempt in (frag, re.sub(r",\s*([\]}])", r"\1", frag)):
                try:
                    v, _ = dec.raw_decode(attempt)
                except ValueError:
                    continue
                if (isinstance(v, list) and v and all(isinstance(o, dict) for o in v)
                        and any("id" in o for o in v) and len(v) > len(best)):
                    best = v
                break
        if best:
            return best
    objs = []                                             # last resort: loose {...} objects with an id
    for m in re.finditer(r"\{", text):
        try:
            v, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(v, dict) and "id" in v and not any(v is o for o in objs):
            objs.append(v)
    return objs


def _as_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def _as_float(v):
    try:
        return float(str(v).strip().rstrip("s"))
    except (TypeError, ValueError):
        return None


def validate_tag(obj: dict, duration: float) -> dict:
    """Keep only db.TAG_FIELDS with sane types/values. Returns {} if nothing usable survived."""
    dur = float(duration or 0) or 1e9
    out = {}
    for k, v in obj.items():
        if k not in db.TAG_FIELDS or v is None or v == "":
            continue
        if k in BOOL_FIELDS:
            b = _as_bool(v)
            if b is not None:
                out[k] = b
        elif k in LIST_FIELDS:
            items = v if isinstance(v, list) else re.split(r"[,;/]", str(v))
            items = [str(x).strip().lower()[:40] for x in items if str(x).strip()]
            if items:
                out[k] = items[:12]
        elif k == "quality":
            q = _as_float(v)
            if q is not None:
                out[k] = int(min(5, max(1, round(q))))
        elif k == "best":
            items = []
            for x in (v if isinstance(v, list) else [v]):
                t = _as_float(x.get("t")) if isinstance(x, dict) else _as_float(x)
                if t is not None and 0 <= t <= dur + 0.5:
                    items.append({"t": round(t, 2), "why": str(x.get("why", "") if isinstance(x, dict) else "")[:160]})
            if items:
                out[k] = items[:8]
        elif k == "avoid":
            items = []
            for x in (v if isinstance(v, list) else [v]):
                if not isinstance(x, dict):
                    continue
                t0, t1 = _as_float(x.get("t0")), _as_float(x.get("t1"))
                if t0 is not None and t1 is not None and 0 <= t0 < t1 <= dur + 0.5:
                    items.append({"t0": round(t0, 2), "t1": round(t1, 2), "why": str(x.get("why", ""))[:160]})
            if items:
                out[k] = items[:8]
        elif k in STR_FIELDS:
            s = " ".join(str(v if not isinstance(v, list) else ", ".join(map(str, v))).split())[:STR_FIELDS[k]]
            if k == "time_of_day":
                s = ENUM_MAP["time_of_day"].get(s.lower(), s.lower())
            elif k in ("shot", "camera"):
                s = s.lower()
            if s:
                out[k] = s
    return out


def call_agent(cfg, ag, prompt, images=(), timeout=None, gov=None) -> dict:
    """One headless tagging call on the chosen agent (agent.py). Returns
    {ok, text, cost_usd, usage, limit, error, seconds}."""
    gov = gov or governor.for_cfg(cfg)
    return ag.tag(cfg, prompt, images, gov, timeout or getattr(cfg, "tag_timeout_s", 900))


def call_claude(cfg, prompt, timeout=None, gov=None) -> dict:
    """One headless Claude Code call (Read tool only) in the project folder. Returns
    {ok, text, cost_usd, usage, limit, error, seconds}."""
    return call_agent(cfg, agentmod.ClaudeAgent(claude_bin()), prompt, (), timeout, gov)


# ==================================================================================================
# the stage process
# ==================================================================================================

class Stopped(Exception):
    pass


class RestartForMemory(Exception):
    pass


class Pipeline:
    def __init__(self, cfg, opts, echo=True):
        from .config import Config
        self.cfg = cfg if isinstance(cfg, Config) else Config.load(cfg)
        self.opts = opts
        self.con = db.connect(self.cfg.db_path)
        self.gov = governor.for_cfg(self.cfg)
        self.log = _Log(self.cfg, echo=echo)
        self.dir = odir(self.cfg)
        self.until = opts.get("until_ts")
        self.run_id = opts.get("run_id")
        self.mx = threading.Lock()
        self.inflight = {}
        self._last_save = 0.0
        self.stage_mem = {}
        self.parent = os.getppid()
        self.rank = {}
        fails = load_failures(self.cfg)["counts"]
        self.fail_counts = fails
        self.tag_thread = None
        self.tag_error = None
        self.tag_result = None
        self._main_done = False
        self._tl = threading.local()
        self._agent = None

    @property
    def agent(self):
        """The tagging agent (Claude Code or Codex, agent.choose), decided once per stage process."""
        if self._agent is None:
            self._agent = agentmod.choose(self.cfg)
        return self._agent

    def agent_missing(self):
        """Why tagging cannot run on this machine, or None."""
        ag = self.agent
        if not ag.path:
            return f"{ag.name} not found"
        if ag.name == "codex" and ag.logged_in is False:
            return "codex not logged in"
        return None

    def thread_con(self):
        """A sqlite connection for the calling thread (sqlite objects are per-thread)."""
        if threading.current_thread() is threading.main_thread():
            return self.con
        c = getattr(self._tl, "con", None)
        if c is None:
            c = self._tl.con = db.connect(self.cfg.db_path)
        return c

    # -- control ------------------------------------------------------------------------------------
    def stop_reason(self):
        if (self.dir / "STOP").exists():
            return "stop requested"
        if self.until and time.time() >= self.until:
            return "reached --until time"
        if self.opts.get("supervised") and os.getppid() != self.parent:
            return "supervisor exited"
        return None

    def check_stop(self):
        r = self.stop_reason()
        if r:
            raise Stopped(r)

    def skipped(self, stage, cid):
        return self.fail_counts.get(f"{stage}:{cid}", 0) >= MAX_CLIP_FAILURES

    def suspect(self, stage, cid):
        return self.fail_counts.get(f"{stage}:{cid}", 0) == 1

    def order(self, rows, key2):
        self.refresh_rank()                  # new clips may have been scanned since the last stage
        big = len(self.rank) + 1
        return sorted(rows, key=lambda r: (self.rank.get(r["id"], big), key2(r)))

    def _inflight_set(self, stage, cid, on):
        with self.mx:
            k = f"{stage}:{cid}"
            if on:
                self.inflight[k] = {"stage": stage, "clip": cid, "since": time.time()}
            else:
                self.inflight.pop(k, None)
            _jsave(self.dir / "inflight.json", list(self.inflight.values()))

    def progress(self, stage, force=False, **fields):
        now = time.time()
        with self.mx:
            self.stage_mem.setdefault(stage, {}).update(fields)
            if not force and now - self._last_save < 1.0:
                return
            self._last_save = now
            w = self.gov.last_wait
            merge_state(self.cfg, {"child_pid": os.getpid(), "throttle": self.gov.throttle_reasons(),
                                   "waiting": {"kind": w[0], "reason": w[1], "since": w[2]} if w else None,
                                   "current": list(self.inflight.values())},
                        {k: dict(v) for k, v in self.stage_mem.items()})

    def refresh_rank(self):
        self.rank = {cid: i for i, cid in enumerate(recent_clip_ids(self.cfg, self.thread_con()))}

    def stage_begin(self, stage, total=0, weight_total=0.0, **kw):
        self.log(f"stage {stage}: {total} item(s)" + (f", {weight_total / 3600:.2f} h of footage" if weight_total else ""))
        merge_state(self.cfg, {"stage": stage})
        self.progress(stage, force=True, status="running", total=total, done=0, errors=0, weight_total=weight_total,
                      weight_done=0.0, started=time.time(), finished=None, rate_per_min=None, eta_s=None,
                      run_id=self.run_id, **kw)

    def stage_end(self, stage, status="done", **kw):
        self.progress(stage, force=True, status=status, finished=time.time(), eta_s=0 if status == "done" else None, **kw)
        self.log(f"stage {stage}: {status}" + (f" ({kw})" if kw else ""))

    def stage_done_this_run(self, stage):
        st = read_state(self.cfg).get("stages", {}).get(stage, {})
        return st.get("run_id") == self.run_id and st.get("status") in ("done", "skipped", "dry-run")

    # -- generic item runner ------------------------------------------------------------------------
    def run_items(self, stage, rows, fn, on_ok, on_err, workers=1, weight=lambda r: float(r["duration"] or 0)):
        """Run fn(row) over rows (already ordered) with at most `workers` in flight, suspects (one earlier
        crash) first and one at a time. Checks stop/--until between items; keeps progress current."""
        rows = [r for r in rows if not self.skipped(stage, r["id"])]
        suspects = [r for r in rows if self.suspect(stage, r["id"])]
        rest = [r for r in rows if not self.suspect(stage, r["id"])]
        st = self.stage_mem.get(stage, {})
        done, errors, wdone = st.get("done", 0), st.get("errors", 0), st.get("weight_done", 0.0)
        wtotal = st.get("weight_total") or sum(weight(r) for r in rows)
        t0 = time.time()
        w0 = wdone

        def finish(r, fut):
            nonlocal done, errors, wdone
            try:
                res = fut.result()
                on_ok(r, res)
            except (MemoryError, RestartForMemory) as e:
                raise RestartForMemory(str(e)) from None
            except Exception as e:  # noqa: BLE001 - one clip never stops the night
                errors += 1
                on_err(r, e)
                self.progress(stage, last_error=f"{r['id']}: {type(e).__name__}: {str(e)[-200:]}")
            finally:
                self._inflight_set(stage, r["id"], False)
            done += 1
            wdone += weight(r)
            el = time.time() - t0
            rate = (wdone - w0) / el if el > 0 else 0
            self.progress(stage, done=done, errors=errors, weight_done=round(wdone, 1),
                          rate_per_min=round(done / max(el, 1e-6) * 60, 2) if el > 5 else None,
                          eta_s=round((wtotal - wdone) / rate) if rate > 0 else None)

        for batch, n in ((suspects, 1), (rest, workers)):
            if not batch:
                continue
            with ThreadPoolExecutor(max(1, n)) as ex:
                pending = {}
                it = iter(batch)
                exhausted = False
                while True:
                    while not exhausted and len(pending) < n:
                        if self.stop_reason():
                            exhausted = True
                            break
                        r = next(it, None)
                        if r is None:
                            exhausted = True
                            break
                        self._inflight_set(stage, r["id"], True)
                        pending[ex.submit(fn, r)] = r
                    if not pending:
                        break
                    fin, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for f in fin:
                        finish(pending.pop(f), f)
        self.check_stop()

    # -- stages ---------------------------------------------------------------------------------------
    def st_scan(self):
        from . import ingest
        self.stage_begin("scan", total=1)
        res = ingest.scan(self.cfg, self.con)
        self.stage_end("scan", done=1, found=res.get("files"), new=res.get("new"))

    def st_probe(self):
        from . import ingest
        n = self.con.execute("SELECT COUNT(*) FROM clips WHERE status='new'").fetchone()[0]
        self.stage_begin("probe", total=n)
        ingest.probe_all(self.cfg, self.con)
        self.stage_end("probe", done=n)

    def _err(self, stage, mark_status=True):
        def on_err(r, e):
            from . import media
            if isinstance(e, media.GovernorKilled):
                self.log(f"  ! {stage} {r['rel']}: {e} (will retry)")
                return
            if mark_status:
                self.con.execute("UPDATE clips SET status='error', error=?, updated=? WHERE id=?",
                                 (f"{stage}: {str(e)[-300:]}", time.time(), r["id"]))
            else:
                self.con.execute("UPDATE clips SET error=? WHERE id=?", (f"{stage}: {str(e)[-300:]}", r["id"]))
            self.con.commit()
            self.log(f"  ! {stage} {r['rel']}: {str(e)[-200:]}")
        return on_err

    def st_proxies(self):
        from . import ingest, media
        rows = self.con.execute("SELECT * FROM clips WHERE status='probed' AND proxy IS NULL").fetchall()
        rows = self.order(rows, lambda r: -(r["duration"] or 0))
        self.stage_begin("proxies", total=len(rows), weight_total=sum(r["duration"] or 0 for r in rows))
        if not rows:
            return self.stage_end("proxies")
        for stale in self.cfg.p("proxies").glob("*.tmp.mp4"):
            stale.unlink(missing_ok=True)
        enc = media.hw_encoder(self.cfg)

        def ok(r, p):
            self.con.execute("UPDATE clips SET proxy=?, status='proxied', error=NULL, updated=? WHERE id=?",
                             (p, time.time(), r["id"]))
            self.con.commit()
        self.run_items("proxies", rows, lambda r: ingest._make_proxy(self.cfg, r, enc), ok, self._err("proxies"),
                       workers=self.gov.heavy_slots)
        self.stage_end("proxies")

    def st_sheets(self):
        from . import ingest
        rows = self.con.execute("SELECT * FROM clips WHERE proxy IS NOT NULL AND sheet IS NULL "
                                "AND status IN ('proxied','analyzed')").fetchall()
        rows = self.order(rows, lambda r: -(r["duration"] or 0))
        self.stage_begin("sheets", total=len(rows), weight_total=sum(r["duration"] or 0 for r in rows))

        def ok(r, p):
            self.con.execute("UPDATE clips SET sheet=?, updated=? WHERE id=?", (p, time.time(), r["id"]))
            self.con.commit()
        self.run_items("sheets", rows, lambda r: ingest._make_sheet(self.cfg, r), ok,
                       self._err("sheets", mark_status=False), workers=self.gov.heavy_slots)
        self.stage_end("sheets")

    def st_analyze(self):
        from . import analyze
        rows = self.con.execute("SELECT * FROM clips WHERE status='proxied' AND proxy IS NOT NULL").fetchall()
        rows = self.order(rows, lambda r: -(r["duration"] or 0))
        self.stage_begin("analyze", total=len(rows), weight_total=sum(r["duration"] or 0 for r in rows))
        if not rows:
            return self.stage_end("analyze")
        n = self.gov.heavy_slots
        pool = {"ex": analyze.make_pool(self.cfg, n)}
        plock = threading.Lock()

        def fn(r):
            ex = pool["ex"]
            try:
                return analyze.analyze_one(self.cfg, ex, r)
            except BrokenProcessPool:
                with plock:
                    if pool["ex"] is ex:
                        ex.shutdown(wait=False, cancel_futures=True)
                        pool["ex"] = analyze.make_pool(self.cfg, n)
                raise RuntimeError("analysis worker died") from None

        def ok(r, res):
            analyze._store(self.con, r["id"], res, r)
            self.con.commit()

        base_err = self._err("analyze")

        def err(r, e):
            if isinstance(e, (RuntimeError, MemoryError)) and ("worker died" in str(e) or isinstance(e, MemoryError)):
                k = record_failure(self.cfg, "analyze", r["id"], e)
                self.fail_counts[f"analyze:{r['id']}"] = k
                if k < MAX_CLIP_FAILURES:
                    self.log(f"  ! analyze {r['rel']}: {e} (retry later)")
                    return
            base_err(r, e)
        try:
            self.run_items("analyze", rows, fn, ok, err, workers=n)
        finally:
            pool["ex"].shutdown(wait=True, cancel_futures=True)
        self.stage_end("analyze")

    def st_transcribe(self):
        from . import asr
        model = self.opts.get("model") or self.cfg.asr_model
        rows = self.con.execute("SELECT * FROM clips WHERE has_audio=1 AND COALESCE(duration,0)>0 "
                                "AND status NOT IN ('new','error')").fetchall()
        done_ids = {r["clip_id"] for r in self.con.execute("SELECT clip_id FROM transcripts WHERE model=?", (model,))}
        pending = self.order([r for r in rows if r["id"] not in done_ids], lambda r: -(r["duration"] or 0))
        self.stage_begin("transcribe", total=len(pending), weight_total=sum(r["duration"] or 0 for r in pending),
                         model=model, phase="speech check")
        speech = {}

        def vad(r):
            return asr._vad_cached(self.cfg, r)[0]

        def vad_ok(r, sp):
            if sp < asr.MIN_SPEECH_S:
                asr._store(self.cfg, self.con, r["id"], None, model, [], 0, sp)
            else:
                speech[r["id"]] = sp
        self.run_items("transcribe", pending, vad, vad_ok, self._err("transcribe", mark_status=False), workers=1,
                       weight=lambda r: 0.0)
        todo = [r for r in pending if r["id"] in speech]
        big = len(self.rank) + 1
        todo.sort(key=lambda r: (self.rank.get(r["id"], big), -speech[r["id"]]))
        st = self.stage_mem["transcribe"]
        st.update(done=len(pending) - len(todo), weight_done=sum(r["duration"] or 0 for r in pending if r["id"] not in speech))
        self.progress("transcribe", force=True, phase=f"transcribing {len(todo)} clip(s) with speech",
                      with_speech=len(todo))

        def tx(r):
            return asr.transcribe_clip(self.cfg, self.thread_con(), r, model, speech_s=speech[r["id"]])

        def tx_err(r, e):
            k = record_failure(self.cfg, "transcribe", r["id"], e)
            self.fail_counts[f"transcribe:{r['id']}"] = k
            self.log(f"  ! transcribe {r['rel']}: {type(e).__name__}: {str(e)[-200:]}")
        self.run_items("transcribe", todo, tx, lambda r, res: None, tx_err, workers=1)
        self.stage_end("transcribe")

    def st_songs(self):
        from . import music
        self.stage_begin("songs", total=1)
        with self.gov.slot("heavy", label="songs index"):
            n = music.index_songs(self.cfg, self.con)
        self.stage_end("songs", done=1, new=n)

    def st_matchcuts(self):
        from . import matchcuts
        r = self.con.execute("SELECT COUNT(*), COALESCE(MAX(updated),0) FROM clips WHERE status='analyzed'").fetchone()
        sig = f"{r[0]}:{r[1]:.0f}"
        sig_file = self.dir / "matchcuts.sig"
        if sig_file.exists() and sig_file.read_text().strip() == sig:
            self.stage_begin("matchcuts", total=0)
            return self.stage_end("matchcuts", status="done", note="library unchanged since last mining")
        self.stage_begin("matchcuts", total=int(r[0]))
        res = matchcuts.mine(self.cfg, self.con)
        sig_file.write_text(sig)
        self.stage_end("matchcuts", done=int(r[0]), candidates=len(res))

    # -- tagging -----------------------------------------------------------------------------------
    def tag_candidates(self, con):
        self.rank = {cid: i for i, cid in enumerate(recent_clip_ids(self.cfg, con))}
        rows = con.execute("""
            SELECT c.*, tr.n_words AS n_words FROM clips c
            LEFT JOIN tags t ON t.clip_id = c.id LEFT JOIN transcripts tr ON tr.clip_id = c.id
            WHERE t.clip_id IS NULL AND c.status='analyzed' AND c.sheet IS NOT NULL""").fetchall()
        rows = [r for r in rows if r["sheet"] and os.path.exists(r["sheet"]) and not self.skipped("tag", r["id"])]
        big = len(self.rank) + 1
        # ready first: transcribed (or no audio) clips get the speech snippet in the prompt
        return sorted(rows, key=lambda r: (self.rank.get(r["id"], big),
                                           0 if (r["n_words"] is not None or not r["has_audio"]) else 1,
                                           -(r["duration"] or 0)))

    def tag_loop(self, until_idle=False):
        """Tag untagged analyzed clips in batches. until_idle=True: return when nothing is left;
        otherwise keep polling while the main stages may still produce clips."""
        con = db.connect(self.cfg.db_path)
        limit = self.opts.get("tag_limit")
        ag = self.agent
        batch_n = max(1, int(getattr(self.cfg, "tag_batch", 8) or 8))
        batch_n = min(batch_n, getattr(ag, "max_images", batch_n))
        pause = float(getattr(self.cfg, "tag_sleep_s", 20) or 0)
        tagger = TAGGERS.get(ag.name, f"{ag.name}-overnight")
        st = self.stage_mem.setdefault("tag", {})
        tagged = st.get("done", 0)
        while True:
            if self.stop_reason():
                return "stopped"
            if limit is not None and tagged >= limit:
                return "limit"
            cands = self.tag_candidates(con)
            if limit is not None:
                cands = cands[:max(0, limit - tagged)]
            self.progress("tag", total=tagged + len(cands))
            if not cands:
                if until_idle or self._main_done:
                    return "done"
                time.sleep(10)
                continue
            batch = cands[:batch_n]
            prompt = build_tag_prompt(self.cfg, con, batch, agent=ag.name)
            for r in batch:
                self._inflight_set("tag", r["id"], True)
            try:
                res = call_agent(self.cfg, ag, prompt, [str(Path(r["sheet"]).resolve()) for r in batch], gov=self.gov)
            finally:
                for r in batch:
                    self._inflight_set("tag", r["id"], False)
            cost = st.get("cost_usd", 0.0) + (res.get("cost_usd") or 0.0)
            calls = st.get("calls", 0) + 1
            usage = st.get("usage", {})
            for k, v in (res.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            self.progress("tag", force=True, cost_usd=round(cost, 4), calls=calls, usage=usage,
                          last_call_s=res.get("seconds"), agent=ag.name)
            if res.get("limit"):
                self.log(f"tag: {ag.label} usage limit reached; stopping tagging ({res.get('error', '')[:200]})")
                self.progress("tag", force=True, last_error=f"usage limit: {res.get('error', '')[:300]}")
                return "usage limit"
            if not res.get("ok"):
                self.log(f"tag: call failed: {res.get('error')}")
                for r in batch:
                    self.fail_counts[f"tag:{r['id']}"] = record_failure(self.cfg, "tag", r["id"], res.get("error"))
                self.progress("tag", force=True, last_error=str(res.get("error"))[:300],
                              errors=st.get("errors", 0) + 1)
                if res.get("missing") or "not found on PATH" in str(res.get("error")):
                    return "no agent"
                self._sleep(max(pause, 30))
                continue
            got = {str(o.get("id")): o for o in parse_tag_output(res.get("text") or "")}
            n_ok = 0
            for r in batch:
                o = got.get(r["id"])
                data = validate_tag(o, r["duration"]) if o else {}
                if not data:
                    self.fail_counts[f"tag:{r['id']}"] = record_failure(self.cfg, "tag", r["id"], "no valid tags in reply")
                    continue
                if db.get_tags(con, r["id"]):         # the user tagged it meanwhile: theirs wins
                    continue
                data.update(tagged_by=tagger, tagged_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                db.set_tags(con, r["id"], data, merge=False)
                n_ok += 1
            tagged += n_ok
            self.log(f"tag: batch of {len(batch)} -> {n_ok} tagged ({res.get('seconds')}s, ${res.get('cost_usd') or 0:.4f})")
            self.progress("tag", force=True, done=tagged)
            self._sleep(pause)

    def _sleep(self, s):
        end = time.time() + s
        while time.time() < end and not self.stop_reason():
            time.sleep(min(1.0, end - time.time()))

    def _tag_thread_main(self):
        try:
            why = self.tag_loop()
            self.tag_result = why
        except Exception as e:  # noqa: BLE001
            self.tag_error = f"{type(e).__name__}: {e}"
            self.log(f"tag: crashed: {self.tag_error}")

    def start_tagging(self):
        if not self.opts.get("tag", True) or self.tag_thread or self.stage_done_this_run("tag"):
            return
        if self.opts.get("dry_run"):
            return
        if self.agent_missing():
            return
        self.stage_begin("tag", total=0, cost_usd=self.stage_mem.get("tag", {}).get("cost_usd", 0.0))
        self._main_done = False
        self.tag_result = None
        self.tag_thread = threading.Thread(target=self._tag_thread_main, daemon=True, name="tagger")
        self.tag_thread.start()

    def st_tag(self):
        if not self.opts.get("tag", True):
            self.stage_begin("tag", total=0)
            return self.stage_end("tag", status="skipped", note="--no-tag")
        if self.opts.get("dry_run"):
            cands = self.tag_candidates(self.con)
            if self.opts.get("tag_limit") is not None:
                cands = cands[:self.opts["tag_limit"]]
            self.stage_begin("tag", total=len(cands))
            n = max(1, int(getattr(self.cfg, "tag_batch", 8) or 8))
            ag = self.agent
            n = min(n, getattr(ag, "max_images", n))
            if cands:
                (self.dir / "tag_prompt_preview.txt").write_text(build_tag_prompt(self.cfg, self.con, cands[:n], agent=ag.name))
            return self.stage_end("tag", status="dry-run", note=f"would send {len(cands)} clip(s) in "
                                  f"{-(-len(cands) // n)} {ag.label} call(s); first prompt in tag_prompt_preview.txt",
                                  agent=ag.name, claude=claude_bin() or "not found", path=ag.path or "not found")
        why_not = self.agent_missing()
        if why_not:
            self.log(f"tag: {why_not}; skipping visual tagging (install Claude Code or Codex and log in "
                     "with your own account, then run `cutroom overnight` again)")
            self.stage_begin("tag", total=0)
            return self.stage_end("tag", status="skipped", note=why_not)
        self.start_tagging()
        self._main_done = True
        if self.tag_thread:
            while self.tag_thread.is_alive():
                self.tag_thread.join(timeout=2)
                self.progress("tag")
        why = self.tag_result or ("error" if self.tag_error else "done")
        if self.stop_reason():
            raise Stopped(self.stop_reason())
        status = {"done": "done", "limit": "done", "usage limit": "stopped", "no agent": "skipped"}.get(why, "error")
        self.stage_end("tag", status=status, note=why if why != "done" else None)

    # -- driver --------------------------------------------------------------------------------------
    def run(self) -> int:
        skip = set(self.opts.get("skip_stages") or ())
        try:
            for stage in STAGES:
                if stage in skip:
                    continue
                if stage != "tag" and self.stage_done_this_run(stage):
                    if stage == "analyze":
                        self.start_tagging()
                    continue
                self.check_stop()
                try:
                    getattr(self, f"st_{stage}")()
                except (Stopped, RestartForMemory):
                    raise
                except MemoryError as e:
                    raise RestartForMemory(str(e)) from None
                except Exception as e:  # noqa: BLE001 - a broken stage must not end the night
                    self.log(f"stage {stage} failed: {type(e).__name__}: {e}")
                    self.stage_end(stage, status="error", last_error=f"{type(e).__name__}: {str(e)[-300:]}")
                    merge_state(self.cfg, {"last_error": f"{stage}: {type(e).__name__}: {str(e)[-300:]}"})
                if stage == "analyze":
                    self.start_tagging()
        except Stopped as e:
            self.log(f"stopping: {e}")
            self._main_done = True
            if self.stage_mem.get(merge_state(self.cfg, {}).get("stage"), {}).get("status") == "running":
                self.progress(merge_state(self.cfg, {}).get("stage"), force=True, status="stopped")
            if self.tag_thread:
                self.tag_thread.join(timeout=self.cfg.tag_timeout_s + 30)
                self.progress("tag", force=True, status="stopped")
            merge_state(self.cfg, {"stop_reason": str(e)})
            return EXIT_STOPPED
        except RestartForMemory as e:
            self.log(f"memory guard: {e}; restarting the stage process to release memory")
            return EXIT_MEMORY
        finally:
            _jsave(self.dir / "inflight.json", [])
        return EXIT_DONE


# ==================================================================================================
# supervisor
# ==================================================================================================

def _parse_until(s):
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        raise SystemExit(f"--until wants HH:MM (local time), got {s!r}")
    now = dt.datetime.now()
    t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t.timestamp()


def _keep_awake(pid):
    """macOS: caffeinate -i (idle sleep only; the display may sleep). Linux: systemd-inhibit."""
    try:
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            return subprocess.Popen(["caffeinate", "-i", "-w", str(pid)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        if shutil.which("systemd-inhibit"):
            return subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=cutroom",
                                     "--why=overnight understanding", "--mode=block", "sleep", "172800"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return None


def _child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run(cfg, until=None, tag=True, tag_limit=None, model=None, dry_run=False, skip_stages=(), echo=True) -> int:
    """Supervise the overnight pipeline in the foreground. Returns 0 when finished or stopped."""
    d = odir(cfg)
    log = _Log(cfg, echo=echo)
    lockfd = os.open(str(d / "run.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lockfd)
        print(f"overnight is already running (pid {_read_pid(cfg)}); `cutroom overnight --status` / --stop")
        return 1
    (d / "STOP").unlink(missing_ok=True)
    (d / "overnight.pid").write_text(str(os.getpid()))
    gov = governor.for_cfg(cfg)
    until_ts = _parse_until(until) if isinstance(until, str) else until
    run_id = f"{int(time.time())}-{os.getpid()}"
    opts = {"until_ts": until_ts, "tag": tag, "tag_limit": tag_limit, "model": model, "dry_run": dry_run,
            "run_id": run_id, "skip_stages": list(skip_stages), "supervised": True}
    with _flocked(d / ".state.lock"):
        _jsave(d / "state.json", {"run_id": run_id, "pid": os.getpid(), "started": time.time(), "until": until_ts,
                                  "options": {k: v for k, v in opts.items() if k != "supervised"},
                                  "stage": None, "stages": {}, "restarts": 0, "finished": False,
                                  "limits": gov.limits(), "last_error": None})
    stop = {"flag": False}

    def on_signal(signum, frame):  # noqa: ARG001
        stop["flag"] = True
        (d / "STOP").touch()
    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    awake = _keep_awake(os.getpid())
    log(f"overnight start: run {run_id}" + (f", until {time.strftime('%H:%M', time.localtime(until_ts))}" if until_ts else "")
        + (" (dry run: no AI calls)" if dry_run else "") + f" · limits {gov.limits()}")
    restarts, status, rc = 0, "finished", EXIT_DONE
    cap_gb = gov.self_mem_gb * 1.25
    try:
        while True:
            if (d / "STOP").exists() or (until_ts and time.time() >= until_ts):
                status = "stopped"
                break
            logf = open(d / "overnight.log", "a")
            child = subprocess.Popen([sys.executable, "-m", "cutroom.overnight", "--child", "--work", str(cfg.work),
                                      "--opts", json.dumps(opts)], cwd=str(cfg.work), env=_child_env(),
                                     stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            logf.close()
            merge_state(cfg, {"child_pid": child.pid})
            killed = None
            stop_sent = None
            while child.poll() is None:
                time.sleep(2)
                fp = governor.footprint(child.pid)
                if fp and fp / governor.GB > cap_gb and not killed:
                    killed = f"stage process footprint {fp / governor.GB:.2f} GB > {cap_gb:.2f} GB"
                    log(f"supervisor: {killed}; killing it (it will be restarted)")
                    with contextlib.suppress(OSError):
                        os.kill(child.pid, signal.SIGKILL)   # not SIGTERM: that would mean "stop the night"
                if until_ts and time.time() >= until_ts and not (d / "STOP").exists():
                    log("supervisor: --until time reached; asking the stage process to finish its current item")
                    (d / "STOP").touch()
                if (d / "STOP").exists():
                    stop_sent = stop_sent or time.time()
                    if time.time() - stop_sent > 900:
                        log("supervisor: stage process did not stop within 15 min; terminating it")
                        governor.Governor._terminate(child.pid, grace=10)
            rc = child.returncode
            # the stage process has its own session: take down anything it left behind (ffmpeg,
            # analysis workers) so an orphan never keeps writing into files the next run cleans up
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
            if rc == EXIT_DONE:
                status = "finished"
                break
            if rc == EXIT_STOPPED:
                status = "stopped"
                break
            if rc == EXIT_MEMORY and not killed:
                log("supervisor: stage process restarting to release memory")
                continue
            # crash (or memory kill): blame whatever was in flight
            restarts += 1
            inflight = _jload(d / "inflight.json", [])
            reason = killed or f"stage process died (exit {rc})"
            gave_up = []
            for it in inflight:
                n = record_failure(cfg, it["stage"], it["clip"], reason)
                if n >= MAX_CLIP_FAILURES:
                    gave_up.append(it)
                    _give_up(cfg, it["stage"], it["clip"], reason)
            _jsave(d / "inflight.json", [])
            msg = (f"{reason}; in flight: {[i['stage'] + ':' + i['clip'] for i in inflight] or 'nothing'}"
                   + (f"; gave up on {[i['clip'] for i in gave_up]}" if gave_up else ""))
            log(f"supervisor: {msg}")
            merge_state(cfg, {"restarts": restarts, "last_error": msg})
            if restarts > MAX_RESTARTS:
                log("supervisor: too many restarts; giving up for tonight")
                status = "gave up"
                break
            back = min(300, 10 * 2 ** min(restarts - 1, 5))
            end = time.time() + back
            while time.time() < end and not (d / "STOP").exists():
                time.sleep(1)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        try:
            from . import db as _db
            con = _db.connect(cfg.db_path)
            st = merge_state(cfg, {"finished": True, "status": status, "ended": time.time(), "restarts": restarts,
                                   "child_pid": None, "current": []})
            write_report(cfg, con, st)
            log(f"overnight {status}; report: {report_path(cfg)}")
        except Exception as e:  # noqa: BLE001
            log(f"report failed: {type(e).__name__}: {e}")
        if awake:
            awake.terminate()
        (d / "overnight.pid").unlink(missing_ok=True)
        (d / "STOP").unlink(missing_ok=True)
        fcntl.flock(lockfd, fcntl.LOCK_UN)
        os.close(lockfd)
    return 0 if status in ("finished", "stopped") else 1


def _give_up(cfg, stage, cid, reason):
    """A clip crashed the stage process twice: mark it so nothing loops on it again."""
    con = db.connect(cfg.db_path)
    if stage in ("proxies", "analyze"):
        con.execute("UPDATE clips SET status='error', error=?, updated=? WHERE id=?",
                    (f"overnight: {stage} crashed twice ({reason})"[:300], time.time(), cid))
    elif stage == "sheets":
        con.execute("UPDATE clips SET error=? WHERE id=?", (f"overnight: sheet crashed twice ({reason})"[:300], cid))
    con.commit()


def start_background(cfg, until=None, tag=True, tag_limit=None, model=None, dry_run=False, only=None) -> int:
    """Start `cutroom overnight` detached (for the studio). Returns the supervisor pid."""
    pid = _read_pid(cfg)
    if pid and governor.pid_alive(pid):
        return pid
    cmd = [sys.executable, "-m", "cutroom.cli", "overnight"]
    if until:
        cmd += ["--until", until]
    if not tag:
        cmd += ["--no-tag"]
    if tag_limit is not None:
        cmd += ["--tag-limit", str(int(tag_limit))]
    if model:
        cmd += ["--model", model]
    if dry_run:
        cmd += ["--dry-run"]
    if only:
        cmd += ["--only", ",".join(only) if isinstance(only, (list, tuple)) else str(only)]
    p = subprocess.Popen(cmd + ["--quiet"], cwd=str(cfg.work), env={**_child_env(), "CUTROOM_WORK": str(cfg.work)},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    return p.pid


def stop(cfg) -> bool:
    """Ask a running overnight to stop after its current item. Returns True if one was running."""
    (odir(cfg) / "STOP").touch()
    pid = _read_pid(cfg)
    if pid and governor.pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return True
    return False


def print_status(cfg):
    st = read_state(cfg)
    if not st.get("run_id"):
        print("overnight has not run in this project yet (`cutroom overnight`)")
        return st
    started = time.strftime("%a %H:%M", time.localtime(st.get("started", 0)))
    print(f"overnight {'RUNNING' if st['running'] else st.get('status', 'not running')} · started {started} · "
          f"stage {st.get('stage')} · restarts {st.get('restarts', 0)}")
    for name in STAGES:
        s = st.get("stages", {}).get(name)
        if not s:
            continue
        eta = s.get("eta_s")
        eta_txt = f" · ETA {eta // 3600:.0f}h{eta % 3600 // 60:02.0f}m" if eta else ""
        extra = f" · ${s['cost_usd']:.2f}" if s.get("cost_usd") else ""
        print(f"  {name:11s} {s.get('status', ''):9s} {s.get('done', 0)}/{s.get('total', 0)}"
              f"{' · errors ' + str(s['errors']) if s.get('errors') else ''}{eta_txt}{extra}"
              f"{' · ' + str(s['note']) if s.get('note') else ''}")
    if st.get("throttle"):
        print(f"  throttled: {'; '.join(st['throttle'])}")
    if st.get("waiting"):
        print(f"  waiting: {st['waiting'].get('reason')}")
    if st.get("last_error"):
        print(f"  last error: {st['last_error']}")
    if report_path(cfg).exists() and not st["running"]:
        print(f"  report: {report_path(cfg)}")
    return st


# ==================================================================================================
# morning report
# ==================================================================================================

def _hours(con, where="1=1"):
    return (con.execute(f"SELECT COALESCE(SUM(duration),0) FROM clips WHERE {where}").fetchone()[0] or 0) / 3600


def write_report(cfg, con, state=None) -> Path:
    from . import analyze
    state = state or read_state(cfg)
    q = lambda s, *a: con.execute(s, a).fetchone()[0]  # noqa: E731
    n_clips = q("SELECT COUNT(*) FROM clips")
    tags = {r["clip_id"]: json.loads(r["data"]) for r in con.execute("SELECT clip_id, data FROM tags")}
    by_ai = Counter(t.get("tagged_by") for t in tags.values() if t.get("tagged_by") in TAGGERS.values())
    clip_rows = {r["id"]: r for r in con.execute("SELECT id, rel, duration, status, error, has_audio FROM clips")}
    L = [f"# Overnight report", "",
         f"Run {state.get('run_id', '?')} · {state.get('status', 'unknown')} · "
         f"started {time.strftime('%Y-%m-%d %H:%M', time.localtime(state.get('started', time.time())))}"
         + (f" · ended {time.strftime('%H:%M', time.localtime(state['ended']))}" if state.get("ended") else "")
         + (f" · stop reason: {state['stop_reason']}" if state.get("stop_reason") else ""), "",
         "## What was done", "",
         "| stage | status | done | errors | notes |", "|---|---|---|---|---|"]
    for name in STAGES:
        s = state.get("stages", {}).get(name)
        if not s:
            continue
        note = s.get("note") or ""
        if name == "tag" and s.get("calls"):
            who = {"claude": "Claude", "codex": "Codex"}.get(s.get("agent"), "AI")
            note = (note + " " if note else "") + f"{s['calls']} {who} call(s)" + (
                f", ${s.get('cost_usd', 0):.2f}" if s.get("cost_usd") else "")
        L.append(f"| {name} | {s.get('status', '')} | {s.get('done', 0)}/{s.get('total', 0)} | {s.get('errors', 0)} | {note} |")
    analyzed_where = "status='analyzed'"
    L += ["", "## The library now", "",
          f"- **{n_clips} clips, {_hours(con):.1f} h** of footage indexed",
          f"- **{_hours(con, analyzed_where):.1f} h understood** (proxied, contact sheets, "
          f"shots and quality analysed): {q('SELECT COUNT(*) FROM clips WHERE status=?', 'analyzed')} clips",
          f"- transcribed: {q('SELECT COUNT(*) FROM transcripts')} clips, "
          f"{q('SELECT COUNT(*) FROM transcripts WHERE n_words>5')} with speech "
          f"({(q('SELECT COALESCE(SUM(speech_s),0) FROM transcripts WHERE n_words>5') or 0) / 3600:.1f} h of talking)",
          f"- tagged: {len(tags)} clips ({by_ai.get(TAGGERS['claude'], 0)} by Claude overnight"
          + (f", {by_ai[TAGGERS['codex']]} by Codex overnight" if by_ai.get(TAGGERS['codex']) else "") + ")",
          f"- match-cut candidates: {q('SELECT COUNT(*) FROM matchcuts')} · songs: {q('SELECT COUNT(*) FROM songs')}"]
    if state.get("restarts"):
        L.append(f"- the stage process was restarted {state['restarts']} time(s); last: {state.get('last_error')}")
    errs = con.execute("SELECT id, rel, error FROM clips WHERE status='error' ORDER BY updated DESC LIMIT 15").fetchall()
    n_err = q("SELECT COUNT(*) FROM clips WHERE status='error'")
    L += ["", "## Errors", ""]
    if n_err or state.get("last_error"):
        if state.get("last_error"):
            L.append(f"- last error: {state['last_error']}")
        for r in errs:
            L.append(f"- `{r['id']}` {r['rel']}: {(r['error'] or '')[:160]}")
        if n_err > len(errs):
            L.append(f"- … and {n_err - len(errs)} more (`cutroom status`)")
    else:
        L.append("None.")

    # top clips
    scored = []
    for cid, t in tags.items():
        if cid not in clip_rows or ("usable" in t and t["usable"] is False):
            continue
        try:
            qv = float(t.get("quality"))
        except (TypeError, ValueError):
            qv = analyze.auto_quality(analyze.clip_quality(con, cid))
        scored.append((qv, clip_rows[cid]["duration"] or 0, cid, t))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    L += ["", "## Best-looking clips", ""]
    if scored:
        for qv, dur, cid, t in scored[:10]:
            L.append(f"- `{cid}` q{qv:g} · {dur:.0f}s · {clip_rows[cid]['rel']}: {t.get('summary', '')}")
    else:
        L.append("No tagged clips yet.")

    # overview
    L += ["", "## Library overview (from tags)", ""]

    def dist(key, n=8):
        c = Counter()
        for t in tags.values():
            v = t.get(key)
            for x in (v if isinstance(v, list) else [v]):
                if x not in (None, ""):
                    c[str(x).lower()] += 1
        return c.most_common(n)
    for key, label in (("setting", "Settings"), ("time_of_day", "Time of day"), ("people", "People"),
                       ("shot", "Shot types"), ("mood", "Moods"), ("topics", "Topics")):
        d = dist(key)
        if d:
            L.append(f"- **{label}:** " + ", ".join(f"{k} ({v})" for k, v in d))
    if not tags:
        L.append("No tags yet: run `cutroom overnight` with Claude Code or Codex installed and logged in, or tag with `cutroom tags todo`.")

    # ideas
    L += ["", "## Edit ideas", ""]
    ideas = []
    talk = [cid for cid, t in tags.items() if t.get("talking_to_camera") is True]
    if len(talk) >= 3:
        longest = sorted(talk, key=lambda c: -(clip_rows.get(c, {"duration": 0})["duration"] or 0))[:3]
        ideas.append(f"**Talking-head story**: {len(talk)} clips talk to camera (longest: "
                     + ", ".join(f"`{c}`" for c in longest) + "). Pick one thread from `cutroom say`, cut it to 60-90 s "
                     "and cover the joins with b-roll.")
    for setting, n in dist("setting", 3):
        if n >= 4:
            good = [c for qv, _, c, t in scored if str(t.get("setting", "")).lower() == setting and qv >= 3]
            ideas.append(f"**A day at the {setting}**: {n} clips ({len(good)} rated 3+). "
                         f"`cutroom search --setting \"{setting}\" --min-quality 3` for a beat-cut montage.")
    golden = [c for c, t in tags.items() if t.get("time_of_day") == "golden"]
    if len(golden) >= 3:
        ideas.append(f"**Golden-hour montage**: {len(golden)} golden-hour clips; cut on the beat to a slow song.")
    mc = con.execute("SELECT * FROM matchcuts ORDER BY score DESC LIMIT 3").fetchall()
    dn = con.execute("SELECT * FROM matchcuts WHERE kind='daynight' ORDER BY score DESC LIMIT 1").fetchone()
    if dn:
        ideas.append(f"**Day → night transition**: `{dn['a']}` @ {dn['ta']:.1f}s → `{dn['b']}` @ {dn['tb']:.1f}s "
                     f"(score {dn['score']:.2f}); a time-jump match cut.")
    if mc:
        ideas.append("**Match-cut moments**: " + "; ".join(f"`{r['a']}`@{r['ta']:.1f}s → `{r['b']}`@{r['tb']:.1f}s "
                                                        f"({r['kind']}, {r['score']:.2f})" for r in mc))
    for topic, n in dist("topics", 3):
        if n >= 3:
            ideas.append(f"**B-roll pack: {topic}**: {n} clips illustrate it (`cutroom search {topic}`).")
    L += [f"{i}. {x}" for i, x in enumerate(ideas[:8], 1)] or ["Not enough tags yet for ideas."]
    L += ["", f"_Generated {time.strftime('%Y-%m-%d %H:%M')}. Progress details: `cutroom overnight --status`; "
              f"log: overnight/overnight.log._", ""]
    p = report_path(cfg)
    p.write_text("\n".join(L))
    return p


# ==================================================================================================
# entry points
# ==================================================================================================

def _child_main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--work", required=True)
    ap.add_argument("--opts", default="{}")
    a = ap.parse_args(argv)
    from .config import Config
    cfg = Config.load(a.work)
    gov = governor.for_cfg(cfg)
    try:
        os.nice(max(0, gov.nice - os.nice(0)))
    except OSError:
        pass
    signal.signal(signal.SIGTERM, lambda *_: (odir(cfg) / "STOP").touch())
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pipe = Pipeline(cfg, json.loads(a.opts), echo=False)     # stdout already goes to the log
    pipe.log(f"stage process {os.getpid()} up (nice {os.nice(0)})")
    return pipe.run()


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
