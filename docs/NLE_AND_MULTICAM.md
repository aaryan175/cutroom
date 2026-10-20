# NLE hand-off and multicam sync

Two features for a creator who shoots events on several cameras, records sound separately, and finishes the edit in
Adobe Premiere Pro:

1. **Open in Premiere** (`cutroom export-nle`, `cutroom/nle.py`) turns any plan into a timeline that Premiere, DaVinci
   Resolve or Final Cut can open. The timeline links to the original camera masters.
2. **Multicam and dual-system audio sync** (`cutroom sync`, `cutroom/sync.py`) finds clips that were recorded at the same
   time and lines them up by their sound. Plans can then cut between angles while keeping the best audio.

## 1. Open in Premiere

```bash
cutroom export-nle plans/show_v7.json                      # Premiere XML (default)
cutroom export-nle plans/show_v7.json --format all --zip   # + FCPXML 1.10, OTIO, EDLs, packed as a zip
cutroom export-nle plans/show_v7.json --clean-render       # rendered effects cut from a caption-free render
cutroom export-nle --verify renders/nle/show_v7            # re-read every file, compare to the plan frame by frame
```

In the Studio, use **Render ▾ → Open in Premiere…** (or **Open in Resolve / Final Cut…**). This runs the same command
as a job on the latest saved version. When the job finishes, a panel shows the folder, the zip and the steps.

### What is in the folder (`renders/nle/<slug>_v<N>/`)

| File | What it is |
|---|---|
| `<slug>_vN.xml` | FCP7 xmeml v4, which Premiere opens with File → Import. Clips link to the **original masters** by absolute path. Each file carries its own timebase and NTSC flag (23.976 is `timebase 24`, `ntsc TRUE`) and its start timecode. The sequence uses the plan's frame size and rate. |
| `<slug>_vN.fcpxml` | FCPXML 1.10 for DaVinci Resolve (File → Import → Timeline) and Final Cut Pro. V1 sits on the primary storyline; everything else is connected clips in lanes. Source starts sit on each master's own frame grid, and audio is sample-exact. |
| `<slug>_vN.otio` | OpenTimelineIO, written only when the optional `opentimelineio` package is installed (`pip install -e '.[nle]'`). |
| `<slug>_vN_V1.edl`, `_V2.edl`, `_A1.edl` | CMX3600 fallback, one per track, with `M2` speed lines and the full source path as a comment. |
| `captions.srt` | The plan's captions at the times they are heard. |
| `graphics/G*.mov` | Callouts, lower thirds, typed titles and title cards as **ProRes 4444 with alpha**, placed on V3. |
| `rendered/FX*.mov` | Anything Premiere can't rebuild natively, cut frame-exactly out of a cutroom render as ProRes 422 HQ and placed on V4: COMING UP cards, film burns, flash / whip / dip transitions, gate / inset / letterbox frames, freeze frames and speed ramps. The original clips stay on V1 underneath, so the editor can rebuild or re-cut. |
| `README_PREMIERE.txt` | The exact steps, the track map, the LUT path and what is approximate. |
| `nle.json` | The manifest: every clip with its track, master and frames. `--verify` reads it. |

**Tracks:**
- **V1** main picture.
- **V2** cutaways.
- **V3** graphics.
- **V4** rendered effects.
- **Audio:** dialogue on A1 (and A2, which takes J/L-cut overlaps), natural sound on A3/A4, music on A5.

**Timing**
- Every clip sits at its exact cutroom frame. Talk tightening produces many short clips on purpose.
- Clip speed comes from the plan: speed, frame-rate conform (25p b-roll in a 23.976 sequence) and the base speed of a
  ramp. It is written as Premiere's Time Remap speed, an FCPXML `timeMap`, and EDL `M2` lines.
- Audio edges that fall between frames (crossfade handles, J/L leads) are moved to the nearest frame. The source in
  point moves by the same amount, so nothing slides against the picture.

**Levels and markers**
- **Music:** the song file goes on the music track, with volume keyframes where it ducks under speech. These are Audio
  Levels keyframes in xmeml and `adjust-volume` in FCPXML.
- **Markers:** sections, segment notes, multicam choices, looks, push-ins, ramps, freeze frames, open review comments, the
  cold-open note and a `GRADE` marker with the LUT path.

