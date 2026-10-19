"""Deterministic intake: each review comment -> the pieces it lands on, the words said there, frames, flags.

This is what the long v023 session did by hand every round (dump the plan, map each timestamp to its
segment, read the words in that span), made into code so every role starts from the same facts.

    map_comment(cfg, con, tl, plan, marker, n) -> note
        {id: "n3", comment, t, t_end, text, drawing, frame, window: [a, b],
         pieces: [{track, kind, type, sec, seg, ref: "s.g", key, clip, clip_rel, src_in, src_out, start, end,
                   words, captions, overlays, label}],
         segments: ["s.g", ...], keys: [segment content keys], words: "...", flags: [...],
         supersedes: decision id | None, frames: [clean frame paths]}

A point comment covers [t - 1, t + 1]; a range comment [t, t_end] (EDITING_RULES: a range note means the
whole span).

The generated cold open (section index -1: card, strobe blocks, teaser lines, the held end shot and its typed
title) is not a plan section, so its pieces map to `cold_open.*` refs of the plan's `cold_open` block
(`cold_open.card`, `cold_open.shots`, `cold_open.lines[k]`, `cold_open.end_shot`, `cold_open.title`, plus
`cold_open` itself). In the real run every note on the intro said "0 segments" and the lock check could not
tell a cold-open change from an unexplained one. Output (timeline) seconds are mapped to source seconds through the resolved pieces, never by
subtracting across a jump cut.
"""
import re
from pathlib import Path

from . import brain

WINDOW = 1.0
FLAG_RES = {
    "restore": re.compile(r"\b(better before|was (good|better) before|go back|put (it|that) back|restore|"
                          r"like (it was|before)|revert|i don'?t know why you changed)\b", re.I),
    "remove": re.compile(r"\b(remove|get rid of|don'?t need|do not need|unneeded|not needed|cut (it|this|that)|"
                         r"delete|drop (it|this|that)|no need for|take (it|this|that) out)\b", re.I),
    "conditional_approval": re.compile(r"\bapprove[ds]?\b.{0,40}\b(but|except|just|only)\b", re.I),
    "praise": re.compile(r"\b(i (really )?(like|love)|looks (so )?good|is good|so good|perfect|keep (it|this|that)|"
                         r"nice|great)\b", re.I),
    "question": re.compile(r"\?\s*$"),
}
NEGATED_PRAISE = re.compile(r"\b(not|n'?t|never)\s+(\w+\s+){0,2}(good|great|nice|like|love|perfect)\b", re.I)


def flags_of(text) -> list:
    t = str(text or "")
    out = [k for k, rx in FLAG_RES.items() if rx.search(t)]
    if "praise" in out and (NEGATED_PRAISE.search(t) or "remove" in out or "restore" in out):
        out.remove("praise")
    return out


def _words_in(cfg, clip, a, b):
    if not clip or a is None or b is None or b <= a:
        return ""
    from .. import asr
    try:
        ws = asr.words(cfg, clip)
    except Exception:  # noqa: BLE001 - no transcript = no words
        return ""
    return " ".join(w for s, e, w in ws if s < b and e > a)


def _seg_of(plan, sec, seg):
    try:
        return plan["sections"][sec]["segments"][seg]
    except (KeyError, IndexError, TypeError):
        return None


CO_TYPES = {"card": "cold_open.card", "montage": "cold_open.shots", "block": "cold_open.shots"}


def cold_open_refs(tl) -> dict:
    """{(sec, seg) of the generated cold open: ref} from the resolved timeline. Teaser lines are numbered in
    timeline order (the plan's cold_open.lines order); the last picture segment that is not a line is the held
    end shot."""
    segs = sorted({(p.get("seg"), p.get("type")) for p in tl.get("pieces") or []
                   if p.get("sec") == -1 and p.get("track") == "V1" and isinstance(p.get("seg"), int)},
                  key=lambda x: x[0])
    out, li = {}, 0
    last_pic = max((g for g, ty in segs if ty not in ("talk", "card")), default=None)
    for g, ty in segs:
        if ty == "talk":
            out[(-1, g)] = f"cold_open.lines[{li}]"
            li += 1
        elif g == last_pic and ty not in CO_TYPES:
            out[(-1, g)] = "cold_open.end_shot"
        elif g == last_pic and ty in ("montage", "block") and any(p.get("sec") == -1 and p.get("seg") == g and p.get("track") == "V3"
                                                                      for p in tl.get("pieces") or []):
            out[(-1, g)] = "cold_open.end_shot"
        else:
            out[(-1, g)] = CO_TYPES.get(ty, "cold_open.shots")
    return out


def cold_open_span(tl):
    """[start, end] of the generated cold open on the timeline, or None."""
    xs = [(p["start"], p["end"]) for p in tl.get("pieces") or [] if p.get("sec") == -1 and p.get("track") == "V1"]
    return [round(min(a for a, _ in xs), 3), round(max(b for _, b in xs), 3)] if xs else None


def _piece_ref(p, co_refs):
    """The note-level ref of a piece: "s.g" for a plan segment, a cold_open.* ref for the generated intro."""
    sec, sg = p.get("sec"), p.get("seg")
    if isinstance(sec, int) and sec >= 0:
        return f"{sec}.{sg}"
    if sec == -1:
        if p.get("track") == "V3" or p.get("kind") in ("typed_title", "title") or p.get("type") == "overlay":
            return "cold_open.title" if "title" in str(p.get("kind") or "") else "cold_open.overlays"
        return co_refs.get((-1, sg), "cold_open")
    return None


