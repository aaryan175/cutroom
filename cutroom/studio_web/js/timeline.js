// Canvas timeline: ruler, section bands, V3/V2/V1/A1/A2, markers, playhead, hand edits.
import { S, on, emit, op, select, setPlayhead, pieceAt, sectionAt, segmentOf, KIND_COLOR, fps, selectedSegs, undo } from './state.js';
import { h, icon, tc, clamp, LS, throttle, toast, eta } from './util.js';
import { media, api } from './api.js';
import { STARTERS } from './chat.js';

const RULER = 24, BAND = 17;
const TRACKS = [
  { id: 'V3', base: 32, h: 32, name: 'Graphics', grow: 0.05, max: 40 },
  { id: 'V2', base: 34, h: 34, name: 'Cutaways', grow: 0.15, max: 56 },
  { id: 'V1', base: 60, h: 60, name: 'Main', grow: 0.5, max: 130 },
  { id: 'A1', base: 32, h: 32, name: 'Dialogue', grow: 0.15, max: 56 },
  { id: 'A2', base: 36, h: 36, name: 'Music', grow: 0.15, max: 56 },
];
let headEls = {};
const SNAP_PX = 8;
const EDGE_PX = 5;

let canvas, ctx, wrap, heads, hscroll, hinner, tip, phTc;
let W = 0, H = 0, DPR = 1;
const view = { pps: LS.get('tl.pps', 0), x0: 0 }; // pixels per second, left time
let trackY = {};
let dirty = true;
let drag = null;   // active interaction
let hover = null;
const flashes = new Map(); // piece id -> t0 (ms)
let dropHint = null;       // {t, track}
let markerPop = null;

// ---------------------------------------------------------------- thumbnails
const thumbs = new Map(); // url -> {img, ok}
let loading = 0; const queue = [];
function thumb(clip, t) {
  const tt = Math.max(0, Math.round(t));
  const url = media.thumb(clip, tt);
  let e = thumbs.get(url);
  if (e) { thumbs.delete(url); thumbs.set(url, e); return e.ok ? e.img : null; }
  e = { img: new Image(), ok: false };
  e.img.decoding = 'async';
  thumbs.set(url, e);
  queue.push([e, url]);
  pump();
  if (thumbs.size > 900) { const k = thumbs.keys().next().value; thumbs.delete(k); }
  return null;
}
function pump() {
  while (loading < 6 && queue.length) {
    const [e, url] = queue.pop();
    loading++;
    e.img.onload = () => { e.ok = true; loading--; dirty = true; pump(); };
    e.img.onerror = () => { loading--; pump(); };
    e.img.src = url;
  }
}

// ---------------------------------------------------------------- geometry
const tToX = (t) => (t - view.x0) * view.pps;
const xToT = (x) => x / view.pps + view.x0;
function layoutTracks(avail) {
  if (avail) {
    const base = TRACKS.reduce((a, t) => a + t.base + 1, 0);
    const extra = Math.max(0, avail - RULER - BAND - base - 6);
    for (const t of TRACKS) t.h = Math.round(Math.min(t.max, t.base + extra * t.grow));
  }
  let y = RULER + BAND;
  trackY = {};
  for (const tr of TRACKS) {
    trackY[tr.id] = { y, h: tr.h };
    const el = headEls[tr.id];
    if (el) { el.style.top = y + 'px'; el.style.height = tr.h + 'px'; }
    y += tr.h + 1;
  }
  return y;
}
function contentW() { return S.tl ? S.tl.duration : 0; }

export function zoomTo(pps, anchorT) {
  const ax = anchorT === undefined ? W / 2 : tToX(anchorT);
  const t = anchorT === undefined ? xToT(W / 2) : anchorT;
  view.pps = clamp(pps, 2, 600);
  view.x0 = t - ax / view.pps;
  clampView();
  LS.set('tl.pps', view.pps);
  syncScroll(); dirty = true;
}
export function zoomBy(f, anchorT = S.playhead) { zoomTo(view.pps * f, anchorT); }
export function fit() {
  const d = contentW();
  if (!d || !W) return;
  view.pps = clamp((W - 30) / d, 2, 600);
  view.x0 = 0;
  LS.set('tl.pps', view.pps);
  syncScroll(); dirty = true;
}
function clampView() {
  const maxX0 = Math.max(0, contentW() - (W * 0.6) / view.pps);
  view.x0 = clamp(view.x0, -10 / view.pps, maxX0 + 0.0001);
}
let syncing = false;
function syncScroll() {
  if (!hinner) return;
  const total = (contentW() + (W * 0.6) / view.pps) * view.pps;
  hinner.style.width = Math.max(W + 1, total) + 'px';
  syncing = true;
  hscroll.scrollLeft = Math.max(0, view.x0 * view.pps);
  requestAnimationFrame(() => { syncing = false; });
}

// ---------------------------------------------------------------- snapping
function snapPoints(exclude) {
  const pts = [S.playhead];
  if (S.tl) {
    for (const p of S.tl.pieces) {
      if (exclude && exclude.has(p.id)) continue;
      pts.push(p.start, p.end);
    }
    for (const m of S.markers) pts.push(m.t);
    for (const b of S.tl.beats) pts.push(b);
  }
  return pts;
}
function snap(t, exclude) {
  if (!S.snapping) return { t, snapped: false };
  const tol = SNAP_PX / view.pps;
  let best = t, bd = tol;
  for (const p of snapPoints(exclude)) { const d = Math.abs(p - t); if (d < bd) { bd = d; best = p; } }
  return { t: best, snapped: best !== t };
}

// ---------------------------------------------------------------- segments
function segPieces(sec, seg) { return S.tl ? S.tl.v1.filter((p) => p.sec === sec && p.seg === seg) : []; }
function segRange(sec, seg) {
  const ps = segPieces(sec, seg);
  if (!ps.length) return null;
  return { start: ps[0].start, end: ps[ps.length - 1].end, first: ps[0], last: ps[ps.length - 1] };
}
/** boundaries where a segment can be inserted: [{t, sec, gi}] */
function boundaries() {
  const out = [];
  const d = S.plan && S.plan.draft;
  if (!d || !S.tl) return out;
  (d.sections || []).forEach((sec, si) => {
    const n = (sec.segments || []).length;
    let lastEnd = null;
    for (let gi = 0; gi < n; gi++) {
      const r = segRange(si, gi);
      if (!r) continue;
      out.push({ t: r.start, sec: si, gi });
      lastEnd = r.end;
    }
    if (lastEnd !== null) out.push({ t: lastEnd, sec: si, gi: n });
    else {
      const s = S.tl.sections.find((x) => x.index === si);
      if (s) out.push({ t: s.start, sec: si, gi: 0 });
    }
  });
  return out;
}
function nearestBoundary(t) {
  const sec = sectionAt(t);
  let best = null, bd = Infinity;
  for (const b of boundaries()) {
    let d = Math.abs(b.t - t);
    if (sec && b.sec !== sec.index) d += 1e-3; // prefer the section under the pointer on ties
    if (d < bd) { bd = d; best = b; }
  }
  return best;
}

