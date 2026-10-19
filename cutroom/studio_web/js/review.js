// Review workspace (Program monitor → Review tab): a factory version side by side with the previous version
// or the reference (locked playback), section-pair chips, the delivery card (note → change → [[t:]] → proof),
// the changed-spots sheet, frame notes with their pictures, option tiles, and the verdicts:
// APPROVE (recorded by code; a full-quality render + QA follow, nothing is published), REJECT (reason required),
// NOTES (your words + the open comments go to the factory).
import { S, on, emit, loadMarkers } from './state.js';
import { api } from './api.js';
import { h, icon, toast, LS } from './util.js';
import { questionEl, sendToFactory } from './factory.js';

const RV = { open: false, data: null, cur: null, cmp: LS.get('rv.cmp', 'prev'), lock: true, refAudio: false, busy: false };
let ws, va, vb, la, lb, side, hdr, verdictBox, tilesEl;
const mm = (t) => { t = Math.max(0, +t || 0); return `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`; };

function cur() { return RV.data && RV.data.versions.find((v) => v.version === RV.cur); }
function prev() {
  const vs = (RV.data && RV.data.versions) || [];
  const i = vs.findIndex((v) => v.version === RV.cur);
  return i >= 0 ? vs.slice(i + 1).find((v) => v.render_url) : null;
}

async function load() {
  if (!S.plan) { RV.data = { versions: [], refs: [], pairs: [], orders: [], variants: [] }; draw(); return; }
  try {
    RV.data = await api.fx.versions(S.plan.slug);
    if (!RV.cur || !RV.data.versions.some((v) => v.version === RV.cur)) RV.cur = (RV.data.versions.find((v) => v.render_url) || RV.data.versions[0] || {}).version;
  } catch (e) { toast(`Review: ${e.message}`, 'error'); RV.data = { versions: [], refs: [], pairs: [], orders: [], variants: [] }; }
  draw();
}

function setSrc(v, url) { if (url && v.getAttribute('src') !== url) { v.src = url; v.load(); } else if (!url) { v.removeAttribute('src'); v.load(); } }
function sync(from, to) {
  if (!RV.lock || !to.src) return;
  if (Math.abs(to.currentTime - from.currentTime) > 0.08) to.currentTime = Math.min(from.currentTime, (to.duration || 1e9) - 0.05);
}
function seek(t) { va.currentTime = t; if (RV.lock && vb.src) vb.currentTime = t; }

function players() {
  const c = cur(), p = prev();
  const refs = RV.data.refs || [];
  setSrc(va, c && c.render_url);
  la.textContent = c ? `v${c.version}${c.theme ? ' · ' + c.theme.replace(/^v\d+:\s*/, '') : ''}` : 'no render yet';
  if (RV.cmp === 'ref' && refs.length) { setSrc(vb, refs[0].proxy_url); lb.textContent = `reference · ${refs[0].title || refs[0].id}`; }
  else if (p) { setSrc(vb, p.render_url); lb.textContent = `v${p.version} (previous)`; }
  else { setSrc(vb, null); lb.textContent = RV.cmp === 'ref' ? 'no reference measured yet' : 'no earlier render'; }
  vb.muted = !(RV.cmp === 'ref' && RV.refAudio);
}

