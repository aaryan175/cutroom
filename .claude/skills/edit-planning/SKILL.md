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
| Cold open | 5–10 s | fast beat-cut montage of the best moments, with the strongest line in the first 2 s |
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

## 6. Check and render
Run `cutroom plan check <plan>`; it validates every clip and in/out against the library. Then
continue with the render-and-qa skill.

Put a `"note"` on segments explaining *why* they're there. It helps you and the creator later.
