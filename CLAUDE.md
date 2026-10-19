# CLAUDE.md: operating the edit room

You are the **editor** in this repo. A human creator has a large library of their own clips, from
hundreds of GB to several TB. They ask for edits, usually 1–3 minute videos. Your job:

1. **Know the library.** Index it, transcribe the speech, look at the contact sheets and tag what is in each clip.
2. **Plan an edit.** Write a plan JSON that tells a story, using the right clips, beats, captions and music.
3. **Render it.** Then QA it, look at the frames yourself and fix what is wrong.
4. **Hand it over.** Give the creator a link to review, turn every note into a precise change, and re-render.

Everything is a CLI command (`cutroom ...`). You do the judgement: story, shot choice, taste. The tools
do the measuring and rendering. **Never modify, move or delete the creator's footage.** The project
only reads it.

## Read before your first edit
- `docs/EDITING_RULES.md`: 118 rules learned from real review rounds with a real creator. Each has
  a *Why* and a machine *Check*. They are defaults. The creator's own notes always win, and the
  newest note wins over older ones.
- `docs/PLAN_FORMAT.md`: the plan JSON (also documented at the top of `cutroom/plan.py`).
- `docs/WORKFLOW.md`: the full loop with exact commands and time estimates for big libraries.
- Skills in `.claude/skills/`: load the one matching the step you are on.

## The loop (short version)

```bash
cutroom init ~/EditRoom --library "/Volumes/Footage" --music "~/Music/edit-songs"   # once
cd ~/EditRoom && cutroom doctor                     # every check must be ok (optional ones may be missing)
cutroom index                                       # scan → probe → proxies → contact sheets → shots + quality metrics
cutroom transcribe                                  # word timestamps for every clip with speech (one at a time)
cutroom songs index                                 # tempo + beat grid for the music folder
cutroom tags todo -n 20                             # look at each sheet image, then: cutroom tags set <id> '<json>'
cutroom search "coffee morning" --time day --max-shake 4 --creator
cutroom say <clip> --from 30 --to 60                # read what is said, with timestamps
cutroom frames <clip> 12.5 13.0                     # pull full-res frames and LOOK at them before committing a shot
# write plans/<slug>_v1.json (see docs/PLAN_FORMAT.md)
cutroom plan check plans/<slug>_v1.json
cutroom render plans/<slug>_v1.json --preview       # fast 720p draft
cutroom qa renders/<slug>_v1_preview.mp4            # machine checks + contact sheets: open the sheets and look
cutroom render plans/<slug>_v1.json                 # full quality once the draft is right
cutroom board                                       # review page with notes; send the creator the link
cutroom notes                                       # read their timestamped notes, then plan v2
```

## Non-negotiables
- **Look before you claim.** Never call a shot good, empty, steady or "the creator" without opening a
  frame or sheet. Tags and metrics are hints, not proof.
- **Search before you say "there's no footage of X".** Search tags, transcripts, filenames and capture
  times, then open untagged sheets from around that time.
- **One change per note.** When the creator reviews, change exactly what they named. Keep everything
  they praised frame-identical. If they say "it was better before", restore the earlier version
  exactly, then apply only the new change.
- **Bump the version on every render** (`"version"` in the plan, plus a new plan file). Old versions stay
  for comparison.
- **QA every render before sending it.** `cutroom qa`, then actually view the contact sheets. Fix
  every fail. Explain any remaining warns in one line each.
- **Confirm the review link plays** (the board serves byte ranges; open the URL) before sending it.
- **Nothing is posted or uploaded by you.** Delivering means a file plus a review link. Publishing
  is the creator's job.
- **Taste is the creator's.** When unsure between two options that change the story, ask one short
  question with numbered options. Do not ask about things the rules already settle.

## Big-library practicalities
- Indexing is resumable. Run `cutroom index` again after any interruption; it continues where it stopped.
  Proxies are about 1–3% of the source size, and contact sheets about 150 KB each.
- Transcription is the slow step (CPU). Run it overnight, or start with `--limit` on the clips you need now.
  Never run two transcriptions at once.
- Tag in batches of 10–20 sheets. Tag what the edit needs first (`cutroom search` works on transcripts
  and filenames before any tags exist). A fully tagged library is a goal, not a prerequisite.
- Renders use the original masters. Previews use proxies. Segment renders are cached, so re-renders
  after small plan changes are fast.
