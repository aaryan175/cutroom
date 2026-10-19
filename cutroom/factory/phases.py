"""The pipeline phases. Code phases are deterministic; role phases are one agent run each (roles.py).

    revise / reject / continue / request:
        intake -> producer -> [forensics] -> [extend] -> edit -> render -> qa -> critic -> [fix -> render2 -> qa2
        -> verify] -> integrate -> (lessons, silent)
    variants: intake -> producer -> variants_edit -> variants_render -> variants_integrate
    approve:  approve_intake -> render_final -> qa_final -> approve_record     (no agent; never publishes)
    extend:   extend_stub                                                      (the extender is a stub)

A phase function takes the OrderCtx and returns its out dict (written to phases/NN_name/out.json), or
raises Skip / NeedsYou / Blocked / Requeue / Stopped (runner.py turns those into bus states).
"""
import copy
import json
import re
import shutil
import time
from pathlib import Path

from . import brain, bus, card, claims, gates, lessons, lint, lockcheck, mapnotes, roles

TEXT_ACTIONS = {"caption_text", "text"}


class Skip(Exception):
    pass


class NeedsYou(Exception):
    def __init__(self, why, questions=(), kind="taste", text=""):
        super().__init__(why)
        self.why, self.questions, self.kind, self.text = why, list(questions), kind, text


class Blocked(Exception):
    pass


class Requeue(Exception):
    def __init__(self, why, delay_s):
        super().__init__(why)
        self.why, self.delay_s = why, delay_s


class Stopped(Exception):
    pass


PLANS = {
    "revise": ["intake", "producer", "forensics", "extend", "edit", "render", "qa", "critic", "fix", "render2", "qa2",
               "verify", "integrate", "lessons"],
    "variants": ["intake", "producer", "variants_edit", "variants_render", "variants_integrate"],
    "approve": ["approve_intake", "render_final", "qa_final", "approve_record"],
    "extend": ["extend_stub"],
}
for _k in ("reject", "continue", "request"):
    PLANS[_k] = PLANS["revise"]
SILENT = {"lessons"}
LABELS = {"intake": "reading the notes", "producer": "planning the changes", "forensics": "measuring the reference",
          "extend": "checking for new features", "edit": "editing", "render": "rendering", "qa": "machine checks",
          "critic": "independent review", "fix": "fixing what the review found", "render2": "re-rendering",
          "qa2": "re-checking", "verify": "re-judging the findings on the fixed render", "integrate": "publishing the version", "lessons": "learning",
          "variants_edit": "making options", "variants_render": "rendering options", "variants_integrate": "publishing options",
          "approve_intake": "approved", "render_final": "full-quality render", "qa_final": "checking the final render",
          "approve_record": "recording", "extend_stub": "feature request"}
STEPS = ["Intake", "Producer", "Reference", "Extend", "Edit", "Render", "QA", "Critic", "Fix", "Ready"]
STEP_OF = {"intake": "Intake", "producer": "Producer", "forensics": "Reference", "extend": "Extend", "edit": "Edit",
           "render": "Render", "qa": "QA", "critic": "Critic", "fix": "Fix", "render2": "Fix", "qa2": "Fix", "verify": "Fix",
           "integrate": "Ready", "lessons": "Ready", "variants_edit": "Edit", "variants_render": "Render",
           "variants_integrate": "Ready", "approve_intake": "Intake", "render_final": "Render", "qa_final": "QA",
           "approve_record": "Ready", "extend_stub": "Extend"}


# ================================================================================================ helpers
def _plan_load(path):
    return json.loads(Path(path).read_text())


def _validate(ctx, plan):
    from .. import plan as P
    from ..render import extra_validate
    try:
        p = P.normalize(copy.deepcopy(plan), ctx.cfg)
        return P.validate(p, ctx.con) + extra_validate(p, ctx.con, ctx.cfg)
    except Exception as e:  # noqa: BLE001
        return [f"plan cannot be read: {type(e).__name__}: {e}"]


def _resolve(ctx, plan):
    from ..render import resolve_timeline
    return resolve_timeline(ctx.cfg, ctx.con, copy.deepcopy(plan), preview=True)


def _rel(ctx, p):
    try:
        return Path(p).resolve().relative_to((Path(ctx.cfg.work) / "renders").resolve()).as_posix()
    except ValueError:
        return Path(p).name


def find_render(cfg, plan_file, preview=True):
    """The base render to compare against: export.find_render with the factory's rules - a PREVIEW render
    (the factory renders previews; comparing them with a final made every PSNR / QA delta wrong), of this
    plan's version, whole (never a cold-open-only partial), fresh (made from this content). A factory
    version's render is found by name (<slug>_v<N>_preview.mp4: its sidecar points at the order's candidate)."""
    from ..export import find_render as _find
    plan_file = Path(plan_file)
    m = bus.VERSION_RE.match(plan_file.name)
    names = [plan_file.stem] if m else []
    return _find(cfg, plan_file, preview=preview, names=names)


def _note_lines(notes, with_pieces=True):
    out = []
    for n in notes:
        head = (f"### {n['id']}" + (f" at {card.mmss(n['t'])}" if n.get("t") is not None else " (no timestamp)")
                + (f" to {card.mmss(n['t_end'])}" if n.get("t_end") is not None else ""))
        out.append(head)
        out.append(f"Operator wrote (verbatim): \"{n['text'] or '(no words)'}\"")
        if n.get("drawing"):
            out.append(f"They drew {n['drawing']} on the frame: {n.get('frame')}")
        elif n.get("frame"):
            out.append(f"Frame: {n['frame']}")
        if n.get("flags"):
            out.append(f"Flags: {', '.join(n['flags'])}")
        if n.get("supersedes"):
            out.append(f"Overrides earlier ruling {n['supersedes']} (newest note wins).")
        if with_pieces and n.get("pieces"):
            out.append("Under it (window " + " - ".join(card.mmss(x) for x in n["window"]) + "):")
            for p in n["pieces"][:8]:
                src = (f" {p['clip_rel'] or p['clip']} src {p['src_in']}-{p['src_out']}" if p.get("clip") else "")
                extra = (f" words: \"{p['words'][:160]}\"" if p.get("words") else "") + \
                        (f" captions: {p['captions'][:3]}" if p.get("captions") else "") + \
                        (f" overlay: {p['overlays'][:2]}" if p.get("overlays") else "")
                out.append(f"- seg {p.get('note_ref') or p['ref']} {p['track']} {p['type']}/{p['kind']} {card.mmss(p['start'])}-"
                           f"{card.mmss(p['end'])}{src}{extra}")
        if n.get("frames"):
            out.append("Clean frames: " + ", ".join(n["frames"]))
        out.append("")
    return "\n".join(out)


KEYWORD_TAGS = [(r"caption|subtitle|word|say", "captions"), (r"music|song|beat", "music"), (r"audio|sound|silence|noise|loud",
                "audio"), (r"b-?roll|clip|shot|footage|cutaway", "broll"), (r"graphic|animation|card|text|title", "graphics"),
                (r"story|order|before|after", "story"), (r"slow|fast|quick|long|pace|montage", "pace"), (r"intro|start|open", "intro")]


def note_tags(notes):
    out = set()
    for n in notes:
        for rx, tg in KEYWORD_TAGS:
            if re.search(rx, n.get("text") or "", re.I):
                out.add(tg)
    return out


def _versions_list(ctx):
    slug = ctx.order["plan"]
    pd = Path(ctx.cfg.work) / "plans"
    vs = sorted(((int(m.group("n")), e.name) for e in pd.iterdir()
                 if (m := bus.VERSION_RE.match(e.name)) and m.group("slug") == slug), reverse=True)
    return [str(pd / name) for _, name in vs[:6]]


def _candidate(ctx):
    return ctx.dir / "candidate.json"


def _intake(ctx):
    return ctx.outs.get("intake") or ctx.outs.get("approve_intake") or {}


