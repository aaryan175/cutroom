// Review comments, frame.io-style: pause, press M (or the comment button under the Program monitor),
// type what should change, draw on the frame (arrow / box / circle / pen), optional range with I/O.
// The Comments dock tab lists them with their thread; "Send to AI" runs one AI turn per comment, in
// timeline order, and the AI's short reply lands on the comment. Storage: studio/markers.json.
import { S, on, emit, setPlayhead, fps, loadMarkers } from './state.js';
import { api } from './api.js';
import { h, icon, tc, toast, ago, clamp, LS } from './util.js';
import { programCtl } from './program.js';
import { richMd, hydrate, wireChips } from './rich.js';

// ---------------------------------------------------------------- tiny local helpers
const SVG = {
  comment: '<path d="M2.5 3.5h11v7.5h-6.2L4 13.6V11H2.5z"/><path d="M5.2 6.6h5.6M5.2 8.6h3.6"/>',
  arrow: '<path d="M3 13 12.5 3.5"/><path d="M7 3.5h5.5V9"/>',
  box: '<rect x="2.5" y="3.5" width="11" height="9" rx="1"/>',
  circle: '<ellipse cx="8" cy="8" rx="5.6" ry="4.8"/>',
  pen: '<path d="M3 13c1.6-.2 2.4-1 3.4-2.6l5.2-7.1a1.2 1.2 0 0 0-1.9-1.4L4.6 9.1C3.7 10.4 3.2 11.5 3 13z"/>',
  undo: '<path d="M5.5 4 2.5 7l3 3"/><path d="M2.5 7h7a4 4 0 0 1 0 8H7"/>',
  clear: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9"/>',
  spark: '<path d="M8 1.8 9.5 6.5 14.2 8 9.5 9.5 8 14.2 6.5 9.5 1.8 8 6.5 6.5z"/>',
  check: '<path d="M3 8.5 6.5 12 13 4.5"/>',
  jump: '<path d="M3 8h9.5M8.5 4l4 4-4 4"/>',
  trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9"/>',
  range: '<path d="M2.5 4v8M13.5 4v8M2.5 8h11"/><path d="M5 6 2.8 8 5 10M11 6l2.2 2L11 10"/>',
  draw: '<path d="M2.5 12.5c2.5 0 3-4 5.5-4s2.5 3 5.5 3"/><circle cx="4" cy="4.5" r="1.5"/>',
  reply: '<path d="M6 4 2.5 7.5 6 11"/><path d="M2.5 7.5H10a3.5 3.5 0 0 1 3.5 3.5v1"/>',
  stop: '<rect x="4.5" y="4.5" width="7" height="7" rx="1.5" fill="currentColor" stroke="none"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  help: '<circle cx="8" cy="8" r="6"/><path d="M6.4 6.3a1.7 1.7 0 0 1 3.2.6c0 1.2-1.6 1.4-1.6 2.5M8 11.4v.1"/>',
};
const ico = (n, s = 14) => `<svg class="ic" viewBox="0 0 16 16" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SVG[n] || ''}</svg>`;
const I = (n, s) => h('span.icw', { html: ico(n, s) });
async function jreq(method, path, body) {
  const r = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => null);
  if (!r.ok) throw new Error((d && (d.error || d.message)) || `${r.status}`);
  return d;
}
const stop = (e) => e.stopPropagation();

export const COLORS = ['#ff453a', '#ffd60a', '#32d74b', '#0a84ff', '#ffffff'];
export const TOOLS = [['arrow', 'Arrow'], ['box', 'Box'], ['circle', 'Circle'], ['pen', 'Pen']];
const STATUS = {
  open: { label: 'Open', cls: 'open' },
  queued: { label: 'Queued', cls: 'queued' },
  working: { label: 'AI working', cls: 'working' },
  done: { label: 'Done', cls: 'done' },
  needs_you: { label: 'Needs you', cls: 'needs' },
};
export const STATUS_COLOR = { open: '#e7a93d', working: '#6b97ff', done: '#5b5b63', needs_you: '#ef5b5b', ref: '#3fb8a8' };

let sel = null;               // selected comment id
let filter = LS.get('cm.filter', 'all');
let run = { running: false, queue: [], results: [], current: null };
const live = new Map();       // comment id -> streamed AI text while it works
let root = null;              // the dock pane while the Comments tab is showing
let showHelp = false;

