"""Camera kit support: published log curves, camera identity from metadata, BWF field-recorder files, split takes,
clap anchors, clock offsets and colour matching to the hero camera (cutroom/colorsci.py, cameras.py, bwf.py, sync.py).

Reference values come from the manufacturers' documents cited in cutroom/colorsci.py; only curves with a public,
checked spec are tested as maths. No real footage: tiny synthetic files and mock ffprobe JSON."""
import json
import sqlite3
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from cutroom import bwf, cameras as CAM, colorsci as CS, db, sync

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
RESOLVE_LUT = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT")


# ============================================================================================ curves
def test_logc3_ei800_matches_arri_white_paper():
    # [ARRI] appendix, EI 800: black (zero exposure) encodes to f = 0.092809, the cut point to e*cut+f = 0.149658
    assert CS.logc3_encode(0.0) == pytest.approx(0.092809, abs=1e-6)
    p = CS.LOGC3_EI800
    assert p["e"] * p["cut"] + p["f"] == pytest.approx(0.149658, abs=2e-6)
    # 18 % grey: c*log10(a*0.18+b)+d with a*0.18 ~ 1.0 -> 0.391 (10-bit code value 400)
    assert CS.logc3_encode(0.18) == pytest.approx(0.247190 * np.log10(1.0 + 0.052272) + 0.385537, abs=1e-7)
    assert float(CS.logc3_encode(0.18)) * 1023 == pytest.approx(400.0, abs=0.5)
    x = np.array([0.0, 0.005, 0.01, 0.18, 0.9, 4.0, 30.0])
    assert CS.logc3_decode(CS.logc3_encode(x)) == pytest.approx(x, abs=1e-9)


def test_awg3_matrix_from_primaries_matches_published():
    # [ARRI] "ALEXA Wide Gamut RGB to CIE 1931 XYZ conversion matrix", derived here from the primaries ([RP177])
    M = CS.rgb_to_xyz(CS.PRIMARIES["awg3"])
    assert M == pytest.approx(CS.AWG3_TO_XYZ_PUBLISHED, abs=3e-6)


def test_slog3_code_values_match_sony():
    # [SONY]: 10-bit code values 95 (0 %), 420 (18 %), 598 (90 %)
    cv = CS.slog3_encode(np.array([0.0, 0.18, 0.90])) * 1023
    assert cv == pytest.approx([95.0, 420.0, 598.0], abs=0.5)
    x = np.array([0.0, 0.005, 0.01125, 0.18, 0.9, 8.0])
    assert CS.slog3_decode(CS.slog3_encode(x)) == pytest.approx(x, abs=1e-9)


def test_flog2_code_values_match_fujifilm():
    # [FUJI] 2-2: 10-bit code values 95 (0 %), 400 (18 %), 570 (90 %)
    cv = CS.flog2_encode(np.array([0.0, 0.18, 0.90])) * 1023
    assert cv == pytest.approx([95.0, 400.0, 570.0], abs=1.0)
    x = np.array([0.0, 0.0005, 0.001, 0.18, 0.9, 10.0])
    assert CS.flog2_decode(CS.flog2_encode(x)) == pytest.approx(x, abs=1e-6)


def test_fgamut_is_bt2020_and_neutral_axis_survives_to_logc3():
    assert CS.PRIMARIES["fgamut"] == CS.PRIMARIES["rec2020"]
    for prof, enc in (("sony_slog3", CS.slog3_encode), ("fuji_flog2", CS.flog2_encode)):
        grey = np.array([[0.18, 0.18, 0.18], [0.02, 0.02, 0.02], [0.9, 0.9, 0.9]])
        out = CS.to_logc3(enc(grey), prof)
        assert out == pytest.approx(CS.logc3_encode(grey), abs=1e-6)       # same scene grey = same LogC3 value


