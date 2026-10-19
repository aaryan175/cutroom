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
  cutroom detect [dedupe|highlights|sessions|takes|all]              the cheap library passes (overnight runs them)
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
  cutroom board [--port 8600]                                        review page for renders (phone friendly)
  cutroom notes [--since VERSION]                                    review notes captured on the board
  cutroom studio [--port 8600] [--host 127.0.0.1] [--open]           the browser editor (docs/STUDIO.md)
  cutroom comments [list|done|needs|reply|open ID] [--reply TEXT]    studio review comments (alias: markers)
         [--plan SLUG] [--all] [--json]                              done --reply "what changed" / needs --reply "Needs you: ..."
  cutroom ref add FILE|URL [--title T] [--no-analyze] [--link]        add a reference video and measure it
  cutroom ref list | ref show ID | ref analyze ID [--force]           references: status, summary + sheets
  cutroom ref cutmap ID [--json]                                     one row per shot, to rebuild one-to-one
  cutroom ref compare ID RENDER.mp4 [--json]                         your render vs the reference, gaps in words
"""
import argparse
import json
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


def cmd_doctor(a):
    from .config import Config
    from .media import doctor
    try:
        cfg = Config.load()
    except SystemExit:
        cfg = Config()
    bad = 0
    for name, ok, detail in doctor(cfg) + _agent_checks(cfg):
        print(f"  {'ok ' if ok else 'MISSING'}  {name:34s} {detail}")
        bad += (not ok) and "optional" not in detail
    sys.exit(1 if bad else 0)


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
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_search)
    p = sp.add_parser("detect", help="duplicates, highlights, shoots/places, best takes")
    p.add_argument("what", nargs="?", default="all", choices=["dedupe", "highlights", "sessions", "takes", "all"])
    p.set_defaults(fn=cmd_detect)
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
    p = sp.add_parser("songs"); p.add_argument("action", choices=["index", "list"]); p.set_defaults(fn=cmd_songs)
    p = sp.add_parser("beats"); p.add_argument("song"); p.set_defaults(fn=cmd_beats)
    p = sp.add_parser("matchcuts"); p.add_argument("--limit", type=int); p.add_argument("--min-score", type=float, default=0.75)
    p.set_defaults(fn=cmd_matchcuts)
    p = sp.add_parser("plan"); p.add_argument("action", choices=["check"]); p.add_argument("plan"); p.set_defaults(fn=cmd_plan)
    p = sp.add_parser("render"); p.add_argument("plan"); p.add_argument("--preview", action="store_true")
    p.add_argument("--out"); p.add_argument("--only"); p.set_defaults(fn=cmd_render)
    p = sp.add_parser("qa"); p.add_argument("video"); p.add_argument("--plan"); p.set_defaults(fn=cmd_qa)
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
    p.add_argument("--no-asr", action="store_true", help="skip speech-to-text"); p.set_defaults(fn=cmd_ref)
    p = sp.add_parser("studio"); p.add_argument("--port", type=int, default=8600)
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_studio)
    for name in ("comments", "markers"):            # one command, two names (markers = the old name)
        p = sp.add_parser(name, help="studio review comments pinned to timeline times")
        p.add_argument("action", nargs="?", default="list", choices=["list", "done", "needs", "reply", "open"])
        p.add_argument("id", nargs="*"); p.add_argument("--plan"); p.add_argument("--all", action="store_true")
        p.add_argument("--reply", help="short answer added to the comment's thread")
        p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_markers)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
