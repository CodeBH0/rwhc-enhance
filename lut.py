from convert_utils import *
import numpy as np
import copy



def convert_transfer(v, src=("gamma", 2.2), dst=("srgb", None),
                     src_peak_nit: float = 10000, dst_peak_nit: float = 10000):
    """
    在 gammaX、sRGB、PQ 之间任意互转（向量化，支持标量或 ndarray）。
    参数:
      - v: 输入码值(0..1)
      - src: 源类型与参数，("gamma", gamma) | ("srgb", None) | ("pq", None)
      - dst: 目标类型与参数，同上
      - src_peak_nit: 源为 PQ 时，用于将绝对亮度归一化为相对亮度的峰值
      - dst_peak_nit: 目标为 PQ 时，用于将相对亮度扩展为绝对亮度的峰值
    返回:
      - 与 v 同形状的目标码值(0..1)
    """
    v = np.asarray(v, dtype=np.float64)

    # 1) 源 -> 线性相对亮度 L_rel ∈ [0,1]
    st, sp = src
    st = (st or "").lower()
    if st == "gamma":
        L_rel = gamma_decode(v, float(sp))
    elif st == "srgb":
        L_rel = srgb_decode(v)
    elif st == "pq":
        L_abs = pq_oetf(v)  # cd/m²
        L_rel = np.clip(L_abs / max(float(src_peak_nit), 1e-12), 0.0, 1.0)
    else:
        raise ValueError(f"未知源类型: {st}（应为 'gamma'|'srgb'|'pq'）")

    # 2) 线性相对亮度 -> 目标
    dt, dp = dst
    dt = (dt or "").lower()
    if dt == "gamma":
        out = gamma_encode(L_rel, float(dp))
    elif dt == "srgb":
        out = srgb_encode(L_rel)
    elif dt == "pq":
        L_abs_t = np.clip(L_rel, 0.0, 1.0) * max(float(dst_peak_nit), 1e-12)
        out = pq_eotf(L_abs_t)
    else:
        raise ValueError(f"未知目标类型: {dt}（应为 'gamma'|'srgb'|'pq'）")

    return np.clip(out, 0.0, 1.0)


def bt2390eetf(V: float, Lb: float, Lw: float, Lmin: float, Lmax: float) -> float:
        """
        BT.2390 EETF
        对 PQ 信号 V 根据黑场/白场限制进行电子-电子传递函数调整。
        Lb, Lw: 参考黑场和白场亮度(0-10000 nit)
        Lmin, Lmax: 目标显示的黑场和白场亮度(0-10000 nit)
        返回调整后的PQ信号值。
        """
        # 将输入PQ值规范化为 EETF 空间 [0,1]
        Vb = pq_oetf(Lb)
        Vw = pq_oetf(Lw)
        E1 = (V - Vb) / (Vw - Vb)
        # 计算目标显示的归一化最小/最大亮度值
        minLum = (pq_oetf(Lmin) - Vb) / (Vw - Vb)
        maxLum = (pq_oetf(Lmax) - Vb) / (Vw - Vb)
        # 膝点和黑场参数:contentReference[oaicite:35]{index=35}
        KS = 1.5 * maxLum - 0.5
        b = minLum
        # 定义 Hermite 样条辅助函数
        T = lambda A: (A - KS) / (1 - KS) if KS != 1 else 0.0
        P = lambda B: (2 * T(B)**3 - 3 * T(B)**2 + 1) * KS \
                    + (T(B)**3 - 2 * T(B)**2 + T(B)) * (1 - KS) \
                    + (-2 * T(B)**3 + 3 * T(B)**2) * maxLum
        # 按照 BT.2390 Step 3.1 & 3.2 计算 E2, E3
        if E1 < KS:
            E2 = E1
        elif KS <= E1 <= 1:
            E2 = P(E1)
        else:
            E2 = E1  # 万一 E1>1（理应不会），则不改变
        E3 = E2
        if 0 <= E2 <= 1:
            E3 = E2 + b * (1 - E2) ** 4  # 黑场提升
        # 反规范化回 PQ 信号
        E4 = E3 * (Vw - Vb) + Vb
        return E4

