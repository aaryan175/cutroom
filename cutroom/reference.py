"""Reference engine: measure a video the creator wants matched ("make it like this one") frame by frame,
then compare a render against it in numbers and plain words.

    ref = reference.add(cfg, "/path/to/ref.mp4")             # or a URL (needs yt-dlp on PATH)
    an = reference.analyze(cfg, ref["id"])                   # resumable, governor-slotted
    rows = reference.cutmap(cfg, ref["id"])                  # one row per shot, to rebuild one-to-one
    cmp = reference.compare(cfg, ref["id"], "renders/x_v3_preview.mp4")
    print("\\n".join(g["text"] for g in cmp["gaps"]))

Layout (all inside the project; the original file is only read):
    <work>/refs/<id>/source.<ext>      the reference: copied, downloaded, or a symlink to renders/reference/<file>
    <work>/refs/<id>/proxy.mp4         540p copy (studio playback; the picture analysis decodes this)
    <work>/refs/<id>/meta.json         {id, title, origin, url, status, stage, progress, error, duration, fps, ...}
    <work>/refs/<id>/analysis.json     every measurement (shape: docs/STUDIO.md, "Reference engine")
    <work>/refs/<id>/thumbs/sNNNN.jpg  a frame from the middle of every shot
    <work>/refs/<id>/sheet_NNN.jpg     contact sheets, one tile per shot (both sides of every cut are neighbours)
    <work>/refs/<id>/work/             per-stage results, so an interrupted analysis resumes
    <work>/refs/_scans/<sig>/          the same measurements for a render that was compared against a reference

Stages (each one resumes from work/): proxy -> scan -> frames -> audio -> asr -> assemble.
  scan    one decode of every frame at 320 px: PySceneDetect ContentDetector + AdaptiveDetector, plus our own
          frame-difference spikes (1-frame shots and flashes), black runs, luma/contrast/saturation/warmth,
          camera shake (the analyze.py LK helper) and a text-band score (high-contrast edge density in the
          top/middle/bottom bands)
  frames  a few frames per shot at proxy size: faces (+ mouth motion), dominant colours, and on-screen text.
          With `tesseract` on PATH the text bands are read (words, size, position, colour, dark-ring check)
  audio   ebur128 loudness + true peak, voice activity, a music-presence estimate, tempo + beat grid of the
          full mix (librosa), onsets, drops
  asr     word timestamps (faster-whisper in the machine-wide ASR slot) when there is speech
  assemble shot kinds, sections (change points in pacing / speech / talking head / loudness), pacing,
          beat sync, captions style, colour per section, structure (cold open, talk/b-roll/montage share,
          ending), contact sheets
Everything is a measurement or a labelled estimate; the AI still looks at the sheets before trusting a kind.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import governor, media

VERSION = 2
SCAN_W = 320                    # picture scan width (every frame)
THUMB_BOX = (480, 270)          # thumbnails fit in this box
TEXT_EVERY = 2                  # text-band score on every Nth frame
TEXT_BANDS = {"top": (0.0, 0.25), "middle": (0.38, 0.62), "bottom": (0.62, 1.0)}
TEXT_SCORE_ON = 0.035           # text-like edge density in the best stripe of a band (see _text_score)
BLACK_LUMA, BLACK_STD = 18.0, 9.0
EFFECT_SAME = 15.0               # 32x18 grey mean abs diff below which the frames around a 1-2 frame run are one scene
FACE_TALK_H = 0.12              # largest face at least this share of the frame height = a talking-head size
MONTAGE_SHOT = 1.2              # b-roll shots shorter than this, three or more in a row, form a montage
SUSTAINED_SPEECH = 2.5          # seconds of continuous speech that end a cold open
MUSIC_PERIODICITY = 0.32        # onset-envelope autocorrelation peak above this = rhythmic (music) window
STAGES = ("proxy", "scan", "frames", "audio", "asr", "assemble")
WEIGHTS = {"proxy": 0.10, "scan": 0.40, "frames": 0.14, "audio": 0.10, "asr": 0.18, "assemble": 0.08}
PROGRESS_PREFIX = "ref-progress"


# ==================================================================================================
# files
# ==================================================================================================

def refs_dir(cfg) -> Path:
    return Path(cfg.work) / "refs"


def ref_dir(cfg, rid) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(rid or "")):
        raise KeyError(f"bad reference id {rid!r}")
    return refs_dir(cfg) / rid


def _jload(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _jsave(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=_json_default))
    os.replace(tmp, p)


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def load_meta(cfg, rid) -> dict:
    m = _jload(ref_dir(cfg, rid) / "meta.json")
    if not m:
        raise KeyError(f"no reference {rid!r}")
    return m


def save_meta(cfg, rid, **fields) -> dict:
    p = ref_dir(cfg, rid) / "meta.json"
    m = _jload(p, {}) or {}
    m.update(fields)
    m["updated"] = time.time()
    _jsave(p, m)
    return m


def source_path(cfg, rid):
    d = ref_dir(cfg, rid)
    for p in sorted(d.glob("source.*")):
        if p.suffix not in (".part", ".tmp", ".ytdl") and (p.is_file() or p.is_symlink()):
            return p
    return None


def is_url(s) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", str(s or ""), re.I)) and not str(s).lower().startswith("file://")


def file_sig(path) -> str:
    """Short content hash: size + first and last MiB (fast on multi-GB files, stable across copies)."""
    p = Path(path)
    st = p.stat()
    h = hashlib.sha1(str(st.st_size).encode())
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > (2 << 20):
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()[:10]


def resolve_id(cfg, ref) -> str:
    """A reference id, a unique id prefix, or a unique title fragment -> id."""
    ref = str(ref or "").strip()
    root = refs_dir(cfg)
    if not ref:
        raise KeyError("no reference given")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", ref) and (root / ref / "meta.json").exists():
        return ref
    rows = list_refs(cfg)
    hits = [r for r in rows if r["id"].startswith(ref)] or \
           [r for r in rows if ref.lower() in str(r.get("title") or "").lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        raise KeyError(f"no reference matches {ref!r} (cutroom ref list)")
    raise KeyError(f"{ref!r} is ambiguous: {[h['id'] for h in hits[:8]]}")


def list_refs(cfg) -> list:
    root = refs_dir(cfg)
    out = []
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if d.name.startswith((".", "_")) or not d.is_dir():
            continue
        m = _jload(d / "meta.json")
        if not m:
            continue
        an = d / "analysis.json"
        if an.exists() and m.get("status") != "analyzing":
            s = _jload(an, {}) or {}
            m["summary"] = summary_numbers(s)
        out.append(m)
    out.sort(key=lambda m: -(m.get("added") or 0))
    return out


def sync_reference_folder(cfg, analyze_now=False, echo=None) -> list:
    """Surface every video in renders/reference/ (the studio's Reference tab folder) as a reference:
    linked, not copied. Cheap; proxies and analysis happen in analyze(). Returns new ids."""
    from .config import VIDEO_EXTS
    base = Path(cfg.work) / "renders" / "reference"
    if not base.is_dir():
        return []
    known = {}
    for m in list_refs(cfg):
        if m.get("origin"):
            known[str(m["origin"])] = m["id"]
    new = []
    for p in sorted(base.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in VIDEO_EXTS:
            continue
        if str(p.resolve()) in known:
            continue
        try:
            m = add(cfg, p, link=True, make_proxy_now=False, analyze_now=analyze_now, echo=echo or (lambda *_: None))
            new.append(m["id"])
        except Exception as e:  # noqa: BLE001 - one odd file must not break the listing
            if echo:
                echo(f"  ! could not import {p.name}: {e}")
    return new


# ==================================================================================================
# add (copy / download / link) + proxy
# ==================================================================================================

def _probe_fields(cfg, path) -> dict:
    info = media.probe(cfg, path)
    s = media.summarize_probe(info)
    v = next((x for x in info.get("streams", []) if x.get("codec_type") == "video"), {}) or {}
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        fr = Fraction(rate) if Fraction(rate) > 0 else Fraction(25)
    except (ValueError, ZeroDivisionError):
        fr = Fraction(25)
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    if abs(int(s.get("rotation") or 0)) in (90, 270):
        w, h = h, w
    return {"duration": round(float(s.get("duration") or 0), 3), "fps": round(float(fr), 4),
            "fps_rational": f"{fr.numerator}/{fr.denominator}", "width": w, "height": h,
            "has_audio": bool(s.get("has_audio")), "codec": s.get("codec")}


def add(cfg, src, title=None, analyze_now=True, link=False, make_proxy_now=True, echo=print) -> dict:
    """Add a reference from a local file (copied into the project; `link=True` symlinks instead) or a URL
    (downloaded with yt-dlp: best mp4 <= 1080p with audio). Idempotent: the same file/URL gives the same id."""
    refs_dir(cfg).mkdir(parents=True, exist_ok=True)
    if is_url(src):
        rid = hashlib.sha1(str(src).strip().encode()).hexdigest()[:10]
        d = ref_dir(cfg, rid)
        d.mkdir(parents=True, exist_ok=True)
        meta = _jload(d / "meta.json", {}) or {}
        if not source_path(cfg, rid):
            save_meta(cfg, rid, id=rid, url=str(src), origin=str(src), title=title or meta.get("title") or "",
                      added=meta.get("added") or time.time(), status="downloading", stage="download", progress=0.0,
                      error=None)
            try:
                got_title = _download(cfg, rid, str(src), echo)
            except Exception as e:
                save_meta(cfg, rid, status="error", error=str(e)[-600:])
                raise
            if not title and got_title:
                title = got_title
    else:
        p = Path(str(src)).expanduser()
        if str(src).lower().startswith("file://"):
            p = Path(str(src)[7:])
        if not p.is_file():
            raise FileNotFoundError(f"no such file: {src}")
        p = p.resolve()
        rid = file_sig(p)
        d = ref_dir(cfg, rid)
        d.mkdir(parents=True, exist_ok=True)
        meta = _jload(d / "meta.json", {}) or {}
        dst = d / f"source{p.suffix.lower() or '.mp4'}"
        if not source_path(cfg, rid):
            if link:
                os.symlink(p, dst)
            else:
                echo(f"ref: copying {p.name} ({p.stat().st_size / 1e6:.0f} MB) into the project")
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                shutil.copyfile(p, tmp)
                os.replace(tmp, dst)
        title = title or meta.get("title") or p.stem
        save_meta(cfg, rid, id=rid, origin=str(p), url=None, linked=bool(link) or meta.get("linked", False),
                  added=meta.get("added") or time.time())
    meta = _jload(ref_dir(cfg, rid) / "meta.json", {}) or {}
    src_file = source_path(cfg, rid)
    fields = _probe_fields(cfg, src_file)
    status = meta.get("status") if meta.get("status") in ("analyzed",) and (ref_dir(cfg, rid) / "analysis.json").exists() else "added"
    meta = save_meta(cfg, rid, id=rid, title=title or meta.get("title") or rid, source=src_file.name,
                     status=status, stage=None if status == "analyzed" else "added",
                     progress=1.0 if status == "analyzed" else 0.0, error=None, **fields)
    echo(f"ref {rid}: {meta['title']} · {fields['duration']:.1f}s · {fields['width']}x{fields['height']} "
         f"@ {fields['fps']:g} fps{'' if fields['has_audio'] else ' · no audio'}")
    if make_proxy_now and not analyze_now:
        make_proxy(cfg, rid, echo=echo)
    if analyze_now:
        analyze(cfg, rid, echo=echo)
        meta = load_meta(cfg, rid)
    return meta


def _download(cfg, rid, url, echo) -> str:
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("downloading a reference from a link needs yt-dlp, which is not installed. Install it "
                           "(`brew install yt-dlp` or `pip install yt-dlp`), or download the video yourself and run "
                           "`cutroom ref add <file>`.")
    d = ref_dir(cfg, rid)
    fmt = ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/"
           "bv*[height<=1080]+ba/b[height<=1080]/b")
    cmd = [ytdlp, "--no-playlist", "--no-simulate", "--newline", "--progress", "-f", fmt,
           "--merge-output-format", "mp4", "-o", str(d / "source.%(ext)s"),
           "--print", "after_move:CUTROOM_TITLE=%(title)s", "--print", "after_move:CUTROOM_FILE=%(filepath)s", url]
    echo(f"ref {rid}: downloading with yt-dlp")
    title = ""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    tail = []
    last = 0.0
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-15:]
        if line.startswith("CUTROOM_TITLE="):
            title = line.split("=", 1)[1].strip()
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m and time.time() - last > 1.0:
            last = time.time()
            frac = float(m.group(1)) / 100
            save_meta(cfg, rid, progress=round(frac * 0.5, 3), stage="download")
            echo(f"{PROGRESS_PREFIX} {rid} {frac * 0.05:.3f} download: {m.group(1)}%")
    rc = proc.wait()
    if rc or not source_path(cfg, rid):
        raise RuntimeError("yt-dlp could not download that link:\n" + "\n".join(tail[-8:]))
    return title


def make_proxy(cfg, rid, echo=print) -> Path:
    """540p H.264 copy of the reference, every frame kept (frame-accurate cuts are measured on it)."""
    d = ref_dir(cfg, rid)
    out = d / "proxy.mp4"
    src = source_path(cfg, rid)
    if src is None:
        raise FileNotFoundError(f"reference {rid} has no source file")
    if out.exists() and out.stat().st_mtime >= Path(src).stat().st_mtime and _frames_ok(cfg, out):
        return out
    meta = load_meta(cfg, rid)
    h = int(getattr(cfg, "proxy_height", 540) or 540)
    vf = f"scale=-2:'min({h},ih)':flags=bicubic" if meta.get("width", 1) >= meta.get("height", 1) else \
        f"scale='min({h},iw)':-2:flags=bicubic"
    gov = governor.for_cfg(cfg)
    gov.wait_for_disk(d)
    tmp = d / "proxy.tmp.mp4"
    with gov.slot("heavy", label=f"ref proxy {rid}"):
        rate = ["-fps_mode", "cfr", "-r", meta.get("fps_rational") or "25/1"]      # CFR: frame i is at i/fps
        args = ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf + ",format=yuv420p", *rate,
                *media.hw_encoder(cfg), "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", tmp]
        try:
            media.ffmpeg(cfg, args, log=d / "ffmpeg.log")
        except media.FFError:
            media.ffmpeg(cfg, ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf + ",format=yuv420p",
                               *rate, "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                               "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", tmp],
                         log=d / "ffmpeg.log")
    os.replace(tmp, out)
    echo(f"ref {rid}: proxy ready")
    return out


def _frames_ok(cfg, p) -> bool:
    try:
        return float(media.summarize_probe(media.probe(cfg, p)).get("duration") or 0) > 0
    except Exception:  # noqa: BLE001
        return False


# ==================================================================================================
# progress
# ==================================================================================================

class _Prog:
    """Overall progress across stages: updates meta.json (throttled) and prints `ref-progress` lines."""

    def __init__(self, cfg, rid, echo, weights=WEIGHTS, write_meta=True):
        self.cfg, self.rid, self.echo, self.w, self.write = cfg, rid, echo, weights, write_meta
        self.done = 0.0
        self.stage = None
        self.last = 0.0

    def begin(self, stage):
        self.stage = stage
        self(0.0, "", force=True)

    def end(self):
        self.done += self.w.get(self.stage, 0.0)
        self(0.0, "done", force=True, finished=True)

    def __call__(self, frac, msg="", force=False, finished=False):
        now = time.time()
        if not force and now - self.last < 1.0:
            return
        self.last = now
        overall = min(0.999, self.done + (0 if finished else self.w.get(self.stage, 0.0) * max(0.0, min(1.0, frac))))
        if self.write and self.rid:
            try:
                save_meta(self.cfg, self.rid, status="analyzing", stage=self.stage, progress=round(overall, 3))
            except OSError:
                pass
        self.echo(f"{PROGRESS_PREFIX} {self.rid or '-'} {overall:.3f} {self.stage}: {msg}")


# ==================================================================================================
# stage: scan (every frame)
# ==================================================================================================

def _text_score(cv2, band):
    """Text-likeness of a grey band: density of edges sitting next to very bright or very dark pixels,
    in the best horizontal stripe (captions are a short, dense stripe of strokes)."""
    edges = cv2.Canny(band, 90, 200)
    ext = ((band > 200) | (band < 45)).astype(np.uint8)
    ext = cv2.dilate(ext, np.ones((3, 3), np.uint8))
    tl = (edges > 0) & (ext > 0)
    rows = tl.mean(axis=1)
    k = max(3, int(round(band.shape[0] * 0.18)))
    if len(rows) <= k:
        return float(rows.mean())
    win = np.convolve(rows, np.ones(k) / k, mode="valid")
    j = int(np.argmax(win))
    # columns: text spreads across, a single bright pole does not
    cols = tl[j:j + k].mean(axis=0)
    spread = float((cols > 0.02).mean())
    return float(win[j]) * min(1.0, spread * 2.5)


def _tc_frame(c, fps):
    if isinstance(c, (int, np.integer)):
        return int(c)
    fn = getattr(c, "frame_num", None)
    if fn is not None:
        return int(fn)
    s = getattr(c, "seconds", None)
    if s is None:
        s = c.get_seconds()
    return int(round(float(s) * fps))


def _detectors(fps):
    from scenedetect import AdaptiveDetector, ContentDetector
    cd = ContentDetector(threshold=27.0, min_scene_len=1)
    ad = AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=max(2, int(round(fps * 0.25))),
                          window_width=2, min_content_val=15.0)
    return cd, ad


def _feed(det, i, frame, fps):
    from scenedetect.common import FrameTimecode
    try:
        cuts = det.process_frame(FrameTimecode(i, fps=fps), frame)
    except TypeError:
        cuts = det.process_frame(i, frame)
    return [_tc_frame(c, fps) for c in (cuts or [])]


def _flush(det, i, fps):
    try:
        from scenedetect.common import FrameTimecode
        return [_tc_frame(c, fps) for c in (det.post_process(FrameTimecode(i, fps=fps)) or [])]
    except Exception:  # noqa: BLE001
        return []


def _nice_prefix(gov):
    n = shutil.which("nice")
    return [n, "-n", str(gov.nice)] if (n and gov.enabled and gov.nice) else []


def scan_video(cfg, video, fps, out_npz, prog=None, label="scan") -> dict:
    """Decode every frame of `video` at SCAN_W and measure it. Writes the per-frame arrays to out_npz."""
    import cv2
    from .analyze import _camera_shift
    cv2.setNumThreads(2)
    info = media.summarize_probe(media.probe(cfg, video))
    W, H = int(info.get("width") or 0), int(info.get("height") or 0)
    if abs(int(info.get("rotation") or 0)) in (90, 270):
        W, H = H, W
    if not W or not H:
        raise RuntimeError(f"{video}: no video stream")
    w = SCAN_W
    h = max(2, int(round(w * H / W / 2)) * 2)
    expect = max(1, int(round(float(info.get("duration") or 0) * fps)))
    gov = governor.for_cfg(cfg)
    cmd = _nice_prefix(gov) + [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(video),
                               "-map", "0:v:0", "-an", "-vf", f"scale={w}:{h}:flags=area", "-fps_mode", "passthrough",
                               "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    cd, ad = _detectors(fps)
    fsize = w * h * 3
    bands = {k: (int(round(a * h)), max(int(round(a * h)) + 4, int(round(b * h)))) for k, (a, b) in TEXT_BANDS.items()}
    L = {k: [] for k in ("luma", "std", "sat", "warm", "mafd", "hd", "d2", "vx", "vy", "tx_top", "tx_middle", "tx_bottom")}
    g32 = []
    c_cuts, a_cuts = [], []
    prev_small = prev2_small = None
    prev_hist = None
    prev_grey, pts = None, None
    stride = 2 if fps >= 20 else 1
    chk = gov.watch_self()
    with gov.slot("heavy", label=f"ref {label}"):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=fsize * 4)
        i = 0
        try:
            while True:
                buf = proc.stdout.read(fsize)
                if len(buf) < fsize:
                    break
                fr = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                grey = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                m, s = cv2.meanStdDev(grey)
                L["luma"].append(float(m[0, 0]))
                L["std"].append(float(s[0, 0]))
                hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
                L["sat"].append(float(hsv[..., 1].mean()))
                bgr = cv2.mean(fr)
                L["warm"].append(float(bgr[2] - bgr[0]))
                small = cv2.resize(grey, (64, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
                g32.append(cv2.resize(grey, (32, 18), interpolation=cv2.INTER_AREA))
                hist = np.bincount((small.ravel() >> 4).astype(np.int64), minlength=16).astype(np.float32) / small.size
                L["mafd"].append(float(np.abs(small - prev_small).mean()) if prev_small is not None else 0.0)
                L["hd"].append(float(0.5 * np.abs(hist - prev_hist).sum()) if prev_hist is not None else 0.0)
                # d2[i-1] = difference between frame i-2 and frame i (a one-frame insert has a small d2)
                if prev2_small is not None:
                    L["d2"].append(float(np.abs(small - prev2_small).mean()))
                elif i >= 1:
                    L["d2"].append(0.0)
                c_cuts += _feed(cd, i, fr, fps)
                a_cuts += _feed(ad, i, fr, fps)
                if i % TEXT_EVERY == 0:
                    for k, (a, b) in bands.items():
                        L[f"tx_{k}"].append(_text_score(cv2, grey[a:b]))
                else:
                    for k in bands:
                        L[f"tx_{k}"].append(np.nan)
                v = (np.nan, np.nan)
                if i % stride == 0:
                    if prev_grey is not None:
                        if (i // stride) % 10 == 0:
                            pts = None
                        vv, pts = _camera_shift(cv2, prev_grey, grey, pts)
                        if vv is not None:
                            v = (vv[0] * 480.0 / w, vv[1] * 480.0 / w)
                    prev_grey = grey
                L["vx"].append(v[0])
                L["vy"].append(v[1])
                prev2_small, prev_small, prev_hist = prev_small, small, hist
                i += 1
                if prog and i % 250 == 0:
                    prog(i / expect, f"{i}/{expect} frames")
                if i % 1000 == 0:
                    chk()
        finally:
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="replace")
            proc.stderr.close()
            rc = proc.wait()
    if i == 0:
        raise RuntimeError(f"decoded zero frames from {video}: {err[-400:]}")
    c_cuts += _flush(cd, i, fps)
    a_cuts += _flush(ad, i, fps)
    L["d2"].append(0.0)
    arr = {k: np.asarray(v[:i], np.float32) for k, v in L.items()}
    arr["d2"] = np.asarray((L["d2"] + [0.0] * i)[:i], np.float32)
    arr["content_cuts"] = np.asarray(sorted(set(c_cuts)), np.int64)
    arr["adaptive_cuts"] = np.asarray(sorted(set(a_cuts)), np.int64)
    arr["n"] = np.asarray([i])
    arr["g32"] = np.asarray(g32[:i], np.uint8).reshape(-1, 18, 32)
    arr["dims"] = np.asarray([w, h, W, H])
    out_npz = Path(out_npz)
    out_npz.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_npz.with_name(out_npz.stem + ".tmp.npz")
    np.savez_compressed(tmp, **arr)
    os.replace(tmp, out_npz)
    return arr


# ==================================================================================================
# cuts + shots
# ==================================================================================================

def _local_med(x, i, r=12):
    lo, hi = max(1, i - r), min(len(x), i + r + 1)
    nb = np.concatenate([x[lo:max(lo, i - 1)], x[min(hi, i + 2):hi]])
    return float(np.median(nb)) if len(nb) else 0.0


def _is_spike(mafd, hd, i) -> bool:
    if i <= 0 or i >= len(mafd):
        return False
    med = _local_med(mafd, i)
    d = float(mafd[i])
    return (d >= 8.0 and d >= 3.5 * (med + 1.5)) or (float(hd[i]) >= 0.45 and d >= 6.0 and d >= 2.0 * (med + 1.5))


def find_cuts(arr, fps) -> tuple:
    """(cut frames, flashes). A cut frame is the first frame of a new shot. Merges PySceneDetect's
    Content + Adaptive detectors with frame-difference spikes (which catch 1-frame shots), black-run
    edges and white-flash edges; refines every candidate to the exact frame of the jump."""
    n = int(arr["n"][0])
    mafd, hd, luma, std = arr["mafd"], arr["hd"], arr["luma"], arr["std"]
    spikes = [i for i in range(1, n) if _is_spike(mafd, hd, i)]
    cand = set(spikes)
    for c in list(arr["content_cuts"]) + list(arr["adaptive_cuts"]):
        c = int(c)
        if c <= 0 or c >= n:
            continue
        lo, hi = max(1, c - 2), min(n, c + 3)
        j = lo + int(np.argmax(mafd[lo:hi]))
        if mafd[j] >= 2.0:
            cand.add(j)
    # black runs and white flashes: boundaries even when the change is gradual
    dark = (luma < BLACK_LUMA) & (std < BLACK_STD)
    extra = set()
    for a, b in _runs(dark):
        if b - a >= 2:
            if a > 0:
                extra.add(a)
            if b < n:
                extra.add(b)
    bright = luma > 200
    for a, b in _runs(bright):
        if b - a <= 4 and a > 0 and b < n:
            around = max(luma[max(0, a - 3):a].max(initial=0), luma[b:min(n, b + 3)].max(initial=0))
            if luma[a:b].mean() - around > 40:
                extra.add(a)
                extra.add(b)
    cand |= extra
    cs = sorted(c for c in cand if 0 < c < n)
    # dedupe clusters within 2 frames: keep every strong spike (1- and 2-frame shots), else the biggest jump
    out, k = [], 0
    while k < len(cs):
        grp = [cs[k]]
        while k + 1 < len(cs) and cs[k + 1] - grp[-1] <= 2:
            k += 1
            grp.append(cs[k])
        k += 1
        strong = [g for g in grp if _is_spike(mafd, hd, g) or g in extra]
        if len(strong) >= 2:
            keep = [strong[0]]
            for g in strong[1:]:
                # two boundaries 1-2 frames apart only when the frames between are their own picture
                inner = mafd[keep[-1] + 1:g + 1]
                if len(inner) and float(np.max(inner)) >= 6.0:
                    keep.append(g)
            out += keep
        elif strong:
            out.append(strong[0])
        else:
            out.append(max(grp, key=lambda g: mafd[g]))
    cuts = sorted(set(out))
    # runs of 1-2 frame shots: picture events. Same scene before and after = an effect inside one shot
    # (glitch, zoom blur, overlay): not a cut. Different scenes = one short transition shot.
    g32 = arr.get("g32")
    flashes = []
    bounds = [0] + cuts + [n]
    drop = set()
    k = 0
    while k < len(bounds) - 1:
        if bounds[k + 1] - bounds[k] > 2 or bounds[k] == 0 or bounds[k + 1] >= n:
            k += 1
            continue
        j = k
        while j + 1 < len(bounds) - 1 and bounds[j + 2] - bounds[j + 1] <= 2 and bounds[j + 2] < n:
            j += 1
        a, b = bounds[k], bounds[j + 1]                     # the run is frames [a, b)
        same = None
        if g32 is not None and len(g32) == n:
            same = float(np.abs(g32[a - 1].astype(np.int16) - g32[b].astype(np.int16)).mean()) < EFFECT_SAME
        mean = float(luma[a:b].mean())
        nb = max(float(luma[a - 1]), float(luma[b]))
        if same:
            kind = "effect"
            drop.update(bounds[k:j + 2])
        else:
            kind = "white" if (mean > 200 or mean - nb > 40) else "insert"
            drop.update(bounds[k + 1:j + 1])                # inner boundaries: one transition shot
        flashes.append({"f0": a, "frames": b - a, "t": round(a / fps, 4), "kind": kind})
        k = j + 1
    cuts = [c for c in cuts if c not in drop]
    return cuts, flashes


def _runs(mask):
    out, s = [], None
    for i, v in enumerate(mask):
        if v and s is None:
            s = i
        elif not v and s is not None:
            out.append((s, i))
            s = None
    if s is not None:
        out.append((s, len(mask)))
    return out


def _shots_from_cuts(cuts, n, fps):
    b = [0] + list(cuts) + [n]
    return [{"i": j, "f0": int(b[j]), "f1": int(b[j + 1]), "frames": int(b[j + 1] - b[j]),
             "start": round(b[j] / fps, 4), "end": round(b[j + 1] / fps, 4), "dur": round((b[j + 1] - b[j]) / fps, 4)}
            for j in range(len(b) - 1)]


def _shot_stats(arr, s):
    a, b = s["f0"], s["f1"]
    sl = slice(a, b)
    vx, vy = arr["vx"][sl], arr["vy"][sl]
    ok = ~np.isnan(vx)
    v = np.stack([vx[ok], vy[ok]], 1) if ok.any() else np.zeros((0, 2))
    motion = float(np.hypot(v[:, 0], v[:, 1]).mean()) if len(v) else 0.0
    shake = float(np.hypot(*np.diff(v, axis=0).T).mean()) if len(v) > 2 else 0.0
    tx = {}
    for k in TEXT_BANDS:
        x = arr[f"tx_{k}"][sl]
        x = x[~np.isnan(x)]
        tx[k] = (float((x >= TEXT_SCORE_ON).mean()) if len(x) else 0.0, float(x.max()) if len(x) else 0.0)
    return {"luma": round(float(arr["luma"][sl].mean()), 1), "contrast": round(float(arr["std"][sl].mean()), 1),
            "sat": round(float(arr["sat"][sl].mean()), 1), "warmth": round(float(arr["warm"][sl].mean()), 1),
            "motion": round(motion, 2), "shake": round(shake, 2), "_tx": tx}


def _text_frame(arr, s, band):
    """Frame inside the shot with the strongest text score in `band` (the best one to read)."""
    x = arr[f"tx_{band}"][s["f0"]:s["f1"]]
    if not len(x) or np.all(np.isnan(x)):
        return (s["f0"] + s["f1"] - 1) // 2
    return s["f0"] + int(np.nanargmax(x))


# ==================================================================================================
# stage: frames (a few per shot)
# ==================================================================================================

def extract_frames(cfg, video, idxs, fps, out_dir, start_time=0.0) -> dict:
    """Pull exact frames by index (0-based, CFR) into out_dir as JPEGs at the video's own size.
    Chunked accurate seeks + select on the frame number; falls back to one seek per frame if a chunk
    comes back short. Returns {index: path}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    idxs = sorted({int(i) for i in idxs if i >= 0})
    got = {}
    for i in idxs:
        p = out_dir / f"f{i:07d}.jpg"
        if p.exists() and p.stat().st_size > 0:
            got[i] = p
    todo = [i for i in idxs if i not in got]
    chunks, cur = [], []
    for i in todo:
        if cur and (len(cur) >= 100 or (i - cur[0]) / fps > 60):
            chunks.append(cur)
            cur = []
        cur.append(i)
    if cur:
        chunks.append(cur)
    for ch in chunks:
        f0 = ch[0]
        tmpd = Path(tempfile.mkdtemp(prefix="x_", dir=out_dir))
        try:
            pre = ["-ss", f"{start_time + max(0.0, (f0 - 0.5) / fps):.6f}"] if f0 > 0 else []
            span = (ch[-1] - f0 + 2) / fps
            expr = "+".join(f"eq(n\\,{i - f0})" for i in ch)
            media.ffmpeg(cfg, [*pre, "-t", f"{span:.6f}", "-i", video, "-map", "0:v:0", "-an",
                               "-vf", f"select='{expr}'", "-fps_mode", "passthrough", "-q:v", "2",
                               str(tmpd / "c_%05d.jpg")], log=out_dir / "ffmpeg.log")
            files = sorted(tmpd.glob("c_*.jpg"))
            if len(files) == len(ch):
                for i, f in zip(ch, files):
                    dst = out_dir / f"f{i:07d}.jpg"
                    os.replace(f, dst)
                    got[i] = dst
            else:
                for i in ch:
                    dst = out_dir / f"f{i:07d}.jpg"
                    media.ffmpeg(cfg, ["-ss", f"{start_time + max(0.0, (i - 0.25) / fps):.6f}", "-i", video,
                                       "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", "-update", "1", dst],
                                 log=out_dir / "ffmpeg.log", check=False)
                    if dst.exists():
                        got[i] = dst
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    return got


def _video_start(cfg, video) -> float:
    try:
        info = media.probe(cfg, video)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}) or {}
        return max(0.0, float(v.get("start_time") or 0.0))
    except Exception:  # noqa: BLE001
        return 0.0


