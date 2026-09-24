"""Tkinter view construction for the HDR calibration application.

The calibration controller lives in :mod:`app`. This module only builds and
lays out widgets, keeping visual changes separate from measurement code.
"""

import os
import tkinter as tk
from tkinter import ttk

from clut_icc import CLUT_GRID_CHOICES, CLUT_GRID_DEFAULT
from color_test_suit import COLOR_CARD_CHOICES, COLOR_CARD_SRGB_12
from i18n.i18n_loader import _


BACKGROUND = "#f8f8f8"
FONT_FAMILY = "Microsoft YaHei"
LABEL_FONT = (FONT_FAMILY, 16)


def build_main_window(controller):
    """Build the main window and expose stateful widgets on ``controller``."""
    MainWindowBuilder(controller).build()


def freeze_window(controller):
    """Disable interactive widgets while remembering their previous states."""
    if getattr(controller, "_ui_frozen", False):
        return
    controller._ui_frozen = True
    controller._disabled_widgets = []

    def remember(widget, state):
        controller._disabled_widgets.append((widget, state))

    def walk(parent):
        for child in parent.winfo_children():
            walk(child)
            if isinstance(
                child,
                (tk.Button, tk.Checkbutton, tk.Radiobutton, tk.Scale, tk.Entry, tk.Text),
            ):
                previous = child.cget("state")
                if previous != "disabled":
                    remember(child, previous)
                    child.configure(state="disabled")
            elif isinstance(child, ttk.Combobox):
                previous = child.cget("state")
                if previous != "disabled":
                    remember(child, previous)
                    child.configure(state="disabled")
            elif isinstance(
                child,
                (ttk.Button, ttk.Checkbutton, ttk.Radiobutton, ttk.Entry, ttk.Menubutton),
            ):
                try:
                    previous = child.cget("state")
                except Exception:
                    previous = "disabled" if "disabled" in child.state() else "normal"
                if previous != "disabled":
                    remember(child, previous)
                    child.configure(state="disabled")

            if isinstance(child, ttk.Menubutton):
                menu = child["menu"] if "menu" in child.keys() else None
                if isinstance(menu, tk.Menu):
                    states = []
                    end = menu.index("end")
                    if end is not None:
                        for index in range(end + 1):
                            try:
                                state = menu.entrycget(index, "state")
                                states.append(state)
                                menu.entryconfigure(index, state="disabled")
                            except Exception:
                                states.append(None)
                    remember(menu, states)

    walk(controller.root)
    controller.root.configure(cursor="wait")
    controller.root.update_idletasks()


def unfreeze_window(controller):
    """Restore widget states saved by :func:`freeze_window`."""
    if not getattr(controller, "_ui_frozen", False):
        return
    for widget, previous in getattr(controller, "_disabled_widgets", []):
        if isinstance(widget, tk.Menu):
            for index, state in enumerate(previous):
                if state is not None:
                    try:
                        widget.entryconfigure(index, state=state)
                    except Exception:
                        pass
            continue
        try:
            if previous != "disabled":
                widget.configure(state=previous)
        except Exception:
            pass
    controller._disabled_widgets.clear()
    controller._ui_frozen = False
    controller.root.configure(cursor="")
    controller.root.update_idletasks()


def show_monitor_overlay(root, monitor_rect, text, duration_ms=1500):
    """Show the selected-display confirmation overlay on one monitor."""
    width, height = 500, 300
    left = int(monitor_rect.get("left", 0))
    top = int(monitor_rect.get("top", 0))
    right = int(monitor_rect.get("right", left + width))
    bottom = int(monitor_rect.get("bottom", top + height))
    x = left + (max(1, right - left) - width) // 2
    y = top + (max(1, bottom - top) - height) // 2

    try:
        previous_topmost = root.attributes("-topmost")
    except Exception:
        previous_topmost = False
    if isinstance(previous_topmost, str):
        previous_topmost = previous_topmost in ("1", "true", "True")
    else:
        previous_topmost = bool(previous_topmost)

    overlay = tk.Toplevel(root)
    overlay.overrideredirect(True)
    overlay.transient(root)
    overlay.attributes("-topmost", True)
    overlay.configure(bg="white")
    overlay.geometry(f"{width}x{height}+{x}+{y}")
    overlay.lift(root)

    frame = tk.Frame(
        overlay, bg="white", highlightthickness=1, highlightbackground="#888888"
    )
    frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(frame, highlightthickness=0, bg="white")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        width // 2,
        height // 2,
        text=text,
        fill="black",
        font=("Segoe UI", 16, "bold"),
    )

    def close_overlay():
        try:
            if overlay.winfo_exists():
                overlay.destroy()
        except Exception:
            pass
        try:
            root.attributes("-topmost", True)
            root.lift()

            def restore_topmost():
                try:
                    root.attributes("-topmost", previous_topmost)
                except Exception:
                    pass

            root.after(10, restore_topmost)
        except Exception:
            pass

    overlay.after(duration_ms, close_overlay)


