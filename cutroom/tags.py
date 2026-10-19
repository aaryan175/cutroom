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

def search(cfg, con, args):
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
                    "sheet": r["sheet"], "proxy": r["proxy"], "tags": tags})
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


def describe(cfg, con, ref, frames=0):
    c = db.get_clip(con, ref)
    cid = c["id"]
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
