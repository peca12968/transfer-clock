"""Run the public CLI and compare indexed results with exhaustive spherical distances."""
import json
import math
from pathlib import Path
from random import Random
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parent
DLL = ROOT / "bin/Release/net10.0/ReturnRadius.dll"
RADIUS = 6_371_000


def station(name, lat, lon, free):
    return dict(id=name, latitude=lat, longitude=lon, freeDocks=free)


def query(lat, lon, needed=1, limit=1):
    return dict(latitude=lat, longitude=lon, needed=needed, limit=limit)


def invoke(data):
    with TemporaryDirectory() as temp:
        path = Path(temp) / "search.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.run(["dotnet", str(DLL), str(path)], capture_output=True, text=True, timeout=30)


def search(stations, queries):
    result = invoke(dict(stations=stations, queries=queries))
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def haversine(q, s):
    a, b = map(math.radians, [q["latitude"], s["latitude"]])
    dl = math.radians(s["longitude"] - q["longitude"])
    h = math.sin((a-b)/2)**2 + math.cos(a)*math.cos(b)*math.sin(dl/2)**2
    return 2 * RADIUS * math.asin(math.sqrt(min(1, h)))


class StationTests(unittest.TestCase):
    def test_riverside_party_and_full_stations(self):
        data = json.loads((ROOT / "examples/riverside.json").read_text(encoding="utf-8"))
        one, pair, four = search(data["stations"], data["queries"])
        self.assertEqual([m["id"] for m in one["matches"]], ["ferry", "market"])
        self.assertEqual([m["id"] for m in pair["matches"]], ["market"])
        self.assertEqual(four, {"matches": [], "visited": 0})

    def test_seeded_global_search_against_every_station(self):
        random = Random(43)
        stations = [station(str(i), random.uniform(-80, 80), random.uniform(-180, 180), random.randrange(9))
                    for i in range(4093)]
        stations.extend([station("WEST", 0, -179.96, 3), station("EAST", 0, 179.7, 5), station("FULL", 0, 179.98, 0)])
        queries = [query(0, 179.99, 2, 2)]
        queries.extend(query(random.uniform(-80, 80), random.uniform(-180, 180), random.randrange(1, 9),
                             random.randrange(1, 8)) for _ in range(63))
        results = search(stations, queries)
        self.assertEqual([m["id"] for m in results[0]["matches"]], ["WEST", "EAST"])
        for q, result in zip(queries, results, strict=True):
            expected = sorted((haversine(q, s), s["id"], s["freeDocks"]) for s in stations
                              if s["freeDocks"] >= q["needed"])[:q["limit"]]
            self.assertEqual(len(result["matches"]), len(expected))
            for match, (meters, name, free) in zip(result["matches"], expected, strict=True):
                self.assertEqual((match["id"], match["freeDocks"]), (name, free))
                self.assertAlmostEqual(match["meters"], meters, delta=1e-5)
        seen = sum(r["visited"] for r in results)
        full = len(stations) * len(queries)
        self.assertLess(seen, full // 10)
        print(f"64 queries verified across 4,096 stations: {seen:,} tree nodes visited; {full:,} exhaustive station checks.")

    def test_antipodal_centimeter_order(self):
        values = [station("A-farthest", 0, 180, 1), station("Z-closer", 0, 179.9999999, 1)]
        matches = search(values, [query(0, 0, limit=2)])[0]["matches"]
        self.assertEqual([m["id"] for m in matches], ["Z-closer", "A-farthest"])
        for match, degrees in zip(matches, [179.9999999, 180], strict=True):
            self.assertAlmostEqual(match["meters"], RADIUS * math.radians(degrees), delta=1e-7)

    def test_distance_limit_includes_exact_boundary_and_intersects_availability(self):
        sites = [station("center", 0, 180, 2), station("across", 0, -179.99, 3), station("closer", 0, 179.995, 0)]
        q = query(0, -180, needed=2, limit=5)
        limit = search(sites, [q])[0]["matches"][-1]["meters"]
        exact, below, zero = search(sites, [dict(q, maxMeters=r) for r in (limit, math.nextafter(limit, 0), 0)])
        self.assertEqual([m["id"] for m in exact["matches"]], ["center", "across"])
        for result in (below, zero):
            self.assertEqual([m["id"] for m in result["matches"]], ["center"])

    def test_ties_at_poles_dateline_and_across_partitions(self):
        stations = [station(f"s-{i:02}", 90, i, 3) for i in reversed(range(23))]
        stations += [station("date-b", 0, -180, 3), station("date-a", 0, 180, 2)]
        for values in (stations, list(reversed(stations))):
            pole, date = search(values, [query(90, -90, limit=3), query(0, -180, limit=2)])
            self.assertEqual([m["id"] for m in pole["matches"]], ["s-00", "s-01", "s-02"])
            self.assertEqual([m["id"] for m in date["matches"]], ["date-a", "date-b"])
            self.assertTrue(all(m["meters"] == 0 for result in (pole, date) for m in result["matches"]))

    def test_empty_and_invalid_batch(self):
        self.assertEqual(search([], [query(0, 0)]), [{"matches": [], "visited": 0}])
        item = station("a", 0, 0, 2)
        for invalid in [
            dict(stations=[item, item], queries=[]), dict(stations=[dict(item, latitude=91)], queries=[]),
            dict(stations=[dict(item, freeDocks=-1)], queries=[]), dict(stations=[None], queries=[]),
            dict(stations=[item], queries=[query(0, 181)]), dict(stations=[item], queries=[query(2, 3, 0)]),
            dict(stations=[item], queries=[query(0, 0), query(0, 0, limit=0)]),
            dict(stations=[item], queries=[{}]), dict(stations=[item], queries=[None]),
            dict(stations=[item], queries=[dict(query(0, 0), maxMeters=-1)]),
        ]:
            with self.subTest(invalid=invalid):
                result = invoke(invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
