"""cutroom command line. Every command is safe to re-run; work is resumable and cached.

  cutroom init WORK --library DIR [--library DIR2] [--music DIR]
  cutroom doctor
  cutroom index [--limit N] [--skip-proxies] [--skip-analysis]      scan + probe + proxies + sheets + shots/metrics
                [--files F ... | --files-from LIST]                  ... or just these files (inside a library folder)
  cutroom transcribe [--limit N] [--model medium] [--clip REF]      word-timestamp ASR for clips with speech
  cutroom status                                                     library counts + what is left to do
  cutroom tags todo [-n 20]                                          untagged clips with their contact sheets
  cutroom tags show REF | cutroom tags set REF JSON [--replace]
  cutroom search [TEXT] [--setting X] [--time day] [--people creator] [--max-shake 4] [--min-quality 3]
                 [--speech] [--no-speech] [--limit 30] [--json]
                 [--highlights] [--dedupe] [--session N] [--near REF]          highlight moments, one per duplicate
                                                                              group, one shoot, look-alikes of a clip
                 [--kind crowd|stage|wide] [--soundbites]                     event footage: shot kind, quotable lines
  cutroom detect [dedupe|highlights|sessions|takes|events|all]       the cheap library passes (overnight runs them)
  cutroom recap NAME [--len 120] [--song X] [--ref REF]              event recap draft plan (event-recap skill)
  cutroom takes [--clip REF]                                         repeated lines and which take is best
  cutroom estimate [--tb 4 | --hours 100] [--bench] [--json]         time + disk for indexing, before you start
  cutroom clip REF [--frames 6]                                      everything known about one clip (+ frame grabs)
  cutroom say REF [--from S --to S]                                  transcript with timestamps
  cutroom frames REF T [T ...]                                       pull full-res frames to look at
  cutroom songs index | cutroom songs list | cutroom beats SONG
  cutroom matchcuts [--limit N] [--min-score 0.75]                   mine match-cut candidates
  cutroom plan check PLAN.json                                       validate against the library
  cutroom render PLAN.json [--preview] [--out FILE] [--only SECTION]
  cutroom qa VIDEO [--plan PLAN.json]                                machine QA + contact sheet
  cutroom export RENDER.mp4|PLAN.json --preset youtube|reels|tiktok|shorts|square|feed|all
                 [--cutdown 15,30,60] [--thumbs] [--no-captions] [--keep-burned] [--trim] [--fps N]
                                                                     platform files, 9:16 reframe, .srt/.vtt,
                                                                     chapters, thumbnails (docs/WORKFLOW.md)
  cutroom cutdown PLAN.json --len 30                                 a 15/30/60 s plan from an edit
  cutroom export-nle PLAN.json [--format premiere|fcpxml|resolve|otio|edl|all] [--zip] [--clean-render]
                                                                     Premiere / Resolve / FCP hand-off on the masters
  cutroom export-nle --verify DIR                                    re-read it, compare every clip frame-exactly
  cutroom sync [--clips A B ...] | sync list | devices | name DEV "Cam A wide" | map G T --from A --to B
                                                                     multicam + recorder sync by audio
  cutroom sync switch PLAN SEC.SEG ANGLE [--audio CODE]              change a multicam segment's angle
  cutroom board [--port 8600]                                        review page for renders (phone friendly)
  cutroom notes [--since VERSION]                                    review notes captured on the board
  cutroom studio [--port 8600] [--host 127.0.0.1] [--open]           the browser editor (docs/STUDIO.md)
  cutroom comments [list|done|needs|reply|open ID] [--reply TEXT]    studio review comments (alias: markers)
         [--plan SLUG] [--all] [--json]                              done --reply "what changed" / needs --reply "Needs you: ..."
  cutroom ref add FILE|URL [--title T] [--no-analyze] [--link]        add a reference video and measure it
  cutroom ref list | ref show ID | ref analyze ID [--force] [--deep]  references: status, summary + sheets
                                                                     (--deep: AI brain pass, cost-capped --budget)
  cutroom ref cutmap ID [--json]                                     one row per shot, to rebuild one-to-one
  cutroom ref compare ID RENDER.mp4 [--json]                         your render vs the reference, gaps in words
  cutroom factory run|tick|status|orders|order ID|send|stop ID|retry ID|pause|resume
  cutroom factory answer ORDER Q N | verdict PLAN V APPROVE|REJECT|NOTES | lessons | brain SLUG
                                                                     the factory (docs/FACTORY.md)
"""
import argparse
import json
import os
import sys
from pathlib import Path


def _cfg_con():
    from .config import Config
    from . import db
    cfg = Config.load()
    return cfg, db.connect(cfg.db_path)


def cmd_init(a):
    from .config import init_project
    cfg = init_project(a.work, a.library, a.music or [])
    try:
        from .studio import ensure_project_claude
        ensure_project_claude(cfg)              # project CLAUDE.md + AGENTS.md (Codex) + .claude/skills link
    except Exception as e:  # noqa: BLE001
        print(f"  (could not link CLAUDE.md / AGENTS.md / skills: {e})")
    print(f"project ready at {cfg.work}\n  library: {cfg.library_roots}\n  next: cd {cfg.work} && cutroom doctor && cutroom index")


def _agent_checks(cfg):
    """Doctor rows for the AI agents (studio chat + overnight tagging). Either one is enough."""
    from . import agent
    rows = []
    try:
        d = agent.detect(cfg)
        chosen = agent.choose(cfg)
    except Exception as e:  # noqa: BLE001
        return [("AI agent", False, f"could not detect: {e}")]
    login = {True: "logged in", False: "NOT logged in", None: "login status unknown"}
    for name, label, fix in (("claude", "Claude Code (claude)", "run `claude` once and sign in"),
                             ("codex", "Codex CLI (codex)", "run `codex login`")):
        x = d[name]
        if not x["path"]:
            rows.append((label, False, "not installed (optional: Claude Code or Codex is enough)"))
        else:
            detail = f"{x['path']} · {login[x['logged_in']]}" + (f" ({x['method']})" if x.get("method") else "")
            if x["logged_in"] is False:
                detail += f" · to use it: {fix} (optional)"
            rows.append((label, x["logged_in"] is not False, detail))
    usable = chosen.path and d[chosen.name]["logged_in"] is not False
    rows.append(("AI agent in use", bool(usable),
                 f"{chosen.label} (setting: {agent.wanted(cfg)}; change with \"agent\" in cutroom.json)" if usable else
                 f"{chosen.label} is not ready: install Claude Code or Codex and log in with your own account "
                 "(subscription, no API key)"))
    return rows


def _factory_checks(cfg):
    """Doctor rows for the factory (docs/FACTORY.md): agent slots, free RAM, git for the extender, its folder."""
    rows = []
    try:
        from . import governor
        from .factory import fconfig, worktree
        g = governor.Governor(cfg)
        fr = governor.free_ram()
        rows.append(("factory: agent slots", True, f"{g.agent_slots} AI run(s) at a time machine-wide, "
                     f"{g.agent_mem_cap_gb:g} GB cap each (CUTROOM_AGENT_SLOTS overrides)"))
        ok_ram = fr is None or fr / governor.GB >= g.min_free_ram_gb
        rows.append(("factory: free RAM", ok_ram, (f"{fr / governor.GB:.1f} GB free" if fr is not None else "unknown")
                     + f" (agents wait below {g.min_free_ram_gb:.1f} GB)" + ("" if ok_ram else " (optional: it waits)")))
        ok, detail = worktree.available()
        rows.append(("factory: git (extender)", ok, detail + ("" if ok else " (optional: the extender is a stub)")))
        if getattr(cfg, "work", ""):
            d = fconfig.factory_dir(cfg)
            try:
                d.mkdir(parents=True, exist_ok=True)
                t = d / ".doctor"
                t.write_text("ok")
                t.unlink()
                fc = fconfig.load(cfg)
                rows.append(("factory: folder", True, f"{d} · autostart {fc['autostart']} · "
                             f"${fc['budgets']['cost_usd']:g} per order · agent {fc['agent']}"))
            except OSError as e:
                rows.append(("factory: folder", False, f"{d} not writable: {e}"))
    except Exception as e:  # noqa: BLE001
        rows.append(("factory", False, f"could not check: {e} (optional)"))
    return rows


def cmd_doctor(a):
    from .config import Config
    from .media import doctor
    try:
        cfg = Config.load()
    except SystemExit:
        cfg = Config()
    bad = 0
    for name, ok, detail in doctor(cfg) + _agent_checks(cfg) + _factory_checks(cfg) + _camera_checks(cfg):
        print(f"  {'ok ' if ok else 'MISSING'}  {name:34s} {detail}")
        bad += (not ok) and "optional" not in detail
    sys.exit(1 if bad else 0)


