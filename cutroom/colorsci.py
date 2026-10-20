"""Colour science for camera matching: published log curves, gamut matrices, 3D LUTs and colour difference.

Only curves and gamuts whose manufacturer specification is public AND was checked against the source are
implemented as maths. Everything else needs the manufacturer's .cube LUT (cameras.py resolves it). Every
constant below cites its source; nothing here is fitted, guessed or rounded from memory.

Code-value convention: every curve is applied to the picture's normalised signal AFTER the usual legal-range
expansion (Y 64..940 -> 0..1, i.e. the RGB an NLE feeds a LUT for a "video levels" clip). Checked on this
machine against two vendor files: Fujifilm's own XT5_FLog2_FGamut_to_WDR_BT.709 LUT reaches black exactly at
input 0.0929 (= F-Log2 black, 95/1023), and DaVinci Resolve's "VFX IO/ARRI LogC to Linear" 1D LUT matches the
ARRI EI 800 formula below to 7 decimals with no range offset.

Sources
  [ARRI]  H. Brendel, "ALEXA Log C Curve - Usage in VFX", ARRI, 09-Mar-2017, Appendix: Log C V3 parameters for
          "conversion between Log C values and exposure values" (EI 800 row), and "ALEXA Wide Gamut RGB" primaries.
          https://www.arri.com/resource/blob/31918/66f56e6abb6e5b6553929edf9aa7483e/2017-03-alexa-logc-curve-in-vfx-data.pdf
  [SONY]  Sony, "Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3" (V1.00), Appendix S-Log3 formula
          and S-Gamut3.Cine chromaticities (also reproduced in colour-science, colour.models.dataset.sony).
  [FUJI]  FUJIFILM, "F-Log2 Data Sheet Ver.1.0", sections 2-3 (conversion formula, F-Log Gamut = ITU-R BT.2020).
          https://dl.fujifilm-x.com/support/lut/F-Log2_DataSheet_E_Ver.1.0.pdf
  [709]   ITU-R BT.709-6, Table "Opto-electronic conversion" / "Primary colours" (R .64/.33, G .30/.60, B .15/.06, D65).
  [2020]  ITU-R BT.2020-2, primaries (R .708/.292, G .170/.797, B .131/.046, D65).
  [1886]  ITU-R BT.1886, reference EOTF L = V^2.4 (Lb = 0).
  [RP177] SMPTE RP 177-1993, derivation of the RGB->XYZ matrix from primaries and white point.
  [CIE15] CIE 15:2004, CIELAB; D65 white (0.3127, 0.3290) per [709].
  [DE00]  G. Sharma, W. Wu, E. Dalal, "The CIEDE2000 Color-Difference Formula: Implementation Notes, Supplementary
          Test Data, and Mathematical Observations", Color Res. Appl. 30(1), 2005.
  [MKL]   F. Pitie, A. Kokaram, "The linear Monge-Kantorovitch linear colour mapping for example-based colour
          transfer", IET CVMP 2007 (closed-form linear map between two colour distributions).

NOT implemented as maths (no checked public spec here; the manufacturer's LUT is required):
  ARRI LogC4 / AWG4, Blackmagic Film Gen 5 / Blackmagic Wide Gamut Gen 5, Fujifilm F-Log (v1), Sony S-Gamut3
  (non-Cine), ARRI's own LogC-to-Rec709 display rendering (K1S1). See docs/NLE_AND_MULTICAM.md "Your kit".
"""
import hashlib
import os
from pathlib import Path

import numpy as np

D65 = (0.3127, 0.3290)                                       # [709]

# ------------------------------------------------------------------------------------------------- curves
# [ARRI] Appendix, Log C V3, "conversion between Log C values and exposure values", EI 800:
#   cut 0.010591, a 5.555556, b 0.052272, c 0.247190, d 0.385537, e 5.367655, f 0.092809 (e*cut+f = 0.149658)
#   lin2log(x) = (x > cut) ? c*log10(a*x+b)+d : e*x+f ;  log2lin(t) = (t > e*cut+f) ? (10^((t-d)/c)-b)/a : (t-f)/e
LOGC3_EI800 = {"cut": 0.010591, "a": 5.555556, "b": 0.052272, "c": 0.247190, "d": 0.385537, "e": 5.367655,
               "f": 0.092809}


def logc3_encode(x, p=LOGC3_EI800):
    x = np.asarray(x, np.float64)
    return np.where(x > p["cut"], p["c"] * np.log10(np.maximum(p["a"] * x + p["b"], 1e-12)) + p["d"], p["e"] * x + p["f"])


