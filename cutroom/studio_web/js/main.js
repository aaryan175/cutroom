// Cutroom Studio bootstrap: header/toolbar, live events, keyboard map, monitor focus.
import { S, on, emit, openPlan, reloadPlan, flushOps, undo, redo, setPlayhead, fps, loadMarkers, normProject, normGovernor, normJob, normOvernight } from './state.js';
import { api, connectEvents } from './api.js';
import { $, h, icon, hydrateIcons, toast, isTyping, LS, ago } from './util.js';
import { initLayout } from './layout.js';
import { initProject } from './project.js';
import { initSource, sourceCtl, insertFromSource, cutawayFromSource } from './source.js';
import { initProgram, programCtl, markReference } from './program.js';
import { initTimeline, razor, freeze, deleteSelection, toggleSnap, zoomBy, fit, revealPlayhead, _test as tlTest } from './timeline.js';
import { initChat } from './chat.js';
import { initDock } from './dock.js';
import { initImporter } from './importer.js';
import { initVersions, draftLabel, draftSha } from './versions.js';
import { initReference, _test as refTest } from './reference.js';
import { initComments } from './comments.js';
import { initLooks } from './looks.js';
import { initTour } from './tour.js';
import { initFactory } from './factory.js';
import { initReview } from './review.js';

// ---------------------------------------------------------------- header
function fmtN(n) { return n === undefined || n === null ? '–' : Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 }); }
function renderHeader() {
  const p = S.project || {};
  $('#proj-name').textContent = p.name || 'Cutroom';
  const st = p.status || {};
  const ls = $('#lib-status');
  ls.innerHTML = '';
  const items = [['clips', st.clips], ['hours', st.hours], ['transcribed', st.transcribed], ['tagged', st.tagged]];
  for (const [k, v] of items) if (v !== undefined) ls.appendChild(h('span', h('b', fmtN(v)), ' ' + k));
}
function renderPlanBits() {
  const pl = S.plan;
  $('#plan-name').textContent = pl ? `${(pl.draft && pl.draft.title) || pl.slug}` : 'No plan open';
  $('#btn-undo').disabled = !pl || !pl.canUndo;
  $('#btn-redo').disabled = !pl || !pl.canRedo;
  $('#btn-commit').disabled = !pl;
  const hasVersion = pl && pl.versions && pl.versions.length;
  $('#btn-render').disabled = !hasVersion;
  $('#btn-render-more').disabled = !hasVersion;
  $('#btn-render').title = hasVersion ? `Render a preview of ${latestVersion().replace(/^.*_v(\d+)\.json$/, 'v$1')}` : 'Save a version first (⌘S)';
  const ds = $('#draft-state');
  ds.textContent = draftLabel();
  ds.classList.toggle('dirty', /unsaved|not saved/.test(ds.textContent));
}
function latestVersion() {
  const v = (S.plan && S.plan.versions) || [];
  return v.slice().sort((a, b) => (+(a.match(/_v(\d+)/) || [0, 0])[1]) - (+(b.match(/_v(\d+)/) || [0, 0])[1])).pop() || null;
}
function renderPlanMenu() {
  const m = $('#plan-menu');
  m.innerHTML = '';
  const plans = (S.project && S.project.plans) || [];
  m.appendChild(h('div.mh', 'Plans'));
  for (const p of plans) {
    m.appendChild(h('button', { class: S.plan && S.plan.slug === p.slug ? 'cur' : '', on: { click: () => { m.hidden = true; openPlan(p.slug); } } },
      p.title || p.slug, h('span.k', `${(p.versions || []).length} versions${p.updated ? ' · ' + ago(p.updated) : ''}`)));
  }
  if (S.plan && S.plan.versions && S.plan.versions.length) {
    m.appendChild(h('hr'));
    m.appendChild(h('div.mh', 'Saved versions of this plan'));
    for (const v of S.plan.versions.slice().reverse()) {
      m.appendChild(h('button', { title: 'Render this version', on: { click: () => { m.hidden = true; doRender(true, v); } } }, v, h('span.k', 'render preview')));
    }
  }
  m.appendChild(h('hr'));
  m.appendChild(h('button', { on: { click: () => { m.hidden = true; emit('needPlan'); } } }, h('span', { html: icon('plus', 13) }), 'New plan…'));
}
function renderGov() {
  const g = S.governor || {};
  const pill = $('#gov-pill');
  pill.className = 'gov-pill ' + (g.health || '');
  const ln = g.load && g.load.now !== undefined ? `load ${(+g.load.now).toFixed(1)}` : '';
  const txt = g.throttle ? 'throttled' : (g.health === 'ok' ? 'healthy' : g.health === 'warn' ? 'busy' : g.health === 'bad' ? 'overloaded' : '–');
  pill.querySelector('.txt').textContent = [txt, ln].filter(Boolean).join(' · ');
  pill.title = g.throttle ? `Throttled: ${g.throttle}` : 'Machine health (click for details)';
}

