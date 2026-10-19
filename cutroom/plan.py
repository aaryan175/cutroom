"""Edit plans: the one document the operator (Claude) writes and the renderer reads.

A plan is JSON. Times are seconds in the SOURCE clip unless a field says otherwise. Clips are
referenced by id, absolute path, relative path or a unique filename fragment (see db.get_clip).

{
  "title": "Day in the life - draft 3",
  "version": 3,
  "output": {"width": 1920, "height": 1080, "fps": 25},          # optional; defaults from config
  "style": {
    "captions": {"enabled": true, "font": "", "size": 54, "position": "bottom",   # bottom|lower|top
                 "max_words": 5, "max_chars": 30, "case": "sentence",             # sentence|lower|upper
                 "fixes": [["teh", "the"]]},                                       # regex replacements per cue
    "grade": {"lut": "", "normalize_exposure": true, "saturation": 1.0,
              "look": {"name": "warm_film", "intensity": 0.8}},           # optional; see cutroom/looks.py
    "voice": {"clean": true, "atten_lim_db": 20},                # DeepFilterNet ceiling; null = off
    "music_under_speech": "mute"                                   # mute (default: no music under talking) | duck | full
  },
  "sections": [
    {
      "name": "cold open",
      "music": {"song": "song-id-or-path", "offset": 42.0, "gain_db": -3, "duck_db": -18},  # optional
      "segments": [ <segment>, ... ]
    }
  ]
}

Segment types (field "type"):
  talk     {"clip", "in", "out", "tighten": true, "gap": 0.35,                 # speech, pauses cut
            "captions": true,
            "cutaways": [{"clip", "in", "at", "dur"}],                          # b-roll over the voice; "at" = seconds into the segment
            "overlays": [<overlay>]}
  broll    {"clip", "in", "out", "speed": 1.0, "audio": "music"|"natural"|"mix", "stabilize": false}
  montage  {"shots": [{"clip", "in", "out"?}], "beat_sync": true, "beats_per_cut": 1,
            "max_shot": 1.6, "min_shot": 0.4, "audio": "music"}
  matchcut {"a": {"clip", "t"}, "b": {"clip", "t"}, "pre": 0.8, "post": 1.4}  # A ends exactly at a.t, B starts exactly at b.t
  title    {"text", "sub": "", "dur": 2.0, "bg": "black"|"blur:<clip>@<t>"}
  black    {"dur": 1.0}
  still    {"clip", "t", "dur", "zoom": 1.08}                                   # freeze-frame with slow push

Overlay (on talk/broll/montage segments): {"kind": "callout"|"lower_third"|"title"|"label",
   "text", "sub"?, "at": 0.3, "dur": 2.5, "position": "top-left"|"top-right"|"bottom-left"|"bottom-right"|"center"}
Callouts default to the corner AWAY from detected faces when "position" is "auto".

Common optional segment fields: "speed", "zoom", "crop": "center"|"face"|[x, y, w, h] (fractions),
"look": {"name", "intensity"} (overrides style.grade.look for this segment; "natural" = no look),
"sfx": [{"name": "whoosh", "at": 0.0, "gain_db": -6}] or [{"name": "hit", "on": "cut"|"end"}] (the sound's
transient peak lands on that frame), "note": "why this segment exists".

Craft fields (docs/PLAN_FORMAT.md "Craft"; defaults measured from a reference vlog):
  style.grade.exposure   {"strength": 0.5, "max_stops": 1.0, "deadband_stops": 0.33, "target_luma": 118,
                          "night_luma": 60, "night_target": 70}   log-domain exposure BEFORE the LUT
  style.grade.finish     {"enabled": true, "black": 0.028, "shoulder": 0.96, "vibrance": 0.18}  one curve, every shot
  style.audio            {"join_xfade_s": 0.02, "room_tone": true, "room_tone_under_voice_db": -32,
                          "bed_fade_s": 0.3, "jl_duck_db": -12, "auto_lead_in": 0.3, "auto_tail": 0.0}
  style.conform          "auto" | "speed" | "blend" | "dup"     frame-rate conform of 23.976/29.97/50/60 sources
  style.auto_push        null | {"min_hold": 4.0, "to": 1.04}   slow push-in on long held talk shots
  section.music          + "start"/"end": "cut" | {"fade": s}, "montage_gain_db": 7, "natural_duck_db": 0,
                          "mode": "under" (+ "under_db": -5, never ducked), "breaks": [{"at": seg-rel s | "before": "cut:<seg>", "dur": 0.3}]
  section.transition     transition into the section's first segment (same shape as segment.transition)
  section.pace           {"asl": 0.6, "max_hold": 6.0, "alternate": true}
  segment.transition     {"kind": "flash"|"dip"|"dip_white"|"whip"|"cut", "frames": 4, "sfx": "hit"|"whoosh"|null,
                          "gain_db": -6, "break": 0.3}         on the cut INTO this segment
  talk                   + "lead_in": s (J-cut: voice starts under the previous shot), "tail": s (L-cut),
                          "room_tone": true
  broll                  "audio" default "natural" (ambience under the music; "music" = none)
  any clip segment/shot  + "push": {"to": 1.06, "ease": "inout"|"linear", "x": 0.5, "y": 0.5},
                          "ramp": {"to": 2.0, "over": 0.4} (broll/montage), "conform", "exposure_stops"
  montage shot           + "dur": planned length when not beat-synced
  plan.cold_open         {"style": "teaser", "dur": 10, "lines": [{"clip", "in", "out"} | {"from": "<section>/<seg>"}],
                          "shots": "auto" | [{"clip", "in"}], "from_reference": "<ref id>", "asl": 0.45,
                          "music": {"song", "offset", "under_db": -5}, "face_s": 0.8, "end_flash": true}
"""
import copy
import json
from pathlib import Path

