// Program monitor: draft playback of the resolved timeline from proxies (two <video>s swapped at
// cuts, captions/overlays drawn as HTML, section music synced) and render playback. The Reference tab
// opens the Reference workspace (reference.js), which borrows this monitor for side by side / onion skin.
import { S, on, emit, setPlayhead, pictureAt, pieceAt, sectionAt, segmentOf, fps } from './state.js';
import { api, media } from './api.js';
import { h, icon, tc, clamp, toast, LS } from './util.js';

let view, frame, vids, rv, music, card, gfx, safe, tcEl, durEl, playBtn, modeSeg, renderSel, emptyEl;
let active = 0;
const clock = { playing: false, rate: 1, wall0: 0, t0: 0 };

// ---------------------------------------------------------------- video helpers
function want(v, clip, t) {
  if (!clip) return;
  if (v._clip !== clip) {
    v._clip = clip; v._ready = false; v._pending = t;
    v.src = media.proxy(clip);
    v.onloadedmetadata = () => { v._ready = true; if (v._pending !== null) { safeSeek(v, v._pending); v._pending = null; } };
    return;
  }
  if (!v._ready) { v._pending = t; return; }
  safeSeek(v, t);
}
function safeSeek(v, t) {
  if (v.seeking) { v._pending = t; return; }
  if (Math.abs(v.currentTime - t) < 0.5 / fps()) return;
  try { v.currentTime = Math.max(0, t); } catch { /* not seekable yet */ }
}
function srcTime(p, T) { return p.in + (T - p.start) * (p.speed || 1); }

// ---------------------------------------------------------------- draft rendering
let lastPic = null;
function showDraftFrame(T, playing) {
  const p = pictureAt(T);
  const v1 = pieceAt(T, 'V1');
  const A = vids[active], B = vids[1 - active];
  if (!p) {
    A.classList.add('hidden'); B.classList.add('hidden'); A.pause(); B.pause();
    card.hidden = false; card.innerHTML = '';
    lastPic = null;
    drawGfx(T);
    return;
  }
  if (p.clip && p.in !== null) {
    card.hidden = true;
    const st = srcTime(p, T);
    let cur = A;
    if (A._clip !== p.clip || Math.abs(A.currentTime - st) > 0.35) {
      // can the standby take over cleanly (preloaded for this cut)?
      if (B._clip === p.clip && B._ready && Math.abs(B.currentTime - st) < 0.35) { active = 1 - active; cur = B; }
      else want(A, p.clip, st);
    }
    const other = vids[1 - active];
    cur.classList.remove('hidden'); other.classList.add('hidden');
    const kindAudio = p.track === 'V1' ? (p.kind === 'talk' || p.audio === 'natural' || p.audio === 'mix') : (v1 && v1.kind === 'talk' ? false : false);
    cur.muted = !kindAudio && !(p.track === 'V2' && false);
    other.muted = true;
    if (playing) {
      cur.playbackRate = clamp(clock.rate * (p.speed || 1), 0.25, 8);
      if (cur.paused && cur._ready) cur.play().catch(() => {});
      if (!other.paused) other.pause();
      if (cur._ready && Math.abs(cur.currentTime - st) > 0.3) safeSeek(cur, st);
      // dialogue under a cutaway: keep the talk audio by playing it from the hidden video
      if (p.track === 'V2' && v1 && v1.kind === 'talk' && v1.clip) { /* approximate: talk audio not played under cutaways */ }
      // preload the next picture into the standby video
      const next = pictureAt(p.end + 0.0005);
      if (next && next !== p && next.clip && next.in !== null && p.end - T < 1.5) {
        if (other._clip !== next.clip || Math.abs(other.currentTime - next.in) > 0.1) want(other, next.clip, next.in);
      }
    } else {
      if (!cur.paused) cur.pause();
      if (!other.paused) other.pause();
      want(cur, p.clip, st);
    }
  } else {
    // title / black / still without a source frame
    vids.forEach((v) => { v.pause(); v.classList.add('hidden'); });
    card.hidden = false;
    if (lastPic !== p.id) {
      const seg = segmentOf(p) || {};
      const H = frame.clientHeight || 300;
      card.innerHTML = '';
      if (p.kind === 'title') card.append(h('div.t', { style: { fontSize: (H * 0.085) + 'px' } }, seg.text || p.title || p.label || ''), seg.sub ? h('div.s', { style: { fontSize: (H * 0.04) + 'px' } }, seg.sub) : null);
    }
  }
  if (p.kind === 'still' && p.clip) {
    // still = freeze: hold the frame (video paused at t)
    const cur = vids[active];
    cur.pause(); want(cur, p.clip, p.in ?? 0); cur.classList.remove('hidden'); card.hidden = true;
  }
  lastPic = p.id;
  drawGfx(T);
  syncMusic(T, playing, p);
}

