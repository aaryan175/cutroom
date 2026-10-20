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

## 3. Your kit: ARRI AMIRA hero, Pocket 6K Pro, X-T5, Sony S-Log3, Rode Wireless PRO, PAL

The creator shoots events on mixed cameras, matches everything to the ARRI, records sound on Rode Wireless PRO units
(internal recording), cuts in Premiere and delivers 25 fps (50 fps for slow motion). Cards are dumped together per shoot
day, so folders say nothing about the camera. This section says what cutroom does for that kit, what is automatic,
what is assumed, and what the creator has to provide.

```bash
cutroom index                      # identifies every clip's camera + log profile while probing
cutroom cameras                    # cameras found, clips per day, profiles (metadata / assumed), transform used
cutroom cameras clips              # every clip in (clock-corrected) shooting order with its camera and profile
cutroom cameras set DSCF0042 --profile fuji_flog2 --all     # correct a clip, or (--all) every clip of its camera
cutroom sync                       # groups, claps, split recorder takes, per-camera clock offsets
cutroom sync clocks                # how far each camera's clock is off the ARRI, per day
cutroom sync recordings            # split recorder takes joined into one recording
cutroom cameras match              # fit each camera's per-day correction toward the ARRI (ΔE before -> after)
cutroom cameras luts               # the per-camera .cube files for Premiere's Lumetri Input LUT
cutroom braw                       # Blackmagic RAW / ARRIRAW clips and how each one is decoded
```

### Camera identity (automatic, per clip, from metadata)

