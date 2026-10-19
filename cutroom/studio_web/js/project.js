// Project panel: Clips (virtualised grid/list over a paginated 50k-clip library), Songs, Plans, Renders.
import { S, on, emit, openPlan } from './state.js';
import { api, media } from './api.js';
import { h, icon, dur, ago, toast, LS, throttle, debounce, clamp } from './util.js';
import { tabs } from './layout.js';

const PAGE = 200;
let body;

// ================================================================ clips
const F = Object.assign({ q: '', setting: '', time: '', people: '', max_shake: '', min_quality: '', speech: false, creator: false }, LS.get('clipFilters', {}));
let viewMode = LS.get('clipView', 'grid');
const lib = { total: null, pages: new Map(), inflight: new Map(), gen: 0 };
let scroller, space, countEl, chipsEl, io, cells = new Map();
let geom = { cols: 1, cw: 150, ch: 110 };

function params(offset) {
  return { q: F.q, setting: F.setting, time: F.time, people: F.people, max_shake: F.max_shake, min_quality: F.min_quality,
    speech: F.speech ? 1 : '', creator: F.creator ? 1 : '', offset, limit: PAGE };
}
function resetLib() {
  lib.gen++; lib.total = null; lib.pages.clear();
  for (const c of lib.inflight.values()) c.abort();
  lib.inflight.clear();
  for (const el of cells.values()) el.remove();
  cells.clear();
  if (scroller) scroller.scrollTop = 0;
  LS.set('clipFilters', F);
  fetchPage(0);
  if (viewMode === 'days') loadDays();
}
async function fetchPage(pi) {
  if (lib.pages.has(pi) || lib.inflight.has(pi)) return;
  const gen = lib.gen, ctrl = new AbortController();
  lib.inflight.set(pi, ctrl);
  try {
    const r = await api.clips(params(pi * PAGE), ctrl.signal);
    if (gen !== lib.gen) return;
    const rows = Array.isArray(r) ? r : (r.rows || r.clips || []);
    lib.pages.set(pi, rows);
    if (r.total !== undefined) lib.total = r.total;
    else if (lib.total === null || rows.length < PAGE) lib.total = Math.max(lib.total || 0, pi * PAGE + rows.length + (rows.length === PAGE ? PAGE : 0));
    layout(); render();
  } catch (e) {
    if (e.name !== 'AbortError' && gen === lib.gen) { toast(`Library search failed: ${e.message}`, 'error'); lib.total = lib.total || 0; render(); }
  } finally { lib.inflight.delete(pi); }
}
function rowAt(i) { const p = lib.pages.get(Math.floor(i / PAGE)); return p ? p[i % PAGE] : undefined; }

function layout() {
  if (viewMode === 'days') return;
  const W = scroller.clientWidth;
  if (viewMode === 'grid') {
    const cols = Math.max(1, Math.floor(W / 138));
    const cw = W / cols;
    geom = { cols, cw, ch: Math.round((cw - 8) * 9 / 16 + 8 + 20) };
  } else geom = { cols: 1, cw: W, ch: 34 };
  const n = lib.total || 0;
  space.style.height = Math.ceil(n / geom.cols) * geom.ch + 'px';
  countEl.firstChild.textContent = lib.total === null ? 'Searching…' : `${lib.total.toLocaleString()} clip${lib.total === 1 ? '' : 's'}`;
}

