"""Frontend-neutral execution adapter for the existing calibration algorithms.

The numerical steps in this module intentionally mirror the legacy Tk controller.
Only interaction, progress reporting, cancellation and process lifetime are new.
"""

from __future__ import annotations

import copy
import logging
import os
import tempfile
import time
import uuid

import numpy as np

from color_history import peak_min_luminance_from_xyz
from color_test_suit import (
    COLOR_CARD_SRGB_24,
    get_D65_white_calibrate_test_XYZ_suit,
    get_P3D65_calibrate_XYZ_suit,
    get_srgb_24_calibrate_XYZ_suit,
    get_srgb_calibrate_XYZ_suit,
)
from convert_utils import XYZ_to_BT2020_PQ_rgb, pq_oetf
from i18n.i18n_loader import _
from lut import generate_bright_pq_lut, generate_mhc2_lut_from_measured_pq
from matrix import calculate_bradford_matrix, fit_XYZ2XYZ_wlock_dropY
from meta_data import D65_WHITE_POINT


class CalibrationAborted(Exception):
    """Raised when a frontend prompt chooses cancel."""


class OperationLogHandler(logging.Handler):
    """Forward records produced by the unchanged algorithms to one operation."""

    def __init__(self, context):
        super().__init__(logging.DEBUG)
        self.context = context
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.context.log(record.levelname.lower(), self.format(record))
        except Exception:
            self.handleError(record)