const notes = () => S.markers.filter((m) => (m.kind || 'note') === 'note');
const isOpen = (m) => !m.status || m.status === 'open' || m.status === 'working' || m.status === 'needs_you';
function statusOf(m) {
  if (run.running && run.queue.includes(m.id) && m.status === 'open') return 'queued';
  return m.status || 'open';
}
function numberOf(m) { return notes().findIndex((x) => x.id === m.id) + 1; }
const rangeTxt = (m) => (m.t_end !== undefined && m.t_end !== null ? `${tc(m.t, fps())} → ${tc(m.t_end, fps())}` : '');

// ================================================================ drawing (canvas over the program frame)
let frameEl = null, canvas = null, cx = null, toolsEl = null;
let draft = null;             // the comment being written: {id, t, t_end, drawing, tool, color, created, prev}
let stroke = null;

function ensureCanvas() {
  if (canvas && canvas.isConnected) return true;
  frameEl = document.querySelector('#p-program .mon-frame');
  if (!frameEl) return false;
  canvas = h('canvas.cm-canvas');
  cx = canvas.getContext('2d');
  frameEl.appendChild(canvas);
  toolsEl = h('div.cm-tools', { hidden: true, on: { pointerdown: stop } });
  frameEl.appendChild(toolsEl);
  new ResizeObserver(() => paint()).observe(frameEl);
  canvas.addEventListener('pointerdown', onDown);
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerup', onUp);
  canvas.addEventListener('pointercancel', onUp);
  return true;
}
function normXY(e) {
  const r = canvas.getBoundingClientRect();
  return [clamp((e.clientX - r.left) / r.width, 0, 1), clamp((e.clientY - r.top) / r.height, 0, 1)];
}
function onDown(e) {
  if (!draft || e.button !== 0) return;
  e.preventDefault(); e.stopPropagation();
  try { canvas.setPointerCapture(e.pointerId); } catch { /* synthetic */ }
  const p = normXY(e);
  stroke = { tool: draft.tool, color: draft.color, points: [p, p] };
  frameEl.classList.add('cm-frame-drawing');
  draft.drawing.push(stroke);
  paint();
}
function onMove(e) {
  if (!stroke) return;
  const p = normXY(e);
  if (stroke.tool === 'pen') {
    const q = stroke.points[stroke.points.length - 1];
    if (Math.hypot(p[0] - q[0], p[1] - q[1]) > 0.002) stroke.points.push(p);
  } else stroke.points[1] = p;
  paint();
}
function onUp() {
  if (!stroke) return;
  const [a, b] = [stroke.points[0], stroke.points[stroke.points.length - 1]];
  if (stroke.tool !== 'pen' && Math.hypot(b[0] - a[0], b[1] - a[1]) < 0.012) draft.drawing.pop();  // a click, not a shape
  if (stroke.tool === 'pen' && stroke.points.length > 2 && stroke.points[1][0] === stroke.points[0][0] && stroke.points[1][1] === stroke.points[0][1]) stroke.points.splice(1, 1);
  stroke = null;
  frameEl.classList.remove('cm-frame-drawing');
  paint(); syncTools(); syncComposer();
}
/** Draw strokes (normalised) on a 2D context of size W x H, the same way the server composites them. */
export function drawStrokes(c, strokes, W, H, scale = 1) {
  const lw = Math.max(2.2, W * 0.0055) * scale;
  for (const s of strokes || []) {
    const P = s.points.map(([x, y]) => [x * W, y * H]);
    for (const [w, col] of [[lw + 3.5 * scale, 'rgba(0,0,0,.55)'], [lw, s.color || '#ff453a']]) {
      c.strokeStyle = col; c.fillStyle = col; c.lineWidth = w; c.lineCap = 'round'; c.lineJoin = 'round';
      c.beginPath();
      if (s.tool === 'pen') {
        c.moveTo(P[0][0], P[0][1]);
        for (const p of P.slice(1)) c.lineTo(p[0], p[1]);
        if (P.length === 1) c.lineTo(P[0][0] + 0.1, P[0][1]);
        c.stroke();
      } else if (s.tool === 'box' || s.tool === 'circle') {
        const [x0, y0] = P[0], [x1, y1] = P[P.length - 1];
        if (s.tool === 'box') c.rect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
        else c.ellipse((x0 + x1) / 2, (y0 + y1) / 2, Math.abs(x1 - x0) / 2, Math.abs(y1 - y0) / 2, 0, 0, Math.PI * 2);
        c.stroke();
      } else {
        const [x0, y0] = P[0], [x1, y1] = P[P.length - 1];
        const ang = Math.atan2(y1 - y0, x1 - x0), head = Math.max(lw * 4.5, 12 * scale);
        const bx = x1 - Math.cos(ang) * head * 0.8, by = y1 - Math.sin(ang) * head * 0.8;
        c.moveTo(x0, y0); c.lineTo(bx, by); c.stroke();
        const k = w > lw ? 1.22 : 1;
        c.beginPath();
        c.moveTo(x1 + Math.cos(ang) * (k - 1) * 6, y1 + Math.sin(ang) * (k - 1) * 6);
        c.lineTo(x1 - head * k * Math.cos(ang - 0.45), y1 - head * k * Math.sin(ang - 0.45));
        c.lineTo(x1 - head * k * Math.cos(ang + 0.45), y1 - head * k * Math.sin(ang + 0.45));
        c.closePath(); c.fill();
      }
    }
  }
}
function shownStrokes() {
  if (draft) return draft.drawing;
  // a selected comment's drawing shows on the paused frame it was drawn on (like a review site)
  const m = sel && S.markers.find((x) => x.id === sel);
  if (m && m.drawing && m.drawing.length && !S.playing && S.programMode === 'draft' && Math.abs(S.playhead - m.t) < 0.6 / fps()) return m.drawing;
  return null;
}
function paint() {
  if (!canvas) return;
  const r = frameEl.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  cx.clearRect(0, 0, W, H);
  const strokes = shownStrokes();
  canvas.classList.toggle('drawing', !!draft);
  canvas.hidden = !strokes && !draft;
  if (strokes) drawStrokes(cx, strokes, W, H, 1);
}
function syncTools() {
  if (!toolsEl) return;
  toolsEl.hidden = !draft;
  if (!draft) return;
  toolsEl.innerHTML = '';
  for (const [k, label] of TOOLS) {
    toolsEl.appendChild(h('button.cm-tool' + (draft.tool === k ? '.on' : ''), { title: `${label} (${k[0].toUpperCase()})`, dataset: { tool: k }, html: ico(k, 14), on: { click: () => { draft.tool = k; syncTools(); } } }));
  }
  toolsEl.appendChild(h('span.cm-tsep'));
  for (const c of COLORS) {
    toolsEl.appendChild(h('button.cm-swatch' + (draft.color === c ? '.on' : ''), { title: 'Colour', style: { '--sw': c }, on: { click: () => { draft.color = c; syncTools(); } } }));
  }
  toolsEl.appendChild(h('span.cm-tsep'));
  toolsEl.appendChild(h('button.cm-tool', { title: 'Undo last mark (⌘Z)', html: ico('undo', 14), disabled: !draft.drawing.length, on: { click: () => { draft.drawing.pop(); paint(); syncTools(); syncComposer(); } } }));
  toolsEl.appendChild(h('button.cm-tool', { title: 'Clear drawing', html: ico('clear', 14), disabled: !draft.drawing.length, on: { click: () => { draft.drawing = []; paint(); syncTools(); syncComposer(); } } }));
}

