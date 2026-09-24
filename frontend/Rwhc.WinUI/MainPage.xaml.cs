using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Rwhc.WinUI.Services;
using System.Text.Json;

namespace Rwhc.WinUI;

public sealed partial class MainPage : Page
{
    private PythonBackendClient? _backendClient;
    private bool _refreshing;
    private string? _calibrationOperationId;
    private bool _startingCalibration;
    private bool _promptOpen;

    public MainPage()
    {
        InitializeComponent();
        GrayscaleSamplesComboBox.ItemsSource = new[] { 128, 256, 512, 1024 };
        GrayscaleSamplesComboBox.SelectedIndex = 0;
        ColorSampleSetComboBox.ItemsSource = new[]
        {
            "sRGB(12)",
            "sRGB(12)+DisplayP3(7)",
            "sRGB(24)",
            "sRGB(24)+DisplayP3(7)",
        };
        ColorSampleSetComboBox.SelectedIndex = 0;
        InstrumentComboBox.SelectionChanged += CalibrationInput_SelectionChanged;
        ModeComboBox.SelectionChanged += CalibrationInput_SelectionChanged;
        Loaded += MainPage_Loaded;
        Unloaded += MainPage_Unloaded;
    }

    private async void MainPage_Loaded(object sender, RoutedEventArgs e) =>
        await RefreshBackendDataAsync();

    private async void RefreshButton_Click(object sender, RoutedEventArgs e) =>
        await RefreshBackendDataAsync();

    private PythonBackendClient EnsureClient()
    {
        if (_backendClient is not null)
        {
            return _backendClient;
        }
        _backendClient = PythonBackendClient.CreateForDevelopment();
        _backendClient.EventReceived += BackendClient_EventReceived;
        return _backendClient;
    }

    private async Task RefreshBackendDataAsync()
    {
        if (_refreshing)
        {
            return;
        }
        _refreshing = true;
        RefreshButton.IsEnabled = false;
        StartCalibrationButton.IsEnabled = false;
        ResultInfoBar.IsOpen = false;
        StatusText.Text = "正在读取 Python backend…";

        try
        {
            PythonBackendClient client = EnsureClient();
            Task<BackendDescription> descriptionTask = client.DescribeAsync();
            Task<DisplayList> displaysTask = client.ListDisplaysAsync();
            Task<InstrumentOptions> instrumentsTask = client.ListInstrumentOptionsAsync();
            Task<HistoryList> historyTask = client.ListHistoryAsync();
            await Task.WhenAll(descriptionTask, displaysTask, instrumentsTask, historyTask);

            BackendDescription description = await descriptionTask;
            DisplayInfo[] displays = (await displaysTask).Displays;
            InstrumentOptions instruments = await instrumentsTask;
            HistoryList history = await historyTask;

            DisplayComboBox.ItemsSource = displays;
            DisplayComboBox.SelectedIndex = displays.Length > 0 ? 0 : -1;
            InstrumentComboBox.ItemsSource = instruments.Instruments;
            InstrumentComboBox.SelectedIndex = instruments.Instruments.Length > 0 ? 0 : -1;
            ModeComboBox.ItemsSource = instruments.Modes;
            ModeComboBox.SelectedIndex = instruments.Modes.Length > 0 ? 0 : -1;
            GrayHistoryComboBox.ItemsSource = new[]
            {
                new GrayHistoryOption("", "实时测量（需要色度计）", 0),
            }.Concat(history.Gray).ToArray();
            ColorHistoryComboBox.ItemsSource = new[]
            {
                new ColorHistoryOption("", "实时测量（需要色度计）", 0, null),
            }.Concat(history.Color).ToArray();
            bool canReplay = history.Gray.Length > 0 && history.Color.Length > 0;
            bool meterAvailable = instruments.Instruments.Length > 0 && instruments.Modes.Length > 0;
            GrayHistoryComboBox.SelectedIndex = !meterAvailable && canReplay ? 1 : 0;
            ColorHistoryComboBox.SelectedIndex = !meterAvailable && canReplay ? 1 : 0;

            StatusText.Text =
                $"已连接 · protocol v{description.ProtocolVersion}.{description.ProtocolMinorVersion} " +
                $"· Python {description.PythonVersion}";
            DetailText.Text =
                $"{description.BackendType} 已加载；MHC2 {description.Mhc2EntryCount} 项；" +
                $"CalibrationRequest schema: {string.Join(", ", description.RequestSchemaVersions)}。";
            DeviceSummaryText.Text =
                $"发现 {displays.Length} 台显示器、{instruments.Instruments.Length} 个仪器选项、" +
                $"{instruments.Modes.Length} 个测量模式；历史灰阶 {history.Gray.Length} 组、" +
                $"历史颜色 {history.Color.Length} 组。";
            ResultInfoBar.Severity = InfoBarSeverity.Success;
            ResultInfoBar.Title = "新版 WinUI 已连接 backend";
            ResultInfoBar.Message = "backend 由 WinUI 自动管理，无需手动启动 backend_host.py。";
            ResultInfoBar.IsOpen = true;
        }
        catch (Exception exception)
        {
            string message = FriendlyError(exception);
            StatusText.Text = "连接失败";
            DetailText.Text = message;
            ResultInfoBar.Severity = InfoBarSeverity.Error;
            ResultInfoBar.Title = "Python backend 启动失败";
            ResultInfoBar.Message = message;
            ResultInfoBar.IsOpen = true;
            GuidanceInfoBar.Severity = InfoBarSeverity.Error;
            GuidanceInfoBar.Title = "无法继续";
            GuidanceInfoBar.Message = message;
            await ResetBackendClientAsync();
        }
        finally
        {
            RefreshButton.IsEnabled = true;
            _refreshing = false;
            UpdateCalibrationAvailability();
        }
    }

