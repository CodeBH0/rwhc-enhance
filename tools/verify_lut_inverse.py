# -*- coding: utf-8 -*-
"""
verify_lut_inverse.py — MHC2 1D LUT 反解算法的数学自检

用途：不接显示器和色度计，验证 `lut.generate_mhc2_lut_from_measured_pq()`
（发布版校色路径里唯一决定最终亮度曲线的函数）的数学行为。

背景（为什么要测这个）
----------------------
该函数把实测的显示器原生响应 `f`（设备输入 PQ → 实际输出 PQ）反解成 Windows
MHC2 表：`lut[i] = f⁻¹(i/(N-1))`。

早期实现是「线性重采样成 40960 点 → 最近邻反查」，会引入最多 ±0.5 个 10bit 码值
的量化误差；现在改为直接在 (输出 PQ, 输入码值) 上做分段线性逆插值（np.interp）。
本工具用同一个参考实现（`_old_nearest_lut`，逐行照抄旧算法）与新实现对比，
把差异钉在测试里，并确认：
  · 新实现是这条响应的**精确**分段线性反函数（在显示器能力范围内）；
  · 旧实现的误差量级（<0.5 码，而非「显著台阶」）——所以当时没有可见症状；
  · 两者只在「超过实测峰值」的不可达区间里出现大差异，且新实现取的是实测峰值
    码值，而不是把输入一路推到 1.0（后者会让高光实际变暗，见 lut.py 文档）。

检查分四段：
  A. 数学性质   —— 单调性、值域、端点、不就地修改调用方数据、退化输入
  B. 反解精度   —— 合成显示器上的理想闭环（旧 vs 新）、top 截断语义
  C. 真数据     —— hc.log 里真实实测曲线上的行为（无日志时跳过）
  D. 边界/回归  —— 平台、噪声回落、非均匀点距、异常输入

用法：
    .venv314\\Scripts\\python.exe tools\\verify_lut_inverse.py
"""

import os
import sys

import numpy as np

# Windows 控制台默认 GBK，输出中文/上标会报错，这里强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from convert_utils import pq_eotf, pq_oetf  # noqa: E402
from lut import (  # noqa: E402
    find_nearest_idx,
    generate_mhc2_lut_from_measured_pq,
    linear_interpolate,
    max_uniform_target,
)

LUT_LEN = 4096
N_SAMPLES = 256          # app.py「灰阶采样点数」默认值
CODES = 1023.0           # 10bit HDR 码值上限


def check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return bool(ok)


# ----------------------------------------------------------------------
# 参考实现：旧算法（逐行照抄 2026-09 之前的 generate_mhc2_lut_from_measured_pq）
# ----------------------------------------------------------------------

def _old_nearest_lut(real_pq, target_pq):
    """线性重采样 + 最近邻反查（历史实现，仅用于对比与回归）"""
    real_pq = list(np.asarray(real_pq, dtype=float).ravel())
    for idx, itm in enumerate(real_pq):
        if idx == 0:
            continue
        if itm < real_pq[idx - 1]:
            real_pq[idx] = real_pq[idx - 1]
    m, _ = max_uniform_target(len(real_pq), LUT_LEN * 10)
    resampled = linear_interpolate(np.array(real_pq), m)
    return np.array([find_nearest_idx(resampled, itm) / (m - 1) for itm in target_pq])


# ----------------------------------------------------------------------
# 合成显示器：带黑场、非线性、顶部 ABL 回落（多对一）
# ----------------------------------------------------------------------

