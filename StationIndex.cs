namespace ReturnRadius;

public sealed record Station(string Id, double Latitude, double Longitude, int FreeDocks);
public sealed record Query(double Latitude, double Longitude, int Needed, int Limit, double? MaxMeters = null);
public sealed record Match(string Id, int FreeDocks, double Meters);
public sealed record Result(IReadOnlyList<Match> Matches, int Visited);

internal readonly record struct Point(double X, double Y, double Z)
{
    public static Point FromDegrees(double latitude, double longitude)
    {
        if (!double.IsFinite(latitude) || Math.Abs(latitude) > 90 ||
            !double.IsFinite(longitude) || Math.Abs(longitude) > 180)
            throw new ArgumentException("Latitude or longitude outside its valid range.");
        if (Math.Abs(latitude) == 90) return new Point(0, 0, Math.Sign(latitude));
        double phi = latitude * (Math.PI / 180);
        double lambda = (longitude == 180 ? -180 : longitude) * (Math.PI / 180);
        return new Point(Math.Cos(phi) * Math.Cos(lambda), Math.Cos(phi) * Math.Sin(lambda), Math.Sin(phi));
    }

    public double Axis(int axis) => axis switch { 0 => X, 1 => Y, _ => Z };
    public double AngleTo(Point p)
    {
        double cx = Y * p.Z - Z * p.Y;
        double cy = Z * p.X - X * p.Z;
        double cz = X * p.Y - Y * p.X;
        return Math.Atan2(Math.Sqrt(cx * cx + cy * cy + cz * cz), X * p.X + Y * p.Y + Z * p.Z);
    }
}

public sealed class StationIndex
{
    private sealed record Entry(Station Station, Point Point);
    private sealed record Node(Entry Entry, int Axis, Node? Left, Node? Right, int MaxFree);
    private sealed record Candidate(Entry Entry, double Angle);
    private const double EarthRadiusMeters = 6371000;
    // On unit coordinates, allow a few ulps for conversion and subtraction at a split plane.
    private const double PlaneTolerance = 2e-15;
    private readonly Node? root;

    public StationIndex(IEnumerable<Station> stations)
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        var entries = stations.Select(s =>
        {
            if (s is null || string.IsNullOrWhiteSpace(s.Id) || !names.Add(s.Id) || s.FreeDocks < 0)
                throw new ArgumentException("Expected unique station IDs and nonnegative dock counts.");
            return new Entry(s, Point.FromDegrees(s.Latitude, s.Longitude));
        }).ToArray();
        root = Build(entries, 0, entries.Length, 0);
    }

    private static Node? Build(Entry[] entries, int start, int length, int axis)
    {
        if (length == 0) return null;
        Array.Sort(entries, start, length, Comparer<Entry>.Create((a, b) =>
        {
            int order = a.Point.Axis(axis).CompareTo(b.Point.Axis(axis));
            return order != 0 ? order : string.CompareOrdinal(a.Station.Id, b.Station.Id);
        }));
        int half = length / 2, mid = start + half, next = (axis + 1) % 3;
        var left = Build(entries, start, half, next);
        var right = Build(entries, mid + 1, length - half - 1, next);
        int maxFree = Math.Max(entries[mid].Station.FreeDocks, Math.Max(left?.MaxFree ?? 0, right?.MaxFree ?? 0));
        return new Node(entries[mid], axis, left, right, maxFree);
    }

    public Result Nearest(Query query)
    {
        if (query.Needed < 1 || query.Limit < 1) throw new ArgumentException("Needed and limit must be positive.");
        if (query.MaxMeters is double radius && (!double.IsFinite(radius) || radius < 0))
            throw new ArgumentException("MaxMeters must be finite and nonnegative.");
        var origin = Point.FromDegrees(query.Latitude, query.Longitude);
        double maxMeters = query.MaxMeters ?? double.PositiveInfinity;
        double maxChord = 2 * Math.Sin(Math.Min(Math.PI, maxMeters / EarthRadiusMeters) / 2);
        var best = new SortedSet<Candidate>(Comparer<Candidate>.Create((a, b) =>
        {
            int order = a.Angle.CompareTo(b.Angle);
            return order != 0 ? order : string.CompareOrdinal(a.Entry.Station.Id, b.Entry.Station.Id);
        }));
        int visited = 0;
        void Search(Node? node)
        {
            if (node == null || node.MaxFree < query.Needed) return;
            visited++;
            var entry = node.Entry;
            if (entry.Station.FreeDocks >= query.Needed)
            {
                double angle = origin.AngleTo(entry.Point);
                if (angle * EarthRadiusMeters <= maxMeters)
                {
                    best.Add(new Candidate(entry, angle));
                    if (best.Count > query.Limit) best.Remove(best.Max!);
                }
            }
            double offset = origin.Axis(node.Axis) - entry.Point.Axis(node.Axis);
            Search(offset < 0 ? node.Left : node.Right);
            double chordLimit = best.Count < query.Limit ? maxChord : 2 * Math.Sin(best.Max!.Angle / 2);
            if (Math.Abs(offset) <= chordLimit + PlaneTolerance)
                Search(offset < 0 ? node.Right : node.Left);
        }
        Search(root);
        var matches = best.Select(c => new Match(c.Entry.Station.Id, c.Entry.Station.FreeDocks,
            EarthRadiusMeters * c.Angle)).ToArray();
        return new Result(matches, visited);
    }
}
