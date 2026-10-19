"""FactoryRunner: the code-driven loop that takes orders off the bus and drives them through the phases.

Plain Python: no model is ever "on". Every 2 s while an order runs (15 s when idle) it:
  1. checks the gates (each makes the tick do nothing): PAUSED file, factory disabled, backoff.json active
     (login refused / limits), the disk floor, the AI login (cached 10 min);
  2. watches the running order: the order's total wall clock and a stop request kill the role's process group;
  3. with nothing running, picks the next order: interrupted orders first (resumed at their first phase without
     done.json; a phase interrupted twice blocks the order), then the highest-priority queued order whose
     not_before has passed (reject > revise/continue/approve > variants > request > extend, oldest first);
  4. writes the heartbeat factory/state.json from code (pid + time; a model never writes it).
Nothing ever relaunches in a loop: a refused login pauses everything until Resume; a usage limit retries at
most 3 times (30 min apart); a timeout or a failed role blocks the order until someone presses Retry.

One runner per project (flock on factory/.runner.lock): the studio starts one as a thread
(`factory.autostart`), or run it alone with `cutroom factory run` (SSH, overnight).
"""
import fcntl
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

from . import brain, bus, card, fconfig, lessons, phases

TICK_BUSY = 2.0
TICK_IDLE = 15.0
LISTENERS = []                     # in-process: fn(kind, data) for every bus line / state change (studio SSE)


def notify(kind, data):
    for fn in list(LISTENERS):
        try:
            fn(kind, data)
        except Exception:  # noqa: BLE001
            pass


def cli_bindir(cfg) -> str:
    """A directory with a `cutroom` command for the role agents (their prompts and allowed tools say
    `cutroom ...`). The interpreter's own bin dir when the console script lives there (venv install);
    otherwise a shim in <work>/factory/.bin that runs this interpreter's `-m cutroom.cli` (a studio started
    with `python -m cutroom.cli studio` has no `cutroom` next to sys.executable: roles got exit 127)."""
    import shutil
    bd = Path(sys.executable).parent
    if (bd / "cutroom").exists():
        return str(bd)
    shim_dir = fconfig.factory_dir(cfg) / ".bin"
    shim = shim_dir / "cutroom"
    body = f'#!/bin/sh\nexec "{sys.executable}" -m cutroom.cli "$@"\n'
    try:
        if not shim.exists() or shim.read_text() != body:
            shim_dir.mkdir(parents=True, exist_ok=True)
            shim.write_text(body)
            shim.chmod(0o755)
        return str(shim_dir)
    except OSError:
        found = shutil.which("cutroom")
        return str(Path(found).parent) if found else str(bd)


def emit(cfg, oid, state, **kw):
    ev = bus.append(cfg, oid, state, **kw)
    try:
        o = bus.load_order(cfg, oid)
        notify("factory", {"order": oid, "plan": o.get("plan"), "kind": o.get("kind"), **ev})
    except KeyError:
        notify("factory", ev)
    return ev


# ================================================================================================ files
def paused_path(cfg):
    return bus.root(cfg) / "PAUSED"


def backoff_path(cfg):
    return bus.root(cfg) / "backoff.json"


def state_path(cfg):
    return bus.root(cfg) / "state.json"


def is_paused(cfg):
    p = paused_path(cfg)
    try:
        return p.exists()
    except OSError:
        return True                         # fails closed


def backoff(cfg):
    d = bus.read_json(backoff_path(cfg))
    if isinstance(d, dict) and float(d.get("until_t") or 0) > time.time():
        return d
    return None


def set_backoff(cfg, why, seconds, order=None, detail=""):
    d = {"until": bus.now_iso(time.time() + seconds), "until_t": time.time() + seconds, "why": why,
         "set_at": bus.now_iso(), "order": order, "detail": str(detail)[:400]}
    bus.write_json(backoff_path(cfg), d)
    notify("factory_state", light(cfg))
    return d


def heartbeat(cfg):
    return bus.read_json(state_path(cfg), {}) or {}


def runner_alive(cfg):
    from ..governor import pid_alive
    hb = heartbeat(cfg)
    return bool(hb.get("runner_pid") and pid_alive(hb["runner_pid"]) and time.time() - float(hb.get("t") or 0) < 30)


