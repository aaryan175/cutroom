"""Resource governor: keeps long, unattended jobs from freezing or crashing the machine.

Every heavy thing cutroom does (proxy encodes, contact sheets, analysis workers, render segments,
match-cut decodes) takes a *slot* first; transcription takes the single ASR slot. Slots are flock'ed
files in <work>/.locks, so the CLI, the overnight run and the studio all share the same budget, and a
slot held by a process that dies is released by the kernel (no stale locks).

What it protects against, and how
  memory     the PHYSICAL FOOTPRINT of each child (macOS proc_pid_rusage ri_phys_footprint; Linux Pss)
             is checked every second; a child over its cap is stopped (SIGTERM, then SIGKILL).
             RSS is not used: compressed and swapped pages don't show in RSS but still exhaust RAM.
             When the system's free RAM drops below the emergency floor, the biggest (then newest)
             registered child of ANY cutroom process is stopped, at most one every 5 s.
             New work waits while free RAM is under `min_free_ram_gb`.
  parallel   heavy work runs in at most `heavy_slots` slots; ASR in exactly one, machine-wide.
             High load, thermal throttling, an active user (optional) and quiet hours (optional)
             throttle heavy work to one slot. Throttling never stops work completely.
  disk       nothing writes proxies/renders while the work volume has less than `min_free_disk_gb`
             free: the job pauses with a message and resumes when space is freed.
  priority   children run at nice `nice_level` (default 10) so the desktop stays responsive.

Use:
    gov = governor.for_cfg(cfg)
    with gov.slot("heavy", label="proxy C0001"):
        ...
    r = gov.run(["ffmpeg", ...], kind="heavy", timeout=600)   # r.killed = reason or None
    gov.check_self()                                           # raises MemoryGuardError over the cap
    gov.status()                                               # dict for UIs

Disable with CUTROOM_GOVERNOR=0 or "governor": false in cutroom.json (the single ASR slot stays).
"""
import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

KINDS = ("heavy", "asr", "light")
POLL_S = 2.0
MONITOR_S = 1.0
EMERGENCY_RATIO = 0.6          # stop a child when free RAM < this x min_free_ram_gb
KILL_GRACE_S = 3.0
KILL_SPACING_S = 5.0
USER_IDLE_S = 120.0
GB = 1024 ** 3


class MemoryGuardError(MemoryError):
    """The current process grew past its memory cap. Free memory / work in smaller chunks."""


class DiskFullError(OSError):
    pass


def _log(msg):
    print(f"  [governor] {msg}", file=sys.stderr, flush=True)


# ==================================================================================================
# measurements (no psutil needed)
# ==================================================================================================

class _RusageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups",
        "ri_child_interrupt_wkups", "ri_child_pageins", "ri_child_elapsed_abstime",
        "ri_diskio_bytesread", "ri_diskio_byteswritten")]


_libproc = None


def _proc_rusage():
    global _libproc
    if _libproc is None:
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
            fn = lib.proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RusageInfoV2)]
            fn.restype = ctypes.c_int
            _libproc = fn
        except (OSError, AttributeError):
            _libproc = False
    return _libproc or None


def footprint(pid=None) -> int | None:
    """Physical memory footprint of `pid` in bytes (None if the process is gone / unreadable).
    macOS: ri_phys_footprint (what jetsam acts on). Linux: Pss (smaps_rollup), else VmRSS."""
    pid = os.getpid() if pid is None else int(pid)
    if sys.platform == "darwin":
        fn = _proc_rusage()
        if fn is not None:
            info = _RusageInfoV2()
            if fn(pid, 2, ctypes.byref(info)) == 0:          # RUSAGE_INFO_V2
                return int(info.ri_phys_footprint)
            if ctypes.get_errno() == errno.ESRCH:
                return None
    elif sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            return None
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        v = r.stdout.strip()
        return int(v) * 1024 if v else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def total_ram() -> int:
    try:
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return 8 * GB


_free_cache = [0.0, None]
_free_lock = threading.Lock()


