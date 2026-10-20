"""Multicam and dual-system audio sync: find clips recorded at the same time on different cameras and audio
recorders, line them up by their sound, and let plans cut between the angles.

    cutroom sync                         detect + align everything new (resumable; cached per pair)
    cutroom sync --clips A B C           align just these clips against each other (no timestamps needed)
    cutroom sync list | show GROUP       the synced groups, their angles, offsets, drift and confidence
    cutroom sync devices                 cameras / recorders found (make, model, serial, the name you gave them)
    cutroom sync name DEVICE "Cam A wide" [--kind camera|recorder|lav] [--code camA]
    cutroom sync rename GROUP "Ceremony"
    cutroom sync map GROUP T [--from ANGLE] [--to ANGLE]      a moment on one angle -> the same moment on another
    cutroom sync switch PLAN SEC.SEG ANGLE [--audio CODE]     change the angle of a multicam segment in a plan

How it works
  1. media metadata: make / model / serial (QuickTime tags, Sony/Canon XML sidecars), start timecode
     (ffprobe `timecode` tags, BWF time_reference), capture time (creation_time, BWF origination date/time,
     sidecar CreationDate, else file mtime - duration). Audio-only files (WAV/BWF/MP3/M4A from recorders)
     under the library folders are registered as clips with status 'audio' so plans can use them as sound.
  2. candidate pairs: clips on different devices whose capture spans overlap within `sync_window_s`
     (default 5 min of clock slack); a second pass tries whole-hour clock offsets (a device left on UTC or
     home time) for devices that matched nothing. Timecode spans that overlap are candidates too.
  3. alignment: a 100 Hz onset envelope (log-spectral flux, 8 kHz mono) per clip, cached on disk; a
     normalised FFT cross-correlation over every lag gives the coarse offset, its peak value (corr) and
     peak / best rival outside +-0.5 s (ratio). Accepted when corr >= sync_min_conf and ratio >=
     sync_min_ratio. Then 30 s envelope windows across the overlap give local offsets (drift), and 2 s
     GCC-PHAT windows on the 8 kHz waveform refine each one to well under a millisecond; a robust line
     fit gives offset + drift (ppm). Pairs are cached in sync_pairs (re-runs only do new clips).
  4. groups: accepted pairs form a graph; each connected component is a group. Offsets are composed along
     the maximum-confidence spanning tree from the reference member (the longest video clip); loops that
     close through other measured pairs give `loop_err_ms` (should be < 1 frame).

Group time: seconds on the reference member's clock, 0 = the earliest member's first sample. A member's
own time t maps to group time g = offset + t * (1 + drift_ppm * 1e-6).

Plans: a talk/broll segment can carry {"multicam": {"group": 3, "angle": "camB", "audio": "auto"}}. With
"clip" set, in/out are that clip's seconds (mapped through the group if it is another angle's clip); without
it they are group seconds. The renderer picks the angle's clip for picture and the best sound (external
recorder > lav > camera) for audio, mapped through the measured offset and drift. See docs/NLE_AND_MULTICAM.md.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import db, media
from .config import AUDIO_EXTS, VIDEO_EXTS

SR = 8000                     # analysis sample rate
HOP = 80                      # 10 ms envelope hop
ENV_RATE = SR / HOP           # 100 Hz
NFFT = 256
FINE_WIN_S = 2.0              # GCC-PHAT window
FINE_MARGIN_S = 0.06          # search +-60 ms around the envelope estimate
LOCAL_WIN_S = 30.0            # local-offset windows for drift
LOCAL_SEARCH_S = 3.0          # +-3 s around the global lag (1 h at 800 ppm)
MAX_FINE_WINDOWS = 12
RIVAL_EXCLUDE_S = 0.5
KIND_RANK = {"recorder": 0, "lav": 1, "camera": 2}
ALGO_VERSION = 4              # bump -> cached pairs are re-measured (4: clap anchors)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, code TEXT, make TEXT, model TEXT, serial TEXT,
  kind TEXT DEFAULT 'camera',    -- camera | recorder | lav
  name TEXT,                     -- what the creator calls it ("Cam A wide", "Zoom H6")
  tc_sync INTEGER DEFAULT 0,     -- 1 = jam-synced time-of-day timecode can be trusted when audio can't decide
  updated REAL
);
CREATE TABLE IF NOT EXISTS clip_media (
  clip_id TEXT PRIMARY KEY, device_id INTEGER, make TEXT, model TEXT, serial TEXT,
  timecode TEXT, tc_rate REAL, tc_seconds REAL, start_epoch REAL, start_src TEXT,
  audio_only INTEGER DEFAULT 0, sample_rate INTEGER, reel TEXT, probed REAL
);
CREATE TABLE IF NOT EXISTS sync_pairs (
  a TEXT, b TEXT, ok INTEGER, lag REAL, drift_ppm REAL, corr REAL, ratio REAL, overlap REAL,
  fine_windows INTEGER, rms_ms REAL, method TEXT, why TEXT, algo INTEGER, updated REAL,
  PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS sync_groups (
  id INTEGER PRIMARY KEY, name TEXT, ref_clip TEXT, day TEXT, start_epoch REAL, duration REAL,
  loop_err_ms REAL, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS sync_members (
  group_id INTEGER, clip_id TEXT, angle TEXT, offset REAL, drift_ppm REAL, confidence REAL,
  method TEXT, kind TEXT, video INTEGER, audio_rank INTEGER, duration REAL,
  PRIMARY KEY (group_id, clip_id)
);
CREATE INDEX IF NOT EXISTS sync_members_clip ON sync_members(clip_id);
CREATE TABLE IF NOT EXISTS device_clock (
  device_id INTEGER, day TEXT, offset_s REAL, n INTEGER, spread_s REAL, ref_device INTEGER, updated REAL,
  PRIMARY KEY (device_id, day)   -- the device's clock minus the reference device's clock (seconds)
);
"""


def ensure_schema(con):
    con.executescript(SCHEMA)


def _log(echo, msg):
    if echo:
        echo(msg)