class CalibrationWorkflow:
    """Run one calibration using backend state and explicit interaction ports."""

    def __init__(self, backend, context):
        self.backend = backend
        self.state = backend.state
        self.context = context
        self._preview_name = None

    def run(self):
        self.context.check_cancelled()
        request = self.state.current_request
        if request is None:
            raise RuntimeError("begin_calibration() must be called first")
        display = self.backend.displays.get(request.monitor_id)
        if display is None:
            raise ValueError(_("The selected display is no longer available"))
        if display.get("color_work_status") != "hdr":
            raise ValueError(
                _("The selected screen HDR is off. Please enable HDR in system settings before calibration.")
            )

        paper_white = self.backend.displays.paper_white_nits(request.monitor_id)
        paper_code = int(round(pq_oetf(paper_white) * 1023))
        self.state.gamut_test_rgb["white_paper"] = [paper_code] * 3
        logging.info(
            _("SDR paper white: {:.1f} nits, white test patch code: {}").format(
                paper_white, paper_code
            )
        )

        need_meter = not self.backend.history_only_calibration(request)
        try:
            if need_meter:
                self._start_meter(request)
            else:
                logging.info(
                    _("Historical data is enough: no colorimeter needed for this calibration")
                )

            self.backend.run_calibration_pipeline(
                self.measure_gamut_before,
                lambda: self.calibrate_pq(request.eetf_enabled),
                self.calibrate_chromaticity,
                self.measure_gamut_after,
            )
            self.context.check_cancelled()
            return {
                "profileReady": True,
                "monitorId": request.monitor_id,
                "historyOnly": not need_meter,
                "grayscaleSamples": len(self.state.measured_pq.get("red", [])),
                "colorSamples": len(self.state.measured_xyz or []),
                "peakLuminance": self.state.MHC2.get("peak_luminance"),
                "minLuminance": self.state.MHC2.get("min_luminance"),
            }
        finally:
            self._remove_preview()
            self.backend.processes.cleanup_measurement_processes()

    def _start_meter(self, request):
        options = self.backend.instruments.list_options()
        instruments = options["instruments"]
        modes = options["modes"]
        try:
            instrument_code = next(
                item["code"] for item in instruments
                if item["description"] == request.instrument_description
            )
            mode_code = next(
                item["code"] for item in modes
                if item["description"] == request.instrument_mode_description
            )
        except StopIteration as exc:
            raise ValueError(_("The selected colorimeter or measurement mode is unavailable")) from exc
        args = ["-x", "-e", "-c", instrument_code, "-y", mode_code.split("|")[0]]
        self.context.progress("instrument-start", 0, 1, _("Starting colorimeter"))
        self.backend.processes.start_measurement(args)
        reader = self.state.proc_color_reader
        while reader.status == "need_calibration":
            response = self.context.prompt(
                "instrument-calibration",
                _("need_calibration"),
                _("Spot read needs a calibration before continuing \nPlace the instrument on its reflective white reference then click OK."),
                ["continue", "cancel"],
                "continue",
            )
            if response.get("choice") != "continue":
                raise CalibrationAborted()
            reader.calibrate()
            self.context.check_cancelled()
        self.state.proc_color_write.write_rgb([800, 800, 800])
        response = self.context.prompt(
            "place-instrument",
            _("Place the colorimeter"),
            _("Move the white window to the target screen, resize it to fully cover the meter, place the meter on the window, then click OK."),
            ["continue", "cancel"],
            "continue",
        )
        if response.get("choice") != "continue":
            raise CalibrationAborted()
        self.context.progress("instrument-start", 1, 1, _("Colorimeter ready"))

    def _measure(self, rgb, delay, phase, current, total, message=""):
        self.context.check_cancelled()
        self.context.progress(phase, current, total, message)
        try:
            self.state.proc_color_write.write_rgb(rgb, delay=delay)
        except Exception:
            self.context.check_cancelled()
            raise
        self.context.check_cancelled()
        try:
            value = self.state.proc_color_reader.read_XYZ()
        except Exception:
            self.context.check_cancelled()
            raise
        self.context.check_cancelled()
        return value

    def _install_preview(self):
        self.context.check_cancelled()
        self._remove_preview()
        request = self.state.current_request
        name = "CC_" + str(uuid.uuid4())
        path = os.path.join(tempfile.gettempdir(), name + ".icc")
        self.backend.save_profile(path, name)
        try:
            self.backend.displays.install_profile(request.monitor_id, path)
            self._preview_name = name
            self.state.preview_icc_name = name
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        time.sleep(0.5)

    def _remove_preview(self):
        if not self._preview_name:
            return
        name, self._preview_name = self._preview_name, None
        self.state.preview_icc_name = None
        try:
            self.backend.displays.remove_profile(
                self.state.current_request.monitor_id, name
            )
        except Exception:
            logging.exception("Failed to remove calibration preview ICC")

    def measure_gamut_before(self):
        self._install_preview()
        items = list(self.state.gamut_test_rgb.items())
        for index, (color, rgb) in enumerate(items, 1):
            xyz = self._measure(rgb, 0.1, "gamut-before", index, len(items), color)
            logging.info(_("Color {} measured XYZ: {}").format(color, xyz))
            self.state.measure_gamut_xyz[color] = xyz

        max_lumi, min_lumi = peak_min_luminance_from_xyz(
            self.state.measure_gamut_xyz,
            self.state.current_request.eetf_enabled,
            self.state.current_request.eetf_args,
        )
        start_lumi = self.state.measure_gamut_xyz["black"][1]
        delta = max(start_lumi * 0.01, 0.0005)
        logging.info(
            _("Start binary search for activated black: start_lumi={} delta={}").format(
                start_lumi, delta
            )
        )

        def measure_gray(code):
            rgb = [code, code, code]
            xyz = self._measure(rgb, 0.1, "activated-black", 0, 0, str(code))
            logging.info(_("Gray test code={} RGB={} measured XYZ: {}").format(code, rgb, xyz))
            return xyz

        high_xyz = measure_gray(255)
        if high_xyz[1] <= start_lumi + delta:
            logging.info(_("No significant luminance increase found in 0-255 range; skipping activated black detection"))
            self.state.measure_gamut_xyz["min_activated_black"] = self.state.measure_gamut_xyz["black"]
        else:
            lo, hi = 1, 255
            found_code = found_xyz = None
            while lo <= hi:
                mid = (lo + hi) // 2
                xyz = measure_gray(mid)
                if xyz[1] > start_lumi + delta:
                    found_code, found_xyz, hi = mid, xyz, mid - 1
                else:
                    lo = mid + 1
            if found_xyz is not None:
                self.state.measure_gamut_xyz["min_activated_black"] = found_xyz
                logging.info(_("Activated black level found: code={} XYZ={}").format(found_code, found_xyz))
            else:
                logging.info(_("Activated black not found (grayscale differences may be below threshold)"))
        self.backend.apply_gamut_data(self.state.measure_gamut_xyz, min_lumi, max_lumi)

    def measure_gamut_after(self):
        self._install_preview()
        if self.state.reused_color_run is not None:
            logging.info(_("Historical color data reused: skipped the post-calibration gamut re-measurement"))
            max_lumi = self.state.MHC2.get("peak_luminance", 0.0)
            min_lumi = self.state.MHC2.get("min_luminance", 0.0)
            if self.state.current_request.bright_mode:
                max_lumi, min_lumi = self.backend.apply_bright_luminance()
            self.backend.apply_gamut_data(self.state.measure_gamut_xyz, min_lumi, max_lumi)
            self.context.progress("gamut-after", 1, 1, _("Historical color data reused"))
            return
        items = list(self.state.gamut_test_rgb.items())
        for index, (color, rgb) in enumerate(items, 1):
            xyz = self._measure(rgb, 0.1, "gamut-after", index, len(items), color)
            logging.info(_("Color {} measured XYZ: {}").format(color, xyz))
            self.state.measure_gamut_xyz[color] = xyz
        max_lumi = self.state.measure_gamut_xyz["white"][1]
        min_lumi = self.state.measure_gamut_xyz["black"][1]
        if self.state.current_request.bright_mode:
            max_lumi, min_lumi = self.backend.apply_bright_luminance()
        self.backend.apply_gamut_data(self.state.measure_gamut_xyz, min_lumi, max_lumi)

    def calibrate_pq(self, eetf=False):
        self._install_preview()
        logging.info(_("Start calibrating PQ grayscale curve"))
        measured = self.state.measured_pq
        measured["red"], measured["green"], measured["blue"] = [], [], []
        wp = [float(value.strip()) for value in self.state.current_request.white_point.split(",")]
        matrix = calculate_bradford_matrix(wp, D65_WHITE_POINT)
        num = self.state.current_request.grayscale_samples
        history_run = self.state.current_request.gray_history_run
        if history_run is not None:
            num = history_run["num"]
            logging.info(_("Using historical gray data: {} points from {}").format(
                num, history_run["end"].strftime("%m-%d %H:%M:%S")))
            for index, (_code, xyz) in enumerate(history_run["points"], 1):
                self.context.check_cancelled()
                self.context.progress("measure-gray", index, num, _("Historical gray data"))
                rgb_measured = XYZ_to_BT2020_PQ_rgb((matrix @ xyz) / 10000)
                for channel, value in zip(("red", "green", "blue"), rgb_measured):
                    measured[channel].append(float(value))
        else:
            codes = np.linspace(0, 1023, num, endpoint=True).round().astype(np.int32)
            for index, grayscale in enumerate(codes, 1):
                grayscale = int(grayscale)
                rgb = [grayscale] * 3
                xyz = self._measure(rgb, 0.03, "measure-gray", index, num, str(grayscale))
                rgb_measured = XYZ_to_BT2020_PQ_rgb((matrix @ xyz) / 10000)
                logging.info(_("({}/{}) Output RGB: {} Measured XYZ: {} RGB: {}").format(
                    index, num, rgb, xyz, rgb_measured * 1023))
                for channel, value in zip(("red", "green", "blue"), rgb_measured):
                    measured[channel].append(float(value))

        eetf_args = None
        if eetf:
            eetf_args = copy.deepcopy(self.state.eetf_args)
            if self.state.eetf_args["monitor_max"] is None:
                eetf_args["monitor_max"] = self.state.measure_gamut_xyz["white"][1]
            if self.state.eetf_args["monitor_min"] is None:
                eetf_args["monitor_min"] = self.state.measure_gamut_xyz["min_activated_black"][1]
        target_pq = self.state.MHC2
        if self.state.current_request.bright_mode:
            bright = generate_bright_pq_lut().tolist()
            target_pq = {"red_lut": bright, "green_lut": bright, "blue_lut": bright}
        red_lut = generate_mhc2_lut_from_measured_pq(measured["red"], target_pq=target_pq["red_lut"])
        blue_lut = generate_mhc2_lut_from_measured_pq(measured["blue"], target_pq=target_pq["blue_lut"])
        green_lut = generate_mhc2_lut_from_measured_pq(measured["green"], target_pq=target_pq["green_lut"])
        self.state.MHC2["red_lut"] = red_lut.tolist()
        self.state.MHC2["green_lut"] = green_lut.tolist()
        self.state.MHC2["blue_lut"] = blue_lut.tolist()
        self.state.MHC2["entry_count"] = len(red_lut)
        self.state.icc_handle.write_MHC2(self.state.MHC2)
        logging.info(_("PQ LUT measurement finished"))

    def calibrate_chromaticity(self):
        self._install_preview()
        logging.info(_("Start color measurement and generate matrix"))
        if self.state.reused_color_run is not None:
            self.state.measured_xyz = [np.array(value, dtype=float) for value in self.state.measured_xyz]
            self.state.target_xyz = [np.array(value, dtype=float) for value in self.state.target_xyz]
            matrix = fit_XYZ2XYZ_wlock_dropY(
                self.state.measured_xyz, self.state.target_xyz,
                self.state.measured_xyz[-1], self.state.target_xyz[-1])
            logging.info(_("Chromaticity fit matrix (diagnostic only): {}").format(matrix.flatten().tolist()))
            self._write_identity_matrix()
            self.context.progress("measure-color", 1, 1, _("Historical color data reused"))
            return

        paper_white = self.backend.displays.paper_white_nits(self.state.current_request.monitor_id)
        mode = self.state.current_request.color_sample_set
        if mode.startswith(COLOR_CARD_SRGB_24):
            target = get_srgb_24_calibrate_XYZ_suit(self.state.measure_gamut_xyz, paper_white)
        else:
            target = get_srgb_calibrate_XYZ_suit(self.state.measure_gamut_xyz, paper_white)
        if "+DisplayP3" in mode:
            target.extend(get_P3D65_calibrate_XYZ_suit(self.state.measure_gamut_xyz, paper_white))
        target.extend(get_D65_white_calibrate_test_XYZ_suit(self.state.measure_gamut_xyz, paper_white))
        self.state.target_xyz = target
        logging.info(_("Color sample set: {}, {} samples").format(mode, len(target)))
        self.state.measured_xyz = []
        wp = [float(value.strip()) for value in self.state.current_request.white_point.split(",")]
        adapt = calculate_bradford_matrix(wp, D65_WHITE_POINT)
        logging.info(_("Color measurement white point: {}").format(self.state.current_request.white_point))
        total = len(target)
        for index, item in enumerate(target, 1):
            rgb = (XYZ_to_BT2020_PQ_rgb(item) * 1023).round().astype(int)
            xyz = self._measure(rgb, 0.1, "measure-color", index, total, str(rgb.tolist()))
            xyz = adapt @ [float(value) / 10000 for value in xyz]
            logging.info(_("({}) Color: {} Target XYZ:{} Measured: {}").format(index / total, rgb, item, xyz))
            self.state.measured_xyz.append(xyz)
        matrix = fit_XYZ2XYZ_wlock_dropY(
            self.state.measured_xyz, target,
            self.state.measured_xyz[-1], target[-1])
        logging.info(_("Chromaticity fit matrix (diagnostic only): {}").format(matrix.flatten().tolist()))
        self._write_identity_matrix()

    def _write_identity_matrix(self):
        self.state.MHC2["matrix"] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        self.state.icc_handle.write_MHC2(self.state.MHC2)
        logging.info(_("Color matrix measurement finished, matrix: {}").format(self.state.MHC2["matrix"]))
