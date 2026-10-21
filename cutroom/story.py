"""Story rules for voice-over edits, as measurements the planner and `plan check` can use.

A voice-over edit (a voice memo, a narration, someone's words over pictures) is cut to the WORDS, not to a
beat. Measured on a brand-film reference built on a parent's phone voice memo (docs/EDITING_RULES.md
"Voice-over edits"):
  - cuts sit on sentence breaks or pauses about half the time (8 of 14), never inside a word;
  - hands and extreme close-ups sit under the most tender lines; the brightest, most joyful shot sits under
    the peak line ("I'm so proud of you");
  - the final burst calls back earlier images (at least 3 repeats of shots seen before);
  - the act structure comes from the reference's measured section shares (noise / silence / voice / leap /
    end card), not from a default template.

Everything here is advisory: `hints(plan, con, cfg)` returns plain-language notes for `cutroom plan check`;
nothing blocks a render. `breaks(words)` is what the planner uses to place cuts: `cutroom breaks CLIP`.
"""
from . import asr

PAUSE_S = 0.30               # a gap between words at least this long is a pause (a cut may land in it)
SENTENCE_END = (".", "!", "?", "…")
CLAUSE_END = (",", ";", ":", "—", "-")
CALLBACK_MIN = 3             # a closing burst replays at least this many earlier shots
BURST_MAX_SHOT = 1.0         # montage shots this short (s) or shorter, 6 or more in a row = a burst
CALLBACK_TOL = 1.5           # a shot within this many source seconds of an earlier use of the clip is a repeat


def breaks(words, a=None, b=None):
    """Cut points between words in [a, b]: [{"t", "kind": "sentence"|"clause"|"pause", "gap", "after"}].
    t is the middle of the gap between the two words (a cut there never clips a word)."""
    ws = [w for w in words if (a is None or w[1] > a) and (b is None or w[0] < b)]
    out = []
    for (s0, e0, w0), (s1, e1, w1) in zip(ws, ws[1:]):
        gap = s1 - e0
        kind = None
        if w0.rstrip('"\')').endswith(SENTENCE_END):
            kind = "sentence"
        elif w0.rstrip('"\')').endswith(CLAUSE_END):
            kind = "clause"
        elif gap >= PAUSE_S:
            kind = "pause"
        if kind:
            out.append({"t": round((e0 + s1) / 2, 3), "kind": kind, "gap": round(gap, 3), "after": w0,
                        "end_word": round(e0, 3), "next_word": round(s1, 3)})
    return out


def inside_word(words, t, margin=0.02):
    """The word (start, end, text) that time t falls inside (with a small margin), or None."""
    for s, e, w in words:
        if s + margin < t < e - margin:
            return (s, e, w)
    return None


def nearest_break(bks, t):
    return min(bks, key=lambda x: abs(x["t"] - t)) if bks else None


def vo_cuts(plan, cfg):
    """Every voice-over boundary in the plan (broll segments with a voiceover): [{where, clip, t, edge, word,
    on_break, nearest}]."""
    rows = []
    for si, sec in enumerate(plan.get("sections") or []):
        for gi, s in enumerate(sec.get("segments") or []):
            vo = s.get("voiceover") if s.get("type") == "broll" else None
            if not isinstance(vo, dict) or vo.get("clip") is None:
                continue
            try:
                ws = asr.words(cfg, _clip_id(cfg, vo["clip"]))
            except Exception:  # noqa: BLE001
                ws = []
            if not ws:
                continue
            bks = breaks(ws)
            for edge in ("in", "out"):
                t = float(vo[edge])
                inw = inside_word(ws, t)
                nb = nearest_break(bks, t)
                rows.append({"where": f"section {si} ({sec.get('name', '')}) segment {gi}", "clip": vo["clip"], "t": t,
                             "edge": edge, "word": inw[2] if inw else None,
                             "on_break": bool(nb and abs(nb["t"] - t) <= max(0.15, nb["gap"] / 2 + 0.05)),
                             "nearest": nb})
    return rows


def _clip_id(cfg, ref):
    from . import db
    con = db.connect(cfg.db_path)
    try:
        return db.get_clip(con, ref)["id"]
    finally:
        con.close()


