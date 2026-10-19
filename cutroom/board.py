"""Review board: a tiny stdlib web server for <work>/renders.

    cutroom board [--port 8600] [--host 0.0.0.0]      then open http://<host>:<port>/

  /                      renders newest first: inline player, QA summary, timestamped notes box
  /compare?a=X&b=Y       two renders (or a render and a reference) side by side, synced play/seek
  /r/<path>              files under renders/ with HTTP Range (206) so phones can scrub video
  POST /note             {"render", "t", "text"[, "version", "author"]} -> appended to notes/notes.jsonl
  /api/renders           JSON list of renders     /api/notes?render=X    JSON notes

Reference videos (a creator's example edit, a previous approved cut) go in renders/reference/.
No external assets; works on a phone over any tunnel/VPN the user trusts.
"""
import json
import mimetypes
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
CHUNK = 256 * 1024
MAX_BODY = 64 * 1024
_lock = threading.Lock()

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("text/vtt", ".vtt")


# ==================================================================================================
# data
# ==================================================================================================

def _renders_dir(cfg) -> Path:
    return Path(cfg.work) / "renders"


def _notes_path(cfg) -> Path:
    return Path(cfg.work) / "notes" / "notes.jsonl"


def version_of(path) -> int:
    """Render version: from the sidecar JSON ("version") or a `_v<N>` in the file name."""
    p = Path(path)
    side = p.with_suffix(".json")
    if side.exists():
        try:
            v = json.loads(side.read_text()).get("version")
            if v is not None:
                return int(v)
        except (ValueError, OSError, AttributeError, TypeError):
            pass
    m = re.search(r"_v(\d+)", p.stem)
    return int(m.group(1)) if m else None


def list_renders(cfg, reference=False) -> list:
    root = _renders_dir(cfg)
    base = root / "reference" if reference else root
    if not base.is_dir():
        return []
    out = []
    for p in base.iterdir():
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("."):
            st = p.stat()
            item = {"name": p.name, "rel": p.relative_to(root).as_posix(), "size": st.st_size, "mtime": st.st_mtime,
                    "version": version_of(p), "reference": reference, "qa": None, "qa_html": None}
            qj = p.with_name(p.stem + ".qa.json")
            if qj.exists():
                try:
                    s = json.loads(qj.read_text()).get("summary", {})
                    item["qa"] = {k: s.get(k) for k in ("status", "fail", "warn", "top_issues", "loudness_lufs",
                                                         "true_peak_dbtp", "shots")}
                except (ValueError, OSError):
                    pass
            qh = p.with_name(p.stem + ".qa.html")
            if qh.exists():
                item["qa_html"] = qh.relative_to(root).as_posix()
            out.append(item)
    out.sort(key=lambda x: -x["mtime"])
    return out


def read_notes(cfg, render=None) -> list:
    p = _notes_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="ignore").splitlines():
        try:
            n = json.loads(line)
        except ValueError:
            continue
        if render is None or n.get("render") == render:
            out.append(n)
    return out


def add_note(cfg, render, t, text, version=None, author=None) -> dict:
    text = str(text or "").strip()
    if not text:
        raise ValueError("empty note")
    render = str(render or "")
    if version is None and render:
        version = version_of(_renders_dir(cfg) / render)
    note = {"id": uuid.uuid4().hex[:10], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "render": render,
            "version": version, "t": round(float(t or 0), 2), "text": text[:4000]}
    if author:
        note["author"] = str(author)[:80]
    p = _notes_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(p, "a") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")
    return note


