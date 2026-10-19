"""Cutroom Studio backend: the HTTP/SSE server behind the browser editor (docs/STUDIO.md).

    cutroom studio [--port 8600] [--host 127.0.0.1] [--open]

stdlib only (ThreadingHTTPServer). What lives where:
  <work>/plans/<slug>.draft.json        the open working draft (hand edits + Claude edit this file)
  <work>/plans/<slug>_v<N>.json         committed versions (render always renders one of these)
  <work>/plans/.history/<slug>/         undo/redo snapshots + index.json
  <work>/studio/context.json            pointing state posted by the frontend (+ draft_path)
  <work>/studio/markers.json            markers  [{id, plan, t, text, frame_url, status, kind, ...}]
  <work>/studio/grabs/  markers/        frame grabs (chat attachments) and marker frames
  <work>/studio/chat.json               Claude session id; chat_history.jsonl the transcript
  <work>/frames/thumbs/ frames/studio/  cached thumbnails (from proxies) and full-res frames

Design rules: every handler is wrapped (an exception becomes a JSON error, never a dead server);
handler threads are capped; long work (render, qa, overnight) runs as `cutroom ...` subprocesses
in their own process group; frame extraction goes through the governor's light lane (or a local
2-slot semaphore until the governor module exists).
"""
import collections
import contextlib
import copy
import fcntl
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import agent as agentmod
from . import db
from . import plan as P

REPO = Path(__file__).resolve().parents[1]
WEB = Path(__file__).with_name("studio_web")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
VERSION_RE = re.compile(r"^(?P<slug>.+)_v(?P<n>\d+)\.json$")
MAX_BODY = 16 * 1024 * 1024
CHUNK = 256 * 1024
HISTORY_MAX = 200
MAX_THREADS = 64
MAX_SSE = 16
SSE_PING_S = 15
MARKER_LOCK = threading.Lock()

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/wav", ".wav")


class ApiError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code, self.msg, self.extra = code, msg, extra


# ================================================================================================ utils
def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _dump_plan(plan):
    return json.dumps(plan, indent=1, ensure_ascii=False) + "\n"


def _f(v, name="value"):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be a number") from None
    if x != x or x in (float("inf"), float("-inf")):
        raise ApiError(400, f"{name} must be finite")
    return x


def _within(root, rel):
    """root/rel resolved, or None if it escapes root (traversal-safe)."""
    root = Path(root).resolve()
    rel = unquote(str(rel or "")).lstrip("/")
    if "\x00" in rel:
        return None
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        return None
    return p


def _mmss(t):
    t = max(0.0, float(t or 0))
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _py():
    return sys.executable or "python3"


def _cli_cmd(*args):
    return [_py(), "-m", "cutroom.cli", *[str(a) for a in args]]


def _sub_env(cfg, extra=None):
    env = dict(os.environ)
    env["CUTROOM_WORK"] = str(cfg.work)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    bindir = str(Path(_py()).parent)
    if bindir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env.update(extra or {})
    return env


# ================================================================================================ governor
def _gov():
    try:
        from . import governor          # built separately; optional until it lands
        return governor
    except Exception:  # noqa: BLE001
        return None


_LIGHT = threading.BoundedSemaphore(2)          # the studio never runs more than 2 frame grabs at once


def _gov_for(cfg):
    gov = _gov()
    fn = getattr(gov, "for_cfg", None) if gov else None
    if not callable(fn):
        return None
    try:
        return fn(cfg)
    except Exception:  # noqa: BLE001
        return None


@contextlib.contextmanager
def light_slot(cfg=None, label="", timeout=30.0):
    """A frame grab / thumbnail: one of the studio's 2 local slots, inside the governor's 'light' lane
    (which waits out RAM/disk emergencies but has no concurrency cap of its own)."""
    if not _LIGHT.acquire(timeout=timeout):
        raise ApiError(503, "busy: too many frame requests, retry shortly")
    try:
        g = _gov_for(cfg) if cfg is not None else None
        if g is not None and hasattr(g, "slot"):
            with g.slot("light", label=label):
                yield
        else:
            yield                      # no governor module: the local semaphore is the only limit
    finally:
        _LIGHT.release()


def governor_status(studio):
    g = _gov_for(studio.cfg)
    running = studio.jobs.running_count() if getattr(studio, "jobs", None) else 0
    if g is not None and hasattr(g, "status"):
        try:
            return {"available": True, "studio_jobs_running": running, **g.status()}
        except Exception as e:  # noqa: BLE001
            return {"available": True, "error": f"{type(e).__name__}: {e}", "studio_jobs_running": running}
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return {"available": False, "load": load, "cpus": os.cpu_count(), "studio_jobs_running": running,
            "note": "governor module not installed; the studio limits itself (2 frame grabs, 1 render at a time)"}


def _overnight():
    try:
        from . import overnight          # built separately; optional
        return overnight
    except Exception:  # noqa: BLE001
        return None


def overnight_state(cfg, report=False):
    ov = _overnight()
    if ov is None:
        return {"available": False}
    try:
        stt = dict(ov.read_state(cfg) or {})
    except Exception as e:  # noqa: BLE001
        return {"available": True, "error": f"{type(e).__name__}: {e}"}
    stt["available"] = True
    stt["running"] = bool(stt.get("running"))
    if report:
        try:
            rp = ov.report_path(cfg)
            if rp.exists():
                stt["report_md"] = rp.read_text(errors="replace")[:200_000]
                stt["report_path"] = str(rp)
        except Exception:  # noqa: BLE001
            pass
    return stt


# ================================================================================================ markers
REPLY_STYLE = (
    "\n\n## How to reply (studio chat)\n"
    "The creator sees your reply in the editor, not a terminal. Plain, short sentences. Every shot you mention is a "
    "clip token `[[clip:<id>@<t>]]` or `[[clip:<id>@<t0>-<t1>]]` (ids from `cutroom search --json` / `cutroom clip`), "
    "several picks on one line form a strip; timeline moments are `[[t:<seconds>]]`, edit pieces `[[seg:<s>.<g>]]`, "
    "bins `[[bin:<name>]]`. Never show file paths, filenames, ids outside tokens, commands, JSON or tables of files."
)


def bins_load(cfg):
    """Bins Claude writes to studio/bins/<name>.json: {"title", "items": [{"clip", "t0", "t1", "why"}]}."""
    out = []
    d = Path(cfg.work) / "studio" / "bins"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, list):
            data = {"items": data}
        items = [i for i in data.get("items", []) if isinstance(i, dict) and i.get("clip")]
        out.append({"name": f.stem, "title": data.get("title") or f.stem.replace("-", " ").replace("_", " "),
                    "items": items, "updated": f.stat().st_mtime})
    return out


def _bins_sig(cfg):
    d = Path(cfg.work) / "studio" / "bins"
    if not d.is_dir():
        return ()
    return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in d.glob("*.json")))


def markers_path(cfg):
    return Path(cfg.work) / "studio" / "markers.json"