@pytest.mark.skipif(not (RESOLVE_LUT / "VFX IO" / "ARRI LogC to Linear.cube").exists(), reason="DaVinci Resolve LUTs not installed")
def test_logc3_matches_resolve_vfx_lut():
    """Cross-check against an independent implementation: Resolve's 1D LogC -> linear LUT (applied to the LUT input
    directly, i.e. the normalised signal after legal-range expansion; confirms the code-value convention too)."""
    rows = [list(map(float, ln.split())) for ln in (RESOLVE_LUT / "VFX IO" / "ARRI LogC to Linear.cube").read_text().splitlines()
            if ln[:1].isdigit() or ln[:1] in "-."]
    rows = np.asarray([r for r in rows if len(r) == 3])[:, 0]
    xs = np.linspace(0, 1, len(rows))
    for v in (0.2, 0.391, 0.5, 0.7):
        assert np.interp(v, xs, rows) == pytest.approx(float(CS.logc3_decode(v)), rel=2e-5)


def test_delta_e2000_sharma_reference_pairs():
    # [DE00] Table 1, pairs 1-3
    pairs = [((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
             ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
             ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412)]
    for a, b, de in pairs:
        assert float(CS.delta_e2000(np.array(a), np.array(b))) == pytest.approx(de, abs=1e-4)


def test_cube_round_trip_and_dat_order(tmp_path):
    cube = CS.Cube.from_function(lambda x: np.clip(x[:, [0, 1, 2]] ** 1.1, 0, 1), 9, title="t")
    p = cube.write(tmp_path / "a.cube")
    back = CS.Cube.read(p)
    pts = np.random.default_rng(1).random((50, 3))
    assert back.apply(pts) == pytest.approx(cube.apply(pts), abs=2e-6)
    # DaVinci .dat: blue changes fastest; a LUT that keeps only red must read back that way
    n = 5
    g = np.linspace(0, 1, n)
    lines = ["3DLUTSIZE 5"] + [f"{r:.6f} 0 0" for r in g for _g in g for _b in g]
    (tmp_path / "r.dat").write_text("\n".join(lines) + "\n")
    d = CS.Cube.read(tmp_path / "r.dat")
    assert d.apply(np.array([0.5, 0.9, 0.1])) == pytest.approx([0.5, 0.0, 0.0], abs=1e-9)


def test_fit_match_recovers_gains_and_lowers_delta_e():
    rng = np.random.default_rng(4)
    ref = rng.random((4000, 3)) * 0.8 + 0.05
    M_true = np.diag([1.12, 1.0, 0.85]) * 2 ** 0.3
    src = CS.apply_display_matrix(ref, np.linalg.inv(M_true))
    fit = CS.fit_match(src, ref)
    after = CS.apply_display_matrix(src, fit["matrix"])
    de_b = CS.delta_e2000(CS.display_to_lab(src.mean(0)), CS.display_to_lab(ref.mean(0)))
    de_a = CS.delta_e2000(CS.display_to_lab(after.mean(0)), CS.display_to_lab(ref.mean(0)))
    assert de_a < 0.6 * de_b and de_a < 1.5


# ============================================================================================ identity
def _info(tags=None, stream_tags=None, w=1920, h=1080, codec="prores", tag="apcn", trc=None, brand=None):
    fmt = {"tags": dict(tags or {})}
    if brand:
        fmt["tags"]["major_brand"] = brand
    v = {"codec_type": "video", "codec_name": codec, "codec_tag_string": tag, "width": w, "height": h,
         "avg_frame_rate": "25/1", "tags": dict(stream_tags or {})}
    if trc:
        v["color_transfer"] = trc
    return {"format": fmt, "streams": [v]}


def _file(tmp_path, name, body=b"\0" * 4096):
    p = tmp_path / name
    p.write_bytes(body)
    return p


def test_identify_blackmagic_from_braw_tags(tmp_path, cfg_stub):
    # tag names as they appear in Blackmagic's own sample .braw (ships with Blackmagic RAW Speed Test)
    info = _info({"manufacturer": "Blackmagic Design", "camera_type": "Pocket Cinema Camera 6K Pro", "camera_id": "abc-1",
                  "viewing_gamma": "Blackmagic Design Film", "viewing_gamut": "Blackmagic Design"},
                 codec="unknown", tag="brhq", w=6144, h=3456)
    r = CAM.identify(cfg_stub, _file(tmp_path, "A001_10181400_C007.braw"), info)
    assert r["family"] == "bmd" and r["confidence"] >= 0.95 and r["raw"] == "braw"
    assert r["model"] == "Pocket Cinema Camera 6K Pro" and r["serial"] == "abc-1"
    assert r["log_profile"] == "bmd_film_gen5" and r["profile_src"] == "metadata"
    assert r["cam_key"] == "bmd-pocket-cinema-camera-6k-pro"


def test_identify_sony_from_xavc_and_sidecar(tmp_path, cfg_stub):
    clip = _file(tmp_path, "C0007.MP4")
    (tmp_path / "C0007M01.XML").write_text(
        '<NonRealTimeMeta><Device manufacturer="Sony" modelName="ILCE-7SM3" serialNo="555"/>'
        '<Item name="CaptureGammaEquation" value="s-log3-cine"/><Item name="CaptureColorPrimaries" value="S-Gamut3Cine"/>'
        '</NonRealTimeMeta>')
    from cutroom import ingest
    ingest._dir_cache.clear()
    r = CAM.identify(cfg_stub, clip, _info(brand="XAVC", codec="h264"))
    assert r["family"] == "sony" and r["model"] == "ILCE-7SM3" and r["serial"] == "555"
    assert r["log_profile"] == "sony_slog3" and r["profile_src"] == "metadata"
    (tmp_path / "C0007M01.XML").write_text(
        '<NonRealTimeMeta><Device manufacturer="Sony" modelName="ILCE-7SM3"/>'
        '<Item name="CaptureGammaEquation" value="s-log3"/><Item name="CaptureColorPrimaries" value="S-Gamut3"/></NonRealTimeMeta>')
    ingest._dir_cache.clear()
    assert CAM.identify(cfg_stub, clip, _info(brand="XAVC"))["log_profile"] == "sony_slog3_sg3"     # LUT-only


def test_identify_fujifilm_and_arri(tmp_path, cfg_stub):
    r = CAM.identify(cfg_stub, _file(tmp_path, "DSCF0042.MOV"),
                     _info({"com.apple.quicktime.make": "FUJIFILM", "com.apple.quicktime.model": "X-T5"}, codec="hevc"))
    assert (r["family"], r["model"], r["log_profile"]) == ("fuji", "X-T5", None)      # F-Log2 isn't tagged: estimated later
    r = CAM.identify(cfg_stub, _file(tmp_path, "DSCF0043.MOV", b"\0" * 64 + b"FUJIFILM X-T5 1.03\0" + b"\0" * 4096),
                     _info(codec="hevc"))
    assert r["family"] == "fuji" and r["confidence"] >= 0.75 and r["model"] == "X-T5"   # brand in the file's own atoms
    r = CAM.identify(cfg_stub, _file(tmp_path, "A001C003_261018_R1AB.mov"), _info())
    assert r["family"] == "arri" and 0.55 <= r["confidence"] < 0.8                       # naming alone: weak evidence
    r = CAM.identify(cfg_stub, _file(tmp_path, "clip.mov"), _info(trc="arib-std-b67"))
    assert r["family"] == "other" and r["log_profile"] == "hlg"


def test_brand_words_inside_picture_data_are_ignored(tmp_path, cfg_stub):
    junk = bytes(np.random.default_rng(0).integers(0, 256, 200_000, dtype=np.uint8)) + b"\x01sony\x02arri\x03"
    r = CAM.identify(cfg_stub, _file(tmp_path, "x.mov", junk), _info())
    assert r["family"] == "other"


def test_log_signature_separates_log_from_display():
    rng = np.random.default_rng(0)
    grey = rng.random((64, 64, 1)) ** 2 * 1.5
    scene = grey * (1 + 0.35 * (rng.random((64, 64, 3)) - 0.5))       # an ordinary, moderately coloured scene
    logf = CS.slog3_encode(scene)
    disp = np.clip(scene, 0, 1) ** (1 / 2.2)
    assert CAM.looks_log(CAM.log_signature([logf])) is True
    assert CAM.looks_log(CAM.log_signature([disp])) is False


# ============================================================================================ BWF / Rode
def _bwf(path, x, sr=48000, tref=0, originator="RODE", ixml=None, rf64=False):
    data = np.asarray(x, np.float32).tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, sr, sr * 4, 4, 32)
    b = bytearray(602)
    b[256:256 + len(originator)] = originator.encode()
    b[320:330], b[330:338] = b"2026-10-18", b"14:00:00"
    b[338:346] = struct.pack("<II", tref & 0xFFFFFFFF, tref >> 32)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"bext" + struct.pack("<I", len(b)) + bytes(b)
    if ixml:
        xb = ixml.encode()
        chunks += b"iXML" + struct.pack("<I", len(xb)) + xb + (b"\0" if len(xb) & 1 else b"")
    if rf64:
        ds = struct.pack("<QQQI", 0, len(data), len(data) // 4, 0)
        chunks = b"ds64" + struct.pack("<I", len(ds)) + ds + chunks + b"data" + struct.pack("<I", 0xFFFFFFFF) + data
        Path(path).write_bytes(b"RF64" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + chunks)
    else:
        chunks += b"data" + struct.pack("<I", len(data)) + data
        Path(path).write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)


