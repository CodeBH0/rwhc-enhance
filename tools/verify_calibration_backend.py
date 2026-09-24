"""Hardware-free checks for the GUI-independent calibration backend boundary."""

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from calibration_backend import (  # noqa: E402
    CalibrationBackend,
    CalibrationRequest,
    InstrumentService,
)


def check(results, label, condition):
    passed = bool(condition)
    results.append(passed)
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}")


def make_request(**overrides):
    values = dict(
        monitor_id="display-1",
        instrument_description="Meter A",
        instrument_mode_description="LCD",
        grayscale_samples=128,
        color_sample_set="sRGB(12)",
        white_point="0.3127,0.3290",
    )
    values.update(overrides)
    return CalibrationRequest(**values)


def main():
    results = []
    backend = CalibrationBackend(base_dir=PROJECT_ROOT)
    check(results, "backend 导入不依赖 tkinter", "tkinter" not in sys.modules)

    backend.state.selected_gray_run = {"id": "gray-before-request"}
    backend.state.selected_color_run = {"id": "color-before-request"}
    check(
        results,
        "请求捕获前按 backend 当前历史选择判定无仪器模式",
        backend.history_only_calibration(),
    )

    request = make_request(bright_mode=True, clut_enabled=True, clut_grid=17)
    backend.begin_calibration(request)
    check(results, "Request 是普通数据对象", backend.state.current_request is request)
    check(results, "校准选项不依赖 Tk Variable", request.grayscale_samples == 128)
    check(results, "仅双历史选择才进入无仪器模式", not backend.history_only_calibration())
    both_history = make_request(gray_history_run={"id": 1}, color_history_run={"id": 2})
    backend.begin_calibration(both_history)
    check(results, "双历史选择可脱离仪器", backend.history_only_calibration())

    backend.state.human_display_config_map = {
        "display-1": {"target": {"sdr_white_level_nits": 160.0}}
    }
    check(results, "纸白读取位于 DisplayService", backend.displays.paper_white_nits("display-1") == 160.0)
    check(results, "纸白缺失时保持 200 nit 回退", backend.displays.paper_white_nits("missing") == 200.0)

    class FakeDisplayPlatform:
        def __init__(self):
            self.operations = []

        def enumerate_displays(self):
            return []

        def monitor_rect(self, gdi_name):
            return None

        def install_profile(self, path):
            self.operations.append(("install", path))

        def add_profile_association(self, info, filename):
            self.operations.append(("associate", filename))

        def remove_profile_association(self, info, filename):
            self.operations.append(("remove", filename))

        def uninstall_profile(self, filename):
            self.operations.append(("uninstall", filename))

    fake_display = FakeDisplayPlatform()
    isolated_backend = CalibrationBackend(
        base_dir=PROJECT_ROOT,
        display_platform=fake_display,
    )
    isolated_backend.state.human_display_config_map = {
        "display-1": {
            "adapter_luid": {"low_part": 1, "high_part": 0},
            "source": {"id": 0},
            "color_work_status": "hdr",
        }
    }
    isolated_backend.displays.install_profile("display-1", "preview.icc")
    isolated_backend.displays.remove_profile("display-1", "preview")
    check(
        results,
        "ICC 操作可在 DisplayService 的 platform port 隔离",
        [name for name, _value in fake_display.operations]
        == ["install", "associate", "remove", "uninstall"],
    )

    help_text = """
      -c listno Choose device
        1 = 'Meter A'
        2 = 'Meter B'
      -y c    LCD
         l    OLED
      -x      high resolution
    """
    instruments, modes = InstrumentService.parse_help(help_text)
    check(results, "spotread 仪器解析已脱离 UI", instruments == [["1", "Meter A"], ["2", "Meter B"]])
    check(results, "spotread 模式解析已脱离 UI", modes == [["c", "LCD"], ["l", "OLED"]])
    args = InstrumentService.build_args(
        ["Meter A", "Meter B"], ["1", "2"], "Meter B",
        ["LCD", "OLED"], ["c", "l"], "OLED",
    )
    check(results, "spotread 参数构造只使用普通字符串", args == ["-x", "-e", "-c", "2", "-y", "l"])

    backend.begin_calibration(make_request())
    calls = []
    backend.run_calibration_pipeline(
        lambda: calls.append("gamut_before"),
        lambda: calls.append("pq"),
        lambda: calls.append("chromaticity"),
        lambda: calls.append("gamut_after"),
    )
    check(
        results,
        "校准管线顺序由 backend 统一管理",
        calls == ["gamut_before", "pq", "chromaticity", "gamut_after"],
    )

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    writer, reader = FakeProcess(), FakeProcess()
    backend.state.proc_color_write = writer
    backend.state.proc_color_reader = reader
    backend.processes.cleanup_measurement_processes()
    check(results, "外部测量进程由 backend 统一清理", writer.terminated and reader.terminated)
    check(results, "进程句柄清理后置空", backend.state.proc_color_write is None and backend.state.proc_color_reader is None)

    created_writer, created_reader = backend.processes.start_measurement(
        ["-x", "-e"], lambda: FakeProcess(), lambda args: FakeProcess()
    )
    check(
        results,
        "测量进程创建已移出 UI",
        created_writer is backend.state.proc_color_write
        and created_reader is backend.state.proc_color_reader,
    )
    backend.processes.cleanup_measurement_processes()

    output = os.path.join(tempfile.gettempdir(), "rwhc_backend_boundary.icc")
    backend.save_profile(output, "backend boundary")
    check(results, "ICC 可由 backend 独立保存", os.path.isfile(output) and os.path.getsize(output) > 0)

    passed = sum(results)
    print(f"\n==== 结果：{passed}/{len(results)} 项通过 ====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
