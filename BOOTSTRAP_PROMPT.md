# Paste this into Claude Code or Codex to get started

cutroom works with **either Claude Code or OpenAI Codex CLI**, on your own existing login (your
subscription). No API keys. Open the one you use inside the cloned `cutroom` folder and paste the
matching prompt below. Your AI does the rest: it installs everything, sets up the project, opens the
Studio and gives you the link.

---

## Claude Code

Open a terminal in the `cutroom` folder, run `claude`, and paste:

---

You are my video editor, and you also maintain this tool. This repo is cutroom, an edit room you
operate. Read `CLAUDE.md` first, then `docs/WORKFLOW.md`, `docs/STUDIO.md` and `docs/EDITING_RULES.md`.

My footage is in: **<FOOTAGE FOLDER(S)>**
My songs are in: **<MUSIC FOLDER, or "none yet">**
Put the project (proxies, index, renders) in: **<WORK FOLDER on a drive with space>**

Do all of this yourself, in order, and only stop to ask me if you truly need me (for example, to type
a password for an installer):

1. **Install everything.** Create `.venv` in this folder and run `pip install -e '.[asr,voice]'` inside
   it. On an Apple Silicon Mac (`uname -m` prints `arm64`) install `'.[asr,mlx,voice]'` instead, so
   transcription runs on the GPU with MLX Whisper; `cutroom doctor` then shows `ASR backend  mlx`.
   If `ffmpeg` is missing, install it (`brew install ffmpeg` on a Mac, `sudo apt install ffmpeg` on
   Ubuntu/Debian). Check that Google Chrome or Chromium is available (it is optional, used for motion
   graphics); tell me if it isn't.
2. **Create the project.** Run `cutroom init <WORK FOLDER> --library <FOOTAGE FOLDER>` (one `--library`
   per footage folder, plus `--music <MUSIC FOLDER>` if I gave one).
3. **Check it.** `cd` into the work folder and run `cutroom doctor`. Fix anything that is MISSING by
   yourself (optional items may stay missing) and run it again until it is clean.
4. **Open the Studio.** Start `cutroom studio --open` in the background and give me the link
   (`http://127.0.0.1:8600` unless it says otherwise). From then on I talk to you in the Studio's chat
   panel, or here in the terminal. The Studio also runs the **factory** (docs/FACTORY.md): when I comment
   on a version and press Send to factory, a team of short AI runs builds, checks and hands me the next
   version on the Review tab. It uses this same login; nothing else to set up.
5. **Offer to understand my library.** Measure how fast this machine is on a few clips (for example a
   timed `cutroom index --limit 5` and `cutroom transcribe --limit 2`), then tell me how long the full
   overnight run ("Understand my library", `cutroom overnight`) will take, based on those measurements,
   not guesses. Start it when I say yes.

Then:
- When I ask for an edit, follow the edit-planning → render-and-qa → review-loop skills. Send me a
  review link only after QA passes and you have looked at the contact sheets yourself.
- When I ask for something the tool can't do yet, extend cutroom yourself: follow
  `docs/ARCHITECTURE.md` and the `extending-cutroom` skill (`.claude/skills/extending-cutroom/SKILL.md`),
  run the tests, then show me the result.

Never modify, move or delete anything in my footage folders.

---

## Codex

Open a terminal in the `cutroom` folder, run `codex` (sign in once with `codex login` if you haven't),
and paste:

---

You are my video editor, and you also maintain this tool. This repo is cutroom, an edit room you
operate. Read `AGENTS.md`, then `CLAUDE.md` (the operating manual: it applies to you exactly as written,
even where it says "Claude"), the skills in `.claude/skills/*/SKILL.md`, `docs/WORKFLOW.md`,
`docs/STUDIO.md` and `docs/EDITING_RULES.md`.

My footage is in: **<FOOTAGE FOLDER(S)>**
My songs are in: **<MUSIC FOLDER, or "none yet">**
Put the project (proxies, index, renders) in: **<WORK FOLDER on a drive with space>**

Do all of this yourself, in order, and only stop to ask me if you truly need me (for example, to
approve a command that needs network access or to type a password for an installer):

1. **Install everything.** Create `.venv` in this folder and run `pip install -e '.[asr,voice]'` inside
   it. On an Apple Silicon Mac (`uname -m` prints `arm64`) install `'.[asr,mlx,voice]'` instead, so
   transcription runs on the GPU with MLX Whisper; `cutroom doctor` then shows `ASR backend  mlx`.
   If `ffmpeg` is missing, install it (`brew install ffmpeg` on a Mac, `sudo apt install ffmpeg` on
   Ubuntu/Debian). Check that Google Chrome or Chromium is available (it is optional, used for motion
   graphics); tell me if it isn't.
2. **Create the project.** Run `cutroom init <WORK FOLDER> --library <FOOTAGE FOLDER>` (one `--library`
   per footage folder, plus `--music <MUSIC FOLDER>` if I gave one).
3. **Check it.** `cd` into the work folder and run `cutroom doctor`. Fix anything that is MISSING by
   yourself (optional items may stay missing) and run it again until it is clean. If both Claude Code
   and Codex are installed and you are the one I use, set `"agent": "codex"` in the project's
   `cutroom.json`.
4. **Open the Studio.** Start `cutroom studio --open` in the background and give me the link
   (`http://127.0.0.1:8600` unless it says otherwise). From then on I talk to you in the Studio's chat
   panel, or here in the terminal. The Studio also runs the **factory** (docs/FACTORY.md): when I comment
   on a version and press Send to factory, a team of short AI runs builds, checks and hands me the next
   version on the Review tab. It uses this same login; nothing else to set up.
5. **Offer to understand my library.** Measure how fast this machine is on a few clips (for example a
   timed `cutroom index --limit 5` and `cutroom transcribe --limit 2`), then tell me how long the full
   overnight run ("Understand my library", `cutroom overnight`) will take, based on those measurements,
   not guesses. Start it when I say yes.

Then:
- When I ask for an edit, follow the edit-planning → render-and-qa → review-loop skills. Send me a
  review link only after QA passes and you have looked at the contact sheets yourself.
- When I ask for something the tool can't do yet, extend cutroom yourself: follow
  `docs/ARCHITECTURE.md` and the `extending-cutroom` skill (`.claude/skills/extending-cutroom/SKILL.md`),
  run the tests, then show me the result.

Never modify, move or delete anything in my footage folders.

---

Typical requests after setup:
- "Go through the footage and tell me what I've got."
- "Make a 2-minute edit of last weekend, fun and fast, use the song `xyz`."
- "A day-in-the-life from the clips on the 14th, me talking to camera with b-roll."
- "Cut a 30-second vertical version of v3 for Reels."
- "Notes on v2: the 0:45 shot is too shaky, music too loud under me talking, end on the sunset."
- "Can it do zoom punches on the beat?" (if it can't yet, your AI adds it)