def free_ram(max_age=1.5) -> int | None:
    """Bytes the system can hand out without swapping hard. macOS: (free + speculative + inactive +
    purgeable) pages from vm_stat. Linux: MemAvailable. Cached for `max_age` seconds."""
    with _free_lock:
        if time.time() - _free_cache[0] < max_age and _free_cache[1] is not None:
            return _free_cache[1]
        val = None
        try:
            if sys.platform == "darwin":
                out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
                ps = int(re.search(r"page size of (\d+)", out).group(1))
                pages = {m.group(1).strip().lower(): int(m.group(2))
                         for m in re.finditer(r"^([^:\n]+):\s+(\d+)\.", out, re.M)}
                val = ps * sum(pages.get(k, 0) for k in ("pages free", "pages speculative", "pages inactive",
                                                          "pages purgeable"))
            else:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemAvailable:"):
                            val = int(line.split()[1]) * 1024
                            break
        except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
            val = None
        _free_cache[:] = [time.time(), val]
        return val


_slow_cache = {}


def _cached(key, max_age, fn):
    hit = _slow_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        v = fn()
    except Exception:  # noqa: BLE001 - a missing tool just means "unknown"
        v = None
    _slow_cache[key] = (time.time(), v)
    return v


def cpu_speed_limit() -> int | None:
    """macOS `pmset -g therm` CPU_Speed_Limit (100 = not throttled); None if unknown."""
    if sys.platform != "darwin":
        return None

    def read():
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        return int(m.group(1)) if m else None
    return _cached("therm", 30, read)


def user_idle_s() -> float | None:
    """Seconds since the last keyboard/mouse input (macOS IOHIDSystem HIDIdleTime); None if unknown."""
    if sys.platform != "darwin":
        return None

    def read():
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else None
    return _cached("idle", 10, read)


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return False