function cardEl(c) {
  const cd = c && c.card;
  if (!c) return h('div.rv-none', 'No version yet.');
  const badges = h('div.rv-badges',
    h('span.rv-b.' + (cd ? (cd.badges.machine_qa === 'pass' ? 'ok' : 'bad') : 'na'), 'machine QA ' + (cd ? cd.badges.machine_qa : '–')),
    h('span.rv-b.' + (cd && cd.badges.critic === 'pass' ? 'ok' : cd && cd.badges.critic === 'hard' ? 'bad' : 'na'), 'critic ' + (cd ? (cd.badges.critic || 'skipped') : '–')),
    h('span.rv-b.' + (c.approved ? 'ok' : 'na'), c.approved ? (c.conditional ? 'approved (with a change)' : 'approved') : 'not approved'),
    h('span.rv-b.na', c.final_render ? 'full-quality render ready' : 'not exported'));
  if (!cd) return h('div.rv-card', badges, h('div.rv-none', `v${c.version} was saved by ${c.by || 'you'}, not by the factory: no card.`));
  const rows = h('div.rv-rows', cd.rows.map((r) => h('div.rv-row',
    h('div.rv-said', h('span.rv-n', r.note), '“' + (r.words || '').slice(0, 140) + '”'),
    h('div.rv-chg', r.change || ''),
    h('div.rv-where', r.t !== null && r.t !== undefined ? h('button.cm-chip.tc', { on: { click: () => seek(+r.t) } }, mm(r.t)) : null,
      r.proof ? h('span.rv-proof', r.proof) : null))));
  const qs = (cd.questions || []).length ? h('div.rv-sec', h('h6', 'Couldn\'t place: needs you'), ...cd.questions.map((q) => questionEl(cd.order, q, { compact: true }))) : null;
  const crit = cd.critic && (cd.critic.findings || []).length ? h('div.rv-sec', h('h6', 'Critic'), ...cd.critic.findings.map((f) =>
    h('div.rv-li.' + (f.sev === 'HARD' ? (f.fix ? 'ok' : 'bad') : 'soft'), `${f.id} ${f.sev}${f.t !== null && f.t !== undefined ? ' @ ' + mm(f.t) : ''}: ${f.text}`, f.fix ? h('span.rv-proof', ' → fixed: ' + f.fix) : null))) : null;
  return h('div.rv-card', badges,
    cd.passed ? null : h('div.fx-why.needs_you', 'Not passed: ' + (cd.not_passed || []).join('; ')),
    h('div.rv-theme', cd.theme), rows, qs, crit,
    (cd.warns || []).length ? h('div.rv-sec', h('h6', 'Warnings, explained'), ...cd.warns.map((w) => h('div.rv-li.soft', w))) : null,
    h('div.rv-sec', h('h6', 'Checked'), ...(cd.checked || []).map((x) => h('div.rv-li', x))),
    (cd.caveats || []).length ? h('div.rv-sec', h('h6', 'Caveats'), ...cd.caveats.map((x) => h('div.rv-li.soft', x))) : null,
    h('div.rv-foot', `order ${cd.order.slice(-4)} · lessons ${cd.lessons_sha} · $${(+cd.cost_usd || 0).toFixed(2)} · ${(cd.wall_s / 60).toFixed(1)} min`));
}

function sheetEl(c) {
  if (!c || !c.sheet_url) return null;
  const spots = ((c.card && c.card.rows) || []).filter((r) => r.t !== null && r.t !== undefined);
  const img = h('img.rv-sheet', { src: c.sheet_url, alt: 'changed spots', title: 'Changed spots: t-0.5 | t | t+0.5. Click a row to jump there.' });
  img.addEventListener('click', (e) => {
    const r = img.getBoundingClientRect();
    const n = Math.max(1, spots.length);
    const i = Math.min(n - 1, Math.floor(((e.clientY - r.top) / r.height) * n));
    if (spots[i]) seek(+spots[i].t);
  });
  return h('div.rv-sec', h('h6', 'Changed spots'), img);
}

function frameNotesEl(c) {
  const ms = (S.markers || []).filter((m) => c && m.render && c.render && m.render === c.render);
  const ta = h('input.inp', { placeholder: `Note at this frame of v${c ? c.version : ''}…` });
  ta.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') addFrameNote(ta); });
  return h('div.rv-sec', h('h6', 'Frame notes'),
    h('div.rv-fn-add', ta, h('button.btn', { title: 'Pin a note (with its picture) to the frame showing on the left', on: { click: () => addFrameNote(ta) } }, h('span', { html: icon('marker', 12) }), 'Note this frame')),
    h('div.rv-fns', ms.map((m) => h('div.rv-fn', { on: { click: () => seek(+m.t) } }, m.frame_url ? h('img', { src: m.frame_url, alt: '' }) : null,
      h('div', h('span.cm-chip.tc', mm(m.t)), ' ', m.text || '', h('span.rv-fns-st', ' · ' + (m.status || 'open')))))));
}
async function addFrameNote(inp) {
  const c = cur();
  if (!c || !c.render) return;
  try {
    await api.addMarker({ plan: S.plan.slug, t: +va.currentTime.toFixed(3), text: inp.value.trim(), render: c.render, kind: 'note' });
    inp.value = '';
    await loadMarkers(); draw();
    toast('Frame note pinned (it goes with NOTES or Send to factory)', 'ok', 2200);
  } catch (e) { toast(e.message, 'error'); }
}

