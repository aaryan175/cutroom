#!/usr/bin/env python3
"""Cutroom Studio mock backend (dev only, stdlib only).

Serves the static frontend from cutroom/studio_web/ and fakes every API route from docs/STUDIO.md with
realistic data, so the UI can be built and driven without the real backend.

    ./make_media.sh /some/dir/mock_media                       # once: synthetic proxies/thumbs/songs (ffmpeg)
    python3 mock_server.py --port 8611 --media /some/dir/mock_media [--verbose]

Response shapes (the frontend relies on these; they mirror the real contract as closely as it is known)
-----------------------------------------------------------------------------------------------------
GET  /api/project   {name, root, status:{clips,hours,proxied,transcribed,tagged,songs},
                     plans:[{slug,title,draft,versions:[file],updated}],
                     renders:[{file,plan,version,preview,duration,created,qa,qa_status}],
                     songs:[{id,title,duration,tempo}], references:[{file,duration}]}
GET  /api/clips?q=&setting=&time=&people=&max_shake=&min_quality=&speech=&creator=&offset=&limit=
                    {total,offset,limit,rows:[{id,rel,duration,width,height,fps,quality,shake,has_transcript,
                     shot_at,tags:{summary,setting,time_of_day,people,creator_visible,talking_to_camera,shot}}]}
GET  /api/clip/<id> row + {codec, tags(full), metrics:[{t0,t1,shake,motion,exposure,sharpness,faces}],
                     shots:[{idx,start,end}], words:[[s,e,w]], sheet_url}
PUT  /api/clip/<id>/tags  (merge) -> {ok, tags}
GET  /media/proxy/<id> · /media/sheet/<id> · /media/thumb/<id>?t=<sec|mid> · /media/frame/<id>?t=&w=
     /media/render/<file> · /media/ref/<file> · /media/song/<id> · /media/grab/<name>     (all byte ranges)
GET  /api/plan/<slug> {slug, draft, version_files, can_undo, can_redo,
                     timeline:{duration, fps, pieces:[{id,sec,seg,kind,track:"V1"|"V2",start,end,clip,clip_rel,
                     in,out,label,captions:[{t0,t1,text}],overlays:[{kind,text,t0,t1,position}]}],
                     sections:[{index,name,start,end,music|null,beats:[timeline s]}]}}
                     Piece ids hash the segment's content, so they are stable when a segment is unchanged
                     (even if it moved).
PUT  /api/plan/<slug> {plan, force?} -> same shape | 400 {errors}
POST /api/plan/<slug>/op   ops: move{seg,to} (to = index after removal) · trim{seg,edge,delta} · split{t} ·
                     delete{seg,ripple} · insert{at_t,segment} · overwrite{at_t,segment} · freeze{t,dur} ·
                     set{seg,fields} (null value deletes a key) · set_style{fields} · add_section{at,name,music} ·
                     set_music{sec,music} · add_cutaway{seg,cutaway} · add_overlay{seg,overlay}
                     -> {draft,timeline,can_undo,can_redo} | 400 {error}; emits SSE plan_changed{slug,by:"hand"}
POST /api/plan/<slug>/undo|redo -> same shape · /commit -> {name, version} · POST /api/plan/new {title} -> plan shape
POST /api/render {plan_file, preview} -> {job} (SSE job ticks, then render_done{file,plan})
POST /api/qa {video} -> {job}
GET  /api/jobs {jobs:[{id,kind,label,status,progress,started,eta_s,log_tail}]}
GET  /api/events  SSE: "event: <name>\\ndata: <json incl. type>" — plan_changed, job, governor (2 s), render_done,
                     notes, markers, overnight (2 s while running); ": ping" every 15 s
GET  /api/markers?plan= {markers:[{id,plan,t,text,frame_url,status,kind,created}]}; POST {plan,t,text,kind} -> marker;
     PATCH /api/markers (body id) or /api/markers/<id> -> marker; DELETE ?id= or /<id> -> {ok}
POST /api/grab {source,clip?,t,file?} -> {url,source,t}
POST /api/context -> {ok}; GET /api/context -> last posted
POST /api/chat {message,attachments,context} -> SSE "data: <json>" stream: text / tool / tool_result /
     plan_changed / done{session} / error{message}
POST /api/chat/stop · GET /api/chat/history {session,messages:[{role,text,tools,attachments}]} · POST /api/chat/new
GET  /api/governor {health, load:{now,limit,cores}, ram:{free_gb,total_gb,min_free_gb},
                     disk:{free_gb,min_free_gb,path}, slots:[{name,used,max,holders:[{job,pid,since}]}], throttle, updated}
POST /api/overnight {action:start|stop|status} -> {running,started,stage,stages:[{name,done,total,status}],eta_s,report_md}
"""
import argparse
import copy
import hashlib
import json
import math
import mimetypes
import os
import queue
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WEB = Path(__file__).resolve().parent.parent
LOCK = threading.RLock()
VERBOSE = False
MEDIA = Path(".")

PROXY_DURS = [34.0, 30.0, 38.0, 32.0, 36.0, 40.0]
SONGS = [
    {"id": "s_drive", "title": "Open road", "duration": 150.0, "tempo": 96.0},
    {"id": "s_summer", "title": "Summer drive", "duration": 150.0, "tempo": 120.0},
    {"id": "s_night", "title": "Night lights", "duration": 150.0, "tempo": 128.0},
]
SONG_BY_ID = {s["id"]: s for s in SONGS}

SETTINGS = ["kitchen", "street", "beach", "office", "car", "gym", "restaurant", "balcony", "park", "hotel",
            "airport", "cafe"]
TIMES = ["day", "golden", "night", "indoor"]
PEOPLE = ["creator", "creator + friend", "strangers", "none"]
SHOTS = ["wide", "medium", "close", "pov", "drone", "detail", "timelapse"]
CAMERAS = ["locked", "handheld", "gimbal", "moving-vehicle"]
ACTIONS = ["walking", "driving", "cooking", "typing", "laughing", "talking", "eating", "swimming", "packing"]
MOODS = ["calm", "hype", "funny", "cinematic", "moody"]
TRIPS = ["01-city", "03-coast", "04-mountains", "06-trip", "07-home", "09-roadtrip", "11-festival", "12-holiday"]
SUMMARY = {
    "kitchen": "making coffee at the counter", "street": "walking through a busy street",
    "beach": "waves rolling in on the shore", "office": "working at the desk", "car": "driving with the window down",
    "gym": "a set on the bench", "restaurant": "dinner arriving at the table", "balcony": "looking out over the city",
    "park": "a slow walk under the trees", "hotel": "checking into the room", "airport": "waiting at the gate",
    "cafe": "ordering at the counter",
}
SENTENCES = [
    "okay so today we are heading out early before the traffic gets bad",
    "honestly this is the best coffee I have had all week",
    "I did not plan any of this it just kind of happened",
    "the light right now is exactly why we got up at six",
    "so the plan is drive up the coast and see where we end up",
    "I want to show you the place we found last time",
    "this is the part of the day I always forget to film",
    "we have about two hours before the sun goes down",
    "look at that view you cannot get this anywhere else",
    "alright we made it and it was absolutely worth it",
    "the food here is unreal you have to try it",
    "let me know in the comments if you want the full route",
]


def h(s, n=10):
    return hashlib.sha1(str(s).encode()).hexdigest()[:n]