# ================================================================================================ order context
class OrderCtx:
    def __init__(self, runner, oid):
        self.r = runner
        self.cfg = runner.cfg
        self.oid = oid
        self.order = bus.load_order(self.cfg, oid)
        self.dir = bus.order_dir(self.cfg, oid)
        self.fc = fconfig.load(self.cfg)
        self.outs = {}
        self.stop_ev = threading.Event()
        self.stop_reason = None
        self.phase = None
        self.pdir = self.dir
        self._last_prog = 0.0
        self._con = None
        st = bus.status(self.cfg, oid)
        self.started = st.get("started") or time.time()
        self._cost = float(st.get("cost_usd") or 0.0)

    @property
    def con(self):
        if self._con is None:
            from .. import db
            self._con = db.connect(self.cfg.db_path)
        return self._con

    def close(self):
        if self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001
                pass

    # -- budgets / walls ---------------------------------------------------------------------------
    def budget(self):
        b = dict(self.order.get("budget") or {})
        b.update(bus.read_json(self.dir / "budget.json", {}) or {})
        return b

    def wall(self, name):
        w = self.fc["walls"]
        size = ((self.outs.get("producer") or {}).get("team") or {}).get("size")
        if name in ("edit", "variants_edit") and size in ("M", "L"):
            return float(w.get("edit_large", 4500))
        key = {"render2": "render", "qa2": "qa", "render_final": "render", "qa_final": "qa", "variants_render": "render",
               "variants_edit": "edit", "fix": "fix"}.get(name, name)
        return float(w.get(key, 1800))

    def order_wall(self):
        b = self.budget()
        size = ((self.outs.get("producer") or {}).get("team") or {}).get("size")
        return float(b.get("wall_s_large") if size == "L" else b.get("wall_s") or 7200)

    def cost_total(self):
        return round(self._cost, 4)

    def role_runs(self):
        d = self.dir / "roles"
        return len([x for x in d.iterdir() if x.is_dir()]) if d.exists() else 0

    def stop_check(self):
        return self.stop_ev.is_set()

    def link_url(self, rel):
        return self.r.link_url(rel)

    def render_fn(self, plan_path, out, preview, log, on_line):
        return self.r.render_fn(self, plan_path, out, preview, log, on_line)

    def notify_markers(self):
        notify("markers", {"order": self.oid})

    # -- events -----------------------------------------------------------------------------------------
    def progress(self, text, force=True, **kw):
        now = time.time()
        if not force and now - self._last_prog < 2.0:
            return
        self._last_prog = now
        emit(self.cfg, self.oid, "progress", phase=self.phase, text=text, cost_total=self.cost_total(), **kw)

    def on_role_event(self, role, ev):
        if ev.get("type") == "tool":
            self.progress(f"{role}: {_step(ev)}", force=False, role=role)
        elif ev.get("type") == "status":
            self.progress(f"{role}: {ev.get('text')}", force=False, role=role)

    # -- one role run -------------------------------------------------------------------------------------
    def role(self, name, prompt, images=(), env=None, quiet=False, budgeted=True):
        from .. import agent as agentmod
        from ..governor import for_cfg
        from .roles import ROLES, schema
        rdef = ROLES[name]
        b = self.budget()
        if budgeted and self.role_runs() >= int(b.get("role_runs") or 6) and not quiet:
            raise phases.NeedsYou("budget reached: role runs", questions=_budget_q(self, "role runs"), kind="budget")
        n = len(list((self.dir / "roles").glob(f"{name}-*"))) + 1 if (self.dir / "roles").exists() else 1
        od = self.dir / "roles" / f"{name}-{n}"
        guard = _guard_snapshot(self) if rdef["readonly"] else None
        fc = self.fc
        model = (fc.get("models") or {}).get(rdef.get("model_key", name), "")
        ag_name = fc.get("agent") if fc.get("agent") in agentmod.AGENTS else None
        e = {"CUTROOM_WORK": str(self.cfg.work), "CUTROOM_FACTORY_ORDER_DIR": str(self.dir),
             "PATH": cli_bindir(self.cfg) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep
                     + os.environ.get("PATH", ""),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2]) + (os.pathsep + os.environ["PYTHONPATH"]
                                                                       if os.environ.get("PYTHONPATH") else "")}
        e.update(env or {})
        if not quiet:
            emit(self.cfg, self.oid, "progress", phase=self.phase, role=f"{name}-{n}", text=f"{name} started",
                 cost_total=self.cost_total())
        left = max(0.5, float(b.get("cost_usd") or 5.0) - self._cost)
        r = agentmod.run_role(name, prompt, cfg=self.cfg, agent=ag_name or agentmod.choose(self.cfg), tools=rdef["tools"],
                              sandbox=rdef["sandbox"], schema=schema(rdef["schema"]), images=[i for i in images if i],
                              timeout=self.wall(rdef["wall"]), model=model, cwd=self.cfg.work, env=e, out_dir=od,
                              max_budget_usd=left, gov=for_cfg(self.cfg),
                              on_event=(lambda ev: self.on_role_event(name, ev)) if not quiet else None,
                              stop_check=self.stop_check, price=fc.get("price_per_mtok"))
        self._cost += float(r.get("cost_usd") or 0)
        if not quiet:
            emit(self.cfg, self.oid, "progress", phase=self.phase, role=f"{name}-{n}",
                 text=f"{name} {'done' if r['ok'] else 'failed'} ({r['seconds']:.0f}s, ${r['cost_usd']:.2f})",
                 cost_total=self.cost_total(), role_cost=r["cost_usd"], role_agent=r["agent"])
        if guard is not None:
            changed = _guard_diff(guard, _guard_snapshot(self))
            if changed:
                raise phases.Blocked(f"{name} modified files it must not touch: {', '.join(changed[:4])}")
        if r["refused"]:
            set_backoff(self.cfg, "account-refused", fc["refusal_backoff_s"], self.oid, r["error"])
            raise phases.NeedsYou("account-refused", kind="login", questions=card.clean_questions([{
                "text": f"The AI login stopped working ({r['agent']}): run `{'codex login' if r['agent'] == 'codex' else 'claude'}`"
                        " once in a terminal, then answer 1.", "options": ["done: resume the factory", "stop this order"]}],
                kind="login"), text=r["error"])
        if r["limited"]:
            raise phases.Requeue(f"usage limit / overload: {r['error'][:200]}", fc["limit_backoff_s"])
        if r["stopped"]:
            raise phases.Stopped()
        if r["timeout"]:
            raise phases.Blocked(f"{name} hit its wall clock ({self.wall(rdef['wall']):.0f}s); nothing is relaunched")
        if not r["ok"]:
            raise phases.Blocked(f"{name} failed: {r['error']}")
        return r


def _step(ev):
    name = ev.get("name") or "tool"
    inp = str(ev.get("input") or "")
    inp = re.sub(r"/[^\s'\"]*/", "", inp)
    return f"{name} {inp[:90]}".strip()


def _budget_q(ctx, what):
    b = ctx.budget()
    return card.clean_questions([{"text": f"Budget reached ({what}: ${ctx.cost_total():.2f} spent, "
                                  f"{ctx.role_runs()} role runs). Continue?",
                                  "options": [f"yes, +${float(b.get('cost_usd') or 5):.0f} and 4 more runs", "stop here"]}],
                                kind="budget")


