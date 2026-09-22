# -*- coding: utf-8 -*-
"""Parse historical colour calibration runs from hc.log.

The colour half of a calibration (``app.py::measure_gamut_before`` +
``calibrate_chromaticity``) writes everything it needs into ``hc.log``.  This
module turns those lines back into *complete* colour runs that a later
calibration can reuse, so the primaries / white / black measurement and the
colour-card measurement do not have to be repeated.

What a "colour run" contains
----------------------------
1. Gamut test points, logged one line each by ``measure_gamut_before``::

       HH:MM:SS [INFO] Color red measured XYZ: [133.15 55.98 7.81]
       (Chinese: ``HH:MM:SS [INFO] 颜色 red 的实测 XYZ：[133.15 55.98 7.81]``)

   Keys: ``red``, ``green``, ``blue``, ``white``, ``white_paper``, ``black``.
   These are the **raw** XYZ (nits) of the panel — they are *not* white-point
   adapted, because they are measured before the calibration ICC is written.

2. The activated-black binary search::

       HH:MM:SS [INFO] Start binary search for activated black: start_lumi=...
       HH:MM:SS [INFO] Gray test code=255 RGB=[255, 255, 255] measured XYZ: [...]
       HH:MM:SS [INFO] Activated black found: code=31 XYZ=[...]
       HH:MM:SS [INFO] No significant luminance increase found ...; skipping ...

3. The colour-card run, logged by ``calibrate_chromaticity``::

       HH:MM:SS [INFO] Start color measurement and generate matrix
       HH:MM:SS [INFO] Color sample set: sRGB(24), 25 samples
       HH:MM:SS [INFO] (1/25) Color: [374 337 306] Target XYZ:[...] Measured: [...]
       ...

4. A ``measure_gamut_after`` block follows (4 gamut points + ``Gamut
   measurement finished``).  Those primaries are measured *with* the
   calibration LUT active, so they are NOT interchangeable with step 1 —
   the parser therefore only accepts a run that it started at an explicit
   "Calibration started" marker, and closes it at the matching "Gamut
   measurement finished".

Why reuse is valid
------------------
Windows applies the calibration THROUGH the ICC profile that is currently
installed.  A run recorded here was taken with the temporary ``hdr_empty.icc``
preview (identity MHC2 matrix, flat LUT) — i.e. the panel's *native* response,
and ``calibrate_chromaticity`` does not white-point adapt its measurements
either.  The same display therefore reproduces the same numbers in a later
session for a different target white point, which is exactly what makes
reusing them possible.  (The historical grey runs follow the same reasoning;
see ``gray_history.py``.)

Only complete runs are returned: all six gamut keys plus at least one colour
sample card entry, otherwise the run was cancelled or the log was rotated.
"""

import os
import re
from datetime import datetime

import numpy as np

from gray_history import timestamped_line_dates

#: Gamut keys written by ``measure_gamut_before`` / ``measure_gamut_after``.
GAMUT_KEYS = ("red", "green", "blue", "white", "white_paper", "black")

#: Colours that a reusable run must contain (``black`` is required too: the
#: peak/black luminance written into MHC2 depends on it).
_REQUIRED_GAMUT_KEYS = GAMUT_KEYS

# (2) 颜色 red 的实测 XYZ：[133.155106  55.988922   7.812743]
# (2) Color red measured XYZ: [133.155106  55.988922   7.812743]
# Note: "measured XYZ" / "的实测 XYZ" is deliberately spelled out — matching a
# bare "XYZ" would also catch the activated-black probes and the PQ points.
_GAMUT_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:颜色|Color) (\w+) (?:的实测|measured) XYZ[：:]\s*\[([^\]]+)\]"
)

# (2) 开始二分搜索激活黑位：start_lumi=0.091026 delta=0.00091026
# (2) Start binary search for activated black: start_lumi=0.091026 delta=0.0009
_BLACK_SEARCH_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:开始二分搜索激活黑位|Start binary search for activated black)"
)

# (2) 灰阶测试代码=255 RGB=[255, 255, 255] 实测 XYZ：[9.552067 10.342036 9.625589]
# (2) Gray test code=255 RGB=[255, 255, 255] measured XYZ: [9.552067 10.342036 9]
_BLACK_PROBE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:灰阶测试代码=(\d+)|Gray test code=(\d+))"
    r".*?XYZ[：:]\[([^\]]+)\]"
)

