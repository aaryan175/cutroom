"""Punch: the measured loudness swing (cutroom.dynamics), the reference intro envelope, the renderer's
cold_open.dynamics shaping, the local true-peak fix after the encode, and QA swing_vs_reference.
Synthetic audio only (numpy noise blocks at known levels), no footage, no agents."""
import json
import subprocess

import numpy as np
import pytest
import soundfile as sf

from cutroom import dynamics as D
from cutroom import plan as P
from cutroom import qa
from cutroom import reference as R
from cutroom import render
from cutroom.config import Config

SR = 48000


def _noise(sec, db, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(sec * SR)).astype(np.float32)
    x *= (10 ** (db / 20)) / np.sqrt(np.mean(x ** 2))
    return np.stack([x, x], axis=1)


def _blocks(spec, seed=0):
    return np.concatenate([_noise(s, db, seed + i) for i, (s, db) in enumerate(spec)])


def _wav(path, x):
    sf.write(str(path), x, SR, subtype="FLOAT")
    return path


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "renders").mkdir()
    return Config(work=str(tmp_path))


def test_swing_is_momentary_p95_minus_p10(cfg, tmp_path):
    flat = _wav(tmp_path / "flat.wav", _noise(6, -20))
    punchy = _wav(tmp_path / "punchy.wav", _blocks([(1.0, -32), (0.6, -12)] * 4))
    sf_ = D.window_stats(cfg, flat, 0, 6)
    sp = D.window_stats(cfg, punchy, 0, 6.4)
    assert sf_["swing"] < 1.5                          # a steady level has no punch
    assert 14 < sp["swing"] < 22                       # 20 dB blocks: most of it shows in 400 ms windows
    # the 3 s short-term value hides it (why the factory uses momentary)
    env = D.envelope(cfg, punchy, 0, 6.4)
    assert D.swing(env, key="S") < sp["swing"] - 6
    # a level change alone can't move the swing
    louder = _wav(tmp_path / "louder.wav", _blocks([(1.0, -26), (0.6, -6)] * 4) * 0.9)
    assert abs(D.window_stats(cfg, louder, 0, 6.4)["swing"] - sp["swing"]) < 1.0
    # the window is cut before measuring: [2, 4] of a file loud only before 2 s reads quiet
    w = _wav(tmp_path / "w.wav", np.concatenate([_noise(2, -10), _noise(2, -30)]))
    assert D.window_stats(cfg, w, 2.0, 4.0)["M_max"] < -20       # the -10 dB part reads ~ -6


def test_target_curve_maps_elements():
    ref = [[t / 10, -30.0 if t < 20 else -10.0] for t in range(1, 41)]        # quiet 0-2 s, loud 2-4 s
    # our cold open: element A 0-1 s <- ref 0-2 s, element B 1-3 s <- ref 2-4 s
    c = dict(render.dyn_target_curve(ref, [(0, 1, 0, 2), (1, 3, 2, 4)], 3.0, 4.0))
    assert c[0.5] == pytest.approx(-30.0) and c[2.0] == pytest.approx(-10.0)
    c2 = dict(render.dyn_target_curve(ref, [], 2.0, 4.0))                     # no spans: stretch
    assert c2[0.5] == pytest.approx(-30.0) and c2[1.5] == pytest.approx(-10.0)
    assert render.dynamics_spec({"dynamics": "reference"}) == {"match": "reference"}
    assert render.dynamics_spec({"dynamics": {"match": "nope"}}) == {} and render.dynamics_spec({}) == {}


class _Ctx:
    def __init__(self, cfg):
        self.cfg, self.fps, self.warns = cfg, 25.0, []

    def warn(self, m, once_key=None):
        self.warns.append(m)


class _TL:
    def __init__(self, spans):
        self.atoms = [{"f0": int(a * 25), "f1": int(b * 25)} for a, b in spans]


