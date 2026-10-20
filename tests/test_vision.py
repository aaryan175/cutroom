"""vision.py: the combined shot detector (flash / burn suppression, pans, gradual transitions) on synthetic
frames, YuNet loading with its Haar fallback, the brain schema + citation checks, and the reference brain pass
end to end with a stubbed agent (caching, cost cap, schema refusal, merge into the analysis)."""
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from cutroom import vision as V  # noqa: E402

W, H = 320, 180


def _texture(seed, w=W, h=H):
    """A textured picture with structure (shapes + blurred noise): ORB and the block matcher both bite on it."""
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(rng.integers(0, 255, (h, w, 3), dtype=np.uint8), (0, 0), 2.0)
    for _ in range(14):
        c = tuple(int(x) for x in rng.integers(0, 255, 3))
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(10, 70)), y + int(rng.integers(10, 50))), c, -1)
        else:
            cv2.circle(img, (x, y), int(rng.integers(6, 30)), c, -1)
    return img


def _jitter(img, k):
    """A tiny bit of sensor noise per frame, so 'static' frames are not byte-identical."""
    rng = np.random.default_rng(1000 + k)
    return np.clip(img.astype(np.int16) + rng.integers(-3, 4, img.shape), 0, 255).astype(np.uint8)


def _leak(img, side="left", strength=0.85):
    """A warm light leak over one side of the frame (what a film-burn frame looks like)."""
    out = img.astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    cx = 0.15 * W if side == "left" else 0.85 * W
    m = np.exp(-(((xx - cx) / (0.28 * W)) ** 2 + ((yy - 0.6 * H) / (0.45 * H)) ** 2))[..., None] * strength
    warm = np.array([30, 140, 255], np.float32)                     # BGR orange
    return np.clip(out * (1 - m) + warm * m, 0, 255).astype(np.uint8)


def _cuts(frames):
    arr = V.shot_features_from_frames(frames)
    return V.detect_shots(arr, 25.0)


def test_hard_cut_found_exactly():
    a, b = _texture(1), _texture(2)
    frames = [_jitter(a, k) for k in range(12)] + [_jitter(b, k) for k in range(12)]
    assert _cuts(frames)["cuts"] == [12]


def test_white_flash_on_one_shot_is_not_a_cut():
    a = _texture(3)
    flash = [np.clip(a.astype(np.int16) + 150, 0, 255).astype(np.uint8)] * 2
    frames = [_jitter(a, k) for k in range(12)] + flash + [_jitter(a, k) for k in range(12, 24)]
    r = _cuts(frames)
    assert r["cuts"] == []
    assert any(f["f0"] == 12 for f in r["flashes"])


def test_light_leak_overlay_on_one_shot_is_not_a_cut():
    a = _texture(4)
    frames = ([_jitter(a, k) for k in range(12)] + [_leak(_jitter(a, 12)), _leak(_jitter(a, 13), "right")]
              + [_jitter(a, k) for k in range(14, 26)])
    assert _cuts(frames)["cuts"] == []


def test_two_frame_burn_across_a_cut_is_one_cut_between_its_frames():
    """Frame A = last outgoing frame under a red leak, frame B = first incoming frame under an orange disc:
    one cut, on frame B (the reference's signature transition), not two cuts around the burn."""
    a, b = _texture(5), _texture(6)
    frames = ([_jitter(a, k) for k in range(12)] + [_leak(_jitter(a, 12), "left"), _leak(_jitter(b, 13), "right")]
              + [_jitter(b, k) for k in range(14, 26)])
    r = _cuts(frames)
    assert r["cuts"] == [13]
    assert any(f.get("cut") == 13 and f["kind"] == "burn" for f in r["flashes"])


def test_fast_pan_is_not_a_cut():
    big = cv2.resize(_texture(7, 640, 180), (900, 180))
    frames = [_jitter(big[:, 14 * k:14 * k + W].copy(), k) for k in range(30)]
    assert _cuts(frames)["cuts"] == []


def test_three_frame_inserts_are_all_cuts():
    pics = [_texture(10 + k) for k in range(6)]
    frames = []
    for k, p in enumerate(pics):
        frames += [_jitter(p, 3 * k + j) for j in range(3)]
    assert _cuts(frames)["cuts"] == [3, 6, 9, 12, 15]