def test_bwf_float_bext_and_ixml_timecode(tmp_path):
    sr = 48000
    tref = (14 * 3600 + 1) * sr + 12000
    _bwf(tmp_path / "a.wav", np.zeros(sr), sr, tref)
    m = bwf.read(tmp_path / "a.wav")
    assert m["ok"] and m["format"] == "float" and m["bits"] == 32 and m["frames"] == sr
    assert m["time_reference"] == tref and m["time_reference_src"] == "bext"
    assert m["bext"]["originator"] == "RODE" and bwf.tc_string(tref, sr, 25) == "14:00:01:06"
    ix = ("<BWFXML><SPEED><TIMECODE_RATE>25/1</TIMECODE_RATE><TIMESTAMP_SAMPLE_RATE>48000</TIMESTAMP_SAMPLE_RATE>"
          f"<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>0</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI><TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>{tref + 5}"
          "</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO></SPEED></BWFXML>")
    _bwf(tmp_path / "b.wav", np.zeros(100), sr, tref, ixml=ix, rf64=True)
    m = bwf.read(tmp_path / "b.wav")
    assert m["ok"] and m["rf64"] and m["time_reference"] == tref + 5 and m["time_reference_src"] == "ixml"
    assert m["frames"] == 100


def test_ffmpeg_decodes_32bit_float_overs(tmp_path):
    """32-bit float recordings above 0 dBFS must survive decoding (no clipping before the mix normalises)."""
    from cutroom.asr import load_audio
    sr = 48000
    x = (np.sin(np.arange(sr) * 2 * np.pi * 440 / sr) * 2.0).astype(np.float32)     # +6 dBFS peaks
    _bwf(tmp_path / "hot.wav", x, sr, 0)
    class C:  # noqa: E701
        ffmpeg = "ffmpeg"
    y = load_audio(C, tmp_path / "hot.wav", sr=sr)
    assert np.abs(y).max() > 1.9


