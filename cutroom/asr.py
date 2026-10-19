"""Speech: word-timestamp transcription (faster-whisper) plus the word-level helpers the planner
and renderer use — tightening pauses, caption cues, director cues ("okay cut"), repeated takes and
overused words.

Transcripts live in <work>/transcripts/<id>.json:
    {"language", "model", "speech_s", "segments": [{"start", "end", "text",
                                       "words": [{"start", "end", "word", "probability"}]}]}

Run ONE transcription at a time: the model is loaded once per process and clips are processed
sequentially (parallel ASR on a 16 GB machine is a reliable way to exhaust memory).
"""
import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import db

SR = 16000
MIN_SPEECH_S = 1.5

# phrases an operator/creator says to the camera crew, not to the audience
DIRECTOR_PHRASES = [
    "are we rolling", "let's go again", "lets go again", "let us go again", "one more time", "take two",
    "take 2", "okay cut", "ok cut", "alright cut", "all right cut", "and cut", "cut", "rolling", "recording",
]

STOPWORDS = set("""
a an the and or but if so then than that this these those to of in on at by for with from as is are was
were be been being am i you he she it we they me him her us them my your his its our their mine yours
do does did done have has had not no yes just like really very um uh oh okay ok yeah so well get got
go going gonna wanna can could would should will shall may might must what which who whom whose when
where why how all any some there here up down out over about into onto off too also only own same
more most other such s t don dont im it's i'm you're that's there's we're they're can't won't didn't
""".split())


# --------------------------------------------------------------------------------------------- audio
def load_audio(cfg, path, sr=SR, start=None, dur=None, timeout=None) -> np.ndarray:
    """Decode the first audio stream of `path` to mono float32 at `sr` via ffmpeg (pipe, no temp file)."""
    cmd = [getattr(cfg, "ffmpeg", "ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, float(start)):.3f}"]
    cmd += ["-i", str(path)]
    if dur is not None:
        cmd += ["-t", f"{float(dur):.3f}"]
    cmd += ["-vn", "-map", "0:a:0?", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"ffmpeg audio decode failed for {path}: {r.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def _energy_speech_spans(audio, sr=SR, frame_s=0.03):
    """Fallback voice activity: speech-band frames well above the clip's own noise floor."""
    n = int(sr * frame_s)
    if len(audio) < n:
        return []
    frames = audio[: len(audio) // n * n].reshape(-1, n)
    # crude speech-band emphasis: first difference removes rumble
    d = np.diff(frames, axis=1)
    rms = 20 * np.log10(np.sqrt(np.mean(d ** 2, axis=1)) + 1e-9)
    floor = np.percentile(rms, 15)
    act = (rms > max(floor + 12, -50)).astype(float)
    # smooth over ~0.3 s so syllable gaps don't fragment speech
    k = max(1, int(0.3 / frame_s))
    act = np.convolve(act, np.ones(k) / k, mode="same") > 0.5
    spans, s = [], None
    for i, a in enumerate(act):
        if a and s is None:
            s = i
        elif not a and s is not None:
            spans.append((s * frame_s, i * frame_s)); s = None
    if s is not None:
        spans.append((s * frame_s, len(act) * frame_s))
    return spans


def speech_spans(audio, sr=SR):
    """List of (start, end) seconds of likely speech. Uses faster-whisper's Silero VAD when available."""
    if len(audio) == 0:
        return []
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=100,
                                                    min_speech_duration_ms=250), sampling_rate=sr)
        return [(t["start"] / sr, t["end"] / sr) for t in ts]
    except Exception:  # noqa: BLE001 - any VAD failure falls back to energy
        return _energy_speech_spans(audio, sr)


# --------------------------------------------------------------------------------------------- model
_MODEL = {}


def _device():
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:  # noqa: BLE001
        pass
    return "cpu", "int8"


def get_model(name):
    if name not in _MODEL:
        from faster_whisper import WhisperModel
        _MODEL.clear()                                   # never hold two models in memory
        dev, ct = _device()
        kw = {"cpu_threads": min(8, os.cpu_count() or 4)} if dev == "cpu" else {}
        _MODEL[name] = WhisperModel(name, device=dev, compute_type=ct, **kw)
    return _MODEL[name]


def _fmt_eta(s):
    s = int(max(0, s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _source(c):
    """Proxy if present (smaller to decode), else the original."""
    p = c["proxy"]
    return p if p and Path(p).exists() else c["path"]


def transcribe_clip(cfg, con, c, model_name, audio=None, speech_s=None):
    """Transcribe one clip row and store JSON + `transcripts` row. Returns the row dict."""
    if audio is None:
        audio = load_audio(cfg, _source(c))
    model = get_model(model_name)
    segs, info = model.transcribe(
        audio, language=(cfg.asr_language or None), word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        condition_on_previous_text=False, beam_size=5)
    out = []
    for s in segs:
        if s.no_speech_prob > 0.8 and s.avg_logprob < -1.0:    # hallucination guard
            continue
        ws = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word,
               "probability": round(w.probability, 3)} for w in (s.words or [])]
        if not ws and not s.text.strip():
            continue
        out.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(), "words": ws})
    n_words = sum(len(s["words"]) for s in out)
    sp = speech_s if speech_s is not None else sum(s["end"] - s["start"] for s in out)
    return _store(cfg, con, c["id"], info.language, model_name, out, n_words, sp)


