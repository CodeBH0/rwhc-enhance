# Changelog

> This project is developed by an maintainer who is not a professional in color
> science, so the calibration logic may not be optimal. If you find issues or
> have better ideas, contributions and feedback are very welcome.
> [中文版](CHANGELOG_zh.md)

All notable changes are listed here, newest first. The format is plain on
purpose — see the README for how to use the program.

---

## [2026-09-22] — v2026.09.22 (GitHub release)

> **Release:** tag `v2026.09.22`, packaged as `rwhc-v2026.09.22.zip` on the
> [Releases](https://github.com/CodeBH0/rwhc-enhance/releases) page. Previous release:
> `v2026.09.02`. Main additions since then: CLUT (`A2B0`/`B2A0`) ICC output, historical
> colour data reuse, calibration without a colorimeter, the rewritten MHC2 LUT inverse,
> and the "Save ICC file" freeze fix.

- **The MHC2 1D LUT is now built by a real inverse of the measured PQ response.**
  `lut.generate_mhc2_lut_from_measured_pq()` used to resample the measured curve to
  40960 points and then look up the nearest sample for every target value; it now
  inverts the (output PQ → input code) relation directly with a piecewise-linear
  interpolation (`np.interp`). The function's docstring states the convention it
  implements (`lut[i] = f⁻¹(i/(N-1))`).
  - **Honest scope note**: this was *review-driven*, not a visible bug fix. Measured
    on all 15 real grayscale runs in `hc.log`, inside the display's reachable range
    the new and old LUTs differ by **mean 0.006 / max 0.013 of a 10-bit code**
    (~1e-5 PQ) — well below the panel's own run-to-run repeatability. So the old
    nearest-neighbour lookup did **not** produce visible stepping or a measurable
    brightness error; what the rewrite buys is an implementation that actually
    matches the mathematical definition, no dependence on the "resample length
    divides evenly" trick, no mutation of the caller's array, and correct behaviour
    on plateau / constant / NaN / tiny input.
  - **The one behavioural change is at the top end.** The panel's brightest point is
    often *not* the last sample (real data: peak at code ≈ 814, PQ 0.80057, then it
    rolls off to 0.79955 at code 1023 because of ABL/power limiting). Beyond that
    peak the response is many-to-one, so the monotone inverse must stay constant —
    every target above the measured peak now maps to **the peak's code value**. A
    naive "clamp to 1.0" (what the review suggested) would have sent codes that
    measurably produce *less* light across the whole highlight range, so it is
    deliberately not done; the reasoning is written into the docstring.
  - `lut.generate_mhc2_lut_from_measure_data()` (the old `real_nit`-based builder,
    reachable only through its dead `eetf_args` branch) is **deleted**: nothing in
    the shipped app called it, and it carried the `convert_idx[0] = 0; convert_idx[1] = 1`
    endpoint hack that is not a valid inverse response. `eetf_from_lut()` (used by
    `tools/cyberpunk2077_hdr_fixer.py`) is kept and its docstring now records that
    EETF does not apply to the calibration path.
  - New verification tool **`tools/verify_lut_inverse.py`** (36 checks, no display or
    colorimeter): math properties (monotone, range, endpoints, no in-place mutation,
    degenerate/NaN/single-point input), inverse accuracy against a verbatim copy of
    the old algorithm, top-end saturation semantics, the same checks on a real
    `hc.log` run, and edge cases (plateaus, noisy roll-off, 6-point input, linear
    response). Run it with `python tools/verify_lut_inverse.py`.

- **Fixed the "Save ICC file" freeze.** With the CLUT enabled, saving ran the
  `B2A0` inverse solve synchronously on the Tk main thread, so the window stopped
  repainting and responding for the whole computation — minutes at a large grid
  (measured: 17³ ≈ 5 s, 25³ ≈ 17 s, 33³ ≈ 37 s, and 45³/65³ scale roughly with
  the node count, i.e. minutes). The window looked hung and could be reported as
  not responding by Windows.
  - `write_clut_if_enabled` was split into a pure-computation step
    (`_make_clut_tags`, which also owns the cache) and a reporting step
    (`_report_clut`); the ICC write stays on the main thread.
  - `generate_and_save_icc` now freezes the controls, runs only the tag
    computation on a worker thread, and writes/`rebuild()`/`save()`s the profile
    and installs the ICC back on the main thread in the callback. Save without
    CLUT keeps the original synchronous path (no slow step).
  - The log now announces the wait before it starts, with the chosen grid:
    `Generating CLUT for saving: 33³ grid, this can take a while (seconds to minutes)…`
    and `ICC profile saved: <path>` afterwards.
  - Verified responsiveness with a Tk heartbeat probe: while saving a 33³ CLUT
    the main loop is blocked for at most **0.21 s** (was ~37 s and up, i.e. a hard
    freeze). The ICC produced through the worker path is **byte-identical**
    (same sha256) to the one the synchronous path produces, and the tags are
    written to/read from the profile identically.
- **Made the CLUT inverse solve ~1.6× faster** (`clut_icc.DisplayModel._candidate_devices`).
  - The first nearest-neighbour stage built a full `(n, seed_grid³, 3)`
    difference array (`sum((fwd - t[:, None])**2, axis=2)`); at 33³ that is a
    ~8.6 GB-scale round of temporaries. It now uses the mathematically identical
    norm expansion `|a|² − 2·a·b + |b|²`, i.e. one BLAS matrix multiply. Output is
    unchanged to the last bit (verified: `max|Δ| = 1.1e-16` against the
    per-point path, which is unchanged).
  - The probe offsets `meshgrid(fine, fine, fine)` were recomputed on every
    chunk; they are now built once.
  - Fixed a latent shape bug in the second stage: `pw.reshape(probe.shape)`
    only works because the point count happens to factor as
    `per_row * refine_grid³`. Any other call size (a single point, a short
    batch, a different `seed_grid`) would have raised or silently mismatched.
    The reshape is now explicit (`e - s, probe_rows`) and the chunk size is
    chosen so the shape is always right.
  - `refine_grid` was measured to contribute only ~14% of the cost, and
    lowering it (7 → 5 → 3) changes neither the closed-loop error nor the
    unclipped accuracy, so it was left at its default rather than traded for
    speed.
- `tools/verify_clut_app_integration.py` updated for the split
  (`_make_clut_tags` / `_report_clut` are now also AST-extracted).
- Small `i18n` additions for the new messages (`messages_zh.po`), plus
  translations for a few strings that had been left empty.

- **Calibration now works with no colorimeter connected** when both history
  dropdowns point at past measurements (a grayscale run + a colour run).
  - `calibrate_monitor` used to construct `spotread` unconditionally, so with no
    instrument attached the run aborted with
    `RuntimeError: spotread exit unexpectedly` before doing anything. The meter
    (and the `dogegen` pattern generator, and the "place the colorimeter"
    dialog) are now only started when the calibration actually needs to measure
    something — i.e. not when `_history_only_calibration()` is true.
  - `measure_gamut_after` (the post-calibration gamut re-measurement) is skipped
    in that mode too: it only re-derives the same `rXYZ`/`gXYZ`/`bXYZ`/`wtpt`,
    `lumi` and `MHC2` peak/black tags from the same primaries, so reusing them
    yields an identical profile. Logged as
    `Historical color data reused: skipped the post-calibration gamut re-measurement`.
  - `measure_pq` ("Measure color accuracy") still needs a real instrument, but
    now says so (`No colorimeter detected...`) instead of failing with a
    `RuntimeError` traceback.
  - New log line `Historical data is enough: no colorimeter needed for this
    calibration`.
- New verification section D in `tools/verify_color_history.py` (11 more checks,
  40 total): it replaces `ColorWriter`, `ColorReader` and the message boxes with
  stubs that raise on any use, then runs the real `calibrate_monitor` — so a pass
  proves no hardware is touched and no dialog is shown. It also checks that
  `hc.log` is not grown by the run (the tool redirects the app's log handler to a
  temporary file and restores the log to its original length).

- **Historical colour data** (`color_history.py`). The counterpart of the
  existing "Historical gray data": a past calibration's colour measurements can
  now be reused instead of re-measured.
  - Parses complete colour runs out of `hc.log`: the six gamut test points
    (red/green/blue/white/white-paper/black), the activated-black binary search
    and the whole colour-card run, with both Chinese and English log formats
    supported. A run is only offered when all six gamut points *and* at least
    one colour-card sample are present, so cancelled calibrations never appear.
  - Verified that reuse is sound: those measurements are taken **before** the
    calibration ICC is written (identity-matrix, flat-LUT preview profile) and
    `calibrate_chromaticity` does not white-point adapt them, so they record the
    panel's native response at that code value — independent of the target white
    point, exactly like the grey runs. Every colour run in the current `hc.log`
    also used the default D65 white point (`WhitePoint` field untouched), i.e.
    no adaptation at all, so the existing runs are directly reusable.
  - New GUI dropdown "Historical color data" (labelled with end time, sample
    count and sample set) plus a "Refresh" button, next to the grey one. The
    list is also refreshed automatically after a calibration.
  - Reuse skips the gamut measurement, the activated-black search and the
    colour-card measurement, then feeds the profile/`MHC2` writes through the
    **same** code path a live run uses (`_apply_gamut_data`, extracted from
    `measure_gamut_before`), so a reused run produces an identically built
    profile. The peak/black luminance rules were factored into
    `color_history.peak_min_luminance_from_xyz` for the same reason.
  - `calibrate_chromaticity` now logs the white point it used
    (`Color measurement white point: ...`) so a future run's data is
    self-describing. Runs from older logs simply have `white_point: None`.
  - `calibrate_chromaticity` also keeps the reused card's target/measured XYZ in
    `target_xyz`/`measured_xyz`, so the state after a reuse matches a live run.
- **The reused colour data is now actually fed into the CLUT path.**
  - The reused primaries/white point go through `primaries_matrix()` into
    `DisplayModel.xyz_matrix`, which is the CLUT's `A2B0` direction — same as a
    live measurement, no separate branch.
  - The reused colour-card samples are attached to the model, and
    `DisplayModel.color_accuracy_report()` / `color_accuracy_summary()` compare
    the model's forward prediction (absolute nits) against them, reporting
    mean/median/max ΔE ITP, mean |ΔXYZ| in nits and the worst sample.
    `write_clut_if_enabled` logs the summary as
    `CLUT colour check (A2B vs measured card): ...` and warns when the mean
    ΔE ITP exceeds a deliberately loose 15 — this is a **model
    self-consistency** indicator, not an absolute accuracy figure: the panel's
    own run-to-run repeatability already shows up here (measured on the
    maintainer's display: repeated gray runs 8–40 min apart differ by up to
    0.016 PQ, and comparing the colour card against a model built from a gray
    run measured 2.4 h earlier gives mean ΔE ITP ≈ 149 / mean |ΔXYZ| ≈ 98 nit).
    The docstring of `color_accuracy_report` states this explicitly. The card
    data is a verification set only — it never changes the CLUT that the
    measured primaries + `MHC2` already determine.
  - Note on the metric's other limit: the model's absolute luminance scale
    depends on the "MHC2 LUT axis = device code 0..1" convention that
    `DisplayModel.device_to_linear` and `generate_mhc2_lut_from_measured_pq`
    share with the CLUT's own self-check. A colour comparison is unaffected in
    its *chromaticity* direction, but a different LUT-index convention would
    scale absolute brightness. Documented rather than assumed away; resolving it
    needs on-display verification.
  - `build_model_from_calibration(..., measured_colors=...)` and
    `DisplayModel(..., measured_colors=...)` are new optional arguments;
    existing callers are unaffected.
- New verification tool `tools/verify_color_history.py` (29 checks, no display or
  colorimeter needed): real-`hc.log` parsing invariants (completeness, ordering,
  luminance ordering black < paper white < peak white, sample magnitudes, shared
  date basis with the grey history, EETF peak/black rules), the ΔE ITP maths
  (perfect data → 0 error; 0.002 code jitter → sub-nit / mean ΔE ITP 0.43; a 5%
  primaries chromaticity perturbation → detectable), and the `app.py` reuse path
  (AST-extracted `_measured_color_samples` / `_make_display_model`, reuse card
  units, model matrix identical to the direct construction, A2B CLUT from reused
  data matching the reference model).
- `tools/verify_clut_app_integration.py` updated for the extra method
  (`_measured_color_samples`) now needed by `_make_display_model`.
- `gray_history.py`: the log date-inference helper was extracted into
  `timestamped_line_dates()` and is now shared with `color_history.py`, so both
  dropdowns show the same dates. `parse_gray_runs` behaviour is unchanged
  (re-verified with the same log).
- Main window default height 1000 → 1050 px, so the new history row does not
  squeeze the log frame (the requested height grew to ~1042 px).
- READMEs document the feature (new numbered note 7, CLUT note referenced) and
  the new self-check tool; the feature is also documented in both changelogs.

- **CLUT (multi-dimensional LUT) ICC output** (`clut_icc.py`). In addition to the
  existing matrix (+ `MHC2`) path, the generated ICC can now also carry the
  standard ICC v4 lookup tables:
  - `A2B0` (device PQ code → PCS) and `B2A0` (PCS → device PQ code), written as
    `lut16Type`/`mft2` (format `mAB`/`mBA` is implemented and self-tested too).
  - Grid size selectable in the GUI (17/25/33/37/45/65, default 33). A new
    checkbox "CLUT (A2B0/B2A0)" enables it; default is off, so existing
    behavior is unchanged.
  - The CLUT is derived **only** from data this calibration already measured —
    measured primaries/white point, the `MHC2` per-channel curves and peak/black
    luminance. No extra measurement is required.
  - Device space is `RGB` + ST 2084 (PQ); the PCS is peak-relative XYZ
    (brightest achievable white = `Y 1.0`), the same convention DisplayCAL's
    XYZLUT monitor profiles use. All CLUT values stay inside ICC's [0,1] range,
    so no extension encoding (`psd`/`hlc`/PCC) or non-standard PCS signature is
    needed.
  - The matrix tags and `MHC2` are left completely intact; the CLUT is purely
    additive.
  - Accuracy (synthetic-display self-test, 33³): `A2B0` mean interpolation error
    ≈ 0.005%, max <1% (worst case right at the PCS ceiling); `B2A0` is exact for
    unclipped colors (median closed-loop error ≈ 0.002%). Inside the clipped
    highlight region the inverse is inherently many-to-one — this is a property
    of the relative-colorimetry PCS, not of the implementation, and it is
    documented in both READMEs.
  - Verified against a real DisplayCAL-generated "XYZLUT+MTX 37" Argyll profile
    for structural reference (33³/37³ `mft2` CLUTs, peak-relative PCS).
- New verification tools (no display or colorimeter needed):
  - `tools/verify_clut_profile.py` — 25 checks: tag structure round-trip, 16-bit
    fixed-point accuracy, tetrahedral interpolation accuracy, A2B↔B2A
    round-trip, CLUT axis-order self-check, real ICC write+re-read (MHC2 and
    matrix tags verified intact), mft2↔mAB cross-check.
  - `tools/verify_clut_app_integration.py` — 13 checks over the real `app.py`
    methods (extracted via AST so the test exercises the shipped code without
    pulling in the GUI/wexpect dependencies), including cache behavior.
- **Fixed three startup blockers that made the app fail to launch.**
  - `app.py`'s `build_ui()` referenced the bare name `root` in five places
    (lines 185/214/225/237/501 — the top bar, intro label, separator, button
    frame and log frame) instead of `self.root`. There is no module-level
    `root`, so this raised `NameError: name 'root' is not defined` during
    `HDRCalibrationUI.__init__` and the window never appeared. Now uses
    `self.root`. The existing `self.root.*` calls in the same method always
    worked, which is why the method looked fine at a glance.
  - The PyPI build of `wexpect` does `import pkg_resources` at module import
    time, but setuptools ≥ 81 no longer ships `pkg_resources`. With a fresh
    venv that means `import app` dies with
    `ModuleNotFoundError: No module named 'pkg_resources'`. The vendored
    copy in `wexpect-4.0.0/` is already patched to a static
    `__version__ = '4.0.0'`; the fix is to install **that** copy rather than
    the PyPI one:
    `pip install --force-reinstall --no-deps ./wexpect-4.0.0/`
  - `requirements.txt` now lists the runtime dependencies that were only
    implied before (`psutil`, `pywin32`) and documents why the vendored
    `wexpect` copy is required.
- Commented out the experimental warm-color correction matrix that was tuned
  on the maintainer's display (the "v7 red-move matrix"). The default behavior is
  now the plain identity matrix again (v3 behavior), which is the most generic
  choice. The commented code keeps the full derivation and a step-by-step
  guide if you want to try it on your own display.
- READMEs now document the "Historical gray data" feature and the CLUT output.
- Added this changelog; moved the internal handover/patch notes and per-display
  measurement logs into `archive\2026-09-02_24card-full\`.

## [2026-09-02]

> **About this version:** this repository is maintained by someone other than
> the original author. This version (2026-09-02, plus the 2026-09-22 changes
> listed above) was developed with **heavy use of DeepSeek Harness** (an AI-assisted
> coding tool). The maintainer has limited programming experience, so the code
> is largely AI-generated and only manually reviewed — it may contain
> non-optimal implementations or oversights. Please review before use/release.

### Added

- **24-color industry-standard test card** (X-Rite ColorChecker Classic, a.k.a.
  Macbeth ColorChecker). The "Color sample set" dropdown now has four tiers:
  `sRGB(12)` / `sRGB(12)+DisplayP3(7)` / `sRGB(24)` / `sRGB(24)+DisplayP3(7)`,
  with the default unchanged (12 colors). The 24-color card uses each patch's
  real chromaticity and relative luminance, scaled to the SDR paper-white
  anchor, so the fit no longer relies only on the brightest colors.
- **Historical gray data** (`gray_history.py`): when calibrating several color
  temperatures on the same display, the PQ grayscale curve no longer needs to
  be re-measured every time — the display's native response does not depend on
  the target white point, only the white-point adaptation does. Select a past
  complete measurement from the "Historical gray data" dropdown (labeled with
  its measurement end time) and the PQ curve step reuses it. A "Refresh"
  button reloads the list; it also refreshes automatically after calibration.

### Fixed / improved

- SDR white point (paper white) is now **read from the Windows system
  setting** instead of being hard-coded, and used as the brightness anchor for
  the white test patch and for the calibration test sets.
  - Source: `DISPLAYCONFIG_SDR_WHITE_LEVEL` (the "SDR content brightness"
    slider). Conversion: `paper white (nits) = SDRWhiteLevel(raw) / 1000 × 80`.
    In slider terms (0–100): `nits = slider × 8`, so slider 20 → 160 nits.
  - The white test patch code is recomputed at the start of every calibration:
    `code = round(pq_oetf(paper_white) × 1023)` (160 nits → 569/1023).
  - Falls back to 200 nits if the system value cannot be read.
  - Note: the value is read once at application start, so move the slider to
    your actual viewing brightness **before** launching the app.
- The fitted correction matrix is no longer written into the MHC2 matrix
  (Windows multiplies content XYZ by that matrix directly, which made warm
  colors undersaturated). The profile keeps an identity matrix and lets the
  measured primaries + 1D LUT do the gamut mapping.
- rXYZ/gXYZ/bXYZ/wtpt tags are jointly scaled to the white point (ICC
  convention, r+g+b = wtpt) and the TRC is written as sRGB EOTF, so profiles
  are ICC-conformant.
- Fixed a wexpect 4.0.0 + virtualenv deadlock that froze the GUI during
  calibration (spawn now uses the real base interpreter and has a timeout;
  leftover helper processes are cleaned up).

## [2026-09-01]

- First verified release on the maintainer's display (FFALCON R27U81): average
  ΔE 1.16 / max 5.52 on the 12-color card after the fixes above.
- Archive snapshots of the intermediate versions are kept in `archive\`.


