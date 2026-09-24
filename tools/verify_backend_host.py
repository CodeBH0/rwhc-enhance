"""Hardware-free integration checks for the WinUI/Python process boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend_host import BackendHost, _handle_line  # noqa: E402
from convert_utils import BT2020_PQ_rgb_to_XYZ  # noqa: E402


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

    # Exercise calibration.start through the real algorithms with only the two
    # physical I/O edges replaced.  This catches accidental fallback to a fake
    # calibration operation while remaining deterministic on CI machines.
    class CalibrationDisplayPlatform(FakeDisplayPlatform):
        def install_profile(self, _path):
            pass

        def add_profile_association(self, _info, _filename):
            pass

        def remove_profile_association(self, _info, _filename):
            pass

        def uninstall_profile(self, _filename):
            pass

    operation_frames = []
    calibration_host = BackendHost(
        ROOT,
        display_platform=CalibrationDisplayPlatform(),
        emit_frame=operation_frames.append,
    )
    calibration_host.backend.log_path = os.path.join(
        tempfile.mkdtemp(prefix="rwhc-operation-test-"), "hc.log"
    )
    shutil.copyfile(os.path.join(ROOT, "hc.log"), calibration_host.backend.log_path)
    calibration_host.backend.displays.refresh()
    calibration_host.backend.instruments.discover_options = lambda: (
        [["1", "Meter A"]], [["c", "LCD"]]
    )
    existing_history = calibration_host.dispatch("history.list", {})
    shared = {"rgb": [0, 0, 0]}

    class FakeWriter:
        def write_rgb(self, rgb, delay=0):
            shared["rgb"] = [int(value) for value in rgb]

        def terminate(self):
            pass

    class FakeReader:
        status = "ready"

        def read_XYZ(self):
            code = np.asarray(shared["rgb"], dtype=float) / 1023.0
            return BT2020_PQ_rgb_to_XYZ(code) * 10000.0

        def terminate(self):
            pass

    def start_fake_measurement(_args):
        calibration_host.backend.state.proc_color_write = FakeWriter()
        calibration_host.backend.state.proc_color_reader = FakeReader()
        return (
            calibration_host.backend.state.proc_color_write,
            calibration_host.backend.state.proc_color_reader,
        )

    calibration_host.backend.processes.start_measurement = start_fake_measurement
    calibration_request = {
        "schemaVersion": 1,
        "monitorId": "4_Test HDR Display_TEST123",
        "instrumentDescription": "Meter A",
        "instrumentModeDescription": "LCD",
        "grayscaleSamples": 128,
        "colorSampleSet": "sRGB(12)",
        "whitePoint": "0.3127,0.3290",
        "brightMode": False,
        "eetfEnabled": False,
        "eetfArgs": {"sourceMax": 10000, "sourceMin": 0, "monitorMax": None, "monitorMin": None},
        "clutEnabled": False,
        "clutGrid": 33,
        "installProfile": True,
        "grayHistoryId": None,
        "colorHistoryId": None,
    }
    accepted = calibration_host.dispatch(
        "calibration.start", {"request": calibration_request}
    )
    calibration_id = accepted["operationId"]
    deadline = time.monotonic() + 20
    terminal = None
    answered_prompts = set()
    while time.monotonic() < deadline and terminal is None:
        for frame in list(operation_frames):
            if frame.get("operationId") != calibration_id:
                continue
            if frame["event"] == "prompt":
                prompt_id = frame["payload"]["promptId"]
                if prompt_id not in answered_prompts:
                    calibration_host.dispatch("prompt.respond", {
                        "operationId": calibration_id,
                        "promptId": prompt_id,
                        "response": {"choice": "continue"},
                    })
                    answered_prompts.add(prompt_id)
            elif frame["event"] in ("result", "error"):
                terminal = frame
                break
        time.sleep(0.01)
    check("calibration.start reaches a terminal result", terminal is not None and terminal["event"] == "result")
    check("real calibration pipeline produces a profile", bool(terminal and terminal["payload"]["value"]["profileReady"]))
    check("calibration operation emits measurement progress", any(
        frame.get("event") == "progress" and frame["payload"].get("phase") == "measure-gray"
        for frame in operation_frames
    ))
    check("calibration operation bridges algorithm logs", any(
        frame.get("event") == "log" and "PQ LUT" in frame["payload"].get("message", "")
        for frame in operation_frames
    ))
    with open(calibration_host.backend.log_path, encoding="utf-8") as log_file:
        operation_log = log_file.read()
    check("calibration operation persists reusable measurement logs", "PQ LUT" in operation_log)

    check(
        "repository history provides a complete replay pair",
        bool(existing_history["gray"] and existing_history["color"]),
    )
    replay_request = {
        **calibration_request,
        "grayHistoryId": existing_history["gray"][0]["id"],
        "colorHistoryId": existing_history["color"][0]["id"],
    }

    def reject_measurement_start(_args):
        raise AssertionError("history replay must not start physical measurement I/O")

    calibration_host.backend.processes.start_measurement = reject_measurement_start
    replay_start_index = len(operation_frames)
    replay_accepted = calibration_host.dispatch(
        "calibration.start", {"request": replay_request}
    )
    replay_id = replay_accepted["operationId"]
    replay_terminal = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and replay_terminal is None:
        for frame in operation_frames[replay_start_index:]:
            if frame.get("operationId") == replay_id and frame["event"] in ("result", "error"):
                replay_terminal = frame
                break
        time.sleep(0.01)
    replay_frames = [
        frame for frame in operation_frames[replay_start_index:]
        if frame.get("operationId") == replay_id
    ]
    check(
        "history replay completes through calibration.start",
        replay_terminal is not None
        and replay_terminal["event"] == "result"
        and replay_terminal["payload"]["value"]["historyOnly"] is True,
    )
    check(
        "history replay emits progress and logs without hardware prompts",
        any(frame["event"] == "progress" for frame in replay_frames)
        and any(frame["event"] == "log" for frame in replay_frames)
        and not any(frame["event"] == "prompt" for frame in replay_frames),
    )
    calibration_host.close()

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
