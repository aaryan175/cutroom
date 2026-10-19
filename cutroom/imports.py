"""Studio Import: streaming uploads, finding dropped files on disk, adding footage folders to the library.

The browser never hands the studio a file's path, so the studio does one of three things with a drop:

  reference / song / image    the bytes are streamed (in pieces, straight to disk) into the project:
                              <work>/refs/_incoming/ (then `cutroom ref add --link`), <work>/music/ (then
                              `cutroom songs index`), <work>/studio/images/ (served at /media/image/<name>)
  footage                     NOT copied. `locate()` finds the original on this computer (Spotlight, the usual
                              folders, the library itself), `add_footage()` registers its folder as a library
                              root when it is not inside one already, and `cutroom index --files-from` indexes
                              just those files.
  footage-link                the explicit fallback when the originals can't be found (a browser on another
                              machine): a copy is uploaded to <work>/imports/footage/, which becomes a library root.

docs/STUDIO.md ("Import") has the HTTP side.
"""
import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path

from .config import AUDIO_EXTS, VIDEO_EXTS

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}          # no svg: it can carry script
GB = 1024 ** 3
KINDS = {
    "reference": {"dir": "refs/_incoming", "exts": VIDEO_EXTS, "max": 20 * GB},
    "song": {"dir": "music", "exts": AUDIO_EXTS, "max": 2 * GB},
    "image": {"dir": "studio/images", "exts": IMAGE_EXTS, "max": 64 * 1024 ** 2},
    "footage-link": {"dir": "imports/footage", "exts": VIDEO_EXTS, "max": 500 * GB},
}
UPLOAD_ID = re.compile(r"^[a-f0-9]{8,32}$")
CHUNK = 1 << 20
STALE_S = 24 * 3600
YTDLP_INSTALL = "brew install yt-dlp    (or: pip install yt-dlp)"


class UploadError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def safe_name(name, exts) -> str:
    """A browser-supplied file name -> a plain, traversal-safe file name with an allowed extension."""
    name = unicodedata.normalize("NFC", str(name or "")).replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable())
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in exts:
        raise UploadError(415, f"{ext or 'that file type'} can't be added here (use {', '.join(sorted(exts))})")
    stem = re.sub(r"[^\w .()+,-]", "_", stem).strip(" .")[:100] or "upload"
    return stem + ext