def _store(cfg, con, cid, language, model_name, segments, n_words, speech_s):
    path = cfg.p("transcripts", f"{cid}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"language": language, "model": model_name, "speech_s": round(speech_s, 2),
                               "segments": segments}, ensure_ascii=False))
    os.replace(tmp, path)
    _WORDS.pop(cid, None)
    row = {"clip_id": cid, "path": str(path), "language": language, "n_words": int(n_words),
           "speech_s": round(float(speech_s), 2), "text": " ".join(s["text"] for s in segments),
           "model": model_name, "updated": time.time()}
    db.upsert(con, "transcripts", row, key=("clip_id",))
    con.commit()
    return row


def _vad_cached(cfg, c):
    """(speech_s, spans) for a clip, cached in <work>/transcripts/<id>.vad.json."""
    vp = cfg.p("transcripts", f"{c['id']}.vad.json")
    src = _source(c)
    if vp.exists():
        try:
            d = json.loads(vp.read_text())
            if d.get("src") == str(src):
                return d["speech_s"], d["spans"]
        except Exception:  # noqa: BLE001
            pass
    audio = load_audio(cfg, src)
    spans = speech_spans(audio)
    sp = float(sum(b - a for a, b in spans))
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text(json.dumps({"src": str(src), "speech_s": round(sp, 2), "spans": [[round(a, 2), round(b, 2)] for a, b in spans]}))
    return sp, spans


def transcribe_all(cfg, con, limit=None, model=None, only=None):
    """Transcribe every clip with audio that is not yet transcribed with `model`.

    Phase 1 runs a cheap VAD over each pending clip; clips with < 1.5 s of likely speech get an
    empty transcript (n_words=0) so they are never retried. Phase 2 transcribes the rest, most
    speech first, one at a time.
    """
    model_name = model or cfg.asr_model
    if only:
        rows = [db.get_clip(con, only)]
    else:
        rows = con.execute("SELECT * FROM clips WHERE has_audio=1 AND COALESCE(duration,0)>0 "
                           "AND status NOT IN ('new') ORDER BY path").fetchall()
    done = {r["clip_id"] for r in con.execute("SELECT clip_id FROM transcripts WHERE model=?", (model_name,))}
    pending = [c for c in rows if c["id"] not in done]
    if only and not pending:
        print(f"{rows[0]['rel'] or rows[0]['path']}: already transcribed with {model_name}")
        return []
    print(f"speech check: {len(pending)} clip(s) pending ({len(rows) - len(pending)} already done with {model_name})")

    todo, t0 = [], time.time()
    for i, c in enumerate(pending, 1):
        try:
            sp, _ = _vad_cached(cfg, c)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {c['rel'] or c['path']}: {e}", file=sys.stderr)
            continue
        if sp < MIN_SPEECH_S:
            _store(cfg, con, c["id"], None, model_name, [], 0, sp)
        else:
            todo.append((sp, c))
        if i % 25 == 0 or i == len(pending):
            print(f"  vad {i}/{len(pending)} · {len(todo)} with speech · {time.time() - t0:.0f}s", flush=True)

    todo.sort(key=lambda x: -x[0])
    if limit:
        todo = todo[:limit]
    total_audio = sum(c["duration"] or 0 for _, c in todo) or 1.0
    done_audio, t0, results = 0.0, time.time(), []
    for i, (sp, c) in enumerate(todo, 1):
        name = c["rel"] or Path(c["path"]).name
        ts = time.time()
        try:
            row = transcribe_clip(cfg, con, c, model_name, speech_s=sp)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        results.append(row)
        done_audio += c["duration"] or 0
        el = time.time() - t0
        eta = el / done_audio * (total_audio - done_audio) if done_audio else 0
        print(f"  [{i}/{len(todo)}] {name}: {row['n_words']} words, {sp:.0f}s speech, "
              f"{time.time() - ts:.1f}s · ETA {_fmt_eta(eta)}", flush=True)
    return results


