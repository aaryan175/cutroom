"""ASR backend selection (faster-whisper / mlx), the MLX result adapter, hardware-aware slot defaults.
None of this needs mlx_whisper installed: the MLX module is stubbed."""
import json
import sys
import types

import numpy as np
import pytest

from cutroom import asr, estimate, governor
from cutroom.config import Config


# ------------------------------------------------------------------------------------------- selection
@pytest.mark.parametrize("want,apple,mlx_ok,model,expect", [
    ("auto", True, True, "medium", "mlx"),
    ("auto", True, True, None, "mlx"),
    ("", True, True, "turbo", "mlx"),
    ("auto", True, False, "medium", "faster-whisper"),            # mlx_whisper not installed
    ("auto", False, True, "medium", "faster-whisper"),            # not Apple Silicon
    ("auto", True, True, "distil-large-v3", "faster-whisper"),    # no MLX weights for this name
    ("auto", True, True, "small", "faster-whisper"),              # small models stay on beam-search CPU
    ("auto", True, True, "tiny", "faster-whisper"),
    ("auto", True, True, "large-v3", "mlx"),
    ("auto", True, True, "mlx-community/whisper-medium-mlx", "mlx"),  # an explicit MLX repo id
    ("mlx", True, True, "tiny", "mlx"),                           # explicit mlx forces any mapped model
    ("faster-whisper", True, True, "medium", "faster-whisper"),
    ("fw", True, True, "medium", "faster-whisper"),
    ("faster_whisper", False, False, "small", "faster-whisper"),
    ("mlx", True, True, "large-v3", "mlx"),
    ("MLX", True, True, "mlx-community/whisper-small-mlx", "mlx"),
])
def test_select_backend(want, apple, mlx_ok, model, expect):
    assert asr.select_backend(want, model, apple=apple, mlx_ok=mlx_ok) == expect


@pytest.mark.parametrize("apple,mlx_ok,model,match", [
    (False, True, "medium", "not an Apple Silicon"),
    (True, False, "medium", "does not import"),
    (True, True, "distil-large-v3", "no MLX weights"),
])
def test_select_backend_mlx_errors(apple, mlx_ok, model, match):
    with pytest.raises(RuntimeError, match=match):
        asr.select_backend("mlx", model, apple=apple, mlx_ok=mlx_ok)


def test_select_backend_rejects_unknown():
    with pytest.raises(ValueError):
        asr.select_backend("whisper.cpp", "medium", apple=True, mlx_ok=True)


def test_auto_never_imports_mlx_off_apple(monkeypatch):
    called = []
    monkeypatch.setattr(asr, "mlx_available", lambda: called.append(1) or True)
    assert asr.select_backend("auto", "medium", apple=False) == "faster-whisper"
    assert not called


def test_mlx_repo_names():
    for name in ("small", "medium", "large-v3", "turbo", "large-v3-turbo"):
        assert asr.mlx_repo(name).startswith("mlx-community/whisper-")
    assert asr.mlx_repo("turbo") == asr.mlx_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    assert asr.mlx_repo("medium") == "mlx-community/whisper-medium-mlx"
    assert asr.mlx_repo("someone/custom-whisper") == "someone/custom-whisper"     # repo ids pass through
    assert asr.mlx_repo("distil-large-v3") is None and asr.mlx_repo("") is None


def test_doctor_summary_reports_backend(monkeypatch):
    cfg = Config(asr_backend="mlx", asr_model="medium")
    monkeypatch.setattr(asr, "is_apple_silicon", lambda: False)
    ok, detail = asr.backend_summary(cfg)
    assert not ok and "Apple Silicon" in detail
    monkeypatch.setattr(asr, "is_apple_silicon", lambda: True)
    monkeypatch.setitem(asr._AVAIL, "mlx", True)
    ok, detail = asr.backend_summary(cfg)
    assert ok and detail.startswith("mlx") and "whisper-medium-mlx" in detail
    ok, detail = asr.backend_summary(Config(asr_backend="mlx", asr_remote="ssh box run {audio} {out}"))
    assert ok and detail.startswith("remote")