# ----------------------------------------------------------------------------- library
def build_clips(n=5000):
    rnd = random.Random(7)
    clips = []
    for i in range(n):
        cid = h(f"clip-{i}")
        p = int(cid, 16) % 6
        setting = rnd.choice(SETTINGS)
        tod = "indoor" if setting in ("kitchen", "office", "gym", "hotel") and rnd.random() < .7 else rnd.choice(TIMES)
        people = rnd.choices(PEOPLE, weights=[5, 2, 2, 3])[0]
        creator = people.startswith("creator")
        talking = creator and rnd.random() < .45
        has_tr = talking or rnd.random() < .35
        tagged = rnd.random() < .55
        year = rnd.choice([2024, 2025])
        trip = rnd.choice(TRIPS)
        month = int(trip[:2])
        day = rnd.randint(1, 28)
        hh = {"day": rnd.randint(9, 15), "golden": rnd.choice([7, 18]), "night": rnd.randint(19, 23),
              "indoor": rnd.randint(8, 22)}[tod]
        clips.append({
            "id": cid, "rel": f"{year}/{trip}/C{i + 1:04d}.MP4", "duration": PROXY_DURS[p],
            "width": rnd.choice([3840, 1920, 3840, 1080]), "height": 0, "fps": rnd.choice([25.0, 29.97, 50.0, 59.94]),
            "quality": rnd.choices([1, 2, 3, 4, 5], weights=[1, 2, 4, 4, 2])[0] if tagged else None,
            "shake": round(min(12.0, max(0.5, rnd.lognormvariate(1.1, 0.6))), 2),
            "has_transcript": has_tr,
            "shot_at": f"{year}-{month:02d}-{day:02d}T{hh:02d}:{rnd.randint(0, 59):02d}:{rnd.randint(0, 59):02d}",
            "_proxy": p, "_talking": talking,
            "tags": ({"summary": ("talking to camera, " if talking else "") + SUMMARY[setting], "setting": setting,
                      "time_of_day": tod, "people": people, "creator_visible": creator,
                      "talking_to_camera": talking, "shot": "medium" if talking else rnd.choice(SHOTS)}
                     if tagged else {}),
            "_extra": {"actions": rnd.sample(ACTIONS, 2), "camera": rnd.choice(CAMERAS),
                       "mood": rnd.sample(MOODS, 2), "usable": rnd.random() > .08,
                       "topics": rnd.sample(["coffee", "travel", "food", "sunset", "work", "friends", "ocean",
                                             "city", "nature", "night"], 3)},
        })
        c = clips[-1]
        c["height"] = {3840: 2160, 1920: 1080, 1080: 1920}[c["width"]]
    return clips


CLIPS = build_clips()
CLIP = {c["id"]: c for c in CLIPS}


def public_row(c):
    return {k: v for k, v in c.items() if not k.startswith("_")}


def clip_words(c):
    if not c["has_transcript"]:
        return []
    rnd = random.Random(c["id"])
    out, t = [], 0.4
    while t < c["duration"] - 1:
        for w in rnd.choice(SENTENCES).split():
            d = 0.22 + len(w) * 0.035 + rnd.random() * 0.08
            if t + d > c["duration"] - 0.2:
                break
            out.append([round(t, 2), round(t + d, 2), w])
            t += d + 0.04
        t += 0.35 + rnd.random() * 0.9
    return out


def clip_detail(c):
    rnd = random.Random("d" + c["id"])
    d = public_row(c)
    d["codec"] = "hevc" if c["width"] == 3840 else "h264"
    tags = dict(c["tags"])
    if tags:
        tags.update({k: v for k, v in c["_extra"].items()})
        tags["quality"] = c["quality"]
        tags["notes"] = ""
    d["tags"] = tags
    metrics, t = [], 0.0
    base_exp = {"day": 140, "golden": 120, "night": 55, "indoor": 100}.get(c["tags"].get("time_of_day", "day"), 110)
    while t < c["duration"] - 0.01:
        t1 = min(c["duration"], t + 2.0)
        metrics.append({"t0": round(t, 2), "t1": round(t1, 2),
                        "shake": round(max(0.2, c["shake"] + rnd.gauss(0, 1.5)), 2),
                        "motion": round(abs(rnd.gauss(3, 2)), 2),
                        "exposure": round(min(250, max(10, base_exp + rnd.gauss(0, 25))), 1),
                        "sharpness": round(abs(rnd.gauss(180, 60)), 1),
                        "faces": (1 if c["tags"].get("creator_visible") else 0) if rnd.random() > .2 else 0})
        t = t1
    d["metrics"] = metrics
    shots, t, i = [], 0.0, 0
    while t < c["duration"] - 0.5:
        e = min(c["duration"], t + rnd.uniform(4, 14))
        shots.append({"idx": i, "start": round(t, 2), "end": round(e, 2)})
        t, i = e, i + 1
    d["shots"] = shots
    d["words"] = clip_words(c)
    d["sheet_url"] = f"/media/sheet/{c['id']}"
    return d


# ----------------------------------------------------------------------------- resolver
def stem(c):
    return Path(c["rel"]).stem if c else ""


def seg_key(seg):
    return h(json.dumps(seg, sort_keys=True), 8)


def song_beats(music, sec_start, sec_end):
    if not music:
        return []
    s = SONG_BY_ID.get(music.get("song"))
    if not s:
        return []
    per = 60.0 / s["tempo"]
    off = float(music.get("offset", 0))
    k = math.ceil(off / per - 1e-9)
    out = []
    while True:
        tl = sec_start + (k * per - off)
        if tl >= sec_end - 1e-6:
            break
        if tl >= sec_start - 1e-6:
            out.append(round(tl, 3))
        k += 1
    return out


def talk_chunks(seg):
    """Simulated tightening: cut every pause between words longer than gap + 0.25 s (keeps gap/2 each side).
    Depends only on absolute source times, so splitting a talk segment keeps the timeline identical."""
    a, b = float(seg["in"]), float(seg["out"])
    if not seg.get("tighten", True):
        return [(a, b)]
    gap = float(seg.get("gap", 0.35))
    ws = [w for w in words_of(seg.get("clip")) if w[1] > a and w[0] < b]
    chunks, t = [], a
    for w0, w1 in zip(ws, ws[1:]):
        if w1[0] - w0[1] > gap + 0.25:
            e, n = w0[1] + gap / 2, w1[0] - gap / 2
            if a < e < n < b:
                chunks.append((t, e))
                t = n
    chunks.append((t, b))
    return [(x, y) for x, y in chunks if y - x > 0.05] or [(a, b)]


def captions_for(c, words, a, b, start, speed=1.0):
    ws = [w for w in words if a <= w[0] < b]
    caps = []
    for i in range(0, len(ws), 4):
        grp = ws[i:i + 4]
        caps.append({"t0": round(start + (grp[0][0] - a) / speed, 3),
                     "t1": round(start + (min(grp[-1][1], b) - a) / speed, 3),
                     "text": " ".join(w[2] for w in grp)})
    return caps


WORDS_CACHE = {}


def words_of(cid):
    if cid not in WORDS_CACHE:
        WORDS_CACHE[cid] = clip_words(CLIP[cid]) if cid in CLIP else []
    return WORDS_CACHE[cid]


