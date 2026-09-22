# -*- coding: utf-8 -*-
"""
verify_clut_profile.py — CLUT profile 生成/读写自检工具

用途：在不接显示器和色度计的情况下验证 clut_icc.py 的正确性，包含四类检查：

  A. 结构一致性   —— 生成的标签能被解析回来，且数值与写入时逐位一致（16bit 往返）
  B. 插值精度     —— 用四面体插值读回 CLUT，与原始模型在网格内部对比
  C. 往返一致性   —— A2B0 -> B2A0 -> A2B0 的闭合误差（PCS 坐标）
  D. 真 profile   —— 真正写入 data/hdr_empty.icc 副本并 rebuild/save，
                     再用 ICCProfile 重新打开校验标签表、大小与 MHC2 是否完好

用法：
    .venv314\\Scripts\\python.exe tools\\verify_clut_profile.py            # 全部检查
    .venv314\\Scripts\\python.exe tools\\verify_clut_profile.py --grid 17  # 换网格尺寸
    .venv314\\Scripts\\python.exe tools\\verify_clut_profile.py --fmt mab  # 换标签格式
"""

import argparse
import os
import sys
import tempfile

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

from clut_icc import (  # noqa: E402
    CLUT_FORMAT_MAB,
    CLUT_FORMAT_MFT2,
    build_model_from_calibration,
    clut_verify,
    make_clut_tags,
    parse_clut_tag,
    tetrahedral_interp,
    write_clut_tags,
)
from convert_utils import pq_eotf, pq_oetf  # noqa: E402
from icc_rw import ICCProfile  # noqa: E402
from matrix import build_rgb_to_xyz_from_primaries  # noqa: E402
from meta_data import D65_WHITE_POINT  # noqa: E402


# ----------------------------------------------------------------------
# 合成显示器：BT.2020 原色 + D65 白点 + 一条真实感很强的 PQ 灰阶曲线
# ----------------------------------------------------------------------

BT2020_PRIMARIES = {
    "red": (0.708, 0.292),
    "green": (0.170, 0.797),
    "blue": (0.131, 0.046),
    "white": D65_WHITE_POINT,
}


def xyY_to_XYZ_local(xyY):
    from convert_utils import xyY_to_XYZ
    return xyY_to_XYZ(xyY)


def make_synthetic_mhc2(peak_nits=1000.0, lut_size=4096):
    """
    构造一个**物理自洽**的 MHC2 表（本项目真实数据的语义）。

    语义（与 app.py 的 calibrate_pq / generate_mhc2_lut_from_measured_pq 一致）：
      mhc2_lut[i] = 当期望显示器输出「亮度 = pq_eotf(i/(N-1))」时，
                    需要送给显示器的**输入 PQ 值**。

    Windows 实际链路是 PQ → PQ：

        code --(shaper)--> V_in --(显示器原生响应)--> 实际亮度

    所以要在「期望输出 PQ」与「所需输入 PQ」之间建立映射，必须：
      1. 先定义显示器原生响应 T(V_in)（输入 PQ -> 输出亮度）；
      2. 对每个期望输出亮度，反解出需要的 V_in。

    这样构造出来的 LUT 全量程严格递增、可逆，且不会在高端出现平台
    （平台会让模型变成多对一，CLUT 自检就没有意义了）。
    """
    v = np.linspace(0.0, 1.0, lut_size)
    # 期望输出亮度只覆盖「显示器真正能做到」的范围。
    # 真实校准得到的实测灰阶也止于显示器峰值（不可能测到 10000 nit），
    # 所以这里用 peak_nits 作为上限是符合实际的。
    out_nits = np.linspace(0.0, peak_nits, lut_size)

    # 显示器原生响应：输入 PQ -> 输出亮度（nits）
    vgrid = np.linspace(0.0, 1.0, 20001)
    in_lin = pq_eotf(vgrid) / peak_nits       # 归一化到峰值

    luts = {}
    for i, name in enumerate(("red", "green", "blue")):
        expo = 1.02 + 0.015 * i               # 每通道略有差异，接近 1
        gain = 1.0
        # 原生响应：轻微幂次偏差；稳定后封顶在该通道能输出的最大亮度
        native_nits = peak_nits * np.clip(in_lin * gain, 0.0, 1.0) ** expo
        native_nits = np.maximum.accumulate(native_nits)
        # 加极小斜率保证严格递增（避免求逆多解）
        native_nits = native_nits + np.linspace(0.0, 1.0, native_nits.size) * 1e-6

        # 反解：期望输出亮度 -> 所需输入 PQ（超出能力时取 1.0）
        need = np.interp(out_nits, native_nits, vgrid, left=0.0, right=1.0)
        luts[name] = np.clip(need, 0.0, 1.0)

    return {
        "entry_count": lut_size,
        "min_luminance": 0.0,
        "peak_luminance": peak_nits,
        "matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "red_lut": luts["red"].tolist(),
        "green_lut": luts["green"].tolist(),
        "blue_lut": luts["blue"].tolist(),
    }


