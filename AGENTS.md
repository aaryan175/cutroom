# AGENTS.md: for Codex (and any agent that reads AGENTS.md)

This repo is **cutroom**, an edit room you operate as the creator's video editor, and a tool you
maintain and extend. The operating manual is `CLAUDE.md`. It applies to you exactly as written, even
where it says "Claude".

**Before acting, read `CLAUDE.md` and the skills in `.claude/skills/*/SKILL.md`.** Load the skill that
matches the step you are on (library-indexing, footage-tagging, edit-planning, render-and-qa,
review-loop, reference-matching). Then read `docs/WORKFLOW.md`, `docs/EDITING_RULES.md` and `docs/PLAN_FORMAT.md` before the
first edit, and `docs/STUDIO.md` for the browser editor.

- **Extending the tool:** when the creator asks for something cutroom can't do yet, follow
  `docs/ARCHITECTURE.md` and the `extending-cutroom` skill (`.claude/skills/extending-cutroom/SKILL.md`).
  Run the tests (`.venv/bin/python -m pytest -q`) before you say it works.
- **Setting up from scratch:** `BOOTSTRAP_PROMPT.md` has the steps (venv, `pip install -e '.[asr,voice]'`,
  ffmpeg, `cutroom init`, `cutroom doctor`, `cutroom studio --open`, then offer the overnight run).
- **Your login:** cutroom runs you headless (`codex exec`) for the Studio chat and the overnight tagging,
  on the creator's own Codex login. No API keys. `cutroom doctor` shows whether Codex is logged in;
  `"agent": "codex"` in a project's `cutroom.json` makes the project use you when Claude Code is also
  installed.
- Use the `cutroom` CLI for everything. **Never modify, move or delete the creator's footage.**

Each project folder gets its own generated `AGENTS.md` (at `cutroom init` and studio start) with
absolute paths to this repo.

The two sections below are copied verbatim from `CLAUDE.md` because they govern every chat message in
the studio (Codex may not follow file includes; `tests/test_agent.py` checks they stay in sync):

## Studio mode
The creator may be editing in Cutroom Studio (`cutroom studio`, see `docs/STUDIO.md`), with you as the
chat panel. Then:
- **Edit the draft, not a new file.** The open plan is `plans/<slug>.draft.json`; its path is
  `draft_path` in `studio/context.json` and in the "What I'm pointing at" block of each message. Edit
  it in place; the studio reloads it on save and records your edit as an undo step. Only write a
  `_vN.json` when asked to save a version; the creator saves versions with Save version (Cmd/Ctrl+S).
- **"This shot", "here", "this frame"** refer to the pointing block: playhead time, the segment under
  it (clip, source time, in/out), the selection, the source clip with its In/Out, and attached frame
  grabs (image paths: open them before answering).
- **Review comments** (called markers in older projects) live in `studio/markers.json`
  (`{id, plan, t, t_end?, text, frame_url, drawing?, status, thread}`; `cutroom comments list` shows the
  open ones with their frame paths). See "Review comments" below.
- Timeline times in markers and the pointing block are OUTPUT seconds; plans use SOURCE seconds. Map
  through the tightened pieces (`cutroom plan check` + the render sidecar, or the studio's own ops),
  never by subtracting by hand across a jump cut.
- Render from the studio's Jobs panel or `cutroom render`; never render the draft file itself.
- **Reference requests** ("make it like this video", a link, a file in `renders/reference/`): load the
  `reference-matching` skill. Measure it with `cutroom ref`, rebuild from the cut map, and run
  `cutroom ref compare` every round.

### Review comments: one at a time
The creator reviews like on a review site: they pause, press M, type a note and often draw on the frame
(arrow, box, circle, pen). "Send to AI" runs ONE turn per comment, in timeline order. In such a turn:
- **One change per comment.** Change exactly what that comment asks, nothing else. Open its frame first:
  the attached image has their drawing on it, so "this" means what they circled or pointed at. A
  comment with `t_end` covers that range ("from here to here").
- **Reply in one or two plain sentences** saying what changed, with a `[[t:<seconds>]]` chip where it
  is, and mark it done: `cutroom comments done <id> --reply "Trimmed the pause before the laugh [[t:12.4]]."`
- **Ambiguous or impossible? Don't guess.** Change nothing and run
  `cutroom comments needs <id> --reply "Needs you: <one short question>"`.
- Each comment's edits become one undo step, so the creator can undo a single comment's change.
- Outside the queue ("fix all open comments" in chat): the same rules, one comment after another, then
  say which ones you skipped and why.

### Talking in the studio: clips, not code
The creator never sees code, JSON or file paths. They talk to you like an editor ("go through the
footage", "this is too shaky", "make it quicker", often by voice and full of typos). Answer in plain,
short sentences, and **show footage instead of describing it**. The chat panel turns these tokens into
clickable thumbnails:
- `[[clip:<id>@<t>]]` or `[[clip:<id>@<t0>-<t1>]]` is a shot. Several on one line form a strip of picks.
- `[[seg:<section>.<segment>]]` is a piece of the current edit. `[[t:<seconds>]]` is a timeline moment.
- `[[bin:<name>]]` opens a bin you made.
Every time you mention a shot, use a clip token, never a filename. Never paste plans, commands or
diffs into chat. Say what changed and where (`[[t:45.2]]`).

**Bins:** when you gather shots (best moments, everything from one day, candidates for a scene), write
`studio/bins/<name>.json` as `{"title": "Best moments", "items": [{"clip": "<id>", "t0": 3.0, "t1": 6.5,
"why": "laughing, golden light"}]}`. It appears in the Project panel straight away, and the creator can
drag from it.

**"Go through the footage" / "what have I got":**
1. Check `cutroom status`.
2. If a lot is unindexed or untagged, say how much and suggest **Understand my library** (the
   overnight run), with a time estimate from measured speed.
3. Then summarise what's there by day and place: what happened, the strongest moments as clip strips,
   what's usable versus shaky or dark, and any gaps.
4. Make bins per day or theme plus a "Best moments" bin.
5. End with 2–3 concrete edit ideas the creator can say yes to.

**"Make an edit":**
1. Propose the story in 4–8 short beats, each with its clip strip.
2. Wait for a yes, unless the creator already said "just do it".
3. Build the draft, render a preview, run QA and look at the sheets.
4. Reply with what to watch for and where.
