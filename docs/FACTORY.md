# Cutroom Factory: comment in the Studio → a team builds the next version

Status: BUILT (build steps 1-12, 14, 15; step 13, the extender, is a stub). Sections 1-11 below are the
design; "As built" right here says what the code does where it differs, how to run it, and how to replay
the acceptance round for real. Code: `cutroom/factory/`. Tests: `tests/test_factory.py`.

## As built (read this first)

**Run it.** `cutroom studio` starts a runner thread for the project (one per project: flock on
`factory/.runner.lock`; `"autostart": false` in `factory/config.json` turns it off). Without the studio:
`cutroom factory run` (foreground, SSH, overnight). Nothing else is needed: the factory uses the person's own
Claude Code **or** Codex login (whichever the chat uses; `"agent"` in `factory/config.json` pins one), no API
keys, no launchd. It stops when the studio stops; unfinished orders resume at their next phase on the next
start.

**Operator flow.** Comments tab → **Send to factory** (Quick fix is the old one-turn-per-comment path) →
the header light turns blue → Program monitor → **Review** tab (or `/review` on the phone) shows the version
side by side with the previous one or the reference, its card, the changed-spots sheet → APPROVE / REJECT /
NOTES, frame notes with pictures. Questions show as one line with numbered buttons (Factory tab, Comments
tab, Review card, phone page).

**Decisions taken on §12's open questions.**
1. The critic runs on every order except a pure text/caption tweak (`phases.critic_needed`).
2. Extensions are never applied automatically. The extender is a stub in this build: a needed capability
   becomes an `extend` sub-order that asks you (needs you); the Apply route answers 501.
3. Cost cap: **per order, default $5** notional (`budgets.cost_usd`). Reaching it stops at the next phase
   boundary with "needs you: Budget reached ... 1 = yes, +$5 / 2 = stop". Role runs (6) and the order wall
   clock (2 h, 4 h for L) are budgets too.
4. APPROVE is recorded by code (versions index + `versions.json` + locks + a ruling) and queues an `approve`
   order: a full-quality render from masters (`renders/<slug>_vN.mp4`) plus QA. **Nothing is ever published.**

**Differences from the design below.**
- Settings live in `<work>/factory/config.json` (not cutroom.json: `Config.save` drops unknown keys).
- Version files keep the studio's naming `plans/<slug>_v<N>.json`; renders are `renders/<slug>_v<N>_preview.mp4`
  (slug + number in every name, so the L0118 collision cannot happen; no zero padding, so the Versions menu
  and Save version keep working).
- Machine QA fails block a version only when they are **new** compared with the base version's render (a
  defect already in v22 is a warn on the v23 card, "already in v22"). With no base render, every fail counts.
- The render-level lock check compares a frame from the middle of up to 12 untouched segments (PSNR: under
  30 dB = HARD, under 35 dB = warn). Audio is not compared byte for byte: loudness is normalised over the
  whole programme, so identical bytes across versions are impossible.
- Read-only guard: the version files, the brain and the order's own comments are hashed around every
  read-only role (not the whole of `plans/` and `markers.json`: you keep editing the draft and adding
  comments while the factory works).
- The lesson-keeper is read-only too: it returns ops and code applies them (it cannot touch other files).
- Grades are project-wide (`factory/grades.jsonl`), set with the HERO / BROLL / NEVER / ME / NOT ME buttons
  in the Source monitor (In/Out span, or the whole clip).
- The Brain view is a sub-tab of the Factory dock tab (Orders · Lessons · Brain), not the Project panel.
- Variants show as tiles on the `/review` page (Keep / Kill); Keep creates a revise order that adopts that
  option without an editor run.
- Chat: a message that asks for edit work ("make a 2 min edit", "fix all my notes", "match this reference")
  gets a **Run in factory** chip; the agent may also write `[[factory:revise]]`.

**Pipeline as built** (`phases.PLANS`): revise / reject / continue / request = intake → producer →
[forensics] → [extend] → edit → render → qa → critic → [fix → render2 → qa2] → integrate → lessons (silent);
variants = intake → producer → variants_edit → variants_render → variants_integrate; approve =
approve_intake → render_final → qa_final → approve_record (no agent); extend = extend_stub.

**Failure handling as built.** Login refused (harness lines only) → `backoff.json` (6 h) + the order needs
you; everything pauses until **Resume** (header / Factory tab / `cutroom factory resume`), which also requeues
it. Usage limit / overload → requeued 30 min later, at most 3 times, then blocked. A role over its wall clock
is killed (process group) and the order blocks; nothing relaunches. A phase interrupted twice blocks.
**Retry** is manual and only from blocked. A runner that died leaves its role/render processes behind: the next
runner stops them (pid files) before it resumes.

**Acceptance (step 14).** `test_acceptance_replay_v022_to_v023_stub` replays the v023 round against the stub
agent: the 11 notes as comments on a rebuilt v022, the v023 edits scripted, 2 notes asked back. It checks
9/11 resolved, 2 questions, no lock-check failures, wall time < 45 min (real render + QA on synthetic footage).

**The real replay (not run: it needs the real footage and a real agent).**
1. APFS-clone the long-form project (`cp -cR <project> <scratch>/replay`); never run it in the original.
2. Rebuild v022 as a cutroom plan: one segment per `render_v5.py` v022 segment (talk / montage / matchcut /
   title / black; cutaways and callouts as overlays), saved as `plans/vlog_v22.json`; render it once
   (`cutroom render plans/vlog_v22.json --preview`) so QA has a base.
3. Add the 11 v023 notes as comments at their output timestamps (`POST /api/markers` with `t` and the verbatim
   text; the dictation of 10-11 08:52 and 08:54).
4. `cutroom factory send --plan vlog --all-open`, then `cutroom factory run`. Watch `cutroom factory status`.
5. Score against v023's layer (supportive restore, "hot" lines trimmed, two callouts removed, caption words,
   dead air split, slow-mo removed, the 7:19 walk removed, the 8:23 shot swapped, the ban animation removed):
   pass at ≥ 9/11 done that way, the rest asked as questions, no lock-check failure, under 45 min.
6. Cost: the budget defaults to $5 per order. Expected spend on a Claude subscription: producer (small) +
   editor + critic (+ fix) + lesson-keeper ≈ 4-6 role runs; set `"budgets": {"cost_usd": 3}` to cap the
   test. Log the `cost_usd` from `cutroom factory order <id> --json` in the run notes.
Repeat for v021 → v022 (14 notes).

---