def _hash_file(p):
    import hashlib
    try:
        return hashlib.sha1(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def _guard_snapshot(ctx):
    """What a read-only role must not change: saved versions, the brain, this order's comments."""
    from .. import studio as studiomod
    out = {}
    pd = Path(ctx.cfg.work) / "plans"
    for e in pd.iterdir() if pd.exists() else []:
        if bus.VERSION_RE.match(e.name):
            out[f"plans/{e.name}"] = _hash_file(e)
    bd = bus.root(ctx.cfg) / "brain"
    for f in bd.rglob("*") if bd.exists() else []:
        if f.is_file() and not f.name.startswith("."):
            out[str(f.relative_to(bus.root(ctx.cfg)))] = _hash_file(f)
    ids = set(bus.order_comments(ctx.cfg, ctx.oid)[0])
    for m in studiomod.markers_load(ctx.cfg):
        if m.get("id") in ids:
            out[f"comment {m['id']}"] = json.dumps({k: m.get(k) for k in ("text", "t", "t_end", "drawing")}, sort_keys=True)
    return out


def _guard_diff(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


# ================================================================================================ the runner
class FactoryRunner:
    def __init__(self, cfg, render_fn=None, link_base=None, tick_busy=TICK_BUSY, tick_idle=TICK_IDLE):
        self.cfg = cfg
        self.tick_busy, self.tick_idle = tick_busy, tick_idle
        self._render_fn = render_fn
        self.link_base = link_base
        self.lock_fd = None
        self.thread = None
        self.stopping = threading.Event()
        self.wake_ev = threading.Event()
        self.driver = None
        self.ctx = None
        self.gate = None
        self._login = (0.0, None)
        self.mx = threading.Lock()
        bus.orders_dir(cfg)

    # -- ownership ------------------------------------------------------------------------------------
    def acquire(self):
        p = bus.root(self.cfg) / ".runner.lock"
        fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": bus.now_iso()}).encode(), 0)
        self.lock_fd = fd
        return True

    def release(self):
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self.lock_fd)
            self.lock_fd = None

    @property
    def owner(self):
        return self.lock_fd is not None

    # -- hooks ----------------------------------------------------------------------------------------------
    def link_url(self, rel):
        if not self.link_base:
            return None
        from urllib.parse import quote
        return self.link_base.rstrip("/") + "/media/render/" + "/".join(quote(x) for x in str(rel).split("/"))

    def render_fn(self, ctx, plan_path, out, preview, log, on_line):
        if self._render_fn:
            return self._render_fn(ctx, plan_path, out, preview, log, on_line)
        from .gates import run_cli
        args = ["render", plan_path, "--out", out] + (["--preview"] if preview else [])
        return run_cli(self.cfg, args, log, ctx.wall("render"), ctx.stop_check, on_line,
                       env={"CUTROOM_PROGRESS": "1", "CUTROOM_MAX_JOBS": os.environ.get("CUTROOM_MAX_JOBS", "2")},
                       pid_path=Path(log).with_suffix(".pid"))

    # -- loop ------------------------------------------------------------------------------------------------
    def start(self):
        if not self.acquire():
            return False
        self._recover_orphans()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="factory-runner")
        self.thread.start()
        return True

    def wake(self):
        self.wake_ev.set()

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop never dies
                sys.stderr.write(f"factory: tick: {traceback.format_exc()}\n")
            busy = self.driver is not None and self.driver.is_alive()
            self.wake_ev.wait(self.tick_busy if busy or self._has_queued() else self.tick_idle)
            self.wake_ev.clear()
        self._heartbeat()

    def shutdown(self, wait=10.0):
        self.stopping.set()
        self.wake()
        if self.ctx:
            self.ctx.stop_reason = "the runner is shutting down"
            self.ctx.stop_ev.set()
        if self.driver is not None:
            self.driver.join(timeout=wait)
        if self.thread is not None:
            self.thread.join(timeout=wait)
        self.release()

    def _has_queued(self):
        return any(o["status"]["state"] == "queued" and not o["status"].get("folded_into") for o in bus.list_orders(self.cfg))

    def _heartbeat(self):
        c = self.ctx
        bus.write_json(state_path(self.cfg), {"runner_pid": os.getpid(), "host": socket.gethostname(), "at": bus.now_iso(),
                                              "t": time.time(), "running_order": c.oid if c and self.driver and
                                              self.driver.is_alive() else None, "phase": c.phase if c else None,
                                              "gate": self.gate, "stopping": self.stopping.is_set()})

    def gates(self):
        fc = fconfig.load(self.cfg)
        if is_paused(self.cfg):
            return "paused"
        if not fc.get("enabled", True):
            return "disabled"
        bo = backoff(self.cfg)
        if bo:
            return f"backoff: {bo.get('why')}"
        try:
            from ..governor import for_cfg
            g = for_cfg(self.cfg, activate=False)
            if g.enabled and not g.disk_ok():
                return f"disk below {g.min_free_disk_gb:g} GB"
        except Exception:  # noqa: BLE001
            pass
        now = time.time()
        if now - self._login[0] > float(fc.get("login_check_ttl_s", 600)):
            self._login = (now, self._check_login(fc))
        if self._login[1]:
            return self._login[1]
        return None

    def _check_login(self, fc):
        from .. import agent as agentmod
        try:
            name = fc.get("agent") if fc.get("agent") in agentmod.AGENTS else agentmod.choose(self.cfg).name
            d = agentmod.detect(self.cfg)[name]
        except Exception:  # noqa: BLE001
            return None
        if not d.get("path"):
            return f"login: {name} is not installed"
        if d.get("logged_in") is False:
            return f"login: {name} is not logged in"
        return None

    def tick(self):
        with self.mx:
            if self.driver is not None and self.driver.is_alive():
                c = self.ctx
                if c and not c.stop_ev.is_set() and c.started and time.time() - c.started > c.order_wall():
                    c.stop_reason = f"order wall clock ({c.order_wall():.0f}s)"
                    c.stop_ev.set()
                self._heartbeat()
                return
            self.driver = None
            oid = self.pick()
            if not oid:                              # idle: no gate checks (no login probes while nothing waits)
                self.gate = None
                self._heartbeat()
                return
            self.gate = self.gates()
            if self.gate:
                self._heartbeat()
                return
            if oid:
                self.ctx = OrderCtx(self, oid)
                self.driver = threading.Thread(target=self.drive_safe, args=(self.ctx,), daemon=True, name=f"factory-{oid}")
                self.driver.start()
            self._heartbeat()

    def pick(self):
        orders = bus.list_orders(self.cfg)
        now = time.time()
        interrupted = [o for o in orders if o["status"]["state"] in ("picked_up", "running") and
                       not o["status"].get("folded_into")]
        if interrupted:
            return interrupted[0]["id"]
        cand = []
        for o in orders:
            st = o["status"]
            if st["state"] != "queued" or st.get("folded_into"):
                continue
            nb = bus.iso_to_t(st.get("not_before")) if st.get("not_before") else None
            if nb and nb > now:
                continue
            cand.append(o)
        cand.sort(key=lambda o: (o.get("priority", 4), o.get("created_t") or 0))
        return cand[0]["id"] if cand else None

    def _recover_orphans(self):
        """Role processes left over from a runner that died: stop them (their phase re-runs once)."""
        from ..governor import kill_group, pid_alive
        for o in bus.list_orders(self.cfg):
            if o["status"]["state"] not in ("picked_up", "running"):
                continue
            od = bus.order_dir(self.cfg, o["id"])
            for pf in list((od / "roles").glob("*/pid")) + list((od / "phases").glob("*/*.pid")):
                try:
                    pid = int(pf.read_text().strip())
                except (OSError, ValueError):
                    continue
                if pid_alive(pid) and _cmd_is_agent(pid):
                    kill_group(pid)

    # -- driving one order ----------------------------------------------------------------------------------
    def drive_safe(self, ctx):
        try:
            self.drive(ctx)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            (ctx.dir / "error.txt").write_text(tb)
            try:
                emit(self.cfg, ctx.oid, "blocked", phase=ctx.phase, why=f"internal error: {type(e).__name__}: {e}"[:280],
                     text="factory error (see error.txt); press Retry after fixing")
            except Exception:  # noqa: BLE001
                pass
        finally:
            ctx.close()
            notify("factory_state", light(self.cfg))
            self.wake()

    def drive(self, ctx):
        cfg, oid = self.cfg, ctx.oid
        st = bus.status(cfg, oid)
        if not st.get("picked"):
            emit(cfg, oid, "picked_up", text=f"{ctx.order['kind']} order picked up")
        else:
            emit(cfg, oid, "running", text="resuming after an interruption")
        ctx.started = bus.status(cfg, oid).get("started") or time.time()
        plan = phases.PLANS.get(ctx.order["kind"]) or phases.PLANS["revise"]
        final, finished = None, st["state"] in ("ready", "done", "needs_you") and st.get("version") is not None
        for i, name in enumerate(plan):
            pdir = ctx.dir / "phases" / f"{i:02d}_{name}"
            pdir.mkdir(parents=True, exist_ok=True)
            ctx.pdir = pdir
            if (pdir / "done.json").exists():
                ctx.outs[name] = bus.read_json(pdir / "out.json", {}) or {}
                continue
            inj = bus.read_json(pdir / "in.json", {}) or {}
            attempts = int(inj.get("attempts") or 0) + 1
            if attempts > 2:
                emit(cfg, oid, "blocked", phase=name, why=f"interrupted twice at {name}",
                     text="press Retry to run this phase again")
                return
            bus.write_json(pdir / "in.json", {"attempts": attempts, "started": bus.now_iso(), "phase": name})
            ctx.phase = name
            # budgets at the phase boundary
            b = ctx.budget()
            if name not in phases.SILENT:
                if ctx.cost_total() >= float(b.get("cost_usd") or 5.0):
                    self._needs(ctx, phases.NeedsYou("budget reached: cost", questions=_budget_q(ctx, "cost"), kind="budget"))
                    _reset_attempt(pdir)
                    return
                if time.time() - ctx.started > ctx.order_wall():
                    self._needs(ctx, phases.NeedsYou("budget reached: time", questions=_budget_q(ctx, "time"), kind="budget"))
                    _reset_attempt(pdir)
                    return
                emit(cfg, oid, "running", phase=name, text=phases.LABELS.get(name, name), cost_total=ctx.cost_total())
            try:
                out = phases.FUNCS[name](ctx)
            except phases.Skip as s:
                out = {"skipped": str(s)}
            except phases.NeedsYou as n:
                self._needs(ctx, n)
                _reset_attempt(pdir)
                return
            except phases.Requeue as rq:
                _reset_attempt(pdir)
                n = int(bus.status(cfg, oid).get("limit_attempts") or 0) + 1
                if n > int(ctx.fc.get("limit_retries", 3)):
                    emit(cfg, oid, "blocked", phase=name, why=f"usage limit {n - 1} times: {rq.why}"[:280],
                         text="press Retry later")
                else:
                    emit(cfg, oid, "queued", phase=name, text=f"usage limit: retry {n} of {ctx.fc.get('limit_retries', 3)} "
                         f"at {bus.now_iso(time.time() + rq.delay_s)}", not_before=bus.now_iso(time.time() + rq.delay_s),
                         limit_attempts=n, why=None)
                return
            except phases.Stopped:
                why = ctx.stop_reason or "stopped"
                if why.startswith("order wall"):
                    self._needs(ctx, phases.NeedsYou("budget reached: time", questions=_budget_q(ctx, "time"), kind="budget"))
                    _reset_attempt(pdir)
                    return
                if self.stopping.is_set() and why == "the runner is shutting down":
                    _reset_attempt(pdir)
                    return                      # resumes at this phase on the next start (not counted)
                emit(cfg, oid, "stopped", phase=name, text=why)
                _release_comments(cfg, oid)
                return
            except phases.Blocked as bl:
                emit(cfg, oid, "blocked", phase=name, why=str(bl)[:280], text="press Retry after fixing")
                return
            if ctx.stop_ev.is_set() and not (out or {}).get("skipped"):
                why = ctx.stop_reason or "stopped"
                if self.stopping.is_set() and why == "the runner is shutting down":
                    bus.write_json(pdir / "out.json", out)
                    bus.write_json(pdir / "done.json", {"at": bus.now_iso()})
                    return
                emit(cfg, oid, "stopped", phase=name, text=why)
                _release_comments(cfg, oid)
                return
            bus.write_json(pdir / "out.json", out)
            bus.write_json(pdir / "done.json", {"at": bus.now_iso(), "skipped": (out or {}).get("skipped")})
            ctx.outs[name] = out
            if isinstance(out, dict) and out.get("state"):
                final = out
                self._finish(ctx, out)
                finished = True
        if final is None:
            final = next((o for o in reversed(list(ctx.outs.values())) if isinstance(o, dict) and o.get("state")), None)
            if final is not None and not finished:
                self._finish(ctx, final)            # resumed after the last phase was written but not announced
            elif final is None:
                emit(cfg, oid, "done", text="finished")
        bus.write_json(ctx.dir / "result.json", {"outcome": (final or {}).get("state") or "done", "version":
                                                 (final or {}).get("version"), "why": (final or {}).get("why"),
                                                 "artifact": (final or {}).get("card"), "cost_usd": ctx.cost_total(),
                                                 "at": bus.now_iso()})

    def _finish(self, ctx, out):
        cfg, oid = self.cfg, ctx.oid
        kw = {"version": out.get("version"), "questions": out.get("questions") or [], "artifact": out.get("card"),
              "cost_total": ctx.cost_total()}
        if out["state"] == "needs_you":
            emit(cfg, oid, "needs_you", phase=ctx.phase, why=out.get("why") or "needs you", text=out.get("theme") or "", **kw)
        elif out["state"] == "done":
            emit(cfg, oid, "done", phase=ctx.phase, text=out.get("text") or "done", **kw)
        else:
            emit(cfg, oid, "ready", phase=ctx.phase, text=out.get("theme") or f"v{out.get('version')} ready", **kw)
        for f in bus.status(cfg, oid).get("folds") or []:            # mirror onto the folded sends
            emit(cfg, f, "done", text=f"folded into {oid}: v{out.get('version')} {out['state']}", version=out.get("version"))

    def _needs(self, ctx, n):
        emit(self.cfg, ctx.oid, "needs_you", phase=ctx.phase, why=n.why[:280], text=n.text or n.why,
             questions=card.clean_questions(n.questions, kind=n.kind) if n.questions and "line" not in n.questions[0]
             else n.questions, needs_kind=n.kind, cost_total=ctx.cost_total())


