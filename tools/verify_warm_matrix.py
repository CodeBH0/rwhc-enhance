import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from convert_utils import srgb_encode

D65 = np.array([0.95047, 1.0, 1.08883])
w_vec = np.array([1.0, -0.515, -0.4])
v_vec = np.array([0.06, -0.09, 0.0])
W = np.eye(3) + np.outer(v_vec, w_vec)
print("W =")
print(np.round(W, 4))
print("w.D65 =", round(float(w_vec @ D65), 6))

def srgb_xyz(rgb):
    lin = srgb_encode(np.array(rgb, float) / 255.0)
    M = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    return M @ lin

def xy(XYZ):
    s = XYZ.sum()
    return XYZ[0] / s, XYZ[1] / s

samples = {
    'gray128': (128, 128, 128), '1G_orange': (233, 138, 0), '6G_orange': (252, 176, 0),
    '3F_yellow': (255, 214, 0), '4F_red': (195, 70, 43), '6F_blue': (0, 57, 185),
    '3A_green': (124, 170, 121), '1A_skin': (217, 138, 117), '5A_blue': (110, 147, 195),
}
print(f"{'sample':<12} {'orig xy':<18} {'after xy':<18} {'dY%':>7} {'dX%':>7}")
for name, rgb in samples.items():
    c = srgb_xyz(rgb)
    c2 = W @ c
    cxy, cxy2 = xy(c), xy(c2)
    print(f"{name:<12} ({cxy[0]:.4f},{cxy[1]:.4f})  ({cxy2[0]:.4f},{cxy2[1]:.4f})  "
          f"{(c2[1]/c[1]-1)*100:+6.1f} {(c2[0]/c[0]-1)*100:+6.1f}")

# 灰阶不变性严格验证（多个亮度）
print("\ngray invariance across levels:")
for level in [32, 96, 160, 224]:
    c = srgb_xyz((level, level, level))
    c2 = W @ c
    print(f"  level {level}: max|d| = {np.max(np.abs(c2 - c)):.2e}")
