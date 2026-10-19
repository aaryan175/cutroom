# Plan format

A plan is one JSON file in `plans/`. You write it and `cutroom render` reads it. The canonical schema is
in the docstring at the top of `cutroom/plan.py`; this page explains it with examples.

**Rules of thumb**
- Times are **seconds in the source clip** (`in`, `out`, `t`), except where a field says it is relative to
  the segment (`at` on cutaways and overlays).
- Clips can be referenced by id (`3f9a01bc2e`), filename fragment (`C0042`), relative path or absolute path.
  If a fragment matches more than one clip, `plan check` lists them.
- Every new version gets a new file (`trip_v3.json`) and `"version": 3`. Never overwrite an approved version.

## Skeleton

```json
{
  "title": "Weekend trip",
  "version": 1,
  "output": {"width": 1920, "height": 1080, "fps": 25},
  "style": {
    "captions": {"enabled": true, "size": 54, "position": "bottom", "max_words": 5},
    "grade": {"lut": "", "normalize_exposure": true},
    "voice": {"clean": true, "atten_lim_db": 20},
    "music_under_speech": "mute"
  },
  "sections": [ ... ]
}
```

For vertical (9:16) output, set `"output": {"width": 1080, "height": 1920, "fps": 30}`. Framing uses
center/face crops automatically.

## Sections
A section is a chapter with one song:

```json
{"name": "cold open",
 "music": {"song": "summer-drive", "offset": 41.2, "gain_db": -2, "duck_db": -18},
 "segments": [ ... ]}
```
`offset` is where in the song this section starts. Pick it from `cutroom beats <song>`; usually it's a
drop or a downbeat. A section without `music` has no music.

## Segment types

**talk**: someone speaking. Pauses are removed, captions are added, and other clips can cover it.
```json
{"type": "talk", "clip": "C0107", "in": 12.4, "out": 41.0, "tighten": true, "gap": 0.35,
 "cutaways": [{"clip": "C0112", "in": 3.0, "at": 6.5, "dur": 2.8}],
 "auto_cover": true,
 "overlays": [{"kind": "callout", "text": "6 AM", "at": 0.4, "dur": 2.2, "position": "auto"}],
 "note": "sets up the day"}
```

**broll**: footage with music or natural sound.
```json
{"type": "broll", "clip": "C0131", "in": 4.0, "out": 7.2, "audio": "music", "speed": 1.0}
```

**montage**: fast shots cut on the beat.
```json
{"type": "montage", "beat_sync": true, "beats_per_cut": 1, "min_shot": 0.4, "max_shot": 1.2,
 "shots": [{"clip": "C0140", "in": 2.0}, {"clip": "C0151", "in": 10.5}, {"clip": "C0163", "in": 0.8}]}
```

**matchcut**: A ends exactly at `a.t` and B starts exactly at `b.t`. For framing matches and time jumps.
```json
{"type": "matchcut", "a": {"clip": "C0201", "t": 14.2}, "b": {"clip": "C0388", "t": 3.1},
 "pre": 0.8, "post": 1.6}
```

**title / black / still**
```json
{"type": "title", "text": "Day 2", "sub": "", "dur": 1.6}
{"type": "black", "dur": 0.5}
{"type": "still", "clip": "C0150", "t": 8.0, "dur": 1.5, "zoom": 1.06}
```

## Common optional fields
`speed`, `zoom`, `crop` (`"center"`, `"face"` or `[x, y, w, h]` fractions), `sfx`
(`[{"name": "whoosh", "at": 0.0, "gain_db": -8}]`; built-ins are whoosh, hit, tick and riser), `note`.

## Overlays
`kind` is one of `callout`, `lower_third`, `title` or `label`. `position` is one of `auto`, `top-left`,
`top-right`, `bottom-left`, `bottom-right` or `center`. `auto` places the overlay away from faces.
Keep the text short. Creators usually want at most one overlay per talking section.

## What render produces
- `renders/<slug>_v<N>[_preview].mp4`, the video.
- `renders/<slug>_v<N>[_preview].json`, the resolved timeline: every atomic piece with timeline
  start/end, source clip and in/out, captions shown and overlays. QA and the review loop use it to map
  "the clip at 1:45" to a segment.
