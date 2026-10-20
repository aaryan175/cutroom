// Looks: a gallery of colour looks rendered on YOUR current frame (server-side ffmpeg, same chain as a
// render), a before/after split on the big preview, intensity, apply to the whole edit or one segment,
// and "match a reference's colour". Draft playback in the program monitor approximates the look with a
// CSS filter so it is visible at once; the render uses the real filter chain (cutroom/looks.py).
import { S, on, emit, op, pictureAt, pieceAt, segmentOf, fps } from './state.js';
import { h, tc, toast, debounce, LS, clamp } from './util.js';

const SVG = {
  look: '<circle cx="6" cy="6.5" r="3.6"/><circle cx="10" cy="6.5" r="3.6"/><circle cx="8" cy="10" r="3.6"/>',
  refresh: '<path d="M13 8a5 5 0 1 1-1.5-3.6"/><path d="M13 2.5v3h-3"/>',
  match: '<rect x="1.8" y="4" width="5.4" height="8" rx="1"/><rect x="8.8" y="4" width="5.4" height="8" rx="1"/><path d="M6 8h4"/>',
  check: '<path d="M3 8.5 6.5 12 13 4.5"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  seg: '<rect x="2" y="5" width="12" height="6" rx="1"/><path d="M6 5v6M10 5v6"/>',
  film: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M5 2.5v11M11 2.5v11"/>',
};
const ico = (n, s = 14) => `<svg class="ic" viewBox="0 0 16 16" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SVG[n] || ''}</svg>`;
const I = (n, s) => h('span.icw', { html: ico(n, s) });
async function jget(path) {
  const r = await fetch(path);
  const d = await r.json().catch(() => null);
  if (!r.ok) throw new Error((d && (d.error || d.message)) || `${r.status}`);
  return d;
}
const enc = encodeURIComponent;

// ---------------------------------------------------------------- catalogue
let CAT = [];
let catP = null;
export function loadCatalogue(force = false) {
  if (!catP || force) catP = jget('/api/looks').then((r) => { CAT = r.looks || []; LUTS_DIR = r.luts_dir || ''; emit('looksCatalogue'); return CAT; }).catch(() => CAT);
  return catP;
}
let LUTS_DIR = '';
const BUILTIN_LABEL = { natural: 'Natural', warm_film: 'Warm Film', cool_clean: 'Cool Clean', golden_hour: 'Golden Hour', teal_orange: 'Teal & Orange', moody_matte: 'Moody Matte', punchy: 'Punchy Contrast', soft_pastel: 'Soft Pastel', bw: 'Black & White', vintage_fade: 'Vintage Fade', night_cinematic: 'Night Cinematic' };
export function lookLabel(name) {
  if (!name) return 'None';
  const c = CAT.find((x) => x.name === name);
  if (c) return c.label;
  return BUILTIN_LABEL[name] || (name.startsWith('lut:') ? name.slice(4).replace(/\.cube$/i, '') : name);
}
const pct = (k) => `${Math.round((k ?? 1) * 100)}%`;
export function lookText(lk) { return lk && lk.name && lk.name !== 'natural' ? `${lookLabel(lk.name)} ${pct(lk.intensity)}` : 'None'; }
function normLook(v) {
  if (!v) return null;
  if (typeof v === 'string') return { name: v, intensity: 1 };
  return v.name ? { name: v.name, intensity: v.intensity ?? 1 } : null;
}

/** CSS filter that approximates a look for instant draft playback. */
export function cssFilter(lk) {
  lk = normLook(lk);
  if (!lk || lk.name === 'natural') return '';
  const c = (CAT.find((x) => x.name === lk.name) || {}).css || {};
  const k = clamp(+lk.intensity || 0, 0, 1);
  const f = [];
  const mul = (key, fn) => { if (c[key] !== undefined) { const v = 1 + (c[key] - 1) * k; if (Math.abs(v - 1) > 0.005) f.push(`${fn}(${v.toFixed(3)})`); } };
  mul('brightness', 'brightness'); mul('contrast', 'contrast'); mul('saturate', 'saturate');
  if (c.sepia) f.push(`sepia(${(c.sepia * k).toFixed(3)})`);
  if (c.hue) f.push(`hue-rotate(${(c.hue * k).toFixed(1)}deg)`);
  return f.join(' ');
}