def find_nearest_idx(arr, value):
    """
    在数组中找到等于或最近的数字的索引
    arr: 数组
    value: 要查找的值
    """
    arr = np.asarray(arr)
    idx = (np.abs(arr - value)).argmin()
    return int(idx)

def max_uniform_target(n, limit=4096):
    k = (limit - n) // (n - 1)
    return n + k * (n - 1), k

def linear_interpolate(arr, target_len):
    """
    线性插值扩展数组到指定长度，每两个数字之间插入的数量相等
    arr: 原数组 (1D)
    target_len: 目标长度 (>= len(arr))
    """
    arr = np.asarray(arr, dtype=float)
    n_points = len(arr)
    if target_len <= n_points:
        return arr

    # 每段插入的点数（不包括端点）
    intervals = n_points - 1
    total_insert = target_len - n_points
    insert_per_interval = total_insert // intervals
    remainder = total_insert % intervals

    result = []
    for i in range(intervals):
        start = arr[i]
        end = arr[i+1]
        # 当前段插值数量，分配余数
        n_insert = insert_per_interval + (1 if i < remainder else 0)
        # 当前段的插值（包括起点，不包括终点）
        segment = np.linspace(start, end, n_insert + 2)[:-1]
        result.extend(segment)
    result.append(arr[-1])
    return np.array(result)

def linear_interpolate_plateau_fix(arr, target_len):
    """
    线性插值扩展数组到指定长度。
    要求:
      1. (target_len - n_points) % (n_points - 1) == 0 才能保证每段插入数量相同；
         若不能整除，把余数依次分配在前面的若干段。
      2. 对出现两个或以上连续相等的数(plateau)，若其后紧跟一个不同值 v_next，
         则把这整段平坦区 + 紧随的那个不同值视作一个“大区间”做线性拆分。
         平坦区内部各原始间隔被赋予逐步递增(或递减)的子区间端点，避免重复值导致插值退化。
         若平坦区位于末尾(后面没有不同值)，保持原样。
    """
    arr = np.asarray(arr, dtype=float)
    n_points = len(arr)
    if target_len <= n_points:
        return arr.copy()

    intervals = n_points - 1
    total_insert = target_len - n_points
    base = total_insert // intervals
    remainder = total_insert % intervals  # 前 remainder 段每段多插 1 个

    # 预计算每段需要插入的点数
    inserts_per_interval = [base + (1 if i < remainder else 0) for i in range(intervals)]

    # 计算“有效”区间端点(处理平坦区)
    # 默认 start/end 就是相邻值
    effective_starts = np.empty(intervals, dtype=float)
    effective_ends = np.empty(intervals, dtype=float)

    i = 0
    while i < intervals:
        v0 = arr[i]
        v1 = arr[i + 1]
        if v0 != v1:
            # 普通区间
            effective_starts[i] = v0
            effective_ends[i] = v1
            i += 1
            continue

        # 平坦区开始 (arr[i] == arr[i+1])
        plateau_start = i
        # 扩展直到值改变或到末尾
        j = i + 1
        while j < n_points and arr[j] == v0:
            j += 1
        # 现在 plateau 索引范围 [plateau_start, j-1] (值都等于 v0)
        # 下一个不同值位置 j (如果 j < n_points)，对应的值 arr[j]
        if j < n_points:
            # 存在后续不同值，拆分为 (j - plateau_start) 个子区间
            plateau_intervals = j - plateau_start
            v_next = arr[j]
            # 在 v0 -> v_next 之间线性划分 plateau_intervals 份
            for k in range(plateau_intervals):
                idx = plateau_start + k
                t0 = k / plateau_intervals
                t1 = (k + 1) / plateau_intervals
                effective_starts[idx] = v0 + (v_next - v0) * t0
                effective_ends[idx] = v0 + (v_next - v0) * t1
            i = plateau_start + plateau_intervals
        else:
            # 平坦区到末尾(没有不同值)，保持原样
            for idx in range(plateau_start, intervals):
                effective_starts[idx] = arr[idx]
                effective_ends[idx] = arr[idx + 1]
            break  # 已到末尾

    # 生成结果
    result = []
    for idx in range(intervals):
        start = effective_starts[idx]
        end = effective_ends[idx]
        n_insert = inserts_per_interval[idx]
        # 该段需要输出: 起点 + n_insert 个内点 (不含终点，终点在下一段或最后统一追加)
        if n_insert == 0:
            # 只放起点
            result.append(start)
        else:
            segment = np.linspace(start, end, n_insert + 2)[:-1]  # 去掉终点
            result.extend(segment)

    # 最后追加最终端点
    result.append(arr[-1])
    return np.array(result, dtype=float)

