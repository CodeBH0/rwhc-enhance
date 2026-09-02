# -*- coding: utf-8 -*-
"""24 色卡新增挡位验证脚本（临时，验证后可删除）"""
import numpy as np
from color_test_suit import *
from convert_utils import xyY_to_XYZ

# 用 hc.log 2026-09-02 实测的色域（raw nits XYZ）构造 color_gamut
color_gamut = {
    "red":   [161.790375,  66.129807,   9.400127],
    "green": [ 66.264977, 218.455429,  30.852547],
    "blue":  [ 58.100735,  15.991588, 317.475616],
    "white": [1389.271362, 1507.126099, 1809.575928],
}
paper_white = 160.0

print("=== 1. 色卡数据检查 ===")
print("ColorChecker24_srgb 数量:", len(ColorChecker24_srgb))
print("sRGB_test_colors_xy_24 数量:", len(sRGB_test_colors_xy_24))
for name, xy in zip([c[0] for c in ColorChecker24_srgb], sRGB_test_colors_xy_24):
    print(f"  {name:16s} xy=({xy[0]:.4f},{xy[1]:.4f})")

print("\n=== 2. 24 色校准套件（真实亮度, 纸白 160nit） ===")
suit24 = get_srgb_24_calibrate_XYZ_suit(color_gamut, paper_white)
print("样本数:", len(suit24), "(预期 24)")
assert len(suit24) == 24, "24 色卡样本数不对"
for i, xyz in enumerate(suit24):
    assert np.all(np.isfinite(xyz)), f"样本 {i} 含 NaN/Inf: {xyz}"
    assert xyz[1] > 0, f"样本 {i} Y<=0: {xyz}"
Y_nit = [xyz[1]*10000 for xyz in suit24]
print("亮度范围: {:.1f} ~ {:.1f} nit".format(min(Y_nit), max(Y_nit)))
print("前 4 个样本 XYZ(norm):", [np.round(suit24[i], 5).tolist() for i in range(4)])

print("\n=== 3. 旧 12 色校准套件（回归检查） ===")
suit12 = get_srgb_calibrate_XYZ_suit(color_gamut, paper_white)
print("样本数:", len(suit12), "(预期 11，白点由调用方另加)")
assert len(suit12) == 11

print("\n=== 4. P3 校准套件（回归检查） ===")
suit_p3 = get_P3D65_calibrate_XYZ_suit(color_gamut, paper_white)
print("样本数:", len(suit_p3), "(0~7，取决于显示器实测色域是否覆盖 P3 色块；本机实测色域 4 个 P3 色块在色域外被跳过)")
assert 0 <= len(suit_p3) <= 7

print("\n=== 5. 24 色测量套件（10 亮度档 x 24 色） ===")
suit24m = get_srgb_24_measure_XYZ_suit(color_gamut)
print("样本数:", len(suit24m), "(预期 240)")
assert len(suit24m) == 240

print("\n=== 6. 旧测量套件（回归检查） ===")
suit12m = get_srgb_measure_XYZ_suit(color_gamut)
suit_p3m = get_P3D65_measure_XYZ_suit(color_gamut)
print("12 色测量样本数:", len(suit12m), "(预期 110); P3 测量样本数:", len(suit_p3m), "(0~70，视色域覆盖)")
assert len(suit12m) == 110 and 0 <= len(suit_p3m) <= 70

print("\n=== 7. 色卡挡位常量 ===")
print(COLOR_CARD_CHOICES)

print("\n全部检查通过 ✔")