SEG_TYPES = {"talk", "broll", "montage", "matchcut", "title", "black", "still"}
OVERLAY_KINDS = {"callout", "lower_third", "title", "label"}
POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center", "auto"}

TRANSITION_KINDS = {"flash", "dip", "dip_black", "dip_white", "whip", "cut"}
CONFORM_MODES = {"auto", "speed", "blend", "dup"}
CAPTION_SIZE_1080 = 43           # font px at 1080; the reference vlog's text measures ~3 % of frame height; scaled with output height

DEFAULT_STYLE = {
    "captions": {"enabled": True, "font": "", "size": CAPTION_SIZE_1080, "position": "bottom", "max_words": 5,
                 "max_chars": 30, "case": "sentence", "fixes": []},
    "grade": {"lut": "", "normalize_exposure": True, "saturation": 1.0, "look": None,
              "exposure": {"strength": 0.5, "max_stops": 1.0, "deadband_stops": 0.33, "target_luma": 118.0,
                           "night_luma": 60.0, "night_target": 70.0},
              "finish": {"enabled": True, "black": 0.028, "shoulder": 0.96, "vibrance": 0.18}},
    "voice": {"clean": True, "atten_lim_db": 20},
    "audio": {"join_xfade_s": 0.02, "room_tone": True, "room_tone_under_voice_db": -32.0, "bed_fade_s": 0.3,
              "jl_duck_db": -12.0, "auto_lead_in": 0.3, "auto_tail": 0.0},
    "conform": "auto",
    "auto_push": None,
    "music_under_speech": "mute",
}
MUSIC_DEFAULTS = {"start": "cut", "end": "cut", "montage_gain_db": 7.0, "natural_duck_db": 0.0, "under_db": -5.0,
                  "breaks": []}
TRANSITION_DEFAULTS = {"flash": {"frames": 4, "sfx": "hit", "gain_db": -6.0, "break": 0.3},
                       "whip": {"frames": 6, "sfx": "whoosh", "gain_db": -10.0, "break": 0.3},
                       "dip": {"frames": 4, "sfx": None, "gain_db": -6.0, "break": 0.0},
                       "dip_black": {"frames": 4, "sfx": None, "gain_db": -6.0, "break": 0.0},
                       "dip_white": {"frames": 4, "sfx": None, "gain_db": -6.0, "break": 0.0},
                       "cut": {"frames": 0, "sfx": None, "gain_db": -6.0, "break": 0.0}}


