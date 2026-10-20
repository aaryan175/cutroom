---
name: deliverables-and-scale
description: Use when an approved edit has to go out to platforms (YouTube, Reels, TikTok, Shorts, square, 4:5), when the creator wants captions files, chapters, thumbnails or 15/30/60 s cutdowns, or when the library is very large (multi-TB) and indexing, transcription and tagging time, disk or cost need planning.
---

# Deliverables and big libraries

## Exports: one edit, every platform

1. Export from the **final** render (`cutroom render plans/<slug>_vN.json` without `--preview`). A 9:16
   crop of a 720p preview is upscaled about 2.7×; the manifest warns about it.
2. Run the export:
   - `cutroom export renders/<slug>_vN.mp4 --preset all --thumbs` for every platform at once.
   - `--preset reels` (or `tiktok`, `shorts`, `square`, `feed`, `youtube`, `youtube_4k`) for one.
   - Add `--cutdown 30` (or `15,30,60`) for short versions.
   - Outputs go to `renders/exports/<render name>/`. `export.json` lists every file with its measured
     size, frame rate, duration and loudness, plus warnings.
3. **Look at the vertical version.** Grab frames from the 9:16 file
   (`ffmpeg -ss T -i FILE -frames:v 1 out.jpg`), at least one per shot, and open them. For each one,
   check:
   - Is the whole head in frame?
   - Is the right person the subject when two people are on screen?
   - Are the captions inside the safe area and off the face?
   - `export.json` → `reframe` lists, per shot, how the crop was decided (`face`, `saliency`, `centre`,
     `fit`). A `centre` shot with a person in it means the face detector missed them. Fix it by
     re-rendering that shot with `"crop": [x, y, w, h]` in a vertical plan, or accept the letterbox.
4. **Look at `thumbs/sheet.jpg`.** The scores favour sharp frames with a face 20–45 % of the height,
   open eyes, good exposure and a subject near a third. They cannot judge flattering expressions or
   private details. Pick or reject by eye (rule: never an unflattering frame of the creator).
5. Captions: `.srt` / `.vtt` are at the exact heard times. Chapters come from the plan's sections.
   YouTube needs at least 3 chapters of 10 s or more; the warnings say when it won't show them. Rename
   sections in the plan when the chapter titles read badly.
6. Nothing is uploaded. Hand the files over (board link or the folder).

## Cutdowns

- `cutroom cutdown plans/<slug>_vN.json --len 30` writes `plans/<slug>_cut30.json`. It keeps the hook
  (the cold open), takes the strongest lines (numbers, superlatives and stakes words, highlight score, a
  clean 1–8 s length) in story order, and ends on the last line (the payoff). The length lands within
  10 % of the target. A plan-level `cold_open` comes along with its teaser lines pinned to the same
  clip/in/out as the full edit (`from: "<section>/<seg>"` references are resolved, since the cut renames the
  sections) and its auto shots frozen; it is dropped when it would take over half the cut.
- **It is a draft.** Read it like any plan:
  - Does the story still make sense without the cut lines?
  - Is there a line that needs its set-up?
  - Is the hook still the best opening?

  Swap lines from `cutroom say <clip>`, add cutaways back (cutdowns drop them), then render and export
  as usual.
- `cutdown.lines` in the plan lists what was picked and why (score), so the creator's "keep the bit
  about X" maps to a concrete line.

## Big libraries (multi-TB)

1. **Estimate first.** Run `cutroom index --skip-proxies --skip-analysis` (scan + probe, minutes), then
   `cutroom estimate`. Use `--tb 4` to project a bigger drive, and `--bench` when nothing has been
   measured on this machine yet. Tell the creator the numbers: hours per stage, how many tagging calls,
   and disk.
2. Settings worth changing for a 4 TB library (`cutroom.json`, details in `docs/OVERNIGHT.md`):
   - `proxy_height: 360` halves the proxy disk. `proxy_bitrate: ""` gives 1.2 Mb/s at 540p and
     0.6 Mb/s at 360p.
   - `asr_fast_model: "small"` gives searchable speech fast; the `upgrade` stage redoes it with
     `asr_model` later.
   - `asr_remote` runs Whisper on a GPU machine over a shared mount.
   - Leave `tag_gate` on.
3. **Start the edit early.** Search or open what the edit needs. Recently used clips go first in every
   overnight stage, so a first edit can start after a few hours instead of after the whole drive.
4. Use the structure the overnight run builds:
   - `cutroom search --highlights "<topic>"` finds moments with applause, laughter, crowd energy or big
     camera moves. Check them on the sheet: loud is not always good.
   - `cutroom search --dedupe ...` shows one clip per duplicate group. `cutroom clip <id>` shows the
     group. Duplicates inherit tags (`"inherited_from"`); correct the representative and re-tag if one
     differs.
   - `cutroom detect sessions` lists the shoots (and GPS places). `cutroom search --session N` lists one
     shoot. This replaces guessing "where was the creator" from file names (rules on location
     continuity).
   - `cutroom search --near <clip>` finds look-alikes: alternative angles, the same spot on another day,
     the cleaner copy.
   - `cutroom takes [--clip X]` lists lines said more than once, with the best take marked. The later
     take wins unless it is incomplete or technically worse. Still listen to both when the line matters.
5. People clustering and semantic (embedding) search are not built. Tags, transcripts and the hashes
   are what there is; say so if the creator asks for "every clip of person X".
