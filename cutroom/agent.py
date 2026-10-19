"""The AI agent behind the studio chat and the overnight tagging: Claude Code or OpenAI Codex CLI.

Both run headless as subprocesses with the person's own existing login (subscription). No API keys.

    detect(cfg)  -> {"claude": {"path", "logged_in", "method"}, "codex": {...}}
    choose(cfg)  -> ClaudeAgent | CodexAgent   (Config.agent = "auto" | "claude" | "codex";
                                                env CUTROOM_AGENT overrides it)

An agent exposes the same interface to both callers:
    .name .label .path .logged_in .install_steps()
    .chat_cmd(sid, work, repo, images=())     argv for one chat turn; the prompt goes to stdin
    .new_state() / .parse_event(line, state)  one stdout line -> list of normalized events
                                              {type: session|text|tool|tool_result|status|stage|done|error}
    .session_gone(stderr)                     the resumed session no longer exists
    .tag(cfg, prompt, images, gov, timeout)   one overnight tagging call -> {ok, text, usage, limit, error, ...}

Binaries: CUTROOM_CLAUDE_BIN (or CUTROOM_CLAUDE) and CUTROOM_CODEX_BIN override the PATH lookup. The real
executable is always used, never a shell alias (subprocesses cannot see aliases anyway).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

AGENTS = ("claude", "codex")
LIMIT_PAT = re.compile(r"usage limit|limit reached|rate limit|too many requests|quota|out of (extra )?usage|"
                       r"credit balance is too low|hit your limit", re.I)
CHAT_TOOLS = ["Bash(cutroom:*)", "Read", "Write", "Edit", "Glob", "Grep"]
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60.0


def _short(v, n=160):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _exe(p):
    p = Path(os.path.expanduser(str(p)))
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None


def _find(name, env_keys, extra=()):
    for k in env_keys:
        v = os.environ.get(k)
        if v:
            return _exe(v) or (shutil.which(v) if os.sep not in v else None)
    p = shutil.which(name)
    if p:
        return p
    home = Path.home()
    for c in extra:
        c = home / c if not str(c).startswith("/") else Path(c)
        if _exe(c):
            return str(c)
    return None


def find_claude():
    """Absolute path of the real `claude` executable, or None."""
    return _find("claude", ("CUTROOM_CLAUDE_BIN", "CUTROOM_CLAUDE"),
                 (".local/bin/claude", ".claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                  ".npm-global/bin/claude"))


def find_codex():
    """Absolute path of the real `codex` executable, or None."""
    return _find("codex", ("CUTROOM_CODEX_BIN",),
                 ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", ".npm-global/bin/codex", ".local/bin/codex"))


def _cached(key, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)
    return val


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def _quiet_env():
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(k, None)
    return env


def claude_login(path):
    """(logged_in, method). `claude auth status` prints JSON {loggedIn, authMethod}; older versions
    without that command give (None, "")."""
    def check():
        try:
            r = subprocess.run([path, "auth", "status", "--json"], capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL, env=_quiet_env())
        except (OSError, subprocess.SubprocessError):
            return None, ""
        try:
            d = json.loads(r.stdout or "")
        except ValueError:
            return None, ""
        if isinstance(d, dict) and "loggedIn" in d:
            return bool(d["loggedIn"]), str(d.get("authMethod") or "")
        return None, ""
    return _cached(("claude", path), check)


def codex_login(path):
    """(logged_in, method) from `codex login status` ("Not logged in" or "Logged in using ...")."""
    def check():
        try:
            r = subprocess.run([path, "login", "status"], capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None, ""
        out = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())
        if re.search(r"not logged in", out, re.I):
            return False, ""
        m = re.search(r"logged in(?: using| with| via)?\s*(.*)", out, re.I)
        if m:
            return True, m.group(1)[:60].strip()
        return (True, "") if r.returncode == 0 and out else (None, "")
    return _cached(("codex", path), check)


def detect(cfg=None, check_login=True):
    """Which agents are installed and logged in. logged_in is True/False, or None when unknown."""
    out = {}
    for name, finder, login in (("claude", find_claude, claude_login), ("codex", find_codex, codex_login)):
        path = finder()
        li, method = (login(path) if (path and check_login) else (None if path else False, ""))
        out[name] = {"path": path, "installed": bool(path), "logged_in": li, "method": method}
    return out


def wanted(cfg=None):
    w = (os.environ.get("CUTROOM_AGENT") or getattr(cfg, "agent", "") or "auto").strip().lower()
    return w if w in AGENTS or w == "auto" else "auto"


def choose(cfg=None):
    """The agent to use. Explicit choice wins (even if not logged in: the caller shows login steps).
    auto: the one that is logged in; Claude Code when both are (or when neither can be told apart)."""
    w = wanted(cfg)
    if w == "claude":
        return ClaudeAgent(find_claude())
    if w == "codex":
        return CodexAgent(find_codex())
    cl, cx = find_claude(), find_codex()
    if cl and not cx:
        return ClaudeAgent(cl)
    if cx and not cl:
        return CodexAgent(cx)
    if not cl and not cx:
        return ClaudeAgent(None)
    if codex_login(cx)[0] is not True:          # codex not logged in: Claude Code
        return ClaudeAgent(cl)
    if claude_login(cl)[0] is False:            # only codex is logged in
        return CodexAgent(cx)
    return ClaudeAgent(cl)


def get(name):
    return CodexAgent(find_codex()) if name == "codex" else ClaudeAgent(find_claude())


def summary(cfg=None):
    """For the studio's /api/project: {name, label, logged_in, path, setting, available, installed}."""
    d = detect(cfg)
    a = choose(cfg)
    return {"name": a.name, "label": a.label, "path": a.path, "logged_in": d[a.name]["logged_in"],
            "setting": wanted(cfg), "available": [n for n in AGENTS if d[n]["installed"]],
            "logged_in_agents": [n for n in AGENTS if d[n]["logged_in"]], "agents": d}