let curSong = null;
function syncMusic(T, playing, p) {
  const sec = sectionAt(T);
  const m = sec && S.tl.music.find((x) => x.sec === sec.index);
  if (!m || !playing) { if (!music.paused) music.pause(); return; }
  const target = m.offset + (T - m.start);
  if (curSong !== m.song) { curSong = m.song; music.src = media.song(m.song); music._ready = false; music.onloadedmetadata = () => { music._ready = true; music.currentTime = m.offset + (S.playhead - m.start); }; }
  if (music._ready && Math.abs(music.currentTime - target) > 0.3) music.currentTime = target;
  const mode = (S.plan && S.plan.draft && S.plan.draft.style && S.plan.draft.style.music_under_speech) || 'mute';
  const talk = p && p.kind === 'talk';
  music.volume = talk ? (mode === 'full' ? 0.7 : mode === 'duck' ? 0.18 : 0.06) : 0.7;
  music.playbackRate = clamp(clock.rate, 0.5, 4);
  if (music.paused && music._ready) music.play().catch(() => {});
}

let gfxKey = '';
function drawGfx(T) {
  if (!S.tl) { gfx.innerHTML = ''; gfxKey = ''; return; }
  const caps = S.tl.captions.filter((c) => T >= c.t0 && T < c.t1);
  const ovs = S.tl.overlays.filter((o) => T >= o.t0 && T < o.t1);
  const key = caps.map((c) => c.t0 + c.text).join('|') + '#' + ovs.map((o) => o.t0 + o.text).join('|') + '#' + frame.clientHeight;
  if (key === gfxKey) return;
  gfxKey = key;
  gfx.innerHTML = '';
  const st = (S.plan && S.plan.draft && S.plan.draft.style) || {};
  const capSt = st.captions || {};
  const outH = (S.plan && S.plan.draft && S.plan.draft.output && S.plan.draft.output.height) || 1080;
  const H = frame.clientHeight;
  const scale = H / outH;
  if (capSt.enabled !== false) {
    for (const c of caps) {
      const pos = c.position || capSt.position || 'bottom';
      const el = h('div.cap', { style: { fontSize: Math.max(9, (capSt.size || 54) * scale) + 'px' } }, applyCase(c.text, capSt.case));
      if (pos === 'top') el.style.top = '8%'; else if (pos === 'lower') el.style.bottom = '22%'; else el.style.bottom = '9%';
      gfx.appendChild(el);
    }
  }
  for (const o of ovs) {
    const el = h('div.ovl.' + (o.kind || 'callout'), { style: { fontSize: Math.max(9, 44 * scale) + 'px' } }, o.text, o.sub ? h('span.sub', o.sub) : null);
    const pos = o.position && o.position !== 'auto' ? o.position : (o.kind === 'lower_third' ? 'bottom-left' : 'top-left');
    const m = '6%';
    if (pos === 'center') { el.style.left = '50%'; el.style.top = '50%'; el.style.transform = 'translate(-50%,-50%)'; }
    else {
      if (pos.includes('top')) el.style.top = m; else el.style.bottom = o.kind === 'lower_third' ? '16%' : m;
      if (pos.includes('left')) el.style.left = m; else el.style.right = m;
    }
    gfx.appendChild(el);
  }
}
function applyCase(t, c) { return c === 'upper' ? String(t).toUpperCase() : c === 'lower' ? String(t).toLowerCase() : t; }