def test_shape_cold_open_follows_reference_envelope(cfg, tmp_path, monkeypatch):
    # the reference: a loud line (2 s), a hit (0.6 s), a quiet held end (2.4 s) -> big swing
    ref_wav = _wav(tmp_path / "ref.wav", _blocks([(2.0, -23), (0.6, -19), (2.4, -36)], seed=10))
    env = D.envelope(cfg, ref_wav, 0, 5.0)
    ref = {"t0": 0.0, "t1": 5.0, "I": env["I"], "swing": D.swing(env), "M_max": max(env["M"]),
           "curve": [[t, m] for t, m in zip(env["t"], env["M"])]}
    monkeypatch.setattr(R, "intro_envelope", lambda cfg_, rid, **k: ref)
    # ours: flat-ish cold open 5 s (the v5 problem: everything at the same level), then a 3 s body
    co = _blocks([(2.0, -26), (0.6, -24), (2.4, -24)], seed=20)
    body = _noise(3.0, -28, seed=30)
    mix = _wav(tmp_path / "mix.wav", np.concatenate([co, body]))
    body_before = sf.read(str(mix), dtype="float32", always_2d=True)[0][int(5.6 * SR):]
    tl = _TL([(0, 2.0), (2.0, 2.6), (2.6, 5.0)])
    segs = [{"sec": -1, "atoms": [i], "spec": {"_ref_span": [a, b]}} for i, (a, b) in
            enumerate([(0, 2.0), (2.0, 2.6), (2.6, 5.0)])] + [{"sec": 0, "atoms": [], "spec": {}}]
    plan = {"cold_open": {"style": "reference", "from_reference": "r1", "dynamics": {"match": "reference"}}}
    before = D.window_stats(cfg, mix, 0, 5.0)
    info = render.shape_cold_open(_Ctx(cfg), plan, tl, segs, mix, {"I": -14.0, "TP": -1.5})
    after = D.window_stats(cfg, mix, 0, 5.0)
    assert info["status"] == "ok" and info["spans"] == 3
    assert before["swing"] < ref["swing"] - 5
    assert abs(after["swing"] - ref["swing"]) <= 2.0, (after, ref["swing"])
    assert abs(after["I"] - ref["I"]) <= 1.0
    assert after["TP"] <= -2.4                                        # the cold open's own peaks are limited
    out = sf.read(str(mix), dtype="float32", always_2d=True)[0]
    np.testing.assert_allclose(out[int(5.6 * SR):], body_before, atol=2e-4)   # the body is untouched (24-bit)


def test_post_encode_peak_fix_is_local(cfg, tmp_path):
    # programme at a steady level with one sharp hit that overshoots after an AAC encode
    x = _noise(6.0, -20, seed=3)
    i = int(3.0 * SR)
    x[i:i + 200] = 0.99 * np.sign(np.sin(np.arange(200) / 3.0))[:, None]
    wav = _wav(tmp_path / "prog.wav", x)
    enc = tmp_path / "prog.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "192k", str(enc)], check=True)
    out = tmp_path / "fixed.wav"
    res = render.relimit_overshoots(cfg, wav, enc, out, -1.5)
    assert res and res["spans"] and all(a <= 3.0 <= b + 0.01 for a, b in res["spans"][:1])
    y = sf.read(str(out), dtype="float32", always_2d=True)[0]
    assert np.abs(y[i:i + 200]).max() < 0.9                             # the hit is limited ...
    far = slice(0, int(2.0 * SR))
    np.testing.assert_allclose(y[far], x[far], atol=2e-4)              # ... the rest keeps its level
    for a, b in ((0, 2.5), (3.6, 6.0)):                                  # programme level kept around the hit
        assert abs(D.window_stats(cfg, out, a, b)["I"] - D.window_stats(cfg, wav, a, b)["I"]) < 0.1
    quiet = _wav(tmp_path / "q.wav", _noise(2.0, -30))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(quiet), "-c:a", "aac", str(tmp_path / "q.m4a")], check=True)
    assert render.relimit_overshoots(cfg, quiet, tmp_path / "q.m4a", tmp_path / "q2.wav", -1.5) is None


