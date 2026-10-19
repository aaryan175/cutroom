// Versions: "Save version" state in the header ("Draft — unsaved changes since v3" / "Saved as v4"), the
// Versions menu (clock): open / compare with draft / restore, and a plan diff computed here in the browser.
// The backend route is still POST /api/plan/<slug>/commit; only the words changed.
import { S, on, emit, applyPlanResponse } from './state.js';
import { api, hooks, media } from './api.js';
import { h, icon, toast, ago, dur, clamp } from './util.js';
import { xicon, explain } from './importer.js';

let last = { slug: null, saved: null, rows: [] };      // from the newest plan payload
hooks.planPayload = (p) => {
  last = { slug: p.slug, saved: p.saved || null, rows: p.version_files || [] };
  if (btn) syncBtn();
};

const vnum = (f) => { const m = String(f || '').match(/_v(\d+)\.json$/); return m ? +m[1] : null; };
export function savedState() { return S.plan && last.slug === S.plan.slug ? last.saved : null; }
/** Header words for the draft's state. */
export function draftLabel() {
  if (!S.plan) return '';
  const s = savedState();
  if (!s) return 'Draft';
  if (!s.latest) return 'Draft · not saved yet';
  const v = s.version ?? vnum(s.latest);
  return s.dirty ? `Draft — unsaved changes since v${v}` : `Saved as v${v}`;
}
function versionRows() {
  if (!S.plan || last.slug !== S.plan.slug) return [];
  return last.rows.filter((r) => typeof r === 'object' && r.kind !== 'draft').slice().sort((a, b) => (b.version ?? -1) - (a.version ?? -1) || b.mtime - a.mtime);
}
function renderOf(file) {
  const stem = String(file).replace(/\.json$/, '');
  const rs = ((S.project && S.project.renders) || []).filter((r) => String(r.file).split('/').pop().startsWith(stem + '_') || String(r.file).split('/').pop().startsWith(stem + '.'));
  rs.sort((a, b) => (+!a.preview) - (+!b.preview) || (b.created || 0) - (a.created || 0));
  return rs.length ? rs[rs.length - 1] : null;      // prefer a full render, else the newest preview
}