// ---------------------------------------------------------------- plan helpers
function planLook() { const g = (S.plan && S.plan.draft && S.plan.draft.style && S.plan.draft.style.grade) || {}; return normLook(g.look); }
function segUnderPlayhead() {
  const p = pieceAt(S.playhead, 'V1');
  if (!p) return null;
  return { piece: p, seg: segmentOf(p), key: [p.sec, p.seg] };
}
async function applyWhole(lk) {
  const r = await op({ op: 'set_style', fields: { grade: { look: lk && lk.name !== 'natural' ? { name: lk.name, intensity: +lk.intensity.toFixed(2) } : null } } });
  if (r) toast(lk && lk.name !== 'natural' ? `${lookText(lk)} on the whole edit` : 'Look removed from the whole edit', 'ok', 1800);
}
async function applySeg(key, lk) {
  const r = await op({ op: 'set', seg: key, fields: { look: lk ? { name: lk.name, intensity: +lk.intensity.toFixed(2) } : null } });
  if (r) toast(lk ? `${lookText(lk)} on this segment` : 'Segment follows the whole edit again', 'ok', 1800);
}

// ---------------------------------------------------------------- program monitor preview (CSS approximation)
function applyProgramFilter() {
  const vids = document.querySelectorAll('#p-program .mon-view .mon-frame > video');
  let f = '';
  if (S.programMode !== 'render' && S.tl) {
    const p = pictureAt(S.playhead);
    f = cssFilter(p ? p.look : (S.tl.raw && S.tl.raw.look));
  }
  for (const v of vids) if (v.style.filter !== f) v.style.filter = f;
  const fr = document.querySelector('#p-program .mon-view .mon-frame');
  if (fr) fr.dataset.look = f ? 'on' : '';
}

// ---------------------------------------------------------------- Inspector field
/** Look picker for the Inspector: select + intensity. `inherit` adds a "same as the whole edit" choice. */
export function lookField(value, onCommit, { inherit = false } = {}) {
  const cur = normLook(value);
  const s = h('select.inp.lk-sel');
  const opts = inherit ? [['', 'Same as whole edit'], ['natural', 'None (natural)']] : [['', 'None']];
  for (const c of CAT.filter((x) => x.name !== 'natural')) opts.push([c.name, c.label + (c.kind === 'lut' ? ' (LUT)' : '')]);
  if (cur && !opts.some((o) => o[0] === cur.name)) opts.push([cur.name, lookLabel(cur.name)]);
  for (const [v, l] of opts) s.appendChild(h('option', { value: v }, l));
  s.value = cur ? cur.name : '';
  const k = h('input.inp.mono.lk-k', { type: 'number', min: 0, max: 100, step: 5, value: Math.round((cur ? cur.intensity : 1) * 100), title: 'Intensity %' });
  const stopK = (e) => { e.stopPropagation(); if (e.key === 'Enter') e.target.blur(); };
  s.addEventListener('keydown', stopK); k.addEventListener('keydown', stopK);
  const commit = () => {
    const name = s.value;
    if (!name) return onCommit(null);
    onCommit({ name, intensity: clamp((+k.value || 0) / 100, 0, 1) });
  };
  s.addEventListener('change', commit); k.addEventListener('change', commit);
  k.disabled = !cur || cur.name === 'natural';
  const sw = h('span.lk-sw', { style: { filter: cssFilter(cur) } });
  return h('div.pair.lk-field', sw, s, k, h('span.lk-pct', '%'));
}

// ---------------------------------------------------------------- gallery pane
let root = null;
let frame = null;           // {plan, t, clip, where, items, before_url}
let frameErr = '';
let loading = false;
let pick = null;            // {name, intensity} selected in the gallery
let split = LS.get('lk.split', 0.5);
let match = null;           // result of /api/looks/match
let camMatch = null;        // result of /api/cameras/match (camera transform + match to the hero camera)
let refs = null;
let reqSeq = 0;

