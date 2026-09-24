# -*- coding: utf-8 -*-
"""
verify_color_history.py — 验证「历史颜色数据」复用链路

不需要显示器 / 色度计，也不启动 GUI：

  A. `color_history.parse_color_runs` 对真实 `hc.log` 的解析
     （完整 run 的识别、六个色域点、激活黑、色卡样本、字节级一致性）
  B. `DisplayModel` 的实测色卡校验（`color_accuracy_report`）数学正确性
     （合成数据：完全一致 → 误差 0；加入抖动 → 误差量级正确）
  C. `calibration_backend.py` 的复用集成路径：
     `measured_color_samples` / `make_display_model`
     以及复用历史原色后的 CLUT 生成是否与直接构造的模型一致

用法：
    .venv314\\Scripts\\python.exe tools\\verify_color_history.py
    .venv314\\Scripts\\python.exe tools\\verify_color_history.py --log hc.log
"""

import logging
import os
import sys

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import color_history as ch  # noqa: E402
from clut_icc import (  # noqa: E402
    build_model_from_calibration,
    make_clut_tags,
    tetrahedral_interp,
    CLUT_FORMAT_MFT2,
)
from gray_history import parse_gray_runs  # noqa: E402
from matrix import build_rgb_to_xyz_from_primaries  # noqa: E402
from meta_data import D65_WHITE_POINT  # noqa: E402
from tools.verify_clut_profile import make_synthetic_mhc2  # noqa: E402


def check(label, ok, detail=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               (" — " + detail) if detail else ""))
    return bool(ok)


# ======================================================================
# A. 真实日志解析
# ======================================================================

def test_parser(log_path):
    results = []
    print("\n=== A. hc.log 历史颜色数据解析 ===")
    if not os.path.isfile(log_path):
        print("  (跳过：找不到 {})".format(log_path))
        return results, []

    runs = ch.parse_color_runs(log_path)
    results.append(check("解析到完整 run", len(runs) > 0, "{} 条".format(len(runs))))
    if not runs:
        return results, []

    # 1) 每个 run 结构完整
    bad_keys = [i for i, r in enumerate(runs)
                if any(r["gamut"].get(k) is None for k in ch.GAMUT_KEYS)]
    results.append(check("六个色域点齐全", not bad_keys, "缺失 run: {}".format(bad_keys)))

    bad_pts = [i for i, r in enumerate(runs) if len(r["points"]) == 0]
    results.append(check("色卡样本非空", not bad_pts, "空 run: {}".format(bad_pts)))

    # 2) 按结束时间倒序
    ends = [r["end"] for r in runs]
    results.append(check("按测量结束时间倒序", ends == sorted(ends, reverse=True)))

    # 3) 时间单调性：run 内部 start <= end
    bad_time = [i for i, r in enumerate(runs) if r["start"] > r["end"]]
    results.append(check("run 内 start <= end", not bad_time, "异常: {}".format(bad_time)))

    # 4) 数值合理性
    ok_values = True
    detail = ""
    for r in runs:
        conv = ch.gamut_xyz_from_run(r)
        w = np.asarray(conv["white"], float)
        b = np.asarray(conv["black"], float)
        wp = np.asarray(conv["white_paper"], float)
        if not (wp[1] > 0 and w[1] > 0 and wp[1] < w[1] and b[1] < wp[1]):
            ok_values = False
            detail = "white={:.1f}, paper={:.1f}, black={:.4f}".format(w[1], wp[1], b[1])
            break
    results.append(check("黑 < 纸白 < 峰值白（亮度关系合理）", ok_values, detail))

    # 5) 色卡样本：目标 XYZ 与实测 XYZ 均为三元、且量级合理（<=10000 nit）
    shape_ok = True
    mag_ok = True
    worst = 0.0
    for r in runs:
        for _i, rgb, tgt, meas in r["points"]:
            if rgb.size != 3 or tgt.size != 3 or meas.size != 3:
                shape_ok = False
            worst = max(worst, float(np.max(np.abs(meas))) * 10000.0)
    mag_ok = worst < 10000.0
    results.append(check("色卡样本三元组完整", shape_ok))
    results.append(check("色卡实测 XYZ 量级合理（< 10000 nit）", mag_ok,
                         "最大分量 {:.1f} nit".format(worst)))

    # 6) 激活黑：要么给出行，要么回退到 black（与 app 的实时路径一致）
    mab_ok = all(ch.gamut_xyz_from_run(r)["min_activated_black"].size == 3 for r in runs)
    have_mab = sum(1 for r in runs if r["min_activated_black"] is not None)
    results.append(check("min_activated_black 有值（或回退到 black）", mab_ok,
                         "{}/{} 条含二分搜索行".format(have_mab, len(runs))))

    # 7) 与灰阶历史共用日期推断：两者对同一文件的时间基准应一致
    grays = parse_gray_runs(log_path)
    if grays:
        g_end = max(g["end"] for g in grays)
        c_end = max(r["end"] for r in runs)
        results.append(check("色卡时间基准与灰阶历史一致（相差 < 1 天）",
                             abs((g_end - c_end).total_seconds()) < 86400,
                             "gray {} / color {}".format(g_end, c_end)))
    else:
        print("  (hc.log 中没有灰阶 run，跳过时间基准对照)")

    # 8) 复用的数据必须与「实测」语义一致：直接再算一遍
    r0 = runs[0]
    gam = ch.gamut_xyz_from_run(r0)
    max_lumi, min_lumi = ch.peak_min_luminance(r0)
    results.append(check("peak/min 亮度取自 white/black 的 Y",
                         abs(max_lumi - float(r0["gamut"]["white"][1])) < 1e-9
                         and abs(min_lumi - float(r0["gamut"]["black"][1])) < 1e-9,
                         "peak {:.2f} / min {:.4f} nit".format(max_lumi, min_lumi)))

    # EETF 规则与 app 一致（历史数据 + EETF 情况下 peak 用 monitor_max、black 归 0）
    e_max, e_min = ch.peak_min_luminance(
        r0, eetf=True, eetf_args={"monitor_max": 800.0, "monitor_min": 0.05})
    results.append(check("EETF 参数生效（monitor_max/min）",
                         abs(e_max - 800.0) < 1e-9 and e_min == 0.0,
                         "peak {} / min {}".format(e_max, e_min)))

    # 9) 标签与摘要可用
    label = ch.run_label(r0)
    summary = ch.run_summary(r0)
    results.append(check("run_label / run_summary 非空且含色卡数量",
                         bool(label) and bool(summary) and str(len(r0["points"])) in summary,
                         label))
    return results, runs


