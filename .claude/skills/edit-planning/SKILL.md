---
name: edit-planning
description: Use when the creator asks for an edit (e.g. "make a 2-minute edit of the trip", "a hype video from last week", "a day-in-the-life", "a story about X"), to choose the story, select clips and write the cutroom plan JSON. Covers structure, talking segments, b-roll, montages on the beat, match cuts, captions, graphics and music.
---

# Planning an edit

Read `docs/EDITING_RULES.md` once per session. These are the rules that matter most at the planning
stage.

## 1. Brief
Get these from the creator's request. Ask **one** short question only if something is missing and it
changes the story:
- length (default 2:00)
- aspect (16:9 by default; 9:16 if it's for Reels or TikTok)
- the story or theme
- must-have moments
- music (their songs folder, or "pick something that fits")
- caption style

## 2. Find the material
- Talking moments: `cutroom search "<topic words>" --speech`, then `cutroom say <id>` to read the
  lines with timestamps.
- Visuals: `cutroom search "<topic>" --creator --max-shake 4 --min-quality 3`.
- Match-cut ideas: `cutroom matchcuts` (run once per library; it is a list of candidates).
- Open the sheets, then pull frames (`cutroom frames <id> t`) for every shot you plan to use.

## 3. Structure a 2-minute edit (default shape)
| Part | Length | What |
|---|---|---|
| Cold open | 5–10 s | the `cold_open` teaser template (below): 2–3 teaser lines from later in the edit, dense micro-cuts, music under the voice |
| Setup | 10–20 s | the creator says what this is (talking clip, captions) |
| Body | 60–90 s | talk ↔ action alternating, one idea per beat, a journey shot whenever the location changes |
| Peak | 10–20 s | the biggest moment, a montage on the drop, or a match cut |
| Out | 5–10 s | a closing line, then an aspirational last shot of the creator alone |

For a no-talk hype edit: cold open → three montage sections on the song's sections → peak → out.

## 4. Writing segments
- **talk.** Choose in/out on sentence boundaries; `tighten: true` removes the pauses.
  - Check `cutroom say` for director cues ("cut", "let's go again") and repeated takes. Keep the
    last take.
  - Captions are on by default.
  - Cutaways go only where the words name something you can show, or over a join where a lot was
    removed. Keep cutaways under ~18% of talk time.
- **broll.** A cutaway must show where the creator is *now* and match the tense: no eating shots
  while they're ordering, no beach before they arrive.
- **montage.** Use 4–10 moving shots of the creator, each about 0.5–1.2 s, with `beat_sync: true`.
  - Use a different source for each shot.
  - Never cut a clip against itself.
  - Every cut must visibly change the picture.
  - Keep `in + shot length` inside the clip, or it freezes on the last frame.
- **matchcut.** Use candidates from `cutroom matchcuts`, or two shots with the same framing. A ends
  exactly at `a.t` and B starts exactly at `b.t`. Use them for time jumps (day to night) or places.
- **title / black / still.** Use sparingly.
- **overlays.** At most one per talking section, and only when it explains something the footage
  can't show.
  - Short text (a place, a number).
  - `position: "auto"` keeps it off faces.
  - Creators reject busy, "AI-looking" graphics.

## 5. Music
- Pick each section's song for mood.
- Set `offset` so the drop lands on the first montage cut (`cutroom beats <song>` lists beats and drops).
- Default `music_under_speech: "mute"`: no music while the creator talks (a rule from real reviews). Use `"duck"` only if the creator asks for a quiet bed under speech.
- Change songs at section boundaries, not in the middle of a line.

## 6. Cold open: use the template
For any edit with talking, write a plan-level `cold_open` instead of hand-cutting a montage:
```json
"cold_open": {"dur": 10, "lines": [{"from": "villa/0"}, {"from": "evening/2"}], "shots": "auto",
              "from_reference": "<ref id>"}
```
- **lines** are open loops: 2–3 short claims (0.8–2.6 s) that pay off later in the edit, each from a
  *different* section. `{"from": "<section>/<segment>"}` picks the strongest line in that talk segment
  (numbers, superlatives, stakes). Read the pick back in `cutroom plan check` / the sidecar note, and give
  explicit `clip/in/out` when you know the better line (rule 49: lines are on the face or a related bright
  shot).
- **rhythm**: when the creator gave a reference, set `from_reference`. The teaser is then rebuilt on that
  reference's own cold-open cut map, one-to-one (rule 45); `cutroom ref show <id>` prints it. Without one,
  `asl: 0.45` gives a varied ~0.45 s rhythm pulled onto half beats.
- **shots: "auto"** takes the edit's own b-roll at windows the edit doesn't use later (no rule-23
  repeats). Pass a list of approved picks when the creator named them (rule 47).
- **music** defaults to the first section's song, playing *under* the voice and never ducked. Point
  `music.offset` at the part of the song you want.
- **Reference intros: `"style": "reference"`.** With `from_reference` set, the cold open is rebuilt element by
  element from the reference's measured devices (`cutroom ref show <id>`, analysis `devices`): the card
  ("COMING UP", `card: {"text"}`), the inset strobe (3-frame shots on black), the lines in the reference's
  frame (window-box gate) **on the face for the whole line** (`face_s` null), the blocks between them in the
  reference's shot lengths and frames, a two-frame burn wherever the reference burns, the gate dropping on
  the biggest hit, and an `end_shot` (`{clip, in, dur: 2.7}`, the creator alone) with the typed `title`
  (`{"text": "DAY THREE"}`, no place names in repo files). The cold open has its own loudness target
  (`loudness.I`, −12) and the music lifts between lines (`music.lift_db`, +8 in reference style).
- `end_flash` is off by default. A white flash is only right when the reference has white flashes (rule 123).

## 7. Craft fields
The defaults already do these. Change them only for a reason:
- **J/L cuts**: a talk segment after b-roll gets `lead_in` 0.3 automatically, so the voice starts under
  the b-roll. Set `"lead_in": 0` for a hard sync cut, or `"tail": 0.4` to let a last line trail over the
  next b-roll. For a voice that leads with words, put `in` ~0.3 s after the first word starts.
- **B-roll sound**: broll defaults to `"audio": "natural"` (ambience under the music). Use `"music"` only
  when the location sound is ugly (wind, strangers talking).
- **Transitions**: put `"transition": {"kind": "flash"}` on the first segment of a section where the
  *location or time* changes. That's at most one per ~20 s of body, never inside a talk segment, and
  devices stay ≤ 25 % of runtime (rule 44). Use `dip` for a time jump and `whip` between two moving
  shots. Use `burn` (`{"kind": "burn"}`, 2 frames, the cut between them) when the reference burns; it is
  generated from the reference's measured shape and colours. Rule 38: only devices the reference has.
- **Grade**: leave `style.grade` alone. The house grade is the LUT only (rule 120); `normalize_exposure`,
  `finish`, `saturation` and `look` are opt-ins for when the creator asks. Never a night look on daylight.
- **Film / lens** (`style.film`: grain, soften, vignette, halation, bloom, ca, rolloff, black): off by
  default. Fill it only from a measurement (`cutroom.film.match(ref, ours)`), never by taste.
- **Frames**: `"frame": {"kind": "gate"}` / `{"kind": "inset", "scale": 0.62}` / `{"kind": "letterbox",
  "aspect": 2.46}` on a section, segment or montage shot. Only when the reference has them.
- **Music**: songs start and stop on the section cut. Montage and b-roll get +7 dB over talk. A flash
  gets a 0.3 s music break before it. For a drop on the first montage cut, set `offset` from
  `cutroom beats`.
- **Pacing**: copy the reference's section ASLs into `section.pace` (`cutroom ref show <id>` lists them,
  e.g. talk 6–10 s, montage 0.5–0.9 s, voice-over b-roll 0.6–2 s). Use `alternate: true` and a
  `max_hold` (talk 6–8 s with an `auto_cover` pool).
- **Push-ins**: use `"push": {"to": 1.05}` on long held talk or static b-roll from 4K masters. Never on
  phone footage or 1080p sources.
- **Speed ramps**: use `"ramp": {"to": 2.0, "over": 0.4}` on a b-roll or montage shot going into a cut
  on a drop. Never on talk, and never as slow motion.

## 8. Check and render
Run `cutroom plan check <plan>`; it validates every clip and in/out against the library. Then
continue with the render-and-qa skill.

Put a `"note"` on segments explaining *why* they're there. It helps you and the creator later.
