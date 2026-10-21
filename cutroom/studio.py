"""Cutroom Studio backend: the HTTP/SSE server behind the browser editor (docs/STUDIO.md).

    cutroom studio [--port 8600] [--host 127.0.0.1] [--open]

stdlib only (ThreadingHTTPServer). What lives where:
  <work>/plans/<slug>.draft.json        the open working draft (hand edits + Claude edit this file)
  <work>/plans/<slug>_v<N>.json         committed versions (render always renders one of these)
  <work>/plans/.history/<slug>/         undo/redo snapshots + index.json
  <work>/studio/context.json            pointing state posted by the frontend (+ draft_path)
  <work>/studio/markers.json            review comments (a.k.a. markers) [{id, plan, t, t_end?, text, frame_url,
                                        frame_raw_url?, drawing?, status, kind, author, thread[]}]
  <work>/studio/grabs/  markers/        frame grabs (chat attachments, annotated comment frames) and marker frames
  <work>/frames/looks/                  look previews (one frame in each look, cached)
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
import hashlib
import json
import math
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


_ABS_PATH = re.compile(r"(?<![\w.~])/(?:[^\s\"'`,;:()\[\]{}<>|]+/)+[^\s\"'`,;:()\[\]{}<>|]*")


def short_paths(text, work=None, keep=2):
    """Absolute paths inside a line of text -> short ones for the UI: relative to the project work folder,
    else ~/..., else the last `keep` parts after an ellipsis. The full text stays available as a tooltip."""
    if not text:
        return text
    roots = []
    if work:
        for w in {str(Path(work)), str(Path(work).resolve())}:
            roots.append(w.rstrip("/") + "/")
    home = str(Path.home()).rstrip("/") + "/"

    def one(m):
        p = m.group(0)
        for r in roots:
            if p.startswith(r):
                return p[len(r):] or "."
            if p == r.rstrip("/"):
                return "."
        parts = [x for x in p.split("/") if x]
        if p.startswith(home):
            rest = p[len(home):]
            return "~/" + rest if rest.count("/") < keep else "~/…/" + "/".join(parts[-keep:])
        return p if len(parts) <= keep else "…/" + "/".join(parts[-keep:])
    return _ABS_PATH.sub(one, str(text))


def role_rows_for_ui(roles, work):
    """Team rows: short relative paths in `last` / `error` (full text kept in last_full / error_full), and a
    finished run's StructuredOutput call reads as 'returned its result' instead of a JSON dump."""
    out = []
    for r in roles or []:
        r = dict(r)
        for k in ("last", "error"):
            v = r.get(k)
            if not v:
                continue
            short = short_paths(v, work)
            if k == "last" and str(v).startswith("StructuredOutput"):
                short = "returned its result"
            if short != v:
                r[k + "_full"], r[k] = v, short
        out.append(r)
    return out


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


COMMENT_STATUSES = ("open", "working", "done", "needs_you")
DRAW_TOOLS = ("arrow", "box", "circle", "pen")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def marker_set_status(cfg, mid, status="done", reply=None, author="ai"):
    """Set a comment's status (open / working / done / needs_you); `reply` is appended to its thread."""
    if status not in COMMENT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(COMMENT_STATUSES)}")

    def f(data):
        for m in data:
            if m.get("id") == mid:
                m["status"] = status
                m["updated"] = time.time()
                if reply and str(reply).strip():
                    m.setdefault("thread", []).append({"author": author, "text": str(reply).strip()[:4000],
                                                       "ts": time.time(), "status": status})
                return m
        raise KeyError(f"no comment {mid!r}")
    return markers_update(cfg, f)


def clean_drawing(drawing):
    """Validate the strokes drawn on a comment's frame: [{tool, points: [[x, y], ...] (0-1), color}]."""
    if drawing in (None, ""):
        return []
    if not isinstance(drawing, list) or len(drawing) > 60:
        raise ApiError(400, "drawing must be a list of at most 60 strokes")
    out = []
    for st in drawing:
        if not isinstance(st, dict) or st.get("tool") not in DRAW_TOOLS:
            raise ApiError(400, f"each stroke needs tool: {'/'.join(DRAW_TOOLS)}")
        pts = st.get("points")
        if not isinstance(pts, list) or not pts or len(pts) > 4000:
            raise ApiError(400, "each stroke needs 1-4000 points")
        try:
            pts = [[round(min(1.0, max(0.0, float(x))), 4), round(min(1.0, max(0.0, float(y))), 4)] for x, y in pts]
        except (TypeError, ValueError):
            raise ApiError(400, "points must be [x, y] pairs from 0 to 1") from None
        if st["tool"] != "pen" and len(pts) < 2:
            raise ApiError(400, f"a {st['tool']} needs a start and an end point")
        col = str(st.get("color") or "#ff3b30")
        out.append({"tool": st["tool"], "points": pts if st["tool"] == "pen" else [pts[0], pts[-1]],
                    "color": col if HEX_RE.match(col) else "#ff3b30"})
    return out


def annotate_frame(src, drawing, out):
    """Composite the comment's drawing onto its frame (what the AI looks at). Returns out."""
    from PIL import Image, ImageDraw
    im = Image.open(src).convert("RGB")
    W, H = im.size
    lw = max(3, int(round(W * 0.0055)))
    d = ImageDraw.Draw(im)

    def rgb(c):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    for st in drawing:
        P = [(x * W, y * H) for x, y in st["points"]]
        col = rgb(st["color"])
        for w, c in ((lw + 4, (0, 0, 0)), (lw, col)):              # dark halo, then the colour
            if st["tool"] == "pen":
                if len(P) == 1:
                    r = w / 2
                    d.ellipse([P[0][0] - r, P[0][1] - r, P[0][0] + r, P[0][1] + r], fill=c)
                else:
                    d.line(P, fill=c, width=w, joint="curve")
            elif st["tool"] in ("box", "circle"):
                (x0, y0), (x1, y1) = P[0], P[-1]
                box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
                (d.rectangle if st["tool"] == "box" else d.ellipse)(box, outline=c, width=w)
            else:                                                      # arrow: shaft + filled head at the end
                (x0, y0), (x1, y1) = P[0], P[-1]
                ang = math.atan2(y1 - y0, x1 - x0)
                head = max(lw * 4.5, 14)
                bx, by = x1 - math.cos(ang) * head * 0.8, y1 - math.sin(ang) * head * 0.8
                d.line([(x0, y0), (bx, by)], fill=c, width=w)
                pts = [(x1, y1), (x1 - head * math.cos(ang - 0.45), y1 - head * math.sin(ang - 0.45)),
                       (x1 - head * math.cos(ang + 0.45), y1 - head * math.sin(ang + 0.45))]
                if w > lw:
                    cx, cy = sum(p[0] for p in pts) / 3, sum(p[1] for p in pts) / 3
                    pts = [(cx + (px - cx) * 1.25, cy + (py - cy) * 1.25) for px, py in pts]
                d.polygon(pts, fill=c)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(out).with_name(f".{Path(out).stem}.{threading.get_ident()}.tmp.jpg")
    im.save(tmp, "JPEG", quality=90)
    os.replace(tmp, out)
    return Path(out)


def drawing_words(drawing):
    """'a circle and an arrow' for the prompt."""
    names = {"arrow": "an arrow", "box": "a box", "circle": "a circle", "pen": "a pen mark"}
    ws = [names[s["tool"]] for s in drawing or []]
    if not ws:
        return ""
    return ws[0] if len(ws) == 1 else ", ".join(ws[:-1]) + " and " + ws[-1]


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
             "- Review comments are in `studio/markers.json`: one change per comment, then\n"
             "  `cutroom comments done <id> --reply \"<what changed, with a [[t:<seconds>]] chip>\"`, or\n"
             "  `cutroom comments needs <id> --reply \"Needs you: <question>\"` when it is ambiguous.\n"
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
             "planning, render-and-qa, review-loop, reference-matching).",
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
        self._vcanon = {}                   # version file -> ((mtime_ns, size), canonical json without `version`)

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
        saved_by = _read_json(self.hist_dir(slug) / "versions.json", {}) if slug and SLUG_RE.match(slug) else {}
        saved_by = saved_by if isinstance(saved_by, dict) else {}
        for e in os.scandir(self.dir):
            c = self.classify(e.name)
            if not c or c[0] != slug or c[1] == "draft" or not e.is_file():
                continue
            v = c[2]
            if v is None:
                d = _read_json(e.path, {})
                v = d.get("version") if isinstance(d, dict) else None
            out.append({"file": e.name, "path": f"plans/{e.name}", "version": v, "kind": c[1],
                        "mtime": e.stat().st_mtime, "by": (saved_by.get(e.name) or {}).get("by")})
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

    def draft_sha(self, plan):
        return hashlib.sha1(_canon(plan).encode()).hexdigest()[:16] if isinstance(plan, dict) else None

    def commit(self, slug, expect=None):
        """Save the draft as the next version. `expect` (the draft_sha the client last saw) refuses a save
        whose draft changed on disk since (another tab, Claude, the factory's Open in draft): 409, never a
        stale or unseen copy. The number comes from the factory's reservation table, so a version a running
        order has reserved is never taken by a hand save (and the reverse)."""
        with self.lock(slug):
            self.sync(slug)
            plan = self.read_draft(slug)
            if expect and self.draft_sha(plan) != expect:
                raise ApiError(409, "the draft changed since this page last saw it; reloaded, press Save version again",
                               draft_sha=self.draft_sha(plan))
            n = 0
            for v in self.version_files(slug):
                if isinstance(v["version"], (int, float)):
                    n = max(n, int(v["version"]))
            n += 1
            try:
                from .factory import bus as fbus
                n = max(n, fbus.reserve_version(self.s.cfg, slug))
            except Exception:  # noqa: BLE001 - no factory folder possible: plain numbering
                pass
            plan["version"] = n
            name = f"{slug}_v{n}.json"
            out = self.dir / name
            if out.exists():
                raise ApiError(409, f"{name} already exists")
            _atomic_write(out, _dump_plan(plan))
            st = out.stat()
            self.own[str(out)] = (st.st_mtime_ns, st.st_size)
            self.write_draft(slug, plan)
            self.note_version(slug, name, "you")                                # "Save version" = the creator
            return name, plan

    def note_version(self, slug, name, by):
        """Remember who saved a version file (versions.json next to the undo history); first writer wins."""
        if not SLUG_RE.match(slug or ""):
            return
        with self.lock(slug):
            log = _read_json(self.hist_dir(slug) / "versions.json", {})
            log = log if isinstance(log, dict) else {}
            if name not in log:
                log[name] = {"by": by, "t": time.time()}
                _atomic_write(self.hist_dir(slug) / "versions.json", json.dumps(log))

    def saved_state(self, slug, plan):
        """{latest, version, dirty, newest, newest_version, newer}: which saved version the draft is based on and
        whether it differs from THAT version (ignoring `version`). The base is the version the draft equals, else
        the one its `version` field names, else the newest. A factory release (plans/<slug>_vN.json written next
        to the draft) adds a newer version; it never makes the draft look 'unsaved since vN'."""
        vs = [v for v in self.version_files(slug) if v["kind"] == "version"]
        if not vs:
            return {"latest": None, "version": None, "dirty": True, "newest": None, "newest_version": None, "newer": []}
        bare = lambda x: {k: v for k, v in x.items() if k != "version"} if isinstance(x, dict) else x   # noqa: E731
        me = _canon(bare(plan))
        own_v = plan.get("version") if isinstance(plan, dict) else None
        by_v = {v["version"]: v for v in vs if isinstance(v["version"], (int, float))}
        base, dirty = None, True
        order = ([by_v[own_v]] if own_v in by_v else []) + [v for v in reversed(vs) if v["version"] != own_v]
        def canon_of(v):
            p = self.dir / v["file"]
            try:
                st = p.stat()
            except OSError:
                return None
            key = (st.st_mtime_ns, st.st_size)
            hit = self._vcanon.get(v["file"])
            if not hit or hit[0] != key:
                hit = (key, _canon(bare(_read_json(p, {}))))
                self._vcanon[v["file"]] = hit
            return hit[1]
        for v in order:                                       # the version the draft IS (own number first)
            if canon_of(v) == me:
                base, dirty = v, False
                break
        if base is None:
            base = by_v.get(own_v) or vs[-1]
        newest = vs[-1]
        newer = [{"file": v["file"], "version": v["version"], "by": v.get("by")} for v in vs
                 if isinstance(v["version"], (int, float)) and isinstance(base["version"], (int, float))
                 and v["version"] > base["version"]]
        return {"latest": base["file"], "version": base["version"], "dirty": dirty, "newest": newest["file"],
                "newest_version": newest["version"], "newer": newer}

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

    def squash_since(self, slug, pos0):
        """Record the draft as it is now and fold every history step after `pos0` into one (so one review
        comment's edits are one undo step). Returns True when the draft changed."""
        with self.lock(slug):
            self.sync(slug)
            idx = self._hidx(slug)
            if idx["pos"] <= pos0:
                return False
            hd = self.hist_dir(slug)
            keep = idx["ids"][idx["pos"]]
            drop = [i for i in idx["ids"][pos0 + 1:idx["pos"]]]
            for i in drop:
                (hd / f"{i}.json").unlink(missing_ok=True)
            idx["ids"] = idx["ids"][:pos0 + 1] + [keep] + idx["ids"][idx["pos"] + 1:]
            idx["pos"] = pos0 + 1
            self._hsave(slug, idx)
            base = _read_json(hd / f"{idx['ids'][pos0]}.json") if pos0 >= 0 else None
            cur = _read_json(hd / f"{keep}.json")
            return base is None or _canon(base) != _canon(cur)

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
    elif typ in ("still", "title", "black", "image"):
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
    elif t in ("still", "title", "black", "image"):
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