**Grade:** the LUT is not baked in. The README says to add a video track after V2, put an adjustment layer on it and
load the LUT in Lumetri → Basic Correction → Input LUT.

### What is verified, and what is not

`cutroom export-nle --verify DIR` runs a fresh `render.resolve_timeline` of the plan and compares every clip:
- the timeline start and end frames must be exact;
- source in and out must land on the right frame (or sample, for FCPXML audio);
- the file must be the master;
- speed must match.

It also runs schema-like structure checks:
- **xmeml:** required elements, every file reference defined, no overlapping clips on a track;
- **FCPXML:** version 1.10, unique resource ids, every ref resolves, a contiguous spine on the frame grid.

OpenTimelineIO's own readers then parse the same files independently. The readers for FCP7 XML and CMX3600 must agree
frame by frame. OTIO's FCPXML reader (otio-fcpx-xml-adapter 1.0.0) is reported as informational only, because it counts
frames with `int(float(...))` and lands several frames off on 1001-based or mixed-rate timelines.

Tests (`tests/test_multicam_nle.py`) round-trip a 25 fps edit and a 23.976 edit. The 23.976 edit includes a 25p clip
conformed by speed, an off-grid source in point and J-cut audio. The tests also cut rendered effect segments and check
that their frame counts match their spans.

**Not verified on real Premiere.** Premiere is not installed on the build machine. Read the xmeml as "should import";
the first real import is the true test. These points were written from the FCP7 XML conventions and are the most likely
to need adjusting:
- the units of the Basic Motion `center` (face-crop framing);
- whether volume keyframe `when` values are media-relative;
- how Premiere reads a nested multicam sequence.

### Real proof on the creator's footage

On a clone of `real_proj_v6` (`real_proj_v6_nle`), `intro_match_v7` was exported to all formats in about 26 s. The
export contains:
- 34 V1 clips and 1 V2 clip;
- 15 audio clips, including the music with ducking keys;
- 6 ProRes 4444 graphics;
- 3 rendered V4 segments (the whole reference-style cold open, a burn and a freeze).

`--verify` reported:
- 50/50 clips frame-exact in both the xmeml and the FCPXML;
- 34/34 in the EDL;
- 34/34 through OTIO's FCP7 and CMX readers and the `.otio` file;
- clean structure checks.

No final render of v7 existed, so the V4 segments were cut from the 720p preview, scaled up, with captions burned in.
The README warns about both. Use `--clean-render` to get a caption-free 1080p cut.

## 2. Multicam and dual-system audio sync

```bash
cutroom sync                                   # detect + align (the overnight `sync` stage runs this after analyze)
cutroom sync list                              # groups: angles, offsets, drift, confidence, loop error
cutroom sync devices                           # the cameras and recorders found
cutroom sync name camA "Cam A wide"            # what the creator calls them
cutroom sync name rec1 "Zoom H6" --kind recorder
cutroom sync name camC "Lav" --kind lav        # a lav recorder: ranks between recorder and camera sound
cutroom sync --clips C0001 C0101 ZOOM0001      # align a hand-picked set (no timestamps needed)
cutroom sync map 3 812.4 --from camA --to camB # the same moment on another angle
cutroom sync switch plans/show.draft.json 1.4 camB   # move a multicam segment to another angle
```

### How it finds groups

1. **Recorder audio.** WAV, BWF, MP3, M4A, FLAC and AAC files inside the library folders (never the music folders) are
   registered as clips with status `audio`. They get no proxy, sheet or analysis, but transcription does pick them up.
   Their waveform shows in the Library.
2. **Metadata** (`clip_media`):
   - make, model and serial (QuickTime tags, Sony/Canon XML sidecars, the BWF originator);
   - start timecode (ffprobe `timecode`, BWF `time_reference`);
   - capture time (`creation_time`, BWF origination date and time, sidecar `CreationDate`, otherwise file mtime minus
     duration).

   A camera is identified by make, model and serial. When there is no serial (two identical bodies), a folder name
   matching `sync_device_hint` tells the bodies apart (`A_CAM`, `CamB`, `ZOOM`, `REC`, `LAV`…).