// ---------------------------------------------------------------- plan diff (version -> draft)
function segDur(g) {
  if (g.in !== undefined && g.out !== undefined && g.in !== null && g.out !== null) return (+g.out - +g.in) / (+g.speed || 1);
  if (g.dur !== undefined) return +g.dur;
  if (Array.isArray(g.shots)) return g.shots.reduce((a, s) => a + ((+s.out - +s.in) || +s.dur || 0), 0);
  return null;
}
function clipName(id) {
  if (!id || !S.tl) return id || '';
  const p = S.tl.pieces.find((q) => q.clip === id && q.clip_rel);
  return p ? String(p.clip_rel).split('/').pop() : id;
}
function segLabel(g) {
  const t = g.type || 'talk';
  if (t === 'title') return `title “${g.text || ''}”`;
  if (g.label || g.note) return `${t} · ${g.label || g.note}`;
  if (t === 'montage') return `montage of ${(g.shots || []).length} shots`;
  return `${t} · ${clipName(g.clip)}${g.in !== undefined && g.in !== null ? ` @ ${(+g.in).toFixed(1)}s` : ''}`;
}
const segId = (g) => [g.type || 'talk', g.clip || '', g.text || '', Array.isArray(g.shots) ? g.shots.map((s) => s.clip).join(',') : ''].join('|');
function flat(plan) {
  const out = [];
  (plan.sections || []).forEach((sec, si) => (sec.segments || []).forEach((g, gi) => out.push({ si, gi, sec: sec.name || `section ${si + 1}`, g, id: segId(g) })));
  return out;
}
function lcs(a, b) {
  const n = a.length, m = b.length;
  if (n * m > 400000) return a.map((x, i) => (b[i] && b[i].id === x.id ? [i, i] : null)).filter(Boolean);
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i].id === b[j].id ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const pairs = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i].id === b[j].id) { pairs.push([i, j]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) i++; else j++;
  }
  return pairs;
}
const strip = (g, keys) => { const o = { ...g }; for (const k of keys) delete o[k]; return JSON.stringify(o); };
/** What changed from plan `a` (a saved version) to plan `b` (the draft): [{kind, text, sec}] */
export function planDiff(a, b) {
  const out = [];
  const A = flat(a || {}), B = flat(b || {});
  const pairs = lcs(A, B);
  const ma = new Set(pairs.map((p) => p[0])), mb = new Set(pairs.map((p) => p[1]));
  const secA = (a.sections || []).map((s) => s.name), secB = (b.sections || []).map((s) => s.name);
  for (const n of secB) if (!secA.includes(n)) out.push({ kind: 'added', text: `section “${n}” added` });
  for (const n of secA) if (!secB.includes(n)) out.push({ kind: 'removed', text: `section “${n}” removed` });
  for (const [i, j] of pairs) {
    const x = A[i], y = B[j];
    const dx = segDur(x.g), dy = segDur(y.g);
    if (x.sec !== y.sec) out.push({ kind: 'moved', text: `${segLabel(y.g)} moved from ${x.sec} to ${y.sec}`, sec: y.sec });
    const trimmed = ['in', 'out', 'dur', 'speed'].some((k) => JSON.stringify(x.g[k]) !== JSON.stringify(y.g[k]));
    if (trimmed) {
      const d = dx !== null && dy !== null ? dy - dx : null;
      out.push({ kind: 'trimmed', text: `${segLabel(y.g)} trimmed${d !== null && Math.abs(d) >= 0.04 ? ` (${d > 0 ? '+' : '−'}${Math.abs(d).toFixed(1)}s)` : ''}`, sec: y.sec });
    }
    if (strip(x.g, ['in', 'out', 'dur', 'speed']) !== strip(y.g, ['in', 'out', 'dur', 'speed'])) {
      const keys = [...new Set([...Object.keys(x.g), ...Object.keys(y.g)])].filter((k) => !['in', 'out', 'dur', 'speed'].includes(k) && JSON.stringify(x.g[k]) !== JSON.stringify(y.g[k]));
      out.push({ kind: 'changed', text: `${segLabel(y.g)}: ${keys.join(', ')} changed`, sec: y.sec });
    }
  }
  A.forEach((x, i) => { if (!ma.has(i)) out.push({ kind: 'removed', text: `${segLabel(x.g)} removed${segDur(x.g) ? ` (${dur(segDur(x.g))})` : ''}`, sec: x.sec }); });
  B.forEach((y, j) => { if (!mb.has(j)) out.push({ kind: 'added', text: `${segLabel(y.g)} added${segDur(y.g) ? ` (${dur(segDur(y.g))})` : ''}`, sec: y.sec }); });
  const sa = (a.sections || []), sb = (b.sections || []);
  sb.forEach((s, k) => { const o = sa.find((x) => x.name === s.name) || sa[k]; if (o && JSON.stringify(o.music || null) !== JSON.stringify(s.music || null)) out.push({ kind: 'changed', text: `music in “${s.name}” changed` }); });
  for (const k of new Set([...Object.keys(a.style || {}), ...Object.keys(b.style || {})])) {
    if (JSON.stringify((a.style || {})[k]) !== JSON.stringify((b.style || {})[k])) out.push({ kind: 'changed', text: `${k.replace(/_/g, ' ')} style changed` });
  }
  if ((a.title || '') !== (b.title || '')) out.push({ kind: 'changed', text: `title “${a.title || ''}” → “${b.title || ''}”` });
  return out;
}

