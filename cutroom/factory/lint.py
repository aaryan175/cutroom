"""Declarative checks learned from lessons (checks.json), run on every later version (machine QA "lint").

    <work>/factory/checks.json      project checks   {"checks": [{id, type, params, lesson, status, why}]}
    ~/.cutroom/checks.json          global checks    (same shape; CUTROOM_GLOBAL_DIR moves ~/.cutroom)

Check types (params):
    banned_clip {clip}                         the clip may not appear anywhere
    banned_span {clip, t0, t1}                 that source span may not appear
    max_broll_pct {pct, section?}              cutaway cover over talk, in % of talk time
    forbid_overlay_kind {kind}                 no overlay / card of that kind
    forbid_transition {kind}                   no transition of that kind
    max_count {kind: sfx|overlay|cutaway, max}
    no_music_under_speech {}                   style.music_under_speech must be "mute"
    caption_forbid {text}                      no caption contains this text (a mis-heard word)
    cutaway_context {forbid, when_words}       no cutaway tagged with `forbid` words while the talk says `when_words`
    qa_metric {check, worst: pass|warn}        a qa.py check may not be worse than `worst`
Clip grades (grades.jsonl) add implicit checks: NEVER bans that clip / span; NOTME on a talk shot warns.
"""
import json
import os
import re
from pathlib import Path

from . import brain, bus, fconfig

TYPES = ("banned_clip", "banned_span", "max_broll_pct", "forbid_overlay_kind", "forbid_transition", "max_count",
         "no_music_under_speech", "caption_forbid", "cutaway_context", "qa_metric")
SEV = {"pass": 0, "info": 0, "warn": 1, "fail": 2}


def global_dir() -> Path:
    return Path(os.environ.get("CUTROOM_GLOBAL_DIR") or (Path.home() / ".cutroom"))


def checks_path(cfg=None, scope="project") -> Path:
    return (fconfig.factory_dir(cfg) if scope == "project" else global_dir()) / "checks.json"


def load_checks(cfg, scope=None) -> list:
    out = []
    for sc in (("project", "global") if scope is None else (scope,)):
        d = bus.read_json(checks_path(cfg, sc), {}) or {}
        for c in (d.get("checks") if isinstance(d, dict) else d) or []:
            if isinstance(c, dict) and c.get("type") in TYPES and c.get("status", "active") == "active":
                out.append({**c, "scope": sc})
    return out


def validate_check(c) -> list:
    errs = []
    if not isinstance(c, dict) or c.get("type") not in TYPES:
        return [f"check type must be one of {TYPES}"]
    p = c.get("params") or {}
    need = {"banned_clip": ["clip"], "banned_span": ["clip", "t0", "t1"], "max_broll_pct": ["pct"],
            "forbid_overlay_kind": ["kind"], "forbid_transition": ["kind"], "max_count": ["kind", "max"],
            "caption_forbid": ["text"], "cutaway_context": ["forbid", "when_words"], "qa_metric": ["check"]}
    for k in need.get(c["type"], []):
        if p.get(k) in (None, ""):
            errs.append(f"{c['type']} needs params.{k}")
    return errs


def add_check(cfg, check, scope="project") -> dict:
    errs = validate_check(check)
    if errs:
        raise ValueError("; ".join(errs))
    p = checks_path(cfg, scope)
    with bus.flocked(p.with_name(".checks.lock")):
        d = bus.read_json(p, {}) or {}
        cs = d.get("checks", []) if isinstance(d, dict) else []
        cid = check.get("id") or f"{'C' if scope == 'project' else 'G'}{len(cs) + 1:04d}"
        rec = {"id": cid, "type": check["type"], "params": check.get("params") or {}, "lesson": check.get("lesson"),
               "why": str(check.get("why") or "")[:300], "status": "active", "at": bus.now_iso()}
        cs = [x for x in cs if x.get("id") != cid] + [rec]
        bus.write_json(p, {"checks": cs})
    return rec