# (2) 激活黑: code=31 XYZ=[0.113964 0.113404 0.16702 ]
# (2) Activated black level found: code=31 XYZ=[0.113964 0.113404 0.16702 ]
# Note the mixed colon usage in app.py: the Chinese line prints "激活黑:" with
# an ASCII colon, and the XYZ list is opened with ASCII "[" as well.
_ACTIVATED_BLACK_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] .*?(?:激活黑|Activated black).*?"
    r"code=(\d+)\s+XYZ=\s*\[([^\]]+)\]"
)

# (2) 开始颜色测量并生成矩阵 / Start color measurement and generate matrix
_COLOR_START_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:开始颜色测量并生成矩阵|Start color measurement and generate matrix)"
)

# (2) Color sample set: sRGB(24), 25 samples
_SAMPLE_SET_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] Color sample set: (?P<mode>[^,]+), (?P<num>\d+) samples"
)

# (2) (0.04) 颜色：[374 337 306] 目标 XYZ：[0.00178 ...] 实测：[0.00223 ...]
# (2) (1/25) Color: [374 337 306] Target XYZ:[0.00178 ...] Measured: [0.00223 ...]
_SAMPLE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] \((?P<idx>[^)]*)\) .*?(?:颜色|Color)[：:]?\s*"
    r"\[(?P<rgb>[\d\s,]+)\]\s*(?:目标|Target)\s*XYZ[：:]\s*\[(?P<target>[^\]]+)\]\s*"
    r"(?:实测|Measured)[：:]\s*\[(?P<measured>[^\]]+)\]"
)

# (2) Calibration started / 已开始校准
_CALIB_START_RE = re.compile(r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:已开始校准|Calibration started)\s*$")

# (2) Gamut measurement finished / 色域测量完成
_GAMUT_DONE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:色域测量完成|Gamut measurement finished)\s*$"
)

# (2) Selected screen HDR state: hdr / 所选屏幕 HDR 状态：hdr
_HDR_STATE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] .*?(?:所选屏幕 HDR 状态|Selected screen HDR state)[：:]\s*(\S+)"
)

# (2) SDR paper white: 160.0 nits, white test patch code: 569
_PAPER_WHITE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] SDR paper white: ([\d.]+) nits, white test patch code: (\d+)"
)

# (2) Color measurement white point: [0.3127, 0.3290]        <- new (optional)
_WHITE_POINT_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] Color measurement white point: \[([^\]]+)\]"
)

# (2) Application started / 应用程序已启动  (used to bound a run that crashed)
_APP_START_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] (?:应用程序已启动|Application started)\s*$"
)


def _parse_xyz(s):
    """Parse ``"1.0 2.0 3.0"`` (or comma separated) into an ndarray."""
    return np.array([float(x) for x in re.split(r"[,\s]+", s.strip()) if x], dtype=float)


def _parse_white_point(s):
    """Parse ``"0.3127,0.3290"`` into ``[0.3127, 0.3290]``; None if malformed."""
    try:
        vals = [float(x.strip()) for x in re.split(r"[,\s]+", s) if x.strip()]
    except ValueError:
        return None
    return vals if len(vals) == 2 else None


def _close_run(run, runs):
    """Finish a run: keep it only when it is genuinely complete.

    A run is usable only if every gamut key was measured AND the colour card
    produced at least one sample — a cancelled calibration is missing one of
    the two and must not be offered for reuse.
    """
    if run is None:
        return
    if len(run["points"]) == 0:
        run["incomplete_reason"] = "no colour-card samples"
        return
    missing = [k for k in _REQUIRED_GAMUT_KEYS if run["gamut"].get(k) is None]
    if missing:
        run["incomplete_reason"] = "missing gamut points: " + ", ".join(missing)
        return
    runs.append(run)