def lut_scale(pq_values, scale):
    scale = float(scale)
    if scale <= 0:
        raise ValueError("scale 必须 > 0")
    pq_arr = np.array(pq_values, copy=True)
    scaled = np.clip(pq_arr * scale, 0.0, 1)
    return scaled


def generate_pq_lut(target_len=4096):
    return np.linspace(0, 1, target_len)

def generate_inversed_lut(lut):
    a = np.asarray(lut, dtype=float).ravel()
    if a.size < 2:
        raise ValueError("lut length must be >= 2")
    L = a.size - 1
    out = np.full(L + 1, np.nan, dtype=float)
    
    for i, y in enumerate(a):
        j = int(round(y * L))
        j = 0 if j < 0 else (L if j > L else j)
        out[j] = i / L
    
    nan_mask = np.isnan(out)
    if np.all(nan_mask):
        raise ValueError("generate reserverd lut failed: all values are NaN")
    
    # fallback linear interpolation for NaN positions
    pos = np.arange(L + 1)
    known_idx = pos[~nan_mask]
    known_val = out[~nan_mask]
    insert_pos = pos  
    j = np.searchsorted(known_idx, insert_pos, side="left")

    left_exist = j > 0
    left_idx = np.where(left_exist, known_idx[j - 1], -1)
    left_val = np.where(left_exist, known_val[j - 1], np.nan)
    dl = np.where(left_exist, insert_pos - left_idx, np.inf)

    right_exist = j < known_idx.size
    right_idx = np.where(right_exist, known_idx[j], -1)
    right_val = np.where(right_exist, known_val[j], np.nan)
    dr = np.where(right_exist, right_idx - insert_pos, np.inf)

    choose_left = dl <= dr
    filled = np.where(choose_left, left_val, right_val)

    out[nan_mask] = filled[nan_mask]

    return out

def generate_bright_pq_lut(target_len=4096):
    lut = generate_pq_lut(target_len)
    lut = np.clip(lut + 0.06307108, 0.0, 1.0)
    # lut = np.clip(lut * 1.1, 0.0, 1.0)
    return lut

def apply_sdr_tint_compensation(lut_r, lut_g, lut_b, r_scale=1.0, g_scale=1.0, b_scale=1.0,
                                sdr_pq_top=0.62, taper=0.15):
    """
    在 SDR 亮度区间（0 ~ sdr_pq_top PQ，约 0~320 nit）对 MHC2 的每通道 1D LUT
    施加平滑缩放，用于补偿该显示器在 HDR 模式下渲染 SDR 内容的系统性暖色偏差
    （经验值，依据校色报告统计：暖色偏亮、偏黄 → G 多降、R 少降、B 微降）。
    sdr_pq_top 以上经 taper 区间线性过渡回 1.0（不影响 HDR 高光）。
    返回 (lut_r, lut_g, lut_b)。
    """
    lut_r = np.asarray(lut_r, dtype=float).copy()
    lut_g = np.asarray(lut_g, dtype=float).copy()
    lut_b = np.asarray(lut_b, dtype=float).copy()
    n = len(lut_r)
    t = np.linspace(0, 1, n)
    w = np.clip((sdr_pq_top + taper - t) / max(float(taper), 1e-9), 0.0, 1.0)
    lut_r = np.clip(lut_r * (1.0 + (r_scale - 1.0) * w), 0.0, 1.0)
    lut_g = np.clip(lut_g * (1.0 + (g_scale - 1.0) * w), 0.0, 1.0)
    lut_b = np.clip(lut_b * (1.0 + (b_scale - 1.0) * w), 0.0, 1.0)
    return lut_r, lut_g, lut_b

