"""Camera identity, log profile, colour-managed matching to the hero camera, and camera RAW handling.

    cutroom cameras                       every camera found: clips, days, log profile, confidence, transform
    cutroom cameras clips [--camera K]    per clip: camera, profile (metadata / assumed), evidence
    cutroom cameras set REF --profile P [--model M] [--family F] [--all]   correct one clip, or (--all) its camera
    cutroom cameras match [--matrix]      fit the per-camera, per-day correction toward the hero camera (ARRI)
    cutroom cameras luts [--out DIR]      write the per-camera .cube files (Premiere Lumetri "Input LUT")
    cutroom braw                          Blackmagic RAW / ARRIRAW clips and how each one is decoded

Identity (per clip, from metadata, never from folder names): ffprobe format + stream tags (QuickTime make /
model / camera identifier, Blackmagic `manufacturer` / `camera_type` / `camera_id` / `viewing_gamma`, the XAVC
brand), camera XML sidecars (Sony NonRealTimeMeta: Device manufacturer / modelName / serialNo,
CaptureGammaEquation, CaptureColorPrimaries), Blackmagic `.sidecar` files, brand strings in the file's own
header / trailer atoms, each brand's file naming, and resolution / codec fingerprints. Each source adds evidence
and a confidence; the strongest wins. A profile the metadata does not state is estimated from the picture
(a lifted, low-contrast, desaturated histogram = log) and stored as `assumed`.

Colour management (render.grade_chain via `grade_for`): every camera goes through ONE combined 3D LUT
    camera input transform -> the show transform (hero camera LogC3 -> Rec.709) -> the per-camera match
  * maths (colorsci.MATH_PROFILES: LogC3, S-Log3/S-Gamut3.Cine, F-Log2/F-Gamut): decode the published curve,
    convert the gamut to ARRI Wide Gamut 3, encode LogC3 EI 800, then the show LUT. Exactly the ARRI pipeline.
  * a LUT-only profile (Blackmagic Film Gen 5, LogC4, F-Log, ...): the manufacturer's log->Rec.709 LUT (the
    creator's `luts/<camera>.cube`, else one found in DaVinci Resolve's / Premiere's LUT folders). That gives
    Rec.709 directly; the statistical match then pulls it toward the hero.
  * show transform: `style.grade.camera_transforms.show_lut`, else `luts/arri_logc3.cube` (or arri.cube,
    show.cube), else ARRI's LogC-to-Rec709 LUT found in Resolve's LUT folder, else the colorimetric maths from
    ARRI's white paper (flagged: not ARRI's look).
  * match: per camera per shoot day, frames of that camera and of the hero at the same moments (sync groups,
    else capture time corrected by the measured clock offsets) give a display-linear 3x3 (white balance,
    exposure, saturation; optionally a Monge-Kantorovich 3x3). Stored in `camera_match`, shown in the Looks
    panel as before/after, checked by QA `camera_match` (CIEDE2000 between angles of a multicam group).
The house doctrine stays: one transform (this combined LUT) plus at most one look. Nothing else is stacked.

RAW: `.braw` (Blackmagic RAW) and ARRIRAW (`.ari` / `.arx` sequences, ARRIRAW `.mxf`) can't be decoded by
ffmpeg. They are registered with status `raw` and decoded through a stand-in (a ProRes / H.264 rendition of
the same clip): `<work>/raw_proxies/<stem>.(mov|mp4|mxf)` or a `Proxy/<stem>.*` folder beside the clip (what
Blackmagic Proxy Generator and Resolve write). `cutroom braw` lists what is missing; docs explain how to make
them (Resolve render queue script tools/resolve_braw_proxies.py, Proxy Generator, or in-camera proxies).
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np

from . import colorsci as CS
from . import db, media

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_camera (
  clip_id TEXT PRIMARY KEY, cam_key TEXT, family TEXT, make TEXT, model TEXT, serial TEXT,
  log_profile TEXT, profile_src TEXT,          -- metadata | filename | assumed | user
  confidence REAL, raw TEXT,                   -- braw | arriraw | NULL
  decode_path TEXT,                            -- stand-in rendition for RAW clips (NULL = the master decodes)
  evidence TEXT, user_set INTEGER DEFAULT 0, updated REAL
);
CREATE INDEX IF NOT EXISTS clip_camera_key ON clip_camera(cam_key);
CREATE TABLE IF NOT EXISTS camera_overrides (
  scope TEXT, key TEXT, family TEXT, model TEXT, log_profile TEXT, updated REAL,
  PRIMARY KEY (scope, key)                     -- scope: clip (key = clip id) | camera (key = cam_key)
);
CREATE TABLE IF NOT EXISTS camera_match (
  cam_key TEXT, day TEXT, data TEXT, updated REAL, PRIMARY KEY (cam_key, day)
);
"""

FAMILIES = {"arri": "ARRI", "bmd": "Blackmagic Design", "fuji": "Fujifilm", "sony": "Sony", "other": "Other"}
FAMILY_LOG = {"arri": "arri_logc3", "bmd": "bmd_film_gen5", "fuji": "fuji_flog2", "sony": "sony_slog3"}
RAW_EXTS = {".braw": "braw", ".ari": "arriraw", ".arx": "arriraw"}
STANDIN_EXTS = (".mov", ".mp4", ".mxf", ".m4v")
HERO_DEFAULT = "arri"

_BRANDS = [  # (family, regex over tag text / file bytes / sidecars)
    ("arri", re.compile(r"(?<![a-z])(arri|alexa|amira)(?![a-z])", re.I)),
    ("bmd", re.compile(r"blackmagic", re.I)),
    ("fuji", re.compile(r"fujifilm", re.I)),
    ("sony", re.compile(r"(?<![a-z])sony(?![a-z])", re.I)),
]
# brand naming (camera-written names; renamed files simply don't match)
_NAMES = [
    ("arri", re.compile(r"^[A-Z]\d{3}C\d{3}_\d{6}_[A-Z0-9]{4}", re.I), 0.6),      # A001C003_200614_R1AB
    ("bmd", re.compile(r"^[A-Z]\d{3}_\d{8}_C\d{3}", re.I), 0.6),                    # A001_06141530_C003
    ("fuji", re.compile(r"^DSCF\d{4}$", re.I), 0.45),                                  # DSCF1234
    ("sony", re.compile(r"^C\d{4}$", re.I), 0.35),                                     # C0001 (XAVC S)
]
# resolution / codec fingerprints: (family, model, (w, h) set, codecs or None, confidence)
_FINGERPRINTS = [
    ("bmd", "Pocket Cinema Camera 6K Pro", {(6144, 3456), (6144, 2560), (5744, 3024), (2868, 1512)}, None, 0.35),
    ("fuji", "X-T5", {(6240, 4160), (6240, 3510), (6240, 2632)}, None, 0.35),
    ("arri", "AMIRA", {(3200, 1800)}, {"prores"}, 0.3),
]


def ensure_schema(con):
    con.executescript(SCHEMA)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def cam_key(family, model=None, serial=None):
    """Stable camera key: family-model (two identical bodies share a key; the serial tells them apart in sync)."""
    fam = family or "other"
    m = _slug(model)
    for pre in ("blackmagic-", "fujifilm-", "sony-", "arri-"):
        if m.startswith(pre):
            m = m[len(pre):]
    return f"{fam}-{m}" if m else fam


