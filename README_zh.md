# Windows 11 真正可用的 HDR 校准软件
中文|[ENGLISH](https://github.com/forbxy/rwhc/blob/master/README.md)

![screenshot](https://github.com/forbxy/rwhc/blob/master/resources/ui_zh.png)

## 使用方法

1. **获取项目代码**  
   下载或 clone 本项目，进入项目根目录。

2. **安装 Python**  
   在 Windows 上安装 Python（任选其一）：
   - Microsoft Store
   - 官方网站：<https://www.python.org/downloads/windows/>

3. **安装依赖**  
   在项目根目录下执行：

   ```bash
   pip install -r requirements.txt
   ```

4. **运行程序**

   ```bash
   python app.py
   ```
   点击校准按钮开始校准

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

7. **明亮模式**  
   对生成的 LUT 进行整体提升(1D LUT*1.1)，仅适合在强环境光下观看电影。

8. **预览校准结果**  
   当执行了校准后，矩阵和 LUT 会存储在内存中。勾选“预览校准结果”会生成临时 ICC 文件并加载到选中的屏幕，取消勾选则自动移除。  
   未执行校准时，加载的是理想 HDR ICC（BT.2020 色域，10000 nit，恒等矩阵和 无修正LUT）。

9. **校准**  
   生成矩阵和 LUT。

10. **测量色准**  
   测量屏幕的色准（若选中“预览校准结果”，会将当前矩阵和 LUT 临时加载到屏幕上再测量）。  
   没有深入验证这个功能的准确性

11. **保存**  
   将矩阵和 LUT 保存为 ICC 配置文件。

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

## 支持
如果你愿意支持作者买一个光度计  
USDT_ERC20:0xa7475effb3f2c5fcb618e8052fc4c45ccc9d9710  
BTC: bc1qa77v8als2f7qradmtmjjy5ad057q9yws6nanx6  

## 许可证

本项目采用 GNU Affero General Public License v3.0（AGPL-3.0）许可协议。  
详细条款请参见项目根目录中的 `LICENSE` 文件。