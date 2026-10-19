"""`cutroom estimate`: how long and how much disk the overnight understanding will take, before starting.

    cutroom estimate [--tb 4 | --hours 100] [--bench] [--json]

It reads the library (after `cutroom index --skip-proxies --skip-analysis`, or any later point) read-only,
takes per-stage speeds measured on THIS machine (overnight/rates.json written by earlier overnight runs, the
live overnight/state.json, or a 30 s benchmark with --bench) and projects the remaining work. --tb / --hours
scale the same footage mix (bitrate, clip length, speech share) up to a bigger library.
Speeds that were not measured are labelled "assumed".
"""
import json
import math
import os
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from . import media

# used only when nothing was measured (labelled "assumed" in the output)
ASSUMED = {
    "proxies": 2.0,          # x realtime, wall clock with the governor's heavy slots (4K 10-bit -> 540p, hardware)
    "analyze": 10.0,         # x realtime, wall clock (2 workers on 540p proxies)
    "vad": 120.0,            # x realtime (ffmpeg decode + Silero VAD)
    "asr": {"tiny": 12.0, "base": 8.0, "small": 4.0, "medium": 1.8, "large-v3": 0.8, "large-v3-turbo": 2.5},
    "sheet_s": 0.6,          # seconds per clip
    "tag_call_s": 55.0,      # one tagging call (20-90 s measured)
    "speech_share": 0.35,    # share of footage with speech
    "redundant_share": 0.12, # near-duplicates that inherit tags instead of a call
    "gated_share": 0.10,     # clips the quality gate skips
}
SHEET_KB = 110.0
DB_KB_PER_S = 0.53