async function refreshProject() {
  try { S.project = normProject(await api.project()); emit('project'); } catch (e) { toast(`Can't load project: ${e.message}`, 'error', 8000); }
}

async function doRender(preview = true, file = null) {
  const v = file || latestVersion();
  if (!v) { toast('Save a version first (⌘S). Renders always use a saved version.', 'warn'); return; }
  const planFile = v.includes('/') ? v : `plans/${v}`;
  try {
    const r = await api.render(planFile, preview);
    toast(`${preview ? 'Preview' : 'Final'} render queued for ${v}`, 'ok', 2200);
    if (r && r.job) S.jobs.set(r.job, { id: r.job, kind: 'render', label: `${v}${preview ? ' preview' : ''}`, status: 'queued', progress: 0, started: Date.now() / 1000 });
    emit('jobs'); emit('showDock', 'jobs');
  } catch (e) { toast(`Render failed to start: ${e.message}`, 'error'); }
}
// ---------------------------------------------------------------- deliverables (cutroom export)
function exportTarget() {
  const all = (S.project && S.project.renders) || [];
  const slug = S.plan && S.plan.slug;
  if (S.renderFile) return S.renderFile;
  const mine = all.filter((r) => slug && (r.plan === slug || (r.file || '').startsWith(slug + '_')));
  const pick = (mine.length ? mine : all).slice().sort((a, b) => (b.created || 0) - (a.created || 0))[0];
  return pick ? pick.file : null;
}
async function doExport(kind) {
  const file = exportTarget();
  if (!file) { toast('Render the edit first: exports are made from a render.', 'warn'); return; }
  const body = { video: file };
  if (kind === 'thumbs') body.thumbs = true;
  else if (kind === 'cut30') { body.cutdown = [30]; body.preset = ['reels']; }
  else if (kind === 'all') { body.preset = ['all']; body.thumbs = true; }
  else body.preset = [kind];
  try {
    const r = await api.exportRender(body);
    toast(`Export queued for ${String(file).split('/').pop()}`, 'ok', 2200);
    if (r && r.job) S.jobs.set(r.job, { id: r.job, kind: 'export', label: `export ${kind}`, status: 'queued', progress: 0, started: Date.now() / 1000 });
    emit('jobs'); emit('showDock', 'jobs');
  } catch (e) { toast(`Export failed to start: ${e.message}`, 'error'); }
}
async function commit() {
  if (!S.plan) return;
  try {
    await flushOps();                                     // every edit already made lands first
    const r = await api.commit(S.plan.slug, { expect: draftSha() });
    toast(`Saved as v${r.version || (String(r.name).match(/_v(\d+)/) || [0, '?'])[1]}`, 'ok', 2200);
    await reloadPlan('commit');
    refreshProject();
  } catch (e) {
    toast(`Save version failed: ${e.message}`, 'error');
    if (/changed since/.test(e.message)) reloadPlan('commit');
  }
}

// ---------------------------------------------------------------- new plan prompt (inline, no dialogs)
function needPlan() {
  emit('showProjectTab', 'plans');
  setTimeout(() => { const i = document.querySelector('#project-body input[placeholder="New plan title"]'); if (i) i.focus(); }, 30);
}

// ---------------------------------------------------------------- monitor focus
function focusMonitor(which) {
  S.activeMonitor = which;
  $('#p-source').classList.toggle('focus', which === 'source');
  $('#p-program').classList.toggle('focus', which === 'program');
}
const mon = () => (S.activeMonitor === 'source' && sourceCtl.loaded ? sourceCtl : programCtl);

