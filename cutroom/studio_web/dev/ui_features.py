#!/usr/bin/env python3
"""Second UI pass: clip-aware chat, quiet steps, picks (bins), Days view, Story tab, first-run (dev only).

    python3 ui_features.py --port 9333 --url http://127.0.0.1:8611/ --out /tmp/shots [--real]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Page  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9333)
ap.add_argument("--url", default="http://127.0.0.1:8611/")
ap.add_argument("--out", default="/tmp/cutroom-shots")
ap.add_argument("--real", action="store_true", help="real backend: skip the chat turn (it would run Claude)")
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


p.size(1600, 1000)
p.send("Page.navigate", url=a.url)
ok("app boots", p.wait("window.__studio && window.__studio.S.tl && window.__studio.S.tl.v1.length > 0", 20))
time.sleep(1.5)
p.eval("window.__studio.emit('showProjectTab', 'clips'); window.__studio.emit('showDock', 'story')")
time.sleep(0.8)

# ---- Story tab
rows = p.eval("document.querySelectorAll('.story tbody tr').length")
ok("Story tab lists one row per piece", rows == S("S.tl.v1.length"), f"{rows} rows")
spoken = p.eval("[...document.querySelectorAll('.story td.line')].filter(td => td.textContent.includes('“')).length")
ok("Story rows show the spoken line", spoken > 0, f"{spoken} spoken rows")
p.eval("document.querySelectorAll('.story tbody tr')[6].click()")
time.sleep(0.3)
ok("clicking a Story row seeks", abs(S("S.playhead") - S("S.tl.v1[6].start")) < 0.05)
shot("20_story")

# ---- chat with clip tokens
if not a.real:
    p.eval("document.querySelector('#claude-body textarea').focus()")
    p.type("Find a better cutaway for the morning")
    p.key("Enter", code="Enter", vk=13)
    p.wait("!document.querySelector('.msg.asst.live') && document.querySelectorAll('.msg.asst').length", 25)
    time.sleep(1.2)
    last = ".msg.asst:last-child"
    ok("clip tokens render as chips", p.eval(f"document.querySelectorAll('{last} .xclip').length") >= 1)
    ok("a row of clip tokens becomes a picks strip", p.eval(f"document.querySelectorAll('{last} .picks .xpick').length") == 3)
    ok("seg / t / bin tokens render", p.eval(f"!!document.querySelector('{last} .xseg') && !!document.querySelector('{last} .xt') && !!document.querySelector('{last} .xbin')"))
    txt = p.eval(f"document.querySelector('{last}').innerText")
    ok("file paths are hidden", "/Users/" not in txt and ".json" not in txt, txt[txt.find('saved'):][:90] if 'saved' in txt else '')
    ok("code is folded under Details", p.eval(f"!!document.querySelector('{last} details.xcode:not([open])')"))
    ok("clip names are filled in", p.eval(f"[...document.querySelectorAll('{last} .xpick .xnm')].every(e => /^C\\d+/.test(e.textContent))"))
    steps = p.eval(f"document.querySelector('{last} .steps-txt').textContent")
    ok("tool steps collapse to one quiet line", steps and "cutroom" not in steps, steps)
    shot("21_chat_chips")
    p.eval(f"document.querySelector('{last} .steps-line').click()")
    time.sleep(0.3)
    ok("raw steps open behind the arrow", p.eval(f"document.querySelectorAll('{last} .step-row').length") >= 3)
    shot("22_chat_steps_open")
    p.eval(f"document.querySelector('{last} .steps-line').click()")
    # interactions
    p.eval(f"document.querySelector('{last} .picks .xpick').click()")
    ok("clicking a pick loads Source with In/Out", p.wait("window.__studio.S.source.clip && window.__studio.S.source.in === 6 && window.__studio.S.source.out === 9", 8))
    time.sleep(1.0)  # let the plan reload from Claude's edit settle
    p.eval(f"document.querySelector('{last} .xseg').click()")
    ok("clicking a segment chip selects it", p.wait("window.__studio.S.selection.size > 0", 3))
    p.eval(f"document.querySelector('{last} .xt').click()")
    time.sleep(0.3)
    ok("clicking a time chip seeks", abs(S("S.playhead") - 14.2) < 0.05)
    d0 = S("S.tl.duration")
    p.eval(f"""(() => {{ const src = document.querySelectorAll('{last} .picks .xpick')[1]; const T = window.__studio.tl;
      const c = document.querySelector('.tl-canvas-wrap canvas'); const b = c.getBoundingClientRect(); const ty = T.trackY().V1;
      const dt = new DataTransfer();
      src.dispatchEvent(new DragEvent('dragstart', {{bubbles: true, dataTransfer: dt}}));
      for (const t of ['dragover', 'drop']) c.dispatchEvent(new DragEvent(t, {{bubbles: true, cancelable: true, clientX: b.left + T.tToX(30), clientY: b.top + ty.y + 10, dataTransfer: dt}})); }})()""")
    time.sleep(1.2)
    ok("dragging a pick onto the timeline inserts it", S("S.tl.duration") > d0 + 2, f"{d0:.2f} -> {S('S.tl.duration'):.2f}")
    p.eval(f"document.querySelector('{last} .xbin').click()")
    time.sleep(0.8)
    ok("bin chip opens Claude's picks", p.eval("!!document.querySelector('#project-body .bin.open .bin-item')"))
    shot("23_picks")

# ---- Picks tab (bins)
p.eval("window.__studio.emit('showProjectTab', 'picks')")
time.sleep(0.6)
ok("Picks tab shows Claude's bins", p.eval("document.querySelectorAll('#project-body .bin').length") >= (0 if a.real else 2))
if a.real:
    shot("23_picks")

# ---- Days view
p.eval("window.__studio.emit('showProjectTab', 'clips')")
time.sleep(0.4)
p.eval("document.querySelector('#project-body .ib[title^=\"Days\"]').click()")
ok("Days view groups clips by capture date", p.wait("document.querySelectorAll('.day').length > 0 && !document.querySelector('.days-status')", 30))
p.eval("(() => { const d = document.querySelector('.day:not(.open) .day-h') ; if (d) d.click(); })()")
time.sleep(1.0)
ok("expanding a day shows its clips", p.eval("document.querySelectorAll('.day.open .cell').length") > 0)
shot("24_days")
p.eval("document.querySelector('#project-body .ib[title=\"Grid view\"]').click()")

# ---- voice button
has_sr = p.eval("!!(window.SpeechRecognition || window.webkitSpeechRecognition)")
ok("mic button matches Web Speech support", p.eval("!document.querySelector('.mic').hidden") == has_sr, f"speech api: {has_sr}")

# ---- first run (simulated: no plans in the project)
p.eval("""(() => { const st = window.__studio; st.S.project = {...st.S.project, plans: []}; st.S.plan = null; st.S.tl = null; st.emit('plan', {by: 'open'}); st.emit('project'); })()""")
time.sleep(0.6)
ok("first-run screen offers Understand my library", p.eval("!!document.querySelector('.first-run:not([hidden]) .big') && document.querySelectorAll('.first-run .prompts button').length === 4"))
shot("25_first_run")

p.drain(0.5)
probs = [x for x in p.problems() if "favicon" not in x]
for x in probs:
    print("  " + x[:300])
print(f"\n{sum(res)}/{len(res)} passed, {len(probs)} console problems")