class MainWindowBuilder:
    """Construct the main window without owning application behavior."""

    def __init__(self, controller):
        self.controller = controller
        self.root = controller.root
        self.style = ttk.Style()

    def build(self):
        self._configure_window()
        self._configure_styles()
        self._build_top_bar()
        self._build_intro()

        settings = tk.Frame(self.root, bg=BACKGROUND)
        settings.pack(pady=20, anchor="w", padx=36)
        self._build_device_settings(settings)
        self._build_calibration_settings(settings)
        self._build_history_settings(settings)
        self._build_action_buttons(settings)
        self._build_log()

    def _configure_window(self):
        self.root.geometry("960x1050")
        self.root.configure(bg=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self.controller.on_exit)
        self.root.option_add("*TCombobox*Listbox*Font", (FONT_FAMILY, 15))

    def _configure_styles(self):
        self.style.theme_use("vista")
        self.style.configure(
            "TopBar.TMenubutton",
            font=(FONT_FAMILY, 14),
            padding=(5, 4),
            background=BACKGROUND,
            relief="flat",
            borderwidth=0,
        )
        self.style.map(
            "TopBar.TMenubutton",
            background=[("active", "#e9eff5"), ("pressed", "#dbe6f0")],
            relief=[("pressed", "flat"), ("!pressed", "flat")],
        )
        try:
            self.style.layout(
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
        self.style.configure("TButton", font=LABEL_FONT, padding=(12, 8), width=20)
        self.style.configure("TCheckbutton", font=LABEL_FONT, padding=(8, 4))

    def _build_top_bar(self):
        top_bar = tk.Frame(self.root, bg=BACKGROUND)
        top_bar.pack(fill="x", padx=36, pady=(0, 4))
        tk.Frame(top_bar, bg="#dcdcdc", height=1).pack(fill="x", side="bottom")

        tools_button = ttk.Menubutton(
            top_bar, text=_("Tools"), style="TopBar.TMenubutton"
        )
        tools_button.pack(side="left", padx=(0, 24))
        tools_menu = tk.Menu(tools_button, tearoff=0, font=(FONT_FAMILY, 14))
        for label, script in (
            ("ICC Modifier", "icc_modifier_app.py"),
            ("Gamut Browser", "gamut_browser_app.py"),
            ("View Grayscale", "view_grayscale_app.py"),
            ("Manual Measurement", "manual_measure_color_app.py"),
        ):
            tools_menu.add_command(
                label=_(label),
                command=lambda name=script: self.controller.open_tools(name),
            )
        tools_button["menu"] = tools_menu

        help_button = ttk.Menubutton(
            top_bar, text=_("Help"), style="TopBar.TMenubutton"
        )
        help_button.pack(side="left")
        help_menu = tk.Menu(help_button, tearoff=0, font=(FONT_FAMILY, 14))
        help_menu.add_command(
            label=_("User Guide"), command=self.controller.open_user_guide_window
        )
        help_menu.add_command(
            label=_("Project Homepage"), command=self.controller.open_project_homepage
        )
        help_button["menu"] = help_menu
        self.controller.help_window = None

    def _build_intro(self):
        intro = "\n".join(
            [
                _("Please read the Help page before use!"),
                _("Require win11 >= 22H2 win10 >= 1709"),
                _("Pattern generator: dogegen"),
                _("Colorimeter driver: argyllcms spotread"),
            ]
        )
        tk.Label(
            self.root,
            text=intro,
            font=LABEL_FONT,
            justify="left",
            bg=BACKGROUND,
            fg="#333333",
            anchor="w",
            pady=12,
            wraplength=2150,
        ).pack(pady=(0, 10), padx=36, anchor="w")
        tk.Frame(self.root, height=1, bg="#dcdcdc").pack(
            fill="x", padx=36, pady=(4, 8)
        )

    def _label(self, parent, text, row, column=0, columnspan=1):
        tk.Label(parent, text=_(text), font=LABEL_FONT, bg=BACKGROUND).grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 10),
            pady=(0, 12),
            columnspan=columnspan,
        )

    def _combobox(self, parent, variable, values, width=40):
        return ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            font=LABEL_FONT,
            width=width,
            state="readonly",
        )

    def _build_device_settings(self, parent):
        c = self.controller
        c.monitor_list = sorted(c.human_display_config_map.keys())
        c.monitor_var = tk.StringVar(
            value=c.monitor_list[0] if c.monitor_list else "No Monitor Found"
        )
        self._label(parent, "Select display:", 0, columnspan=3)
        monitor_menu = self._combobox(parent, c.monitor_var, c.monitor_list)
        monitor_menu.grid(
            row=0, column=0, sticky="we", padx=(120, 0), pady=(0, 12), columnspan=3
        )
        monitor_menu.bind("<<ComboboxSelected>>", c.on_monitor_changed)

        instruments, modes = c.get_instrument_mode_options()
        c.instrument_desc = [item[1] for item in instruments]
        c.instrument_choose = [item[0] for item in instruments]
        c.mode_desc = [item[1] for item in modes]
        c.mode_choose = [item[0] for item in modes]
        if not c.instrument_desc:
            c.instrument_desc = [_("No instrument found")]
        if not c.mode_desc:
            c.mode_desc = [_("No instrument found")]

        c.instrument_var = tk.StringVar(value=c.instrument_desc[0])
        self._label(parent, "Select instrument:", 1, columnspan=3)
        self._combobox(parent, c.instrument_var, c.instrument_desc).grid(
            row=1, column=0, sticky="we", padx=(120, 0), pady=(0, 12), columnspan=3
        )
        c.mode_var = tk.StringVar(value=c.mode_desc[0])
        self._label(parent, "Instrument mode:", 2, columnspan=3)
        self._combobox(parent, c.mode_var, c.mode_desc).grid(
            row=2, column=0, sticky="we", padx=(120, 0), pady=(0, 12), columnspan=3
        )

    def _build_calibration_settings(self, parent):
        c = self.controller
        c.pq_points_var = tk.StringVar(value="128")
        self._label(parent, "Grayscale samples:", 3)
        self._combobox(
            parent, c.pq_points_var, ["128", "256", "512", "1024"], width=6
        ).grid(row=3, column=0, sticky="we", padx=(120, 33), pady=(0, 12))

        c.color_space_var = tk.StringVar(value=COLOR_CARD_SRGB_12)
        self._label(parent, "Color sample set:", 3, column=1)
        self._combobox(
            parent, c.color_space_var, COLOR_CARD_CHOICES, width=22
        ).grid(row=3, column=1, sticky="we", padx=(120, 33), pady=(0, 12))

        c.eetf_var = tk.BooleanVar(value=False)
        c.eetf_check = ttk.Checkbutton(
            parent,
            text=_("Luminance mapping"),
            variable=c.eetf_var,
            style="TCheckbutton",
            command=c.on_eetf_toggle,
        )
        c.bright_var = tk.BooleanVar(value=False)
        c.bright_checkbutton = ttk.Checkbutton(
            parent,
            text=_("Bright mode"),
            variable=c.bright_var,
            style="TCheckbutton",
        )
        c.bright_checkbutton.grid(
            row=3, column=2, sticky="w", padx=(0, 0), pady=(0, 14)
        )

        c.white_point_var = tk.StringVar(value="0.3127,0.3290")
        self._label(parent, "WhitePoint:", 5)
        ttk.Entry(
            parent, textvariable=c.white_point_var, font=LABEL_FONT, width=12
        ).grid(row=5, column=0, sticky="we", padx=(120, 33), pady=(0, 12))

        c.preview_var = tk.BooleanVar(value=False)
        c.preview_var.trace_add("write", lambda *_: c.on_preview_toggle())
        c.preview_checkbutton = ttk.Checkbutton(
            parent,
            text=_("Preview calibration result"),
            variable=c.preview_var,
            style="TCheckbutton",
        )
        c.preview_checkbutton.grid(
            row=5, column=1, sticky="w", padx=(0, 0), pady=(0, 14)
        )

        c.icc_set_var = tk.BooleanVar(value=True)
        c.icc_set_checkbutton = ttk.Checkbutton(
            parent,
            text=_("Load as default ICC after saving"),
            variable=c.icc_set_var,
            style="TCheckbutton",
        )
        c.icc_set_checkbutton.grid(
            row=5, column=2, sticky="w", padx=(0, 0), pady=(0, 14)
        )

    def _build_history_settings(self, parent):
        self._build_gray_history(parent)
        self._build_clut_settings(parent)
        self._build_color_history(parent)

    def _build_gray_history(self, parent):
        c = self.controller
        c.gray_history_var = tk.StringVar()
        c.gray_history_choices = c._build_gray_history_choices()
        self._label(parent, "Historical gray data:", 4)
        frame = tk.Frame(parent, bg=BACKGROUND)
        frame.grid(
            row=4, column=0, columnspan=2, sticky="we", padx=(120, 33), pady=(0, 12)
        )
        c.gray_history_menu = self._combobox(
            frame, c.gray_history_var, c.gray_history_choices, width=30
        )
        c.gray_history_menu.pack(side="left", fill="x", expand=True)
        c.gray_history_menu.bind("<<ComboboxSelected>>", c.on_gray_history_selected)
        ttk.Button(
            frame, text=_("Refresh"), command=c.refresh_gray_history, width=10
        ).pack(side="left", padx=(8, 0))
        if c.gray_history_choices:
            c.gray_history_var.set(c.gray_history_choices[0])

    def _build_clut_settings(self, parent):
        c = self.controller
        frame = tk.Frame(parent, bg=BACKGROUND)
        frame.grid(row=4, column=2, sticky="we", padx=(0, 0), pady=(0, 12))
        c.clut_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text=_("CLUT (A2B0/B2A0)"),
            variable=c.clut_var,
            style="TCheckbutton",
        ).pack(side="left")
        c.clut_grid_var = tk.StringVar(value=str(CLUT_GRID_DEFAULT))
        ttk.Combobox(
            frame,
            textvariable=c.clut_grid_var,
            values=[str(grid) for grid in CLUT_GRID_CHOICES],
            font=(FONT_FAMILY, 14),
            width=4,
            state="readonly",
        ).pack(side="left", padx=(8, 0))

    def _build_color_history(self, parent):
        c = self.controller
        c.color_history_var = tk.StringVar()
        c.color_history_choices = c._build_color_history_choices()
        self._label(parent, "Historical color data:", 6)
        frame = tk.Frame(parent, bg=BACKGROUND)
        frame.grid(
            row=6, column=0, columnspan=2, sticky="we", padx=(120, 33), pady=(0, 12)
        )
        c.color_history_menu = self._combobox(
            frame, c.color_history_var, c.color_history_choices, width=30
        )
        c.color_history_menu.pack(side="left", fill="x", expand=True)
        c.color_history_menu.bind("<<ComboboxSelected>>", c.on_color_history_selected)
        ttk.Button(
            frame, text=_("Refresh"), command=c.refresh_color_history, width=10
        ).pack(side="left", padx=(8, 0))
        if c.color_history_choices:
            c.color_history_var.set(c.color_history_choices[0])

    def _build_action_buttons(self, parent):
        c = self.controller
        for column, text, callback in (
            (0, "Calibrate", c.calibrate_monitor),
            (1, "Measure color accuracy", c.measure_pq),
            (2, "Save as ICC file", c.generate_and_save_icc),
        ):
            ttk.Button(parent, text=_(text), command=callback, width=20).grid(
                row=7,
                column=column,
                padx=(0, 30) if column < 2 else 0,
                pady=(10, 0),
                sticky="w",
            )

        for column, text, command in (
            (0, "Open Device Manager", "start devmgmt.msc"),
            (1, "Open Windows Services", "start services.msc"),
        ):
            ttk.Button(
                parent,
                text=_(text),
                command=lambda value=command: os.system(value),
                width=20,
            ).grid(
                row=8,
                column=column,
                columnspan=2,
                pady=(20, 0),
                sticky="w",
            )
        ttk.Button(
            parent,
            text=_("Install Spyder driver"),
            command=c.open_argyll_download,
            width=20,
        ).grid(row=8, column=2, columnspan=2, pady=(20, 0), sticky="w")

    def _build_log(self):
        c = self.controller
        frame = ttk.LabelFrame(self.root, text=_("Log"))
        frame.pack(fill="both", expand=True, padx=36, pady=(0, 20))
        c.log_text = tk.Text(
            frame,
            height=12,
            wrap="word",
            state="disabled",
            font=(FONT_FAMILY, 12),
            bg="#ffffff",
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=c.log_text.yview)
        c.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        c.log_text.pack(side="left", fill="both", expand=True, padx=6, pady=6)
