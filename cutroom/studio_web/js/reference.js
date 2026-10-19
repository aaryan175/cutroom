// Reference workspace: the references list, the reference player, the cut strip (every shot, section bands,
// flashes, beats, captions), the breakdown, "Compare with my edit", side-by-side + onion skin against the
// program, "Match…" requests to the AI, and the optional ghost track under the timeline.
// Backend contract: docs/STUDIO.md "Reference engine" (/api/refs, /api/ref/<id>, /compare, SSE `ref`).
import { S, on, emit, setPlayhead, fps } from './state.js';
import { api } from './api.js';
import { h, icon, tc, tshort, dur, clamp, toast, LS, isTyping } from './util.js';
import { xicon, explain, openAddReference } from './importer.js';
import { programCtl } from './program.js';
import { _test as tlTest } from './timeline.js';

const SEC_COLOR = { 'cold open': '#3c74fb', talk: '#27767a', montage: '#82597e', 'voice-over b-roll': '#646393', 'b-roll': '#646393', outro: '#7a6733' };
const KIND_COL = { talk: '#2f8a8f', broll: '#6f6ea3', montage: '#93658e', title: '#8a7539', black: '#3a3a3f', flash: '#e8e8ec' };
const secColor = (n) => SEC_COLOR[n] || '#55555e';

const R = {
  open: false, list: [], cur: LS.get('refCur', null), data: null, loading: false,
  mode: 'single', lock: LS.get('refLock', true), offset: LS.get('refOffset', 0), align: LS.get('refAlign', 'time'),
  pps: 0, sel: null, tab: LS.get('refTab', 'breakdown'), ghost: LS.get('refGhost', false),
  onion: LS.get('refOnion', 'fade'), alpha: LS.get('refAlpha', 0.5), wipe: 0.5,
  cmp: null, cmpKey: null, cmpBusy: false,
};
let ws, hd, listEl, center, stage, refPane, progSlot, refFrame, refVideo, bar, stripWrap, stripScroll, stripInner, stripCv, side, sideBody;
let tcEl, durEl, shotEl, playBtn, selInfo, zoomInp, modeSeg, linkBox, onionBox, ghostBtn, titleEl, metaEl;
let progMon = null, progHome = null;

const an = () => (R.data && R.data.analysis) || null;
const row = () => R.list.find((r) => r.id === R.cur) || (R.data && R.data.id === R.cur ? R.data : null);
const refDur = () => (an() ? an().source.duration : (row() && row().duration) || refVideo.duration || 0);
const rfps = () => (an() && an().source.fps) || (row() && row().fps) || 25;
const linked = () => R.open && R.lock && R.mode !== 'single' && !!refVideo.src;

// ---------------------------------------------------------------- list
async function loadList(selectFirst = false) {
  try {
    const r = await api.refs();
    R.list = r.refs || [];
  } catch { R.list = []; }
  if ((!R.cur || !R.list.some((x) => x.id === R.cur)) && R.list.length && (selectFirst || !R.cur)) R.cur = R.list[0].id;
  drawList();
  if (R.cur && R.shown !== R.cur) select(R.cur);
  else if (!R.list.length) drawEmpty();
  drawGhost(true);
}
let listT = 0;
const reloadListSoon = () => { if (listT) return; listT = setTimeout(() => { listT = 0; loadList(); }, 700); };
function statusText(r) {
  if (r.status === 'analyzed') return `${dur(r.duration)} · ${(r.summary && r.summary.n_shots) || '–'} shots`;
  if (r.status === 'error') return 'could not be measured';
  if (r.status === 'downloading') return `downloading${r.progress ? ` ${Math.round(r.progress * 200)}%` : '…'}`;
  if (r.status === 'analyzing') return `measuring · ${r.stage || ''}`;
  return r.job ? 'measuring…' : 'not measured yet';
}
function drawList() {
  listEl.innerHTML = '';
  listEl.appendChild(h('div.rw-lh', h('span', 'References'), h('span.grow'),
    h('button.btn', { title: 'Add a reference: a link or a video file', on: { click: () => openAddReference() } }, h('span', { html: icon('plus', 12) }), 'Add reference')));
  if (!R.list.length) {
    listEl.appendChild(h('div.rw-lempty', h('span', { html: xicon('ref', 18) }), h('p', 'No references yet.'), h('p.sub', 'Drop a video anywhere, or paste a link.')));
    return;
  }
  const ul = h('div.rw-items');
  for (const r of R.list) {
    const busy = ['downloading', 'analyzing'].includes(r.status) || (r.job && r.status !== 'analyzed');
    const prog = r.status === 'downloading' ? (r.progress || 0) * 0.5 : r.progress || 0;
    ul.appendChild(h('button.rw-item' + (r.id === R.cur ? '.on' : '') + (r.status === 'error' ? '.err' : ''), { dataset: { id: r.id }, on: { click: () => select(r.id) } },
      h('div.rw-th', r.thumb ? h('img', { src: r.thumb, loading: 'lazy', alt: '' }) : h('span', { html: xicon('ref', 16) }),
        r.duration ? h('span.rw-dur', dur(r.duration)) : null),
      h('div.rw-tx', h('div.rw-t', { title: r.title }, r.title), h('div.rw-m', statusText(r)),
        busy ? h('div.bar', h('i', { style: { width: clamp(prog * 100, 2, 100) + '%' } })) : null)));
  }
  listEl.appendChild(ul);
}

// ---------------------------------------------------------------- select + load one
async function select(id) {
  if (!id) return;
  R.cur = id; LS.set('refCur', id);
  R.sel = null; R.cmp = null; R.cmpKey = null;
  listEl.querySelectorAll('.rw-item').forEach((b) => b.classList.toggle('on', b.dataset.id === id));
  R.loading = true;
  try { R.data = await api.ref(id); } catch (e) { R.data = null; toast(`Couldn't open that reference: ${e.message}`, 'error'); }
  R.loading = false;
  if (R.cur !== id) return;
  const r = R.data || row() || {};
  titleEl.textContent = r.title || id;
  metaEl.textContent = r.duration ? `${dur(r.duration)} · ${r.width}×${r.height} · ${(+r.fps || 0).toFixed(r.fps % 1 ? 2 : 0)} fps${r.origin === 'link' ? ' · from a link' : ''}` : '';
  if (r.proxy_url) { if (!refVideo.src.endsWith(r.proxy_url)) { refVideo.src = r.proxy_url; } }
  else refVideo.removeAttribute('src');
  center.classList.toggle('noref', !r.proxy_url);
  R.pps = 0;
  R.shown = id;
  buildStrip();
  drawSide();
  drawGhost(true);
  emit('context');
}

function drawEmpty() {
  R.data = null; R.shown = null;
  titleEl.textContent = 'Reference'; metaEl.textContent = '';
  refVideo.removeAttribute('src');
  stripInner.innerHTML = ''; drawStripCanvas();
  center.classList.add('noref');
  sideBody.innerHTML = '';
  sideBody.appendChild(h('div.rw-teach',
    h('h4', 'How references work'),
    h('ol', h('li', h('b', 'Give me a video'), ' whose style you like: drop the file here, or paste a YouTube / Instagram / TikTok link.'),
      h('li', h('b', 'I break it down'), ': every cut, the pace of each part, the music and beats, the captions, the colour.'),
      h('li', h('b', 'Compare'), ' it with your edit, side by side or overlaid, and ask me to match any part of it.'))));
}