# ======================================================================
# B. 实测色卡校验的数学正确性
# ======================================================================

def make_synth(peak=1000.0):
    mhc2 = make_synthetic_mhc2(peak_nits=peak)
    M = build_rgb_to_xyz_from_primaries((0.680, 0.320), (0.265, 0.690),
                                        (0.150, 0.060), D65_WHITE_POINT)
    scale = peak / float(M[1].sum())
    gam = {k: list(M[:, i] * scale) for i, k in enumerate(("red", "green", "blue"))}
    gam["white_paper"] = list(M.sum(axis=1) * scale)
    model = build_model_from_calibration(
        mhc2, gam["red"], gam["green"], gam["blue"], gam["white_paper"], peak_nits=peak)
    return mhc2, gam, model


def test_accuracy_math():
    results = []
    print("\n=== B. DisplayModel 实测色卡校验（合成显示器）===")
    peak = 1000.0
    mhc2, gam, model = make_synth(peak)
    rng = np.random.default_rng(7)
    dev = rng.random((25, 3))
    meas = np.asarray(model.device_to_xyz(dev))

    axis = np.linspace(0.0, 1.0, model.lut_size)
    # 由 MHC2 反解设备码值（与真实复用场景一致：只从「输出 PQ」推出输入码值）
    out_pq = np.stack([np.interp(dev[:, i], axis, np.asarray(model.luts[i]))
                       for i in range(3)], axis=1)
    dev_back = np.stack([np.interp(out_pq[:, i], np.asarray(model.luts[i]), axis)
                         for i in range(3)], axis=1)
    results.append(check("由输出 PQ 反解设备码值可复现（误差 < 1e-6）",
                         float(np.abs(dev_back - dev).max()) < 1e-6,
                         "max {:.2e}".format(float(np.abs(dev_back - dev).max()))))

    perfect = build_model_from_calibration(
        mhc2, gam["red"], gam["green"], gam["blue"], gam["white_paper"], peak_nits=peak,
        measured_colors=[(dev_back[i], meas[i]) for i in range(25)])
    rep = perfect.color_accuracy_report()
    results.append(check("完全一致时 ΔE ITP 为 0",
                         rep["mean_de_itp"] < 1e-9 and rep["max_de_itp"] < 1e-9,
                         "mean {:.2e}".format(rep["mean_de_itp"])))
    results.append(check("n 与样本数一致", rep["n"] == 25, str(rep["n"])))

    jit = np.clip(dev_back + rng.normal(0.0, 0.002, dev_back.shape), 0.0, 1.0)
    jmodel = build_model_from_calibration(
        mhc2, gam["red"], gam["green"], gam["blue"], gam["white_paper"], peak_nits=peak,
        measured_colors=[(jit[i], meas[i]) for i in range(25)])
    jrep = jmodel.color_accuracy_report()
    results.append(check("码值抖动 0.002 时 ΔE ITP 量级正确（0 < mean < 2）",
                         0.0 < jrep["mean_de_itp"] < 2.0,
                         "mean {:.4f}, max {:.4f}".format(jrep["mean_de_itp"], jrep["max_de_itp"])))
    results.append(check("抖动时 mean |ΔXYZ| 为亚 nit 量级（< 1 nit）",
                         jrep["mean_d_xyz_nits"] < 1.0,
                         "{:.4f} nit".format(jrep["mean_d_xyz_nits"])))

    # 原色被改动 → 校验必须报错（否则这个指标没有诊断价值）。
    # 注意：`primaries_matrix` 会把矩阵按 Y 行归一，因此单纯缩放某个原色的
    # 亮度会被完全吸收（预测不变）——必须扰动**色度**才能真正改变模型。
    pert_xy = ((0.680, 0.320), (0.265, 0.720), (0.150, 0.060))
    Mp = build_rgb_to_xyz_from_primaries(*pert_xy, D65_WHITE_POINT)
    sp = peak / float(Mp[1].sum())
    pert = {
        "red": list(Mp[:, 0] * sp),
        "green": list(Mp[:, 1] * sp),
        "blue": list(Mp[:, 2] * sp),
        "white_paper": list(Mp.sum(axis=1) * sp),
    }
    pert_model = build_model_from_calibration(
        mhc2, pert["red"], pert["green"], pert["blue"], pert["white_paper"], peak_nits=peak)
    pert_meas = np.asarray(pert_model.device_to_xyz(dev))
    pmodel = build_model_from_calibration(
        mhc2, gam["red"], gam["green"], gam["blue"], gam["white_paper"], peak_nits=peak,
        measured_colors=[(dev[i], pert_meas[i]) for i in range(25)])
    prep = pmodel.color_accuracy_report()
    results.append(check("原色色度被扰动 5% 时校验能报出明显偏差",
                         prep["mean_de_itp"] > 1.0,
                         "mean {:.2f}, max {:.2f}".format(prep["mean_de_itp"], prep["max_de_itp"])))

    # 没有色卡数据时不应报错，返回 None
    bare = build_model_from_calibration(
        mhc2, gam["red"], gam["green"], gam["blue"], gam["white_paper"], peak_nits=peak)
    results.append(check("无实测色卡时 report/summary 均为 None",
                         bare.color_accuracy_report() is None
                         and bare.color_accuracy_summary() is None))
    return results