def _in_window(spec: str, now=None) -> bool:
    """'22:00-07:00' style local-time window (may wrap midnight)."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", spec or "")
    if not m:
        return False
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    t = time.localtime(now)
    x = t.tm_hour * 60 + t.tm_min
    return a <= x < b if a <= b else (x >= a or x < b)


# ==================================================================================================
# the governor
# ==================================================================================================

class RunResult(subprocess.CompletedProcess):
    """CompletedProcess plus `killed`: why the governor stopped the process (None = it wasn't)."""
    killed = None
    peak_gb = 0.0


class Governor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.work = Path(getattr(cfg, "work", "") or ".").resolve()
        self.lock_dir = self.work / ".locks"
        self.ram_total = total_ram()
        ncpu = os.cpu_count() or 2
        g = lambda k, d=0: getattr(cfg, k, d) or 0     # noqa: E731 - 0 / "" / None = automatic
        ram_gb = self.ram_total / GB
        self.enabled = bool(getattr(cfg, "governor", True)) and os.environ.get("CUTROOM_GOVERNOR", "1") != "0"
        self.max_load = float(g("max_load") or ncpu * 0.8)
        self.min_free_ram_gb = float(os.environ.get("CUTROOM_MIN_FREE_RAM_GB") or g("min_free_ram_gb")
                                     or max(2.0, 0.15 * ram_gb))
        self.max_proc_mem_gb = float(g("max_proc_mem_gb") or min(4.0, 0.25 * ram_gb))
        # in-process work (ASR model, match-cut matrices, the overnight stage process) gets more room
        self.self_mem_gb = max(self.max_proc_mem_gb, min(8.0, 0.35 * ram_gb))
        self.min_free_disk_gb = float(getattr(cfg, "min_free_disk_gb", 20.0) if getattr(cfg, "min_free_disk_gb", None)
                                      is not None else 20.0)
        self.heavy_slots = int(g("heavy_slots") or max(1, ncpu // 4))
        self.asr_slots = 1                                  # never more: parallel ASR has panicked machines
        self.nice = int(getattr(cfg, "nice_level", 10) if getattr(cfg, "nice_level", None) is not None else 10)
        self.pause_when_user_active = bool(getattr(cfg, "pause_when_user_active", False))
        self.quiet_hours = str(getattr(cfg, "quiet_hours", "") or "")
        self.poll_s = POLL_S
        self._tl = threading.local()
        self._mx = threading.Lock()
        self.last_wait = None                               # (kind, reason, since) while this process waits
        self._waits = {}

    # -- paths -------------------------------------------------------------------------------------
    def _p(self, *parts) -> Path:
        p = self.lock_dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # -- policy ------------------------------------------------------------------------------------
    def limits(self) -> dict:
        return {"max_load": round(self.max_load, 2), "min_free_ram_gb": round(self.min_free_ram_gb, 2),
                "emergency_free_ram_gb": round(self.min_free_ram_gb * EMERGENCY_RATIO, 2),
                "max_proc_mem_gb": round(self.max_proc_mem_gb, 2), "self_mem_gb": round(self.self_mem_gb, 2),
                "min_free_disk_gb": self.min_free_disk_gb, "heavy_slots": self.heavy_slots, "asr_slots": 1,
                "nice": self.nice, "pause_when_user_active": self.pause_when_user_active,
                "quiet_hours": self.quiet_hours, "enabled": self.enabled}

    def throttle_reasons(self) -> list:
        """Conditions that cut heavy work to one slot (never to zero)."""
        out = []
        try:
            la = os.getloadavg()[0]
            if la > self.max_load:
                out.append(f"load {la:.1f} > {self.max_load:.1f}")
        except OSError:
            pass
        lim = cpu_speed_limit()
        if lim is not None and lim < 80:
            out.append(f"thermal: CPU speed limit {lim}%")
        if self.pause_when_user_active:
            idle = user_idle_s()
            if idle is not None and idle < USER_IDLE_S:
                out.append(f"user active ({idle:.0f}s since input)")
        if self.quiet_hours and _in_window(self.quiet_hours):
            out.append(f"quiet hours {self.quiet_hours}")
        return out

    def allowed(self, kind) -> int:
        if kind == "asr":
            return 1
        if kind == "heavy":
            return 1 if self.throttle_reasons() else self.heavy_slots
        return 1 << 30

    def free_disk_gb(self, path=None) -> float:
        p = Path(path or self.work)
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(str(p)).free / GB

    def disk_ok(self, path=None) -> bool:
        return self.free_disk_gb(path) >= self.min_free_disk_gb

    def blockers(self, kind) -> list:
        """Hard conditions that make new work of `kind` wait."""
        out = []
        if kind in ("heavy", "asr"):
            fr = free_ram()
            if fr is not None and fr / GB < self.min_free_ram_gb:
                out.append(f"free RAM {fr / GB:.1f} GB < {self.min_free_ram_gb:.1f} GB")
        if kind == "heavy" and not self.disk_ok():
            out.append(f"disk {self.free_disk_gb():.1f} GB free < {self.min_free_disk_gb:g} GB on {self.work}")
        return out

    def wait_for_disk(self, path=None, poll=None):
        """Block (with a clear message) while the volume holding `path` is under the free-space floor.
        Call before writing proxies / renders. Never raises for low space; it waits."""
        if not self.enabled:
            return
        poll = poll or self.poll_s * 15
        t0, said = time.time(), 0.0
        while not self.disk_ok(path):
            if time.time() - said > 300:
                _log(f"PAUSED: only {self.free_disk_gb(path):.1f} GB free on {path or self.work} "
                     f"(floor {self.min_free_disk_gb:g} GB). Free some space (or lower min_free_disk_gb in "
                     f"cutroom.json); work resumes by itself.")
                said = time.time()
            self.last_wait = ("disk", f"disk below {self.min_free_disk_gb:g} GB", t0)
            time.sleep(poll)
        self.last_wait = None

    # -- slots -------------------------------------------------------------------------------------
    def _held(self) -> dict:
        h = getattr(self._tl, "held", None)
        if h is None:
            h = self._tl.held = {}
        return h

    def _lockfile(self, kind, i) -> Path:
        return self._p(f"{kind}.{i}.lock")

    @staticmethod
    def _try_flock(path) -> int | None:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return None
            raise

    def _busy(self, kind, i) -> bool:
        fd = self._try_flock(self._lockfile(kind, i))
        if fd is None:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return False

    def _waiter_file(self, kind) -> Path:
        return self._p("waiting", f"{kind}.{os.getpid()}.{threading.get_ident()}.json")

    def _first_in_line(self, kind, me: Path, since: float) -> bool:
        """FIFO between processes: a waiter that started earlier (and is alive) goes first."""
        for f in me.parent.glob(f"{kind}.*.json"):
            if f == me:
                continue
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not pid_alive(d.get("pid")):
                f.unlink(missing_ok=True)
                continue
            if d.get("pid") != os.getpid() and d.get("since", 1e18) < since:
                return False
        return True

    def _try_acquire(self, kind) -> int | None:
        """Atomically (gate lock) take one free slot file of `kind` if fewer than allowed are busy."""
        n_files = self.asr_slots if kind == "asr" else self.heavy_slots
        allowed = self.allowed(kind)
        gate = self._try_gate()
        if gate is None:
            return None
        try:
            busy = [self._busy(kind, i) for i in range(n_files)]
            if sum(busy) >= allowed:
                return None
            for i in range(n_files):
                if busy[i]:
                    continue
                fd = self._try_flock(self._lockfile(kind, i))
                if fd is not None:
                    return (i, fd)
            return None
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)
            os.close(gate)

    def _try_gate(self):
        path = self._p("gate.lock")
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        for _ in range(50):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                time.sleep(0.02)
        os.close(fd)
        return None

    @contextlib.contextmanager
    def slot(self, kind="heavy", label=""):
        """Hold one slot of `kind` ('heavy', 'asr', 'light') for the duration of the block.
        Blocks (polling every 2 s, logging why) until resources allow. Re-entrant per thread."""
        if kind not in KINDS:
            raise ValueError(f"unknown slot kind {kind!r}")
        held = self._held()
        if held.get(kind) or (kind == "light" and any(held.values())):
            held[kind] = held.get(kind, 0) + 1
            try:
                yield
            finally:
                held[kind] -= 1
            return
        if not self.enabled and kind != "asr":
            held[kind] = 1
            try:
                yield
            finally:
                held[kind] = 0
            return
        since = time.time()
        me = self._waiter_file(kind)
        me.write_text(json.dumps({"pid": os.getpid(), "kind": kind, "label": label, "since": since}))
        said, reason, got = 0.0, "", None
        try:
            while True:
                blocks = self.blockers(kind) if self.enabled else []
                if not blocks and self._first_in_line(kind, me, since):
                    if kind == "light":
                        got = (None, None)
                        break
                    got = self._try_acquire(kind)
                    if got is not None:
                        break
                    reason = (f"all {self.allowed(kind)} {kind} slot(s) busy"
                              + (f" (throttled: {'; '.join(self.throttle_reasons())})" if kind == "heavy"
                                 and self.allowed(kind) < self.heavy_slots else ""))
                elif blocks:
                    reason = "; ".join(blocks)
                else:
                    reason = "another process is first in line"
                self.last_wait = (kind, reason, since)
                if time.time() - said > 60:
                    if time.time() - since > 1:
                        _log(f"waiting for a {kind} slot{' for ' + label if label else ''}: {reason}")
                    said = time.time()
                me.write_text(json.dumps({"pid": os.getpid(), "kind": kind, "label": label, "since": since,
                                          "reason": reason}))
                time.sleep(self.poll_s)
        finally:
            me.unlink(missing_ok=True)
            self.last_wait = None
        i, fd = got
        if fd is not None:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps({"pid": os.getpid(), "kind": kind, "label": label, "index": i,
                                      "started": time.time()}).encode(), 0)
        held[kind] = 1
        try:
            yield
        finally:
            held[kind] = 0
            if fd is not None:
                try:
                    os.ftruncate(fd, 0)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def wait_resources(self, kind="heavy"):
        """Block while hard limits (RAM floor, disk floor) forbid new `kind` work; no slot is taken."""
        if not self.enabled:
            return
        t0, said = time.time(), 0.0
        while True:
            b = self.blockers(kind)
            if not b:
                self.last_wait = None
                return
            self.last_wait = (kind, "; ".join(b), t0)
            if time.time() - said > 60:
                _log(f"waiting: {'; '.join(b)}")
                said = time.time()
            time.sleep(self.poll_s)

    # -- memory --------------------------------------------------------------------------------------
    def check_self(self, cap_gb=None, what="this process"):
        """Raise MemoryGuardError if the current process footprint exceeds `cap_gb` (default self_mem_gb)."""
        if not self.enabled:
            return 0.0
        cap = float(cap_gb or self.self_mem_gb)
        fp = footprint()
        if fp is not None and fp / GB > cap:
            raise MemoryGuardError(f"{what} footprint {fp / GB:.2f} GB > cap {cap:.2f} GB")
        return (fp or 0) / GB

    def watch_self(self, cap_gb=None):
        """Return a cheap checker `chk()` for loops (raises MemoryGuardError over the cap)."""
        return lambda: self.check_self(cap_gb)

    # -- children ------------------------------------------------------------------------------------
    def _register(self, pid, cmd, cap):
        self._p("children", f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "parent": os.getpid(), "cmd": " ".join(map(str, cmd))[:300], "cap_gb": cap,
             "started": time.time()}))

    def _unregister(self, pid):
        self._p("children", f"{pid}.json").unlink(missing_ok=True)

    def children(self) -> list:
        out = []
        for f in self._p("children", "x").parent.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not pid_alive(d.get("pid")):
                f.unlink(missing_ok=True)
                continue
            fp = footprint(d["pid"])
            d["footprint_gb"] = round((fp or 0) / GB, 3)
            out.append(d)
        return out

    @staticmethod
    def _terminate(pid, grace=KILL_GRACE_S):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        end = time.time() + grace
        while time.time() < end:
            if not pid_alive(pid):
                return
            try:                                            # reap if it is our child
                if os.waitpid(pid, os.WNOHANG)[0]:
                    return
            except ChildProcessError:
                pass
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def emergency_check(self) -> str | None:
        """If system free RAM is under the emergency floor, stop the biggest (then newest) registered
        child of any cutroom process. Returns a description of what was done, or None."""
        fr = free_ram()
        floor = self.min_free_ram_gb * EMERGENCY_RATIO
        if fr is None or fr / GB >= floor:
            return None
        gate = self._p("killer.lock")
        fd = self._try_flock(gate)
        if fd is None:
            return None
        try:
            last = self._p("last_kill")
            if last.exists() and time.time() - last.stat().st_mtime < KILL_SPACING_S:
                return None
            kids = self.children()
            if not kids:
                return None
            victim = max(kids, key=lambda d: (d["footprint_gb"], d["started"]))
            reason = (f"system free RAM {fr / GB:.2f} GB < emergency floor {floor:.2f} GB; stopped the biggest "
                      f"child ({victim['footprint_gb']:.2f} GB)")
            self._p("killed", f"{victim['pid']}.txt").write_text(reason)
            last.write_text(str(victim["pid"]))
            _log(f"{reason}: pid {victim['pid']} {victim['cmd'][:120]}")
            threading.Thread(target=self._terminate, args=(int(victim["pid"]),), daemon=True).start()
            return reason
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def run(self, cmd, kind="heavy", timeout=None, mem_cap_gb=None, capture=True, text=True, check=False,
            label="", input=None, **popen_kw) -> RunResult:  # noqa: A002 - mirrors subprocess.run
        """Run `cmd` (optionally inside a slot of `kind`; None = no slot) at nice `nice_level`, watching
        its physical footprint every second. Over `mem_cap_gb` (default max_proc_mem_gb), or when the
        system runs out of RAM, it is stopped and `result.killed` says why. Timeout behaves like
        subprocess.run (TimeoutExpired)."""
        ctx = self.slot(kind, label or os.path.basename(str(cmd[0]))) if kind else contextlib.nullcontext()
        with ctx:
            return self._run(cmd, timeout, mem_cap_gb, capture, text, check, popen_kw, input)

    def _run(self, cmd, timeout, mem_cap_gb, capture, text, check, popen_kw, input=None):  # noqa: A002
        cap = float(mem_cap_gb or self.max_proc_mem_gb)
        if input is not None:
            popen_kw.setdefault("stdin", subprocess.PIPE)
        if capture:
            popen_kw.setdefault("stdout", subprocess.PIPE)
            popen_kw.setdefault("stderr", subprocess.PIPE)
        proc = subprocess.Popen([str(c) for c in cmd], text=text, **popen_kw)
        if self.enabled and self.nice:
            try:
                os.setpriority(os.PRIO_PROCESS, proc.pid, max(self.nice, os.getpriority(os.PRIO_PROCESS, proc.pid)))
            except OSError:
                pass
        self._register(proc.pid, cmd, cap)
        state = {"killed": None, "peak": 0.0}
        done = threading.Event()

        def monitor():
            first = True
            while not done.wait(0.25 if first else MONITOR_S):
                first = False
                if proc.poll() is not None:
                    return
                fp = footprint(proc.pid)
                if fp is not None:
                    state["peak"] = max(state["peak"], fp / GB)
                    if fp / GB > cap:
                        state["killed"] = f"memory footprint {fp / GB:.2f} GB > cap {cap:.2f} GB"
                        _log(f"stopping pid {proc.pid}: {state['killed']}: {' '.join(map(str, cmd))[:160]}")
                        self._terminate(proc.pid)
                        return
                self.emergency_check()

        th = threading.Thread(target=monitor, daemon=True) if self.enabled else None
        if th:
            th.start()
        try:
            try:
                out, err = proc.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate(proc.pid, grace=2.0)
                out, err = proc.communicate()
                raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err) from None
        finally:
            done.set()
            if th:
                th.join(timeout=5)
            self._unregister(proc.pid)
            km = self._p("killed", f"{proc.pid}.txt")
            if km.exists():
                state["killed"] = state["killed"] or km.read_text()
                km.unlink(missing_ok=True)
        r = RunResult(proc.args, proc.returncode, out, err)
        r.killed, r.peak_gb = state["killed"], round(state["peak"], 3)
        if check and r.returncode:
            raise subprocess.CalledProcessError(r.returncode, proc.args, out, err)
        return r

    def media_hook(self, cmd, capture=True, timeout=None):
        """media.run() calls this when a governor is active: nice + footprint watch, no slot (callers
        that do heavy work hold one)."""
        return self._run(cmd, timeout, None, capture, True, False, {})

    # -- status --------------------------------------------------------------------------------------
    def status(self) -> dict:
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            load = None
        fr = free_ram()
        slots = []
        for kind, n in (("heavy", self.heavy_slots), ("asr", 1)):
            for i in range(n):
                path = self._lockfile(kind, i)
                busy = self._busy(kind, i)
                d = {"kind": kind, "index": i, "busy": busy}
                if busy:
                    try:
                        info = json.loads(path.read_text() or "{}")
                    except (OSError, ValueError):
                        info = {}
                    d.update(pid=info.get("pid"), label=info.get("label"), started=info.get("started"),
                             age_s=round(time.time() - info["started"], 1) if info.get("started") else None)
                slots.append(d)
        waiting = []
        for f in self._p("waiting", "x").parent.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if pid_alive(d.get("pid")):
                d["waited_s"] = round(time.time() - d.get("since", time.time()), 1)
                waiting.append(d)
            else:
                f.unlink(missing_ok=True)
        try:
            disk = round(self.free_disk_gb(), 2)
        except OSError:
            disk = None
        return {"time": time.time(), "enabled": self.enabled, "load": load, "cpus": os.cpu_count(),
                "ram_total_gb": round(self.ram_total / GB, 2),
                "ram_free_gb": None if fr is None else round(fr / GB, 2),
                "disk_free_gb": disk, "work": str(self.work),
                "thermal_cpu_limit": cpu_speed_limit(), "user_idle_s": user_idle_s(),
                "limits": self.limits(), "allowed": {"heavy": self.allowed("heavy"), "asr": 1},
                "throttled": self.throttle_reasons(), "blocked": self.blockers("heavy"),
                "slots": slots, "waiting": waiting, "children": self.children()}


