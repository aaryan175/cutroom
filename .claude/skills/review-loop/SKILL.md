---
name: review-loop
description: Use when the creator gives feedback on a render, as timestamped notes on the board, voice-to-text messages or screenshots, to turn every note into an exact plan change and deliver the next version without breaking what they liked.
---

# Turning feedback into the next version

Creators type fast and casually: "the 1:45 walk is too shaky", "too much AI", "it was better
before", "the music under me talking". Every note is a specific instruction.

1. **Collect.** Use `cutroom notes` for board notes, plus anything they sent in chat.
2. **Map each note to the timeline.** Open the previous render's sidecar JSON
   (`renders/<name>.json`). It lists every atomic segment with its timeline start and end, source
   clip and in/out.
   - A timestamp names a *window*. List every shot on screen within ±1 s and decide which one they
     mean by content: "the water one", "the person walking left".
   - If it's still ambiguous, pull the frame (`ffmpeg -ss T -i render.mp4 -frames:v 1`) and look.
3. **Write a change list** before touching the plan. One line per note: note → segment → change.
   Show it to the creator only when a note is genuinely ambiguous. Otherwise just do it.
4. **Change only what was named.**
   - Copy the plan to `_v(N+1)` and edit that copy.
   - Everything else stays identical: same clips, same in/outs, same order.
   - Anything the creator praised is locked.
5. **The newest note wins.** If an old note asked for X and a new note rejects X, X goes. Keep a
   short `notes/decisions.md` log of rulings ("no smoking shots in intros", "no music under talking",
   "callouts off") and re-read it before every version.
6. **"It was better before."** Restore that part from the earlier plan exactly, then apply the new
   change on top.
7. **Translating common notes.**
   - **"too long"** means make it quicker, more rapid: shorter shots, not just a trimmed end.
   - **"doesn't make sense"** means a story or continuity problem: wrong place, wrong time, or a
     missing journey shot.
   - **"too much going on" / "too AI"** means remove graphics and effects; keep fewer, cleaner ones.
   - **"shaky"** means swap in a locked-off or steadier window (`analyze` metrics), or stabilize.
   - **"audio is bad / silence"** means the voice cleaning is too strong (lower `atten_lim_db`), or
     there is a hole to fill.
   - **"remove that clip"** means remove it and close the gap. Don't leave a black frame or a jump in
     the music.
8. Render, QA, then deliver the version with a short list: what changed, at which timestamps.
   Anything you couldn't place goes in one short question with options.