# ============================================================================================ metadata
def _parse_time(s):
    """ISO-ish timestamp -> epoch seconds (naive = local time), or None."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    s = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", s)            # EXIF-style dates
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    for cand in (s, s.replace(" ", "T")):
        try:
            d = dt.datetime.fromisoformat(cand)
            return d.timestamp()
        except ValueError:
            continue
    return None


def parse_timecode(tc, rate):
    """'HH:MM:SS:FF' (or ';' drop-frame) -> seconds."""
    m = re.match(r"^(\d{1,2})[:;.](\d{2})[:;.](\d{2})[:;.](\d{2,3})$", str(tc or "").strip())
    if not m:
        return None
    h, mi, s, f = (int(x) for x in m.groups())
    nominal = int(round(rate or 25)) or 25
    frames = ((h * 60 + mi) * 60 + s) * nominal + f
    if ";" in str(tc) and nominal in (30, 60):                          # drop-frame: 2 (4) per minute except tens
        dropn = 2 if nominal == 30 else 4
        mins = h * 60 + mi
        frames -= dropn * (mins - mins // 10)
    return frames / (rate or nominal)


def _xml_attrs(text):
    out = {}
    for key, pat in (("make", r'manufacturer="([^"]+)"'), ("model", r'modelName="([^"]+)"'),
                     ("serial", r'serialNo="([^"]+)"'), ("created", r'<CreationDate value="([^"]+)"'),
                     ("timecode", r'<LtcChange [^>]*frameCount="0"[^>]*value="(\d{8})"')):
        m = re.search(pat, text)
        if m:
            out[key] = m.group(1)
    if out.get("timecode"):                                             # Sony stores FFSSMMHH as BCD
        v = out["timecode"]
        try:
            out["timecode"] = f"{v[6:8]}:{v[4:6]}:{v[2:4]}:{v[0:2]}"
        except Exception:  # noqa: BLE001
            out.pop("timecode")
    return out


def media_meta(cfg, path, info=None) -> dict:
    """make / model / serial / timecode / capture start from ffprobe tags and camera XML sidecars."""
    from .ingest import _sidecars
    info = info or media.probe(cfg, path)
    fmt = info.get("format", {}) or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for s in info.get("streams", []) or []:
        for k, v in (s.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    out = {"make": tags.get("com.apple.quicktime.make") or tags.get("make") or tags.get("manufacturer"),
           "model": tags.get("com.apple.quicktime.model") or tags.get("model") or tags.get("originator")
           or tags.get("encoded_by"),
           "serial": tags.get("com.apple.quicktime.camera.identifier") or tags.get("serial")
           or tags.get("serial_number") or tags.get("com.apple.quicktime.serial"),
           "timecode": tags.get("timecode"), "audio_only": int(v is None and a is not None),
           "sample_rate": int(a.get("sample_rate") or 0) if a else 0,
           "duration": float(fmt.get("duration") or 0)}
    fps = None
    if v:
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else None
    out["tc_rate"] = fps or 25.0
    created = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    src = "creation_time" if created else None
    if created and re.match(r"^\d{1,2}[:.-]\d{2}[:.-]\d{2}$", str(created).strip()) and tags.get("date"):
        # ffprobe maps BWF origination_date/time to `date` + a time-only `creation_time` (local wall clock)
        created, src = f"{str(tags['date']).replace(':', '-')}T{str(created).strip().replace('-', ':').replace('.', ':')}", "bwf"
    # BWF (Zoom, Tascam, Sound Devices): origination date/time + samples since midnight
    od, ot = tags.get("origination_date"), tags.get("origination_time")
    if od and ot:
        created, src = f"{od.replace(':', '-')}T{ot.replace('-', ':')}", "bwf"
    tref = tags.get("time_reference")
    if str(path).lower().endswith((".wav", ".bwf", ".w64")):
        from . import bwf
        bm = bwf.read(path)
        if bm.get("ok"):
            out["bwf"] = {"format": bm.get("format"), "bits": bm.get("bits"), "time_reference": bm.get("time_reference"),
                          "time_reference_src": bm.get("time_reference_src"), "frames": bm.get("frames"),
                          "family_uid": (bm.get("ixml") or {}).get("family_uid")}
            if bm.get("time_reference") is not None:
                tref = str(bm["time_reference"])
                ixr = (bm.get("ixml") or {}).get("timecode_rate")
                if ixr:
                    try:                                       # iXML TIMECODE_RATE: "25/1", "30000/1001"
                        from fractions import Fraction
                        out["tc_rate"] = float(Fraction(str(ixr).strip()))
                    except (ValueError, ZeroDivisionError):
                        pass
            mk = bwf.maker(bm)
            if mk and not out.get("make"):
                out["make"] = mk.split()[0]
                out["model"] = out.get("model") or mk
            be = bm.get("bext") or {}
            if be.get("origination_date") and be.get("origination_time") and not (od and ot):
                od, ot = be["origination_date"], be["origination_time"]
                created, src = f"{od.replace(':', '-')}T{ot.replace('-', ':')}", "bwf"
    if tref and out["sample_rate"]:
        try:
            secs = int(tref) / out["sample_rate"]
            out["tc_seconds"] = secs
            if not out["timecode"]:
                from .bwf import tc_string
                out["tc_rate"] = out["tc_rate"] if out.get("bwf") and out["tc_rate"] else 25.0
                out["timecode"] = tc_string(int(tref), out["sample_rate"], out["tc_rate"])
        except ValueError:
            pass
    for sc in _sidecars(str(path)):
        if not sc.lower().endswith(".xml"):
            continue
        try:
            if os.path.getsize(sc) > 512 * 1024:
                continue
            x = _xml_attrs(Path(sc).read_text(errors="ignore"))
        except OSError:
            continue
        for k in ("make", "model", "serial", "timecode"):
            if x.get(k) and not out.get(k):
                out[k] = x[k]
        if x.get("created") and src != "bwf":
            created, src = x["created"], "xml"
    ep = _parse_time(created)
    if ep is None:
        try:
            ep = os.stat(path).st_mtime - out["duration"]
            src = "mtime"
        except OSError:
            src = None
    out["start_epoch"], out["start_src"] = ep, src
    if out.get("timecode") and out.get("tc_seconds") is None:
        out["tc_seconds"] = parse_timecode(out["timecode"], out["tc_rate"])
    for k in ("make", "model", "serial"):
        if out.get(k):
            out[k] = str(out[k]).strip()[:80]
    return out


def _folder_hint(cfg, rel):
    rx = re.compile(getattr(cfg, "sync_device_hint", "") or r"$^")
    for part in reversed(Path(rel or "").parts[:-1]):
        if rx.search(part):
            return part
    return ""


def device_key(cfg, meta, rel) -> str:
    make, model = (meta.get("make") or "").lower(), (meta.get("model") or "").lower()
    if meta.get("serial"):
        return f"{make}|{model}|sn:{str(meta['serial']).lower()}"
    hint = _folder_hint(cfg, rel)
    base = f"{make}|{model}" if (make or model) else ("audio" if meta.get("audio_only") else "unknown")
    return f"{base}|dir:{hint.lower()}" if hint else base


def _next_code(con, kind):
    pre = "rec" if kind == "recorder" else "cam"
    have = {r[0] for r in con.execute("SELECT code FROM devices")}
    if pre == "cam":
        for i in range(26 * 27):
            c = "cam" + (chr(65 + i) if i < 26 else chr(64 + i // 26) + chr(65 + i % 26))
            if c not in have:
                return c
    k = 1
    while f"{pre}{k}" in have:
        k += 1
    return f"{pre}{k}"


def _device_id(cfg, con, meta, rel) -> int:
    key = device_key(cfg, meta, rel)
    r = con.execute("SELECT id FROM devices WHERE key=?", (key,)).fetchone()
    if r:
        return r[0]
    kind = "recorder" if meta.get("audio_only") else "camera"
    code = _next_code(con, kind)
    label = meta.get("model") or (_folder_hint(cfg, rel) or ("audio recorder" if kind == "recorder" else "camera"))
    name = (f"Cam {code[3:]}" if kind == "camera" else f"Recorder {code[3:]}") + f" ({label})"
    cur = con.execute("INSERT INTO devices (key, code, make, model, serial, kind, name, updated) VALUES (?,?,?,?,?,?,?,?)",
                      (key, code, meta.get("make"), meta.get("model"), meta.get("serial"), kind, name, time.time()))
    return cur.lastrowid


def register_audio(cfg, con, echo=print) -> list:
    """Register audio-only recordings under the library folders (not the music folders) as clips with status
    'audio' (no proxy / sheet / analysis; transcription does pick them up). Returns new clip ids."""
    from .ingest import MIN_SIZE, _iter_files
    music = [os.path.realpath(m) for m in (cfg.music_roots or [])]
    known = {r["path"]: (r["size"], r["mtime"]) for r in con.execute("SELECT path, size, mtime FROM clips")}
    new = []
    for root in cfg.library_roots:
        rr = os.path.realpath(root)
        if not os.path.isdir(rr):
            continue
        for path, size, mtime in _iter_files(rr):
            if os.path.splitext(path)[1].lower() not in AUDIO_EXTS or size < MIN_SIZE:
                continue
            if any(path == m or path.startswith(m.rstrip(os.sep) + os.sep) for m in music):
                continue
            prev = known.get(path)
            if prev and prev[0] == size and abs((prev[1] or 0) - mtime) < 1e-3:
                continue
            try:
                info = media.probe(cfg, path)
            except Exception as e:  # noqa: BLE001
                _log(echo, f"  ! audio probe failed {path}: {e}")
                continue
            s = media.summarize_probe(info)
            if not s.get("has_audio") or s.get("width"):
                continue
            cid = db.clip_id(path)
            db.upsert(con, "clips", {"id": cid, "path": path, "rel": os.path.relpath(path, rr), "size": size,
                                     "mtime": mtime, "duration": s["duration"], "fps": 0.0, "width": 0, "height": 0,
                                     "codec": next((x.get("codec_name") for x in info.get("streams", [])
                                                    if x.get("codec_type") == "audio"), None),
                                     "has_audio": 1, "audio_channels": s.get("audio_channels"),
                                     "shot_at": s.get("shot_at"), "status": "audio", "error": None,
                                     "updated": time.time()})
            new.append(cid)
    con.commit()
    if new:
        _log(echo, f"sync: registered {len(new)} audio recording(s)")
    return new


def probe_media(cfg, con, ids=None, force=False, echo=print) -> int:
    """Fill clip_media (+ devices) for clips with audio. Returns the number probed."""
    ensure_schema(con)
    rows = con.execute("SELECT id, path, rel, duration FROM clips WHERE has_audio=1 AND COALESCE(duration,0)>0 "
                       "AND status NOT IN ('new','error') ORDER BY rel, path").fetchall()
    if ids is not None:
        ids = set(ids)
        rows = [r for r in rows if r["id"] in ids]
    done = set() if force else {r[0] for r in con.execute("SELECT clip_id FROM clip_media")}
    n = 0
    for r in rows:
        if r["id"] in done:
            continue
        try:
            m = media_meta(cfg, r["path"])
        except Exception as e:  # noqa: BLE001
            _log(echo, f"  ! sync probe {r['rel']}: {e}")
            continue
        if not m.get("audio_only"):
            try:
                from . import cameras as CAM
                cam = CAM.get(con, r["id"])
            except Exception:  # noqa: BLE001
                cam = None
            if cam and cam.get("family") not in (None, "other") and float(cam.get("confidence") or 0) >= 0.6:
                m["make"] = m.get("make") or cam.get("make")
                m["model"] = m.get("model") or cam.get("model")
                m["serial"] = m.get("serial") or cam.get("serial")
        did = _device_id(cfg, con, m, r["rel"])
        db.upsert(con, "clip_media", {"clip_id": r["id"], "device_id": did, "make": m.get("make"), "model": m.get("model"),
                                      "serial": m.get("serial"), "timecode": m.get("timecode"), "tc_rate": m.get("tc_rate"),
                                      "tc_seconds": m.get("tc_seconds"), "start_epoch": m.get("start_epoch"),
                                      "start_src": m.get("start_src"), "audio_only": m.get("audio_only"),
                                      "sample_rate": m.get("sample_rate"), "reel": _reel(r["rel"]), "probed": time.time()},
                  key=("clip_id",))
        n += 1
    con.commit()
    return n


def _reel(rel):
    return re.sub(r"[^A-Za-z0-9_]", "_", Path(rel or "").stem)[:32] or "REEL"


# ============================================================================================ signal
def onset_envelope(x) -> np.ndarray:
    """100 Hz onset strength (positive log-spectral flux, 60 Hz-3.5 kHz) of 8 kHz mono audio, high-passed."""
    x = np.asarray(x, np.float32)
    if len(x) < NFFT + HOP:
        return np.zeros(0, np.float32)
    from numpy.lib.stride_tricks import sliding_window_view
    frames = sliding_window_view(x, NFFT)[::HOP]
    win = np.hanning(NFFT).astype(np.float32)
    lo, hi = 2, int(3500 / (SR / NFFT))
    out = np.empty(len(frames), np.float32)
    prev = None
    CH = 16384
    for s in range(0, len(frames), CH):
        mag = np.abs(np.fft.rfft(frames[s:s + CH] * win, axis=1))[:, lo:hi]
        lm = np.log1p(1000.0 * mag).astype(np.float32)
        d = np.diff(lm, axis=0, prepend=(lm[:1] if prev is None else prev[None, :]))
        out[s:s + len(lm)] = np.maximum(d, 0).sum(axis=1)
        prev = lm[-1]
    k = 51                                                   # 0.5 s moving average removed: transients only
    pad = np.pad(out, (k // 2, k // 2), mode="edge")
    ma = np.convolve(pad, np.ones(k, np.float32) / k, mode="valid")
    return (out - ma).astype(np.float32)


def _nxcorr_full(x, y, min_overlap):
    """Normalised cross-correlation r(L) of x[i] with y[i-L] for every lag L (-(m-1)..n-1), with the
    overlap count. Lags with fewer than min_overlap overlapping samples get r = -1."""
    n, m = len(x), len(y)
    N = 1 << int(math.ceil(math.log2(n + m)))
    X = np.fft.rfft(x.astype(np.float64), N)
    Y = np.fft.rfft(y.astype(np.float64), N)
    c = np.fft.irfft(X * np.conj(Y), N)
    L = np.arange(-(m - 1), n)
    sxy = c[L % N]
    cx = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    cxx = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    cy = np.concatenate([[0.0], np.cumsum(y, dtype=np.float64)])
    cyy = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
    i0, i1 = np.maximum(0, L), np.minimum(n, m + L)
    j0, j1 = np.maximum(-L, 0), np.minimum(n - L, m)
    cnt = (i1 - i0).astype(np.float64)
    sx, sxx = cx[i1] - cx[i0], cxx[i1] - cxx[i0]
    sy, syy = cy[j1] - cy[j0], cyy[j1] - cyy[j0]
    with np.errstate(divide="ignore", invalid="ignore"):
        num = sxy - sx * sy / cnt
        den = np.sqrt(np.maximum(sxx - sx * sx / cnt, 0) * np.maximum(syy - sy * sy / cnt, 0))
        r = np.where((cnt >= min_overlap) & (den > 1e-12), num / den, -1.0)
    return L, r, cnt


def _parabolic(r, k):
    if 0 < k < len(r) - 1:
        a, b, c = r[k - 1], r[k], r[k + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-12:
            return float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))
    return 0.0


def coarse_align(ex, ey, min_overlap_s=2.0):
    """Global offset of y against x from their envelopes: {lag (s, y starts at x-time lag), corr, ratio, overlap}."""
    if len(ex) < 50 or len(ey) < 50:
        return None
    mo = int(min(max(min_overlap_s, 0.0) * ENV_RATE, 0.9 * min(len(ex), len(ey))))
    L, r, cnt = _nxcorr_full(ex, ey, max(mo, 30))
    k = int(np.argmax(r))
    if r[k] <= -1:
        return None
    ex_ = int(RIVAL_EXCLUDE_S * ENV_RATE)
    rr = r.copy()
    rr[max(0, k - ex_):k + ex_ + 1] = -1
    rival = float(rr.max()) if len(rr) else -1.0
    lag = (L[k] + _parabolic(r, k)) / ENV_RATE
    return {"lag": float(lag), "corr": float(r[k]), "rival": rival,
            "ratio": float(r[k] / rival) if rival > 1e-6 else 99.0, "overlap": float(cnt[k] / ENV_RATE)}


def local_lags(ex, ey, lag, win_s=LOCAL_WIN_S, search_s=LOCAL_SEARCH_S):
    """Local offsets across the overlap: [(x_time_centre, lag_s, r)]. Windows step by half a window."""
    W, S = int(win_s * ENV_RATE), int(search_s * ENV_RATE)
    L0 = int(round(lag * ENV_RATE))
    n, m = len(ex), len(ey)
    a0, a1 = max(0, L0), min(n, m + L0)                     # overlap in x indices
    if a1 - a0 < 2 * ENV_RATE:
        return []
    W = min(W, a1 - a0)
    out = []
    step = max(W // 2, 1)
    for s in range(a0, a1 - W + 1, step):
        xw = ex[s:s + W].astype(np.float64)
        xw = xw - xw.mean()
        nx = np.sqrt((xw * xw).sum())
        if nx < 1e-9:
            continue
        best = (-2.0, None)
        rs = []
        for d in range(L0 - S, L0 + S + 1):
            j = s - d
            if j < 0 or j + W > m:
                rs.append(-1.0)
                continue
            yw = ey[j:j + W].astype(np.float64)
            yw = yw - yw.mean()
            ny = np.sqrt((yw * yw).sum())
            v = float((xw * yw).sum() / (nx * ny)) if ny > 1e-9 else -1.0
            rs.append(v)
            if v > best[0]:
                best = (v, d)
        if best[1] is None:
            continue
        rs = np.asarray(rs)
        k = best[1] - (L0 - S)
        out.append(((s + W / 2) / ENV_RATE, (best[1] + _parabolic(rs, k)) / ENV_RATE, best[0]))
    return out


def gcc_phat_lag(xs, ys, margin_n):
    """Shift s (samples, 0..2*margin) maximising sum x[i]*y[i+s] with PHAT weighting; (s_float, sharpness)."""
    n = len(xs) + len(ys)
    N = 1 << int(math.ceil(math.log2(n)))
    X = np.fft.rfft(xs, N)
    Y = np.fft.rfft(ys, N)
    R = np.conj(X) * Y
    R /= np.maximum(np.abs(R), 1e-12)
    c = np.fft.irfft(R, N)[: 2 * margin_n + 1]
    k = int(np.argmax(c))
    sharp = float(c[k] / (np.median(np.abs(c)) + 1e-12))
    return k + _parabolic(c, k), sharp


def _robust_line(ts, ys, w=None):
    ts, ys = np.asarray(ts, float), np.asarray(ys, float)
    keep = np.ones(len(ts), bool)
    a, b = float(np.median(ys)), 0.0
    for _ in range(4):
        if keep.sum() >= 3 and np.ptp(ts[keep]) > 1.0:
            b, a = np.polyfit(ts[keep], ys[keep], 1)
        elif keep.sum():
            a, b = float(np.median(ys[keep])), 0.0
        res = ys - (a + b * ts)
        mad = float(np.median(np.abs(res[keep]))) if keep.sum() else 0.0
        new = np.abs(res) <= max(3.0 * 1.4826 * mad, 0.0015)
        if new.sum() < max(1, len(ts) // 3) or (new == keep).all():
            break
        keep = new
    res = ys - (a + b * ts)
    rms = float(np.sqrt(np.mean(res[keep] ** 2))) if keep.sum() else 0.0
    return float(a), float(b), rms, int(keep.sum())


# ============================================================================================ claps
CLAP_RISE_DB = 18.0           # a clap/slate: energy jumps this much over the preceding 100 ms ...
CLAP_DECAY_DB = 10.0          # ... and falls this much again within 150 ms (a hit, not a word or music onset)
CLAP_HF_SHARE = 0.25          # ... with broadband energy (share above 1 kHz)
CLAP_AGREE_S = 0.025          # two recordings' claps agree with an alignment within this


def detect_claps(x, sr=SR, max_n=40) -> list:
    """Sharp, broadband, short transients (hand clap, slate, clapper): [(t_onset_s, strength_db)], strongest first.
    The onset is the first sample whose |x| reaches half the hit's peak (sample-precise at `sr`)."""
    x = np.asarray(x, np.float32)
    hop = max(1, int(sr * 0.002))                                  # 2 ms energy frames
    n = len(x) // hop
    pre_w, post_w, tail0 = int(0.1 / 0.002), int(0.15 / 0.002), int(0.12 / 0.002)
    if n < pre_w + post_w + 10:
        return []
    fr = x[: n * hop].reshape(n, hop).astype(np.float64)
    e = (fr ** 2).mean(1) + 1e-12
    d = np.diff(x, prepend=x[:1])[: n * hop].reshape(n, hop).astype(np.float64)
    hf = (d ** 2).mean(1) + 1e-12                                  # first difference ~ high-pass (white noise: 2x energy)
    edb = 10 * np.log10(e)
    cs = np.concatenate([[0.0], np.cumsum(e)])
    idx = np.arange(pre_w, n - post_w)
    pre = 10 * np.log10((cs[idx - 2] - cs[idx - pre_w]) / (pre_w - 2) + 1e-12)
    ct = np.concatenate([[0.0], np.cumsum(edb)])
    tail = (ct[idx + post_w] - ct[idx + tail0]) / (post_w - tail0)
    peak = (edb[idx] >= edb[idx - 1]) & (edb[idx] >= edb[idx + 1]) & (edb[idx] >= edb.max() - 60.0)
    ok = peak & (edb[idx] - pre >= CLAP_RISE_DB) & (edb[idx] - tail >= CLAP_DECAY_DB) & (hf[idx] / e[idx] >= 2.0 * CLAP_HF_SHARE)
    cand = sorted(zip(idx[ok], (edb[idx] - pre)[ok]), key=lambda c: -c[1])
    out = []
    for i, rise in cand:
        if any(abs(i * hop / sr - t) < 0.3 for t, _ in out):
            continue
        a0 = max(0, (int(i) - 3) * hop)
        seg = np.abs(x[a0:(int(i) + 2) * hop])
        if not len(seg) or seg.max() <= 0:
            continue
        k = int(np.argmax(seg >= 0.5 * seg.max()))
        out.append((float((a0 + k) / sr), round(float(rise), 1)))
        if len(out) >= max_n:
            break
    return out


