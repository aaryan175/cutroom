"""agent.py: detect/choose between Claude Code and Codex, and the Codex event normalisation
(fake binaries only; no real agent is ever called)."""
import json
import sys

import pytest

from cutroom import agent
from cutroom.config import Config


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CUTROOM_AGENT", "CUTROOM_CLAUDE_BIN", "CUTROOM_CLAUDE", "CUTROOM_CODEX_BIN"):
        monkeypatch.delenv(k, raising=False)
    agent.clear_cache()
    yield
    agent.clear_cache()


def _bin(tmp_path, name, out, rc=0):
    p = tmp_path / name
    p.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({out!r})\nsys.exit({rc})\n")
    p.chmod(0o755)
    return str(p)


def _setup(tmp_path, monkeypatch, claude=None, codex=None):
    """claude/codex: None = not installed, True/False = logged in or not."""
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", str(tmp_path / "none-claude"))
    monkeypatch.setenv("CUTROOM_CODEX_BIN", str(tmp_path / "none-codex"))
    if claude is not None:
        monkeypatch.setenv("CUTROOM_CLAUDE_BIN", _bin(tmp_path, "claude", json.dumps(
            {"loggedIn": claude, "authMethod": "claude.ai" if claude else "none"})))
    if codex is not None:
        monkeypatch.setenv("CUTROOM_CODEX_BIN", _bin(tmp_path, "codex", "Logged in using ChatGPT\n" if codex
                                                     else "Not logged in\n", 0 if codex else 1))


@pytest.mark.parametrize("claude,codex,setting,want", [
    (True, True, "auto", "claude"),
    (True, False, "auto", "claude"),
    (False, True, "auto", "codex"),
    (None, True, "auto", "codex"),
    (None, False, "auto", "codex"),
    (True, None, "auto", "claude"),
    (None, None, "auto", "claude"),
    (True, True, "codex", "codex"),
    (None, True, "claude", "claude"),
])
def test_choose(tmp_path, monkeypatch, claude, codex, setting, want):
    _setup(tmp_path, monkeypatch, claude, codex)
    a = agent.choose(Config(agent=setting))
    assert a.name == want
    assert bool(a.path) == ({"claude": claude, "codex": codex}[want] is not None)


def test_detect_and_env_override(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, claude=True, codex=False)
    d = agent.detect(Config())
    assert d["claude"]["logged_in"] is True and d["claude"]["method"] == "claude.ai"
    assert d["codex"]["installed"] and d["codex"]["logged_in"] is False
    monkeypatch.setenv("CUTROOM_AGENT", "codex")
    assert agent.choose(Config(agent="claude")).name == "codex"
    s = agent.summary(Config())
    assert s["name"] == "codex" and s["available"] == ["claude", "codex"] and s["logged_in"] is False


def test_codex_old_style_claude_status(tmp_path, monkeypatch):
    # an older claude without `auth status` (prints something that is not JSON): login unknown, still preferred
    monkeypatch.setenv("CUTROOM_CLAUDE_BIN", _bin(tmp_path, "claude", "error: unknown command 'auth'", 1))
    monkeypatch.setenv("CUTROOM_CODEX_BIN", _bin(tmp_path, "codex", "Logged in using ChatGPT\n"))
    assert agent.detect()["claude"]["logged_in"] is None
    assert agent.choose(Config()).name == "claude"


def _feed(lines):
    a = agent.CodexAgent("/bin/codex")
    st = a.new_state()
    out = []
    for ln in lines:
        out += a.parse_event(json.dumps(ln) if isinstance(ln, dict) else ln, st)
    return out


def test_codex_parse_tolerant():
    evs = _feed([
        "not json at all", "", {"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
        {"type": "something.new", "x": 1}, {"type": "item.completed"}, {"type": "item.completed", "item": {}},
        {"type": "item.updated", "item": {"id": "m1", "type": "agent_message", "text": "Hel"}},
        {"type": "item.updated", "item": {"id": "m1", "type": "agent_message", "text": "Hello"}},
        {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "Hello"}},
        {"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "ls",
                                            "aggregated_output": "boom", "exit_code": 2, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "m2", "type": "agent_message", "text": "Done."}},
        {"type": "item.completed", "item": {"id": "w1", "type": "web_search", "query": "cats"}},
        {"type": "turn.completed"},
    ])
    kinds = [e["type"] for e in evs if e["type"] not in ("stage",)]
    assert kinds == ["session", "text", "text", "tool", "tool_result", "text", "text", "tool", "tool_result", "done"]
    texts = "".join(e["text"] for e in evs if e["type"] == "text")
    assert texts == "Hello\n\nDone."
    r = next(e for e in evs if e["type"] == "tool_result")
    assert r["is_error"] and "exit 2" in r["text"]
    assert evs[-1]["session_id"] == "t1" and evs[-1]["usage"] == {}


def test_codex_errors_once():
    evs = _feed([{"type": "thread.started", "thread_id": "t1"},
                 {"type": "error", "message": "Reconnecting... 1/5"},
                 {"type": "error", "message": "stream error: 401"},
                 {"type": "turn.failed", "error": {"message": "stream error: 401"}},
                 {"type": "turn.failed", "error": {"message": "again"}}])
    assert [e["type"] for e in evs] == ["session", "status", "error"]
    assert evs[-1]["error"] == "stream error: 401"


def test_codex_chat_cmd_shapes(tmp_path):
    a = agent.CodexAgent("/x/codex")
    c = a.chat_cmd(None, tmp_path, "/repo", [tmp_path / "a.jpg"])
    assert c[:2] == ["/x/codex", "exec"] and c[-1] == "-" and c[c.index("-i") + 1] == str(tmp_path / "a.jpg")
    assert c.index("-i") < c.index("-s")                 # -i takes several values: a flag must follow it
    r = a.chat_cmd("abc", tmp_path, "/repo")
    assert r[r.index("resume") + 1] == "abc" and r.index("-C") < r.index("resume") and r[-1] == "-"
    assert a.session_gone("Error: no rollout found for thread id abc")
    assert not a.session_gone("unexpected status 401")


def test_claude_chat_cmd_unchanged():
    c = agent.ClaudeAgent("/x/claude").chat_cmd("s1", "/w", "/repo")
    assert c[:2] == ["/x/claude", "-p"] and c[c.index("--resume") + 1] == "s1"
    assert "--strict-mcp-config" in c and c[c.index("--setting-sources") + 1] == "project,local"
    assert "Bash(cutroom:*)" in c and c[c.index("--add-dir") + 1] == "/repo"


def test_repo_agents_md_in_sync():
    """The repo-root AGENTS.md inlines CLAUDE.md's studio sections verbatim; regenerate it if this fails."""
    from pathlib import Path
    from cutroom import studio
    repo = Path(__file__).resolve().parents[1]
    sec = studio._md_section((repo / "CLAUDE.md").read_text(), "## Studio mode")
    ag = (repo / "AGENTS.md").read_text()
    assert "### Talking in the studio" in sec and sec.strip() in ag
    assert "docs/ARCHITECTURE.md" in ag and "extending-cutroom" in ag and "CLAUDE.md" in ag
