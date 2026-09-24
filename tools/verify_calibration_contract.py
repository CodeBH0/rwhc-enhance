"""Hardware-free checks for the versioned CalibrationRequest wire contract."""

from __future__ import annotations

import copy
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calibration_contract import (  # noqa: E402
    REQUEST_SCHEMA_VERSION,
    RequestValidationError,
    request_from_wire,
    request_to_wire,
)


def valid_payload():
    return {
        "schemaVersion": 1,
        "monitorId": "display-1",
        "instrumentDescription": "Meter A",
        "instrumentModeDescription": "LCD",
        "grayscaleSamples": 128,
        "colorSampleSet": "sRGB(12)",
        "whitePoint": "0.3127,0.3290",
        "brightMode": False,
        "eetfEnabled": False,
        "eetfArgs": {
            "sourceMax": 10000,
            "sourceMin": 0,
            "monitorMax": None,
            "monitorMin": None,
        },
        "clutEnabled": False,
        "clutGrid": 33,
        "installProfile": True,
        "grayHistoryId": None,
        "colorHistoryId": None,
    }


def rejected(payload, field):
    try:
        request_from_wire(payload)
    except RequestValidationError as exc:
        return exc.field == field
    return False


def main():
    checks = []

    def check(label, value):
        checks.append(bool(value))
        print(f"  [{'PASS' if value else 'FAIL'}] {label}")

    payload = valid_payload()
    request = request_from_wire(payload)
    check("schema version is explicit", REQUEST_SCHEMA_VERSION == 1)
    check("camelCase wire values map to CalibrationRequest", request.monitor_id == "display-1")
    check("white point is canonicalized", request.white_point == "0.3127,0.3290")
    check("EETF keys map to backend naming", request.eetf_args["source_max"] == 10000.0)
    check("canonical serialization is stable", request_to_wire(request) == payload)

    minimal = {key: payload[key] for key in (
        "schemaVersion", "monitorId", "instrumentDescription",
        "instrumentModeDescription", "grayscaleSamples", "colorSampleSet",
        "whitePoint",
    )}
    defaults = request_to_wire(request_from_wire(minimal))
    check("optional fields receive documented defaults", defaults["clutGrid"] == 33 and defaults["installProfile"])

    bad = copy.deepcopy(payload)
    bad["schemaVersion"] = 2
    check("unknown schema version is rejected", rejected(bad, "schemaVersion"))
    bad = copy.deepcopy(payload)
    bad["typoField"] = True
    check("unknown fields are rejected", rejected(bad, "request"))
    bad = copy.deepcopy(payload)
    del bad["monitorId"]
    check("missing required fields are rejected", rejected(bad, "request"))
    bad = copy.deepcopy(payload)
    bad["grayscaleSamples"] = 64
    check("unsupported grayscale count is rejected", rejected(bad, "grayscaleSamples"))
    bad = copy.deepcopy(payload)
    bad["clutGrid"] = 31
    check("unsupported CLUT grid is rejected", rejected(bad, "clutGrid"))
    bad = copy.deepcopy(payload)
    bad["whitePoint"] = "0.8,0.4"
    check("invalid chromaticity is rejected", rejected(bad, "whitePoint"))
    bad = copy.deepcopy(payload)
    bad["eetfArgs"]["sourceMin"] = 11000
    check("invalid EETF range is rejected", rejected(bad, "eetfArgs.sourceMin"))
    bad = copy.deepcopy(payload)
    bad["brightMode"] = 1
    check("booleans do not accept integers", rejected(bad, "brightMode"))

    history_payload = copy.deepcopy(payload)
    history_payload["grayHistoryId"] = "gray-1"
    history_payload["colorHistoryId"] = "color-1"
    runs = {("gray", "gray-1"): {"kind": "gray"}, ("color", "color-1"): {"kind": "color"}}
    history_request = request_from_wire(history_payload, lambda kind, key: runs.get((kind, key)))
    check("history IDs resolve to backend-owned runs", history_request.gray_history_run["kind"] == "gray")
    round_trip = request_to_wire(history_request, lambda kind, run: f"{kind}-1")
    check("history IDs survive canonical round-trip", round_trip == history_payload)

    passed = sum(checks)
    print(f"\n==== 结果：{passed}/{len(checks)} 项通过 ====")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