def _camera_checks(cfg):
    """doctor: camera RAW decoding and the colour pipeline's LUTs (all optional: what is missing only limits matching)."""
    from . import cameras as CAM
    out = []
    st = CAM.braw_sdk_status()
    if st["decoder"]:
        out.append(("Blackmagic RAW decoder", True, st["helper"]))
    else:
        why = ("framework present (" + st["framework"][0] + ") but no SDK headers to build a decoder"
               if st["framework"] else "no Blackmagic RAW SDK")
        alt = [x for x, ok in (("DaVinci Resolve proxies (tools/resolve_braw_proxies.py)", st["resolve"]),
                               ("Blackmagic Proxy Generator", st["proxy_generator"])) if ok]
        out.append(("Blackmagic RAW (.braw) decode", False, f"optional: {why}; .braw needs a ProRes stand-in"
                    + (f" via {' or '.join(alt)}" if alt else " (record ProRes / proxies in camera)")))
    out.append(("ARRIRAW (.ari/.arx/.mxf) decode", False, "optional: unsupported; record ProRes (the AMIRA default)"))
    if cfg.work:
        try:
            show = CAM.show_transform(cfg, {})
            out.append(("show LUT (ARRI LogC3 -> Rec.709)", show["kind"] == "lut",
                        (show["path"] + f" [{show['src']}]") if show["path"] else f"optional: {show['src']}"))
        except Exception as e:  # noqa: BLE001
            out.append(("show LUT (ARRI LogC3 -> Rec.709)", False, f"optional: {e}"))
        v = CAM.vendor_lut("bmd_film_gen5") or CAM._user_lut(cfg, ["bmd.cube", "bmd_film_gen5.cube"])
        out.append(("Blackmagic Film Gen 5 LUT", bool(v), v or "optional: put Blackmagic's Gen 5 Film to Video LUT at "
                                                              "luts/bmd.cube (no public formula)"))
        try:
            from . import db as DB
            con = DB.connect(cfg.db_path)
            raw = CAM.raw_report(cfg, con)
            nb = len(raw["braw"])
            miss = sum(1 for x in raw["braw"] if not x["standin"])
            if nb:
                out.append((f"{nb} .braw clip(s) in the library", miss == 0,
                            "all have a stand-in" if not miss else f"optional: {miss} without a stand-in (cutroom braw)"))
            if raw["arriraw"]:
                out.append((f"{len(raw['arriraw'])} ARRIRAW clip(s)", False, "optional: need ProRes stand-ins"))
        except Exception:  # noqa: BLE001
            pass
    return out


def cmd_cameras(a):
    from . import cameras as CAM
    cfg, con = _cfg_con()
    act = a.action or "list"
    if act == "identify" or (act == "list" and not CAM.all_clips(con)):
        r = CAM.identify_all(cfg, con, force=a.force, echo=print)
        if act == "identify":
            print(f"cameras: {r['identified']} clip(s) identified")
            return
    if act == "list":
        cams = CAM.cameras(con)
        if a.json:
            print(json.dumps(cams, indent=1, default=str))
            return
        if not cams:
            print("no cameras identified yet (cutroom index, then cutroom cameras identify)")
        for c in cams:
            print(f"{c['key']:28s} {c['label']:34s} {c['clips']:5d} clip(s) {c['seconds'] / 60:7.1f} min · "
                  f"days {', '.join(c['days'][:4])}{' ...' if len(c['days']) > 4 else ''} · conf {c['confidence']:.2f}")
            print(f"{'':28s} profiles: " + ", ".join(f"{k}: {v}" for k, v in c["profiles"].items())
                  + (f" · serials {', '.join(c['serials'][:3])}" if c["serials"] else "")
                  + (f" · {c['slowmo']} slow-mo candidate(s) (50p)" if c["slowmo"] else "")
                  + (f" · {c['raw']} RAW ({c['raw_missing']} without stand-in)" if c["raw"] else ""))
            st = CAM.ct_settings({})
            it = CAM.input_transform(cfg, {"cam_key": c["key"], "family": c["family"],
                                           "log_profile": max(c["profiles"], key=c["profiles"].get).split(" (")[0]}, st)
            print(f"{'':28s} transform: {it['kind']}" + (f" ({it.get('path')})" if it.get("path") else "")
                  + (f" · {it['why']}" if it.get("why") else ""))
            for row in con.execute("SELECT day, data FROM camera_match WHERE cam_key=?", (c["key"],)):
                d = json.loads(row["data"])
                print(f"{'':28s} match {row['day']}: ΔE00 {d.get('de_before')} -> {d.get('de_after')} · gains {d.get('gains')}"
                      f" sat {d.get('sat')}{' (left off)' if d.get('disabled') else ''}")
        return
    if act == "clips":
        rows = con.execute("""SELECT k.*, c.rel, c.shot_at, c.fps FROM clip_camera k JOIN clips c ON c.id=k.clip_id
                              ORDER BY c.shot_at, c.rel""").fetchall()
        from . import sync as SY
        out = []
        for r in rows:
            if a.camera and a.camera not in (r["cam_key"], r["family"]):
                continue
            cs = None
            try:
                cs = SY.corrected_start(con, r["clip_id"])
            except Exception:  # noqa: BLE001
                cs = None
            out.append({**dict(r), "corrected_start": cs})
        out.sort(key=lambda r: (r["corrected_start"] or 0, r["rel"]))
        if a.json:
            print(json.dumps(out, indent=1, default=str))
            return
        import datetime as _dt
        for r in out:
            ts = _dt.datetime.fromtimestamp(r["corrected_start"]).strftime("%Y-%m-%d %H:%M:%S") if r["corrected_start"] else "-"
            print(f"{ts}  {r['cam_key']:24s} {r['log_profile'] or '?':14s} {r['profile_src'] or '-':9s} "
                  f"conf {r['confidence'] or 0:.2f}{' 50p' if (r['fps'] or 0) >= 47 else ''}  {r['rel']}")
        return
    if act == "show":
        from . import db
        r = CAM.get(con, db.get_clip(con, a.args[0])["id"])
        print(json.dumps(r, indent=1, default=str))
        return
    if act == "set":
        if not a.args:
            sys.exit("usage: cutroom cameras set CLIP --profile P [--family F] [--model M] [--all]")
        ids = CAM.set_override(con, a.args[0], family=a.family, model=a.model, profile=a.profile,
                               scope="camera" if a.all else "clip")
        print(f"corrected {len(ids)} clip(s)")
        return
    if act == "match":
        res = CAM.match_all(cfg, con, matrix=a.matrix, echo=print)
        bad = [r for r in res if not r.get("ok")]
        for r in bad:
            print(f"  {r['cam_key']} {r.get('day') or ''}: not matched: {r.get('why')}")
        if a.json:
            print(json.dumps(res, indent=1, default=str))
        return
    if act == "luts":
        out = Path(a.out or cfg.p("renders", "camera_luts"))
        res = CAM.export_luts(cfg, con, out)
        for x in res:
            print(f"{Path(x['file']).name:40s} {len(x['clips'])} clip(s) · {x['profile']} ({x['profile_src']}) · {x['input']}"
                  + (" · matched" if x["match"] else "") + (f" · + {Path(x['logc3_file']).name}" if x.get("logc3_file") else ""))
        print(f"-> {out}")
        return
    sys.exit(f"unknown cameras action {act!r}")


def cmd_braw(a):
    from . import cameras as CAM
    cfg, con = _cfg_con()
    rep = CAM.raw_report(cfg, con)
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
        return
    sdk = rep["sdk"]
    print("Blackmagic RAW decode on this machine:")
    print(f"  framework: {', '.join(sdk['framework']) or 'none'}")
    print(f"  SDK headers: {', '.join(sdk['headers']) or 'not installed (Blackmagic RAW SDK)'}")
    print(f"  decoder helper: {sdk['helper'] or 'not built'} · DaVinci Resolve: {'yes' if sdk['resolve'] else 'no'}"
          f" · Proxy Generator: {'yes' if sdk['proxy_generator'] else 'no'}")
    for kind, label in (("braw", "Blackmagic RAW"), ("arriraw", "ARRIRAW")):
        rows = rep[kind]
        if not rows:
            continue
        miss = [r for r in rows if not r["standin"]]
        print(f"{label}: {len(rows)} clip(s), {len(miss)} without a stand-in")
        for r in rows[: a.limit]:
            print(f"  {'ok ' if r['standin'] else 'MISSING'} {r['rel']}" + (f" <- {r['standin']}" if r["standin"] else ""))
    if any(not r["standin"] for r in rep["braw"]):
        tool = Path(__file__).resolve().parents[1] / "tools" / "resolve_braw_proxies.py"
        print("\nMake stand-ins (ProRes, same clip name) in <work>/raw_proxies/, then run cutroom index again:")
        print(f"  DaVinci Resolve Studio (scripting): python3 {tool} --work {cfg.work}")
        print("  DaVinci Resolve (free): Media pool > select the clips > Generate Proxy Media; or a Deliver page render "
              "('Individual clips', ProRes 422 Proxy, file name = source name) into that folder")
        print("  or Blackmagic Proxy Generator (writes a Proxy folder beside the clips; cutroom finds it)")
        print("  or record ProRes / camera proxies on the Pocket 6K Pro")