def parse_color_runs(log_path="hc.log"):
    """Return complete colour calibration runs from ``hc.log``, newest first.

    Each run::

        {
            "start":            datetime,   # "Calibration started"
            "end":              datetime,   # colour card finished
            "gamut":            {key: np.ndarray([x, y, z]), ...},  # raw XYZ (nits)
            "min_activated_black": np.ndarray([x, y, z]) | None,
            "white_patch_code": int | None,
            "paper_white_nits": float | None,
            "hdr_state":        str | None,
            "white_point":      [x, y] | None,   # only for logs that record it
            "sample_mode":      str | None,      # "sRGB(24)", ...
            "points":           [(idx, np.array(rgb), np.array(target), np.array(measured)), ...],
        }

    ``start``/``end`` carry a date as well: the log only stores ``HH:MM:SS``,
    so dates are inferred by anchoring the last timestamped line to the log
    file mtime and walking backwards across midnight wraps (shared helper
    ``gray_history.timestamped_line_dates``).
    """
    if not os.path.isfile(log_path):
        return []
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    dated = timestamped_line_dates(lines, log_path)

    runs = []
    run = None
    in_black_search = False

    for idx, ln in enumerate(lines):
        dt = dated.get(idx)

        if _CALIB_START_RE.match(ln) or _APP_START_RE.match(ln):
            # A new calibration (or a fresh process) begins: anything still open
            # was cancelled or crashed — close it, it will be dropped as incomplete.
            _close_run(run, runs)
            run = None
            in_black_search = False
            if _CALIB_START_RE.match(ln):
                run = _new_run(dt)
            continue

        if run is None:
            continue

        m = _HDR_STATE_RE.match(ln)
        if m:
            run["hdr_state"] = m.group(1)
            _touch(run, dt)
            continue

        m = _PAPER_WHITE_RE.match(ln)
        if m:
            run["paper_white_nits"] = float(m.group(1))
            run["white_patch_code"] = int(m.group(2))
            _touch(run, dt)
            continue

        m = _GAMUT_RE.match(ln)
        if m:
            key = m.group(1)
            if key in run["gamut"] and not in_black_search:
                run["gamut"][key] = _parse_xyz(m.group(2))
                _touch(run, dt)
            continue

        if _BLACK_SEARCH_RE.match(ln):
            in_black_search = True
            _touch(run, dt)
            continue

        if in_black_search:
            m = _BLACK_PROBE_RE.match(ln)
            if m:
                _touch(run, dt)
                continue
            m = _ACTIVATED_BLACK_RE.match(ln)
            if m:
                xyz = _parse_xyz(m.group(2))
                if len(xyz) == 3:
                    run["min_activated_black"] = xyz
                _touch(run, dt)
                continue
            # "No significant ... skipping" line also only updates the timestamp.

        if _COLOR_START_RE.match(ln):
            run["card_started"] = True
            _touch(run, dt)
            continue

        m = _WHITE_POINT_RE.match(ln)
        if m:
            wp = _parse_white_point(m.group(1))
            if wp is not None:
                run["white_point"] = wp
            _touch(run, dt)
            continue

        m = _SAMPLE_SET_RE.match(ln)
        if m:
            run["sample_mode"] = m.group("mode").strip()
            run["sample_declared"] = int(m.group("num"))
            _touch(run, dt)
            continue

        m = _SAMPLE_RE.match(ln)
        if m:
            rgb = _parse_xyz(m.group("rgb"))
            target = _parse_xyz(m.group("target"))
            measured = _parse_xyz(m.group("measured"))
            if rgb.size == 3 and target.size == 3 and measured.size == 3:
                run["points"].append((m.group("idx").strip(), rgb, target, measured))
                _touch(run, dt)
            continue

        if _GAMUT_DONE_RE.match(ln):
            # First "Gamut measurement finished" after a colour card belongs to
            # measure_gamut_after -> the run is over.  The colour card itself is
            # written straight after the gamut block, so a second one (or a PQ
            # step) never follows inside the same run.
            if run["card_started"]:
                _touch(run, dt)
                _close_run(run, runs)
                run = None
                in_black_search = False
            continue

    _close_run(run, runs)
    runs.sort(key=lambda r: r["end"], reverse=True)
    return runs


def _new_run(dt):
    return {
        "start": dt,
        "end": dt,
        "gamut": {k: None for k in GAMUT_KEYS},
        "min_activated_black": None,
        "white_patch_code": None,
        "paper_white_nits": None,
        "hdr_state": None,
        "white_point": None,
        "sample_mode": None,
        "sample_declared": None,
        "card_started": False,
        "points": [],
        "incomplete_reason": None,
    }