def logc3_decode(t, p=LOGC3_EI800):
    t = np.asarray(t, np.float64)
    return np.where(t > p["e"] * p["cut"] + p["f"], (10.0 ** ((t - p["d"]) / p["c"]) - p["b"]) / p["a"], (t - p["f"]) / p["e"])


# [SONY] Appendix: reflection -> S-Log3 (code value / 1023):
#   x >= 0.01125000: (420 + log10((x + 0.01) / (0.18 + 0.01)) * 261.5) / 1023
#   x <  0.01125000: (x * (171.2102946929 - 95) / 0.01125000 + 95) / 1023
# and the inverse (S-Log3 -> reflection):
#   y >= 171.2102946929/1023: 10^((y*1023 - 420) / 261.5) * (0.18 + 0.01) - 0.01
#   else:                     (y*1023 - 95) * 0.01125000 / (171.2102946929 - 95)
def slog3_encode(x):
    x = np.asarray(x, np.float64)
    return np.where(x >= 0.01125, (420.0 + np.log10(np.maximum((x + 0.01) / (0.18 + 0.01), 1e-12)) * 261.5) / 1023.0,
                    (x * (171.2102946929 - 95.0) / 0.01125 + 95.0) / 1023.0)


def slog3_decode(y):
    y = np.asarray(y, np.float64)
    return np.where(y >= 171.2102946929 / 1023.0, 10.0 ** ((y * 1023.0 - 420.0) / 261.5) * (0.18 + 0.01) - 0.01,
                    (y * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0))


# [FUJI] 2-3: a 5.555556, b 0.064829, c 0.245281, d 0.384316, e 8.799461, f 0.092864, cut1 0.000889,
#   cut2 0.100686685370811 ; out = c*log10(a*in+b)+d (in >= cut1) else e*in+f ;
#   inverse: (10^((in-d)/c))/a - b/a (in >= cut2) else (in-f)/e
FLOG2 = {"a": 5.555556, "b": 0.064829, "c": 0.245281, "d": 0.384316, "e": 8.799461, "f": 0.092864,
         "cut1": 0.000889, "cut2": 0.100686685370811}


def flog2_encode(x, p=FLOG2):
    x = np.asarray(x, np.float64)
    return np.where(x >= p["cut1"], p["c"] * np.log10(np.maximum(p["a"] * x + p["b"], 1e-12)) + p["d"], p["e"] * x + p["f"])


def flog2_decode(y, p=FLOG2):
    y = np.asarray(y, np.float64)
    return np.where(y >= p["cut2"], 10.0 ** ((y - p["d"]) / p["c"]) / p["a"] - p["b"] / p["a"], (y - p["f"]) / p["e"])


# ------------------------------------------------------------------------------------------------- gamuts
PRIMARIES = {
    "rec709": ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)),                 # [709]
    "rec2020": ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)),          # [2020]
    "awg3": ((0.6840, 0.3130), (0.2210, 0.8480), (0.0861, -0.1020)),      # [ARRI] ALEXA Wide Gamut RGB, white 0.3127/0.3290
    "sgamut3cine": ((0.766, 0.275), (0.225, 0.800), (0.089, -0.087)),     # [SONY] S-Gamut3.Cine, white D65
    "fgamut": ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)),           # [FUJI] 3: F-Log Gamut = ITU-R BT.2020
}

# [ARRI] "The ALEXA Wide Gamut RGB to CIE 1931 XYZ conversion matrix" (used by the tests to check rgb_to_xyz)
AWG3_TO_XYZ_PUBLISHED = np.array([[0.638008, 0.214704, 0.097744],
                                  [0.291954, 0.823841, -0.115795],
                                  [0.002798, -0.067034, 1.153294]])


def apply3(x, M) -> np.ndarray:
    """(..., 3) colours times a 3x3 (x' = M x) without BLAS: numpy 2.2 on Apple's Accelerate raises spurious
    divide-by-zero / overflow warnings from matmul on large arrays; einsum gives the same numbers quietly."""
    return np.einsum("...j,ij->...i", np.asarray(x, np.float64), np.asarray(M, np.float64))


def rgb_to_xyz(primaries, white=D65) -> np.ndarray:
    """[RP177]: columns = primaries' XYZ scaled so that RGB (1,1,1) maps to the white point with Y = 1."""
    P = np.asarray(primaries, np.float64)
    M = np.vstack([P[:, 0] / P[:, 1], np.ones(3), (1.0 - P[:, 0] - P[:, 1]) / P[:, 1]])
    w = np.array([white[0] / white[1], 1.0, (1.0 - white[0] - white[1]) / white[1]])
    return M * np.linalg.solve(M, w)


