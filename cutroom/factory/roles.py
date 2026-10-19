"""Role table + prompt assembly. Every prompt is generated from the brain and the lessons on every run, so
nothing drifts (the reel factory's hand-copied re-briefs did).

Read-only roles (producer, forensics, critic, lessons) are guarded by code: the runner hashes the version
files, the brain and the order's comments before and after the run; any change fails the phase as HARD.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
READ_TOOLS = ["Read", "Glob", "Grep"]
ROLES = {
    "producer": {"tools": READ_TOOLS + ["Bash(cutroom say:*)", "Bash(cutroom frames:*)", "Bash(cutroom search:*)",
                                         "Bash(cutroom clip:*)"],
                 "sandbox": "read-only", "readonly": True, "schema": "changes", "wall": "producer", "prompt": "producer"},
    "forensics": {"tools": READ_TOOLS + ["Bash(cutroom ref:*)", "Bash(cutroom frames:*)"],
                  "sandbox": "read-only", "readonly": True, "schema": "forensics", "wall": "forensics", "prompt": "forensics"},
    "editor": {"tools": READ_TOOLS + ["Write", "Edit", "Bash(cutroom:*)"],
               "sandbox": "workspace-write", "readonly": False, "schema": "ledger", "wall": "edit", "prompt": "editor"},
    "fix": {"tools": READ_TOOLS + ["Write", "Edit", "Bash(cutroom:*)"],
            "sandbox": "workspace-write", "readonly": False, "schema": "ledger", "wall": "fix", "prompt": "fix",
            "model_key": "editor"},
    "variants": {"tools": READ_TOOLS + ["Write", "Edit", "Bash(cutroom:*)"],
                 "sandbox": "workspace-write", "readonly": False, "schema": "variants", "wall": "edit", "prompt": "editor",
                 "model_key": "editor"},
    "critic": {"tools": READ_TOOLS + ["Bash(cutroom frames:*)", "Bash(cutroom ref compare:*)"],
               "sandbox": "read-only", "readonly": True, "schema": "findings", "wall": "critic", "prompt": "critic"},
    "lessons": {"tools": ["Read", "Grep"], "sandbox": "read-only", "readonly": True, "schema": "lessonops",
                "wall": "lessons", "prompt": "lessonkeeper"},
    "extender": {"tools": READ_TOOLS, "sandbox": "read-only", "readonly": True, "schema": "extend", "wall": "extend",
                 "prompt": "extender"},
}
CONTEXT_CAP = 8000


def schema(name) -> dict:
    return json.loads((HERE / "schemas" / f"{name}.json").read_text())


def prompt_text(name) -> str:
    return (HERE / "prompts" / f"{name}.md").read_text()


def _clip(s, n=CONTEXT_CAP):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + f"\n… ({len(s) - n} more characters in the file)"


def build(role, *, order, context: dict, brief_text="", sections=()) -> str:
    """common.md + <role>.md + the order context (paths, small facts) + the scoped lessons brief +
    extra sections [(heading, text)] + the output contract."""
    r = ROLES[role]
    parts = [prompt_text("common"), prompt_text(r["prompt"])]
    parts.append(f"## This order\n- order `{order['id']}` ({order['kind']}) on plan `{order.get('plan')}`")
    for k, v in context.items():
        if v is None or v == "" or v == []:
            continue
        parts.append(f"- {k}: `{v}`" if isinstance(v, (str, Path)) and len(str(v)) < 300 else
                     f"- {k}: {_clip(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, 3000)}")
    for head, text in sections:
        if text:
            parts.append(f"## {head}\n{_clip(text)}")
    if brief_text:
        parts.append("## Rules for this order (scoped brief)\n" + _clip(brief_text, 12000))
    parts.append("## Output\nWhen done, reply with ONLY the JSON object matching the schema you were given "
                 "(no prose around it).")
    return "\n\n".join(parts) + "\n"
