---
name: render-and-qa
description: Use after a plan passes `cutroom plan check`, to render a preview or final video, run machine QA, look at the contact sheets and fix problems before anything is sent to the creator.
---

# Render, QA, fix

1. Run `cutroom render plans/<slug>_vN.json --preview`. It renders a 720p draft from proxies, with the
   same timing as the final.
2. Run `cutroom qa renders/<slug>_vN_preview.mp4`. It writes `.qa.json`, a `.qa.html` page and contact
   sheets in `renders/<slug>_vN_preview.mp4.qa/`.
3. **Open the contact sheets and look at them.** Machine checks miss strangers in reflections,
   unflattering faces, wrong-place cutaways and captions sitting on the face. For every cut, ask:
   - Does this shot show what's being said, where the creator actually is right now?
   - Is the creator the subject, clean and well lit, with the whole head in frame?
   - Does the cut visibly change the picture?
   - Is anything off-brand (props, crew, private text, a stranger)?
4. Fix every **fail** in the plan, bump `version`, re-render (cached segments make this fast) and
   re-run QA.
   - **Silence holes.** Lower `voice.atten_lim_db`, or extend the talk piece's pad.
   - **Frozen frames.** The in-point plus duration runs past the end of the clip; move the in-point.
   - **Invisible cuts.** The two shots look alike; replace one.
   - **Caption on a face.** Set `captions.position` to `"top"` for that segment, or re-pick the shot.
   - **Off-beat montage cut.** Check the section's song offset, and that `beat_sync` is on.
   - **Too dark.** Choose another window (`cutroom clip <id>` shows exposure per 2 s).
5. When the preview is clean, run `cutroom render plans/<slug>_vN.json` (full quality from masters),
   then `cutroom qa` on the final once more.
6. Run `cutroom board` and send the creator the link. **Open the link first and confirm the video plays
   and scrubs.**
7. Add a one-line explanation for each remaining **warn** in your message to the creator, plus a short
   beat sheet: time, line, shot.
