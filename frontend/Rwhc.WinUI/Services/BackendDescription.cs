using System.Text.Json.Serialization;

namespace Rwhc.WinUI.Services;

public sealed record BackendDescription(
    [property: JsonPropertyName("protocolVersion")] int ProtocolVersion,
    [property: JsonPropertyName("protocolMinorVersion")] int ProtocolMinorVersion,
    [property: JsonPropertyName("requestSchemaVersions")] int[] RequestSchemaVersions,
    [property: JsonPropertyName("eventTypes")] string[] EventTypes,
    [property: JsonPropertyName("pythonVersion")] string PythonVersion,
    [property: JsonPropertyName("backendType")] string BackendType,
    [property: JsonPropertyName("profileReady")] bool ProfileReady,
    [property: JsonPropertyName("mhc2EntryCount")] int Mhc2EntryCount,
    [property: JsonPropertyName("grayHistoryCount")] int GrayHistoryCount,
    [property: JsonPropertyName("colorHistoryCount")] int ColorHistoryCount,
    [property: JsonPropertyName("supportedClutGrids")] int[] SupportedClutGrids,
    [property: JsonPropertyName("requestFields")] string[] RequestFields,
    [property: JsonPropertyName("capabilities")] string[] Capabilities);
