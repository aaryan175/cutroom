// The phone review page (/review): everything waiting on you, each with a player, its card and buttons.
// Standalone (no editor modules): questions as numbered buttons, versions with APPROVE / REJECT / NOTES,
// option tiles with KEEP / KILL. Refreshes on the studio's SSE `factory_state` / `factory` events.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const mm = (t) => { t = Math.max(0, +t || 0); return `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`; };
async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.status);
  return d;
}
function toast(msg) {
  const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}
let data = null, busy = false;

function qHtml(q) {
  return `<div class="card q" data-order="${esc(q.order)}" data-q="${esc(q.id)}"><div class="sub">${esc(q.plan)}${q.version ? ' · v' + q.version : ''}</div>
    <h3>${esc(q.text)}</h3><div class="opts">${q.options.map((o, i) => `<button class="opt" data-k="${i + 1}"><b>${i + 1}</b>${esc(o)}</button>`).join('')}</div></div>`;
}
function vHtml(v) {
  const cd = v.card;
  const rows = cd ? cd.rows.map((r) => `<div class="row"><div class="said">“${esc((r.words || '').slice(0, 160))}”</div><div class="chg">${esc(r.change || '')}</div>
    ${r.t !== null && r.t !== undefined ? `<button class="t" data-seek="${r.t}">${mm(r.t)}</button>` : ''}</div>`).join('') : '';
  const badges = cd ? `<span class="badge ${cd.badges.machine_qa === 'pass' ? 'ok' : 'bad'}">QA ${esc(cd.badges.machine_qa)}</span><span class="badge ${cd.badges.critic === 'hard' ? 'bad' : cd.badges.critic === 'pass' ? 'ok' : ''}">critic ${esc(cd.badges.critic || 'skipped')}</span>` : '';
  const verdict = v.verdict ? `<span class="badge ${v.verdict === 'APPROVE' ? 'ok' : ''}">${esc(v.verdict.toLowerCase())}</span>` : '';
  return `<div class="card" data-plan="${esc(v.plan)}" data-v="${v.version}"><h3>${esc(v.plan)} · v${v.version}</h3>
    <div class="sub">${esc((v.theme || '').replace(/^v\d+:\s*/, ''))}</div><div style="margin-top:6px">${badges}${verdict}${v.passed === false ? '<span class="badge bad">not passed</span>' : ''}</div>
    ${v.render_url ? `<video controls playsinline preload="metadata" src="${esc(v.render_url)}"></video>` : '<div class="sub">no render</div>'}
    ${rows}
    ${cd && cd.checked ? `<details><summary>What was checked</summary>${cd.checked.map((c) => `<div class="chk">• ${esc(c)}</div>`).join('')}${(cd.warns || []).map((c) => `<div class="chk">⚠ ${esc(c)}</div>`).join('')}</details>` : ''}
    <textarea placeholder="Notes / why (REJECT needs a reason)"></textarea>
    <div class="btns"><button class="b approve" data-d="APPROVE">APPROVE</button><button class="b reject" data-d="REJECT">REJECT</button><button class="b notes" data-d="NOTES">NOTES</button></div></div>`;
}
function varHtml(o) {
  return `<div class="card" data-order="${esc(o.order)}"><h3>${esc(o.plan)} · options</h3><div class="sub">${esc(o.theme || '')}</div><div class="tiles">
    ${o.variants.map((x) => `<div class="tile" data-k="${x.k}"><div class="sub"><b>${x.k}. ${esc(x.name || '')}</b> ${esc(x.summary || '')}</div>
      ${x.url ? `<video controls playsinline preload="metadata" src="${esc(x.url)}"></video>` : `<div class="sub">${esc(x.error || 'no render')}</div>`}
      <div class="btns"><button class="b approve" data-vv="KEEP" ${x.verdict ? 'disabled' : ''}>KEEP</button><button class="b reject" data-vv="KILL" ${x.verdict ? 'disabled' : ''}>KILL</button></div>
      ${x.verdict ? `<div class="sub">${esc(x.verdict.toLowerCase())}</div>` : ''}</div>`).join('')}</div></div>`;
}
async function load() {
  try { data = await (await fetch('/api/review')).json(); } catch { $('#main').innerHTML = '<div class="empty">Can\'t reach the studio.</div>'; return; }
  const L = data.light || {};
  $('#light').className = 'light ' + (L.light || 'idle');
  $('#light').lastElementChild.textContent = L.text || 'idle';
  const open = data.items.filter((v) => !v.verdict), judged = data.items.filter((v) => v.verdict);
  let html = '';
  if (data.questions.length) html += `<h2>Questions (${data.questions.length})</h2>` + data.questions.map(qHtml).join('');
  if (open.length) html += `<h2>To review (${open.length})</h2>` + open.map(vHtml).join('');
  if (data.variants.length) html += '<h2>Options</h2>' + data.variants.map(varHtml).join('');
  if (judged.length) html += '<h2>Done</h2>' + judged.map(vHtml).join('');
  $('#main').innerHTML = html || '<div class="empty">Nothing is waiting on you.</div>';
}
document.addEventListener('click', async (e) => {
  const seek = e.target.closest('[data-seek]');
  if (seek) { const v = seek.closest('.card').querySelector('video'); if (v) { v.currentTime = +seek.dataset.seek; v.play().catch(() => {}); } return; }
  if (busy) return;
  const opt = e.target.closest('.opt');
  const vb = e.target.closest('[data-d]');
  const vv = e.target.closest('[data-vv]');
  try {
    busy = true;
    if (opt) {
      const c = opt.closest('.card');
      await post('/api/factory/answer', { order: c.dataset.order, question: c.dataset.q, option: +opt.dataset.k });
      toast(`Answered ${opt.dataset.k}`);
    } else if (vb) {
      const c = vb.closest('.card');
      const text = c.querySelector('textarea').value.trim();
      if (vb.dataset.d === 'REJECT' && !text) { toast('Say why first: a REJECT needs a reason'); c.querySelector('textarea').focus(); return; }
      await post('/api/factory/verdict', { plan: c.dataset.plan, version: +c.dataset.v, disposition: vb.dataset.d, text });
      toast(vb.dataset.d === 'APPROVE' ? 'Approved: a full-quality render follows. Nothing is published.' : vb.dataset.d === 'REJECT' ? 'Rejected: the factory reworks it' : 'Notes sent to the factory');
    } else if (vv) {
      const c = vv.closest('[data-order]'), t = vv.closest('.tile');
      await post(`/api/factory/variants/${c.dataset.order}`, { items: [{ variant: +t.dataset.k, verdict: vv.dataset.vv }] });
      toast(vv.dataset.vv === 'KEEP' ? 'Kept: it goes into the next version' : 'Killed');
    } else return;
    await load();
  } catch (err) { toast(err.message); } finally { busy = false; }
});
load();
try {
  const es = new EventSource('/api/events');
  let t = 0;
  const again = () => { clearTimeout(t); t = setTimeout(load, 600); };
  es.addEventListener('factory', (e) => { const d = JSON.parse(e.data); if (['ready', 'needs_you', 'done', 'blocked'].includes(d.state)) again(); });
  es.addEventListener('factory_state', (e) => { const L = JSON.parse(e.data); $('#light').className = 'light ' + (L.light || 'idle'); $('#light').lastElementChild.textContent = L.text || 'idle'; });
} catch { /* no SSE */ }
