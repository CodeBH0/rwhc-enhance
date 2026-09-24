# RWHC Enhance WinUI frontend

This is the replacement frontend skeleton. It targets C#/.NET 10, WinUI 3,
Windows App SDK 2.5.1, and XAML. The project is currently unpackaged and
self-contained with respect to the Windows App SDK runtime.

Build and run from the repository root:

```powershell
dotnet build frontend/Rwhc.WinUI/Rwhc.WinUI.csproj -c Debug
dotnet run --project frontend/Rwhc.WinUI/Rwhc.WinUI.csproj -c Debug
```

The first screen starts `backend_host.py`, discovers real displays, reads the
selected display's SDR paper white, and obtains Argyll instrument/mode options.
The client also supports the versioned `CalibrationRequest` contract and
multiplexed asynchronous operation events/cancellation. See
`docs/FRONTEND_IPC.md` for protocol and development overrides.

For an automated process-boundary smoke test (including WinUI startup), run
the built executable with `--smoke-test`; exit code `0` means the C# client
successfully read the backend/device services, validated a request, and
completed an asynchronous prompt/event operation.