// ---------------------------------------------------------------- dialogs
let modal = null;
function closeModal() { if (modal) { modal.querySelectorAll('video').forEach((v) => v.pause()); modal.remove(); modal = null; } }
function openModal(cls, title, sub, ...kids) {
  closeModal();
  const card = h('div.imp-card.' + cls, { on: { click: (e) => e.stopPropagation() } },
    h('div.imp-hd', h('div', h('h3', title), sub ? h('p', sub) : null), h('button.ib', { title: 'Close', html: icon('x'), on: { click: closeModal } })), ...kids);
  modal = h('div.overlay.imp-ov', { on: { click: closeModal } }, card);
  document.body.appendChild(modal);
  return card;
}
const KIND_MARK = { added: '+', removed: '−', trimmed: '↔', moved: '⇅', changed: '•' };
async function compareWithDraft(row) {
  const slug = S.plan.slug;
  const v = row.version ?? vnum(row.file);
  const s = savedState();
  const rA = renderOf(row.file);
  const latest = s && s.latest;
  const rB = latest && !s.dirty && latest !== row.file ? renderOf(latest) : null;
  if (rA && rB) return sideBySide(rA, rB, `v${v}`, `v${s.version ?? vnum(latest)} (= your draft)`);
  let plan;
  try { plan = (await api.planVersion(slug, row.file)).plan; } catch (e) { toast(`Couldn't read v${v}: ${e.message}`, 'error'); return; }
  const diff = planDiff(plan, S.plan.draft || {});
  const list = h('div.vdiff');
  if (!diff.length) list.appendChild(h('div.vdiff-empty', h('span', { html: icon('check', 16) }), `Your draft is the same as v${v}.`));
  for (const d of diff) list.appendChild(h('div.vd-row.' + d.kind, h('span.vd-k', KIND_MARK[d.kind] || '•'), h('span.vd-t', d.text), d.sec ? h('span.vd-s', d.sec) : null));
  const counts = ['added', 'removed', 'trimmed', 'moved', 'changed'].map((k) => [k, diff.filter((d) => d.kind === k).length]).filter((x) => x[1]);
  openModal('vcmp', `v${v} → your draft`, diff.length ? counts.map(([k, n]) => `${n} ${k}`).join(' · ') : 'No differences',
    list,
    h('div.imp-ft', h('span.note', rA ? 'Render the draft as a new version to compare the pictures side by side.' : 'Render both versions to compare the pictures side by side.'), h('span.grow'),
      h('button.btn', { on: { click: () => { closeModal(); restore(row); } } }, 'Restore v' + v), h('button.btn.primary', { on: { click: closeModal } }, 'Done')));
}
function sideBySide(rA, rB, la, lb) {
  const va = h('video', { src: media.render(rA.file), preload: 'auto', playsInline: true, muted: true });
  const vb = h('video', { src: media.render(rB.file), preload: 'auto', playsInline: true });
  const bar = h('input.vs-scrub', { type: 'range', min: 0, max: 1000, value: 0 });
  const play = h('button.ib', { html: icon('play'), title: 'Play both' });
  const sync = () => { if (Math.abs(va.currentTime - vb.currentTime) > 0.08) va.currentTime = vb.currentTime; };
  play.onclick = () => { if (vb.paused) { vb.play(); va.play(); play.innerHTML = icon('pause'); } else { vb.pause(); va.pause(); play.innerHTML = icon('play'); } };
  vb.addEventListener('timeupdate', () => { sync(); if (vb.duration) bar.value = Math.round(vb.currentTime / vb.duration * 1000); });
  bar.addEventListener('input', () => { if (vb.duration) { vb.currentTime = va.currentTime = bar.value / 1000 * vb.duration; } });
  openModal('vsbs', `${la} vs ${lb}`, 'Both renders, locked together.',
    h('div.vs-grid', h('figure', va, h('figcaption', la)), h('figure', vb, h('figcaption', lb))),
    h('div.vs-bar', play, bar));
}
async function restore(row) {
  const v = row.version ?? vnum(row.file);
  try {
    const plan = (await api.planVersion(S.plan.slug, row.file)).plan;
    const r = await api.putPlan(S.plan.slug, plan, true);
    applyPlanResponse(r, { by: 'hand' });
    toast(`Restored v${v} into your draft. Undo (⌘Z) brings your changes back.`, 'ok', 5000);
  } catch (e) { toast(`Restore failed: ${e.message}`, 'error'); }
}
async function openVersion(row) {
  const v = row.version ?? vnum(row.file);
  const r = renderOf(row.file);
  if (r) { emit('playRender', r.file); toast(`Playing the render of v${v}`, 'info', 1800); return; }
  try {
    const res = await api.render(`plans/${row.file}`, true);
    if (res && res.job) S.jobs.set(res.job, { id: res.job, kind: 'render', label: `${row.file} preview`, status: 'queued', progress: 0, started: Date.now() / 1000 });
    emit('jobs'); emit('showDock', 'jobs');
    toast(`v${v} has no render yet — rendering a preview now.`, 'info', 3000);
  } catch (e) { toast(`Couldn't render v${v}: ${e.message}`, 'error'); }
}

