// Claude panel: streaming chat over POST /api/chat (SSE via fetch), clip-aware replies, quiet tool steps,
// voice input, attachments, "pointing at" context chips, quick actions.
import { S, on, emit, buildContext, reloadPlan, pieceAt, selectedPieces, fps, setPlayhead, sectionAt } from './state.js';
import { api, chatStream } from './api.js';
import { h, icon, tc, toast, LS, throttle, parseTc, isTyping } from './util.js';
import { richMd, hydrate, wireChips, stepLabel, stepSummary, stepNow, stripPaths } from './rich.js';

let log, input, sendBtn, stopBtn, micBtn, ctxRow, attRow, quickRow, abort = null, streaming = false;

const QUICK = [
  { label: 'Fix open markers', msg: () => `Fix all open markers (${openMarkers().length}). Change exactly what each note asks, nothing else, then tell me what you changed per marker.`, need: () => openMarkers().length > 0 },
  { label: 'Tighten this', msg: () => 'Tighten this: cut dead air and slow starts in the segment I’m pointing at, keep the meaning and the best lines.', need: () => !!S.tl },
  { label: 'Find a better shot for this', msg: () => 'Find a better shot for this: search the library for a stronger clip for the segment I’m pointing at (steadier, better light, same idea). Show me 2–3 options before swapping.', need: () => !!S.tl },
  { label: 'Make a 2-min edit from…', msg: () => 'Make a 2-minute edit from ', fill: true },
];
export const STARTERS = [
  { label: 'Go through all my footage and tell me what’s there', msg: 'Go through all my footage and tell me what’s there.' },
  { label: 'Make a 2-minute edit of [day/trip]', msg: 'Make a 2-minute edit of ', fill: true },
  { label: 'Find my best moments', msg: 'Find my best moments and put them in a bin.' },
  { label: 'Fix all my markers', msg: 'Fix all my open markers. Change exactly what each note asks, nothing else.' },
];
const openMarkers = () => S.markers.filter((m) => m.status !== 'done');