@contextlib.contextmanager
def _markers_locked(cfg):
    p = markers_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with MARKER_LOCK, open(p.with_suffix(".lock"), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield p
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def markers_load(cfg):
    d = _read_json(markers_path(cfg), [])
    return d if isinstance(d, list) else d.get("markers", []) if isinstance(d, dict) else []


def markers_update(cfg, fn):
    """Read-modify-write markers.json under a process + thread lock. fn(list) -> result."""
    with _markers_locked(cfg) as p:
        data = markers_load(cfg)
        res = fn(data)
        _atomic_write(p, json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        return res


def marker_set_status(cfg, mid, status="done"):
    def f(data):
        for m in data:
            if m.get("id") == mid:
                m["status"] = status
                m["updated"] = time.time()
                return m
        raise KeyError(f"no marker {mid!r}")
    return markers_update(cfg, f)


# ================================================================================================ project CLAUDE.md
STUDIO_MARK = "<!-- cutroom-studio -->"


def ensure_project_claude(cfg):
    """Project CLAUDE.md imports the repo's operating manual + a Studio-mode note; .claude/skills is
    linked from the repo. Never overwrites what the operator wrote: the block is appended once."""
    work = Path(cfg.work)
    md = work / "CLAUDE.md"
    block = (f"{STUDIO_MARK}\n@{REPO / 'CLAUDE.md'}\n\n"
             "## Studio mode\n"
             "This project may be open in Cutroom Studio (`cutroom studio`). When it is:\n"
             "- `studio/context.json` says what the creator is pointing at (playhead, selection, source In/Out) and\n"
             "  `draft_path`, the plan draft open in the editor. Edit that draft file in place; the studio reloads\n"
             "  it on save and keeps your edit undoable. Do not create `_vN.json` files unless asked to commit.\n"
             "- Markers are in `studio/markers.json`. \"Fix all open markers\" = read every open marker for the plan,\n"
             "  make each change in the draft, then `cutroom markers done <id>` for each one you handled.\n"
             "- Use the `cutroom` CLI for everything (search, say, frames, plan check, render, qa).\n")
    try:
        text = md.read_text() if md.exists() else ""
        if STUDIO_MARK not in text:
            head = text if text else "# Edit room\n\nProject-specific notes for this edit go above the studio block.\n\n"
            _atomic_write(md, head.rstrip("\n") + "\n\n" + block)
    except OSError as e:
        print(f"studio: could not write {md}: {e}", file=sys.stderr)
    ensure_project_agents(cfg)
    src = REPO / ".claude" / "skills"
    dst = work / ".claude" / "skills"
    try:
        if src.is_dir() and not dst.exists() and not dst.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(src, dst, target_is_directory=True)
            except OSError:
                shutil.copytree(src, dst)
    except OSError as e:
        print(f"studio: could not link skills: {e}", file=sys.stderr)


AGENTS_BEGIN = "<!-- cutroom-agents:begin (generated by cutroom; edit outside this block) -->"
AGENTS_END = "<!-- cutroom-agents:end -->"


def _md_section(text, heading):
    """A markdown section from its heading line up to the next heading of the same or higher level."""
    lines = text.splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    out = None
    for ln in lines:
        if out is None:
            if ln.strip() == heading.strip():
                out = [ln]
            continue
        if ln.startswith("#"):
            lv = len(ln) - len(ln.lstrip("#"))
            if lv <= level and not ln.startswith(heading):
                break
        out.append(ln)
    return "\n".join(out).strip() + "\n" if out else ""


def agents_md_block(repo=None, studio_note=True):
    """AGENTS.md text for Codex (which reads AGENTS.md, not CLAUDE.md, and may not follow file includes):
    a pointer to the repo's manual and skills, plus the Studio-mode sections inlined verbatim."""
    repo = Path(repo or REPO)
    try:
        manual = (repo / "CLAUDE.md").read_text()
    except OSError:
        manual = ""
    studio = _md_section(manual, "## Studio mode")       # includes "### Talking in the studio: clips, not code"
    parts = [AGENTS_BEGIN,
             "# Instructions for Codex (and any agent that reads AGENTS.md)",
             "",
             f"Read `{repo / 'CLAUDE.md'}` and the skills in `{repo / '.claude/skills'}/*/SKILL.md` before acting. "
             "`CLAUDE.md` is the operating manual for this edit room; it applies to you exactly as written, even "
             "where it says \"Claude\". Load the skill that matches the step you are on (indexing, tagging, "
             "planning, render-and-qa, review-loop).",
             f"When the creator asks for something the tool cannot do yet, extend cutroom itself: follow "
             f"`{repo / 'docs/ARCHITECTURE.md'}` and the `extending-cutroom` skill "
             f"(`{repo / '.claude/skills/extending-cutroom/SKILL.md'}`).",
             "Use the `cutroom` CLI for everything (search, say, frames, plan check, render, qa). Never modify, "
             "move or delete the creator's footage.",
             ""]
    if studio_note and studio:
        parts += ["The two sections below are copied verbatim from that manual because they govern every chat "
                  "message in the studio:", "", studio.rstrip("\n"), ""]
    parts.append(AGENTS_END)
    return "\n".join(parts) + "\n"


def ensure_project_agents(cfg_or_dir, repo=None):
    """Write/refresh the generated block in <work>/AGENTS.md. Text the operator wrote outside the block
    is kept; the block itself is regenerated so it follows the repo's CLAUDE.md."""
    work = Path(getattr(cfg_or_dir, "work", cfg_or_dir))
    md = work / "AGENTS.md"
    block = agents_md_block(repo)
    try:
        text = md.read_text() if md.exists() else ""
        if AGENTS_BEGIN in text and AGENTS_END in text:
            a = text.index(AGENTS_BEGIN)
            b = text.index(AGENTS_END) + len(AGENTS_END)
            new = text[:a] + block.rstrip("\n") + text[b:]
            if not new.endswith("\n"):
                new += "\n"
        else:
            head = text if text else "# Edit room\n\nProject-specific notes for this edit go above the generated block.\n\n"
            new = head.rstrip("\n") + "\n\n" + block
        if new != text:
            _atomic_write(md, new)
    except OSError as e:
        print(f"studio: could not write {md}: {e}", file=sys.stderr)


# ================================================================================================ db pool
class ConPool:
    def __init__(self, path, n=6):
        self.q = queue.Queue()
        for _ in range(n):
            con = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(db.SCHEMA)
            self.q.put(con)

    @contextlib.contextmanager
    def get(self):
        try:
            con = self.q.get(timeout=30)
        except queue.Empty:
            raise ApiError(503, "database busy") from None
        try:
            yield con
        finally:
            try:
                if con.in_transaction:
                    con.rollback()
            except sqlite3.Error:
                pass
            self.q.put(con)

    def close(self):
        while not self.q.empty():
            try:
                self.q.get_nowait().close()
            except Exception:  # noqa: BLE001
                pass


# ================================================================================================ events
class Bus:
    def __init__(self):
        self.subs = set()
        self.lock = threading.Lock()

    def subscribe(self):
        with self.lock:
            if len(self.subs) >= MAX_SSE:
                raise ApiError(503, "too many event streams open")
            q = queue.Queue(maxsize=2000)
            self.subs.add(q)
            return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, event, data):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

    @property
    def n(self):
        return len(self.subs)


# ================================================================================================ plans
class PlanStore:
    def __init__(self, studio):
        self.s = studio
        self.dir = studio.cfg.p("plans")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks = {}
        self._glock = threading.Lock()
        self.own = {}                       # path -> (mtime_ns, size) of the studio's own writes

    def lock(self, slug):
        with self._glock:
            return self._locks.setdefault(slug, threading.RLock())

    @staticmethod
    def check_slug(slug):
        if not slug or not SLUG_RE.match(slug) or slug == "new":
            raise ApiError(400, f"bad plan name {slug!r} (letters, digits, - and _)")
        return slug

    def draft_path(self, slug):
        return self.dir / f"{slug}.draft.json"

    def hist_dir(self, slug):
        return self.dir / ".history" / slug

    # -- listing ------------------------------------------------------------------------------
    @staticmethod
    def classify(name):
        """file name -> (slug, kind, version) or None. kind: draft | version | plan."""
        if not name.endswith(".json") or name.startswith("."):
            return None
        if name.endswith(".draft.json"):
            return name[:-11], "draft", None
        m = VERSION_RE.match(name)
        if m:
            return m.group("slug"), "version", int(m.group("n"))
        return name[:-5], "plan", None

    def version_files(self, slug):
        out = []
        for e in os.scandir(self.dir):
            c = self.classify(e.name)
            if not c or c[0] != slug or c[1] == "draft" or not e.is_file():
                continue
            v = c[2]
            if v is None:
                d = _read_json(e.path, {})
                v = d.get("version") if isinstance(d, dict) else None
            out.append({"file": e.name, "path": f"plans/{e.name}", "version": v, "kind": c[1],
                        "mtime": e.stat().st_mtime})
        out.sort(key=lambda x: (x["version"] if isinstance(x["version"], (int, float)) else -1, x["mtime"]))
        return out

    def list(self):
        groups = {}
        for e in os.scandir(self.dir):
            if not e.is_file():
                continue
            c = self.classify(e.name)
            if not c:
                continue
            slug, kind, v = c
            g = groups.setdefault(slug, {"slug": slug, "draft": None, "versions": [], "mtime": 0, "title": None})
            st = e.stat()
            g["mtime"] = max(g["mtime"], st.st_mtime)
            if kind == "draft":
                g["draft"] = e.name
            else:
                g["versions"].append({"file": e.name, "version": v, "kind": kind, "mtime": st.st_mtime})
        for g in groups.values():
            g["versions"].sort(key=lambda x: (x["version"] or 0, x["mtime"]))
            src = g["draft"] or (g["versions"][-1]["file"] if g["versions"] else None)
            d = _read_json(self.dir / src, {}) if src else {}
            g["title"] = d.get("title") if isinstance(d, dict) else None
            g["updated"] = g["mtime"]
        return sorted(groups.values(), key=lambda g: -g["mtime"])

    # -- draft io --------------------------------------------------------------------------------
    def read_draft(self, slug):
        p = self.draft_path(slug)
        if not p.exists():
            raise ApiError(404, f"no draft for {slug!r}")
        try:
            d = json.loads(p.read_text())
        except ValueError as e:
            raise ApiError(409, f"draft {p.name} is not valid JSON: {e}") from None
        if not isinstance(d, dict):
            raise ApiError(409, f"draft {p.name} is not a JSON object")
        return d

    def write_draft(self, slug, plan, push=True):
        p = self.draft_path(slug)
        _atomic_write(p, _dump_plan(plan))
        st = p.stat()
        self.own[str(p)] = (st.st_mtime_ns, st.st_size)
        if push:
            self.push(slug, plan)

    def open(self, slug):
        """Draft for slug, created from the newest version file when there is none yet."""
        self.check_slug(slug)
        with self.lock(slug):
            p = self.draft_path(slug)
            if not p.exists():
                vs = self.version_files(slug)
                if not vs:
                    raise ApiError(404, f"no plan named {slug!r}")
                base = _read_json(self.dir / vs[-1]["file"])
                if not isinstance(base, dict):
                    raise ApiError(409, f"{vs[-1]['file']} is not a valid plan")
                self.write_draft(slug, base, push=False)
            self.sync(slug)
            return self.read_draft(slug)

    def new(self, title, slug=None):
        from .render import slugify
        base = self.check_slug(slug) if slug else slugify(title or "untitled")
        s, k = base, 1
        while self.draft_path(s).exists() or self.version_files(s):
            k += 1
            s = f"{base}-{k}"
        plan = {"title": title or "Untitled", "version": 1, "sections": [{"name": "main", "segments": []}]}
        with self.lock(s):
            self.write_draft(s, plan)
        return s, plan

    def commit(self, slug):
        with self.lock(slug):
            self.sync(slug)
            plan = self.read_draft(slug)
            n = 0
            for v in self.version_files(slug):
                if isinstance(v["version"], (int, float)):
                    n = max(n, int(v["version"]))
            n += 1
            plan["version"] = n
            name = f"{slug}_v{n}.json"
            out = self.dir / name
            if out.exists():
                raise ApiError(409, f"{name} already exists")
            _atomic_write(out, _dump_plan(plan))
            st = out.stat()
            self.own[str(out)] = (st.st_mtime_ns, st.st_size)
            self.write_draft(slug, plan)
            return name, plan

    # -- history ----------------------------------------------------------------------------------
    def _hidx(self, slug):
        d = _read_json(self.hist_dir(slug) / "index.json", None)
        if not isinstance(d, dict) or "ids" not in d:
            d = {"ids": [], "pos": -1, "next": 0}
        return d

    def _hsave(self, slug, idx):
        _atomic_write(self.hist_dir(slug) / "index.json", json.dumps(idx))

    def current(self, slug):
        idx = self._hidx(slug)
        if not (0 <= idx["pos"] < len(idx["ids"])):
            return None
        return _read_json(self.hist_dir(slug) / f"{idx['ids'][idx['pos']]}.json")

    def push(self, slug, plan):
        with self.lock(slug):
            idx = self._hidx(slug)
            hd = self.hist_dir(slug)
            for i in idx["ids"][idx["pos"] + 1:]:                  # a new edit drops the redo branch
                (hd / f"{i}.json").unlink(missing_ok=True)
            idx["ids"] = idx["ids"][:idx["pos"] + 1]
            nid = idx["next"]
            idx["next"] += 1
            _atomic_write(hd / f"{nid}.json", json.dumps(plan, ensure_ascii=False))
            idx["ids"].append(nid)
            while len(idx["ids"]) > HISTORY_MAX:
                (hd / f"{idx['ids'].pop(0)}.json").unlink(missing_ok=True)
            idx["pos"] = len(idx["ids"]) - 1
            self._hsave(slug, idx)

    def sync(self, slug):
        """If the draft changed outside the studio (Claude, an editor), record it as an undo step."""
        with self.lock(slug):
            p = self.draft_path(slug)
            if not p.exists():
                return False
            try:
                d = json.loads(p.read_text())
            except ValueError:
                return False
            cur = self.current(slug)
            if cur is None or _canon(cur) != _canon(d):
                self.push(slug, d)
                return True
            return False

    def step(self, slug, delta):
        with self.lock(slug):
            self.sync(slug)
            idx = self._hidx(slug)
            np_ = idx["pos"] + delta
            if not (0 <= np_ < len(idx["ids"])):
                raise ApiError(409, "nothing to " + ("undo" if delta < 0 else "redo"))
            plan = _read_json(self.hist_dir(slug) / f"{idx['ids'][np_]}.json")
            if not isinstance(plan, dict):
                raise ApiError(500, "history snapshot missing")
            idx["pos"] = np_
            self._hsave(slug, idx)
            self.write_draft(slug, plan, push=False)
            return plan

    def history_info(self, slug):
        idx = self._hidx(slug)
        return {"pos": idx["pos"], "n": len(idx["ids"]), "can_undo": idx["pos"] > 0,
                "can_redo": idx["pos"] < len(idx["ids"]) - 1}


# ================================================================================================ plan ops
EPS = 0.02


def _segs_of(plan, si):
    secs = plan.get("sections") or []
    if not isinstance(si, int) or not 0 <= si < len(secs):
        raise ApiError(400, f"no section {si}")
    return secs[si].setdefault("segments", [])


def _seg_ref(op, key="seg"):
    v = op.get(key)
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ApiError(400, f"{key} must be [section, segment]")
    try:
        return int(v[0]), int(v[1])
    except (TypeError, ValueError):
        raise ApiError(400, f"{key} must be [section, segment] integers") from None


def _get_seg(plan, si, gi):
    segs = _segs_of(plan, si)
    if not 0 <= gi < len(segs):
        raise ApiError(400, f"no segment {gi} in section {si}")
    return segs[gi]


def _segrow(tl, si, gi):
    r = next((s for s in tl["segments"] if s["sec"] == si and s["seg"] == gi), None)
    if r is None:
        raise ApiError(409, f"segment [{si}, {gi}] is not on the resolved timeline")
    return r


def _part(items, cut):
    """Split items with segment-relative 'at' at `cut`: (before, after-with-at-shifted)."""
    a, b = [], []
    for it in items or []:
        at = float(it.get("at", 0) or 0)
        if at < cut - 1e-6:
            a.append(it)
        else:
            b.append({**it, "at": round(at - cut, 4)})
    return a, b


def _split_seg(st, plan, tl, r, t):
    """Split plan segment r (resolved row) at timeline time t, in place: [A, B] replaces it."""
    from .render import locate
    si, gi = r["sec"], r["seg"]
    seg = _get_seg(plan, si, gi)
    typ = seg.get("type")
    rel = t - r["start"]
    if rel < EPS or r["end"] - t < EPS:
        raise ApiError(409, "split point is on a cut already")
    A, B = copy.deepcopy(seg), copy.deepcopy(seg)
    loc = locate(tl, t)
    if typ == "talk":
        if not loc or loc["sec"] != si or loc["seg"] != gi or loc["src_t"] is None:
            raise ApiError(409, "no source time under the split point")
        s = loc["src_t"]
        a_in, a_out = float(seg["in"]), float(seg["out"])
        if not (a_in + 0.04 < s < a_out - 0.04):
            raise ApiError(409, f"split at source {s:.3f}s is outside {a_in}-{a_out}")
        A["out"], B["in"] = round(s, 4), round(s, 4)
        d = s - a_in                                  # talk layers are anchored to source time
        for k in ("overlays", "cutaways"):
            if seg.get(k):
                A[k], B[k] = _part(seg[k], d)
        if seg.get("sfx"):
            A["sfx"], B["sfx"] = _part(seg["sfx"], rel)
    elif typ == "broll":
        s = loc["src_t"] if loc and loc["sec"] == si and loc["seg"] == gi else None
        if s is None:
            raise ApiError(409, "no source time under the split point")
        A["out"], B["in"] = round(s, 4), round(s, 4)
        vo = seg.get("voiceover")
        if vo:
            vs = round(float(vo["in"]) + rel, 4)
            A["voiceover"] = {**vo, "out": vs}
            B["voiceover"] = {**vo, "in": vs}
        for k in ("overlays", "cutaways", "sfx"):
            if seg.get(k):
                A[k], B[k] = _part(seg[k], rel)
    elif typ == "montage":
        pcs = sorted([p for p in tl["pieces"] if p["track"] == "V1" and p["sec"] == si and p["seg"] == gi],
                     key=lambda p: p["start"])
        shots = seg.get("shots") or []
        k = next((i for i, p in enumerate(pcs) if p["start"] - 1e-6 <= t < p["end"] - 1e-6), None)
        if k is None or k >= len(shots):
            raise ApiError(409, "no montage shot under the split point")
        p = pcs[k]
        if t - p["start"] <= EPS:
            A["shots"], B["shots"] = shots[:k], shots[k:]
        else:
            s = round(p["in"] + (t - p["start"]) * float(p.get("speed") or 1), 4)
            sa = {**shots[k], "in": p["in"], "out": s}
            sb = {**shots[k], "in": s}
            if shots[k].get("out") is not None:
                sb["out"] = shots[k]["out"]
            A["shots"], B["shots"] = shots[:k] + [sa], [sb] + shots[k + 1:]
        if not A["shots"] or not B["shots"]:
            raise ApiError(409, "split point is on a cut already")
        for k2 in ("overlays", "cutaways", "sfx"):
            if seg.get(k2):
                A[k2], B[k2] = _part(seg[k2], rel)
    elif typ in ("still", "title", "black"):
        A["dur"], B["dur"] = round(rel, 4), round(r["end"] - t, 4)
        for k2 in ("overlays", "cutaways", "sfx"):
            if seg.get(k2):
                A[k2], B[k2] = _part(seg[k2], rel)
    else:
        raise ApiError(409, f"a {typ} segment can't be split")
    _segs_of(plan, si)[gi:gi + 1] = [A, B]


def _cut(st, plan, tl, t):
    """Make a cut at timeline t (splitting the segment there if needed); return the (section,
    index) where a segment inserted at t goes."""
    secs = plan.setdefault("sections", [])
    if not secs:
        secs.append({"name": "main", "segments": []})
    rows = tl["segments"]
    if not rows or t >= rows[-1]["end"] - EPS:          # at/after the end: append to the last section
        return len(secs) - 1, len(secs[-1].setdefault("segments", []))
    if t <= rows[0]["start"] + EPS:
        return rows[0]["sec"], rows[0]["seg"]
    for r in rows:
        if abs(t - r["start"]) <= EPS:
            return r["sec"], r["seg"]
        if r["start"] < t < r["end"]:
            if r["end"] - t <= EPS:
                return r["sec"], r["seg"] + 1
            _split_seg(st, plan, tl, r, t)
            return r["sec"], r["seg"] + 1
    last = rows[-1]
    return last["sec"], last["seg"] + 1


def _check_segment(seg):
    if not isinstance(seg, dict) or seg.get("type") not in P.SEG_TYPES:
        raise ApiError(400, f"segment must be an object with type in {sorted(P.SEG_TYPES)}")
    return copy.deepcopy(seg)


def op_move(st, plan, tl, op):
    si, gi = _seg_ref(op)
    ti, tg = _seg_ref(op, "to")
    seg = _get_seg(plan, si, gi)
    _segs_of(plan, ti)                                   # validates target section
    _segs_of(plan, si).pop(gi)
    dst = _segs_of(plan, ti)
    dst.insert(max(0, min(tg, len(dst))), seg)


def op_trim(st, plan, tl, op):
    si, gi = _seg_ref(op)
    seg = _get_seg(plan, si, gi)
    edge = op.get("edge")
    if edge not in ("in", "out"):
        raise ApiError(400, "edge must be 'in' or 'out'")
    d = _f(op.get("delta"), "delta")
    t = seg.get("type")
    if t in ("talk", "broll"):
        dur = None
        try:
            with st.pool.get() as con:
                dur = float(db.get_clip(con, seg["clip"])["duration"] or 0)
        except Exception:  # noqa: BLE001
            pass
        a, b = float(seg["in"]), float(seg["out"])
        if edge == "in":
            na = max(0.0, min(a + d, b - 0.1))
            seg["in"] = round(na, 4)
            if t == "talk":                         # keep talk layers on their source moments
                for k in ("overlays", "cutaways"):
                    for it in seg.get(k) or []:
                        it["at"] = round(max(0.0, float(it.get("at", 0) or 0) - (na - a)), 4)
        else:
            nb = max(a + 0.1, b + d)
            if dur:
                nb = min(nb, dur)
            seg["out"] = round(nb, 4)
    elif t in ("still", "title", "black"):
        cur = float(seg.get("dur", 2.0 if t != "black" else 1.0))
        seg["dur"] = round(max(0.04, cur + (d if edge == "out" else -d)), 4)
        if t == "still" and edge == "in":
            seg["t"] = round(max(0.0, float(seg.get("t", 0)) + d), 4)
            seg["dur"] = round(max(0.04, cur), 4)
    elif t == "matchcut":
        if edge == "in":
            seg["pre"] = round(max(0.04, float(seg.get("pre", 0.8)) - d), 4)
        else:
            seg["post"] = round(max(0.04, float(seg.get("post", 1.4)) + d), 4)
    elif t == "montage":
        shots = seg.get("shots") or []
        if not shots:
            raise ApiError(409, "montage has no shots")
        if edge == "in":
            sh = shots[0]
            if sh.get("in") is not None:
                sh["in"] = round(max(0.0, float(sh["in"]) + d), 4)
        else:
            seg["max_shot"] = round(max(float(seg.get("min_shot", 0.4)), float(seg.get("max_shot", 1.6)) + d / max(1, len(shots))), 4)
    else:
        raise ApiError(409, f"can't trim a {t} segment")


def op_split(st, plan, tl, op):
    t = _f(op.get("t"), "t")
    r = next((x for x in tl["segments"] if x["start"] + EPS < t < x["end"] - EPS), None)
    if r is None:
        raise ApiError(409, f"no segment to split at {t:.3f}s (on a cut or outside the timeline)")
    _split_seg(st, plan, tl, r, t)


def op_delete(st, plan, tl, op):
    si, gi = _seg_ref(op)
    _get_seg(plan, si, gi)
    if op.get("ripple", True):
        _segs_of(plan, si).pop(gi)
    else:                                                # lift: leave a gap of the same length
        r = _segrow(tl, si, gi)
        _segs_of(plan, si)[gi] = {"type": "black", "dur": round(max(0.04, r["end"] - r["start"]), 4),
                                  "note": "gap left by a lift"}


def op_insert(st, plan, tl, op):
    seg = _check_segment(op.get("segment"))
    si, gi = _cut(st, plan, tl, _f(op.get("at_t", 0), "at_t"))
    _segs_of(plan, si).insert(gi, seg)


def _seg_duration(st, plan, seg):
    mini = {k: v for k, v in plan.items() if k in ("output", "style")}
    mini["sections"] = [{"name": "x", "segments": [seg]}]
    return st.resolve(mini)["duration"]


def op_overwrite(st, plan, tl, op):
    seg = _check_segment(op.get("segment"))
    t0 = _f(op.get("at_t", 0), "at_t")
    d = _seg_duration(st, plan, seg)
    t1 = t0 + d
    if t1 < tl["duration"] - EPS:
        _cut(st, plan, tl, t1)
        tl = st.resolve(plan)
    si, gi = _cut(st, plan, tl, t0)
    tl = st.resolve(plan)
    doomed = [(r["sec"], r["seg"]) for r in tl["segments"] if r["start"] >= t0 - EPS and r["end"] <= t1 + EPS]
    for a, b in sorted(doomed, reverse=True):
        _segs_of(plan, a).pop(b)
    _segs_of(plan, si).insert(min(gi, len(_segs_of(plan, si))), seg)


def op_freeze(st, plan, tl, op):
    from .render import locate
    t = _f(op.get("t"), "t")
    dur = _f(op.get("dur", 1.0), "dur")
    if dur <= 0:
        raise ApiError(400, "dur must be > 0")
    loc = locate(tl, t, "V2")
    if not (loc and loc["piece"]["start"] - 1e-6 <= t < loc["piece"]["end"]):
        loc = locate(tl, t)
    if not loc or not loc["clip"] or loc["src_t"] is None:
        raise ApiError(409, "no picture under the playhead to freeze")
    still = {"type": "still", "clip": loc["clip"], "t": round(loc["src_t"], 4), "dur": round(dur, 4), "zoom": 1.0,
             "note": "freeze frame"}
    si, gi = _cut(st, plan, tl, t)
    _segs_of(plan, si).insert(gi, still)


def op_set(st, plan, tl, op):
    si, gi = _seg_ref(op)
    seg = _get_seg(plan, si, gi)
    fields = op.get("fields")
    if not isinstance(fields, dict):
        raise ApiError(400, "fields must be an object")
    for k, v in fields.items():
        if v is None:
            seg.pop(k, None)
        else:
            seg[k] = v


def _deep_merge(base, over):
    for k, v in over.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def op_set_style(st, plan, tl, op):
    fields = op.get("fields")
    if not isinstance(fields, dict):
        raise ApiError(400, "fields must be an object")
    _deep_merge(plan.setdefault("style", {}), fields)


def op_add_section(st, plan, tl, op):
    secs = plan.setdefault("sections", [])
    new = {"name": str(op.get("name") or f"section {len(secs) + 1}"), "segments": []}
    if op.get("music"):
        new["music"] = op["music"]
    if op.get("at_t") is not None:                       # split the timeline: everything after t moves
        si, gi = _cut(st, plan, tl, _f(op["at_t"], "at_t"))
        segs = _segs_of(plan, si)
        new["segments"] = segs[gi:]
        del segs[gi:]
        secs.insert(si + 1, new)
        return
    at = op.get("at", len(secs))
    try:
        at = int(at)
    except (TypeError, ValueError):
        raise ApiError(400, "at must be a section index (or use at_t for a timeline time)") from None
    secs.insert(max(0, min(at, len(secs))), new)


def op_set_music(st, plan, tl, op):
    si = op.get("sec")
    if not isinstance(si, int):
        raise ApiError(400, "sec must be a section index")
    _segs_of(plan, si)
    m = op.get("music")
    if m:
        if not isinstance(m, dict) or not m.get("song"):
            raise ApiError(400, "music must be {song, offset, ...} or null")
        plan["sections"][si]["music"] = m
    else:
        plan["sections"][si].pop("music", None)


def _rel_at(st, plan, tl, si, gi, item):
    """Turn an item's timeline `at_t` into the segment-relative `at` the plan uses (source-relative
    on talk, output-relative elsewhere)."""
    from .render import locate
    item = dict(item)
    if item.get("at_t") is None:
        return item
    t = _f(item.pop("at_t"), "at_t")
    seg = _get_seg(plan, si, gi)
    r = _segrow(tl, si, gi)
    if seg.get("type") == "talk":
        loc = locate(tl, min(max(t, r["start"]), r["end"] - 1e-3))
        if not loc or loc["sec"] != si or loc["seg"] != gi or loc["src_t"] is None:
            raise ApiError(409, "at_t is outside the segment")
        item["at"] = round(loc["src_t"] - float(seg["in"]), 4)
    else:
        item["at"] = round(max(0.0, t - r["start"]), 4)
    return item


def op_add_cutaway(st, plan, tl, op):
    si, gi = _seg_ref(op)
    c = op.get("cutaway")
    if not isinstance(c, dict) or not c.get("clip"):
        raise ApiError(400, "cutaway must be {clip, in, at|at_t, dur}")
    c = _rel_at(st, plan, tl, si, gi, c)
    c.setdefault("in", 0.0)
    c.setdefault("at", 0.0)
    c.setdefault("dur", 2.0)
    _get_seg(plan, si, gi).setdefault("cutaways", []).append(c)


def op_add_overlay(st, plan, tl, op):
    si, gi = _seg_ref(op)
    o = op.get("overlay")
    if not isinstance(o, dict) or not str(o.get("text", "")).strip():
        raise ApiError(400, "overlay must be {kind, text, at|at_t, dur, position}")
    o = _rel_at(st, plan, tl, si, gi, o)
    o.setdefault("kind", "callout")
    o.setdefault("at", 0.3)
    o.setdefault("dur", 2.5)
    o.setdefault("position", "auto")
    _get_seg(plan, si, gi).setdefault("overlays", []).append(o)


OPS = {"move": op_move, "trim": op_trim, "split": op_split, "delete": op_delete, "insert": op_insert,
       "overwrite": op_overwrite, "freeze": op_freeze, "set": op_set, "set_style": op_set_style,
       "add_section": op_add_section, "set_music": op_set_music, "add_cutaway": op_add_cutaway,
       "add_overlay": op_add_overlay}


# ================================================================================================ jobs
class Job:
    def __init__(self, kind, cmd, title="", meta=None):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.cmd, self.title, self.meta = kind, cmd, title or kind, meta or {}
        self.status, self.progress, self.stage = "queued", 0.0, "queued"
        self.log = collections.deque(maxlen=600)
        self.created, self.started, self.ended = time.time(), None, None
        self.rc, self.result, self.error = None, None, None
        self.proc = None
        self.stop_requested = False

    def eta_s(self):
        if self.status != "running" or not self.started or self.progress < 0.05:
            return None
        el = time.time() - self.started
        return round(el * (1 - self.progress) / self.progress, 1)

    def to_dict(self, tail=30):
        return {"id": self.id, "kind": self.kind, "title": self.title, "label": self.title, "status": self.status,
                "eta_s": self.eta_s(),
                "progress": round(self.progress, 4), "stage": self.stage, "created": self.created,
                "started": self.started, "ended": self.ended, "rc": self.rc, "result": self.result,
                "error": self.error, "meta": self.meta, "cmd": " ".join(map(str, self.cmd))[:300] if self.cmd else "",
                "log_tail": list(self.log)[-tail:] if tail else []}


def _parse_render(job, line):
    s = line.strip()
    if s.startswith("render '"):
        job.progress, job.stage = 0.02, "expanding"
    elif s.startswith("timeline:"):
        job.progress, job.stage = 0.05, "probing " + s[len("timeline:"):].strip()
    elif s.startswith("graphics:"):
        job.progress, job.stage = 0.1, "graphics done"
    elif s.startswith("progress "):
        m = re.match(r"progress (\d+)/(\d+)", s)
        if m:
            k, n = int(m.group(1)), max(1, int(m.group(2)))
            job.progress, job.stage = 0.1 + 0.8 * k / n, f"segments {k}/{n}"
    elif s.startswith("segments:"):
        job.progress, job.stage = 0.9, "mixing + encoding"
    elif s.startswith("done:"):
        job.progress, job.stage = 1.0, "done"
        job.result = s[5:].split(" · ")[0].strip()


def _parse_qa(job, line):
    s = line.strip()
    if s.startswith("{"):
        job.stage = "summarising"
        job.progress = max(job.progress, 0.9)
    elif s:
        job.progress = min(0.85, job.progress + 0.02)
        job.stage = s[:80]


class Jobs:
    def __init__(self, studio):
        self.s = studio
        self.jobs = collections.OrderedDict()
        self.lock = threading.Lock()
        self.lanes = {"heavy": threading.Semaphore(1), "overnight": threading.Semaphore(1)}

    def running_count(self):
        return sum(1 for j in list(self.jobs.values()) if j.status == "running")

    def list(self):
        with self.lock:
            return [j.to_dict() for j in reversed(self.jobs.values())]

    def get(self, jid):
        j = self.jobs.get(jid)
        if not j:
            raise ApiError(404, f"no job {jid}")
        return j

    def add(self, job):
        with self.lock:
            queued = sum(1 for j in self.jobs.values() if j.status == "queued")
            if queued >= 20:
                raise ApiError(429, "too many queued jobs")
            self.jobs[job.id] = job
            while len(self.jobs) > 100:
                old = next(iter(self.jobs))
                if self.jobs[old].status in ("running", "queued"):
                    break
                self.jobs.pop(old)
        return job

    def emit(self, job, force=False):
        now = time.time()
        if force or now - getattr(job, "_last_emit", 0) > 0.25:
            job._last_emit = now
            self.s.bus.publish("job", job.to_dict(tail=5))

    def submit(self, kind, cmd, title="", parse=None, on_done=None, lane="heavy", meta=None, env=None):
        job = self.add(Job(kind, cmd, title, meta))
        threading.Thread(target=self._run, args=(job, parse, on_done, lane, env), daemon=True,
                         name=f"job-{job.id}").start()
        self.emit(job, True)
        return job

    def _run(self, job, parse, on_done, lane, env):
        sem = self.lanes.setdefault(lane, threading.Semaphore(1))
        while not sem.acquire(timeout=1.0):
            if job.stop_requested or self.s.stopping.is_set():
                job.status, job.stage, job.ended = "stopped", "stopped before start", time.time()
                self.emit(job, True)
                return
        try:
            if job.stop_requested:
                job.status, job.stage, job.ended = "stopped", "stopped before start", time.time()
                return
            job.status, job.started, job.stage = "running", time.time(), "starting"
            self.emit(job, True)
            try:
                job.proc = subprocess.Popen(job.cmd, cwd=str(self.s.cfg.work), env=_sub_env(self.s.cfg, env),
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            text=True, bufsize=1, errors="replace", start_new_session=True)
            except OSError as e:
                job.status, job.error, job.ended = "error", f"could not start: {e}", time.time()
                return
            for line in job.proc.stdout:
                line = line.rstrip("\n")
                job.log.append(line)
                if parse:
                    try:
                        parse(job, line)
                    except Exception:  # noqa: BLE001
                        pass
                self.emit(job)
            job.rc = job.proc.wait()
            job.ended = time.time()
            if job.stop_requested:
                job.status, job.stage = "stopped", "stopped"
            elif job.rc == 0:
                job.status, job.progress = "done", 1.0
                job.stage = "done"
            else:
                job.status = "error"
                tail = [x for x in list(job.log)[-12:] if x.strip()]
                job.error = "\n".join(tail)[-2000:] or f"exit code {job.rc}"
            if on_done:
                try:
                    on_done(job)
                except Exception as e:  # noqa: BLE001
                    job.log.append(f"studio: on_done failed: {e}")
        finally:
            sem.release()
            if job.ended is None:
                job.ended = time.time()
            self.emit(job, True)
            self.s.bus.publish("jobs", {"running": self.running_count()})

    def stop(self, jid):
        job = self.get(jid)
        job.stop_requested = True
        p = job.proc
        if p and p.poll() is None:
            _terminate(p)
        return job

    def stop_all(self):
        for j in list(self.jobs.values()):
            if j.status in ("queued", "running"):
                try:
                    self.stop(j.id)
                except Exception:  # noqa: BLE001
                    pass


def _terminate(p, grace=5.0):
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.terminate()
        except OSError:
            pass

    def reap():
        try:
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                try:
                    p.kill()
                except OSError:
                    pass
    threading.Thread(target=reap, daemon=True).start()


# ================================================================================================ agent bridge
# The chat panel talks to Claude Code or Codex (cutroom/agent.py), whichever the project uses.

INSTALL_STEPS = agentmod.ClaudeAgent(None).install_steps()
ALLOWED_TOOLS = agentmod.CHAT_TOOLS
find_claude = agentmod.find_claude
tool_summary = agentmod._tool_summary
_result_text = agentmod._result_text


def _short(v, n=160):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


class ChatBridge:
    def __init__(self, studio):
        self.s = studio
        self.dir = Path(studio.cfg.work) / "studio"
        self.proc = None
        self.lock = threading.Lock()
        self.stopped = False
        self.busy = False
        self.job = None
        self.agent = None

    @property
    def state_path(self):
        return self.dir / "chat.json"

    @property
    def history_path(self):
        return self.dir / "chat_history.jsonl"

    def session_id(self, agent=None):
        """The saved session; with `agent`, only if it belongs to that agent (a Claude session can't be
        resumed by Codex and vice versa)."""
        st = _read_json(self.state_path, {}) or {}
        if agent and (st.get("agent") or "claude") != agent:
            return None
        return st.get("session_id")

    def set_session(self, sid, agent=None):
        _atomic_write(self.state_path, json.dumps({"session_id": sid, "agent": agent or (self.agent.name if self.agent else None),
                                                   "updated": time.time()}))

    def new(self):
        sid = self.session_id()
        self.set_session(None)
        self._hist({"role": "system", "text": "new chat", "previous_session": sid})

    def _hist(self, rec):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def history(self, n=200):
        if not self.history_path.exists():
            return []
        out = []
        with open(self.history_path, errors="replace") as f:
            for line in collections.deque(f, maxlen=n):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def stop(self):
        self.stopped = True
        p = self.proc
        if p and p.poll() is None:
            _terminate(p, grace=3)
            return True
        return False

    def running(self):
        return self.busy or (self.proc is not None and self.proc.poll() is None)

    # -- prompt -----------------------------------------------------------------------------------
    def attachment_paths(self, attachments):
        out, bad = [], []
        for a in attachments or []:
            path = self.s.media_url_to_path(str(a))
            (out if path and path.is_file() else bad).append(str(path) if path and path.is_file() else str(a))
        return out, bad

    def compose(self, message, context, attachments):
        ctx = context if isinstance(context, dict) and context else (_read_json(self.dir / "context.json", {}) or {})
        lines = []
        slug = ctx.get("plan")
        tl = None
        if slug and SLUG_RE.match(str(slug)):
            dp = self.s.plans.draft_path(slug)
            if dp.exists():
                lines.append(f"- Plan draft open in the editor: `{dp}`. Edit this draft file; the studio reloads it on save.")
                try:
                    tl = self.s.resolve(self.s.plans.read_draft(slug))
                except Exception:  # noqa: BLE001
                    tl = None
        ph = ctx.get("playhead")
        if ph is not None:
            try:
                ph = float(ph)
                line = f"- Playhead: {ph:.2f}s ({_mmss(ph)})"
                if tl:
                    from .render import locate
                    loc = locate(tl, ph)
                    if loc:
                        p = loc["piece"]
                        line += f", segment [{loc['sec']}, {loc['seg']}] ({p['type']}"
                        if p.get("clip"):
                            line += f", clip {p['clip_rel']} ({p['clip']}) at source {loc['src_t']:.2f}s, piece in {p['in']} out {p['out']}"
                        line += f", label {p['label']!r})"
                        v2 = locate(tl, ph, "V2")
                        if v2 and v2["piece"]["start"] <= ph < v2["piece"]["end"]:
                            line += f"; cutaway on screen: {v2['piece']['clip_rel']} at source {v2['src_t']:.2f}s"
                lines.append(line)
            except (TypeError, ValueError):
                pass
        sel = ctx.get("selection")
        if sel:
            lines.append(f"- Selected segments [section, segment]: {json.dumps(sel)}")
        src = ctx.get("source")
        if isinstance(src, dict) and src.get("clip"):
            s = f"- Source monitor: clip {src.get('clip_rel') or ''} ({src['clip']})"
            if src.get("in") is not None or src.get("out") is not None:
                s += f", In {src.get('in')} Out {src.get('out')}"
            if src.get("t") is not None:
                s += f", at {src.get('t')}s"
            lines.append(s)
        if ctx.get("render"):
            lines.append(f"- Program monitor is playing the render `renders/{ctx['render']}`")
        ref = ctx.get("reference")
        if isinstance(ref, dict) and ref.get("file"):
            lines.append(f"- Reference video: `renders/reference/{ref['file']}` at {ref.get('t', 0)}s")
        try:
            ms = [m for m in markers_load(self.s.cfg) if m.get("status", "open") == "open"
                  and (not slug or not m.get("plan") or m.get("plan") == slug)]
        except Exception:  # noqa: BLE001
            ms = []
        if ms:
            lines.append(f"- Open markers ({len(ms)}; full list in `studio/markers.json`, mark handled ones with "
                         f"`cutroom markers done <id>`):")
            for m in ms[:20]:
                fr = self.s.media_url_to_path(m.get("frame_url") or "")
                lines.append(f"  - {m['id']} @ {float(m.get('t', 0)):.2f}s [{m.get('kind', 'note')}]: "
                             f"{m.get('text', '')}" + (f" (frame `{fr}`)" if fr else ""))
        for k in ("note", "extra"):
            if ctx.get(k):
                lines.append(f"- {k}: {_short(ctx[k], 400)}")
        out = str(message or "").strip()
        if lines:
            out += "\n\n## What I'm pointing at\n" + "\n".join(lines)
        paths, bad = self.attachment_paths(attachments)
        if paths:
            out += "\n\nLook at these images:\n" + "\n".join(f"- {p}" for p in paths)
        out += REPLY_STYLE
        return out, bad

    # -- run ----------------------------------------------------------------------------------------
    def cmd(self, binary, sid, images=()):
        a = self.agent if (self.agent and self.agent.path == binary) else agentmod.ClaudeAgent(binary)
        return a.chat_cmd(sid, self.s.cfg.work, REPO, images)

    def run(self, message, attachments, context, emit):
        """Run one chat turn; emit(dict) is called for every event (it may raise if the client left:
        the turn still runs to completion so the session and history stay consistent)."""
        client = {"ok": True}

        def send(ev):
            if ev.get("type") == "error":
                ev.setdefault("message", ev.get("error"))
            elif ev.get("type") == "done":
                ev.setdefault("session", ev.get("session_id"))
            elif ev.get("type") == "tool_result":
                ev.setdefault("ok", not ev.get("is_error"))
            self.s.bus.publish("chat", ev)
            if client["ok"]:
                try:
                    emit(ev)
                except Exception:  # noqa: BLE001
                    client["ok"] = False

        if not str(message or "").strip():
            raise ApiError(400, "message is empty")
        ag = agentmod.choose(self.s.cfg)
        if not ag.path:
            send({"type": "error", "error": f"{ag.name} not installed", "install": ag.install_steps(), "agent": ag.name})
            return
        if ag.name == "codex" and ag.logged_in is False:
            send({"type": "error", "error": "codex is not logged in", "install": ag.install_steps(), "agent": ag.name})
            return
        with self.lock:
            if self.running():
                raise ApiError(409, f"{ag.label} is already working on a message; stop it first")
            self.busy, self.stopped, self.proc, self.agent = True, False, None, ag
        try:
            self._turn(message, attachments, context, send, ag)
        finally:
            with self.lock:
                self.busy, self.proc = False, None

    def _turn(self, message, attachments, context, send, ag):
        if isinstance(context, dict) and context:
            self.s.save_context(context)
        prompt, bad = self.compose(message, context, attachments)
        images, _ = self.attachment_paths(attachments)
        if bad:
            send({"type": "warning", "text": f"attachments not found: {bad}"})
        self._hist({"role": "user", "text": str(message), "attachments": attachments or [], "context": context or {},
                    "agent": ag.name})
        job = self.s.jobs.add(Job("chat", [ag.path], title=_short(message, 60), meta={"agent": ag.name}))
        job.status, job.started, job.stage = "running", time.time(), "thinking"
        self.job = job
        self.s.jobs.emit(job, True)
        sid = self.session_id(ag.name)
        acc = {"text": [], "tools": [], "done": None, "err": None, "err_sent": False, "final": None}
        try:
            for attempt in (1, 2):
                rc, stderr = self._once(ag, sid, prompt, images, send, acc, job)
                if (acc["done"] is None and sid and attempt == 1 and not self.stopped
                        and ag.session_gone(stderr + " " + str(acc.get("last_error") or ""))):
                    send({"type": "warning", "text": "previous chat session is gone; starting a new one"})
                    self.set_session(None, ag.name)
                    sid = None
                    acc.update(err=None, err_sent=False, last_error=None, final=None)
                    continue
                break
            if self.stopped:
                acc["final"] = {"type": "error", "error": "stopped", "stopped": True}
                acc["err"] = "stopped"
            elif acc["done"] is None and not acc["err_sent"]:
                msg = (acc.get("last_error") or stderr.strip()[-1500:] or f"{ag.name} exited with code {rc}")
                acc["final"] = {"type": "error", "error": msg}
                acc["err"] = msg
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            acc["err"] = f"{type(e).__name__}: {e}"
            acc["final"] = {"type": "error", "error": acc["err"]}
        finally:
            d = acc["done"] or {}
            self._hist({"role": "assistant", "text": "".join(acc["text"]), "tools": acc["tools"], "agent": ag.name,
                        "session_id": d.get("session_id") or self.session_id(), "cost_usd": d.get("cost_usd"),
                        "usage": d.get("usage"), "duration_ms": d.get("duration_ms"), "error": acc["err"]})
            job.ended = time.time()
            job.status = "stopped" if self.stopped else ("error" if acc["err"] else "done")
            job.progress, job.stage, job.error = 1.0, job.status, acc["err"]
            job.result = d.get("session_id")
            self.s.jobs.emit(job, True)
            with self.lock:
                self.busy, self.proc = False, None
        # the final event (done or one error) goes last, after the bridge is free: when the panel sees it,
        # the next message can go
        final = acc["done"] if acc["done"] is not None else acc.get("final")
        if final is not None:
            send(final)

    def _once(self, ag, sid, prompt, images, send, acc, job):
        env = _sub_env(self.s.cfg)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        cmd = ag.chat_cmd(sid, self.s.cfg.work, REPO, images)
        job.cmd = cmd
        try:
            p = subprocess.Popen(cmd, cwd=str(self.s.cfg.work), env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                 errors="replace", start_new_session=True)
        except OSError as e:
            acc["final"] = {"type": "error", "error": f"could not start {ag.name}: {e}", "install": ag.install_steps()}
            acc["err"], acc["err_sent"] = str(e), True
            return -1, ""
        with self.lock:
            self.proc = p
        job.proc = p
        err = collections.deque(maxlen=200)
        t_err = threading.Thread(target=lambda: [err.append(x) for x in p.stderr], daemon=True)
        t_err.start()
        try:
            p.stdin.write(prompt)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        watchdog = threading.Timer(3600, lambda: _terminate(p))     # a turn never runs longer than an hour
        watchdog.daemon = True
        watchdog.start()
        state = ag.new_state()
        try:
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evs = ag.parse_event(line, state)
                except Exception as e:  # noqa: BLE001   a malformed event never kills the turn
                    job.log.append(f"unparsed event ({type(e).__name__}): {line[:200]}")
                    continue
                if not evs and not line.startswith("{"):
                    job.log.append(line[:300])
                for ev in evs:
                    self._handle(ev, send, acc, job)
        finally:
            watchdog.cancel()
        rc = p.wait()
        t_err.join(timeout=2)
        if state.get("last_error"):
            acc["last_error"] = state["last_error"]
        return rc, "".join(err)

    def _handle(self, ev, send, acc, job):
        """One normalized agent event (agent.py) -> panel / history / job."""
        t = ev.get("type")
        if t == "stage":
            job.stage = ev.get("stage") or job.stage
            self.s.jobs.emit(job)
        elif t == "status":
            job.log.append(f"status: {ev.get('text')}")
            job.stage = _short(ev.get("text") or "", 60)
            self.s.jobs.emit(job)
            send(ev)
        elif t == "text":
            acc["text"].append(ev["text"])
            send(ev)
        elif t == "tool":
            acc["tools"].append({"name": ev.get("name"), "input": ev.get("input")})
            job.log.append(f"tool {ev.get('name')}: {ev.get('input')}")
            send(ev)
        elif t == "tool_result":
            send(ev)
        elif t == "session":
            send(ev)
        elif t == "error":                       # a turn-ending error: reported once, at the end of the turn
            acc["err"], acc["err_sent"], acc["final"] = ev.get("error"), True, ev
        elif t == "done":
            sid = ev.get("session_id")
            if sid:
                self.set_session(sid, self.agent.name if self.agent else None)
            acc["done"] = ev
            if ev.get("is_error"):
                acc["err"] = ev.get("error") or "error"


# ================================================================================================ the app
class Studio:
    def __init__(self, cfg):
        self.cfg = cfg
        self.work = Path(cfg.work)
        for d in ("studio", "studio/grabs", "studio/markers", "frames/thumbs", "frames/studio", "renders/reference"):
            (self.work / d).mkdir(parents=True, exist_ok=True)
        self.stopping = threading.Event()
        self.bus = Bus()
        self.pool = ConPool(cfg.db_path)
        self.plans = PlanStore(self)
        self.jobs = Jobs(self)
        self.chat = ChatBridge(self)
        self._klocks = {}
        self._klock = threading.Lock()
        ensure_project_claude(cfg)
        self._ensure_web()
        with self.pool.get() as con:
            try:
                from . import tags
                tags.refresh_stats(con)
            except Exception as e:  # noqa: BLE001
                print(f"studio: clip stats refresh failed: {e}", file=sys.stderr)
        self._threads = [threading.Thread(target=self._watch, daemon=True, name="studio-watch"),
                         threading.Thread(target=self._gov_loop, daemon=True, name="studio-governor")]
        for t in self._threads:
            t.start()

    @staticmethod
    def _ensure_web():
        idx = WEB / "index.html"
        if not idx.exists():
            try:
                WEB.mkdir(parents=True, exist_ok=True)
                if not idx.exists():
                    idx.write_text("<!doctype html><meta charset=utf-8><title>Cutroom Studio</title>"
                                   "<body style='font:15px system-ui;background:#111;color:#ddd;padding:2em'>"
                                   "<h1>Cutroom Studio</h1><p>The editor UI is not built yet "
                                   "(<code>cutroom/studio_web/</code>). The API is up: try "
                                   "<a style='color:#8af' href='/api/project'>/api/project</a>.</p>")
            except OSError:
                pass

    def close(self):
        self.stopping.set()
        self.chat.stop()
        self.jobs.stop_all()
        self.pool.close()

    # -- helpers ---------------------------------------------------------------------------------
    def keylock(self, key):
        with self._klock:
            if len(self._klocks) > 5000:
                self._klocks.clear()
            return self._klocks.setdefault(key, threading.Lock())

    def resolve(self, plan):
        from .render import resolve_timeline
        with self.pool.get() as con:
            return resolve_timeline(self.cfg, con, plan, preview=True)

    def validate(self, plan):
        from .render import extra_validate
        try:
            p = P.normalize(plan, self.cfg)
            with self.pool.get() as con:
                return P.validate(p, con) + extra_validate(p, con, self.cfg)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            return [f"plan cannot be read: {type(e).__name__}: {e}"]

    def plan_payload(self, slug, plan=None):
        plan = plan if plan is not None else self.plans.read_draft(slug)
        hist = self.plans.history_info(slug)
        out = {"slug": slug, "draft": plan, "draft_file": f"plans/{slug}.draft.json",
               "draft_path": str(self.plans.draft_path(slug)),
               "version_files": self.plans.version_files(slug), "history": hist,
               "can_undo": hist["can_undo"], "can_redo": hist["can_redo"]}
        try:
            out["timeline"] = self.resolve(plan)
        except Exception as e:  # noqa: BLE001
            out["timeline"] = None
            out["timeline_error"] = f"{type(e).__name__}: {e}"
        out["errors"] = self.validate(plan)
        return out

    def save_context(self, ctx):
        if not isinstance(ctx, dict):
            raise ApiError(400, "context must be an object")
        ctx = dict(ctx)
        slug = ctx.get("plan")
        if slug and SLUG_RE.match(str(slug)):
            ctx["draft_path"] = str(self.plans.draft_path(slug))
        ctx["updated"] = time.time()
        _atomic_write(self.work / "studio" / "context.json", json.dumps(ctx, indent=1, ensure_ascii=False))
        return ctx

    def media_url_to_path(self, url):
        return self.frame_path_of(self.cfg, url)

    @staticmethod
    def frame_path_of(cfg, url):
        """A media URL served by the studio (or an absolute path inside the project) -> file path."""
        if not url:
            return None
        work = Path(cfg.work)
        u = urlparse(str(url))
        path = unquote(u.path)
        table = {"/media/grab/": work / "studio" / "grabs", "/media/marker/": work / "studio" / "markers",
                 "/media/render/": work / "renders", "/media/ref/": work / "renders" / "reference"}
        for pre, root in table.items():
            if path.startswith(pre):
                return _within(root, path[len(pre):])
        if path.startswith("/") and not u.scheme:
            p = Path(path).resolve()
            if work.resolve() in p.parents:
                return p
        return None

    # -- frames ---------------------------------------------------------------------------------------
    def clip_row(self, cid):
        with self.pool.get() as con:
            r = con.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
            if r is None:
                try:
                    r = db.get_clip(con, cid)
                except KeyError as e:
                    raise ApiError(404, str(e)) from None
            return dict(r)

    def _extract(self, src, t, out, width, quality=3):
        out = Path(out)
        with self.keylock(str(out)):
            if out.exists() and out.stat().st_size > 0:
                return out
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f".{out.stem}.{threading.get_ident()}.tmp.jpg")
            err = ""
            # past the last decodable frame ffmpeg writes nothing: back off once
            for tt in ([t, max(0.0, t - 0.5)] if t > 0.2 else [t]):
                args = [self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                        "-ss", f"{max(0.0, tt):.3f}", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={int(width)}:-2", "-q:v", str(quality), "-f", "image2", str(tmp)]
                with light_slot(self.cfg, label=f"frame {out.name}"):
                    try:
                        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
                    except subprocess.TimeoutExpired:
                        tmp.unlink(missing_ok=True)
                        raise ApiError(504, "frame extraction timed out") from None
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    os.replace(tmp, out)
                    return out
                tmp.unlink(missing_ok=True)
                err = (r.stderr or "").strip()[-300:]
            raise ApiError(500, f"frame extraction failed: {err or 'no frame decoded'}")

    def thumb(self, cid, t=None):
        row = self.clip_row(cid)
        dur = float(row.get("duration") or 0)
        t = dur / 2 if t is None else max(0.0, min(float(t), max(0.0, dur - 0.05)))
        out = self.work / "frames" / "thumbs" / f"{row['id']}_{int(round(t * 10)):07d}.jpg"
        if out.exists():
            return out
        src = row.get("proxy") if row.get("proxy") and os.path.exists(row["proxy"]) else row["path"]
        return self._extract(src, round(t, 1), out, 320, quality=5)

    def frame(self, cid, t, w=1280):
        row = self.clip_row(cid)
        dur = float(row.get("duration") or 0)
        t = max(0.0, min(float(t), max(0.0, dur - 0.05)))
        w = int(max(64, min(int(w), 3840)))
        out = self.work / "frames" / "studio" / f"{row['id']}_{int(round(t * 1000)):09d}_{w}.jpg"
        if out.exists():
            return out
        return self._extract(row["path"], t, out, w, quality=2)

    def _black(self, out, text=""):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (1280, 720), (0, 0, 0))
        if text:
            ImageDraw.Draw(img).text((40, 340), str(text)[:80], fill=(230, 230, 230))
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "JPEG", quality=88)
        return out

    def grab(self, source, t, clip=None, file=None, plan=None, dest="grabs", w=1280):
        """Save a frame jpg into studio/<dest>/ and return (path, url)."""
        t = _f(t, "t")
        root = self.work / "studio" / dest
        stamp = f"{int(time.time() * 1000) % 10**10:010d}"
        if source == "clip":
            if not clip:
                raise ApiError(400, "clip is required for source=clip")
            src = self.frame(clip, t, w)
            name = f"clip_{Path(src).stem}_{stamp}.jpg"
        elif source == "ref":
            p = _within(self.work / "renders" / "reference", file or "")
            if not p or not p.is_file():
                raise ApiError(404, f"no reference video {file!r}")
            src = self._extract(p, t, self.work / "frames" / "studio" / f"ref_{p.stem}_{int(t * 1000):09d}_{w}.jpg", w, 2)
            name = f"ref_{p.stem}_{stamp}.jpg"
        elif source == "program":
            if file:                                      # a render is playing: grab the real picture
                p = _within(self.work / "renders", file)
                if not p or not p.is_file():
                    raise ApiError(404, f"no render {file!r}")
                src = self._extract(p, t, self.work / "frames" / "studio" / f"render_{p.stem}_{int(t * 1000):09d}_{w}.jpg", w, 2)
                name = f"program_{p.stem}_{stamp}.jpg"
            else:
                if not plan:
                    raise ApiError(400, "program grab needs plan (or file)")
                from .render import locate
                tl = self.resolve(self.plans.read_draft(plan))
                loc = locate(tl, t, "V2")
                if not (loc and loc["piece"]["start"] - 1e-6 <= t < loc["piece"]["end"]):
                    loc = locate(tl, t)
                name = f"program_{plan}_{int(t * 1000):09d}_{stamp}.jpg"
                if loc and loc["clip"] and loc["src_t"] is not None:
                    src = self.frame(loc["clip"], loc["src_t"], w)
                else:
                    src = self._black(root / name, (loc or {}).get("piece", {}).get("label", "") if loc else "")
        else:
            raise ApiError(400, "source must be program, clip or ref")
        out = root / name
        if Path(src) != out:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out)
        return out, f"/media/{'grab' if dest == 'grabs' else 'marker'}/{quote(name)}"

    # -- background loops ------------------------------------------------------------------------------
    def _watch(self):
        seen, first = {}, True
        notes = self.work / "notes" / "notes.jsonl"
        mk = markers_path(self.cfg)
        extra = {"notes": notes, "markers": mk}
        extra_seen = {k: _stat(p) for k, p in extra.items()}
        bins_seen = _bins_sig(self.cfg)
        while not self.stopping.wait(1.0):
            try:
                bs = _bins_sig(self.cfg)
                if bs != bins_seen:
                    bins_seen = bs
                    self.bus.publish("bins_changed", {"bins": bins_load(self.cfg)})
                cur = {}
                for e in os.scandir(self.plans.dir):
                    if e.is_file() and e.name.endswith(".json") and not e.name.startswith("."):
                        st = e.stat()
                        cur[e.path] = (st.st_mtime_ns, st.st_size)
                if first:
                    seen, first = cur, False
                else:
                    for path in set(cur) | set(seen):
                        if cur.get(path) == seen.get(path):
                            continue
                        name = os.path.basename(path)
                        c = PlanStore.classify(name) or (name, "plan", None)
                        own = self.plans.own.get(path) == cur.get(path)
                        ev = {"file": name, "path": f"plans/{name}", "slug": c[0], "kind": c[1],
                              "source": "studio" if own else "external", "by": "hand" if own else "claude",
                              "deleted": path not in cur,
                              "mtime": (cur.get(path) or (0,))[0] / 1e9}
                        if c[1] == "draft" and not own and path in cur:
                            try:
                                self.plans.sync(c[0])
                                ev["history"] = self.plans.history_info(c[0])
                            except Exception:  # noqa: BLE001
                                pass
                        self.bus.publish("plan_changed", ev)
                    seen = cur
                for k, p in extra.items():
                    s = _stat(p)
                    if s != extra_seen[k]:
                        extra_seen[k] = s
                        if k == "notes":
                            from . import board
                            self.bus.publish("notes", {"count": len(board.read_notes(self.cfg))})
                        else:
                            self.bus.publish("markers", {"markers": markers_load(self.cfg)})
            except Exception as e:  # noqa: BLE001
                print(f"studio: watcher: {e}", file=sys.stderr)

    def _gov_loop(self):
        was_running = False
        while not self.stopping.wait(3.0):
            if self.bus.n:
                try:
                    self.bus.publish("governor", governor_status(self))
                    ov = overnight_state(self.cfg)
                    if ov.get("running") or was_running:          # one last event when it finishes
                        self.bus.publish("overnight", ov)
                    was_running = bool(ov.get("running"))
                except Exception:  # noqa: BLE001
                    pass