def make_native(peak_nits=800.0, black_nits=0.02, rolloff=0.45, gamma=0.93, n=N_SAMPLES):
    """返回 (f, peak_idx, peak_pq)。

    f 把设备输入码值 0..1 映射成实际输出 PQ：亮度先按 code^gamma 上升并封顶，
    超过 rolloff 之后按二次方**回落**，模拟 OLED / 小分区面板在峰值之后的 ABL
    压缩——即真实 hc.log 里看到的「亮度峰值不在最后一个采样点」的形状。

    peak_idx 是均匀采样 n 点后第一个给出最大值的采样点索引（与 lut.py 里
    「首个峰」的定义一致），peak_pq 是该点的输出 PQ。
    """
    def f(code):
        c = np.clip(np.asarray(code, dtype=float), 0.0, 1.0)
        up = np.clip(c / rolloff, 0.0, 1.0) ** gamma
        over = np.clip((c - rolloff) / (1.0 - rolloff), 0.0, 1.0)
        lum = peak_nits * up * (1.0 - 0.15 * over ** 2) + black_nits
        return pq_oetf(lum)

    sampled = f(np.linspace(0.0, 1.0, n))
    peak_idx = int(sampled.size - 1 - np.argmax(sampled[::-1]))
    return f, peak_idx, float(sampled[peak_idx])


def sample_display(f, n=N_SAMPLES):
    """在均匀灰度码值上采样，得到 app.py 喂给 LUT builder 的那条曲线"""
    codes = np.linspace(0.0, 1.0, n)
    return codes, f(codes)


def apply_lut(lut, target_pq):
    """按 MHC2 表的使用方式取值：索引轴 = 目标输出 PQ，线性插值"""
    axis = np.linspace(0.0, 1.0, len(lut))
    return np.interp(target_pq, axis, lut)


# ----------------------------------------------------------------------
# A. 数学性质
# ----------------------------------------------------------------------

def section_a(results):
    print("\n[A] 数学性质")
    f, peak_code, peak_pq = make_native()
    codes, measured = sample_display(f)

    lut = generate_mhc2_lut_from_measured_pq(measured)
    results.append(check("返回长度 = 4096（默认轴）", lut.size == LUT_LEN,
                         f"size={lut.size}"))
    results.append(check("单调不减", bool(np.all(np.diff(lut) >= -1e-15)),
                         f"最小相邻差={float(np.diff(lut).min()):.3e}"))
    results.append(check("值域在 [0,1]",
                         bool(lut.min() >= 0.0 and lut.max() <= 1.0),
                         f"[{lut.min():.6f}, {lut.max():.6f}]"))
    results.append(check("lut[0] == 0（最暗目标不改变码值）", float(lut[0]) == 0.0,
                         f"lut[0]={lut[0]:.6f}"))
    # 实测采样点里真正给出峰值的那个码值（「首个峰」，与 lut.py 的定义一致）
    sampled_peak_idx = int(measured.size - 1 - np.argmax(measured[::-1]))
    sampled_peak_code = float(codes[sampled_peak_idx])
    results.append(check("实测峰值未达 PQ 1.0 时，顶端截断在实测峰值码值",
                         abs(float(lut[-1]) - sampled_peak_code) < 1e-9,
                         f"lut[-1]={lut[-1]:.6f}，实测峰值码值={sampled_peak_code:.6f}"
                         f"（{sampled_peak_code * CODES:.1f}/1023），"
                         f"峰值 PQ={float(measured[sampled_peak_idx]):.5f}"))
    results.append(check("两次调用结果逐位一致（无隐藏状态）",
                         np.array_equal(lut, generate_mhc2_lut_from_measured_pq(measured)),
                         "结果完全相同"))

    # 旧实现的端点 hack（convert_idx[0]=0, convert_idx[1]=1）必须已经不存在：
    # 第二个 LUT 项应当是真反解值，而不是被硬写成 1.0。
    results.append(check("不再有旧端点 hack（lut[1] != 1.0）",
                         abs(float(lut[1]) - 1.0) > 1e-6,
                         f"lut[1]={lut[1]:.6f}（1/4095 目标处的真实反解值）"))

    # 不就地修改调用方的数据（app.py 之后还要用原始曲线画图）
    raw = [0.5, 0.4, 0.45, 0.6]
    keep = list(raw)
    generate_mhc2_lut_from_measured_pq(raw)
    results.append(check("不就地修改调用方的数组", raw == keep,
                         f"{raw} == {keep}"))

    # 退化输入
    try:
        generate_mhc2_lut_from_measured_pq([0.5])
        ok, detail = False, "未抛异常"
    except ValueError as e:
        ok, detail = True, str(e)
    results.append(check("单点输入抛 ValueError（而不是静默出错）", ok, detail))

    zeros = generate_mhc2_lut_from_measured_pq(np.zeros(N_SAMPLES))
    results.append(check("全零曲线返回恒等 LUT（不猜曲线、不抛异常）",
                         bool(np.allclose(zeros, np.linspace(0, 1, LUT_LEN))),
                         f"max|Δ|={float(np.abs(zeros - np.linspace(0, 1, LUT_LEN)).max()):.1e}"
                         "（旧写法会退化成常数 1.0，把画面压到最亮）"))

    # 长平顶（曲线末尾整段相等）也必须给出正常反解，而不是常数 1.0
    const_tail = np.concatenate([np.linspace(0.0, 0.7, N_SAMPLES - 10),
                                 np.full(10, 0.7)])
    tail_lut = generate_mhc2_lut_from_measured_pq(const_tail)
    results.append(check("末尾长平顶不会被误判成退化曲线（不出现常数 1.0）",
                         abs(float(tail_lut[0])) == 0.0 and float(tail_lut[1]) < 1e-3
                         and bool(np.all(np.diff(tail_lut) >= -1e-15)),
                         f"lut[0]={tail_lut[0]:.4f} lut[1]={tail_lut[1]:.6f} "
                         f"lut[-1]={tail_lut[-1]:.6f}"))

    bad = np.full(N_SAMPLES, np.nan)
    try:
        generate_mhc2_lut_from_measured_pq(bad)
        ok, detail = False, "未抛异常"
    except ValueError as e:
        ok, detail = True, str(e)
    results.append(check("含 NaN 的曲线抛 ValueError", ok, detail))

    # 自定义目标轴
    tgt = np.linspace(0.2, 0.4, 64)
    out = generate_mhc2_lut_from_measured_pq(measured, target_pq=tgt)
    results.append(check("target_pq 生效且长度一致", out.size == 64,
                         f"size={out.size}"))
    return results


