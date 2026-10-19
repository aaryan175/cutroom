---
name: library-indexing
description: Use when setting up cutroom on a new footage library or drive, when new clips were added, or when indexing, transcription or proxies stalled. Covers init, doctor, index, transcribe, songs, status and time and disk estimates for 300 GB–2 TB+ libraries.
---

# Indexing a footage library

## First run
1. `cutroom init <work_dir> --library <footage_dir> [--library <second_dir>] [--music <songs_dir>]`
   - Put `<work_dir>` on a fast internal or SSD drive with free space of roughly 3% of the library
     size, plus 20 GB for render caches. The footage can stay on any external drive.
   - The footage folders are only read. Nothing is written into them.
2. `cd <work_dir> && cutroom doctor`. Fix anything marked MISSING that isn't optional.
   - **macOS:** `brew install ffmpeg`.
   - **Linux:** your distro's ffmpeg with libx264.
   - **Python packages:** `pip install -e '.[asr]'` from the repo, for transcription.
3. `cutroom index`. This runs scan → probe → proxies → contact sheets → shot detection → quality
   metrics. Resumable: if it is interrupted, run it again.
4. `cutroom transcribe`. Every clip with speech gets word-level timestamps.
   - It is the slowest step. Run it overnight for big libraries.
   - Use `--limit N` to do the most speech-heavy clips first.
   - **Never run two transcriptions at once.** They compete for the CPU and can exhaust memory.
5. `cutroom songs index`. Indexes tempo, beats and likely drops for every song in the music folders.
6. `cutroom status`. Shows counts and what is left to do.

## Rough timings (one modern laptop)
| Step | Speed | 1 TB of 4K footage (~40–60 h) |
|---|---|---|
| scan + probe | thousands of files/min | minutes |
| proxies (hardware encoder) | 10–40× realtime | 2–5 h |
| sheets + analysis | 20–60× realtime | 1–3 h |
| transcription (medium model, CPU) | 1–4× realtime on speech-bearing clips only | overnight |

Run `cutroom status` to report progress. To estimate time left, divide the remaining hours of
footage by the measured speed.

## When things go wrong
- **A clip is stuck with status `error`.** Run `cutroom clip <id>` to see the error. Usually the file
  is corrupt or still being copied. Skip it; it never blocks the rest.
- **The library drive is unplugged.** Proxies and sheets are still in the project, so tagging and
  planning keep working. Final renders need the masters, so plug the drive back in before rendering.
- **New footage was added.** Run `cutroom index` again; only new or changed files are processed.
- **Log footage (S-Log, V-Log, C-Log, HLG)** looks flat on sheets. Set `"lut": "/path/to/camera.cube"`
  in `cutroom.json` (the creator's own LUT) so renders are graded. Without one, a generic contrast curve
  is applied and a warning is printed.
