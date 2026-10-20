"""Hand an edit to a desktop NLE: Adobe Premiere Pro (FCP7 XML), Final Cut Pro / DaVinci Resolve (FCPXML 1.10),
OpenTimelineIO (.otio, when the `opentimelineio` package is installed) and CMX3600 EDL.

    cutroom export-nle PLAN.json [--format premiere|fcpxml|resolve|otio|edl|all] [--out DIR] [--render R.mp4]
                       [--clean-render] [--preview] [--no-graphics] [--no-transitions] [--zip] [--json]
    cutroom export-nle --verify DIR [PLAN.json]      re-read the files and compare every clip to the plan

What lands in renders/nle/<slug>_v<N>/ (and <slug>_v<N>_nle.zip with --zip):
  <slug>_v<N>.xml          Premiere Pro (File > Import). FCP7 xmeml v4; clips link to the ORIGINAL MASTERS
                           (absolute paths) at their own timebase / NTSC flag; exact frame in/out.
  <slug>_v<N>.fcpxml       FCPXML 1.10 (Final Cut Pro, DaVinci Resolve: File > Import > Timeline).
  <slug>_v<N>.otio         OpenTimelineIO (optional extra).
  <slug>_v<N>_V1.edl ...   CMX3600 fallback, one EDL per track (V1, V2, dialogue).
  captions.srt             the plan's captions at the exact heard times (Premiere: File > Import, drag to C1).
  graphics/                overlay graphics as ProRes 4444 with alpha (callouts, lower thirds, typed titles,
                           title cards), placed on V3.
  rendered/                what Premiere can't rebuild natively (cards, film burns, flashes, whips, dips, gate /
                           inset / letterbox frames, freeze frames, speed ramps), cut frame-exactly from a
                           cutroom render as ProRes 422 HQ and placed on V4 over the original clips (V1 keeps
                           the masters, so the editor can rebuild or re-cut them).
  README_PREMIERE.txt      the steps: import, relink, the LUT on an adjustment layer, what each track holds.
  nle.json                 manifest: every clip with its track, master path, frames; what verify reads.

Tracks: V1 main picture, V2 cutaways, V3 graphics, V4 rendered effects. Audio: dialogue (A1, A2 when J/L
cuts overlap), natural sound (A3, A4), music (A5, with keyframed volume where the music ducks under speech).
Multicam segments use the synced angle's master for picture and the best sound source (recorder > lav >
camera); each synced group used also gets a "MC <group>" source sequence with every angle stacked in sync
(Premiere: nest it, then Multi-Camera > Enable) so the editor can re-cut angles by hand.

The model is built from render.resolve_timeline (the same frame grid the renderer uses), so every clip's
timeline position is the cutroom frame; source in/out are rounded to the nearest frame of the clip's rate
(xmeml expresses them in the sequence timebase, FCPXML on the source's own frame grid, sample-exact for
audio). `verify()` re-parses the written files with an independent parser (and OTIO's own readers when
installed) and compares every clip, frame for frame, against a fresh resolve_timeline.
"""
import datetime as _dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import db, media
from . import plan as P

FORMATS = ("premiere", "fcpxml", "otio", "edl")
ALIASES = {"xml": "premiere", "xmeml": "premiere", "fcp7": "premiere", "resolve": "fcpxml", "fcpx": "fcpxml",
           "finalcut": "fcpxml", "cmx": "edl", "cmx3600": "edl"}
ROLE_TRACKS = (("dialogue", 2), ("natural", 2), ("music", 1))       # audio roles, minimum tracks each
FX_TRANSITIONS = ("flash", "whip", "burn", "dip", "dip_black", "dip_white")


def formats(spec) -> list:
    out = []
    for x in (spec if isinstance(spec, (list, tuple)) else [spec or "premiere"]):
        for f in str(x).split(","):
            f = ALIASES.get(f.strip().lower(), f.strip().lower())
            if not f:
                continue
            if f == "all":
                out += [g for g in FORMATS if g not in out]
            elif f in FORMATS:
                if f not in out:
                    out.append(f)
            else:
                raise ValueError(f"unknown NLE format {f!r}; formats: premiere, fcpxml (resolve), otio, edl, all")
    return out


# ============================================================================================ rates / time
def rate_of(fps):
    """fps (float or 'num/den') -> (timebase int, ntsc bool, exact Fraction)."""
    if isinstance(fps, str) and "/" in fps:
        n, d = fps.split("/")
        fr = Fraction(int(n), int(d) or 1)
    else:
        f = float(fps or 25.0)
        if abs(f - round(f)) < 0.005:
            fr = Fraction(int(round(f)))
        else:
            tb = int(round(f * 1.001))
            fr = Fraction(tb * 1000, 1001) if abs(f - tb * 1000 / 1001) < 0.01 else Fraction(f).limit_denominator(1001)
    if fr.denominator == 1001:
        return int(round(fr * Fraction(1001, 1000))), True, fr
    if fr.denominator == 1:
        return int(fr), False, fr
    return int(round(float(fr))), False, fr


def frames(t, rate: Fraction) -> int:
    return int(math.floor(float(t) * float(rate) + 0.5 + 1e-9))


