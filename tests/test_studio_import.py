"""Studio Import (cutroom/imports.py + the /api/upload, /api/import/* routes): streaming uploads in pieces and
as a chunked body, size limits, kind routing, traversal-safe names, footage linked (never copied) and indexed
as a job, a reference added from an upload and measured as a job that reports on SSE `ref`, saved-version
state. Runs the real server on a random port over a tiny synthetic project."""
import http.client
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def _video(path, secs=3, colour="0x3060a0", tone=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(FF + ["-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=25:d={secs}",
                         "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={secs}",
                         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                         "-shortest", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    from cutroom import governor, studio
    from cutroom.config import init_project
    old = os.environ.get("CUTROOM_REF_ASR_MODEL")
    os.environ["CUTROOM_REF_ASR_MODEL"] = "tiny"
    base = tmp_path_factory.mktemp("imp")
    _video(base / "lib" / "a.mp4")
    cfg = init_project(base / "work", [base / "lib"])
    cfg.workers = 1
    cfg.min_free_disk_gb = 0.0
    cfg.save()
    governor.reset()
    srv_, st = studio.make_server(cfg, 0)
    th = threading.Thread(target=srv_.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    yield {"cfg": cfg, "port": srv_.server_address[1], "studio": st, "base": base}
    st.close()
    srv_.shutdown()
    srv_.server_close()
    if old is None:
        os.environ.pop("CUTROOM_REF_ASR_MODEL", None)
    else:
        os.environ["CUTROOM_REF_ASR_MODEL"] = old


def call(S, method, path, body=None, raw_body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", S["port"], timeout=120)
    h = dict(headers or {})
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    try:
        return r.status, json.loads(payload or b"null")
    except ValueError:
        return r.status, payload


def chunked(S, path, pieces):
    """POST with Transfer-Encoding: chunked (what a streaming client sends)."""
    c = http.client.HTTPConnection("127.0.0.1", S["port"], timeout=120)
    c.putrequest("POST", path)
    c.putheader("Transfer-Encoding", "chunked")
    c.endheaders()
    for p in pieces:
        c.send(f"{len(p):x}\r\n".encode() + p + b"\r\n")
    c.send(b"0\r\n\r\n")
    r = c.getresponse()
    out = r.status, json.loads(r.read())
    c.close()
    return out


def wait_job(S, jid, timeout=180):
    t = time.time()
    while time.time() - t < timeout:
        code, j = call(S, "GET", f"/api/jobs/{jid}")
        if j["status"] in ("done", "error", "stopped"):
            return j
        time.sleep(0.3)
    raise AssertionError(f"job {jid} did not finish")


class Events:
    def __init__(self, S):
        self.q = queue.Queue()
        self.c = http.client.HTTPConnection("127.0.0.1", S["port"], timeout=300)
        self.c.request("GET", "/api/events")
        self.r = self.c.getresponse()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        ev, data = None, []
        try:
            while True:
                line = self.r.fp.readline()
                if not line:
                    return
                line = line.decode().rstrip("\n")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line == "" and data:
                    self.q.put((ev, json.loads("\n".join(data))))
                    ev, data = None, []
        except Exception:  # noqa: BLE001
            return

    def collect(self, until, timeout=240):
        got, t_end = [], time.time() + timeout
        while time.time() < t_end:
            try:
                e = self.q.get(timeout=1)
            except queue.Empty:
                continue
            got.append(e)
            if until(*e):
                return got
        raise AssertionError(f"no matching event; saw {[g[0] for g in got][-20:]}")

    def close(self):
        try:
            self.c.close()
        except Exception:  # noqa: BLE001
            pass


def test_safe_names_and_kinds():
    from cutroom import imports as I
    assert I.safe_name("../../etc/passwd.mp4", I.KINDS["reference"]["exts"]) == "passwd.mp4"
    assert I.safe_name("..\\..\\x\\clip.MOV", I.KINDS["reference"]["exts"]) == "clip.mov"
    assert I.safe_name(".hidden.mp3", I.KINDS["song"]["exts"]) == "hidden.mp3"
    assert I.safe_name("a/b/<script>.png", I.KINDS["image"]["exts"]) == "_script_.png"
    for bad, kind in (("x.svg", "image"), ("evil.sh", "song"), ("noext", "reference")):
        with pytest.raises(I.UploadError) as e:
            I.safe_name(bad, I.KINDS[kind]["exts"])
        assert e.value.code == 415


def test_upload_image_pieces_and_resume(srv):
    S = srv
    data = os.urandom(300_000)
    q = "/api/upload?kind=image&name=../../evil%2Fgrade%20ref.png&id=abcdef0123456789&total=300000"
    code, r = call(S, "POST", q + "&offset=0&final=0", raw_body=data[:100_000])
    assert code == 200 and r["received"] == 100_000 and not r["done"]
    code, r = call(S, "GET", "/api/upload/abcdef0123456789?kind=image")
    assert r["received"] == 100_000
    code, r = call(S, "POST", q + "&offset=50000&final=0", raw_body=data[100_000:200_000])     # out of order
    assert code == 409 and r["received"] == 100_000
    code, r = call(S, "POST", q + "&offset=100000&final=0", raw_body=data[100_000:200_000])
    assert code == 200 and r["received"] == 200_000
    code, r = call(S, "POST", q + "&offset=200000&final=1", raw_body=data[200_000:])
    assert code == 200 and r["done"] and r["name"] == "grade ref.png" and r["size"] == 300_000
    out = Path(r["path"])
    assert out.parent == S["cfg"].p("studio", "images") and out.read_bytes() == data
    assert not list(out.parent.glob(".upload-*"))
    code, got = call(S, "GET", r["url"])
    assert code == 200 and got == data
    # the same name again gets a new file, never overwrites
    code, r2 = call(S, "POST", "/api/upload?kind=image&name=grade%20ref.png", raw_body=b"\x89PNG" + b"0" * 10)
    assert code == 200 and r2["name"] == "grade ref-2.png"


def test_upload_limits_and_bad_requests(srv, monkeypatch):
    from cutroom import imports as I
    S = srv
    code, r = call(S, "POST", "/api/upload?kind=nope&name=a.mp4", raw_body=b"x")
    assert code == 400
    code, r = call(S, "POST", "/api/upload?kind=image&name=a.svg", raw_body=b"<svg/>")
    assert code == 415
    monkeypatch.setitem(I.KINDS["image"], "max", 1000)
    code, r = call(S, "POST", "/api/upload?kind=image&name=big.png", raw_body=b"0" * 5000)
    assert code == 413
    code, r = chunked(S, "/api/upload?kind=image&name=big2.png", [b"0" * 600, b"0" * 600])
    assert code == 413
    code, r = call(S, "POST", "/api/upload?kind=image&name=big3.png&total=99999", raw_body=b"0")
    assert code == 413
    assert not any(p.name.startswith("big") for p in S["cfg"].p("studio", "images").iterdir())
    monkeypatch.setitem(I.KINDS["image"], "max", 64 << 20)
    S["cfg"].min_free_disk_gb = 1e9                   # "keep a petabyte free": the disk floor refuses the upload
    try:
        code, r = call(S, "POST", "/api/upload?kind=image&name=floor.png", raw_body=b"0" * 10)
        assert code == 507
    finally:
        S["cfg"].min_free_disk_gb = 0.0
    code, r = call(S, "POST", "/api/upload?kind=image&name=ok.png&id=../../x", raw_body=b"0")
    assert code == 400


def test_upload_song_chunked_runs_songs_index(srv):
    S = srv
    wav = S["base"] / "beep.wav"
    subprocess.run(FF + ["-f", "lavfi", "-i", "sine=frequency=880:duration=4", str(wav)], check=True)
    blob = wav.read_bytes()
    code, r = chunked(S, "/api/upload?kind=song&name=My%20Song.wav", [blob[:50_000], blob[50_000:]])
    assert code == 200 and r["done"] and Path(r["path"]).parent == S["cfg"].p("music") and r["job"]
    assert Path(r["path"]).read_bytes() == blob
    j = wait_job(S, r["job"])
    assert j["status"] == "done", j
    code, p = call(S, "GET", "/api/project")
    assert any(s["title"] == "My Song" for s in p["songs"])


def test_footage_is_linked_not_copied(srv):
    S = srv
    cfg = S["cfg"]
    outside = _video(S["base"] / "elsewhere" / "trip" / "beach.mp4", colour="0xa07030")
    code, info = call(S, "GET", "/api/import")
    assert code == 200 and "reference" in info["kinds"] and "ytdlp_install" in info
    code, loc = call(S, "POST", "/api/import/locate",
                     {"files": [{"name": "beach.mp4", "size": outside.stat().st_size}, {"name": "nothere.mp4", "size": 5}],
                      "folder": str(outside.parent)})
    assert code == 200 and loc["files"][0]["path"] == str(outside.resolve()) and loc["files"][1]["path"] is None
    before = set(cfg.p().rglob("*.mp4"))
    code, r = call(S, "POST", "/api/import/footage", {"paths": [str(outside)]})
    assert code == 200 and r["files"] == 1 and r["roots_added"] == 1, r
    j = wait_job(S, r["job"])
    assert j["status"] == "done", j.get("log_tail")
    from cutroom.config import Config
    assert str(outside.parent.resolve()) in Config.load(cfg.work).library_roots
    from cutroom import db
    con = db.connect(cfg.db_path)
    row = con.execute("SELECT status, proxy FROM clips WHERE id=?", (db.clip_id(outside),)).fetchone()
    assert row and row["status"] == "analyzed" and row["proxy"]
    # only the dropped file was indexed: the original library clip was not touched by this job
    assert con.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1
    # nothing was copied into the project (proxies are the only new video files)
    new = set(cfg.p().rglob("*.mp4")) - before
    assert all(p.parent == cfg.p("proxies") for p in new), new
    # already in the library now
    code, loc = call(S, "POST", "/api/import/locate", {"files": [{"name": "beach.mp4", "size": outside.stat().st_size}]})
    assert loc["files"][0]["in_library"] and loc["files"][0]["clip"]
    # refused: a file directly in a top-level folder; a missing path
    code, r = call(S, "POST", "/api/import/footage", {"paths": ["/definitely/not/here.mp4"]})
    assert code == 422 and r["skipped"]


def test_reference_from_upload_is_linked_and_measured(srv):
    S = srv
    vid = _video(S["base"] / "src" / "my ref.mp4", secs=4, colour="0x208040", tone=660)
    blob = vid.read_bytes()
    ev = Events(S)
    try:
        half = len(blob) // 2
        code, r = call(S, "POST", f"/api/upload?kind=reference&name=my%20ref.mp4&id=0123abcd&total={len(blob)}&final=0",
                       raw_body=blob[:half])
        assert code == 200
        code, r = call(S, "POST", f"/api/upload?kind=reference&name=my%20ref.mp4&id=0123abcd&total={len(blob)}"
                                  f"&offset={half}&final=1", raw_body=blob[half:])
        assert code == 200 and r["done"] and Path(r["path"]).parent == S["cfg"].p("refs", "_incoming")
        code, j = call(S, "POST", "/api/refs", {"src": r["path"], "title": "uploaded ref"})
        assert code == 200 and j["job"]
        got = ev.collect(lambda e, d: e == "ref" and d.get("job") == j["job"] and d.get("status") in ("done", "error", "analyzed")
                         and "title" in d)
        progress = [d for e, d in got if e == "ref" and d.get("status") == "analyzing"]
        final = got[-1][1]
        assert final["status"] != "error", final
        assert progress and progress[-1]["id"] == final["id"]
        rid = final["id"]
    finally:
        ev.close()
    from cutroom import reference as R
    src = R.source_path(S["cfg"], rid)
    assert src.is_symlink() and Path(os.path.realpath(src)) == Path(r["path"]).resolve()    # linked, no 2nd copy
    code, row = call(S, "GET", f"/api/ref/{rid}")
    assert row["status"] == "analyzed" and row["analysis"]["shots"] and row["cutmap"]
    code, g = call(S, "POST", "/api/grab", {"source": "ref", "ref": rid, "t": 1.0})
    assert code == 200 and g["url"].startswith("/media/grab/")


def test_reference_link_needs_ytdlp(srv, monkeypatch):
    import shutil
    from cutroom import studio
    real = shutil.which
    monkeypatch.setattr(studio.shutil, "which", lambda n, *a, **k: None if n == "yt-dlp" else real(n, *a, **k))
    code, r = call(srv, "POST", "/api/refs", {"src": "https://example.com/watch?v=1"})
    assert code == 424 and "yt-dlp" in r["install"]


def test_saved_state_and_version_restore(srv):
    S = srv
    code, p = call(S, "POST", "/api/plan/new", {"title": "Versions"})
    slug = p["slug"]
    assert p["saved"]["dirty"] and p["saved"]["latest"] is None
    code, c = call(S, "POST", f"/api/plan/{slug}/commit")
    assert code == 200
    code, p = call(S, "GET", f"/api/plan/{slug}")
    assert not p["saved"]["dirty"] and p["saved"]["version"] == c["version"]
    assert p["version_files"][-1]["by"] == "you"
    d = p["draft"]
    d["title"] = "Versions edited"
    code, p = call(S, "PUT", f"/api/plan/{slug}", {"plan": d})
    assert p["saved"]["dirty"]
    # a version written outside the studio counts as the AI's
    ext = dict(d, version=c["version"] + 1)
    S["cfg"].p("plans", f"{slug}_v{c['version'] + 1}.json").write_text(json.dumps(ext))
    t_end = time.time() + 10
    while time.time() < t_end:                     # the watcher notes it within a second or two
        code, p = call(S, "GET", f"/api/plan/{slug}")
        if p["version_files"][-1]["by"] == "ai":
            break
        time.sleep(0.3)
    assert p["version_files"][-1]["by"] == "ai"
    code, v = call(S, "GET", f"/api/plan/{slug}/version/{c['name']}")
    assert code == 200 and v["plan"]["title"] == "Versions"
    code, v = call(S, "GET", f"/api/plan/{slug}/version/{slug}.draft.json")
    assert code == 404
    code, v = call(S, "GET", f"/api/plan/{slug}/version/other_v1.json")
    assert code == 404