def resolve(plan):
    pieces, sections = [], []
    t = 0.0
    seen = {}
    style_caps = plan.get("style", {}).get("captions", {}).get("enabled", True)

    def add(si, gi, seg, sub, kind, track, start, end, clip=None, a=None, b=None, label=None, caps=None, ovs=None):
        key = f"{seg_key(seg)}-{sub}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        c = CLIP.get(clip) if clip else None
        pieces.append({"id": "p" + h(f"{key}#{n}", 8), "sec": si, "seg": gi, "kind": kind, "track": track,
                       "start": round(start, 3), "end": round(end, 3), "clip": clip,
                       "clip_rel": c["rel"] if c else None,
                       "in": None if a is None else round(a, 3), "out": None if b is None else round(b, 3),
                       "label": label or (f"{stem(c)} · {kind}" if c else kind),
                       "captions": caps or [], "overlays": ovs or []})

    for si, sec in enumerate(plan.get("sections", [])):
        s0 = t
        music = sec.get("music")
        song = SONG_BY_ID.get((music or {}).get("song"))
        per = 60.0 / song["tempo"] if song else None
        for gi, seg in enumerate(sec.get("segments", [])):
            typ = seg.get("type")
            g0 = t
            if typ == "talk":
                cid = seg.get("clip")
                speed = float(seg.get("speed", 1.0) or 1.0)
                spans = []
                for k, (a, b) in enumerate(talk_chunks(seg)):
                    caps = captions_for(CLIP.get(cid), words_of(cid), a, b, t, speed) \
                        if (seg.get("captions", True) and style_caps) else []
                    add(si, gi, seg, k, "talk", "V1", t, t + (b - a) / speed, cid, a, b, caps=caps)
                    spans.append((a, b, t))
                    t += (b - a) / speed

                def src_to_tl(s):
                    for a, b, st in spans:
                        if s <= b:
                            return st + max(0.0, s - a) / speed
                    return t
                ovs = []
                for o in seg.get("overlays", []) or []:
                    o0 = src_to_tl(float(seg["in"]) + float(o.get("at", 0)))
                    ovs.append({"kind": o.get("kind", "callout"), "text": o.get("text", ""),
                                "t0": round(o0, 3), "t1": round(min(t, o0 + float(o.get("dur", 2.0))), 3),
                                "position": o.get("position", "auto")})
                if ovs:
                    for p in pieces:
                        if p["sec"] == si and p["seg"] == gi and p["track"] == "V1":
                            p["overlays"] = [o for o in ovs if o["t0"] < p["end"] and o["t1"] > p["start"]]
                for k, cw in enumerate(seg.get("cutaways", []) or []):
                    c0 = src_to_tl(float(seg["in"]) + float(cw.get("at", 0)))
                    dur = float(cw.get("dur", 2.0))
                    add(si, gi, {"cw": cw, "of": seg_key(seg)}, f"cw{k}", "broll", "V2", c0, min(t, c0 + dur),
                        cw.get("clip"), float(cw.get("in", 0)), float(cw.get("in", 0)) + dur)
            elif typ == "broll":
                speed = float(seg.get("speed", 1.0) or 1.0)
                a, b = float(seg["in"]), float(seg["out"])
                d = (b - a) / speed
                ovs = [{"kind": o.get("kind", "callout"), "text": o.get("text", ""), "t0": round(t + float(o.get("at", 0)), 3),
                        "t1": round(min(t + d, t + float(o.get("at", 0)) + float(o.get("dur", 2))), 3),
                        "position": o.get("position", "auto")} for o in seg.get("overlays", []) or []]
                add(si, gi, seg, 0, "broll", "V1", t, t + d, seg.get("clip"), a, b, ovs=ovs)
                t += d
            elif typ == "montage":
                bpc = max(1, int(seg.get("beats_per_cut", 1) or 1))
                if seg.get("beat_sync", True) and per:
                    L = per * bpc
                    while L < float(seg.get("min_shot", 0.4)):
                        L += per
                else:
                    L = float(seg.get("max_shot", 1.6))
                for k, sh in enumerate(seg.get("shots", [])):
                    a = float(sh.get("in", 0))
                    d = min(L, float(sh["out"]) - a) if sh.get("out") is not None else L
                    add(si, gi, seg, k, "montage", "V1", t, t + d, sh.get("clip"), a, a + d)
                    t += d
            elif typ == "matchcut":
                A, B = seg.get("a", {}), seg.get("b", {})
                pre, post = float(seg.get("pre", 0.8)), float(seg.get("post", 1.4))
                add(si, gi, seg, "a", "matchcut", "V1", t, t + pre, A.get("clip"), float(A["t"]) - pre, float(A["t"]))
                t += pre
                add(si, gi, seg, "b", "matchcut", "V1", t, t + post, B.get("clip"), float(B["t"]), float(B["t"]) + post)
                t += post
            elif typ == "title":
                d = float(seg.get("dur", 2.0))
                add(si, gi, seg, 0, "title", "V1", t, t + d, label=f"“{seg.get('text', '')}”",
                    ovs=[{"kind": "title", "text": seg.get("text", ""), "t0": round(t, 3), "t1": round(t + d, 3),
                          "position": "center"}])
                t += d
            elif typ == "black":
                d = float(seg.get("dur", 1.0))
                add(si, gi, seg, 0, "black", "V1", t, t + d, label="black")
                t += d
            elif typ == "still":
                d = float(seg.get("dur", 2.0))
                cid = seg.get("clip")
                add(si, gi, seg, 0, "still", "V1", t, t + d, cid, float(seg.get("t", 0)), float(seg.get("t", 0)),
                    label=f"{stem(CLIP.get(cid))} · still")
                t += d
            del g0
        sections.append({"index": si, "name": sec.get("name", f"section {si + 1}"), "start": round(s0, 3),
                         "end": round(t, 3), "music": music or None, "beats": song_beats(music, s0, t)})
    return {"duration": round(t, 3), "fps": 25, "pieces": pieces, "sections": sections}


def seg_spans(tl):
    """{(si,gi): [start,end]} over V1 pieces."""
    spans = {}
    for p in tl["pieces"]:
        if p["track"] != "V1":
            continue
        k = (p["sec"], p["seg"])
        if k in spans:
            spans[k][0] = min(spans[k][0], p["start"])
            spans[k][1] = max(spans[k][1], p["end"])
        else:
            spans[k] = [p["start"], p["end"]]
    return spans


# ----------------------------------------------------------------------------- plans
def _clips_where(pred, n, seed):
    rnd = random.Random(seed)
    pool = [c for c in CLIPS if pred(c)]
    return rnd.sample(pool, n)


def default_plans():
    talkers = _clips_where(lambda c: c["_talking"] and c["has_transcript"], 12, 1)
    br = _clips_where(lambda c: not c["_talking"] and c["tags"], 60, 2)
    bi = iter(br)
    ti = iter(talkers)

    def nb():
        return next(bi)["id"]

    def shots(n):
        return [{"clip": nb(), "in": round(random.Random(n + i).uniform(1, 12), 2)} for i in range(n)]

    t1, t2, t3, t4, t5 = (next(ti)["id"] for _ in range(5))
    day = {
        "title": "Day trip", "version": 2, "output": {"width": 1920, "height": 1080, "fps": 25},
        "style": {"captions": {"enabled": True, "size": 54, "position": "bottom", "max_words": 5},
                  "grade": {"lut": "", "normalize_exposure": True}, "voice": {"clean": True, "atten_lim_db": 20},
                  "music_under_speech": "mute"},
        "sections": [
            {"name": "cold open", "music": {"song": "s_summer", "offset": 32.0, "gain_db": -1},
             "segments": [
                 {"type": "talk", "clip": t1, "in": 8.2, "out": 10.6, "tighten": False, "note": "the hook line"},
                 {"type": "montage", "beat_sync": True, "beats_per_cut": 2, "shots": shots(6),
                  "note": "best moments of the day"}]},
            {"name": "morning", "music": {"song": "s_drive", "offset": 0.0, "gain_db": -4},
             "segments": [
                 {"type": "talk", "clip": t2, "in": 3.0, "out": 15.0,
                  "cutaways": [{"clip": nb(), "in": 3.0, "at": 5.5, "dur": 2.4}],
                  "overlays": [{"kind": "callout", "text": "6:40 AM", "at": 0.4, "dur": 2.0, "position": "auto"}],
                  "note": "sets up the day"},
                 {"type": "broll", "clip": nb(), "in": 1.0, "out": 3.5, "note": "leaving the house"},
                 {"type": "talk", "clip": t3, "in": 12.0, "out": 20.0},
                 {"type": "broll", "clip": nb(), "in": 4.0, "out": 6.5},
                 {"type": "still", "clip": nb(), "t": 8.0, "dur": 1.5, "zoom": 1.06}]},
            {"name": "the drive", "music": {"song": "s_night", "offset": 20.0, "gain_db": -3},
             "segments": [
                 {"type": "montage", "beat_sync": True, "beats_per_cut": 2, "shots": shots(8)},
                 {"type": "broll", "clip": nb(), "in": 2.0, "out": 4.8},
                 {"type": "broll", "clip": nb(), "in": 6.0, "out": 8.2, "speed": 1.0},
                 {"type": "talk", "clip": t4, "in": 5.0, "out": 13.0,
                  "cutaways": [{"clip": nb(), "in": 1.0, "at": 3.0, "dur": 2.0}]}]},
            {"name": "day to night",
             "segments": [
                 {"type": "title", "text": "Later that night", "sub": "", "dur": 1.6},
                 {"type": "matchcut", "a": {"clip": nb(), "t": 14.2}, "b": {"clip": nb(), "t": 3.1},
                  "pre": 0.8, "post": 1.6, "note": "same framing, day to night"},
                 {"type": "broll", "clip": nb(), "in": 3.0, "out": 6.0}]},
            {"name": "out", "music": {"song": "s_summer", "offset": 96.0, "gain_db": -2},
             "segments": [
                 {"type": "talk", "clip": t5, "in": 10.0, "out": 20.0,
                  "overlays": [{"kind": "lower_third", "text": "Coast road", "sub": "day 2", "at": 1.0, "dur": 2.5,
                                "position": "bottom-left"}]},
                 {"type": "montage", "beat_sync": True, "beats_per_cut": 2, "shots": shots(5)},
                 {"type": "broll", "clip": nb(), "in": 2.0, "out": 5.0, "note": "last shot, alone"},
                 {"type": "black", "dur": 0.5}]},
        ],
    }
    t6 = next(ti)["id"]
    city = {
        "title": "City at night", "version": 1, "output": {"width": 1080, "height": 1920, "fps": 30},
        "style": {"captions": {"enabled": True}},
        "sections": [{"name": "main", "music": {"song": "s_night", "offset": 8.0},
                      "segments": [{"type": "talk", "clip": t6, "in": 2.0, "out": 9.0},
                                   {"type": "montage", "beats_per_cut": 2, "shots": shots(4)},
                                   {"type": "broll", "clip": nb(), "in": 1.0, "out": 4.0}]}],
    }
    return day, city