// ================================================================ composer (under the program monitor)
let compEl = null, taEl = null;
function composerEl() {
  if (compEl && compEl.isConnected) return compEl;
  const mon = document.querySelector('#p-program .mon');
  if (!mon) return null;
  compEl = h('div.cm-composer', { hidden: true, on: { pointerdown: stop } });
  mon.appendChild(compEl);
  return compEl;
}
function syncComposer() {
  const el = composerEl();
  if (!el) return;
  el.hidden = !draft;
  if (!draft) return;
  const m = S.markers.find((x) => x.id === draft.id) || {};
  const rangeOn = draft.t_end !== null && draft.t_end !== undefined;
  if (!el.querySelector('.cm-c-text')) {
    el.innerHTML = '';
    taEl = h('textarea.inp.cm-c-text', { rows: 1, placeholder: 'What should change here? Draw on the frame to point at it.' });
    taEl.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    const grow = () => { taEl.style.height = 'auto'; taEl.style.height = Math.min(64, taEl.scrollHeight + 2) + 'px'; };
    taEl.addEventListener('input', grow);
    el.append(
      h('div.cm-c-top', h('div.cm-c-thumb'), h('span.cm-c-lbl', draft.editing ? 'Edit comment' : 'New comment'), h('div.cm-c-meta'), h('span.grow'),
        h('button.ib.sm.cm-cancel', { title: 'Cancel (Esc)', html: ico('x', 12), on: { click: cancel } })),
      h('div.cm-c-row', taEl, h('button.btn.primary.cm-post', { title: 'Post (Enter)', on: { click: submit } }, I('comment', 13), draft.editing ? 'Save' : 'Post')));
    taEl.value = draft.text || '';
  }
  el.querySelector('.cm-c-meta').replaceChildren(...[
    h('button.cm-chip.tc', { title: 'The comment frame (I sets it to the playhead)', on: { click: () => { setPlayhead(draft.t, 'comments'); emit('revealPlayhead'); } } }, tc(draft.t, fps())),
    rangeOn
      ? h('span.cm-chip.range', { title: 'From the comment frame to here' }, I('range', 11), tc(draft.t_end, fps()), h('button.cm-x', { title: 'Remove the range', html: ico('x', 10), on: { click: () => { draft.t_end = null; syncComposer(); emit('markers'); } } }))
      : h('button.cm-chip.add', { title: 'Move the playhead, then click (or press O) to make it "from here to here"', on: { click: () => markRange('out') } }, I('range', 11), 'Range'),
    draft.drawing.length ? h('span.cm-chip.drew', { title: 'Marks drawn on the frame' }, I('draw', 11), `${draft.drawing.length}`) : null,
  ].filter(Boolean));
  const th = el.querySelector('.cm-c-thumb');
  const src = m.frame_raw_url || m.frame_url;
  if (src && (!th.firstChild || th.firstChild.getAttribute('src') !== src)) th.replaceChildren(h('img', { src, alt: '' }));
  else if (!src && !th.firstChild) th.replaceChildren(h('div.cm-c-spin'));
}

