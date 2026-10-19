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
  auto-saves it and pushes an undo snapshot to `plans/.history/<slug>/`. **Save version** (⌘S) copies the
  draft to `<slug>_v<N+1>.json` (the route is still `/commit`). Render always renders a saved version. The
  header says "Saved as v4" or "Draft — unsaved changes since v3"; the **Versions** menu (clock) lists v1..vN
  with when and who saved each (you / AI), and offers Open (play its render, or render it), Compare with draft
  (both renders side by side when both exist, else a change list computed in the browser: segments added,
  removed, trimmed, moved) and Restore (copies it into the draft; undoable).
- **Pointing context.** Every chat message carries the current pointing state: playhead time and the
  segment under it, selected segments, source clip and its In/Out, markers, and attached frame grabs.
  The state also lives in `studio/context.json`, so "this shot", "here" and "this frame" are
  unambiguous.
- **Comments (frame.io-style review).** Pause anywhere and press `M` (or the comment button under the
  Program monitor): a comment pins to that frame, with its thumbnail. Type what should change, and draw on
  the frame (arrow, box, circle, pen; keys `A B C P`, ⌘Z undoes a mark). `O` (or **Range**) makes it "from
  here to here" up to the playhead, `I` moves its start. The Comments tab lists them (Open, AI working,
  Done, Needs you) with the frame, the drawing and a thread; pins on the ruler are coloured by status.
  **Send to AI** works through open comments ONE AT A TIME in timeline order: one AI turn per comment
  (its text, timecode, range, the segment under it and the frame with the drawing composited on it). The
  AI makes that one change, replies on the comment in a sentence with a `[[t:]]` chip, and marks it done,
  or asks "Needs you: ...". Each comment's edit is one undo step. **Stop** finishes the current comment
  and leaves the rest open. Stored as `studio/markers.json` (the old name; `cutroom markers` still works).
- **Looks.** The Looks tab renders YOUR current frame in every look (cutroom/looks.py: Natural, Warm Film,
  Cool Clean, Golden Hour, Teal & Orange, Moody Matte, Punchy Contrast, Soft Pastel, Black & White, Vintage
  Fade, Night Cinematic, plus any `.cube` in `<work>/luts/`), with a before/after split on the big preview
  and an intensity slider. Apply to the whole edit (`style.grade.look`) or one segment (`segment.look`);
  the Inspector shows both. Draft playback approximates the look with a CSS filter; renders use the real
  chain. **Match a reference's colour** picks the look and intensity whose result is closest to a measured
  reference (luma, contrast, saturation, warmth) and shows reference / yours / yours-with-the-look.
- **Tour.** First visit (and `?` → **Show me around**): a 10-step spotlight tour of the real UI.
- **Freeze frame.** Press `F` at the playhead. This inserts a `still` segment of the frame under the
  playhead and splits the segment. The default is 1.0 s, editable in the Inspector.
- **Reference.** Drop a video (or paste a link) and pick "Reference video", or put files in
  `renders/reference/`. The Reference tab opens the **Reference workspace** (`studio_web/js/reference.js`):
  the list of references, the reference player, the **cut strip** (every shot as a tile as wide as it lasts,
  section bands, flash markers, beat ticks, caption-text badges; click = seek, shift-drag = select a part),
  the **Breakdown** (shots, ASL, first-3 s cuts, cold open, content mix, beat sync, tempo, loudness, caption
  swatch, colour per section) and **Compare with my edit** (verdict, pace curve, gaps with jump chips, the
  metrics table). **Side by side** and **Onion skin** (your edit over the reference at partial opacity, or a
  wipe divider) borrow the program monitor and keep both locked (same time, or same % of each runtime, plus
  an offset). **Match…** sends the AI a request naming the reference id and the selected part. "Show on
  timeline" draws the reference's shots as a ghost strip under the timeline, aligned at 0. Press `R` to
  attach a reference frame to chat as "match this". See **Reference engine** below.
