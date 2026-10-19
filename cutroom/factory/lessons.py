"""The learning loop: every rejection, ruling and critic HARD finding becomes an inbox item; the lesson-keeper
triages them into lessons; lessons that can be measured become declarative checks (lint.py).

    <work>/factory/lessons.json     project lessons {lessons: [...], inbox: [...], seen: [...]}
    ~/.cutroom/lessons.json         global lessons (seeded once from docs/EDITING_RULES.md; CUTROOM_GLOBAL_DIR)

Lesson: {id: P0012 | G0003 | R017, scope, at, source, symptom, rule, check: <check id> | None, tags, status:
active | superseded, superseded_by, provenance}. Inbox item: {id: I0007, key, at, order, source, kind, text,
tags, provenance, status: open | unconfirmed | triaged, lesson?, reason?}.

PROVENANCE (the real run taught false lessons from QA bugs: "never add a film look" came from a broken
rule-38 check, not from the creator). A lesson is only ever created from the CREATOR: a reject reason, a
NOTES text, a comment (note / restore / the same spot noted again), an answer, or a critic / machine item
the creator CONFIRMED (`confirm_item`). Critic findings and machine-QA fails land in the inbox as
`unconfirmed`: they are shown, never triaged into a lesson, and `apply_ops` refuses to turn them into one.
    provenance = {kind: creator_note | creator_reject | creator_answer | critic | machine | operator | rules,
                  creator: bool, confirmed: bool, by?, at?}
`supersede` and `delete` retire a lesson AND its learned check (a check whose lesson is gone must not keep
failing versions: C0002 kept the poisoned P0002 alive in lint).

Nothing here blocks a version: triage runs at the end of an order over that order's items only (at most 10),
and an untriaged backlog only shows as a count. Roles get a SCOPED brief (brief()): every active project
decision and lesson + the global lessons whose tags match this order's changes + a small fixed core, newest
first, at most 60 rules. Superseded rules are never shown.
"""
import hashlib
import json
import re
from pathlib import Path

from . import brain, bus, fconfig, lint

BRIEF_CAP = 60
ACTION_TAGS = {"remove": ["pace", "talk", "story"], "trim": ["pace", "talk"], "swap": ["broll", "quality"],
               "restore": ["process", "story"], "caption_text": ["captions"], "text": ["captions", "graphics"],
               "reorder": ["story"], "add": ["broll", "story"], "music": ["music", "audio"], "audio": ["audio"],
               "graphics": ["graphics"], "style": ["grade", "graphics"], "new_capability": ["process"],
               "new_edit": ["story", "pace", "broll", "intro"], "none": []}
SECTION_TAGS = [("story", ["story"]), ("talking", ["talk", "pace"]), ("b-roll", ["broll"]), ("montage", ["montage", "pace"]),
                ("match cut", ["transitions"]), ("intro", ["intro"]), ("caption", ["captions"]),
                ("graphic", ["graphics"]), ("music", ["music", "audio"]), ("voice", ["audio"]),
                ("shot quality", ["quality", "broll"]), ("pacing", ["pace"]), ("review", ["process"]), ("craft", ["craft"])]


def _path(cfg, scope):
    return (fconfig.factory_dir(cfg) / "lessons.json") if scope == "project" else (lint.global_dir() / "lessons.json")


def load(cfg, scope="project") -> dict:
    if scope == "global":
        ensure_global_seed()
    d = bus.read_json(_path(cfg, scope), {}) or {}
    if not isinstance(d, dict):
        d = {}
    for k in ("lessons", "inbox", "seen"):
        d.setdefault(k, [])
    return d


def _save(cfg, scope, d):
    bus.write_json(_path(cfg, scope), d)


def _lock(cfg, scope):
    return bus.flocked(_path(cfg, scope).with_name(".lessons.lock"))


# ---------------------------------------------------------------------------------------------- seed
def parse_rules(md_text) -> list:
    """docs/EDITING_RULES.md -> [{id: R###, rule, why, check_text, tags}]"""
    out, tags = [], ["general"]
    rx = re.compile(r"^(\d+)\.\s+\*\*(.+?)\*\*")
    lines = md_text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            h = ln[3:].lower()
            tags = next((t for k, t in SECTION_TAGS if k in h), ["general"])
            continue
        m = rx.match(ln.strip())
        if not m:
            continue
        why = chk = ""
        for nxt in lines[i + 1:i + 5]:
            s = nxt.strip()
            if s.startswith("Why:"):
                why = s[4:].strip()
            elif s.startswith("Check:"):
                chk = s[6:].strip()
        out.append({"id": f"R{int(m.group(1)):03d}", "rule": m.group(2).strip(), "why": why, "check_text": chk,
                    "tags": list(tags)})
    return out


