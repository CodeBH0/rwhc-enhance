using Microsoft.UI.Xaml;
using Rwhc.WinUI.Services;

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
}
