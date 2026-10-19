# Architecture: for the AI that maintains and extends cutroom

This repo is meant to be handed to an AI coding agent (Claude Code or Codex) that both **operates** it
(edits videos for the creator) and **extends** it (adds features the creator asks for). This page is
the map for the second job. The operating manual is `CLAUDE.md`.

## The shape in one picture
```
footage (read-only)
   │  ingest.py   scan → probe → proxies → contact sheets          ─┐
   │  analyze.py  shots + shake/exposure/sharpness/faces windows    │  all heavy work goes through
   │  asr.py      word-timestamp transcripts (+ tighten, cues)      │  governor.py (slots, memory caps,
   │  music.py    beats/drops, music bed with ducking, sfx          │  disk floor) and media.py (ffmpeg)
   │  matchcuts.py match-cut candidates                             │
   │  overnight.py runs all of the above + AI tagging, supervised  ─┘
   ▼
library.db (db.py schema) ── tags.py search/describe ── agent.py (Claude Code / Codex bridge)
   │
plans/*.json (plan.py schema = THE contract between the AI, the UI and the renderer)
   │
render.py  expand plan → atomic pieces on a frame grid → cached per-piece encodes → mix → loudnorm
   │   captions.py (Pillow caption PNGs) · overlays.py (HTML/Chrome or Pillow cards) · voice.py
   ▼
renders/<slug>_vN.mp4 + .json sidecar ── qa.py (machine checks + contact sheets)
   │
studio.py (HTTP API + SSE + AI chat bridge) ── studio_web/ (the editor UI, plain ES modules)
board.py (simple phone review page)
```

## Module guide
| Module | Owns | Extend it when |
|---|---|---|
| `config.py` | `cutroom.json` fields + defaults | a new setting is needed (add a field with a default; old configs keep loading) |
| `db.py` | SQLite schema, `TAG_FIELDS` | new per-clip data (add a table or column with `CREATE ... IF NOT EXISTS` / guarded `ALTER`) |
| `media.py` | every ffmpeg/ffprobe call, hardware encoder choice | never call ffmpeg directly elsewhere; use `media.ffmpeg()` so the governor and logs apply |
| `governor.py` | resource safety (slots, memory caps, disk floor, throttling) | new heavy work: wrap it in `gov.slot("heavy")` or run it via `gov.run()` |
| `ingest.py` / `analyze.py` | indexing stages | new per-clip analysis (add a stage, make it resumable via a status/column) |
| `asr.py` | transcripts, `tighten`, caption `cues`, director-cue/repeat finders | caption splitting, new speech heuristics |
| `music.py` | beats, drops, beat-snapping, music bed, built-in SFX | new music behaviour (e.g. stems, crossfades) |
| `plan.py` | plan schema, defaults, validation | **any new edit feature starts here**: add the field or segment type, default and validation |
| `render.py` | plan → video; `resolve_timeline` for the UI | implementing a new segment type, effect or transition |
| `captions.py` / `overlays.py` | caption and card rendering | new caption styles or graphic cards (keep them minimal; see EDITING_RULES) |
| `qa.py` | automated checks on renders | every new failure the creator catches should become a check here |
| `studio.py` | Studio HTTP API, plan ops, jobs, SSE, chat bridge | new UI actions (add an op or route, document it in `docs/STUDIO.md`) |
| `agent.py` | Claude Code / Codex CLI bridge | a new AI agent CLI |
| `studio_web/` | the editor UI (no build step) | UI features; keep the plain ES modules and no CDNs |

## How to add a feature (the recipe)
Example request: "add a zoom-punch transition".
1. **Plan schema** (`plan.py`): a new optional field (`"transition": {"kind": "punch", "dur": 0.2}`) with
   a default and validation. Document it in `docs/PLAN_FORMAT.md`.
2. **Renderer** (`render.py`): implement it inside the per-piece filter graph. Every input that changes
   the output must be in the piece's cache key; it's automatic if it's in the piece dict.
3. **Timeline** (`render.resolve_timeline`): expose it so the UI shows it.
4. **UI** (`studio_web/`): show and edit it (Inspector field, timeline badge). If needed, add a plan
   op in `studio.py`.
5. **QA** (`qa.py`): if it can go wrong visibly, add a check.
6. **Rules and skills:** if it changes how edits should be made, update `docs/EDITING_RULES.md` or the
   relevant skill.
7. **Test** (`tests/`): synthesise footage with ffmpeg lavfi like the existing tests (no real footage
   in tests). Run the full suite: `pytest -q`.
8. Render a real preview on the creator's footage and **look at it** before saying it's done.

## Invariants (don't break these)
- **The footage is read-only.** Nothing ever writes, moves or deletes inside `library_roots`.
- **The plan JSON is the single source of truth.** The UI, the AI and the renderer all go through it.
  Hand edits in the Studio are plan ops, so never keep edit state only in the UI.
- **Renders are frame-exact.** Every piece's duration is a whole number of frames, and its audio is
  exactly as long as its video. Both drift bugs happened before this rule.
- **Heavy work goes through the governor.** At most one transcription runs at a time, anywhere. Memory is
  measured as physical footprint. A process that exceeds its cap is stopped, not trusted.
- **Caches are keyed by content.** Never add hand-bumped version salts. Hash the inputs instead.
- **No bundled copyrighted assets** (fonts, LUTs, music, reference footage). The creator supplies them.
- **Taste rules live in `docs/EDITING_RULES.md`**, not in code defaults that silently override what the
  creator said.

## Running things while developing
```bash
source .venv/bin/activate
pytest -q                                   # full suite, about 2–4 min, synthetic footage only
cutroom studio --port 8600                  # UI against the current project
python cutroom/studio_web/dev/mock_server.py --port 8611 --media <dir>   # UI against fake data
```