def callbacks(plan):
    """Closing bursts (montages of short shots in the last half of the plan) and how many of their shots repeat a
    window used earlier in the edit: [{where, shots, repeats}]."""
    used = []                     # (clip, t) of every earlier picture use
    out = []
    secs = plan.get("sections") or []
    n_segs = sum(len(s.get("segments") or []) for s in secs)
    k = 0
    for si, sec in enumerate(secs):
        for gi, s in enumerate(sec.get("segments") or []):
            k += 1
            t = s.get("type")
            if t == "montage":
                shots = s.get("shots") or []
                short = [sh for sh in shots if sh.get("dur") is None or float(sh["dur"]) <= BURST_MAX_SHOT]
                if len(shots) >= 6 and len(short) >= 0.6 * len(shots) and k > n_segs / 2:
                    rep = sum(1 for sh in shots if any(c == str(sh.get("clip")) and sh.get("in") is not None
                                                       and abs(float(sh["in"]) - u) <= CALLBACK_TOL for c, u in used))
                    out.append({"where": f"section {si} ({sec.get('name', '')}) segment {gi}", "shots": len(shots),
                                "repeats": rep})
                for sh in shots:
                    if sh.get("in") is not None:
                        used.append((str(sh.get("clip")), float(sh["in"])))
            elif t in ("broll", "talk") and s.get("in") is not None:
                used.append((str(s.get("clip")), float(s["in"])))
                for c in s.get("cutaways") or []:
                    if c.get("in") is not None:
                        used.append((str(c.get("clip")), float(c["in"])))
            elif t == "still" and s.get("t") is not None:
                used.append((str(s.get("clip")), float(s["t"])))
    return out


def act_shares(plan):
    """Planned share of runtime per section (estimate before tightening), and the reference's share when the
    section carries `ref_span` (its act in the reference)."""
    from . import plan as P
    durs = []
    for sec in plan.get("sections") or []:
        durs.append(P.estimate_duration({"sections": [sec]}))
    tot = sum(durs) or 1.0
    spans = [sec.get("ref_span") for sec in plan.get("sections") or []]
    ref_tot = sum(float(b) - float(a) for a, b in (x for x in spans if x)) if all(spans) and spans else None
    rows = []
    for sec, d, sp in zip(plan.get("sections") or [], durs, spans):
        r = {"name": sec.get("name", ""), "share": round(d / tot, 3), "seconds": round(d, 2)}
        if sp and ref_tot:
            r["ref_share"] = round((float(sp[1]) - float(sp[0])) / ref_tot, 3)
        rows.append(r)
    return rows


def hints(plan, con, cfg):
    """Plain-language story notes for `cutroom plan check` (never errors)."""
    out = []
    cuts = vo_cuts(plan, cfg)
    bad = [c for c in cuts if c["word"]]
    for c in bad:
        nb = c["nearest"]
        out.append(f"{c['where']}: voice-over {c['edge']} {c['t']:.2f}s is inside the word '{c['word']}'"
                   + (f"; nearest {nb['kind']} break {nb['t']:.2f}s (after '{nb['after']}')" if nb else ""))
    inner = [c for c in cuts if c["edge"] == "in"]
    if len(inner) >= 3:
        on = sum(1 for c in inner if c["on_break"])
        if on < 0.4 * len(inner):
            out.append(f"voice-over: {on} of {len(inner)} cuts sit on a sentence break or pause; the reference cuts "
                       "at breaks about half the time (cutroom breaks <clip> lists them)")
    for b in callbacks(plan):
        if b["repeats"] < CALLBACK_MIN:
            out.append(f"{b['where']}: a closing burst of {b['shots']} shots with {b['repeats']} callback(s); replay at "
                       f"least {CALLBACK_MIN} earlier images (same clip, same moment) so the ending remembers the film")
    for r in act_shares(plan):
        if r.get("ref_share") is not None and abs(r["share"] - r["ref_share"]) > 0.05:
            out.append(f"section '{r['name']}': {r['share'] * 100:.0f}% of the runtime vs the reference's act "
                       f"{r['ref_share'] * 100:.0f}% (ref_span)")
    return out
