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
    "grade": {"lut": "", "normalize_exposure": true, "saturation": 1.0},
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
"sfx": [{"name": "whoosh", "at": 0.0, "gain_db": -6}], "note": "why this segment exists".
"""
import copy
import json
from pathlib import Path

SEG_TYPES = {"talk", "broll", "montage", "matchcut", "title", "black", "still"}
OVERLAY_KINDS = {"callout", "lower_third", "title", "label"}
POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center", "auto"}

DEFAULT_STYLE = {
    "captions": {"enabled": True, "font": "", "size": 54, "position": "bottom", "max_words": 5, "max_chars": 30,
                 "case": "sentence", "fixes": []},
    "grade": {"lut": "", "normalize_exposure": True, "saturation": 1.0},
    "voice": {"clean": True, "atten_lim_db": 20},
    "music_under_speech": "mute",
}


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
        for s in sec.get("segments", []):
            t = s.get("type")
            if t == "talk":
                s.setdefault("tighten", True); s.setdefault("gap", 0.35); s.setdefault("captions", True)
                s.setdefault("cutaways", []); s.setdefault("overlays", [])
            elif t == "broll":
                s.setdefault("speed", 1.0); s.setdefault("audio", "music"); s.setdefault("overlays", [])
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
    errs = []
    if not plan.get("sections"):
        errs.append("plan has no sections")

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

    for si, sec in enumerate(plan.get("sections", [])):
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
            for k, o in enumerate(s.get("overlays", []) or []):
                if o.get("kind") not in OVERLAY_KINDS:
                    errs.append(f"{w} overlay {k}: kind must be one of {sorted(OVERLAY_KINDS)}")
                if o.get("position", "auto") not in POSITIONS:
                    errs.append(f"{w} overlay {k}: position must be one of {sorted(POSITIONS)}")
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
