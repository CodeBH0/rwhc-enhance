"""GUI-independent application backend for HDR calibration.

The module owns mutable calibration state, Windows display/ICC integration,
Argyll discovery, external process lifetime, profile construction and the
high-level calibration pipeline.  It deliberately has no tkinter dependency;
frontends provide plain :class:`CalibrationRequest` values and callbacks for
the measurement algorithms that still live in the legacy controller.
"""

from dataclasses import dataclass, field
import copy
import logging
import os
import re
import subprocess
import sys
from typing import Any, Callable, Optional

import numpy as np

from clut_icc import (
    CLUT_FORMAT_MFT2,
    build_model_from_calibration,
    make_clut_tags,
    write_clut_tags,
)
from color_history import (
    gamut_xyz_from_run,
    parse_color_runs,
    peak_min_luminance,
)
from convert_utils import (
    BT2020_PQ_rgb_to_XYZ,
    XYZ_to_BT2020_PQ_rgb,
    apply_lut,
    build_primaries_xyz_tags,
    srgb_encode,
)
from gray_history import parse_gray_runs
from i18n.i18n_loader import _
from icc_rw import ICCProfile
from lut import generate_bright_pq_lut, generate_inversed_lut, generate_pq_lut
from win_display import (
    cp_add_display_association,
    cp_remove_display_association,
    get_all_display_config,
    get_monitor_rect_by_gdi_name,
    install_icc,
    luid_from_dict,
    uninstall_icc,
)


DEFAULT_EETF_ARGS = {
    "source_max": 10000,
    "source_min": 0,
    "monitor_max": None,
    "monitor_min": None,
}


@dataclass(frozen=True)
class CalibrationRequest:
    """Plain-data snapshot of every frontend option used by one calibration."""

    monitor_id: str
    instrument_description: str
    instrument_mode_description: str
    grayscale_samples: int
    color_sample_set: str
    white_point: str
    bright_mode: bool = False
    eetf_enabled: bool = False
    eetf_args: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_EETF_ARGS))
    clut_enabled: bool = False
    clut_grid: int = 33
    install_profile: bool = True
    gray_history_run: Optional[dict[str, Any]] = None
    color_history_run: Optional[dict[str, Any]] = None


@dataclass
class CalibrationState:
    """Mutable backend state shared by calibration operations."""

    icc_handle: Optional[ICCProfile] = None
    icc_data: dict[str, Any] = field(default_factory=dict)
    MHC2: dict[str, Any] = field(default_factory=dict)
    target_xyz: Any = field(default_factory=list)
    convert_command: list[Any] = field(default_factory=list)
    measured_xyz: Any = field(default_factory=dict)
    gamut_test_rgb: dict[str, list[int]] = field(
        default_factory=lambda: {
            "red": [592, 0, 0],
            "green": [0, 592, 0],
            "blue": [0, 0, 592],
            "white": [1023, 1023, 1023],
            "white_paper": [592, 592, 592],
            "black": [0, 0, 0],
        }
    )
    measure_gamut_xyz: dict[str, Any] = field(default_factory=dict)
    preview_icc_name: Optional[str] = None
    measured_pq: dict[str, list[float]] = field(
        default_factory=lambda: {"red": [], "green": [], "blue": []}
    )
    gray_history_runs: list[dict[str, Any]] = field(default_factory=list)
    selected_gray_run: Optional[dict[str, Any]] = None
    color_history_runs: list[dict[str, Any]] = field(default_factory=list)
    selected_color_run: Optional[dict[str, Any]] = None
    reused_color_run: Optional[dict[str, Any]] = None
    reused_color_card: Optional[list[Any]] = None
    proc_color_write: Any = None
    proc_color_reader: Any = None
    icc_change_delay: float = 0
    clut_cache: Any = None
    eetf_args: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_EETF_ARGS))
    displays_config: list[dict[str, Any]] = field(default_factory=list)
    human_display_config_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_request: Optional[CalibrationRequest] = None