def tc_string(frame, tb, drop=False) -> str:
    f = int(frame)
    tb = int(tb)
    if drop and tb in (30, 60):
        d = 2 if tb == 30 else 4
        per10 = tb * 600 - d * 9
        per1 = tb * 60 - d
        q, r = divmod(f, per10)
        f += d * 9 * q + (d * ((r - d) // per1) if r > d else 0)
    hh, rem = divmod(f, tb * 3600)
    mm, rem = divmod(rem, tb * 60)
    ss, ff = divmod(rem, tb)
    return f"{hh % 24:02d}:{mm:02d}:{ss:02d}{';' if drop else ':'}{ff:02d}"


def tc_frames(tc, tb) -> int:
    m = re.match(r"^(\d+)[:;.](\d+)[:;.](\d+)[:;.](\d+)$", str(tc).strip())
    if not m:
        raise ValueError(f"bad timecode {tc!r}")
    h, mi, s, f = (int(x) for x in m.groups())
    return ((h * 60 + mi) * 60 + s) * int(tb) + f


def tc_seconds(fi) -> Fraction:
    """A file's start timecode as a rational time on its own frame grid (0 when it has none)."""
    tc = fi.get("timecode")
    if not tc or not fi.get("tb"):
        return Fraction(0)
    try:
        f = tc_frames(tc, fi["tb"])
    except ValueError:
        return Fraction(0)
    return Fraction(f) / Fraction(fi.get("rate") or fi["tb"])


def path_url(p) -> str:
    return "file://localhost" + quote(str(Path(p).resolve()) if not str(p).startswith("/") else str(p))


def url_path(u) -> str:
    pu = urlparse(u)
    return unquote(pu.path)


def _rat(fr: Fraction) -> str:
    fr = Fraction(fr)
    return f"{fr.numerator}/{fr.denominator}s" if fr.denominator != 1 else f"{fr.numerator}s"


def _parse_rat(s) -> Fraction:
    s = str(s or "0s").strip().rstrip("s")
    if "/" in s:
        n, d = s.split("/")
        return Fraction(int(n), int(d))
    return Fraction(s) if s else Fraction(0)


# ============================================================================================ file info
_PROBE = {}


def file_info(cfg, path, row=None) -> dict:
    """Master file facts for the NLE: exact frame rate, size, timecode start, audio channels / rate."""
    key = str(path)
    if key in _PROBE:
        return dict(_PROBE[key])
    try:
        info = media.probe(cfg, path)
    except Exception:  # noqa: BLE001 - missing master: describe it from the library row
        info = {}
    fmt = info.get("format", {}) or {}
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(fmt.get("duration") or (row or {}).get("duration") or 0)
    if v:
        rf = v.get("r_frame_rate") or v.get("avg_frame_rate") or "25/1"
        tb, ntsc, fr = rate_of(rf if rf != "0/0" else str((row or {}).get("fps") or 25))
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if abs(rot) % 180 == 90:
            w, h = h, w
    elif row and (row.get("fps") or 0) > 0:
        tb, ntsc, fr = rate_of(row["fps"])
        w, h = int(row.get("width") or 0), int(row.get("height") or 0)
    else:
        tb, ntsc, fr, w, h = None, False, None, 0, 0
    tags = {}
    for s in [fmt] + list(info.get("streams", []) or []):
        for k, val in (s.get("tags") or {}).items():
            tags.setdefault(k.lower(), val)
    tc = tags.get("timecode")
    out = {"path": str(path), "name": Path(path).name, "duration": dur, "has_video": v is not None,
           "has_audio": a is not None or bool((row or {}).get("has_audio")),
           "channels": int((a or {}).get("channels") or (row or {}).get("audio_channels") or 2),
           "sample_rate": int((a or {}).get("sample_rate") or 48000), "width": w, "height": h,
           "tb": tb, "ntsc": ntsc, "rate": fr, "timecode": tc, "codec": (v or a or {}).get("codec_name"),
           "reel": re.sub(r"[^A-Za-z0-9_]", "_", Path(path).stem)[:32] or "REEL"}
    _PROBE[key] = out
    return dict(out)


# ============================================================================================ model
def _clip_rows(con, ids):
    out = {}
    for cid in ids:
        r = con.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
        if r:
            out[cid] = dict(r)
    return out


def _seq_rate(fps):
    tb, ntsc, fr = rate_of(fps)
    return tb, ntsc, fr


def _cover_rect(dw, dh, W, H, zoom=1.0):
    if not dw or not dh:
        return (0.0, 0.0, 1.0, 1.0)
    A = W / H
    if dw / dh > A:
        cw, ch = dh * A, dh
    else:
        cw, ch = dw, dw / A
    z = max(1.0, float(zoom or 1.0))
    cw, ch = cw / z, ch / z
    return ((dw - cw) / 2 / dw, (dh - ch) / 2 / dh, cw / dw, ch / dh)


def motion_of(rect, fi, W, H) -> dict | None:
    """Crop rect (fractions of the source frame) -> Premiere Basic Motion: scale % of source pixels and the
    centre offset in FCP7 units (fraction of half the frame, + = right / down)."""
    dw, dh = fi.get("width") or 0, fi.get("height") or 0
    if not dw or not dh or not rect:
        return None
    x, y, w, h = rect
    s = W / (w * dw)
    cx, cy = (x + w / 2) * dw, (y + h / 2) * dh
    ox, oy = -(cx - dw / 2) * s, -(cy - dh / 2) * s
    return {"scale": round(s * 100.0, 4), "horiz": round(ox / (W / 2.0), 5), "vert": round(oy / (H / 2.0), 5),
            "rect": [round(v, 5) for v in rect]}


def _sidecar_for(cfg, plan_path, render=None):
    from . import export as EX
    if render:
        sc = EX.load_sidecar(render)
        return sc, Path(render)
    r = EX.find_render(cfg, plan_path, preview=None, names=(Path(plan_path).stem,))   # by name: factory renders
    if r:                                                                              # point at their candidate
        return EX.load_sidecar(r), r
    return None, None


def build_model(cfg, con, plan_path, render=None) -> dict:
    """The NLE timeline of a plan: tracks of items on the cutroom frame grid with master paths and source frames.
    Nothing is written. `render` (a cutroom render of this plan) supplies the exact crops (face framing)."""
    from . import render as R
    plan_path = Path(plan_path)
    raw = P.load(plan_path)
    plan = P.normalize(raw, cfg)
    res = R.resolve_timeline(cfg, con, raw, preview=False)
    fps = float(res["fps"])
    tb, ntsc, srate = _seq_rate(fps)
    W, H = int(res["width"]), int(res["height"])
    slug = re.sub(r"_v\d+$", "", plan_path.stem)
    name = f"{slug}_v{plan.get('version', 1)}"
    sc, render_path = _sidecar_for(cfg, plan_path, render)
    crops = {}
    if sc:
        for r_ in sc.get("segments", []):
            if r_.get("crop"):
                crops[int(r_["i"])] = r_["crop"]
    ids = {p["clip"] for p in res["pieces"] if p.get("clip")}
    rows = _clip_rows(con, ids)
    files = {}

    def fkey(cid):
        if cid not in files:
            row = rows.get(cid) or dict(db.get_clip(con, cid))
            fi = file_info(cfg, row["path"], row)
            fi.update(id=f"file-{cid}", clip=cid, rel=row.get("rel"))
            if fi["tb"] is None:                                 # audio-only: the sequence timebase
                fi.update(tb=tb, ntsc=ntsc, rate=srate)
            files[cid] = fi
        return files[cid]["id"]

    def F(t):                                                    # timeline seconds -> the cutroom frame
        return int(round(float(t) * fps))

    items, markers = [], []
    v1 = [p for p in res["pieces"] if p["track"] == "V1"]
    # ---- V1 / V2
    for idx, p in enumerate(v1):
        f0, f1 = F(p["start"]), F(p["end"])
        if p.get("clip"):
            fk = fkey(p["clip"])
            fi = files[p["clip"]]
            rect = crops.get(idx) or _cover_rect(fi["width"], fi["height"], W, H, 1.0)
            out_s = p["out"] if p["kind"] != "still" else p["in"] + (f1 - f0) / fps
            items.append({"track": "V1", "role": "video", "kind": p["kind"], "piece": p["id"], "file": fk, "clip": p["clip"],
                          "name": Path(fi["path"]).stem, "start": f0, "end": f1, "in_s": float(p["in"]), "out_s": float(out_s),
                          "speed": float(p.get("speed") or 1.0), "still": p["kind"] == "still",
                          "motion": motion_of(rect, fi, W, H), "sec": p["sec"], "seg": p["seg"],
                          "look": p.get("look"), "push": p.get("push"), "ramp": p.get("ramp"),
                          "frame": p.get("frame"), "transition_in": p.get("transition_in"), "conform": p.get("conform")})
    for p in res["pieces"]:
        if p["track"] == "V2" and p.get("clip"):
            fk = fkey(p["clip"])
            fi = files[p["clip"]]
            items.append({"track": "V2", "role": "video", "kind": p["kind"], "piece": p["id"], "file": fk, "clip": p["clip"],
                          "name": Path(fi["path"]).stem, "start": F(p["start"]), "end": F(p["end"]),
                          "in_s": float(p["in"]), "out_s": float(p["out"]), "speed": 1.0, "still": False,
                          "motion": motion_of(_cover_rect(fi["width"], fi["height"], W, H), fi, W, H),
                          "sec": p["sec"], "seg": p["seg"]})
    # ---- audio: dialogue / natural (A1 pieces), music (A2 pieces)
    for p in res["pieces"]:
        if p["track"] == "A1" and p.get("clip"):
            fk = fkey(p["clip"])
            role = "dialogue" if p["kind"] == "voice" else "natural"
            f0, f1, in_s, out_s = snap(p, fps)
            items.append({"track": None, "role": role, "kind": p["kind"], "piece": p["id"], "file": fk, "clip": p["clip"],
                          "name": Path(files[p["clip"]]["path"]).stem, "start": f0, "end": f1,
                          "in_s": in_s, "out_s": out_s, "speed": float(p.get("speed") or 1.0),
                          "sec": p["sec"], "seg": p["seg"]})
    songs = {}
    for b in res["sections"]:
        m = b.get("music")
        if not m:
            continue
        piece = next((p for p in res["pieces"] if p["track"] == "A2" and p["sec"] == b["index"]), None)
        if not piece:
            continue
        sp = m["path"]
        key = "song-" + hashlib.sha1(sp.encode()).hexdigest()[:10]
        if key not in songs:
            fi = file_info(cfg, sp)
            fi.update(id=f"file-{key}", clip=None, tb=tb, ntsc=ntsc, rate=srate, has_video=False)
            songs[key] = fi
        files[key] = songs[key]
        items.append({"track": None, "role": "music", "kind": "music", "piece": piece["id"], "file": f"file-{key}",
                      "clip": None, "song": sp, "name": m.get("title") or Path(sp).stem,
                      "start": F(piece["start"]), "end": F(piece["end"]), "in_s": float(piece["in"]),
                      "out_s": float(piece["out"]), "speed": 1.0, "sec": b["index"], "seg": None,
                      "levels": music_levels(plan, b, res, fps)})
    _assign_audio_tracks(items)
    # ---- graphics (V3) and rendered effects (V4) are filled in by export(); their spans are known now
    gfx = [{"track": "V3", "role": "graphics", "kind": p["kind"], "piece": p["id"], "start": F(p["start"]), "end": F(p["end"]),
            "text": p.get("text") or p.get("label"), "sub": p.get("sub_text"), "position": p.get("position"),
            "sec": p["sec"], "seg": p["seg"]} for p in res["pieces"] if p["track"] == "V3"]
    for p in v1:
        if p["kind"] == "title":
            gfx.append({"track": "V3", "role": "graphics", "kind": "title_card", "piece": p["id"], "start": F(p["start"]),
                        "end": F(p["end"]), "text": (p.get("title") or {}).get("text") or p.get("label"),
                        "sub": (p.get("title") or {}).get("sub", ""), "position": "center", "sec": p["sec"], "seg": p["seg"]})
    fx = fx_spans(res, fps)
    # ---- markers: sections, notes, devices that are rendered, comments, the grade
    lut = (plan["style"].get("grade") or {}).get("lut") or getattr(cfg, "lut", "") or ""
    markers.append({"frame": 0, "name": "GRADE", "comment": (f"Lumetri on an adjustment layer over V1-V2: LUT {lut}" if lut
                                                               else "No LUT in the plan: grade as you like")})
    for b in res["sections"]:
        markers.append({"frame": F(b["start"]), "name": f"Section: {b['name']}",
                        "comment": f"{'music ' + str((b.get('music') or {}).get('title') or '') if b.get('music') else 'no music'}"})
    segspec = {}
    for si, sec in enumerate(plan.get("sections", [])):
        for gi, sg in enumerate(sec.get("segments", [])):
            segspec[(si, gi)] = sg
    for r_ in res["segments"]:
        sg = segspec.get((r_["sec"], r_["seg"])) or {}
        bits = []
        if sg.get("note"):
            bits.append(str(sg["note"]))
        if sg.get("multicam"):
            mc = sg["multicam"]
            bits.append(f"multicam group {mc.get('group')} angle {mc.get('angle')} (audio {mc.get('audio', 'auto')})")
        if sg.get("look"):
            bits.append(f"look {json.dumps(sg['look'])}")
        if bits:
            markers.append({"frame": F(r_["start"]), "name": f"{r_['label'][:40]}", "comment": " · ".join(bits)})
    for it in items:
        if it["track"] != "V1":
            continue
        notes = []
        if it.get("push"):
            notes.append(f"push-in to {it['push'].get('to')} (rendered on V4 only when framed; add Scale keyframes)")
        if it.get("ramp"):
            notes.append(f"speed ramp to {it['ramp'].get('to')}x over the last {it['ramp'].get('over')} s (rendered on V4)")
        if it.get("still"):
            notes.append("freeze frame (rendered on V4)")
        if it.get("conform") and it["conform"] not in ("none", None):
            notes.append(f"frame-rate conform: {it['conform']}")
        if notes:
            markers.append({"frame": it["start"], "name": it["name"], "comment": "; ".join(notes)})
    co = plan.get("cold_open") or {}
    if co.get("note"):
        markers.append({"frame": 0, "name": "Cold open", "comment": str(co["note"])[:900]})
    if plan.get("note"):
        markers.append({"frame": 0, "name": "Plan note", "comment": str(plan["note"])[:900]})
    for c in _comments(cfg, plan_path, slug):
        markers.append({"frame": F(c["t"]), "end": F(c["t_end"]) if c.get("t_end") is not None else None,
                        "name": f"Comment ({c.get('status', 'open')})", "comment": str(c.get("text", ""))[:900]})
    markers.sort(key=lambda m: m["frame"])
    captions = sorted((float(c["t0"]), float(c["t1"]), c["text"]) for p in v1 for c in p.get("captions", []))
    groups = multicam_groups(cfg, con, plan, fps, files, fkey)
    return {"schema": "cutroom-nle-v1", "title": plan.get("title", slug), "version": plan.get("version", 1), "slug": slug,
            "name": name, "plan": str(plan_path.resolve()), "fps": fps, "tb": tb, "ntsc": ntsc, "rate": str(srate),
            "width": W, "height": H, "frames": int(res["frames"]), "items": items, "graphics": gfx, "fx": fx,
            "markers": markers, "captions": captions, "files": files, "lut": lut, "groups": groups,
            "render": str(render_path) if render_path else None, "warnings": list(res.get("warnings") or [])}


def snap(p, fps):
    """A piece on the NLE frame grid, keeping sync: (start frame, end frame, source in s, source out s). Audio pieces
    start between frames (crossfade handles, J/L leads); moving the edge to the frame moves the source by the same
    amount, so nothing slides against the picture."""
    sp = float(p.get("speed") or 1.0)
    f0, f1 = int(round(float(p["start"]) * fps)), int(round(float(p["end"]) * fps))
    return (f0, f1, float(p["in"]) + (f0 / fps - float(p["start"])) * sp,
            float(p["out"]) + (f1 / fps - float(p["end"])) * sp)


def _comments(cfg, plan_path, slug):
    p = Path(cfg.work) / "studio" / "markers.json"
    try:
        ms = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    names = {slug, Path(plan_path).stem, Path(plan_path).name}
    return [m for m in ms if isinstance(m, dict) and m.get("t") is not None
            and (str(m.get("plan") or "") in names or str(m.get("plan") or "").startswith(slug))]


def _assign_audio_tracks(items):
    """Checkerboard overlapping audio of one role onto that role's tracks (J/L cuts overlap their neighbours)."""
    tracks = {}
    for role, nmin in ROLE_TRACKS:
        tracks[role] = [[] for _ in range(nmin)]
    for it in sorted((i for i in items if i["role"] in tracks), key=lambda i: (i["start"], i["end"])):
        lanes = tracks[it["role"]]
        for k, lane in enumerate(lanes):
            if not lane or lane[-1]["end"] <= it["start"]:
                lane.append(it)
                it["lane"] = k
                break
        else:
            lanes.append([it])
            it["lane"] = len(lanes) - 1
    n = 0
    order = {}
    for role, _ in ROLE_TRACKS:
        for k in range(len(tracks[role])):
            n += 1
            order[(role, k)] = n
    for it in items:
        if it["role"] in tracks:
            it["track"] = f"A{order[(it['role'], it['lane'])]}"
    return {f"A{v}": f"{r} {k + 1}" for (r, k), v in order.items()}


def audio_track_roles(model) -> dict:
    out, n = {}, 0
    lanes = {}
    for it in model["items"]:
        if it["role"] in ("dialogue", "natural", "music"):
            lanes.setdefault(it["role"], set()).add(it["lane"])
    for role, nmin in ROLE_TRACKS:
        k = max(nmin, max(lanes.get(role, {-1})) + 1)
        for j in range(k):
            n += 1
            out[f"A{n}"] = (role, j)
    return out


def music_levels(plan, band, res, fps) -> list:
    """Volume keyframes (timeline frames, dB) for a section's music: the renderer's shape (lift where nobody
    talks, duck / mute under dialogue, 0.3 s ramps). Levels are relative: set the music track by ear."""
    m = band["music"]
    st = plan["style"]
    aud = st.get("audio") or {}
    sec = plan["sections"][band["index"]] if band["index"] >= 0 else {}
    mus = (sec.get("music") or {}) if sec else {}
    mode = m.get("mode") or st.get("music_under_speech", "mute")
    base = float(m.get("gain_db") or 0.0)
    lift = float(m.get("montage_gain_db") or 0.0) if mode != "under" else 0.0
    duck = {"mute": -60.0, "duck": float(mus.get("duck_db", -18.0)), "full": 0.0}.get(mode, 0.0)
    if mode == "under":
        return [{"frame": int(round(band["start"] * fps)), "db": round(base + float(mus.get("under_db", -5.0)), 2)}]
    ramp = float(aud.get("bed_fade_s", 0.3) or 0.3)
    spans = sorted((p["start"], p["end"]) for p in res["pieces"] if p["track"] == "A1" and p["kind"] == "voice"
                   and p["end"] > band["start"] and p["start"] < band["end"])
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    hi, lo = base + lift, base + lift + duck if mode != "full" else base + lift
    pts = [(band["start"], hi)]
    for a, b in merged:
        a, b = max(a, band["start"]), min(b, band["end"])
        pts += [(max(band["start"], a - ramp), hi), (a, lo), (b, lo), (min(band["end"], b + ramp), hi)]
    pts.append((band["end"], hi))
    out = []
    for t, v in sorted(pts):
        f = int(round(t * fps))
        if out and out[-1]["frame"] == f:
            out[-1]["db"] = round(v, 2)
            continue
        out.append({"frame": f, "db": round(v, 2)})
    clean = [out[0]] if out else []                              # drop flat middle points
    for i in range(1, len(out) - 1):
        if not (out[i - 1]["db"] == out[i]["db"] == out[i + 1]["db"]):
            clean.append(out[i])
    if len(out) > 1:
        clean.append(out[-1])
    return clean


def fx_spans(res, fps) -> list:
    """Timeline frame spans the NLE can't rebuild natively (rendered from cutroom onto V4), merged."""
    spans = []
    v1 = [p for p in res["pieces"] if p["track"] == "V1"]
    for p in v1:
        f0, f1 = int(round(p["start"] * fps)), int(round(p["end"] * fps))
        why = []
        if p["kind"] == "card":
            why.append("card")
        if p["kind"] == "still":
            why.append("freeze")
        if p.get("frame") and p["frame"] not in ("full", "none"):
            why.append(f"frame:{p['frame']}")
        if p.get("ramp"):
            spans.append((max(f0, f1 - int(round(float(p["ramp"].get("over", 0.4)) * fps))), f1, ["ramp"]))
        tr = p.get("transition_in")
        if tr and tr.get("kind") in FX_TRANSITIONS:
            fo, fi_ = int(tr.get("frames_out", 0) or 0), int(tr.get("frames_in", 0) or 0)
            spans.append((max(0, f0 - fo), f0 + max(fi_, 1), [tr["kind"]]))
        if why:
            spans.append((f0, f1, why))
    for t in res.get("transitions") or []:
        if t.get("kind") in FX_TRANSITIONS and t.get("t") is not None:
            f = int(round(float(t["t"]) * fps))
            spans.append((max(0, f - int(t.get("frames_out", 1) or 1)), f + max(1, int(t.get("frames_in", 1) or 1)), [t["kind"]]))
    spans.sort()
    merged = []
    for a, b, w in spans:
        if merged and a <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], b)
            merged[-1]["why"] = sorted(set(merged[-1]["why"]) | set(w))
        else:
            merged.append({"start": a, "end": b, "why": sorted(set(w))})
    total = int(res["frames"])
    return [s for s in merged if s["end"] > s["start"] and s["start"] < total]


