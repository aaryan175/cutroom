// Guided tour: a spotlight + callout walk through the real UI. Runs once on first visit (not in headless
// test browsers) and from the "?" menu → "Show me around". ←/→ to step, Esc to close; completion is
// remembered in localStorage. Steps whose element is missing still show, centred, so nothing breaks
// while a panel is being built.
import { S, emit } from './state.js';
import { h, LS } from './util.js';

const STEPS = [
  { sel: ['#p-project'], title: 'Your footage lives here', text: 'Clips, the picks the AI gathers into bins, songs, plans and renders. Search by what is said or what is in the shot.', side: 'right' },
  { sel: ['#p-source'], title: 'Source: pick the moment', text: 'Play a clip, read its transcript underneath, set In and Out with I and O, then press , to drop it on the timeline.', side: 'bottom' },
  { sel: ['#p-program'], title: 'Program: the edit itself', text: 'Draft plays your timeline instantly from proxies; Render plays the real rendered file with grade, captions and sound.', side: 'bottom' },
  { sel: ['#p-timeline'], title: 'The timeline is yours too', text: 'Drag, trim, razor with C, freeze with F. Your hand edits and the AI’s edits change the same draft, and undo covers both.', side: 'top' },
  { sel: ['#p-claude'], title: 'Talk to the AI like an editor', text: 'It always knows what you are pointing at: the playhead, the selected shot and the source In/Out. Say “this shot” and it knows which.', side: 'left' },
  { sel: ['#p-program .mon-bar .cm-btn', '#p-program .mon-bar .ib[title^="Add comment"]', '#p-dock .ptab[data-tab="comments"]'], title: 'Comments, one by one', text: 'Pause, press M, type what should change and draw on the frame. Send them to the AI: it fixes each one in order and replies on it.', side: 'top', dock: 'comments' },
  { sel: ['#p-program .ptab[data-ptab="reference"]', '[data-tab="reference"]', '#tab-reference', '#p-reference'], title: 'Make it like this video', text: 'Drop in a reference video: it gets measured (cuts, pacing, captions, colour) and the AI rebuilds your edit to match.', side: 'bottom' },
  { sel: ['#p-dock .ptab[data-tab="looks"]'], title: 'Looks: see it before you pick it', text: 'Every look rendered on your own frame. Hover the big preview to compare before and after, then apply it to the whole edit or one segment.', side: 'top', dock: 'looks' },
  { sel: ['#hdr .hdr-c'], title: 'Save a version, then render', text: 'Saving keeps a numbered version you can always go back to. Render turns a saved version into a real video file.', side: 'bottom' },
  { sel: ['#p-timeline .tb-btn.big', '#p-dock .ptab[data-tab="overnight"]', '#lib-status'], title: 'Let it understand your library', text: '“Understand my library” (the Overnight tab) transcribes and tags everything while you sleep, so the AI can find any moment by what is in it.', side: 'top', dock: 'overnight' },
];

let ov = null, spot = null, card = null, idx = 0, onKey = null;

function target(step) {
  for (const s of step.sel) {
    const el = document.querySelector(s);
    if (el && el.getClientRects().length) {
      const r = el.getBoundingClientRect();
      if (r.width > 2 && r.height > 2) return el;
    }
  }
  return null;
}

function place() {
  const step = STEPS[idx];
  const el = target(step);
  const W = window.innerWidth, H = window.innerHeight;
  const cw = Math.min(340, W - 32);
  card.style.width = cw + 'px';
  const ch = card.offsetHeight || 150;
  let r = null;
  if (el) {
    const b = el.getBoundingClientRect();
    const pad = 6;
    r = { x: Math.max(4, b.left - pad), y: Math.max(4, b.top - pad), w: Math.min(W - 8, b.width + pad * 2), h: Math.min(H - 8, b.height + pad * 2) };
    spot.hidden = false;
    Object.assign(spot.style, { left: r.x + 'px', top: r.y + 'px', width: r.w + 'px', height: r.h + 'px' });
  } else {
    spot.hidden = true;
  }
  ov.classList.toggle('nospot', !el);
  let x, y, side = step.side;
  if (!r) { x = (W - cw) / 2; y = (H - ch) / 2; side = 'none'; }
  else {
    const fits = { right: r.x + r.w + 16 + cw < W, left: r.x - 16 - cw > 0, bottom: r.y + r.h + 16 + ch < H, top: r.y - 16 - ch > 0 };
    if (!fits[side]) side = ['bottom', 'top', 'right', 'left'].find((s) => fits[s]) || 'inside';
    if (side === 'right') { x = r.x + r.w + 16; y = r.y + Math.min(40, r.h / 2 - 20); }
    else if (side === 'left') { x = r.x - 16 - cw; y = r.y + Math.min(40, r.h / 2 - 20); }
    else if (side === 'bottom') { x = r.x + r.w / 2 - cw / 2; y = r.y + r.h + 16; }
    else if (side === 'top') { x = r.x + r.w / 2 - cw / 2; y = r.y - 16 - ch; }
    else { x = r.x + r.w / 2 - cw / 2; y = r.y + r.h / 2 - ch / 2; }
    x = Math.max(12, Math.min(W - cw - 12, x)); y = Math.max(12, Math.min(H - ch - 12, y));
  }
  card.dataset.side = side;
  Object.assign(card.style, { left: x + 'px', top: y + 'px' });
  const arrow = card.querySelector('.tour-arrow');
  if (r && side !== 'inside' && side !== 'none') {
    const cx = clampN(r.x + r.w / 2 - x, 18, cw - 18), cy = clampN(r.y + Math.min(r.h / 2, 60) - y, 18, ch - 18);
    arrow.hidden = false;
    arrow.style.left = side === 'top' || side === 'bottom' ? cx + 'px' : '';
    arrow.style.top = side === 'left' || side === 'right' ? cy + 'px' : '';
  } else arrow.hidden = true;
}
const clampN = (v, a, b) => Math.max(a, Math.min(b, v));