# ==================================================================================================
class _Agent:
    name = ""
    label = ""

    def __init__(self, path):
        self.path = path

    @property
    def logged_in(self):
        return None

    def new_state(self):
        return {}

    def _decode(self, line):
        if isinstance(line, dict):
            return line
        line = (line or "").strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        return ev if isinstance(ev, dict) else None

    @staticmethod
    def _result(r, t0):
        return {"ok": False, "text": "", "cost_usd": 0.0, "usage": {}, "limit": False, "error": None,
                "seconds": round(time.time() - t0, 1)}


def _tool_summary(name, inp):
    inp = inp or {}
    if not isinstance(inp, dict):
        return _short(inp)
    for k in ("command", "file_path", "pattern", "path", "url", "query", "description"):
        if inp.get(k):
            extra = f" in {inp['path']}" if k == "pattern" and inp.get("path") else ""
            return _short(str(inp[k]) + extra)
    return _short(inp)


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return str(content or "")


# ================================================================================================== Claude Code
class ClaudeAgent(_Agent):
    name = "claude"
    label = "Claude Code"

    @property
    def logged_in(self):
        return claude_login(self.path)[0] if self.path else False

    def install_steps(self):
        if not self.path:
            return ("Claude Code is not installed (no `claude` on PATH).\n"
                    "1. Install it: `npm install -g @anthropic-ai/claude-code` (or the native installer from "
                    "https://claude.com/claude-code).\n"
                    "2. Run `claude` once in a terminal and sign in.\n"
                    "3. Restart `cutroom studio`. To use a binary somewhere else, set CUTROOM_CLAUDE_BIN=/path/to/claude.")
        return ("Claude Code is installed but not signed in.\n"
                "1. Run `claude` once in a terminal and sign in with your Claude account.\n"
                "2. Send your message again.")

    def chat_cmd(self, sid, work, repo, images=()):
        c = [self.path, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--permission-mode", "acceptEdits"]
        if sid:
            c += ["--resume", sid]
        c += ["--allowedTools", *CHAT_TOOLS, "--add-dir", str(repo)]
        # Isolate the editor from the person's global Claude setup (user CLAUDE.md, hooks, MCP servers,
        # broad permissions): smaller context per turn, and only the tools listed above. The login
        # (subscription) still works. CUTROOM_CLAUDE_ISOLATE=0 turns this off.
        if os.environ.get("CUTROOM_CLAUDE_ISOLATE", "1") != "0":
            c += ["--setting-sources", "project,local", "--strict-mcp-config"]
        if os.environ.get("CUTROOM_CLAUDE_MODEL"):
            c += ["--model", os.environ["CUTROOM_CLAUDE_MODEL"]]
        return c       # images: Claude opens the paths listed in the prompt with its Read tool

    def session_gone(self, stderr):
        return bool(re.search(r"no conversation found|session.*not found", stderr or "", re.I))

    def new_state(self):
        return {"delta": False, "any_text": False}

    def parse_event(self, line, state):
        ev = self._decode(line)
        if ev is None or ev.get("parent_tool_use_id"):          # sub-agent chatter: not shown in the panel
            return []
        out = []
        t = ev.get("type")

        def text(s):
            state["any_text"] = True
            out.append({"type": "text", "text": s})
        if t == "system" and ev.get("subtype") == "init":
            if ev.get("session_id"):
                out.append({"type": "session", "session_id": ev["session_id"], "model": ev.get("model")})
        elif t == "stream_event":
            e = ev.get("event") or {}
            if e.get("type") == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    state["delta"] = True
                    text(d["text"])
            elif e.get("type") == "content_block_start":
                cb = e.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    out.append({"type": "stage", "stage": f"tool: {cb.get('name')}"})
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and not state["delta"] and c.get("text"):
                    text(c["text"])
                elif c.get("type") == "tool_use":
                    out.append({"type": "tool", "id": c.get("id"), "name": c.get("name"),
                                "input": _tool_summary(c.get("name"), c.get("input"))})
            state["delta"] = False
        elif t == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    out.append({"type": "tool_result", "id": c.get("tool_use_id"), "is_error": bool(c.get("is_error")),
                                "text": _short(_result_text(c.get("content")), 300)})
        elif t == "result":
            if not state["any_text"] and ev.get("result"):
                text(str(ev["result"]))
            d = {"type": "done", "session_id": ev.get("session_id"),
                 "cost_usd": ev.get("total_cost_usd", ev.get("cost_usd")), "duration_ms": ev.get("duration_ms"),
                 "num_turns": ev.get("num_turns"), "is_error": bool(ev.get("is_error")), "subtype": ev.get("subtype"),
                 "usage": ev.get("usage")}
            if d["is_error"]:
                d["error"] = _short(ev.get("result") or ev.get("subtype") or "error", 500)
            out.append(d)
        return out

    # -- overnight tagging: Read tool only, the prompt lists the sheet paths --------------------------
    def tag(self, cfg, prompt, images, gov, timeout):
        t0 = time.time()
        if not self.path:
            return {"ok": False, "error": "claude not found on PATH", "limit": False, "missing": True}
        cmd = [self.path, "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
               "--allowedTools", "Read", "--tools", "Read", "--strict-mcp-config", "--no-session-persistence",
               "--setting-sources", "project,local"]   # skip the user's global setup: far smaller context per call
        if getattr(cfg, "tag_model", ""):
            cmd += ["--model", cfg.tag_model]
        try:
            r = gov.run(cmd, kind="light", timeout=timeout, mem_cap_gb=max(2.0, gov.max_proc_mem_gb), input=prompt,
                        cwd=str(cfg.work), label="claude tagging")
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "claude call timed out", "limit": False, "seconds": time.time() - t0}
        out = self._result(r, t0)
        out["text"] = r.stdout or ""
        env = None
        try:
            env = json.loads(r.stdout or "")
        except ValueError:
            pass
        if isinstance(env, dict):
            out["text"] = env.get("result") if isinstance(env.get("result"), str) else (r.stdout or "")
            out["cost_usd"] = float(env.get("total_cost_usd") or env.get("cost_usd") or 0)
            out["usage"] = env.get("usage") or {}
            if env.get("is_error") or env.get("subtype") not in (None, "success"):
                out["error"] = (out["text"] or env.get("subtype") or "error")[:500]
        if r.killed:
            out["error"] = f"stopped by governor: {r.killed}"
        elif r.returncode and not out["error"]:
            out["error"] = f"claude exited {r.returncode}: {(r.stderr or r.stdout or '')[-500:]}"
        blob = " ".join(str(x) for x in (out["error"] or "", r.stderr or "", out["text"] if out["error"] else ""))
        out["limit"] = bool(out["error"] and LIMIT_PAT.search(blob))
        out["ok"] = not out["error"]
        return out