# ==================================================================================================
# per-project singletons + media hook
# ==================================================================================================

_GOVS = {}
_GLOCK = threading.Lock()
_ACTIVE = None


def active():
    """The governor whose media hook is installed in this process, or None."""
    return _ACTIVE


def for_cfg(cfg, activate=True) -> Governor:
    """The process-wide Governor for this project (created once). Activating it routes every
    media.run() (ffmpeg/ffprobe) through nice + footprint monitoring."""
    global _ACTIVE
    key = str(Path(getattr(cfg, "work", "") or ".").resolve())
    with _GLOCK:
        g = _GOVS.get(key)
        if g is None:
            g = _GOVS[key] = Governor(cfg)
        if activate and g.enabled:
            from . import media
            media.set_run_hook(g.media_hook)
            _ACTIVE = g
    return g


def reset():
    """Forget cached governors and remove the media hook (tests)."""
    global _ACTIVE
    from . import media
    with _GLOCK:
        _GOVS.clear()
        _ACTIVE = None
    media.set_run_hook(None)


def worker_init(nice=10, cap_gb=None):
    """ProcessPoolExecutor initializer: lower priority and remember the per-worker memory cap."""
    global _WORKER_CAP_GB
    try:
        if nice:
            os.nice(max(0, nice - os.nice(0)))
    except OSError:
        pass
    _WORKER_CAP_GB = cap_gb


_WORKER_CAP_GB = None


def worker_check(what="worker"):
    """Call inside pool workers: raises MemoryGuardError past the cap set by worker_init."""
    if _WORKER_CAP_GB:
        fp = footprint()
        if fp is not None and fp / GB > _WORKER_CAP_GB:
            raise MemoryGuardError(f"{what} footprint {fp / GB:.2f} GB > cap {_WORKER_CAP_GB:.2f} GB")
