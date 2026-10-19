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
| analyze | shots, shake, exposure, sharpness, faces (+ a smile hint and 5 perceptual hashes per clip) | heavy slots (worker processes) |
| dedupe | byte-identical copies and near-duplicates (perceptual hashes, same folder or close in capture time). One representative per group is tagged; the others inherit its tags | light |
| highlights | a 0–1 score per 2 s window from audio peaks (applause / crowd, laughter / cheering, loudness that stands out), camera-motion peaks and faces | heavy slots (audio decode only) |
| sessions | shoots (capture-time gaps over `session_gap_min`) and GPS places (`place_radius_m`) | light (one ffprobe per clip, once) |
| transcribe | speech check on every clip (no Whisper model is loaded for it), then word-level ASR of the clips with speech, most speech first. Exact copies share one transcript | the single ASR slot |
| takes | lines said more than once in a shoot, ranked: the later take wins unless it is incomplete, shaky, soft or faceless | light |
| songs | tempo and beat grid for the music folders | heavy slot |
| matchcuts | match-cut candidates across the library. Skipped if nothing changed | memory-guarded |
| refs | measure reference videos that are not analysed yet (`refs/` and anything dropped in `renders/reference/`): cut map, pacing, sections, beat sync, loudness, captions, transcript. See `cutroom ref` | heavy slots + the single ASR slot |
| tag | the AI agent looks at each contact sheet and writes tags (`db.TAG_FIELDS`, `"tagged_by": "claude-overnight"` or `"codex-overnight"`). Before the first call it logs the plan: untagged, skipped by the quality gate, duplicates that inherit, calls to make | Claude Code or Codex, headless |
| upgrade | only with `asr_fast_model`: the clips with speech again with `asr_model` (the accurate pass), most-used first | the single ASR slot |

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
   `~/.cutroom/locks` (machine-wide across all projects).
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
| `proxy_height` | 540 | proxy height; 360 halves the disk and is enough for tagging and sheets (preview renders then look softer) |
| `proxy_bitrate` | "" | hardware-encoder proxy bitrate; `""` = 1200k at 540p, 600k at 360p |
| `asr_device` | "" | `cpu`, `cuda`, or `""` (CUDA when present) |
| `asr_remote` | "" | run ASR elsewhere: a command template with `{audio}` `{out}` `{model}` `{language}`, e.g. `ssh gpu-box /opt/cutroom/.venv/bin/python -m cutroom.asr {audio} {out} --model {model}`. Paths must be on a shared mount. The local ASR slot is still held, so it is still one transcription at a time |
| `asr_fast_model` | "" | two passes: everything with this model first (e.g. `small`), the `upgrade` stage redoes clips with speech with `asr_model` |
| `tag_gate` | true | skip AI tagging for clips that can't be useful (they stay searchable; set false to tag them later) |
| `tag_min_dur` / `tag_gate_dark` / `tag_gate_shake` | 1.0 / 6 / 12 | the gate: shorter than this many seconds, median luma below this (0–255), median shake above this |
| `dedupe_hamming` / `dedupe_window_h` | 6 / 2 | near-duplicate: mean perceptual-hash distance (of 64 bits); compared within a folder or this many hours of capture time |
| `session_gap_min` / `place_radius_m` | 45 / 150 | a capture gap this long starts a new shoot; GPS points this close share a place |

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

## Big libraries (4 TB and more)

Run this first. It reads the library (after `cutroom index --skip-proxies --skip-analysis`, which only
scans and probes, in minutes) and projects every stage from speeds measured on this machine:

```bash
cutroom estimate                 # the remaining work for this library
cutroom estimate --tb 4          # the same footage mix scaled to 4 TB
cutroom estimate --bench         # nothing measured yet? a 30 s benchmark on one real clip
cutroom estimate --copy-db       # safe to run while an overnight run is writing the database
```

Speeds come from `overnight/rates.json` (every overnight run records how fast each stage really went),
the live `overnight/state.json`, `--bench`, or, failing all of those, assumptions that are labelled
"assumed" in the output.

Planning numbers for a 4 TB library of 4K camera footage (about 100 h, about 12,600 clips at a mean of
28.5 s), measured on a shared 10-core machine under heavy load:

| stage | as configured before | with the scale settings |
|---|---|---|
| proxies | 1.67× realtime, throttled → **60 h**; 117 GB (2.5 Mb/s at 540p) | 540p at 1.2 Mb/s → **54 GB**, or 360p at 0.6 Mb/s → 27 GB. A faster or second machine: 8–12 h |
| analysis | ~13.6× realtime per process → 10–12 h | 2 workers: 4–6 h; perceptual hashes and the smile hint come in the same decode |
| sheets | ~2 h, 1.4 GB | same |
| transcription | 30–40 % speech ≈ 35 h of audio; `medium` on CPU at 1–4× → **9–35 h** | the speech check skips silent clips first; `asr_fast_model: "small"` gives searchable speech in a third of the time, the `upgrade` stage redoes it later; `asr_remote` on a GPU machine: 1–2 h |
| AI tagging | 1,575 calls × (20–90 s + 20 s) ≈ **17–48 h**, likely hitting plan limits | quality gate (about −10–30 %) and duplicate inheritance (about −10–20 %) before any call; the plan is logged first |
| disk | ≈ **120 GB** | ≈ 30–60 GB + per-edit voice WAVs and render cache (2–3 GB per edit) |
| database | ≈ 210–250 MB | same, plus the scale tables (a few MB) |

How to run a big library:

- **Edit before everything is indexed.** Search or open the clips an edit needs (or put them in a plan):
  every stage does recently-used clips first, so an edit can start after the first few hours.
- **Proxies decide the disk.** `proxy_height: 360` + `proxy_bitrate: ""` for a library you mostly
  search; keep 540 when you preview-render from proxies a lot. Changing these affects new proxies only.
- **Transcription is the long pole.** `asr_fast_model: "small"` first; `asr_remote` to put Whisper on
  a GPU machine (the far side runs `python -m cutroom.asr AUDIO OUT.json --model M`).
- **Tagging is the cost.** Leave `tag_gate` on; duplicates inherit tags (`"inherited_from"`); look at
  the logged tag plan or `cutroom overnight --dry-run` before a big run.
- **Use the structure.** `cutroom search --highlights`, `--dedupe`, `--session N`, `--near <clip>`;
  `cutroom detect sessions` lists the shoots; `cutroom takes` shows repeated lines and the best take.
- **Not done (yet):** people clustering (face embeddings) and CLIP-style semantic embeddings. Both need
  a model download and a GPU to be worth it at 4 TB; the perceptual hashes cover duplicates and
  look-alikes without one.

## Checking and stopping

```bash
cutroom overnight --status        # per-stage done/total, ETA, cost of tagging, throttle reason, last error
tail -f overnight/overnight.log   # everything, including restarts
cutroom overnight --stop          # graceful: finishes the current item, writes the report
                                  # (work still queued for a slot is dropped, never started)
```

Files in `<work>/overnight/`:

| file | contents |
|---|---|
| `state.json` | live progress. The studio polls it |
| `overnight.log` | the full log |
| `REPORT.md` | the morning report |
| `overnight.pid` | the supervisor's pid while it runs |
| `failures.json` | per-clip crash and failure counts |
| `rates.json` | measured speed of each stage per run (what `cutroom estimate` projects from) |
| `usage.jsonl` | clips you touched recently, which drives the priority order |
