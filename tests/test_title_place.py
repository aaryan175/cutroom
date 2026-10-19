"""Typed-title placement: pos {x, y} / "auto" (titleplace.py), measured on synthetic frames and a small render.
Auto picks a quiet dark area for a yellow title, never a face/person box or the sun, and the contrast check
rejects yellow on white; the renderer draws the title where the plan (or the measurement) says; the factory's
extend stub now answers the typed-title-position request instead of asking."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from cutroom import titleplace as TP

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p"]
W, H, FPS = 640, 360, 25
YELLOW = "#FFCD00"


def _ff(*a):
    subprocess.run(FF + [str(x) for x in a], check=True)


def _frame(sky=(18, 28, 58), ground=(246, 246, 246), split=0.45, noise=3, seed=0):
    rng = np.random.default_rng(seed)
    f = np.empty((H, W, 3), np.uint8)
    f[: int(H * split)] = sky
    f[int(H * split):] = ground
    return np.clip(f.astype(int) + rng.integers(-noise, noise + 1, f.shape), 0, 255).astype(np.uint8)


def _inside(res, box_wh, region):
    """The chosen title box sits inside region [x0, y0, x1, y1]."""
    bw, bh = box_wh
    return (res["x"] - bw / 2 >= region[0] - 1e-6 and res["x"] + bw / 2 <= region[2] + 1e-6
            and res["y"] - bh / 2 >= region[1] - 1e-6 and res["y"] + bh / 2 <= region[3] + 1e-6)


BOX = (0.3, 0.1)


def test_contrast_rejects_yellow_on_white():
    assert TP.contrast((255, 205, 0), (255, 255, 255)) < 1.6           # yellow on white: unreadable
    assert TP.contrast((255, 205, 0), (20, 28, 58)) > 7                 # yellow on a dark sky: fine
    r = TP.score_frames([_frame(sky=(250, 250, 250))], BOX, YELLOW, detect=False)   # an all-white frame
    assert r["ok"] is False and r["backing"] is True and "contrast" in r["why"]
    assert not any(c["readable"] for c in r["candidates"])


def test_auto_picks_the_dark_sky_not_the_white_area():
    r = TP.score_frames([_frame(seed=s) for s in range(3)], BOX, YELLOW, detect=False)
    assert r["ok"] and not r["backing"] and r["contrast"] > 7
    assert r["y"] + BOX[1] / 2 <= 0.45 + 1e-6, r                          # wholly in the sky band
    white = [c for c in r["candidates"] if c["y"] - BOX[1] / 2 >= 0.45]
    assert white and not any(c["readable"] for c in white)               # every box on the white area fails contrast


def test_auto_avoids_a_face_box():
    face = [0.0, 0.0, 0.62, 0.45]                                         # a person over the left of the sky band
    r = TP.score_frames([_frame()], BOX, YELLOW, avoid=[face], detect=False)
    assert r["ok"] and r["x"] - BOX[0] / 2 >= 0.62 - 0.02, r
    hit = [c for c in r["candidates"] if "face/person" in c["rejected"]]
    assert hit


def test_auto_avoids_the_sun():
    import cv2
    f = _frame()
    cv2.circle(f, (int(0.25 * W), int(0.2 * H)), 34, (255, 255, 250), -1)  # a blown sun on the left of the sky
    r = TP.score_frames([f], BOX, YELLOW, detect=False)
    assert r["ok"]
    sun_box = [r["x"] - BOX[0] / 2, r["y"] - BOX[1] / 2, BOX[0], BOX[1]]
    from cutroom.qa import overlap
    assert overlap(sun_box, [0.25 - 34 / W, 0.2 - 34 / H, 68 / W, 68 / H]) == 0, r
    assert any(any("sun" in w for w in c["rejected"]) for c in r["candidates"])


def test_plan_pos_validation():
    from cutroom import plan as P
    assert P._validate_typed("t", {"text": "X", "pos": "auto"}) == []
    assert P._validate_typed("t", {"text": "X", "pos": {"x": 0.3, "y": 0.2}}) == []
    assert P._validate_typed("t", {"text": "X", "pos": {"x": 1.4, "y": 0.2}})
    assert P._validate_typed("t", {"text": "X", "pos": "top"})


def test_extend_stub_closes_built_capability_and_keeps_unknown():
    from cutroom.factory import phases

    class Ctx:
        def __init__(self, text):
            self.order = {"text": text, "id": "o1", "plan": "p"}
            self.said = []

        def progress(self, s, **k):
            self.said.append(s)
    out = phases.extend_stub(Ctx("typed_title_position"))
    assert out["state"] == "done" and out["capability"] == "typed_title_position" and '"auto"' in out["text"]
    assert phases.built_capability("move the typed title position")[0] == "typed_title_position"
    with pytest.raises(phases.NeedsYou):
        phases.extend_stub(Ctx("word-pop captions"))


# ------------------------------------------------------------------------------------------- render
def _yellow(img):
    r, g, b = (img[..., k].astype(int) for k in range(3))
    return (r > 180) & (g > 150) & (b < 110)


@pytest.fixture(scope="module")
def placed(tmp_path_factory):
    from cutroom import db, ingest
    from cutroom import render as R
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("titleplace")
    lib = base / "footage"
    lib.mkdir()
    # a: white picture with a dark-navy "sky" box top-left (the only place a yellow title reads)
    _ff("-f", "lavfi", "-i", f"color=c=0xf4f4f4:s={W}x{H}:r=25:d=6", "-f", "lavfi", "-i", "color=c=0x141c3a:s=330x150:r=25:d=6",
        "-f", "lavfi", "-i", "sine=f=330:d=6", "-filter_complex", "[0:v][1:v]overlay=x=0:y=0,noise=alls=5:allf=t[v]",
        "-map", "[v]", "-map", "2:a", *X264, "-c:a", "aac", "-shortest", lib / "a.mp4")
    # b: mid grey, for an explicit position
    _ff("-f", "lavfi", "-i", f"color=c=0x404448:s={W}x{H}:r=25:d=6", "-f", "lavfi", "-i", "sine=f=220:d=6",
        "-vf", "noise=alls=5:allf=t", *X264, "-c:a", "aac", "-shortest", lib / "b.mp4")
    # c: all white (no area reads a yellow title)
    _ff("-f", "lavfi", "-i", f"color=c=0xf6f6f6:s={W}x{H}:r=25:d=6", "-f", "lavfi", "-i", "sine=f=260:d=6",
        "-vf", "noise=alls=5:allf=t", *X264, "-c:a", "aac", "-shortest", lib / "c.mp4")
    cfg = init_project(base / "work", [lib])
    cfg.workers = 2
    cfg.out_width, cfg.out_height, cfg.preview_height = W, H, H
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    plan = {"title": "Place", "version": 1,
            "style": {"captions": {"enabled": False}, "voice": {"clean": False}, "loudness": {"I": -16, "TP": -1.5}},
            "sections": [{"name": "open", "segments": [
                {"type": "broll", "clip": "a.mp4", "in": 1.0, "out": 3.0, "audio": "natural",
                 "overlays": [{"kind": "typed_title", "text": "DAY", "at": 0.0, "dur": 2.0, "start": 0.0,
                               "char_s": 0.02, "sfx": None, "pos": "auto"}]},
                {"type": "broll", "clip": "b.mp4", "in": 1.0, "out": 3.0, "audio": "natural",
                 "overlays": [{"kind": "typed_title", "text": "DAY", "at": 0.0, "dur": 2.0, "start": 0.0,
                               "char_s": 0.02, "sfx": None, "pos": {"x": 0.72, "y": 0.78}}]}]}]}
    plan_p = cfg.p("plans", "place_v1.json")
    plan_p.parent.mkdir(parents=True, exist_ok=True)
    plan_p.write_text(json.dumps(plan))
    out = R.render_plan(cfg, con, plan_p, preview=True)
    side = json.loads(Path(out[:-4] + ".json").read_text())
    r = subprocess.run(FF[:-1] + ["-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return {"side": side, "frames": np.frombuffer(r.stdout, np.uint8).reshape(-1, H, W, 3), "cfg": cfg, "con": con,
            "lib": lib}


def _centroid(img):
    ys, xs = np.nonzero(_yellow(img))
    assert len(xs) > 30, "no title ink"
    return xs.mean() / W, ys.mean() / H


def test_render_explicit_pos(placed):
    fr, side = placed["frames"], placed["side"]
    tt = side["typed_titles"][1]
    assert tt["pos"] == {"x": 0.72, "y": 0.78} and tt["pos_why"] == "given in the plan"
    cx, cy = _centroid(fr[int(round((tt["t1"] - 0.3) * FPS))])
    assert abs(cx - 0.72) < 0.04 and abs(cy - 0.78) < 0.05, (cx, cy)
    ov = [o for at in side["segments"] for o in at.get("overlays") or [] if o["kind"] == "typed_title"]
    assert ov and all(o.get("box") for o in ov)                                 # QA's faces-under-graphics sees the real box


def test_render_auto_pos_lands_in_the_dark_area(placed):
    fr, side = placed["frames"], placed["side"]
    tt = side["typed_titles"][0]
    assert tt["pos"] and tt["pos_why"].startswith("auto:") and not tt["backing"]
    cx, cy = _centroid(fr[int(round((tt["t1"] - 0.3) * FPS))])
    assert cx < 330 / W and cy < 150 / H, (cx, cy, tt)                   # inside the navy box, off the white


def test_choose_shot_prefers_the_readable_held_shot_and_skips_used_windows(placed):
    cfg, lib = placed["cfg"], placed["lib"]
    cands = [{"clip": "c", "in": 1.0, "path": str(lib / "c.mp4")}, {"clip": "a", "in": 1.0, "path": str(lib / "a.mp4")}]
    r = TP.choose_shot(cfg, cands, "DAY THREE", dur=2.0)
    assert r["clip"] == "a" and r["ok"] and r["y"] < 150 / H, r
    r2 = TP.choose_shot(cfg, cands, "DAY THREE", dur=2.0, used={"a": [(1.5, 2.0)]})      # a's window shown nearby
    assert r2["clip"] == "c" and r2["ok"] is False                                           # only c left: backed
    assert any(x.get("skipped", "").startswith("source window used") for x in r2["ranking"])
    r3 = TP.choose_shot(cfg, [{**cands[1], "duration": 6.0}], "DAY THREE", dur=2.0, used={"a": [(1.5, 2.0)]})
    assert r3["clip"] == "a" and r3["in"] >= 2.2                                             # slid to a free window


def test_cold_open_end_shot_auto(placed):
    from types import SimpleNamespace
    from cutroom import db
    from cutroom import render as R
    cfg, con = placed["cfg"], placed["con"]
    rows = {k: dict(db.get_clip(con, f"{k}.mp4")) for k in ("a", "c")}

    class Ctx:
        def __init__(self):
            self.cfg, self.cache, self.warnings = cfg, cfg.p("cache", "render"), []

        def clip(self, ref):
            return dict(db.get_clip(con, ref))

        def source(self, row):
            return row.get("proxy") or row["path"]

        def warn(self, m, once_key=None):
            self.warnings.append(m)
    pool = SimpleNamespace(pool=[{"ref": "c.mp4", "row": rows["c"], "next": 1.0},
                                 {"ref": "a.mp4", "row": rows["a"], "next": 1.0}], used={}, marks=[])
    pool.free_at = lambda p, dur: R._ShotPool.free_at(pool, p, dur)
    pool.mark = lambda cid, a, b: pool.marks.append((cid, a, b))
    co = {"end_shot": {"auto": True}, "title": {"text": "DAY THREE", "pos": "auto"}}
    ctx = Ctx()
    seg = R._end_shot_seg(ctx, co, pool, 2.0)
    assert seg["clip"] == "a.mp4" and ctx.end_shot_choice["clip"] == "a.mp4"
    o = seg["overlays"][0]
    assert o["pos"]["y"] < 150 / H and pool.marks and pool.marks[0][0] == rows["a"]["id"]
