---
name: event-recap
description: Use when the creator asks for a recap or hype edit of a live event (seminar, conference, show, a stage speaker and a crowd), e.g. "make a 2-minute recap of the event", "hype video from the weekend conference". Covers finding the soundbites, crowd, stage and wide shots, picking the song, the recap structure, flash and ramp rules, and the checks before rendering.
---

# Event recap

The shape the creator wants is a **2-minute hype recap**: a cold open on a big line, a build where crowd,
wide and stage shots cut on the beat with white flashes on downbeats, the speaker's best one-liners as
soundbites over a building song, a speed ramp here and there, a climax on the biggest crowd moment, and an
end card. `cutroom recap` writes a draft with exactly that structure. Your job is to make it good.

Read `docs/EDITING_RULES.md` and the `edit-planning` skill once per session. With a reference edit, also
load `reference-matching`.

## 1. Measure the library for events
```bash
cutroom index && cutroom transcribe          # transcripts are needed for soundbites (stage clips first: --clip)
cutroom detect events                        # stage / crowd / wide per 2 s window, energy peaks, soundbites
cutroom search --kind crowd                  # crowd clips, strongest window first ([crowd 0.81 @34s, 60% of clip, energy 0.72])
cutroom search --kind wide --max-shake 4     # establishing views of the room
cutroom search --kind stage                  # the speaker on stage
cutroom search --soundbites                  # quotable lines, best first; add words to filter: --soundbites "dream"
cutroom clip <id>                            # the event line: kind shares, energy peak, soundbite count
```
What the numbers are (estimates, so look before you trust them):
- **stage**: a dominant face (1–3 faces, the largest ≥ ~8 % of the frame height) or spotlights in a dark
  frame, with voice-like sound.
- **crowd**: many faces and/or lots of *local* motion (frame difference after removing the camera's own
  movement) in a busy frame, plus applause or cheering.
- **wide**: a busy frame with little local motion and no big face.
- **energy**: loudness + applause/cheering + local motion. `peak` marks local maxima ≥ 0.5.
- **soundbites**: 0.8–7 s lines of 3–18 words, scored by loudness and pitch against the speaker's own
  median, emphasis (loudness swing, "!", caps), wording (short, direct, "you/we/never/dream", numbers),
  and **the crowd's response in the 4 s after the line** (louder + applause-like). A line the room cheered
  is usually the one to use.
- Haar faces miss small or side-on faces, and a dark crowd lit by stage lights can look like a stage.
  A soundbite's crowd response is heard on *that* clip's audio only: a speaker on a lav with no room sound
  gets no response score.

## 2. Pick the song
Ask once if the creator didn't name one ("which song?" with 2–3 numbered options from `cutroom songs list`).
Otherwise pick a song that **builds**: a clear drop 60–80 % of the way into the runtime, tempo 110–150 BPM.
`cutroom beats <song>` lists beats, downbeats and drops. The recap puts the climax on a drop.

## 3. Write the draft
```bash
cutroom recap <name> --len 120 --song <song> [--ref <ref id>]
cutroom plan check plans/<name>_v1.json
```
The draft (`cutroom/recap.py`):
| Section | What |
|---|---|
| cold open | the best soundbite (talk, music `under` the voice), then the crowd reacting (natural sound) |
| build | one song ducked under the voice (`mode: duck`). An establishing montage of wide shots, then 8-beat montage blocks of crowd / stage / wide, a soundbite every ~20 s (J-cut: `lead_in` 0.3, the voice starts under the last montage shot), each followed by a crowd reaction whose length lands the next block on a downbeat |
| climax | a flash onto the song's drop, a fast montage of the strongest energy peaks, the single biggest peak held ~2.5 s, then the end card (`title` with "LOGO" on a blurred crowd frame, song fading out) |

With `--ref`, the reference sets the cold-open length, the flash rate, the montage pace (beats per cut)
and where the climax starts (`cutroom ref show <id>` first; see `reference-matching`). The draft's
`note` says what it took.

## 4. Refine it (this is the real work)
Go through the draft section by section, like an editor:
- **Read every soundbite** (`cutroom say <clip> --from <in-2> --to <out+2>`). Keep lines that stand on
  their own without context, a promise, a punchline or a call to the room. Drop logistics, sponsor reads,
  names of people who aren't on screen, and anything half-finished. Story order: cold open = the biggest
  line, then lines that build in intensity, the most emotional one last before the climax.
- **Never cut mid-word.** `in` and `out` must sit in the gaps between words (the draft starts and ends on
  word boundaries; check every change against `cutroom say`). For a J-cut, put `in` ~0.3 s *after* the
  first word starts and set `lead_in: 0.3`, so the voice itself leads and the audio still starts on the word.
- **A crowd reaction after every soundbite**: clapping, cheering, people on their feet, ideally from the
  same moment of the event (the draft prefers applause-heavy crowd shots close in capture time). It plays
  with natural sound so the cheer is heard.
- **Look at every shot.** `cutroom frames <clip> <t>` for each montage shot. Crowd shots must show energy
  (jumping, hands up, hugging). Wide shots must be steady and show the scale of the room. Swap anything
  dark, soft, empty or mislabelled.
- **Strangers' faces in focus**: a crowd shot where one stranger's face is sharp and large (the draft
  notes them as "CHECK the faces in …") is a person featured, not a crowd. Keep it only when it's a
  flattering, clearly public moment the creator would show. Never feature someone crying, eating,
  drunk-looking or a child up close without the creator saying yes. When unsure, use a wider moment.
- **Flashes**: white `flash` transitions only on the cut into a montage block or a section, and only on a
  **downbeat** (or the drop). Never inside a talk segment, never on a soundbite's first frame. At most
  **2 per 10 s** by default, or the reference's own rate (its white flashes per 10 s). Flashes bring a
  0.3 s music break by themselves; don't stack a whip on top.
- **Speed ramps** (`"ramp": {"to": 2.0, "over": 0.4}`) only on high-motion crowd shots going into a cut on
  a flash or a drop, never on talk and never slow motion (25p sources have no frames for it).
- **Montage blocks**: 4–10 shots, a different clip for every shot, no clip cut against itself, wide →
  crowd → stage variety. Use `beats_per_cut` 2 for establishing and 1 for the build and climax (or the
  reference's ASL).
- **Climax**: the biggest energy peak (`cutroom search --kind crowd`, highest energy) is the last held
  shot before the end card. Check it's the moment the room actually erupted.
- **End card**: the draft's `title` says "LOGO". Ask the creator for the logo/end card text; never invent
  event names, speaker names or dates.
- **Music**: one building song for cold open + build (the draft sets offsets so the build ends on the
  drop); the climax starts exactly on the drop. `duck_db` -14 keeps the song alive under the soundbites;
  use `-18` if a line gets lost.

## 5. Render and check
Continue with `render-and-qa`. For a recap, also check on the contact sheets:
- every flash sits on a beat (qa's flash checks) and there are no more than the limit in any 10 s;
- every soundbite is understandable over the music (listen to it, not just the meters);
- no stranger's face is featured without a reason;
- the climax peak and the end card are on screen long enough to read.
With a reference, run `cutroom ref compare <id> renders/<name>_vN_preview.mp4` every round.