// ---------------------------------------------------------------- menu
let btn, menu;
function syncBtn() { btn.disabled = !S.plan; const n = versionRows().length; btn.querySelector('.n').textContent = n ? String(n) : ''; }
function fillMenu() {
  menu.innerHTML = '';
  const rows = versionRows();
  const s = savedState();
  menu.appendChild(h('div.mh', 'Saved versions', h('span.grow'), h('span.k', draftLabel())));
  if (!rows.length) {
    menu.appendChild(h('div.vm-empty', 'No saved versions yet. Save version (⌘S) makes v1 — a numbered copy you can render, compare and come back to.'));
    return;
  }
  for (const r of rows) {
    const v = r.version ?? vnum(r.file);
    const isCur = s && s.latest === r.file && !s.dirty;
    const rend = renderOf(r.file);
    menu.appendChild(h('div.vm-row' + (isCur ? '.cur' : ''),
      h('span.vm-v', v !== null && v !== undefined ? `v${v}` : r.file.replace(/\.json$/, '')),
      h('span.vm-meta', h('span', ago(r.mtime)), r.by ? h('span.vm-by.' + (r.by === 'you' ? 'you' : 'ai'), { title: r.by === 'you' ? 'You saved this version' : r.by === 'factory' ? 'The factory built this version (see the Review tab for its card)' : 'The AI saved this version' }, r.by === 'you' ? 'you' : r.by === 'factory' ? 'factory' : 'AI') : null,
        isCur ? h('span.pill.blue', 'same as draft') : null, rend ? h('span.pill', 'rendered') : null),
      h('span.vm-acts',
        h('button.btn.ghost', { title: rend ? 'Play its render in the Program monitor' : 'No render yet: render a preview of it', on: { click: () => { menu.hidden = true; openVersion(r); } } }, 'Open'),
        h('button.btn.ghost', { title: 'What changed between this version and your draft', on: { click: () => { menu.hidden = true; compareWithDraft(r); } } }, 'Compare with draft'),
        h('button.btn.ghost', { title: 'Copy this version into your draft (undoable)', disabled: isCur, on: { click: () => { menu.hidden = true; restore(r); } } }, 'Restore'))));
  }
}
export function initVersions() {
  const commit = document.getElementById('btn-commit');
  if (!commit) return;
  btn = h('button#btn-versions.tb-icon', { title: 'Versions: open, compare or restore a saved version', disabled: true, on: { click: (e) => { e.stopPropagation(); document.querySelectorAll('.menu').forEach((m) => { if (m !== menu) m.hidden = true; }); if (menu.hidden) fillMenu(); menu.hidden = !menu.hidden; } } },
    h('span.icw', { html: xicon('clock', 15) }), h('span.n'));
  menu = h('div#versions-menu.menu.vmenu', { hidden: true, on: { click: (e) => e.stopPropagation() } });
  const wrap = h('div.vwrap', btn, menu);
  commit.after(wrap, explain('Saves a numbered copy (v1, v2…) of your edit that you can render, compare and go back to. Your edits already auto-save as a draft.', { title: 'Save version' }));
  const render = document.querySelector('#hdr .split-btn');
  if (render) render.after(explain('Render makes a real video file from your latest saved version — with colour, captions and music — so you can watch it properly or send it.', { title: 'Render' }));
  document.addEventListener('click', () => { menu.hidden = true; });
  on('plan', () => { syncBtn(); if (!menu.hidden) fillMenu(); });
  on('project', () => { if (!menu.hidden) fillMenu(); });
  syncBtn();
}
export const _test = { planDiff, versionRows, draftLabel, clamp };
