# -*- coding: utf-8 -*-
from meta_data import *
from convert_utils import *
from matrix import *
import logging
import numpy as np
from lut import pq_oetf, pq_eotf 


def build_device_M_from_measured(XYZ_R, XYZ_G, XYZ_B, XYZ_W, XYZ_black=None):
    """
    输入：实测的纯红/纯绿/纯蓝/白（同一窗口/APL）的 XYZ。
         可选：黑场 XYZ，用于扣黑。
    输出：M (3x3) 设备矩阵（RGB -> XYZ），满足 M@[1,1,1]=W (已扣黑)。
    """
    R = np.array(XYZ_R, float)
    G = np.array(XYZ_G, float)
    B = np.array(XYZ_B, float)
    W = np.array(XYZ_W, float)
    if XYZ_black is not None:
        K = np.array(XYZ_black, float)
        R, G, B, W = R - K, G - K, B - K, W - K

    M0 = np.column_stack([R, G, B])       # 3x3，列分别是 R/G/B 的绝对 XYZ
    # 列缩放，使得 M@[1,1,1]=W（白锁定）
    s = np.linalg.solve(M0, W)            # 若奇异会抛异常
    M = M0 @ np.diag(s)
    return M

def ymax_for_xy_with_M(M_device, xy, caps=(1.0, 1.0, 1.0), tol=1e-12):
    """
    设备线性坐标系（绝对单位）下，给定色度 (x,y) 的最大可显示亮度（nit）。
    M_device: 3x3 实测 RGB->XYZ（绝对单位）
    xy: (x, y)
    caps: 每通道线性上限（考虑提前夹顶；默认全 1）
    """
    M = np.array(M_device, float)
    Minv = np.linalg.inv(M)
    x, y = xy
    if y <= 0:
        return 0.0

    # 每 1 nit 该色所需的设备线性 RGB
    X_unit = np.array([x/y, 1.0, (1-x-y)/y], float)
    r_perY = Minv @ X_unit

    # 色域外：需要负通道
    if np.any(r_perY < -tol):
        return 0.0

    # 仅正分量限制缩放
    pos = r_perY > tol
    if not np.any(pos):
        return 0.0

    caps = np.array(caps, float)
    Y_max = np.min(caps[pos] / r_perY[pos])  
    return float(max(0.0, Y_max))

def ymax_for_many_with_M(M_device, xys, caps=(1.0, 1.0, 1.0), tol=1e-12):
    """批量版本：xys 为 [(x1,y1), (x2,y2), ...]，返回 numpy.array([Ymax...])"""
    return np.array([ymax_for_xy_with_M(M_device, xy, caps, tol) for xy in xys], dtype=float)


def ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, xy, caps=(1.0,1.0,1.0)):
    """
    用色域定义（原色+白点）与设定的白亮度 Yw，计算给定 xy 的最大亮度。
    返回单位与 Yw 一致（例如 nits，或相对 Y=1）。
    """
    try:
        M = build_rgb_to_xyz_from_primaries(xy_R, xy_G, xy_B, xy_W)
    except np.linalg.LinAlgError:
        return 0.0
    return ymax_for_xy_with_M(M, xy, caps)

def ymax_many_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, xys, caps=(1.0,1.0,1.0)):
    try:
        M = build_rgb_to_xyz_from_primaries(xy_R, xy_G, xy_B, xy_W)
    except np.linalg.LinAlgError:
        return np.zeros(len(xys), dtype=float)
    return np.array([ymax_for_xy_with_M(M, xy, caps) for xy in xys], dtype=float)

# 人眼敏感色 
sRGB_test_colors_xy = [
    (0.389, 0.365),   # 肤色（中性）
    (0.322, 0.510),   # 草地绿
    (0.388, 0.487),   # 叶绿偏黄
    (0.235, 0.263),   # 天空蓝
    (0.237, 0.337),   # 海洋青蓝
    (0.577, 0.322),   # 中亮红
    (0.421, 0.480),   # 高亮黄
    (0.274, 0.174),   # 暗紫
    (0.59049931, 0.34674064),   # 0.8饱和度sRGB红
    (0.31165312, 0.54200542),   # 0.8饱和度sRGB绿
    (0.17143554, 0.0812876),   # 0.8饱和度sRGB蓝
]