def multicam_groups(cfg, con, plan, fps, files, fkey) -> list:
    """Synced source sequences for every multicam group the plan uses: each angle on its own track at its offset."""
    gids = []
    for sec in plan.get("sections", []):
        for sg in sec.get("segments", []):
            mc = sg.get("multicam")
            if isinstance(mc, dict) and mc.get("group") is not None and mc["group"] not in gids:
                gids.append(mc["group"])
    if not gids:
        return []
    from . import sync as MC
    out = []
    for gid in gids:
        try:
            g = MC.group(con, gid)
        except KeyError:
            continue
        mem = []
        for m in g["members"]:
            fk = fkey(m["clip_id"])
            fi = files[m["clip_id"]]
            mem.append({"angle": m["angle"], "name": m.get("device_name") or m["angle"], "file": fk, "clip": m["clip_id"],
                        "video": bool(m["video"]), "start": int(round(float(m["offset"]) * fps)),
                        "end": int(round(float(m["offset"]) * fps)) + int(round(float(m["duration"] or fi["duration"]) * fps)),
                        "in_s": 0.0, "out_s": float(m["duration"] or fi["duration"]), "drift_ppm": m["drift_ppm"],
                        "audio_rank": m["audio_rank"]})
        out.append({"group": g["id"], "name": g["name"], "members": mem,
                    "frames": max(x["end"] for x in mem) if mem else 0})
    return out


# ============================================================================================ graphics + fx media
def render_graphics(cfg, con, model, plan_path, dest, echo=print) -> list:
    """Overlay graphics (and title cards) as ProRes 4444 + alpha .mov files, one per V3 item."""
    from . import render as R
    raw = P.load(plan_path)
    plan = P.normalize(raw, cfg)
    ctx = R.Ctx(cfg, con, plan, preview=False)
    co = R.build_cold_open(ctx, plan) if plan.get("cold_open") else None
    sections = ([(-1, co)] if co else []) + list(enumerate(plan["sections"]))
    tl, segs, secs = R.expand(ctx, sections)
    specs, _ = R.apply_layers(ctx, tl, segs)
    for at in tl.atoms:
        if at["kind"] == "title":
            specs.append({"id": len(specs), "kind": "title_card", "text": at["title"]["text"], "sub": at["title"]["sub"],
                          "position": "center", "dur": at["n"] / ctx.fps, "t0": at["f0"] / ctx.fps, "t1": at["f1"] / ctx.fps})
    if not specs:
        return []
    gfx = R.render_graphics(ctx, specs)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    fps = model["fps"]
    for k, sp in enumerate(specs):
        g = gfx.get(sp["id"])
        if not g:
            continue
        f0 = int(round(float(sp["t0"]) * fps))
        n = int(g["n"])
        slug = re.sub(r"[^a-z0-9]+", "_", str(sp.get("text") or sp["kind"]).lower()).strip("_")[:24] or sp["kind"]
        mov = dest / f"G{k + 1:02d}_{sp['kind']}_{slug}.mov"
        src = Path(g["dir"]) / "%04d.png"
        if not mov.exists() or mov.stat().st_mtime < Path(g["dir"]).stat().st_mtime:
            tmp = mov.with_suffix(".tmp.mov")
            media.ffmpeg(cfg, ["-framerate", str(Fraction(model["rate"])), "-start_number", "0", "-i", str(src),
                               "-frames:v", str(n), "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                               "-vendor", "apl0", str(tmp)])
            os.replace(tmp, mov)
        out.append({"track": "V3", "role": "graphics", "kind": sp["kind"], "file": f"gfx-{k + 1}", "path": str(mov),
                    "name": mov.stem, "start": f0, "end": f0 + n, "in_s": 0.0, "out_s": n / fps, "speed": 1.0,
                    "text": sp.get("text")})
    return out


def _render_has_burned(sc, f0, f1, fps):
    a, b = f0 / fps, f1 / fps
    for r_ in (sc or {}).get("segments", []):
        for c in (r_.get("captions") or []) + (r_.get("overlays") or []):
            if c.get("t0", 0) < b and c.get("t1", 0) > a:
                return True
    return False


def render_fx(cfg, model, render, dest, echo=print) -> list:
    """Cut every V4 span frame-exactly out of a cutroom render (ProRes 422 HQ at the sequence size)."""
    from . import export as EX
    if not render or not Path(render).exists():
        return []
    sc = EX.load_sidecar(render) or {}
    vi = EX.video_info(cfg, render)
    fps = model["fps"]
    if abs(vi["fps"] - fps) > 0.01:
        model["warnings"].append(f"render {Path(render).name} is {vi['fps']} fps, the plan {fps}: effects not cut from it")
        return []
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    W, H = model["width"], model["height"]
    out = []
    lead = int(round(fps))
    for k, s in enumerate(model["fx"]):
        n = s["end"] - s["start"]
        if n <= 0 or s["end"] > int(round(vi["duration"] * fps)) + 1:
            continue
        mov = dest / f"FX{k + 1:02d}_{s['start']:06d}_{'-'.join(s['why'])[:30].replace(':', '_')}.mov"
        key = hashlib.sha1(f"{Path(render).resolve()}|{Path(render).stat().st_mtime}|{s['start']}|{n}|{W}x{H}".encode()).hexdigest()[:12]
        stamp = mov.with_suffix(".key")
        if not (mov.exists() and stamp.exists() and stamp.read_text() == key):
            sf = max(0, s["start"] - lead)
            tmp = mov.with_suffix(".tmp.mov")
            media.ffmpeg(cfg, ["-ss", f"{sf / fps:.6f}", "-i", str(render),
                               "-vf", f"trim=start_frame={s['start'] - sf}:end_frame={s['end'] - sf},setpts=PTS-STARTPTS,"
                                      f"scale={W}:{H}:flags=lanczos,setsar=1",
                               "-an", "-frames:v", str(n), "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
                               "-vendor", "apl0", "-r", str(Fraction(model["rate"])), str(tmp)])
            os.replace(tmp, mov)
            stamp.write_text(key)
        burned = _render_has_burned(sc, s["start"], s["end"], fps)
        out.append({"track": "V4", "role": "rendered", "kind": "+".join(s["why"]), "file": f"fx-{k + 1}", "path": str(mov),
                    "name": mov.stem, "start": s["start"], "end": s["end"], "in_s": 0.0, "out_s": n / fps, "speed": 1.0,
                    "burned": burned})
    if any(o["burned"] for o in out):
        model["warnings"].append("some rendered effect segments come from a render with captions/overlays burned in "
                                 "(use --clean-render for a caption-free cut)")
    if vi["h"] and vi["h"] != H:
        model["warnings"].append(f"effects were cut from a {vi['w']}x{vi['h']} render and scaled to {W}x{H}: "
                                 "render the final (or --clean-render) for full quality")
    return out


def _media_file(path, fps_rate: Fraction, W, H, alpha=False) -> dict:
    tb, ntsc, fr = rate_of(f"{fps_rate.numerator}/{fps_rate.denominator}")
    return {"path": str(path), "name": Path(path).name, "duration": None, "has_video": True, "has_audio": False,
            "channels": 0, "sample_rate": 48000, "width": W, "height": H, "tb": tb, "ntsc": ntsc, "rate": fr,
            "timecode": None, "reel": re.sub(r"[^A-Za-z0-9_]", "_", Path(path).stem)[:32], "alpha": alpha}


# ============================================================================================ xmeml (Premiere)
def _sub(parent, tag, text=None, **attrib):
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrib.items()})
    if text is not None:
        e.text = str(text)
    return e


def _rate_el(parent, tb, ntsc):
    r = _sub(parent, "rate")
    _sub(r, "timebase", tb)
    _sub(r, "ntsc", "TRUE" if ntsc else "FALSE")
    return r


def _file_el(parent, fi, seen, seq_tb, seq_ntsc):
    fid = fi["id"]
    if fid in seen:
        _sub(parent, "file", id=fid)
        return
    seen.add(fid)
    f = _sub(parent, "file", id=fid)
    _sub(f, "name", fi["name"])
    _sub(f, "pathurl", path_url(fi["path"]))
    tb, ntsc = fi["tb"] or seq_tb, fi["ntsc"] if fi["tb"] else seq_ntsc
    _rate_el(f, tb, ntsc)
    rate = fi["rate"] or Fraction(tb)
    if fi.get("duration"):
        _sub(f, "duration", frames(fi["duration"], rate))
    tc = _sub(f, "timecode")
    _rate_el(tc, tb, ntsc)
    tcs = fi.get("timecode") or "00:00:00:00"
    try:
        tcf = tc_frames(tcs, tb)
    except ValueError:
        tcs, tcf = "00:00:00:00", 0
    _sub(tc, "string", tcs.replace(";", ":") if not ";" in tcs else tcs)
    _sub(tc, "frame", tcf)
    _sub(tc, "displayformat", "DF" if ";" in tcs else "NDF")
    reel = _sub(tc, "reel")
    _sub(reel, "name", fi.get("reel") or "")
    md = _sub(f, "media")
    if fi.get("has_video"):
        v = _sub(md, "video")
        scx = _sub(v, "samplecharacteristics")
        _rate_el(scx, tb, ntsc)
        _sub(scx, "width", fi.get("width") or 1920)
        _sub(scx, "height", fi.get("height") or 1080)
        _sub(scx, "anamorphic", "FALSE")
        _sub(scx, "pixelaspectratio", "square")
        _sub(scx, "fielddominance", "none")
    if fi.get("has_audio"):
        a = _sub(md, "audio")
        scx = _sub(a, "samplecharacteristics")
        _sub(scx, "depth", 16)
        _sub(scx, "samplerate", fi.get("sample_rate") or 48000)
        _sub(a, "channelcount", fi.get("channels") or 2)