function variantsEl() {
  const vs = (RV.data.variants || []);
  if (!vs.length) return null;
  return h('div.rv-sec', h('h6', 'Options to choose from'), ...vs.map((o) => h('div.rv-var',
    h('div.rv-var-h', o.line || 'options'),
    h('div.rv-tiles', h('span.fx-none', 'Open the /review page or the Factory tab order to Keep / Kill each option.')))));
}

async function verdict(disp) {
  const c = cur();
  if (!c || RV.busy) return;
  const text = (verdictBox.querySelector('textarea').value || '').trim();
  if (disp === 'REJECT' && !text) { toast('Say why: a REJECT needs a reason', 'warn'); verdictBox.querySelector('textarea').focus(); return; }
  RV.busy = true;
  try {
    const r = await api.fx.verdict(S.plan.slug, c.version, disp, text);
    toast(disp === 'APPROVE' ? `v${c.version} approved${r.order ? ' with a change (a new order is queued)' : ''}: full-quality render + QA queued. Nothing is published.`
      : disp === 'REJECT' ? `v${c.version} rejected: the factory reworks it first` : 'Notes sent to the factory', 'ok', 5000);
    verdictBox.querySelector('textarea').value = '';
    loadMarkers(); load();
  } catch (e) { toast(e.message, 'error'); } finally { RV.busy = false; }
}

function draw() {
  if (!ws || !RV.open) return;
  const d = RV.data || { versions: [] };
  const c = cur();
  hdr.innerHTML = '';
  const sel = h('select.inp.rv-vsel', { title: 'Versions' }, d.versions.map((v) => h('option', { value: v.version }, `v${v.version}${v.by === 'factory' ? ' · factory' : ''}${v.approved ? ' ✓' : v.verdict ? ' · ' + v.verdict.toLowerCase() : ''}${v.theme ? ' · ' + v.theme.replace(/^v\d+:\s*/, '').slice(0, 60) : ''}`)));
  sel.value = RV.cur;
  sel.onchange = () => { RV.cur = +sel.value; draw(); };
  hdr.append(...[h('span.rw-badge', { html: icon('check', 14) }), h('span.rw-h', 'Review'), sel,
    h('div.mode-seg.sm', h('button' + (RV.cmp === 'prev' ? '.on' : ''), { on: { click: () => { RV.cmp = 'prev'; LS.set('rv.cmp', 'prev'); draw(); } } }, 'vs previous'),
      h('button' + (RV.cmp === 'ref' ? '.on' : ''), { on: { click: () => { RV.cmp = 'ref'; LS.set('rv.cmp', 'ref'); draw(); } } }, 'vs reference')),
    h('label.rv-chk', h('input', { type: 'checkbox', checked: RV.lock, on: { change: (e) => { RV.lock = e.target.checked; } } }), 'lock'),
    RV.cmp === 'ref' ? h('label.rv-chk', h('input', { type: 'checkbox', checked: RV.refAudio, on: { change: (e) => { RV.refAudio = e.target.checked; vb.muted = !RV.refAudio; } } }), 'reference audio') : null,
    h('span.grow'),
    h('button.btn.ghost', { title: 'Open the phone review page', on: { click: () => window.open('/review', '_blank') } }, 'Phone page'),
    h('button.ib', { title: 'Back to the editor (Esc)', html: icon('x'), on: { click: () => close() } })].filter(Boolean));
  const chips = h('div.rv-chips', (d.pairs || []).map((p) => h('button.cm-chip', { title: p.desc || '', on: { click: () => { va.currentTime = +p.our_t; if (RV.cmp === 'ref' && vb.src) vb.currentTime = +p.ref_t; } } }, p.label)));
  side.innerHTML = '';
  side.append(cardEl(c), sheetEl(c) || h('span'), frameNotesEl(c), variantsEl() || h('span'));
  ws.querySelector('.rv-chips-slot').replaceChildren(chips);
  const dis = !c;
  verdictBox.querySelectorAll('button').forEach((b) => { b.disabled = dis; });
  verdictBox.querySelector('.rv-vlabel').textContent = c ? `Your verdict on v${c.version}` : 'No version';
  players();
}