def transition_spec(tr):
    """Normalised transition dict (kind, frames_out, frames_in, sfx, gain_db, break) or None for none."""
    if not tr:
        return None
    if isinstance(tr, str):
        tr = {"kind": tr}
    kind = tr.get("kind", "flash")
    if kind == "dip_black":
        kind = "dip"
    d = {**TRANSITION_DEFAULTS.get(kind, TRANSITION_DEFAULTS["flash"]), **tr, "kind": kind}
    fr = int(d.get("frames") or 0)
    fo = d.get("frames_out")
    fi = d.get("frames_in")
    fo = int(fo) if fo is not None else fr // 2
    fi = int(fi) if fi is not None else fr - fr // 2
    return {"kind": kind, "frames_out": max(0, fo), "frames_in": max(0, fi), "sfx": d.get("sfx"),
            "gain_db": float(d.get("gain_db", -6.0)), "break": float(d.get("break") or 0.0)}


class PlanError(ValueError):
    pass


def load(path) -> dict:
    return json.loads(Path(path).read_text())


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def normalize(plan: dict, cfg=None) -> dict:
    """Fill defaults; does not touch the library."""
    p = copy.deepcopy(plan)
    p.setdefault("title", "untitled")
    p.setdefault("version", 1)
    out = {"width": getattr(cfg, "out_width", 1920), "height": getattr(cfg, "out_height", 1080),
           "fps": getattr(cfg, "out_fps", 25.0)}
    p["output"] = {**out, **p.get("output", {})}
    p["style"] = _merge(DEFAULT_STYLE, p.get("style", {}))
    for sec in p.get("sections", []):
        if isinstance(sec.get("music"), dict) and sec["music"].get("song"):
            sec["music"] = {**MUSIC_DEFAULTS, **sec["music"]}
        for s in sec.get("segments", []):
            t = s.get("type")
            if t == "talk":
                s.setdefault("tighten", True); s.setdefault("gap", 0.35); s.setdefault("captions", True)
                s.setdefault("cutaways", []); s.setdefault("overlays", [])
            elif t == "broll":
                s.setdefault("speed", 1.0); s.setdefault("audio", "natural"); s.setdefault("overlays", [])
            elif t == "montage":
                s.setdefault("beat_sync", True); s.setdefault("beats_per_cut", 1)
                s.setdefault("max_shot", 1.6); s.setdefault("min_shot", 0.4); s.setdefault("audio", "music")
                s.setdefault("overlays", [])
            elif t == "matchcut":
                s.setdefault("pre", 0.8); s.setdefault("post", 1.4)
            elif t == "title":
                s.setdefault("sub", ""); s.setdefault("dur", 2.0); s.setdefault("bg", "black")
            elif t == "still":
                s.setdefault("dur", 2.0); s.setdefault("zoom", 1.08)
    return p