// ---------------------------------------------------------------- player
function curShot(t) {
  const a = an();
  if (!a) return -1;
  const sh = a.shots;
  let lo = 0, hi = sh.length - 1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (t < sh[m].start) hi = m - 1; else if (t >= sh[m].end) lo = m + 1; else return m; }
  return -1;
}
function updTransport() {
  const t = refVideo.currentTime || 0;
  tcEl.textContent = tc(t, rfps());
  const i = curShot(t);
  shotEl.textContent = an() && i >= 0 ? `shot ${i + 1} / ${an().shots.length}` : '';
  stripDirty = true;
}
/** Seek the reference (and, when linked, the program to the same moment). */
function seekRef(t, { fromProgram = false } = {}) {
  t = clamp(t, 0, Math.max(0, refDur() - 0.001));
  try { refVideo.currentTime = t; } catch { /* not loaded yet */ }
  updTransport();
  if (!fromProgram && linked()) setPlayhead(progTarget(t), 'ref');
  revealStrip(t);
}
function refTarget(T) {
  const dP = (S.tl && S.tl.duration) || 1;
  return R.align === 'scale' ? (T / dP) * refDur() + R.offset : T + R.offset;
}
function progTarget(t) {
  const dP = (S.tl && S.tl.duration) || 1;
  return R.align === 'scale' ? ((t - R.offset) / Math.max(0.001, refDur())) * dP : t - R.offset;
}
function followProgram(from) {
  if (!linked() || from === 'ref') return;
  const t = clamp(refTarget(S.playhead), 0, refDur());
  if (S.playing) {
    const rate = R.align === 'scale' ? clamp(refDur() / Math.max(0.5, (S.tl && S.tl.duration) || 1), 0.25, 16) : 1;
    if (Math.abs(refVideo.playbackRate - rate) > 0.01) refVideo.playbackRate = rate;
    if (refVideo.paused) refVideo.play().catch(() => {});
    if (Math.abs(refVideo.currentTime - t) > (R.align === 'scale' ? 0.6 : 0.25)) refVideo.currentTime = t;
  } else {
    if (!refVideo.paused) refVideo.pause();
    if (Math.abs(refVideo.currentTime - t) > 0.5 / rfps()) refVideo.currentTime = t;
  }
  updTransport();
}
function togglePlay() {
  if (linked()) { programCtl.togglePlay(); return; }
  if (refVideo.paused) refVideo.play().catch(() => {}); else refVideo.pause();
}
function step(n) { refVideo.pause(); seekRef((refVideo.currentTime || 0) + n / rfps()); }

