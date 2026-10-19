// The factory in the Studio (docs/FACTORY.md): the header light, "Send to factory", and the Factory dock tab
// (orders, the pipeline stepper, team activity, the ledger, questions as numbered buttons, Lessons, Brain).
// Live over SSE: `factory` (one line per bus event) and `factory_state` (the light).
import { S, on, emit, setPlayhead, fps, loadMarkers } from './state.js';
import { api } from './api.js';
import { h, icon, tc, toast, ago } from './util.js';

const STEPS = ['Intake', 'Producer', 'Reference', 'Extend', 'Edit', 'Render', 'QA', 'Critic', 'Fix', 'Ready'];
const STATE_LABEL = { queued: 'queued', picked_up: 'picked up', running: 'working', needs_you: 'needs you', ready: 'ready',
  blocked: 'blocked', stopped: 'stopped', done: 'done' };
export const FX = { light: null, orders: [], sel: null, detail: null, sub: 'orders', lessons: null, brain: null };
let root = null;
let refreshT = 0;

const fmtMoney = (x) => `$${(+x || 0).toFixed(2)}`;
const stLabel = (s) => STATE_LABEL[s] || s || '–';

// ================================================================ header light
function renderLight() {
  const b = document.getElementById('fx-light');
  if (!b) return;
  const L = FX.light || { light: 'idle', text: 'idle' };
  b.className = `fx-light ${L.light || 'idle'}`;
  b.querySelector('.txt').textContent = L.light === 'idle' ? 'factory idle' : (L.text || L.light);
  b.title = `Factory: ${L.text || L.light}${L.runner_alive === false ? ' (no runner)' : ''}. Click for the Factory tab.`;
  const bd = document.getElementById('badge-factory');
  if (bd) { const n = (L.needs || 0) + (L.blocked || []).length; bd.textContent = n || ''; bd.className = 'badge' + (n ? ' hot' : ''); }
  const br = document.getElementById('badge-review');
  if (br) { const n = (L.review || []).length; br.textContent = n || ''; br.className = 'badge' + (n ? ' hot' : ''); }
}

export async function refreshLight() {
  try { FX.light = await api.fx.state(); renderLight(); emit('factoryLight'); } catch { /* server down */ }
}

// ================================================================ actions
export async function sendToFactory(ids, extra = {}) {
  if (!S.plan) { toast('Open a plan first', 'warn'); return null; }
  try {
    const body = { plan: S.plan.slug, ...(ids && ids.length ? { comment_ids: ids } : { all_open: true }), ...extra };
    const r = await api.fx.send(body);
    toast(r.folded_into ? `Added to the order already waiting (${r.folded_into.slice(-4)}): one version for the whole burst`
      : `Sent to the factory: notes sent within 90 s go into the same order`, 'ok', 3500);
    loadMarkers(); refresh();
    return r;
  } catch (e) { toast(`Send to factory: ${e.message}`, 'error'); return null; }
}
async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok, 'ok', 2200); refresh(); return r; } catch (e) { toast(e.message, 'error'); return null; }
}
export async function answer(order, q, k) {
  return act(() => api.fx.answer(order, q.id, k, ''), `Answered: ${k} = ${q.options[k - 1]}`);
}

/** A question as one short line with numbered option buttons. */
export function questionEl(order, q, { compact = false } = {}) {
  const done = q.answered;
  return h('div.fx-q' + (done ? '.done' : '') + (compact ? '.compact' : ''),
    h('div.fx-q-t', h('span.fx-q-ic', '?'), q.text),
    h('div.fx-q-opts', q.options.map((o, i) => h('button.btn' + (done && done.option === i + 1 ? '.primary' : ''),
      { disabled: !!done, title: `${i + 1} = ${o}`, on: { click: () => answer(order, q, i + 1) } }, h('b', `${i + 1}`), ' ', o))));
}

// ================================================================ data
async function refresh() {
  clearTimeout(refreshT);
  refreshT = setTimeout(async () => {
    try {
      const r = await api.fx.orders(); FX.orders = r.orders || [];
      if (!FX.sel && FX.orders.length) FX.sel = (FX.orders.find((o) => ['running', 'picked_up', 'needs_you', 'blocked'].includes(o.state)) || FX.orders[0]).id;
      if (FX.sel) FX.detail = await api.fx.order(FX.sel).catch(() => null);
    } catch { /* not available */ }
    draw();
  }, 120);
  refreshLight();
}