- **Import.** Drop files anywhere (or the header's Import menu, or paste): Footage / Reference video / Song /
  Image. Footage is never copied (see **Import** below). Progress shows in the import queue and the Jobs tab.
- **Draft playback.** The program monitor plays the timeline straight from proxies by switching video
  sources at cut times. This is approximate (no grade, captions or overlays drawn as HTML on top), so
  you see edits instantly without rendering. **Play render** switches to the real rendered file.

## Keyboard (Premiere-like)
`Space` play/pause · `J K L` shuttle · `←/→` one frame · `Shift+←/→` 1 s · `I`/`O` set In/Out (source)
· `,` insert source In/Out at the playhead · `.` overwrite · `C` razor (split at playhead) · `Del`
delete · `Shift+Del` ripple delete · `F` freeze frame · `M` marker · `R` reference mark · `Cmd/Ctrl+Z` /
`Shift+Cmd/Ctrl+Z` undo/redo · `Cmd/Ctrl+S` save version · `/` focus chat · `=`/`-` timeline zoom.

## HTTP API (backend ↔ frontend contract)
All JSON unless noted. Media routes support byte ranges (206).

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the app (static files from `cutroom/studio_web/`) |
| GET | `/api/project` | config summary, status counts (same as `cutroom status`), list of plans (draft + versions), renders, songs |
| GET | `/api/clips?q=&setting=&time=&people=&max_shake=&min_quality=&speech=&creator=&offset=0&limit=200` | library search; rows: id, rel, duration, width, height, fps, quality, shake, tags summary, has transcript. Scale filters: `highlights=1` (clips with a highlight moment, strongest first; rows carry `highlight {score, t, kind}`), `dedupe=1` (one clip per duplicate group; rows carry `dupe_of`), `session=N` (one shoot), `near=<clip>` (look-alikes, closest first, `near_dist`). `/api/days` rows also list that day's `sessions` |
| GET | `/api/clip/<id>` | full detail: probe fields, tags, metrics windows, shots, transcript words `[[s,e,w],...]`, sheet URL |
| PUT | `/api/clip/<id>/tags` | merge tags |
| GET | `/media/proxy/<id>` | proxy mp4 (range) |
| GET | `/media/sheet/<id>` | contact sheet jpg |
| GET | `/media/thumb/<id>?t=` | 320-px jpg at t, cached in `frames/thumbs/` (from proxy) |
| GET | `/media/frame/<id>?t=&w=1280` | full-res frame jpg from the original |
| GET | `/media/render/<file>` | rendered video/sidecar/qa files (range) |
| GET | `/media/ref/<file>` | reference videos (range) |
| GET | `/api/refs` | measured references (see **Reference engine**): `{refs: [row]}`; files in `renders/reference/` are imported on the way |
| POST | `/api/refs` | `{src, title?, analyze?}` (a link or a file path) → `{job}`; download + measure as a job, progress on SSE `ref` |
| GET | `/api/ref/<id>` | row + `meta` + `analysis` (analysis.json) + `thumbs` / `sheets` URLs + `cutmap` |
| POST | `/api/ref/<id>/analyze` | `{force?}` → `{job}` (re-measure; resumes an interrupted analysis) |
| GET | `/api/ref/<id>/compare?plan=<slug>` · `?render=<file>` | your latest render of that plan (or a given render) against the reference; no render yet = pacing-only from the resolved draft |
| GET | `/media/refthumb/<id>/<n>` | mid frame of shot `n` (jpg, ≤ 480×270) |
| GET | `/media/refsheet/<id>/<n>` | contact sheet `n` (1-based) |
| GET | `/media/refproxy/<id>` | the reference's 540p proxy (range) |
| GET | `/media/song/<id>` | song audio (range) |
| GET | `/api/plan/<slug>/version/<file>` | a saved version's plan `{file, plan}` (Restore, Compare with draft) |
| POST | `/api/upload?kind=&name=[&id=&offset=&total=&final=]` | streaming upload straight to disk (see **Import**) |
| GET | `/api/upload/<id>?kind=` | `{received}`: bytes stored so far for a piecewise upload (resume) |
| GET | `/api/import` | `{ytdlp, ytdlp_install, kinds: {kind: {max, exts}}, library_roots}` |
| POST | `/api/import/locate` | `{files: [{name, size}], folder?}` → `{files: [{name, size, path, clip, in_library}], found, total}` |
| POST | `/api/import/footage` | `{paths: [file or folder]}` → `{job, files, roots_added, skipped}` (links, never copies) |
| GET | `/media/image/<file>` | images imported into `studio/images/` |
| GET | `/api/plan/<slug>` | `{draft, version_files, timeline}`. `timeline` is the resolved pieces (render.resolve_timeline, no encoding): `[{id, sec, seg, kind, track, start, end, clip, in, out, label, captions, overlays}]` + `sections` bands + `beats` per music section |
| PUT | `/api/plan/<slug>` | replace the draft (validated; returns errors without saving if invalid unless `force`) |
| POST | `/api/plan/<slug>/op` | hand-edit ops, see below; returns the new `{draft, timeline}` |
| POST | `/api/plan/<slug>/undo` · `/redo` | |
| POST | `/api/plan/<slug>/commit` | "Save version": draft → `<slug>_v<N+1>.json`, returns name |
| POST | `/api/plan/new` | `{title}` → empty draft |
| POST | `/api/render` | `{plan_file, preview}` → job id (queued behind the governor) |
| POST | `/api/qa` | `{video}` → job id |
| POST | `/api/export` | `{video: "renders/x.mp4"` or `plan_file, preset: "reels" \| [..] \| "all", cutdown: [15, 30, 60], thumbs, captions}` → job id; runs `cutroom export`; outputs in `renders/exports/<stem>/`; event `export_done {dir, files, warnings}`. The Render menu's Export items call it for the selected (or latest) render |
| GET | `/api/jobs` | list jobs (render/qa/overnight/chat) with progress, log tail |
| GET | `/api/events` | **SSE**: `plan_changed`, `job`, `governor`, `render_done`, `notes`, `ref`, `library_changed` |
| GET/POST/PATCH/DELETE | `/api/markers[?plan=]` | review comments `{id, plan, t, t_end?, text, frame_url, frame_raw_url?, drawing?, status: open/working/done/needs_you, kind: note/ref, author, thread:[{author, text, ts}]}` in `studio/markers.json` |
| POST | `/api/comments/run` | `{plan, ids?: [...], all_open?: true}` → run state; the AI handles the comments one at a time (409 while a run or a chat turn is busy) |
| GET | `/api/comments/run` | run state `{running, plan, queue, current, index, total, done, results:[{id, status, changed, error}], stop_requested}` |
| POST | `/api/comments/stop` | stop after the current comment |
| GET | `/api/looks` | `{looks: [{name, label, desc, kind: builtin/lut, css}], luts_dir}` |
| GET | `/api/looks/preview?clip=&t=` · `?plan=&t=` + `&look=&intensity=` | cached 480-px JPEG of that frame in that look (`intensity` 0-1 or 0-100); `plan` + timeline `t` = the picture under the playhead, with the plan's base grade |
| GET | `/api/looks/sheet?clip=&t=` · `?plan=&t=` | `{clip, t, where, items:[{name, label, kind, css, url}], before_url}`; all looks from one ffmpeg run |
| GET | `/api/looks/match?ref=<id>&plan=&t=[&section=]` | nearest look + intensity to the reference's colour: `{pick:{name, label, intensity}, target, yours, distance, base_distance, ranked, preview_url, before_url, ref_thumb}` |
| GET | `/media/look/<file>` | look preview files (`frames/looks/`) |
| POST | `/api/grab` | `{source: program/clip/ref, clip?, t, file?, ref?}` (`ref` = a reference id) → saved jpg URL in `studio/grabs/` (for chat attachments) |
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
`{slug, draft, draft_file, draft_path, version_files:[{file, version, kind, mtime, by}], history:{pos, n,
can_undo, can_redo}, saved:{latest, version, dirty}, timeline, errors:[..]}`. `by` is `you` (saved with Save
version), `ai` (the file appeared from outside the studio) or null (unknown, older); it lives in
`plans/.history/<slug>/versions.json`. `saved.dirty` = the draft differs from the newest version (ignoring
`version`). GET creates the draft from the newest version file when
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
(slug derived from the title, made unique). **Save version** (`/commit`) refuses a draft with errors unless
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
qa_html}`, `governor` (every 3 s), `notes {count}`, `markers {created|updated|deleted|markers}`, `comments
{type: start/working/chat/result/stopping/end, id?, status?, event?, run}` (the review-comment run), `chat`
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
`DELETE /api/markers/<id>`. A comment can carry `t_end` (a range), `drawing` (`[{tool: arrow|box|circle|pen,
points: [[x, y], ...] (0-1 of the frame), color: "#rrggbb"}]`, composited onto the frame with Pillow into
`studio/grabs/comment_<id>_<hash>.jpg`, which becomes `frame_url`; the plain frame stays in `frame_raw_url`),
`author` and a `thread`. PATCH also takes `status` (open/working/done/needs_you), `reply: {text, author}`
(appended to the thread), `drawing` (re-composited), `t_end: null` (no range); a new `t` re-grabs the frame.
The CLI side: `cutroom comments [list|done|needs|reply|open ID] [--reply TEXT] [--plan] [--all] [--json]`
(`cutroom markers` is the same command).

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

## Reference engine
"Make it like this video" is measured, not guessed (`cutroom/reference.py`, CLI `cutroom ref`, skill
`reference-matching`). A reference lives in `<work>/refs/<id>/` (`id` = short content hash of the file, or of
the link): `source.<ext>` (copied; a symlink for files from `renders/reference/`; downloaded with `yt-dlp` for
links, which must be installed separately), `proxy.mp4` (540p, constant frame rate: frame `i` is at `i / fps`),
`meta.json`, `analysis.json`, `thumbs/sNNNN.jpg`, `sheet_NNN.jpg` and `work/` (per-stage results, so an
interrupted analysis resumes). Stages: proxy → scan (every frame) → frames (a few per shot) → audio → asr →
assemble. Heavy stages take governor heavy slots; speech-to-text takes the single ASR slot
(`ref_asr_model` in `cutroom.json`, default `small`; `CUTROOM_REF_ASR_MODEL` overrides). The overnight run
measures unanalysed references in its `refs` stage. Renders compared against a reference are measured the
same way (no ASR) and cached in `refs/_scans/`.

**Ref row** (`GET /api/refs` → `{refs: [row]}`, also the top level of `GET /api/ref/<id>`):
`{id, title, status: added|downloading|analyzing|analyzed|error, stage, progress 0-1, error, duration, fps,
width, height, url, origin: file|link|"reference folder", file, summary, added, job, thumb, proxy_url}`.
`summary` (once analysed) = `{duration, n_shots, asl, first3_cuts, cold_open_s, talk_pct, broll_pct,
montage_pct, beat_sync_pct, tempo, integrated_lufs, captions, n_sections}`.

**SSE `ref`**: `{id, status: "analyzing", stage, progress, message, job}` while a job runs, then one final
`{...row, status: done|error|analyzed, job, error}`. The job itself is a normal `job` (`kind: "ref"`, its own
lane, `meta.ref` = the id once known).

**`GET /api/ref/<id>`** adds `meta` (meta.json), `analysis` (null until analysed), `thumbs` (one URL per shot,
index = shot `i`), `sheets` (URLs) and `cutmap` (rows below). `analysis`:
```
{version, id, title, origin, url, measured_at, seconds,
 source: {file, proxy, fps, fps_rational, frames, duration, width, height},
 shots: [{i, start, end, dur, f0, f1, frames,                       # f0/f1 = frame numbers, end exclusive
          kind: talk|broll|title|black|flash, kind_conf: high|medium|low|plan, montage, flash,
          luma, contrast, sat, warmth, motion, shake, faces, face_h, mouth, colors: [{hex, pct}],
          speech (0-1 of the shot), beat (cut lands on a beat, ±1 frame), beat_dt, section,
          text: null | {present, band: top|middle|bottom, method: ocr|heuristic, words, n_words, n_lines,
                        size_pct, y_center, x_center, color, outline}, thumb}],
 cuts: [t], flashes: [{f0, frames, t, kind: white|insert}],
 pacing: {n_shots, n_cuts, asl, median_shot, min_shot, max_shot, shots_under_0_5s, first3_cuts,
          cuts_per_s: [count per 1 s bin], cuts_per_s_overall, time_to_first_word_s,
          by_section: [{i, name, start, end, asl, cuts_per_s}]},
 sections: [{i, name: cold open|talk|montage|voice-over b-roll|b-roll|outro, pos, start, end, dur, n_shots,
             asl, median_shot, cuts_per_s, speech_pct, music_pct, talk_pct, broll_pct, montage_pct,
             luma, contrast, sat, warmth, loudness_lufs}],
 audio: {has_audio, duration, integrated_lufs, true_peak_dbtp, lra, speech_spans: [[a, b]], speech_s,
         speech_pct, music_spans, music_pct, tempo, beats: [t], downbeats, drops, onsets, beat_resolution_s,
         loudness_curve: [LUFS per 1 s or null], beat_sync_pct, cuts_on_beat, cuts_considered, considered,
         chance_pct, tolerance_s, cuts_on_onset_pct},
 transcript: null | {language, model, text, segments: [{start, end, text}], words: [[s, e, w]]},
 captions: {detected, method, band, position: top|middle|lower|bottom, y_center, size_pct,
            approx_plan_size_1080, words_per_caption, color, outline, coverage_pct,
            spans: [[t0, t1, band]], samples: [{shot, t, text}]},
 colour: {overall: {luma, contrast, sat, warmth}, by_section: [{i, name, luma, contrast, sat, warmth}]},
 structure: {cold_open_s, cold_open_basis, first_sustained_speech_s, time_to_first_word_s, talk_pct,
             broll_pct, montage_pct, other_pct, ending: {type, last_shot_dur}},
 sheets: ["sheet_001.jpg", ...], summary: ["plain-English line", ...], tools: {...}}