// ---------------------------------------------------------------- clock
function tick() {
  if (!clock.playing) return;
  const dur = S.programMode === 'render' ? (rv.duration || 0) : (S.tl ? S.tl.duration : 0);
  if (S.programMode === 'render') {
    setPlayhead(rv.currentTime, 'program');
    if (rv.ended) pause();
  } else {
    let T = clock.t0 + ((performance.now() - clock.wall0) / 1000) * clock.rate;
    if (T >= dur) { T = dur; setPlayhead(T, 'program'); pause(); return; }
    if (T <= 0) { T = 0; setPlayhead(0, 'program'); pause(); return; }
    setPlayhead(T, 'program');
    showDraftFrame(T, clock.rate > 0);
  }
  requestAnimationFrame(tick);
}
function play(rate = 1) {
  if (S.programMode === 'draft' && (!S.tl || !S.tl.duration)) return;
  if (S.programMode === 'draft' && S.playhead >= S.tl.duration - 0.05 && rate > 0) setPlayhead(0, 'force');
  clock.playing = true; clock.rate = rate; clock.wall0 = performance.now(); clock.t0 = S.playhead;
  S.playing = true;
  if (S.programMode === 'render') { rv.playbackRate = clamp(Math.abs(rate), 0.25, 8); if (rate > 0) rv.play().catch(() => {}); }
  playBtn.innerHTML = icon('pause');
  emit('playing', true);
  requestAnimationFrame(tick);
}
function pause() {
  clock.playing = false; S.playing = false;
  vids.forEach((v) => v.pause()); music.pause(); rv.pause();
  playBtn.innerHTML = icon('play');
  emit('playing', false);
  if (S.programMode === 'draft') showDraftFrame(S.playhead, false);
}

export const programCtl = {
  togglePlay() { clock.playing ? pause() : play(1); },
  shuttle(dir) {
    if (dir === 0) { pause(); return; }
    let r = clock.playing ? clock.rate : 0;
    if (Math.sign(r) !== Math.sign(dir)) r = 0;
    const ladder = [1, 2, 4, 8];
    const idx = r === 0 ? 0 : Math.min(3, ladder.indexOf(Math.abs(r)) + 1);
    const rate = dir * ladder[idx];
    if (S.programMode === 'render' && rate < 0) { toast('Reverse play works in Draft mode', 'warn', 1500); return; }
    if (clock.playing) { clock.t0 = S.playhead; clock.wall0 = performance.now(); clock.rate = rate; if (S.programMode === 'render') rv.playbackRate = Math.abs(rate); }
    else play(rate);
  },
  step(n) { pause(); setPlayhead(S.playhead + n / fps(), 'program'); },
  seekRel(s) { const was = clock.playing; pause(); setPlayhead(S.playhead + s, 'program'); if (was) play(); },
  pause,
  get playing() { return clock.playing; },
};