P3D65_test_colors_xy = [
    (0.624, 0.370),     # 深橙（更靠红）
    (0.480, 0.499),     # 鲜亮黄
    (0.311, 0.649),     # 鲜黄绿（Chartreuse）
    (0.243, 0.571),     # 青绿（靠绿的青）
    (0.550, 0.256),     # 洋红（靠红的紫）
    (0.63655244, 0.34614053), # 0.8饱和度P3红
    (0.29110012, 0.61804697), # 0.8饱和度P3绿
]

# ============================================================================
# 24 色行业标准色卡：X-Rite ColorChecker Classic（即 Macbeth ColorChecker）
# 参考 Danny Pascale, "RGB coordinates of the Macbeth ColorChecker" (2006)：
# D65 照明、sRGB 编码下的 24 个标准色块（业界最常用的 24 色卡定义）。
# 每项：(名称, R, G, B)（0-255 sRGB 码值）。
# ============================================================================
ColorChecker24_srgb = [
    ("dark_skin",        115,  82,  68),
    ("light_skin",       194, 150, 130),
    ("blue_sky",          98, 122, 157),
    ("foliage",           87, 108,  67),
    ("blue_flower",      133, 128, 177),
    ("bluish_green",     103, 189, 170),
    ("orange",           214, 126,  44),
    ("purplish_blue",     80,  91, 166),
    ("moderate_red",     193,  90,  99),
    ("purple",            94,  60, 108),
    ("yellow_green",     157, 188,  64),
    ("orange_yellow",    224, 163,  46),
    ("blue",              56,  61, 150),
    ("green",             70, 148,  73),
    ("red",              175,  54,  60),
    ("yellow",           231, 198,  31),
    ("magenta",          187,  86, 149),
    ("cyan",               8, 133, 161),
    ("white_9.5",        243, 243, 242),
    ("neutral_8",        200, 200, 200),
    ("neutral_6.5",      160, 160, 160),
    ("neutral_5",        122, 122, 121),
    ("neutral_3.5",       85,  85,  85),
    ("black_2",           52,  52,  52),
]

# 标准 sRGB (D65) -> XYZ 矩阵（IEC 61966-2-1），白点 Y=1
SRGB_TO_XYZ_MATRIX = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])

def srgb_rgb255_to_XYZ_norm(rgb255):
    """
    sRGB 0-255 码值 -> 相对 XYZ（白点 (255,255,255) 的 Y=1）。
    采用 IEC 61966-2-1 的 sRGB OETF 与 D65 矩阵。
    """
    c = np.clip(np.asarray(rgb255, dtype=float) / 255.0, 0.0, 1.0)
    lin = srgb_encode(c)
    return SRGB_TO_XYZ_MATRIX @ lin

# 24 色卡的色度（xy），供测量套件等复用
sRGB_test_colors_xy_24 = [
    tuple(XYZ_to_xy(srgb_rgb255_to_XYZ_norm(rgb)).tolist())
    for _, *rgb in ColorChecker24_srgb
]

# 色卡挡位（GUI "Color sample set" 下拉框取值）
COLOR_CARD_SRGB_12    = "sRGB(12)"
COLOR_CARD_SRGB_P3_12 = "sRGB(12)+DisplayP3(7)"
COLOR_CARD_SRGB_24    = "sRGB(24)"
COLOR_CARD_SRGB_P3_24 = "sRGB(24)+DisplayP3(7)"
COLOR_CARD_CHOICES = [
    COLOR_CARD_SRGB_12,
    COLOR_CARD_SRGB_P3_12,
    COLOR_CARD_SRGB_24,
    COLOR_CARD_SRGB_P3_24,
]

