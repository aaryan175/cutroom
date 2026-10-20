#!/usr/bin/env python3
"""Make ProRes stand-ins for Blackmagic RAW clips with DaVinci Resolve's render queue (cutroom can't decode .braw).

    python3 tools/resolve_braw_proxies.py --work ~/EditRoom             # every .braw clip without a stand-in
    python3 tools/resolve_braw_proxies.py --files A001_0614_C003.braw ... --out ~/EditRoom/raw_proxies

Writes <work>/raw_proxies/<clip name>.mov (ProRes 422 Proxy, source resolution capped at 1920 wide, same frame
rate, audio included) through Resolve's documented scripting API (README.txt in Resolve's Developer/Scripting
folder: ProjectManager.CreateProject, MediaPool.ImportMedia / CreateTimelineFromClips, Project.SetRenderSettings,
SetCurrentRenderFormatAndCodec, SetCurrentRenderMode(0 = individual clips), AddRenderJob, StartRendering).
`cutroom index` then finds the stand-ins and indexes the clips through them.

Needs: DaVinci Resolve RUNNING, and scripting allowed (Preferences > System > General > External scripting using:
Local). Blackmagic documents external scripting for DaVinci Resolve Studio; the free version may refuse the
connection. Then use the free version's own buttons instead (cutroom braw prints them). The decode uses the clip's
own BRAW settings (as shot: Blackmagic Design Film for a log workflow), so the stand-in carries the same
Film / Gen 5 picture that cutroom's Blackmagic transform (luts/bmd.cube) expects.

Nothing is written next to the footage; the Resolve project is named cutroom_braw_proxies and can be deleted.
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"


def resolve_app():
    os.environ.setdefault("RESOLVE_SCRIPT_API", API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", LIB)
    sys.path.append(os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules"))
    try:
        import DaVinciResolveScript as dvr  # noqa: N813 - Blackmagic's module name
    except ImportError as e:
        sys.exit(f"Resolve scripting module not found ({e}); is DaVinci Resolve installed?")
    r = dvr.scriptapp("Resolve")
    if r is None:
        sys.exit("Could not connect to DaVinci Resolve. Start Resolve, allow external scripting (Preferences > System > "
                 "General > External scripting using: Local). The free version may not allow it: then use Generate "
                 "Proxy Media or a Deliver render by hand (see `cutroom braw`).")
    return r


def braw_without_standin(work):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cutroom.config import Config
    from cutroom import cameras as CAM
    cfg = Config.load(work)
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row
    rep = CAM.raw_report(cfg, con)
    return cfg, [con.execute("SELECT path FROM clips WHERE id=?", (r["clip"],)).fetchone()[0]
                 for r in rep["braw"] if not r["standin"]]


def pick_codec(project):
    fmts = project.GetRenderFormats() or {}
    fmt = next((k for k, v in fmts.items() if str(v).lower() == "mov"), None) or "QuickTime"
    codecs = project.GetRenderCodecs(fmt) or {}
    want = next((v for k, v in codecs.items() if "proxy" in k.lower() and "prores" in k.lower()), None) \
        or next((v for k, v in codecs.items() if "prores 422 lt" in k.lower()), None) \
        or next((v for k, v in codecs.items() if "prores" in k.lower()), None)
    if not want:
        sys.exit(f"no ProRes codec offered by Resolve for {fmt}: {list(codecs)}")
    return fmt, want


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", help="cutroom project (finds .braw clips without a stand-in)")
    ap.add_argument("--files", nargs="*", default=[], help=".braw files")
    ap.add_argument("--out", help="output folder (default <work>/raw_proxies)")
    ap.add_argument("--width", type=int, default=1920)
    a = ap.parse_args()
    files = [str(Path(f).resolve()) for f in a.files]
    out = a.out
    if a.work:
        cfg, more = braw_without_standin(a.work)
        files += more
        out = out or str(Path(cfg.work) / "raw_proxies")
    if not files:
        print("nothing to do: no .braw clips without a stand-in")
        return
    if not out:
        sys.exit("--out (or --work) is needed")
    Path(out).mkdir(parents=True, exist_ok=True)
    resolve = resolve_app()
    pm = resolve.GetProjectManager()
    by_rate = {}
    for f in files:                                       # a Resolve project has one frame rate: one project per rate
        by_rate.setdefault(probe_rate(f), []).append(f)
    jobs_all = []
    for rate, group in sorted(by_rate.items()):
        pname = f"cutroom_braw_proxies_{rate:g}".replace(".", "_")
        proj = pm.CreateProject(pname) or pm.LoadProject(pname)
        if not proj:
            sys.exit(f"could not create or open the Resolve project {pname}")
        # keys as used in Blackmagic's own scripting examples (1_sorted_timeline_from_folder.py)
        proj.SetSetting("timelineFrameRate", f"{rate:g}")
        proj.SetSetting("timelineResolutionWidth", str(a.width))
        proj.SetSetting("timelineResolutionHeight", str(int(round(a.width * 9 / 16 / 2)) * 2))
        mp = proj.GetMediaPool()
        items = mp.ImportMedia(group) or []
        if not items:
            print(f"  ! Resolve imported nothing for {group}")
            continue
        fmt, codec = pick_codec(proj)
        jobs = []
        for it in items:
            name = Path(it.GetClipProperty("File Name") or it.GetName()).stem   # "File Name": Blackmagic example 5
            tl = mp.CreateTimelineFromClips(f"proxy_{name}_{int(time.time())}", [it])
            if not tl:
                print(f"  ! could not make a timeline for {name}")
                continue
            proj.SetCurrentTimeline(tl)
            proj.SetCurrentRenderFormatAndCodec(fmt, codec)
            proj.SetCurrentRenderMode(1)                  # single clip: one file per timeline, named by us
            proj.SetRenderSettings({"SelectAllFrames": True, "TargetDir": out, "CustomName": name,
                                    "ExportVideo": True, "ExportAudio": True})
            jid = proj.AddRenderJob()
            if jid:
                jobs.append((jid, name))
        if not jobs:
            continue
        proj.StartRendering([j for j, _ in jobs])
        while proj.IsRenderingInProgress():
            time.sleep(2)
        for jid, name in jobs:
            st = proj.GetRenderJobStatus(jid) or {}
            print(f"  {name}: {st.get('JobStatus')} ({st.get('CompletionPercentage')} %)")
        jobs_all += jobs
    if not jobs_all:
        sys.exit("no render jobs ran")
    print(f"stand-ins in {out}; now run: cutroom index")


def probe_rate(path) -> float:
    """The clip's frame rate from ffprobe (the BRAW container is plain QuickTime)."""
    import json
    import subprocess
    from fractions import Fraction
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                            "-of", "json", path], capture_output=True, text=True, timeout=60)
        q = Fraction(json.loads(r.stdout)["streams"][0]["r_frame_rate"])
        return round(float(q), 3)
    except Exception:  # noqa: BLE001
        return 25.0


if __name__ == "__main__":
    main()
