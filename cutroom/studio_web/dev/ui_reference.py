#!/usr/bin/env python3
"""UI pass for references, importing and versions (dev only): the drop overlay, the Add reference dialog
(paste a link), the Import queue, the Reference workspace (list, cut strip, breakdown), Compare with my
edit, side by side + onion skin, Match… (the chat request is intercepted, never sent), the ghost track,
"Save version" wording and the Versions menu.

    python3 cdp.py --port 9335 launch
    python3 ui_reference.py --port 9335 --url http://127.0.0.1:8643/ --out /tmp/shots
    python3 cdp.py --port 9335 quit

Needs a project with at least one analysed reference and a plan with a saved version. It uploads one
1x1 PNG (Import → Image) into the project's studio/images/ and removes it again through the filesystem
only when --cleanup-dir is given.
"""
import argparse
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Page  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9335)
ap.add_argument("--url", default="http://127.0.0.1:8643/")
ap.add_argument("--out", default="/tmp/cutroom-shots")
ap.add_argument("--cleanup-dir", default="", help="the project's work folder: delete the test image afterwards")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
p = Page(a.port)
p.console_on()
p.send("Network.enable")
p.send("Network.setCacheDisabled", cacheDisabled=True)
res = []


def ok(name, cond, extra=""):
    res.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra else ""), flush=True)


def S(expr):
    return p.eval(f"(() => {{ const S = window.__studio.S; return {expr}; }})()")


def shot(n):
    p.shot(os.path.join(a.out, n + ".png"))


def rect(sel):
    return p.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }})()")


def click(sel):
    return p.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return false; e.click(); return true; }})()")


p.size(1600, 1000)
p.send("Page.navigate", url=a.url)
ok("app boots", p.wait("window.__studio && window.__studio.S.tl && window.__studio.S.plan", 25))
time.sleep(2)
# never let a chat message reach the backend: /api/chat is answered locally and recorded
p.eval("""(() => { window.__chatSent = []; const f0 = window.fetch;
  window.fetch = (u, o) => { if (String(u).includes('/api/chat') && o && o.method === 'POST') {
      window.__chatSent.push(JSON.parse(o.body).message);
      const body = 'event: done\\ndata: {"type":"done","session_id":"ui-test"}\\n\\n';
      return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })); }
    return f0(u, o); }; })()""")

# ---- Save version wording + draft state
hdr = p.eval("document.querySelector('#hdr').innerText")
titles = p.eval("[...document.querySelectorAll('#hdr [title]')].map(e => e.title).join(' | ')")
ok("'Save version' replaces 'Commit'", "Save version" in hdr and "commit" not in (hdr + titles).lower(), hdr.replace("\n", " ")[:120])
ds = p.eval("document.querySelector('#draft-state').textContent")
ok("header says Saved as vN / Draft — unsaved changes", ds.startswith("Saved as v") or ds.startswith("Draft"), ds)
p.key("?", code="Slash", vk=191, mods=8)
time.sleep(0.3)
helptxt = p.eval("document.getElementById('help').innerText")
ok("help lists Save version (⌘S)", "Save version" in helptxt and "Commit" not in helptxt)
p.key("Escape", code="Escape", vk=27)
click("#btn-commit + .qhelp") or click(".vwrap + .qhelp")
time.sleep(0.3)
ok("Save version has a ? explainer", "numbered copy" in (p.eval("(document.querySelector('.qpop') || {}).innerText || ''") or ""))
p.eval("document.body.click()")

# ---- Versions menu
nv = S("S.plan.versions.length")
p.eval("document.querySelector('#btn-versions').click()")
time.sleep(0.4)
rows = p.eval("document.querySelectorAll('#versions-menu .vm-row').length")
ok("versions menu lists the saved versions", rows == nv and rows > 0, f"{rows} rows, {nv} versions")
acts = p.eval("[...document.querySelector('#versions-menu .vm-row').querySelectorAll('.vm-acts button')].map(b => b.textContent)")
ok("each version: Open / Compare with draft / Restore", acts == ["Open", "Compare with draft", "Restore"], str(acts))
shot("36_versions_menu")
p.eval("[...document.querySelectorAll('#versions-menu .vm-row')].pop().querySelectorAll('.vm-acts button')[1].click()")
ok("Compare with draft opens a change list (or both renders)", p.wait("!!document.querySelector('.imp-card.vcmp .vdiff, .imp-card.vsbs video')", 8))
time.sleep(0.4)
shot("37_version_compare")
p.key("Escape", code="Escape", vk=27)
p.eval("document.querySelectorAll('.imp-ov').forEach(e => e.click())")

