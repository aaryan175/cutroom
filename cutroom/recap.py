"""Event recap mode: a draft plan for a hype recap of a live event (stage speaker, packed room, music, crowd).

    cutroom detect events                                  # stage / crowd / wide windows, energy peaks, soundbites
    cutroom recap <name> --len 120 [--song X] [--ref REF]  # writes plans/<name>_v1.json (next free version)

The draft only uses plan fields that already exist (docs/PLAN_FORMAT.md). Its shape:

    cold open   the biggest soundbite (talk, music under the voice) -> the crowd reacting (natural sound)
    build       one song, ducked under the voice: an establishing montage of wide shots, then blocks of
                crowd / wide / stage shots cut on the beat, each soundbite (J-cut: the voice starts under the
                last montage shot) followed by a crowd reaction whose length lands the next block on a downbeat
    climax      a flash on the song's drop, fast montage of the strongest energy peaks, the single biggest
                peak held, then the end card (a title; the creator's logo replaces it) with the song fading out

Flashes go only on montage blocks and section starts that land on a downbeat or a drop, at most
`flash_max_per_10s` in any 10 s (from the reference when one is given). Speed ramps go on high-motion crowd
shots going into a flash. With `--ref`, the reference's cold-open length, flash rate, montage pace, talk share
and the position of its loudest late section set those numbers (cutroom/reference.py analysis).

The AI then refines the draft (the `event-recap` skill): it reads every soundbite, looks at every shot, and
checks strangers' faces before anything is rendered.
"""
import json
import re
from pathlib import Path

import numpy as np

from . import analyze, asr, music, plan as P

# Defaults live here (not in config.py): everything the recap template decides on its own.
RECAP_DEFAULTS = {
    "len": 120.0,
    "cold_open_s": 8.0,            # target cold-open length (soundbite + reaction [+ a short fill montage])
    "climax_frac": 0.76,           # where the climax starts, as a share of the runtime
    "end_card_s": 3.0,
    "end_card_text": "LOGO",       # placeholder: the creator's logo / end card replaces it
    "flash_max_per_10s": 2.0,      # never more flashes than this in any 10 s window
    "flash_frames": 4,
    "block_beats": 8,              # a montage block spans this many beats (two bars), so blocks start on downbeats
    "build_bpc": 1,                # beats per cut in build blocks (2 = the establishing block)
    "climax_bpc": 1,
    "bite_every_s": 20.0,          # roughly one soundbite per this many seconds of build
    "reaction_s": (1.2, 3.2),      # crowd reaction after a soundbite: min / max seconds
    "duck_db": -14.0,              # music under the soundbites
    "ramp": {"to": 2.0, "over": 0.4},
    "ramp_motion": 4.0,            # local motion (mean abs frame difference) that counts as a high-motion crowd shot
    "downbeat_tol": 0.09,          # a flash is "on the downbeat" within this many seconds
    "big_face_h": 0.15,            # a crowd shot with a face this large (share of frame height) gets a check note
}


class RecapError(ValueError):
    pass


# -- reference profile ------------------------------------------------------------------------------