def _speed_filter(parent, speed):
    flt = _sub(parent, "filter")
    ef = _sub(flt, "effect")
    _sub(ef, "name", "Time Remap")
    _sub(ef, "effectid", "timeremap")
    _sub(ef, "effectcategory", "motion")
    _sub(ef, "effecttype", "motion")
    _sub(ef, "mediatype", "video")
    for pid, val in (("variablespeed", "0"), ("speed", f"{speed * 100:.4f}"), ("reverse", "FALSE"), ("frameblending", "FALSE")):
        p = _sub(ef, "parameter", authoringApp="PremierePro")
        _sub(p, "parameterid", pid)
        _sub(p, "name", pid)
        if pid == "speed":
            _sub(p, "valuemin", "-100000")
            _sub(p, "valuemax", "100000")
        _sub(p, "value", val)


def _motion_filter(parent, mo):
    flt = _sub(parent, "filter")
    ef = _sub(flt, "effect")
    _sub(ef, "name", "Basic Motion")
    _sub(ef, "effectid", "basic")
    _sub(ef, "effectcategory", "motion")
    _sub(ef, "effecttype", "motion")
    _sub(ef, "mediatype", "video")
    p = _sub(ef, "parameter", authoringApp="PremierePro")
    _sub(p, "parameterid", "scale")
    _sub(p, "name", "Scale")
    _sub(p, "valuemin", "0")
    _sub(p, "valuemax", "1000")
    _sub(p, "value", mo["scale"])
    p = _sub(ef, "parameter", authoringApp="PremierePro")
    _sub(p, "parameterid", "center")
    _sub(p, "name", "Center")
    v = _sub(p, "value")
    _sub(v, "horiz", mo["horiz"])
    _sub(v, "vert", mo["vert"])


def _levels_filter(parent, levels, item, seq_rate):
    flt = _sub(parent, "filter")
    ef = _sub(flt, "effect")
    _sub(ef, "name", "Audio Levels")
    _sub(ef, "effectid", "audiolevels")
    _sub(ef, "effectcategory", "audiolevels")
    _sub(ef, "effecttype", "audiolevels")
    _sub(ef, "mediatype", "audio")
    p = _sub(ef, "parameter", authoringApp="PremierePro")
    _sub(p, "parameterid", "level")
    _sub(p, "name", "Level")
    _sub(p, "valuemin", "0")
    _sub(p, "valuemax", "3.98109")
    first = levels[0]["db"] if levels else 0.0
    _sub(p, "value", f"{10 ** (first / 20):.6f}")
    in_f = frames(item["in_s"], seq_rate)
    for k in levels:
        kf = _sub(p, "keyframe")
        _sub(kf, "when", in_f + (k["frame"] - item["start"]))          # media-relative frames (FCP7 convention)
        _sub(kf, "value", f"{10 ** (k['db'] / 20):.6f}")


def _marker_el(parent, m):
    e = _sub(parent, "marker")
    _sub(e, "comment", m.get("comment", ""))
    _sub(e, "name", m.get("name", ""))
    _sub(e, "in", m["frame"])
    _sub(e, "out", m["end"] if m.get("end") is not None else -1)


def _clipitem(track_el, it, fi, idx, seen, seq_tb, seq_ntsc, seq_rate, media_type, trackindex=1, links=None):
    ci = _sub(track_el, "clipitem", id=f"clipitem-{idx}")
    _sub(ci, "name", it.get("name") or fi["name"])
    _sub(ci, "enabled", "TRUE")
    _sub(ci, "duration", frames(fi["duration"], seq_rate) if fi.get("duration") else it["end"] - it["start"])
    _rate_el(ci, seq_tb, seq_ntsc)
    _sub(ci, "start", it["start"])
    _sub(ci, "end", it["end"])
    _sub(ci, "in", frames(it["in_s"], seq_rate))
    _sub(ci, "out", frames(it["out_s"], seq_rate))
    if media_type == "video":
        _sub(ci, "alphatype", "straight" if fi.get("alpha") else "none")
    _file_el(ci, fi, seen, seq_tb, seq_ntsc)
    st = _sub(ci, "sourcetrack")
    _sub(st, "mediatype", media_type)
    if media_type == "audio":
        _sub(st, "trackindex", trackindex)
    if abs(float(it.get("speed") or 1.0) - 1.0) > 1e-4 and not it.get("still"):
        _speed_filter(ci, float(it["speed"]))
    if media_type == "video" and it.get("motion"):
        _motion_filter(ci, it["motion"])
    if media_type == "audio" and it.get("levels"):
        _levels_filter(ci, it["levels"], it, seq_rate)
    lbl = _sub(ci, "labels")
    _sub(lbl, "label2", {"V1": "Iris", "V2": "Caribbean", "V3": "Lavender", "V4": "Rose"}.get(it.get("track"), "Forest"))
    return ci


def _sequence_el(parent, sid, name, model, video_tracks, audio_tracks, files, seen, markers=(), counter=None):
    tb, ntsc, rate = model["tb"], model["ntsc"], Fraction(model["rate"])
    seq = _sub(parent, "sequence", id=sid)
    _sub(seq, "name", name)
    total = max([model.get("frames", 0)] + [it["end"] for tr in video_tracks + audio_tracks for it in tr])
    _sub(seq, "duration", total)
    _rate_el(seq, tb, ntsc)
    tc = _sub(seq, "timecode")
    _rate_el(tc, tb, ntsc)
    _sub(tc, "string", "00:00:00:00")
    _sub(tc, "frame", 0)
    _sub(tc, "displayformat", "NDF")
    md = _sub(seq, "media")
    v = _sub(md, "video")
    fmt = _sub(v, "format")
    scx = _sub(fmt, "samplecharacteristics")
    _rate_el(scx, tb, ntsc)
    _sub(scx, "width", model["width"])
    _sub(scx, "height", model["height"])
    _sub(scx, "anamorphic", "FALSE")
    _sub(scx, "pixelaspectratio", "square")
    _sub(scx, "fielddominance", "none")
    for tr in video_tracks:
        t = _sub(v, "track")
        for it in sorted(tr, key=lambda i: i["start"]):
            counter[0] += 1
            _clipitem(t, it, files[it["file"]], counter[0], seen, tb, ntsc, rate, "video")
        _sub(t, "enabled", "TRUE")
        _sub(t, "locked", "FALSE")
    a = _sub(md, "audio")
    _sub(a, "numOutputChannels", 2)
    fmt = _sub(a, "format")
    scx = _sub(fmt, "samplecharacteristics")
    _sub(scx, "depth", 16)
    _sub(scx, "samplerate", 48000)
    for tr in audio_tracks:
        t = _sub(a, "track")
        for it in sorted(tr, key=lambda i: i["start"]):
            counter[0] += 1
            _clipitem(t, it, files[it["file"]], counter[0], seen, tb, ntsc, rate, "audio")
        _sub(t, "enabled", "TRUE")
        _sub(t, "locked", "FALSE")
    for m in markers:
        _marker_el(seq, m)
    return seq


def _files_by_id(model):
    out = {}
    for fi in model["files"].values():
        out[fi["id"]] = fi
    for it in model.get("graphics_media", []) + model.get("fx_media", []):
        out[it["file"]] = _media_file(it["path"], Fraction(model["rate"]), model["width"], model["height"],
                                      alpha=it["track"] == "V3")
        out[it["file"]].update(id=it["file"], duration=(it["end"] - it["start"]) / model["fps"])
    return out


def _video_tracks(model):
    v1 = [i for i in model["items"] if i["track"] == "V1"]
    v2 = [i for i in model["items"] if i["track"] == "V2"]
    return [v1, v2, list(model.get("graphics_media", [])), list(model.get("fx_media", []))]


def _audio_tracks(model):
    roles = audio_track_roles(model)
    return [[i for i in model["items"] if i["track"] == name] for name in roles]


def write_xmeml(model, path) -> Path:
    files = _files_by_id(model)
    root = ET.Element("xmeml", version="4")
    proj = _sub(root, "project")
    _sub(proj, "name", model["name"])
    ch = _sub(proj, "children")
    seen, counter = set(), [0]
    _sequence_el(ch, "sequence-1", model["name"], model, _video_tracks(model), _audio_tracks(model), files, seen,
                 markers=model["markers"], counter=counter)
    if model.get("groups"):
        b = _sub(ch, "bin")
        _sub(b, "name", "Multicam source sequences")
        bch = _sub(b, "children")
        for k, g in enumerate(model["groups"]):
            vt = [[{**m, "track": "V1", "speed": 1.0}] for m in g["members"] if m["video"]]
            at = [[{**m, "track": "A", "speed": 1.0}] for m in sorted(g["members"], key=lambda m: m["audio_rank"])]
            sub = {**model, "frames": g["frames"]}
            _sequence_el(bch, f"sequence-mc-{g['group']}", f"MC {g['name']}", sub, vt, at, files, seen,
                         markers=[{"frame": 0, "name": "Synced by cutroom",
                                   "comment": "; ".join(f"{m['angle']} = {m['name']} offset {m['start'] / model['fps']:.3f}s"
                                                        f" drift {m['drift_ppm']} ppm" for m in g["members"])}],
                         counter=counter)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    Path(path).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n")
    return Path(path)