// ---------------------------------------------------------------- render mode
function rendersForPlan() {
  const all = (S.project && S.project.renders) || [];
  const slug = S.plan && S.plan.slug;
  const mine = slug ? all.filter((r) => r.plan === slug || (r.file || '').startsWith(slug + '_')) : [];
  return (mine.length ? mine : all).slice().sort((a, b) => (b.created || 0) - (a.created || 0));
}
export function playRender(file) {
  S.renderFile = file;
  setMode('render');
  if (file) { rv.src = media.render(file); rv.currentTime = 0; }
}
function setMode(mode) {
  pause();
  S.programMode = mode;
  modeSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  const isR = mode === 'render';
  rv.hidden = !isR;
  vids.forEach((v) => (v.hidden = isR));
  gfx.hidden = isR; card.hidden = true;
  renderSel.hidden = !isR;
  if (isR) {
    const list = rendersForPlan();
    renderSel.innerHTML = '';
    for (const r of list) renderSel.appendChild(h('option', { value: r.file }, r.file));
    if (!list.length) renderSel.appendChild(h('option', { value: '' }, 'No renders yet'));
    if (!S.renderFile && list[0]) S.renderFile = list[0].file;
    if (S.renderFile) { renderSel.value = S.renderFile; if (!rv.src.endsWith(media.render(S.renderFile))) rv.src = media.render(S.renderFile); }
    emptyEl.hidden = !!S.renderFile;
    emptyEl.querySelector('div').textContent = 'No render for this plan yet. Save a version, then Render preview.';
  } else {
    emptyEl.hidden = !!S.tl;
    emptyEl.querySelector('div').textContent = 'Open a plan to play the edit';
    showDraftFrame(S.playhead, false);
  }
  emit('programMode', mode);
  emit('context');
}

// ---------------------------------------------------------------- reference
// The Reference tab opens the Reference workspace (reference.js); R grabs a reference frame to chat.
export function markReference() { emit('markReference'); }

// ---------------------------------------------------------------- frame fitting
function fitFrame(container, el, ar) {
  const r = container.getBoundingClientRect();
  let w = r.width, hh = w / ar;
  if (hh > r.height) { hh = r.height; w = hh * ar; }
  el.style.width = Math.floor(w) + 'px'; el.style.height = Math.floor(hh) + 'px';
}
function outAR() {
  const o = S.plan && S.plan.draft && S.plan.draft.output;
  return o && o.width && o.height ? o.width / o.height : 16 / 9;
}
function fitMain() { fitFrame(view, frame, outAR()); gfxKey = ''; drawGfx(S.playhead); }

// ---------------------------------------------------------------- grab
async function grabProgram() {
  try {
    const T = +S.playhead.toFixed(3);
    const p = pictureAt(T);
    const body = { source: 'program', t: T, plan: S.plan && S.plan.slug };
    if (S.programMode === 'render' && S.renderFile) body.file = S.renderFile;
    if (p && p.clip) { body.clip = p.clip; body.clip_t = +srcTime(p, T).toFixed(3); }
    const r = await api.grab(body);
    emit('attach', { url: r.url, label: `program @ ${tc(T, fps())}`, source: 'program', t: T });
  } catch (e) { toast(`Grab failed: ${e.message}`, 'error'); }
}

