"""Operator-facing library browsing: what to tag next, search, and a full report on one clip."""
import json
import os
import re
import statistics

from . import analyze, db, media
from .ingest import display_size

SPARK = " ▁▂▃▄▅▆▇█"


# -- helpers ---------------------------------------------------------------------------------

def _flatten(v) -> str:
    if isinstance(v, dict):
        return " ".join(_flatten(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return " ".join(_flatten(x) for x in v)
    return "" if v is None else str(v)


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1", "y")


def _norm(w: str) -> str:
    return re.sub(r"[^\w']+", "", w.lower())


def _median_shake(con, ids) -> dict:
    out, ids = {}, list(ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        vals = {}
        for r in con.execute(f"SELECT clip_id, shake, exposure FROM metrics WHERE clip_id IN "
                             f"({','.join('?' * len(chunk))})", chunk):
            vals.setdefault(r["clip_id"], ([], []))
            vals[r["clip_id"]][0].append(r["shake"])
            vals[r["clip_id"]][1].append(r["exposure"])
        for cid, (sh, ex) in vals.items():
            out[cid] = {"shake": statistics.median(sh), "exposure": statistics.median(ex)}
    return out


def _quality_line(q: dict) -> str:
    if not q:
        return "not analyzed"
    return (f"shake {q['shake']:.1f} · exp {q['exposure']:.0f} · sharp {q['sharpness']:.0f} · "
            f"faces {q['faces']} · auto-q {analyze.auto_quality(q)}")


def transcript_words(cfg, cid) -> list:
    """Flat list of {start, end, word} from <work>/transcripts/<id>.json (empty if none)."""
    p = cfg.p("transcripts", f"{cid}.json")
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    return [w for s in data.get("segments", []) for w in (s.get("words") or [])]


def first_hit(cfg, cid, words) -> float | None:
    """Start time of the first transcript word matching any of `words`."""
    want = [_norm(w) for w in words if _norm(w)]
    for w in transcript_words(cfg, cid):
        tok = _norm(w.get("word", ""))
        if tok and any(x in tok for x in want):
            return round(float(w.get("start", 0)), 2)
    return None


# -- todo ------------------------------------------------------------------------------------

def todo(cfg, con, n=20):
    rows = con.execute("""
        SELECT c.*, tr.text AS tr_text, COALESCE(tr.n_words, 0) AS n_words
        FROM clips c LEFT JOIN tags t ON t.clip_id = c.id LEFT JOIN transcripts tr ON tr.clip_id = c.id
        WHERE t.clip_id IS NULL AND c.status = 'analyzed'
        ORDER BY (COALESCE(tr.n_words, 0) > 5) DESC, c.duration DESC LIMIT ?""", (n,)).fetchall()
    left = con.execute("SELECT COUNT(*) FROM clips c LEFT JOIN tags t ON t.clip_id=c.id "
                       "WHERE t.clip_id IS NULL AND c.status='analyzed'").fetchone()[0]
    if not rows:
        print("nothing to tag (no analyzed, untagged clips). Run `cutroom index` first.")
        return
    print(f"{left} analyzed clips untagged; showing {len(rows)}:\n")
    for r in rows:
        print(f"{r['id']}  {r['duration'] or 0:6.1f}s  {r['rel']}")
        print(f"    sheet: {r['sheet'] or '(none yet: cutroom index)'}")
        if r["tr_text"]:
            print(f"    says:  {' '.join(r['tr_text'].split())[:120]}")
        print(f"    {_quality_line(analyze.clip_quality(con, r['id']))}")
    print("\nNext: open each sheet image and look at it, then run "
          "`cutroom tags set <id> '<json>'` with the fields from `cutroom tags fields` "
          "(e.g. '{\"summary\": \"...\", \"setting\": \"street\", \"quality\": 4}').")


# -- search ----------------------------------------------------------------------------------

HL_MIN = 0.35                    # a window scoring this (0-1) or more counts as a highlight


def _hl_dict(v):
    return None if not v else {"score": round(v[0], 3), "t": v[1], "kind": v[2]}


def _scale_maps(cfg, con, args) -> dict:
    """Highlight peaks, duplicate representatives, shoots and --near ranking (analyze.py scale tables)."""
    analyze.ensure_scale_schema(con)
    hl = {r["clip_id"]: (r["score"] or 0.0, r["t"], r["kind"]) for r in con.execute("SELECT * FROM highlight_peaks")}
    rep = {r["clip_id"]: r["rep"] for r in con.execute("SELECT clip_id, rep FROM dupes")}
    sess = {r["clip_id"]: r["session"] for r in con.execute("SELECT clip_id, session FROM sessions")}
    only, near_rank = None, None
    if getattr(args, "session", None) is not None:
        only = {cid for cid, sn in sess.items() if sn == int(args.session)}
    if getattr(args, "near", None):
        lst = analyze.near(cfg, con, args.near, limit=max(50, getattr(args, "limit", 30) or 30))
        near_rank = {x["id"]: (i, x["dist"]) for i, x in enumerate(lst)}
        only = set(near_rank) if only is None else only & set(near_rank)
    ev = None
    kind = getattr(args, "kind", None)
    if kind:
        ev = event_kind_map(con, kind)
        only = set(ev) if only is None else only & set(ev)
    return {"hl": hl, "rep": rep, "sess": sess, "only": only, "near": near_rank, "ev": ev}


def event_kind_map(con, kind) -> dict:
    """clip id -> {score, t, energy, windows, share} for clips with at least one window of this event kind
    (`cutroom detect events`)."""
    if kind not in analyze.EVENT_KINDS:
        raise ValueError(f"kind must be one of {analyze.EVENT_KINDS}")
    analyze.ensure_event_schema(con)
    out = {}
    for r in con.execute(f"SELECT clip_id, MAX({kind}) AS sc, COUNT(*) AS n, MAX(energy) AS en FROM event_windows "
                         "WHERE kind = ? GROUP BY clip_id", (kind,)):
        best = con.execute(f"SELECT t0 FROM event_windows WHERE clip_id=? AND kind=? ORDER BY {kind} DESC LIMIT 1",
                           (r["clip_id"], kind)).fetchone()
        tot = con.execute("SELECT COUNT(*) FROM event_windows WHERE clip_id=?", (r["clip_id"],)).fetchone()[0]
        out[r["clip_id"]] = {"score": round(r["sc"], 3), "t": best["t0"], "energy": round(r["en"] or 0, 3),
                             "windows": r["n"], "share": round(r["n"] / max(1, tot), 3)}
    return out


def print_soundbites(cfg, con, args) -> list:
    rows = analyze.soundbite_list(con, limit=getattr(args, "limit", 30) or 30, text=getattr(args, "text", "") or None)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=1))
        return rows
    if not rows:
        print("no soundbites (transcribe the stage clips, then `cutroom detect events`)")
        return rows
    print(f"{'clip':10s} {'in':>7s} {'out':>7s} {'score':>5s}  loud pitch emph words resp  line")
    for r in rows:
        print(f"{r['clip_id']:10s} {r['t0']:7.2f} {r['t1']:7.2f} {r['score']:5.2f}  {r['loud']:4.2f} {r['pitch']:5.2f} "
              f"{r['emphasis']:4.2f} {r['wording']:5.2f} {r['response']:4.2f}  \"{(r['text'] or '')[:70]}\"")
    return rows


def search(cfg, con, args):
    if getattr(args, "soundbites", False):
        return print_soundbites(cfg, con, args)
    words = [_norm(w) for w in (getattr(args, "text", "") or "").split() if _norm(w)]
    rows = con.execute("""
        SELECT c.id, c.rel, c.path, c.duration, c.sheet, c.proxy, c.status, t.data AS tags,
               tr.text AS tr_text, COALESCE(tr.n_words, 0) AS n_words
        FROM clips c LEFT JOIN tags t ON t.clip_id = c.id LEFT JOIN transcripts tr ON tr.clip_id = c.id
        WHERE c.status != 'error'""").fetchall()

    def tag_has(tags, key, want):
        return want is None or want.lower() in str(tags.get(key, "")).lower()

    cand = []
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else {}
        if tags and "usable" in tags and not _truthy(tags["usable"]):
            continue
        if not (tag_has(tags, "setting", getattr(args, "setting", None))
                and tag_has(tags, "time_of_day", getattr(args, "time", None))
                and tag_has(tags, "people", getattr(args, "people", None))
                and tag_has(tags, "shot", getattr(args, "shot", None))
                and tag_has(tags, "camera", getattr(args, "camera", None))):
            continue
        if getattr(args, "creator", False) and not _truthy(tags.get("creator_visible")):
            continue
        if getattr(args, "speech", False) and r["n_words"] <= 5:
            continue
        if getattr(args, "no_speech", False) and r["n_words"] > 5:
            continue
        if getattr(args, "min_dur", None) and (r["duration"] or 0) < args.min_dur:
            continue
        score, where = 0.0, ""
        if words:
            hay_t = _flatten(tags).lower()
            hay_s = (r["tr_text"] or "").lower()
            hay_p = (r["rel"] or "").lower()
            in_t = [w in hay_t for w in words]
            in_s = [w in hay_s for w in words]
            in_p = [w in hay_p for w in words]
            if not all(a or b or c for a, b, c in zip(in_t, in_s, in_p)):
                continue
            score = 3 * sum(in_t) + 2 * sum(in_s) + sum(in_p)
            where = "tags" if all(in_t) else "speech" if all(in_s) else "path" if all(in_p) else "mixed"
        cand.append({"row": r, "tags": tags, "score": score, "where": where, "in_speech": words and any(in_s)})

    scale = _scale_maps(cfg, con, args)
    if scale["only"] is not None:
        cand = [c for c in cand if c["row"]["id"] in scale["only"]]
    if getattr(args, "dedupe", False):
        cand = [c for c in cand if scale["rep"].get(c["row"]["id"], c["row"]["id"]) == c["row"]["id"]]
    if getattr(args, "highlights", False):
        cand = [c for c in cand if (scale["hl"].get(c["row"]["id"]) or (0,))[0] >= HL_MIN]
    qmap = _median_shake(con, [c["row"]["id"] for c in cand])
    out = []
    max_shake, min_q = getattr(args, "max_shake", None), getattr(args, "min_quality", None)
    for c in cand:
        cid = c["row"]["id"]
        m = qmap.get(cid)
        if max_shake is not None and (m is None or m["shake"] > max_shake):
            continue
        tq = c["tags"].get("quality")
        try:
            quality = float(tq) if tq not in (None, "") else None
        except (TypeError, ValueError):
            quality = None
        if quality is None and m:
            quality = analyze.auto_quality({"shake": m["shake"], "exposure": m["exposure"], "sharpness": 100})
        if min_q is not None and (quality or 0) < min_q:
            continue
        c.update(quality=quality, shake=m["shake"] if m else None)
        out.append(c)
    hl = scale["hl"]
    ev = scale.get("ev")
    if scale["near"] is not None:
        out.sort(key=lambda c: scale["near"].get(c["row"]["id"], (999, None))[0])
    elif ev is not None:
        out.sort(key=lambda c: (-c["score"], -ev[c["row"]["id"]]["score"] * (0.5 + ev[c["row"]["id"]]["share"])))
    elif getattr(args, "highlights", False):
        out.sort(key=lambda c: (-c["score"], -(hl.get(c["row"]["id"]) or (0,))[0], -(c["quality"] or 0)))
    else:
        out.sort(key=lambda c: (-c["score"], -(c["quality"] or 0), -(c["row"]["duration"] or 0)))
    out = out[:getattr(args, "limit", 30) or 30]

    res = []
    for c in out:
        r, tags = c["row"], c["tags"]
        hit_t = first_hit(cfg, r["id"], words) if c["in_speech"] else None
        res.append({"id": r["id"], "rel": r["rel"], "path": r["path"], "duration": r["duration"],
                    "quality": c["quality"], "shake": None if c["shake"] is None else round(c["shake"], 2),
                    "setting": tags.get("setting"), "time_of_day": tags.get("time_of_day"),
                    "summary": tags.get("summary"), "match": c["where"], "hit_t": hit_t,
                    "sheet": r["sheet"], "proxy": r["proxy"], "tags": tags,
                    "highlight": _hl_dict(hl.get(r["id"])), "session": scale["sess"].get(r["id"]),
                    "dupe_of": (lambda rep: rep if rep and rep != r["id"] else None)(scale["rep"].get(r["id"])),
                    "near_dist": (scale["near"] or {}).get(r["id"], (0, None))[1],
                    "event": (ev or {}).get(r["id"])})
    _note_usage(cfg, [x["id"] for x in res[:10]] if words else [])
    if getattr(args, "json", False):
        print(json.dumps(res, indent=1))
        return res
    if not res:
        print("no matches")
        return res
    print(f"{'id':10s} {'dur':>6s} {'q':>3s} {'shake':>5s}  {'setting/time':22s} summary / path")
    for x in res:
        st = "/".join(str(v) for v in (x["setting"], x["time_of_day"]) if v)[:22]
        desc = x["summary"] or x["rel"]
        if x["hit_t"] is not None:
            desc = f"[says @{x['hit_t']:.1f}s] {desc}"
        if x["highlight"] and (getattr(args, "highlights", False) or x["highlight"]["score"] >= HL_MIN):
            h = x["highlight"]
            desc = f"[hl {h['score']:.2f} @{h['t'] or 0:.0f}s{(' ' + h['kind']) if h['kind'] else ''}] {desc}"
        if x["near_dist"] is not None:
            desc = f"[near {x['near_dist']:.1f}] {desc}"
        if x.get("event"):
            e = x["event"]
            desc = f"[{args.kind} {e['score']:.2f} @{e['t']:.0f}s, {100 * e['share']:.0f}% of clip, energy {e['energy']:.2f}] {desc}"
        if x["session"] is not None and getattr(args, "session", None) is None:
            desc = f"[shoot {x['session']}] {desc}"
        q = f"{x['quality']:.0f}" if x["quality"] is not None else "-"
        sh = f"{x['shake']:.1f}" if x["shake"] is not None else "-"
        print(f"{x['id']:10s} {x['duration'] or 0:6.1f} {q:>3s} {sh:>5s}  {st:22s} {desc[:90]}")
    return res


# -- describe --------------------------------------------------------------------------------

def _spark(vals, lo, hi) -> str:
    out = []
    for v in vals:
        if v is None:
            out.append("·")
            continue
        f = (min(max(v, lo), hi) - lo) / ((hi - lo) or 1)
        out.append(SPARK[1 + round(f * (len(SPARK) - 2))])
    return "".join(out)


def _note_usage(cfg, ids):
    """Searched / opened clips are processed first by `cutroom overnight`."""
    try:
        from .overnight import note_usage
        note_usage(cfg, ids)
    except Exception:  # noqa: BLE001 - bookkeeping only
        pass


def describe(cfg, con, ref, frames=0):
    c = db.get_clip(con, ref)
    cid = c["id"]
    _note_usage(cfg, [cid])
    dw, dh = display_size(c)
    print(f"{cid}  {c['rel']}\n  path     {c['path']}")
    print(f"  status   {c['status']}" + (f"  ({c['error']})" if c["error"] else ""))
    print(f"  video    {c['duration'] or 0:.2f}s · {dw}x{dh} (coded {c['width']}x{c['height']}, rot {c['rotation']}) · "
          f"{c['fps'] or 0:g} fps · {c['codec']} {c['pix_fmt']} · transfer {c['color_transfer'] or '-'} · "
          f"log {'yes' if c['is_log'] else 'no'}")
    print(f"  audio    {'%d ch' % c['audio_channels'] if c['has_audio'] else 'none'} · shot_at {c['shot_at'] or '-'}")
    print(f"  proxy    {c['proxy'] or '-'}\n  sheet    {c['sheet'] or '-'}")
    tags = db.get_tags(con, cid)
    print("  tags     " + (json.dumps(tags, ensure_ascii=False) if tags else "(untagged)"))
    analyze.ensure_scale_schema(con)
    dup = analyze.dupe_info(con, cid)
    if dup:
        print(f"  dupes    {dup['kind']} group of {len(dup['members'])}: " + ", ".join(dup["members"][:8])
              + ("  (this is the representative)" if dup["rep"] == cid else f"  (representative {dup['rep']})"))
    pk = con.execute("SELECT score, t, kind FROM highlight_peaks WHERE clip_id=?", (cid,)).fetchone()
    if pk and pk["t"] is not None:
        print(f"  highlight {pk['score']:.2f} at {pk['t']:.0f}s" + (f" ({pk['kind']})" if pk["kind"] else ""))
    analyze.ensure_event_schema(con)
    ev = con.execute("SELECT * FROM event_clips WHERE clip_id=?", (cid,)).fetchone()
    if ev:
        print(f"  event    {ev['kind'] or 'mixed'} (stage {100 * ev['stage']:.0f}% · crowd {100 * ev['crowd']:.0f}% · "
              f"wide {100 * ev['wide']:.0f}% of windows) · energy peak {ev['energy']:.2f}"
              + (f" at {ev['energy_t']:.0f}s" if ev["energy_t"] is not None else ""))
        nb = con.execute("SELECT COUNT(*), MAX(score) FROM soundbites WHERE clip_id=?", (cid,)).fetchone()
        if nb[0]:
            print(f"  soundbites {nb[0]} (best {nb[1]:.2f}; cutroom search --soundbites)")
    se = con.execute("SELECT session, place FROM sessions WHERE clip_id=?", (cid,)).fetchone()
    if se:
        print(f"  shoot    {se['session']}" + (f" · place {se['place']}" if se["place"] else ""))
    gate = con.execute("SELECT reason FROM tag_gate WHERE clip_id=?", (cid,)).fetchone()
    if gate and not tags:
        print(f"  not sent for AI tagging: {gate['reason']}")

    q = analyze.clip_quality(con, cid)
    print(f"  quality  {_quality_line(q)}")
    ms = con.execute("SELECT * FROM metrics WHERE clip_id=? ORDER BY t0", (cid,)).fetchall()
    if ms:
        print(f"  per 2s   (one char per window, {len(ms)} windows)")
        print(f"    shake  {_spark([m['shake'] for m in ms], 0, 12)}   low=steady")
        print(f"    expo   {_spark([m['exposure'] for m in ms], 0, 255)}")
        print(f"    sharp  {_spark([m['sharpness'] for m in ms], 0, 400)}")
        print(f"    motion {_spark([m['motion'] for m in ms], 0, 20)}")
        print(f"    faces  {''.join(str(min(m['faces'], 9)) for m in ms)}")
    shots = con.execute("SELECT start, end FROM shots WHERE clip_id=? ORDER BY idx", (cid,)).fetchall()
    if shots:
        print(f"  shots    {len(shots)}: " + ", ".join(f"{s['start']:.1f}-{s['end']:.1f}" for s in shots[:40]))
    tr = con.execute("SELECT * FROM transcripts WHERE clip_id=?", (cid,)).fetchone()
    if tr:
        print(f"  speech   {tr['n_words']} words · {tr['speech_s'] or 0:.0f}s · {tr['language']}")
        print("    " + " ".join((tr["text"] or "").split())[:600])
    if frames > 0:
        times = [round((i + 0.5) * float(c["duration"] or 0) / frames, 2) for i in range(frames)]
        print("  frames")
        for t in times:
            out = cfg.p("frames", f"{cid}_{t:08.2f}.jpg")
            if not os.path.exists(out):
                media.frame_at(cfg, c["path"], t, out, width=max(dw, 2) // 2 * 2)
            print(f"    {t:7.2f}s  {out}")


# -- paged query (studio) --------------------------------------------------------------------
# `search` above is the operator's CLI: it scores in Python over every clip. The studio needs pages
# of a 50k-clip library in milliseconds, so this does the filtering, scoring, ordering and paging in
# SQL. Per-clip medians of the metrics windows live in `clip_stats`, refreshed incrementally.

STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_stats (
  clip_id TEXT PRIMARY KEY, shake REAL, exposure REAL, sharpness REAL, faces INTEGER,
  auto_q REAL, n INTEGER, clip_updated REAL
);
CREATE INDEX IF NOT EXISTS transcripts_words ON transcripts(n_words);
CREATE INDEX IF NOT EXISTS clips_duration ON clips(duration);
"""


def refresh_stats(con, force=False) -> int:
    """Bring clip_stats up to date for clips analyzed since the last refresh. Returns rows written."""
    con.executescript(STATS_SCHEMA)
    q = ("SELECT c.id, c.updated FROM clips c LEFT JOIN clip_stats s ON s.clip_id = c.id "
         "WHERE c.status = 'analyzed' AND (s.clip_id IS NULL OR COALESCE(s.clip_updated, -1) != COALESCE(c.updated, -1))")
    todo = con.execute("SELECT id, updated FROM clips WHERE status='analyzed'" if force else q).fetchall()
    n = 0
    for i in range(0, len(todo), 500):
        chunk = todo[i:i + 500]
        ids = [r["id"] for r in chunk]
        vals = {cid: [] for cid in ids}
        for r in con.execute(f"SELECT clip_id, shake, exposure, sharpness, faces FROM metrics WHERE clip_id IN "
                             f"({','.join('?' * len(ids))})", ids):
            vals[r["clip_id"]].append(r)
        rows = []
        for r in chunk:
            ms = vals[r["id"]]
            if not ms:
                rows.append((r["id"], None, None, None, None, None, 0, r["updated"]))
                continue
            sh = statistics.median([m["shake"] for m in ms if m["shake"] is not None] or [0])
            ex = statistics.median([m["exposure"] for m in ms if m["exposure"] is not None] or [0])
            shp = min((m["sharpness"] for m in ms if m["sharpness"] is not None), default=0)
            fc = max((m["faces"] or 0 for m in ms), default=0)
            aq = analyze.auto_quality({"shake": sh, "exposure": ex, "sharpness": 100})
            rows.append((r["id"], sh, ex, shp, fc, aq, len(ms), r["updated"]))
        con.executemany("INSERT OR REPLACE INTO clip_stats VALUES (?,?,?,?,?,?,?,?)", rows)
        n += len(rows)
    if n:
        con.commit()
    return n


_TRUE = "('true','yes','1','y')"
_STATS_CHECKED = {}


def query(cfg, con, text="", setting=None, time=None, people=None, shot=None, camera=None, max_shake=None,
          min_quality=None, speech=None, creator=False, min_dur=None, offset=0, limit=200,
          include_unusable=False, day=None, highlights=False, dedupe=False, session=None, near=None,
          event_kind=None) -> dict:
    """Filtered, scored, paged clip search in SQL. Same filters and ranking as `search`
    (tags x3, speech x2, path x1 per word; then quality, then duration). speech: True = with
    speech, False = without, None = either. Returns {total, offset, limit, rows}."""
    import time as _time
    key = str(getattr(cfg, "db_path", ""))
    if _time.time() - _STATS_CHECKED.get(key, 0) > 30:          # new analysis shows up within 30 s
        refresh_stats(con)
        _STATS_CHECKED[key] = _time.time()
    words = [_norm(w) for w in (text or "").split() if _norm(w)]
    where, args = ["c.status != 'error'"], []
    analyze.ensure_scale_schema(con)
    if session not in (None, ""):
        where.append("se.session = ?")
        args.append(int(session))
    if dedupe:
        where.append("(d.clip_id IS NULL OR d.rep = c.id)")
    if event_kind:                                                # stage | crowd | wide (cutroom detect events)
        analyze.ensure_event_schema(con)
        where.append("c.id IN (SELECT clip_id FROM event_windows WHERE kind = ?)")
        args.append(str(event_kind))
    if highlights:
        where.append("COALESCE(hp.score, 0) >= ?")
        args.append(HL_MIN)
    near_pos = {}
    if near:
        lst = analyze.near(cfg, con, near, limit=max(100, int(limit or 200)))
        near_pos = {x["id"]: (i, x["dist"]) for i, x in enumerate(lst)}
        ids = list(near_pos) or ["-"]
        where.append(f"c.id IN ({','.join('?' * len(ids))})")
        args += ids
    tag = lambda k: f"lower(COALESCE(json_extract(t.data, '$.{k}'), ''))"     # noqa: E731
    if not include_unusable:
        where.append(f"(t.data IS NULL OR json_type(t.data, '$.usable') IS NULL OR "
                     f"lower(CAST(json_extract(t.data, '$.usable') AS TEXT)) IN {_TRUE})")
    for key, want in (("setting", setting), ("time_of_day", time), ("people", people), ("shot", shot),
                      ("camera", camera)):
        if want:
            where.append(f"instr({tag(key)}, ?) > 0")
            args.append(str(want).lower())
    if creator:
        where.append(f"lower(CAST(json_extract(t.data, '$.creator_visible') AS TEXT)) IN {_TRUE}")
    if speech is True:
        where.append("COALESCE(tr.n_words, 0) > 5")
    elif speech is False:
        where.append("COALESCE(tr.n_words, 0) <= 5")
    if day:                                                       # "YYYY-MM-DD", or "unknown"
        if day == "unknown":
            where.append("(c.shot_at IS NULL OR c.shot_at = '')")
        else:
            where.append("substr(c.shot_at, 1, 10) = ?")
            args.append(str(day))
    if min_dur:
        where.append("COALESCE(c.duration, 0) >= ?")
        args.append(float(min_dur))
    if max_shake is not None:
        where.append("s.shake IS NOT NULL AND s.shake <= ?")
        args.append(float(max_shake))
    qexpr = ("COALESCE(CASE WHEN json_type(t.data, '$.quality') IN ('integer','real') THEN json_extract(t.data, '$.quality') "
             "WHEN CAST(json_extract(t.data, '$.quality') AS REAL) > 0 THEN CAST(json_extract(t.data, '$.quality') AS REAL) END, s.auto_q)")
    if min_quality is not None:
        where.append(f"COALESCE({qexpr}, 0) >= ?")
        args.append(float(min_quality))
    score_parts, score_args = [], []
    for w in words:
        ht, hs, hp = "instr(lower(COALESCE(t.data, '')), ?) > 0", "instr(lower(COALESCE(tr.text, '')), ?) > 0", \
            "instr(lower(COALESCE(c.rel, '')), ?) > 0"
        where.append(f"({ht} OR {hs} OR {hp})")
        args += [w, w, w]
        score_parts.append(f"3 * ({ht}) + 2 * ({hs}) + ({hp})")
        score_args += [w, w, w]
    score = " + ".join(score_parts) or "0"
    speech_hit = " OR ".join(["instr(lower(COALESCE(tr.text, '')), ?) > 0"] * len(words)) or "0"
    frm = ("FROM clips c LEFT JOIN tags t ON t.clip_id = c.id LEFT JOIN transcripts tr ON tr.clip_id = c.id "
           "LEFT JOIN clip_stats s ON s.clip_id = c.id LEFT JOIN highlight_peaks hp ON hp.clip_id = c.id "
           "LEFT JOIN dupes d ON d.clip_id = c.id LEFT JOIN sessions se ON se.clip_id = c.id WHERE " + " AND ".join(where))
    order = "score DESC, COALESCE(quality, 0) DESC, COALESCE(c.duration, 0) DESC, c.id"
    order_args = []
    if near_pos:
        order = "CASE c.id " + " ".join("WHEN ? THEN ?" for _ in near_pos) + " END, c.id"
        for cid, (i, _) in near_pos.items():
            order_args += [cid, i]
    elif highlights:
        order = "score DESC, COALESCE(hp.score, 0) DESC, COALESCE(quality, 0) DESC, c.id"
    total = con.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))
    sql = (f"SELECT c.id, c.rel, c.duration, c.width, c.height, c.rotation, c.fps, c.proxy, c.sheet, c.status, "
           f"c.shot_at, c.has_audio, t.data AS tags, COALESCE(tr.n_words, 0) AS n_words, tr.clip_id AS has_tr, "
           f"s.shake AS shake, {qexpr} AS quality, ({score}) AS score, ({speech_hit}) AS speech_hit, "
           f"hp.score AS hl_score, hp.t AS hl_t, hp.kind AS hl_kind, d.rep AS dupe_rep, d.grp AS dupe_grp, "
           f"se.session AS session {frm} ORDER BY {order} LIMIT ? OFFSET ?")
    rows = con.execute(sql, score_args + list(words) + args + order_args + [limit, offset]).fetchall()
    out = []
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else {}
        dw, dh = display_size(r)
        out.append({"id": r["id"], "rel": r["rel"], "duration": r["duration"], "width": dw, "height": dh,
                    "fps": r["fps"], "quality": None if r["quality"] is None else round(float(r["quality"]), 2),
                    "shake": None if r["shake"] is None else round(r["shake"], 2), "status": r["status"],
                    "shot_at": r["shot_at"], "has_audio": bool(r["has_audio"]),
                    "has_transcript": bool(r["has_tr"]), "n_words": r["n_words"], "has_proxy": bool(r["proxy"]),
                    "has_sheet": bool(r["sheet"]), "score": r["score"],
                    "tags": {k: tags.get(k) for k in ("summary", "setting", "time_of_day", "people", "shot", "mood",
                                                       "quality", "usable", "creator_visible") if k in tags},
                    "tagged": bool(tags),
                    "highlight": None if r["hl_score"] is None else {"score": round(r["hl_score"], 3), "t": r["hl_t"],
                                                                     "kind": r["hl_kind"]},
                    "session": r["session"],
                    "dupe_of": r["dupe_rep"] if r["dupe_rep"] and r["dupe_rep"] != r["id"] else None,
                    "dupe_group": r["dupe_grp"],
                    "near_dist": near_pos.get(r["id"], (0, None))[1] if near_pos else None,
                    "hit_t": first_hit(cfg, r["id"], words) if words and r["speech_hit"] else None})
    return {"total": total, "offset": offset, "limit": limit, "rows": out}


def days(con) -> list:
    """Clips grouped by capture date: [{day, clips, hours, speech_clips}], newest first."""
    rows = con.execute(
        "SELECT COALESCE(NULLIF(substr(c.shot_at, 1, 10), ''), 'unknown') AS day, COUNT(*) AS n, "
        "COALESCE(SUM(c.duration), 0) AS dur, SUM(CASE WHEN COALESCE(tr.n_words, 0) > 5 THEN 1 ELSE 0 END) AS sp "
        "FROM clips c LEFT JOIN transcripts tr ON tr.clip_id = c.id WHERE c.status != 'error' "
        "GROUP BY day ORDER BY day DESC").fetchall()
    by_day = {}
    for s_ in analyze.session_list(con):
        by_day.setdefault(s_["day"], []).append(s_)
    return [{"day": r["day"], "clips": r["n"], "hours": round(r["dur"] / 3600, 2), "speech_clips": r["sp"] or 0,
             "sessions": sorted(by_day.get(r["day"], []), key=lambda x: x["start"] or "")}
            for r in rows]
