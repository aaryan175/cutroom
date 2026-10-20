"""Library ingest: scan -> probe -> proxies -> contact sheets.

Every step is resumable: it only touches clips that still need the work, writes outputs
atomically (tmp file + rename) and records progress in the `clips` table.
"""
import math
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from . import db, governor, media
from .config import VIDEO_EXTS

MIN_SIZE = 100 * 1024          # smaller files are thumbnails / junk
BATCH = 2000                   # rows per commit during scan
TILE_W = 320                   # contact-sheet tile width
SHEET_COLS = 6
SIDECAR_EXTS = (".xml", ".json", ".sidecar")
RAW_SEQ_EXTS = {".ari", ".arx"}        # ARRIRAW frame sequences: reported, never registered (one file per frame)


# -- helpers ---------------------------------------------------------------------------------

def _update(con, cid, fields: dict):
    con.execute(f"UPDATE clips SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), cid])


def _log(msg: str):
    print(msg, flush=True)


def _workers(cfg, cap=None) -> int:
    n = cfg.n_workers()
    return max(1, min(n, cap) if cap else n)


def display_size(row) -> tuple:
    """(width, height) as the clip is meant to be seen (rotation applied)."""
    w, h = int(row["width"] or 0), int(row["height"] or 0)
    if abs(int(row["rotation"] or 0)) % 180 == 90:
        w, h = h, w
    return w, h


def _hint_regex(hints) -> re.Pattern:
    # a hint must stand alone as a token ("slog3", "s-log", "hlg") so "catalog" or "blog" don't match
    alts = "|".join(re.escape(h.lower()) for h in sorted(hints, key=len, reverse=True) if h)
    return re.compile(rf"(?<![a-z])({alts})(?![a-z])")


def _iter_files(root: str):
    """Yield (path, size, mtime) for regular files under root. Follows directory symlinks but
    never visits the same directory twice (no loops)."""
    seen = set()
    stack = [os.path.realpath(root)]
    while stack:
        d = stack.pop()
        try:
            st = os.stat(d)
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                name = e.name
                if name.startswith("."):          # hidden files/dirs, AppleDouble ._*
                    continue
                try:
                    if e.is_dir():
                        stack.append(e.path)
                    elif e.is_file():
                        st = e.stat()
                        yield e.path, st.st_size, st.st_mtime
                except OSError:
                    continue


# -- scan ------------------------------------------------------------------------------------

def scan(cfg, con) -> dict:
    """Register every video file under cfg.library_roots. New or changed files get status 'new';
    unchanged files keep their status. Files that vanished are reported, not deleted."""
    t = time.time()
    known = {r["path"]: (r["size"], r["mtime"]) for r in con.execute("SELECT path, size, mtime FROM clips")}
    seen_paths, seen_ids = set(), set()
    n_files = n_new = n_changed = 0
    pending = 0
    now = time.time()
    raw_seq = set()
    for root in cfg.library_roots:
        root_real = os.path.realpath(root)
        if not os.path.isdir(root_real):
            _log(f"  ! library root missing: {root}")
            continue
        for path, size, mtime in _iter_files(root_real):
            ext = os.path.splitext(path)[1].lower()
            if ext in RAW_SEQ_EXTS:
                raw_seq.add(os.path.dirname(path))
                continue
            if ext not in VIDEO_EXTS or size < MIN_SIZE:
                continue
            n_files += 1
            cid = db.clip_id(path)
            if cid in seen_ids or path in seen_paths:      # same file reached through two links
                continue
            seen_ids.add(cid)
            seen_paths.add(path)
            prev = known.get(path)
            if prev and prev[0] == size and abs((prev[1] or 0) - mtime) < 1e-3:
                continue
            if prev:
                n_changed += 1
            else:
                n_new += 1
            rel = os.path.relpath(path, root_real)
            db.upsert(con, "clips", {"id": cid, "path": path, "rel": rel, "size": size, "mtime": mtime,
                                     "status": "new", "error": None, "proxy": None, "sheet": None,
                                     "updated": now})
            pending += 1
            if pending >= BATCH:
                con.commit()
                pending = 0
    con.commit()
    missing = len(set(known) - seen_paths)
    if raw_seq:
        _log(f"  ! {len(raw_seq)} ARRIRAW frame-sequence folder(s) (.ari/.arx) skipped: ffmpeg can't decode ARRIRAW. "
             f"Record ProRes on the camera or make ProRes proxies (ARRI Reference Tool / Resolve): "
             f"{', '.join(sorted(raw_seq)[:3])}{' ...' if len(raw_seq) > 3 else ''}")
    dt = time.time() - t
    _log(f"scan: {n_files} video files in {dt:.1f}s · {n_new} new · {n_changed} changed · "
         f"{missing} missing on disk · {con.execute('SELECT COUNT(*) FROM clips').fetchone()[0]} in library")
    return {"files": n_files, "new": n_new, "changed": n_changed, "missing": missing, "seconds": dt,
            "arriraw_sequences": sorted(raw_seq)}


def register_files(cfg, con, paths) -> list:
    """Register just these video files (each must sit under one of cfg.library_roots); returns their clip ids.
    New or changed files get status 'new', unchanged ones keep their status. Used by the studio's Import."""
    roots = [os.path.realpath(r) for r in cfg.library_roots]
    ids, now = [], time.time()
    for path in paths:
        path = os.path.realpath(str(path))
        root = next((r for r in sorted(roots, key=len, reverse=True)
                     if path == r or path.startswith(r.rstrip(os.sep) + os.sep)), None)
        if root is None:
            _log(f"  ! not inside a library folder, skipped: {path}")
            continue
        if os.path.splitext(path)[1].lower() not in VIDEO_EXTS or not os.path.isfile(path):
            _log(f"  ! not a video file, skipped: {path}")
            continue
        st = os.stat(path)
        cid = db.clip_id(path)
        prev = con.execute("SELECT size, mtime FROM clips WHERE id=?", (cid,)).fetchone()
        if not (prev and prev["size"] == st.st_size and abs((prev["mtime"] or 0) - st.st_mtime) < 1e-3):
            db.upsert(con, "clips", {"id": cid, "path": path, "rel": os.path.relpath(path, root), "size": st.st_size,
                                     "mtime": st.st_mtime, "status": "new", "error": None, "proxy": None,
                                     "sheet": None, "updated": now})
        ids.append(cid)
    con.commit()
    _log(f"register: {len(ids)} of {len(paths)} files in the library")
    return ids


# -- probe -----------------------------------------------------------------------------------

_dir_cache: dict = {}


def _sidecars(path: str) -> list:
    """Small metadata files next to a clip that start with its stem (camera XML, colour notes)."""
    d, name = os.path.split(path)
    stem = os.path.splitext(name)[0].lower()
    if d not in _dir_cache:
        if len(_dir_cache) > 256:
            _dir_cache.clear()
        try:
            _dir_cache[d] = [n for n in os.listdir(d) if n.lower().endswith(SIDECAR_EXTS) and not n.startswith(".")]
        except OSError:
            _dir_cache[d] = []
    return [os.path.join(d, n) for n in _dir_cache[d] if n.lower().startswith(stem)]


def detect_log(cfg, path: str, info: dict, summary: dict) -> bool:
    """Best-effort log/HLG detection from transfer characteristics, filename, tags and sidecars.
    analyze.py may additionally flip 10-bit, very desaturated clips to log."""
    rx = _hint_regex(cfg.log_transfer_hint)
    trc = (summary.get("color_transfer") or "").lower()
    if trc in ("arib-std-b67",) or rx.search(trc):
        return True
    if rx.search(os.path.basename(path).lower()):
        return True
    tag_text = " ".join(str(v) for s in info.get("streams", []) for v in (s.get("tags") or {}).values())
    tag_text += " " + " ".join(str(v) for v in (info.get("format", {}).get("tags") or {}).values())
    if re.search(r"gamma[^\n]{0,60}", tag_text.lower()) and rx.search(tag_text.lower()):
        return True
    for sc in _sidecars(path):
        try:
            if os.path.getsize(sc) > 256 * 1024:
                continue
            txt = Path(sc).read_text(errors="ignore").lower()
        except OSError:
            continue
        for m in re.finditer(r"gamma[^\n]{0,60}", txt):      # e.g. CaptureGammaEquation value="s-log3-cine"
            if rx.search(m.group(0)):
                return True
    return False


def probe_all(cfg, con, limit=None, ids=None):
    """ffprobe clips with status 'new'. `limit` bounds how many are probed this run; `ids` = only these clips."""
    rows = con.execute("SELECT id, path, rel FROM clips WHERE status='new' ORDER BY rel").fetchall()
    if ids is not None:
        rows = [r for r in rows if r["id"] in ids]
    if limit:
        rows = rows[:limit]
    if not rows:
        _log("probe: nothing to do")
        return
    t = time.time()
    ok = err = 0

    from . import cameras as CAM

    def work(r):
        info = media.probe(cfg, r["path"])
        s = media.summarize_probe(info)
        raw = CAM.raw_kind(r["path"], info)
        sd = CAM.standin(cfg, r["path"]) if raw else None
        if sd:                                        # RAW master: picture facts come from its decodable stand-in
            sinfo = media.probe(cfg, sd)
            ss = media.summarize_probe(sinfo)
            s.update({k: ss[k] for k in ("codec", "pix_fmt", "color_transfer") if ss.get(k)})
            s["width"], s["height"] = s.get("width") or ss.get("width"), s.get("height") or ss.get("height")
        if not s.get("width"):
            raise media.FFError("no video stream")
        s["is_log"] = int(detect_log(cfg, r["path"], info, s))
        ident = CAM.analyze(cfg, {"path": r["path"], "rel": r["rel"], "duration": s.get("duration"),
                                  "pix_fmt": s.get("pix_fmt")}, info=info)
        return s, ident, raw, sd

    with ThreadPoolExecutor(_workers(cfg)) as ex:
        futs = {ex.submit(work, r): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            try:
                s, ident, raw, sd = f.result()
                if raw and not sd:
                    s.update(status="raw", error=f"{'Blackmagic RAW' if raw == 'braw' else 'ARRIRAW'}: ffmpeg can't decode "
                             "it; add a ProRes stand-in (cutroom braw)", updated=time.time())
                else:
                    s.update(status="probed", error=None, updated=time.time())
                _update(con, r["id"], s)
                CAM.store(con, r["id"], ident)
                ok += 1
            except Exception as e:  # noqa: BLE001 - any probe failure marks the clip
                _update(con, r["id"], {"status": "error", "error": f"probe: {str(e)[-300:]}", "updated": time.time()})
                err += 1
            if i % 200 == 0:
                con.commit()
                _log(f"  probe {i}/{len(rows)} ({i / (time.time() - t):.0f}/s)")
    con.commit()
    _log(f"probe: {ok} ok · {err} errors · {time.time() - t:.1f}s")


# -- proxies ---------------------------------------------------------------------------------

# Same colour pipe as render.grade_chain: decode with an explicit BT.709 matrix into float RGB, apply the
# LUT there, then convert back to BT.709 limited-range 4:2:0. Without in_color_matrix swscale assumes
# BT.601 for the YUV->RGB step the LUT needs, which shifts hues (greens/skin) in every graded proxy.
# threads=1: the colorspace filter's Floyd-Steinberg dither is not deterministic with slice threads (render.HOUSE_TAIL)
PROXY_LUT_TAIL = ("colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv420p"
                  ":dither=fsb:threads=1")
PROXY_709_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


def proxy_lut(cfg, row):
    """The LUT a proxy of this clip gets baked in, or None: the clip's camera transform (cameras.py, without the
    per-day match, which previews add on top) when it has one, else cfg.lut for log clips."""
    if isinstance(row, dict) and "_cam_lut" in row:
        if row["_cam_lut"]:
            return row["_cam_lut"]
    return cfg.lut if cfg.lut and row["is_log"] and os.path.exists(cfg.lut) else None


def with_camera_lut(cfg, con, row) -> dict:
    """row as a dict carrying its camera transform LUT (`_cam_lut`) and decode source (`_src`) for the proxy."""
    from . import cameras as CAM
    d = dict(row)
    try:
        d["_cam_lut"], _ = CAM.lut_for_clip(cfg, con, d, {}, match=False)
    except Exception as e:  # noqa: BLE001 - a missing camera LUT falls back to the legacy proxy grade
        _log(f"  ! camera transform for {d.get('rel')}: {e}")
        d["_cam_lut"] = None
    d["_src"] = CAM.decode_path(con, d)
    return d


def lut_sha(path) -> str:
    import hashlib
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _proxy_args(cfg, row, src, out, enc):
    H = int(cfg.proxy_height)
    lut = proxy_lut(cfg, row)
    if lut:
        vf = [f"scale=-2:'min({H},ih)':flags=bicubic:in_color_matrix=bt709:out_range=full",
              "format=gbrpf32le", f"lut3d=file='{lut}':interp=tetrahedral", "format=gbrp16le", PROXY_LUT_TAIL]
        tags = PROXY_709_TAGS + ["-metadata", f"comment=cutroom proxy graded lut={lut_sha(lut)}"]
    else:
        vf = [f"scale=-2:'min({H},ih)':out_range=tv", "format=yuv420p"]
        tags = ["-metadata", "comment=cutroom proxy ungraded"]
    if (row["fps"] or 0) > 25.5:
        vf.insert(1, "fps=25")
    return ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf),
            *enc, "-g", "25", "-keyint_min", "1",
            "-c:a", "aac", "-ac", "1", "-b:a", "96k",
            "-map_metadata", "-1", *tags, "-dn", "-sn", "-movflags", "+faststart", "-f", "mp4", out]


