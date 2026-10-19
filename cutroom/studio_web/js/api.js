// HTTP + SSE layer. Every backend call goes through here (contract: docs/STUDIO.md).

export class ApiError extends Error {
  constructor(msg, status, body) { super(msg); this.status = status; this.body = body; }
}

async function req(method, path, body, opts = {}) {
  const init = { method, headers: {} , signal: opts.signal };
  if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, init); }
  catch (e) { if (e.name === 'AbortError') throw e; throw new ApiError(`Can't reach the studio server (${method} ${path})`, 0); }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json().catch(() => null) : await r.text();
  if (!r.ok) {
    const msg = (data && (data.error || (data.errors && data.errors.join('; ')) || data.message)) || `${r.status} ${r.statusText}`;
    throw new ApiError(typeof msg === 'string' ? msg : JSON.stringify(msg), r.status, data);
  }
  return data;
}

const qs = (o) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o || {})) if (v !== undefined && v !== null && v !== '' && v !== false) p.set(k, v === true ? '1' : v);
  const s = p.toString();
  return s ? '?' + s : '';
};
const enc = encodeURIComponent;

export const api = {
  project: () => req('GET', '/api/project'),
  clips: (params, signal) => req('GET', '/api/clips' + qs(params), undefined, { signal }),
  clip: (id) => req('GET', `/api/clip/${enc(id)}`),
  setTags: (id, tags) => req('PUT', `/api/clip/${enc(id)}/tags`, tags),
  plan: (slug) => req('GET', `/api/plan/${enc(slug)}`),
  putPlan: (slug, plan, force) => req('PUT', `/api/plan/${enc(slug)}`, force ? { plan, force: true } : { plan }),
  op: (slug, op) => req('POST', `/api/plan/${enc(slug)}/op`, op),
  undo: (slug) => req('POST', `/api/plan/${enc(slug)}/undo`),
  redo: (slug) => req('POST', `/api/plan/${enc(slug)}/redo`),
  commit: (slug) => req('POST', `/api/plan/${enc(slug)}/commit`),
  newPlan: (title) => req('POST', '/api/plan/new', { title }),
  render: (plan_file, preview) => req('POST', '/api/render', { plan_file, preview }),
  qa: (video) => req('POST', '/api/qa', { video }),
  jobs: () => req('GET', '/api/jobs'),
  markers: (plan) => req('GET', '/api/markers' + qs({ plan })),
  addMarker: (m) => req('POST', '/api/markers', m),
  patchMarker: (id, fields) => req('PATCH', `/api/markers/${enc(id)}`, { id, ...fields }),
  delMarker: (id) => req('DELETE', `/api/markers/${enc(id)}?id=${enc(id)}`),
  grab: (g) => req('POST', '/api/grab', g),
  context: (ctx) => req('POST', '/api/context', ctx),
  chatStop: () => req('POST', '/api/chat/stop'),
  chatHistory: () => req('GET', '/api/chat/history'),
  chatNew: () => req('POST', '/api/chat/new'),
  governor: () => req('GET', '/api/governor'),
  bins: () => req('GET', '/api/bins'),
  days: () => req('GET', '/api/days'),
  overnight: (action) => req('POST', '/api/overnight', { action }),
  text: async (url) => { const r = await fetch(url); if (!r.ok) throw new ApiError(`${r.status} ${url}`, r.status); return r.text(); },
  json: async (url) => { const r = await fetch(url); if (!r.ok) throw new ApiError(`${r.status} ${url}`, r.status); return r.json(); },
};

export const media = {
  proxy: (id) => `/media/proxy/${enc(id)}`,
  sheet: (id) => `/media/sheet/${enc(id)}`,
  thumb: (id, t = 'mid') => (typeof t === 'number' ? `/media/thumb/${enc(id)}?t=${t.toFixed(1)}` : `/media/thumb/${enc(id)}`),
  frame: (id, t, w = 1280) => `/media/frame/${enc(id)}?t=${t.toFixed(2)}&w=${w}`,
  render: (f) => `/media/render/${f.split('/').map(enc).join('/')}`,
  ref: (f) => `/media/ref/${enc(f)}`,
  song: (id) => `/media/song/${enc(id)}`,
};

/** Parse an SSE byte stream from fetch(); calls onEvent({event, data}) per message. */
export async function readSSE(response, onEvent, signal) {
  const reader = response.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const flush = (block) => {
    let event = 'message', data = [];
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      const val = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') event = val;
      else if (field === 'data') data.push(val);
    }
    if (!data.length) return;
    const raw = data.join('\n');
    let obj; try { obj = JSON.parse(raw); } catch { obj = { type: event === 'message' ? 'text' : event, text: raw }; }
    if (obj && typeof obj === 'object' && !obj.type && event !== 'message') obj.type = event;
    onEvent(obj);
  };
  while (true) {
    if (signal && signal.aborted) { try { reader.cancel(); } catch { /* ignore */ } break; }
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
    let k;
    while ((k = buf.indexOf('\n\n')) >= 0) { flush(buf.slice(0, k)); buf = buf.slice(k + 2); }
  }
  if (buf.trim()) flush(buf);
}

/** POST /api/chat and stream events. */
export async function chatStream(body, onEvent, signal) {
  let r;
  try {
    r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify(body), signal });
  } catch (e) {
    if (e.name === 'AbortError') return;
    throw new ApiError("Can't reach the studio server to start the chat", 0);
  }
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); msg = j.error || j.message || msg; } catch { /* not json */ }
    throw new ApiError(msg, r.status);
  }
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) { onEvent(await r.json()); return; }
  await readSSE(r, onEvent, signal);
}

/** Live /api/events with auto-reconnect and backoff. handlers: {name: fn(data)}, onState(state). */
export function connectEvents(handlers, onState) {
  let es = null, retry = 1000, stopped = false, timer = null;
  const names = ['hello', 'plan_changed', 'job', 'jobs', 'governor', 'render_done', 'qa_done', 'notes', 'markers', 'overnight', 'chat', 'bins_changed'];
  const dispatch = (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = { text: raw }; }
    const n = (name && name !== 'message') ? name : (d && d.type);
    const fn = handlers[n];
    if (fn) { try { fn(d || {}); } catch (e) { console.error('event handler', n, e); } }
  };
  const open = () => {
    if (stopped) return;
    onState && onState('connecting');
    es = new EventSource('/api/events');
    es.onopen = () => { retry = 1000; onState && onState('live'); };
    es.onmessage = (e) => dispatch('message', e.data);
    for (const n of names) es.addEventListener(n, (e) => dispatch(n, e.data));
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED || es.readyState === EventSource.CONNECTING) {
        onState && onState('down');
        es.close();
        clearTimeout(timer);
        timer = setTimeout(open, retry);
        retry = Math.min(retry * 1.7, 15000);
      }
    };
  };
  open();
  return { close() { stopped = true; clearTimeout(timer); es && es.close(); } };
}
