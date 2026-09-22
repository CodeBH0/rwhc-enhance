# -*- coding: utf-8 -*-
"""
clut_icc.py — HDR 显示器 profile 的 CLUT（多维查找表）生成与读写

本模块做什么
------------
本项目的原有链路是「矩阵 + 1D LUT」方案：

  · rXYZ / gXYZ / bXYZ / wtpt + rTRC / gTRC / bTRC  —— 矩阵 / TRC 标签
  · MHC2（私有标签）                                —— Windows HDR 校准真正用的数据

该方案在 Windows 的 HDR 链路上工作正常，但 profile 里没有 ICC 标准的多维查找表，
色彩管理软件只能退回矩阵近似。本模块在**完整保留原有矩阵 / MHC2 标签**的前提下，
额外生成标准 CLUT 标签，使同一个 ICC 文件同时具备：

  A2B0 : 设备编码 RGB  ->  PCS（peak-relative XYZ）   前向描述
  B2A0 : PCS          ->  设备编码 RGB                反向映射

设备编码（device space）
------------------------
RGB，传递函数为 **ST 2084 / PQ**（HDR 显示器在 Windows HDR 模式下的原生输入）。
因此 A2B0 的 A（输入）曲线是恒等，CLUT 的轴就是 PQ 码值 0..1（= 10bit 0..1023）。
B2A0 的 M（输出）曲线同样是恒等，CLUT 节点直接存 PQ 码值。

PCS 锚定（关键约定）
-------------------
PCS 为 **peak-relative XYZ**：

    PCS = (XYZ_absolute(nits) / anchor_nits) * direction

其中 `anchor_nits` 与 `direction` 由模型在设备白点（PQ 1.0）处的输出决定
（见 `DisplayModel.pcs_anchor`），因此：

  · 显示器能输出的最亮白对应 PCS **Y = 1.0**，X/Z 按其自身色度等比取值；
  · A2B0 的白色节点 == rXYZ + gXYZ + bXYZ == wtpt 方向；
  · `lumi` 标签给出绝对峰值亮度，PCS 可据此还原为绝对亮度。

这与 DisplayCAL 生成的 XYZLUT 显示器 profile 采用同一约定。全部 CLUT 数值
都落在 ICC 的 [0,1] 内，**不需要任何扩展编码**（psd / hlc / PCC）或非标准
PCS 签名，因此任何 CMM 都能正确读取。

局限（务必了解）：PCS 每个分量上限为 1，而 HDR 显示器上「亮而饱和」的颜色
其 XYZ 常有分量超过白点（例如 BT.2020 原色在峰值白上，红 X≈0.95、
蓝 Z≈1.19），这些分量会被裁切。于是：

  · A2B0（前向）完全准确；
  · B2A0（反向）在未被裁切的颜色上可精确求逆；在被裁切的高光区，多个设备
    码值会映射到同一个 PCS 值，反向本质上是多对一的（见 README）。

生成 CLUT 的数据来源
--------------------
全部来自校准过程中已经得到的实测数据，**不需要额外测量**：

  1. 实测原色 / 白点 XYZ   -> 设备矩阵 M（线性 RGB -> XYZ，白点锁定）
  2. MHC2 每通道 1D LUT    -> 显示器实际响应（期望输出 PQ -> 所需输入 PQ，
                              与 app.py 的 calibrate_pq 语义一致）
  3. 峰值 / 黑场亮度        -> PCS 标度

正向模型（设备码值 -> PCS）：

    pq_in  = code                        # 设备编码就是 PQ
    pq_out = M.pcs_anchor... 见 device_to_linear：对 MHC2 LUT 做插值求逆
    rgb    = pq_decode(pq_out)           # 线性 RGB，按峰值归一
    XYZ    = peak_nits * (M @ rgb)       # 绝对 XYZ（nits）
    PCS    = (XYZ / anchor_nits) * direction

反向模型（PCS -> 设备码值）：先按「密集正向表 + 局部细网格」取前 K 个候选初值，
再对每个候选做列尺度归一 + 信任域限制的阻尼牛顿迭代，最后选渲染误差最小者。
这样既避免落入错误的局部形状，也不用解析近似逆。

参考
----
  · ICC.1:2022 §10.11（mAB/mBA）、§10.3（lut16Type/mft2）—— 标签二进制布局
  · reference/color-algorithms-handbook 01/04/06 —— 矩阵构造与 PQ 传递函数
"""

from __future__ import annotations

import struct
import numpy as np

from convert_utils import (
    pq_eotf,
    pq_oetf,
    XYZ_to_bt2020_linear,
    rgb2020_linear_to_lms,
    lms_p_to_ictcp,
    pq_encode,
)


# ======================================================================
# 常量
# ======================================================================

#: 标签格式
CLUT_FORMAT_MFT2 = "mft2"   # lut16Type：ICC v2 起的通用格式，兼容性最好
CLUT_FORMAT_MAB = "mab"     # mAB：ICC v4 格式，体积约为 mft2 的 2/3
CLUT_FORMATS = (CLUT_FORMAT_MFT2, CLUT_FORMAT_MAB)

#: 每维网格点数（37 与 DisplayCAL 的 "XYZLUT+MTX 37" 一致；33 体积更小）
CLUT_GRID_CHOICES = (17, 25, 33, 37, 45, 65)
CLUT_GRID_DEFAULT = 33

#: 通道数（RGB -> XYZ）
CLUT_CHANNELS = 3

#: PQ 满量程亮度（ST 2084 定义为 10000 cd/m²）
PQ_MAX_NITS = 10000.0

#: mAB 中矩阵的固定值（mAB 的矩阵列不参与媒体白点缩放，按 ICC 规定取 1.0）
MAB_MATRIX_ELEMS = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

#: mft2 中矩阵的固定值（相对色度学：媒体白点缩放到 PCS 白点）
MFT2_MATRIX_ELEMS = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


# ======================================================================
# 定点数打包 / 解包（ICC 数据类型）
# ======================================================================

def pack_u8fixed8(values):
    """数值 -> u8Fixed8Number（1 字节整数 + 1 字节小数）。"""
    arr = np.asarray(values, dtype=np.float64).ravel()
    raw = np.clip(np.round(np.clip(arr, 0.0, 255.9961) * 256.0), 0, 65535).astype(">u2")
    return raw.tobytes()


def unpack_u8fixed8(raw):
    """u8Fixed8Number 字节 -> 数值数组。"""
    return np.frombuffer(raw, dtype=">u2").astype(np.float64) / 256.0


def pack_s15fixed16(values):
    """数值 -> s15Fixed16Number。"""
    arr = np.asarray(values, dtype=np.float64).ravel()
    ints = np.clip(np.round(np.clip(arr, -32768.0, 32767.99998) * 65536.0),
                   -(2 ** 31), 2 ** 31 - 1).astype(">i4")
    return ints.tobytes()


def unpack_s15fixed16(raw):
    """s15Fixed16Number 字节 -> 数值数组。"""
    return np.frombuffer(raw, dtype=">i4").astype(np.float64) / 65536.0


def pack_u16fixed16(values):
    """数值（0..1）-> u16Fixed16Number / uInt16Number。"""
    arr = np.clip(np.asarray(values, dtype=np.float64).ravel(), 0.0, 1.0)
    return np.round(arr * 65535.0).astype(">u2").tobytes()


def unpack_u16fixed16(raw):
    """uInt16Number 字节 -> 数值数组（0..1）。"""
    return np.frombuffer(raw, dtype=">u2").astype(np.float64) / 65535.0


# ======================================================================
# 传递函数 / PQ 工具
# ======================================================================

def pq_encode_norm(linear):
    """线性亮度（按 10000 nit 归一，0..1）-> PQ 码值 0..1。"""
    return pq_oetf(np.asarray(linear, dtype=np.float64) * PQ_MAX_NITS)