# ================================================================================================ intake
def intake(ctx):
    from .. import studio as studiomod
    cfg, o = ctx.cfg, ctx.order
    slug = o.get("plan")
    if not slug:
        raise Blocked("the order names no plan")
    base_file = o.get("base_file")
    base_n = None
    if base_file:
        bp = Path(cfg.work) / "plans" / base_file
        m = bus.VERSION_RE.match(base_file)
        base_n = int(m.group("n")) if m else None
    else:
        base_file, base_n = bus.latest_version(cfg, slug)
        bp = Path(cfg.work) / "plans" / base_file if base_file else None
    if not bp or not bp.exists():
        dp = Path(cfg.work) / "plans" / f"{slug}.draft.json"
        if not dp.exists():
            raise Blocked(f"plan {slug} has no saved version to work from")
        bp, base_file, base_n = dp, dp.name, 0
    base = _plan_load(bp)
    pdir = ctx.pdir
    bus.write_json(pdir / "base.json", base)
    version = bus.reserve_version(cfg, slug)
    base_render = find_render(cfg, bp)
    cids, texts = bus.order_comments(cfg, o["id"])
    if o["kind"] in ("reject", "request", "continue") and o.get("text") and o["text"] not in texts:
        texts = [o["text"]] + texts
    ms = [m for m in studiomod.markers_load(cfg) if m.get("id") in set(cids)]
    decs = brain.decisions(cfg, slug)
    notes, tl = mapnotes.map_all(cfg, ctx.con, base, ms, texts, frames_dir=pdir / "frames", decisions=decs)
    if not notes:
        raise Blocked("nothing to do: no comments with a note or a drawing, and no text")
    praise = [n for n in notes if "praise" in n["flags"] and "remove" not in n["flags"]]
    if praise:
        brain.add_locks(cfg, slug, [k for n in praise for k in n["keys"]], why="praised: " + praise[0]["text"][:120],
                        version=base_n)

    def mark(data):
        for m in data:
            if m.get("id") in set(cids):
                m["status"] = "working"
                m["factory"] = {"order": o["id"], "version": version}
                m["updated"] = time.time()
    studiomod.markers_update(cfg, mark)
    ctx.notify_markers()
    ctx.progress(f"v{version}: {len(notes)} note(s) on {len({s for n in notes for s in n['segments']})} segment(s)", version=version)
    return {"base_file": base_file, "base_n": base_n, "base_path": str(pdir / "base.json"), "version": version,
            "base_render": str(base_render) if base_render else None, "notes": notes, "comments": cids,
            "duration": tl.get("duration")}


# ================================================================================================ producer
def size_team(order, notes, res):
    """Code checks the producer's sizing (it can't under-size an order)."""
    team = dict(res.get("team") or {})
    size = team.get("size") or "S"
    rank = {"S": 0, "M": 1, "L": 2}
    why = []
    if any("restore" in n["flags"] for n in notes) and rank[size] < 1:
        size = "M"
        why.append("a restore must be lock-checked")
    if any(c.get("action") == "new_capability" for c in res.get("changes") or []) or team.get("extend"):
        size = "L"
        why.append("new capability")
    if order["kind"] == "request":
        size = "L"
        why.append("a request is a new direction")
    if len(notes) > 12 and rank[size] < 1:
        size = "M"
        why.append("more than 12 notes")
    team["size"] = size
    team["code_why"] = why
    return team


def producer(ctx):
    it = _intake(ctx)
    notes = it["notes"]
    slug = ctx.order["plan"]
    br = lessons.brief(ctx.cfg, slug, tags=note_tags(notes))
    sections = [("The notes (mapped by code)", _note_lines(notes)),
                ("Notes history (what earlier rounds did)", "\n".join(
                    f"- v{r.get('version')} {r.get('note')}: \"{str(r.get('words') or '')[:100]}\" -> {r.get('change')}"
                    for r in brain.notes_ledger(ctx.cfg, slug, 30))),
                ("Order text", ctx.order.get("text") if ctx.order["kind"] in ("reject", "request", "continue") else "")]
    if ctx.order["kind"] == "reject":
        sections.append(("This is a REJECT", "The operator rejected the version. Decide per noted span between "
                         "'restore the previous version there' and 'a new approach'; ask with numbered options when "
                         "it is unclear."))
    p = roles.build("producer", order=ctx.order, brief_text=br["text"], sections=sections, context={
        "base plan (read it)": it["base_path"], "older versions": _versions_list(ctx),
        "project brain folder": str(brain.bdir(ctx.cfg, slug))})
    imgs = [n["frame"] for n in notes if n.get("frame")][:8]
    r = ctx.role("producer", p, images=imgs)
    res = r["result_json"]
    team = size_team(ctx.order, notes, res)
    qs = card.clean_questions(res.get("questions") or [])
    ch = [c for c in res.get("changes") or [] if isinstance(c, dict)]
    ctx.progress(f"{len(ch)} change(s), size {team['size']}" + (f", {len(qs)} question(s)" if qs else ""))
    return {"changes": ch, "team": team, "questions": qs, "summary": res.get("summary"), "brief_rules": br["rules"]}


# ================================================================================================ forensics / extend
def forensics(ctx):
    team = (ctx.outs.get("producer") or {}).get("team") or {}
    if not team.get("forensics"):
        raise Skip("not needed")
    base = _plan_load(_intake(ctx)["base_path"])
    ref = ctx.order.get("ref") or base.get("reference") or (base.get("style") or {}).get("reference")
    if not ref:
        raise Skip("no reference attached")
    slug = ctx.order["plan"]
    p = roles.build("forensics", order=ctx.order, sections=[], context={
        "reference id": ref, "our latest render": _intake(ctx).get("base_render"),
        "brain folder": str(brain.bdir(ctx.cfg, slug))})
    r = ctx.role("forensics", p)
    res = r["result_json"]
    if res.get("reference_md"):
        brain.write_file(ctx.cfg, slug, "REFERENCE.md", res["reference_md"])
    if res.get("pairs"):
        brain.write_file(ctx.cfg, slug, "ref_pairs.json", res["pairs"])
    return {"ref": ref, "pairs": len(res.get("pairs") or [])}


def extend(ctx):
    pr = ctx.outs.get("producer") or {}
    caps = list(dict.fromkeys((pr.get("team") or {}).get("extend") or
                              [c.get("summary") for c in pr.get("changes") or [] if c.get("action") == "new_capability"]))
    caps = [c for c in caps if c]
    if not caps:
        raise Skip("no new capability needed")
    subs = []
    for cap in caps[:3]:
        o = bus.create(ctx.cfg, "extend", ctx.order["plan"], text=cap, parent=ctx.order["id"],
                       by={"user": "factory", "via": "producer"})
        subs.append(o["id"])
    ctx.progress(f"needs {len(caps)} new feature(s): sub-order(s) {', '.join(subs)}")
    return {"capabilities": caps, "suborders": subs}


# capabilities a producer asked for that have since been built: name pattern -> how to use it
BUILT_CAPABILITIES = [
    (re.compile(r"typed[ _-]?title.*(pos|place|placement|position|move)|title[ _-]?(pos|placement|position)", re.I),
     "typed_title_position",
     "Built: typed titles take \"pos\": {\"x\": 0-1, \"y\": 0-1} (title centre, frame fractions) or \"pos\": \"auto\" "
     "(measured: the quietest readable area of the held shot, off faces/people and the sun, with a contrast check; "
     "a dark backing plate when no area reads) on a typed_title overlay or on cold_open.title. "
     "cold_open.end_shot: \"auto\" picks the held shot whose quiet/dark area suits the title, at a source window not "
     "used nearby. Preview it with `cutroom title place <plan> --choose-shot`. Send the note again (or a new comment) "
     "to have the factory use it."),
]


def built_capability(cap):
    """(name, how-to text) when the requested capability already exists, else None."""
    for rx, name, how in BUILT_CAPABILITIES:
        if rx.search(str(cap or "")):
            return name, how
    return None