# ================================================================================================ identity
def _tags(info):
    fmt = info.get("format", {}) or {}
    t = {k.lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    for s in info.get("streams", []) or []:
        for k, v in (s.get("tags") or {}).items():
            t.setdefault(k.lower(), str(v))
    return t


def _video(info):
    return next((s for s in info.get("streams", []) or [] if s.get("codec_type") == "video"
                 and not (s.get("disposition") or {}).get("attached_pic")), None)


def _file_bytes_brand(path, n=256 * 1024):
    """Brand strings in the first and last `n` bytes (QuickTime udta / XMP / maker atoms)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(n)
            tail = b""
            if size > n:
                f.seek(max(n, size - n))
                tail = f.read(n)
    except OSError:
        return {}
    # only printable runs of 6+ characters: brand words inside compressed picture data are chance, not metadata
    txt = "\n".join(m.group(0).decode("ascii") for m in re.finditer(rb"[ -~]{6,}", head + b"\n" + tail))
    found = {}
    for fam, rx in _BRANDS:
        m = rx.search(txt)
        if m:
            found[fam] = m.group(0)
    for rx, fam, model in ((r"X-T5", "fuji", "X-T5"), (r"Pocket Cinema Camera 6K Pro", "bmd", "Pocket Cinema Camera 6K Pro"),
                           (r"AMIRA", "arri", "AMIRA"), (r"ILCE-[0-9A-Z]+|FX[0-9]{1,2}\b|ILME-[0-9A-Z]+", "sony", None)):
        m = re.search(rx, txt)
        if m:
            found.setdefault(f"{fam}_model", model or m.group(0))
    return found


def _sidecar_text(path):
    from .ingest import _sidecars
    out = []
    for sc in _sidecars(str(path)):
        try:
            if os.path.getsize(sc) <= 512 * 1024:
                out.append((sc, Path(sc).read_text(errors="ignore")))
        except OSError:
            continue
    return out


def raw_kind(path, info=None):
    ext = os.path.splitext(str(path))[1].lower()
    if ext in RAW_EXTS:
        return RAW_EXTS[ext]
    v = _video(info or {}) or {}
    tag = str(v.get("codec_tag_string") or "").lower()
    if ext == ".mxf" and (v.get("codec_name") in (None, "unknown", "none")) and re.search(r"ari|arri", tag + json.dumps(_tags(info or {})), re.I):
        return "arriraw"
    if v and v.get("codec_name") in (None, "unknown") and tag.startswith("br"):
        return "braw"
    return None


def _profile_from_text(txt):
    t = (txt or "").lower()
    if re.search(r"s-?log3", t):
        return "sony_slog3"
    if re.search(r"f-?log2", t):
        return "fuji_flog2"
    if re.search(r"f-?log(?!2)", t):
        return "fuji_flog"
    if re.search(r"log ?c ?4|logc4", t):
        return "arri_logc4"
    if re.search(r"log ?c ?3|logc3|log ?c(?![a-z0-9])", t):
        return "arri_logc3"
    if re.search(r"hlg|hybrid log|arib-std-b67|rec2100-hlg", t):
        return "hlg"
    return None


def identify(cfg, path, info=None, rel=None) -> dict:
    """Camera identity + log profile of one file from its metadata. No picture decoding here (see estimate_profile)."""
    info = info if info is not None else media.probe(cfg, path)
    tags = _tags(info)
    v = _video(info) or {}
    ev, score = [], {}
    make = model = serial = None
    profile, psrc = None, None

    def vote(fam, conf, why):
        score[fam] = max(score.get(fam, 0.0), conf)
        ev.append(f"{fam}: {why} ({conf:.2f})")

    # 1. tags
    mk = tags.get("com.apple.quicktime.make") or tags.get("make") or tags.get("manufacturer")
    md = tags.get("com.apple.quicktime.model") or tags.get("model") or tags.get("camera_type")
    sn = tags.get("com.apple.quicktime.camera.identifier") or tags.get("camera_id") or tags.get("serial") \
        or tags.get("serial_number")
    tag_text = " ".join(f"{k}={v_}" for k, v_ in tags.items())
    for fam, rx in _BRANDS:
        if mk and rx.search(mk):
            vote(fam, 0.95, f"make tag {mk!r}")
        elif md and rx.search(md):
            vote(fam, 0.9, f"model tag {md!r}")
        elif rx.search(tag_text):
            vote(fam, 0.8, "brand in metadata tags")
    if (tags.get("major_brand") or "").upper().startswith("XAVC"):
        vote("sony", 0.9, "XAVC brand")
    if mk or md:
        make, model = mk, md
    if sn:
        serial = sn
    for k in ("viewing_gamma", "com.apple.quicktime.camera.gamma", "gamma", "transfer_characteristics", "comment",
              "description", "encoder"):
        if tags.get(k) and not profile:
            vg = tags[k]
            if k == "viewing_gamma" and "film" in vg.lower() and "blackmagic" in vg.lower():
                profile, psrc = "bmd_film_gen5", "metadata"
                ev.append(f"profile: viewing_gamma {vg!r}")
            elif k == "viewing_gamma" and re.search(r"video", vg, re.I):
                profile, psrc = "rec709", "metadata"
                ev.append(f"profile: viewing_gamma {vg!r}")
            else:
                p = _profile_from_text(vg)
                if p:
                    profile, psrc = p, "metadata"
                    ev.append(f"profile: tag {k}={vg!r}")
    trc = (v.get("color_transfer") or "").lower()
    if not profile and trc == "arib-std-b67":
        profile, psrc = "hlg", "metadata"
        ev.append("profile: transfer arib-std-b67 (HLG)")
    # 2. sidecars (Sony XML, Blackmagic .sidecar)
    for sc, txt in _sidecar_text(path):
        m = re.search(r'<Device[^>]*manufacturer="([^"]+)"[^>]*modelName="([^"]+)"(?:[^>]*serialNo="([^"]+)")?', txt)
        if m:
            fam = next((f for f, rx in _BRANDS if rx.search(m.group(1))), None)
            if fam:
                vote(fam, 0.95, f"sidecar {Path(sc).name} Device")
            make, model = make or m.group(1), model or m.group(2)
            serial = serial or m.group(3)
        g = re.search(r'name="CaptureGammaEquation"\s+value="([^"]+)"', txt)
        prim = re.search(r'name="CaptureColorPrimaries"\s+value="([^"]+)"', txt)
        if g:
            gv = g.group(1).lower()
            pv = (prim.group(1) if prim else "").lower()
            p = _profile_from_text(gv)
            if p == "sony_slog3" and pv and "cine" not in pv and "cine" not in gv:
                p = "sony_slog3_sg3"                     # S-Gamut3 (not .Cine): LUT-only here
            if p:
                profile, psrc = p, "metadata"
            elif re.search(r"rec709|bt709|cine[1-4]|still|s-cinetone", gv):
                profile, psrc = "rec709", "metadata"
            ev.append(f"profile: sidecar CaptureGammaEquation={g.group(1)!r}" + (f" primaries={prim.group(1)!r}" if prim else ""))
        if sc.lower().endswith(".sidecar"):
            vote("bmd", 0.7, f"Blackmagic sidecar {Path(sc).name}")
            gm = re.search(r'"?gamma"?\s*[:=]\s*"([^"]+)"', txt, re.I)
            if gm and not profile:
                p = "bmd_film_gen5" if "film" in gm.group(1).lower() else (_profile_from_text(gm.group(1)) or None)
                if p:
                    profile, psrc = p, "metadata"
                    ev.append(f"profile: .sidecar gamma {gm.group(1)!r}")
    # 3. raw containers
    raw = raw_kind(path, info)
    if raw == "braw":
        vote("bmd", 0.9, "Blackmagic RAW file")
    elif raw == "arriraw":
        vote("arri", 0.9, "ARRIRAW file")
    # 4. file bytes (only when the tags didn't settle it: a cheap read, but on 50k clips every MB counts)
    fb = {}
    if not score or max(score.values()) < 0.9 or not (model or md):
        fb = _file_bytes_brand(path)
    for fam in ("arri", "bmd", "fuji", "sony"):
        if fam in fb:
            vote(fam, 0.75, f"{fb[fam]!r} in the file header/trailer")
    # 5. naming
    stem = Path(str(path)).stem
    for fam, rx, conf in _NAMES:
        if rx.search(stem):
            vote(fam, conf, f"{FAMILIES[fam]} file naming {stem!r}")
    # 6. resolution / codec
    wh = (int(v.get("width") or 0), int(v.get("height") or 0))
    for fam, mdl, sizes, codecs, conf in _FINGERPRINTS:
        if wh in sizes and (codecs is None or (v.get("codec_name") or "") in codecs):
            vote(fam, conf, f"{wh[0]}x{wh[1]} frame size of the {mdl}")
    family = max(score, key=score.get) if score else "other"
    conf = score.get(family, 0.0)
    # several independent pieces of evidence for the same family raise the confidence a little
    n_same = sum(1 for e in ev if e.startswith(family + ":"))
    if n_same >= 2:
        conf = min(0.99, conf + 0.04 * (n_same - 1))
    if not model:
        model = fb.get(f"{family}_model") or next((m for f, m, s, c, _ in _FINGERPRINTS if f == family and wh in s), None)
    if not make and family != "other":
        make = FAMILIES[family]
    if not profile:
        p = _profile_from_text(os.path.basename(str(path)))
        if p:
            profile, psrc = p, "filename"
            ev.append(f"profile: file name {os.path.basename(str(path))!r}")
    return {"family": family, "make": (make or None), "model": (str(model).strip()[:80] if model else None),
            "serial": (str(serial).strip()[:80] if serial else None), "log_profile": profile, "profile_src": psrc,
            "confidence": round(float(conf), 3), "raw": raw, "evidence": ev,
            "cam_key": cam_key(family, model)}


# ================================================================================================ picture estimate
def read_frames(cfg, path, times, width=256, in_range="tv") -> list:
    """RGB float frames (0-1, full range after the usual legal-range expansion, BT.709 matrix) at `times`."""
    out = []
    for t in times:
        cmd = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
               "-frames:v", "1", "-vf", f"scale={width}:-2:flags=area:in_range={in_range}:out_range=full:in_color_matrix=bt709,"
               "format=rgb48le", "-f", "rawvideo", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode or not r.stdout:
            continue
        a = np.frombuffer(r.stdout, np.uint16).astype(np.float64) / 65535.0
        h = len(a) // 3 // width
        if h:
            out.append(a[: h * width * 3].reshape(h, width, 3))
    return out


def log_signature(frames) -> dict:
    """Numbers that separate log from display pictures: black point, highlight point, contrast, saturation."""
    if not frames:
        return {}
    a = np.concatenate([f.reshape(-1, 3) for f in frames])
    y = np.einsum("...j,j->...", a, CS.LUMA709)
    ch = a.max(1) - a.min(1)
    return {"p1": round(float(np.percentile(y, 1)), 4), "p99": round(float(np.percentile(y, 99)), 4),
            "std": round(float(y.std()), 4), "sat": round(float(np.mean(ch)), 4)}


def looks_log(sig) -> bool | None:
    """True = log signature (lifted blacks, no highlight above ~85 %, flat, desaturated); False = display; None = unsure."""
    if not sig:
        return None
    if sig["p1"] >= 0.06 and sig["p99"] <= 0.86 and sig["sat"] <= 0.3:
        return True
    if sig["p1"] <= 0.035 or sig["p99"] >= 0.93 or sig["sat"] >= 0.4:
        return False
    return None


def estimate_profile(cfg, path, duration, family, in_range="tv") -> tuple:
    """(profile, evidence) from the picture histogram; the profile is always 'assumed'."""
    d = float(duration or 0)
    times = [d * k for k in (0.2, 0.5, 0.8)] if d > 1 else [0.0]
    sig = log_signature(read_frames(cfg, path, times, width=192, in_range=in_range))
    lg = looks_log(sig)
    why = f"histogram p1 {sig.get('p1')} p99 {sig.get('p99')} sat {sig.get('sat')}" if sig else "no frame decoded"
    if lg is True:
        return FAMILY_LOG.get(family, "log_unknown"), why + " -> log"
    if lg is False:
        return "rec709", why + " -> display (Rec.709)"
    return (FAMILY_LOG.get(family) if family in FAMILY_LOG else None), why + " -> unsure"


IS_LOG = set(CS.MATH_PROFILES) | set(CS.LUT_ONLY_PROFILES)


# ================================================================================================ store
def _override(con, cid, key):
    ensure_schema(con)
    o = con.execute("SELECT * FROM camera_overrides WHERE scope='clip' AND key=?", (cid,)).fetchone()
    c = con.execute("SELECT * FROM camera_overrides WHERE scope='camera' AND key=?", (key,)).fetchone()
    res = {}
    for r in (c, o):                                     # the clip's own correction wins over its camera's
        if r:
            for k in ("family", "model", "log_profile"):
                if r[k]:
                    res[k] = r[k]
    return res


def analyze(cfg, row, info=None, estimate=True) -> dict:
    """Identity + profile of one clip without touching the database (safe in worker threads): metadata first, then
    the picture histogram when the profile is still unknown. `row` needs path (and duration, pix_fmt, rel)."""
    g = (lambda k: row[k] if k in row.keys() else None) if hasattr(row, "keys") else (lambda k: row.get(k))
    path = g("path")
    try:
        ident = identify(cfg, path, info=info, rel=g("rel"))
    except Exception as e:  # noqa: BLE001 - identity is best-effort, never breaks indexing
        ident = {"family": "other", "make": None, "model": None, "serial": None, "log_profile": None, "profile_src": None,
                 "confidence": 0.0, "raw": raw_kind(path), "evidence": [f"identify failed: {e}"], "cam_key": "other"}
    ident["decode_path"] = standin(cfg, path) if ident.get("raw") else None
    if estimate and not ident["log_profile"]:
        src = ident["decode_path"] or (None if ident.get("raw") else path)
        if src and (ident["family"] != "other" or _high_bit(g("pix_fmt"))):
            try:
                p, why = estimate_profile(cfg, src, g("duration") or 0, ident["family"], in_range=_range(None, info))
            except Exception as e:  # noqa: BLE001
                p, why = None, f"histogram failed: {e}"
            ident["evidence"].append("profile: " + why)
            if p:
                ident["log_profile"], ident["profile_src"] = p, "assumed"
        elif ident.get("raw") == "braw":
            ident["log_profile"], ident["profile_src"] = "bmd_film_gen5", "assumed"
            ident["evidence"].append("profile: Blackmagic RAW is scene-referred; decoded as Blackmagic Film Gen 5")
        elif ident.get("raw") == "arriraw":
            ident["log_profile"], ident["profile_src"] = "arri_logc3", "assumed"
    return ident


def store(con, cid, ident) -> dict:
    """Write one clip's identity (applying the creator's corrections) and keep clips.is_log in step."""
    ensure_schema(con)
    ident = dict(ident)
    ident["evidence"] = list(ident.get("evidence") or [])
    ov = _override(con, cid, ident["cam_key"])
    user = 0
    if ov:
        user = 1
        ident.update({k: v for k, v in ov.items() if k in ("family", "model")})
        if ov.get("log_profile"):
            ident["log_profile"], ident["profile_src"] = ov["log_profile"], "user"
        ident["cam_key"] = cam_key(ident["family"], ident["model"])
        ident["evidence"].append("corrected by the creator")
    db.upsert(con, "clip_camera", {"clip_id": cid, "cam_key": ident["cam_key"], "family": ident["family"],
                                   "make": ident["make"], "model": ident["model"], "serial": ident["serial"],
                                   "log_profile": ident["log_profile"], "profile_src": ident["profile_src"],
                                   "confidence": ident["confidence"], "raw": ident.get("raw"),
                                   "decode_path": ident.get("decode_path"),
                                   "evidence": json.dumps(ident["evidence"][:30]), "user_set": user,
                                   "updated": time.time()}, key=("clip_id",))
    if ident["log_profile"] in IS_LOG:
        con.execute("UPDATE clips SET is_log=1 WHERE id=?", (cid,))
    elif ident["log_profile"] == "rec709" and ident["profile_src"] in ("metadata", "user"):
        con.execute("UPDATE clips SET is_log=0 WHERE id=?", (cid,))
    return ident


def record(cfg, con, row, info=None, estimate=True, echo=None) -> dict:
    """Identify one clip and store it (clip_camera). Applies the creator's corrections. Returns the stored dict."""
    return store(con, row["id"], analyze(cfg, row, info=info, estimate=estimate))