## 0. Why, and the one idea that matters

Today the Studio has two AI paths:
- **Chat** is one `claude -p` / `codex exec` turn per message.
- **Send to AI** on comments is one turn per comment (`CommentRunner`), and each turn sees one note
  and nothing else.

Neither path carries what made v023 good. Part 1 of the research showed that v023 was **not** built by
a swarm. One long session carried, across 23 versions:
- a deep understanding of the footage;
- a **measured** reference breakdown;
- a ledger of every ruling ("no music under talk", "no smoking in intros", "it was better before");
- a note → segment → change table per round;
- targeted frame checks at the changed spots;
- a board for comparing versions.

The quality came from **process plus durable context**, not parallelism.

So the factory design rests on three pillars:

1. **A project brain on disk** replaces chat memory. Every role reads it and the factory keeps it
   current: the reference breakdown, the story, the decisions ledger, the notes ledger and the lessons.
2. **A fixed pipeline of short, single-purpose agent runs.** Each run is a headless `claude -p` or
   `codex exec` with a role prompt, a narrow tool set, a wall clock and a structured output.
   Deterministic code does everything that can be measured: mapping notes to segments, rendering,
   machine QA, the lock check and the gates.
3. **A code-driven runner on a durable on-disk bus.** It is copied from the reel factory's generation
   3 (`orderd`) and does not repeat that factory's failure modes. No LLM is "always on". The runner is
   plain Python, so it costs nothing while idle.

The operator flow stays exactly what he already does:
1. Watch the version.
2. Press `M` and comment (and draw).
3. Press **Send to factory**.
4. Get a new version on the review board with a card saying what changed where.
5. Give a verdict.

---

## 1. Concepts

| Term | Meaning |
|---|---|
| **Order** | One unit of factory work. It comes from a comment batch, a chat request, a verdict, a "try options" request or a feature request. It has one owner plan (slug) and one base version. |
| **Bus** | `<work>/factory/orders/<order-id>/`: one folder per order, with an append-only `events.jsonl`. |
| **Runner** | A Python loop that picks the next order and drives it through the phases. It runs inside `cutroom studio` as a thread, or alone as `cutroom factory run`. One runner per project, enforced by flock. |
| **Phase** | One pipeline step. Each phase writes a checkpoint, so a restart resumes and does not start over. |
| **Role** | One headless agent run with a role prompt (producer, forensics, editor, critic, extender, lesson-keeper). |
| **Brain** | `<work>/factory/brain/<slug>/`: durable project knowledge that every role reads. |
| **Card** | The delivery note for a version: note → change → `[[t:]]` → proof, plus caveats and questions. |
| **Lesson** | A rule learned from a rejection, a correction or a critic HARD finding. Where possible it is backed by a machine check (lint or QA). |

---

## 2. Job model and the bus

### 2.1 Layout (all inside the project's work folder, so it is portable and per project)
```
<work>/factory/
  config.json?                 (optional; defaults live in cutroom.json "factory": {...})
  PAUSED                       (exists = runner idles; kill switch, fails closed if unreadable)
  .runner.lock                 (flock: exactly one runner per project)
  backoff.json                 {until, why: "account-refused"|"limit", set_at}
  orders/<id>/
    order.json                 immutable request (written once)
    events.jsonl               append-only status lines (the ONLY status source)
    phases/<nn>_<name>/        checkpoint per phase: in.json, out.json, done.json, role logs
    roles/<role>-<n>/          prompt.md, stream.jsonl (raw agent events), result.json, usage.json
    candidate.json             the plan the editor produced (never the user's draft)
    ledger.json                note → segments → change → output t → proof
    card.json / card.md        delivery card
    result.json                {outcome: ready|needs_you|blocked|stopped, version, why, artifact}
  brain/<slug>/                see §4
  lessons.json                 project lessons + inbox (see §7)
  checks.json                  declarative lint/QA checks derived from lessons (see §7.3)
```
The order id is `o<UTC yyyymmddThhmmssZ>-<4 hex>`. Every artifact the factory writes carries
`<slug>_v<NNN>` (lesson L0118 from the reel factory: bare `v008.mp4` names collided).

### 2.2 `order.json`
```json
{"v": 1, "id": "o20261019T120501Z-3f2a", "kind": "revise",
 "plan": "dayvlog", "base_version": "dayvlog_v022.json", "base_render": "renders/dayvlog_v022.mp4",
 "reserved_version": 23,
 "comments": ["m_81c2", "m_81c3"],
 "text": "optional free text from chat / verdict",
 "attachments": ["studio/grabs/…jpg"],
 "ref": "r_9ab1" ,
 "variants": null,
 "by": {"user": "you", "via": "comments|chat|verdict|review"},
 "priority": 2, "created": "2026-10-19T12:05:01Z",
 "budget": {"wall_s": 7200, "role_runs": 6, "cost_usd": 15.0}}
```
**Kinds:**
- `revise`: comment batch or NOTES verdict.
- `reject`: REJECT verdict with a reason.
- `request`: chat requests such as "make a 2-min edit", "match this reference" or "go through the footage".
- `variants`: "try 2–3 options for this comment".
- `extend`: a new renderer capability.
- `continue`: resume a `needs_you` or `blocked` order after an answer.
- `approve`: deterministic, see §8.4.

**Priority** (from the reel factory): `reject` 1 > `revise`/`continue` 2 > `variants` 3 >
`request` 4 > `extend` 5. Ties go oldest first.

### 2.3 `events.jsonl` (the status line, written only by `factory/bus.py`)
```json
{"v":1,"at":"2026-10-19T12:05:03Z","order":"o…","phase":"edit","role":"editor-1",
 "state":"progress","text":"swapped the 7:19 walk for C5641 @ 12.0–14.4","why":null,"artifact":null}
```
- `state` is a **closed set**: `queued · picked_up · running · progress · needs_you · ready · blocked ·
  stopped · done`.
- `needs_you` and `blocked` require `why`.
- Timestamps are always stamped by code, never by a model. This prevents the reel factory's
  back-dated and fake-UTC stamps.
- Role agents never write events directly. The runner turns their streamed tool steps into `progress`
  lines, rate-limited to one every 2 s, which feed the UI.
- The newest line wins. An order's status is computed by folding its events.

### 2.4 Creating orders and bundling
- **Send to factory** (Comments tab) posts `POST /api/factory/orders {kind: "revise", plan, comment_ids |
  all_open}`.