def op_multicam_angle(st, plan, tl, op):
    """Switch a multicam segment to another angle (and optionally another sound source): clip, in and out follow
    the group's measured offsets, so the cut stays on the same moment."""
    from . import sync as MC
    si, gi = _seg_ref(op)
    seg = _get_seg(plan, si, gi)
    if not seg.get("multicam"):
        raise ApiError(400, "this segment is not a multicam segment")
    angle = op.get("angle") or (seg["multicam"] or {}).get("angle")
    try:
        with st.pool.get() as con:
            new = MC.switch_angle(con, seg, angle, audio=op.get("audio"))
    except (KeyError, ValueError) as e:
        raise ApiError(400, str(e)) from None
    seg.clear()
    seg.update(new)


OPS = {"move": op_move, "trim": op_trim, "split": op_split, "delete": op_delete, "insert": op_insert,
       "overwrite": op_overwrite, "freeze": op_freeze, "set": op_set, "set_style": op_set_style,
       "add_section": op_add_section, "set_music": op_set_music, "add_cutaway": op_add_cutaway,
       "add_overlay": op_add_overlay, "multicam_angle": op_multicam_angle}


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


def hashlib_sha(obj, n=10):
    import hashlib
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:n]


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
        if ctx.get("comment"):                       # a review-comment turn is about that one comment only
            ms = []
        if ms:
            lines.append(f"- Open review comments ({len(ms)}; full list in `studio/markers.json`, mark handled ones with "
                         f"`cutroom comments done <id> --reply \"...\"`):")
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

    def run(self, message, attachments, context, emit, display=None):
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
            g = _gov_for(self.s.cfg)
            slot = (g.try_slot("agent", label="studio chat") if g is not None and hasattr(g, "try_slot")
                    else contextlib.nullcontext(True))
            with slot as free:
                if not free:
                    busy = g.busy_slots("agent") if hasattr(g, "busy_slots") else []
                    who = (busy[0].get("label") if busy else None) or "another AI run"
                    send({"type": "error", "error": f"The AI is busy ({who}). Your message can go as soon as it "
                          "finishes, or press Send to factory to queue the work.", "busy": True})
                    return
                self._turn(message, attachments, context, send, ag, display)
        finally:
            with self.lock:
                self.busy, self.proc = False, None

    def _turn(self, message, attachments, context, send, ag, display=None):
        if isinstance(context, dict) and context:
            self.s.save_context(context)
        prompt, bad = self.compose(message, context, attachments)
        images, _ = self.attachment_paths(attachments)
        if bad:
            send({"type": "warning", "text": f"attachments not found: {bad}"})
        self._hist({"role": "user", "text": str(display or message), "attachments": attachments or [], "context": context or {},
                    "agent": ag.name, **({"comment": context.get("comment")} if isinstance(context, dict) and context.get("comment") else {})})
        job = self.s.jobs.add(Job("chat", [ag.path], title=_short(display or message, 60), meta={"agent": ag.name}))
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


# ================================================================================================ review comments
class CommentRunner:
    """"Send open comments to AI": one chat turn per comment, in timeline order, like a real review.

    Each turn gets that comment's text, timecode (and range), the segment under it and its annotated
    frame; the AI makes the one change and marks the comment done with a short reply
    (`cutroom comments done <id> --reply ...`), or says "Needs you: <question>". Whatever the AI did to
    the draft during one comment is squashed into ONE undo step, so a single comment's change can be
    undone. Stop finishes the current comment and leaves the rest open. Progress goes out on SSE
    `comments`."""

    def __init__(self, studio):
        self.s = studio
        self.lock = threading.Lock()
        self.thread = None
        self.state = self._idle()

    @staticmethod
    def _idle():
        return {"running": False, "plan": None, "queue": [], "current": None, "index": 0, "total": 0,
                "done": [], "results": [], "stop_requested": False, "started": None, "ended": None, "error": None}

    def status(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def _emit(self, **ev):
        st = self.status()
        self.s.bus.publish("comments", {**ev, "run": st})

    def start(self, plan, ids=None, all_open=False):
        if not plan or not SLUG_RE.match(str(plan)):
            raise ApiError(400, "plan is required")
        ms = [m for m in markers_load(self.s.cfg) if m.get("plan") == plan or (not m.get("plan"))]
        if all_open or not ids:
            pick = [m for m in ms if m.get("status", "open") == "open" and m.get("kind", "note") == "note"]
        else:
            want = list(dict.fromkeys(str(i) for i in ids))
            byid = {m["id"]: m for m in ms}
            missing = [i for i in want if i not in byid]
            if missing:
                raise ApiError(404, f"no comment {missing[0]}")
            pick = [byid[i] for i in want]
        pick.sort(key=lambda m: float(m.get("t") or 0))
        pick = [m for m in pick if str(m.get("text") or "").strip() or m.get("drawing")]
        if not pick:
            raise ApiError(409, "no open comments with a note or a drawing to send")
        with self.lock:
            if self.state["running"]:
                raise ApiError(409, "the AI is already working through comments; stop it first")
            if self.s.chat.running():
                raise ApiError(409, "the AI is busy with a chat message; wait for it or stop it first")
            self.state = {**self._idle(), "running": True, "plan": plan, "queue": [m["id"] for m in pick],
                          "total": len(pick), "started": time.time()}
        for m in pick:                                        # queued comments show as queued at once
            try:
                marker_set_status(self.s.cfg, m["id"], "open")
            except KeyError:
                pass
        self.thread = threading.Thread(target=self._loop, daemon=True, name="comment-runner")
        self.thread.start()
        self._emit(type="start")
        return self.status()

    def stop(self):
        with self.lock:
            if not self.state["running"]:
                return False
            self.state["stop_requested"] = True
        self._emit(type="stopping")
        return True

    def _loop(self):
        try:
            k = 0
            while True:
                with self.lock:
                    if self.state["stop_requested"] or not self.state["queue"]:
                        break
                    mid = self.state["queue"].pop(0)
                    k += 1
                    self.state.update(current=mid, index=k)
                self._one(mid, k)
                with self.lock:
                    self.state["done"].append(mid)
                    self.state["current"] = None
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"studio: comment runner: {traceback.format_exc()}\n")
        finally:
            with self.lock:
                self.state.update(running=False, current=None, ended=time.time())
                left = list(self.state["queue"])
            self._emit(type="end", left=left)

    def prompt(self, m, k, n):
        t = float(m.get("t") or 0)
        rng = ""
        if m.get("t_end") is not None and float(m["t_end"]) > t + 0.04:
            rng = f" to {float(m['t_end']):.2f}s ({_mmss(float(m['t_end']))})"
        lines = [f"Review comment {k} of {n} (comment id `{m['id']}`), at {t:.2f}s ({_mmss(t)}){rng} on the timeline:",
                 f"\"{str(m.get('text') or '').strip() or '(no words: look at what they drew on the frame)'}\""]
        if m.get("drawing"):
            lines.append(f"The creator drew {drawing_words(m['drawing'])} on the frame; the attached frame shows exactly "
                         "what they marked. Open it before you change anything.")
        if rng:
            lines.append("The comment covers that whole range (\"from here to here\").")
        try:
            tl = self.s.resolve(self.s.plans.read_draft(m["plan"])) if m.get("plan") else None
        except Exception:  # noqa: BLE001
            tl = None
        if tl and rng:
            from .render import locate
            a, b = locate(tl, t), locate(tl, float(m["t_end"]) - 1e-3)
            if a and b:
                lines.append(f"Segments in the range: [{a['sec']}, {a['seg']}] to [{b['sec']}, {b['seg']}].")
        for th in (m.get("thread") or [])[-6:]:
            lines.append(f"Earlier in this thread, {'you' if th.get('author') == 'ai' else 'the creator'} said: "
                         f"\"{_short(th.get('text', ''), 300)}\"")
        lines += ["",
                  "Make exactly this one change in the draft, nothing else (one change per comment). Then run "
                  f"`cutroom comments done {m['id']} --reply \"<one or two plain sentences: what you changed, with a "
                  "[[t:<seconds>]] chip where it is>\"`.",
                  "If the note is ambiguous or you cannot do it, do not guess: run "
                  f"`cutroom comments needs {m['id']} --reply \"Needs you: <one short question>\"` and change nothing."]
        return "\n".join(lines)

    def _one(self, mid, k):
        st = self.s
        m = next((x for x in markers_load(st.cfg) if x.get("id") == mid), None)
        if m is None:
            return
        n = self.state["total"]
        slug = m.get("plan") or self.state["plan"]
        marker_set_status(st.cfg, mid, "working")
        self._emit(type="working", id=mid, index=k, total=n)
        pos0 = None
        if slug and st.plans.draft_path(slug).exists():
            with st.plans.lock(slug):
                st.plans.sync(slug)
                pos0 = st.plans.history_info(slug)["pos"]
        acc = {"text": [], "final": None}

        def emit(ev):
            if ev.get("type") == "text":
                acc["text"].append(ev.get("text") or "")
            elif ev.get("type") in ("done", "error"):
                acc["final"] = ev
            self.s.bus.publish("comments", {"type": "chat", "id": mid, "event": ev})
        ctx = {"plan": slug, "playhead": float(m.get("t") or 0), "selection": [], "comment": mid}
        atts = [m["frame_url"]] if m.get("frame_url") else []
        err = None
        try:
            label = f"Comment {k} of {n} at {_mmss(float(m.get('t') or 0))}: {str(m.get('text') or '').strip() or '(drawing)'}"
            st.chat.run(self.prompt(m, k, n), atts, ctx, emit, display=label)
        except ApiError as e:
            err = e.msg
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        fin = acc["final"] or {}
        if fin.get("type") == "error":
            err = err or fin.get("error") or "the AI turn failed"
        squashed = False
        if slug and pos0 is not None:
            try:
                squashed = st.plans.squash_since(slug, pos0)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"studio: squash after comment {mid}: {e}\n")
        text = "".join(acc["text"]).strip()
        cur = next((x for x in markers_load(st.cfg) if x.get("id") == mid), None) or {}
        stopped = bool(fin.get("stopped"))
        if cur.get("status") in ("done", "needs_you"):          # the AI marked it itself (CLI), reply attached
            status = cur["status"]
        elif stopped:
            status = "open"
            marker_set_status(st.cfg, mid, "open")
        elif err:
            status = "needs_you"
            marker_set_status(st.cfg, mid, status, reply=f"Couldn't finish this one: {_short(err, 300)}")
        else:
            needs = bool(re.search(r"\bneeds you\b", text, re.I)) or (text.rstrip().endswith("?") and not squashed)
            status = "needs_you" if needs else "done"
            marker_set_status(st.cfg, mid, status, reply=reply_of(text) or ("Done." if status == "done" else None))
        with self.lock:
            self.state["results"].append({"id": mid, "status": status, "changed": squashed, "error": err})
        st.bus.publish("markers", {"updated": next((x for x in markers_load(st.cfg) if x.get("id") == mid), None)})
        self._emit(type="result", id=mid, status=status, changed=squashed, index=k, total=n)