def pq_uniform_test_suit(xy, Ymin, Ymax, count):
    """
    Given chromaticity (x,y) and luminance range [Ymin, Ymax] in nits,
    generate 'count' perceptually-uniform (PQ-uniform) samples of that chromaticity.
    Returns an array of shape (count, 3) with XYZ for each sample.
    """
    x, y = float(xy[0]), float(xy[1])
    if y <= 0.0:
        raise ValueError("y must be > 0")
    Ymin = float(Ymin); Ymax = float(Ymax)
    if Ymin > Ymax:
        Ymin, Ymax = Ymax, Ymin
    # clamp to PQ domain [0, 10000] nits
    Ymin = max(0.0, min(1.0, Ymin))
    Ymax = max(0.0, min(1.0, Ymax))
    if count <= 0:
        return np.zeros((0, 3), dtype=float)
    if count == 1:
        # pick PQ-midpoint for a single sample
        E = 0.5 * (pq_oetf(Ymin*10000) + pq_oetf(Ymax*10000))
        Ys = np.array([pq_eotf(E)], dtype=float)
    else:
        Emin = pq_oetf(Ymin*10000)
        Emax = pq_oetf(Ymax*10000)
        Es = np.linspace(Emin, Emax, int(count))
        Ys = pq_eotf(Es)  # back to absolute nits

    # xyY -> XYZ, vectorized
    # X = x/y * Y, Z = (1-x-y)/y * Y
    invy = 1.0 / y
    Xs = x * invy * Ys
    Zs = (1.0 - x - y) * invy * Ys
    XYZ = np.stack([Xs, Ys, Zs], axis=1)/10000.0 
    return XYZ

def get_D65_white_calibrate_test_XYZ_suit(color_gamut, paper_white_nit=200):
    # 返回白场的测试集
    # paper_white_nit: SDR 纸白亮度（nit）。默认 200 保持旧行为；
    # 建议传入系统实际报告的纸白（Windows HDR 的 SDR 内容亮度），
    # 使色度校准锚定在实际使用的 SDR 白亮度上。
    Y_max = color_gamut["white"][1]
    Y = min(Y_max * 0.8, paper_white_nit)
    
    return [xyY_to_XYZ([*D65_WHITE_POINT, Y])]

def get_D65_white_measure_test_XYZ_suit(color_gamut):
    # 返回白场的测试集
    Y_max = color_gamut["white"][1]
    Y_min = 0.005
    x = D65_WHITE_POINT[0]
    y = D65_WHITE_POINT[1]
    suit = pq_uniform_test_suit(xy=(x,y), Ymin=Y_min, Ymax=Y_max*0.8, count=10).tolist()
    return suit

def get_srgb_calibrate_XYZ_suit(color_gamut, paper_white_nit=200):
    """
    返回 sRGB 色域内校准用测试集。这里按“色域定义”构矩阵：
    - 原色/白点：color_gamut中定义
    - 白亮度：由 get_white_test_XYZ_suit 提供的 Y (nit)
    """
    xy_R = XYZ_to_xy(color_gamut["red"])
    xy_G = XYZ_to_xy(color_gamut["green"])
    xy_B = XYZ_to_xy(color_gamut["blue"])
    xy_W = XYZ_to_xy(color_gamut["white"])

    ret = []
    for white in get_D65_white_calibrate_test_XYZ_suit(color_gamut, paper_white_nit):
        white = [itm*10000.0 for itm in white]  # 转为 nit
        Yw = float(white[1])                    # 白的亮度（nits）
        caps = (1.0, 1.0, 1.0)
        for x, y in sRGB_test_colors_xy:
            Y_max = ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, (x, y), caps)*Yw
            if Y_max == 0:
                logging.info("skip Ymax=0 srgb %s %s", x, y)
                continue
            XYZ = xyY_to_XYZ([x, y, Y_max])
            logging.info("white %snit Y_max for sRGB %s %s -> %s %s", Yw, x, y, Y_max, XYZ.tolist())
            ret.append(XYZ.tolist())
    return ret

