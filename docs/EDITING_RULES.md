# Editing Rulebook for an AI Video Editor

Distilled from a creator's review notes on roughly 23 versions of a long-form day-in-the-life vlog, plus about 140 short-form reels cut from the same creator's own footage. Every rule traces back to a real note or a real measured defect. Generic editing advice was left out.

Tags: **[creator-feedback]** means the creator asked for it or rejected something. **[measured-failure]** means a build shipped a defect that a measurement or an audit caught.

---

## 1. Story & structure

1. **Order beats in real capture order, and never show a place before the creator has reached it.** [creator-feedback]
   Why: a beach shot while the creator is still getting ready to leave, or a clip from later in the day used early, breaks the story.
   Check: sort every shot by its capture timestamp inside each chapter. Flag any shot whose location tag belongs to a later chapter than the line it covers.

2. **A cutaway must show where the speaker is at that moment and what they are doing.** [creator-feedback]
   Why: putting a vehicle shot under "back to work" while the creator stands at their own front door, or the home driveway under "now leaving the restaurant", contradicts the words.
   Check: for every cutaway, assert location_tag(cutaway) == location_tag(the talking clip it covers), unless the line names another place.

3. **Match the tense of the action: no eating shots while the creator says they are ordering. Show the food after it arrives.** [creator-feedback]
   Why: showing the result before the setup kills the reveal and reads as careless.
   Check: if the transcript window has order/waiting/arriving words, ban cutaways tagged eating/served. Allow them only after the arrival line.

4. **Give every location change a journey shot.** [creator-feedback]
   Why: going straight from "leaving the restaurant" to "finally back" with no travel felt like a teleport. The gap also left a loud silence.
   Check: between consecutive chapters with different location tags, require at least one transit shot (walking out, getting on, riding).

5. **A departure shot belongs to the trip it actually shows.** [creator-feedback]
   Why: the creator's own driveway was used for leaving a venue, but that driveway is only ever the start of a trip out.
   Check: map each departure or arrival clip to its real origin and destination by capture time, and only place it on the matching trip.

6. **Start each talking line at the start of the sentence and end it after the thought finishes.** [creator-feedback]
   Why: lines that began mid-sentence ("…does this place look") or stopped on "like I said, that" sounded broken.
   Check: the first word of each segment follows a pause of at least 0.3 s in the source. The last word is followed by a pause or sentence-final punctuation in the transcript.

7. **Never name a shot as something it is not.** [creator-feedback]
   Why: a clip labelled "gym" was actually the creator trying on clothes.
   Check: every noun in a caption or graphic over a shot must appear in that shot's action or object tags. Otherwise flag it for an eye check.

8. **Cut asides that confuse the viewer: chats with strangers, inside jokes with no setup, and any segment the creator calls unneeded.** [creator-feedback]
   Why: a stranger's chat with no context, a "what happened to your house" bit and a "subscribers" moment all made viewers ask "what is this?"
   Check: flag every transcript span where an unidentified second voice leads for more than 3 s. Send each one to the creator as keep or cut.

9. **For a memory beat, play the archive clip under soft music, then match it to a "now" shot.** [creator-feedback]
   Why: hype music under a nostalgic home-video clip ruined the moment. A then-and-now match was what the creator wanted.
   Check: the music under an archive clip comes from a calm, low-BPM track. The next shot rhymes with it in framing or subject.

10. **Build the outro recap from the whole day in real order, using only shots of the creator.** [creator-feedback]
    Why: a recap with random clips, one of which was someone else, felt unplanned.
    Check: the recap takes one shot per chapter in chronological order, and every shot passes face-ID against the creator.

11. **Before delivery, read the whole cut against the transcript to confirm it makes sense.** [creator-feedback]
    Why: "make sure it makes sense with what I'm saying" came up in almost every review.
    Check: generate a beat sheet (time | spoken line | thumbnail | shot description) and read it top to bottom. Every row must make sense with the row before it.

---

## 2. Talking segments & trimming

12. **Remove every director cue and every line spoken to the camera operator.** [creator-feedback]
    Why: "Cut", "All right, cut" and "All right, let's go" (said to the cameraman) were left in.
    Check: search the transcript for cut, recording, rolling and let's go within 1.5 s of a segment edge. Trim the segment to start or end outside that cue.

13. **When a line was said twice, keep only the final take.** [creator-feedback]
    Why: the same statement played twice back to back. The creator said "remove the first, keep the second."
    Check: compare each sentence with the sentences in the next 90 s using normalized text similarity. Above 0.7, drop the earlier one.

14. **Thin out a word the creator overuses within one section.** [creator-feedback]
    Why: the creator said the same adjective four times around one meal and noticed it themselves.
    Check: count non-stopword lemmas per 60 s window. Any word at 3 or more gets its weakest occurrences cut, and the sentence must still make sense.

15. **Cut dead air inside a sentence.** [measured-failure]
    Why: a 3.8 s pause inside "it's lively out here, actually" stalled the scene.
    Check: in the forced alignment, any gap between words over 0.7 s inside a sentence is trimmed down to 0.15–0.25 s.

16. **Hide a fluffed word with a cutaway over the join. Do not play the stumble, and do not leave a hard jump on the face.** [creator-feedback]
    Why: a heartfelt line was first left with the slip in, then played as one long take with the pauses. The creator preferred the earlier trimmed version with the cut covered.
    Check: at every join that removed more than 1.5 s, check whether a cutaway covers it. If none does, add one or confirm the face is continuous.

