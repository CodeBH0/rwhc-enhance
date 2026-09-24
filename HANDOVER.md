# 交接说明

## Start here

WinUI 迁移已完成第二阶段：IPC 现在具备严格请求契约、异步事件、提示交互和取消能力，
第一组真实设备设置也已接入。下一步应在不改写校准数学的前提下，把现有 Tk 长任务抽成
backend operation，并实现 `calibration.start`。

开始前阅读：

- `docs/BACKEND.md`
- `docs/FRONTEND_IPC.md`
- `calibration_contract.py`
- `backend_host.py`
- `frontend/Rwhc.WinUI/Services/PythonBackendClient.cs`

## 当前目标与结果

- 长期目标：以 C# + WinUI 3 替换 Python/Tk 前端；Tk 只作为迁移期功能和交互参考。
- 本轮目标：稳定 WinUI/Python 边界并迁移显示器、SDR paper white、仪器选项，不追求
  完整 UI parity，不修改校准算法。
- 结果：协议升级为 v1.1；短 RPC 与异步 operation 可并发；`CalibrationRequest` schema v1
  已有严格 codec/校验；WinUI 已展示 backend 的真实设备数据，并通过完整 smoke test。

## 已完成工作

### 请求契约

- 新增 `calibration_contract.py`，作为 `CalibrationRequest` 唯一 wire codec。
- schema v1 使用 camelCase；拒绝缺失必填字段、未知字段、错误 JSON 类型和非法取值。
- 明确校验 grayscale/color set/white point/EETF/CLUT 等范围；输出规范化数据。
- 历史测量仅传 backend 生成的不透明 ID，不把 Python 内部 history dict 暴露给 C#。
- `calibration.validateRequest` 会解析、校验并返回规范化请求；实际开始校准时仍须再次校验。

### IPC 与异步 operation

- `backend_host.py` 协议主版本仍为 1，minor 为 1；兼容首版缺少 `type` 的 v1 请求。
- stdout 复用 `response` 与 `event` 帧；每个 operation 有唯一 ID 和严格递增 sequence。
- 支持 `progress`、`log`、`prompt`、`result`、`error` 事件。
- prompt 在 worker 内等待，stdin 主循环和短 RPC 继续响应；使用 `prompt.respond` 回答。
- `operation.cancel` 提供协作式取消；EOF 会取消任务并清理测量进程。
- 同时仅允许一个长任务，以保护单实例 `CalibrationState` 和色度计。
- `diagnostics.startEventProbe` 只用于验证事件/提示/取消传输，不是假校准功能。

### 第一组真实功能

- `DisplayService.list_displays()` 返回真实显示器 DTO；`paper_white_info()` 返回系统 SDR
  paper white 或明确标记的 fallback。
- `InstrumentService.list_options()` 调用真实 `spotread --help` 发现，返回 instrument/mode。
- 新增短 RPC：`display.list`、`display.getPaperWhite`、`instrument.listOptions`、
  `history.list`、`calibration.validateRequest`。
- WinUI 页面已使用真实显示器选择、paper white、仪器和模式下拉框；不使用 UI mock。
- C# client 使用单 stdout pump 分流 response/event，支持并发短 RPC、结构化错误、超时和
  退出清理；UI 更新切回 DispatcherQueue。
- `--smoke-test` 会走真实 backend describe、设备发现、请求校验、异步事件和 prompt 回答。

## 关键设计决定

- 保持“WinUI 持有长生命周期 Python 子进程 + UTF-8 无 BOM NDJSON/stdio”；不嵌入
  CPython，也不引入本地 HTTP/端口。
- 协议以 capabilities、request schema versions 和 event types 协商；minor 版本只允许
  添加可选能力，破坏性变更必须提升主版本或 schema 版本。
- stdout 只用于协议，诊断写 stderr；所有写帧使用锁保证跨线程原子性。
- cancellation 是协作式：Python 循环和测量间隙必须检查；阻塞的原生/子进程读取需要由
  adapter 终止进程才能及时取消。
- 本轮未改写 `calibrate_pq`、`calibrate_chromaticity` 或颜色计算。

## 距离完整校准仍缺的接口

1. `calibration.start`：再次校验 request，调用 `begin_calibration`，在 worker 中编排现有算法。
2. 把 Tk 内长任务抽成 backend 可调用的算法 adapter，并为 pattern generator 写入、色度计
   读取建立显式 port；不要搬运/重写数学。
3. 在每次测量之间加入取消检查；阻塞 `spotread` 时实现进程终止和统一清理。
4. 将现有 Python logging 按 operation 关联为 `log` event；完善仪器校准、放置等 prompt。
5. 定义成功结果 DTO（profile、校准摘要、history ID）和失败/取消后的 preview ICC 回滚策略。
6. 增加保存/导出 RPC；路径选择归 WinUI，文件生成归 Python。
7. WinUI 增加正式 progress/log/prompt/result UI；当前只有设备控件和通用事件状态。
8. 准确度测量仍需单独 operation；历史选择 UI 尚未迁移，但 `history.list` 已具备。

## 验证结果

- 全部 11 个 `tools/verify_*.py` 脚本通过：backend host 21/21、request contract 16/16、
  calibration backend 17/17、app UI 7/7、CLUT app 13/13、CLUT profile 25/25、
  color history 41/41、LUT inverse 36/36，另含 24-card/matrix/warm-matrix 诊断。
- Python 相关文件 `py_compile` 通过。
- `dotnet build frontend\Rwhc.WinUI\Rwhc.WinUI.csproj -c Debug -p:Platform=x64
  --no-restore`：0 warning / 0 error。
- WinUI `--smoke-test`：exit 0；已发现真实显示器、完成 schema v1 请求校验并走通
  progress/log/prompt/result。当前机器 instrument 数为 0，反映真实 `spotread` 状态。
- 普通 WinUI 窗口启动后保持运行 5 秒，再关闭该测试进程。
- PowerShell 跑全量旧脚本需设置项目根目录为 `PYTHONPATH` 和 `PYTHONIOENCODING=utf-8`；
  否则旧脚本可能因相对 import 或 GBK 无法输出 Unicode 勾号而失败，这不是代码回归。

## 风险与约束

- 当前只验证 x64；新机器首次 build 需要 NuGet restore。
- WinUI 使用 .NET SDK 10.0.401、Windows App SDK 2.5.1、unpackaged/self-contained。
- 未接真实色度计执行完整校准，未写系统 ICC。
- 同一 backend host 不支持并行校准，这是有意的硬件/状态约束。
- request schema v1 的字段语义已冻结；新增可选字段可保持 v1，破坏性变化必须增加 schema。

## Git 状态

- 分支 `master`；本轮迁移成果已作为单次提交保存。
- 工作区在本轮之前已有 backend/UI 解耦修改和未跟踪文件，均已保留。
- 本轮主要新增/修改：`calibration_contract.py`、`backend_host.py`、
  `calibration_backend.py`、`docs/FRONTEND_IPC.md`、`docs/BACKEND.md`、
  `frontend/Rwhc.WinUI/`、`tools/verify_backend_host.py`、
  `tools/verify_calibration_contract.py`、README/CHANGELOG/HANDOVER。
