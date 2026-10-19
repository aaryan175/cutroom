// Source monitor: proxy playback, In/Out marks, clickable transcript, quality strip, insert/overwrite.
import { S, on, emit, op, pieceAt, segmentOf, pushContext } from './state.js';
import { api, media } from './api.js';
import { h, icon, tc, clamp, toast, LS } from './util.js';

let video, scrub, sctx, tcEl, durEl, ioEl, words = [], wordEls = [], trEl, tagsEl, emptyEl, playBtn, frameEl;
let shuttle = 0, revTimer = null;
const SFPS = () => (S.source.detail && S.source.detail.fps) || (S.source.clip && S.source.clip.fps) || 25;

export const sourceCtl = {
  togglePlay() { if (!video.src) return; if (video.paused) { shuttle = 1; video.playbackRate = 1; video.play(); } else { stop(); } },
  shuttle(dir) { shuttleBy(dir); },
  step(n) { stop(); seek(video.currentTime + n / SFPS()); },
  seekRel(s) { seek(video.currentTime + s); },
  markIn() { setIO('in', video.currentTime); },
  markOut() { setIO('out', video.currentTime); },
  get loaded() { return !!S.source.clip; },
};

function stop() { shuttle = 0; clearInterval(revTimer); revTimer = null; video.pause(); video.playbackRate = 1; }
function shuttleBy(dir) {
  if (!video.src) return;
  if (dir === 0) { stop(); return; }
  if (Math.sign(dir) !== Math.sign(shuttle)) shuttle = 0;
  shuttle = clamp(shuttle + dir, -4, 4);
  if (shuttle > 0) { clearInterval(revTimer); revTimer = null; video.playbackRate = [1, 1, 2, 4, 8][shuttle]; video.play(); }
  else {
    video.pause(); clearInterval(revTimer);
    const sp = [1, 1, 2, 4, 8][-shuttle];
    revTimer = setInterval(() => { if (video.currentTime <= 0) { stop(); return; } seek(video.currentTime - sp / 12); }, 1000 / 12);
  }
}
function seek(t) {
  const d = video.duration || (S.source.clip && S.source.clip.duration) || 0;
  video.currentTime = clamp(t, 0, Math.max(0, d - 0.001));
}

function setIO(which, t) {
  if (!S.source.clip) return;
  t = +(+t).toFixed(3);
  S.source[which] = t;
  if (S.source.in !== null && S.source.out !== null && S.source.out <= S.source.in) {
    if (which === 'in') S.source.out = null; else S.source.in = null;
  }
  drawScrub(); paintWords(); updateIO(); pushContext();
}

export async function loadSource({ id, t = null, in: i = null, out = null }) {
  if (!id) return;
  try {
    const d = await api.clip(id);
    S.source = { clip: d, detail: d, in: i, out, t: t ?? 0 };
    video.src = media.proxy(d.id || id);
    video.currentTime = 0;
    video.addEventListener('loadedmetadata', () => { if (t !== null) seek(t); }, { once: true });
    emptyEl.hidden = true; frameEl.hidden = false;
    document.getElementById('src-title').textContent = (d.rel || d.id).split('/').pop();
    words = (d.words || []).map((w) => Array.isArray(w) ? { s: +w[0], e: +w[1], w: w[2] } : { s: +w.s ?? +w.start, e: +w.e ?? +w.end, w: w.w ?? w.word });
    buildTranscript(); buildTags(); drawScrub(); updateIO();
    LS.set('lastSource', d.id || id);
    emit('sourceLoaded', d);
    pushContext();
    emit('focusMonitor', 'source');
  } catch (e) { toast(`Couldn't load clip ${id}: ${e.message}`, 'error'); }
}