def ensure_global_seed():
    p = lint.global_dir() / "lessons.json"
    if p.exists():
        return
    md = Path(__file__).resolve().parents[2] / "docs" / "EDITING_RULES.md"
    try:
        rules = parse_rules(md.read_text())
    except OSError:
        rules = []
    les = [{"id": r["id"], "scope": "global", "at": bus.now_iso(), "source": "docs/EDITING_RULES.md",
            "symptom": r["why"], "rule": r["rule"], "check": None, "check_text": r["check_text"], "tags": r["tags"],
            "status": "active", "superseded_by": None} for r in rules]
    p.parent.mkdir(parents=True, exist_ok=True)
    with bus.flocked(p.with_name(".lessons.lock")):
        if not p.exists():
            bus.write_json(p, {"lessons": les, "inbox": [], "seen": [], "seeded_from": str(md)})


# ---------------------------------------------------------------------------------------------- provenance
CREATOR_KINDS = {"reject": "creator_reject", "notes": "creator_note", "note": "creator_note", "restore": "creator_note",
                 "repeat_note": "creator_note", "answer": "creator_answer", "ruling": "creator_answer",
                 "operator": "operator"}
MACHINE_KINDS = {"machine_fail": "machine", "lint_fail": "machine", "critic_hard": "critic", "critic": "critic"}


def provenance_of(item) -> dict:
    """Where an inbox signal came from, and whether it may become a lesson."""
    pv = item.get("provenance")
    if isinstance(pv, dict) and pv.get("kind"):
        return {**pv, "creator": bool(pv.get("creator")), "confirmed": bool(pv.get("confirmed") or pv.get("creator"))}
    kind = str(item.get("kind") or "")
    src = str(item.get("source") or "")
    if kind in CREATOR_KINDS or src.startswith(("verdict:", "comment:", "answer:", "operator")):
        k = CREATOR_KINDS.get(kind) or ("creator_reject" if kind == "reject" else "creator_note")
        return {"kind": k, "creator": True, "confirmed": True}
    k = MACHINE_KINDS.get(kind) or ("critic" if src.startswith("critic:") else "machine")
    return {"kind": k, "creator": False, "confirmed": False}


def eligible(item) -> bool:
    """May this inbox item become a lesson? Only the creator's own words, or a machine/critic item the creator
    confirmed."""
    pv = provenance_of(item)
    return bool(pv["creator"] or pv["confirmed"])


def _next_id(rows, prefix):
    n = 0
    for x in rows:
        m = re.match(rf"{prefix}(\d+)$", str(x.get("id") or ""))
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1:04d}"


# ---------------------------------------------------------------------------------------------- harvest
def harvest(cfg, items, order=None) -> list:
    """Add signals to the project inbox (idempotent by key). items: [{source, kind, text, tags?, provenance?}]
    -> new rows. Creator signals are `open` (triage may make them lessons); critic / machine signals are
    `unconfirmed` until the creator confirms them."""
    new = []
    with _lock(cfg, "project"):
        d = load(cfg, "project")
        seen = set(d["seen"])
        for it in items:
            text = " ".join(str(it.get("text") or "").split())
            if not text:
                continue
            key = f"{it.get('source')}::{it.get('kind')}::{text[:80]}"
            if key in seen:
                continue
            seen.add(key)
            pv = provenance_of(it)
            row = {"id": _next_id(d["inbox"], "I"), "key": key, "at": bus.now_iso(), "order": order or it.get("order"),
                   "source": it.get("source"), "kind": it.get("kind"), "text": text[:1500],
                   "tags": list(it.get("tags") or []), "provenance": pv,
                   "status": "open" if eligible({**it, "provenance": pv}) else "unconfirmed"}
            d["inbox"].append(row)
            new.append(row)
        d["seen"] = sorted(seen)[-5000:]
        _save(cfg, "project", d)
    return new


def inbox(cfg, order=None, open_only=True) -> list:
    """Items the lesson-keeper may triage (open AND eligible); open_only=False lists everything."""
    rows = load(cfg, "project")["inbox"]
    return [r for r in rows if (not open_only or (r.get("status") == "open" and eligible(r)))
            and (order is None or r.get("order") == order)]


def unconfirmed(cfg, order=None) -> list:
    return [r for r in load(cfg, "project")["inbox"] if r.get("status") == "unconfirmed"
            and (order is None or r.get("order") == order)]


def confirm_item(cfg, iid, by="operator", text=None) -> dict:
    """The creator confirms a critic / machine finding ("yes, that is a real problem"): it becomes eligible
    for triage. `text` may restate it in the creator's words."""
    with _lock(cfg, "project"):
        d = load(cfg, "project")
        item = next((r for r in d["inbox"] if r["id"] == iid), None)
        if item is None:
            raise KeyError(f"no inbox item {iid}")
        pv = provenance_of(item)
        item["provenance"] = {**pv, "confirmed": True, "by": by, "at": bus.now_iso()}
        if text:
            item["text"] = " ".join(str(text).split())[:1500]
        if item.get("status") == "unconfirmed":
            item["status"] = "open"
        _save(cfg, "project", d)
    return item