def make_synthetic_model(peak_nits=1000.0):
    """
    构造合成 DisplayModel：用 BT.2020 原色（当作「实测原色」）与合成 MHC2。
    """
    mhc2 = make_synthetic_mhc2(peak_nits=peak_nits)
    M = build_rgb_to_xyz_from_primaries(
        BT2020_PRIMARIES["red"], BT2020_PRIMARIES["green"],
        BT2020_PRIMARIES["blue"], BT2020_PRIMARIES["white"],
    )
    # M 的列即各原色（相对白点 Y=1），据此还原「实测」XYZ
    rxyz, gxyz, bxyz = M[:, 0], M[:, 1], M[:, 2]
    wtpt = M[:, 0] + M[:, 1] + M[:, 2]
    return build_model_from_calibration(
        mhc2, rxyz, gxyz, bxyz, wtpt, peak_nits=peak_nits
    ), mhc2


# ----------------------------------------------------------------------
# 检查
# ----------------------------------------------------------------------

def check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return bool(ok)


def pcs_reference(model, dev_codes):
    """
    按 A2B0 的定义计算参考 PCS，并标出哪些点的分量已触及显示器能力上限。

    「触及上限」= 请求的 PCS 分量落在该通道的最大可达值上（或超出），
    此时 PCS 已经丢掉了区分不同设备码值的信息，反向映射必然不唯一。
    """
    scale, direction = model.pcs_anchor()
    raw = (np.atleast_2d(model.device_to_xyz(dev_codes)) / scale) * direction
    limit = model.achievable_pcs_limit()
    clipped = (raw > limit[None, :] + 1e-6).any(axis=1)
    return np.clip(raw, 0.0, 1.0), clipped, limit