def test_dissolve_is_a_gradual_transition():
    a = _texture(20).astype(np.float32) * 0.6                       # a darker scene dissolving into a brighter one
    b = np.clip(_texture(21).astype(np.float32) * 0.7 + np.linspace(40, 110, W)[None, :, None], 0, 255)
    frames = [_jitter(a.astype(np.uint8), k) for k in range(10)]
    for k in range(14):
        al = (k + 1) / 15
        frames.append(((1 - al) * a + al * b).astype(np.uint8))
    frames += [_jitter(b.astype(np.uint8), k) for k in range(10)]
    r = _cuts(frames)
    assert r["gradual"], r
    assert r["gradual"][0]["kind"] == "dissolve"
    assert len(r["cuts"]) == 1 and 14 <= r["cuts"][0] <= 20


# ------------------------------------------------------------------------------------------------ faces

def test_faces_fall_back_to_haar_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTROOM_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("CUTROOM_OFFLINE", "1")
    V._detectors.clear()
    assert V.ensure_model() is None
    d = V.face_detector()
    assert d.backend == "haar"
    assert d.detect(np.zeros((180, 320, 3), np.uint8)) == []
    V._detectors.clear()


def test_model_checksum_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTROOM_MODEL_DIR", str(tmp_path / "models"))
    src = tmp_path / "fake.onnx"
    src.write_bytes(b"not a model")
    good = hashlib.sha256(b"not a model").hexdigest()
    V._model_state.update(tried=False, url=None)
    assert V.ensure_model(download=True, url=src.as_uri(), sha256="0" * 64) is None     # wrong checksum: refused
    assert not (tmp_path / "models" / V.YUNET_FILE).exists()
    V._model_state.update(tried=False, url=None)
    p = V.ensure_model(download=True, url=src.as_uri(), sha256=good)
    assert p and p.read_bytes() == b"not a model"
    # a corrupt "model" never crashes the detector: it falls back to Haar
    d = V.FaceDetector(model_path=p)
    assert d.backend == "haar"
    V._model_state.update(tried=False, url=None)


@pytest.mark.skipif(not (V.model_dir() / V.YUNET_FILE).exists(), reason="YuNet model not cached on this machine")
def test_yunet_loads_from_cache(monkeypatch):
    monkeypatch.delenv("CUTROOM_FACES", raising=False)
    d = V.FaceDetector()
    assert d.backend == "yunet"
    assert d.detect(_texture(30)) == []                 # texture and shapes are not faces


def test_face_summary_words():
    s = V.face_summary([{"box": [0.4, 0.2, 0.2, 0.35]}, {"box": [0.05, 0.3, 0.05, 0.08]}])
    assert s["count"] == 2 and s["size"] == 0.35 and s["position"] == "centre"
    assert V.face_size_word(0.35) == "close" and V.face_size_word(0.15) == "medium" and V.face_size_word(0.05) == "wide"


# ------------------------------------------------------------------------------------------------ schema + citations

def _reply(**over):
    r = {"events": [], "missed_cuts": [], "shots": [], "devices": [], "text_on_screen": [], "sections": [],
         "why_it_works": [], "summary": "ok"}
    r.update(over)
    return r


def test_schema_validator():
    assert V.schema_errors(_reply(), V.BRAIN_SCHEMA) == []
    bad = _reply(devices=[{"kind": "laser", "f0": 1, "f1": 2, "detail": "", "frames": [1]}])
    assert any("not in" in e for e in V.schema_errors(bad, V.BRAIN_SCHEMA))
    bad = _reply(extra=1)
    assert any("unexpected" in e for e in V.schema_errors(bad, V.BRAIN_SCHEMA))
    bad = _reply(events=[{"id": "E1", "verdict": "cut", "frame": "12", "reason": "", "frames": [12]}])
    assert V.schema_errors(bad, V.BRAIN_SCHEMA)
    bad = _reply(shots=[{"id": "S1", "type": "close", "subject": "creator", "camera": "static", "framing": "good",
                         "faces": True, "text": None, "note": None, "frames": [3]}])
    assert V.schema_errors(bad, V.BRAIN_SCHEMA)          # a boolean is not an integer


def test_citations_must_point_at_shown_frames():
    raw = _reply(
        events=[{"id": "E1", "verdict": "cut", "frame": 13, "reason": "new shot", "frames": [12, 13]},
                {"id": "E2", "verdict": "cut", "frame": 99, "reason": "outside its strip", "frames": [40]},
                {"id": "E9", "verdict": "cut", "frame": 5, "reason": "unknown id", "frames": [5]}],
        devices=[{"kind": "burn", "f0": 12, "f1": 13, "detail": "warm leak", "frames": [12, 13]},
                 {"kind": "glitch", "f0": 500, "f1": 501, "detail": "never shown", "frames": [500]}],
        why_it_works=[{"point": "density then air", "frames": [777]}])
    clean, dropped = V.check_citations(raw, shown_frames=list(range(9, 17)) + [40],
                                       events_shown={"E1": list(range(9, 17)), "E2": [37, 38, 39, 40, 41]},
                                       shots_shown={}, n_frames=1000)
    assert [e["id"] for e in clean["events"]] == ["E1"]
    assert [d["kind"] for d in clean["devices"]] == ["burn"]
    assert clean["why_it_works"] == []
    assert len(dropped) == 4