def generate_mhc2_lut_from_measured_pq(real_pq, target_pq=None):
    """
    由实测灰阶响应反解出 Windows MHC2 的每通道 1D LUT（默认 4096 项）。

    信号方向（与本项目其余部分一致，见 README / CHANGELOG）
    ------------------------------------------------------
    实测数据 `real_pq[k]` 是「设备收到第 k 个均匀灰度码值时，实际输出的 PQ」，
    即显示器原生响应 `f` 在均匀码值上的采样：``real_pq = f(code_k)``，
    ``code_k = k/(n-1)``（k=0 最暗，k=n-1 最亮，码值即送入显示器的 PQ 输入）。

    MHC2 LUT 是这条响应的**反函数**：

        ``lut[i] = f⁻¹(目标输出 PQ = i/(4096-1))``

    即 LUT 的索引轴是「期望显示器输出的 PQ」，值是「应当送入显示器的 PQ 输入」。

    实现
    ----
    1. 单调化：``np.maximum.accumulate`` 抹掉测量噪声造成的回落（假定真实响应不随
       输入增加而下降；原始数组不会被就地修改）。
    2. 求逆：在 (输出 PQ, 输入码值) 上做分段线性逆插值（``np.interp``）。
       这取代了早期「线性重采样成 40960 点 + 最近邻反查」的做法：分段线性插值
       精确经过每一个实测点，且不依赖「重采样点数整除」这类脆弱假设。
    3. 超出实测范围的目标：低于最暗实测输出 → 0；高于最亮实测输出 → 截断到
       「实测最亮点所在的码值」（见下）。

    实测影响（用 hc.log 里 15 次真实灰阶 run 对比新旧算法）
    ------------------------------------------------------
    在显示器可达范围内（目标输出 PQ ≤ 实测峰值），新旧算法给出的 MHC2 表
    差异极小：**平均 0.006 个 10bit 码值、最大 0.013 个码值**（约 1e-5 PQ），
    远低于面板自身的重复性误差。也就是说旧实现的最近邻量化**本来就没有造成
    可见的台阶**——这次改动是让实现与数学定义一致、去掉脆弱的重采样/索引运算，
    而不是修一个用户看得见的 bug。旧实现在峰值以上还会给出比峰值更低的码值
    （见下），那才是这里真正修掉的行为。

    关于顶部截断（重要，不要改成「一律取 1.0」）
    ---------------------------------------------
    显示器的实测最亮点不一定在最后一个采样点上：实机 hc.log 数据里，曲线在
    code≈814 处达到峰值，之后由于 ABL/功率限制反而略微回落（峰值 PQ 0.80057 →
    末点 0.79955）。这段「越亮越暗」的响应是多对一的，其单调化反函数在峰值之后
    必须保持恒定，即所有高于峰值的目标输出都取峰值处的码值。
    若改成超峰目标一律取码值 1.0（即输入 PQ 1023），就会在整段高光里送出**实测
    亮度更低**的码值——那是真实的偏暗误差。所以这里显式取实测最亮点。

    参数:
      - real_pq: 序列或 ndarray，按输入码值从暗到亮的实测输出 PQ（0..1）。
        由 `XYZ_to_BT2020_PQ_rgb(实测 XYZ / 10000)` 得到（app.py::calibrate_pq）。
      - target_pq: 目标输出 PQ 轴；None 时取均匀 4096 点（即 i/(4096-1)）。

    返回:
      - np.ndarray, shape=(len(target_pq) 或 4096,), dtype=float64
        单调不减、值域 [0, 1]；`lut[0] == 0.0`。
        `lut[-1]` 取决于面板能否达到 PQ 1.0：实测末点就是峰值时为 1.0，否则为
        「实测峰值码值」（见上，显示器达不到的目标输出只能停在最亮码值上）。

    历史数据复用（README 第 6 条）不会改变这里的语义：复用的同样是该显示器的
    原生响应采样，只是采样时刻不同。
    """
    DEFAULT_LUT_LEN = 4096

    measured = np.asarray(real_pq, dtype=np.float64).ravel()
    if measured.size < 2:
        raise ValueError("generate_mhc2_lut_from_measured_pq 至少需要 2 个实测点")
    if not np.all(np.isfinite(measured)):
        raise ValueError("实测 PQ 曲线包含非有限值")

    # 1) 单调化（不就地修改调用方的数据；app.py 之后还要用原始曲线绘图）
    measured = np.maximum.accumulate(np.clip(measured, 0.0, 1.0))
    codes = np.linspace(0.0, 1.0, measured.size)

    if target_pq is None:
        target_pq = np.linspace(0.0, 1.0, DEFAULT_LUT_LEN)
    else:
        target_pq = np.asarray(target_pq, dtype=np.float64).ravel()

    # 2) 实测最亮点之后的响应是多对一的，反函数在那里必须保持恒定：
    #    只对峰值（含）之前的单调段求逆，超峰目标一律取峰值码值。
    peak_idx = 0
    for i in range(1, measured.size):
        if measured[i] > measured[peak_idx]:
            peak_idx = i
    if peak_idx < 1:
        # 退化曲线（完全测不到亮度变化，例如整条曲线都是 0）：没有可反解的信息，
        # 返回恒等 LUT（不改变任何码值），而不是抛异常或猜一条曲线。
        return np.clip(target_pq, 0.0, 1.0).copy()
    return np.interp(target_pq, measured[:peak_idx + 1], codes[:peak_idx + 1],
                     left=0.0, right=codes[peak_idx])