def cmd_index(a):
    from . import ingest, analyze
    cfg, con = _cfg_con()
    ids = None
    if a.files or a.files_from:                          # just these files (the studio's Import)
        paths = list(a.files or [])
        if a.files_from:
            from pathlib import Path
            paths += [x.strip() for x in Path(a.files_from).read_text().splitlines() if x.strip()]
        ids = set(ingest.register_files(cfg, con, paths))
        if not ids:
            sys.exit("index: none of those files are inside a library folder")
        steps = 4 - bool(a.skip_proxies) * 2 - bool(a.skip_analysis)
        print(f"index-files {len(ids)} clips · {steps} steps", flush=True)
    else:
        ingest.scan(cfg, con)
        from . import cameras as CAM
        CAM.raw_report(cfg, con)                         # RAW clips whose ProRes stand-in appeared get indexed now
    print("index-step probe", flush=True)
    ingest.probe_all(cfg, con, ids=ids)
    if not a.skip_proxies:
        print("index-step proxies", flush=True)
        ingest.make_proxies(cfg, con, limit=a.limit, ids=ids)
        print("index-step sheets", flush=True)
        ingest.make_sheets(cfg, con, limit=a.limit, ids=ids)
    if not a.skip_analysis:
        print("index-step analyze", flush=True)
        analyze.analyze_all(cfg, con, limit=a.limit, ids=ids)
    cmd_status(a)


def cmd_transcribe(a):
    from . import asr
    cfg, con = _cfg_con()
    asr.transcribe_all(cfg, con, limit=a.limit, model=a.model, only=a.clip)


def cmd_status(a):
    cfg, con = _cfg_con()
    q = lambda s: con.execute(s).fetchone()[0]
    st = lambda s: q(f"SELECT COUNT(*) FROM clips WHERE status='{s}'")  # no quotes-in-f-string (py<3.12)
    print(f"clips {q('SELECT COUNT(*) FROM clips')} · hours {q('SELECT COALESCE(SUM(duration),0) FROM clips')/3600:.1f} · "
          f"proxied {q('SELECT COUNT(*) FROM clips WHERE proxy IS NOT NULL')} · "
          f"analyzed {st('analyzed')} · errors {st('error')}")
    print(f"transcribed {q('SELECT COUNT(*) FROM transcripts')} · with speech "
          f"{q('SELECT COUNT(*) FROM transcripts WHERE n_words>5')} · tagged {q('SELECT COUNT(*) FROM tags')} · "
          f"songs {q('SELECT COUNT(*) FROM songs')} · matchcut candidates {q('SELECT COUNT(*) FROM matchcuts')}")


def cmd_tags(a):
    from . import tags, db
    cfg, con = _cfg_con()
    if a.action == "todo":
        tags.todo(cfg, con, a.n)
    elif a.action == "show":
        print(json.dumps(db.get_tags(con, db.get_clip(con, a.ref)["id"]), indent=2))
    elif a.action == "set":
        c = db.get_clip(con, a.ref)
        print(json.dumps(db.set_tags(con, c["id"], json.loads(a.json), merge=not a.replace), indent=2))
    elif a.action == "fields":
        for k, v in db.TAG_FIELDS.items():
            print(f"  {k:18s} {v}")


def cmd_search(a):
    from . import tags
    cfg, con = _cfg_con()
    tags.search(cfg, con, a)


def cmd_clip(a):
    from . import tags
    cfg, con = _cfg_con()
    tags.describe(cfg, con, a.ref, frames=a.frames)


def cmd_say(a):
    from . import asr, db
    cfg, con = _cfg_con()
    c = db.get_clip(con, a.ref)
    asr.print_transcript(cfg, c["id"], a.start, a.end)


def cmd_frames(a):
    from . import db, media
    cfg, con = _cfg_con()
    c = db.get_clip(con, a.ref)
    for t in a.t:
        out = cfg.p("frames", f"{c['id']}_{float(t):08.2f}.jpg")
        media.frame_at(cfg, c["path"], float(t), out, width=a.width)
        print(out)


def cmd_title(a):
    """cutroom title place <plan> [--choose-shot]: where each typed title would sit with pos "auto" (measured
    on the held shot under it), and with --choose-shot which cold-open pool shot suits it best."""
    from pathlib import Path
    from . import cards as CD
    from . import db, titleplace as TP
    from .render import resolve_timeline
    cfg, con = _cfg_con()
    plan = json.loads(Path(a.plan).read_text())
    tl = resolve_timeline(cfg, con, plan, preview=True)
    pcs = tl.get("pieces") or []
    co = plan.get("cold_open") or {}
    ti = co.get("title") or {}
    ti = {"text": ti} if isinstance(ti, str) else dict(ti)
    out = []

    def path_of(cid):
        r = dict(db.get_clip(con, cid))
        return r["proxy"] if r.get("proxy") and Path(r["proxy"]).exists() else r["path"]
    for t in [p for p in pcs if p.get("kind") == "typed_title"]:
        mid = (t["start"] + t["end"]) / 2
        under = next((p for p in pcs if p.get("track") == "V1" and p.get("clip") and p["start"] <= mid < p["end"]), None)
        row = {"text": t.get("label"), "t0": t["start"], "t1": t["end"]}
        if under is None:
            row["why"] = "no clip under the title"
            out.append(row)
            continue
        sp = float(under.get("speed") or 1.0)
        a0 = float(under["in"]) + (t["start"] - under["start"]) * sp
        a1 = a0 + (min(t["end"], under["end"]) - t["start"]) * sp
        size, color = float(ti.get("size", CD.TITLE["size"])), ti.get("color", CD.TITLE["color"])
        r = TP.place(cfg, path_of(under["clip"]), a0, a1, row["text"] or "", size, color)
        row.update(clip=under.get("clip_rel") or under["clip"], src=[round(a0, 3), round(a1, 3)],
                   pos={"x": r["x"], "y": r["y"]}, ok=r.get("ok"), backing=r.get("backing"), why=r.get("why"))
        if a.debug_dir:
            fr = TP.sample_frames(cfg, path_of(under["clip"]), a0, a1, n=1)
            if fr:
                row["debug"] = TP.debug_image(fr[0], r, Path(a.debug_dir) / f"title_{under['clip']}_{a0:.2f}.jpg")
        if a.choose_shot:
            used = {}
            for p in pcs:
                if p.get("track") in ("V1", "V2") and p.get("clip") and p.get("in") is not None and p is not under \
                        and p["start"] < t["end"] + 10 and p["end"] > t["start"] - 10:
                    sp2 = float(p.get("speed") or 1.0)
                    used.setdefault(p["clip"], []).append((float(p["in"]), float(p["in"]) + (p["end"] - p["start"]) * sp2))
            cands = []
            for x in (co.get("shots") or []) + ([co["end_shot"]] if isinstance(co.get("end_shot"), dict)
                                                 and co["end_shot"].get("clip") else []):
                crow = dict(db.get_clip(con, x["clip"]))
                cid = crow["id"]
                cands.append({"clip": cid, "ref": x["clip"], "in": float(x.get("in") or 0.0), "path": path_of(cid),
                              "duration": crow.get("duration")})
            ch = TP.choose_shot(cfg, cands, row["text"] or "", size, color, dur=t["end"] - t["start"], used=used)
            names = {c["clip"]: c["ref"] for c in cands}
            row["choose_shot"] = {**ch, "ref": names.get(ch.get("clip")),
                                  "ranking": [{**r2, "ref": names.get(r2["clip"])} for r2 in ch.get("ranking") or []]}
        out.append(row)
    print(json.dumps(out, indent=1, default=str))


def cmd_songs(a):
    from . import music
    cfg, con = _cfg_con()
    if a.action == "index":
        music.index_songs(cfg, con)
    for r in con.execute("SELECT id, title, duration, tempo FROM songs ORDER BY title"):
        print(f"  {r['id']}  {r['tempo'] or 0:6.1f} bpm  {r['duration'] or 0:6.1f}s  {r['title']}")


def cmd_beats(a):
    from . import music
    cfg, con = _cfg_con()
    b = music.beats(cfg, con, a.song)
    print(json.dumps({"n": len(b), "first": [round(x, 3) for x in b[:16]]}))