def ref_profile(cfg, ref, beat_s=0.5) -> dict:
    """Numbers the recap copies from a measured reference: cold-open length, white-flash rate per 10 s,
    montage ASL (-> beats per cut), talk share and where its climax (loudest / fastest late section) starts."""
    from . import reference as R
    rid = R.resolve_id(cfg, ref)
    an = R.load_analysis(cfg, rid)
    dur = float((an.get("source") or {}).get("duration") or sum(s["dur"] for s in an.get("shots", [])) or 1.0)
    st, pc = an.get("structure") or {}, an.get("pacing") or {}
    whites = [f for f in an.get("flashes") or [] if f.get("kind") in ("white", "insert")]
    secs = an.get("sections") or []
    mont = [s for s in secs if s.get("name") in ("montage", "cold open")] or secs
    asl = float(np.median([s["asl"] for s in mont])) if mont else float(pc.get("median_shot") or 0.6)
    late = [s for s in secs if s["start"] >= 0.4 * dur]
    climax = None
    if late:
        best = max(late, key=lambda s: ((s.get("loudness_lufs") or -60) + 6 * (s.get("cuts_per_s") or 0)))
        climax = best["start"] / dur
    prof = {"id": rid, "duration": round(dur, 2),
            "cold_open_s": float(st.get("cold_open_s") or RECAP_DEFAULTS["cold_open_s"]),
            "flash_per_10s": round(10 * len(whites) / dur, 2), "n_flashes": len(whites),
            "montage_asl": round(asl, 3), "bpc": int(min(4, max(1, round(asl / max(beat_s, 0.2))))),
            "talk_pct": float(st.get("talk_pct") or 0.0), "climax_frac": climax,
            "sections": [{"name": s["name"], "start": s["start"], "end": s["end"], "asl": s["asl"]} for s in secs]}
    return prof


# -- material ---------------------------------------------------------------------------------------

def _clip_rows(con):
    return {r["id"]: r for r in con.execute("SELECT * FROM clips WHERE status != 'error'")}


def _sessions(con):
    analyze.ensure_scale_schema(con)
    return {r["clip_id"]: (r["session"], r["ts"]) for r in con.execute("SELECT clip_id, session, ts FROM sessions")}


class _Picker:
    """Hands out event windows without repeating a window and never the same clip twice in a row."""

    def __init__(self, con, clips):
        self.clips = clips
        self.pools = {k: [w for w in analyze.event_pool(con, k, limit=2000) if self._ok(w)] for k in analyze.EVENT_KINDS}
        self.used = set()
        self.last = None
        self.reuse = {}                 # small libraries: windows come round again, later inside the window
        self.reserved = set()           # never handed out again (the climax's held peak)

    def _ok(self, w):
        c = self.clips.get(w["clip_id"])
        return c is not None and float(c["duration"] or 0) >= 2.0

    def avail(self, kind):
        return [w for w in self.pools.get(kind, []) if (w["clip_id"], w["t0"]) not in self.used]

    def take(self, kinds, key=None, avoid=()):
        if not any(self.avail(k) for k in kinds):
            for k in kinds:
                if self.pools.get(k):
                    self.used -= {(w["clip_id"], w["t0"]) for w in self.pools[k]} - self.reserved
                    self.reuse[k] = self.reuse.get(k, 0) + 1
        for kind in kinds:
            cand = [w for w in self.avail(kind) if w["clip_id"] != self.last and w["clip_id"] not in avoid]
            if not cand:
                cand = [w for w in self.avail(kind) if w["clip_id"] not in avoid]
            if not cand:
                continue
            w = max(cand, key=key) if key else cand[0]
            self.used.add((w["clip_id"], w["t0"]))
            self.last = w["clip_id"]
            return {**w, "_kind": kind, "_round": self.reuse.get(kind, 0)}
        return None

    def shot_in(self, w, need=1.6):
        dur = float(self.clips[w["clip_id"]]["duration"] or 0)
        start = float(w["t0"]) + 0.15 + 0.8 * (w.get("_round") or 0) % 1.6
        return round(max(0.0, min(start, dur - need - 0.3)), 3)