function render() {
  const step = STEPS[idx];
  if (step.dock) emit('showDock', step.dock);
  const n = STEPS.length;
  card.innerHTML = '';
  card.append(
    h('div.tour-arrow'),
    h('div.tour-top', h('span.tour-n', `${idx + 1} / ${n}`), h('div.tour-dots', STEPS.map((_, i) => h('span' + (i === idx ? '.on' : i < idx ? '.past' : ''), { on: { click: () => go(i) } })))),
    h('div.tour-title', step.title),
    h('div.tour-text', step.text),
    h('div.tour-acts',
      h('button.btn.ghost.tour-skip', { on: { click: () => end(true) } }, idx === n - 1 ? 'Close' : 'Skip tour'),
      h('span.grow'),
      idx > 0 ? h('button.btn', { on: { click: () => go(idx - 1) } }, 'Back') : null,
      h('button.btn.primary', { on: { click: () => (idx === n - 1 ? end(true) : go(idx + 1)) } }, idx === n - 1 ? 'Start editing' : 'Next')));
  requestAnimationFrame(() => { place(); requestAnimationFrame(place); });
}
function go(i) { idx = Math.max(0, Math.min(STEPS.length - 1, i)); render(); }

export function startTour(at = 0) {
  if (ov) end(false);
  idx = at;
  ov = h('div.tour', { on: { click: (e) => { if (e.target === ov) go(idx + 1 < STEPS.length ? idx + 1 : idx); } } });
  spot = h('div.tour-spot');
  card = h('div.tour-card', { role: 'dialog', 'aria-label': 'Studio tour' });
  ov.append(spot, card);
  document.body.appendChild(ov);
  onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end(true); }
    else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); if (idx === STEPS.length - 1) end(true); else go(idx + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); go(idx - 1); }
    else e.stopPropagation();
  };
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', place);
  render();
}
function end(done) {
  if (!ov) return;
  window.removeEventListener('keydown', onKey, true);
  window.removeEventListener('resize', place);
  ov.classList.add('out');
  const o = ov; ov = null;
  setTimeout(() => o.remove(), 160);
  if (done) LS.set('tourDone', true);
}

function helpMenu(btn) {
  const wrap = btn.parentElement;
  const m = h('div.menu.right.tour-menu', { hidden: true },
    h('button', { on: { click: () => { m.hidden = true; startTour(0); } } }, 'Show me around', h('span.k', '10 steps')),
    h('button', { on: { click: () => { m.hidden = true; btn._keys && btn._keys(); } } }, 'Keyboard shortcuts', h('span.k', '?')));
  wrap.style.position = 'relative';
  wrap.appendChild(m);
  btn._keys = btn.onclick;
  btn.onclick = (e) => { e.stopPropagation(); document.querySelectorAll('.menu').forEach((x) => { if (x !== m) x.hidden = true; }); m.hidden = !m.hidden; };
  document.addEventListener('click', () => { m.hidden = true; });
  btn.title = 'Help: tour and shortcuts';
}

export function initTour() {
  const btn = document.getElementById('btn-help');
  if (btn) helpMenu(btn);
  window.__tour = { start: startTour, get index() { return idx; }, get open() { return !!ov; }, steps: STEPS.length };
  const headless = /HeadlessChrome/.test(navigator.userAgent) || navigator.webdriver;
  if (!LS.get('tourDone', false) && !headless) setTimeout(() => { if (!ov && S.project) startTour(0); }, 1400);
}
