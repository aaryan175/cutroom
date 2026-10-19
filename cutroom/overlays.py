"""Overlay graphics: callout, lower_third, title, label, and the full-screen title card.

Each overlay becomes a PNG sequence `NNNN.png` (RGBA) plus, for frosted-glass cards, a mask sequence
`mNNNN.png` (white where the glass sits). The segment renderer blurs the footage through the mask and
lays the graphic on top, so cards are real frosted glass over the shot.

Two renderers draw the same small, consistent set of designs:
  html  headless Chrome/Chromium driven over the DevTools protocol (no Python deps). Each page has a
        deterministic JS `render(t, dur)`; frames are screenshots with a transparent background.
  pil   Pillow fallback: same layouts drawn statically, with an eased fade + slide.

Only the entrance (~0.7 s) and exit (~0.45 s) are captured; the hold frames are hard links to the last
entrance frame, so a 6 s card costs ~30 screenshots. Sequences are cached by a hash of the spec, size,
fps, font and this file's code.

Design rules (from review rounds): few elements, short text, one consistent style, no busy
"template" motion. A soft spring in, a quick fade out, nothing else moves.
"""
import base64
import hashlib
import html
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import captions

KINDS = ("callout", "lower_third", "title", "label", "title_card")
GLASS_KINDS = ("callout", "lower_third", "label")
INTRO_S, OUTRO_S = 0.7, 0.45
MARGIN_X, MARGIN_Y = 96, 84                    # design px at 1080p
CODE_VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10]


# ============================================================================== chrome discovery
def find_chrome():
    env = os.environ.get("CUTROOM_CHROME")
    if env:
        return env if os.path.exists(env) else shutil.which(env)
    mac = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Chromium.app/Contents/MacOS/Chromium",
           os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    if sys.platform == "darwin":
        for p in mac:
            if os.path.exists(p):
                return p
    for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(n)
        if p:
            return p
    win = [os.path.join(os.environ.get(k, ""), "Google/Chrome/Application/chrome.exe")
           for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return next((p for p in win if os.path.exists(p)), None)


# ============================================================================== tiny CDP client
class _WS:
    """Minimal RFC 6455 client (text frames, client masking, fragmentation, ping/pong)."""

    def __init__(self, url, timeout=60):
        assert url.startswith("ws://")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.s = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            buf += chunk
        head, _, self.rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head[:200])

    def _read(self, n):
        out = self.rest[:n]
        self.rest = self.rest[n:]
        while len(out) < n:
            chunk = self.s.recv(max(65536, n - len(out)))
            if not chunk:
                raise ConnectionError("websocket closed")
            out += chunk
        if len(out) > n:
            self.rest = out[n:] + self.rest
            out = out[:n]
        return out

    def send(self, text, opcode=1):
        data = text.encode() if isinstance(text, str) else text
        hdr = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) if n < 4096 else _xor(data, mask)
        self.s.sendall(hdr + mask + body)

    def recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b1 & 0x80:
                m = self._read(4)
                data = _xor(self._read(n), m)
            else:
                data = self._read(n)
            if op == 9:
                self.send(data, opcode=10)
                continue
            if op == 8:
                raise ConnectionError("websocket closed by peer")
            msg += data
            if b0 & 0x80:
                return msg.decode()

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def _xor(data, mask):
    import numpy as np
    a = np.frombuffer(data, dtype=np.uint8)
    m = np.resize(np.frombuffer(mask, dtype=np.uint8), len(a))
    return (a ^ m).tobytes()