def _reset_attempt(pdir):
    """A phase that ends in needs_you / a limit retry was not 'interrupted': its next run starts at attempt 1."""
    try:
        (Path(pdir) / "in.json").unlink()
    except OSError:
        pass


def _cmd_is_agent(pid):
    import subprocess
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(re.search(r"claude|codex|fake_agent|cutroom", r.stdout))


def _release_comments(cfg, oid):
    """Comments of a stopped order go back to open (no reply)."""
    from .. import studio as studiomod
    ids = set(bus.order_comments(cfg, oid)[0])

    def f(data):
        for m in data:
            if m.get("id") in ids and (m.get("factory") or {}).get("order") == oid and m.get("status") == "working":
                m["status"] = "open"
                m.pop("factory", None)
    studiomod.markers_update(cfg, f)
    notify("markers", {"order": oid})


# ================================================================================================ actions (studio + CLI)
def submit(cfg, kind, plan, comment_ids=None, all_open=False, text="", attachments=(), by=None, **kw):
    """Send to factory. Comments must exist, belong to the plan and have words or a drawing."""
    from .. import studio as studiomod
    ids = list(comment_ids or [])
    if all_open or (kind in ("revise",) and not ids and not text):
        ids = [m["id"] for m in studiomod.markers_load(cfg) if (m.get("plan") == plan or not m.get("plan"))
               and m.get("status", "open") in ("open", "needs_you") and m.get("kind", "note") == "note"
               and (str(m.get("text") or "").strip() or m.get("drawing"))]
    if kind == "revise" and not ids and not str(text or "").strip():
        raise ValueError("nothing to send: no open comments with a note or a drawing, and no text")
    if kind == "reject" and not str(text or "").strip():
        raise ValueError("a REJECT needs a reason")
    res = bus.submit(cfg, kind, plan, comments=ids, text=text, attachments=attachments, by=by, **kw)
    o = res["order"]

    def mark(data):
        for m in data:
            if m.get("id") in set(ids):
                m["factory"] = {"order": res.get("folded_into") or o["id"], "queued": True}
                m["updated"] = time.time()
    if ids:
        studiomod.markers_update(cfg, mark)
        notify("markers", {"order": o["id"]})
    notify("factory", {"order": o["id"], "plan": plan, "kind": kind, "state": "queued", "folded_into": res.get("folded_into")})
    return res


