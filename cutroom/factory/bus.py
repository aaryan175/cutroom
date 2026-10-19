"""The factory bus: one folder per order under <work>/factory/orders/<id>/, status as append-only events.

    order.json      the request, written once (never edited)
    events.jsonl    append-only status lines; the ONLY status source. Every line is stamped by code:
                    {"v":1, "at", "order", "state", "phase", "role", "text", "why", "artifact", ...}
    phases/NN_name/ per-phase checkpoint (in.json, out.json, done.json) - see runner.py
    roles/<role>-<n>/ prompt.md, stream.jsonl, result.json, usage.json - see agent.run_role

States are a closed set. needs_you and blocked need a `why`. The newest line wins: an order's status is
the fold of its events (fold()). Nothing here calls a model.
"""
import calendar
import contextlib
import fcntl
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import fconfig

STATES = ("queued", "picked_up", "running", "progress", "needs_you", "ready", "blocked", "stopped", "done")
TERMINAL = ("ready", "blocked", "stopped", "done")
ACTIVE = ("picked_up", "running", "progress")         # an order the runner was driving
KINDS = ("revise", "reject", "request", "variants", "extend", "continue", "approve")
PRIORITY = {"reject": 1, "revise": 2, "continue": 2, "approve": 2, "variants": 3, "request": 4, "extend": 5}
ID_RE = re.compile(r"^o\d{8}T\d{6}Z-[0-9a-f]{4}$")
_TLOCK = threading.Lock()


class BusError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- paths
def root(cfg) -> Path:
    return fconfig.factory_dir(cfg)


def orders_dir(cfg) -> Path:
    d = root(cfg) / "orders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def order_dir(cfg, oid) -> Path:
    if not ID_RE.match(str(oid or "")):
        raise BusError(f"bad order id {oid!r}")
    return orders_dir(cfg) / oid