# ---- drop overlay
p.eval("""(() => { const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(16)], 'trip.mp4', {type: 'video/mp4'})); dt.items.add(new File([new Uint8Array(16)], 'b.mov', {type: 'video/quicktime'}));
  window.dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true, cancelable: true})); })()""")
time.sleep(0.5)
ok("drop overlay appears on dragenter", p.eval("!document.querySelector('#drop-ov').hidden"))
kinds = p.eval("[...document.querySelectorAll('#drop-ov .drop-t')].map(t => t.dataset.kind + (t.classList.contains('off') ? ':off' : ''))")
ok("four targets; videos can't go to Song / Image", kinds == ["footage", "reference", "song:off", "image:off"], str(kinds))
ok("drop overlay has a ? explainer", p.eval("!!document.querySelector('#drop-ov .qhelp')"))
shot("35_drop_overlay")
p.key("Escape", code="Escape", vk=27)
time.sleep(0.2)
ok("Escape cancels the drop", p.eval("document.querySelector('#drop-ov').hidden"))

# ---- an image dropped on the overlay goes straight to Image (uploads, shows in the queue)
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
p.eval(f"""(() => {{ const b = Uint8Array.from(atob('{base64.b64encode(png).decode()}'), c => c.charCodeAt(0));
  const dt = new DataTransfer(); dt.items.add(new File([b], 'ui-test-look.png', {{type: 'image/png'}}));
  window.dispatchEvent(new DragEvent('dragenter', {{dataTransfer: dt, bubbles: true, cancelable: true}}));
  document.querySelector('#drop-ov').dispatchEvent(new DragEvent('drop', {{dataTransfer: dt, bubbles: true, cancelable: true}})); }})()""")
ok("an image upload shows in the import queue and finishes", p.wait("!!document.querySelector('#imp-q:not([hidden])') && [...window.__studio.S.jobs.values()].some(j => j.kind === 'upload' && j.status === 'done')", 10))
p.eval("document.querySelector('.imp-pop').hidden && document.querySelector('#imp-q').click()")
time.sleep(0.3)
ok("queue popover lists the upload", p.eval("!document.querySelector('.imp-pop').hidden") and "ui-test-look" in (p.eval("document.querySelector('.imp-pop').innerText") or ""))
shot("40_import_queue")
p.eval("document.body.click()")

# ---- paste a link -> Add reference dialog (not submitted)
p.eval("document.activeElement && document.activeElement.blur()")
p.eval("""(() => { const dt = new DataTransfer(); dt.setData('text/plain', 'https://www.youtube.com/watch?v=abc123');
  document.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); })()""")
ok("pasting a link opens Add reference with the link filled in", p.wait("(document.querySelector('.imp-card.addref .ref-link') || {}).value === 'https://www.youtube.com/watch?v=abc123'", 4))
time.sleep(0.6)
shot("38_add_reference")
p.eval("document.querySelectorAll('.imp-ov').forEach(e => e.click())")

# ---- Reference workspace
p.eval("document.querySelector('#p-program .ptab[data-ptab=reference]').click()")
ok("Reference tab opens the workspace", p.wait("!!document.querySelector('#ref-ws:not([hidden]) video')", 6))
ok("reference list shows the analysed reference", p.wait("document.querySelectorAll('.rw-item').length >= 1 && /shots/.test(document.querySelector('.rw-item.on .rw-m').textContent)", 10))
p.wait("document.querySelectorAll('.cs-tile').length > 20", 15)
n_tiles = p.eval("document.querySelectorAll('.cs-tile').length")
n_shots = p.eval("window.__studio.ref.R.data.analysis.shots.length")
ok("cut strip: one tile per shot", n_tiles == n_shots and n_tiles > 0, f"{n_tiles} tiles, {n_shots} shots")
ok("strip has section bands, caption badges and the beat/flash canvas", p.eval("document.querySelectorAll('.cs-band').length > 1 && document.querySelectorAll('.cs-cap').length > 0 && !!document.querySelector('.cs-cv')"))
p.eval("document.querySelector('.rw-tabs button[data-tab=breakdown]').click()")
time.sleep(0.3)
ok("breakdown: stats, content mix, caption swatch, colour per part", p.eval("document.querySelectorAll('.bk-grid .st').length === 6 && !!document.querySelector('.mix-bar') && document.querySelector('.cap-sw').getBoundingClientRect().height > 40 && document.querySelectorAll('.sec-row .sec-sw i').length > 3"))
ok("reference panel has a ? explainer", p.eval("!!document.querySelector('.rw-hd .qhelp')"))
time.sleep(1.5)
shot("30_ref_workspace")

