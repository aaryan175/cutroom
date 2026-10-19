"""The lock check: nothing the operator did not name may change (EDITING_RULES "one change per note").

Plan level (`plan_lock`): base and candidate segments are compared in order. A base segment that is
deleted, replaced or moved must be named by a mapped note (or by a ledger row of this order); a new
segment must sit next to a named one. Praised / approved segments (brain locks) may only change when a
note of THIS order names them and is not just praise. Plan-wide fields (style, output, cold open, section
music) may only change when the producer's changes have a global action (music, style, audio, graphics).

Render level (`render_lock`): for untouched segments, a frame from the middle of the segment in the base
render and in the candidate render must match (PSNR). Loudness is normalised over the whole programme, so
bit-identical audio across versions is not expected and audio is not compared byte for byte.
"""
import difflib
import json

from . import brain

GLOBAL_ACTIONS = {"music", "style", "audio", "graphics", "grade", "captions_style", "story", "reorder", "new_edit"}
IGNORE_TOP = {"version", "sections", "notes", "factory"}


def _canon(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def flat(plan):
    out = []
    for si, sec in enumerate(plan.get("sections") or []):
        for gi, seg in enumerate(sec.get("segments") or []):
            out.append({"ref": f"{si}.{gi}", "sec": si, "seg": gi, "section": sec.get("name"), "key": brain.seg_key(seg),
                        "canon": _canon(seg), "seg_obj": seg})
    return out


def _sections_meta(plan):
    return [{k: v for k, v in (s or {}).items() if k != "segments"} for s in plan.get("sections") or []]


def plan_lock(base, cand, allowed_refs=(), locked_keys=(), allowed_global=False, allowed_locked_refs=()) -> dict:
    """{status: pass|fail, issues: [{sev: HARD|warn, text, ref?}], changed: [base refs], added: [cand refs],
        pairs: [(base ref, cand ref)] untouched}"""
    allowed = set(allowed_refs)
    a, b = flat(base), flat(cand)
    issues, changed, added, pairs = [], [], [], []
    sm = difflib.SequenceMatcher(a=[x["canon"] for x in a], b=[x["canon"] for x in b], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            pairs += [(a[i1 + k]["ref"], b[j1 + k]["ref"]) for k in range(i2 - i1)]
            continue
        region = [a[i]["ref"] for i in range(i1, i2)]
        changed += region
        for i in range(i1, i2):
            x = a[i]
            if x["ref"] not in allowed:
                issues.append({"sev": "HARD", "ref": x["ref"], "text": f"segment {x['ref']} ({x['section']}) changed "
                               "but no note named it"})
            elif x["key"] in set(locked_keys) and x["ref"] not in set(allowed_locked_refs):
                issues.append({"sev": "HARD", "ref": x["ref"], "text": f"segment {x['ref']} was praised/approved "
                               "(locked) and only praised in this round"})
        if j2 > j1:
            added += [b[j]["ref"] for j in range(j1, j2)]
            near = region or [a[i]["ref"] for i in (i1 - 1, i1) if 0 <= i < len(a)]
            if not any(r in allowed for r in near) and op == "insert":
                issues.append({"sev": "HARD", "ref": b[j1]["ref"], "text": f"{j2 - j1} new segment(s) at {b[j1]['ref']} "
                               "not next to any noted segment"})
    # plan-wide fields
    tb = {k: v for k, v in base.items() if k not in IGNORE_TOP}
    tc = {k: v for k, v in cand.items() if k not in IGNORE_TOP}
    diff_top = sorted(k for k in set(tb) | set(tc) if _canon(tb.get(k)) != _canon(tc.get(k)))
    if _sections_meta(base) != _sections_meta(cand) and len(base.get("sections") or []) == len(cand.get("sections") or []):
        diff_top.append("sections (names / music)")
    for k in diff_top:
        if not allowed_global:
            issues.append({"sev": "HARD", "ref": None, "text": f"plan-wide '{k}' changed but no note asked for a "
                           "global change"})
        else:
            issues.append({"sev": "warn", "ref": None, "text": f"plan-wide '{k}' changed (a note asked for it)"})
    return {"status": "fail" if any(i["sev"] == "HARD" for i in issues) else "pass", "issues": issues,
            "changed": changed, "added": added, "pairs": pairs, "global_changed": diff_top}


def _seg_mid(tl, sec, seg):
    """Middle of a segment on a resolved timeline (render.resolve_timeline: same times as the render)."""
    row = next((r for r in tl.get("segments") or [] if r.get("sec") == sec and r.get("seg") == seg), None)
    if not row or row.get("end") is None:
        return None
    return (float(row["start"]) + float(row["end"])) / 2


def psnr(img_a, img_b):
    import numpy as np
    x = np.asarray(img_a, dtype=np.float32)
    y = np.asarray(img_b, dtype=np.float32)
    if x.shape != y.shape:
        return 0.0
    mse = float(((x - y) ** 2).mean())
    return 99.0 if mse <= 1e-9 else round(10 * np.log10(255.0 ** 2 / mse), 2)


def render_lock(cfg, base_video, base_tl, cand_video, cand_tl, pairs, out_dir, max_n=12, fail_db=30.0,
                warn_db=35.0) -> dict:
    """Frame PSNR at the middle of up to `max_n` untouched segments (base vs candidate render)."""
    from pathlib import Path

    from PIL import Image

    from .. import media
    rows, issues = [], []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    step = max(1, len(pairs) // max_n) if pairs else 1
    for ra, rb in pairs[::step][:max_n]:
        sa, ga = map(int, ra.split("."))
        sb, gb = map(int, rb.split("."))
        ta, tb = _seg_mid(base_tl, sa, ga), _seg_mid(cand_tl, sb, gb)
        if ta is None or tb is None:
            continue
        fa, fb = out_dir / f"lock_{ra}_base.jpg", out_dir / f"lock_{rb}_cand.jpg"
        try:
            for v, t, f in ((base_video, ta, fa), (cand_video, tb, fb)):
                media.ffmpeg(cfg, ["-ss", f"{t:.3f}", "-i", v, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "2", f],
                             timeout=60)
            p = psnr(Image.open(fa).convert("L"), Image.open(fb).convert("L"))
        except Exception as e:  # noqa: BLE001
            issues.append({"sev": "warn", "ref": ra, "text": f"could not compare {ra}: {e}"})
            continue
        rows.append({"base": ra, "cand": rb, "t_base": round(ta, 3), "t_cand": round(tb, 3), "psnr": p})
        if p < fail_db:
            issues.append({"sev": "HARD", "ref": ra, "text": f"untouched segment {ra} looks different in the render "
                           f"(PSNR {p} dB at {tb:.1f}s)", "t": tb})
        elif p < warn_db:
            issues.append({"sev": "warn", "ref": ra, "text": f"untouched segment {ra}: PSNR {p} dB (re-encode drift?)",
                           "t": tb})
    return {"status": "fail" if any(i["sev"] == "HARD" for i in issues) else "pass", "rows": rows, "issues": issues}