def _pieces(tl, track):
    return [p for p in tl.get("pieces") or [] if p.get("track") == track]


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def run(cfg, plan, tl, qa_report=None, con=None, checks=None) -> list:
    """[{id, type, status, msg, t, lesson, scope}] for every active check (+ implicit grade checks)."""
    checks = load_checks(cfg) if checks is None else checks
    out = []
    v1, v2, v3 = _pieces(tl, "V1"), _pieces(tl, "V2"), _pieces(tl, "V3")
    pics = v1 + v2

    def res(c, status, msg, t=None):
        out.append({"id": c.get("id"), "type": c.get("type"), "status": status, "msg": msg,
                    "t": None if t is None else round(float(t), 2), "lesson": c.get("lesson"), "scope": c.get("scope")})

    for c in checks:
        p, ty = c.get("params") or {}, c.get("type")
        try:
            if ty in ("banned_clip", "banned_span"):
                hits = []
                for x in pics:
                    if not x.get("clip") or str(x["clip"]) != str(p["clip"]):
                        continue
                    if ty == "banned_span" and (x.get("in") is None or _overlap(x["in"], x["out"] or x["in"],
                                                                                 float(p["t0"]), float(p["t1"])) <= 0):
                        continue
                    hits.append(x)
                res(c, "fail" if hits else "pass", (f"{p['clip']} used at {hits[0]['start']:.1f}s ({len(hits)} piece(s))"
                                                     if hits else f"{p['clip']} not used"), hits[0]["start"] if hits else None)
            elif ty == "max_broll_pct":
                talk = [x for x in v1 if x.get("type") == "talk" and (not p.get("section") or
                                                                     _secname(tl, x) == p["section"])]
                tt = sum(x["end"] - x["start"] for x in talk) or 0.0
                cov = sum(_overlap(x["start"], x["end"], y["start"], y["end"]) for x in talk for y in v2)
                pct = 100.0 * cov / tt if tt else 0.0
                res(c, "fail" if pct > float(p["pct"]) else "pass", f"cutaways cover {pct:.1f}% of talk (max {p['pct']}%)")
            elif ty == "forbid_overlay_kind":
                hits = [x for x in v3 if str(x.get("kind") or "") == str(p["kind"])]
                res(c, "fail" if hits else "pass", f"{len(hits)} '{p['kind']}' overlay(s)", hits[0]["start"] if hits else None)
            elif ty == "forbid_transition":
                trs = [t for t in tl.get("transitions") or [] if str(t.get("kind") or "") == str(p["kind"])]
                trs += [x for x in v1 if isinstance(x.get("transition_in"), dict) and
                        x["transition_in"].get("kind") == p["kind"]]
                res(c, "fail" if trs else "pass", f"{len(trs)} '{p['kind']}' transition(s)",
                    (trs[0].get("t", trs[0].get("start")) if trs else None))
            elif ty == "max_count":
                k = p["kind"]
                n = len(tl.get("sfx") or []) if k == "sfx" else len(v3) if k == "overlay" else len(v2) if k == "cutaway" else 0
                res(c, "fail" if n > int(p["max"]) else "pass", f"{n} {k} (max {p['max']})")
            elif ty == "no_music_under_speech":
                mode = ((plan.get("style") or {}).get("music_under_speech") or "mute")
                res(c, "pass" if mode == "mute" else "fail", f"music under speech: {mode}")
            elif ty == "caption_forbid":
                bad = str(p["text"]).lower()
                hits = [(x["start"], cc.get("text")) for x in v1 for cc in x.get("captions") or []
                        if bad in str(cc.get("text") or "").lower()]
                res(c, "fail" if hits else "pass", f"caption says “{hits[0][1]}”" if hits else "no such caption",
                    hits[0][0] if hits else None)
            elif ty == "cutaway_context":
                hits = _cutaway_context(cfg, con, v1, v2, p)
                res(c, "fail" if hits else "pass", hits[0][1] if hits else "no cutaway against the words",
                    hits[0][0] if hits else None)
            elif ty == "qa_metric":
                st = ((qa_report or {}).get("summary") or {}).get("checks", {}).get(p["check"])
                worst = p.get("worst", "warn")
                if st is None:
                    res(c, "info", f"qa check {p['check']} not measured")
                else:
                    res(c, "fail" if SEV.get(st, 0) > SEV.get(worst, 1) else "pass", f"qa {p['check']}: {st}")
        except (KeyError, TypeError, ValueError) as e:
            res(c, "warn", f"check could not run: {e}")
    # implicit: clip grades
    for g in brain.grades(cfg):
        if g.get("grade") == "NEVER":
            c = {"id": f"grade:{g['clip']}", "type": "banned_span" if g.get("t0") is not None else "banned_clip",
                 "params": {"clip": g["clip"], "t0": g.get("t0"), "t1": g.get("t1")}, "scope": "grades"}
            hits = [x for x in pics if x.get("clip") == g["clip"] and (g.get("t0") is None or (
                x.get("in") is not None and _overlap(x["in"], x["out"] or x["in"], g["t0"], g["t1"]) > 0))]
            if hits:
                res(c, "fail", f"{g['clip']} is graded NEVER but is used at {hits[0]['start']:.1f}s", hits[0]["start"])
        elif g.get("ident") == "NOTME":
            c = {"id": f"notme:{g['clip']}", "type": "grade", "scope": "grades"}
            hits = [x for x in v1 if x.get("clip") == g["clip"] and x.get("type") == "talk"]
            if hits:
                res(c, "warn", f"{g['clip']} is graded NOT ME but is a talking shot at {hits[0]['start']:.1f}s",
                    hits[0]["start"])
    return out


def _secname(tl, x):
    for s in tl.get("sections") or []:
        if s.get("i", s.get("index")) == x.get("sec"):
            return s.get("name")
    return None


def _cutaway_context(cfg, con, v1, v2, p):
    if con is None:
        return []
    from .. import db
    forbid = [w.lower() for w in (p["forbid"] if isinstance(p["forbid"], list) else [p["forbid"]])]
    when = re.compile("|".join(re.escape(w) for w in (p["when_words"] if isinstance(p["when_words"], list)
                                                      else [p["when_words"]])), re.I)
    from .mapnotes import _words_in
    out = []
    for y in v2:
        talk = next((x for x in v1 if x["start"] <= y["start"] < x["end"] and x.get("clip")), None)
        if not talk or not y.get("clip"):
            continue
        a = talk["in"] + (y["start"] - talk["start"])
        words = _words_in(cfg, talk["clip"], a, a + (y["end"] - y["start"]))
        if not when.search(words or ""):
            continue
        try:
            tags = json.dumps(db.get_tags(con, y["clip"]) or {}).lower()
        except Exception:  # noqa: BLE001
            continue
        if any(f in tags for f in forbid):
            out.append((y["start"], f"cutaway {y.get('clip_rel') or y['clip']} ({', '.join(forbid)}) over “{words[:60]}”"))
    return out