17. **Start a talking line on the speaker's face, not on a stray B-roll frame.** [creator-feedback]
    Why: one frame of B-roll at the start of a punchline looked like an error.
    Check: run face detection on the first 3 frames of every talking segment. The speaker must be present in all of them.

18. **Use a repeated punchline once.** [creator-feedback]
    Why: the same announcement played twice, then a third time.
    Check: same method as rule 13, applied with a 30 s window inside a single talking clip.

---

## 3. B-roll selection

19. **While the creator talks, stay on their face. Cut away only when the words name something you can show.** [creator-feedback]
    Why: "a bit too much B-roll when I'm speaking". The reference cuts away for only 0–16% of talk time.
    Check: per talking piece, cutaway time ≤ 18%, at least 12 s of face between cutaways, each cutaway held about 3 s, and each needs a strong topic match.

20. **Use cutaways to cover jump cuts, not to decorate.** [creator-feedback]
    Why: "B-roll should be when there's a lag or something".
    Check: every cutaway either covers a join that removed more than 1.5 s or matches a noun the speaker says within ±0.5 s.

21. **Pick B-roll by what is being said. Time of day only breaks a tie.** [creator-feedback]
    Why: "B-roll related, if possible". The nearest-in-time picks showed a desk for "so hungry" and a dark lounge for a restaurant name.
    Check: the topic-match score between the transcript phrase and the clip tags must clear a threshold. Reject desk or generic shots under food, place or activity words.

22. **When the creator asks for a specific cutaway, place it exactly on the named action.** [creator-feedback]
    Why: the creator asked for a shot of themselves at the laptop right before the line about a work setback.
    Check: the cutaway starts within ±0.3 s of the word that names the action, and the face returns before the punchline.

23. **Never reuse a B-roll window or setup in the same video.** [creator-feedback]
    Why: "the B-roll is repeated"; "you keep using the same really good start".
    Check: every (clip, in-point window) is unique across the timeline. No two cutaways share a setup (same place, light and framing) within 60 s.

24. **Every shot must show the creator. No empty walls, rooms, pools, desks or scenery plates.** [creator-feedback]
    Why: "it's just an empty clip… it's the wall"; "there isn't even anyone in them". Empty shots were the most repeated rejection.
    Check: run person segmentation on first, middle and last frame of every shot. Any shot with no person blob fails unless it is a requested object insert.

25. **Never put another person in a slot meant for the creator.** [creator-feedback]
    Why: "that last person isn't even me", seen several times, in montages and in the outro.
    Check: compare a face-ID embedding against the creator's reference set on every person shot. Below threshold, or no face, means recast.

26. **Search every capture series before saying a moment is not in the footage.** [creator-feedback]
    Why: the editor said "there's no footage of X" from a tag search. The clips existed untagged, and the creator was annoyed.
    Check: search by tags, by capture time and by clip-number series, then eyeball contact sheets of untagged clips around that time.

27. **Fill montages with good unused in-camera footage before reusing clips.** [creator-feedback]
    Why: "if you didn't use that in-camera stuff, put that in the montage".
    Check: list the clips with no usage in the timeline, rank them by quality, and pull montage candidates from that list first.

---

## 4. Montages & beat sync

28. **Cut every montage shot on a beat of the song.** [creator-feedback]
    Why: "understand what would work with the beat songs". Off-beat cuts felt amateur next to the reference.
    Check: run beat tracking on the music bed. Every montage cut lands within ±1 frame of a beat.

29. **Use moving shots in montages, not static ones.** [creator-feedback]
    Why: "the montage is shit". The rebuilt version with moving shots across the day was accepted.
    Check: the mean absolute frame difference inside each montage shot is above the clip's static baseline. Reject near-frozen spans.

30. **Never cut a locked-off clip against itself back to back.** [measured-failure]
    Why: six consecutive cuts from one tripod clip read as playback stutter or lag.
    Check: merge consecutive montage shots from the same source into one continuous shot, and assert that adjacent sources differ.

31. **Make every montage cut visibly change the picture.** [measured-failure]
    Why: cuts between near-identical poses or takes from the same setup were invisible (frame difference 1–9 against 20+ elsewhere).
    Check: the mean absolute luma difference across each cut is ≥ 20 (0–255 scale). Recast the weaker side if it is lower.

32. **Keep montages short and purposeful: about 0.8 s cuts and well under 10 s unless the montage is the chapter.** [creator-feedback]
    Why: "the montage is way too long and very random"; one night montage went from 28 s to 9 s.
    Check: montage length ≤ 10 s, average shot ≤ 1 s, and no source clip used twice.

33. **Never let a montage window run past the end of its source clip.** [measured-failure]
    Why: an in-point near the end of a 6.2 s clip produced a visible freeze at 9–10 s in the cold open.
    Check: assert in + duration ≤ clip duration − 1 frame. Scan the output for 3 or more identical consecutive frames.

34. **Never slow a clip down to fill a slot unless slow motion was asked for.** [measured-failure]
    Why: a clip shorter than its slot was auto-slowed into a slow-motion water shot the creator called unnecessary.
    Check: assert playback speed is 1.0 for every shot. If clip length is less than slot length, pick another clip.

35. **Put a short action montage of 2–4 s right where the creator names the action, then go back to the creator.** [creator-feedback]
    Why: "before I say I'm so full, have a quick montage, like 3 seconds of me eating".
    Check: the montage starts within 1 s of the trigger line and ends before their next line.

36. **Build an intro montage from several moments across the day, not from one prop or activity.** [creator-feedback]
    Why: an intro made mostly of smoking shots was rejected, and the creator said "not all smoking clips".
    Check: the intro montage covers at least 5 distinct capture chapters, and no prop or activity tag appears on more than 2 shots.