def test_chunk_chains_join_split_takes():
    sr = 48000
    def it(i, tref, frames, dev=1, fam=None, idx=None):
        ix = {"family_uid": fam, "total_files": "2", "file_set_index": str(idx)} if fam else {}
        return {"id": f"c{i}", "path": f"/x/{i}.wav", "device": dev, "duration": frames / sr,
                "meta": {"time_reference": tref, "sample_rate": sr, "frames": frames, "ixml": ix}}
    t0 = 50000 * sr
    chains = bwf.chunk_chains([it(2, t0 + 1000 * sr, 300 * sr), it(1, t0, 1000 * sr), it(3, t0 + 5000 * sr, 10 * sr)])
    assert chains == [["c1", "c2"]]                                     # c3 starts much later: separate take
    chains = bwf.chunk_chains([it(5, None, 10, fam="F1", idx=2), it(4, None, 10, fam="F1", idx=1)])
    assert chains == [["c4", "c5"]]


# ============================================================================================ claps + clocks
def test_clap_detection_and_anchor():
    rng = np.random.default_rng(2)
    sr = 8000
    x = (0.01 * rng.standard_normal(sr * 10)).astype(np.float32)
    t_clap = 4.3217
    i = int(t_clap * sr)
    x[i:i + 160] += (rng.standard_normal(160) * np.exp(-np.arange(160) / 25)).astype(np.float32) * 0.8
    ca = sync.detect_claps(x, sr)
    assert ca and abs(ca[0][0] - t_clap) < 0.002
    y = np.concatenate([np.zeros(int(1.25 * sr), np.float32), x])          # the same clap 1.25 s later
    cb = sync.detect_claps(y, sr)
    anch = sync.clap_anchor(ca, cb, lambda t: -1.25)
    assert anch and abs(anch[0][2]) < 0.002
    assert not sync.clap_anchor(ca, cb, lambda t: -0.9)                   # a different alignment: no agreement