def extend_stub(ctx):
    cap = ctx.order.get("text") or "a new capability"
    have = built_capability(cap)
    if have:
        ctx.progress(f"already built: {have[0]}")
        return {"state": "done", "why": None, "version": None, "questions": [], "capability": have[0],
                "text": f"feature {have[0]}: {have[1]}"}
    raise NeedsYou(f"new feature needed: {cap[:120]}", questions=card.clean_questions([{
        "text": f"New feature needed: {cap[:150]}. The extender is not enabled in this build.",
        "options": ["build it by hand (see the extending-cutroom skill)", "skip it"]}], kind="extend"), kind="extend")


# ================================================================================================ edit
def _ledger_rows(rows, notes):
    ids = {n["id"] for n in notes}
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("note") not in ids:
            continue
        t = r.get("t_out")
        out.append({"note": r["note"], "change": str(r.get("change") or "").strip(), "segments": list(r.get("segments") or []),
                    "t_out": None if t is None else round(float(t), 3), "proof": str(r.get("proof") or "").strip()})
    return out


def _finish_candidate(ctx, label="editor"):
    cp = _candidate(ctx)
    try:
        cand = _plan_load(cp)
    except (OSError, ValueError) as e:
        raise Blocked(f"the {label} left no readable candidate plan: {e}") from None
    if not isinstance(cand, dict) or not isinstance(cand.get("sections"), list):
        raise Blocked(f"the {label}'s candidate is not a plan")
    cand["version"] = _intake(ctx)["version"]
    bus.write_json(cp, cand)
    errs = _validate(ctx, cand)
    if errs:
        raise Blocked(f"the {label}'s plan does not validate: {'; '.join(errs[:3])}")
    return cand


def edit(ctx):
    it, pr = _intake(ctx), ctx.outs.get("producer") or {}
    cp = _candidate(ctx)
    base = _plan_load(it["base_path"])
    base["version"] = it["version"]
    if ctx.order.get("adopt"):                                    # Keep on a variant: no editor run
        src = Path(ctx.order["adopt"])
        shutil.copyfile(src, cp)
        _finish_candidate(ctx, "variant")
        ledger = _ledger_rows(ctx.order.get("adopt_ledger") or [], it["notes"])
        bus.write_json(ctx.dir / "ledger.json", ledger)
        return {"ledger": ledger, "unplaced": [], "proofs": [], "summary": "kept a variant", "adopted": str(src)}
    bus.write_json(cp, base)                                       # always start from the base (a re-run is clean)
    slug = ctx.order["plan"]
    tags = lessons.tags_for(pr.get("changes")) | note_tags(it["notes"])
    br = lessons.brief(ctx.cfg, slug, tags=tags)
    never = [g for g in brain.grades(ctx.cfg) if g.get("grade") == "NEVER" or g.get("ident") == "NOTME"]
    sections = [("The notes (mapped by code)", _note_lines(it["notes"])),
                ("The changes to make (from the producer)", "\n".join(
                    f"- {c.get('note')}: {c.get('action')} {c.get('segments')}: {c.get('summary')}"
                    for c in pr.get("changes") or [])),
                ("Questions already going to the operator (do not do these notes)", "\n".join(
                    q["line"] for q in pr.get("questions") or [])),
                ("Clips the operator graded NEVER / NOT ME", "\n".join(
                    f"- {g['clip']} {g.get('t0') or ''}-{g.get('t1') or ''} {g.get('grade') or ''} {g.get('ident') or ''}"
                    for g in never)),
                ("Praised / approved segments (locked unless a note names them)", "\n".join(
                    f"- {k}: {v.get('why')}" for k, v in brain.locks(ctx.cfg, slug).items()))]
    p = roles.build("editor", order=ctx.order, brief_text=br["text"], sections=sections, context={
        "candidate plan (edit this file in place)": str(cp), "base plan (read-only copy)": it["base_path"],
        "older versions": _versions_list(ctx), "STORY.md": str(brain.bdir(ctx.cfg, slug) / "STORY.md"),
        "REFERENCE.md": str(brain.bdir(ctx.cfg, slug) / "REFERENCE.md"), "bins folder": str(Path(ctx.cfg.work) / "studio" / "bins")})
    imgs = [n["frame"] for n in it["notes"] if n.get("frame")][:8]
    r = ctx.role("editor", p, images=imgs, env={"CUTROOM_FACTORY_BASE_PLAN": it["base_path"],
                                                  "CUTROOM_FACTORY_CANDIDATE": str(cp)})
    res = r["result_json"]
    _finish_candidate(ctx)
    ledger = _ledger_rows(res.get("ledger"), it["notes"])
    unplaced = card.clean_questions([{"text": u.get("question"), "options": u.get("options"), "note": u.get("note")}
                                     for u in res.get("unplaced") or [] if isinstance(u, dict)])
    bus.write_json(ctx.dir / "ledger.json", ledger)
    ctx.progress(f"edited: {len(ledger)} ledger row(s)" + (f", {len(unplaced)} unplaced" if unplaced else ""))
    return {"ledger": ledger, "unplaced": unplaced, "proofs": res.get("proofs") or [], "summary": res.get("summary")}


# ================================================================================================ render
def _render_out(ctx, suffix=""):
    it = _intake(ctx)
    return Path(ctx.cfg.work) / "renders" / f"{ctx.order['plan']}_v{it['version']}{suffix}_preview.mp4"


def do_render(ctx, plan_path, out, preview=True):
    log = ctx.pdir / "render.log"
    t0 = time.time()

    def line(s):
        m = re.match(r"\s*progress (\d+)/(\d+)", s)
        if m:
            ctx.progress(f"rendering {m.group(1)}/{m.group(2)}")
    rc, tail, why = ctx.render_fn(plan_path, out, preview, log, line)
    if why == "stopped":
        raise Stopped()
    if why == "timeout":
        raise Blocked(f"render hit its wall clock ({ctx.wall('render')}s)")
    if rc != 0 or not Path(out).exists():
        raise Blocked(f"render failed: {' | '.join([x for x in tail if x.strip()][-3:])[:400]}")
    side = Path(str(out)[:-4] + ".json")
    d = bus.read_json(side, {}) or {}
    return {"render": _rel(ctx, out), "sidecar": _rel(ctx, side), "duration": d.get("duration"),
            "seconds": round(time.time() - t0, 1), "preview": preview}


def base_render(ctx):
    """The base version's render (intake found one, or the render phase made one)."""
    br = _intake(ctx).get("base_render") or (ctx.outs.get("render") or {}).get("base_render")
    return br if br and Path(br).exists() else None


def _render_base(ctx):
    """No fresh preview of the base version exists (only a final, a partial or a stale one): render it once
    so QA regressions, the render lock and the claim deltas compare like with like. Never overwrites."""
    it = _intake(ctx)
    if it.get("base_render") or not it.get("base_n") or not ctx.fc.get("render_base", True):
        return None
    rd = Path(ctx.cfg.work) / "renders"
    out = rd / f"{ctx.order['plan']}_v{it['base_n']}_preview.mp4"
    if out.exists():
        out = rd / f"{ctx.order['plan']}_v{it['base_n']}_base_preview.mp4"
    if out.exists():
        return str(out)
    ctx.progress(f"no fresh preview of v{it['base_n']}: rendering it once as the base")
    plan = Path(ctx.cfg.work) / "plans" / it["base_file"]
    try:
        do_render(ctx, plan if plan.exists() else it["base_path"], out)
    except Blocked as e:
        ctx.progress(f"base render failed ({str(e)[:120]}): comparing without a base")
        return None
    return str(out) if out.exists() else None


def render(ctx):
    _finish_candidate(ctx, "candidate")
    out = do_render(ctx, _candidate(ctx), _render_out(ctx))
    br = _render_base(ctx)
    if br:
        out["base_render"] = br
    return out