def _touch(run, dt):
    if dt is not None:
        run["end"] = dt


# ======================================================================
# 复用：把历史 run 还原成 app.py 的状态
# ======================================================================

def gamut_xyz_from_run(run):
    """Historical run -> the ``measure_gamut_xyz`` dict used everywhere else.

    ``min_activated_black`` falls back to the plain black measurement, which is
    exactly what ``measure_gamut_before`` does when the binary search finds no
    significant increase.
    """
    out = {k: np.asarray(run["gamut"][k], dtype=float).copy() for k in GAMUT_KEYS}
    mab = run.get("min_activated_black")
    out["min_activated_black"] = (
        np.asarray(mab, dtype=float).copy() if mab is not None else out["black"].copy()
    )
    return out


def peak_min_luminance_from_xyz(gamut_xyz, eetf=False, eetf_args=None):
    """Peak/black luminance rules shared by a live run and a reused run.

    ``max_lumi`` is the measured white, or the EETF monitor maximum when the
    user capped the display's peak in the EETF dialog; ``min_lumi`` is the
    measured black, or 0 under EETF.  Keeping this in one place guarantees a
    reused run produces byte-identical MHC2 / ``lumi`` data to measuring it.
    """
    max_lumi = float(np.asarray(gamut_xyz["white"], dtype=float)[1])
    min_lumi = float(np.asarray(gamut_xyz["black"], dtype=float)[1])
    if eetf:
        args = eetf_args or {}
        m_max = args.get("monitor_max")
        m_min = args.get("monitor_min")
        if m_max is not None and m_max != 10000:
            max_lumi = float(m_max)
        if m_min is not None and m_min != 0:
            min_lumi = 0.0
    return max_lumi, min_lumi


def peak_min_luminance(run, eetf=False, eetf_args=None):
    """Same as `peak_min_luminance_from_xyz`, for a parsed run."""
    return peak_min_luminance_from_xyz(run["gamut"], eetf, eetf_args)


def measured_and_target_xyz(run):
    """Colour-card samples -> ``(measured_xyz, target_xyz)`` in app units.

    The log stores what ``calibrate_chromaticity`` appended to
    ``self.measured_xyz`` / ``self.target_xyz``: the meter reading already
    divided by 10000, and the target XYZ as computed from the gamut points.
    Both are returned in that same unit (absolutely scaled XYZ, 1.0 = 10000
    nits) so a reused run is numerically identical to a fresh measurement.
    """
    measured = [np.asarray(meas, dtype=float) for _i, _rgb, _tgt, meas in run["points"]]
    target = [np.asarray(tgt, dtype=float) for _i, _rgb, tgt, _meas in run["points"]]
    return measured, target


def run_label(run):
    """Short human-readable label for a run (timestamp + colour-card size)."""
    n = len(run["points"])
    mode = run.get("sample_mode")
    end = run["end"].strftime("%m-%d %H:%M:%S") if run.get("end") else "?"
    if mode:
        return f"{end} · {n} pts · {mode}"
    return f"{end} · {n} pts"


def run_summary(run):
    """Multi-line summary used when a run is selected (for the log)."""
    lines = []
    end = run["end"].strftime("%Y-%m-%d %H:%M:%S") if run.get("end") else "?"
    lines.append(f"colour run {end}: {len(run['points'])} colour-card samples")
    if run.get("sample_mode"):
        lines.append(f"  sample set: {run['sample_mode']}")
    if run.get("white_patch_code") is not None:
        lines.append(
            "  paper white: {} nits (white patch code {})".format(
                run.get("paper_white_nits"), run["white_patch_code"]
            )
        )
    if run.get("white_point"):
        lines.append("  white point at measurement time: {}".format(run["white_point"]))
    w = np.asarray(run["gamut"]["white"], dtype=float)
    wp = np.asarray(run["gamut"]["white_paper"], dtype=float)
    b = np.asarray(run["gamut"]["black"], dtype=float)
    lines.append("  white Y={:.1f} nit, paper-white Y={:.1f} nit, black Y={:.4f} nit".format(
        w[1], wp[1], b[1]))
    return "\n".join(lines)