- **Debounce of 90 s per plan.** A second Send, or new comments pressed into the same plan within the
  window, are **folded** into the pending order. A folded order gets `picked_up: folded into o…`.
  This is the reel factory's 120 s bundling, so one burst of notes produces one version, not five.
- **One running order per plan.** New comments made while an order runs queue as the next order,
  based on the version that order produces. This matches "your notes go first".
- **Reserving a version number** happens at intake, under the plans lock. If an order fails, its
  number is skipped and the card says "v024 skipped (blocked: …)". Numbers never collide.
- An order snapshots its base (plan file hash and render hash) into `phases/00_intake/in.json`. Later
  edits by the operator in the draft never leak into a running order.

### 2.5 Runner loop (`factory/runner.py`)
Every 2 s while orders exist, every 15 s when idle. This is pure Python and makes no LLM call.
1. **Gates.** Each gate makes the tick do nothing:
   - `PAUSED` exists;
   - `factory.enabled` is false;
   - `backoff.json` is active;
   - the governor reports the disk floor;
   - the login check failed (cached 10 min).
2. **Running order:**
   - if its current phase's process is dead, finalize that phase from its checkpoint;
   - if the role exceeded its wall, kill the process group (TERM, then KILL after 3 s) and mark the
     phase `timeout`;
   - the order's total wall works the same way.
3. **No running order:** pick the highest-priority `queued` order whose plan has no running order, and
   whose `not_before` (if any) has passed.
4. Drive the order to its next phase (§3).
5. Heartbeat: write `factory/state.json` `{runner_pid, at, running_order, phase}` **from code**. The UI
   treats the runner as alive when the pid is alive and `at` is under 30 s old. A model never writes
   the heartbeat.

**Restart:** on start, the runner reads every order whose last state is non-terminal and resumes it
at the first phase without `done.json`. A phase that was mid-run is re-run once
(`attempts.phase <= 2`). After that the order goes to `blocked`, with "interrupted twice at <phase>".
Nothing ever relaunches in a loop.

---

## 3. The pipeline (phases) and who does what

```
intake → producer → [forensics] → [extend] → edit → render → machine-QA → critic → [fix → render → machine-QA]
       → integrate (gates) → READY (on the review board) → operator verdict → lessons
```
- The phases in brackets run only when the producer's **team plan** asks for them (§3.3).
- Code phases (white below) are deterministic. Role phases (★) are one agent run each.

| # | Phase | Who | Reads | Writes | Default wall |
|---|---|---|---|---|---|
| 0 | **intake** | code | order, comments, base plan/render/sidecar | `mapped_notes.json` | 30 s |
| 1 | **producer** ★ | role, small model | mapped notes, brain (decisions, story), lessons brief | `changes.json` + `team.json` + questions | 4 min |
| 2 | **forensics** ★ | code (`cutroom ref analyze/compare`) + role | reference analysis, sheets, our render | `brain/REFERENCE.md`, `brain/ref_pairs.json` | 20 min (+ heavy slots) |
| 3 | **extend** ★ | role, in a git worktree | request, ARCHITECTURE.md, the extending-cutroom skill | patch + tests (sub-order) | 60 min |
| 4 | **edit** ★ | role, strong model | changes.json, brain, base plan, lessons brief, frames | `candidate.json`, `ledger.json` | 30 min (S) / 75 min (M/L) |
| 5 | **render** | code (Jobs heavy lane) | candidate | `renders/<slug>_vNNN_preview.mp4` + sidecar | governor |
| 6 | **machine-QA** | code | render, sidecar, base | `qa.json`, `lock.json`, `changed_sheet.jpg`, `refcmp.json`, `lint.json` | 10 min |
| 7 | **critic** ★ | role, strong model, **blind** | render, sheets, operator words, rules, REFERENCE.md | `findings.json` | 15 min |
| 8 | **fix** ★ | editor again (1 round max) | findings + machine fails | candidate v2, proofs | 30 min |
| 9 | **integrate** | code | everything | version file, card, comment replies | 1 min |

### 3.1 Code phases in detail

**intake (`factory/mapnotes.py`).** This is how the v023 session resolved notes, made deterministic.
The session dumped the plan, mapped each timestamp to its segment and read the words in that span.
For each comment:
- **Pieces in the window.** List every piece of the base timeline overlapping `[t−1, t+1]`, or
  `[t, t_end]` for a range comment (EDITING_RULES 108). Use `render.resolve_timeline` on the base
  plan.
- **Per piece:** track, kind, clip, source in/out, output start/end, the spoken words in that span
  (from the transcript), any overlay or caption text, and the segment key.
- **Frames:** the annotated frame (`frame_url` with the drawing) plus a clean frame per candidate piece.
- **Note flags** (regex plus the decisions ledger):
  - `restore` ("better before", "go back", "was good before");
  - `remove` ("remove", "get rid of", "don't need", "unneeded");
  - `conditional_approval` ("approve but…", rule 118);
  - `praise` ("I like", "good"). Praised segments are written into `locks` in the brain.
- **Conflicts.** A note that contradicts an active ruling in `decisions.jsonl` is marked
  `supersedes: <ruling id>`. The newest note wins (rule 109).

**render.** This is the existing `cutroom render <candidate> --preview` through the studio Jobs heavy
lane (governor slots). Segment caching makes small revisions fast, as `render_v5.py`'s per-segment
md5 cache did for v023. A full-quality render happens only after APPROVE, or on request.

**machine-QA (`factory/gates.py`).** This is everything the v023 session checked by hand, run by code:
1. **`cutroom qa`** on the render: cuts on planned frames, black, freeze, loudness, silence holes,
   music under speech, faces under captions, shake, repeats.
2. **Lock check (`factory/lockcheck.py`, rule 106).** It compares base and candidate plans at segment
   level. Every segment **not** named by a mapped note must be byte-identical: same clip, in/out,
   order, overlays and captions. Any unexplained diff is HARD. At render level, it compares sampled
   frames of untouched spans (PSNR ≥ 45 dB, from the sidecars) and the audio md5 per untouched span.
3. **Changed-spots sheet.** For every ledger row, grab the frames at output t−0.5, t and t+0.5 at
   960 px, then tile them into `changed_sheet.jpg` with labels "note 3 → 7:19". The v023 session did
   this with 9–24 grabs per version; here it is automatic.
4. **Lint** (`checks.json`, §7.3) on the candidate plan: banned clips/spans (grader NEVER, "not me"),
   max B-roll %, forbidden overlay kinds/transitions, caption-vs-transcript match, and so on.