def render2(ctx):
    if (ctx.outs.get("fix") or {}).get("skipped"):
        raise Skip("no fix round")
    return do_render(ctx, _candidate(ctx), _render_out(ctx))


# ================================================================================================ machine QA
def _base_qa(ctx):
    br = base_render(ctx)
    if not br:
        return None
    qj = Path(br).with_name(Path(br).stem + ".qa.json")
    if qj.exists() and qj.stat().st_mtime >= Path(br).stat().st_mtime:
        return bus.read_json(qj)
    ctx.progress("measuring the base render once (to tell new defects from old ones)")
    rep, _ = gates.run_qa(ctx.cfg, br, _intake(ctx)["base_path"], ctx.pdir / "qa_base.log",
                          timeout=ctx.wall("qa"), stop_check=ctx.stop_check)
    return rep


def machine_qa(ctx, round_=1):
    it = _intake(ctx)
    pr = ctx.outs.get("producer") or {}
    ed = ctx.outs.get("fix") if round_ == 2 else ctx.outs.get("edit")
    ledger = (ed or {}).get("ledger") or (ctx.outs.get("edit") or {}).get("ledger") or []
    rend = ctx.outs.get("render2" if round_ == 2 else "render") or {}
    video = Path(ctx.cfg.work) / "renders" / rend["render"]
    cand = _plan_load(_candidate(ctx))
    base = _plan_load(it["base_path"])
    fails, warns, checked = [], [], []
    rep, err = gates.run_qa(ctx.cfg, video, _candidate(ctx), ctx.pdir / "qa.log", timeout=ctx.wall("qa"),
                            stop_check=ctx.stop_check)
    if ctx.stop_check():
        raise Stopped()
    base_rep = _base_qa(ctx) if rep else None
    if err:
        fails.append({"id": "qa", "text": f"machine QA did not run: {err}", "t": None})
        new, kept = [], []
    else:
        new, kept = gates.qa_regressions(rep, base_rep)
        s = rep.get("summary") or {}
        checked.append(f"machine QA ({len(rep.get('checks') or [])} checks: cuts, black, freeze, loudness, silence, "
                       f"music under speech, faces under captions, shake, repeats): {s.get('fail', 0)} fail, "
                       f"{s.get('warn', 0)} warn")
        for x in new:
            fails.append({"id": f"qa:{x['check']}", "text": f"{x['title']}: {'; '.join(m for m in x['msgs'] if m)}",
                          "t": x["t"], "evidence": x.get("evidence")})
        for x in kept:
            warns.append(f"{x['title']} ({x['n']} fail{'s' if x['n'] > 1 else ''}) "
                         + (f"is new, but v{it.get('base_n')} was measured before this check existed: not comparable"
                            if x.get("not_on_base") else
                            f"was already in v{it.get('base_n')}: not caused by this round"))
        if s.get("warn"):
            warns.append(f"{s['warn']} QA warning(s): see the QA report")
    # lock check
    notes = it["notes"]
    noted = {r for n in notes if not ("praise" in n["flags"] and len(n["flags"]) == 1) for r in n["segments"]}
    ledger_refs = {s for r in ledger for s in r.get("segments") or []}
    acts = {c.get("action") for c in pr.get("changes") or []}
    lk = lockcheck.plan_lock(base, cand, allowed_refs=noted | ledger_refs,
                             locked_keys=set(brain.locks(ctx.cfg, ctx.order["plan"])), allowed_locked_refs=noted,
                             allowed_global=bool(acts & lockcheck.GLOBAL_ACTIONS) or ctx.order["kind"] == "request",
                             allowed_top={"cold_open"} if any(n.get("cold_open") for n in notes) else ())
    checked.append(f"lock check: {len(lk['pairs'])} untouched segment(s) identical in the plan, "
                   f"{len(set(lk['changed']))} changed, {len(lk['added'])} new")
    for i in lk["issues"]:
        (fails.append({"id": "lock", "text": i["text"], "t": None}) if i["sev"] == "HARD" else warns.append(i["text"]))
    rlk = None
    br = base_render(ctx)
    if br and lk["pairs"]:
        try:
            rlk = lockcheck.render_lock(ctx.cfg, br, _resolve(ctx, base), video, _resolve(ctx, cand), lk["pairs"],
                                        ctx.pdir / "lock")
            checked.append(f"render lock: {len(rlk['rows'])} untouched segment(s) compared frame by frame "
                           f"(min PSNR {min([r['psnr'] for r in rlk['rows']] or [99]):.1f} dB)")
            rlk["issues"] = lockcheck.excuse_global(rlk["issues"], lk.get("global_changed"),
                                                    bool(acts & lockcheck.GLOBAL_ACTIONS) or ctx.order["kind"] == "request")
            rlk["status"] = "fail" if any(i["sev"] == "HARD" for i in rlk["issues"]) else "pass"
            for i in rlk["issues"]:
                (fails.append({"id": "lock_render", "text": i["text"], "t": i.get("t")}) if i["sev"] == "HARD"
                 else warns.append(i["text"]))
        except Exception as e:  # noqa: BLE001
            warns.append(f"render lock not measured: {e}")
    tl = _resolve(ctx, cand)
    # claims: every measurable number the editor / fixer wrote, measured on this render (and vs the base)
    cl_rows = []
    try:
        btl = _resolve(ctx, base) if br else None
        src = "fix" if round_ == 2 else "editor"
        cl_rows = claims.check(ctx.cfg, claims.texts_of(notes, ledger, (ed or {}).get("summary"),
                                                        (ed or {}).get("proofs") if round_ == 2 else (), src),
                               video, tl, br, btl, rep)
    except Exception as e:  # noqa: BLE001 - the claim check never stops QA
        warns.append(f"claims not measured: {type(e).__name__}: {str(e)[:120]}")
    bad = [r for r in cl_rows if r.get("ok") is False]
    if cl_rows:
        checked.append(f"claims: {len(cl_rows)} number(s) in the {'fix' if round_ == 2 else 'edit'} summary measured "
                       f"on the render, {len(bad)} not shown by it")
    # lint (lessons -> checks)
    lt = lint.run(ctx.cfg, cand, tl, rep, con=ctx.con)
    checked.append(f"lint: {len(lt)} learned check(s)" if lt else "lint: no learned checks yet")
    for x in lt:
        if x["status"] == "fail":
            fails.append({"id": f"lint:{x['id']}", "text": x["msg"], "t": x.get("t"), "lesson": x.get("lesson")})
        elif x["status"] == "warn":
            warns.append(f"lint {x['id']}: {x['msg']}")
    # A/V drift + link
    drift = gates.av_drift(rep) if rep else None
    if drift:
        fails.append({"id": "av_drift", "text": "audio and video lengths differ (lip sync)", "t": None})
    elif drift == 0:
        checked.append("A/V length: matched (drift 0)")
    link = gates.link_check(video, ctx.link_url(rend["render"]))
    (checked.append(f"review link: {link['how']} -> {link.get('code') or 'ok'}") if link["status"] == "pass"
     else fails.append({"id": "link", "text": f"review link does not play: {link}", "t": None}))
    # changed-spots sheet
    tmap = {n["id"]: n.get("t") for n in notes}
    spots = [(r["note"], r["t_out"] if r.get("t_out") is not None else tmap.get(r["note"]) or 0.0) for r in ledger
             if r.get("t_out") is not None or tmap.get(r["note"]) is not None]
    sheet = Path(ctx.cfg.work) / "renders" / f"{video.stem}.changed.jpg"
    rows = gates.changed_sheet(ctx.cfg, video, spots, sheet) if spots else []
    if rows:
        checked.append(f"changed-spots sheet: {len(rows)} spot(s) x 3 frames")
    # reference compare
    refcmp = None
    ref = ctx.order.get("ref") or cand.get("reference")
    if ref and isinstance(ref, str):
        log = ctx.pdir / "refcmp.log"
        rc, tail, _ = gates.run_cli(ctx.cfg, ["ref", "compare", ref, video, "--json"], log, ctx.wall("qa"), ctx.stop_check)
        try:
            refcmp = json.loads("\n".join(tail[tail.index(next(x for x in tail if x.startswith("{"))):])) if rc == 0 else None
        except (StopIteration, ValueError):
            refcmp = None
        if refcmp:
            checked.append(f"reference compare: {refcmp.get('verdict')}")
    return {"round": round_, "render": rend["render"], "qa_json": _rel(ctx, video.with_name(video.stem + ".qa.json"))
            if rep else None, "qa_summary": (rep or {}).get("summary"), "new_fails": new, "kept_fails": kept,
            "lock": {k: lk[k] for k in ("status", "issues", "changed", "added", "global_changed")},
            "render_lock": rlk, "lint": lt, "drift": drift, "link": link,
            "sheet": _rel(ctx, sheet) if rows else None, "spots": rows, "refcmp": refcmp, "fails": fails,
            "claims": cl_rows, "claim_findings": claims.findings(cl_rows, prefix="K" if round_ == 1 else "KF"),
            "warns": warns, "checked": checked, "lessons_sha": lessons.lessons_sha(ctx.cfg)}


