# 顶点移动矩阵预检工具（2026-09-02，支持 v7 红点移动 / v8 蓝点移动）
#
# v7（red 模式）：保持绿色、蓝色顶点不变，把红色顶点沿斜率 -m 方向移动
#   （v7 验证版 m=1.0，x+y=const；v7 精调 m=1.2）。W = I + u⊗n，n = sRGB绿 × sRGB红。
# v8（blue 模式）：保持红色、绿色顶点不变，把蓝色顶点沿斜率 -2 方向移动。
#   W = I + u⊗n，n = sRGB红 × sRGB绿。
# 两种模式下：被固定的两个顶点严格不变；被移动的顶点与白点移动同一 XYZ 增量
# （白=红+绿+蓝，线性代数必然结果）——灰阶代价与顶点移动同量，LUT 无法补偿。
#
# 用法：python tools/simulate_redmove_matrix.py [red|blue] [s] [slope]
#   s = 内容空间顶点扰动幅度（相对 |该原色| 的比例）。
#       red 模式默认 0.02（v7 已验证值）；blue 模式默认 0.005（蓝通道较敏感）。
#   slope（仅 red 模式，默认 1.0，与 app.py 一致）= 移动方向斜率绝对值；
#       dir = (-0.5, slope, -0.2)，经验标定 slope 1.0 → 渲染斜率 ≈ -1.0
#       （v7 已验证；slope 1.2 曾试写后回退）。
#   输出：矩阵、固定顶点不变性、移动顶点/白点的内容空间 xy 移动、样本估计。
#
# 注意：内容空间的移动与报告实测并非严格 1:1（v7 教训：模型预测纯红 Δxy
# (-0.009,+0.011)，报告实测 (-0.037,+0.037)，约 3~4 倍）。以重新校准后的报告为准。
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from convert_utils import srgb_encode
from delteE import XYZdeltaE2000

M_srgb = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])
WP_D65 = np.array([0.95047, 1.0, 1.08883])

R = np.array([0.4124, 0.2126, 0.0193])
G = np.array([0.3576, 0.7152, 0.1192])
B = np.array([0.1805, 0.0722, 0.9505])
D = np.array([0.95047, 1.0, 1.08883])

def srgb_xyz(rgb):
    return M_srgb @ srgb_encode(np.array(rgb, float) / 255.0)

def lab_to_xyz(Lab):
    L, a, b = Lab
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    def finv(t):
        d = 6/29
        return t**3 if t > d else 3*d*d*(t - 4/29)
    return np.array([WP_D65[0]*finv(fx), WP_D65[1]*finv(fy), WP_D65[2]*finv(fz)])

def xy(v):
    s = v.sum()
    return (v[0]/s, v[1]/s)

def build_matrix(mode, s, slope=1.2):
    if mode == "red":
        # 固定绿、蓝；红 + 白一起移动。方向：减X加Y微减Z（斜率 -slope 上移）
        n = np.cross(G, B)
        n_dot = float(np.dot(n, R))
        prim = R
        ddir = np.array([-0.5, slope, -0.2])
        label = "红"
    else:  # blue
        # 固定红、绿；蓝 + 白一起移动。方向：减X加Y（斜率 -2 上移）
        n = np.cross(R, G)
        n_dot = float(np.dot(n, B))
        prim = B
        ddir = np.array([-1.0, 4.0, 0.0])
        label = "蓝"
    ddir = ddir / np.linalg.norm(ddir)
    delta = s * np.linalg.norm(prim) * ddir
    u = delta / n_dot
    W = np.eye(3) + np.outer(u, n)
    return W, delta, label