# ======================================================================
# C. calibration_backend 集成路径
# ======================================================================


def build_fake_app(run, gam, peak, black):
    """构造只带 DisplayModel 所需状态的 backend（色卡来自历史 run）。"""
    card = []
    for _idx, rgb, _tgt, meas in run["points"]:
        card.append(((np.asarray(rgb, float) / 1023.0).tolist(),
                     (np.asarray(meas, float) * 10000.0).tolist()))
    from calibration_backend import CalibrationBackend
    backend = CalibrationBackend(base_dir=PROJECT_ROOT)
    backend.state.MHC2 = {
        "red_lut": [0.0, 1.0],
        "green_lut": [0.0, 1.0],
        "blue_lut": [0.0, 1.0],
        "peak_luminance": peak,
        "min_luminance": black,
    }
    backend.state.measure_gamut_xyz = gam
    backend.state.measured_pq = None
    backend.state.reused_color_card = card
    backend.state.measured_xyz = [
        list(np.asarray(m, float)) for _i, _r, _t, m in run["points"]
    ]
    backend.state.target_xyz = [
        list(np.asarray(t, float)) for _i, _r, t, _m in run["points"]
    ]
    return backend


def test_app_integration(runs):
    results = []
    print("\n=== C. calibration_backend 复用集成 ===")
    if not runs:
        print("  (跳过：没有可用的历史颜色 run)")
        return results

    run = runs[0]
    gam = ch.gamut_xyz_from_run(run)
    peak, black = ch.peak_min_luminance(run)

    fake = build_fake_app(run, gam, peak, black)
    samples = fake.measured_color_samples()
    results.append(check("_measured_color_samples 返回全部色卡样本",
                         samples is not None and len(samples) == len(run["points"]),
                         "{} 个".format(len(samples) if samples else 0)))
    dev0, xyz0 = samples[0]
    _i0, rgb0, _t0, meas0 = run["points"][0]
    results.append(check("样本码值 = 记录 RGB/1023",
                         np.allclose(dev0, np.asarray(rgb0, float) / 1023.0),
                         "{} vs {}".format(np.round(dev0, 4).tolist(),
                                           np.round(np.asarray(rgb0, float) / 1023.0, 4).tolist())))
    results.append(check("样本 XYZ 单位 = nit（记录值 × 10000）",
                         np.allclose(xyz0, np.asarray(meas0, float) * 10000.0),
                         "{:.2f} nit".format(float(xyz0[1]))))

    # MHC2 用合成表（形状正确、可逆）：这里验证的是「复用数据能否走通 CLUT 链路」，
    # 真实 MHC2 只能来自一次真实校准，无法在无显示器环境下复现。
    # 峰值亮度用历史 run 的真实值，避免与合成表量级不一致。
    mhc2 = make_synthetic_mhc2(peak_nits=peak)
    fake.state.MHC2 = dict(mhc2)
    fake.state.MHC2["peak_luminance"] = peak
    fake.state.MHC2["min_luminance"] = black

    model = fake.make_display_model()
    results.append(check("_make_display_model 用复用数据成功构造模型",
                         model is not None,
                         "peak {:.0f} nit, lut {} 项".format(model.peak_nits, model.lut_size)))
    results.append(check("模型携带了实测色卡", model.measured_colors is not None,
                         "{} 个样本".format(0 if model.measured_colors is None
                                            else model.measured_colors[0].shape[0])))
    rep = model.color_accuracy_report()
    results.append(check("复用数据可产出校验报告",
                         rep is not None and rep["n"] == len(run["points"]),
                         "mean dE ITP {:.2f}".format(rep["mean_de_itp"]) if rep else ""))

    # 复用的原色确实进了模型矩阵（与直接构造的参考模型一致）
    ref = build_model_from_calibration(
        mhc2, gam["red"], gam["green"], gam["blue"], gam["white_paper"],
        peak_nits=peak, black_nits=black)
    results.append(check("模型矩阵与直接构造一致",
                         np.allclose(model.xyz_matrix, ref.xyz_matrix),
                         "max diff {:.2e}".format(
                             float(np.abs(model.xyz_matrix - ref.xyz_matrix).max()))))

    # CLUT 生成：复用路径产出的 A2B 与参考模型一致
    tags = make_clut_tags(model, grid=17, fmt=CLUT_FORMAT_MFT2, with_b2a=False)
    from clut_icc import parse_clut_tag
    a2b = parse_clut_tag(tags["A2B0"])
    pts = np.random.default_rng(11).random((300, 3))
    got = tetrahedral_interp(a2b["clut"], pts)
    want = np.clip(ref.device_to_pcs(pts), 0.0, 1.0)
    err = float(np.abs(got - want).max())
    results.append(check("复用数据生成的 A2B CLUT 与参考模型一致（< 2%）",
                         err < 0.02, "max {:.4f}%".format(err * 100.0)))
    results.append(check("A2B 自检误差在阈值内",
                         tags["_meta"]["a2b_interp_mean_err"] < 0.01,
                         "mean {:.4f}%".format(tags["_meta"]["a2b_interp_mean_err"] * 100.0)))
    return results