// ================================================================ dock tab
function stepper(d) {
  const st = d.status.state;
  const byStep = {};
  for (const p of d.phases) {
    const cur = byStep[p.step];
    const rank = { running: 5, blocked: 4, needs_you: 4, stopped: 4, done: 3, skipped: 2, waiting: 1 };
    if (!cur || rank[p.state] > rank[cur]) byStep[p.step] = p.state;
  }
  if (['ready', 'done'].includes(st)) byStep.Ready = 'done';
  return h('div.fx-steps', STEPS.filter((s) => byStep[s] !== undefined).map((s) =>
    h('div.fx-step.' + (byStep[s] || 'waiting'), { title: `${s}: ${byStep[s] || 'waiting'}` }, h('span.fx-sdot'), h('span', s))));
}
function rolesEl(d) {
  if (!d.roles.length) return h('div.fx-none', 'No agent has run yet.');
  return h('div.fx-roles', d.roles.map((r) => h('div.fx-role' + (r.running ? '.run' : r.ok === false ? '.bad' : ''),
    h('span.fx-rname', r.role), h('span.fx-ragent', r.agent || ''),
    h('span.fx-rtime', r.seconds ? `${Math.round(r.seconds)}s` : r.running ? 'working…' : ''),
    h('span.fx-rcost', r.cost_usd ? fmtMoney(r.cost_usd) : ''),
    h('span.fx-rlast', r.error ? r.error.slice(0, 120) : (r.last || '')),
    h('button.ib.sm', { title: 'Show this run\'s log', html: icon('doc', 12), on: { click: () => showLog(d.order.id, r.role) } }))));
}
async function showLog(oid, role) {
  try {
    const r = await api.fx.roleLog(oid, role);
    const lb = document.getElementById('lightbox');
    lb.innerHTML = '';
    lb.appendChild(h('pre.fx-log', { on: { click: (e) => e.stopPropagation() } }, `${role}\n\n` + r.lines.slice(-120).join('\n')));
    lb.hidden = false; lb.onclick = () => (lb.hidden = true);
  } catch (e) { toast(e.message, 'error'); }
}
function ledgerEl(d) {
  const rows = (d.card && d.card.rows) || d.ledger.map((r) => ({ ...r, t: r.t_out }));
  if (!rows.length) return null;
  return h('table.tbl.fx-ledger', h('thead', h('tr', h('th', 'note'), h('th', 'change'), h('th', 'where'), h('th', 'proof'))),
    h('tbody', rows.map((r) => h('tr', h('td', r.note), h('td', r.change || ''),
      h('td', r.t !== null && r.t !== undefined ? h('a.tc-link', { on: { click: () => { setPlayhead(+r.t, 'factory'); emit('revealPlayhead'); } } }, tc(+r.t, fps())) : '–'),
      h('td.fx-proof', r.proof || '')))));
}
function eventsEl(d) {
  const evs = d.events.slice(-40).reverse();
  return h('div.fx-events', evs.map((e) => h('div.fx-ev.' + e.state, h('span.fx-ev-at', (e.at || '').slice(11, 19)),
    h('span.fx-ev-st', stLabel(e.state)), h('span.fx-ev-ph', e.role || e.phase || ''), h('span.fx-ev-tx', e.why || e.text || ''))));
}
function detailEl(d) {
  if (!d) return h('div.fx-detail.empty-d', h('div.fx-none', 'Select an order.'));
  const st = d.status, o = d.order;
  const head = h('div.fx-dh',
    h('span.fx-pill.' + st.state, stLabel(st.state)), h('b', `${o.kind} · ${o.plan}`),
    st.version ? h('span.fx-v', `v${st.version}`) : null,
    h('span.fx-meta', `${o.comments.length} comment(s)${(d.folds || []).length ? ` + ${d.folds.length} folded send(s)` : ''} · ${fmtMoney(st.cost_usd)} · ${o.created ? ago(Date.parse(o.created) / 1000) : ''}`),
    h('span.grow'),
    ['queued', 'picked_up', 'running'].includes(st.state) ? h('button.btn', { on: { click: () => act(() => api.fx.stop(o.id), 'Stopping') } }, h('span', { html: icon('stop', 11) }), 'Stop') : null,
    st.state === 'blocked' ? h('button.btn.primary', { title: 'Run the failed phase again (manual only)', on: { click: () => act(() => api.fx.retry(o.id), 'Queued again') } }, 'Retry') : null,
    st.version && ['ready', 'needs_you'].includes(st.state) ? h('button.btn', { on: { click: () => emit('toggleReview', true) } }, 'Review v' + st.version) : null);
  const why = st.why ? h('div.fx-why.' + st.state, st.why) : null;
  const qs = (st.questions || []).length ? h('div.fx-qs', st.questions.map((q) => questionEl(o.id, q))) : null;
  const text = o.text ? h('div.fx-otext', '“' + o.text.slice(0, 300) + '”') : null;
  return h('div.fx-detail', head, stepper(d), why, qs, text,
    h('h5', 'Team'), rolesEl(d), ledgerEl(d) ? h('h5', 'What changed') : null, ledgerEl(d),
    h('h5', 'Activity'), eventsEl(d));
}
function listEl() {
  const L = FX.light || {};
  const bar = h('div.fx-bar',
    h('span.fx-lt.' + (L.light || 'idle'), h('span.dot'), L.text || 'idle'),
    h('span.grow'),
    L.paused || L.backoff ? h('button.btn.primary', { title: 'Clear the pause / login backoff and go on', on: { click: () => act(api.fx.resume, 'Factory resumed') } }, 'Resume')
      : h('button.btn.ghost', { title: 'Nothing new starts until Resume', on: { click: () => act(api.fx.pause, 'Factory paused') } }, 'Pause'));
  const list = h('div.fx-list', FX.orders.length ? FX.orders.filter((o) => !o.folded_into).map((o) => h('button.fx-row' + (o.id === FX.sel ? '.sel' : ''),
    { on: { click: async () => { FX.sel = o.id; FX.detail = await api.fx.order(o.id).catch(() => null); draw(); } } },
    h('span.fx-pill.' + o.state, stLabel(o.state)), h('span.fx-rk', o.kind), h('span.fx-rp', o.plan || ''),
    o.version ? h('span.fx-v', 'v' + o.version) : null,
    o.open_questions ? h('span.fx-qn', `${o.open_questions}?`) : null,
    h('span.fx-rl', o.why || o.line || ''), h('span.fx-ra', o.t ? ago(o.t) : ''))) : h('div.fx-none', 'No orders yet. In the Comments tab press Send to factory.'));
  return h('div.fx-left', bar, list);
}

