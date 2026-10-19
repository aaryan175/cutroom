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

## 6. Deliver
```bash
cutroom export renders/<slug>_vN.mp4 --preset all --thumbs        # every platform + captions + thumbnails
cutroom export renders/<slug>_vN.mp4 --preset reels --cutdown 30   # a 30 s cut as a face-tracked 9:16
cutroom cutdown plans/<slug>_vN.json --len 15,30,60                # just the cutdown plans, to refine first
```
Everything lands in `renders/exports/<render name>/`: one file per preset (`youtube`, `youtube_4k`,
`reels`, `tiktok`, `shorts`, `square`, `feed`), `.srt` and `.vtt` at the exact heard times, a YouTube
`chapters.txt` from the plan's sections, `thumbs/thumb_01..06.jpg` + `thumbs/sheet.jpg`, and `export.json`
with the measured size, frame rate, duration and loudness of each file.
- Export from the **final** render: a 9:16 crop of a 720p preview is upscaled about 2.7× (the manifest
  warns). Thumbnails come from the masters when the sidecar can locate them, so they are full resolution
  either way.
- The vertical versions are reframed from the 16:9 edit: the crop follows the face in every shot
  (static when the person barely moves, never cutting a detected head), follows the picture's detail when
  nobody is in frame, and letterboxes title cards and shots with a graphic over a blurred copy. Captions
  are re-set inside each platform's safe area from a caption-free render of the same plan.
- Frame rates are kept (platforms take 24–60 fps); `--fps 30` forces a conform.
- Cutdowns keep the cold open, pick the strongest lines (stakes words, highlight score, length) in story
  order and end on the last line. **Look at the cutdown before sending it**: it is a first draft plan.
- Nothing is uploaded anywhere. Look at the 9:16 frames and the thumbnail sheet before sending.

## Big libraries
Run `cutroom estimate` (or `cutroom estimate --tb 4`) before the first overnight run on a multi-TB
library; see "Big libraries" in `docs/OVERNIGHT.md` for proxy, transcription and tagging settings.

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