function render() {
  if (!scroller || viewMode === 'days') return;
  const n = lib.total || 0;
  const top = scroller.scrollTop, hgt = scroller.clientHeight;
  const r0 = Math.max(0, Math.floor(top / geom.ch) - 2), r1 = Math.ceil((top + hgt) / geom.ch) + 2;
  const i0 = r0 * geom.cols, i1 = Math.min(n, r1 * geom.cols);
  const need = new Set();
  for (let i = i0; i < i1; i++) {
    need.add(i);
    const row = rowAt(i);
    if (!row) fetchPage(Math.floor(i / PAGE));
    let el = cells.get(i);
    if (el && el._id !== (row && row.id)) { el.remove(); cells.delete(i); el = null; }
    if (!el) { el = viewMode === 'grid' ? makeCell(row) : makeRow(row); el._id = row && row.id; cells.set(i, el); space.appendChild(el); }
    const col = i % geom.cols, rr = Math.floor(i / geom.cols);
    el.style.transform = `translate(${col * geom.cw}px, ${rr * geom.ch}px)`;
    if (viewMode === 'grid') { el.style.width = geom.cw + 'px'; }
    el.classList.toggle('sel', !!(row && S.source.clip && S.source.clip.id === row.id));
  }
  for (const [i, el] of cells) if (!need.has(i)) { if (el._img) io.unobserve(el._img); el.remove(); cells.delete(i); }
  if (!n && lib.total === 0) showEmpty(); else hideEmpty();
}
let emptyEl;
function showEmpty() {
  if (emptyEl) return;
  const filtered = F.q || F.setting || F.time || F.people || F.max_shake || F.min_quality || F.speech || F.creator;
  emptyEl = h('div.empty', { style: { position: 'absolute', inset: 0 } },
    h('span', { html: icon('search') }),
    h('h4', filtered ? 'No clips match' : 'Library is empty'),
    h('p', filtered ? 'Loosen a filter or search transcripts with different words.' : 'Index the footage folder (cutroom index) or start the overnight pass.'),
    filtered ? h('div.acts', h('button.btn', { on: { click: () => { Object.assign(F, { q: '', setting: '', time: '', people: '', max_shake: '', min_quality: '', speech: false, creator: false }); syncChips(); resetLib(); } } }, 'Clear filters')) : null);
  scroller.appendChild(emptyEl);
}
function hideEmpty() { if (emptyEl) { emptyEl.remove(); emptyEl = null; } }

function qdots(q) {
  const el = h('span.qdots', { title: q ? `quality ${q}/5` : 'not rated' });
  for (let k = 1; k <= 5; k++) el.appendChild(h('i', { class: k <= (q || 0) ? 'f' : '' }));
  if (q >= 4) el.classList.add('hi'); else if (q && q <= 2) el.classList.add('lo');
  return el;
}
function name(row) { return (row.rel || row.id).split('/').pop(); }
function dragData(row) {
  const t = row.tags || {};
  return JSON.stringify({ id: row.id, duration: row.duration, talk: !!(row.has_transcript && t.talking_to_camera) });
}
function makeCell(row) {
  if (!row) return h('div.cell', h('div.th'), h('div.meta', h('span.name', ' ')));
  const img = h('img', { alt: '', draggable: false });
  img.dataset.src = media.thumb(row.id, 'mid');
  img.onload = () => img.classList.add('ld');
  const scrub = h('div.scrub');
  const t = row.tags || {};
  const flags = h('div.flags',
    row.has_transcript ? h('span', { title: 'Has speech (transcribed)', html: icon('speech', 11) }) : null,
    t.creator_visible ? h('span', { title: 'Creator in frame', html: icon('person', 11) }) : null);
  const th = h('div.th', img, scrub, flags, h('span.dur', dur(row.duration)));
  const el = h('div.cell', { draggable: true, title: `${row.rel}\n${t.summary || ''}${row.shake !== undefined ? `\nshake ${(+row.shake).toFixed(1)}` : ''}` },
    th, h('div.meta', h('span.name', name(row)), qdots(row.quality ?? t.quality)));
  el._img = img;
  io.observe(img);
  const hoverScrub = throttle((frac) => { img.src = media.thumb(row.id, Math.floor(frac * (row.duration || 1))); }, 90);
  th.addEventListener('mousemove', (e) => {
    const r = th.getBoundingClientRect();
    const frac = clamp((e.clientX - r.left) / r.width, 0, 0.999);
    scrub.style.left = (frac * 100) + '%';
    hoverScrub(frac);
  });
  th.addEventListener('mouseleave', () => { setTimeout(() => { img.src = media.thumb(row.id, 'mid'); }, 100); });
  wireClip(el, row);
  return el;
}
function makeRow(row) {
  if (!row) return h('div.row', h('div.th'), h('div.nm', h('div', ' ')));
  const img = h('img', { alt: '', draggable: false }); img.dataset.src = media.thumb(row.id, 'mid');
  const t = row.tags || {};
  const el = h('div.row', { draggable: true, title: row.rel },
    h('div.th', img),
    h('div.nm', h('div', name(row)), h('div.s', [t.summary, t.setting, t.time_of_day, row.has_transcript ? 'speech' : null].filter(Boolean).join(' · ') || row.rel)),
    qdots(row.quality ?? t.quality), h('span.d', dur(row.duration)));
  el._img = img; io.observe(img);
  wireClip(el, row);
  return el;
}
function wireClip(el, row) {
  el.addEventListener('dblclick', () => emit('loadSource', { id: row.id }));
  el.addEventListener('click', () => { for (const c of cells.values()) c.classList.remove('sel'); el.classList.add('sel'); });
  el.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('application/x-cutroom-clip', dragData(row));
    e.dataTransfer.setData('text/plain', row.id);
    e.dataTransfer.effectAllowed = 'copy';
  });
}