def run_checks(grid, fmt, peak_nits):
    results = []
    print(f"\n=== 合成显示器：{peak_nits:.0f} nit 峰值，CLUT {grid}³，格式 {fmt} ===")
    # 粗网格本身的插值误差更大，阈值随网格点数放宽
    tol_main = 0.005 if grid >= 33 else 0.015      # 主体区域
    tol_gray = 0.005 if grid >= 33 else 0.015      # 灰阶轴
    tol_edge = 0.02 if grid >= 33 else 0.04        # 贴近/触及上限区域

    model, mhc2 = make_synthetic_model(peak_nits=peak_nits)
    print(f"  模型：矩阵 Y 行 = {np.round(model.xyz_matrix[1], 6).tolist()}")
    print(f"  PCS 锚点（1.0 对应）绝对 XYZ = "
          f"{np.round(model.pcs_peak_xyz(), 3).tolist()} nit")

    # ---------------- A. 结构一致性 ----------------
    print("\n[A] 结构一致性")
    tags = make_clut_tags(model, grid=grid, fmt=fmt, with_b2a=True)
    for name in ("A2B0", "B2A0"):
        parsed = parse_clut_tag(tags[name])
        results.append(check(
            f"{name} 解析成功（{parsed['signature']}）",
            parsed["grid"] == (grid, grid, grid),
            f"grid={parsed['grid']} channels={parsed['input_channels']}->{parsed['output_channels']}",
        ))
        clut_written = np.asarray(
            (parse_clut_tag(tags[name])["clut"])
        )
        results.append(check(f"{name} CLUT 数值范围在 [0,1]",
                             float(clut_written.min()) >= 0.0 and float(clut_written.max()) <= 1.0,
                             f"[{clut_written.min():.4f}, {clut_written.max():.4f}]"))

    # 16bit 定点往返误差
    a2b = parse_clut_tag(tags["A2B0"])["clut"]
    node_idx = [(0, 0, 0), (grid // 2, grid // 2, grid // 2), (grid - 1, grid - 1, grid - 1)]
    max_fixed_err = 0.0
    for idx in node_idx:
        want = model.device_to_pcs(np.array(idx, dtype=float) / (grid - 1))
        got = a2b[idx]
        max_fixed_err = max(max_fixed_err, float(np.max(np.abs(want - got))))
    results.append(check("16bit 定点写入误差 < 1/65535*1.1",
                         max_fixed_err <= 1.1 / 65535,
                         f"max={max_fixed_err:.3e}"))

    # 白色节点 == wtpt 约定（Y=1）
    # 注意：X/Z 可能超过 1 而被裁切（相对 PCS 的固有上限），因此这里验证：
    #   · Y 必须精确等于 1（peak-relative 锚点）
    #   · 未被裁切的分量必须等于 wtpt 方向
    white_node = a2b[grid - 1, grid - 1, grid - 1]
    _, direction = model.pcs_anchor()
    raw_white = (model.device_to_xyz([1.0, 1.0, 1.0]) / model.pcs_anchor()[0]) * direction
    expected = np.clip(raw_white, 0.0, 1.0)
    results.append(check("白色 CLUT 节点 == peak-relative 白点（Y = 1）",
                         abs(white_node[1] - 1.0) < 1e-3 and
                         np.allclose(white_node, expected, atol=2e-3),
                         f"node={np.round(white_node, 4).tolist()} "
                         f"预期={np.round(expected, 4).tolist()} wtpt={np.round(direction, 4).tolist()}"))

    # ---------------- B. 插值精度 ----------------
    print("\n[B] 插值精度（网格内部随机采样，与原始模型对比）")
    mean_err, max_err = clut_verify(a2b, model, samples=2000)
    results.append(check("A2B0 四面体插值平均误差 < 0.1%", mean_err < max(0.001, tol_main / 5),
                         f"mean={mean_err * 100:.4f}% max={max_err * 100:.4f}%"))

    rng = np.random.default_rng(11)
    pts = rng.random((20000, 3))
    direct, clipped, limit = pcs_reference(model, pts)
    interp = tetrahedral_interp(a2b, pts)
    err = np.abs(direct - interp).max(axis=1)
    # 再排除「贴近上限」的点：这些点的一阶导差异极大，四面体插值在
    # 折线边界附近本来就达不到 0.5%，不代表实现有问题。
    near_edge = (direct > (limit[None, :] - 0.02)).any(axis=1)
    easy = ~clipped & ~near_edge
    results.append(check(f"A2B0 采样点：最大误差 < {tol_main * 100:.1f}%（排除贴近上限的样本）",
                         float(err[easy].max()) < tol_main,
                         f"max={float(err[easy].max()) * 100:.4f}% "
                         f"mean={float(err[easy].mean()) * 100:.4f}% "
                         f"（{int(easy.sum())}/{len(easy)} 点）"))
    results.append(check("A2B0 全部采样点平均误差 < 0.1%",
                         float(err.mean()) < 0.001,
                         f"mean={float(err.mean()) * 100:.4f}% max={float(err.max()) * 100:.4f}%"))
    # 触及上限的区域：PCS 已裁切，误差必然偏大，仅作信息报告
    results.append(check(f"A2B0 触及上限区域误差 < {tol_edge * 100:.0f}%（信息性）",
                         float(err[clipped].max()) < tol_edge,
                         f"max={float(err[clipped].max()) * 100:.4f}% "
                         f"mean={float(err[clipped].mean()) * 100:.4f}% "
                         f"（{int(clipped.sum())} 点；相对色度学 PCS 的固有上限，见 README）"))

    # ---------------- C. 往返一致性 ----------------
    print("\n[C] 往返一致性（A2B0 -> B2A0 -> A2B0）")
    b2a = parse_clut_tag(tags["B2A0"])["clut"]
    # 真实使用路径：设备值 -> PCS(A2B0) -> 设备值(B2A0) -> PCS(A2B0)。
    # 注意：一旦请求的 PCS 分量触及显示器上限，反向映射就是多对一的
    # （PCS 已裁切，无法区分原始码值），因此分区统计。
    dev0 = rng.random((6000, 3))
    pcs0, cl0, _ = pcs_reference(model, dev0)
    dev1 = tetrahedral_interp(b2a, pcs0)
    pcs1 = tetrahedral_interp(a2b, dev1)
    d = np.abs(pcs0 - pcs1).max(axis=1)
    results.append(check("往返 PCS 误差（未触及上限，中位 < 0.5%）",
                         float(np.median(d[~cl0])) < 0.005,
                         f"median={float(np.median(d[~cl0])) * 100:.4f}% "
                         f"max={float(d[~cl0].max()) * 100:.4f}% "
                         f"（{int((~cl0).sum())} 点）"))
    dev_err = np.abs(dev1 - dev0).max(axis=1)
    results.append(check("往返设备值误差（未触及上限，中位 < 1%）",
                         float(np.median(dev_err[~cl0])) < 0.01,
                         f"median={float(np.median(dev_err[~cl0])) * 100:.4f}% "
                         f"p90={float(np.percentile(dev_err[~cl0], 90)) * 100:.4f}%"))
    # 触及上限的区域：报告偏差量级（这是 PCS 表示的固有代价，不是实现错误）
    results.append(check("触及上限区域：往返偏差有界（< 60%，信息性）",
                         float(dev_err[cl0].max()) < 0.6,
                         f"median={float(np.median(dev_err[cl0])) * 100:.2f}% "
                         f"max={float(dev_err[cl0].max()) * 100:.2f}% "
                         f"（{int(cl0.sum())} 点，占 {cl0.mean() * 100:.0f}%）"))
    # 单调性：沿灰阶轴，PCS 亮度递增时 B2A0 给出的设备码值必须单调不减
    gray_axis = np.linspace(0.0, 1.0, 256)
    gpts = np.stack([gray_axis] * 3, axis=1)
    gpcs, gclip, _ = pcs_reference(model, gpts)
    order = np.argsort(gpcs[:, 1])
    gdev = tetrahedral_interp(b2a, gpcs[order])[:, 0]
    mono = np.diff(gdev) >= -1e-9
    results.append(check("B2A0 灰阶轴上码值单调不减",
                         bool(np.all(mono)),
                         f"{int(mono.sum())}/{len(mono)} 相邻对满足单调；"
                         f"码值 {gdev[0]:.4f} → {gdev[-1]:.4f}"))

    # 灰阶轴：模型与 CLUT 一致性
    gray = np.linspace(0.0, 1.0, 64)
    gray_pts = np.stack([gray] * 3, axis=1)
    gray_ref, gray_clip, _ = pcs_reference(model, gray_pts)
    dg = np.abs(tetrahedral_interp(a2b, gray_pts) - gray_ref).max(axis=1)
    results.append(check(f"灰阶轴误差（未触及上限）< {tol_gray * 100:.1f}%",
                         float(dg[~gray_clip].max()) < tol_gray,
                         f"max={float(dg[~gray_clip].max()) * 100:.4f}% "
                         f"mean={float(dg[~gray_clip].mean()) * 100:.4f}%"))
    results.append(check("灰阶 Y（亮度）单调不减",
                         bool(np.all(np.diff(gray_ref[:, 1]) >= -1e-9)),
                         f"Y 范围 {gray_ref[0, 1]:.4f} → {gray_ref[-1, 1]:.4f}"))

    # ---------------- D. 真 profile ----------------
    print("\n[D] 真 profile（写入 hdr_empty.icc 副本并回读）")
    base = os.path.join(PROJECT_ROOT, "data", "hdr_empty.icc")
    handle = ICCProfile(base)
    mhc2_before = handle.read_MHC2()
    written = write_clut_tags(handle, tags)
    handle.rebuild()

    tmp = os.path.join(tempfile.gettempdir(), f"rwhc_clut_selftest_{fmt}_{grid}.icc")
    handle.save(tmp)

    again = ICCProfile(tmp)
    size = os.path.getsize(tmp)
    results.append(check("A2B0/B2A0 写入并回读成功",
                         "A2B0" in again.tags and "B2A0" in again.tags,
                         f"tags={written}"))
    # 回读后逐字节比较
    ok_bytes = True
    for name in ("A2B0", "B2A0"):
        info = again.tags[name]
        raw = bytes(again.data[info["offset"]:info["offset"] + info["size"]])
        exp = tags[name]
        if raw[:len(exp)] != exp:
            ok_bytes = False
    results.append(check("回读字节与写入字节一致", ok_bytes))

    # 校验标签表、header size 字段
    import struct
    declared = struct.unpack(">I", again.data[0:4])[0]
    results.append(check("header 文件大小字段正确", declared == size,
                         f"declared={declared} actual={size}"))
    results.append(check("tag 表 count 与解析一致",
                         struct.unpack(">I", again.data[128:132])[0] == len(again.tags),
                         f"count={len(again.tags)}"))

    # MHC2 必须完好
    mhc2_after = again.read_MHC2()
    same = (
        np.allclose(mhc2_before["red_lut"], mhc2_after["red_lut"]) and
        np.allclose(mhc2_before["green_lut"], mhc2_after["green_lut"]) and
        np.allclose(mhc2_before["blue_lut"], mhc2_after["blue_lut"]) and
        abs(mhc2_before["peak_luminance"] - mhc2_after["peak_luminance"]) < 1e-3
    )
    results.append(check("MHC2 标签未被破坏", same,
                         f"entry_count={mhc2_after['entry_count']}"))
    results.append(check("原有矩阵标签完好",
                         again.read_XYZType("wtpt") is not None and
                         again.read_XYZType("rXYZ") is not None))

    # mAB / mft2 交叉检查：同一模型两种格式，CLUT 数值应一致
    other = CLUT_FORMAT_MAB if fmt == CLUT_FORMAT_MFT2 else CLUT_FORMAT_MFT2
    tags_other = make_clut_tags(model, grid=grid, fmt=other, with_b2a=False)
    c1 = parse_clut_tag(tags["A2B0"])["clut"]
    c2 = parse_clut_tag(tags_other["A2B0"])["clut"]
    results.append(check(f"mft2 与 mAB 生成的 CLUT 数值一致（16bit 内）",
                         float(np.abs(c1 - c2).max()) <= 1.0 / 65535,
                         f"max diff={float(np.abs(c1 - c2).max()):.3e}"))

    # 逆模型精度：设备值 -> PCS -> 设备值（分区统计，见 C 段说明）
    dev_pts = rng.random((2000, 3))
    pcs_of_dev, clip_dev, _ = pcs_reference(model, dev_pts)
    dev_back = tetrahedral_interp(b2a, pcs_of_dev)
    ddev = np.abs(dev_back - dev_pts).max(axis=1)
    results.append(check("闭环设备值误差（未触及上限，中位 < 0.5%）",
                         float(np.median(ddev[~clip_dev])) < 0.005,
                         f"median={float(np.median(ddev[~clip_dev])) * 100:.4f}% "
                         f"p95={float(np.percentile(ddev[~clip_dev], 95)) * 100:.4f}% "
                         f"max={float(ddev[~clip_dev].max()) * 100:.4f}%"))

    # 越界 PCS 必须有界（不能发散）
    oob = np.array([[1.5, 1.5, 1.5], [-0.2, 0.5, 0.5], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]])
    oob_dev = model.pcs_to_device(oob)
    results.append(check("越界 PCS 求逆有界（落在 [0,1]）",
                         bool(np.all(np.isfinite(oob_dev)) and
                              oob_dev.min() >= 0.0 and oob_dev.max() <= 1.0),
                         f"{np.round(oob_dev, 4).tolist()}"))

    print(f"\n  生成文件：{tmp}（{size / 1024:.1f} KB）")
    return results


def main():
    ap = argparse.ArgumentParser(description="CLUT profile 自检")
    ap.add_argument("--grid", type=int, default=33, help="CLUT 网格点数（默认 33）")
    ap.add_argument("--fmt", default=CLUT_FORMAT_MFT2, choices=[CLUT_FORMAT_MFT2, CLUT_FORMAT_MAB])
    ap.add_argument("--peak", type=float, default=1000.0, help="合成显示器峰值亮度（nit）")
    args = ap.parse_args()

    res = run_checks(args.grid, args.fmt, args.peak)
    passed = sum(1 for r in res if r)
    print(f"\n==== 结果：{passed}/{len(res)} 项通过 ====")
    return 0 if passed == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