function buildTranscript() {
  trEl.innerHTML = '';
  wordEls = [];
  if (!words.length) {
    trEl.appendChild(h('div.empty', { style: { padding: '12px' } }, h('p', S.source.clip.has_transcript === false ? 'No speech in this clip.' : 'Not transcribed yet. Run the overnight pass or ask Claude to transcribe it.')));
    return;
  }
  const frag = document.createDocumentFragment();
  let prevEnd = words[0].s;
  words.forEach((w, i) => {
    const gap = w.s - prevEnd;
    if (gap > 1.6) frag.appendChild(h('span.para'));
    else if (gap > 0.6) frag.appendChild(h('span.gap'));
    const el = h('span.w', { dataset: { i } }, w.w);
    wordEls.push(el);
    frag.appendChild(el); frag.appendChild(document.createTextNode(' '));
    prevEnd = w.e;
  });
  trEl.appendChild(frag);
  paintWords();
}
function paintWords() {
  const { in: a, out: b } = S.source;
  for (let i = 0; i < wordEls.length; i++) {
    const w = words[i];
    wordEls[i].classList.toggle('io', a !== null && b !== null && w.s >= a - 0.01 && w.e <= b + 0.01);
  }
}
let nowIdx = -1;
function paintNow(t) {
  if (!words.length) return;
  let i = -1, lo = 0, hi = words.length - 1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (words[m].s <= t) { i = m; lo = m + 1; } else hi = m - 1; }
  if (i >= 0 && t > words[i].e + 0.3) i = -1;
  if (i === nowIdx) return;
  if (nowIdx >= 0 && wordEls[nowIdx]) wordEls[nowIdx].classList.remove('now');
  nowIdx = i;
  if (i >= 0) {
    wordEls[i].classList.add('now');
    if (!video.paused) {
      const r = wordEls[i].getBoundingClientRect(), pr = trEl.getBoundingClientRect();
      if (r.bottom > pr.bottom - 8 || r.top < pr.top) trEl.scrollTop += r.top - pr.top - 30;
    }
  }
}
function wireTranscript() {
  let down = null;
  trEl.addEventListener('pointerdown', (e) => {
    const el = e.target.closest('.w'); if (!el) return;
    e.preventDefault();
    down = { i: +el.dataset.i, j: +el.dataset.i, moved: false };
  });
  trEl.addEventListener('pointerover', (e) => {
    if (!down) return;
    const el = e.target.closest('.w'); if (!el) return;
    down.j = +el.dataset.i; down.moved = down.j !== down.i;
    const [a, b] = [Math.min(down.i, down.j), Math.max(down.i, down.j)];
    wordEls.forEach((w, k) => w.classList.toggle('io', k >= a && k <= b));
  });
  window.addEventListener('pointerup', () => {
    if (!down) return;
    const d = down; down = null;
    if (!d.moved) { stop(); seek(words[d.i].s); return; }
    const [a, b] = [Math.min(d.i, d.j), Math.max(d.i, d.j)];
    // pad slightly so the first/last syllables aren't clipped
    S.source.in = +Math.max(0, words[a].s - 0.08).toFixed(3);
    S.source.out = +Math.min(video.duration || 1e9, words[b].e + 0.12).toFixed(3);
    seek(S.source.in);
    drawScrub(); paintWords(); updateIO(); pushContext();
  });
}

function buildTags() {
  const d = S.source.detail || {}, t = d.tags || {};
  tagsEl.innerHTML = '';
  if (t.summary) tagsEl.appendChild(h('div.summary', t.summary));
  const kv = [['setting', t.setting], ['time', t.time_of_day], ['people', t.people], ['shot', t.shot], ['camera', t.camera],
    ['quality', t.quality], ['creator', t.creator_visible === undefined ? null : (t.creator_visible ? 'yes' : 'no')],
    ['mood', (t.mood || []).join(', ')], ['actions', (t.actions || []).join(', ')], ['topics', (t.topics || []).join(', ')],
    ['size', d.width ? `${d.width}×${d.height} ${d.fps ? Math.round(d.fps) + 'p' : ''}` : null], ['shot at', d.shot_at ? String(d.shot_at).replace('T', ' ').slice(0, 16) : null]];
  for (const [k, v] of kv) if (v !== null && v !== undefined && v !== '') tagsEl.appendChild(h('span', h('span.k', k), String(v)));
  if (t.notes) tagsEl.appendChild(h('div.summary', { style: { color: 'var(--text-2)' } }, t.notes));
  if (!tagsEl.children.length) tagsEl.appendChild(h('span', { style: { color: 'var(--text-3)' } }, 'Not tagged yet.'));
}