# clicking a tile seeks the reference
r = p.eval("(() => { const t = [...document.querySelectorAll('.cs-tile')].find(e => { const b = e.getBoundingClientRect(); return b.left > 380 && b.width > 18; }); const b = t.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2, +t.dataset.i]; })()")
p.click(r[0], r[1])
time.sleep(0.6)
rt = p.eval("document.querySelector('#ref-ws video').currentTime")
want = p.eval(f"window.__studio.ref.R.data.analysis.shots[{r[2]}].start")
ok("clicking a tile seeks the reference", abs(rt - want) < 0.1, f"shot {r[2] + 1}: {rt:.2f}s vs {want:.2f}s")
ok("the transport shows the shot number", f"shot {r[2] + 1} /" in p.eval("document.querySelector('.rw-shot').textContent"))

# shift-drag selects a range
box = rect(".cs-scroll")
y = box[1] + 45
p.mouse("mouseMoved", box[0] + 60, y, buttons=0)
p.mouse("mousePressed", box[0] + 60, y, mods=8)
for k in range(1, 9):
    p.mouse("mouseMoved", box[0] + 60 + 25 * k, y, mods=8)
    time.sleep(0.02)
p.mouse("mouseReleased", box[0] + 260, y, mods=8)
time.sleep(0.3)
sel = p.eval("window.__studio.ref.R.sel")
ok("shift-drag on the strip selects a part", sel and sel["t1"] > sel["t0"], json.dumps(sel))
ok("the selection shows as a pill", "shot" in (p.eval("(document.querySelector('.sel-pill') || {}).textContent || ''") or ""))

# Match… composes the request (intercepted)
p.eval("document.querySelector('.match-btn').click()")
time.sleep(0.3)
ok("Match… offers the four requests", p.eval("[...document.querySelectorAll('.match-menu button')].map(b => b.dataset.match).join()") == "intro,pacing,captions,colour")
shot("34_ref_match_menu")
p.eval("document.querySelector('.match-menu button[data-match=intro]').click()")
ok("Match intro sends one chat message", p.wait("window.__chatSent.length === 1", 5))
msg = p.eval("window.__chatSent[0] || ''")
rid = p.eval("window.__studio.ref.R.cur")
ok("…naming the reference id and the selected part", "intro" in msg and f"ref {rid}" in msg and "(shots " in msg, msg.replace("\n", " / "))
p.eval("window.__studio.ref.R.sel = null")
p.eval("window.__studio.ref.matchMessage && 0")
cap = p.eval("window.__studio.ref.matchMessage('captions')")
ok("Match caption style message", "caption style" in cap and f"ref {rid}" in cap, cap.replace("\n", " / "))
pace = p.eval("window.__studio.ref.matchMessage('pacing')")
ok("Match pacing names a section range", "pacing" in pace and "(shots " in pace, pace.replace("\n", " / "))

# ---- Compare with my edit
p.eval("document.querySelector('.rw-tabs button[data-tab=compare]').click()")
ok("compare shows a verdict", p.wait("!!document.querySelector('.verdict')", 150))
time.sleep(0.6)
ok("compare draws the two-line pace curve", p.eval("(() => { const c = document.querySelector('canvas.curve'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n > 500; })()"))
ngaps = p.eval("document.querySelectorAll('.gap').length")
ok("compare lists the gaps in plain words", ngaps > 0, f"{ngaps} gaps")
ok("metrics table: reference | yours | gap", p.eval("document.querySelectorAll('table.mt tbody tr').length > 2"))
shot("31_ref_compare")
jt = p.eval("(() => { const b = [...document.querySelectorAll('.gap .jump')].pop(); b.click(); return b.textContent; })()")
time.sleep(0.8)
gt = p.eval("window.__studio.ref.R.cmp.gaps.filter(g => g.t !== null && g.t !== undefined).pop().t")
ok("a gap's time chip seeks the program", abs(S("S.playhead") - min(gt, S("S.tl.duration"))) < 0.1, f"{jt} -> {S('S.playhead'):.2f}")
ok("…and shows it side by side", p.eval("document.querySelector('#ref-ws').classList.contains('m-sbs') && !!document.querySelector('.rw-progslot .mon video')"))