// ---------------------------------------------------------------- stage modes: single / side by side / onion skin
function setMode(mode) {
  if (mode === R.mode && (mode === 'single' || progMon)) return;
  R.mode = mode;
  modeSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  ws.classList.toggle('m-sbs', mode === 'sbs');
  ws.classList.toggle('m-onion', mode === 'onion');
  linkBox.hidden = mode === 'single';
  onionBox.hidden = mode !== 'onion';
  unOnion();
  refVideo.muted = mode !== 'single';            // your edit's sound plays; the reference is silent next to it
  if (mode === 'single') { returnProgram(); }
  else {
    borrowProgram();
    if (mode === 'onion') doOnion();
  }
  requestAnimationFrame(() => { fitRef(); emit('layout'); followProgram('mode'); });
}
function borrowProgram() {
  if (progMon) return;
  progHome = document.getElementById('program-body');
  progMon = progHome && progHome.querySelector(':scope > .mon');
  if (progMon) progSlot.appendChild(progMon);
}
function returnProgram() {
  unOnion();
  if (progMon && progHome) progHome.appendChild(progMon);
  progMon = null;
  requestAnimationFrame(() => emit('layout'));
}
let wipeEl = null;
function progFrame() { return progMon && progMon.querySelector('.mon-frame'); }
function doOnion() {
  const f = progFrame();
  if (!f) return;
  refVideo.classList.add('onion-ref');
  f.prepend(refVideo);
  f.classList.add('onion');
  f.classList.toggle('wipe', R.onion === 'wipe');
  f.style.setProperty('--onion-a', String(R.alpha));
  f.style.setProperty('--wipe', (R.wipe * 100).toFixed(2) + '%');
  if (!wipeEl) {
    wipeEl = h('div.wipe-h', h('span.wipe-k', { html: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M6 4 2.5 8 6 12M10 4l3.5 4L10 12"/></svg>' }));
    wipeEl.addEventListener('pointerdown', (e) => {
      e.preventDefault(); e.stopPropagation();
      wipeEl.setPointerCapture(e.pointerId);
      const fr = progFrame().getBoundingClientRect();
      const move = (ev) => { R.wipe = clamp((ev.clientX - fr.left) / fr.width, 0, 1); progFrame().style.setProperty('--wipe', (R.wipe * 100).toFixed(2) + '%'); };
      const up = () => { wipeEl.removeEventListener('pointermove', move); wipeEl.removeEventListener('pointerup', up); };
      wipeEl.addEventListener('pointermove', move); wipeEl.addEventListener('pointerup', up);
    });
  }
  wipeEl.hidden = R.onion !== 'wipe';
  f.appendChild(wipeEl);
  f.appendChild(onionLabels());
}
let onionLbl = null;
function onionLabels() {
  if (!onionLbl) onionLbl = h('div.onion-lbl', h('span.l', 'Reference'), h('span.r', 'Your edit'));
  onionLbl.classList.toggle('fade', R.onion !== 'wipe');
  onionLbl.querySelector('.l').textContent = R.onion === 'wipe' ? '◀ Reference' : 'Reference + your edit';
  onionLbl.querySelector('.r').hidden = R.onion !== 'wipe';
  onionLbl.querySelector('.r').textContent = 'Your edit ▶';
  return onionLbl;
}
function unOnion() {
  const f = document.querySelector('.mon-frame.onion');
  if (f) { f.classList.remove('onion', 'wipe'); }
  if (wipeEl) wipeEl.remove();
  if (onionLbl) onionLbl.remove();
  if (refVideo && refVideo.parentNode !== refFrame) { refVideo.classList.remove('onion-ref'); refFrame.prepend(refVideo); }
}
function fitRef() {
  const r = refPane.getBoundingClientRect();
  const ar = refVideo.videoWidth && refVideo.videoHeight ? refVideo.videoWidth / refVideo.videoHeight : (row() && row().width ? row().width / row().height : 16 / 9);
  let w = r.width - 16, hh = w / ar;
  if (hh > r.height - 16) { hh = r.height - 16; w = hh * ar; }
  refFrame.style.width = Math.max(10, Math.floor(w)) + 'px'; refFrame.style.height = Math.max(10, Math.floor(hh)) + 'px';
}

// ---------------------------------------------------------------- cut strip
const ROW = { band: 18, tiles: 56, caps: 16, beats: 12 };
let stripDirty = true;
function ppsFit() { return Math.max(0.2, (stripScroll.clientWidth - 2) / Math.max(1, refDur())); }
function defaultPps() {
  const a = an();
  const med = a ? a.pacing.median_shot || a.pacing.asl || 2 : 2;
  return clamp(26 / Math.max(0.2, med), 1.5, 60);
}
const PMIN = 0.2, PMAX = 160;
const ppsToSlider = (p) => Math.round(Math.log(p / PMIN) / Math.log(PMAX / PMIN) * 1000);
const sliderToPps = (v) => PMIN * Math.pow(PMAX / PMIN, v / 1000);
function buildStrip() {
  stripInner.innerHTML = '';
  const a = an();
  const D = refDur();
  if (!a || !D) {
    const r = row() || {};
    const msg = r.status === 'error' ? 'This reference could not be measured.' : ['downloading', 'analyzing'].includes(r.status) || r.job ? 'Breaking it down… the cut strip appears when it\'s measured.' : 'Not measured yet.';
    stripInner.style.width = '100%';
    stripInner.appendChild(h('div.cs-wait', msg));
    selInfo.textContent = '';
    drawStripCanvas();
    return;
  }
  if (!R.pps) R.pps = defaultPps();
  zoomInp.value = ppsToSlider(R.pps);
  const P = R.pps;
  stripInner.style.width = Math.ceil(D * P + 2) + 'px';
  const bands = h('div.cs-bands');
  for (const s of a.sections) {
    bands.appendChild(h('div.cs-band', { style: { left: s.start * P + 'px', width: Math.max(1, (s.end - s.start) * P - 1) + 'px', '--c': secColor(s.name) }, title: `${s.name} · ${tshort(s.start)}–${tshort(s.end)} · avg shot ${(+s.asl).toFixed(1)}s`, dataset: { sec: s.i } },
      h('span', s.name)));
  }
  const tiles = h('div.cs-tiles');
  const caps = h('div.cs-caps');
  const thumbs = R.data.thumbs || [];
  for (const s of a.shots) {
    const w = Math.max(1, s.dur * P - 1);
    const kind = s.flash ? 'flash' : s.kind;
    const t = h('div.cs-tile.k-' + kind + (s.montage ? '.mont' : ''), { style: { left: s.start * P + 'px', width: w + 'px', '--kc': KIND_COL[kind] || '#555' }, dataset: { i: s.i },
      title: `Shot ${s.i + 1} · ${kind}${s.montage ? ' (montage)' : ''} · ${s.dur.toFixed(2)}s (${s.frames} frames) at ${tshort(s.start)}${s.beat ? ' · on the beat' : ''}${s.text && s.text.present && s.text.words ? `\n“${s.text.words}”` : ''}` });
    if (w >= 16 && thumbs[s.i]) t.appendChild(h('img', { src: thumbs[s.i], loading: 'lazy', alt: '', draggable: false }));
    tiles.appendChild(t);
    if (s.text && s.text.present && w >= 6) {
      const words = String(s.text.words || '').trim();
      caps.appendChild(h('div.cs-cap', { style: { left: s.start * P + 'px', maxWidth: Math.max(8, w) + 'px' }, title: words ? `On screen: “${words}”` : 'Text on screen' }, w > 34 && words ? words : 'Aa'));
    }
  }
  stripInner.append(bands, tiles, caps, h('div.cs-beats-row'));
  drawSel();
  drawStripCanvas();
}
function zoomStrip(pps, anchorT) {
  const D = refDur();
  if (!D) return;
  const ax = anchorT === undefined ? stripScroll.clientWidth / 2 : anchorT * R.pps - stripScroll.scrollLeft;
  const t = anchorT === undefined ? (stripScroll.scrollLeft + ax) / R.pps : anchorT;
  R.pps = clamp(pps, Math.min(PMIN, ppsFit()), PMAX);
  buildStrip();
  stripScroll.scrollLeft = Math.max(0, t * R.pps - ax);
}
function revealStrip(t) {
  if (!an() || !R.pps) return;
  const x = t * R.pps, l = stripScroll.scrollLeft, w = stripScroll.clientWidth;
  if (x < l + 20 || x > l + w - 40) stripScroll.scrollLeft = Math.max(0, x - w * 0.3);
}
function xToT(clientX) { const r = stripScroll.getBoundingClientRect(); return (clientX - r.left + stripScroll.scrollLeft) / R.pps; }
function drawSel() {
  const a = an();
  if (!a || !R.sel) { selInfo.innerHTML = ''; if (a) selInfo.append(h('span.dim', 'Shift-drag on the strip to pick a part, then use Match…')); return; }
  const { t0, t1 } = R.sel;
  const shots = a.shots.filter((s) => s.end > t0 && s.start < t1);
  selInfo.innerHTML = '';
  selInfo.append(h('span.sel-pill', `${tshort(t0)}–${tshort(t1)} · ${shots.length} shot${shots.length === 1 ? '' : 's'}`),
    h('button.ib.sm', { title: 'Clear the selection', html: icon('x', 11), on: { click: () => { R.sel = null; drawSel(); stripDirty = true; } } }));
}
function drawStripCanvas() {
  if (!stripCv) return;
  const W = stripScroll.clientWidth, H = stripScroll.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  if (stripCv.width !== Math.round(W * dpr) || stripCv.height !== Math.round(H * dpr)) { stripCv.width = Math.round(W * dpr); stripCv.height = Math.round(H * dpr); stripCv.style.width = W + 'px'; stripCv.style.height = H + 'px'; }
  const c = stripCv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  const a = an();
  if (!a || !R.pps) return;
  const L = stripScroll.scrollLeft, P = R.pps;
  const x = (t) => t * P - L;
  const yT = ROW.band, yC = ROW.band + ROW.tiles, yB = yC + ROW.caps;
  // beats (+ downbeats taller)
  const beats = (a.audio && a.audio.beats) || [];
  const down = new Set(((a.audio && a.audio.downbeats) || []).map((b) => b.toFixed(2)));
  const minGap = 3 / P;
  let lastB = -1;
  c.fillStyle = 'rgba(122,170,255,.5)';
  for (const b of beats) {
    const xb = x(b);
    if (xb < -2 || xb > W + 2 || b - lastB < minGap) continue;
    lastB = b;
    const big = down.has(b.toFixed(2));
    c.fillRect(Math.round(xb), yB + (big ? 1 : 4), 1, big ? ROW.beats - 2 : ROW.beats - 5);
  }
  // flashes
  c.fillStyle = '#fff';
  for (const f of a.flashes || []) {
    const xf = x(f.t);
    if (xf < -4 || xf > W + 4) continue;
    c.beginPath(); c.moveTo(xf - 3.5, yT); c.lineTo(xf + 3.5, yT); c.lineTo(xf, yT + 5); c.closePath(); c.fill();
  }
  // selection
  if (R.sel) {
    const x0 = x(R.sel.t0), x1 = x(R.sel.t1);
    c.fillStyle = 'rgba(60,116,251,.18)'; c.fillRect(x0, 0, x1 - x0, H);
    c.fillStyle = 'rgba(107,151,255,.9)'; c.fillRect(Math.round(x0), 0, 1.5, H); c.fillRect(Math.round(x1) - 1, 0, 1.5, H);
  }
  // playhead
  const xp = x(refVideo.currentTime || 0);
  if (xp >= -1 && xp <= W + 1) {
    c.fillStyle = '#6b97ff'; c.fillRect(Math.round(xp) - 0.5, 0, 2, H);
    c.beginPath(); c.moveTo(xp - 5, 0); c.lineTo(xp + 6, 0); c.lineTo(xp + 0.5, 6); c.closePath(); c.fill();
  }
  // current shot outline
  const i = curShot(refVideo.currentTime || 0);
  if (i >= 0) {
    const s = a.shots[i];
    c.strokeStyle = 'rgba(255,255,255,.85)'; c.lineWidth = 1.5;
    c.strokeRect(x(s.start) + 0.75, yT + 0.75, Math.max(1, s.dur * P - 2.5), ROW.tiles - 1.5);
  }
}
function initStripEvents() {
  let down = null;
  stripScroll.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !an()) return;
    const t = xToT(e.clientX);
    down = { x: e.clientX, sl: stripScroll.scrollLeft, t, shift: e.shiftKey, moved: false, tile: e.target.closest('.cs-tile') };
    stripScroll.setPointerCapture(e.pointerId);
    if (e.shiftKey) { R.sel = { t0: t, t1: t }; stripDirty = true; }
  });
  stripScroll.addEventListener('pointermove', (e) => {
    if (!down) return;
    if (Math.abs(e.clientX - down.x) > 3) down.moved = true;
    if (down.shift) {
      const t = clamp(xToT(e.clientX), 0, refDur());
      R.sel = { t0: Math.min(down.t, t), t1: Math.max(down.t, t) };
      stripDirty = true; drawSel();
    } else if (down.moved) stripScroll.scrollLeft = down.sl - (e.clientX - down.x);
  });
  const up = (e) => {
    if (!down) return;
    const d = down; down = null;
    if (d.shift) {
      if (R.sel && R.sel.t1 - R.sel.t0 < 0.05) {     // a shift-click selects the shot under the pointer
        const s = an().shots[curShotAt(d.t)];
        R.sel = s ? { t0: s.start, t1: s.end } : null;
      }
      drawSel(); stripDirty = true; return;
    }
    if (d.moved) return;
    if (d.tile) { const s = an().shots[+d.tile.dataset.i]; refVideo.pause(); seekRef(s.start + Math.min(0.02, s.dur / 2)); }
    else seekRef(xToT(e.clientX));
  };
  stripScroll.addEventListener('pointerup', up);
  stripScroll.addEventListener('pointercancel', () => { down = null; });
  stripScroll.addEventListener('wheel', (e) => {
    if (!an()) return;
    if (e.ctrlKey || e.metaKey) { e.preventDefault(); zoomStrip(R.pps * Math.exp(-e.deltaY * 0.004), xToT(e.clientX)); }
    else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); stripScroll.scrollLeft += e.deltaY; }
  }, { passive: false });
  stripScroll.addEventListener('scroll', () => { stripDirty = true; });
  stripScroll.addEventListener('dblclick', (e) => {      // double-click a band = select that section
    const b = e.target.closest('.cs-band');
    if (!b || !an()) return;
    const s = an().sections[+b.dataset.sec];
    R.sel = { t0: s.start, t1: s.end }; drawSel(); stripDirty = true;
  });
}
const curShotAt = (t) => { const i = curShot(t); return i < 0 ? 0 : i; };