// ---------------------------------------------------------------- mini scrubber
function drawScrub() {
  const r = scrub.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = r.width, H = r.height;
  if (!W) return;
  if (sctx.canvas.width !== Math.round(W * dpr)) { sctx.canvas.width = Math.round(W * dpr); sctx.canvas.height = Math.round(H * dpr); }
  const c = sctx; c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  c.fillStyle = '#161618'; c.fillRect(0, 0, W, H);
  const d = video.duration || (S.source.clip && S.source.clip.duration) || 0;
  if (!d) return;
  const X = (t) => (t / d) * W;
  // quality heat strip (shake, exposure) per metrics window
  const m = (S.source.detail && S.source.detail.metrics) || [];
  for (const w of m) {
    const x1 = X(w.t0), x2 = X(w.t1);
    const sh = +w.shake || 0;
    const a = clamp((sh - 2) / 8, 0, 1);
    c.fillStyle = `hsl(${120 - a * 120}, 45%, ${30 + a * 10}%)`;
    c.fillRect(x1, H - 6, Math.max(1, x2 - x1 - 0.5), 3);
    const ex = +w.exposure; const eb = isFinite(ex) ? clamp(Math.abs(ex - 118) / 90, 0, 1) : 0;
    c.fillStyle = `rgba(212,162,60,${eb})`;
    c.fillRect(x1, H - 3, Math.max(1, x2 - x1 - 0.5), 3);
  }
  // shot boundaries
  c.fillStyle = 'rgba(255,255,255,.18)';
  for (const s of (S.source.detail && S.source.detail.shots) || []) { const st = Array.isArray(s) ? s[0] : s.start; if (st > 0.01) c.fillRect(Math.round(X(st)), 4, 1, H - 12); }
  // ticks
  c.fillStyle = '#3a3a40';
  const step = d > 120 ? 10 : d > 30 ? 5 : 1;
  for (let t = 0; t < d; t += step) c.fillRect(Math.round(X(t)), 0, 1, t % (step * 5) === 0 ? 6 : 3);
  // in/out
  const { in: a, out: b } = S.source;
  if (a !== null || b !== null) {
    const x1 = X(a ?? 0), x2 = X(b ?? d);
    c.fillStyle = 'rgba(201,162,39,.2)'; c.fillRect(x1, 0, x2 - x1, H - 7);
    c.fillStyle = '#c9a227';
    if (a !== null) { c.fillRect(x1, 0, 2, H - 7); c.fillRect(x1, 0, 6, 2); c.fillRect(x1, H - 9, 6, 2); }
    if (b !== null) { c.fillRect(x2 - 2, 0, 2, H - 7); c.fillRect(x2 - 6, 0, 6, 2); c.fillRect(x2 - 6, H - 9, 6, 2); }
  }
  // playhead
  const px = Math.round(X(video.currentTime || 0));
  c.fillStyle = '#4c8dff'; c.fillRect(px - 0.5, 0, 2, H - 6);
  c.beginPath(); c.moveTo(px - 4.5, 0); c.lineTo(px + 5.5, 0); c.lineTo(px + 0.5, 5); c.fill();
}
function updateIO() {
  const { in: a, out: b } = S.source;
  const f = SFPS();
  ioEl.innerHTML = '';
  if (a === null && b === null) { ioEl.className = 'tc dim io-lbl'; ioEl.textContent = 'No In/Out'; return; }
  ioEl.className = 'tc in io-lbl';
  const d = (b ?? (video.duration || 0)) - (a ?? 0);
  ioEl.textContent = tc(Math.max(0, d), f);
  ioEl.title = `In ${a !== null ? tc(a, f) : '–'}  Out ${b !== null ? tc(b, f) : '–'}`;
}