# ---- side by side, linked
p.eval("document.querySelector('.rw-tabs button[data-tab=breakdown]').click()")
time.sleep(0.4)
if not p.eval("window.__studio.ref.R.lock"):
    click(".rw-link .btn")
r = p.eval("(() => { const t = [...document.querySelectorAll('.cs-tile')].find(e => { const b = e.getBoundingClientRect(); return b.left > 120 && b.width > 18; }); const b = t.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2, +t.dataset.i]; })()")
p.click(r[0], r[1])
time.sleep(0.8)
rt = p.eval("document.querySelector('#ref-ws video').currentTime")
ok("locked: seeking the reference moves your edit to the same moment", abs(S("S.playhead") - min(rt - p.eval("window.__studio.ref.R.offset"), S("S.tl.duration"))) < 0.12, f"ref {rt:.2f}s, program {S('S.playhead'):.2f}s")
time.sleep(1.0)
shot("32_ref_side_by_side")

# ---- onion skin
p.eval("document.querySelector('.rw-modes button[data-mode=onion]').click()")
time.sleep(0.8)
ok("onion skin: your edit over the reference", p.eval("!!document.querySelector('.rw-progslot .mon-frame.onion > video.onion-ref')"))
p.eval("document.querySelector('.rw-onion button[data-o=fade]').click()")
time.sleep(0.6)
op_ = p.eval("getComputedStyle([...document.querySelectorAll('.mon-frame.onion > video:not(.onion-ref)')][0]).opacity")
ok("…at partial opacity", 0 < float(op_) < 1, op_)
shot("33_ref_onion")
p.eval("document.querySelector('.rw-onion button[data-o=wipe]').click()")
time.sleep(0.6)
ok("wipe: a divider you drag", p.eval("!!document.querySelector('.mon-frame.onion.wipe .wipe-h:not([hidden])')"))
wr = rect(".wipe-h")
p.drag(wr[0] + 1, wr[1] + wr[3] / 2, wr[0] - 120, wr[1] + wr[3] / 2, steps=8)
time.sleep(0.3)
w = p.eval("window.__studio.ref.R.wipe")
ok("dragging the divider moves the wipe", w < 0.45, f"wipe at {w:.2f}")
shot("33b_ref_wipe")
p.eval("document.querySelector('.rw-modes button[data-mode=single]').click()")
time.sleep(0.5)
ok("leaving side by side puts the program monitor back", p.eval("!!document.querySelector('#program-body > .mon') && !document.querySelector('.mon-frame.onion')"))

# ---- ghost track
if not p.eval("window.__studio.ref.R.ghost"):
    p.eval("[...document.querySelectorAll('.rw-hd .btn')].find(b => /Show on timeline/.test(b.textContent)).click()")
p.eval("document.querySelector('.rw-hd .btn.back').click()")
time.sleep(0.8)
ok("closing the workspace returns to the edit", p.eval("document.querySelector('#ref-ws').hidden"))
ok("ghost track shows the reference's shots under the timeline", p.eval("(() => { const c = document.querySelector('canvas.ref-ghost'); return c && !c.hidden && c.width > 100; })()"))
shot("39_ghost_track")
p.eval("[...document.querySelectorAll('.rw-hd .btn')].find(b => /Show on timeline/.test(b.textContent)).click()")

# ---- R grabs a reference frame into chat (workspace opens itself)
p.eval("document.activeElement && document.activeElement.blur()")
p.key("r", code="KeyR", vk=82)
ok("R grabs a reference frame into chat", p.wait("window.__studio.S.attachments.some(x => x.source === 'ref')", 8))
p.key("Escape", code="Escape", vk=27)
p.eval("window.__studio.S.attachments.length = 0")

p.drain(0.5)
probs = [x for x in p.problems() if "favicon" not in x]
for x in probs:
    print("  " + x[:300])
if a.cleanup_dir:
    for f in os.listdir(os.path.join(a.cleanup_dir, "studio", "images")) if os.path.isdir(os.path.join(a.cleanup_dir, "studio", "images")) else []:
        if f.startswith("ui-test-look"):
            os.remove(os.path.join(a.cleanup_dir, "studio", "images", f))
print(f"\n{sum(res)}/{len(res)} passed, {len(probs)} console problems")
sys.exit(0 if all(res) and not probs else 1)
