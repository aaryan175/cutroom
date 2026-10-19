"""Synthesise a tiny footage library with ffmpeg lavfi and run the ingest + analyze pipeline once."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "64k", "-shortest"]
SRC = "testsrc2=size=1920x1080:rate=25:duration=8"
TONE = "sine=frequency=440:duration=8"


def _ff(*args):
    subprocess.run(FF + [str(a) for a in args], check=True)


def make_library(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    (root / "day").mkdir(exist_ok=True)
    p = {k: root / "day" / f"{k}.mp4" for k in ("steady", "shaky", "dark", "scenes")}
    p["vertical"] = root / "phone" / "vertical.MOV"
    p["vertical"].parent.mkdir(exist_ok=True)

    _ff("-f", "lavfi", "-i", SRC, "-f", "lavfi", "-i", TONE, *X264, *AAC, p["steady"])
    # hand-held look: an oversized frame cropped with jittering offsets, every frame differently
    _ff("-f", "lavfi", "-i", "testsrc2=size=2240x1260:rate=25:duration=8",
        "-vf", "crop=1920:1080:x='160+90*sin(n*1.9)+50*sin(n*4.3)':y='90+60*sin(n*2.7)+30*sin(n*5.1)'",
        *X264, p["shaky"])
    _ff("-f", "lavfi", "-i", SRC, "-vf", "lutrgb=r='val*0.12':g='val*0.12':b='val*0.12'", *X264, p["dark"])
    _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=3",
        "-f", "lavfi", "-i", "smptehdbars=size=1920x1080:rate=25:duration=3",
        "-f", "lavfi", "-i", "mandelbrot=size=1920x1080:rate=25",
        "-filter_complex", "[2:v]trim=duration=3,setpts=PTS-STARTPTS[m];[0:v][1:v][m]concat=n=3:v=1[v]",
        "-map", "[v]", *X264, p["scenes"])
    # vertical phone clip: landscape pixels + a 90° display matrix
    tmp = root / "phone" / "_tmp.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=4", "-f", "lavfi",
        "-i", "sine=frequency=300:duration=4", *X264, *AAC, tmp)
    _ff("-display_rotation", "90", "-i", tmp, "-c", "copy", p["vertical"])
    tmp.unlink()

    # junk that scan must ignore
    (root / "day" / "._steady.mp4").write_bytes(b"\0" * 200_000)
    (root / "day" / ".hidden.mp4").write_bytes(b"\0" * 200_000)
    (root / "day" / "tiny.mp4").write_bytes(b"\0" * 1000)
    (root / "day" / "notes.txt").write_text("not a video")
    os.symlink(root, root / "day" / "loop")         # directory loop must not hang the scan
    return p


@pytest.fixture(scope="session")
def project(tmp_path_factory):
    from cutroom import analyze, db, ingest
    from cutroom.config import init_project

    base = tmp_path_factory.mktemp("cutroom")
    clips = make_library(base / "footage")
    cfg = init_project(base / "work", [base / "footage"])
    cfg.workers = 3
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    ingest.make_sheets(cfg, con)
    analyze.analyze_all(cfg, con)
    ids = {k: db.clip_id(v) for k, v in clips.items()}
    return {"cfg": cfg, "con": con, "clips": clips, "ids": ids}