function previewUrl(lk) {
  if (!frame) return '';
  return `/api/looks/preview?plan=${enc(frame.plan)}&t=${frame.t.toFixed(3)}&look=${enc(lk.name)}&intensity=${Math.round(lk.intensity * 100)}`;
}
async function loadFrame(force = false) {
  if (!S.plan || !S.tl) return;
  const t = +S.playhead.toFixed(2);
  if (!force && frame && frame.plan === S.plan.slug && Math.abs(frame.t - t) < 0.02) return;
  const my = ++reqSeq;
  loading = true; frameErr = ''; draw();
  try {
    const r = await jget(`/api/looks/sheet?plan=${enc(S.plan.slug)}&t=${t.toFixed(3)}`);
    if (my !== reqSeq) return;
    frame = { ...r, plan: S.plan.slug, t };
    camMatch = null;
    jget(`/api/cameras/match?plan=${enc(S.plan.slug)}&t=${t.toFixed(3)}`).then((m) => { if (my === reqSeq) { camMatch = m; draw(); } }).catch(() => {});
  } catch (e) {
    if (my !== reqSeq) return;
    frameErr = e.message;
  } finally {
    if (my === reqSeq) { loading = false; draw(); }
  }
}
const autoFrame = debounce(() => { if (root && root.isConnected && !S.playing) loadFrame(); }, 700);

function stage() {
  const st = h('div.lk-stage');
  if (!frame) {
    st.appendChild(h('div.lk-ph', loading ? h('span.lk-spin') : null, frameErr || (loading ? 'Rendering your frame in every look…' : 'Move the playhead onto a shot')));
    return st;
  }
  const lk = pick || planLook() || { name: 'natural', intensity: 1 };
  const before = h('img.lk-before', { src: frame.before_url, alt: '' });
  const after = h('img.lk-after', { src: lk.name === 'natural' ? frame.before_url : previewUrl(lk), alt: '' });
  const handle = h('div.lk-handle', h('span.lk-knob'));
  const box = h('div.lk-split', before, after, handle,
    h('span.lk-tag.l', 'Before'), h('span.lk-tag.r', `After · ${lookText(lk)}`));
  const setSplit = (f) => { split = clamp(f, 0, 1); after.style.clipPath = `inset(0 0 0 ${(split * 100).toFixed(2)}%)`; handle.style.left = `${(split * 100).toFixed(2)}%`; };
  setSplit(split);
  box.addEventListener('pointermove', (e) => { const r = box.getBoundingClientRect(); setSplit((e.clientX - r.left) / r.width); });
  box.addEventListener('pointerleave', () => { setSplit(0.5); LS.set('lk.split', 0.5); });
  after.addEventListener('load', () => box.classList.remove('busy'));
  before.addEventListener('load', () => { if (before.naturalWidth) box.style.aspectRatio = `${before.naturalWidth} / ${before.naturalHeight}`; });
  box.classList.add('busy');
  st.appendChild(box);
  const w = frame.where;
  st.appendChild(h('div.lk-cap', I('film', 12), `Your frame at ${tc(frame.t, fps())}${w ? ` · section ${w.sec + 1}, segment ${w.seg + 1}` : ''}`, h('span.grow'),
    h('button.btn.ghost.sm', { title: 'Render the gallery on the frame under the playhead', on: { click: () => loadFrame(true) } }, I('refresh', 12), 'Use playhead frame')));
  return st;
}

function gallery() {
  const g = h('div.lk-grid');
  const cur = pick || planLook() || { name: 'natural', intensity: 1 };
  const items = frame ? frame.items : CAT.map((c) => ({ ...c, url: null }));
  const whole = planLook();
  for (const it of items) {
    const on = cur.name === it.name;
    const applied = whole && whole.name === it.name;
    const tile = h('button.lk-tile' + (on ? '.on' : ''), { dataset: { look: it.name }, title: it.desc || it.label,
      on: { click: () => { pick = { name: it.name, intensity: on && pick ? pick.intensity : (whole && whole.name === it.name ? whole.intensity : 1) }; match = null; draw(); } } },
    h('div.lk-thumb', it.url ? h('img', { src: it.url, alt: '', loading: 'lazy' }) : h('span.lk-spin')),
    h('div.lk-name', h('span', it.label), it.kind === 'lut' ? h('span.lk-badge', 'LUT') : null, applied ? h('span.lk-badge.on', I('check', 10), 'In use') : null));
    g.appendChild(tile);
  }
  g.appendChild(h('div.lk-tile.lk-add', h('div.lk-thumb.lk-addbox', h('span', '+ .cube')),
    h('div.lk-name', h('span', 'Your own LUTs')),
    h('div.lk-addp', LUTS_DIR ? 'Drop .cube files into the project’s luts folder; they show up here.' : 'Drop .cube files into the luts folder.')));
  return g;
}

