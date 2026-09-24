using System.Text.Json;
using System.Text.Json.Serialization;

namespace Rwhc.WinUI.Services;

public sealed record DisplayBounds(
    [property: JsonPropertyName("left")] int Left,
    [property: JsonPropertyName("top")] int Top,
    [property: JsonPropertyName("right")] int Right,
    [property: JsonPropertyName("bottom")] int Bottom);

public sealed record DisplayInfo(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("pnpDeviceId")] string PnpDeviceId,
    [property: JsonPropertyName("gdiName")] string GdiName,
    [property: JsonPropertyName("colorMode")] string ColorMode,
    [property: JsonPropertyName("isHdr")] bool IsHdr,
    [property: JsonPropertyName("sdrWhiteNits")] double SdrWhiteNits,
    [property: JsonPropertyName("bounds")] DisplayBounds Bounds)
{
    public override string ToString() => $"{Name} · {ColorMode.ToUpperInvariant()}";
}

public sealed record DisplayList(
    [property: JsonPropertyName("displays")] DisplayInfo[] Displays);

public sealed record PaperWhiteInfo(
    [property: JsonPropertyName("monitorId")] string MonitorId,
    [property: JsonPropertyName("nits")] double Nits,
    [property: JsonPropertyName("source")] string Source);

public sealed record InstrumentOption(
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("description")] string Description)
{
    public override string ToString() => Description;
}

public sealed record InstrumentOptions(
    [property: JsonPropertyName("instruments")] InstrumentOption[] Instruments,
    [property: JsonPropertyName("modes")] InstrumentOption[] Modes);

public sealed record EetfArguments(
    [property: JsonPropertyName("sourceMax")] double SourceMax = 10000,
    [property: JsonPropertyName("sourceMin")] double SourceMin = 0,
    [property: JsonPropertyName("monitorMax")] double? MonitorMax = null,
    [property: JsonPropertyName("monitorMin")] double? MonitorMin = null);

public sealed record CalibrationRequestWire(
    [property: JsonPropertyName("schemaVersion")] int SchemaVersion,
    [property: JsonPropertyName("monitorId")] string MonitorId,
    [property: JsonPropertyName("instrumentDescription")] string InstrumentDescription,
    [property: JsonPropertyName("instrumentModeDescription")] string InstrumentModeDescription,
    [property: JsonPropertyName("grayscaleSamples")] int GrayscaleSamples,
    [property: JsonPropertyName("colorSampleSet")] string ColorSampleSet,
    [property: JsonPropertyName("whitePoint")] string WhitePoint,
    [property: JsonPropertyName("brightMode")] bool BrightMode = false,
    [property: JsonPropertyName("eetfEnabled")] bool EetfEnabled = false,
    [property: JsonPropertyName("eetfArgs")] EetfArguments? EetfArgs = null,
    [property: JsonPropertyName("clutEnabled")] bool ClutEnabled = false,
    [property: JsonPropertyName("clutGrid")] int ClutGrid = 33,
    [property: JsonPropertyName("installProfile")] bool InstallProfile = true,
    [property: JsonPropertyName("grayHistoryId")] string? GrayHistoryId = null,
    [property: JsonPropertyName("colorHistoryId")] string? ColorHistoryId = null);

public sealed record RequestValidationResult(
    [property: JsonPropertyName("valid")] bool Valid,
    [property: JsonPropertyName("schemaVersion")] int SchemaVersion,
    [property: JsonPropertyName("normalizedRequest")] CalibrationRequestWire NormalizedRequest,
    [property: JsonPropertyName("historyOnly")] bool HistoryOnly);

public sealed record OperationAccepted(
    [property: JsonPropertyName("operationId")] string OperationId,
    [property: JsonPropertyName("status")] string Status);

public sealed record CancellationResult(
    [property: JsonPropertyName("operationId")] string OperationId,
    [property: JsonPropertyName("cancellationRequested")] bool CancellationRequested);

public sealed record PromptResponseResult(
    [property: JsonPropertyName("operationId")] string OperationId,
    [property: JsonPropertyName("promptId")] string PromptId,
    [property: JsonPropertyName("accepted")] bool Accepted);

public sealed record BackendEvent(
    [property: JsonPropertyName("operationId")] string OperationId,
    [property: JsonPropertyName("sequence")] long Sequence,
    [property: JsonPropertyName("event")] string Event,
    [property: JsonPropertyName("timestamp")] DateTimeOffset Timestamp,
    [property: JsonPropertyName("payload")] JsonElement Payload);

public sealed class BackendProtocolException : Exception
{
    public BackendProtocolException(string code, string message, JsonElement? details = null)
        : base($"{code}: {message}")
    {
        Code = code;
        Details = details;
    }

    public string Code { get; }

    public JsonElement? Details { get; }
}