// ---------------------------------------------------------------- filter chips
const CHIP_DEFS = [
  { k: 'setting', label: 'Setting', opts: ['kitchen', 'street', 'beach', 'office', 'car', 'gym', 'restaurant', 'park', 'cafe', 'hotel', 'balcony', 'airport'], free: true },
  { k: 'time', label: 'Time of day', opts: ['day', 'golden', 'night', 'indoor'] },
  { k: 'people', label: 'People', opts: ['creator', 'creator + friend', 'strangers', 'none'] },
  { k: 'max_shake', label: 'Max shake', range: [1, 12, 0.5], fmt: (v) => `≤ ${v}` },
  { k: 'min_quality', label: 'Min quality', opts: ['1', '2', '3', '4', '5'], fmt: (v) => `≥ ${v}` },
  { k: 'speech', label: 'Speech', bool: true, icon: 'speech' },
  { k: 'creator', label: 'Creator visible', bool: true, icon: 'person' },
];
let openPop = null;
function closePop() { if (openPop) { openPop.remove(); openPop = null; } }
function syncChips() {
  chipsEl.innerHTML = '';
  for (const d of CHIP_DEFS) {
    const v = F[d.k];
    const active = d.bool ? !!v : v !== '' && v !== null && v !== undefined;
    const chip = h('button.chip', { class: active ? 'chip on' : 'chip' },
      d.icon ? h('span', { html: icon(d.icon, 12) }) : null,
      d.label, active && !d.bool ? h('span.v', (d.fmt ? d.fmt(v) : v)) : null,
      !d.bool ? h('span', { html: icon('chev', 12), style: { opacity: 0.6 } }) : null);
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      if (d.bool) { F[d.k] = !F[d.k]; syncChips(); resetLib(); return; }
      const wasMine = openPop && openPop._k === d.k;
      closePop();
      if (wasMine) return;
      openPop = buildPop(d);
      openPop._k = d.k;
      chip.appendChild(openPop);
    });
    chipsEl.appendChild(chip);
  }
}
function buildPop(d) {
  const pop = h('div.chip-pop', { on: { click: (e) => e.stopPropagation() } });
  const set = (v) => { F[d.k] = v; closePop(); syncChips(); resetLib(); };
  if (d.range) {
    const val = h('span.mono', F[d.k] || 'any');
    const r = h('input', { type: 'range', min: d.range[0], max: d.range[1], step: d.range[2], value: F[d.k] || d.range[1] });
    r.addEventListener('input', () => (val.textContent = r.value));
    r.addEventListener('change', () => { F[d.k] = r.value; syncChips(); resetLib(); });
    pop.append(h('label', d.label, val), r, h('div.opts', h('button.btn.ghost', { on: { click: () => set('') } }, 'Any')));
    pop.lastChild.prepend(h('span', { style: { color: 'var(--text-3)', fontSize: '11px', flex: 1, alignSelf: 'center' } }, '<3 steady, >8 shaky'));
    return pop;
  }
  const opts = h('div.opts');
  for (const o of d.opts) opts.appendChild(h('button.chip', { class: String(F[d.k]) === o ? 'chip on' : 'chip', on: { click: () => set(o) } }, d.fmt ? d.fmt(o) : o));
  pop.append(opts);
  if (d.free) {
    const inp = h('input.inp', { placeholder: 'Other setting…', value: d.opts.includes(F[d.k]) ? '' : F[d.k] });
    inp.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') set(inp.value.trim()); });
    pop.append(inp);
  }
  pop.append(h('div.opts', h('button.btn.ghost', { on: { click: () => set('') } }, 'Any')));
  return pop;
}