def _ro(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _q(con, sql, *a, default=0):
    try:
        r = con.execute(sql, a).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error:
        return default


def library_facts(cfg, con) -> dict:
    n = _q(con, "SELECT COUNT(*) FROM clips WHERE status != 'error'")
    probed = _q(con, "SELECT COUNT(*) FROM clips WHERE duration IS NOT NULL AND status != 'error'")
    secs = float(_q(con, "SELECT SUM(duration) FROM clips WHERE status != 'error'", default=0.0))
    size = float(_q(con, "SELECT SUM(size) FROM clips WHERE status != 'error' AND duration > 0", default=0.0))
    with_audio = float(_q(con, "SELECT SUM(duration) FROM clips WHERE has_audio=1 AND status != 'error'", default=0.0))
    proxied_s = float(_q(con, "SELECT SUM(duration) FROM clips WHERE proxy IS NOT NULL", default=0.0))
    analyzed_s = float(_q(con, "SELECT SUM(duration) FROM clips WHERE status='analyzed'", default=0.0))
    sheets = _q(con, "SELECT COUNT(*) FROM clips WHERE sheet IS NOT NULL")
    tr_n = _q(con, "SELECT COUNT(*) FROM transcripts")
    tr_s = float(_q(con, "SELECT SUM(c.duration) FROM transcripts t JOIN clips c ON c.id=t.clip_id", default=0.0))
    speech_s = float(_q(con, "SELECT SUM(speech_s) FROM transcripts", default=0.0))
    tagged = _q(con, "SELECT COUNT(*) FROM tags")
    redundant = _q(con, "SELECT COUNT(*) FROM dupes WHERE clip_id != rep")
    gated = _q(con, "SELECT COUNT(*) FROM tag_gate")
    analyzed_n = _q(con, "SELECT COUNT(*) FROM clips WHERE status='analyzed'")
    res = {"clips": n, "probed": probed, "hours": secs / 3600, "bytes": size, "audio_hours": with_audio / 3600,
           "proxied_hours": proxied_s / 3600, "analyzed_hours": analyzed_s / 3600, "sheets": sheets,
           "transcribed": tr_n, "transcribed_hours": tr_s / 3600, "speech_hours": speech_s / 3600,
           "tagged": tagged, "redundant": redundant, "gated": gated, "analyzed": analyzed_n,
           "mean_clip_s": secs / n if n else 0.0, "gb_per_hour": size / 1e9 / (secs / 3600) if secs else 0.0}
    res["speech_share"] = (speech_s / tr_s) if tr_s > 600 else None
    res["redundant_share"] = redundant / analyzed_n if analyzed_n >= 50 else None
    res["gated_share"] = gated / analyzed_n if analyzed_n >= 50 else None
    # proxy bytes per second actually on disk (if any proxies exist)
    pdir = cfg.p("proxies")
    try:
        sizes = [(e.stat().st_size) for e in os.scandir(pdir) if e.name.endswith(".mp4") and not e.name.endswith(".tmp.mp4")]
    except OSError:
        sizes = []
    res["proxy_bytes"] = float(sum(sizes))
    res["proxy_mb_per_s"] = (sum(sizes) / 1e6 / proxied_s) if proxied_s > 60 and sizes else None
    return res


def measured_rates(cfg) -> dict:
    """{stage: (x_realtime or per-hour figure, source)} from rates.json, then the live state.json."""
    out = {}
    od = cfg.p("overnight")
    try:
        rates = json.loads((od / "rates.json").read_text())
    except (OSError, ValueError):
        rates = {}
    for stage, hist in rates.items():
        xs = [h["x_realtime"] for h in hist[-3:] if h.get("x_realtime")]
        if xs:
            out[stage] = (statistics.median(xs), f"measured (last {len(xs)} run(s))")
        if stage == "transcribe":
            for h in reversed(hist):
                if h.get("model") and h.get("x_realtime"):
                    out[f"asr:{h['model']}"] = (h["x_realtime"], "measured")
                    break
    try:
        st = json.loads((od / "state.json").read_text())
    except (OSError, ValueError):
        st = {}
    now = st.get("updated") or time.time()
    for stage, s in (st.get("stages") or {}).items():
        w, t0 = float(s.get("weight_done") or 0), s.get("started")
        el = (s.get("finished") or now) - (t0 or now)
        if w > 0 and el > 60 and stage not in out:
            src = "measured (current run)" if s.get("status") == "running" else "measured (last run)"
            out[stage] = (w / el, src)
            if stage == "transcribe" and s.get("model"):
                out[f"asr:{s['model']}"] = (w / el, src)
    if "tag" in (st.get("stages") or {}):
        s = st["stages"]["tag"]
        if s.get("calls") and s.get("started"):
            el = (s.get("finished") or now) - s["started"]
            out["tag_call_s"] = (el / s["calls"], "measured")
    out["_load"] = (st.get("throttle"), "state")
    return out


def bench(cfg, con, seconds=10.0, echo=print) -> dict:
    """30 s benchmark on one real clip: a proxy of `seconds` of it, the analysis of that proxy and the VAD.
    Writes only to a temporary folder (deleted afterwards); the footage is only read."""
    from . import analyze, asr, ingest
    row = con.execute("SELECT * FROM clips WHERE status != 'error' AND duration > ? ORDER BY duration DESC LIMIT 1",
                      (seconds + 2,)).fetchone()
    if not row:
        return {}
    d = Path(tempfile.mkdtemp(prefix="cutroom-bench-"))
    out = {}
    try:
        p = d / "bench.mp4"
        r = dict(row)
        args = ingest._proxy_args(cfg, r, row["path"], str(p), media.hw_encoder(cfg))
        args = ["-t", f"{seconds:.1f}"] + args
        t = time.time()
        media.ffmpeg(cfg, args, log=d / "ff.log", timeout=600)
        out["proxies"] = seconds / (time.time() - t)
        t = time.time()
        analyze.analyze_proxy(cfg.ffmpeg, str(p), seconds, cfg.ffprobe)
        out["analyze"] = seconds / (time.time() - t)
        t = time.time()
        asr.speech_spans(asr.load_audio(cfg, str(p)))
        out["vad"] = seconds / (time.time() - t)
        out["proxy_mb_per_s"] = p.stat().st_size / 1e6 / seconds
    finally:
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
        d.rmdir()
    echo(f"bench on {row['rel']}: proxy {out.get('proxies', 0):.2f}x · analysis {out.get('analyze', 0):.1f}x · "
         f"VAD {out.get('vad', 0):.0f}x realtime (one process)")
    return out


def _fmt_h(h):
    if h is None:
        return "-"
    if h < 1:
        return f"{h * 60:.0f} min"
    return f"{h:.1f} h"


def estimate(cfg, con, tb=None, hours=None, do_bench=False, echo=print) -> dict:
    from . import governor
    facts = library_facts(cfg, con)
    rates = measured_rates(cfg)
    gov = governor.for_cfg(cfg)
    slots = gov.heavy_slots
    b = bench(cfg, con, echo=echo) if do_bench else {}

    def rate(stage, default):
        if stage in rates and rates[stage][0]:
            return rates[stage][0], rates[stage][1]
        if stage in b:
            par = slots * 0.8 if stage in ("proxies", "analyze") else 1.0
            return b[stage] * par, f"benchmark x{par:.1f} slots"
        return default, "assumed"

    model = getattr(cfg, "asr_model", "medium") or "medium"
    fast = (getattr(cfg, "asr_fast_model", "") or "").strip()
    asr_model = fast or model
    r_px, s_px = rate("proxies", ASSUMED["proxies"])
    r_an, s_an = rate("analyze", ASSUMED["analyze"])
    r_vad, s_vad = rate("vad", ASSUMED["vad"])
    if f"asr:{asr_model}" in rates:
        r_asr, s_asr = rates[f"asr:{asr_model}"]
    elif asr_model == model and "transcribe" in rates:
        r_asr, s_asr = rates["transcribe"]
    else:
        r_asr, s_asr = ASSUMED["asr"].get(asr_model, 1.8), "assumed"
    if getattr(cfg, "asr_remote", ""):
        s_asr += " (asr_remote set: the far machine's speed applies)"
    call_s, s_call = rates.get("tag_call_s", (ASSUMED["tag_call_s"], "assumed"))
    speech_share = facts["speech_share"] or ASSUMED["speech_share"]
    red = facts["redundant_share"] if facts["redundant_share"] is not None else ASSUMED["redundant_share"]
    gat = facts["gated_share"] if facts["gated_share"] is not None else ASSUMED["gated_share"]
    # new proxies follow the current settings (existing ones may have been made with older ones)
    pmb = b.get("proxy_mb_per_s") or (float(media.proxy_bitrate(cfg).rstrip("kK")) * 1000 + 96000) / 8 / 1e6
    batch = max(1, int(getattr(cfg, "tag_batch", 8) or 8))
    pause = float(getattr(cfg, "tag_sleep_s", 20) or 0)

    def project(total_h, done):
        """Stage times (h) and disk (GB) for a library of total_h hours, minus what is already done."""
        clips = total_h * 3600 / facts["mean_clip_s"] if facts["mean_clip_s"] else total_h * 3600 / 28.5
        frac = lambda d: max(0.0, 1 - d)                                                # noqa: E731
        px_h = total_h * frac(done.get("proxied", 0))
        an_h = total_h * frac(done.get("analyzed", 0))
        audio_h = total_h * (facts["audio_hours"] / facts["hours"] if facts["hours"] else 1.0)
        tx_h = audio_h * frac(done.get("transcribed", 0))
        sp_h = tx_h * speech_share
        tag_clips = clips * frac(done.get("tagged", 0)) * (1 - red) * (1 - gat)
        calls = math.ceil(tag_clips / batch)
        rows = [
            ("proxies", f"{px_h:.1f} h footage", px_h / r_px, s_px, f"{r_px:.2f}x"),
            ("sheets", f"{clips * frac(done.get('sheets', 0)):.0f} clips",
             clips * frac(done.get("sheets", 0)) * ASSUMED["sheet_s"] / 3600 / max(1, slots), "assumed", "0.6 s/clip"),
            ("analyze", f"{an_h:.1f} h footage", an_h / r_an, s_an, f"{r_an:.1f}x"),
            ("dedupe+highlights+sessions", f"{an_h:.1f} h footage", an_h / (r_vad * 0.6), "from VAD speed", "audio pass"),
            ("speech check (VAD)", f"{tx_h:.1f} h audio", tx_h / r_vad, s_vad, f"{r_vad:.0f}x"),
            (f"transcribe ({asr_model})", f"{sp_h:.1f} h speech", sp_h / r_asr, s_asr, f"{r_asr:.2f}x"),
        ]
        if fast and fast != model:
            r_full = rates.get(f"asr:{model}", (ASSUMED["asr"].get(model, 1.8), "assumed"))
            rows.append((f"upgrade pass ({model})", f"{sp_h:.1f} h speech", sp_h / r_full[0], r_full[1],
                         f"{r_full[0]:.2f}x"))
        rows.append(("AI tagging", f"{tag_clips:.0f} clips = {calls} calls", calls * (call_s + pause) / 3600, s_call,
                     f"{call_s:.0f}+{pause:.0f} s/call"))
        disk = {"proxies": total_h * 3600 * pmb / 1e3, "sheets": clips * SHEET_KB / 1e6,
                "database": total_h * 3600 * DB_KB_PER_S / 1e6 * 1.3, "transcripts": sp_h * 0.6 / 1e3}
        return {"hours": total_h, "clips": round(clips), "stages": rows, "disk_gb": disk,
                "total_h": sum(r[2] for r in rows if not r[0].startswith(("AI tagging", "upgrade"))),
                "tag_h": rows[-1][2], "calls": calls}

    done = {"proxied": facts["proxied_hours"] / facts["hours"] if facts["hours"] else 0,
            "analyzed": facts["analyzed_hours"] / facts["hours"] if facts["hours"] else 0,
            "transcribed": facts["transcribed_hours"] / facts["audio_hours"] if facts["audio_hours"] else 0,
            "sheets": facts["sheets"] / facts["clips"] if facts["clips"] else 0,
            "tagged": facts["tagged"] / facts["clips"] if facts["clips"] else 0}
    out = {"facts": facts, "slots": slots, "this_library": project(facts["hours"], done),
           "proxy_settings": {"height": int(getattr(cfg, "proxy_height", 540)), "bitrate": media.proxy_bitrate(cfg),
                              "mb_per_s": round(pmb, 4)}}
    target_h = None
    if tb:
        target_h = tb * 1e12 / (facts["bytes"] / facts["hours"]) if facts["bytes"] and facts["hours"] else tb * 25.0
    elif hours:
        target_h = float(hours)
    if target_h:
        out["projection"] = project(target_h, {})
        out["projection"]["label"] = f"{tb:g} TB" if tb else f"{hours:g} h"
    return out


def print_estimate(res, echo=print):
    f = res["facts"]
    echo(f"library: {f['clips']} clips · {f['hours']:.1f} h · {f['bytes'] / 1e12:.2f} TB "
         f"({f['gb_per_hour']:.1f} GB per hour, mean clip {f['mean_clip_s']:.1f}s) · heavy slots {res['slots']}")
    echo(f"done so far: proxies {f['proxied_hours']:.1f} h · analysed {f['analyzed_hours']:.1f} h · "
         f"transcribed {f['transcribed']} clips · tagged {f['tagged']}")
    ps = res.get("proxy_settings") or {}
    if ps:
        echo(f"proxies: {ps['height']}p at {ps['bitrate']} = {ps['mb_per_s'] * 3600 / 1000:.2f} GB per hour of footage"
             + (f" (existing proxies average {f['proxy_mb_per_s'] * 3600 / 1000:.2f} GB/h: made with other settings)"
                if f.get("proxy_mb_per_s") and abs(f["proxy_mb_per_s"] - ps["mb_per_s"]) > 0.2 * ps["mb_per_s"] else ""))
    for key in ("this_library", "projection"):
        p = res.get(key)
        if not p:
            continue
        title = "remaining work for this library" if key == "this_library" else \
            f"projection for {p['label']} of the same footage (~{p['hours']:.0f} h, ~{p['clips']} clips)"
        echo(f"\n{title}")
        echo(f"  {'stage':30s} {'work':26s} {'time':>9s}   rate / source")
        for name, work, h, src, r in p["stages"]:
            echo(f"  {name:30s} {work:26s} {_fmt_h(h):>9s}   {r} ({src})")
        echo(f"  {'machine time (without tagging)':30s} {'':26s} {_fmt_h(p['total_h']):>9s}")
        echo(f"  {'AI tagging (runs alongside)':30s} {'':26s} {_fmt_h(p['tag_h']):>9s}   "
             f"{p['calls']} calls; plan usage limits may stretch this over several nights")
        d = p["disk_gb"]
        echo(f"  disk: proxies {d['proxies']:.1f} GB · sheets {d['sheets']:.1f} GB · database {d['database']:.2f} GB · "
             f"transcripts {d['transcripts']:.2f} GB · total ~{sum(d.values()):.0f} GB "
             "(+ voice WAVs and render cache per edit, ~2-3 GB each)")