def dismiss_item(cfg, iid, reason="not a real problem") -> dict:
    with _lock(cfg, "project"):
        d = load(cfg, "project")
        item = next((r for r in d["inbox"] if r["id"] == iid), None)
        if item is None:
            raise KeyError(f"no inbox item {iid}")
        item.update(status="triaged", reason=str(reason)[:300], dismissed=True)
        _save(cfg, "project", d)
    return item


def apply_ops(cfg, ops, order=None) -> list:
    """Apply the lesson-keeper's ops: add / merge / supersede / none. Returns what was done."""
    done = []
    with _lock(cfg, "project"):
        d = load(cfg, "project")
        by_in = {r["id"]: r for r in d["inbox"]}
        by_les = {x["id"]: x for x in d["lessons"]}
        for op in ops or []:
            kind = op.get("op")
            item = by_in.get(op.get("inbox"))
            if item is None or item.get("status") not in ("open", "unconfirmed"):
                continue
            if item.get("status") == "unconfirmed" and kind == "none":
                continue                                # waits for the creator, not for the keeper
            if kind in ("add", "supersede", "merge") and not eligible(item):
                done.append({"op": "refused", "inbox": item["id"],
                             "why": "not from the creator and not confirmed by the creator"})
                continue
            if kind == "none":
                item.update(status="triaged", reason=str(op.get("reason") or "no lesson")[:300])
                done.append({"op": "none", "inbox": item["id"]})
                continue
            if kind == "merge" and op.get("lesson") in by_les:
                les = by_les[op["lesson"]]
                les.setdefault("also", []).append(item["text"][:300])
                item.update(status="triaged", lesson=les["id"])
                done.append({"op": "merge", "inbox": item["id"], "lesson": les["id"]})
                continue
            if kind in ("add", "supersede"):
                if not op.get("rule"):
                    continue
                lid = _next_id(d["lessons"] + d.get("deleted", []), "P")
                les = {"id": lid, "scope": "project", "at": bus.now_iso(), "source": item.get("source"),
                       "symptom": str(op.get("symptom") or item["text"])[:600], "rule": str(op["rule"])[:600],
                       "check": None, "tags": list(op.get("tags") or item.get("tags") or []), "status": "active",
                       "superseded_by": None, "order": order,
                       "provenance": {**provenance_of(item), "inbox": item["id"], "words": item["text"][:300]}}
                chk = op.get("check_json")
                if chk:
                    try:
                        c = json.loads(chk) if isinstance(chk, str) else chk
                        rec = lint.add_check(cfg, {**c, "lesson": lid, "why": les["rule"]})
                        les["check"] = rec["id"]
                    except (ValueError, TypeError) as e:
                        les["check_error"] = str(e)[:200]
                if kind == "supersede" and op.get("lesson") in by_les:
                    old = by_les[op["lesson"]]
                    old.update(status="superseded", superseded_by=lid, superseded_at=bus.now_iso())
                    _retire_check(cfg, old.get("check"), "project", f"lesson {old['id']} superseded by {lid}")
                d["lessons"].append(les)
                by_les[lid] = les
                item.update(status="triaged", lesson=lid)
                done.append({"op": kind, "inbox": item["id"], "lesson": lid, "check": les["check"]})
        _save(cfg, "project", d)
    return done


def add_lesson(cfg, rule, symptom="", tags=(), check=None, source="operator", scope="project") -> dict:
    """A lesson the creator writes by hand (Lessons tab / CLI): creator provenance by definition."""
    with _lock(cfg, scope):
        d = load(cfg, scope)
        lid = _next_id(d["lessons"] + d.get("deleted", []), "P" if scope == "project" else "G")
        les = {"id": lid, "scope": scope, "at": bus.now_iso(), "source": source, "symptom": symptom, "rule": rule,
               "check": None, "tags": list(tags), "status": "active", "superseded_by": None,
               "provenance": {"kind": "operator", "creator": True, "confirmed": True}}
        if check:
            les["check"] = lint.add_check(cfg, {**check, "lesson": lid, "why": rule}, scope=scope)["id"]
        d["lessons"].append(les)
        _save(cfg, scope, d)
    return les


def _retire_check(cfg, cid, scope, why):
    """Deactivate a learned check (kept in checks.json with status retired, so the history stays readable)."""
    if not cid:
        return None
    p = lint.checks_path(cfg, scope)
    with bus.flocked(p.with_name(".checks.lock")):
        d = bus.read_json(p, {}) or {}
        cs = d.get("checks", []) if isinstance(d, dict) else []
        hit = None
        for c in cs:
            if c.get("id") == cid:
                c.update(status="retired", retired_at=bus.now_iso(), retired_why=str(why)[:300])
                hit = c
        if hit is not None:
            bus.write_json(p, {**(d if isinstance(d, dict) else {}), "checks": cs})
    return hit


