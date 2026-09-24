using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Rwhc.WinUI.Services;

namespace Rwhc.WinUI;

public sealed partial class MainPage : Page
{
    private PythonBackendClient? _backendClient;
    private bool _refreshing;

    public MainPage()
    {
        InitializeComponent();
        Loaded += MainPage_Loaded;
        Unloaded += MainPage_Unloaded;
    }

    private async void MainPage_Loaded(object sender, RoutedEventArgs e)
    {
        await RefreshBackendDataAsync();
    }

    private async void RefreshButton_Click(object sender, RoutedEventArgs e)
    {
        await RefreshBackendDataAsync();
    }

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
        ResultInfoBar.IsOpen = false;
        StatusText.Text = "正在读取 Python backend…";

        try
        {
            PythonBackendClient client = EnsureClient();
            Task<BackendDescription> descriptionTask = client.DescribeAsync();
            Task<DisplayList> displaysTask = client.ListDisplaysAsync();
            Task<InstrumentOptions> instrumentsTask = client.ListInstrumentOptionsAsync();
            await Task.WhenAll(descriptionTask, displaysTask, instrumentsTask);

            BackendDescription description = await descriptionTask;
            DisplayInfo[] displays = (await displaysTask).Displays;
            InstrumentOptions instruments = await instrumentsTask;

            DisplayComboBox.ItemsSource = displays;
            DisplayComboBox.SelectedIndex = displays.Length > 0 ? 0 : -1;
            InstrumentComboBox.ItemsSource = instruments.Instruments;
            InstrumentComboBox.SelectedIndex = instruments.Instruments.Length > 0 ? 0 : -1;
            ModeComboBox.ItemsSource = instruments.Modes;
            ModeComboBox.SelectedIndex = instruments.Modes.Length > 0 ? 0 : -1;

            StatusText.Text =
                $"已连接 · protocol v{description.ProtocolVersion}.{description.ProtocolMinorVersion} " +
                $"· Python {description.PythonVersion}";
            DetailText.Text =
                $"{description.BackendType} 已加载；MHC2 {description.Mhc2EntryCount} 项；" +
                $"CalibrationRequest schema: {string.Join(", ", description.RequestSchemaVersions)}。";
            DeviceSummaryText.Text =
                $"backend 返回 {displays.Length} 台显示器、" +
                $"{instruments.Instruments.Length} 个仪器选项、{instruments.Modes.Length} 个测量模式。";
            ResultInfoBar.Severity = InfoBarSeverity.Success;
            ResultInfoBar.Title = "真实 backend 数据已刷新";
            ResultInfoBar.Message = "显示器、SDR paper white 与 Argyll 选项均来自 Python service。";
            ResultInfoBar.IsOpen = true;
        }
        catch (Exception exception)
        {
            StatusText.Text = "连接失败";
            DetailText.Text = exception.Message;
            ResultInfoBar.Severity = InfoBarSeverity.Error;
            ResultInfoBar.Title = "Python backend 调用失败";
            ResultInfoBar.Message = exception.Message;
            ResultInfoBar.IsOpen = true;
        }
        finally
        {
            RefreshButton.IsEnabled = true;
            _refreshing = false;
        }
    }

    private async void DisplayComboBox_SelectionChanged(
        object sender,
        SelectionChangedEventArgs e)
    {
        if (_backendClient is null || DisplayComboBox.SelectedItem is not DisplayInfo display)
        {
            PaperWhiteText.Text = "—";
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
            PaperWhiteText.Text = $"读取失败：{exception.Message}";
        }
    }

    private void BackendClient_EventReceived(object? sender, BackendEvent backendEvent)
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            EventStatusText.Text =
                $"最近事件：{backendEvent.Event} · operation {backendEvent.OperationId} " +
                $"· seq {backendEvent.Sequence}";
        });
    }

    private async void MainPage_Unloaded(object sender, RoutedEventArgs e)
    {
        if (_backendClient is not null)
        {
            _backendClient.EventReceived -= BackendClient_EventReceived;
            await _backendClient.DisposeAsync();
            _backendClient = null;
        }
    }
}