5. **Reference compare** (`cutroom ref compare`) when the plan has a reference.
6. **A/V drift** = 0 from the sidecar (the v012 lip-sync lesson).
7. **Link check.** A Range GET on the render URL returns 206 (rule 113).

Each check is pass, warn or fail, with a timestamp and an evidence frame.

**integrate.** This is the gate before anything is shown as ready (§6).

### 3.2 Roles in detail

Every role runs through a new `agent.run_role()` (§9.2) with:
- the role prompt `cutroom/factory/prompts/<role>.md`;
- a generated context block (paths, never pasted file contents beyond about 8k chars);
- a **scoped lessons brief** (§7.4);
- a JSON output schema (`--json-schema` for Claude, `--output-schema` + `-o` for Codex).

Role runs are **fresh and never resumed**. Continuity lives in the brain, not in a chat session.
Optional: `factory.editor_resume: true` keeps one editor session per plan for people who prefer it.

| Role | Purpose (mapped from what the v023 session did) | Tools (Claude names) | Codex sandbox | Model default | Output |
|---|---|---|---|---|---|
| **producer** | Reads the notes like the session did at the top of each round. Turns each note into an explicit change (one per note), sizes the job, decides the team, writes questions for notes it cannot place (≤ 320 chars, numbered options). Never edits. | Read, Glob, Grep, `Bash(cutroom say:*)`, `Bash(cutroom frames:*)`, `Bash(cutroom search:*)` | read-only | small/fast (`factory.models.producer`) | `changes.json`, `team.json`, `questions[]` |
| **forensics** | The session's reference phase: what the reference does, per section; pacing and cutaway density; teaser grammar; songs and SFX; caption style; the "same recipe on your footage" mapping; section pairs for the compare view. Runs only when needed and is cached by ref id. | Read, Glob, Grep, `Bash(cutroom ref:*)`, `Bash(cutroom frames:*)` | read-only (+ ref jobs run by code) | strong | `REFERENCE.md`, `ref_pairs.json` |
| **editor** | The builder. Applies `changes.json` to a copy of the base plan and nothing else. Searches footage, opens frames and contact sheets **before** choosing a shot, writes one ledger row per note, runs `cutroom plan check`. In the fix round it fixes every critic HARD finding and machine fail, with a proof per fix. | Read, Write, Edit, Glob, Grep, `Bash(cutroom:*)` except `render` and `transcribe` (the runner renders) | workspace-write | strong | `candidate.json`, `ledger.json`, `proofs[]` |
| **critic** | The independent audit. It watches what was delivered against the operator's words, the rules and the reference. It **never** sees the editor's ledger reasoning, only the render, the sheets, the notes and the machine QA. It checks every note: addressed yes/no/partial, plus new defects. | Read, Glob, Grep, `Bash(cutroom frames:*)`, `Bash(cutroom qa:*)`, `Bash(cutroom ref compare:*)` | read-only | strong (may differ from the editor: Claude critic on a Codex editor and the reverse, when both are logged in) | `findings.json` `[{sev: HARD|SOFT, t, note, rule, text, evidence}]`, `per_note[]` |
| **extender** | The `extending-cutroom` skill as a role: a new segment type, effect, caption style or QA check. It works in a git worktree of the cutroom repo, with `pytest -q` green required. | Read, Write, Edit, Glob, Grep, Bash (inside the worktree only) | workspace-write in the worktree | strong | patch, tests, `EXTEND.md` |
| **lesson-keeper** | After a verdict or at the end of an order: triages that order's inbox into lessons (add, merge, supersede, or none with a reason) and proposes a check for `checks.json`. | Read, Write (`factory/lessons.json` and `checks.json` only, enforced by a hash check of other files), Grep | workspace-write | small | lesson ops |

**Enforcement by code, not trust:**
- **Read-only roles (producer, forensics, critic).** The runner hashes `plans/`, `studio/markers.json`
  and `factory/brain/` before and after. Any change aborts the phase as HARD: "critic modified files".
- **The editor writes only `candidate.json`, the ledger and bins.** It never writes the user's draft
  or `_vN` files. The integrator is the only writer of a version.
- **No role runs `cutroom render`, `transcribe` or `export`.** The allowed-tools patterns exclude them,
  and the runner sets `CUTROOM_FACTORY_ROLE=<role>`; `cli.py` refuses those subcommands when it is set.
  Heavy work stays in the runner, where the governor sees it.
- **Footage is never writable.** Library roots are never `--add-dir`, which is the existing invariant.

### 3.3 When the team expands (the producer's `team.json`)

The producer sizes each order. Code then validates the size, so the producer can't under-size it.

| Size | Typical notes (from the v019–v023 notes) | Team |
|---|---|---|
| **S: small edit** | remove a clip, trim dead air, drop a graphic, fix caption words, restore an earlier cut, shorten a montage | editor → render → machine-QA → (critic only if `factory.always_critic`, default **on for ≥ 3 notes**) → integrate |
| **M: craft edit** | swap shots ("the clip at 1:37 is horrible"), re-pick B-roll for a line, story reorder, new montage, music change, voice clean change | editor → render → machine-QA → **critic** → fix (≤1) → integrate |
| **L: new capability or new direction** | "make cool motion graphics for X" when no overlay kind exists, a new transition, "make it like this reference" for the first time, a new edit from scratch | **forensics** (if a reference is involved and the cached breakdown is missing or stale) and/or **extend** (if `changes.json` has an action `new_capability`), then the M path |

Code overrides:
- any `restore` flag → at least M (a restore must be lock-checked against the old version);
- any `new_capability` → L;
- a `request` order of type "new edit" → L, with forensics when a reference is attached;
- more than 12 notes → M even if every note is trivial.