// ---------------------------------------------------------------- edits
function sourceSegment() {
  const c = S.source.clip;
  if (!c) { toast('Load a clip in the Source monitor first (double-click one in Project)', 'warn'); return null; }
  const d = video.duration || c.duration || 0;
  let a = S.source.in, b = S.source.out;
  if (a === null && b === null) { a = video.currentTime; b = Math.min(d, a + 3); }
  if (a === null) a = 0;
  if (b === null) b = d;
  if (b - a < 0.05) { toast('In/Out range is empty', 'warn'); return null; }
  const spoken = words.filter((w) => w.s >= a && w.e <= b).length;
  const type = spoken >= 3 && spoken / (b - a) > 0.8 ? 'talk' : 'broll';
  return { type, clip: c.id, in: +a.toFixed(3), out: +b.toFixed(3) };
}
export async function insertFromSource(mode = 'insert') {
  const seg = sourceSegment(); if (!seg) return;
  if (!S.plan) { emit('needPlan'); return; }
  const r = await op({ op: mode, at_t: +S.playhead.toFixed(3), segment: seg });
  if (r) toast(`${mode === 'insert' ? 'Inserted' : 'Overwrote with'} ${seg.type} ${(seg.out - seg.in).toFixed(2)}s`, 'ok', 1800);
}
export async function cutawayFromSource() {
  const seg = sourceSegment(); if (!seg) return;
  const p = pieceAt(S.playhead, 'V1');
  if (!p) { toast('Put the playhead over a segment to add a cutaway', 'warn'); return; }
  const parent = segmentOf(p);
  const srcT = p.in !== null ? p.in + (S.playhead - p.start) * (p.speed || 1) : 0;
  const at = parent && parent.in !== undefined ? srcT - parent.in : 0;
  await op({ op: 'add_cutaway', seg: [p.sec, p.seg], cutaway: { clip: seg.clip, in: seg.in, at: +Math.max(0, at).toFixed(3), dur: +(seg.out - seg.in).toFixed(3) } });
}
async function grab() {
  if (!S.source.clip) return;
  try {
    const t = +video.currentTime.toFixed(3);
    const r = await api.grab({ source: 'clip', clip: S.source.clip.id, t });
    emit('attach', { url: r.url, label: `${(S.source.clip.rel || '').split('/').pop()} @ ${t.toFixed(2)}s`, source: 'clip', t, clip: S.source.clip.id });
  } catch (e) { toast(`Grab failed: ${e.message}`, 'error'); }
}