def pq_decode_norm(code):
    """PQ 码值 0..1 -> 线性亮度（按 10000 nit 归一，0..1）。"""
    return np.clip(np.asarray(pq_eotf(code), dtype=np.float64) / PQ_MAX_NITS, 0.0, 1.0)


# ======================================================================
# cLUT 基本运算
# ======================================================================

def clut_node_indices(grid: int) -> np.ndarray:
    """
    返回 (N³, 3) 的网格节点索引，顺序符合 ICC 规定：
    **第一通道（R）变化最快**（idx = R + G·N + B·N²）。
    """
    grid = int(grid)
    idx = np.arange(grid ** 3, dtype=np.int64)
    r = idx % grid
    g = (idx // grid) % grid
    b = idx // (grid * grid)
    return np.stack([r, g, b], axis=1)


def clut_grid_values(grid: int) -> np.ndarray:
    """返回 (N³, 3) 的网格节点值（0..1，R 变化最快）。"""
    return clut_node_indices(grid) / float(grid - 1)


def tetrahedral_interp(clut: np.ndarray, values) -> np.ndarray:
    """
    对 (N,N,N,3) 的 CLUT 做四面体插值（ICC 允许的两种插值之一，精度更高）。

    values: (...,3)；超出 [0,1] 的部分按端点裁切。
    """
    clut = np.asarray(clut, dtype=np.float64)
    if clut.ndim != 4 or clut.shape[3] != CLUT_CHANNELS:
        raise ValueError("clut 形状必须为 (N,N,N,3)")
    if not (clut.shape[0] == clut.shape[1] == clut.shape[2]):
        raise ValueError("CLUT 必须是立方网格")

    grid = clut.shape[0]
    arr = np.asarray(values, dtype=np.float64)
    single = arr.ndim == 1
    if single:
        arr = arr.reshape(1, 3)
    if arr.shape[-1] != CLUT_CHANNELS:
        raise ValueError("输入值最后一维必须是 3")

    pos = np.clip(arr, 0.0, 1.0) * (grid - 1)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, grid - 1)
    frac = pos - lo

    r0, g0, b0 = lo[:, 0], lo[:, 1], lo[:, 2]
    r1, g1, b1 = hi[:, 0], hi[:, 1], hi[:, 2]

    c000 = clut[r0, g0, b0]
    c100 = clut[r1, g0, b0]
    c010 = clut[r0, g1, b0]
    c110 = clut[r1, g1, b0]
    c001 = clut[r0, g0, b1]
    c101 = clut[r1, g0, b1]
    c011 = clut[r0, g1, b1]
    c111 = clut[r1, g1, b1]

    dr = frac[:, 0:1]
    dg = frac[:, 1:2]
    db = frac[:, 2:3]

    out = np.empty((arr.shape[0], CLUT_CHANNELS), dtype=np.float64)
    masks = [
        (dr >= dg) & (dg >= db),
        (dr >= db) & (db >= dg),
        (db >= dr) & (dr >= dg),
        (dg >= dr) & (dr >= db),
        (dg >= db) & (db >= dr),
        (db >= dg) & (dg >= dr),
    ]
    formulas = [
        c000 + dr * (c100 - c000) + dg * (c110 - c100) + db * (c111 - c110),
        c000 + dr * (c100 - c000) + db * (c101 - c100) + dg * (c111 - c101),
        c000 + db * (c001 - c000) + dr * (c101 - c001) + dg * (c111 - c101),
        c000 + dg * (c010 - c000) + dr * (c110 - c010) + db * (c111 - c110),
        c000 + dg * (c010 - c000) + db * (c011 - c010) + dr * (c111 - c011),
        c000 + db * (c001 - c000) + dg * (c011 - c001) + dr * (c111 - c011),
    ]
    for mask, val in zip(masks, formulas):
        m = mask.ravel()
        if np.any(m):
            out[m] = val[m]

    return out[0] if single else out


# ======================================================================
# 显示器模型
# ======================================================================

def monotonic_lut(lut):
    """把 1D LUT 处理成严格单调（去噪 + 极小斜率），便于稳定求逆。"""
    a = np.clip(np.asarray(lut, dtype=np.float64).ravel(), 0.0, 1.0)
    if a.size < 2:
        raise ValueError("LUT 至少需要 2 个点")
    a = np.maximum.accumulate(a)
    a = np.clip(a + np.linspace(0.0, 1.0, a.size) * 1e-9, 0.0, 1.0)
    return a


def primaries_matrix(xyz_red, xyz_green, xyz_blue, xyz_white, normalize_y=True):
    """
    由实测原色 / 白点 XYZ 构造「线性 RGB -> XYZ」矩阵（白点锁定）。

    normalize_y=True 时把矩阵整体缩放到 M@[1,1,1] 的 Y == 1，
    这样 M 与峰值亮度解耦：abs_XYZ = peak_nits * (M @ rgb)，rgb 以峰值为 1。
    """
    R = np.asarray(xyz_red, dtype=np.float64).reshape(3)
    G = np.asarray(xyz_green, dtype=np.float64).reshape(3)
    B = np.asarray(xyz_blue, dtype=np.float64).reshape(3)
    W = np.asarray(xyz_white, dtype=np.float64).reshape(3)

    M0 = np.column_stack([R, G, B])
    s = np.linalg.solve(M0, W)          # 列缩放，使 M@[1,1,1] == W
    M = M0 @ np.diag(s)
    if normalize_y:
        ysum = float(M[1].sum())
        if ysum <= 0:
            raise ValueError("原色矩阵 Y 行之和 <= 0，实测原色数据异常")
        M = M / ysum
    return M


