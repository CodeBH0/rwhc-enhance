"""Parse historical PQ grayscale calibration runs from hc.log.

The calibration flow (`app.py::calibrate_pq`) measures the native PQ response
of the display at a set of grayscale codes and logs one line per point::

    HH:MM:SS [INFO] (i/N) ... RGB: [c, c, c] ... XYZ: [x y z] ... RGB: [...]

(Chinese: ``(i/N) 写入 RGB: [c, c, c] 测得 XYZ: [x y z] RGB: [...]``,
English:  ``(i/N) Output RGB: [c, c, c] Measured XYZ: [x y z] RGB: [...]``)

A *run* is a consecutive sequence ``(1/N) .. (N/N)`` with the same N.  Only
complete runs (all N points present, no gaps) are returned, newest first.

The raw measured XYZ is the display's *native* response: it does not depend
on the calibration target white point, so a run measured at one color
temperature can be reused for another (only the white-point Bradford
adaptation changes).  See `app.py::calibrate_pq`.
"""

import os
import re
from datetime import datetime, timedelta

import numpy as np

# (1/128) 写入 RGB: [0, 0, 0] 测得 XYZ: [0.085102 0.092339 0.126244] RGB: [58.37 ...]
# (1/128) Output RGB: [0, 0, 0] Measured XYZ: [0.085102 0.092339 0.126244] RGB: [58.37 ...]
_POINT_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}) \[INFO\] \((\d+)/(\d+)\) .*?"
    r"RGB: \[(\d+), (\d+), (\d+)\].*?"
    r"XYZ: \[([^\]]+)\]"
)
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")


def _parse_xyz(s):
    return np.array([float(x) for x in s.split()], dtype=float)


def parse_gray_runs(log_path="hc.log"):
    """Return complete PQ grayscale runs from hc.log.

    Each run::

        {
            "num":    int,          # number of points (N)
            "start":  datetime,     # first point timestamp
            "end":    datetime,     # last point timestamp (measurement end)
            "points": [(code, np.ndarray([x, y, z])), ...],  # in code order
        }

    Runs are sorted newest-first by end time.  The log stores only HH:MM:SS;
    dates are inferred by anchoring the last timestamped line to the file
    mtime and walking backwards across midnight wraps.
    """
    if not os.path.isfile(log_path):
        return []
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    # Assign a date to every timestamped line.
    times = []  # (line_idx, (h, m, s))
    for idx, ln in enumerate(lines):
        m = _TIME_RE.match(ln)
        if m:
            times.append((idx, (int(m.group(1)), int(m.group(2)), int(m.group(3)))))

    day_offsets = {}
    day = 0
    prev = None
    for idx, t in times:
        if prev is not None and t < prev:
            day += 1
        day_offsets[idx] = day
        prev = t

    if not times:
        return []
    max_day = day_offsets[times[-1][0]]
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(log_path))
    except OSError:
        mtime = datetime.now()
    base = mtime.date() - timedelta(days=max_day)

    def line_dt(idx, t):
        d = day_offsets.get(idx, 0)
        return datetime(base.year, base.month, base.day, t[0], t[1], t[2]) + timedelta(days=d)

    runs = []
    cur = None
    for idx, ln in enumerate(lines):
        m = _POINT_RE.match(ln)
        if not m:
            continue
        h, mi, s, ist, nst, r, g, b, xyzstr = m.groups()
        i, n = int(ist), int(nst)
        code = int(r)
        xyz = _parse_xyz(xyzstr)
        if len(xyz) != 3:
            continue
        t = (int(h), int(mi), int(s))
        dt = line_dt(idx, t)

        if cur is None:
            cur = {"num": n, "start": dt, "end": dt, "points": []}
        elif i == 1:
            # new run started -> close previous (may be incomplete)
            runs.append(cur)
            cur = {"num": n, "start": dt, "end": dt, "points": []}
        elif not (i == len(cur["points"]) + 1 and n == cur["num"]):
            # index gap or N changed -> close current, start fresh
            runs.append(cur)
            cur = {"num": n, "start": dt, "end": dt, "points": []}

        if cur["num"] == n:
            cur["points"].append((code, xyz))
            cur["end"] = dt

    if cur is not None:
        runs.append(cur)

    complete = [r for r in runs if len(r["points"]) == r["num"]]
    complete.sort(key=lambda r: r["end"], reverse=True)
    return complete
