// Central store. Panels subscribe to named changes; nothing else holds shared state.
import { api } from './api.js';
import { toast, debounce, LS } from './util.js';

const subs = new Map();
export function on(evt, fn) {
  if (!subs.has(evt)) subs.set(evt, new Set());
  subs.get(evt).add(fn);
  return () => subs.get(evt).delete(fn);
}
export function emit(evt, data) {
  for (const fn of subs.get(evt) || []) {
    try { fn(data); } catch (e) { console.error(`[${evt}]`, e); }
  }
}

export const S = {
  project: null,
  plan: null,            // {slug, draft, versions[], canUndo, canRedo}
  tl: null,              // normalised timeline (see normTimeline)
  playhead: 0,
  playing: false,
  activeMonitor: 'program',
  selection: new Set(),  // piece ids
  source: { clip: null, detail: null, in: null, out: null, t: 0 },
  markers: [],
  jobs: new Map(),
  governor: null,
  overnight: null,
  qa: null,              // {file, report}
  programMode: 'draft', // draft | render
  renderFile: null,
  snapping: LS.get('snapping', true),
  safeArea: LS.get('safeArea', false),
  ctxInclude: LS.get('ctxInclude', { playhead: true, selection: true, source: true, markers: true }),
  attachments: [],       // [{url, label, source, t}]
  bins: [],              // Claude-written bins [{name, title, items, updated}]
  busy: false,
};

// ---------------------------------------------------------------- timeline normalisation
export const KIND_COLOR = {
  talk: '#3d7d84', broll: '#5f58a6', montage: '#8e5585', matchcut: '#a77a37',
  title: '#7a6c4a', still: '#4c6f93', black: '#3a3a40', cutaway: '#5f58a6',
  caption: '#5d5a3c', overlay: '#86633a', dialogue: '#3b7452', music: '#3f5f7d',
};

function trackOf(p) {
  const t = p.track;
  if (t === undefined || t === null) return p.cutaway ? 'V2' : 'V1';
  if (typeof t === 'number') return 'V' + t;
  return String(t).toUpperCase();
}

/** Accepts the backend's resolved timeline in a few tolerated shapes and returns
 *  {fps, duration, pieces (V1+V2), v1, v2, captions, overlays, dialogue, music, sections, beats} */
