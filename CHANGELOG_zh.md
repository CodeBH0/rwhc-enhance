# 更新日志

> 维护者并不是色彩科学相关职业，因此校色逻辑可能并非最佳。
> 如发现问题或有更好的想法，欢迎指出来。
> [English](CHANGELOG.md)

所有重要变更按时间倒序列在这里，格式保持平实——使用方法请看 README。

---

## [2026-09-22] — v2026.09.22（GitHub 发布）

> **发布信息：** 标签 `v2026.09.22`，打包为 `rwhc-v2026.09.22.zip` 附在
> [Releases](https://github.com/CodeBH0/rwhc-enhance/releases) 页面。上一个发布版本：
> `v2026.09.02`。此后的主要新增：CLUT（`A2B0`/`B2A0`）ICC 输出、历史颜色数据复用、
> 无光度计校准、重写的 MHC2 LUT 反函数，以及「保存 ICC 文件」卡死修复。

- **MHC2 的 1D LUT 现在用「实测 PQ 响应的真正反函数」生成。**
  `lut.generate_mhc2_lut_from_measured_pq()` 过去是先把实测曲线线性重采样到 40960 点、
  再对每个目标值做最近邻反查；现在直接在 (输出 PQ → 输入码值) 上做分段线性逆插值
  （`np.interp`）。它实现的约定（`lut[i] = f⁻¹(i/(N-1))`）已写进函数文档。
  - **先说清范围**：这是**代码审查驱动**的改动，不是修一个用户看得见的 bug。用
    `hc.log` 里全部 15 次真实灰阶 run 实测，在显示器可达范围内新旧 LUT 的差异是
    **平均 0.006、最大 0.013 个 10bit 码值**（约 1e-5 PQ），远低于面板自身的重复性
    误差。也就是说旧的最近邻反查**并没有**造成可见台阶或可测的亮度误差；这次重写
    换来的是「实现与数学定义一致」、不再依赖「重采样长度恰好整除」这类假设、不再
    就地修改调用方的数组，以及在平台/常量/NaN/点数极少等输入下行为正确。
  - **唯一的实际行为变化在顶端。** 显示器最亮点常常不是最后一个采样点（实机数据：
    峰值在 code≈814、PQ 0.80057，之后因 ABL/功率限制回落到 code 1023 处的 0.79955）。
    峰值之后响应是多对一的，单调化反函数必须在峰值之后保持恒定——因此现在所有
    高于实测峰值的目标都取**峰值处的码值**。若按「一律截断到 1.0」的直觉写法，会在
    整段高光里送出**实测更暗**的码值，所以刻意不那么做；理由已写入文档字符串。
  - 删除了 `lut.generate_mhc2_lut_from_measure_data()`（旧的按 `real_nit` 构表的实现，
    只有它自己那个从未被启用的 `eetf_args` 分支能走到）：发布版没有任何调用点，
    而且带着 `convert_idx[0] = 0; convert_idx[1] = 1` 这个并非有效反响应的端点 hack。
    `eetf_from_lut()`（`tools/cyberpunk2077_hdr_fixer.py` 在用）保留，文档里注明
    EETF 不作用于校色路径。
  - 新增自检工具 **`tools/verify_lut_inverse.py`**（36 项，无需显示器/色度计）：
    数学性质（单调、值域、端点、不就地修改、退化/NaN/单点输入）、与旧算法逐行副本的
    精度对比、顶端饱和语义、真实 `hc.log` 曲线上的同样检查，以及边界情形（平台、
    带噪声回落、6 点输入、线性响应）。运行：`python tools/verify_lut_inverse.py`。

- **修复「保存 ICC 文件」卡死。** 开启 CLUT 时，保存会把 `B2A0` 反向求解同步跑在 Tk
  主线程上，整个计算期间窗口不重绘也不响应——网格较大时是分钟级
  （实测：17³ ≈ 5 秒、25³ ≈ 17 秒、33³ ≈ 37 秒；45³/65³ 大致按节点数增长，即数分钟），
  Windows 也会把它判定为「未响应」。
  - 把 `write_clut_if_enabled` 拆成**纯计算**（`_make_clut_tags`，同时负责缓存）与
    **结果汇报**（`_report_clut`）两步；写 profile 仍然留在主线程。
  - `generate_and_save_icc` 现在会先禁用控件，只把「生成标签」放到工作线程，
    回到主线程后再写标签、`rebuild()`、`save()` 并安装 ICC。
    不启用 CLUT 时没有慢步骤，保持原来的同步路径。
  - 日志会在开始前说明预计耗时与网格：
    `正在生成 CLUT 以便保存：33³ 网格，可能需要一段时间（数秒到数分钟）……`，
    完成后再写 `已保存 ICC 配置文件：<路径>`。
  - 用 Tk 心跳探针验证响应性：保存 33³ CLUT 期间主循环最长只被阻塞 **0.21 秒**
    （原来是 37 秒以上，等于硬卡死）。工作线程产出的 ICC 与同步路径**逐字节相同**
    （sha256 一致），标签写入/回读也完全一致。
- **CLUT 反向求解提速约 1.6 倍**（`clut_icc.DisplayModel._candidate_devices`）。
  - 第一级最近邻原来会构造完整的 `(n, seed_grid³, 3)` 差值数组
    （`sum((fwd - t[:, None])**2, axis=2)`），33³ 时是约 8.6 GB 量级的临时内存往返。
    现在改用数学上完全等价的范数展开 `|a|² − 2·a·b + |b|²`，只做一次矩阵乘（BLAS）。
    输出逐位不变（与逐点路径比对 `max|Δ| = 1.1e-16`，逐点路径本身未改动）。
  - 细网格偏移 `meshgrid(fine, fine, fine)` 原来每个 chunk 都重算，现在只算一次。
  - 修掉第二级里的一个**潜在形状 bug**：`pw.reshape(probe.shape)` 只在
    「点数恰好等于 `per_row × refine_grid³`」时成立（33³ 的 35937 正好整除，所以一直
    没暴露）。换任何一个别的调用规模（单点、短批次、不同 `seed_grid`）都会报错或
    静默错位。现在按 `(e - s, probe_rows)` 显式 reshape，并把 chunk 取成
    保证形状成立的规模。
  - 实测 `refine_grid` 只占总耗时约 14%，且从 7 降到 5/3 既不改变闭环误差也不改变
    未裁切区域的精度，因此保留默认值，没有为了速度牺牲它。
- `tools/verify_clut_app_integration.py` 跟随拆分更新（`_make_clut_tags` /
  `_report_clut` 现在也一起用 AST 抽取）。
- `i18n/locales/messages_zh.po` 补上本次新增文案，并顺手补了几条原本空着的翻译。

- **两个历史数据都选中时，不接色度计也能完成校准并生成 ICC。**
  - 原来的 `calibrate_monitor` 无条件构造 `spotread`，没插仪器时会直接以
    `RuntimeError: spotread exit unexpectedly` 失败，什么都做不了。现在只有当本次校准
    真的需要测量时才启动色度计（以及 `dogegen` 与「放置色度计」对话框）——
    也就是 `_history_only_calibration()` 为真时完全不启动。
  - 该模式下也跳过「校准后」的色域复测（`measure_gamut_after`）：它只是用同样的原色
    重算同样的 `rXYZ`/`gXYZ`/`bXYZ`/`wtpt`、`lumi` 与 `MHC2` 峰值/黑场标签，
    复用结果与重测完全一致。日志：`已复用历史颜色数据：跳过校准后的色域复测`。
  - 「测量色准」仍然必须接仪器，但现在会明确提示
    （`未检测到色度计。测量色准需要连接色度计（校准本身只靠历史数据即可完成）。`），
    而不是抛 `RuntimeError` 堆栈。
  - 新增日志：`历史数据已足够：本次校准不需要连接色度计`。
- `tools/verify_color_history.py` 新增 D 段自检（共 40 项）：把 `ColorWriter`、
  `ColorReader` 与消息框全部替换成「一调用就抛异常」的替身，再跑真实的
  `calibrate_monitor`——跑通即证明完全没有碰硬件、也没有弹窗。该段同时验证
  运行不会增长 `hc.log`（工具把 app 的日志句柄切到临时文件，并把日志长度恢复原状）。

- **新增历史颜色数据（`color_history.py`）**：现有「历史灰阶数据」的对应功能——
  过去某次校准的颜色测量也可以直接复用，不必重测。
  - 从 `hc.log` 解析**完整**的颜色 run：六个色域测试点（红/绿/蓝/白/纸白/黑）、
    激活黑的二分搜索、以及整段色卡测量；中英文两种日志格式都支持。
    只有「六个色域点齐全 **且** 至少一个色卡样本」才认定为完整，被取消的校准不会出现。
  - 复用的正确性：这些测量都发生在写入校准 ICC **之前**（单位阵 + 平坦 LUT 的临时
    预览 profile），`calibrate_chromaticity` 也不对实测值做白点适配，因此记录的是该码值
    下面板的**原生响应**，与目标白点无关——与灰阶历史的道理完全一样。
    当前 `hc.log` 里所有颜色 run 用的也都是默认 D65 白点（`WhitePoint` 未改动），
    等于完全没有适配，所以既有数据可以直接复用。
  - 界面新增「历史颜色数据」下拉框（标注测量结束时间、色卡样本数与采样集）与
    「Refresh」按钮，位置紧挨灰阶历史那一行；校准完成后列表也会自动刷新。
  - 复用时跳过色域测量、激活黑搜索与整段色卡测量，之后 profile / `MHC2` 的写入走的是与
    实时测量**完全相同**的代码路径（从 `measure_gamut_before` 抽出的 `_apply_gamut_data`），
    因此复用产出的 profile 构造方式与重测没有区别。峰值/黑场亮度规则同样抽到
    `color_history.peak_min_luminance_from_xyz`，保证两条路径一致。
  - `calibrate_chromaticity` 现在会把使用的白点写进日志
    （`Color measurement white point: ...`），让以后的 run 自带这个信息；
    旧日志解析出来的 run 该字段为 `None`。
  - 复用后 `target_xyz` / `measured_xyz` 保持与实时测量一致的状态。
- **复用的颜色数据真正接进了 CLUT 链路**：
  - 复用的原色/白点经 `primaries_matrix()` 进入 `DisplayModel.xyz_matrix`，也就是 CLUT 的
    `A2B0` 方向——与实时测量同一条路径，没有单独分支。
  - 复用的色卡样本会挂到模型上，`DisplayModel.color_accuracy_report()` /
    `color_accuracy_summary()` 用模型的正向预测（绝对 nit）与其对比，给出
    mean/median/max ΔE ITP、平均 |ΔXYZ|（nit）与最差样本。`write_clut_if_enabled`
    会把结果写进日志（`CLUT colour check (A2B vs measured card): ...`），
    平均 ΔE ITP 超过刻意放宽的 15 时才警告。这是一个**模型自一致性**指标，
    不是绝对色准数字：面板自身的重复性误差就会体现在这里（在维护者显示器上实测：
    相隔 8–40 分钟的两次灰阶测量，PQ 输出差异最大 0.016；把色卡与「2.4 小时前的
    灰阶」构造出的模型对比，平均 ΔE ITP ≈ 149、平均 |ΔXYZ| ≈ 98 nit）。
    `color_accuracy_report` 的 docstring 已明确写出这一点。色卡数据只用于**校验**，
    不会改变由实测原色 + `MHC2` 已经决定的 CLUT。
  - 该指标的另一处限制：模型的绝对亮度标度依赖「MHC2 的 LUT 轴 = 设备码值 0..1」
    这一约定（`DisplayModel.device_to_linear` 与 `generate_mhc2_lut_from_measured_pq`、
    CLUT 自检共用）。比较的**色度**方向不受影响，但若某个 CMM 用的是别的索引约定，
    绝对亮度会整体缩放。此处如实记录，未擅自假定；要确认需要在实际显示器上验证。
  - `build_model_from_calibration(..., measured_colors=...)` 与
    `DisplayModel(..., measured_colors=...)` 为新增可选参数，原有调用不受影响。
- 新增自检工具 `tools/verify_color_history.py`（29 项，无需显示器与色度计）：
  真实 `hc.log` 的解析不变量（完整性、排序、黑 < 纸白 < 峰值白的亮度关系、样本量级、
  与灰阶历史共用日期基准、EETF 峰值/黑场规则）、ΔE ITP 数学正确性
  （完全一致 → 误差 0；码值抖动 0.002 → 亚 nit / 平均 ΔE ITP 0.43；原色色度扰动 5% → 可检出），
  以及 `app.py` 的复用路径（AST 抽取 `_measured_color_samples` / `_make_display_model`、
  样本单位、模型矩阵与直接构造一致、复用数据生成的 A2B CLUT 与参考模型一致）。
- `tools/verify_clut_app_integration.py` 同步更新：`_make_display_model` 现在还会调用
  `_measured_color_samples`，因此该测试也要抽取这个方法。
- `gray_history.py`：把日志日期推断抽成 `timestamped_line_dates()` 并在两个模块间共用，
  两个下拉框显示的时间基准保持一致；`parse_gray_runs` 行为不变（用同一份日志重新验证过）。
- 主窗口默认高度 1000 → 1050 px：新增一行后 requested height 约 1042 px，
  原高度会把日志框底部挤掉。

- **新增 CLUT（多维查找表）ICC 生成**（`clut_icc.py`）。在原有「矩阵 + `MHC2`」
  链路之外，生成的 ICC 现在还可以带上 ICC 标准查找表：
  - `A2B0`（设备 PQ 码值 → PCS）与 `B2A0`（PCS → 设备 PQ 码值），以
    `lut16Type`（`mft2`）写出；`mAB`/`mBA` 格式也已实现并纳入自检。
  - 网格点数可在界面选择（17/25/33/37/45/65，默认 33），新增勾选项
    “CLUT (A2B0/B2A0)”，默认关闭，因此原有行为完全不变。
  - CLUT **只用本次校准已经测到的数据**推导：实测原色/白点、`MHC2` 每通道
    曲线、峰值/黑场亮度——**不需要任何额外测量**。
  - 设备空间为 `RGB` + ST 2084（PQ）；PCS 采用 peak-relative XYZ
    （可达最亮白 = `Y 1.0`），与 DisplayCAL 生成的 XYZLUT 显示器 profile 同
    一套约定。全部 CLUT 数值都落在 ICC 的 [0,1] 内，因此不需要扩展编码
    （`psd`/`hlc`/PCC）或非标准 PCS 签名。
  - 矩阵标签与 `MHC2` 完全保留，CLUT 是纯增量，不影响 Windows HDR 校准链路。
  - 精度（合成显示器自检，33³）：`A2B0` 平均插值误差约 0.005%、最大 <1%
    （最差点恰好位于 PCS 上限处）；`B2A0` 在未被裁切的颜色上可精确求逆
    （闭环中位误差约 0.002%）。在被裁切的高光区，反向映射本质上是多对一的
    ——这是相对色度学 PCS 的性质而非实现缺陷，两个 README 都已说明。
  - 结构参考：对照了本机上真实的 DisplayCAL “XYZLUT+MTX 37” Argyll profile
    （33³/37³ `mft2` CLUT、peak-relative PCS）。
- 新增两个自检工具（无需显示器与色度计）：
  - `tools/verify_clut_profile.py`：25 项检查——标签结构往返、16bit 定点精度、
    四面体插值精度、A2B↔B2A 往返、CLUT 轴序自检、真实 ICC 写入并回读
    （校验 `MHC2` 与矩阵标签完好）、`mft2`↔`mAB` 交叉比对。
  - `tools/verify_clut_app_integration.py`：13 项检查，直接跑 `app.py` 的真实方法
    （用 AST 抽取源码，避免测试引入 GUI/wexpect 依赖），含缓存行为验证。
- **修复了三个导致程序无法启动的问题。**
  - `app.py` 的 `build_ui()` 里有 5 处写成了裸名 `root`（第 185/214/225/237/501 行：
    顶栏、说明标签、分隔线、按钮容器、日志框），而不是 `self.root`。模块级并没有
    `root`，因此 `HDRCalibrationUI.__init__` 会抛
    `NameError: name 'root' is not defined`，窗口根本出不来。现已改为 `self.root`。
    同一函数里本来就有 `self.root.*` 的写法，所以粗看不容易发现。
  - PyPI 版 `wexpect` 在模块顶层就 `import pkg_resources`，而 setuptools ≥ 81 已经
    不再提供 `pkg_resources`。全新 venv 下会导致 `import app` 直接失败，
    报 `ModuleNotFoundError: No module named 'pkg_resources'`。仓库里 vendored 的
    `wexpect-4.0.0/` 已经改成静态 `__version__ = '4.0.0'`，因此要装**这一份**而不是
    PyPI 那份：
    `pip install --force-reinstall --no-deps ./wexpect-4.0.0/`
  - `requirements.txt` 补上了原本只被隐式带进来的运行依赖（`psutil`、`pywin32`），
    并注明为什么必须用仓库里这份 `wexpect`。
- 注释掉了维护者显示器上标定的实验性暖色修正矩阵（即 “v7 红点移动矩阵”）。
  现在默认恢复为单位阵（v3 行为），这是最通用的选择。被注释的代码保留了
  完整推导和逐步启用指引，如果你想在自己显示器上尝试。
- README 补充了 “历史灰阶数据”、“历史颜色数据” 与 CLUT 输出的说明。
- 新增本更新日志；内部交接/补丁笔记和本机测量日志移入
  `archive\2026-09-02_24card-full\`。

## [2026-09-02]

> **关于本版本的开发说明：** 本仓库在原作者版本基础上由维护者（**非原作者**）
> 继续开发。本版本（2026-09-02 及上方 2026-09-22 的改动）**重度使用 DeepSeek Harness
> （AI 辅助编程工具）** 完成；维护者自身编程水平有限，代码主要由 AI 生成后
> 人工核对，可能存在非最佳实现或疏漏，使用 / 发布前请自行审查。

### 新增

- **24 色行业标准色卡**（X-Rite ColorChecker Classic，即 Macbeth
  ColorChecker）。“色彩采样集”下拉框现有 4 挡：
  `sRGB(12)` / `sRGB(12)+DisplayP3(7)` / `sRGB(24)` / `sRGB(24)+DisplayP3(7)`，
  默认仍为 12 色（行为不变）。24 色卡采用每个色块的真实色度与相对亮度，
  并按 SDR 纸白锚定缩放，拟合不再只依赖最亮的颜色。
- **历史灰阶数据**（`gray_history.py`）：在同一台显示器上做多个色温校色时，
  PQ 灰阶曲线不必每次重测——显示器的原生响应与目标白点无关，变的只是白点
  适配。在 “Historical gray data” 下拉框中选择一条过去完整的测量（按测量
  结束时间标注），PQ 曲线步骤就直接复用该数据。“Refresh” 按钮可重新读取
  列表，校准完成时也会自动刷新。

### 修复 / 改进

- SDR 纸白（paper white）**不再写死**，改为读取 Windows 系统设置，并作为
  白色测试块与校准测试集的亮度锚点。
  - 来源：`DISPLAYCONFIG_SDR_WHITE_LEVEL`（即系统" SDR 内容亮度"滑条）。
    换算公式：`纸白亮度(nit) = SDRWhiteLevel(raw) / 1000 × 80`；
    按滑条刻度（0–100）即 `nit = 滑条值 × 8`，滑条 20 → 160 nit。
  - 每次校准开始时按纸白重算白色测试块码值：
    `code = round(pq_oetf(纸白) × 1023)`（160 nit → 569/1023）。
  - 读不到系统值时回退 200 nit。
  - 注意：纸白在应用启动时读取一次，请先把系统滑条调到实际观看亮度，
    再启动应用。
- 拟合出的校正矩阵不再写入 MHC2 矩阵（Windows 会把该矩阵直接乘在内容 XYZ
  上，导致暖色欠饱和）。profile 保持单位阵，色域映射交给实测原色 +
  1D LUT。
- rXYZ/gXYZ/bXYZ/wtpt 标签按 ICC 惯例联合缩放到白点（r+g+b = wtpt），TRC
  写为 sRGB EOTF，profile 更符合规范。
- 修复 wexpect 4.0.0 在虚拟环境下的死锁（校准时会冻结 GUI）：改用真实基础
  解释器启动并加超时，残留辅助进程会被清理。

## [2026-09-01]

- 维护者显示器（FFALCON R27U81）上的首个验证版本：上述修复后 12 色卡平均
  ΔE 1.16 / max 5.52。
- 中间版本快照保留在 `archive\`。