def test_history_only_mode(runs):
    """
    D. 「两个历史数据都选中 → 不接色度计也能生成 ICC」

    证明方式：把 ColorWriter / ColorReader / tk.messagebox 全部换成**一调用就抛异常**
    的替身，然后跑真实的 `app.calibrate_monitor`。只要它跑通，就说明这条路径
    完全没有碰硬件，也没有弹「放置色度计」对话框。
    """
    results = []
    print("\n=== D. 纯历史数据模式（不接色度计）===")
    if not runs:
        print("  (跳过：没有可用的历史颜色 run)")
        return results
    try:
        from gray_history import parse_gray_runs
        grays = parse_gray_runs(os.path.join(PROJECT_ROOT, "hc.log"))
    except Exception as e:  # noqa: BLE001
        print("  (跳过：灰阶历史解析失败 {})".format(e))
        return results
    if not grays:
        print("  (跳过：hc.log 中没有灰阶 run)")
        return results

    try:
        import tkinter as tk  # noqa: F401
    except Exception:  # noqa: BLE001
        print("  (跳过：无 tkinter)")
        return results

    try:
        import app as appmod
    except ModuleNotFoundError as exc:
        print("  (跳过：app 运行依赖未安装：{})".format(exc))
        return results

    class Boom(Exception):
        pass

    def boom(*a, **k):
        raise Boom("hardware/dialog was touched")

    class FakeDisplayPlatform:
        """Device-edge fake; UI and backend services still run their real paths."""

        def __init__(self):
            self.operations = []

        def enumerate_displays(self):
            return [{
                "path_index": 0,
                "adapter_luid": {"low_part": 1, "high_part": 0},
                "source": {"id": 0, "gdi_name": r"\\.\DISPLAY_TEST"},
                "target": {
                    "device_path": r"\\?\DISPLAY#TEST123#INSTANCE#{GUID}",
                    "friendly_name": "Test Display",
                    "sdr_white_level_nits": 200.0,
                    "advanced_color": {
                        "enabled": True,
                        "wide_color_enforced": False,
                    },
                },
            }]

        def monitor_rect(self, gdi_name):
            return {
                "gdi_name": gdi_name,
                "left": 0,
                "top": 0,
                "right": 1920,
                "bottom": 1080,
                "is_primary": True,
            }

        def install_profile(self, path):
            self.operations.append(("install", os.path.basename(path)))

        def add_profile_association(self, info, filename):
            self.operations.append(("associate", filename))

        def remove_profile_association(self, info, filename):
            self.operations.append(("remove_association", filename))

        def uninstall_profile(self, filename):
            self.operations.append(("uninstall", filename))

    # 这一关会真的跑一次校准并写日志。做法分两步：
    #   1) 按原样构造 UI —— 让 app.py 从**真实** hc.log 读历史数据；
    #   2) 立刻把它的 FileHandler 换成临时文件，并清掉日志文件里 app 启动时那两行。
    # 这样既用到真实历史数据，又不会给用户的 hc.log 增加内容。
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="rwhc_verify_")
    tmp_log = os.path.join(tmp_dir, "hc.log")
    real_log_path = os.path.join(PROJECT_ROOT, "hc.log")
    size_real_log = os.path.getsize(real_log_path)

    # 清掉上一次运行留下的文件日志句柄，避免越跑越多
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler):
            logging.getLogger().removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    root = None
    root_logger = logging.getLogger()
    orig_level = root_logger.level
    try:
        # 构造 UI 时 app.py 会写几行启动日志到真实 hc.log。先把日志级别抬到
        # CRITICAL 之上，这几行就不会被写出来（历史数据解析读的是文件内容，
        # 与日志级别无关，所以不受影响）。
        root_logger.setLevel(logging.CRITICAL + 1)
        root = appmod.tk.Tk()
        display_platform = FakeDisplayPlatform()
        backend = appmod.CalibrationBackend(
            base_dir=PROJECT_ROOT,
            display_platform=display_platform,
        )
        ui = appmod.HDRCalibrationUI(root, backend=backend)
        root.update_idletasks()
    except Exception as e:  # noqa: BLE001
        root_logger.setLevel(orig_level)
        print("  (跳过：无法构造 GUI：{}: {})".format(type(e).__name__, e))
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        return results

    try:
        # 把日志切到临时文件；并确保真实 hc.log 与构造前完全一致
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        root_logger.setLevel(orig_level)
        fh = logging.FileHandler(tmp_log, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        fh.setLevel(logging.DEBUG)
        root_logger.addHandler(fh)
        ui.log_path = tmp_log
        with open(real_log_path, "rb+") as f:
            f.truncate(size_real_log)

        results.append(check("校准日志已切到临时文件（不污染 hc.log）",
                             os.path.abspath(ui.log_path) == os.path.abspath(tmp_log),
                             tmp_log))

        # 硬件/对话框替身：任何触碰都会抛异常
        appmod.ColorWriter = boom
        appmod.ColorReader = boom
        appmod.tk.messagebox.askokcancel = boom
        appmod.tk.messagebox.showerror = boom

        ui.human_display_config_map[ui.monitor_var.get()]["color_work_status"] = "hdr"
        ui.clut_var.set(False)   # 这里只验证「能产出不带 CLUT 的 profile」

        grun, crun = grays[0], runs[0]
        ui.gray_history_var.set(ui._gray_run_label(grun))
        ui.on_gray_history_selected()
        ui.color_history_var.set(ui._color_run_label(crun))
        ui.on_color_history_selected()

        results.append(check("两个历史数据都已选中",
                             ui.selected_gray_run is not None
                             and ui.selected_color_run is not None))
        results.append(check("_history_only_calibration() 判定为无需仪器",
                             ui._history_only_calibration() is True))

        def sync_run(worker, on_done):
            try:
                res = worker()
            except Exception as e:  # noqa: BLE001
                res = e
            on_done(res)

        ui.run_in_thread = sync_run

        err = None
        try:
            ui.calibrate_monitor()
        except Boom as e:
            err = "hardware touched: {}".format(e)
        except Exception as e:  # noqa: BLE001
            err = "{}: {}".format(type(e).__name__, e)

        results.append(check("calibrate_monitor 在不接仪器时跑通（未触碰硬件/弹窗）",
                             err is None, err or "no hardware access"))
        operation_names = [name for name, _value in display_platform.operations]
        results.append(check("ICC 预览通过 backend display port 隔离",
                             "install" in operation_names
                             and "associate" in operation_names
                             and "remove_association" in operation_names
                             and "uninstall" in operation_names,
                             ", ".join(operation_names)))
        results.append(check("profile 原色/白点来自历史颜色数据",
                             all(ui.measure_gamut_xyz.get(k) is not None
                                 for k in ("red", "green", "blue", "white", "white_paper", "black"))
                             and ui.reused_color_run is not None))
        results.append(check("MHC2 峰值/黑场已写入",
                             ui.MHC2.get("peak_luminance") and ui.MHC2.get("min_luminance") is not None,
                             "peak {} / min {}".format(ui.MHC2.get("peak_luminance"),
                                                       ui.MHC2.get("min_luminance"))))
        results.append(check("MHC2 LUT 来自历史灰阶（长度与单调性合理）",
                             len(ui.MHC2["red_lut"]) > 16
                             and all(b >= a for a, b in zip(ui.MHC2["red_lut"],
                                                            ui.MHC2["red_lut"][1:])),
                             "{} 项".format(len(ui.MHC2["red_lut"]))))
        results.append(check("内容矩阵保持单位阵",
                             list(ui.MHC2["matrix"]) == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]))
        results.append(check("复用的色卡已挂到模型上（供 CLUT 校验）",
                             bool(ui.reused_color_card)
                             and ui._make_display_model().measured_colors is not None,
                             "{} 个样本".format(len(ui.reused_color_card or []))))

        # 真的存一份 ICC 出来
        import tempfile
        out = os.path.join(tempfile.gettempdir(), "rwhc_history_only_verify.icc")
        ui.icc_handle.rebuild()
        ui.icc_handle.save(out)
        from icc_rw import ICCProfile
        again = ICCProfile(out)
        results.append(check("产出的 ICC 可回读且含矩阵/MHC2 标签",
                             all(t in again.tags for t in ("rXYZ", "gXYZ", "bXYZ", "wtpt", "MHC2")),
                             "{} bytes, tags={}".format(os.path.getsize(out), len(again.tags))))
    finally:
        # 收尾（销毁窗口）也可能写日志：先静音再收尾，最后把真实 hc.log
        # 长度强制恢复到进入本段之前的值。
        root_logger.setLevel(logging.CRITICAL + 1)
        try:
            root.destroy()
        except Exception:
            pass
        for h in list(root_logger.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    h.flush()
                except Exception:
                    pass
        root_logger.setLevel(orig_level)
        try:
            with open(real_log_path, "rb+") as f:
                f.truncate(size_real_log)
        except Exception:
            pass
        size_after = os.path.getsize(real_log_path)
        results.append(check("hc.log 长度未被本次测试改动",
                             size_after == size_real_log,
                             "{} -> {} bytes".format(size_real_log, size_after)))
    return results


def main(log_path=None):
    log_path = log_path or os.path.join(PROJECT_ROOT, "hc.log")
    results = []
    runs = ch.parse_color_runs(log_path)
    results += test_parser(log_path)[0]
    results += test_accuracy_math()
    results += test_app_integration(runs)
    results += test_history_only_mode(runs)

    passed = sum(1 for r in results if r)
    print("\n==== 结果：{}/{} 项通过 ====".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    arg_log = None
    if "--log" in sys.argv:
        arg_log = sys.argv[sys.argv.index("--log") + 1]
    sys.exit(main(arg_log))
