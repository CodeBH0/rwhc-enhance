import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from convert_utils import srgb_encode, XYZ_to_Lab

# 21:03 实测原色/白点 (nits)
def xyY(x, y, Y):
    return np.array([x / y * Y, Y, (1 - x - y) / y * Y])

R = xyY(0.675, 0.283, 60.34)
G = xyY(0.213, 0.681, 197.47)
B = xyY(0.152, 0.038, 14.14)
W = xyY(0.299, 0.323, 188.96)   # white_paper

WP_D65 = np.array([0.95047, 1.0, 1.08883])
M_srgb = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])

def lab_of(xyz):
    return XYZ_to_Lab(xyz, WP_D65)

def xy(XYZ):
    s = XYZ.sum()
    return XYZ[0]/s, XYZ[1]/s

# --- 当前(独立Y=1)标签矩阵 ---
M_y1 = np.column_stack([R/R[1], G/G[1], B/B[1]])
# --- 正确(联合缩放 r+g+b=wtpt, wtpt.Y=1)标签矩阵 ---
Wn = W / W[1]
s = np.linalg.solve(np.column_stack([R, G, B]), Wn)
M_correct = np.column_stack([R, G, B]) @ np.diag(s)

print("当前独立Y=1矩阵列(原色):")
print(np.round(M_y1.T, 4))
print("正确联合缩放矩阵列(原色):")
print(np.round(M_correct.T, 4))
print("正确: r+g+b =", np.round(M_correct.sum(axis=1), 4), " (wtpt Y=1 =", np.round(Wn,4), ")")
print("当前: r+g+b =", np.round(M_y1.sum(axis=1), 4))

# --- 用两种矩阵分别把 6F 蓝色 (0,57,185) 映射到设备RGB, 再经显示器原色还原 ---
def render_blue(M_inv):
    lin = srgb_encode(np.array([0.0, 57/255, 185/255]))
    xyz = M_srgb @ lin                     # 内容XYZ
    d = M_inv @ xyz                        # 设备线性RGB
    out = M_correct @ np.clip(d, 0, None)  # 显示器输出(用真实原色)
    return out, d

out_y1, d_y1 = render_blue(np.linalg.inv(M_y1))
out_correct, d_correct = render_blue(np.linalg.inv(M_correct))

content_xyz = M_srgb @ srgb_encode(np.array([0.0, 57/255, 185/255]))
print("\n内容(真实sRGB蓝) XYZ:", np.round(content_xyz, 4), " xy:", np.round(xy(content_xyz), 4))
print("sRGB蓝 Lab:", np.round(lab_of(content_xyz), 2))
print("用当前(错误)矩阵映射后显示器输出 Lab:", np.round(lab_of(out_y1), 2), " xy:", np.round(xy(out_y1), 4))
print("用正确矩阵映射后显示器输出 Lab:", np.round(lab_of(out_correct), 2), " xy:", np.round(xy(out_correct), 4))
print("报告实测 mLab:", "(22.30, 17.60, -53.30)")
