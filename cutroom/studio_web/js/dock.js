// Bottom dock: Inspector · Markers · QA · Jobs · Overnight.
import { S, on, emit, op, selectedPieces, segmentOf, KIND_COLOR, fps, setPlayhead, loadMarkers, piecesOfSeg, normOvernight } from './state.js';
import { api, media } from './api.js';
import { h, icon, tc, md, toast, eta, ago, LS, clamp } from './util.js';
import { tabs } from './layout.js';
import { renderComments } from './comments.js';
import { renderLooks, lookField } from './looks.js';
import { renderFactory } from './factory.js';

let body, current = 'inspector';
const pane = () => { body.innerHTML = ''; const p = h('div.dock-pane'); body.appendChild(p); return p; };
const stopKeys = (el) => { el.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Escape') e.target.blur(); }); return el; };

// ================================================================ inspector
function num(v) { return v === '' || v === null || v === undefined ? null : +v; }
function field(label, input) { return h('div.fld', h('label', label), input); }
function inp(value, onCommit, { type = 'text', step, width, mono = true, placeholder } = {}) {
  const el = stopKeys(h('input.inp' + (mono && type === 'number' ? '.mono' : ''), { type, step, value: value ?? '', placeholder, style: width ? { width } : null }));
  el.addEventListener('change', () => onCommit(type === 'number' ? num(el.value) : el.value));
  el.addEventListener('keydown', (e) => { if (e.key === 'Enter') el.blur(); });
  return el;
}
function sel(value, opts, onCommit) {
  const el = stopKeys(h('select.inp', null, opts.map((o) => h('option', { value: o }, o))));
  el.value = value ?? opts[0];
  el.addEventListener('change', () => onCommit(el.value));
  return el;
}
function chk(value, onCommit) {
  const el = h('input', { type: 'checkbox', checked: !!value });
  el.addEventListener('change', () => onCommit(el.checked));
  return h('div', { style: { display: 'flex', alignItems: 'center', height: '22px' } }, el);
}

function renderInspector() {
  const p0 = pane();
  const sel0 = selectedPieces();
  const v1 = sel0.filter((p) => p.track === 'V1');
  const pick = v1[0] || sel0[0];
  if (!S.plan) { p0.appendChild(h('div.empty', h('h4', 'Nothing to inspect'), h('p', 'Open a plan, then click a segment on the timeline.'))); return; }
  if (!pick) {
    p0.appendChild(planStyle());
    return;
  }
  const segKeys = new Set(v1.map((p) => `${p.sec}:${p.seg}`));
  const seg = segmentOf(pick);
  if (!seg) { p0.appendChild(h('div.empty', h('p', 'Selected piece has no segment in the draft.'))); return; }
  const key = [pick.sec, pick.seg];
  const set = (fields) => op({ op: 'set', seg: key, fields });
  const secObj = S.plan.draft.sections[pick.sec] || {};
  const ps = piecesOfSeg(pick.sec, pick.seg);
  const tlDur = ps.reduce((a, p) => a + (p.end - p.start), 0);
  const col1 = h('div.insp-col',
    h('div.seg-title', h('span.sw', { style: { background: KIND_COLOR[seg.type] || '#555' } }), h('b', seg.type),
      h('span', `${secObj.name || 'section ' + (pick.sec + 1)} · #${pick.seg + 1} · ${tlDur.toFixed(2)}s on timeline${ps.length > 1 ? ` · ${ps.length} pieces` : ''}${segKeys.size > 1 ? ` · ${segKeys.size} selected (editing first)` : ''}`)));
  const t = seg.type;
  if (seg.clip !== undefined) col1.appendChild(field('Clip', h('div.pair', inp(seg.clip, (v) => set({ clip: v }), { mono: true }),
    h('button.ib.sm', { title: 'Open in Source', html: icon('film', 13), on: { click: () => emit('loadSource', { id: pick.clip || seg.clip, in: seg.in ?? null, out: seg.out ?? null, t: seg.in ?? seg.t ?? 0 }) } }))));
  if (t !== 'black' && t !== 'title') col1.appendChild(field('Look', lookField(seg.look, (v) => set({ look: v }), { inherit: true })));
  if (t === 'talk' || t === 'broll') {
    col1.appendChild(field('In / Out', h('div.pair', inp(seg.in, (v) => set({ in: v }), { type: 'number', step: 0.04 }), inp(seg.out, (v) => set({ out: v }), { type: 'number', step: 0.04 }))));
    col1.appendChild(field('Speed', inp(seg.speed ?? 1, (v) => set({ speed: v }), { type: 'number', step: 0.05 })));
  }
  if (t === 'talk') {
    col1.appendChild(field('Tighten', h('div.pair', chk(seg.tighten !== false, (v) => set({ tighten: v })), h('span', { style: { color: 'var(--text-3)', fontSize: '11px' } }, 'gap'), inp(seg.gap ?? 0.35, (v) => set({ gap: v }), { type: 'number', step: 0.05 }))));
    col1.appendChild(field('Captions', chk(seg.captions !== false, (v) => set({ captions: v }))));
  }
  if (t === 'broll') col1.appendChild(field('Audio', sel(seg.audio || 'music', ['music', 'natural', 'mix'], (v) => set({ audio: v }))));
  if (t === 'title') {
    col1.appendChild(field('Text', inp(seg.text, (v) => set({ text: v }), { mono: false })));
    col1.appendChild(field('Sub', inp(seg.sub, (v) => set({ sub: v }), { mono: false })));
  }
  if (['title', 'black', 'still'].includes(t)) col1.appendChild(field('Duration', inp(seg.dur, (v) => set({ dur: v }), { type: 'number', step: 0.1 })));
  if (t === 'still') {
    col1.appendChild(field('Frame at', inp(seg.t, (v) => set({ t: v }), { type: 'number', step: 0.04 })));
    col1.appendChild(field('Zoom', inp(seg.zoom ?? 1.08, (v) => set({ zoom: v }), { type: 'number', step: 0.01 })));
  }
  if (t === 'montage') {
    col1.appendChild(field('Beat sync', h('div.pair', chk(seg.beat_sync !== false, (v) => set({ beat_sync: v })), h('span', { style: { color: 'var(--text-3)', fontSize: '11px' } }, 'beats/cut'), inp(seg.beats_per_cut ?? 1, (v) => set({ beats_per_cut: v }), { type: 'number', step: 1 }))));
    col1.appendChild(field('Shot len', h('div.pair', inp(seg.min_shot ?? 0.4, (v) => set({ min_shot: v }), { type: 'number', step: 0.1 }), inp(seg.max_shot ?? 1.6, (v) => set({ max_shot: v }), { type: 'number', step: 0.1 }))));
    col1.appendChild(field('Shots', h('span', { style: { color: 'var(--text-2)' } }, `${(seg.shots || []).length} shots`)));
  }
  if (t === 'matchcut') {
    col1.appendChild(field('A / B at', h('div.pair', inp(seg.a && seg.a.t, (v) => set({ a: { ...seg.a, t: v } }), { type: 'number', step: 0.04 }), inp(seg.b && seg.b.t, (v) => set({ b: { ...seg.b, t: v } }), { type: 'number', step: 0.04 }))));
    col1.appendChild(field('Pre / post', h('div.pair', inp(seg.pre ?? 0.8, (v) => set({ pre: v }), { type: 'number', step: 0.1 }), inp(seg.post ?? 1.4, (v) => set({ post: v }), { type: 'number', step: 0.1 }))));
  }
  if (t !== 'black' && t !== 'title') {
    const cropV = Array.isArray(seg.crop) ? seg.crop.join(',') : (seg.crop || 'center');
    col1.appendChild(field('Crop', inp(cropV, (v) => set({ crop: /,/.test(v) ? v.split(',').map(Number) : v }), { mono: true, placeholder: 'center | face | x,y,w,h' })));
  }
  const note = stopKeys(h('textarea.inp', { rows: 2, placeholder: 'Why this segment exists', value: seg.note || '', style: { width: '100%' } }));
  note.addEventListener('change', () => set({ note: note.value }));
  col1.appendChild(field('Note', note));

  // cutaways
  const col2 = h('div.insp-col');
  if (t === 'talk' || t === 'broll') {
    const cas = (seg.cutaways || []).map((c) => ({ ...c }));
    const commit = () => set({ cutaways: cas });
    col2.appendChild(h('h5', 'Cutaways', h('span.grow'), h('button.btn.ghost', { title: 'Add the Source In/Out as a cutaway', on: { click: () => emit('cutawayFromSource') } }, h('span', { html: icon('plus', 12) }), 'From source')));
    if (!cas.length) col2.appendChild(h('div', { style: { color: 'var(--text-3)', fontSize: '11.5px' } }, 'None. Load b-roll in Source, set In/Out, then “From source” to cover this segment.'));
    else {
      const list = h('div.sub-list', h('div.sub-row.ca.hd', h('span', 'clip'), h('span', 'in'), h('span', 'at'), h('span', 'dur'), h('span')));
      cas.forEach((c, i) => list.appendChild(h('div.sub-row.ca',
        inp(c.clip, (v) => { c.clip = v; commit(); }),
        inp(c.in, (v) => { c.in = v; commit(); }, { type: 'number', step: 0.1 }),
        inp(c.at, (v) => { c.at = v; commit(); }, { type: 'number', step: 0.1 }),
        inp(c.dur, (v) => { c.dur = v; commit(); }, { type: 'number', step: 0.1 }),
        h('button.ib.sm.danger', { title: 'Remove', html: icon('x', 11), on: { click: () => { cas.splice(i, 1); commit(); } } }))));
      col2.appendChild(list);
    }
  } else col2.appendChild(h('h5', 'Cutaways'), h('div', { style: { color: 'var(--text-3)', fontSize: '11.5px' } }, 'Cutaways go on talk and b-roll segments.'));

  // overlays
  const col3 = h('div.insp-col');
  if (['talk', 'broll', 'montage'].includes(t)) {
    const ovs = (seg.overlays || []).map((o) => ({ ...o }));
    const commit = () => set({ overlays: ovs });
    col3.appendChild(h('h5', 'Overlays', h('span.grow'), h('button.btn.ghost', { on: { click: () => op({ op: 'add_overlay', seg: key, overlay: { kind: 'callout', text: 'Text', at: 0.3, dur: 2.0, position: 'auto' } }) } }, h('span', { html: icon('plus', 12) }), 'Add')));
    if (!ovs.length) col3.appendChild(h('div', { style: { color: 'var(--text-3)', fontSize: '11.5px' } }, 'None. Keep it to one short card per talking section.'));
    else {
      const list = h('div.sub-list', h('div.sub-row.ov.hd', h('span', 'kind'), h('span', 'text'), h('span', 'at'), h('span', 'dur'), h('span')));
      ovs.forEach((o, i) => list.appendChild(h('div.sub-row.ov',
        sel(o.kind, ['callout', 'lower_third', 'title', 'label'], (v) => { o.kind = v; commit(); }),
        inp(o.text, (v) => { o.text = v; commit(); }, { mono: false }),
        inp(o.at, (v) => { o.at = v; commit(); }, { type: 'number', step: 0.1 }),
        inp(o.dur, (v) => { o.dur = v; commit(); }, { type: 'number', step: 0.1 }),
        h('button.ib.sm', { title: 'Remove', html: icon('x', 11), on: { click: () => { ovs.splice(i, 1); commit(); } } }))));
      col3.appendChild(list);
    }
  }
  // section music
  const mu = secObj.music || null;
  col3.appendChild(h('h5', { style: { marginTop: '10px' } }, `Section music · ${secObj.name || ''}`));
  const songs = ((S.project && S.project.songs) || []).map((s) => s.id);
  const songTitle = Object.fromEntries(((S.project && S.project.songs) || []).map((s) => [s.id, s.title || s.id]));
  const setMusic = (fields) => op({ op: 'set_music', sec: pick.sec, music: fields === null ? null : { ...(mu || {}), ...fields } });
  const songSel = sel(mu ? mu.song : '(none)', ['(none)', ...songs.filter((s) => !mu || s !== mu.song), ...(mu && mu.song ? [mu.song] : [])], (v) => setMusic(v === '(none)' ? null : { song: v, offset: (mu && mu.offset) || 0 }));
  for (const o of songSel.options) if (songTitle[o.value]) o.textContent = songTitle[o.value];
  col3.appendChild(field('Song', songSel));
  if (mu) col3.appendChild(field('Offset / gain', h('div.pair', inp(mu.offset ?? 0, (v) => setMusic({ offset: v }), { type: 'number', step: 0.1 }), inp(mu.gain_db ?? -8, (v) => setMusic({ gain_db: v }), { type: 'number', step: 1 }))));
  p0.appendChild(h('div.insp', col1, col2, col3));
}

function planStyle() {
  const d = S.plan.draft || {}, st = d.style || {}, cap = st.captions || {}, out = d.output || {};
  const setStyle = (fields) => op({ op: 'set_style', fields });
  const c1 = h('div.insp-col',
    h('div.seg-title', h('b', d.title || S.plan.slug), h('span', `v${d.version ?? '?'} draft · ${(d.sections || []).length} sections · ${S.tl ? S.tl.duration.toFixed(1) : 0}s · ${out.width || '?'}×${out.height || '?'} ${out.fps || ''}fps`)),
    h('div', { style: { color: 'var(--text-3)', fontSize: '11.5px', marginBottom: '8px' } }, 'Nothing selected: these are the plan-wide settings. Click a segment on the timeline to edit it.'),
    field('Music / speech', sel(st.music_under_speech || 'mute', ['mute', 'duck', 'full'], (v) => setStyle({ music_under_speech: v }))));
  const c2 = h('div.insp-col', h('h5', 'Captions'),
    field('Enabled', chk(cap.enabled !== false, (v) => setStyle({ captions: { ...cap, enabled: v } }))),
    field('Size', inp(cap.size ?? 54, (v) => setStyle({ captions: { ...cap, size: v } }), { type: 'number', step: 2 })),
    field('Position', sel(cap.position || 'bottom', ['bottom', 'lower', 'top'], (v) => setStyle({ captions: { ...cap, position: v } }))),
    field('Max words', inp(cap.max_words ?? 5, (v) => setStyle({ captions: { ...cap, max_words: v } }), { type: 'number', step: 1 })),
    field('Case', sel(cap.case || 'sentence', ['sentence', 'lower', 'upper'], (v) => setStyle({ captions: { ...cap, case: v } }))));
  const gr = st.grade || {}, vo = st.voice || {};
  const c3 = h('div.insp-col', h('h5', 'Grade & voice'),
    field('LUT', inp(gr.lut || '', (v) => setStyle({ grade: { ...gr, lut: v } }), { mono: false, placeholder: 'none' })),
    field('Exposure', chk(gr.normalize_exposure !== false, (v) => setStyle({ grade: { ...gr, normalize_exposure: v } }))),
    field('Saturation', inp(gr.saturation ?? 1, (v) => setStyle({ grade: { ...gr, saturation: v } }), { type: 'number', step: 0.05 })),
    field('Look', lookField(gr.look, (v) => setStyle({ grade: { look: v } }))),
    field('Clean voice', chk(vo.clean !== false, (v) => setStyle({ voice: { ...vo, clean: v } }))));
  return h('div.insp', c1, c2, c3);
}

// ================================================================ markers
// Markers appear instantly (the server grabs a frame, which can take a moment); edits made before the
// server answers are queued and applied to the real id.
const pendingEdits = new Map();
async function addMarker() {
  if (!S.plan) { toast('Open a plan to add markers', 'warn'); return; }
  const tmp = { id: 'tmp' + Date.now(), plan: S.plan.slug, t: +S.playhead.toFixed(3), text: '', kind: 'note', status: 'open', pending: true };
  S.markers = [...S.markers, tmp].sort((a, b) => a.t - b.t);
  emit('markers');
  emit('showDock', 'markers');
  requestAnimationFrame(() => { const el = body.querySelector(`[data-mid="${tmp.id}"] input`); if (el) el.focus(); });
  try {
    const m = await api.addMarker({ plan: tmp.plan, t: tmp.t, text: '', kind: 'note' });
    const q = pendingEdits.get(tmp.id); pendingEdits.delete(tmp.id);
    const typed = body.querySelector(`[data-mid="${tmp.id}"] input`);
    const text = typed && typed.value ? typed.value : (q && q.text) || '';
    const fields = { ...(q || {}), ...(text ? { text } : {}) };
    if (Object.keys(fields).length) await api.patchMarker(m.id, fields);
    const ae = document.activeElement;
    const wasFocused = (typed && ae === typed) || !ae || ae === document.body;
    await loadMarkers();
    if (current === 'markers') renderMarkers();
    if (wasFocused) { const el = body.querySelector(`[data-mid="${m.id}"] input`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
  } catch (e) {
    S.markers = S.markers.filter((x) => x.id !== tmp.id); emit('markers');
    toast(`Marker: ${e.message}`, 'error');
  }
}
async function editMarker({ id, ...fields }) {
  if (String(id).startsWith('tmp')) {
    pendingEdits.set(id, { ...(pendingEdits.get(id) || {}), ...fields });
    const m = S.markers.find((x) => x.id === id); if (m) Object.assign(m, fields);
    emit('markers');
    return;
  }
  try { await api.patchMarker(id, fields); await loadMarkers(); } catch (e) { toast(`Marker: ${e.message}`, 'error'); }
}
function renderMarkers() {
  const p0 = pane();
  const ms = S.markers;
  const open = ms.filter((m) => m.status !== 'done');
  if (!ms.length) { p0.appendChild(h('div.empty', h('span', { html: icon('marker') }), h('h4', 'No markers'), h('p', 'Press M at the playhead to drop a note. Claude can fix them one by one or all at once.'))); return; }
  const tbl = h('table.tbl', h('thead', h('tr', h('th', { style: { width: '96px' } }, 'Time'), h('th', { style: { width: '54px' } }, 'Frame'), h('th', 'Note'), h('th', { style: { width: '60px' } }, 'Kind'),
    h('th', { style: { width: '220px', textAlign: 'right' } }, open.length > 1 ? h('button.btn', { on: { click: () => emit('sendChat', `Fix all open markers (${open.length}). Change exactly what each note asks, nothing else.`) } }, `Send all ${open.length} to Claude`) : ''))));
  const tb = h('tbody');
  for (const m of ms) {
    const txt = stopKeys(h('input.inp', { value: m.text || '', placeholder: 'Write what should change…' }));
    txt.addEventListener('change', () => editMarker({ id: m.id, text: txt.value }));
    txt.addEventListener('keydown', (e) => { if (e.key === 'Enter') txt.blur(); });
    tb.appendChild(h('tr', { class: m.status === 'done' ? 'done' : '', dataset: { mid: m.id } },
      h('td.tc.mono', { on: { click: () => { setPlayhead(m.t, 'markers'); emit('revealPlayhead'); } } }, tc(m.t, fps())),
      h('td', m.frame_url ? h('img.fr', { src: m.frame_url, alt: '', on: { click: () => lightbox(m.frame_url) } }) : ''),
      h('td', txt),
      h('td', h('span.pill.' + (m.kind === 'ref' ? 'blue' : 'warn'), m.kind || 'note')),
      h('td', h('div.acts',
        h('button.btn.ghost', { on: { click: () => editMarker({ id: m.id, status: m.status === 'done' ? 'open' : 'done' }) } }, h('span', { html: icon('check', 12) }), m.status === 'done' ? 'Reopen' : 'Done'),
        h('button.btn.ghost', { on: { click: () => emit('markerToClaude', m) } }, 'Send to Claude'),
        h('button.ib.sm', { title: 'Delete marker', html: icon('trash', 13), on: { click: async () => { try { await api.delMarker(m.id); await loadMarkers(); } catch (e) { toast(e.message, 'error'); } } } })))));
  }
  tbl.appendChild(tb);
  p0.appendChild(tbl);
}

// ================================================================ QA
async function openQA(r) {
  const file = typeof r === 'string' ? r : (r.qa || (r.file || '').replace(/\.mp4$/, '.qa.json'));
  try {
    const rep = await api.json(media.render(file));
    S.qa = { file, render: typeof r === 'string' ? null : r.file, report: rep };
    emit('qa');
    emit('showDock', 'qa');
  } catch (e) { toast(`Couldn't open QA report ${file}: ${e.message}`, 'error'); }
}
function sheetUrl(p) {
  const s = String(p);
  const i = s.lastIndexOf('/renders/');
  return media.render(i >= 0 ? s.slice(i + 9) : s.split('/').slice(-2).join('/'));
}
function renderQA() {
  const p0 = pane();
  const q = S.qa;
  if (!q) {
    const latest = ((S.project && S.project.renders) || []).filter((r) => r.qa).sort((a, b) => (b.created || 0) - (a.created || 0))[0];
    p0.appendChild(h('div.empty', h('span', { html: icon('check') }), h('h4', 'No QA report open'),
      h('p', 'Every render gets machine-checked: silence holes, frozen or black frames, off-beat cuts, captions over faces.'),
      latest ? h('div.acts', h('button.btn', { on: { click: () => openQA(latest) } }, `Open QA for ${latest.file}`)) : null));
    return;
  }
  const rep = q.report, sum = rep.summary || {};
  const side = h('div.qa-side',
    h('div.big', h('span.pill.' + (sum.status === 'pass' ? 'ok' : sum.status || 'warn'), (sum.status || '?').toUpperCase()), h('span', (rep.video || q.file).split('/').pop())),
    h('div', { style: { color: 'var(--text-2)' } }, `${sum.fail ?? 0} fail · ${sum.warn ?? 0} warn · ${sum.shots ?? '?'} shots`),
    h('div', { style: { color: 'var(--text-3)', fontSize: '11px' } }, `${sum.loudness_lufs !== undefined && sum.loudness_lufs !== null ? sum.loudness_lufs.toFixed(1) + ' LUFS · ' : ''}${sum.true_peak_dbtp !== undefined && sum.true_peak_dbtp !== null ? sum.true_peak_dbtp.toFixed(1) + ' dBTP · ' : ''}${rep.duration ? rep.duration.toFixed(1) + 's' : ''}`),
    q.render ? h('button.btn', { on: { click: () => emit('playRender', q.render) } }, h('span', { html: icon('play', 12) }), 'Play this render') : null,
    (sum.sheets || []).length ? h('div', { style: { color: 'var(--text-3)', fontSize: '11px' } }, 'Contact sheets') : null,
    h('div.sheets', (sum.sheets || []).slice(0, 12).map((s) => h('img', { src: sheetUrl(s), alt: '', on: { click: () => lightbox(sheetUrl(s)) } }))));
  const rows = [];
  for (const c of rep.checks || []) {
    if (!c.issues || !c.issues.length) { if (c.status !== 'pass') rows.push({ c, i: { severity: c.status, msg: c.detail || c.title } }); continue; }
    for (const i of c.issues) rows.push({ c, i });
  }
  const sevO = { fail: 0, warn: 1, info: 2, pass: 3 };
  rows.sort((a, b) => (sevO[a.i.severity] ?? 4) - (sevO[b.i.severity] ?? 4) || (a.i.t ?? 1e9) - (b.i.t ?? 1e9));
  const tbl = h('table.tbl', h('thead', h('tr', h('th', { style: { width: '90px' } }, 'Time'), h('th', { style: { width: '150px' } }, 'Check'), h('th', 'Issue'), h('th', { style: { width: '60px' } }, ''))));
  const tb = h('tbody');
  if (!rows.length) tb.appendChild(h('tr', h('td', { colSpan: 4, style: { color: 'var(--text-3)' } }, 'All checks passed.')));
  for (const { c, i } of rows.slice(0, 400)) {
    tb.appendChild(h('tr',
      h('td.tc.mono', { on: { click: () => { if (i.t !== null && i.t !== undefined) { if (q.render) emit('playRender', q.render); setPlayhead(i.t, 'qa'); emit('revealPlayhead'); } } } }, i.t !== null && i.t !== undefined ? tc(i.t, fps()) : '–'),
      h('td', h('span.sev.' + i.severity), c.title || c.id),
      h('td', { style: { color: 'var(--text-2)' } }, i.msg || ''),
      h('td', (i.evidence || []).length ? h('button.btn.ghost', { on: { click: () => lightbox(sheetUrl(i.evidence[0])) } }, 'frame') : '')));
  }
  tbl.appendChild(tb);
  p0.appendChild(h('div.qa-wrap', side, h('div.qa-main', tbl)));
}

// ================================================================ jobs
function renderJobs() {
  const p0 = pane();
  const jobs = [...S.jobs.values()].sort((a, b) => (b.started || 0) - (a.started || 0));
  if (!jobs.length) { p0.appendChild(h('div.empty', h('span', { html: icon('render') }), h('h4', 'No jobs'), h('p', 'Renders, QA runs and the overnight pass show progress here.'))); return; }
  const wrap = h('div.jobs');
  for (const j of jobs) {
    const pct = clamp((+j.progress || 0) * 100, 0, 100);
    const st = j.status || 'queued';
    wrap.appendChild(h('div.job',
      h('span.kind', j.kind || 'job'),
      h('span.lbl', { title: j.label || j.id }, j.label || j.id),
      h('div.bar.' + (st === 'error' ? 'bad' : st === 'done' ? 'ok' : ''), h('i', { style: { width: pct + '%' } })),
      h('span.st.' + st, st === 'running' ? `${Math.round(pct)}%` : st),
      h('span.st', st === 'running' && j.eta_s ? eta(j.eta_s) : (j.started ? ago(j.started) : '')),
      j.log_tail ? h('pre', String(j.log_tail).split('\n').slice(-4).join('\n')) : null));
  }
  p0.appendChild(wrap);
}

// ================================================================ overnight + governor
function gauge(label, frac, value, limitFrac, warn, level) {
  const cls = level || (frac === null ? '' : warn ? 'bad' : frac > 0.8 ? 'warn' : 'ok');
  return h('div.gauge', h('span.l', label),
    h('div.bar.' + cls, h('i', { style: { width: clamp((frac || 0) * 100, 0, 100) + '%' } }), limitFrac !== null && limitFrac !== undefined ? h('b', { style: { left: clamp(limitFrac * 100, 0, 100) + '%' } }) : null),
    h('span.v', value));
}
function renderOvernight() {
  const p0 = pane();
  const g = S.governor || {}, o = S.overnight || {};
  const running = !!o.running;
  const c1 = h('div.on-col', h('h5', 'Understand library overnight'),
    h('div', { style: { color: 'var(--text-2)', fontSize: '11.5px', lineHeight: 1.5 } }, 'Indexes, transcribes, tags and analyses the whole library in the background, throttled so the machine stays usable. Safe to stop and resume.'),
    h('div', { style: { display: 'flex', gap: '6px', marginTop: '4px' } },
      h('button.tb-btn' + (running ? '' : '.primary'), { on: { click: async () => { try { await api.overnight(running ? 'stop' : 'start'); S.overnight = normOvernight(await api.overnight('status')); renderOvernight(); emit('overnightPoll'); } catch (e) { toast(e.message, 'error'); } } } }, h('span', { html: icon(running ? 'stop' : 'moon', 13) }), running ? 'Stop' : 'Start overnight pass'),
      running && o.started ? h('span', { style: { color: 'var(--text-3)', fontSize: '11px', alignSelf: 'center' } }, `started ${ago(o.started)}${o.eta_s ? ' · ETA ' + eta(o.eta_s) : ''}`) : null));
  if (o.unavailable) c1.appendChild(h('div.throttle', `Overnight isn't available: ${o.unavailable}`));
  else if (!running && o.status) c1.appendChild(h('div', { style: { color: 'var(--text-3)', fontSize: '11px' } }, `Last run ${o.status}${o.ended ? ' ' + ago(o.ended) : ''}${o.last_error ? ' · ' + o.last_error : ''}`));
  for (const s of o.stages || []) {
    const frac = s.total ? s.done / s.total : (s.status === 'done' ? 1 : 0);
    c1.appendChild(h('div.stage', h('span.n', s.name), h('div.bar.' + (s.status === 'done' ? 'ok' : ''), h('i', { style: { width: frac * 100 + '%' } })),
      h('span.c', s.total ? `${(s.done || 0).toLocaleString()}/${s.total.toLocaleString()}` : s.status || '')));
  }
  const load = g.load || {}, ram = g.ram || {}, disk = g.disk || {};
  const loadNow = load.now ?? g.load_now ?? (typeof g.load === 'number' ? g.load : null);
  const loadLim = load.limit ?? g.load_limit ?? null;
  const loadMax = Math.max(loadLim || 0, load.cores || 0, loadNow || 0) * 1.25 || 1;
  const c2 = h('div.on-col', h('h5', 'Machine', h('span.pill.' + (g.health || 'ok'), g.health || '–')));
  if (loadNow !== null) c2.appendChild(gauge('Load', loadNow / loadMax, `${(+loadNow).toFixed(2)}${loadLim ? ' / ' + loadLim : ''}`, loadLim ? loadLim / loadMax : null, loadLim && loadNow > loadLim));
  const freeLevel = (free, min) => (!min ? 'ok' : free < min ? 'bad' : free < min * 1.5 ? 'warn' : 'ok');
  if (ram.free_gb !== undefined && ram.free_gb !== null) c2.appendChild(gauge('Free RAM', ram.total_gb ? ram.free_gb / ram.total_gb : null, `${(+ram.free_gb).toFixed(1)} GB`, ram.total_gb && ram.min_free_gb ? ram.min_free_gb / ram.total_gb : null, false, freeLevel(ram.free_gb, ram.min_free_gb)));
  if (disk.free_gb !== undefined && disk.free_gb !== null) {
    const scale = disk.total_gb || Math.max((disk.min_free_gb || 20) * 5, disk.free_gb * 1.1);
    c2.appendChild(gauge('Free disk', disk.free_gb / scale, `${Math.round(disk.free_gb)} GB`, disk.min_free_gb ? disk.min_free_gb / scale : null, false, freeLevel(disk.free_gb, disk.min_free_gb)));
  }
  for (const s of g.slots || []) {
    const boxes = h('span.boxes'); for (let k = 0; k < (s.max || 0); k++) boxes.appendChild(h('i', { class: k < (s.used || 0) ? 'u' : '' }));
    c2.appendChild(h('div.slot', h('span', { style: { width: '70px', color: 'var(--text-3)' } }, s.name), boxes,
      h('span.who', (s.holders || []).map((x) => x.job || x.pid || x).join(', ') || 'idle')));
  }
  if (g.throttle) c2.appendChild(h('div.throttle', `Throttled: ${g.throttle}`));
  if (!Object.keys(g).length) c2.appendChild(h('div', { style: { color: 'var(--text-3)' } }, 'Waiting for governor status…'));
  const c3 = h('div.on-col', h('h5', 'Morning report'));
  if (o.report_md) c3.appendChild(h('div.report', { html: md(o.report_md, { links: false }) }));
  else c3.appendChild(h('div', { style: { color: 'var(--text-3)', fontSize: '11.5px' } }, 'The report (REPORT.md) appears here when a pass finishes: what was indexed, what failed, what to look at first.'));
  p0.appendChild(h('div.on-wrap', c1, c2, c3));
}

// ================================================================ story (beat sheet)
const KIND_WORDS = { broll: 'B-roll', montage: 'Montage shot', matchcut: 'Match cut', still: 'Freeze frame', black: 'Black', title: 'Title', cutaway: 'Cutaway', talk: 'Talking' };
function storyRows() {
  if (!S.tl) return [];
  const caps = S.tl.captions;
  return S.tl.v1.map((p) => {
    const sec = S.tl.sections.find((s) => p.start >= s.start - 1e-3 && p.start < s.end) || null;
    const said = caps.filter((c) => c.t0 < p.end - 0.05 && c.t1 > p.start + 0.05).map((c) => c.text).join(' ');
    const seg = segmentOf(p) || {};
    let line = said ? `“${said}”` : null;
    if (!line) line = p.kind === 'title' ? `Title: ${seg.text || p.label || ''}` : (KIND_WORDS[p.kind] || p.kind) + (seg.note ? ` · ${seg.note}` : '');
    return { p, sec, line, spoken: !!said, clip: p.clip_rel ? p.clip_rel.split('/').pop().replace(/\.[^.]+$/, '') : (p.clip || '') };
  });
}
let storyNow = null;
function renderStory() {
  const p0 = pane();
  if (!S.plan || !S.tl || !S.tl.v1.length) { p0.appendChild(h('div.empty', h('h4', 'No story yet'), h('p', 'Once there’s an edit, this reads it back as a beat sheet: what’s said and shown, shot by shot.'))); return; }
  const tbl = h('table.tbl.story', h('thead', h('tr', h('th', 'Time'), h('th', 'Section'), h('th', 'What happens'), h('th', ''), h('th', 'Clip'))));
  const tb = h('tbody');
  let lastSec = null;
  for (const r of storyRows()) {
    const secName = r.sec ? r.sec.name : '';
    const tr = h('tr', { dataset: { pid: r.p.id }, class: secName !== lastSec ? 'secstart' : '' },
      h('td.tc.mono', tc(r.p.start, fps())),
      h('td.sec', secName !== lastSec ? secName : ''),
      h('td.line' + (r.spoken ? '' : '.kind'), h('span.sw', { style: { background: KIND_COLOR[r.p.kind] || '#555', marginRight: '8px' } }), r.line),
      h('td.th', r.p.clip ? h('img', { src: media.thumb(r.p.clip, (r.p.in || 0) + Math.min(0.5, (r.p.end - r.p.start) / 2)), alt: '', loading: 'lazy' }) : ''),
      h('td.clip', r.clip));
    tr.addEventListener('click', () => { setPlayhead(r.p.start + 0.001, 'story'); emit('revealPlayhead'); });
    tb.appendChild(tr);
    lastSec = secName;
  }
  tbl.appendChild(tb);
  p0.appendChild(tbl);
  storyNow = null; markStoryNow();
}
function markStoryNow() {
  if (current !== 'story' || !S.tl) return;
  const p = S.tl.v1.find((q) => S.playhead >= q.start && S.playhead < q.end);
  const id = p ? p.id : null;
  if (id === storyNow) return;
  const old = body.querySelector('tr.now'); if (old) old.classList.remove('now');
  storyNow = id;
  if (!id) return;
  const tr = body.querySelector(`tr[data-pid="${CSS.escape(id)}"]`);
  if (tr) { tr.classList.add('now'); if (!S.playing || true) { const pr = body.querySelector('.dock-pane'); const r = tr.getBoundingClientRect(), q = pr.getBoundingClientRect(); if (r.top < q.top + 24 || r.bottom > q.bottom) pr.scrollTop += r.top - q.top - 60; } }
}

// ================================================================ lightbox
export function lightbox(url) {
  const lb = document.getElementById('lightbox');
  lb.innerHTML = ''; lb.appendChild(h('img', { src: url, alt: '' }));
  lb.hidden = false;
  lb.onclick = () => (lb.hidden = true);
}

// ================================================================ init
const RENDER = { inspector: renderInspector, story: renderStory, markers: renderMarkers, comments: () => renderComments(pane()), looks: () => renderLooks(pane()), factory: () => renderFactory(pane()), qa: renderQA, jobs: renderJobs, overnight: renderOvernight };
function show(name) { if (!RENDER[name]) name = 'inspector'; current = name; RENDER[name](); }
export function initDock(el) {
  body = el;
  const t = tabs(document.getElementById('p-dock'), 'dock', show);
  t.set(t.initial === 'markers' ? 'comments' : t.initial);
  const badges = () => {
    const om = S.markers.filter((m) => m.status !== 'done').length;
    const bm = document.getElementById('badge-markers'); bm.textContent = om || ''; bm.className = 'badge' + (om ? ' hot' : '');
    const running = [...S.jobs.values()].filter((j) => j.status === 'running' || j.status === 'queued').length;
    document.getElementById('badge-jobs').textContent = running || '';
    const qa = S.qa && S.qa.report && S.qa.report.summary;
    const bq = document.getElementById('badge-qa'); bq.textContent = qa && qa.fail ? qa.fail : ''; bq.className = 'badge' + (qa && qa.fail ? ' bad' : '');
  };
  let insTimer = null;
  on('selection', () => { if (current === 'inspector') { clearTimeout(insTimer); insTimer = setTimeout(renderInspector, 30); } });
  on('plan', () => { if (current === 'inspector') { const a = document.activeElement; if (!(a && body.contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName))) renderInspector(); } });
  on('plan', () => { if (current === 'story') renderStory(); });
  let storyT = 0;
  on('playhead', () => { if (current === 'story' && !storyT) storyT = setTimeout(() => { storyT = 0; markStoryNow(); }, 120); });
  on('markers', () => { badges(); if (current === 'markers') { const a = document.activeElement; if (!(a && body.contains(a) && a.tagName === 'INPUT')) renderMarkers(); } });
  on('jobs', () => { badges(); if (current === 'jobs') renderJobs(); });
  on('qa', () => { badges(); if (current === 'qa') renderQA(); });
  on('governor', () => { if (current === 'overnight') renderOvernight(); });
  on('overnight', () => { if (current === 'overnight') renderOvernight(); });
  on('project', () => { if (current === 'qa' && !S.qa) renderQA(); });
  on('showDock', (n) => t.set(n === 'markers' ? 'comments' : n));
  on('openQA', openQA);
  void addMarker; // M opens the comment composer (comments.js)
  on('markerEdit', editMarker);
  badges();
  void LS;
}