| Camera | How it is recognised | Log profile |
|---|---|---|
| ARRI AMIRA | QuickTime make/model or brand words in the file's own header/trailer atoms; ARRI file naming (`A001C003_YYMMDD_R1AB`, weak alone) | LogC3: read from tags when present, otherwise **estimated** from the picture (`assumed`) |
| Blackmagic Pocket 6K Pro | `manufacturer` / `camera_type` / `camera_id` tags (the names Blackmagic writes, checked on Blackmagic's own sample .braw), `.braw` container, `.sidecar` files, BMD naming (`A001_MMDDhhmm_C001`), 6K frame sizes | `viewing_gamma` "Blackmagic Design Film" = Film Gen 5 (metadata); "Video" = Rec.709 |
| Fujifilm X-T5 | make `FUJIFILM` / model `X-T5` tags or the same words in the file atoms, `DSCF####` naming, X-T5 frame sizes | F-Log2 is not tagged by the camera: **estimated** from the picture (`assumed`) |
| Sony (S-Log3) | the `XAVC` brand, the XML sidecar (`C0001M01.XML`: manufacturer, model, serial) | `CaptureGammaEquation` / `CaptureColorPrimaries` from the sidecar (metadata). S-Gamut3 (not .Cine) is LUT-only |
| anything else | HLG from the `arib-std-b67` transfer tag; other log from file names or the histogram | `log_unknown` keeps the old single-LUT path |

Each clip stores `camera_make`, `camera_model`, `camera_serial`, `log_profile`, how it was found (`metadata`, `filename`,
`assumed`, `user`) and a confidence. The Studio library shows a camera badge on every clip (amber with `?` = assumed),
a **Camera** filter chip, and clicking the badge corrects the camera or profile for that clip or every clip of the camera.
Brand words are only searched in printable text runs, so random bytes in compressed picture data can't name a camera.

**What is assumed here:** the exact metadata an AMIRA and an X-T5 write was not available (no real files from either).
The recognisers use the generic QuickTime keys, brand strings and naming; the first real card dump is the test.

### Colour: everything matched to the ARRI

One combined LUT per clip, applied where the house LUT was (`render.grade_chain`), so the doctrine holds: one
transform, plus at most one look.

1. **Camera input transform** to the ARRI's space (LogC3 EI 800 / ARRI Wide Gamut 3):
   - **maths, from published specs** (cited constant by constant in `cutroom/colorsci.py`, unit-tested against the
     documents' own reference values): ARRI LogC3 EI 800 + AWG3 (ARRI "ALEXA Log C Curve – Usage in VFX"), Sony S-Log3
     + S-Gamut3.Cine (Sony "Technical Summary for S-Gamut3.Cine/S-Log3"), Fujifilm F-Log2 + F-Gamut = BT.2020
     (Fujifilm "F-Log2 Data Sheet"). The curves apply to the signal after the usual legal-range expansion; checked on
     this machine against Fujifilm's own F-Log2 LUT (black lands at 95/1023) and Resolve's ARRI LogC 1D LUT
     (identical to 7 decimals).
   - **LUT only (no formula implemented)**: Blackmagic Film Gen 5. Its constants circulate, but no copy on a
     Blackmagic domain could be checked here, so cutroom does not use them. It needs Blackmagic's "Gen 5 Film to
     Video" LUT: `luts/bmd.cube` (or `luts/<camera key>.cube`); when DaVinci Resolve is installed, cutroom finds
     Blackmagic's own copy in Resolve's LUT folder. Same for LogC4, F-Log (v1) and S-Gamut3 (non-Cine).
2. **Show transform** (the ARRI look): `luts/arri_logc3.cube` (or `arri.cube`, `show.cube`, or
   `style.grade.camera_transforms.show_lut`). Without one, cutroom uses the ARRI LogC-to-Rec709 LUT it finds in
   Resolve's LUT folder, and failing that the colorimetric conversion from ARRI's white paper (flagged: correct
   colours, not ARRI's look; the render warns).
3. **Match to the hero**: per camera and per shoot day, frames of that camera and of the ARRI at the same moments
   (sync groups first, else capture time corrected by the measured clock offsets, within 15 min) give a correction
   in display-linear light: white balance + exposure gains and a saturation factor (optionally a Monge-Kantorovich
   3×3, kept only when it is better). A correction that would make it worse is stored but left off. Stored in
   `camera_match`; the Looks panel shows the frame under the playhead with the camera transform only and with the
   match, plus ΔE before → after. QA `camera_match` measures CIEDE2000 between every pair of angles in each multicam
   group the render uses (warn above 5).

**Measured on the synthetic kit** (`camera-samples/synthetic-kit`: one scene "shot" by all four cameras through the
published curves, Blackmagic through the inverse of Blackmagic's Gen 5 LUT, each non-ARRI camera given a known white
balance / exposure / saturation error; identical framing, so every pixel is a ground-truth pair). Per-pixel CIEDE2000
against the ARRI, median / 95th percentile:

| Camera | one LUT on everything | camera transform | + match |
|---|---|---|---|
| Sony S-Log3 | 10.28 / 10.60 | 6.14 / 6.44 | 0.85 / 3.11 |
| Fujifilm F-Log2 | 5.40 / 9.10 | 4.88 / 5.18 | 0.85 / 2.34 |
| Blackmagic Film Gen 5 | 2.69 / 4.77 | 4.20 / 4.41 | 0.88 / 1.94 |

"Camera transform" still contains the deliberate camera error; the transform alone is exact for the maths cameras
(unit test: 95th percentile ΔE < 0.5 against the ARRI rendering of the same scene). QA `camera_match` on the rendered
multicam group: median ΔE 8.05 before, 0.32 after. These are synthetic numbers: real cameras differ in spectral
response, which a 3×3 can only approximate. The first real shoot day will give the real numbers.

### Premiere hand-off

`cutroom export-nle` writes `luts/` beside the XML: one `.cube` per camera (and shoot day when the match differs by day)
holding the whole transform (camera → ARRI show LUT → match), `<camera>_to_LogC3.cube` for the maths cameras (curve
and gamut only), `luts/clips.csv` (each clip file → its LUT), and a sequence marker on every clip naming its LUT.
README_PREMIERE.txt: select a camera's clips → Lumetri Color → Basic Correction → **Input LUT** → that camera's file.
Do not also put the ARRI LUT on an adjustment layer for those clips; adjustment layers are for the creative grade.
Option B in the README keeps the ARRI LUT on an adjustment layer and uses the `_to_LogC3` files instead (no match).
`cutroom cameras luts` writes the same files without a timeline.

### Blackmagic RAW (.braw) and ARRIRAW

- ffmpeg reads the .braw container (timing, sound, Blackmagic's metadata) but cannot decode the picture. `cutroom index`
  registers .braw clips with status `raw` and says so; `cutroom doctor` and `cutroom braw` list them.
- **Decode status on this machine:** the Blackmagic RAW runtime (`BlackmagicRawAPI.framework`) is present inside
  Blackmagic RAW Player / Speed Test and DaVinci Resolve, but the SDK headers are not. The framework exposes one C
  entry point (`CreateBlackmagicRawFactoryInstance`) and COM-style interfaces whose layout is only in the SDK headers,
  so no decoder helper was built: guessing that layout would crash or decode wrongly. With the Blackmagic RAW SDK
  installed (a free download behind a registration form), a small helper can be built; until then:
- **Stand-ins (works now):** a ProRes / H.264 rendition with the same file name in `<work>/raw_proxies/` or in a
  `Proxy/` folder beside the clip is used to decode the picture (index, proxies, previews, renders); the NLE export
  still links the .braw master (Premiere needs Blackmagic's BRAW plug-in for that). Ways to make them:
  - `tools/resolve_braw_proxies.py --work <project>` drives DaVinci Resolve's render queue through Resolve's
    documented scripting API (ProRes, one file per clip, one Resolve project per frame rate). Blackmagic documents
    external scripting for Resolve Studio; this machine has the free version, so the script is **untested here** and
    may be refused. The free version can do it by hand: Deliver page, "Individual clips", ProRes 422 Proxy, file
    name = source name, into `<work>/raw_proxies/`.
  - Blackmagic Proxy Generator (installed here): it writes a `Proxy` folder beside the clips, which cutroom finds.
  - Recording ProRes, or proxies, on the Pocket 6K Pro.
  Decode the stand-in with the clip's as-shot settings (Film for a log workflow) so it matches the Blackmagic transform.
- **ARRIRAW** (`.ari` / `.arx` frame sequences, ARRIRAW `.mxf`): reported as unsupported (scan lists the folders,
  never registers one clip per frame). The AMIRA records ProRes, which works directly.
- Tested: Blackmagic's own sample .braw (shipped with Blackmagic RAW Speed Test) is identified (camera, serial,
  Film gamma), reported as missing a stand-in, then indexed through a synthetic stand-in. Real BRAW decoding: not done.

### Rode Wireless PRO audio

- The recorder's files are registered as sound (status `audio`), never as picture. `bwf.py` reads the headers:
  32-bit float (`fmt` tag 3 or extensible float), `bext` (originator, origination date/time, time reference =
  samples since midnight), `iXML` (timecode rate, timestamp, file-set family for split takes) and RF64 for > 4 GB.
- 32-bit float stays float: decoding is `f32le`, and the rendered pieces cut from a float recording stay `pcm_f32le`
  until the final mix, so a recording peaking above 0 dBFS is not clipped before loudness normalisation brings it
  down. Integer sources keep 16-bit pieces (the room-tone gating was tuned on their digital silence; switching all
  pieces to float measurably changed it).
- **Split takes**: files of one device in an iXML file set, or whose time reference continues sample-exactly where the
  previous file ended, are joined into one logical recording (exact sync pairs, method `chunk`; `cutroom sync
  recordings`). A multicam segment takes its sound from whichever file of the take covers it.
- **Sync**: the audio cross-correlation decides, as before. New: **clap / slate anchors**: a sharp, broadband, short
  transient found in both recordings at the aligned moment confirms the offset (method `audio+clap`, the number of
  agreeing claps in the pair's note). When the envelopes alone can't decide, matching claps whose local correlation is
  clearly the best can rescue a pair (method `clap`). **Timecode**: when both clips carry timecode, its agreement is
  checked (method `+tc`); a device whose timecode agreed on two or more pairs and never disagreed is trusted for silent
  clips automatically (`tc_sync`).
- The recorder is the dialogue source (external recorder > lav > camera); camera sound is scratch. A Rode plugged into
  a camera is camera sound to cutroom: mark that camera `--kind lav` if it should outrank the others.
- **Assumed:** the exact Rode Wireless PRO file names, whether it writes iXML, and how it splits long takes were not
  available; the reader follows the BWF/iXML standards and the joiner uses the time reference, so it works for any
  recorder that writes them.

### Mixed card dumps and clock offsets

Cameras are told apart by their own identity (make, model, serial), not by folders. After sync, `estimate_clocks`
measures each device's clock against the ARRI's per day (the audio says when a clip really started; the file says what
the clock claimed), robust per device pair, solved over the pair graph. The corrected times put clips that did not sync
into the right moment (`cutroom cameras clips` sorts by them; matching uses them), and a third sync pass tries the
pairs the corrected clocks bring together. A camera that matched nothing yet is tried within 30 min
(`sync_lonely_window_s`, default 1800 s); the audio still decides. Synthetic test: an X-T5
whose clock was 7.5 min fast joined the group, measured +454.4 s off (truth +454.4 s).

### PAL

The project default is 25 fps. A 50p clip is a slow-motion candidate (`cutroom cameras` counts them). In a 25p edit
it plays in real time (every other frame dropped) unless the plan asks for slow motion: a b-roll shot with a `ramp`
plays as 2× slow motion into the ramp; `"conform": "slowmo"` asks for it without a ramp. Sync talk is never slowed.

### Things to send me

1. One shoot day's sample, straight off the cards: 2–3 clips from each camera (AMIRA, Pocket 6K Pro as .braw and any
   ProRes, X-T5, each Sony body), plus the Rode files from the same moments, ideally including one clap.
2. Your LUTs: the ARRI LogC3 → Rec.709 LUT you grade with (→ `luts/arri_logc3.cube`), Blackmagic's Gen 5 Film to
   Video LUT you use (→ `luts/bmd.cube`), and any Sony / Fujifilm LUTs you use in Premiere.
3. How you decode BRAW today (Premiere plug-in, Resolve, proxies) and whether you can record ProRes proxies on the 6K Pro.
4. One finished edit (and its Premiere project if you can) as the reference for look and cutting.

## 4. What I need from the creator's camera list

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