def qa(ctx):
    return machine_qa(ctx, 1)


def qa2(ctx):
    if (ctx.outs.get("fix") or {}).get("skipped"):
        raise Skip("no fix round")
    return machine_qa(ctx, 2)


# ================================================================================================ critic / fix
def critic_needed(ctx):
    pr = ctx.outs.get("producer") or {}
    acts = {c.get("action") for c in pr.get("changes") or []}
    if acts and acts <= TEXT_ACTIONS | {"none"} and (acts & TEXT_ACTIONS):
        return False, "pure text / caption tweak"
    return True, ""


def critic(ctx):
    need, why = critic_needed(ctx)
    if not need:
        raise Skip(why)
    it, q = _intake(ctx), ctx.outs["qa"]
    rend = ctx.outs["render"]
    sheets = ((q.get("qa_summary") or {}).get("sheets") or [])[:4]
    sheet = str(Path(ctx.cfg.work) / "renders" / q["sheet"]) if q.get("sheet") else None
    slug = ctx.order["plan"]
    br = lessons.brief(ctx.cfg, slug, tags=lessons.tags_for((ctx.outs.get("producer") or {}).get("changes"))
                       | note_tags(it["notes"]))
    sections = [("The operator's notes (verbatim, with where they pointed)", _note_lines(it["notes"], with_pieces=False)),
                ("Changed spots (from the sheet labels; times are on the NEW version)", "\n".join(
                    f"- {r['label']} -> {card.mmss(r['t'])}" for r in q.get("spots") or [])),
                ("Machine QA (measured by code)", "\n".join(
                    [f"- FAIL {f['id']}: {f['text']}" for f in q["fails"]] + [f"- warn: {w}" for w in q["warns"][:10]]
                    + [f"- {c}" for c in q["checked"]]))]
    p = roles.build("critic", order=ctx.order, brief_text=br["text"], sections=sections, context={
        "the delivered render": str(Path(ctx.cfg.work) / "renders" / rend["render"]),
        "changed-spots sheet (open it)": sheet, "QA contact sheets": sheets,
        "REFERENCE.md": str(brain.bdir(ctx.cfg, slug) / "REFERENCE.md") if (brain.bdir(ctx.cfg, slug) / "REFERENCE.md").exists() else None})
    imgs = [x for x in [sheet] + sheets[:2] if x]
    r = ctx.role("critic", p, images=imgs)
    res = r["result_json"]
    fs = [f for f in res.get("findings") or [] if isinstance(f, dict)]
    for i, f in enumerate(fs):
        f["id"] = f.get("id") or f"F{i + 1}"
    fs += list(q.get("claim_findings") or [])      # numbers the editor claimed that the render does not show
    hard = [f for f in fs if f.get("sev") == "HARD"]
    ctx.progress(f"critic: {len(hard)} HARD, {len(fs) - len(hard)} SOFT")
    return {"findings": fs, "per_note": res.get("per_note") or [], "summary": res.get("summary"),
            "status": "hard" if hard else "pass"}


def fix(ctx):
    q = ctx.outs["qa"]
    cr = ctx.outs.get("critic") or {}
    hard = [f for f in cr.get("findings") or [] if f.get("sev") == "HARD"]
    have = {f.get("id") for f in hard}
    hard += [f for f in q.get("claim_findings") or [] if f["id"] not in have]     # critic skipped: still fix claims
    machine = q["fails"]
    if not hard and not machine:
        raise Skip("nothing to fix")
    it = _intake(ctx)
    cp = _candidate(ctx)
    r1 = ctx.dir / "candidate_r1.json"
    if not r1.exists():
        shutil.copyfile(cp, r1)
    else:
        shutil.copyfile(r1, cp)                                   # a re-run fixes round 1's plan again
    slug = ctx.order["plan"]
    ed = ctx.outs.get("edit") or {}
    br = lessons.brief(ctx.cfg, slug, tags=lessons.tags_for((ctx.outs.get("producer") or {}).get("changes")))
    sections = [("Fix these (HARD findings and machine fails)", "\n".join(
                    [f"- {f['id']} HARD @ {card.mmss(f.get('t'))}: {f.get('text')} (evidence {f.get('evidence')})" for f in hard]
                    + [f"- {m['id']} machine fail @ {card.mmss(m.get('t'))}: {m['text']}" for m in machine])),
                ("Your first-round ledger", json.dumps(ed.get("ledger") or [], ensure_ascii=False, indent=1)),
                ("The notes", _note_lines(it["notes"], with_pieces=False))]
    p = roles.build("fix", order=ctx.order, brief_text=br["text"], sections=sections, context={
        "candidate plan (edit this file in place)": str(cp), "base plan (read-only copy)": it["base_path"]})
    r = ctx.role("fix", p, env={"CUTROOM_FACTORY_BASE_PLAN": str(cp), "CUTROOM_FACTORY_CANDIDATE": str(cp)})
    res = r["result_json"]
    _finish_candidate(ctx, "fixer")
    ledger = _ledger_rows(res.get("ledger"), it["notes"]) or ed.get("ledger") or []
    bus.write_json(ctx.dir / "ledger.json", ledger)
    proofs = [p for p in res.get("proofs") or [] if isinstance(p, dict)]
    ctx.progress(f"fix round: {len(proofs)} proof(s) for {len(hard) + len(machine)} item(s)")
    return {"proofs": proofs, "ledger": ledger, "targets": [f["id"] for f in hard] + [m["id"] for m in machine],
            "summary": res.get("summary")}


# ================================================================================================ verify
def _hard_findings(ctx):
    cr = ctx.outs.get("critic") or {}
    q = ctx.outs.get("qa") or {}
    hard = [f for f in cr.get("findings") or [] if f.get("sev") == "HARD"]
    have = {f.get("id") for f in hard}
    return hard + [f for f in q.get("claim_findings") or [] if f["id"] not in have]


