# Overnight understanding

Click **Understand library** in the studio, or run this before bed:

```bash
cutroom overnight --until 07:00
```

By morning the whole library is indexed, transcribed, analysed and match-cut-mined, and the AI agent (Claude Code or Codex) has looked at
every contact sheet and tagged the clip. The summary is in `overnight/REPORT.md`.

## What it does

| stage | what | runs in |
|---|---|---|
| scan, probe | find new or changed files and read their formats | light |
| proxies | small H.264 copies (hardware encoder) | heavy slots |
| sheets | one contact-sheet JPEG per clip | heavy slots |
| analyze | shots, shake, exposure, sharpness, faces | heavy slots (worker processes) |
| transcribe | speech check on every clip, then word-level ASR. Clips with the most speech go first | the single ASR slot |
| songs | tempo and beat grid for the music folders | heavy slot |
| matchcuts | match-cut candidates across the library. Skipped if nothing changed | memory-guarded |
| tag | the AI agent looks at each contact sheet and writes tags (`db.TAG_FIELDS`, `"tagged_by": "claude-overnight"` or `"codex-overnight"`) | Claude Code or Codex, headless |

- **Priority.** Clips you touched recently come first in every stage: searched, opened with
  `cutroom clip`, used in a plan, or marked in the studio. After those come the longest clips, or the clips
  with the most speech.
- **Tagging overlaps transcription.** Tagging starts as soon as analysis is done and runs alongside
  transcription. Each call sends the agent about 8 clips. For each clip it sends the id, the file name, the
  duration, a metrics summary, a transcript snippet and the sheet. **Footage never leaves the machine. The
  agent only sees the sheet JPEGs.** Claude Code opens them itself with its Read tool (`--tools Read`).
  Codex gets them attached to the prompt (`codex exec -i <sheet>...`, at most 8 per call), runs with
  `-s read-only --ephemeral`, and its final message is forced into a JSON schema (`--output-schema`) and
  read back from `-o <file>`. Both replies go through the same parse and validation. Clips you have
  already tagged are never overwritten.
- **Resumable.** Progress lives in the database. If you stop, interrupt or restart, the next run continues
  where the last one stopped.
- **Crash-proof.** A supervisor runs the stages in a child process. If the child crashes or is killed for
  memory, the supervisor restarts it after a back-off. Any clip that was being processed during two
  crashes is marked `error` with the reason, so one corrupt file can never loop forever.
- **Keeps the computer awake.** On macOS it runs `caffeinate -i`, which stops idle sleep but lets the
  display sleep. On Linux it uses `systemd-inhibit`.
- **Needs Claude Code or Codex for tagging.** Tagging uses your own login (subscription, no API key) of
  whichever agent the project uses: `"agent"` in `cutroom.json` is `"auto"` (the one that is logged in;
  Claude Code when both are), `"claude"` or `"codex"`; `CUTROOM_AGENT` overrides it. `cutroom doctor`
  shows which agents are installed and logged in. If the agent is missing (or Codex is not logged in),
  the tag stage is skipped with a message and everything else still runs. A usage-limit message from
  either agent stops the tag stage cleanly; the next run picks up the rest.

## Options

```
--until 07:00     stop gracefully at this local time (the current item finishes first)
--no-tag          skip AI tagging
--tag-limit N     tag at most N clips this run
--model small     ASR model (small = faster, medium = more accurate, the default comes from cutroom.json)
--only tag        run only these stages (comma-separated)
--dry-run         everything except the AI calls; writes overnight/tag_prompt_preview.txt
--status          progress, ETA, throttling, last error
--stop            graceful stop
```

## Safety limits (the governor)

These are the ways this class of machine has crashed before, and what the governor does about each:

1. **One process grows huge.** For example, numpy reaching 26 GB triggered a jetsam kill and a WindowServer
   reboot. The governor measures **physical footprint**, which is what macOS acts on, not RSS. It checks
   every child once a second and stops any child that passes its cap. In-process work stops itself: the
   match-cut search retries in smaller chunks, ASR drops and reloads its model, and the stage process gets
   restarted.
2. **Two transcriptions at once.** These caused a kernel panic. There is exactly **one ASR slot** for the
   whole machine, shared by the CLI, the studio and the overnight run through lock files in
   `<work>/.locks`.
3. **The disk fills up.** Proxy and render writes **pause** when free space drops below the floor, with a
   message, and resume once space is freed.

All the limits live in `cutroom.json`. A value of `0` or `""` means automatic.

| key | default | meaning |
|---|---|---|
| `max_load` | cores × 0.8 | above this 1-minute load average, heavy work drops to 1 slot |
| `min_free_ram_gb` | max(2, 15 % of RAM) | new heavy and ASR work waits below this. Below 60 % of it, the biggest running child is stopped |
| `max_proc_mem_gb` | min(4, 25 % of RAM) | cap per child process (ffmpeg, analysis worker). In-process work (ASR model, match-cut matrices) may use up to min(8, 35 % of RAM) |
| `min_free_disk_gb` | 20 | free-space floor on the project volume |
| `heavy_slots` | cores ÷ 4 (at least 1) | concurrent encodes and analysis across all cutroom processes |
| `nice_level` | 10 | priority of child processes, so the desktop stays responsive |
| `pause_when_user_active` | false | macOS: drop to 1 heavy slot while the keyboard or mouse has been used in the last 2 min |
| `quiet_hours` | "" | for example `"09:00-18:00"`: 1 heavy slot inside this window |
| `governor` | true | `false` turns throttling off. The single ASR slot always stays |
| `agent` | "auto" | `auto`, `claude` or `codex`: who tags (and who answers in the studio chat) |
| `tag_batch` | 8 | clips per tagging call (Codex: at most 8) |
| `tag_sleep_s` | 20 | pause between tagging calls |
| `tag_model` | "" | Claude Code only: for example `"sonnet"` for cheaper tagging. `""` uses your Claude Code default |
| `codex_model` | "" | Codex only: model for tagging. `""` uses the Codex default |

Throttling never stops work completely. It only reduces it to one slot. Waiting is logged with the reason
(`waiting for a heavy slot: free RAM 1.9 GB < 2.4 GB`). Run `cutroom governor` to see the live state:
load, free RAM and disk, who holds each slot, who is waiting, and the running children with their
footprints.

## Expected timings

These are rough figures for a 2 TB library of 4K camera footage, about 40–60 h of material.

- **Scan and probe:** minutes.
- **Proxies:** 1–3 h with the hardware encoder.
- **Sheets and analysis:** 1–2 h.
- **Transcription:** the slow part. Roughly 1–4× realtime on CPU with `medium`, about twice as fast with
  `small`. Only clips with speech are transcribed.
- **Tagging:** about 20–90 s per Claude call of 8 clips, plus `tag_sleep_s`. That is about 300–600 clips
  an hour, subject to your plan's usage limits. When the agent reports a usage limit, tagging stops
  cleanly. The next run picks up the rest. (Codex timings are not measured yet.)

A first night may not finish everything on a very large library. Run it again the next night; it
continues where it stopped.

## Checking and stopping

```bash
cutroom overnight --status        # per-stage done/total, ETA, cost of tagging, throttle reason, last error
tail -f overnight/overnight.log   # everything, including restarts
cutroom overnight --stop          # graceful: finishes the current item, writes the report
```

Files in `<work>/overnight/`:

| file | contents |
|---|---|
| `state.json` | live progress. The studio polls it |
| `overnight.log` | the full log |
| `REPORT.md` | the morning report |
| `overnight.pid` | the supervisor's pid while it runs |
| `failures.json` | per-clip crash and failure counts |
| `usage.jsonl` | clips you touched recently, which drives the priority order |