def test_config_default_is_auto():
    assert Config().asr_backend == "auto"


# ------------------------------------------------------------------------------------------- adapter
def _stub_result():
    """What mlx_whisper.transcribe returns (openai-whisper shape, numpy scalars included)."""
    return {"language": "en", "text": "...", "segments": [
        {"id": 0, "seek": 0, "start": 0.52, "end": 2.3049, "text": " Okay so this is the plan.",
         "no_speech_prob": 0.01, "avg_logprob": -0.2, "temperature": 0.0, "compression_ratio": 1.1, "tokens": [1],
         "words": [{"word": " Okay", "start": np.float64(0.52), "end": 0.8, "probability": np.float32(0.91)},
                   {"word": " so", "start": 0.8, "end": 0.95, "probability": 0.88},
                   {"word": " this", "start": 0.95, "end": 1.1, "probability": 0.97},
                   {"word": " is", "start": 1.1, "end": 1.2, "probability": 0.99},
                   {"word": " the", "start": 1.2, "end": 1.3, "probability": 0.99},
                   {"word": " plan.", "start": 1.3, "end": 2.3049, "probability": 0.95}]},
        {"id": 1, "seek": 0, "start": 3.0, "end": 9.0, "text": " Thanks for watching!",          # hallucination
         "no_speech_prob": 0.93, "avg_logprob": -1.4, "words": [{"word": " Thanks", "start": 3.0, "end": 3.5,
                                                                 "probability": 0.2}]},
        {"id": 2, "seek": 0, "start": 4.1, "end": 5.6, "text": " Let's go.", "no_speech_prob": 0.05,
         "avg_logprob": -0.3, "words": [{"word": " Let's", "start": 4.1, "end": 4.6, "probability": 0.9},
                                        {"word": " go.", "start": 4.6, "end": 5.6, "probability": 0.92}]},
        {"id": 3, "seek": 0, "start": 6.0, "end": 6.2, "text": "  ", "no_speech_prob": 0.1, "avg_logprob": -0.5,
         "words": []},                                                                    # empty: dropped
    ]}


def test_mlx_adapter_matches_transcript_shape(tmp_path):
    lang, segs = asr.mlx_to_segments(_stub_result())
    assert lang == "en" and len(segs) == 2
    assert [s["text"] for s in segs] == ["Okay so this is the plan.", "Let's go."]
    for s in segs:
        assert set(s) == {"start", "end", "text", "words"}
        for w in s["words"]:
            assert set(w) == {"start", "end", "word", "probability"}
            assert all(type(w[k]) is float for k in ("start", "end", "probability"))
    assert segs[0]["end"] == 2.305 and segs[0]["words"][0]["probability"] == 0.91
    assert segs[0]["words"][0]["word"] == " Okay"                   # leading space kept, like faster-whisper
    json.dumps(segs)                                                # plain JSON, no numpy scalars

    # the rest of the code reads it exactly as it reads a faster-whisper transcript
    cfg = Config(work=str(tmp_path))
    cfg.p("transcripts").mkdir(parents=True)
    cfg.p("transcripts", "c1.json").write_text(json.dumps({"language": lang, "model": "small", "backend": "mlx",
                                                           "speech_s": 3.3, "segments": segs}))
    ws = asr.words(cfg, "c1")
    assert [w[2] for w in ws] == ["Okay", "so", "this", "is", "the", "plan.", "Let's", "go."]
    assert asr.tighten(ws, 0.0, 6.0) == [(0.44, 2.385), (4.02, 5.78)]          # the 1.8 s pause is cut
    cues = asr.cues(ws, 0.0, 6.0)
    assert [c[2] for c in cues] == ["Okay so this is the", "plan.", "Let's go."]


def test_clip_spans_pad_merge_clamp():
    spans = [(0.1, 1.0), (1.5, 2.0), (5.0, 9.95)]
    assert asr._clip_spans(spans, 10.0) == [0.0, 2.2, 4.8, 10.0]
    assert asr._clip_spans([], 10.0) == []