def _dominant_colours(cv2, img, k=3):
    small = cv2.resize(img, (48, 27), interpolation=cv2.INTER_AREA).reshape(-1, 3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    try:
        _, lab, cen = cv2.kmeans(small, k, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    except cv2.error:
        return []
    cnt = np.bincount(lab.ravel(), minlength=k)
    order = np.argsort(-cnt)
    return [{"hex": "#%02x%02x%02x" % tuple(int(x) for x in cen[j][::-1]), "pct": round(float(cnt[j]) / len(lab) * 100, 1)}
            for j in order]


def _ocr(cv2, img, band):
    """Read text in one band with tesseract (if installed). Returns word boxes in frame fractions."""
    exe = shutil.which("tesseract")
    if not exe:
        return None
    H, W = img.shape[:2]
    a, b = TEXT_BANDS[band]
    y0, y1 = int(a * H), int(b * H)
    crop = img[y0:y1]
    scale = 2.0 if H < 900 else 1.0
    if scale != 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cv2.imwrite(tmp, crop)
        r = subprocess.run([exe, tmp, "stdout", "--psm", "11", "tsv"], capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        os.unlink(tmp)
    words = []
    for ln in r.stdout.splitlines()[1:]:
        p = ln.split("\t")
        if len(p) < 12:
            continue
        try:
            conf = float(p[10])
        except ValueError:
            continue
        txt = p[11].strip()
        if conf < 65 or len(re.sub(r"[^A-Za-z0-9]", "", txt)) < 2:
            continue
        x, y, w, h = (int(p[6]) / scale, int(p[7]) / scale, int(p[8]) / scale, int(p[9]) / scale)
        if h < 0.012 * H or h > 0.3 * H:
            continue
        words.append({"text": txt, "conf": conf, "box": [x / W, (y0 + y) / H, w / W, h / H],
                      "line": (p[2], p[3], p[4])})
    # a few short low-confidence fragments are texture, not text
    letters = sum(len(re.sub(r"[^A-Za-z0-9]", "", w_["text"])) for w_ in words)
    if letters < 5 or (np.mean([w_["conf"] for w_ in words]) < 75 and not any(len(w_["text"]) >= 4 for w_ in words)):
        return []
    return words


def _ink_style(cv2, img, boxes):
    """Ink colour and the rule-53 dark-ring check around glyphs inside the given word boxes."""
    H, W = img.shape[:2]
    cols, rings = [], []
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for bx in boxes[:12]:
        x, y, w, h = bx
        x0, y0 = max(0, int(x * W) - 2), max(0, int(y * H) - 2)
        x1, y1 = min(W, int((x + w) * W) + 2), min(H, int((y + h) * H) + 2)
        if x1 - x0 < 6 or y1 - y0 < 6:
            continue
        g = grey[y0:y1, x0:x1]
        border = np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]])
        bed = float(np.median(border))
        light_ink = float(np.percentile(g, 95)) - bed > bed - float(np.percentile(g, 5))
        ink = (g > max(bed + 40, np.percentile(g, 80))) if light_ink else (g < min(bed - 40, np.percentile(g, 20)))
        if ink.sum() < 8:
            continue
        cols.append(np.median(img[y0:y1, x0:x1][ink], axis=0))
        # rings measured on a wider crop so the 5-8 px bed ring exists
        X0, Y0, X1, Y1 = max(0, x0 - 10), max(0, y0 - 10), min(W, x1 + 10), min(H, y1 + 10)
        G = grey[Y0:Y1, X0:X1].astype(np.float32)
        INK = np.zeros(G.shape, np.uint8)
        INK[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = ink.astype(np.uint8)
        d2 = cv2.dilate(INK, np.ones((5, 5), np.uint8))
        d5 = cv2.dilate(INK, np.ones((11, 11), np.uint8))
        d8 = cv2.dilate(INK, np.ones((17, 17), np.uint8))
        near = (d2 > 0) & (INK == 0)
        bedr = (d8 > 0) & (d5 == 0)
        if near.sum() > 5 and bedr.sum() > 5:
            rings.append((float(G[near].mean()), float(G[bedr].mean()), light_ink))
    colour = None
    if cols:
        c = np.median(np.array(cols), axis=0)
        colour = "#%02x%02x%02x" % (int(c[2]), int(c[1]), int(c[0]))
    outline = None
    if rings:
        # light ink: a dark keyline makes the near ring darker than the bed by > 5 levels (rule 53)
        votes = [(bed - near > 5) if light else (near - bed > 5) for near, bed, light in rings]
        outline = bool(sum(votes) > len(votes) / 2)
    return colour, outline


def _heuristic_caption(cv2, img, band):
    """No tesseract: the densest text-like stripe of the band -> approximate size/position/colour."""
    H, W = img.shape[:2]
    a, b = TEXT_BANDS[band]
    y0, y1 = int(a * H), int(b * H)
    g = cv2.cvtColor(img[y0:y1], cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(g, 90, 200)
    ext = cv2.dilate(((g > 200) | (g < 45)).astype(np.uint8), np.ones((3, 3), np.uint8))
    rows = ((edges > 0) & (ext > 0)).mean(axis=1)
    on = rows > max(0.04, rows.max() * 0.35)
    if not on.any():
        return None
    runs = _runs(on)
    ra, rb = max(runs, key=lambda r: rows[r[0]:r[1]].sum())
    cols = ((edges[ra:rb] > 0) & (ext[ra:rb] > 0)).mean(axis=0)
    xs = np.where(cols > 0.02)[0]
    if not len(xs):
        return None
    box = [xs[0] / W, (y0 + ra) / H, (xs[-1] - xs[0]) / W, (rb - ra) / H]
    return {"box": box}


def frames_stage(cfg, video, arr, shots, fps, d, prog=None, label="frames") -> list:
    """Per shot: faces (+ mouth motion), dominant colours, text read/estimated, a thumbnail."""
    import cv2
    from .qa import detect_faces
    cv2.setNumThreads(2)
    fdir = d / "work" / "frames"
    tdir = d / "thumbs"
    tdir.mkdir(parents=True, exist_ok=True)
    plan = {}
    for s in shots:
        mid = (s["f0"] + s["f1"] - 1) // 2
        want = {"mid": mid}
        if s["frames"] >= 7:
            want["mouth"] = min(s["f1"] - 1, mid + 3)
        st = s["_stats"]["_tx"]
        tb = [k for k, (frac, mx) in st.items() if frac >= 0.25 or (s["frames"] <= 12 and mx >= TEXT_SCORE_ON)]
        if tb:
            band = max(tb, key=lambda k: st[k][0] + st[k][1])
            want["text"] = _text_frame(arr, s, band)
            want["band"] = band
        plan[s["i"]] = want
    idxs = [v for w in plan.values() for k, v in w.items() if k != "band"]
    gov = governor.for_cfg(cfg)
    with gov.slot("heavy", label=f"ref {label}"):
        files = extract_frames(cfg, video, idxs, fps, fdir, start_time=_video_start(cfg, video))
        out = []
        for k, s in enumerate(shots):
            w = plan[s["i"]]
            img = cv2.imread(str(files.get(w["mid"], ""))) if w["mid"] in files else None
            res = {"faces": 0, "face_h": 0.0, "mouth": None, "colors": [], "text": None, "thumb": None}
            if img is not None:
                H, W = img.shape[:2]
                faces = detect_faces(img)
                res["faces"] = len(faces)
                if faces:
                    big = max(faces, key=lambda f: f[3])
                    res["face_h"] = round(float(big[3]), 3)
                    if "mouth" in w and w["mouth"] in files:
                        img2 = cv2.imread(str(files[w["mouth"]]))
                        if img2 is not None and img2.shape == img.shape:
                            x, y, fw, fh = big
                            mx0, mx1 = int((x + fw * 0.2) * W), int((x + fw * 0.8) * W)
                            my0, my1 = int((y + fh * 0.6) * H), int(min(1.0, y + fh * 1.05) * H)
                            g1 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.int16)
                            g2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY).astype(np.int16)
                            if mx1 > mx0 + 2 and my1 > my0 + 2:
                                mouth = float(np.abs(g1[my0:my1, mx0:mx1] - g2[my0:my1, mx0:mx1]).mean())
                                whole = float(np.abs(g1 - g2).mean())
                                res["mouth"] = round(mouth - whole, 2)
                res["colors"] = _dominant_colours(cv2, img)
                tw, th = THUMB_BOX
                sc = min(tw / W, th / H)
                thumb = cv2.resize(img, (max(2, int(W * sc)), max(2, int(H * sc))), interpolation=cv2.INTER_AREA)
                tp = tdir / f"s{s['i']:04d}.jpg"
                cv2.imwrite(str(tp), thumb, [cv2.IMWRITE_JPEG_QUALITY, 82])
                res["thumb"] = f"thumbs/{tp.name}"
            if "text" in w and w["text"] in files:
                timg = cv2.imread(str(files[w["text"]]))
                if timg is not None:
                    res["text"] = _read_text(cv2, timg, w["band"])
            out.append(res)
            if prog and k % 20 == 0:
                prog(k / max(1, len(shots)), f"{k}/{len(shots)} shots")
    shutil.rmtree(fdir, ignore_errors=True)
    return out


