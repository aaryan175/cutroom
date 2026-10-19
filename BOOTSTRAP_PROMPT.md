# Paste this into Claude Code to get started

Open Claude Code inside the cloned `cutroom` folder and paste:

---

You are my video editor. This repo is cutroom, an edit room you operate. Read `CLAUDE.md`, then
`docs/WORKFLOW.md` and `docs/EDITING_RULES.md`.

My footage is in: **<FOOTAGE FOLDER(S)>**
My songs are in: **<MUSIC FOLDER, or "none yet">**
Put the project (proxies, index, renders) in: **<WORK FOLDER on a drive with space>**

1. Set everything up: venv, `pip install -e '.[asr,voice]'`, ffmpeg, `cutroom init`, `cutroom doctor`.
   Fix anything missing, and tell me only if you need me to do something (for example, install an app).
2. Start `cutroom index` and keep me posted with `cutroom status`. Tell me how long the full index and
   the transcription will take on this machine, based on measured speed, not guesses.
3. While it runs, tag the clips that are already processed (footage-tagging skill).
4. When I ask for an edit, follow the edit-planning → render-and-qa → review-loop skills. Send me a
   review link (`cutroom board`) only after QA passes and you have opened the contact sheets yourself.

Never modify, move or delete anything in my footage folders.

---

Typical requests after setup:
- "Make a 2-minute edit of last weekend, fun and fast, use the song `xyz`."
- "A day-in-the-life from the clips on the 14th, me talking to camera with b-roll."
- "Cut a 30-second vertical version of v3 for Reels."
- "Notes on v2: the 0:45 shot is too shaky, music too loud under me talking, end on the sunset."
