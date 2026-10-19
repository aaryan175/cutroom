"""The cutroom factory: "comment in the Studio -> factory -> team -> next version" (docs/FACTORY.md).

A code-driven runner on a durable folder-per-order bus drives each order through fixed phases. Code does
everything that can be measured (mapping notes to segments, rendering, machine QA, the lock check, the
gates); short, single-purpose agent runs (producer, editor, critic, lesson-keeper) do the judgement.
No AI process is ever "always on": the runner is plain Python and costs nothing while idle.

    bus.py        orders + events (atomic writes, flock per order, folded status, closed state set)
    fconfig.py    defaults + <work>/factory/config.json
    runner.py     FactoryRunner: tick loop, gates, backoff, resume, phase driver, budgets
    phases.py     intake / render / machine QA / integrate (code) + the role phase wrappers
    roles.py      role table (tools, sandbox, model, wall, schema) + prompt assembly
    mapnotes.py   comment -> pieces / words / frames + flags (restore / remove / praise / conditional)
    lockcheck.py  plan-level and render-level "nothing un-noted changed"
    gates.py      machine QA phase: qa, lock check, changed-spots sheet, lint, drift, link check
    lint.py       declarative checks.json evaluator
    lessons.py    harvest / triage apply / scoped brief / lessons_sha
    brain.py      the project brain (story, reference, decisions, locks, notes ledger, grades)
    card.py       the delivery card + version theme + comment replies
    worktree.py   extender stub (git worktree sub-orders; never auto-applied)
"""
