"""Broadcast WAV (BWF) reading for field recorders (Rode Wireless PRO internal recordings, Zoom, Tascam, Sound Devices).

Pure Python, reads only the chunk headers (never the audio), so it is cheap on multi-GB files:
  fmt   format tag (1 PCM, 3 IEEE float, 0xFFFE extensible + sub-format GUID), channels, rate, bits
  bext  EBU Tech 3285 (v2): Description[256] Originator[32] OriginatorReference[32] OriginationDate[10]
        OriginationTime[8] TimeReferenceLow/High (uint32 LE each: samples since midnight) Version (uint16) ...
  iXML  iXML spec (ixml.info): PROJECT, SCENE, TAKE, TAPE, NOTE, SPEED/TIMECODE_RATE, TIMECODE_FLAG,
        TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI/LO, TIMESTAMP_SAMPLE_RATE, FILE_SET/TOTAL_FILES, FAMILY_UID,
        FILE_SET_INDEX
  LIST/INFO  ISFT (software), IART, ICMT
  ds64  RF64 / BW64 64-bit sizes (files over 4 GB)

`chunk_chains()` joins split recordings (a long take written as several files) into one logical recording:
files of the same device in an iXML file set, or files whose time reference continues sample-exactly where the
previous one ended. sync.py turns each link into an exact sync pair (method "chunk"), so a split take becomes one
piece of a sync group without any audio matching.
"""
import re
import struct
from pathlib import Path

FLOAT_GUID_TAIL = bytes.fromhex("000000001000800000aa00389b71")   # KSDATAFORMAT_SUBTYPE_* tail, first 2 bytes = tag


def _xml_get(xml, tag):
    m = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", xml or "", re.I | re.S)
    return m.group(1) if m else None


def read(path) -> dict:
    """{ok, format: pcm|float, bits, channels, sample_rate, frames, duration, bext{...}, ixml{...}, info{...},
    time_reference (samples since midnight) and its source, rf64}. ok=False for a non-WAV."""
    p = Path(path)
    out = {"ok": False, "path": str(p)}
    try:
        f = open(p, "rb")
    except OSError as e:
        return {**out, "error": str(e)}
    with f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[8:12] != b"WAVE" or hdr[:4] not in (b"RIFF", b"RF64", b"BW64"):
            return out
        rf64 = hdr[:4] != b"RIFF"
        out["rf64"] = rf64
        data_size = None
        fsize = p.stat().st_size
        pos = 12
        while pos + 8 <= fsize:
            f.seek(pos)
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, size = ch[:4], struct.unpack("<I", ch[4:8])[0]
            body_pos = pos + 8
            if cid == b"ds64":
                b = f.read(min(size, 64))
                if len(b) >= 24:
                    data_size = struct.unpack("<Q", b[8:16])[0]
            elif cid == b"fmt ":
                b = f.read(min(size, 64))
                tag, chn, sr, _br, align, bits = struct.unpack("<HHIIHH", b[:16])
                if tag == 0xFFFE and len(b) >= 40:
                    tag = struct.unpack("<H", b[24:26])[0]
                out.update(format={1: "pcm", 3: "float"}.get(tag, f"tag{tag}"), channels=chn, sample_rate=sr,
                           bits=bits, block_align=align)
            elif cid == b"bext":
                b = f.read(min(size, 602))
                if len(b) >= 346:
                    def s(a, n):
                        return b[a:a + n].split(b"\0", 1)[0].decode("latin-1", "replace").strip()
                    lo, hi = struct.unpack("<II", b[338:346])
                    out["bext"] = {"description": s(0, 256), "originator": s(256, 32),
                                   "originator_reference": s(288, 32), "origination_date": s(320, 10),
                                   "origination_time": s(330, 8), "time_reference": (hi << 32) | lo,
                                   "version": struct.unpack("<H", b[346:348])[0] if len(b) >= 348 else None}
            elif cid in (b"iXML", b"IXML"):
                xml = f.read(min(size, 256 * 1024)).decode("utf-8", "replace")
                ix = {}
                for k in ("PROJECT", "SCENE", "TAKE", "TAPE", "NOTE", "TIMECODE_RATE", "TIMECODE_FLAG",
                          "TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI", "TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO",
                          "TIMESTAMP_SAMPLE_RATE", "TOTAL_FILES", "FAMILY_UID", "FILE_SET_INDEX", "FILE_UID"):
                    v = _xml_get(xml, k)
                    if v not in (None, ""):
                        ix[k.lower()] = v
                if "timestamp_samples_since_midnight_lo" in ix:
                    try:
                        ix["time_reference"] = (int(ix.get("timestamp_samples_since_midnight_hi", 0)) << 32) | \
                            int(ix["timestamp_samples_since_midnight_lo"])
                    except ValueError:
                        pass
                m = re.search(r"<(?:MANUFACTURER|DEVICE|RECORDER)[^>]*>\s*([^<]+)<", xml, re.I)
                if m:
                    ix["device"] = m.group(1).strip()
                out["ixml"] = ix
            elif cid == b"LIST":
                b = f.read(min(size, 8192))
                if b[:4] == b"INFO":
                    info, i = {}, 4
                    while i + 8 <= len(b):
                        k, n = b[i:i + 4].decode("latin-1"), struct.unpack("<I", b[i + 4:i + 8])[0]
                        info[k] = b[i + 8:i + 8 + n].split(b"\0", 1)[0].decode("latin-1", "replace").strip()
                        i += 8 + n + (n & 1)
                    out["info"] = info
            elif cid == b"data":
                if size == 0xFFFFFFFF:                      # RF64: the real size is in ds64
                    size = data_size if data_size is not None else max(0, fsize - body_pos)
                size = min(size, max(0, fsize - body_pos))  # a recorder that died mid-take
                out["data_bytes"] = size
            pos = body_pos + size + (size & 1)
    if "sample_rate" not in out:
        return out
    out["ok"] = True
    if out.get("data_bytes") is not None and out.get("block_align"):
        out["frames"] = out["data_bytes"] // out["block_align"]
        out["duration"] = out["frames"] / out["sample_rate"] if out["sample_rate"] else 0.0
    tr, src = None, None
    ix, be = out.get("ixml") or {}, out.get("bext") or {}
    if ix.get("time_reference") is not None:
        tr, src = int(ix["time_reference"]), "ixml"
        ts = ix.get("timestamp_sample_rate")
        if ts and str(ts).isdigit() and int(ts) and int(ts) != out["sample_rate"]:
            tr = int(round(tr * out["sample_rate"] / int(ts)))
    elif be.get("time_reference"):
        tr, src = int(be["time_reference"]), "bext"
    out["time_reference"], out["time_reference_src"] = tr, src
    return out


