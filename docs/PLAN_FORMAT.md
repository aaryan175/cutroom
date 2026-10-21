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
    "captions": {"enabled": true, "size": 32, "position": "bottom", "max_words": 5},
    "grade": {"lut": ""},
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

**multicam** (on talk or broll): picture from one angle of a synced group, sound from the group's best source
(`cutroom sync`; see `docs/NLE_AND_MULTICAM.md` and the `multicam` skill).
```json
{"type": "talk", "clip": "C0101", "in": 812.4, "out": 828.0, "multicam": {"group": 3, "angle": "camB", "audio": "auto"}}
```
- `angle` is an angle code from `cutroom sync list` (or a device name).
- `audio` is one of:
  - `"auto"`: external recorder > lav > camera (among cameras, the angle's own);
  - `"camera"`: the angle's own sound;
  - a source code, such as `"rec1"`.
- With `clip`, `in` and `out` are that clip's seconds. If `clip` belongs to another angle, the times are mapped through
  the group's offsets and drift, so changing only `angle` keeps the moment. Without `clip`, `in` and `out` are group
  seconds.
- `cutroom sync switch PLAN SEC.SEG ANGLE` rewrites `clip`, `in` and `out` for the new angle.
- Tightening and captions use the recorder's words when it has a transcript.
- `plan check` reports when the angle or the sound source doesn't cover the range.

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

**card**: the reference's "COMING UP" card, generated (cutroom/cards.py): yellow `#FBFF09` text in a heavy
rounded soft-serif (Cooper Black if installed, else the closest installed face: Superclarendon, Rockwell,
Georgia; `font` takes a path or a name) at 14.2 % of the frame height, centred, on a near-black crumpled-paper
texture that re-randomises every 2.5 frames. The text scales 87 % → 100 % (ease-out) over 10 frames, then
holds; it leaves on a hard cut or a burn. A riser rides under it (its peak on the card's last frame).
```json
{"type": "card", "text": "COMING UP", "dur": 0.84, "color": "#FBFF09", "size": 0.142, "font": "",
 "scale_from": 0.873, "scale_frames": 10, "boil": 2.5, "sfx": "riser"}
```

## Common optional fields
`speed`, `zoom`, `crop` (`"center"`, `"face"` or `[x, y, w, h]` fractions), `sfx`
(`[{"name": "whoosh", "at": 0.0, "gain_db": -8}]` or `[{"name": "hit", "on": "cut"}]`; built-ins are whoosh,
hit, tick and riser), `transition`, `push`, `ramp`, `conform`, `exposure_stops`, `note` (see Craft below).

## Craft (measured on a reference vlog; all optional, defaults shown)
These fields encode what a finished edit does that a rough cut doesn't. The defaults come from a frame- and
sample-level audit of a reference vlog; change them only when the creator asks.

**Captions.** `style.captions.size` is 43 px at 1080 (text about 3 % of the frame height, as in the reference) and
scales with the output height. Position and the no-outline rule are unchanged.

**Grade (house grade = the LUT only).** The default is the creator's approved look: the log→709 LUT and
nothing after it (`normalize_exposure: false`, `finish.enabled: false`, vibrance 0, saturation 1.0, no
look). The stack that used to run after the LUT (exposure pull, finishing curve, vibrance, saturation, a
look) doubled the saturation and crushed the black point from 33 to 2 on the same frame ("insane"). Each
part stays available as an opt-in:
```json
"grade": {"normalize_exposure": true,
          "exposure": {"strength": 0.5, "max_stops": 1.0, "deadband_stops": 0.33, "target_luma": 118,
                       "night_luma": 60, "night_target": 70},
          "finish": {"enabled": true, "black": 0.028, "shoulder": 0.96, "vibrance": 0.18}}
```
The exposure correction runs in the LOG domain *before* the LUT: half of the measured error, at most ±1
stop. Nothing is ever brightened after the LUT, because that turns blacks milky.

**Camera transforms (mixed cameras, matched to the hero camera).** `style.grade.camera_transforms` is `"auto"`
by default. Every clip whose camera was identified (`cutroom cameras`) gets ONE combined LUT instead of the
plan's LUT: its camera input transform (published log curve + gamut maths, or the manufacturer's LUT), then the
show transform (the hero ARRI's LogC3 → Rec.709 LUT), then the per-camera, per-day match to the hero. It is
still the house grade: one transform, plus at most one look. Clips with no identified camera keep the old path
(`style.grade.lut` / config `lut` on log footage).
```json
"grade": {"camera_transforms": {"show_lut": "arri_logc3.cube", "hero": "arri", "match": true, "matrix": false,
                                "cameras": {"bmd-pocket-cinema-camera-6k-pro": {"lut": "bmd.cube"},
                                            "fuji-x-t5": {"profile": "fuji_flog2"}}}}
```
`"off"` switches it off for one plan. `matrix: true` also fits a full 3×3 (used only when it beats the
white-balance / exposure / saturation fit). See docs/NLE_AND_MULTICAM.md "Your kit".

**Film / lens** (`style.film`, cutroom/film.py), all off by default: `grain` (std, 0–12), `grain_size`
(1–3 px), `soften` (gaussian sigma px at 1080), `vignette` (corner fall-off 0–0.6), `halation` / `bloom`
(0–1), `ca` (lateral fringe px at the corners), `rolloff` (`{"knee": 0.86, "ceiling": 0.96}`), `black`
(lift 0–0.1). Fill it from a measurement only: `film.match(ref_stats, ours_stats)` returns the traits the
reference measurably has more of (rule 38); set `"from_reference": "<ref id>"` beside them (the sidecar carries it, and QA
`devices_vs_reference` fails film without it).
`"exposure_stops": 0.5` on a segment is a manual trim the creator names (rule 94). The sidecar records each
piece's applied `stops` and its LUT-only probe numbers (`lut_stats`: luma, p1, p99, sat) that QA
`grade_bounds` checks the render against.

**Dialogue audio.**
```json
"audio": {"join_xfade_s": 0.02, "room_tone": true, "room_tone_under_voice_db": -32, "bed_fade_s": 0.3,
          "jl_duck_db": -12, "auto_lead_in": 0.3, "auto_tail": 0.0}
```
- Every join between two pieces of the same sound (talk→talk, ambience→ambience) is a 20 ms equal-power
  crossfade over handles taken from the source.
- A room-tone bed runs under every talk segment, 0.3 s past each end. It is the clip's own quietest steady
  patch, taken from the cleaned dialogue, at max(the segment's floor, voice − 32 dB). The noise floor
  never steps at a jump cut. `"room_tone": false` on a talk segment turns it off there.

**J/L cuts.** On talk segments, `"lead_in": 0.3` starts the voice that many seconds before its picture,
under the previous shot (a J-cut). The audio comes from the source before `in`, so put `in` just after the
first word starts if you want the voice itself to lead. `"tail": 0.4` lets the audio run on under the next
shot (an L-cut). Neither changes the timeline length. When a talk segment follows b-roll, montage, a match
cut or a still, `lead_in` defaults to `style.audio.auto_lead_in` (0.3 s). In the reference, the voice
leads the face in 33 of 40 cuts into talk. The b-roll's ambience ducks 12 dB under the arriving voice, and
captions follow the heard audio.

**B-roll sound.** `broll.audio` now defaults to `"natural"`, so the location sound plays under the music
(about −24 LUFS under music, −20 without). `"music"` means no natural sound. Montage ambience sits a
further 6 dB down.

**Music structure** (`section.music`):
```json
"music": {"song": "...", "offset": 41.2, "gain_db": -8, "mode": "mute",
          "start": "cut", "end": "cut", "montage_gain_db": 7, "natural_duck_db": 0,
          "breaks": [{"before": "cut:2", "dur": 0.3}, {"at": 12.4, "dur": 0.25}]}
```
- `start` and `end` are `"cut"` by default: the song starts and stops on the section's cut frame with an
  8 ms ramp. `{"fade": 0.6}` gives a fade instead.
- The music sits `montage_gain_db` (+7) higher wherever nobody is talking. That puts it about 7 LU above
  talk, as in the reference.
- Only real dialogue ducks the music. Natural sound never ducks it by more than `natural_duck_db` (0 to −6).
- `breaks` silence the music for 0.2–0.4 s ending on an event. A flash or whip adds its own break
  automatically (`transition.break`, 0.3 s).
- `"mode": "under"` (used by the cold open) is a fixed bed `under_db` (−5) below the voice. It is never
  ducked.

**Transitions.** Put `"transition"` on a segment (the cut into it), or on a section (the cut into its
first segment):
```json
"transition": {"kind": "flash", "frames": 4, "sfx": "hit", "gain_db": -6, "break": 0.3}
```
- `burn` is the reference's two-frame film burn (cutroom/cards.py): on the last outgoing frame a red glow
  rises from the lower left, on the first incoming frame an orange-yellow disc with a magenta/blue fringe and
  a blown core enters from the right; the cut sits between them, nothing fades. It is light composited onto
  the picture (a normal mix toward the light, then a screen), different on every cut (`seed`), with a
  whoosh-into-impact (`sfx: "burn"`) whose impact lands on the cut. Fields: `frames` (2), `palette`
  (`warm` | `red` | `gold`), `side` (`right` | `left`, where the disc enters), `strength` (0.1–1.5).
- `flash` mixes toward white, made from the footage itself, with a small log-domain exposure push
  underneath. Its peak is on the cut frame. Use it only when the reference has white flashes.
- `dip` goes through black, `dip_white` through white.
- `whip` is a temporal mix plus a horizontal smear on the 3 frames either side, with a `whoosh` by default.
- `frames` is split between the outgoing and incoming side; `frames_out` and `frames_in` set them
  directly.
- The sound's transient *peak*, not its file start, lands on the cut frame.
- The reference wins (rule 38): rebuild the devices it measurably has (burns, insets, gates, cards, typed
  titles); never add one it doesn't. No stock or downloaded overlay assets.
- Segment SFX can bind the same way: `{"name": "hit", "on": "cut"|"end"}`.

**Frame-rate conform.** `style.conform` (or per segment/shot `conform`) is `"auto"` by default.
- A 23.976 or 24 fps b-roll source is sped up to 25 (+4.3 %, pitch-kept atempo on its ambience), so
  there are no duplicate frames.
- Sync talk and match cuts are frame-blended (`framerate`), so lip-sync holds.
- Sources far from the output rate (50, 60) go through `fps` (frames dropped, never duplicated), so they
  play in real time.
- PAL slow motion: a 50p (or 100p) b-roll shot in a 25p edit that carries a `ramp` plays as 2× (4×) slow motion,
  every source frame shown once, then ramps into the cut. `"conform": "slowmo"` asks for that without a ramp.
  Sync talk is never slowed.
- `"dup"` forces the old fps behaviour.

**Push-in.** `"push": {"to": 1.06, "ease": "inout"}` on talk, broll or a montage shot (or the montage)
does a slow digital move. It uses sub-pixel `perspective`, the smoothest of the measured methods.
- On talk it runs across the whole segment, through the jump cuts. With `crop: "face"` it centres on the
  face.
- `to` < 1 pulls out.
- `style.auto_push: {"min_hold": 4, "to": 1.04}` adds one to every talk segment held at least that long.
- Keep pushes to 3–8 %. A 1080p source has no headroom for more.

**Speed ramp.** `"ramp": {"to": 2.0, "over": 0.4}` on broll or a montage shot.
- The speed rises linearly over the last `over` seconds into the cut, with a temporal-mix motion blur.
  The piece keeps its planned length and consumes more source.
- The ambience fades out with the ramp.
- True slow motion needs a ≥ 48 fps source (none in the current library). Never slow 25p down (rule 34).

**Pacing.** `section.pace: {"asl": 0.6, "max_hold": 6, "alternate": true}`.
- Montages without beat sync get shot lengths around `asl`, in alternating fast and slow blocks.
- Beat-synced montages snap those lengths to beats.
- `max_hold` caps any shot. On a talk segment with `auto_cover` and a `cover` pool, it inserts a cutaway
  whenever the face is held longer than that.
- Take the numbers from the reference's sections (`cutroom ref show <id>`): ASL per section type.

**Frames ("resolution change").** `frame` on a section, a segment or a montage shot (shot wins, then the
segment, then the section; `"full"` turns it off). All hard snaps, no animation; the picture is cropped to
the window, never squeezed.
```json
"frame": {"kind": "gate", "w": 0.973, "h": 0.906, "radius": 9, "color": "#0F0100"}
"frame": {"kind": "gate", "inner": {"w": 0.77, "h": 0.74}}          // an inner black matte inside the gate
"frame": {"kind": "inset", "scale": 0.62, "radius": 5, "bg": "#000"} // or "w"/"h" for the reference's 61 x 77 %
"frame": {"kind": "letterbox", "aspect": 2.46}
```
The gate's numbers are the reference's: 97.3 % × 90.6 %, margins 25/45/27/56 px, near-black `#0F0100`,
9 px corners. A montage of 3-frame shots in an inset on black is the reference's strobe. A burn into an
unframed segment drops the gate under the light.

**Typed title** (overlay `kind: "typed_title"`): golden `#FFCD00` condensed caps (Bebas Neue if installed,
else Anton, League Gothic, Oswald, Impact …; `font` takes a path or a name) at 9.2 % of the frame height,
centred, typed one character every `char_s` (0.07 s default; the reference types 17 characters in 0.36 s)
starting `start` s after the overlay begins, then held until the segment's cut or burn. `sfx: "hiss"` adds
the reference's broadband hiss under the typing.
```json
{"kind": "typed_title", "text": "DAY THREE", "at": 0.0, "dur": 2.7, "start": 1.12, "char_s": 0.07}
```
`"pos"` places it (default: centred on the 0.505 line): `{"x": 0.72, "y": 0.2}` is the title's centre in
frame fractions (kept inside the frame), `"auto"` measures the held shot under it (`cutroom/titleplace.py`):
a grid of title-sized boxes over 4 frames of the span, scored by detail (gradient energy) and luma spread;
boxes touching a face/head or person, or holding blown highlights (the sun), are out; the title colour
must have a WCAG contrast of 3:1 against the box (2:1 against its bright patches), so yellow on a white
shirt or a pale sky fails. When no box reads, the quietest one gets a dark backing plate. The sidecar's
`typed_titles[]` records `pos`, `backing` and `pos_why`, and the overlay's real `box` (QA's faces check uses
it). `cold_open.title` takes `pos` too.

**Cold open (teaser template).** A plan-level object. It renders as a generated first section (index −1
in the sidecar and in `resolve_timeline`):
```json
"cold_open": {"style": "teaser", "dur": 10, "lines": [{"from": "villa/0"}, {"clip": "C0107", "in": 41.2, "out": 43.0}],
              "shots": "auto", "asl": 0.45, "from_reference": "9fe6442b1b",
              "music": {"song": "...", "offset": 52.0, "under_db": -5}}
```
`"style": "reference"` rebuilds the reference's measured cold open element by element (needs
`from_reference`; the reference's `devices` are measured on demand):
```json
"cold_open": {"style": "reference", "from_reference": "9fe6442b1b", "card": {"text": "COMING UP"},
              "lines": [...3 lines...], "shots": [...picks...],
              "end_shot": {"clip": "C5655", "in": 6.6, "dur": 2.7}, "title": {"text": "DAY THREE"},
              "loudness": {"I": -12}, "music": {"song": "...", "offset": 17.8}}
```
card → inset strobe (the reference's 3-frame shots) → line 1 in the gate → strobe block in the reference's
mattes → line 2 → block → burn → line 3 → burn (the biggest hit; the gate drops) → the end shot, held, with
the typed title → burn into the edit. Optional: `inset` / `gate` (override the measured frame), `burns`
(false or `{palette, strength}`), `ticks` (a tick per strobe image, default on), `captions` (on the lines).
- `face_s` (default null = the whole line): every line stays on the creator's face for its whole length
  (rule 49). A number cuts away to b-roll after that many seconds (style "teaser").
- `loudness.I` (−12 LUFS): the cold open's own dialogue target, about 2 LU over the programme, so the intro
  is not quieter than the body. `music.lift_db` raises the bed between lines (reference ≈ +10 dB).
- `dynamics: {"match": "reference"}` (punch): the cold open follows the reference's measured
  momentary-loudness envelope (`cutroom.reference.intro_envelope`, p95−p10 "swing" in LU), element by
  element: each cold-open segment is mapped onto its reference element's span, and a gain curve (0.1 s
  knots) on the summed cold-open audio (voice + music + SFX) is solved by measuring (apply → limit → ebur128 →
  correct). Where the reference is quiet (its held end shot) ours goes quiet; where it hits, ours hits. Only
  the cold open's peaks are limited (the same 4× oversampled limiter, before the encode), so the programme
  is not pulled down. Options: `offset_db` (0: the reference's absolute level), `min_db` / `max_db` (gain
  clamp, −24 / +15), `reference` (another ref id). It replaces the `music.under_db` / `lift_db` balancing as
  the thing that sets the swing; QA `swing_vs_reference` checks the result (fails over 2 LU off when
  `dynamics` is set, warns otherwise). The sidecar records `cold_open_dynamics`.
- An encode that overshoots the true-peak ceiling (AAC on sharp hits) is fixed by limiting the peaks lower
  (up to two passes), not by lowering the whole programme; a whole-programme drop is the last resort.
- `end_flash` defaults to false.
- `end_shot: "auto"` (or `{"auto": true, "dur": 2.7}`) picks the held end shot from the cold-open shot pool:
  each pool clip's first window not used anywhere in the edit is scored for the title (as `pos: "auto"`),
  readable without a backing first, then the quietest area; with `title.pos: "auto"` the title goes where
  that measurement found room. The choice is cached and recorded in the sidecar (`end_shot_choice`).
  `cutroom title place <plan> [--choose-shot] [--debug-dir D]` prints where auto would put each typed
  title, and the held-shot ranking (pool shots, sliding to a free window when the given one is used within
  10 s), without rendering.
- `card`, `inset`, `gate`, `burns`, `end_shot` and `title` also work in style "teaser".
- Lines are teaser lines from later in the edit. `{"from": "<section>/<segment>"}` picks the strongest
  0.8–2.6 s claim in that talk segment by itself (numbers, superlatives, stakes words, clean edges).
- Micro-cuts run between the lines (and under them when `face_s` is a number).
  The rhythm comes from `from_reference` (that reference's cold-open cut map, one-to-one) or from `asl`
  (~0.45 s, varied, pulled onto the half-beat grid).
- `shots: "auto"` takes the edit's own b-roll at windows the edit does *not* use later: talk ranges,
  cutaways, cover clips, b-roll, montage shots, stills and match-cut frames are all marked used, and an
  explicit `shots` list keeps those marks.
- The music runs `under` the voice (not ducked). Defaults: the first section's song.
- Style "teaser" puts a flash (or a burn with `burns: true`) into the second block of micro-cuts.

## Brand-film toolkit (a stylised cinematic reference)
Built to rebuild a measured brand film (black-and-white + teal night grade, 5:3 pillarbox, a voice memo over
pictures, a near-silent hole, a sub riser into the first word, a closing burst of callbacks, logo end cards).
`cutroom looks ref <id>` maps each reference shot to a look; `cutroom ref show <id>` gives the act shares and how the
cuts follow the voice. All fields are optional and off by default.

**Picture**
- Looks `bw_contrast` and `teal_night` (generated LUTs from the measured recipe, see Looks) per segment, per montage
  shot (`shots[k].look`) and per cutaway (`cutaways[k].look`, wins over its segment's look).
- `frame: {"kind": "pillarbox", "aspect": 1.667}`: black side bars, a 5:3 picture inside 16:9 (60 px a side at 1920).
- `{"type": "image", "file": "logo.png", "dur": 2.8, "size": 0.3, "bg": "#000000"}`: an image / logo card, centred,
  `size` = image height / frame height, PNG alpha kept, hard cut in and out, held. Full frame (no pillarbox) unless
  the segment sets `frame`. The file is the creator's (path relative to the project).
- `exposure_stops` takes an automation as well as a number: `{"from": -1.5, "to": 0, "at": 0, "over": 6}` or
  `[[t, stops], ...]` (seconds from the segment start): an exposure ramp across one shot (log domain before the LUT,
  as the fixed trim; quantised to ≤ 48 invisible steps).
- `montage.min_shot` / `max_shot` go down to 0.04 s; a burst shot of 0.2 s = 5 frames at 25 fps (`shots[k].dur`).
- `slowmo: 8` or `{"factor": 8, "method": "auto"|"frames"|"flow"}` on broll or a montage shot (1.05–16x). `frames`
  when the source has them (source fps / factor ≥ output fps: a 100p source gives a real 4x in 25p, every frame a
  new one); otherwise `flow`: the missing frames are optical-flow interpolated (`minterpolate`) after the grade, and
  the render warns (flow warps on fast motion and edges; real 8x needs 200 fps footage). The piece's length is
  (out − in) × factor; its natural sound is dropped (music / the bed carries it).

**Sound**
- `sound` on a segment (times from the segment start) or a section (times from the section start), on that
  piece's own audio (natural sound or voice):
  ```json
  "sound": {"gain_db": -30, "gain": {"from": -30, "to": 0, "at": 4, "over": 2}, "lowpass": {"from": 1900, "to": 4500, "at": 11.8, "over": 4}}
  ```
  `gain_db` is static (−30 under a −24 LUFS ambience = the reference's −50 LUFS near-silence hole), `gain` is dB
  automation, `lowpass` is cutoff automation in Hz (a bank of 4th-order low-passes crossfaded in log frequency, so
  a slam to 2.2 kHz never clicks; ≥ 18 kHz = open). Automation values: a number, `[[t, v], ...]`,
  `{"keys": [[t, v], ...]}` or `{"from", "to", "at", "over"}` (`over` defaults to the rest of the span).
- `section.music.lowpass` (same automation, section time) muffles the song; `section.music.gain_keys` is dB
  automation on the bed; `section.music.under_db` may be a ramp `{"from": -14, "to": 0}` (mode `under`).
- `section.music.duck_bands: {"sub": -25, "mid": -14, "high": -14}` (mode `duck`): a frequency-dependent duck under
  real dialogue: the bed is split at 120 Hz and 4 kHz (the mid is the exact remainder, so an un-ducked bed is
  unchanged) and each band ducks by its own amount.
- Voice-over from a voice memo: audio-only files under the library (WAV / M4A / MP3 ...) are registered by
  `cutroom index` (status `audio`, no proxy), transcribed like any clip, and used as
  `broll.voiceover: {"clip": "<memo>", "in", "out", "clean": false, "gain_db": 0}`. `clean: false` keeps the raw
  recording (its hiss and phone character; no de-noise) for that voice-over only.
- New sounds: `sub_riser` (`dur` 4–8 s, 32→110 Hz glide + 30–200 Hz noise, ~3.6 Hz tremolo, climbing ~36 dB; its
  END lands on the event and it stops dead: `{"name": "sub_riser", "on": "end", "dur": 6}` on the silent segment
  stops on the cut into the first word) and `sub_drop` (60→30 Hz over ~1.5 s, peak on the event). Any sfx takes
  `offset` (s, e.g. −0.14: the impact leads its cut) and `align` (`peak` | `end`); a transition takes `sfx_offset`.
- `style.dynamics: {"match": "reference", "reference": "<ref id>", "offset_db": 0, "min_db": -30, "max_db": 12}`
  (or `plan.reference`): after loudness normalisation the WHOLE programme follows the reference's measured
  momentary-loudness envelope (the cold-open solver extended: gain knots every 0.1 s, apply → limit → ebur128 →
  correct). The time map is each section's `ref_span: [ref_t0, ref_t1]` (an act onto the reference's act), or
  `style.dynamics.anchors: [[our_t, ref_t], ...]`, else a linear stretch. The sidecar records `programme_dynamics`.
- `plan.reference: "<ref id>"`: the reference the whole edit rebuilds (QA rule 38 checks devices against it).

**Story** (voice-over edits; `cutroom plan check` prints hints, never errors): see the edit-planning skill.
`cutroom breaks <clip>` lists where a voice-over can be cut (sentence / clause / pause gaps).

## Looks
`style.grade.look` colours the whole edit; `look` on a segment overrides it for that segment
(`{"name": "natural"}` = no look there). Value: `{"name": ..., "intensity": 0-1}` or just the name
(intensity 1). Built-ins: `natural`, `warm_film`, `cool_clean`, `golden_hour`, `teal_orange`,
`moody_matte`, `punchy`, `soft_pastel`, `bw`, `bw_contrast`, `teal_night`, `vintage_fade`, `night_cinematic`; a
`.cube` in the project's `luts/` folder is `"lut:<file name>"`.
- `bw` is Rec.709 luma written to R = G = B (an RGB channel mix): neutral, chroma exactly 128 (ffmpeg's
  `eq=saturation=0` wrote 127, a green cast).
- `bw_contrast` and `teal_night` are generated 33³ LUTs from a measured recipe (`looks.GEN_RECIPES`, cached by
  content in `cache/luts/`): `bw_contrast` = neutral low-key mono, crushed toe, white ceiling 0.85 (217);
  `teal_night` = one cyan hue at every level, a 4/255 cyan floor, 0.94 ceiling, chroma ×0.70 (×0.35 on skin hues).
- `teal_night` neutralises white balance first (`"neutralize": true` by default; any look takes the flag): grey-world
  gains from the shot's own mid-tones (measured per piece and per cutaway), so warm daylight footage lands on the
  same cyan as neutral footage. The look runs after the base grade (LUT / log curve, saturation)
and the exposure correction, on every shot of the segment including its cutaways (`cutroom/looks.py`).
Looks are opt-in (the house grade is the LUT only). Matching a reference's colour (`looks.match`, the
Studio's "match look") never picks `night_cinematic` for daylight footage and caps the intensity so the
black point stays ≥ 15 and saturation within +15 % of the LUT-only picture. **Reference look mode**
(`looks.match(..., reference=True)`, on when the reference's own blacks are crushed: `looks.reference_mode`) lifts
those guards, because then the crushed black and the night tint ARE the look. A stylised reference switches looks
by shot: `cutroom looks ref <id>` classifies every reference shot (mono / cool / warm / natural) and names the look
for it (in a teal film the warm shots get `teal_night` too: the same grade on practical light).

## Overlays
`kind` is one of `callout`, `lower_third`, `title`, `label` or `typed_title` (above). `position` is one of `auto`, `top-left`,
`top-right`, `bottom-left`, `bottom-right` or `center`. `auto` places the overlay away from faces.
Keep the text short. Creators usually want at most one overlay per talking section.

## Handing the edit to an NLE
`cutroom export-nle PLAN.json --format premiere|fcpxml|otio|edl|all [--zip]` writes a Premiere XML, FCPXML 1.10, OTIO
and EDLs. They link to the original masters and are frame-exact against the render's timeline. Graphics come as ProRes
4444, cutroom-only effects as rendered segments, captions as an .srt, plus a README. `--verify DIR` checks every clip.
See `docs/NLE_AND_MULTICAM.md`.

## What render produces
- `renders/<slug>_v<N>[_preview].mp4`, the video.
- `renders/<slug>_v<N>[_preview].json`, the resolved timeline: every atomic piece with timeline
  start/end, source clip and in/out, captions shown and overlays. QA and the review loop use it to map
  "the clip at 1:45" to a segment.