class PlanState:
    def __init__(self, slug, draft, versions):
        self.slug, self.draft, self.versions = slug, draft, versions
        self.undo, self.redo = [], []
        self.updated = time.time()

    def payload(self):
        return {"slug": self.slug, "draft": self.draft, "version_files": list(self.versions),
                "can_undo": bool(self.undo), "can_redo": bool(self.redo), "timeline": resolve(self.draft)}

    def push(self):
        self.undo.append(copy.deepcopy(self.draft))
        self.undo = self.undo[-200:]
        self.redo.clear()
        self.updated = time.time()


PLANS = {}


def init_plans():
    day, city = default_plans()
    PLANS["daytrip"] = PlanState("daytrip", day, ["daytrip_v1.json", "daytrip_v2.json"])
    PLANS["city-night"] = PlanState("city-night", city, ["city-night_v1.json"])


# ----------------------------------------------------------------------------- ops
class OpError(Exception):
    pass


def _seg(plan, ref):
    try:
        si, gi = int(ref[0]), int(ref[1])
        return plan["sections"][si]["segments"][gi], si, gi
    except (TypeError, ValueError, IndexError, KeyError):
        raise OpError(f"no segment at {ref}")


def seg_duration(seg, sec=None):
    tl = resolve({"sections": [{"music": (sec or {}).get("music"), "segments": [seg]}]})
    return tl["duration"]


def piece_at(tl, t):
    for p in tl["pieces"]:
        if p["track"] == "V1" and p["start"] <= t < p["end"]:
            return p
    v1 = [p for p in tl["pieces"] if p["track"] == "V1"]
    if v1 and abs(t - v1[-1]["end"]) < 1e-3:
        return v1[-1]
    return None


def src_time(p, t):
    if p["in"] is None:
        return None
    if p["kind"] == "still":
        return p["in"]
    span = p["end"] - p["start"]
    srcspan = (p["out"] - p["in"]) if p["out"] is not None else span
    return p["in"] + (t - p["start"]) * (srcspan / span if span else 1)


def split_seg(plan, tl, t):
    p = piece_at(tl, t)
    if not p:
        raise OpError(f"nothing on V1 at {t:.2f}s")
    si, gi = p["sec"], p["seg"]
    seg = plan["sections"][si]["segments"][gi]
    typ = seg["type"]
    spans = seg_spans(tl)[(si, gi)]
    if t - spans[0] < 0.04 or spans[1] - t < 0.04:
        return si, gi + (1 if spans[1] - t < 0.04 else 0), False   # on a cut already
    if typ in ("talk", "broll"):
        ts = src_time(p, t)
        a, b = copy.deepcopy(seg), copy.deepcopy(seg)
        a["out"] = round(ts, 3)
        b["in"] = round(ts, 3)
        rel = ts - float(seg["in"])
        for key in ("cutaways", "overlays"):
            if seg.get(key):
                a[key] = [x for x in seg[key] if float(x.get("at", 0)) < rel]
                b[key] = [dict(x, at=round(float(x.get("at", 0)) - rel, 3)) for x in seg[key] if float(x.get("at", 0)) >= rel]
        b.pop("note", None)
    elif typ in ("title", "black", "still"):
        d1 = t - spans[0]
        a, b = copy.deepcopy(seg), copy.deepcopy(seg)
        a["dur"] = round(d1, 3)
        b["dur"] = round(float(seg.get("dur", 1.0)) - d1, 3)
    elif typ == "montage":
        k = [q for q in tl["pieces"] if q["track"] == "V1" and q["sec"] == si and q["seg"] == gi].index(p)
        cut = k + (1 if t - p["start"] > (p["end"] - p["start"]) / 2 else 0)
        if cut <= 0 or cut >= len(seg["shots"]):
            return si, gi + (1 if cut > 0 else 0), False
        a, b = copy.deepcopy(seg), copy.deepcopy(seg)
        a["shots"], b["shots"] = seg["shots"][:cut], seg["shots"][cut:]
    else:
        raise OpError("a matchcut can't be split; trim its pre/post instead")
    plan["sections"][si]["segments"][gi:gi + 1] = [a, b]
    return si, gi + 1, True


def consume(plan, si, gi, D):
    segs = plan["sections"][si]["segments"]
    sec = plan["sections"][si]
    while D > 1e-3 and gi < len(segs):
        s = segs[gi]
        d = seg_duration(s, sec)
        if d <= D + 1e-3:
            segs.pop(gi)
            D -= d
            continue
        if s["type"] in ("talk", "broll"):
            s["in"] = round(float(s["in"]) + D * float(s.get("speed", 1.0) or 1.0), 3)
        elif s["type"] in ("title", "black", "still"):
            s["dur"] = round(float(s["dur"]) - D, 3)
        elif s["type"] == "montage":
            s["shots"] = s["shots"][max(1, int(D)):] or s["shots"][-1:]
        D = 0


def insert_pos(plan, tl, at_t):
    spans = seg_spans(tl)
    if not plan.get("sections"):
        plan["sections"] = [{"name": "main", "segments": []}]
        return 0, 0
    best, bd = None, 1e9
    for (si, gi), (a, b) in spans.items():
        for pos, edge in (((si, gi), a), ((si, gi + 1), b)):
            if abs(edge - at_t) < bd:
                best, bd = pos, abs(edge - at_t)
    if best is None:
        return 0, len(plan["sections"][0]["segments"])
    return best


