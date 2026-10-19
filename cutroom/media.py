"""Thin, logged wrappers around ffprobe/ffmpeg. Every module renders through `ffmpeg()` so one
place controls binaries, logging, retries and the optional remote-render prefix."""
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


class FFError(RuntimeError):
    pass


def which(name, fallback=None):
    p = shutil.which(name)
    if not p and fallback:
        p = shutil.which(fallback)
    return p


def run(cmd, check=True, capture=True, log=None, timeout=None):
    t = time.time()
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    if log:
        with open(log, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {time.time() - t:6.1f}s rc={r.returncode} {shlex.join(map(str, cmd))[:4000]}\n")
            if r.returncode and r.stderr:
                f.write(r.stderr[-4000:] + "\n")
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
                     ("faster_whisper", "transcription (optional extra 'asr')"), ("df", "voice clean (optional extra 'voice')")):
        try:
            __import__(mod)
            res.append((f"python: {mod}", True, why))
        except Exception as e:  # noqa
            res.append((f"python: {mod}", False, f"{why}: {type(e).__name__}"))
    res.append(("python", sys.version_info >= (3, 10), sys.version.split()[0]))
    return res


def hw_encoder(cfg) -> list:
    """Fast encoder args for proxies/previews: VideoToolbox on macOS, NVENC on NVIDIA, else x264 veryfast."""
    enc = run([cfg.ffmpeg, "-hide_banner", "-encoders"], check=False).stdout
    if "h264_videotoolbox" in enc and sys.platform == "darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", "2500k"]
    if "h264_nvenc" in enc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.proxy_crf)]


def frame_at(cfg, src, t, out, width=640):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(cfg, ["-ss", f"{max(0, t):.3f}", "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2", "-update", "1", out])
    return out