def test_final_cuts_detectors_propose_ai_decides():
    events = [{"id": "E1", "frame": 12}, {"id": "E2", "frame": 30}, {"id": "E3", "frame": 50}]
    clean = {"events": [{"id": "E1", "verdict": "cut", "frame": 13, "reason": "burn hides the cut", "frames": [13]},
                        {"id": "E2", "verdict": "not_a_cut", "frame": None, "reason": "flash", "frames": [30]},
                        {"id": "E3", "verdict": "cut", "frame": 50, "reason": "missed by the detector", "frames": [50]}],
             "missed_cuts": [{"frame": 70, "reason": "jump cut", "frames": [70]}]}
    cuts, changes = V.final_cuts([12, 30, 90], events, clean)
    assert cuts == [13, 50, 70, 90]
    assert {c["change"] for c in changes} == {"moved", "removed", "added"}


def test_clip_citations():
    tag = {"shot": "close", "subject": "creator", "faces": "1 close centre", "seen_at": [99.0], "summary": "x"}
    out = V.check_clip_citations(tag, 20.0, 2.0, 24)
    assert "subject" not in out and "faces" not in out and out["summary"] == "x"
    tag["seen_at"] = [1.0, 7.0]                         # tiles of a 20 s clip every 2 s sit at 1, 3, 5, ...
    out = V.check_clip_citations(tag, 20.0, 2.0, 24)
    assert out["subject"] == "creator" and out["seen_at"] == [1.0, 7.0]


# ------------------------------------------------------------------------------------------------ the brain pass, stubbed

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


@pytest.fixture(scope="module")
def ref(tmp_path_factory):
    """A 4 s reference: shot A, a 2-frame white flash inside A, a two-frame burn across the cut to B, shot B."""
    from cutroom import reference as R
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("vision")
    lib = base / "footage"
    lib.mkdir()
    a, b = _texture(41, 640, 360), _texture(42, 640, 360)
    frames = []
    for k in range(100):
        if k < 50:
            f = _jitter(a, k)
            if k in (20, 21):
                f = np.clip(f.astype(np.int16) + 140, 0, 255).astype(np.uint8)
            if k == 49:
                f = _leak(cv2.resize(f, (W, H)), "left")
                f = cv2.resize(f, (640, 360))
        else:
            f = _jitter(b, k)
            if k == 50:
                f = cv2.resize(_leak(cv2.resize(f, (W, H)), "right"), (640, 360))
        frames.append(f)
    src = base / "ref.mp4"
    p = subprocess.Popen(FF + ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "640x360", "-r", "25", "-i", "-",
                               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
                               "-c:a", "aac", str(src)], stdin=subprocess.PIPE)
    p.communicate(b"".join(f.tobytes() for f in frames))
    cfg = init_project(base / "work", [lib])
    m = R.add(cfg, str(src), analyze_now=False, echo=lambda *x: None)
    an = R.analyze(cfg, m["id"], asr=False, echo=lambda *x: None)
    return {"cfg": cfg, "rid": m["id"], "an": an}


def test_reference_detector_suppresses_flash_and_burn(ref):
    cuts = [s["f0"] for s in ref["an"]["shots"][1:]]
    assert cuts == [50], cuts
    det = ref["an"]["shot_detector"]
    assert any(f["f0"] == 20 for f in det["flashes"])


def _stub(reply, calls, cost=0.12):
    def run(prompt, schema=None, images=(), max_budget_usd=None):
        calls.append({"prompt": prompt, "images": list(images), "budget": max_budget_usd, "schema": schema})
        return {"ok": True, "result_json": reply, "cost_usd": cost, "seconds": 1.0, "error": None}
    return run


