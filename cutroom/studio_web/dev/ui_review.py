#!/usr/bin/env python3
"""Drive the review-comments, looks and tour UI in headless Chrome and screenshot each step (dev only).

    python3 cdp.py --port 9336 launch
    python3 ui_review.py --port 9336 --url http://127.0.0.1:8644/ --out /tmp/shots [--only comments,looks,tour]
    python3 cdp.py --port 9336 quit

Run the studio with a fake agent (CUTROOM_CLAUDE_BIN=<stub>) when using "Send to AI": the run queue
makes real chat turns otherwise. Comments this script creates are deleted at the end (--keep keeps them),
and the looks it applies are undone.
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
ap.add_argument("--port", type=int, default=9336)
ap.add_argument("--url", default="http://127.0.0.1:8644/")
ap.add_argument("--out", default="/tmp/cutroom-shots")
ap.add_argument("--only", default="comments,looks,tour")
ap.add_argument("--run", action="store_true", help="also press 'Send to AI' (needs a fake agent!)")
ap.add_argument("--keep", action="store_true")
a = ap.parse_args()
only = set(a.only.split(","))
os.makedirs(a.out, exist_ok=True)
p = Page(a.port)
p.console_on()
p.send("Network.enable")
p.send("Network.setCacheDisabled", cacheDisabled=True)
results, created = [], []
base = a.url.rstrip("/")


def ok(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra else ""), flush=True)


def shot(name):
    p.shot(os.path.join(a.out, name + ".png"))


def S(expr):
    return p.eval(f"(() => {{ const S = window.__studio.S; return {expr}; }})()")


def rect(sel):
    return p.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }})()")


def click_sel(sel):
    r = rect(sel)
    if not r:
        return False
    p.click(r[0] + r[2] / 2, r[1] + r[3] / 2)
    return True


def http(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


def seek(t):
    p.eval(f"window.__studio.emit('seekTest', {t})")
    p.eval(f"(() => {{ const S = window.__studio.S; S.playhead = {t}; window.__studio.emit('playhead', 'force'); }})()")


p.size(1600, 1000)
p.send("Page.navigate", url=a.url)
p.send("Runtime.discardConsoleEntries")
p.events.clear()
ok("app boots", p.wait("window.__studio && window.__studio.S.tl && window.__studio.S.tl.v1.length > 0 && window.__comments && window.__looks", 25))
time.sleep(1.5)
p.eval("localStorage.setItem('cutroom.layout', JSON.stringify(Object.assign(JSON.parse(localStorage.getItem('cutroom.layout') || '{}'), {dockH: 262})))")
p.send("Page.reload", ignoreCache=True)
p.wait("window.__studio && window.__studio.S.tl && window.__studio.S.tl.v1.length > 0 && window.__comments", 25)
time.sleep(1.5)
slug = S("S.plan.slug")
hist0 = http("GET", f"/api/plan/{slug}")["history"]["pos"]

try:
    if "comments" in only:
        p.eval("window.__studio.emit('showDock', 'comments')")
        time.sleep(0.4)
        shot("comments_00_empty")
        n0 = S("S.markers.length")
        # pause on a talking shot, press M
        t1 = S("(S.tl.v1.find(p => p.kind === 'talk' || p.type === 'talk') || S.tl.v1[1]).start + 2.0")
        seek(t1)
        time.sleep(0.6)
        p.eval("document.activeElement && document.activeElement.blur()")
        click_sel("#p-program .mon-view")
        p.key("m", code="KeyM", vk=77)
        ok("M opens the composer", p.wait("window.__comments.draft && !document.querySelector('.cm-composer').hidden", 5))
        ok("comment is created at once", p.wait(f"window.__studio.S.markers.length === {n0 + 1}", 5))
        ok("composer text box has focus", p.eval("document.activeElement && document.activeElement.classList.contains('cm-c-text')"))
        ok("drawing toolbar shows over the program", p.eval("!document.querySelector('.cm-tools').hidden && document.querySelectorAll('.cm-tools .cm-tool').length >= 4"))
        # draw: circle, then arrow, then pen
        fr = rect("#p-program .mon-frame")
        X = lambda f: fr[0] + fr[2] * f  # noqa: E731
        Y = lambda f: fr[1] + fr[3] * f  # noqa: E731
        click_sel(".cm-tools .cm-tool[data-tool='circle']")
        p.drag(X(0.42), Y(0.3), X(0.62), Y(0.62))
        click_sel(".cm-tools .cm-tool[data-tool='arrow']")
        p.drag(X(0.18), Y(0.86), X(0.38), Y(0.6))
        click_sel(".cm-tools .cm-tool[data-tool='pen']")
        p.mouse("mouseMoved", X(0.7), Y(0.75), buttons=0)
        p.mouse("mousePressed", X(0.7), Y(0.75))
        for k in range(14):
            p.mouse("mouseMoved", X(0.7 + k * 0.01), Y(0.75 + 0.03 * ((-1) ** k)))
            time.sleep(0.01)
        p.mouse("mouseReleased", X(0.84), Y(0.75))
        time.sleep(0.2)
        ok("three marks drawn", p.eval("window.__comments.draft.drawing.length") == 3, p.eval("JSON.stringify(window.__comments.draft.drawing.map(s => s.tool))"))
        # range: move 1.5 s on, "Range to playhead"
        seek(t1 + 1.5)
        time.sleep(0.3)
        p.eval("document.querySelector('.cm-c-meta button.add').click()")
        ok("range end set", abs((p.eval("window.__comments.draft.t_end") or 0) - (t1 + 1.5)) < 0.05)
        p.eval("document.querySelector('.cm-c-text').focus()")
        p.type("Cut the pause before this line, start on the word")
        time.sleep(0.4)
        shot("comments_01_composer_drawing")
        p.key("Enter", code="Enter", vk=13)
        ok("posting closes the composer", p.wait("!window.__comments.draft", 5))
        ok("comment saved with text, drawing and range", p.wait(
            "window.__studio.S.markers.some(m => m.text === 'Cut the pause before this line, start on the word' && (m.drawing||[]).length === 3 && m.t_end > m.t && /comment_/.test(m.frame_url || ''))", 15))
        mid = S("S.markers.find(m => m.text === 'Cut the pause before this line, start on the word').id")
        created.append(mid)
        # the annotated frame really has the drawing
        mm = next(m for m in http("GET", f"/api/markers?plan={slug}")["markers"] if m["id"] == mid)
        ok("server composited the drawing into the frame", mm["frame_url"].startswith("/media/grab/comment_") and mm["frame_raw_url"] != mm["frame_url"])
        # two more comments (for the list + run): one plain, one a question
        for dt, txt in ((6.0, "Too long here, make it snappier"), (11.0, "Use the wide shot or the close-up?")):
            seek(t1 + dt)
            time.sleep(0.3)
            p.eval("document.activeElement && document.activeElement.blur()")
            p.key("m", code="KeyM", vk=77)
            p.wait("window.__comments.draft", 5)
            time.sleep(0.3)
            p.type(txt)
            p.key("Enter", code="Enter", vk=13)
            p.wait(f"window.__studio.S.markers.some(m => m.text === {json.dumps(txt)} && !String(m.id).startsWith('tmp'))", 10)
            created.append(S(f"S.markers.find(m => m.text === {json.dumps(txt)}).id"))
        time.sleep(1.0)
        p.eval(f"window.__comments.select({json.dumps(mid)})")
        time.sleep(0.6)
        ok("comments list shows the cards", p.eval("document.querySelectorAll('.cm-card').length") >= 3)
        ok("badge counts open comments", (p.eval("+document.getElementById('badge-markers').textContent") or 0) >= 3)
        shot("comments_02_list_detail")
        # timeline pins: colour by status + hover text + click opens the card
        pin = p.eval(f"(() => {{ const T = window.__studio.tl; const m = window.__studio.S.markers.find(x => x.id === {json.dumps(mid)}); const c = document.querySelector('.tl-canvas-wrap canvas').getBoundingClientRect(); return [c.left + T.tToX(m.t), c.top + 7]; }})()")
        p.mouse("mouseMoved", pin[0], pin[1], buttons=0)
        time.sleep(0.4)
        tip = p.eval("(() => { const t = [...document.querySelectorAll('#p-timeline *')].find(e => !e.hidden && e.children.length === 0 && /^Comment ·/.test(e.textContent)); return t ? t.textContent : ''; })()")
        ok("hovering a pin shows the comment", "Cut the pause" in (tip or ""), tip)
        shot("comments_03_timeline_pin_hover")
        p.eval("window.__studio.emit('showDock', 'inspector')")
        p.click(pin[0], pin[1])
        time.sleep(0.5)
        ok("clicking a pin opens its card", p.eval("document.querySelector('#p-dock .ptab.on').dataset.tab") == "comments"
           and p.eval(f"window.__comments.sel === {json.dumps(mid)}"))
        ok("its drawing shows on the paused program frame", p.eval("!document.querySelector('.cm-canvas').hidden"))
        shot("comments_04_pin_click_drawing")
        if a.run:
            p.eval("[...document.querySelectorAll('.cm-runbar button')].find(b => /Send \\d+ open to AI/.test(b.textContent)).click()")
            ok("run starts and marks the first one AI working", p.wait("!!(document.querySelector('.cm-pill.working') && document.querySelector('.cm-runbar .cm-prog'))", 15))
            time.sleep(1.2)
            shot("comments_05_ai_working")
            ok("run goes one by one to the end", p.wait("!document.querySelector('.cm-runbar .cm-prog') && !document.querySelector('.cm-pill.working')", 90))
            time.sleep(1.0)
            ms = {m["id"]: m for m in http("GET", f"/api/markers?plan={slug}")["markers"]}
            sts = [ms[i]["status"] for i in created]
            ok("statuses after the run", sts == ["done", "done", "needs_you"], str(sts))
            ok("AI replies attached to the comments", all(ms[i].get("thread") for i in created))
            p.eval(f"window.__comments.select({json.dumps(created[0])})")
            time.sleep(0.6)
            shot("comments_06_done_thread")
            p.eval(f"window.__comments.select({json.dumps(created[2])})")
            time.sleep(0.6)
            shot("comments_07_needs_you")
            p.eval("document.querySelector('.cm-filters .cm-ftab:nth-child(3)').click()")
            time.sleep(0.4)
            shot("comments_08_filter_needs_you")
            p.eval("document.querySelector('.cm-filters .cm-ftab:nth-child(1)').click()")

    if "looks" in only:
        t2 = S("(S.tl.v1.find(p => p.kind === 'talk' || p.type === 'talk') || S.tl.v1[1]).start + 1.0")
        seek(t2)
        time.sleep(0.3)
        p.eval("window.__studio.emit('showDock', 'looks')")
        ok("looks gallery renders real previews", p.wait("document.querySelectorAll('.lk-grid .lk-tile img').length >= 11 && [...document.querySelectorAll('.lk-grid .lk-tile img')].every(i => i.complete && i.naturalWidth > 100)", 60))
        time.sleep(0.8)
        shot("looks_01_gallery")
        p.eval("document.querySelector('.lk-tile[data-look=teal_orange]').click()")
        time.sleep(0.3)
        ok("after image loads the picked look", p.wait("(() => { const i = document.querySelector('.lk-after'); return i && i.complete && i.naturalWidth > 100 && /teal_orange/.test(i.src); })()", 30))
        sp = rect(".lk-split")
        p.mouse("mouseMoved", sp[0] + sp[2] * 0.35, sp[1] + sp[3] * 0.5, buttons=0)
        time.sleep(0.5)
        shot("looks_02_before_after_split")
        # intensity 70 %, apply to whole edit
        p.eval("(() => { const r = document.querySelector('.lk-range'); r.value = 70; r.dispatchEvent(new Event('input')); })()")
        time.sleep(1.2)
        p.eval("[...document.querySelectorAll('.lk-acts button')].find(b => /whole edit/.test(b.textContent)).click()")
        ok("apply to whole edit sets style.grade.look", p.wait("(() => { const g = (window.__studio.S.plan.draft.style || {}).grade || {}; return g.look && g.look.name === 'teal_orange' && Math.abs(g.look.intensity - 0.7) < 0.01; })()", 10))
        ok("timeline pieces carry the look", p.wait("window.__studio.S.tl.v1.some(p => p.look && p.look.name === 'teal_orange')", 10))
        ok("draft playback shows the look (CSS filter)", p.wait("/sepia|saturate|contrast/.test(document.querySelector('#p-program .mon-frame > video').style.filter)", 5))
        time.sleep(0.6)
        shot("looks_03_applied_whole_edit")
        # apply a different look to the segment under the playhead
        p.eval("document.querySelector('.lk-tile[data-look=bw]').click()")
        time.sleep(0.4)
        p.eval("[...document.querySelectorAll('.lk-acts button')].find(b => /this segment/.test(b.textContent)).click()")
        ok("apply to this segment sets segment.look", p.wait("window.__studio.S.tl.v1.some(p => p.look && p.look.name === 'bw')", 10))
        time.sleep(0.6)
        shot("looks_04_segment_bw")
        # inspector reflects it
        p.eval("(() => { const S = window.__studio.S; const pc = S.tl.v1.find(p => p.look && p.look.name === 'bw'); S.selection = new Set([pc.id]); window.__studio.emit('selection'); window.__studio.emit('showDock', 'inspector'); })()")
        time.sleep(0.6)
        ok("inspector shows the segment look", p.eval("[...document.querySelectorAll('.lk-field .lk-sel')].some(s => s.value === 'bw')"))
        shot("looks_05_inspector_segment_look")
        p.eval("(() => { window.__studio.S.selection = new Set(); window.__studio.emit('selection'); })()")
        time.sleep(0.5)
        ok("inspector shows the whole-edit look", p.eval("[...document.querySelectorAll('.lk-field .lk-sel')].some(s => s.value === 'teal_orange')"))
        shot("looks_06_inspector_whole_edit")
        # match a reference (if one is measured)
        refs = [r for r in http("GET", "/api/refs")["refs"] if r.get("status") == "analyzed"]
        if refs:
            p.eval("window.__studio.emit('showDock', 'looks')")
            p.wait("document.querySelectorAll('.lk-grid .lk-tile img').length >= 11", 30)
            time.sleep(0.5)
            p.eval("[...document.querySelectorAll('.lk-match button')].find(b => /Match/.test(b.textContent)).click()")
            ok("match a reference shows the side-by-side", p.wait("document.querySelectorAll('.lk-mrow img').length >= 2", 60))
            time.sleep(1.0)
            shot("looks_07_match_reference")
        else:
            ok("match a reference (skipped: no analyzed reference)", True)

    if "tour" in only:
        p.eval("window.__studio.emit('showDock', 'inspector')")
        p.eval("document.activeElement && document.activeElement.blur()")
        click_sel("#btn-help")
        time.sleep(0.3)
        ok("? menu offers the tour", p.eval("[...document.querySelectorAll('.tour-menu button')].some(b => /Show me around/.test(b.textContent))"))
        shot("tour_00_help_menu")
        p.eval("[...document.querySelectorAll('.tour-menu button')].find(b => /Show me around/.test(b.textContent)).click()")
        ok("tour opens", p.wait("!!(window.__tour.open && document.querySelector('.tour-card'))", 3))
        n = p.eval("window.__tour.steps")
        for i in range(n):
            time.sleep(0.45)
            ok(f"tour step {i + 1} shows", p.eval("window.__tour.index") == i and p.eval("!!document.querySelector('.tour-title').textContent"),
               p.eval("document.querySelector('.tour-title').textContent"))
            shot(f"tour_{i + 1:02d}")
            p.key("ArrowRight", code="ArrowRight", vk=39)
        time.sleep(0.4)
        ok("tour ends after the last step and is remembered", not p.eval("window.__tour.open") and p.eval("localStorage.getItem('cutroom.tourDone')") == "true")
        p.eval("window.__tour.start(3)")
        time.sleep(0.3)
        p.key("Escape", code="Escape", vk=27)
        time.sleep(0.3)
        ok("Esc closes the tour", not p.eval("window.__tour.open"))
finally:
    # cleanup: comments this run created, looks it applied
    if not a.keep:
        for mid in created:
            try:
                http("DELETE", f"/api/markers/{mid}?id={mid}")
            except Exception as e:  # noqa: BLE001
                print("cleanup", mid, e)
        try:
            for _ in range(10):
                pos = http("GET", f"/api/plan/{slug}")["history"]["pos"]
                if pos <= hist0:
                    break
                http("POST", f"/api/plan/{slug}/undo")
        except Exception as e:  # noqa: BLE001
            print("undo cleanup", e)
    p.drain(0.5)
    probs = [x for x in p.problems() if "importer.css" not in x and "reference.css" not in x]
    ok("no console errors", not probs, " | ".join(probs[:5]))
    print(f"\n{sum(r[1] for r in results)}/{len(results)} passed")