def get_P3D65_calibrate_XYZ_suit(color_gamut, paper_white_nit=200):
    """
    返回 Display P3(D65) 色域内校准用测试集。按色域定义构矩阵：
    - 原色：P3 primaries
    - 白点：color_gamut中定义
    - 白亮度：由 get_white_test_XYZ_suit 提供的 Y (nit)
    """
    xy_R = XYZ_to_xy(color_gamut["red"])
    xy_G = XYZ_to_xy(color_gamut["green"])
    xy_B = XYZ_to_xy(color_gamut["blue"])
    xy_W = XYZ_to_xy(color_gamut["white"])

    ret = []
    for white in get_D65_white_calibrate_test_XYZ_suit(color_gamut, paper_white_nit):
        white = [itm*10000.0 for itm in white]  # 转为 nit
        Yw = float(white[1])                    # 白的亮度（nit）
        caps = (1.0, 1.0, 1.0)
        for x, y in P3D65_test_colors_xy:
            Y_max = ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, (x, y), caps)*Yw
            if Y_max == 0:
                logging.info("skip Ymax=0 p3 %s %s", x, y)
                continue
            XYZ = xyY_to_XYZ([x, y, Y_max])
            logging.info("white %snit Y_max for P3D65 %s %s -> %s %s", Yw, x, y, Y_max, XYZ.tolist())
            ret.append(XYZ.tolist())
    return ret


def get_srgb_measure_XYZ_suit(color_gamut):
    """
    返回 sRGB 色域内测量色准用测试集。这里按“色域定义”构矩阵：
    - 原色/白点：color_gamut中定义
    - 白亮度：由 get_white_test_XYZ_suit 提供的 Y (nits)
    """
    xy_R = XYZ_to_xy(color_gamut["red"])
    xy_G = XYZ_to_xy(color_gamut["green"])
    xy_B = XYZ_to_xy(color_gamut["blue"])
    xy_W = XYZ_to_xy(color_gamut["white"])

    ret = []
    for white in get_D65_white_measure_test_XYZ_suit(color_gamut):
        white = [itm*10000.0 for itm in white]  # 转为 nits
        Yw = float(white[1])                    # 白的亮度（nits）
        caps = (1.0, 1.0, 1.0)
        for x, y in sRGB_test_colors_xy:
            Y_max = ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, (x, y), caps)*Yw
            if Y_max == 0:
                logging.info("skip Ymax=0 srgb %s %s", x, y)
                continue
            XYZ = xyY_to_XYZ([x, y, Y_max])
            logging.info("white %snit Y_max for sRGB %s %s -> %s %s", Yw, x, y, Y_max, XYZ.tolist())
            ret.append(XYZ.tolist())
    return ret

def get_P3D65_measure_XYZ_suit(color_gamut):
    """
    返回 Display P3(D65) 色域内测量色准用测试集。按色域定义构矩阵：
    - 原色：P3 primaries
    - 白点：color_gamut中定义
    - 白亮度：由 get_white_test_XYZ_suit 提供的 Y (nits)
    """
    xy_R = XYZ_to_xy(color_gamut["red"])
    xy_G = XYZ_to_xy(color_gamut["green"])
    xy_B = XYZ_to_xy(color_gamut["blue"])
    xy_W = XYZ_to_xy(color_gamut["white"])

    ret = []
    for white in get_D65_white_measure_test_XYZ_suit(color_gamut):
        white = [itm*10000.0 for itm in white]  # 转为 nits
        Yw = float(white[1])                    # 白的亮度（nits）
        caps = (1.0, 1.0, 1.0)
        for x, y in P3D65_test_colors_xy:
            Y_max = ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, (x, y), caps)*Yw
            if Y_max == 0:
                logging.info("skip Ymax=0 p3 %s %s", x, y)
                continue
            XYZ = xyY_to_XYZ([x, y, Y_max])
            logging.info("white %snit Y_max for P3D65 %s %s -> %s %s", Yw, x, y, Y_max, XYZ.tolist())
            ret.append(XYZ.tolist())
    return ret