async function startComposer(existing = null) {
  if (!S.plan) { toast('Open a plan to add comments', 'warn'); return; }
  if (!ensureCanvas()) return;
  if (draft) { if (taEl) taEl.focus(); return; }
  programCtl.pause();
  emit('focusMonitor', 'program');
  if (existing) {
    setPlayhead(existing.t, 'comments');
    draft = { id: existing.id, t: existing.t, t_end: existing.t_end ?? null, text: existing.text || '', drawing: JSON.parse(JSON.stringify(existing.drawing || [])),
      tool: LS.get('cm.tool', 'circle'), color: LS.get('cm.color', COLORS[0]), editing: true, created: Promise.resolve(existing.id) };
  } else {
    const t = +S.playhead.toFixed(3);
    const tmp = { id: 'tmp' + Date.now(), plan: S.plan.slug, t, text: '', kind: 'note', status: 'open', author: 'you', thread: [], pending: true };
    S.markers = [...S.markers, tmp].sort((a, b) => a.t - b.t);
    draft = { id: tmp.id, t, t_end: null, text: '', drawing: [], tool: LS.get('cm.tool', 'circle'), color: LS.get('cm.color', COLORS[0]), editing: false };
    const body = { plan: S.plan.slug, t, text: '', kind: 'note' };
    if (S.programMode === 'render' && S.renderFile) body.render = S.renderFile;
    draft.created = api.addMarker(body).then((m) => {
      const i = S.markers.findIndex((x) => x.id === tmp.id);
      if (i >= 0) S.markers[i] = { ...m, t: +m.t, text: S.markers[i].text || '' };
      if (draft && draft.id === tmp.id) draft.id = m.id;
      sel = m.id;
      syncComposer(); emit('markers');
      return m.id;
    }).catch((e) => {
      S.markers = S.markers.filter((x) => x.id !== tmp.id); emit('markers');
      toast(`Comment: ${e.message}`, 'error');
      closeComposer();
      return null;
    });
    sel = tmp.id;
  }
  S.commentDraft = draft;
  syncComposer(); syncTools(); paint();
  emit('markers');
  emit('showDock', 'comments');
  if (taEl) { taEl.value = draft.text || ''; taEl.focus(); }
  requestAnimationFrame(() => { if (taEl && document.activeElement !== taEl) taEl.focus(); });
}
function closeComposer() {
  if (draft) { LS.set('cm.tool', draft.tool); LS.set('cm.color', draft.color); }
  draft = null; S.commentDraft = null; stroke = null;
  if (compEl) compEl.innerHTML = '';
  syncComposer(); syncTools(); paint();
  emit('markers');
}
async function submit() {
  if (!draft) return;
  const d = draft;
  const text = (taEl ? taEl.value : d.text || '').trim();
  if (!text && !d.drawing.length) { toast('Write what should change, or draw on the frame', 'warn', 2200); taEl && taEl.focus(); return; }
  const m0 = S.markers.find((x) => x.id === d.id);
  if (m0) { m0.text = text; m0.drawing = d.drawing; if (d.t_end !== null) m0.t_end = d.t_end; else delete m0.t_end; }
  closeComposer();
  const id = await d.created;
  if (!id) return;
  sel = id;
  try {
    const fields = { text, drawing: d.drawing, t_end: d.t_end ?? null };
    if (d.editing && Math.abs((m0 && m0.t) - d.t) > 1e-3) fields.t = d.t;
    await api.patchMarker(id, fields);
    await loadMarkers();
  } catch (e) { toast(`Comment: ${e.message}`, 'error'); }
}
async function cancel() {
  if (!draft) return;
  const d = draft;
  closeComposer();
  if (!d.editing) {
    const id = await d.created;
    S.markers = S.markers.filter((x) => x.id !== d.id && x.id !== id);
    emit('markers');
    if (id) { try { await api.delMarker(id); } catch { /* already gone */ } }
    loadMarkers();
  }
}
function markRange(which) {
  if (!draft) return;
  const t = +S.playhead.toFixed(3);
  if (which === 'in') {
    if (draft.t_end !== null && t >= draft.t_end) draft.t_end = null;
    draft.t = t;
    if (!draft.editing) draft.editing = false;
    const m = S.markers.find((x) => x.id === draft.id); if (m) m.t = t;
    draft.created.then((id) => id && api.patchMarker(id, { t }).then(() => loadMarkers()).catch(() => {}));
  } else {
    if (t <= draft.t + 0.04) { toast('Move the playhead after the comment frame, then set the end', 'info', 2200); return; }
    draft.t_end = t;
  }
  const m = S.markers.find((x) => x.id === draft.id); if (m) { if (draft.t_end !== null) m.t_end = draft.t_end; else delete m.t_end; }
  syncComposer(); emit('markers');
}

