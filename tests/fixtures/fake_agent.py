"""A fake `claude` / `codex` for the factory: never calls a model, costs nothing.

Point CUTROOM_CLAUDE_BIN (or CUTROOM_CODEX_BIN) at an executable wrapper of this file. It answers the login
checks, and for a role run (CUTROOM_FACTORY_ROLE is set by the factory) it prints the agent's own event stream
(Claude stream-json, or Codex JSONL + the -o file) with a schema-shaped result.

Behaviour per role comes from FAKE_AGENT_SCRIPT, a JSON file {"<role>": step | [step, step, ...], "*": step}.
The n-th run of a role uses step n (the last one repeats). A step:
    mode        ok (default) | refuse | limit | timeout | noresult | quote_limit
    sleep       seconds before answering
    edits       plan edits on $CUTROOM_FACTORY_CANDIDATE (editor / fix): [{op: delete|set|insert|set_style|replace,
                sec, seg, fields | segment | plan}]
    variant_edits  [[edits for option 1], [option 2], ...] on $CUTROOM_FACTORY_VARIANTS
    result      merged over the default result for the role
    cost        total_cost_usd to report (default 0.01)
    write       {"relative/path": "text"} written into the working dir (to test the read-only guard)
Without a script every role does something sensible with the notes the runner mapped (phases/00_intake/out.json).
FAKE_AGENT_LOG appends one JSON line per call: {argv, role, cwd, prompt_head}.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
is_codex = bool(argv) and argv[0] in ("exec", "login")
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.environ.get("FAKE_AGENT_LOGGED_OUT") != "1", "authMethod": "fake"}))
    sys.exit(0)
if argv[:2] == ["login", "status"]:
    if os.environ.get("FAKE_AGENT_LOGGED_OUT") == "1":
        print("Not logged in")
        sys.exit(1)
    print("Logged in using ChatGPT (fake)")
    sys.exit(0)

prompt = sys.stdin.read()
role = os.environ.get("CUTROOM_FACTORY_ROLE") or "chat"
odir = Path(os.environ.get("CUTROOM_FACTORY_ORDER_DIR") or ".")
if os.environ.get("FAKE_AGENT_LOG"):
    with open(os.environ["FAKE_AGENT_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv, "role": role, "cwd": os.getcwd(), "prompt_head": prompt[:4000],
                            "prompt_len": len(prompt)}) + "\n")

script = {}
if os.environ.get("FAKE_AGENT_SCRIPT"):
    try:
        script = json.loads(Path(os.environ["FAKE_AGENT_SCRIPT"]).read_text())
    except (OSError, ValueError):
        script = {}
steps = script.get(role, script.get("*", {}))
n = 0
if os.environ.get("FAKE_AGENT_SCRIPT"):
    cdir = Path(os.environ["FAKE_AGENT_SCRIPT"] + ".calls")
    cdir.mkdir(exist_ok=True)
    cf = cdir / role
    n = int(cf.read_text()) if cf.exists() else 0
    cf.write_text(str(n + 1))
step = (steps[min(n, len(steps) - 1)] if steps else {}) if isinstance(steps, list) else (steps or {})
mode = step.get("mode", "ok")
if step.get("sleep"):
    time.sleep(float(step["sleep"]))


def intake():
    try:
        p = next((odir / "phases").glob("00_intake/out.json"))
        return json.loads(p.read_text())
    except (StopIteration, OSError, ValueError):
        return {"notes": []}


def apply_edits(path, edits):
    plan = json.loads(Path(path).read_text())
    for e in edits or []:
        op = e.get("op")
        if op == "replace":
            keep_v = plan.get("version")
            plan = json.loads(json.dumps(e["plan"]))
            plan["version"] = keep_v
            continue
        if op == "set_style":
            plan.setdefault("style", {}).update(e.get("fields") or {})
            continue
        segs = plan["sections"][int(e["sec"])]["segments"]
        if op == "delete":
            segs.pop(int(e["seg"]))
        elif op == "set":
            for k, v in (e.get("fields") or {}).items():
                if v is None:
                    segs[int(e["seg"])].pop(k, None)
                else:
                    segs[int(e["seg"])][k] = v
        elif op == "insert":
            segs.insert(int(e["seg"]), e["segment"])
    Path(path).write_text(json.dumps(plan, indent=1))


def default_result():
    it = intake()
    notes = it.get("notes") or []
    if role == "producer":
        ch = []
        for x in notes:
            fl = x.get("flags") or []
            act = "remove" if "remove" in fl else "restore" if "restore" in fl else "none" if "praise" in fl else "trim"
            ch.append({"note": x["id"], "action": act, "segments": (x.get("segments") or [])[:1],
                       "summary": f"{act} for {x['id']}", "tags": []})
        return {"changes": ch, "team": {"size": "S", "forensics": False, "extend": [], "why": "stub"},
                "questions": [], "summary": f"{len(ch)} change(s)"}
    if role in ("editor", "fix"):
        led = [{"note": x["id"], "change": f"stub change for {x['id']}", "segments": (x.get("segments") or [])[:1],
                "t_out": x.get("t"), "proof": "stub: looked at the frame"} for x in notes]
        proofs = []
        if role == "fix":
            for fid in re.findall(r"^- ((?:F\d+)|(?:[a-z_]+:[\w:-]+)|lock|link|av_drift|lock_render) ", prompt, re.M):
                proofs.append({"finding": fid, "proof": f"stub proof for {fid}"})
        return {"candidate": os.environ.get("CUTROOM_FACTORY_CANDIDATE", ""), "ledger": led, "proofs": proofs,
                "unplaced": [], "summary": "stub edit"}
    if role == "variants":
        paths = [p for p in os.environ.get("CUTROOM_FACTORY_VARIANTS", "").split(",") if p]
        return {"variants": [{"name": f"option {i + 1}", "candidate": p, "segments": [], "summary": f"stub option {i + 1}"}
                             for i, p in enumerate(paths)], "summary": "stub options"}
    if role == "critic":
        return {"findings": [], "per_note": [{"note": x["id"], "addressed": "yes", "why": "stub"} for x in notes],
                "summary": "stub critic: nothing found"}
    if role == "lessons":
        ids = re.findall(r"^- (I\d{4}) ", prompt, re.M)
        return {"ops": [{"op": "none", "inbox": i, "lesson": None, "rule": None, "symptom": None, "tags": [],
                         "check_json": None, "reason": "stub"} for i in ids], "summary": "stub"}
    if role == "forensics":
        return {"reference_md": "# Reference (stub)\n", "pairs": [{"label": "start", "desc": "stub", "ref_t": 0, "our_t": 0}],
                "summary": "stub"}
    if role == "extender":
        return {"capability": "stub", "patch_summary": "stub", "tests": "stub", "summary": "stub"}
    return {"summary": "chat"}


if mode == "ok" or mode == "quote_limit":
    if role in ("editor", "fix") and step.get("edits"):
        apply_edits(os.environ["CUTROOM_FACTORY_CANDIDATE"], step["edits"])
    if role == "variants" and step.get("variant_edits"):
        paths = [p for p in os.environ.get("CUTROOM_FACTORY_VARIANTS", "").split(",") if p]
        for p, ed in zip(paths, step["variant_edits"]):
            apply_edits(p, ed)
for rel, text in (step.get("write") or {}).items():
    p = Path(os.getcwd()) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
res = default_result()
res.update(step.get("result") or {})
cost = float(step.get("cost", 0.01))
say = "Done. The note says: Usage limit reached, which is just a quote." if mode == "quote_limit" else "Done."


def out(o):
    print(json.dumps(o), flush=True)


if mode == "timeout":
    time.sleep(3600)
    sys.exit(0)

if is_codex:
    last = argv[argv.index("-o") + 1] if "-o" in argv else None
    out({"type": "thread.started", "thread_id": "fake-thread"})
    out({"type": "turn.started"})
    if mode == "noresult":
        sys.stderr.write("fake codex crashed\n")
        sys.exit(2)
    if mode == "refuse":
        out({"type": "error", "message": "unexpected status 401 Unauthorized: Missing bearer or basic authentication"})
        out({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: Missing bearer"}})
        sys.exit(1)
    if mode == "limit":
        out({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}})
        sys.exit(1)
    out({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cutroom status",
                                            "aggregated_output": "ok", "exit_code": 0, "status": "completed"}})
    out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": say + "\n" + json.dumps(res)}})
    if last:
        Path(last).write_text(json.dumps(res))
    out({"type": "turn.completed", "usage": {"input_tokens": 2000, "cached_input_tokens": 1000, "output_tokens": 300}})
    sys.exit(0)

out({"type": "system", "subtype": "init", "session_id": "fake-sess", "model": "fake"})
if mode == "noresult":
    sys.stderr.write("fake claude crashed\n")
    sys.exit(2)
if mode == "refuse":
    out({"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login",
         "session_id": "fake-sess", "total_cost_usd": 0})
    sys.exit(1)
if mode == "limit":
    out({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1760000000",
         "session_id": "fake-sess", "total_cost_usd": 0})
    sys.exit(1)
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                                                   "input": {"file_path": str(odir / "candidate.json")}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": say}]}})
out({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost, "duration_ms": 50, "num_turns": 2,
     "result": json.dumps(res), "structured_output": res, "session_id": "fake-sess",
     "usage": {"input_tokens": 100, "output_tokens": 20}})
