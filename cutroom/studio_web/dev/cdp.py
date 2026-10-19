#!/usr/bin/env python3
"""Tiny Chrome DevTools Protocol driver for UI smoke tests of the studio (dev only).

    python3 cdp.py launch --port 9333                 # headless Chrome, temp profile (prints pid)
    python3 cdp.py --port 9333 open http://127.0.0.1:8611/ --size 1600x1000
    python3 cdp.py --port 9333 eval "document.title"
    python3 cdp.py --port 9333 shot out.png [--size 1600x1000]
    python3 cdp.py --port 9333 console                # console messages + exceptions since `open`
    python3 cdp.py --port 9333 quit

Needs `websocket-client`. Never point it at a browser you use yourself: `launch` always makes a fresh
temporary profile.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]


class Page:
    def __init__(self, port):
        self.port = port
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        tab = next(t for t in tabs if t.get("type") == "page")
        self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        self.i = 0
        self.events = []

    def send(self, method, **params):
        self.i += 1
        my = self.i
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    def eval(self, expr, await_promise=True):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=await_promise, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:800])
        return r.get("result", {}).get("value")

    def size(self, w, h):
        self.send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    def mouse(self, typ, x, y, buttons=1, button="left", mods=0, clicks=1):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y, button=button, buttons=buttons, modifiers=mods,
                  clickCount=clicks)

    def drag(self, x0, y0, x1, y1, steps=12):
        self.mouse("mouseMoved", x0, y0, buttons=0)
        self.mouse("mousePressed", x0, y0)
        for k in range(1, steps + 1):
            self.mouse("mouseMoved", x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps)
            time.sleep(0.016)
        self.mouse("mouseReleased", x1, y1)

    def click(self, x, y, clicks=1):
        self.mouse("mouseMoved", x, y, buttons=0)
        self.mouse("mousePressed", x, y, clicks=clicks)
        self.mouse("mouseReleased", x, y, clicks=clicks)

    def key(self, key, code=None, text=None, mods=0, vk=None):
        p = {"key": key, "code": code or key, "modifiers": mods}
        if vk:
            p["windowsVirtualKeyCode"] = vk
        self.send("Input.dispatchKeyEvent", type="keyDown", text=text or (key if len(key) == 1 else ""), **p)
        self.send("Input.dispatchKeyEvent", type="keyUp", **p)

    def type(self, text):
        self.send("Input.insertText", text=text)

    def wait(self, expr, timeout=10):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.eval(expr):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.1)
        return False

    def console_on(self):
        self.send("Runtime.enable")
        self.send("Log.enable")
        self.send("Page.enable")

    def drain(self, secs=0.3):
        self.ws.settimeout(secs)
        try:
            while True:
                self.events.append(json.loads(self.ws.recv()))
        except Exception:
            pass
        self.ws.settimeout(60)

    def problems(self):
        out = []
        for e in self.events:
            m = e.get("method")
            p = e.get("params", {})
            if m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning", "assert"):
                out.append(f"console.{p['type']}: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                out.append("exception: " + (d.get("exception", {}).get("description") or d.get("text", "")))
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") in ("error",):
                en = p["entry"]
                out.append(f"log.{en['level']}: {en.get('text')} {en.get('url', '')}")
        return out


def launch(port):
    exe = next((c for c in CHROME_CANDIDATES if os.path.exists(c)), None)
    if not exe:
        sys.exit("Chrome not found")
    prof = tempfile.mkdtemp(prefix="cutroom-cdp-")
    p = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                          "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                          "--autoplay-policy=no-user-gesture-required", "--mute-audio", "--window-size=1600,1000",
                          "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    with open(os.path.join(tempfile.gettempdir(), f"cutroom-cdp-{port}.json"), "w") as f:
        json.dump({"pid": p.pid, "profile": prof}, f)
    print(p.pid, prof)


def quit_(port):
    meta = os.path.join(tempfile.gettempdir(), f"cutroom-cdp-{port}.json")
    try:
        info = json.load(open(meta))
    except FileNotFoundError:
        print("no launched chrome on that port")
        return
    try:
        os.kill(info["pid"], 15)
    except ProcessLookupError:
        pass
    time.sleep(0.5)
    shutil.rmtree(info["profile"], ignore_errors=True)
    os.remove(meta)
    print("closed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("cmd")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--size")
    a = ap.parse_args()
    if a.cmd == "launch":
        return launch(a.port)
    if a.cmd == "quit":
        return quit_(a.port)
    pg = Page(a.port)
    if a.size:
        w, h = map(int, a.size.split("x"))
        pg.size(w, h)
    if a.cmd == "open":
        pg.console_on()
        pg.send("Page.navigate", url=a.arg)
        time.sleep(2)
    elif a.cmd == "eval":
        print(json.dumps(pg.eval(a.arg), indent=1))
    elif a.cmd == "shot":
        pg.shot(a.arg)
        print(a.arg)


if __name__ == "__main__":
    main()