// ---------------------------------------------------------------- keyboard
const KEYS = [
  ['Playback', [['Play / pause', ['Space']], ['Shuttle back / stop / forward', ['J', 'K', 'L']], ['One frame', ['←', '→']], ['One second', ['⇧←', '⇧→']]]],
  ['Source', [['Set In / Out', ['I', 'O']], ['Insert at playhead', [',']], ['Overwrite at playhead', ['.']]]],
  ['Timeline', [['Razor at playhead', ['C']], ['Delete / ripple delete', ['Del', '⇧Del']], ['Freeze frame', ['F']], ['Comment (draw: A B C P, range: I / O)', ['M']], ['Snapping', ['S']], ['Zoom in / out / fit', ['=', '−', '\\']]]],
  ['Edit', [['Undo / redo', ['⌘Z', '⇧⌘Z']], ['Save version', ['⌘S']], ['Reference frame → chat', ['R']], ['Focus chat', ['/']], ['This help', ['?']]]],
];
function showHelp(onOff) {
  const o = $('#help');
  if (onOff === false || (!o.hidden && onOff === undefined)) { o.hidden = true; return; }
  o.innerHTML = '';
  const grid = h('div.help-grid');
  for (const [g, rows] of KEYS) {
    grid.appendChild(h('h6', g));
    for (const [label, keys] of rows) grid.appendChild(h('div.help-row', h('span', label), h('span.keys', keys.map((k) => h('kbd', k)))));
  }
  o.appendChild(h('div.help-card', { on: { click: (e) => e.stopPropagation() } },
    h('h3', 'Keyboard shortcuts', h('span.grow'), h('button.ib', { html: icon('x'), on: { click: () => showHelp(false) } })), grid,
    h('p', { style: { color: 'var(--text-3)', margin: '12px 0 0', fontSize: '11.5px' } }, 'Keys act on the focused monitor (blue line on its tab). Shortcuts are off while you type in a field. On Windows/Linux use Ctrl for ⌘.')));
  o.hidden = false;
  o.onclick = () => showHelp(false);
}
function onKey(e) {
  if (e.key === 'Escape') { showHelp(false); $('#lightbox').hidden = true; document.querySelectorAll('.menu').forEach((m) => (m.hidden = true)); }
  if (isTyping(e)) return;
  const mod = e.metaKey || e.ctrlKey;
  const k = e.key;
  const handled = () => { e.preventDefault(); e.stopPropagation(); };
  if (mod) {
    if (k.toLowerCase() === 'z') { handled(); e.shiftKey ? redo() : undo(); return; }
    if (k.toLowerCase() === 'y') { handled(); redo(); return; }
    if (k.toLowerCase() === 's') { handled(); commit(); return; }
    if (k === '=' || k === '+') { handled(); zoomBy(1.5); return; }
    if (k === '-') { handled(); zoomBy(1 / 1.5); return; }
    return;
  }
  if (e.altKey) return;
  switch (k) {
    case ' ': handled(); mon().togglePlay(); break;
    case 'j': case 'J': handled(); mon().shuttle(-1); break;
    case 'k': case 'K': handled(); mon().shuttle(0); break;
    case 'l': case 'L': handled(); mon().shuttle(1); break;
    case 'ArrowLeft': handled(); e.shiftKey ? mon().seekRel(-1) : mon().step(-1); break;
    case 'ArrowRight': handled(); e.shiftKey ? mon().seekRel(1) : mon().step(1); break;
    case 'i': case 'I': handled(); if (S.commentDraft) emit('commentMark', 'in'); else sourceCtl.markIn(); break;
    case 'o': case 'O': handled(); if (S.commentDraft) emit('commentMark', 'out'); else sourceCtl.markOut(); break;
    case ',': handled(); insertFromSource('insert'); break;
    case '.': handled(); insertFromSource('overwrite'); break;
    case 'c': case 'C': handled(); razor(); break;
    case 'Delete': case 'Backspace': handled(); deleteSelection(e.shiftKey); break;
    case 'f': case 'F': handled(); freeze(); break;
    case 'm': case 'M': handled(); emit('addMarker'); break;
    case 'r': case 'R': handled(); markReference(); break;
    case 's': case 'S': handled(); toggleSnap(); toast(`Snapping ${S.snapping ? 'on' : 'off'}`, 'info', 900); break;
    case '=': case '+': handled(); zoomBy(1.5); break;
    case '-': case '_': handled(); zoomBy(1 / 1.5); break;
    case '\\': handled(); fit(); break;
    case '/': handled(); emit('focusChat'); break;
    case '?': handled(); showHelp(); break;
    case 'Home': handled(); setPlayhead(0, 'key'); revealPlayhead(); break;
    case 'End': handled(); setPlayhead(S.tl ? S.tl.duration : 0, 'key'); revealPlayhead(); break;
    case 'ArrowUp': case 'ArrowDown': {
      if (!S.tl) break;
      handled();
      const cuts = [...new Set(S.tl.v1.flatMap((p) => [p.start, p.end]))].sort((a, b) => a - b);
      const t = S.playhead;
      const nt = k === 'ArrowUp' ? [...cuts].reverse().find((c) => c < t - 1e-3) : cuts.find((c) => c > t + 1e-3);
      if (nt !== undefined) { setPlayhead(nt, 'key'); revealPlayhead(); }
      break;
    }
    default: break;
  }
}

