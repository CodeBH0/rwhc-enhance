"""Smoke-test the extracted Tk UI without a display or colorimeter."""

import os
import sys
import tkinter as tk
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app_ui import build_main_window, freeze_window, unfreeze_window  # noqa: E402


def main():
    root = tk.Tk()
    root.withdraw()

    def noop(*args, **kwargs):
        return None

    controller = SimpleNamespace(
        root=root,
        on_exit=noop,
        open_tools=noop,
        open_user_guide_window=noop,
        open_project_homepage=noop,
        human_display_config_map={},
        get_instrument_mode_options=lambda: ([], []),
        on_monitor_changed=noop,
        on_eetf_toggle=noop,
        on_preview_toggle=noop,
        _build_gray_history_choices=lambda: ["Live measurement"],
        on_gray_history_selected=noop,
        refresh_gray_history=noop,
        _build_color_history_choices=lambda: ["Live measurement"],
        on_color_history_selected=noop,
        refresh_color_history=noop,
        calibrate_monitor=noop,
        measure_pq=noop,
        generate_and_save_icc=noop,
        open_argyll_download=noop,
    )

    checks = []
    try:
        build_main_window(controller)
        checks.extend(
            [
                ("日志控件已创建", controller.log_text.winfo_exists()),
                ("灰阶采样默认值不变", controller.pq_points_var.get() == "128"),
                ("CLUT 网格默认值不变", controller.clut_grid_var.get() == "33"),
                (
                    "历史数据下拉框保持只读",
                    str(controller.gray_history_menu.cget("state")) == "readonly",
                ),
            ]
        )

        freeze_window(controller)
        checks.append(
            (
                "冻结时禁用交互控件",
                str(controller.gray_history_menu.cget("state")) == "disabled",
            )
        )
        unfreeze_window(controller)
        checks.extend(
            [
                (
                    "解冻后恢复下拉框只读状态",
                    str(controller.gray_history_menu.cget("state")) == "readonly",
                ),
                ("日志控件仍保持禁用", str(controller.log_text.cget("state")) == "disabled"),
            ]
        )
    finally:
        root.destroy()

    for description, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {description}")
    passed_count = sum(passed for _, passed in checks)
    print(f"\n==== 结果：{passed_count}/{len(checks)} 项通过 ====")
    return 0 if passed_count == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