// ---------------------------------------------------------------- breakdown + compare (side panel)
function stat(label, value, sub) { return h('div.st', h('span.st-l', label), h('b.st-v', value), sub ? h('span.st-s', sub) : null); }
function mixBar(parts) {
  const total = parts.reduce((a, p) => a + p[1], 0) || 1;
  return h('div.mix',
    h('div.mix-bar', parts.filter((p) => p[1] > 0).map(([n, v, c]) => h('i', { style: { width: (v / total * 100).toFixed(2) + '%', background: c }, title: `${n} ${v.toFixed(0)}%` }))),
    h('div.mix-lg', parts.map(([n, v, c]) => h('span', h('i', { style: { background: c } }), n, h('b', `${Math.round(v)}%`)))));
}
function captionSwatch(cap, sample) {
  const fr = h('div.cap-sw');
  if (!cap || !cap.detected) { fr.appendChild(h('span.cap-none', 'No captions or on-screen text found')); return fr; }
  const yc = cap.y_center ?? (cap.position === 'top' ? 0.12 : cap.position === 'middle' ? 0.5 : cap.position === 'lower' ? 0.75 : 0.9);
  const el = h('span.cap-tx', { style: { top: (yc * 100).toFixed(1) + '%', fontSize: `max(8px, ${((cap.size_pct || 5) * 0.5625).toFixed(2)}cqw)`, color: cap.color || '#fff' } }, sample || 'Your caption here');
  if (cap.outline) el.classList.add('ol');
  fr.appendChild(el);
  return fr;
}
function gradeWords(c) {
  if (!c) return '';
  const w = [];
  w.push(c.luma > 125 ? 'bright' : c.luma < 80 ? 'dark' : 'mid');
  w.push(c.warmth > 12 ? 'warm' : c.warmth < -12 ? 'cool' : 'neutral');
  w.push(c.sat > 110 ? 'saturated' : c.sat < 60 ? 'muted' : 'natural colour');
  w.push(c.contrast > 65 ? 'punchy' : c.contrast < 45 ? 'soft' : '');
  return w.filter(Boolean).join(' · ');
}
function gradeChip(c) {          // a rough colour from the section's mean luma / warmth / saturation
  const L = clamp(c.luma, 10, 245), wm = clamp(c.warmth, -60, 60), s = clamp(c.sat / 255, 0, 1);
  const r = clamp(L + wm * 0.6 * s * 2, 0, 255), b = clamp(L - wm * 0.6 * s * 2, 0, 255), g = clamp(L + (r + b) / 2 * 0 - Math.abs(wm) * 0.1, 0, 255);
  return `rgb(${r | 0},${g | 0},${b | 0})`;
}
function sectionColours(a, s) {
  const acc = new Map();
  for (const sh of a.shots) {
    if (sh.end <= s.start || sh.start >= s.end) continue;
    for (const c of sh.colors || []) acc.set(c.hex, (acc.get(c.hex) || 0) + c.pct * sh.dur);
  }
  return [...acc.entries()].sort((x, y) => y[1] - x[1]).slice(0, 4).map((x) => x[0]);
}
function drawSide() {
  side.querySelectorAll('.rw-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === R.tab));
  sideBody.innerHTML = '';
  const r = R.data || row();
  if (!r) { drawEmpty(); return; }
  const a = an();
  if (!a) {
    const busy = ['downloading', 'analyzing'].includes(r.status) || r.job;
    sideBody.appendChild(h('div.rw-state',
      r.status === 'error' ? h('div.rw-err', h('b', 'This one couldn\'t be measured.'), h('p', String(r.error || '').split('\n').slice(-2).join(' ')),
        h('button.btn', { on: { click: () => analyze(true) } }, 'Try again'))
        : busy ? h('div', h('b', 'Breaking it down…'), h('p', `${r.stage || 'starting'} · cuts, pace, music, captions and colour.`), h('div.bar', h('i', { style: { width: clamp((r.progress || 0) * 100, 3, 100) + '%' } })))
          : h('div', h('b', 'Not measured yet.'), h('p', 'Measuring finds every cut, the pace, the music and beats, captions and colour. It takes a few minutes and runs in the background.'),
            h('button.btn.primary', { on: { click: () => analyze(false) } }, 'Break it down'))));
    return;
  }
  if (R.tab === 'compare') return drawCompare();
  const st = a.structure || {}, p = a.pacing || {}, au = a.audio || {}, cap = a.captions || {};
  const big = [
    stat('Shots', String(p.n_shots), `${p.shots_under_0_5s || 0} under ½ s`),
    stat('Average shot', `${(+p.asl).toFixed(1)}s`, `median ${(+p.median_shot).toFixed(1)}s`),
    stat('First 3 s', `${p.first3_cuts ?? '–'} cuts`, p.first3_cuts >= 4 ? 'fast hook' : p.first3_cuts >= 2 ? 'steady open' : 'slow open'),
    stat('Cold open', st.cold_open_s ? `${(+st.cold_open_s).toFixed(1)}s` : '–', st.time_to_first_word_s !== undefined && st.time_to_first_word_s !== null ? `talks at ${(+st.time_to_first_word_s).toFixed(1)}s` : ''),
    stat('Tempo', au.tempo ? `${Math.round(au.tempo)} BPM` : '–', au.music_pct !== undefined ? `music ${Math.round(au.music_pct)}%` : ''),
    stat('Loudness', au.integrated_lufs !== null && au.integrated_lufs !== undefined ? `${(+au.integrated_lufs).toFixed(1)} LUFS` : '–', au.true_peak_dbtp !== undefined && au.true_peak_dbtp !== null ? `peak ${(+au.true_peak_dbtp).toFixed(1)} dBTP` : ''),
  ];
  const secs = a.sections.map((s) => {
    const cols = sectionColours(a, s);
    return h('button.sec-row.two', { title: `Select this part on the strip\ngrade: ${gradeWords(s)}`, on: { click: () => { R.sel = { t0: s.start, t1: s.end }; drawSel(); stripDirty = true; seekRef(s.start + 0.01); } } },
      h('i.sec-dot', { style: { background: secColor(s.name) } }),
      h('span.sec-n', s.name), h('span.sec-a', `${(+s.asl).toFixed(1)}s`, h('span.u', ' avg shot')),
      h('span.sec-t', `${tshort(s.start)} – ${tshort(s.end)} · ${s.n_shots} shot${s.n_shots === 1 ? '' : 's'}`),
      h('span.sec-sw', h('i.gr', { style: { background: gradeChip(s) }, title: `grade: ${gradeWords(s)}` }), cols.map((c) => h('i', { style: { background: c }, title: c }))));
  });
  const sample = (cap.samples && cap.samples[0] && cap.samples[0].text) || '';
  const beatPct = au.beat_sync_pct;
  sideBody.append(
    h('div.bk-grid', big),
    h('h5', 'What it\'s made of'),
    mixBar([['talking', +st.talk_pct || 0, KIND_COL.talk], ['b-roll', +st.broll_pct || 0, KIND_COL.broll], ['montage', +st.montage_pct || 0, KIND_COL.montage], ['other', +st.other_pct || 0, '#4a4a52']]),
    beatPct !== undefined && beatPct !== null ? h('div.beat',
      h('h5', 'Cuts on the beat', h('span.grow'), h('b', `${Math.round(beatPct)}%`)),
      h('div.beat-bar', h('i', { style: { width: clamp(beatPct, 0, 100) + '%' } }), au.chance_pct ? h('b', { style: { left: clamp(au.chance_pct, 0, 100) + '%' }, title: `${Math.round(au.chance_pct)}% would land on a beat by chance` }) : null),
      h('p.sub', beatPct > (au.chance_pct || 0) + 15 ? 'Cut to the music on purpose.' : 'Cuts mostly follow the talking, not the music.')) : null,
    h('h5', 'Captions', h('span.grow'), h('span.sub', cap.detected ? `${cap.position} · ~${cap.approx_plan_size_1080 || '?'} px at 1080p` : '')),
    captionSwatch(cap, sample),
    cap.detected ? h('p.sub', `${Math.round(cap.words_per_caption || 0)} words at a time · ${cap.outline ? 'outlined' : 'no outline'} · colour ${cap.color} · on screen ${Math.round(cap.coverage_pct || 0)}% of the time`) : null,
    h('h5', 'Parts and colour', h('span.grow'), h('span.sub', gradeWords(a.colour && a.colour.overall))),
    h('div.secs', secs),
    h('details.readout', h('summary', 'The full read-out'), h('ul', (a.summary || []).map((l) => h('li', l)))));
}
async function analyze(force) {
  try { await api.analyzeRef(R.cur, force); toast('Measuring it now — this runs in the background.', 'ok', 2500); reloadListSoon(); }
  catch (e) { toast(`Couldn't start: ${e.message}`, 'error'); }
}

// ---------------------------------------------------------------- compare with my edit
const VERDICT = { close: ['Close to the reference', 'ok'], near: ['Getting there', 'warn'], far: ['Far from the reference', 'bad'] };
async function runCompare(force = false) {
  if (!S.plan) { R.cmp = { error: 'Open a plan first: the comparison is against your current edit.' }; drawSide(); return; }
  const key = `${R.cur}|${S.plan.slug}`;
  if (!force && R.cmpKey === key && R.cmp) { drawSide(); return; }
  R.cmpBusy = true; R.cmpKey = key; R.cmp = null; drawSide();
  try { R.cmp = await api.refCompare(R.cur, S.plan.slug); }
  catch (e) { R.cmp = { error: e.message }; }
  R.cmpBusy = false;
  if (R.cmpKey === key && R.tab === 'compare') drawSide();
}
function fmtVal(v, unit) {
  if (v === null || v === undefined) return '–';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v !== 'number') return String(v);
  const n = Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2).replace(/0$/, '');
  return unit ? `${n}${unit === '%' ? '%' : ' ' + unit}` : n;
}
function seekProgram(t) {
  if (R.mode === 'single') setMode('sbs');
  setPlayhead(t, 'force');
  emit('revealPlayhead');
}
function drawCurve(cv, curve) {
  const W = cv.clientWidth || 300, H = cv.clientHeight || 110, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  const ref = curve.ref || [], yours = curve.yours || [];
  const n = Math.max(ref.length, yours.length);
  if (!n) return;
  const max = Math.max(0.5, ...ref, ...yours) * 1.15;
  const pad = { l: 24, r: 6, t: 8, b: 16 };
  const X = (i) => pad.l + (i / Math.max(1, n - 1)) * (W - pad.l - pad.r);
  const Y = (v) => H - pad.b - (v / max) * (H - pad.t - pad.b);
  c.strokeStyle = 'rgba(255,255,255,.06)'; c.lineWidth = 1;
  c.fillStyle = 'rgba(161,161,170,.8)'; c.font = '10px ui-monospace, SF Mono, Menlo, monospace';
  for (const g of [0, max / 2, max]) { c.beginPath(); c.moveTo(pad.l, Y(g)); c.lineTo(W - pad.r, Y(g)); c.stroke(); c.fillText(g.toFixed(1), 2, Y(g) + 3); }
  c.fillText('start', pad.l, H - 3); c.textAlign = 'right'; c.fillText('end', W - pad.r, H - 3); c.textAlign = 'left';
  const line = (arr, col, fill) => {
    if (!arr.length) return;
    c.beginPath();
    arr.forEach((v, i) => (i ? c.lineTo(X(i), Y(v)) : c.moveTo(X(i), Y(v))));
    c.strokeStyle = col; c.lineWidth = 1.75; c.lineJoin = 'round'; c.stroke();
    if (fill) { c.lineTo(X(arr.length - 1), Y(0)); c.lineTo(X(0), Y(0)); c.closePath(); c.fillStyle = fill; c.fill(); }
  };
  line(ref, 'rgba(236,236,239,.75)', 'rgba(236,236,239,.06)');
  line(yours, '#6b97ff', 'rgba(60,116,251,.12)');
}
function drawCompare() {
  const plan = S.plan ? ((S.plan.draft && S.plan.draft.title) || S.plan.slug) : null;
  const head = h('div.cmp-hd', h('div', h('div.sub', 'Your edit'), h('b', plan || 'No plan open')), h('span.grow'),
    h('button.btn', { disabled: R.cmpBusy || !S.plan, on: { click: () => runCompare(true) } }, h('span', { html: icon('sync', 12) }), R.cmp ? 'Refresh' : 'Compare'));
  sideBody.appendChild(head);
  if (R.cmpBusy) { sideBody.appendChild(h('div.imp-wait', h('span.spin'), 'Measuring your edit against the reference… the first time a render is measured this takes up to a minute.')); return; }
  const c = R.cmp;
  if (!c) { sideBody.appendChild(h('p.sub', 'See how your edit differs: pace, structure, captions, loudness — in plain words, with links to the moments.')); return; }
  if (c.error) { sideBody.appendChild(h('div.rw-err', h('p', c.error))); return; }
  const [vt, vc] = VERDICT[c.verdict] || [c.verdict, ''];
  sideBody.appendChild(h('div.verdict.' + vc, h('i.vd'), h('div', h('b', vt), h('span', `${c.n_big || 0} big gap${c.n_big === 1 ? '' : 's'} · vs ${c.render ? 'your latest render' : 'your draft timeline'}`))));
  if (c.mode === 'timeline') sideBody.appendChild(h('p.sub.note', 'Your draft isn\'t rendered yet, so only pace and structure are compared. Render a preview to compare captions, loudness and picture too.'));
  const cv = h('canvas.curve');
  sideBody.append(h('h5', 'Pace over time', h('span.grow'), h('span.lg', h('i.r'), 'reference'), h('span.lg', h('i.y'), 'yours')), cv,
    c.curve && c.curve.corr !== null && c.curve.corr !== undefined ? h('p.sub', `Cuts per second from start to end · the shapes are ${c.curve.corr > 0.6 ? 'similar' : c.curve.corr > 0.3 ? 'partly alike' : 'different'} (r ${(+c.curve.corr).toFixed(2)})`) : null);
  requestAnimationFrame(() => drawCurve(cv, c.curve || {}));
  const gaps = c.gaps || [];
  sideBody.appendChild(h('h5', 'What to change', h('span.grow'), h('span.sub', gaps.length ? `${gaps.length}` : '')));
  const gl = h('div.gaps');
  if (!gaps.length) gl.appendChild(h('p.sub', 'Nothing big — it already moves like the reference.'));
  for (const g of gaps) {
    gl.appendChild(h('div.gap.' + g.severity, h('i.sevd'), h('span.gt', g.text),
      g.t !== null && g.t !== undefined ? h('button.xt.jump', { title: 'Jump there in your edit', on: { click: () => seekProgram(+g.t) } }, h('span', { html: icon('play', 9) }), tshort(+g.t)) : null));
  }
  sideBody.appendChild(gl);
  const tb = h('table.mt', h('thead', h('tr', h('th', ''), h('th', 'Reference'), h('th', 'Yours'), h('th', 'Gap'))),
    h('tbody', (c.rows || []).map((r) => h('tr', h('td', r.label || r.metric), h('td', fmtVal(r.ref, r.unit)), h('td', fmtVal(r.yours, r.unit)),
      h('td.d', r.delta === null || r.delta === undefined ? '' : (typeof r.delta === 'number' ? (r.delta > 0 ? '+' : '') + fmtVal(r.delta, r.unit) : String(r.delta)))))));
  sideBody.append(h('h5', 'The numbers'), h('div.mt-wrap', tb));
  if ((c.sections || []).length) {
    sideBody.append(h('h5', 'Part by part', h('span.grow'), h('span.sub', 'average shot')), h('div.secs.cmp',
      c.sections.map((s) => h('div.sec-row', h('i.sec-dot', { style: { background: secColor(s.name) } }), h('span.sec-n', s.name),
        h('span.sec-a', `${fmtVal(s.ref_asl, 's')} → `, h('b', fmtVal(s.yours_asl, 's'))),
        s.yours_start !== null && s.yours_start !== undefined ? h('button.xt.jump', { title: 'Jump there in your edit', on: { click: () => seekProgram(+s.yours_start) } }, h('span', { html: icon('play', 9) }), tshort(+s.yours_start)) : null))));
  }
}