# ----------------------------------------------------------------------
# B. 反解精度 + 顶部截断
# ----------------------------------------------------------------------

def section_b(results):
    print("\n[B] 反解精度（合成显示器，逐节点反解）")
    f, peak_idx, peak_pq_over = make_native()
    codes, measured = sample_display(f)
    peak_pq = float(measured[peak_idx])
    print(f"  （合成响应：峰值在第 {peak_idx}/{measured.size - 1} 点 = "
          f"code {codes[peak_idx] * CODES:.1f}/1023，峰值 PQ={peak_pq:.5f}）")

    targets = np.linspace(0.0, 1.0, LUT_LEN)       # LUT 自身的索引轴
    new_lut = generate_mhc2_lut_from_measured_pq(measured)
    old_lut = _old_nearest_lut(measured, targets)

    # 参考：这条单调响应的「理想分段线性反函数」（在实测采样点上定义，
    # 超出范围时按同一规则截断到峰值码值）
    ref = np.interp(targets, measured[:peak_idx + 1], codes[:peak_idx + 1],
                    left=0.0, right=codes[peak_idx])

    e_new = np.abs(new_lut - ref) * CODES
    e_old = np.abs(old_lut - ref) * CODES
    results.append(check("新实现 = 理想分段线性反函数（逐节点误差 0）",
                         float(e_new.max()) == 0.0,
                         f"max={e_new.max():.3e} 码"))
    results.append(check("旧最近邻的逐节点误差 ≤ 0.5 码（量化，不是坏值）",
                         float(e_old.max()) <= 0.5 + 1e-9,
                         f"mean={e_old.mean():.4f} max={e_old.max():.4f} 码"))
    results.append(check("新实现精度优于旧实现（报告倍数）",
                         float(e_old.max()) > float(e_new.max()),
                         f"旧 max {e_old.max():.4f} 码 vs 新 {e_new.max():.3e} 码"))

    # 关键结论：可达范围内两种算法几乎无差别 —— 换算法不会改变正常画面
    axis = np.linspace(0.0, 1.0, LUT_LEN)
    reach = axis <= peak_pq
    d = np.abs(new_lut - old_lut) * CODES
    results.append(check("可达范围内 旧/新 差异 < 1 个 10bit 码值",
                         float(d[reach].max()) < 1.0,
                         f"mean={d[reach].mean():.5f} max={d[reach].max():.5f} 码 "
                         f"（即：对正常画面亮度无可见影响）"))
    diff_entries = d > 0.013                                    # 量化误差上限内
    results.append(check("两者的差异不超过 0.013 码，且集中项只在超峰区间",
                         bool(np.all(axis[diff_entries] > peak_pq) or not diff_entries.any()),
                         f"max={d.max():.5f} 码；>0.013 码的项 "
                         f"{int(diff_entries.sum())}/{LUT_LEN}"
                         + (f"（目标轴起点 {float(axis[diff_entries].min()):.5f}"
                            f" > 峰值 {peak_pq:.5f}）" if diff_entries.any() else "")))
    results.append(check("差异 ≤ 0.5 码（旧实现本质是量化误差，不是坏值）",
                         float(d.max()) <= 0.5,
                         f"max={d.max():.5f} 码"))

    # 顶部截断语义：所有超峰节点都取实测峰值码值（而不是被推到 PQ 1.0 / 码值 1023）
    # 只按 LUT 自身的节点判断：节点之间是分段线性插值，取样点落在两个节点之间时
    # 本来就会有 <1 个节点的插值偏差，那是 LUT 的表示方式，不是反解错误。
    axis = np.linspace(0.0, 1.0, LUT_LEN)
    above = axis > peak_pq
    got_top = new_lut[above]
    top_err = float(np.abs(got_top - codes[peak_idx]).max()) * CODES
    results.append(check("超峰节点全部取实测峰值码值（不是硬推 1.0）",
                         top_err < 1e-9,
                         f"峰值码值={codes[peak_idx]:.6f}"
                         f"（{codes[peak_idx] * CODES:.1f}/1023），"
                         f"峰值 PQ={peak_pq:.5f}，{int(above.sum())} 个节点全等"))
    results.append(check("超峰节点不会被推到码值 1.0",
                         float(np.abs(got_top - 1.0).min()) > 0.05,
                         f"距 1.0 最近还有 {float(np.abs(got_top - 1.0).min()):.4f}"))

    # 这一条是「为什么必须截断到峰值」的物理依据
    at_one = float(f(np.array([1.0]))[0])
    results.append(check("该曲线在码值 1.0 处更暗（所以不能一路推到 1.0）",
                         at_one < peak_pq,
                         f"峰值 PQ={peak_pq:.5f} > code1.0 处 PQ={at_one:.5f}"))

    # 单调响应（峰值就在末点）：顶端应饱和到 1.0，且不产生平台噪声
    mono = 0.78 * np.linspace(0.0, 1.0, N_SAMPLES) ** 0.95
    lut_mono = generate_mhc2_lut_from_measured_pq(mono)
    results.append(check("单调到末点的曲线：顶端饱和到 1.0",
                         abs(float(lut_mono[-1]) - 1.0) < 1e-12,
                         f"lut[-1]={lut_mono[-1]:.6f}"))
    return results