// ---------------------------------------------------------------- lessons
async function lessonsEl() {
  const box = h('div.fx-lessons', h('div.fx-none', 'Loading…'));
  try {
    const r = FX.lessons = await api.fx.lessons();
    box.innerHTML = '';
    const proj = r.lessons.filter((x) => x.scope === 'project'), glob = r.lessons.filter((x) => x.scope === 'global');
    const inp = h('input.inp', { placeholder: 'Add a rule for this project ("never use the 701 clip")…' });
    inp.addEventListener('keydown', async (e) => { e.stopPropagation(); if (e.key === 'Enter' && inp.value.trim()) { await act(() => api.fx.addLesson({ rule: inp.value.trim() }), 'Lesson added'); draw(); } });
    box.append(h('div.fx-lh', h('b', `${proj.filter((x) => x.status === 'active').length} project lessons`), h('span', ` · ${glob.length} house rules · inbox ${r.inbox_count} · ${r.checks.length} machine checks · lessons ${r.lessons_sha}`)),
      h('div.fx-ladd', inp),
      h('div.fx-lrows', [...proj.slice().reverse(), ...glob.slice(0, 30)].map((x) => h('div.fx-lrow' + (x.status !== 'active' ? '.dead' : ''),
        h('span.fx-lid', x.id), h('span.fx-lrule', x.rule), (x.tags || []).length ? h('span.fx-ltags', x.tags.join(' ')) : null,
        x.check ? h('span.fx-lcheck', { title: 'Enforced by a machine check on every later version' }, 'check ' + x.check) : null,
        x.scope === 'project' && x.status === 'active' ? h('button.ib.sm', { title: 'Supersede (retire) this lesson', html: icon('x', 11), on: { click: async () => { await act(() => api.fx.patchLesson({ id: x.id, status: 'superseded' }), 'Superseded'); draw(); } } }) : null))),
      r.inbox.length ? h('h5', 'Inbox (triaged at the end of each order; never blocks a version)') : null,
      ...r.inbox.slice(-15).map((i) => h('div.fx-lrow.inbox', h('span.fx-lid', i.id), h('span.fx-lrule', `[${i.kind}] ${i.text}`))));
  } catch (e) { box.innerHTML = ''; box.appendChild(h('div.fx-none', e.message)); }
  return box;
}