def gamut_matrix(src, dst) -> np.ndarray:
    """3x3 linear RGB(src) -> linear RGB(dst), both D65 (no chromatic adaptation needed)."""
    return np.linalg.inv(rgb_to_xyz(PRIMARIES[dst])) @ rgb_to_xyz(PRIMARIES[src])


# ------------------------------------------------------------------------------------------------- profiles
# Which log profiles are maths here (curve decode + gamut). Everything else is LUT-only.
MATH_PROFILES = {
    "arri_logc3": {"decode": logc3_decode, "encode": logc3_encode, "gamut": "awg3", "label": "ARRI LogC3 (EI 800) / AWG3"},
    "sony_slog3": {"decode": slog3_decode, "encode": slog3_encode, "gamut": "sgamut3cine",
                   "label": "Sony S-Log3 / S-Gamut3.Cine"},
    "fuji_flog2": {"decode": flog2_decode, "encode": flog2_encode, "gamut": "fgamut", "label": "Fujifilm F-Log2 / F-Gamut"},
}
LUT_ONLY_PROFILES = {
    "arri_logc4": "ARRI LogC4 / AWG4",
    "bmd_film_gen5": "Blackmagic Film Gen 5 / Wide Gamut Gen 5",
    "fuji_flog": "Fujifilm F-Log / F-Gamut",
    "sony_slog3_sg3": "Sony S-Log3 / S-Gamut3",
    "log_unknown": "log (unknown curve)",
}
DISPLAY_PROFILES = {"rec709": "Rec.709 (display-referred)", "hlg": "HLG (display-referred, treated as video)"}
ALL_PROFILES = {**{k: v["label"] for k, v in MATH_PROFILES.items()}, **LUT_ONLY_PROFILES, **DISPLAY_PROFILES}


def to_logc3(code, profile) -> np.ndarray:
    """Camera code values (..., 3) in `profile` -> ARRI LogC3 EI 800 / AWG3 code values (the matching space)."""
    pr = MATH_PROFILES[profile]
    if profile == "arri_logc3":
        return np.asarray(code, np.float64)
    lin = pr["decode"](code)
    awg = apply3(lin, gamut_matrix(pr["gamut"], "awg3"))
    return logc3_encode(awg)


def logc3_to_display_colorimetric(t) -> np.ndarray:
    """[ARRI] "Colorimetric Information", second approach: invert Log C to scene linear (exposure value, white
    reflector = 1.0), convert ALEXA Wide Gamut RGB to the display's RGB (Rec.709) and correct for the display
    non-linearity ([1886] inverse, V = L^(1/2.4)); values above 1.0 clip. A mathematically defined fallback,
    NOT ARRI's look: with the creator's ARRI LogC3-to-Rec709 LUT the show uses that instead."""
    lin = apply3(logc3_decode(t), gamut_matrix("awg3", "rec709"))
    return np.clip(lin, 0.0, 1.0) ** (1.0 / 2.4)