# ============================================================================================ FCPXML 1.10
def write_fcpxml(model, path) -> Path:
    """FCPXML 1.10: V1 on the primary storyline (gaps where cutroom draws titles / cards / black), everything
    else as connected clips in lanes (V2 = 1, graphics = 2, rendered effects = 3; audio below: dialogue -1..,
    natural, music lowest). Source starts are on each asset's own frame grid; audio is sample-exact."""
    files = _files_by_id(model)
    seq_rate = Fraction(model["rate"])
    fd = 1 / seq_rate
    root = ET.Element("fcpxml", version="1.10")
    res = _sub(root, "resources")
    fmt_ids = {}

    def fmt_id(rate, w, h):
        key = (str(rate), w, h)
        if key not in fmt_ids:
            fid = f"r{len(fmt_ids) + 1}"
            fmt_ids[key] = fid
            _sub(res, "format", id=fid, frameDuration=_rat(1 / Fraction(rate)), width=w, height=h)
        return fmt_ids[key]
    seq_fmt = fmt_id(seq_rate, model["width"], model["height"])
    assets, a_start = {}, {}
    for fid, fi in files.items():
        aid = f"a{len(assets) + 1}"
        assets[fid] = aid
        rate = fi.get("rate") or seq_rate
        a_start[fid] = tc_seconds(fi) if fi.get("has_video") else Fraction(0)     # media timecode, as FCP writes it
        attrs = {"id": aid, "name": fi["name"], "start": _rat(a_start[fid]),
                 "duration": _rat(Fraction(round(float(fi.get("duration") or 0) * 48000), 48000)),
                 "hasVideo": "1" if fi.get("has_video") else "0", "hasAudio": "1" if fi.get("has_audio") else "0"}
        if fi.get("has_video"):
            attrs["format"] = fmt_id(rate, fi.get("width") or model["width"], fi.get("height") or model["height"])
        if fi.get("has_audio"):
            attrs.update(audioSources="1", audioChannels=str(fi.get("channels") or 2), audioRate=str(fi.get("sample_rate") or 48000))
        a = _sub(res, "asset", **attrs)
        _sub(a, "media-rep", kind="original-media", src="file://" + quote(fi["path"]))
    lib = _sub(root, "library")
    ev = _sub(lib, "event", name="cutroom")

    def src_start(it, fi, audio=False):
        base = a_start.get(it["file"], Fraction(0))
        if audio or not fi.get("has_video"):
            return base + Fraction(round(float(it["in_s"]) * 48000), 48000)
        r = Fraction(fi.get("rate") or seq_rate)
        return base + Fraction(frames(it["in_s"], r)) / r

    def clip_el(parent, it, fi, offset, lane=None, audio=False):
        dur = (it["end"] - it["start"]) * fd
        st = src_start(it, fi, audio)
        attrs = {"ref": assets[it["file"]], "name": it.get("name") or fi["name"], "offset": _rat(offset),
                 "duration": _rat(dur), "start": _rat(st)}
        if lane is not None:
            attrs["lane"] = str(lane)
        if fi.get("has_video") and fi.get("has_audio"):
            attrs["srcEnable"] = "audio" if audio else "video"
        if audio:
            attrs["audioRole"] = {"dialogue": "dialogue", "natural": "effects", "music": "music"}.get(it["role"], "dialogue")
        c = _sub(parent, "asset-clip", **attrs)
        sp = float(it.get("speed") or 1.0)
        if abs(sp - 1.0) > 1e-4 and not it.get("still"):
            tm = _sub(c, "timeMap")
            _sub(tm, "timept", time=_rat(st), value=_rat(st), interp="linear")
            _sub(tm, "timept", time=_rat(st + dur), value=_rat(st + Fraction(round(float(dur) * sp * 48000), 48000)),
                 interp="linear")
        if it.get("motion") and not audio and abs(it["motion"]["scale"] - 100.0 * model["width"] / max(1, fi.get("width") or model["width"])) > 0.01:
            fit = 100.0 * min(model["width"] / max(1, fi.get("width") or 1), model["height"] / max(1, fi.get("height") or 1))
            s = it["motion"]["scale"] / fit if fit else 1.0
            _sub(c, "adjust-transform", scale=f"{s:.5f} {s:.5f}",
                 position=f"{it['motion']['horiz'] * 50 * model['width'] / model['height']:.4f} {-it['motion']['vert'] * 50:.4f}")
        if audio and it.get("levels"):
            av = _sub(c, "adjust-volume")
            prm = _sub(av, "param", name="amount")
            ka = _sub(prm, "keyframeAnimation")
            for k in it["levels"]:
                _sub(ka, "keyframe", time=_rat(st + (k["frame"] - it["start"]) * fd), value=f"{k['db']:.2f}dB")
        return c

    def build_project(name, frames_total, v1_items, connected, markers):
        proj = _sub(ev, "project", name=name)
        seq = _sub(proj, "sequence", format=seq_fmt, duration=_rat(frames_total * fd), tcStart="0s", tcFormat="NDF",
                   audioLayout="stereo", audioRate="48k")
        spine = _sub(seq, "spine")
        # primary storyline: V1 clips and gaps, frame-exact
        prim = []
        t = 0
        for it in sorted(v1_items, key=lambda i: i["start"]):
            if it["start"] > t:
                prim.append(("gap", t, it["start"], None))
            prim.append(("clip", it["start"], it["end"], it))
            t = it["end"]
        if t < frames_total:
            prim.append(("gap", t, frames_total, None))
        els = []
        for kind, a, b, it in prim:
            if kind == "gap":
                e = _sub(spine, "gap", name="Gap", offset=_rat(a * fd), duration=_rat((b - a) * fd), start="0s")
                els.append((a, b, e, Fraction(0)))
            else:
                fi = files[it["file"]]
                e = clip_el(spine, it, fi, a * fd)
                els.append((a, b, e, src_start(it, fi)))

        def host(f):
            for a, b, e, st in els:
                if a <= f < b:
                    return a, e, st
            a, b, e, st = els[-1]
            return a, e, st
        for it, lane, audio in connected:
            a, e, st = host(it["start"])
            clip_el(e, it, files[it["file"]], st + (it["start"] - a) * fd, lane=lane, audio=audio)
        for m in markers:
            a, e, st = host(m["frame"])
            _sub(e, "marker", start=_rat(st + (m["frame"] - a) * fd), duration=_rat(fd), value=m.get("name", "")[:120],
                 note=m.get("comment", "")[:900])
        return proj
    roles = audio_track_roles(model)
    lane_of = {name: -(k + 1) for k, name in enumerate(roles)}
    connected = [(i, 1, False) for i in model["items"] if i["track"] == "V2"]
    connected += [(i, 2, False) for i in model.get("graphics_media", [])]
    connected += [(i, 3, False) for i in model.get("fx_media", [])]
    connected += [(i, lane_of[i["track"]], True) for i in model["items"] if i["track"] in lane_of]
    build_project(model["name"], model["frames"], [i for i in model["items"] if i["track"] == "V1"], connected, model["markers"])
    for g in model.get("groups") or []:
        vids = [m for m in g["members"] if m["video"]]
        if not vids:
            continue
        base = vids[0]
        con_ = [({**m, "track": "V", "speed": 1.0}, k, False) for k, m in enumerate(vids[1:], 1)]
        con_ += [({**m, "track": "A", "role": "dialogue", "speed": 1.0}, -(k + 1), True)
                 for k, m in enumerate(sorted(g["members"], key=lambda m: m["audio_rank"]))]
        build_project(f"MC {g['name']}", g["frames"], [{**base, "track": "V1", "speed": 1.0}], con_, [])
    ET.indent(root, space="  ")
    Path(path).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode") + "\n")
    return Path(path)


# ============================================================================================ EDL
def write_edl(model, path, track="V1") -> Path | None:
    """CMX3600 for one track (V1, V2 or a dialogue track): events with reel, source/record timecodes at the sequence
    rate, M2 speed lines, clip names and full source paths as comments."""
    if track.startswith("A"):
        its = [i for i in model["items"] if i["role"] == "dialogue"]
    else:
        its = [i for i in model["items"] if i["track"] == track]
    if not its:
        return None
    files = _files_by_id(model)
    tb = model["tb"]
    rate = Fraction(model["rate"])
    lines = [f"TITLE: {model['name']}_{track}", "FCM: NON-DROP FRAME", ""]
    n = 0
    for it in sorted(its, key=lambda i: (i["start"], i["track"])):
        fi = files[it["file"]]
        n += 1
        try:
            base = tc_frames(fi.get("timecode") or "00:00:00:00", tb)
        except ValueError:
            base = 0
        si = base + frames(it["in_s"], rate)
        so = base + frames(it["out_s"], rate)
        if it.get("still"):
            so = si + (it["end"] - it["start"])
        reel = (fi.get("reel") or "AX")[:32]
        ch = "V" if not track.startswith("A") else "A"
        lines.append(f"{n:03d}  {reel:<8} {ch:<5} C        {tc_string(si, tb)} {tc_string(so, tb)} "
                     f"{tc_string(it['start'], tb)} {tc_string(it['end'], tb)}")
        sp = float(it.get("speed") or 1.0)
        if abs(sp - 1.0) > 1e-4 and not it.get("still"):
            lines.append(f"M2   {reel:<8} {sp * float(rate):06.1f}                {tc_string(si, tb)}")
        lines.append(f"* FROM CLIP NAME: {fi['name']}")
        lines.append(f"* SOURCE FILE: {fi['path']}")
        lines.append("")
    Path(path).write_text("\n".join(lines))
    return Path(path)


# ============================================================================================ OTIO
def have_otio() -> bool:
    try:
        import opentimelineio  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def write_otio(model, path) -> Path | None:
    if not have_otio():
        return None
    import opentimelineio as otio
    files = _files_by_id(model)
    rate = float(Fraction(model["rate"]))
    tl = otio.schema.Timeline(name=model["name"])
    tl.global_start_time = otio.opentime.RationalTime(0, rate)

    def track_of(kind, name, its):
        tr = otio.schema.Track(name=name, kind=kind)
        t = 0
        for it in sorted(its, key=lambda i: i["start"]):
            if it["start"] > t:
                tr.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(0, rate), otio.opentime.RationalTime(it["start"] - t, rate))))
            fi = files[it["file"]]
            frate = float(fi.get("rate") or rate)
            ref = otio.schema.ExternalReference(target_url="file://" + quote(fi["path"]),
                                                available_range=otio.opentime.TimeRange(
                                                    otio.opentime.RationalTime(0, frate),
                                                    otio.opentime.RationalTime(round(float(fi.get("duration") or 0) * frate), frate)))
            n = it["end"] - it["start"]
            src = otio.opentime.TimeRange(otio.opentime.RationalTime(frames(it["in_s"], Fraction(frate).limit_denominator(1001)), frate),
                                          otio.opentime.RationalTime(n * frate / rate, frate))
            c = otio.schema.Clip(name=it.get("name") or fi["name"], media_reference=ref, source_range=src)
            sp = float(it.get("speed") or 1.0)
            if abs(sp - 1.0) > 1e-4 and not it.get("still"):
                c.effects.append(otio.schema.LinearTimeWarp(time_scalar=sp))
            if it.get("still"):
                c.effects.append(otio.schema.FreezeFrame())
            c.metadata["cutroom"] = {k: it.get(k) for k in ("piece", "role", "kind", "track", "start", "end", "in_s", "out_s")}
            tr.append(c)
            t = it["end"]
        return tr
    vt = _video_tracks(model)
    for k, its in enumerate(vt):
        tl.tracks.append(track_of(otio.schema.TrackKind.Video, ["V1 main", "V2 cutaways", "V3 graphics", "V4 rendered"][k], its))
    roles = audio_track_roles(model)
    for name, its in zip(roles, _audio_tracks(model)):
        tl.tracks.append(track_of(otio.schema.TrackKind.Audio, f"{name} {roles[name][0]}", its))
    for m in model["markers"]:
        tl.tracks[0].markers.append(otio.schema.Marker(name=m.get("name", ""), marked_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(m["frame"], rate), otio.opentime.RationalTime(1, rate)),
            metadata={"comment": m.get("comment", "")}))
    otio.adapters.write_to_file(tl, str(path))
    return Path(path)


# ============================================================================================ README
def readme(model, out_files, fmts) -> str:
    fps = model["fps"]
    lut = model.get("lut") or ""
    roles = audio_track_roles(model)
    lines = [
        f"{model['title']} v{model['version']}: hand-off for Adobe Premiere Pro (and Resolve / Final Cut)",
        "=" * 72, "",
        f"Sequence: {model['width']}x{model['height']} at {fps:g} fps ({'NTSC ' if model['ntsc'] else ''}timebase {model['tb']}), "
        f"{model['frames']} frames ({model['frames'] / fps:.2f} s). Clips link to the ORIGINAL camera masters, not proxies.", "",
        "PREMIERE PRO",
        f"1. File > Import... and pick {model['name']}.xml. Premiere makes a bin with the sequence"
        + (" and a 'Multicam source sequences' bin." if model.get("groups") else "."),
        "2. If a clip shows as offline (the drive or folder moved): right-click it > Link Media..., point at the",
        "   first missing master and leave 'Relink others automatically' on. The full path of every master is in",
        "   nle.json and in the EDL comments.",
        "3. Captions: File > Import... captions.srt, then drag it onto the sequence (it lands on a caption track).",
        "4. Grade: Sequence > Add Tracks... one video track after V2 (V3 and V4 move up one). File > New >",
        "   Adjustment Layer, put it on that new track for the whole length, then Lumetri Color > Basic Correction >",
        "   Input LUT > Browse...:" if lut else
        "4. Grade: the plan has no LUT; grade the masters on an adjustment layer between V2 and the graphics.",
    ]
    if lut:
        lines += [f"   {lut}",
                  "   The graphics and rendered segments above it are finished already, so the layer must sit below them.",
                  "   On another machine copy that .cube file along and browse to the copy."]
    cl = model.get("camera_luts") or []
    if cl:
        lines += ["", "CAMERAS AND COLOUR (one input LUT per camera, made by cutroom)",
                  "Every camera is matched to the hero camera (ARRI). Each .cube in luts/ is that camera's whole transform:",
                  "its log curve -> the ARRI LogC3 show LUT -> the per-camera match (white balance, exposure, saturation).",
                  "A. Simplest: select all clips of one camera in the sequence (the markers say which), Lumetri Color > Basic",
                  "   Correction > Input LUT > Browse... the camera's file below. Do NOT also put the ARRI LUT on an adjustment",
                  "   layer for these clips (it is already inside). Use adjustment layers only for your own creative grade.",
                  "B. Or keep the ARRI LUT on an adjustment layer: give each non-ARRI clip the <camera>_to_LogC3.cube Input LUT",
                  "   (the curve and gamut only; the match is then not included, so re-balance by eye)."]
        for x in cl:
            how = {"identity": "hero camera", "math": "published log curve + gamut maths", "lut": "manufacturer LUT",
                   "display": "Rec.709 camera (match only)"}.get(x.get("input"), x.get("input"))
            lines.append(f"  luts/{Path(x['file']).name:<40} {len(x['clips'])} clip(s) · {x.get('profile')} "
                         f"({x.get('profile_src') or '?'}) · {how}" + (" · matched" if x.get("match") else ""))
            if x.get("logc3_file"):
                lines.append(f"  luts/{Path(x['logc3_file']).name:<40} option B for the same clips")
        lines.append("  Profiles marked (assumed) were estimated from the picture: check them (cutroom cameras).")
    lines += ["", "TRACKS",
              "V1  main picture, every cut at the exact cutroom frame (talk tightening = many short clips on purpose)",
              "V2  cutaways / b-roll covering the talk",
              "V3  graphics with alpha (ProRes 4444): callouts, lower thirds, typed titles, title cards",
              "V4  rendered segments (ProRes 422 HQ) for what Premiere can't rebuild: " +
              ", ".join(sorted({w for s in model["fx"] for w in s["why"]})) if model["fx"] else
              "V4  rendered segments: none in this edit"]
    for name, (role, k) in roles.items():
        lines.append(f"{name}  {role}{' (overlaps: J/L cuts)' if k else ''}")
    lines += ["", "WHAT IS APPROXIMATE",
              "- Music volume keyframes follow cutroom's ducking shape (lift between lines, duck/mute under speech,",
              "  0.3 s ramps). Absolute levels are relative: set the music track by ear.",
              "- Face-crop framing is exported as Basic Motion scale/position; check reframed shots.",
              "- Push-ins, speed ramps and looks are listed as sequence markers (the rendered V4 segments show them).",
              "- xmeml can't place audio between frames: a separate recorder can sit up to half a frame off picture",
              "  (FCPXML keeps it sample-exact). Nudge with Alt+arrow if lips look late.",
              "- Rendered V4 segments are cut from the cutroom render named in nle.json; re-export after a re-render."]
    if model.get("groups"):
        lines += ["", "MULTICAM",
                  "The main sequence already cuts between angles (picture from the chosen camera, sound from the best",
                  "source). To re-cut angles by hand: open the 'MC ...' sequence in the Multicam bin (every angle on its",
                  "own track, in sync), drag it into a new sequence, right-click the nested clip > Multi-Camera > Enable,",
                  "then cut in the Program monitor's multi-camera view."]
        for g in model["groups"]:
            lines.append(f"  MC {g['name']}: " + ", ".join(f"{m['angle']} ({m['name']})" for m in g["members"]))
    lines += ["", "FILES"] + [f"  {Path(p).name}" for p in out_files] + ["",
              f"Formats written: {', '.join(fmts)}. Final Cut Pro / DaVinci Resolve: import {model['name']}.fcpxml.",
              "Made by cutroom export-nle; verify with: cutroom export-nle --verify <this folder>"]
    if model.get("warnings"):
        lines += ["", "WARNINGS"] + [f"- {w}" for w in model["warnings"]]
    return "\n".join(lines) + "\n"


