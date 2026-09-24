using Microsoft.UI.Xaml;
using Rwhc.WinUI.Services;
using System.Text.Json;

namespace Rwhc.WinUI;

public partial class App : Application
{
    private Window? _window;

    public App()
    {
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        if (Environment.GetCommandLineArgs().Contains("--smoke-test"))
        {
            _ = RunBackendSmokeTestAsync();
            return;
        }
        if (Environment.GetCommandLineArgs().Contains("--history-smoke-test"))
        {
            _ = RunHistoryReplaySmokeTestAsync();
            return;
        }

        _window = new MainWindow();
        _window.Activate();
    }

    private static async Task RunBackendSmokeTestAsync()
    {
        string resultPath = Path.Combine(Path.GetTempPath(), "rwhc-winui-smoke.txt");
        try
        {
            await using PythonBackendClient client =
                PythonBackendClient.CreateForDevelopment();
            BackendDescription description = await client.DescribeAsync();
            DisplayInfo[] displays = (await client.ListDisplaysAsync()).Displays;
            InstrumentOptions instruments = await client.ListInstrumentOptionsAsync();
            if (displays.Length > 0)
            {
                _ = await client.GetPaperWhiteAsync(displays[0].Id);
            }

            var request = new CalibrationRequestWire(
                SchemaVersion: 1,
                MonitorId: displays.FirstOrDefault()?.Id ?? "smoke-display",
                InstrumentDescription: instruments.Instruments.FirstOrDefault()?.Description ?? "Not selected",
                InstrumentModeDescription: instruments.Modes.FirstOrDefault()?.Description ?? "Not selected",
                GrayscaleSamples: 128,
                ColorSampleSet: "sRGB(12)",
                WhitePoint: "0.3127,0.3290",
                EetfArgs: new EetfArguments());
            RequestValidationResult validation =
                await client.ValidateCalibrationRequestAsync(request);

            var terminalEvent = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            async void OnBackendEvent(object? sender, BackendEvent backendEvent)
            {
                try
                {
                    if (backendEvent.Event == "prompt")
                    {
                        string promptId = backendEvent.Payload.GetProperty("promptId").GetString()!;
                        await client.RespondToPromptAsync(
                            backendEvent.OperationId,
                            promptId,
                            new { choice = "continue" });
                    }
                    else if (backendEvent.Event == "result")
                    {
                        terminalEvent.TrySetResult(
                            backendEvent.Payload.GetProperty("status").GetString() == "completed");
                    }
                    else if (backendEvent.Event == "error")
                    {
                        terminalEvent.TrySetException(new InvalidOperationException(
                            backendEvent.Payload.GetProperty("message").GetString()));
                    }
                }
                catch (Exception exception)
                {
                    terminalEvent.TrySetException(exception);
                }
            }

            client.EventReceived += OnBackendEvent;
            OperationAccepted operation = await client.StartEventProbeAsync(
                steps: 2,
                delayMs: 1,
                requirePrompt: true);
            bool eventsCompleted = await terminalEvent.Task.WaitAsync(TimeSpan.FromSeconds(10));
            client.EventReceived -= OnBackendEvent;

            await File.WriteAllTextAsync(
                resultPath,
                $"OK protocol={description.ProtocolVersion}.{description.ProtocolMinorVersion} " +
                $"backend={description.BackendType} mhc2={description.Mhc2EntryCount} " +
                $"displays={displays.Length} instruments={instruments.Instruments.Length} " +
                $"requestSchema={validation.SchemaVersion} events={eventsCompleted} " +
                $"operation={operation.OperationId}");
            Environment.Exit(
                description.ProfileReady
                && description.Mhc2EntryCount > 1
                && validation.Valid
                && eventsCompleted
                    ? 0
                    : 2);
        }
        catch (Exception exception)
        {
            await File.WriteAllTextAsync(resultPath, exception.ToString());
            Environment.Exit(1);
        }
    }

