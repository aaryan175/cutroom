// Small shared helpers: DOM building, time formatting, throttling, icons, toasts, markdown-lite.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** h('div.cls#id', {attrs, on:{click}}, ...children) */
export function h(tag, props, ...kids) {
  tag = tag.replace(/[.#]+$/, '').replace(/\.{2,}/g, '.');
  const m = tag.match(/^([a-z0-9-]+)?((?:[.#][\w-]+)*)$/i);
  const el = document.createElement((m && m[1]) || 'div');
  if (m && m[2]) {
    for (const part of m[2].match(/[.#][\w-]+/g)) {
      if (part[0] === '.') el.classList.add(part.slice(1));
      else el.id = part.slice(1);
    }
  }
  if (props && (typeof props !== 'object' || props instanceof Node || Array.isArray(props))) {
    kids.unshift(props); props = null;
  }
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'on') { for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn); }
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'text') el.textContent = v;
      else if (k in el && typeof v !== 'string') el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  appendKids(el, kids);
  return el;
}
function appendKids(el, kids) {
  for (const k of kids) {
    if (k === null || k === undefined || k === false) continue;
    if (Array.isArray(k)) appendKids(el, k);
    else el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** Timecode HH:MM:SS:FF */
export function tc(t, fps = 25) {
  if (!isFinite(t)) t = 0;
  const neg = t < 0; t = Math.abs(t);
  const fr = Math.round(t * fps);
  const ifps = Math.round(fps);
  const f = fr % ifps, s = Math.floor(fr / ifps);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return (neg ? '-' : '') + [hh, mm, ss, f].map((n) => String(n).padStart(2, '0')).join(':');
}
/** Short m:ss.s for labels */
export function tshort(t) {
  if (!isFinite(t)) return '–';
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}
export function dur(t) {
  if (!isFinite(t)) return '–';
  t = Math.max(0, t);
  if (t < 60) return `${t.toFixed(t < 10 ? 1 : 0)}s`;
  const m = Math.floor(t / 60), s = Math.round(t % 60);
  if (m < 60) return `${m}:${String(s).padStart(2, '0')}`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}`;
}
export function ago(epoch) {
  if (!epoch) return '';
  const d = Date.now() / 1000 - epoch;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(epoch * 1000).toLocaleDateString();
}
export function eta(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return '';
  if (sec < 90) return `${Math.round(sec)} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}

export function debounce(fn, ms) {
  let id; return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); };
}
export function throttle(fn, ms) {
  let last = 0, timer = null, lastArgs;
  return (...a) => {
    lastArgs = a;
    const now = performance.now();
    if (now - last >= ms) { last = now; fn(...a); }
    else if (!timer) timer = setTimeout(() => { timer = null; last = performance.now(); fn(...lastArgs); }, ms - (now - last));
  };
}

export const isTyping = (e) => {
  const t = e.target;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
};

// ---------------------------------------------------------------- icons (16px, 1.5 stroke)
const P = {
  chev: '<path d="M4.5 6.5 8 10l3.5-3.5"/>',
  undo: '<path d="M5.5 4 2.5 7l3 3"/><path d="M2.5 7h7a4 4 0 0 1 0 8H7"/>',
  redo: '<path d="M10.5 4l3 3-3 3"/><path d="M13.5 7h-7a4 4 0 0 0 0 8H9"/>',
  commit: '<circle cx="8" cy="8" r="2.5"/><path d="M1.5 8h4M10.5 8h4"/>',
  render: '<rect x="2" y="3.5" width="12" height="9" rx="1.5"/><path d="M6.8 6v4l3.4-2z" fill="currentColor" stroke="none"/>',
  help: '<rect x="1.5" y="4" width="13" height="8.5" rx="1.5"/><path d="M4 7h1M7 7h1M10 7h2M4 9.8h8"/>',
  play: '<path d="M5 3.2v9.6L12.5 8z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M4.5 3.5h2.3v9H4.5zM9.2 3.5h2.3v9H9.2z" fill="currentColor" stroke="none"/>',
  stepb: '<path d="M11 4 6 8l5 4z" fill="currentColor" stroke="none"/><path d="M4.5 4v8"/>',
  stepf: '<path d="M5 4l5 4-5 4z" fill="currentColor" stroke="none"/><path d="M11.5 4v8"/>',
  markin: '<path d="M6 3H4.5v10H6"/><path d="M8 8h4"/>',
  markout: '<path d="M10 3h1.5v10H10"/><path d="M4 8h4"/>',
  insert: '<path d="M2 11h12"/><path d="M8 2.5V8"/><path d="M5.5 5.5 8 8l2.5-2.5"/><path d="M2 9v4M14 9v4"/>',
  overwrite: '<rect x="2" y="9" width="12" height="4" rx=".5"/><path d="M8 2.5V7"/><path d="M5.5 4.5 8 7l2.5-2.5"/>',
  cutaway: '<rect x="2" y="9.5" width="12" height="3.5" rx=".5"/><rect x="5" y="4" width="6" height="3.5" rx=".5"/>',
  grab: '<path d="M2 5V3h2M12 3h2v2M14 11v2h-2M4 13H2v-2"/><circle cx="8" cy="8" r="2.2"/>',
  safe: '<rect x="1.5" y="3" width="13" height="10" rx="1"/><rect x="3.8" y="5" width="8.4" height="6" stroke-dasharray="1.5 1.2"/>',
  snap: '<path d="M4 2.5v6a4 4 0 0 0 8 0v-6"/><path d="M4 5h2.4M9.6 5H12"/>',
  zoomin: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14M5 7h4M7 5v4"/>',
  zoomout: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14M5 7h4"/>',
  fit: '<path d="M2 6V2.5h3.5M14 6V2.5h-3.5M2 10v3.5h3.5M14 10v3.5h-3.5"/>',
  razor: '<path d="M8 1.5v13" stroke-dasharray="2 1.6"/><path d="M4 5.5 8 8l-4 2.5zM12 5.5 8 8l4 2.5z"/>',
  freeze: '<path d="M8 2v12M2.8 5l10.4 6M2.8 11l10.4-6"/>',
  marker: '<path d="M4 2.5h8v7L8 13.5 4 9.5z"/>',
  trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9"/>',
  grid: '<rect x="2" y="2" width="5" height="5" rx=".5"/><rect x="9" y="2" width="5" height="5" rx=".5"/><rect x="2" y="9" width="5" height="5" rx=".5"/><rect x="9" y="9" width="5" height="5" rx=".5"/>',
  list: '<path d="M5.5 4h8.5M5.5 8h8.5M5.5 12h8.5"/><path d="M2 4h1M2 8h1M2 12h1"/>',
  search: '<circle cx="7" cy="7" r="4.3"/><path d="M10.3 10.3 14 14"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  plus: '<path d="M8 3v10M3 8h10"/>',
  send: '<path d="M2.5 8 13.5 2.8 10.4 13.5 7.6 8.9z"/><path d="M7.6 8.9 13.5 2.8"/>',
  stop: '<rect x="4" y="4" width="8" height="8" rx="1" fill="currentColor" stroke="none"/>',
  newchat: '<path d="M2.5 3.5h11v7.5h-6L4 13.5V11H2.5z"/><path d="M8 5.2v4M6 7.2h4"/>',
  speech: '<path d="M2.5 3.5h11v7.5h-6L4 13.5V11H2.5z"/>',
  person: '<circle cx="8" cy="5.5" r="2.6"/><path d="M3 14c.6-2.8 2.6-4.3 5-4.3s4.4 1.5 5 4.3"/>',
  music: '<path d="M6 12V3.5l7-1.5V10.5"/><circle cx="4.5" cy="12" r="1.6"/><circle cx="11.5" cy="10.5" r="1.6"/>',
  film: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M5 2.5v11M11 2.5v11M2 6h3M2 10h3M11 6h3M11 10h3"/>',
  doc: '<path d="M4 1.8h5.5L12.5 5v9.2H4z"/><path d="M9.5 1.8V5h3"/>',
  check: '<path d="M3 8.5 6.5 12 13 4.5"/>',
  link: '<path d="M7 9a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-.8.8"/><path d="M9 7a3 3 0 0 0-4.2 0l-2 2A3 3 0 0 0 7 13.2l.8-.8"/>',
  sync: '<path d="M2.5 7a5.5 5.5 0 0 1 10-2.5M13.5 9a5.5 5.5 0 0 1-10 2.5"/><path d="M12.5 1.8v2.8H9.7M3.5 14.2v-2.8h2.8"/>',
  moon: '<path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"/>',
  warn: '<path d="M8 2 14.5 13.5h-13z"/><path d="M8 6.5v3.2M8 11.3v.2"/>',
  dot: '<circle cx="8" cy="8" r="3" fill="currentColor" stroke="none"/>',
  attach: '<path d="M11.5 7.5 7 12a2.8 2.8 0 0 1-4-4l5-5a1.9 1.9 0 0 1 2.7 2.7L5.8 10.6a.9.9 0 0 1-1.3-1.3L9 4.8"/>',
  tool: '<path d="M10 2.3a3.3 3.3 0 0 0-3.8 4.4L2.3 10.6a1.3 1.3 0 0 0 1.8 1.9l3.9-3.9a3.3 3.3 0 0 0 4.4-3.8l-2 2-1.9-.4-.4-1.9z"/>',
  spark: '<path d="M8 1.8v3.4M8 10.8v3.4M1.8 8h3.4M10.8 8h3.4M3.6 3.6l2.2 2.2M10.2 10.2l2.2 2.2M3.6 12.4l2.2-2.2M10.2 5.8l2.2-2.2"/>',
  play2: '<path d="M5 3.2v9.6L12.5 8z"/>',
  cal: '<rect x="2" y="3" width="12" height="11" rx="1.2"/><path d="M2 6.5h12M5 1.8v2.4M11 1.8v2.4"/>',
};
export function icon(name, size = 16) {
  return `<svg class="ic" viewBox="0 0 16 16" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ''}</svg>`;
}
export function iconEl(name, size) {
  const s = document.createElement('span');
  s.className = 'icw';
  s.innerHTML = icon(name, size);
  return s;
}
export function hydrateIcons(root = document) {
  for (const i of root.querySelectorAll('i[data-icon]')) i.outerHTML = icon(i.dataset.icon);
}

// ---------------------------------------------------------------- toasts
export function toast(msg, kind = 'info', ms = 4200) {
  const box = document.getElementById('toasts');
  const t = h('div.toast.' + kind, h('span', msg), h('button.toast-x', { html: icon('x', 12), on: { click: () => t.remove() } }));
  box.appendChild(t);
  setTimeout(() => t.classList.add('out'), ms);
  setTimeout(() => t.remove(), ms + 300);
}

// ---------------------------------------------------------------- markdown-lite
let mdLinks = true;
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
  if (mdLinks) s = s.replace(/\b(\d{1,2}:\d{2}(?::\d{2})?(?:[.:]\d{1,2})?)\b/g, '<a class="tc-link" data-tc="$1">$1</a>');
  return s;
}
/** paragraphs, -/* and 1. lists, ``` code fences, #-headings, **bold**, `code`; timecodes become seek links */
export function md(src, { links = true } = {}) {
  mdLinks = links;
  const lines = String(src || '').replace(/\r/g, '').split('\n');
  let out = '', i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out += `<pre><code>${esc(buf.join('\n'))}</code></pre>`;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(ln)) {
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
      const [head, ...rest] = body;
      out += '<div class="md-table"><table><thead><tr>' + cells(head).map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>' +
        rest.map((r) => '<tr>' + cells(r).map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>';
      continue;
    }
    const hm = ln.match(/^(#{1,4})\s+(.*)/);
    if (hm) { out += `<h${hm[1].length + 2}>${inline(hm[2])}</h${hm[1].length + 2}>`; i++; continue; }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(ln)) {
      const ordered = /^\s*\d+[.)]/.test(ln);
      const buf = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        buf.push(`<li>${inline(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ''))}</li>`); i++;
      }
      out += ordered ? `<ol>${buf.join('')}</ol>` : `<ul>${buf.join('')}</ul>`;
      continue;
    }
    if (!ln.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^```|^#{1,4}\s|^\s*([-*•]|\d+[.)])\s+|^\s*\|.*\|\s*$/.test(lines[i])) buf.push(lines[i++]);
    out += `<p>${buf.map(inline).join('<br>')}</p>`;
  }
  return out;
}
/** "1:12" / "00:01:12:04" / "1:12.5" → seconds */
export function parseTc(s, fps = 25) {
  const p = String(s).trim().split(':');
  if (p.length === 4) return (+p[0]) * 3600 + (+p[1]) * 60 + (+p[2]) + (+p[3]) / fps;
  if (p.length === 3) return (+p[0]) * 3600 + (+p[1]) * 60 + parseFloat(p[2]);
  if (p.length === 2) return (+p[0]) * 60 + parseFloat(p[1]);
  return parseFloat(s);
}

export const LS = {
  get(k, d) { try { const v = localStorage.getItem('cutroom.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('cutroom.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
