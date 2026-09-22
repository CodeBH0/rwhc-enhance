from lut import *
from meta_data import *
from matrix import *
from convert_utils import *
from delteE import *
from icc_rw import ICCProfile
from clut_icc import (
    CLUT_FORMAT_MFT2,
    CLUT_GRID_CHOICES,
    CLUT_GRID_DEFAULT,
    build_model_from_calibration,
    make_clut_tags,
    write_clut_tags,
)
from color_test_suit import *
from color_rw import ColorReader, ColorWriter
from log import logging, TextHandler
from i18n.i18n_loader import _
from gray_history import parse_gray_runs
from color_history import (
    parse_color_runs,
    gamut_xyz_from_run as color_run_gamut_xyz,
    peak_min_luminance as color_run_peak_min,
    peak_min_luminance_from_xyz as color_run_peak_min_xyz,
    run_label as color_run_label,
    run_summary as color_run_summary,
)

from win_display import (
    get_all_display_config,
    get_monitor_rect_by_gdi_name,
    cp_add_display_association,
    install_icc,
    uninstall_icc,
    cp_remove_display_association,
    luid_from_dict,
)

from tkinter import filedialog, ttk, Canvas
import tkinter as tk
import numpy as np
import webbrowser
import subprocess
import threading
import traceback
import tempfile
import ctypes
import time
import uuid
import copy
import sys
import re
import os