37. **Keep montages full-frame and clean, with no film-frame borders, plates or picture-in-picture.** [creator-feedback]
    Why: "what is the put me on the plate stuff, make it more clean".
    Check: the active picture area is 100% of the frame. No border or overlay layers are in the montage graph.

---

## 5. Match cuts & transitions

38. **The reference wins. Rebuild every effect the creator's reference measurably has (film burns, light leaks, inset strobes, window-box gates, letterboxes, title cards, typed titles), matched to its measured colour, size and timing. Home-made decorative effects the reference does not have stay banned.** [creator-feedback]
    Why: home-made leaks, burns, scribbles, film strips and smears that no reference had looked like template-app presets ("the colours are so bad", v019). Then a match without the reference's own devices was rejected the other way: "the flashes are missing, there's no Coming Up, the resolution change is missing". The forensic inventory of the reference intro found a "COMING UP" card, a two-frame film burn on every major boundary, an inset strobe on black and a window-box gate that drops on the biggest hit. Those are the creator's taste, not presets.
    Check: QA `devices_vs_reference`: every device in the render (sidecar `devices`: burn, card, inset, gate, letterbox, typed_title, film) appears in the reference's measured devices (`cutroom ref` analysis `devices`, sidecar `reference.devices`). A device the reference lacks fails. Effects are generated from the measured numbers (cards.py), never stock or downloaded assets.

39. **Remove the whoosh with the overlay transition it belonged to.** [measured-failure]
    Why: when the overlays were removed, their whooshes still fired over plain cuts.
    Check: every SFX event must sit on a flash or device that still exists in the timeline.

40. **Frame-lock both sides of a match cut: same framing, same scale, the camera still.** [creator-feedback]
    Why: what made the creator's favourite edit work is that the change reads as a glitch in time, not a cut.
    Check: compute edge-structure correlation (contrast-normalised) between the last A frame and the first B frame. It must beat a cross-shot null by a clear margin. Never use raw luma correlation, because day-to-night pairs invert luma.

41. **In a day-to-night split, the boundary must be the person or the scene, never a visible line.** [creator-feedback]
    Why: the creator liked the split only if they walk through from the other side and "phases through smooth".
    Check: no straight-line or geometric matte edge in the transition frames. The boundary follows a segmentation mask.

42. **Composite clone or walk-through effects with full-resolution person segmentation.** [creator-feedback]
    Why: a difference matte against a non-empty plate smudged half of the creator's head.
    Check: zoom 3x on the head in every composited frame. No smearing, ghosting or missing hair.

43. **Use match cuts where the reference uses beat-cut intro montages, and keep any match cut the creator has praised.** [creator-feedback]
    Why: "a lot of them should just be match cuts"; "it's a good cut, I like it" (about a desk-to-departure match) even though the shot around it was wrong.
    Check: lock praised device pairs by (clip, in-point) and assert they are unchanged in later versions.

44. **Use devices as seasoning: one device as the first-second hook, one or two on the drop or a time jump, at most about 25% of runtime.** [creator-feedback]
    Why: the creator liked the devices but wanted them inside normal edits, not whole reels of effects.
    Check: device frames ÷ total frames ≤ 0.25, and the first device starts within the first second.

---

## 6. Intro / cold open

45. **When the creator gives a reference cold open, measure its cut map frame by frame and rebuild it one-to-one.** [creator-feedback]
    Why: a loosely inspired teaser was "worse than before… the coming-up isn't the same, the audio isn't the same". The one-to-one rebuild was accepted.
    Check: on a 25 fps side-by-side, every reference cut, flash and text line has a matching event within ±1 frame, and the loudness envelope tracks within about 3 dB.

46. **Open fast and flashy. The first seconds must not let anyone get bored.** [creator-feedback]
    Why: "the flashy thing of the reference is still missing… it needs to be quick and fast."
    Check: at least 4 cuts or flashes in the first 3 s, and the first talking line within about 10 s.

47. **Pick cold-open micro-cuts from the best shots the creator has approved, never from filler.** [creator-feedback]
    Why: "the trailer clip is horrible".
    Check: every teaser shot ID must be in the approved-picks list.

48. **End the cold open on an aspirational shot of the creator alone.** [measured-failure]
    Why: the first replacement end shot had a stranger and a dog in frame. A blurry dark sunset and a cigar shot were also rejected.
    Check: the last teaser shot passes person count = 1 (the creator), sharpness above threshold and mean luma above threshold.

49. **Keep teaser lines on the creator's face or on a related bright shot, never on a dull or dark frame.** [creator-feedback]
    Why: a teaser line over a flat dark shot lost the hook.
    Check: shots under teaser lines have mean luma ≥ 35% of the bright end and either a speaker face or a topic match.

50. **Short-form: frame the hook face at a medium distance, not right up against the lens.** [creator-feedback]
    Why: "my face is so close to the camera". Hooks cast from medium shots passed.
    Check: hook face height is about 7–16% of frame height, and reject anything above 0.27.

51. **Short-form: put text on screen within the first second.** [measured-failure]
    Why: across the genre, flops were often the same footage with no text, and the creator's own account measured text-first above no-text.
    Check: OCR on frames 0–30 finds at least one caption word.

---

## 7. Captions

52. **Make captions say exactly what was said. Fix every transcription error by ear.** [creator-feedback]
    Why: a misheard word flipped the meaning of a line ("trying to *stay*" instead of "trying to *sell*").
    Check: re-transcribe with a larger model, diff it against the captions, and listen to every line where the two disagree.