# ================================================================================================== Codex CLI
RECONNECT_PAT = re.compile(r"reconnecting|retrying|stream disconnected|re-?connect", re.I)


class CodexAgent(_Agent):
    name = "codex"
    label = "Codex"
    max_images = 8

    @property
    def logged_in(self):
        return codex_login(self.path)[0] if self.path else False

    def install_steps(self):
        if not self.path:
            return ("OpenAI Codex CLI is not installed (no `codex` on PATH).\n"
                    "1. Install it: `npm install -g @openai/codex` (or `brew install codex`).\n"
                    "2. Run `codex login` once in a terminal and sign in with your ChatGPT account.\n"
                    "3. Restart `cutroom studio`. To use a binary somewhere else, set CUTROOM_CODEX_BIN=/path/to/codex.")
        return ("Codex is installed but not signed in.\n"
                "1. Run `codex login` once in a terminal and sign in with your ChatGPT account.\n"
                "2. Send your message again.")

    def _common(self, ephemeral=False):
        c = ["--json", "--skip-git-repo-check"]
        if ephemeral:
            c.append("--ephemeral")
        if os.environ.get("CUTROOM_CODEX_ISOLATE", "1") != "0":
            c.append("--ignore-user-config")    # like Claude's isolation: no user MCP servers / profiles; login still works
        return c

    def chat_cmd(self, sid, work, repo, images=()):
        imgs = []
        for p in list(images)[:self.max_images]:
            imgs += ["-i", str(p)]              # -i takes several values: always followed by a flag below
        where = ["-s", "workspace-write", "-C", str(work), "--add-dir", str(repo)]
        if sid:
            c = [self.path, "exec", *where, "resume", sid, *imgs, *self._common()]
        else:
            c = [self.path, "exec", *imgs, *where, *self._common()]
        if os.environ.get("CUTROOM_CODEX_MODEL"):
            c += ["-m", os.environ["CUTROOM_CODEX_MODEL"]]
        return c + ["-"]

    def session_gone(self, stderr):
        return bool(re.search(r"no (saved |recorded )?(session|conversation|thread|rollout)s? found|"
                              r"(session|conversation|thread|rollout)[^\n]{0,40}not found", stderr or "", re.I))

    def new_state(self):
        return {"thread_id": None, "msgs": {}, "order": [], "tools": set(), "results": set(), "last_error": None,
                "error_sent": False, "any_text": False}

    def parse_event(self, line, state):
        ev = self._decode(line)
        if ev is None:
            return []
        out = []
        t = ev.get("type")
        if t == "thread.started":
            tid = ev.get("thread_id") or ev.get("id")
            if tid:
                state["thread_id"] = tid
                out.append({"type": "session", "session_id": tid, "model": ev.get("model")})
        elif t == "turn.started":
            out.append({"type": "stage", "stage": "thinking"})
        elif t in ("item.started", "item.updated", "item.completed"):
            out += self._item(t.split(".")[1], ev.get("item") or {}, state)
        elif t == "turn.completed":
            out.append({"type": "done", "session_id": state.get("thread_id"), "usage": ev.get("usage") or {},
                        "cost_usd": None, "is_error": False, "subtype": "success"})
        elif t == "turn.failed":
            err = ev.get("error") or {}
            msg = (err.get("message") if isinstance(err, dict) else str(err)) or state.get("last_error") or "turn failed"
            if not state["error_sent"]:
                state["error_sent"] = True
                out.append({"type": "error", "error": _short(msg, 1500)})
        elif t == "error":
            msg = str(ev.get("message") or ev.get("error") or "error")
            if RECONNECT_PAT.search(msg):
                out.append({"type": "status", "text": _short(msg, 300)})
            else:
                state["last_error"] = msg               # turn.failed (or the process exit) reports it once
        return out

    def _item(self, phase, item, state):
        out = []
        iid = item.get("id") or f"item{len(state['order'])}"
        kind = item.get("type") or item.get("item_type") or ""
        if kind in ("agent_message", "assistant_message"):
            txt = item.get("text")
            if not isinstance(txt, str):
                return out
            prev = state["msgs"].get(iid)
            if prev is None:
                state["order"].append(iid)
                if state["any_text"] and txt:
                    out.append({"type": "text", "text": "\n\n"})
                prev = ""
            if txt.startswith(prev):
                new = txt[len(prev):]
            else:
                new = txt[len(prev):] if len(txt) > len(prev) else ""
            if len(txt) >= len(prev):
                state["msgs"][iid] = txt
            if new:
                state["any_text"] = True
                out.append({"type": "text", "text": new})
        elif kind in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
            if iid not in state["tools"]:
                state["tools"].add(iid)
                name, inp = self._tool_desc(kind, item)
                out.append({"type": "tool", "id": iid, "name": name, "input": inp})
                out.append({"type": "stage", "stage": f"tool: {name}"})
            if phase == "completed" and iid not in state["results"]:
                state["results"].add(iid)
                out.append(self._tool_result(kind, iid, item))
        elif kind == "reasoning":
            out.append({"type": "stage", "stage": "thinking"})
        elif kind == "error":
            msg = item.get("message") or item.get("text")
            if msg:
                out.append({"type": "status", "text": _short(msg, 300)})
        return out                                  # todo_list and unknown types: ignored

    @staticmethod
    def _tool_desc(kind, item):
        if kind == "command_execution":
            return "Bash", _short(item.get("command") or "", 160)
        if kind == "file_change":
            files = [Path(str(c.get("path"))).name for c in (item.get("changes") or [])
                     if isinstance(c, dict) and c.get("path")]
            return "Edit", _short(", ".join(files) or "files", 160)
        if kind == "mcp_tool_call":
            return f"mcp:{item.get('server', '')}.{item.get('tool', '')}".rstrip("."), _short(item.get("arguments") or "", 160)
        return "WebSearch", _short(item.get("query") or "", 160)

    @staticmethod
    def _tool_result(kind, iid, item):
        status = str(item.get("status") or "")
        if kind == "command_execution":
            code = item.get("exit_code")
            bad = (code not in (None, 0)) or status in ("failed", "declined")
            text = _short(item.get("aggregated_output") or "", 300)
            text = (text + " " if text else "") + (f"(exit {code})" if code is not None else f"({status or 'done'})")
            return {"type": "tool_result", "id": iid, "is_error": bad, "text": text}
        bad = status in ("failed", "declined", "error")
        return {"type": "tool_result", "id": iid, "is_error": bad, "text": status or "done"}

    # -- overnight tagging: sheets attached as images, final message forced into a JSON schema -------
    def tag(self, cfg, prompt, images, gov, timeout):
        t0 = time.time()
        if not self.path:
            return {"ok": False, "error": "codex not found on PATH", "limit": False, "missing": True}
        d = Path(cfg.work) / "overnight" / "codex"
        d.mkdir(parents=True, exist_ok=True)
        tag_id = uuid.uuid4().hex[:10]
        schema_f, last_f = d / f"schema-{tag_id}.json", d / f"last-{tag_id}.txt"
        schema_f.write_text(json.dumps(tag_schema()))
        cmd = [self.path, "exec"]
        for p in list(images)[:self.max_images]:
            cmd += ["-i", str(p)]
        cmd += [*self._common(ephemeral=True), "-s", "read-only", "-C", str(cfg.work),
                "--output-schema", str(schema_f), "-o", str(last_f)]
        model = getattr(cfg, "codex_model", "")
        if model:
            cmd += ["-m", model]
        cmd.append("-")
        try:
            try:
                r = gov.run(cmd, kind="light", timeout=timeout, mem_cap_gb=max(2.0, gov.max_proc_mem_gb), input=prompt,
                            cwd=str(cfg.work), label="codex tagging")
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "codex call timed out", "limit": False, "seconds": time.time() - t0}
            out = self._result(r, t0)
            st = self.new_state()
            usage, texts, failed = {}, [], None
            for line in (r.stdout or "").splitlines():
                ev = self._decode(line)
                if not ev:
                    continue
                if ev.get("type") == "turn.completed":
                    for k, v in (ev.get("usage") or {}).items():
                        if isinstance(v, (int, float)):
                            usage[k] = usage.get(k, 0) + v
                for e in self.parse_event(ev, st):
                    if e["type"] == "error":
                        failed = e["error"]
                item = ev.get("item") or {}
                if ev.get("type") == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                    texts.append(item["text"])
            try:
                last = last_f.read_text()
            except OSError:
                last = ""
            out["text"] = last.strip() or (texts[-1] if texts else "")
            out["usage"] = usage
            if r.killed:
                out["error"] = f"stopped by governor: {r.killed}"
            elif failed or (st["last_error"] and not out["text"]):
                out["error"] = _short(failed or st["last_error"], 500)
            elif r.returncode and not out["text"]:
                out["error"] = f"codex exited {r.returncode}: {(r.stderr or '')[-500:]}"
            blob = " ".join(str(x) for x in (out["error"] or "", st["last_error"] or "", r.stderr or ""))
            out["limit"] = bool(out["error"] and LIMIT_PAT.search(blob))
            out["ok"] = not out["error"]
            return out
        finally:
            for f in (schema_f, last_f):
                f.unlink(missing_ok=True)