// ---------------------------------------------------------------- transcript rendering
function scrollDown(force) {
  const near = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  if (force || near) log.scrollTop = log.scrollHeight;
}
function clearEmpty() { const e = log.querySelector('.cl-empty'); if (e) e.remove(); }
export function useStarter(s) {
  if (s.fill) { input.value = s.msg; autosize(); input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  else send(s.msg);
}
function emptyState() {
  log.innerHTML = '';
  log.appendChild(h('div.cl-empty',
    h('h4', 'Claude edits, you steer'),
    h('p', 'Ask for a cut, point at a shot on the timeline and say what to change, or drop a marker (M) and send it here. Claude sees what you’re pointing at, shown above the box.'),
    h('div.ex', STARTERS.map((s) => h('button', { on: { click: () => useStarter(s) } }, s.label)))));
}
function userMsg(text, atts, ctxLine) {
  clearEmpty();
  const el = h('div.msg.user', stripPaths(text),
    atts && atts.length ? h('div.atts', atts.map((a) => h('img', { src: a.url, title: a.label || '' }))) : null,
    ctxLine ? h('span.ctxline', ctxLine) : null);
  log.appendChild(el); scrollDown(true);
}
function asstMsg() {
  clearEmpty();
  const el = h('div.msg.asst');
  const stepsEl = h('div.steps', { hidden: true });
  const textEl = h('div.atext');
  el.append(stepsEl, textEl);
  log.appendChild(el);
  return { el, stepsEl, textEl, text: '', steps: [], open: false, live: true };
}

// ---------------------------------------------------------------- quiet steps
function renderSteps(m) {
  const st = m.steps;
  m.stepsEl.hidden = !st.length;
  if (!st.length) return;
  const running = m.live && st.some((s) => s.status === 'run');
  const bad = st.filter((s) => s.status === 'bad').length;
  const line = running ? stepNow(st) : stepSummary(st);
  m.stepsEl.innerHTML = '';
  const head = h('button.steps-line', { title: m.open ? 'Hide details' : 'Show what Claude did', 'aria-expanded': String(m.open) },
    h('span.steps-ic' + (running ? '.run' : bad ? '.bad' : '.ok'), { html: icon(running ? 'spark' : bad ? 'warn' : 'check', 12) }),
    h('span.steps-txt', line),
    h('span.steps-n', `${st.length} step${st.length > 1 ? 's' : ''}`),
    h('span.steps-arrow' + (m.open ? '.open' : ''), { html: icon('chev', 12) }));
  head.onclick = () => { m.open = !m.open; renderSteps(m); };
  m.stepsEl.appendChild(head);
  if (m.open) {
    const raw = h('div.steps-raw');
    for (const s of st) {
      raw.appendChild(h('div.step-row.' + s.status,
        h('span.sr-l', s.label.now.replace(/…$/, '')),
        h('code.sr-i', s.name + (s.inputText ? ': ' + s.inputText : '')),
        s.result ? h('span.sr-r', s.result) : null));
    }
    m.stepsEl.appendChild(raw);
  }
}
function addStep(m, ev) {
  const inputText = typeof ev.input === 'string' ? ev.input : ev.input ? (ev.input.command || ev.input.file_path || ev.input.pattern || JSON.stringify(ev.input)) : (ev.summary || '');
  m.steps.push({ id: ev.id || ev.tool_use_id || `s${m.steps.length}`, name: ev.name || 'tool', inputText: String(inputText).slice(0, 400),
    label: stepLabel(ev.name, ev.input || ev.summary), status: 'run', result: '' });
  renderSteps(m); scrollDown();
}
function stepResult(m, ev) {
  const s = m.steps.find((x) => x.id === (ev.id || ev.tool_use_id)) || [...m.steps].reverse().find((x) => x.status === 'run');
  if (!s) return;
  s.status = ev.ok === false || ev.is_error ? 'bad' : 'ok';
  const t = ev.text || ev.summary || ev.content;
  if (typeof t === 'string') s.result = t.split('\n')[0].slice(0, 160);
  renderSteps(m);
}
const renderText = throttle((m) => { m.textEl.innerHTML = richMd(m.text); hydrate(m.textEl); scrollDown(); }, 60);

// ---------------------------------------------------------------- send
function ctxSummary(c) {
  const parts = [];
  if (c.include.playhead && c.playhead) parts.push(`@ ${c.playhead.tc}${c.playhead.piece ? ' · ' + (c.playhead.piece.label || c.playhead.piece.kind) : ''}`);
  if (c.include.selection && c.selection.length) parts.push(`${c.selection.length} selected`);
  if (c.include.source && c.source) parts.push(`source ${(c.source.rel || '').split('/').pop()}${c.source.in !== null && c.source.in !== undefined ? ` ${c.source.in.toFixed(2)}–${(c.source.out ?? 0).toFixed(2)}` : ''}`);
  if (c.include.markers && c.markers.length) parts.push(`${c.markers.length} markers`);
  return parts.join(' · ');
}
export async function send(text) {
  text = (text ?? input.value).trim();
  if (!text || streaming) return;
  if (rec) stopVoice();
  const full = buildContext();
  const ctx = { ...full };
  if (!full.include.playhead) delete ctx.playhead;
  if (!full.include.selection) ctx.selection = [];
  if (!full.include.source) ctx.source = null;
  if (!full.include.markers) ctx.markers = [];
  const atts = S.attachments.slice();
  userMsg(text, atts, ctxSummary(full));
  input.value = ''; autosize();
  S.attachments = []; renderAtts();
  const m = asstMsg(); m.el.classList.add('live');
  streaming = true; syncBtns();
  abort = new AbortController();
  try {
    await chatStream({ message: text, attachments: atts.map((a) => a.url), attachment_notes: atts.map((a) => ({ url: a.url, label: a.label, match: !!a.match })), context: ctx }, (ev) => {
      switch (ev.type) {
        case 'text': case 'delta': m.text += ev.text ?? ev.delta ?? ''; renderText(m); break;
        case 'tool': case 'tool_use': addStep(m, ev); break;
        case 'tool_result': stepResult(m, ev); break;
        case 'plan_changed': reloadPlan('claude'); break;
        case 'error': {
          const msg = ev.error || ev.message || ev.text || 'error';
          if (ev.stopped || msg === 'stopped') m.text += '\n\n*Stopped.*';
          else if (ev.install) m.text += `\n\n**${ev.agent === 'codex' ? 'Codex' : 'Claude Code'} isn't set up on this computer yet.** ${Array.isArray(ev.install) ? '\n\n' + ev.install.map((s) => '- ' + s).join('\n') : 'Install it, sign in once, then restart the studio.'}`;
          else m.text += `\n\n**Something went wrong:** ${msg.split('\n')[0].slice(0, 300)}`;
          renderText(m); break;
        }
        case 'warning': m.text += `\n\n*${ev.text || ev.message}*\n\n`; renderText(m); break;
        case 'status': addStep(m, { name: 'status', input: ev.text || ev.message || 'Reconnecting…' }); break;
        case 'done': if (ev.text && !m.text) { m.text = ev.text; renderText(m); } break;
        default: break;
      }
    }, abort.signal);
  } catch (e) {
    m.text += e.status === 409 ? '\n\nStill working on the previous message. Press Stop first.' : `\n\nCouldn't reach Claude: ${e.message}`;
    renderText(m);
  } finally {
    streaming = false; abort = null; syncBtns();
    m.live = false;
    m.el.classList.remove('live');
    for (const s of m.steps) if (s.status === 'run') s.status = 'ok';
    renderSteps(m);
    m.textEl.innerHTML = richMd(m.text.replace(/\[\[factory:\w+\]\]/g, '')); hydrate(m.textEl);
    const fk = (m.text.match(/\[\[factory:(\w+)\]\]/) || [])[1];
    if (fk || FACTORY_WORK.test(text)) m.el.appendChild(factoryChip(text, fk));
    scrollDown();
  }
}

// "make an edit", "fix all my notes", "match this reference": real edit work -> offer the factory
const FACTORY_WORK = /\b(make (me )?(an?|the) (\d+[- ]?(min|minute|sec|second)s? )?(edit|cut|version|video)|fix (all|every)( of)? (my |the )?(notes|comments|markers)|match (this|the) reference|re-?edit|go through (all )?(my )?notes|new version)\b/i;
function factoryChip(text, kind) {
  const b = h('button.btn.fx-chip', { title: 'The factory team builds a checked new version (producer, editor, critic, render, QA) and hands you a card' },
    h('span', { html: icon('spark', 12) }), 'Run in factory');
  b.onclick = async () => {
    if (!S.plan) { toast('Open a plan first', 'warn'); return; }
    b.disabled = true;
    try {
      const fixAll = /notes|comments|markers/i.test(text) && !/make|match/i.test(text);
      const r = await api.fx.send(fixAll ? { plan: S.plan.slug, kind: 'revise', all_open: true, via: 'chat' }
        : { plan: S.plan.slug, kind: kind === 'revise' ? 'revise' : 'request', text, via: 'chat' });
      b.textContent = `In the factory (${r.id.slice(-4)})`;
      emit('showDock', 'factory');
    } catch (e) { b.disabled = false; toast(e.message, 'error'); }
  };
  return h('div.fx-chiprow', b);
}
async function stop() {
  if (abort) abort.abort();
  try { await api.chatStop(); } catch { /* best effort */ }
}
async function newChat() {
  if (streaming) await stop();
  try { await api.chatNew(); } catch (e) { toast(`New chat: ${e.message}`, 'error'); }
  emptyState();
}
function syncBtns() {
  sendBtn.disabled = streaming || !input.value.trim();
  stopBtn.disabled = !streaming;
  sendBtn.hidden = streaming; stopBtn.hidden = !streaming;
}
function autosize() { input.style.height = 'auto'; input.style.height = Math.min(180, input.scrollHeight) + 'px'; syncBtns(); }

// ---------------------------------------------------------------- voice input (Web Speech API)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, voiceBase = '', voiceFinal = '';
function startVoice() {
  if (!SR || rec) return;
  try {
    rec = new SR();
    rec.continuous = true; rec.interimResults = true;
    rec.lang = navigator.language || 'en-US';
    voiceBase = input.value ? input.value.replace(/\s*$/, ' ') : '';
    voiceFinal = '';
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) voiceFinal += r[0].transcript; else interim += r[0].transcript;
      }
      input.value = voiceBase + voiceFinal + interim;
      input.classList.toggle('interim', !!interim);
      autosize();
    };
    rec.onerror = (e) => { if (e.error !== 'no-speech' && e.error !== 'aborted') toast(e.error === 'not-allowed' ? 'Microphone access is blocked for this page' : `Voice input: ${e.error}`, 'warn'); };
    rec.onend = () => { rec = null; input.classList.remove('interim'); micBtn.classList.remove('rec'); micBtn.title = 'Talk to Claude (hold ` to talk)'; };
    rec.start();
    micBtn.classList.add('rec'); micBtn.title = 'Listening… click to stop';
    input.focus();
  } catch (e) { rec = null; toast(`Voice input unavailable: ${e.message}`, 'warn'); }
}
function stopVoice() { if (rec) { try { rec.stop(); } catch { /* already stopped */ } } }

