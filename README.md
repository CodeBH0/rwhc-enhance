# A Truly Usable HDR Calibration Tool for Windows 11
[中文](https://github.com/CodeBH0/rwhc-enhance/blob/master/README_zh.md)|ENGLISH

![screenshot](https://github.com/CodeBH0/rwhc-enhance/blob/master/resources/ui.png)

## Frontend Migration Status

The frontend is being replaced with **C# + WinUI 3** (Windows App SDK + XAML).
The first migration stage is available in `frontend/Rwhc.WinUI` and includes a
buildable WinUI shell plus a working process boundary to the existing Python
backend. The current Python/Tk UI is retained only as the feature and
interaction reference until the WinUI frontend reaches parity; this project is
not adopting two permanent UI implementations.

Current WinUI scope:

- .NET 10, WinUI 3, XAML, and Windows App SDK 2.5.1;
- a versioned, allow-listed UTF-8 NDJSON protocol over a WinUI-owned Python
  child process, with multiplexed short RPC responses and asynchronous
  progress/log/prompt/result/error events plus cooperative cancellation;
- a strict, versioned JSON contract for `CalibrationRequest`;
- real backend calls for display discovery/selection, SDR paper white, and
  Argyll instrument/mode options;
- a first end-to-end `calibration.start` path that runs the existing gamut,
  PQ and chromaticity algorithms with asynchronous progress, logs, prompts,
  cancellation and deterministic measurement-process/preview cleanup;
- no calibration-algorithm rewrite and no display ICC changes from these
  read-only calls.

The WinUI page is the new application entry point. It exposes live calibration
and complete gray+color history replay, but does not yet provide profile
saving/export or accuracy measurement. `app.py` remains available only as the
legacy Tk UI while migration is in progress.

Install the .NET 10 SDK and Python dependencies, then use the one-command
launcher from the repository root:

```powershell
.\run-winui.cmd
```

The launcher calls `run-winui.ps1`, checks the Python backend import, builds the WinUI project when
needed, and starts the application. WinUI starts and owns `backend_host.py`
automatically; users must not launch the backend host separately. The equivalent
development commands are:

```powershell
dotnet build frontend\Rwhc.WinUI\Rwhc.WinUI.csproj -c Debug -p:Platform=x64
dotnet run --project frontend\Rwhc.WinUI\Rwhc.WinUI.csproj -c Debug
```

The WinUI page automatically reads the real device environment and can refresh
it on demand. An automated request/event/device smoke test is also available:

```powershell
frontend\Rwhc.WinUI\bin\x64\Debug\net10.0-windows10.0.26100.0\win-x64\Rwhc.WinUI.exe --smoke-test
```

Exit code `0` means that the WinUI runtime started and the C# client read a
ready Python backend profile. See [the IPC protocol](docs/FRONTEND_IPC.md),
[the backend boundary](docs/BACKEND.md), and
[the WinUI project notes](frontend/Rwhc.WinUI/README.md) for details.

## Usage

1. **Get the Project Code**  
   Download or clone this repository and go to the project root directory.  
   Packaged snapshots are attached to [Releases](https://github.com/CodeBH0/rwhc-enhance/releases)
   as `rwhc-v<version>.zip` — unzip and run, no git required.

2. **Install Python**  
   Install Python on Windows (choose one of the following):
   - Microsoft Store
   - Official website: <https://www.python.org/downloads/windows/>

3. **Install Dependencies**  
   In the project root directory, run:

   ```bash
   pip install -r requirements.txt
   ```

4. **Run the new WinUI application (recommended)**

   ```powershell
   .\run-winui.cmd
   ```

   The application manages the Python backend process itself. Select live
   measurement or complete historical gray/color data, review the parameters,
   then start calibration.

5. **Legacy Tk UI**

   `python app.py` starts the legacy Tk frontend. It is retained during the
   migration for features not yet available in WinUI; it is not the new
   application entry point.

### History replay without a colorimeter

If `hc.log` contains at least one complete grayscale run and one complete color
run, WinUI lists them in the two history selectors. When no colorimeter is
detected, the latest complete pair is selected automatically. Click **Use
historical data calibration** to run the real `calibration.start` workflow.
The backend resolves the opaque history IDs, runs the unchanged calibration
math, and reports progress, logs and the terminal result without launching
`dogegen` or `spotread`. Historical data must belong to the same display and
display state.

For a hardware-isolated process-boundary verification after building:

```powershell
frontend\Rwhc.WinUI\bin\x64\Debug\net10.0-windows10.0.26100.0\win-x64\Rwhc.WinUI.exe --history-smoke-test
```

This explicit test mode replaces only the Windows display/ICC edge; history
parsing, IPC, request validation, workflow and calibration algorithms remain
the production implementations.

## Color Measurement Devices

Currently the device driver is based on ArgyllCMS, which supports common devices such as the full X-Rite lineup and Datacolor devices.  
For the full list, please refer to the official documentation.

## Additional Usage Notes

1. **Using X-Rite Colorimeters with Logitech Mice**  
   Logitech mouse drivers may aggressively scan all devices and end up occupying the X-Rite colorimeter.  
   First, change the mouse driver in Device Manager back to the default Windows driver, then go to “Services” and stop the `Logitech LampArray Service`.

2. **Using Datacolor Spyder Colorimeters**  
   Click the “Install driver” button to open the Argyll website, download the driver, then run the installer in the `usb` folder.  
   After that, in Device Manager find the Spyder device (under “Universal Serial Bus controllers”):
   - Right-click the device and choose “Update driver”
   - Choose “Browse my computer for drivers”
   - Choose “Let me pick from a list of available drivers on my computer”
   - Select the Argyll driver from the list

3. **SDR Content Brightness (paper white)**  
   The calibration anchors the SDR white patch and the test-set brightness to the **actual SDR white level of the selected display** — the same "SDR content brightness" slider you see in Windows settings — rather than a hard-coded value.  
   Before starting the app, set that slider to the brightness you actually use for SDR content.
   - How it is read: the app queries `DISPLAYCONFIG_SDR_WHITE_LEVEL` (raw 0–10000) and converts with `nits = raw / 1000 × 80`.  
     In slider terms (0–100): `nits = slider × 8` (slider 20 → 160 nits).
   - The white test patch code is recomputed at each calibration: `code = round(pq_oetf(paper_white) × 1023)` (160 nits → 569/1023).
   - If the system value cannot be read it falls back to 200 nits.
   - Note: the value is read **once at application start** — move the slider first, then launch the app.

4. **Number of Grayscale Samples**  
   10‑bit HDR has 1024 grayscale levels (R=G=B, range 0–1023).  
   The program will sample a specified number of grayscale points evenly from these 1024 levels and interpolate the unsampled levels.  
   More samples generally (but not always) mean more accurate PQ curve calibration, at the cost of longer measurement time.  
   The measured curve becomes the `MHC2` 1D LUT by **inversion**: for every target output PQ the LUT holds the input PQ the panel needs (`lut[i] = f⁻¹(i/4095)`, noise-monotone-corrected, piecewise-linear inverse). One deliberate detail: **above the measured peak the LUT stays at the code value where the panel was brightest**, even when that is not the last sample — on the maintainer's OLED the curve peaks at code ≈ 814 and then rolls back to code 1023 because of ABL/power limiting, so clamping to PQ 1.0 there would make highlights measurably darker instead of brighter.  
   Self-check: `python tools/verify_lut_inverse.py` (36 checks, no display or colorimeter needed).

5. **Color Sample Set**  
   The program generates a test set within the selected gamut, and fits a matrix from the relationship between the expected XYZ values and the measured XYZ values. Four tiers are available:
   - **sRGB(12)**: the original 12-color sRGB card
   - **sRGB(12)+DisplayP3(7)**: 12 sRGB colors + 7 Display-P3 colors (for wide-gamut displays where colors look dull)
   - **sRGB(24)**: the industry-standard 24-color card (X-Rite ColorChecker Classic)
   - **sRGB(24)+DisplayP3(7)**: the 24-color card + 7 Display-P3 colors
   More samples generally give a more robust matrix fit, at the cost of longer measurement time.

6. **Historical Gray Data (skip re-measuring the PQ curve)**  
   When making several calibration profiles with different color temperatures on the same display, the PQ grayscale curve does not need to be re-measured every time — the display's native response does not depend on the target white point, only the white-point adaptation changes.  
   Select a past complete grayscale measurement from the "Historical gray data" dropdown (each entry is labeled with its measurement end time), and the PQ curve step will reuse that data instead of measuring.  
   Click "Refresh" to reload the list (e.g. after a new calibration in this session); the list is also refreshed automatically when a calibration finishes.  
   Note: the historical data must come from the same display, and interrupted/incomplete measurements are not shown.

7. **Historical Color Data (skip re-measuring primaries and the color card)**  
   The same reasoning applies to the colour half of a calibration. The primaries / white / paper-white / black points and the colour-card samples are measured **before** the calibration ICC is written, i.e. with the identity-matrix, flat-LUT preview profile — so what they record is the panel's *native* response at that code value, which does not depend on the target white point. `calibrate_chromaticity` does not white-point-adapt its measurements either.  
   Select a past complete colour measurement from the "Historical color data" dropdown (labeled with its measurement end time, the number of colour-card samples and the sample set), and the whole colour part of the calibration is skipped:
   - the gamut test points (red/green/blue/white/white-paper/black) and the activated-black binary search,
   - the colour-card run (12–25 meter readings, ~1.5 s each),
   - and the `rXYZ`/`gXYZ`/`bXYZ`/`wtpt`, `lumi`, TRC and `MHC2` peak/black writes all come from the reused run — through exactly the same code path (`_apply_gamut_data`) a live measurement uses, so the resulting profile is built the same way.

   ### Calibrating with no colorimeter at all

   If **both** dropdowns point at historical data (a past grayscale run *and* a past colour run), the calibration needs no measurement whatsoever. In that case the app
   - does **not** launch `dogegen` or `spotread`, and
   - does **not** show the "place the colorimeter" dialog,
   - skips the post-calibration gamut re-measurement too (it only re-derives the same tags from the same primaries),

   and you can press **Calibrate** and then **Save as ICC file** to produce a profile with no instrument connected at all. The log states this explicitly:
   ```
   Historical data is enough: no colorimeter needed for this calibration
   Historical color data reused: skipped gamut and color-card measurement
   Historical color data reused: skipped the post-calibration gamut re-measurement
   ```
   Everything that *is* measured still applies: the reused runs must come from the same display in the same state, and the ICC is only as good as those two runs. "Measure color accuracy" is a live measurement, so it still needs a colorimeter and says so instead of failing.

   **How it reaches the CLUT algorithm**: the reused primaries and white point are what `primaries_matrix()` turns into the linear `RGB → XYZ` matrix of the forward model (`DisplayModel`), and that matrix *is* the `A2B0` direction of the CLUT. In addition, the reused colour-card samples are attached to the model, so when the CLUT is generated the app checks the forward model against them and logs the result:
   ```
   CLUT colour check (A2B vs measured card): measured colour card: 25 samples,
   mean dE ITP 1.83, median 1.55, max 5.41, mean |ΔXYZ| 12.4 nit
   ```
   The card data is used as a **verification set**, not as fit input — it never silently changes the CLUT that the measured primaries / `MHC2` already determine (the same "matrix stays identity, primaries + 1D LUT do the work" policy as before). The app warns when the mean ΔE ITP is above a deliberately loose 15: this is a **model self-consistency** indicator rather than an absolute accuracy figure, because the panel's own run-to-run repeatability already shows up in it (on the maintainer's display, gray runs 8–40 min apart differ by up to 0.016 PQ, and comparing the colour card with a model built from a gray run measured 2.4 h earlier gives mean ΔE ITP ≈ 149). A clearly large value means the reused data and the current session disagree, e.g. the run came from another display.

   Notes and caveats:
   - Only complete runs are listed: all six gamut points **and** at least one colour-card sample. Cancelled calibrations are not offered.
   - The measurement is still a measurement of one display in one state: a colour run is only valid for the same display, the same SDR paper white / HDR mode, and with no calibration ICC loaded. If you changed any of those, re-measure.
   - `Refresh` reloads the list; it is also refreshed automatically when a calibration finishes.
   - Colour runs are parsed from `hc.log` with the same date inference as the grey history, so both dropdowns show the same dates.
8. **Bright Mode**  
   Applies an overall boost to the generated LUT (1D LUT * 1.1).  
   This is only suitable for watching movies in strong ambient light.

9. **Preview Calibration Result**  
   After calibration, the matrix and LUT are stored in memory.  
   When “Preview calibration result” is checked, a temporary ICC profile will be generated and applied to the selected display.  
   When unchecked, the temporary profile is automatically removed.  
   
   If calibration has not been run yet, an ideal HDR ICC profile is loaded instead  
   (BT.2020 gamut, 10000 nits, identity matrix and unmodified LUT).

10. **Calibrate**  
    Generates the matrix and LUT.

11. **Measure Color Accuracy**  
    Measures the color accuracy of the display.  
    If “Preview calibration result” is checked, the current matrix and LUT are temporarily applied to the display before measurement.  
    The accuracy of this feature has not been deeply validated.

12. **Save**  
    Saves the matrix and LUT as an ICC profile.

13. **CLUT output (A2B0/B2A0)**  
    When checked, the saved/previewed ICC additionally gets the standard ICC multi-dimensional lookup tables
    (`A2B0` forward, `B2A0` reverse, written as `lut16Type`); the dropdown next to it selects the grid size
    (17/25/33/37/45/65, default 33). The existing matrix tags and the private `MHC2` tag are **kept exactly as
    before** — the CLUT only adds the standard layer, so the normal Windows HDR calibration path is unaffected.

    - **Where the data comes from**: everything is derived from measurements this calibration already made
      (measured primaries/white point + the `MHC2` per-channel curves + peak/black luminance). **No extra measurement.**
      Those primaries may come from a live measurement or from
      "Historical color data" (see note 7) — either way the CLUT is built from the same inputs.
    - **Device encoding**: `RGB` with the ST 2084 (PQ) transfer function — the display's native input in
      Windows HDR mode. The CLUT axes are therefore PQ code values (0–1023 normalized).
    - **PCS anchoring**: peak-relative XYZ — the brightest white the display can produce maps to PCS `Y = 1.0`,
      with `X`/`Z` scaled by its own chromaticity. This is the same convention DisplayCAL's "XYZLUT+MTX" monitor
      profiles use (`wtpt` = native white, `lumi` = peak luminance).
    - **Known limitation (important)**: every PCS component can only span 0–1. Many bright saturated HDR colors
      have an XYZ component above the white point (e.g. for BT.2020 primaries at peak white, red `X≈0.95`,
      blue `Z≈1.19`); those components get clipped at 1.0. Consequently:
      * `A2B0` (forward): fully accurate — mean interpolation error ≈ **0.005%** on the synthetic-display self-test (33³);
      * `B2A0` (reverse): exact for colors that are **not** clipped (median closed-loop error ≈ 0.002%); inside the
        clipped highlight region different device codes map to the same PCS value, so the inverse is inherently
        many-to-one and can only return one reasonable answer for the request. For accuracy-critical use, treat
        `A2B0` (the forward description) as authoritative.
    - **Cost**: the `B2A0` inverse solve dominates. Measured on the maintainer's machine:
      17³ ≈ 5 s, 25³ ≈ 17 s, 33³ ≈ 37 s; 45³/65³ scale roughly with the node count
      (minutes). It is computed once per calibration state and cached, so preview and
      save do not recompute it. The work runs on a worker thread while saving, so the
      window stays responsive (controls are disabled and the log announces the wait);
      a save without CLUT has no slow step at all.
    - **Self-checks**: `python tools/verify_clut_profile.py` (full synthetic-display round trip, 25 checks),
      `python tools/verify_clut_app_integration.py` (app integration path, 13 checks) and
      `python tools/verify_color_history.py` (historical colour data parsing + reuse + meter-free
      calibration + CLUT check, 40 checks).


## Integrated External Tools

- **Pattern / Color Generator**  
  dogegen  
  <https://github.com/ledoge/dogegen>

- **Colorimeter Driver / Measurement Tool**  
  ArgyllCMS `spotread`  
  <https://www.argyllcms.com/>  
  Since DisplayCAL also uses the same driver, you can also refer to DisplayCAL’s documentation.

## Notes on Colorimeter Calibration

Colorimeters require display‑type‑specific spectral correction files.  
For details, see:

- ArgyllCMS documentation: <https://www.argyllcms.com/doc/oeminst.html>  
- DisplayCAL related tutorials and documentation

## About This Project

The author does not work professionally in color science, so the calibration logic may not be optimal.  
If you find issues or have better ideas, contributions and feedback are very welcome.

See [CHANGELOG](CHANGELOG.md) for the version history (中文版见 [CHANGELOG_zh](CHANGELOG_zh.md)).  
Released versions are tagged `v<year>.<month>.<day>` (e.g. `v2026.09.02`, `v2026.09.22`) and
published with a packaged archive on the [Releases](https://github.com/CodeBH0/rwhc-enhance/releases) page.

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL‑3.0).  
See the `LICENSE` file in the project root for details.
