# Changelog

> This project is developed by an maintainer who is not a professional in color
> science, so the calibration logic may not be optimal. If you find issues or
> have better ideas, contributions and feedback are very welcome.
> [中文版](CHANGELOG_zh.md)

All notable changes are listed here, newest first. The format is plain on
purpose — see the README for how to use the program.

---

## [2026-09-02] — v2026.09.02 (first tagged release)

> **Release:** tag `v2026.09.02`, packaged as `rwhc-v2026.09.02.zip` on the
> [Releases](https://github.com/CodeBH0/rwhc-enhance/releases) page.

> **About this version:** this repository is maintained by someone other than
> the original author. This version (2026-09-02) was developed with **heavy use
> of DeepSeek Harness** (an AI-assisted coding tool). The maintainer has limited
> programming experience, so the code is largely AI-generated and only manually
> reviewed — it may contain non-optimal implementations or oversights. Please
> review before use/release.

### Fixed / improved

- Commented out the experimental warm-color correction matrix that was tuned
  on the maintainer's display (the "v7 red-move matrix"). The default behavior is
  now the plain identity matrix again (v3 behavior), which is the most generic
  choice. The commented code keeps the full derivation and a step-by-step
  guide if you want to try it on your own display.
- READMEs now document the "Historical gray data" feature.
- Added this changelog; moved the internal handover/patch notes and per-display
  measurement logs into `archive\2026-09-02_24card-full\`.

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