// ---------------------------------------------------------------- Match… (composes a chat message)
function rangeFor(kind) {
  const a = an();
  if (!a) return null;
  if (R.sel) return { ...R.sel, label: 'this part' };
  if (kind === 'intro') { const s = a.sections[0]; return { t0: s.start, t1: s.end, label: `its ${s.name}` }; }
  if (kind === 'pacing') { const t = refVideo.currentTime || 0; const s = a.sections.find((x) => t >= x.start && t < x.end) || a.sections[0]; return { t0: s.start, t1: s.end, label: `its ${s.name} part` }; }
  return null;
}
export function matchMessage(kind) {
  const r = row() || R.data || {};
  const a = an();
  const rg = rangeFor(kind);
  const shots = rg && a ? a.shots.filter((s) => s.end > rg.t0 && s.start < rg.t1) : [];
  const where = rg ? `${tshort(rg.t0)}–${tshort(rg.t1)}${shots.length ? ` (shots ${shots[0].i + 1}–${shots[shots.length - 1].i + 1})` : ''}` : 'the whole video';
  const head = {
    intro: `Make my intro like this reference's intro${R.sel ? ' (the part I selected)' : ''}.`,
    pacing: `Match the pacing of ${rg ? rg.label : 'this part'} of the reference in my edit${S.tl ? ` around ${tshort(S.playhead)}` : ''}.`,
    captions: 'Match the reference\'s caption style (size, position, colour, outline, words at a time).',
    colour: 'Match the reference\'s colour and look.',
  }[kind];
  return `${head}\nReference: “${r.title || R.cur}” (ref ${R.cur}), ${where}.`;
}
function matchMenu(btn) {
  const m = h('div.menu.match-menu', { on: { click: (e) => e.stopPropagation() } },
    h('div.mh', 'Ask the AI to match the reference', h('span.grow'), R.sel ? h('span.k', `selected ${tshort(R.sel.t0)}–${tshort(R.sel.t1)}`) : null),
    ...[['intro', 'Make my intro like this reference\'s intro'], ['pacing', 'Match pacing for this section'], ['captions', 'Match caption style'], ['colour', 'Match colour / look']]
      .map(([k, label]) => h('button', { dataset: { match: k }, on: { click: () => { m.remove(); sendMatch(k); } } }, label)),
    h('hr'), h('div.mi-note', R.sel ? 'Uses the part you selected on the strip.' : 'Tip: shift-drag on the strip to say "this part".'));
  document.body.appendChild(m);
  const r = btn.getBoundingClientRect();
  m.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
  m.style.bottom = (window.innerHeight - r.top + 6) + 'px';
  const close = () => { m.remove(); document.removeEventListener('click', close); };
  setTimeout(() => document.addEventListener('click', close), 0);
}
function sendMatch(kind) {
  if (!an()) { toast('Wait until the reference is measured.', 'warn'); return; }
  emit('sendChat', matchMessage(kind));
  emit('focusChat');
}

