#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
color_math.py —— 色彩算法参考实现

对应手册 01–07 篇。仅依赖标准库与 numpy。
所有函数在非法输入下返回 None（标量函数），并且不产生副作用。

    import color_math as C
    C.xyz_to_lab(0.2, 0.5, 0.9, 0.9505, 1.0, 1.0890)

直接运行本文件会执行内置自检：
    python color_math.py
"""

from __future__ import annotations

import json
import math
import os

try:
    import numpy as np
except ImportError:  # numpy 仅色域矩阵部分需要
    np = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(os.path.dirname(_HERE), "data")

# ===========================================================================
# 常数
# ===========================================================================

# --- Lab / Luv 的 f 函数（01 篇 §4）---------------------------------------
EPS   = 0.008856                  # (6/29)^3 的工程近似
SLOPE = 7.787                     # 1/(3*eps^(2/3)) 的工程近似
ICPT  = 0.13793103448275862       # 精确 4/29
KAPPA = 903.292                   # Luv 低亮度斜率

C_116, C_16, C_500, C_200 = 116.0, 16.0, 500.0, 200.0
C_13, C_15, C_3, C_4, C_9 = 13.0, 15.0, 3.0, 4.0, 9.0

# --- 色温（02 篇）----------------------------------------------------------
HC       = 1.9864458571489286e-25
TWOPIHC2 = 3.741771852192758e-16
KB       = 1.38064903e-23
AIR      = 1.00028
AIR2     = 1.0005600784000002

A3, A2_, A1, A0 = -4.607,  2.9678, 0.09911, 0.244063     # 日光 4000..7000 K
B3, B2_, B1, B0 = -2.0064, 1.9018, 0.24748, 0.237040     # 日光 7000..25000 K

CCT_K3, CCT_K2, CCT_K1, CCT_K0 = 437.0, 3601.0, 6861.0, 5514.31
CCT_CA, CCT_CB = 0.332, 0.1858

CCT_T_MIN, CCT_T_GAP_LO, CCT_T_GAP_HI, CCT_T_MAX = 2000.0, 3500.0, 4000.0, 25000.0
CCT_T_SPLIT = 7000.0

# --- 传递函数（04 篇）------------------------------------------------------
PQ_M1   = 2610.0 / 16384.0
PQ_M2   = 2523.0 / 4096.0 * 32.0
PQ_C1   = 3424.0 / 4096.0
PQ_C2   = 2413.0 / 4096.0 * 32.0
PQ_C3   = 2392.0 / 4096.0 * 32.0
PQ_PEAK = 10000.0

HLG_A = 0.17883277
HLG_B = 1.0 - 4.0 * HLG_A
HLG_C = 0.5 - HLG_A * math.log(4.0 * HLG_A)

TABLE_QUANT = 65280.0             # 255 * 256


# ===========================================================================
# 01 · 色度坐标变换
# ===========================================================================

def xyz_to_xy(X, Y, Z):
    """(X,Y,Z) -> (x,y)。总和为 0 时返回 None。"""
    s = X + Y + Z
    if s == 0.0:
        return None
    return (X / s, Y / s)


def yxy_to_XZ(Y, x, y):
    """(Y,x,y) -> (X,Z)。y 为 0 时返回 None。"""
    if y == 0.0:
        return None
    return (Y * x / y, Y * (1.0 - x - y) / y)


def yxy_to_XYZ(Y, x, y):
    """(Y,x,y) -> (X,Y,Z)。"""
    xz = yxy_to_XZ(Y, x, y)
    if xz is None:
        return None
    return (xz[0], Y, xz[1])


def xyz_to_Yxy(X, Y, Z):
    """(X,Y,Z) -> (Y,x,y)。"""
    xy = xyz_to_xy(X, Y, Z)
    if xy is None:
        return None
    return (Y, xy[0], xy[1])


def xyz_to_uv1976(X, Y, Z):
    """(X,Y,Z) -> (u',v') CIE 1976。"""
    d = X + C_15 * Y + C_3 * Z
    if d == 0.0:
        return None
    return (4.0 * X / d, 9.0 * Y / d)


def xy_to_uv1976(x, y):
    """(x,y) -> (u',v')。"""
    d = -2.0 * x + 12.0 * y + 3.0
    if d == 0.0:
        return None
    return (4.0 * x / d, 9.0 * y / d)


def uv1976_to_xy(u, v):
    """(u',v') -> (x,y)。"""
    d = 6.0 * u - 16.0 * v + 12.0
    if d == 0.0:
        return None
    return (9.0 * u / d, 4.0 * v / d)


def _f_fwd(t):
    return t ** (1.0 / 3.0) if t > EPS else t * SLOPE + ICPT


def _f_inv(v):
    v3 = v * v * v
    return v3 if v3 > EPS else (v - ICPT) / SLOPE


def lab_hue(a, b, convention="B"):
    """
    色相角。convention='B' 得 (-pi/2, pi/2]（工程变体）；'A' 得 (-pi, pi]（CIE 标准）。
    两套约定在第一、三象限相同，第二、四象限差 pi —— 跨系统交换数据前务必确认。
    """
    if a == 0.0 and b == 0.0:
        return 0.0
    if a == 0.0:                      # 避免下面减 pi 时把 +-pi/2 拉偏
        return math.pi / 2 if b > 0 else -math.pi / 2
    if convention == "A":
        return math.atan2(b, a)
    h = math.atan(b / a)
    return h


def xyz_to_lab(X, Y, Z, Xn, Yn, Zn, hue_convention="B"):
    """(X,Y,Z) -> (L*, a*, b*, C*, h)。参考白点分量为 0 时返回 None。"""
    if Xn == 0.0 or Yn == 0.0 or Zn == 0.0:
        return None
    fx, fy, fz = _f_fwd(X / Xn), _f_fwd(Y / Yn), _f_fwd(Z / Zn)
    L = C_116 * fy - C_16
    a = C_500 * (fx - fy)
    b = C_200 * (fy - fz)
    return (L, a, b, math.hypot(a, b), lab_hue(a, b, hue_convention))


def lab_to_xyz(L, a, b, Xn, Yn, Zn):
    """(L*,a*,b*) -> (X,Y,Z)。"""
    fy = (L + C_16) / C_116
    fx = fy + a / C_500
    fz = fy - b / C_200
    return (_f_inv(fx) * Xn, _f_inv(fy) * Yn, _f_inv(fz) * Zn)


def xyz_to_luv(X, Y, Z, Xn, Yn, Zn):
    """(X,Y,Z) -> (L*, u*, v*)。"""
    yn = Y / Yn
    L = C_116 * (yn ** (1.0 / 3.0)) - C_16 if yn > EPS else KAPPA * yn
    D = X + C_15 * Y + C_3 * Z
    Dn = Xn + C_15 * Yn + C_3 * Zn
    if D == 0.0 or Dn == 0.0:
        return None
    u = C_13 * L * (4.0 * X / D - 4.0 * Xn / Dn)
    v = C_13 * L * (9.0 * Y / D - 9.0 * Yn / Dn)
    return (L, u, v)


def lab_to_lch(L, a, b, hue_convention="B"):
    return (L, math.hypot(a, b), lab_hue(a, b, hue_convention))


def lch_to_lab(L, C, h):
    return (L, C * math.cos(h), C * math.sin(h))


def white_point_to_XYZ(xn, yn, Yn=1.0):
    """参考白点色度 -> 三刺激值。"""
    if yn == 0.0:
        return None
    return (xn / yn * Yn, Yn, (1.0 - xn - yn) / yn * Yn)


# ===========================================================================
# 02 · 色温
# ===========================================================================

_cmf_cache = None


def load_cmf(path=None):
    """加载 CIE 1931 2° 色匹配函数表 -> [(lambda, xbar, ybar, zbar), ...]"""
    global _cmf_cache
    if _cmf_cache is None or path is not None:
        p = path or os.path.join(_DATA, "cie1931_2deg_1nm.json")
        with open(p, encoding="utf-8") as f:
            _cmf_cache = [tuple(r) for r in json.load(f)["rows"]]
    return _cmf_cache


def planck_exitance(lam_nm, T):
    """Planck 谱出射度（含空气修正）。"""
    lam = lam_nm * 1e-9 * AIR
    e = HC / (lam * KB * T)
    return TWOPIHC2 / (lam ** 5 * AIR2) / (math.exp(e) - 1.0)


def cct_to_xy_blackbody(T, cmf_rows=None):
    """黑体轨迹：Planck 积分 + 色匹配函数。"""
    rows = cmf_rows if cmf_rows is not None else load_cmf()
    sx = sy = sz = 0.0
    for lam, xb, yb, zb in rows:
        M = planck_exitance(lam, T)
        sx += M * xb
        sy += M * yb
        sz += M * zb
    s = sx + sy + sz
    if s == 0.0:
        return None
    return (sx / s, sy / s)


def daylight_x(T):
    """CIE 日光轨迹的 xD。"""
    if 4000.0 <= T <= CCT_T_SPLIT:
        return A3 * 1e9 / T ** 3 + A2_ * 1e6 / T ** 2 + A1 * 1e3 / T + A0
    if CCT_T_SPLIT < T <= CCT_T_MAX:
        return B3 * 1e9 / T ** 3 + B2_ * 1e6 / T ** 2 + B1 * 1e3 / T + B0
    raise ValueError("T outside daylight locus range")


def daylight_y(x):
    return -3.0 * x * x + 2.87 * x - 0.275


def cct_to_xy(T, cmf_rows=None):
    """
    色温 -> 色度坐标。

    分段：
        T < 2000                 返回 None
        2000 <= T < 3500         黑体轨迹
        3500 <= T < 4000         未定义，返回 None
        4000 <= T <= 25000       日光轨迹
        T > 25000                返回 None
    """
    if T < CCT_T_MIN or T > CCT_T_MAX:
        return None
    if CCT_T_GAP_LO <= T < CCT_T_GAP_HI:
        return None
    if T < CCT_T_GAP_LO:
        return cct_to_xy_blackbody(T, cmf_rows)
    x = daylight_x(T)
    return (x, daylight_y(x))


def xy_to_cct(x, y):
    """
    色度坐标 -> 相关色温（三次拟合）。

        t = (x - 0.332) / (y - 0.1858)
        CCT = ((437t 的补正)...

    注意：本式不含范围检查，只对靠近轨迹的色度有意义。
    """
    den = y - CCT_CB
    if den == 0.0:
        return None
    t = (x - CCT_CA) / den
    return ((CCT_K2 - CCT_K3 * t) * t - CCT_K1) * t + CCT_K0


def cct_distance_uv(x, y, cmf_rows=None, T_lo=2000.0, T_hi=25000.0, n=400):
    """
    色度点到黑体轨迹的最短 u'v' 距离，用于判断色温描述是否有效。
    建议阈值：< 0.05 可认为在轨迹附近。
    """
    uv = xy_to_uv1976(x, y)
    if uv is None:
        return None
    best = None
    for i in range(n + 1):
        T = T_lo + (T_hi - T_lo) * i / n
        if CCT_T_GAP_LO <= T < CCT_T_GAP_HI:
            continue
        p = cct_to_xy(T, cmf_rows)
        if p is None:
            continue
        quv = xy_to_uv1976(p[0], p[1])
        if quv is None:
            continue
        d = math.hypot(uv[0] - quv[0], uv[1] - quv[1])
        if best is None or d < best:
            best = d
    return best


# ===========================================================================
# 04 · 传递函数
# ===========================================================================

def gamma_eotf(V, g=2.2):
    return V ** g


def srgb_eotf(V):
    return V / 12.92 if V <= 0.04045 else ((V + 0.055) / 1.055) ** 2.4


def srgb_oetf(L):
    return L * 12.92 if L <= 0.0031308 else 1.055 * (L ** (1.0 / 2.4)) - 0.055


def rec709_inv(V):
    """Rec.709 OETF 的逆：编码值 -> 线性亮度。"""
    return V / 4.5 if V < 0.081 else ((V + 0.099) / 1.099) ** (1.0 / 0.45)


def pq_eotf(N):
    """ST 2084 PQ EOTF：归一化编码 -> 绝对亮度（cd/m²，峰值 10000）。"""
    t = N ** (1.0 / PQ_M2)
    num = max(t - PQ_C1, 0.0)
    den = PQ_C2 - PQ_C3 * t
    return (num / den) ** (1.0 / PQ_M1) * PQ_PEAK


def pq_inv(L_abs):
    """PQ 逆变换：绝对亮度 -> 归一化编码。"""
    p = (L_abs / PQ_PEAK) ** PQ_M1
    return ((PQ_C1 + PQ_C2 * p) / (1.0 + PQ_C3 * p)) ** PQ_M2


def hlg_eotf(V):
    """BT.2100 HLG EOTF：编码 -> 相对亮度。"""
    if V <= 0.5:
        return V * V / 3.0
    return (math.exp((V - HLG_C) / HLG_A) + HLG_B) / 12.0


def hlg_inv(L):
    if L <= 1.0 / 12.0:
        return math.sqrt(3.0 * L)
    return HLG_A * math.log(12.0 * L - HLG_B) + HLG_C


def build_transfer_table(func, n=256, peak=None, quantise=None):
    """
    生成传递函数查找表。

    func     : 单参数曲线
    n        : 表长，须为 256 或 1024
    peak     : 若给定，结果按 10000/peak 归一化并裁到 1.0（用于 PQ 这类绝对亮度曲线）
    quantise : 若给定（如 65535），额外返回量化后的整数表
    """
    if n not in (256, 1024):
        raise ValueError("n must be 256 or 1024")
    if peak is not None and not (0.0 < peak <= PQ_PEAK):
        raise ValueError("peak out of range")
    out = []
    for i in range(n):
        v = func(i / (n - 1.0))
        if peak is not None:
            v = min(v * (PQ_PEAK / peak), 1.0)
        if quantise is not None:
            v = round(v * quantise) / quantise
        out.append(v)
    return out


def standard_transfer_table(index, i=None):
    """
    第 index 张标准传递函数表的第 i 项（i 为 None 时返回整表）。

    量化规则：round(f(i/255) * 65280) / 65280
    """
    funcs = {
        0: lambda x: x ** 2.2, 6: lambda x: x ** 2.2,
        1: srgb_eotf,
        2: lambda x: x ** 2.35,
        3: rec709_inv, 5: rec709_inv,
        4: lambda x: x ** 2.4,
        7: lambda x: x ** 2.6,
    }
    if index not in funcs:
        raise ValueError("index out of range")
    f = funcs[index]
    if i is None:
        return [round(f(k / 255.0) * TABLE_QUANT) / TABLE_QUANT for k in range(256)]
    return round(f(i / 255.0) * TABLE_QUANT) / TABLE_QUANT


# ===========================================================================
# 05 · 色差
# ===========================================================================

def delta_e_76(lab1, lab2):
    """CIE76：Lab 欧氏距离。"""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(lab1, lab2)))


def delta_e_94(lab1, lab2, K1=0.045, K2=0.015, kL=1.0, kC=1.0, kH=1.0):
    """CIE94（默认图形艺术参数）。注意：公式不对称，参考色应放在第一位。"""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    dL = L1 - L2
    dC = C1 - C2
    dH2 = (a1 - a2) ** 2 + (b1 - b2) ** 2 - dC ** 2
    dH = math.sqrt(max(dH2, 0.0))
    SL, SC, SH = 1.0, 1.0 + K1 * C1, 1.0 + K2 * C1
    return math.sqrt((dL / (kL * SL)) ** 2 +
                     (dC / (kC * SC)) ** 2 +
                     (dH / (kH * SH)) ** 2)


def delta_e_2000(lab1, lab2, kL=1.0, kC=1.0, kH=1.0):
    """CIEDE2000。"""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2

    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    Cb = 0.5 * (C1 + C2)
    Cb7 = Cb ** 7
    G = 0.5 * (1.0 - math.sqrt(Cb7 / (Cb7 + 25.0 ** 7)))

    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2
    C1p = math.hypot(a1p, b1)
    C2p = math.hypot(a2p, b2)

    h1p = math.degrees(math.atan2(b1, a1p)) % 360.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p

    if C1p * C2p == 0.0:
        dhp = 0.0
    else:
        d = h2p - h1p
        if d > 180.0:
            dhp = d - 360.0
        elif d < -180.0:
            dhp = d + 360.0
        else:
            dhp = d
    dHp = 2.0 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp) / 2.0)

    Lbp = 0.5 * (L1 + L2)
    Cbp = 0.5 * (C1p + C2p)

    if C1p * C2p == 0.0:
        hbp = h1p + h2p
    else:
        s = h1p + h2p
        if abs(h1p - h2p) <= 180.0:
            hbp = 0.5 * s
        elif s < 360.0:
            hbp = 0.5 * (s + 360.0)
        else:
            hbp = 0.5 * (s - 360.0)

    T = (1.0 - 0.17 * math.cos(math.radians(hbp - 30.0))
         + 0.24 * math.cos(math.radians(2.0 * hbp))
         + 0.32 * math.cos(math.radians(3.0 * hbp + 6.0))
         - 0.20 * math.cos(math.radians(4.0 * hbp - 63.0)))

    SL = 1.0 + 0.015 * (Lbp - 50.0) ** 2 / math.sqrt(20.0 + (Lbp - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cbp
    SH = 1.0 + 0.015 * Cbp * T

    dth = 30.0 * math.exp(-(((hbp - 275.0) / 25.0) ** 2))
    Cbp7 = Cbp ** 7
    RC = 2.0 * math.sqrt(Cbp7 / (Cbp7 + 25.0 ** 7))
    RT = -math.sin(math.radians(2.0 * dth)) * RC

    t1 = dLp / (kL * SL)
    t2 = dCp / (kC * SC)
    t3 = dHp / (kH * SH)
    return math.sqrt(t1 * t1 + t2 * t2 + t3 * t3 + RT * t2 * t3)


def delta_e_uv(luv1, luv2):
    """CIE 1976 Luv 色差。"""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(luv1, luv2)))


def delta_uv_prime(xy1, xy2):
    """仅色度差（u'v' 平面上）。"""
    a = xy_to_uv1976(*xy1)
    b = xy_to_uv1976(*xy2)
    if a is None or b is None:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


def judd_vos(x, y):
    """Judd-Vos 短波段 CMU 修正。"""
    den = 0.03845 * x + 0.01496 * y + 1.0
    if den == 0.0:
        return None
    xp = (1.0271 * x - 0.00008 * y - 0.00009) / den
    yp = (0.00376 * x + 1.0072 * y + 0.00764) / den
    return (xp, yp)


# ===========================================================================
# 06 · 矩阵与色域
# ===========================================================================

def _require_numpy():
    if np is None:
        raise RuntimeError("channel matrix helpers need numpy")


def primaries_to_matrix(prim, white, normalize="Y"):
    """
    由三原色与白点色度构造 RGB->XYZ 矩阵（列 = R/G/B 的 XYZ 向量）。

    prim    : ((xr,yr), (xg,yg), (xb,yb))
    white   : (xw, yw)
    normalize: 'Y'（用第二行元素和归一化整个矩阵）或 'row' 或 None
    """
    _require_numpy()

    def unit(x, y):
        return np.array([x / y, 1.0, (1.0 - x - y) / y], dtype=float)

    A = np.column_stack([unit(*prim[0]), unit(*prim[1]), unit(*prim[2])])
    S = np.linalg.solve(A, unit(*white))
    # 注意：必须按**列**缩放。numpy 的 `A * S` 会沿最后一个轴广播（即按行乘），是错的。
    M = A * S[np.newaxis, :]
    if normalize == "Y":
        return normalize_by_Y(M)
    if normalize == "row":
        return normalize_by_row(M)
    return M


def normalize_by_Y(M):
    """用一个标量（第二行元素之和）除全部 9 个元素。和 <= 0 时原样返回。"""
    _require_numpy()
    M = np.array(M, dtype=float, copy=True)
    s = M[1, 0] + M[1, 1] + M[1, 2]
    if s > 0.0:
        M = M / s
    return M


def normalize_by_row(M):
    """每行各自除以本行元素之和。"""
    _require_numpy()
    M = np.array(M, dtype=float, copy=True)
    for i in range(3):
        s = M[i].sum()
        if s > 0.0:
            M[i] = M[i] / s
    return M


def rgb_to_xyz(rgb, M):
    _require_numpy()
    return np.asarray(M, dtype=float) @ np.asarray(rgb, dtype=float)


def xyz_to_rgb(XYZ, M):
    _require_numpy()
    return np.linalg.solve(np.asarray(M, dtype=float), np.asarray(XYZ, dtype=float))


def in_gamut(rgb, tol=1e-9):
    lo, hi = min(rgb), max(rgb)
    return lo >= -tol and hi <= 1.0 + tol


def clip_to_gamut(rgb):
    return tuple(0.0 if v < 0.0 else (1.0 if v > 1.0 else v) for v in rgb)


# ===========================================================================
# 数据表加载
# ===========================================================================

def load_white_points():
    with open(os.path.join(_DATA, "white_points.json"), encoding="utf-8") as f:
        d = json.load(f)["white_points"]
    return {int(k): tuple(v) for k, v in d.items()}


def load_gamuts():
    with open(os.path.join(_DATA, "gamuts.json"), encoding="utf-8") as f:
        d = json.load(f)["gamuts"]
    return {int(k): {k2: tuple(v2) for k2, v2 in v.items()} for k, v in d.items()}


# ===========================================================================
# 自检
# ===========================================================================

def _close(a, b, tol=1e-9):
    if isinstance(a, (list, tuple)):
        return all(_close(x, y, tol) for x, y in zip(a, b))
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol * max(1.0, abs(b))


def _self_test():
    ok = fail = 0

    def chk(name, got, want, tol=1e-9):
        nonlocal ok, fail
        if _close(got, want, tol):
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {name}\n       got  {got}\n       want {want}")

    print("01 坐标变换")
    chk("xyz_to_xy(D65)", xyz_to_xy(0.9505, 1.0, 1.0890),
        (0.3127159072215825, 0.3290014805066623))
    chk("xyz_to_xy(black)", xyz_to_xy(0, 0, 0), None)
    chk("yxy_to_XZ", yxy_to_XZ(0.5, 0.2, 0.5), (0.2, 0.30000000000000004))
    chk("xyz_to_lab(D65)", xyz_to_lab(0.9505, 1.0, 1.0890, 0.9505, 1.0, 1.0890)[:4],
        (100.0, 0.0, 0.0, 0.0))
    chk("xyz_to_lab(0,0,0 wp)", xyz_to_lab(1, 1, 1, 0, 1, 1), None)
    r = xyz_to_lab(0.2, 0.5, 0.9, 0.9505, 1.0, 1.0890)
    chk("xyz_to_lab(0.2,0.5,0.9)", r[:4],
        (76.06926101415557, -99.45825324868119, -28.947188522519518, 103.58515271329713))
    chk("lab round trip", lab_to_xyz(100.0, 0.0, 0.0, 0.9505, 1.0, 1.0890),
        (0.9505, 1.0, 1.0890))
    chk("hue convention B (Q2)", lab_hue(-33.7749199837, 123.0188190643, "B"),
        -1.3028477453, 1e-6)
    chk("hue convention B (Q4)", lab_hue(35.9530915640, -58.8981849493, "B"),
        -1.0227445615, 1e-6)
    chk("uv1976 round trip", uv1976_to_xy(*xy_to_uv1976(0.3127, 0.3290)),
        (0.3127, 0.3290), 1e-12)

    print("02 色温")
    chk("xy_to_cct(D65)", xy_to_cct(0.3127, 0.3290), 6459.28, 0.01)
    chk("xy_to_cct(0.24,0.30)", xy_to_cct(0.24, 0.30), 13607.08187894203)
    chk("cct_to_xy(4500)", cct_to_xy(4500.0),
        (0.36208854183813444, 0.37086977868404647))
    chk("cct_to_xy(8250)", cct_to_xy(8250.0),
        (0.29140635445362717, 0.30658324703405104))
    chk("cct_to_xy(1500)", cct_to_xy(1500.0), None)
    chk("cct_to_xy(3750)", cct_to_xy(3750.0), None)
    chk("cct_to_xy(30000)", cct_to_xy(30000.0), None)
    bb = cct_to_xy(2856.0)          # 黑体路径（Illuminant A）
    chk("cct_to_xy(2856) x", bb[0], 0.4476, 5e-3)

    print("04 传递函数")
    chk("srgb_eotf(0.5)", srgb_eotf(0.5), 0.21404114048223255)
    chk("gamma 2.2 at 0.5", gamma_eotf(0.5, 2.2), 0.21763764082403100)
    chk("pq_eotf(1)", pq_eotf(1.0), PQ_PEAK)
    chk("pq_eotf(0)", pq_eotf(0.0), 0.0)
    chk("pq_inv(10000)", pq_inv(PQ_PEAK), 1.0)
    chk("pq round trip", pq_inv(pq_eotf(0.5)), 0.5)
    chk("pq c1 identity", PQ_C1, PQ_C3 - PQ_C2 + 1.0)
    chk("hlg_eotf(0.5)", hlg_eotf(0.5), 1.0 / 12.0)
    # 常数 a 为截断值，V=1 时会略高于 1（约 2.7e-8），属预期
    chk("hlg_eotf(1)", hlg_eotf(1.0), 1.0, 1e-7)
    chk("hlg round trip", hlg_inv(hlg_eotf(0.7)), 0.7, 1e-9)
    chk("std table last", standard_transfer_table(1, 255), 1.0)
    t = standard_transfer_table(0)
    chk("std table monotonic", all(t[i] <= t[i + 1] for i in range(255)), True)
    chk("std table idx128", standard_transfer_table(0, 128), 0.21951593137254902)

    print("05 色差")
    # CIEDE2000 参考向量（kL=kC=kH=1）
    sharma = [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
        ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
    ]
    for i, (a, b, want) in enumerate(sharma, 1):
        chk(f"dE2000 vector #{i}", delta_e_2000(a, b), want, 1e-3)
    chk("dE2000 self", delta_e_2000((50, 1, 2), (50, 1, 2)), 0.0)
    # 纯明度差可解析验证：SL = 1 + 0.015*25/sqrt(45)，dE = 10/SL
    chk("dE2000 L-only", delta_e_2000((50, 0, 0), (60, 0, 0)),
        10.0 / (1.0 + 0.015 * 25.0 / math.sqrt(45.0)), 1e-12)
    chk("dE2000 symmetric",
        delta_e_2000((50, 2.6772, -79.7751), (60, 0, -82.7485)),
        delta_e_2000((60, 0, -82.7485), (50, 2.6772, -79.7751)))
    chk("judd_vos", judd_vos(0.3127, 0.3290),
        (0.31570812685567096, 0.33451612442167544))

    print("06 矩阵")
    if np is not None:
        # Adobe RGB (1998)：G 原色为 (0.21, 0.71)
        M = primaries_to_matrix(
            ((0.64, 0.33), (0.21, 0.71), (0.15, 0.06)), (0.3127, 0.3290))
        # 注意：矩阵元素对白点/原色的位数很敏感。用 2 位小数的色度（0.3127, 0.3290）
        # 与公开矩阵表（可能取自更高精度白点）在第 5 位小数会有约 3e-5 的差异，
        # 因此这里只比对到 1e-4。
        chk("AdobeRGB Y row", [float(v) for v in M[1]],
            [0.2973614, 0.6273558, 0.0752828], 1e-4)
        chk("normalize_by_Y row sum", float(M[1].sum()), 1.0)
        Mr = normalize_by_row(M)
        chk("normalize_by_row", [float(Mr[i].sum()) for i in range(3)], [1.0, 1.0, 1.0])
        # Rec.709 / sRGB：G 原色为 (0.30, 0.60)
        Ms = primaries_to_matrix(
            ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)), (0.3127, 0.3290))
        chk("Rec.709 Y row", [float(v) for v in Ms[1]],
            [0.2126729, 0.7151522, 0.0721750], 1e-4)
        chk("in_gamut false", in_gamut((1.1, 0.5, 0.5)), False)
        chk("in_gamut true", in_gamut((0.5, 0.5, 0.5)), True)
    else:
        print("  (numpy 缺失，跳过矩阵自检)")

    print(f"\n通过 {ok} / 失败 {fail}")
    return fail == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