def test_estimate_clocks_from_matched_pairs(tmp_path):
    from types import SimpleNamespace
    con = db.connect(tmp_path / "l.db")
    sync.ensure_schema(con)
    for d in (1, 2, 3):
        con.execute("INSERT INTO devices (id, key, code, kind, name) VALUES (?,?,?,?,?)", (d, f"k{d}", f"cam{d}", "camera", f"D{d}"))
    base = 1_760_000_000.0
    true_start = {"a1": 0.0, "a2": 600.0, "b1": 5.0, "b2": 610.0, "c1": 2.0}
    clock = {1: 0.0, 2: 437.0, 3: -12.5}                                  # device 2's clock 7 min fast, 3 is 12.5 s slow
    dev = {"a1": 1, "a2": 1, "b1": 2, "b2": 2, "c1": 3}
    for c, s in true_start.items():
        con.execute("INSERT INTO clips (id, path, duration, width) VALUES (?,?,?,?)", (c, f"/{c}", 60.0, 1920))
        con.execute("INSERT INTO clip_media (clip_id, device_id, start_epoch, start_src) VALUES (?,?,?,?)",
                    (c, dev[c], base + s + clock[dev[c]], "creation_time"))
    for a, b in (("a1", "b1"), ("a2", "b2"), ("a1", "c1")):
        con.execute("INSERT INTO sync_pairs (a, b, ok, lag, method, algo) VALUES (?,?,1,?, 'audio', 4)",
                    (a, b, true_start[b] - true_start[a]))
    con.commit()
    rows = {r["device_id"]: r for r in sync.estimate_clocks(SimpleNamespace(), con, ref_device=1)}
    assert rows[2]["offset_s"] == pytest.approx(437.0, abs=0.01) and rows[3]["offset_s"] == pytest.approx(-12.5, abs=0.01)
    assert sync.corrected_start(con, "b2") == pytest.approx(base + 610.0, abs=0.01)


# ============================================================================================ transforms end to end
@pytest.fixture
def cfg_stub(tmp_path):
    from cutroom.config import Config
    c = Config(work=str(tmp_path / "work"))
    Path(c.work, "luts").mkdir(parents=True, exist_ok=True)
    Path(c.work, "cache").mkdir(parents=True, exist_ok=True)
    return c


