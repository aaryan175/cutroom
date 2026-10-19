"""Claims vs the render: every number an editor / fix summary states that code can measure is measured.

The real run's editor wrote "the floor under the lines is 6 dB deeper", "a ~11 LU swing", "C5655 is not used
anywhere else within 10 s"; the render showed 1-3 dB, 7.2 LU and a repeat. Those sentences reached the card as
if they were facts. Here every such sentence is checked against the render (and, for "deeper / louder / more"
claims, against the base version's render in the same window) BEFORE the card is built. A false claim becomes
a HARD finding (source "claims") that the fix round must answer, and stays on the card as "not on the
render" if it is still false after the fix.

    check(cfg, texts, video, tl, base_video=None, base_tl=None, qa_report=None) -> [row]
    row = {note, source, text, qty, kind: abs|delta, claimed, measured, ok, tol, window, why}

Measured quantities (dynamics.py, momentary loudness unless noted):
    I       integrated loudness of the window (LUFS)           programme   whole-file integrated (LUFS)
    swing   p95 - p10 of momentary loudness (LU)               hits        p95 of momentary (LUFS)
    floor   p10 of momentary (LUFS)                            lines       median momentary (LUFS)
    hits_over_lines  p95 - median (LU)                         TP          whole-file true peak (dBTP)
    LRA     whole-file loudness range (LU)                     repeats / clipping   from the QA report
Window: the generated cold open when the note is on it (or the text says cold open / intro / teaser), else
the note's output time +-3 s, else the whole programme.

Parameter statements are not claims about the render and are skipped: "from 20 to 22 dB", a plan key
(cold_open.music.lift_db), "target", "set to", "default", "tolerance", "ceiling".
"""
import re

from .. import dynamics

NUM = re.compile(r"(?P<pre>~|≈|about|around|roughly|approx\.?|approximately)?\s*(?P<sign>[+\-−–])?\s*"
                 r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>LUFS|LKFS|dBTP|dBFS|LU|dB)\b")
PARAM = re.compile(r"\bfrom\s+(?:about |around |roughly |~)?[+\-−–]?\d+(?:\.\d+)?\s*(?:LUFS|LU|dB)?\s+to\b|\b[a-z_]+\.[a-z_]+(?:\.[a-z_]+)*\b|"
                   r"\btarget|\bset (?:it |them )?to\b|\bdefault|\btolerance|\bceiling|\bthreshold|\bknee\b|"
                   r"\bmax(?:imum)?\b|\bcap\b|\bpreset\b", re.I)
SUBJ = [("TP", r"true[- ]peak|\bTP\b|dBTP"), ("LRA", r"\bLRA\b|loudness range"),
        ("I", r"average|(?:overall|integrated|cold[- ]open'?s?) level|level of the (?:cold open|intro|teaser)"),
        ("programme", r"programme|program\b|overall|whole[- ](?:file|film|video)|integrated"),
        ("hits_over_lines", r"(?:dialog(?:ue)?|voice|lines?|talk)\s*[+]\s*\d|above (?:the )?(?:lines|dialog|voice|talk)"),
        ("swing", r"swing|quiet[- ](?:to|vs\.?|versus)[- ]hit|punch|apart|jump(?:s)? out"),
        ("floor", r"floor|under (?:the )?(?:lines|talk|voice)|bed\b|quiet(?:er)? parts?|quiet\b"),
        ("hits", r"\bhits?\b|burns?\b|peaks?\b|loud(?:est)? (?:moments|parts)|lift"),
        ("lines", r"\blines?\b|talk|dialog|voice|speech")]
SUBJ_RX = dict(SUBJ)
BASE = re.compile(r"\bbase\b|\breference\b|\bref\b|instead of|\bwas\b|\bwere\b|\bold\b|\bbefore\b|previous|\bv\d+'s\b|\(v\d+\)|"
                  r"\bfrom\s*$|on (?:the|that) .{0,20}scale|operator measured", re.I)
