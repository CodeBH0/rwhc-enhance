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

def generate_mhc2_lut_from_measure_data(real_nit, target_pq=None, max_nit=10000, ratio=1, eetf_args=None):
    """
    依据实测灰阶亮度曲线生成 PQ→PQ 的 1D LUT来校准显示器亮度响应（长度 4096）。
    原理：用实测曲线（输入PQ→设备输出PQ）求其“近似反函数”，再按目标曲线（可选 BT.2390 EETF）
    在 [0..1] 上采样，得到前向补偿 LUT：给定目标输出PQ，返回需要送入设备的输入PQ。

    参数:
      - real_nit: list[float] | np.ndarray
          实测灰阶亮度（cd/m²），按输入码值从暗到亮采样（建议均匀 PQ 步进）。
          函数内部会做非递减处理以去噪。
      - max_nit: float = 10000
          亮度上限裁剪（cd/m²）。实测值高于该值时按该上限换算为 PQ（防止越界）。
      - ratio: float = 1
          亮度缩放比。若你的 real_nit 不是以 cd/m² 存储，可用 ratio 做换算（使用 L/ratio）。
      - eetf_args: dict | None
          若提供则使用 BT.2390 EETF 定义目标输出曲线。需包含:
            {
              "source_min": 母带黑位 (nit),
              "source_max": 母带峰值白 (nit), 
              "monitor_min": 设备黑位 (nit),
              "monitor_max": 设备峰值白 (nit)
            }
          若为 None，则目标曲线为线性 PQ ramp（恒等目标）。

    返回:
      - np.ndarray, shape=(4096,), dtype=float
          1D LUT：索引 i 表示“目标输出 PQ”= i/4095，对应的值为“应该送入设备的输入 PQ”。
    """
    DEFAULT_LUT_LEN = 4096
    real_nit = copy.deepcopy(real_nit)
    # 去除噪声(假定显示器实际响应不会随着输入亮度增加而下降)
    for idx, itm in enumerate(real_nit):
        if idx == 0:
            continue
        if itm < real_nit[idx-1]:
            real_nit[idx] = real_nit[idx-1]
    monitor_real_pq = []
    max_pq = pq_oetf(max_nit/ratio)
    for idx, itm in enumerate(real_nit):
        if itm <= max_nit:
            pq = pq_oetf(itm/ratio)
        else:
            pq = max_pq
        monitor_real_pq.append(pq)
    if not target_pq:
        target_pq = np.linspace(0, 1, DEFAULT_LUT_LEN)
    else:
        target_pq = np.array(target_pq, dtype=float)
    # target_pq = np.clip(target_pq*1.1, 0.0, 1.0)
    if eetf_args:
        target_pq_eetf = []
        lt = len(target_pq)
        for idx in range(lt):
            V = idx/(lt-1)
            Lb = eetf_args["source_min"]
            Lw = eetf_args["source_max"]
            Lmin = eetf_args["monitor_min"]
            Lmax = eetf_args["monitor_max"]
            NV_index = int(round(float(bt2390eetf(V, Lb, Lw, Lmin, Lmax))*(lt-1)))
            NV = target_pq[NV_index]
            target_pq_eetf.append(NV)
        target_pq = np.array(target_pq_eetf)
    
    m, k1 = max_uniform_target(len(monitor_real_pq), DEFAULT_LUT_LEN*10)
    monitor_real_pq = linear_interpolate(np.array(monitor_real_pq), m)
    convert_idx = []
    len_idx_real = m
    for itm in target_pq:
        idx= find_nearest_idx(monitor_real_pq, itm)
        pq = idx/(len_idx_real-1)
        convert_idx.append(pq)
    if not eetf_args:
        convert_idx[0] = 0
        convert_idx[1] = 1
    return np.array(convert_idx)


def generate_mhc2_lut_from_measured_pq(real_pq, target_pq=None):
    DEFAULT_LUT_LEN = 4096
    for idx, itm in enumerate(real_pq):
        if idx == 0:
            continue
        if itm < real_pq[idx-1]:
            real_pq[idx] = real_pq[idx-1]

    if not target_pq:
        target_pq = np.linspace(0, 1, DEFAULT_LUT_LEN)
    else:
        target_pq = np.array(target_pq, dtype=float)
    
    m, k1 = max_uniform_target(len(real_pq), DEFAULT_LUT_LEN*10)
    real_pq = linear_interpolate(np.array(real_pq), m)
    convert_idx = []
    len_idx_real = m
    for itm in target_pq:
        idx= find_nearest_idx(real_pq, itm)
        pq = idx/(len_idx_real-1)
        convert_idx.append(pq)
    return np.array(convert_idx)


def eetf_from_lut(lut, eetf_args=None):
    """
    从现有的 LUT 生成 EETF 曲线
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