def cmd_matchcuts(a):
    from . import matchcuts
    cfg, con = _cfg_con()
    matchcuts.mine(cfg, con, limit=a.limit, min_score=a.min_score)


def cmd_plan(a):
    from . import plan as P
    cfg, con = _cfg_con()
    p = P.normalize(P.load(a.plan), cfg)
    errs = P.validate(p, con)
    for e in errs:
        print("  ✗", e)
    print(f"{'INVALID' if errs else 'ok'} · {sum(len(s['segments']) for s in p['sections'])} segments · "
          f"~{P.estimate_duration(p):.0f}s before tightening")
    sys.exit(1 if errs else 0)


def cmd_render(a):
    from . import render
    cfg, con = _cfg_con()
    out = render.render_plan(cfg, con, a.plan, preview=a.preview, out=a.out, only=a.only)
    print(out)


def cmd_export(a):
    from . import export
    cfg, con = _cfg_con()
    cuts = [float(x) for x in (a.cutdown or "").split(",") if x.strip()]
    if not a.preset and not cuts and not a.thumbs:
        a.preset = ["youtube"]
    m = export.export(cfg, con, a.target, presets=a.preset or [], cutdowns=cuts, thumbs=a.thumbs,
                      captions=not a.no_captions, keep_burned=a.keep_burned, fps=a.fps, trim=a.trim,
                      preview=True if a.preview else None, max_dur=a.max_dur)
    if a.json:
        print(json.dumps(m, indent=1, default=str))
        return
    def show(mm, ind="  "):
        for o in mm.get("outputs", []):
            lo = o.get("loudness") or {}
            print(f"{ind}{o['name']:10s} {o['width']}x{o['height']} @{o['fps']:g} · {o['duration']:.2f}s · "
                  f"{o['size_mb']} MB · I {lo.get('I')} LUFS TP {lo.get('TP')} · {o['file']}")
        for k in ("srt", "vtt", "chapters", "thumb_sheet"):
            if mm.get(k):
                print(f"{ind}{k:10s} {mm[k]}")
    print(f"export -> {m['dir']}")
    show(m)
    for c in m.get("cutdowns", []):
        print(f"  cutdown {c['length']:g}s: plan {c['plan']} · render {c['render']}")
        show(c, "    ")
    for w in m.get("warnings", []) + [w for c in m.get("cutdowns", []) for w in c.get("warnings", [])]:
        print(f"  ! {w}")


def cmd_cutdown(a):
    from . import export
    cfg, con = _cfg_con()
    for L in [float(x) for x in str(a.len).split(",") if x.strip()]:
        print(export.build_cutdown(cfg, con, a.plan, L))


def cmd_export_nle(a):
    from . import nle
    cfg, con = _cfg_con()
    if a.verify:
        rep = nle.verify(cfg, con, a.verify, plan_path=a.plan)
        if a.json:
            print(json.dumps(rep, indent=1, default=str))
        else:
            print(f"verify {a.verify}: {'OK' if rep['ok'] else 'FAILED'} · {rep['frames']} frames @ {rep['fps']:g}")
            for k, c in rep["checks"].items():
                what = ("structure " + ("clean" if c["ok"] else "problems")) if k.endswith("_schema") else \
                    f"{c['matched']}/{c['expected']} clips frame-exact"
                tag = "ok " if c["ok"] else ("inf" if c.get("informational") else "BAD")
                print(f"  {tag} {k:24s} {what}{' (informational: third-party reader limits)' if c.get('informational') else ''}"
                      + "".join(f"\n      ! {p}" for p in c["problems"][:12]))
        sys.exit(0 if rep["ok"] else 1)
    if not a.plan:
        sys.exit("export-nle: pass a plan (or --verify DIR)")
    m = nle.export(cfg, con, a.plan, fmts=a.format or ["premiere"], out=a.out, render=a.render,
                   clean_render=a.clean_render, preview=a.preview, graphics=not a.no_graphics, make_zip=a.zip)
    if a.json:
        print(json.dumps({k: m[k] for k in ("dir", "outputs", "warnings", "formats") if k in m} | {"zip": m.get("zip")},
                         indent=1, default=str))
        return
    print(f"export-nle -> {m['dir']}")
    for o in m["outputs"]:
        print(f"  {Path(o).name}")
    print(f"  {len(m.get('graphics_media', []))} graphic(s) on V3 · {len(m.get('fx_media', []))} rendered segment(s) on V4"
          f" · {len(m.get('groups') or [])} multicam source sequence(s)")
    if m.get("zip"):
        print(f"  zip {m['zip']}")
    for w in m.get("warnings", []):
        print(f"  ! {w}")
    print("  Premiere: File > Import the .xml; README_PREMIERE.txt has the steps. Check: cutroom export-nle --verify DIR")


def cmd_sync(a):
    from . import sync
    cfg, con = _cfg_con()
    act = a.action or "detect"
    args = a.args or []
    if act == "detect":
        st = sync.detect(cfg, con, clips=a.clips, force=a.force)
        if a.json:
            print(json.dumps(st, indent=1))
        return
    if act == "list":
        gs = sync.groups(con)
        if a.json:
            print(json.dumps(gs, indent=1, default=str))
            return
        if not gs:
            print("no synced groups (run: cutroom sync)")
        for g in gs:
            print(f"group {g['id']} · {g['name']} · {g['duration']:.1f}s · {len(g['members'])} members · "
                  f"loop error {g['loop_err_ms'] or 0:.2f} ms")
            for m in g["members"]:
                print(f"   {m['angle']:7s} {'video' if m['video'] else 'audio'} {m['kind']:8s} offset {m['offset']:+10.4f}s "
                      f"drift {m['drift_ppm']:+7.1f} ppm conf {m['confidence']:.2f}  {m['device_name'] or ''} · {m['rel']}")
        return
    if act == "show":
        g = sync.group(con, args[0])
        print(json.dumps(g, indent=1, default=str))
        return
    if act == "devices":
        for d in sync.devices(con):
            print(f"{d['id']:3d} {d['code']:6s} {d['kind']:8s} {d['name'] or '':28s} {d['make'] or ''} {d['model'] or ''} "
                  f"{('sn ' + d['serial']) if d['serial'] else ''} · {d['clips']} clip(s)")
        return
    if act == "name":
        if len(args) < 1:
            sys.exit('usage: cutroom sync name DEVICE "Cam A wide" [--kind camera|recorder|lav] [--code camA]')
        d = sync.name_device(con, args[0], name=args[1] if len(args) > 1 else None, kind=a.kind, code=a.code,
                             tc_sync=(a.tc_sync == "on") if a.tc_sync else None)
        print(f"{d['code']} = {d['name']} ({d['kind']})")
        return
    if act == "rename":
        sync.rename_group(con, args[0], " ".join(args[1:]))
        return
    if act == "clocks":
        rows = sync.estimate_clocks(cfg, con) if a.force else [dict(r) for r in con.execute("SELECT * FROM device_clock")]
        names = {d["id"]: d for d in sync.devices(con)}
        if a.json:
            print(json.dumps(rows, indent=1, default=str))
            return
        if not rows:
            print("no clock offsets measured yet (they come from matched sync pairs: cutroom sync)")
        for r in sorted(rows, key=lambda r: (r["day"], r["device_id"])):
            d, ref = names.get(r["device_id"], {}), names.get(r["ref_device"], {})
            print(f"{r['day']}  {d.get('code', r['device_id'])!s:6s} {d.get('name') or '':30s} clock {r['offset_s']:+9.2f}s "
                  f"vs {ref.get('code', r['ref_device'])} · {r['n']} pair(s) · spread {r['spread_s']:.2f}s")
        return
    if act == "recordings":
        for ch in sync.logical_recordings(con):
            rels = [con.execute("SELECT rel FROM clips WHERE id=?", (c,)).fetchone()[0] for c in ch]
            print(f"{len(ch)} files: " + " + ".join(rels))
        return
    if act == "map":
        t = sync.map_time(con, args[0], float(args[1]), frm=a.frm, to=a.to)
        print(f"{t:.4f}")
        return
    if act == "switch":
        from . import plan as P
        if len(args) < 3:
            sys.exit("usage: cutroom sync switch PLAN SEC.SEG ANGLE [--audio CODE]")
        path = Path(args[0])
        plan = P.load(path)
        si, gi = (int(x) for x in args[1].split("."))
        seg = plan["sections"][si]["segments"][gi]
        plan["sections"][si]["segments"][gi] = sync.switch_angle(con, seg, args[2], audio=a.audio)
        path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
        s2 = plan["sections"][si]["segments"][gi]
        print(f"section {si} segment {gi}: angle {args[2]} · clip {s2['clip']} {s2['in']}-{s2['out']}")
        return
    sys.exit(f"unknown sync action {act!r}")


