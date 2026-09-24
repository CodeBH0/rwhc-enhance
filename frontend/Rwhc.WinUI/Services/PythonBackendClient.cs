using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Rwhc.WinUI.Services;

public sealed class PythonBackendClient : IAsyncDisposable
{
    private const int ProtocolVersion = 1;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = false,
    };

    private readonly Process _process;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<string, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly CancellationTokenSource _lifetime = new();
    private readonly Task _stdoutPump;
    private readonly Task _stderrPump;
    private readonly ConcurrentQueue<string> _recentStderr = new();
    private long _nextRequestId;
    private bool _disposed;

    private PythonBackendClient(Process process)
    {
        _process = process;
        _stdoutPump = PumpStandardOutputAsync(process, _lifetime.Token);
        _stderrPump = PumpStandardErrorAsync(process, _lifetime.Token);
    }

    public event EventHandler<BackendEvent>? EventReceived;

    public static PythonBackendClient CreateForDevelopment()
        => Create(replayTestHardware: false);

    public static PythonBackendClient CreateForHistoryReplayTest()
        => Create(replayTestHardware: true);

    private static PythonBackendClient Create(bool replayTestHardware)
    {
        string root = LocateBackendRoot();
        string python = LocatePython(root);
        var utf8WithoutBom = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = utf8WithoutBom,
            StandardOutputEncoding = utf8WithoutBom,
            StandardErrorEncoding = utf8WithoutBom,
        };
        // ProcessStartInfo's stream encodings configure the .NET readers and
        // writers, but do not force Python's own redirected stdio encoding.
        // Without these variables a Chinese Windows code page can emit GBK
        // bytes that the NDJSON client then correctly (but incompatibly)
        // decodes as UTF-8.
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
        startInfo.Environment["PYTHONUTF8"] = "1";
        startInfo.ArgumentList.Add(Path.Combine(root, "backend_host.py"));
        startInfo.ArgumentList.Add("--stdio");
        startInfo.ArgumentList.Add("--base-dir");
        startInfo.ArgumentList.Add(root);
        if (replayTestHardware)
        {
            startInfo.ArgumentList.Add("--replay-test-hardware");
        }

        Process process;
        try
        {
            process = Process.Start(startInfo)
                ?? throw new InvalidOperationException("无法创建 Python backend 进程。");
        }
        catch (Exception exception)
        {
            throw new InvalidOperationException(
                $"无法启动 Python backend。请确认已安装 Python 依赖，或设置 RWHC_PYTHON。" +
                $"{Environment.NewLine}解释器：{python}{Environment.NewLine}{exception.Message}",
                exception);
        }
        return new PythonBackendClient(process);
    }

    public Task<BackendDescription> DescribeAsync(CancellationToken cancellationToken = default) =>
        CallAsync<BackendDescription>("backend.describe", new { }, cancellationToken);

    public Task<DisplayList> ListDisplaysAsync(CancellationToken cancellationToken = default) =>
        CallAsync<DisplayList>("display.list", new { }, cancellationToken);

    public Task<PaperWhiteInfo> GetPaperWhiteAsync(
        string monitorId,
        CancellationToken cancellationToken = default) =>
        CallAsync<PaperWhiteInfo>(
            "display.getPaperWhite",
            new { monitorId },
            cancellationToken);

    public Task<InstrumentOptions> ListInstrumentOptionsAsync(
        CancellationToken cancellationToken = default) =>
        CallAsync<InstrumentOptions>("instrument.listOptions", new { }, cancellationToken);

    public Task<HistoryList> ListHistoryAsync(
        CancellationToken cancellationToken = default) =>
        CallAsync<HistoryList>("history.list", new { }, cancellationToken);

    public Task<RequestValidationResult> ValidateCalibrationRequestAsync(
        CalibrationRequestWire request,
        CancellationToken cancellationToken = default) =>
        CallAsync<RequestValidationResult>(
            "calibration.validateRequest",
            new { request },
            cancellationToken);

    public Task<OperationAccepted> StartCalibrationAsync(
        CalibrationRequestWire request,
        CancellationToken cancellationToken = default) =>
        CallAsync<OperationAccepted>(
            "calibration.start",
            new { request },
            cancellationToken);

    public Task<OperationAccepted> StartEventProbeAsync(
        int steps = 3,
        int delayMs = 10,
        bool requirePrompt = false,
        bool fail = false,
        CancellationToken cancellationToken = default) =>
        CallAsync<OperationAccepted>(
            "diagnostics.startEventProbe",
            new { steps, delayMs, requirePrompt, fail },
            cancellationToken);

    public Task<CancellationResult> CancelOperationAsync(
        string operationId,
        CancellationToken cancellationToken = default) =>
        CallAsync<CancellationResult>(
            "operation.cancel",
            new { operationId },
            cancellationToken);

    public Task<PromptResponseResult> RespondToPromptAsync(
        string operationId,
        string promptId,
        object response,
        CancellationToken cancellationToken = default) =>
        CallAsync<PromptResponseResult>(
            "prompt.respond",
            new { operationId, promptId, response },
            cancellationToken);

    private async Task<T> CallAsync<T>(
        string method,
        object parameters,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_process.HasExited)
        {
            throw new InvalidOperationException(
                $"Python backend 已退出，exit code {_process.ExitCode}。");
        }

        string id = Interlocked.Increment(ref _nextRequestId).ToString();
        var completion = new TaskCompletionSource<JsonElement>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        if (!_pending.TryAdd(id, completion))
        {
            throw new InvalidOperationException($"重复的 IPC request id: {id}");
        }

        try
        {
            string request = JsonSerializer.Serialize(new
            {
                protocol = ProtocolVersion,
                type = "request",
                id,
                method,
                @params = parameters,
            }, JsonOptions);
            await _writeLock.WaitAsync(cancellationToken);
            try
            {
                await _process.StandardInput.WriteLineAsync(request.AsMemory(), cancellationToken);
                await _process.StandardInput.FlushAsync(cancellationToken);
            }
            finally
            {
                _writeLock.Release();
            }

            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(15));
            JsonElement root = await completion.Task.WaitAsync(timeout.Token);
            if (!root.GetProperty("ok").GetBoolean())
            {
                JsonElement error = root.GetProperty("error");
                JsonElement? details = error.TryGetProperty("details", out JsonElement value)
                    ? value.Clone()
                    : null;
                throw new BackendProtocolException(
                    error.GetProperty("code").GetString() ?? "unknown_error",
                    error.GetProperty("message").GetString() ?? "Unknown backend error",
                    details);
            }

            return root.GetProperty("result").Deserialize<T>(JsonOptions)
                ?? throw new InvalidOperationException("Python backend 返回了空结果。");
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    private async Task PumpStandardOutputAsync(
        Process process,
        CancellationToken cancellationToken)
    {
        Exception? terminalError = null;
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                string? line = await process.StandardOutput.ReadLineAsync(cancellationToken);
                if (line is null)
                {
                    if (!cancellationToken.IsCancellationRequested)
                    {
                        terminalError = new InvalidOperationException(
                            "Python backend 启动后意外退出。请检查 Python 依赖与项目路径。" +
                            FormatStderrTail());
                    }
                    break;
                }

                using JsonDocument document = JsonDocument.Parse(line);
                JsonElement root = document.RootElement;
                if (root.GetProperty("protocol").GetInt32() != ProtocolVersion)
                {
                    throw new InvalidOperationException("Python backend 返回了不兼容的协议版本。");
                }

                string type = root.GetProperty("type").GetString() ?? "";
                if (type == "event")
                {
                    BackendEvent backendEvent = root.Deserialize<BackendEvent>(JsonOptions)
                        ?? throw new InvalidOperationException("无法解析 backend event。");
                    EventReceived?.Invoke(this, backendEvent);
                    continue;
                }
                if (type != "response")
                {
                    throw new InvalidOperationException($"未知 backend frame type: {type}");
                }

                JsonElement idElement = root.GetProperty("id");
                string? id = idElement.ValueKind == JsonValueKind.String
                    ? idElement.GetString()
                    : idElement.GetRawText();
                if (id is not null && _pending.TryGetValue(id, out var completion))
                {
                    completion.TrySetResult(root.Clone());
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            terminalError = exception;
        }
        finally
        {
            if (terminalError is not null)
            {
                foreach (TaskCompletionSource<JsonElement> completion in _pending.Values)
                {
                    completion.TrySetException(terminalError);
                }
            }
        }
    }

    private static string LocateBackendRoot()
    {
        string? configured = Environment.GetEnvironmentVariable("RWHC_BACKEND_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            string fullPath = Path.GetFullPath(configured);
            if (File.Exists(Path.Combine(fullPath, "backend_host.py")))
            {
                return fullPath;
            }
            throw new DirectoryNotFoundException(
                $"RWHC_BACKEND_ROOT 不包含 backend_host.py: {fullPath}");
        }

        foreach (string start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            var current = new DirectoryInfo(start);
            while (current is not null)
            {
                if (File.Exists(Path.Combine(current.FullName, "backend_host.py")))
                {
                    return current.FullName;
                }
                current = current.Parent;
            }
        }
        throw new DirectoryNotFoundException(
            "找不到 backend_host.py；请设置 RWHC_BACKEND_ROOT。");
    }

    private static string LocatePython(string root)
    {
        string? configured = Environment.GetEnvironmentVariable("RWHC_PYTHON");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return configured;
        }

        string projectPython = Path.Combine(root, ".venv314", "Scripts", "python.exe");
        return File.Exists(projectPython) ? projectPython : "python.exe";
    }

    private async Task PumpStandardErrorAsync(
        Process process,
        CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                string? line = await process.StandardError.ReadLineAsync(cancellationToken);
                if (line is null)
                {
                    return;
                }
                _recentStderr.Enqueue(line);
                while (_recentStderr.Count > 20)
                {
                    _recentStderr.TryDequeue(out _);
                }
                Debug.WriteLine($"[python] {line}");
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    private string FormatStderrTail()
    {
        string[] lines = _recentStderr.ToArray();
        return lines.Length == 0
            ? ""
            : $"{Environment.NewLine}Python 错误：{string.Join(Environment.NewLine, lines)}";
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        try
        {
            _process.StandardInput.Close();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            await _process.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException)
        {
            if (!_process.HasExited)
            {
                _process.Kill(entireProcessTree: true);
                await _process.WaitForExitAsync();
            }
        }
        catch (Exception) when (_process.HasExited)
        {
            // A failed backend can close stdin before the UI disposes the client.
        }
        finally
        {
            _lifetime.Cancel();
            try
            {
                await Task.WhenAll(_stdoutPump, _stderrPump);
            }
            catch (OperationCanceledException)
            {
            }
            var disposed = new ObjectDisposedException(nameof(PythonBackendClient));
            foreach (TaskCompletionSource<JsonElement> completion in _pending.Values)
            {
                completion.TrySetException(disposed);
            }
            _pending.Clear();
            _process.Dispose();
            _writeLock.Dispose();
            _lifetime.Dispose();
        }
    }
}
