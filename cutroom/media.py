"""Thin, logged wrappers around ffprobe/ffmpeg. Every module renders through `ffmpeg()` so one
place controls binaries, logging, retries and the optional remote-render prefix."""
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


class FFError(RuntimeError):
    pass


class GovernorKilled(FFError):
    """The resource governor stopped the process (memory cap or system RAM emergency)."""


_RUN_HOOK = None


def set_run_hook(fn):
    """Route every run() through `fn(cmd, capture=, timeout=)` (the resource governor sets this so
    ffmpeg/ffprobe run niced and footprint-monitored). None restores plain subprocess.run."""
    global _RUN_HOOK
    _RUN_HOOK = fn


def which(name, fallback=None):
    p = shutil.which(name)
    if not p and fallback:
        p = shutil.which(fallback)
    return p


def run(cmd, check=True, capture=True, log=None, timeout=None):
    t = time.time()
    hook = _RUN_HOOK
    if hook is not None:
        r = hook(cmd, capture=capture, timeout=timeout)
    else:
        r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    killed = getattr(r, "killed", None)
    if log:
        with open(log, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {time.time() - t:6.1f}s rc={r.returncode} {shlex.join(map(str, cmd))[:4000]}\n")
            if killed:
                f.write(f"stopped by resource governor: {killed}\n")
            if r.returncode and r.stderr:
                f.write(r.stderr[-4000:] + "\n")
    if killed and (check or r.returncode):
        raise GovernorKilled(f"stopped by resource governor: {killed}: {shlex.join(map(str, cmd))[:300]}")
    if check and r.returncode:
        raise FFError(f"command failed ({r.returncode}): {shlex.join(map(str, cmd))[:600]}\n{(r.stderr or '')[-2000:]}")
    return r


def ffmpeg(cfg, args, log=None, check=True, timeout=None):
    """Run ffmpeg with `args` (list, without the binary). Honors cfg.remote_render as a command prefix."""
    prefix = shlex.split(cfg.remote_render) if getattr(cfg, "remote_render", "") else []
    cmd = prefix + [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"] + [str(a) for a in args]
    return run(cmd, check=check, log=log or cfg.p("renders", "ffmpeg.log"), timeout=timeout)


def probe(cfg, path) -> dict:
    r = run([cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(r.stdout or "{}")


def summarize_probe(info: dict) -> dict:
    """Flatten ffprobe output into the fields the clips table stores."""
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    out = {"duration": float(fmt.get("duration") or 0), "has_audio": int(a is not None),
           "audio_channels": int(a.get("channels") or 0) if a else 0}
    tags = {**fmt.get("tags", {}), **(v or {}).get("tags", {})}
    out["shot_at"] = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
    if v:
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        rot = int(tags.get("rotate", rot) or 0)
        out.update(fps=round(fps, 3), width=int(v.get("width") or 0), height=int(v.get("height") or 0),
                   codec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"),
                   color_transfer=v.get("color_transfer") or "", rotation=rot)
        if not out["duration"]:
            out["duration"] = float(v.get("duration") or 0)
    return out


def doctor(cfg) -> list:
    """Return a list of (check, ok, detail) for the environment."""
    res = []
    for b in (cfg.ffmpeg, cfg.ffprobe):
        res.append((b, bool(which(b)), which(b) or "not on PATH"))
    if which(cfg.ffmpeg):
        filt = run([cfg.ffmpeg, "-hide_banner", "-filters"], check=False).stdout
        for f in ("lut3d", "loudnorm", "scale", "overlay", "afftdn"):
            res.append((f"ffmpeg filter {f}", f" {f} " in filt, ""))
        enc = run([cfg.ffmpeg, "-hide_banner", "-encoders"], check=False).stdout
        res.append(("libx264", "libx264" in enc, ""))
        res.append(("h264_videotoolbox (mac hw)", "h264_videotoolbox" in enc, "optional"))
        res.append(("h264_nvenc (nvidia hw)", "h264_nvenc" in enc, "optional"))
    for mod, why in (("cv2", "analysis"), ("librosa", "beats"), ("scenedetect", "shot detection"),
                     ("df", "voice clean (optional extra 'voice')")):
        try:
            __import__(mod)
            res.append((f"python: {mod}", True, why))
        except Exception as e:  # noqa
            res.append((f"python: {mod}", False, f"{why}: {type(e).__name__}"))
    from . import asr
    fw = asr.faster_whisper_available()
    res.append(("python: faster_whisper", fw, "transcription (optional extra 'asr')" + ("" if fw else ": not installed")))
    if asr.is_apple_silicon():
        ml = asr.mlx_available()
        res.append(("python: mlx_whisper", ml, "Apple GPU transcription (optional extra 'mlx')"
                    + ("" if ml else ": not installed")))
    res.append(("ASR backend", *asr.backend_summary(cfg)))
    res.append(("python", sys.version_info >= (3, 10), sys.version.split()[0]))
    return res


def proxy_bitrate(cfg) -> str:
    """Hardware-encoder bitrate for proxies: cfg.proxy_bitrate, or scaled from proxy_height.
    540p at 1.2 Mb/s is about 0.16 MB per second of footage (54 GB per 100 h); 360p at 0.6 Mb/s half that."""
    b = str(getattr(cfg, "proxy_bitrate", "") or "").strip()
    if b:
        return b
    h = int(getattr(cfg, "proxy_height", 540) or 540)
    return f"{max(300, int(round(1200 * (h / 540) ** 1.7 / 50)) * 50)}k"


_ENCODERS = {}


def hw_encoder(cfg, bitrate=None) -> list:
    """Fast encoder args for proxies/previews: VideoToolbox on macOS, NVENC on NVIDIA, else x264 veryfast.
    The hardware bitrate comes from cfg (proxy_bitrate / proxy_height), never a hard-coded number."""
    key = cfg.ffmpeg
    if key not in _ENCODERS:
        _ENCODERS[key] = run([cfg.ffmpeg, "-hide_banner", "-encoders"], check=False).stdout or ""
    enc = _ENCODERS[key]
    b = bitrate or proxy_bitrate(cfg)
    if "h264_videotoolbox" in enc and sys.platform == "darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", b]
    if "h264_nvenc" in enc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "28", "-maxrate", b]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.proxy_crf), "-maxrate", b, "-bufsize", b]


