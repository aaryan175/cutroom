"""cutroom command line. Every command is safe to re-run; work is resumable and cached.

  cutroom init WORK --library DIR [--library DIR2] [--music DIR]
  cutroom doctor
  cutroom index [--limit N] [--skip-proxies] [--skip-analysis]      scan + probe + proxies + sheets + shots/metrics
  cutroom transcribe [--limit N] [--model medium] [--clip REF]      word-timestamp ASR for clips with speech
  cutroom status                                                     library counts + what is left to do
  cutroom tags todo [-n 20]                                          untagged clips with their contact sheets
  cutroom tags show REF | cutroom tags set REF JSON [--replace]
  cutroom search [TEXT] [--setting X] [--time day] [--people creator] [--max-shake 4] [--min-quality 3]
                 [--speech] [--no-speech] [--limit 30] [--json]
  cutroom clip REF [--frames 6]                                      everything known about one clip (+ frame grabs)
  cutroom say REF [--from S --to S]                                  transcript with timestamps
  cutroom frames REF T [T ...]                                       pull full-res frames to look at
  cutroom songs index | cutroom songs list | cutroom beats SONG
  cutroom matchcuts [--limit N] [--min-score 0.75]                   mine match-cut candidates
  cutroom plan check PLAN.json                                       validate against the library
  cutroom render PLAN.json [--preview] [--out FILE] [--only SECTION]
  cutroom qa VIDEO [--plan PLAN.json]                                machine QA + contact sheet
  cutroom board [--port 8600]                                        review page for renders (phone friendly)
  cutroom notes [--since VERSION]                                    review notes captured on the board
  cutroom studio [--port 8600] [--host 127.0.0.1] [--open]           the browser editor (docs/STUDIO.md)
  cutroom markers [list|done ID] [--plan SLUG] [--all] [--json]      studio markers (notes pinned to timeline times)
"""
import argparse
import json
import sys


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
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    if not a.skip_proxies:
        ingest.make_proxies(cfg, con, limit=a.limit)
        ingest.make_sheets(cfg, con, limit=a.limit)
    if not a.skip_analysis:
        analyze.analyze_all(cfg, con, limit=a.limit)
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
    from . import studio
    from .config import Config
    cfg = Config.load()
    if a.action == "done":
        if not a.id:
            sys.exit("usage: cutroom markers done ID [ID ...]")
        for mid in a.id:
            try:
                m = studio.marker_set_status(cfg, mid, "done")
            except KeyError as e:
                sys.exit(str(e))
            print(f"done  {m['id']}  @ {float(m.get('t') or 0):.2f}s  {m.get('text', '')}")
        return
    ms = studio.markers_load(cfg)
    if a.plan:
        ms = [m for m in ms if m.get("plan") == a.plan]
    if not a.all:
        ms = [m for m in ms if m.get("status", "open") == "open"]
    ms.sort(key=lambda m: (str(m.get("plan") or ""), float(m.get("t") or 0)))
    if a.json:
        print(json.dumps(ms, indent=1, ensure_ascii=False))
        return
    if not ms:
        print("no " + ("" if a.all else "open ") + "markers")
        return
    for m in ms:
        frame = studio.Studio.frame_path_of(cfg, m.get("frame_url"))
        print(f"{m['id']}  {m.get('status', 'open'):4s}  {m.get('plan') or '-':16s} @ {float(m.get('t') or 0):7.2f}s"
              f"  [{m.get('kind', 'note')}] {m.get('text', '')}" + (f"\n    frame: {frame}" if frame else ""))


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
    p.add_argument("--skip-analysis", action="store_true"); p.set_defaults(fn=cmd_index)
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
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_search)
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
    p = sp.add_parser("studio"); p.add_argument("--port", type=int, default=8600)
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_studio)
    p = sp.add_parser("markers"); p.add_argument("action", nargs="?", default="list", choices=["list", "done"])
    p.add_argument("id", nargs="*"); p.add_argument("--plan"); p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_markers)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