53. **Never put a black outline, keyline, stroke or dark drop shadow on caption text.** [creator-feedback]
    Why: the creator rejected it over and over, and reads a directional shadow as "a black outline" too.
    Check: measure a 1–2 px ring around the glyphs against a 5–8 px bed ring. The near ring must not be darker than the bed by more than 5 levels on any caption frame.

54. **Fix caption readability by moving the caption or re-picking the shot, never by adding an outline or flipping the ink colour.** [measured-failure]
    Why: a shadow fallback kept firing silently whenever a bright bed failed contrast.
    Check: the build stops if any caption state fails flat-ink contrast (≥ 3.0, with 0.3 margin for the encode). Fix it by moving the caption or the in-point.

55. **Captions must never touch the face, including beard, ears and hair.** [measured-failure]
    Why: captions shipped over the mouth, nose, beard and ear. Face boxes stop above the jaw, so they missed it.
    Check: count caption ink pixels inside face boxes on every frame and require 0. Also eye-check a native crop of the caption band over the head.

56. **Keep one caption style for the whole video.** [creator-feedback]
    Why: "the captions keep changing". Lines got different treatments and colours that the creator hadn't asked for.
    Check: every caption state uses the same font, weight, size, ink colour and effect. A diff of the style parameters across states must be empty.

57. **Time captions from the heard audio, not from cut points.** [measured-failure]
    Why: captions snapped to the nearest cut showed lines a frame late or held them too long.
    Check: each caption's in and out sit within 1 frame of the forced-alignment word times.

58. **Keep air between letters and keep glyph edges clean.** [creator-feedback]
    Why: "messed up with random stuff on the side… not clean". Letters fused, traced specks and stair-step edges showed.
    Check: at least 4 px between adjacent letter inks at delivery size. No connected ink component under 40 px that isn't part of a letter. Bed brightness merging into a letter's outer ring stays ≤ 10%.

59. **Do not blur things on the creator's own clothing or props. If something really must go, re-pick the shot.** [creator-feedback]
    Why: "why is my jersey blurred out? Stop the blurring."
    Check: no blur or redact patches in the render graph. Compare Laplacian sharpness of the old patch regions against the previous version.

60. **Short-form: use elegant serif or italic or small clean type, never impact or typewriter fonts.** [creator-feedback]
    Why: references with aggressive heavy fonts were rejected as not matching the brand's look.
    Check: the caption font family is on an approved list (serif, italic or clean sans).

61. **When the creator says "make the font like video X", copy X's whole caption model.** [creator-feedback]
    Why: copying only the typeface, then refitting size, tracking or colour, still "didn't look like" the target.
    Check: a side-by-side crop of the source video's caption next to ours shows the same face, tracking, size, ink and effect.

---

## 8. Graphics & text overlays

62. **Make graphics look like designed UI, not code drawings.** [creator-feedback]
    Why: "it just seems like it's made out of code… make it more realistic and cleaner". Flat procedural shapes were rejected.
    Check: build graphics with a real layout engine (HTML/CSS-class), real map or photo assets, and true frosted blur of the footage behind. Compare against a designed reference card.

63. **Use fewer graphics. Remove any that feel AI-made or busy.** [creator-feedback]
    Why: an age/title glass pill, animated flight maps, a cycle diagram, spec callouts (bedroom counts), price callouts and a "banned" card were all removed as "too much going on, too much AI".
    Check: at most one graphic per talking section, and only where it explains something the footage cannot show. Every graphic needs the creator's explicit keep.

64. **Keep graphics off the face. Put paired cards on opposite sides of the frame, clear of the speaker.** [creator-feedback]
    Why: two weather cards both sat on the creator's face.
    Check: graphic bounding boxes ∩ face boxes = ∅ on every frame.

65. **Keep graphic text minimal: the place name, the number, nothing extra.** [creator-feedback]
    Why: extra tags on a weather card were "doing too much"; the creator said to just show the city and the temperature.
    Check: each card has at most 2 text fields, and every word on it must match the spoken line.

66. **Show a location or activity card only once the creator is actually at that location, and keep its tone playful, not aggressive.** [creator-feedback]
    Why: a card for the venue appeared before arrival, in a hard style.
    Check: the card timestamp is ≥ the first shot tagged inside that location.

67. **For an abstract claim (time zones, a process), offer one simple explainer visual, then cut it if the creator calls it AI-looking.** [creator-feedback]
    Why: the creator first asked for a motion graphic on a time-zone line, then rejected the animation as "too much AI".
    Check: mark explainer graphics as provisional in the review notes and remove them unless the creator approves them.

68. **When the creator talks about a tool or screen, show a clean on-screen visual of it instead of a generic shot.** [creator-feedback]
    Why: "show an image of [the tool], something cool on the screen while speaking".
    Check: the cutaway or graphic shows the named object, and OCR or eye check confirms it is the right one.

69. **OCR every graphic in the render to catch missing glyphs and overlapping text.** [measured-failure]
    Why: an arrow glyph rendered as a box, and two labels overlapped.
    Check: OCR each graphic frame and compare it with the intended string. Bounding boxes of text runs must not overlap.

---

## 9. Music

70. **Put no music under the creator's talking.** [creator-feedback]
    Why: "I can hear the music when I'm speaking after the montage."
    Check: music bus RMS during voice-activity spans ≤ −40 dBFS, or fully muted.

71. **Match the music level to the moment: low under calm action, full only on montages.** [creator-feedback]
    Why: "while I'm leaving, the music is too high for what I'm doing."
    Check: music loudness in non-montage action stays at least 8 LU under montage loudness.