def cmd_detect(a):
    from . import analyze
    cfg, con = _cfg_con()
    what = a.what
    if what in ("dedupe", "all"):
        analyze.dedupe(cfg, con)
    if what in ("highlights", "all"):
        analyze.highlights_all(cfg, con)
    if what in ("sessions", "all"):
        analyze.sessions(cfg, con)
        for s in analyze.session_list(con)[:20]:
            print(f"  shoot {s['session']:4d}  {s['day']} {s['start']}-{s['end']}  {s['clips']:4d} clips  "
                  f"{s['hours']:.2f} h" + (f"  place {s['place']} ({s['lat']}, {s['lon']})" if s["place"] else ""))
    if what in ("takes", "all"):
        analyze.takes(cfg, con)
    if what in ("events", "all"):
        analyze.events_all(cfg, con)


def cmd_recap(a):
    from . import recap
    cfg, con = _cfg_con()
    try:
        path, rep, errs = recap.write(cfg, con, a.name, length=a.len, song=a.song, ref=a.ref)
    except (recap.RecapError, KeyError) as e:
        sys.exit(f"recap: {str(e).strip(chr(39))}")
    if a.json:
        print(json.dumps({"plan": str(path), "report": rep, "errors": errs}, indent=1, default=str))
        sys.exit(1 if errs else 0)
    print(f"recap draft -> {path}")
    print(f"  {rep['duration']:.1f}s (target {rep['target']:.0f}s) · song {rep['song']} · climax on the drop at "
          f"{rep['drop']:.1f}s · {rep['flashes']} flash(es), max {rep['flash_max_per_10s']:g} per 10 s · "
          f"{rep['ramps']} ramp(s)")
    for s_ in rep["sections"]:
        print(f"  {s_['name']:10s} {s_['start']:6.1f}-{s_['end']:6.1f}s")
    for b in rep["soundbites"]:
        print(f"  soundbite {b['clip']} {b['t0']:.2f}-{b['t1']:.2f}s ({b['score']:.2f}) \"{b['text'][:80]}\"")
    if rep.get("reference"):
        r = rep["reference"]
        print(f"  from reference {r['id']}: cold open {r['cold_open_s']:.1f}s, {r['flash_per_10s']:g} flashes per 10 s, "
              f"montage ASL {r['montage_asl']:.2f}s, talk {r['talk_pct']:.0f}%")
    for w in rep.get("warnings") or []:
        print(f"  ! {w}")
    for e in errs:
        print("  ✗", e)
    print("next: refine it with the event-recap skill, then `cutroom plan check` and `cutroom render --preview`")
    sys.exit(1 if errs else 0)


def cmd_takes(a):
    from . import analyze, db
    cfg, con = _cfg_con()
    analyze.ensure_scale_schema(con)
    if a.clip:
        cid = db.get_clip(con, a.clip)["id"]
        grps = [r[0] for r in con.execute("SELECT DISTINCT grp FROM takes WHERE clip_id=?", (cid,))]
    else:
        grps = [r[0] for r in con.execute("SELECT DISTINCT grp FROM takes ORDER BY grp")]
    if not grps:
        print("no repeated lines found (run `cutroom detect takes` after transcribing)")
    for g in grps:
        rows = con.execute("SELECT * FROM takes WHERE grp=? ORDER BY rank", (g,)).fetchall()
        print(f"line {g}: {len(rows)} takes")
        for r in rows:
            print(f"  {'BEST' if r['best'] else '    '} #{r['rank']} {r['clip_id']} {r['t0']:7.2f}-{r['t1']:7.2f}s "
                  f"score {r['score']:.2f}  {r['text'][:90]}")


def cmd_estimate(a):
    import shutil as _sh
    import tempfile as _tf
    from .config import Config
    from . import estimate
    cfg = Config.load()
    src = cfg.db_path
    tmpd = None
    if a.copy_db:                                   # never touch a database another process is writing
        tmpd = _tf.mkdtemp(prefix="cutroom-estimate-")
        import sqlite3
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(str(Path(tmpd) / "library.db"))
        s.backup(d)
        s.close()
        src = Path(tmpd) / "library.db"
    con = estimate._ro(src)
    try:
        res = estimate.estimate(cfg, con, tb=a.tb, hours=a.hours, do_bench=a.bench)
    finally:
        con.close()
        if tmpd:
            _sh.rmtree(tmpd, ignore_errors=True)
    if a.json:
        print(json.dumps(res, indent=1, default=str))
    else:
        estimate.print_estimate(res)


def cmd_qa(a):
    from . import qa
    cfg, con = _cfg_con()
    rep = qa.run(cfg, a.video, plan_path=a.plan)
    print(json.dumps(rep["summary"], indent=2))


def cmd_measure(a):
    """Loudness / dynamics of a video (or a window of it), measured by ebur128: what the factory's claim check
    and QA swing_vs_reference use. Roles measure with this; they never estimate a number."""
    from . import dynamics
    cfg, _ = _cfg_con()
    v = a.video if Path(a.video).is_absolute() or Path(a.video).exists() else str(Path(cfg.work) / a.video)
    st = dynamics.window_stats(cfg, v, a.start or 0.0, a.end)
    if a.json:
        print(json.dumps(st))
        return
    w = f"{a.start or 0:.2f}-{a.end:.2f}s" if a.end is not None else "whole file"
    print(f"{Path(v).name} [{w}]  I {st['I']} LUFS  swing {st['swing']} LU (momentary p95-p10)  "
          f"M p10 {st['M_p10']} / p95 {st['M_p95']} / max {st['M_max']} LUFS  TP {st['TP']} dBTP  LRA {st['LRA']} LU")


def cmd_board(a):
    from . import board
    cfg, _ = _cfg_con()
    board.serve(cfg, port=a.port, host=a.host)


def cmd_notes(a):
    from . import board
    cfg, _ = _cfg_con()
    board.print_notes(cfg, since=a.since)


def cmd_studio(a):
    from . import studio
    cfg, _ = _cfg_con()
    studio.serve(cfg, port=a.port, host=a.host, open_browser=a.open)


def cmd_markers(a):
    """Review comments (stored as studio markers). done/needs/open set the status; --reply adds the AI's
    short answer to the comment's thread (what changed, or "Needs you: <question>")."""
    from . import studio
    from .config import Config
    cfg = Config.load()
    act = a.action
    if act in ("done", "needs", "open", "reply"):
        if not a.id:
            sys.exit(f"usage: cutroom comments {act} ID [ID ...] [--reply TEXT]")
        reply = getattr(a, "reply", None)
        if act == "reply" and not reply:
            sys.exit("usage: cutroom comments reply ID --reply TEXT")
        for mid in a.id:
            try:
                if act == "reply":
                    cur = next((m for m in studio.markers_load(cfg) if m.get("id") == mid), None)
                    if cur is None:
                        raise KeyError(f"no comment {mid!r}")
                    m = studio.marker_set_status(cfg, mid, cur.get("status", "open"), reply=reply)
                else:
                    st = {"done": "done", "needs": "needs_you", "open": "open"}[act]
                    if act == "needs" and reply and not reply.lower().startswith("needs you"):
                        reply = "Needs you: " + reply
                    m = studio.marker_set_status(cfg, mid, st, reply=reply)
            except KeyError as e:
                sys.exit(str(e).strip("'\""))
            print(f"{m['status']:9s} {m['id']}  @ {float(m.get('t') or 0):.2f}s  {m.get('text', '')}")
        return
    ms = studio.markers_load(cfg)
    if a.plan:
        ms = [m for m in ms if m.get("plan") == a.plan]
    if not a.all:
        ms = [m for m in ms if m.get("status", "open") in ("open", "working")]
    ms.sort(key=lambda m: (str(m.get("plan") or ""), float(m.get("t") or 0)))
    if a.json:
        print(json.dumps(ms, indent=1, ensure_ascii=False))
        return
    if not ms:
        print("no " + ("" if a.all else "open ") + "markers")
        return
    for m in ms:
        frame = studio.Studio.frame_path_of(cfg, m.get("frame_url"))
        rng = f"-{float(m['t_end']):.2f}s" if m.get("t_end") is not None else ""
        drew = f"  (drew {studio.drawing_words(m['drawing'])})" if m.get("drawing") else ""
        print(f"{m['id']}  {m.get('status', 'open'):9s}  {m.get('plan') or '-':16s} @ {float(m.get('t') or 0):7.2f}s{rng}"
              f"  [{m.get('kind', 'note')}] {m.get('text', '')}{drew}" + (f"\n    frame: {frame}" if frame else ""))