def tag_schema():
    """JSON schema for the tagging reply (strict structured output: every key present, null = unknown)."""
    from . import db
    s = lambda: {"type": ["string", "null"]}                                    # noqa: E731
    lst = lambda: {"type": ["array", "null"], "items": {"type": "string"}}     # noqa: E731
    props = {"id": {"type": "string"}}
    for k in db.TAG_FIELDS:
        if k in ("creator_visible", "talking_to_camera", "usable"):
            props[k] = {"type": ["boolean", "null"]}
        elif k in ("actions", "mood", "topics"):
            props[k] = lst()
        elif k == "quality":
            props[k] = {"type": ["integer", "null"]}
        elif k == "best":
            props[k] = {"type": ["array", "null"], "items": {
                "type": "object", "additionalProperties": False, "required": ["t", "why"],
                "properties": {"t": {"type": "number"}, "why": {"type": "string"}}}}
        elif k == "avoid":
            props[k] = {"type": ["array", "null"], "items": {
                "type": "object", "additionalProperties": False, "required": ["t0", "t1", "why"],
                "properties": {"t0": {"type": "number"}, "t1": {"type": "number"}, "why": {"type": "string"}}}}
        else:
            props[k] = s()
    item = {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
    return {"type": "object", "additionalProperties": False, "required": ["tags"],
            "properties": {"tags": {"type": "array", "items": item}}}