def pieces_in(cfg, tl, plan, a, b):
    """Every piece of the resolved timeline overlapping [a, b] (all tracks except music), with the source
    span inside the window and the words spoken in it."""
    out = []
    co_refs = cold_open_refs(tl)
    for p in tl.get("pieces") or []:
        if p.get("track") == "A2" or p["end"] <= a or p["start"] >= b:
            continue
        lo, hi = max(a, p["start"]), min(b, p["end"])
        sp = float(p.get("speed") or 1.0)
        src_a = src_b = None
        if p.get("clip") and p.get("in") is not None:
            if p.get("kind") == "still":
                src_a = src_b = p["in"]
            else:
                src_a = round(p["in"] + (lo - p["start"]) * sp, 3)
                src_b = round(p["in"] + (hi - p["start"]) * sp, 3)
        sec, sg = p.get("sec"), p.get("seg")
        seg = _seg_of(plan, sec, sg) if isinstance(sec, int) and sec >= 0 else None
        out.append({"track": p.get("track"), "kind": p.get("kind"), "type": p.get("type"), "sec": sec, "seg": sg,
                    "ref": f"{sec}.{sg}", "note_ref": _piece_ref(p, co_refs),
                    "key": brain.seg_key(seg) if seg is not None else None,
                    "clip": p.get("clip"), "clip_rel": p.get("clip_rel"), "src_in": src_a, "src_out": src_b,
                    "start": round(lo, 3), "end": round(hi, 3), "label": p.get("label"),
                    "words": _words_in(cfg, p.get("clip"), src_a, src_b) if p.get("track") in ("V1", "A1") and
                    p.get("type") in ("talk", "broll") else "",
                    "captions": [c.get("text") for c in p.get("captions") or [] if c.get("t1", 0) > lo and c.get("t0", 0) < hi],
                    "overlays": [o.get("text") or o.get("kind") for o in p.get("overlays") or []]})
    return out


def map_comment(cfg, con, tl, plan, marker, n, frames_dir=None, decisions=None) -> dict:
    t = float(marker.get("t") or 0)
    te = marker.get("t_end")
    a, b = (t, float(te)) if te is not None and float(te) > t + 0.04 else (max(0.0, t - WINDOW), t + WINDOW)
    ps = pieces_in(cfg, tl, plan, a, b)
    segs = list(dict.fromkeys(p["note_ref"] for p in ps if p.get("note_ref")))
    if any(str(r).startswith("cold_open") for r in segs) and "cold_open" not in segs:
        segs.append("cold_open")
    keys = list(dict.fromkeys(p["key"] for p in ps if p.get("key")))
    words = " ".join(dict.fromkeys(p["words"] for p in ps if p.get("words")))
    from ..studio import Studio, drawing_words
    frame = Studio.frame_path_of(cfg, marker.get("frame_url")) if marker.get("frame_url") else None
    note = {"id": f"n{n}", "comment": marker.get("id"), "t": round(t, 3), "t_end": None if te is None else round(float(te), 3),
            "text": str(marker.get("text") or "").strip(), "drawing": drawing_words(marker.get("drawing")) or None,
            "frame": str(frame) if frame and Path(frame).exists() else None, "window": [round(a, 3), round(b, 3)],
            "pieces": ps, "segments": segs, "keys": keys, "words": words, "flags": flags_of(marker.get("text")),
            "supersedes": None, "frames": [], "cold_open": any(str(r).startswith("cold_open") for r in segs)}
    for d in decisions or []:
        if set(d.get("segments") or []) & set(keys):
            note["supersedes"] = d.get("id")
            break
    if frames_dir:
        note["frames"] = clean_frames(cfg, con, ps, Path(frames_dir), note["id"])
    return note


def text_note(text, n) -> dict:
    """A note without a timestamp (chat request, NOTES verdict text, an answer)."""
    return {"id": f"n{n}", "comment": None, "t": None, "t_end": None, "text": str(text or "").strip(), "drawing": None,
            "frame": None, "window": None, "pieces": [], "segments": [], "keys": [], "words": "",
            "flags": flags_of(text), "supersedes": None, "frames": []}


def clean_frames(cfg, con, pieces, out_dir, nid, width=640) -> list:
    """One clean frame per picture piece in the window (from the proxy, at the middle of the span)."""
    from .. import db, media
    out = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate([p for p in pieces if p.get("track") in ("V1", "V2") and p.get("clip")][:4]):
        try:
            row = dict(db.get_clip(con, p["clip"]))
        except Exception:  # noqa: BLE001
            continue
        src = row.get("proxy") if row.get("proxy") and Path(row["proxy"]).exists() else row.get("path")
        if not src or p.get("src_in") is None:
            continue
        tt = (p["src_in"] + p["src_out"]) / 2 if p.get("src_out") is not None else p["src_in"]
        f = out_dir / f"{nid}_p{i}_{p['track']}.jpg"
        try:
            media.ffmpeg(cfg, ["-ss", f"{max(0.0, tt):.3f}", "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2",
                               "-q:v", "4", f], timeout=60)
            if f.exists():
                out.append(str(f))
        except Exception:  # noqa: BLE001 - a missing frame never stops intake
            continue
    return out


def map_all(cfg, con, plan, markers, texts=(), frames_dir=None, decisions=None, tl=None):
    """All of an order's comments (timeline order) + free-text notes -> notes list."""
    from ..render import resolve_timeline
    tl = tl or resolve_timeline(cfg, con, plan, preview=True)
    notes = []
    for m in sorted(markers, key=lambda m: float(m.get("t") or 0)):
        notes.append(map_comment(cfg, con, tl, plan, m, len(notes) + 1, frames_dir, decisions))
    for t in texts:
        if str(t or "").strip():
            notes.append(text_note(t, len(notes) + 1))
    return notes, tl