**Extend never blocks the whole order.** The capability becomes a sub-order (`kind: extend`, parent
id). The parent continues with every other note, and the note that needs the feature is answered
"Needs a new feature: <x>. Building it; it will land in the next version." When the extend sub-order
passes `pytest` in its worktree, applying it is a **needs_you** card ("New feature ready: word-pop
captions. Apply?") unless `factory.auto_apply_extensions: true`. After it is applied, a follow-up
`revise` order re-runs that one note.

### 3.4 Variants (from the Deck's 10-variant batches, made sane for long form)
**Try options** on a comment (or "give me 3 options" in chat) creates a `variants` order:
- the editor produces N = 2–3 candidates that differ **only** in that note's segments;
- each candidate renders a short **excerpt** (the changed span ± 5 s), not the whole film;
- the review board shows them as tiles with **Keep / Kill / note**;
- Keep folds the chosen variant into the next version as a normal `revise` change.

---

## 4. The project brain (`factory/brain/<slug>/`)

This is the memory the v023 session had and a one-turn bridge lacks. It holds plain files, which
people can read and edit in the UI.

| File | Content | Written by | Read by |
|---|---|---|---|
| `STORY.md` | beats/chapters, through-line, what each section is for (cf. `plan.html`/`story.html`) | editor on `request` orders; editable | producer, editor, critic |
| `REFERENCE.md` | measured "what the reference does" table, per-section pacing, cutaway density, caption style, songs/SFX, "the recipe on your footage" | forensics | all |
| `ref_pairs.json` | `[{label, desc, ref_t, our_t}]` section pairs for the compare view (the v023 `compare_pairs.json`) | forensics; updated by editor when sections move | UI, critic |
| `decisions.jsonl` | rulings `{id, at, subject, ruling, source: comment/verdict/answer, supersedes?}` (e.g. "no music under talk", "no smoking shots in intro", "callouts off") | intake (from flags), producer, answers | all roles; lint |
| `locks.json` | segments the operator praised or approved, keyed by segment key | intake (praise), approve | lockcheck, editor |
| `notes_ledger.jsonl` | every note ever: version, words verbatim, mapped segments, change, proof, status | integrate | producer (re-read before each version), critic |
| `grades.jsonl` | clip grades HERO / BROLL / NEVER and ME / NOTME per span (the Deck grader) | UI | lint, editor |

The brain is the **single source of law** for the project. Prompts are generated from it on every
run, so nothing can drift the way the reel factory's PostCompact re-brief drifted.

---

## 5. Budgets, limits and the governor

**Per role:** a wall clock (table §3), killed by process group. The reel factory's 5 h single-lane
wall turned into lost work; here the walls are short and per phase, and checkpoints keep finished
phases.

**Per order:**
- `budget.wall_s` (default 2 h for S/M, 4 h for L);
- `budget.role_runs` (default 6: producer, editor, critic, fix, plus 2 spare);
- `budget.cost_usd`: the notional cost from Claude's `total_cost_usd` or Codex token usage × a price
  table, default $15.

Crossing any budget stops the order at the next phase boundary, sets `needs_you` with
"budget reached: continue? 1 = yes, +$10 / 2 = stop", and keeps everything built so far.
`claude --max-budget-usd` is passed as well. It is documented for API spend, and whether it applies
under a subscription login is unverified, so the runner's own accounting is the real cap.

**Governor additions (`governor.py`):**
- **New slot kind `agent`.** Default `agent_slots = 1` (auto 2 when RAM ≥ 32 GB) machine-wide, shared
  by the factory, the chat and the comment runner. A headless agent with its child tools uses
  hundreds of MB to 1 GB+, and on a 16 GB machine two of them plus a render is where freezes start.
  The chat and the comment runner already refuse to run while a turn is busy, so this generalizes
  that.
- Agent processes go through `gov.run(kind="agent")`. They are registered, memory-capped
  (`agent_mem_cap_gb`, default 3) and stopped first in a memory emergency.
- Renders keep the **heavy** lane; ASR keeps **exactly one** slot machine-wide. Roles can't call
  `transcribe` (§3.2), and forensics' reference ASR runs as a code job in the ASR slot.
- A free-RAM floor applies before spawning any agent (`min_free_ram_gb`, the existing setting).

**Account limits:**
- **Refusal** patterns (not logged in, subscription access disabled, OAuth expired, credit too low)
  write `backoff.json` `{until: +6 h, why: "account-refused"}`. The order goes to `needs_you`, and a
  header banner says "The AI login stopped working: run `claude` (or `codex login`) once, then press
  Resume". **Resume** clears the backoff, so the operator never waits 6 h after fixing it.
- **Usage limit / overload** patterns set `not_before = now + 30 min` on the order, with at most 3
  attempts, then `blocked`.
- The patterns match **only harness lines** from the agent's own stream: the `result` / `error`
  events for Claude, and `turn.failed` for Codex. They never match text the model wrote. The reel
  factory once flagged itself `limited` for 7 h because its brief quoted "Usage limit reached".

---

## 6. Gates before a version reaches the operator

A version is marked **ready** only when integrate passes all of these:

1. **The candidate plan validates.** `cutroom plan check` passes and lint has no fail.
2. **The render exists and is complete.** The duration matches the timeline and the A/V drift is 0.
3. **Machine QA has no fail.** Each remaining warn is explained in one line on the card (CLAUDE.md
   rule).
4. **The lock check passes.** No unexplained change outside the noted segments.
5. **Every note is resolved.** Each is either done, with a ledger row and proof (frame or metric), or
   `needs_you` with a numbered question. None are silently skipped.
6. **Critic HARD findings are closed** (M/L): each one is fixed, with a machine proof on the
   **re-rendered** file. Only **one** critic run and one fix round are allowed (rule 117 / reel
   L0092). Any HARD finding still open after the fix round sets the order to `needs_you`, with the
   version visible but labelled "not passed: <finding>".
7. **The review link plays.** A Range GET returns 206.
8. **Integrate is the only writer of versions.** It writes `plans/<slug>_vNNN.json` (`by: "factory"`
   in versions.json), the card and the comment replies (`done` with a `[[t:]]` chip, or `needs_you`).
   It never touches the user's draft. The operator gets **Open in draft** on the card.

**What the factory never does:**
- publish, upload, post, or export to a platform;
- send a link anywhere outside the Studio;
- delete renders, plans or footage.

Export stays a human button.

Technical pass, critic pass, operator approval and publish are four different states and are shown as
four different badges (reel invariant 3).

---

## 7. The learning loop

### 7.1 Sources (harvest, code, idempotent; key = `source::kind::text[:80]`)
- REJECT reasons and NOTES text, verbatim;
- `needs_you` answers (rulings);
- critic HARD findings;
- machine-QA fails that reached the critic;
- "it was better before" restores, a strong signal: the factory changed something it shouldn't have;
- comments on the same segment in two consecutive versions (the fix didn't land).

These land in `lessons.json.inbox[]`.

### 7.2 Triage (lesson-keeper role, bounded)
- Runs at the end of each order and after each verdict, over **that order's inbox items only**, at
  most 10 per run.
- For each item it either:
  - adds a lesson,
  - merges it into an existing one,
  - supersedes an older lesson (`status: superseded, superseded_by`), or
  - drops it with a reason ("one-off taste note for this shot").
- Lesson schema: `{id: "P0012", scope: project|global, at, source, symptom, rule, check: <check id>|null,
  tags[], status: active|superseded, superseded_by}`.
- **Global** lessons live in `~/.cutroom/lessons.json`. They carry learning across projects on that
  person's machine and are seeded from `docs/EDITING_RULES.md`, whose rules already have
  Why/Check. **Project** lessons live with the project.
- Unlike the reel factory, untriaged backlog **never blocks** a version: there were 187 untriaged
  items there and rows got stuck behind them. The UI shows the inbox count instead.

### 7.3 Lessons become checks (`checks.json` + `factory/lint.py`)
Most taste lessons are expressible without writing code, as **declarative checks** over the plan or
the QA report:

| check type | example from v001–v023 |
|---|---|
| `banned_clip` / `banned_span` | "This clip, the 701, is horrible"; grader NEVER; "That last person isn't even me" |
| `max_broll_pct {section?}` | B-roll over talk cut from 40% → 3% |
| `forbid_overlay_kind` / `forbid_transition` | "liquid glass … AI vibes"; leak/burn/scribble transitions removed |
| `max_count {kind: sfx}` | SFX 98 → 29 |
| `no_music_under_speech` (QA metric threshold) | "i can heaar th emusic when im speina g" |
| `caption_matches_transcript` | "sell you this house", "I even bought the second account" |
| `cutaway_context {forbid_tags_when_words}` | no eating shots while ordering (rule 3) |
| `max_silence_s` (QA) | "such a loud silence at 3:00" |
| `qa_metric {name, op, value}` | anything `qa.json` already measures (shake, faces under captions …) |

- A lesson with a check runs in **machine-QA (lint)** on every later version of that project, or of
  every project for global lessons.
- Lessons that need new measurement code become an `extend` sub-order to add a `qa.py` check with a
  test. This is the existing rule "every new failure the creator catches should become a check".
- **Render token (from the reel preflight):** machine-QA records `lessons_sha` (active lessons +
  checks) on the card. If the lessons change between edit and integrate, lint re-runs before ready.
  This is cheaper than the reel factory's "a new lesson invalidates all tokens".

### 7.4 Scoped brief (fixes "the brief prints all 465 rules")
Each role gets:
- all active **project** decisions and lessons;
- global lessons whose tags intersect the tags of this order's `changes.json` actions (e.g. `broll`,
  `captions`, `audio`, `graphics`, `story`, `pace`);
- a small fixed core (the CLAUDE.md non-negotiables).

The cap is about 60 rules, newest first, with superseded rules excluded. The brief is generated per
run; nothing is hand-copied into prompts.

---

## 8. The review board (Studio "Review" workspace + phone page)

It copies the Deck's options and what v023's `board/` had, inside the Studio.

### 8.1 Where it lives
- **A new Program-monitor tab, "Review",** next to the Reference tab, for desktop review.
- **`/review` page** served by the studio for phones on the LAN or VPN (the `board.py` heritage). It
  lists everything waiting on you, each with a player, its card and its buttons (the Deck's `/review`).

### 8.2 What it shows (per version)
- **Players:**
  - a **two-up**: vN against the previous version, or vN against the **reference**, chosen by a
    toggle;
  - sync lock (same time, or same % of runtime), plus a "reference audio" toggle;
  - an **onion/wipe** mode reusing the Reference workspace.
- **Section jump chips** from `brain/ref_pairs.json` (the 12 section-pair buttons from `compare.html`).
- **A version dropdown** with a one-line theme per version, e.g. "v010: no stutter cuts, b-roll only
  on jumps". The theme is written by integrate from the ledger.
- **The card:**
  - "What changed": note → change → `[[t:]]` chip → proof thumbnail;
  - "Couldn't place": questions with numbered option buttons;
  - "Caveats" (e.g. "audio checked by measurement only");
  - warn explanations;
  - critic findings → fix → proof.
- **The changed-spots sheet:** click a tile to seek.
- **A story strip:** beats with thumbnails and spoken lines (the `story.html` view), from
  `STORY.md` + the timeline.
- **Badges:** machine QA, critic, approved, exported. These are separate states.

### 8.3 Verdict buttons (from the Deck)
- **APPROVE.** Deterministic, with no agent, as in the reel factory's instant approvals. It records
  `approved: true` in versions.json and adds the version's segments to `locks.json`. If the text has a
  change verb ("approve but change the last clip", rule 118), it records a conditional approval and
  opens a `revise` order for exactly that change.
- **REJECT** (reason required). Creates a priority-1 `reject` order. The producer must decide between
  "restore vN−1 for these spans" and "new approach"; when unclear it asks with numbered options.
- **NOTES.** The same as Send to factory, with free text plus any open comments on that version.
- **Per comment:** the thread, status (open / in factory / done / needs you), **Quick fix** (the
  existing one-turn path) and **Try options** (variants).
- **Variant tiles:** Keep / Kill / note (Deck batch verdict).
- **Clip grader** in the Source monitor: HERO / BROLL / NEVER and ME / NOTME on the current span. It
  writes `grades.jsonl`, and lint and the editor read it.
- **Answers:** a `needs_you` question shows its options as buttons in the Comments tab, on the card and
  in the Factory tab. An answer writes a ruling to `decisions.jsonl` and queues a `continue` order.
  Auto-default answers are **off** by default. If enabled, they apply only to questions marked
  "(recommended)", after N minutes, and never for spend, publish or export.
- **Editor accounts** (the Deck's advisory role) are out of scope for v1. Every Studio user is the
  operator, and the `author` field is kept for later.

### 8.4 Receipts
Every button press is stored as a line in the order's `events.jsonl`, or for approvals in
`versions.json` + `decisions.jsonl`, with code-stamped time and `by`. The **Sends** list in the
Factory tab shows each press with the factory's state (the Deck's `/api/row/<n>/sends`): waiting,
picked up, building, ready, needs you, blocked.

---

## 9. Studio and code changes

### 9.1 UI (plain ES modules, no build step)
- **Header: a factory light.**
  - idle (grey); working (blue, with "v024 · editing 3/6 notes");
  - needs you (amber, with a count); blocked or paused (red, with the reason);
  - login problem (red, with a fix button).
- **Comments tab:**
  - **Send to factory** (primary) next to **Quick fix** (renamed from the current "Send to AI");
  - a per-comment pill "in factory · v024";
  - question buttons on `needs_you`.
- **Chat panel:**
  - Stays for conversation, questions and tiny edits.
  - When a message asks for real edit work ("make an edit", "fix all my notes", "match this
    reference"), the reply offers a **Run in factory** chip. A classifier in code is the first pass
    (verbs and keywords); the chat agent can also emit `[[factory:<kind>]]`.
  - Factory progress appears in the chat as a compact live card, not as a stream of tool steps.
- **Dock: a new "Factory" tab:**
  - orders list (status, plan, version, age, cost);
  - for the selected order, a **pipeline stepper**: Intake · Producer · Reference · Extend · Edit ·
    Render · QA · Critic · Fix · Ready;
  - per role: agent, model, elapsed, cost, last step ("opened C5641 frames"), log;
  - the ledger table; Stop; **Retry** (manual, only from `blocked`); Resume (after a backoff).
- **Dock: a "Lessons" sub-tab:**
  - the inbox count;
  - active lessons, filterable by scope and tag, each showing its check;
  - supersede/edit, and "Make this a check".
- **Project panel: a "Brain" section.** View and edit STORY / REFERENCE / Decisions.
- **Versions menu:** a `factory` author tag and badges, and **Open in draft** for a factory version.
- New files: `studio_web/js/factory.js`, `review.js`, `css/factory.css`.

### 9.2 Backend modules
```
cutroom/factory/
  __init__.py
  bus.py          orders + events (atomic writes, flock per order, fold status, closed state set)
  runner.py       FactoryRunner: tick loop, gates, backoff, resume, phase driver, budgets
  phases.py       intake / render / machine_qa / integrate (code) + role phase wrappers
  roles.py        role table (tools, sandbox, model key, wall, schema) + prompt assembly
  prompts/        common.md producer.md forensics.md editor.md critic.md extender.md lessonkeeper.md
  schemas/        changes.json team.json ledger.json findings.json lessonops.json (JSON Schema)
  mapnotes.py     comment → pieces/words/frames (+ flags: restore/remove/praise/conditional)
  lockcheck.py    plan-level + render-level untouched-span equality
  lint.py         declarative checks.json evaluator over plan + qa.json
  lessons.py      harvest / triage apply / brief (scoped) / sha
  brain.py        brain files read/write, decisions fold (newest wins), locks
  card.py         card.json/md + version theme line + comment replies
  worktree.py     git worktree create/test/apply for extend sub-orders
```
**`agent.py` gains `run_role(role, prompt, *, tools, sandbox, schema, images, timeout, model, cwd,
env)`.** It returns `{ok, result_json, text, cost_usd, usage, refused, limited, error, stream_path}`.
- **Claude:** `claude -p --output-format stream-json --verbose --no-session-persistence
  --permission-mode acceptEdits --allowedTools <role tools> --add-dir <repo> --setting-sources
  project,local --strict-mcp-config --json-schema <schema> [--model M] [--max-budget-usd B]`, prompt
  on stdin.
- **Codex:** `codex exec --ephemeral -s <read-only|workspace-write> -C <work> --add-dir <repo> --json
  --skip-git-repo-check --ignore-user-config --output-schema <file> -o <out> [-m M] [-i img…] -`.
- The event normalisation reuses the existing `parse_event` code. Every run streams to
  `roles/<role>-<n>/stream.jsonl`.
- Images are paths for Claude (read with Read) and `-i` for Codex, as the chat does today.

**Other module changes:**
- **`governor.py`:** the `agent` kind, `agent_slots`, `agent_mem_cap_gb`.
- **`studio.py`:**
  - a `FactoryRunner` thread started with the studio (`factory.autostart`, default true);
  - routes (below);
  - SSE event `factory`;
  - `CommentRunner` kept as Quick fix;
  - both paths take the `agent` slot so they never overlap.
- **`cli.py`:** `cutroom factory run|tick|status|orders|order <id>|stop <id>|pause|resume|lessons
  [inbox|list|add|supersede]|brain [show|edit]`. `cutroom factory run` works without the studio
  (overnight, SSH). Its flock means it can't run alongside a studio runner on the same project.
- **`cli.py` guard:** refuse `render`, `transcribe` and `export` when `CUTROOM_FACTORY_ROLE` is set.
- **`config.py`:** `"factory": {enabled, autostart, debounce_s: 90, agent_slots, models:
  {producer, editor, critic, forensics, extender, lessons}, always_critic_min_notes: 3, budgets: {...},
  auto_apply_extensions: false, auto_default_answers: false, editor_resume: false}`.

### 9.3 Routes
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/factory/orders` | `{kind, plan, comment_ids?|all_open?, text?, attachments?, ref?, variants?}` → order (or the folded order) |
| GET | `/api/factory/orders[?plan=&state=]` | list with folded status |
| GET | `/api/factory/orders/<id>` | order + events + phases + ledger + card + role summaries |
| GET | `/api/factory/orders/<id>/role/<role>/log` | role stream (tail, or full with `?all=1`) |
| POST | `/api/factory/orders/<id>/stop` | stop at once (kill the role process group), state `stopped` |
| POST | `/api/factory/orders/<id>/retry` | manual retry from the failed phase (blocked only) |
| POST | `/api/factory/answer` | `{order, question, option, text?}` → ruling + `continue` order |
| POST | `/api/factory/verdict` | `{plan, version, disposition: APPROVE|REJECT|NOTES, text}` |
| POST | `/api/factory/variants/<order>` | `{items: [{variant, verdict: KEEP|KILL|NOTES, note}]}` |
| POST | `/api/factory/apply-extension/<order>` | apply a passed extend patch (operator only) |
| GET/POST | `/api/factory/state` · `/pause` · `/resume` | runner state, kill switch, clear backoff |
| GET/POST/PATCH | `/api/factory/lessons` | list, inbox, add, supersede, attach check |
| GET/PUT | `/api/factory/brain/<slug>/<file>` | brain files (validated per file) |
| POST | `/api/grades` | clip grades |
| GET | `/review` | phone review page |

SSE `factory`: `{order, plan, state, phase, role, text, version?, cost_usd?, needs_you?}` and
`factory_state` (light, every 5 s while anything runs).

---

## 10. Failure modes and the countermeasure built in

| Seen in | Failure | This design |
|---|---|---|
| reel gen 2 | always-on LLM orchestrator burned about $950 idle | runner is code; agents run only inside phases |
| reel gen 2 | 2,110-launch healer storm (no refusal branch, no backoff, timer never reset) | refusal → backoff file + needs_you; limit → bounded retry; a phase re-runs at most once after an interruption; **no healer exists** |
| reel gen 2 | model-written heartbeat killed healthy workers | heartbeat written by the runner from pid liveness |
| reel gen 2 | self-detected "Usage limit reached" from its own brief | patterns match harness result/error events only |
| reel gen 3 | 5 h wall loses the whole build; 42 "shell" exits with no result | short per-phase walls + checkpoints; a missing `result.json` = phase failed with the stream tail captured, resumable once |
| reel gen 3 | 187-item untriaged inbox blocks finishes | triage is bounded per order and never gates a version |
| reel gen 3 | brief prints all 465 rules | scoped brief, superseded rules excluded |
| reel | bare filenames collided across rows | `<slug>_vNNN` everywhere, reserved at intake |
| reel | operator chats raced the worker on one row | one running order per plan; chat and comment runner share the `agent` slot |
| reel | audit/fix loops repeated whole rounds | one critic, one fix round, then a needs_you card |
| reel | self-certification without measuring at the cited frame | intake gives frames per note; the critic is blind; every fix needs a proof |
| reel | TCC / tmux / typed launch command wedges | plain subprocesses with process groups, no tmux, no typing into shells (the studio is started by the user, so it inherits their permissions) |
| reel | OAuth refresh races between lanes | `agent_slots` default 1; one agent at a time per machine |
| v023 | "it was better before" regressions (v020 supportive cut) | lockcheck + praise locks + restore flag forces lock-checked restore |
| v023 | render died at segment 97 under load 260 | governor heavy slots + resumable segment cache (already there) |
| v023 | stale graphics shipped because of a cache key bug | lock/changed-spots sheets make it visible; extender's tests cover cache keys |
| v023 | review links didn't load | link check gate (206) |
| v023 | multi-hour sessions, 5 compactions | memory moved to brain files; short fresh roles |
| v023 | notes "couldn't place" left for chat | questions as numbered buttons, a ruling recorded, a `continue` order |

---

## 11. Build order (each step shippable and tested, `pytest -q` green)

1. **Bus + config.** `factory/bus.py` (orders, events, fold, closed states, atomic writes, flock),
   `config.py` "factory" defaults. Tests cover fold, debounce/fold, version reservation and restart
   resume.
2. **`agent.run_role()` + fake agents.** A stub `claude`/`codex` script in `tests/fixtures/` (set via
   `CUTROOM_CLAUDE_BIN`) that replays canned stream-json, including refusal, limit, timeout and
   no-result cases. Tests cover the schema output, refusal → backoff, limit → not_before, timeout →
   kill, and harness-only pattern matching.
3. **Governor `agent` kind.** Slots, memory cap, emergency stop order. Tests run alongside the existing
   governor tests.
4. **Deterministic intake.** `mapnotes.py` + `brain.py` (decisions fold, locks). Tests build a
   synthetic plan where a comment window covers 2 pieces, a range comment, the restore/praise/
   conditional flags, and a supersede.
5. **Machine QA phase.** `lockcheck.py` (plan level first, render-level PSNR next), the changed-spots
   sheet, link check, drift, and wiring to `cutroom qa` / `ref compare`. Tests use lavfi footage.
6. **Runner + the S path end to end.** intake → producer → editor → render → machine-QA → integrate,
   with the fake agent. `cutroom factory run|status|stop|pause|resume`. Acceptance: a fake editor edits
   one segment, and v+1 appears with a card and comment replies. Killing the runner mid-render and
   restarting resumes at render.
7. **Studio wiring.** Routes, SSE, the `FactoryRunner` thread, "Send to factory" + pills, the header
   light, the Factory dock tab (stepper, roles, ledger). Extend `studio_web/dev/ui_smoke.py`, and use
   `mock_server.py` for the UI.
8. **Critic + fix round + gates.** findings schema, the read-only hash guard, one fix round, the
   integrate gates (§6). Tests: a HARD finding blocks ready until a proof exists; a critic that writes
   a file fails the phase.
9. **Review workspace + verdicts.** The Review tab (two-up vs previous/reference, section chips,
   version themes, the card, the changed-spots sheet, story strip), APPROVE (deterministic, with
   conditional parsing), REJECT, NOTES, and the `/review` phone page. Questions as buttons + `answer`.
10. **Brain + forensics.** The forensics role on top of `cutroom ref`; `REFERENCE.md` / `ref_pairs.json`;
    the Brain section in the Project panel; the team-sizing rules (§3.3).
11. **Learning loop.** Harvest, the lesson-keeper role, global/project scopes seeded from
    EDITING_RULES.md, `lint.py` + `checks.json`, scoped briefs, `lessons_sha` on cards, and the
    Lessons tab. Grader buttons → `grades.jsonl` → lint.
12. **Variants.** Excerpt renders, tiles, Keep/Kill.
13. **Extender.** Worktree sub-orders, pytest gate, apply card, follow-up revise.
14. **Acceptance benchmark: replay v022 → v023.** Feed the 11 operator notes of the v023 round as
    comments against the v022 plan, rebuilt as a cutroom plan, and run the factory. Pass when:
    - at least 9 of 11 notes are resolved the way the real v023 resolved them (supportive restore,
      "hot" lines trimmed, two callouts removed, caption words fixed, dead air split, slow-mo removed,
      the 7:19 walk removed, the 8:23 shot swapped, the ban animation removed);
    - the remaining notes become questions (the real session asked about two);
    - the lock check shows no other changes;
    - total wall time is under 45 min on a 16 GB machine.

    Repeat for the v021 → v022 round (14 notes, mostly M-size).
15. **Docs + portability.**
    - `docs/FACTORY.md` (this file, trimmed to as-built) and the STUDIO.md route table;
    - a CLAUDE.md section "Factory roles";
    - `cutroom doctor` checks (agent login, agent slots, git for the extender, free RAM);
    - a "first run" note: the factory uses the person's own Claude Code **or** Codex login, needs no
      API keys and no launchd, and stops when the studio stops unless `cutroom factory run` is used.

---

## 12. Open questions for the operator (one line each)

1. Should the critic always run, even for 1–2 trivial notes? 1 = only for 3 or more notes (default) /
   2 = always.
2. Should a feature the extender built be applied without asking? 1 = ask every time (default) /
   2 = apply when tests pass.
3. Should the per-version budget cap be lower than the default notional $15? 1 = $15 / 2 = $5 /
   3 = no cap.
4. Should an approved version also get a full-quality render? 1 = render automatically at full
   quality / 2 = only when Export is pressed.