PROXY_INFO_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_info (
  clip_id TEXT PRIMARY KEY, graded INTEGER, lut_sha TEXT, height INTEGER, bitrate TEXT, size INTEGER, updated REAL
);
"""


def record_proxy(cfg, con, row, path):
    """Record what a proxy is (LUT baked in or not, which LUT, size) as a fact, so nothing has to guess it
    from the picture later (render's preview path and the export reframe read this)."""
    con.executescript(PROXY_INFO_SCHEMA)
    lut = proxy_lut(cfg, row)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    con.execute("INSERT OR REPLACE INTO proxy_info VALUES (?,?,?,?,?,?,?)",
                (row["id"], int(bool(lut)), lut_sha(lut) if lut else None, int(cfg.proxy_height),
                 media.proxy_bitrate(cfg), size, time.time()))


def proxy_graded(con, cid):
    """True / False when the proxy's grade was recorded at creation, None when unknown (older proxies)."""
    try:
        r = con.execute("SELECT graded FROM proxy_info WHERE clip_id=?", (cid,)).fetchone()
    except Exception:  # noqa: BLE001 - table not there yet
        return None
    return None if r is None else bool(r["graded"] if hasattr(r, "keys") else r[0])


def _make_proxy(cfg, row, enc) -> str:
    out = cfg.p("proxies", f"{row['id']}.mp4")
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    gov = governor.for_cfg(cfg)
    with gov.slot("heavy", label=f"proxy {row['id']}"):
        gov.wait_for_disk(out.parent)
        return _encode_proxy(cfg, row, enc, out)


def _encode_proxy(cfg, row, enc, out) -> str:
    tmp = out.with_suffix(".tmp.mp4")
    timeout = max(600, (row["duration"] or 0) * 10)
    log = cfg.p("proxies", "ffmpeg.log")
    try:
        media.ffmpeg(cfg, _proxy_args(cfg, row, (row.get("_src") if isinstance(row, dict) else None) or row["path"], tmp, enc), log=log, timeout=timeout)
    except media.GovernorKilled:
        tmp.unlink(missing_ok=True)
        raise
    except Exception:
        if "libx264" in enc:
            raise
        # hardware encoders can refuse odd sizes or run out of sessions: fall back to x264
        media.ffmpeg(cfg, _proxy_args(cfg, row, (row.get("_src") if isinstance(row, dict) else None) or row["path"], tmp,
                                      ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.proxy_crf)]),
                     log=log, timeout=timeout)
    os.replace(tmp, out)
    return str(out)


