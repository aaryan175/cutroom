"""SQLite library index. One file, safe to rebuild; the footage is never modified.

Tables
  clips        one row per source file (id = stable short hash of the absolute path)
  shots        detected shot boundaries inside a clip
  metrics      per-window measurements (shake, exposure, sharpness, motion, faces)
  transcripts  ASR summary per clip (words live in <work>/transcripts/<id>.json)
  tags         visual/semantic tags written by the operator (Claude) after looking at sheets
  matchcuts    candidate match-cut pairs found by `cutroom matchcuts`
  songs        indexed music with tempo + beat grid path
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,           -- 10-char hash of absolute path
  path TEXT UNIQUE NOT NULL,     -- absolute path to the original (read-only)
  rel TEXT,                      -- path relative to its library root (for display)
  size INTEGER, mtime REAL,
  duration REAL, fps REAL, width INTEGER, height INTEGER,
  codec TEXT, pix_fmt TEXT, color_transfer TEXT, is_log INTEGER DEFAULT 0,
  rotation INTEGER DEFAULT 0,
  has_audio INTEGER DEFAULT 0, audio_channels INTEGER,
  shot_at TEXT,                  -- creation time from metadata (ISO) if present
  proxy TEXT, sheet TEXT,        -- paths inside the project
  status TEXT DEFAULT 'new',     -- new | probed | proxied | analyzed | error
  error TEXT,
  updated REAL
);
CREATE TABLE IF NOT EXISTS shots (
  clip_id TEXT, idx INTEGER, start REAL, end REAL,
  PRIMARY KEY (clip_id, idx)
);
CREATE TABLE IF NOT EXISTS metrics (
  clip_id TEXT, t0 REAL, t1 REAL,
  shake REAL,                    -- mean frame-to-frame change of camera velocity (px @ 480w); <3 steady, >8 shaky
  motion REAL,                   -- mean camera speed (px/frame @ 480w)
  exposure REAL,                 -- mean luma 0-255 (after LUT if log)
  contrast REAL,                 -- luma std
  sharpness REAL,                -- variance of Laplacian
  faces INTEGER,                 -- max faces detected in the window
  PRIMARY KEY (clip_id, t0)
);
CREATE TABLE IF NOT EXISTS transcripts (
  clip_id TEXT PRIMARY KEY, path TEXT, language TEXT,
  n_words INTEGER, speech_s REAL, text TEXT, model TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS tags (
  clip_id TEXT PRIMARY KEY,
  data TEXT,                     -- JSON object, see TAG_FIELDS
  updated REAL
);
CREATE TABLE IF NOT EXISTS matchcuts (
  a TEXT, ta REAL, b TEXT, tb REAL, score REAL, kind TEXT,
  PRIMARY KEY (a, ta, b, tb)
);
CREATE TABLE IF NOT EXISTS songs (
  id TEXT PRIMARY KEY, path TEXT UNIQUE, title TEXT, duration REAL,
  tempo REAL, beats_path TEXT, updated REAL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
"""

# The operator writes tags with these fields (all optional). Keep values short and concrete.
TAG_FIELDS = {
    "summary": "one sentence: what happens in the clip",
    "setting": "where: e.g. kitchen, street, beach, office, car, gym, restaurant",
    "time_of_day": "day | golden | night | indoor",
    "people": "who is on screen: e.g. 'creator', 'creator + friend', 'strangers', 'none'",
    "creator_visible": "true/false: is the main subject (the person the edits are about) in frame",
    "talking_to_camera": "true/false",
    "actions": "list of verbs: walking, driving, cooking, typing, laughing ...",
    "shot": "wide | medium | close | pov | drone | detail | timelapse",
    "camera": "locked | handheld | gimbal | moving-vehicle",
    "mood": "list: calm, hype, funny, cinematic, moody ...",
    "quality": "1-5 overall usability (focus, exposure, framing, shake)",
    "usable": "true/false; false = never use (strangers as subject, accidental, unflattering, broken)",
    "best": "list of {t: seconds, why: text} best moments",
    "avoid": "list of {t0, t1, why} ranges to never use",
    "topics": "list of keywords this clip can illustrate as b-roll",
    "notes": "free text",
}


def clip_id(path) -> str:
    return hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:10]


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def upsert(con, table, row: dict, key=("id",)):
    cols = list(row)
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
           f"ON CONFLICT({','.join(key)}) DO UPDATE SET " +
           ",".join(f"{c}=excluded.{c}" for c in cols if c not in key))
    con.execute(sql, [row[c] for c in cols])


def get_clip(con, ref) -> sqlite3.Row:
    """Look a clip up by id, absolute path, relative path, or unique filename stem."""
    r = con.execute("SELECT * FROM clips WHERE id=? OR path=? OR rel=?", (ref, ref, ref)).fetchone()
    if r:
        return r
    rows = con.execute("SELECT * FROM clips WHERE rel LIKE ? OR path LIKE ?", (f"%{ref}%", f"%{ref}%")).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise KeyError(f"no clip matches {ref!r}")
    raise KeyError(f"{ref!r} is ambiguous: {[x['rel'] for x in rows[:8]]}")


def get_tags(con, cid) -> dict:
    r = con.execute("SELECT data FROM tags WHERE clip_id=?", (cid,)).fetchone()
    return json.loads(r["data"]) if r else {}


def set_tags(con, cid, data: dict, merge=True):
    cur = get_tags(con, cid) if merge else {}
    cur.update(data)
    upsert(con, "tags", {"clip_id": cid, "data": json.dumps(cur), "updated": time.time()}, key=("clip_id",))
    con.commit()
    return cur
