using System.Text.Json;
using System.Text.Json.Serialization;
using ReturnRadius;

var json = new JsonSerializerOptions
{
    PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    RespectRequiredConstructorParameters = true,
    UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
};
try
{
    if (args.Length != 1) throw new ArgumentException("Usage: ReturnRadius INPUT.json");
    using var input = File.OpenRead(args[0]);
    var data = JsonSerializer.Deserialize<Batch>(input, json);
    if (data?.Stations is null || data.Queries is null || data.Queries.Any(q => q is null))
        throw new ArgumentException("Expected stations and queries arrays.");
    var index = new StationIndex(data.Stations);
    Console.WriteLine(JsonSerializer.Serialize(data.Queries.Select(index.Nearest).ToArray(), json));
    return 0;
}
catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or JsonException)
{
    Console.Error.WriteLine(error.Message);
    return 1;
}

internal sealed record Batch(Station[] Stations, Query[] Queries);
