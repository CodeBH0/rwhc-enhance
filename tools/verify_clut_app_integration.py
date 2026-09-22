# -*- coding: utf-8 -*-
"""
verify_clut_app_integration.py — 验证 app.py 的 CLUT 集成代码路径

不启动 GUI、不连接色度计：构造一个带有真实结构的假 app 对象，直接调用
app.HDRCalibrationUI 上的三个方法：

  · _make_display_model()   —— 由校准结果构造正向模型
  · _model_signature()      —— 模型指纹（缓存键）
  · write_clut_if_enabled() —— 生成并写入 CLUT 标签

然后重新打开产出的 ICC，确认：
  · A2B0/B2A0 已写入且能被解析
  · 原有矩阵标签与 MHC2 完好
  · 缓存命中（二次调用不重复生成）

用法：
    .venv314\\Scripts\\python.exe tools\\verify_clut_app_integration.py
"""

import os
import sys
import time
import types
import tempfile

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from clut_icc import parse_clut_tag, tetrahedral_interp  # noqa: E402
from icc_rw import ICCProfile  # noqa: E402
from tools.verify_clut_profile import make_synthetic_mhc2  # noqa: E402


def load_app_methods(*method_names):
    """
    从 app.py 里**原样提取**指定方法的源码，编译进一个精简命名空间执行。

    为什么不直接 `import app`：app.py 顶层会 import color_rw -> wexpect -> psutil，
    还要拉起 tkinter 窗口。本测试只关心 CLUT 相关的三个方法，用 AST 抽取真实
    源码可以保证「测的就是实现」，又不引入 GUI/串口依赖。
    """
    import ast
    app_path = os.path.join(PROJECT_ROOT, "app.py")
    src = open(app_path, encoding="utf-8").read()
    tree = ast.parse(src)

    wanted = set(method_names)
    chunks = []
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "HDRCalibrationUI":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in wanted:
                    if item.decorator_list:
                        print(f"  (注意：{item.name} 带装饰器，测试中不再应用)")
                    chunk = ast.get_source_segment(src, item)
                    # 去掉装饰器行，保留 def
                    chunk = chunk[chunk.index("def "):]
                    chunks.append(chunk)
                    found.add(item.name)
    missing = wanted - found
    if missing:
        raise RuntimeError(f"app.py 中找不到方法: {sorted(missing)}")

    # 从 app.py 里取出 clut_icc（以及 convert_utils 的色卡换算）的 import 语句，
    # 保证用的符号和实现一致
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(src, node)
            if "clut_icc" in seg or "logging" in seg:
                imports.append(seg)
    imports.append("from convert_utils import XYZ_to_BT2020_PQ_rgb")

    ns = {"__name__": "app_methods_test"}
    exec("import tkinter\nimport numpy as np\nimport os\n", ns)
    exec("\n".join(imports), ns)
    if "_" not in ns:
        ns["_"] = lambda s: s
    if "logging" not in ns:
        import logging as _logging
        ns["logging"] = _logging
    exec("\n".join(chunks), ns)
    return {name: ns[name] for name in method_names}, ns


def build_fake_app(grid=17, clut_enabled=True):
    """
    构造一个「假 app」：只带 write_clut_if_enabled 需要的属性。

    用 app 模块里的真实方法，避免复制逻辑导致测试与实现脱节。
    """
    mhc2 = make_synthetic_mhc2(peak_nits=1000.0)

    # 实测原色/白点：用 BT.2020 + D65 的方向向量，按峰值亮度给尺度
    from matrix import build_rgb_to_xyz_from_primaries
    from meta_data import D65_WHITE_POINT
    M = build_rgb_to_xyz_from_primaries((0.708, 0.292), (0.170, 0.797),
                                        (0.131, 0.046), D65_WHITE_POINT)
    rxyz, gxyz, bxyz = M[:, 0], M[:, 1], M[:, 2]
    wtpt = M[:, 0] + M[:, 1] + M[:, 2]
    scale = 1000.0 / float(M[1].sum())

    fake = types.SimpleNamespace(
        MHC2=dict(mhc2),
        measure_gamut_xyz={
            "red": (rxyz * scale).tolist(),
            "green": (gxyz * scale).tolist(),
            "blue": (bxyz * scale).tolist(),
            "white_paper": (wtpt * scale).tolist(),
            "white": (wtpt * scale).tolist(),
            "black": [0.0, 0.0, 0.0],
        },
        measured_pq=None,
        icc_handle=ICCProfile(os.path.join(PROJECT_ROOT, "data", "hdr_empty.icc")),
        _clut_cache=None,
        clut_var=types.SimpleNamespace(get=lambda: clut_enabled),
        clut_grid_var=types.SimpleNamespace(get=lambda: str(grid)),
    )
    # 绑定从 app.py 原样抽取的真实方法
    # （`_make_display_model` 会调用 `_measured_color_samples` 取实测色卡；
    #   `write_clut_if_enabled` 现在拆成了 `_make_clut_tags` + `_report_clut`
    #   两步（保存时把前者放到工作线程），所以这几个方法都要一起抽取。）
    methods, _ns = load_app_methods(
        "_make_display_model", "_model_signature", "write_clut_if_enabled",
        "_measured_color_samples", "_make_clut_tags", "_report_clut")
    for name, fn in methods.items():
        setattr(fake, name, types.MethodType(fn, fake))
    return fake


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return bool(ok)


