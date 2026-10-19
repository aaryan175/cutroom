"""Factory settings: code defaults, overridden by <work>/factory/config.json (any subset of keys).

cutroom.json is not used for these: Config drops unknown keys when it saves, and the factory settings
belong to the factory folder (portable with the project). Environment overrides for tests and SSH runs:
CUTROOM_FACTORY_DEBOUNCE_S, CUTROOM_FACTORY_COST_USD, CUTROOM_FACTORY_AGENT (claude|codex|auto).
"""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "autostart": True,                 # the studio starts a runner thread (one per project, flock)
    "debounce_s": 90,                  # notes sent within this window fold into one order
    "agent": "auto",                   # auto = the project's chat agent (agent.choose); claude | codex
    "models": {"producer": "", "editor": "", "critic": "", "forensics": "", "extender": "", "lessons": ""},
    "critic": "always_except_text",    # the critic runs on every order except pure text/caption tweaks
    "budgets": {"cost_usd": 5.0,       # per order (notional: Claude total_cost_usd, Codex tokens x price)
                "wall_s": 7200,        # S/M orders; L orders get wall_s_large
                "wall_s_large": 14400,
                "role_runs": 6},
    "walls": {"intake": 120, "producer": 240, "forensics": 1200, "extend": 3600, "edit": 1800, "edit_large": 4500,
              "render": 5400, "qa": 900, "critic": 900, "fix": 1800, "verify": 600, "integrate": 300, "lessons": 300},
    "render_base": True,               # no fresh preview of the base version: render it once (like-for-like QA)
    "limit_backoff_s": 1800,           # usage limit / overload: not_before = now + this, at most limit_retries
    "limit_retries": 3,
    "refusal_backoff_s": 21600,        # login refused: everything pauses (Resume clears it at once)
    "login_check_ttl_s": 600,
    "auto_apply_extensions": False,    # never: an extension waits for Apply in the Studio
    "auto_default_answers": False,
    "editor_resume": False,
    "max_notes_small": 12,
    "price_per_mtok": {"input": 1.25, "cached_input": 0.125, "output": 10.0},   # Codex token -> notional $
}


def factory_dir(cfg) -> Path:
    return Path(cfg.work) / "factory"


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(cfg) -> dict:
    p = factory_dir(cfg) / "config.json"
    over = {}
    if p.exists():
        try:
            over = json.loads(p.read_text())
        except (OSError, ValueError):
            over = {}
    fc = _merge(DEFAULTS, over if isinstance(over, dict) else {})
    env = os.environ
    if env.get("CUTROOM_FACTORY_DEBOUNCE_S"):
        fc["debounce_s"] = float(env["CUTROOM_FACTORY_DEBOUNCE_S"])
    if env.get("CUTROOM_FACTORY_COST_USD"):
        fc["budgets"]["cost_usd"] = float(env["CUTROOM_FACTORY_COST_USD"])
    if env.get("CUTROOM_FACTORY_AGENT"):
        fc["agent"] = env["CUTROOM_FACTORY_AGENT"]
    return fc


def save(cfg, over: dict) -> dict:
    """Merge `over` into config.json (only the keys given are written)."""
    p = factory_dir(cfg) / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if p.exists():
        try:
            cur = json.loads(p.read_text())
        except (OSError, ValueError):
            cur = {}
    cur = _merge(cur if isinstance(cur, dict) else {}, over)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    os.replace(tmp, p)
    return load(cfg)
