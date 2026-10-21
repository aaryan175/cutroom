"""Reference engine (cutroom/reference.py): a synthetic reference is measured frame-exactly, compared against
a render with different pacing, and served by the studio routes.

The reference (24 fps, so every 120 BPM beat is a whole frame): a fast cold open with a one-frame white flash,
a title card with real text in the lower band, a 4 s shot with speech (macOS `say`, when present), and a
0.5 s-per-shot montage, all over a click track whose beats the cuts land on."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

FPS = 24
W, H = 640, 360
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
# (frames, lavfi source or TITLE)
SHOTS = [(24, "color=c=0x8b1a1a"), (12, "color=c=0x1a6b8b"), (12, "color=c=0x2e8b1a"), (1, "color=c=white"),
         (23, "color=c=0x6b1a8b"), (48, "TITLE"), (96, "testsrc2"),
         (12, "color=c=0xb07020"), (12, "color=c=0x2050c0"), (12, "color=c=0x20a090"), (12, "color=c=0xa02060"),
         (12, "color=c=0x80a020"), (12, "color=c=0x404040")]
RENDER_SHOTS = [(72, "color=c=0x30507a"), (72, "color=c=0x7a5030"), (72, "color=c=0x307a50"), (72, "color=c=0x6a306a")]


def expected_cuts(shots=SHOTS):
    out, f = [], 0
    for n, _ in shots[:-1]:
        f += n
        out.append(f)
    return out


def _title_png(path, text=True):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (W, H), (24, 24, 28))
    if text:
        d = ImageDraw.Draw(im)
        font = None
        for fp in ("/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"):
            if Path(fp).exists():
                font = ImageFont.truetype(fp, int(H * 0.075))
                break
        font = font or ImageFont.load_default()
        msg = "MAKE IT LIKE THIS"
        bb = d.textbbox((0, 0), msg, font=font)
        d.text(((W - bb[2]) / 2, H * 0.80 - bb[3] / 2), msg, fill=(255, 255, 255), font=font)
    im.save(path)


def make_video(out, shots, speech=False):
    """Concatenate exact-length shots + a 120 BPM click (+ a spoken line from 5 s). Returns (path, has_speech)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = sum(n for n, _ in shots) / FPS
    inputs, chains, k = [], [], 0
    for j, (n, src) in enumerate(shots):
        if src == "TITLE":
            png = out.with_name(out.stem + "_title.png")
            _title_png(png)
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{n / FPS + 1:.3f}", "-i", str(png)]
            chains.append(f"[{k}:v]scale={W}:{H},format=yuv420p,trim=end_frame={n},setpts=PTS-STARTPTS[v{j}]")
            k += 1
        else:
            sep = ":" if "=" in src else "="
            chains.append(f"{src}{sep}s={W}x{H}:r={FPS}:d={n / FPS + 1:.3f},format=yuv420p,trim=end_frame={n},"
                          f"setpts=PTS-STARTPTS[v{j}]")
    graph = ";".join(chains) + ";" + "".join(f"[v{j}]" for j in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=0[v]"
    a_in = ["-f", "lavfi", "-i", f"aevalsrc='0.6*sin(2*PI*1000*t)*exp(-60*mod(t\\,0.5))':d={dur:.3f}:s=44100"]
    agraph = f"[{k}:a]aformat=channel_layouts=mono,volume=0.35[ck]"
    has_speech = False
    if speech and shutil.which("say"):
        sp = out.with_name(out.stem + "_speech.aiff")
        r = subprocess.run(["say", "-o", str(sp), "Hello and welcome back. Today we are going to make it exactly "
                                                  "like this video."], capture_output=True)
        if r.returncode == 0 and sp.exists():
            a_in += ["-i", str(sp)]
            agraph += (f";[{k + 1}:a]aresample=44100,aformat=channel_layouts=mono,atrim=0:3.9,volume=2.0,adelay=5000[sp];"
                       f"[ck][sp]amix=inputs=2:normalize=0:duration=first[a]")
            has_speech = True
    if not has_speech:
        agraph += ";[ck]anull[a]"
    subprocess.run(FF + inputs + a_in + ["-filter_complex", graph + ";" + agraph, "-map", "[v]", "-map", "[a]",
                                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-g", "12",
                                         "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "128k",
                                         "-t", f"{dur:.4f}", str(out)], check=True)
    return out, has_speech


@pytest.fixture(scope="module")
def refproj(tmp_path_factory):
    from cutroom import governor
    from cutroom import reference as R
    from cutroom.config import init_project
    old = os.environ.get("CUTROOM_REF_ASR_MODEL")
    os.environ["CUTROOM_REF_ASR_MODEL"] = "tiny"           # fast; the real default is cfg.ref_asr_model
    base = tmp_path_factory.mktemp("refs")
    (base / "lib").mkdir()
    ref, has_speech = make_video(base / "src" / "reference.mp4", SHOTS, speech=True)
    render, _ = make_video(base / "src" / "my_render.mp4", RENDER_SHOTS)
    cfg = init_project(base / "work", [base / "lib"])
    governor.reset()
    t = time.time()
    m = R.add(cfg, ref, title="synthetic reference", analyze_now=True, echo=lambda *_: None)
    took = time.time() - t
    yield {"cfg": cfg, "id": m["id"], "meta": m, "ref": ref, "render": render, "speech": has_speech, "base": base,
           "took": took}
    if old is None:
        os.environ.pop("CUTROOM_REF_ASR_MODEL", None)
    else:
        os.environ["CUTROOM_REF_ASR_MODEL"] = old


def test_add_copies_and_analyzes(refproj):
    from cutroom import reference as R
    cfg, rid = refproj["cfg"], refproj["id"]
    d = R.ref_dir(cfg, rid)
    src = R.source_path(cfg, rid)
    assert src.is_file() and not src.is_symlink() and src.parent == d            # copied into the project
    assert refproj["ref"].exists()                                                # original untouched
    assert (d / "proxy.mp4").exists() and (d / "analysis.json").exists()
    m = R.load_meta(cfg, rid)
    assert m["status"] == "analyzed" and m["fps"] == 24 and abs(m["duration"] - 12.0) < 0.05
    # the same file again gives the same id and keeps the analysis
    m2 = R.add(cfg, refproj["ref"], analyze_now=False, make_proxy_now=False, echo=lambda *_: None)
    assert m2["id"] == rid and m2["status"] == "analyzed"


def test_cuts_frame_exact_and_flash(refproj):
    from cutroom import reference as R
    an = R.load_analysis(refproj["cfg"], refproj["id"])
    got = [s["f0"] for s in an["shots"][1:]]
    want = expected_cuts()
    assert len(got) == len(want), (got, want)
    assert all(abs(a - b) <= 1 for a, b in zip(got, want)), (got, want)
    fl = an["flashes"]
    assert len(fl) == 1 and abs(fl[0]["f0"] - 48) <= 1 and fl[0]["frames"] == 1 and fl[0]["kind"] == "white"
    flash_shot = next(s for s in an["shots"] if s["f0"] == fl[0]["f0"])
    assert flash_shot["kind"] == "flash" and flash_shot["frames"] == 1


def test_pacing_beats_loudness_captions_sheets(refproj):
    from cutroom import reference as R
    cfg, rid = refproj["cfg"], refproj["id"]
    an = R.load_analysis(cfg, rid)
    p, au = an["pacing"], an["audio"]
    assert p["n_shots"] == 13 and abs(p["asl"] - 12.0 / 13) < 0.02
    assert p["first3_cuts"] == 4 and sum(p["cuts_per_s"]) == 12 and len(p["cuts_per_s"]) == 12
    assert au["integrated_lufs"] is not None and au["true_peak_dbtp"] is not None
    assert 110 <= au["tempo"] <= 130 and au["music_pct"] > 50
    assert au["beat_sync_pct"] >= 80, au
    assert au["beatless"] is False and au["pulse_strength"] >= R.PULSE_BEATLESS     # a click track has a beat
    assert an["section_shares"] and abs(sum(a["share"] for a in an["section_shares"]) - 100) < 0.5
    assert au["chance_pct"] < au["beat_sync_pct"]
    cap = an["captions"]
    assert cap["detected"] and cap["band"] == "bottom" and cap["position"] in ("lower", "bottom")
    assert 3.0 <= cap["size_pct"] <= 9.0 and cap["outline"] is not True
    title = next(s for s in an["shots"] if s["f0"] == 72)
    assert title["kind"] == "title" and title["text"]["present"]
    if title["text"]["method"] == "ocr":
        assert "LIKE" in title["text"]["words"].upper()
    assert an["sheets"] and all((R.ref_dir(cfg, rid) / s).exists() for s in an["sheets"])
    assert all((R.ref_dir(cfg, rid) / s["thumb"]).exists() for s in an["shots"])
    assert an["sections"] and an["sections"][0]["start"] == 0 and abs(an["sections"][-1]["end"] - 12.0) < 0.05
    assert an["structure"]["montage_pct"] > 30
    assert len(an["summary"]) >= 5


def test_speech_and_cold_open(refproj):
    from cutroom import reference as R
    if not refproj["speech"]:
        pytest.skip("no `say` voice on this machine")
    an = R.load_analysis(refproj["cfg"], refproj["id"])
    st = an["structure"]
    assert an["audio"]["speech_pct"] > 15
    assert 4.0 <= st["time_to_first_word_s"] <= 6.0
    assert 2.5 <= st["cold_open_s"] <= 5.5, st
    talk = next(s for s in an["shots"] if s["f0"] == 120)
    assert talk["speech"] > 0.6
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return
    assert an["transcript"] and "welcome" in an["transcript"]["text"].lower()
    assert an["transcript"]["model"] == "tiny" and an["transcript"]["words"]


def test_cutmap_rows(refproj):
    from cutroom import reference as R
    rows = R.cutmap(refproj["cfg"], refproj["id"])
    assert len(rows) == 13
    assert rows[3]["frames"] == 1 and rows[3]["kind"].startswith("flash")
    assert rows[7]["beat"] is True and rows[7]["kind"] == "broll/montage"
    assert {"i", "start", "dur", "kind", "beat", "text", "luma", "section"} <= set(rows[0])


def test_analyze_resumes_without_redoing(refproj):
    from cutroom import reference as R
    cfg, rid = refproj["cfg"], refproj["id"]
    before = R.load_analysis(cfg, rid)["measured_at"]
    t = time.time()
    an = R.analyze(cfg, rid, echo=lambda *_: None)
    assert an["measured_at"] == before and time.time() - t < 5
    # an interrupted run keeps its finished stages: drop the final file, keep work/, re-assemble
    (R.ref_dir(cfg, rid) / "analysis.json").unlink()
    lines = []
    an2 = R.analyze(cfg, rid, echo=lines.append)
    assert [s["f0"] for s in an2["shots"]] == [s["f0"] for s in an["shots"]]
    assert R.load_meta(cfg, rid)["status"] == "analyzed"


def test_compare_render_and_timeline(refproj):
    from cutroom import reference as R
    cfg, rid = refproj["cfg"], refproj["id"]
    res = R.compare(cfg, rid, refproj["render"], echo=lambda *_: None)
    assert res["mode"] == "render" and res["verdict"] == "far"
    text = " ".join(g["text"] for g in res["gaps"])
    assert "cold open" in text or "cut every" in text, text
    metrics = {g["metric"] for g in res["gaps"]}
    assert "asl" in metrics and "first3_cuts" in metrics, metrics
    row = {r["metric"]: r for r in res["rows"]}
    assert row["n_shots"]["yours"] == 4 and row["n_shots"]["ref"] == 13
    assert len(res["sections"]) == len(R.load_analysis(cfg, rid)["sections"])
    assert len(res["curve"]["ref"]) == len(res["curve"]["yours"])
    # the reference against itself has no big gaps
    same = R.compare(cfg, rid, R.source_path(cfg, rid), echo=lambda *_: None)
    assert same["n_big"] == 0, same["gaps"]
    # pacing-only: a resolved-timeline-like dict
    tl = {"duration": 12.0, "fps": 24, "sections": [{"name": "cold open", "start": 0.0, "end": 9.0},
                                                    {"name": "main", "start": 9.0, "end": 12.0}],
          "pieces": [{"track": "V1", "start": 0.0, "end": 9.0, "type": "broll", "clip": "a", "in": 0, "out": 9},
                     {"track": "V1", "start": 9.0, "end": 12.0, "type": "talk", "clip": "b", "in": 0, "out": 3}]}
    r2 = R.compare(cfg, rid, tl, style={"captions": {"enabled": True, "position": "top", "size": 54}}, out_h=1080)
    assert r2["mode"] == "timeline" and any(g["metric"] == "cold_open" for g in r2["gaps"])
    assert any(g["metric"] == "caption_position" for g in r2["gaps"])


def test_cli(refproj):
    cfg, rid = refproj["cfg"], refproj["id"]
    env = {**os.environ, "CUTROOM_WORK": cfg.work}
    run = lambda *a: subprocess.run([sys.executable, "-m", "cutroom.cli", "ref", *a], capture_output=True,  # noqa: E731
                                    text=True, env=env, cwd=cfg.work, timeout=300)
    r = run("list")
    assert r.returncode == 0 and rid in r.stdout and "analyzed" in r.stdout
    r = run("show", rid[:6])
    assert r.returncode == 0 and "ASL" in r.stdout and "sheet:" in r.stdout
    r = run("cutmap", rid, "--json")
    assert r.returncode == 0 and len(json.loads(r.stdout)) == 13
    r = run("cutmap", rid)
    assert r.returncode == 0 and "flash" in r.stdout
    r = run("compare", rid, str(refproj["render"]))
    assert r.returncode == 0 and "gaps:" in r.stdout and "[big]" in r.stdout, r.stdout + r.stderr
    r = run("show", "nope-no-such")
    assert r.returncode != 0


# ------------------------------------------------------------------------------------------------ studio
def _req(port, method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r.status, dict(r.getheaders()), data
    return r.status, json.loads(data or b"null")


@pytest.fixture(scope="module")
def srv(refproj):
    from cutroom import studio
    srv, st = studio.make_server(refproj["cfg"], 0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    yield {"port": srv.server_address[1], "studio": st}
    st.close()
    srv.shutdown()
    srv.server_close()


def _wait_job(port, jid, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        code, j = _req(port, "GET", f"/api/jobs/{jid}")
        assert code == 200
        if j["status"] in ("done", "error", "stopped"):
            return j
        time.sleep(0.5)
    raise AssertionError(f"job {jid} did not finish")


def test_studio_reference_routes(refproj, srv):
    cfg, rid, port = refproj["cfg"], refproj["id"], srv["port"]
    # the old Reference-tab folder keeps working and its files surface as references
    refdir = Path(cfg.work) / "renders" / "reference"
    refdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(refproj["render"], refdir / "dropped.mp4")
    code, hdr, body = _req(port, "GET", "/media/ref/dropped.mp4", headers={"Range": "bytes=0-99"}, raw=True)
    assert code == 206 and len(body) == 100
    code, out = _req(port, "GET", "/api/refs")
    assert code == 200
    rows = {r["id"]: r for r in out["refs"]}
    assert rows[rid]["status"] == "analyzed" and rows[rid]["thumb"] and rows[rid]["proxy_url"]
    dropped = [r for r in out["refs"] if r["origin"] == "reference folder"]
    assert len(dropped) == 1 and dropped[0]["status"] == "added" and dropped[0]["file"] == "dropped.mp4"
    code, out2 = _req(port, "GET", "/api/refs")                    # imported once, not again
    assert len(out2["refs"]) == len(out["refs"])

    code, one = _req(port, "GET", f"/api/ref/{rid}")
    assert code == 200 and one["analysis"]["pacing"]["n_shots"] == 13
    assert len(one["thumbs"]) == 13 and len(one["cutmap"]) == 13 and one["sheets"]
    code, hdr, body = _req(port, "GET", one["thumbs"][3], raw=True)
    assert code == 200 and hdr["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
    code, hdr, body = _req(port, "GET", one["sheets"][0], raw=True)
    assert code == 200 and body[:2] == b"\xff\xd8"
    code, hdr, body = _req(port, "GET", f"/media/refproxy/{rid}", headers={"Range": "bytes=10-209"}, raw=True)
    assert code == 206 and len(body) == 200 and hdr["Content-Range"].startswith("bytes 10-209/")
    assert _req(port, "GET", "/api/ref/doesnotexist")[0] == 404
    assert _req(port, "GET", f"/media/refthumb/{rid}/999")[0] == 404
    assert _req(port, "GET", f"/api/ref/{dropped[0]['id']}/compare?render=x")[0] == 409   # not analyzed yet

    # compare: an explicit render, then a plan that has no render (pacing-only from the resolved draft)
    shutil.copyfile(refproj["render"], Path(cfg.work) / "renders" / "mine_v1_preview.mp4")
    code, cmp_ = _req(port, "GET", f"/api/ref/{rid}/compare?render=mine_v1_preview.mp4")
    assert code == 200 and cmp_["mode"] == "render" and cmp_["gaps"] and cmp_["render_url"]
    plan = {"title": "Demo", "version": 1, "sections": [
        {"name": "cold open", "segments": [{"type": "black", "dur": 2.0}]},
        {"name": "main", "segments": [{"type": "title", "text": "hello", "dur": 4.0}]}]}
    (Path(cfg.work) / "plans" / "demo_v1.json").write_text(json.dumps(plan))
    code, cmp2 = _req(port, "GET", f"/api/ref/{rid}/compare?plan=demo")
    assert code == 200 and cmp2["mode"] == "timeline" and cmp2["render"] is None, cmp2
    assert cmp2["yours"]["duration"] == pytest.approx(6.0, abs=0.05)
    assert _req(port, "GET", f"/api/ref/{rid}/compare")[0] == 400

    # POST: add (same file -> same id, analysis reused) and re-analyze, as jobs with progress
    code, j = _req(port, "POST", "/api/refs", {"src": str(refproj["ref"]), "title": "again"})
    assert code == 200 and j["job"]
    done = _wait_job(port, j["job"])
    assert done["status"] == "done" and done["kind"] == "ref" and done["meta"].get("ref") == rid, done
    code, j = _req(port, "POST", f"/api/ref/{rid}/analyze", {})
    assert code == 200 and _wait_job(port, j["job"])["status"] == "done"
    assert _req(port, "POST", "/api/refs", {"src": "/no/such/file.mp4"})[0] == 404
    assert _req(port, "POST", "/api/refs", {})[0] == 400


# ------------------------------------------------------------------------------------------------ reader units

def test_pulse_strength_beatless_pad_vs_click_track():
    """A sustained pad with slow swells (a beatless film score) has no beat grid; a click track does."""
    from cutroom import reference as R
    sr = 16000
    t = np.arange(40 * sr) / sr
    rng = np.random.default_rng(0)
    pad = (0.3 * np.sin(2 * np.pi * 110 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.07 * t))
           + 0.02 * rng.standard_normal(len(t))).astype(np.float32)
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-60 * (t % 0.5))).astype(np.float32)
    p, c = R.pulse_strength(pad, sr), R.pulse_strength(click, sr)
    assert p["beatless"] is True and p["pulse_strength"] < R.PULSE_BEATLESS, p
    assert c["beatless"] is False and c["pulse_strength"] > 0.25, c
    # a beatless reference reports no beat sync (no BPM fitted to speech or swells)
    bs = R._beat_sync([1.0, 2.0, 3.0], {"beatless": True, "pulse_strength": 0.12, "beats": [0.5, 1.0, 1.5, 2.0]}, 24)
    assert bs["beat_sync_pct"] is None and bs["beatless"] and "beatless" in bs["note"]


def test_voice_cuts_breaks_mid_sentence_and_onsets():
    from cutroom import reference as R
    words = [[1.0, 1.2, "One"], [1.25, 1.5, "thing."], [2.2, 2.4, "You"], [2.45, 2.7, "did"], [2.75, 3.0, "it."],
             [3.05, 3.3, "Love"], [3.35, 3.6, "you."]]
    vc = R.voice_cuts([0.2, 1.9, 2.44, 3.04, 9.0], words)
    # 0.2: no words before it; 9.0: none after within 3 s -> not under the voice
    assert vc["n_under_voice"] == 3
    by_t = {r["t"]: r for r in vc["cuts"]}
    assert by_t[1.9]["on"] == "break"                       # in the 0.7 s pause after "thing."
    assert by_t[2.44]["on"] == "mid"                        # between "You" and "did"
    assert by_t[3.04]["on"] == "break"                      # after "it." (sentence end), right on "Love"
    assert vc["on_break"] == 2 and vc["mid_sentence"] == 1
    assert vc["near_word_onset"] == 2 and vc["near_word_onset_30ms"] == 2
    assert R.voice_cuts([1.0], [])["n_under_voice"] == 0


def test_section_shares_split_at_silence_hole_and_end_card():
    """The reference's acts: noise, then a hard cut to a near-silent hole, voice, payoff, end card on black."""
    from cutroom import reference as R
    dur = 60.0
    loud = [-12.0] * 10 + [-50.0] * 8 + [-14.0] * 38 + [-60.0, -70.0, None, None]
    audio = {"integrated_lufs": -14.0, "loudness_curve": loud}
    starts = [0.0, 4.0, 9.9, 14.0, 18.0, 30.0, 45.0, 56.0]
    shots = [{"start": a, "end": b, "luma": 40.0, "speech": 0.0} for a, b in zip(starts, starts[1:] + [dur])]
    shots[-1]["luma"] = 1.0                                  # a logo on black
    sections = [{"start": 0.0, "end": 18.0, "name": "b-roll"}, {"start": 18.0, "end": 60.0, "name": "voice-over b-roll"}]
    sh = R.section_shares(sections, shots, audio, dur)
    assert [a["name"] for a in sh] == ["b-roll", "silence", "voice-over b-roll", "end card"], sh
    assert [a["t0"] for a in sh] == [0.0, 9.9, 18.0, 56.0]
    assert abs(sum(a["share"] for a in sh) - 100) < 0.5


def test_pillarbox_frames_read_as_pillarbox():
    """5:3 picture inside a 16:9 frame: flat black bars left and right, picture top to bottom (DC06: 60/59 px
    bars at 1918 wide). It reads as its own device kind, never as a gate or an inset."""
    from cutroom import reference as R
    rng = np.random.default_rng(3)
    feats = []
    for k in range(30):
        f = np.zeros((180, 320, 3), np.uint8)
        pic = rng.integers(10, 200, (180, 300, 3), dtype=np.uint8)
        f[:, 10:310] = pic                                    # 10 px bars each side at 320 = 3.1 %
        feats.append(R.device_features(f))
    assert all(R._frame_kind(x) == "pillarbox" for x in feats)
    d = R.detect_devices(feats, 24.0)
    fr = [r for r in d["frames"] if r["kind"] == "pillarbox"]
    assert fr and abs(fr[0]["box"][0] - 10 / 320) < 0.01 and abs(fr[0]["box"][2] - 310 / 320) < 0.01
    assert fr[0]["color"] == "#000000"
    assert R.device_kinds({"devices": d}) == ["pillarbox"]
    # a full-frame picture with dark edges only in the corners is not a pillarbox
    g = np.full((180, 320, 3), 120, np.uint8)
    assert R._frame_kind(R.device_features(g)) == "full"