// ---------------------------------------------------------------- reference frame -> chat (R)
async function grabRef() {
  if (!R.cur) {                 // not measured yet: fall back to a plain file in renders/reference/
    const f = ((S.project && S.project.references) || [])[0];
    if (!f) { toast('Add a reference first', 'warn'); return; }
    try {
      const g = await api.grab({ source: 'ref', file: f.file, t: 0 });
      emit('attach', { url: g.url, label: `match this · ${f.file}`, source: 'ref', t: 0, match: true });
    } catch (e) { toast(`Reference grab failed: ${e.message}`, 'error'); }
    return;
  }
  try {
    const t = +(refVideo.currentTime || 0).toFixed(3);
    const g = await api.grab({ source: 'ref', ref: R.cur, t });
    emit('attach', { url: g.url, label: `match this · ${(row() || {}).title || R.cur} @ ${tshort(t)}`, source: 'ref', t, match: true, ref: R.cur });
    toast('Reference frame attached to chat', 'ok', 1500);
  } catch (e) { toast(`Reference grab failed: ${e.message}`, 'error'); }
}
export async function markReference() {
  if (!R.open) await openWs();
  if (!R.cur) { await loadList(true); }
  return grabRef();
}

// ---------------------------------------------------------------- ghost track under the timeline
let ghostCv = null, ghostSig = '';
function drawGhost(force) {
  if (!ghostCv) return;
  const on_ = R.ghost && !!an();
  ghostCv.hidden = !on_;
  ghostBtn && ghostBtn.classList.toggle('on', R.ghost);
  if (!on_) return;
  const view = tlTest.view;
  const W = ghostCv.clientWidth, H = ghostCv.clientHeight, dpr = window.devicePixelRatio || 1;
  const sig = `${view.pps}|${view.x0}|${W}|${R.cur}`;
  if (!force && sig === ghostSig) return;
  ghostSig = sig;
  ghostCv.width = Math.round(W * dpr); ghostCv.height = Math.round(H * dpr);
  const c = ghostCv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  c.fillStyle = 'rgba(8,8,10,.82)'; c.fillRect(0, 0, W, H);
  const a = an();
  const x = (t) => (t - view.x0) * view.pps;
  for (const s of a.sections) { const x0 = x(s.start), x1 = x(s.end); if (x1 < 0 || x0 > W) continue; c.fillStyle = secColor(s.name) + '40'; c.fillRect(x0, 0, x1 - x0, 3); }
  for (const s of a.shots) {
    const x0 = x(s.start), x1 = x(s.end);
    if (x1 < 0) continue;
    if (x0 > W) break;
    const kind = s.flash ? 'flash' : s.kind;
    c.globalAlpha = s.flash ? 0.9 : 0.62;
    c.fillStyle = KIND_COL[kind] || '#55555e';
    c.fillRect(x0, 4, Math.max(1, x1 - x0 - (x1 - x0 > 3 ? 1 : 0)), H - 6);
  }
  c.globalAlpha = 1;
  const lbl = `REF · ${(row() || {}).title || ''}`.slice(0, 40);
  c.font = '600 9.5px Inter, -apple-system, system-ui, sans-serif';
  const tw = c.measureText(lbl).width + 12;
  c.fillStyle = 'rgba(17,17,19,.86)'; c.fillRect(W - tw - 4, 3, tw, H - 5);
  c.fillStyle = '#d4d4d8'; c.fillText(lbl, W - tw + 2, H / 2 + 3.5);
}
function initGhost() {
  const wrap = document.querySelector('#p-timeline .tl-canvas-wrap');
  if (!wrap) return;
  ghostCv = h('canvas.ref-ghost', { hidden: true, title: 'Reference shots, aligned at 0 (Reference → Show on timeline)' });
  wrap.appendChild(ghostCv);
}