def tc_string(samples, sr, rate=25.0) -> str:
    """Samples since midnight -> HH:MM:SS:FF at `rate` (non-drop)."""
    secs = samples / float(sr)
    fr = int(round(rate)) or 25
    tot = int(round(secs * fr))
    ff = tot % fr
    s = tot // fr
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{ff:02d}"


def maker(meta) -> str:
    """The recorder's maker/model as far as the file says (bext originator, iXML, INFO software)."""
    bits = [(meta.get("bext") or {}).get("originator"), (meta.get("ixml") or {}).get("device"),
            (meta.get("info") or {}).get("ISFT")]
    return " ".join(b for b in bits if b).strip()


_SPLIT_SUFFIX = re.compile(r"^(?P<base>.*?)[ _-]?(?:\((?P<p>\d{1,3})\)|(?P<n>\d{1,3}))$")


def chunk_chains(items, tol_s=0.06) -> list:
    """items: [{id, path, device, duration, meta (read())}] for ONE library. Returns [[id, id, ...], ...]: each
    list is one logical recording in order (length >= 2). Links when (a) both are in the same iXML file set
    (FAMILY_UID) ordered by FILE_SET_INDEX, or (b) same device, same sample rate, and B's time reference starts
    within tol_s of A's end (sample-contiguous continuation)."""
    chains = []
    fam = {}
    for it in items:
        ix = (it.get("meta") or {}).get("ixml") or {}
        if ix.get("family_uid") and str(ix.get("total_files", "1")) not in ("", "1"):
            fam.setdefault((it.get("device"), ix["family_uid"]), []).append(it)
    used = set()
    for _, lst in fam.items():
        if len(lst) >= 2:
            lst.sort(key=lambda x: int(((x["meta"].get("ixml") or {}).get("file_set_index") or 0) or 0))
            chains.append([x["id"] for x in lst])
            used |= {x["id"] for x in lst}
    # time-reference continuation
    by_dev = {}
    for it in items:
        m = it.get("meta") or {}
        if it["id"] in used or m.get("time_reference") is None or not m.get("sample_rate"):
            continue
        by_dev.setdefault((it.get("device"), m["sample_rate"]), []).append(it)
    for (_, sr), lst in by_dev.items():
        lst.sort(key=lambda x: (x["meta"]["time_reference"], x["path"]))
        cur = [lst[0]]
        for prev, nxt in zip(lst, lst[1:]):
            end = prev["meta"]["time_reference"] + (prev["meta"].get("frames") or round((prev.get("duration") or 0) * sr))
            gap = (nxt["meta"]["time_reference"] - end) / float(sr)
            if abs(gap) <= tol_s:
                cur.append(nxt)
            else:
                if len(cur) >= 2:
                    chains.append([x["id"] for x in cur])
                cur = [nxt]
        if len(cur) >= 2:
            chains.append([x["id"] for x in cur])
    return chains