def clap_anchor(ca, cb, lag_fn, tol=CLAP_AGREE_S) -> list:
    """Clap pairs that agree with an alignment: [(t_a, t_b, error_s)] where lag_fn(t_a) is the expected a-b lag."""
    out = []
    for ta, _ in ca:
        exp = lag_fn(ta)
        best = min(((abs(ta - tb - exp), tb) for tb, _ in cb), default=None)
        if best and best[0] <= tol:
            out.append((ta, best[1], ta - best[1] - exp))
    return out


# ============================================================================================ audio cache
class AudioCache:
    """Decoded 8 kHz mono audio, a few clips at a time (pairs are processed clip by clip)."""

    def __init__(self, cfg, size=3):
        self.cfg, self.size, self.d = cfg, size, OrderedDict()

    def get(self, row):
        cid = row["id"]
        if cid in self.d:
            self.d.move_to_end(cid)
            return self.d[cid]
        from .asr import load_audio
        x = load_audio(self.cfg, row["path"], sr=SR)
        self.d[cid] = x
        while len(self.d) > self.size:
            self.d.popitem(last=False)
        return x


def _env_path(cfg, row):
    st = os.stat(row["path"])
    h = hashlib.sha1(f"{row['path']}|{st.st_size}|{st.st_mtime}|{ALGO_VERSION}".encode()).hexdigest()[:16]
    return cfg.p("cache", "sync", f"{row['id']}_{h}.npy")


