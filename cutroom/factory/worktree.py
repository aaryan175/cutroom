"""Extender (build step 13) - STUB in this build.

Design (docs/FACTORY.md "Extender"): when the producer says a note needs a capability cutroom lacks (an
overlay kind, a transition, a QA check), the runner creates an `extend` sub-order. The extender role works in
a git worktree of the cutroom repo (`git worktree add <work>/factory/worktrees/<order> -b factory/<order>`),
writes the feature with tests, and `pytest -q` must pass there. The patch then waits for the operator's
**Apply** in the Studio (never applied automatically); after Apply a follow-up revise order re-runs the note.

What exists now: the sub-order, its needs-you card ("New feature needed: ...") and the Apply placeholder in
the UI (POST /api/factory/apply-extension/<order> answers 501). `available()` is what `cutroom doctor` checks.
"""
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def available() -> tuple:
    """(ok, detail): git installed and the cutroom checkout is a git repository."""
    git = shutil.which("git")
    if not git:
        return False, "git not installed (needed for the extender's worktrees)"
    try:
        r = subprocess.run([git, "-C", str(REPO), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git failed: {e}"
    if r.stdout.strip() != "true":
        return False, f"{REPO} is not a git checkout (the extender needs one)"
    return True, f"{git} · {REPO} is a git checkout"


def apply(cfg, oid):
    raise NotImplementedError("the extender is a stub in this build: build the feature by hand "
                              "(the extending-cutroom skill), then answer the order's question")