class StateField:
    """Compatibility descriptor forwarding legacy controller fields to state."""

    def __init__(self, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.backend.state, self.name)

    def __set__(self, instance, value):
        setattr(instance.backend.state, self.name, value)


class WindowsDisplayPlatform:
    """Thin adapter around the Windows display and colour-profile APIs."""

    @staticmethod
    def enumerate_displays():
        return get_all_display_config()

    @staticmethod
    def monitor_rect(gdi_name):
        return get_monitor_rect_by_gdi_name(gdi_name)

    @staticmethod
    def install_profile(path):
        install_icc(path)

    @staticmethod
    def add_profile_association(info, filename):
        cp_add_display_association(
            luid_from_dict(info["adapter_luid"]),
            info["source"]["id"],
            filename,
            set_as_default=True,
            associate_as_advanced_color=info["color_work_status"] == "hdr",
        )

    @staticmethod
    def remove_profile_association(info, filename):
        cp_remove_display_association(
            luid_from_dict(info["adapter_luid"]),
            info["source"]["id"],
            filename,
            associate_as_advanced_color=info["color_work_status"] == "hdr",
        )

    @staticmethod
    def uninstall_profile(filename):
        uninstall_icc(filename, force=True)


class DisplayService:
    """Display discovery and ICC operations behind an injectable platform port."""

    def __init__(self, state, platform=None):
        self.state = state
        self.platform = platform or WindowsDisplayPlatform()

    def refresh(self):
        displays = self.platform.enumerate_displays()
        mapped = {}
        for item in displays:
            product_id = item["target"]["device_path"].split("#")[1]
            friendly_name = item["target"].get("friendly_name") or "none"
            display_id = f"{item['path_index']}_{friendly_name}_{product_id}"
            info = copy.deepcopy(item)
            info["monitor_rect"] = self.platform.monitor_rect(
                item["source"]["gdi_name"]
            )
            info["color_work_status"] = "sdr"
            parts = item["target"]["device_path"].split("\\")[-1].split("#")[:-1]
            info["pnp_device_id"] = "\\".join(parts)
            advanced = info["target"]["advanced_color"]
            if advanced["enabled"]:
                info["color_work_status"] = (
                    "sdr_acm" if advanced["wide_color_enforced"] else "hdr"
                )
            mapped[display_id] = info
        self.state.displays_config = displays
        self.state.human_display_config_map = mapped
        return mapped

    def get(self, monitor_id):
        return self.state.human_display_config_map.get(monitor_id)

    def list_displays(self):
        """Return frontend-safe display summaries from the real Windows port."""
        mapped = self.refresh()
        summaries = []
        for display_id, info in mapped.items():
            target = info.get("target", {})
            source = info.get("source", {})
            rect = info.get("monitor_rect") or {}
            if isinstance(rect, (tuple, list)) and len(rect) == 4:
                rect = dict(zip(("left", "top", "right", "bottom"), rect))
            if not isinstance(rect, dict):
                rect = {}
            summaries.append(
                {
                    "id": display_id,
                    "name": target.get("friendly_name") or "Unknown display",
                    "pnpDeviceId": info.get("pnp_device_id") or "",
                    "gdiName": source.get("gdi_name") or "",
                    "colorMode": info.get("color_work_status") or "sdr",
                    "isHdr": info.get("color_work_status") == "hdr",
                    "sdrWhiteNits": self.paper_white_nits(display_id),
                    "bounds": {
                        "left": int(rect.get("left", 0)),
                        "top": int(rect.get("top", 0)),
                        "right": int(rect.get("right", 0)),
                        "bottom": int(rect.get("bottom", 0)),
                    },
                }
            )
        return summaries

    def paper_white_info(self, monitor_id, fallback=200.0):
        info = self.get(monitor_id)
        if info is None:
            raise KeyError(f"Unknown monitor: {monitor_id}")
        value = info.get("target", {}).get("sdr_white_level_nits")
        from_system = value is not None and float(value) > 0
        return {
            "monitorId": monitor_id,
            "nits": float(value) if from_system else float(fallback),
            "source": "system" if from_system else "fallback",
        }

    def paper_white_nits(self, monitor_id, fallback=200.0):
        try:
            value = self.get(monitor_id)["target"].get("sdr_white_level_nits")
            if value and value > 0:
                return float(value)
        except Exception:
            pass
        return float(fallback)

    def monitor_rect(self, monitor_id):
        info = self.get(monitor_id)
        if not info:
            return None
        rect = info.get("monitor_rect")
        if rect:
            return rect
        return self.platform.monitor_rect(info["source"]["gdi_name"])

    def install_profile(self, monitor_id, path):
        info = self.get(monitor_id)
        self.platform.install_profile(path)
        self.platform.add_profile_association(info, os.path.basename(path))

    def remove_profile(self, monitor_id, name):
        filename = f"{name}.icc"
        info = self.get(monitor_id)
        self.platform.remove_profile_association(info, filename)
        self.platform.uninstall_profile(filename)