// ---------------------------------------------------------------- hit testing
function hit(x, y) {
  if (y < RULER) {
    const m = markerAt(x);
    return { area: 'ruler', marker: m };
  }
  if (y < RULER + BAND) return { area: 'band', section: S.tl && S.tl.sections.find((s) => tToX(s.start) <= x && tToX(s.end) > x) };
  const t = xToT(x);
  for (const tr of TRACKS) {
    const ty = trackY[tr.id];
    if (y >= ty.y && y < ty.y + ty.h) {
      if (!S.tl) return { area: 'track', track: tr.id, t };
      if (tr.id === 'V1' || tr.id === 'V2') {
        const p = pieceAt(t, tr.id);
        let edge = null;
        if (tr.id === 'V1') {
          // segment edges are trimmable; check the nearest segment boundary in pixels
          for (const q of S.tl.v1) {
            if (Math.abs(tToX(q.start) - x) <= EDGE_PX || Math.abs(tToX(q.end) - x) <= EDGE_PX) {
              const r = segRange(q.sec, q.seg);
              if (!r || ['montage', 'matchcut'].includes(q.kind)) continue;
              if (Math.abs(tToX(r.start) - x) <= EDGE_PX && (!p || p.sec !== q.sec || p.seg !== q.seg || x - tToX(r.start) < EDGE_PX)) {
                if (!edge || x >= tToX(r.start)) edge = { edge: 'in', sec: q.sec, seg: q.seg, r };
              }
              if (Math.abs(tToX(r.end) - x) <= EDGE_PX) {
                if (!edge || x <= tToX(r.end)) edge = { edge: 'out', sec: q.sec, seg: q.seg, r };
              }
            }
          }
        }
        return { area: 'track', track: tr.id, t, piece: p, edge };
      }
      return { area: 'track', track: tr.id, t };
    }
  }
  return { area: 'none', t };
}
function markerAt(x) {
  for (const m of S.markers) if (Math.abs(tToX(m.t) - x) <= 6) return m;
  return null;
}

// ---------------------------------------------------------------- drawing
function waveH(seed, i) {
  // deterministic speech-like envelope
  const a = Math.sin(seed * 12.9898 + i * 0.71) * 43758.5453;
  const n = a - Math.floor(a);
  const env = 0.55 + 0.45 * Math.sin(i * 0.23 + seed) * Math.sin(i * 0.051 + seed * 3);
  return Math.max(0.08, Math.min(1, n * 0.6 * Math.abs(env) + 0.15 * Math.abs(env)));
}
function hashStr(s) { let x = 0; for (let i = 0; i < (s || '').length; i++) x = (x * 31 + s.charCodeAt(i)) | 0; return (x % 1000) / 37; }

function rrect(x, y, w, hh, r) {
  ctx.beginPath();
  r = Math.min(r, w / 2, hh / 2);
  ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + hh - r); ctx.quadraticCurveTo(x + w, y + hh, x + w - r, y + hh);
  ctx.lineTo(x + r, y + hh); ctx.quadraticCurveTo(x, y + hh, x, y + hh - r);
  ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}