// ---------------------------------------------------------------- live events
let lastHandOp = 0;
function startEvents() {
  connectEvents({
    hello: (d) => {
      if (d.governor) { S.governor = normGovernor(d.governor); renderGov(); emit('governor'); }
      for (const j of d.jobs || []) S.jobs.set(j.id, normJob(j));
      emit('jobs');
    },
    plan_changed: (d) => {
      if (d.kind && d.kind !== 'draft') { refreshProject(); if (S.plan && d.slug === S.plan.slug) reloadPlan('commit'); return; }
      if (!S.plan || (d.slug && d.slug !== S.plan.slug)) { refreshProject(); return; }
      const own = d.source === 'studio' || (d.by === 'hand' && performance.now() - lastHandOp < 2500);
      if (own) return; // our own edit, already applied
      reloadPlan(d.by === 'hand' ? 'remote' : 'claude');
    },
    bins_changed: () => emit('binsChanged'),
    qa_done: (d) => { toast(`QA finished: ${(d.video || '').split('/').pop()}`, 'ok', 4000); refreshProject(); },
    job: (d0) => {
      const d = normJob(d0);
      const prev = S.jobs.get(d.id) || {};
      S.jobs.set(d.id, { ...prev, ...d });
      emit('jobs');
      if (d.status === 'error' && prev.status !== 'error') toast(`${d.kind || 'Job'} failed: ${d.label || d.id}`, 'error', 6000);
    },
    governor: (d) => { S.governor = normGovernor(d); renderGov(); emit('governor'); },
    overnight: (d) => { S.overnight = normOvernight(d); emit('overnight'); },
    render_done: (d) => { toast(`Render finished: ${String(d.file).split('/').pop()}`, 'ok', 5000); refreshProject().then(() => { if (S.programMode === 'render') emit('playRender', d.file); }); },
    export_done: (d) => { const n = (d.files || []).filter((f) => /\.(mp4|jpg)$/.test(f.file)).length; toast(`Export finished: ${n} file(s) in ${d.dir}${(d.warnings || []).length ? ` · ${d.warnings[0]}` : ''}`, 'ok', 7000); refreshProject(); },
    notes: () => { loadMarkers(); },
    markers: (d) => { if (!d.plan || !S.plan || d.plan === S.plan.slug) loadMarkers(); },
    ref: (d) => emit('ref', d),
    library_changed: () => { refreshProject(); emit('libraryChanged'); },
    comments: (d) => emit('commentsRun', d),
    factory: (d) => emit('factoryEvent', d),
    factory_state: (d) => emit('factoryState', d),
  }, (state) => { const c = $('#conn'); c.className = 'conn ' + state; c.title = state === 'live' ? 'Live updates connected' : state === 'down' ? 'Live updates disconnected, retrying…' : 'Connecting…'; });
}