def apply_op(ps, body):
    plan = ps.draft
    op = body.get("op")
    tl = resolve(plan)
    work = copy.deepcopy(plan)
    if op == "move":
        seg, si, gi = _seg(work, body.get("seg"))
        to = body.get("to")
        work["sections"][si]["segments"].pop(gi)
        tsi, tgi = int(to[0]), int(to[1])
        if not (0 <= tsi < len(work["sections"])):
            raise OpError(f"no section {tsi}")
        segs = work["sections"][tsi]["segments"]
        segs.insert(max(0, min(len(segs), tgi)), seg)
    elif op == "trim":
        seg, si, gi = _seg(work, body.get("seg"))
        edge, delta = body.get("edge"), float(body.get("delta", 0))
        typ = seg["type"]
        if typ in ("talk", "broll"):
            c = CLIP.get(seg.get("clip"))
            dur = c["duration"] if c else 1e9
            if edge == "in":
                seg["in"] = round(min(float(seg["out"]) - 0.2, max(0.0, float(seg["in"]) + delta)), 3)
            elif edge == "out":
                seg["out"] = round(max(float(seg["in"]) + 0.2, min(dur, float(seg["out"]) + delta)), 3)
            else:
                raise OpError("edge must be in or out")
        elif typ in ("title", "black", "still"):
            seg["dur"] = round(max(0.2, float(seg.get("dur", 1.0)) + (delta if edge == "out" else -delta)), 3)
        elif typ == "matchcut":
            if edge == "in":
                seg["pre"] = round(max(0.2, float(seg.get("pre", 0.8)) - delta), 3)
            else:
                seg["post"] = round(max(0.2, float(seg.get("post", 1.4)) + delta), 3)
        elif typ == "montage":
            sh = seg["shots"][0 if edge == "in" else -1]
            sh["in"] = round(max(0.0, float(sh.get("in", 0)) + (delta if edge == "in" else 0)), 3)
            if edge == "out":
                seg["max_shot"] = round(max(0.3, float(seg.get("max_shot", 1.6)) + delta), 3)
        else:
            raise OpError(f"can't trim {typ}")
    elif op == "split":
        split_seg(work, tl, float(body.get("t", 0)))
    elif op == "delete":
        seg, si, gi = _seg(work, body.get("seg"))
        if body.get("ripple"):
            work["sections"][si]["segments"].pop(gi)
        else:
            d = seg_spans(tl).get((si, gi), [0, 1])
            work["sections"][si]["segments"][gi] = {"type": "black", "dur": round(d[1] - d[0], 3)}
    elif op in ("insert", "overwrite"):
        new = body.get("segment")
        if not isinstance(new, dict) or new.get("type") not in ("talk", "broll", "montage", "matchcut", "title",
                                                                 "black", "still"):
            raise OpError("segment needs a valid type")
        at_t = float(body.get("at_t", 0))
        if op == "insert":
            si, gi = insert_pos(work, tl, at_t)
            work["sections"][si]["segments"].insert(gi, new)
        else:
            if not work.get("sections") or not tl["pieces"]:
                si, gi = insert_pos(work, tl, at_t)
                work["sections"][si]["segments"].insert(gi, new)
            else:
                si, gi, _ = split_seg(work, tl, at_t) if piece_at(tl, at_t) else (*insert_pos(work, tl, at_t), False)
                work["sections"][si]["segments"].insert(gi, new)
                consume(work, si, gi + 1, seg_duration(new, work["sections"][si]))
    elif op == "freeze":
        t = float(body.get("t", 0))
        p = piece_at(tl, t)
        if not p or not p["clip"]:
            raise OpError("freeze needs a clip under the playhead")
        ts = src_time(p, t)
        si, gi, _ = split_seg(work, tl, t) if p["kind"] != "matchcut" else (p["sec"], p["seg"] + 1, False)
        work["sections"][si]["segments"].insert(gi, {"type": "still", "clip": p["clip"], "t": round(ts, 3),
                                                     "dur": float(body.get("dur", 1.0)), "zoom": 1.0})
    elif op == "set":
        seg, si, gi = _seg(work, body.get("seg"))
        for k, v in (body.get("fields") or {}).items():
            if v is None:
                seg.pop(k, None)
            else:
                seg[k] = v
    elif op == "set_style":
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(a.get(k), dict):
                    merge(a[k], v)
                else:
                    a[k] = v
        work.setdefault("style", {})
        merge(work["style"], body.get("fields") or {})
    elif op == "add_section":
        at = int(body.get("at", len(work.get("sections", []))))
        sec = {"name": body.get("name") or "new section", "segments": []}
        if body.get("music"):
            sec["music"] = body["music"]
        work.setdefault("sections", []).insert(max(0, min(at, len(work["sections"]))), sec)
    elif op == "set_music":
        try:
            sec = work["sections"][int(body.get("sec"))]
        except (TypeError, ValueError, IndexError):
            raise OpError("no such section")
        if body.get("music"):
            sec["music"] = body["music"]
        else:
            sec.pop("music", None)
    elif op in ("add_cutaway", "add_overlay"):
        seg, si, gi = _seg(work, body.get("seg"))
        key = "cutaways" if op == "add_cutaway" else "overlays"
        if op == "add_cutaway" and seg["type"] != "talk":
            raise OpError("cutaways go on talk segments")
        item = body.get("cutaway" if op == "add_cutaway" else "overlay")
        if not isinstance(item, dict):
            raise OpError(f"{op} needs an object")
        seg.setdefault(key, []).append(item)
    else:
        raise OpError(f"unknown op {op!r}")
    ps.push()
    ps.draft = work


# ----------------------------------------------------------------------------- events / jobs
SUBS = []


def emit(name, data):
    data = dict(data, type=name)
    msg = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
    with LOCK:
        for q in list(SUBS):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


JOBS = []
RENDERS = []
JOB_N = [0]


def new_job(kind, label, seconds, on_done=None):
    with LOCK:
        JOB_N[0] += 1
        j = {"id": f"j{JOB_N[0]}", "kind": kind, "label": label, "status": "queued", "progress": 0.0,
             "started": time.time(), "eta_s": seconds, "log_tail": "queued behind the governor"}
        JOBS.append(j)

    def run():
        time.sleep(0.6)
        steps = int(seconds / 0.5)
        for i in range(1, steps + 1):
            with LOCK:
                j["status"] = "running"
                j["progress"] = round(i / steps, 3)
                j["eta_s"] = round(seconds * (1 - i / steps), 1)
                lines = [f"[{kind}] atom {k}/{steps} rendered" for k in range(max(1, i - 3), i + 1)]
                j["log_tail"] = "\n".join(lines)
            emit("job", j)
            time.sleep(0.5)
        with LOCK:
            j["status"], j["progress"], j["eta_s"] = "done", 1.0, 0
            j["log_tail"] += f"\n[{kind}] done"
        emit("job", j)
        if on_done:
            on_done()
    threading.Thread(target=run, daemon=True).start()
    return j


def governor_state():
    t = time.time()
    load = 4.2 + 2.8 * math.sin(t / 23) + 1.4 * math.sin(t / 7.3)
    free = 5.2 + 1.8 * math.sin(t / 31)
    throttle = None
    health = "ok"
    if load > 7.6:
        health, throttle = "warn", f"load {load:.1f} > 7.5, pausing transcribe"
    if free < 3.6:
        health = "warn"
        throttle = throttle or f"free RAM {free:.1f} GB < 3.5 GB, holding new ffmpeg"
    run = [j for j in JOBS if j["status"] == "running"]
    ff_holders = [{"job": j["id"], "pid": 40000 + int(j["id"][1:]), "since": j["started"]}
                  for j in run if j["kind"] in ("render", "qa")][:2]
    wh = [{"job": "overnight", "pid": 39001, "since": OVERNIGHT["started"]}] if OVERNIGHT["running"] else []
    return {"health": health, "load": {"now": round(load, 2), "limit": 7.5, "cores": 10},
            "ram": {"free_gb": round(free, 2), "total_gb": 16, "min_free_gb": 2},
            "disk": {"free_gb": round(612.4 - (t % 1000) / 500, 1), "min_free_gb": 20, "path": "/project"},
            "slots": [{"name": "ffmpeg", "used": len(ff_holders), "max": 2, "holders": ff_holders},
                      {"name": "whisper", "used": len(wh), "max": 1, "holders": wh}],
            "throttle": throttle, "updated": t}


OVERNIGHT = {"running": False, "started": None, "stage": None, "eta_s": None,
             "stages": [{"name": "index", "done": 5000, "total": 5000, "status": "done"},
                        {"name": "transcribe", "done": 3120, "total": 4100, "status": "pending"},
                        {"name": "tag", "done": 1870, "total": 5000, "status": "pending"},
                        {"name": "songs", "done": 3, "total": 3, "status": "done"},
                        {"name": "matchcuts", "done": 0, "total": 240, "status": "pending"}]}
REPORT_MD = """# Morning report

Overnight run finished its window at 06:58. The library is in better shape for editing.

## What got done
- **Transcribed** 412 clips (6.1 h of speech), 0 errors
- **Tagged** 960 clips from contact sheets; 41 marked unusable
- **Match cuts**: 38 candidate pairs above 0.8 similarity

## Worth a look
- 14 talking clips from the coast trip have strong hooks, see `cutroom search --speech "honestly"`
- Night footage from the festival is mostly too shaky (median shake 8.4)
- Two songs have no beat grid yet: re-run `cutroom songs index`

## Governor
- Throttled 3 times (load), 22 min total
- Peak RAM use 11.8 GB; disk never below 590 GB free

## Next
1. Finish tagging the remaining 3,130 clips (about 2 nights)
2. Re-run match cuts once tagging is done
"""


def overnight_status():
    o = dict(OVERNIGHT)
    o["stages"] = [dict(s) for s in OVERNIGHT["stages"]]
    o["report_md"] = REPORT_MD
    return o