class Chrome:
    """One headless Chrome page. Use as a context manager."""

    def __init__(self, binary, width, height, scale=1.0):
        self.binary, self.w, self.h, self.scale = binary, width, height, scale
        self.proc = self.ws = None
        self._id = 0

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="cutroom-chrome-")
        args = [self.binary, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.tmp}",
                "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars",
                "--mute-audio", "--disable-extensions", "--force-color-profile=srgb",
                "--allow-file-access-from-files", "--disable-background-networking",
                f"--window-size={self.w},{self.h}", "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.tmp, "DevToolsActivePort")
        t0 = time.time()
        while not (port_file.exists() and port_file.read_text().strip()):
            if self.proc.poll() is not None or time.time() - t0 > 30:
                self.__exit__()
                raise RuntimeError("chrome did not start")
            time.sleep(0.05)
        port = int(port_file.read_text().split()[0])
        page = None
        while page is None and time.time() - t0 < 30:
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
                page = next((t for t in targets if t.get("type") == "page"), None)
            except OSError:
                pass
            if page is None:
                time.sleep(0.1)
        if page is None:
            self.__exit__()
            raise RuntimeError("chrome page target not found")
        self.ws = _WS(page["webSocketDebuggerUrl"])
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=self.w, height=self.h,
                  deviceScaleFactor=self.scale, mobile=False)
        self.call("Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0})
        return self

    def __exit__(self, *exc):
        if self.ws:
            self.ws.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error: {r['exceptionDetails'].get('text')} in {expr[:80]}")
        return r.get("result", {}).get("value")

    def load(self, path):
        self.call("Page.navigate", url=Path(path).resolve().as_uri())
        t0 = time.time()
        while time.time() - t0 < 20:
            try:
                if self.eval("document.readyState==='complete' && window.READY===true"):
                    self.eval("document.fonts.ready.then(()=>true)")
                    return
            except RuntimeError:
                pass
            time.sleep(0.05)
        raise RuntimeError("page did not become ready")

    def shot(self, out):
        r = self.call("Page.captureScreenshot", format="png", fromSurface=True, captureBeyondViewport=False)
        Path(out).write_bytes(base64.b64decode(r["data"]))


# ============================================================================== designs (HTML)
BASE_CSS = r"""
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1920px;height:1080px;background:transparent;overflow:hidden;color:#fff;
  font-family:"CutroomFont","Inter","Helvetica Neue",Helvetica,Arial,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:geometricPrecision}
.abs{position:absolute}
.glass{border-radius:26px;background:linear-gradient(160deg,rgba(255,255,255,.20),rgba(255,255,255,.08) 60%,rgba(255,255,255,.06));
  box-shadow:0 30px 70px -24px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.38),inset 0 0 0 1px rgba(255,255,255,.14)}
.ts{text-shadow:0 2px 18px rgba(0,0,0,.28)}
body.mask *{color:transparent!important;text-shadow:none!important;box-shadow:none!important;background:transparent!important;border-color:transparent!important}
body.mask .glass{background:#fff!important}
"""

BASE_JS = r"""
const clamp=(x,a=0,b=1)=>Math.max(a,Math.min(b,x));
const lerp=(a,b,t)=>a+(b-a)*t;
const eo=t=>1-Math.pow(1-clamp(t),3);
const spring=t=>{t=Math.max(0,t);return 1-Math.exp(-7*t)*Math.cos(10*t)};
const prog=(t,a,d)=>clamp((t-a)/d);
// entrance: soft spring up + de-blur; hold perfectly still; exit: quick fade + small drop
function card(el,t,dur,dy=18,delay=0){
  const ti=t-delay, i=prog(ti,0,.5), o=prog(t,dur-%(OUT).2f,%(OUT).2f);
  const s=(ti>=.62?1:lerp(.94,1,spring(ti/.6)))-o*.02;
  el.style.opacity=Math.min(clamp(i*1.8),1-o);
  el.style.transform=`translateY(${(1-eo(i))*dy+o*8}px) scale(${s})`;
  el.style.filter=(i<1||o>0)?`blur(${(1-eo(i))*8+o*3}px)`:'none';
}
""" % {"OUT": OUTRO_S}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>{fontface}{css}{extra}</style></head>
<body>{body}<script>{js}
window.READY=true;
function render(t,dur){{ {render} }}
render(0,1);</script></body></html>"""


def _pos_css(position, kind):
    p = position if position in ("top-left", "top-right", "bottom-left", "bottom-right", "center") else "top-left"
    v, _, h = p.partition("-")
    if p == "center":
        return "left:50%;top:50%;translate:-50% -50%;"
    bottom = 210 if kind == "lower_third" else MARGIN_Y + 90
    css = f"top:{MARGIN_Y}px;" if v == "top" else f"bottom:{bottom}px;"
    css += f"left:{MARGIN_X}px;" if h == "left" else f"right:{MARGIN_X}px;"
    return css


def _design(spec):
    """(extra_css, body_html, render_js) for one overlay spec."""
    kind, pos = spec["kind"], spec.get("position") or "top-left"
    t = html.escape(spec.get("text", ""))
    sub = html.escape(spec.get("sub", "") or "")
    pc = _pos_css(pos, kind)
    if kind == "callout":
        css = f"#c{{{pc}padding:20px 34px 20px 28px;display:flex;align-items:center;gap:18px;font-size:40px;font-weight:600;letter-spacing:-.4px;white-space:nowrap}}" \
              "#dot{width:14px;height:14px;border-radius:50%;background:#ffcf6e;box-shadow:0 0 16px rgba(255,207,110,.8)}"
        body = f'<div class="abs glass" id="c"><div id="dot"></div><span class="ts">{t}</span></div>'
        return css, body, "card(c,t,dur,16);"
    if kind == "lower_third":
        css = (f"#c{{{pc}padding:24px 38px 26px 34px;display:flex;gap:22px;align-items:stretch;max-width:1100px}}"
               "#bar{width:5px;border-radius:3px;background:#ffcf6e}"
               "#t{font-size:46px;font-weight:650;letter-spacing:-.5px;line-height:1.1}"
               "#s{font-size:28px;font-weight:450;opacity:.82;margin-top:8px;letter-spacing:.2px}")
        body = (f'<div class="abs glass" id="c"><div id="bar"></div><div><div id="t" class="ts">{t}</div>'
                + (f'<div id="s" class="ts">{sub}</div>' if sub else "") + "</div></div>")
        js = "card(c,t,dur,22);"
        if sub:
            js += "const k=prog(t,.18,.45);s.style.opacity=.82*eo(k)*(1-prog(t,dur-%.2f,%.2f));s.style.transform=`translateY(${(1-eo(k))*8}px)`;" % (OUTRO_S, OUTRO_S)
        return css, body, js
    if kind == "label":
        css = f"#c{{{pc}padding:13px 24px;font-size:25px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;border-radius:16px;white-space:nowrap}}"
        body = f'<div class="abs glass" id="c"><span class="ts">{t}</span></div>'
        return css, body, "card(c,t,dur,10);"
    if kind in ("title", "title_card"):
        big = 118 if kind == "title_card" else 92
        css = (f"#c{{{pc}text-align:center;width:1600px}}"
               f"#t{{font-size:{big}px;font-weight:700;letter-spacing:-2px;line-height:1.04}}"
               "#s{font-size:30px;font-weight:500;letter-spacing:.32em;text-transform:uppercase;opacity:.85;margin-top:22px}")
        body = (f'<div class="abs" id="c"><div id="t" class="ts">{t}</div>'
                + (f'<div id="s" class="ts">{sub}</div>' if sub else "") + "</div>")
        js = "card(c,t,dur,24);"
        if sub:
            js += "const k=prog(t,.25,.5);s.style.opacity=.85*eo(k)*(1-prog(t,dur-%.2f,%.2f));s.style.letterSpacing=lerp(.5,.32,eo(k))+'em';" % (OUTRO_S, OUTRO_S)
        return css, body, js
    raise ValueError(f"unknown overlay kind {kind!r}")


def _fontface(font_pi):
    path = font_pi[0]
    if not path or path.lower().endswith(".ttc"):
        return ""            # collections can't be loaded by @font-face; the family name falls through
    return f"@font-face{{font-family:'CutroomFont';src:url('{Path(path).resolve().as_uri()}');font-weight:100 900}}"


# ============================================================================== frame plan
def _frame_plan(n, fps):
    """Indices to actually draw (entrance + exit) and a map hold-frame -> source frame."""
    ni = min(n, max(1, int(round(INTRO_S * fps))))
    no = min(n - ni, int(round(OUTRO_S * fps)))
    draw = list(range(ni)) + list(range(n - no, n))
    hold = {f: ni - 1 for f in range(ni, n - no)}
    return draw, hold


def _link(src, dst):
    try:
        if os.path.exists(dst):
            os.unlink(dst)
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def spec_key(spec, W, H, fps, dur, font_pi, renderer):
    blob = json.dumps({"s": {k: spec.get(k) for k in ("kind", "text", "sub", "position")}, "W": W, "H": H,
                       "fps": fps, "dur": round(dur, 3), "font": captions.font_hash(font_pi), "r": renderer,
                       "code": CODE_VERSION}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _done(d, n, glass):
    return os.path.exists(d / f"{n - 1:04d}.png") and (not glass or os.path.exists(d / f"m{n - 1:04d}.png"))


def render_all(specs, W, H, fps, cache_dir, font_pi, renderer=None, warn=print):
    """Render every overlay spec ({kind,text,sub,position,dur}). Returns {key: {"dir", "n", "mask"}}
    in the same order. One Chrome session serves all specs; anything cached is skipped."""
    renderer = renderer or os.environ.get("CUTROOM_GFX") or ("html" if find_chrome() else "pil")
    out, todo = {}, []
    for sp in specs:
        n = max(1, int(round(sp["dur"] * fps)))
        key = spec_key(sp, W, H, fps, sp["dur"], font_pi, renderer)
        d = Path(cache_dir, f"gfx_{key}")
        glass = sp["kind"] in GLASS_KINDS
        out[sp["id"]] = {"dir": str(d), "n": n, "mask": glass, "key": key, "renderer": renderer}
        if not _done(d, n, glass):
            todo.append((sp, d, n, glass))
    if not todo:
        return out
    if renderer == "html":
        try:
            _render_html(todo, W, H, fps, font_pi)
            return out
        except Exception as e:  # noqa: BLE001 - any browser failure falls back to Pillow
            warn(f"html overlay renderer failed ({type(e).__name__}: {e}); using Pillow fallback")
            for sp in specs:     # re-key as pil so a later html run doesn't reuse pil frames
                n = out[sp["id"]]["n"]
                key = spec_key(sp, W, H, fps, sp["dur"], font_pi, "pil")
                out[sp["id"]].update(dir=str(Path(cache_dir, f"gfx_{key}")), key=key, renderer="pil")
            todo = [(sp, Path(out[sp["id"]]["dir"]), out[sp["id"]]["n"], sp["kind"] in GLASS_KINDS) for sp in specs
                    if not _done(Path(out[sp["id"]]["dir"]), out[sp["id"]]["n"], sp["kind"] in GLASS_KINDS)]
    _render_pil(todo, W, H, fps, font_pi)
    return out


def _render_html(todo, W, H, fps, font_pi):
    scale = H / 1080.0
    vw = int(round(W / scale))
    with Chrome(find_chrome(), vw, 1080, scale) as ch:
        for sp, d, n, glass in todo:
            tmp = Path(str(d) + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            css, body, js = _design(sp)
            page = PAGE.format(fontface=_fontface(font_pi), css=BASE_CSS.replace("1920px", f"{vw}px"), extra=css,
                               body=body, js=BASE_JS, render=js)
            hp = tmp / "page.html"
            hp.write_text(page)
            ch.load(hp)
            dur = n / fps
            draw, hold = _frame_plan(n, fps)
            for mask in ((False, True) if glass else (False,)):
                ch.eval(f"document.body.classList.toggle('mask',{str(mask).lower()})")
                pre = "m" if mask else ""
                for f in draw:
                    ch.eval(f"render({f / fps:.5f},{dur:.5f})")
                    ch.shot(tmp / f"{pre}{f:04d}.png")
                for f, src in hold.items():
                    _link(tmp / f"{pre}{src:04d}.png", tmp / f"{pre}{f:04d}.png")
            shutil.rmtree(d, ignore_errors=True)
            os.replace(tmp, d)


# ============================================================================== Pillow fallback
def _ease_out(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def _pil_layout(sp, W, H, font_pi):
    """Static RGBA card + mask at full frame, and the card's anchor box."""
    s = H / 1080.0
    kind = sp["kind"]
    text, sub = sp.get("text", ""), sp.get("sub", "") or ""
    if kind == "label":
        text = text.upper()
    sizes = {"callout": (40, 0), "lower_third": (46, 28), "label": (25, 0), "title": (92, 30), "title_card": (118, 30)}
    ts, ss = sizes[kind]
    ft = captions.load_font(font_pi, int(ts * s), "Bold" if kind.startswith("title") else "SemiBold")
    fs = captions.load_font(font_pi, int(ss * s)) if sub and ss else None
    tw = ft.getlength(text) + (len(text) * 0.16 * ts * s if kind == "label" else 0)
    sw = fs.getlength(sub.upper() if kind.startswith("title") else sub) if fs else 0
    a, dsc = ft.getmetrics()
    th = a + dsc
    sh = sum(fs.getmetrics()) if fs else 0
    padx, pady = {"callout": (34, 20), "lower_third": (38, 25), "label": (24, 13)}.get(kind, (0, 0))
    padx, pady = padx * s, pady * s
    dot = 32 * s if kind == "callout" else (27 * s if kind == "lower_third" else 0)
    cw = int(max(tw, sw) + 2 * padx + dot)
    chh = int(th + (sh + 8 * s if sub and fs else 0) + 2 * pady)
    pos = sp.get("position") or "top-left"
    if pos == "center" or kind.startswith("title"):
        x0, y0 = (W - cw) // 2, (H - chh) // 2
    else:
        v, _, h = pos.partition("-")
        x0 = int(MARGIN_X * s) if h == "left" else int(W - MARGIN_X * s - cw)
        bottom = (210 if kind == "lower_third" else MARGIN_Y + 90) * s
        y0 = int(MARGIN_Y * s) if v == "top" else int(H - bottom - chh)
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(card)
    r = int((16 if kind == "label" else 26) * s)
    if kind in GLASS_KINDS:
        ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x0 + cw, y0 + chh], r, fill=255)
        d.rounded_rectangle([x0, y0, x0 + cw, y0 + chh], r, fill=(255, 255, 255, 34), outline=(255, 255, 255, 50), width=max(1, int(s)))
    tx = x0 + padx + dot
    if kind == "callout":
        cy = y0 + chh / 2
        d.ellipse([x0 + padx, cy - 7 * s, x0 + padx + 14 * s, cy + 7 * s], fill=(255, 207, 110, 255))
    if kind == "lower_third":
        d.rounded_rectangle([x0 + padx, y0 + pady, x0 + padx + 5 * s, y0 + chh - pady], int(3 * s), fill=(255, 207, 110, 255))
    if kind.startswith("title"):
        tx = (W - tw) / 2
    shadow = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shadow)
    tl = Image.new("L", (W, H), 0)
    td = ImageDraw.Draw(tl)
    ty = y0 + pady
    if kind == "label":
        x = tx
        for chh_ in text:
            td.text((x, ty), chh_, font=ft, fill=255)
            x += ft.getlength(chh_) + 0.16 * ts * s
    else:
        td.text((tx, ty), text, font=ft, fill=255)
        sd.text((tx, ty + 2 * s), text, font=ft, fill=255)
    if sub and fs:
        stxt = sub.upper() if kind.startswith("title") else sub
        sx = (W - sw) / 2 if kind.startswith("title") else tx
        td.text((sx, ty + th + 8 * s), stxt, font=fs, fill=215)
        sd.text((sx, ty + th + 10 * s), stxt, font=fs, fill=255)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * s)).point(lambda v: int(v * 0.3))
    sh_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh_img.putalpha(shadow)
    card = Image.alpha_composite(sh_img, card)
    txt = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    txt.putalpha(tl)
    card = Image.alpha_composite(card, txt)
    return card, mask