def stop(cfg, oid, runner=None):
    st = bus.status(cfg, oid)
    if st["state"] in bus.TERMINAL:
        return st
    if runner and runner.ctx and runner.ctx.oid == oid and runner.driver and runner.driver.is_alive():
        runner.ctx.stop_reason = "stopped by you"
        runner.ctx.stop_ev.set()
        return {**st, "stopping": True}
    emit(cfg, oid, "stopped", text="stopped by you")
    _release_comments(cfg, oid)
    return bus.status(cfg, oid)


def retry(cfg, oid):
    st = bus.status(cfg, oid)
    if st["state"] != "blocked":
        raise ValueError("only a blocked order can be retried")
    d = bus.order_dir(cfg, oid) / "phases"
    for p in d.iterdir() if d.exists() else []:
        if (p / "in.json").exists() and not (p / "done.json").exists():
            (p / "in.json").unlink()
    emit(cfg, oid, "queued", text="retry (by you)", limit_attempts=0)
    return bus.status(cfg, oid)


def pause(cfg):
    bus.atomic_write(paused_path(cfg), bus.now_iso())
    notify("factory_state", light(cfg))


def resume(cfg):
    """Clear PAUSED and any backoff, and requeue the orders that stopped on a login refusal."""
    for p in (paused_path(cfg), backoff_path(cfg)):
        try:
            p.unlink()
        except OSError:
            pass
    n = 0
    for o in bus.list_orders(cfg):
        st = o["status"]
        if st["state"] == "needs_you" and st.get("why") == "account-refused":
            emit(cfg, o["id"], "queued", text="resumed after the login was fixed")
            n += 1
    notify("factory_state", light(cfg))
    return n


