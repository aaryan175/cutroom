#!/usr/bin/env python3
"""Drive the studio UI in headless Chrome and screenshot each step (dev only).

    python3 cdp.py --port 9333 launch
    python3 ui_smoke.py --port 9333 --url http://127.0.0.1:8611/ --out /tmp/shots
    python3 cdp.py --port 9333 quit

Prints PASS/FAIL per step plus any console errors or exceptions. --real skips steps that need mock
data (chat edits, seeded markers).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Page  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9333)
ap.add_argument("--url", default="http://127.0.0.1:8611/")
ap.add_argument("--out", default="/tmp/cutroom-shots")
ap.add_argument("--real", action="store_true")
ap.add_argument("--only", default="")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
p = Page(a.port)
p.console_on()
p.send("Network.enable")
p.send("Network.setCacheDisabled", cacheDisabled=True)
results = []


def ok(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra else ""), flush=True)


def shot(name):
    p.shot(os.path.join(a.out, name + ".png"))


def rect(sel):
    return p.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }})()")


def S(expr):
    return p.eval(f"(() => {{ const S = window.__studio.S; return {expr}; }})()")


def tl_xy(t, track):
    return p.eval(f"(() => {{ const T = window.__studio.tl; const c = document.querySelector('.tl-canvas-wrap canvas').getBoundingClientRect(); const ty = T.trackY()['{track}']; return [c.left + T.tToX({t}), c.top + ty.y + ty.h / 2]; }})()")


p.size(1600, 1000)
p.send("Page.navigate", url=a.url)
ok("app boots and opens a plan", p.wait("window.__studio && window.__studio.S.tl && window.__studio.S.tl.v1.length > 0", 20))
time.sleep(2)
p.eval("document.activeElement && document.activeElement.blur()")
original_draft = S("S.plan && S.plan.draft")
p.eval("window.__studio.emit('showProjectTab', 'clips'); window.__studio.emit('showDock', 'inspector')")
time.sleep(0.8)
shot("01_overview_1600")
n_v1 = S("S.tl.v1.length")
ok("timeline has pieces", n_v1 > 0, f"{n_v1} V1 pieces")

# ---- project panel: virtualised grid
cells = p.eval("document.querySelectorAll('#project-body .cell').length")
ok("clip grid is virtualised", 0 < cells < 200, f"{cells} cells in DOM")
p.eval("document.querySelector('#project-body .vscroll').scrollTop = 40000")
time.sleep(1.2)
cells2 = p.eval("[...document.querySelectorAll('#project-body .cell')].filter(c => c._id).length")
ok("deep scroll fetches a later page", cells2 > 0, f"{cells2} filled cells after scroll")
p.eval("document.querySelector('#project-body .vscroll').scrollTop = 0")
time.sleep(0.6)

# ---- search + filter
p.eval("(() => { const i = document.querySelector('#project-body input[type=search]'); i.value = 'coffee'; i.dispatchEvent(new Event('input')); })()")
time.sleep(1.0)
cnt = p.eval("document.querySelector('.pj-count span').textContent")
ok("search filters the library", cnt and 'clip' in cnt, cnt)
p.eval("(() => { const i = document.querySelector('#project-body input[type=search]'); i.value = ''; i.dispatchEvent(new Event('input')); })()")
time.sleep(0.8)

# ---- hover scrub on a thumb
r = rect("#project-body .cell .th")
if r:
    p.mouse("mouseMoved", r[0] + r[2] * 0.2, r[1] + r[3] / 2, buttons=0)
    time.sleep(0.15)
    p.mouse("mouseMoved", r[0] + r[2] * 0.8, r[1] + r[3] / 2, buttons=0)
    time.sleep(0.4)
    src = p.eval("document.querySelector('#project-body .cell .th img').src")
    ok("hover-scrub requests a thumb at a later time", 't=' in (src or ''), src)
    p.mouse("mouseMoved", 5, 5, buttons=0)

# ---- load a talking clip in the Source monitor
cid = p.eval("""(async () => { const r = await fetch('/api/clips?speech=1&limit=5').then(r => r.json()); return r.rows[0].id; })()""")
p.eval(f"window.__studio.emit('loadSource', {{id: {json.dumps(cid)}}})")
ok("source monitor loads the clip", p.wait("window.__studio.S.source.clip && document.querySelectorAll('.transcript .w').length > 5", 10))
time.sleep(1.0)

# ---- drag-select transcript words -> In/Out
w = p.eval("(() => { const ws = [...document.querySelectorAll('.transcript .w')]; const a = ws[3].getBoundingClientRect(), b = ws[9].getBoundingClientRect(); return [a.left + 3, a.top + a.height/2, b.left + b.width - 3, b.top + b.height/2]; })()")
p.drag(w[0], w[1], w[2], w[3], steps=10)
time.sleep(0.3)
io = S("[S.source.in, S.source.out]")
ok("drag-selecting words sets In/Out", io[0] is not None and io[1] is not None and io[1] > io[0], str(io))
shot("02_source_inout")

# ---- insert at playhead (",")
p.eval("window.__studio.S.playhead = 0")
before = S("S.tl.v1.length")
dur_before = S("S.tl.duration")
p.key(",", code="Comma", vk=188)
time.sleep(1.2)
ok("insert (,) adds a segment", S("S.tl.duration") > dur_before + 0.05 or S("S.tl.v1.length") > before, f"{dur_before:.2f}s -> {S('S.tl.duration'):.2f}s")

# ---- click timeline to move playhead, razor (C)
x, y = tl_xy(12.3, "V1")
rr = rect(".tl-canvas-wrap canvas")
p.click(x, rr[1] + 10)   # ruler
time.sleep(0.3)
ph = S("S.playhead")
ok("clicking the ruler moves the playhead", abs(ph - 12.3) < 0.6, f"{ph:.2f}")
n0 = S("S.tl.v1.length")
p.key("c", code="KeyC", vk=67)
time.sleep(1.0)
ok("razor (C) splits at the playhead", S("S.tl.v1.length") == n0 + 1, f"{n0} -> {S('S.tl.v1.length')}")

# ---- select a segment and trim its out edge by dragging
seg = S("(() => { const p = S.tl.v1.find(q => q.kind === 'broll' && (q.end - q.start) > 1.5); return p && {sec: p.sec, seg: p.seg, start: p.start, end: p.end, out: p.out}; })()")
if seg:
    x0, y0 = tl_xy(seg["end"], "V1")
    pps = p.eval("window.__studio.tl.view.pps")
    p.mouse("mouseMoved", x0 - 2, y0, buttons=0)
    time.sleep(0.1)
    p.drag(x0 - 2, y0, x0 - 2 - pps * 0.6, y0, steps=8)
    time.sleep(1.0)
    seg2 = S(f"(() => {{ const s = S.plan.draft.sections[{seg['sec']}].segments[{seg['seg']}]; return s && s.out; }})()")
    ok("dragging a segment's out edge trims it", seg2 is not None and abs(seg2 - seg["out"]) > 0.2, f"out {seg['out']} -> {seg2}")
    # click to select it
    p.eval("window.__studio.emit('showDock', 'inspector')")
    x1, y1 = tl_xy((seg["start"] + seg["end"]) / 2 - 0.3, "V1")
    p.click(x1, y1)
    time.sleep(0.4)
    ok("clicking a segment selects it and fills the Inspector", S("S.selection.size") > 0 and p.wait("!!document.querySelector('.insp .seg-title .sw')", 3))
    shot("03_trim_select_inspector")

# ---- freeze frame
n1 = S("S.tl.v1.length")
p.key("f", code="KeyF", vk=70)
time.sleep(1.0)
ok("freeze (F) inserts a still", S("S.tl.v1.length") > n1 and S("S.tl.v1.some(p => p.kind === 'still')"))

# ---- delete / ripple delete on a selected segment
def select_seg(kind):
    q = S(f"(() => {{ const p = S.tl.v1.find(q => q.kind === '{kind}' && (q.end - q.start) > 1); return p && [p.start, p.end]; }})()")
    if not q:
        return False
    x, y = tl_xy((q[0] + q[1]) / 2, "V1")
    p.click(x, y)
    time.sleep(0.4)
    return True
if select_seg("broll"):
    d0 = S("S.tl.duration")
    p.key("Delete", code="Delete", vk=46)
    time.sleep(1.0)
    ok("Delete lifts the segment and keeps the gap", abs(S("S.tl.duration") - d0) < 0.05, f"{d0:.2f} -> {S('S.tl.duration'):.2f}")
if select_seg("broll"):
    d0 = S("S.tl.duration")
    p.key("Delete", code="Delete", vk=46, mods=8)
    time.sleep(1.0)
    ok("Shift+Delete ripple-deletes", S("S.tl.duration") < d0 - 0.3, f"{d0:.2f} -> {S('S.tl.duration'):.2f}")

# ---- undo / redo
d1 = S("S.tl.v1.length")
p.key("z", code="KeyZ", vk=90, mods=4)  # meta
time.sleep(1.0)
ok("undo (Cmd+Z) reverts the last edit", S("S.tl.v1.length") != d1, f"{d1} -> {S('S.tl.v1.length')}")
p.key("z", code="KeyZ", vk=90, mods=4 | 8)
time.sleep(1.0)
ok("redo (Shift+Cmd+Z) re-applies it", S("S.tl.v1.length") == d1)

# ---- marker
m0 = S("S.markers.length")
p.key("m", code="KeyM", vk=77)
time.sleep(1.0)
ok("marker (M) is created", S("S.markers.length") == m0 + 1)
p.type("Too long, trim the start")
p.key("Enter", code="Enter", vk=13)
time.sleep(0.8)
ok("marker note is saved", S("S.markers.some(m => m.text === 'Too long, trim the start')"))
shot("04_markers")

# ---- program playback
p.eval("window.__studio.S.playhead = 0")
p.eval("document.querySelector('#p-program .mon-view').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
p.key(" ", code="Space", vk=32)
time.sleep(2.5)
ph2 = S("S.playhead")
playing = p.eval("[...document.querySelectorAll('#p-program video')].some(v => !v.paused && !v.classList.contains('hidden'))")
ok("program plays the draft from proxies", ph2 > 1.5, f"playhead {ph2:.2f}s, video playing={playing}")
shot("05_playing")
p.key(" ", code="Space", vk=32)
time.sleep(0.3)

# ---- chat
if not a.real:
    pre = S("S.tl.v1.map(p => p.id + p.in + p.out).join()")
    p.eval("(() => { const t = document.querySelector('#claude-body textarea'); t.focus(); })()")
    p.type("Tighten the morning section")
    p.key("Enter", code="Enter", vk=13)
    ok("chat streams a reply", p.wait("document.querySelectorAll('.msg.asst').length && document.querySelector('.msg.asst:last-child').textContent.length > 80", 15))
    ok("quiet tool steps render", p.wait("!!document.querySelector('.msg.asst:last-child .steps-line')", 10))
    p.wait("!document.querySelector('.msg.asst.live')", 20)
    time.sleep(1.5)
    ok("Claude's plan edit refreshes the timeline", S("S.tl.v1.map(p => p.id + p.in + p.out).join()") != pre)
    shot("06_chat")

# ---- reference side by side
p.eval("document.querySelector('#p-program .ptab[data-ptab=reference]').click()")
time.sleep(1.5)
ok("reference view opens side by side", p.eval("!!document.querySelector('#p-program .mon-split > div:not([hidden]) video')"))
shot("07_reference")
p.eval("document.activeElement && document.activeElement.blur()")
if S("(S.project.references || []).length"):
    p.key("r", code="KeyR", vk=82)
    time.sleep(1.0)
    ok("R grabs a reference frame into chat", S("S.attachments.some(x => x.source === 'ref')"))
else:
    print("skip  R reference grab (project has no reference videos)")
p.eval("document.querySelector('#p-program .ptab[data-ptab=reference]').click()")

# ---- dock tabs
p.eval("window.__studio.emit('showDock', 'qa')")
time.sleep(0.3)
btn = p.eval("(() => { const b = document.querySelector('#dock-body .empty .acts button'); if (b) { b.click(); return true; } return false; })()")
time.sleep(1.0)
ok("QA report opens", p.eval("!!document.querySelector('.qa-wrap')"), "via button" if btn else "")
shot("08_qa")
p.eval("(() => { const td = document.querySelector('.qa-main td.tc'); if (td) td.click(); })()")
time.sleep(0.5)

nv = S("S.plan.versions.length")
if a.real:
    print("skip  commit / render / overnight (would write versions and start ffmpeg on the real project)")
else:
  p.eval("document.querySelector('#btn-commit').click()")
  ok("commit version (toolbar) adds a version", p.wait(f"window.__studio.S.plan.versions.length > {nv}", 5))
  p.wait("!document.querySelector('#btn-render').disabled", 3)
  p.eval("document.querySelector('#btn-render').click()")
  ok("render job shows progress", p.wait("!!document.querySelector('.job .bar i')", 6), p.eval("document.querySelector('#p-dock .ptab.on').textContent + ' :: ' + document.querySelector('#dock-body').innerText.slice(0, 200) + ' :: ' + [...document.querySelectorAll('.toast')].map(t => t.textContent).join(' | ')"))
  time.sleep(1.5)
  shot("09_jobs")
  p.eval("window.__studio.emit('showDock', 'overnight')")
  time.sleep(0.5)
  p.eval("(() => { const b = [...document.querySelectorAll('#dock-body button')].find(b => /Start overnight/.test(b.textContent)); if (b) b.click(); })()")
  time.sleep(3)
  ok("overnight panel shows governor gauges", p.eval("document.querySelectorAll('.gauge').length >= 2"))
  shot("10_overnight")

if a.real:
    p.eval("window.__studio.emit('showDock', 'overnight')")
    time.sleep(1.0)
    ok("overnight panel shows governor gauges", p.eval("document.querySelectorAll('.gauge').length >= 1"))
    shot("10_overnight")
    # put the real project's draft back the way we found it
    r = p.eval(f"fetch('/api/plan/' + window.__studio.S.plan.slug, {{method: 'PUT', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{plan: {json.dumps(original_draft)}, force: true}})}}).then(r => r.status)")
    ok("restored the real project's draft", r == 200, str(r))

# ---- help + small viewport
p.eval("document.activeElement && document.activeElement.blur()")
p.key("?", code="Slash", vk=191, mods=8)
time.sleep(0.4)
ok("help overlay opens", p.eval("!document.getElementById('help').hidden"))
shot("11_help")
p.key("Escape", code="Escape", vk=27)
p.size(1280, 800)
time.sleep(1.2)
shot("12_overview_1280")
p.size(1600, 1000)
time.sleep(0.8)
shot("13_final_1600")

p.drain(0.5)
probs = [x for x in p.problems() if "favicon" not in x]
print("\nconsole problems:" if probs else "\nno console errors or exceptions")
for x in probs:
    print("  " + x[:400])
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails or probs else 0)