def _render_pil(todo, W, H, fps, font_pi):
    import numpy as np
    s = H / 1080.0
    for sp, d, n, glass in todo:
        tmp = Path(str(d) + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        card, mask = _pil_layout(sp, W, H, font_pi)
        ca = np.asarray(card).copy()
        ma = np.asarray(mask)
        draw, hold = _frame_plan(n, fps)
        dur = n / fps
        for f in draw:
            t = f / fps
            i = _ease_out(t / 0.5)
            o = min(1.0, max(0.0, (t - (dur - OUTRO_S)) / OUTRO_S))
            alpha = min(min(1.0, i * 1.8), 1 - o)
            dy = int(round(((1 - i) * 18 + o * 8) * s))
            fr = np.zeros_like(ca)
            mk = np.zeros_like(ma)
            if dy >= 0:
                fr[dy:] = ca[:H - dy] if dy else ca
                mk[dy:] = ma[:H - dy] if dy else ma
            fr[..., 3] = (fr[..., 3].astype(np.float32) * alpha).astype(np.uint8)
            Image.fromarray(fr, "RGBA").save(tmp / f"{f:04d}.png", compress_level=1)
            if glass:
                mrgba = np.zeros((H, W, 4), np.uint8)
                mrgba[..., :3] = 255
                mrgba[..., 3] = (mk.astype(np.float32) * alpha).astype(np.uint8)
                Image.fromarray(mrgba, "RGBA").save(tmp / f"m{f:04d}.png", compress_level=1)
        for f, src in hold.items():
            _link(tmp / f"{src:04d}.png", tmp / f"{f:04d}.png")
            if glass:
                _link(tmp / f"m{src:04d}.png", tmp / f"m{f:04d}.png")
        shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)


# ============================================================================== placement
def auto_position(kind, faces, W, H, captions_bottom=True):
    """Corner farthest from the detected faces (output pixel boxes). Top corners are preferred
    when captions sit at the bottom so cards never fight the subtitles."""
    if kind in ("title", "title_card"):
        return "center"
    if kind == "lower_third":
        cands = ["bottom-left", "bottom-right"]
    else:
        cands = ["top-left", "top-right"] + ([] if captions_bottom else ["bottom-left", "bottom-right"])
    if not faces:
        return cands[0]
    pts = {"top-left": (W * 0.15, H * 0.12), "top-right": (W * 0.85, H * 0.12),
           "bottom-left": (W * 0.15, H * 0.82), "bottom-right": (W * 0.85, H * 0.82)}
    centres = [(x + w / 2, y + h / 2) for x, y, w, h in faces]
    return max(cands, key=lambda c: min((pts[c][0] - fx) ** 2 + (pts[c][1] - fy) ** 2 for fx, fy in centres))