// ---------------------------------------------------------------- boot
async function boot() {
  hydrateIcons();
  initLayout();
  initProject($('#project-body'));
  initSource($('#source-body'));
  initProgram($('#program-body'), $('#program-tools'));
  initTimeline($('#timeline-body'), $('#timeline-tools'));
  initChat($('#claude-body'), $('#claude-tools'));
  initDock($('#dock-body'));
  initImporter();
  initVersions();
  initReference();
  initComments();
  initLooks();
  initFactory();
  initReview();

  on('project', () => { renderHeader(); renderPlanBits(); });
  on('plan', () => { renderPlanBits(); });
  on('busy', () => { const d = $('#draft-state'); d.classList.toggle('saving', S.busy); if (S.busy) { lastHandOp = performance.now(); d.textContent = 'saving…'; } else renderPlanBits(); });
  on('needPlan', needPlan);
  on('projectRefresh', refreshProject);
  on('focusMonitor', focusMonitor);
  on('revealPlayhead', revealPlayhead);
  on('cutawayFromSource', cutawayFromSource);
  on('grabProgram', () => document.querySelector('#p-program .mon-bar .ib[title="Grab frame to chat"]').click());
  focusMonitor('program');

  $('#btn-undo').onclick = undo;
  $('#btn-redo').onclick = redo;
  $('#btn-commit').onclick = commit;
  $('#btn-render').onclick = () => doRender(true);
  $('#btn-render-more').onclick = (e) => { e.stopPropagation(); const m = $('#render-menu'); m.hidden = !m.hidden; };
  $('#render-menu').addEventListener('click', (e) => {
    const x = e.target.closest('button[data-export]');
    if (x) { $('#render-menu').hidden = true; doExport(x.dataset.export); return; }
    const b = e.target.closest('button[data-render]'); if (!b) return; $('#render-menu').hidden = true; doRender(b.dataset.render === 'preview');
  });
  $('#plan-btn').onclick = (e) => { e.stopPropagation(); const m = $('#plan-menu'); if (m.hidden) renderPlanMenu(); m.hidden = !m.hidden; };
  $('#gov-pill').onclick = () => emit('showDock', 'overnight');
  $('#btn-help').onclick = () => showHelp();
  initTour();
  document.addEventListener('click', () => { $('#plan-menu').hidden = true; $('#render-menu').hidden = true; });
  document.addEventListener('keydown', onKey);
  // clicking anywhere in a monitor panel focuses it for transport keys
  $('#p-source').addEventListener('pointerdown', () => focusMonitor('source'));
  $('#p-program').addEventListener('pointerdown', () => focusMonitor('program'));
  $('#p-timeline').addEventListener('pointerdown', () => focusMonitor('program'));

  startEvents();
  // overnight status: poll while a pass runs (the server may not push it)
  let onPoll = 0;
  const pollOvernight = async () => {
    try {
      const was = S.overnight && S.overnight.running;
      S.overnight = normOvernight(await api.overnight('status'));
      emit('overnight');
      if (was && !S.overnight.running) refreshProject();
    } catch { /* not available */ }
  };
  on('overnightPoll', () => { onPoll = 6; pollOvernight(); });
  setInterval(() => { if ((S.overnight && S.overnight.running) || onPoll > 0) { onPoll--; pollOvernight(); } }, 5000);
  await refreshProject();
  api.governor().then((g) => { S.governor = normGovernor(g); renderGov(); emit('governor'); }).catch(() => {});
  api.overnight('status').then((o) => { S.overnight = normOvernight(o); emit('overnight'); }).catch((e) => { S.overnight = { unavailable: e.message }; emit('overnight'); });
  api.jobs().then((r) => { for (const j of (r.jobs || r || [])) S.jobs.set(j.id, normJob(j)); emit('jobs'); }).catch(() => {});
  const plans = (S.project && S.project.plans) || [];
  const last = LS.get('lastPlan', null);
  const pick = plans.find((p) => p.slug === last) || plans[0];
  if (pick) await openPlan(pick.slug); else { emit('plan', { by: 'open' }); loadMarkers(); }
  window.__studio = { S, emit, fps, tl: tlTest, ref: refTest };
}
boot().catch((e) => { console.error(e); toast(`Studio failed to start: ${e.message}`, 'error', 10000); });