// ---------------------------------------------------------------- init
export function initSource(body) {
  video = h('video', { preload: 'auto', playsInline: true });
  frameEl = h('div.mon-frame', { hidden: true }, video);
  emptyEl = h('div.mon-empty', h('span', { html: icon('film', 22) }), h('div', 'Double-click a clip in Project to load it here'));
  const view = h('div.mon-view', frameEl, emptyEl);
  const cv = h('canvas'); sctx = cv.getContext('2d');
  scrub = h('div.mini-scrub', cv);
  tcEl = h('span.tc', '00:00:00:00');
  durEl = h('span.tc.dim', '');
  ioEl = h('span.tc.dim', 'No In/Out');
  const ioWrap = h('span.nw2', { style: { display: 'contents' } }, ioEl);
  playBtn = h('button.ib', { title: 'Play / pause (Space)', html: icon('play'), on: { click: () => sourceCtl.togglePlay() } });
  const bar = h('div.mon-bar',
    tcEl,
    h('button.ib', { title: 'Mark In (I)', html: icon('markin'), on: { click: () => sourceCtl.markIn() } }),
    h('button.ib', { title: 'Mark Out (O)', html: icon('markout'), on: { click: () => sourceCtl.markOut() } }),
    h('span.sep'),
    h('button.ib.nw', { title: 'Back one frame (←)', html: icon('stepb'), on: { click: () => sourceCtl.step(-1) } }),
    playBtn,
    h('button.ib.nw', { title: 'Forward one frame (→)', html: icon('stepf'), on: { click: () => sourceCtl.step(1) } }),
    h('span.sep.nw'),
    h('button.ib', { title: 'Insert In/Out at the playhead (,)', html: icon('insert'), on: { click: () => insertFromSource('insert') } }),
    h('button.ib', { title: 'Overwrite at the playhead (.)', html: icon('overwrite'), on: { click: () => insertFromSource('overwrite') } }),
    h('button.ib.nw2', { title: 'Add as cutaway over the segment at the playhead', html: icon('cutaway'), on: { click: cutawayFromSource } }),
    h('button.ib', { title: 'Grab frame to chat', html: icon('grab'), on: { click: grab } }),
    h('span.grow'),
    ioWrap);
  void durEl;
  trEl = h('div.transcript');
  tagsEl = h('div.tags-sum', { hidden: true });
  const tTr = h('button.ptab.on', 'Transcript'), tTg = h('button.ptab', 'Tags');
  const setT = (which) => { tTr.classList.toggle('on', which === 'tr'); tTg.classList.toggle('on', which !== 'tr'); trEl.hidden = which !== 'tr'; tagsEl.hidden = which === 'tr'; LS.set('srcTab', which); };
  tTr.onclick = () => setT('tr'); tTg.onclick = () => setT('tags');
  const lower = h('div.src-lower', h('div.src-tabs', tTr, tTg), trEl, tagsEl);
  setT(LS.get('srcTab', 'tr'));
  body.appendChild(h('div.mon', view, scrub, bar, lower));

  // fit the frame to 16:9 (or the clip's aspect) inside the view
  const fit = () => {
    const r = view.getBoundingClientRect();
    const c = S.source.clip;
    const ar = video.videoWidth && video.videoHeight ? video.videoWidth / video.videoHeight : (c && c.width && c.height ? c.width / c.height : 16 / 9);
    let w = r.width, hh = w / ar;
    if (hh > r.height) { hh = r.height; w = hh * ar; }
    frameEl.style.width = w + 'px'; frameEl.style.height = hh + 'px';
    drawScrub();
  };
  new ResizeObserver(fit).observe(view);
  on('sourceLoaded', fit);
  video.addEventListener('loadedmetadata', fit);

  // scrubbing
  let sdrag = false;
  const scrubAt = (e) => {
    const r = scrub.getBoundingClientRect();
    const d = video.duration || 0;
    if (!d) return;
    stop(); seek(clamp((e.clientX - r.left) / r.width, 0, 1) * d);
  };
  scrub.addEventListener('pointerdown', (e) => { sdrag = true; scrub.setPointerCapture(e.pointerId); scrubAt(e); emit('focusMonitor', 'source'); });
  scrub.addEventListener('pointermove', (e) => { if (sdrag) scrubAt(e); });
  scrub.addEventListener('pointerup', () => { sdrag = false; });
  view.addEventListener('click', () => { emit('focusMonitor', 'source'); if (S.source.clip) sourceCtl.togglePlay(); });

  let raf = 0;
  const tick = () => {
    raf = 0;
    const t = video.currentTime || 0;
    tcEl.textContent = tc(t, SFPS());
    S.source.t = t;
    paintNow(t);
    drawScrub();
    if (!video.paused) raf = requestAnimationFrame(tick);
  };
  const kick = () => { if (!raf) raf = requestAnimationFrame(tick); };
  video.addEventListener('timeupdate', kick);
  video.addEventListener('seeked', () => { kick(); pushContext(); });
  video.addEventListener('play', () => { playBtn.innerHTML = icon('pause'); kick(); });
  video.addEventListener('pause', () => { playBtn.innerHTML = icon('play'); kick(); pushContext(); });
  video.addEventListener('error', () => { if (S.source.clip) toast('Source proxy failed to load. Is the proxy built for this clip?', 'error'); });
  wireTranscript();
  on('loadSource', loadSource);
  on('layout', fit);

  const last = LS.get('lastSource', null);
  if (last) loadSource({ id: last }).then(() => emit('focusMonitor', 'program'));
}
