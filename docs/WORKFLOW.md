# Workflow: from a drive full of clips to a reviewed 2-minute edit

## 0. Setup (once, about 15 min)
```bash
git clone <this repo> cutroom && cd cutroom
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[asr]'          # add '.[voice]' too for DeepFilterNet voice cleaning (recommended)
brew install ffmpeg              # macOS; on Linux use your package manager
cutroom init ~/EditRoom --library /Volumes/Footage --music ~/Music/edit-songs
cd ~/EditRoom && cutroom doctor
```
Optional: a Chrome or Chromium install enables the HTML motion-graphics renderer. Without one, a simpler
Pillow fallback is used.

## 1. Index (hours; unattended, resumable)
`cutroom index`, then `cutroom transcribe`, then `cutroom songs index`.
For a 2 TB library, start these in the evening. `cutroom status` shows progress at any time.
Or run all of it, plus Claude tagging of every contact sheet, unattended and within safe resource limits:
`cutroom overnight --until 07:00` (see `docs/OVERNIGHT.md`).

Want to edit tonight? Index everything (fast), then transcribe just what you need:
`cutroom search "<topic>"` works on filenames immediately; then run `cutroom transcribe --clip <id>`.

## 2. Understand the library (ongoing)
- `cutroom tags todo` → look at the sheets → `cutroom tags set ...` (see the footage-tagging skill).
- `cutroom matchcuts` once, to collect match-cut candidates.

## 3. Brief → plan (about 20–40 min of operator time per edit)
The creator says what they want. Find material with `search` / `say` / `frames`, write
`plans/<slug>_v1.json` (see the edit-planning skill and `PLAN_FORMAT.md`), then run `cutroom plan check`.

## 4. Render → QA → fix (minutes)
```bash
cutroom render plans/<slug>_v1.json --preview
cutroom qa renders/<slug>_v1_preview.mp4      # then OPEN the contact sheets
cutroom render plans/<slug>_v1.json           # final, from masters
```

## 5. Review
`cutroom board` serves `renders/` with a player, scrubbing, notes and side-by-side compare. To share it
off the machine, use any tunnel or VPN you trust (a Tailscale address, `cloudflared tunnel`, or just
send the file). The creator leaves timestamped notes; `cutroom notes` prints them. Then follow the
review-loop skill and make v2.

## Hardware notes
- **Proxies use the hardware encoder** (VideoToolbox on Mac, NVENC on NVIDIA). That keeps a 2 TB index
  to an evening rather than days.
- **Transcription is CPU/GPU heavy.** On NVIDIA it uses CUDA automatically. On a Mac it uses int8 on CPU,
  around 1–4× realtime with the medium model. Use `--model small` to go faster with slightly worse
  accuracy.
- **Renders run segments in parallel** (`workers` in `cutroom.json`; default is half your cores). Set
  `CUTROOM_MAX_JOBS=2` on a busy machine.
- **To render on another machine,** set `"remote_render"` in `cutroom.json` to a command prefix (for
  example `ssh render-box`). The paths must be identical on both machines (a shared mount).
