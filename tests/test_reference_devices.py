"""Reference devices, measured on synthetic footage: the two-frame film burn, the window-box gate and the
inset (border pixels, colour, geometry), the "COMING UP" card (yellow text, scale-in), the typed title's
character timing, the LUT-only house grade, the cold-open shot pool, rule 38 in QA, and the reference
device detector reading all of it back from a render."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p"]
W, H, FPS = 640, 360, 25


def _ff(*a):
    subprocess.run(FF + [str(x) for x in a], check=True)


def _frames(path, w=W, h=H):
    r = subprocess.run(FF[:-1] + ["-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8)
    return a.reshape(-1, h, w, 3)


def _yellow(img):
    r, g, b = (img[..., k].astype(int) for k in range(3))
    return (r > 180) & (g > 170) & (b < 110)


PLAN = {
    "title": "Devices", "version": 1,
    "style": {"captions": {"enabled": False}, "voice": {"clean": False}, "loudness": {"I": -16, "TP": -1.5}},
    "sections": [{"name": "open", "segments": [
        {"type": "card", "text": "COMING UP", "dur": 0.84},
        {"type": "montage", "beat_sync": False, "audio": "music", "transition": {"kind": "burn", "strength": 0.5},
         "shots": [{"clip": "a.mp4", "in": 1.0, "dur": 0.12, "frame": {"kind": "inset", "scale": 0.62}},
                   {"clip": "b.mp4", "in": 2.0, "dur": 0.12, "frame": {"kind": "inset", "scale": 0.62}},
                   {"clip": "a.mp4", "in": 3.0, "dur": 0.12, "frame": {"kind": "inset", "scale": 0.62}}]},
        {"type": "broll", "clip": "b.mp4", "in": 1.0, "out": 3.0, "audio": "natural", "frame": {"kind": "gate"}},
        {"type": "broll", "clip": "a.mp4", "in": 4.0, "out": 7.0, "audio": "natural", "transition": {"kind": "burn"},
         "overlays": [{"kind": "typed_title", "text": "DAY THREE", "at": 0.0, "dur": 3.0, "start": 1.0, "char_s": 0.08}]},
    ]}],
}


@pytest.fixture(scope="module")
def dev(tmp_path_factory):
    from cutroom import db, ingest
    from cutroom import render as R
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("devices")
    lib = base / "footage"
    lib.mkdir()
    # bright, busy pictures (no yellow: the title and card are the only yellow on screen)
    _ff("-f", "lavfi", "-i", f"color=c=0x6f8fa8:s={W}x{H}:r=25:d=8", "-f", "lavfi", "-i", "testsrc2=s=200x120:r=25:d=8,hue=s=0",
        "-f", "lavfi", "-i", "sine=f=330:d=8", "-filter_complex", "[0:v][1:v]overlay=x='100+60*sin(t)':y=80[v]",
        "-map", "[v]", "-map", "2:a", *X264, "-c:a", "aac", "-shortest", lib / "a.mp4")
    _ff("-f", "lavfi", "-i", f"color=c=0x8a9a88:s={W}x{H}:r=25:d=8", "-f", "lavfi", "-i", "testsrc2=s=180x140:r=25:d=8,hue=s=0",
        "-f", "lavfi", "-i", "sine=f=220:d=8", "-filter_complex", "[0:v][1:v]overlay=x='300+50*sin(t*1.3)':y=150[v]",
        "-map", "[v]", "-map", "2:a", *X264, "-c:a", "aac", "-shortest", lib / "b.mp4")
    cfg = init_project(base / "work", [lib])
    cfg.workers = 2
    cfg.out_width, cfg.out_height = W, H
    cfg.preview_height = H
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    ingest.make_proxies(cfg, con)
    plan_p = cfg.p("plans", "devices_v1.json")
    plan_p.parent.mkdir(parents=True, exist_ok=True)
    plan_p.write_text(json.dumps(PLAN))
    out = R.render_plan(cfg, con, plan_p, preview=True)
    side = json.loads(Path(out[:-4] + ".json").read_text())
    return {"cfg": cfg, "con": con, "out": out, "side": side, "frames": _frames(out)}


def _burns(side):
    return [t for t in side["transitions"] if t["kind"] == "burn"]


# ------------------------------------------------------------------------------------------- burns
def test_burn_layer_shape_and_colour():
    from cutroom import cards
    w, h = 480, 270
    A = cards.burn_layer(w, h, "out", seed=3).astype(float)
    B = cards.burn_layer(w, h, "in", seed=3).astype(float)
    # outgoing: red glow, brightest in the lower left, almost nothing upper right
    red = (A[..., 0] > 120) & (A[..., 1] < 0.6 * A[..., 0]) & (A[..., 2] < 0.5 * A[..., 0])
    ys, xs = np.where(red)
    assert red.mean() > 0.12 and xs.mean() / w < 0.4 and ys.mean() / h > 0.55, (red.mean(), xs.mean() / w, ys.mean() / h)
    assert A[: h // 3, -w // 3:].mean() < 10
    # incoming: orange-yellow disc from the right, a magenta fringe on its leading edge, a hot core
    org = (B[..., 0] > 200) & (B[..., 1] > 70) & (B[..., 2] < 120)
    ys, xs = np.where(org)
    assert org.mean() > 0.2 and xs.mean() / w > 0.65, (org.mean(), xs.mean() / w)
    mag = (B[..., 0] > 120) & (B[..., 2] > 100) & (B[..., 1] < 0.6 * B[..., 0])
    assert mag.mean() > 0.003 and np.where(mag)[1].mean() < xs.mean()
    blown = cards.composite_burn(np.full((h, w, 3), 100.0), B)
    assert (blown.min(axis=2) > 225).mean() > 0.01                     # a blown-white core once composited
    # seeded: the same seed is the same frame, another seed differs
    assert np.array_equal(cards.burn_layer(w, h, "in", seed=3), B.astype(np.uint8))
    assert np.abs(cards.burn_layer(w, h, "in", seed=9).astype(float) - B).mean() > 2
    # luma jump on a mid-grey picture: +30..70 (reference 81->149, 126->155, 106->137)
    grey = np.full((h, w, 3), 100.0)
    jumps = [float((cards.composite_burn(grey, L) @ np.array([0.2126, 0.7152, 0.0722])).mean() - 100) for L in (A, B)]
    assert 6 <= jumps[0] < jumps[1] and 30 <= jumps[1] <= 70, jumps       # the peak is on the incoming frame


def test_burn_on_the_cut_in_render(dev):
    fr, side = dev["frames"], dev["side"]
    b = next(t for t in _burns(side) if t["strength"] == 1.0)
    f = b["frame"]
    assert b["frames_out"] == 1 and b["frames_in"] == 1 and b["sfx"] == "burn"

    def red(i):
        x = fr[i].astype(int)
        return float(((x[..., 0] > 170) & (x[..., 1] < 110) & (x[..., 2] < 110)).mean())

    def org(i):
        x = fr[i].astype(int)
        return float(((x[..., 0] > 200) & (x[..., 1] > 70) & (x[..., 2] < 130) & (x[..., 0] - x[..., 2] > 100)).mean())
    assert red(f - 1) > 0.08 and red(f - 2) < 0.02 and red(f) < red(f - 1)       # glow only on the last outgoing frame
    assert org(f) > 0.15 and org(f + 1) < 0.03 and org(f - 2) < 0.03             # disc only on the first incoming frame
    lum = fr[:, :, :, :].astype(float) @ [0.2126, 0.7152, 0.0722]
    m = lum.mean(axis=(1, 2))
    assert max(m[f - 1], m[f]) - (m[f - 3] + m[f + 2]) / 2 > 15, m[f - 4:f + 4]
    # the cut is between the two burn frames: the gate (outgoing) is still there on f-1, gone on f
    gate_px = fr[f - 1][3, W // 2].astype(int)
    assert gate_px.max() < 60 and fr[f + 1][3, W // 2].astype(int).max() > 60
    # its sound: the burn sfx is bound to this transition with the transient peak on the cut
    ev = next(e for e in side["sfx"] if e.get("on", "").startswith("transition:") and abs(e["t"] - b["t"]) < 1e-3)
    assert ev["name"] == "burn" and ev["align"] == "peak" and 0.3 < ev["peak_s"] < 0.42


# ------------------------------------------------------------------------------------------- frames
def test_gate_geometry_and_colour(dev):
    from cutroom import cards
    side, fr = dev["side"], dev["frames"]
    seg = next(s for s in side["segments"] if (s.get("frame") or {}).get("kind") == "gate")
    f = int(round((seg["start"] + seg["end"]) / 2 * FPS))
    img = fr[f].astype(int)
    x0, y0, x1, y1 = (int(round(v * s)) for v, s in zip(seg["frame"]["window"], (W, H, W, H)))
    sp = cards.frame_spec({"kind": "gate"})
    assert abs(x0 - round(sp["window"][0] * W)) <= 1 and abs(y1 - round(sp["window"][3] * H)) <= 1
    # border: the measured near-black maroon #0F0100 on every side (bottom margin heavier than the top)
    for px in (img[2, W // 2], img[H - 3, W // 2], img[H // 2, 2], img[H // 2, W - 3]):
        assert abs(px[0] - 15) <= 7 and px[1] <= 8 and px[2] <= 8, px
    assert (H - y1) > y0
    # inside the window: the picture (bright), right up to the edge
    assert img[y0 + 4:y1 - 4, x0 + 4:x1 - 4].mean() > 70
    # rounded corners: the exact corner pixel is border, a few pixels in diagonally is picture
    assert img[y0, x0].max() < 60 and img[y0 + 6, x0 + 6].mean() > 60


def test_inset_geometry(dev):
    side, fr = dev["side"], dev["frames"]
    seg = next(s for s in side["segments"] if (s.get("frame") or {}).get("kind") == "inset")
    assert seg["frames"] == 3                                     # 3-frame strobe shots render as 3 frames
    f = int(round(seg["start"] * FPS)) + 1
    img = fr[f].astype(int)
    lum = img.mean(axis=2)
    rows = np.where(lum[:, W // 2] > 30)[0]
    cols = np.where(lum[H // 2] > 30)[0]
    hfrac, wfrac = (rows[-1] - rows[0] + 1) / H, (cols[-1] - cols[0] + 1) / W
    assert abs(hfrac - 0.62) < 0.03 and abs(wfrac - 0.62) < 0.03, (hfrac, wfrac)
    assert img[:rows[0] - 2].max() < 24 and img[:, :cols[0] - 2].max() < 24          # pure black surround


# ------------------------------------------------------------------------------------------- card
def test_card_text_and_scale(dev):
    from cutroom import cards
    fr, side = dev["frames"], dev["side"]
    card = next(s for s in side["segments"] if s["kind"] == "card")
    assert card["start"] == 0 and card["frames"] == 21 and card["card"]["text"] == "COMING UP"

    def width(i):
        cols = np.where(_yellow(fr[i]).any(axis=0))[0]
        return cols[-1] - cols[0] + 1
    y0 = _yellow(fr[0])
    assert y0.mean() > 0.01
    ys, xs = np.where(_yellow(fr[15]))
    assert abs(xs.mean() / W - 0.5) < 0.04 and abs(ys.mean() / H - 0.5) < 0.06       # dead centre
    ratio = width(0) / width(15)
    assert abs(ratio - cards.CARD["scale_from"]) < 0.04, ratio                        # 87 % -> 100 %
    assert width(5) < width(10) and abs(width(12) - width(18)) <= 2                   # ease-out, then hold
    bg = fr[3][~_yellow(fr[3])].astype(float).mean()
    assert 2 < bg < 25, bg                                                            # near-black texture
    # the texture boils: background pixels change every 2-3 frames, not every frame, and never stand still
    d = [np.abs(fr[i].astype(int) - fr[i + 1].astype(int))[~(_yellow(fr[i]) | _yellow(fr[i + 1]))].mean() for i in range(12, 19)]
    assert sum(x > 0.8 for x in d) >= 2 and sum(x < 0.8 for x in d) >= 2, d
    # its riser lands on the card's last frame
    ev = next(e for e in side["sfx"] if e["name"] == "riser")
    assert ev["align"] == "peak" and abs(ev["t"] - card["end"]) < 1e-3


# ------------------------------------------------------------------------------------------- typed title
def test_typed_title_character_timing(dev):
    from cutroom import cards
    fr, side = dev["frames"], dev["side"]
    tt = side["typed_titles"][0]
    assert tt["text"] == "DAY THREE" and tt["char_s"] == 0.08 and tt["start"] == 1.0
    f0 = int(round(tt["t0"] * FPS))
    ink = [int(_yellow(fr[f0 + i]).sum()) for i in range(0, 3 * FPS)]
    first = next(i for i, v in enumerate(ink) if v > 20 and i >= 2)       # frame 0 carries the burn's disc
    assert abs(first / FPS - 1.0) <= 1.5 / FPS, first
    # one more character every 0.08 s: ink grows in steps, monotonic, and is complete after 9 characters
    times = cards.typed_char_times("DAY THREE", 1.0, 0.08)
    steps = [ink[int(round(t * FPS)) + 1] for t in times]
    assert all(b >= a - 8 for a, b in zip(steps, steps[1:])) and steps[-1] > 2 * steps[0], steps     # a space adds no ink
    full = ink[int(round((times[-1] + 0.2) * FPS))]
    assert abs(ink[-1] - full) <= 0.05 * full                                          # then holds


# ------------------------------------------------------------------------------------------- grade
def test_grade_default_is_lut_only(tmp_path):
    """The house default: the LUT and nothing after it; no curve, vibrance, saturation or look, so a lifted
    LUT's black point survives (not crushed) and the opt-in finish still crushes when asked for."""
    from cutroom import plan as P
    from cutroom import render
    st = P.normalize({"sections": []})["style"]
    g = st["grade"]
    assert g["normalize_exposure"] is False and g["finish"]["enabled"] is False and g["finish"]["vibrance"] == 0
    assert g["saturation"] == 1.0 and g["look"] is None and st["film"] is None
    cube = tmp_path / "lift.cube"
    n = 9
    cube.write_text(f"LUT_3D_SIZE {n}\n" + "\n".join(
        f"{0.06 + 0.88 * r / (n - 1):.5f} {0.06 + 0.88 * gg / (n - 1):.5f} {0.06 + 0.88 * b / (n - 1):.5f}"
        for b in range(n) for gg in range(n) for r in range(n)) + "\n")

    class C:
        lut = str(cube)

        def __init__(self, style):
            self.style = style

        def info(self, src):
            return {"range": "tv", "w": 640, "h": 360}

        def pregraded(self, row, src):
            return False

        def warn(self, *a, **k):
            pass
    f, tag = render.grade_chain(C(st), {"is_log": 1}, "x", 320, 180)
    s = ",".join(f)
    assert tag == "lut" and "lut3d" in s
    assert not any(k in s for k in ("curves=master", "vibrance", "eq=saturation", "colorbalance", "vignette"))
    src = tmp_path / "ramp.mp4"
    # a fixed left-to-right black->white ramp. Not lavfi `gradients`: an out-of-range point (x1=320 on a
    # 320-wide frame) is silently replaced by a RANDOM one per run, which made this test flaky.
    _ff("-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=1,format=yuv444p,geq=lum='255*X/(W-1)':cb=128:cr=128",
        *X264, src)
    p1 = {}
    for k, style in (("lut_only", st), ("finish", P.normalize({"sections": [], "style": {"grade": {
            "finish": {"enabled": True, "vibrance": 0.18}, "saturation": 1.05}}})["style"])):
        ch, _ = render.grade_chain(C(style), {"is_log": 1}, str(src), 320, 180)
        r = subprocess.run(FF[:-1] + ["-i", str(src), "-frames:v", "1", "-vf", ",".join(ch) + ",format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, check=True)
        p1[k] = float(np.percentile(np.frombuffer(r.stdout, np.uint8), 1))
    assert p1["lut_only"] >= 15 and p1["finish"] < p1["lut_only"] - 5, p1               # LUT-only: no crush


def test_looks_match_never_night_on_daylight_and_caps():
    from cutroom import looks as LK
    base = {"luma": 150.0, "contrast": 50.0, "sat": 50.0, "warmth": 10.0, "p1": 32.0}
    night = {"luma": 110.0, "contrast": 60.0, "sat": 48.0, "warmth": 2.0, "p1": 4.0}
    punchy = {"luma": 150.0, "contrast": 62.0, "sat": 80.0, "warmth": 12.0, "p1": 10.0}
    target = {"luma": 112.0, "contrast": 60.0, "sat": 49.0, "warmth": 2.0}     # a dark, cool reference
    res = LK.match(base, {"night_cinematic": night, "punchy": punchy}, target)
    assert res["name"] != "night_cinematic"
    ex = next(r for r in res["ranked"] if r["name"] == "night_cinematic")
    assert ex.get("excluded") and ex["intensity"] == 0
    # night footage may use it, but only as far as the black point stays >= 15
    res2 = LK.match({**base, "luma": 50.0}, {"night_cinematic": night}, target)
    k = res2["ranked"][0]["intensity"]
    assert 0 < k <= (32 - 15) / (32 - 4) + 1e-6, res2
    # saturation within +15 %
    r3 = LK.match(base, {"punchy": punchy}, {**base, "sat": 90.0})
    k3 = r3["ranked"][0]["intensity"]
    assert base["sat"] + (punchy["sat"] - base["sat"]) * k3 <= base["sat"] * 1.15 + 1e-6


# ------------------------------------------------------------------------------------------- cold open pool
class _Ctx:
    def __init__(self, rows):
        self.rows = rows

    def clip(self, ref):
        return self.rows[ref]


def _rows():
    return {k: {"id": k, "duration": 20.0} for k in ("talk", "cut", "cov", "br", "x")}


def test_cold_open_pool_marks_cutaways_and_cover():
    from cutroom.render import _ShotPool
    plan = {"sections": [{"segments": [
        {"type": "talk", "clip": "talk", "in": 0.0, "out": 10.0,
         "cutaways": [{"clip": "cut", "in": 2.0, "at": 1.0, "dur": 3.0}], "cover": [{"clip": "cov", "in": 1.0}]}]}]}
    pool = _ShotPool(_Ctx(_rows()), plan, {"shots": "auto"}, [])
    taken = [pool.take(0.5) for _ in range(12)]
    for t in taken:
        if t["clip"] == "cut":
            assert not (t["in"] < 5.0 + 0.2 and t["in"] + 0.5 > 2.0 - 0.2), t      # never the cutaway's window
        if t["clip"] == "cov":
            assert not (t["in"] < 4.5 + 0.2 and t["in"] + 0.5 > 1.0 - 0.2), t      # nor the cover clip's


def test_cold_open_explicit_shots_keep_used_windows():
    from cutroom.render import _ShotPool
    plan = {"sections": [{"segments": [{"type": "broll", "clip": "br", "in": 3.0, "out": 9.0}]}]}
    pool = _ShotPool(_Ctx(_rows()), plan, {"shots": [{"clip": "br", "in": 4.0}, {"clip": "x", "in": 1.0}]}, [])
    for _ in range(10):
        t = pool.take(0.4)
        if t["clip"] == "br":
            assert not (t["in"] < 9.0 + 0.2 and t["in"] + 0.4 > 3.0 - 0.2), t      # the edit shows 3-9 s later


def test_cold_open_defaults():
    from cutroom import plan as P
    errs = P.validate({"sections": [{"name": "a", "segments": [{"type": "black", "dur": 1}]}],
                       "cold_open": {"style": "reference", "lines": []}})
    assert any("from_reference" in e for e in errs)
    assert not P.validate({"sections": [{"name": "a", "segments": [{"type": "black", "dur": 1}]}],
                           "cold_open": {"style": "reference", "from_reference": "r1", "card": {"text": "COMING UP"},
                                         "inset": {"scale": 0.62}, "gate": {"w": 0.973, "h": 0.906}, "face_s": None,
                                         "title": {"text": "DAY THREE"}, "loudness": {"I": -12}}})


# ------------------------------------------------------------------------------------------- rule 38 in QA
def test_rule38_devices_need_the_reference():
    from cutroom import qa
    assert qa.device_rule_issues({"devices": []}) == []
    bad = qa.device_rule_issues({"devices": ["burn", "card"], "reference": None})
    assert len(bad) == 2 and all(sev == "fail" and "rule 38" in m for sev, m in bad)
    ok = qa.device_rule_issues({"devices": ["burn", "card", "gate", "inset", "typed_title"],
                                "reference": {"id": "r1", "devices": ["burn", "card", "gate", "inset", "typed_title"]}})
    assert ok == []
    part = qa.device_rule_issues({"devices": ["burn", "letterbox"], "reference": {"id": "r1", "devices": ["burn"]}})
    assert len(part) == 1 and "letterbox" in part[0][1]


def test_qa_reports_devices_and_burns(dev, tmp_path):
    from cutroom import qa
    rep = qa.run(dev["cfg"], dev["out"], out_dir=tmp_path)
    chk = {c["id"]: c for c in rep["checks"]}
    d = chk["devices_vs_reference"]
    assert d["status"] == "fail" and set(d["value"]["used"]) >= {"burn", "card", "gate", "inset", "typed_title"}
    bt = chk["burn_timing"]
    full = [r for r in bt["value"]["burns"] if r["disc_pct"] > 0.1]
    assert full and not [i for i in bt["issues"] if i["severity"] == "fail"], bt
    assert chk["black_level"]["value"]["range"] == [8.0, 35.0]


# ------------------------------------------------------------------------------------------- detector
def test_detector_reads_the_devices_back(dev):
    """The reference detector, run on our own render, finds what the plan put there."""
    from cutroom import reference as R
    side = dev["side"]
    feats = [R.device_features(f) for f in dev["frames"]]
    cuts = [int(round(s["start"] * FPS)) for s in side["segments"][1:]]
    found = R.detect_devices(feats, FPS, cuts)
    assert found["card"] and abs(found["card"]["t1"] - 0.84) <= 0.08 and found["card"]["scale_from"] < 0.95
    hard = [b for b in _burns(side) if b["strength"] == 1.0][0]["frame"]
    assert any(abs(b["frame"] - hard) <= 1 for b in found["burns"]), found["burns"]
    kinds = {r["kind"] for r in found["frames"]}
    assert {"inset", "gate"} <= kinds, found["frames"]
    gate = next(r for r in found["frames"] if r["kind"] == "gate")
    assert abs((gate["box"][2] - gate["box"][0]) - 0.973) < 0.02 and abs((gate["box"][3] - gate["box"][1]) - 0.906) < 0.03
    t = next(t for t in found["titles"] if t.get("type_on"))
    assert abs(t["type_on"]["start_s"] - 1.0) <= 0.1, t


# ------------------------------------------------------------------------------------------- film / lens
def test_film_chain_runs_and_measures(tmp_path):
    """style.film rebuilds measurable traits: the chain is a valid ffmpeg graph, and the measurements move
    the right way (more grain, softer, darker corners, no clipping)."""
    from cutroom import film
    src = tmp_path / "src.png"
    _ff("-f", "lavfi", "-i", "testsrc2=s=960x540:d=1", "-frames:v", "1", src)
    spec = {"grain": 4.0, "grain_size": 1.5, "soften": 1.0, "vignette": 0.25, "halation": 0.5, "ca": 1.5,
            "rolloff": {"knee": 0.82, "ceiling": 0.94}, "seed": 3}
    assert film.validate(spec) == [] and film.validate({"grain": 50}) and film.validate({"bogus": 1})
    from PIL import Image

    def run(sp, name):
        parts = film.expand(film.chain(sp, 960, 540), "t", 960, 540)
        fc = ";".join(c.replace("[IN]", "[0:v]format=yuv420p,", 1) if c.startswith("[IN]") else c for c in parts)
        out = tmp_path / f"{name}.png"
        _ff("-i", src, "-filter_complex", fc, "-map", "[OUT]", "-frames:v", "1", out)
        return film.measure_frame(np.asarray(Image.open(out).convert("RGB")))
    ma = film.measure_frame(np.asarray(Image.open(src).convert("RGB")))
    mb = run(spec, "all")                                   # every trait at once is one valid graph
    assert mb["grain_std"] > ma["grain_std"] + 0.2 and mb["p999"] <= 0.95 * 255 + 2 and mb["vignette"] < ma["vignette"], (ma, mb)
    assert run({"soften": 1.0}, "soft")["sharpness"] < ma["sharpness"] - 0.05
    assert film.chain(None, 960, 540) == [] and film.chain({"grain": 0}, 960, 540) == []
    m = film.match({"grain_std": 2.0, "sharpness": 0.10, "vignette": 0.7, "p999": 232, "clip_pct": 0.0},
                   {"grain_std": 1.0, "sharpness": 0.16, "vignette": 0.85, "p999": 253, "clip_pct": 1.2})
    assert m["grain"] > 1 and m["soften"] > 0 and m["vignette"] > 0 and m["rolloff"]["ceiling"] < 0.96 and m["why"]
    assert film.match({"grain_std": 1.0}, {"grain_std": 1.0}) == {"why": {}}          # nothing measured -> nothing added
