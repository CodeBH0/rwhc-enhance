from lut import *
from meta_data import *
from matrix import *
from convert_utils import *
from delteE import *
from clut_icc import (
    CLUT_GRID_DEFAULT,
)
from color_test_suit import *
from color_rw import ColorReader, ColorWriter
from log import logging, TextHandler
from i18n.i18n_loader import _
from color_history import (
    peak_min_luminance_from_xyz as color_run_peak_min_xyz,
    run_label as color_run_label,
    run_summary as color_run_summary,
)
from app_ui import (
    build_main_window,
    freeze_window,
    show_monitor_overlay,
    unfreeze_window,
)
from calibration_backend import CalibrationBackend, CalibrationRequest, StateField

from tkinter import filedialog, ttk, Canvas
import tkinter as tk
import numpy as np
import webbrowser
import threading
import traceback
import tempfile
import ctypes
import time
import uuid
import copy
import os


class HDRCalibrationUI:
    # Transitional compatibility surface: existing calibration algorithms keep
    # their field names, while ownership now lives in the GUI-independent backend.
    icc_handle = StateField("icc_handle")
    icc_data = StateField("icc_data")
    MHC2 = StateField("MHC2")
    target_xyz = StateField("target_xyz")
    convert_command = StateField("convert_command")
    measured_xyz = StateField("measured_xyz")
    gamut_test_rgb = StateField("gamut_test_rgb")
    measure_gamut_xyz = StateField("measure_gamut_xyz")
    preview_icc_name = StateField("preview_icc_name")
    measured_pq = StateField("measured_pq")
    gray_history_runs = StateField("gray_history_runs")
    selected_gray_run = StateField("selected_gray_run")
    color_history_runs = StateField("color_history_runs")
    selected_color_run = StateField("selected_color_run")
    reused_color_run = StateField("reused_color_run")
    reused_color_card = StateField("reused_color_card")
    proc_color_write = StateField("proc_color_write")
    proc_color_reader = StateField("proc_color_reader")
    icc_change_delay = StateField("icc_change_delay")
    _clut_cache = StateField("clut_cache")
    eetf_args = StateField("eetf_args")
    displays_config = StateField("displays_config")
    human_display_config_map = StateField("human_display_config_map")
    current_request = StateField("current_request")

    def __init__(self, root, backend=None):
        self.backend = backend or CalibrationBackend(base_dir=os.path.dirname(__file__))
        self._eetf_window = None

        self.project_url = "https://github.com/forbxy/rwhc"
        self.argyll_download_url = "https://www.argyllcms.com/downloadwin.html"

        self.root = root
        self.root.title("RealWindowsHDRCalibrator")
        self.set_dpi_awareness()

        self.backend.initialize_environment()
        self.log_path = self.backend.log_path

        build_main_window(self)
        self.init_logging()
        self.on_monitor_changed()
        

    def init_logging(self):
        th = TextHandler(self.log_text)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        th.setFormatter(fmt)
        root_logger = logging.getLogger()
        root_logger.addHandler(th)

        log_path = getattr(self, "log_path", os.path.join(os.path.dirname(__file__), "hc.log"))
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        root_logger.addHandler(fh)

        root_logger.setLevel(logging.INFO)
        logging.info(_("Application started"))

    def init_base_icc(self):
        self.backend.reset_profile()

    def set_dpi_awareness(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1) 
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    def _gray_run_label(self, run):
        """Human-readable label for a historical gray run (by measurement end time)."""
        return _("{} · {} pts").format(run["end"].strftime("%m-%d %H:%M:%S"), run["num"])

    def _build_gray_history_choices(self):
        """First item is always 'live measurement'; then one entry per complete run."""
        choices = [_("Live measurement")]
        for run in self.gray_history_runs:
            choices.append(self._gray_run_label(run))
        return choices

    def on_gray_history_selected(self, event=None):
        """Combobox changed: remember the picked historical run (or None for live)."""
        label = self.gray_history_var.get()
        self.selected_gray_run = None
        if label == _("Live measurement"):
            return
        for run in self.gray_history_runs:
            if self._gray_run_label(run) == label:
                self.selected_gray_run = run
                logging.info(_("Selected historical gray data: {}").format(label))
                return

    def refresh_gray_history(self):
        """Re-parse hc.log and refresh the combobox (keep current selection if still valid)."""
        self.backend.log_path = self.log_path
        self.gray_history_runs = self.backend.reload_gray_history()
        prev = self.gray_history_var.get() if hasattr(self, "gray_history_var") else ""
        self.gray_history_choices = self._build_gray_history_choices()
        if hasattr(self, "gray_history_menu"):
            self.gray_history_menu["values"] = self.gray_history_choices
        if prev in self.gray_history_choices:
            self.gray_history_var.set(prev)
        else:
            self.gray_history_var.set(self.gray_history_choices[0])
        self.on_gray_history_selected()
        logging.info(_("Gray history refreshed: {} runs").format(len(self.gray_history_runs)))

    # ------------------------------------------------------------------
    # 历史颜色数据（原色/白点 + 色卡）
    # ------------------------------------------------------------------
    def _color_run_label(self, run):
        """Human-readable label for a historical colour run."""
        return color_run_label(run)

    def _build_color_history_choices(self):
        """First item is always 'live measurement'; then one entry per complete run."""
        choices = [_("Live measurement")]
        for run in self.color_history_runs:
            choices.append(self._color_run_label(run))
        return choices

    def on_color_history_selected(self, event=None):
        """Combobox changed: remember the picked historical colour run (or None)."""
        label = self.color_history_var.get()
        self.selected_color_run = None
        if label == _("Live measurement"):
            return
        for run in self.color_history_runs:
            if self._color_run_label(run) == label:
                self.selected_color_run = run
                logging.info(_("Selected historical color data: {}").format(label))
                return

    def refresh_color_history(self):
        """Re-parse hc.log and refresh the colour combobox (keep selection if valid)."""
        self.backend.log_path = self.log_path
        self.color_history_runs = self.backend.reload_color_history()
        prev = self.color_history_var.get() if hasattr(self, "color_history_var") else ""
        self.color_history_choices = self._build_color_history_choices()
        if hasattr(self, "color_history_menu"):
            self.color_history_menu["values"] = self.color_history_choices
        if prev in self.color_history_choices:
            self.color_history_var.set(prev)
        else:
            self.color_history_var.set(self.color_history_choices[0])
        self.on_color_history_selected()
        logging.info(_("Color history refreshed: {} runs").format(len(self.color_history_runs)))

    def _apply_color_run(self, run):
        """
        把一条历史颜色数据还原成本次校准的状态。

        复用内容与本次「实时测量」完全一致：
          · 六个色域测试点（原色/白点/纸白/黑）→ self.measure_gamut_xyz
          · 激活黑（二分搜索结果）→ min_activated_black
          · 峰值/黑场亮度 → MHC2 与 lumi 标签
          · rXYZ/gXYZ/bXYZ/wtpt + sRGB TRC + MHC2 写回 profile 句柄
          · 色卡样本 → 供 CLUT 正向模型校验用（self.reused_color_card）
        这些都发生在 `measure_gamut_before` 真正测量时同一条代码路径上。
        """
        self.backend.apply_color_run(run, self.current_request)

    def _history_only_calibration(self):
        """
        两个历史数据都选中时，本次校准完全不需要测量。

        · 灰阶曲线来自「历史灰阶数据」（`calibrate_pq` 走复用分支）；
        · 原色/白点/纸白/黑场与色卡来自「历史颜色数据」（`_apply_color_run`）；
        · 「校准后」重测（`measure_gamut_after`）也被复用分支取代。

        因此可以不启动 dogegen 与 spotread，直接产出 ICC。
        """
        return self.backend.history_only_calibration()

    def _capture_calibration_request(self):
        """Translate Tk values into the backend's GUI-independent request DTO."""
        return CalibrationRequest(
            monitor_id=self.monitor_var.get(),
            instrument_description=self.instrument_var.get(),
            instrument_mode_description=self.mode_var.get(),
            grayscale_samples=int(self.pq_points_var.get()),
            color_sample_set=self.color_space_var.get(),
            white_point=self.white_point_var.get(),
            bright_mode=bool(self.bright_var.get()),
            eetf_enabled=bool(self.eetf_var.get()),
            eetf_args=copy.deepcopy(self.eetf_args),
            clut_enabled=bool(self.clut_var.get()),
            clut_grid=int(self.clut_grid_var.get() or CLUT_GRID_DEFAULT),
            install_profile=bool(self.icc_set_var.get()),
            gray_history_run=self.selected_gray_run,
            color_history_run=self.selected_color_run,
        )

    def run_in_thread(self, worker, on_done):
        """
        worker: a function that takes no arguments (or only uses closure variables) — returns a result
        on_done(result): callback executed on the main (GUI) thread
        """

        def _wrap():
            try:
                res = worker()
            except Exception as e:
                res = e
            
            self.root.after(0, lambda: on_done(res))

        threading.Thread(target=_wrap, daemon=True).start()

    def get_instrument_mode_options(self):
        """
        Dynamically parse the output of spotread.exe --help
        to extract available instruments and their device file names.
        """
        c_list, y_list = self.backend.instruments.discover_options()
        if not y_list:
            logging.error(_("Failed to parse spotread -y modes; the output format may have changed"))
        return c_list, y_list

    def get_spotread_args(self, request=None):
        selected_instrument = (
            request.instrument_description if request else self.instrument_var.get()
        )
        selected_mode = (
            request.instrument_mode_description if request else self.mode_var.get()
        )
        return self.backend.instruments.build_args(
            self.instrument_desc,
            self.instrument_choose,
            selected_instrument,
            self.mode_desc,
            self.mode_choose,
            selected_mode,
        )
    
    def open_tools(self, tool_name):
        try:
            self.backend.processes.launch_tool(tool_name)
        except FileNotFoundError as e:
            missing = e.filename or (e.args[0] if e.args else tool_name)
            tk.messagebox.showerror(_("Error"), _("File not found: {}").format(missing))
        except Exception as e:
            tk.messagebox.showerror(_("Error"), _("Failed to launch {}: {}").format(tool_name, e))


    def open_project_homepage(self):
        webbrowser.open(self.project_url)

    def open_user_guide_window(self):
        webbrowser.open(self.project_url)

    def open_argyll_download(self):
        webbrowser.open(self.argyll_download_url)

    def set_icc(self, path, monitor_id=None):
        """
        path: The file path of the ICC profile to install.
        Install the specified ICC file, 
        associate it with the currently selected display, 
        and set it as that display's default ICC.
        """
        monitor = monitor_id or self.monitor_var.get()
        self.backend.displays.install_profile(monitor, path)

    def clean_icc(self, name, monitor_id=None):
        """
        name: The name of the ICC profile to clean (without .icc or .icm).
        Unassociate the specified ICC file from the currently selected display, 
        unset it as that display's default profile, 
        and remove the ICC file from the system.
        """
        monitor = monitor_id or self.monitor_var.get()
        self.backend.displays.remove_profile(monitor, name)

    def freeze_ui(self):
        freeze_window(self)

    def unfreeze_ui(self):
        unfreeze_window(self)

    @staticmethod
    def safe_call(func):
        """
        Decorator for GUI action methods that:
        - Catches and logs any exception raised by the wrapped function.
        - Ensures cleanup steps run on error (unfreeze UI, stop reader/writer processes).
        - Resets transient ICC/preview state to a safe default.
        - Prevents the exception from propagating to the caller (avoids crashing the GUI thread).
        """
        def wrapper(*args, **kwargs):
            self = args[0]
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logging.error(_("Error while executing {}: {}").format(func.__name__, e))
                logging.error(traceback.format_exc())
                try:
                    self.unfreeze_ui()
                except Exception:
                    pass
                try:
                    self.clean_color_rw_process()
                except Exception:
                    pass
                self.icc_change_delay = 0
                if self.preview_icc_name:
                    try:
                        self.preview_var.set(False)
                    except Exception:
                        pass

        return wrapper

    def clean_color_rw_process(self):
        try:
            self.backend.processes.cleanup_measurement_processes()
        except Exception as e:
            logging.error(_("Error cleaning color read/write processes: {}").format(e))
            logging.error(traceback.format_exc())

    def on_exit(self):
        # exit clean
        try:
            if self.preview_icc_name:
                self.clean_icc(self.preview_icc_name)
            self.clean_color_rw_process()
        except Exception as e:
            logging.error(_("Error during on_exit: {}").format(e))
            logging.error(traceback.format_exc())
        try:
            self.root.destroy()
        except Exception:
            pass

    def on_monitor_changed(self, event=None):
        """
        Display change notification: 
        show a short, undecorated overlay centered on the selected monitor 
        """
        sel = self.monitor_var.get()
        info = self.human_display_config_map.get(sel)
        if not info:
            return

        mon = info.get("monitor_rect")
        if not mon:
            try:
                mon = self.backend.displays.monitor_rect(sel)
            except Exception:
                return

        show_monitor_overlay(self.root, mon, _("Selected this display"))

    def on_eetf_toggle(self):
        if self.eetf_var.get():
            self.open_eetf_window()
        else:
            self.eetf_args = {
                "source_max": 10000,
                "source_min": 0,
                "monitor_max": None,
                "monitor_min": None,
            }

    def open_eetf_window(self):
        if self._eetf_window and self._eetf_window.winfo_exists():
            self._eetf_window.lift()
            return

        win = tk.Toplevel(self.root)
        win.title(_("EETF parameters"))
        win.geometry("360x330")
        win.resizable(True, True)
        self._eetf_window = win

        frm = tk.Frame(win, padx=14, pady=12)
        frm.pack(fill="both", expand=True)

        src_max_default = self.eetf_args.get("source_max", 10000)
        src_min_default = self.eetf_args.get("source_min", 0)
        mon_max_default = self.eetf_args.get("monitor_max")
        mon_min_default = self.eetf_args.get("monitor_min")

        v_src_max = tk.StringVar(
            value=str(src_max_default if src_max_default is not None else "10000")
        )
        v_src_min = tk.StringVar(
            value=str(src_min_default if src_min_default is not None else "0")
        )
        v_max = tk.StringVar(
            value=("" if mon_max_default is None else f"{mon_max_default}")
        )
        v_min = tk.StringVar(
            value=("" if mon_min_default is None else f"{mon_min_default}")
        )

        tk.Label(frm, text=_("Source max luminance (nit):"), font=("Microsoft YaHei", 13)).grid(
            row=0, column=0, sticky="w", pady=6
        )
        e1 = ttk.Entry(frm, textvariable=v_src_max, width=18)
        e1.grid(row=0, column=1, sticky="w", pady=6)

        tk.Label(frm, text=_("Source min luminance (nit):"), font=("Microsoft YaHei", 13)).grid(
            row=1, column=0, sticky="w", pady=6
        )
        e2 = ttk.Entry(frm, textvariable=v_src_min, width=18)
        e2.grid(row=1, column=1, sticky="w", pady=6)

        tk.Label(frm, text=_("Display max luminance (nit):"), font=("Microsoft YaHei", 13)).grid(
            row=2, column=0, sticky="w", pady=6
        )
        e3 = ttk.Entry(frm, textvariable=v_max, width=18)
        e3.grid(row=2, column=1, sticky="w", pady=6)

        tk.Label(frm, text=_("Display min luminance (nit):"), font=("Microsoft YaHei", 13)).grid(
            row=3, column=0, sticky="w", pady=6
        )
        e4 = ttk.Entry(frm, textvariable=v_min, width=18)
        e4.grid(row=3, column=1, sticky="w", pady=6)

        tk.Label(
            frm,
            text=_("Tip: leave display max/min empty to use measured values."),
            font=("Microsoft YaHei", 13),
            fg="#333333",
            wraplength=320,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        btns = tk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, pady=(14, 0), sticky="e")

        def on_ok():
            # Parse to float; allow empty values as None
            try:
                smx = float(v_src_max.get())
                smn = float(v_src_min.get())
            except Exception:
                tk.messagebox.showerror(_("Error"), _("Source luminance must be numeric"))
                return
            try:
                mmx = float(v_max.get()) if v_max.get().strip() != "" else None
                mmn = float(v_min.get()) if v_min.get().strip() != "" else None
            except Exception:
                tk.messagebox.showerror(_("Error"), _("Display luminance must be numeric or left blank"))
                return

            if smx <= 0 or smn < 0 or smn > smx:
                tk.messagebox.showerror(
                    _("Error"), _("Source max must be > 0 and source min must be <= source max")
                )
                return

            if mmx is not None and mmx <= 0:
                tk.messagebox.showerror(_("Error"), _("Display max luminance must be > 0"))
                return
            if (mmx is not None and mmn is not None) and (mmn < 0 or mmn > mmx):
                tk.messagebox.showerror(_("Error"), _("Display min luminance must be within [0, max]"))
                return

            self.eetf_args = {
                "source_max": smx,
                "source_min": smn,
                "monitor_max": mmx,
                "monitor_min": mmn,
            }
            logging.info(_("EETF params: %s"), self.eetf_args)
            win.destroy()

        def on_cancel():
            self.eetf_var.set(False)
            win.destroy()

        ttk.Button(btns, text=_("OK"), command=on_ok, width=10).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(btns, text=_("Cancel"), command=on_cancel, width=10).pack(side="right")

        win.bind("<Return>", lambda e: on_ok())
        win.bind("<Escape>", lambda e: on_cancel())

    def on_preview_toggle(self):
        """
        Implement a preview: loading a temporary ICC profile onto the selected display.
        before measurement load hdr_empty.icc as a preview; 
        after measurement preview the measured results 
        """
        state = self.preview_var.get()
        if state:
            if self.preview_icc_name:
                self.clean_icc(self.preview_icc_name)
                self.preview_icc_name = None
                time.sleep(self.icc_change_delay)
            self.preview_icc_name = "CC_" + str(uuid.uuid4())
            temp_dir = tempfile.gettempdir()
            icc_file_name = self.preview_icc_name + ".icc"
            path = os.path.join(temp_dir, icc_file_name)
            desc = [{"lang": "en", "country": "US", "text": self.preview_icc_name}]
            self.icc_handle.write_desc(desc)
            self.icc_handle.rebuild()
            self.icc_handle.save(path)
            self.set_icc(path)
            os.remove(path)
            time.sleep(self.icc_change_delay)
        else:
            if self.preview_icc_name:
                self.clean_icc(self.preview_icc_name)
                self.preview_icc_name = None
                time.sleep(self.icc_change_delay)

    def temp_save_icc(self, path):
        # debug only
        name_without_ext = os.path.splitext(os.path.basename(path))[0]
        # 预览必须与最终保存的文件一致，所以这里也写入 CLUT（结果有缓存，
        # 只有校准数据变了才会重新生成）。
        try:
            self.write_clut_if_enabled()
        except Exception as exc:
            logging.error(_("CLUT generation failed: {}").format(exc))
        self.backend.save_profile(path, name_without_ext)

    def get_paper_white_nit(self):
        """
        返回当前所选显示器在 Windows HDR 模式下系统实际使用的 SDR 纸白亮度（nit）。
        优先取 OS 报告的 DISPLAYCONFIG_SDR_WHITE_LEVEL（滑条设置），
        取不到时回退到 200 nit（旧行为）。
        """
        monitor = self.current_request.monitor_id if self.current_request else self.monitor_var.get()
        return self.backend.displays.paper_white_nits(monitor)

    @safe_call
    def calibrate_monitor(self):
        logging.info(_("Calibration started"))
        self.freeze_ui()
        request = self._capture_calibration_request()
        self.backend.begin_calibration(request)
        hdr_status = self.human_display_config_map[request.monitor_id]["color_work_status"]
        logging.info(_("Selected screen HDR state: {}").format(hdr_status))
        if hdr_status != "hdr":
            msg = _("The selected screen HDR is off. Please enable HDR in system settings before calibration.")
            tk.messagebox.showerror(_("Error"), msg)
            logging.error(msg)
            self.clean_color_rw_process()
            self.unfreeze_ui()
            return

        # 用系统实际报告的 SDR 纸白（滑条设置）锚定白点测试码值，
        # 使色度校准对准用户实际观看 SDR 的亮度。
        paper_white = self.get_paper_white_nit()
        paper_code = int(round(pq_oetf(paper_white) * 1023))
        self.gamut_test_rgb["white_paper"] = [paper_code] * 3
        logging.info(_("SDR paper white: {:.1f} nits, white test patch code: {}").format(
            paper_white, paper_code))

        # 灰阶曲线与颜色数据都选了历史数据时，本次校准不需要任何测量，
        # 因此不启动 dogegen / spotread，也不弹出「放置色度计」对话框。
        # （之前这里无条件构造 spotread，未接色度计时会直接报错失败。）
        need_meter = not self._history_only_calibration()
        if need_meter:
            args = self.get_spotread_args(request)
            self.backend.processes.start_measurement(args, ColorWriter, ColorReader)
            if self.proc_color_reader.status == "need_calibration":
                while 1:
                    msg = _("Spot read needs a calibration before continuing \nPlace the instrument on its reflective white reference then click OK.")
                    answer = tk.messagebox.askokcancel(_("need_calibration"), msg)
                    if answer:
                        self.proc_color_reader.calibrate()
                    else:
                        logging.info(_("User canceled calibration"))
                        self.clean_color_rw_process()
                        self.unfreeze_ui()
                        return
                    if self.proc_color_reader.status != "need_calibration":
                        break
            # Send command to the child process
            self.proc_color_write.write_rgb([800, 800, 800])
            msg = _("Move the white window to the target screen, resize it to fully cover the meter, place the meter on the window, then click OK.")
            answer = tk.messagebox.askokcancel(_("Place the colorimeter"), msg)
            if not answer:
                logging.info(_("User canceled calibration"))
                self.clean_color_rw_process()
                self.unfreeze_ui()
                return
        else:
            logging.info(_("Historical data is enough: no colorimeter needed for this calibration"))

        origin_preview_status = self.preview_var.get()
        self.icc_change_delay = 0.5

        # 历史颜色数据：整段的色域测量 + 色卡测量都可以跳过。
        # 复用的数据是原始实测 XYZ，进入的是与实时测量完全相同的后续链路，
        # 所以 profile / CLUT 的产出与重新测量一致（只是省掉了测量时间）。
        self.reused_color_run = None
        self.reused_color_card = None
        reuse_color = request.color_history_run
        if reuse_color is not None:
            logging.info(_("Using historical color data: {}").format(self._color_run_label(reuse_color)))
            logging.info(color_run_summary(reuse_color))

        def calibrate_control():
            self.backend.run_calibration_pipeline(
                self.measure_gamut_before,
                self.calibrate_pq,
                self.calibrate_chromaticity,
                self.measure_gamut_after,
            )
            

        def measure_control_cb(result):
            if isinstance(result, Exception):
                msg = _("Matrix LUT generation failed: {}").format(result)
                logging.error(msg)
                tk.messagebox.showerror(_("Error"), msg)
            else:
                logging.info(_("Matrix LUT generated"))
            self.unfreeze_ui()
            self.clean_color_rw_process()
            self.icc_change_delay = 0
            self.preview_var.set(origin_preview_status)
            # A fresh PQ measurement was just logged: make it selectable for
            # the next calibration (e.g. after changing the color temperature).
            self.refresh_gray_history()
            # A live colour measurement was just logged as well.
            self.refresh_color_history()
            if isinstance(result, Exception):
                raise result
            

        self.run_in_thread(calibrate_control, measure_control_cb)

    def measure_gamut_before(self):
        self.preview_var.set(True)
        for color, rgb in self.gamut_test_rgb.items():
            self.proc_color_write.write_rgb(rgb, delay=0.1)
            XYZ = self.proc_color_reader.read_XYZ()
            logging.info(_("Color {} measured XYZ: {}").format(color, XYZ))
            self.measure_gamut_xyz[color] = XYZ

        # Peak/black luminance rules live in one helper so a reused historical
        # colour run produces exactly the same MHC2 / lumi data as a live run.
        max_lumi, min_lumi = color_run_peak_min_xyz(
            self.measure_gamut_xyz,
            self.current_request.eetf_enabled,
            self.current_request.eetf_args,
        )

        start_lumi = self.measure_gamut_xyz["black"][1]
        delta = max(start_lumi * 0.01, 0.0005)  # Adjust threshold as needed
        logging.info(_("Start binary search for activated black: start_lumi={} delta={}").format(start_lumi, delta))

        def measure_gray(code):
            rgb = [code, code, code]
            self.proc_color_write.write_rgb(rgb, delay=0.1)
            XYZ = self.proc_color_reader.read_XYZ()
            logging.info(_("Gray test code={} RGB={} measured XYZ: {}").format(code, rgb, XYZ))
            return XYZ

        high_XYZ = measure_gray(255)
        if high_XYZ[1] <= start_lumi + delta:
            logging.info(_("No significant luminance increase found in 0-255 range; skipping activated black detection"))
            self.measure_gamut_xyz["min_activated_black"] = self.measure_gamut_xyz["black"]
        else:
            lo, hi = 1, 255
            found_code = None
            found_XYZ = None
            while lo <= hi:
                mid = (lo + hi) // 2
                XYZ = measure_gray(mid)
                if XYZ[1] > start_lumi + delta:
                    found_code = mid
                    found_XYZ = XYZ
                    hi = mid - 1  
                else:
                    lo = mid + 1
            if found_XYZ is not None:
                self.measure_gamut_xyz["min_activated_black"] = found_XYZ
                logging.info(_("Activated black level found: code={} XYZ={}").format(found_code, found_XYZ))
            else:
                logging.info(_("Activated black not found (grayscale differences may be below threshold)"))

        self._apply_gamut_data(self.measure_gamut_xyz, min_lumi, max_lumi)

    def _apply_gamut_data(self, gamut_xyz, min_lumi, max_lumi):
        """
        把一组色域测量结果（实测或复用历史数据）写进 profile。

        `measure_gamut_before` / `measure_gamut_after` 与历史颜色数据复用
        (`_apply_color_run`) 共用这一段：峰值/黑场亮度 → MHC2 + lumi 标签，
        实测原色 → rXYZ/gXYZ/bXYZ/wtpt，TRC → sRGB EOTF。

        `gamut_xyz` 为 {red,green,blue,white,white_paper,black,...} 的实测 XYZ；
        `min_lumi` / `max_lumi` 由调用方按 EETF 规则决定（见峰值亮度处理）。
        """
        self.backend.apply_gamut_data(gamut_xyz, min_lumi, max_lumi)
    
    def measure_gamut_after(self):
        self.preview_var.set(True)

        # 历史颜色数据复用：不做「校准后」重测。
        # 这一步只用实测原色重算与 measure_gamut_before 完全相同的 profile 标签
        # （rXYZ/gXYZ/bXYZ/wtpt、lumi、MHC2 峰值/黑场），而复用的原色本来就会写进
        # 同样的标签，所以结果与重测一致——但没有必要再测一遍（既省时间，
        # 也让「两个历史数据都选中时不接色度计」成为可能）。
        if getattr(self, "reused_color_run", None) is not None:
            logging.info(_("Historical color data reused: skipped the post-calibration gamut re-measurement"))
            max_lumi = self.MHC2.get("peak_luminance", 0.0)
            min_lumi = self.MHC2.get("min_luminance", 0.0)
            # 明亮模式：与重测路径一致地做亮度补偿
            if self.current_request.bright_mode:
                max_lumi, min_lumi = self._apply_bright_luminance()
            self._apply_gamut_data(self.measure_gamut_xyz, min_lumi, max_lumi)
            return

        for color, rgb in self.gamut_test_rgb.items():
            self.proc_color_write.write_rgb(rgb, delay=0.1)
            XYZ = self.proc_color_reader.read_XYZ()
            logging.info(_("Color {} measured XYZ: {}").format(color, XYZ))
            self.measure_gamut_xyz[color] = XYZ

        max_lumi = self.measure_gamut_xyz["white"][1]
        min_lumi = self.measure_gamut_xyz["black"][1]

        # 明亮模式的亮度补偿（与历史数据复用共用同一段，保证两条路径产出相同）
        if self.current_request.bright_mode:
            max_lumi, min_lumi = self._apply_bright_luminance()

        # 同一段写入逻辑（与 measure_gamut_before / 历史颜色数据复用共用）
        self._apply_gamut_data(self.measure_gamut_xyz, min_lumi, max_lumi)

    def _apply_bright_luminance(self):
        """
        明亮模式（`bright_var`）下的亮度补偿：返回 (max_lumi, min_lumi)。

        实测峰值/黑场先按反向明亮 LUT 折算，再写进 MHC2 与 lumi。
        这一步原本内联在 `measure_gamut_after` 里；抽出来是为了让
        「历史颜色数据复用」跳过复测时也能算出同样的结果（否则复用与重测
        在开启明亮模式时不一致）。
        """
        return self.backend.apply_bright_luminance()

    def calibrate_chromaticity(self):
        # measure and build matrix
        self.preview_var.set(True)
        logging.info(_("Start color measurement and generate matrix"))

        # 历史颜色数据复用：整段色卡测量都跳过，直接沿用选中 run 的
        # 目标/实测 XYZ（数据在 _apply_color_run 里已经装好）。
        if self.reused_color_run is not None:
            run = self.reused_color_run
            self.measured_xyz = [np.array(v, dtype=float) for v in self.measured_xyz]
            self.target_xyz = [np.array(v, dtype=float) for v in self.target_xyz]
            matrix = fit_XYZ2XYZ_wlock_dropY(
                self.measured_xyz, self.target_xyz,
                self.measured_xyz[-1], self.target_xyz[-1])
            logging.info(_("Chromaticity fit matrix (diagnostic only): {}").format(matrix.flatten().tolist()))
            self.MHC2["matrix"] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
            self.icc_handle.write_MHC2(self.MHC2)
            logging.info(_("Color matrix measurement finished, matrix: {}").format(self.MHC2["matrix"]))
            return

        paper_white = self.get_paper_white_nit()
        mode = self.current_request.color_sample_set
        if mode.startswith(COLOR_CARD_SRGB_24):
            self.target_xyz = get_srgb_24_calibrate_XYZ_suit(self.measure_gamut_xyz, paper_white)
        else:
            self.target_xyz = get_srgb_calibrate_XYZ_suit(self.measure_gamut_xyz, paper_white)
        if "+DisplayP3" in mode:
            self.target_xyz.extend(get_P3D65_calibrate_XYZ_suit(self.measure_gamut_xyz, paper_white))
        white_points = get_D65_white_calibrate_test_XYZ_suit(self.measure_gamut_xyz, paper_white)
        self.target_xyz.extend(white_points)
        logging.info(_("Color sample set: {}, {} samples").format(mode, len(self.target_xyz)))
        self.measured_xyz = []
        i = 1
        l = len(self.target_xyz)

        wp = [float(x.strip()) for x in self.current_request.white_point.split(",")]
        m = calculate_bradford_matrix(wp, D65_WHITE_POINT)
        # 记录本次色卡的适配白点：`color_history` 会读这一行，方便日后复用时
        # 判断历史数据是在哪个白点下测的（色卡本身不做白点适配，仅作诊断信息）。
        logging.info(_("Color measurement white point: {}").format(self.current_request.white_point))
        for itm in self.target_xyz:
            pq = XYZ_to_BT2020_PQ_rgb(itm)
            rgb = (pq * 1023).round().astype(int)
            self.proc_color_write.write_rgb(rgb, delay=0.1)
            XYZ = self.proc_color_reader.read_XYZ()
            XYZ = [float(itm) / 10000 for itm in XYZ]
            XYZ = m@XYZ
            logging.info(_("({}) Color: {} Target XYZ:{} Measured: {}").format(i/l, rgb , itm, XYZ))
            self.measured_xyz.append(XYZ)
            i += 1
        matrix = fit_XYZ2XYZ_wlock_dropY(self.measured_xyz, self.target_xyz,self.measured_xyz[-1], self.target_xyz[-1])
        # matrix = fit_XYZ2XYZ(self.measure_convert_xyz, self.convert_xyz)
        # 仅用于诊断：记录拟合矩阵（不写入 profile）
        logging.info(_("Chromaticity fit matrix (diagnostic only): {}").format(matrix.flatten().tolist()))
        # 关键修复（2026-09-01）：不要把上面的拟合矩阵写入 profile 的 MHC2。
        # 实测验证（校色报告 vs 矩阵计算）：Windows 的 SDR→HDR 转换会把 MHC2 矩阵
        # 直接乘在内容 XYZ 上（测得颜色 == MHC2矩阵 @ 内容XYZ）。这里拟合出的矩阵
        # 是 measured→target 的“校正”，若写入 profile 会被 Windows 当作对内容的
        # 变换再套一层，导致宽色域显示器上黄/绿/橙严重欠饱和（b* 崩塌 20~60）。
        #
        # 默认行为（发布版）：MHC2 矩阵保持单位阵（v3 行为），色域映射交给实测
        # 原色标签 (rXYZ/gXYZ/bXYZ/wtpt) 与 1D LUT 完成——这是最通用的选择。
        self.MHC2["matrix"] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        self.icc_handle.write_MHC2(self.MHC2)
        logging.info(_("Color matrix measurement finished, matrix: {}").format(self.MHC2["matrix"]))

        # ------------------------------------------------------------------
        # 实验性暖色修正矩阵（已注释，默认不启用）
        # ------------------------------------------------------------------
        # 历史：纯单位阵会让暖色残余（偏亮、偏黄）无法修正。v4 实验证明用 1D LUT
        # 做全局缩放会拖累灰阶/蓝色。v5/v6 实验（灰阶不变暖矩阵）修暖色有代价：
        #   v5 纯红顶点被推向原生深红（品红观感）、蓝色系被推向青色（2G 9.4 dE）；
        #   v6 改为“红顶点+灰阶不变”后暖色没修好（1G 仍 4.2、3F 4.2）且绿/蓝损伤
        #   依旧（avg 1.71→2.30、2G 9.35），v6 已被否。
        # v7（作者 FFALCON R27U81 标定，2026-09-02）：保持绿色、蓝色顶点不变，
        # 把红色顶点沿 x+y=const（斜率 -1）方向上移。数学：
        #   约束 W·绿=绿、W·蓝=蓝 ⇒ W = I + u⊗n，n = 绿×蓝（sRGB 内容原色）。
        #   此时红点移动 δ = u·(n·红)，且白点移动同一 δ（白=红+绿+蓝，
        #   线性代数必然结果）——灰阶会有与红点同量的偏移，下游 1D LUT 无法
        #   补偿（LUT 逐通道、在矩阵之后）。这是本设计的固有代价，幅度由 s 控制。
        # 参数 s：内容空间红点扰动幅度（相对 |红| 的比例）。s=0 → 单位阵（v3 行为）。
        #   s≈0.02：红顶点估计移动 Δxy≈(-0.008,+0.011)，白点 Δxy≈(-0.0016,+0.0024)
        #   （灰阶 dE 约 +1~2）；实际渲染响应需重新校准 + 报告验证（参考 v5 教训：
        #   内容空间的移动与报告实测并非严格 1:1）。
        # v7 实测（archive\2026-09-01_v7-verified\）：avg 1.16 / max 5.52，
        #   红顶点 (0.669,0.281)→(0.6323,0.3183)，绿/蓝不动，灰阶 ≤1.3 dE。
        # 说明：该矩阵针对作者显示器标定（avg 1.16 / max 5.52），对其他显示器
        # 未必适用，故发布版默认关闭。如需在你的显示器上尝试，取消下方注释并按
        # 需调整 s 后重新校准（完整推导见 PATCHES.md 归档 / CHANGELOG）。
        # sRGB_RED_XYZ   = np.array([0.4124, 0.2126, 0.0193])
        # sRGB_GREEN_XYZ = np.array([0.3576, 0.7152, 0.1192])
        # sRGB_BLUE_XYZ  = np.array([0.1805, 0.0722, 0.9505])
        # n_vec = np.cross(sRGB_GREEN_XYZ, sRGB_BLUE_XYZ)      # (0.6712, -0.3184, -0.1033)
        # n_dot_r = float(np.dot(n_vec, sRGB_RED_XYZ))         # 0.2071
        # # u 的方向：给红点内容“减 X、加 Y、微减 Z”（往绿混、离开品红），
        # # 即把渲染出的红顶点向上（y 增大）推；斜率由 delta_dir 的 Y/X 比决定
        # # （Y/X=2.0 → 渲染斜率 ≈ -1.0，已验证），幅度由 s 控制。
        # s = 0.02                                             # 红点扰动幅度（0=关闭，即 v3）
        # delta_dir = np.array([-0.5, 1.0, -0.2])              # 斜率 1.0（已验证 v7）
        # delta_dir = delta_dir / np.linalg.norm(delta_dir)
        # delta_xyz = s * np.linalg.norm(sRGB_RED_XYZ) * delta_dir
        # u_vec = delta_xyz / n_dot_r
        # warm_matrix = np.eye(3) + np.outer(u_vec, n_vec)
        # self.MHC2["matrix"] = warm_matrix.flatten().tolist()
        # self.icc_handle.write_MHC2(self.MHC2)
        # logging.info(_("v7 red-move matrix: s={}, red content delta={}, white content delta={} (same delta, see comment)").format(
        #     s, delta_xyz.tolist(), delta_xyz.tolist()))
    
    def calibrate_white_by_lut(self):
        logging.info(_("Start calibrating grayscale chromaticity to D65"))
        MEASURE_POINTS_COUNT = 32
        pq_lut_origin = {"red": copy.deepcopy(self.MHC2["red_lut"]),
                         "green": copy.deepcopy(self.MHC2["green_lut"]),
                         "blue": copy.deepcopy(self.MHC2["blue_lut"])}
        max_nit = 0
        self.proc_color_write.write_rgb([1023,1023,1023], delay=0.3)
        XYZ = self.proc_color_reader.read_XYZ()
        max_nit = XYZ[1]
        min_nit = 10
        logging.info(_("Measured display peak luminance: {} nit").format(max_nit))
        measure_points_t = np.linspace(0, 1023, MEASURE_POINTS_COUNT, dtype=int)
        measure_points = [i for i in measure_points_t if min_nit < pq_eotf(i / 1023) < max_nit*0.8]
        
        # measure_points = [719]
        
        measure_points.insert(0, 0)
        measure_points.append(1023)
        logging.info(_("Grayscale points measured this run ({}): {}".format(len(measure_points), measure_points)))
        scales = []
        for grayscale in measure_points:
            nit = pq_eotf(grayscale / 1023)
            if nit > max_nit:
                logging.info(_("Grayscale {} target {} nit exceeds 90% of display peak {}, skip").format(
                    grayscale, nit, max_nit*0.9))
                scales.append({"grayscale":grayscale, "red": None, "green": None, "blue": None})
                continue
            if grayscale == 0:
                logging.info(_("Grayscale {} target {} nit is 0, skip").format(grayscale, nit))
                scales.append({"grayscale":grayscale, "red": None, "green": None, "blue": None})
                continue
            self.MHC2["red_lut"]     = copy.deepcopy(pq_lut_origin["red"])
            self.MHC2["green_lut"]   = copy.deepcopy(pq_lut_origin["green"])
            self.MHC2["blue_lut"]    = copy.deepcopy(pq_lut_origin["blue"])
            self.MHC2["entry_count"] = len(pq_lut_origin["red"])
            self.icc_handle.write_MHC2(self.MHC2)
            rgb_pq = [int(grayscale)] * 3
            target_rgb_pq = np.array(rgb_pq)/1023
            channel_scale = {"grayscale": grayscale, "red": 1, "green": 1, "blue": 1}
            for loop_count in range(2):
                for idx, channel in enumerate(["red","green", "blue"]):
                    logging.info(_("Adjust {} channel").format(channel))
                    total_scale = channel_scale[channel]
                    current_scale = 1
                    step = 0.00390625
                    target = target_rgb_pq[idx]
                    done = False
                    last_ratio = None
                    while 1:
                        self.preview_var.set(True)
                        logging.info(_("Grayscale {} loop {} channel {} target: PQ->{} RGB->{}").format(
                            grayscale, loop_count, channel, target_rgb_pq, rgb_pq))
                        self.proc_color_write.write_rgb(rgb_pq, delay=0.1)
                        measure_xyz = np.array(self.proc_color_reader.read_XYZ())
                        measure_rgb_pq = XYZ_to_BT2020_PQ_rgb(measure_xyz/10000)
                        measure = measure_rgb_pq[idx]
                        ratio = measure / target
                        total_ratio = (measure_rgb_pq / target_rgb_pq).round(4).tolist()
                        if last_ratio is None:
                            last_ratio = ratio
                        logging.info(_("Grayscale {} loop {} channel {} measured: XYZ->{} PQ->{} "
                                       "ratio->{} total_ratio->{} scale->{}").format(
                            grayscale, loop_count, channel,
                            measure_xyz.round(4).tolist(),
                            measure_rgb_pq,
                            round(ratio, 4),
                            total_ratio,
                            round(current_scale, 4)))

                        if ratio > 1:
                            if current_scale > 1:
                                # 加超了
                                last = abs(last_ratio - 1)
                                current = abs(ratio - 1)
                                if current >= last:
                                    current_scale -= step
                                done = True
                            else:
                                current_scale -= step
                        elif ratio < 1:
                            if current_scale < 1:
                                # 减超了
                                last = abs(last_ratio - 1)
                                current = abs(ratio - 1)
                                if current >= last:
                                    current_scale += step
                                done = True
                            else:
                                current_scale += step
                        else:
                            done = True
                        scale = total_scale + current_scale - 1
                        pq_lut_scale = lut_scale(pq_lut_origin[channel], scale)
                        pq_lut_scale[0] = 0
                        self.MHC2[f"{channel}_lut"] = pq_lut_scale.tolist()
                        self.icc_handle.write_MHC2(self.MHC2)
                        last_ratio = ratio
                        if done:
                            channel_scale[channel] = scale
                            logging.info(_("Grayscale {} loop {} channel {} adjusted, scale {}").format(
                                grayscale, loop_count, channel, scale))
                            break
                logging.info(_("Grayscale {} loop {} calibration done, red {} green {} blue {}").format(
                    grayscale, loop_count,
                    channel_scale["red"], channel_scale["green"], channel_scale["blue"]))
            scales.append(channel_scale)
            self.proc_color_write.write_rgb(rgb_pq, delay=0.3)
            measure_xyz = np.array(self.proc_color_reader.read_XYZ())
            measure_rgb_pq = XYZ_to_bt2020_linear(measure_xyz/10000)
            logging.info(_("Grayscale {} post-calibration: CIEXYZ->{} Linear RGB:{}->{}").format(
                grayscale, measure_xyz, target_rgb_pq, measure_rgb_pq))
        logging.info(_("All grayscale calibration finished, scales: {}").format(scales))
        last_activated_scale = None
        target_pq_lut_red = copy.deepcopy(pq_lut_origin["red"])
        target_pq_lut_green = copy.deepcopy(pq_lut_origin["green"])
        target_pq_lut_blue = copy.deepcopy(pq_lut_origin["blue"])
        if scales[0]["grayscale"] == 0:
            scales[0] = copy.deepcopy(scales[1])
            scales[0]["grayscale"] = 0
        lrscale = None
        lgscale = None
        lbscale = None
        lgrayscale = None
        for scale in scales:
            cgrayscale = scale["grayscale"]
            if scale["red"] is None:
                crscale = last_activated_scale["red"]
                cgscale = last_activated_scale["green"]
                cbscale = last_activated_scale["blue"]
            else:
                last_activated_scale = scale
                crscale = scale["red"]
                cgscale = scale["green"]
                cbscale = scale["blue"]
            if lgrayscale is None:
                lrscale = crscale
                lgscale = cgscale
                lbscale = cbscale
                lgrayscale = 0
            logging.info(_("Grayscale {} RED interpolation range {}-{} scale {}-{}").format(
                cgrayscale, lgrayscale, cgrayscale, lrscale, crscale))
            logging.info(_("Grayscale {} GREEN interpolation range {}-{} scale {}-{}").format(
                cgrayscale, lgrayscale, cgrayscale, lgscale, cgscale))
            logging.info(_("Grayscale {} BLUE interpolation range {}-{} scale {}-{}").format(
                cgrayscale, lgrayscale, cgrayscale, lbscale, cbscale))
            for idx in range(lgrayscale, cgrayscale):
                
                num = cgrayscale - lgrayscale
                rstep = (crscale - lrscale) / num if num != 0 else 0
                gstep = (cgscale - lgscale) / num if num != 0 else 0
                bstep = (cbscale - lbscale) / num if num != 0 else 0
                r = idx - lgrayscale
                target_pq_lut_red[idx] = lut_scale(target_pq_lut_red[idx], lrscale + r*rstep) 
                target_pq_lut_green[idx] = lut_scale(target_pq_lut_green[idx], lgscale + r*gstep)
                target_pq_lut_blue[idx] = lut_scale(target_pq_lut_blue[idx], lbscale + r*bstep)

            target_pq_lut_red[cgrayscale] = lut_scale(target_pq_lut_red[cgrayscale], crscale)
            target_pq_lut_green[cgrayscale] = lut_scale(target_pq_lut_green[cgrayscale], cgscale)
            target_pq_lut_blue[cgrayscale] = lut_scale(target_pq_lut_blue[cgrayscale], cbscale)

            lrscale = crscale
            lgscale = cgscale
            lbscale = cbscale
            lgrayscale = cgrayscale
        self.MHC2["red_lut"]     = target_pq_lut_red
        self.MHC2["green_lut"]   = target_pq_lut_green
        self.MHC2["blue_lut"]    = target_pq_lut_blue
        self.MHC2["entry_count"] = len(target_pq_lut_red)
        self.icc_handle.write_MHC2(self.MHC2)

        return
    
    def calibrate_pq(self, eetf=False):
        self.preview_var.set(True)
        logging.info(_("Start calibrating PQ grayscale curve"))
        self.measured_pq["red"] = []
        self.measured_pq["green"] = []
        self.measured_pq["blue"] = []
        wp = [float(x.strip()) for x in self.current_request.white_point.split(",")]
        m = calculate_bradford_matrix(wp, D65_WHITE_POINT)
        num = self.current_request.grayscale_samples

        history_run = self.current_request.gray_history_run
        if history_run is not None:
            # Reuse the raw XYZ logged by a previous PQ measurement on this
            # monitor, re-applying the CURRENT white point's Bradford matrix.
            # The monitor's native response does not depend on the target
            # color temperature, so no re-measurement is needed.
            num = history_run["num"]
            logging.info(_("Using historical gray data: {} points from {}").format(
                num, history_run["end"].strftime("%m-%d %H:%M:%S")))
            for code, xyz in history_run["points"]:
                XYZ_converted = m @ xyz
                rgb_measured = XYZ_to_BT2020_PQ_rgb(XYZ_converted/10000)
                self.measured_pq["red"].append(float(rgb_measured[0]))
                self.measured_pq["green"].append(float(rgb_measured[1]))
                self.measured_pq["blue"].append(float(rgb_measured[2]))
        else:
            for idx, grayscale in enumerate(np.linspace(0, 1023, num, endpoint=True).round().astype(np.int32)):
                grayscale = int(grayscale)
                rgb = [grayscale, grayscale, grayscale]
                self.proc_color_write.write_rgb(rgb, delay=0.03)
                XYZ = self.proc_color_reader.read_XYZ()
                XYZ_converted = m@XYZ
                rgb_measured = XYZ_to_BT2020_PQ_rgb(XYZ_converted/10000)
                logging.info(_("({}/{}) Output RGB: {} Measured XYZ: {} RGB: {}").format(idx+1, num, rgb, XYZ, rgb_measured*1023))
                self.measured_pq["red"].append(float(rgb_measured[0]))
                self.measured_pq["green"].append(float(rgb_measured[1]))
                self.measured_pq["blue"].append(float(rgb_measured[2]))

        eetf_args = None
        if eetf:
            eetf_args = copy.deepcopy(self.eetf_args)
            if self.eetf_args["monitor_max"] is None:
                eetf_args["monitor_max"] = self.measure_gamut_xyz["white"][1]
            if self.eetf_args["monitor_min"] is None:
                eetf_args["monitor_min"] = self.measure_gamut_xyz["min_activated_black"][1]
        target_pq = self.MHC2
        if self.current_request.bright_mode:
            target_pq = {"red_lut": generate_bright_pq_lut().tolist(),
                         "green_lut": generate_bright_pq_lut().tolist(),
                         "blue_lut": generate_bright_pq_lut().tolist()}
        
        red_lut = generate_mhc2_lut_from_measured_pq(
            self.measured_pq["red"], target_pq=target_pq["red_lut"])
        blue_lut = generate_mhc2_lut_from_measured_pq(
            self.measured_pq["blue"], target_pq=target_pq["blue_lut"])
        green_lut = generate_mhc2_lut_from_measured_pq(
            self.measured_pq["green"], target_pq=target_pq["green_lut"])

        # 注：曾尝试 SDR 区间 LUT 每通道缩放（R0.98/G0.95/B0.99）修正暖色，
        # v4 报告显示该方案以全局灰阶/蓝色退化为代价，avg 1.71->2.01，已撤销。
        # 暖色残余改用 calibrate_chromaticity 中的灰阶不变矩阵修正（见 PATCHES.md）。
        
        
            
            # red_lut = generate_mhc2_lut_from_measured_pq(
            #     red_lut, target_pq=target_pq["red_lut"])
            # blue_lut = generate_mhc2_lut_from_measured_pq(
            #     blue_lut, target_pq=target_pq["blue_lut"])
            # green_lut = generate_mhc2_lut_from_measured_pq(
            #     green_lut, target_pq=target_pq["green_lut"])

        self.MHC2["red_lut"] = red_lut.tolist()
        self.MHC2["green_lut"] = green_lut.tolist()
        self.MHC2["blue_lut"] = blue_lut.tolist()
        self.MHC2["entry_count"] = len(red_lut)
        self.icc_handle.write_MHC2(self.MHC2)
        logging.info(_("PQ LUT measurement finished"))

    
    @safe_call
    def measure_pq(self):
        # 「测量色准」必须有色度计：历史数据复用能让「校准」在没有仪器时跑通，
        # 但色准测量本身是实时测量，缺仪器时给出明确提示而不是抛异常。
        # `instrument_choose` 只在 spotread --help 真的列出仪器时才有内容。
        if not getattr(self, "instrument_choose", None):
            msg = _("No colorimeter detected. Connect a colorimeter to measure color accuracy (calibration itself can run on historical data alone).")
            logging.error(msg)
            tk.messagebox.showerror(_("No instrument found"), msg)
            return
        args = self.get_spotread_args()
        self.backend.processes.start_measurement(args, ColorWriter, ColorReader)
        if self.proc_color_reader.status == "need_calibration":
            while 1:
                msg = _("Spot read needs a calibration before continuing \nPlace the instrument on its reflective white reference then click OK.")
                answer = tk.messagebox.askokcancel(_("need_calibration"), msg)
                if answer:
                    self.proc_color_reader.calibrate()
                else:
                    logging.info(_("User canceled calibration"))
                    self.clean_color_rw_process()
                    return
                if self.proc_color_reader.status != "need_calibration":
                    break
        self.proc_color_write.write_rgb([800, 800, 800])
        answer = tk.messagebox.askokcancel(
            _("Notice"),
            _(
                "If you want to measure calibrated but unsaved/unloaded data, please enable Preview first.\n\n"
                "Resize and position the white window, place the colorimeter on it, then click OK"
            ),
        )
        if not answer:
            self.clean_color_rw_process()
            logging.info(_("User canceled measurement"))
            return
        self.freeze_ui()
        logging.info(_("Start measuring PQ response"))
        measurement_mode = self.color_space_var.get()
        def m():
            target_white_xyz = []
            target_pq = []
            measured_pq = []
            measured_white_xyz = []
            num = 256

            for idx, grayscale in enumerate(np.linspace(0, 1023, num, endpoint=True).round().astype(np.int32)):
                grayscale = int(grayscale)
                pq = grayscale / 1023
                target_white_xyz.append(BT2020_PQ_rgb_to_XYZ([pq, pq, pq]))
                target_pq.append(pq)
                rgb = [grayscale, grayscale , grayscale]
                self.proc_color_write.write_rgb(rgb, delay=0.1)
                XYZ = np.array(self.proc_color_reader.read_XYZ())
                logging.info(_("({}/{}) Measure RGB: {} Result: {}").format(idx+1, num, rgb, XYZ))
                measured_white_xyz.append([itm/10000 for itm in XYZ])
                nit = float(XYZ[1])
                measured_pq.append(float(pq_oetf(nit)))
            
            max_nit = max([itm[1] for itm in measured_white_xyz])
            color_gamut = {"red": xyY_to_XYZ([*BT2020_xy["red"], max_nit*10000]),
                           "green": xyY_to_XYZ([*BT2020_xy["green"], max_nit*10000]),
                           "blue": xyY_to_XYZ([*BT2020_xy["blue"], max_nit*10000]),
                           "white": xyY_to_XYZ([*BT2020_xy["white"], max_nit*10000])}
            mode = measurement_mode
            if mode.startswith(COLOR_CARD_SRGB_24):
                target_colored_xyz = get_srgb_24_measure_XYZ_suit(color_gamut)
            else:
                target_colored_xyz = get_srgb_measure_XYZ_suit(color_gamut)
            if "+DisplayP3" in mode:
                target_colored_xyz.extend(get_P3D65_measure_XYZ_suit(color_gamut))
            measured_colored_xyz = []
            num = len(target_colored_xyz)
            logging.info(_("Start measuring color points"))
            for idx, xyz in enumerate(target_colored_xyz):
                rgb = (XYZ_to_BT2020_PQ_rgb(xyz) * 1023).round().astype(int).tolist()
                self.proc_color_write.write_rgb(rgb, delay=0.1)
                XYZ = np.array(self.proc_color_reader.read_XYZ())
                logging.info(_("({}/{}) Measure RGB: {} Target XYZ:{} Result: {}").format(
                    idx+1, num, rgb, xyz, XYZ/10000))
                measured_colored_xyz.append([itm/10000 for itm in XYZ])
            
            logging.info(_("Measurement finished: {}").format(len(measured_colored_xyz)))

            return {
                "target_xyz": np.array(target_white_xyz),
                "measured_xyz": np.array(measured_white_xyz),
                "target_pq": np.array(target_pq),
                "measured_pq": np.array(measured_pq),
                "target_colored_xyz": np.array(target_colored_xyz),
                "measured_colored_xyz": np.array(measured_colored_xyz),
            }
        
        def cb(result):
            self.unfreeze_ui()
            self.clean_color_rw_process()
            if isinstance(result, Exception):
                msg = _("Measuring PQ response failed: {}").format(result)
                logging.error(msg)
                tk.messagebox.showerror(_("Error"), msg)
                raise result
            else:
                logging.info(_("Measuring PQ response finished"))
                try:
                    self._show_pq_plot(result["target_pq"], result["measured_pq"])
                except Exception as e:
                    logging.error(_("Failed to plot PQ curve: {}").format(e))
            min_care_nit = max(1/10000, result["measured_xyz"][0][1] * 1.1)
            max_care_nit = result["measured_xyz"][-1][1] * 0.9
            logging.info(_("Measured min luminance: {} nit, max luminance: {} nit").format(
                min_care_nit*10000, max_care_nit*10000))
            white_de_result = []
            logging.info(_("Start computing grayscale deltaE_ITP"))
            for idx in range(len(result["measured_xyz"])):
                if min_care_nit < result["measured_xyz"][idx][1] < max_care_nit:
                    t = result["target_xyz"][idx]
                    m = result["measured_xyz"][idx]
                    de = XYZdeltaE_ITP(t, m)
                    white_de_result.append([t, m, de])
                    logging.info(_("Target: {} Measured: {} dE_ITP: {}").format(
                        t, m, de.round(2)))
            colored_de_result = []
            logging.info(_("Start computing color deltaE_ITP"))
            for idx in range(len(result["measured_colored_xyz"])):
                t = result["target_colored_xyz"][idx]
                m = result["measured_colored_xyz"][idx]
                de = XYZdeltaE_ITP(t, m)
                colored_de_result.append([t, m, de])
                logging.info(_("Target: {} Measured: {} dE_ITP: {}").format(
                    t, m, de.round(2)))
            white_de_avg = np.mean([itm[2] for itm in white_de_result]).round(2)
            white_de_max = np.max([itm[2] for itm in white_de_result]).round(2)
            colored_de_avg = np.mean([itm[2] for itm in colored_de_result]).round(2)
            colored_de_max = np.max([itm[2] for itm in colored_de_result]).round(2)
            logging.info(_("Within luminance range ({}-{}), grayscale average deltaE_ITP: {}, max deltaE_ITP: {}").format(
                round(min_care_nit*10000,2), round(max_care_nit*10000,2),
                white_de_avg, white_de_max))
            logging.info(_("At 200 nit D65 white, color average deltaE_ITP: {}, max deltaE_ITP: {}").format(
                colored_de_avg, colored_de_max))
        self.run_in_thread(m, cb)
    
    def _show_pq_plot(self, target_pq, measured_pq):
        """
        绘制 PQ 曲线（自适应窗口大小）:
        - x 轴: 索引/(N-1)*100 (%), 0..100
        - y 轴: PQ*100 (%), 0..100
        在同一图中绘制 target_pq 与 measured_pq
        """
        tp = np.asarray(target_pq, dtype=float).flatten()
        mp = np.asarray(measured_pq, dtype=float).flatten()
        if tp.size != mp.size or tp.size < 2:
            tk.messagebox.showwarning(_("Warning"), _("Not enough data to plot"))
            return
        n = tp.size

        win = tk.Toplevel(self.root)
        win.title(_("PQ measurement curve"))
        w, h = 760, 460
        win.geometry(f"{w}x{h}")
        frm = tk.Frame(win, bg="white")
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        canvas = Canvas(frm, bg="white", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        def draw():
            canvas.delete("all")
            # 当前画布大小
            cw = max(200, canvas.winfo_width())
            ch = max(160, canvas.winfo_height())
            margin_l = 60
            margin_r = 20
            margin_t = 20
            margin_b = 50
            x0, y0 = margin_l, ch - margin_b
            x1, y1 = cw - margin_r, margin_t
            plot_w = max(1, x1 - x0)
            plot_h = max(1, y0 - y1)

            # 轴线
            canvas.create_line(x0, y0, x1, y0, fill="#444")  # x 轴
            canvas.create_line(x0, y0, x0, y1, fill="#444")  # y 轴

            # 刻度与标签（0..100 每 20）
            for p in range(0, 101, 20):
                # x 轴刻度
                xx = x0 + plot_w * (p / 100.0)
                canvas.create_line(xx, y0, xx, y0 + 5, fill="#444")
                canvas.create_text(xx, y0 + 18, text=str(p), fill="#444", font=("Segoe UI", 10))
                # y 轴刻度
                yy = y0 - plot_h * (p / 100.0)
                canvas.create_line(x0 - 5, yy, x0, yy, fill="#444")
                canvas.create_text(x0 - 28, yy, text=str(p), fill="#444", font=("Segoe UI", 10))

            # 标签
            canvas.create_text((x0 + x1) // 2, y1 - 6, text="PQ (%)", fill="#333", font=("Microsoft YaHei", 11))
            canvas.create_text((x0 + x1) // 2, y0 + 35, text=_("Position (%)"), fill="#333", font=("Microsoft YaHei", 11))

            def to_points(arr):
                pts = []
                for i, v in enumerate(arr):
                    fx = i / (n - 1)                  # 0..1
                    xx = x0 + fx * plot_w
                    fy = np.clip(float(v), 0.0, 1.0)  # v in [0,1]
                    yy = y0 - (fy * plot_h)
                    pts.append((xx, yy))
                return pts

            pts_t = to_points(tp)
            pts_m = to_points(mp)

            def draw_poly(points, color, width=2):
                flat = []
                for (px, py) in points:
                    flat.extend([px, py])
                if len(flat) >= 4:
                    canvas.create_line(*flat, fill=color, width=width, smooth=True)

            draw_poly(pts_t, "#1f77b4", 2)  # target: 蓝
            draw_poly(pts_m, "#d62728", 2)  # measured: 红

            # 图例
            box_w = 160
            box_h = 44
            canvas.create_rectangle(x1 - box_w, y1 + 8, x1 - 20, y1 + 8 + box_h, outline="#ccc", fill="#fff")
            canvas.create_line(x1 - box_w + 10, y1 + 20, x1 - box_w + 40, y1 + 20, fill="#1f77b4", width=2)
            canvas.create_text(x1 - box_w + 50, y1 + 20, text="target_pq", anchor="w", fill="#333", font=("Segoe UI", 10))
            canvas.create_line(x1 - box_w + 10, y1 + 36, x1 - box_w + 40, y1 + 36, fill="#d62728", width=2)
            canvas.create_text(x1 - box_w + 50, y1 + 36, text="measured_pq", anchor="w", fill="#333", font=("Segoe UI", 10))

        # 绑定自适应重绘
        canvas.bind("<Configure>", lambda e: draw())
        # 初次绘制
        win.after(10, draw)
    
    @safe_call
    def measure_color_accuracy(self):
        with open("data\\verify_video_extended_smpte2084_1000_p3_2020.ti1", "r") as f:
            lines = f.readlines()
        data_section = False
        rgb_list = []
        xyz_list = []
        for line in lines:
            line = line.strip()
            if line == "BEGIN_DATA":
                data_section = True
                continue
            elif line == "END_DATA":
                break
            if data_section:
                parts = line.split()
                t = list(map(float, parts))
                xyz = [itm * 6 / 10000 for itm in t[4:7]]
                xyz_list.append(xyz)
                rgb_list.append(
                    [
                        round(float(itm) * 1023)
                        for itm in XYZ_to_BT2020_PQ_rgb(xyz).tolist()
                    ]
                )

        args = self.get_spotread_args()
        self.backend.processes.start_measurement(args, ColorWriter, ColorReader)
        if self.proc_color_reader.status == "need_calibration":
            while 1:
                msg = _("Spot read needs a calibration before continuing \nPlace the instrument on its reflective white reference then click OK.")
                answer = tk.messagebox.askokcancel(_("need_calibration"), msg)
                if answer:
                    self.proc_color_reader.calibrate()
                else:
                    logging.info(_("User canceled calibration"))
                    self.clean_color_rw_process()
                    return
                if self.proc_color_reader.status != "need_calibration":
                    break

        self.proc_color_write.write_rgb([800, 800, 800])
        answer = tk.messagebox.askokcancel(
            _("Notice"),
            _(
                "If you want to measure calibrated but unsaved/unloaded data, please enable Preview first.\n\n"
                "Resize and position the white window, place the colorimeter on it, then click OK"
            ),
        )
        if not answer:
            self.clean_color_rw_process()
            logging.info(_("User canceled measurement"))
            return
        def cb(result):
            pass
        def m():
            real_xyz = []
            logging.info(_("Measured RGB list: {}").format(rgb_list))
            l = len(rgb_list)
            for i, rgb in enumerate(rgb_list):
                self.proc_color_write.write_rgb(rgb, delay=0.1)
                XYZ = self.proc_color_reader.read_XYZ()
                logging.info(_("({}/{}) Measure RGB: {} Target XYZ:{} Result: {}").format(
                    i+1, l, rgb, xyz_list[i], XYZ))
                real_xyz.append([float(itm) / 10000 for itm in XYZ])

            self.clean_color_rw_process()
            de_list = []
            for idx in range(len(real_xyz)):
                de = XYZdeltaE_ITP(real_xyz[idx], xyz_list[idx])
                de_list.append(de)
                logging.info(_("Target {}: {}").format(xyz_list[idx], de))
            logging.info(_("Measured XYZ list: {}").format(xyz_list))
            logging.info(_("Measured actual XYZ values: {}").format(real_xyz))
            logging.info(_("Measured color differences: {}").format(de_list))
            logging.info(_("Average color difference: {}, maximum difference: {}").format(
                sum(de_list) / len(de_list), max(de_list)))

        self.run_in_thread(m, cb)

    def _measured_color_samples(self):
        """
        取本次校准可用的实测色卡样本，供 CLUT 正向模型校验。

        数据来源：实时测量（`calibrate_chromaticity` 写入的 `measured_xyz`）或
        历史颜色数据（`_apply_color_run` 写入的 `reused_color_card`）。
        返回 [(code3_pq_0..1, xyz3_abs_nits), ...]，没有数据时返回 None。
        """
        return self.backend.measured_color_samples()

    def _make_display_model(self):
        """
        用当前校准结果构造 DisplayModel（CLUT 生成的正向模型）。

        只依赖校准过程中已经得到的实测数据，不需要额外测量：
          · rXYZ/gXYZ/bXYZ/wtpt 实测原色与白点（可为历史颜色数据）
          · MHC2 每通道 1D LUT（显示器实际响应）
          · 峰值 / 黑场亮度
          · 可选：实测色卡样本，用于校验正向模型（`color_accuracy_report`）
        """
        return self.backend.make_display_model()

    def _model_signature(self):
        """模型指纹：任一输入变化都会让它变，用于缓存 CLUT 生成结果。"""
        return self.backend.model_signature()

    def _make_clut_tags(self):
        """
        生成（或从缓存取出）CLUT 标签块。

        这是**纯计算**、只读自我当前状态的一步，也是保存/预览里最慢的一步
        （B2A0 反向求解，33³ 约 30 s，17³ 约 3 s），因此它被单独拆出来，
        好让调用方把这一步放到工作线程里跑，避免界面「卡死」。

        返回 (tags, model)；未启用 CLUT 时返回 (None, None)。
        """
        grid = int(self.clut_grid_var.get() or CLUT_GRID_DEFAULT)
        return self.backend.make_clut_tags(grid)

    def _report_clut(self, tags, model):
        """把 CLUT 结果与色卡校验写进日志（保留原提示文本）。"""
        return self.backend.report_clut(tags, model)

    def write_clut_if_enabled(self):
        """
        按用户选择把 CLUT（A2B0/B2A0）标签写入内存中的 profile。

        返回 (写入的标签列表, 提示文本)；未启用时返回 (None, None)。
        CLUT 不改变矩阵/MHC2 标签，只在同一个 ICC 文件里补上标准多维表。

        注意：这一版是**同步**的（生成 + 写标签都在当前线程）。界面上的「保存」
        走的是 `generate_and_save_icc`，它把耗时的那一步放到工作线程；这里保留
        同步版本给测试与内部调用。
        """
        enabled = bool(getattr(self, "clut_var", None) and self.clut_var.get())
        grid = int(self.clut_grid_var.get() or CLUT_GRID_DEFAULT)
        return self.backend.write_clut(enabled, grid)

    def generate_and_save_icc(self):
        """
        保存 ICC。

        CLUT 的反向求解很慢：维护者机器上实测 17³ ≈ 5 s、25³ ≈ 17 s、33³ ≈ 37 s，
        45³/65³ 是分钟级。原来它在主线程里同步跑，界面在这段时间完全没有响应，
        看起来就是「保存卡死」（Windows 也可能报「未响应」）。
        现在只把「生成 CLUT 标签」这一步放到工作线程，主线程负责写 profile 与安装
        ICC；期间用 `freeze_ui()` 禁用交互（并显示等待光标）。
        不启用 CLUT 时没有慢步骤，保持原来的同步路径。
        """
        path = filedialog.asksaveasfilename(
            initialdir=os.path.expanduser("~/Documents"),
            title=_("Save ICC file"),
            defaultextension=".icc",
            filetypes=[(_("ICC file"), "*.icc"), (_("All files"), "*.*")],
        )
        if not path:
            return
        filename = os.path.basename(path)
        name_without_ext = os.path.splitext(filename)[0]
        desc = name_without_ext

        clut_on = bool(getattr(self, "clut_var", None) and self.clut_var.get())
        grid = int(self.clut_grid_var.get() or CLUT_GRID_DEFAULT) if clut_on else None
        if clut_on:
            # CLUT 的反向求解是这个程序里最慢的一步（17³≈5s、25³≈17s、33³≈37s、
            # 45³≈3min、65³≈8min，取决于机器）。所以：先把预计耗时写进日志，
            # 再把计算放到工作线程，界面保持可拖动/可重绘（控件暂时禁用）。
            logging.info(_("Generating CLUT for saving: {}³ grid, this can take a while (seconds to minutes)…").format(grid))

        def worker():
            # 只做纯计算：不碰 GUI，也不改 profile（写 profile 放回主线程）
            if not clut_on:
                return None
            return self.backend.make_clut_tags(grid)

        def on_done(result):
            exc = result if isinstance(result, Exception) else None
            try:
                if exc is not None:
                    msg = _("CLUT generation failed: {}").format(exc)
                    logging.error(msg)
                    tk.messagebox.showerror(_("Error"), msg)
                elif result is not None:
                    tags, model = result
                    self.backend.attach_clut_tags(tags)
                    self._report_clut(tags, model)
                self.backend.save_profile(path, desc)
                logging.info(_("ICC profile saved: {}").format(path))
            except Exception as save_exc:  # noqa: BLE001
                msg = _("Failed to save ICC file: {}").format(save_exc)
                logging.error(msg)
                logging.error(traceback.format_exc())
                tk.messagebox.showerror(_("Error"), msg)
            finally:
                self.unfreeze_ui()
            if self.icc_set_var.get():
                self.preview_var.set(False)
                self.set_icc(path)

        if clut_on:
            self.freeze_ui()
            self.run_in_thread(worker, on_done)
        else:
            # 没有 CLUT 时没有慢步骤，保持原来的同步行为（含安装 ICC）
            self.backend.save_profile(path, desc)
            logging.info(_("ICC profile saved: {}").format(path))
            if self.icc_set_var.get():
                self.preview_var.set(False)
                self.set_icc(path)


if __name__ == "__main__":
    root = tk.Tk()
    app = HDRCalibrationUI(root)
    root.mainloop()