UP = re.compile(r"\bopen(?:s|ed)\b|widen(?:s|ed)?|hotter|louder|higher|\bup\b|raised|boost|lift(?:ed|s)?\b|more|wider|further|increase|added|bigger|\+", re.I)
DOWN = re.compile(r"deeper|lower|quieter|\bdown\b|drop(?:s|ped)?|cut\b|reduced?|less|narrower|−|-\s*\d", re.I)
DELTA = re.compile(r"\bby\b|\bopen(?:s|ed)\b|widen|deeper|hotter|moves?|louder|quieter|higher|lower|more|less|further|wider|narrower|increase|decrease|drops?|"
                   r"raised|reduced|than (?:before|v\d+|the base)|compared with", re.I)
NO_REPEAT = re.compile(r"\bno (?:repeats?|re-?use|duplicate)|not (?:used|reused|repeated) (?:anywhere )?(?:else|nearby)|"
                       r"isn'?t (?:already )?used (?:anywhere|nearby)|not used anywhere", re.I)
NO_CLIP = re.compile(r"\bno clipping|doesn'?t clip|does not clip|without clipping|no longer clips", re.I)
CO_WORDS = re.compile(r"cold[- ]open|teaser|intro\b|the open(?:ing)?\b", re.I)


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;!?])\s+(?=[A-Z(\"'0-9])", str(text or "")) if s.strip()]


def _clause(sent, a, b):
    """The clause around a match: back to the previous , ; ( or sentence start, forward to the next , ; )."""
    lo = max(sent.rfind(c, 0, a) for c in ",;(:") + 1
    hi = min([x for x in (sent.find(c, b) for c in ",;)") if x >= 0] or [len(sent)])
    return sent[lo:hi]


UNIT_QTYS = {"LUFS": {"programme", "hits", "floor", "lines", "I"}, "LKFS": {"programme", "hits", "floor", "lines", "I"}}


def _nearest_subject(sent, start, end, unit):
    """The quantity a number is about: the nearest subject word before it in its own clause; else the first
    one after it in the clause; else the nearest one before it in the sentence. A "dialogue +6 dB" construct
    is its own subject only for the number inside it."""
    lo = max(sent.rfind(c, 0, start) for c in ",;(:") + 1
    hi = min([x for x in (sent.find(c, end) for c in ",;)") if x >= 0] or [len(sent)])
    allowed = UNIT_QTYS.get(unit)
    hol = [(m.start(), m.end()) for m in re.finditer(SUBJ_RX["hits_over_lines"], sent, re.I)]
    if allowed is None and any(a <= start + 2 and start < b + 1 for a, b in hol) or \
            (allowed is None and any(a <= start < b or a < end <= b for a, b in hol)):
        return "hits_over_lines"
    hits = []
    for q, rx in SUBJ:
        for m in re.finditer(rx, sent, re.I):
            if allowed is not None and q not in allowed:
                continue
            if q != "hits_over_lines" and any(a <= m.start() < b for a, b in hol):
                continue
            if q == "hits_over_lines" and not (m.start() <= start <= m.end() + 2):
                continue
            hits.append((m.start(), m.end(), q))
    before_clause = [h for h in hits if lo <= h[0] and h[1] <= start]
    if before_clause:
        return max(before_clause, key=lambda h: h[1])[2]
    after_clause = [h for h in hits if end <= h[0] < hi]
    if after_clause:
        return min(after_clause)[2]
    before = [h for h in hits if h[1] <= start]
    return max(before, key=lambda h: h[1])[2] if before else None


def _direction(sent, start, end, clause):
    """+1 / -1 from the words right after the number ("6 dB deeper"), else the clause."""
    post = sent[end:end + 24]
    for txt in (post, clause):
        u, d = UP.search(txt), DOWN.search(txt)
        if u and d:
            return 1 if u.start() < d.start() else -1
        if u or d:
            return 1 if u else -1
    return 1


def parse(text) -> list:
    """Measurable claims in a text: [{text, qty, kind, claimed, sign}] (no measuring)."""
    out = []
    for sent in _sentences(text):
        for m in NUM.finditer(sent):
            cl = _clause(sent, m.start(), m.end())
            if PARAM.search(cl):
                continue
            unit = m.group("unit")
            sign = -1 if m.group("sign") in ("-", "−", "–") else 1
            val = float(m.group("num"))
            pre = sent[max(0, sent.rfind(",", 0, m.start()) + 1, m.start() - 60):m.start()]
            if BASE.search(pre) or BASE.search(cl[:max(0, cl.find(m.group('num')))]):
                continue                                        # a number about the OLD version, not this render
            qty = _nearest_subject(sent, m.start(), m.end(), unit)
            dirn = _direction(sent, m.end(), m.end(), cl)
            if unit == "dBTP":
                qty = "TP"
            elif unit == "LU" and qty not in ("hits_over_lines", "LRA") and re.search(SUBJ_RX["swing"], cl, re.I):
                qty = "swing"
            if unit in ("LUFS", "LKFS"):
                qty = qty if qty in ("programme", "hits", "floor", "lines") else "I"
                out.append({"text": sent, "qty": qty, "kind": "abs", "claimed": sign * val, "unit": "LUFS"})
            elif unit == "dBTP" or qty == "TP":
                out.append({"text": sent, "qty": "TP", "kind": "abs", "claimed": sign * val, "unit": "dBTP"})
            elif qty == "LRA":
                out.append({"text": sent, "qty": "LRA", "kind": "abs", "claimed": val, "unit": "LU"})
            elif qty == "hits_over_lines":
                out.append({"text": sent, "qty": "hits_over_lines", "kind": "abs", "claimed": val, "unit": "LU"})
            elif unit == "LU" and qty == "I":
                if DELTA.search(cl) or m.group("sign"):
                    d = sign if m.group("sign") else dirn
                    out.append({"text": sent, "qty": "I", "kind": "delta", "claimed": d * val, "unit": "LU"})
            elif unit == "LU" and qty in ("swing", None, "hits", "floor", "lines"):
                if DELTA.search(cl) or m.group("sign") == "+":
                    d = sign if m.group("sign") else dirn
                    out.append({"text": sent, "qty": "swing", "kind": "delta", "claimed": d * val, "unit": "LU"})
                else:
                    out.append({"text": sent, "qty": "swing", "kind": "abs", "claimed": val, "unit": "LU"})
            elif unit == "dB" and qty in ("floor", "hits", "lines", "swing", "programme"):
                if not DELTA.search(cl) and not m.group("sign"):
                    continue                                    # "+6 dB" with no direction or comparison: not checkable
                d = sign if m.group("sign") else dirn
                q = {"programme": "I"}.get(qty, qty)
                out.append({"text": sent, "qty": q, "kind": "delta", "claimed": d * val, "unit": "dB"})
        if NO_REPEAT.search(sent):
            out.append({"text": sent, "qty": "repeats", "kind": "none", "claimed": 0, "unit": ""})
        if NO_CLIP.search(sent):
            out.append({"text": sent, "qty": "clipping", "kind": "none", "claimed": 0, "unit": ""})
    return out


# ------------------------------------------------------------------------------------------ measuring
class _Meter:
    """Window stats per (video, window), measured once."""

    def __init__(self, cfg):
        self.cfg, self.cache = cfg, {}

    def stats(self, video, win):
        key = (str(video), None if win is None else (round(win[0], 2), round(win[1], 2)))
        if key not in self.cache:
            import numpy as np
            env = dynamics.envelope(self.cfg, video, *(win or (0.0, None)))
            m = np.asarray([x for x in env["M"] if x > dynamics.GATE], float)
            st = {"I": env["I"], "swing": dynamics.swing(env), "LRA": env["LRA"], "TP": env["TP"]}
            if len(m):
                st.update(hits=float(np.percentile(m, 95)), floor=float(np.percentile(m, 10)),
                          lines=float(np.percentile(m, 50)))
                st["hits_over_lines"] = st["hits"] - st["lines"]
            self.cache[key] = st
        return self.cache[key]

    def programme(self, video):
        st = self.stats(video, None)
        return {"programme": st["I"], "TP": st["TP"], "LRA": st["LRA"]}


def _note_window(note, tl, co_span, text):
    on_co = note.get("cold_open") or any(p.get("sec") == -1 for p in note.get("pieces") or [])
    if co_span and (on_co or CO_WORDS.search(text or "")):
        return co_span
    t = note.get("t_out") if note.get("t_out") is not None else note.get("t")
    if t is not None:
        return [max(0.0, float(t) - 3.0), float(t) + 3.0]
    return None


def _repeat_issues(qa_report, win):
    for c in (qa_report or {}).get("checks") or []:
        if c.get("id") != "repeats" or c.get("status") in ("pass", "info", None):
            continue
        for i in c.get("issues") or []:
            t = i.get("t")
            if win is None or t is None or (win[0] - 10 <= float(t) <= win[1] + 10):
                return i.get("msg") or "repeat"
    return None


def _clip_issue(qa_report, prog):
    if prog.get("TP") is not None and prog["TP"] > 0.0:
        return f"true peak {prog['TP']:+.1f} dBTP"
    for c in (qa_report or {}).get("checks") or []:
        if "clip" in str(c.get("id") or "") and c.get("status") == "fail":
            return (c.get("issues") or [{}])[0].get("msg") or c["id"]
    return None


def tolerance(row):
    if row["kind"] == "delta":
        return max(1.5, 0.35 * abs(row["claimed"]))
    if row["qty"] == "TP":
        return 0.5
    return max(1.5, 0.15 * abs(row["claimed"])) if row["qty"] in ("swing", "hits_over_lines", "LRA") else 1.5


def check(cfg, texts, video, tl=None, base_video=None, base_tl=None, qa_report=None) -> list:
    """texts: [{note: {id, t, t_out, cold_open}, source, text}] -> measured claim rows (only measurable ones)."""
    from .mapnotes import cold_open_span
    meter = _Meter(cfg)
    co = cold_open_span(tl or {}) if tl else None
    bco = cold_open_span(base_tl or {}) if base_tl else None
    rows = []
    for item in texts or []:
        note = item.get("note") or {}
        for c in parse(item.get("text")):
            win = _note_window(note, tl, co, item.get("text"))
            row = {"note": note.get("id"), "source": item.get("source"), "text": c["text"][:300], "qty": c["qty"],
                   "kind": c["kind"], "claimed": c["claimed"], "unit": c["unit"], "window": win, "measured": None,
                   "ok": None, "why": ""}
            try:
                if c["qty"] == "repeats":
                    iss = _repeat_issues(qa_report, win)
                    named = re.findall(r"\b(?:C\d{3,}|[0-9a-f]{10})\b", c["text"])
                    if iss and named and not any(n in iss for n in named):
                        row.update(ok=None, measured=iss, why=f"QA flags a repeat nearby ({iss}); it does not say "
                                   f"whether it is {', '.join(named[:2])}")
                    else:
                        row.update(ok=iss is None, measured=iss or "no repeat flagged by QA",
                                   why=f"QA repeats: {iss}" if iss else "")
                elif c["qty"] == "clipping":
                    iss = _clip_issue(qa_report, meter.programme(video))
                    row.update(ok=iss is None, measured=iss or "no clipping", why=iss or "")
                elif c["qty"] in ("programme", "TP", "LRA"):
                    m = meter.programme(video)[c["qty"]]
                    row["measured"] = m
                    row["window"] = None
                    if m is None:
                        continue
                    row["ok"] = (m <= c["claimed"] + tolerance(c)) if c["qty"] == "TP" else abs(m - c["claimed"]) <= tolerance(c)
                elif c["kind"] == "delta":
                    if not base_video:
                        continue
                    bwin = (bco if (win == co and bco) else win)
                    m1, m0 = meter.stats(video, win).get(c["qty"]), meter.stats(base_video, bwin).get(c["qty"])
                    if m1 is None or m0 is None:
                        continue
                    d = round(m1 - m0, 2)
                    row.update(measured=d, base=round(m0, 2), new=round(m1, 2))
                    row["ok"] = abs(d - c["claimed"]) <= tolerance(c) and (d * c["claimed"] > 0 or abs(c["claimed"]) < 1.5)
                else:
                    m = meter.stats(video, win).get(c["qty"])
                    if m is None:
                        continue
                    row["measured"] = round(m, 2)
                    row["ok"] = abs(m - c["claimed"]) <= tolerance(c)
            except Exception as e:  # noqa: BLE001 - an unmeasurable claim is skipped, never a crash
                row.update(ok=None, why=f"not measured: {type(e).__name__}: {e}"[:200])
            row["tol"] = round(tolerance(c), 2) if c["kind"] != "none" else None
            if row["ok"] is False and not row["why"]:
                row["why"] = describe(row)
            rows.append(row)
    return rows


NAMES = {"I": "loudness", "programme": "programme loudness", "swing": "quiet-to-hit swing", "hits": "hit level (p95)",
         "floor": "floor (p10)", "lines": "line level (median)", "hits_over_lines": "hits above the lines", "TP": "true peak",
         "LRA": "loudness range", "repeats": "repeats", "clipping": "clipping"}


def _fmt(v, unit):
    return f"{v:+.1f} {unit}".strip() if isinstance(v, (int, float)) else str(v)


def describe(row) -> str:
    n = NAMES.get(row["qty"], row["qty"])
    where = (f" in {row['window'][0]:.1f}-{row['window'][1]:.1f}s" if row.get("window") else "")
    if row["kind"] == "delta":
        return (f"{n}{where}: claimed {_fmt(row['claimed'], row['unit'])} vs the base, measured "
                f"{_fmt(row['measured'], row['unit'])} ({row.get('base')} -> {row.get('new')})")
    if row["kind"] == "none":
        return f"{n}: claimed none, measured {row['measured']}"
    u = "LUFS" if row["unit"] == "LUFS" else row["unit"]
    return f"{n}{where}: claimed {row['claimed']:g} {u}, measured {row['measured']:g} {u}"


def findings(rows, prefix="K") -> list:
    """False claims as critic-shaped HARD findings."""
    out = []
    for i, r in enumerate([r for r in rows if r.get("ok") is False]):
        out.append({"id": f"{prefix}{i + 1}", "sev": "HARD", "t": (r.get("window") or [None])[0], "note": r.get("note"),
                    "rule": "measure-before-claim", "source": "claims",
                    "text": f"{r.get('note') or 'summary'} states a number the render does not show: {describe(r)}. "
                            f"Said: \"{r['text'][:160]}\"",
                    "evidence": f"measured by code on the render ({describe(r)})", "claim": r})
    return out


def texts_of(notes, ledger, summary=None, proofs=(), source="editor") -> list:
    """The texts to check: each ledger row's change + proof (with its note), the summary, the fix proofs."""
    by_id = {n["id"]: n for n in notes or []}
    out = []
    for r in ledger or []:
        n = {**(by_id.get(r.get("note")) or {}), "t_out": r.get("t_out")}
        for k in ("change", "proof"):
            if r.get(k):
                out.append({"note": n, "source": f"{source} {r.get('note')} {k}", "text": r[k]})
    for p in proofs or []:
        if p.get("proof"):
            out.append({"note": {}, "source": f"{source} proof {p.get('finding')}", "text": p["proof"]})
    if summary:
        co = any(n.get("cold_open") for n in notes or [])
        out.append({"note": {"cold_open": co} if co else {}, "source": f"{source} summary", "text": summary})
    return out


def annotate_ledger(ledger, rows) -> list:
    """Card rows carry the measured numbers: true claims get 'measured: ...' on the proof, false ones a
    'NOT on the render' mark on the change."""
    out = []
    for r in ledger or []:
        r = dict(r)
        mine = [x for x in rows if x.get("note") == r.get("note") and str(x.get("source") or "").split()[-1] in ("change", "proof")]
        bad = [x for x in mine if x.get("ok") is False]
        good = [x for x in mine if x.get("ok") is True and x["kind"] != "none"]
        if bad:
            r["change"] = (r.get("change") or "") + " [NOT on the render: " + "; ".join(describe(x) for x in bad[:2]) + "]"
        if good:
            r["proof"] = ((r.get("proof") or "") + " | measured: " + "; ".join(describe(x) for x in good[:2])).strip(" |")
        r["claims"] = [{k: x.get(k) for k in ("qty", "kind", "claimed", "measured", "ok")} for x in mine]
        out.append(r)
    return out