# ============================================================================================ camera LUTs
def camera_luts(cfg, con, model, plan_path, dest, echo=print) -> list:
    """Per-camera Lumetri Input LUTs for every clip on the picture tracks (cameras.export_luts), a CSV mapping each clip
    to its LUT, and a sequence marker on each clip naming the LUT to load."""
    from . import cameras as CAM
    try:
        plan = json.loads(Path(plan_path).read_text())
    except (OSError, ValueError):
        plan = {}
    ids = sorted({it["clip"] for it in model["items"] if it.get("clip") and it["track"] in ("V1", "V2")})
    if not ids:
        return []
    try:
        out = CAM.export_luts(cfg, con, dest / "luts", clip_ids=ids, style=plan.get("style") or {})
    except Exception as e:  # noqa: BLE001 - the timeline is still useful without them
        model["warnings"].append(f"camera LUTs not written ({type(e).__name__}: {e})")
        return []
    by_clip = {c: x for x in out for c in x["clips"]}
    rows = ["clip_file,camera,profile,profile_source,input_lut"]
    for cid in ids:
        x = by_clip.get(cid)
        fi = model["files"].get(cid) or {}
        cam = CAM.get(con, cid) or {}
        rows.append(",".join(str(v).replace(",", " ") for v in (Path(fi.get("path", cid)).name, cam.get("cam_key", ""),
                                                                cam.get("log_profile", ""), cam.get("profile_src", ""),
                                                                f"luts/{Path(x['file']).name}" if x else "")))
    (dest / "luts").mkdir(parents=True, exist_ok=True)
    (dest / "luts" / "clips.csv").write_text("\n".join(rows) + "\n")
    seen = set()
    for it in model["items"]:
        x = by_clip.get(it.get("clip"))
        if x and it["track"] in ("V1", "V2") and (it["clip"], it["track"]) not in seen:
            seen.add((it["clip"], it["track"]))
            model["markers"].append({"frame": it["start"], "name": f"Input LUT {x['cam_key']}",
                                     "comment": f"Lumetri > Basic Correction > Input LUT: luts/{Path(x['file']).name}"})
    if out:
        for m in model["markers"]:
            if m["name"] == "GRADE":
                m["comment"] = ("Colour-managed: each clip gets its camera's Input LUT from luts/ (markers below, README "
                                "'CAMERAS AND COLOUR'); adjustment layers for the creative grade only")
    model["markers"].sort(key=lambda m: m["frame"])
    if out:
        echo(f"export-nle: {len(out)} camera input LUT(s) in {dest / 'luts'}")
    return out


# ============================================================================================ export
def default_dir(cfg, model) -> Path:
    return cfg.p("renders", "nle", model["name"])


def _json_safe(o):
    if isinstance(o, Fraction):
        return f"{o.numerator}/{o.denominator}"
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def export(cfg, con, plan_path, fmts=("premiere",), out=None, render=None, clean_render=False, preview=False,
           graphics=True, make_zip=False, echo=print) -> dict:
    """Write the hand-off package. Returns the manifest (also written as nle.json)."""
    from . import export as EX
    fmts = formats(list(fmts) or ["premiere"])
    plan_path = Path(plan_path)
    if clean_render and not render:
        r = EX.find_render(cfg, plan_path, preview=None, names=(plan_path.stem,))
        if r is None:
            echo(f"export-nle: rendering {plan_path.name}{' (preview)' if preview else ''} first")
            r = EX.render_plan(cfg, plan_path, preview=preview, echo=echo)
        render = EX.clean_feed(cfg, r, EX.load_sidecar(r), echo=echo) or r
    model = build_model(cfg, con, plan_path, render=render)
    if render is None and model.get("render"):
        render = model["render"]
    dest = Path(out) if out else default_dir(cfg, model)
    dest.mkdir(parents=True, exist_ok=True)
    model["graphics_media"] = []
    if graphics and model["graphics"]:
        echo(f"export-nle: {len(model['graphics'])} graphic(s) -> ProRes 4444")
        try:
            model["graphics_media"] = render_graphics(cfg, con, model, plan_path, dest / "graphics", echo=echo)
        except Exception as e:  # noqa: BLE001
            model["warnings"].append(f"graphics not rendered ({type(e).__name__}: {e}); they are markers only")
    model["fx_media"] = []
    if model["fx"]:
        if render:
            echo(f"export-nle: {len(model['fx'])} rendered effect segment(s) from {Path(render).name}")
            model["fx_media"] = render_fx(cfg, model, render, dest / "rendered", echo=echo)
        else:
            model["warnings"].append("no cutroom render of this plan: cards / burns / frames are markers only "
                                     "(render it, or pass --render / --clean-render)")
        done = {(m["start"], m["end"]) for m in model["fx_media"]}
        for s in model["fx"]:
            if (s["start"], s["end"]) not in done:
                model["markers"].append({"frame": s["start"], "end": s["end"], "name": "RENDER IN CUTROOM",
                                         "comment": f"{', '.join(s['why'])}: not rebuilt here"})
        model["markers"].sort(key=lambda m: m["frame"])
    model["camera_luts"] = camera_luts(cfg, con, model, plan_path, dest, echo=echo)
    written = []
    if "premiere" in fmts:
        written.append(write_xmeml(model, dest / f"{model['name']}.xml"))
    if "fcpxml" in fmts:
        written.append(write_fcpxml(model, dest / f"{model['name']}.fcpxml"))
    if "otio" in fmts:
        p = write_otio(model, dest / f"{model['name']}.otio")
        if p:
            written.append(p)
        else:
            model["warnings"].append("OTIO not written: pip install opentimelineio (optional extra 'nle')")
    if "edl" in fmts:
        for tr in ("V1", "V2", "A1"):
            p = write_edl(model, dest / f"{model['name']}_{tr}.edl", tr)
            if p:
                written.append(p)
    if model["captions"]:
        written.append(EX.write_srt(model["captions"], dest / "captions.srt"))
    files = written + [Path(m["path"]) for m in model["graphics_media"] + model["fx_media"]] + \
        [Path(x["file"]) for x in model["camera_luts"]] + [Path(x["logc3_file"]) for x in model["camera_luts"] if x.get("logc3_file")]
    rd = dest / "README_PREMIERE.txt"
    rd.write_text(readme(model, files, fmts))
    written.append(rd)
    man = {k: v for k, v in model.items() if k not in ("files",)}
    man["files"] = {k: {kk: vv for kk, vv in v.items()} for k, v in model["files"].items()}
    man["outputs"] = [str(p) for p in written]
    man["dir"] = str(dest)
    man["formats"] = fmts
    (dest / "nle.json").write_text(json.dumps(man, indent=1, default=_json_safe))
    if make_zip:
        zp = dest.parent / f"{dest.name}_nle.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(dest.rglob("*")):
                if f.is_file() and not f.name.endswith((".key", ".tmp.mov")):
                    z.write(f, f"{dest.name}/{f.relative_to(dest)}")
        man["zip"] = str(zp)
    return man


# ============================================================================================ verify
def expected(cfg, con, plan_path) -> dict:
    """What the files must contain, straight from a fresh resolve_timeline (not from the writer's model)."""
    from . import render as R
    raw = P.load(plan_path)
    res = R.resolve_timeline(cfg, con, raw, preview=False)
    fps = float(res["fps"])
    tb, ntsc, srate = rate_of(fps)
    rows = {}
    out = []
    for p in res["pieces"]:
        if p["track"] not in ("V1", "V2", "A1") or not p.get("clip"):
            continue
        if p["clip"] not in rows:
            rows[p["clip"]] = dict(db.get_clip(con, p["clip"]))
        row = rows[p["clip"]]
        f0, f1, in_s, out_s = snap(p, fps)
        out.append({"track": p["track"], "kind": p["kind"], "piece": p["id"], "path": row["path"], "start": f0, "end": f1,
                    "in_s": in_s if p["kind"] != "still" else float(p["in"]),
                    "out_s": out_s if p["kind"] != "still" else float(p["in"]) + (f1 - f0) / fps,
                    "speed": float(p.get("speed") or 1.0), "still": p["kind"] == "still",
                    "src_fps": float(row.get("fps") or 0)})
    for b in res["sections"]:
        if b.get("music"):
            piece = next((p for p in res["pieces"] if p["track"] == "A2" and p["sec"] == b["index"]), None)
            if piece:
                out.append({"track": "A2", "kind": "music", "piece": piece["id"], "path": b["music"]["path"],
                            "start": int(round(piece["start"] * fps)), "end": int(round(piece["end"] * fps)),
                            "in_s": float(piece["in"]), "out_s": float(piece["out"]), "speed": 1.0, "still": False,
                            "src_fps": 0.0})
    return {"fps": fps, "tb": tb, "ntsc": ntsc, "rate": srate, "frames": int(res["frames"]), "items": out}


