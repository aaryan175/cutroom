// Import: drop files anywhere (Footage / Reference video / Song / Image), paste a link, the header Import
// menu, the Add reference dialog, and a small queue of uploads + import jobs. Contract: docs/STUDIO.md "Import".
// Footage is never copied: the original is found on this computer and its folder joins the library.
import { S, on, emit } from './state.js';
import { api } from './api.js';
import { h, icon, toast, isTyping } from './util.js';

// ---------------------------------------------------------------- icons (same 16px / 1.5 stroke family)
const XI = {
  import: '<path d="M8 2.5v7.5M5 7l3 3 3-3"/><path d="M2.5 10.5v2a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-2"/>',
  ref: '<rect x="1.5" y="3" width="13" height="10" rx="1.5"/><path d="M5.5 3v10M1.5 6.5h4M1.5 9.5h4"/><path d="M8.5 6.3v3.4l2.8-1.7z" fill="currentColor" stroke="none"/>',
  image: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><circle cx="5.8" cy="6.2" r="1.2"/><path d="m2.5 12 3.8-3.6 2.4 2.2 2-1.8 2.8 2.6"/>',
  footage: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M5 2.5v11M11 2.5v11M2 6h3M2 10h3M11 6h3M11 10h3"/>',
  clock: '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 1.4"/>',
  q: '<circle cx="8" cy="8" r="6.2"/><path d="M6.3 6.3a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.4"/><circle cx="8" cy="11.6" r=".35" fill="currentColor"/>',
};
export function xicon(name, size = 16) {
  return `<svg class="ic" viewBox="0 0 16 16" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${XI[name] || ''}</svg>`;
}
const ic = (n, s) => h('span.icw', { html: XI[n] ? xicon(n, s) : icon(n, s) });

// ---------------------------------------------------------------- "?" explainers
let openPop = null;
function closePop() { if (openPop) { openPop.el.remove(); openPop.btn.classList.remove('on'); openPop = null; } }
/** A small "?" button that explains the thing next to it in one plain sentence. */
export function explain(text, { title } = {}) {
  const b = h('button.qhelp', { type: 'button', title: 'What is this?', 'aria-label': 'What is this?', html: xicon('q', 14) });
  b.addEventListener('click', (e) => {
    e.stopPropagation(); e.preventDefault();
    if (openPop && openPop.btn === b) { closePop(); return; }
    closePop();
    const el = h('div.qpop', { role: 'tooltip', on: { click: (ev) => ev.stopPropagation() } }, title ? h('b', title) : null, h('span', text));
    document.body.appendChild(el);
    const r = b.getBoundingClientRect();
    const w = Math.min(300, window.innerWidth - 16);
    el.style.width = w + 'px';
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    const below = r.bottom + 8 + el.offsetHeight < window.innerHeight;
    el.style.left = left + 'px';
    el.style.top = (below ? r.bottom + 8 : r.top - 8 - el.offsetHeight) + 'px';
    el.style.setProperty('--ax', (r.left + r.width / 2 - left) + 'px');
    el.classList.add(below ? 'below' : 'above');
    b.classList.add('on');
    openPop = { el, btn: b };
  });
  return b;
}