function buildClips(pane) {
  const search = h('input.inp', { type: 'search', placeholder: 'Search clips, transcripts, tags', value: F.q, spellcheck: false });
  const onSearch = debounce(() => { F.q = search.value.trim(); resetLib(); }, 250);
  search.addEventListener('input', onSearch);
  search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { search.value = ''; onSearch(); search.blur(); } });
  const gridBtn = h('button.ib', { title: 'Grid view', html: icon('grid') });
  const listBtn = h('button.ib', { title: 'List view', html: icon('list') });
  const daysBtn = h('button.ib', { title: 'Days: clips grouped by the day they were shot', html: icon('cal') });
  const setView = (m) => {
    viewMode = m; LS.set('clipView', m);
    gridBtn.classList.toggle('on', m === 'grid'); listBtn.classList.toggle('on', m === 'list'); daysBtn.classList.toggle('on', m === 'days');
    for (const el of cells.values()) el.remove(); cells.clear();
    scroller.hidden = m === 'days'; daysWrap.hidden = m !== 'days';
    if (m === 'days') loadDays(); else { layout(); render(); }
  };
  gridBtn.onclick = () => setView('grid'); listBtn.onclick = () => setView('list'); daysBtn.onclick = () => setView('days');
  chipsEl = h('div.chips');
  countEl = h('div.pj-count', h('span', ''), h('span', 'Double-click to open · drag to timeline'));
  scroller = h('div.vscroll'); space = h('div.vspace'); scroller.appendChild(space);
  daysWrap = h('div.days', { hidden: true });
  pane.append(h('div.pj-bar', h('div.pj-search', h('span', { html: icon('search') }), search), gridBtn, listBtn, daysBtn), chipsEl, countEl, scroller, daysWrap);
  daysIO = new IntersectionObserver((ents) => {
    for (const e of ents) if (e.isIntersecting) { const img = e.target; if (img.dataset.src && !img.src) img.src = img.dataset.src; daysIO.unobserve(img); }
  }, { root: daysWrap, rootMargin: '200px 0px' });
  io = new IntersectionObserver((ents) => {
    for (const e of ents) if (e.isIntersecting) { const img = e.target; if (img.dataset.src && !img.src) img.src = img.dataset.src; io.unobserve(img); }
  }, { root: scroller, rootMargin: '200px 0px' });
  let raf = 0;
  scroller.addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); }, { passive: true });
  new ResizeObserver(() => { const oldCols = geom.cols; layout(); if (geom.cols !== oldCols) { for (const el of cells.values()) el.remove(); cells.clear(); } render(); }).observe(scroller);
  document.addEventListener('click', closePop);
  syncChips();
  on('sourceLoaded', render);
  setView(viewMode);
  if (lib.total === null) fetchPage(0); else { layout(); render(); }
}