def validate(plan: dict, con=None) -> list:
    """Return a list of human-readable problems (empty = valid). With `con`, also checks clips
    exist and in/out points fall inside each clip's duration."""
    from . import db as _db
    from .looks import validate_look
    errs = []
    if not plan.get("sections"):
        errs.append("plan has no sections")
    grade = (plan.get("style") or {}).get("grade") or {}
    if isinstance(grade, dict) and grade.get("look") is not None:
        errs += validate_look(grade["look"], "style.grade.look")

    def chk_clip(where, ref, *times):
        if con is None or ref is None:
            return
        try:
            c = _db.get_clip(con, ref)
        except KeyError as e:
            errs.append(f"{where}: {e}")
            return
        for t in times:
            if t is not None and not (0 <= float(t) <= (c["duration"] or 0) + 0.01):
                errs.append(f"{where}: time {t} outside clip {c['rel']} (0-{c['duration']:.2f}s)")

    errs += _validate_style(plan.get("style") or {})
    co = plan.get("cold_open")
    if co:
        errs += _validate_cold_open(co, plan, chk_clip)
    for si, sec in enumerate(plan.get("sections", [])):
        ws = f"section {si} ({sec.get('name', '')})"
        errs += _validate_section(ws, sec)
        for gi, s in enumerate(sec.get("segments", [])):
            w = f"section {si} ({sec.get('name', '')}) segment {gi}"
            t = s.get("type")
            if t not in SEG_TYPES:
                errs.append(f"{w}: unknown type {t!r}")
                continue
            if t in ("talk", "broll"):
                if s.get("in") is None or s.get("out") is None or float(s["out"]) <= float(s["in"]):
                    errs.append(f"{w}: needs in < out")
                chk_clip(w, s.get("clip"), s.get("in"), s.get("out"))
                for k, c in enumerate(s.get("cutaways", [])):
                    chk_clip(f"{w} cutaway {k}", c.get("clip"), c.get("in"))
                    if c.get("dur", 0) <= 0:
                        errs.append(f"{w} cutaway {k}: dur must be > 0")
            elif t == "montage":
                if not s.get("shots"):
                    errs.append(f"{w}: montage needs shots")
                for k, sh in enumerate(s.get("shots", [])):
                    chk_clip(f"{w} shot {k}", sh.get("clip"), sh.get("in"), sh.get("out"))
                    if sh.get("dur") is not None and not 0.04 <= _num(sh["dur"], -1) <= 30:
                        errs.append(f"{w} shot {k}: dur must be 0.04-30 s")
                    errs += _validate_craft(f"{w} shot {k}", "shot", sh)
            elif t == "matchcut":
                for side in ("a", "b"):
                    if not s.get(side) or s[side].get("t") is None:
                        errs.append(f"{w}: matchcut needs {side}.clip and {side}.t")
                    else:
                        chk_clip(f"{w} {side}", s[side].get("clip"), s[side]["t"])
            elif t in ("title", "black", "still"):
                if t != "black" and t == "title" and not s.get("text"):
                    errs.append(f"{w}: title needs text")
                if t == "still":
                    chk_clip(w, s.get("clip"), s.get("t"))
            if s.get("look") is not None:
                errs += validate_look(s["look"], f"{w} look")
            errs += _validate_craft(w, t, s)
            for k, o in enumerate(s.get("overlays", []) or []):
                if o.get("kind") not in OVERLAY_KINDS:
                    errs.append(f"{w} overlay {k}: kind must be one of {sorted(OVERLAY_KINDS)}")
                if o.get("position", "auto") not in POSITIONS:
                    errs.append(f"{w} overlay {k}: position must be one of {sorted(POSITIONS)}")
    return errs


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _validate_transition(where, tr):
    if not tr:
        return []
    if isinstance(tr, str):
        tr = {"kind": tr}
    if not isinstance(tr, dict):
        return [f"{where}: transition must be an object or a kind name"]
    errs = []
    if tr.get("kind", "flash") not in TRANSITION_KINDS:
        errs.append(f"{where}: transition kind must be one of {sorted(TRANSITION_KINDS)}")
    for k, lo, hi in (("frames", 0, 12), ("frames_out", 0, 8), ("frames_in", 0, 8), ("break", 0, 1.0),
                      ("gain_db", -40, 6)):
        if tr.get(k) is not None and not lo <= _num(tr[k], lo - 1) <= hi:
            errs.append(f"{where}: transition {k} must be {lo}-{hi}")
    sp = transition_spec(tr) if not errs else None
    if sp and sp["kind"] != "cut" and sp["frames_out"] + sp["frames_in"] < 2:
        errs.append(f"{where}: a {sp['kind']} needs frames >= 2")
    if tr.get("sfx") not in (None, "", False) and not isinstance(tr.get("sfx"), str):
        errs.append(f"{where}: transition sfx must be a sound name or null")
    return errs