def eetf_from_lut(lut, eetf_args=None):
    """
    从现有的 LUT 生成 EETF 曲线。

    仅供 `tools/cyberpunk2077_hdr_fixer.py` 这类离线工具使用：它把一条已存在的
    MHC2 LUT 重新采样到 BT.2390 EETF 目标轴上。

    注意：**发布版校色路径不使用 EETF**。app.py 的 `calibrate_pq()` 只用实测响应
    反解 MHC2（`generate_mhc2_lut_from_measured_pq`），EETF 参数目前只影响
    `color_history.peak_min_luminance_from_xyz` 的峰值/黑场规则；历史上那个
    「按 EETF 重采样 + 最近邻反查」的实现（`generate_mhc2_lut_from_measure_data`）
    因为写错了端点、且与校色路径算法不一致，已经删除，不要再恢复。
    """
    TARGET_LEN = 4096
    idx_target = np.linspace(0, 1, TARGET_LEN)
    if eetf_args:
        Lb = eetf_args["source_min"]
        Lw = eetf_args["source_max"]
        Lmin = eetf_args["monitor_min"]
        Lmax = eetf_args["monitor_max"]
        idx_target_eetf = []
        for idx in range(len(idx_target)):
            V = idx_target[idx]
            idx_target_eetf.append(float(bt2390eetf(V, Lb, Lw, Lmin, Lmax)))
        idx_target = np.array(idx_target_eetf)
    if lut == [0, 1]:
        return idx_target
    len_lut = len(lut)
    convert_idx = []
    for itm in idx_target:
        idx= int(round(itm * (len_lut-1)))
        pq = lut[idx]
        convert_idx.append(pq)
    # When pq idx 0, turn off the mini‑LED backlight or power down the OLED.
    convert_idx[0] = 0
    return np.array(convert_idx)

# 示例：生成 LUT 数据并输出（可根据需要修改参数）
if __name__ == "__main__":
    lut = generate_pq_lut(128)
    generate_inversed_lut(lut)
