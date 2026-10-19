---
name: reference-matching
description: Use when the creator gives a reference video ("make it like this", "same pacing as this", "copy this intro", a link or a file, or anything in renders/reference/) to measure it frame by frame, decide what to match, rebuild it from the creator's own footage, and prove how close the render is with a side-by-side comparison.
---

# Matching a reference video

Creators work from references. "Make it like this" means its cut map, the pace of each section, the
cold-open structure, where the cuts sit on the music, the caption look, how much is talking versus
b-roll, the colour and brightness, and the loudness. The biggest wins have come from **measuring the
reference frame by frame and rebuilding it one-to-one** (EDITING_RULES 45, 101), not from "inspired by".
Guessing loses: a loosely inspired teaser was rejected, and the one-to-one rebuild was accepted.

## 1. Add and measure it
```bash
cutroom ref add <file-or-link> --title "short name"   # copies it into refs/<id>/ and measures it (resumable)
cutroom ref list                                       # anything in renders/reference/ shows up here too
cutroom ref show <id>                                  # the summary + the contact-sheet paths
cutroom ref cutmap <id>                                # one row per shot: start, length, kind, beat, text, luma
```
- Links need `yt-dlp` installed. If it isn't, say so in one line and ask for the file.
- A 10-minute reference takes a few minutes (most of it is speech-to-text). Re-running continues where it stopped.
- In the studio, `POST /api/refs` does the same as a job. The overnight run measures any reference that
  is not measured yet.

## 2. Read it before deciding anything
**Measure before you guess.** Open every `sheet_*.jpg` that `cutroom ref show` lists (one frame per shot,
so both sides of every cut sit next to each other), then read the cut map. Check what the numbers claim:
- **Kinds are estimates.** `talk` = a large face plus speech. `title` = text on a flat frame. `flash` =
  a 1–2 frame shot. A low-confidence `talk` is a long shot with continuous speech but no face found.
  Confirm each one on the sheet.
- **Cuts**: if a sheet shows two tiles of the same picture side by side, that cut is a false one (a
  whip pan or a flash inside a shot). If one tile clearly holds two different pictures, a cut was missed.
  For anything rule-45 exact, pull the frames: `ffmpeg -ss T -i refs/<id>/proxy.mp4 -frames:v 1 x.jpg`.
- **Sections** are change points in pace, speech, talking-head share and loudness. Rename them in your
  own head by what happens ("teaser", "morning talk", "drive montage").
- **Beat sync** counts as meaningful only well above `chance_pct`. Music presence is an estimate from
  rhythm.
- **Captions**: `method: ocr` read real words (tesseract). `heuristic` only knows that text-like strokes
  are there. `outline: true` means a dark ring around the glyphs. Never copy that (rule 53).

## 3. Decide what to match, and say it in one line
Ask with numbered options only if it's genuinely unclear:
1. **One-to-one** (rule 45): the same cut clock, shot by shot. Use it for cold opens, teasers and short
   pieces the creator called out.