def _read_text(cv2, img, band):
    H, W = img.shape[:2]
    words = _ocr(cv2, img, band)
    if words is not None:
        if not words:
            return {"present": False, "band": band, "method": "ocr"}
        boxes = [w_["box"] for w_ in words]
        hs = [b[3] for b in boxes]
        ys = [b[1] + b[3] / 2 for b in boxes]
        colour, outline = _ink_style(cv2, img, boxes)
        lines = {}
        for w_ in words:
            lines.setdefault(w_["line"], []).append(w_["text"])
        return {"present": True, "band": band, "method": "ocr", "words": " ".join(w_["text"] for w_ in words),
                "n_words": len(words), "n_lines": len(lines), "size_pct": round(float(np.median(hs)) * 100, 2),
                "y_center": round(float(np.median(ys)), 3),
                "x_center": round(float(np.median([b[0] + b[2] / 2 for b in boxes])), 3),
                "color": colour, "outline": outline}
    h = _heuristic_caption(cv2, img, band)
    if not h:
        return {"present": False, "band": band, "method": "heuristic"}
    colour, outline = _ink_style(cv2, img, [h["box"]])
    b = h["box"]
    return {"present": True, "band": band, "method": "heuristic", "words": "", "n_words": None, "n_lines": None,
            "size_pct": round(b[3] * 100 * 0.8, 2), "y_center": round(b[1] + b[3] / 2, 3),
            "x_center": round(b[0] + b[2] / 2, 3), "color": colour, "outline": outline}


# ==================================================================================================
# stage: audio
# ==================================================================================================

def audio_stage(cfg, src, d, prog=None, label="audio") -> tuple:
    """(audio facts dict, 16 kHz mono array). ebur128 + VAD + music estimate + beat grid + onsets."""
    from . import asr as A
    from . import music as M
    from .qa import _AudioScan
    gov = governor.for_cfg(cfg)
    wd = d / "work"
    wd.mkdir(parents=True, exist_ok=True)
    with gov.slot("heavy", label=f"ref {label}"):
        au = _AudioScan(cfg, src, wd)
        au.run()
    y = au.audio
    sr = au.SR
    if au.error or not len(y):
        return {"has_audio": False}, np.zeros(0, np.float32)
    if prog:
        prog(0.4, "voice activity")
    spans = [(round(a, 3), round(b, 3)) for a, b in A.speech_spans(y, sr)]
    dur = len(y) / sr
    import librosa
    hop = 256
    if prog:
        prog(0.6, "beats")
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    bt = M.beats_from_signal(y, sr, hop=hop, env=env)
    onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=hop, units="time")
    music_win = _music_windows(env, sr / hop, y, sr)
    loud = []
    if len(au.momentary):
        for k in range(int(math.ceil(dur))):
            m = au.momentary[(au.momentary[:, 0] >= k) & (au.momentary[:, 0] < k + 1), 1]
            m = m[m > -70]
            loud.append(round(float(10 * np.log10(np.mean(10 ** (m / 10)))), 1) if len(m) else None)
    music_spans = _merge_spans([(w[0], w[1]) for w in music_win if w[2]], 0.5)
    speech_s = sum(b - a for a, b in spans)
    facts = {"has_audio": True, "duration": round(dur, 3), "integrated_lufs": au.integrated, "true_peak_dbtp": au.true_peak,
             "lra": au.lra, "speech_spans": [list(x) for x in spans], "speech_s": round(speech_s, 2),
             "speech_pct": round(100 * speech_s / max(dur, 1e-6), 1),
             "music_spans": [[round(a, 2), round(b, 2)] for a, b in music_spans],
             "music_pct": round(min(100.0, 100 * sum(b - a for a, b in music_spans) / max(dur, 1e-6)), 1),
             "music_windows": [[round(w[0], 2), round(w[1], 2), bool(w[2]), round(w[3], 3)] for w in music_win],
             "tempo": bt["tempo"], "beats": bt["beats"], "downbeats": bt["downbeats"], "drops": bt["drops"],
             "onsets": [round(float(x), 3) for x in onsets], "beat_resolution_s": round(hop / sr, 4),
             "loudness_curve": loud}
    return facts, y


