# WinUI ↔ Python backend protocol

The WinUI application owns one long-lived Python child process. Communication
uses UTF-8 **without a BOM**, with one compact JSON object per line (NDJSON)
over redirected standard input/output. Python diagnostics use standard error;
standard output is exclusively protocol frames.

This is a migration seam, not a permanent second UI architecture. WinUI owns
presentation and interaction policy while the existing algorithms and native
Python dependencies remain behind `CalibrationBackend`.

## Lifecycle and concurrency

1. WinUI starts `backend_host.py --stdio --base-dir <root>`.
2. One C# stdout reader continuously demultiplexes `response` and `event`
   frames. It is incorrect to assume that the line after a request is its
   response: an operation event may be emitted first.
3. Short RPC requests may be in flight concurrently. Request `id` correlates
   responses; asynchronous events use `operationId` plus a per-operation,
   strictly increasing `sequence`.
4. Only one long-running operation is accepted at a time. This protects the
   single mutable `CalibrationState` and physical colorimeter.
5. Closing stdin cancels active operations, cleans measurement processes, and
   exits the host. WinUI kills the process tree only if graceful exit times out.

During development, `RWHC_PYTHON` overrides the interpreter and
`RWHC_BACKEND_ROOT` overrides the application root. Otherwise the client
prefers `.venv314\Scripts\python.exe` and searches parent directories for
`backend_host.py`.

## Protocol compatibility

- `protocol` is the major transport version. The host rejects any value other
  than `1` with `unsupported_protocol`.
- `protocolMinorVersion` is reported by `system.ping`/`backend.describe` and is
  currently `1`. Minor revisions may add optional methods, fields, or event
  types without changing existing semantics.
- Clients must inspect advertised `capabilities`, `requestSchemaVersions`, and
  `eventTypes` rather than infer support from the minor number.
- Existing v1 requests without `type` are accepted as `request` for compatibility
  with the first skeleton. All new clients emit `type` explicitly.
- Frames are limited to 1 MiB. Method names are allow-listed; this is not an
  arbitrary Python object RPC surface.

## Short RPC frames

Request:

```json
{"protocol":1,"type":"request","id":"42","method":"display.list","params":{}}
```

Successful response:

```json
{"protocol":1,"type":"response","id":"42","ok":true,"result":{"displays":[]}}
```

Error response:

```json
{"protocol":1,"type":"response","id":"42","ok":false,"error":{"code":"validation_error","message":"whitePoint: must contain exactly x,y","details":{"field":"whitePoint","reason":"must contain exactly x,y"}}}
```

`id` is a client-selected string or integer. Errors always have stable `code`
and `message`; validation errors additionally identify the field in `details`.

### Version 1 short methods

- `system.ping`: protocol, request-schema and event capabilities.
- `backend.describe`: backend/profile/history summary and capabilities.
- `display.list`: calls `DisplayService.refresh()` and returns frontend-safe
  monitor DTOs (`id`, name, PNP/GDI IDs, HDR mode, paper white, bounds).
- `display.getPaperWhite`: selected monitor's SDR paper white and whether it
  came from Windows or the 200-nit fallback.
- `instrument.listOptions`: calls `InstrumentService`/`spotread --help` and
  returns code/description pairs for instruments and measurement modes.
- `history.list`: opaque, backend-owned history IDs and display labels.
- `calibration.validateRequest`: parses, validates, resolves history IDs, and
  returns the canonical request without changing calibration state.
- `operation.cancel`: cooperative cancellation request.
- `prompt.respond`: reply to one outstanding prompt.
- `diagnostics.startEventProbe`: verification-only operation covering all
  asynchronous channels; it is not a calibration mock.

## CalibrationRequest schema v1

The Python `CalibrationRequest` dataclass remains the in-process model.
`calibration_contract.py` is the sole JSON codec and validation authority.

Canonical shape:

```json
{
  "schemaVersion": 1,
  "monitorId": "4_Display_Product",
  "instrumentDescription": "Meter A",
  "instrumentModeDescription": "LCD",
  "grayscaleSamples": 128,
  "colorSampleSet": "sRGB(12)",
  "whitePoint": "0.3127,0.3290",
  "brightMode": false,
  "eetfEnabled": false,
  "eetfArgs": {
    "sourceMax": 10000,
    "sourceMin": 0,
    "monitorMax": null,
    "monitorMin": null
  },
  "clutEnabled": false,
  "clutGrid": 33,
  "installProfile": true,
  "grayHistoryId": null,
  "colorHistoryId": null
}
```

Rules:

- `schemaVersion` is exact. Unsupported versions are rejected rather than
  guessed. Adding fields requires a new advertised schema version.
- Unknown fields and missing required fields are rejected, preventing spelling
  mistakes from silently changing a calibration.
- Optional values receive canonical defaults matching the existing Tk UI.
- `grayscaleSamples` is one of `128/256/512/1024`; `clutGrid` is one of
  `17/25/33/37/45/65`; the color sample set must be one shipped choice.
- `whitePoint` is parsed as `x,y`, checked for a physically valid chromaticity,
  and normalized to four decimal places.
- EETF source/monitor min/max relationships and exact boolean types are checked.
- Historical runs are never accepted as client-supplied Python-shaped blobs.
  The client sends opaque IDs from `history.list`; the host resolves them
  against its current parsed history before constructing `CalibrationRequest`.
- `calibration.validateRequest` is read-only. A future `calibration.start` must
  validate again immediately before installing the immutable snapshot into
  backend state, avoiding time-of-check/time-of-use drift.

## Asynchronous operations

A start RPC returns quickly:

```json
{"operationId":"d5...","status":"accepted"}
```

The worker then emits frames such as:

```json
{"protocol":1,"type":"event","operationId":"d5...","sequence":1,"event":"progress","timestamp":"2026-09-24T00:00:00+00:00","payload":{"phase":"measure-gray","current":4,"total":128,"message":""}}
```

Defined events:

- `progress`: phase, current, total, and an optional human-readable message.
- `log`: level and already-formatted message. When calibration is connected,
  Python logging must be bridged through an operation-scoped handler.
- `prompt`: `promptId`, semantic kind, title/message, choices, and default.
  The worker blocks without blocking stdin; WinUI replies via `prompt.respond`.
- `result`: the single successful terminal event. `status` is `completed` or
  `cancelled`; `value` contains operation-specific output.
- `error`: the single failed terminal event with stable code/message/details
  and a `recoverable` flag.

Cancellation is cooperative: `operation.cancel` sets the operation token,
wakes a pending prompt, and returns whether an active operation was found.
Workers must check cancellation between measurement units and during waits.
The future calibration worker must also clean `dogegen`/`spotread` and preview
ICC state in `finally`; the transport cannot make an arbitrary blocking native
meter read instantly cancellable.

## Remaining calibration integration

The transport boundary is ready, but `calibration.start` is intentionally not
implemented yet. Before moving the unchanged algorithms out of the Tk adapter,
the backend still needs explicit ports for pattern output/meter reads, preview
ICC policy, operation-scoped logging, prompt kinds used by meter setup, save
destination handling, and deterministic cleanup/rollback. These are adapter
interfaces around existing algorithms, not permission to rewrite their maths.