// ---------------------------------------------------------------- kinds
const KIND = {
  footage: { title: 'Footage', icon: 'footage', desc: 'Adds the clips to your library. They stay where they are — nothing is copied.', accept: 'video' },
  reference: { title: 'Reference video', icon: 'ref', desc: 'A video to copy the style of. I break it down: every cut, pace, music, captions, colour.', accept: 'video' },
  song: { title: 'Song', icon: 'music', desc: 'Music for your edits. I find its tempo and beats so cuts can land on them.', accept: 'audio' },
  image: { title: 'Image', icon: 'image', desc: 'A still for overlays or a look to match. Saved with the project.', accept: 'image' },
};
const EXT = {
  video: ['.mp4', '.mov', '.m4v', '.mkv', '.mxf', '.avi', '.mts', '.m2ts', '.insv', '.lrv', '.webm'],
  audio: ['.mp3', '.m4a', '.wav', '.aac', '.flac', '.ogg'],
  image: ['.jpg', '.jpeg', '.png', '.webp', '.gif'],
};
function typeOf(f) {
  const n = String(f.name || '').toLowerCase();
  const ext = n.includes('.') ? n.slice(n.lastIndexOf('.')) : '';
  for (const [k, list] of Object.entries(EXT)) if (list.includes(ext)) return k;
  const m = String(f.type || '').split('/')[0];
  return ['video', 'audio', 'image'].includes(m) ? m : 'other';
}
const fmtSize = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`);
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

// ---------------------------------------------------------------- queue (uploads + the jobs they start)
const Q = [];   // {id, kind, name, size, progress, status: uploading|processing|done|error, job, error, t}
let qBtn, qPop;
function syncPseudoJob(it) {
  const st = it.status === 'uploading' ? 'running' : it.status === 'error' ? 'error' : 'done';
  S.jobs.set('up-' + it.id, { id: 'up-' + it.id, kind: 'upload', label: `${KIND[it.kind] ? KIND[it.kind].title.toLowerCase() : it.kind} · ${it.name}`,
    status: st, progress: it.status === 'uploading' ? it.progress : 1, started: it.t / 1000, error: it.error || null, log_tail: it.error || '' });
}
function addItem(kind, name, size) {
  const it = { id: Math.random().toString(16).slice(2, 10), kind, name, size, progress: 0, status: 'uploading', job: null, error: null, t: Date.now() };
  Q.unshift(it);
  if (Q.length > 30) Q.length = 30;
  syncPseudoJob(it); emit('jobs'); renderQueue();
  return it;
}
function updItem(it, fields) {
  Object.assign(it, fields);
  if (it.status === 'uploading' || fields.status) { syncPseudoJob(it); emit('jobs'); }
  renderQueue();
}
function itemText(it) {
  if (it.status === 'error') return it.error || 'failed';
  if (it.status === 'uploading') return `uploading ${Math.round(it.progress * 100)}%`;
  if (it.status === 'done') return it.doneText || 'done';
  const j = it.job && S.jobs.get(it.job);
  return j ? `${j.stage && j.stage !== 'running' ? j.stage : 'working'}${j.status === 'running' ? ` · ${Math.round((+j.progress || 0) * 100)}%` : j.status === 'queued' ? ' · waiting its turn' : ''}` : 'starting…';
}
function itemProgress(it) {
  if (it.status === 'uploading') return it.progress;
  if (it.status === 'done') return 1;
  const j = it.job && S.jobs.get(it.job);
  return j ? +j.progress || 0 : 0;
}
function renderQueue() {
  if (!qBtn) return;
  const active = Q.filter((x) => x.status === 'uploading' || x.status === 'processing');
  qBtn.hidden = !Q.length;
  qBtn.classList.toggle('busy', !!active.length);
  qBtn.querySelector('.n').textContent = active.length ? String(active.length) : '✓';
  qBtn.title = active.length ? `${plural(active.length, 'import')} in progress` : 'Imports';
  if (qPop && !qPop.hidden) fillQueuePop();
}
function fillQueuePop() {
  qPop.innerHTML = '';
  qPop.appendChild(h('div.mh', 'Imports', h('span.grow'), h('button.btn.ghost', { on: { click: () => { for (let i = Q.length - 1; i >= 0; i--) if (['done', 'error'].includes(Q[i].status)) Q.splice(i, 1); renderQueue(); if (!Q.length) qPop.hidden = true; } } }, 'Clear finished')));
  for (const it of Q) {
    const st = it.status;
    qPop.appendChild(h('div.imp-row.' + st,
      ic(KIND[it.kind] ? KIND[it.kind].icon : 'import', 14),
      h('div.imp-main', h('div.imp-name', { title: it.name }, it.name), h('div.bar.' + (st === 'error' ? 'bad' : st === 'done' ? 'ok' : ''), h('i', { style: { width: (itemProgress(it) * 100).toFixed(1) + '%' } })),
        h('div.imp-st', itemText(it))),
      it.size ? h('span.imp-sz', fmtSize(it.size)) : null));
  }
}

// ---------------------------------------------------------------- uploads (pieces, resumable, straight to disk)
const PIECE = 32 * 1024 * 1024;
function xhrPost(url, blob, onProg) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open('POST', url);
    x.upload.onprogress = (e) => onProg && onProg(e.loaded);
    x.onload = () => {
      let d = null; try { d = JSON.parse(x.responseText); } catch { /* not json */ }
      if (x.status >= 200 && x.status < 300) resolve(d);
      else { const e = new Error((d && d.error) || `${x.status} ${x.statusText}`); e.status = x.status; e.body = d; reject(e); }
    };
    x.onerror = () => { const e = new Error("Can't reach the studio server"); e.status = 0; reject(e); };
    x.send(blob);
  });
}
/** Upload one File in pieces; resumes after a dropped connection. Resolves to the final upload record. */
export async function uploadFile(file, kind, onProgress) {
  const id = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('');
  const total = file.size;
  let off = 0, fails = 0;
  for (;;) {
    const end = Math.min(total, off + PIECE);
    const final = end >= total;
    const url = `/api/upload?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(file.name)}&id=${id}&offset=${off}&total=${total}&final=${final ? 1 : 0}`;
    try {
      const r = await xhrPost(url, file.slice(off, end), (n) => onProgress && onProgress(total ? (off + n) / total : 1));
      if (final) return r;
      off = end; fails = 0;
    } catch (e) {
      if (e.status === 409 && e.body && typeof e.body.received === 'number') { off = e.body.received; continue; }
      if (e.status && e.status !== 0 && e.status < 500) throw e;
      if (++fails > 4) throw e;
      await new Promise((r) => setTimeout(r, 800 * fails));
      try { const s = await api.json(`/api/upload/${id}?kind=${encodeURIComponent(kind)}`); off = s.received || 0; } catch { /* retry from where we were */ }
    }
  }
}

async function importOne(file, kind) {
  const it = addItem(kind, file.name, file.size);
  try {
    const up = await uploadFile(file, kind, (p) => { it.progress = p; if (performance.now() - (it._p || 0) > 120) { it._p = performance.now(); updItem(it, {}); } });
    if (kind === 'reference') {
      const r = await api.addRef({ src: up.path, title: file.name.replace(/\.[^.]+$/, '') });
      updItem(it, { status: 'processing', job: r.job });
      emit('refAdded', { job: r.job, title: file.name });
    } else if (kind === 'song') {
      updItem(it, { status: up.job ? 'processing' : 'done', job: up.job || null, doneText: 'added to Songs' });
    } else if (kind === 'image') {
      updItem(it, { status: 'done', doneText: 'saved', url: up.url });
      emit('imageImported', up);
      toast(`Image saved: ${up.name}`, 'ok', 2500);
    } else {
      updItem(it, { status: up.job ? 'processing' : 'done', job: up.job || null, doneText: 'in your library' });
    }
    return up;
  } catch (e) {
    updItem(it, { status: 'error', error: e.message });
    toast(`${file.name}: ${e.message}`, 'error', 6000);
    return null;
  }
}
/** Route dropped/picked files to a target. */
export async function importFiles(files, kind) {
  files = [...files];
  if (!files.length) return;
  const want = KIND[kind] && KIND[kind].accept;
  const ok = files.filter((f) => typeOf(f) === want);
  const bad = files.length - ok.length;
  if (bad) toast(`${plural(bad, 'file')} skipped: not ${want === 'video' ? 'a video' : want === 'audio' ? 'audio' : 'an image'}`, 'warn', 4000);
  if (!ok.length) return;
  if (kind === 'footage') return footageDialog(ok);
  if (kind === 'reference') emit('showReference');
  showQueue(true);
  for (const f of ok) await importOne(f, kind);
}

// ---------------------------------------------------------------- modal shell
let modal = null;
function closeModal() { if (modal) { modal.remove(); modal = null; } }
function openModal(cls, ...kids) {
  closeModal();
  const card = h('div.imp-card.' + cls, { on: { click: (e) => e.stopPropagation() } }, ...kids);
  modal = h('div.overlay.imp-ov', { on: { click: closeModal } }, card);
  document.body.appendChild(modal);
  return card;
}
const cardHead = (title, sub, help) => h('div.imp-hd', h('div', h('h3', title, help || null), sub ? h('p', sub) : null), h('button.ib', { title: 'Close', html: icon('x'), on: { click: closeModal } }));

// ---------------------------------------------------------------- footage: find, link, index
async function footageDialog(files) {
  const card = openModal('foot', cardHead('Add footage to your library', `${plural(files.length, 'clip')} · ${fmtSize(files.reduce((a, f) => a + f.size, 0))}`,
    explain('Your clips stay exactly where they are. Cutroom remembers their folder, makes small previews and finds the shots — nothing is copied or changed.')));
  const list = h('div.foot-list', h('div.imp-wait', h('span.spin'), 'Looking for these files on this computer…'));
  const foot = h('div.imp-ft');
  card.append(list, foot);
  let found = [];
  const look = async (folder) => {
    try {
      const r = await api.locate(files.map((f) => ({ name: f.name, size: f.size })), folder);
      found = r.files || [];
    } catch (e) { found = files.map((f) => ({ name: f.name, size: f.size, path: null })); toast(`Couldn't look for the files: ${e.message}`, 'error'); }
    draw();
  };
  const draw = () => {
    if (!modal || !card.isConnected) return;
    list.innerHTML = ''; foot.innerHTML = '';
    const missing = found.filter((f) => !f.path);
    const toAdd = found.filter((f) => f.path && !f.in_library);
    const already = found.filter((f) => f.in_library);
    for (const f of found) {
      const where = f.in_library ? 'already in your library' : f.path ? 'found in ' + f.path.split('/').slice(-3, -1).join(' / ') : 'not found on this computer';
      list.appendChild(h('div.foot-row.' + (f.in_library ? 'have' : f.path ? 'ok' : 'miss'), h('span.dot'), h('span.nm', f.name), h('span.wh', where), h('span.sz', fmtSize(f.size))));
    }
    if (missing.length) {
      const inp = h('input.inp', { placeholder: 'Folder they are in, e.g. /Volumes/Footage/Trip', on: { keydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') again.click(); } } });
      const again = h('button.btn', { on: { click: () => { list.prepend(h('div.imp-wait', h('span.spin'), 'Looking…')); look(inp.value.trim() || undefined); } } }, 'Look again');
      list.appendChild(h('div.foot-miss', h('p', `${plural(missing.length, 'file')} couldn't be found (the browser doesn't say where a dropped file lives). Paste the folder they're in:`),
        h('div.irow', inp, again),
        h('p.alt', 'Or, if this browser is on another computer: ', h('button.linkish', { on: { click: () => { const fs = files.filter((f) => missing.some((m) => m.name === f.name)); closeModal(); showQueue(true); fs.forEach((f) => importOne(f, 'footage-link')); } } }, `upload copies instead (${fmtSize(files.filter((f) => missing.some((m) => m.name === f.name)).reduce((a, f) => a + f.size, 0))})`))));
    }
    const go = h('button.btn.primary', { disabled: !toAdd.length, on: { click: async () => {
      go.disabled = true;
      try {
        const r = await api.importFootage(toAdd.map((f) => f.path));
        closeModal(); showQueue(true);
        const it = addItem('footage', `${plural(r.files, 'clip')}`, toAdd.reduce((a, f) => a + (f.size || 0), 0));
        updItem(it, { status: 'processing', job: r.job, doneText: 'in your library' });
        toast(`Adding ${plural(r.files, 'clip')} — previews and shots are being made in the background.`, 'ok', 3500);
      } catch (e) { go.disabled = false; toast(`Couldn't add: ${e.message}`, 'error', 6000); }
    } } }, toAdd.length ? `Add ${plural(toAdd.length, 'clip')}` : already.length ? 'Already in your library' : 'Add');
    foot.append(h('span.note', 'Nothing is copied. Big folders index in the background.'), h('span.grow'), h('button.btn.ghost', { on: { click: closeModal } }, 'Cancel'), go);
  };
  look();
}

// ---------------------------------------------------------------- add reference dialog (link or file)
let info = null;
async function importInfo() { if (!info) { try { info = await api.importInfo(); } catch { info = {}; } } return info; }
export async function openAddReference(prefill = '') {
  const inp = h('input.inp.ref-link', { placeholder: 'Paste a link: YouTube, Instagram, TikTok, Vimeo…', value: prefill, on: { keydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') add.click(); } } });
  const title = h('input.inp', { placeholder: 'Name (optional)', on: { keydown: (e) => e.stopPropagation() } });
  const warn = h('div.ytdlp', { hidden: true });
  const add = h('button.btn.primary', { on: { click: async () => {
    const url = inp.value.trim();
    if (!/^https?:\/\//i.test(url)) { inp.focus(); toast('Paste a full link (starting with https://), or choose a file below.', 'warn'); return; }
    add.disabled = true;
    try {
      const r = await api.addRef({ src: url, title: title.value.trim() || undefined });
      closeModal(); showQueue(true);
      const it = addItem('reference', title.value.trim() || url.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60), 0);
      updItem(it, { status: 'processing', job: r.job, doneText: 'measured' });
      emit('showReference'); emit('refAdded', { job: r.job });
    } catch (e) {
      add.disabled = false;
      if (e.body && e.body.install) { showYtdlp(warn, e.body.install); } else toast(`Couldn't add that link: ${e.message}`, 'error', 6000);
    }
  } } }, 'Add');
  const pick = h('input', { type: 'file', accept: 'video/*,' + EXT.video.join(','), hidden: true, on: { change: () => { const f = [...pick.files]; closeModal(); importFiles(f, 'reference'); } } });
  const zone = h('div.ref-zone', { on: { click: () => pick.click(), dragover: (e) => { e.preventDefault(); e.stopPropagation(); zone.classList.add('hot'); }, dragleave: () => zone.classList.remove('hot'), drop: (e) => { e.preventDefault(); e.stopPropagation(); const f = [...e.dataTransfer.files]; closeModal(); hideDrop(); importFiles(f, 'reference'); } } },
    ic('ref', 18), h('b', 'Choose a video file'), h('span', 'or drop it here'));
  const card = openModal('addref', cardHead('Add a reference video', 'Give me a video whose style you want. I\'ll break it down — every cut, the pace, the music, captions and colour — so we can match it.'),
    h('div.ref-form', h('div.irow', inp, add), title, warn), h('div.or', h('span', 'or')), zone, pick);
  void card;
  setTimeout(() => inp.focus(), 30);
  const i = await importInfo();
  if (i && i.ytdlp === false) showYtdlp(warn, i.ytdlp_install);
}
function showYtdlp(el, cmd) {
  const first = String(cmd || 'brew install yt-dlp').split('    ')[0].trim();
  el.hidden = false; el.innerHTML = '';
  el.append(h('div', h('b', 'Links need one free helper (yt-dlp).'), ' Install it once in Terminal, then add the link again:'),
    h('div.cmd', h('code', first), h('button.btn.ghost', { on: { click: () => { navigator.clipboard && navigator.clipboard.writeText(first).then(() => toast('Copied', 'ok', 1200)).catch(() => {}); } } }, 'Copy')),
    String(cmd || '').includes('pip') ? h('div.sub', 'No Homebrew? pip install yt-dlp works too. Or download the video yourself and drop the file here.') : null);
}

// ---------------------------------------------------------------- drop overlay
let dropEl, dropDepth = 0, staged = null, dragTypes = [], dragCount = 0;
const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
function kindsFromItems(items) {
  const set = new Set();
  for (const it of items || []) {
    if (it.kind && it.kind !== 'file') continue;
    const m = String(it.type || '').split('/')[0];
    set.add(['video', 'audio', 'image'].includes(m) ? m : 'other');
  }
  return set;
}
function buildDrop() {
  dropEl = h('div#drop-ov.drop-ov', { hidden: true });
  document.body.appendChild(dropEl);
  dropEl.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; const t = e.target.closest('.drop-t'); dropEl.querySelectorAll('.drop-t').forEach((x) => x.classList.toggle('hot', x === t && !x.classList.contains('off'))); });
  dropEl.addEventListener('drop', (e) => {
    e.preventDefault();
    dropDepth = 0;
    const files = [...(e.dataTransfer.files || [])];
    const t = e.target.closest('.drop-t');
    if (t && !t.classList.contains('off')) { hideDrop(); importFiles(files, t.dataset.kind); return; }
    stage(files);
  });
  dropEl.addEventListener('click', (e) => { if (e.target === dropEl || e.target.closest('.drop-x')) hideDrop(); });
}
function drawDrop(mode, types, files) {
  dropEl.innerHTML = '';
  const n = files ? files.length : null;
  const nd = dragCount;
  const what = types.size === 1 && !types.has('other') ? [...types][0] : 'file';
  const sub = files ? `${plural(n, 'file')} · ${fmtSize(files.reduce((a, f) => a + f.size, 0))} — where should ${n === 1 ? 'it' : 'they'} go?`
    : `${nd ? plural(nd, what === 'audio' ? 'song' : what) : 'Files'} — drop ${nd === 1 ? 'it' : 'them'} on what ${nd === 1 ? 'it is' : 'they are'}`;
  const grid = h('div.drop-grid');
  for (const [k, d] of Object.entries(KIND)) {
    const off = types.size && !types.has(d.accept) && !(types.size === 1 && types.has('other'));
    const el = h('div.drop-t' + (off ? '.off' : ''), { dataset: { kind: k }, tabindex: off ? -1 : 0, on: { click: () => { if (off || !staged) return; const f = staged; hideDrop(); importFiles(f, k); } } },
      h('span.drop-ic', { html: XI[d.icon] ? xicon(d.icon, 22) : icon(d.icon, 22) }), h('b', d.title), h('span', d.desc));
    grid.appendChild(el);
  }
  dropEl.append(h('div.drop-card', { on: { click: (e) => e.stopPropagation() } },
    h('div.drop-hd', h('h3', mode === 'choose' ? 'Add these files' : 'Drop to add', explain('Footage joins your library where it already is (nothing copied). A reference video gets broken down so we can match its style. Songs get their beats found. Images are kept for overlays and looks.')),
      h('span.grow'), h('button.ib.drop-x', { title: 'Cancel (Esc)', html: icon('x') })),
    h('p.drop-sub', sub), grid,
    h('p.drop-tip', 'Tip: paste a YouTube, Instagram or TikTok link anywhere to add it as a reference.')));
}
function showDrop(types) { dragTypes = types; staged = null; drawDrop('drag', types, null); dropEl.hidden = false; dropEl.classList.remove('choose'); }
export function hideDrop() { if (dropEl) { dropEl.hidden = true; staged = null; dropDepth = 0; } }
function stage(files) {
  if (!files.length) { hideDrop(); return; }
  const types = new Set(files.map(typeOf));
  const kinds = Object.keys(KIND).filter((k) => types.has(KIND[k].accept));
  if (kinds.length === 1 && types.size === 1 && !types.has('video')) { hideDrop(); importFiles(files, kinds[0]); return; }   // a song or an image: obvious
  staged = files;
  drawDrop('choose', types, files);
  dropEl.hidden = false; dropEl.classList.add('choose');
}
function initDrop() {
  buildDrop();
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dropDepth++;
    if (dropEl.hidden || dropEl.classList.contains('choose')) { dragCount = [...(e.dataTransfer.items || [])].filter((i) => i.kind === 'file').length; showDrop(kindsFromItems(e.dataTransfer.items)); }
  });
  window.addEventListener('dragleave', (e) => {
    if (!hasFiles(e) || dropEl.classList.contains('choose')) return;
    dropDepth = Math.max(0, dropDepth - 1);
    if (dropDepth === 0 || (e.clientX <= 0 && e.clientY <= 0)) hideDrop();
  });
  window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => { if (hasFiles(e)) { e.preventDefault(); if (!dropEl.contains(e.target)) { dropDepth = 0; stage([...e.dataTransfer.files]); } } });
}