# ------------------------------------------------------------------------------------------------- 3D LUTs
class Cube:
    """A 3D LUT (.cube, red fastest; or a DaVinci .dat, blue fastest) with trilinear lookup."""

    def __init__(self, size, table, dmin=(0.0, 0.0, 0.0), dmax=(1.0, 1.0, 1.0), title=""):
        self.size = int(size)
        self.t = np.asarray(table, np.float64).reshape(self.size, self.size, self.size, 3)   # [b][g][r]
        self.dmin, self.dmax = np.asarray(dmin, np.float64), np.asarray(dmax, np.float64)
        self.title = title

    @classmethod
    def read(cls, path):
        p = Path(path)
        size, dmin, dmax, rows, title, blue_fast = None, [0.0] * 3, [1.0] * 3, [], "", False
        for ln in p.read_text(errors="replace").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            u = s.upper()
            if u.startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
            elif u.startswith("3DLUTSIZE"):                      # DaVinci .dat: blue changes fastest
                size, blue_fast = int(s.split()[1]), True
            elif u.startswith("LUT_1D_SIZE"):
                raise ValueError(f"{p.name} is a 1D LUT; a 3D LUT is needed")
            elif u.startswith("DOMAIN_MIN"):
                dmin = [float(x) for x in s.split()[1:4]]
            elif u.startswith("DOMAIN_MAX"):
                dmax = [float(x) for x in s.split()[1:4]]
            elif u.startswith("TITLE"):
                title = s[5:].strip().strip('"')
            elif u[0].isalpha():
                continue
            else:
                parts = s.split()
                if len(parts) >= 3:
                    rows.append([float(x) for x in parts[:3]])
        if not size or len(rows) != size ** 3:
            raise ValueError(f"{p.name}: not a valid 3D LUT (size {size}, {len(rows)} entries)")
        t = np.asarray(rows, np.float64)
        if blue_fast:                                           # [r][g][b] -> [b][g][r]
            t = t.reshape(size, size, size, 3).transpose(2, 1, 0, 3).reshape(-1, 3)
        return cls(size, t, dmin, dmax, title or p.stem)

    def apply(self, rgb) -> np.ndarray:
        """Trilinear interpolation (the same maths as ffmpeg lut3d interp=trilinear) of (..., 3) inputs."""
        x = np.asarray(rgb, np.float64)
        sh = x.shape
        x = x.reshape(-1, 3)
        n = self.size - 1
        u = np.clip((x - self.dmin) / np.maximum(self.dmax - self.dmin, 1e-12), 0.0, 1.0) * n
        i0 = np.minimum(np.floor(u).astype(np.int64), n - 1)
        f = u - i0
        r0, g0, b0 = i0[:, 0], i0[:, 1], i0[:, 2]
        fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
        T = self.t
        c000, c100 = T[b0, g0, r0], T[b0, g0, r0 + 1]
        c010, c110 = T[b0, g0 + 1, r0], T[b0, g0 + 1, r0 + 1]
        c001, c101 = T[b0 + 1, g0, r0], T[b0 + 1, g0, r0 + 1]
        c011, c111 = T[b0 + 1, g0 + 1, r0], T[b0 + 1, g0 + 1, r0 + 1]
        c00 = c000 * (1 - fr) + c100 * fr
        c10 = c010 * (1 - fr) + c110 * fr
        c01 = c001 * (1 - fr) + c101 * fr
        c11 = c011 * (1 - fr) + c111 * fr
        c0 = c00 * (1 - fg) + c10 * fg
        c1 = c01 * (1 - fg) + c11 * fg
        return (c0 * (1 - fb) + c1 * fb).reshape(sh)

    @staticmethod
    def grid(size) -> np.ndarray:
        """(size^3, 3) input points in .cube order (red fastest)."""
        g = np.linspace(0.0, 1.0, size)
        b, gg, r = np.meshgrid(g, g, g, indexing="ij")
        return np.stack([r.ravel(), gg.ravel(), b.ravel()], axis=1)

    @classmethod
    def from_function(cls, fn, size=33, title=""):
        pts = cls.grid(size)
        return cls(size, np.clip(fn(pts), 0.0, 1.0), title=title)

    def text(self) -> str:
        lines = [f'TITLE "{self.title[:120]}"', f"LUT_3D_SIZE {self.size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
        flat = self.t.reshape(-1, 3)
        lines += [f"{a:.6f} {b:.6f} {c:.6f}" for a, b, c in flat]
        return "\n".join(lines) + "\n"

    def sha(self) -> str:
        return hashlib.sha1(np.round(self.t, 6).tobytes()).hexdigest()[:16]

    def write(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(self.text())
        os.replace(tmp, path)
        return path


# ------------------------------------------------------------------------------------------------- colour difference
_XYZ709 = rgb_to_xyz(PRIMARIES["rec709"])
_WHITE_XYZ = _XYZ709 @ np.ones(3)


def display_to_lab(v) -> np.ndarray:
    """Rec.709 display code values (0-1) -> CIELAB (D65): [1886] EOTF, [RP177] matrix, [CIE15] Lab."""
    lin = np.clip(np.asarray(v, np.float64), 0.0, 1.0) ** 2.4
    xyz = apply3(lin, _XYZ709) / _WHITE_XYZ
    d = 6.0 / 29.0
    f = np.where(xyz > d ** 3, np.cbrt(xyz), xyz / (3 * d * d) + 4.0 / 29.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def delta_e2000(lab1, lab2) -> np.ndarray:
    """CIEDE2000 [DE00] with kL = kC = kH = 1."""
    L1, a1, b1 = (np.asarray(lab1, np.float64)[..., i] for i in range(3))
    L2, a2, b2 = (np.asarray(lab2, np.float64)[..., i] for i in range(3))
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2.0
    G = 0.5 * (1.0 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25.0 ** 7)))
    a1p, a2p = (1.0 + G) * a1, (1.0 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.where((a1p == 0) & (b1 == 0), 0.0, np.degrees(np.arctan2(b1, a1p)) % 360.0)
    h2p = np.where((a2p == 0) & (b2 == 0), 0.0, np.degrees(np.arctan2(b2, a2p)) % 360.0)
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dh = np.where(C1p * C2p == 0, 0.0, np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh)))
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh) / 2.0)
    Lbp = (L1 + L2) / 2.0
    Cbp = (C1p + C2p) / 2.0
    hs = h1p + h2p
    hbp = np.where(C1p * C2p == 0, hs, np.where(np.abs(h1p - h2p) <= 180, hs / 2.0,
                                                np.where(hs < 360, (hs + 360) / 2.0, (hs - 360) / 2.0)))
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dtheta = 30.0 * np.exp(-(((hbp - 275.0) / 25.0) ** 2))
    Rc = 2.0 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25.0 ** 7))
    Sl = 1 + 0.015 * (Lbp - 50) ** 2 / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))


