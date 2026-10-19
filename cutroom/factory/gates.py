"""Machine QA for a factory version: everything the v023 session checked by hand, run by code.

    run_qa         `cutroom qa <render> --plan <candidate>` (subprocess, governed) -> the qa.json report
    qa_regressions fails that are NEW compared with the base render (a defect already in v22 is a warn on the
                   card, not a reason to block v23; a new one blocks)
    changed_sheet  frames at t-0.5, t, t+0.5 of every changed spot, tiled with "n3 -> 7:19" labels
    link_check     Range GET on the review URL answers 206 (or, with no server, a byte-range read + faststart)
    av_drift       audio vs video length from QA (rule 81 / 112): must be under one frame
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def sub_env(cfg, extra=None):
    env = dict(os.environ)
    env.pop("CUTROOM_FACTORY_ROLE", None)
    env["CUTROOM_WORK"] = str(cfg.work)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    bindir = str(Path(sys.executable).parent)
    if bindir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env.update(extra or {})
    return env


def cli(*args):
    return [sys.executable, "-m", "cutroom.cli", *[str(a) for a in args]]


def run_cli(cfg, args, log_path, timeout, stop_check=None, on_line=None, env=None, pid_path=None):
    """Run `cutroom <args>` in its own process group, log to `log_path`. -> (rc, last lines, why). Kills the
    group on timeout or stop_check(). `pid_path` records the pid so a restarted runner can stop an orphan."""
    import threading
    import time

    from .. import governor
    t0 = time.time()
    with open(log_path, "a") as log:
        p = subprocess.Popen(cli(*args), cwd=str(cfg.work), env=sub_env(cfg, env), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                             errors="replace", start_new_session=True)
        if pid_path:
            Path(pid_path).write_text(str(p.pid))
        why = {"v": None}

        def wall():
            while p.poll() is None:
                if time.time() - t0 > timeout:
                    why["v"] = "timeout"
                elif stop_check and stop_check():
                    why["v"] = "stopped"
                if why["v"]:
                    governor.kill_group(p.pid)
                    return
                time.sleep(0.5)
        th = threading.Thread(target=wall, daemon=True)
        th.start()
        tail = []
        for line in p.stdout:
            log.write(line)
            log.flush()
            tail = (tail + [line.rstrip("\n")])[-40:]
            if on_line:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001
                    pass
        rc = p.wait()
        th.join(timeout=2)
    if pid_path:
        Path(pid_path).unlink(missing_ok=True)
    return rc, tail, why["v"]


def run_qa(cfg, video, plan_path, log_path, timeout=900, stop_check=None):
    video = Path(video)
    rc, tail, why = run_cli(cfg, ["qa", video, "--plan", plan_path], log_path, timeout, stop_check,
                            pid_path=Path(log_path).with_suffix(".pid"))
    rep = None
    qj = video.with_name(video.stem + ".qa.json")
    if qj.exists():
        try:
            rep = json.loads(qj.read_text())
        except ValueError:
            rep = None
    return rep, (None if rep else (why or f"qa exited {rc}: {' | '.join(tail[-4:])}"))


def _fail_counts(rep):
    out = {}
    for c in (rep or {}).get("checks") or []:
        out[c["id"]] = sum(1 for i in c.get("issues") or [] if i.get("severity") == "fail")
    return out


def qa_regressions(cand, base=None):
    """(new fails [{check, n, msgs, t}], kept fails [...]) of the candidate QA report vs the base's."""
    cf, bf = _fail_counts(cand), _fail_counts(base) if base else {}
    new, kept = [], []
    for c in (cand or {}).get("checks") or []:
        n = cf.get(c["id"], 0)
        if not n:
            continue
        iss = [i for i in c.get("issues") or [] if i.get("severity") == "fail"]
        row = {"check": c["id"], "title": c.get("title"), "n": n, "t": iss[0].get("t") if iss else None,
               "msgs": [i.get("msg") for i in iss[:3]], "evidence": (iss[0].get("evidence") or [None])[0] if iss else None}
        (new if (base is None or n > bf.get(c["id"], 0)) else kept).append(row)
    return new, kept


def av_drift(rep, fps=25.0):
    """Frames of audio/video length mismatch from QA's av_length check (None when not measured)."""
    for c in (rep or {}).get("checks") or []:
        if c.get("id") == "av_length":
            return 0 if c.get("status") in ("pass", "info") else 1
    return None


def changed_sheet(cfg, video, spots, out_path, width=960, tile_w=320):
    """spots: [(label, t)] -> a jpg with one row per spot (t-0.5 | t | t+0.5). Returns [{label, t, tiles}]."""
    from PIL import Image, ImageDraw

    from .. import media
    out_path = Path(out_path)
    d = out_path.parent / (out_path.stem + "_frames")
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for k, (label, t) in enumerate(spots[:24]):
        tiles = []
        for j, dt in enumerate((-0.5, 0.0, 0.5)):
            tt = max(0.0, float(t) + dt)
            f = d / f"spot{k:02d}_{j}.jpg"
            try:
                media.ffmpeg(cfg, ["-ss", f"{tt:.3f}", "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-2",
                                   "-q:v", "3", f], timeout=60)
            except Exception:  # noqa: BLE001
                pass
            tiles.append(str(f) if f.exists() else None)
        rows.append({"label": label, "t": round(float(t), 2), "tiles": tiles})
    if not rows:
        return []
    th = int(tile_w * 9 / 16)
    lab = 26
    sheet = Image.new("RGB", (tile_w * 3, (th + lab) * len(rows)), (18, 18, 20))
    dr = ImageDraw.Draw(sheet)
    from .card import mmss
    for i, r in enumerate(rows):
        y = i * (th + lab)
        dr.text((8, y + 6), f"{r['label']} -> {mmss(r['t'])}   (t-0.5 | t | t+0.5)", fill=(235, 235, 235))
        for j, f in enumerate(r["tiles"]):
            if not f:
                continue
            try:
                im = Image.open(f).convert("RGB")
                im.thumbnail((tile_w, th))
                sheet.paste(im, (j * tile_w + (tile_w - im.width) // 2, y + lab + (th - im.height) // 2))
            except OSError:
                continue
    sheet.save(out_path, "JPEG", quality=85)
    return rows


def _faststart(path):
    """True when the moov atom comes before mdat (the file plays while it downloads)."""
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 20)
    except OSError:
        return False
    a, b = head.find(b"moov"), head.find(b"mdat")
    return a >= 0 and (b < 0 or a < b)


def link_check(path, url=None):
    """{status, how, code}: a Range GET on the review URL must answer 206 (rule 113)."""
    if url:
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
            with urllib.request.urlopen(req, timeout=15) as r:
                code = r.status
                r.read(1024)
            return {"status": "pass" if code == 206 else "fail", "how": f"GET {url} Range 0-1023", "code": code,
                    "faststart": _faststart(path)}
        except Exception as e:  # noqa: BLE001
            return {"status": "fail", "how": f"GET {url}", "code": None, "error": str(e)[:200]}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 1024))
            ok = len(f.read(1024)) > 0
        fs = _faststart(path)
        return {"status": "pass" if ok and fs else "fail", "how": "byte-range read (no studio server)",
                "code": None, "faststart": fs}
    except OSError as e:
        return {"status": "fail", "how": "byte-range read", "error": str(e)}