# -- GPS --------------------------------------------------------------------------------------------
_ISO6709 = re.compile(r"^\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?")


def parse_iso6709(s) -> tuple | None:
    """'+37.7749-122.4194+010.000/' -> (lat, lon, alt|None). Phones and many cameras write this in the
    `location` / `com.apple.quicktime.location.ISO6709` tags. None when it is not decimal-degree ISO 6709."""
    m = _ISO6709.match(str(s or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon, float(m.group(3)) if m.group(3) else None


def _dms(v, ref) -> float | None:
    """'35:39.1234' or '35:39:07.4' (+ N/S/E/W) -> decimal degrees."""
    try:
        parts = [float(x) for x in str(v).replace(",", ".").split(":")]
    except ValueError:
        return None
    deg = parts[0] + (parts[1] / 60 if len(parts) > 1 else 0) + (parts[2] / 3600 if len(parts) > 2 else 0)
    return -deg if str(ref or "").upper()[:1] in ("S", "W") else deg


def geo_from_xml(text: str) -> tuple | None:
    """Camera XML sidecars (e.g. NonRealTimeMeta ExifGPS items Latitude/LatitudeRef/Longitude/LongitudeRef)."""
    items = dict(re.findall(r'<Item\s+name="(\w+)"\s+value="([^"]*)"', text or ""))
    if "Latitude" in items and "Longitude" in items:
        lat, lon = _dms(items["Latitude"], items.get("LatitudeRef")), _dms(items["Longitude"], items.get("LongitudeRef"))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
            return lat, lon, None
    return None


def geo_from_probe(info: dict) -> tuple | None:
    """GPS (lat, lon, alt) from ffprobe format/stream tags, or None."""
    tags = dict((info.get("format") or {}).get("tags") or {})
    for s in info.get("streams", []) or []:
        tags.update(s.get("tags") or {})
    for k, v in tags.items():
        kl = k.lower()
        if kl.startswith("location") or kl.endswith("location.iso6709"):
            g = parse_iso6709(v)
            if g:
                return g
    return None


def frame_at(cfg, src, t, out, width=640):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(cfg, ["-ss", f"{max(0, t):.3f}", "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2", "-update", "1", out])
    return out