// ================================================================ dock panel
function setFilter(f) { filter = f; LS.set('cm.filter', f); draw(); }
function filtered() {
  const all = notes();
  if (filter === 'open') return all.filter((m) => !m.status || m.status === 'open' || m.status === 'working');
  if (filter === 'needs') return all.filter((m) => m.status === 'needs_you');
  if (filter === 'done') return all.filter((m) => m.status === 'done');
  return all;
}
function pill(st) {
  const s = STATUS[st] || STATUS.open;
  return h('span.cm-pill.' + s.cls, st === 'working' ? h('span.cm-spin') : h('span.cm-pdot'), s.label);
}
function authorEl(a) {
  const ai = a === 'ai';
  return h('span.cm-author' + (ai ? '.ai' : ''), h('span.cm-av', ai ? h('span', { html: ico('spark', 10) }) : 'Y'), ai ? 'AI' : 'You');
}

async function sendToAI(ids) {
  if (!S.plan) return;
  try {
    const body = ids ? { plan: S.plan.slug, ids } : { plan: S.plan.slug, all_open: true };
    run = await jreq('POST', '/api/comments/run', body);
    draw(); emit('markers');
  } catch (e) { toast(e.message, 'warn', 4000); }
}
async function stopRun() {
  try { run = await jreq('POST', '/api/comments/stop'); draw(); } catch (e) { toast(e.message, 'error'); }
}
async function patch(id, fields) {
  try { await api.patchMarker(id, fields); await loadMarkers(); } catch (e) { toast(`Comment: ${e.message}`, 'error'); }
}
function jump(m) {
  setPlayhead(m.t, 'comments'); emit('revealPlayhead'); emit('focusMonitor', 'program');
  sel = m.id; draw(); paint();
}
function lightbox(url) {
  const lb = document.getElementById('lightbox');
  lb.innerHTML = ''; lb.appendChild(h('img', { src: url, alt: '' }));
  lb.hidden = false; lb.onclick = () => (lb.hidden = true);
}

function explainer(compact) {
  const step = (n, title, text, ic) => h('div.cm-step', h('div.cm-step-n', n), h('div', h('div.cm-step-t', I(ic, 13), title), h('div.cm-step-p', text)));
  return h('div.cm-explain' + (compact ? '.compact' : ''),
    step(1, 'Pause and press M', 'Or click the comment button under the Program monitor. The comment pins to that exact frame. Press O for a range ("from here to here").', 'comment'),
    step(2, 'Say it, draw it', 'Type what should change. Circle, box, arrow or scribble on the frame: the AI sees exactly what you marked.', 'draw'),
    step(3, 'Send to AI, one by one', 'Like a real review: it takes each comment in order, makes that one change, and replies on the comment. Undo works per comment.', 'spark'));
}