export function normTimeline(raw, draft) {
  raw = raw || {};
  const pieces0 = Array.isArray(raw) ? raw : (raw.pieces || raw.segments || []);
  const fps = raw.fps || (draft && draft.output && draft.output.fps) || 25;
  const pieces = pieces0.map((p, i) => {
    const sec = p.sec ?? p.section ?? 0;
    const seg = Array.isArray(p.seg) ? p.seg[1] : (p.seg ?? 0);
    const kind = (p.type && p.type !== p.kind && ['clip', 'still', 'title', 'black'].includes(p.kind)) ? p.type : (p.kind || p.type || 'broll');
    return {
      ...p, id: String(p.id ?? `p${i}`), sec: Array.isArray(p.seg) ? p.seg[0] : sec, seg, kind,
      track: trackOf(p), start: +p.start, end: +p.end,
      in: p.in === null || p.in === undefined ? null : +p.in,
      out: p.out === null || p.out === undefined ? null : +p.out,
      captions: p.captions || [], overlays: p.overlays || [],
    };
  }).filter((p) => isFinite(p.start) && isFinite(p.end) && p.end > p.start);
  pieces.sort((a, b) => a.start - b.start);
  const v1 = pieces.filter((p) => p.track === 'V1');
  const v2 = pieces.filter((p) => p.track === 'V2');
  const v3p = pieces.filter((p) => p.track === 'V3');
  const a1p = pieces.filter((p) => p.track === 'A1');
  const a2p = pieces.filter((p) => p.track === 'A2');
  // flatten + dedupe captions / overlays (they may be repeated on every piece of a segment)
  const capSeen = new Set(), ovSeen = new Set();
  const captions = [], overlays = [];
  for (const p of pieces) {
    for (const c of p.captions) {
      const k = `${(+c.t0).toFixed(2)}|${c.text}`;
      if (!capSeen.has(k)) { capSeen.add(k); captions.push({ ...c, t0: +c.t0, t1: +c.t1, piece: p.id }); }
    }
    if (v3p.length) continue;
    for (const o of p.overlays) {
      const k = `${(+o.t0).toFixed(2)}|${o.text}|${o.kind}`;
      if (!ovSeen.has(k)) { ovSeen.add(k); overlays.push({ ...o, t0: +o.t0, t1: +o.t1, piece: p.id }); }
    }
  }
  for (const p of v3p) overlays.push({ kind: p.kind, text: p.text ?? p.label, sub: p.sub_text || '', position: p.position, t0: p.start, t1: p.end, piece: p.id });
  for (const c of raw.captions || []) captions.push({ ...c, t0: +c.t0, t1: +c.t1 });
  for (const o of raw.overlays || []) overlays.push({ ...o, t0: +o.t0, t1: +o.t1 });
  captions.sort((a, b) => a.t0 - b.t0);
  overlays.sort((a, b) => a.t0 - b.t0);

  let duration = +raw.duration || 0;
  for (const p of pieces) duration = Math.max(duration, p.end);

  // sections: [{index,name,start,end,music,beats}]
  const draftSecs = (draft && draft.sections) || [];
  let sections = (raw.sections || []).map((s, i) => ({
    index: s.index ?? s.i ?? i, name: s.name ?? (draftSecs[i] && draftSecs[i].name) ?? `section ${i + 1}`,
    start: +s.start, end: +s.end, music: s.music ?? (draftSecs[s.index ?? i] || {}).music ?? null,
    beats: s.beats || s.beats_tl || (s.music && s.music.beats_in_section) || s.beats_in_section || null,
  }));
  if (!sections.length && pieces.length) {
    const by = new Map();
    for (const p of v1) {
      const r = by.get(p.sec) || { index: p.sec, start: p.start, end: p.end };
      r.start = Math.min(r.start, p.start); r.end = Math.max(r.end, p.end); by.set(p.sec, r);
    }
    sections = [...by.values()].sort((a, b) => a.start - b.start).map((r) => ({
      ...r, name: (draftSecs[r.index] && draftSecs[r.index].name) || `section ${r.index + 1}`,
      music: (draftSecs[r.index] && draftSecs[r.index].music) || null, beats: null,
    }));
  }
  // raw.beats may be {secIndex: [...]}, or [[...], ...]
  if (raw.beats) {
    for (const s of sections) {
      const b = Array.isArray(raw.beats) ? raw.beats[s.index] : raw.beats[s.index] || raw.beats[String(s.index)];
      if (b && !s.beats) s.beats = b;
    }
  }
  const beats = [];
  for (const s of sections) {
    if (!s.beats) { s.beats = []; continue; }
    // beats may be section-relative (all < section length and start at ~0) or timeline-absolute
    const rel = s.beats.length && s.start > 0.5 && s.beats[s.beats.length - 1] < (s.end - s.start) + 0.01 && s.beats[0] < s.start - 0.01;
    s.beats = s.beats.map((b) => +b + (rel ? s.start : 0)).filter((b) => b >= s.start - 0.01 && b <= s.end + 0.01);
    beats.push(...s.beats);
  }
  beats.sort((a, b) => a - b);
  const music = sections.filter((s) => s.music && (s.music.song || typeof s.music === 'string'))
    .map((s) => ({ id: `m${s.index}`, sec: s.index, start: s.start, end: s.end, song: s.music.song || s.music, title: s.music.title, offset: +(s.music.offset || 0), beats: s.beats, name: s.name }));
  const v1At = (t) => v1.find((q) => t >= q.start && t < q.end);
  const dialogue = a1p.length
    ? a1p.map((p) => { const v = v1At((p.start + p.end) / 2); return { id: p.id, piece: v ? v.id : p.id, start: p.start, end: p.end, clip: p.clip, in: p.in, sec: p.sec, seg: p.seg, kind: p.kind || 'dialogue' }; })
    : v1.filter((p) => p.kind === 'talk' || (p.audio && p.audio !== 'music') || p.voiceover)
      .map((p) => ({ id: 'a' + p.id, piece: p.id, start: p.start, end: p.end, clip: p.clip, in: p.in, sec: p.sec, seg: p.seg, kind: 'dialogue' }));
  const pcs = pieces.filter((p) => p.track === 'V1' || p.track === 'V2');
  return { fps, duration, pieces: pcs, v1, v2, captions, overlays, dialogue, music, sections, beats, raw, error: raw.error || null };
}