class HDRCalibrationUI:
    def __init__(self, root):
        self.init_base_icc()
        self.target_xyz = []
        self.convert_command = []
        self.measured_xyz = {}

        self.gamut_test_rgb = {
            "red": [592, 0, 0],
            "green": [0, 592, 0],
            "blue": [0, 0, 592],
            "white": [1023, 1023, 1023],
            # SDR 纸白测试点：码值在 calibrate_monitor 里按系统报告的实际纸白重新计算
            "white_paper": [592, 592, 592],
            "black": [0, 0, 0],
        }
        self.measure_gamut_xyz = {}

        self.preview_icc_name = None
        self.measured_pq = {"red": [], "green": [], "blue": []}

        # Historical PQ gray data (parsed from hc.log) for skipping re-measurement
        self.gray_history_runs = []
        self.selected_gray_run = None

        # Historical colour data (parsed from hc.log): primaries + white/black +
        # the colour-card run of an earlier calibration.  Reusing it feeds the
        # CLUT's forward model (primaries matrix) without re-measuring.
        self.color_history_runs = []
        self.selected_color_run = None
        self.reused_color_run = None
        self.reused_color_card = None

        self.proc_color_write = None
        self.proc_color_reader = None

        self.icc_change_delay = 0

        # CLUT 标签缓存（键 = 网格点数 + 模型指纹），避免预览/保存重复计算
        self._clut_cache = None

        self.eetf_args = {
            "source_max": 10000,
            "source_min": 0,
            "monitor_max": None,
            "monitor_min": None,
        }
        self._eetf_window = None

        self.project_url = "https://github.com/forbxy/rwhc"
        self.argyll_download_url = "https://www.argyllcms.com/downloadwin.html"

        self.root = root
        self.root.title("RealWindowsHDRCalibrator")
        self.set_dpi_awareness()

        self.displays_config = get_all_display_config()
        hc = {}
        for itm in self.displays_config:
            product_id = itm["target"]["device_path"].split("#")[1]
            f_name = itm["target"].get("friendly_name") or "none"
            h_dname = f"{itm['path_index']}_{f_name}_{product_id}"
            hc[h_dname] = copy.deepcopy(itm)
            hc[h_dname]["monitor_rect"] = get_monitor_rect_by_gdi_name(
                itm["source"]["gdi_name"]
            )
            hc[h_dname]["color_work_status"] = "sdr"
            # Generate the screen's address in the Windows registry
            t = itm["target"]["device_path"].split("\\")[-1].split("#")[:-1]
            hc[h_dname]["pnp_device_id"] = "\\".join(t)
            """SDR_ACM = advanced_color  & wide_color_enforced
               HDR =     advanced_color  & !wide_color_enforced
               SDR =     !advanced_color & !wide_color_enforced
            """
            if hc[h_dname]["target"]["advanced_color"]["enabled"]:
                hc[h_dname]["color_work_status"] = "hdr"
                if hc[h_dname]["target"]["advanced_color"]["wide_color_enforced"]:
                    hc[h_dname]["color_work_status"] = "sdr_acm"
        self.human_display_config_map = hc

        # Parse historical PQ gray runs at startup so the user can skip re-measuring.
        self.log_path = os.path.join(os.path.dirname(__file__), "hc.log")
        try:
            self.gray_history_runs = parse_gray_runs(self.log_path)
        except Exception:
            self.gray_history_runs = []
        # Same for the historical colour runs (primaries + colour card).
        try:
            self.color_history_runs = parse_color_runs(self.log_path)
        except Exception:
            self.color_history_runs = []

        self.build_ui()
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

    def build_ui(self):
        # 1000px 在加入「历史颜色数据」一行后会挤掉日志框底部（requested height
        # 约 1042px），因此把默认高度提到 1050 留出余量；窗口仍可自由缩放。
        self.root.geometry("960x1050")
        self.root.configure(bg="#f8f8f8")
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)
        intro_font = ("Microsoft YaHei", 16)

        style = ttk.Style()
        style.theme_use("vista")
        style.configure(
            "TopBar.TMenubutton",
            font=("Microsoft YaHei", 14),
            padding=(5, 4),
            background="#f8f8f8",
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "TopBar.TMenubutton",
            background=[("active", "#e9eff5"), ("pressed", "#dbe6f0")],
            relief=[("pressed", "flat"), ("!pressed", "flat")],
        )
        try:
            style.layout(
                "TopBar.TMenubutton",
                [
                    (
                        "Menubutton.padding",
                        {
                            "children": [("Menubutton.label", {"sticky": "nswe"})],
                            "sticky": "nswe",
                        },
                    )
                ],
            )
        except tk.TclError:
            pass

        top_bar = tk.Frame(self.root, bg="#f8f8f8")
        top_bar.pack(fill="x", padx=36, pady=(0, 4))

        tk.Frame(top_bar, bg="#dcdcdc", height=1).pack(fill="x", side="bottom")

        tools_btn = ttk.Menubutton(top_bar, text=_("Tools"), style="TopBar.TMenubutton")
        tools_btn.pack(side="left", padx=(0, 24))
        tools_menu = tk.Menu(tools_btn, tearoff=0, font=("Microsoft YaHei", 14))
        tools_menu.add_command(label=_("ICC Modifier"), command=lambda: self.open_tools("icc_modifier_app.py"))
        tools_menu.add_command(label=_("Gamut Browser"), command=lambda: self.open_tools("gamut_browser_app.py"))
        tools_menu.add_command(label=_("View Grayscale"), command=lambda: self.open_tools("view_grayscale_app.py"))
        tools_menu.add_command(label=_("Manual Measurement"), command=lambda: self.open_tools("manual_measure_color_app.py"))
        tools_btn["menu"] = tools_menu  

        help_btn = ttk.Menubutton(top_bar, text=_("Help"), style="TopBar.TMenubutton")
        help_btn.pack(side="left")
        help_menu = tk.Menu(help_btn, tearoff=0, font=("Microsoft YaHei", 14))
        help_menu.add_command(label=_("User Guide"), command=self.open_user_guide_window)
        help_menu.add_command(label=_("Project Homepage"), command=self.open_project_homepage)
        help_btn["menu"] = help_menu
        self.help_window = None

        intro = "\n".join([
            _("Please read the Help page before use!"),
            _("Require win11 >= 22H2 win10 >= 1709"),
            _("Pattern generator: dogegen"),
            _("Colorimeter driver: argyllcms spotread")
        ])
        tk.Label(
            self.root,
            text=intro,
            font=intro_font,
            justify="left",
            bg="#f8f8f8",
            fg="#333333",
            anchor="w",
            pady=12,
            wraplength=2150,
        ).pack(pady=(0, 10), padx=36, anchor="w")

        tk.Frame(self.root, height=1, bg="#dcdcdc").pack(fill="x", padx=36, pady=(4, 8))

        style.configure(
            "TButton", font=("Microsoft YaHei", 16), padding=(12, 8), width=20
        )
        style.configure(
            "TCheckbutton",
            font=("Microsoft YaHei", 16),  # Set font size
            padding=(8, 4),  # Optional: adjust padding
        )


        button_frame = tk.Frame(self.root, bg="#f8f8f8")
        button_frame.pack(pady=20, anchor="w", padx=36)
        self.root.option_add("*TCombobox*Listbox*Font", ("Microsoft YaHei", 15))

        self.monitor_list = list(self.human_display_config_map.keys())
        self.monitor_list.sort()
        self.monitor_var = tk.StringVar(
            value=self.monitor_list[0] if self.monitor_list else "No Monitor Found"
        )
        tk.Label(
            button_frame, text=_("Select display:"), font=("Microsoft YaHei", 16), bg="#f8f8f8"
        ).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 12), columnspan=3)
        monitor_menu = ttk.Combobox(
            button_frame,
            textvariable=self.monitor_var,
            values=self.monitor_list,
            font=("Microsoft YaHei", 16),
            width=40,
            state="readonly",
        )
        monitor_menu.grid(
            row=0, column=0, sticky="we", padx=(120, 0), pady=(0, 12), columnspan=3
        )
        monitor_menu.bind("<<ComboboxSelected>>", lambda e: self.on_monitor_changed())

        im = self.get_instrument_mode_options()
        self.instrument_desc = [itm[1] for itm in im[0]]
        self.instrument_choose = [itm[0] for itm in im[0]]
        self.mode_desc = [itm[1] for itm in im[1]]
        self.mode_choose = [itm[0] for itm in im[1]]
        if not self.instrument_desc:
            self.instrument_desc = [_("No instrument found")]
        self.instrument_var = tk.StringVar(value=self.instrument_desc[0])
        tk.Label(
            button_frame, text=_("Select instrument:"), font=("Microsoft YaHei", 16), bg="#f8f8f8"
        ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 12), columnspan=3)
        instrument_menu = ttk.Combobox(
            button_frame,
            textvariable=self.instrument_var,
            values=self.instrument_desc,
            font=("Microsoft YaHei", 16),
            width=40,
            state="readonly",
        )
        instrument_menu.grid(
            row=1, column=0, sticky="we", padx=(120, 0), pady=(0, 12), columnspan=3
        )
        if not self.mode_desc:
            self.mode_desc = [_("No instrument found")]
        self.mode_var = tk.StringVar(value=self.mode_desc[0])
        tk.Label(
            button_frame, text=_("Instrument mode:"), font=("Microsoft YaHei", 16), bg="#f8f8f8"
        ).grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(0, 12), columnspan=3)
        mode_menu = ttk.Combobox(
            button_frame,
            textvariable=self.mode_var,
            values=self.mode_desc,
            font=("Microsoft YaHei", 16),
            width=40,
            state="readonly",
        )
        mode_menu.grid(
            row=2, column=0, sticky="we", padx=(120, 0), pady=(0, 12), columnspan=3
        )

        self.pq_points_var = tk.StringVar(value="128")  
        tk.Label(
            button_frame,
            text=_("Grayscale samples:"),
            font=("Microsoft YaHei", 16),
            bg="#f8f8f8",
        ).grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        pq_points_menu = ttk.Combobox(
            button_frame,
            textvariable=self.pq_points_var,
            values=["128", "256", "512", "1024"],
            font=("Microsoft YaHei", 16),
            width=6,
            state="readonly",
        )
        pq_points_menu.grid(row=3, column=0, sticky="we", padx=(120, 33), pady=(0, 12))

        self.color_space_var = tk.StringVar(value=COLOR_CARD_SRGB_12)
        tk.Label(
            button_frame,
            text=_("Color sample set:"),
            font=("Microsoft YaHei", 16),
            bg="#f8f8f8",
        ).grid(row=3, column=1, sticky="w", padx=(0, 10), pady=(0, 12))
        color_space_menu = ttk.Combobox(
            button_frame,
            textvariable=self.color_space_var,
            values=COLOR_CARD_CHOICES,
            font=("Microsoft YaHei", 16),
            width=22,
            state="readonly",
        )
        color_space_menu.grid(
            row=3, column=1, sticky="we", padx=(120, 33), pady=(0, 12)
        )

        self.eetf_var = tk.BooleanVar(value=False)
        self.eetf_check = ttk.Checkbutton(
            button_frame,
            text=_("Luminance mapping"),
            variable=self.eetf_var,
            style="TCheckbutton",
            command=self.on_eetf_toggle,
        )
        # self.eetf_check.grid(row=3, column=2, sticky="w", padx=(0, 10), pady=(0, 12))
        # EETF functionality needs refactor before enabling
        
        self.bright_var = tk.BooleanVar(value=False) 
        self.bright_checkbutton = ttk.Checkbutton(
            button_frame,
            text=_("Bright mode"),
            variable=self.bright_var,
            style="TCheckbutton",
        )
        self.bright_checkbutton.grid(
            row=3, column=2, sticky="w", padx=(0, 0), pady=(0, 14)
        )

        # Historical PQ gray data selector: reuse a past measurement instead of
        # re-measuring the PQ curve when only the color temperature changed.
        self.gray_history_var = tk.StringVar()
        self.gray_history_choices = self._build_gray_history_choices()
        tk.Label(
            button_frame,
            text=_("Historical gray data:"),
            font=("Microsoft YaHei", 16),
            bg="#f8f8f8",
        ).grid(row=4, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        gray_history_frame = tk.Frame(button_frame, bg="#f8f8f8")
        gray_history_frame.grid(
            row=4, column=0, columnspan=2, sticky="we", padx=(120, 33), pady=(0, 12)
        )
        self.gray_history_menu = ttk.Combobox(
            gray_history_frame,
            textvariable=self.gray_history_var,
            values=self.gray_history_choices,
            font=("Microsoft YaHei", 16),
            width=30,
            state="readonly",
        )
        self.gray_history_menu.pack(side="left", fill="x", expand=True)
        self.gray_history_menu.bind("<<ComboboxSelected>>", self.on_gray_history_selected)
        ttk.Button(
            gray_history_frame,
            text=_("Refresh"),
            command=self.refresh_gray_history,
            style="TButton",
            width=10,
        ).pack(side="left", padx=(8, 0))
        if self.gray_history_choices:
            self.gray_history_var.set(self.gray_history_choices[0])

        # CLUT（ICC 标准多维查找表）输出选项。
        # 原有的矩阵 + MHC2 标签始终保留；这里只决定是否额外写入 A2B0/B2A0。
        clut_frame = tk.Frame(button_frame, bg="#f8f8f8")
        clut_frame.grid(row=4, column=2, sticky="we", padx=(0, 0), pady=(0, 12))
        self.clut_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            clut_frame,
            text=_("CLUT (A2B0/B2A0)"),
            variable=self.clut_var,
            style="TCheckbutton",
        ).pack(side="left")
        self.clut_grid_var = tk.StringVar(value=str(CLUT_GRID_DEFAULT))
        ttk.Combobox(
            clut_frame,
            textvariable=self.clut_grid_var,
            values=[str(g) for g in CLUT_GRID_CHOICES],
            font=("Microsoft YaHei", 14),
            width=4,
            state="readonly",
        ).pack(side="left", padx=(8, 0))

        self.white_point_var = tk.StringVar(value="0.3127,0.3290")
        tk.Label(
            button_frame,
            text=_("WhitePoint:"),
            font=("Microsoft YaHei", 16),
            bg="#f8f8f8",
        ).grid(row=5, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        white_point_entry = ttk.Entry(
            button_frame,
            textvariable=self.white_point_var,
            font=("Microsoft YaHei", 16),
            width=12,
        )
        white_point_entry.grid(row=5, column=0, sticky="we", padx=(120, 33), pady=(0, 12))

        self.preview_var = tk.BooleanVar(value=False) 
        self.preview_var.trace_add("write", lambda *a: self.on_preview_toggle())
        self.preview_checkbutton = ttk.Checkbutton(
            button_frame,
            text=_("Preview calibration result"),
            variable=self.preview_var,
            style="TCheckbutton",
        )
        self.preview_checkbutton.grid(
            row=5, column=1, sticky="w", padx=(0, 0), pady=(0, 14)
        )

        self.icc_set_var = tk.BooleanVar(value=True) 
        self.icc_set_checkbutton = ttk.Checkbutton(
            button_frame,
            text=_("Load as default ICC after saving"),
            variable=self.icc_set_var,
            style="TCheckbutton",
        )
        self.icc_set_checkbutton.grid(
            row=5, column=2, sticky="w", padx=(0, 0), pady=(0, 14)
        )

        # 历史颜色数据（原色/白点 + 色卡）选择器：同一台显示器做多个色温 profile 时，
        # 色域与色卡测量同样不必每次重做——它们由 panel 原生响应决定，与目标白点无关。
        self.color_history_var = tk.StringVar()
        self.color_history_choices = self._build_color_history_choices()
        tk.Label(
            button_frame,
            text=_("Historical color data:"),
            font=("Microsoft YaHei", 16),
            bg="#f8f8f8",
        ).grid(row=6, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        color_history_frame = tk.Frame(button_frame, bg="#f8f8f8")
        color_history_frame.grid(
            row=6, column=0, columnspan=2, sticky="we", padx=(120, 33), pady=(0, 12)
        )
        self.color_history_menu = ttk.Combobox(
            color_history_frame,
            textvariable=self.color_history_var,
            values=self.color_history_choices,
            font=("Microsoft YaHei", 16),
            width=30,
            state="readonly",
        )
        self.color_history_menu.pack(side="left", fill="x", expand=True)
        self.color_history_menu.bind("<<ComboboxSelected>>", self.on_color_history_selected)
        ttk.Button(
            color_history_frame,
            text=_("Refresh"),
            command=self.refresh_color_history,
            style="TButton",
            width=10,
        ).pack(side="left", padx=(8, 0))
        if self.color_history_choices:
            self.color_history_var.set(self.color_history_choices[0])

        ttk.Button(
            button_frame,
            text=_("Calibrate"),
            command=self.calibrate_monitor,
            style="TButton",  
            
            width=20,
        ).grid(row=7, column=0, padx=(0, 30), pady=(10, 0), sticky="w")

        ttk.Button(
            button_frame,
            text=_("Measure color accuracy"),
            command=self.measure_pq,
            style="TButton",
            width=20,
        ).grid(row=7, column=1, padx=(0, 30), pady=(10, 0), sticky="w")

        ttk.Button(
            button_frame,
            text=_("Save as ICC file"),
            command=self.generate_and_save_icc,
            style="TButton",
            width=20,
        ).grid(row=7, column=2, pady=(10, 0), sticky="w")

        ttk.Button(
            button_frame,
            text=_("Open Device Manager"),
            command=lambda: os.system("start devmgmt.msc"),
            style="TButton",
            width=20,
        ).grid(row=8, column=0, columnspan=2, pady=(20, 0), sticky="w")
        ttk.Button(
            button_frame,
            text=_("Open Windows Services"),
            command=lambda: os.system("start services.msc"),
            style="TButton",
            width=20,
        ).grid(row=8, column=1, columnspan=2, pady=(20, 0), sticky="w")

        ttk.Button(
            button_frame,
            text=_("Install Spyder driver"),
            command=lambda: webbrowser.open(self.argyll_download_url),
            style="TButton",
            width=20,
        ).grid(row=8, column=2, columnspan=2, pady=(20, 0), sticky="w")

        log_frame = ttk.LabelFrame(self.root, text=_("Log"))
        log_frame.pack(fill="both", expand=True, padx=36, pady=(0, 20))
        self.log_text = tk.Text(
            log_frame,
            height=12,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei", 12),
            bg="#ffffff",
        )
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    def init_base_icc(self):
        self.icc_handle = ICCProfile("data/hdr_empty.icc")
        self.icc_data = self.icc_handle.read_all()
        self.MHC2 = copy.deepcopy(self.icc_data["MHC2"])
        if self.MHC2["red_lut"] == [0, 1]:
            self.MHC2["red_lut"] = generate_pq_lut().tolist()
            self.MHC2["green_lut"] = generate_pq_lut().tolist()
            self.MHC2["blue_lut"] = generate_pq_lut().tolist()
            self.MHC2["entry_count"] = len(self.MHC2["red_lut"])

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
        try:
            self.gray_history_runs = parse_gray_runs(self.log_path)
        except Exception as e:
            logging.error(_("Failed to refresh gray history: {}").format(e))
            self.gray_history_runs = []
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
        try:
            self.color_history_runs = parse_color_runs(self.log_path)
        except Exception as e:
            logging.error(_("Failed to refresh color history: {}").format(e))
            self.color_history_runs = []
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
        gamut = color_run_gamut_xyz(run)
        max_lumi, min_lumi = color_run_peak_min(run, self.eetf_var.get(), self.eetf_args)
        # `_apply_gamut_data` 会把六个色域点写入 self.measure_gamut_xyz，
        # 并按实时测量的同一套逻辑写 lumi / rXYZ..wtpt / TRC / MHC2。
        self._apply_gamut_data(gamut, min_lumi, max_lumi)

        self.reused_color_run = run
        card = []
        for _idx, rgb, _target, measured in run["points"]:
            code = (np.asarray(rgb, dtype=float) / 1023.0).tolist()
            xyz_nits = (np.asarray(measured, dtype=float) * 10000.0).tolist()
            card.append((code, xyz_nits))
        self.reused_color_card = card or None
        # Keep the app's own state consistent with a live run, so anything that
        # reads measured_xyz / target_xyz after calibration sees the reuse.
        self.target_xyz = [list(np.asarray(t, float)) for _i, _r, t, _m in run["points"]]
        self.measured_xyz = [list(np.asarray(m, float)) for _i, _r, _t, m in run["points"]]

    def _history_only_calibration(self):
        """
        两个历史数据都选中时，本次校准完全不需要测量。

        · 灰阶曲线来自「历史灰阶数据」（`calibrate_pq` 走复用分支）；
        · 原色/白点/纸白/黑场与色卡来自「历史颜色数据」（`_apply_color_run`）；
        · 「校准后」重测（`measure_gamut_after`）也被复用分支取代。

        因此可以不启动 dogegen 与 spotread，直接产出 ICC。
        """
        return (self.selected_gray_run is not None
                and self.selected_color_run is not None)

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
        exe_path = os.path.join(os.path.dirname(__file__), "bin", "spotread.exe")
        if not os.path.isfile(exe_path):
            return ([], [])
        try:
            proc = subprocess.run(
                [exe_path, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
                check=False,
            )
            output = proc.stdout
        except Exception as e:
            return ([], [])

        lines = [itm.strip() for itm in output.splitlines()]

        c_list = []
        for i, ln in enumerate(lines):
            if "-c listno" in ln:
                # Collect downward until hitting the next argument (line starts with '-' and is not indented)
                j = i
                while j < len(lines):
                    l2 = lines[j]
                    if j != i and re.match(r"^-\w", l2):
                        break
                    # Match pattern like "  1 = 'xxxx'"
                    m = re.match(r"\s*(\d+)\s*=\s*'(.+)'", l2)
                    if m:
                        c_list.append([m.group(1), m.group(2)])
                    j += 1
                break  # Exit after finishing the block

        # -y
        y_list = []
        start_idx = None
        for i, ln in enumerate(lines):
            if re.match(r"^-y\s+", ln):
                start_idx = i
                break
        if start_idx is not None:
            # Collect the -y block
            block = []
            for j in range(start_idx, len(lines)):
                l2 = lines[j]
                if j == start_idx:
                    l2 = l2[3:]  # Strip the "-y " prefix
                if j > start_idx and re.match(r"^\-\w", l2):  # Next argument begins
                    break
                block.append(l2.rstrip())
            for raw in block:
                if not raw.strip():
                    continue
                m = raw.split("    ")
                code = m[0].strip()
                desc = m[-1].strip()
                y_list.append([code, desc])
        if not y_list:
            logging.error(_("Failed to parse spotread -y modes; the output format may have changed"))
        return c_list, y_list

    def get_spotread_args(self):
        args = []
        instrument_idx = self.instrument_desc.index(self.instrument_var.get())
        mode_idx = self.mode_desc.index(self.mode_var.get())
        args.append("-x")
        args.append("-e")
        print(instrument_idx, mode_idx)
        if len(self.instrument_choose) > 0:
            args.extend(["-c", self.instrument_choose[instrument_idx]])
        if len(self.mode_choose) > 0:
            args.extend(["-y", self.mode_choose[mode_idx].split("|")[0]])
        return args
    
    def open_tools(self, tool_name):
        try:
            script = os.path.join(os.path.dirname(__file__), "tools", tool_name)
            if not os.path.isfile(script):
                tk.messagebox.showerror(_("Error"), _("File not found: {}").format(script))
                return
            print([sys.executable, script], os.path.dirname(script))
            subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script), env=os.environ.copy())
        except Exception as e:
                tk.messagebox.showerror(_("Error"), _("Failed to launch {}: {}").format(tool_name, e))


    def open_project_homepage(self):
        webbrowser.open(self.project_url)

    def open_user_guide_window(self):
        webbrowser.open(self.project_url)

    def set_icc(self, path):
        """
        path: The file path of the ICC profile to install.
        Install the specified ICC file, 
        associate it with the currently selected display, 
        and set it as that display's default ICC.
        """
        install_icc(path)
        icc_name = os.path.basename(path)
        monitor = self.monitor_var.get()
        info = self.human_display_config_map.get(monitor)
        luid = luid_from_dict(info["adapter_luid"])
        sid = info["source"]["id"]
        hdr = False
        if info["color_work_status"] == "hdr":
            hdr = True
        cp_add_display_association(
            luid, sid, icc_name, set_as_default=True, associate_as_advanced_color=hdr
        )

    def clean_icc(self, name):
        """
        name: The name of the ICC profile to clean (without .icc or .icm).
        Unassociate the specified ICC file from the currently selected display, 
        unset it as that display's default profile, 
        and remove the ICC file from the system.
        """
        path = f"{name}.icc"
        monitor = self.monitor_var.get()
        info = self.human_display_config_map.get(monitor)
        luid = luid_from_dict(info["adapter_luid"])
        sid = info["source"]["id"]
        hdr = False
        if info["color_work_status"] == "hdr":
            hdr = True
        cp_remove_display_association(luid, sid, path, associate_as_advanced_color=hdr)
        uninstall_icc(path, force=True)

    def freeze_ui(self):
        """
        Disable all interactive controls in the window; 
        save their original states (including Combobox 'readonly' state).
        """
        if getattr(self, "_ui_frozen", False):
            return
        self._ui_frozen = True
        self._disabled_widgets = []

        def add(widget, prev_state):
            self._disabled_widgets.append((widget, prev_state))

        def walk(w):
            for child in w.winfo_children():
                walk(child)
                if isinstance(
                    child,
                    (
                        tk.Button,
                        tk.Checkbutton,
                        tk.Radiobutton,
                        tk.Scale,
                        tk.Entry,
                        tk.Text,
                    ),
                ):
                    prev = child.cget("state")
                    if prev != "disabled":
                        add(child, prev)
                        child.config(state="disabled")
                elif isinstance(
                    child,
                    (
                        ttk.Button,
                        ttk.Checkbutton,
                        ttk.Radiobutton,
                        ttk.Entry,
                        ttk.Menubutton,
                    ),
                ):
                    try:
                        prev = child.cget("state")
                    except Exception:
                        try:
                            prev = (
                                "disabled" if "disabled" in child.state() else "normal"
                            )
                        except Exception:
                            prev = "normal"
                    if prev != "disabled":
                        add(child, prev)
                        child.configure(state="disabled")
                elif isinstance(child, ttk.Combobox):
                    prev = child.cget("state")  # Could be 'readonly' or 'normal'
                    if prev != "disabled":
                        add(child, prev)
                        child.configure(state="disabled")
                if isinstance(child, ttk.Menubutton):
                    m = child["menu"] if "menu" in child.keys() else None
                    if isinstance(m, tk.Menu):
                        entry_states = []
                        end = m.index("end")
                        if end is not None:
                            for i in range(end + 1):
                                try:
                                    st = m.entrycget(i, "state")
                                    entry_states.append(st)
                                    m.entryconfig(i, state="disabled")
                                except Exception:
                                    entry_states.append(None)
                        add(m, entry_states)

        walk(self.root)
        self.root.configure(cursor="wait")
        self.root.update_idletasks()

    def unfreeze_ui(self):
        """
        Restore the interactive controls' states that were saved prior to calling freeze_ui.
        """
        if not getattr(self, "_ui_frozen", False):
            return
        for widget, prev in getattr(self, "_disabled_widgets", []):
            if isinstance(widget, tk.Menu):
                for i, st in enumerate(prev):
                    if st is not None:
                        try:
                            widget.entryconfig(i, state=st)
                        except Exception:
                            pass
                continue
            try:
                if prev != "disabled":
                    widget.configure(state=prev)
            except Exception:
                pass
        self._disabled_widgets.clear()
        self._ui_frozen = False
        self.root.configure(cursor="")
        self.root.update_idletasks()

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
            if self.proc_color_write:
                self.proc_color_write.terminate()
                self.proc_color_write = None
            if self.proc_color_reader:
                self.proc_color_reader.terminate()
                self.proc_color_reader = None
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
                mon = get_monitor_rect_by_gdi_name(info["source"]["gdi_name"])
            except Exception:
                return

        box_w = 500
        box_h = 300
        duration_ms = 1500
        text = _("Selected this display")

        mon_left = int(mon.get("left", 0))
        mon_top = int(mon.get("top", 0))
        mon_right = int(mon.get("right", mon_left + box_w))
        mon_bottom = int(mon.get("bottom", mon_top + box_h))
        mon_w = max(1, mon_right - mon_left)
        mon_h = max(1, mon_bottom - mon_top)

        x = mon_left + (mon_w - box_w) // 2
        y = mon_top + (mon_h - box_h) // 2

        try:
            prev_top = self.root.attributes("-topmost")
        except Exception:
            prev_top = False

        def _to_bool(v):
            if isinstance(v, str):
                return v in ("1", "true", "True")
            return bool(v)

        prev_top_bool = _to_bool(prev_top)

        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.transient(self.root)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="white")
        overlay.geometry(f"{box_w}x{box_h}+{x}+{y}")
        overlay.lift(self.root)

        frm = tk.Frame(
            overlay, bg="white", highlightthickness=1, highlightbackground="#888888"
        )
        frm.pack(fill="both", expand=True)

        canvas = tk.Canvas(
            frm, width=box_w, height=box_h, highlightthickness=0, bg="white"
        )
        canvas.pack(fill="both", expand=True)

        canvas.create_text(
            box_w // 2,
            box_h // 2,
            text=text,
            fill="black",
            font=("Segoe UI", 16, "bold"),
        )

        def _cleanup():
            try:
                if overlay.winfo_exists():
                    overlay.destroy()
            except Exception:
                pass
            try:
                self.root.attributes("-topmost", True)
                self.root.lift()
                # try:
                #     self.root.focus_force()
                # except Exception:
                #     pass

                def _restore():
                    try:
                        self.root.attributes("-topmost", prev_top_bool)
                    except Exception:
                        pass

                self.root.after(10, _restore)
            except Exception:
                pass

        overlay.after(duration_ms, _cleanup)

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
        filename = os.path.basename(path)
        name_without_ext = os.path.splitext(filename)[0]
        desc = [{"lang": "en", "country": "US", "text": name_without_ext}]
        self.icc_handle.write_desc(desc)
        # 预览必须与最终保存的文件一致，所以这里也写入 CLUT（结果有缓存，
        # 只有校准数据变了才会重新生成）。
        try:
            self.write_clut_if_enabled()
        except Exception as exc:
            logging.error(_("CLUT generation failed: {}").format(exc))
        self.icc_handle.rebuild()
        self.icc_handle.save(path)

    def get_paper_white_nit(self):
        """
        返回当前所选显示器在 Windows HDR 模式下系统实际使用的 SDR 纸白亮度（nit）。
        优先取 OS 报告的 DISPLAYCONFIG_SDR_WHITE_LEVEL（滑条设置），
        取不到时回退到 200 nit（旧行为）。
        """
        try:
            info = self.human_display_config_map.get(self.monitor_var.get())
            v = info["target"].get("sdr_white_level_nits")
            if v and v > 0:
                return float(v)
        except Exception:
            pass
        return 200.0

    @safe_call
    def calibrate_monitor(self):
        logging.info(_("Calibration started"))
        self.freeze_ui()
        hdr_status = self.human_display_config_map[self.monitor_var.get()]["color_work_status"]
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
            self.proc_color_write = ColorWriter()
            args = self.get_spotread_args()
            self.proc_color_reader = ColorReader(args)
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
        reuse_color = self.selected_color_run
        if reuse_color is not None:
            logging.info(_("Using historical color data: {}").format(self._color_run_label(reuse_color)))
            logging.info(color_run_summary(reuse_color))

        def calibrate_control():
            # 注意顺序：`init_base_icc()` 会把 profile 重置为模板，所以历史颜色数据必须
            # 在那之后再写入（与实时路径中 measure_gamut_before 的位置一致）。
            self.init_base_icc()
            if reuse_color is not None:
                self._apply_color_run(reuse_color)
                logging.info(_("Historical color data reused: skipped gamut and color-card measurement"))
            else:
                self.measure_gamut_before()
            self.calibrate_pq()
            # self.calibrate_white_by_lut()
            self.calibrate_chromaticity()
            self.measure_gamut_after()
            
            return
            

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
            self.measure_gamut_xyz, self.eetf_var.get(), self.eetf_args)

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
        for key, xyz in gamut_xyz.items():
            self.measure_gamut_xyz[key] = xyz

        """
        FIXME 
        The full‑frame luminance of an OLED may be lower than the maximum luminance of a patch, 
        but currently I can't get dogegen to display in fullscreen.
        """
        peak_lumi = max_lumi
        logging.info(_("Writing max full-frame luminance {}, peak luminance {}, min luminance {}").format(max_lumi, peak_lumi, min_lumi))
        self.MHC2["min_luminance"] = min_lumi
        self.MHC2["peak_luminance"] = peak_lumi
        self.icc_handle.write_XYZType("lumi", [[max_lumi, max_lumi, max_lumi]])

        r, g, b, w = build_primaries_xyz_tags(
            self.measure_gamut_xyz["red"],
            self.measure_gamut_xyz["green"],
            self.measure_gamut_xyz["blue"],
            self.measure_gamut_xyz["white_paper"])
        logging.info(_("Writing RGBW XYZ:\n {}\n {}\n {}\n {}").format(r, g, b, w))
        self.icc_handle.write_XYZType("rXYZ", [r])
        self.icc_handle.write_XYZType("gXYZ", [g])
        self.icc_handle.write_XYZType("bXYZ", [b])
        self.icc_handle.write_XYZType("wtpt", [w])
        # SDR 路径 TRC：校准后显示器在 HDR 模式下的 SDR 响应即 sRGB EOTF
        # （Windows 以纸白为锚点经 MHC2 LUT 驱动）。模板里的 gamma 2.2 与之不符，
        # 会导致按 profile 做 SDR 预测/渲染时色度偏移。
        srgb_trc = {'type': 'curve', 'values': srgb_encode(np.linspace(0, 1, 1024)).tolist()}
        self.icc_handle.write_rgbTRC({'rTRC': srgb_trc, 'gTRC': srgb_trc, 'bTRC': srgb_trc})

        self.icc_handle.write_MHC2(self.MHC2)

        logging.info(_("Gamut measurement finished"))
    
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
            if self.bright_var.get():
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
        if self.bright_var.get():
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
        lut = generate_inversed_lut(generate_bright_pq_lut())
        bright_lut_inv = {"red_lut": lut.tolist(),
                          "green_lut": lut.tolist(),
                          "blue_lut": lut.tolist()}

        white_rgb_fix = apply_lut(XYZ_to_BT2020_PQ_rgb(self.measure_gamut_xyz["white"]/10000), bright_lut_inv)
        black_rgb_fix = apply_lut(XYZ_to_BT2020_PQ_rgb(self.measure_gamut_xyz["black"]/10000), bright_lut_inv)
        white_xyz_fix = BT2020_PQ_rgb_to_XYZ(white_rgb_fix)
        black_xyz_fix = BT2020_PQ_rgb_to_XYZ(black_rgb_fix)
        logging.info(_("Brightness-compensated white RGB: {} XYZ: {}").format(white_rgb_fix, white_xyz_fix))
        logging.info(_("Brightness-compensated black RGB: {} XYZ: {}").format(black_rgb_fix, black_xyz_fix))
        return white_xyz_fix[1]*10000, black_xyz_fix[1]*10000

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
        mode = self.color_space_var.get()
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

        wp = [float(x.strip()) for x in self.white_point_var.get().split(",")]
        m = calculate_bradford_matrix(wp, D65_WHITE_POINT)
        # 记录本次色卡的适配白点：`color_history` 会读这一行，方便日后复用时
        # 判断历史数据是在哪个白点下测的（色卡本身不做白点适配，仅作诊断信息）。
        logging.info(_("Color measurement white point: {}").format(self.white_point_var.get()))
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
        wp = [float(x.strip()) for x in self.white_point_var.get().split(",")]
        m = calculate_bradford_matrix(wp, D65_WHITE_POINT)
        num = int(self.pq_points_var.get())

        history_run = getattr(self, "selected_gray_run", None)
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
        if self.bright_var.get():
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
        self.proc_color_write = ColorWriter()
        args = self.get_spotread_args()
        self.proc_color_reader = ColorReader(args)
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
            mode = self.color_space_var.get()
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

        self.proc_color_write = ColorWriter()
        args = self.get_spotread_args()
        self.proc_color_reader = ColorReader(args)
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
        card = getattr(self, "reused_color_card", None)
        if card:
            return card
        measured = getattr(self, "measured_xyz", None)
        target = getattr(self, "target_xyz", None)
        if not measured or not target or len(measured) != len(target):
            return None
        samples = []
        for itm, meas in zip(target, measured):
            try:
                pq = XYZ_to_BT2020_PQ_rgb(np.asarray(itm, dtype=float))
                code = np.clip(np.asarray(pq, dtype=float), 0.0, 1.0)
                xyz_nits = np.asarray(meas, dtype=float) * 10000.0
            except Exception:
                continue
            samples.append((code, xyz_nits))
        return samples or None

    def _make_display_model(self):
        """
        用当前校准结果构造 DisplayModel（CLUT 生成的正向模型）。

        只依赖校准过程中已经得到的实测数据，不需要额外测量：
          · rXYZ/gXYZ/bXYZ/wtpt 实测原色与白点（可为历史颜色数据）
          · MHC2 每通道 1D LUT（显示器实际响应）
          · 峰值 / 黑场亮度
          · 可选：实测色卡样本，用于校验正向模型（`color_accuracy_report`）
        """
        mhc2 = self.MHC2
        xyz = self.measure_gamut_xyz
        required = ("red", "green", "blue", "white_paper")
        if any(k not in xyz or xyz[k] is None for k in required):
            raise ValueError(_("Primaries not measured yet; run calibration first"))

        gray = getattr(self, "measured_pq", None)
        usable_gray = gray if (gray and len(gray.get("red", [])) > 4) else None
        model = build_model_from_calibration(
            mhc2,
            xyz["red"], xyz["green"], xyz["blue"], xyz["white_paper"],
            peak_nits=mhc2.get("peak_luminance"),
            black_nits=mhc2.get("min_luminance") or 0.0,
            measured_pq=usable_gray,
            measured_colors=self._measured_color_samples(),
        )
        return model

    def _model_signature(self):
        """模型指纹：任一输入变化都会让它变，用于缓存 CLUT 生成结果。"""
        mhc2 = self.MHC2
        xyz = self.measure_gamut_xyz
        try:
            return (
                float(mhc2.get("peak_luminance") or 0.0),
                float(mhc2.get("min_luminance") or 0.0),
                mhc2["red_lut"][::16], mhc2["green_lut"][::16], mhc2["blue_lut"][::16],
                tuple(round(float(v), 6) for k in ("red", "green", "blue", "white_paper")
                      for v in xyz[k]),
            )
        except Exception:
            return None

    def _make_clut_tags(self):
        """
        生成（或从缓存取出）CLUT 标签块。

        这是**纯计算**、只读自我当前状态的一步，也是保存/预览里最慢的一步
        （B2A0 反向求解，33³ 约 30 s，17³ 约 3 s），因此它被单独拆出来，
        好让调用方把这一步放到工作线程里跑，避免界面「卡死」。

        返回 (tags, model)；未启用 CLUT 时返回 (None, None)。
        """
        grid = int(self.clut_grid_var.get() or CLUT_GRID_DEFAULT)
        model = self._make_display_model()
        signature = (grid, self._model_signature())
        cache = getattr(self, "_clut_cache", None)
        if cache is None or cache[0] != signature:
            tags = make_clut_tags(model, grid=grid, fmt=CLUT_FORMAT_MFT2, with_b2a=True)
            self._clut_cache = (signature, tags)
        else:
            tags = cache[1]
        return tags, model

    def _report_clut(self, tags, model):
        """把 CLUT 结果与色卡校验写进日志（保留原提示文本）。"""
        meta = tags["_meta"]
        text = _("CLUT written: {} ({}³, {:.0f} KB, interpolation error {:.3f}%)").format(
            "A2B0/B2A0", meta["grid"],
            sum(meta["tag_sizes"].values()) / 1024.0,
            meta["a2b_interp_mean_err"] * 100.0,
        )
        logging.info(text)
        if meta["a2b_interp_max_err"] > 0.02:
            logging.warning(_("CLUT self-check: max interpolation error {:.3f}% (near the "
                              "PCS ceiling)").format(meta["a2b_interp_max_err"] * 100.0))

        # 实测色卡（本次测量或历史颜色数据）对正向模型的校验：把历史颜色数据
        # 真正「带进」CLUT —— 报告显示模型对这些实测颜色的预测偏差。
        # 阈值定得比较宽松（15 ΔE ITP）：这是**模型自一致性**指标，真实数据上
        # 面板状态漂移本身就能达到这个量级，只有明显不一致（例如选错了 run）
        # 才值得报警。详见 clut_icc.DisplayModel.color_accuracy_report 的说明。
        summary = model.color_accuracy_summary()
        if summary:
            logging.info(_("CLUT colour check (A2B vs measured card): {}").format(summary))
            mean_de = model.color_accuracy_report()["mean_de_itp"]
            if mean_de > 15.0:
                logging.warning(_("CLUT colour check: mean dE ITP {:.2f} is high — the "
                                  "forward model deviates strongly from the measured "
                                  "colour card (reused historical data may come from "
                                  "another display or an incompatible session)").format(mean_de))
        return text

    def write_clut_if_enabled(self):
        """
        按用户选择把 CLUT（A2B0/B2A0）标签写入内存中的 profile。

        返回 (写入的标签列表, 提示文本)；未启用时返回 (None, None)。
        CLUT 不改变矩阵/MHC2 标签，只在同一个 ICC 文件里补上标准多维表。

        注意：这一版是**同步**的（生成 + 写标签都在当前线程）。界面上的「保存」
        走的是 `generate_and_save_icc`，它把耗时的那一步放到工作线程；这里保留
        同步版本给测试与内部调用。
        """
        if not getattr(self, "clut_var", None) or not self.clut_var.get():
            return None, None

        tags, model = self._make_clut_tags()
        written = write_clut_tags(self.icc_handle, tags)
        text = self._report_clut(tags, model)
        return written, text

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
        desc = [{"lang": "en", "country": "US", "text": name_without_ext}]
        self.icc_handle.write_desc(desc)

        clut_on = bool(getattr(self, "clut_var", None) and self.clut_var.get())
        if clut_on:
            # CLUT 的反向求解是这个程序里最慢的一步（17³≈5s、25³≈17s、33³≈37s、
            # 45³≈3min、65³≈8min，取决于机器）。所以：先把预计耗时写进日志，
            # 再把计算放到工作线程，界面保持可拖动/可重绘（控件暂时禁用）。
            grid = int(getattr(self, "clut_grid_var", None) and self.clut_grid_var.get()
                       or CLUT_GRID_DEFAULT)
            logging.info(_("Generating CLUT for saving: {}³ grid, this can take a while (seconds to minutes)…").format(grid))

        def worker():
            # 只做纯计算：不碰 GUI，也不改 profile（写 profile 放回主线程）
            if not clut_on:
                return None
            return self._make_clut_tags()

        def on_done(result):
            exc = result if isinstance(result, Exception) else None
            try:
                if exc is not None:
                    msg = _("CLUT generation failed: {}").format(exc)
                    logging.error(msg)
                    tk.messagebox.showerror(_("Error"), msg)
                elif result is not None:
                    tags, model = result
                    write_clut_tags(self.icc_handle, tags)
                    self._report_clut(tags, model)
                self.icc_handle.rebuild()
                self.icc_handle.save(path)
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
            self.icc_handle.rebuild()
            self.icc_handle.save(path)
            logging.info(_("ICC profile saved: {}").format(path))
            if self.icc_set_var.get():
                self.preview_var.set(False)
                self.set_icc(path)


if __name__ == "__main__":
    root = tk.Tk()
    app = HDRCalibrationUI(root)
    root.mainloop()