# ------------------------------------------------------------------------------------------------- matching
LUMA709 = np.array([0.2126, 0.7152, 0.0722])                # [709] luminance weights


def sat_matrix(s) -> np.ndarray:
    """Saturation about Rec.709 luminance in linear light: x' = Y + s (x - Y)."""
    return s * np.eye(3) + (1.0 - s) * np.outer(np.ones(3), LUMA709)


def _mid(lin):
    """Pixels usable for statistics: not clipped, not in the noise floor (display-linear 0.002..0.85)."""
    lin = lin.reshape(-1, 3)
    m = (lin.min(1) > 0.002) & (lin.max(1) < 0.85)
    return lin[m] if m.sum() > 64 else lin


def fit_match(src_disp, ref_disp, matrix=False, bounds=None) -> dict:
    """Fit a correction that moves `src` frames toward `ref` frames of the same scene (different angles, so no
    pixel correspondence): per-channel gains from the trimmed means (white balance + exposure), then a
    saturation factor from mean chroma, optionally a full 3x3 from [MKL] blended half-way toward that model.
    Works in display-linear light (Rec.709 code ^ 2.4). Returns {matrix (3x3), gains, exposure_stops, sat, ...}."""
    b = {"gain": (0.5, 2.0), "sat": (0.75, 1.35), **(bounds or {})}
    s = _mid(np.clip(np.asarray(src_disp, np.float64), 0, 1) ** 2.4)
    r = _mid(np.clip(np.asarray(ref_disp, np.float64), 0, 1) ** 2.4)

    def tmean(x):
        lo, hi = np.percentile(x, [5, 95], axis=0)
        return np.array([x[(x[:, i] >= lo[i]) & (x[:, i] <= hi[i]), i].mean() for i in range(3)])
    ms, mr = tmean(s), tmean(r)
    g = np.clip(mr / np.maximum(ms, 1e-6), *b["gain"])
    s2 = s * g

    def chroma(x):
        y = np.einsum("...j,j->...", x, LUMA709)
        return float(np.mean(np.abs(x - y[:, None]).sum(1) / np.maximum(y, 1e-4)))
    sat = float(np.clip(chroma(r) / max(chroma(s2), 1e-6), *b["sat"]))
    M = sat_matrix(sat) @ np.diag(g)
    out = {"gains": [round(float(v), 4) for v in g], "sat": round(sat, 4),
           "exposure_stops": round(float(np.log2(max(1e-6, (g @ LUMA709)))), 3), "method": "gains+sat"}
    if matrix:
        A = mk_linear(s, r)
        if A is not None:
            M = 0.5 * M + 0.5 * A
            out["method"] = "gains+sat+mk3x3"
    out["matrix"] = [[round(float(v), 6) for v in row] for row in M]
    return out


def mk_linear(src, ref):
    """[MKL] closed form: T = Cs^-1/2 (Cs^1/2 Cr Cs^1/2)^1/2 Cs^-1/2, applied about the means folded into a
    3x3 that maps the source mean onto the reference mean along the same line (no offset). None if degenerate."""
    def msqrt(C):
        w, V = np.linalg.eigh(C)
        if w.min() <= 1e-12:
            return None
        return V @ np.diag(np.sqrt(w)) @ V.T
    Cs, Cr = np.cov(src.T), np.cov(ref.T)
    Ch = msqrt(Cs)
    if Ch is None:
        return None
    inner = msqrt(Ch @ Cr @ Ch)
    if inner is None:
        return None
    Ci = np.linalg.inv(Ch)
    T = Ci @ inner @ Ci
    if not np.all(np.isfinite(T)) or np.linalg.cond(T) > 50:
        return None
    return T


def apply_display_matrix(disp, M) -> np.ndarray:
    """Apply a display-linear 3x3 to Rec.709 code values (decode ^2.4, matrix, encode ^1/2.4, clip)."""
    lin = np.clip(np.asarray(disp, np.float64), 0, 1) ** 2.4
    return np.clip(apply3(lin, M), 0, 1) ** (1 / 2.4)