def dest_dir(cfg, kind) -> Path:
    if kind not in KINDS:
        raise UploadError(400, f"kind must be one of {', '.join(KINDS)}")
    d = Path(cfg.work) / KINDS[kind]["dir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def unique_path(d, name) -> Path:
    p = Path(d) / name
    stem, ext = os.path.splitext(name)
    k = 1
    while p.exists():
        k += 1
        p = Path(d) / f"{stem}-{k}{ext}"
    return p


def part_path(d, uid) -> Path:
    if not UPLOAD_ID.match(str(uid or "")):
        raise UploadError(400, "upload id must be 8-32 hex characters")
    return Path(d) / f".upload-{uid}.part"


def sweep_stale(d):
    now = time.time()
    for p in Path(d).glob(".upload-*.part"):
        try:
            if now - p.stat().st_mtime > STALE_S:
                p.unlink()
        except OSError:
            pass


def check_disk(cfg, d, incoming):
    free = shutil.disk_usage(d).free
    floor = float(getattr(cfg, "min_free_disk_gb", 0) or 0) * 1e9
    if free - incoming < floor:
        raise UploadError(507, f"not enough free disk space for this file ({free / 1e9:.1f} GB free, "
                               f"{floor / 1e9:.0f} GB must stay free)")


def read_body(rfile, headers, write, limit):
    """Stream a request body (Content-Length or Transfer-Encoding: chunked) into write(bytes), never holding
    more than 1 MiB. Returns the byte count. UploadError(413) as soon as it would pass `limit`."""
    te = (headers.get("Transfer-Encoding") or "").lower()
    n = 0
    if "chunked" in te:
        while True:
            line = rfile.readline(1024)
            if not line:
                raise UploadError(400, "upload cut off")
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                raise UploadError(400, "bad chunked body") from None
            if size == 0:
                while rfile.readline(1024) not in (b"\r\n", b"\n", b""):        # trailers
                    pass
                return n
            if n + size > limit:
                raise UploadError(413, "file is too large")
            left = size
            while left:
                buf = rfile.read(min(CHUNK, left))
                if not buf:
                    raise UploadError(400, "upload cut off")
                write(buf)
                n += len(buf)
                left -= len(buf)
            rfile.readline(4)                                                     # the chunk's CRLF
    cl = headers.get("Content-Length")
    if cl is None:
        raise UploadError(411, "send Content-Length or a chunked body")
    try:
        left = int(cl)
    except ValueError:
        raise UploadError(400, "bad Content-Length") from None
    if left < 0 or left > limit:
        raise UploadError(413, "file is too large")
    while left:
        buf = rfile.read(min(CHUNK, left))
        if not buf:
            raise UploadError(400, "upload cut off")
        write(buf)
        n += len(buf)
        left -= len(buf)
    return n


# ---------------------------------------------------------------------------------------------- footage
def _videos_under(folder, cap=20000):
    out = []
    for dp, dns, fns in os.walk(folder):
        dns[:] = [d for d in dns if not d.startswith(".")]
        for f in fns:
            if not f.startswith(".") and Path(f).suffix.lower() in VIDEO_EXTS:
                out.append(str(Path(dp, f)))
                if len(out) >= cap:
                    return out
    return out


def _inside(path, root) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _bad_root(cfg, root) -> str:
    r = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    if r in ("/", "/Volumes", "/Users", home) or Path(r).parent == Path(r):
        return "that file sits directly in a top-level folder; put it in its own folder first"
    work = os.path.realpath(cfg.work)
    if _inside(r, work) and not _inside(r, os.path.join(work, KINDS["footage-link"]["dir"])):
        return "that file is inside the project folder"
    return ""


def plan_footage(cfg, paths, folder=None):
    """paths (files or folders) -> {files: [...], roots_new: [...], skipped: [{path, why}]}. Nothing is written."""
    items = list(paths or [])
    if folder:
        items.append(folder)
    files, new_roots, skipped = [], [], []
    roots = [os.path.realpath(r) for r in cfg.library_roots]
    for raw in items:
        p = Path(os.path.expanduser(str(raw)))
        if not p.exists():
            skipped.append({"path": str(raw), "why": "not found on this computer"})
            continue
        p = Path(os.path.realpath(p))
        if p.is_dir():
            vids, root = _videos_under(p), str(p)
        elif p.suffix.lower() in VIDEO_EXTS:
            vids, root = [str(p)], str(p.parent)
        else:
            skipped.append({"path": str(p), "why": "not a video file"})
            continue
        if not vids:
            skipped.append({"path": str(p), "why": "no video files in that folder"})
            continue
        if not any(_inside(vids[0], r) for r in roots + new_roots):
            why = _bad_root(cfg, root)
            if why:
                skipped.append({"path": str(p), "why": why})
                continue
            new_roots = [r for r in new_roots if not _inside(r, root)] + [root]
        files += vids
    seen = set()
    files = [f for f in files if not (f in seen or seen.add(f))]
    return {"files": files, "roots_new": new_roots, "skipped": skipped}


def add_roots(cfg, roots):
    """Append library roots to cutroom.json (re-read first, so settings written by others survive)."""
    if not roots:
        return
    from .config import Config
    fresh = Config.load(cfg.work)
    for r in roots:
        if not any(_inside(r, x) for x in fresh.library_roots):
            fresh.library_roots.append(r)
    fresh.save()
    cfg.library_roots = list(fresh.library_roots)


def locate(cfg, con, files, folder=None, budget_s=4.0):
    """Find dropped files on this computer by name + size. files: [{name, size}] ->
    [{name, size, path|None, clip|None, in_library}]. Read-only; bounded by `budget_s`."""
    t_end = time.time() + budget_s
    want = []
    for f in files or []:
        name = os.path.basename(str((f or {}).get("name") or "")).strip()
        try:
            size = int((f or {}).get("size") or -1)
        except (TypeError, ValueError):
            size = -1
        if name:
            want.append({"name": name, "size": size, "path": None, "clip": None, "in_library": False})

    def match(p, w):
        try:
            return os.path.basename(p) == w["name"] and (w["size"] < 0 or os.path.getsize(p) == w["size"])
        except OSError:
            return False

    for w in want:                                         # already in the library?
        like = "%" + w["name"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for r in con.execute("SELECT id, path FROM clips WHERE path LIKE ? ESCAPE '\\'", (like,)):
            if match(r["path"], w):
                w.update(path=r["path"], clip=r["id"], in_library=True)
                break
    if folder:
        for w in want:
            if not w["path"]:
                p = os.path.join(os.path.expanduser(str(folder)), w["name"])
                if match(p, w):
                    w["path"] = os.path.realpath(p)
    mdfind = shutil.which("mdfind")
    for w in want:
        if w["path"] or not mdfind or time.time() > t_end:
            continue
        try:
            out = subprocess.run([mdfind, "-name", w["name"]], capture_output=True, text=True,
                                 timeout=max(0.5, t_end - time.time())).stdout
        except (subprocess.TimeoutExpired, OSError):
            continue
        for p in out.splitlines():
            if match(p, w) and "/." not in p:
                w["path"] = os.path.realpath(p)
                break
    home = Path.home()
    places = [home / "Downloads", home / "Desktop", home / "Movies", home / "Documents", home / "Pictures"]
    places += [Path(r) for r in cfg.library_roots]
    try:
        places += [p for p in Path("/Volumes").iterdir() if p.is_dir()]
    except OSError:
        pass
    left = [w for w in want if not w["path"]]
    for base in places:
        if not left or time.time() > t_end:
            break
        for dp, dns, fns in os.walk(base):
            if time.time() > t_end:
                break
            depth = len(Path(dp).relative_to(base).parts) if Path(dp) != base else 0
            dns[:] = [] if depth >= 3 else [d for d in dns if not d.startswith(".")]
            names = set(fns)
            for w in left:
                if not w["path"] and w["name"] in names and match(os.path.join(dp, w["name"]), w):
                    w["path"] = os.path.realpath(os.path.join(dp, w["name"]))
            left = [w for w in left if not w["path"]]
            if not left:
                break
    return want