// ================================================================ days (clips grouped by capture date)
let daysWrap, daysIO;
const days = { key: null, rows: [], total: 0, done: false, gen: 0, open: new Set(LS.get('daysOpen', [])) };
const DAY_PAGE = 1000;
function filterKey() { return JSON.stringify(F); }
async function loadDays() {
  // Fast path: per-day counts from the server, then each day's clips only when it is opened.
  const key = filterKey();
  if (days.key === key && (days.done || days.loading)) { renderDays(); return; }
  const gen = ++days.gen;
  Object.assign(days, { key, rows: [], total: 0, done: false, loading: true, summary: null, byDay: new Map() });
  renderDays();
  try {
    const s = await api.days();
    if (gen !== days.gen) return;
    days.summary = Array.isArray(s) ? s : null;
  } catch (e) { days.summary = null; }
  if (days.summary) {
    days.done = true; days.loading = false;
    for (const k of days.open) loadDay(k);
    renderDays();
    return;
  }
  // Fallback (older server): page through every clip and group in the browser.
  let off = 0;
  try {
    while (true) {
      const r = await api.clips({ ...params(off), limit: DAY_PAGE });
      if (gen !== days.gen) return;
      const rows = Array.isArray(r) ? r : (r.rows || []);
      days.rows.push(...rows.map((x) => ({ id: x.id, rel: x.rel, duration: x.duration, shot_at: x.shot_at, quality: x.quality, has_transcript: x.has_transcript, tags: x.tags || {} })));
      days.total = r.total ?? days.rows.length;
      off += rows.length;
      renderDays();
      if (!rows.length || off >= days.total) break;
      await new Promise((res) => setTimeout(res, 30));
    }
    days.done = true;
  } catch (e) { toast(`Couldn't group clips by day: ${e.message}`, 'error'); }
  finally { if (gen === days.gen) { days.loading = false; renderDays(); } }
}
async function loadDay(k) {
  if (!days.summary || days.byDay.has(k)) return;
  const gen = days.gen;
  days.byDay.set(k, null);
  try {
    const r = await api.clips({ ...params(0), day: k, limit: 600 });
    if (gen !== days.gen) return;
    const rows = Array.isArray(r) ? r : (r.rows || []);
    days.byDay.set(k, rows.map((x) => ({ id: x.id, rel: x.rel, duration: x.duration, shot_at: x.shot_at, quality: x.quality, has_transcript: x.has_transcript, tags: x.tags || {} })));
  } catch (e) { days.byDay.delete(k); toast(`Couldn't load that day: ${e.message}`, 'error'); }
  renderDays();
}
function dayKey(r) { const s = r.shot_at ? String(r.shot_at) : ''; return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : 'unknown'; }
function dayLabel(k) {
  if (k === 'unknown') return 'No capture date';
  const d = new Date(k + 'T12:00:00');
  return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short', year: 'numeric' });
}
let daysRaf = 0;
function renderDays() { if (!daysRaf) daysRaf = setTimeout(() => { daysRaf = 0; renderDaysNow(); }, 16); }
function renderDaysNow() {
  if (!daysWrap || daysWrap.hidden) return;
  const scroll = daysWrap.scrollTop;
  daysWrap.innerHTML = '';
  if (!days.done) {
    const pct = days.total ? (days.rows.length / days.total) * 100 : 0;
    daysWrap.appendChild(h('div.days-status', `Grouping by day… ${days.rows.length.toLocaleString()}${days.total ? ' of ' + days.total.toLocaleString() : ''} clips`, h('div.bar', h('i', { style: { width: pct + '%' } }))));
  }
  const groups = new Map();
  const meta = new Map();
  if (days.summary) {
    for (const d of days.summary) { groups.set(d.day, days.byDay.get(d.day) || []); meta.set(d.day, d); }
  } else {
    for (const r of days.rows) { const k = dayKey(r); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
  }
  const keys = [...groups.keys()].sort((a, b) => (a === 'unknown') - (b === 'unknown') || (a < b ? 1 : -1));
  if (days.done && !keys.length) daysWrap.appendChild(h('div.empty', h('h4', 'No clips match'), h('p', 'Loosen a filter to see more days.')));
  for (const k of keys) {
    const rows = groups.get(k).sort((a, b) => String(a.shot_at || a.rel).localeCompare(String(b.shot_at || b.rel)));
    const m = meta.get(k);
    const total = m ? m.hours * 3600 : rows.reduce((a, r) => a + (r.duration || 0), 0);
    const count = m ? m.clips : rows.length;
    const isOpen = days.open.has(k);
    const sec = h('div.day' + (isOpen ? '.open' : ''));
    const head = h('button.day-h', h('span.chev', { html: icon('chev', 12) }), h('span.t', dayLabel(k)),
      h('span.n', `${count} clip${count !== 1 ? 's' : ''} · ${dur(total)}`));
    head.onclick = () => { if (days.open.has(k)) days.open.delete(k); else { days.open.add(k); loadDay(k); } LS.set('daysOpen', [...days.open].slice(-50)); renderDaysNow(); };
    sec.appendChild(head);
    if (isOpen) {
      const grid = h('div.day-grid');
      const lim = sec._all ? rows.length : 120;
      for (const r of rows.slice(0, lim)) {
        const el = makeCell(r);
        if (el._img) { io.unobserve(el._img); daysIO.observe(el._img); }
        grid.appendChild(el);
      }
      sec.appendChild(grid);
      if (rows.length > lim) sec.appendChild(h('div', { style: { padding: '0 8px 8px' } }, h('button.btn', { on: { click: () => { sec._all = true; const more = rows.slice(lim); for (const r of more) { const el = makeCell(r); if (el._img) { io.unobserve(el._img); daysIO.observe(el._img); } grid.appendChild(el); } } } }, `Show all ${rows.length}`)));
    }
    daysWrap.appendChild(sec);
  }
  daysWrap.scrollTop = scroll;
}

// ================================================================ Claude's picks (bins)
export async function loadBins() {
  try {
    const r = await api.bins();
    S.bins = (Array.isArray(r) ? r : (r && r.bins) || []).slice().sort((a, b) => (b.updated || 0) - (a.updated || 0));
  } catch { S.bins = S.bins || []; }
  const b = document.getElementById('badge-picks');
  if (b) b.textContent = S.bins.length || '';
  emit('bins');
}
const binOpen = new Set(LS.get('binsOpen', []));
function buildPicks(pane, focus) {
  const bins = S.bins || [];
  pane.appendChild(h('div.picks-h', h('b', 'Claude’s picks'), h('span', bins.length ? 'Bins Claude made while working. Click to open in Source, drag onto the timeline.' : '')));
  const list = h('div.list-plain');
  if (!bins.length) {
    list.appendChild(h('div.empty', h('span', { html: icon('grid') }), h('h4', 'No picks yet'),
      h('p', 'Ask Claude to find your best moments, or to collect shots for an idea. They show up here as bins.'),
      h('div.acts', h('button.btn', { on: { click: () => emit('sendChat', 'Find my best moments and put them in a bin.') } }, 'Find my best moments'))));
  }
  if (focus) binOpen.add(focus);
  for (const b of bins) {
    const isOpen = binOpen.has(b.name) || bins.length === 1;
    const el = h('div.bin' + (isOpen ? '.open' : ''), { dataset: { bin: b.name } });
    const head = h('button.bin-h', h('span.chev', { html: icon('chev', 12) }), h('span.t', b.title || b.name),
      h('span.n', `${(b.items || []).length} · ${ago(b.updated)}`));
    head.onclick = () => { if (binOpen.has(b.name)) binOpen.delete(b.name); else binOpen.add(b.name); LS.set('binsOpen', [...binOpen]); showTab('picks'); };
    el.appendChild(head);
    if (isOpen) {
      const grid = h('div.bin-grid');
      for (const it of b.items || []) {
        const t0 = +it.t0 || 0, t1 = it.t1 !== undefined && it.t1 !== null ? +it.t1 : null;
        const img = h('img', { src: media.thumb(it.clip, t0 + (t1 !== null ? Math.min(1, (t1 - t0) / 2) : 0)), alt: '', loading: 'lazy', draggable: false });
        const card = h('div.bin-item', { draggable: true, title: it.why || '' },
          h('div.th', img, t1 !== null ? h('span.dur', dur(t1 - t0)) : null),
          h('div.why', it.why || ''));
        card.addEventListener('click', () => emit('loadSource', { id: it.clip, t: t0, in: t0, out: t1 }));
        card.addEventListener('dragstart', (e) => {
          e.dataTransfer.setData('application/x-cutroom-clip', JSON.stringify({ id: it.clip, in: t0, out: t1 ?? t0 + 3, duration: null }));
          e.dataTransfer.setData('text/plain', it.clip);
          e.dataTransfer.effectAllowed = 'copy';
        });
        grid.appendChild(card);
      }
      el.appendChild(grid);
    }
    list.appendChild(el);
  }
  pane.appendChild(list);
  if (focus) requestAnimationFrame(() => {
    const el = list.querySelector(`[data-bin="${CSS.escape(focus)}"]`);
    if (el) { el.scrollIntoView({ block: 'start' }); el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1400); }
  });
}