def reply_of(text, limit=600):
    """The AI's short reply for a comment thread: its last paragraph(s), at most `limit` characters."""
    text = re.sub(r"```.*?(```|$)", "", str(text or ""), flags=re.S).strip()
    if not text:
        return ""
    k = text.lower().rfind("needs you:")
    if k >= 0:                                    # the question is the reply
        text = text[k:]
        text = "Needs you:" + text[len("needs you:"):]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = paras[-1] if paras else text
    if len(out) < 40 and len(paras) > 1 and k < 0:
        out = paras[-2] + "\n\n" + out
    return out if len(out) <= limit else "…" + out[-(limit - 1):]


# ================================================================================================ the app
class Studio:
    def __init__(self, cfg):
        self.cfg = cfg
        self.work = Path(cfg.work)
        for d in ("studio", "studio/grabs", "studio/markers", "frames/thumbs", "frames/studio", "frames/looks",
                  "renders/reference"):
            (self.work / d).mkdir(parents=True, exist_ok=True)
        self.stopping = threading.Event()
        self.bus = Bus()
        self.pool = ConPool(cfg.db_path)
        self.plans = PlanStore(self)
        self.jobs = Jobs(self)
        self.chat = ChatBridge(self)
        self.comments = CommentRunner(self)
        self.link_base = None
        self._factory_init()
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
                         threading.Thread(target=self._gov_loop, daemon=True, name="studio-governor"),
                         threading.Thread(target=self._factory_loop, daemon=True, name="studio-factory")]
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
        try:
            from .factory import runner as FR
            if self._factory_event in FR.LISTENERS:
                FR.LISTENERS.remove(self._factory_event)
            if self.factory is not None:
                self.factory.shutdown(wait=5)
        except Exception as e:  # noqa: BLE001
            print(f"studio: factory shutdown: {e}", file=sys.stderr)
        self.comments.stop()
        self.chat.stop()
        self.jobs.stop_all()
        self.pool.close()

    # -- factory (cutroom/factory, docs/FACTORY.md) ------------------------------------------------
    def _factory_init(self):
        """A FactoryRunner thread for this project (one per project: flock). Its renders go through the Jobs
        heavy lane so they show in the Jobs tab and never overlap a hand render."""
        self.factory, self.factory_note = None, None
        try:
            from .factory import fconfig
            from .factory import runner as FR
        except Exception as e:  # noqa: BLE001
            self.factory_note = f"factory unavailable: {e}"
            return
        FR.LISTENERS.append(self._factory_event)
        self.factory = FR.FactoryRunner(self.cfg, render_fn=self._factory_render)
        fc = fconfig.load(self.cfg)
        if not (fc.get("enabled", True) and fc.get("autostart", True)) or os.environ.get("CUTROOM_FACTORY_AUTOSTART") == "0":
            self.factory_note = "the factory runner is not started (factory.autostart false)"
        elif not self.factory.start():
            self.factory_note = "another factory runner holds this project (`cutroom factory run`?)"

    def _factory_event(self, kind, data):
        if kind == "markers":
            self.bus.publish("markers", {"markers": markers_load(self.cfg)})
        else:
            self.bus.publish(kind, data)

    def factory_light(self):
        from .factory import runner as FR
        lt = FR.light(self.cfg)
        lt["owner"] = bool(self.factory and self.factory.owner)
        lt["note"] = self.factory_note
        return lt

    def _factory_render(self, ctx, plan_path, out, preview, log, on_line):
        """Factory render as a studio job (heavy lane) -> (rc, tail, why)."""
        cmd = _cli_cmd("render", plan_path, "--out", out, *(["--preview"] if preview else []))
        env = {"CUTROOM_PROGRESS": "1", "CUTROOM_MAX_JOBS": os.environ.get("CUTROOM_MAX_JOBS", "2")}

        def parse(job, line):
            _parse_render(job, line)
            try:
                on_line(line)
            except Exception:  # noqa: BLE001
                pass
        job = self.jobs.submit("render", cmd, title=f"factory: {Path(out).name}", parse=parse, lane="heavy",
                               meta={"factory": ctx.oid, "plan_file": str(plan_path), "preview": preview}, env=env)
        wall = ctx.wall("render")
        t0, why = time.time(), None
        pidf = Path(log).with_suffix(".pid")
        while job.status in ("queued", "running"):
            if job.proc is not None and not pidf.exists():
                try:
                    pidf.write_text(str(job.proc.pid))
                except OSError:
                    pass
            if ctx.stop_check():
                why = "stopped"
                self.jobs.stop(job.id)
            elif time.time() - t0 > wall:
                why = "timeout"
                self.jobs.stop(job.id)
            time.sleep(0.5)
        pidf.unlink(missing_ok=True)
        try:
            Path(log).write_text("\n".join(job.log) + "\n")
        except OSError:
            pass
        if job.status == "done" and job.result:
            rel = Path(job.result)
            try:
                f = rel.resolve().relative_to((self.work / "renders").resolve()).as_posix()
                self.bus.publish("render_done", {"job": job.id, "file": f, "url": f"/media/render/{quote(f)}",
                                                 "plan_file": str(plan_path), "plan": Path(plan_path).name, "preview": preview,
                                                 "factory": ctx.oid})
            except ValueError:
                pass
        return (0 if job.status == "done" else (job.rc if job.rc is not None else 1)), list(job.log)[-40:], why

    def _factory_loop(self):
        last = None
        while not self.stopping.wait(3.0):
            if not self.bus.n:
                continue
            try:
                lt = self.factory_light()
                sig = json.dumps({k: lt.get(k) for k in ("light", "text", "needs", "blocked", "queued", "runner_alive")},
                                 sort_keys=True, default=str)
                if sig != last or lt["light"] == "working":
                    last = sig
                    self.bus.publish("factory_state", lt)
            except Exception:  # noqa: BLE001
                pass

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
        out = {"slug": slug, "draft": plan, "draft_file": f"plans/{slug}.draft.json", "draft_sha": self.plans.draft_sha(plan),
               "draft_path": str(self.plans.draft_path(slug)),
               "version_files": self.plans.version_files(slug), "history": hist,
               "can_undo": hist["can_undo"], "can_redo": hist["can_redo"]}
        try:
            out["saved"] = self.plans.saved_state(slug, plan)
        except Exception:  # noqa: BLE001
            out["saved"] = None
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

    def annotate_comment(self, m):
        """m["frame_url"] = its raw frame with the drawing composited (studio/grabs/), for the AI to see."""
        raw = self.media_url_to_path(m.get("frame_raw_url") or "")
        if not raw or not Path(raw).is_file():
            return m
        if not m.get("drawing"):
            m["frame_url"] = m["frame_raw_url"]
            return m
        name = f"comment_{m['id']}_{hashlib_sha(m['drawing'])}.jpg"
        out = self.work / "studio" / "grabs" / name
        if not out.exists():
            annotate_frame(raw, m["drawing"], out)
        m["frame_url"] = f"/media/grab/{quote(name)}"
        return m

    # -- references ----------------------------------------------------------------------------------
    def ref_row(self, m):
        """The list row the Reference panel shows for one reference (reference.py meta + URLs)."""
        rid = m["id"]
        d = self.work / "refs" / rid
        q = quote(rid)
        origin = str(m.get("origin") or "")
        ref_folder = str((self.work / "renders" / "reference").resolve())
        job = next((j for j in self.jobs.jobs.values() if j.kind == "ref" and j.status in ("queued", "running")
                    and j.meta.get("ref") == rid), None)
        return {"id": rid, "title": m.get("title") or rid, "status": m.get("status"), "stage": m.get("stage"),
                "progress": m.get("progress"), "error": m.get("error"), "duration": m.get("duration"),
                "fps": m.get("fps"), "width": m.get("width"), "height": m.get("height"), "url": m.get("url"),
                "origin": "link" if m.get("url") else ("reference folder" if origin.startswith(ref_folder) else "file"),
                "file": Path(origin).name if origin and not m.get("url") else None,
                "summary": m.get("summary"), "added": m.get("added"), "job": job.id if job else None,
                "thumb": f"/media/refthumb/{q}/0" if (d / "thumbs" / "s0000.jpg").exists() else None,
                "proxy_url": f"/media/refproxy/{q}" if ((d / "proxy.mp4").exists() or m.get("source")) else None}

    def ref_job(self, cmd, title, meta):
        """Run `cutroom ref ...` as a job; its progress lines become `job` and `ref` SSE events."""
        rx = re.compile(r"^ref-progress (\S+) ([\d.]+) ([^:]*):?\s*(.*)$")
        rx_id = re.compile(r"^ref ([0-9a-f]{6,40}): ")

        def parse(job, line):
            m = rx.match(line.strip())
            if m:
                rid, frac, stage, msg = m.group(1), float(m.group(2)), m.group(3).strip(), m.group(4)
                if rid != "-":
                    job.meta["ref"] = rid
                job.progress, job.stage = max(job.progress, frac), f"{stage}{': ' + msg if msg else ''}"[:120]
                self.bus.publish("ref", {"id": job.meta.get("ref"), "status": "analyzing", "stage": stage,
                                         "progress": round(frac, 3), "message": msg, "job": job.id})
                return
            m = rx_id.match(line.strip())
            if m:
                job.meta["ref"] = m.group(1)
                job.stage = line.strip()[:120]

        def done(job):
            rid = job.meta.get("ref")
            row = {"id": rid, "job": job.id, "status": "error" if job.status != "done" else "done", "error": job.error}
            if rid:
                try:
                    from . import reference as R
                    row.update(self.ref_row(R.load_meta(self.cfg, rid)))
                    row["job"] = job.id
                except Exception:  # noqa: BLE001
                    pass
            job.result = rid
            self.bus.publish("ref", row)
        return self.jobs.submit("ref", cmd, title=title, parse=parse, on_done=done, lane="ref", meta=dict(meta))

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
        if not row.get("width") and row.get("status") == "audio":      # recorder audio: a waveform tile
            return self._wave(row, self.work / "frames" / "thumbs" / f"{row['id']}_wave.jpg")
        src = row.get("proxy") if row.get("proxy") and os.path.exists(row["proxy"]) else row["path"]
        return self._extract(src, round(t, 1), out, 320, quality=5)

    def _wave(self, row, out):
        if out.exists() and out.stat().st_size > 0:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.stem}.{threading.get_ident()}.tmp.jpg")
        args = [self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(row["path"]),
                "-filter_complex", "aformat=channel_layouts=mono,showwavespic=s=320x180:colors=0x6fb3ff",
                "-frames:v", "1", "-q:v", "4", str(tmp)]
        with light_slot(self.cfg, label=f"wave {row['id']}"):
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, out)
            return out
        tmp.unlink(missing_ok=True)
        return self._black(out, Path(row.get("rel") or "").name)

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

    def grab(self, source, t, clip=None, file=None, plan=None, dest="grabs", w=1280, ref=None):
        """Save a frame jpg into studio/<dest>/ and return (path, url). source=ref takes `ref` (an id from the
        reference engine) or `file` (a video in renders/reference/)."""
        t = _f(t, "t")
        root = self.work / "studio" / dest
        stamp = f"{int(time.time() * 1000) % 10**10:010d}"
        if source == "clip":
            if not clip:
                raise ApiError(400, "clip is required for source=clip")
            src = self.frame(clip, t, w)
            name = f"clip_{Path(src).stem}_{stamp}.jpg"
        elif source == "ref" and ref:
            from . import reference as R
            try:
                rid = R.resolve_id(self.cfg, ref)
            except KeyError as e:
                raise ApiError(404, str(e).strip("'\"")) from None
            p = R.ref_dir(self.cfg, rid) / "proxy.mp4"
            p = p if p.is_file() else R.source_path(self.cfg, rid)
            if not p:
                raise ApiError(404, f"reference {rid} has no video yet")
            src = self._extract(Path(p), t, self.work / "frames" / "studio" / f"ref_{rid}_{int(t * 1000):09d}_{w}.jpg", w, 2)
            name = f"ref_{rid}_{stamp}.jpg"
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
                        if c[1] == "version" and not own and path in cur and path not in seen:
                            self.plans.note_version(c[0], name, "ai")        # a version the AI (or anyone) wrote
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
            if method != "GET" and path != "/api/upload":          # uploads stream their body to disk
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
                             day=q.get("day") or None, highlights=bool(flag("highlights")),
                             dedupe=bool(flag("dedupe")),
                             session=int(num("session")) if num("session") is not None else None,
                             near=q.get("near") or None, body=q.get("cam") or None)
        mc, cams = {}, {}
        try:
            from . import sync as MC
            with self.studio.pool.get() as con:
                mc = MC.clip_groups(con)
        except Exception:  # noqa: BLE001 - library without sync tables yet
            mc = {}
        try:
            from . import cameras as CAM
            with self.studio.pool.get() as con:
                cams = CAM.badges(con)
        except Exception:  # noqa: BLE001
            cams = {}
        for r in res["rows"]:
            if r["id"] in cams:
                r["cam"] = cams[r["id"]]                # camera badge (cameras.py): label, profile, assumed
            r["thumb"] = f"/media/thumb/{r['id']}"
            r["proxy_url"] = f"/media/proxy/{r['id']}" if r["has_proxy"] else None
            r["sheet_url"] = f"/media/sheet/{r['id']}" if r["has_sheet"] else None
            if r["id"] in mc:
                r["mc"] = mc[r["id"]]                   # multicam group badge (cutroom sync)
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
            raise ApiError(422, "draft has problems; fix them before saving a version (or force: true)", errors=errs)
        name, plan = st.plans.commit(slug, expect=body.get("expect") if isinstance(body, dict) else None)
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
            raise ApiError(409, "render a saved version (Save version first), or pass allow_draft: true")
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

    def api_export(self):
        """Deliverables (cutroom/export.py) as a job: {video: "renders/x.mp4" | plan_file, preset: "reels" |
        [..] | "all", cutdown: [15, 30, 60], thumbs: bool, captions: bool}. Outputs land in
        renders/exports/<stem>/; the `export_done` event lists them."""
        st = self.studio
        body = self._body() or {}
        if body.get("video"):
            target = _within(st.work / "renders", str(body["video"]).removeprefix("renders/"))
            if not target or not target.is_file():
                raise ApiError(404, f"no render {body.get('video')!r}")
        elif body.get("plan_file"):
            target = self._plan_file(body["plan_file"])
        else:
            raise ApiError(400, "pass video (a render) or plan_file")
        presets = body.get("preset") or []
        presets = [presets] if isinstance(presets, str) else list(presets)
        from . import export as EX
        try:
            EX.preset_names(presets)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        cuts = [str(float(x)) for x in (body.get("cutdown") or [])]
        args = ["export", target]
        for pr in presets:
            args += ["--preset", pr]
        if cuts:
            args += ["--cutdown", ",".join(cuts)]
        if body.get("thumbs"):
            args.append("--thumbs")
        if body.get("captions") is False:
            args.append("--no-captions")
        cmd = _cli_cmd(*args)
        stem = target.stem

        def parse(job, line):
            s_ = line.strip()
            if s_.startswith(("export:", "cutdown:")):
                job.stage = s_[:100]
                job.progress = min(0.95, job.progress + 0.08)

        def done(job):
            d = st.work / "renders" / "exports" / stem
            man = _read_json(d / "export.json", {}) or {}
            files = []
            for f in sorted(d.rglob("*")) if d.exists() else []:
                if f.is_file() and not f.name.startswith(".") and f.suffix in (".mp4", ".srt", ".vtt", ".txt", ".jpg", ".json"):
                    rel = f.relative_to(st.work / "renders").as_posix()
                    files.append({"file": rel, "url": f"/media/render/{quote(rel)}"})
            job.meta.update(dir=f"renders/exports/{stem}", files=files, warnings=man.get("warnings", []))
            st.bus.publish("export_done", {"job": job.id, "dir": job.meta["dir"], "files": files,
                                           "warnings": job.meta["warnings"]})
        job = st.jobs.submit("export", cmd, title=f"export {target.name} ({', '.join(presets) or 'files'})",
                             parse=parse, on_done=done, meta={"target": target.name, "presets": presets})
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
             "status": "open", "kind": kind, "created": time.time(), "frame_url": None,
             "author": str(body.get("author") or "you")[:40], "thread": []}
        if body.get("t_end") is not None:
            te = _f(body["t_end"], "t_end")
            if te > t + 0.02:
                m["t_end"] = round(te, 3)
        drawing = clean_drawing(body.get("drawing"))
        if drawing:
            m["drawing"] = drawing
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
        if m["frame_url"]:
            m["frame_raw_url"] = m["frame_url"]
            if drawing:
                st.annotate_comment(m)
        markers_update(st.cfg, lambda data: data.append(m))
        st.bus.publish("markers", {"created": m})
        self._json(m)

    def api_marker_patch(self, mid=None):
        st = self.studio
        body = self._body() or {}
        mid = mid or body.get("id")
        if not mid:
            raise ApiError(400, "marker id required")

        drawing = clean_drawing(body["drawing"]) if "drawing" in body else None
        reply = body.get("reply")
        if reply is not None and (not isinstance(reply, dict) or not str(reply.get("text") or "").strip()):
            raise ApiError(400, "reply must be {text, author?}")

        def f(data):
            for m in data:
                if m.get("id") == mid:
                    for k in ("text", "status", "t", "t_end", "kind", "plan", "frame_url", "author"):
                        if k in body:
                            if k == "status" and body[k] not in COMMENT_STATUSES:
                                raise ApiError(400, f"status must be one of {', '.join(COMMENT_STATUSES)}")
                            if k == "t_end" and body[k] is None:
                                m.pop("t_end", None)
                                continue
                            m[k] = round(_f(body[k], k), 3) if k in ("t", "t_end") else body[k]
                    if body.get("thread") is not None:
                        k_thread = body["thread"]
                        if not isinstance(k_thread, list):
                            raise ApiError(400, "thread must be a list")
                        m["thread"] = [{"author": str(x.get("author") or "you")[:40], "text": str(x.get("text") or "")[:4000],
                                        "ts": x.get("ts") or time.time()} for x in k_thread if isinstance(x, dict)]
                    if reply:
                        m.setdefault("thread", []).append({"author": str(reply.get("author") or "you")[:40],
                                                           "text": str(reply["text"]).strip()[:4000], "ts": time.time()})
                    if drawing is not None:
                        m["drawing"] = drawing
                        if m.get("frame_raw_url"):
                            st.annotate_comment(m)
                    m["updated"] = time.time()
                    return m
            raise ApiError(404, f"no marker {mid}")
        m = markers_update(st.cfg, f)
        if "t" in body and not body.get("frame_url") and m.get("kind", "note") == "note" and m.get("plan"):
            try:                                           # the comment moved: its frame follows
                _, raw = st.grab("program", float(m["t"]), plan=m["plan"], dest="markers")
                fresh = dict(m, frame_raw_url=raw)
                st.annotate_comment(fresh)

                def g(data):
                    for x in data:
                        if x.get("id") == mid:
                            x.update(frame_raw_url=fresh["frame_raw_url"], frame_url=fresh["frame_url"])
                            return x
                    return m
                m = markers_update(st.cfg, g)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"studio: regrab for comment {mid}: {e}\n")
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

    # -- review comments run queue ---------------------------------------------------------------------------------
    def api_comments_run(self):
        body = self._body() or {}
        st = self.studio.comments.start(body.get("plan") or self.q.get("plan"), ids=body.get("ids"),
                                        all_open=bool(body.get("all_open")))
        self._json(st)

    def api_comments_stop(self):
        self._json({"stopping": self.studio.comments.stop(), **self.studio.comments.status()})

    def api_comments_status(self):
        self._json(self.studio.comments.status())

    # -- looks (cutroom/looks.py) ----------------------------------------------------------------------------------
    def _look_frame(self):
        """(clip, source t, plan style, where) from ?clip=&t= or ?plan=&t= (timeline time)."""
        st = self.studio
        q = self.q
        style = None
        slug = q.get("plan")
        if slug:
            if not SLUG_RE.match(slug):
                raise ApiError(400, "bad plan slug")
            plan = st.plans.open(slug)
            style = plan.get("style") or {}
        if q.get("clip"):
            return q["clip"], _f(q.get("t", 0), "t"), style, None
        if not slug:
            raise ApiError(400, "pass ?clip=<id>&t=<source s> or ?plan=<slug>&t=<timeline s>")
        from .render import locate
        t = _f(q.get("t", 0), "t")
        tl = st.resolve(plan)
        loc = locate(tl, t, "V2")
        if not (loc and loc["piece"]["start"] - 1e-6 <= t < loc["piece"]["end"]):
            loc = locate(tl, t)
        if not loc or not loc.get("clip") or loc.get("src_t") is None:
            raise ApiError(409, "no picture at that moment (title or black); move the playhead onto a shot")
        return loc["clip"], loc["src_t"], style, {"sec": loc["sec"], "seg": loc["seg"], "t": t}

    def _look_previews(self, clip, t, lks, style):
        from . import looks as LK
        st = self.studio
        cfg = st.cfg

        def run(args):
            with light_slot(cfg, label="look previews"):
                from . import media
                media.ffmpeg(cfg, args, timeout=180)
        with st.keylock(f"looks:{clip}:{round(t, 2)}"), st.pool.get() as con:
            try:
                return LK.previews(cfg, con, clip, t, lks, plan_style=style, run=run)
            except KeyError as e:
                raise ApiError(404, str(e).strip("'\"")) from None
            except LK.LookError as e:
                raise ApiError(400, str(e)) from None

    @staticmethod
    def _look_q(name, k):
        from . import looks as LK
        try:
            k = 1.0 if k in (None, "") else float(k)
        except ValueError:
            raise ApiError(400, "intensity must be a number") from None
        if k > 1.0:
            k = k / 100.0                                   # 0-100 % also accepted
        try:
            return LK.normalize_look({"name": name or "natural", "intensity": max(0.0, min(1.0, k))})
        except LK.LookError as e:
            raise ApiError(400, str(e)) from None

    def api_looks(self):
        from . import looks as LK
        st = self.studio
        self._json({"looks": LK.catalogue(st.cfg), "luts_dir": str(LK.luts_dir(st.cfg))})

    def api_looks_preview(self):
        clip, t, style, _ = self._look_frame()
        lk = self._look_q(self.q.get("look"), self.q.get("intensity"))
        (p,) = self._look_previews(clip, t, [lk], style)
        self._file(p, cache="max-age=3600")

    def api_looks_sheet(self):
        from . import looks as LK
        st = self.studio
        clip, t, style, where = self._look_frame()
        k = self.q.get("intensity")
        cat = LK.catalogue(st.cfg)
        lks = [self._look_q(c["name"], k) for c in cat]
        paths = self._look_previews(clip, t, lks, style)
        items = [{"name": c["name"], "label": c["label"], "kind": c["kind"], "desc": c["desc"], "css": c["css"],
                  "intensity": lk["intensity"], "url": f"/media/look/{quote(p.name)}"}
                 for c, lk, p in zip(cat, lks, paths)]
        self._json({"clip": clip, "t": round(t, 3), "where": where, "items": items,
                    "before_url": items[0]["url"] if items else None})

    def api_looks_match(self):
        """Nearest built-in look (+ intensity) to a reference's colour (overall or one section)."""
        from . import looks as LK
        from . import reference as R
        st = self.studio
        rid = self._ref_id(self.q.get("ref") or "")
        an = R._jload(R.ref_dir(st.cfg, rid) / "analysis.json")
        if not an or not an.get("colour"):
            raise ApiError(409, f"reference {rid} is not analyzed yet", ref=rid)
        col = an["colour"]
        target, scope = col.get("overall"), "whole reference"
        if self.q.get("section") not in (None, ""):
            sec = next((x for x in col.get("by_section") or [] if str(x.get("i")) == str(self.q["section"])), None)
            if sec:
                target, scope = sec, f"section {sec.get('name') or sec.get('i')}"
        if not target or any(target.get(k) is None for k in ("luma", "contrast", "sat", "warmth")):
            raise ApiError(409, "the reference has no colour measurements")
        clip, t, style, where = self._look_frame()
        names = [b["name"] for b in LK.BUILTIN]
        paths = self._look_previews(clip, t, [{"name": n, "intensity": 1.0} for n in names], style)
        stats = {n: LK.image_stats(p) for n, p in zip(names, paths)}
        res = LK.match(stats["natural"], {n: v for n, v in stats.items() if n != "natural"}, target)
        (pv,) = self._look_previews(clip, t, [{"name": res["name"], "intensity": res["intensity"]}], style)
        thumb = None
        shots = an.get("shots") or []
        if shots:
            mid = shots[len(shots) // 2]
            if scope != "whole reference":
                inside = [x for x in shots if str(x.get("section")) == str(self.q.get("section"))]
                mid = inside[len(inside) // 2] if inside else mid
            if mid.get("thumb"):
                thumb = f"/media/refthumb/{quote(rid)}/{mid['i']}"
        self._json({"ref": rid, "scope": scope, "target": {k: target[k] for k in ("luma", "contrast", "sat", "warmth")},
                    "yours": stats["natural"], "pick": {"name": res["name"], "label": LK.label_of(res["name"]),
                                                       "intensity": res["intensity"]},
                    "distance": res["distance"], "base_distance": res["base_distance"], "ranked": res["ranked"][:5],
                    "preview_url": f"/media/look/{quote(pv.name)}", "before_url": f"/media/look/{quote(paths[0].name)}",
                    "ref_thumb": thumb, "clip": clip, "t": round(t, 3), "where": where})

    def media_look(self, rel):
        self._file(_within(self.studio.work / "frames" / "looks", rel), cache="max-age=3600")

    def api_grab(self):
        body = self._body() or {}
        src = body.get("source", "program")
        path, url = self.studio.grab(src, body.get("t", 0), clip=body.get("clip"), ref=body.get("ref"),
                                     file=body.get("file"), plan=body.get("plan"), w=int(_f(body.get("w", 1280), "w")))
        self._json({"url": url, "path": str(path), "source": src, "t": body.get("t", 0)})

    def api_context(self):
        self._json({"ok": True, "context": self.studio.save_context(self._body() or {})})

    def api_days(self):
        from . import tags
        with self.studio.pool.get() as con:
            days = tags.days(con)
            try:
                from . import sync as MC
                by = MC.groups_by_day(con)
            except Exception:  # noqa: BLE001
                by = {}
        for d in days:
            if by.get(d["day"]):
                d["multicam"] = by[d["day"]]
        self._json(days)

    def api_cameras(self):
        """Cameras found in the library (cameras.py) + the profiles a clip can be corrected to."""
        from . import cameras as CAM
        from . import colorsci as CS
        with self.studio.pool.get() as con:
            cams = CAM.cameras(con)
        self._json({"cameras": cams, "profiles": CS.ALL_PROFILES, "families": CAM.FAMILIES})

    def api_clip_camera(self, cid):
        """Correct a clip's camera / log profile: {profile?, family?, model?, scope: clip|camera}."""
        from . import cameras as CAM
        body = self._body() or {}
        scope = body.get("scope", "clip")
        if scope not in ("clip", "camera"):
            raise ApiError(400, "scope must be clip or camera")
        row = self.studio.clip_row(cid)
        with self.studio.pool.get() as con:
            try:
                ids = CAM.set_override(con, row["id"], family=body.get("family") or None, model=body.get("model"),
                                       profile=body.get("profile") or None, scope=scope)
            except (ValueError, KeyError) as e:
                raise ApiError(400, str(e)) from None
            badge = CAM.badges(con).get(row["id"])
        self._json({"ok": True, "clips": ids, "cam": badge})

    def api_camera_match(self):
        """Before / after of the camera match on the frame under the playhead (?plan=&t= or ?clip=&t=)."""
        from PIL import Image
        from . import cameras as CAM
        st = self.studio
        clip, t, style, where = self._look_frame()
        with st.pool.get() as con:
            row = dict(db.get_clip(con, clip))
            cam = CAM.get(con, row["id"])
            pv = CAM.preview_pair(st.cfg, con, row["id"], t, style=style or {}, width=480) if cam else {}
        if not cam or not pv:
            self._json({"clip": row["id"], "camera": cam and CAM.short_label(cam), "available": False})
            return
        d = st.work / "frames" / "looks"
        d.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(json.dumps([row["id"], round(t, 3), pv.get("match")], sort_keys=True, default=str).encode()).hexdigest()[:12]
        urls = {}
        for k in ("before", "after"):
            fp = d / f"cam_{row['id']}_{key}_{k}.jpg"
            if not fp.exists():
                Image.fromarray(pv[k]).save(fp, quality=88)
            urls[k] = f"/media/look/{quote(fp.name)}"
        m = pv.get("match") or {}
        self._json({"clip": row["id"], "available": True, "camera": CAM.short_label(cam), "cam_key": cam["cam_key"],
                    "profile": cam["log_profile"], "profile_src": cam["profile_src"], "confidence": cam["confidence"],
                    "before_url": urls["before"], "after_url": urls["after"], "where": where,
                    "match": {k: m.get(k) for k in ("gains", "sat", "exposure_stops", "de_before", "de_after", "n_pairs",
                                                    "day", "disabled", "why", "method")} if m else None})

    def api_multicam(self):
        """Synced multicam groups (cutroom sync): members, angles, offsets, drift; and the devices."""
        from . import sync as MC
        with self.studio.pool.get() as con:
            self._json({"groups": MC.groups(con), "devices": MC.devices(con)})

    def api_export_nle(self):
        """Open in Premiere: {plan_file?, formats?: ["premiere", "fcpxml", ...], zip?: true} -> a job running
        `cutroom export-nle`; the `export_done` event carries {nle: true, dir, zip, howto}."""
        st = self.studio
        body = self._body() or {}
        if body.get("plan_file"):
            target = self._plan_file(body["plan_file"])
        else:
            slug = str(body.get("slug") or "").strip()
            if not slug:
                raise ApiError(400, "pass plan_file or slug")
            vs = sorted(st.plans.dir.glob(f"{slug}_v*.json"), key=lambda p: int(re.search(r"_v(\d+)$", p.stem).group(1))
                        if re.search(r"_v(\d+)$", p.stem) else -1) if slug else []
            if not vs:
                raise ApiError(400, "save a version first (Cmd/Ctrl+S): the hand-off is made from a saved version")
            target = vs[-1]
        fmts = body.get("formats") or ["premiere", "fcpxml"]
        fmts = [fmts] if isinstance(fmts, str) else list(fmts)
        from . import nle as NL
        try:
            NL.formats(fmts)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        args = ["export-nle", target, "--json"]
        for f in fmts:
            args += ["--format", f]
        if body.get("zip", True):
            args.append("--zip")
        if body.get("clean_render"):
            args.append("--clean-render")
        cmd = _cli_cmd(*args)

        def parse(job, line):
            s_ = line.strip()
            if s_.startswith("export-nle:"):
                job.stage = s_[:100]
                job.progress = min(0.95, job.progress + 0.2)
            job.meta.setdefault("out", []).append(line)

        def done(job):
            try:
                txt = "\n".join(job.meta.get("out", []))
                man = json.loads(txt[txt.index("{"):])
            except (ValueError, KeyError):
                man = {}
            d = Path(man.get("dir") or "")
            rel = lambda x: Path(x).relative_to(st.work / "renders").as_posix() if str(x).startswith(str(st.work / "renders")) else str(x)  # noqa: E731
            files = [{"file": rel(f), "url": f"/media/render/{quote(rel(f))}"} for f in man.get("outputs", [])]
            howto = ("Premiere Pro: File > Import... the .xml in this folder (the zip holds the same files). Relink "
                     "with Link Media... if the footage drive moved. Import captions.srt the same way. Put the LUT "
                     "on an adjustment layer between V2 and the graphics. README_PREMIERE.txt has the details.")
            job.meta.update(dir=str(d), zip=man.get("zip"), files=files, warnings=man.get("warnings", []), howto=howto)
            st.bus.publish("export_done", {"job": job.id, "nle": True, "dir": str(d), "zip": man.get("zip"),
                                           "files": files, "warnings": man.get("warnings", []), "howto": howto})
        job = st.jobs.submit("export", cmd, title=f"Open in Premiere: {target.name}", parse=parse, on_done=done,
                             meta={"target": target.name, "formats": fmts})
        self._json({"job": job.id, "status": job.status})

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

    # -- references (cutroom/reference.py, docs/STUDIO.md "Reference engine") -------------------------------------
    def _ref_id(self, rid):
        from . import reference as R
        try:
            return R.resolve_id(self.studio.cfg, rid)
        except KeyError as e:
            raise ApiError(404, str(e).strip("'\"")) from None

    def api_refs(self):
        from . import reference as R
        st = self.studio
        try:
            R.sync_reference_folder(st.cfg)          # renders/reference/* show up as references (linked, not copied)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"studio: reference folder sync failed: {e}\n")
        self._json({"refs": [st.ref_row(m) for m in R.list_refs(st.cfg)]})

    def api_refs_post(self):
        from . import reference as R
        st = self.studio
        body = self._body() or {}
        src = str(body.get("src") or body.get("url") or body.get("file") or "").strip()
        if not src:
            raise ApiError(400, "src is required: a link (needs yt-dlp) or a video file path")
        link = False
        if R.is_url(src) and not shutil.which("yt-dlp"):
            from .imports import YTDLP_INSTALL
            raise ApiError(424, "adding a reference from a link needs yt-dlp, which is not installed",
                           install=YTDLP_INSTALL)
        if not R.is_url(src):
            if src.startswith(("/media/ref/", "renders/reference/")):          # a file in the Reference tab folder
                p = _within(st.work / "renders" / "reference", unquote(src.split("/reference/", 1)[-1].split("/ref/", 1)[-1]))
            elif Path(src).expanduser().is_absolute():
                p = Path(src).expanduser()
            else:
                p = _within(st.work, src)
            if not p or not Path(p).is_file():
                raise ApiError(404, f"no video file at {src!r}")
            src = str(p)
            link = (st.work / "refs" / "_incoming").resolve() in Path(src).resolve().parents   # an upload: no 2nd copy
        title = str(body.get("title") or "").strip()
        args = ["ref", "add", src] + (["--title", title] if title else []) + (["--link"] if link else []) + \
            (["--no-analyze"] if body.get("analyze") is False else [])
        job = st.ref_job(_cli_cmd(*args), f"reference {title or Path(src).name}"[:120], {"src": src})
        self._json({"job": job.id, "status": job.status})

    def api_ref_analyze(self, rid):
        st = self.studio
        rid = self._ref_id(rid)
        body = self._body() or {}
        job = st.ref_job(_cli_cmd("ref", "analyze", rid, *(["--force"] if body.get("force") else [])),
                         f"analyze reference {rid}", {"ref": rid})
        self._json({"job": job.id, "status": job.status, "ref": rid})

    def api_ref(self, rid):
        from . import reference as R
        st = self.studio
        rid = self._ref_id(rid)
        m = R.load_meta(st.cfg, rid)
        an = R._jload(R.ref_dir(st.cfg, rid) / "analysis.json") if m.get("status") == "analyzed" else None
        out = st.ref_row(m)
        out["meta"] = m
        out["analysis"] = an
        if an:
            q = quote(rid)
            out["thumbs"] = [f"/media/refthumb/{q}/{s['i']}" if s.get("thumb") else None for s in an["shots"]]
            out["sheets"] = [f"/media/refsheet/{q}/{k + 1}" for k in range(len(an.get("sheets") or []))]
            out["cutmap"] = R.cutmap_rows(an)
        self._json(out)

    def media_refthumb(self, rid, n):
        from . import reference as R
        rid = self._ref_id(rid)
        try:
            k = int(n)
        except ValueError:
            raise ApiError(400, "shot number must be an integer") from None
        self._file(R.ref_dir(self.studio.cfg, rid) / "thumbs" / f"s{k:04d}.jpg", cache="max-age=3600")

    def media_refsheet(self, rid, n):
        from . import reference as R
        rid = self._ref_id(rid)
        try:
            k = int(n)
        except ValueError:
            raise ApiError(400, "sheet number must be an integer") from None
        self._file(R.ref_dir(self.studio.cfg, rid) / f"sheet_{k:03d}.jpg", cache="no-cache")

    def media_refproxy(self, rid):
        from . import reference as R
        rid = self._ref_id(rid)
        d = R.ref_dir(self.studio.cfg, rid)
        p = d / "proxy.mp4"
        if not p.is_file():
            p = R.source_path(self.studio.cfg, rid)
        self._file(p and Path(p).resolve())

    def api_ref_compare(self, rid):
        from . import reference as R
        st = self.studio
        rid = self._ref_id(rid)
        if not (R.ref_dir(st.cfg, rid) / "analysis.json").exists():
            raise ApiError(409, f"reference {rid} is not analyzed yet", ref=rid)
        render, slug = self.q.get("render"), self.q.get("plan")
        if render:
            v = _within(st.work / "renders", render.removeprefix("renders/").removeprefix("/media/render/"))
            if not v or not v.is_file():
                raise ApiError(404, f"no render {render!r}")
        elif slug:
            if not SLUG_RE.match(slug):
                raise ApiError(400, "bad plan slug")
            v = R.latest_render_for_plan(st.cfg, slug)
        else:
            raise ApiError(400, "pass ?plan=<slug> or ?render=<file in renders/>")
        if v is not None:
            res = R.compare(st.cfg, rid, v, echo=lambda *_: None)
            try:
                res["render"] = v.resolve().relative_to((st.work / "renders").resolve()).as_posix()
            except ValueError:                      # a render outside this project (moved / copied project)
                res["render"] = v.name
            res["render_url"] = f"/media/render/{quote(res['render'])}"
        else:
            plan = st.plans.open(slug)
            tl = st.resolve(plan)
            norm = P.normalize(plan, st.cfg)
            out_h = (norm.get("output") or {}).get("height") or st.cfg.out_height
            res = R.compare(st.cfg, rid, {**tl, "label": f"{slug} (draft timeline, not rendered)"},
                            style=norm.get("style"), out_h=out_h)
            res["render"] = None
        res["plan"] = slug
        self._json(res)

    # -- import (cutroom/imports.py, docs/STUDIO.md "Import") ------------------------------------------------------
    def api_upload(self):
        """POST /api/upload?kind=reference|song|image|footage-link&name=<file>[&id=<hex>&offset=<n>&final=0|1&total=<n>]
        Body = the bytes (Content-Length or chunked), streamed straight to disk. Big files come in pieces with the
        same id; each piece must start where the last one ended (409 + received otherwise)."""
        from . import imports as I
        st = self.studio
        q = self.q
        try:
            kind = q.get("kind", "")
            d = I.dest_dir(st.cfg, kind)
            name = I.safe_name(q.get("name"), I.KINDS[kind]["exts"])
            uid = q.get("id") or uuid.uuid4().hex[:16]
            part = I.part_path(d, uid)
            offset = int(q.get("offset") or 0)
            final = str(q.get("final", "1")) not in ("0", "false", "")
            total = int(q.get("total") or 0)
            limit = I.KINDS[kind]["max"]
            if total > limit:
                raise I.UploadError(413, f"file is too large (max {limit / 1024 ** 3:.1f} GB for {kind})")
            with st.keylock("upload:" + uid):
                have = part.stat().st_size if part.exists() else 0
                if offset != have:
                    self.close_connection = True
                    raise ApiError(409, "upload piece out of order", received=have, id=uid)
                if offset == 0:
                    I.sweep_stale(d)
                cl = int(self.headers.get("Content-Length") or 0)
                I.check_disk(st.cfg, d, max(total - offset, cl))
                try:
                    with open(part, "ab") as fh:
                        n = I.read_body(self.rfile, self.headers, fh.write, limit - offset)
                except I.UploadError:
                    self.close_connection = True
                    raise
                size = offset + n
                if not final:
                    return self._json({"id": uid, "kind": kind, "received": size, "done": False})
                if total and size != total:
                    raise ApiError(409, f"upload incomplete: {size} of {total} bytes", received=size, id=uid)
                out = I.unique_path(d, name)
                os.replace(part, out)
        except I.UploadError as e:
            self.close_connection = True
            raise ApiError(e.code, e.msg) from None
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "offset and total must be integers") from None
        res = {"id": uid, "kind": kind, "name": out.name, "path": str(out), "size": out.stat().st_size, "done": True}
        if kind == "image":
            res["url"] = f"/media/image/{quote(out.name)}"
        elif kind == "song":
            job = st.jobs.submit("songs", _cli_cmd("songs", "index"), title=f"add song {out.stem}"[:120], lane="songs",
                                 meta={"file": out.name})
            res["job"] = job.id
        elif kind == "footage-link":
            I.add_roots(st.cfg, [str(d.resolve())])
            res["job"] = self._index_job([str(out.resolve())], f"import {out.name}").id
        self._json(res)

    def api_upload_status(self, uid):
        from . import imports as I
        try:
            part = I.part_path(I.dest_dir(self.studio.cfg, self.q.get("kind", "")), uid)
        except I.UploadError as e:
            raise ApiError(e.code, e.msg) from None
        self._json({"id": uid, "received": part.stat().st_size if part.exists() else 0})

    def media_image(self, rel):
        self._file(_within(self.studio.work / "studio" / "images", rel), cache="max-age=3600")

    def api_import_info(self):
        from . import imports as I
        self._json({"ytdlp": bool(shutil.which("yt-dlp")), "ytdlp_install": I.YTDLP_INSTALL,
                    "kinds": {k: {"max": v["max"], "exts": sorted(v["exts"])} for k, v in I.KINDS.items()},
                    "library_roots": len(self.studio.cfg.library_roots)})

    def api_import_locate(self):
        """{files: [{name, size}], folder?} -> {files: [{name, size, path|null, clip|null, in_library}]}"""
        from . import imports as I
        body = self._body() or {}
        files = body.get("files") if isinstance(body.get("files"), list) else []
        if len(files) > 500:
            raise ApiError(400, "at most 500 files at a time")
        with self.studio.pool.get() as con:
            got = I.locate(self.studio.cfg, con, files, folder=body.get("folder"))
        self._json({"files": got, "found": sum(1 for g in got if g["path"]), "total": len(got)})

    def _index_job(self, files, title):
        st = self.studio
        lst = st.work / "studio" / "imports" / f"{uuid.uuid4().hex[:10]}.txt"
        lst.parent.mkdir(parents=True, exist_ok=True)
        lst.write_text("\n".join(files) + "\n")
        steps = {"probe": (0.05, "reading files"), "proxies": (0.15, "making previews"),
                 "sheets": (0.6, "contact sheets"), "analyze": (0.75, "finding shots")}

        def parse(job, line):
            s_ = line.strip()
            if s_.startswith("index-step "):
                job.progress, job.stage = steps.get(s_[11:], (job.progress, s_[11:]))
            elif s_.startswith(("proxies ", "analyze ")) and "/" in s_:
                m = re.match(r"(proxies|analyze) (\d+)/(\d+)", s_)
                if m:
                    a, b = (0.15, 0.6) if m.group(1) == "proxies" else (0.75, 0.98)
                    job.progress = a + (b - a) * int(m.group(2)) / max(1, int(m.group(3)))

        def done(job):
            st.bus.publish("library_changed", {"job": job.id, "n": len(files), "status": job.status})
        return st.jobs.submit("import", _cli_cmd("index", "--files-from", lst), title=title[:120], parse=parse,
                              on_done=done, lane="import", meta={"files": len(files)})

    def api_import_footage(self):
        """{paths: [file or folder, ...], folder?} -> footage is linked, never copied: a folder outside the library
        becomes a library root, then just those files are indexed as a job."""
        from . import imports as I
        st = self.studio
        body = self._body() or {}
        paths = body.get("paths") if isinstance(body.get("paths"), list) else []
        if not paths and not body.get("folder"):
            raise ApiError(400, "paths (files or folders) are required")
        plan = I.plan_footage(st.cfg, [str(x) for x in paths][:5000], folder=body.get("folder"))
        if not plan["files"]:
            raise ApiError(422, "nothing to add" + (f": {plan['skipped'][0]['why']}" if plan["skipped"] else ""),
                           skipped=plan["skipped"])
        I.add_roots(st.cfg, plan["roots_new"])
        n = len(plan["files"])
        job = self._index_job(plan["files"], f"import {n} clip{'s' if n != 1 else ''}")
        self._json({"job": job.id, "files": n, "roots_added": len(plan["roots_new"]), "skipped": plan["skipped"]})

    # -- factory (docs/FACTORY.md; cutroom/factory) --------------------------------------------------------------
    def _fr(self):
        from .factory import runner as FR
        return FR

    def _fcall(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except KeyError as e:
            raise ApiError(404, str(e).strip("'\"")) from None
        except ValueError as e:
            raise ApiError(400, str(e)) from None

    def _oid(self, oid):
        from .factory import bus
        if not bus.ID_RE.match(oid or "") or not (bus.orders_dir(self.studio.cfg) / oid).is_dir():
            raise ApiError(404, f"no order {oid}")
        return oid

    def api_factory_orders_post(self):
        st = self.studio
        body = self._body() or {}
        kind = body.get("kind") or "revise"
        plan = body.get("plan")
        if not plan or not SLUG_RE.match(str(plan)):
            raise ApiError(400, "plan is required")
        if kind not in ("revise", "reject", "request", "variants"):
            raise ApiError(400, "kind must be revise, reject, request or variants")
        extra = {}
        if body.get("ref"):
            extra["ref"] = str(body["ref"])[:80]
        res = self._fcall(self._fr().submit, st.cfg, kind, plan, comment_ids=body.get("comment_ids") or body.get("ids"),
                          all_open=bool(body.get("all_open")), text=str(body.get("text") or ""),
                          attachments=body.get("attachments") or [], by={"user": "you", "via": body.get("via") or "studio"},
                          **({"variants": int(body["variants"])} if kind == "variants" and body.get("variants") else {}),
                          **({"ref": extra["ref"]} if extra.get("ref") else {}))
        if st.factory:
            st.factory.wake()
        o = res["order"]
        self._json({"order": o, "id": o["id"], "folded": res.get("folded"), "folded_into": res.get("folded_into"),
                    "status": self._fr().bus.status(st.cfg, o["id"])})

    def api_factory_orders(self):
        self._json({"orders": self._fr().list_view(self.studio.cfg, plan=self.q.get("plan") or None,
                                                   state=self.q.get("state") or None)})

    def api_factory_order(self, oid):
        d = self._fr().order_detail(self.studio.cfg, self._oid(oid))
        d["roles"] = role_rows_for_ui(d.get("roles"), self.studio.cfg.work)
        self._json(d)

    def api_factory_role_log(self, oid, role):
        from .factory import bus
        d = bus.order_dir(self.studio.cfg, self._oid(oid)) / "roles"
        p = _within(d, role)
        if not p or not p.is_dir():
            raise ApiError(404, f"no role run {role}")
        lines = []
        try:
            with open(p / "stream.jsonl", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            pass
        if not self.q.get("all"):
            lines = lines[-200:]
        self._json({"role": role, "lines": lines, "usage": _read_json(p / "usage.json"),
                    "prompt": (p / "prompt.md").read_text()[:20000] if (p / "prompt.md").exists() else None})

    def api_factory_stop(self, oid):
        st = self.studio
        self._json(self._fcall(self._fr().stop, st.cfg, self._oid(oid), runner=st.factory))

    def api_factory_retry(self, oid):
        st = self.studio
        out = self._fcall(self._fr().retry, st.cfg, self._oid(oid))
        if st.factory:
            st.factory.wake()
        self._json(out)

    def api_factory_answer(self):
        st = self.studio
        b = self._body() or {}
        self._json(self._fcall(self._fr().answer, st.cfg, self._oid(b.get("order")), str(b.get("question") or ""),
                               b.get("option"), text=str(b.get("text") or ""), runner=st.factory))

    def api_factory_verdict(self):
        st = self.studio
        b = self._body() or {}
        if not SLUG_RE.match(str(b.get("plan") or "")):
            raise ApiError(400, "plan is required")
        try:
            v = int(b.get("version"))
        except (TypeError, ValueError):
            raise ApiError(400, "version must be a number") from None
        self._json(self._fcall(self._fr().verdict, st.cfg, b["plan"], v, b.get("disposition"), text=b.get("text") or "",
                               runner=st.factory))

    def api_factory_variants(self, oid):
        st = self.studio
        b = self._body() or {}
        self._json({"results": self._fcall(self._fr().variant_verdict, st.cfg, self._oid(oid), b.get("items") or [],
                                           runner=st.factory)})

    def api_factory_apply_extension(self, oid):
        self._oid(oid)
        raise ApiError(501, "the extender is a stub in this build: features are not built or applied automatically yet",
                       stub=True)

    def api_factory_state(self):
        from .factory import fconfig
        st = self.studio
        self._json({**st.factory_light(), "config": fconfig.load(st.cfg)})

    def api_factory_pause(self):
        self._fr().pause(self.studio.cfg)
        self._json(self.studio.factory_light())

    def api_factory_resume(self):
        st = self.studio
        n = self._fr().resume(st.cfg)
        if st.factory:
            st.factory._login = (0.0, None)
            st.factory.wake()
        self._json({**st.factory_light(), "requeued": n})

    def api_factory_lessons(self):
        from .factory import lessons, lint
        cfg = self.studio.cfg
        d = lessons.load(cfg, "project")
        g = lessons.load(cfg, "global")
        tag = self.q.get("tag")
        rows = [x for x in d["lessons"] + g["lessons"] if (not tag or tag in (x.get("tags") or []))
                and (self.q.get("scope") in (None, "", x.get("scope")))]
        self._json({"lessons": rows, "inbox": [r for r in d["inbox"] if r.get("status") == "open"],
                    "unconfirmed": [r for r in d["inbox"] if r.get("status") == "unconfirmed"][-30:],
                    "deleted": (d.get("deleted") or [])[-20:],
                    "inbox_count": len([r for r in d["inbox"] if r.get("status") == "open"]),
                    "checks": lint.load_checks(cfg), "check_types": list(lint.TYPES), "lessons_sha": lessons.lessons_sha(cfg)})

    def api_factory_lessons_post(self):
        from .factory import lessons
        b = self._body() or {}
        if not str(b.get("rule") or "").strip():
            raise ApiError(400, "rule is required")
        les = self._fcall(lessons.add_lesson, self.studio.cfg, str(b["rule"]).strip()[:600], symptom=str(b.get("symptom") or ""),
                          tags=b.get("tags") or [], check=b.get("check"), source="studio")
        self._json(les)

    def api_factory_lessons_patch(self):
        from .factory import lessons, lint
        b = self._body() or {}
        cfg = self.studio.cfg
        lid = b.get("id")
        if b.get("status") == "superseded":
            return self._json(self._fcall(lessons.supersede, cfg, lid, by=b.get("by") or None,
                                          scope=self._lscope(b), reason=b.get("reason")))
        if b.get("status") == "deleted":
            return self._json(self._fcall(lessons.delete, cfg, lid, scope=self._lscope(b), reason=b.get("reason")))
        if b.get("check"):
            rec = self._fcall(lint.add_check, cfg, {**b["check"], "lesson": lid}, scope=b.get("scope") or "project")
            with lessons._lock(cfg, "project"):
                d = lessons.load(cfg, "project")
                for x in d["lessons"]:
                    if x["id"] == lid:
                        x["check"] = rec["id"]
                lessons._save(cfg, "project", d)
            return self._json(rec)
        raise ApiError(400, "send {id, status: superseded} or {id, check: {type, params}}")

    @staticmethod
    def _lscope(b):
        sc = (b or {}).get("scope") or "project"
        if sc not in ("project", "global"):
            raise ApiError(400, "scope is project or global")
        return sc

    def api_factory_lesson_delete(self, lid):
        """Delete a wrong lesson (e.g. one a QA bug taught): it leaves the law and its learned check retires."""
        from .factory import lessons
        b = self._body() or {}
        self._json(self._fcall(lessons.delete, self.studio.cfg, lid, scope=self._lscope(b), reason=b.get("reason")))

    def api_factory_lesson_supersede(self, lid):
        """Retire a lesson, optionally naming the lesson that replaces it ({by, reason})."""
        from .factory import lessons
        b = self._body() or {}
        by = str(b.get("by") or "").strip() or None
        if by == lid:
            raise ApiError(400, "a lesson can't supersede itself")
        self._json(self._fcall(lessons.supersede, self.studio.cfg, lid, by=by, scope=self._lscope(b), reason=b.get("reason")))

    def api_factory_inbox_confirm(self, iid):
        """The creator confirms a critic / machine finding is a real problem: it may now become a lesson."""
        from .factory import lessons
        b = self._body() or {}
        self._json(self._fcall(lessons.confirm_item, self.studio.cfg, iid, by="operator", text=b.get("text")))

    def api_factory_inbox_dismiss(self, iid):
        from .factory import lessons
        b = self._body() or {}
        self._json(self._fcall(lessons.dismiss_item, self.studio.cfg, iid, reason=b.get("reason") or "not a real problem"))

    def api_factory_brain(self, slug):
        from .factory import brain
        PlanStore.check_slug(slug)
        cfg = self.studio.cfg
        self._json({**brain.summary(cfg, slug), "files": {n: brain.read_file(cfg, slug, n) for n in brain.FILES},
                    "grades": brain.grades(cfg)})

    def api_factory_brain_file(self, slug, name):
        from .factory import brain
        PlanStore.check_slug(slug)
        if name not in brain.FILES:
            raise ApiError(404, f"no brain file {name}")
        self._json({"name": name, "value": brain.read_file(self.studio.cfg, slug, name)})

    def api_factory_brain_put(self, slug, name):
        from .factory import brain
        PlanStore.check_slug(slug)
        if name not in brain.FILES:
            raise ApiError(404, f"no brain file {name}")
        b = self._body()
        val = b.get("value") if isinstance(b, dict) and "value" in b else b
        self._json({"name": name, "value": self._fcall(brain.write_file, self.studio.cfg, slug, name, val)})

    def api_grades(self):
        from .factory import brain
        self._json({"grades": brain.grades(self.studio.cfg)})

    def api_grades_post(self):
        from .factory import brain
        b = self._body() or {}
        rec = self._fcall(brain.add_grade, self.studio.cfg, b.get("clip"), b.get("t0"), b.get("t1"),
                          grade=b.get("grade") or None, ident=b.get("ident") or None)
        self._json(rec)

    def api_factory_versions(self):
        """Versions of a plan for the Review tab: file, render, card, theme, verdict, badges."""
        from .factory import bus, phases
        st = self.studio
        slug = self.q.get("plan")
        PlanStore.check_slug(slug or "")
        vi = phases.versions_index(st.cfg)
        meta = _read_json(st.plans.hist_dir(slug) / "versions.json", {}) or {}
        out = []
        for v in st.plans.version_files(slug):
            if v["kind"] != "version":
                continue
            key = f"{slug}_v{v['version']}"
            fx = vi.get(key) or {}
            cardp = Path(fx["card"]) if fx.get("card") else None
            cd = _read_json(cardp, None) if cardp else None
            plan_path = st.plans.dir / v["file"]
            rr = fx.get("render")
            if not rr:
                from .factory.phases import find_render
                r0 = find_render(st.cfg, plan_path)
                rr = r0.resolve().relative_to((st.work / "renders").resolve()).as_posix() if r0 else None
            out.append({"file": v["file"], "version": v["version"], "mtime": v["mtime"], "by": v.get("by"),
                        "theme": fx.get("theme") or (meta.get(v["file"]) or {}).get("theme"),
                        "render": rr, "render_url": f"/media/render/{quote(rr)}" if rr else None,
                        "final_render": fx.get("final_render"), "order": fx.get("order"), "passed": fx.get("passed"),
                        "not_passed": fx.get("not_passed"), "verdict": fx.get("verdict"), "approved": fx.get("approved"),
                        "conditional": fx.get("conditional"), "card": cd,
                        "sheet_url": f"/media/render/{quote(cd['changed_sheet'])}" if cd and cd.get("changed_sheet") else None})
        out.sort(key=lambda x: x["version"] if isinstance(x["version"], (int, float)) else -1, reverse=True)
        refs = []
        try:
            from . import reference as R
            refs = [{"id": m["id"], "title": m.get("title"), "proxy_url": f"/media/refproxy/{quote(m['id'])}"}
                    for m in R.list_refs(st.cfg)] if hasattr(R, "list_refs") else []
        except Exception:  # noqa: BLE001
            refs = []
        from .factory import brain
        orders = [o for o in self._fr().list_view(st.cfg, plan=slug)[:30]]
        self._json({"plan": slug, "versions": out, "refs": refs, "pairs": brain.read_file(st.cfg, slug, "ref_pairs.json"),
                    "orders": orders, "variants": [o for o in orders if o["kind"] == "variants" and o["state"] == "ready"]})

    def api_review(self):
        """Everything waiting on you (the phone page): newest unjudged version per plan, open questions, options."""
        from .factory import bus, phases
        st = self.studio
        vi = phases.versions_index(st.cfg)
        newest = {}
        for k, v in vi.items():
            if v.get("plan") and (v.get("version") or 0) >= (newest.get(v["plan"], {}).get("version") or 0):
                newest[v["plan"]] = {**v, "key": k}
        items = []
        for plan, v in newest.items():
            cd = _read_json(v.get("card"), None) if v.get("card") else None
            items.append({"type": "version", "plan": plan, "version": v.get("version"), "theme": v.get("theme"),
                          "verdict": v.get("verdict"), "passed": v.get("passed"), "not_passed": v.get("not_passed"),
                          "render_url": f"/media/render/{quote(v['render'])}" if v.get("render") else None,
                          "final_render": v.get("final_render"), "card": cd,
                          "sheet_url": f"/media/render/{quote(cd['changed_sheet'])}" if cd and cd.get("changed_sheet") else None})
        qs = []
        for o in self._fr().list_view(st.cfg)[:200]:
            if not o["open_questions"]:
                continue
            d = self._fr().order_detail(st.cfg, o["id"])
            for q in d["status"].get("questions") or []:
                if not q.get("answered"):
                    qs.append({"order": o["id"], "plan": o["plan"], "kind": o["kind"], "version": o.get("version"), **q})
        var = []
        for o in self._fr().list_view(st.cfg)[:100]:
            if o["kind"] == "variants" and o["state"] == "ready":
                cd = _read_json(bus.order_dir(st.cfg, o["id"]) / "card.json", {}) or {}
                vv = _read_json(bus.order_dir(st.cfg, o["id"]) / "variant_verdicts.json", {}) or {}
                if len(vv) >= len(cd.get("variants") or []):
                    continue
                var.append({"order": o["id"], "plan": o["plan"], "notes": cd.get("notes"), "theme": cd.get("theme"),
                            "variants": [{**x, "url": f"/media/render/{quote(x['render'])}" if x.get("render") else None,
                                          "verdict": (vv.get(str(x["k"])) or {}).get("verdict")} for x in cd.get("variants") or []]})
        items.sort(key=lambda x: (x["verdict"] is not None, -(x["version"] or 0)))
        self._json({"items": items, "questions": qs, "variants": var, "light": st.factory_light()})

    def media_factory(self, rel):
        from .factory import bus
        self._file(_within(bus.orders_dir(self.studio.cfg), rel))

    def page_review(self):
        self._file(WEB / "review.html", cache="no-cache")

    def api_plan_version(self, slug, name):
        """A saved version's plan JSON (for Restore / Compare with draft)."""
        st = self.studio
        st.plans.check_slug(slug)
        c = PlanStore.classify(name)
        if not c or c[0] != slug or c[1] == "draft":
            raise ApiError(404, f"no saved version {name!r} of {slug}")
        d = _read_json(st.plans.dir / name, None)
        if not isinstance(d, dict):
            raise ApiError(404, f"no saved version {name!r} of {slug}")
        self._json({"file": name, "plan": d})


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
    _r("POST", r"/api/export", Handler.api_export),
    _r("POST", r"/api/export_nle", Handler.api_export_nle),
    _r("GET", r"/api/multicam", Handler.api_multicam),
    _r("GET", r"/api/cameras", Handler.api_cameras),
    _r("GET", r"/api/cameras/match", Handler.api_camera_match),
    _r("POST", rf"/api/clip/{ID}/camera", Handler.api_clip_camera),
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
    _r("POST", r"/api/comments/run", Handler.api_comments_run),
    _r("GET", r"/api/comments/run", Handler.api_comments_status),
    _r("POST", r"/api/comments/stop", Handler.api_comments_stop),
    _r("GET", r"/api/looks", Handler.api_looks),
    _r("GET", r"/api/looks/preview", Handler.api_looks_preview),
    _r("GET", r"/api/looks/sheet", Handler.api_looks_sheet),
    _r("GET", r"/api/looks/match", Handler.api_looks_match),
    _r("GET", rf"/media/look/{REST}", Handler.media_look),
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
    _r("GET", r"/api/refs", Handler.api_refs),
    _r("POST", r"/api/refs", Handler.api_refs_post),
    _r("GET", rf"/api/ref/{ID}", Handler.api_ref),
    _r("POST", rf"/api/ref/{ID}/analyze", Handler.api_ref_analyze),
    _r("GET", rf"/api/ref/{ID}/compare", Handler.api_ref_compare),
    _r("GET", rf"/media/refthumb/{ID}/(\d+)", Handler.media_refthumb),
    _r("GET", rf"/media/refsheet/{ID}/(\d+)", Handler.media_refsheet),
    _r("GET", rf"/media/refproxy/{ID}", Handler.media_refproxy),
    _r("POST", r"/api/upload", Handler.api_upload),
    _r("GET", rf"/api/upload/{ID}", Handler.api_upload_status),
    _r("GET", rf"/media/image/{REST}", Handler.media_image),
    _r("GET", r"/api/import", Handler.api_import_info),
    _r("POST", r"/api/import/locate", Handler.api_import_locate),
    _r("POST", r"/api/import/footage", Handler.api_import_footage),
    _r("GET", rf"/api/plan/{ID}/version/{ID}", Handler.api_plan_version),
    _r("POST", r"/api/factory/orders", Handler.api_factory_orders_post),
    _r("GET", r"/api/factory/orders", Handler.api_factory_orders),
    _r("GET", rf"/api/factory/orders/{ID}", Handler.api_factory_order),
    _r("GET", rf"/api/factory/orders/{ID}/role/{ID}/log", Handler.api_factory_role_log),
    _r("POST", rf"/api/factory/orders/{ID}/stop", Handler.api_factory_stop),
    _r("POST", rf"/api/factory/orders/{ID}/retry", Handler.api_factory_retry),
    _r("POST", r"/api/factory/answer", Handler.api_factory_answer),
    _r("POST", r"/api/factory/verdict", Handler.api_factory_verdict),
    _r("POST", rf"/api/factory/variants/{ID}", Handler.api_factory_variants),
    _r("POST", rf"/api/factory/apply-extension/{ID}", Handler.api_factory_apply_extension),
    _r("GET", r"/api/factory/state", Handler.api_factory_state),
    _r("POST", r"/api/factory/pause", Handler.api_factory_pause),
    _r("POST", r"/api/factory/resume", Handler.api_factory_resume),
    _r("GET", r"/api/factory/lessons", Handler.api_factory_lessons),
    _r("POST", r"/api/factory/lessons", Handler.api_factory_lessons_post),
    _r("POST", rf"/api/factory/lessons/{ID}/delete", Handler.api_factory_lesson_delete),
    _r("POST", rf"/api/factory/lessons/{ID}/supersede", Handler.api_factory_lesson_supersede),
    _r("POST", rf"/api/factory/inbox/{ID}/confirm", Handler.api_factory_inbox_confirm),
    _r("POST", rf"/api/factory/inbox/{ID}/dismiss", Handler.api_factory_inbox_dismiss),
    _r("PATCH", r"/api/factory/lessons", Handler.api_factory_lessons_patch),
    _r("GET", rf"/api/factory/brain/{ID}", Handler.api_factory_brain),
    _r("GET", rf"/api/factory/brain/{ID}/{ID}", Handler.api_factory_brain_file),
    _r("PUT", rf"/api/factory/brain/{ID}/{ID}", Handler.api_factory_brain_put),
    _r("GET", r"/api/factory/versions", Handler.api_factory_versions),
    _r("GET", r"/api/grades", Handler.api_grades),
    _r("POST", r"/api/grades", Handler.api_grades_post),
    _r("GET", r"/api/review", Handler.api_review),
    _r("GET", rf"/media/factory/{REST}", Handler.media_factory),
    _r("GET", r"/review/?", Handler.page_review),
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
    try:
        srv = StudioServer((host, port), handler)
    except Exception:
        studio.close()
        raise
    srv.studio = studio
    p_ = srv.server_address[1]
    studio.link_base = f"http://127.0.0.1:{p_}"
    if studio.factory is not None:
        studio.factory.link_base = studio.link_base
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