72. **Match the music mood to the content.** [creator-feedback]
    Why: a hype rap track over a tender memory was rejected, and so was a song the creator simply disliked on a walk.
    Check: tag each chapter's mood (hype, chill, nostalgic). The track's energy and valence tag must agree. Leave no placeholder songs in a delivered cut.

73. **When the creator says "use the reference's songs", identify each one exactly and start it at the reference's offset.** [creator-feedback]
    Why: guessing at tracks missed the feel.
    Check: run audio fingerprinting plus chroma cross-correlation to find each track and its offset. Log the song and offset per chapter.

74. **Use the clean studio master of a song, not a vocal-stripped rip.** [measured-failure]
    Why: the source-separated teaser bed sounded watery.
    Check: compare spectral flatness and artefact energy above 8 kHz against the studio master, and prefer the master.

75. **Land the song's drop on the first cut of a montage, and change songs at chapter boundaries.** [measured-failure]
    Why: when a shot was removed upstream, the work song stopped landing on its montage.
    Check: after any timeline edit, recompute the song offsets. The drop time must equal the montage's first cut within ±1 frame.

76. **Short-form remakes: mux the reference's original audio untouched and time captions to what is heard. Never swap in another song.** [creator-feedback]
    Why: "put the original audio… I don't know why the song is playing."
    Check: the packet MD5 of our audio equals the reference download, and a transcription without hints matches the captions.

77. **Short-form: never lay someone else's spoken voice-over over the creator's footage.** [creator-feedback]
    Why: "the audio is just someone else recording" over lifestyle shots that didn't match the words.
    Check: if the reference audio contains speech (voice-activity share > 30%), replace it with an approved music track cut to the same beat grid.

---

## 10. Voice & audio cleanup

78. **Isolate the voice from background noise on every talking clip.** [creator-feedback]
    Why: "the background noise in some clips is a lot."
    Check: noise floor in speech gaps ≤ −55 dBFS after cleanup, and per-clip SNR is reported.

79. **Cap noise reduction so the voice doesn't sound thin or "podcast" in busy rooms.** [creator-feedback]
    Why: aggressive denoise made a restaurant scene sound processed.
    Check: attenuation limit about 20 dB, and 12–15 dB in busy rooms. Energy at 2–8 kHz stays within 3 dB of the source on voiced frames.

80. **Never leave digital-silence holes. Fill gaps with room tone.** [measured-failure]
    Why: a denoise gate created a −68 dB hole that sounded like "such a loud silence".
    Check: scan the talk track in 0.25 s RMS windows. Any window below −65 dBFS gets room tone at about −50 dBFS.

81. **Keep lip-sync at zero drift across the whole video.** [measured-failure]
    Why: sync drifted to 1.5 s by the end. Causes: video rounded to frames while audio wasn't, clips with a nonzero start time lost a frame, loudness normalisation trimmed the tail, and a length flag cut audio before video.
    Check: cross-correlate the final audio against each source segment at its start and end, and require 0 frames of offset everywhere.

82. **Keep true peak at or below −1.5 dBTP, measured on the encoded file.** [measured-failure]
    Why: a sample-peak limiter let inter-sample peaks reach −0.7 dBTP after the encode.
    Check: run the loudness meter with true-peak on the delivered file, and lower the gain until TP ≤ −1.5.

83. **Put sound effects only on visible events (flashes, hard punches), using the reference's own SFX when available.** [creator-feedback]
    Why: "use the sound effects" from the reference. Synthetic generic SFX felt off.
    Check: every SFX onset sits within ±1 frame of a visual event.

---

## 11. Shot quality (shake, exposure, faces, strangers, repeats)

84. **Reject shaky handheld walking footage. Swap in a locked-off shot or stabilize it.** [creator-feedback]
    Why: "too shaky… gives it a poor vibe, not well thought about."
    Check: a camera-shake metric from feature-tracked jitter (high-pass of the trajectory). Reject anything above the library median for walking shots (the rejected clip scored 14, its replacement 0.5).

85. **Reject unflattering face shots: bad lighting, odd expressions, near-lens faces and zooms into an awkward face.** [creator-feedback]
    Why: "I hate the lighting on it, my face looks like a…"; "I'm making a weird face, you just zoom into it"; "a clip I look stupid in".
    Check: face luma inside the 25–85% band, no punch-in past 1.2x on a face, and an eye check of the face at mid-shot.

86. **Reject banned poses for the creator: squatting, hunched over, head in hands, slumped.** [creator-feedback]
    Why: "why am I squatting like that?" Head-in-hands shots read as sad, which is off-brand.
    Check: pose estimation on first, middle and last frames. Reject hip-below-knee, head-below-shoulders and hand-on-head poses.

87. **Reject smoking, cigars, vapes, clip-on mics, tripods and crew in frame.** [creator-feedback]
    Why: "no cigar for intro". Mics, transmitters and a camera operator in reflections all shipped once.
    Check: object detection plus an eye check at ≥ 960 px on first, middle and last frames of every shot.

88. **Reject any shot with a stranger visible at any size, including in glass, mirrors or car paint.** [measured-failure]
    Why: strangers walking through the lounge, beach-goers, diners and reflected figures all shipped. Detectors missed most of them.
    Check: run person detection on full frames and 3×3 tiles, then an eye pass at ≥ 960 px, with every glossy surface checked.

89. **Reject readable signage, screens, plates, credentials and brand marks that could identify the creator or a venue.** [measured-failure]
    Why: a school sign, a wifi password board, venue banners and readable screens reached a render.
    Check: OCR every 2nd–4th decoded frame at native resolution. Any legible identifying text means recast or crop. Also judge at phone width (390 px) for what viewers can actually read.