def lint_xmeml(path) -> list:
    """Schema-like checks of an FCP7 xmeml file (what Premiere's importer needs): required elements and their
    types, file references resolvable, frames ordered, no overlapping clips on a track. [] = clean."""
    probs = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        return [f"not well-formed XML: {e}"]
    if root.tag != "xmeml" or root.get("version") not in ("4", "5"):
        probs.append("root must be <xmeml version=4|5>")
    defined = {f.get("id") for f in root.iter("file") if f.find("pathurl") is not None}
    ids = [e.get("id") for e in root.iter() if e.get("id") and e.tag in ("clipitem", "sequence")]
    if len(ids) != len(set(ids)):
        probs.append("duplicate clipitem/sequence ids")

    def need_int(el, tag, where):
        v = el.findtext(tag)
        try:
            return int(v)
        except (TypeError, ValueError):
            probs.append(f"{where}: <{tag}> missing or not an integer ({v!r})")
            return None
    for f in root.iter("file"):
        if f.find("pathurl") is None:
            if f.get("id") not in defined:
                probs.append(f"file {f.get('id')}: referenced before / without a definition")
            continue
        if not (f.findtext("pathurl") or "").startswith("file://"):
            probs.append(f"file {f.get('id')}: pathurl must be a file:// URL")
        if f.find("rate/timebase") is None or f.findtext("rate/ntsc") not in ("TRUE", "FALSE"):
            probs.append(f"file {f.get('id')}: rate/timebase + rate/ntsc required")
        if f.find("media/video") is None and f.find("media/audio") is None:
            probs.append(f"file {f.get('id')}: media/video or media/audio required")
    for s in root.iter("sequence"):
        w = f"sequence {s.get('id')}"
        for tag in ("name", "duration", "rate/timebase", "rate/ntsc", "media/video/format/samplecharacteristics/width",
                    "media/video/format/samplecharacteristics/height"):
            if s.find(tag) is None:
                probs.append(f"{w}: <{tag}> required")
        for kind in ("video", "audio"):
            for ti, t in enumerate(s.findall(f"media/{kind}/track")):
                spans = []
                for ci in t.findall("clipitem"):
                    wc = f"{w} {kind} track {ti + 1} {ci.get('id')}"
                    vals = {k: need_int(ci, k, wc) for k in ("start", "end", "in", "out", "duration")}
                    if None in vals.values():
                        continue
                    if ci.find("name") is None or ci.find("rate/timebase") is None or ci.find("file") is None:
                        probs.append(f"{wc}: name, rate and file required")
                    if vals["end"] <= vals["start"] or vals["out"] < vals["in"]:
                        probs.append(f"{wc}: end <= start or out < in")
                    if vals["in"] < 0 or vals["start"] < 0:
                        probs.append(f"{wc}: negative in/start")
                    st = ci.findtext("sourcetrack/mediatype")
                    if st != kind:
                        probs.append(f"{wc}: sourcetrack/mediatype must be {kind}")
                    fe = ci.find("file")
                    if fe is not None and fe.get("id") not in defined:
                        probs.append(f"{wc}: file {fe.get('id')} is never defined")
                    spans.append((vals["start"], vals["end"]))
                spans.sort()
                for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
                    if b0 < a1:
                        probs.append(f"{w} {kind} track {ti + 1}: clips overlap at frame {b0}")
        for m in s.findall("marker"):
            if need_int(m, "in", f"{w} marker") is None:
                pass
    return probs


def lint_fcpxml(path) -> list:
    """Schema-like checks of FCPXML 1.10: version, unique resource ids, every ref resolves, times are rationals,
    offsets / durations of timeline items on the sequence frame grid, spine items contiguous."""
    probs = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        return [f"not well-formed XML: {e}"]
    if root.tag != "fcpxml" or root.get("version") != "1.10":
        probs.append("root must be <fcpxml version=1.10>")
    res = root.find("resources")
    ids = [e.get("id") for e in (res if res is not None else [])]
    if len(ids) != len(set(ids)):
        probs.append("duplicate resource ids")
    fmts = {f.get("id"): f for f in root.iter("format")}
    assets = {a.get("id") for a in root.iter("asset")}
    rx = re.compile(r"^-?\d+(/\d+)?s$")
    for a in root.iter("asset"):
        if a.find("media-rep") is None or not (a.find("media-rep").get("src") or "").startswith("file://"):
            probs.append(f"asset {a.get('id')}: media-rep src file:// required")
        if a.get("format") and a.get("format") not in fmts:
            probs.append(f"asset {a.get('id')}: unknown format {a.get('format')}")
    for pj in root.iter("project"):
        seq = pj.find("sequence")
        if seq is None or seq.get("format") not in fmts:
            probs.append(f"project {pj.get('name')}: sequence with a known format required")
            continue
        fd = _parse_rat(fmts[seq.get("format")].get("frameDuration"))
        t = Fraction(0)
        for el in seq.find("spine"):
            if el.tag not in ("asset-clip", "gap", "clip", "ref-clip", "title", "sync-clip"):
                continue
            for k in ("offset", "duration", "start"):
                if el.get(k) is not None and not rx.match(el.get(k)):
                    probs.append(f"{pj.get('name')}: {el.tag} {k}={el.get(k)!r} is not a rational time")
            off, dur = _parse_rat(el.get("offset")), _parse_rat(el.get("duration"))
            if off != t:
                probs.append(f"{pj.get('name')}: spine gap/overlap at {off} (expected {t})")
            t = off + dur
            for x in (off, dur):
                if (x / fd).denominator != 1:
                    probs.append(f"{pj.get('name')}: {el.tag} time {x} is off the {fd} frame grid")
            if el.tag == "asset-clip" and el.get("ref") not in assets:
                probs.append(f"{pj.get('name')}: asset-clip ref {el.get('ref')} unknown")
            for ch in el.findall("asset-clip"):
                if ch.get("ref") not in assets:
                    probs.append(f"{pj.get('name')}: connected ref {ch.get('ref')} unknown")
                if ch.get("lane") is None:
                    probs.append(f"{pj.get('name')}: connected clip without a lane")
                if (_parse_rat(ch.get("duration")) / fd).denominator != 1:
                    probs.append(f"{pj.get('name')}: connected clip duration {ch.get('duration')} off the frame grid")
        if t != _parse_rat(seq.get("duration")):
            probs.append(f"{pj.get('name')}: spine ends at {t}, sequence says {seq.get('duration')}")
    return probs


def parse_xmeml(path) -> dict:
    """An independent reader: sequences -> tracks -> clipitems with file path, start/end, in/out (frames)."""
    tree = ET.parse(path)
    root = tree.getroot()
    files = {}
    for f in root.iter("file"):
        if f.find("pathurl") is not None:
            files[f.get("id")] = {"path": url_path(f.findtext("pathurl")),
                                  "tb": int(f.findtext("rate/timebase") or 0), "ntsc": f.findtext("rate/ntsc") == "TRUE"}
    seqs = []
    for s in root.iter("sequence"):
        seq = {"id": s.get("id"), "name": s.findtext("name"), "tb": int(s.findtext("rate/timebase")),
               "ntsc": s.findtext("rate/ntsc") == "TRUE", "duration": int(s.findtext("duration") or 0),
               "width": int(s.findtext("media/video/format/samplecharacteristics/width") or 0),
               "height": int(s.findtext("media/video/format/samplecharacteristics/height") or 0),
               "video": [], "audio": [], "markers": []}
        for kind in ("video", "audio"):
            for ti, t in enumerate(s.findall(f"media/{kind}/track")):
                items = []
                for ci in t.findall("clipitem"):
                    fe = ci.find("file")
                    sp = 1.0
                    for ef in ci.findall("filter/effect"):
                        if ef.findtext("effectid") == "timeremap":
                            for prm in ef.findall("parameter"):
                                if prm.findtext("parameterid") == "speed":
                                    sp = float(prm.findtext("value")) / 100.0
                    items.append({"file": files.get(fe.get("id"), {}).get("path") if fe is not None else None,
                                  "start": int(ci.findtext("start")), "end": int(ci.findtext("end")),
                                  "in": int(ci.findtext("in")), "out": int(ci.findtext("out")),
                                  "tb": int(ci.findtext("rate/timebase") or seq["tb"]),
                                  "ntsc": (ci.findtext("rate/ntsc") or ("TRUE" if seq["ntsc"] else "FALSE")) == "TRUE",
                                  "speed": sp, "name": ci.findtext("name")})
                seq[kind].append(items)
        for m in s.findall("marker"):
            seq["markers"].append({"name": m.findtext("name"), "in": int(m.findtext("in") or 0)})
        seqs.append(seq)
    return {"sequences": seqs, "files": files}


def parse_fcpxml(path) -> dict:
    """Absolute timeline positions of every asset-clip of every project (connected clips resolved through their
    parent's local time)."""
    root = ET.parse(path).getroot()
    fmts = {f.get("id"): _parse_rat(f.get("frameDuration")) for f in root.iter("format")}
    assets = {}
    for a in root.iter("asset"):
        mr = a.find("media-rep")
        assets[a.get("id")] = {"path": url_path(mr.get("src")) if mr is not None else a.get("src"),
                               "fd": fmts.get(a.get("format")), "start": _parse_rat(a.get("start")),
                               "video": a.get("hasVideo") == "1"}
    projects = []
    for pj in root.iter("project"):
        seq = pj.find("sequence")
        fd = fmts[seq.get("format")]
        out = []
        for el in seq.find("spine"):
            off, dur, st = _parse_rat(el.get("offset")), _parse_rat(el.get("duration")), _parse_rat(el.get("start"))
            if el.tag == "asset-clip":
                out.append(_fcpx_clip(el, assets, off, st, dur, 0))
            for ch in el.findall("asset-clip"):
                coff = _parse_rat(ch.get("offset"))
                absol = off + (coff - st)
                out.append(_fcpx_clip(ch, assets, absol, _parse_rat(ch.get("start")), _parse_rat(ch.get("duration")),
                                      int(ch.get("lane") or 0)))
        projects.append({"name": pj.get("name"), "fd": fd, "duration": _parse_rat(seq.get("duration")), "clips": out})
    return {"projects": projects}


def _fcpx_clip(el, assets, absol, st, dur, lane):
    a = assets.get(el.get("ref"), {})
    sp = 1.0
    tm = el.find("timeMap")
    if tm is not None:
        pts = tm.findall("timept")
        if len(pts) >= 2:
            t0, t1 = _parse_rat(pts[0].get("time")), _parse_rat(pts[-1].get("time"))
            v0, v1 = _parse_rat(pts[0].get("value")), _parse_rat(pts[-1].get("value"))
            sp = float((v1 - v0) / (t1 - t0)) if t1 != t0 else 1.0
    audio = el.get("srcEnable") == "audio" or not a.get("video", True) or lane < 0
    return {"path": a.get("path"), "asset_fd": a.get("fd"), "offset": absol, "start": st - a.get("start", Fraction(0)),
            "duration": dur, "lane": lane, "src": "audio" if audio else (el.get("srcEnable") or "all"), "speed": sp,
            "name": el.get("name")}


def parse_edl(path, tb) -> list:
    out = []
    cur = None
    for ln in Path(path).read_text().splitlines():
        m = re.match(r"^(\d{3,})\s+(\S+)\s+(\S+)\s+C\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", ln)
        if m:
            cur = {"reel": m.group(2), "src_in": tc_frames(m.group(4), tb), "src_out": tc_frames(m.group(5), tb),
                   "start": tc_frames(m.group(6), tb), "end": tc_frames(m.group(7), tb), "path": None}
            out.append(cur)
        elif ln.startswith("* SOURCE FILE:") and cur is not None:
            cur["path"] = ln.split(":", 1)[1].strip()
    return out


def _match(exp, got, keyf, cmp):
    """Pair each expected item with one parsed item by key; returns (problems, matched)."""
    pool = {}
    for g in got:
        pool.setdefault(keyf(g), []).append(g)
    probs, n = [], 0
    for e in exp:
        cands = pool.get(keyf(e), [])
        if not cands:
            probs.append(f"missing {e['track']} {Path(e['path']).name} at frame {e['start']}")
            continue
        best = None
        for c in cands:
            p = cmp(e, c)
            if not p:
                best = c
                break
            best = best or (c, p)
        if isinstance(best, tuple):
            probs.append(f"{e['track']} {Path(e['path']).name} @ {e['start']}: " + "; ".join(best[1]))
            cands.remove(best[0])
        else:
            cands.remove(best)
            n += 1
    return probs, n