def cmd_factory(a):
    """The factory (docs/FACTORY.md): orders, the runner, verdicts, lessons, the brain."""
    from .config import Config
    from .factory import brain, bus, lessons, runner
    cfg = Config.load()
    act, args = a.action, a.args
    js = lambda o: print(json.dumps(o, indent=1, ensure_ascii=False, default=str))   # noqa: E731

    def need(k, usage):
        if len(args) < k:
            sys.exit(f"usage: cutroom factory {usage}")
    if act == "run":
        runner.run_forever(cfg, link_base=a.link_base)
    elif act == "tick":
        r = runner.FactoryRunner(cfg, link_base=a.link_base)
        if not r.acquire():
            sys.exit("another factory runner holds this project")
        try:
            r._recover_orphans()
            r.tick()
            if r.driver is not None:
                r.driver.join()
            print(json.dumps(runner.light(cfg)) if a.json else runner.light(cfg)["text"])
        finally:
            r.release()
    elif act == "status":
        lt = runner.light(cfg)
        if a.json:
            return js(lt)
        print(f"factory: {lt['light']} · {lt['text']}")
        print(f"  runner: {'alive pid ' + str(lt['runner_pid']) if lt['runner_alive'] else 'not running'}"
              f"{' · gate: ' + lt['gate'] if lt.get('gate') else ''} · queued {lt['queued']}")
        for o in runner.list_view(cfg)[:10]:
            print(f"  {o['id']}  {o['kind']:8s} {o['plan'] or '-':16s} {o['state']:9s} {o['phase'] or '':10s} "
                  f"{('v' + str(o['version'])) if o['version'] else '':5s} {o['why'] or o['line'] or ''}"[:160])
    elif act == "orders":
        rows = runner.list_view(cfg, plan=a.plan, state=a.state)
        if a.json:
            return js(rows)
        for o in rows:
            print(f"{o['id']}  {o['kind']:8s} {o['plan'] or '-':16s} {o['state']:9s} {o['phase'] or '':10s} "
                  f"${o['cost_usd'] or 0:.2f}  {o['why'] or o['line'] or ''}"[:170])
    elif act == "order":
        need(1, "order ID")
        d = runner.order_detail(cfg, args[0])
        if a.json:
            return js(d)
        st = d["status"]
        print(f"{args[0]} {d['order']['kind']} on {d['order']['plan']}: {st['state']} {st.get('phase') or ''} "
              f"{st.get('why') or st.get('text') or ''}")
        for p in d["phases"]:
            print(f"  {p['name']:18s} {p['state']}" + (f" ({p['skipped']})" if p.get("skipped") else ""))
        for q in st.get("questions") or []:
            print(f"  ? {q['id']}: {q['line']}" + (f"  [answered {q['answered']['option']}]" if q.get("answered") else ""))
        if d.get("card"):
            print(f"  card: {bus.order_dir(cfg, args[0]) / 'card.md'}")
    elif act == "send":
        if not a.plan:
            sys.exit("usage: cutroom factory send --plan SLUG [--comments ID,ID | --all-open] [--text T] [--kind revise]")
        res = runner.submit(cfg, a.kind or "revise", a.plan, comment_ids=[x for x in (a.comments or "").split(",") if x],
                            all_open=a.all_open, text=a.text or "", by={"user": "you", "via": "cli"})
        print(f"order {res['order']['id']}" + (f" (folded into {res['folded_into']})" if res.get("folded_into") else ""))
    elif act in ("stop", "retry"):
        need(1, f"{act} ID")
        st = runner.stop(cfg, args[0]) if act == "stop" else runner.retry(cfg, args[0])
        print(f"{args[0]}: {st['state']}")
    elif act == "pause":
        runner.pause(cfg)
        print("factory paused (the running role finishes; nothing new starts). `cutroom factory resume` to go on.")
    elif act == "resume":
        n = runner.resume(cfg)
        print(f"factory resumed{f'; {n} order(s) requeued after the login problem' if n else ''}")
    elif act == "answer":
        need(3, "answer ORDER QUESTION OPTION [--text T]")
        js(runner.answer(cfg, args[0], args[1], args[2], text=a.text or ""))
    elif act == "verdict":
        need(3, "verdict PLAN VERSION APPROVE|REJECT|NOTES [--text T]")
        js(runner.verdict(cfg, args[0], int(args[1]), args[2], text=a.text or ""))
    elif act == "lessons":
        sub = args[0] if args else "list"
        if sub == "inbox":
            for r in lessons.inbox(cfg):
                print(f"{r['id']} [{r['kind']}] {r['text'][:140]}")
        elif sub == "add":
            need(2, "lessons add 'RULE' [--text SYMPTOM]")
            print(lessons.add_lesson(cfg, args[1], symptom=a.text or "")["id"])
        elif sub == "supersede":
            need(2, "lessons supersede ID [NEW_ID]")
            print(lessons.supersede(cfg, args[1], by=args[2] if len(args) > 2 else None)["status"])
        else:
            for x in lessons.active(cfg) if not a.all else lessons.load(cfg)["lessons"]:
                if x["scope"] == "global" and not a.all and not a.json:
                    continue
                print(f"{x['id']} [{x['scope']}] {x['rule'][:150]}" + (f"  (check {x['check']})" if x.get("check") else ""))
    elif act == "brain":
        need(1, "brain SLUG [FILE]")
        if len(args) > 1:
            v = brain.read_file(cfg, args[0], args[1])
            print(v if isinstance(v, str) else json.dumps(v, indent=1, ensure_ascii=False))
        else:
            js(brain.summary(cfg, args[0]))
    else:
        sys.exit(f"unknown factory action {act}")


def _echo(m):
    print(m, flush=True)


def cmd_ref(a):
    from . import reference as R
    from .config import Config
    cfg = Config.load()
    args = list(a.args or [])

    def need(n, usage):
        if len(args) < n:
            sys.exit(f"usage: cutroom ref {usage}")
    try:
        if a.action == "add":
            need(1, "add FILE|URL [--title T] [--no-analyze]")
            m = R.add(cfg, args[0], title=a.title, analyze_now=not a.no_analyze, echo=_echo,
                      make_proxy_now=not a.no_analyze, link=a.link)
            print(f"ref {m['id']}: {m.get('status')}  (cutroom ref show {m['id']})")
            if m.get("status") == "analyzed":
                _ref_show(cfg, m["id"])
        elif a.action == "analyze":
            need(1, "analyze ID [--force]")
            rid = R.resolve_id(cfg, args[0])
            R.analyze(cfg, rid, force=a.force, asr=not a.no_asr, echo=_echo)
            if a.deep:
                from . import vision
                _, brain = vision.deep_analyze(cfg, rid, budget_usd=a.budget, agent=a.agent, echo=_echo,
                                               force=a.force)
                for ln in vision.brain_lines(brain):
                    print(ln)
            _ref_show(cfg, rid)
        elif a.action == "list":
            R.sync_reference_folder(cfg)
            rows = R.list_refs(cfg)
            if a.json:
                print(json.dumps(rows, indent=1, ensure_ascii=False))
                return
            if not rows:
                print("no references yet: cutroom ref add <file|url>")
            for m in rows:
                sm = m.get("summary") or {}
                st = m.get("status")
                if st == "analyzing":
                    st = f"analyzing {m.get('stage') or ''} {100 * float(m.get('progress') or 0):.0f}%"
                print(f"{m['id']}  {st:22s} {float(m.get('duration') or 0):7.1f}s  "
                      + (f"{sm.get('n_shots')} shots, ASL {sm.get('asl')}s  " if sm else "") + str(m.get("title") or ""))
        elif a.action == "show":
            need(1, "show ID")
            _ref_show(cfg, R.resolve_id(cfg, args[0]), as_json=a.json)
        elif a.action == "cutmap":
            need(1, "cutmap ID [--json]")
            rows = R.cutmap(cfg, args[0])
            if a.json:
                print(json.dumps(rows, ensure_ascii=False))
                return
            print(f"{'#':>4} {'start':>8} {'dur':>6} {'fr':>4}  {'kind':16s} {'beat':4s} {'luma':>5}  section / text")
            for r in rows:
                print(f"{r['i']:>4} {r['start']:8.3f} {r['dur']:6.2f} {r['frames']:>4}  {r['kind']:16s} "
                      f"{'yes' if r['beat'] else ('-' if r['beat'] is None else 'no'):4s} {r['luma']:5.0f}  "
                      f"{r['section']}" + (f"  \"{r['text']}\"" if r['text'] else ""))
        elif a.action == "compare":
            need(2, "compare ID RENDER.mp4 [--json]")
            res = R.compare(cfg, R.resolve_id(cfg, args[0]), args[1], echo=lambda m: None)
            if a.json:
                print(json.dumps(res, indent=1, ensure_ascii=False))
                return
            print(f"{res['target']} vs reference {res['ref']['title']} ({res['mode']}): {res['verdict']}")
            for r in res["rows"]:
                if r["ref"] is None and r["yours"] is None:
                    continue
                print(f"  {r['label']:28s} ref {str(r['ref']):>10}   yours {str(r['yours']):>10} {r['unit']}")
            print("  sections (aligned by relative position):")
            for s_ in res["sections"]:
                print(f"    {s_['name']:18s} ref {s_['ref_start']:7.1f}-{s_['ref_end']:7.1f}s ASL {s_['ref_asl']:5.2f}s"
                      f"   yours {s_['yours_start']:6.1f}-{s_['yours_end']:6.1f}s ASL {s_['yours_asl']:5.2f}s")
            print("gaps:" if res["gaps"] else "no gaps worth naming")
            for g in res["gaps"]:
                print(f"  [{g['severity']}] {g['text']}" + (f"  (at {g['t']:.1f}s)" if g.get("t") is not None else ""))
    except KeyError as e:
        sys.exit(str(e).strip("'\""))