def get_srgb_24_calibrate_XYZ_suit(color_gamut, paper_white_nit=200):
    """
    返回 24 色行业标准色卡（X-Rite ColorChecker Classic）的校准用测试集。
    与 get_srgb_calibrate_XYZ_suit（12 色）的区别：
    - 24 个色块全部来自标准色卡（含 6 级灰阶）；
    - 每个色块的色度与相对亮度均为色卡真实值，亮度按纸白锚定缩放
      （白块 ≈ 纸白亮度，其余色块按反射率等比缩放）；
    - 若某色块亮度超出所选色域在该色度的最大可显示亮度，则截断到该最大值；
      完全超出色域（Y_max=0）的色块跳过。
    说明：矩阵拟合是色度拟合（fit_XYZ2XYZ_wlock_dropY + 白点锁定），
    亮度只影响显示与测量信噪比，不影响拟合结果。
    """
    xy_R = XYZ_to_xy(color_gamut["red"])
    xy_G = XYZ_to_xy(color_gamut["green"])
    xy_B = XYZ_to_xy(color_gamut["blue"])
    xy_W = XYZ_to_xy(color_gamut["white"])

    ret = []
    for white in get_D65_white_calibrate_test_XYZ_suit(color_gamut, paper_white_nit):
        white = [itm*10000.0 for itm in white]  # 转为 nit
        Yw = float(white[1])                    # 纸白亮度（nits）
        caps = (1.0, 1.0, 1.0)
        for name, r, g, b in ColorChecker24_srgb:
            xyz_rel = srgb_rgb255_to_XYZ_norm((r, g, b))
            x, y = XYZ_to_xy(xyz_rel)
            Y_patch = float(xyz_rel[1]) * Yw    # 色卡真实亮度（nits）
            Y_max = ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, (x, y), caps)*Yw
            if Y_max == 0:
                logging.info("skip Ymax=0 srgb24 %s %s %s", name, x, y)
                continue
            Y_patch = min(Y_patch, Y_max)       # 窄色域保护：截断到可显示亮度
            XYZ = xyY_to_XYZ([x, y, Y_patch])
            logging.info("srgb24 %s (%s,%s,%s) xy=(%.4f,%.4f) Y=%.1fnit -> %s", name, r, g, b, x, y, Y_patch, XYZ.tolist())
            ret.append(XYZ.tolist())
    return ret

def get_srgb_24_measure_XYZ_suit(color_gamut):
    """
    返回 24 色行业标准色卡（X-Rite ColorChecker Classic）的测量色准用测试集。
    结构同 get_srgb_measure_XYZ_suit：每个色块 × 10 个 PQ 均匀亮度档。
    """
    xy_R = XYZ_to_xy(color_gamut["red"])
    xy_G = XYZ_to_xy(color_gamut["green"])
    xy_B = XYZ_to_xy(color_gamut["blue"])
    xy_W = XYZ_to_xy(color_gamut["white"])

    ret = []
    for white in get_D65_white_measure_test_XYZ_suit(color_gamut):
        white = [itm*10000.0 for itm in white]  # 转为 nits
        Yw = float(white[1])                    # 白的亮度（nits）
        caps = (1.0, 1.0, 1.0)
        for name, r, g, b in ColorChecker24_srgb:
            xyz_rel = srgb_rgb255_to_XYZ_norm((r, g, b))
            x, y = XYZ_to_xy(xyz_rel)
            Y_max = ymax_from_defined_primaries(xy_R, xy_G, xy_B, xy_W, (x, y), caps)*Yw
            if Y_max == 0:
                logging.info("skip Ymax=0 srgb24 %s %s %s", name, x, y)
                continue
            XYZ = xyY_to_XYZ([x, y, Y_max])
            logging.info("white %snit Y_max for srgb24 %s (%s,%s,%s) -> %s %s", Yw, name, r, g, b, Y_max, XYZ.tolist())
            ret.append(XYZ.tolist())
    return ret



if __name__ == "__main__":
    pass