# v3 报告样本（与 simulate_matrix.py 相同，v3 残差基线）
data = [
    ("1A",(217,138,117),(61.35,34.81,18.38),(63.3761,31.9315,20.4283)),
    ("2A",(221,186,0),(75.50,5.84,50.42),(76.6610,4.4693,54.9091)),
    ("3A",(124,170,121),(66.82,-25.06,23.47),(66.4824,-25.6664,23.0163)),
    ("4A",(42,154,183),(60.53,-22.62,-20.40),(59.4685,-23.5669,-22.5752)),
    ("5A",(110,147,195),(59.66,-2.03,-28.46),(59.4319,-1.7131,-29.4797)),
    ("6A",(194,134,155),(59.15,30.83,-5.72),(60.6887,29.1121,-4.5359)),
    ("1C",(241,211,189),(85.42,9.41,14.49),(86.1202,8.2443,13.7581)),
    ("2C",(215,180,131),(74.28,9.05,27.21),(75.1838,7.6607,28.9356)),
    ("3C",(197,154,87),(64.57,12.39,37.24),(65.8317,10.4328,40.4696)),
    ("4C",(147,103,56),(44.49,17.23,26.24),(45.7768,15.3300,32.5729)),
    ("5C",(82,64,56),(25.29,7.95,8.87),(26.4646,7.9770,7.5702)),
    ("6C",(64,62,65),(22.67,2.11,-1.10),(24.3711,1.7036,-1.5113)),
    ("1F",(0,123,162),(47.12,-32.52,-28.75),(46.9271,-24.7554,-29.7483)),
    ("2F",(200,102,145),(50.49,53.45,-13.55),(52.5755,50.3112,-10.7475)),
    ("3F",(255,214,0),(83.61,3.36,87.02),(87.5315,2.7213,85.1216)),
    ("4F",(195,70,43),(41.05,60.75,31.17),(43.1566,58.3238,36.6556)),
    ("5F",(47,140,69),(54.14,-40.76,34.75),(53.2993,-42.9675,34.2274)),
    ("6F",(0,59,138),(24.75,13.78,-49.48),(22.5458,17.5577,-54.1741)),
    ("1G",(234,138,0),(60.94,38.21,61.31),(64.6457,34.0451,65.9184)),
    ("2G",(0,91,161),(37.80,7.30,-43.04),(36.0788,2.3289,-46.0589)),
    ("3G",(202,102,92),(49.81,48.50,15.76),(52.1522,44.7985,19.8081)),
    ("5G",(164,184,28),(72.45,-23.57,60.47),(72.6049,-23.8961,69.2207)),
    ("6G",(252,176,0),(71.65,23.74,72.28),(76.4324,20.4716,76.0461)),
    ("1H",(87,180,169),(70.19,-31.85,1.98),(69.0412,-32.0620,-0.1017)),
    ("2H",(127,131,176),(54.38,8.84,-25.71),(54.7407,8.6882,-25.6992)),
    ("3H",(87,106,61),(42.03,-15.78,22.93),(42.1660,-15.6576,25.0517)),
    ("4H",(80,121,157),(48.82,-5.11,-23.08),(48.5421,-5.7313,-24.2857)),
    ("5H",(201,153,125),(65.10,18.14,18.68),(66.1400,16.9181,19.9692)),
    ("6H",(117,84,62),(36.13,14.15,15.78),(37.1140,13.0646,18.3443)),
]
refs = [lab_to_xyz(np.array(olab)) for _, _, olab, _ in data]

def evaluate(W, name):
    des = []
    for (nm, rgb, olab, mlab), ref in zip(data, refs):
        c = srgb_xyz(rgb)
        r = lab_to_xyz(np.array(mlab)) - c          # v3 显示路径残差
        out = W @ c + r
        d = XYZdeltaE2000(out, ref)
        des.append((nm, d))
    avg = sum(d for _, d in des) / len(des)
    mx = max(d for _, d in des)
    worst = " ".join(f"{n}:{d:.1f}" for n, d in sorted(des, key=lambda x: -x[1])[:5])
    print(f"{name:<26} avg={avg:.2f} max={mx:.2f}  worst: {worst}")

def main():
    args = sys.argv[1:]
    mode = "red"
    if args and args[0] in ("red", "blue"):
        mode = args.pop(0)
    s = float(args[0]) if args else (0.02 if mode == "red" else 0.005)
    if args:
        args.pop(0)
    slope = 1.0
    if args and mode == "red":
        slope = float(args[0])
    W, delta, label = build_matrix(mode, s, slope)
    moved = R if mode == "red" else B

    print(f"=== 顶点移动矩阵预检 ({'v7 red' if mode=='red' else 'v8 blue'}, s={s}{', slope='+str(slope) if mode=='red' else ''}) ===")
    print("W =", np.round(W.flatten(), 6).tolist())
    print(f"绿顶点不变: {np.allclose(W @ G, G)}  蓝顶点不变: {np.allclose(W @ B, B)}  红顶点不变: {np.allclose(W @ R, R)}")
    print(f"{label}内容: xy {tuple(round(float(v),4) for v in xy(moved))} -> {tuple(round(float(v),4) for v in xy(W@moved))}  (Δ={tuple(round(float(v),4) for v in (W@moved-moved))})")
    print(f"白内容: xy {tuple(round(float(v),4) for v in xy(D))} -> {tuple(round(float(v),4) for v in xy(W@D))}  (Δ={tuple(round(float(v),4) for v in (W@D-D))})")
    print(f"灰 0.5白: xy {tuple(round(float(v),4) for v in xy(0.5*D))} -> {tuple(round(float(v),4) for v in xy(W@(0.5*D)))}")
    print()
    print("样本 avg/max ΔE 估计（v3 残差基线，绝对数值仅供参考）:")
    print(f"{'v3 identity':<26} avg=1.71 max=4.82  (报告实测)")
    if mode == "red":
        print(f"{'v7 s=0.02 slope=1.0':<26} avg=1.16 max=5.52  (报告实测)")
    else:
        print(f"{'v8 (本版)':<26} 待实测")
    evaluate(np.eye(3), "v3 identity(残差复算)")
    evaluate(W, f"{'v7 red' if mode=='red' else 'v8 blue'} s={s}{' slope='+str(slope) if mode=='red' else ''}")
    for s2 in (0.0, 0.01, 0.02, 0.05):
        W2, _, _ = build_matrix(mode, s2, slope)
        evaluate(W2, f"{mode} s={s2}{' slope='+str(slope) if mode=='red' else ''}")
    print()
    print("注意：内容空间移动与报告实测非严格 1:1（v7 实测约为模型 3~4 倍）；")
    print("重新校准后用报告核对移动顶点/白点，必要时调 s 或 slope。")

if __name__ == "__main__":
    main()
