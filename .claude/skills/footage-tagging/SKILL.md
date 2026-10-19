---
name: footage-tagging
description: Use when you need to know what is inside clips by looking at their contact sheets, and record it as tags so cutroom search can find shots. Use it for batch tagging the library or tagging around a specific moment the creator asked for.
---

# Looking at footage and tagging it

Metrics such as shake, exposure, faces and transcripts are automatic. **What is in the shot is only
known once you look at it.** Contact sheets exist for exactly that: one image per clip, a timestamped
tile every few seconds.

## Batch loop
1. Run `cutroom tags todo -n 15`. It lists untagged clips with their sheet paths, any transcript
   snippet and the quality numbers.
2. Open each sheet image. Read the timestamps on the tiles.
3. Write tags with `cutroom tags set <id> '<json>'`. Run `cutroom tags fields` for the schema. Example:
   ```json
   {"summary": "walks out of the house to the scooter, sunny", "setting": "driveway",
    "time_of_day": "day", "people": "creator", "creator_visible": true, "talking_to_camera": false,
    "actions": ["walking", "leaving"], "shot": "wide", "camera": "locked", "mood": ["calm"],
    "quality": 4, "usable": true,
    "best": [{"t": 6.0, "why": "clean walk-through, face visible"}],
    "avoid": [{"t0": 14, "t1": 18, "why": "camera whip + stranger in frame"}],
    "topics": ["leaving", "scooter", "morning"]}
   ```
4. Repeat. Fifteen sheets a batch keeps your judgement sharp. Tag what the current edit needs first.

## What to look for (the things creators reject)
- **Who is in it.** Mark `people` honestly. A stranger as the subject means `usable: false` for any
  slot meant for the creator. A stranger in the background goes in `avoid` with the time range.
- **Empty plates.** These are walls, rooms, scenery or desks with nobody in frame. Tag
  `people: "none"`. They are only usable when the creator asks for a scenery or object insert.
- **Unflattering moments.** Bad light on the face, odd expressions, head in hands, slumped poses,
  faces right up against the lens, cut-off heads. List them as `avoid` ranges.
- **Props and crew.** Mic transmitters, tripods, a camera operator in a reflection, cigarettes or
  vapes when the creator has banned them. List them as `avoid`.
- **Readable private info.** Screens, signs, number plates, documents. List them as `avoid`, or use
  `usable: false`.
- **Shake.** If the sheet looks smeared or the measured shake is high (`cutroom clip <id>`), mark
  `camera: handheld` and `quality` ≤ 2 for walking shots.
- **Best moments.** Clear action, good light, face visible. These are the shots montages and
  cold opens are built from.

## Before using a shot in a plan
Tags come from a sheet; sheets are small. Pull real frames at the exact in-point with
`cutroom frames <id> <t> <t+1>` and look at them at full size before committing the shot.
