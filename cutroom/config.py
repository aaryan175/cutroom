"""Project configuration.

A *project* is a work directory holding `cutroom.json`, the library database, proxies, contact
sheets, transcripts, render caches and outputs. The footage itself stays where it is (read-only);
the project only stores paths to it.

    <work>/cutroom.json        this config
    <work>/library.db          SQLite index (see db.py)
    <work>/proxies/            small H.264 copies for analysis + previews
    <work>/sheets/             contact sheets (JPEG) per clip, for visual review
    <work>/frames/             single frames pulled on demand
    <work>/transcripts/        ASR JSON per clip (word timestamps)
    <work>/voice/              cleaned dialogue WAVs per clip
    <work>/cache/              per-segment render cache (safe to delete)
    <work>/plans/              edit plans (JSON) written by the operator
    <work>/renders/            finished videos + QA reports
    <work>/notes/              review notes captured from the board
"""
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_NAME = "cutroom.json"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".mxf", ".avi", ".mts", ".m2ts", ".insv", ".lrv", ".webm"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}

SUBDIRS = ["proxies", "sheets", "frames", "transcripts", "voice", "cache", "plans", "renders", "notes", "music", "luts", "fonts"]


@dataclass
class Config:
    work: str = ""
    library_roots: list = field(default_factory=list)      # folders scanned for footage (read-only)
    music_roots: list = field(default_factory=list)        # folders scanned for songs
    proxy_height: int = 540                                # proxies are this tall
    proxy_crf: int = 28
    sheet_every_s: float = 2.0                             # one contact-sheet tile per N seconds (capped)
    sheet_max_tiles: int = 24
    asr_model: str = "medium"                              # faster-whisper model name
    asr_language: str = ""                                 # "" = auto-detect
    lut: str = ""                                          # optional .cube applied to log footage
    log_transfer_hint: list = field(default_factory=lambda: ["slog", "s-log", "log", "vlog", "clog", "flog", "hlg"])
    out_width: int = 1920
    out_height: int = 1080
    out_fps: float = 25.0
    caption_font: str = ""                                 # path to .ttf/.otf; "" = auto (Helvetica/Inter/DejaVu)
    caption_size: int = 54
    preview_height: int = 720
    workers: int = 0                                       # 0 = auto (cpu_count // 2)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    remote_render: str = ""                                # optional command prefix to run ffmpeg elsewhere
    # -- resource governor (governor.py, docs/OVERNIGHT.md). 0 / "" = automatic for this machine
    governor: bool = True                                  # false = no throttling (the single ASR slot stays)
    max_load: float = 0.0                                  # 1-min load above this throttles heavy work to 1 slot (auto cpus*0.8)
    min_free_ram_gb: float = 0.0                           # new work waits below this (auto max(2, 15% of RAM))
    max_proc_mem_gb: float = 0.0                           # per child process footprint cap (auto min(4, 25% of RAM))
    min_free_disk_gb: float = 20.0                         # writes pause below this much free space on the work volume
    heavy_slots: int = 0                                   # concurrent encodes/analysis machine-wide (auto cpus//4, >= 1)
    nice_level: int = 10                                   # scheduling priority of child processes
    pause_when_user_active: bool = False                   # macOS: 1 heavy slot while keyboard/mouse used in last 2 min
    quiet_hours: str = ""                                  # e.g. "09:00-18:00": 1 heavy slot inside this window
    # -- AI agent (agent.py): studio chat + overnight tagging, on the person's own login (no API keys)
    agent: str = "auto"                                    # "auto" (the logged-in one; Claude Code if both) | "claude" | "codex"
    codex_model: str = ""                                  # Codex model for tagging ("" = Codex default); chat: CUTROOM_CODEX_MODEL
    # -- overnight understanding (overnight.py)
    tag_batch: int = 8                                     # clips per tagging call (Codex: at most 8 images per call)
    tag_sleep_s: float = 20.0                              # pause between tagging calls
    tag_model: str = ""                                    # Claude model alias for tagging ("" = your default)
    tag_timeout_s: float = 900.0                           # one tagging call may take this long

    # -- paths ---------------------------------------------------------------------------------
    def p(self, *parts) -> Path:
        return Path(self.work).joinpath(*parts)

    @property
    def db_path(self) -> Path:
        return self.p("library.db")

    def n_workers(self) -> int:
        return self.workers or max(1, (os.cpu_count() or 2) // 2)

    # -- io ------------------------------------------------------------------------------------
    def save(self):
        Path(self.work).mkdir(parents=True, exist_ok=True)
        with open(self.p(CONFIG_NAME), "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, work=None) -> "Config":
        work = Path(work or os.environ.get("CUTROOM_WORK") or find_work())
        data = json.loads((work / CONFIG_NAME).read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        cfg.work = str(work.resolve())
        return cfg


def find_work(start=None) -> Path:
    """Walk up from cwd looking for cutroom.json; fall back to $CUTROOM_WORK."""
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CONFIG_NAME).exists():
            return d
    raise SystemExit("No cutroom project found. Run `cutroom init <work_dir> --library <footage_dir>` "
                     "or set CUTROOM_WORK.")


def init_project(work, library_roots, music_roots=()) -> Config:
    cfg = Config(work=str(Path(work).resolve()),
                 library_roots=[str(Path(r).expanduser().resolve()) for r in library_roots],
                 music_roots=[str(Path(r).expanduser().resolve()) for r in music_roots])
    for d in SUBDIRS:
        cfg.p(d).mkdir(parents=True, exist_ok=True)
    cfg.save()
    return cfg