90. **Reject blurry, dark or "shows nothing" shots.** [creator-feedback]
    Why: "that's a bad clip, it doesn't show anything"; blurry sunset shots; a dark shadow cutaway.
    Check: Laplacian variance above a sharpness floor. Mean luma ≥ 35 for night shots with a lit face. Reject near-black share > 30%.

91. **Keep the creator's whole head in frame with headroom.** [measured-failure]
    Why: heads were cropped at the top edge in crops, short bursts and action shots.
    Check: the face-box top plus 0.45 × face height must stay inside the frame on every frame, with ≥ 6% headroom.

92. **Never use the same setup twice within 10 s, and never show the same clip twice in a section.** [creator-feedback]
    Why: "it's like the same clip you just showed me twice"; "change the clip after that, it's a repeat". Different files from the same setup still read as a repeat.
    Check: cluster shots by setup (same place, light and framing; adjacent clip numbers). No two shots from one cluster within 10 s, and at most one back-to-camera beat per setup.

93. **Limit one location family (pool, beach, sea) to one wide per piece unless the story is set there.** [creator-feedback]
    Why: "so many beach clips for no reason"; "most of these pool clips are bad"; "too much pool".
    Check: count shots per location family. Wides with the creator tiny in frame (face < 7% of frame height) count as filler.

94. **Use one consistent house grade. Never grade shots individually unless the creator names a shot.** [creator-feedback]
    Why: six rounds of per-shot matching produced "random colours". The approved look was one LUT on every shot.
    Check: the grade chain string is identical for every shot. Mean colour shifts between adjacent shots are reported, and cool swings against a warm look are flagged.

95. **Measure colour, softness or audio complaints at the cited frame, then fix them. Never wave them off as "not a defect".** [creator-feedback]
    Why: a colour note was dismissed by the editor, and the creator escalated.
    Check: report before and after numbers (mean RGB outside the text band, edge width, packet MD5) at the frame the creator named.

96. **Never cut vertical phone footage into a 16:9 frame, or the reverse.** [measured-failure]
    Why: rotated vertical files looked whole on squashed contact sheets but rendered as headless torsos.
    Check: read the display-matrix rotation and require display width > height for every 16:9 shot.

97. **Do not use the tail of a clip that runs into a camera whip or smear.** [measured-failure]
    Why: shots ended on blur with the head leaving the frame, and once the piece ended on a smear.
    Check: max frame difference over [in, in + duration] ≤ 2× the clip's median.

98. **Make the closing shot the most aspirational frame in the piece.** [creator-feedback]
    Why: a stiff stand-and-stare with props in the foreground was "not my vibe". A clip rejected as one closer was reused as another.
    Check: the closer shows the creator in motion through a clean, bright lifestyle setting. Keep a list of rejected closers and grep it before casting.

99. **Short-form: keep the mood aspirational lifestyle, never sad, dark grind or gym-aggression content.** [creator-feedback]
    Why: references that were slow, dark-ambient or "sad grind" were rejected wholesale.
    Check: mean luma per shot ≥ 35, no despair poses, and an eye pass of every distinct clip against the taste list before rendering.

100. **Never turn a reel into a slideshow of still images. Every beat is real moving footage at real speed.** [creator-feedback]
     Why: a 26-still photo-dump version was "horrible… just images".
     Check: the in-shot frame difference of every shot is above the near-still threshold.

---

## 12. Pacing

101. **Match the reference's shot length for each section type instead of one global pace.** [measured-failure]
     Why: our first cuts were too slow at the front and too busy in talking sections.
     Check: measured reference average shot lengths were intro talk 8.2 s, desk talk 5.6 s, sit-down 3–5 s, walk 1.6 s and montage 0.34–1 s. Ours must be within ±30% per section.

102. **Cut walking-through filler that doesn't move the story.** [creator-feedback]
     Why: "walking through the gallery area doesn't need to be there… 3 seconds of the camera blurred looking at an alleyway."
     Check: flag any non-talking shot over 2 s with no story tag (arrive, leave, show, react).

103. **Shorten any single shot the creator calls "too long". They mean make it quicker and more rapid.** [creator-feedback]
     Why: "way too long, make it quicker, more rapid."
     Check: after the fix, the flagged span's average shot length is ≤ 0.8 s or its total length is roughly halved.

104. **Short-form: keep it under 20 s, cutting at about 1.7–2.8 cuts per second on the beat.** [measured-failure]
     Why: genre hit rate halved past 20 s, and every slow reference (≤ 0.7 cuts/s) was rejected on taste.
     Check: duration < 20 s and cuts per second within 1.7–2.8, all on beats.

105. **Short-form: one moving beat for each word or held line. Do not race cuts faster than the beat.** [measured-failure]
     Why: on the creator's own account, fewer and cleaner cuts kept watch time better than frantic cutting.
     Check: no cut lands off-beat, and no shot is shorter than one beat interval unless it is a device.

---

## 13. Review loop process

106. **Lock everything the creator has praised. Change only what they name.** [creator-feedback]
     Why: "why did you change the other clips, it was better before"; "order is going to shit, I swear it was good before".
     Check: compare frames of every untouched section against the previous version (PSNR ≥ 45 dB), with matching audio MD5 for untouched sections.

107. **When the creator says "it was better before", restore the earlier version exactly, then apply only the new change.** [creator-feedback]
     Why: a rebuilt intro and a re-trimmed heartfelt line both regressed, and the creator asked for the earlier cut back.
     Check: the restored section matches the old version frame for frame. Then diff only the named change.