def _fmt(t) -> str:
    t = max(0.0, float(t or 0))
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def print_notes(cfg, since=None, file=None):
    """Notes grouped by render/version, newest version first, each as `[mm:ss.s] note`."""
    file = file or sys.stdout
    notes = read_notes(cfg)
    if since is not None:
        notes = [n for n in notes if n.get("version") is not None and int(n["version"]) >= int(since)]
    if not notes:
        print("no notes" + (f" since v{since}" if since is not None else ""), file=file)
        return
    groups = {}
    for n in notes:
        groups.setdefault((n.get("render", ""), n.get("version")), []).append(n)

    def key(k):
        render, ver = k
        last = max(x.get("ts", "") for x in groups[k])
        return (-(ver if ver is not None else -1), _neg(last), render)

    for k in sorted(groups, key=key):
        render, ver = k
        items = sorted(groups[k], key=lambda x: (x.get("t", 0), x.get("ts", "")))
        print(f"== {render}" + (f"  (v{ver})" if ver is not None else "") + f"  · {len(items)} note(s)", file=file)
        for n in items:
            who = f" ({n['author']})" if n.get("author") else ""
            print(f"[{_fmt(n.get('t'))}] {n.get('text', '')}{who}", file=file)
        print(file=file)


def _neg(s):
    return tuple(-ord(c) for c in s)


# ==================================================================================================
# http
# ==================================================================================================

def _parse_range(h, size):
    """(start, end) inclusive, or None for no/unsupported range, or 'bad' if unsatisfiable."""
    if not h:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(,.*)?", h)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.group(1), m.group(2)
    if a == "":
        n = int(b)
        if n == 0:
            return "bad"
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size or end < start:
        return "bad"
    return start, min(end, size - 1)


class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "cutroom-board/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: one line per request only for errors
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("board: " + (fmt % args) + "\n")

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _resolve(self, rel):
        root = _renders_dir(self.cfg).resolve()
        rel = unquote(rel).strip("/")
        p = (root / rel).resolve()
        if p != root and root not in p.parents:
            return None
        return p

    def _file(self, path: Path):
        if not path or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = _parse_range(self.headers.get("Range"), size)
        if rng == "bad":
            return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        if rng is None:
            start, end, code = 0, size - 1, 200
        else:
            (start, end), code = rng, 206
        length = end - start + 1 if size else 0
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache" if ctype.startswith(("text/", "application/json")) else "max-age=60")
        self.end_headers()
        if self.command == "HEAD" or length <= 0:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass          # the player cancelled the range (normal while scrubbing)

    # ---------------------------------------------------------------- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._send(200, _index_html(self.cfg).encode())
            if path.rstrip("/") == "/compare":
                return self._send(200, _compare_html(self.cfg, q.get("a"), q.get("b")).encode())
            if path.rstrip("/") == "/api/renders":
                return self._json({"renders": list_renders(self.cfg), "reference": list_renders(self.cfg, True)})
            if path.rstrip("/") == "/api/notes":
                return self._json({"notes": read_notes(self.cfg, q.get("render"))})
            if path.rstrip("/") == "/healthz":
                return self._send(200, b"ok", "text/plain")
            for pre in ("/r/", "/renders/"):
                if path.startswith(pre):
                    return self._file(self._resolve(path[len(pre):]))
            # bare file names (e.g. links in a QA page) resolve against renders/
            p = self._resolve(path)
            if p and p.is_file():
                return self._file(p)
            return self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") not in ("/note", "/api/note", "/notes"):
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._json({"error": "bad body"}, 400)
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            note = add_note(self.cfg, data.get("render"), data.get("t", 0), data.get("text"),
                            version=data.get("version"), author=data.get("author"))
        except (ValueError, TypeError, AttributeError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"ok": True, "note": note})