export function pieceAt(t, track = 'V1') {
  const arr = S.tl ? (track === 'V2' ? S.tl.v2 : S.tl.v1) : [];
  let lo = 0, hi = arr.length - 1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1, p = arr[m];
    if (t < p.start) hi = m - 1; else if (t >= p.end) lo = m + 1; else return p;
  }
  return null;
}
export function pictureAt(t) { return pieceAt(t, 'V2') || pieceAt(t, 'V1'); }
export function sectionAt(t) { return S.tl ? S.tl.sections.find((s) => t >= s.start && t < s.end) || null : null; }
export function segmentOf(p) {
  const d = S.plan && S.plan.draft;
  if (!d || !p) return null;
  const sec = d.sections && d.sections[p.sec];
  return sec && sec.segments ? sec.segments[p.seg] || null : null;
}
export function piecesOfSeg(sec, seg) { return S.tl ? S.tl.v1.filter((p) => p.sec === sec && p.seg === seg) : []; }
export function selectedPieces() { return S.tl ? S.tl.pieces.filter((p) => S.selection.has(p.id)) : []; }
/** unique [sec, seg] pairs of the selection (V1 pieces), sorted */
export function selectedSegs() {
  const m = new Map();
  for (const p of selectedPieces()) if (p.track === 'V1') m.set(`${p.sec}:${p.seg}`, [p.sec, p.seg]);
  return [...m.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
}
export const fps = () => (S.tl && S.tl.fps) || 25;

// ---------------------------------------------------------------- plan loading + ops
export function applyPlanResponse(r, { by = 'hand' } = {}) {
  if (!r) return;
  r = normPlanResponse(r);
  const prev = S.tl;
  if (r.draft !== undefined) S.plan.draft = r.draft;
  if (r.version_files) S.plan.versions = r.version_files;
  if (r.can_undo !== undefined) S.plan.canUndo = !!r.can_undo;
  if (r.can_redo !== undefined) S.plan.canRedo = !!r.can_redo;
  if (r.timeline !== undefined) S.tl = normTimeline(r.timeline, S.plan.draft);
  // keep selection only for pieces that still exist
  const ids = new Set(S.tl ? S.tl.pieces.map((p) => p.id) : []);
  for (const id of [...S.selection]) if (!ids.has(id)) S.selection.delete(id);
  const changed = diffPieces(prev, S.tl);
  emit('plan', { by, changed });
  emit('selection');
}

function diffPieces(a, b) {
  const out = new Set();
  if (!a || !b) return out;
  const old = new Map(a.pieces.map((p) => [p.id, p]));
  for (const p of b.pieces) {
    const o = old.get(p.id);
    if (!o || o.clip !== p.clip || Math.abs((o.in ?? 0) - (p.in ?? 0)) > 0.02 || Math.abs((o.out ?? 0) - (p.out ?? 0)) > 0.02 ||
      Math.abs((o.end - o.start) - (p.end - p.start)) > 0.02 || o.kind !== p.kind) out.add(p.id);
  }
  return out;
}

export async function openPlan(slug) {
  if (!slug) { S.plan = null; S.tl = null; S.selection.clear(); emit('plan', { by: 'open' }); return; }
  try {
    const r = normPlanResponse(await api.plan(slug));
    S.plan = { slug: r.slug || slug, draft: r.draft, versions: r.version_files || [], canUndo: !!r.can_undo, canRedo: !!r.can_redo };
    S.tl = normTimeline(r.timeline, r.draft);
    S.selection.clear();
    S.playhead = Math.min(S.playhead, S.tl.duration || 0);
    LS.set('lastPlan', slug);
    emit('plan', { by: 'open' });
    emit('playhead', 'force');
    emit('selection');
    loadMarkers();
  } catch (e) {
    toast(`Couldn't open plan ${slug}: ${e.message}`, 'error');
  }
}

export async function reloadPlan(by = 'claude') {
  if (!S.plan) return;
  try {
    const r = await api.plan(S.plan.slug);
    applyPlanResponse({ ...r }, { by });
  } catch (e) { toast(`Reload failed: ${e.message}`, 'error'); }
}

let opChain = Promise.resolve();
/** Serialised plan op; returns the response or null on failure. */
export function op(o, { quiet = false } = {}) {
  if (!S.plan) { toast('Open or create a plan first', 'warn'); return Promise.resolve(null); }
  const slug = S.plan.slug;
  opChain = opChain.then(async () => {
    S.busy = true; emit('busy');
    try {
      const r = await api.op(slug, o);
      if (S.plan && S.plan.slug === slug) applyPlanResponse(r, { by: 'hand' });
      return r;
    } catch (e) {
      if (!quiet) toast(`${o.op} failed: ${e.message}`, 'error');
      return null;
    } finally { S.busy = false; emit('busy'); }
  });
  return opChain;
}
export async function undo() {
  if (!S.plan) return;
  try { applyPlanResponse(await api.undo(S.plan.slug), { by: 'undo' }); } catch (e) { toast(`Undo: ${e.message}`, 'error'); }
}
export async function redo() {
  if (!S.plan) return;
  try { applyPlanResponse(await api.redo(S.plan.slug), { by: 'redo' }); } catch (e) { toast(`Redo: ${e.message}`, 'error'); }
}

// ---------------------------------------------------------------- playhead / selection
export function setPlayhead(t, from = 'ui') {
  const d = S.tl ? S.tl.duration : 0;
  t = Math.max(0, Math.min(d || 0, t));
  if (Math.abs(t - S.playhead) < 1e-6 && from !== 'force') return;
  S.playhead = t;
  emit('playhead', from);
  pushContext();
}
export function select(ids, mode = 'set') {
  if (mode === 'set') S.selection = new Set(ids);
  else if (mode === 'add') ids.forEach((i) => S.selection.add(i));
  else if (mode === 'toggle') ids.forEach((i) => (S.selection.has(i) ? S.selection.delete(i) : S.selection.add(i)));
  emit('selection');
  pushContext();
}

// ---------------------------------------------------------------- markers
export async function loadMarkers() {
  try {
    const r = await api.markers(S.plan ? S.plan.slug : undefined);
    S.markers = (Array.isArray(r) ? r : r.markers || []).map((m) => ({ ...m, t: +m.t }));
    S.markers.sort((a, b) => a.t - b.t);
    emit('markers');
  } catch (e) { console.warn('markers', e); }
}

// ---------------------------------------------------------------- pointing context
export function buildContext() {
  const ph = S.playhead, p = pictureAt(ph), v1 = pieceAt(ph, 'V1');
  const sec = sectionAt(ph);
  const brief = (q) => q && ({ id: q.id, sec: q.sec, seg: q.seg, kind: q.kind, track: q.track, clip: q.clip, clip_rel: q.clip_rel,
    label: q.label, start: +q.start.toFixed(3), end: +q.end.toFixed(3), in: q.in, out: q.out,
    src_t: q.in !== null ? +(q.in + (ph - q.start) * (q.speed || 1)).toFixed(3) : null });
  const src = S.source;
  const open = S.markers.filter((m) => m.status !== 'done');
  return {
    plan: S.plan ? S.plan.slug : null,
    program_mode: S.programMode,
    render: S.programMode === 'render' ? S.renderFile : null,
    playhead: { t: +ph.toFixed(3), tc: tcFmt(ph), section: sec ? sec.name : null, piece: brief(p), v1: p !== v1 ? brief(v1) : undefined },
    selection: selectedPieces().map(brief),
    source: src.clip ? { clip: src.clip.id, rel: src.clip.rel, t: +(+src.t).toFixed(3), in: src.in, out: src.out } : null,
    markers: open.map((m) => ({ id: m.id, t: m.t, text: m.text, kind: m.kind, frame_url: m.frame_url })),
    include: { ...S.ctxInclude },
  };
}
function tcFmt(t) {
  const f = fps(), fr = Math.round(t * f), s = Math.floor(fr / Math.round(f));
  return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}:${String(fr % Math.round(f)).padStart(2, '0')}`;
}
const postCtx = debounce(() => {
  api.context(buildContext()).catch(() => { /* backend may be restarting; next change re-posts */ });
}, 300);
export function pushContext() { postCtx(); emit('context'); }

// ---------------------------------------------------------------- adapters (tolerate backend shape drift)
const vname = (v) => (typeof v === 'string' ? v : (v && (v.file || v.name)) || '');
export function normProject(p) {
  if (!p) return p;
  p.plans = (p.plans || []).map((x) => ({
    ...x, versions: (x.versions || []).map(vname).filter(Boolean),
    updated: x.updated ?? x.mtime, draft: !!x.draft,
  }));
  p.renders = (p.renders || []).map((r) => {
    const file = r.file || r.rel || r.name;
    const stem = String(file).replace(/\.[^.]+$/, '');
    const qaObj = r.qa && typeof r.qa === 'object' ? r.qa : null;
    const m = String(file).split('/').pop().match(/^(.*?)_v(\d+)/);
    return {
      ...r, file, created: r.created ?? r.mtime,
      plan: r.plan || (m ? m[1] : null), version: r.version ?? (m ? +m[2] : null),
      preview: r.preview ?? /_preview\b/.test(file),
      qa: typeof r.qa === 'string' ? r.qa : (qaObj || r.qa_html ? `${stem}.qa.json` : null),
      qa_status: r.qa_status || (qaObj && qaObj.status) || null,
    };
  });
  p.references = (p.references || p.reference || []).map((r) => ({ ...r, file: r.file || r.rel || r.name }))
    .map((r) => ({ ...r, file: String(r.file).replace(/^reference\//, '') }));
  return p;
}
export function normPlanResponse(r) {
  if (!r) return r;
  const out = { ...r };
  if (r.version_files) out.version_files = r.version_files.map(vname).filter(Boolean);
  if (r.history && out.can_undo === undefined) { out.can_undo = !!r.history.can_undo; out.can_redo = !!r.history.can_redo; }
  if (r.timeline === null && r.timeline_error) out.timeline = { pieces: [], sections: [], duration: 0, error: r.timeline_error };
  return out;
}
export function normJob(j) {
  if (!j) return j;
  return { ...j, label: j.label || j.title || j.id, started: j.started || j.created,
    log_tail: Array.isArray(j.log_tail) ? j.log_tail.join('\n') : j.log_tail };
}
export function normGovernor(g) {
  if (!g || typeof g !== 'object') return {};
  if (g.health && g.load && !Array.isArray(g.load) && typeof g.load === 'object') return g; // already canonical
  const lim = g.limits || {};
  const la = Array.isArray(g.load) ? g.load[0] : (typeof g.load === 'number' ? g.load : null);
  const out = {
    available: g.available !== false,
    load: la === null ? {} : { now: la, limit: lim.max_load ?? null, cores: g.cpus ?? null },
    ram: g.ram_free_gb !== undefined ? { free_gb: g.ram_free_gb, total_gb: g.ram_total_gb, min_free_gb: lim.min_free_ram_gb } : {},
    disk: g.disk_free_gb !== undefined && g.disk_free_gb !== null ? { free_gb: g.disk_free_gb, min_free_gb: lim.min_free_disk_gb } : {},
    slots: [], throttle: null, blocked: g.blocked || [], note: g.note || g.error || null,
  };
  const byKind = new Map();
  for (const s of g.slots || []) {
    const k = s.kind || s.name || 'slot';
    const e = byKind.get(k) || { name: k, used: 0, max: 0, holders: [] };
    e.max++; if (s.busy) { e.used++; e.holders.push({ job: s.label || s.pid, pid: s.pid }); }
    byKind.set(k, e);
  }
  out.slots = [...byKind.values()];
  const th = Array.isArray(g.throttled) ? g.throttled : (g.throttled ? [g.throttled] : []);
  out.throttle = th.length ? th.join('; ') : null;
  if (out.blocked.length) out.throttle = [out.throttle, 'blocked: ' + out.blocked.join('; ')].filter(Boolean).join(' · ');
  const over = la !== null && (lim.max_load ? la > lim.max_load : (g.cpus ? la > g.cpus : false));
  out.health = out.blocked.length ? 'bad' : (th.length || over) ? 'warn' : 'ok';
  return out;
}

export function normOvernight(o) {
  if (!o || typeof o !== 'object') return {};
  const out = { ...o };
  if (o.stages && !Array.isArray(o.stages)) {
    out.stages = Object.entries(o.stages).map(([name, v]) => ({ name, ...(v || {}) }));
  }
  if (out.eta_s === undefined && Array.isArray(out.stages)) {
    const e = out.stages.filter((s) => s.status === 'running').map((s) => s.eta_s).filter((x) => x);
    if (e.length) out.eta_s = Math.max(...e);
  }
  if (Array.isArray(out.throttle)) out.throttle = out.throttle.join('; ') || null;
  return out;
}