    private async void DisplayComboBox_SelectionChanged(
        object sender,
        SelectionChangedEventArgs e)
    {
        if (_backendClient is null || DisplayComboBox.SelectedItem is not DisplayInfo display)
        {
            PaperWhiteText.Text = "—";
            UpdateCalibrationAvailability();
            return;
        }
        try
        {
            PaperWhiteInfo paperWhite = await _backendClient.GetPaperWhiteAsync(display.Id);
            string source = paperWhite.Source == "system" ? "Windows 系统值" : "fallback";
            PaperWhiteText.Text = $"{paperWhite.Nits:F1} nit · {source}";
        }
        catch (Exception exception)
        {
            PaperWhiteText.Text = $"读取失败：{FriendlyError(exception)}";
        }
        UpdateCalibrationAvailability();
    }

    private void CalibrationInput_SelectionChanged(object sender, SelectionChangedEventArgs e) =>
        UpdateCalibrationAvailability();

    private bool HistoryOnlySelected() =>
        GrayHistoryComboBox.SelectedItem is GrayHistoryOption { Id.Length: > 0 } &&
        ColorHistoryComboBox.SelectedItem is ColorHistoryOption { Id.Length: > 0 };

    private void UpdateCalibrationAvailability()
    {
        if (_refreshing || _calibrationOperationId is not null || _startingCalibration)
        {
            StartCalibrationButton.IsEnabled = false;
            return;
        }
        bool hasDisplay = DisplayComboBox.SelectedItem is DisplayInfo;
        bool historyOnly = HistoryOnlySelected();
        bool hasMeter = InstrumentComboBox.SelectedItem is InstrumentOption &&
            ModeComboBox.SelectedItem is InstrumentOption;
        StartCalibrationButton.IsEnabled = hasDisplay && (historyOnly || hasMeter);
        StartCalibrationButton.Content = historyOnly ? "使用历史数据校准" : "开始实时校准";

        if (!hasDisplay)
        {
            GuidanceInfoBar.Severity = InfoBarSeverity.Error;
            GuidanceInfoBar.Title = "未发现显示器";
            GuidanceInfoBar.Message = "请确认 Windows 能识别目标显示器，然后点击“刷新 backend 设备”。";
        }
        else if (historyOnly)
        {
            GuidanceInfoBar.Severity = InfoBarSeverity.Success;
            GuidanceInfoBar.Title = "可在无色度计模式下运行";
            GuidanceInfoBar.Message = "已选择完整历史数据。请确认它们来自当前显示器及相同 HDR/纸白状态；workflow 不会启动 dogegen/spotread，也不会要求放置仪器。";
        }
        else if (!hasMeter)
        {
            GuidanceInfoBar.Severity = InfoBarSeverity.Warning;
            GuidanceInfoBar.Title = "未检测到色度计";
            GuidanceInfoBar.Message = "请选择一组历史灰阶和历史颜色数据；若没有完整历史数据，则需连接色度计后刷新。";
        }
        else
        {
            GuidanceInfoBar.Severity = InfoBarSeverity.Informational;
            GuidanceInfoBar.Title = "已准备实时校准";
            GuidanceInfoBar.Message = "确认目标显示器已开启 HDR，然后开始校准并按提示放置仪器。";
        }
    }