```
Luma/contrast are 0-255 means; `sat` is HSV saturation 0-255; `warmth` = mean red minus mean blue. Kinds are
estimates (faces + speech + text + flatness); the AI confirms them on the sheets.

**Cut map row** (`cutmap`, `cutroom ref cutmap <id> --json`): `{i, start, dur, frames, kind ("broll/montage"
for montage shots), beat, text, luma, faces, section (name), thumb}`.

**Compare** (`GET /api/ref/<id>/compare`, `cutroom ref compare`): `{mode: render|timeline, target, plan,
render, render_url, ref: {id, title, duration}, yours: {duration, label}, verdict: close|near|far, n_big,
rows: [{metric, label, ref, yours, unit, delta}], sections: [{i, name, pos, ref_start, ref_end, yours_start,
yours_end, ref_asl, yours_asl, ref_cuts_per_s, yours_cuts_per_s}], curve: {bins, ref: [cuts/s], yours:
[cuts/s], corr}, gaps: [{metric, severity: big|small|info, text, t}]}`. Sections are aligned by relative
position (the same share of each runtime). `gaps[].text` is plain English ("your cold open is 9.1s vs 3.8s in
the reference"); `t` is a time in YOUR video to jump to. The first compare of a render measures it (seconds to
a minute, under a heavy slot); later calls are cached. In `timeline` mode only pacing, structure from section
names, beat sync against the timeline's beats, and the plan's caption style are compared.

## Import
`cutroom/imports.py` + `studio_web/js/importer.js`. The browser never tells the studio where a dropped file
lives, so:
- **Footage is linked, never copied.** `POST /api/import/locate` finds the originals by name + size (the
  library itself, a folder you name, Spotlight `mdfind`, then the usual folders and mounted volumes, a few
  seconds at most). `POST /api/import/footage {paths}` adds each file's folder to `library_roots` in
  `cutroom.json` when it is not inside one already (never `/`, a volume root, the home folder or the
  project), then runs `cutroom index --files-from <list>` as an `import` job: just those files are
  registered, probed, proxied, sheeted and analysed. SSE `library_changed` when it ends.
- **Uploads** (`POST /api/upload`, the body is the bytes, `Content-Length` or `Transfer-Encoding: chunked`,
  read 1 MiB at a time straight to `<dest>/.upload-<id>.part`). Big files come in pieces with the same `id`
  and `offset` (each piece must start where the last ended, else 409 with `received`; `GET /api/upload/<id>`
  for resuming); `final=1` renames the part to a unique, traversal-safe name. Limits per kind; 507 when the
  disk floor (`min_free_disk_gb`) would be crossed. Kinds:
  `reference` → `refs/_incoming/` (then `POST /api/refs {src: path}`, which links it instead of copying);
  `song` → `music/` + a `cutroom songs index` job; `image` → `studio/images/` (returns `url`);
  `footage-link` → `imports/footage/` (the explicit fallback when the originals can't be found, e.g. a
  browser on another computer; that folder becomes a library root) + an `import` job.
- **Links**: pasting a URL anywhere opens "Add a reference video"; `POST /api/refs {src: url}` answers 424
  with `install` when `yt-dlp` is missing.

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
