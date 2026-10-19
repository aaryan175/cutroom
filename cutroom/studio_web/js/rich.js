// Clip-aware rendering of Claude's replies: [[clip:..]], [[seg:..]], [[t:..]], [[bin:..]] tokens become
// interactive chips, rows of clip tokens become a picks strip, file paths are hidden and code is folded.
import { S, emit, select, setPlayhead, fps, piecesOfSeg, KIND_COLOR } from './state.js';
import { api, media } from './api.js';
import { md, esc, tc, icon } from './util.js';

const TOKEN = /\[\[(clip|seg|t|bin):([^\]\n]+)\]\]/g;
const MEDIA_EXT = /\.(mp4|mov|m4v|mxf|mts|avi|mkv)$/i;

function short(t) {
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${(s < 10 ? '0' : '') + s.toFixed(s % 1 ? 1 : 0)}`;
}
function parseClip(arg) {
  const [id, range] = arg.split('@');
  let t0 = null, t1 = null;
  if (range) { const [a, b] = range.split('-'); t0 = parseFloat(a); t1 = b !== undefined ? parseFloat(b) : null; }
  return { id: id.trim(), t0: isFinite(t0) ? t0 : null, t1: isFinite(t1) ? t1 : null };
}
const prettyName = (s) => String(s).replace(/[-_]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

function clipChip(arg, big) {
  const { id, t0, t1 } = parseClip(arg);
  const tt = t0 ?? 'mid';
  const range = t0 === null ? '' : t1 !== null ? `${short(t0)}–${short(t1)}` : short(t0);
  const data = `data-clip="${esc(id)}" data-t0="${t0 ?? ''}" data-t1="${t1 ?? ''}"`;
  const img = `<img src="${media.thumb(id, typeof tt === 'number' ? tt : 'mid')}" alt="" loading="lazy" draggable="false">`;
  if (big) {
    return `<div class="xpick" draggable="true" ${data} title="Click to open in Source, drag to the timeline">${img}` +
      `<div class="xmeta"><span class="xnm" data-name-of="${esc(id)}">${esc(id)}</span><span class="xtc">${range}${t1 !== null ? ` · ${(t1 - t0).toFixed(1)}s` : ''}</span></div></div>`;
  }
  return `<span class="xclip" draggable="true" ${data} title="Click to open in Source, drag to the timeline">${img}` +
    `<span class="xnm" data-name-of="${esc(id)}">${esc(id)}</span>${range ? `<span class="xtc">${range}</span>` : ''}</span>`;
}
function segLabel(si, gi) {
  const d = S.plan && S.plan.draft;
  const sec = d && d.sections && d.sections[si];
  const seg = sec && sec.segments && sec.segments[gi];
  if (!seg) return { label: `section ${si + 1}, shot ${gi + 1}`, kind: 'broll' };
  const what = seg.type === 'title' ? `title “${seg.text || ''}”` : seg.type;
  const ps = piecesOfSeg(si, gi);
  const clip = ps[0] && ps[0].clip_rel ? ps[0].clip_rel.split('/').pop().replace(/\.[^.]+$/, '') : '';
  return { label: `${sec.name || 'section ' + (si + 1)} · ${what}${clip ? ' ' + clip : ''}`, kind: seg.type };
}
function chip(kind, arg) {
  if (kind === 'clip') return clipChip(arg, false);
  if (kind === 'seg') {
    const [si, gi] = arg.split(/[.,:]/).map((x) => parseInt(x, 10));
    const { label, kind: k } = segLabel(si, gi);
    return `<span class="xseg" data-seg="${si}.${gi}" title="Select on the timeline"><i style="background:${KIND_COLOR[k] || '#666'}"></i>${esc(label)}</span>`;
  }
  if (kind === 't') {
    const t = parseFloat(arg);
    return `<span class="xt" data-t="${t}" title="Jump the program here">${icon('play2', 10)}${tc(t, fps())}</span>`;
  }
  if (kind === 'bin') {
    const name = arg.trim();
    const b = (S.bins || []).find((x) => x.name === name);
    return `<span class="xbin" data-bin="${esc(name)}" title="Open this bin">${icon('grid', 11)}${esc(b ? b.title : prettyName(name))}</span>`;
  }
  return '';
}

/** Hide file paths from people: media files become their clip name, everything else a plain word. */
export function stripPaths(s) {
  return s
    .replace(/(^|[\s(“"'])((?:~|\.{1,2})?\/(?:[\w.@ -]+\/)+[\w.@-]+|(?:plans|renders|frames|studio|proxies|cache|overnight|sheets)\/[\w./@-]+)/g, (m, pre, p0) => {
      const trail = (p0.match(/[.,;:!?]+$/) || [''])[0];
      const p = trail ? p0.slice(0, -trail.length) : p0;
      const word = (w) => pre + w + trail;
      const base = p.split('/').pop();
      if (MEDIA_EXT.test(base)) return word(base.replace(/\.[^.]+$/, ''));
      if (/\.(json)$/i.test(base) && /plans?\//.test(p)) return word('the edit');
      if (/\.(jpe?g|png|webp)$/i.test(base)) return word('a frame');
      if (/\.md$/i.test(base)) return word('the report');
      return word('the project');
    })
    .replace(/\b[\w-]+\.draft\.json\b/g, 'the edit')
    .replace(/\b[\w-]+_v\d+\.json\b/g, 'a saved version');
}

/** Claude text -> HTML with chips, picks strips and folded code. */
export function richMd(src) {
  src = String(src || '').replace(/\r/g, '');
  const codes = [];
  src = src.replace(/```[^\n]*\n([\s\S]*?)(```|$)/g, (m, body) => { codes.push(body); return `\n\n@@CODE${codes.length - 1}@@\n\n`; });
  src = stripPaths(src);
  const blocks = [];
  // a line made only of clip tokens (2+) is a picks strip
  src = src.split('\n').map((ln) => {
    const toks = ln.match(/\[\[clip:[^\]\n]+\]\]/g) || [];
    if (toks.length >= 2 && !ln.replace(/\[\[clip:[^\]\n]+\]\]/g, '').replace(/[\s,;·|•-]/g, '')) {
      blocks.push(`<div class="picks">${toks.map((t) => clipChip(t.slice(7, -2), true)).join('')}</div>`);
      return `\n\n@@BLOCK${blocks.length - 1}@@\n\n`;
    }
    return ln;
  }).join('\n');
  const inl = [];
  src = src.replace(TOKEN, (m, kind, arg) => { inl.push(chip(kind, arg)); return `@@TOK${inl.length - 1}@@`; });
  let html = md(src);
  html = html.replace(/<p>@@BLOCK(\d+)@@<\/p>/g, (m, i) => blocks[+i])
    .replace(/<p>@@CODE(\d+)@@<\/p>/g, (m, i) => `<details class="xcode"><summary>Details</summary><pre><code>${esc(codes[+i].replace(/\n$/, ''))}</code></pre></details>`)
    .replace(/@@TOK(\d+)@@/g, (m, i) => inl[+i])
    .replace(/@@(BLOCK|CODE)(\d+)@@/g, (m, k, i) => (k === 'BLOCK' ? blocks[+i] : `<details class="xcode"><summary>Details</summary><pre><code>${esc(codes[+i])}</code></pre></details>`));
  return html;
}

// ---------------------------------------------------------------- names + interaction
const names = new Map();
function nameOf(id) {
  if (!names.has(id)) {
    names.set(id, api.clip(id).then((c) => ({ name: (c.rel || id).split('/').pop().replace(/\.[^.]+$/, ''), duration: c.duration }))
      .catch(() => ({ name: id, duration: null })));
  }
  return names.get(id);
}
export function hydrate(root) {
  for (const el of root.querySelectorAll('[data-name-of]:not([data-named])')) {
    el.dataset.named = '1';
    nameOf(el.dataset.nameOf).then((r) => { el.textContent = r.name; });
  }
}
function clipData(el) {
  const t0 = el.dataset.t0 === '' ? null : +el.dataset.t0, t1 = el.dataset.t1 === '' ? null : +el.dataset.t1;
  return { id: el.dataset.clip, t0, t1 };
}
/** Install click / drag handlers for chips inside `root` (event delegation, once). */
export function wireChips(root) {
  root.addEventListener('click', (e) => {
    const c = e.target.closest('.xclip, .xpick');
    if (c) {
      const { id, t0, t1 } = clipData(c);
      emit('loadSource', { id, t: t0 ?? 0, in: t0, out: t1 });
      return;
    }
    const s = e.target.closest('.xseg');
    if (s) {
      const [si, gi] = s.dataset.seg.split('.').map(Number);
      const ps = piecesOfSeg(si, gi);
      if (ps.length) { select(ps.map((p) => p.id), 'set'); setPlayhead(ps[0].start, 'chat'); emit('revealPlayhead'); emit('showDock', 'inspector'); }
      return;
    }
    const t = e.target.closest('.xt');
    if (t) { setPlayhead(+t.dataset.t, 'chat'); emit('revealPlayhead'); emit('focusMonitor', 'program'); return; }
    const b = e.target.closest('.xbin');
    if (b) emit('openBin', b.dataset.bin);
  });
  root.addEventListener('dragstart', (e) => {
    const c = e.target.closest && e.target.closest('.xclip, .xpick');
    if (!c) return;
    const { id, t0, t1 } = clipData(c);
    const payload = { id, in: t0 ?? undefined, out: t1 ?? (t0 !== null ? t0 + 3 : undefined), duration: null };
    e.dataTransfer.setData('application/x-cutroom-clip', JSON.stringify(payload));
    e.dataTransfer.setData('text/plain', id);
    e.dataTransfer.effectAllowed = 'copy';
  });
}

// ---------------------------------------------------------------- quiet tool steps
const RULES = [
  [/cutroom\s+search/, 'Searching footage', 'Searched footage'],
  [/cutroom\s+say/, 'Reading what was said', 'Read what was said'],
  [/cutroom\s+(sheet|sheets|show|info)\b|\/sheets?\/|_sheet\.|sheet\.jpe?g/i, 'Watching clips', 'Watched {n} clip{s}', true],
  [/cutroom\s+frames|\.(jpe?g|png|webp)\b/i, 'Looking at frames', 'Looked at {n} frame{s}', true],
  [/cutroom\s+render/, 'Rendering', 'Rendered'],
  [/cutroom\s+qa/, 'Checking the cut', 'Checked the cut'],
  [/cutroom\s+tags?/, 'Tagging clips', 'Tagged clips'],
  [/cutroom\s+(beats|songs)/, 'Listening to the music', 'Listened to the music'],
  [/cutroom\s+plan\s+check/, 'Checking the edit', 'Checked the edit'],
  [/cutroom\s+bins?/, 'Saving picks', 'Saved picks'],
  [/cutroom\s+(notes|board)/, 'Reading your notes', 'Read your notes'],
  [/cutroom\s+(matchcuts?|match)/, 'Looking for match cuts', 'Looked for match cuts'],
  [/cutroom\s+(index|transcribe|overnight)/, 'Working through the library', 'Worked through the library'],
];
export function stepLabel(name, input) {
  const txt = typeof input === 'string' ? input : input ? (input.command || input.file_path || input.path || input.pattern || JSON.stringify(input)) : '';
  const n = String(name || '');
  if (/^(Edit|Write|MultiEdit|NotebookEdit)$/i.test(n) || /^edited\b/i.test(txt)) {
    if (/plans?\/|\.draft\.json|_v\d+\.json|draft/i.test(txt)) return { key: 'edit', now: 'Editing the timeline', past: 'Edited the timeline' };
    if (/bins?\//.test(txt)) return { key: 'bin', now: 'Saving picks', past: 'Saved picks' };
    return { key: 'notes', now: 'Taking notes', past: 'Took notes' };
  }
  for (const [re, now, past, count] of RULES) if (re.test(txt)) return { key: now, now, past, count: !!count };
  if (/^Read$/i.test(n)) {
    if (/plans?\/|\.json$/i.test(txt)) return { key: 'readplan', now: 'Reading the edit', past: 'Read the edit' };
    return { key: 'read', now: 'Reading notes', past: 'Read notes' };
  }
  if (/^(Grep|Glob)$/i.test(n)) return { key: 'search', now: 'Searching', past: 'Searched' };
  if (/cutroom/.test(txt)) return { key: 'lib', now: 'Working in the library', past: 'Worked in the library' };
  return { key: 'work', now: 'Working', past: 'Worked' };
}
export function stepSummary(steps) {
  const order = [], byKey = new Map();
  for (const s of steps) {
    const L = s.label;
    if (!byKey.has(L.key)) { byKey.set(L.key, { L, n: 0, bad: 0 }); order.push(L.key); }
    const e = byKey.get(L.key); e.n++; if (s.status === 'bad') e.bad++;
  }
  return order.map((k) => {
    const { L, n } = byKey.get(k);
    return L.past.replace('{n}', n).replace('{s}', n === 1 ? '' : 's');
  }).join(' · ');
}
export function stepNow(steps) {
  const last = steps[steps.length - 1];
  if (!last) return '';
  let n = 0;
  for (let i = steps.length - 1; i >= 0 && steps[i].label.key === last.label.key; i--) n++;
  const L = last.label;
  if (L.count && n > 1) return L.now.replace(/^(Watching|Looking at) (\w+)/, (m, v, w) => `${v} ${n} ${w}`) + '…';
  return L.now + '…';
}
