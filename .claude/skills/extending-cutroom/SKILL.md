---
name: extending-cutroom
description: Use when the creator asks for something the tool can't do yet, such as a new transition, effect, caption style, graphic, UI button, analysis, export format or integration, or when something in cutroom is broken. Covers where to change the code, the add-a-feature recipe, invariants and how to verify.
---

# Extending cutroom

The creator doesn't code; you do. When they ask for something new ("can it do zoom punches on the
beat?", "make the captions pop word by word", "add a button to export a vertical version"):

1. **Check it doesn't already exist.** Look in `docs/PLAN_FORMAT.md`, `cutroom --help` and `docs/STUDIO.md`.
   Many requests are a plan field or a few overlays.
2. **Read `docs/ARCHITECTURE.md`.** It has the module map, the add-a-feature recipe and the invariants.
3. **Make the change end to end:**
   - plan schema
   - renderer
   - `resolve_timeline`
   - UI
   - QA check
   - docs
   - a test on synthetic footage
4. **Run `pytest -q`.** It must be fully green.
5. **Prove it on their footage.** Add it to their current draft, render a preview, open frames or the
   contact sheet, and look.
6. **Tell the creator in one or two plain sentences** what they can now say or click. Show the result
   on their own clip (a `[[clip:...]]` or `[[t:...]]` chip in the Studio), not the code.

## Bug reports from the creator
"It froze", "the audio is out of sync", "the captions are on my face":
1. Reproduce it on their render. The sidecar JSON maps time to piece.
2. Find the root cause in the module that owns it.
3. Fix it, then add a test or QA check that would have caught it.
4. Re-render and verify.

Never patch around a bug in the plan file when the renderer is wrong.

## Keep the machine safe while you work
- Heavy commands queue through the governor. If they wait, `cutroom governor` shows why.
- Never run two transcriptions at once.
- Keep footage out of `tests/`; use ffmpeg lavfi to make test clips.

## The factory's extender (planned)
When a factory order needs a capability cutroom lacks, it becomes an `extend` sub-order (needs you). The
extender role is a stub in this build: build the feature by hand with this skill (plan.py → render.py → UI →
qa.py check → tests), then answer the sub-order's question. Never apply a factory-built patch without the
creator pressing Apply. A lesson that needs new measuring code becomes a `qa.py` check or a
`cutroom/factory/lint.py` check type, with a test.