    private void BackendClient_EventReceived(object? sender, BackendEvent backendEvent)
    {
        DispatcherQueue.TryEnqueue(async () =>
        {
            EventStatusText.Text =
                $"最近事件：{backendEvent.Event} · operation {backendEvent.OperationId} " +
                $"· seq {backendEvent.Sequence}";
            if (_startingCalibration && _calibrationOperationId is null)
            {
                _calibrationOperationId = backendEvent.OperationId;
            }
            if (backendEvent.OperationId != _calibrationOperationId)
            {
                return;
            }
            JsonElement payload = backendEvent.Payload;
            switch (backendEvent.Event)
            {
                case "progress":
                    int current = payload.GetProperty("current").GetInt32();
                    int total = payload.GetProperty("total").GetInt32();
                    string phase = payload.GetProperty("phase").GetString() ?? "calibration";
                    string message = payload.GetProperty("message").GetString() ?? "";
                    CalibrationProgress.IsIndeterminate = total <= 0;
                    if (total > 0)
                    {
                        CalibrationProgress.Maximum = total;
                        CalibrationProgress.Value = current;
                    }
                    CalibrationStatusText.Text = $"{PhaseLabel(phase)} · {current}/{total} {message}";
                    break;
                case "log":
                    CalibrationLogText.Text +=
                        $"[{payload.GetProperty("level").GetString()}] " +
                        $"{payload.GetProperty("message").GetString()}{Environment.NewLine}";
                    break;
                case "prompt":
                    await ShowCalibrationPromptAsync(backendEvent);
                    break;
                case "result":
                    string status = payload.GetProperty("status").GetString() ?? "completed";
                    string resultMessage = status == "completed"
                        ? FormatCalibrationResult(payload.GetProperty("value"))
                        : "校准已取消，测量进程和临时 ICC 已清理。";
                    FinishCalibration(status == "completed", resultMessage);
                    break;
                case "error":
                    string errorCode = payload.GetProperty("code").GetString() ?? "unknown_error";
                    string errorMessage = payload.GetProperty("message").GetString() ?? "校准失败";
                    FinishCalibration(false, FriendlyBackendError(errorCode, errorMessage));
                    break;
            }
        });
    }

    private CalibrationRequestWire BuildCalibrationRequest()
    {
        if (DisplayComboBox.SelectedItem is not DisplayInfo display)
        {
            throw new InvalidOperationException("未发现可用于校准的显示器。");
        }
        string? grayHistoryId = (GrayHistoryComboBox.SelectedItem as GrayHistoryOption)?.Id;
        string? colorHistoryId = (ColorHistoryComboBox.SelectedItem as ColorHistoryOption)?.Id;
        grayHistoryId = string.IsNullOrEmpty(grayHistoryId) ? null : grayHistoryId;
        colorHistoryId = string.IsNullOrEmpty(colorHistoryId) ? null : colorHistoryId;
        bool historyOnly = grayHistoryId is not null && colorHistoryId is not null;
        InstrumentOption? instrument = InstrumentComboBox.SelectedItem as InstrumentOption;
        InstrumentOption? mode = ModeComboBox.SelectedItem as InstrumentOption;
        if (!historyOnly && (instrument is null || mode is null))
        {
            throw new InvalidOperationException(
                "未检测到色度计。请选择完整的历史灰阶和历史颜色数据，或连接色度计后刷新设备。");
        }
        return new CalibrationRequestWire(
            1,
            display.Id,
            instrument?.Description ?? "Historical replay",
            mode?.Description ?? "Historical replay",
            (int)(GrayscaleSamplesComboBox.SelectedItem ?? 128),
            (string)(ColorSampleSetComboBox.SelectedItem ?? "sRGB(12)"),
            WhitePointTextBox.Text,
            EetfArgs: new EetfArguments(),
            GrayHistoryId: grayHistoryId,
            ColorHistoryId: colorHistoryId);
    }