def _music_windows(env, efps, y, sr, win_s=8.0, step_s=4.0):
    """[(t0, t1, is_music, periodicity)]: rhythmic, audible windows of the onset envelope."""
    out = []
    n = len(env)
    w, st = int(win_s * efps), int(step_s * efps)
    lag0, lag1 = int(0.27 * efps), int(1.5 * efps)
    if n < max(w // 2, lag1 + 2):
        w = n
    k = 0
    while True:
        a = k * st
        b = min(n, a + w)
        if b - a < lag1 + 2:
            break
        x = env[a:b] - env[a:b].mean()
        ac = np.correlate(x, x, mode="full")[len(x) - 1:]
        sc = float(ac[lag0:lag1].max() / ac[0]) if ac[0] > 1e-9 else 0.0
        t0, t1 = a / efps, b / efps
        seg = y[int(t0 * sr):int(t1 * sr)]
        rms = 20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) + 1e-9) if len(seg) else -120
        out.append((t0, t1, bool(sc >= MUSIC_PERIODICITY and rms > -45), sc))
        if b >= n:
            break
        k += 1
    return out


def _merge_spans(spans, join=0.5):
    out = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _overlap(a, b, spans):
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in spans)


# ==================================================================================================
# stage: asr
# ==================================================================================================

def asr_model_name(cfg):
    return os.environ.get("CUTROOM_REF_ASR_MODEL") or getattr(cfg, "ref_asr_model", "") or cfg.asr_model or "small"


def asr_stage(cfg, y, audio_facts, label="asr"):
    from . import asr as A
    if not audio_facts.get("has_audio") or audio_facts.get("speech_s", 0) < A.MIN_SPEECH_S:
        return {"skipped": "no speech"}
    if not A.any_backend_available():
        return {"skipped": "no ASR backend is installed (pip install -e '.[asr]', or '.[mlx]' on Apple Silicon)"}
    model = asr_model_name(cfg)
    with np.errstate(all="ignore"):                    # silent padding makes the mel filter warn; harmless
        lang, segs = A.transcribe_audio(cfg, y, model, label=f"ref {label}")
    words = [[w["start"], w["end"], str(w["word"]).strip()] for s in segs for w in s.get("words", [])
             if str(w["word"]).strip()]
    return {"language": lang, "model": model, "text": " ".join(s["text"] for s in segs),
            "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs], "words": words}


# ==================================================================================================
# assemble
# ==================================================================================================

def _kind(s, st, fr, speech_frac):
    tx = fr.get("text") or {}
    if s["frames"] <= 2 and st["luma"] > 170:
        return "flash", "high"
    if st["luma"] < BLACK_LUMA and st["contrast"] < BLACK_STD:
        return "black", "high"
    top = (fr.get("colors") or [{"pct": 0}])[0]["pct"]
    if tx.get("present") and fr.get("faces", 0) == 0 and st["motion"] < 1.5 and (st["contrast"] < 40 or top >= 60):
        return "title", "medium"
    fh = fr.get("face_h") or 0.0
    mouth = fr.get("mouth")
    if fh >= FACE_TALK_H and speech_frac >= 0.4:
        return "talk", "high" if (mouth is None or mouth > 0) else "medium"
    if fh >= FACE_TALK_H * 0.7 and speech_frac >= 0.25 and (mouth or 0) > 1.0:
        return "talk", "medium"
    if fh == 0 and speech_frac >= 0.85 and s["dur"] >= 3.0 and st["motion"] < 6:
        return "talk", "low"            # continuous speech on a long shot with no frontal face found
    if s["frames"] <= 2:
        return "flash", "medium"
    return "broll", "medium" if fh else "high"


def _mark_montage(shots):
    """B-roll/flash shots shorter than MONTAGE_SHOT, three or more in a row, are montage."""
    run = []

    def close():
        if len(run) >= 3:
            for j in run:
                shots[j]["montage"] = True
        run.clear()
    for j, s in enumerate(shots):
        s["montage"] = False
        if s["kind"] in ("broll", "flash") and s["dur"] < MONTAGE_SHOT:
            run.append(j)
        else:
            close()
    close()


def _segment(feats, min_len, max_k=14, pen_scale=1.0):
    """Optimal partition of an (n, d) feature series into contiguous sections (least squares around each
    section's mean) with a per-section penalty. Returns boundaries [0, b1, ..., n]."""
    n, dim = feats.shape
    if n < 2 * min_len:
        return [0, n]
    cs = np.vstack([np.zeros((1, dim)), np.cumsum(feats, 0)])
    cs2 = np.concatenate([[0.0], np.cumsum((feats ** 2).sum(1))])

    def cost(i, j):                              # vectorised over i (array), j scalar
        m = (j - i)[:, None]
        s = cs[j] - cs[i]
        return (cs2[j] - cs2[i]) - (s ** 2 / np.maximum(m, 1)).sum(1)
    K = min(max_k, n // min_len)
    INF = 1e18
    best = np.full((K + 1, n + 1), INF)
    arg = np.zeros((K + 1, n + 1), np.int64)
    best[0, 0] = 0.0
    for k in range(1, K + 1):
        for j in range(k * min_len, n + 1):
            i = np.arange((k - 1) * min_len, j - min_len + 1)
            if not len(i):
                continue
            v = best[k - 1, i] + cost(i, j)
            t = int(np.argmin(v))
            best[k, j], arg[k, j] = v[t], i[t]
    pen = pen_scale * dim * math.log(max(n, 3)) * 2.0
    scores = [best[k, n] + pen * k if best[k, n] < INF else INF for k in range(1, K + 1)]
    kbest = 1 + int(np.argmin(scores))
    b, j = [n], n
    for k in range(kbest, 0, -1):
        j = int(arg[k, j])
        b.append(j)
    return sorted(set(b))


def _sections(shots, dur, audio, transcript_words):
    """Sections by change points in pacing, speech, talking-head share and loudness (1 s bins)."""
    n = max(1, int(math.ceil(dur)))
    cut_t = np.array([s["start"] for s in shots[1:]])
    centers = np.arange(n) + 0.5
    win = 3.0
    cnt = np.array([((cut_t >= c - win) & (cut_t < c + win)).sum() for c in centers], float)
    pace = np.log((2 * win) / (cnt + 1))
    sp_spans = audio.get("speech_spans") or []
    speech = np.array([_overlap(k, k + 1, sp_spans) for k in range(n)])
    talk_spans = [(s["start"], s["end"]) for s in shots if s["kind"] == "talk"]
    talk = np.array([_overlap(k, k + 1, talk_spans) for k in range(n)])
    loud = np.array([(x if x is not None else -60.0) for x in (audio.get("loudness_curve") or [])][:n] +
                    [-60.0] * max(0, n - len(audio.get("loudness_curve") or [])), float)
    loud = np.clip(loud, -60, 0)

    def smooth(x, k=3):
        return np.convolve(x, np.ones(k) / k, mode="same") if len(x) >= k else x
    F = []
    for x, wgt in ((pace, 1.0), (smooth(speech), 1.0), (smooth(talk), 1.0), (smooth(loud), 0.5)):
        sd = float(np.std(x))
        F.append(wgt * (x - float(np.mean(x))) / max(sd, 0.25 if x is not pace else 0.3))
    feats = np.stack(F, 1)
    min_len = int(max(4, min(10, round(dur * 0.04))))
    bounds = _segment(feats, min_len)
    # snap inner boundaries to the nearest cut within 2 s
    cuts = [s["start"] for s in shots[1:]]
    edges = [0.0]
    for b in bounds[1:-1]:
        t = float(b)
        if cuts:
            c = min(cuts, key=lambda x: abs(x - t))
            if abs(c - t) <= 2.0:
                t = c
        if t - edges[-1] >= 1.0:
            edges.append(t)
    edges.append(round(dur, 4))
    return edges


def _section_rows(edges, shots, audio, dur):
    rows = []
    for k in range(len(edges) - 1):
        a, b = edges[k], edges[k + 1]
        inside = [s for s in shots if s["start"] < b - 1e-6 and s["end"] > a + 1e-6]
        for s in inside:
            if a - 1e-6 <= (s["start"] + s["end"]) / 2 < b + 1e-6:
                s["section"] = k
        span = max(b - a, 1e-6)
        n_cuts = sum(1 for s in shots[1:] if a - 1e-6 <= s["start"] < b - 1e-6)
        n_sh = max(1, len(inside))
        durs = [min(s["end"], b) - max(s["start"], a) for s in inside]

        def share(pred):
            return round(100 * sum(min(s["end"], b) - max(s["start"], a) for s in inside if pred(s)) / span, 1)
        wts = np.array([max(1e-6, d) for d in durs])

        def wavg(key):
            v = np.array([s[key] for s in inside], float)
            return round(float((v * wts).sum() / wts.sum()), 1) if len(v) else None
        lc = audio.get("loudness_curve") or []
        seg = [x for x in lc[int(a):int(math.ceil(b))] if x is not None]
        row = {"i": k, "start": round(a, 3), "end": round(b, 3), "dur": round(b - a, 3), "n_shots": len(inside),
               "asl": round(span / n_sh, 2), "median_shot": round(float(np.median(durs)), 2) if durs else None,
               "cuts_per_s": round(n_cuts / span, 3),
               "speech_pct": round(100 * _overlap(a, b, audio.get("speech_spans") or []) / span, 1),
               "music_pct": round(100 * _overlap(a, b, audio.get("music_spans") or []) / span, 1),
               "talk_pct": share(lambda s: s["kind"] == "talk"),
               "montage_pct": share(lambda s: s.get("montage")),
               "broll_pct": share(lambda s: s["kind"] == "broll" and not s.get("montage")),
               "luma": wavg("luma"), "contrast": wavg("contrast"), "sat": wavg("sat"), "warmth": wavg("warmth"),
               "loudness_lufs": round(float(10 * np.log10(np.mean(10 ** (np.array(seg) / 10)))), 1) if seg else None,
               "pos": _pos_word((a + b) / 2 / max(dur, 1e-6))}
        rows.append(row)
    for r in rows:
        r["name"] = _section_name(r, rows)
    return rows


def _pos_word(f):
    return "the opening" if f < 0.12 else "the early part" if f < 0.35 else "the middle" if f < 0.65 else \
        "the late part" if f < 0.88 else "the ending"


def _section_name(r, rows):
    if r["i"] == 0 and len(rows) > 1 and (r["asl"] <= 2.0 or (r["talk_pct"] < 40 and r["dur"] < 25)) and r["n_shots"] >= 3:
        return "cold open"
    if r["talk_pct"] >= 50:
        return "talk"
    if r["montage_pct"] >= 40:
        return "montage"
    if r["speech_pct"] >= 40:
        return "voice-over b-roll"
    if r["i"] == len(rows) - 1 and len(rows) > 2:
        return "outro"
    return "b-roll"


def _beat_sync(cut_times, audio, fps):
    beats = np.array(audio.get("beats") or [], float)
    if len(beats) < 4 or not cut_times:
        return {"beat_sync_pct": None, "note": "no beat grid (no rhythmic audio)"}
    tol = 1.0 / fps + 0.5 * float(audio.get("beat_resolution_s") or 0.016)
    mspans = audio.get("music_spans") or []
    in_music = [t for t in cut_times if any(a <= t <= b for a, b in mspans)]
    considered = in_music if len(in_music) >= max(3, 0.2 * len(cut_times)) else list(cut_times)
    d = [float(np.min(np.abs(beats - t))) for t in considered]
    on = sum(1 for x in d if x <= tol)
    ibi = float(np.median(np.diff(beats)))
    ons = np.array(audio.get("onsets") or [], float)
    on_onset = sum(1 for t in considered if len(ons) and float(np.min(np.abs(ons - t))) <= tol)
    return {"beat_sync_pct": round(100 * on / len(considered), 1), "cuts_on_beat": on, "cuts_considered": len(considered),
            "considered": "cuts while music plays" if considered is in_music else "all cuts",
            "chance_pct": round(min(100.0, 100 * 2 * tol / max(ibi, 1e-6)), 1), "tolerance_s": round(tol, 4),
            "cuts_on_onset_pct": round(100 * on_onset / len(considered), 1) if len(ons) else None}


def _captions_summary(shots, arr, fps, dur):
    texts = [(s, s["text"]) for s in shots if s.get("text") and s["text"].get("present")]
    spans = {}
    n = int(arr["n"][0])
    for k in TEXT_BANDS:
        x = arr[f"tx_{k}"]
        idx = np.where(~np.isnan(x))[0]
        on = [(int(i) / fps, (int(i) + TEXT_EVERY) / fps) for i in idx if x[i] >= TEXT_SCORE_ON]
        merged = [(a, b) for a, b in _merge_spans(on, 0.3) if b - a >= 0.25]
        spans[k] = merged
    if not texts:
        return {"detected": False, "coverage_pct": 0.0, "spans": []}
    wts = {}
    for s, t in texts:
        wts[t["band"]] = wts.get(t["band"], 0.0) + s["dur"]
    band = max(wts, key=wts.get)
    sel = [t for s, t in texts if t["band"] == band]
    sizes = [t["size_pct"] for t in sel if t.get("size_pct")]
    ys = [t["y_center"] for t in sel if t.get("y_center") is not None]
    nw = [t["n_words"] for t in sel if t.get("n_words")]
    cols = [t["color"] for t in sel if t.get("color")]
    outl = [t["outline"] for t in sel if t.get("outline") is not None]
    cov_spans = _merge_spans([sp for k in spans for sp in spans[k]], 0.0)
    y = float(np.median(ys)) if ys else None
    pos = None if y is None else ("top" if y < 0.33 else "middle" if y < 0.6 else "lower" if y < 0.78 else "bottom")
    size = float(np.median(sizes)) if sizes else None
    colour = None
    if cols:
        rgb = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in cols])
        m = np.median(rgb, axis=0)
        colour = "#%02x%02x%02x" % tuple(int(v) for v in m)
    return {"detected": True, "method": sel[0].get("method"), "band": band, "position": pos,
            "y_center": round(y, 3) if y is not None else None, "size_pct": round(size, 2) if size else None,
            # plan "size" is a font size in px at the output height; glyph boxes are ~0.75 of it
            "approx_plan_size_1080": int(round(size / 100 * 1080 / 0.75)) if size else None,
            "words_per_caption": float(np.median(nw)) if nw else None, "color": colour,
            "outline": (sum(outl) > len(outl) / 2) if outl else None,
            "coverage_pct": round(100 * sum(b - a for a, b in cov_spans) / max(dur, 1e-6), 1),
            "spans": [[round(a, 2), round(b, 2), k] for k in spans for a, b in spans[k]][:2000],
            "samples": [{"shot": s["i"], "t": s["start"], "text": t.get("words", "")} for s, t in texts[:60]]}