108. **Map every timestamped note to the cut list before acting.** [measured-failure]
     Why: the paused frame was not the shot the creator meant ("the second clip"). A window like "the 4–5 second clips" covers every shot on screen inside it.
     Check: resolve each note by shot order and content on a decoded still. List every shot overlapping the named window.

109. **The newest note wins over an earlier one about the same shot.** [creator-feedback]
     Why: the creator first asked for smoking shots in the intro, then for fewer, then for none. Following the old note re-broke it.
     Check: keep a per-shot note log and apply the latest ruling only.

110. **Run a side-by-side comparison against the reference for every review.** [creator-feedback]
     Why: "give me the comparison side by side". Gaps against the reference were only obvious that way.
     Check: publish a two-up player with a section jump list, plus a 10 fps contact sheet of the reference cuts against ours.

111. **Make a contact sheet at every cut of the decoded final and check it by eye before sending.** [measured-failure]
     Why: thumbnail-size checks missed strangers, mics, signage, head cuts and a frozen shot that the full-size check caught.
     Check: one frame per side of every cut at ≥ 960 px, plus first, middle and last of every shot.

112. **Prove cut timing on the delivered file, not the edit list.** [measured-failure]
     Why: stream-copy joins put every cut one frame late and dropped one-frame shots.
     Check: a frame-difference spike must sit exactly on each planned cut frame. The decoded frame count must equal the timeline length.

113. **Confirm the review link actually plays before sending it.** [creator-feedback]
     Why: "isn't loading", "doesn't work, please fix". The preview server was down twice.
     Check: an HTTP range request on the URL returns 206 and the first seconds decode.

114. **Hand over a beat sheet with the cut so story problems are easy to spot.** [creator-feedback]
     Why: "are you sure all the story is right? Show me the story." A beat list with thumbnails sped up their review.
     Check: deliver a page with chapter → line → thumbnail → shot description for every beat.

115. **Ask questions as one short question with numbered options, next to where the creator comments.** [creator-feedback]
     Why: three-paragraph questions were confusing and went unanswered.
     Check: each question is ≤ 320 characters and has numbered options.

116. **If the creator names a moment the footage doesn't contain, offer it as an on-screen caption on the nearest real beat.** [creator-feedback]
     Why: a requested "lights out" moment didn't exist in the footage, and the caption option was accepted.
     Check: search the footage first (light steps, action tags). If it's absent, propose caption plus nearest beat as option 1.

117. **Run one independent audit per version, fix every finding, prove each fix and deliver. No repeated audit rounds.** [creator-feedback]
     Why: "stop having these audits, fix them". Repeated audits delayed delivery.
     Check: the delivery note lists each finding with its fix and the machine proof on the final file.

118. **An approval that names a change is conditional. Build only that change, then record the approval on the new version.** [measured-failure]
     Why: "approve, but change the last clip" nearly shipped with the unwanted clip.
     Check: parse approvals for change verbs. If any are present, rebuild with only that change and diff everything else against the previous version.

---

## 14. Craft (measured on a reference vlog)

Source: a frame- and sample-level audit of a creator's favourite reference vlog against the approved
predecessor cut and a cutroom render. These rules hold the measured numbers. Renderer support is in
`docs/PLAN_FORMAT.md` under "Craft".

119. **Never let the noise floor step at a jump cut. Room tone runs under all dialogue, and every join is an equal-power crossfade.** [measured-failure]
    Why: the reference's floor steps 2.2 dB (median) across talk-to-talk cuts. The approved predecessor stepped 9.5 dB and the creator heard "loud silence".
    Check: QA `room_tone_steps`: median |step| ≤ 3 dB, p90 ≤ 8 dB, and the quietest 250 ms inside talk is above −55 dBFS.

120. **The house grade is the LUT and nothing else. Exposure normalisation, the finishing curve, vibrance, saturation and looks are opt-in, only when the creator asks. Never brighten after the LUT, and never crush the toe.** [measured-failure]
    Why: the stack after the LUT (a log exposure pull on every bright shot, a finishing curve with vibrance, saturation 1.05 and a night look at 40 %) took the approved LUT-only picture from black point 33 / saturation 54 to black point 2 / saturation 104 on the same frame; the creator called it "insane". Earlier, a post-LUT brightness lift put the black point at 28/255 and the night interior went milky. The approved v023 is LUT-only and measures p1 29–41 on daylight; the reference measures 8.
    Check: QA `black_level`: median p1 between 8 and 35 (not crushed, not milky), and no daylight shot below p1 4. QA `grade_bounds`: every clip shot within ±15 luma of its LUT-only probe, black point no more than 12 below it, saturation at most +15 %. The sidecar's `grade_style` shows the opt-ins.

121. **Build the cold open from teaser lines that pay off later. Cut it dense (about 0.45 s, faster than the beat), and keep the music full under the teaser voice.** [measured-failure]
    Why: the reference opens with 22 shots in 10.4 s. Its first word comes at 1.43 s, and the music runs 3.5–7 dB under the voice without ducking. The rough cut opened with 8 shots at 1.33 s each, then a title card.
    Check: the cold open has ≥ 4 cuts in the first 3 s. Every teaser line comes from a talk segment later in the edit.

122. **Start and stop songs hard on the section cut. Lift the music about 7 LU over montage and b-roll. Break the music for 0.2–0.4 s before a flash. Duck only under real dialogue.** [measured-failure]
    Why: in the reference, the bass falls 20 dB within one 10 ms frame at section cuts, and the montage music sits 7 LU above the talking. Natural sound under b-roll used to duck the music 18 dB.
    Check: music edges are within a frame of a cut, and the music under natural-sound b-roll is within 3 dB of the montage level.