// ---------------------------------------------------------------- open / close
async function openWs() {
  R.open = true;
  ws.hidden = false;
  document.querySelectorAll('#p-program .ptab[data-ptab]').forEach((b) => b.classList.toggle('on', b.dataset.ptab === 'reference'));
  await loadList(!R.cur);
  requestAnimationFrame(() => { fitRef(); stripDirty = true; });
  emit('context');
}
function closeWs() {
  if (!R.open) return;
  R.open = false;
  if (R.mode !== 'single') setMode('single');
  refVideo.pause();
  ws.hidden = true;
  document.querySelectorAll('#p-program .ptab[data-ptab]').forEach((b) => b.classList.toggle('on', b.dataset.ptab === 'program'));
  emit('context');
}
export function toggleReference(onOff) { (onOff === undefined ? !R.open : onOff) ? openWs() : closeWs(); }
export function referenceContext() { return R.open && R.cur ? { ref: R.cur, t: +(refVideo.currentTime || 0).toFixed(3), selection: R.sel } : null; }

// ---------------------------------------------------------------- build
function build() {
  const col = document.getElementById('col-left');
  refVideo = h('video', { preload: 'auto', playsInline: true });
  refFrame = h('div.mon-frame.rw-frame', refVideo);
  refPane = h('div.rw-pane.rw-refpane', h('span.mon-label', 'Reference'), refFrame,
    h('div.rw-noref', h('span', { html: xicon('ref', 22) }), h('h4', 'Drop a video here or paste a link'),
      h('p', 'I\'ll break it down: every cut, pace, music, captions, colour.'),
      h('div.acts', h('button.btn.primary', { on: { click: () => openAddReference() } }, h('span', { html: icon('plus', 12) }), 'Add reference'),
        h('button.btn', { on: { click: () => emit('importPick', 'reference') } }, 'Choose a file'))));
  progSlot = h('div.rw-pane.rw-progslot');
  stage = h('div.rw-stage', refPane, progSlot);

  tcEl = h('span.tc', '00:00:00:00'); durEl = h('span.tc.dim', ''); shotEl = h('span.rw-shot');
  playBtn = h('button.ib', { title: 'Play / pause (Space)', html: icon('play'), on: { click: togglePlay } });
  const matchBtn = h('button.btn.primary.match-btn', { title: 'Ask the AI to match part of the reference', on: { click: (e) => { e.stopPropagation(); matchMenu(matchBtn); } } }, h('span', { html: icon('spark', 12) }), 'Match…', h('span', { html: icon('chev', 11) }));
  bar = h('div.mon-bar.rw-bar', tcEl,
    h('button.ib', { title: 'Back one frame (←)', html: icon('stepb'), on: { click: () => step(-1) } }), playBtn,
    h('button.ib', { title: 'Forward one frame (→)', html: icon('stepf'), on: { click: () => step(1) } }),
    h('span.sep'), shotEl, h('span.grow'),
    h('button.btn.ghost', { title: 'Attach this reference frame to the chat as "match this" (R)', on: { click: grabRef } }, h('span', { html: icon('grab', 12) }), 'Frame to chat'),
    h('span.match-wrap', matchBtn), durEl);

  zoomInp = h('input.rw-zoom', { type: 'range', min: 0, max: 1000, title: 'Zoom the strip (⌘ + wheel)' });
  zoomInp.addEventListener('input', () => zoomStrip(sliderToPps(+zoomInp.value), refVideo.currentTime || 0));
  selInfo = h('span.rw-selinfo');
  stripScroll = h('div.cs-scroll');
  stripInner = h('div.cs-inner');
  stripScroll.appendChild(stripInner);
  stripCv = h('canvas.cs-cv');
  stripWrap = h('div.rw-strip',
    h('div.rw-sh', h('b', 'Cut strip'), explain('Every shot of the reference, as wide as it lasts. Coloured bands are its parts; white ticks are flashes; blue ticks under it are the music\'s beats; "Aa" marks text on screen. Click a shot to jump there, shift-drag to pick a part.'),
      h('span.legend', ['talk', 'broll', 'montage', 'title'].map((k) => h('span', h('i', { style: { background: KIND_COL[k] } }), k === 'broll' ? 'b-roll' : k))),
      selInfo, h('span.grow'),
      h('button.ib.sm', { title: 'Zoom out', html: icon('zoomout', 13), on: { click: () => zoomStrip(R.pps / 1.6, refVideo.currentTime || 0) } }), zoomInp,
      h('button.ib.sm', { title: 'Zoom in', html: icon('zoomin', 13), on: { click: () => zoomStrip(R.pps * 1.6, refVideo.currentTime || 0) } }),
      h('button.ib.sm', { title: 'Fit the whole reference', html: icon('fit', 13), on: { click: () => zoomStrip(ppsFit(), 0) } })),
    h('div.cs-box', stripScroll, stripCv));
  center = h('div.rw-center', stage, bar, stripWrap);

  listEl = h('div.rw-list');
  sideBody = h('div.rw-sbody');
  side = h('div.rw-side', h('div.rw-tabs.mode-seg',
    h('button', { dataset: { tab: 'breakdown' }, on: { click: () => { R.tab = 'breakdown'; LS.set('refTab', R.tab); drawSide(); } } }, 'Breakdown'),
    h('button', { dataset: { tab: 'compare' }, on: { click: () => { R.tab = 'compare'; LS.set('refTab', R.tab); runCompare(); } } }, 'Compare with my edit')), sideBody);

  titleEl = h('b.rw-title', 'Reference'); metaEl = h('span.rw-meta');
  modeSeg = h('div.mode-seg.rw-modes',
    h('button.on', { dataset: { mode: 'single' }, title: 'Just the reference', on: { click: () => setMode('single') } }, 'Reference'),
    h('button', { dataset: { mode: 'sbs' }, title: 'Reference and your edit next to each other, in sync', on: { click: () => setMode('sbs') } }, 'Side by side'),
    h('button', { dataset: { mode: 'onion' }, title: 'Your edit over the reference: see framing and graphics differences', on: { click: () => setMode('onion') } }, 'Onion skin'));
  const lockBtn = h('button.btn' + (R.lock ? '.on' : ''), { title: 'Lock the reference to your edit: play and seek together', on: { click: () => { R.lock = !R.lock; LS.set('refLock', R.lock); lockBtn.classList.toggle('on', R.lock); followProgram('lock'); } } }, h('span', { html: icon('link', 12) }), 'Lock');
  const offInp = h('input.inp.mono', { type: 'number', step: 0.04, value: R.offset, style: { width: '62px' }, title: 'Offset: the reference runs this many seconds ahead of your edit' });
  offInp.addEventListener('change', () => { R.offset = +offInp.value || 0; LS.set('refOffset', R.offset); followProgram('offset'); });
  offInp.addEventListener('keydown', (e) => e.stopPropagation());
  const alignSel = h('select.inp', { title: 'Same time, or the same point of each video (stretch)' }, h('option', { value: 'time' }, 'same time'), h('option', { value: 'scale' }, 'same % (stretch)'));
  alignSel.value = R.align;
  alignSel.addEventListener('change', () => { R.align = alignSel.value; LS.set('refAlign', R.align); followProgram('align'); });
  linkBox = h('div.rw-link', { hidden: true }, lockBtn, alignSel, h('span.lbl', 'offset'), offInp);
  const alpha = h('input', { type: 'range', min: 0, max: 100, value: Math.round(R.alpha * 100), title: 'How much of your edit shows over the reference' });
  alpha.addEventListener('input', () => { R.alpha = +alpha.value / 100; LS.set('refAlpha', R.alpha); const f = progFrame(); if (f) f.style.setProperty('--onion-a', String(R.alpha)); });
  const onionSeg = h('div.mode-seg.sm',
    h('button' + (R.onion === 'fade' ? '.on' : ''), { dataset: { o: 'fade' }, title: 'Your edit at partial opacity over the reference', on: { click: () => setOnion('fade') } }, '50%'),
    h('button' + (R.onion === 'wipe' ? '.on' : ''), { dataset: { o: 'wipe' }, title: 'A divider you drag: reference left, your edit right', on: { click: () => setOnion('wipe') } }, 'Wipe'));
  function setOnion(o) { R.onion = o; LS.set('refOnion', o); onionSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.o === o)); alpha.hidden = o === 'wipe'; if (R.mode === 'onion') { unOnion(); doOnion(); } }
  alpha.hidden = R.onion === 'wipe';
  onionBox = h('div.rw-onion', { hidden: true }, onionSeg, alpha);
  ghostBtn = h('button.btn.ghost' + (R.ghost ? '.on' : ''), { title: 'Show the reference\'s shots as a strip under your timeline, aligned at 0', on: { click: () => { R.ghost = !R.ghost; LS.set('refGhost', R.ghost); drawGhost(true); } } }, h('span', { html: icon('list', 12) }), 'Show on timeline');
  hd = h('div.rw-hd',
    h('span.rw-badge', { html: xicon('ref', 14) }), h('span.rw-h', 'Reference'),
    explain('A reference is a video whose style you want. It\'s measured shot by shot so you can compare it with your edit and ask the AI to match any part of it.', { title: 'Reference' }),
    h('span.hdr-sep'), titleEl, metaEl, h('span.grow'), modeSeg, linkBox, onionBox, ghostBtn,
    h('button.btn.back', { title: 'Back to your edit (Esc)', on: { click: closeWs } }, h('span', { html: icon('x', 12) }), 'Close'));
  ws = h('section#ref-ws.panel.ref-ws', { hidden: true }, hd, h('div.rw-body', listEl, center, side));
  col.appendChild(ws);

  refVideo.addEventListener('loadedmetadata', () => { fitRef(); durEl.textContent = tc(refVideo.duration, rfps()); updTransport(); if (!an()) buildStrip(); });
  refVideo.addEventListener('timeupdate', updTransport);
  refVideo.addEventListener('seeked', updTransport);
  refVideo.addEventListener('play', () => { playBtn.innerHTML = icon('pause'); });
  refVideo.addEventListener('pause', () => { playBtn.innerHTML = icon('play'); });
  refPane.addEventListener('dblclick', togglePlay);
  new ResizeObserver(() => { fitRef(); stripDirty = true; }).observe(refPane);
  new ResizeObserver(() => { stripDirty = true; }).observe(stripScroll);
  initStripEvents();
  // a file dropped on the empty workspace goes straight to "Reference video"
  for (const el of [refPane, listEl]) {
    el.addEventListener('dragover', (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); e.stopPropagation(); } });
    el.addEventListener('drop', (e) => { if (!e.dataTransfer || !e.dataTransfer.files.length) return; e.preventDefault(); e.stopPropagation(); emit('importFiles', { files: [...e.dataTransfer.files], kind: 'reference' }); });
  }
  const loop = () => {
    if (R.open) {
      if (!refVideo.paused) { stripDirty = true; if (!linked()) revealStrip(refVideo.currentTime); }
      if (stripDirty) { stripDirty = false; drawStripCanvas(); }
    }
    if (R.ghost) drawGhost(false);
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}