def _scene(n=48):
    rng = np.random.default_rng(9)
    g = rng.random((n, n, 3)) ** 1.6 * 0.9 + 0.01
    g[: n // 4] = np.linspace(0.01, 1.2, n)[None, :, None]
    return g


def test_math_cameras_land_on_the_arri_rendering(cfg_stub):
    """Sony S-Log3 and Fuji F-Log2 pictures of a scene, through their camera transform and the (maths) show transform,
    equal the ARRI's picture of the same scene: the matching space is exact for published curves."""
    lin = _scene()
    st = CAM.ct_settings({})
    arri, _ = CAM.transform_fn(cfg_stub, {"cam_key": "arri", "family": "arri", "log_profile": "arri_logc3"}, st)
    ref = arri(CS.logc3_encode(CS.apply3(lin, CS.gamut_matrix("rec709", "awg3"))))
    for prof, gam, enc in (("sony_slog3", "sgamut3cine", CS.slog3_encode), ("fuji_flog2", "fgamut", CS.flog2_encode)):
        code = enc(CS.apply3(lin, CS.gamut_matrix("rec709", gam)))
        fn, desc = CAM.transform_fn(cfg_stub, {"cam_key": prof, "family": prof[:4], "log_profile": prof}, st)
        assert desc["input"]["kind"] == "math"
        out = fn(code)
        de = CS.delta_e2000(CS.display_to_lab(out), CS.display_to_lab(ref))
        assert float(np.percentile(de, 95)) < 0.5, prof


def test_lut_only_profile_needs_a_lut(cfg_stub):
    st = CAM.ct_settings({})
    it = CAM.input_transform(cfg_stub, {"cam_key": "bmd-x", "family": "bmd", "log_profile": "bmd_film_gen5"}, st)
    if CAM.vendor_lut("bmd_film_gen5"):
        assert it["kind"] == "lut" and "Gen 5" in it["path"]
    else:
        assert it["kind"] == "none" and "luts/bmd-x.cube" in it["why"]
    CS.Cube.from_function(lambda x: x, 5).write(Path(cfg_stub.work) / "luts" / "bmd.cube")
    it = CAM.input_transform(cfg_stub, {"cam_key": "bmd-x", "family": "bmd", "log_profile": "bmd_film_gen5"}, st)
    assert it["kind"] == "lut" and it["path"].endswith("bmd.cube") and it["src"] == "creator"


def test_show_lut_resolution_order(cfg_stub):
    sh = CAM.show_transform(cfg_stub, {})
    assert sh["kind"] in ("lut", "math")                       # vendor (Resolve) LUT if installed, else ARRI maths
    CS.Cube.from_function(lambda x: x, 5).write(Path(cfg_stub.work) / "luts" / "arri_logc3.cube")
    assert CAM.show_transform(cfg_stub, {})["src"] == "creator"
    with pytest.raises(FileNotFoundError):
        CAM.show_transform(cfg_stub, {"show_lut": "missing.cube"})
    from cutroom import looks
    assert "arri_logc3.cube" not in [p.name for p in looks.lut_files(cfg_stub)]   # not offered as a creative look


def test_camera_transforms_setting_validation():
    assert CAM.validate({"grade": {"camera_transforms": "auto"}}) == []
    assert CAM.validate({"grade": {"camera_transforms": {"hero": "arri", "cameras": {"fuji-x-t5": {"profile": "fuji_flog2"}}}}}) == []
    assert CAM.validate({"grade": {"camera_transforms": {"cameras": {"x": {"profile": "nope"}}}}})
    assert CAM.ct_settings({"grade": {"camera_transforms": "off"}})["mode"] == "off"


def _clip(path, rgb_code, dur=2.0):
    import cv2
    png = Path(path).with_suffix(".png")
    cv2.imwrite(str(png), (np.clip(rgb_code, 0, 1)[..., ::-1] * 65535 + 0.5).astype(np.uint16))
    subprocess.run(FF + ["-loop", "1", "-framerate", "25", "-i", png, "-t", str(dur), "-vf",
                         "scale=out_range=tv:out_color_matrix=bt709:flags=accurate_rnd+full_chroma_int,"
                         "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
                         "-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le", str(path)], check=True)
    png.unlink()


def test_match_fit_and_grade_chain_use_one_combined_lut(tmp_path):
    """A Sony clip with a white-balance / exposure error and an ARRI clip of the same scene at the same time: the
    match is fitted from their frames (by capture time), lowers ΔE, and the render's grade chain carries exactly one
    lut3d (the combined camera LUT) for the Sony clip."""
    from cutroom import ingest, render
    from cutroom import plan as P
    from cutroom.config import init_project
    lib = tmp_path / "lib"
    lib.mkdir()
    lin = _scene(96)
    _clip(lib / "A001C001_261018_R1AB.mov", CS.logc3_encode(CS.apply3(lin, CS.gamut_matrix("rec709", "awg3"))))
    off = lin * np.array([1.1, 1.0, 0.85]) * 2 ** 0.4
    _clip(lib / "C0001.mov", CS.slog3_encode(CS.apply3(off, CS.gamut_matrix("rec709", "sgamut3cine"))))
    (lib / "C0001M01.XML").write_text('<NonRealTimeMeta><Device manufacturer="Sony" modelName="FX3"/>'
                                      '<Item name="CaptureGammaEquation" value="s-log3-cine"/></NonRealTimeMeta>')
    cfg = init_project(tmp_path / "work", [lib])
    cfg.min_free_disk_gb = 0.0
    cfg.save()
    con = db.connect(cfg.db_path)
    ingest._dir_cache.clear()
    ingest.scan(cfg, con)
    ingest.probe_all(cfg, con)
    con.execute("UPDATE clips SET shot_at='2026-10-18T14:00:00'")
    con.commit()
    cams = {c["family"]: c for c in CAM.cameras(con)}
    assert set(cams) == {"arri", "sony"}
    assert cams["arri"]["profiles"] == {"arri_logc3 (assumed)": 1}          # from the histogram (no tag)
    assert cams["sony"]["profiles"] == {"sony_slog3 (metadata)": 1}
    res = CAM.match_camera(cfg, con, cams["sony"]["key"], "2026-10-18")
    assert res["ok"] and res["de_after"] < 0.5 * res["de_before"], res
    row = dict(db.get_clip(con, "C0001"))
    plan = P.normalize({"sections": [], "style": {}}, cfg)
    ctx = render.Ctx(cfg, con, plan, preview=False)
    f, tag = render.grade_chain(ctx, row, ctx.source(row), 640, 360)
    assert tag.startswith("camera:sony") and sum("lut3d" in x for x in f) == 1
    ctx_off = render.Ctx(cfg, con, P.normalize({"sections": [], "style": {"grade": {"camera_transforms": "off"}}}, cfg), False)
    _, tag_off = render.grade_chain(ctx_off, row, row["path"], 640, 360)
    assert not tag_off.startswith("camera")                                  # legacy path when switched off
    out = CAM.export_luts(cfg, con, tmp_path / "luts")
    names = sorted(Path(x["file"]).name for x in out)
    assert any(n.startswith("sony") for n in names) and any(x.get("logc3_file") for x in out)
    # the creator corrects the ARRI's assumed profile for the whole camera; library filter by camera
    from cutroom import tags
    ids = CAM.set_override(con, "A001C001", profile="arri_logc3", scope="camera")
    assert len(ids) == 1 and CAM.get(con, ids[0])["profile_src"] == "user"
    assert CAM.badges(con)[ids[0]]["assumed"] is False
    q = tags.query(cfg, con, body="sony")
    assert q["total"] == 1 and q["rows"][0]["id"] == row["id"]
    CAM.record(cfg, con, dict(db.get_clip(con, "A001C001")))               # re-identifying keeps the correction
    assert CAM.get(con, ids[0])["profile_src"] == "user"


def test_conform_50p_slowmo_only_with_ramp(tmp_path):
    from types import SimpleNamespace
    from cutroom import render
    ctx = SimpleNamespace(fps=25.0, style={"conform": "auto"}, src_fps=lambda row, src: (50, 1))
    assert render._conform(ctx, {"fps": 50}, "x", {"ramp": {"to": 2}}, sync=False) == ("slowmo", 0.5)
    assert render._conform(ctx, {"fps": 50}, "x", {}, sync=False) == ("dup", 1.0)       # real time otherwise
    assert render._conform(ctx, {"fps": 50}, "x", {"ramp": {"to": 2}}, sync=True)[0] == "dup"
    ctx.src_fps = lambda row, src: (30000, 1001)
    assert render._conform(ctx, {"fps": 29.97}, "x", {"conform": "slowmo"}, sync=False)[0] == "dup"   # not 2x: refused