def _ref_show(cfg, rid, as_json=False):
    from . import reference as R
    m = R.load_meta(cfg, rid)
    d = R.ref_dir(cfg, rid)
    if as_json:
        print(json.dumps({"meta": m, "analysis": R._jload(d / "analysis.json")}, ensure_ascii=False))
        return
    print(f"{rid}  {m.get('title')}  [{m.get('status')}]" + (f"  error: {m['error']}" if m.get("error") else ""))
    print(f"  file: {d / (m.get('source') or '')}" + (f"   from: {m['url']}" if m.get("url") else ""))
    an = R._jload(d / "analysis.json")
    if not an:
        print(f"  not analyzed yet: cutroom ref analyze {rid}")
        return
    for ln in an.get("summary") or R.summary_lines(an):
        print("  " + ln)
    for sh in an.get("sheets") or []:
        print(f"  sheet: {d / sh}")
    print(f"  thumbs: {d / 'thumbs'}   analysis: {d / 'analysis.json'}   measured in {an.get('seconds')}s")


def cmd_overnight(a):
    from .config import Config
    from . import overnight
    cfg = Config.load()
    if a.status:
        overnight.print_status(cfg)
        return
    if a.stop:
        print("stop requested; the current item finishes first" if overnight.stop(cfg) else "overnight is not running")
        return
    skip = []
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        bad = want - set(overnight.STAGES)
        if bad:
            sys.exit(f"unknown stage(s) {sorted(bad)}; stages: {', '.join(overnight.STAGES)}")
        skip = [s for s in overnight.STAGES if s not in want]
    sys.exit(overnight.run(cfg, until=a.until, tag=not a.no_tag, tag_limit=a.tag_limit, model=a.model,
                           dry_run=a.dry_run, skip_stages=skip, echo=not a.quiet))


def cmd_governor(a):
    from .config import Config
    from . import governor
    st = governor.for_cfg(Config.load()).status()
    if a.json:
        print(json.dumps(st, indent=1))
        return
    print(f"load {st['load']} · free RAM {st['ram_free_gb']} / {st['ram_total_gb']} GB · free disk {st['disk_free_gb']} GB"
          f" · heavy allowed {st['allowed']['heavy']}/{st['limits']['heavy_slots']}")
    for k in ("throttled", "blocked"):
        if st[k]:
            print(f"  {k}: {'; '.join(st[k])}")
    for sl in st["slots"]:
        print(f"  {sl['kind']}.{sl['index']}: " + (f"pid {sl.get('pid')} {sl.get('label') or ''} ({sl.get('age_s')}s)"
                                                    if sl["busy"] else "free"))
    for w in st["waiting"]:
        print(f"  waiting: pid {w['pid']} {w['kind']} {w.get('label', '')} {w['waited_s']}s ({w.get('reason', '')})")
    print(f"  limits: {st['limits']}")