// ---------------------------------------------------------------- paste: a link = a reference; files = choose
function initPaste() {
  document.addEventListener('paste', (e) => {
    if (isTyping(e)) return;
    const cd = e.clipboardData;
    if (!cd) return;
    const files = [...(cd.files || [])];
    if (files.length) { e.preventDefault(); stage(files); return; }
    const t = (cd.getData('text') || '').trim();
    if (/^https?:\/\/\S+$/i.test(t)) { e.preventDefault(); openAddReference(t); }
  });
}

// ---------------------------------------------------------------- header: Import menu + queue
function pickFiles(kind, multiple = true) {
  const acc = KIND[kind].accept;
  const inp = h('input', { type: 'file', multiple, accept: `${acc}/*,${EXT[acc].join(',')}`, hidden: true });
  inp.addEventListener('change', () => { importFiles([...inp.files], kind); inp.remove(); });
  document.body.appendChild(inp);
  inp.click();
}
export function showQueue(onOff) {
  if (!qPop) return;
  qPop.hidden = onOff === undefined ? !qPop.hidden : !onOff;
  if (!qPop.hidden) fillQueuePop();
}
function initHeader() {
  const hr = document.querySelector('.hdr-r');
  if (!hr) return;
  const menu = h('div.menu.right.imp-menu', { hidden: true, on: { click: (e) => e.stopPropagation() } });
  const item = (k, label, sub, fn) => h('button', { on: { click: () => { menu.hidden = true; fn(); } } }, ic(k, 15), h('span.mi-t', label, h('span.mi-s', sub)));
  menu.append(
    h('div.mh', 'Import'),
    item('footage', 'Footage…', 'Clips stay where they are', () => pickFiles('footage')),
    item('ref', 'Reference video…', 'A file to match the style of', () => pickFiles('reference', false)),
    item('link', 'Reference from a link…', 'YouTube, Instagram, TikTok…', () => openAddReference()),
    item('music', 'Song…', 'Tempo and beats are found', () => pickFiles('song')),
    item('image', 'Image…', 'For overlays and looks', () => pickFiles('image')),
    h('hr'),
    h('div.mi-note', 'Or drop files anywhere in the window.'));
  const btn = h('button#btn-import.tb-btn', { title: 'Add footage, a reference video, songs or images', on: { click: (e) => { e.stopPropagation(); document.querySelectorAll('.menu').forEach((m) => { if (m !== menu) m.hidden = true; }); menu.hidden = !menu.hidden; } } }, ic('import', 14), 'Import', h('span', { html: icon('chev', 12) }));
  qBtn = h('button#imp-q.imp-q', { hidden: true, title: 'Imports', on: { click: (e) => { e.stopPropagation(); showQueue(); } } }, h('span.ring'), h('span.n', ''));
  qPop = h('div.menu.right.imp-pop', { hidden: true, on: { click: (e) => e.stopPropagation() } });
  const wrap = h('div.imp-wrap', btn, menu, qBtn, qPop);
  hr.prepend(wrap);
  document.addEventListener('click', () => { menu.hidden = true; qPop.hidden = true; closePop(); });
}

export function initImporter() {
  initHeader();
  initDrop();
  initPaste();
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { hideDrop(); closeModal(); closePop(); } });
  window.addEventListener('resize', closePop);
  on('jobs', () => {
    let changed = false;
    for (const it of Q) {
      if (it.status !== 'processing' || !it.job) continue;
      const j = S.jobs.get(it.job);
      if (!j) continue;
      if (j.status === 'done') {
        it.status = 'done'; changed = true;
        if (it.kind === 'song') emit('projectRefresh');
        if (it.kind === 'footage' || it.kind === 'footage-link') { emit('projectRefresh'); emit('libraryChanged'); }
      } else if (j.status === 'error' || j.status === 'stopped') { it.status = 'error'; it.error = (j.error || j.status).split('\n').pop(); changed = true; }
      if (changed) syncPseudoJob(it);
    }
    renderQueue();
  });
  on('openAddReference', (u) => openAddReference(u || ''));
  on('importFiles', ({ files, kind }) => importFiles(files, kind));
  on('importPick', (kind) => pickFiles(kind, kind !== 'reference'));
}
export const _test = { Q, stage, showDrop, hideDrop, typeOf };