def make_server(cfg, port=8600, host="127.0.0.1") -> ThreadingHTTPServer:
    """Build (not start) the server; port 0 = any free port (see server.server_address)."""
    handler = type("BoardHandler", (Handler,), {"cfg": cfg})
    _renders_dir(cfg).mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(cfg, port=8600, host="127.0.0.1"):
    srv = make_server(cfg, port, host)
    h, p = srv.server_address[:2]
    shown = "localhost" if h in ("127.0.0.1", "0.0.0.0") else h
    print(f"board: http://{shown}:{p}/   (renders: {_renders_dir(cfg)})  ctrl-c to stop", flush=True)
    if host == "127.0.0.1":
        print("  only this machine can open it; use --host 0.0.0.0 plus a VPN/tunnel to share", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ==================================================================================================
# pages
# ==================================================================================================

CSS = """
:root{--bg:#0f1012;--card:#18191d;--line:#2a2c32;--fg:#e8e8ea;--mut:#9399a6;--acc:#7fb0ff;--ok:#3fb67a;--warn:#e0a23a;--fail:#e05555}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:650}header a{color:var(--acc);text-decoration:none;font-size:14px}
main{max-width:1100px;margin:0 auto;padding:12px 16px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:14px 0}
.card h2{font-size:16px;margin:0 0 6px;word-break:break-all}.meta{color:var(--mut);font-size:13px}
video{width:100%;max-height:70vh;background:#000;border-radius:8px;display:block}
.chip{display:inline-block;padding:1px 8px;border-radius:9px;color:#111;font-size:12px;font-weight:700;text-transform:uppercase}
.pass{background:var(--ok)}.warn{background:var(--warn)}.fail{background:var(--fail)}.none{background:#555;color:#ddd}
.qa ul{margin:6px 0;padding-left:18px;color:var(--mut);font-size:13px}
.notebox{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
textarea{flex:1 1 260px;min-height:44px;background:#101114;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit}
button,select{background:#2a3b5c;color:#fff;border:0;border-radius:8px;padding:10px 14px;font:inherit;cursor:pointer}
select{background:#202227;border:1px solid var(--line)}
button.sec{background:#26282e}
.notes{list-style:none;padding:0;margin:8px 0 0}.notes li{padding:6px 0;border-top:1px solid var(--line);font-size:14px}
.notes a{color:var(--acc);cursor:pointer;font-variant-numeric:tabular-nums;margin-right:8px;text-decoration:none}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1 1 420px;min-width:0}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#2a3b5c;padding:8px 14px;border-radius:8px;display:none}
"""

JS_COMMON = r"""
const fmt=t=>{t=Math.max(0,t||0);const m=Math.floor(t/60),s=(t%60).toFixed(1);return String(m).padStart(2,'0')+':'+s.padStart(4,'0')};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',1800)}
async function loadNotes(card){const r=card.dataset.render;const res=await fetch('/api/notes?render='+encodeURIComponent(r));
  const js=await res.json();const ul=card.querySelector('.notes');const v=card.querySelector('video');
  ul.innerHTML=js.notes.sort((a,b)=>a.t-b.t).map(n=>`<li><a data-t="${n.t}">[${fmt(n.t)}]</a>${esc(n.text)}</li>`).join('');
  ul.querySelectorAll('a').forEach(a=>a.onclick=()=>{v.currentTime=parseFloat(a.dataset.t);v.pause();v.scrollIntoView({block:'center'})});}
function wire(card){const v=card.querySelector('video'),ta=card.querySelector('textarea'),b=card.querySelector('.add');
  if(!v||!ta)return;let held=null;
  const upd=()=>{b.textContent='Add note @ '+fmt(held!==null?held:v.currentTime)};
  v.addEventListener('timeupdate',()=>{if(held===null)upd()});
  v.addEventListener('pause',()=>{held=v.currentTime;upd()});v.addEventListener('play',()=>{held=null;upd()});
  ta.addEventListener('focus',()=>{if(!v.paused){v.pause()}held=v.currentTime;upd()});
  const send=async()=>{const text=ta.value.trim();if(!text)return;const t=held!==null?held:v.currentTime;
    const res=await fetch('/note',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({render:card.dataset.render,version:card.dataset.version?parseInt(card.dataset.version):null,t:t,text:text})});
    if(res.ok){ta.value='';toast('saved at '+fmt(t));loadNotes(card)}else{toast('save failed')}};
  b.onclick=send;ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();send()}});
  upd();loadNotes(card);}
"""


def _esc(s):
    import html
    return html.escape(str(s), quote=True)


def _qa_block(r):
    q = r.get("qa")
    if not q:
        return "<div class='qa meta'><span class='chip none'>no QA</span> run <code>cutroom qa</code> on it</div>"
    top = "".join(f"<li>{_esc(x)}</li>" for x in (q.get("top_issues") or [])[:6])
    link = f" · <a href='/r/{quote(r['qa_html'])}' style='color:var(--acc)'>full QA + contact sheets</a>" if r.get("qa_html") else ""
    return (f"<div class='qa'><span class='chip {_esc(q.get('status') or 'none')}'>{_esc(q.get('status'))}</span> "
            f"<span class='meta'>{q.get('fail', 0)} fail · {q.get('warn', 0)} warn · {q.get('shots')} shots · "
            f"{q.get('loudness_lufs')} LUFS · TP {q.get('true_peak_dbtp')}{link}</span><ul>{top}</ul></div>")


def _card(r, notes=True):
    ver = r.get("version")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))
    src = "/r/" + quote(r["rel"])
    nb = ("<div class='notebox'><textarea placeholder='Pause, type what to change, save. The note keeps the exact time.'>"
          "</textarea><button class='add'>Add note</button></div><ul class='notes'></ul>") if notes else ""
    return (f"<div class='card' data-render='{_esc(r['rel'])}' data-version='{'' if ver is None else ver}'>"
            f"<h2>{_esc(r['name'])}</h2><div class='meta'>{'v' + str(ver) + ' · ' if ver is not None else ''}{when} · "
            f"{r['size'] / 1e6:.1f} MB · <a style='color:var(--acc)' href='{src}'>file</a></div>"
            f"<video controls playsinline preload='metadata' src='{src}'></video>"
            f"{_qa_block(r) if not r.get('reference') else ''}{nb}</div>")


