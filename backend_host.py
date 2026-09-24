"""Process host exposing :mod:`calibration_backend` to non-Python frontends.

The transport is UTF-8 NDJSON over stdin/stdout.  Short RPC responses and
asynchronous operation events share stdout and are distinguished by ``type``;
stderr is reserved for diagnostics.  Every callable method is explicitly
allow-listed.
"""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Any, Callable, Optional, TextIO
import uuid

from calibration_backend import CalibrationBackend, CalibrationRequest
from calibration_contract import (
    REQUEST_SCHEMA_VERSION,
    RequestValidationError,
    request_from_wire,
    request_to_wire,
)
from color_history import run_label as color_run_label


PROTOCOL_VERSION = 1
PROTOCOL_MINOR_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
EVENT_TYPES = ("progress", "log", "prompt", "result", "error")


class ProtocolError(Exception):
    """An error that is safe to return to an IPC client."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.details = details


class OperationCancelled(Exception):
    """Internal cooperative-cancellation signal."""


class FrameWriter:
    """Serialize complete frames atomically across request and worker threads."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self._lock = threading.Lock()

    def write(self, frame: dict[str, Any]) -> None:
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.stream.write(data + "\n")
            self.stream.flush()


class OperationContext:
    """Event/cancellation/prompt port presented to a long-running worker."""

    def __init__(self, operation_id: str, emit_frame: Callable[[dict], None]):
        self.operation_id = operation_id
        self._emit_frame = emit_frame
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self._prompts: dict[str, queue.Queue] = {}
        self._prompt_lock = threading.Lock()

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if event not in EVENT_TYPES:
            raise ValueError(f"Unsupported event type: {event}")
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        self._emit_frame(
            {
                "protocol": PROTOCOL_VERSION,
                "type": "event",
                "operationId": self.operation_id,
                "sequence": sequence,
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
        )

    def log(self, level: str, message: str) -> None:
        self.emit("log", {"level": level, "message": message})

    def progress(
        self,
        phase: str,
        current: int,
        total: int,
        message: str = "",
    ) -> None:
        self.emit(
            "progress",
            {
                "phase": phase,
                "current": current,
                "total": total,
                "message": message,
            },
        )

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise OperationCancelled()

    def wait(self, seconds: float) -> None:
        if self.cancel_event.wait(max(0.0, seconds)):
            raise OperationCancelled()

    def prompt(
        self,
        kind: str,
        title: str,
        message: str,
        choices: list[str],
        default_choice: Optional[str] = None,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        self.check_cancelled()
        prompt_id = str(uuid.uuid4())
        responses: queue.Queue = queue.Queue(maxsize=1)
        with self._prompt_lock:
            self._prompts[prompt_id] = responses
        self.emit(
            "prompt",
            {
                "promptId": prompt_id,
                "kind": kind,
                "title": title,
                "message": message,
                "choices": choices,
                "defaultChoice": default_choice,
            },
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                self.check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError("prompt_timeout", "Prompt response timed out")
                try:
                    response = responses.get(timeout=min(0.1, remaining))
                    return response
                except queue.Empty:
                    continue
        finally:
            with self._prompt_lock:
                self._prompts.pop(prompt_id, None)

    def respond_prompt(self, prompt_id: str, response: dict[str, Any]) -> bool:
        with self._prompt_lock:
            responses = self._prompts.get(prompt_id)
        if responses is None:
            return False
        try:
            responses.put_nowait(response)
            return True
        except queue.Full:
            return False

    def cancel(self) -> None:
        self.cancel_event.set()


class OperationManager:
    """Own active operations and their cooperative control channels."""

    def __init__(self, emit_frame: Callable[[dict], None]):
        self._emit_frame = emit_frame
        self._operations: dict[str, OperationContext] = {}
        self._lock = threading.Lock()

    def start(
        self,
        worker: Callable[[OperationContext], dict[str, Any]],
    ) -> str:
        operation_id = str(uuid.uuid4())
        context = OperationContext(operation_id, self._emit_frame)
        with self._lock:
            if self._operations:
                raise ProtocolError(
                    "operation_conflict", "Another long-running operation is active"
                )
            self._operations[operation_id] = context

        def run() -> None:
            try:
                value = worker(context)
                context.check_cancelled()
                context.emit("result", {"status": "completed", "value": value})
            except OperationCancelled:
                context.emit("result", {"status": "cancelled", "value": None})
            except ProtocolError as exc:
                context.emit(
                    "error",
                    {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                        "recoverable": False,
                    },
                )
            except Exception as exc:
                logging.exception("Unhandled asynchronous operation failure")
                context.emit(
                    "error",
                    {
                        "code": "internal_error",
                        "message": str(exc),
                        "details": None,
                        "recoverable": False,
                    },
                )
            finally:
                with self._lock:
                    self._operations.pop(operation_id, None)

        threading.Thread(target=run, daemon=True, name=f"operation-{operation_id}").start()
        return operation_id

    def cancel(self, operation_id: str) -> bool:
        with self._lock:
            context = self._operations.get(operation_id)
        if context is None:
            return False
        context.cancel()
        return True

    def respond_prompt(
        self, operation_id: str, prompt_id: str, response: dict[str, Any]
    ) -> bool:
        with self._lock:
            context = self._operations.get(operation_id)
        return bool(context and context.respond_prompt(prompt_id, response))

    def cancel_all(self) -> None:
        with self._lock:
            operations = list(self._operations.values())
        for context in operations:
            context.cancel()


class BackendHost:
    """Allow-listed facade around the GUI-independent backend."""

    def __init__(
        self,
        base_dir: str,
        display_platform=None,
        emit_frame: Optional[Callable[[dict], None]] = None,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.backend = CalibrationBackend(
            base_dir=self.base_dir,
            display_platform=display_platform,
        )
        self._emit_frame = emit_frame or (lambda _frame: None)
        self.operations = OperationManager(self._emit_frame)

    def set_emitter(self, emit_frame: Callable[[dict], None]) -> None:
        self._emit_frame = emit_frame
        self.operations = OperationManager(emit_frame)

    def close(self) -> None:
        self.operations.cancel_all()
        try:
            self.backend.processes.cleanup_measurement_processes()
        except Exception:
            logging.exception("Failed to clean backend processes during shutdown")

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        methods = {
            "system.ping": self._ping,
            "backend.describe": self._describe,
            "display.list": self._display_list,
            "display.getPaperWhite": self._display_paper_white,
            "instrument.listOptions": self._instrument_options,
            "history.list": self._history_list,
            "calibration.validateRequest": self._validate_request,
            "diagnostics.startEventProbe": self._start_event_probe,
            "operation.cancel": self._cancel_operation,
            "prompt.respond": self._respond_prompt,
        }
        handler = methods.get(method)
        if handler is None:
            raise ProtocolError("method_not_found", f"Unknown method: {method}")
        return handler(params)

    def _ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "protocolMinorVersion": PROTOCOL_MINOR_VERSION,
            "requestSchemaVersions": [REQUEST_SCHEMA_VERSION],
            "eventTypes": list(EVENT_TYPES),
            "service": "rwhc-python-backend",
            "pythonVersion": sys.version.split()[0],
            "processId": os.getpid(),
        }

    def _describe(self, params: dict[str, Any]) -> dict[str, Any]:
        gray_runs, color_runs = self.backend.reload_history()
        state = self.backend.state
        return {
            **self._ping(params),
            "backendType": type(self.backend).__name__,
            "profileReady": state.icc_handle is not None,
            "mhc2EntryCount": int(state.MHC2.get("entry_count", 0)),
            "grayHistoryCount": len(gray_runs),
            "colorHistoryCount": len(color_runs),
            "supportedClutGrids": [17, 25, 33, 37, 45, 65],
            "requestFields": [item.name for item in fields(CalibrationRequest)],
            "capabilities": [
                "profile-state",
                "history-read",
                "calibration-request-v1",
                "async-events-v1",
                "operation-cancellation",
                "interactive-prompts",
                "display-discovery",
                "instrument-options",
            ],
        }

    def _display_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"displays": self.backend.displays.list_displays()}

    def _display_paper_white(self, params: dict[str, Any]) -> dict[str, Any]:
        monitor_id = params.get("monitorId")
        if not isinstance(monitor_id, str) or not monitor_id:
            raise ProtocolError("invalid_params", "monitorId must be a non-empty string")
        try:
            return self.backend.displays.paper_white_info(monitor_id)
        except KeyError as exc:
            raise ProtocolError("not_found", str(exc)) from exc

    def _instrument_options(self, _params: dict[str, Any]) -> dict[str, Any]:
        return self.backend.instruments.list_options()

    @staticmethod
    def _run_id(kind: str, run: dict[str, Any]) -> str:
        end = run.get("end")
        end_text = end.isoformat() if hasattr(end, "isoformat") else str(end)
        count = run.get("num") if kind == "gray" else len(run.get("points", []))
        digest = hashlib.sha256(f"{kind}|{end_text}|{count}".encode()).hexdigest()[:16]
        return f"{kind}-{digest}"

    def _history_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        gray_runs, color_runs = self.backend.reload_history()
        return {
            "gray": [
                {
                    "id": self._run_id("gray", run),
                    "label": f"{run['end']:%m-%d %H:%M:%S} · {run['num']} pts",
                    "samples": int(run["num"]),
                }
                for run in gray_runs
            ],
            "color": [
                {
                    "id": self._run_id("color", run),
                    "label": color_run_label(run),
                    "samples": len(run.get("points", [])),
                    "sampleSet": run.get("sample_set"),
                }
                for run in color_runs
            ],
        }

    def _resolve_history(self, kind: str, run_id: str) -> Optional[dict[str, Any]]:
        runs = (
            self.backend.reload_gray_history()
            if kind == "gray"
            else self.backend.reload_color_history()
        )
        return next(
            (run for run in runs if self._run_id(kind, run) == run_id),
            None,
        )

    def _identify_history(
        self, kind: str, selected: dict[str, Any]
    ) -> Optional[str]:
        return self._run_id(kind, selected)

    def _validate_request(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            request = request_from_wire(
                params.get("request"),
                history_resolver=self._resolve_history,
            )
            normalized = request_to_wire(request, self._identify_history)
        except RequestValidationError as exc:
            raise ProtocolError(
                "validation_error",
                str(exc),
                {"field": exc.field, "reason": exc.message},
            ) from exc
        return {
            "valid": True,
            "schemaVersion": REQUEST_SCHEMA_VERSION,
            "normalizedRequest": normalized,
            "historyOnly": self.backend.history_only_calibration(request),
        }

    def _start_event_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        steps = params.get("steps", 3)
        delay_ms = params.get("delayMs", 10)
        require_prompt = params.get("requirePrompt", False)
        fail = params.get("fail", False)
        if type(steps) is not int or not 1 <= steps <= 100:
            raise ProtocolError("invalid_params", "steps must be an integer from 1 to 100")
        if type(delay_ms) is not int or not 0 <= delay_ms <= 5000:
            raise ProtocolError("invalid_params", "delayMs must be an integer from 0 to 5000")
        if type(require_prompt) is not bool or type(fail) is not bool:
            raise ProtocolError("invalid_params", "requirePrompt/fail must be booleans")

        def worker(context: OperationContext) -> dict[str, Any]:
            context.log("info", "Event probe started")
            for index in range(steps):
                context.check_cancelled()
                context.progress("probe", index + 1, steps, f"Step {index + 1}")
                context.wait(delay_ms / 1000.0)
            prompt_response = None
            if require_prompt:
                prompt_response = context.prompt(
                    "confirmation",
                    "Continue event probe?",
                    "This prompt verifies the asynchronous reply channel.",
                    ["continue", "cancel"],
                    "continue",
                    timeout_seconds=30.0,
                )
                if prompt_response.get("choice") != "continue":
                    raise OperationCancelled()
            if fail:
                raise ProtocolError("probe_failure", "Requested probe failure")
            context.log("info", "Event probe finished")
            return {"steps": steps, "promptResponse": prompt_response}

        operation_id = self.operations.start(worker)
        return {"operationId": operation_id, "status": "accepted"}

    def _cancel_operation(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = params.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise ProtocolError("invalid_params", "operationId must be a string")
        return {
            "operationId": operation_id,
            "cancellationRequested": self.operations.cancel(operation_id),
        }

    def _respond_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = params.get("operationId")
        prompt_id = params.get("promptId")
        response = params.get("response")
        if not isinstance(operation_id, str) or not operation_id:
            raise ProtocolError("invalid_params", "operationId must be a string")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ProtocolError("invalid_params", "promptId must be a string")
        if not isinstance(response, dict):
            raise ProtocolError("invalid_params", "response must be an object")
        accepted = self.operations.respond_prompt(operation_id, prompt_id, response)
        if not accepted:
            raise ProtocolError("not_found", "Operation or prompt is no longer active")
        return {"operationId": operation_id, "promptId": prompt_id, "accepted": True}


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
        "ok": True,
        "result": result,
    }


def _error_response(
    request_id: Any,
    code: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {
        "protocol": PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
        "ok": False,
        "error": error,
    }


def _handle_line(host: BackendHost, raw_line: str) -> dict[str, Any]:
    request_id = None
    try:
        if len(raw_line.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ProtocolError("request_too_large", "Request exceeds 1 MiB")
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ProtocolError("invalid_json", str(exc)) from exc
        if not isinstance(request, dict):
            raise ProtocolError("invalid_request", "Request must be a JSON object")
        request_id = request.get("id")
        if request.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(
                "unsupported_protocol",
                f"Expected protocol major version {PROTOCOL_VERSION}",
            )
        frame_type = request.get("type", "request")
        if frame_type != "request":
            raise ProtocolError("invalid_request", "Client frame type must be request")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            raise ProtocolError("invalid_request", "id must be a string or integer")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise ProtocolError("invalid_request", "method must be a non-empty string")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError("invalid_request", "params must be a JSON object")
        return _response(request_id, host.dispatch(method, params))
    except ProtocolError as exc:
        return _error_response(request_id, exc.code, str(exc), exc.details)
    except Exception as exc:
        logging.exception("Unhandled backend request failure")
        return _error_response(request_id, "internal_error", str(exc))


def serve_stdio(host: BackendHost, input_stream: TextIO, output_stream: TextIO) -> None:
    """Serve short RPCs while workers independently publish event frames."""

    writer = FrameWriter(output_stream)
    host.set_emitter(writer.write)
    try:
        for raw_line in input_stream:
            if not raw_line.strip():
                continue
            writer.write(_handle_line(host, raw_line))
    finally:
        host.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rwhc WinUI backend host")
    parser.add_argument("--stdio", action="store_true", help="serve NDJSON on stdio")
    parser.add_argument(
        "--base-dir",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="repository/application data root",
    )
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("--stdio is required")

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    host = BackendHost(args.base_dir)
    serve_stdio(host, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