def _stat(p):
    try:
        st = os.stat(p)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


# ================================================================================================ http
def _parse_range(h, size):
    from .board import _parse_range as pr
    return pr(h, size)


class Handler(BaseHTTPRequestHandler):
    studio: Studio = None
    server_version = "cutroom-studio/1"
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith("5"):
            sys.stderr.write("studio: " + (fmt % args) + "\n")

    # -- responses --------------------------------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _file(self, path, cache="max-age=60"):
        if not path or not Path(path).is_file():
            raise ApiError(404, "not found")
        path = Path(path)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = _parse_range(self.headers.get("Range"), size)
        if rng == "bad":
            return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        if rng is None:
            start, end, code = 0, size - 1, 200
        else:
            (start, end), code = rng, 206
        length = end - start + 1 if size else 0
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command == "HEAD" or length <= 0:
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(CHUNK, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    def _read_raw(self):
        """Read the request body exactly once (also when a handler ignores it, so keep-alive stays in sync)."""
        if getattr(self, "_raw", None) is None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True
                raise ApiError(413, "body too large")
            self._raw = self.rfile.read(n) if n > 0 else b""
        return self._raw

    def _body(self):
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError(400, f"body is not JSON: {e}") from None

    # -- dispatch ------------------------------------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        u = urlparse(self.path)
        self.q = {k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        path = u.path
        self._raw = None
        try:
            if method != "GET":
                self._read_raw()
            for m, rx, fn in ROUTES:
                if m != method:
                    continue
                mt = rx.fullmatch(path)
                if mt:
                    return fn(self, *[unquote(g) for g in mt.groups()])
            if method == "GET" and not path.startswith(("/api/", "/media/")):
                return self.static(path)
            raise ApiError(404, f"no route {method} {path}")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except ApiError as e:
            try:
                self._json({"error": e.msg, **e.extra}, e.code)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            sys.stderr.write(f"studio: {method} {path}: {tb}\n")
            try:
                self._json({"error": f"{type(e).__name__}: {e}", "trace": tb.splitlines()[-6:]}, 500)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

    # -- static -------------------------------------------------------------------------------------------
    def static(self, path):
        rel = "index.html" if path in ("", "/") else path
        p = _within(WEB, rel)
        if p and p.is_dir():
            p = p / "index.html"
        if not p or not p.is_file():
            raise ApiError(404, "not found")
        return self._file(p, cache="no-cache")

    # -- project / library -----------------------------------------------------------------------------
    def api_project(self):
        st = self.studio
        cfg = st.cfg
        from . import board
        with st.pool.get() as con:
            q = lambda s: con.execute(s).fetchone()[0]        # noqa: E731
            counts = {"clips": q("SELECT COUNT(*) FROM clips"),
                      "hours": round(q("SELECT COALESCE(SUM(duration),0) FROM clips") / 3600, 2),
                      "proxied": q("SELECT COUNT(*) FROM clips WHERE proxy IS NOT NULL"),
                      "analyzed": q("SELECT COUNT(*) FROM clips WHERE status='analyzed'"),
                      "errors": q("SELECT COUNT(*) FROM clips WHERE status='error'"),
                      "transcribed": q("SELECT COUNT(*) FROM transcripts"),
                      "with_speech": q("SELECT COUNT(*) FROM transcripts WHERE n_words>5"),
                      "tagged": q("SELECT COUNT(*) FROM tags"), "songs": q("SELECT COUNT(*) FROM songs"),
                      "matchcuts": q("SELECT COUNT(*) FROM matchcuts")}
            songs = [{"id": r["id"], "title": r["title"], "duration": r["duration"], "tempo": r["tempo"],
                      "url": f"/media/song/{r['id']}"}
                     for r in con.execute("SELECT id, title, duration, tempo FROM songs ORDER BY title LIMIT 2000")]
        renders = board.list_renders(cfg)
        for r in renders:
            r["file"] = r["rel"]
            r["url"] = f"/media/render/{quote(r['rel'])}"
            side = st.work / "renders" / (Path(r["name"]).stem + ".json")
            r["sidecar"] = f"/media/render/{quote(side.name)}" if side.exists() else None
            if r.get("qa_html"):
                r["qa_url"] = f"/media/render/{quote(r['qa_html'])}"
        refs = board.list_renders(cfg, reference=True)
        for r in refs:
            r["file"] = r["name"]
            r["url"] = f"/media/ref/{quote(r['name'])}"
        claude = find_claude()
        try:
            ag = agentmod.summary(cfg)
        except Exception as e:  # noqa: BLE001
            ag = {"name": agentmod.wanted(cfg), "logged_in": None, "available": [], "error": str(e)}
        self._json({"work": cfg.work, "name": Path(cfg.work).name, "repo": str(REPO),
                    "config": {"library_roots": cfg.library_roots, "music_roots": cfg.music_roots,
                               "output": {"width": cfg.out_width, "height": cfg.out_height, "fps": cfg.out_fps},
                               "preview_height": cfg.preview_height, "proxy_height": cfg.proxy_height},
                    "root": cfg.work, "status": counts, "plans": st.plans.list(), "renders": renders,
                    "reference": refs, "references": refs,
                    "songs": songs, "claude": {"available": bool(claude), "path": claude}, "agent": ag,
                    "governor": _gov() is not None})

    def api_clips(self):
        from . import tags
        q = self.q

        def num(k):
            return _f(q[k], k) if q.get(k) not in (None, "") else None

        def flag(k):
            v = q.get(k)
            if v in (None, ""):
                return None
            return str(v).lower() in ("1", "true", "yes", "y", "on")
        speech = flag("speech")
        with self.studio.pool.get() as con:
            res = tags.query(self.studio.cfg, con, text=q.get("q", ""), setting=q.get("setting") or None,
                             time=q.get("time") or None, people=q.get("people") or None,
                             shot=q.get("shot") or None, camera=q.get("camera") or None,
                             max_shake=num("max_shake"), min_quality=num("min_quality"), speech=speech,
                             creator=bool(flag("creator")), min_dur=num("min_dur"),
                             offset=int(num("offset") or 0), limit=int(num("limit") or 200),
                             day=q.get("day") or None)
        for r in res["rows"]:
            r["thumb"] = f"/media/thumb/{r['id']}"
            r["proxy_url"] = f"/media/proxy/{r['id']}" if r["has_proxy"] else None
            r["sheet_url"] = f"/media/sheet/{r['id']}" if r["has_sheet"] else None
        self._json(res)

    def api_clip(self, cid):
        from . import analyze, asr
        from .ingest import display_size
        st = self.studio
        row = st.clip_row(cid)
        cid = row["id"]
        with st.pool.get() as con:
            tagsd = db.get_tags(con, cid)
            metrics = [dict(r) for r in con.execute("SELECT t0, t1, shake, motion, exposure, contrast, sharpness, faces "
                                                     "FROM metrics WHERE clip_id=? ORDER BY t0", (cid,))]
            shots = [{"idx": r["idx"], "start": r["start"], "end": r["end"]}
                     for r in con.execute("SELECT idx, start, end FROM shots WHERE clip_id=? ORDER BY idx", (cid,))]
            tr = con.execute("SELECT language, n_words, speech_s, text, model FROM transcripts WHERE clip_id=?", (cid,)).fetchone()
            quality = analyze.clip_quality(con, cid)
        words = [[round(a, 3), round(b, 3), w] for a, b, w in asr.words(st.cfg, cid)]
        dw, dh = display_size(row)
        self._json({"id": cid, "rel": row["rel"], "path": row["path"], "duration": row["duration"], "fps": row["fps"],
                    "width": dw, "height": dh, "coded_width": row["width"], "coded_height": row["height"],
                    "rotation": row["rotation"], "codec": row["codec"], "pix_fmt": row["pix_fmt"],
                    "color_transfer": row["color_transfer"], "is_log": bool(row["is_log"]),
                    "has_audio": bool(row["has_audio"]), "audio_channels": row["audio_channels"],
                    "shot_at": row["shot_at"], "status": row["status"], "error": row["error"],
                    "tags": tagsd, "quality": quality, "metrics": metrics, "shots": shots,
                    "transcript": dict(tr) if tr else None, "words": words,
                    "proxy_url": f"/media/proxy/{cid}" if row["proxy"] else None,
                    "sheet_url": f"/media/sheet/{cid}" if row["sheet"] else None,
                    "thumb": f"/media/thumb/{cid}"})

    def api_clip_tags(self, cid):
        body = self._body()
        if not isinstance(body, dict):
            raise ApiError(400, "tags must be an object")
        replace = bool(body.pop("_replace", False))
        data = body.get("tags") if isinstance(body.get("tags"), dict) and len(body) == 1 else body
        row = self.studio.clip_row(cid)
        with self.studio.pool.get() as con:
            out = db.set_tags(con, row["id"], data, merge=not replace)
        self._json({"ok": True, "id": row["id"], "tags": out})

    # -- media --------------------------------------------------------------------------------------------------
    def media_proxy(self, cid):
        row = self.studio.clip_row(cid)
        if not row.get("proxy"):
            raise ApiError(404, "clip has no proxy yet (cutroom index)")
        self._file(row["proxy"])

    def media_sheet(self, cid):
        row = self.studio.clip_row(cid)
        if not row.get("sheet"):
            raise ApiError(404, "clip has no contact sheet yet (cutroom index)")
        self._file(row["sheet"])

    def media_thumb(self, cid):
        t = self.q.get("t")
        self._file(self.studio.thumb(cid, None if t in (None, "") else _f(t, "t")), cache="max-age=86400")

    def media_frame(self, cid):
        self._file(self.studio.frame(cid, _f(self.q.get("t", 0), "t"), int(_f(self.q.get("w", 1280), "w"))),
                   cache="max-age=86400")

    def media_render(self, rel):
        self._file(_within(self.studio.work / "renders", rel), cache="no-cache")

    def media_ref(self, rel):
        self._file(_within(self.studio.work / "renders" / "reference", rel))

    def media_grab(self, rel):
        self._file(_within(self.studio.work / "studio" / "grabs", rel))

    def media_marker(self, rel):
        self._file(_within(self.studio.work / "studio" / "markers", rel))

    def media_song(self, sid):
        with self.studio.pool.get() as con:
            r = con.execute("SELECT path FROM songs WHERE id=?", (sid,)).fetchone()
        if not r:
            raise ApiError(404, f"no song {sid}")
        self._file(r["path"])

    # -- plans ---------------------------------------------------------------------------------------------------
    def api_plan_get(self, slug):
        st = self.studio
        plan = st.plans.open(slug)
        self._json(st.plan_payload(slug, plan))

    def api_plan_put(self, slug):
        st = self.studio
        st.plans.check_slug(slug)
        body = self._body()
        force = bool(body.get("force")) if isinstance(body, dict) else False
        plan = body.get("plan") if isinstance(body, dict) and isinstance(body.get("plan"), dict) else body
        if not isinstance(plan, dict) or not isinstance(plan.get("sections"), list):
            raise ApiError(400, "body must be a plan (or {plan, force}) with a sections list")
        errs = st.validate(plan)
        if errs and not force:
            raise ApiError(422, "plan has problems; not saved (send force: true to save anyway)", errors=errs)
        with st.plans.lock(slug):
            st.plans.sync(slug)
            st.plans.write_draft(slug, plan)
        self._json(st.plan_payload(slug, plan))

    def api_plan_op(self, slug):
        st = self.studio
        st.plans.check_slug(slug)
        op = self._body()
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise ApiError(400, f"op must be one of {sorted(OPS)}")
        with st.plans.lock(slug):
            st.plans.open(slug)
            plan = st.plans.read_draft(slug)
            tl = st.resolve(plan)
            before = set(st.validate(plan))
            new = copy.deepcopy(plan)
            OPS[op["op"]](st, new, tl, op)
            errs = st.validate(new)
            added = [e for e in errs if e not in before]
            if added and not op.get("force"):
                raise ApiError(422, f"{op['op']} would make the plan invalid; not applied", errors=added)
            st.plans.write_draft(slug, new)
        self._json(st.plan_payload(slug, new))

    def api_plan_undo(self, slug):
        st = self.studio
        st.plans.check_slug(slug)
        self._json(st.plan_payload(slug, st.plans.step(slug, -1)))

    def api_plan_redo(self, slug):
        st = self.studio
        st.plans.check_slug(slug)
        self._json(st.plan_payload(slug, st.plans.step(slug, +1)))

    def api_plan_commit(self, slug):
        st = self.studio
        st.plans.check_slug(slug)
        errs = st.validate(st.plans.open(slug))
        body = self._body()
        if errs and not (isinstance(body, dict) and body.get("force")):
            raise ApiError(422, "draft has problems; fix them before committing (or force: true)", errors=errs)
        name, plan = st.plans.commit(slug)
        self._json({"name": name, "file": f"plans/{name}", "version": plan["version"], "slug": slug})

    def api_plan_new(self):
        body = self._body()
        title = str((body or {}).get("title") or "Untitled").strip()[:200]
        slug, plan = self.studio.plans.new(title, (body or {}).get("slug"))
        self._json(self.studio.plan_payload(slug, plan))

    def api_plan_timeline(self, slug):
        st = self.studio
        plan = st.plans.open(slug)
        self._json(st.resolve(plan))

    # -- jobs -----------------------------------------------------------------------------------------------------
    def _plan_file(self, rel):
        st = self.studio
        rel = str(rel or "").strip()
        if rel.startswith("plans/"):
            rel = rel[6:]
        p = _within(st.plans.dir, rel) if rel else None
        if p is None or not p.is_file() or p.suffix != ".json":
            raise ApiError(404, f"no plan file {rel!r} in plans/")
        return p

    def api_render(self):
        st = self.studio
        body = self._body() or {}
        p = self._plan_file(body.get("plan_file"))
        if p.name.endswith(".draft.json") and not body.get("allow_draft"):
            raise ApiError(409, "render a committed version (commit the draft first), or pass allow_draft: true")
        preview = bool(body.get("preview", True))
        cmd = _cli_cmd("render", p, *(["--preview"] if preview else []),
                       *(["--only", str(body["only"])] if body.get("only") else []))
        env = {"CUTROOM_PROGRESS": "1", "CUTROOM_MAX_JOBS": os.environ.get("CUTROOM_MAX_JOBS", "2")}

        def done(job):
            if job.status == "done" and job.result:
                out = Path(job.result)
                try:
                    rel = out.resolve().relative_to((st.work / "renders").resolve()).as_posix()
                except ValueError:
                    rel = out.name
                job.meta["file"] = rel
                job.meta["url"] = f"/media/render/{quote(rel)}"
                st.bus.publish("render_done", {"job": job.id, "file": rel, "url": job.meta["url"],
                                               "plan_file": f"plans/{p.name}", "plan": p.name, "preview": preview,
                                               "sidecar": f"/media/render/{quote(str(Path(rel).with_suffix('.json')))}"})
        job = st.jobs.submit("render", cmd, title=f"render {p.name}{' (preview)' if preview else ''}",
                             parse=_parse_render, on_done=done, meta={"plan_file": f"plans/{p.name}", "preview": preview},
                             env=env)
        self._json({"job": job.id, "status": job.status})

    def api_qa(self):
        st = self.studio
        body = self._body() or {}
        v = _within(st.work / "renders", str(body.get("video") or "").removeprefix("renders/"))
        if not v or not v.is_file():
            raise ApiError(404, f"no render {body.get('video')!r}")
        cmd = _cli_cmd("qa", v, *(["--plan", str(self._plan_file(body["plan"]))] if body.get("plan") else []))

        def done(job):
            qj = v.with_name(v.stem + ".qa.json")
            qh = v.with_name(v.stem + ".qa.html")
            rel = lambda x: x.relative_to(st.work / "renders").as_posix()     # noqa: E731
            job.result = rel(qj) if qj.exists() else None
            job.meta.update(qa_json=f"/media/render/{quote(rel(qj))}" if qj.exists() else None,
                            qa_html=f"/media/render/{quote(rel(qh))}" if qh.exists() else None)
            st.bus.publish("qa_done", {"job": job.id, "video": rel(v), **job.meta})
        job = st.jobs.submit("qa", cmd, title=f"qa {v.name}", parse=_parse_qa, on_done=done, meta={"video": v.name})
        self._json({"job": job.id, "status": job.status})

    def api_jobs(self):
        self._json({"jobs": self.studio.jobs.list()})

    def api_job(self, jid):
        self._json(self.studio.jobs.get(jid).to_dict(tail=600))

    def api_job_stop(self, jid):
        self._json(self.studio.jobs.stop(jid).to_dict())

    def api_governor(self):
        self._json(governor_status(self.studio))

    def api_overnight(self):
        st = self.studio
        body = self._body() or {}
        action = body.get("action", "status")
        if action not in ("start", "stop", "status"):
            raise ApiError(400, "action must be start, stop or status")
        ov = _overnight()
        if ov is None:
            raise ApiError(501, "overnight module not available")
        if action == "start":
            cur = overnight_state(st.cfg)
            if cur.get("running"):
                raise ApiError(409, "overnight is already running", **cur)
            # a detached process (survives a studio restart); progress comes from its state file
            pid = ov.start_background(st.cfg, until=body.get("until"), tag=bool(body.get("tag", True)),
                                      tag_limit=body.get("tag_limit"), model=body.get("model"),
                                      dry_run=bool(body.get("dry_run", False)))
            st.bus.publish("overnight", {"started": True, "pid": pid})
            return self._json({"action": "start", "started": True, "pid": pid, **overnight_state(st.cfg)})
        if action == "stop":
            ok = ov.stop(st.cfg)
            return self._json({"action": "stop", "stopped": bool(ok), **overnight_state(st.cfg)})
        self._json({"action": "status", **overnight_state(st.cfg, report=True)})

    # -- events ------------------------------------------------------------------------------------------------------
    def api_events(self):
        st = self.studio
        q = st.bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self._sse("hello", {"jobs": st.jobs.list()[:20], "governor": governor_status(st), "time": time.time()})
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not st.stopping.is_set():
                try:
                    name, data = q.get(timeout=SSE_PING_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse(name, data)
        finally:
            st.bus.unsubscribe(q)

    def _sse(self, name, data):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    # -- markers / grabs / context ------------------------------------------------------------------------------------
    def api_markers_get(self):
        ms = markers_load(self.studio.cfg)
        if self.q.get("plan"):
            ms = [m for m in ms if m.get("plan") == self.q["plan"]]
        if self.q.get("status"):
            ms = [m for m in ms if m.get("status") == self.q["status"]]
        self._json({"markers": sorted(ms, key=lambda m: (str(m.get("plan") or ""), float(m.get("t") or 0)))})

    def api_markers_post(self):
        st = self.studio
        body = self._body() or {}
        t = _f(body.get("t", 0), "t")
        plan = body.get("plan") or self.q.get("plan")
        kind = body.get("kind", "note")
        if kind not in ("note", "ref"):
            raise ApiError(400, "kind must be note or ref")
        m = {"id": uuid.uuid4().hex[:8], "plan": plan, "t": round(t, 3), "text": str(body.get("text") or "")[:4000],
             "status": "open", "kind": kind, "created": time.time(), "frame_url": None}
        for k in ("render", "clip", "seg"):
            if body.get(k) is not None:
                m[k] = body[k]
        if body.get("frame_url"):
            m["frame_url"] = body["frame_url"]
        else:
            try:
                if kind == "ref" and body.get("file"):
                    _, m["frame_url"] = st.grab("ref", t, file=body["file"], dest="markers")
                elif body.get("render"):
                    _, m["frame_url"] = st.grab("program", t, file=body["render"], dest="markers")
                elif plan:
                    _, m["frame_url"] = st.grab("program", t, plan=plan, dest="markers")
            except Exception as e:  # noqa: BLE001
                m["frame_error"] = str(getattr(e, "msg", e))[:300]
        markers_update(st.cfg, lambda data: data.append(m))
        st.bus.publish("markers", {"created": m})
        self._json(m)

    def api_marker_patch(self, mid=None):
        st = self.studio
        body = self._body() or {}
        mid = mid or body.get("id")
        if not mid:
            raise ApiError(400, "marker id required")

        def f(data):
            for m in data:
                if m.get("id") == mid:
                    for k in ("text", "status", "t", "kind", "plan", "frame_url"):
                        if k in body:
                            if k == "status" and body[k] not in ("open", "done"):
                                raise ApiError(400, "status must be open or done")
                            m[k] = round(_f(body[k], "t"), 3) if k == "t" else body[k]
                    m["updated"] = time.time()
                    return m
            raise ApiError(404, f"no marker {mid}")
        m = markers_update(st.cfg, f)
        st.bus.publish("markers", {"updated": m})
        self._json(m)

    def api_marker_delete(self, mid=None):
        st = self.studio
        mid = mid or self.q.get("id")
        if not mid:
            raise ApiError(400, "marker id required")

        def f(data):
            n = len(data)
            data[:] = [m for m in data if m.get("id") != mid]
            if len(data) == n:
                raise ApiError(404, f"no marker {mid}")
        markers_update(st.cfg, f)
        st.bus.publish("markers", {"deleted": mid})
        self._json({"ok": True, "deleted": mid})

    def api_grab(self):
        body = self._body() or {}
        src = body.get("source", "program")
        path, url = self.studio.grab(src, body.get("t", 0), clip=body.get("clip"),
                                     file=body.get("file"), plan=body.get("plan"), w=int(_f(body.get("w", 1280), "w")))
        self._json({"url": url, "path": str(path), "source": src, "t": body.get("t", 0)})

    def api_context(self):
        self._json({"ok": True, "context": self.studio.save_context(self._body() or {})})

    def api_days(self):
        from . import tags
        with self.studio.pool.get() as con:
            self._json(tags.days(con))

    def api_bins(self):
        self._json(bins_load(self.studio.cfg))

    def api_context_get(self):
        self._json(_read_json(self.studio.work / "studio" / "context.json", {}) or {})

    # -- chat ---------------------------------------------------------------------------------------------------------
    def api_chat(self):
        st = self.studio
        body = self._body() or {}
        if st.chat.running():
            raise ApiError(409, "The AI is already working on a message; stop it first")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(ev):
            self.wfile.write(f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        try:
            st.chat.run(body.get("message"), body.get("attachments") or [], body.get("context") or {}, emit)
        except ApiError as e:
            try:
                emit({"type": "error", "error": e.msg, "message": e.msg})
            except OSError:
                pass

    def api_agent(self):
        """Switch the AI agent: {name: auto|claude|codex}. Persisted to cutroom.json; a running chat turn
        finishes on the agent it started with."""
        body = self._body() or {}
        name = str(body.get("name") or "").strip().lower()
        if name not in (*agentmod.AGENTS, "auto"):
            raise ApiError(400, "name must be auto, claude or codex")
        cfg = self.studio.cfg
        cfg.agent = name
        cfg.save()
        agentmod.clear_cache()
        a = agentmod.summary(cfg)
        if os.environ.get("CUTROOM_AGENT"):
            a["note"] = "CUTROOM_AGENT is set in the studio's environment and overrides this setting"
        self._json(a)

    def api_agent_get(self):
        self._json(agentmod.summary(self.studio.cfg))

    def api_chat_stop(self):
        self._json({"stopped": self.studio.chat.stop()})

    def api_chat_history(self):
        ch = self.studio.chat
        sid = ch.session_id()
        self._json({"session_id": sid, "session": sid, "running": ch.running(), "messages": ch.history()})

    def api_chat_new(self):
        ch = self.studio.chat
        if ch.running():
            ch.stop()
        ch.new()
        self._json({"ok": True, "session_id": None})


def _r(method, pattern, fn):
    return method, re.compile(pattern), fn


ID = r"([A-Za-z0-9_.-]+)"
REST = r"(.+)"
ROUTES = [
    _r("GET", r"/api/project", Handler.api_project),
    _r("GET", r"/api/clips", Handler.api_clips),
    _r("GET", rf"/api/clip/{ID}", Handler.api_clip),
    _r("PUT", rf"/api/clip/{ID}/tags", Handler.api_clip_tags),
    _r("GET", rf"/media/proxy/{ID}", Handler.media_proxy),
    _r("GET", rf"/media/sheet/{ID}", Handler.media_sheet),
    _r("GET", rf"/media/thumb/{ID}", Handler.media_thumb),
    _r("GET", rf"/media/frame/{ID}", Handler.media_frame),
    _r("GET", rf"/media/render/{REST}", Handler.media_render),
    _r("GET", rf"/media/ref/{REST}", Handler.media_ref),
    _r("GET", rf"/media/song/{ID}", Handler.media_song),
    _r("GET", rf"/media/grab/{REST}", Handler.media_grab),
    _r("GET", rf"/media/marker/{REST}", Handler.media_marker),
    _r("POST", r"/api/plan/new", Handler.api_plan_new),
    _r("GET", rf"/api/plan/{ID}", Handler.api_plan_get),
    _r("PUT", rf"/api/plan/{ID}", Handler.api_plan_put),
    _r("GET", rf"/api/plan/{ID}/timeline", Handler.api_plan_timeline),
    _r("POST", rf"/api/plan/{ID}/op", Handler.api_plan_op),
    _r("POST", rf"/api/plan/{ID}/undo", Handler.api_plan_undo),
    _r("POST", rf"/api/plan/{ID}/redo", Handler.api_plan_redo),
    _r("POST", rf"/api/plan/{ID}/commit", Handler.api_plan_commit),
    _r("POST", r"/api/render", Handler.api_render),
    _r("POST", r"/api/qa", Handler.api_qa),
    _r("GET", r"/api/jobs", Handler.api_jobs),
    _r("GET", rf"/api/jobs/{ID}", Handler.api_job),
    _r("POST", rf"/api/jobs/{ID}/stop", Handler.api_job_stop),
    _r("GET", r"/api/events", Handler.api_events),
    _r("GET", r"/api/markers", Handler.api_markers_get),
    _r("POST", r"/api/markers", Handler.api_markers_post),
    _r("PATCH", r"/api/markers", Handler.api_marker_patch),
    _r("PATCH", rf"/api/markers/{ID}", Handler.api_marker_patch),
    _r("DELETE", r"/api/markers", Handler.api_marker_delete),
    _r("DELETE", rf"/api/markers/{ID}", Handler.api_marker_delete),
    _r("POST", r"/api/grab", Handler.api_grab),
    _r("POST", r"/api/context", Handler.api_context),
    _r("GET", r"/api/context", Handler.api_context_get),
    _r("GET", r"/api/bins", Handler.api_bins),
    _r("GET", r"/api/days", Handler.api_days),
    _r("GET", r"/api/agent", Handler.api_agent_get),
    _r("POST", r"/api/agent", Handler.api_agent),
    _r("POST", r"/api/chat", Handler.api_chat),
    _r("POST", r"/api/chat/stop", Handler.api_chat_stop),
    _r("GET", r"/api/chat/history", Handler.api_chat_history),
    _r("POST", r"/api/chat/new", Handler.api_chat_new),
    _r("GET", r"/api/governor", Handler.api_governor),
    _r("POST", r"/api/overnight", Handler.api_overnight),
]


class StudioServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on handler threads (extra connections get a 503)."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, max_threads=MAX_THREADS):
        self._slots = threading.BoundedSemaphore(max_threads)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Content-Length: 27\r\nRetry-After: 1\r\nConnection: close\r\n\r\n"
                                b'{"error": "server is busy"}')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:  # noqa: BLE001
            self._slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def make_server(cfg, port=8600, host="127.0.0.1"):
    """(server, studio). port 0 = any free port. Call studio.close() + server.server_close() to stop."""
    studio = Studio(cfg)
    handler = type("StudioHandler", (Handler,), {"studio": studio})
    srv = StudioServer((host, port), handler)
    srv.studio = studio
    return srv, studio


def serve(cfg, port=8600, host="127.0.0.1", open_browser=False):
    srv, studio = make_server(cfg, port, host)
    h, p = srv.server_address[:2]
    url = f"http://{'127.0.0.1' if h in ('0.0.0.0', '') else h}:{p}/"
    print(f"studio: {url}   (project {cfg.work})  ctrl-c to stop", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("  ! listening beyond this machine: anyone who can reach it can edit plans and drive Claude. "
              "Use it only on a network you trust.", flush=True)
    try:
        a = agentmod.summary(cfg)
        li = {True: "logged in", False: "NOT logged in", None: "login unknown"}[a["logged_in"]]
        print(f"  AI agent: {a['label']} ({a['path'] or 'not installed: chat panel will show install steps'}, {li}); "
              f"installed: {', '.join(a['available']) or 'none'}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  AI agent: could not detect ({e})", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    def bye(*_):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, bye)
    except ValueError:
        pass
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        studio.close()
        srv.server_close()