def answers(cfg, oid):
    return bus.read_json(bus.order_dir(cfg, oid) / "answers.json", {}) or {}


def answer(cfg, oid, qid, option, text="", runner=None):
    """Answer a numbered question: records a ruling and acts (budget -> continue the same order, login -> resume,
    taste -> a `continue` order for that note)."""
    from .. import studio as studiomod
    st = bus.status(cfg, oid)
    o = bus.load_order(cfg, oid)
    q = next((x for x in st.get("questions") or [] if x.get("id") == qid), None)
    if q is None:
        raise KeyError(f"no question {qid} on {oid}")
    try:
        k = int(option)
    except (TypeError, ValueError):
        raise ValueError("option must be the option number") from None
    if not 1 <= k <= len(q["options"]):
        raise ValueError(f"option must be 1-{len(q['options'])}")
    label = q["options"][k - 1]
    p = bus.order_dir(cfg, oid) / "answers.json"
    with bus.flocked(p.with_name(".answers.lock")):
        a = answers(cfg, oid)
        if qid in a:
            raise ValueError("already answered")
        a[qid] = {"option": k, "label": label, "text": str(text or "")[:2000], "at": bus.now_iso()}
        bus.write_json(p, a)
    kind = q.get("kind") or "taste"
    out = {"order": oid, "question": qid, "option": k, "label": label}
    if kind not in ("budget", "login"):
        rec = brain.add_decision(cfg, o["plan"], q.get("text"), f"{k} = {label}" + (f" ({text})" if text else ""),
                                 "answer", order=oid, question=qid)
        out["ruling"] = rec["id"]
    if kind == "budget":
        if k == 1:
            b = bus.read_json(bus.order_dir(cfg, oid) / "budget.json", {}) or {}
            base = {**(o.get("budget") or {}), **b}
            inc = float(fconfig.load(cfg)["budgets"]["cost_usd"])
            bus.write_json(bus.order_dir(cfg, oid) / "budget.json", {
                "cost_usd": float(base.get("cost_usd") or inc) + inc, "role_runs": int(base.get("role_runs") or 6) + 4,
                "wall_s": float(base.get("wall_s") or 7200) * 1.5, "wall_s_large": float(base.get("wall_s_large") or 14400) * 1.5})
            emit(cfg, oid, "queued", text="budget raised by you; continuing", questions=[])
        else:
            emit(cfg, oid, "stopped", text="stopped at the budget (by you)", questions=[])
            _release_comments(cfg, oid)
    elif kind == "login":
        if k == 1:
            out["requeued"] = resume(cfg)
        else:
            emit(cfg, oid, "stopped", text="stopped by you after a login problem")
            _release_comments(cfg, oid)
    elif kind == "extend":
        emit(cfg, oid, "done", text=f"feature request: {label}", questions=[])
    elif kind == "notpassed":
        if k == 2:
            out["order_created"] = submit(cfg, "continue", o["plan"], text=f"v{st.get('version')} did not pass: "
                                          f"{st.get('why')}. Another round. {text}".strip(), parent=oid,
                                          by={"user": "you", "via": "answer"})["order"]["id"]
    else:
        note_comment = None
        if q.get("note"):
            try:
                it = bus.read_json(next((bus.order_dir(cfg, oid) / "phases").glob("00_intake/out.json")), {}) or {}
                note_comment = next((n.get("comment") for n in it.get("notes") or [] if n["id"] == q["note"]), None)
            except StopIteration:
                note_comment = None
        if not (kind == "unresolved" and k == 2):
            c = submit(cfg, "continue", o["plan"], comment_ids=[note_comment] if note_comment else [],
                       text=f"Answer to \"{q.get('text')}\": {k} = {label}. {text}".strip(), parent=oid,
                       by={"user": "you", "via": "answer"})
            out["order_created"] = c["order"]["id"]
        if note_comment:
            studiomod.marker_set_status(cfg, note_comment, "open" if not (kind == "unresolved" and k == 2) else "done",
                                        reply=f"{k} = {label}" + (f": {text}" if text else ""), author="you")
    if runner:
        runner.wake()
    notify("factory", {"order": oid, "plan": o["plan"], "answered": qid})
    return out


CHANGE_VERB = re.compile(r"\b(but|change|swap|replace|remove|cut|trim|move|fix|shorter|longer|instead|except)\b", re.I)


