// Resizable docked panels. Sizes are CSS variables on :root, persisted in localStorage.
import { $, $$, LS, clamp } from './util.js';
import { emit } from './state.js';

const W0 = window.innerWidth, H0 = window.innerHeight;
const DEFAULTS = {
  projW: Math.round(Math.min(340, Math.max(250, W0 * 0.2))), srcFrac: 0.5,
  claudeW: Math.round(Math.min(400, Math.max(290, W0 * 0.23))), topH: 0.5,
  dockH: Math.round(Math.min(230, Math.max(140, H0 * 0.19))),
};

let fitDock = () => {};
/** Tabs that need room (the Factory tab: stepper + team + questions) ask the dock for a comfortable height
 *  while they show: a temporary boost that is not saved; the dock goes back to your height on any other tab.
 *  Dragging the splitter while boosted keeps the boosted height as yours. */
export function dockFit(on) { fitDock(on); }

export function initLayout() {
  const L = { ...DEFAULTS, ...LS.get('layout', {}) };
  const root = document.documentElement.style;
  let boost = 0;
  fitDock = (on) => {
    const want = on ? Math.round(clamp(window.innerHeight * 0.42, 300, Math.min(460, window.innerHeight * 0.55))) : 0;
    const nb = want > L.dockH ? want : 0;
    if (nb !== boost) { boost = nb; apply(); emit('layout'); }
  };
  const apply = () => {
    root.setProperty('--projW', L.projW + 'px');
    root.setProperty('--srcFrac', L.srcFrac);
    root.setProperty('--claudeW', L.claudeW + 'px');
    root.setProperty('--topH', (L.topH * 100).toFixed(2) + '%');
    root.setProperty('--dockH', Math.max(L.dockH, boost) + 'px');
  };
  apply();

  for (const sp of $$('.splitter')) {
    sp.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      sp.setPointerCapture(e.pointerId);
      sp.classList.add('drag');
      const key = sp.dataset.split;
      const vertical = sp.classList.contains('v');
      document.body.classList.add('resizing', vertical ? 'resizing-v' : 'resizing-h');
      if (key === 'dockH' && boost) { L.dockH = Math.max(L.dockH, boost); boost = 0; }     // you take over the boosted height
      const x0 = e.clientX, y0 = e.clientY, start = { ...L };
      const topRow = $('#row-top'), colLeft = $('#col-left');
      const monW = $('#p-source').offsetWidth + $('#p-program').offsetWidth;
      const colH = colLeft.offsetHeight;
      const move = (ev) => {
        const dx = ev.clientX - x0, dy = ev.clientY - y0;
        if (key === 'projW') L.projW = clamp(start.projW + dx, 220, topRow.offsetWidth - 420);
        else if (key === 'srcFrac') L.srcFrac = clamp(start.srcFrac + dx / monW, 0.2, 0.8);
        else if (key === 'claudeW') L.claudeW = clamp(start.claudeW - dx, 280, window.innerWidth * 0.5);
        else if (key === 'topH') L.topH = clamp(start.topH + dy / colH, 0.22, 0.8);
        else if (key === 'dockH') L.dockH = clamp(start.dockH - dy, 90, window.innerHeight * 0.55);
        apply();
        emit('layout');
      };
      const up = () => {
        sp.classList.remove('drag');
        document.body.classList.remove('resizing', 'resizing-v', 'resizing-h');
        sp.removeEventListener('pointermove', move);
        sp.removeEventListener('pointerup', up);
        LS.set('layout', L);
        emit('layout');
      };
      sp.addEventListener('pointermove', move);
      sp.addEventListener('pointerup', up);
    });
    sp.addEventListener('dblclick', () => {
      const key = sp.dataset.split;
      L[key] = DEFAULTS[key];
      if (key === 'dockH') boost = 0;
      apply(); LS.set('layout', L); emit('layout');
    });
  }
  window.addEventListener('resize', () => emit('layout'));
}

/** Tab strip helper: returns setter; calls onChange(tab). Persists the choice under `key`. */
export function tabs(container, key, onChange) {
  const btns = $$('.ptab[data-tab]', container);
  const set = (name) => {
    for (const b of btns) b.classList.toggle('on', b.dataset.tab === name);
    LS.set('tab.' + key, name);
    onChange(name);
  };
  for (const b of btns) b.addEventListener('click', () => set(b.dataset.tab));
  return { set, initial: LS.get('tab.' + key, btns[0] && btns[0].dataset.tab) };
}