function camBlock() {
  const m = camMatch;
  if (!m || !m.available) return null;
  const mm = m.match;
  const line = mm ? (mm.disabled ? `Match left off: ${mm.why || ''}` :
    `Matched to the ARRI${mm.day ? ` (${mm.day})` : ''}: ΔE ${mm.de_before} → ${mm.de_after} · exposure ${mm.exposure_stops >= 0 ? '+' : ''}${mm.exposure_stops} st · sat ×${mm.sat}`)
    : 'No match fitted for this camera yet (cutroom cameras match)';
  return h('div.lk-cam',
    h('div', h('b', m.camera), ` · ${m.profile || 'unknown profile'}${m.profile_src === 'assumed' ? ' (assumed)' : ''}`),
    h('div.s', line),
    h('div.lk-cam-pair', h('div', h('img', { src: m.before_url, alt: '' }), h('div.s', 'Camera transform')),
      h('div', h('img', { src: m.after_url, alt: '' }), h('div.s', '+ match to ARRI'))));
}

function side() {
  const lk = pick || planLook() || { name: 'natural', intensity: 1 };
  const c = CAT.find((x) => x.name === lk.name) || {};
  const whole = planLook();
  const sg = segUnderPlayhead();
  const segLk = sg && sg.seg ? normLook(sg.seg.look) : null;
  const k = h('input.lk-range', { type: 'range', min: 0, max: 100, step: 5, value: Math.round(lk.intensity * 100), disabled: lk.name === 'natural' });
  const kv = h('span.lk-kv.mono', pct(lk.intensity));
  const upd = debounce(() => draw(), 260);
  k.addEventListener('input', () => { pick = { name: lk.name, intensity: +k.value / 100 }; kv.textContent = pct(pick.intensity); upd(); });
  k.addEventListener('keydown', (e) => e.stopPropagation());
  const s = h('div.lk-side',
    h('div.lk-title', h('b', c.label || lookLabel(lk.name)), c.kind === 'lut' ? h('span.lk-badge', 'LUT') : null),
    h('div.lk-desc', c.desc || ''),
    h('label.lk-klabel', 'Intensity', h('span.grow'), kv), k,
    h('div.lk-acts',
      h('button.btn.primary', { disabled: !S.plan, on: { click: () => applyWhole(lk) } }, I('check', 12), lk.name === 'natural' ? 'Use no look on the whole edit' : 'Apply to whole edit'),
      h('button.btn', { disabled: !sg || !sg.seg, title: sg ? `Only the segment under the playhead (${sg.piece.label || ''})` : 'Move the playhead onto a segment', on: { click: () => applySeg(sg.key, lk) } }, I('seg', 12), 'Apply to this segment')),
    h('div.lk-now',
      h('div', h('span', 'Whole edit'), h('b', lookText(whole))),
      h('div', h('span', 'This segment'), h('b', segLk ? lookText(segLk) : sg ? 'Same as whole edit' : '–'),
        segLk ? h('button.cm-x.lk-clear', { title: 'Follow the whole edit again', html: ico('x', 10), on: { click: () => applySeg(sg.key, null) } }) : null)),
    matchBox(), camBlock());
  return s;
}