function runBar() {
  const all = notes();
  const open = all.filter((m) => !m.status || m.status === 'open');
  const bar = h('div.cm-runbar');
  if (run.running) {
    const k = (run.done || []).length + (run.current ? 1 : 0);
    const n = run.total || 1;
    const cur = all.find((m) => m.id === run.current);
    bar.append(
      h('div.cm-prog', h('div.cm-prog-t', h('span.cm-spin'), run.stop_requested ? 'Stopping after this comment…' : `AI is on comment ${k} of ${n}`,
        cur ? h('span.cm-prog-sub', `#${numberOf(cur)} · ${tc(cur.t, fps())}`) : null),
      h('div.cm-bar', h('i', { style: { width: `${Math.round(((run.done || []).length / n) * 100)}%` } }))),
      h('button.btn', { disabled: !!run.stop_requested, title: 'Finish the current comment, leave the rest open', on: { click: stopRun } }, I('stop', 11), 'Stop'));
  } else {
    bar.append(
      h('button.btn' + (open.length ? '.primary' : ''), { disabled: !open.length, title: 'The AI takes them one at a time, in timeline order', on: { click: () => sendToAI(null) } }, I('spark', 13),
        open.length ? `Send ${open.length} open to AI` : 'Nothing open to send'),
      h('span.cm-runhint', 'One at a time, in order'));
  }
  return bar;
}

function card(m) {
  const st = statusOf(m);
  const replies = (m.thread || []).length;
  const last = (m.thread || []).slice(-1)[0];
  const c = h('div.cm-card' + (m.id === sel ? '.sel' : '') + `.st-${STATUS[st] ? STATUS[st].cls : 'open'}`, {
    dataset: { mid: m.id }, tabIndex: 0,
    on: { click: () => { sel = m.id; jump(m); }, keydown: (e) => { if (e.key === 'Enter') jump(m); } },
  },
  h('div.cm-thumb', m.frame_url ? h('img', { src: m.frame_url, alt: '', loading: 'lazy' }) : h('div.cm-c-spin'), h('span.cm-num', `${numberOf(m)}`)),
  h('div.cm-cbody',
    h('div.cm-row1', h('span.cm-chip.tc', tc(m.t, fps())), m.t_end != null ? h('span.cm-chip.range', I('range', 11), tc(m.t_end, fps())) : null, h('span.grow'), pill(st)),
    h('div.cm-text', m.text || (m.drawing && m.drawing.length ? h('i', 'Drawing only') : h('i', 'No note yet'))),
    h('div.cm-meta', authorEl(m.author || 'you'),
      replies ? h('span', `· ${replies} repl${replies > 1 ? 'ies' : 'y'}`) : null,
      last && last.author === 'ai' ? h('span.cm-last', `· ${plain(last.text).slice(0, 90)}`) : null)));
  return c;
}

/** a reply as one plain line: [[t:12.4]] -> 0:12.4, other tokens dropped */
function plain(t) {
  return String(t || '').replace(/\[\[t:([\d.]+)\]\]/g, (m, x) => { const v = +x, mm = Math.floor(v / 60); return `${mm}:${(v - mm * 60).toFixed(1).padStart(4, '0')}`; })
    .replace(/\[\[[^\]]+\]\]/g, '').replace(/\s+/g, ' ').trim();
}

function bubble(author, text, { live: isLive = false, ts = null, kind = '' } = {}) {
  const ai = author === 'ai';
  const b = h('div.cm-bubble' + (ai ? '.ai' : '.you') + (kind ? '.' + kind : ''),
    h('div.cm-bhead', authorEl(author), ts ? h('span.cm-when', ago(ts)) : null, isLive ? h('span.cm-when', h('span.cm-spin'), 'working…') : null));
  const body = h('div.cm-btext.cl-md');
  if (ai) { body.innerHTML = richMd(text || ''); hydrate(body); } else body.textContent = text || '';
  b.appendChild(body);
  return b;
}