def _validate_craft(w, t, s):
    """Craft fields on a segment or montage shot."""
    errs = []
    if t != "shot":
        errs += _validate_transition(w, s.get("transition"))
    for k in ("lead_in", "tail"):
        if s.get(k) is not None:
            if t != "talk":
                errs.append(f"{w}: {k} only applies to talk segments")
            elif not 0 <= _num(s[k], -1) <= 2.0:
                errs.append(f"{w}: {k} must be 0-2 s")
    pu = s.get("push")
    if pu is not None and pu is not False:
        to = _num(pu.get("to") if isinstance(pu, dict) else pu, -1)
        if not 0.85 <= to <= 1.2:
            errs.append(f"{w}: push.to must be 0.85-1.2 (a 3-8 % move reads as a slider; > 1.08 softens 1080p sources)")
        if isinstance(pu, dict) and pu.get("ease", "inout") not in ("inout", "linear", "in", "out"):
            errs.append(f"{w}: push.ease must be inout|linear|in|out")
    rp = s.get("ramp")
    if rp is not None and rp is not False:
        if t not in ("broll", "montage", "shot"):
            errs.append(f"{w}: ramp applies to broll and montage shots only (never to sync talk)")
        elif not isinstance(rp, dict) or not 1.1 <= _num(rp.get("to"), 0) <= 8 or not 0.12 <= _num(rp.get("over", 0.4), 0) <= 3:
            errs.append(f"{w}: ramp must be {{\"to\": 1.1-8, \"over\": 0.12-3 s}}")
    if s.get("conform") is not None and s["conform"] not in CONFORM_MODES:
        errs.append(f"{w}: conform must be one of {sorted(CONFORM_MODES)}")
    if s.get("exposure_stops") is not None and not -2.0 <= _num(s["exposure_stops"], 9) <= 2.0:
        errs.append(f"{w}: exposure_stops must be -2..2")
    if t == "broll" and s.get("audio") not in (None, "music", "natural", "mix", "none"):
        errs.append(f"{w}: broll audio must be music|natural|mix")
    for k, e in enumerate(s.get("sfx", []) or []):
        if e.get("on") is not None and e["on"] not in ("cut", "end", "transition"):
            errs.append(f"{w} sfx {k}: on must be cut|end|transition")
    return errs


def _validate_section(w, sec):
    errs = _validate_transition(w, sec.get("transition"))
    m = sec.get("music") or {}
    if m:
        for k in ("start", "end"):
            v = m.get(k)
            if v is not None and v != "cut" and not (isinstance(v, dict) and _num(v.get("fade"), -1) >= 0):
                errs.append(f"{w} music: {k} must be \"cut\" or {{\"fade\": seconds}}")
        if m.get("mode") not in (None, "duck", "mute", "full", "under"):
            errs.append(f"{w} music: mode must be duck|mute|full|under")
        if m.get("montage_gain_db") is not None and not 0 <= _num(m["montage_gain_db"], -1) <= 12:
            errs.append(f"{w} music: montage_gain_db must be 0-12")
        if m.get("natural_duck_db") is not None and not -6 <= _num(m["natural_duck_db"], 1) <= 0:
            errs.append(f"{w} music: natural_duck_db must be -6..0 (ambience never ducks music hard)")
        for k, b in enumerate(m.get("breaks") or []):
            if not isinstance(b, dict) or (b.get("at") is None and b.get("before") is None) \
                    or not 0.05 <= _num(b.get("dur", 0.3), 0) <= 1.0:
                errs.append(f"{w} music break {k}: needs at|before and dur 0.05-1.0 s")
    pc = sec.get("pace")
    if pc is not None:
        if not isinstance(pc, dict):
            errs.append(f"{w}: pace must be an object")
        else:
            if pc.get("asl") is not None and not 0.12 <= _num(pc["asl"], 0) <= 30:
                errs.append(f"{w}: pace.asl must be 0.12-30 s")
            if pc.get("max_hold") is not None and not 0.5 <= _num(pc["max_hold"], 0) <= 120:
                errs.append(f"{w}: pace.max_hold must be 0.5-120 s")
    return errs


