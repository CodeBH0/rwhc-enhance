"""Versioned wire contract for :class:`CalibrationRequest`.

The Python dataclass remains the in-process application model.  This module is
the only place that translates it to or from the JSON shape used by non-Python
frontends, keeping transport naming and compatibility rules out of the
calibration algorithms.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional

from calibration_backend import CalibrationRequest, DEFAULT_EETF_ARGS
from clut_icc import CLUT_GRID_CHOICES
from color_test_suit import COLOR_CARD_CHOICES


REQUEST_SCHEMA_VERSION = 1
GRAYSCALE_SAMPLE_CHOICES = (128, 256, 512, 1024)

_REQUIRED_FIELDS = {
    "schemaVersion",
    "monitorId",
    "instrumentDescription",
    "instrumentModeDescription",
    "grayscaleSamples",
    "colorSampleSet",
    "whitePoint",
}
_OPTIONAL_FIELDS = {
    "brightMode",
    "eetfEnabled",
    "eetfArgs",
    "clutEnabled",
    "clutGrid",
    "installProfile",
    "grayHistoryId",
    "colorHistoryId",
}
_ALL_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


class RequestValidationError(ValueError):
    """A stable validation error with a machine-readable field path."""

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


HistoryResolver = Callable[[str, str], Optional[dict[str, Any]]]
HistoryIdentifier = Callable[[str, dict[str, Any]], Optional[str]]


def _exact_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise RequestValidationError(field, "must be a boolean")
    return value


def _exact_int(value: Any, field: str) -> int:
    if type(value) is not int:
        raise RequestValidationError(field, "must be an integer")
    return value


def _number_or_none(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise RequestValidationError(field, "must be a number or null")
    return float(value)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError(field, "must be a non-empty string")
    return value.strip()


def _history_id(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _validate_white_point(value: Any) -> str:
    text = _nonempty_string(value, "whitePoint")
    try:
        parts = [float(item.strip()) for item in text.split(",")]
    except ValueError as exc:
        raise RequestValidationError(
            "whitePoint", "must contain numeric x,y chromaticity coordinates"
        ) from exc
    if len(parts) != 2:
        raise RequestValidationError("whitePoint", "must contain exactly x,y")
    x, y = parts
    if not (0.0 < x < 1.0 and 0.0 < y < 1.0 and x + y < 1.0):
        raise RequestValidationError(
            "whitePoint", "must satisfy 0 < x,y < 1 and x + y < 1"
        )
    return f"{x:.4f},{y:.4f}"


def _validate_eetf_args(value: Any) -> dict[str, Optional[float]]:
    field = "eetfArgs"
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RequestValidationError(field, "must be an object")
    allowed = {"sourceMax", "sourceMin", "monitorMax", "monitorMin"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RequestValidationError(field, f"unknown fields: {', '.join(unknown)}")

    source_max = _number_or_none(value.get("sourceMax", 10000), "eetfArgs.sourceMax")
    source_min = _number_or_none(value.get("sourceMin", 0), "eetfArgs.sourceMin")
    monitor_max = _number_or_none(value.get("monitorMax"), "eetfArgs.monitorMax")
    monitor_min = _number_or_none(value.get("monitorMin"), "eetfArgs.monitorMin")
    if source_max is None or source_max <= 0:
        raise RequestValidationError("eetfArgs.sourceMax", "must be greater than 0")
    if source_min is None or source_min < 0 or source_min > source_max:
        raise RequestValidationError(
            "eetfArgs.sourceMin", "must be between 0 and sourceMax"
        )
    if monitor_max is not None and monitor_max <= 0:
        raise RequestValidationError("eetfArgs.monitorMax", "must be greater than 0")
    if monitor_min is not None and monitor_min < 0:
        raise RequestValidationError("eetfArgs.monitorMin", "must not be negative")
    if (
        monitor_max is not None
        and monitor_min is not None
        and monitor_min > monitor_max
    ):
        raise RequestValidationError(
            "eetfArgs.monitorMin", "must not exceed monitorMax"
        )
    return {
        "source_max": source_max,
        "source_min": source_min,
        "monitor_max": monitor_max,
        "monitor_min": monitor_min,
    }


def request_from_wire(
    payload: Any,
    history_resolver: HistoryResolver | None = None,
) -> CalibrationRequest:
    """Validate schema v1 JSON data and create an immutable request snapshot.

    Schema versions are exact: an unsupported version is rejected rather than
    guessed.  Unknown fields are also rejected, so misspellings cannot silently
    change a calibration.  New optional fields therefore require a new schema
    version and an advertised capability.
    """

    if not isinstance(payload, dict):
        raise RequestValidationError("request", "must be an object")
    missing = sorted(_REQUIRED_FIELDS - set(payload))
    if missing:
        raise RequestValidationError("request", f"missing fields: {', '.join(missing)}")
    unknown = sorted(set(payload) - _ALL_FIELDS)
    if unknown:
        raise RequestValidationError("request", f"unknown fields: {', '.join(unknown)}")

    version = _exact_int(payload["schemaVersion"], "schemaVersion")
    if version != REQUEST_SCHEMA_VERSION:
        raise RequestValidationError(
            "schemaVersion",
            f"unsupported version {version}; supported: {REQUEST_SCHEMA_VERSION}",
        )

    samples = _exact_int(payload["grayscaleSamples"], "grayscaleSamples")
    if samples not in GRAYSCALE_SAMPLE_CHOICES:
        raise RequestValidationError(
            "grayscaleSamples",
            f"must be one of {list(GRAYSCALE_SAMPLE_CHOICES)}",
        )
    sample_set = _nonempty_string(payload["colorSampleSet"], "colorSampleSet")
    if sample_set not in COLOR_CARD_CHOICES:
        raise RequestValidationError(
            "colorSampleSet", f"must be one of {list(COLOR_CARD_CHOICES)}"
        )
    clut_grid = _exact_int(payload.get("clutGrid", 33), "clutGrid")
    if clut_grid not in CLUT_GRID_CHOICES:
        raise RequestValidationError(
            "clutGrid", f"must be one of {list(CLUT_GRID_CHOICES)}"
        )

    gray_id = _history_id(payload.get("grayHistoryId"), "grayHistoryId")
    color_id = _history_id(payload.get("colorHistoryId"), "colorHistoryId")
    gray_run = color_run = None
    if gray_id is not None or color_id is not None:
        if history_resolver is None:
            raise RequestValidationError(
                "history", "history IDs require a backend history resolver"
            )
        if gray_id is not None:
            gray_run = history_resolver("gray", gray_id)
            if gray_run is None:
                raise RequestValidationError("grayHistoryId", "unknown history ID")
        if color_id is not None:
            color_run = history_resolver("color", color_id)
            if color_run is None:
                raise RequestValidationError("colorHistoryId", "unknown history ID")

    return CalibrationRequest(
        monitor_id=_nonempty_string(payload["monitorId"], "monitorId"),
        instrument_description=_nonempty_string(
            payload["instrumentDescription"], "instrumentDescription"
        ),
        instrument_mode_description=_nonempty_string(
            payload["instrumentModeDescription"], "instrumentModeDescription"
        ),
        grayscale_samples=samples,
        color_sample_set=sample_set,
        white_point=_validate_white_point(payload["whitePoint"]),
        bright_mode=_exact_bool(payload.get("brightMode", False), "brightMode"),
        eetf_enabled=_exact_bool(payload.get("eetfEnabled", False), "eetfEnabled"),
        eetf_args=_validate_eetf_args(payload.get("eetfArgs")),
        clut_enabled=_exact_bool(payload.get("clutEnabled", False), "clutEnabled"),
        clut_grid=clut_grid,
        install_profile=_exact_bool(
            payload.get("installProfile", True), "installProfile"
        ),
        gray_history_run=gray_run,
        color_history_run=color_run,
    )


def request_to_wire(
    request: CalibrationRequest,
    history_identifier: HistoryIdentifier | None = None,
) -> dict[str, Any]:
    """Serialize an in-process request using the complete canonical v1 shape."""

    def identify(kind: str, run: Optional[dict[str, Any]]) -> Optional[str]:
        if run is None:
            return None
        if history_identifier is None:
            raise RequestValidationError(
                f"{kind}HistoryId", "history data requires an ID provider"
            )
        value = history_identifier(kind, run)
        if not value:
            raise RequestValidationError(f"{kind}HistoryId", "history run has no ID")
        return value

    eetf = copy.deepcopy(request.eetf_args or DEFAULT_EETF_ARGS)
    payload = {
        "schemaVersion": REQUEST_SCHEMA_VERSION,
        "monitorId": request.monitor_id,
        "instrumentDescription": request.instrument_description,
        "instrumentModeDescription": request.instrument_mode_description,
        "grayscaleSamples": request.grayscale_samples,
        "colorSampleSet": request.color_sample_set,
        "whitePoint": request.white_point,
        "brightMode": request.bright_mode,
        "eetfEnabled": request.eetf_enabled,
        "eetfArgs": {
            "sourceMax": eetf.get("source_max", 10000),
            "sourceMin": eetf.get("source_min", 0),
            "monitorMax": eetf.get("monitor_max"),
            "monitorMin": eetf.get("monitor_min"),
        },
        "clutEnabled": request.clut_enabled,
        "clutGrid": request.clut_grid,
        "installProfile": request.install_profile,
        "grayHistoryId": identify("gray", request.gray_history_run),
        "colorHistoryId": identify("color", request.color_history_run),
    }
    # Reuse the decoder as the canonical final validation for non-history data.
    if request.gray_history_run is None and request.color_history_run is None:
        request_from_wire(payload)
    return payload