function matchBox() {
  const box = h('div.lk-match');
  const ready = (refs || []).filter((r) => r.status === 'analyzed');
  const selRef = h('select.inp', { disabled: !ready.length });
  if (!ready.length) selRef.appendChild(h('option', { value: '' }, refs === null ? 'Loading references…' : 'No measured reference yet'));
  for (const r of ready) selRef.appendChild(h('option', { value: r.id }, r.title || r.id));
  const want = LS.get('lk.ref', null);
  if (want && ready.some((r) => r.id === want)) selRef.value = want;
  selRef.addEventListener('change', () => LS.set('lk.ref', selRef.value));
  selRef.addEventListener('keydown', (e) => e.stopPropagation());
  const go = h('button.btn', { disabled: !ready.length || !frame, on: { click: async () => {
    go.disabled = true; go.textContent = 'Measuring…';
    try {
      match = await jget(`/api/looks/match?ref=${enc(selRef.value)}&plan=${enc(frame.plan)}&t=${frame.t.toFixed(3)}`);
      pick = { name: match.pick.name, intensity: match.pick.intensity };
    } catch (e) { toast(`Match: ${e.message}`, 'error'); }
    draw();
  } } }, I('match', 12), 'Match a reference’s colour');
  box.append(h('div.lk-mh', 'Match a reference'), h('div.pair', selRef), go);
  if (!ready.length && refs !== null) box.appendChild(h('div.lk-desc', 'Add a reference video (Reference tab) and let it measure; then its colour can be matched here.'));
  return box;
}

function matchView() {
  const m = match;
  const stat = (o) => o ? `luma ${Math.round(o.luma)} · contrast ${Math.round(o.contrast)} · sat ${Math.round(o.sat)} · warmth ${o.warmth > 0 ? '+' : ''}${Math.round(o.warmth)}` : '';
  const col = (title, url, sub) => h('div.lk-mcol', h('div.lk-mimg', url ? h('img', { src: url, alt: '' }) : h('div.lk-ph', 'no frame')), h('div.lk-mt', title), h('div.lk-ms', sub));
  return h('div.lk-stage.lk-mstage',
    h('div.lk-mrow',
      col(`Reference · ${m.scope}`, m.ref_thumb, stat(m.target)),
      col('Yours now', m.before_url, stat(m.yours)),
      col(`Yours with ${m.pick.label} ${pct(m.pick.intensity)}`, m.preview_url, m.pick.name === 'natural' ? 'Already the closest' : `${Math.round((1 - m.distance / Math.max(1e-6, m.base_distance)) * 100)}% closer to the reference`)),
    h('div.lk-cap', h('span', `Closest look: ${m.pick.label} at ${pct(m.pick.intensity)}`), h('span.grow'),
      h('button.btn.ghost.sm', { on: { click: () => { match = null; draw(); } } }, 'Back to gallery'),
      h('button.btn.primary.sm', { on: { click: () => applyWhole(m.pick) } }, I('check', 12), 'Apply to whole edit')));
}

function draw() {
  if (!root || !root.isConnected) return;
  root.innerHTML = '';
  root.classList.add('lk-root');
  if (!S.plan) { root.appendChild(h('div.empty', h('span', { html: ico('look', 18) }), h('h4', 'Looks'), h('p', 'Open a plan to try looks on your own footage.'))); return; }
  if (match) { root.classList.add('matching'); root.append(matchView(), side()); return; }
  root.classList.remove('matching');
  root.append(stage(), h('div.lk-gwrap', h('div.lk-ghead', I('look', 13), h('b', 'Looks'), h('span.lk-gsub', frame ? 'on your frame' : ''), h('span.grow'),
    loading ? h('span.lk-gsub', h('span.lk-spin'), 'rendering…') : null), gallery()), side());
}

export function renderLooks(el) {
  root = el;
  if (!CAT.length) loadCatalogue().then(() => draw());
  if (refs === null) jget('/api/refs').then((r) => { refs = r.refs || []; draw(); }).catch(() => { refs = []; draw(); });
  draw();
  loadFrame();
}

export function initLooks() {
  loadCatalogue();
  on('playhead', () => { applyProgramFilter(); autoFrame(); });
  on('plan', ({ by } = {}) => { applyProgramFilter(); if (by === 'open') { frame = null; pick = null; match = null; } if (root && root.isConnected) { draw(); } });
  on('programMode', applyProgramFilter);
  on('looksCatalogue', () => { applyProgramFilter(); emit('selection'); });
  on('playing', (p) => { if (!p) autoFrame(); });
  window.__looks = { cssFilter, loadFrame, get frame() { return frame; }, get pick() { return pick; }, select: (name, k = 1) => { pick = { name, intensity: k }; draw(); }, applyWhole, applySeg };
}