// ================================================================ songs
const songAudio = new Audio();
let playingSong = null;
function buildSongs(pane) {
  const songs = (S.project && S.project.songs) || [];
  const list = h('div.list-plain');
  if (!songs.length) list.appendChild(h('div.empty', h('span', { html: icon('music') }), h('h4', 'No songs indexed'), h('p', 'Point the project at a music folder and run cutroom songs index.')));
  for (const s of songs) {
    const btn = h('button.ib', { title: 'Preview', html: icon(playingSong === s.id ? 'pause' : 'play') });
    btn.onclick = () => {
      if (playingSong === s.id) { songAudio.pause(); playingSong = null; }
      else { songAudio.src = media.song(s.id); songAudio.currentTime = 0; songAudio.play().catch(() => toast('Preview failed to play', 'error')); playingSong = s.id; }
      showTab('songs');
    };
    list.appendChild(h('div.lrow', btn,
      h('div.grow', h('div.t1', s.title || s.id), h('div.t2', s.id)),
      h('span.num', s.tempo ? `${Math.round(s.tempo)} bpm` : '– bpm'), h('span.num', dur(s.duration))));
  }
  songAudio.onended = () => { playingSong = null; showTab('songs'); };
  pane.appendChild(list);
}

// ================================================================ plans
function buildPlans(pane) {
  const plans = (S.project && S.project.plans) || [];
  const title = h('input.inp', { placeholder: 'New plan title', style: { flex: 1 } });
  const create = async () => {
    const t = title.value.trim();
    if (!t) { title.focus(); return; }
    try { const r = await api.newPlan(t); emit('projectRefresh'); await openPlan(r.slug); toast(`Created plan “${t}”`, 'ok', 2000); }
    catch (e) { toast(`Couldn't create plan: ${e.message}`, 'error'); }
  };
  title.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') create(); });
  pane.appendChild(h('div.pj-bar', title, h('button.btn', { on: { click: create } }, h('span', { html: icon('plus', 13) }), 'New plan')));
  const list = h('div.list-plain');
  if (!plans.length) list.appendChild(h('div.empty', h('span', { html: icon('doc') }), h('h4', 'No plans yet'), h('p', 'Create one above, or ask Claude for an edit and it will write the plan.')));
  for (const p of plans) {
    const cur = S.plan && S.plan.slug === p.slug;
    const vers = (p.versions || []).map((v) => h('span.pill', { title: v }, (v.match(/_v(\d+)\.json$/) || [, v])[1] ? 'v' + v.match(/_v(\d+)\.json$/)[1] : v));
    list.appendChild(h('div.lrow', { class: cur ? 'lrow cur' : 'lrow', on: { click: () => openPlan(p.slug) } },
      h('span', { html: icon('doc'), style: { color: cur ? 'var(--accent)' : 'var(--text-3)' } }),
      h('div.grow', h('div.t1', p.title || p.slug), h('div.t2', `${p.slug}${p.draft ? ' · draft' : ''}${p.updated ? ' · ' + ago(p.updated) : ''}`),
        vers.length ? h('div.vers', vers) : null),
      cur ? h('span.pill.blue', 'open') : null));
  }
  pane.appendChild(list);
}