    private async void StartCalibrationButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            StartCalibrationButton.IsEnabled = false;
            PythonBackendClient client = EnsureClient();
            CalibrationRequestWire request = BuildCalibrationRequest();
            RequestValidationResult validation = await client.ValidateCalibrationRequestAsync(request);
            CalibrationLogText.Text = validation.HistoryOnly
                ? "[info] 已验证双历史请求，不会访问色度计。" + Environment.NewLine
                : "[info] 已验证实时测量请求。" + Environment.NewLine;
            CalibrationProgress.Value = 0;
            CalibrationProgress.IsIndeterminate = true;
            CalibrationStatusText.Text = "正在启动 calibration.start…";
            _startingCalibration = true;
            OperationAccepted accepted = await client.StartCalibrationAsync(request);
            if (!_startingCalibration && _calibrationOperationId is null)
            {
                return;
            }
            _calibrationOperationId = accepted.OperationId;
            _startingCalibration = false;
            CancelCalibrationButton.IsEnabled = true;
        }
        catch (Exception exception)
        {
            _startingCalibration = false;
            FinishCalibration(false, FriendlyError(exception));
        }
    }

    private async void CancelCalibrationButton_Click(object sender, RoutedEventArgs e)
    {
        if (_backendClient is null || _calibrationOperationId is null)
        {
            return;
        }
        try
        {
            CancelCalibrationButton.IsEnabled = false;
            await _backendClient.CancelOperationAsync(_calibrationOperationId);
            CalibrationStatusText.Text = "正在取消并清理测量进程…";
        }
        catch (Exception exception)
        {
            FinishCalibration(false, FriendlyError(exception));
        }
    }

    private async Task ShowCalibrationPromptAsync(BackendEvent backendEvent)
    {
        if (_backendClient is null || _promptOpen)
        {
            return;
        }
        _promptOpen = true;
        try
        {
            JsonElement payload = backendEvent.Payload;
            var dialog = new ContentDialog
            {
                XamlRoot = XamlRoot,
                Title = payload.GetProperty("title").GetString(),
                Content = payload.GetProperty("message").GetString(),
                PrimaryButtonText = "继续",
                CloseButtonText = "取消",
                DefaultButton = ContentDialogButton.Primary,
            };
            ContentDialogResult result = await dialog.ShowAsync();
            try
            {
                await _backendClient.RespondToPromptAsync(
                    backendEvent.OperationId,
                    payload.GetProperty("promptId").GetString()!,
                    new { choice = result == ContentDialogResult.Primary ? "continue" : "cancel" });
            }
            catch (BackendProtocolException exception) when (
                exception.Code == "not_found" &&
                _calibrationOperationId != backendEvent.OperationId)
            {
                // The operation was cancelled while the dialog was open.
            }
        }
        finally
        {
            _promptOpen = false;
        }
    }

    private void FinishCalibration(bool succeeded, string message)
    {
        CalibrationProgress.IsIndeterminate = false;
        CalibrationStatusText.Text = message;
        CancelCalibrationButton.IsEnabled = false;
        _calibrationOperationId = null;
        _startingCalibration = false;
        ResultInfoBar.Severity = succeeded ? InfoBarSeverity.Success : InfoBarSeverity.Error;
        ResultInfoBar.Title = succeeded ? "校准完成" : "校准未完成";
        ResultInfoBar.Message = message;
        ResultInfoBar.IsOpen = true;
        UpdateCalibrationAvailability();
    }

    private static string PhaseLabel(string phase) => phase switch
    {
        "instrument-start" => "准备色度计",
        "gamut-before" => "测量原始色域",
        "activated-black" => "检测有效黑位",
        "measure-gray" => "生成 PQ 灰阶 LUT",
        "measure-color" => "生成颜色矩阵",
        "gamut-after" => "复核校准后色域",
        _ => phase,
    };

    private static string FormatCalibrationResult(JsonElement value)
    {
        bool historyOnly = value.TryGetProperty("historyOnly", out JsonElement history) && history.GetBoolean();
        int gray = value.TryGetProperty("grayscaleSamples", out JsonElement grayValue) ? grayValue.GetInt32() : 0;
        int color = value.TryGetProperty("colorSamples", out JsonElement colorValue) ? colorValue.GetInt32() : 0;
        return $"校准 workflow 已完成。模式：{(historyOnly ? "历史数据回放" : "实时测量")}；" +
            $"灰阶 {gray} 点，颜色 {color} 点。结果已保存在当前 backend 会话中。";
    }

    private static string FriendlyError(Exception exception)
    {
        if (exception is BackendProtocolException protocol)
        {
            return protocol.Code switch
            {
                "calibration_precondition_failed" => $"校准条件不满足：{protocol.Message}",
                "validation_error" => $"校准参数无效：{protocol.Message}",
                "operation_conflict" => "已有校准任务正在运行，请先等待或取消当前任务。",
                _ => protocol.Message,
            };
        }
        return exception.Message;
    }

    private static string FriendlyBackendError(string code, string message) => code switch
    {
        "calibration_precondition_failed" => $"校准条件不满足：{message}",
        "prompt_timeout" => "等待用户确认超时，校准已停止并完成清理。",
        "internal_error" => $"Python backend 执行失败：{message}",
        _ => $"{code}: {message}",
    };

    private async Task ResetBackendClientAsync()
    {
        if (_backendClient is null)
        {
            return;
        }
        _backendClient.EventReceived -= BackendClient_EventReceived;
        await _backendClient.DisposeAsync();
        _backendClient = null;
    }

    private async void MainPage_Unloaded(object sender, RoutedEventArgs e) =>
        await ResetBackendClientAsync();
}