def ticker():
    n = 0
    while True:
        time.sleep(2)
        n += 1
        emit("governor", governor_state())
        with LOCK:
            if OVERNIGHT["running"]:
                cur = next((s for s in OVERNIGHT["stages"] if s["status"] != "done"), None)
                if not cur:
                    OVERNIGHT["running"] = False
                    OVERNIGHT["stage"] = None
                else:
                    cur["status"] = "running"
                    OVERNIGHT["stage"] = cur["name"]
                    cur["done"] = min(cur["total"], cur["done"] + max(1, cur["total"] // 60))
                    if cur["done"] >= cur["total"]:
                        cur["status"] = "done"
                    left = sum(s["total"] - s["done"] for s in OVERNIGHT["stages"])
                    OVERNIGHT["eta_s"] = left * 2
                emit("overnight", overnight_status())


# ----------------------------------------------------------------------------- markers / chat
MARKERS = []
MARK_N = [0]
GRABS = {}
CONTEXT = {}
CHAT = {"session": None, "messages": [], "stop": threading.Event()}


def frame_url_at(slug, t):
    ps = PLANS.get(slug)
    if ps:
        p = piece_at(resolve(ps.draft), t)
        if p and p["clip"]:
            return f"/media/thumb/{p['clip']}?t={src_time(p, t):.2f}"
    return f"/media/thumb/{CLIPS[0]['id']}?t=mid"


def add_marker(plan, t, text, kind="note", status="open"):
    MARK_N[0] += 1
    m = {"id": f"m{MARK_N[0]}", "plan": plan, "t": round(float(t), 3), "text": text,
         "frame_url": frame_url_at(plan, float(t)), "status": status, "kind": kind, "created": time.time()}
    MARKERS.append(m)
    return m


def seed_markers():
    add_marker("daytrip", 3.2, "montage starts a beat late, pull it up", "note")
    add_marker("daytrip", 14.8, "this cutaway is too dark", "note")
    add_marker("daytrip", 41.0, "hold this shot longer", "note")
    add_marker("daytrip", 58.5, "caption typo: 'teh'", "note", status="done")


CHAT_REPLY = ("I looked at the **morning** section and the cut around the coffee line. The talk segment runs long "
              "after the punchline, so here is what I'd change:\n\n"
              "- Trim the tail of the talk at `C{n}` by about 0.8 s so it ends on the laugh\n"
              "- Swap the cutaway for a steadier shot (shake under 3)\n"
              "- Keep the callout, it still lands on the right word\n\n"
              "Everything else in the section stays frame-identical. Tell me if you want the montage tightened too.")


BINS = []


def seed_bins():
    rnd = random.Random(3)
    pool = [c for c in CLIPS[:600]]
    for name, title, why in (("best-moments", "Best moments", ["laughing at the table", "the wave hits right on cue",
                                                                  "golden light on the cliff", "the dog steals the show"]),
                             ("morning-shots", "Morning shots", ["steam off the coffee", "window light, steady",
                                                                  "packing the car", "first look at the sea"])):
        items = []
        for k in range(9 if name == "best-moments" else 6):
            c = rnd.choice(pool)
            t0 = round(rnd.uniform(1, c["duration"] - 5), 1)
            items.append({"clip": c["id"], "t0": t0, "t1": round(t0 + rnd.uniform(1.5, 4), 1), "why": why[k % len(why)]})
        BINS.append({"name": name, "title": title, "items": items, "updated": time.time() - rnd.randint(60, 9000)})


def chat_reply():
    talk = [c for c in CLIPS[:400] if c["_talking"]]
    br = [c for c in CLIPS[:400] if not c["_talking"]]
    a, b, c, d = br[0]["id"], br[1]["id"], br[2]["id"], talk[0]["id"]
    return ("I looked at the **morning** section. The talk in [[seg:1.0]] runs long after the punchline, "
            "around [[t:14.2]], so here is what I'd change:\n\n"
            f"- Trim the tail so it ends on the laugh at [[clip:{d}@12.4-15.0]]\n"
            f"- Swap the cutaway for a steadier shot: [[clip:{a}@6.0]] (shake 2.1)\n"
            "- Keep the callout, it still lands on the right word\n\n"
            "Steadier options for that cutaway, best first:\n\n"
            f"[[clip:{a}@6.0-9.0]] [[clip:{b}@3.5-6.0]] [[clip:{c}@11.0-13.5]]\n\n"
            "I saved the rest in [[bin:best-moments]]. The draft is in /Users/someone/EditRoom/plans/daytrip.draft.json.\n\n"
            "```\ncutroom search \"coffee\" --creator --max-shake 3\n12 clips\n```\n\n"
            "Everything else in the section stays frame-identical. Tell me if you want the montage tightened too.")


def chat_stream(handler, body):
    msg = str(body.get("message", ""))
    CHAT["stop"].clear()
    CHAT["messages"].append({"role": "user", "text": msg, "tools": [],
                             "attachments": body.get("attachments") or []})
    CHAT["session"] = CHAT["session"] or "s1"
    edits = bool(re.search(r"tighten|fix|edit|better|cut", msg, re.I))
    reply = chat_reply()
    tools = []
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True

    def send(obj):
        handler.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        handler.wfile.flush()

    try:
        tokens = re.findall(r"\S+\s*|\n+", reply)
        half = len(tokens) // 3
        out = []
        for i, tok in enumerate(tokens):
            if CHAT["stop"].is_set():
                send({"type": "error", "message": "stopped"})
                break
            if i == half:
                tool = {"type": "tool", "id": "t1", "name": "Bash",
                        "input": {"command": 'cutroom search "coffee" --creator --max-shake 3'},
                        "summary": 'ran cutroom search "coffee"'}
                send(tool)
                time.sleep(0.6)
                send({"type": "tool_result", "id": "t1", "ok": True, "text": "12 clips"})
                for k in range(3):
                    send({"type": "tool", "id": f"r{k}", "name": "Read",
                          "input": {"file_path": f"/Users/someone/EditRoom/frames/studio/{CLIPS[k]['id']}_{k}.jpg"}})
                    time.sleep(0.15)
                    send({"type": "tool_result", "id": f"r{k}", "ok": True, "text": "image"})
                tools.append({"name": "Bash", "summary": tool["summary"], "ok": True})
                if edits:
                    send({"type": "tool", "id": "t2", "name": "Edit", "input": {"file_path": "plans/daytrip.draft.json"},
                          "summary": "edited draft daytrip"})
                    time.sleep(0.4)
                    with LOCK:
                        ps = PLANS["daytrip"]
                        ps.push()
                        segs = ps.draft["sections"][1]["segments"]
                        talk = next((s for s in segs if s["type"] == "talk"), None)
                        if talk:
                            talk["out"] = round(float(talk["out"]) - 0.8, 3)
                        br = next((s for s in segs if s["type"] == "broll"), None)
                        if br:
                            br["clip"] = random.choice([c["id"] for c in CLIPS[:400] if not c["_talking"]])
                    send({"type": "tool_result", "id": "t2", "ok": True, "text": "draft saved"})
                    send({"type": "plan_changed", "slug": "daytrip"})
                    emit("plan_changed", {"slug": "daytrip", "by": "claude"})
                    tools.append({"name": "Edit", "summary": "edited draft daytrip", "ok": True})
            send({"type": "text", "text": tok})
            out.append(tok)
            time.sleep(0.03)
        else:
            if re.search(r"best|picks|moments", msg, re.I):
                with LOCK:
                    rnd = random.Random(len(BINS))
                    items = [{"clip": c["id"], "t0": 2.0, "t1": 5.0, "why": "strong moment"} for c in rnd.sample(CLIPS[:300], 5)]
                    BINS.insert(0, {"name": f"picks-{len(BINS)}", "title": "Saturday highlights", "items": items, "updated": time.time()})
                emit("bins_changed", {"bins": BINS})
            send({"type": "done", "session": CHAT["session"]})
        CHAT["messages"].append({"role": "assistant", "text": "".join(out), "tools": tools, "attachments": []})
    except (BrokenPipeError, ConnectionResetError):
        pass


# ----------------------------------------------------------------------------- HTTP
def qa_doc():
    def iss(sev, t, msg):
        m, s = divmod(t, 60)
        return {"severity": sev, "t": t, "t_end": None, "tc": f"{int(m):02d}:{s:04.1f}", "frame": int(t * 25),
                "msg": msg, "evidence": []}
    checks = [
        {"id": "silence", "category": "audio", "title": "Silence holes", "status": "pass", "value": 0, "detail": "",
         "rule": "R12", "issues": []},
        {"id": "offbeat_cuts", "category": "rhythm", "title": "Cuts off the beat", "status": "warn", "value": 2,
         "detail": "montage cuts more than 2 frames from a beat", "rule": "R41",
         "issues": [iss("warn", 4.1, "cut 3 frames after the beat"), iss("warn", 31.6, "cut 4 frames before the beat")]},
        {"id": "caption_face", "category": "graphics", "title": "Captions over faces", "status": "fail", "value": 1,
         "detail": "", "rule": "R77", "issues": [iss("fail", 12.4, "caption covers the speaker's mouth")]},
        {"id": "shaky", "category": "picture", "title": "Shaky shots", "status": "warn", "value": 1, "detail": "",
         "rule": "R20", "issues": [iss("warn", 44.0, "shake 8.9 in the drive montage")]},
        {"id": "repeat", "category": "story", "title": "Repeated shots", "status": "pass", "value": 0, "detail": "",
         "rule": "R5", "issues": []},
        {"id": "loudness", "category": "audio", "title": "Programme loudness", "status": "pass", "value": -14.1,
         "detail": "-14.1 LUFS / -1.6 dBTP", "rule": "R90", "issues": []},
    ]
    return {"video": "renders/daytrip_v2_preview.mp4", "version": 2, "created": "2026-10-18T22:14:03",
            "duration": 60.0, "fps": 25.0, "width": 1280, "height": 720,
            "summary": {"status": "fail", "fail": 1, "warn": 3, "checks": {c["id"]: c["status"] for c in checks},
                        "shots": 41, "cuts_detected": 40, "duration": 60.0, "loudness_lufs": -14.1,
                        "true_peak_dbtp": -1.6,
                        "top_issues": ["[00:12.4] FAIL caption_face: caption covers the speaker's mouth",
                                       "[00:04.1] WARN offbeat_cuts: cut 3 frames after the beat"],
                        "sheets": [], "runtime_s": 21.4},
            "checks": checks}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cutroom-mock/1"

    def log_message(self, fmt, *a):
        if VERBOSE:
            super().log_message(fmt, *a)

    # -- helpers
    def json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def file(self, path, ctype=None, cache=None):
        path = Path(path)
        if not path.is_file():
            return self.json({"error": f"not found: {path.name}"}, 404)
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            ctype = "text/javascript"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", cache or "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def thumb_path(self, cid, t):
        c = CLIP.get(cid)
        if not c:
            return None
        p = c["_proxy"]
        n = int(PROXY_DURS[p])
        if t in (None, "", "mid"):
            sec = n // 2
        else:
            try:
                sec = int(round(float(t)))
            except ValueError:
                sec = n // 2
        sec = max(0, min(n - 1, sec))
        return MEDIA / "thumbs" / str(p) / f"{sec}.jpg"

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = unquote(u.path)
        try:
            if path.startswith("/api/"):
                return self.api_get(path, q)
            if path.startswith("/media/"):
                return self.media(path, q)
            return self.static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def static(self, path):
        rel = path.lstrip("/") or "index.html"
        p = (WEB / rel).resolve()
        if WEB not in p.parents and p != WEB:
            return self.json({"error": "forbidden"}, 403)
        if p.is_dir():
            p = p / "index.html"
        if not p.is_file():
            return self.json({"error": f"not found: {rel}"}, 404)
        self.file(p, cache="no-cache")

    def media(self, path, q):
        parts = path.split("/")[2:]
        kind, rest = parts[0], "/".join(parts[1:])
        if kind == "proxy":
            c = CLIP.get(rest)
            return self.file(MEDIA / "proxy" / f"p{c['_proxy']}.mp4", "video/mp4") if c else self.json({"error": "no clip"}, 404)
        if kind == "sheet":
            c = CLIP.get(rest)
            return self.file(MEDIA / "sheets" / f"p{c['_proxy']}.jpg", "image/jpeg", "max-age=3600") if c else \
                self.json({"error": "no clip"}, 404)
        if kind in ("thumb", "frame"):
            p = self.thumb_path(rest, q.get("t"))
            return self.file(p, "image/jpeg", "max-age=3600") if p else self.json({"error": "no clip"}, 404)
        if kind == "render":
            p = MEDIA / "render" / rest
            if not p.is_file():
                if rest.endswith(".qa.json"):
                    return self.json(qa_doc())
                if rest.endswith(".json"):
                    ps = PLANS.get(rest.split("_v")[0]) or PLANS["daytrip"]
                    tl = resolve(ps.draft)
                    return self.json({"schema": "cutroom-render-v1", "duration": tl["duration"],
                                      "sections": tl["sections"], "segments": tl["pieces"]})
                p = MEDIA / "render" / "daytrip_v2_preview.mp4"
            return self.file(p, "video/mp4" if p.suffix == ".mp4" else None)
        if kind == "ref":
            return self.file(MEDIA / "ref" / rest, "video/mp4")
        if kind == "song":
            return self.file(MEDIA / "songs" / f"{rest}.m4a", "audio/mp4")
        if kind == "grab":
            src = GRABS.get(rest)
            return self.file(src, "image/jpeg", "max-age=3600") if src else self.json({"error": "no grab"}, 404)
        return self.json({"error": "unknown media"}, 404)

    def project(self):
        with LOCK:
            plans = [{"slug": s, "title": ps.draft.get("title", s), "draft": True, "versions": list(ps.versions),
                      "updated": ps.updated} for s, ps in PLANS.items()]
            return {"name": "Weekend edits", "root": "/path/project",
                    "status": {"clips": len(CLIPS), "hours": 412.6, "proxied": len(CLIPS), "transcribed": 3120,
                               "tagged": sum(1 for c in CLIPS if c["tags"]), "songs": len(SONGS)},
                    "plans": plans, "renders": list(reversed(RENDERS)), "songs": SONGS,
                    "references": [{"file": "ref_edit.mp4", "duration": 45.0}]}

    def api_get(self, path, q):
        if path == "/api/project":
            return self.json(self.project())
        if path == "/api/clips":
            rows = CLIPS
            qq = (q.get("q") or "").lower().strip()
            if qq:
                terms = qq.split()
                rows = [c for c in rows if all(t in (c["rel"] + " " + c["tags"].get("summary", "") + " " +
                                                     c["tags"].get("setting", "")).lower() for t in terms)]
            if q.get("setting"):
                rows = [c for c in rows if c["tags"].get("setting") == q["setting"]]
            if q.get("time"):
                rows = [c for c in rows if c["tags"].get("time_of_day") == q["time"]]
            if q.get("people"):
                rows = [c for c in rows if q["people"].lower() in (c["tags"].get("people") or "").lower()]
            if q.get("max_shake"):
                rows = [c for c in rows if c["shake"] <= float(q["max_shake"])]
            if q.get("min_quality"):
                rows = [c for c in rows if (c["quality"] or 0) >= int(float(q["min_quality"]))]
            if q.get("speech") in ("1", "true"):
                rows = [c for c in rows if c["has_transcript"]]
            if q.get("creator") in ("1", "true"):
                rows = [c for c in rows if c["tags"].get("creator_visible")]
            off, lim = int(q.get("offset", 0)), min(1000, int(q.get("limit", 200)))
            return self.json({"total": len(rows), "offset": off, "limit": lim,
                              "rows": [public_row(c) for c in rows[off:off + lim]]})
        m = re.fullmatch(r"/api/clip/([0-9a-f]+)", path)
        if m:
            c = CLIP.get(m.group(1))
            return self.json(clip_detail(c)) if c else self.json({"error": "no such clip"}, 404)
        m = re.fullmatch(r"/api/plan/([\w.-]+)", path)
        if m:
            with LOCK:
                ps = PLANS.get(m.group(1))
                return self.json(ps.payload()) if ps else self.json({"error": f"no plan {m.group(1)}"}, 404)
        if path == "/api/jobs":
            with LOCK:
                return self.json({"jobs": list(reversed(JOBS))})
        if path == "/api/events":
            return self.events()
        if path == "/api/markers":
            with LOCK:
                ms = [m for m in MARKERS if not q.get("plan") or m["plan"] == q["plan"]]
                return self.json({"markers": ms})
        if path == "/api/context":
            return self.json(CONTEXT)
        if path == "/api/chat/history":
            return self.json({"session": CHAT["session"], "messages": CHAT["messages"]})
        if path == "/api/governor":
            return self.json(governor_state())
        if path == "/api/bins":
            with LOCK:
                return self.json(BINS)
        if path == "/api/overnight":
            return self.json(overnight_status())
        return self.json({"error": f"unknown route {path}"}, 404)

    def events(self):
        qq = queue.Queue(maxsize=500)
        with LOCK:
            SUBS.append(qq)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.write(f"event: governor\ndata: {json.dumps(dict(governor_state(), type='governor'))}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = qq.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LOCK:
                if qq in SUBS:
                    SUBS.remove(qq)

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        b = self.body()
        m = re.fullmatch(r"/api/clip/([0-9a-f]+)/tags", path)
        if m:
            c = CLIP.get(m.group(1))
            if not c:
                return self.json({"error": "no such clip"}, 404)
            with LOCK:
                c["tags"].update(b.get("tags", b))
            return self.json({"ok": True, "tags": c["tags"]})
        m = re.fullmatch(r"/api/plan/([\w.-]+)", path)
        if m:
            with LOCK:
                ps = PLANS.get(m.group(1))
                if not ps:
                    return self.json({"error": "no such plan"}, 404)
                plan = b.get("plan", b)
                errs = [] if plan.get("sections") is not None else ["plan has no sections"]
                if errs and not b.get("force"):
                    return self.json({"errors": errs}, 400)
                ps.push()
                ps.draft = plan
                out = ps.payload()
            emit("plan_changed", {"slug": ps.slug, "by": "hand"})
            return self.json(out)
        return self.json({"error": "unknown route"}, 404)

    def do_PATCH(self):
        path = unquote(urlparse(self.path).path)
        b = self.body()
        m = re.fullmatch(r"/api/markers(?:/([\w-]+))?", path)
        if m:
            mid = m.group(1) or b.get("id")
            with LOCK:
                mk = next((x for x in MARKERS if x["id"] == mid), None)
                if not mk:
                    return self.json({"error": "no such marker"}, 404)
                for k in ("t", "text", "status", "kind"):
                    if k in b:
                        mk[k] = b[k]
            emit("markers", {"plan": mk["plan"]})
            return self.json(mk)
        return self.json({"error": "unknown route"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        m = re.fullmatch(r"/api/markers(?:/([\w-]+))?", path)
        if m:
            mid = m.group(1) or q.get("id") or self.body().get("id")
            with LOCK:
                mk = next((x for x in MARKERS if x["id"] == mid), None)
                if not mk:
                    return self.json({"error": "no such marker"}, 404)
                MARKERS.remove(mk)
            emit("markers", {"plan": mk["plan"]})
            return self.json({"ok": True})
        return self.json({"error": "unknown route"}, 404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        b = self.body()
        if path == "/api/chat":
            return chat_stream(self, b)
        if path == "/api/chat/stop":
            CHAT["stop"].set()
            return self.json({"ok": True})
        if path == "/api/chat/new":
            CHAT["session"] = None
            CHAT["messages"] = []
            return self.json({"ok": True, "session": None})
        if path == "/api/context":
            CONTEXT.clear()
            CONTEXT.update(b)
            return self.json({"ok": True})
        if path == "/api/plan/new":
            title = str(b.get("title") or "Untitled edit")
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "edit"
            with LOCK:
                base, n = slug, 2
                while slug in PLANS:
                    slug, n = f"{base}-{n}", n + 1
                PLANS[slug] = PlanState(slug, {"title": title, "version": 0, "style": {},
                                               "sections": [{"name": "main", "segments": []}]}, [])
                out = PLANS[slug].payload()
            return self.json(out)
        m = re.fullmatch(r"/api/plan/([\w.-]+)/(op|undo|redo|commit)", path)
        if m:
            slug, act = m.groups()
            with LOCK:
                ps = PLANS.get(slug)
                if not ps:
                    return self.json({"error": f"no plan {slug}"}, 404)
                if act == "op":
                    try:
                        apply_op(ps, b)
                    except OpError as e:
                        return self.json({"error": str(e)}, 400)
                    except (KeyError, TypeError, ValueError) as e:
                        return self.json({"error": f"bad op: {e}"}, 400)
                elif act in ("undo", "redo"):
                    src, dst = (ps.undo, ps.redo) if act == "undo" else (ps.redo, ps.undo)
                    if not src:
                        return self.json({"error": f"nothing to {act}"}, 400)
                    dst.append(copy.deepcopy(ps.draft))
                    ps.draft = src.pop()
                elif act == "commit":
                    v = len(ps.versions) + 1
                    name = f"{slug}_v{v}.json"
                    ps.versions.append(name)
                    ps.draft["version"] = v
                    return self.json({"name": name, "version": v})
                out = ps.payload()
            emit("plan_changed", {"slug": slug, "by": "hand"})
            return self.json(out)
        if path == "/api/render":
            pf = str(b.get("plan_file") or "daytrip_v2.json")
            preview = bool(b.get("preview", True))
            stem_ = Path(pf).stem
            slug = stem_.split("_v")[0]
            out = f"{stem_}{'_preview' if preview else ''}.mp4"

            def done():
                with LOCK:
                    prev = next((r for r in RENDERS if r["file"] == out), {})
                    RENDERS[:] = [r for r in RENDERS if r["file"] != out]
                    RENDERS.append({**prev, "file": out, "plan": slug,
                                    "version": int(re.findall(r"_v(\d+)", stem_)[0]) if "_v" in stem_ else 1,
                                    "preview": preview, "duration": 60.0, "created": time.time(),
                                    "qa": prev.get("qa"), "qa_status": prev.get("qa_status")})
                emit("render_done", {"file": out, "plan": slug})
            j = new_job("render", f"render {out}", 8.0, done)
            return self.json({"job": j["id"]})
        if path == "/api/qa":
            video = str(b.get("video") or "")

            def done():
                with LOCK:
                    for r in RENDERS:
                        if r["file"] == Path(video).name:
                            r["qa"] = Path(video).stem + ".qa.json"
                            r["qa_status"] = "warn"
            j = new_job("qa", f"qa {Path(video).name}", 4.0, done)
            return self.json({"job": j["id"]})
        if path == "/api/markers":
            plan = b.get("plan") or "daytrip"
            with LOCK:
                mk = add_marker(plan, float(b.get("t", 0)), str(b.get("text", "")), b.get("kind", "note"))
            emit("markers", {"plan": plan})
            return self.json(mk)
        if path == "/api/grab":
            src = b.get("source", "program")
            t = float(b.get("t", 0))
            if src == "clip" and b.get("clip") in CLIP:
                fp = self.thumb_path(b["clip"], t)
            elif src == "ref":
                fp = MEDIA / "thumbs" / "1" / f"{max(0, min(29, int(t)))}.jpg"
            else:
                ps = PLANS.get(b.get("plan") or "daytrip") or PLANS["daytrip"]
                p = piece_at(resolve(ps.draft), t)
                fp = self.thumb_path(p["clip"], src_time(p, t)) if p and p["clip"] else MEDIA / "thumbs" / "0" / "0.jpg"
            with LOCK:
                name = f"g_{len(GRABS) + 1}.jpg"
                GRABS[name] = fp
            return self.json({"url": f"/media/grab/{name}", "source": src, "t": t})
        if path == "/api/overnight":
            act = b.get("action", "status")
            with LOCK:
                if act == "start" and not OVERNIGHT["running"]:
                    OVERNIGHT["running"] = True
                    OVERNIGHT["started"] = time.time()
                    for s in OVERNIGHT["stages"]:
                        if s["status"] != "done":
                            s["status"] = "pending"
                elif act == "stop":
                    OVERNIGHT["running"] = False
                    OVERNIGHT["stage"] = None
                    for s in OVERNIGHT["stages"]:
                        if s["status"] == "running":
                            s["status"] = "pending"
                st = overnight_status()
            emit("overnight", st)
            return self.json(st)
        return self.json({"error": f"unknown route {path}"}, 404)


def main():
    global MEDIA, VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8611)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--media", default=os.environ.get("CUTROOM_MOCK_MEDIA", "mock_media"))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    MEDIA = Path(a.media).expanduser().resolve()
    VERBOSE = a.verbose
    if not (MEDIA / "proxy" / "p0.mp4").exists():
        print(f"warning: no media in {MEDIA}; run dev/make_media.sh {MEDIA}")
    init_plans()
    seed_markers()
    seed_bins()
    RENDERS.append({"file": "daytrip_v1_preview.mp4", "plan": "daytrip", "version": 1, "preview": True,
                    "duration": 58.2, "created": time.time() - 86400, "qa": "daytrip_v1_preview.qa.json",
                    "qa_status": "fail"})
    RENDERS.append({"file": "daytrip_v2_preview.mp4", "plan": "daytrip", "version": 2, "preview": True,
                    "duration": 60.0, "created": time.time() - 3600, "qa": "daytrip_v2_preview.qa.json",
                    "qa_status": "warn"})
    threading.Thread(target=ticker, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    srv.daemon_threads = True
    print(f"cutroom mock studio on http://{a.host}:{a.port}  (media: {MEDIA})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