2. **Same feel**: the same pace per section (rule 101: within ±30% of each section's ASL), the same cold-open
   length and structure, the same talk / b-roll / montage balance, the same caption band and size, and
   the same loudness. Use it for long-form ("make my vlog like this").
Most long requests are 2, with 1 for the opening.

## 4. Map the reference onto the creator's footage
For each reference shot (one-to-one) or section (feel):
- Use **the same kind of shot**: talk ↔ the creator talking, b-roll ↔ b-roll of the same scale and energy,
  and a montage gets fast shots of the same length. Search with `cutroom search` and
  `cutroom clip <id> --frames 6`, and look before you commit.
- Keep **the same length and position.** Plan montage shot lengths from the cut map
  (`max_shot`/`min_shot`, `beats_per_cut`). Where the reference cut lands on a beat, set `beat_sync` and let
  the beat grid place it. Keep flashes as 1–2 frame shots or `still` segments.
- **Captions** go in the same band at the same size. `captions.size_pct` is the glyph height as a share of
  the frame. `approx_plan_size_1080` is the matching plan `style.captions.size` at 1080p. Use the same words
  per caption (`max_words`) and the same colour.
- **Music**: if the creator says "use its music", identify the song and **start it at the reference's own
  offset** (rule 73). Never put copyrighted audio into a delivery unless the creator chose to. Otherwise
  pick a song from their music folder with the same tempo (the reference's `tempo`) and land its drop
  where the reference's `drops` are.
- **Devices (the reference wins, rule 38)**: the analysis has `devices` (card, burns, frames, titles) and
  every cut-map row lists the devices on its shot. Rebuild each one the reference has, with its measured
  numbers: `cold_open.style: "reference"` does it for intros; elsewhere use `transition.kind: "burn"`,
  `frame` (gate / inset / letterbox), `card` segments and `typed_title` overlays. Don't add any device the
  reference lacks; QA `devices_vs_reference` fails it. Older analyses get devices with
  `reference.ensure_devices(cfg, id)` (no re-analysis).
- **Colour and brightness**: compare `colour.by_section`, but start from the LUT-only house grade (rule 120).
  Never pick a night look for daylight footage; `looks.match` refuses one and caps intensity so the black
  point stays ≥ 15 and saturation within +15 %. If the reference still differs, say so rather than
  stacking curves.
- **Film / lens**: measure it (`cutroom.film.measure_frames` on the reference and on your LUT-only frames),
  then `film.match(ref, ours)` gives a `style.film` with only the traits the reference measurably has more
  of. Compare on full-resolution frames: proxies (540p) are softer and blotchier than the masters, so judge
  the look on a final render.
- Where the creator's footage has nothing that fits, say which reference shot is missing and offer the
  nearest real beat (rule 116). Never fake it.

## 5. Build, render, compare, iterate
```bash
cutroom plan check plans/<slug>_v1.json
cutroom render plans/<slug>_v1.json --preview
cutroom qa renders/<slug>_v1_preview.mp4
cutroom ref compare <id> renders/<slug>_v1_preview.mp4      # rows + aligned sections + gaps in plain words
```
The compare measures your render exactly like the reference (cuts, pace, beats, captions, loudness,
brightness, and the devices: card, burns, inset / gate time, typed title) and lists the gaps, biggest
first, each with a time in your video. Fix the big ones, bump the
version and render again. **Stop when only small gaps are left**, or when the remaining gaps are choices
the creator made (a shorter runtime, no copyrighted song). In the studio, `GET
/api/ref/<id>/compare?plan=<slug>` gives the same against the latest render, or a pacing-only check of the
draft before anything is rendered.

## 6. Show it side by side, every round (rule 110)
Creators judge by eye, so show both at **the same moment**: the studio's Reference tab plays them synced.
In chat, name the reference moment by its time ("the reference at 0:03.8") and give your moment as a
`[[t:<seconds>]]` chip. Then report the gaps that are left in plain words, for example:

> Your cold open is 6.2 s against 3.8 s in the reference [[t:6.2]], so I'd cut the walk shot in half.
> The middle now cuts every 2.1 s, close to its 1.9 s. The music hits the same beats (82% vs 85%). The
> captions sit lower than theirs: same size, but bottom band instead of lower third.

Never paste the compare table, JSON or paths into chat. Say what changed, where, and what is still off.

## Lessons that cost real review rounds
- **Measure before you guess.** Every "it's not the same" note so far was a number nobody had measured:
  cut count in the first 3 s, a section's ASL, cold-open length, or where the song started.
- **One global pace is wrong** (rule 101). Match the pace per section: talking parts are slower and
  montages are much faster.
- **Open fast when the reference does** (rule 46). Match `first3_cuts` and the cold-open length before
  anything else. It is the first thing the creator sees.
- **Use the reference's song offset if they say "use its music"** (rule 73). Guessing tracks or offsets missed
  the feel. Never ship copyrighted audio without the creator choosing to.
- **Side by side every round** (rule 110). Gaps were only obvious that way.
- **Lock what was approved** (rule 106). When matching the next section, the sections the creator already
  liked stay frame-identical.
