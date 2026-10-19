# Cutroom Studio: the editor UI

`cutroom studio` opens a Premiere-style editor in the browser at http://127.0.0.1:8600, or on a LAN or
VPN address with `--host`. The AI (Claude Code or Codex) is a panel inside it. The AI does the heavy editing, and you steer
it, point at things and make your own hand edits on the same timeline.

## Layout
```
┌──────────────┬──────────────────────────────┬──────────────────────────────┬──────────────┐
│ PROJECT      │ SOURCE monitor               │ PROGRAM monitor              │ CLAUDE       │
│ bins/search  │ clip proxy, I/O marks,       │ the edit (draft playback     │ chat panel   │
│ thumbnails   │ transcript under it, ⇥ insert│ from proxies, or the render),│ (streams,    │
│ tags/filters │                              │ REFERENCE tab side-by-side   │ tool steps,  │
│ songs        │                              │                              │ attachments) │
├──────────────┴──────────────────────────────┴──────────────────────────────┤              │
│ TIMELINE  V3 graphics/captions · V2 cutaways · V1 main · A1 dialogue · A2 music          │
│ ruler, playhead, markers, section bands, beat ticks on A2, zoom, snapping                │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ INSPECTOR (selected segment fields) · MARKERS/NOTES · QA issues · JOBS (render/overnight) │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Core ideas
- **The plan is the single source of truth.** Hand edits on the timeline and Claude's edits both
  change the same plan JSON. The studio watches `plans/` and reloads when Claude saves.
- **Working draft plus versions.** The open plan is `plans/<slug>.draft.json`. Every hand edit
  auto-saves it and pushes an undo snapshot to `plans/.history/<slug>/`. **Commit version** copies the
  draft to `<slug>_v<N+1>.json`. Render always renders a committed version.
- **Pointing context.** Every chat message carries the current pointing state: playhead time and the
  segment under it, selected segments, source clip and its In/Out, markers, and attached frame grabs.
  The state also lives in `studio/context.json`, so "this shot", "here" and "this frame" are
  unambiguous.
- **Markers.** Press `M` to drop one: time, note text and an auto-grabbed frame JPEG. Markers can be
  sent to Claude one at a time or as a batch ("Fix all open markers").
- **Freeze frame.** Press `F` at the playhead. This inserts a `still` segment of the frame under the
  playhead and splits the segment. The default is 1.0 s, editable in the Inspector.
- **Reference.** Load any video into `renders/reference/`. The Reference tab plays it side by side,
  optionally synced with the program. Press `R` to mark a reference frame, which is attached to chat as
  "match this".
- **Draft playback.** The program monitor plays the timeline straight from proxies by switching video
  sources at cut times. This is approximate (no grade, captions or overlays drawn as HTML on top), so
  you see edits instantly without rendering. **Play render** switches to the real rendered file.

## Keyboard (Premiere-like)
`Space` play/pause · `J K L` shuttle · `←/→` one frame · `Shift+←/→` 1 s · `I`/`O` set In/Out (source)
· `,` insert source In/Out at the playhead · `.` overwrite · `C` razor (split at playhead) · `Del`
delete · `Shift+Del` ripple delete · `F` freeze frame · `M` marker · `R` reference mark · `Cmd/Ctrl+Z` /
`Shift+Cmd/Ctrl+Z` undo/redo · `Cmd/Ctrl+S` commit version · `/` focus chat · `=`/`-` timeline zoom.

## HTTP API (backend ↔ frontend contract)
All JSON unless noted. Media routes support byte ranges (206).

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the app (static files from `cutroom/studio_web/`) |
| GET | `/api/project` | config summary, status counts (same as `cutroom status`), list of plans (draft + versions), renders, songs |
| GET | `/api/clips?q=&setting=&time=&people=&max_shake=&min_quality=&speech=&creator=&offset=0&limit=200` | library search; rows: id, rel, duration, width, height, fps, quality, shake, tags summary, has transcript |
| GET | `/api/clip/<id>` | full detail: probe fields, tags, metrics windows, shots, transcript words `[[s,e,w],...]`, sheet URL |
| PUT | `/api/clip/<id>/tags` | merge tags |
| GET | `/media/proxy/<id>` | proxy mp4 (range) |
| GET | `/media/sheet/<id>` | contact sheet jpg |
| GET | `/media/thumb/<id>?t=` | 320-px jpg at t, cached in `frames/thumbs/` (from proxy) |
| GET | `/media/frame/<id>?t=&w=1280` | full-res frame jpg from the original |
| GET | `/media/render/<file>` | rendered video/sidecar/qa files (range) |
| GET | `/media/ref/<file>` | reference videos (range) |
| GET | `/media/song/<id>` | song audio (range) |
| GET | `/api/plan/<slug>` | `{draft, version_files, timeline}`. `timeline` is the resolved pieces (render.resolve_timeline, no encoding): `[{id, sec, seg, kind, track, start, end, clip, in, out, label, captions, overlays}]` + `sections` bands + `beats` per music section |
| PUT | `/api/plan/<slug>` | replace the draft (validated; returns errors without saving if invalid unless `force`) |
| POST | `/api/plan/<slug>/op` | hand-edit ops, see below; returns the new `{draft, timeline}` |
| POST | `/api/plan/<slug>/undo` · `/redo` | |
| POST | `/api/plan/<slug>/commit` | draft → `<slug>_v<N+1>.json`, returns name |
| POST | `/api/plan/new` | `{title}` → empty draft |
| POST | `/api/render` | `{plan_file, preview}` → job id (queued behind the governor) |
| POST | `/api/qa` | `{video}` → job id |
| GET | `/api/jobs` | list jobs (render/qa/overnight/chat) with progress, log tail |
| GET | `/api/events` | **SSE**: `plan_changed`, `job`, `governor`, `render_done`, `notes` |
| GET/POST/PATCH/DELETE | `/api/markers[?plan=]` | markers `{id, plan, t, text, frame_url, status: open/done, kind: note/ref}` in `studio/markers.json` |
| POST | `/api/grab` | `{source: program/clip/ref, clip?, t, file?}` → saved jpg URL in `studio/grabs/` (for chat attachments) |
| POST | `/api/context` | frontend posts pointing state (debounced). Saved to `studio/context.json` |
| POST | `/api/chat` | `{message, attachments:[grab urls], context}` → **SSE stream** of `{type: session/text/tool/tool_result/status/warning/done/error, ...}` from the agent bridge (Claude Code or Codex) |
| GET | `/api/agent` | `{name, label, logged_in, setting, available:[...], agents:{claude:{path, installed, logged_in}, codex:{...}}}` |
| POST | `/api/agent` | `{name: auto|claude|codex}` → switch the chat/tagging agent; persisted to `cutroom.json` (`CUTROOM_AGENT` in the studio's environment overrides it) |
| POST | `/api/chat/stop` · GET `/api/chat/history` · POST `/api/chat/new` | |
| GET | `/api/governor` | live resource state + limits (see `cutroom/governor.py`) |
| POST | `/api/overnight` | `{action: start/stop/status}` |

### Plan ops (`/op`)
`{op: "move", seg: [si, gi], to: [si, gi]}` · `{op: "trim", seg, edge: "in"/"out", delta}` (source
seconds) · `{op: "split", t}` (timeline seconds) · `{op: "delete", seg, ripple: true}` · `{op: "insert",
at_t, segment}` · `{op: "overwrite", at_t, segment}` · `{op: "freeze", t, dur: 1.0}` · `{op: "set", seg,
fields}` · `{op: "set_style", fields}` · `{op: "add_section", at, name, music}` · `{op: "set_music",
sec, music}` · `{op: "add_cutaway", seg, cutaway}` · `{op: "add_overlay", seg, overlay}`. Times inside
talk segments are mapped back from timeline to source through the resolved pieces (tightened gaps
included).

### Backend details (as implemented in `cutroom/studio.py`)
**Errors.** Every failure is JSON `{error, ...}` with a 4xx/5xx code; nothing a request does can stop
the server. 422 carries `errors: [..]` (plan validation). 409 = conflict (nothing to undo, chat busy,
split on a cut). Handler threads are capped (64); over the cap the server answers 503 with
`Retry-After`, so lazy-load thumbnails instead of requesting hundreds at once.

**Plan payload** (`GET /api/plan/<slug>`, PUT, `/op`, undo, redo, `/new` all return it):
`{slug, draft, draft_file, draft_path, version_files:[{file, version, kind, mtime}], history:{pos, n,
can_undo, can_redo}, timeline, errors:[..]}`. GET creates the draft from the newest version file when
none exists (that is "open plan"). `GET /api/plan/<slug>/timeline` returns only the timeline.
`timeline` = `render.resolve_timeline` (same expansion as a render, nothing encoded, cached by plan hash):
- `pieces`: `{id, sec, seg, sub, type, kind, track, start, end, clip, clip_rel, in, out, speed, label,
  captions:[{t0, t1, text}], overlays:[{kind, text, sub, position, t0, t1}]}`. Times are timeline
  seconds; `in`/`out` are source seconds. Tracks: `V1` main picture (one piece per tightened talk
  piece / montage shot / matchcut side; kind `clip`, `still`, `title`, `black`, or `invalid` for a
  segment that could not be resolved), `V2` cutaways (`kind` `cutaway` or `auto_cover`), `V3` overlay
  graphics (`text`, `sub_text`, `position`), `A1` dialogue/camera sound (`kind` voice/natural/mix),
  `A2` music per section (`song`, `in`/`out` = song time). Ids are `<track>:<hash of the segment's
  content>.<sub>`, so they stay the same while a segment is unchanged, even when it moves.
- `segments`: one row per plan segment `{sec, seg, key, type, start, end, label, clip, invalid, pieces,
  src_map?}` (`src_map` = `[[src_a, src_b, timeline_t], ...]` for talk).
- `sections`: `{i (= index), name, start, end, beats:[timeline t], music:{song, path, title, offset,
  gain_db, mode, beats_in_section}}`; top-level `beats` = all of them flattened. Also `duration`, `frames`, `fps`,
  `width`, `height`, `sfx`, `warnings`.

**PUT /api/plan/<slug>** body: the plan, or `{plan, force}`. **POST /api/plan/new** `{title, slug?}`
(slug derived from the title, made unique). **Commit** refuses a draft with errors unless
`{force: true}`; it also bumps the draft's `version`. **Undo** history lives in
`plans/.history/<slug>/` (200 steps); an edit made outside the studio (Claude) is recorded as a step
when the watcher sees it, so Undo reverts Claude's edit too.

**Op semantics.** Every op validates afterwards: an op that adds validation errors is refused (422)
unless `force: true`. `move`: `to` is the index after the segment is removed. `trim`: talk/broll move
`in`/`out` (talk overlays/cutaways keep their source moment when `in` moves); still/title/black change
`dur` (still `in` also moves `t`); matchcut `in` → `pre`, `out` → `post`; montage `in` moves the first
shot, `out` spreads over `max_shot`. `split`: talk/broll split at the exact source time under `t`
(overlays, cutaways and sfx go to the half they start in, with `at` re-based); montage splits the shot
list (cutting the shot under `t`); still/title/black split `dur`; a matchcut can't be split.
`delete` with `ripple: false` leaves a black gap of the same length. `insert`/`overwrite`/`freeze`
cut the segment under the time first; at/after the end they append to the last section.
`add_section`: `at` = section index (empty section) or `at_t` = timeline time (everything after it moves
into the new section). `add_cutaway`/`add_overlay` accept `at_t` (timeline seconds) instead of `at`.
`set`/`set_style`: a field set to `null` is removed.

**Jobs.** `POST /api/render {plan_file: "plans/x_v2.json", preview: true, only?}` refuses a draft
(409) unless `allow_draft: true`. Jobs are subprocesses (`python -m cutroom.cli render|qa|overnight`,
own process group); render/qa share one lane, so a second render queues. Job:
`{id, kind, title, status: queued/running/done/error/stopped, progress 0-1, stage, log_tail, result,
error, meta}`. `GET /api/jobs/<id>` (full log), `POST /api/jobs/<id>/stop`.

**Overnight.** `POST /api/overnight {action: "start", until?: "HH:MM", tag?, tag_limit?, model?,
dry_run?}` starts `cutroom overnight` detached (`overnight.start_background`, so it survives a studio
restart; it is not in `/api/jobs`). `status` returns its `state.json` (`running`, `stage`, `stages`, ...)
plus `report_md` once a report exists; `stop` asks it to stop after the current item. While it runs the
SSE stream carries `overnight` (the same state) every 3 s.

**SSE `/api/events`** (`event: <name>` + `data: <json>`; `: ping` every 15 s): `hello {jobs, governor}`
on connect, `plan_changed {file, path, slug, kind: draft/version/plan, source: studio/external, deleted,
history?}` (plans/ polled every 1 s; reload on `source: external`), `job` (a job dict), `jobs
{running}`, `render_done {job, file, url, sidecar, plan_file, preview}`, `qa_done {job, video, qa_json,
qa_html}`, `governor` (every 3 s), `notes {count}`, `markers {created|updated|deleted|markers}`, `chat`
(copies of chat events, for other tabs).

**Aliases** kept for the frontend: plan payload `can_undo`/`can_redo`; project `root`, `references`,
render/reference `file`, plan `updated`; job `label`, `eta_s`; `render_done.plan`; `plan_changed.by`
(`hand` = the studio wrote it, `claude` = anything else); chat `done.session`, `error.message`,
`tool_result.ok`; chat history `session`; grab `source`, `t`.

**Media extras.** `/media/grab/<file>` and `/media/marker/<file>` serve `studio/grabs/` and
`studio/markers/`. `/media/thumb/<id>` without `t` = middle of the clip. All media routes are
traversal-safe and support Range (206/416).

**Markers.** `POST /api/markers {plan, t, text, kind?, render?, file?}` auto-grabs the frame (from the
plan's draft at `t`, the cutaway if one is on screen; from the render if `render` is given; from the
reference video `file` for `kind: ref`). `PATCH /api/markers/<id> {text?, status?, t?}`,
`DELETE /api/markers/<id>`. The CLI side: `cutroom markers [list|done ID] [--plan] [--all] [--json]`.

**Grab** `POST /api/grab {source: program, plan, t}` · `{source: program, file: <render>, t}` ·
`{source: clip, clip, t}` · `{source: ref, file, t}` → `{url, path}`.

**Context** (`POST /api/context`, also accepted inline in `/api/chat`): `{plan: slug, playhead: t,
selection: [[si, gi], ...], source: {clip, clip_rel?, in, out, t}, render?: <file playing>,
reference?: {file, t}, note?}`. The server adds `draft_path`. `GET /api/context` returns it.

**Chat stream** (`POST /api/chat` answers `text/event-stream`, read it with `fetch`): events
`session {session_id}`, `text {text}` (deltas), `tool {id, name, input}` (input = short summary),
`tool_result {id, is_error, text}`, `warning {text}`, `status {text}` (Codex retries; informational), `error {error, install?, agent?, stopped?}`, and
`done {session_id, cost_usd, usage?, duration_ms, num_turns, is_error}` (or the turn's one final
`error`) sent LAST (after it the next message
can be sent; while a turn runs a second POST gets 409). `GET /api/chat/history` →
`{session_id, running, messages:[{ts, role, text, agent, ...}]}`. Set `CUTROOM_CLAUDE_BIN` /
`CUTROOM_CODEX_BIN` to use a specific binary.

## Agent bridge (Claude Code or Codex)
The chat panel works with **either Claude Code or OpenAI Codex CLI**, each on the person's own existing
login (subscription). No API keys. `cutroom/agent.py` picks one: `"agent"` in `cutroom.json` is `"auto"`
(the one that is logged in; Claude Code when both are), `"claude"` or `"codex"`. `CUTROOM_AGENT` overrides
it, `POST /api/agent` switches it, `cutroom doctor` shows what is installed and logged in, and
`/api/project` returns `agent: {name, logged_in, available}`. The binaries are found on PATH (the real
executable, never a shell alias); `CUTROOM_CLAUDE_BIN` / `CUTROOM_CODEX_BIN` point at others.

Both run headless in the project folder with the prompt on stdin: the user message plus a
pointing-context block (from `studio/context.json`) and attachment image paths.

- **Claude Code:** `claude -p --output-format stream-json --verbose --include-partial-messages
  [--resume <session>] --permission-mode acceptEdits --allowedTools "Bash(cutroom:*)" Read Write Edit Glob
  Grep --add-dir <repo> --setting-sources project,local --strict-mcp-config` (the last two isolate it from
  the person's global setup; `CUTROOM_CLAUDE_ISOLATE=0` turns that off; `CUTROOM_CLAUDE_MODEL` picks a
  model). It opens attached frame grabs with its Read tool.
- **Codex:** `codex exec [-i <grab>...] -s workspace-write -C <work> --add-dir <repo> --json
  --skip-git-repo-check --ignore-user-config -` for a new chat, and `codex exec -s workspace-write -C <work>
  --add-dir <repo> resume <thread id> [-i <grab>...] --json --skip-git-repo-check --ignore-user-config -`
  to continue one. Frame grabs are attached as images. `CUTROOM_CODEX_ISOLATE=0` drops
  `--ignore-user-config`; `CUTROOM_CODEX_MODEL` picks a model. Its JSONL events are normalised to the
  same panel events: `thread.started` → `session`; `agent_message` updates → `text` (only the new suffix,
  so the reply streams); `command_execution` → `tool` "Bash" + `tool_result` (output, exit code);
  `file_change` → `tool` "Edit" (file names); `turn.completed` → `done` (with token `usage`);
  `turn.failed` → one `error`. "Reconnecting..." retries are not errors: they arrive as `status` lines.
  Unknown event and item types are ignored.

The session id (Claude session or Codex thread) is kept in `studio/chat.json` with the agent it belongs
to, so the conversation continues, a session is never handed to the other agent, and **New chat** starts
fresh. If a resumed session is gone, the bridge starts a new one and says so. The final event of a turn
(`done` or one `error`) is sent after the bridge is free. If the agent isn't installed (or Codex is not
logged in), the panel gets an `error` with `install` steps for that agent.

Instructions: at `init` and studio start the project gets `CLAUDE.md` (which imports the repo's
`CLAUDE.md`), `.claude/skills` (linked from the repo) and `AGENTS.md` for Codex. `AGENTS.md` tells Codex
to read the repo's `CLAUDE.md`, its skills and `docs/ARCHITECTURE.md`, and inlines the "Studio mode" and
"Talking in the studio" sections verbatim (Codex may not follow file includes). That block is
regenerated each time; anything written outside it is kept.

## Overnight understanding + governor
See `cutroom overnight --help` and `docs/OVERNIGHT.md`.