# --------------------------------------------------------------------------------------------- words
_WORDS = {}


def words(cfg, cid):
    """[(start, end, word), ...] for a clip, cached in-process (invalidated by file mtime)."""
    p = cfg.p("transcripts", f"{cid}.json")
    if not p.exists():
        return []
    m = p.stat().st_mtime
    hit = _WORDS.get(cid)
    if hit and hit[0] == m:
        return hit[1]
    d = json.loads(p.read_text())
    ws = [(float(w["start"]), float(w["end"]), str(w["word"]).strip())
          for s in d.get("segments", []) for w in s.get("words", []) if str(w["word"]).strip()]
    _WORDS[cid] = (m, ws)
    return ws


def _within(ws, a, b):
    """Words whose midpoint lies in [a, b]."""
    return [w for w in ws if a <= (w[0] + w[1]) / 2 <= b]


def tighten(words_list, a, b, gap=0.35, pad=0.08, min_piece=0.8):
    """Split source range [a, b] into spoken pieces, removing pauses longer than `gap`.
    Returns [(a_i, b_i), ...] in source seconds. No words in range -> [(a, b)]."""
    ws = _within(words_list, a, b)
    if not ws:
        return [(a, b)]
    spans = []
    cur = max(a, ws[0][0] - pad)
    last = ws[0]
    for w in ws[1:]:
        if w[0] - last[1] > gap:
            spans.append([cur, min(b, last[1] + pad)])
            cur = max(a, w[0] - pad, spans[-1][1])
        last = w
    spans.append([cur, min(b, last[1] + pad + 0.1)])
    # merge fragments shorter than min_piece into the previous piece (first one into the next)
    out = []
    for s in spans:
        if out and (s[1] - s[0] < min_piece):
            out[-1][1] = s[1]
        else:
            out.append(s)
    if len(out) > 1 and out[0][1] - out[0][0] < min_piece:
        out[1][0] = out[0][0]
        out.pop(0)
    return [(round(x, 3), round(y, 3)) for x, y in out if y > x]


_NUM_TIME = [(re.compile(r"\b(\d{1,2}) ?[.:] (\d{2})\b"), r"\1:\2"),
             (re.compile(r"\b(\d{1,2}) [.:](\d{2})\b"), r"\1:\2")]