def _index_html(cfg) -> str:
    renders = list_renders(cfg)
    refs = list_renders(cfg, reference=True)
    opts = "".join(f"<option value='{_esc(r['rel'])}'>{_esc(r['rel'])}</option>" for r in renders + refs)
    cmp_form = ("" if len(renders) + len(refs) < 2 else
                f"<form action='/compare' class='card row' style='align-items:center'>"
                f"<div><b>Compare</b> <select name='a'>{opts}</select> vs <select name='b'>"
                + "".join(f"<option value='{_esc(r['rel'])}'{' selected' if i == 1 or (r.get('reference') and i == len(renders)) else ''}>"
                          f"{_esc(r['rel'])}</option>" for i, r in enumerate(renders + refs))
                + "</select> <button>Side by side</button></div></form>")
    body = "".join(_card(r) for r in renders) or "<p class='meta'>No renders yet. <code>cutroom render plans/x_v1.json</code></p>"
    ref_html = ("<h2 style='font-size:15px;color:var(--mut)'>Reference</h2>" + "".join(_card(r, notes=False) for r in refs)) if refs else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>cutroom board</title><style>{CSS}</style></head><body>
<header><h1>Review board</h1><span class='meta'>{len(renders)} render(s), newest first</span></header>
<main>{cmp_form}{body}{ref_html}</main><div id="toast" class="toast"></div>
<script>{JS_COMMON}
document.querySelectorAll('.card[data-render]').forEach(wire);
// only one video plays at a time
document.querySelectorAll('video').forEach(v=>v.addEventListener('play',()=>document.querySelectorAll('video').forEach(o=>{{if(o!==v)o.pause()}})));
</script></body></html>"""


def _compare_html(cfg, a, b) -> str:
    items = {r["rel"]: r for r in list_renders(cfg) + list_renders(cfg, reference=True)}
    names = list(items)
    a = a if a in items else (names[0] if names else None)
    b = b if b in items else (names[1] if len(names) > 1 else a)
    if not a:
        return "<!doctype html><p>No renders.</p>"

    def sel(name, cur):
        return f"<select name='{name}'>" + "".join(
            f"<option value='{_esc(n)}'{' selected' if n == cur else ''}>{_esc(n)}</option>" for n in names) + "</select>"

    def side(rel, tag):
        r = items[rel]
        ver = r.get("version")
        return (f"<div class='card' data-render='{_esc(rel)}' data-version='{'' if ver is None else ver}'>"
                f"<h2>{tag} · {_esc(r['name'])}</h2>"
                f"<video id='{tag}' playsinline preload='auto' src='/r/{quote(rel)}'></video>"
                + (_qa_block(r) if not r.get("reference") else "")
                + "<div class='notebox'><textarea placeholder='Note on this side at the current time'></textarea>"
                  "<button class='add'>Add note</button></div><ul class='notes'></ul></div>")

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>compare</title><style>{CSS}
.bar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}} input[type=range]{{flex:1 1 300px}}
.t{{font-variant-numeric:tabular-nums;color:var(--mut)}}</style></head><body>
<header><h1>Side by side</h1><a href="/">← all renders</a>
<form action='/compare' style='display:flex;gap:6px;flex-wrap:wrap'>{sel('a', a)}{sel('b', b)}<button class='sec'>Load</button></form></header>
<main>
<div class='bar'><button id='pp'>Play</button><button class='sec' id='back'>-1 f</button><button class='sec' id='fwd'>+1 f</button>
<input type='range' id='seek' min='0' max='1000' value='0'><span class='t' id='tc'>00:00.0</span>
<button class='sec' id='aud'>Audio: A</button><label class='meta'><input type='checkbox' id='lock' checked> lock</label></div>
<div class='row'><div>{side(a, 'A')}</div><div>{side(b, 'B')}</div></div></main><div id="toast" class="toast"></div>
<script>{JS_COMMON}
const A=document.getElementById('A'),B=document.getElementById('B'),seek=document.getElementById('seek'),tc=document.getElementById('tc');
const lock=document.getElementById('lock');let aud='A';B.muted=true;
const dur=()=>Math.max(A.duration||0,B.duration||0)||1;
document.getElementById('pp').onclick=()=>{{if(A.paused){{A.play();if(lock.checked)B.play()}}else{{A.pause();B.pause()}}}};
A.addEventListener('play',()=>{{document.getElementById('pp').textContent='Pause'}});
A.addEventListener('pause',()=>{{document.getElementById('pp').textContent='Play';if(lock.checked){{B.pause();B.currentTime=A.currentTime}}}});
const step=d=>{{A.pause();B.pause();A.currentTime=Math.max(0,A.currentTime+d);if(lock.checked)B.currentTime=A.currentTime}};
document.getElementById('back').onclick=()=>step(-1/25);document.getElementById('fwd').onclick=()=>step(1/25);
seek.oninput=()=>{{const t=seek.value/1000*dur();A.currentTime=t;if(lock.checked)B.currentTime=t}};
document.getElementById('aud').onclick=e=>{{aud=aud==='A'?'B':'A';A.muted=aud!=='A';B.muted=aud!=='B';e.target.textContent='Audio: '+aud}};
A.addEventListener('timeupdate',()=>{{seek.value=Math.round(A.currentTime/dur()*1000);tc.textContent=fmt(A.currentTime)}});
A.addEventListener('seeked',()=>{{if(lock.checked&&Math.abs(B.currentTime-A.currentTime)>0.04)B.currentTime=A.currentTime}});
setInterval(()=>{{if(lock.checked&&!A.paused&&Math.abs(B.currentTime-A.currentTime)>0.12)B.currentTime=A.currentTime}},500);
document.querySelectorAll('.card[data-render]').forEach(wire);
</script></body></html>"""