def _high_bit(pix_fmt):
    return bool(re.search(r"(10|12|16)(le|be)", str(pix_fmt or "")))


def _range(row, info):
    v = _video(info or {}) or {}
    return "full" if v.get("color_range") in ("pc", "jpeg") else "tv"


def get(con, cid) -> dict | None:
    ensure_schema(con)
    r = con.execute("SELECT * FROM clip_camera WHERE clip_id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["evidence"] = json.loads(d.get("evidence") or "[]")
    except ValueError:
        d["evidence"] = []
    return d


def all_clips(con) -> dict:
    ensure_schema(con)
    return {r["clip_id"]: dict(r) for r in con.execute("SELECT * FROM clip_camera")}


def badges(con) -> dict:
    """clip id -> {key, label, profile, assumed} for the Studio library."""
    out = {}
    for cid, r in all_clips(con).items():
        out[cid] = {"key": r["cam_key"], "family": r["family"], "label": short_label(r), "profile": r["log_profile"],
                    "assumed": r["profile_src"] in ("assumed", None) or (r["confidence"] or 0) < 0.6,
                    "raw": r["raw"]}
    return out


def short_label(r) -> str:
    fam = {"arri": "ARRI", "bmd": "BMD", "fuji": "Fuji", "sony": "Sony"}.get(r.get("family") or "", "Cam")
    prof = {"arri_logc3": "LogC3", "arri_logc4": "LogC4", "bmd_film_gen5": "Film", "fuji_flog2": "F-Log2",
            "fuji_flog": "F-Log", "sony_slog3": "S-Log3", "sony_slog3_sg3": "S-Log3", "rec709": "709", "hlg": "HLG",
            "log_unknown": "log"}.get(r.get("log_profile") or "", "")
    return f"{fam} {prof}".strip()


def cameras(con) -> list:
    """Every camera: key, family, model, clips, days, profiles (with how they were found), raw clips."""
    ensure_schema(con)
    rows = con.execute("""SELECT k.*, c.shot_at, c.fps, c.width, c.height, c.duration FROM clip_camera k
                          JOIN clips c ON c.id=k.clip_id WHERE COALESCE(c.width,0) > 0""").fetchall()
    cams = {}
    for r in rows:
        d = cams.setdefault(r["cam_key"], {"key": r["cam_key"], "family": r["family"], "make": r["make"],
                                           "model": r["model"], "clips": 0, "seconds": 0.0, "days": set(),
                                           "profiles": {}, "raw": 0, "raw_missing": 0, "serials": set(),
                                           "slowmo": 0, "confidence": []})
        d["clips"] += 1
        d["seconds"] += float(r["duration"] or 0)
        d["days"].add((r["shot_at"] or "")[:10] or "unknown")
        k = f"{r['log_profile'] or 'unknown'} ({r['profile_src'] or '-'})"
        d["profiles"][k] = d["profiles"].get(k, 0) + 1
        d["raw"] += int(bool(r["raw"]))
        d["raw_missing"] += int(bool(r["raw"]) and not r["decode_path"])
        if r["serial"]:
            d["serials"].add(r["serial"])
        if float(r["fps"] or 0) >= 47:
            d["slowmo"] += 1
        d["confidence"].append(float(r["confidence"] or 0))
    out = []
    for d in cams.values():
        d["days"] = sorted(d["days"])
        d["serials"] = sorted(d["serials"])
        d["confidence"] = round(float(np.median(d["confidence"])), 2) if d["confidence"] else 0.0
        d["label"] = f"{FAMILIES.get(d['family'], d['family'])} {d['model'] or ''}".strip()
        out.append(d)
    return sorted(out, key=lambda d: (d["family"] != HERO_DEFAULT, -d["clips"]))


def set_override(con, ref, family=None, model=None, profile=None, scope="clip") -> list:
    """The creator corrects a clip (scope clip) or every clip of its camera (scope camera). Returns clip ids touched."""
    ensure_schema(con)
    if profile and profile not in CS.ALL_PROFILES:
        raise ValueError(f"unknown profile {profile!r}; one of {', '.join(CS.ALL_PROFILES)}")
    if family and family not in FAMILIES:
        raise ValueError(f"unknown camera family {family!r}; one of {', '.join(FAMILIES)}")
    row = db.get_clip(con, ref) if scope == "clip" else None
    if scope == "clip":
        key = row["id"]
    else:
        r = con.execute("SELECT cam_key FROM clip_camera WHERE clip_id=? OR cam_key=?", (ref, ref)).fetchone()
        if not r:
            try:
                r = con.execute("SELECT cam_key FROM clip_camera WHERE clip_id=?", (db.get_clip(con, ref)["id"],)).fetchone()
            except KeyError:
                r = None
        if not r:
            raise KeyError(f"no camera or clip {ref!r} (cutroom cameras)")
        key = r["cam_key"]
    db.upsert(con, "camera_overrides", {"scope": scope, "key": key, "family": family, "model": model,
                                        "log_profile": profile, "updated": time.time()}, key=("scope", "key"))
    if scope == "clip":
        ids = [key]
    else:
        ids = [r[0] for r in con.execute("SELECT clip_id FROM clip_camera WHERE cam_key=?", (key,))]
    for cid in ids:
        cur = get(con, cid) or {}
        upd = {"user_set": 1, "updated": time.time()}
        if family:
            upd["family"] = family
        if model is not None:
            upd["model"] = model or None
        if profile:
            upd.update(log_profile=profile, profile_src="user")
        fam, mdl = upd.get("family", cur.get("family")), upd.get("model", cur.get("model"))
        upd["cam_key"] = cam_key(fam, mdl)
        con.execute(f"UPDATE clip_camera SET {','.join(f'{k}=?' for k in upd)} WHERE clip_id=?", [*upd.values(), cid])
        if profile:
            con.execute("UPDATE clips SET is_log=? WHERE id=?", (int(profile in IS_LOG), cid))
    con.commit()
    return ids


def identify_all(cfg, con, force=False, echo=print) -> dict:
    """Identify every video clip not identified yet (or all with force). Resumable."""
    ensure_schema(con)
    have = set() if force else {r[0] for r in con.execute("SELECT clip_id FROM clip_camera")}
    rows = [r for r in con.execute("SELECT * FROM clips WHERE COALESCE(width,0) > 0 OR status='raw' ORDER BY rel")
            if r["id"] not in have]
    n = 0
    for r in rows:
        record(cfg, con, r, echo=echo)
        n += 1
        if n % 100 == 0:
            con.commit()
            if echo:
                echo(f"  cameras {n}/{len(rows)}")
    con.commit()
    return {"identified": n}


# ================================================================================================ RAW stand-ins
def standin(cfg, path) -> str | None:
    """A decodable rendition of a RAW clip: <work>/raw_proxies/<stem>.*, else <clip dir>/Proxy/<stem>.*."""
    stem = Path(str(path)).stem
    cands = [Path(cfg.work) / "raw_proxies", Path(str(path)).parent / "Proxy", Path(str(path)).parent / "proxy"]
    for d in cands:
        for ext in STANDIN_EXTS:
            for nm in (stem + ext, stem + ext.upper(), stem + "_proxy" + ext, stem + "_Proxy" + ext):
                p = d / nm
                if p.is_file() and p.stat().st_size > 0:
                    return str(p)
    return None


def decode_path(con, row) -> str:
    """The file ffmpeg decodes for this clip's picture (the master, or a RAW clip's stand-in)."""
    try:
        r = con.execute("SELECT decode_path FROM clip_camera WHERE clip_id=?", (row["id"],)).fetchone()
    except Exception:  # noqa: BLE001 - no table yet
        r = None
    return (r[0] if r and r[0] and os.path.exists(r[0]) else None) or row["path"]


def braw_sdk_status() -> dict:
    """What this machine has for decoding Blackmagic RAW. The runtime framework ships inside Blackmagic apps; building
    a decoder needs the Blackmagic RAW SDK headers (BlackmagicRawAPI.h, a free download that sits behind a
    registration form), which is why no decoder is built here unless they are installed."""
    roots = ["/Applications/Blackmagic RAW", "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Frameworks",
             "/Library/Frameworks", "/Applications/Blackmagic RAW/Blackmagic RAW SDK"]
    fw, hdr = [], []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for dp, dn, fn in os.walk(r):
            if dp.count(os.sep) - r.count(os.sep) > 5:
                dn[:] = []
                continue
            for d in list(dn):
                if d == "BlackmagicRawAPI.framework":
                    fw.append(os.path.join(dp, d))
            for f in fn:
                if f == "BlackmagicRawAPI.h":
                    hdr.append(os.path.join(dp, f))
    resolve = "/Applications/DaVinci Resolve/DaVinci Resolve.app"
    helper = Path(__file__).resolve().parents[1] / "tools" / "brawdecode"
    return {"framework": fw[:4], "headers": hdr[:2], "resolve": os.path.isdir(resolve),
            "proxy_generator": os.path.isdir("/Applications/Blackmagic Proxy Generator Lite.app")
            or os.path.isdir("/Applications/Blackmagic Proxy Generator.app"),
            "helper": str(helper) if helper.exists() else None,
            "decoder": bool(helper.exists())}


def raw_report(cfg, con) -> dict:
    ensure_schema(con)
    rows = con.execute("SELECT k.clip_id, k.raw, k.decode_path, c.rel, c.path, c.duration FROM clip_camera k JOIN clips c "
                       "ON c.id=k.clip_id WHERE k.raw IS NOT NULL ORDER BY c.rel").fetchall()
    out = {"braw": [], "arriraw": [], "sdk": braw_sdk_status()}
    for r in rows:
        sd = standin(cfg, r["path"])
        if sd != r["decode_path"]:
            con.execute("UPDATE clip_camera SET decode_path=? WHERE clip_id=?", (sd, r["clip_id"]))
            if sd:                                     # a stand-in appeared: index the clip through it
                con.execute("UPDATE clips SET status='new', error=NULL WHERE id=? AND status IN ('raw','error')",
                            (r["clip_id"],))
        out[r["raw"]].append({"clip": r["clip_id"], "rel": r["rel"], "standin": sd, "duration": r["duration"]})
    con.commit()
    return out


# ================================================================================================ transforms
RESOLVE_LUT_DIRS = ["/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
                    os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"),
                    "C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/LUT", "/opt/resolve/LUT"]
VENDOR_LUTS = {   # profile (or "show") -> relative paths inside a DaVinci Resolve LUT folder (the vendors' own LUTs)
    "show": ["Arri/Arri Alexa LogC to Rec709.dat"],
    "bmd_film_gen5": ["Blackmagic Design/Blackmagic Gen 5 Film to Video.cube"],
    "sony_slog3": ["Sony/SLog3SGamut3.CineToLC-709.cube"],
}
SHOW_NAMES = ("arri_logc3.cube", "arri_logc3_to_rec709.cube", "arri.cube", "show.cube")


def reserved_lut_names() -> set:
    """LUT file names in <work>/luts/ that are camera transforms, not creative looks (looks.py hides them)."""
    names = set(SHOW_NAMES)
    for k in list(FAMILIES) + list(CS.ALL_PROFILES) + ["bmpcc6k", "bmpcc6kpro", "amira", "xt5", "x-t5"]:
        names.add(f"{k}.cube")
    return names


def vendor_lut(profile) -> str | None:
    for d in RESOLVE_LUT_DIRS:
        for rel in VENDOR_LUTS.get(profile, []):
            p = Path(d) / rel
            if p.is_file():
                return str(p)
    if profile == "show":
        import glob
        for pat in ("/Applications/Adobe Premiere Pro*/*.app/Contents/Lumetri/LUTs/Technical/*LogC*Rec709*.cube",
                    "/Applications/Adobe Premiere Pro*/*.app/Contents/Lumetri/LUTs/Technical/*LogC*709*.cube"):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[0]
    return None


def ct_settings(style) -> dict:
    """style.grade.camera_transforms -> {mode: auto|off, show_lut, hero, match, matrix, cameras{key: {lut, profile}}}."""
    v = ((style or {}).get("grade") or {}).get("camera_transforms", "auto")
    if v in (None, "", "auto", True):
        v = {}
    if v in ("off", False):
        return {"mode": "off"}
    if not isinstance(v, dict):
        return {"mode": "off"}
    return {"mode": v.get("mode", "auto"), "show_lut": v.get("show_lut") or "", "hero": v.get("hero") or HERO_DEFAULT,
            "match": v.get("match", True) is not False, "matrix": bool(v.get("matrix", False)),
            "cameras": v.get("cameras") or {}}


def validate(style) -> list:
    v = ((style or {}).get("grade") or {}).get("camera_transforms", "auto")
    if v in (None, "", "auto", "off", True, False):
        return []
    if not isinstance(v, dict):
        return ["style.grade.camera_transforms must be \"auto\", \"off\" or an object"]
    errs = []
    if v.get("mode") not in (None, "auto", "off"):
        errs.append("style.grade.camera_transforms.mode must be auto or off")
    if v.get("hero") not in (None, *FAMILIES):
        errs.append(f"style.grade.camera_transforms.hero must be one of {', '.join(FAMILIES)}")
    for k, c in (v.get("cameras") or {}).items():
        if not isinstance(c, dict):
            errs.append(f"style.grade.camera_transforms.cameras.{k} must be an object")
        elif c.get("profile") and c["profile"] not in CS.ALL_PROFILES:
            errs.append(f"style.grade.camera_transforms.cameras.{k}.profile: unknown {c['profile']!r}")
    return errs


def _user_lut(cfg, names):
    d = Path(cfg.work) / "luts"
    for n in names:
        if not n:
            continue
        p = Path(os.path.expanduser(n))
        if not p.is_absolute():
            p = d / n
        if p.is_file():
            return str(p)
    return None


def show_transform(cfg, st) -> dict:
    if st.get("show_lut"):
        p = _user_lut(cfg, [st["show_lut"]])
        if not p:
            raise FileNotFoundError(f"show LUT not found: {st['show_lut']}")
    else:
        p = _user_lut(cfg, list(SHOW_NAMES))
    if p:
        return {"kind": "lut", "path": p, "src": "creator"}
    v = vendor_lut("show")
    if v:
        return {"kind": "lut", "path": v, "src": "vendor (found in an installed app)"}
    return {"kind": "math", "path": None, "src": "ARRI colorimetric formula (not ARRI's look: add the ARRI LUT)"}


def input_transform(cfg, cam, st) -> dict:
    """How this clip's camera gets to the matching space. kind: identity (hero LogC3) | math | lut | display | none."""
    prof = cam.get("log_profile")
    over = (st.get("cameras") or {}).get(cam.get("cam_key")) or (st.get("cameras") or {}).get(cam.get("family")) or {}
    prof = over.get("profile") or prof
    lut = _user_lut(cfg, [over.get("lut"), f"{cam.get('cam_key')}.cube", f"{cam.get('family')}.cube", f"{prof}.cube"]) \
        if prof not in (None, "rec709", "hlg") else _user_lut(cfg, [over.get("lut")])
    if prof == "arri_logc3":
        return {"kind": "identity", "profile": prof}
    if over.get("lut") or (lut and prof not in CS.MATH_PROFILES):
        return {"kind": "lut", "profile": prof, "path": lut, "src": "creator"} if lut else {"kind": "none", "profile": prof,
                                                                                         "why": f"LUT {over.get('lut')} not found"}
    if prof in CS.MATH_PROFILES:
        return {"kind": "math", "profile": prof}
    if prof in CS.LUT_ONLY_PROFILES:
        v = vendor_lut(prof)
        if v:
            return {"kind": "lut", "profile": prof, "path": v, "src": "vendor (found in an installed app)"}
        return {"kind": "none", "profile": prof, "why": f"{CS.ALL_PROFILES[prof]} has no published formula here: put the "
                                                       f"manufacturer's log->Rec.709 LUT at luts/{cam.get('cam_key')}.cube"}
    if prof in ("rec709", "hlg"):
        return {"kind": "display", "profile": prof}
    return {"kind": "none", "profile": prof, "why": "camera profile unknown"}


def _cube(path):
    return _CUBES.get(path) or _CUBES.setdefault(path, CS.Cube.read(path))


_CUBES = {}


def transform_fn(cfg, cam, st, match=None):
    """numpy function: camera RGB (0-1, as decoded) -> final display RGB, the whole combined transform; and a
    description. None when the clip has no camera transform (legacy grade path)."""
    it = input_transform(cfg, cam, st)
    if it["kind"] == "none":
        return None, it
    sh = show_transform(cfg, st)
    showf = (lambda t: _cube(sh["path"]).apply(np.clip(t, 0, 1))) if sh["kind"] == "lut" \
        else (lambda t: CS.logc3_to_display_colorimetric(t))
    if it["kind"] == "identity":
        base = lambda x: showf(x)  # noqa: E731
    elif it["kind"] == "math":
        prof = it["profile"]
        base = lambda x: showf(np.clip(CS.to_logc3(x, prof), 0, 1))  # noqa: E731
    elif it["kind"] == "lut":
        cube = _cube(it["path"])
        base = lambda x: cube.apply(x)  # noqa: E731
    else:                                                       # display: no input transform
        base = lambda x: np.asarray(x, np.float64)  # noqa: E731
    M = (match or {}).get("matrix") if match and not match.get("disabled") else None
    if M is not None:
        fn = lambda x: CS.apply_display_matrix(base(x), M)  # noqa: E731
    else:
        fn = base
    return fn, {"input": it, "show": sh if it["kind"] in ("identity", "math") else None, "match": M}


def match_for(con, cam_key_, day=None) -> dict | None:
    ensure_schema(con)
    rows = [dict(r) for r in con.execute("SELECT * FROM camera_match WHERE cam_key=?", (cam_key_,))]
    if not rows:
        return None
    pick = next((r for r in rows if r["day"] == day), None) if day else None
    if pick is None:                                            # nearest day
        def dist(r):
            try:
                return abs((dt.date.fromisoformat(r["day"]) - dt.date.fromisoformat(day)).days) if day else 0
            except ValueError:
                return 9999
        pick = min(rows, key=dist)
    try:
        return json.loads(pick["data"])
    except ValueError:
        return None


def clip_day(row) -> str:
    return (str(row["shot_at"] or "")[:10] if "shot_at" in row.keys() else "") or "unknown"


def lut_for_clip(cfg, con, row, style, match=True, size=33, range_="tv"):
    """Path of the combined camera LUT for a clip (cached in cache/luts/), plus its description; (None, desc) when
    the clip keeps the legacy grade. range_ = how the source is decoded (the LUT input is always after expansion)."""
    st = ct_settings(style)
    if st["mode"] == "off":
        return None, {"why": "camera_transforms off"}
    cam = get(con, row["id"])
    if not cam:
        return None, {"why": "camera not identified"}
    it = input_transform(cfg, cam, st)
    mt = match_for(con, cam["cam_key"], clip_day(row)) if (match and st["match"] and cam["family"] != st["hero"]) else None
    if it["kind"] == "display" and not (mt and mt.get("matrix")):
        return None, {"input": it, "why": "display-referred camera without a match"}
    fn, desc = transform_fn(cfg, cam, st, mt)
    if fn is None:
        return None, desc
    key_src = [it, desc.get("show") and {k: desc["show"][k] for k in ("kind", "path")}, desc.get("match"), size]
    for p in (it.get("path"), (desc.get("show") or {}).get("path")):
        if p:
            try:
                key_src.append(hashlib.sha1(Path(p).read_bytes()).hexdigest()[:12])
            except OSError:
                pass
    h = hashlib.sha1(json.dumps(key_src, sort_keys=True, default=str).encode()).hexdigest()[:16]
    out = Path(cfg.p("cache", "luts", f"cam_{cam['cam_key']}_{h}.cube"))
    if not out.exists():
        CS.Cube.from_function(fn, size, title=f"cutroom {cam['cam_key']} {it['kind']}").write(out)
    desc.update(cam_key=cam["cam_key"], profile=cam["log_profile"], profile_src=cam["profile_src"], path=str(out),
                log=it.get("profile") in IS_LOG)
    return str(out), desc


def match_only_lut(cfg, M, size=33) -> str:
    """A display-space LUT holding only the match matrix (preview proxies already have the transform baked in)."""
    h = hashlib.sha1(json.dumps(M).encode()).hexdigest()[:16]
    out = Path(cfg.p("cache", "luts", f"match_{h}.cube"))
    if not out.exists():
        CS.Cube.from_function(lambda x: CS.apply_display_matrix(x, M), size, title="cutroom camera match").write(out)
    return str(out)


# ================================================================================================ matching
def _corrected_start(con, cid):
    try:
        from . import sync as SY
        return SY.corrected_start(con, cid)
    except Exception:  # noqa: BLE001
        return None


def _pairs_for(cfg, con, cam_key_, hero, day, max_pairs=8):
    """[(clip_row, t, hero_row, t_hero, how)] frames of this camera and the hero at the same moment."""
    from . import sync as SY
    SY.ensure_schema(con)
    clips = {r["id"]: dict(r) for r in con.execute(
        "SELECT c.*, k.cam_key, k.family FROM clips c JOIN clip_camera k ON k.clip_id=c.id WHERE COALESCE(c.width,0)>0")}
    mine = {c for c, r in clips.items() if r["cam_key"] == cam_key_ and (day is None or clip_day(r) == day)}
    heroes = {c for c, r in clips.items() if r["family"] == hero}
    out = []
    for g in SY.groups(con):
        mem = [m for m in g["members"] if m["video"]]
        for m in [m for m in mem if m["clip_id"] in mine]:
            for hm in [x for x in mem if x["clip_id"] in heroes]:
                a0, a1 = max(m["offset"], hm["offset"]), min(m["end"], hm["end"])
                if a1 - a0 < 1.0:
                    continue
                for k in range(3):
                    gt = a0 + (a1 - a0) * (k + 0.5) / 3
                    out.append((clips[m["clip_id"]], SY.from_group(m, gt), clips[hm["clip_id"]], SY.from_group(hm, gt), "sync"))
    if len(out) < 2:                                     # same scene by (clock-corrected) capture time
        def start(r):
            s = _corrected_start(con, r["id"])
            if s is None and r.get("shot_at"):
                try:
                    s = dt.datetime.fromisoformat(str(r["shot_at"]).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    s = None
            return s
        hs = [(start(clips[h]), clips[h]) for h in heroes]
        hs = [x for x in hs if x[0] is not None]
        for c in sorted(mine):
            s = start(clips[c])
            if s is None or not hs:
                continue
            near = sorted(hs, key=lambda x: abs(x[0] - s))[:2]
            for hs_, hr in near:
                if abs(hs_ - s) > 900:
                    continue
                out.append((clips[c], float(clips[c]["duration"] or 0) / 2, hr, float(hr["duration"] or 0) / 2, "time"))
    return out[:max_pairs]


def _frame(cfg, con, row, t, width=192):
    src = decode_path(con, row)
    rng = "tv"
    try:
        v = _video(media.probe(cfg, src)) or {}
        rng = "full" if v.get("color_range") in ("pc", "jpeg") else "tv"
    except Exception:  # noqa: BLE001
        pass
    fr = read_frames(cfg, src, [t], width=width, in_range=rng)
    return fr[0] if fr else None


def signature_lab(disp):
    """Mean CIELAB of a display picture's usable pixels (the colour one angle shows)."""
    a = np.asarray(disp, np.float64).reshape(-1, 3)
    y = np.einsum("...j,j->...", a, CS.LUMA709)
    m = (y > 0.03) & (a.max(1) < 0.97)
    a = a[m] if m.sum() > 64 else a
    return CS.display_to_lab(a).mean(0)


def match_camera(cfg, con, cam_key_, day=None, style=None, matrix=False, echo=None) -> dict:
    """Fit and store the correction of one camera (on one day) toward the hero camera. Returns the stored data."""
    ensure_schema(con)
    st = ct_settings(style or {})
    st["mode"] = "auto"
    hero = st.get("hero") or HERO_DEFAULT
    pairs = _pairs_for(cfg, con, cam_key_, hero, day)
    if not pairs:
        return {"cam_key": cam_key_, "day": day, "ok": False, "why": f"no {FAMILIES.get(hero, hero)} shot at the same "
                                                                       "moment (sync group or capture time within 15 min)"}
    src_frames, ref_frames, used = [], [], []
    for row, t, hrow, ht, how in pairs:
        a, b = _frame(cfg, con, row, t), _frame(cfg, con, hrow, ht)
        if a is None or b is None:
            continue
        cam, hcam = get(con, row["id"]), get(con, hrow["id"])
        fa, da = transform_fn(cfg, cam, st, None)
        fb, _ = transform_fn(cfg, hcam, st, None)
        if fa is None or fb is None:
            return {"cam_key": cam_key_, "day": day, "ok": False, "why": (da or {}).get("why") or "no transform"}
        src_frames.append(fa(a))
        ref_frames.append(fb(b))
        used.append({"clip": row["id"], "t": round(float(t), 3), "hero": hrow["id"], "t_hero": round(float(ht), 3), "how": how})
    if not src_frames:
        return {"cam_key": cam_key_, "day": day, "ok": False, "why": "frames could not be decoded"}
    S_ = np.concatenate([f.reshape(-1, 3) for f in src_frames])
    R_ = np.concatenate([f.reshape(-1, 3) for f in ref_frames])

    def score(fit_):
        b_, a_ = [], []
        for s, r in zip(src_frames, ref_frames):
            lr = signature_lab(r)
            b_.append(float(CS.delta_e2000(signature_lab(s), lr)))
            a_.append(float(CS.delta_e2000(signature_lab(CS.apply_display_matrix(s, fit_["matrix"])), lr)))
        return b_, a_
    fit = CS.fit_match(S_, R_, matrix=False)
    de_b, de_a = score(fit)
    if matrix:                                  # the full 3x3 only when it actually beats gains + saturation
        fit3 = CS.fit_match(S_, R_, matrix=True)
        b3, a3 = score(fit3)
        if np.median(a3) < np.median(de_a) - 0.05:
            fit, de_a = fit3, a3
    data = {**fit, "ok": True, "cam_key": cam_key_, "day": day or "all", "hero": hero, "pairs": used,
            "n_pairs": len(used), "de_before": round(float(np.median(de_b)), 2), "de_after": round(float(np.median(de_a)), 2),
            "updated": time.time()}
    if data["de_after"] > data["de_before"] + 0.2:          # never store a correction that makes it worse
        data["disabled"] = True
        data["why"] = "the fitted correction did not reduce the difference; left off"
    db.upsert(con, "camera_match", {"cam_key": cam_key_, "day": day or "all", "data": json.dumps(data), "updated": time.time()},
              key=("cam_key", "day"))
    con.commit()
    if echo:
        echo(f"  {cam_key_} {day or 'all days'}: {len(used)} frame pair(s) · ΔE00 {data['de_before']} -> {data['de_after']}"
             f" · gains {fit['gains']} sat {fit['sat']}" + (" (left off)" if data.get("disabled") else ""))
    return data


def match_all(cfg, con, style=None, matrix=False, echo=print) -> list:
    """Every non-hero camera, per shoot day."""
    st = ct_settings(style or {})
    hero = st.get("hero") or HERO_DEFAULT
    out = []
    for c in cameras(con):
        if c["family"] == hero or c["family"] == "other" and not c["model"]:
            continue
        for day in c["days"]:
            out.append(match_camera(cfg, con, c["key"], None if day == "unknown" else day, style=style, matrix=matrix, echo=echo))
    return out


def preview_pair(cfg, con, clip, t, style=None, width=480) -> dict:
    """Before / after of the match on one frame (the Looks panel): {before, after, match} as RGB uint8 arrays."""
    row = dict(db.get_clip(con, clip))
    cam = get(con, row["id"])
    st = ct_settings(style or {})
    st["mode"] = "auto"
    f = _frame(cfg, con, row, t, width=width)
    if cam is None or f is None:
        return {}
    fb, _ = transform_fn(cfg, cam, st, None)
    mt = match_for(con, cam["cam_key"], clip_day(row))
    fa, _ = transform_fn(cfg, cam, st, mt)
    if fb is None:
        return {}
    to8 = lambda x: (np.clip(x, 0, 1) * 255 + 0.5).astype(np.uint8)  # noqa: E731
    return {"before": to8(fb(f)), "after": to8(fa(f)), "match": mt}


def group_delta_e(cfg, con, gid, style=None) -> dict:
    """QA camera_match: CIEDE2000 between the angles of one multicam group at shared moments, without and with the
    camera transforms + match (both through the hero's show transform). {pairs: [...], before, after}."""
    from . import sync as SY
    st = ct_settings(style or {})
    st["mode"] = "auto"
    g = SY.group(con, gid)
    vids = [m for m in g["members"] if m["video"]]
    rows = []
    for i, a in enumerate(vids):
        for b in vids[i + 1:]:
            ca, cb = get(con, a["clip_id"]), get(con, b["clip_id"])
            if not ca or not cb or ca["cam_key"] == cb["cam_key"]:
                continue
            a0, a1 = max(a["offset"], b["offset"]), min(a["end"], b["end"])
            if a1 - a0 < 1:
                continue
            gt = (a0 + a1) / 2
            ra, rb = dict(db.get_clip(con, a["clip_id"])), dict(db.get_clip(con, b["clip_id"]))
            fa_, fb_ = _frame(cfg, con, ra, SY.from_group(a, gt)), _frame(cfg, con, rb, SY.from_group(b, gt))
            if fa_ is None or fb_ is None:
                continue
            # before: every camera through the hero's show transform as if it were the hero (one LUT for all)
            hero_like = {"log_profile": "arri_logc3", "cam_key": "hero", "family": st["hero"]}
            n0, _ = transform_fn(cfg, hero_like, st, None)
            t_a, _ = transform_fn(cfg, ca, st, match_for(con, ca["cam_key"], clip_day(ra)) if ca["family"] != st["hero"] else None)
            t_b, _ = transform_fn(cfg, cb, st, match_for(con, cb["cam_key"], clip_day(rb)) if cb["family"] != st["hero"] else None)
            if None in (n0, t_a, t_b):
                continue
            before = float(CS.delta_e2000(signature_lab(n0(fa_)), signature_lab(n0(fb_))))
            after = float(CS.delta_e2000(signature_lab(t_a(fa_)), signature_lab(t_b(fb_))))
            rows.append({"a": a["angle"], "b": b["angle"], "cam_a": ca["cam_key"], "cam_b": cb["cam_key"], "t": round(gt, 2),
                         "de_before": round(before, 2), "de_after": round(after, 2)})
    return {"group": gid, "pairs": rows,
            "before": round(float(np.median([r["de_before"] for r in rows])), 2) if rows else None,
            "after": round(float(np.median([r["de_after"] for r in rows])), 2) if rows else None}


# ================================================================================================ NLE export
def export_luts(cfg, con, out_dir, clip_ids=None, style=None) -> list:
    """Per camera (and shoot day when the match differs by day): the combined .cube (camera -> show -> match) for
    Premiere's Lumetri "Input LUT", plus <camera>_to_LogC3.cube for the maths cameras (then put the ARRI LUT on an
    adjustment layer instead). Returns [{cam_key, day, file, logc3_file, clips: [ids], profile, how}]."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT * FROM clips WHERE COALESCE(width,0)>0").fetchall()
    if clip_ids is not None:
        want = set(clip_ids)
        rows = [r for r in rows if r["id"] in want]
    st = ct_settings(style or {})
    done = {}
    for r in rows:
        cam = get(con, r["id"])
        if not cam or st["mode"] == "off":
            continue
        mt = match_for(con, cam["cam_key"], clip_day(r)) if cam["family"] != st.get("hero", HERO_DEFAULT) else None
        k = (cam["cam_key"], (mt or {}).get("day") if mt else None)
        if k in done:
            done[k]["clips"].append(r["id"])
            continue
        fn, desc = transform_fn(cfg, cam, st, mt)
        it = (desc or {}).get("input") or {}
        if fn is None or (it.get("kind") == "display" and not (mt and mt.get("matrix"))):
            continue
        name = cam["cam_key"] + (f"_{k[1]}" if k[1] and k[1] != "all" else "")
        p = out_dir / f"{name}.cube"
        CS.Cube.from_function(fn, 33, title=f"cutroom {name}: {it.get('kind')} -> show -> match").write(p)
        logc = None
        if it.get("kind") == "math" and it["profile"] != "arri_logc3":
            logc = out_dir / f"{cam['cam_key']}_to_LogC3.cube"
            prof = it["profile"]
            CS.Cube.from_function(lambda x, pr=prof: CS.to_logc3(x, pr), 33, title=f"{prof} to ARRI LogC3 EI800/AWG3").write(logc)
        done[k] = {"cam_key": cam["cam_key"], "day": k[1], "file": str(p), "logc3_file": str(logc) if logc else None,
                   "clips": [r["id"]], "profile": cam["log_profile"], "profile_src": cam["profile_src"],
                   "input": it.get("kind"), "input_lut": it.get("path"), "show": ((desc.get("show") or {}).get("src")),
                   "match": bool(mt and mt.get("matrix") and not mt.get("disabled"))}
    return list(done.values())