// ---------------------------------------------------------------- init
let split;
export function initProgram(body, tools) {
  vids = [h('video', { preload: 'auto', playsInline: true }), h('video.hidden', { preload: 'auto', playsInline: true, muted: true })];
  vids.forEach((v) => { v._clip = null; v._ready = false; v._pending = null; v.addEventListener('seeked', () => { if (v._pending !== null) { const t = v._pending; v._pending = null; safeSeek(v, t); } }); });
  rv = h('video', { preload: 'auto', playsInline: true, hidden: true });
  music = h('audio', { preload: 'auto' });
  card = h('div.title-card', { hidden: true });
  gfx = h('div.gfx');
  safe = h('div.safe', { hidden: !S.safeArea });
  frame = h('div.mon-frame', vids[0], vids[1], rv, card, gfx, safe);
  emptyEl = h('div.mon-empty', h('span', { html: icon('render', 22) }), h('div', 'Open a plan to play the edit'));
  view = h('div.mon-view', frame, emptyEl);
  split = h('div.mon-split', view);

  tcEl = h('span.tc', '00:00:00:00');
  durEl = h('span.tc.dim', '00:00:00:00');
  playBtn = h('button.ib', { title: 'Play / pause (Space)', html: icon('play'), on: { click: () => programCtl.togglePlay() } });
  modeSeg = h('div.mode-seg',
    h('button.on', { dataset: { mode: 'draft' }, title: 'Play the timeline from proxies (instant, approximate)', on: { click: () => setMode('draft') } }, 'Draft'),
    h('button', { dataset: { mode: 'render' }, title: 'Play the rendered file', on: { click: () => setMode('render') } }, 'Render'));
  renderSel = h('select.inp.render-sel', { hidden: true });
  renderSel.addEventListener('change', () => playRender(renderSel.value));
  const safeBtn = h('button.ib', { title: 'Safe-area guides', html: icon('safe'), on: { click: () => { S.safeArea = !S.safeArea; LS.set('safeArea', S.safeArea); safe.hidden = !S.safeArea; safeBtn.classList.toggle('on', S.safeArea); } } });
  safeBtn.classList.toggle('on', S.safeArea);
  const bar = h('div.mon-bar',
    tcEl,
    h('button.ib.nw', { title: 'Back one frame (←)', html: icon('stepb'), on: { click: () => programCtl.step(-1) } }),
    playBtn,
    h('button.ib.nw', { title: 'Forward one frame (→)', html: icon('stepf'), on: { click: () => programCtl.step(1) } }),
    h('span.sep'),
    modeSeg, renderSel,
    h('span.grow'),
    safeBtn,
    h('button.ib.nw.cm-btn', { title: 'Add comment at this frame (M)', html: '<svg class="ic" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.5h11v7.5h-6.2L4 13.6V11H2.5z"/><path d="M5.2 6.6h5.6M5.2 8.6h3.6"/></svg>', on: { click: () => emit('addMarker') } }),
    h('button.ib', { title: 'Grab frame to chat', html: icon('grab'), on: { click: grabProgram } }),
    durEl);
  durEl.classList.add('nw');
  durEl.title = 'Timeline duration';
  body.appendChild(h('div.mon', split, bar));
  document.body.appendChild(music);

  document.querySelectorAll('#p-program .ptab[data-ptab]').forEach((b) => b.addEventListener('click', () => {
    emit('toggleReference', b.dataset.ptab === 'reference' ? undefined : false);
  }));
  void tools;

  new ResizeObserver(fitMain).observe(view);
  view.addEventListener('click', () => { emit('focusMonitor', 'program'); });
  view.addEventListener('dblclick', () => programCtl.togglePlay());

  rv.addEventListener('timeupdate', () => { if (S.programMode === 'render' && !clock.playing) setPlayhead(rv.currentTime, 'program'); });
  rv.addEventListener('pause', () => { if (clock.playing && S.programMode === 'render') pause(); });
  vids.forEach((v) => v.addEventListener('error', () => { if (v._clip) console.warn('proxy failed', v._clip); }));

  on('playhead', (from) => {
    tcEl.textContent = tc(S.playhead, fps());
    if (from === 'program') return;
    if (clock.playing) { clock.t0 = S.playhead; clock.wall0 = performance.now(); }
    if (S.programMode === 'render') { if (Math.abs(rv.currentTime - S.playhead) > 0.02) rv.currentTime = S.playhead; }
    else showDraftFrame(S.playhead, clock.playing);
  });
  on('scrubStart', () => { if (clock.playing) { pause(); } });
  on('plan', () => {
    durEl.textContent = S.tl ? tc(S.tl.duration, fps()) : '';
    const noPieces = S.tl && !S.tl.v1.length;
    emptyEl.hidden = S.programMode === 'render' ? !!S.renderFile : (!!S.tl && !noPieces);
    if (S.programMode !== 'render') emptyEl.querySelector('div').textContent = noPieces ? 'Nothing on the timeline yet' : 'Open a plan to play the edit';
    lastPic = null; gfxKey = '';
    fitMain();
    if (S.programMode === 'draft') showDraftFrame(S.playhead, clock.playing);
  });
  on('project', () => { if (S.programMode === 'render') setMode('render'); });
  on('playRender', playRender);
  on('layout', fitMain);
}
