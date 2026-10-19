"""The project brain: durable knowledge every role reads, kept current by the factory (plain files, editable).

    <work>/factory/brain/<slug>/
        STORY.md            beats / chapters / through-line                       (editor on request orders; editable)
        REFERENCE.md        measured "what the reference does" + the recipe        (forensics)
        ref_pairs.json      [{label, desc, ref_t, our_t}] section pairs (compare)  (forensics)
        decisions.jsonl     rulings {id, at, subject, ruling, source, supersedes?, segments?}  (intake, answers, verdicts)
        locks.json          {segment key: {at, why, version}} praised / approved segments  (intake praise, approve)
        notes_ledger.jsonl  every note ever {version, note, words, segments, change, t_out, proof, status}  (integrate)
    <work>/factory/grades.jsonl  clip grades {clip, t0, t1, grade: HERO|BROLL|NEVER, ident: ME|NOTME} (project-wide)

decisions fold newest-wins per subject; superseded rulings stay in the file (nothing is deleted).
"""
import hashlib
import json
import time
from pathlib import Path

from . import bus, fconfig

FILES = {"STORY.md": "text", "REFERENCE.md": "text", "ref_pairs.json": "json", "decisions.jsonl": "jsonl",
         "locks.json": "json", "notes_ledger.jsonl": "jsonl"}


def bdir(cfg, slug) -> Path:
    d = fconfig.factory_dir(cfg) / "brain" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def seg_key(seg) -> str:
    """Content key of a plan segment: unchanged segments keep their key across versions."""
    return hashlib.sha1(json.dumps(seg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _append(path, rec):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with bus.flocked(path.with_name("." + path.name + ".lock")):
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------------------------- decisions
def decisions(cfg, slug, active_only=True) -> list:
    rows = _jsonl(bdir(cfg, slug) / "decisions.jsonl")
    if not active_only:
        return rows
    dead = {r.get("supersedes") for r in rows if r.get("supersedes")}
    latest = {}
    for r in rows:                                    # newest wins per subject
        if r.get("id") in dead:
            continue
        latest[str(r.get("subject") or r.get("id")).strip().lower()] = r
    return [r for r in latest.values() if r.get("id") not in dead]


def add_decision(cfg, slug, subject, ruling, source, supersedes=None, segments=None, **extra) -> dict:
    rows = _jsonl(bdir(cfg, slug) / "decisions.jsonl")
    rid = f"D{len(rows) + 1:04d}"
    rec = {"id": rid, "at": bus.now_iso(), "subject": str(subject)[:200], "ruling": str(ruling)[:1000],
           "source": source, "supersedes": supersedes, "segments": segments or [], **extra}
    _append(bdir(cfg, slug) / "decisions.jsonl", rec)
    return rec


# ---------------------------------------------------------------------------------------------- locks
def locks(cfg, slug) -> dict:
    d = bus.read_json(bdir(cfg, slug) / "locks.json", {}) or {}
    return d if isinstance(d, dict) else {}


def add_locks(cfg, slug, keys, why, version=None):
    p = bdir(cfg, slug) / "locks.json"
    with bus.flocked(p.with_name(".locks.lock")):
        d = locks(cfg, slug)
        for k in keys:
            d[k] = {"at": bus.now_iso(), "why": str(why)[:300], "version": version}
        bus.write_json(p, d)
    return d


def remove_locks(cfg, slug, keys):
    p = bdir(cfg, slug) / "locks.json"
    with bus.flocked(p.with_name(".locks.lock")):
        d = locks(cfg, slug)
        for k in keys:
            d.pop(k, None)
        bus.write_json(p, d)


# ---------------------------------------------------------------------------------------------- notes ledger
def notes_ledger(cfg, slug, n=None) -> list:
    rows = _jsonl(bdir(cfg, slug) / "notes_ledger.jsonl")
    return rows[-n:] if n else rows


def add_note_rows(cfg, slug, rows):
    for r in rows:
        _append(bdir(cfg, slug) / "notes_ledger.jsonl", {"at": bus.now_iso(), **r})


# ---------------------------------------------------------------------------------------------- grades
def grades_path(cfg) -> Path:
    return fconfig.factory_dir(cfg) / "grades.jsonl"


GRADES = ("HERO", "BROLL", "NEVER")
IDENTS = ("ME", "NOTME")


def add_grade(cfg, clip, t0=None, t1=None, grade=None, ident=None, by="you") -> dict:
    if grade is not None and grade not in GRADES:
        raise ValueError(f"grade must be one of {GRADES}")
    if ident is not None and ident not in IDENTS:
        raise ValueError(f"ident must be one of {IDENTS}")
    if not clip:
        raise ValueError("clip is required")
    rec = {"at": bus.now_iso(), "clip": str(clip), "t0": None if t0 is None else round(float(t0), 3),
           "t1": None if t1 is None else round(float(t1), 3), "grade": grade, "ident": ident, "by": by}
    _append(grades_path(cfg), rec)
    return rec


def grades(cfg) -> list:
    """Folded: the latest line per (clip, t0, t1) wins per field; a later null never erases a value."""
    out = {}
    for r in _jsonl(grades_path(cfg)):
        k = (r.get("clip"), r.get("t0"), r.get("t1"))
        cur = out.setdefault(k, {"clip": k[0], "t0": k[1], "t1": k[2], "grade": None, "ident": None})
        for f in ("grade", "ident"):
            if r.get(f) is not None:
                cur[f] = r[f]
    return list(out.values())


# ---------------------------------------------------------------------------------------------- files
def read_file(cfg, slug, name):
    if name not in FILES:
        raise KeyError(name)
    p = bdir(cfg, slug) / name
    kind = FILES[name]
    if kind == "text":
        return p.read_text() if p.exists() else ""
    if kind == "json":
        return bus.read_json(p, [] if name == "ref_pairs.json" else {})
    return _jsonl(p)


def write_file(cfg, slug, name, value):
    """Validated per file. jsonl files are replaced whole (the UI edits the list)."""
    if name not in FILES:
        raise KeyError(name)
    p = bdir(cfg, slug) / name
    kind = FILES[name]
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        bus.atomic_write(p, value)
    elif kind == "json":
        if name == "ref_pairs.json":
            if not isinstance(value, list) or not all(isinstance(x, dict) and "label" in x for x in value):
                raise ValueError("ref_pairs.json must be a list of {label, desc, ref_t, our_t}")
        elif not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        bus.write_json(p, value)
    else:
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            raise ValueError(f"{name} must be a list of objects")
        bus.atomic_write(p, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in value))
    return read_file(cfg, slug, name)


def summary(cfg, slug) -> dict:
    d = bdir(cfg, slug)
    return {"slug": slug, "story": (d / "STORY.md").exists(), "reference": (d / "REFERENCE.md").exists(),
            "decisions": len(decisions(cfg, slug)), "locks": len(locks(cfg, slug)),
            "notes": len(notes_ledger(cfg, slug)), "updated": max([f.stat().st_mtime for f in d.iterdir()] or [0]),
            "t": time.time()}
