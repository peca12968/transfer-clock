namespace ReturnRadius;

public sealed record OfflineMap(int MaxZoom, long TileBudget);
public sealed record TileWindow(int Zoom, long FirstColumn, long Columns, long FirstRow, long Rows)
{
    public long TileCount => Columns * Rows;

    internal static TileWindow Fit(Query query, IEnumerable<Station> matches, OfflineMap options)
    {
        if (options.MaxZoom is < 0 or > 31 || options.TileBudget < 1)
            throw new ArgumentException("offlineMap requires maxZoom between 0 and 31 and a positive tileBudget.");
        var points = matches.Select(s => Project(s.Latitude, s.Longitude))
            .Append(Project(query.Latitude, query.Longitude)).ToArray();
        for (int zoom = options.MaxZoom; zoom >= 0; zoom--)
        {
            long side = 1L << zoom;
            long Index(double coordinate) => Math.Clamp((long)Math.Floor(coordinate * side), 0, side - 1);
            var columns = points.Select(p => Index(p.X)).Distinct().Order().ToArray();
            long first = columns[0], largestGap = columns[0] + side - columns[^1];
            for (int i = 1; i < columns.Length; i++)
            {
                long gap = columns[i] - columns[i - 1];
                if (gap > largestGap)
                {
                    largestGap = gap;
                    first = columns[i];
                }
            }
            long north = points.Min(p => Index(p.Y)), south = points.Max(p => Index(p.Y));
            var window = new TileWindow(zoom, first, side - largestGap + 1, north, south - north + 1);
            if (window.TileCount <= options.TileBudget) return window;
        }
        throw new InvalidOperationException("Zoom zero always fits a positive budget.");
    }

    private static (double X, double Y) Project(double latitude, double longitude)
    {
        double radians = Math.Clamp(latitude, -85.0511287798066, 85.0511287798066) * Math.PI / 180;
        double y = 0.5 - Math.Asinh(Math.Tan(radians)) / (2 * Math.PI);
        return (longitude == 180 ? 0 : (longitude + 180) / 360, y);
    }
}