def verdict(cfg, plan, version, disposition, text="", runner=None):
    """APPROVE (code only; conditional when the text asks for a change), REJECT (reason required), NOTES."""
    from .. import studio as studiomod
    disposition = str(disposition or "").upper()
    if disposition not in ("APPROVE", "REJECT", "NOTES"):
        raise ValueError("disposition must be APPROVE, REJECT or NOTES")
    v = int(version)
    name = f"{plan}_v{v}.json"
    if not (Path(cfg.work) / "plans" / name).exists():
        raise KeyError(f"no saved version {name}")
    key = f"{plan}_v{v}"
    text = str(text or "").strip()
    out = {"plan": plan, "version": v, "disposition": disposition}
    rec = {"verdict": disposition, "verdict_at": bus.now_iso(), "verdict_text": text}
    if disposition == "APPROVE":
        conditional = bool(text) and bool(CHANGE_VERB.search(text))
        rec.update(approved=True, conditional=conditional)
        phases.versions_index_update(cfg, key, **rec)
        phases.versions_meta_write(cfg, plan, name, {"approved": True, "approved_at": bus.now_iso(),
                                                     "conditional": conditional})
        pl = json.loads((Path(cfg.work) / "plans" / name).read_text())
        keys = [brain.seg_key(s) for sec in pl.get("sections") or [] for s in sec.get("segments") or []]
        brain.add_locks(cfg, plan, keys, why=f"v{v} approved", version=v)
        brain.add_decision(cfg, plan, f"v{v}", "approved" + (f" with a change: {text}" if conditional else ""), "verdict")
        out["approve_order"] = bus.create(cfg, "approve", plan, by={"user": "you", "via": "verdict"},
                                          extra={"base_file": name})["id"]
        if conditional:
            out["order"] = submit(cfg, "revise", plan, text=text, by={"user": "you", "via": "verdict"},
                                  extra={"base_file": name})["order"]["id"]
    elif disposition == "REJECT":
        if not text:
            raise ValueError("a REJECT needs a reason")
        phases.versions_index_update(cfg, key, **rec)
        lessons.harvest(cfg, [{"source": f"verdict:{key}", "kind": "reject", "text": text, "tags": []}])
        out["order"] = submit(cfg, "reject", plan, text=text, by={"user": "you", "via": "verdict"},
                              extra={"base_file": name, "rejected": key})["order"]["id"]
    else:
        phases.versions_index_update(cfg, key, **rec)
        if text:
            lessons.harvest(cfg, [{"source": f"verdict:{key}", "kind": "notes", "text": text, "tags": []}])
        ids = [m["id"] for m in studiomod.markers_load(cfg) if m.get("plan") == plan and m.get("status", "open") == "open"
               and m.get("kind", "note") == "note" and (str(m.get("text") or "").strip() or m.get("drawing"))]
        if not ids and not text:
            raise ValueError("NOTES needs text or open comments")
        out["order"] = submit(cfg, "revise", plan, comment_ids=ids, text=text, by={"user": "you", "via": "verdict"},
                              debounce_s=0)["order"]["id"]
    if runner:
        runner.wake()
    notify("factory_state", light(cfg))
    return out


def variant_verdict(cfg, oid, items, runner=None):
    """[{variant: k, verdict: KEEP|KILL|NOTES, note}] -> Keep folds that option into the next version."""
    o = bus.load_order(cfg, oid)
    if o["kind"] != "variants":
        raise ValueError("not a variants order")
    cd = bus.read_json(bus.order_dir(cfg, oid) / "card.json", {}) or {}
    byk = {v["k"]: v for v in cd.get("variants") or []}
    out = []
    p = bus.order_dir(cfg, oid) / "variant_verdicts.json"
    rec = bus.read_json(p, {}) or {}
    for it in items or []:
        k = int(it.get("variant"))
        vd = str(it.get("verdict") or "").upper()
        if k not in byk or vd not in ("KEEP", "KILL", "NOTES"):
            raise ValueError("each item needs a variant number and KEEP / KILL / NOTES")
        rec[str(k)] = {"verdict": vd, "note": it.get("note"), "at": bus.now_iso()}
        if vd == "KEEP":
            it0 = bus.read_json(next((bus.order_dir(cfg, oid) / "phases").glob("00_intake/out.json")), {}) or {}
            notes = it0.get("notes") or []
            ledger = [{"note": n["id"], "change": f"option {k}: {byk[k].get('name')}", "segments": byk[k].get("segments") or n["segments"],
                       "t_out": n.get("t"), "proof": "chosen by you from the options"} for n in notes]
            c = submit(cfg, "revise", o["plan"], comment_ids=[n["comment"] for n in notes if n.get("comment")],
                       text=it.get("note") or "", by={"user": "you", "via": "variants"}, debounce_s=0,
                       extra={"adopt": byk[k]["path"], "adopt_ledger": ledger})
            out.append({"variant": k, "verdict": vd, "order": c["order"]["id"]})
        else:
            out.append({"variant": k, "verdict": vd})
    bus.write_json(p, rec)
    if runner:
        runner.wake()
    return out


# ================================================================================================ views
def order_detail(cfg, oid) -> dict:
    o = bus.load_order(cfg, oid)
    d = bus.order_dir(cfg, oid)
    evs = bus.events(cfg, oid)
    st = bus.fold(evs)
    plan = phases.PLANS.get(o["kind"]) or phases.PLANS["revise"]
    ph = []
    for i, name in enumerate(plan):
        pd = d / "phases" / f"{i:02d}_{name}"
        done = bus.read_json(pd / "done.json")
        inj = bus.read_json(pd / "in.json")
        state = "done" if done else ("running" if inj and st["phase"] == name and st["state"] in bus.ACTIVE + ("running",)
                                     else "waiting")
        if done and done.get("skipped"):
            state = "skipped"
        if not done and st["phase"] == name and st["state"] in ("blocked", "needs_you", "stopped"):
            state = st["state"]
        ph.append({"name": name, "step": phases.STEP_OF.get(name, name), "label": phases.LABELS.get(name, name),
                   "state": state, "skipped": (done or {}).get("skipped"), "attempts": (inj or {}).get("attempts")})
    roles_ = []
    order_of = {"producer": 0, "forensics": 1, "extender": 2, "editor": 3, "variants": 3, "critic": 4, "fix": 5,
                "verify": 6, "lessons": 7}

    def _rk(p):                       # pipeline order (producer, editor, critic, fix, verify, lessons), then run number
        name, _, n = p.name.rpartition("-")
        return (order_of.get(name, 9), int(n) if n.isdigit() else 0, p.name)
    for rd in sorted((d / "roles").iterdir(), key=_rk) if (d / "roles").exists() else []:
        u = bus.read_json(rd / "usage.json", {}) or {}
        roles_.append({"role": rd.name, "agent": u.get("agent") or (bus.read_json(rd / "cmd.json", {}) or {}).get("agent"),
                       "ok": u.get("ok"), "cost_usd": u.get("cost_usd"), "seconds": u.get("seconds"),
                       "error": u.get("error"), "running": not (rd / "usage.json").exists(),
                       "last": _last_step(rd / "stream.jsonl")})
    ans = answers(cfg, oid)
    qs = [{**q, "answered": ans.get(q["id"])} for q in st.get("questions") or []]
    return {"order": o, "status": {**st, "questions": qs}, "events": evs[-300:], "phases": ph, "roles": roles_,
            "ledger": bus.read_json(d / "ledger.json", []) or [], "card": bus.read_json(d / "card.json"),
            "result": bus.read_json(d / "result.json"), "folds": st.get("folds")}


