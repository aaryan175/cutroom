#!/usr/bin/env python3
"""Drive the factory UI in headless Chrome and screenshot it (dev only): comments -> Send to factory -> the
Factory tab while it works -> the Review tab and the /review page at desktop and phone width.

    python3 cdp.py --port 9351 launch
    python3 ui_factory.py --port 9351 --url http://127.0.0.1:8671/ --plan real --out /tmp/shots
    python3 cdp.py --port 9351 quit

Run the studio with the STUB agent (CUTROOM_CLAUDE_BIN=<wrapper of tests/fixtures/fake_agent.py>,
FAKE_AGENT_SCRIPT=<scenario>): pressing Send to factory starts a real order (real render, real QA).
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Page  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9351)
ap.add_argument("--url", default="http://127.0.0.1:8671/")
ap.add_argument("--plan", default="real")
ap.add_argument("--out", default="/tmp/cutroom-factory-shots")
ap.add_argument("--comments", default="", help="JSON list of {t, text, drawing?} to create first")
ap.add_argument("--timeout", type=int, default=3600)
ap.add_argument("--no-send", action="store_true", help="only screenshot (the order already ran)")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
base = a.url.rstrip("/")
p = Page(a.port)
p.console_on()
p.send("Network.enable")
p.send("Network.setCacheDisabled", cacheDisabled=True)
results = []


def ok(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra else ""), flush=True)


def api(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"null")


def shot(name):
    f = os.path.join(a.out, name + ".png")
    p.shot(f)
    print("shot", f, flush=True)


def go(url, w, h, mobile=False):
    p.send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=2 if mobile else 1, mobile=mobile)
    p.send("Page.navigate", url=url)
    time.sleep(2.5)


def js(expr):
    return p.eval(expr)


# ------------------------------------------------------------------ desktop: comments + Send to factory
go(base + "/", 1600, 1000)
ok("studio booted", p.wait("window.__studio && window.__factory", 30))
js(f"localStorage.setItem('cutroom.lastPlan', JSON.stringify({json.dumps(a.plan)}))")
go(base + "/", 1600, 1000)
p.wait(f"window.__studio.S.plan && window.__studio.S.plan.slug === {json.dumps(a.plan)}", 30)
ok("plan open", js("window.__studio.S.plan && window.__studio.S.plan.slug") == a.plan)
oid = None
if not a.no_send:
    for c in json.loads(a.comments or "[]"):
        api("POST", "/api/markers", {"plan": a.plan, "kind": "note", **c})
    js("window.__studio.emit('showDock', 'comments')")
    time.sleep(1.5)
    shot("01_comments_desktop")
    n = js("document.querySelector('.cm-factory-btn') && document.querySelector('.cm-factory-btn').textContent")
    ok("Send to factory button", n and "factory" in n, n)
    ok("Quick fix button", js("!!document.querySelector('.cm-quick-btn')"))
    js("document.querySelector('.cm-factory-btn').click()")
    time.sleep(2)
    orders = api("GET", f"/api/factory/orders?plan={a.plan}")["orders"]
    oid = orders[0]["id"] if orders else None
    ok("order created from the button", oid, oid)
    ok("comment pill 'in factory'", p.wait("!!document.querySelector('.cm-fx')", 10))
    js("window.__studio.emit('showDock', 'factory')")
    t_end = time.time() + a.timeout
    got_working = False
    while time.time() < t_end:
        st = api("GET", f"/api/factory/orders/{oid}")["status"]
        if st["state"] in ("running", "picked_up") and st["phase"] in ("edit", "render", "qa", "critic", "fix", "render2") \
                and not got_working:
            time.sleep(2)
            p.send("Emulation.setDeviceMetricsOverride", width=1600, height=1000, deviceScaleFactor=1, mobile=False)
            shot("02_factory_tab_working_desktop")
            ok("header light working", js("document.getElementById('fx-light').className").find("working") >= 0,
               js("document.getElementById('fx-light').textContent"))
            got_working = True
        if st["state"] in ("ready", "needs_you", "blocked", "stopped", "done"):
            break
        time.sleep(3)
    ok("order finished", st["state"] in ("ready", "needs_you"), f"{st['state']} {st.get('why') or ''}")
    time.sleep(4)
else:
    orders = api("GET", f"/api/factory/orders?plan={a.plan}")["orders"]
    oid = orders[0]["id"] if orders else None
# ------------------------------------------------------------------ desktop screenshots
go(base + "/", 1600, 1000)
p.wait("window.__factory", 20)
js("window.__studio.emit('showDock', 'factory')")
time.sleep(2.5)
shot("03_factory_tab_done_desktop")
ok("stepper shown", js("document.querySelectorAll('.fx-step').length") >= 6)
ok("team rows", js("document.querySelectorAll('.fx-role').length") >= 3)
js("window.__studio.emit('toggleReview', true)")
time.sleep(4)
shot("04_review_tab_desktop")
ok("review card rows", js("document.querySelectorAll('.rv-row').length") >= 1)
ok("changed-spots sheet", js("!!document.querySelector('.rv-sheet')"))
ok("verdict buttons", js("document.querySelectorAll('.rv-vbtns .btn').length") == 3)
ok("two players", js("[...document.querySelectorAll('.rv-pane video')].filter(v => v.src).length") == 2)
js("window.__factory.FX.sub = 'lessons'; window.__studio.emit('showDock', 'factory')")
time.sleep(0.5)
js("window.__review.close(); window.__studio.emit('showDock', 'factory')")
time.sleep(2)
shot("05_factory_lessons_desktop")
js("window.__factory.FX.sub = 'orders'")
go(base + "/review", 1600, 1000)
time.sleep(2)
shot("06_review_page_desktop")
ok("/review lists the version", js("document.querySelectorAll('.card').length") >= 1)
# ------------------------------------------------------------------ phone width
go(base + "/review", 400, 860, mobile=True)
time.sleep(2.5)
shot("07_review_page_phone")
ok("/review phone: verdict buttons", js("document.querySelectorAll('.b.approve').length") >= 1)
p.send("Emulation.setDeviceMetricsOverride", width=400, height=860, deviceScaleFactor=2, mobile=True)
js("window.scrollTo(0, 900)")
time.sleep(1)
shot("08_review_page_phone_scrolled")
go(base + "/", 400, 860, mobile=True)
p.wait("window.__factory", 20)
js("window.__studio.emit('showDock', 'factory')")
time.sleep(2.5)
shot("09_factory_tab_phone")
js("window.__studio.emit('toggleReview', true)")
time.sleep(3.5)
shot("10_review_tab_phone")
probs = [x for x in p.problems() if "favicon" not in x]
ok("no console errors", not probs, "; ".join(probs[:5]))
print(json.dumps({"order": oid, "passed": sum(r[1] for r in results), "failed": [r[0] for r in results if not r[1]]}))