function detail(m) {
  if (!m) {
    return h('div.cm-detail.empty-d', h('div.cm-d-empty', I('comment', 16), h('div', 'Select a comment to see its frame and the conversation.')));
  }
  const st = statusOf(m);
  const done = m.status === 'done';
  const d = h('div.cm-detail');
  const head = h('div.cm-d-head',
    h('span.cm-num.big', `${numberOf(m)}`),
    h('button.cm-chip.tc', { title: 'Jump to this frame', on: { click: () => jump(m) } }, tc(m.t, fps())),
    m.t_end != null ? h('span.cm-chip.range', I('range', 11), `to ${tc(m.t_end, fps())} · ${(m.t_end - m.t).toFixed(1)}s`) : null,
    pill(st),
    h('span.cm-when', m.created ? ago(m.created) : ''),
    h('span.grow'),
    h('button.btn.ghost', { title: 'Edit the note, drawing or range', on: { click: () => startComposer(m) } }, I('draw', 12), 'Edit'),
    h('button.btn', { disabled: run.running || done || st === 'working', title: 'The AI makes this one change and replies here', on: { click: () => sendToAI([m.id]) } }, I('spark', 12), st === 'needs_you' ? 'Send answer to AI' : 'Send to AI'),
    h('button.btn.ghost', { on: { click: () => patch(m.id, { status: done ? 'open' : 'done' }) } }, I('check', 12), done ? 'Reopen' : 'Resolve'),
    h('button.ib.sm', { title: 'Delete comment', html: ico('trash', 13), on: { click: async () => { try { await api.delMarker(m.id); if (sel === m.id) sel = null; await loadMarkers(); } catch (e) { toast(e.message, 'error'); } } } }));
  const fr = h('div.cm-d-frame', m.frame_url ? h('img', { src: m.frame_url, alt: '', title: 'Click to enlarge', on: { click: () => lightbox(m.frame_url) } }) : h('div.cm-c-spin'),
    m.drawing && m.drawing.length ? h('span.cm-d-tag', I('draw', 11), `${m.drawing.length} mark${m.drawing.length > 1 ? 's' : ''}`) : null);
  const thread = h('div.cm-thread');
  thread.appendChild(bubble(m.author || 'you', m.text || '(drawing only)', { ts: m.created }));
  for (const r of m.thread || []) {
    const q = r.author === 'ai' && /^needs you/i.test(r.text || '');
    thread.appendChild(bubble(r.author, r.text, { ts: r.ts, kind: q ? 'question' : '' }));
  }
  if (st === 'working') thread.appendChild(bubble('ai', live.get(m.id) || '', { live: true }));
  if (st === 'queued') thread.appendChild(h('div.cm-queued', h('span.cm-pdot'), `Queued: the AI gets to this after ${run.current ? 'the current one' : 'the ones before it'}.`));
  const reply = h('input.inp.cm-reply-in', { placeholder: st === 'needs_you' ? 'Answer the AI’s question…' : 'Reply…' });
  const sendReply = async (andRun) => {
    const text = reply.value.trim();
    if (!text) return;
    reply.value = '';
    await patch(m.id, { reply: { text, author: 'you' }, ...(m.status === 'needs_you' || andRun ? { status: 'open' } : {}) });
    if (andRun) sendToAI([m.id]);
  };
  reply.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); sendReply(m.status === 'needs_you'); } });
  const replyRow = h('div.cm-reply', reply,
    h('button.btn', { on: { click: () => sendReply(false) } }, I('reply', 12), 'Reply'),
    st === 'needs_you' ? h('button.btn.primary', { title: 'Reply and let the AI try again', on: { click: () => sendReply(true) } }, I('spark', 12), 'Reply & send') : null);
  d.append(head, h('div.cm-d-body', fr, h('div.cm-d-conv', thread, replyRow)));
  wireChips(thread);
  requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  return d;
}