def _same_line(a, b) -> bool:
    """The same words (a second camera on the same line, or the speaker repeating it)."""
    import difflib
    na, nb = (re.sub(r"[^a-z0-9 ]", "", x.lower()).split() for x in (a, b))
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def _bites(cfg, con, clips) -> list:
    """Soundbites with word-boundary in/out points: [{clip, t0, t1, a, b, text, score, ...}], best first.
    A line caught by two cameras (or said twice) appears once: the best-scored copy."""
    out = []
    for r in analyze.soundbite_list(con, limit=400):
        c = clips.get(r["clip_id"])
        if c is None or any(_same_line(r["text"] or "", o["text"] or "") for o in out):
            continue
        ws = asr.words(cfg, r["clip_id"])
        prev = max((w[1] for w in ws if w[1] <= r["t0"] + 1e-3), default=0.0)
        nxt = min((w[0] for w in ws if w[0] >= r["t1"] - 1e-3), default=float(c["duration"] or r["t1"] + 1))
        a = max(0.0, prev + 0.01 if prev > 0 else 0.0, r["t0"] - 0.05)          # never into the previous word
        b = min(float(c["duration"] or r["t1"] + 1), max(r["t1"] + 0.02, min(r["t1"] + 0.15, nxt - 0.01)))
        out.append({**r, "clip": r["clip_id"], "a": round(a, 3), "b": round(b, 3)})
    return out


# -- segments ---------------------------------------------------------------------------------------

def _talk(bite, lead=0.0, note=""):
    """A soundbite as a talk segment. With `lead`, the picture starts `lead` s after the first word, so the
    voice itself plays under the previous shot (a J-cut) and the audio still starts on a word boundary."""
    a = bite["a"] + lead
    seg = {"type": "talk", "clip": bite["clip"], "in": round(a, 3), "out": bite["b"], "tighten": True,
           "note": note or f"soundbite ({bite['score']:.2f}): \"{bite['text'][:80]}\""}
    if lead:
        seg["lead_in"] = round(lead, 3)
    else:
        seg["lead_in"] = 0.0
    return seg


def _block(picker, kinds_cycle, n, bpc, note, avoid=()):
    shots, used_kinds, checks = [], [], []
    for k in range(n):
        order = kinds_cycle[k % len(kinds_cycle):] + kinds_cycle[:k % len(kinds_cycle)]
        w = picker.take(order, avoid=avoid)
        if w is None:
            break
        shots.append({"clip": w["clip_id"], "in": picker.shot_in(w), "_w": w})
        used_kinds.append(w["_kind"])
        if w["_kind"] == "crowd" and (w.get("face_h") or 0) >= RECAP_DEFAULTS["big_face_h"]:
            checks.append(w["clip_id"])
    if len(shots) < 2:
        return None
    seg = {"type": "montage", "beat_sync": True, "beats_per_cut": int(bpc), "min_shot": 0.3,
           "shots": shots, "note": note + " (" + ", ".join(used_kinds) + ")"}
    if checks:
        seg["note"] += "; CHECK the faces in " + ", ".join(sorted(set(checks))) + " (large stranger face)"
    return seg


def _reaction(picker, bite, sess, clips, dur=1.8):
    """The crowd reacting right after a soundbite: applause-heavy crowd windows, closest in capture time."""
    s_ts = sess.get(bite["clip"], (None, None))

    def key(w):
        o = sess.get(w["clip_id"], (None, None))
        near = 0.0
        if s_ts[1] is not None and o[1] is not None:
            dt = o[1] - s_ts[1]
            near = 1.0 if 0 <= dt <= 900 else 0.5 if abs(dt) <= 1800 else 0.0
        return 0.5 * (w.get("applause") or 0) + 0.3 * (w.get("energy") or 0) + 0.2 * near
    w = picker.take(["crowd"], key=key, avoid=(bite["clip"],)) or picker.take(["wide", "stage"], avoid=(bite["clip"],))
    if w is None:
        return None
    a = picker.shot_in(w, need=RECAP_DEFAULTS["reaction_s"][1])
    return {"type": "broll", "clip": w["clip_id"], "in": a, "out": round(a + dur, 3), "audio": "natural",
            "note": f"crowd reaction to the soundbite before (applause {w.get('applause') or 0:.2f})", "_w": w}


def _strip(plan):
    """Drop the private helper keys before writing."""
    for sec in plan["sections"]:
        for s in sec["segments"]:
            s.pop("_w", None)
            for sh in s.get("shots", []) or []:
                sh.pop("_w", None)
    return plan