def _verify_claim(ctx, f, q2, fx):
    """A false-claim finding closes when the claim now measures true on the fixed render, or the fix round no
    longer makes it (the card then carries the measured number instead)."""
    c = f.get("claim") or {}
    it = _intake(ctx)
    note = next((n for n in it["notes"] if n["id"] == c.get("note")), {})
    video = Path(ctx.cfg.work) / "renders" / ctx.outs["render2"]["render"]
    cand = _plan_load(_candidate(ctx))
    br = base_render(ctx)
    try:
        rows = claims.check(ctx.cfg, [{"note": note, "source": "recheck", "text": c.get("text")}], video, _resolve(ctx, cand),
                            br, _resolve(ctx, _plan_load(it["base_path"])) if br else None,
                            bus.read_json(video.with_name(video.stem + ".qa.json"), {}))
    except Exception as e:  # noqa: BLE001
        rows = []
        why = f"not re-measured: {e}"[:160]
    same = [r for r in rows if r.get("qty") == c.get("qty") and r.get("kind") == c.get("kind")]
    if same and all(r.get("ok") for r in same):
        return True, "now measures true on the fixed render: " + claims.describe(same[0])
    fix_text = " ".join([str((fx or {}).get("summary") or "")] + [str(r.get(k) or "") for r in (fx or {}).get("ledger") or []
                                                                   for k in ("change", "proof")])
    if c.get("text") and c["text"][:60] not in fix_text and not any(
            r.get("ok") is False and r.get("qty") == c.get("qty") for r in q2.get("claims") or []):
        return True, ("the fix round withdrew the claim; the card shows the measured number"
                      + (f" ({claims.describe(same[0])})" if same else ""))
    return False, ("still not shown by the fixed render: " + claims.describe(same[0])) if same else "still unproven"


def verify(ctx):
    """Re-judge every HARD finding against the FIXED render (the real run's findings could never close: the
    gate only asked for a proof string and a clean re-render). Claim findings are re-measured by code; critic
    findings get one short read-only verify run with the new render, its sheet and the fix proofs."""
    fx = ctx.outs.get("fix") or {}
    if fx.get("skipped") or "fix" not in ctx.outs:
        raise Skip("no fix round")
    hard = _hard_findings(ctx)
    if not hard:
        raise Skip("no HARD findings to re-judge")
    q2 = ctx.outs.get("qa2") or {}
    results = {}
    for f in hard:
        if f.get("source") == "claims":
            ok, ev = _verify_claim(ctx, f, q2, fx)
            results[f["id"]] = {"closed": ok, "evidence": ev, "by": "code"}
    crit = [f for f in hard if f.get("source") != "claims"]
    if crit:
        rend = ctx.outs["render2"]
        sheet = str(Path(ctx.cfg.work) / "renders" / q2["sheet"]) if q2.get("sheet") else None
        proofs = {p.get("finding"): p.get("proof") for p in fx.get("proofs") or []}
        sections = [("HARD findings to re-judge on the NEW render", "\n".join(
                        f"- {f['id']} @ {card.mmss(f.get('t'))}: {f.get('text')}" for f in crit)),
                    ("What the fix round says it did (claims to check, not facts)", "\n".join(
                        f"- {f['id']}: {proofs.get(f['id']) or '(no proof given)'}" for f in crit)),
                    ("The operator's notes (verbatim)", _note_lines(_intake(ctx)["notes"], with_pieces=False)),
                    ("Machine QA on the fixed render", "\n".join([f"- FAIL {x['id']}: {x['text']}" for x in q2.get("fails") or []]
                                                                  + [f"- {c}" for c in q2.get("checked") or []]))]
        p = roles.build("verify", order=ctx.order, sections=sections, context={
            "the FIXED render": str(Path(ctx.cfg.work) / "renders" / rend["render"]), "changed-spots sheet (open it)": sheet})
        try:
            r = ctx.role("verify", p, images=[x for x in [sheet] if x], budgeted=False)
            for x in (r["result_json"] or {}).get("results") or []:
                if isinstance(x, dict) and x.get("id") in {f["id"] for f in crit}:
                    results[x["id"]] = {"closed": bool(x.get("closed")), "evidence": str(x.get("evidence") or "")[:400],
                                        "by": "verify"}
        except (Blocked, Requeue) as e:
            ctx.progress(f"verify run failed ({str(e)[:120]}): findings stay open")
    closed = [k for k, v in results.items() if v["closed"]]
    ctx.progress(f"re-judged {len(results)} HARD finding(s) on the fixed render: {len(closed)} closed")
    return {"results": results, "closed": closed}


# ================================================================================================ integrate
def versions_meta_write(cfg, slug, name, rec):
    """plans/.history/<slug>/versions.json (the studio's 'who saved it'), first writer wins, plus our fields."""
    p = Path(cfg.work) / "plans" / ".history" / slug / "versions.json"
    with bus.flocked(p.with_name(".versions.lock")):
        d = bus.read_json(p, {}) or {}
        cur = d.get(name) or {}
        d[name] = {**cur, "by": cur.get("by") or "factory", "t": cur.get("t") or time.time(), **rec}
        bus.write_json(p, d)


def versions_index(cfg) -> dict:
    return bus.read_json(bus.root(cfg) / "versions.json", {}) or {}


def versions_index_update(cfg, key, **fields):
    p = bus.root(cfg) / "versions.json"
    with bus.flocked(p.with_name(".vindex.lock")):
        d = bus.read_json(p, {}) or {}
        d[key] = {**(d.get(key) or {}), **fields}
        bus.write_json(p, d)
        return d[key]


def round_questions(pr, edit_out, fix_out=None):
    """Producer questions + the editor's unplaced notes, minus the notes the fix round then placed (a
    question the fix answered must not stay on the card or mark its comment needs_you)."""
    placed = {r.get("note") for r in (fix_out or {}).get("ledger") or [] if r.get("change")}
    unplaced = [q for q in (edit_out or {}).get("unplaced") or [] if not q.get("note") or q["note"] not in placed]
    return list((pr or {}).get("questions") or []) + unplaced


