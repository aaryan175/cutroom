"""Regression: the shot detector against a frame-exact hand-checked cut list of a real 24 fps brand film (118 s,
2,834 frames: tracking shots with strobes photographed in them, very dark low-key B&W, a dark-to-dark B&W -> teal
cut, a cut hidden behind a dark out-of-focus foreground pass, a subject jumping over the lens into black).

The film itself is third-party and is never in the repo: the test runs only when the file is on this machine
(CUTROOM_DC06, or cutroom-refs/DC06-w1u3ki/ref.mp4 next to the repo) and skips otherwise. Only the measured cut frames live here."""
import os
from pathlib import Path

import pytest

REF = Path(os.environ.get("CUTROOM_DC06") or Path(__file__).resolve().parents[2] / "cutroom-refs" / "DC06-w1u3ki" / "ref.mp4")
FPS = 24.0
# first frame of every new shot (43 cuts, 44 shots), checked frame by frame
TRUTH = [19, 159, 283, 311, 342, 363, 373, 377, 388, 466, 542, 659, 819, 890, 942, 988, 1053, 1141, 1229, 1283,
         1324, 1386, 1441, 1802, 1822, 1915, 2153, 2174, 2207, 2228, 2242, 2257, 2268, 2278, 2290, 2297, 2311, 2321,
         2327, 2332, 2349, 2713, 2780]
# what an older detector got wrong (each must stay fixed)
HARD_MISSES = [283, 1441, 2207]            # jump-out on the same view, cut behind a dark foreground, dark B&W -> teal
FALSE_BEFORE = [844, 1068, 1372, 2076, 2143]   # light drift / dark tilt / murky room / smoke / subject over the lens

pytestmark = pytest.mark.skipif(not REF.exists(), reason="DC06 reference film not on this machine")


@pytest.fixture(scope="module")
def detected(tmp_path_factory):
    pytest.importorskip("cv2")
    from cutroom import governor
    from cutroom import reference as R
    from cutroom.config import init_project
    base = tmp_path_factory.mktemp("dc06")
    (base / "lib").mkdir()
    cfg = init_project(base / "work", [base / "lib"])
    governor.reset()
    arr = R.scan_video(cfg, REF, FPS, base / "scan.npz", label="dc06")
    return R.find_shots(arr, FPS), int(arr["n"][0])


def test_dc06_cut_list_is_frame_exact(detected):
    r, n = detected
    assert n == 2834
    cuts = r["cuts"]
    missed = [t for t in TRUTH if not any(abs(t - c) <= 1 for c in cuts)]
    false = [c for c in cuts if not any(abs(t - c) <= 1 for t in TRUTH)]
    assert not missed and not false, f"missed {missed}, false {false}"
    exact = sum(1 for t in TRUTH if t in cuts)
    assert exact == len(TRUTH), f"{len(TRUTH) - exact} cuts off by one frame"


def test_dc06_former_failures_stay_fixed(detected):
    r, _ = detected
    cuts = set(r["cuts"])
    assert all(f in cuts for f in HARD_MISSES)
    assert not any(abs(f - c) <= 1 for f in FALSE_BEFORE for c in cuts)
    assert r["gradual"] == []                  # the film has no dissolves: all 43 transitions are hard cuts