def _last_step(stream):
    try:
        with open(stream, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    from ..agent import _tool_summary
                    return f"{c.get('name')} {_tool_summary(c.get('name'), c.get('input'))}"[:120]
        if ev.get("type") == "item.completed":
            it = ev.get("item") or {}
            if it.get("type") == "command_execution":
                return f"Bash {str(it.get('command') or '')[:100]}"
    return None


def list_view(cfg, plan=None, state=None, limit=200) -> list:
    out = []
    for o in bus.list_orders(cfg, plan=plan, state=state)[-limit:]:
        st = o["status"]
        ans = answers(cfg, o["id"]) if st.get("questions") else {}
        out.append({"id": o["id"], "kind": o["kind"], "plan": o.get("plan"), "created": o.get("created"),
                    "created_t": o.get("created_t"), "n_comments": len(o.get("comments") or []), "text": (o.get("text") or "")[:200],
                    "state": st["state"], "phase": st["phase"], "step": phases.STEP_OF.get(st["phase"] or "", None),
                    "role": st.get("role"), "line": st["text"], "why": st["why"], "version": st.get("version"),
                    "cost_usd": st.get("cost_usd"), "at": st.get("at"), "t": st.get("t"), "folded_into": st.get("folded_into"),
                    "folds": st.get("folds"), "not_before": st.get("not_before"),
                    "open_questions": len([q for q in st.get("questions") or [] if q["id"] not in ans]),
                    "by": o.get("by"), "parent": o.get("parent")})
    return list(reversed(out))


def light(cfg) -> dict:
    """The header status light: idle (grey) / working (blue) / needs you (amber) / blocked (red)."""
    hb = heartbeat(cfg)
    alive = runner_alive(cfg)
    bo = backoff(cfg)
    paused = is_paused(cfg)
    rows = list_view(cfg, limit=400)
    running = [r for r in rows if r["state"] in ("picked_up", "running") and not r["folded_into"]]
    needs = [r for r in rows if r["state"] == "needs_you" or (r["state"] == "ready" and r["open_questions"])]
    blocked = [r for r in rows if r["state"] == "blocked" and time.time() - float(r.get("t") or 0) < 86400]
    vi = phases.versions_index(cfg)
    newest = {}
    for k, v in vi.items():
        if v.get("plan") and (v.get("version") or 0) > (newest.get(v["plan"], {}).get("version") or 0):
            newest[v["plan"]] = {**v, "key": k}
    review = [v for v in newest.values() if not v.get("verdict")]
    queued = [r for r in rows if r["state"] == "queued" and not r["folded_into"]]
    out = {"runner_alive": alive, "runner_pid": hb.get("runner_pid"), "gate": hb.get("gate"), "paused": paused,
           "backoff": bo, "running": running[:1], "needs": len(needs) + len(review), "needs_orders": [r["id"] for r in needs],
           "review": [v["key"] for v in review], "blocked": [r["id"] for r in blocked], "queued": len(queued),
           "at": bus.now_iso()}
    if paused or bo or blocked:
        out["light"] = "blocked"
        out["text"] = ("paused" if paused else
                       "the AI login stopped working: fix it, then press Resume" if bo and bo.get("why") == "account-refused"
                       else f"waiting: {bo.get('why')} until {bo.get('until')}" if bo else
                       f"blocked: {blocked[0]['why']}")
        out["login"] = bool(bo and bo.get("why") == "account-refused")
    elif running:
        r = running[0]
        out["light"] = "working"
        out["text"] = f"v{r['version'] or '?'} · {phases.LABELS.get(r['phase'] or '', r['phase'] or 'starting')}"
    elif needs or review:
        out["light"] = "needs_you"
        out["text"] = (f"{len(needs)} question{'s' if len(needs) != 1 else ''}" if needs else "") + \
                      (" · " if needs and review else "") + (f"{len(review)} to review" if review else "")
    elif queued:
        out["light"] = "working"
        out["text"] = f"{len(queued)} queued" + ("" if alive else " (no runner)")
    else:
        out["light"] = "idle"
        out["text"] = "idle"
    return out


def run_forever(cfg, link_base=None):
    """`cutroom factory run`: a foreground runner (flock: never alongside the studio's runner)."""
    r = FactoryRunner(cfg, link_base=link_base)
    if not r.acquire():
        raise SystemExit("another factory runner holds this project (the studio, or `cutroom factory run`)")
    r._recover_orphans()
    print(f"factory: runner {os.getpid()} on {cfg.work} (ctrl-c to stop; orders resume on the next start)", flush=True)

    def show(kind, data):
        if kind == "factory" and data.get("state"):
            print(f"  {data.get('at', '')} {data.get('order')} {data.get('state'):9s} {data.get('phase') or '':10s} "
                  f"{data.get('text') or ''}", flush=True)
    LISTENERS.append(show)
    try:
        r.loop()
    except KeyboardInterrupt:
        pass
    finally:
        r.shutdown()
        LISTENERS.remove(show)


def clear_finished_marker_flags(cfg):
    """Housekeeping: drop `factory.queued` flags from comments whose order ended without touching them."""
    from .. import studio as studiomod
    ended = {o["id"] for o in bus.list_orders(cfg) if o["status"]["state"] in bus.TERMINAL}

    def f(data):
        for m in data:
            fo = m.get("factory") or {}
            if fo.get("queued") and fo.get("order") in ended and m.get("status") == "open":
                m.pop("factory", None)
    studiomod.markers_update(cfg, f)


__all__ = ["FactoryRunner", "submit", "stop", "retry", "pause", "resume", "answer", "verdict", "variant_verdict",
           "order_detail", "list_view", "light", "run_forever", "LISTENERS"]