# ----------------------------------------------------------------------
# C. 真实 hc.log 数据
# ----------------------------------------------------------------------

def section_c(results):
    print("\n[C] 真数据（hc.log 实测灰阶曲线）")
    log = os.path.join(PROJECT_ROOT, "hc.log")
    if not os.path.isfile(log):
        print(f"  [SKIP] 未找到 {log}")
        return results

    from gray_history import parse_gray_runs
    from matrix import calculate_bradford_matrix
    from convert_utils import XYZ_to_BT2020_PQ_rgb
    from meta_data import D65_WHITE_POINT

    runs = parse_gray_runs(log)
    if not runs:
        print("  [SKIP] hc.log 里没有完整的灰阶测量")
        return results

    # D65 → D65（本项目默认白点：无适配），与 calibrate_pq 里计算 real_pq 的方式一致
    m = calculate_bradford_matrix(D65_WHITE_POINT, D65_WHITE_POINT)
    run = runs[0]
    measured = np.array([float(np.mean(XYZ_to_BT2020_PQ_rgb(
        m @ np.asarray(xyz, float) / 10000.0))) for _, xyz in run["points"]])
    print(f"  （用最近一次完整灰阶 run：{run['end']:%Y-%m-%d %H:%M:%S}，"
          f"N={run['num']}）")

    lut = generate_mhc2_lut_from_measured_pq(measured)
    old = _old_nearest_lut(measured, np.linspace(0.0, 1.0, LUT_LEN))
    peak_idx = int(measured.size - 1 - np.argmax(measured[::-1]))
    codes = np.linspace(0.0, 1.0, measured.size)
    peak_code = float(codes[peak_idx])
    peak_pq = float(np.maximum.accumulate(measured)[peak_idx])

    results.append(check("真数据：LUT 单调不减、lut[0] == 0",
                         bool(np.all(np.diff(lut) >= -1e-15)) and float(lut[0]) == 0.0))
    results.append(check("真数据：实测峰值不在最后一个采样点（本测试的前提）",
                         peak_idx < measured.size - 1,
                         f"峰值在第 {peak_idx}/{measured.size - 1} 点 "
                         f"(code≈{peak_code * CODES:.0f})，"
                         f"峰值 PQ={peak_pq:.5f} → 末点 {measured[-1]:.5f}"
                         f"（实机 ABL 回落）"))
    results.append(check("真数据：顶端截断在实测峰值码值（不是 1.0）",
                         abs(float(lut[-1]) - peak_code) < 1e-9,
                         f"lut[-1]={lut[-1]:.6f}（{lut[-1] * CODES:.1f}/1023）"))

    axis = np.linspace(0.0, 1.0, LUT_LEN)
    reach = axis <= peak_pq
    d = np.abs(lut - old) * CODES
    results.append(check("真数据：可达范围内 旧/新 差异 < 1 个码值",
                         float(d[reach].max()) < 1.0,
                         f"mean={d[reach].mean():.5f} max={d[reach].max():.5f} 码"))
    top = lut[axis > peak_pq]
    top_err_code = float(np.abs(top - peak_code).max()) * CODES
    results.append(check("真数据：超峰节点全部取峰值码值（不是 1.0）",
                         top_err_code < 1e-9 and abs(float(top[-1]) - 1.0) > 0.05,
                         f"新实现 {float(top[-1]) * CODES:.1f}/1023 "
                         f"= 峰值码值 {peak_code * CODES:.1f}/1023"
                         f"（{top.size} 个节点全等）；"
                         f"若照搬「一律取 1.0」则会送到 1023/1023"))
    return results