function draw() {
  if (!root || !root.isConnected) return;
  const a = document.activeElement;
  const typing = a && root.contains(a) && a.tagName === 'INPUT' && a.value;
  if (typing) {
    if (!pendingDraw) { pendingDraw = true; a.addEventListener('blur', () => { pendingDraw = false; draw(); }, { once: true }); }
    return;
  }
  const all = notes();
  root.innerHTML = '';
  root.classList.add('cm-root');
  if (!all.length || showHelp) {
    root.appendChild(h('div.cm-empty',
      h('div.cm-empty-h', h('span.cm-empty-ic', { html: ico('comment', 18) }), h('div', h('h4', all.length ? 'How comments work' : 'Give feedback like on a review site'),
        h('p', 'Every comment is pinned to a frame. The AI works through them one by one and answers on each one.'))),
      explainer(false),
      h('div.cm-empty-acts',
        h('button.btn.primary', { on: { click: () => startComposer() } }, I('comment', 13), 'Add a comment here', h('kbd', 'M')),
        all.length ? h('button.btn.ghost', { on: { click: () => { showHelp = false; draw(); } } }, 'Back to comments') : null)));
    return;
  }
  const counts = { all: all.length, open: all.filter((m) => !m.status || m.status === 'open' || m.status === 'working').length,
    needs: all.filter((m) => m.status === 'needs_you').length, done: all.filter((m) => m.status === 'done').length };
  const tabs = h('div.cm-filters', ...[['all', 'All'], ['open', 'Open'], ['needs', 'Needs you'], ['done', 'Done']].map(([k, l]) =>
    h('button.cm-ftab' + (filter === k ? '.on' : '') + (k === 'needs' && counts.needs ? '.hot' : ''), { on: { click: () => setFilter(k) } }, l, h('span.cm-fcount', `${counts[k]}`))));
  const list = h('div.cm-list');
  const rows = filtered();
  if (!rows.length) list.appendChild(h('div.cm-none', filter === 'needs' ? 'Nothing needs you right now.' : filter === 'done' ? 'Nothing resolved yet.' : 'No open comments.'));
  for (const m of rows) list.appendChild(card(m));
  const left = h('div.cm-left',
    h('div.cm-lhead', tabs, h('span.grow'),
      h('button.ib.sm', { title: 'How comments work', html: ico('help', 14), on: { click: () => { showHelp = true; draw(); } } }),
      h('button.ib.sm', { title: 'New comment at the playhead (M)', html: ico('comment', 14), on: { click: () => startComposer() } })),
    runBar(), list);
  const m = S.markers.find((x) => x.id === sel && (x.kind || 'note') === 'note') || null;
  root.append(left, detail(m));
  const selEl = list.querySelector('.cm-card.sel');
  if (selEl) selEl.scrollIntoView({ block: 'nearest' });
}
let pendingDraw = false;

/** Dock tab renderer (dock.js calls this with a fresh pane). */
export function renderComments(el) { root = el; draw(); }

// ================================================================ events
function onRunEvent(d) {
  if (d.run) run = d.run;
  if (d.type === 'chat' && d.event && d.event.type === 'text') {
    live.set(d.id, (live.get(d.id) || '') + (d.event.text || ''));
    if (sel === d.id) { const b = root && root.querySelector('.cm-bubble.ai:last-child .cm-btext'); if (b) { b.innerHTML = richMd(live.get(d.id)); hydrate(b); return; } }
    return;
  }
  if (d.type === 'working') { live.set(d.id, ''); if (!sel || !S.markers.some((x) => x.id === sel && x.status === 'working')) sel = d.id; }
  if (d.type === 'result') { live.delete(d.id); }
  if (d.type === 'end') {
    const res = (run.results || []);
    if (res.length) {
      const nd = res.filter((r) => r.status === 'done').length, nn = res.filter((r) => r.status === 'needs_you').length;
      toast(`AI went through ${res.length} comment${res.length > 1 ? 's' : ''}: ${nd} done${nn ? `, ${nn} need${nn > 1 ? '' : 's'} you` : ''}${(d.left || []).length ? `, ${d.left.length} left open` : ''}`, nn ? 'warn' : 'ok', 6000);
    }
  }
  loadMarkers();
  draw();
}

export function initComments() {
  ensureCanvas();
  on('addMarker', () => startComposer());
  on('openComment', (m) => { sel = m.id; emit('showDock', 'comments'); jump(m); });
  on('commentMark', markRange);
  on('commentsRun', onRunEvent);
  on('markers', () => {
    if (draft) syncComposer();
    draw(); paint();
  });
  on('playhead', () => { if (canvas && !draft) paint(); });
  on('playing', () => paint());
  on('programMode', () => paint());
  on('layout', () => paint());
  on('plan', ({ by } = {}) => { if (by === 'open') { sel = null; if (draft) closeComposer(); } });
  // drawing shortcuts while the composer is open and focus is not in a text field
  window.addEventListener('keydown', (e) => {
    if (!draft) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '');
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel(); return; }
    if (typing) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.stopPropagation(); draft.drawing.pop(); paint(); syncTools(); syncComposer(); return; }
    const t = { a: 'arrow', b: 'box', c: 'circle', p: 'pen' }[e.key.toLowerCase()];
    if (t && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); e.stopPropagation(); draft.tool = t; syncTools(); }
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); submit(); }
  }, true);
  jreq('GET', '/api/comments/run').then((r) => { run = r; draw(); }).catch(() => {});
  window.__comments = { start: startComposer, submit, cancel, get draft() { return draft; }, get sel() { return sel; }, select: (id) => { sel = id; draw(); paint(); }, sendToAI, drawStrokes };
}