// ---------------------------------------------------------------- pointing chips
function chip(key, iconName, content, title) {
  const offd = !S.ctxInclude[key];
  const el = h('button.cchip' + (offd ? '.off' : ''), { title: (offd ? 'Not sent. Click to include: ' : 'Sent with your message. Click to leave out: ') + title },
    h('span', { html: icon(iconName, 12) }), content);
  el.onclick = () => { S.ctxInclude[key] = !S.ctxInclude[key]; LS.set('ctxInclude', S.ctxInclude); renderCtx(); emit('context'); };
  return el;
}
function renderCtxNow() {
  ctxRow.innerHTML = '';
  ctxRow.appendChild(h('span.lbl', 'Pointing at'));
  if (S.tl) {
    const p = pieceAt(S.playhead, 'V2') || pieceAt(S.playhead, 'V1');
    const sec = sectionAt(S.playhead);
    ctxRow.appendChild(chip('playhead', 'marker', h('span', h('span.tc.mono', tc(S.playhead, fps())), p ? ` ${sec ? sec.name + ' / ' : ''}${p.label || p.kind}` : ''), 'the playhead and the shot under it'));
  }
  const sel = selectedPieces();
  if (sel.length) {
    const segs = new Set(sel.map((p) => `${p.sec}:${p.seg}:${p.track}`)).size;
    ctxRow.appendChild(chip('selection', 'check', h('span', `${segs} selected`), 'the selected shots'));
  }
  const s = S.source;
  if (s.clip) {
    const nm = (s.clip.rel || s.clip.id).split('/').pop().replace(/\.[^.]+$/, '');
    const io = s.in !== null || s.out !== null ? ` ${s.in !== null ? s.in.toFixed(1) : '…'}–${s.out !== null ? s.out.toFixed(1) : '…'}s` : '';
    ctxRow.appendChild(chip('source', 'film', h('span', nm + io), 'the Source clip and its In/Out'));
  }
  const om = openMarkers();
  if (om.length) ctxRow.appendChild(chip('markers', 'marker', h('span', `${om.length} open marker${om.length > 1 ? 's' : ''}`), 'open markers'));
  if (ctxRow.children.length === 1) ctxRow.appendChild(h('span.lbl', 'nothing yet'));
}
const renderCtx = throttle(renderCtxNow, 120);
function renderQuick() {
  quickRow.innerHTML = '';
  for (const q of QUICK) {
    const b = h('button.btn', { disabled: q.need ? !q.need() : false }, q.label);
    b.onclick = () => { if (q.fill) { input.value = q.msg(); autosize(); input.focus(); input.setSelectionRange(input.value.length, input.value.length); } else send(q.msg()); };
    quickRow.appendChild(b);
  }
}
function renderAtts() {
  attRow.innerHTML = '';
  attRow.hidden = !S.attachments.length;
  S.attachments.forEach((a, i) => {
    attRow.appendChild(h('div.att', { title: a.label },
      h('img', { src: a.url, alt: '' }),
      h('div.cap2', a.match ? 'match this' : a.label),
      h('button', { title: 'Remove', html: icon('x', 9), on: { click: () => { S.attachments.splice(i, 1); renderAtts(); } } })));
  });
}