def integrate(ctx):
    from .. import studio as studiomod
    cfg, o = ctx.cfg, ctx.order
    it, pr = _intake(ctx), ctx.outs.get("producer") or {}
    slug, N = o["plan"], it["version"]
    fixed = not (ctx.outs.get("fix") or {}).get("skipped") and "fix" in ctx.outs
    fq = ctx.outs["qa2"] if fixed and not ctx.outs["qa2"].get("skipped") else ctx.outs["qa"]
    ed = ctx.outs.get("fix") if fixed else ctx.outs.get("edit")
    ledger = (ed or {}).get("ledger") or (ctx.outs.get("edit") or {}).get("ledger") or []
    notes = it["notes"]
    cand = _plan_load(_candidate(ctx))
    not_passed = []
    errs = _validate(ctx, cand)
    if errs:
        not_passed.append(f"plan check: {errs[0]}")
    for f in fq["fails"]:
        not_passed.append(f"machine {f['id']}: {f['text'][:140]}")
    questions = round_questions(pr, ctx.outs.get("edit"), ctx.outs.get("fix") if fixed else None)
    asked = {q.get("note") for q in questions if q.get("note")}
    done_notes = {r["note"] for r in ledger if r.get("change")}
    praise_only = {c.get("note") for c in pr.get("changes") or [] if c.get("action") == "none"}
    unresolved = [n for n in notes if n["id"] not in done_notes | asked | praise_only]
    for n in unresolved:
        questions.append({"text": f"I couldn't do {n['id']} (\"{card.cut_words(n['text'], 90)}\").", "options": [
            "try again with more detail from me", "leave it as it is"], "note": n["id"], "kind": "unresolved"})
    questions = card.clean_questions(questions)
    # HARD findings (critic + false claims): closed only when re-judged closed on the FIXED render (verify).
    # Pre-existing QA fails (already in the base version) are warnings and never keep a finding open.
    cr = ctx.outs.get("critic") or {}
    proofs = {p.get("finding"): p.get("proof") for p in (ctx.outs.get("fix") or {}).get("proofs") or []}
    ver = (ctx.outs.get("verify") or {}).get("results") or {}
    crit_rows = []
    hard_ids = set()
    for f in list(cr.get("findings") or []) + [f for f in (ctx.outs.get("qa") or {}).get("claim_findings") or []
                                               if f["id"] not in {x.get("id") for x in cr.get("findings") or []}]:
        row = {k: v for k, v in f.items() if k != "claim"}
        if f.get("sev") == "HARD":
            hard_ids.add(f.get("id"))
            v = ver.get(f.get("id"))
            closed = bool(fixed and v and v.get("closed"))
            row["fix"] = proofs.get(f.get("id")) if fixed else None
            row["verified"] = v.get("evidence") if v else None
            row["status"] = "closed" if closed else "open"
            if not closed:
                why = (" (re-judged on the fixed render: " + v["evidence"][:100] + ")") if v else \
                    (" (no fix round)" if not fixed else " (not re-judged)")
                not_passed.append(f"{'claim' if f.get('source') == 'claims' else 'critic'} {f.get('id')}: "
                                  f"{str(f.get('text'))[:120]}{why}")
        crit_rows.append(row)
    if fixed:                                   # numbers the FIX round wrote that the render does not show
        for f in fq.get("claim_findings") or []:
            row = {k: v for k, v in f.items() if k != "claim"}
            row["status"] = "open"
            crit_rows.append(row)
            not_passed.append(f"claim {f['id']}: {f['text'][:140]}")
    ledger = claims.annotate_ledger(ledger, fq.get("claims") or [])
    passed = not not_passed
    # ---- publish the version (integrate is the only writer of versions)
    name = f"{slug}_v{N}.json"
    th = card.theme(N, ledger)
    versions_meta_write(cfg, slug, name, {"by": "factory", "order": o["id"], "theme": th, "passed": passed})
    vp = Path(cfg.work) / "plans" / name
    if vp.exists():
        raise Blocked(f"{name} already exists (reserved number collided)")
    cand["version"] = N
    bus.atomic_write(vp, json.dumps(cand, indent=1, ensure_ascii=False) + "\n")
    rend = ctx.outs.get("render2") if fixed and not (ctx.outs.get("render2") or {}).get("skipped") else ctx.outs["render"]
    caveats = ["audio is checked by measurement only (loudness, silence, music under speech), not by ear"]
    if not base_render(ctx):
        caveats.append(f"no render of v{it.get('base_n')} existed, so every QA fail counts as new and the render "
                       "lock was skipped")
    cc = {"status": cr.get("status") or ("skipped" if (ctx.outs.get("critic") or {}).get("skipped") else None),
          "findings": crit_rows, "per_note": cr.get("per_note") or [], "skipped": (ctx.outs.get("critic") or {}).get("skipped")}
    cd = card.build(o, N, notes, ledger, questions, fq["checked"], fq["warns"], cc, caveats, passed, not_passed,
                    it.get("base_n"), rend["render"], fq.get("sheet"), fq.get("lessons_sha"), ctx.cost_total(),
                    time.time() - (ctx.started or time.time()))
    cd["plan_file"] = f"plans/{name}"
    cd["skipped_version_note"] = None
    bus.write_json(ctx.dir / "card.json", cd)
    bus.atomic_write(ctx.dir / "card.md", card.markdown(cd))
    rp = Path(cfg.work) / "renders" / rend["render"]
    bus.write_json(rp.with_name(rp.stem + ".card.json"), cd)
    versions_index_update(cfg, f"{slug}_v{N}", plan=slug, version=N, order=o["id"], card=str(ctx.dir / "card.json"),
                          render=rend["render"], theme=th, passed=passed, not_passed=not_passed, at=bus.now_iso(),
                          verdict=None, base_n=it.get("base_n"), plan_file=f"plans/{name}")
    # ---- replies on the comments
    q_by_note = {q.get("note"): q for q in questions if q.get("note")}
    replies = {}
    for n in notes:
        if not n.get("comment"):
            continue
        rows = [r for r in ledger if r["note"] == n["id"]]
        if n["id"] in q_by_note:
            replies[n["comment"]] = ("needs_you", f"Needs you: {q_by_note[n['id']]['line']}")
        elif rows:
            r = rows[0]
            chip = f" [[t:{float(r['t_out']):.2f}]]" if r.get("t_out") is not None else ""
            replies[n["comment"]] = ("done", f"v{N}: {r['change'].rstrip('.')}.{chip}")
        else:
            replies[n["comment"]] = ("done", f"v{N}: nothing to change here (kept as it was).")

    def upd(data):
        for m in data:
            if m.get("id") in replies:
                st, txt = replies[m["id"]]
                m["status"] = st
                m["factory"] = {"order": o["id"], "version": N, "done": True}
                m.setdefault("thread", []).append({"author": "ai", "text": txt[:4000], "ts": time.time(), "status": st,
                                                   "via": "factory"})
                m["updated"] = time.time()
    studiomod.markers_update(cfg, upd)
    ctx.notify_markers()
    # ---- brain: notes ledger + rulings
    brain.add_note_rows(cfg, slug, [{"version": N, "order": o["id"], "note": n["id"], "comment": n.get("comment"),
                                     "words": n["text"], "segments": n["segments"], "keys": n["keys"],
                                     "change": next((r["change"] for r in ledger if r["note"] == n["id"]), None),
                                     "t_out": next((r["t_out"] for r in ledger if r["note"] == n["id"]), None),
                                     "proof": next((r["proof"] for r in ledger if r["note"] == n["id"]), None),
                                     "status": replies.get(n.get("comment"), ("done",))[0]} for n in notes])
    for n in notes:
        if n.get("supersedes"):
            brain.add_decision(cfg, slug, f"segment {', '.join(n['segments'])}", n["text"], "comment",
                               supersedes=n["supersedes"], segments=n["keys"])
    # ---- harvest for the learning loop (never blocks). Only the creator's words can become lessons; critic
    # findings and machine fails go in as `unconfirmed` (a QA bug taught "never add a film look" in the real run).
    items = []
    for n in notes:
        if n.get("text") and n.get("comment"):
            items.append({"source": f"comment:{n['comment']}", "kind": "note", "text": n["text"], "tags": [],
                          "provenance": {"kind": "creator_note", "creator": True, "confirmed": True}})
    for f in cr.get("findings") or []:
        if f.get("sev") == "HARD":
            items.append({"source": f"critic:{o['id']}", "kind": "critic_hard", "text": f.get("text"), "tags": [],
                          "provenance": {"kind": "critic", "creator": False, "confirmed": False,
                                         "finding": f.get("id")}})
    for f in (ctx.outs.get("qa") or {}).get("fails") or []:
        items.append({"source": f"qa:{o['id']}", "kind": "machine_fail", "text": f["text"], "tags": [],
                      "provenance": {"kind": "machine", "creator": False, "confirmed": False, "check": f.get("id")}})
    for n in notes:
        if "restore" in n["flags"]:
            items.append({"source": f"comment:{n.get('comment')}", "kind": "restore", "text": n["text"], "tags": ["process"]})
    prev = {k for r in brain.notes_ledger(cfg, slug) if r.get("version") == it.get("base_n") for k in r.get("keys") or []}
    for n in notes:
        if prev & set(n["keys"]):
            items.append({"source": f"comment:{n.get('comment')}", "kind": "repeat_note",
                          "text": f"noted again after v{it.get('base_n')}: {n['text']}", "tags": []})
    lessons.harvest(cfg, items, order=o["id"])
    state = "ready" if passed else "needs_you"
    why = None if passed else "not passed: " + card.cut_words(not_passed[0], 200)
    if not passed:
        questions = card.clean_questions([{"text": f"v{N} did not pass ({card.cut_words(not_passed[0], 120)}).",
                                           "options": ["I'll look at it as it is", "send it back for another round"],
                                           "kind": "notpassed"}]) + questions
    return {"state": state, "why": why, "version": N, "file": name, "passed": passed, "not_passed": not_passed,
            "questions": questions, "card": str(ctx.dir / "card.json"), "render": rend["render"], "theme": th}