def test_brain_pass_with_stubbed_agent(ref):
    cfg, rid = ref["cfg"], ref["rid"]
    an = ref["an"]
    sh = an["shots"]
    reply = _reply(
        events=[{"id": "E1", "verdict": "cut", "frame": 50, "reason": "new picture under the burn", "frames": [49, 50]}],
        shots=[{"id": f"S{s['i']}", "type": "graphic", "subject": "object", "camera": "static", "framing": "ok",
                "faces": 0, "text": None, "note": "test pattern", "frames": [(s["f0"] + s["f1"] - 1) // 2]} for s in sh],
        devices=[{"kind": "burn", "f0": 49, "f1": 50, "detail": "two-frame warm leak across the cut", "frames": [49, 50]},
                 {"kind": "flash", "f0": 20, "f1": 21, "detail": "white flash", "frames": [20, 21]}],
        why_it_works=[{"point": "the burn hides the cut", "frames": [49, 50]}], summary="two shots joined by a burn")
    calls = []
    brain = V.brain_reference(cfg, rid, budget_usd=1.0, runner=_stub(reply, calls), agent=_FakeAgent())
    assert len(calls) == 1
    assert calls[0]["budget"] == 1.0 and calls[0]["schema"] is V.BRAIN_SCHEMA
    assert all(Path(p).exists() for p in calls[0]["images"])
    assert "E1" in calls[0]["prompt"] and "frame 50" in calls[0]["prompt"]
    assert brain["cuts"] == [50] and brain["cost_usd"] == 0.12 and not brain["cached"]
    assert {d["kind"] for d in brain["devices"]} == {"burn", "flash"}
    # same pictures + prompt = cached: no second agent call, no cost
    brain2 = V.brain_reference(cfg, rid, budget_usd=1.0, runner=_stub(reply, calls), agent=_FakeAgent())
    assert len(calls) == 1 and brain2["cached"] and brain2["cost_usd"] == 0.0
    # merge: shots carry the AI labels, the cut map shows them, the summary has the breakdown
    from cutroom import reference as R
    an2, _ = V.deep_analyze(cfg, rid, runner=_stub(reply, calls), agent=_FakeAgent())
    rows = R.cutmap_rows(an2)
    assert rows[0]["shot_type"] == "graphic" and "burn" in rows[1]["devices"]
    assert an2["brain"]["why_it_works"][0]["point"] == "the burn hides the cut"
    assert any(ln.startswith("AI breakdown") for ln in an2["summary"])


def test_brain_pass_cost_cap_and_schema(ref):
    cfg, rid = ref["cfg"], ref["rid"]
    calls = []
    with pytest.raises(RuntimeError, match="budget"):
        V.brain_reference(cfg, rid, budget_usd=0.001, runner=_stub(_reply(), calls), agent=_FakeAgent(), force=True)
    assert calls == []                                    # never called over budget
    with pytest.raises(RuntimeError, match="schema"):
        V.brain_reference(cfg, rid, budget_usd=1.0, runner=_stub({"events": "nope"}, calls), agent=_FakeAgent(),
                          force=True)
    bad = _reply(events=[{"id": "E1", "verdict": "not_a_cut", "frame": None, "reason": "x", "frames": [999]}])
    brain = V.brain_reference(cfg, rid, budget_usd=1.0, runner=_stub(bad, calls), agent=_FakeAgent(), force=True)
    assert brain["cuts"] == [50]                          # an uncited verdict changes nothing
    assert brain["dropped"]


from cutroom import agent as _agentmod  # noqa: E402


class _FakeAgent(_agentmod._Agent):
    name = "claude"

    def __init__(self):
        super().__init__("/bin/true")


def test_qa_faces_use_vision_with_haar_fallback(monkeypatch):
    from cutroom import qa
    monkeypatch.setenv("CUTROOM_FACES", "haar")
    V._detectors.clear()
    assert qa.face_backend() == "haar"
    assert qa.detect_faces(np.zeros((180, 320, 3), np.uint8)) == []
    monkeypatch.setattr(V, "detect_faces", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken model")))
    assert qa.detect_faces(np.zeros((180, 320, 3), np.uint8)) == []      # falls back to the Haar cascade
    V._detectors.clear()


def test_tag_schema_and_validation_carry_vision_fields():
    from cutroom import agent, db, overnight
    props = agent.tag_schema()["properties"]["tags"]["items"]["properties"]
    assert props["seen_at"]["items"]["type"] == "number" and "subject" in props and "faces" in props
    t = overnight.validate_tag({"subject": "Creator", "framing": "Good", "faces": "1 close centre", "seen_at": [1, "3.0", 99]}, 20.0)
    assert t == {"subject": "creator", "framing": "good", "faces": "1 close centre", "seen_at": [1.0, 3.0]}
    assert set(V.VISION_TAG_FIELDS) <= set(db.TAG_FIELDS)
