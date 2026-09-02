import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from convert_utils import srgb_encode

# 20:45 校准写入 MHC2 的矩阵 C (measured->target, xy空间拟合, Y行=identity)
C = np.array([
    [0.8743918111009713, 0.1977335255356282, -0.0650952981112017],
    [9.201787541976365e-16, 0.9999999999999993, -1.3775229297001723e-16],
    [-0.19999783111041755, 0.7317750627698932, 0.49704565883859697],
])

# 显示器实测原色（13:54 日志, nits -> 归一化）
def xyY(x, y, Y):
    return np.array([x / y * Y, Y, (1 - x - y) / y * Y]) / 10000.0

M_display = np.column_stack([
    xyY(0.683, 0.279, 1.0),   # red 原色方向
    xyY(0.206, 0.692, 1.0),   # green
    xyY(0.148, 0.041, 1.0),   # blue
])

# 报告样本: (mRGB, oLab, mLab)
samples = [
    ("1A red   ", (219,137,81),  (61.3500,34.8100,18.3800),  (62.6532,32.3540,18.9116)),
    ("2A yellow", (221,186,0),   (75.5000,5.8400,50.4200),   (76.4155,8.2115,27.8931)),
    ("3F yellow", (248,209,0),   (83.6100,3.3600,87.0200),   (85.0591,8.7754,29.8464)),
    ("5G yellow-green",(158,185,0),(72.4500,-23.5700,60.4700),(72.0597,-16.4573,24.7589)),
    ("5F green",(14,142,0),      (54.1400,-40.7600,34.7500), (52.7094,-38.2816,17.6745)),
    ("6F blue  ", (0,57,185),    (24.7500,13.7800,-49.4800), (24.3136,14.1948,-51.5643)),
    ("3D gray  ", (185,181,176), (73.4200,0.9900,1.8900),    (73.4895,0.9416,1.6653)),
]

def srgb_to_xyz(rgb):
    lin = srgb_encode(np.array(rgb, float) / 255.0)
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ])
    return M @ lin

def lab_to_xyz(lab, wp=(0.95047, 1.0, 1.08883)):
    L, a, b = lab
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    def finv(t):
        d = 6/29
        return t**3 if t > d else 3*d*d*(t - 4/29)
    return np.array([wp[0]*finv(fx), wp[1]*finv(fy), wp[2]*finv(fz)])

def xy(XYZ):
    s = XYZ.sum()
    return XYZ[0]/s, XYZ[1]/s

print(f"{'sample':<14} {'target_xy':<20} {'C@srgb_xy':<20} {'measured_xy':<20}")
for name, rgb, olab, mlab in samples:
    tgt = xy(srgb_to_xyz(rgb))
    c_applied = xy(C @ srgb_to_xyz(rgb))
    meas = xy(lab_to_xyz(mlab))
    print(f"{name:<14} ({tgt[0]:.4f},{tgt[1]:.4f})    ({c_applied[0]:.4f},{c_applied[1]:.4f})    ({meas[0]:.4f},{meas[1]:.4f})")

# 模型2检验: 若 Windows 把 C 当原色矩阵(逆矩阵用于转换), 净效果应为 M_display @ inv(C) @ content
print("\n=== 模型2检验: M_display @ C^-1 vs C (若相等则模型2成立) ===")
Cinv = np.linalg.inv(C)
model2 = M_display @ Cinv
print("M_display @ C^-1 =\n", np.round(model2, 4))
print("C =\n", np.round(C, 4))
print("是否近似相等:", np.allclose(model2, C, atol=0.1))
