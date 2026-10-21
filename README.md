# cutroom

**An edit room run by your own AI: Claude Code or OpenAI Codex.** It uses the login you already have
(your Claude or ChatGPT subscription). No API keys. Point it at a drive of your own footage, from
hundreds of GB to several TB, and ask for an edit: *"a 2-minute day-in-the-life from last weekend, fast,
with captions, on this song."* The AI works the way an editor does, and the tools here make that
possible:

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
  captions over faces and shaky shots, plus contact sheets at every cut for the AI to look at.
- **Delivers to every platform** (`cutroom export`): YouTube master, face-tracked 9:16 for Reels /
  TikTok / Shorts, 1:1 and 4:5, each at its platform's resolution and loudness; `.srt` / `.vtt`
  captions, YouTube chapters, thumbnail candidates with a contact sheet, and 15 / 30 / 60 s cutdowns
  built as new plans the AI can refine.
- **Scales to multi-TB libraries.** Duplicate and near-duplicate collapse, a quality gate before AI
  tagging, highlight moments (applause, laughter, motion peaks), capture-time shoots and GPS places,
  best-take ranking for repeated lines, a fast-then-accurate two-pass transcription option, ASR on
  another machine, and `cutroom estimate` to see the time and disk a library needs before starting.
- **Serves a review page.** You watch and scrub on your phone, leave timestamped notes, and the AI
  turns each note into the next version.
- **Has a browser editor** (`cutroom studio`): a timeline, source and program monitors, markers, and
  your AI as a chat panel that sees what you point at.
- **Grows with you.** Ask for something it can't do yet and your AI extends the tool itself
  (`docs/ARCHITECTURE.md`).

The taste comes from `docs/EDITING_RULES.md`: 133 rules learned over many review rounds with a real
creator. Every rule has a *Why* and a machine-checkable *Check*.

## Quick start

```bash
git clone <this repo> cutroom && cd cutroom
```
Then open **Claude Code** (`claude`) or **Codex** (`codex`) in this folder and paste the matching prompt
from `BOOTSTRAP_PROMPT.md`, filling in your footage folder. Your AI does the rest: it installs
everything (venv, Python packages, ffmpeg), creates the project, runs `cutroom doctor` and fixes what's
missing, opens the Studio and gives you the link, then offers to "understand your library" overnight
with a time estimate measured on your machine. From then on you talk to it in the Studio's chat panel
or in the terminal.

Which AI the Studio chat and the overnight tagging use: `"agent"` in the project's `cutroom.json` is
`"auto"` (whichever is logged in; Claude Code when both are), `"claude"` or `"codex"`. `cutroom doctor`
shows what is installed and logged in.

To drive it by hand instead:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[asr,voice]'     # Apple Silicon: '.[asr,mlx,voice]' for GPU transcription
brew install ffmpeg        # or apt/dnf install ffmpeg
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
                     the AI looks, tags, searches, reads transcripts
                                         ▼
                                 plans/<edit>_vN.json
                                         │
                     render ──► renders/<edit>_vN.mp4 + timeline sidecar
                                         │
                       qa ──► report + contact sheets ──► the AI fixes
                                         │
                     board ──► creator watches, leaves notes ──► vN+1
```

| Path | What |
|---|---|
| `CLAUDE.md` | operating manual for the AI (Claude Code loads it automatically) |
| `AGENTS.md` | the same for Codex: points it at `CLAUDE.md`, the skills and `docs/ARCHITECTURE.md` |
| `BOOTSTRAP_PROMPT.md` | the paste-in prompts (Claude Code and Codex) that set everything up |
| `.claude/skills/` | indexing, tagging, planning, render-and-qa, review-loop, reference-matching, deliverables-and-scale, event-recap and extending-cutroom skills |
| `cutroom recap` | event recap mode: stage / crowd / wide detection, energy peaks and soundbites (`cutroom detect events`), then a draft hype-recap plan (`event-recap` skill) |
| `docs/ARCHITECTURE.md` | map of the code for the AI that extends the tool |
| `docs/STUDIO.md` | the browser editor and its API, including the Claude Code / Codex bridge |
| `docs/OVERNIGHT.md` | the unattended "understand my library" run and the resource governor |
| `docs/EDITING_RULES.md` | the rulebook |
| `docs/PLAN_FORMAT.md` | the edit-plan JSON |
| `docs/WORKFLOW.md` | the end-to-end loop, timings, hardware notes |
| `cutroom/` | the Python package (CLI: `cutroom --help`) |
| `examples/` | an example plan |
| `tests/` | synthetic-footage tests (`pytest`) |

## Requirements
- Claude Code or OpenAI Codex CLI, signed in with your own account (subscription). No API key.
- Python 3.10+ and ffmpeg (with libx264).
- macOS, Linux or Windows (WSL).
- For transcription, `faster-whisper`. It runs on CUDA if available, otherwise CPU.
- On Apple Silicon Macs, add the `mlx` extra (`pip install -e '.[asr,mlx,voice]'`): transcription then runs on
  the GPU with MLX Whisper (`asr_backend: "auto"` picks it for `medium` and larger; `cutroom doctor` shows which
  backend is active).
- For best voice cleaning, `deepfilternet`. An ffmpeg fallback is built in.
- Optional: Chrome or Chromium for HTML motion graphics.
- Disk for the project: about 3% of the library size plus about 20 GB for render caches.

## What it doesn't do
- **It never edits, moves or deletes your footage.** It only reads it.
- **It never posts anything anywhere.** You get files and a review page.
- **No fonts, LUTs or music are bundled.** Use your own. Fonts default to free system fonts.

MIT licensed.