// ---------------------------------------------------------------- brain
async function brainEl() {
  const box = h('div.fx-brain', h('div.fx-none', 'Loading…'));
  if (!S.plan) { box.innerHTML = ''; box.appendChild(h('div.fx-none', 'Open a plan to see its brain.')); return box; }
  try {
    const b = FX.brain = await api.fx.brain(S.plan.slug);
    box.innerHTML = '';
    const ed = (name, label, ph) => {
      const ta = h('textarea.inp.fx-ta', { rows: 6, placeholder: ph, value: b.files[name] || '' });
      ta.addEventListener('keydown', (e) => e.stopPropagation());
      return h('div.fx-bf', h('div.fx-bfh', h('b', label), h('span.grow'), h('button.btn', { on: { click: () => act(() => api.fx.putBrain(S.plan.slug, name, ta.value), `${label} saved`) } }, 'Save')), ta);
    };
    box.append(h('div.fx-lh', h('b', `Brain of ${S.plan.slug}`), h('span', ` · ${b.decisions} rulings · ${b.locks} locked segments · ${b.notes} notes on file · every role reads this`)),
      h('div.fx-bgrid', ed('STORY.md', 'Story', 'Beats, chapters, the through-line…'), ed('REFERENCE.md', 'Reference', 'What the reference does (forensics writes this)'),
        h('div.fx-bf', h('div.fx-bfh', h('b', 'Rulings (newest wins)')), h('div.fx-dec', (b.files['decisions.jsonl'] || []).slice().reverse().map((r) =>
          h('div.fx-lrow' + (r.supersedes ? '.sup' : ''), h('span.fx-lid', r.id), h('span.fx-lrule', `${r.subject}: ${r.ruling}`), h('span.fx-ltags', r.source))))),
        h('div.fx-bf', h('div.fx-bfh', h('b', 'Clip grades')), h('div.fx-dec', (b.grades || []).map((g) => h('div.fx-lrow', h('span.fx-lid', g.grade || g.ident || ''), h('span.fx-lrule', `${g.clip} ${g.t0 ?? ''}${g.t1 !== null && g.t1 !== undefined ? '–' + g.t1 : ''} ${g.ident || ''}`)))))));
  } catch (e) { box.innerHTML = ''; box.appendChild(h('div.fx-none', e.message)); }
  return box;
}

function draw() {
  if (!root || !root.isConnected) return;
  const a = document.activeElement;
  if (a && root.contains(a) && /INPUT|TEXTAREA/.test(a.tagName) && a.value) return;
  root.innerHTML = '';
  root.classList.add('fx-root');
  const tabs = h('div.fx-tabs', [['orders', 'Orders'], ['lessons', 'Lessons'], ['brain', 'Brain']].map(([k, l]) =>
    h('button.cm-ftab' + (FX.sub === k ? '.on' : ''), { on: { click: () => { FX.sub = k; draw(); } } }, l,
      k === 'lessons' && FX.lessons && FX.lessons.inbox_count ? h('span.cm-fcount', `${FX.lessons.inbox_count}`) : null)),
  h('span.grow'), h('button.ib.sm', { title: 'Extender: features cutroom lacks are built in a git worktree and wait for your Apply (stub in this build)', html: icon('tool', 13), on: { click: () => toast('The extender is a placeholder in this build: a needed feature shows as a question; Apply is not available yet.', 'info', 5000) } }));
  root.appendChild(tabs);
  if (FX.sub === 'lessons') { lessonsEl().then((el) => root.appendChild(el)); return; }
  if (FX.sub === 'brain') { brainEl().then((el) => root.appendChild(el)); return; }
  root.appendChild(h('div.fx-main', listEl(), detailEl(FX.detail)));
}

/** Dock tab renderer (dock.js calls this with a fresh pane). */
export function renderFactory(el) { root = el; draw(); refresh(); }

export function initFactory() {
  const b = document.getElementById('fx-light');
  if (b) b.onclick = () => emit('showDock', 'factory');
  on('factoryState', (d) => { FX.light = d; renderLight(); emit('factoryLight'); });
  on('factoryEvent', (d) => {
    if (d.order && (!FX.sel || ['picked_up'].includes(d.state))) FX.sel = d.order;
    if (d.state && ['ready', 'needs_you', 'blocked'].includes(d.state)) {
      toast(d.state === 'ready' ? `Factory: v${d.version || '?'} is ready to review` : `Factory: ${d.state === 'blocked' ? 'blocked' : 'needs you'}: ${d.why || d.text || ''}`.slice(0, 160),
        d.state === 'ready' ? 'ok' : 'warn', 6000);
      loadMarkers(); emit('reviewRefresh');
    }
    if (root && root.isConnected && FX.sub === 'orders') refresh(); else refreshLight();
  });
  refreshLight();
  window.__factory = { FX, refresh, sendToFactory };
}