function onKey(e) {
  if (!R.open || isTyping(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (k === 'Escape') { if (document.querySelector('.overlay:not([hidden]), .drop-ov:not([hidden])')) return; e.preventDefault(); closeWs(); return; }
  if (linked()) return;                                  // linked: the normal program keys drive both
  const stop = () => { e.preventDefault(); e.stopPropagation(); };
  if (k === ' ') { stop(); togglePlay(); }
  else if (k === 'ArrowLeft') { stop(); e.shiftKey ? seekRef((refVideo.currentTime || 0) - 1) : step(-1); }
  else if (k === 'ArrowRight') { stop(); e.shiftKey ? seekRef((refVideo.currentTime || 0) + 1) : step(1); }
  else if (k === 'ArrowUp' || k === 'ArrowDown') {
    const a = an(); if (!a) return; stop();
    const i = curShot(refVideo.currentTime || 0);
    const j = clamp(i + (k === 'ArrowDown' ? 1 : -1), 0, a.shots.length - 1);
    refVideo.pause(); seekRef(a.shots[j].start + 0.01);
  } else if (k === 'r' || k === 'R') { stop(); grabRef(); }
}

export function initReference() {
  build();
  initGhost();
  document.addEventListener('keydown', onKey, true);
  on('toggleReference', (v) => toggleReference(v));
  on('showReference', () => { if (!R.open) openWs(); else loadList(); });
  on('markReference', markReference);
  on('ref', (d) => {
    const r = d && d.id && R.list.find((x) => x.id === d.id);
    if (r && d.status === 'analyzing') { Object.assign(r, { status: r.status === 'downloading' && d.stage === 'download' ? 'downloading' : 'analyzing', stage: d.stage, progress: d.progress, job: d.job }); if (R.open) drawList(); if (d.id === R.cur && !an()) drawSide(); }
    else reloadListSoon();
    if (d && d.id === R.cur && ['done', 'analyzed', 'error'].includes(d.status)) { R.data = null; R.shown = null; setTimeout(() => select(d.id), 300); }
  });
  on('refAdded', () => { if (R.open) reloadListSoon(); });
  on('playhead', followProgram);
  on('playing', (p) => { if (linked() && !p) { refVideo.pause(); followProgram('pause'); } });
  on('plan', () => { if (R.tab === 'compare' && R.open && S.plan && R.cmpKey && !R.cmpKey.endsWith('|' + S.plan.slug)) runCompare(); });
  on('layout', () => { if (R.open) { fitRef(); stripDirty = true; } ghostSig = ''; });
  window.addEventListener('resize', () => { ghostSig = ''; });
  if (R.ghost) api.refs().then((r) => { R.list = r.refs || []; const id = R.cur && R.list.some((x) => x.id === R.cur) ? R.cur : (R.list[0] && R.list[0].id); if (id) { R.cur = id; api.ref(id).then((d) => { R.data = d; drawGhost(true); }).catch(() => {}); } }).catch(() => {});
}
export const _test = { R, matchMessage, setMode, seekRef, refVideo: () => refVideo, select, runCompare, drawGhost };