def make_proxies(cfg, con, limit=None, max_jobs=None, ids=None):
    """Small H.264 copies (cfg.proxy_height tall, <=25 fps, 1 s GOP, mono AAC) for analysis + preview."""
    for stale in cfg.p("proxies").glob("*.tmp.mp4"):
        stale.unlink(missing_ok=True)
    rows = con.execute("SELECT * FROM clips WHERE status='probed' AND proxy IS NULL ORDER BY rel").fetchall()
    if ids is not None:
        rows = [r for r in rows if r["id"] in ids]
    if limit:
        rows = rows[:limit]
    if not rows:
        _log("proxies: nothing to do")
        return
    rows = [with_camera_lut(cfg, con, r) for r in rows]
    enc = media.hw_encoder(cfg)
    t = time.time()
    total_s = sum(r["duration"] or 0 for r in rows)
    ok = err = 0
    with ThreadPoolExecutor(_workers(cfg, max_jobs)) as ex:
        futs = {ex.submit(_make_proxy, cfg, r, enc): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            try:
                p = f.result()
                con.execute("UPDATE clips SET proxy=?, status='proxied', error=NULL, updated=? WHERE id=?",
                            (p, time.time(), r["id"]))
                record_proxy(cfg, con, r, p)
                ok += 1
            except media.GovernorKilled as e:          # not the clip's fault: retried next run
                _log(f"  ! {r['rel']}: {e}")
                err += 1
            except Exception as e:  # noqa: BLE001
                con.execute("UPDATE clips SET status='error', error=?, updated=? WHERE id=?",
                            (f"proxy: {str(e)[-300:]}", time.time(), r["id"]))
                err += 1
            con.commit()
            if i % 20 == 0:
                _log(f"  proxies {i}/{len(rows)}")
    dt = time.time() - t
    _log(f"proxies: {ok} ok · {err} errors · {dt:.1f}s for {total_s / 60:.1f} min of footage "
         f"({total_s / max(dt, 1e-6):.0f}x realtime)")


# -- contact sheets --------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def load_font(size: int):
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def sheet_times(duration: float, every: float, max_tiles: int) -> list:
    """Tile timestamps: one per `every` seconds, at most `max_tiles`, spread over the whole clip."""
    if duration <= 0:
        return [0.0]
    n = max(1, min(int(max_tiles), math.ceil(duration / max(every, 0.1))))
    return [round((i + 0.5) * duration / n, 2) for i in range(n)]


def _read_frames(cmd, w, h, n) -> list:
    g = governor.active()
    if g is not None:                      # niced + footprint-watched, like media.run
        r = g.run(cmd, kind=None, timeout=600, text=False)
        if r.killed:
            raise media.GovernorKilled(f"stopped by resource governor: {r.killed}")
    else:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode:
        raise media.FFError(r.stderr.decode(errors="ignore")[-500:])
    fs = w * h * 3
    buf = r.stdout
    return [np.frombuffer(buf[i * fs:(i + 1) * fs], np.uint8).reshape(h, w, 3) for i in range(min(n, len(buf) // fs))]


def extract_tiles(cfg, proxy: str, times: list, duration: float, pw: int, ph: int, width=TILE_W) -> list:
    """RGB frames (width wide) at `times` from a proxy. Short clips: one decode pass; long: one seek per tile."""
    th = max(2, int(round(width * ph / max(pw, 1) / 2)) * 2)
    base = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    out_args = ["-vf", None, "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    n = len(times)
    if duration <= 120 and n > 1:
        step = duration / n
        args = base + ["-ss", f"{times[0]:.3f}", "-i", proxy] + out_args
        args[args.index(None)] = f"fps={1 / step:.6f}:round=near,scale={width}:{th}"
        frames = _read_frames(args, width, th, n)
        if len(frames) >= n:
            return frames[:n]
    frames = []
    for t in times:
        args = base + ["-ss", f"{max(0.0, min(t, duration - 0.05)):.3f}", "-i", proxy, "-frames:v", "1"] + out_args
        args[args.index(None)] = f"scale={width}:{th}"
        got = _read_frames(args, width, th, 1)
        frames.append(got[0] if got else np.zeros((th, width, 3), np.uint8))
    return frames


def render_sheet(frames: list, times: list, header: list, out: str, cols=SHEET_COLS):
    from PIL import Image, ImageDraw
    th, tw = frames[0].shape[:2]
    cols = min(cols, len(frames))
    rows = math.ceil(len(frames) / cols)
    hdr_font, ts_font = load_font(22), load_font(18)
    hdr_h = 14 + 30 * len(header)
    W = max(cols * tw, 960)
    img = Image.new("RGB", (W, hdr_h + rows * th), (16, 16, 16))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(header):
        d.text((10, 8 + 30 * i), line, fill=(235, 235, 235) if i == 0 else (170, 170, 170), font=hdr_font)
    for i, (fr, t) in enumerate(zip(frames, times)):
        x, y = (i % cols) * tw, hdr_h + (i // cols) * th
        img.paste(Image.fromarray(fr), (x, y))
        label = f"{t:.1f}s"
        bx = d.textbbox((0, 0), label, font=ts_font)
        d.rectangle([x + 4, y + 4, x + 14 + bx[2], y + 10 + bx[3]], fill=(0, 0, 0))
        d.text((x + 9, y + 5), label, fill=(255, 230, 80), font=ts_font)
    tmp = out + ".tmp.jpg"
    img.save(tmp, "JPEG", quality=85)
    os.replace(tmp, out)


def _make_sheet(cfg, row) -> str:
    out = str(cfg.p("sheets", f"{row['id']}.jpg"))
    if os.path.exists(out):
        return out
    gov = governor.for_cfg(cfg)
    with gov.slot("heavy", label=f"sheet {row['id']}"):
        gov.wait_for_disk(os.path.dirname(out))
        return _build_sheet(cfg, row, out)


def _build_sheet(cfg, row, out) -> str:
    dur = float(row["duration"] or 0)
    times = sheet_times(dur, cfg.sheet_every_s, cfg.sheet_max_tiles)
    dw, dh = display_size(row)
    info = media.probe(cfg, row["proxy"])                 # proxy dims = what we decode
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    frames = extract_tiles(cfg, row["proxy"], times, dur, int(v["width"]), int(v["height"]))
    flags = [f"{dw}x{dh}", f"{row['fps'] or 0:g} fps", row["codec"] or "?", row["pix_fmt"] or ""]
    if row["is_log"]:
        flags.append("LOG")
    if not row["has_audio"]:
        flags.append("no audio")
    header = [f"{row['rel']}", f"{row['id']} · {dur:.1f}s · " + " · ".join(x for x in flags if x)]
    render_sheet(frames, times, header, out)
    return out


def make_sheets(cfg, con, limit=None, max_jobs=None, ids=None):
    """One contact sheet per proxied clip at <work>/sheets/<id>.jpg (what the operator looks at)."""
    rows = con.execute("SELECT * FROM clips WHERE proxy IS NOT NULL AND sheet IS NULL "
                       "AND status IN ('proxied','analyzed') ORDER BY rel").fetchall()
    if ids is not None:
        rows = [r for r in rows if r["id"] in ids]
    if limit:
        rows = rows[:limit]
    if not rows:
        _log("sheets: nothing to do")
        return
    t = time.time()
    ok = err = 0
    with ThreadPoolExecutor(_workers(cfg, max_jobs)) as ex:
        futs = {ex.submit(_make_sheet, cfg, r): r for r in rows}
        for f in as_completed(futs):
            r = futs[f]
            try:
                con.execute("UPDATE clips SET sheet=?, updated=? WHERE id=?", (f.result(), time.time(), r["id"]))
                ok += 1
            except Exception as e:  # noqa: BLE001 - a missing sheet is not fatal for the clip
                con.execute("UPDATE clips SET error=? WHERE id=?", (f"sheet: {str(e)[-300:]}", r["id"]))
                err += 1
            con.commit()
    _log(f"sheets: {ok} ok · {err} errors · {time.time() - t:.1f}s")