// ================================================================ renders
function buildRenders(pane) {
  const rs = ((S.project && S.project.renders) || []).slice().sort((a, b) => (b.created || 0) - (a.created || 0));
  const list = h('div.list-plain');
  if (!rs.length) list.appendChild(h('div.empty', h('span', { html: icon('render') }), h('h4', 'No renders yet'), h('p', 'Commit a version (⌘S), then Render preview in the toolbar.')));
  for (const r of rs) {
    const qa = r.qa_status || (r.qa ? 'qa' : null);
    list.appendChild(h('div.lrow',
      h('button.ib', { title: 'Play in Program', html: icon('play'), on: { click: () => emit('playRender', r.file) } }),
      h('div.grow', h('div.t1', r.file), h('div.t2', [r.version ? `v${r.version}` : null, r.preview ? 'preview' : 'final', r.duration ? dur(r.duration) : null, ago(r.created)].filter(Boolean).join(' · '))),
      qa ? h('span.pill.' + (qa === 'pass' ? 'ok' : qa), { title: 'QA result' }, qa) : null,
      r.qa ? h('button.btn.ghost', { on: { click: () => emit('openQA', r) } }, 'QA')
        : h('button.btn.ghost', { title: 'Run QA on this render', on: { click: async () => { try { await api.qa(r.file); toast('QA queued', 'ok', 1500); } catch (e) { toast(e.message, 'error'); } } } }, 'Run QA')));
  }
  pane.appendChild(list);
}