def supersede(cfg, lid, by=None, scope="project", reason=None) -> dict:
    """Retire a lesson (and its check). `by` names the lesson that replaces it, if any."""
    with _lock(cfg, scope):
        d = load(cfg, scope)
        les = next((x for x in d["lessons"] if x["id"] == lid), None)
        if les is None:
            raise KeyError(f"no lesson {lid}")
        if by and not any(x["id"] == by for x in d["lessons"]):
            raise KeyError(f"no lesson {by} to supersede with")
        les.update(status="superseded", superseded_by=by, superseded_at=bus.now_iso())
        if reason:
            les["superseded_why"] = str(reason)[:300]
        _save(cfg, scope, d)
    _retire_check(cfg, les.get("check"), scope, f"lesson {lid} superseded" + (f" by {by}" if by else ""))
    return les


def delete(cfg, lid, scope="project", reason=None) -> dict:
    """Remove a lesson from the law (a wrong one: e.g. taught by a QA bug). It moves to `deleted` (kept for the
    record, never shown in a brief), its check is retired, and the inbox items that pointed at it are marked."""
    with _lock(cfg, scope):
        d = load(cfg, scope)
        les = next((x for x in d["lessons"] if x["id"] == lid), None)
        if les is None:
            raise KeyError(f"no lesson {lid}")
        d["lessons"] = [x for x in d["lessons"] if x["id"] != lid]
        rec = {**les, "status": "deleted", "deleted_at": bus.now_iso(), "deleted_why": str(reason or "")[:300]}
        d.setdefault("deleted", []).append(rec)
        for x in d["lessons"]:                       # a lesson superseded BY the deleted one comes back
            if x.get("superseded_by") == lid and x.get("status") == "superseded":
                x.update(status="active", superseded_by=None)
        for r in d["inbox"]:
            if r.get("lesson") == lid:
                r["lesson_deleted"] = True
        _save(cfg, scope, d)
    _retire_check(cfg, les.get("check"), scope, f"lesson {lid} deleted" + (f": {reason}" if reason else ""))
    return rec


# ---------------------------------------------------------------------------------------------- brief
def tags_for(changes) -> set:
    out = set()
    for ch in changes or []:
        out.update(ACTION_TAGS.get(ch.get("action"), []))
        out.update(ch.get("tags") or [])
    return out


def active(cfg) -> list:
    rows = [x for x in load(cfg, "project")["lessons"] if x.get("status") == "active"]
    rows += [x for x in load(cfg, "global")["lessons"] if x.get("status") == "active"]
    return rows


def brief(cfg, slug=None, tags=(), cap=BRIEF_CAP) -> dict:
    """{text, rules: [ids], n_project, n_global} for a role prompt."""
    tags = set(tags or [])
    proj = [x for x in load(cfg, "project")["lessons"] if x.get("status") == "active"]
    glob = [x for x in load(cfg, "global")["lessons"] if x.get("status") == "active"
            and (not tags or set(x.get("tags") or []) & tags)]
    proj.sort(key=lambda x: x.get("at") or "", reverse=True)
    glob.sort(key=lambda x: (x.get("at") or "", x.get("id")), reverse=True)
    lines, ids = [], []
    decs = brain.decisions(cfg, slug) if slug else []
    if decs:
        lines.append("### Rulings for this project (newest wins; they beat every rule below)")
        for r in sorted(decs, key=lambda r: r.get("at") or "", reverse=True)[:cap]:
            lines.append(f"- [{r['id']}] {r.get('subject')}: {r.get('ruling')}")
    room = max(0, cap - len(decs))
    pick = (proj + glob)[:room]
    if pick:
        lines.append("### Lessons (project first, then house rules matching this order)")
        for x in pick:
            ids.append(x["id"])
            chk = f" (machine check {x['check']})" if x.get("check") else ""
            lines.append(f"- [{x['id']}] {x['rule']}{chk}")
    return {"text": "\n".join(lines), "rules": ids, "n_project": len([x for x in pick if x["scope"] == "project"]),
            "n_global": len([x for x in pick if x["scope"] == "global"]), "n_decisions": len(decs)}


def lessons_sha(cfg) -> str:
    les = sorted((x["id"], x["rule"], x.get("check")) for x in active(cfg))
    chks = sorted((c.get("id"), c.get("type"), json.dumps(c.get("params"), sort_keys=True)) for c in lint.load_checks(cfg))
    return hashlib.sha1(json.dumps([les, chks]).encode()).hexdigest()[:16]