def clean_text(t, fixes=()):
    for pat, rep in _NUM_TIME:
        t = pat.sub(rep, t)
    t = re.sub(r"\s+([,.!?;:%])", r"\1", t)
    for f in fixes or ():
        pat, rep = f[0], f[1]
        t = re.sub(pat, rep, t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def cues(words_list, a, b, max_words=5, max_chars=30, fixes=()):
    """Caption lines for source range [a, b]: [(t0, t1, text)] with times relative to `a`."""
    ws = _within(words_list, a, b)
    groups, line = [], []
    for w in ws:
        if line:
            chars = len(" ".join(x[2] for x in line + [w]))
            if len(line) + 1 > max_words or chars > max_chars or w[0] - line[-1][1] > 0.4:
                groups.append(line); line = []
        line.append(w)
        if re.search(r"[.!?]['\")\]]*$", w[2]):
            groups.append(line); line = []
    if line:
        groups.append(line)
    out = []
    for g in groups:
        t0 = max(0.0, g[0][0] - a)
        t1 = min(g[-1][1] - a + 0.2, b - a)
        out.append([t0, t1, clean_text(" ".join(x[2] for x in g), fixes)])
    for i in range(len(out) - 1):
        out[i][1] = min(out[i][1], out[i + 1][0] - 0.02)
    return [(round(t0, 3), round(t1, 3), t) for t0, t1, t in out if t1 - t0 >= 0.15 and t]


def _norm(w):
    return re.sub(r"[^\w']+", "", w.lower()).replace("’", "'")


def director_cues(words_list):
    """[(t0, t1, phrase)] for crew-directed phrases ("okay cut", "rolling", "take two" ...).
    A lone "cut" only counts when it stands apart (pause or sentence break on both sides)."""
    toks = [(_norm(w[2]), w) for w in words_list]
    phrases = sorted((p.split() for p in DIRECTOR_PHRASES), key=len, reverse=True)
    out, i = [], 0
    while i < len(toks):
        hit = None
        for ph in phrases:
            n = len(ph)
            if [t for t, _ in toks[i:i + n]] == ph:
                hit = ph
                break
        if hit:
            first, last = toks[i][1], toks[i + len(hit) - 1][1]
            ok = True
            if hit == ["cut"]:
                prev = toks[i - 1][1] if i > 0 else None
                nxt = toks[i + 1][1] if i + 1 < len(toks) else None
                before = prev is None or first[0] - prev[1] > 0.3 or re.search(r"[.!?,]$", prev[2])
                after = nxt is None or nxt[0] - last[1] > 0.5 or re.search(r"[.!?]$", last[2])
                ok = bool(before and after)
            if ok:
                out.append((first[0], last[1], " ".join(hit)))
                i += len(hit)
                continue
        i += 1
    return out


def sentences(words_list, gap=0.7):
    """Group words into sentences on terminal punctuation or long pauses: [(t0, t1, text)]."""
    out, cur = [], []
    for w in words_list:
        if cur and w[0] - cur[-1][1] > gap:
            out.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.!?]['\")\]]*$", w[2]):
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return [(s[0][0], s[-1][1], " ".join(x[2] for x in s)) for s in out]


def repeats(words_list, window_s=90, threshold=0.7):
    """Sentences said twice within `window_s`: [((a0, a1), (b0, b1), similarity)]. Keep the later take (b)."""
    sents = [(t0, t1, [_norm(x) for x in txt.split() if _norm(x)]) for t0, t1, txt in sentences(words_list)]
    sents = [s for s in sents if len(s[2]) >= 3]
    out = []
    for i, (a0, a1, ta) in enumerate(sents):
        for b0, b1, tb in sents[i + 1:]:
            if b0 - a1 > window_s:
                break
            r = difflib.SequenceMatcher(None, ta, tb).ratio()
            if r >= threshold:
                out.append(((round(a0, 3), round(a1, 3)), (round(b0, 3), round(b1, 3)), round(r, 3)))
    return out


def overused(words_list, a, b, per_s=60, min_count=3):
    """Non-stopwords said >= min_count times within any `per_s` window inside [a, b]:
    [(word, count, [times...])] sorted by count."""
    occ = {}
    for w in _within(words_list, a, b):
        n = _norm(w[2])
        if len(n) < 3 or n in STOPWORDS:
            continue
        occ.setdefault(n, []).append(round(w[0], 2))
    out = []
    for word, ts in occ.items():
        best, j = 0, 0
        for i in range(len(ts)):
            while ts[i] - ts[j] > per_s:
                j += 1
            best = max(best, i - j + 1)
        if best >= min_count:
            out.append((word, best, ts))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def _mmss(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def print_transcript(cfg, cid, start=None, end=None):
    p = cfg.p("transcripts", f"{cid}.json")
    if not p.exists():
        print(f"no transcript for {cid} (run `cutroom transcribe --clip {cid}`)")
        return
    d = json.loads(p.read_text())
    lo, hi = start or 0.0, end if end is not None else float("inf")
    segs = [s for s in d.get("segments", []) if s["end"] >= lo and s["start"] <= hi]
    print(f"# {cid} · {d.get('language')} · {d.get('model')} · {sum(len(s['words']) for s in segs)} words")
    if not segs:
        print("  (no speech)")
    for s in segs:
        print(f"[{_mmss(s['start'])} → {_mmss(s['end'])}] {s['text']}")
    ws = [w for w in words(cfg, cid) if lo <= w[0] <= hi]
    dc = director_cues(ws)
    if dc:
        print("\ndirector cues (trim these):")
        for t0, t1, ph in dc:
            print(f"  [{_mmss(t0)} → {_mmss(t1)}] {ph!r}")
    rp = repeats(ws)
    if rp:
        print("\nrepeated takes (keep the later one):")
        for (a0, a1), (b0, b1), r in rp:
            print(f"  [{_mmss(a0)} → {_mmss(a1)}] ≈ [{_mmss(b0)} → {_mmss(b1)}]  sim {r:.2f}")