function build() {
  const col = document.getElementById('col-left');
  va = h('video', { controls: true, playsInline: true, preload: 'auto' });
  vb = h('video', { controls: true, playsInline: true, preload: 'auto', muted: true });
  la = h('span.mon-label'); lb = h('span.mon-label');
  va.addEventListener('seeked', () => sync(va, vb));
  va.addEventListener('play', () => { if (RV.lock && vb.src) { sync(va, vb); vb.play().catch(() => {}); } });
  va.addEventListener('pause', () => { if (RV.lock) vb.pause(); });
  va.addEventListener('timeupdate', () => { if (RV.lock && vb.src && !vb.paused && Math.abs(vb.currentTime - va.currentTime) > 0.25) vb.currentTime = va.currentTime; });
  hdr = h('div.rw-hd.rv-hd');
  side = h('div.rv-side');
  const ta = h('textarea.inp', { rows: 2, placeholder: 'Notes / why (a REJECT needs a reason; "approve but change the last clip" approves and queues that change)' });
  ta.addEventListener('keydown', (e) => e.stopPropagation());
  verdictBox = h('div.rv-verdict', h('span.rv-vlabel', 'Your verdict'), ta,
    h('div.rv-vbtns',
      h('button.btn.rv-approve', { title: 'Recorded by code. Locks this version; a full-quality render + QA follow. Never published.', on: { click: () => verdict('APPROVE') } }, h('span', { html: icon('check', 12) }), 'APPROVE'),
      h('button.btn.rv-reject', { title: 'The factory reworks it first (your reason is required and becomes a lesson)', on: { click: () => verdict('REJECT') } }, h('span', { html: icon('x', 12) }), 'REJECT'),
      h('button.btn.rv-notes', { title: 'Your words plus the open comments go to the factory as the next round', on: { click: () => verdict('NOTES') } }, h('span', { html: icon('marker', 12) }), 'NOTES')));
  ws = h('div.ref-ws.rv-ws', { hidden: true }, hdr,
    h('div.rv-body',
      h('div.rv-main', h('div.rv-stage', h('div.rv-pane', la, va), h('div.rv-pane', lb, vb)), h('div.rv-chips-slot'), verdictBox),
      side));
  col.appendChild(ws);
}

async function open() {
  RV.open = true;
  emit('toggleReference', false);
  ws.hidden = false;
  document.querySelectorAll('#p-program .ptab[data-ptab]').forEach((b) => b.classList.toggle('on', b.dataset.ptab === 'review'));
  await load();
}
function close() {
  if (!RV.open) return;
  RV.open = false;
  va.pause(); vb.pause();
  ws.hidden = true;
  document.querySelectorAll('#p-program .ptab[data-ptab]').forEach((b) => b.classList.toggle('on', b.dataset.ptab === 'program'));
}

export function initReview() {
  build();
  on('toggleReview', (v) => ((v === undefined ? !RV.open : v) ? open() : close()));
  on('reviewRefresh', () => { if (RV.open) load(); });
  on('plan', ({ by } = {}) => { if (RV.open && by === 'open') { RV.cur = null; load(); } });
  on('markers', () => { if (RV.open) draw(); });
  document.addEventListener('keydown', (e) => { if (RV.open && e.key === 'Escape' && !/INPUT|TEXTAREA/.test((e.target && e.target.tagName) || '')) close(); });
  window.__review = { RV, open, close, load, sendToFactory };
}