def test_run_model_routes_to_mlx(monkeypatch):
    seen = {}

    def fake_transcribe(audio, **kw):
        seen.update(kw, n=len(audio), dtype=audio.dtype)
        return _stub_result()
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe))
    monkeypatch.setattr(asr, "is_apple_silicon", lambda: True)
    monkeypatch.setitem(asr._AVAIL, "mlx", True)
    monkeypatch.setattr(asr, "speech_spans", lambda audio, sr=asr.SR: [(0.5, 2.3), (4.0, 5.6)])
    monkeypatch.setattr(asr, "get_model", lambda name: pytest.fail("faster-whisper must not load"))
    cfg = Config(asr_backend="mlx", asr_language="en")
    lang, segs = asr._run_model(cfg, np.zeros(asr.SR * 8, dtype=np.float32), "small")
    assert seen["path_or_hf_repo"] == "mlx-community/whisper-small-mlx"
    assert seen["word_timestamps"] is True and seen["condition_on_previous_text"] is False
    assert seen["language"] == "en" and seen["clip_timestamps"] == [0.3, 2.5, 3.8, 5.8]
    assert seen["n"] == asr.SR * 8 and seen["dtype"] == np.float32
    assert lang == "en" and len(segs) == 2


def test_run_model_mlx_skips_silence(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(
        transcribe=lambda *a, **k: pytest.fail("silence must not be decoded")))
    monkeypatch.setattr(asr, "is_apple_silicon", lambda: True)
    monkeypatch.setitem(asr._AVAIL, "mlx", True)
    monkeypatch.setattr(asr, "speech_spans", lambda audio, sr=asr.SR: [])
    assert asr._run_model(Config(asr_backend="auto"), np.zeros(asr.SR, dtype=np.float32), "medium") == (None, [])


# ------------------------------------------------------------------------------------------- hardware
@pytest.mark.parametrize("cpus,ram,perf,apple,expect", [
    (10, 16, 4, True, 2),       # 10-core, 16 GB, 4 performance cores
    (10, 32, 8, True, 4),       # 10-core, 32 GB, 8 performance cores
    (10, 64, 8, True, 4),
    (20, 64, 16, True, 8),      # 20-core, 64 GB
    (20, 128, 16, True, 8),     # capped at 8
    (8, 8, 4, True, 1),         # 8 GB: RAM-bound
    (10, 16, None, False, 2),   # generic machines: cores // 4, as before
    (32, 64, None, False, 8),
    (2, 4, None, False, 1),
])
def test_auto_heavy_slots(cpus, ram, perf, apple, expect):
    cap, floor = min(4.0, 0.25 * ram), max(2.0, 0.15 * ram)
    self_mem = max(cap, min(8.0, 0.35 * ram))
    assert governor.auto_heavy_slots(cpus, ram, perf, apple, cap, floor, self_mem) == expect


def test_governor_uses_hardware(monkeypatch, tmp_path):
    monkeypatch.setattr(governor, "hardware", lambda: {"cpus": 20, "perf_cores": 16, "ram_gb": 64.0,
                                                       "chip": "test chip", "apple_silicon": True})
    monkeypatch.setattr(governor, "total_ram", lambda: 64 * governor.GB)
    monkeypatch.setattr(governor.os, "cpu_count", lambda: 20)
    g = governor.Governor(Config(work=str(tmp_path)))
    assert g.heavy_slots == 8 and g.asr_slots == 1 and g.allowed("asr") == 1
    assert governor.Governor(Config(work=str(tmp_path), heavy_slots=3)).heavy_slots == 3    # explicit wins


def test_estimate_keeps_backend_rates_apart(tmp_path):
    cfg = Config(work=str(tmp_path))
    cfg.p("overnight").mkdir(parents=True)
    (cfg.p("overnight") / "rates.json").write_text(json.dumps({"transcribe": [
        {"x_realtime": 1.5, "model": "medium"},                                 # older runs: no backend field
        {"x_realtime": 9.0, "model": "medium", "backend": "mlx"}]}))
    r = estimate.measured_rates(cfg)
    assert r["asr:faster-whisper:medium"][0] == 1.5 and r["asr:mlx:medium"][0] == 9.0