def _structure(shots, sections, audio, transcript, dur, fps):
    sp = _merge_spans([tuple(x) for x in audio.get("speech_spans") or []], 0.6)
    sustained = next((a for a, b in sp if b - a >= SUSTAINED_SPEECH), None)
    first_word = None
    if transcript and transcript.get("words"):
        first_word = float(transcript["words"][0][0])
    elif sp:
        first_word = float(sp[0][0])
    # fast cuts until the first long talking shot = a cold open; it ends where that intro line starts
    first_long = next((s for s in shots if (s["kind"] == "talk" and s["dur"] >= 2.0) or
                       (s["dur"] >= 2.5 and s["speech"] >= 0.6 and s["kind"] not in ("title", "black"))), None)
    before = [s for s in shots if first_long and s["end"] <= first_long["start"] + 1e-6]
    if first_long and len(before) >= 4 and first_long["start"] / len(before) <= 2.0:
        cold, basis = first_long["start"], "fast cuts end at the first long talking shot"
        prev_end = 0.0
        for a, b in sp:
            if a > cold:
                break
            if cold - 4.0 <= a and a - prev_end >= 1.0:
                cuts = [s["start"] for s in shots[1:]]
                near = min(cuts, key=lambda c: abs(c - a)) if cuts else a
                cold, basis = (near if abs(near - a) <= 0.5 else a), "the intro line after the teaser starts"
                break
            prev_end = b
    elif sustained is not None and sustained >= 1.0:
        cold, basis = sustained, "first sustained speech"
        if len(sections) > 1 and sections[1]["start"] < cold:
            cold, basis = sections[1]["start"], "first section change"
    else:
        cold, basis = 0.0, "opens straight on talk" if (sustained is not None) else "no cold open found"
    tot = max(dur, 1e-6)

    def share(pred):
        return round(100 * sum(s["dur"] for s in shots if pred(s)) / tot, 1)
    talk = share(lambda s: s["kind"] == "talk")
    mont = share(lambda s: s.get("montage"))
    broll = share(lambda s: s["kind"] == "broll" and not s.get("montage"))
    other = round(max(0.0, 100 - talk - mont - broll), 1)
    last = shots[-1] if shots else None
    tail_speech = _overlap(max(0, dur - 3), dur, audio.get("speech_spans") or [])
    tail_music = _overlap(max(0, dur - 5), dur, audio.get("music_spans") or [])
    if last and (last["kind"] == "black" or (last["luma"] < 30 and len(shots) > 1)):
        end = "fade to black"
    elif last and last["kind"] == "title":
        end = "end card"
    elif last and last["kind"] == "talk" and tail_speech > 1.0:
        end = "hard out on talk"
    elif tail_music > 2.5 and tail_speech < 0.5:
        end = "music outro over b-roll"
    else:
        end = "hard cut on b-roll"
    return {"cold_open_s": round(float(cold), 2), "cold_open_basis": basis,
            "first_sustained_speech_s": round(sustained, 2) if sustained is not None else None,
            "time_to_first_word_s": round(first_word, 2) if first_word is not None else None,
            "talk_pct": talk, "broll_pct": broll, "montage_pct": mont, "other_pct": other,
            "ending": {"type": end, "last_shot_dur": last["dur"] if last else None}}


def assemble(arr, shots_raw, frames, audio, transcript, fps, dur, flashes, sidecar=None) -> dict:
    shots = []
    sp_spans = audio.get("speech_spans") or []
    beats = np.array(audio.get("beats") or [], float)
    tol = 1.0 / fps + 0.5 * float(audio.get("beat_resolution_s") or 0.016)
    side_segs = (sidecar or {}).get("segments") or []
    for s, fr in zip(shots_raw, frames):
        st = s["_stats"]
        sf = _overlap(s["start"], s["end"], sp_spans) / max(s["dur"], 1e-6)
        kind, conf = _kind(s, st, fr, sf)
        if side_segs:
            seg = next((g for g in side_segs if g["start"] <= (s["start"] + s["end"]) / 2 < g["end"]), None)
            if seg:
                k = str(seg.get("type") or seg.get("kind") or "").lower()
                mapped = {"talk": "talk", "broll": "broll", "montage": "broll", "matchcut": "broll",
                          "title": "title", "black": "black", "still": "broll"}.get(k)
                if mapped and kind not in ("flash", "black"):
                    kind, conf = mapped, "plan"
        row = {"i": s["i"], "start": s["start"], "end": s["end"], "dur": s["dur"], "f0": s["f0"], "f1": s["f1"],
               "frames": s["frames"], "kind": kind, "kind_conf": conf, "flash": s["frames"] <= 2,
               **{k: v for k, v in st.items() if not k.startswith("_")},
               "faces": fr.get("faces", 0), "face_h": fr.get("face_h", 0.0), "mouth": fr.get("mouth"),
               "colors": fr.get("colors") or [], "speech": round(sf, 2), "text": fr.get("text"),
               "thumb": fr.get("thumb"), "section": 0}
        if s["i"] > 0 and len(beats):
            dt = float(beats[np.argmin(np.abs(beats - s["start"]))] - s["start"])
            row["beat_dt"] = round(dt, 4)
            row["beat"] = abs(dt) <= tol
        else:
            row["beat_dt"], row["beat"] = None, None
        shots.append(row)
    _mark_montage(shots)
    edges = _sections(shots, dur, audio, (transcript or {}).get("words"))
    sections = _section_rows(edges, shots, audio, dur)
    cut_times = [s["start"] for s in shots[1:]]
    nb = max(1, int(math.ceil(dur)))
    cps = [0] * nb
    for t in cut_times:
        cps[min(nb - 1, int(t))] += 1
    durs = [s["dur"] for s in shots]
    pacing = {"n_shots": len(shots), "n_cuts": len(cut_times), "asl": round(dur / max(1, len(shots)), 3),
              "median_shot": round(float(np.median(durs)), 3) if durs else None,
              "min_shot": round(min(durs), 3) if durs else None, "max_shot": round(max(durs), 3) if durs else None,
              "shots_under_0_5s": sum(1 for x in durs if x < 0.5), "first3_cuts": sum(1 for t in cut_times if t < 3.0),
              "cuts_per_s": cps, "cuts_per_s_overall": round(len(cut_times) / max(dur, 1e-6), 3),
              "by_section": [{"i": r["i"], "name": r["name"], "start": r["start"], "end": r["end"], "asl": r["asl"],
                              "cuts_per_s": r["cuts_per_s"]} for r in sections]}
    structure = _structure(shots, sections, audio, transcript, dur, fps)
    pacing["time_to_first_word_s"] = structure["time_to_first_word_s"]
    sync = _beat_sync(cut_times, audio, fps) if audio.get("has_audio") else {"beat_sync_pct": None, "note": "no audio"}
    w = np.array([max(1e-6, s["dur"]) for s in shots])

    def wavg(k):
        v = np.array([s[k] for s in shots], float)
        return round(float((v * w).sum() / w.sum()), 1) if len(v) else None
    colour = {"overall": {"luma": wavg("luma"), "contrast": wavg("contrast"), "sat": wavg("sat"), "warmth": wavg("warmth")},
              "by_section": [{"i": r["i"], "name": r["name"], "luma": r["luma"], "contrast": r["contrast"], "sat": r["sat"],
                              "warmth": r["warmth"]} for r in sections]}
    aud = {k: v for k, v in audio.items() if k != "music_windows"}
    aud.update(sync)
    return {"shots": shots, "cuts": [round(t, 4) for t in cut_times], "flashes": flashes, "pacing": pacing,
            "sections": sections, "audio": aud, "captions": _captions_summary(shots, arr, fps, dur), "colour": colour,
            "structure": structure}