def now_iso(t=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def iso_to_t(s):
    try:
        return float(calendar.timegm(time.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def new_id(t=None) -> str:
    return "o" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t or time.time())) + "-" + secrets.token_hex(2)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=1, ensure_ascii=False, default=str) + "\n")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def flocked(path):
    """Exclusive flock on `path` (created). Process- and thread-safe."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------------------------- events
def append(cfg, oid, state, *, phase=None, role=None, text="", why=None, artifact=None, **extra) -> dict:
    """Append one status line (code-stamped). The only writer of events.jsonl."""
    if state not in STATES:
        raise BusError(f"state {state!r} is not one of {STATES}")
    if state in ("needs_you", "blocked") and not why:
        raise BusError(f"state {state} needs a why")
    d = order_dir(cfg, oid)
    ev = {"v": 1, "at": now_iso(), "t": round(time.time(), 3), "order": oid, "state": state, "phase": phase,
          "role": role, "text": str(text or "")[:2000], "why": (str(why)[:300] if why else None), "artifact": artifact}
    for k, v in extra.items():
        if v is not None:
            ev[k] = v
    line = json.dumps(ev, ensure_ascii=False, default=str) + "\n"
    p = d / "events.jsonl"
    with flocked(d / ".events.lock"):
        with open(p, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    return ev


def events(cfg, oid) -> list:
    p = order_dir(cfg, oid) / "events.jsonl"
    out = []
    try:
        with open(p) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def fold(evs) -> dict:
    """Status of an order from its events: the newest line wins; a progress line keeps the state 'running'."""
    st = {"state": None, "phase": None, "role": None, "text": "", "why": None, "at": None, "t": None,
          "version": None, "cost_usd": 0.0, "not_before": None, "questions": [], "folded_into": None, "folds": [],
          "limit_attempts": 0, "artifact": None, "picked": False, "started": None, "ended": None}
    for e in evs:
        s = e.get("state")
        st["state"] = "running" if s == "progress" else s
        for k in ("phase", "role", "artifact", "version"):
            if e.get(k) is not None:
                st[k] = e[k]
        st["text"] = e.get("text") or st["text"]
        st["at"], st["t"] = e.get("at"), e.get("t")
        if s in ("needs_you", "blocked"):
            st["why"] = e.get("why")
        elif s not in ("progress",):
            st["why"] = None
        if e.get("cost_total") is not None:
            st["cost_usd"] = float(e["cost_total"])
        if "not_before" in e:
            st["not_before"] = e["not_before"]
        if s == "picked_up":
            if e.get("folded_into"):
                st["folded_into"] = e["folded_into"]
            else:
                st["picked"] = True
                st["not_before"] = None
                st["started"] = st["started"] or e.get("t")
        if e.get("folds"):
            st["folds"] = list(dict.fromkeys(st["folds"] + list(e["folds"])))
        if e.get("questions") is not None:
            st["questions"] = e["questions"]
        if e.get("limit_attempts") is not None:
            st["limit_attempts"] = int(e["limit_attempts"])
        if s in TERMINAL:
            st["ended"] = e.get("t")
    return st


# ---------------------------------------------------------------------------------------------- orders
def load_order(cfg, oid) -> dict:
    d = read_json(order_dir(cfg, oid) / "order.json")
    if not isinstance(d, dict):
        raise KeyError(f"no order {oid}")
    return d


def status(cfg, oid) -> dict:
    return fold(events(cfg, oid))


def list_ids(cfg) -> list:
    return sorted(p.name for p in orders_dir(cfg).iterdir() if p.is_dir() and ID_RE.match(p.name))


def list_orders(cfg, plan=None, state=None) -> list:
    out = []
    for oid in list_ids(cfg):
        try:
            o = load_order(cfg, oid)
        except KeyError:
            continue
        if plan and o.get("plan") != plan:
            continue
        st = status(cfg, oid)
        if state and st["state"] != state:
            continue
        out.append({**o, "status": st})
    return out


def create(cfg, kind, plan, *, comments=(), text="", attachments=(), ref=None, variants=None, by=None,
           parent=None, priority=None, budget=None, not_before=None, extra=None) -> dict:
    """Write order.json (once) + the first `queued` line. Returns the order dict."""
    if kind not in KINDS:
        raise BusError(f"kind must be one of {KINDS}")
    fc = fconfig.load(cfg)
    b = dict(fc["budgets"])
    b.update(budget or {})
    oid = new_id()
    while (orders_dir(cfg) / oid).exists():
        time.sleep(0.01)
        oid = new_id()
    o = {"v": 1, "id": oid, "kind": kind, "plan": plan, "comments": list(dict.fromkeys(comments or [])),
         "text": str(text or "")[:8000], "attachments": list(attachments or []), "ref": ref, "variants": variants,
         "by": by or {"user": "you", "via": "studio"}, "parent": parent,
         "priority": int(priority or PRIORITY.get(kind, 4)), "created": now_iso(), "created_t": round(time.time(), 3),
         "budget": {"wall_s": b.get("wall_s"), "wall_s_large": b.get("wall_s_large"), "role_runs": b.get("role_runs"),
                    "cost_usd": b.get("cost_usd")}}
    o.update(extra or {})
    d = orders_dir(cfg) / oid
    d.mkdir(parents=True)
    write_json(d / "order.json", o)
    append(cfg, oid, "queued", text=f"{kind} order queued", not_before=not_before)
    return o


def intake_lock(cfg):
    return flocked(root(cfg) / ".intake.lock")


def submit(cfg, kind, plan, *, comments=(), text="", attachments=(), by=None, debounce_s=None, **kw) -> dict:
    """Create an order, folding a revise into a queued (not yet picked up) revise of the same plan sent
    within the debounce window. Returns {order, folded_into?}."""
    fc = fconfig.load(cfg)
    deb = float(fc["debounce_s"] if debounce_s is None else debounce_s)
    now = time.time()
    with intake_lock(cfg):
        if kind == "revise" and deb > 0:
            for o in reversed(list_orders(cfg, plan=plan)):
                st = o["status"]
                if o["kind"] != "revise" or st["state"] != "queued" or st["picked"] or st["folded_into"]:
                    continue
                last = max(float(o.get("created_t") or 0), float(st.get("t") or 0))
                if now - last > deb:
                    continue
                new = create(cfg, kind, plan, comments=comments, text=text, attachments=attachments, by=by,
                             parent=o["id"], **kw)
                append(cfg, new["id"], "picked_up", text=f"folded into {o['id']}", folded_into=o["id"])
                append(cfg, o["id"], "queued", text=f"folded in {len(new['comments'])} more note(s)",
                       folds=[new["id"]], not_before=now_iso(now + deb))
                return {"order": load_order(cfg, o["id"]), "folded": new["id"], "folded_into": o["id"]}
        nb = now_iso(now + deb) if (kind == "revise" and deb > 0) else None
        o = create(cfg, kind, plan, comments=comments, text=text, attachments=attachments, by=by, not_before=nb, **kw)
        return {"order": o}


def order_comments(cfg, oid) -> list:
    """The order's comment ids plus those of every order folded into it, in order."""
    o = load_order(cfg, oid)
    ids = list(o.get("comments") or [])
    texts = [o.get("text")] if o.get("text") else []
    for f in status(cfg, oid).get("folds") or []:
        try:
            fo = load_order(cfg, f)
        except KeyError:
            continue
        ids += fo.get("comments") or []
        if fo.get("text"):
            texts.append(fo["text"])
    return list(dict.fromkeys(ids)), texts


# ---------------------------------------------------------------------------------------------- versions
VERSION_RE = re.compile(r"^(?P<slug>.+)_v(?P<n>\d+)\.json$")


def reserve_version(cfg, slug) -> int:
    """Next version number for `slug`, reserved under a lock so two orders never collide. A failed order's
    number is skipped (the card says so); it is never reused."""
    plans = Path(cfg.work) / "plans"
    with flocked(root(cfg) / ".versions.lock"):
        res = read_json(root(cfg) / "reservations.json", {}) or {}
        n = max([0] + [int(x) for x in res.get(slug, [])])
        if plans.is_dir():
            for e in os.scandir(plans):
                m = VERSION_RE.match(e.name)
                if m and m.group("slug") == slug:
                    n = max(n, int(m.group("n")))
        n += 1
        res.setdefault(slug, []).append(n)
        res[slug] = res[slug][-200:]
        write_json(root(cfg) / "reservations.json", res)
        return n


def latest_version(cfg, slug):
    """(file name, n) of the newest saved version of slug, or (None, 0)."""
    plans = Path(cfg.work) / "plans"
    best = (None, 0)
    if plans.is_dir():
        for e in os.scandir(plans):
            m = VERSION_RE.match(e.name)
            if m and m.group("slug") == slug and int(m.group("n")) > best[1]:
                best = (e.name, int(m.group("n")))
    return best