// ================================================================ tabs
let current = 'clips', clipsPane = null, pendingBin = null;
function showTab(name) {
  current = name;
  for (const c of [...body.children]) if (c !== clipsPane) c.remove();
  if (name === 'clips') {
    if (!clipsPane) { clipsPane = h('div', { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } }); body.appendChild(clipsPane); buildClips(clipsPane); }
    clipsPane.hidden = false;
    requestAnimationFrame(() => { layout(); render(); });
    return;
  }
  if (clipsPane) clipsPane.hidden = true;
  const pane = h('div', { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } });
  body.appendChild(pane);
  if (name === 'picks') buildPicks(pane, pendingBin), pendingBin = null;
  else if (name === 'songs') buildSongs(pane);
  else if (name === 'plans') buildPlans(pane);
  else buildRenders(pane);
}

export function initProject(el) {
  body = el;
  const t = tabs(document.getElementById('p-project'), 'project', showTab);
  t.set(t.initial);
  on('project', () => { if (current !== 'clips') showTab(current); });
  on('plan', () => { if (current === 'plans') showTab('plans'); });
  on('showProjectTab', (n) => t.set(n));
  on('bins', () => { if (current === 'picks') showTab('picks'); });
  on('openBin', (name) => { pendingBin = name; t.set('picks'); });
  on('binsChanged', loadBins);
  loadBins();
}
