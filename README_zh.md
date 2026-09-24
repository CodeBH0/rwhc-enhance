# Windows 11 真正可用的 HDR 校准软件
中文|[ENGLISH](https://github.com/CodeBH0/rwhc-enhance/blob/master/README.md)

![screenshot](https://github.com/CodeBH0/rwhc-enhance/blob/master/resources/ui_zh.png)

## 前端迁移状态

项目正在把前端替换为 **C# + WinUI 3**（Windows App SDK + XAML）。第一阶段代码位于
`frontend/Rwhc.WinUI`，目前已经具备可构建运行的 WinUI 外壳，以及连接现有 Python
backend 的真实进程边界。现有 Python/Tk UI 只在 WinUI 达到功能完整前保留为功能和
交互参考；项目不会长期维护两套正式 UI。

当前 WinUI 范围：

- .NET 10、WinUI 3、XAML 与 Windows App SDK 2.5.1；
- WinUI 持有 Python 子进程，通过版本化、方法白名单的 UTF-8 NDJSON 协议通信；
  短 RPC 与异步 progress/log/prompt/result/error 事件可并行复用同一管道，并支持
  协作取消；
- 已为 `CalibrationRequest` 建立严格、版本化的 JSON 契约；
- 显示器发现/选择、SDR paper white、Argyll 仪器与测量模式均已调用真实 backend；
- `calibration.start` 已打通真实色域、PQ、色度和复测流程，支持进度、日志、提示、结果、
  错误与取消；
- WinUI 已支持选择完整灰阶/颜色历史数据，在无色度计时运行真实历史回放 workflow。

新版应用的入口是 WinUI。`app.py` 只是迁移期保留的 legacy Tk UI，不是新版入口。
安装 .NET 10 SDK 和项目 Python 依赖后，在项目根目录一键启动：

```powershell
.\run-winui.cmd
```

启动器会调用 `run-winui.ps1` 检查 Python backend、构建并启动 WinUI。WinUI 会自动启动和关闭
`backend_host.py`，用户不需要、也不应手动运行 backend host。等价的开发命令是：

```powershell
dotnet build frontend\Rwhc.WinUI\Rwhc.WinUI.csproj -c Debug -p:Platform=x64
dotnet run --project frontend\Rwhc.WinUI\Rwhc.WinUI.csproj -c Debug
```

WinUI 页面会自动读取真实设备环境，也可手动刷新。还可以运行覆盖请求、事件和设备调用的
自动 smoke test：

```powershell
frontend\Rwhc.WinUI\bin\x64\Debug\net10.0-windows10.0.26100.0\win-x64\Rwhc.WinUI.exe --smoke-test
```

退出码为 `0` 表示 WinUI runtime 已启动，并且 C# client 成功读到就绪的 Python
backend profile。详细设计见 [IPC 协议](docs/FRONTEND_IPC.md)、
[backend 边界](docs/BACKEND.md)和 [WinUI 工程说明](frontend/Rwhc.WinUI/README.md)。

## 使用方法

1. **获取项目代码**  
   下载或 clone 本项目，进入项目根目录。  
   也可以在 [Releases](https://github.com/CodeBH0/rwhc-enhance/releases) 页面直接下载打包好的
   `rwhc-v<版本>.zip`，解压即可运行，无需 git。

2. **安装 Python**  
   在 Windows 上安装 Python（任选其一）：
   - Microsoft Store
   - 官方网站：<https://www.python.org/downloads/windows/>

3. **安装依赖**  
   在项目根目录下执行：

   ```bash
   pip install -r requirements.txt
   ```

4. **运行新版 WinUI 应用（推荐）**

   ```powershell
   .\run-winui.cmd
   ```

   应用会自行管理 Python backend。选择实时测量，或同时选择历史灰阶与历史颜色数据，
   确认参数后开始校准。

5. **旧版 Tk 界面**

   `python app.py` 启动的是迁移期保留的 legacy Tk UI，仅用于 WinUI 尚未覆盖的功能，
   不是新版应用入口。

### 无色度计历史回放

如果 `hc.log` 至少包含一组完整灰阶和一组完整颜色测量，WinUI 会把它们列在两个历史
下拉框中。未检测到色度计时会自动选择最近的完整组合。点击“使用历史数据校准”后，
应用仍会通过 WinUI → IPC → `calibration.start` 执行真实 workflow 和原有校准数学，
只是不会启动 `dogegen`/`spotread`。进度、日志和最终结果会显示在页面中。历史数据必须
来自同一显示器及相同显示状态。

构建后可运行硬件隔离的端到端验证：

```powershell
frontend\Rwhc.WinUI\bin\x64\Debug\net10.0-windows10.0.26100.0\win-x64\Rwhc.WinUI.exe --history-smoke-test
```

该显式测试模式只替换 Windows 显示器/ICC 边缘；历史解析、IPC、请求校验、workflow 和
校准算法均使用正式实现。

## 校色设备
目前设备驱动使用argyllcms，该驱动支持的常见设备有爱色丽全系和Datacolor  
具体可前往官网查询

## 使用说明补充

1. **使用爱色丽校色仪和罗技鼠标** 
   罗技鼠标驱动强制扫描所有设备会导致爱色丽校色仪被占用！！！ 
   先在设备管理器中将鼠标的驱动更换为 Windows 默认驱动，然后在“服务”中停止 `Logitech LampArray Service`。

2. **使用 Datacolor Spyder 校色仪**  
   点击安装驱动按钮跳转值argyll网站，下载后在usb文件夹中点击安装，然后在设备管理器中找到 Spyder 设备（位于“通用串行总线控制器”下）：
   - 右键设备选择“更新驱动”
   - 选择“浏览我的电脑以查找驱动程序”
   - 选择“让我从计算机的可用驱动程序列表中选取”
   - 从列表中选择 Argyll 驱动

3. **SDR 内容亮度（纸白）**  
   校准会把 SDR 白色测试块与测试集的亮度锚定到所选显示器**实际使用的 SDR 纸白**——也就是 Windows 设置里的“SDR 内容亮度”滑条，而不是写死一个值。  
   启动程序前，请先把该滑条调到你看 SDR 内容时实际使用的亮度。
   - 读取方式：程序查询 `DISPLAYCONFIG_SDR_WHITE_LEVEL`（raw 值 0–10000），换算公式为 `nit = raw / 1000 × 80`。  
     按滑条刻度（0–100）就是 `nit = 滑条值 × 8`（滑条 20 → 160 nit）。
   - 每次校准开始时按纸白重算白色测试块码值：`code = round(pq_oetf(纸白) × 1023)`（160 nit → 569/1023）。
   - 读不到系统值时回退 200 nit。
   - 注意：纸白在应用启动时读取一次——请先调好滑条，再启动应用。

4. **灰阶采样数**  
   10bit HDR 有 1024 级灰阶（R=G=B，范围 0–1023）。程序会在 1024 级灰阶中等距离采集指定数量的灰阶点，并对未测量的灰阶进行插值。  
   采集数量越多，PQ 曲线校准越精准(可能)，但耗时越长。  
   实测曲线会以**反解**的方式变成 `MHC2` 的 1D LUT：对每一个「期望输出 PQ」，表里存的是显示器需要的输入 PQ（`lut[i] = f⁻¹(i/4095)`，先做去噪单调化，再分段线性逆插值）。有一个刻意保留的细节：**超过实测峰值后，LUT 保持在实际最亮点的码值上**（即使那不是最后一个采样点）——维护者的 OLED 曲线在 code≈814 达到峰值，之后因 ABL/功率限制回落到 code 1023，此时若按直觉截断到 PQ 1.0，高光反而会实测变暗。  
   自检：`python tools/verify_lut_inverse.py`（36 项，无需显示器与色度计）。

5. **色彩采样集**  
   程序会在所选色域内生成一个测试集，根据测试集预期 XYZ 与实测 XYZ 进行拟合得到矩阵。共 4 个挡位可选：
   - **sRGB(12)**：原 12 色 sRGB 色卡
   - **sRGB(12)+DisplayP3(7)**：12 色 sRGB + 7 色 Display-P3（宽色域显示器上颜色偏暗淡时选）
   - **sRGB(24)**：24 色行业标准色卡（X-Rite ColorChecker Classic）
   - **sRGB(24)+DisplayP3(7)**：24 色标准色卡 + 7 色 Display-P3
   样本越多矩阵拟合通常越稳健，但测量耗时越长。

6. **历史灰阶数据（跳过 PQ 曲线重测）**  
   在同一台显示器上做多个色温（白点）校色时，PQ 灰阶曲线其实不必每次都重测——显示器的原生响应与目标白点无关，变的只是白点适配。  
   在“Historical gray data”（历史灰阶数据）下拉框中选一条过去**完整**的灰阶测量（每条按测量结束时间标注），PQ 曲线步骤就会直接复用该数据，不再测量。  
   点“Refresh”（刷新）可重新读取日志列表（例如本次会话刚校准完产生的新测量）；校准完成时列表也会自动刷新。  
   注意：历史数据必须来自同一台显示器；中断/不完整的测量不会出现在下拉框中。

7. **历史颜色数据（跳过原色与色卡重测）**  
   同一台显示器做多个色温 profile 时，颜色那一半测量同样不必每次重做。原色/白点/纸白/黑场，以及色卡采样，都是在写入校准 ICC **之前**测的，
   也就是在「单位阵 + 平坦 LUT」的临时预览 profile 下测的——它们记录的是该码值下**面板的原生响应**，与目标白点无关；
   `calibrate_chromaticity` 自己也不对实测值做白点适配。  
   在“Historical color data”（历史颜色数据）下拉框中选一条过去**完整**的颜色测量（按测量结束时间标注，并显示色卡样本数与采样集），
   整个颜色部分都会被跳过：
   - 色域测试点（红/绿/蓝/白/纸白/黑）与激活黑的二分搜索；
   - 色卡测量（12–25 次读数，每次约 1.5 秒）；
   - `rXYZ`/`gXYZ`/`bXYZ`/`wtpt`、`lumi`、TRC 与 `MHC2` 的峰值/黑场全部取自该历史 run——走的是与实时测量**完全相同**的代码路径
     （`_apply_gamut_data`），因此产出的 profile 构造方式没有任何区别。

   ### 完全不接色度计也能校准

   如果**两个**下拉框都选了历史数据（一条历史灰阶 + 一条历史颜色），本次校准完全不需要任何测量。此时程序：
   - **不会**启动 `dogegen` 与 `spotread`；
   - **不会**弹出「放置色度计」对话框；
   - 也会跳过「校准后」的色域复测（那一步只是用同样的原色重算同样的标签），

   于是可以点「校准」再点「保存为 ICC 文件」，在没有连接任何仪器的情况下产出 profile。日志会明确写出：
   ```
   历史数据已足够：本次校准不需要连接色度计
   已复用历史颜色数据：跳过色域与色卡测量
   已复用历史颜色数据：跳过校准后的色域复测
   ```
   需要测量的部分依然是测量：复用的两条 run 必须来自同一台显示器的同一状态，ICC 的好坏等同于这两条 run 的好坏。
   「测量色准」本身是实时测量，仍然需要色度计——缺仪器时会直接提示，而不是报错。

   **如何带进 CLUT 算法**：复用的原色与白点正是 `primaries_matrix()` 用来构造正向模型（`DisplayModel`）线性 `RGB → XYZ` 矩阵的输入，
   而这个矩阵就是 CLUT 的 `A2B0` 方向；此外，复用的色卡样本会挂到模型上，生成 CLUT 时程序会用它们校验正向模型并写入日志：
   ```
   CLUT 颜色校验（A2B 对实测色卡）：measured colour card: 25 samples, mean dE ITP 1.83,
   median 1.55, max 5.41, mean |ΔXYZ| 12.4 nit
   ```
   色卡数据只作为**校验集**使用，不参与拟合——它不会悄悄改变「实测原色 + `MHC2`」已经决定的 CLUT（依旧保持“矩阵为单位阵、
   由实测原色 + 1D LUT 完成色域映射”的策略）。平均 ΔE ITP 超过刻意放宽的 15 时程序会给出警告：
   这是**模型自一致性**指标而非绝对色准数字，因为面板自身的重复性误差就会体现在其中
   （维护者显示器上实测：相隔 8–40 分钟的两次灰阶测量，PQ 输出差异最大 0.016；把色卡与「2.4 小时前的灰阶」构造的模型相比，
   平均 ΔE ITP ≈ 149）。数值明显偏大时说明复用的历史数据与本次会话不一致，例如该 run 来自另一台显示器。

   注意事项：
   - 只列出**完整**的 run：六个色域点齐全 **且** 至少有一个色卡样本；被取消的校准不会出现在列表里。
   - 颜色 run 仍然只是「某台显示器在某个状态下的一次测量」：只在同一显示器、同一 SDR 纸白 / HDR 模式、且没有加载校准 ICC 时有效。任一条件变了请重新测量。
   - 点“Refresh”（刷新）重新读取列表；校准完成时也会自动刷新。
   - 颜色 run 与灰阶历史从同一个 `hc.log` 解析、共用日期推断，所以两个下拉框显示的时间基准一致。

8. **明亮模式**  
   对生成的 LUT 进行整体提升(1D LUT*1.1)，仅适合在强环境光下观看电影。

9. **预览校准结果**  
   当执行了校准后，矩阵和 LUT 会存储在内存中。勾选“预览校准结果”会生成临时 ICC 文件并加载到选中的屏幕，取消勾选则自动移除。  
   未执行校准时，加载的是理想 HDR ICC（BT.2020 色域，10000 nit，恒等矩阵和 无修正LUT）。

10. **校准**  
   生成矩阵和 LUT。

11. **测量色准**  
   测量屏幕的色准（若选中“预览校准结果”，会将当前矩阵和 LUT 临时加载到屏幕上再测量）。  
   没有深入验证这个功能的准确性

12. **保存**  
   将矩阵和 LUT 保存为 ICC 配置文件。

13. **CLUT（A2B0/B2A0）输出**  
    勾选后，保存/预览生成的 ICC 会**额外**写入 ICC 标准的多维查找表标签（`A2B0` 前向、`B2A0` 反向，格式为 `lut16Type`），
    右侧下拉框选择网格点数（17/25/33/37/45/65，默认 33）。原有的矩阵标签与 `MHC2` 标签**完全保留**，CLUT 只是补上标准那一层，
    不会影响 Windows HDR 校准原本的行为。

    - **数据来源**：全部来自本次校准已经测到的数据（实测原色/白点 + `MHC2` 每通道曲线 + 峰值/黑场），**不需要任何额外测量**。
      这里的实测原色可以来自实时测量，也可以来自「历史颜色数据」（见第 7 条）——两种情况 CLUT 的输入完全相同。
    - **设备编码**：`RGB` + ST 2084（PQ）传递函数，也就是 HDR 显示器在 Windows HDR 模式下的原生输入；
      CLUT 的轴就是 PQ 码值（0–1023 归一）。
    - **PCS 锚定**：peak-relative XYZ —— 显示器能输出的最亮白对应 PCS `Y = 1.0`，`X/Z` 按其自身色度等比取值。
      这与 DisplayCAL 生成的 XYZLUT 显示器 profile 采用同一约定（`wtpt` 为设备原生白、`lumi` 给出峰值亮度）。
    - **已知局限（重要）**：ICC 的 PCS 每个分量只能表示 0–1。HDR 显示器上很多「亮而饱和」的颜色，其 XYZ 会有分量超过白点
      （例如 BT.2020 原色在峰值白上，红 `X≈0.95`、蓝 `Z≈1.19`），这些分量会被裁切到 1.0。于是：
      * `A2B0`（前向）：完全准确，合成显示器自检的平均插值误差约 **0.005%**（33³）；
      * `B2A0`（反向）：在**未被裁切**的颜色上可精确求逆（闭环中位误差约 0.002%）；在被裁切的高光区，
        不同设备码值会映射到同一个 PCS 值，反向映射本质上是多对一的，只能给出该请求下的一个合理解。
        因此对色准要求高的场合请以 `A2B0`（前向描述）为准。
    - **耗时**：主要开销是 `B2A0` 反向求解。维护者机器上实测：17³ ≈ 5 秒、25³ ≈ 17 秒、
      33³ ≈ 37 秒；45³/65³ 大致按节点数增长（分钟级）。结果按校准数据缓存，预览与保存不会重复计算。
      保存时这一步在工作线程里跑，界面保持响应（期间控件禁用、日志会说明预计耗时）；
      不勾选 CLUT 的保存则完全没有慢步骤。
    - **自检**：`python tools/verify_clut_profile.py`（合成显示器全链路自检，25 项）、
      `python tools/verify_clut_app_integration.py`（app 集成路径自检，13 项）与
      `python tools/verify_color_history.py`（历史颜色数据解析 + 复用 + 免仪器校准 + CLUT 校验，40 项）。

## 集成的外部工具

- **色彩生成器**  
  dogegen  
  <https://github.com/ledoge/dogegen>

- **校色设备驱动 / 测量工具**  
  ArgyllCMS `spotread`  
  <https://www.argyllcms.com/>  
  因为displaycal也是使用同的驱动，因此也可以参考displaycal文档

## 色度计校准说明

色度计需要不同类型屏幕对应的光皮校准文件，具体可参考：

- ArgyllCMS 文档：<https://www.argyllcms.com/doc/oeminst.html>  
- DisplayCAL 相关教程与文档

## 关于本项目的代码

作者并不是色彩科学相关职业，因此校色逻辑可能并非最佳  
如发现问题或有更好的想法，欢迎指出来

版本历史见 [CHANGELOG_zh](CHANGELOG_zh.md)（English: [CHANGELOG](CHANGELOG.md)）。  
发布版本以 `v<年>.<月>.<日>` 打标签（如 `v2026.09.02`、`v2026.09.22`），并在
[Releases](https://github.com/CodeBH0/rwhc-enhance/releases) 页面附上打包好的压缩包。

## 许可证

本项目采用 GNU Affero General Public License v3.0（AGPL-3.0）许可协议。  
详细条款请参见项目根目录中的 `LICENSE` 文件。