def lessons_phase(ctx):
    items = lessons.inbox(ctx.cfg, order=ctx.order["id"])[:10]      # creator / creator-confirmed items only
    if not items:
        raise Skip("nothing from the creator in this order's inbox" + (
            f" ({len(lessons.unconfirmed(ctx.cfg, ctx.order['id']))} critic/machine item(s) wait for the creator "
            "to confirm)" if lessons.unconfirmed(ctx.cfg, ctx.order["id"]) else ""))
    have = [x for x in lessons.load(ctx.cfg, "project")["lessons"] if x.get("status") == "active"]
    sections = [("This order's inbox", "\n".join(f"- {x['id']} [{x['kind']}] {x['text']}" for x in items)),
                ("Project lessons already active", "\n".join(f"- {x['id']}: {x['rule']}" for x in have[-40:])),
                ("Check types for check_json", lint.__doc__.split("Check types (params):")[1])]
    p = roles.build("lessons", order=ctx.order, sections=sections, context={})
    try:
        r = ctx.role("lessons", p, quiet=True)
    except (Blocked, Requeue, NeedsYou, Stopped) as e:
        return {"error": str(e)[:300]}
    done = lessons.apply_ops(ctx.cfg, (r["result_json"] or {}).get("ops"), order=ctx.order["id"])
    return {"ops": done}


# ================================================================================================ variants
def variants_edit(ctx):
    it, pr = _intake(ctx), ctx.outs.get("producer") or {}
    n_var = max(2, min(3, int(ctx.order.get("variants") or 3)))
    vd = ctx.dir / "variants"
    vd.mkdir(exist_ok=True)
    base = _plan_load(it["base_path"])
    base["version"] = it["version"]
    paths = []
    for k in range(1, n_var + 1):
        p = vd / f"var{k}.json"
        bus.write_json(p, base)
        paths.append(str(p))
    br = lessons.brief(ctx.cfg, ctx.order["plan"], tags=lessons.tags_for(pr.get("changes")))
    sections = [("The note to try options for", _note_lines(it["notes"])),
                ("Make options", f"Write {n_var} DIFFERENT candidates, one per file below, each a copy of the base "
                 "that differs ONLY in the noted segments (different shots / trims for the same note). In `variants` "
                 "give each one's name (a few words), candidate path, segments and a one-line summary.")]
    p = roles.build("variants", order=ctx.order, brief_text=br["text"], sections=sections, context={
        "variant files (edit each in place)": paths, "base plan": it["base_path"]})
    r = ctx.role("variants", p, env={"CUTROOM_FACTORY_BASE_PLAN": it["base_path"], "CUTROOM_FACTORY_VARIANTS": ",".join(paths)})
    res = r["result_json"]
    out = []
    for k, path in enumerate(paths):
        meta = next((v for v in res.get("variants") or [] if Path(str(v.get("candidate"))).name == Path(path).name), {})
        plan = _plan_load(path)
        plan["version"] = it["version"]
        bus.write_json(path, plan)
        errs = _validate(ctx, plan)
        out.append({"k": k + 1, "path": path, "name": meta.get("name") or f"option {k + 1}", "summary": meta.get("summary"),
                    "segments": meta.get("segments") or [], "errors": errs[:3]})
    return {"variants": out}


def _excerpt_plan(plan, tl, a, b):
    """Only the segments whose timeline span overlaps [a, b] (same section structure)."""
    keep = {(r["sec"], r["seg"]) for r in tl.get("segments") or [] if r["end"] > a and r["start"] < b and r["sec"] >= 0}
    out = copy.deepcopy(plan)
    out.pop("cold_open", None)
    secs = []
    for si, sec in enumerate(out.get("sections") or []):
        segs = [s for gi, s in enumerate(sec.get("segments") or []) if (si, gi) in keep]
        if segs:
            secs.append({**sec, "segments": segs})
    out["sections"] = secs or out.get("sections", [])[:1]
    return out


def variants_render(ctx):
    it = _intake(ctx)
    n0 = next((n for n in it["notes"] if n.get("t") is not None), None)
    a, b = ((n0["window"][0] - 5, n0["window"][1] + 5) if n0 else (0, 20))
    out = []
    for v in ctx.outs["variants_edit"]["variants"]:
        if v["errors"]:
            out.append({**v, "render": None, "error": v["errors"][0]})
            continue
        plan = _plan_load(v["path"])
        ex = _excerpt_plan(plan, _resolve(ctx, plan), max(0.0, a), b)
        ep = Path(v["path"]).with_name(f"var{v['k']}_excerpt.json")
        bus.write_json(ep, ex)
        dest = Path(ctx.cfg.work) / "renders" / f"{ctx.order['plan']}_v{it['version']}_var{v['k']}_preview.mp4"
        r = do_render(ctx, ep, dest)
        out.append({**v, "render": r["render"], "excerpt": [round(max(0.0, a), 2), round(b, 2)]})
    return {"variants": out}


def variants_integrate(ctx):
    it = _intake(ctx)
    vs = ctx.outs["variants_render"]["variants"]
    cd = {"v": 1, "order": ctx.order["id"], "kind": "variants", "plan": ctx.order["plan"], "version": it["version"],
          "base_version": it.get("base_n"), "variants": [{k: v.get(k) for k in ("k", "name", "summary", "render", "path",
                                                                                "segments", "excerpt", "error")} for v in vs],
          "notes": [{"note": n["id"], "comment": n.get("comment"), "words": n["text"], "t": n.get("t")} for n in it["notes"]],
          "theme": f"options for {it['notes'][0]['text'][:60]}" if it["notes"] else "options"}
    bus.write_json(ctx.dir / "card.json", cd)
    ok = [v for v in vs if v.get("render")]
    return {"state": "ready", "why": None, "version": it["version"], "questions": [], "card": str(ctx.dir / "card.json"),
            "variants": len(ok)}


# ================================================================================================ approve (code only)
def approve_intake(ctx):
    o = ctx.order
    f = o.get("base_file")
    if not f or not (Path(ctx.cfg.work) / "plans" / f).exists():
        raise Blocked(f"approved version {f} not found")
    m = bus.VERSION_RE.match(f)
    return {"base_file": f, "version": int(m.group("n")) if m else None, "base_path": str(Path(ctx.cfg.work) / "plans" / f),
            "notes": [], "base_n": int(m.group("n")) if m else None}


def render_final(ctx):
    it = ctx.outs["approve_intake"]
    out = Path(ctx.cfg.work) / "renders" / f"{ctx.order['plan']}_v{it['version']}.mp4"
    return do_render(ctx, it["base_path"], out, preview=False)


def qa_final(ctx):
    it = ctx.outs["approve_intake"]
    video = Path(ctx.cfg.work) / "renders" / ctx.outs["render_final"]["render"]
    rep, err = gates.run_qa(ctx.cfg, video, it["base_path"], ctx.pdir / "qa.log", timeout=ctx.wall("qa"),
                            stop_check=ctx.stop_check)
    link = gates.link_check(video, ctx.link_url(ctx.outs["render_final"]["render"]))
    return {"qa_summary": (rep or {}).get("summary"), "error": err, "link": link}


def approve_record(ctx):
    it = ctx.outs["approve_intake"]
    q = ctx.outs["qa_final"]
    st = (q.get("qa_summary") or {}).get("status")
    versions_index_update(ctx.cfg, f"{ctx.order['plan']}_v{it['version']}", final_render=ctx.outs["render_final"]["render"],
                          final_qa=st, final_at=bus.now_iso())
    return {"state": "done", "why": None, "version": it["version"], "questions": [],
            "text": f"v{it['version']} approved; full-quality render ready ({ctx.outs['render_final']['render']}, QA {st}). "
                    "Nothing was published."}


FUNCS = {"intake": intake, "producer": producer, "forensics": forensics, "extend": extend, "edit": edit,
         "render": render, "qa": qa, "critic": critic, "fix": fix, "render2": render2, "qa2": qa2, "verify": verify,
         "integrate": integrate, "lessons": lessons_phase, "variants_edit": variants_edit,
         "variants_render": variants_render, "variants_integrate": variants_integrate, "approve_intake": approve_intake,
         "render_final": render_final, "qa_final": qa_final, "approve_record": approve_record, "extend_stub": extend_stub}
