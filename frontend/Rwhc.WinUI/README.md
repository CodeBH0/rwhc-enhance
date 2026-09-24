# RWHC Enhance WinUI frontend

This is the replacement frontend skeleton. It targets C#/.NET 10, WinUI 3,
Windows App SDK 2.5.1, and XAML. The project is currently unpackaged and
self-contained with respect to the Windows App SDK runtime.

Recommended launch from the repository root:

```powershell
.\run-winui.cmd
```

The launcher checks the Python import and invokes the WinUI project. For direct
development builds:

```powershell
dotnet build frontend/Rwhc.WinUI/Rwhc.WinUI.csproj -c Debug
dotnet run --project frontend/Rwhc.WinUI/Rwhc.WinUI.csproj -c Debug
```

The first screen starts and owns `backend_host.py`, discovers real displays, reads the
selected display's SDR paper white, and obtains Argyll instrument/mode options.
The client also supports the versioned `CalibrationRequest` contract and
multiplexed asynchronous operation events/cancellation. See
`docs/FRONTEND_IPC.md` for protocol and development overrides.

Complete gray and color history runs from `hc.log` are shown in the page. If no
meter is detected, the latest complete pair is selected automatically and can
run through the real `calibration.start` workflow without physical measurement.
The Python/Tk `app.py` entry point is legacy and is not the WinUI launcher.

For an automated process-boundary smoke test (including WinUI startup), run
the built executable with `--smoke-test`; exit code `0` means the C# client
successfully read the backend/device services, validated a request, and
completed an asynchronous prompt/event operation.

Use `--history-smoke-test` for the full C# → IPC → history resolver → calibration
workflow → result path. This test uses the production history and algorithms
but replaces the Windows display/ICC edge with the explicit no-op test adapter.