123. **A flash is the reference's own device, in the reference's own shape. When the reference burns (the two-frame film burn: a red glow rising from the lower left on the last outgoing frame, an orange-yellow disc with a magenta fringe entering from the right on the first incoming frame, the cut between them), use `transition.kind: "burn"`. When it strobes, use 3-frame inset shots on black and the inset↔gate↔full-frame snaps. Mix-to-white flashes are only for references that have white flashes. Every flash sits on a real event and its one SFX lands with the transient peak on the cut frame.** [measured-failure]
    Why: the reference's flashes are 2-frame warm burns (16 at section and location changes; the biggest hit of the intro at 7.32 s) and luma swings between an inset on black and full frame. v2's mix-to-white dips (luma 205–237) read as a CapCut white flash and the creator said the flashes were missing. Pushing exposure past about +1 stop through the LUT turns magenta, so a white flash mixes toward white rather than pushing exposure.
    Check: QA `burn_timing`: the glow is on the frame before the cut and the disc on the cut frame, nowhere else. QA `flash_sfx_alignment`: a white flash peaks on the cut frame ±1, and every transition sound's transient lands within ±5 ms. QA `card_present` fails when the reference opens on a card and the render does not.

124. **Lead the voice into the face (J-cut, about 0.3 s) and let it trail over b-roll (L-cut). Keep the b-roll's own sound under the music.** [measured-failure]
    Why: in the reference, speech is already running before the cut in 33 of 40 cuts into talk. B-roll keeps its location sound at about −35 dBFS between lines.
    Check: `lead_in`/`tail` show up as audio before/after the picture in the sidecar (`audio_pre`/`audio_post`), and captions follow the heard words.

125. **Captions are about 43 px at 1080 (text about 3 % of the frame height), white, with no outline.** [measured-failure]
    Why: the rough cut's 54 px captions were 1.7× the reference's size.
    Check: QA `caption_size`: 2.4–4.0 % of the frame height.

126. **Conform 23.976 fps footage to 25 without duplicate frames: speed up b-roll by 4.3 %, frame-blend sync talk.** [measured-failure]
    Why: 271 of 395 masters in the library are 23.976 fps, and `fps=25` repeats a frame about once a second (visible judder on pans).
    Check: QA `duplicate_frames` reports 0 in moving shots.

127. **Give long held shots a slow push-in (3–6 % over 4 s or more) as a slider stand-in. Never zoom-punch a jump cut.** [measured-failure]
    Why: the reference's long talk holds drift 3–9 %. It punches in on only 1 of 110 same-setup cuts.
    Check: push values stay within 1.03–1.08. On talk, the push runs across the whole segment, not per piece.

128. **Vary the pace by section: fast blocks (ASL 0.5–0.9 s) between long holds (ASL 5–10 s), and cap the longest hold.** [measured-failure]
    Why: the reference's section ASLs run 0.47 / 10.3 / 1.85 / 0.67 / 6.3 / 0.85 / 8.8 / 0.63 / 1.8 / 4.7 / 2.2 / 5.0 s. One global pace reads flat.
    Check: `section.pace` takes the reference's ASL for that section type. No talk hold runs past `max_hold` unless it is on purpose.

## 15. Voice-over edits (measured on a brand film cut to a voice memo)

129. **Cut a voice-over at its sentence breaks and pauses, never inside a word.** [measured]
    Why: in the reference, 8 of the 14 cuts under the voice memo land on a sentence break or pause; none lands inside a word. The cuts follow her words (9 of 16 within 0.13 s of a word onset), not a beat (the score is beatless).
    Check: `cutroom breaks <memo>` lists the gaps; `cutroom plan check` hints every voice-over `in`/`out` that falls inside a word, and when fewer than 40 % of the voice-over cuts sit on a break.

130. **Put hands and extreme close-ups under the most tender lines, and the brightest, most joyful shot under the peak line.** [measured]
    Why: the reference lays hands holding hands under "I don't even know how you do it" and "Love you", and its brightest shot (a girl clapping, p50 117) under "I'm so proud of you".
    Check: the planner names the tender lines and the peak line from the transcript and picks shots by the tags (ECU / hands / smiling) and measured luma; review the strip.

131. **The closing burst remembers the film: replay at least three earlier images.** [measured]
    Why: the reference's 13-shot memory burst (4–21 frames per shot, colour against black and white) repeats 4 earlier shots (the bowed head, the child in the panelled room, the pedalling legs, the profile).
    Check: `cutroom plan check` hints a late montage of 6+ short shots with fewer than 3 shots within 1.5 s of an earlier use of the same clip.

132. **Take the act structure from the reference's measured section shares, not from a template.** [measured]
    Why: the reference is noise 13.7 % / silence 15.2 % / voice 38.7 % / leap 28.2 % / end card 4.3 %; the silence and the end card are acts in their own right.
    Check: `cutroom ref show <id>` prints `acts (share of runtime)`; give each plan section its `ref_span` and `plan check` hints a section more than 5 points off its act's share.

133. **A stylised reference's grade is the look: switch it by shot, and match it in reference look mode.** [measured]
    Why: 70 % of the reference is neutral low-key black and white, 25 % one cyan tint; its blacks are crushed on purpose (p1 3), which the daylight match guards forbid.
    Check: `cutroom looks ref <id>` gives the look per reference shot; the rebuild uses `bw_contrast` / `teal_night` on the segments that stand in for them (teal neutralises white balance first).

