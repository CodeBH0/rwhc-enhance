# Backend boundary

`calibration_backend.py` is the application boundary for non-UI code. It does
not import `tkinter`, create windows, or read Tk variables. The current Tk app
is an adapter over this API. The replacement WinUI frontend integrates through
the process host in `backend_host.py` instead of porting `HDRCalibrationUI`.

## Input and state

- `CalibrationRequest` is a plain-data snapshot of one calibration request.
  Frontends must build it before starting work; background calibration code
  must not read live UI controls.
- `CalibrationState` owns the current ICC profile, MHC2 data, measurements,
  selected/reused history, external process handles, display discovery data and
  CLUT cache.
- `CalibrationBackend.begin_calibration(request)` installs the immutable input
  snapshot into the current state.

## Services

- `DisplayService`: display discovery, SDR paper-white lookup and ICC
  install/remove operations. Its device edge is the injectable
  `WindowsDisplayPlatform`; tests and future hosts can provide the same port
  without invoking Windows ICC APIs.
- `InstrumentService`: `spotread --help` discovery/parsing and argument
  construction from plain strings.
- `ExternalProcessService`: helper-tool launch plus dogegen/spotread creation
  and cleanup. `color_rw` is imported lazily so importing the backend does not
  require the instrument runtime.
- `CalibrationBackend`: history loading, base-profile reset, calibration
  pipeline ordering, historical-colour restoration, gamut/profile writes,
  brightness compensation, DisplayModel/CLUT generation and profile saving.

## Frontend responsibilities

The frontend owns only interaction policy:

- choose values and create `CalibrationRequest`;
- display errors, confirmation prompts and progress;
- schedule backend work and marshal completion back to its UI thread;
- choose save paths and open documentation/tools.

The Tk adapter currently retains the long-running measurement algorithms
(`calibrate_pq`, `calibrate_chromaticity`, and live accuracy measurement) for
compatibility. Their calibration options now come from `CalibrationRequest`,
not Tk controls, and their persistent state/processes are backend-owned. They
are the next migration seam if the Python backend is later exposed through
IPC to WinUI; this refactor intentionally does not rewrite their mathematics.

## WinUI process adapter

`backend_host.py` owns a `CalibrationBackend` instance and exposes an explicit,
versioned method allow-list over NDJSON/stdin/stdout. Short RPC responses and
asynchronous operation events are multiplexed on stdout. It is the adapter from
the C# process boundary to this Python API; it does not duplicate backend state
or calibration logic. `calibration_contract.py` owns the strict JSON codec for
`CalibrationRequest`, including schema-version, type/range, history-ID and
canonical-default rules. See `docs/FRONTEND_IPC.md` for framing, lifecycle,
events, cancellation, prompts, and the currently implemented methods.

## Verification

Run without a display or colorimeter:

```powershell
.\.venv314\Scripts\python.exe tools/verify_calibration_backend.py
.\.venv314\Scripts\python.exe tools/verify_clut_app_integration.py
.\.venv314\Scripts\python.exe tools/verify_lut_inverse.py
.\.venv314\Scripts\python.exe tools/verify_backend_host.py
.\.venv314\Scripts\python.exe tools/verify_calibration_contract.py
```

`verify_calibration_backend.py` also asserts that importing the backend does
not import `tkinter`.