3. **Candidates.** Clips from different devices whose capture spans overlap, within `sync_window_s` (5 min) of clock
   slack, plus clips whose timecode-of-day spans overlap. A second pass tries quarter-hour clock offsets from −14 h to
   +14 h, but only for device pairs that matched nothing in the first pass. This catches a recorder left on another
   time zone (BWF times are local wall-clock times). The audio still has to agree.

### How it aligns

1. **Coarse offset.** A 100 Hz onset envelope is made per clip: positive log-spectral flux at 8 kHz, 60 Hz–3.5 kHz, with
   a 0.5 s moving average removed. It is cached in `cache/sync/`. A normalised FFT cross-correlation over every lag gives
   the coarse offset.
2. **Acceptance.** The peak value must be ≥ `sync_min_conf` (0.25) and above the noise floor (6/√overlap). The peak must
   also be ≥ `sync_min_ratio` (1.35) times the best rival outside ±0.5 s, which rejects beat-periodic music matches.
3. **Drift.** 30 s envelope windows across the overlap give local offsets.
4. **Refinement.** Up to 12 two-second GCC-PHAT windows on the 8 kHz waveform refine each local offset to well under a
   millisecond. A robust line fit then gives offset + drift (ppm). Drift is reported when it exceeds 2 ppm over at
   least 20 s.
5. **Groups.** Accepted pairs form a graph, and each connected component is a group. Offsets are composed along the
   maximum-confidence spanning tree from the reference member (the longest video clip). Every other measured pair closes
   a loop; the worst disagreement is stored as `loop_err_ms`. Group ids and names survive re-runs. Pairs are cached in
   `sync_pairs`, so later nights only measure new clips.

**Group time** is measured in seconds on the reference member's clock; 0 is the earliest member's first sample. A
member's own time `t` maps to group time `g = offset + t·(1 + drift_ppm·1e-6)`.

**Measured on the synthetic test shoot.** The shoot has two cameras 3.37 s apart with different mics and noise, and a
recorder that starts 3 s earlier with its clock running 150 ppm fast:
- camera offsets came back within 0.06–0.07 ms;
- recorder drift came back as −152.1 ppm (true value −149.98);
- loop error was under 1 ms;
- a rendered multicam edit's sound matches the recorder within 0.7–1.2 ms;
- an unrelated camera rolling at the same time was rejected.

### Database (`sync.ensure_schema`)

| Table | Holds |
|---|---|
| `devices` | `key`, `code` (camA, camB, rec1…), `make`, `model`, `serial`, `kind` (camera / recorder / lav), `name`, `tc_sync` |
| `clip_media` | per clip: device, timecode, capture start and its source, audio-only flag |
| `sync_pairs` | every measured pair: lag, drift, corr, ratio, overlap, fine windows, rms, why rejected |
| `sync_groups` / `sync_members` | the groups; per member: angle, offset, drift, confidence, kind, video flag, audio rank |

### In plans