def verify(cfg, con, folder, plan_path=None, use_otio=True) -> dict:
    """Re-read every exported file in `folder` and compare clip by clip with a fresh resolve_timeline of the plan:
    timeline start/end frames exact, source in/out on the right frame, file = the master. Returns a report."""
    folder = Path(folder)
    man = json.loads((folder / "nle.json").read_text())
    plan_path = Path(plan_path or man["plan"])
    if not plan_path.exists():
        alt = cfg.p("plans", plan_path.name)
        plan_path = alt if alt.exists() else plan_path
    ex = expected(cfg, con, plan_path)
    rate, tb = ex["rate"], ex["tb"]
    fps = ex["fps"]
    rep = {"plan": str(plan_path), "fps": fps, "frames": ex["frames"], "checks": {}, "ok": True}
    vids = [e for e in ex["items"] if e["track"] in ("V1", "V2")]
    auds = [e for e in ex["items"] if e["track"] in ("A1", "A2")]
    stills = [e for e in ex["items"] if e["still"]]

    def done(name, probs, n, total, extra=None):
        ok = not probs and n == total
        rep["checks"][name] = {"ok": ok, "matched": n, "expected": total, "problems": probs[:40], **(extra or {})}
        rep["ok"] &= ok
    # ---- Premiere xmeml
    xml = folder / f"{man['name']}.xml"
    if xml.exists():
        lp = lint_xmeml(xml)
        done("premiere_xml_schema", lp, 0 if lp else 1, 1)
        px = parse_xmeml(xml)
        seq = px["sequences"][0]
        probs = []
        if (seq["tb"], seq["ntsc"]) != (tb, ex["ntsc"]):
            probs.append(f"sequence rate {seq['tb']}/{seq['ntsc']} != {tb}/{ex['ntsc']}")
        if (seq["width"], seq["height"]) != (man["width"], man["height"]):
            probs.append("sequence frame size differs")
        got = []
        for ti, tr in enumerate(seq["video"]):
            for c in tr:
                got.append({**c, "track": f"V{ti + 1}", "path": c["file"]})
        for ti, tr in enumerate(seq["audio"]):
            for c in tr:
                got.append({**c, "track": "A", "path": c["file"]})
        # overlapping clips on one track are illegal in an NLE
        for kind in ("video", "audio"):
            for ti, tr in enumerate(seq[kind]):
                s_ = sorted(tr, key=lambda c: c["start"])
                for a, b in zip(s_, s_[1:]):
                    if b["start"] < a["end"]:
                        probs.append(f"{kind} track {ti + 1}: clips overlap at frame {b['start']}")

        def cmp(e, c):
            p = []
            if (c["start"], c["end"]) != (e["start"], e["end"]):
                p.append(f"timeline {c['start']}-{c['end']} != {e['start']}-{e['end']}")
            r = rate_of(f"{c['tb'] * 1000}/1001") [2] if c["ntsc"] else Fraction(c["tb"])
            if c["in"] != frames(e["in_s"], r):
                p.append(f"in {c['in']} != {frames(e['in_s'], r)}")
            if c["out"] != frames(e["out_s"], r):
                p.append(f"out {c['out']} != {frames(e['out_s'], r)}")
            if not e["still"] and abs(c["speed"] - e["speed"]) > 1e-4:
                p.append(f"speed {c['speed']} != {e['speed']}")
            return p
        pv, nv = _match(vids, [g for g in got if g["track"] in ("V1", "V2")], lambda x: (x["track"], x["path"], x["start"]), cmp)
        pa, na = _match(auds, [g for g in got if g["track"] == "A"], lambda x: (x["path"], x["start"]), cmp)
        # graphics and rendered segments: present, right span, the file exists with that many frames
        pg = []
        for it in man.get("graphics_media", []) + man.get("fx_media", []):
            tr = seq["video"][2 if it["track"] == "V3" else 3] if len(seq["video"]) > 3 else []
            hit = [c for c in tr if c["start"] == it["start"] and c["end"] == it["end"]]
            if not hit:
                pg.append(f"{it['track']} {Path(it['path']).name} not at {it['start']}-{it['end']}")
            nfr = _count_frames(cfg, it["path"])
            if nfr is not None and nfr != it["end"] - it["start"]:
                pg.append(f"{Path(it['path']).name} has {nfr} frames, the span is {it['end'] - it['start']}")
        extra_v = sum(len(t) for t in seq["video"][:2]) - nv
        done("premiere_xml", probs + pv + pa + pg + ([f"{extra_v} unexpected clip(s) on V1/V2"] if extra_v > 0 else []),
             nv + na, len(vids) + len(auds), {"video": nv, "audio": na, "graphics_fx": len(man.get("graphics_media", [])) +
                                               len(man.get("fx_media", [])), "stills": len(stills),
                                               "sequences": len(px["sequences"])})
    # ---- FCPXML
    fx = folder / f"{man['name']}.fcpxml"
    if fx.exists():
        lf = lint_fcpxml(fx)
        done("fcpxml_schema", lf, 0 if lf else 1, 1)
        pf = parse_fcpxml(fx)
        pj = pf["projects"][0]
        fd = pj["fd"]
        probs = []
        if fd != 1 / rate:
            probs.append(f"sequence frameDuration {fd} != {1 / rate}")
        got = []
        for c in pj["clips"]:
            f0 = c["offset"] / fd
            if f0.denominator != 1:
                probs.append(f"{c['name']}: offset {c['offset']} is not on the frame grid")
            got.append({**c, "startf": int(round(float(f0))), "endf": int(round(float((c["offset"] + c["duration"]) / fd)))})

        def kf(x):
            if "startf" in x:
                trk = "V" if x["src"] != "audio" else "A"
                return (trk, x["path"], x["startf"])
            return ("V" if x["track"] in ("V1", "V2") else "A", x["path"], x["start"])

        def cmpf(e, c):
            p = []
            if (c["startf"], c["endf"]) != (e["start"], e["end"]):
                p.append(f"timeline {c['startf']}-{c['endf']} != {e['start']}-{e['end']}")
            if c["src"] == "audio" or not c["asset_fd"]:
                if abs(float(c["start"]) - e["in_s"]) > 0.5 / 48000 + 1e-9:
                    p.append(f"audio start {float(c['start']):.6f} != {e['in_s']:.6f}")
            else:
                r = 1 / c["asset_fd"]
                if c["start"] * r != frames(e["in_s"], r):
                    p.append(f"start frame {float(c['start'] * r)} != {frames(e['in_s'], r)}")
            if not e["still"] and abs(c["speed"] - e["speed"]) > 1e-3:
                p.append(f"speed {c['speed']:.5f} != {e['speed']}")
            return p
        pv, nv = _match(vids, [g for g in got if g["src"] != "audio"], kf, cmpf)
        pa, na = _match(auds, [g for g in got if g["src"] == "audio"], kf, cmpf)
        done("fcpxml", probs + pv + pa, nv + na, len(vids) + len(auds), {"video": nv, "audio": na})
    # ---- EDL (V1)
    edl = folder / f"{man['name']}_V1.edl"
    if edl.exists():
        ev = parse_edl(edl, tb)
        v1 = [e for e in vids if e["track"] == "V1"]
        files = man.get("files", {})
        tcbase = {}
        for f in files.values():
            try:
                tcbase[f["path"]] = tc_frames(f.get("timecode") or "00:00:00:00", tb)
            except ValueError:
                tcbase[f["path"]] = 0

        def cmpe(e, c):
            p = []
            if (c["start"], c["end"]) != (e["start"], e["end"]):
                p.append(f"record {c['start']}-{c['end']} != {e['start']}-{e['end']}")
            si = tcbase.get(e["path"], 0) + frames(e["in_s"], rate)
            if c["src_in"] != si:
                p.append(f"source in {c['src_in']} != {si}")
            return p
        pe, ne = _match(v1, [{**c, "track": "V1"} for c in ev], lambda x: (x["path"], x["start"]), cmpe)
        done("edl_v1", pe, ne, len(v1))
    # ---- OTIO's own readers (an independent implementation) over the same files
    if use_otio and have_otio():
        rep["checks"].update(_verify_with_otio(folder, man, ex))
        rep["ok"] = rep["ok"] and all(c["ok"] or c.get("informational") for c in rep["checks"].values())
    return rep


def _count_frames(cfg, path):
    try:
        r = media.run([cfg.ffprobe, "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
                       "stream=nb_read_packets", "-of", "csv=p=0", str(path)])
        return int((r.stdout or "").strip().split(",")[0])
    except Exception:  # noqa: BLE001
        return None


def _verify_with_otio(folder, man, ex) -> dict:
    """OTIO's own fcp_xml / fcpx_xml / cmx_3600 readers over our files: every V1 clip must come back at its frame
    with its source time (OTIO reports source times including the media's start timecode; that is taken off).
    EDLs carry no gaps in OTIO's reader, so the EDL is compared in order (source frame + length)."""
    import opentimelineio as otio
    out = {}
    fps = ex["fps"]
    vids = [e for e in ex["items"] if e["track"] == "V1"]
    tcs = {}
    for f in (man.get("files") or {}).values():
        try:
            r = Fraction(f["rate"]) if isinstance(f.get("rate"), str) else Fraction(f.get("tb") or 25)
            tcs[f["path"]] = float(tc_frames(f.get("timecode") or "00:00:00:00", f.get("tb") or 25) / r)
        except (ValueError, TypeError, ZeroDivisionError):
            tcs[f["path"]] = 0.0
    for name, fname, adapter in (("otio_reads_premiere_xml", f"{man['name']}.xml", "fcp_xml"),
                                 ("otio_reads_fcpxml", f"{man['name']}.fcpxml", "fcpx_xml"),
                                 ("otio_reads_edl", f"{man['name']}_V1.edl", "cmx_3600"),
                                 ("otio_file", f"{man['name']}.otio", None)):
        p = folder / fname
        if not p.exists():
            continue
        try:
            if adapter == "cmx_3600":
                tl = otio.adapters.read_from_file(str(p), adapter_name=adapter, rate=float(ex["rate"]))
            elif adapter:
                tl = otio.adapters.read_from_file(str(p), adapter_name=adapter)
            else:
                tl = otio.adapters.read_from_file(str(p))
            if isinstance(tl, otio.schema.SerializableCollection):
                tl = next(x for x in tl if isinstance(x, otio.schema.Timeline))
            clips = []
            for tr in tl.tracks:
                if tr.kind != otio.schema.TrackKind.Video:
                    continue
                for c in tr.find_clips():
                    rng = tr.range_of_child(c)
                    ref = c.media_reference
                    path = url_path(ref.target_url) if getattr(ref, "target_url", None) else None
                    clips.append({"start": int(round(rng.start_time.to_seconds() * fps)),
                                  "end": int(round(rng.end_time_exclusive().to_seconds() * fps)),
                                  "src": c.source_range.start_time.to_seconds(),
                                  "dur": int(round(c.source_range.duration.to_seconds() * fps)), "path": path,
                                  "name": c.name})
            probs, n = [], 0
            if adapter == "cmx_3600":
                v1c = clips[:len(vids)]
                for e, c in zip(vids, v1c):
                    if c["dur"] != e["end"] - e["start"]:
                        probs.append(f"V1 {Path(e['path']).name} @ {e['start']}: length {c['dur']} != {e['end'] - e['start']}")
                    elif abs(c["src"] - tcs.get(e["path"], 0.0) - e["in_s"]) > 0.5 / fps + 1e-6:
                        probs.append(f"V1 {Path(e['path']).name} @ {e['start']}: source {c['src'] - tcs.get(e['path'], 0):.4f}s"
                                     f" != {e['in_s']:.4f}s")
                    else:
                        n += 1
            else:
                # otio-fcpx-xml-adapter 1.0.0 counts frames with int(float(a / b) * rate): 29/25 s -> 28 frames.
                # Its reading is checked to +-1 frame; our own parser above is the exact check.
                slack = 1 if adapter == "fcpx_xml" else 0
                for e in vids:
                    cs = [c for c in clips if abs(c["start"] - e["start"]) <= slack and abs(c["end"] - e["end"]) <= slack
                          and c["path"] in (None, e["path"])]
                    if not cs:
                        probs.append(f"V1 {Path(e['path']).name} @ {e['start']}-{e['end']} not found")
                        continue
                    tol = (0.5 + slack) / min(fps, e["src_fps"] or fps) + 1e-6
                    src = [c["src"] - (tcs.get(e["path"], 0.0) if adapter else 0.0) for c in cs]
                    if not any(abs(x - e["in_s"]) <= tol for x in src):
                        probs.append(f"V1 {Path(e['path']).name} @ {e['start']}: source {src[0]:.4f}s != {e['in_s']:.4f}s")
                        continue
                    n += 1
            out[name] = {"ok": not probs and n == len(vids), "matched": n, "expected": len(vids), "problems": probs[:20]}
            if adapter == "fcpx_xml":
                # informational only: otio-fcpx-xml-adapter 1.0.0 counts frames with int(float) and reads asset
                # durations at the sequence rate, so 1001-based / mixed-rate timelines come back several frames off
                # (measured: a 23.976 sequence with a 25p clip). Our own parser (check "fcpxml") is exact.
                out[name]["informational"] = True
        except Exception as e:  # noqa: BLE001
            out[name] = {"ok": False, "matched": 0, "expected": len(vids), "problems": [f"{type(e).__name__}: {e}"]}
    return out