def envelope(cfg, row, audio_cache=None) -> np.ndarray:
    p = _env_path(cfg, row)
    if p.exists():
        try:
            return np.load(p)
        except (OSError, ValueError):
            pass
    x = (audio_cache or AudioCache(cfg, size=1)).get(row)
    e = onset_envelope(x)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp.npy")
    np.save(tmp, e)
    os.replace(tmp, p)
    return e


# ============================================================================================ alignment
def align_pair(cfg, ra, rb, audio_cache=None, min_conf=None, min_ratio=None) -> dict:
    """Align clip b against clip a. Result: {ok, lag, drift_ppm, corr, ratio, overlap, fine_windows, rms_ms,
    why}. lag(t) = lag + drift * t: the event at a-time t is at b-time t - lag(t)."""
    min_conf = float(min_conf if min_conf is not None else getattr(cfg, "sync_min_conf", 0.25))
    min_ratio = float(min_ratio if min_ratio is not None else getattr(cfg, "sync_min_ratio", 1.35))
    ac = audio_cache or AudioCache(cfg)
    ea, eb = envelope(cfg, ra, ac), envelope(cfg, rb, ac)
    shorter = min(len(ea), len(eb)) / ENV_RATE
    co = coarse_align(ea, eb, min_overlap_s=min(8.0, max(1.5, 0.5 * shorter)))
    base = {"ok": 0, "lag": None, "drift_ppm": 0.0, "corr": None, "ratio": None, "overlap": 0.0, "fine_windows": 0,
            "rms_ms": None, "method": "audio"}
    if not co:
        return {**base, "why": "too short or silent"}
    base.update(lag=co["lag"], corr=round(co["corr"], 4), ratio=round(min(co["ratio"], 99.0), 3),
                overlap=round(co["overlap"], 2))
    need = max(min_conf, 6.0 / math.sqrt(max(1.0, co["overlap"] * ENV_RATE)))     # above the noise floor of a
    claps_a = claps_b = None
    if co["corr"] < need or co["ratio"] < min_ratio:                                # max over ~all lags
        claps_a, claps_b = detect_claps(ac.get(ra)), detect_claps(ac.get(rb))
        rescue = _clap_rescue(ea, eb, claps_a, claps_b)
        if not rescue:
            return {**base, "why": f"low confidence (corr {co['corr']:.2f}, ratio {co['ratio']:.2f})"}
        co = {**co, "lag": rescue["lag"], "corr": rescue["corr"], "ratio": max(co["ratio"], min_ratio)}
        base.update(lag=co["lag"], corr=round(co["corr"], 4), method="clap")
    # local envelope offsets -> drift, then sample-level refinement on the waveform
    loc = [(t, l_) for t, l_, r in local_lags(ea, eb, co["lag"]) if r >= max(0.2, 0.5 * co["corr"])]
    if len(loc) >= 3:
        a0, b0, _, _ = _robust_line([t for t, _ in loc], [l_ for _, l_ in loc])
    else:
        a0, b0 = co["lag"], 0.0
    xa, xb = ac.get(ra), ac.get(rb)
    ov0, ov1 = max(0.0, co["lag"]), min(len(xa) / SR, len(xb) / SR + co["lag"])
    span = ov1 - ov0
    nwin = int(max(1, min(MAX_FINE_WINDOWS, span // (FINE_WIN_S * 2))))
    centres = [ov0 + span * (k + 0.5) / nwin for k in range(nwin)]
    M = int(FINE_MARGIN_S * SR)
    W = int(FINE_WIN_S * SR)
    ts, lags = [], []
    for c in centres:
        t0 = c - FINE_WIN_S / 2
        pred = a0 + b0 * c
        i0 = int(round(t0 * SR))
        j0 = int(round((t0 - pred) * SR)) - M
        if i0 < 0 or j0 < 0 or i0 + W > len(xa) or j0 + W + 2 * M > len(xb):
            continue
        xs, ys = xa[i0:i0 + W].astype(np.float64), xb[j0:j0 + W + 2 * M].astype(np.float64)
        if np.std(xs) < 1e-5 or np.std(ys) < 1e-5:
            continue
        s, sharp = gcc_phat_lag(xs, ys, M)
        if sharp < 8.0:
            continue
        lam = (i0 - j0 - s) / SR                                  # a-time minus b-time at this window
        if abs(lam - pred) > FINE_MARGIN_S * 0.95:
            continue
        ts.append(c)
        lags.append(lam)
    if lags:
        a, b, rms, k = _robust_line(ts, lags)
        if np.ptp(ts) < 20.0 or k < 3:                             # too short a span to measure drift
            b, a, rms, k = 0.0, float(np.median(lags)), float(np.std(lags)), len(lags)
    else:
        a, b, rms, k = a0, b0, None, 0
    drift = b if abs(b) * 1e6 > 2.0 else 0.0
    if drift == 0.0 and lags:
        a = float(np.median(np.asarray(lags) - 0.0))
    method = ("clap" if base.get("method") == "clap" else "audio") if k else \
        ("clap" if base.get("method") == "clap" else "audio-envelope")
    # clap / slate anchor: a sharp transient in both recordings at the aligned moment confirms the offset; when the
    # waveform refinement had nothing to work on, the clap pins it to the sample
    if claps_a is None:
        claps_a, claps_b = detect_claps(xa), detect_claps(xb)
    anch = clap_anchor(claps_a, claps_b, lambda t: a + drift * t)
    clap_info = ""
    if anch:
        errs = [e for _, _, e in anch]
        if not k:
            a = float(a + np.median(errs))
        if method == "audio":
            method = "audio+clap"
        clap_info = f"{len(anch)} clap(s) agree within {max(abs(e) for e in errs) * 1000:.1f} ms"
    return {**base, "ok": 1, "lag": round(float(a), 6), "drift_ppm": round(float(drift) * 1e6, 3),
            "fine_windows": int(k), "rms_ms": round(rms * 1000, 3) if rms is not None else None,
            "method": method, "why": clap_info, "claps": len(anch)}


def _clap_rescue(ea, eb, ca, cb, min_r=0.3, win_s=10.0):
    """When the envelopes alone can't decide: test every clap pair's lag; accept the one whose local envelope
    correlation (+-win_s around the clap) is >= min_r and clearly the best within +-0.5 s, preferring lags that
    several clap pairs agree on. {lag, corr} or None."""
    if not ca or not cb:
        return None
    pairs = [(ta, tb) for ta, _ in ca[:12] for tb, _ in cb[:12]]
    lags = [ta - tb for ta, tb in pairs]
    W = int(win_s * ENV_RATE)
    best = None
    for ta, tb in pairs:
        lag = ta - tb
        n_agree = sum(1 for l2 in lags if abs(l2 - lag) <= CLAP_AGREE_S)
        L0 = int(round(lag * ENV_RATE))
        c = int(ta * ENV_RATE)
        r_at = {}
        for d in range(L0 - 50, L0 + 51):
            i0, i1 = max(0, c - W, d), min(len(ea), c + W, len(eb) + d)
            if i1 - i0 < 200:
                continue
            xw, yw = ea[i0:i1].astype(np.float64), eb[i0 - d:i1 - d].astype(np.float64)
            xw, yw = xw - xw.mean(), yw - yw.mean()
            den = math.sqrt(float((xw * xw).sum() * (yw * yw).sum()))
            r_at[d] = float((xw * yw).sum() / den) if den > 1e-12 else -1.0
        if L0 not in r_at:
            continue
        r0 = max(r_at.get(L0 + q, -1.0) for q in (-1, 0, 1))
        rival = max((v for d, v in r_at.items() if abs(d - L0) > 10), default=-1.0)
        if r0 < min_r or r0 < 1.2 * rival:
            continue
        score = (n_agree, r0)
        if best is None or score > best[0]:
            best = (score, {"lag": float(lag), "corr": r0})
    return best[1] if best else None


# ============================================================================================ candidates
def _spans(con):
    rows = con.execute("""SELECT c.id, c.path, c.rel, c.duration, c.width, c.fps, c.shot_at, m.device_id,
                                 m.start_epoch, m.tc_seconds, m.audio_only, d.serial AS dserial
                          FROM clips c JOIN clip_media m ON m.clip_id = c.id LEFT JOIN devices d ON d.id = m.device_id
                          WHERE c.has_audio=1 AND COALESCE(c.duration,0) > 0 AND c.status NOT IN ('new','error')""").fetchall()
    return [dict(r) for r in rows]


def _same_body(a, b):
    return a["device_id"] == b["device_id"] and a.get("dserial")


def candidates(cfg, con, window_s=None, hour_shift=0, devices=None, corrected=False) -> list:
    """Pairs (id_a, id_b) whose capture spans overlap within window_s (with b's clock moved by hour_shift
    hours when given; `devices` limits shifted passes to those device pairs). corrected=True uses capture starts
    corrected by the measured per-device clock offsets (estimate_clocks)."""
    W = float(window_s if window_s is not None else getattr(cfg, "sync_window_s", 300.0))
    sp = [s for s in _spans(con) if s["start_epoch"] is not None]
    if corrected:
        offs = _clock_table(con)
        for s_ in sp:
            s_["start_epoch"] = s_["start_epoch"] - _offset_for(offs, s_["device_id"], s_["start_epoch"])
    ev = sorted(sp, key=lambda s: s["start_epoch"])
    out = set()
    for i, a in enumerate(ev):
        a0, a1 = a["start_epoch"], a["start_epoch"] + a["duration"]
        for b in ev if hour_shift else ev[i + 1:]:
            if b is a or _same_body(a, b):
                continue
            if hour_shift and devices is not None and (a["device_id"], b["device_id"]) not in devices:
                continue
            b0 = b["start_epoch"] + hour_shift * 3600.0
            if not hour_shift and b0 > a1 + W:
                break
            b1 = b0 + b["duration"]
            if b0 <= a1 + W and a0 <= b1 + W:
                out.add(tuple(sorted((a["id"], b["id"]))))
    # timecode-of-day spans
    tc = [s for s in sp if s["tc_seconds"] is not None]
    for i, a in enumerate(tc):
        for b in tc[i + 1:]:
            if _same_body(a, b):
                continue
            if b["tc_seconds"] <= a["tc_seconds"] + a["duration"] + 2 and a["tc_seconds"] <= b["tc_seconds"] + b["duration"] + 2:
                if a["start_epoch"] is None or b["start_epoch"] is None or abs(a["start_epoch"] - b["start_epoch"]) < 86400 * 1.5:
                    out.add(tuple(sorted((a["id"], b["id"]))))
    return sorted(out)


# ============================================================================================ detect
def _pair_cached(con, a, b):
    r = con.execute("SELECT * FROM sync_pairs WHERE a=? AND b=? AND algo=?", (a, b, ALGO_VERSION)).fetchone()
    return dict(r) if r else None


def _store_pair(con, a, b, res):
    db.upsert(con, "sync_pairs", {"a": a, "b": b, "ok": int(res["ok"]), "lag": res.get("lag"), "drift_ppm": res.get("drift_ppm"),
                                  "corr": res.get("corr"), "ratio": res.get("ratio"), "overlap": res.get("overlap"),
                                  "fine_windows": res.get("fine_windows"), "rms_ms": res.get("rms_ms"),
                                  "method": res.get("method"), "why": res.get("why"), "algo": ALGO_VERSION,
                                  "updated": time.time()}, key=("a", "b"))


def _timecode_fallback(con, a, b, res):
    """When the audio can't decide (a silent angle), jam-synced time-of-day timecode can: both devices marked
    tc_sync (cutroom sync name DEV --tc-sync) and overlapping timecode spans."""
    q = ("SELECT m.tc_seconds, d.tc_sync, c.duration FROM clip_media m JOIN devices d ON d.id=m.device_id "
         "JOIN clips c ON c.id=m.clip_id WHERE m.clip_id=?")
    ra, rb = con.execute(q, (a,)).fetchone(), con.execute(q, (b,)).fetchone()
    if not (ra and rb and ra["tc_sync"] and rb["tc_sync"] and ra["tc_seconds"] is not None and rb["tc_seconds"] is not None):
        return res
    lag = float(rb["tc_seconds"]) - float(ra["tc_seconds"])
    if lag > float(ra["duration"] or 0) or -lag > float(rb["duration"] or 0):
        return res
    return {**res, "ok": 1, "lag": round(lag, 6), "drift_ppm": 0.0, "method": "timecode",
            "why": f"timecode (audio: {res.get('why') or 'no match'})"}


def _timecode_check(con, a, b, res):
    """Audio matched: when both clips carry timecode, note whether it agrees (within one frame). Consistent
    timecode across a day lets silent angles sync on timecode alone (auto_tc_sync)."""
    q = "SELECT tc_seconds, tc_rate FROM clip_media WHERE clip_id=?"
    ra, rb = con.execute(q, (a,)).fetchone(), con.execute(q, (b,)).fetchone()
    if not (ra and rb and ra["tc_seconds"] is not None and rb["tc_seconds"] is not None):
        return res
    tc_lag = float(rb["tc_seconds"]) - float(ra["tc_seconds"])
    frame = 1.0 / float(ra["tc_rate"] or 25.0)
    if abs(tc_lag - float(res["lag"])) <= frame:
        return {**res, "method": (res.get("method") or "audio") + "+tc",
                "why": ((res.get("why") or "") + " · timecode agrees").strip(" ·")}
    return {**res, "why": ((res.get("why") or "") + f" · timecode off by {tc_lag - float(res['lag']):+.2f}s").strip(" ·")}


def auto_tc_sync(con, echo=None, min_pairs=2) -> list:
    """Devices whose timecode agreed with the audio on at least `min_pairs` matched pairs (and never disagreed) are
    marked tc_sync, so their silent clips can sync on timecode. Returns the device ids newly marked."""
    ok, bad = {}, {}
    for r in con.execute("""SELECT p.method, p.why, ma.device_id AS da, mb.device_id AS db FROM sync_pairs p
                            JOIN clip_media ma ON ma.clip_id=p.a JOIN clip_media mb ON mb.clip_id=p.b WHERE p.ok=1"""):
        for d in (r["da"], r["db"]):
            if "+tc" in (r["method"] or ""):
                ok[d] = ok.get(d, 0) + 1
            elif "timecode off" in (r["why"] or ""):
                bad[d] = bad.get(d, 0) + 1
    marked = []
    for d, n in ok.items():
        if n >= min_pairs and not bad.get(d):
            cur = con.execute("SELECT tc_sync, name FROM devices WHERE id=?", (d,)).fetchone()
            if cur and not cur["tc_sync"]:
                con.execute("UPDATE devices SET tc_sync=1, updated=? WHERE id=?", (time.time(), d))
                marked.append(d)
                _log(echo, f"sync: {cur['name']}: timecode agreed with the audio on {n} pairs -> trusted for silent clips")
    con.commit()
    return marked


def join_chunks(cfg, con, echo=None) -> list:
    """Split recordings (one long take written as several files: iXML file sets, or sample-contiguous BWF time
    references on the same device) become exact sync pairs (method 'chunk'): lag = the earlier file's length."""
    from . import bwf
    rows = con.execute("""SELECT c.id, c.path, c.duration, m.device_id FROM clips c JOIN clip_media m ON m.clip_id=c.id
                          WHERE m.audio_only=1""").fetchall()
    items = []
    for r in rows:
        if not str(r["path"]).lower().endswith((".wav", ".bwf", ".w64")):
            continue
        meta = bwf.read(r["path"])
        if meta.get("ok"):
            items.append({"id": r["id"], "path": r["path"], "device": r["device_id"], "duration": r["duration"], "meta": meta})
    by_id = {it["id"]: it for it in items}
    chains = bwf.chunk_chains(items)
    for ch in chains:
        for x, y in zip(ch, ch[1:]):
            mx = by_id[x]["meta"]
            lag = (mx.get("frames") or 0) / float(mx["sample_rate"]) if mx.get("frames") else float(by_id[x]["duration"] or 0)
            a, b = sorted((x, y))
            res = {"ok": 1, "lag": round(lag if a == x else -lag, 6), "drift_ppm": 0.0, "corr": 1.0, "ratio": 99.0,
                   "overlap": 0.0, "fine_windows": 0, "rms_ms": 0.0, "method": "chunk",
                   "why": "split recording (continues sample-exactly)"}
            _store_pair(con, a, b, res)
        _log(echo, f"sync: joined a split recording of {len(ch)} files")
    con.commit()
    return chains


def logical_recordings(con) -> list:
    """Chains of split files as [[clip ids in order]] (from the stored 'chunk' pairs)."""
    nxt, has_prev = {}, set()
    for r in con.execute("SELECT a, b, lag FROM sync_pairs WHERE ok=1 AND method='chunk'"):
        first, second = (r["a"], r["b"]) if float(r["lag"]) >= 0 else (r["b"], r["a"])
        nxt[first] = second
        has_prev.add(second)
    out = []
    for start in [k for k in nxt if k not in has_prev]:
        ch = [start]
        while ch[-1] in nxt and len(ch) < 1000:
            ch.append(nxt[ch[-1]])
        out.append(ch)
    return out


def _clock_table(con):
    ensure_schema(con)
    return [dict(r) for r in con.execute("SELECT * FROM device_clock")]


def _day_of(ep):
    return dt.datetime.fromtimestamp(ep).strftime("%Y-%m-%d") if ep is not None else "unknown"


def _offset_for(table, device_id, ep):
    rows = [r for r in table if r["device_id"] == device_id]
    if not rows:
        return 0.0
    day = _day_of(ep)
    same = [r for r in rows if r["day"] == day]
    return float((same or rows)[0]["offset_s"] or 0.0)


def corrected_start(con, cid):
    """A clip's capture start (epoch s) corrected by its device's measured clock offset for that day."""
    r = con.execute("SELECT start_epoch, device_id FROM clip_media WHERE clip_id=?", (cid,)).fetchone()
    if not r or r["start_epoch"] is None:
        return None
    return float(r["start_epoch"]) - _offset_for(_clock_table(con), r["device_id"], r["start_epoch"])


def estimate_clocks(cfg, con, echo=None, ref_device=None) -> list:
    """Per device and shoot day: its clock minus the reference device's clock, from every matched pair (the audio says
    when b really started relative to a; the files' capture times say what each clock claimed). Robust: the median
    per device pair, then a least-squares solve over the pair graph. The reference is the hero camera's device
    (ARRI) when there is one, else the device with the most matches. Writes device_clock; returns its rows."""
    ensure_schema(con)
    rows = con.execute("""SELECT p.a, p.b, p.lag, p.method, ma.device_id AS da, mb.device_id AS db, ma.start_epoch AS sa,
                                 mb.start_epoch AS sb, ma.start_src AS srca, mb.start_src AS srcb
                          FROM sync_pairs p JOIN clip_media ma ON ma.clip_id=p.a JOIN clip_media mb ON mb.clip_id=p.b
                          WHERE p.ok=1 AND p.method != 'chunk' AND ma.start_epoch IS NOT NULL AND mb.start_epoch IS NOT NULL
                                AND ma.device_id != mb.device_id""").fetchall()
    per = {}
    for r in rows:
        if "mtime" in (r["srca"] or "") or "mtime" in (r["srcb"] or ""):
            continue                                            # a file date is not a camera clock
        err = (float(r["sb"]) - float(r["sa"])) - float(r["lag"])   # b's clock minus a's clock
        day = _day_of(r["sa"])
        per.setdefault(day, {}).setdefault((r["da"], r["db"]), []).append(err)
    hero = None
    try:
        from . import cameras as CAM
        CAM.ensure_schema(con)
        h = con.execute("""SELECT m.device_id, COUNT(*) n FROM clip_media m JOIN clip_camera k ON k.clip_id=m.clip_id
                           WHERE k.family=? GROUP BY m.device_id ORDER BY n DESC LIMIT 1""", (CAM.HERO_DEFAULT,)).fetchone()
        hero = h["device_id"] if h else None
    except Exception:  # noqa: BLE001
        hero = None
    con.execute("DELETE FROM device_clock")
    out = []
    for day, edges in per.items():
        devs = sorted({d for e in edges for d in e})
        cnt = {d: sum(len(v) for e, v in edges.items() if d in e) for d in devs}
        ref = ref_device if ref_device in devs else (hero if hero in devs else max(devs, key=lambda d: (cnt[d], -d)))
        # connected to the reference only
        idx = {d: i for i, d in enumerate(devs)}
        A, y, w = [], [], []
        for (da, db_), v in edges.items():
            row = np.zeros(len(devs))
            row[idx[db_]] += 1.0
            row[idx[da]] -= 1.0
            A.append(row)
            y.append(float(np.median(v)))
            w.append(math.sqrt(len(v)))
        row = np.zeros(len(devs))
        row[idx[ref]] = 1.0
        A.append(row)
        y.append(0.0)
        w.append(100.0)
        A, y, w = np.asarray(A), np.asarray(y), np.asarray(w)
        sol, *_ = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)
        reach = {ref}
        changed = True
        while changed:
            changed = False
            for (da, db_) in edges:
                if (da in reach) != (db_ in reach):
                    reach |= {da, db_}
                    changed = True
        for d in devs:
            if d not in reach or d == ref:
                continue
            spread = [abs((v_ - (sol[idx[b_]] - sol[idx[a_]]))) for (a_, b_), vs in edges.items() if d in (a_, b_) for v_ in vs]
            rec = {"device_id": d, "day": day, "offset_s": round(float(sol[idx[d]]), 3), "n": cnt[d],
                   "spread_s": round(float(np.median(spread)) if spread else 0.0, 3), "ref_device": ref, "updated": time.time()}
            db.upsert(con, "device_clock", rec, key=("device_id", "day"))
            out.append(rec)
            if echo and abs(rec["offset_s"]) >= 1.0:
                nm = con.execute("SELECT name FROM devices WHERE id=?", (d,)).fetchone()
                _log(echo, f"sync: {nm['name'] if nm else d} clock is {rec['offset_s']:+.1f}s off on {day} "
                           f"({rec['n']} pair(s))")
    con.commit()
    return out


def detect(cfg, con, clips=None, force=False, echo=print) -> dict:
    """Register recorder audio, probe metadata, align candidate pairs, rebuild groups. `clips` (refs) = align
    exactly these against each other (no timestamps needed)."""
    from . import governor
    ensure_schema(con)
    t0 = time.time()
    register_audio(cfg, con, echo=echo)
    ids = None
    if clips:
        ids = [db.get_clip(con, c)["id"] for c in clips]
    probe_media(cfg, con, ids=ids, echo=echo)
    if ids:
        pairs = sorted({tuple(sorted((a, b))) for i, a in enumerate(ids) for b in ids[i + 1:]})
    else:
        pairs = candidates(cfg, con)
    rows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM clips WHERE has_audio=1")}
    ac = AudioCache(cfg)
    gov = governor.for_cfg(cfg)
    stats = {"pairs": 0, "measured": 0, "accepted": 0, "rejected": 0}
    chains = join_chunks(cfg, con, echo=echo)
    stats["chunk_chains"] = len(chains)

    def run(plist):
        for a, b in sorted(plist):
            if a not in rows or b not in rows:
                continue
            stats["pairs"] += 1
            cached = _pair_cached(con, a, b)
            if cached and cached.get("method") == "chunk":          # a split recording: exact, never re-measured
                stats["accepted"] += 1
                continue
            if not force and cached:
                if _pair_cached(con, a, b)["ok"]:
                    stats["accepted"] += 1
                continue
            with gov.slot("heavy", label=f"sync {a}~{b}"):
                try:
                    res = align_pair(cfg, rows[a], rows[b], ac)
                except Exception as e:  # noqa: BLE001
                    res = {"ok": 0, "why": f"{type(e).__name__}: {e}"}
            if not res["ok"]:
                res = _timecode_fallback(con, a, b, res)
            else:
                res = _timecode_check(con, a, b, res)
            _store_pair(con, a, b, res)
            con.commit()
            stats["measured"] += 1
            stats["accepted" if res["ok"] else "rejected"] += 1
            _log(echo, f"  {'ok ' if res['ok'] else 'no '} {rows[a]['rel']} ~ {rows[b]['rel']}: " +
                 (f"offset {res['lag']:+.4f}s drift {res['drift_ppm']:+.1f} ppm corr {res['corr']:.2f} "
                  f"ratio {res['ratio']:.1f} rms {res.get('rms_ms')} ms" if res["ok"] else res.get("why", "")))

    run(pairs)
    # pass 2: whole-hour (quarter-hour) clock offsets for device pairs that matched nothing (wrong time zone on one device)
    if not ids and getattr(cfg, "sync_tz_slop", True):
        sp = {s["id"]: s for s in _spans(con)}
        matched = set()
        for r in con.execute("SELECT a, b FROM sync_pairs WHERE ok=1"):
            if r[0] in sp and r[1] in sp:
                matched.add(frozenset((sp[r[0]]["device_id"], sp[r[1]]["device_id"])))
        devs = {s["device_id"] for s in sp.values()}
        lonely = {(x, y) for x in devs for y in devs if x != y and frozenset((x, y)) not in matched}
        if lonely:
            # a camera whose clock is minutes off (never set, drifted, a battery swap reset it): a wider window,
            # for device pairs that matched nothing yet; the audio still has to agree
            wide = float(getattr(cfg, "sync_lonely_window_s", 1800.0) or 0)
            if wide > float(getattr(cfg, "sync_window_s", 300.0)):
                near = {pr for pr in candidates(cfg, con, window_s=wide)
                        if pr[0] in sp and pr[1] in sp and (sp[pr[0]]["device_id"], sp[pr[1]]["device_id"]) in lonely}
                near -= set(pairs)
                if near:
                    _log(echo, f"sync: trying {len(near)} pair(s) of unmatched cameras within {wide / 60:.0f} min")
                    run(near)
                    matched = set()
                    for r in con.execute("SELECT a, b FROM sync_pairs WHERE ok=1"):
                        if r[0] in sp and r[1] in sp:
                            matched.add(frozenset((sp[r[0]]["device_id"], sp[r[1]]["device_id"])))
                    lonely = {(x, y) for x, y in lonely if frozenset((x, y)) not in matched}
                    pairs = sorted(set(pairs) | near)
        if lonely:
            extra = set()
            for q in range(-56, 57):                          # every quarter hour from -14 h to +14 h (all time zones)
                if q:
                    extra |= set(candidates(cfg, con, window_s=min(120.0, float(getattr(cfg, "sync_window_s", 300))),
                                            hour_shift=q / 4.0, devices=lonely))
            extra -= set(pairs)
            if extra:
                _log(echo, f"sync: trying {len(extra)} pair(s) with whole-hour clock offsets")
                run(extra)
    groups = rebuild_groups(cfg, con, echo=echo)
    # pass 3: per-camera clock offsets measured from the matched pairs, then the pairs that the corrected clocks
    # now put side by side (a camera whose clock is minutes off still lands next to the right moment)
    if not ids:
        clocks = estimate_clocks(cfg, con, echo=echo)
        stats["clock_offsets"] = len(clocks)
        if clocks:
            tried = {(r[0], r[1]) for r in con.execute("SELECT a, b FROM sync_pairs")}
            extra = set(candidates(cfg, con, corrected=True)) - tried - set(pairs)
            if extra:
                _log(echo, f"sync: trying {len(extra)} pair(s) after clock correction")
                run(extra)
                groups = rebuild_groups(cfg, con, echo=echo)
                estimate_clocks(cfg, con, echo=None)
        auto_tc_sync(con, echo=echo)
    stats.update(groups=len(groups), seconds=round(time.time() - t0, 1))
    _log(echo, f"sync: {stats['pairs']} pair(s) ({stats['measured']} measured) · {stats['accepted']} matched · "
               f"{len(groups)} group(s) · {stats['seconds']}s")
    return stats


def rebuild_groups(cfg, con, echo=None) -> list:
    """Connected components of accepted pairs -> sync_groups / sync_members (group ids and names survive)."""
    ensure_schema(con)
    pairs = [dict(r) for r in con.execute("SELECT * FROM sync_pairs WHERE ok=1")]
    clips = {r["id"]: dict(r) for r in con.execute(
        "SELECT c.id, c.rel, c.duration, c.width, c.shot_at, m.device_id, m.start_epoch, d.kind, d.code "
        "FROM clips c LEFT JOIN clip_media m ON m.clip_id=c.id LEFT JOIN devices d ON d.id=m.device_id")}
    pairs = [p for p in pairs if p["a"] in clips and p["b"] in clips]
    adj = {}
    for p in pairs:
        adj.setdefault(p["a"], []).append(p)
        adj.setdefault(p["b"], []).append(p)
    seen, comps = set(), []
    for c in sorted(adj):
        if c in seen:
            continue
        stack, comp = [c], set()
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            for p in adj[x]:
                stack.append(p["b"] if p["a"] == x else p["a"])
        seen |= comp
        comps.append(comp)
    old = {}
    for r in con.execute("SELECT group_id, clip_id FROM sync_members"):
        old.setdefault(r[0], set()).add(r[1])
    old_meta = {r["id"]: dict(r) for r in con.execute("SELECT * FROM sync_groups")}
    used_ids, out = set(), []
    con.execute("DELETE FROM sync_members")
    for comp in comps:
        ref = max(comp, key=lambda c: ((clips[c]["width"] or 0) > 0, clips[c]["duration"] or 0, c))
        # maximum-confidence spanning tree from the reference; compose g = off + t*(1+d)
        sol = {ref: (0.0, 0.0, 1.0, "reference")}               # clip -> (offset, drift, conf, method)
        frontier = [ref]
        while frontier:
            best = None
            for p in pairs:
                if (p["a"] in sol) == (p["b"] in sol):
                    continue
                score = (p["corr"] or 0) * min(p["ratio"] or 1, 5)
                if best is None or score > best[0]:
                    best = (score, p)
            if best is None:
                break
            p = best[1]
            lag, drift = float(p["lag"]), float(p["drift_ppm"] or 0) * 1e-6
            if p["a"] in sol:                                   # a known: a-time = b-time*(1/(1-b)) + lag/(1-b)
                off_a, d_a, _, _ = sol[p["a"]]
                s_a = 1.0 + d_a
                off_b = off_a + s_a * lag / (1.0 - drift)
                s_b = s_a / (1.0 - drift)
                sol[p["b"]] = (off_b, s_b - 1.0, float(p["corr"] or 0), p["method"] or "audio")
            else:                                               # b known: b-time = a-time*(1-drift) - lag
                off_b, d_b, _, _ = sol[p["b"]]
                s_b = 1.0 + d_b
                off_a = off_b - s_b * lag
                s_a = s_b * (1.0 - drift)
                sol[p["a"]] = (off_a, s_a - 1.0, float(p["corr"] or 0), p["method"] or "audio")
            frontier = [x for x in comp if x not in sol]
        # loop closure error over every measured pair
        err = 0.0
        for p in pairs:
            if p["a"] in sol and p["b"] in sol:
                oa, da, _, _ = sol[p["a"]]
                ob, db_, _, _ = sol[p["b"]]
                # the event at a-time t (group g) must be at b-time t - lag
                t = 0.5 * max(0.0, float(clips[p["a"]]["duration"] or 0))
                g = oa + t * (1 + da)
                tb = (g - ob) / (1 + db_)
                pred = t - (float(p["lag"]) + float(p["drift_ppm"] or 0) * 1e-6 * t)
                err = max(err, abs(tb - pred))
        g0 = min(o for o, _, _, _ in sol.values())
        # keep the id of the old group this one overlaps most
        gid, best_n = None, 0
        for og, mem in old.items():
            n = len(mem & comp)
            if n > best_n and og not in used_ids:
                gid, best_n = og, n
        now = time.time()
        rref = clips[ref]
        name = (old_meta.get(gid) or {}).get("name") if gid else None
        dur = max(sol[c][0] - g0 + (clips[c]["duration"] or 0) * (1 + sol[c][1]) for c in sol)
        start_ep = (rref["start_epoch"] + g0) if rref.get("start_epoch") is not None else None
        day = (rref.get("shot_at") or "")[:10] or (dt.datetime.fromtimestamp(start_ep).strftime("%Y-%m-%d") if start_ep else "unknown")
        if gid is None:
            cur = con.execute("INSERT INTO sync_groups (name, ref_clip, day, start_epoch, duration, loop_err_ms, created, updated) "
                              "VALUES (?,?,?,?,?,?,?,?)", (None, ref, day, start_ep, dur, err * 1000, now, now))
            gid = cur.lastrowid
        else:
            con.execute("UPDATE sync_groups SET ref_clip=?, day=?, start_epoch=?, duration=?, loop_err_ms=?, updated=? WHERE id=?",
                        (ref, day, start_ep, dur, err * 1000, now, gid))
        if not name:
            name = f"Group {gid} · {day}"
            con.execute("UPDATE sync_groups SET name=? WHERE id=?", (name, gid))
        used_ids.add(gid)
        codes = {}
        for c in sorted(sol, key=lambda c: (sol[c][0], c)):
            off, drift, conf, meth = sol[c]
            row = clips[c]
            kind = row.get("kind") or ("camera" if (row["width"] or 0) else "recorder")
            code = row.get("code") or ("cam" if (row["width"] or 0) else "rec")
            k = codes.get(code, 0) + 1
            codes[code] = k
            angle = code if k == 1 else f"{code}{k}"
            con.execute("INSERT INTO sync_members (group_id, clip_id, angle, offset, drift_ppm, confidence, method, kind, video, "
                        "audio_rank, duration) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (gid, c, angle, round(off - g0, 6), round(drift * 1e6, 3), round(conf, 4), meth, kind,
                         int((row["width"] or 0) > 0), KIND_RANK.get(kind, 2), row["duration"]))
        out.append(gid)
    stale = set(old_meta) - used_ids
    for g in stale:
        con.execute("DELETE FROM sync_groups WHERE id=?", (g,))
    con.commit()
    return out


# ============================================================================================ queries
def groups(con) -> list:
    ensure_schema(con)
    out = []
    for g in con.execute("SELECT * FROM sync_groups ORDER BY day DESC, start_epoch, id"):
        out.append(group(con, g["id"]))
    return out


def group(con, gid) -> dict:
    ensure_schema(con)
    g = con.execute("SELECT * FROM sync_groups WHERE id=?", (int(gid),)).fetchone()
    if not g:
        raise KeyError(f"no sync group {gid!r} (cutroom sync list)")
    mem = []
    for r in con.execute("""SELECT m.*, c.rel, c.path, c.fps, c.width, c.height, d.name AS device_name, d.code AS device_code,
                                   d.make, d.model, d.serial
                            FROM sync_members m JOIN clips c ON c.id=m.clip_id
                            LEFT JOIN clip_media cm ON cm.clip_id=m.clip_id LEFT JOIN devices d ON d.id=cm.device_id
                            WHERE m.group_id=? ORDER BY m.video DESC, m.angle""", (int(gid),)):
        d = dict(r)
        d["end"] = round(d["offset"] + (d["duration"] or 0) * (1 + (d["drift_ppm"] or 0) * 1e-6), 4)
        mem.append(d)
    return {**dict(g), "members": mem, "angles": [m["angle"] for m in mem if m["video"]],
            "audio_sources": [m["angle"] for m in sorted(mem, key=lambda m: (m["audio_rank"], -(m["confidence"] or 0)))]}


def clip_groups(con) -> dict:
    """clip id -> {group, angle, name} (for library badges)."""
    ensure_schema(con)
    return {r["clip_id"]: {"group": r["group_id"], "angle": r["angle"], "name": r["name"]}
            for r in con.execute("SELECT m.clip_id, m.group_id, m.angle, g.name FROM sync_members m "
                                 "JOIN sync_groups g ON g.id=m.group_id")}


def groups_by_day(con) -> dict:
    ensure_schema(con)
    out = {}
    for r in con.execute("SELECT g.id, g.name, g.day, g.duration, COUNT(m.clip_id) AS n, SUM(m.video) AS nv "
                         "FROM sync_groups g JOIN sync_members m ON m.group_id=g.id GROUP BY g.id"):
        out.setdefault(r["day"] or "unknown", []).append({"group": r["id"], "name": r["name"], "duration": r["duration"],
                                                          "members": r["n"], "angles": r["nv"] or 0})
    return out


def member(g, ref) -> dict:
    """A member of group g by angle code, device name, clip id or filename fragment."""
    ref_s = str(ref).strip()
    low = ref_s.lower()
    for m in g["members"]:
        if m["angle"].lower() == low or m["clip_id"] == ref_s:
            return m
    for m in g["members"]:
        if (m.get("device_name") or "").lower() == low or (m.get("device_code") or "").lower() == low:
            return m
    hits = [m for m in g["members"] if low and low in (m["rel"] or "").lower()]
    if len(hits) == 1:
        return hits[0]
    raise KeyError(f"group {g['id']} has no angle {ref!r}; angles: {', '.join(m['angle'] for m in g['members'])}")


def to_group(m, t):
    return float(m["offset"]) + float(t) * (1.0 + float(m["drift_ppm"] or 0) * 1e-6)


def from_group(m, g):
    return (float(g) - float(m["offset"])) / (1.0 + float(m["drift_ppm"] or 0) * 1e-6)


def best_audio(g, prefer=None, angle_member=None, span=None):
    """The member whose sound a multicam segment uses. prefer: 'auto' (external recorder > lav > camera; among
    cameras the angle's own sound wins), 'camera' (the angle's own sound), or an angle / device name.
    span = (g0, g1) group seconds the segment needs: with 'auto', a source that covers it beats one that doesn't
    (a recorder take split into several files: the file that holds the moment)."""
    if prefer and str(prefer).lower() not in ("auto", "best"):
        if str(prefer).lower() == "camera" and angle_member is not None:
            return angle_member
        return member(g, prefer)
    cands = [m for m in g["members"]]

    def covers(m):
        if not span:
            return 0
        return 0 if (float(m["offset"]) - 0.05 <= span[0] and span[1] <= float(m["end"]) + 0.05) else 1
    return min(cands, key=lambda m: (covers(m), m["audio_rank"], 0 if angle_member is not None and m["clip_id"] == angle_member["clip_id"]
                                     else 1, -(m["confidence"] or 0), m["angle"]))


def concrete(con, seg) -> dict:
    """A multicam talk/broll segment -> the same segment on the chosen angle's clip, plus `_mc_audio`
    {clip, k, b} (audio time = k * picture time + b) for the best sound. Segments without multicam pass."""
    mc = seg.get("multicam")
    if not mc:
        return seg
    g = group(con, mc["group"])
    ang = member(g, mc.get("angle") or g["angles"][0])
    out = dict(seg)
    if seg.get("clip"):
        src = db.get_clip(con, seg["clip"])
        src_m = next((m for m in g["members"] if m["clip_id"] == src["id"]), None)
        if src_m is None:
            raise ValueError(f"multicam: clip {seg['clip']} is not in group {g['id']}")
        if src_m["clip_id"] != ang["clip_id"]:
            for k in ("in", "out"):
                if seg.get(k) is not None:
                    out[k] = round(from_group(ang, to_group(src_m, float(seg[k]))), 4)
    else:
        for k in ("in", "out"):
            if seg.get(k) is not None:
                out[k] = round(from_group(ang, float(seg[k])), 4)
    out["clip"] = ang["clip_id"]
    span = None
    if out.get("in") is not None and out.get("out") is not None:
        span = tuple(sorted((to_group(ang, float(out["in"])), to_group(ang, float(out["out"])))))
    au = best_audio(g, mc.get("audio"), ang, span=span)
    if au["clip_id"] != ang["clip_id"]:
        s_p = 1.0 + float(ang["drift_ppm"] or 0) * 1e-6
        s_a = 1.0 + float(au["drift_ppm"] or 0) * 1e-6
        k_ = s_p / s_a
        b_ = (float(ang["offset"]) - float(au["offset"])) / s_a
        out["_mc_audio"] = {"clip": au["clip_id"], "k": k_, "b": b_, "angle": au["angle"]}
    out["_mc"] = {"group": g["id"], "angle": ang["angle"], "audio": au["angle"]}
    return out


def validate_segment(con, w, seg) -> list:
    mc = seg.get("multicam")
    if not mc:
        return []
    if not isinstance(mc, dict) or mc.get("group") is None:
        return [f"{w}: multicam must be {{group, angle, audio?}}"]
    if con is None:
        return []
    try:
        c = concrete(con, seg)
        g = group(con, mc["group"])
        ang = member(g, mc.get("angle") or g["angles"][0])
    except (KeyError, ValueError) as e:
        return [f"{w}: {e}"]
    errs = []
    for k in ("in", "out"):
        t = c.get(k)
        if t is not None and not (-0.01 <= float(t) <= float(ang["duration"] or 0) + 0.01):
            errs.append(f"{w}: angle {ang['angle']} does not cover {k}={seg.get(k)} (its clip runs 0-{ang['duration']:.2f}s, "
                        f"group {to_group(ang, 0):.2f}-{ang['end']:.2f}s)")
    au = c.get("_mc_audio")
    if au:
        am = member(g, au["angle"])
        for k in ("in", "out"):
            if c.get(k) is not None:
                ta = au["k"] * float(c[k]) + au["b"]
                if not (-0.05 <= ta <= float(am["duration"] or 0) + 0.05):
                    errs.append(f"{w}: audio source {am['angle']} does not cover {k} (set multicam.audio to 'camera' "
                                "or another source)")
                    break
    return errs


def switch_angle(con, seg, angle, audio=None) -> dict:
    """Rewrite a multicam segment onto another angle: clip, in and out follow the group's offsets."""
    mc = dict(seg.get("multicam") or {})
    if not mc.get("group"):
        raise ValueError("segment has no multicam group")
    mc["angle"] = angle
    if audio is not None:
        mc["audio"] = audio
    c = concrete(con, {**seg, "multicam": mc})
    out = {k: v for k, v in seg.items()}
    out.update(clip=c["clip"], multicam=mc)
    for k in ("in", "out"):
        if c.get(k) is not None:
            out[k] = c[k]
    return out


def map_time(con, gid, t, frm=None, to=None):
    g = group(con, gid)
    gt = to_group(member(g, frm), t) if frm else float(t)
    return from_group(member(g, to), gt) if to else gt


def name_device(con, ref, name=None, kind=None, code=None, tc_sync=None):
    ensure_schema(con)
    r = con.execute("SELECT * FROM devices WHERE CAST(id AS TEXT)=? OR code=? OR name=?", (str(ref), ref, ref)).fetchone()
    if not r:
        raise KeyError(f"no device {ref!r} (cutroom sync devices)")
    f = {}
    if name:
        f["name"] = name
    if kind:
        if kind not in KIND_RANK:
            raise ValueError(f"kind must be one of {sorted(KIND_RANK)}")
        f["kind"] = kind
    if code:
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,15}$", code):
            raise ValueError("code: letters/digits, starts with a letter, at most 16")
        f["code"] = code
    if tc_sync is not None:
        f["tc_sync"] = int(bool(tc_sync))
    if f:
        con.execute(f"UPDATE devices SET {', '.join(k + '=?' for k in f)}, updated=? WHERE id=?", [*f.values(), time.time(), r["id"]])
        con.commit()
        if "kind" in f or "code" in f:                       # members carry kind / angle code: refresh them
            rebuild_groups(None, con)
    return dict(con.execute("SELECT * FROM devices WHERE id=?", (r["id"],)).fetchone())


def devices(con) -> list:
    ensure_schema(con)
    return [dict(r) for r in con.execute(
        "SELECT d.*, COUNT(m.clip_id) AS clips FROM devices d LEFT JOIN clip_media m ON m.device_id=d.id GROUP BY d.id ORDER BY d.code")]


def rename_group(con, gid, name):
    ensure_schema(con)
    con.execute("UPDATE sync_groups SET name=?, updated=? WHERE id=?", (name, time.time(), int(gid)))
    con.commit()