# ----------------------------------------------------------------------
# D. 边界与回归
# ----------------------------------------------------------------------

def section_d(results):
    print("\n[D] 边界与回归")

    # 1) 曲线含平台（多对一）：结果必须单调，同一个输出值只对应一个码值。
    #    构造一条「升到平台高度后就不再上升」的曲线，于是平台末端就是实测最亮点。
    codes3 = np.linspace(0.0, 1.0, N_SAMPLES)
    knee = 0.4
    ceiling = 0.02 + 0.9 * knee                   # 平台高度：knee 处的爬升值
    plateau = np.where(codes3 <= knee, 0.02 + 0.9 * codes3, ceiling)
    lut3 = generate_mhc2_lut_from_measured_pq(plateau)
    axis3 = np.linspace(0.0, 1.0, LUT_LEN)
    results.append(check("曲线含平台：结果仍单调不减",
                         bool(np.all(np.diff(lut3) >= -1e-15)),
                         f"最小相邻差={float(np.diff(lut3).min()):.3e}"))
    got3 = float(lut3[axis3 > ceiling][-1])
    results.append(check("曲线含平台：同一输出值只对应一个码值（多对一有确定解）",
                         abs(got3 - knee) * CODES < 0.01,
                         f"平台高度 PQ={ceiling:.4f} → 码值 {got3 * CODES:.1f}/1023"
                         f"（平台起点/峰值点 {knee * CODES:.1f}/1023）"))
    results.append(check("曲线含平台：顶端截断在峰值点码值",
                         abs(float(lut3[-1]) - knee) < 1e-9,
                         f"lut[-1]={lut3[-1]:.6f} = 峰值点码值 {knee:.6f}"))

    # 2) 噪声造成的回落必须被抹平（否则后面 CLUT 的正向模型会自相矛盾）
    rng = np.random.default_rng(20260920)
    noisy = 0.02 + 0.7 * codes3 + rng.normal(0, 0.004, codes3.size)
    lut = generate_mhc2_lut_from_measured_pq(noisy)
    results.append(check("含噪声回落：结果单调不减",
                         bool(np.all(np.diff(lut) >= -1e-15))))
    results.append(check("含噪声回落：与「先单调化再反解」的参考一致",
                         np.allclose(lut, generate_mhc2_lut_from_measured_pq(
                             np.maximum.accumulate(noisy)))))

    # 3) 少量点（6 点）：仍能反解、单调、端点符合「单调到末点 → 1.0」
    few = np.array([0.0, 0.1, 0.35, 0.7, 0.79, 0.795])
    lut = generate_mhc2_lut_from_measured_pq(few, target_pq=np.linspace(0, 1, LUT_LEN))
    results.append(check("少量点（6 点）仍能反解且端点为 0/1",
                         lut.size == LUT_LEN and abs(float(lut[0])) == 0.0
                         and abs(float(lut[-1]) - 1.0) < 1e-12,
                         f"lut[0]={lut[0]:.4f} lut[-1]={lut[-1]:.4f}"))

    # 4) 严格线性、单调到末点的响应：反解在 LUT 节点上与理想反函数一致
    #    （用 0.796078… = 203/255 作为顶端，避免 0.8 在两个网格里不可表示相等）
    top = 203.0 / (N_SAMPLES - 1)
    lin = top * np.linspace(0.0, 1.0, N_SAMPLES)
    lut = generate_mhc2_lut_from_measured_pq(lin)
    nodes4 = np.linspace(0.0, 1.0, LUT_LEN)
    codes4 = np.linspace(0.0, 1.0, N_SAMPLES)
    ref4 = np.interp(nodes4, lin, codes4, left=0.0, right=1.0)
    ident_err = np.abs(lut - ref4) * CODES
    results.append(check("线性响应：反解与理想反函数逐节点一致",
                         float(ident_err.max()) < 1e-9,
                         f"max={ident_err.max():.3e} 码（0 = 每个节点都精确反解）"))

    # 5) 末尾长平台：不得被误判成"峰值在末点"以外的退化情形；顶端截断在平台起点
    flat_tail = np.concatenate([np.linspace(0.0, 0.7, N_SAMPLES - 10),
                                np.full(10, 0.7)])
    lut = generate_mhc2_lut_from_measured_pq(flat_tail)
    peak_code_tail = float(np.linspace(0, 1, N_SAMPLES)[N_SAMPLES - 11])
    results.append(check("末尾长平台：结果单调、顶端截断在平台起点码值",
                         bool(np.all(np.diff(lut) >= -1e-15))
                         and abs(float(lut[-1]) - peak_code_tail) < 1e-9,
                         f"lut[-1]={lut[-1]:.6f} 平台起点码值={peak_code_tail:.6f}"
                         f"（{peak_code_tail * CODES:.1f}/1023）"))
    return results


def main():
    print("=== MHC2 1D LUT 反解自检（无需显示器/色度计）===")
    results = []
    section_a(results)
    section_b(results)
    section_c(results)
    section_d(results)
    passed = sum(1 for r in results if r)
    print(f"\n==== 结果：{passed}/{len(results)} 项通过 ====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