def _validate_style(st):
    errs = []
    if st.get("conform") is not None and st["conform"] not in CONFORM_MODES:
        errs.append(f"style.conform must be one of {sorted(CONFORM_MODES)}")
    ex = ((st.get("grade") or {}).get("exposure")) or {}
    if ex and not 0 <= _num(ex.get("max_stops", 1), -1) <= 2:
        errs.append("style.grade.exposure.max_stops must be 0-2")
    au = st.get("audio") or {}
    if au.get("join_xfade_s") is not None and not 0.0 <= _num(au["join_xfade_s"], -1) <= 0.12:
        errs.append("style.audio.join_xfade_s must be 0-0.12 s")
    ap = st.get("auto_push")
    if ap and (not isinstance(ap, dict) or not 1.0 < _num(ap.get("to", 1.04), 0) <= 1.1):
        errs.append("style.auto_push must be {\"min_hold\": s, \"to\": 1.01-1.1}")
    cs = st.get("captions") or {}
    if cs.get("size") is not None and not 12 <= _num(cs["size"], 0) <= 120:
        errs.append("style.captions.size must be 12-120 (px at 1080; reference 32)")
    return errs


def _validate_cold_open(co, plan, chk_clip):
    errs = []
    if not isinstance(co, dict):
        return ["cold_open must be an object"]
    if co.get("style", "teaser") != "teaser":
        errs.append("cold_open.style must be \"teaser\"")
    if not 2.0 <= _num(co.get("dur", 10), 0) <= 30:
        errs.append("cold_open.dur must be 2-30 s")
    lines = co.get("lines") or []
    if not isinstance(lines, list):
        errs.append("cold_open.lines must be a list")
        lines = []
    names = [str(s.get("name", "")).lower() for s in plan.get("sections", [])]
    for k, ln in enumerate(lines):
        w = f"cold_open line {k}"
        if isinstance(ln, str):
            ln = {"from": ln}
        if ln.get("from") is not None:
            sec_ref = str(ln["from"]).split("/")[0].strip().lower()
            if not (sec_ref.isdigit() and int(sec_ref) < len(names)) and sec_ref not in names:
                errs.append(f"{w}: from {ln['from']!r} names no section ({names})")
        elif ln.get("clip") is None or ln.get("in") is None or ln.get("out") is None:
            errs.append(f"{w}: needs clip/in/out or from: \"<section>/<segment>\"")
        else:
            if not 0.3 <= _num(ln["out"], 0) - _num(ln["in"], 0) <= 6:
                errs.append(f"{w}: a teaser line is 0.3-6 s long")
            chk_clip(w, ln.get("clip"), ln.get("in"), ln.get("out"))
    sh = co.get("shots", "auto")
    if sh != "auto" and not isinstance(sh, list):
        errs.append("cold_open.shots must be \"auto\" or a list of {clip, in}")
    elif isinstance(sh, list):
        for k, x in enumerate(sh):
            chk_clip(f"cold_open shot {k}", x.get("clip"), x.get("in"))
    if co.get("asl") is not None and not 0.12 <= _num(co["asl"], 0) <= 2:
        errs.append("cold_open.asl must be 0.12-2 s")
    return errs


def estimate_duration(plan: dict) -> float:
    """Rough runtime before tightening/beat-snapping (talk pauses will shorten it)."""
    tot = 0.0
    for sec in plan.get("sections", []):
        for s in sec.get("segments", []):
            t = s.get("type")
            if t in ("talk", "broll"):
                tot += (float(s["out"]) - float(s["in"])) / float(s.get("speed", 1.0) or 1.0)
            elif t == "montage":
                tot += sum(min(float(s.get("max_shot", 1.6)),
                               float(sh.get("out", sh["in"] + s.get("max_shot", 1.6))) - float(sh["in"]))
                           for sh in s.get("shots", []))
            elif t == "matchcut":
                tot += float(s.get("pre", 0.8)) + float(s.get("post", 1.4))
            else:
                tot += float(s.get("dur", 1.0))
    return tot