def _sheets(d, shots, fps, title, n_frames):
    """Contact sheets (qa's sheet helper): one tile per shot, so both sides of every cut sit side by side."""
    import cv2
    from .qa import _contact_sheets
    thumbs = {}
    for s in shots:
        if s.get("thumb"):
            thumbs[(s["f0"] + s["f1"] - 1) // 2] = d / s["thumb"]
    if not thumbs:
        return []
    first = cv2.imread(str(next(iter(thumbs.values()))))
    if first is None:
        return []
    th, tw = first.shape[:2]

    class _VS:
        n, w, h = n_frames, tw, th

        @staticmethod
        def frame(f):
            p = thumbs.get(f)
            im = cv2.imread(str(p)) if p else None
            if im is not None and im.shape[:2] != (th, tw):
                im = cv2.resize(im, (tw, th))
            return im
    rows = [{"idx": s["i"], "sample_frames": [(s["f0"] + s["f1"] - 1) // 2], "dur": s["dur"],
             "kind": s["kind"] + (" montage" if s.get("montage") else "") + (" beat" if s.get("beat") else ""),
             "faces": s.get("faces")} for s in shots]
    for old in d.glob("sheet_*.jpg"):
        old.unlink()
    return [p.name for p in _contact_sheets(_VS, rows, fps, d, title, {}, note="one frame from the middle of each shot")]


# ==================================================================================================
# the pipeline
# ==================================================================================================

def _work_sig(src, extra=""):
    st = Path(src).stat()
    return hashlib.sha1(f"{VERSION}|{Path(src).resolve()}|{st.st_size}|{st.st_mtime_ns}|{extra}".encode()).hexdigest()[:12]


def measure(cfg, d, picture, audio_src, fps, title, prog, asr=True, sidecar=None, ref_label="") -> dict:
    """Run scan -> frames -> audio -> asr -> assemble for one video into directory d. Resumable."""
    d = Path(d)
    wd = d / "work"
    wd.mkdir(parents=True, exist_ok=True)
    sig = _work_sig(audio_src, f"{picture}|{asr}|{asr_model_name(cfg) if asr else ''}")
    if (wd / "sig.txt").exists() and (wd / "sig.txt").read_text().strip() != sig:
        shutil.rmtree(wd, ignore_errors=True)
        wd.mkdir(parents=True, exist_ok=True)
    (wd / "sig.txt").write_text(sig)
    t0 = time.time()
    # scan
    prog.begin("scan")
    npz = wd / "scan.npz"
    if npz.exists():
        arr = dict(np.load(npz))
    else:
        arr = scan_video(cfg, picture, fps, npz, prog, label=f"scan {ref_label}")
    n = int(arr["n"][0])
    cuts, flashes = find_cuts(arr, fps)
    shots = _shots_from_cuts(cuts, n, fps)
    for s in shots:
        s["_stats"] = _shot_stats(arr, s)
    prog.end()
    # frames
    prog.begin("frames")
    fj = wd / "frames.json"
    frames = _jload(fj)
    if not frames or len(frames) != len(shots):
        frames = frames_stage(cfg, picture, arr, shots, fps, d, prog, label=f"frames {ref_label}")
        _jsave(fj, frames)
    prog.end()
    # devices (burns, frames, cards, typed titles) in the opening
    dj = wd / "devices.json"
    dev = _jload(dj)
    if not dev or dev.get("version") != DEV_VERSION:
        try:
            dev = devices_for(cfg, picture, fps, [int(s["f0"]) for s in shots[1:]], DEV_WINDOW_S, work=wd,
                              label=f"devices {ref_label}")
        except Exception as e:  # noqa: BLE001 - devices are additive; never fail a measurement on them
            dev = {"version": DEV_VERSION, "error": f"{type(e).__name__}: {e}"}
    # audio
    prog.begin("audio")
    aj = wd / "audio.json"
    audio = _jload(aj)
    y = None
    if audio is None:
        audio, y = audio_stage(cfg, audio_src, d, prog, label=f"audio {ref_label}")
        _jsave(aj, audio)
    prog.end()
    # asr
    prog.begin("asr")
    tj = wd / "transcript.json"
    transcript = _jload(tj)
    if transcript is None:
        if asr and audio.get("has_audio") and audio.get("speech_s", 0) >= 1.5:
            from . import asr as A
            if y is None:
                y = A.load_audio(cfg, audio_src)
            prog(0.05, f"transcribing {audio.get('speech_s', 0):.0f}s of speech with {asr_model_name(cfg)}", force=True)
            transcript = asr_stage(cfg, y, audio, label=ref_label)
        else:
            transcript = {"skipped": "no speech" if asr else "not requested"}
        _jsave(tj, transcript)
    prog.end()
    # assemble
    prog.begin("assemble")
    dur = n / fps
    an = assemble(arr, shots, frames, audio, transcript if transcript and transcript.get("words") is not None else None,
                  fps, dur, flashes, sidecar=sidecar)
    w, h, W, H = (int(x) for x in arr["dims"])
    an.update({"version": VERSION, "title": title, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "source": {"fps": round(fps, 4), "frames": n, "duration": round(dur, 3), "width": W, "height": H},
               "transcript": transcript if transcript and transcript.get("words") is not None else None,
               "transcript_note": (transcript or {}).get("skipped"),
               "tools": {"tesseract": bool(shutil.which("tesseract")), "faces": _faces_available(),
                         "asr_model": (transcript or {}).get("model"),
                         "detectors": ["ContentDetector", "AdaptiveDetector", "frame-difference spikes", "black/flash runs"]},
               "detector_counts": {"content": int(len(arr["content_cuts"])), "adaptive": int(len(arr["adaptive_cuts"]))}})
    an["devices"] = dev
    _tag_shots(an)
    an["sheets"] = _sheets(d, an["shots"], fps, title or d.name, n)
    an["summary"] = summary_lines(an)
    an["seconds"] = round(time.time() - t0, 1)
    prog.end()
    return an


def _faces_available():
    try:
        from .qa import _faces_detector
        return _faces_detector() is not None
    except Exception:  # noqa: BLE001
        return False


def analyze(cfg, rid, force=False, asr=True, echo=print) -> dict:
    """Measure reference `rid` and write refs/<id>/analysis.json. Resumable: finished stages are kept."""
    rid = resolve_id(cfg, rid)
    d = ref_dir(cfg, rid)
    meta = load_meta(cfg, rid)
    src = source_path(cfg, rid)
    if src is None or not Path(src).exists():
        save_meta(cfg, rid, status="error", error="the reference file is missing")
        raise FileNotFoundError(f"reference {rid}: source file missing")
    out = d / "analysis.json"
    if out.exists() and not force:
        an = _jload(out, {})
        if an.get("version") == VERSION and an.get("_sig") == _work_sig(src, "final"):
            save_meta(cfg, rid, status="analyzed", stage=None, progress=1.0, error=None)
            return an
    if force:
        shutil.rmtree(d / "work", ignore_errors=True)
    prog = _Prog(cfg, rid, echo)
    t0 = time.time()
    try:
        prog.begin("proxy")
        proxy = make_proxy(cfg, rid, echo=lambda *_: None)
        prog.end()
        fields = _probe_fields(cfg, proxy)
        fps = float(Fraction(fields["fps_rational"]))
        an = measure(cfg, d, proxy, src, fps, meta.get("title") or rid, prog, asr=asr, ref_label=rid)
    except BaseException as e:
        save_meta(cfg, rid, status="error" if not isinstance(e, KeyboardInterrupt) else "added",
                  error=f"{type(e).__name__}: {str(e)[-500:]}", stage=prog.stage)
        raise
    an.update({"id": rid, "origin": meta.get("origin"), "url": meta.get("url"), "_sig": _work_sig(src, "final"),
               "seconds": round(time.time() - t0, 1)})
    an["source"].update({"file": src.name, "proxy": "proxy.mp4", "fps_rational": fields["fps_rational"]})
    _jsave(out, an)
    save_meta(cfg, rid, status="analyzed", stage=None, progress=1.0, error=None, analyzed=time.time(),
              seconds=an["seconds"], n_shots=an["pacing"]["n_shots"])
    echo(f"{PROGRESS_PREFIX} {rid} 1.000 done: {an['pacing']['n_shots']} shots in {an['seconds']:.0f}s")
    for ln in an["summary"]:
        echo("  " + ln)
    return an


def load_analysis(cfg, rid) -> dict:
    rid = resolve_id(cfg, rid)
    an = _jload(ref_dir(cfg, rid) / "analysis.json")
    if not an:
        raise KeyError(f"reference {rid} is not analyzed yet (cutroom ref analyze {rid})")
    return an


def pending(cfg) -> list:
    """Ids of references that still need (or failed) an analysis."""
    out = []
    for m in list_refs(cfg):
        if m.get("status") != "analyzed" or not (ref_dir(cfg, m["id"]) / "analysis.json").exists():
            out.append(m["id"])
    return out


# ==================================================================================================
# summaries + cut map
# ==================================================================================================

def _mmss(t):
    t = max(0.0, float(t or 0))
    return f"{int(t // 60)}:{t % 60:04.1f}"


def summary_numbers(an) -> dict:
    p, st, au, cap = an.get("pacing", {}), an.get("structure", {}), an.get("audio", {}), an.get("captions", {})
    return {"duration": (an.get("source") or {}).get("duration"), "n_shots": p.get("n_shots"), "asl": p.get("asl"),
            "first3_cuts": p.get("first3_cuts"), "cold_open_s": st.get("cold_open_s"), "talk_pct": st.get("talk_pct"),
            "broll_pct": st.get("broll_pct"), "montage_pct": st.get("montage_pct"),
            "beat_sync_pct": au.get("beat_sync_pct"), "tempo": au.get("tempo"),
            "integrated_lufs": au.get("integrated_lufs"), "captions": cap.get("position") if cap.get("detected") else None,
            "n_sections": len(an.get("sections") or [])}


def summary_lines(an) -> list:
    p, st, au, cap = an["pacing"], an["structure"], an["audio"], an["captions"]
    dur = an["source"]["duration"] if "source" in an else sum(s["dur"] for s in an["shots"])
    out = [f"{_mmss(dur)} · {p['n_shots']} shots · ASL {p['asl']:.2f}s (median {p['median_shot']:.2f}s) · "
           f"{p['shots_under_0_5s']} shots under 0.5s · {len(an.get('flashes') or [])} flash/1-2 frame shots",
           f"cold open {st['cold_open_s']:.1f}s ({st['cold_open_basis']}) · {p['first3_cuts']} cuts in the first 3s · "
           f"first word at {st['time_to_first_word_s'] if st['time_to_first_word_s'] is not None else '-'}s",
           f"talking head {st['talk_pct']:.0f}% · b-roll {st['broll_pct']:.0f}% · montage {st['montage_pct']:.0f}% · "
           f"other {st['other_pct']:.0f}% · ending: {st['ending']['type']}"]
    secs = " | ".join(f"{s['name']} {_mmss(s['start'])}-{_mmss(s['end'])} ASL {s['asl']:.1f}s" for s in an["sections"])
    out.append(f"sections: {secs}")
    if au.get("has_audio"):
        bs = au.get("beat_sync_pct")
        out.append(f"loudness {au.get('integrated_lufs')} LUFS, true peak {au.get('true_peak_dbtp')} dBTP · speech "
                   f"{au.get('speech_pct')}% · music ~{au.get('music_pct')}% · tempo {au.get('tempo')} BPM · beat sync "
                   + (f"{bs:.0f}% of {au.get('cuts_considered')} cuts (chance {au.get('chance_pct')}%)" if bs is not None
                      else f"n/a ({au.get('note', '')})"))
    else:
        out.append("no audio")
    if cap.get("detected"):
        out.append(f"captions/text: {cap.get('position')} band, ~{cap.get('size_pct')}% of frame height "
                   f"(~{cap.get('approx_plan_size_1080')} px at 1080p), {cap.get('words_per_caption') or '?'} words, "
                   f"colour {cap.get('color')}, outline {cap.get('outline')}, on screen {cap.get('coverage_pct')}% "
                   f"({cap.get('method')})")
    else:
        out.append("no on-screen text detected")
    c = an["colour"]["overall"]
    out.append(f"picture: luma {c['luma']} · contrast {c['contrast']} · saturation {c['sat']} · warmth {c['warmth']}")
    return out


def cutmap(cfg, rid) -> list:
    """One compact row per shot, in order: what the AI rebuilds from one-to-one."""
    an = load_analysis(cfg, rid)
    return cutmap_rows(an)


def cutmap_rows(an) -> list:
    secs = {s["i"]: s["name"] for s in an.get("sections", [])}
    rows = []
    for s in an["shots"]:
        t = s.get("text") or {}
        rows.append({"i": s["i"], "start": s["start"], "dur": s["dur"], "frames": s["frames"],
                     "kind": s["kind"] + ("/montage" if s.get("montage") else ""), "beat": s.get("beat"),
                     "text": (t.get("words") or ("(text)" if t.get("present") else "")) if t else "",
                     "luma": s["luma"], "faces": s["faces"], "section": secs.get(s.get("section"), ""),
                     "devices": s.get("devices") or [], "thumb": s.get("thumb")})
    return rows


# ==================================================================================================
# compare
# ==================================================================================================

def _sidecar_for(video):
    p = Path(video).with_suffix(".json")
    if p.exists():
        from .qa import load_sidecar
        return load_sidecar(p)
    return None


def latest_render_for_plan(cfg, slug):
    """Newest rendered video whose sidecar says it was rendered from plans/<slug>[_vN|.draft].json, or None."""
    rx = re.compile(rf"^{re.escape(slug)}(_v\d+|\.draft)?\.json$")
    best = None
    for side in (Path(cfg.work) / "renders").glob("*.json"):
        if side.name.endswith((".qa.json",)):
            continue
        d = _jload(side, {}) or {}
        if not isinstance(d, dict) or not rx.match(Path(str(d.get("plan") or "")).name):
            continue
        # the video next to its sidecar first: `output` is absolute and goes stale when a project is moved or copied
        vid = next((side.with_suffix(x) for x in (".mp4", ".mov") if side.with_suffix(x).exists()), None)
        if not vid and d.get("output") and Path(str(d["output"])).exists():
            vid = Path(str(d["output"]))
        if vid and vid.exists() and (best is None or vid.stat().st_mtime > best.stat().st_mtime):
            best = vid
    return best


def measure_render(cfg, video, echo=print) -> dict:
    """The reference measurements for a render (no ASR; kinds from its sidecar). Cached by file."""
    video = Path(video).resolve()
    st = video.stat()
    sig = hashlib.sha1(f"{VERSION}|{video}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]
    d = refs_dir(cfg) / "_scans" / sig
    out = d / "analysis.json"
    an = _jload(out)
    if an and an.get("version") == VERSION:
        if (an.get("devices") or {}).get("version") != DEV_VERSION:
            an["devices"] = devices_for(cfg, video, float(an["source"]["fps"]), [int(s["f0"]) for s in an["shots"][1:]],
                                        DEV_WINDOW_S, work=d / "work", label=f"devices {video.stem}")
            _tag_shots(an)
            _jsave(out, an)
        return an
    d.mkdir(parents=True, exist_ok=True)
    fields = _probe_fields(cfg, video)
    fps = float(Fraction(fields["fps_rational"]))
    side = _sidecar_for(video)
    prog = _Prog(cfg, None, echo, weights={"scan": 0.6, "frames": 0.15, "audio": 0.15, "asr": 0.0, "assemble": 0.1},
                 write_meta=False)
    an = measure(cfg, d, video, video, fps, video.stem, prog, asr=False, sidecar=side, ref_label=video.stem)
    an["render"] = str(video)
    raw = _jload(video.with_suffix(".json"), {}) or {}
    secs = raw.get("sections") if isinstance(raw, dict) else None
    if secs and len(secs) > 1 and str(secs[0].get("name", "")).strip().lower() in ("cold open", "intro", "teaser", "hook"):
        an["structure"]["cold_open_s"] = round(float(secs[1].get("start") or secs[0].get("end") or 0), 2)
        an["structure"]["cold_open_basis"] = f"the plan's \"{secs[0]['name']}\" section"
    if side and side.get("caption_position"):
        an["captions"]["plan_position"] = side["caption_position"]
    _jsave(out, an)
    _jsave(d / "meta.json", {"video": str(video), "sig": sig, "measured": time.time()})
    return an


def timeline_metrics(tl, style=None, out_h=None) -> dict:
    """Pacing-only measurements from a resolved timeline (render.resolve_timeline) or a render sidecar
    (qa.load_sidecar): no picture or sound is decoded."""
    pieces = tl.get("pieces")
    if pieces is not None:
        v1 = sorted([p for p in pieces if p.get("track") == "V1"], key=lambda p: p["start"])
        segs = [{"start": p["start"], "end": p["end"], "kind": p.get("type") or p.get("kind"), "clip": p.get("clip"),
                 "in": p.get("in"), "out": p.get("out")} for p in v1]
        beats = [float(b) for b in tl.get("beats") or []]
        dur = float(tl.get("duration") or (segs[-1]["end"] if segs else 0))
        fps = float(tl.get("fps") or 25)
    else:
        segs = [{"start": s["start"], "end": s["end"], "kind": s.get("kind"), "clip": s.get("clip"), "in": s.get("in"),
                 "out": s.get("out")} for s in tl.get("segments") or []]
        beats = []
        dur = max([s["end"] for s in segs] or [0])
        fps = float(tl.get("fps") or 25)
    shots = []
    for s in segs:
        if s["end"] - s["start"] <= 1e-6:
            continue
        if shots:
            p = shots[-1]
            cont = (p["clip"] is not None and p["clip"] == s["clip"] and s["in"] is not None and p["out"] is not None
                    and abs(float(s["in"]) - float(p["out"])) <= 1.0 / fps + 1e-3 and abs(s["start"] - p["end"]) < 1e-3)
            if cont:
                p["end"], p["out"] = s["end"], s["out"]
                continue
        shots.append(dict(s))
    kmap = {"talk": "talk", "broll": "broll", "montage": "broll", "matchcut": "broll", "title": "title", "black": "black",
            "still": "broll", "clip": "broll"}
    rows = []
    for i, s in enumerate(shots):
        k = str(s["kind"] or "").lower()
        rows.append({"i": i, "start": round(s["start"], 4), "end": round(s["end"], 4), "dur": round(s["end"] - s["start"], 4),
                     "kind": kmap.get(k, "broll"), "montage": k == "montage", "luma": None})
    cut_times = [s["start"] for s in rows[1:]]
    nb = max(1, int(math.ceil(dur)))
    cps = [0] * nb
    for t in cut_times:
        cps[min(nb - 1, int(t))] += 1
    sync = {"beat_sync_pct": None}
    if beats:
        tol = 1.0 / fps + 1e-3
        on = sum(1 for t in cut_times if min(abs(b - t) for b in beats) <= tol)
        sync = {"beat_sync_pct": round(100 * on / max(1, len(cut_times)), 1), "cuts_considered": len(cut_times)}
    tot = max(dur, 1e-6)

    def share(pred):
        return round(100 * sum(s["dur"] for s in rows if pred(s)) / tot, 1)
    secs = tl.get("sections") or []
    cold = None
    if secs and len(secs) > 1 and str(secs[0].get("name", "")).lower() in ("cold open", "intro", "teaser", "hook"):
        cold = float(secs[1]["start"])
    durs = [s["dur"] for s in rows]
    cap = {"detected": False}
    cs = (style or {}).get("captions") if isinstance(style, dict) else None
    if cs and cs.get("enabled", True):
        size = cs.get("size")
        cap = {"detected": True, "method": "plan", "position": cs.get("position", "bottom"),
               "size_pct": round(float(size) * 0.75 / float(out_h) * 100, 2) if size and out_h else None}
    an = {"mode": "timeline", "shots": rows, "cuts": cut_times,
          "source": {"duration": round(dur, 3), "fps": fps},
          "pacing": {"n_shots": len(rows), "n_cuts": len(cut_times), "asl": round(dur / max(1, len(rows)), 3),
                     "median_shot": round(float(np.median(durs)), 3) if durs else None,
                     "first3_cuts": sum(1 for t in cut_times if t < 3.0), "cuts_per_s": cps,
                     "shots_under_0_5s": sum(1 for x in durs if x < 0.5)},
          "structure": {"cold_open_s": cold, "talk_pct": share(lambda s: s["kind"] == "talk"),
                        "montage_pct": share(lambda s: s["montage"]),
                        "broll_pct": share(lambda s: s["kind"] == "broll" and not s["montage"])},
          "audio": sync, "captions": cap, "colour": {"overall": {}}}
    return an


def _window_stats(an, a, b):
    """ASL and cuts/s of `an` inside [a, b) (timeline seconds)."""
    shots = an["shots"]
    span = max(b - a, 1e-6)
    inside = [s for s in shots if s["start"] < b and s["end"] > a]
    n_cuts = sum(1 for s in shots[1:] if a <= s["start"] < b)
    return {"asl": round(span / max(1, len(inside)), 2), "cuts_per_s": round(n_cuts / span, 3), "n_shots": len(inside)}


def _curve(an, bins=40):
    dur = float(an["source"]["duration"] or 0)
    if dur <= 0:
        return [0.0] * bins
    ct = np.array(an.get("cuts") or [s["start"] for s in an["shots"][1:]], float)
    edges = np.linspace(0, dur, bins + 1)
    h, _ = np.histogram(ct, edges)
    return [round(float(x) / (dur / bins), 3) for x in h]


def compare(cfg, rid, target, echo=print, style=None, out_h=None) -> dict:
    """Side-by-side metrics + plain-English gaps between reference `rid` and `target`: a rendered video
    (measured exactly like the reference, kinds from its sidecar), a render sidecar .json, or a resolved
    timeline dict (pacing-only)."""
    ref = load_analysis(cfg, rid)
    if isinstance(target, dict):
        yours = timeline_metrics(target, style, out_h)
        mode, label = "timeline", target.get("label") or "draft timeline"
    else:
        tp = Path(str(target))
        if not tp.is_absolute():
            tp = (Path(cfg.work) / tp) if (Path(cfg.work) / tp).exists() else tp.resolve()
        if tp.suffix.lower() == ".json":
            vid = next((tp.with_suffix(x) for x in (".mp4", ".mov", ".m4v") if tp.with_suffix(x).exists()), None)
            if vid is None:
                from .qa import load_sidecar
                side = load_sidecar(tp) or {}
                yours = timeline_metrics({"segments": side.get("segments") or [], "fps": side.get("fps")})
                mode, label = "timeline", tp.name
            else:
                tp = vid
        if tp.suffix.lower() != ".json":
            if not tp.exists():
                raise FileNotFoundError(tp)
            yours = measure_render(cfg, tp, echo=echo)
            mode, label = "render", tp.name
    return compare_analyses(ref, yours, mode=mode, label=label)


def _compare_devices(ref, yours, row, gap, window_s=15.0):
    """Rows + gaps for the reference's devices (card, burns, frames, typed title) in the opening."""
    rd, yd = ref.get("devices"), yours.get("devices")
    if not rd:
        return
    yd = yd or {}
    rc, yc = rd.get("card"), yd.get("card")
    row("card", "Title card at the start", f"{rc['t1']:.2f}s" if rc else "none", f"{yc['t1']:.2f}s" if yc else "none")
    if rc and not yc:
        gap("card", "big", f"the reference opens on a {rc['t1']:.2f}s title card"
                           f"{(' (' + rc['text'] + ')') if rc.get('text') else ''}; yours has none", 0.0)
    elif rc and yc and abs(rc["t1"] - yc["t1"]) > 0.2:
        gap("card", "small", f"your card lasts {yc['t1']:.2f}s vs {rc['t1']:.2f}s", 0.0)
    rb = [b for b in rd.get("burns") or [] if b["t"] <= window_s]
    yb = [b for b in yd.get("burns") or [] if b["t"] <= window_s]
    row("burns", f"Film burns in the first {window_s:.0f} s", len(rb), len(yb))
    row("burn_times", "Burn times", ", ".join(f"{b['t']:.2f}" for b in rb) or "none",
        ", ".join(f"{b['t']:.2f}" for b in yb) or "none")
    if rb and len(yb) < len(rb):
        gap("burns", "big" if not yb else "small",
            f"the reference has {len(rb)} two-frame film burns in its first {window_s:.0f} s ("
            + ", ".join(f"{b['t']:.2f}s" for b in rb) + f"); yours has {len(yb)}")
    for kind in ("inset", "gate"):
        rr = sum(r["t1"] - r["t0"] for r in rd.get("frames") or [] if r["kind"] == kind and r["t0"] < window_s)
        yy = sum(r["t1"] - r["t0"] for r in yd.get("frames") or [] if r["kind"] == kind and r["t0"] < window_s)
        row(f"frame_{kind}", f"{kind.capitalize()} frame time (first {window_s:.0f} s)", round(rr, 2), round(yy, 2), "s")
        if rr >= 0.3 and yy < 0.5 * rr:
            gap(f"frame_{kind}", "big" if yy == 0 else "small",
                f"the reference spends {rr:.1f}s in a{'n' if kind == 'inset' else ''} {kind} frame in its opening; yours {yy:.1f}s")
    rt = [t for t in rd.get("titles") or [] if t.get("type_on") and t["t0"] < window_s]
    yt = [t for t in yd.get("titles") or [] if t.get("type_on") and t["t0"] < window_s]
    row("typed_title", "Typed title", f"{rt[0]['t0']:.2f}s" if rt else "none", f"{yt[0]['t0']:.2f}s" if yt else "none")
    if rt and not yt:
        gap("typed_title", "big", f"the reference types a title on at {rt[0]['t0']:.2f}s; yours has none", rt[0]["t0"])


def compare_analyses(ref, yours, mode="render", label="") -> dict:
    rd, yd = float(ref["source"]["duration"]), float(yours["source"]["duration"] or 0)
    rows, gaps = [], []

    def row(metric, title, r, y, unit=""):
        rows.append({"metric": metric, "label": title, "ref": r, "yours": y, "unit": unit,
                     "delta": (round(y - r, 3) if isinstance(r, (int, float)) and isinstance(y, (int, float)) else None)})

    def gap(metric, sev, text, t=None):
        gaps.append({"metric": metric, "severity": sev, "text": text, "t": None if t is None else round(float(t), 2)})

    def ratio(a, b):
        return (max(a, b) / max(min(a, b), 1e-6)) if (a and b) else None
    rp, yp = ref["pacing"], yours["pacing"]
    rs, ys = ref["structure"], yours["structure"]
    row("duration", "Duration", round(rd, 2), round(yd, 2), "s")
    if yd and (yd / rd < 0.8 or yd / rd > 1.25):
        gap("duration", "info", f"yours runs {_mmss(yd)} vs the reference's {_mmss(rd)}; pacing below is compared by "
                                f"relative position (the same share of each runtime)")
    row("n_shots", "Shots", rp["n_shots"], yp["n_shots"])
    row("asl", "Average shot length", rp["asl"], yp["asl"], "s")
    rr = ratio(rp["asl"], yp["asl"])
    if rr and rr >= 1.25:
        gap("asl", "big" if rr >= 1.6 else "small",
            f"overall you cut every {yp['asl']:.1f}s vs every {rp['asl']:.1f}s in the reference "
            f"({'slower' if yp['asl'] > rp['asl'] else 'faster'})")
    row("first3_cuts", "Cuts in the first 3 s", rp["first3_cuts"], yp["first3_cuts"])
    if abs(rp["first3_cuts"] - yp["first3_cuts"]) >= 2:
        gap("first3_cuts", "big" if abs(rp["first3_cuts"] - yp["first3_cuts"]) >= 3 else "small",
            f"the reference cuts {rp['first3_cuts']} times in its first 3 seconds; you cut {yp['first3_cuts']} times", 0.0)
    rc, yc = rs.get("cold_open_s"), ys.get("cold_open_s")
    row("cold_open_s", "Cold open", rc, yc, "s")
    if rc is not None and yc is not None and abs(rc - yc) >= max(1.0, 0.25 * max(rc, 1e-6)):
        gap("cold_open", "big" if ratio(rc, yc) and ratio(rc, yc) >= 1.6 else "small",
            f"your cold open is {yc:.1f}s vs {rc:.1f}s in the reference", yc)
    elif rc and yc is None:
        gap("cold_open", "info", f"the reference opens with a {rc:.1f}s cold open; your plan has no section named "
                                 f"\"cold open\" to compare (pacing-only check)")
    rt, yt = rs.get("time_to_first_word_s"), ys.get("time_to_first_word_s")
    if rt is not None or yt is not None:
        row("time_to_first_word_s", "First spoken word", rt, yt, "s")
        if rt is not None and yt is not None and abs(rt - yt) >= 2.0:
            gap("first_word", "small", f"the first spoken word comes at {yt:.1f}s in yours vs {rt:.1f}s in the reference", yt)
    # sections, aligned by relative position
    secs = []
    for s in ref["sections"]:
        a, b = s["start"] / rd * yd, s["end"] / rd * yd
        w = _window_stats(yours, a, b)
        secs.append({"i": s["i"], "name": s["name"], "pos": s["pos"], "ref_start": s["start"], "ref_end": s["end"],
                     "yours_start": round(a, 2), "yours_end": round(b, 2), "ref_asl": s["asl"], "yours_asl": w["asl"],
                     "ref_cuts_per_s": s["cuts_per_s"], "yours_cuts_per_s": w["cuts_per_s"]})
        r = ratio(s["asl"], w["asl"])
        if r and r >= 1.4 and (s["end"] - s["start"]) >= 3.0:
            gap("section_asl", "big" if r >= 2.0 else "small",
                f"in {s['pos']} ({s['name']}, reference {_mmss(s['start'])}-{_mmss(s['end'])}) you cut every "
                f"{w['asl']:.1f}s vs every {s['asl']:.1f}s", a)
    rcurve, ycurve = _curve(ref), _curve(yours)
    corr = None
    if np.std(rcurve) > 1e-6 and np.std(ycurve) > 1e-6:
        corr = round(float(np.corrcoef(rcurve, ycurve)[0, 1]), 3)
    diffs = np.array(ycurve) - np.array(rcurve)
    j = int(np.argmax(np.abs(diffs))) if len(diffs) else 0
    if corr is not None and corr < 0.3 and len(diffs):
        f = (j + 0.5) / len(diffs)
        gap("pacing_shape", "small", f"your pacing doesn't follow the reference's shape (correlation {corr:.2f}); the "
                                     f"biggest difference is in {_pos_word(f)}: {ycurve[j]:.2f} cuts/s in yours vs "
                                     f"{rcurve[j]:.2f} in the reference", f * yd)
    rb, yb = ref["audio"].get("beat_sync_pct"), yours["audio"].get("beat_sync_pct")
    row("beat_sync_pct", "Cuts on the beat", rb, yb, "%")
    if rb is not None and yb is not None and rb - yb >= 20:
        gap("beat_sync", "big" if rb - yb >= 40 else "small",
            f"{rb:.0f}% of the reference's cuts land on a beat vs {yb:.0f}% of yours")
    for k, title in (("talk_pct", "talking head"), ("broll_pct", "b-roll"), ("montage_pct", "montage")):
        r_, y_ = rs.get(k), ys.get(k)
        row(k, title.capitalize() + " share", r_, y_, "%")
        if r_ is not None and y_ is not None and abs(r_ - y_) >= 15:
            gap(k, "big" if abs(r_ - y_) >= 30 else "small",
                f"{title} is {r_:.0f}% of the reference's runtime vs {y_:.0f}% of yours")
    rl, yl = ref["audio"].get("integrated_lufs"), yours["audio"].get("integrated_lufs")
    row("integrated_lufs", "Loudness", rl, yl, "LUFS")
    if rl is not None and yl is not None and abs(rl - yl) >= 1.5:
        gap("loudness", "small" if abs(rl - yl) < 4 else "big",
            f"you are {abs(yl - rl):.1f} LU {'louder' if yl > rl else 'quieter'} than the reference ({yl:.1f} vs {rl:.1f} LUFS)")
    rcap, ycap = ref.get("captions") or {}, yours.get("captions") or {}
    row("caption_position", "Caption position", rcap.get("position"), ycap.get("position"))
    row("caption_size_pct", "Caption size (% of height)", rcap.get("size_pct"), ycap.get("size_pct"), "%")
    if rcap.get("detected") and not ycap.get("detected") and (rcap.get("coverage_pct") or 0) >= 10:
        gap("captions", "big", f"the reference has on-screen text for {rcap['coverage_pct']:.0f}% of its runtime "
                               f"({rcap.get('position')} band); yours has none detected")
    elif rcap.get("detected") and ycap.get("detected"):
        if rcap.get("position") and ycap.get("position") and rcap["position"] != ycap["position"]:
            gap("caption_position", "small", f"your captions sit in the {ycap['position']} band vs the reference's "
                                             f"{rcap['position']} band")
        rz = ratio(rcap.get("size_pct"), ycap.get("size_pct"))
        if rz and rz >= 1.3:
            gap("caption_size", "small", f"your captions are {'bigger' if ycap['size_pct'] > rcap['size_pct'] else 'smaller'}"
                                         f": {ycap['size_pct']:.1f}% of the frame height vs {rcap['size_pct']:.1f}%")
        if rcap.get("outline") is False and ycap.get("outline"):
            gap("caption_outline", "small", "your captions have a dark outline/shadow; the reference's don't (rule 53)")
    _compare_devices(ref, yours, row, gap)
    rco, yco = (ref.get("colour") or {}).get("overall") or {}, (yours.get("colour") or {}).get("overall") or {}
    for k, title, thr in (("luma", "Brightness (mean luma)", 12), ("sat", "Saturation", None), ("contrast", "Contrast", 10)):
        row(k, title, rco.get(k), yco.get(k))
    if rco.get("luma") is not None and yco.get("luma") is not None and abs(rco["luma"] - yco["luma"]) >= 12:
        gap("luma", "small", f"your picture is {'brighter' if yco['luma'] > rco['luma'] else 'darker'} "
                             f"(mean luma {yco['luma']:.0f} vs {rco['luma']:.0f})")
    rz = ratio(rco.get("sat"), yco.get("sat"))
    if rz and rz >= 1.25:
        gap("saturation", "small", f"your colours are {'more' if yco['sat'] > rco['sat'] else 'less'} saturated "
                                   f"({yco['sat']:.0f} vs {rco['sat']:.0f})")
    order = {"big": 0, "small": 1, "info": 2}
    gaps.sort(key=lambda g: order.get(g["severity"], 3))
    return {"mode": mode, "target": label, "ref": {"id": ref.get("id"), "title": ref.get("title"), "duration": rd},
            "yours": {"duration": yd, "label": label}, "rows": rows, "sections": secs,
            "curve": {"bins": len(rcurve), "ref": rcurve, "yours": ycurve, "corr": corr}, "gaps": gaps,
            "n_big": sum(1 for g in gaps if g["severity"] == "big"),
            "verdict": "close" if not any(g["severity"] in ("big", "small") for g in gaps) else
            ("far" if any(g["severity"] == "big" for g in gaps) else "near")}


# ==================================================================================================
# devices: film burns, frame changes (inset / window-box gate / letterbox), title cards, type-on titles
# ==================================================================================================
DEV_VERSION = 1
DEV_W, DEV_H = 320, 180
DEV_WINDOW_S = 60.0              # devices are measured in the opening minute (where intros live)


def _box_from(mask, thr=0.25):
    """(x0, y0, x1, y1) fractions of the region whose rows/columns are at least `thr` content."""
    h, w = mask.shape
    cols = mask.mean(axis=0)
    rows = mask.mean(axis=1)
    xs = np.where(cols > thr)[0]
    ys = np.where(rows > thr)[0]
    if not len(xs) or not len(ys):
        return None
    return (xs[0] / w, ys[0] / h, (xs[-1] + 1) / w, (ys[-1] + 1) / h)


def device_features(rgb):
    """Per-frame numbers the device detectors run on (rgb uint8, any size; ~320x180 is plenty)."""
    a = rgb.astype(np.int16)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    h, w = R.shape
    luma = 0.2126 * R + 0.7152 * G + 0.0722 * B
    out = {"luma": float(luma.mean())}
    # frame box: content = differs from the flat outer ring
    ring = np.concatenate([a[:2].reshape(-1, 3), a[-2:].reshape(-1, 3), a[:, :2].reshape(-1, 3), a[:, -2:].reshape(-1, 3)])
    rc = np.median(ring, axis=0)
    out["ring"] = [float(x) for x in rc]
    out["ring_std"] = float(np.abs(ring - rc).max(axis=1).mean())
    content = np.abs(a - rc).max(axis=2) > 20
    box = _box_from(content) or (0.0, 0.0, 1.0, 1.0)
    out["box"] = [round(float(x), 4) for x in box]
    # an inner matte: inside the box, a second flat dark ring
    x0, y0, x1, y1 = (int(round(box[0] * w)), int(round(box[1] * h)), int(round(box[2] * w)), int(round(box[3] * h)))
    out["inner"] = None
    if x1 - x0 > 20 and y1 - y0 > 20:
        sub = a[y0 + 1:y1 - 1, x0 + 1:x1 - 1]
        sr = np.concatenate([sub[:2].reshape(-1, 3), sub[-2:].reshape(-1, 3), sub[:, :2].reshape(-1, 3), sub[:, -2:].reshape(-1, 3)])
        sc = np.median(sr, axis=0)
        if sc.max() < 30 and np.abs(sr - sc).max(axis=1).mean() < 8 and np.abs(sc - rc).max() > 4:
            ib = _box_from(np.abs(sub - sc).max(axis=2) > 20)
            if ib and (ib[2] - ib[0] < 0.97 or ib[3] - ib[1] < 0.97):
                bw, bh = (x1 - x0 - 2) / w, (y1 - y0 - 2) / h
                out["inner"] = [round(float((x0 + 1) / w + ib[0] * bw), 4), round(float((y0 + 1) / h + ib[1] * bh), 4),
                                round(float((x0 + 1) / w + ib[2] * bw), 4), round(float((y0 + 1) / h + ib[3] * bh), 4)]
    # burn light
    yy, xx = np.mgrid[0:h, 0:w]
    red = (R > 170) & (G < 95) & (B < 95) & (R - G > 95)
    org = (R > 200) & (G > 70) & (G < 240) & (B < 120) & (R - B > 110)
    for k, m in (("red", red), ("orange", org)):
        out[f"{k}_pct"] = float(m.mean())
        out[f"{k}_c"] = [float(xx[m].mean() / w), float(yy[m].mean() / h)] if m.any() else None
    # saturated "ink" (graphic text): high saturation, high value, yellow-ish hue
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1)
    ink = (sat > 0.6) & (mx > 170) & (R > 150) & (G > 120) & (B < 110) & (np.abs(R - G) < 90)
    out["ink_pct"] = float(ink.mean())
    if ink.sum() > 6:
        ys, xs = np.where(ink)
        out["ink_box"] = [float(np.percentile(xs, 1) / w), float(np.percentile(ys, 1) / h),
                          float((np.percentile(xs, 99) + 1) / w), float((np.percentile(ys, 99) + 1) / h)]
        px = a[ink]
        top = px[px.max(axis=1) >= np.percentile(px.max(axis=1), 50)]      # the solid fill, not the edges
        out["ink_rgb"] = [float(x) for x in np.median(top, axis=0)]
        out["bg_luma"] = float(luma[~ink].mean())
    else:
        out["ink_box"], out["ink_rgb"], out["bg_luma"] = None, None, out["luma"]
    return out


def _hex(rgb):
    return "#" + "".join(f"{int(round(max(0, min(255, v)))):02X}" for v in rgb)


def _frame_kind(f):
    """full | inset | gate | letterbox for one frame's features."""
    x0, y0, x1, y1 = f["box"]
    w, h = x1 - x0, y1 - y0
    flat = f["ring_std"] < 7 and max(f["ring"]) < 50
    if not flat or (w > 0.985 and h > 0.985):
        return "full"
    if w > 0.985 and h < 0.95:
        return "letterbox"
    if w > 0.9 and h > 0.82 and x0 > 0.003 and y0 > 0.01:
        return "gate"
    tinted = f["ring"][0] - f["ring"][2] >= 6          # the gate's maroon-black, not a pure black surround
    if tinted and (w < 0.985 or h < 0.985):
        return "gate_inner"                              # a matte / letterbox nested inside the gate
    if w < 0.92 and h < 0.92 and 0.04 < x0 and 0.04 < y0:
        return "inset"
    return "full"


def detect_devices(feats, fps, cuts=None):
    """Device events from per-frame features: {"version", "card", "burns", "frames", "titles"}."""
    n = len(feats)
    out = {"version": DEV_VERSION, "fps": fps, "frames_scanned": n, "card": None, "burns": [], "frames": [], "titles": []}
    if not n:
        return out
    # ---- title card at the very start: dark textured background, one centred saturated text block
    k = 0
    while k < n:
        f = feats[k]
        ib = f.get("ink_box")
        if not (ib and 0.004 < f["ink_pct"] < 0.25 and f["bg_luma"] < 35 and abs((ib[0] + ib[2]) / 2 - 0.5) < 0.1
                and abs((ib[1] + ib[3]) / 2 - 0.5) < 0.15):
            break
        k += 1
    if k >= 8:
        widths = [feats[i]["ink_box"][2] - feats[i]["ink_box"][0] for i in range(k)]
        wf = float(np.median(widths[-max(3, k // 3):]))
        settle = next((i for i, w_ in enumerate(widths) if w_ >= 0.99 * wf), 0)
        box = [float(np.median([feats[i]["ink_box"][j] for i in range(k - max(3, k // 3), k)])) for j in range(4)]
        out["card"] = {"f0": 0, "f1": k, "t0": 0.0, "t1": round(k / fps, 4), "box": [round(v, 4) for v in box],
                       "size": round(box[3] - box[1], 4), "width": round(box[2] - box[0], 4),
                       "color": _hex(np.median([feats[i]["ink_rgb"] for i in range(k)], axis=0)),
                       "scale_from": round(widths[0] / wf, 3) if wf else 1.0, "scale_frames": int(settle),
                       "bg_luma": round(float(np.median([feats[i]["bg_luma"] for i in range(k)])), 1), "text": None}
    card_end = out["card"]["f1"] if out["card"] else 0
    if out["card"] and card_end < n - 1:
        # the card leaves on a soft burn: its last frame warms and brightens (a dim red glow lower left)
        cl = float(np.median([feats[i]["luma"] for i in range(max(0, card_end - 6), card_end - 1)]))
        last = feats[card_end - 1]
        if last["luma"] > cl + 6:
            out["burns"].append({"frame": card_end, "t": round(card_end / fps, 4), "frames": 2, "side": "right",
                                 "soft": True, "out": {"red_pct": round(last["red_pct"], 3), "centroid": None},
                                 "in": {"orange_pct": round(feats[card_end]["orange_pct"], 3), "centroid": None},
                                 "peak_luma": round(last["luma"], 1), "base_luma": round(cl, 1),
                                 "luma_jump": round(last["luma"] - cl, 1)})
    # ---- burns: a red glow on the last outgoing frame and/or an orange disc on the first incoming frame
    red = np.array([f["red_pct"] for f in feats])
    org = np.array([f["orange_pct"] for f in feats])
    lum = np.array([f["luma"] for f in feats])
    cutset = set(int(c) for c in (cuts or []))
    i = max(2, card_end - 2)
    while i < n - 2:
        a_ok = red[i - 1] > 0.035 and red[i - 1] > 3 * max(red[i - 2], 0.004)
        b_ok = org[i] > 0.10 and org[i] > 3 * max(org[i + 1], 0.01) and org[i] > 3 * max(org[i - 2], 0.01)
        ca = feats[i - 1].get("red_c")
        cb = feats[i].get("orange_c")
        if (a_ok and b_ok) or (b_ok and (i in cutset or i - 1 in cutset)) or (a_ok and org[i] > 0.05):
            base = float(np.mean([lum[max(0, i - 3)], lum[min(n - 1, i + 2)]]))
            out["burns"].append({"frame": i, "t": round(i / fps, 4), "frames": 2,
                                 "side": "right" if (cb or [0.8])[0] >= 0.5 else "left",
                                 "out": {"red_pct": round(float(red[i - 1]), 3), "centroid": [round(v, 3) for v in ca] if ca else None},
                                 "in": {"orange_pct": round(float(org[i]), 3), "centroid": [round(v, 3) for v in cb] if cb else None},
                                 "peak_luma": round(float(max(lum[i - 1], lum[i])), 1), "base_luma": round(base, 1),
                                 "luma_jump": round(float(max(lum[i - 1], lum[i]) - base), 1)})
            i += 3
            continue
        i += 1
    burn_frames = {b["frame"] + d for b in out["burns"] for d in (-1, 0)}
    # ---- frame runs (inset / gate / letterbox)
    kinds = [("card" if i < card_end else ("burn" if i in burn_frames else _frame_kind(f))) for i, f in enumerate(feats)]
    runs, s = [], 0
    for i in range(1, n + 1):
        if i == n or kinds[i] != kinds[s]:
            runs.append((s, i, kinds[s]))
            s = i
    last_gate = None
    for f0, f1, kd in runs:
        if kd == "gate":
            last_gate = [float(v) for v in np.median(np.array([feats[i]["box"] for i in range(f0, f1)]), axis=0)]
        if kd in ("full", "card", "burn") or f1 - f0 < 2:
            continue
        if kd == "gate_inner":
            boxes = np.array([feats[i]["box"] for i in range(f0, f1)])
            r = {"f0": f0, "f1": f1, "t0": round(f0 / fps, 4), "t1": round(f1 / fps, 4), "kind": "gate",
                 "box": [round(v, 4) for v in (last_gate or [0.013, 0.042, 0.986, 0.948])],
                 "color": _hex(np.median([feats[i]["ring"] for i in range(f0, f1)], axis=0)),
                 "inner": {"box": [round(float(v), 4) for v in np.median(boxes, axis=0)], "color": "#000000"}}
            out["frames"].append(r)
            continue
        boxes = np.array([feats[i]["box"] for i in range(f0, f1)])
        inners = [feats[i]["inner"] for i in range(f0, f1) if feats[i].get("inner")]
        r = {"f0": f0, "f1": f1, "t0": round(f0 / fps, 4), "t1": round(f1 / fps, 4), "kind": kd,
             "box": [round(float(v), 4) for v in np.median(boxes, axis=0)],
             "color": _hex(np.median([feats[i]["ring"] for i in range(f0, f1)], axis=0))}
        if len(inners) > (f1 - f0) / 2:
            r["inner"] = {"box": [round(float(v), 4) for v in np.median(np.array(inners), axis=0)], "color": "#000000"}
        prev = out["frames"][-1] if out["frames"] else None
        if prev and prev["kind"] == kd and f0 - prev["f1"] <= 3 and np.abs(np.array(prev["box"]) - np.array(r["box"])).max() < 0.02:
            prev["f1"], prev["t1"] = f1, r["t1"]          # a burn or a 1-frame blip inside one framed run
        else:
            out["frames"].append(r)
    # ---- type-on titles: a centred saturated text block whose right edge grows frame by frame
    i = card_end
    while i < n:
        f = feats[i]
        ib = f.get("ink_box")
        if not (ib and f["ink_pct"] > 0.0006 and 0.02 < ib[3] - ib[1] < 0.2 and kinds[i] != "burn"):
            i += 1
            continue
        j = i
        while j + 1 < n and feats[j + 1].get("ink_box") and feats[j + 1]["ink_pct"] > 0.0005 and kinds[j + 1] != "burn":
            j += 1
        if j - i + 1 >= 8:
            rights = [feats[x]["ink_box"][2] for x in range(i, j + 1)]
            lefts = [feats[x]["ink_box"][0] for x in range(i, j + 1)]
            final = float(np.median(rights[-5:]))
            settle = next((x for x, r_ in enumerate(rights) if r_ >= final - 0.01), 0)
            box = [float(np.median([feats[x]["ink_box"][q] for x in range(j - 4, j + 1)])) for q in range(4)]
            cx = (box[0] + box[2]) / 2
            grew = settle >= 2 and rights[0] < final - 0.05 and np.ptp(lefts[:settle + 5]) < 0.03
            if abs(cx - 0.5) < 0.15:
                shot0 = max([c for c in (cuts or []) if c <= i] or [0])
                hgt = box[3] - box[1]
                est_chars = max(1, int(round((box[2] - box[0]) * 16 / 9 / max(1e-3, hgt * 0.42))))
                t = {"f0": i, "f1": j + 1, "t0": round(i / fps, 4), "t1": round((j + 1) / fps, 4),
                     "box": [round(v, 4) for v in box], "size": round(hgt * 1.25, 4),
                     "color": _hex(np.median([feats[x]["ink_rgb"] for x in range(i, j + 1)], axis=0)), "text": None,
                     "est_chars": est_chars}
                if grew:
                    t["type_on"] = {"f0": i, "f1": i + settle + 1, "dur_s": round((settle + 1) / fps, 3),
                                    "start_s": round((i - shot0) / fps, 3),
                                    "char_s": round((settle + 1) / fps / est_chars, 4)}
                out["titles"].append(t)
        i = j + 1
    return out


def scan_devices(cfg, video, fps, window_s=None, prog=None, label="devices"):
    """Decode `video` at 320x180 and compute device features for every frame (first `window_s` s only)."""
    gov = governor.for_cfg(cfg)
    cmd = _nice_prefix(gov) + [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if window_s:
        cmd += ["-t", f"{float(window_s):.3f}"]
    cmd += ["-i", str(video), "-map", "0:v:0", "-an", "-vf", f"scale={DEV_W}:{DEV_H}:flags=area", "-fps_mode",
            "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    fs = DEV_W * DEV_H * 3
    feats = []
    with gov.slot("heavy", label=f"ref {label}"):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=fs * 4)
        try:
            while True:
                buf = proc.stdout.read(fs)
                if len(buf) < fs:
                    break
                feats.append(device_features(np.frombuffer(buf, np.uint8).reshape(DEV_H, DEV_W, 3)))
                if prog and len(feats) % 500 == 0:
                    prog(min(0.99, len(feats) / max(1.0, (window_s or 600) * fps)), f"{len(feats)} frames")
        finally:
            proc.stdout.close()
            proc.wait()
    return feats


def _ocr_card(cfg, video, t, box):
    """Read a card's / title's text with tesseract when it is installed (None otherwise)."""
    exe = shutil.which("tesseract")
    if not exe:
        return None
    import cv2
    with tempfile.TemporaryDirectory(prefix="cutroom-ocr-") as td:
        p = Path(td) / "f.png"
        try:
            media.ffmpeg(cfg, ["-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1", str(p)])
            img = cv2.imread(str(p))
            h, w = img.shape[:2]
            x0, y0, x1, y1 = box
            crop = img[max(0, int((y0 - 0.03) * h)):int((y1 + 0.03) * h), max(0, int((x0 - 0.03) * w)):int((x1 + 0.03) * w)]
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[..., 1]
            _, bw = cv2.threshold(g, 120, 255, cv2.THRESH_BINARY_INV)
            cv2.imwrite(str(p), bw)
            r = subprocess.run([exe, str(p), "-", "--psm", "7"], capture_output=True, text=True, timeout=30)
            txt = re.sub(r"[^A-Za-z0-9 ,.'!?&-]", "", r.stdout or "").strip()
            return txt or None
        except Exception:  # noqa: BLE001
            return None


def devices_for(cfg, video, fps, cuts=None, window_s=None, work=None, label="devices"):
    """Detect the devices of `video` (cached in work/devices.json when `work` is given)."""
    if work:
        cached = _jload(Path(work) / "devices.json")
        if cached and cached.get("version") == DEV_VERSION and cached.get("window_s") == window_s:
            return cached
    feats = scan_devices(cfg, video, fps, window_s, label=label)
    dev = detect_devices(feats, fps, cuts)
    dev["window_s"] = window_s
    if dev.get("card"):
        dev["card"]["text"] = _ocr_card(cfg, video, max(0.0, dev["card"]["t1"] - 0.1), dev["card"]["box"])
    for t in dev.get("titles") or []:
        t["text"] = _ocr_card(cfg, video, t["t1"] - 0.1, t["box"])
        if t["text"] and t.get("type_on"):
            nch = max(1, len(t["text"].replace(" ", "")))
            t["type_on"]["char_s"] = round(t["type_on"]["dur_s"] / nch, 4)
    if work:
        _jsave(Path(work) / "devices.json", dev)
    return dev


def ensure_devices(cfg, rid, window_s=DEV_WINDOW_S):
    """Add device detection to an analysed reference (no re-analysis): writes analysis["devices"] and
    tags every cut-map shot with the devices on it. Returns the updated analysis."""
    rid = resolve_id(cfg, rid)
    d = ref_dir(cfg, rid)
    an = load_analysis(cfg, rid)
    if (an.get("devices") or {}).get("version") == DEV_VERSION:
        return an
    video = d / "proxy.mp4"
    if not video.exists():
        video = source_path(cfg, rid)
    fps = float(an["source"]["fps"])
    cuts = [int(s["f0"]) for s in an["shots"][1:]]
    dev = devices_for(cfg, video, fps, cuts, window_s, work=d / "work", label=f"devices {rid}")
    an["devices"] = dev
    _tag_shots(an)
    _jsave(d / "analysis.json", an)
    return an


def _tag_shots(an):
    dev = an.get("devices") or {}
    for s in an.get("shots", []):
        tags = []
        c = dev.get("card")
        if c and s["f0"] < c["f1"] and s["f1"] > c["f0"]:
            tags.append("card")
        for b in dev.get("burns") or []:
            if s["f0"] - 1 <= b["frame"] <= s["f1"]:
                tags.append("burn")
        for r in dev.get("frames") or []:
            if min(s["f1"], r["f1"]) - max(s["f0"], r["f0"]) > (s["f1"] - s["f0"]) / 2:
                tags.append(r["kind"])
        for t in dev.get("titles") or []:
            if s["f0"] < t["f1"] and s["f1"] > t["f0"]:
                tags.append("typed_title" if t.get("type_on") else "title")
        s["devices"] = sorted(set(tags))


def device_kinds(an):
    """The device kinds a reference measurably uses (rule 38: only these may be rebuilt)."""
    dev = an.get("devices") or {}
    k = []
    if dev.get("card"):
        k.append("card")
    if dev.get("burns"):
        k.append("burn")
    k += [r["kind"] for r in dev.get("frames") or []]
    k += ["typed_title" if t.get("type_on") else "title" for t in dev.get("titles") or []]
    return sorted(set(k))