```json
{"type": "talk", "clip": "C0101", "in": 812.4, "out": 828.0,
 "multicam": {"group": 3, "angle": "camB", "audio": "auto"}}
```
- **Picture** comes from the angle's clip.
- **Sound** comes from the best member: external recorder > lav > camera (among cameras, the angle's own). It is mapped
  through the measured offset and drift. `"audio"` can be `"camera"` (the angle's own sound) or any angle or recorder
  code.
- **Tightening and captions** use the recorder's words when the recorder has a transcript.
- **In and out.** With `"clip"`, `in` and `out` are in that clip's seconds. If `clip` is another angle, they are mapped
  through the group, so changing only `"angle"` is enough. Without `"clip"`, they are group seconds.
  `cutroom sync switch` and the Studio rewrite `clip`, `in` and `out` to the new angle so every other tool sees a plain
  segment.
- **Validation.** `plan check` says when an angle or the sound source doesn't cover the range.

**Premiere export of multicam.** The main sequence already contains the cut: each multicam segment is the chosen angle's
master on V1 and the recorder on the dialogue track. Each group used also gets a **"MC <group>" source sequence** in a
"Multicam source sequences" bin, with every angle on its own video track and every sound source on its own audio track,
all at their measured offsets. To re-cut by hand in Premiere, nest it, then use Multi-Camera → Enable. A native multicam
clip in xmeml (`<multiclip>`) was not used. Premiere's support for it on import is undocumented and fragile, and a
mistake there would scramble the edit. Stacked synced angles are plain clips that every NLE reads the same way. In
FCPXML each group becomes its own project. Note that xmeml cannot place audio between frames, so recorder sound can sit
up to half a frame off picture there (≤ 20 ms at 25p); FCPXML keeps it sample-exact.

### Library and Studio

- **Library:** clips in a group get an `MC camA` badge. The Days view shows "N multicam" per day, with each group in
  the tooltip.
- **Inspector:** a multicam segment shows its angles as buttons and a Sound picker. Switching is the plan op
  `multicam_angle`, so it is one undo step.
- **API:** `GET /api/multicam` returns the groups and devices.

## 3. What I need from the creator's camera list

There is no real multicam footage in the test library. The pipeline is generic, but these answers decide the defaults:
the device hint, the time-zone slop, whether timecode can be trusted, and which sound is "best".

### Questionnaire

1. **Cameras.** For each body: make and model (e.g. "Sony FX3", "A7S III", "Canon R5 C", "GoPro 12", "iPhone 15
   Pro"), how many of each, and whether two bodies are the same model.
2. **Codecs and containers.** XAVC S/HS (MP4), XAVC (MXF), ProRes (MOV), H.264/H.265 from phones, BRAW, R3D. Do you
   record internal proxies?
3. **Log profile.** S-Log3 / S-Gamut3.Cine, C-Log2/3, V-Log, HLG, or Rec.709. Do you use one LUT for everything? Send
   the `.cube` files you use in Premiere.
4. **Frame rates.** The project rate (23.976 / 25 / 29.97 / 50 / 59.94). Do you shoot slow motion (50/60/120 in a 25
   or 24 timeline)? Do any cameras record at a different rate than the timeline?
5. **Timecode.** Free-run time-of-day, record-run, or none? Are the cameras jam-synced (Tentacle, Deity TC-1, UltraSync)
   and the recorder too? If so, timecode can decide a match when the audio can't (e.g. a silent angle).
6. **Camera clocks.** Are all cameras and recorders set to the same time zone and roughly the right time? A device on
   UTC or home time still syncs, but it is slower to find.
7. **Audio.**
   - Recorders: Zoom H4n / H5 / H6 / F3, Tascam, Sound Devices, Rode Wireless PRO / GO internal recording, DJI Mic,
     or a desk/board feed.
   - What each one is used for: lav on the speaker, room ambience, board mix.
   - The file format (BWF WAV 32-bit float or 24-bit?) and whether files are split every 2 GB / 4 GB.
   - Does every camera also record scratch audio (needed for sync)?
8. **Long takes.** The typical length of one camera file at an event (whole sets? split files?). Long takes are where
   drift matters.
9. **NLE.** The Premiere Pro version (and OS). Do you also use Resolve or Final Cut? Should Premiere open the XML
   straight from the footage drive, or do you copy media to a project drive first (and what is that drive called)?
10. **Card and folder naming.** How you offload: one folder per camera per day (`2026-05-01/CamA/…`), per card
    (`A001`, `B002`), or the raw card structure (`PRIVATE/M4ROOT/CLIP/C0001.MP4`)? Do you rename files? This decides how
    cutroom tells two identical bodies apart (`sync_device_hint`).
11. **Angles.** What each camera usually covers (wide, close, roaming, crowd, DJ booth) and what you want them to be
    called in the edit ("Cam A wide", "Cam B close").
12. **Delivery.** Do you want the Premiere sequence to cut between angles already (the default), or only the synced
    multicam sequences to cut yourself?

### What changes with the answers

- **Folder naming** becomes `sync_device_hint` in `cutroom.json`, and the names become `cutroom sync name …`.
- **Lav or board recorders** get `--kind lav` or `--kind recorder`. That sets the audio ranking.
- **Jam-synced timecode** sets `tc_sync=1` on those devices.
- **The project frame rate** becomes the plans' `output.fps`. NTSC rates already export as `timebase 24/30/60` with
  `ntsc TRUE`.
- **Log profile and LUT** go into `style.grade.lut` and the README's LUT step.