    private static async Task RunHistoryReplaySmokeTestAsync()
    {
        string resultPath = Path.Combine(Path.GetTempPath(), "rwhc-winui-history-smoke.txt");
        try
        {
            await using PythonBackendClient client = PythonBackendClient.CreateForHistoryReplayTest();
            DisplayInfo display = (await client.ListDisplaysAsync()).Displays.FirstOrDefault(item => item.IsHdr)
                ?? throw new InvalidOperationException("历史回放需要一台已开启 HDR 的显示器。");
            HistoryList history = await client.ListHistoryAsync();
            GrayHistoryOption gray = history.Gray.FirstOrDefault()
                ?? throw new InvalidOperationException("没有完整的历史灰阶数据。");
            ColorHistoryOption color = history.Color.FirstOrDefault()
                ?? throw new InvalidOperationException("没有完整的历史颜色数据。");
            int graySamples = new[] { 128, 256, 512, 1024 }.Contains(gray.Samples)
                ? gray.Samples
                : 128;
            var request = new CalibrationRequestWire(
                1,
                display.Id,
                "Historical replay",
                "Historical replay",
                graySamples,
                "sRGB(12)",
                "0.3127,0.3290",
                EetfArgs: new EetfArguments(),
                GrayHistoryId: gray.Id,
                ColorHistoryId: color.Id);
            RequestValidationResult validation = await client.ValidateCalibrationRequestAsync(request);
            if (!validation.HistoryOnly)
            {
                throw new InvalidOperationException("历史请求未被 backend 识别为 history-only。 ");
            }

            int progressCount = 0;
            int logCount = 0;
            int promptCount = 0;
            int unicodeLogCount = 0;
            int malformedUnicodeCount = 0;
            var terminal = new TaskCompletionSource<JsonElement>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            async void OnBackendEvent(object? sender, BackendEvent backendEvent)
            {
                try
                {
                    switch (backendEvent.Event)
                    {
                        case "progress":
                            Interlocked.Increment(ref progressCount);
                            break;
                        case "log":
                            Interlocked.Increment(ref logCount);
                            string logMessage =
                                backendEvent.Payload.GetProperty("message").GetString() ?? "";
                            if (logMessage.Contains('�'))
                            {
                                Interlocked.Increment(ref malformedUnicodeCount);
                            }
                            if (logMessage.Contains("历史"))
                            {
                                Interlocked.Increment(ref unicodeLogCount);
                            }
                            break;
                        case "prompt":
                            Interlocked.Increment(ref promptCount);
                            await client.RespondToPromptAsync(
                                backendEvent.OperationId,
                                backendEvent.Payload.GetProperty("promptId").GetString()!,
                                new { choice = "cancel" });
                            break;
                        case "result":
                            terminal.TrySetResult(backendEvent.Payload.Clone());
                            break;
                        case "error":
                            terminal.TrySetException(new InvalidOperationException(
                                backendEvent.Payload.GetProperty("message").GetString()));
                            break;
                    }
                }
                catch (Exception exception)
                {
                    terminal.TrySetException(exception);
                }
            }

            client.EventReceived += OnBackendEvent;
            OperationAccepted accepted = await client.StartCalibrationAsync(request);
            JsonElement result = await terminal.Task.WaitAsync(TimeSpan.FromSeconds(30));
            client.EventReceived -= OnBackendEvent;
            bool completed = result.GetProperty("status").GetString() == "completed";
            bool historyOnly = result.GetProperty("value").GetProperty("historyOnly").GetBoolean();
            bool unicodeLogs = unicodeLogCount > 0 && malformedUnicodeCount == 0;
            bool passed = completed && historyOnly && progressCount > 0 && logCount > 0 &&
                promptCount == 0 && unicodeLogs;
            await File.WriteAllTextAsync(
                resultPath,
                $"{(passed ? "OK" : "FAIL")} operation={accepted.OperationId} " +
                $"display={display.Name} gray={gray.Id} color={color.Id} " +
                $"progress={progressCount} logs={logCount} prompts={promptCount} " +
                $"unicodeLogs={unicodeLogs} completed={completed} historyOnly={historyOnly}");
            Environment.Exit(passed ? 0 : 2);
        }
        catch (Exception exception)
        {
            await File.WriteAllTextAsync(resultPath, exception.ToString());
            Environment.Exit(1);
        }
    }
}