FACTORY_REFUSED = ("render", "transcribe", "export", "cutdown", "overnight", "index", "studio", "factory", "export-nle")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cutroom", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("init"); p.add_argument("work"); p.add_argument("--library", action="append", required=True)
    p.add_argument("--music", action="append"); p.set_defaults(fn=cmd_init)
    sp.add_parser("doctor").set_defaults(fn=cmd_doctor)
    p = sp.add_parser("index"); p.add_argument("--limit", type=int); p.add_argument("--skip-proxies", action="store_true")
    p.add_argument("--skip-analysis", action="store_true")
    p.add_argument("--files", nargs="+", help="index just these video files (they must be inside a library folder)")
    p.add_argument("--files-from", help="a text file with one video path per line (like --files)")
    p.set_defaults(fn=cmd_index)
    p = sp.add_parser("transcribe"); p.add_argument("--limit", type=int); p.add_argument("--model")
    p.add_argument("--clip"); p.set_defaults(fn=cmd_transcribe)
    sp.add_parser("status").set_defaults(fn=cmd_status)
    p = sp.add_parser("tags"); p.add_argument("action", choices=["todo", "show", "set", "fields"])
    p.add_argument("ref", nargs="?"); p.add_argument("json", nargs="?"); p.add_argument("-n", type=int, default=20)
    p.add_argument("--replace", action="store_true"); p.set_defaults(fn=cmd_tags)
    p = sp.add_parser("search"); p.add_argument("text", nargs="?", default="")
    for k in ("setting", "time", "people", "shot", "camera"):
        p.add_argument(f"--{k}")
    p.add_argument("--max-shake", type=float); p.add_argument("--min-quality", type=float)
    p.add_argument("--speech", action="store_true"); p.add_argument("--no-speech", action="store_true")
    p.add_argument("--creator", action="store_true", help="main subject visible")
    p.add_argument("--min-dur", type=float); p.add_argument("--limit", type=int, default=30)
    p.add_argument("--highlights", action="store_true", help="clips with a highlight moment, strongest first")
    p.add_argument("--dedupe", action="store_true", help="one clip per duplicate / near-duplicate group")
    p.add_argument("--session", type=int, help="only this shoot (`cutroom detect sessions` lists them)")
    p.add_argument("--near", help="clips that look like this one (perceptual hash), closest first")
    p.add_argument("--kind", choices=["crowd", "stage", "wide"], help="event footage: clips with this shot kind "
                                                                      "(`cutroom detect events`), best window first")
    p.add_argument("--soundbites", action="store_true", help="quotable spoken lines, best first (TEXT filters them)")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_search)
    p = sp.add_parser("detect", help="duplicates, highlights, shoots/places, best takes, event shot kinds")
    p.add_argument("what", nargs="?", default="all",
                   choices=["dedupe", "highlights", "sessions", "takes", "events", "all"])
    p.set_defaults(fn=cmd_detect)
    p = sp.add_parser("recap", help="event recap: write a draft plan (cold open, build, climax, end card)")
    p.add_argument("name"); p.add_argument("--len", type=float, default=120.0)
    p.add_argument("--song", help="song id, path or title fragment (default: the fastest long-enough song)")
    p.add_argument("--ref", help="a measured reference: copy its pacing, flash rate and section structure")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_recap)
    p = sp.add_parser("takes"); p.add_argument("--clip"); p.set_defaults(fn=cmd_takes)
    p = sp.add_parser("estimate", help="time and disk for indexing this library (or --tb 4)")
    p.add_argument("--tb", type=float); p.add_argument("--hours", type=float)
    p.add_argument("--bench", action="store_true", help="30 s benchmark on one clip when nothing was measured yet")
    p.add_argument("--copy-db", action="store_true", help="read a snapshot of library.db (safe while overnight runs)")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_estimate)
    p = sp.add_parser("clip"); p.add_argument("ref"); p.add_argument("--frames", type=int, default=0); p.set_defaults(fn=cmd_clip)
    p = sp.add_parser("say"); p.add_argument("ref"); p.add_argument("--from", dest="start", type=float)
    p.add_argument("--to", dest="end", type=float); p.set_defaults(fn=cmd_say)
    p = sp.add_parser("frames"); p.add_argument("ref"); p.add_argument("t", nargs="+")
    p.add_argument("--width", type=int, default=960); p.set_defaults(fn=cmd_frames)
    p = sp.add_parser("title", help="typed-title placement: where pos \"auto\" puts it, which held shot suits it")
    p.add_argument("action", choices=["place"]); p.add_argument("plan")
    p.add_argument("--choose-shot", action="store_true"); p.add_argument("--debug-dir"); p.set_defaults(fn=cmd_title)
    p = sp.add_parser("songs"); p.add_argument("action", choices=["index", "list"]); p.set_defaults(fn=cmd_songs)
    p = sp.add_parser("beats"); p.add_argument("song"); p.set_defaults(fn=cmd_beats)
    p = sp.add_parser("matchcuts"); p.add_argument("--limit", type=int); p.add_argument("--min-score", type=float, default=0.75)
    p.set_defaults(fn=cmd_matchcuts)
    p = sp.add_parser("plan"); p.add_argument("action", choices=["check"]); p.add_argument("plan"); p.set_defaults(fn=cmd_plan)
    p = sp.add_parser("render"); p.add_argument("plan"); p.add_argument("--preview", action="store_true")
    p.add_argument("--out"); p.add_argument("--only"); p.set_defaults(fn=cmd_render)
    p = sp.add_parser("qa"); p.add_argument("video"); p.add_argument("--plan"); p.set_defaults(fn=cmd_qa)
    p = sp.add_parser("measure", help="loudness of a video or a window: I, swing (p95-p10 momentary), TP, LRA")
    p.add_argument("video"); p.add_argument("--from", dest="start", type=float); p.add_argument("--to", dest="end", type=float)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_measure)
    p = sp.add_parser("export", help="platform files, reframes, captions, chapters, thumbnails, cutdowns")
    p.add_argument("target", help="a render .mp4 (with its .json sidecar) or a plan .json")
    p.add_argument("--preset", action="append", help="youtube, youtube_4k, reels, tiktok, shorts, square, feed, all "
                                                     "(repeat or comma-separate)")
    p.add_argument("--cutdown", help="also build + render + export cutdowns of these lengths, e.g. 15,30,60")
    p.add_argument("--thumbs", action="store_true", help="thumbnail candidates + contact sheet")
    p.add_argument("--no-captions", action="store_true"); p.add_argument("--keep-burned", action="store_true",
                                                                         help="reframe the captioned render as is")
    p.add_argument("--trim", action="store_true", help="cut to the preset's max duration instead of skipping it")
    p.add_argument("--fps", type=float, help="force a frame rate (default: keep the render's)")
    p.add_argument("--max-dur", type=float, help="override the presets' maximum duration")
    p.add_argument("--preview", action="store_true", help="render missing plans/cutdowns as previews")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_export)
    p = sp.add_parser("cutdown"); p.add_argument("plan"); p.add_argument("--len", default="30")
    p.set_defaults(fn=cmd_cutdown)
    p = sp.add_parser("export-nle", help="Premiere XML / FCPXML / OTIO / EDL hand-off (docs/NLE_AND_MULTICAM.md)")
    p.add_argument("plan", nargs="?"); p.add_argument("--format", action="append",
                                                     help="premiere (default), fcpxml (= resolve), otio, edl, all")
    p.add_argument("--out", help="folder (default renders/nle/<slug>_vN)")
    p.add_argument("--render", help="a cutroom render of this plan to cut the V4 effect segments from")
    p.add_argument("--clean-render", action="store_true", help="cut effects from a caption-free render (renders one)")
    p.add_argument("--preview", action="store_true", help="with --clean-render: a 720p preview is enough")
    p.add_argument("--no-graphics", action="store_true", help="skip the ProRes 4444 overlay graphics")
    p.add_argument("--zip", action="store_true", help="also pack the folder as a zip")
    p.add_argument("--verify", metavar="DIR", help="re-read an export and compare every clip to the plan")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_export_nle)
    p = sp.add_parser("sync", help="multicam + dual-system audio sync (docs/NLE_AND_MULTICAM.md)")
    p.add_argument("action", nargs="?", default="detect",
                   choices=["detect", "list", "show", "devices", "name", "rename", "map", "switch", "clocks",
                            "recordings"])
    p.add_argument("args", nargs="*"); p.add_argument("--clips", nargs="+", help="detect: align just these clips")
    p.add_argument("--force", action="store_true", help="detect: re-measure cached pairs")
    p.add_argument("--kind", choices=["camera", "recorder", "lav"]); p.add_argument("--code")
    p.add_argument("--tc-sync", choices=["on", "off"], help="name: jam-synced timecode decides when audio can't")
    p.add_argument("--from", dest="frm"); p.add_argument("--to"); p.add_argument("--audio")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_sync)
    p = sp.add_parser("cameras", help="camera identity, log profiles, matching to the hero camera (docs/NLE_AND_MULTICAM.md)")
    p.add_argument("action", nargs="?", default="list", choices=["list", "identify", "clips", "show", "set", "match", "luts"])
    p.add_argument("args", nargs="*"); p.add_argument("--camera", help="clips: only this camera key or family")
    p.add_argument("--profile", help="set: " + ", ".join(__import__("cutroom.colorsci", fromlist=["x"]).ALL_PROFILES))
    p.add_argument("--family", choices=["arri", "bmd", "fuji", "sony", "other"]); p.add_argument("--model")
    p.add_argument("--all", action="store_true", help="set: every clip of this clip's camera")
    p.add_argument("--matrix", action="store_true", help="match: also fit a full 3x3 (Monge-Kantorovich)")
    p.add_argument("--force", action="store_true", help="identify: redo every clip")
    p.add_argument("--out", help="luts: output folder"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_cameras)
    p = sp.add_parser("braw", help="Blackmagic RAW / ARRIRAW clips and their decodable stand-ins")
    p.add_argument("--limit", type=int, default=50); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_braw)
    p = sp.add_parser("board"); p.add_argument("--port", type=int, default=8600)
    p.add_argument("--host", default="127.0.0.1"); p.set_defaults(fn=cmd_board)
    p = sp.add_parser("notes"); p.add_argument("--since", type=int); p.set_defaults(fn=cmd_notes)
    p = sp.add_parser("overnight", help="unattended library understanding (see docs/OVERNIGHT.md)")
    p.add_argument("--until", help="local HH:MM to stop gracefully"); p.add_argument("--no-tag", action="store_true")
    p.add_argument("--tag-limit", type=int); p.add_argument("--model", help="ASR model: small (faster) or medium")
    p.add_argument("--dry-run", action="store_true", help="everything except the Claude calls")
    p.add_argument("--status", action="store_true"); p.add_argument("--stop", action="store_true")
    p.add_argument("--only", help="comma-separated stages to run, e.g. tag or transcribe,tag")
    p.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS); p.set_defaults(fn=cmd_overnight)
    p = sp.add_parser("governor"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_governor)
    p = sp.add_parser("ref", help="reference videos: add, measure, cut map, compare (docs/STUDIO.md)")
    p.add_argument("action", choices=["add", "list", "show", "analyze", "cutmap", "compare"])
    p.add_argument("args", nargs="*"); p.add_argument("--title"); p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--link", action="store_true", help="add: symlink the file instead of copying it")
    p.add_argument("--json", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--no-asr", action="store_true", help="skip speech-to-text")
    p.add_argument("--deep", action="store_true", help="analyze: + the AI brain pass (vision.py): reviews every cut and "
                                                       "device, labels shots, reads text, explains why it works")
    p.add_argument("--budget", type=float, default=None, help="--deep: cost cap in USD (default vision_ref_budget_usd, $1)")
    p.add_argument("--agent", default=None, help="--deep: claude | codex (default: the configured agent)")
    p.set_defaults(fn=cmd_ref)
    p = sp.add_parser("studio"); p.add_argument("--port", type=int, default=8600)
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_studio)
    p = sp.add_parser("factory", help="the factory: comment -> team -> next version (docs/FACTORY.md)")
    p.add_argument("action", choices=["run", "tick", "status", "orders", "order", "send", "stop", "retry", "pause",
                                      "resume", "answer", "verdict", "lessons", "brain"])
    p.add_argument("args", nargs="*"); p.add_argument("--plan"); p.add_argument("--state")
    p.add_argument("--comments"); p.add_argument("--all-open", action="store_true"); p.add_argument("--text")
    p.add_argument("--kind", choices=["revise", "reject", "request", "variants"]); p.add_argument("--all", action="store_true")
    p.add_argument("--link-base", help="studio URL for the review-link check, e.g. http://127.0.0.1:8600")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_factory)
    for name in ("comments", "markers"):            # one command, two names (markers = the old name)
        p = sp.add_parser(name, help="studio review comments pinned to timeline times")
        p.add_argument("action", nargs="?", default="list", choices=["list", "done", "needs", "reply", "open"])
        p.add_argument("id", nargs="*"); p.add_argument("--plan"); p.add_argument("--all", action="store_true")
        p.add_argument("--reply", help="short answer added to the comment's thread")
        p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_markers)

    a = ap.parse_args(argv)
    role = os.environ.get("CUTROOM_FACTORY_ROLE")
    if role and a.cmd in FACTORY_REFUSED:
        # heavy work stays in the factory runner, where the governor sees it (docs/FACTORY.md)
        print(f"cutroom {a.cmd}: refused inside the factory ({role} role). The factory runner renders, transcribes "
              "and exports; your job ends at the plan.", file=sys.stderr)
        sys.exit(2)
    a.fn(a)


if __name__ == "__main__":
    main()