def main(grid=17):
    results = []
    print(f"\n=== app.py CLUT 集成自检（网格 {grid}³）===")

    fake = build_fake_app(grid=grid, clut_enabled=True)

    # 1) 模型构造
    model = fake._make_display_model()
    results.append(check("_make_display_model 成功", model is not None,
                         f"peak={model.peak_nits:.0f} nit, lut={model.lut_size} 项"))
    results.append(check("模型矩阵 Y 行合理",
                         0.2 < float(model.xyz_matrix[1].sum()) < 1.2,
                         f"Y 行和 = {float(model.xyz_matrix[1].sum()):.5f}"))

    # 2) 模型指纹
    sig1, sig2 = fake._model_signature(), fake._model_signature()
    results.append(check("_model_signature 稳定", sig1 == sig2))
    fake.MHC2["peak_luminance"] = 900.0
    results.append(check("_model_signature 对输入变化敏感", fake._model_signature() != sig1))
    fake.MHC2["peak_luminance"] = 1000.0

    # 3) 写入 CLUT
    t0 = time.time()
    written, text = fake.write_clut_if_enabled()
    dt1 = time.time() - t0
    results.append(check("write_clut_if_enabled 返回标签", written == ["A2B0", "B2A0"],
                         f"{written}；提示：{text}"))
    results.append(check("提示文本包含网格与误差信息", bool(text) and "³" in text))

    # 4) 缓存命中
    t0 = time.time()
    written2, _ = fake.write_clut_if_enabled()
    dt2 = time.time() - t0
    results.append(check("二次调用命中缓存（不再重建）", dt2 < dt1 / 3,
                         f"首次 {dt1:.2f}s，二次 {dt2:.4f}s"))

    # 5) 保存并回读
    fake.icc_handle.rebuild()
    tmp = os.path.join(tempfile.gettempdir(), f"rwhc_app_integration_{grid}.icc")
    fake.icc_handle.save(tmp)
    size = os.path.getsize(tmp)

    again = ICCProfile(tmp)
    results.append(check("产出 profile 含 A2B0/B2A0",
                         "A2B0" in again.tags and "B2A0" in again.tags,
                         f"{size / 1024:.1f} KB, tags={sorted(again.tags)}"))
    a2b = parse_clut_tag(bytes(again.data[again.tags["A2B0"]["offset"]:
                                            again.tags["A2B0"]["offset"] + again.tags["A2B0"]["size"]]))
    results.append(check("回读 A2B0 解析正确", a2b["grid"] == (grid, grid, grid),
                         f"grid={a2b['grid']}"))

    # 6) 原有内容必须完好
    # 注意：这里回读的是 data/hdr_empty.icc 里原有的 MHC2（短表），
    # 本项目真实的校准流程会先用完整的 4096 项 LUT 覆写它，所以断言
    # 只检查「长度与写入前一致」。
    src_mhc2 = ICCProfile(os.path.join(PROJECT_ROOT, "data", "hdr_empty.icc")).read_MHC2()
    mhc2 = again.read_MHC2()
    results.append(check("MHC2 完好",
                         mhc2 is not None and
                         len(mhc2["red_lut"]) == len(src_mhc2["red_lut"]) and
                         np.allclose(mhc2["red_lut"], src_mhc2["red_lut"]),
                         f"entry_count={mhc2['entry_count']} LUT 项={len(mhc2['red_lut'])}"))
    results.append(check("矩阵标签完好",
                         again.read_XYZType("rXYZ") is not None and
                         again.read_XYZType("wtpt") is not None and
                         again.read_TRC("rTRC") is not None))

    # 7) CLUT 与模型一致（用回读的标签插值比对）
    pts = np.random.default_rng(3).random((500, 3))
    got = tetrahedral_interp(a2b["clut"], pts)
    want = np.clip(model.device_to_pcs(pts), 0, 1)
    err = float(np.abs(got - want).max())
    results.append(check("回读 CLUT 与模型一致（最大误差 < 2%）", err < 0.02,
                         f"max={err * 100:.4f}%"))

    # 8) 关闭开关时不应写入
    fake2 = build_fake_app(grid=grid, clut_enabled=False)
    w3, t3 = fake2.write_clut_if_enabled()
    results.append(check("开关关闭时不写入 CLUT", w3 is None and t3 is None))

    print(f"\n  生成文件：{tmp}")
    passed = sum(1 for r in results if r)
    print(f"\n==== 结果：{passed}/{len(results)} 项通过 ====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    grid = int(sys.argv[1]) if len(sys.argv) > 1 else 17
    sys.exit(main(grid))
