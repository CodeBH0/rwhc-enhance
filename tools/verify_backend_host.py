"""Hardware-free integration checks for the WinUI/Python process boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend_host import BackendHost, _handle_line  # noqa: E402


class ProtocolClient:
    def __init__(self, process):
        self.process = process
        self.responses = {}
        self.events = []
        self.received_events = []

    def send(self, request_id, method, params=None):
        message = {
            "protocol": 1,
            "type": "request",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def read_one(self):
        line = self.process.stdout.readline()
        assert line, "backend host exited without a frame"
        frame = json.loads(line)
        if frame["type"] == "response":
            self.responses[str(frame["id"])] = frame
        else:
            self.events.append(frame)
            self.received_events.append(frame)
        return frame

    def response(self, request_id):
        key = str(request_id)
        while key not in self.responses:
            self.read_one()
        return self.responses.pop(key)

    def event(self, operation_id, event_name=None, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, frame in enumerate(self.events):
                if frame["operationId"] == operation_id and (
                    event_name is None or frame["event"] == event_name
                ):
                    return self.events.pop(index)
            self.read_one()
        raise AssertionError(f"event timeout: {operation_id} {event_name}")


class FakeDisplayPlatform:
    def enumerate_displays(self):
        return [
            {
                "path_index": 4,
                "adapter_luid": {"low_part": 1, "high_part": 0},
                "source": {"id": 0, "gdi_name": r"\\.\DISPLAY1"},
                "target": {
                    "device_path": r"\\?\DISPLAY#TEST123#INSTANCE#{GUID}",
                    "friendly_name": "Test HDR Display",
                    "sdr_white_level_nits": 160.0,
                    "advanced_color": {"enabled": True, "wide_color_enforced": False},
                },
            }
        ]

    def monitor_rect(self, _gdi_name):
        return {"left": 0, "top": 0, "right": 3840, "bottom": 2160}

    def install_profile(self, _path):
        raise AssertionError("read-only IPC must not install profiles")

    def add_profile_association(self, _info, _filename):
        raise AssertionError("read-only IPC must not associate profiles")

    def remove_profile_association(self, _info, _filename):
        raise AssertionError("read-only IPC must not remove profiles")

    def uninstall_profile(self, _filename):
        raise AssertionError("read-only IPC must not uninstall profiles")


def main():
    checks = []

    def check(label, condition):
        checks.append(bool(condition))
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}")

    direct = BackendHost(ROOT, display_platform=FakeDisplayPlatform())
    direct.backend.instruments.discover_options = lambda: (
        [["1", "Meter A"]], [["c", "LCD"]]
    )
    displays = direct.dispatch("display.list", {})["displays"]
    check("display.list uses DisplayService", len(displays) == 1)
    check("display DTO reports HDR", displays[0]["isHdr"] is True)
    check("display DTO reports real paper white", displays[0]["sdrWhiteNits"] == 160.0)
    paper = direct.dispatch("display.getPaperWhite", {"monitorId": displays[0]["id"]})
    check("paper-white RPC uses selected display", paper["nits"] == 160.0 and paper["source"] == "system")
    options = direct.dispatch("instrument.listOptions", {})
    check("instrument RPC uses InstrumentService", options["instruments"][0]["code"] == "1")

    invalid = _handle_line(direct, json.dumps({
        "protocol": 1, "id": "bad", "method": "calibration.validateRequest",
        "params": {"request": {"schemaVersion": 99}},
    }))
    check("request validation errors are structured", invalid["error"]["code"] == "validation_error")
    check("validation error includes field details", invalid["error"]["details"]["field"] == "request")

    process = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "backend_host.py"), "--stdio", "--base-dir", ROOT],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    client = ProtocolClient(process)
    try:
        client.send("ping", "system.ping")
        ping = client.response("ping")
        check("short RPC remains available", ping["ok"] and ping["result"]["protocolVersion"] == 1)
        check("capabilities advertise request schema", ping["result"]["requestSchemaVersions"] == [1])
        check("capabilities advertise event types", set(ping["result"]["eventTypes"]) == {"progress", "log", "prompt", "result", "error"})

        client.send("probe", "diagnostics.startEventProbe", {"steps": 2, "delayMs": 1, "requirePrompt": True})
        probe = client.response("probe")["result"]
        operation_id = probe["operationId"]
        check("async operation is accepted immediately", probe["status"] == "accepted")
        prompt = client.event(operation_id, "prompt")
        client.send("prompt-reply", "prompt.respond", {
            "operationId": operation_id,
            "promptId": prompt["payload"]["promptId"],
            "response": {"choice": "continue"},
        })
        prompt_reply = client.response("prompt-reply")
        result = client.event(operation_id, "result")
        operation_events = [
            frame for frame in client.received_events
            if frame["operationId"] == operation_id
        ]
        check("prompt reply is a short RPC", prompt_reply["result"]["accepted"])
        check("operation emits progress", any(frame["event"] == "progress" for frame in operation_events))
        check("operation emits logs", any(frame["event"] == "log" for frame in operation_events))
        check("operation emits terminal result", result["payload"]["status"] == "completed")
        sequences = [frame["sequence"] for frame in operation_events]
        check(
            "event sequence is monotonic and unique",
            sequences == sorted(sequences) and len(sequences) == len(set(sequences)),
        )

        client.send("long", "diagnostics.startEventProbe", {"steps": 50, "delayMs": 20})
        long_operation = client.response("long")["result"]["operationId"]
        client.event(long_operation, "progress")
        client.send("cancel", "operation.cancel", {"operationId": long_operation})
        cancel = client.response("cancel")
        cancelled = client.event(long_operation, "result")
        check("cancellation request reaches active operation", cancel["result"]["cancellationRequested"])
        check("cancelled operation has terminal result", cancelled["payload"]["status"] == "cancelled")

        client.send("failure", "diagnostics.startEventProbe", {"steps": 1, "delayMs": 0, "fail": True})
        failed_operation = client.response("failure")["result"]["operationId"]
        error = client.event(failed_operation, "error")
        check("operation failures use terminal error event", error["payload"]["code"] == "probe_failure")

        client.send("missing", "backend.not-allowed")
        missing = client.response("missing")
        check("unknown methods remain blocked", not missing["ok"] and missing["error"]["code"] == "method_not_found")
    finally:
        process.stdin.close()
        process.wait(timeout=10)

    check("host exits cleanly on EOF", process.returncode == 0)
    direct.close()
    passed = sum(checks)
    print(f"\n==== 结果：{passed}/{len(checks)} 项通过 ====")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