function shade(hex, f) {
  const n = parseInt(hex.slice(1), 16);
  let r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  if (f > 0) { r += (255 - r) * f; g += (255 - g) * f; b += (255 - b) * f; } else { r *= 1 + f; g *= 1 + f; b *= 1 + f; }
  return `rgb(${r | 0},${g | 0},${b | 0})`;
}
function niceStep(minPx) {
  const f = fps();
  const steps = [1 / f, 2 / f, 5 / f, 10 / f, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const s of steps) if (s * view.pps >= minPx) return s;
  return 1200;
}
function rulerLabel(t, step) {
  const f = Math.round(fps());
  const fr = Math.round(t * f), s = Math.floor(fr / f);
  const mm = Math.floor(s / 60), ss = s % 60;
  const base = `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
  return step < 1 ? `${base}:${String(fr % f).padStart(2, '0')}` : base;
}

function draw() {
  if (!ctx) return;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#161618'; ctx.fillRect(0, 0, W, H);
  ctx.font = '11px -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  const now = performance.now();
  const tl = S.tl;
  const tA = xToT(0), tB = xToT(W);
  const endX = tl ? tToX(tl.duration) : 0;

  // lanes
  for (const tr of TRACKS) {
    const { y, h: hh } = trackY[tr.id];
    ctx.fillStyle = tr.id[0] === 'V' ? '#1b1b1e' : '#19191c';
    ctx.fillRect(0, y, W, hh);
    ctx.fillStyle = '#121214'; ctx.fillRect(0, y + hh, W, 1);
  }
  if (tl && endX < W) { ctx.fillStyle = 'rgba(0,0,0,.22)'; ctx.fillRect(Math.max(0, endX), RULER + BAND, W, H); }

  // ruler
  ctx.fillStyle = '#202024'; ctx.fillRect(0, 0, W, RULER);
  ctx.fillStyle = '#2b2b30'; ctx.fillRect(0, RULER - 1, W, 1);
  const major = niceStep(78), minor = major / (major >= 1 && major % 5 === 0 ? 5 : 4);
  ctx.strokeStyle = '#4a4a52'; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let t = Math.floor(tA / minor) * minor; t <= tB; t += minor) {
    const x = Math.round(tToX(t)) + 0.5;
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    ctx.moveTo(x, RULER - (isMajor ? 9 : 4)); ctx.lineTo(x, RULER - 1);
  }
  ctx.stroke();
  ctx.fillStyle = '#8c8c95';
  ctx.font = '10px ui-monospace, "SF Mono", Menlo, Consolas, monospace';
  for (let t = Math.ceil(tA / major) * major; t <= tB; t += major) {
    if (t < 0) continue;
    ctx.fillText(rulerLabel(t, major), Math.round(tToX(t)) + 4, 8);
  }
  ctx.font = '11px -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", system-ui, sans-serif';

  // section bands
  ctx.fillStyle = '#1e1e22'; ctx.fillRect(0, RULER, W, BAND);
  if (tl) {
    tl.sections.forEach((s, i) => {
      const x1 = tToX(s.start), x2 = tToX(s.end);
      if (x2 < 0 || x1 > W) return;
      ctx.fillStyle = i % 2 ? '#26262c' : '#2b2b32';
      ctx.fillRect(x1, RULER + 1, Math.max(1, x2 - x1 - 1), BAND - 2);
      ctx.fillStyle = s.music ? '#5b7895' : '#55555e';
      ctx.fillRect(x1, RULER + 1, 2, BAND - 2);
      ctx.save(); ctx.beginPath(); ctx.rect(x1, RULER, Math.max(0, x2 - x1 - 4), BAND); ctx.clip();
      ctx.fillStyle = '#b4b4bc';
      ctx.fillText(s.name, Math.max(x1, 0) + 7, RULER + BAND / 2 + 0.5);
      ctx.restore();
      // section divider through the tracks
      ctx.fillStyle = 'rgba(255,255,255,.05)';
      ctx.fillRect(Math.round(x1), RULER + BAND, 1, H);
    });
  }
  ctx.fillStyle = '#121214'; ctx.fillRect(0, RULER + BAND - 1, W, 1);

  if (tl) {
    drawV3(tl, tA, tB);
    drawV(tl.v2, 'V2', now);
    drawV(tl.v1, 'V1', now);
    drawA1(tl, tA, tB);
    drawA2(tl, tA, tB);
  }

  // in-flight interactions
  if (drag && drag.kind === 'move' && drag.moved) {
    const { y, h: hh } = trackY.V1;
    const w = (drag.r.end - drag.r.start) * view.pps;
    const gx = tToX(drag.r.start) + (drag.curX - drag.x0);
    ctx.globalAlpha = 0.55; ctx.fillStyle = KIND_COLOR[drag.r.first.kind] || '#666';
    rrect(gx, y + 3, w, hh - 6, 3); ctx.fill(); ctx.globalAlpha = 1;
    if (drag.target) {
      const x = Math.round(tToX(drag.target.t)) + 0.5;
      ctx.fillStyle = '#ffffff'; ctx.fillRect(x - 1, y - 4, 3, hh + 8);
      ctx.beginPath(); ctx.moveTo(x - 5, y - 6); ctx.lineTo(x + 6, y - 6); ctx.lineTo(x + 0.5, y); ctx.fill();
    }
  }
  if (drag && drag.kind === 'trim' && drag.moved) {
    const { y, h: hh } = trackY.V1;
    const r = drag.r;
    const a = drag.edge === 'in' ? r.start + drag.dt : r.start;
    const b = drag.edge === 'out' ? r.end + drag.dt : r.end;
    ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
    rrect(tToX(Math.min(a, b)), y + 2, Math.abs(tToX(b) - tToX(a)), hh - 4, 3); ctx.stroke();
    ctx.setLineDash([]);
    const ex = tToX(drag.edge === 'in' ? a : b);
    ctx.fillStyle = drag.snapped ? '#ffd166' : '#ffffff'; ctx.fillRect(ex - 1, y, 2, hh);
  }
  if (drag && drag.kind === 'marquee' && drag.moved) {
    const x = Math.min(drag.x0, drag.curX), y = Math.min(drag.y0, drag.curY);
    ctx.fillStyle = 'rgba(76,141,255,.12)'; ctx.strokeStyle = 'rgba(76,141,255,.8)'; ctx.lineWidth = 1;
    ctx.fillRect(x, y, Math.abs(drag.curX - drag.x0), Math.abs(drag.curY - drag.y0));
    ctx.strokeRect(x + 0.5, y + 0.5, Math.abs(drag.curX - drag.x0), Math.abs(drag.curY - drag.y0));
  }
  if (dropHint) {
    const x = Math.round(tToX(dropHint.t)) + 0.5;
    const tr = trackY[dropHint.track] || trackY.V1;
    ctx.fillStyle = '#ffffff'; ctx.fillRect(x - 1, tr.y - 2, 3, tr.h + 4);
  }

  // markers (ruler)
  for (const m of S.markers) {
    const x = Math.round(tToX(m.t)) + 0.5;
    if (x < -8 || x > W + 8) continue;
    const col = m.status === 'done' ? '#6d6d76' : (m.kind === 'ref' ? '#4fb3a8' : '#d4a23c');
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.moveTo(x - 4.5, 2); ctx.lineTo(x + 4.5, 2); ctx.lineTo(x + 4.5, 9); ctx.lineTo(x, 13); ctx.lineTo(x - 4.5, 9); ctx.closePath(); ctx.fill();
    if (m.status !== 'done') { ctx.fillStyle = col; ctx.globalAlpha = 0.35; ctx.fillRect(x, 13, 1, H); ctx.globalAlpha = 1; }
  }

  // playhead
  const px = Math.round(tToX(S.playhead)) + 0.5;
  if (px >= -10 && px <= W + 10) {
    ctx.fillStyle = '#4c8dff';
    ctx.fillRect(px - 0.5, RULER - 2, 1.5, H);
    ctx.beginPath(); ctx.moveTo(px - 6, RULER - 12); ctx.lineTo(px + 6.5, RULER - 12); ctx.lineTo(px + 6.5, RULER - 6); ctx.lineTo(px + 0.25, RULER); ctx.lineTo(px - 6, RULER - 6); ctx.closePath(); ctx.fill();
  }
  if (flashes.size) dirty = true;
}

function drawV(arr, track, now) {
  const { y, h: hh } = trackY[track];
  const segSel = new Set();
  for (const id of S.selection) segSel.add(id);
  for (let i = 0; i < arr.length; i++) {
    const p = arr[i];
    const x1 = tToX(p.start), x2 = tToX(p.end);
    if (x2 < -2 || x1 > W + 2) continue;
    const w = Math.max(1, x2 - x1);
    const col = KIND_COLOR[track === 'V2' ? 'cutaway' : p.kind] || '#555';
    const sel = segSel.has(p.id);
    const prev = arr[i - 1], next = arr[i + 1];
    const joinL = track === 'V1' && prev && prev.sec === p.sec && prev.seg === p.seg && Math.abs(prev.end - p.start) < 1e-3;
    const joinR = track === 'V1' && next && next.sec === p.sec && next.seg === p.seg && Math.abs(next.start - p.end) < 1e-3;
    const bx = x1 + (joinL ? 0 : 1), bw = w - (joinL ? 0 : 1) - (joinR ? 0 : 1);
    const top = y + 2, bh = hh - 4;
    // body
    ctx.fillStyle = sel ? shade(col, 0.18) : shade(col, -0.18);
    rrect(bx, top, Math.max(1, bw), bh, (joinL || joinR) ? 1 : 3); ctx.fill();
    const labelH = track === 'V1' ? 15 : bh;
    ctx.fillStyle = sel ? shade(col, 0.3) : col;
    ctx.fillRect(bx, top, Math.max(1, bw), Math.min(labelH, bh));
    // thumbnails
    if (track === 'V1' && p.clip && p.in !== null && bw > 24) {
      const ty = top + labelH, th = bh - labelH - 1, tw = Math.round(th * 16 / 9);
      ctx.save(); ctx.beginPath(); ctx.rect(bx + 1, ty, bw - 2, th); ctx.clip();
      const spd = p.speed || 1;
      const tiles = view.pps * (p.end - p.start) > tw * 1.6 ? Math.ceil(bw / tw) : 1;
      for (let k = 0; k < tiles; k++) {
        const tx = bx + 1 + k * tw;
        if (tx > W || tx + tw < 0) continue;
        const tt = p.in + ((tx - x1) / view.pps) * spd;
        const img = thumb(p.clip, tt);
        if (img) { ctx.globalAlpha = sel ? 1 : 0.85; ctx.drawImage(img, tx, ty, tw, th); ctx.globalAlpha = 1; }
        else { ctx.fillStyle = 'rgba(0,0,0,.25)'; ctx.fillRect(tx, ty, tw - 1, th); }
      }
      ctx.restore();
    } else if (track === 'V1' && (p.kind === 'title' || p.kind === 'black')) {
      ctx.fillStyle = p.kind === 'black' ? '#0c0c0e' : 'rgba(0,0,0,.35)';
      ctx.fillRect(bx + 1, top + labelH, bw - 2, bh - labelH - 1);
    }
    // join marker (tightened pause removed)
    if (joinR && bw > 4) {
      ctx.fillStyle = 'rgba(0,0,0,.55)'; ctx.fillRect(Math.round(x2) - 0.5, top + labelH, 1, bh - labelH);
      ctx.fillStyle = 'rgba(255,255,255,.35)'; ctx.fillRect(Math.round(x2) - 2, top + 3, 3, 3);
    }
    // label
    if (bw > 26) {
      ctx.save(); ctx.beginPath(); ctx.rect(bx + 3, top, bw - 6, labelH); ctx.clip();
      ctx.fillStyle = 'rgba(255,255,255,.93)';
      const label = p.label || [p.clip_rel ? p.clip_rel.split('/').pop() : p.clip, p.kind].filter(Boolean).join(' · ');
      ctx.fillText(joinL ? '' : label, bx + 5, top + Math.min(labelH, bh) / 2 + 0.5);
      ctx.restore();
    }
    if (sel) {
      ctx.strokeStyle = '#4c8dff'; ctx.lineWidth = 1.5;
      rrect(bx + 0.75, top + 0.75, Math.max(1, bw - 1.5), bh - 1.5, 3); ctx.stroke();
    }
    const f = flashes.get(p.id);
    if (f !== undefined) {
      const a = 1 - (now - f) / 2600;
      if (a <= 0) flashes.delete(p.id);
      else {
        ctx.strokeStyle = `rgba(255,214,102,${a})`; ctx.lineWidth = 2;
        rrect(bx + 1, top + 1, Math.max(1, bw - 2), bh - 2, 3); ctx.stroke();
        ctx.fillStyle = `rgba(255,214,102,${a * 0.18})`; ctx.fill();
      }
    }
  }
  // hover edge affordance
  if (track === 'V1' && hover && hover.edge && !drag) {
    const x = tToX(hover.edge.edge === 'in' ? hover.edge.r.start : hover.edge.r.end);
    ctx.fillStyle = '#ffd166';
    ctx.fillRect(Math.round(x) - (hover.edge.edge === 'in' ? 0 : 2), y + 2, 2, hh - 4);
  }
}

function drawV3(tl, tA, tB) {
  const { y, h: hh } = trackY.V3;
  const half = (hh - 3) / 2;
  for (const o of tl.overlays) {
    if (o.t1 < tA || o.t0 > tB) continue;
    const x1 = tToX(o.t0), w = Math.max(2, tToX(o.t1) - x1 - 1);
    ctx.fillStyle = KIND_COLOR.overlay; rrect(x1, y + 1.5, w, half, 2); ctx.fill();
    if (w > 20) { ctx.save(); ctx.beginPath(); ctx.rect(x1, y, w - 3, half + 2); ctx.clip(); ctx.fillStyle = '#f3e6cf'; ctx.font = '10px -apple-system, system-ui, sans-serif'; ctx.fillText(o.text || o.kind, x1 + 4, y + 1.5 + half / 2 + 0.5); ctx.restore(); }
  }
  for (const c of tl.captions) {
    if (c.t1 < tA || c.t0 > tB) continue;
    const x1 = tToX(c.t0), w = Math.max(1, tToX(c.t1) - x1 - 1);
    ctx.fillStyle = KIND_COLOR.caption; rrect(x1, y + 2 + half, w, half, 2); ctx.fill();
    if (w > 24) { ctx.save(); ctx.beginPath(); ctx.rect(x1, y + half, w - 3, half + 3); ctx.clip(); ctx.fillStyle = '#e9e5c6'; ctx.font = '10px -apple-system, system-ui, sans-serif'; ctx.fillText(c.text, x1 + 3, y + 2 + half * 1.5 + 0.5); ctx.restore(); }
  }
  ctx.font = '11px -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", system-ui, sans-serif';
}

function drawA1(tl, tA, tB) {
  const { y, h: hh } = trackY.A1;
  for (const d of tl.dialogue) {
    if (d.end < tA || d.start > tB) continue;
    const x1 = tToX(d.start) + 1, x2 = tToX(d.end) - 1, w = x2 - x1;
    if (w < 1) continue;
    const sel = S.selection.has(d.piece);
    ctx.fillStyle = sel ? '#2f5f45' : '#24412f'; rrect(x1, y + 2, w, hh - 4, 2); ctx.fill();
    ctx.fillStyle = sel ? '#8fe0ae' : '#5fae7c';
    const mid = y + hh / 2, amp = (hh - 8) / 2;
    const seed = hashStr(d.clip || '') + (d.in || 0);
    const step = 3;
    const startI = Math.max(0, Math.floor((0 - x1) / step));
    for (let xx = x1 + startI * step; xx < Math.min(x2, W); xx += step) {
      const srcT = (d.in || 0) + (xx - x1) / view.pps;
      const v = waveH(seed, Math.floor(srcT * 12)) * amp;
      ctx.fillRect(xx, mid - v, 2, v * 2);
    }
  }
}

function drawA2(tl, tA, tB) {
  const { y, h: hh } = trackY.A2;
  for (const m of tl.music) {
    if (m.end < tA || m.start > tB) continue;
    const x1 = tToX(m.start) + 1, w = tToX(m.end) - x1 - 1;
    ctx.fillStyle = '#233547'; rrect(x1, y + 2, w, hh - 4, 2); ctx.fill();
    ctx.fillStyle = KIND_COLOR.music; ctx.fillRect(x1, y + 2, w, 13);
    // beats
    const showBeats = view.pps * 0.25 > 3;
    ctx.fillStyle = 'rgba(180,210,240,.55)';
    if (showBeats) {
      m.beats.forEach((b, i) => {
        const x = Math.round(tToX(b)) + 0.5;
        if (x < x1 || x > x1 + w) return;
        const strong = i % 4 === 0;
        ctx.fillRect(x - 0.5, y + hh - (strong ? 15 : 9), strong ? 1.5 : 1, strong ? 13 : 7);
      });
    }
    if (w > 30) {
      ctx.save(); ctx.beginPath(); ctx.rect(x1 + 3, y, w - 6, 16); ctx.clip();
      ctx.fillStyle = '#dbe7f3';
      const lx = Math.max(x1, 0);
      const song = ((S.project && S.project.songs) || []).find((s) => s.id === m.song);
      ctx.fillText(`${m.title || (song && song.title) || m.song}  +${m.offset.toFixed(1)}s`, lx + 5, y + 9);
      ctx.restore();
    }
  }
}

// ---------------------------------------------------------------- interaction
function localXY(e) { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

function segIdsOf(p) {
  if (!p) return [];
  if (p.track !== 'V1') return [p.id];
  return segPieces(p.sec, p.seg).map((q) => q.id);
}

function onDown(e) {
  if (e.button !== 0) return;
  closeMarkerPop();
  const [x, y] = localXY(e);
  const hh = hit(x, y);
  try { canvas.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
  emit('focusMonitor', 'program');
  if (hh.area === 'ruler' && hh.marker) { openMarkerPop(hh.marker, x); return; }
  if (hh.area === 'ruler' || hh.area === 'band') {
    drag = { kind: 'scrub' };
    emit('scrubStart');
    scrubTo(x, e.shiftKey);
    return;
  }
  if (hh.area === 'track' && hh.edge && hh.track === 'V1' && S.plan) {
    const seg = segmentOf(hh.edge.r.first);
    drag = { kind: 'trim', edge: hh.edge.edge, sec: hh.edge.sec, seg: hh.edge.seg, r: hh.edge.r, x0: x, curX: x, dt: 0, moved: false,
      speed: (seg && +seg.speed) || 1, segType: seg && seg.type };
    return;
  }
  if (hh.area === 'track' && hh.piece) {
    const ids = segIdsOf(hh.piece);
    if (e.shiftKey || e.metaKey || e.ctrlKey) select(ids, 'toggle');
    else if (!S.selection.has(hh.piece.id)) select(ids, 'set');
    if (hh.track === 'V1') drag = { kind: 'move', piece: hh.piece, r: segRange(hh.piece.sec, hh.piece.seg), x0: x, curX: x, moved: false };
    else drag = { kind: 'none' };
    return;
  }
  drag = { kind: 'marquee', x0: x, y0: y, curX: x, curY: y, moved: false, additive: e.shiftKey };
}

function scrubTo(x, noSnap) {
  let t = xToT(x);
  if (!noSnap && S.snapping) t = snap(t).t;
  setPlayhead(Math.max(0, t), 'timeline');
}

function onMove(e) {
  const [x, y] = localXY(e);
  if (!drag) {
    const hh = hit(x, y);
    const prevEdge = hover && hover.edge;
    hover = hh;
    canvas.style.cursor = hh.edge ? 'ew-resize' : (hh.area === 'ruler' || hh.area === 'band') ? (hh.marker ? 'pointer' : 'col-resize') : (hh.piece ? 'grab' : 'default');
    if (!!prevEdge !== !!hh.edge || (prevEdge && hh.edge && prevEdge.edge !== hh.edge.edge)) dirty = true;
    showHoverTip(hh, x, y);
    return;
  }
  if (drag.kind === 'scrub') { scrubTo(x, e.shiftKey); autoscroll(x); return; }
  drag.curX = x; drag.curY = y;
  if (!drag.moved && Math.abs(x - drag.x0) < 3 && Math.abs(y - (drag.y0 ?? y)) < 3) return;
  drag.moved = true;
  if (drag.kind === 'move') {
    canvas.style.cursor = 'grabbing';
    const dt = (x - drag.x0) / view.pps;
    const lead = drag.r.start + dt;
    const b = nearestBoundary(x - drag.x0 > 0 ? drag.r.end + dt : lead);
    drag.target = b;
    if (b) setTip(x, y, `Move to ${S.plan.draft.sections[b.sec].name} · position ${b.gi + 1}`);
  } else if (drag.kind === 'trim') {
    const r = drag.r;
    let edgeT = (drag.edge === 'in' ? r.start : r.end) + (x - drag.x0) / view.pps;
    const ids = new Set(segPieces(drag.sec, drag.seg).map((q) => q.id));
    const s = snap(edgeT, ids);
    edgeT = s.t; drag.snapped = s.snapped;
    let dt = edgeT - (drag.edge === 'in' ? r.start : r.end);
    // keep at least 2 frames
    const minLen = 2 / fps();
    if (drag.edge === 'in') dt = Math.min(dt, r.end - r.start - minLen);
    else dt = Math.max(dt, -(r.end - r.start - minLen));
    drag.dt = dt;
    const src = dt * drag.speed;
    const sign = src >= 0 ? '+' : '−';
    setTip(x, y, `${drag.edge === 'in' ? 'In' : 'Out'} ${sign}${Math.abs(src).toFixed(2)}s  ${sign}${Math.abs(Math.round(dt * fps()))}f${drag.snapped ? '  ⟂ snap' : ''}`);
  } else if (drag.kind === 'marquee') {
    const tA = xToT(Math.min(drag.x0, x)), tB = xToT(Math.max(drag.x0, x));
    const yA = Math.min(drag.y0, y), yB = Math.max(drag.y0, y);
    const ids = [];
    for (const tr of ['V1', 'V2']) {
      const ty = trackY[tr];
      if (yB < ty.y || yA > ty.y + ty.h) continue;
      for (const p of tr === 'V1' ? S.tl.v1 : S.tl.v2) if (p.end > tA && p.start < tB) ids.push(...segIdsOf(p));
    }
    select(ids, drag.additive ? 'add' : 'set');
  }
  dirty = true;
}

async function onUp(e) {
  const d = drag; drag = null;
  hideTip();
  canvas.style.cursor = 'default';
  if (!d) return;
  if (d.kind === 'scrub') { emit('scrubEnd'); return; }
  if (d.kind === 'marquee' && !d.moved) {
    if (!d.additive) select([], 'set');
    const [x] = localXY(e);
    scrubTo(x, e.shiftKey);
  }
  if (d.kind === 'move' && !d.moved) {
    // plain click on a clip: move the playhead there too (Premiere selects; we also seek for convenience)
    const [x] = localXY(e);
    if (!(e.shiftKey || e.metaKey || e.ctrlKey)) select(segIdsOf(d.piece), 'set');
    scrubTo(x, true);
  }
  if (d.kind === 'move' && d.moved && d.target) {
    const from = [d.piece.sec, d.piece.seg];
    let to = [d.target.sec, d.target.gi];
    if (to[0] === from[0] && to[1] > from[1]) to = [to[0], to[1] - 1];
    if (to[0] !== from[0] || to[1] !== from[1]) await op({ op: 'move', seg: from, to });
  }
  if (d.kind === 'trim' && d.moved && Math.abs(d.dt) > 1e-4) {
    const delta = +(d.dt * d.speed).toFixed(4);
    await op({ op: 'trim', seg: [d.sec, d.seg], edge: d.edge, delta: ['title', 'black', 'still'].includes(d.segType) ? +(d.dt).toFixed(4) : delta });
  }
  dirty = true;
}

function onDbl(e) {
  const [x, y] = localXY(e);
  const hh = hit(x, y);
  if (hh.piece && hh.piece.clip) {
    const p = hh.piece;
    const srcT = p.in + (hh.t - p.start) * (p.speed || 1);
    emit('loadSource', { id: p.clip, t: srcT, in: p.in, out: p.out });
  }
}

function autoscroll(x) {
  if (x > W - 20) { view.x0 += 12 / view.pps; clampView(); syncScroll(); }
  else if (x < 20 && view.x0 > 0) { view.x0 -= 12 / view.pps; clampView(); syncScroll(); }
}

function setTip(x, y, text) {
  tip.hidden = false; tip.textContent = text;
  tip.style.left = Math.min(W - 200, x + 14) + 'px'; tip.style.top = Math.max(0, y - 30) + 'px';
}
function hideTip() { tip.hidden = true; }
const showHoverTip = throttle((hh, x, y) => {
  if (drag) return;
  if (hh.marker) { setTip(x, y, `${tc(hh.marker.t, fps())}  ${hh.marker.text || '(no note)'}`); return; }
  if (hh.edge) { setTip(x, y, `Drag to trim ${hh.edge.edge === 'in' ? 'In' : 'Out'}`); return; }
  if (hh.piece && hh.area === 'track') {
    const p = hh.piece;
    const seg = segmentOf(p);
    const src = p.in !== null ? `  src ${p.in.toFixed(2)}–${p.out.toFixed(2)}` : '';
    setTip(x, y, `${p.clip_rel || p.clip || p.kind}  ${p.kind}  ${(p.end - p.start).toFixed(2)}s${src}${seg && seg.note ? '  · ' + seg.note : ''}`);
    return;
  }
  hideTip();
}, 60);

function onWheel(e) {
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    const [x] = localXY(e);
    zoomTo(view.pps * Math.exp(-e.deltaY * 0.0025), xToT(x));
  } else {
    const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    view.x0 += d / view.pps; clampView(); syncScroll(); dirty = true;
  }
}

// ---------------------------------------------------------------- marker popover
function openMarkerPop(m, x) {
  closeMarkerPop();
  setPlayhead(m.t, 'timeline');
  markerPop = h('div.marker-pop', { style: { left: clamp(x - 130, 4, W - 264) + 'px', top: '26px' } },
    h('div.hdr', h('span.tc', tc(m.t, fps())), h('span', { style: { flex: 1 } }), h('span.pill.' + (m.status === 'done' ? 'ok' : 'warn'), m.status || 'open')),
    m.frame_url ? h('img', { src: m.frame_url, alt: '' }) : null,
    h('textarea.inp', { rows: 3, placeholder: 'What should change here?', value: m.text || '', on: { keydown: (e) => e.stopPropagation(), change: (e) => emit('markerEdit', { id: m.id, text: e.target.value }) } }),
    h('div.acts',
      h('button.btn.ghost', { on: { click: () => { emit('markerEdit', { id: m.id, status: m.status === 'done' ? 'open' : 'done' }); closeMarkerPop(); } } }, m.status === 'done' ? 'Reopen' : 'Mark done'),
      h('button.btn', { on: { click: () => { emit('markerToClaude', m); closeMarkerPop(); } } }, 'Send to Claude'),
      h('button.btn.ghost', { html: icon('x', 12), title: 'Close', on: { click: closeMarkerPop } })));
  wrap.appendChild(markerPop);
  const ta = markerPop.querySelector('textarea');
  if (!m.text) ta.focus();
}
function closeMarkerPop() { if (markerPop) { const ta = markerPop.querySelector('textarea'); ta && ta.blur(); markerPop.remove(); markerPop = null; } }
export function openMarker(m) {
  if (tToX(m.t) < 0 || tToX(m.t) > W) { view.x0 = m.t - (W * 0.3) / view.pps; clampView(); syncScroll(); }
  openMarkerPop(m, tToX(m.t));
}

// ---------------------------------------------------------------- drag & drop from project / source
function dropData(e) {
  const types = Array.from(e.dataTransfer.types || []);
  return types.includes('application/x-cutroom-clip');
}
function trackAtY(y) { for (const tr of TRACKS) { const ty = trackY[tr.id]; if (y >= ty.y && y < ty.y + ty.h) return tr.id; } return 'V1'; }
function onDragOver(e) {
  if (!dropData(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  const [x, y] = localXY(e);
  const track = trackAtY(y) === 'V2' ? 'V2' : 'V1';
  let t = xToT(x);
  if (track === 'V1') { const b = nearestBoundary(t); t = b ? b.t : (S.tl ? S.tl.duration : 0); }
  dropHint = { t, track };
  dirty = true;
}
async function onDrop(e) {
  if (!dropData(e)) return;
  e.preventDefault();
  const hint = dropHint; dropHint = null; dirty = true;
  let d; try { d = JSON.parse(e.dataTransfer.getData('application/x-cutroom-clip')); } catch { return; }
  if (!S.plan) { emit('needPlan'); return; }
  const [x, y] = localXY(e);
  const t = xToT(x);
  const len = d.out !== undefined && d.out !== null ? d.out - d.in : Math.min(3, d.duration || 3);
  const inT = d.in ?? Math.max(0, (d.duration || 3) / 2 - len / 2);
  if (hint && hint.track === 'V2') {
    const p = pieceAt(t, 'V1');
    if (!p) return;
    const seg = segmentOf(p);
    const srcT = p.in !== null ? p.in + (t - p.start) * (p.speed || 1) : 0;
    const at = seg && seg.in !== undefined ? +(srcT - seg.in).toFixed(3) : +(t - segRange(p.sec, p.seg).start).toFixed(3);
    await op({ op: 'add_cutaway', seg: [p.sec, p.seg], cutaway: { clip: d.id, in: +inT.toFixed(3), at: Math.max(0, at), dur: +len.toFixed(3) } });
    return;
  }
  const type = d.talk ? 'talk' : 'broll';
  await op({ op: 'insert', at_t: +(hint ? hint.t : t).toFixed(3), segment: { type, clip: d.id, in: +inT.toFixed(3), out: +(inT + len).toFixed(3) } });
  void y;
}

// ---------------------------------------------------------------- public actions
export async function razor() {
  if (!S.tl) return;
  await op({ op: 'split', t: +S.playhead.toFixed(4) });
}
export async function freeze() {
  if (!S.tl || !pieceAt(S.playhead, 'V1')) return;
  await op({ op: 'freeze', t: +S.playhead.toFixed(4), dur: 1.0 });
}
export async function deleteSelection(ripple) {
  const segs = selectedSegs();
  const v2 = [...S.selection].map((id) => S.tl.v2.find((p) => p.id === id)).filter(Boolean);
  if (!segs.length && !v2.length) return;
  // cutaways first (by parent segment), then segments from the end so indices stay valid
  for (const c of v2) await deleteCutaway(c);
  for (const s of segs.reverse()) await op({ op: 'delete', seg: s, ripple: !!ripple });
}
async function deleteCutaway(p) {
  const v1 = pieceAt((p.start + p.end) / 2, 'V1');
  const parent = v1 ? segmentOf(v1) : null;
  if (!parent || !parent.cutaways) return;
  let best = -1, bd = Infinity;
  parent.cutaways.forEach((c, i) => { if (c.clip === p.clip || c.clip === p.clip_rel) { const d = Math.abs((+c.in || 0) - (p.in || 0)); if (d < bd) { bd = d; best = i; } } });
  if (best < 0 && parent.cutaways.length === 1) best = 0;
  if (best < 0) return;
  const next = parent.cutaways.filter((_, i) => i !== best);
  await op({ op: 'set', seg: [v1.sec, v1.seg], fields: { cutaways: next } });
}
export function toggleSnap() {
  S.snapping = !S.snapping; LS.set('snapping', S.snapping); emit('snap'); dirty = true;
}
export function revealPlayhead() {
  const x = tToX(S.playhead);
  if (x < 0 || x > W - 30) { view.x0 = S.playhead - W * 0.15 / view.pps; clampView(); syncScroll(); }
  dirty = true;
}

// ---------------------------------------------------------------- init
export function initTimeline(body, tools) {
  const tl = h('div.tl');
  heads = h('div.tl-heads');
  wrap = h('div.tl-canvas-wrap');
  canvas = h('canvas', { tabindex: 0, 'aria-label': 'Timeline' });
  tip = h('div.tl-tip', { hidden: true });
  hscroll = h('div.tl-hscroll'); hinner = h('div'); hscroll.appendChild(hinner);
  wrap.append(canvas, tip, hscroll);
  tl.append(heads, wrap);
  body.appendChild(tl);
  ctx = canvas.getContext('2d');

  // heads
  const totalH = layoutTracks();
  phTc = h('span.tc', '00:00:00:00');
  heads.append(h('div.tl-head.ruler', { style: { top: '0px', height: RULER + 'px' } }, phTc));
  heads.append(h('div.tl-head', { style: { top: RULER + 'px', height: BAND + 'px' } }, h('span', 'Sections')));
  for (const tr of TRACKS) { headEls[tr.id] = h('div.tl-head', { style: { top: trackY[tr.id].y + 'px', height: tr.h + 'px' } }, h('b', tr.id), h('span', tr.name)); heads.append(headEls[tr.id]); }
  void totalH;

  // empty state
  const empty = h('div.tl-empty.empty', { hidden: true },
    h('span', { html: icon('film') }),
    h('h4', 'No edit open'),
    h('p', 'Ask Claude for a cut, or start an empty plan and drag clips in from the Project panel.'),
    h('div.acts',
      h('button.btn', { on: { click: () => emit('askClaude', 'Make a 2-minute edit of ') } }, 'Ask Claude: make a 2-minute edit of…'),
      h('button.btn', { on: { click: () => emit('needPlan') } }, 'New plan')));
  wrap.appendChild(empty);
  const hint = h('div.tl-hint', { hidden: true },
    h('b', 'This plan is empty'),
    h('span', 'Drag clips here from Project, set In/Out in Source and press , to insert, or ask Claude to cut it.'));
  wrap.appendChild(hint);
  const first = h('div.first-run', { hidden: true });
  wrap.appendChild(first);
  const renderFirst = () => {
    const plans = (S.project && S.project.plans) || [];
    const fresh = !!S.project && !S.plan && !plans.length;
    first.hidden = !fresh;
    empty.hidden = !!S.plan || fresh;
    if (!fresh) return;
    const st = (S.project && S.project.status) || {};
    const o = S.overnight || {};
    const running = !!o.running;
    const understood = st.clips && st.tagged >= st.clips * 0.9;
    first.innerHTML = '';
    const big = h('button.tb-btn.primary.big', { disabled: running }, h('span', { html: icon(running ? 'spark' : 'moon', 15) }),
      running ? 'Understanding your library…' : understood ? 'Update my library' : 'Understand my library');
    big.onclick = async () => {
      try { await api.overnight('start'); toast('Started. Claude is working through your footage in the background.', 'ok', 3500); emit('showDock', 'overnight'); emit('overnightPoll'); }
      catch (e) { toast(`Couldn't start: ${e.message}`, 'error'); }
    };
    const n = st.clips || 0;
    first.append(...[
      h('h3', n ? `${n.toLocaleString()} clips, ${(+st.hours || 0).toFixed(1)} hours of footage` : 'Welcome to your edit room'),
      h('p', n ? 'Let Claude watch, transcribe and tag everything first. It runs in the background without slowing the computer down, and afterwards you can ask for anything by what happens in it.'
        : 'Point the project at your footage folder, then let Claude watch, transcribe and tag everything.'),
      big,
      running && o.stage ? h('div.sub', `Working on: ${o.stage}${o.eta_s ? ' · about ' + eta(o.eta_s) + ' left' : ''}`) : null,
      h('div.sub', 'Or ask Claude straight away:'),
      h('div.prompts', STARTERS.map((s) => h('button', { on: { click: () => emit('starter', s) } }, s.label)))].filter(Boolean));
  };
  on('project', renderFirst);
  on('overnight', renderFirst);

  // tools
  const snapBtn = h('button.ib', { title: 'Snapping (S)', html: icon('snap'), on: { click: toggleSnap } });
  const zl = h('input.tl-zoom', { type: 'range', min: 0, max: 1000, title: 'Zoom (= / −, ⌘ + wheel)' });
  const ppsFromSlider = (v) => 2 * Math.pow(300, v / 1000);
  const sliderFromPps = (p) => Math.round(Math.log(p / 2) / Math.log(300) * 1000);
  zl.addEventListener('input', () => zoomTo(ppsFromSlider(+zl.value), S.playhead));
  tools.append(
    h('button.ib', { title: 'Razor at playhead (C)', html: icon('razor'), on: { click: razor } }),
    h('button.ib', { title: 'Freeze frame (F)', html: icon('freeze'), on: { click: freeze } }),
    h('button.ib', { title: 'Add marker (M)', html: icon('marker'), on: { click: () => emit('addMarker') } }),
    h('span', { style: { width: '1px', height: '16px', background: 'var(--line-2)', margin: '0 4px' } }),
    snapBtn,
    h('button.ib', { title: 'Zoom out (−)', html: icon('zoomout'), on: { click: () => zoomBy(1 / 1.5) } }),
    zl,
    h('button.ib', { title: 'Zoom in (=)', html: icon('zoomin'), on: { click: () => zoomBy(1.5) } }),
    h('button.ib', { title: 'Zoom to fit (\\)', html: icon('fit'), on: { click: fit } }));
  const syncTools = () => { snapBtn.classList.toggle('on', S.snapping); zl.value = sliderFromPps(view.pps || 10); };
  on('snap', syncTools);

  const resize = () => {
    const r = wrap.getBoundingClientRect();
    DPR = window.devicePixelRatio || 1;
    W = Math.max(10, r.width); H = Math.max(10, r.height - 11);
    canvas.style.height = H + 'px';
    layoutTracks(H);
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    if (!view.pps && S.tl) fit();
    clampView(); syncScroll();
    dirty = true;
  };
  new ResizeObserver(resize).observe(wrap);

  canvas.addEventListener('pointerdown', onDown);
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerup', onUp);
  canvas.addEventListener('pointercancel', () => { drag = null; hideTip(); dirty = true; });
  canvas.addEventListener('pointerleave', () => { if (!drag) { hover = null; hideTip(); dirty = true; } });
  canvas.addEventListener('dblclick', onDbl);
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('dragover', onDragOver);
  canvas.addEventListener('dragleave', () => { dropHint = null; dirty = true; });
  canvas.addEventListener('drop', onDrop);
  hscroll.addEventListener('scroll', () => { if (syncing) return; view.x0 = hscroll.scrollLeft / view.pps; dirty = true; });

  let lastSlug = null;
  on('plan', ({ by, changed } = {}) => {
    renderFirst();
    hint.hidden = !(S.plan && S.tl && !S.tl.v1.length);
    if (S.tl && S.tl.error) hint.hidden = false, hint.firstChild.textContent = 'Timeline could not be resolved', hint.lastChild.textContent = S.tl.error;
    document.getElementById('tl-title').textContent = S.plan ? `${(S.plan.draft && S.plan.draft.title) || S.plan.slug}` : '';
    if (S.plan && (S.plan.slug !== lastSlug || !view.pps)) { lastSlug = S.plan.slug; if (W > 10) fit(); }
    if (changed && by !== 'open') {
      const t0 = performance.now();
      for (const id of changed) flashes.set(id, by === 'hand' ? t0 - 1600 : t0);
    }
    clampView(); syncScroll(); syncTools();
    dirty = true;
  });
  on('selection', () => { dirty = true; });
  on('markers', () => { dirty = true; });
  on('playhead', (from) => {
    phTc.textContent = tc(S.playhead, fps());
    if (S.playing && from !== 'timeline') {
      const x = tToX(S.playhead);
      if (x > W - 40 || x < 0) { view.x0 = S.playhead - 40 / view.pps; clampView(); syncScroll(); }
    }
    dirty = true;
  });
  on('layout', () => { dirty = true; });
  syncTools();

  const loop = () => {
    if (dirty) { dirty = false; draw(); }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}

export const _test = { view, hit: (x, y) => hit(x, y), tToX, xToT, trackY: () => trackY, undo };
