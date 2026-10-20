---
name: multicam
description: Use when the footage has several cameras and/or a separate audio recorder (events, interviews, shows, podcasts), when the creator says "use the other angle", "switch to the wide", "the sound from the Zoom", or asks for the edit to go to Premiere / Resolve / Final Cut. Covers syncing, reading groups, cutting angles in a plan, the angle-change rules, sound choice and the NLE hand-off.
---

# Multicam: sync, cut between angles, hand off to the NLE

Everything is in `docs/NLE_AND_MULTICAM.md`. This is the working order.

## 1. Sync (once per shoot; the overnight `sync` stage already does it)
```bash
cutroom sync                    # registers recorder WAVs, reads camera metadata, aligns by sound
cutroom sync list               # groups: angle codes, offsets, drift, confidence, loop error
cutroom sync devices            # which camera is camA / camB, which recorder is rec1
```
- Read the list before cutting. A group needs every camera you expect. A loop error under one frame (40 ms at 25p) is
  normal: they are ~0.1 ms when sync is right.
- A camera missing from a group? It probably had no usable sound or a far-off clock. Try
  `cutroom sync --clips <cam clip> <other clip>`. If it still fails, say so: never line it up by eye in a plan.
- Name the devices the way the creator talks about them: `cutroom sync name camA "Cam A wide"`. Mark a lav with
  `--kind lav`. Mark jam-synced timecode with `--tc-sync on`.

## 2. Look before you cut
`cutroom frames <clip> <t>` on each angle at the same moment (`cutroom sync map G T --from camA --to camB` gives
the times). Know which angle is the wide, which is the close-up and which is the roamer. Note who is in frame and
whether focus holds.

## 3. Write multicam segments
```json
{"type": "talk", "clip": "<camB clip id>", "in": 812.4, "out": 828.0,
 "multicam": {"group": 3, "angle": "camB", "audio": "auto"}}
```
- `audio: "auto"` takes the external recorder over a lav over the camera. Keep it unless the recorder is clipped,
  missing or noisier (then `"camera"` or another source code).
- To switch angles, cut the talk into consecutive segments with a different `angle` each. The renderer maps `in` and
  `out` through the group, so the words stay continuous across the angle change. Rewrite with
  `cutroom sync switch PLAN SEC.SEG ANGLE` (or the Studio's Inspector buttons), so `clip`, `in` and `out` match the
  angle.
- Keep `tighten` on talk as usual: the jump cuts come from the recorder's words when it has a transcript.
- `cutroom plan check` tells you when an angle doesn't cover the range (that camera wasn't rolling). Pick another angle
  there.

## 4. Angle-change rules (defaults; the creator's notes win)
1. **Cut on speech emphasis.** Change angle on the stressed word, the start of a new sentence or a punchline. Never
   mid-word, never in the middle of a breath.
2. **Wide for reactions and context.** Laughter, applause, a room turning to look, a new person walking in: go to the
   wide (or the crowd camera). Come back to the close-up for the next important line.
3. **Close-up for the point.** The key claim, the emotional line, the name-drop: on the close angle.
4. **Hold at least ~2 s per angle.** Switch no more often than about every 2 s, unless the pace demands it (a hype
   montage, a fast back-and-forth between two speakers, a cold open). Then match the section's ASL.
5. **Don't ping-pong.** Avoid A-B-A within 4 s. Each switch should show something new: a reaction, a gesture, a
   different person.
6. **Follow the speaker.** In a two-person conversation, the angle on whoever speaks is the default. Cut to the listener
   only for a real reaction.
7. **Bad frames lose.** Out of focus, someone blocking the lens, the camera moving between positions: use another angle
   for those seconds, even if it breaks rule 4.
8. **Sound never jumps.** The sound stays on the best source across angle changes (the recorder). Only switch sound
   when that source is missing or broken for that range.
9. **Cutaways still work.** B-roll and cutaways go over a multicam talk segment like any other talk segment.

## 5. Hand-off to Premiere (or Resolve / Final Cut)
```bash
cutroom export-nle plans/<slug>_vN.json --format premiere --format fcpxml --zip
cutroom export-nle --verify renders/nle/<slug>_vN      # must say OK before you give it to the creator
```
- Give the creator the folder or zip path and the three steps from `README_PREMIERE.txt`: import, relink if needed, LUT
  on an adjustment layer.
- The main sequence already cuts between angles. The "MC <group>" source sequences let them re-cut by hand in
  Premiere's multi-camera view.
- For full-quality rendered effects (cards, burns, frames), add `--clean-render` or render the final first.
- Say plainly that the Premiere import itself has not been tested on real Premiere. `--verify` checks the files
  against the plan, frame by frame.
