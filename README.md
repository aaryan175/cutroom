# cutroom

**An edit room run by Claude Code.** Point it at a drive of your own footage, from hundreds of GB to
several TB, and ask for an edit: *"a 2-minute day-in-the-life from last weekend, fast, with captions,
on this song."* Claude works the way an editor does, and the tools here make that possible:

- **Indexes the library.** Proxies, contact sheets, shot boundaries, shake, exposure, sharpness and
  faces for every clip.
- **Transcribes** every clip with speech, with word-level timestamps.
- **Looks at the footage** through contact sheets and full-res frames, and tags what's in each clip.
- **Writes an edit plan** (JSON): talking segments with the pauses tightened out, b-roll cutaways,
  montages cut to the beat, match cuts, captions, minimal graphics and music sections.
- **Renders it** with ffmpeg:
  - frame-exact segments
  - the house grade, with an optional LUT for log footage
  - cleaned dialogue
  - music ducked under speech
  - loudness normalised to −14 LUFS / −1.5 dBTP
- **QA's its own output.** Silence holes, frozen or black frames, off-beat cuts, repeated shots,
  captions over faces and shaky shots, plus contact sheets at every cut for Claude to look at.
- **Serves a review page.** You watch and scrub on your phone, leave timestamped notes, and Claude
  turns each note into the next version.

The taste comes from `docs/EDITING_RULES.md`: 118 rules learned over many review rounds with a real
creator. Every rule has a *Why* and a machine-checkable *Check*.

## Quick start

```bash
git clone <this repo> cutroom && cd cutroom
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[asr,voice]'
brew install ffmpeg        # or apt/dnf install ffmpeg
```
Then open Claude Code in this folder and paste `BOOTSTRAP_PROMPT.md`, filling in your footage folder.
Claude does the rest: init, index, transcribe, tag, plan, render, QA, review.

To drive it by hand instead:
```bash
cutroom init ~/EditRoom --library /Volumes/Footage --music ~/Music/songs
cd ~/EditRoom
cutroom doctor && cutroom index && cutroom transcribe && cutroom songs index
cutroom search "beach sunset" --creator --max-shake 4
cutroom render plans/trip_v1.json --preview && cutroom qa renders/trip_v1_preview.mp4
cutroom board
```

## How it fits together

```
footage (read-only) ──► index ──► library.db + proxies + contact sheets + transcripts
                                         │
                       Claude looks, tags, searches, reads transcripts
                                         ▼
                                 plans/<edit>_vN.json
                                         │
                     render ──► renders/<edit>_vN.mp4 + timeline sidecar
                                         │
                       qa ──► report + contact sheets ──► Claude fixes
                                         │
                     board ──► creator watches, leaves notes ──► vN+1
```

| Path | What |
|---|---|
| `CLAUDE.md` | operating manual for Claude (loaded automatically) |
| `.claude/skills/` | indexing, tagging, planning, render-and-qa and review-loop skills |
| `docs/EDITING_RULES.md` | the rulebook |
| `docs/PLAN_FORMAT.md` | the edit-plan JSON |
| `docs/WORKFLOW.md` | the end-to-end loop, timings, hardware notes |
| `cutroom/` | the Python package (CLI: `cutroom --help`) |
| `examples/` | an example plan |
| `tests/` | synthetic-footage tests (`pytest`) |

## Requirements
- Python 3.10+ and ffmpeg (with libx264).
- macOS, Linux or Windows (WSL).
- For transcription, `faster-whisper`. It runs on CUDA if available, otherwise CPU.
- For best voice cleaning, `deepfilternet`. An ffmpeg fallback is built in.
- Optional: Chrome or Chromium for HTML motion graphics.
- Disk for the project: about 3% of the library size plus about 20 GB for render caches.

## What it doesn't do
- **It never edits, moves or deletes your footage.** It only reads it.
- **It never posts anything anywhere.** You get files and a review page.
- **No fonts, LUTs or music are bundled.** Use your own. Fonts default to free system fonts.

MIT licensed.
