# 交接说明

## Start here

新版 WinUI 已形成“启动 → 选择实时/历史数据 → calibration.start → progress/log/prompt →
result/error”的首个使用闭环。推荐入口是仓库根目录 `run-winui.cmd`；`app.py` 明确为
legacy Tk UI。下一步应优先实现 profile 保存/导出，而不是继续扩展基础架构。

## 当前目标与结果

- 已实现一键启动、自动 backend 生命周期、历史数据选择、无色度计回放和用户可理解的
  首屏/错误提示。
- 未修改 `calibrate_pq()`、`calibrate_chromaticity()` 等校准数学；正式与历史模式仍走
  同一个 `CalibrationWorkflow`/`calibration.start`。
- 当前无色度计环境已通过现有真实历史数据完成完整 C# → IPC → Python workflow → result
  验证；硬件隔离只发生在显式 smoke test 的 Windows display/ICC platform 边缘。

## 已完成工作

### 应用入口

- 新增 `run-winui.cmd` 与 `run-winui.ps1`。启动器发现 `.venv314` 或系统 Python，执行
  backend import 检查并运行 WinUI；`-CheckOnly` 可只检查环境。
- README/README_zh/WinUI README 明确 WinUI 是新版入口，`app.py` 是 legacy Tk UI。
- `PythonBackendClient` 继续自行启动 `backend_host.py`、关闭 stdin、等待退出并在必要时
  kill process tree；用户无需手动启动 backend。

### 历史数据使用闭环

- WinUI client/model 新增 `history.list` DTO 与调用。
- 页面增加历史灰阶/颜色选择、灰阶采样数、颜色样本集和白点；没有色度计但存在完整
  历史组合时自动选择最近一对，并把按钮切换为“使用历史数据校准”。
- 双历史请求使用普通非空仪器占位描述通过 schema，但 backend 根据 resolved history run
  判定 history-only，不启动 `dogegen`/`spotread`，仍执行真实校准数学和 profile 写入链路。
- progress phase 显示中文含义，日志实时追加，result 显示历史/实时模式及样本数量。

### 无硬件验证

- `tools/verify_backend_host.py` 从现有 `hc.log` 复制并解析真实历史 run，通过
  `calibration.start` 完成 history replay；测试把测量启动替换为必定抛错，证明没有访问
  色度计，同时要求 progress/log 且不得出现硬件 prompt。
- WinUI 新增 `--history-smoke-test`：C# 启动真实 backend 子进程、列出历史、校验请求、
  启动 workflow 并等待 result。
- backend 的 `--replay-test-hardware` 只供上述 smoke 使用：注入 no-op HDR display/ICC
  platform，并从临时 `hc.log` 副本工作；request/history/workflow/算法均为正式实现，且不
  污染用户日志。普通 WinUI 从不传该参数。

### 用户体验

- 首屏会明确显示下一步：实时校准、历史无仪器校准、未发现显示器或缺少完整历史数据。
- backend/Python 启动失败包含解释器、依赖和 stderr 尾部信息；失败 client 会释放，刷新可
  重试。
- 参数无效、HDR 前置条件、任务冲突、prompt 超时和内部执行失败均显示更可理解的信息。
- 修复任务日志中文乱码：C# 子进程环境设置 `PYTHONIOENCODING=utf-8`/`PYTHONUTF8=1`，
  Python host 同时强制重配 stdin/stdout/stderr 为 UTF-8；history smoke 校验真实中文 log
  包含“历史”且不含 Unicode replacement character。

## 主要文件

- `run-winui.cmd` / `run-winui.ps1`：推荐启动入口。
- `frontend/Rwhc.WinUI/MainPage.xaml(.cs)`：历史/参数选择、引导与 operation 展示。
- `frontend/Rwhc.WinUI/Services/ProtocolModels.cs`：history DTO。
- `frontend/Rwhc.WinUI/Services/PythonBackendClient.cs`：history RPC、backend 启动诊断。
- `frontend/Rwhc.WinUI/App.xaml.cs`：普通 smoke 与 history smoke。
- `backend_host.py`：`calibration.start` 及显式 replay test display adapter。
- `calibration_workflow.py`：无 Tk 的真实校准执行器。
- `tools/verify_backend_host.py`：实时算法链与真实历史回放验证。

## 验证结果

- 11 个 `tools/verify_*.py` 全部通过：backend host 29/29、request contract 16/16、
  calibration backend 17/17、app UI 7/7、CLUT app 13/13、CLUT profile 25/25、
  color history 41/41、LUT inverse 36/36，另含 24-card/matrix/warm-matrix 诊断。
- Python `py_compile` 通过。
- WinUI x64 Debug build：0 warning / 0 error。
- 普通 `--smoke-test`：exit 0，protocol 1.1、schema 1、异步 prompt/event 正常。
- `--history-smoke-test`：exit 0；progress 258、logs 17、prompts 0、unicodeLogs=True、
  completed=True、historyOnly=True。
- `run-winui.cmd -CheckOnly`：通过。
- `git diff --check`：通过，仅有 Git 的 LF→CRLF 提示。

## 距离替代 Tk UI 的剩余功能

1. profile 保存/另存为、最终安装/关联和路径选择。
2. CLUT、bright mode、EETF 等完整高级参数 UI。
3. 色准测量、报告/图表及其他工具入口。
4. 更严格的历史数据与显示器身份/纸白状态匹配元数据；当前仍依赖用户确认。
5. 真实色度计上的 live workflow、测量中取消和驱动兼容性验证。
6. 发布打包/安装体验；目前仍要求 .NET 10 SDK 与 Python 环境。

## 风险与说明

- 当前机器无色度计，未做真实硬件 live calibration。
- 一次直接使用真实 Windows ICC 边缘的历史 smoke 遇到 `InstallColorProfileW WinErr=5`；
  之后 smoke 已改为显式可注入 display adapter。普通应用仍使用正式 Windows API，实际
  用户环境的 ICC 权限需要继续验证。
- `calibration.start` 结果仍只保存在 backend 会话内，关闭应用前尚无 WinUI 保存入口。

## Git 状态

- 分支 `master`，远端为 `origin`（GitHub）。
- WinUI 校准 workflow、历史回放、启动闭环及 UTF-8 日志修复已合并为当前最新提交；
  本地 `master` 比 `origin/master` 超前 2 个提交。
- 2026-09-24 三次推送 GitHub 均因无法连接 `github.com:443` 失败；网络恢复后执行
  `git push origin master`。