class _Rep:
    def __init__(self):
        self.checks = []

    def issue(self, sev, msg, t=None, t_end=None, frames=(), tag=""):
        return {"severity": sev, "msg": msg, "t": t}

    def add(self, cid, category, title, issues=None, value=None, detail="", status=None, rule=None):
        st = status or ("fail" if any(i["severity"] == "fail" for i in issues or []) else
                        "warn" if issues else "pass")
        self.checks.append({"id": cid, "status": st, "value": value, "detail": detail, "issues": issues or []})


def test_qa_swing_vs_reference(cfg, monkeypatch):
    monkeypatch.setattr(R, "intro_envelope", lambda cfg_, rid, **k: {"t0": 0, "t1": 10, "swing": 12.3, "M_max": -10.6})
    t = np.arange(1, 101) / 10
    flat = np.stack([t, np.where((t * 10).astype(int) % 5 == 0, -10.0, -16.0)], 1)       # ~6 LU swing
    punchy = np.stack([t, np.where(t > 7.5, -24.0, np.where((t * 10).astype(int) % 3 == 0, -10.5, -13.0))], 1)
    raw = {"sections": [{"index": -1, "start": 0.0, "end": 10.0, "generated": "cold_open"}],
           "reference": {"id": "r1"}}
    rep = _Rep()
    qa.swing_check(cfg, rep, flat, raw)
    c = rep.checks[-1]
    assert c["id"] == "swing_vs_reference" and c["status"] == "warn" and "dynamics" in c["issues"][0]["msg"]
    qa.swing_check(cfg, rep, flat, {**raw, "cold_open_dynamics": {"status": "ok", "reference": "r1"}})
    assert rep.checks[-1]["status"] == "fail"                           # promised by the plan, not delivered
    qa.swing_check(cfg, rep, punchy, {**raw, "cold_open_dynamics": {"status": "ok", "reference": "r1"}})
    assert rep.checks[-1]["status"] == "pass" and abs(rep.checks[-1]["value"]["diff_lu"]) <= 2
    qa.swing_check(cfg, rep, flat, {"sections": [{"index": 0, "start": 0, "end": 5}]})
    assert rep.checks[-1]["status"] == "info" and "no cold open" in rep.checks[-1]["detail"]
    qa.swing_check(cfg, rep, flat, {"sections": raw["sections"]})
    assert rep.checks[-1]["status"] == "info" and "no reference" in rep.checks[-1]["detail"]


def test_reference_intro_envelope_cached(cfg, tmp_path):
    d = tmp_path / "refs" / "rx1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": "rx1", "title": "x"}))
    (d / "analysis.json").write_text(json.dumps({"sections": [{"start": 0.0, "end": 4.0, "name": "cold open"}]}))
    _wav(tmp_path / "src.wav", _blocks([(2.0, -12), (2.0, -28), (2.0, -14)]))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "src.wav"), "-c:a", "aac", str(d / "source.m4a")],
                   check=True)
    e = R.intro_envelope(cfg, "rx1")
    assert e["t1"] == 4.0 and e["swing"] > 10 and len(e["curve"]) >= 35 and (d / "intro_envelope.json").exists()
    e["swing"] = -1
    (d / "intro_envelope.json").write_text(json.dumps(e))
    assert R.intro_envelope(cfg, "rx1")["swing"] == -1                       # cached by window + file sig
    assert R.intro_envelope(cfg, "rx1", t1=3.0)["t1"] == 3.0                 # another window re-measures


def test_plan_validates_dynamics():
    def errs(co):
        return [e for e in P._validate_cold_open(co, {}, lambda *a: None) if "dynamics" in e]
    base = {"style": "reference", "from_reference": "r1"}
    assert errs({**base, "dynamics": {"match": "reference"}}) == []
    assert errs({**base, "dynamics": "reference"}) == []
    assert errs({**base, "dynamics": {"match": "loud"}})
    assert errs({"style": "teaser", "dynamics": {"match": "reference"}})          # nothing to match