def _resolve(cfg, con, plan):
    from . import render
    return render.resolve_timeline(cfg, con, _strip(json.loads(json.dumps(plan, default=str))), preview=True)


# -- the template -----------------------------------------------------------------------------------

def _pick_song(cfg, con, song, L):
    if song:
        return music.song_info(cfg, con, song)
    rows = con.execute("SELECT id, duration, tempo FROM songs ORDER BY tempo DESC").fetchall()
    if not rows:
        raise RecapError("no songs indexed: put songs in the music folder and run `cutroom songs index`, or pass --song")
    long_enough = [r for r in rows if (r["duration"] or 0) >= 0.6 * L] or rows
    return music.song_info(cfg, con, long_enough[0]["id"])


def _downbeats(si):
    d = np.asarray(si.get("downbeats") or [], float)
    if len(d) < 2:
        d = np.asarray(si["beats"][::4], float)
    return d


def build(cfg, con, name, length=None, song=None, ref=None, echo=print) -> dict:
    """Build the draft recap plan (a dict with a private `_report`)."""
    D = dict(RECAP_DEFAULTS)
    L = float(length or D["len"])
    clips = _clip_rows(con)
    analyze.ensure_event_schema(con)
    if not con.execute("SELECT COUNT(*) FROM event_windows").fetchone()[0]:
        raise RecapError("no event scores yet: run `cutroom detect events` first")
    si = _pick_song(cfg, con, song, L)
    beats = np.asarray(si["beats"], float)
    if len(beats) < 8:
        raise RecapError(f"song {si['title']!r} has no usable beat grid")
    beat_s = float(np.median(np.diff(beats)))
    downs = _downbeats(si)
    prof = ref_profile(cfg, ref, beat_s) if ref else None
    if prof:
        D["cold_open_s"] = min(15.0, max(4.0, prof["cold_open_s"]))
        if prof["n_flashes"]:
            D["flash_max_per_10s"] = min(4.0, max(0.5, prof["flash_per_10s"]))
        D["build_bpc"] = D["climax_bpc"] = prof["bpc"]
        if prof["climax_frac"]:
            D["climax_frac"] = min(0.85, max(0.55, prof["climax_frac"]))
        if prof["talk_pct"]:
            D["bite_every_s"] = min(40.0, max(10.0, 6.0 / max(prof["talk_pct"] / 100, 0.05)))
    picker = _Picker(con, clips)
    sess = _sessions(con)
    bites = _bites(cfg, con, clips)
    if not any(picker.pools.values()):
        raise RecapError("no stage / crowd / wide windows found; check `cutroom search --kind crowd`")
    # the climax's held shot: the biggest energy peak, reserved before anything else takes it
    peaks = sorted(picker.avail("crowd") + picker.avail("stage"), key=lambda w: -(w.get("energy") or 0))
    big = peaks[0] if peaks else None
    if big:
        picker.used.add((big["clip_id"], big["t0"]))
        picker.reserved.add((big["clip_id"], big["t0"]))

    # the climax drop: a drop that leaves room for the climax + end card after it (the latest such)
    climax_s = max(8.0, (1 - D["climax_frac"]) * L - D["end_card_s"])
    song_dur = float(si.get("duration") or beats[-1])
    drops = [d for d in (si.get("drops") or []) if d + climax_s + D["end_card_s"] <= song_dur] or \
        [float(downs[min(len(downs) - 1, max(0, int(np.searchsorted(downs, song_dur - climax_s - D['end_card_s'])) - 1))])]
    drop = float(drops[-1])

    used_bites = []
    cold_bite = bites[0] if bites else None
    if cold_bite:
        used_bites.append(cold_bite)
    # build soundbites: the next best, one per source moment, in source order
    build_s = D["climax_frac"] * L - D["cold_open_s"]
    nb = max(1, int(round(build_s / D["bite_every_s"])))
    def clash(b, u):                                  # overlapping (or touching) moments of one clip
        return b["clip"] == u["clip"] and b["a"] < u["b"] + 1.0 and u["a"] < b["b"] + 1.0
    build_bites = []
    for b in bites[1:]:
        if len(build_bites) >= nb:
            break
        if not any(clash(b, u) for u in used_bites + build_bites):
            build_bites.append(b)
    build_bites.sort(key=lambda b: (str(clips[b["clip"]]["shot_at"] or ""), b["clip"], b["t0"]))
    used_bites += build_bites

    mus = lambda off, **kw: {"song": si["id"], "offset": round(float(off), 3), **kw}  # noqa: E731
    # -- cold open
    cold = []
    if cold_bite:
        cold.append(_talk(cold_bite, note=f"COLD OPEN: the biggest soundbite ({cold_bite['score']:.2f}): "
                                          f"\"{cold_bite['text'][:80]}\""))
        r = _reaction(picker, cold_bite, sess, clips)
        if r:
            cold.append(r)
    else:
        blk = _block(picker, ["crowd", "wide"], 6, 1, "COLD OPEN (no soundbites found: transcribe the stage clips)")
        if blk:
            cold.append(blk)
    sections = [{"name": "cold open", "music": mus(0.0, mode="under"), "segments": cold}]
    short = D["cold_open_s"] - ((cold_bite["b"] - cold_bite["a"]) + 1.8 if cold_bite else 0.0)
    if cold_bite and short >= 2.0:                    # the reference opens longer: micro-cuts after the reaction
        n_fill = max(2, int(round(short / (beat_s * max(1, int(D["build_bpc"]))))))
        fill = _block(picker, ["crowd", "wide", "stage"], n_fill, max(1, int(D["build_bpc"])),
                      "cold open: fast crowd / wide cuts after the reaction (the reference's cold open is longer)")
        if fill:
            cold.append(fill)
    # -- build
    est_block = D["block_beats"] * beat_s
    bpc_b = int(D["build_bpc"])
    n_block = max(2, int(round(D["block_beats"] / bpc_b)))
    build_segs = []
    est = 0.0
    wide = _block(picker, ["wide", "wide", "crowd", "stage"], max(2, int(round(D["block_beats"] / max(2, bpc_b)))),
                  max(2, bpc_b), "establishing: wide views of the room")
    if wide:
        build_segs.append(wide)
        est += est_block
    queue = list(build_bites)
    budget = max(est_block * 2, D["climax_frac"] * L - D["cold_open_s"])
    while est < budget:
        blk = _block(picker, ["crowd", "stage", "crowd", "wide"], n_block, bpc_b, "build: crowd / stage / wide on the beat")
        if blk is None:
            break
        build_segs.append(blk)
        est += est_block
        due = queue and est >= (len(build_bites) - len(queue) + 1) * budget / (len(build_bites) + 1)
        if due and est < budget:
            b = queue.pop(0)
            build_segs.append(_talk(b, lead=0.3))
            est += b["b"] - b["a"]
            r = _reaction(picker, b, sess, clips)
            if r:
                build_segs.append(r)
                est += 1.8
    for b in queue:                                   # bites the blocks ran out before
        used_bites.remove(b)
    sections.append({"name": "build", "music": mus(max(float(beats[0]), drop - budget), mode="duck", duck_db=D["duck_db"]),
                     "segments": build_segs})
    # -- climax + end card
    n_cl = max(4, int(round((climax_s - 2.5) / (beat_s * D["climax_bpc"]))))
    climax = []
    cm = _block(picker, ["crowd", "crowd", "stage", "wide"], n_cl, D["climax_bpc"], "climax: the strongest energy peaks, fast",
                avoid=(big["clip_id"],) if big else ())
    if cm:
        climax.append(cm)
    if big:
        a = picker.shot_in(big, need=2.6)
        climax.append({"type": "broll", "clip": big["clip_id"], "in": a, "out": round(a + 2.5, 3), "audio": "natural",
                       "note": f"the biggest energy peak in the library (energy {big['energy']:.2f})"})
    climax.append({"type": "title", "text": D["end_card_text"], "sub": "", "dur": D["end_card_s"],
                   "bg": f"blur:{big['clip_id']}@{picker.shot_in(big, need=1):.2f}" if big else "black",
                   "transition": {"kind": "dip"}, "note": "END CARD: replace the text with the creator's logo / end card"})
    sections.append({"name": "climax", "music": mus(drop, mode="duck", duck_db=D["duck_db"], end={"fade": 1.5}),
                     "transition": {"kind": "flash", "frames": D["flash_frames"]}, "segments": climax})
    plan = {"title": f"{name} (event recap)", "version": 1, "sections": sections,
            "style": {"music_under_speech": "duck"}}

    # -- fit the length: add / drop build blocks against the resolved timeline
    def total():
        return float(_resolve(cfg, con, plan)["duration"])
    for _ in range(8):
        T = total()
        if abs(T - L) <= 0.04 * L:
            break
        bs = sections[1]["segments"]
        if T > L:
            if len(bs) <= 2:
                break
            gone = bs.pop()
            if gone["type"] == "broll" and bs and bs[-1]["type"] == "talk":
                bs.pop()
        else:
            blk = _block(picker, ["crowd", "wide", "stage", "crowd"], n_block, bpc_b, "build: crowd / stage / wide on the beat")
            if blk is None:
                break
            bs.append(blk)
    talk_keys = {(s["clip"], s["out"]) for sec in sections for s in sec["segments"] if s["type"] == "talk"}
    used_bites = [b for b in used_bites if (b["clip"], b["b"]) in talk_keys]

    # -- music offsets: the build ends on the drop; the cold open runs straight into the build
    res = _resolve(cfg, con, plan)
    secs = {s["index"]: s for s in res["sections"]}
    b_len = secs[1]["end"] - secs[1]["start"]
    c_len = secs[0]["end"] - secs[0]["start"]
    b_off = drop - b_len
    if b_off < float(beats[0]):                      # the song is too short before the drop: start on its first bar
        b_off = float(downs[0])
    else:
        b_off = float(downs[np.argmin(np.abs(downs - b_off))])
    sections[1]["music"]["offset"] = round(b_off, 3)
    sections[0]["music"]["offset"] = round(max(0.0, b_off - c_len), 3)   # "under" the voice: no beat grid needed

    # -- reactions end on a downbeat, so the next block starts on one
    for k, seg in enumerate(sections[1]["segments"]):
        if seg["type"] != "broll":
            continue
        res = _resolve(cfg, con, plan)
        row = next(r for r in res["segments"] if r["sec"] == 1 and r["seg"] == k)
        st = b_off + (row["start"] - secs[1]["start"])
        lo, hi = RECAP_DEFAULTS["reaction_s"]
        cand = downs[(downs >= st + lo) & (downs <= st + hi)]
        if not len(cand):
            cand = beats[(beats >= st + lo) & (beats <= st + hi)]
        if len(cand):
            seg["out"] = round(seg["in"] + float(cand[0] - st), 3)

    # -- flashes: on downbeats / drops only, rate-limited; ramps into them on high-motion crowd shots
    res = _resolve(cfg, con, plan)
    secs = {s["index"]: s for s in res["sections"]}
    tol = D["downbeat_tol"]
    cands = []
    for r in res["segments"]:
        if r["sec"] != 1 or r["type"] != "montage" or r["seg"] == 0:
            continue
        prev = sections[1]["segments"][r["seg"] - 1]
        st = b_off + (r["start"] - secs[1]["start"])
        on_down = bool(len(downs)) and float(np.min(np.abs(downs - st))) <= tol
        near_drop = any(abs(st - d) <= tol + beat_s for d in si.get("drops") or [])
        prio = 2 if near_drop else 1 if prev["type"] == "broll" else 0
        cands.append((r["start"], r["seg"], on_down, prio))
    fixed = [secs[1]["start"], secs[2]["start"]]          # section flashes (into the build, onto the drop)
    sections[1]["transition"] = {"kind": "flash", "frames": D["flash_frames"]}
    chosen = list(fixed)
    flash_segs = []
    for t, gi, on_down, prio in sorted(cands, key=lambda c: (-c[3], c[0])):
        if not on_down:
            continue
        trial = sorted(chosen + [t])
        if any(sum(1 for x in trial if a <= x < a + 10.0) > D["flash_max_per_10s"] for a in trial):
            continue
        chosen.append(t)
        flash_segs.append(gi)
    for gi in flash_segs:
        sections[1]["segments"][gi]["transition"] = {"kind": "flash", "frames": D["flash_frames"]}
    ramps = 0
    for gi in flash_segs + [len(sections[1]["segments"])]:
        prev = sections[1]["segments"][gi - 1] if gi - 1 >= 0 else None
        if prev and prev["type"] == "montage":
            last = prev["shots"][-1]
            w = last.get("_w") or {}
            if w.get("_kind") == "crowd" and (w.get("local_motion") or 0) >= D["ramp_motion"]:
                last["ramp"] = dict(D["ramp"])
                ramps += 1
    for seg in climax:
        if seg["type"] == "montage" and len(seg["shots"]) > 1:
            w = seg["shots"][-1].get("_w") or {}
            if w.get("_kind") == "crowd" and (w.get("local_motion") or 0) >= D["ramp_motion"]:
                seg["shots"][-1]["ramp"] = dict(D["ramp"])
                ramps += 1

    res = _resolve(cfg, con, plan)
    n_fl = 1 + 1 + len(flash_segs)
    plan["note"] = ("Event recap draft from `cutroom recap` (event-recap skill): refine it. "
                    f"Song {si['title']!r} ({si['tempo']:.0f} BPM), climax on the drop at {drop:.1f}s. "
                    f"{len(used_bites)} soundbite(s), {n_fl} flash(es) on downbeats (max {D['flash_max_per_10s']:g} per 10 s), "
                    f"{ramps} speed ramp(s)." + (f" Pacing from reference {prof['id']}." if prof else ""))
    plan["_report"] = {"duration": res["duration"], "target": L, "song": si["title"], "drop": drop,
                       "soundbites": [{"clip": b["clip"], "t0": b["t0"], "t1": b["t1"], "score": b["score"],
                                       "text": b["text"]} for b in used_bites],
                       "flashes": n_fl, "flash_max_per_10s": D["flash_max_per_10s"], "ramps": ramps,
                       "sections": [{"name": s["name"], "start": s["start"], "end": s["end"]} for s in res["sections"]],
                       "reference": prof, "warnings": res.get("warnings", [])}
    return _strip(plan)


def next_plan_path(cfg, slug) -> Path:
    d = Path(cfg.work) / "plans"
    d.mkdir(parents=True, exist_ok=True)
    v = 1
    while (d / f"{slug}_v{v}.json").exists():
        v += 1
    return d / f"{slug}_v{v}.json"


def write(cfg, con, name, length=None, song=None, ref=None, echo=print) -> tuple:
    """Build and write the draft. Returns (path, report, validation errors)."""
    from .render import slugify
    plan = build(cfg, con, name, length=length, song=song, ref=ref, echo=echo)
    rep = plan.pop("_report")
    slug = slugify(name) or "recap"
    path = next_plan_path(cfg, slug)
    plan["version"] = int(re.search(r"_v(\d+)\.json$", path.name).group(1))
    path.write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    errs = P.validate(P.normalize(plan, cfg), con)
    return path, rep, errs