class InstrumentService:
    """Argyll spotread discovery and command-line construction."""

    def __init__(self, base_dir):
        self.executable = os.path.join(base_dir, "bin", "spotread.exe")

    def discover_options(self):
        if not os.path.isfile(self.executable):
            return [], []
        try:
            result = subprocess.run(
                [self.executable, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
                check=False,
            )
        except Exception:
            return [], []
        return self.parse_help(result.stdout)

    def list_options(self):
        instruments, modes = self.discover_options()
        return {
            "instruments": [
                {"code": str(code), "description": description}
                for code, description in instruments
            ],
            "modes": [
                {"code": str(code), "description": description}
                for code, description in modes
            ],
        }

    @staticmethod
    def parse_help(output):
        lines = [item.strip() for item in output.splitlines()]
        instruments = []
        for index, line in enumerate(lines):
            if "-c listno" not in line:
                continue
            for candidate in lines[index:]:
                if candidate != line and re.match(r"^-\w", candidate):
                    break
                match = re.match(r"\s*(\d+)\s*=\s*'(.+)'", candidate)
                if match:
                    instruments.append([match.group(1), match.group(2)])
            break

        modes = []
        start = next(
            (index for index, line in enumerate(lines) if re.match(r"^-y\s+", line)),
            None,
        )
        if start is not None:
            block = []
            for index in range(start, len(lines)):
                line = lines[index]
                if index == start:
                    line = line[3:]
                if index > start and re.match(r"^-\w", line):
                    break
                block.append(line.rstrip())
            for raw in block:
                if raw.strip():
                    parts = raw.split("    ")
                    modes.append([parts[0].strip(), parts[-1].strip()])
        return instruments, modes

    @staticmethod
    def build_args(
        instrument_descriptions,
        instrument_codes,
        selected_instrument,
        mode_descriptions,
        mode_codes,
        selected_mode,
    ):
        args = ["-x", "-e"]
        instrument_index = instrument_descriptions.index(selected_instrument)
        mode_index = mode_descriptions.index(selected_mode)
        if instrument_codes:
            args.extend(["-c", instrument_codes[instrument_index]])
        if mode_codes:
            args.extend(["-y", mode_codes[mode_index].split("|")[0]])
        return args


class ExternalProcessService:
    """External helper launching and meter/pattern-process lifetime."""

    def __init__(self, base_dir, state):
        self.base_dir = base_dir
        self.state = state

    def launch_tool(self, tool_name):
        script = os.path.join(self.base_dir, "tools", tool_name)
        if not os.path.isfile(script):
            raise FileNotFoundError(script)
        return subprocess.Popen(
            [sys.executable, script],
            cwd=os.path.dirname(script),
            env=os.environ.copy(),
        )

    def start_measurement(self, args, writer_factory=None, reader_factory=None):
        """Start dogegen/spotread lazily so importing the backend stays lightweight."""
        if writer_factory is None or reader_factory is None:
            from color_rw import ColorReader, ColorWriter

            writer_factory = writer_factory or ColorWriter
            reader_factory = reader_factory or ColorReader
        self.cleanup_measurement_processes()
        self.state.proc_color_write = writer_factory()
        try:
            self.state.proc_color_reader = reader_factory(args)
        except Exception:
            self.cleanup_measurement_processes()
            raise
        return self.state.proc_color_write, self.state.proc_color_reader

    def cleanup_measurement_processes(self):
        errors = []
        for field_name in ("proc_color_write", "proc_color_reader"):
            process = getattr(self.state, field_name)
            if process is None:
                continue
            try:
                process.terminate()
            except Exception as exc:
                errors.append(exc)
            finally:
                setattr(self.state, field_name, None)
        if errors:
            raise errors[0]


class CalibrationBackend:
    """Stateful backend facade consumable by Tk today and WinUI later."""

    def __init__(self, base_dir=None, log_path=None, display_platform=None):
        self.base_dir = os.path.abspath(base_dir or os.path.dirname(__file__))
        self.log_path = log_path or os.path.join(self.base_dir, "hc.log")
        self.state = CalibrationState()
        self.displays = DisplayService(self.state, display_platform)
        self.instruments = InstrumentService(self.base_dir)
        self.processes = ExternalProcessService(self.base_dir, self.state)
        self.reset_profile()

    def initialize_environment(self):
        self.displays.refresh()
        self.reload_history()

    def reload_history(self):
        self.reload_gray_history()
        self.reload_color_history()
        return self.state.gray_history_runs, self.state.color_history_runs

    def reload_gray_history(self):
        try:
            self.state.gray_history_runs = parse_gray_runs(self.log_path)
        except Exception as exc:
            logging.error(_("Failed to refresh gray history: {}").format(exc))
            self.state.gray_history_runs = []
        return self.state.gray_history_runs

    def reload_color_history(self):
        try:
            self.state.color_history_runs = parse_color_runs(self.log_path)
        except Exception as exc:
            logging.error(_("Failed to refresh color history: {}").format(exc))
            self.state.color_history_runs = []
        return self.state.color_history_runs

    def reset_profile(self):
        profile_path = os.path.join(self.base_dir, "data", "hdr_empty.icc")
        handle = ICCProfile(profile_path)
        data = handle.read_all()
        mhc2 = copy.deepcopy(data["MHC2"])
        if mhc2["red_lut"] == [0, 1]:
            identity = generate_pq_lut().tolist()
            mhc2["red_lut"] = identity.copy()
            mhc2["green_lut"] = identity.copy()
            mhc2["blue_lut"] = identity.copy()
            mhc2["entry_count"] = len(identity)
        self.state.icc_handle = handle
        self.state.icc_data = data
        self.state.MHC2 = mhc2
        self.state.clut_cache = None

    def begin_calibration(self, request):
        self.state.current_request = request
        self.state.selected_gray_run = request.gray_history_run
        self.state.selected_color_run = request.color_history_run
        self.state.eetf_args = copy.deepcopy(request.eetf_args)
        self.state.reused_color_run = None
        self.state.reused_color_card = None

    def history_only_calibration(self, request=None):
        if request is not None:
            return bool(
                request.gray_history_run is not None
                and request.color_history_run is not None
            )
        return bool(
            self.state.selected_gray_run is not None
            and self.state.selected_color_run is not None
        )

    def run_calibration_pipeline(
        self,
        measure_gamut_before: Callable[[], None],
        calibrate_pq: Callable[[], None],
        calibrate_chromaticity: Callable[[], None],
        measure_gamut_after: Callable[[], None],
    ):
        """Run the invariant calibration sequence without any frontend calls."""
        request = self.state.current_request
        if request is None:
            raise RuntimeError("begin_calibration() must be called first")
        self.reset_profile()
        if request.color_history_run is not None:
            self.apply_color_run(request.color_history_run, request)
            logging.info(
                _("Historical color data reused: skipped gamut and color-card measurement")
            )
        else:
            measure_gamut_before()
        calibrate_pq()
        calibrate_chromaticity()
        measure_gamut_after()

    def apply_color_run(self, run, request=None):
        request = request or self.state.current_request
        eetf_enabled = bool(request and request.eetf_enabled)
        eetf_args = request.eetf_args if request else self.state.eetf_args
        gamut = gamut_xyz_from_run(run)
        max_lumi, min_lumi = peak_min_luminance(run, eetf_enabled, eetf_args)
        self.apply_gamut_data(gamut, min_lumi, max_lumi)
        self.state.reused_color_run = run
        card = []
        for _index, rgb, _target, measured in run["points"]:
            code = (np.asarray(rgb, dtype=float) / 1023.0).tolist()
            xyz_nits = (np.asarray(measured, dtype=float) * 10000.0).tolist()
            card.append((code, xyz_nits))
        self.state.reused_color_card = card or None
        self.state.target_xyz = [
            list(np.asarray(target, float)) for _i, _r, target, _m in run["points"]
        ]
        self.state.measured_xyz = [
            list(np.asarray(measured, float))
            for _i, _r, _t, measured in run["points"]
        ]

    def apply_gamut_data(self, gamut_xyz, min_lumi, max_lumi):
        self.state.measure_gamut_xyz.update(gamut_xyz)
        logging.info(
            _(
                "Writing max full-frame luminance {}, peak luminance {}, min luminance {}"
            ).format(max_lumi, max_lumi, min_lumi)
        )
        mhc2 = self.state.MHC2
        mhc2["min_luminance"] = min_lumi
        mhc2["peak_luminance"] = max_lumi
        profile = self.state.icc_handle
        profile.write_XYZType("lumi", [[max_lumi, max_lumi, max_lumi]])
        xyz = self.state.measure_gamut_xyz
        red, green, blue, white = build_primaries_xyz_tags(
            xyz["red"], xyz["green"], xyz["blue"], xyz["white_paper"]
        )
        logging.info(
            _("Writing RGBW XYZ:\n {}\n {}\n {}\n {}").format(
                red, green, blue, white
            )
        )
        for tag, value in (
            ("rXYZ", red),
            ("gXYZ", green),
            ("bXYZ", blue),
            ("wtpt", white),
        ):
            profile.write_XYZType(tag, [value])
        curve = {
            "type": "curve",
            "values": srgb_encode(np.linspace(0, 1, 1024)).tolist(),
        }
        profile.write_rgbTRC({"rTRC": curve, "gTRC": curve, "bTRC": curve})
        profile.write_MHC2(mhc2)
        logging.info(_("Gamut measurement finished"))

    def apply_bright_luminance(self):
        lut = generate_inversed_lut(generate_bright_pq_lut())
        inverse = {
            "red_lut": lut.tolist(),
            "green_lut": lut.tolist(),
            "blue_lut": lut.tolist(),
        }
        xyz = self.state.measure_gamut_xyz
        white_rgb = apply_lut(XYZ_to_BT2020_PQ_rgb(xyz["white"] / 10000), inverse)
        black_rgb = apply_lut(XYZ_to_BT2020_PQ_rgb(xyz["black"] / 10000), inverse)
        white_xyz = BT2020_PQ_rgb_to_XYZ(white_rgb)
        black_xyz = BT2020_PQ_rgb_to_XYZ(black_rgb)
        logging.info(
            _("Brightness-compensated white RGB: {} XYZ: {}").format(
                white_rgb, white_xyz
            )
        )
        logging.info(
            _("Brightness-compensated black RGB: {} XYZ: {}").format(
                black_rgb, black_xyz
            )
        )
        return white_xyz[1] * 10000, black_xyz[1] * 10000

    def measured_color_samples(self):
        if self.state.reused_color_card:
            return self.state.reused_color_card
        measured, target = self.state.measured_xyz, self.state.target_xyz
        if not measured or not target or len(measured) != len(target):
            return None
        samples = []
        for target_xyz, measured_xyz in zip(target, measured):
            try:
                code = np.clip(
                    np.asarray(XYZ_to_BT2020_PQ_rgb(np.asarray(target_xyz, float))),
                    0.0,
                    1.0,
                )
                xyz_nits = np.asarray(measured_xyz, float) * 10000.0
            except Exception:
                continue
            samples.append((code, xyz_nits))
        return samples or None

    def make_display_model(self):
        mhc2 = self.state.MHC2
        xyz = self.state.measure_gamut_xyz
        required = ("red", "green", "blue", "white_paper")
        if any(key not in xyz or xyz[key] is None for key in required):
            raise ValueError(_("Primaries not measured yet; run calibration first"))
        gray = self.state.measured_pq
        usable_gray = gray if gray and len(gray.get("red", [])) > 4 else None
        return build_model_from_calibration(
            mhc2,
            xyz["red"],
            xyz["green"],
            xyz["blue"],
            xyz["white_paper"],
            peak_nits=mhc2.get("peak_luminance"),
            black_nits=mhc2.get("min_luminance") or 0.0,
            measured_pq=usable_gray,
            measured_colors=self.measured_color_samples(),
        )

    def model_signature(self):
        mhc2, xyz = self.state.MHC2, self.state.measure_gamut_xyz
        try:
            return (
                float(mhc2.get("peak_luminance") or 0.0),
                float(mhc2.get("min_luminance") or 0.0),
                mhc2["red_lut"][::16],
                mhc2["green_lut"][::16],
                mhc2["blue_lut"][::16],
                tuple(
                    round(float(value), 6)
                    for key in ("red", "green", "blue", "white_paper")
                    for value in xyz[key]
                ),
            )
        except Exception:
            return None

    def make_clut_tags(self, grid):
        model = self.make_display_model()
        signature = (int(grid), self.model_signature())
        cache = self.state.clut_cache
        if cache is None or cache[0] != signature:
            tags = make_clut_tags(
                model, grid=int(grid), fmt=CLUT_FORMAT_MFT2, with_b2a=True
            )
            self.state.clut_cache = (signature, tags)
        else:
            tags = cache[1]
        return tags, model

    @staticmethod
    def report_clut(tags, model):
        meta = tags["_meta"]
        text = _(
            "CLUT written: {} ({}³, {:.0f} KB, interpolation error {:.3f}%)"
        ).format(
            "A2B0/B2A0",
            meta["grid"],
            sum(meta["tag_sizes"].values()) / 1024.0,
            meta["a2b_interp_mean_err"] * 100.0,
        )
        logging.info(text)
        if meta["a2b_interp_max_err"] > 0.02:
            logging.warning(
                _(
                    "CLUT self-check: max interpolation error {:.3f}% (near the PCS ceiling)"
                ).format(meta["a2b_interp_max_err"] * 100.0)
            )
        summary = model.color_accuracy_summary()
        if summary:
            logging.info(
                _("CLUT colour check (A2B vs measured card): {}").format(summary)
            )
            mean_de = model.color_accuracy_report()["mean_de_itp"]
            if mean_de > 15.0:
                logging.warning(
                    _(
                        "CLUT colour check: mean dE ITP {:.2f} is high — the "
                        "forward model deviates strongly from the measured "
                        "colour card (reused historical data may come from "
                        "another display or an incompatible session)"
                    ).format(mean_de)
                )
        return text

    def write_clut(self, enabled, grid):
        if not enabled:
            return None, None
        tags, model = self.make_clut_tags(grid)
        written = write_clut_tags(self.state.icc_handle, tags)
        return written, self.report_clut(tags, model)

    def attach_clut_tags(self, tags):
        return write_clut_tags(self.state.icc_handle, tags)

    def save_profile(self, path, description=None):
        if description is not None:
            desc = [{"lang": "en", "country": "US", "text": description}]
            self.state.icc_handle.write_desc(desc)
        self.state.icc_handle.rebuild()
        self.state.icc_handle.save(path)