class DisplayModel:
    """
    HDR 显示器正向模型：设备 PQ 码值 -> 绝对 XYZ(nits)

    参数
    ----
    mhc2 : dict
        MHC2 数据（red_lut / green_lut / blue_lut / entry_count / peak_luminance /
        min_luminance），即本项目已有结构。三个 LUT 表示「目标输出 PQ -> 所需输入 PQ」。
    xyz_matrix : (3,3)
        RGB_linear -> XYZ 矩阵。推荐用 `primaries_matrix` 构造（Y 行归一），
        此时 `peak_nits` 给出 1.0 线性 RGB 对应的绝对白点亮度。
    peak_nits : float
        峰值亮度（cd/m²）。`None` 时由 MHC2 的 peak_luminance 推断。
    black_nits : float
        黑场亮度（cd/m²），默认 0（不扣黑）。
    gray_gain : (3,) or None
        逐通道灰阶增益，用于修正模型预测与实测灰阶的系统偏差；None = [1,1,1]。
    measured_colors : list of (code3, xyz3) or None
        可选的**实测色卡**数据：`code3` 是设备 PQ 码值 0..1，`xyz3` 是同一次
        测量的绝对 XYZ（nits）。历史颜色数据被复用时由 `app.py` 传入
        （见 `color_history.py`），仅用于 `color_accuracy_report()` 校验正向
        模型——它不参与拟合，因此不会悄悄改变已由实测原色/MHC2 推导出的 CLUT。
    """

    def __init__(self, mhc2, xyz_matrix, peak_nits=None, black_nits=0.0, gray_gain=None,
                 seed_grid=33, refine_grid=7, candidates=4, measured_colors=None):
        self.luts = [
            monotonic_lut(mhc2["red_lut"]),
            monotonic_lut(mhc2["green_lut"]),
            monotonic_lut(mhc2["blue_lut"]),
        ]
        if len({len(l) for l in self.luts}) != 1:
            raise ValueError("MHC2 三个通道 LUT 长度不一致")
        self.lut_size = self.luts[0].size
        self.lut_axis = np.linspace(0.0, 1.0, self.lut_size)

        # 反向求解参数：粗搜索表网格、局部细网格、候选初值个数
        self.seed_grid = int(seed_grid)
        self.refine_grid = int(refine_grid)
        self.candidates = int(candidates)

        self.xyz_matrix = np.asarray(xyz_matrix, dtype=np.float64).reshape(3, 3)
        if peak_nits is None:
            peak_nits = mhc2.get("peak_luminance") or 0.0
        self.peak_nits = float(peak_nits)
        if not np.isfinite(self.peak_nits) or self.peak_nits <= 0:
            raise ValueError("peak_nits 必须为 > 0 的有限值")
        self.black_nits = float(black_nits or 0.0)
        self.gray_gain = (np.ones(3) if gray_gain is None
                          else np.asarray(gray_gain, dtype=np.float64).reshape(3))

        self.measured_colors = None
        if measured_colors:
            self.set_measured_colors(measured_colors)

    # ------------------------------------------------------------------
    # 实测色卡参考数据（校验用）
    # ------------------------------------------------------------------
    def set_measured_colors(self, samples):
        """
        记录一组实测色卡样本：`samples` 为 [(code3, xyz3_abs_nits), ...]。

        码值不是整数码而是 0..1 的 PQ 值（与 CLUT 的设备轴一致），实测 XYZ
        为绝对 nits（本项目 `self.measured_xyz` 的 10000 倍）。
        """
        dev, meas = [], []
        for code, xyz in samples:
            c = np.asarray(code, dtype=np.float64).reshape(3)
            x = np.asarray(xyz, dtype=np.float64).reshape(3)
            if not (np.all(np.isfinite(c)) and np.all(np.isfinite(x))):
                continue
            dev.append(np.clip(c, 0.0, 1.0))
            meas.append(x)
        if not dev:
            self.measured_colors = None
            return None
        self.measured_colors = (np.array(dev, dtype=np.float64),
                                np.array(meas, dtype=np.float64))
        return self.measured_colors

    def color_accuracy_report(self, samples=None):
        """
        用实测色卡校验正向模型（A2B 面）：模型预测的绝对 XYZ vs 实测 XYZ。

        返回 dict（无数据时返回 None）：
            n, mean_de_itp, median_de_itp, max_de_itp,
            mean_d_xyz_nits, max_d_xyz_nits, mean_rel_Y_err, worst_index,
            predicted, measured
        其中 ΔE ITP 与 `delteE.XYZdeltaE_ITP` 同一套公式
        （720·√(ΔI² + 0.25·ΔT² + ΔP²)），输入 XYZ 按 10000 nit 归一。

        怎么解读（重要）
        ----------------
        这是一个**模型自一致性**指标，不是绝对的色准报告：
          · 当实测色卡与构造模型所用的数据来自同一状态（同一次测量的
            原色/灰阶）时，误差只反映模型的近似程度（原色矩阵 + 每通道 1D LUT
            对三维设备的近似），量级很小；
          · 当两者来自**不同会话/状态**时，误差还包含面板状态的漂移
            （该项目在真实 hc.log 上观察到：同一台机器不同时刻的灰阶测量之间，
            PQ 输出差异可达 0.01~0.04；跨会话比较色卡时 ΔE ITP 会明显变大）。
        因此这个数值应当**结合面板重复性**来看，而不是当作硬性合格线。

        另外，模型的亮度标度依赖「MHC2 的 LUT 轴 = 设备码值 0..1」这一约定
        （见 README 关于 CLUT 的说明）；若某个 CMM/显示器用的是别的索引约定，
        绝对亮度会整体缩放，而**色度**方向不受影响。

        模型对黑场的处理是线性扣黑（`device_to_xyz`），而实测样本本身含真实
        黑场，所以暗部差异会被略微放大；这是设计取舍，见 `black_direction`。
        """
        if samples is not None:
            self.set_measured_colors(samples)
        if getattr(self, "measured_colors", None) is None:
            return None
        dev, meas = self.measured_colors
        pred = np.atleast_2d(np.asarray(self.device_to_xyz(dev), dtype=np.float64))

        diff = pred - meas
        d_xyz = np.linalg.norm(diff, axis=1)

        def _ictcp(xyz_nits):
            """(N,3) 绝对 XYZ(nits，行=样本) -> ICtCp。

            `convert_utils` 里的这几个函数都只按单点（长度 3 的向量）运算，
            这里显式转置成 (3,N) 走它们的矩阵乘法，再转回 (N,3)。
            约定同 `XYZ_to_ictcp`：输入 0..1 = 0..10000 nit。
            """
            x = np.asarray(xyz_nits, dtype=np.float64) / PQ_MAX_NITS
            lms = rgb2020_linear_to_lms(XYZ_to_bt2020_linear(x.T)).T
            return lms_p_to_ictcp(pq_encode(np.clip(lms, 0.0, 1.0)).T).T

        d_itp = _ictcp(pred) - _ictcp(meas)
        de = 720.0 * np.sqrt(d_itp[:, 0] ** 2
                             + 0.25 * d_itp[:, 1] ** 2
                             + d_itp[:, 2] ** 2)

        y_pred = np.maximum(pred[:, 1], 1e-9)
        rel_y = np.abs(y_pred - meas[:, 1]) / y_pred
        return {
            "n": int(dev.shape[0]),
            "mean_de_itp": float(de.mean()),
            "median_de_itp": float(np.median(de)),
            "max_de_itp": float(de.max()),
            "mean_d_xyz_nits": float(d_xyz.mean()),
            "max_d_xyz_nits": float(d_xyz.max()),
            "mean_rel_Y_err": float(rel_y.mean()),
            "worst_index": int(np.argmax(de)),
            "predicted": pred,
            "measured": meas,
        }

    def color_accuracy_summary(self):
        """`color_accuracy_report` 的一行摘要，没有实测色卡时返回 None。"""
        rep = self.color_accuracy_report()
        if not rep:
            return None
        return ("measured colour card: {} samples, mean dE ITP {:.2f}, median {:.2f}, "
                "max {:.2f}, mean |ΔXYZ| {:.1f} nit").format(
            rep["n"], rep["mean_de_itp"], rep["median_de_itp"],
            rep["max_de_itp"], rep["mean_d_xyz_nits"])

    # ------------------------------------------------------------------
    # 正向
    # ------------------------------------------------------------------
    def device_to_linear(self, pq_code):
        """设备 PQ 码值 -> 线性 RGB（0..1，1 = 峰值）。"""
        code = np.clip(np.asarray(pq_code, dtype=np.float64), 0.0, 1.0)
        single = code.ndim == 1
        if single:
            code = code.reshape(1, 3)
        lin = np.empty_like(code)
        for ch in range(3):
            pq_out = np.interp(code[:, ch], self.lut_axis, self.luts[ch])
            lin[:, ch] = pq_decode_norm(pq_out)
        lin = np.clip(lin * self.gray_gain, 0.0, 1.0)
        return lin[0] if single else lin

    def device_to_xyz(self, pq_code):
        """设备 PQ 码值 -> 绝对 XYZ（nits）。"""
        lin = np.atleast_2d(self.device_to_linear(pq_code))
        xyz = (self.xyz_matrix @ lin.T).T * self.peak_nits
        # 保持 C 连续：(N,3) 且行为样本；避免下游 reshape 成网格时轴序错乱
        xyz = np.ascontiguousarray(xyz, dtype=np.float64)
        if self.black_nits > 0.0:
            xyz = np.maximum(xyz - self.black_nits * self.black_direction(), 0.0)
        return xyz[0] if np.asarray(pq_code).ndim == 1 else xyz

    def device_to_pcs(self, pq_code):
        """
        设备 PQ 码值 -> PCS（peak-relative XYZ，0..1）。

        以「显示器在目标白点码值处能输出的最亮 XYZ」为 1.0 锚点，
        并按白点 X/Z 归一，使白色节点恒等于 wtpt（Y = 1）。
        超出 [0,1] 的分量会被裁切——这是 ICC 相对色度学 PCS 的固有上限。
        """
        xyz = np.atleast_2d(self.device_to_xyz(pq_code))
        scale, direction = self.pcs_anchor()
        pcs = (xyz / scale) * direction
        pcs = np.ascontiguousarray(np.clip(pcs, 0.0, 1.0), dtype=np.float64)
        return pcs[0] if np.asarray(pq_code).ndim == 1 else pcs

    def black_direction(self):
        """黑位扣除方向（用矩阵列和近似中性黑）。"""
        col = self.xyz_matrix.sum(axis=1)
        n = np.linalg.norm(col)
        return col / n if n > 0 else np.zeros(3)

    # ------------------------------------------------------------------
    # PCS 锚点
    # ------------------------------------------------------------------
    def white_xyz(self, white_code=1.0):
        """设备白点码值（默认 PQ 1.0）对应的绝对 XYZ（nits）。"""
        return np.asarray(self.device_to_xyz(np.array([[white_code] * 3]))[0], dtype=np.float64)

    def pcs_anchor(self, white_code=1.0):
        """
        返回 (scale, direction)：
          PCS = (XYZ_abs / scale) * direction
        · scale     = 白点实测亮度（Y）
        · direction = 白点 XYZ 的方向（X/Y, 1, Z/Y），使白色节点 == wtpt
        """
        W = self.white_xyz(white_code)
        y = float(W[1])
        if y <= 0:
            raise ValueError("模型预测的峰值白亮度 <= 0，请检查原色/LUT 数据")
        direction = np.array([W[0] / y, 1.0, W[2] / y], dtype=np.float64)
        return y, direction

    def pcs_peak_xyz(self, white_code=1.0):
        """PCS 的 1.0 对应的绝对 XYZ（nits），可直接写 lumi / wtpt 标签。"""
        scale, direction = self.pcs_anchor(white_code)
        return scale * direction

    # ------------------------------------------------------------------
    # 反向
    # ------------------------------------------------------------------
    def achievable_pcs_limit(self, dense=True):
        """
        返回显示器可复现的 PCS 各分量上限（peak-relative XYZ 通道极值）。

        用密集正向表统计而非只看 8 个顶点——只看顶点会严重高估上限
        （例如「红分量最大」与「蓝分量最大」并不出现在同一点），
        导致本该精确求逆的颜色被误判成「已达上限」。

        PCS 分量达到该上限时，多个不同设备码值会被裁切到同一个 PCS 值，
        反向映射在该区域本质上是多对一的（见 README 的局限说明）。
        """
        if dense:
            _, fwd = self.forward_map(self.seed_grid)
            return np.clip(fwd.max(axis=0), 1e-6, 1.0)
        corners = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
                            [1.0, 1.0, 1.0], [1.0, 1.0, 0.0], [1.0, 0.0, 1.0],
                            [0.0, 1.0, 1.0], [0.0, 0.0, 0.0]])
        pcs = np.asarray(self.device_to_pcs(corners), dtype=np.float64)
        return np.clip(pcs.max(axis=0), 1e-6, 1.0)

    def pcs_to_device(self, pcs, tol=1e-6, iters=90, damp=0.5, seed_grid=None):
        """
        PCS（0..1）-> 设备 PQ 码值。

        做法（标准 LUT 求逆）：
          1. 在密集正向映射（`seed_grid`³ 个设备码值 → PCS）里取前 K 个最近邻
             作为候选初值——比直接对模型做牛顿迭代鲁棒得多；
          2. 每个候选各自做加权阻尼牛顿精修（信任域限制单步幅度）；
          3. 选「渲染结果最接近请求」的那个候选。

        对已经触及 PCS 上限（`achievable_pcs_limit`）的请求：该区域里不同
        设备码值会被裁切到同一个 PCS 值，反向映射本质上是多对一的。此时
        返回的是满足该请求的候选中最优的一个，映射仍然稳定、单调。
        这是相对色度学 PCS 的固有局限，详见 README。
        """
        target_pcs = np.clip(np.asarray(pcs, dtype=np.float64), 0.0, 1.0)
        single = target_pcs.ndim == 1
        if single:
            target_pcs = target_pcs.reshape(1, 3)
        if target_pcs.shape[-1] != CLUT_CHANNELS:
            raise ValueError("PCS 最后一维必须是 3")

        scale, direction = self.pcs_anchor()
        target = np.atleast_2d(target_pcs)

        free, weights = self._constraint_weights(target)
        target_xyz = (target / direction) * scale

        # 多个候选初值：正向模型强非线性，单一最近邻有时会落在错误的局部形状里
        candidates = self._candidate_devices(target, k=self.candidates)
        n = target.shape[0]
        k = candidates.shape[1]
        flat_dev = candidates.reshape(n * k, 3)
        flat_xyz = np.repeat(target_xyz, k, axis=0)
        flat_free = np.repeat(free, k, axis=0)
        flat_w = np.repeat(weights, k, axis=0)

        refined = self._refine_inverse(flat_dev, flat_xyz, flat_free, flat_w,
                                       tol, iters, damp)
        # 按「渲染结果与请求的差距」选最优候选
        rendered = (np.atleast_2d(self.device_to_xyz(refined)) / scale) * direction
        cost = np.max(np.abs(rendered - np.repeat(target, k, axis=0)), axis=1)
        cost = cost.reshape(n, k)
        best = np.argmin(cost, axis=1)
        device = refined.reshape(n, k, 3)[np.arange(n), best]
        return device[0] if single else device

    # -- 反向的内部工具 -------------------------------------------------
    def forward_map(self, grid=33):
        """缓存一份正向映射：(grid³,3) 的设备码值与其 PCS（peak-relative XYZ）。"""
        key = int(grid)
        cache = getattr(self, "_fwd_cache", None)
        if cache is None or cache[0] != key:
            nodes = clut_grid_values(key)
            cache = (key, nodes, np.asarray(self.device_to_pcs(nodes), dtype=np.float64))
            self._fwd_cache = cache
        return cache[1], cache[2]

    def _candidate_devices(self, target_pcs, k=4, seed_grid=None,
                           refine_grid=None, chunk_elems=12_000_000):
        """
        两级搜索给出牛顿迭代的候选初值：

          1. 在较粗的正向表（`seed_grid`³）里取前 k 个最近邻 —— 全局、稳健；
          2. 用第一级最优解在 ±1 格范围内以 `refine_grid`³ 精细重采样，
             把量化误差从 1/(seed_grid−1) 降到 1/(refine_grid−1)。

        这样既有全局搜索的鲁棒性（避免落进错误的局部形状），又只需一份
        便宜的粗表 + 极小的局部细表，不必构造 65³ 的全量距离矩阵。

        性能说明（B2A 33³ 的主要开销就在这里）：
          第一级原来写成 `sum((fwd - t[:,None,:])**2, axis=2)`，会真的分配一个
          (n, m, 3) 的临时数组——33³ 时约 8.6 GB 级别的访存。现在改用等价的
          范数展开 `|a|² - 2·a·b + |b|²`，只做一次矩阵乘（BLAS），数值上完全
          等价（结果差异仅为浮点舍入量级），耗时降到约 1/15。
        """
        seed_grid = int(seed_grid or self.seed_grid)
        refine_grid = int(refine_grid or self.refine_grid)
        nodes, fwd = self.forward_map(seed_grid)
        target = np.atleast_2d(np.asarray(target_pcs, dtype=np.float64))
        m = fwd.shape[0]
        kk = min(int(k), m)
        w = np.array([1.0, 2.0, 1.0])
        fwd_w = fwd * w
        # 预计算，避免每个 chunk 重复算（同时让距离保持「越大越远」的正号形式）
        fwd_sq = np.einsum("ij,ij->i", fwd_w, fwd_w)

        n = target.shape[0]
        out = np.empty((n, int(k), 3), dtype=np.float64)
        # chunk 取 250k 个元素（≈ 一个 33³ 粗表的规模），保证 device_to_pcs 的
        # 返回形状恰好是 (per_row, m, 3) —— 下面的 dd 依赖这一点。
        per_row = max(1, int(250_000 // max(m, 1)))
        step_seed = 1.0 / (seed_grid - 1)
        off = np.stack(np.meshgrid(*[np.linspace(-1.0, 1.0, refine_grid) * step_seed] * 3,
                                   indexing="ij"), axis=-1).reshape(-1, 3)
        for s in range(0, n, per_row):
            e = min(s + per_row, n)
            t = target[s:e] * w
            # 精确等价的最近邻：d = |fwd|² - 2·fwd·t + |t|²（|t|² 与 argpartition 无关）
            d = fwd_sq[None, :] - 2.0 * (t @ fwd_w.T)
            part = np.argpartition(d, kk - 1, axis=1)[:, :kk]
            rows = np.arange(d.shape[0])[:, None]
            order = np.argsort(d[rows, part], axis=1)
            cand = nodes[part[rows, order]]                    # (n, k, 3)

            # 第二级：在每个候选的 ±1 粗格邻域内做细网格局部搜索
            probe_rows = off.shape[0]
            for j in range(kk):
                base = cand[:, j, :]
                probe = np.clip(base[:, None, :] + off[None, :, :], 0.0, 1.0)
                # 注意：`device_to_pcs` 返回 (e-s, probe_rows, 3)，依赖
                # `probe.reshape(-1, 3)` 的形状；把 per_row 固定成 33³ 的整数倍
                # （见上面的 per_row 计算）就永远成立。
                pw = np.atleast_2d(self.device_to_pcs(probe.reshape(-1, 3))) * w
                # 范数展开求距离，避免分配两份 (e-s, probe_rows, 3) 的临时数组：
                #   |pw - t|² = |pw|² - 2·pw·t + |t|²
                dd = (np.einsum("ij,ij->i", pw, pw)
                      - 2.0 * np.einsum("ij,ij->i", pw, np.repeat(t, probe_rows, axis=0))
                      + np.repeat((t * t).sum(axis=1), probe_rows))
                cand[:, j, :] = probe[
                    np.arange(e - s), np.argmin(dd.reshape(e - s, probe_rows), axis=1)]
            out[s:e] = cand
        return out

    def _constraint_weights(self, target_pcs):
        """
        判断哪些 PCS 分量已经触及显示器的能力上限。

        触及上限的分量在反向映射里本质上是多对一的（不同设备码值会被裁切到
        同一个 PCS 值），因此不作为等式约束，只保留一个很小的权重以避免
        该分量在迭代中发散。
        """
        limit = self.achievable_pcs_limit()
        t = np.atleast_2d(np.asarray(target_pcs, dtype=np.float64))
        free = t >= (limit[None, :] - 1e-6)
        weights = np.where(free, 0.05, 1.0)
        return free, weights

    def _refine_inverse(self, dev_seed, target_xyz, free, weights, tol, iters, damp,
                        chunk=8192):
        """
        分批加权阻尼牛顿精修（已收敛的点会被剔除，不再参与后续迭代）。

        关键细节：正向模型的雅可比各列量级差异极大（PQ 是感知编码，暗部码值
        对 XYZ 的贡献可能比亮部小 5 个数量级）。直接解 J δ = −r 会因列尺度
        悬殊而数值失真（曾观察到 1e3 量级的荒谬步长）；这里先按列范数归一，
        求出的步长换回真实单位后再放进信任域（单步 ≤ 0.1 码值）。
        """
        n = target_xyz.shape[0]
        out = np.array(dev_seed, dtype=np.float64, copy=True)
        h = 1e-3
        eye = np.eye(3)
        for s in range(0, n, chunk):
            e = min(s + chunk, n)
            cur = np.clip(out[s:e], 0.0, 1.0)
            tgt = target_xyz[s:e]
            w = weights[s:e]
            fr = free[s:e]
            m = e - s
            active = np.ones(m, dtype=bool)       # 尚未收敛的点
            idx_all = np.arange(m)
            for _ in range(int(iters)):
                if not np.any(active):
                    break
                ai = idx_all[active]
                c = cur[ai]
                t = tgt[ai]
                wa = w[ai]
                fa = fr[ai]
                f = np.atleast_2d(self.device_to_xyz(c))
                res = np.where(fa, 0.0, f - t)
                conv = np.max(np.abs(res) * wa, axis=1) < tol
                if np.any(conv):
                    active[ai[conv]] = False
                    keep = ~conv
                    ai, c, t, wa, fa = ai[keep], c[keep], t[keep], wa[keep], fa[keep]
                    f, res = f[keep], res[keep]
                    if ai.size == 0:
                        break
                mm = ai.size
                jac = np.empty((mm, 3, 3), dtype=np.float64)
                for ch in range(3):
                    step = np.zeros(3)
                    step[ch] = h
                    fp = np.atleast_2d(self.device_to_xyz(np.clip(c + step, 0.0, 1.0)))
                    jac[:, :, ch] = (fp - f) / h

                # 列尺度归一 + 批量求解（全向量化，避免逐点 Python 循环）
                col = np.linalg.norm(jac, axis=1)              # (mm,3)
                col = np.where(col > 1e-12, col, 1.0)
                jw = (jac / col[:, None, :]) * wa[:, :, None]   # (mm,3,3)
                a = np.einsum("mij,mik->mjk", jw, jw) + 1e-9 * eye
                b = -np.einsum("mij,mi->mj", jw, res)[:, :, None]
                delta_scaled = np.linalg.solve(a, b)[:, :, 0]    # (mm,3)
                delta = np.clip(delta_scaled / col, -0.1, 0.1)  # 信任域
                delta[~np.all(np.isfinite(delta), axis=1)] = 0.0
                cur[ai] = np.clip(c + damp * delta, 0.0, 1.0)
            out[s:e] = cur
        return out

    # ------------------------------------------------------------------
    # 灰阶一致性
    # ------------------------------------------------------------------
    def predicted_out_pq(self, code_1d):
        """灰阶输入码值 -> 模型预测的「输出 PQ」（三通道）。"""
        code_1d = np.clip(np.asarray(code_1d, dtype=np.float64), 0.0, 1.0)
        xyz = np.atleast_2d(self.device_to_xyz(np.stack([code_1d] * 3, axis=-1)))
        lin = np.clip(xyz / self.peak_nits, 0.0, 1.0)
        minv = np.linalg.inv(self.xyz_matrix)
        rgb_lin = (minv @ lin.T).T
        return pq_encode_norm(np.clip(rgb_lin, 0.0, 1.0))

    def fit_gray_gain(self, measured_pq, codes=None, clip_range=(0.5, 2.0)):
        """
        由实测灰阶估计逐通道增益，使模型预测与实测一致。

        measured_pq: {"red":[...], "green":[...], "blue":[...]}，即本项目
                     `self.measured_pq`（实测输出 PQ，按 calibrate_pq 的采样顺序）
        codes:       对应输入码值 0..1；默认 np.linspace(0,1,num)
        返回:        (3,) 增益（已限幅）
        """
        if not measured_pq:
            return np.ones(3)
        keys = ("red", "green", "blue")
        if not all(k in measured_pq and len(measured_pq[k]) > 0 for k in keys):
            return np.ones(3)
        arrs = [np.asarray(measured_pq[k], dtype=np.float64).ravel() for k in keys]
        num = min(len(a) for a in arrs)
        if num < 4:
            return np.ones(3)
        arrs = [a[:num] for a in arrs]
        codes = (np.linspace(0.0, 1.0, num) if codes is None
                 else np.asarray(codes, dtype=np.float64).ravel()[:num])

        pred = self.predicted_out_pq(codes)
        gains = []
        for ch in range(3):
            meas = arrs[ch]
            p = np.maximum(pred[:, ch], 1e-6)
            ratio = np.where(meas > 1e-4, meas / p, np.nan)
            ok = np.isfinite(ratio)
            if not np.any(ok):
                gains.append(1.0)
                continue
            gains.append(float(np.clip(np.median(ratio[ok]), clip_range[0], clip_range[1])))
        return np.array(gains, dtype=np.float64)


# ======================================================================
# CLUT 生成
# ======================================================================

def records_to_grid(records, grid: int) -> np.ndarray:
    """
    把 (N³, 3) 的记录数组按 ICC 轴序（**R 变化最快**）装进 (N,N,N,3) 网格。

    这里刻意不走 `reshape`：reshape 的 C/F 顺序语义会随 numpy 版本/内存
    布局变化，一旦搞错，CLUT 的轴序就会静默错位（文件看起来完全正常，
    插值结果却是错的）。改成显式按索引赋值，语义固定且可自检。
    """
    grid = int(grid)
    arr = np.asarray(records, dtype=np.float64)
    if arr.shape != (grid ** 3, 3):
        raise ValueError(f"记录数组形状必须为 ({grid ** 3}, 3)，实际为 {arr.shape}")
    out = np.empty((grid, grid, grid, 3), dtype=np.float64)
    idx = clut_node_indices(grid)
    out[idx[:, 0], idx[:, 1], idx[:, 2]] = arr
    return out


def grid_to_records(grid_array) -> np.ndarray:
    """`records_to_grid` 的逆： (N,N,N,3) -> (N³,3)，R 变化最快。"""
    g = np.asarray(grid_array, dtype=np.float64)
    if g.ndim != 4 or g.shape[3] != CLUT_CHANNELS:
        raise ValueError("clut 形状必须为 (N,N,N,3)")
    n = g.shape[0]
    idx = clut_node_indices(n)
    return g[idx[:, 0], idx[:, 1], idx[:, 2]]


def build_a2b_clut(model: DisplayModel, grid: int = CLUT_GRID_DEFAULT):
    """
    A2B0 的 cLUT：设备 PQ 码值网格 -> PCS（peak-relative XYZ）。

    形状 (N,N,N,3)，轴序为「R 变化最快」（ICC 规定），见 `records_to_grid`。
    """
    grid = int(grid)
    nodes = clut_grid_values(grid)
    pcs = np.clip(np.asarray(model.device_to_pcs(nodes), dtype=np.float64), 0.0, 1.0)
    return records_to_grid(pcs, grid)


def build_b2a_clut(model: DisplayModel, grid: int = CLUT_GRID_DEFAULT, **solver_kw):
    """B2A0 的 cLUT：PCS 网格 -> 设备 PQ 码值（同样为 R 变化最快）。"""
    grid = int(grid)
    nodes = clut_grid_values(grid)
    dev = np.clip(np.asarray(model.pcs_to_device(nodes, **solver_kw), dtype=np.float64),
                  0.0, 1.0)
    return records_to_grid(dev, grid)


def clut_verify(clut_a2b, model: DisplayModel, samples=300, seed=20260902):
    """
    自检：在网格内部随机取点，比较「四面体插值」与「模型直接计算」。
    返回 (mean_abs_err, max_abs_err)，单位为 PCS 值（0..1）。

    另外会验证网格节点本身与模型一致（轴序自检）——轴序错了这里会立刻暴露。
    """
    grid = int(np.asarray(clut_a2b).shape[0])
    nodes = clut_grid_values(grid)
    # 容差取 2 个 16bit 量化步长（解析回来的 CLUT 已经过 uInt16 量化）
    tol = 2.5 / 65535.0
    node_err = float(np.abs(tetrahedral_interp(clut_a2b, nodes)
                            - np.clip(model.device_to_pcs(nodes), 0.0, 1.0)).max())
    if node_err > tol:
        raise ValueError(
            f"CLUT 轴序自检失败：网格节点与模型不符（max err={node_err:.3e} > {tol:.3e}）。"
            "请检查 records_to_grid / clut_node_indices 的轴序约定。"
        )

    rng = np.random.default_rng(seed)
    pts = rng.random((int(samples), 3))
    diff = np.abs(tetrahedral_interp(clut_a2b, pts) - np.clip(model.device_to_pcs(pts), 0.0, 1.0))
    return float(diff.mean()), float(diff.max())


# ======================================================================
# 标签构建：mft2 (lut16Type)
#
#   偏移 0  : 'mft2' + 4 字节保留
#   偏移 8  : 输入通道数=3, 输出通道数=3, 输入网格点数, 输出网格点数
#   偏移 12 : 3x3 矩阵（9 × s15Fixed16）
#   偏移 48 : 3 × 输入表，每条 gridIn 个 uInt16
#   随后    : CLUT，gridIn³ 个单元，每单元 3 个 uInt16（R 变化最快）
#   随后    : 3 × 输出表，每条 gridOut 个 uInt16
# ======================================================================

def build_mft2_tag(clut, in_curves=None, out_curves=None, matrix=None,
                   grid=None, out_grid=None):
    """
    构建 lut16Type（mft2）标签。

    clut: (N,N,N,3)，节点值 0..1
    in_curves / out_curves: 3 条曲线（uInt16 采样，0..1），None = 恒等（count=2）
    """
    clut = np.asarray(clut, dtype=np.float64)
    if clut.ndim != 4 or clut.shape[3] != CLUT_CHANNELS:
        raise ValueError("clut 形状必须为 (N,N,N,3)")
    g_in = int(clut.shape[0])
    if not (clut.shape[1] == clut.shape[2] == g_in):
        raise ValueError("CLUT 必须是立方网格")
    if grid is not None and int(grid) != g_in:
        raise ValueError("grid 与 clut 尺寸不一致")
    g_out = int(out_grid) if out_grid else g_in

    in_tables = _prep_curves(in_curves, 2)
    out_tables = _prep_curves(out_curves, 2)
    mtx = (np.asarray(MFT2_MATRIX_ELEMS, dtype=np.float64) if matrix is None
           else np.asarray(matrix, dtype=np.float64).ravel())
    if mtx.size != 9:
        raise ValueError("matrix 必须是 9 个元素")

    block = bytearray()
    block += b"mft2" + b"\x00\x00\x00\x00"
    block += struct.pack(">BBBB", CLUT_CHANNELS, CLUT_CHANNELS, g_in, g_out)
    block += pack_s15fixed16(mtx)

    for c in in_tables:
        block += pack_u16fixed16(c)
    block += pack_u16fixed16(grid_to_records(clut).reshape(-1))
    for c in out_tables:
        block += pack_u16fixed16(c)

    # 不加尾部填充：ICC 的 tag 数据由 profile 写入时统一补齐到 4 字节边界
    return bytes(block)


def parse_mft2_tag(block):
    """
    解析 lut16Type（mft2）标签，返回与 `parse_clut_tag` 一致的结构。

    注意：mft2 的头里**没有**记录输入/输出曲线的采样点数，只有网格点数。
    因此 CLUT 的起始位置必须由标签总长度反推：

        len = 48 + 3·(in_count·2) + g_in³·out_ch·2 + 3·(out_count·2)

    当输入与输出曲线采样点数相同（本模块写出的都是 2 点恒等曲线）时，
    该方程有唯一解。
    """
    block = bytes(block)
    if block[0:4] != b"mft2":
        raise ValueError("不是 mft2 标签")
    in_ch, out_ch, g_in, g_out = struct.unpack(">BBBB", block[8:12])
    matrix = unpack_s15fixed16(block[12:48]).tolist()

    clut_bytes = g_in ** 3 * out_ch * 2
    fixed = 48 + clut_bytes
    remaining = len(block) - fixed
    if remaining < 0:
        raise ValueError("mft2 标签长度不足，无法容纳 CLUT")
    counts, rem = divmod(remaining, 2 * (in_ch + out_ch))
    if rem != 0:
        # 容错：可能是尾部补零到 4 字节造成的，尝试按曲线点数相同处理
        counts = remaining // (2 * (in_ch + out_ch))
    in_count = out_count = int(counts)
    if in_count < 2:
        # 兜底：按 ICC 允许的最小曲线点数处理
        in_count = out_count = 2

    pos = 48
    in_tables = []
    for _ in range(in_ch):
        in_tables.append(unpack_u16fixed16(block[pos:pos + in_count * 2]))
        pos += in_count * 2

    n_vals = g_in ** 3 * out_ch
    clut_flat = unpack_u16fixed16(block[pos:pos + n_vals * 2])
    if clut_flat.size != n_vals:
        raise ValueError("mft2 CLUT 数据长度与网格尺寸不符")
    pos += n_vals * 2
    clut = records_to_grid(clut_flat.reshape(-1, out_ch), g_in)

    out_tables = []
    for _ in range(out_ch):
        out_tables.append(unpack_u16fixed16(block[pos:pos + out_count * 2]))
        pos += out_count * 2

    return {
        "format": CLUT_FORMAT_MFT2,
        "signature": "mft2",
        "input_channels": in_ch,
        "output_channels": out_ch,
        "grid": (g_in, g_in, g_in),
        "out_grid": g_out,
        "in_curves": in_tables,
        "out_curves": out_tables,
        "matrix": matrix,
        "clut": clut,
    }


# ======================================================================
# 标签构建：mAB / mBA (ICC v4)
#
#   A2B(mAB):  输入 -> A 曲线 -> CLUT -> B 曲线 -> 输出
#   B2A(mBA):  输入 -> B 曲线 -> CLUT -> matrix -> M 曲线 -> 输出
#
#   偏移字段顺序（两种都是）：B / matrix / M / CLUT / A
#   通道数恒为 (3, 3)。本实现里 CLUT 直接存最终数值，故所有曲线恒等。
# ======================================================================

def build_mab_tag(clut, b_curves=None, a_curves=None, grid=None):
    """构建 mAB 标签（A2B0/A2B1/A2B2）。"""
    clut = np.asarray(clut, dtype=np.float64)
    if clut.ndim != 4 or clut.shape[3] != CLUT_CHANNELS:
        raise ValueError("clut 形状必须为 (N,N,N,3)")
    n = int(clut.shape[0])
    if not (clut.shape[1] == clut.shape[2] == n):
        raise ValueError("CLUT 必须是立方网格")
    if grid is not None and int(grid) != n:
        raise ValueError("grid 与 clut 尺寸不一致")

    a = _prep_curves(a_curves, 2)
    b = _prep_curves(b_curves, 2)

    block = bytearray()
    block += b"mAB " + b"\x00\x00\x00\x00"
    block += struct.pack(">BB", CLUT_CHANNELS, CLUT_CHANNELS)
    offset_pos = len(block)
    block += b"\x00" * 20                    # B / matrix / M / CLUT / A

    off_b = len(block)
    block += struct.pack(">I", len(b[0]))
    for c in b:
        block += pack_u8fixed8(c)
    if len(block) % 4:
        block += b"\x00" * (4 - len(block) % 4)

    off_mtx = len(block)
    block += pack_s15fixed16(MAB_MATRIX_ELEMS)

    off_m = 0                                # mAB 不使用 M 曲线

    off_clut = len(block)
    block += _clut_head(n)
    block += pack_u16fixed16(grid_to_records(clut).reshape(-1))
    if len(block) % 4:
        block += b"\x00" * (4 - len(block) % 4)

    off_a = len(block)
    block += struct.pack(">I", len(a[0]))
    for c in a:
        block += pack_u8fixed8(c)
    if len(block) % 4:
        block += b"\x00" * (4 - len(block) % 4)

    struct.pack_into(">IIIII", block, offset_pos, off_b, off_mtx, off_m, off_clut, off_a)
    return bytes(block)


def build_mba_tag(clut, b_curves=None, m_curves=None, grid=None):
    """构建 mBA 标签（B2A0/B2A1/B2A2）。"""
    clut = np.asarray(clut, dtype=np.float64)
    if clut.ndim != 4 or clut.shape[3] != CLUT_CHANNELS:
        raise ValueError("clut 形状必须为 (N,N,N,3)")
    n = int(clut.shape[0])
    if not (clut.shape[1] == clut.shape[2] == n):
        raise ValueError("CLUT 必须是立方网格")
    if grid is not None and int(grid) != n:
        raise ValueError("grid 与 clut 尺寸不一致")

    b = _prep_curves(b_curves, 2)
    m = _prep_curves(m_curves, 2)

    block = bytearray()
    block += b"mBA " + b"\x00\x00\x00\x00"
    block += struct.pack(">BB", CLUT_CHANNELS, CLUT_CHANNELS)
    offset_pos = len(block)
    block += b"\x00" * 20

    off_b = len(block)
    block += struct.pack(">I", len(b[0]))
    for c in b:
        block += pack_u8fixed8(c)
    if len(block) % 4:
        block += b"\x00" * (4 - len(block) % 4)

    off_mtx = len(block)
    block += pack_s15fixed16(MAB_MATRIX_ELEMS)

    off_m = len(block)
    block += struct.pack(">I", len(m[0]))
    for c in m:
        block += pack_u8fixed8(c)
    if len(block) % 4:
        block += b"\x00" * (4 - len(block) % 4)

    off_clut = len(block)
    block += _clut_head(n)
    block += pack_u16fixed16(grid_to_records(clut).reshape(-1))
    if len(block) % 4:
        block += b"\x00" * (4 - len(block) % 4)

    off_a = 0
    struct.pack_into(">IIIII", block, offset_pos, off_b, off_mtx, off_m, off_clut, off_a)
    return bytes(block)


def _clut_head(grid):
    """mAB/mBA 的 CLUT 头：3 字节网格点数 + 1 字节精度 + 3 字节保留。"""
    g = int(grid)
    if not 2 <= g <= 255:
        raise ValueError("网格点数必须是 2..255")
    return struct.pack(">BBBB", g, g, g, 2) + b"\x00\x00\x00"


def _prep_curves(curves, count=2):
    """
    把 None / 单条曲线 / 三条曲线统一成 3 条数组。

    count: 为 None 时生成的**恒等曲线**每条的采样点数（最少 2）。
           注意它只对 None 生效；显式传入的曲线用自己的长度。
    """
    if curves is None:
        n = max(int(count), 2)
        ident = np.array([0.0, 1.0]) if n == 2 else np.linspace(0.0, 1.0, n)
        return [ident.copy() for _ in range(CLUT_CHANNELS)]
    arr = np.asarray(curves, dtype=np.float64)
    if arr.ndim == 1:
        return [arr.copy() for _ in range(CLUT_CHANNELS)]
    out = [np.asarray(c, dtype=np.float64).ravel() for c in curves]
    if len(out) != CLUT_CHANNELS:
        raise ValueError("曲线必须是 3 条")
    return out


# ======================================================================
# 统一解析 / 应用（自检用）
# ======================================================================

def parse_clut_tag(block):
    """解析 mft2 / mAB / mBA 标签，统一成同一结构（见 `apply_ab` / `apply_ba`）。"""
    block = bytes(block)
    sig = block[0:4]
    if sig == b"mft2":
        return parse_mft2_tag(block)
    if sig in (b"mAB ", b"mBA "):
        return parse_mab_tag(block)
    raise ValueError(f"不支持的标签签名: {sig!r}（支持 mft2 / mAB / mBA）")


def parse_mab_tag(block):
    """解析 mAB / mBA 标签。"""
    block = bytes(block)
    sig = block[0:4]
    if sig not in (b"mAB ", b"mBA "):
        raise ValueError("不是 mAB / mBA 标签")
    in_ch, out_ch = struct.unpack(">BB", block[8:10])
    # 偏移字段紧跟在两个通道计数之后：字节 10..29（五个 uint32）
    off_b, off_mtx, off_m, off_clut, off_a = struct.unpack(">IIIII", block[10:30])

    def curves(off, count):
        if off == 0:
            return []
        pos = off + 4
        out = []
        for _ in range(CLUT_CHANNELS):
            out.append(unpack_u8fixed8(block[pos:pos + count]))
            pos += count
        return out

    def curve_count(off):
        return struct.unpack(">I", block[off:off + 4])[0] if off else 0

    clut = None
    grid = None
    if off_clut:
        g1, g2, g3, prec = struct.unpack(">BBBB", block[off_clut:off_clut + 4])
        grid = (g1, g2, g3)
        pos = off_clut + 7
        n_vals = int(g1) * int(g2) * int(g3) * out_ch
        vals = unpack_u16fixed16(block[pos:pos + n_vals * 2])
        if vals.size != n_vals:
            raise ValueError("mAB/mBA CLUT 数据长度与网格尺寸不符")
        clut = records_to_grid(vals.reshape(-1, out_ch), int(g1))

    matrix = unpack_s15fixed16(block[off_mtx:off_mtx + 36]).tolist() if off_mtx else None

    if sig == b"mAB ":
        # A2B0: 输入 = A 曲线，输出 = B 曲线
        in_curves = curves(off_a, curve_count(off_a))
        out_curves = curves(off_b, curve_count(off_b))
    else:
        # B2A0: 输入 = B 曲线，输出 = M 曲线
        in_curves = curves(off_b, curve_count(off_b))
        out_curves = curves(off_m, curve_count(off_m))

    return {
        "format": CLUT_FORMAT_MAB,
        "signature": sig.decode("ascii").strip(),
        "input_channels": in_ch,
        "output_channels": out_ch,
        "grid": grid,
        "out_grid": grid[0] if grid else None,
        "in_curves": in_curves,
        "out_curves": out_curves,
        "matrix": matrix,
        "clut": clut,
    }


def apply_ab(parsed, device_values):
    """按 A2B 语义执行：曲线 -> CLUT -> 曲线，返回输出值（0..1）。"""
    arr = np.asarray(device_values, dtype=np.float64)
    single = arr.ndim == 1
    arr = arr.reshape(1, 3) if single else arr

    x = _apply_curves(arr, parsed.get("in_curves"))
    y = tetrahedral_interp(parsed["clut"], x)
    if parsed.get("matrix") and parsed["format"] == CLUT_FORMAT_MAB:
        m = np.asarray(parsed["matrix"], dtype=np.float64).reshape(3, 3)
        y = np.clip((m @ y.T).T, 0.0, 1.0)
    y = _apply_curves(y, parsed.get("out_curves"))
    return y[0] if single else y


def apply_ba(parsed, pcs_values):
    """按 B2A 语义执行：曲线 -> CLUT -> 曲线，返回设备值（0..1）。"""
    arr = np.asarray(pcs_values, dtype=np.float64)
    single = arr.ndim == 1
    arr = arr.reshape(1, 3) if single else arr

    x = _apply_curves(arr, parsed.get("in_curves"))
    y = tetrahedral_interp(parsed["clut"], x)
    y = _apply_curves(y, parsed.get("out_curves"))
    return y[0] if single else y


def _apply_curves(values, curves):
    if not curves:
        return values
    out = np.empty_like(values)
    for ch in range(CLUT_CHANNELS):
        c = np.asarray(curves[ch], dtype=np.float64)
        if c.size == 0:
            out[:, ch] = values[:, ch]
            continue
        axis = np.linspace(0.0, 1.0, c.size)
        out[:, ch] = np.interp(np.clip(values[:, ch], 0.0, 1.0), axis, c)
    return out


# ======================================================================
# 对外主入口
# ======================================================================

def make_clut_tags(model: DisplayModel, grid: int = CLUT_GRID_DEFAULT,
                   fmt: str = CLUT_FORMAT_MFT2, with_b2a: bool = True,
                   solver_kw=None):
    """
    生成要写入 profile 的标签字典：

      {'A2B0': 块, 'B2A0': 块 | None, 'format': fmt, 'grid': grid, '_meta': {...}}

    fmt: 'mft2'（默认，兼容性最好）或 'mab'（ICC v4 格式，体积更小）
    """
    grid = int(grid)
    if grid < 2:
        raise ValueError("CLUT 网格点数必须 >= 2")
    if fmt not in CLUT_FORMATS:
        raise ValueError(f"未知 CLUT 格式: {fmt!r}（应为 {CLUT_FORMATS}）")

    a2b = build_a2b_clut(model, grid=grid)
    b2a = build_b2a_clut(model, grid=grid, **(solver_kw or {})) if with_b2a else None

    tags = {}
    if fmt == CLUT_FORMAT_MFT2:
        tags["A2B0"] = build_mft2_tag(a2b, grid=grid)
        if b2a is not None:
            tags["B2A0"] = build_mft2_tag(b2a, grid=grid)
    else:
        tags["A2B0"] = build_mab_tag(a2b, grid=grid)
        if b2a is not None:
            tags["B2A0"] = build_mba_tag(b2a, grid=grid)

    mean_err, max_err = clut_verify(a2b, model, samples=200)
    meta = {
        "format": fmt,
        "grid": grid,
        "peak_nits": model.peak_nits,
        "a2b_clut_min": float(a2b.min()),
        "a2b_clut_max": float(a2b.max()),
        "a2b_interp_mean_err": mean_err,
        "a2b_interp_max_err": max_err,
        "tag_sizes": {k: len(v) for k, v in tags.items()},
    }
    if b2a is not None:
        meta["b2a_clut_min"] = float(b2a.min())
        meta["b2a_clut_max"] = float(b2a.max())
    tags["_meta"] = meta
    return tags


def write_clut_tags(icc_handle, tags):
    """把 `make_clut_tags` 的结果写入 ICCProfile 实例，返回写入的标签名列表。"""
    written = []
    for name in ("A2B0", "B2A0"):
        block = tags.get(name)
        if block:
            icc_handle.write_tag(name, block)
            written.append(name)
    return written


def build_model_from_calibration(mhc2, rxyz, gxyz, bxyz, wtpt, peak_nits=None,
                                 black_nits=0.0, measured_pq=None, measured_colors=None):
    """
    由本项目的校准结果构造 DisplayModel。

    mhc2        : self.MHC2（含每通道 LUT）
    rxyz/...    : 实测原色/白点 XYZ（任意绝对尺度，只取方向）
    peak_nits   : 峰值亮度；None 时用 MHC2 的 peak_luminance
    measured_pq : 可选，self.measured_pq；提供时会拟合 `gray_gain` 提升灰阶一致性
    measured_colors : 可选，实测色卡 [(code3_pq, xyz3_abs_nits), ...]；提供后可用
                  `model.color_accuracy_report()` 校验正向模型（历史颜色数据被
                  复用时由 app.py 传入，不影响任何拟合结果）
    """
    M = primaries_matrix(rxyz, gxyz, bxyz, wtpt, normalize_y=True)
    model = DisplayModel(mhc2, M, peak_nits=peak_nits, black_nits=black_nits,
                         measured_colors=measured_colors)
    if measured_pq:
        try:
            model.gray_gain = model.fit_gray_gain(measured_pq)
        except Exception:
            model.gray_gain = np.ones(3)
    return model