async function loadHistory() {
  try {
    const r = await api.chatHistory();
    const msgs = (r && r.messages) || [];
    if (!msgs.length) { emptyState(); return; }
    log.innerHTML = '';
    for (const m of msgs) {
      if (m.role === 'user') userMsg(m.text || '', (m.attachments || []).map((u) => ({ url: typeof u === 'string' ? u : u.url })));
      else {
        const a = asstMsg();
        a.live = false;
        for (const t of m.tools || []) { addStep(a, t); stepResult(a, { ...t, id: a.steps[a.steps.length - 1].id }); }
        a.text = m.text || '';
        if (m.error && m.error !== 'stopped') a.text += `\n\n**Something went wrong:** ${String(m.error).split('\n')[0].slice(0, 200)}`;
        a.textEl.innerHTML = richMd(a.text); hydrate(a.textEl);
      }
    }
    scrollDown(true);
  } catch { emptyState(); }
}

export function initChat(body, tools) {
  log = h('div.cl-log');
  quickRow = h('div.cl-quick');
  ctxRow = h('div.cl-ctx');
  attRow = h('div.cl-atts', { hidden: true });
  input = h('textarea', { rows: 1, placeholder: 'Ask Claude to edit… (/ to focus, hold ` to talk)', spellcheck: true });
  sendBtn = h('button.send', { title: 'Send (Enter)', html: icon('send'), on: { click: () => send() } });
  stopBtn = h('button.send', { title: 'Stop', html: icon('stop'), hidden: true, style: { background: 'var(--bad)' }, on: { click: stop } });
  micBtn = h('button.ib.sm.mic', { title: 'Talk to Claude (hold ` to talk)', hidden: !SR,
    html: '<svg class="ic" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><rect x="5.5" y="1.8" width="5" height="8" rx="2.5"/><path d="M3 7.5a5 5 0 0 0 10 0M8 12.5v2"/></svg>',
    on: { click: () => (rec ? stopVoice() : startVoice()) } });
  const attachBtn = h('button.ib.sm', { title: 'Grab the program frame', html: icon('grab', 14), on: { click: () => emit('grabProgram') } });
  const box = h('div.cl-box', input, h('div.row2', attachBtn, micBtn, h('span.hint', 'Shift+Enter for a new line'), stopBtn, sendBtn));
  body.appendChild(h('div.cl', log, quickRow, ctxRow, attRow, h('div.cl-input', box)));
  tools.append(h('button.btn.ghost', { title: 'Start a new conversation', on: { click: newChat } }, h('span', { html: icon('newchat', 13) }), 'New chat'));

  wireChips(log);
  input.addEventListener('input', () => { if (!rec) input.classList.remove('interim'); autosize(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    if (e.key === 'Escape') input.blur();
  });
  // hold-to-talk on the backtick key (anywhere except other text fields)
  if (SR) {
    window.addEventListener('keydown', (e) => {
      if (e.key !== '`' || e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e) && e.target !== input) return;
      e.preventDefault(); e.stopPropagation();
      if (!e.repeat) startVoice();
    }, true);
    window.addEventListener('keyup', (e) => { if (e.key === '`' && rec) { e.preventDefault(); stopVoice(); } }, true);
  }
  log.addEventListener('click', (e) => {
    const a = e.target.closest('a.tc-link');
    if (a) { setPlayhead(parseTc(a.dataset.tc, fps()), 'chat'); emit('revealPlayhead'); }
  });

  on('context', renderCtx);
  on('selection', renderCtx);
  on('sourceLoaded', renderCtx);
  on('markers', () => { renderCtx(); renderQuick(); });
  on('plan', () => { renderCtx(); renderQuick(); });
  on('attach', (a) => { S.attachments.push(a); renderAtts(); });
  on('askClaude', (t) => { input.value = t; autosize(); input.focus(); input.setSelectionRange(t.length, t.length); });
  on('focusChat', () => input.focus());
  on('sendChat', (t) => send(t));
  on('starter', useStarter);
  on('markerToClaude', (m) => send(`Marker at ${tc(m.t, fps())}: ${m.text || '(no note, look at the frame)'}\nFix this exactly, nothing else.`));
  renderQuick(); renderCtxNow(); syncBtns();
  loadHistory();
}
