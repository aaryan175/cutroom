"""The delivery card: note -> change -> [[t:]] -> proof, what was checked, caveats, and short questions.

card.json is what the Review tab and the /review page show; card.md is the same for humans and agents.
A question is ONE short line (<= 320 chars) with numbered options ("1 = ... / 2 = ..."), shown as buttons.
"""
import re

QMAX = 320


def mmss(t):
    if t is None:
        return "--:--"
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{t % 60:04.1f}"


def question_problem(q) -> str | None:
    """Why a question is not acceptable, or None. Shape law from the reel factory (L0071)."""
    text = str(q.get("text") or "").strip()
    opts = [str(o).strip() for o in q.get("options") or [] if str(o).strip()]
    if not text:
        return "empty question"
    if len(opts) < 2:
        return "a question needs at least 2 numbered options"
    line = question_line(text, opts)
    if len(line) > QMAX:
        return f"question is {len(line)} characters (max {QMAX})"
    return None


def question_line(text, opts) -> str:
    return f"{text.strip()} " + " / ".join(f"{i + 1} = {o}" for i, o in enumerate(opts))


def clean_questions(qs, kind="taste") -> list:
    """Shape-checked questions; an over-long one is cut to fit (never dropped: the operator must see it)."""
    out = []
    for i, q in enumerate(qs or []):
        opts = [str(o).strip()[:80] for o in q.get("options") or [] if str(o).strip()][:4]
        if len(opts) < 2:
            opts = (opts + ["yes", "no"])[:2] if opts else ["yes", "no"]
        text = " ".join(str(q.get("text") or q.get("question") or "").split())
        room = QMAX - len(question_line("", opts)) - 1
        if len(text) > room:
            text = text[:max(20, room - 1)].rstrip() + "…"
        out.append({"id": q.get("id") or f"q{i + 1}", "text": text, "options": opts, "note": q.get("note"),
                    "kind": q.get("kind") or kind, "line": question_line(text, opts)})
    return out


def theme(version, rows) -> str:
    """'v24: removed the 7:19 walk; swapped the 8:23 shot' (<= 90 chars)."""
    bits = [re.sub(r"\s+", " ", str(r.get("change") or "")).strip().rstrip(".") for r in rows if r.get("change")]
    s = "; ".join(b[:1].lower() + b[1:] for b in bits if b) or "no changes"
    s = f"v{version}: {s}"
    return s if len(s) <= 90 else s[:89] + "…"


def build(order, version, notes, ledger, questions, checked, warns, critic, caveats, passed, not_passed, base_version,
          render_rel, sheet_rel, lessons_sha, cost_usd, wall_s, skipped_notes=()) -> dict:
    by_note = {}
    for r in ledger:
        by_note.setdefault(r.get("note"), []).append(r)
    rows = []
    for n in notes:
        for r in by_note.get(n["id"]) or []:
            rows.append({"note": n["id"], "comment": n.get("comment"), "words": n.get("text"), "t_note": n.get("t"),
                         "change": r.get("change"), "t": r.get("t_out"), "chip": f"[[t:{float(r['t_out']):.2f}]]"
                         if r.get("t_out") is not None else "", "proof": r.get("proof"), "segments": r.get("segments")})
    return {"v": 1, "order": order["id"], "kind": order["kind"], "plan": order.get("plan"), "version": version,
            "base_version": base_version, "render": render_rel, "changed_sheet": sheet_rel, "passed": passed,
            "not_passed": not_passed, "theme": theme(version, ledger), "rows": rows, "questions": questions,
            "checked": checked, "warns": warns, "critic": critic, "caveats": caveats,
            "skipped_notes": list(skipped_notes), "badges": {"machine_qa": "pass" if passed or not
                                                             [x for x in not_passed if x.startswith("machine")] else "fail",
                                                             "critic": critic.get("status") if critic else "skipped",
                                                             "approved": False, "exported": False},
            "lessons_sha": lessons_sha, "cost_usd": round(cost_usd or 0, 4), "wall_s": round(wall_s or 0, 1)}


def markdown(card) -> str:
    L = [f"# {card['plan']} v{card['version']}  ({'passed' if card['passed'] else 'NOT PASSED: ' + '; '.join(card['not_passed'])})",
         "", f"Order `{card['order']}` ({card['kind']}), from v{card.get('base_version')}. {card['theme']}", "",
         "## What changed", "", "| note | said | change | where | proof |", "|---|---|---|---|---|"]
    for r in card["rows"]:
        L.append(f"| {r['note']} | {str(r.get('words') or '')[:80]} | {r.get('change') or ''} | {mmss(r.get('t'))} | "
                 f"{str(r.get('proof') or '')[:100]} |")
    if card["questions"]:
        L += ["", "## Couldn't place (needs you)", ""] + [f"- {q['line']}" for q in card["questions"]]
    if card.get("critic") and card["critic"].get("findings"):
        L += ["", "## Critic", ""]
        for f in card["critic"]["findings"]:
            L.append(f"- {f.get('id')} {f.get('sev')} @ {mmss(f.get('t'))}: {f.get('text')} -> {f.get('fix') or 'open'}")
    L += ["", "## Checked", ""] + [f"- {c}" for c in card["checked"]]
    if card["warns"]:
        L += ["", "## Warnings (explained)", ""] + [f"- {w}" for w in card["warns"]]
    if card["caveats"]:
        L += ["", "## Caveats", ""] + [f"- {c}" for c in card["caveats"]]
    L += ["", f"lessons {card['lessons_sha']} · cost ${card['cost_usd']:.2f} · {card['wall_s'] / 60:.1f} min"]
    return "\n".join(L) + "\n"
