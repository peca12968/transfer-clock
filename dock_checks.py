"""Exercise the real Python and .NET commands and compare multimodal schedules."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from dock_trip import connect, DEFAULT_DLL
from router import Call, seconds
from test_router import enumerate_journeys


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


class DockTests(unittest.TestCase):
    dll = DEFAULT_DLL

    def cli(self, time, *flags, snapshot=None, destination="Depot", stop_times="night.csv", success=True):
        args = [sys.executable, str(ROOT / "dock_trip.py"), str(EXAMPLES / stop_times),
                str(snapshot or EXAMPLES / "dock-night.json"), destination, time, "--radius-dll", str(self.dll), *flags]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        return result.stderr

    def test_farther_dock_catches_express_and_one_second_later_misses(self):
        report = self.cli("23:54:00", "--max-meters", "180")
        self.assertEqual(report["eligibleDocks"], 2)
        market, ferry = report["journeys"]
        self.assertGreater(market["meters"], ferry["meters"])
        self.assertEqual((market["dock"], market["atDock"], market["readyAtStop"], market["arrival"]),
                         ("market", "23:57:00", "24:05:00", "24:12:00"))
        self.assertEqual([leg["trip"] for leg in market["legs"]], ["express"])
        self.assertEqual([leg["trip"] for leg in ferry["legs"]], ["feeder", "shuttle"])
        self.assertEqual(seconds(ferry["arrival"]) - seconds(market["arrival"]), 660)
        missed = self.cli("23:54:01", "--max-meters", "180")
        self.assertEqual(missed["eligibleDocks"], 2)
        self.assertEqual([(j["dock"], j["readyAtStop"], j["arrival"]) for j in missed["journeys"]],
                         [("market", "24:05:01", "24:23:00")])

    def test_deadline_deducts_ride_and_walk_and_honors_availability(self):
        for deadline, leave, arrive in [("24:23:00", "23:58:00", "24:23:00"),
                                        ("24:22:59", "23:54:00", "24:12:00")]:
            report = self.cli(deadline, "--arrive-by", "--max-meters", "180", "--needed", "2")
            self.assertEqual(report["eligibleDocks"], 1)
            journey, = report["journeys"]
            self.assertEqual((journey["dock"], journey["departOrigin"], journey["arrival"]), ("market", leave, arrive))
            self.assertEqual(seconds(journey["readyAtStop"]) - seconds(leave), 660)
            self.assertEqual(journey["readyAtStop"], journey["legs"][0]["departure"])
        self.assertEqual(self.cli("24:23:00", "--arrive-by", "--max-meters", "100", "--needed", "2"),
                         {"eligibleDocks": 0, "journeys": []})
        near = self.cli("24:23:00", "--arrive-by", "--max-meters", "100", "--max-transfers", "0")
        self.assertEqual(near, {"eligibleDocks": 1, "journeys": []})

    def test_disruptions_reschedule_bike_connections_and_deadlines(self):
        args = ["--max-meters", "180", "--service-updates", str(EXAMPLES / "night-disruption.json")]
        forward = self.cli("23:54:01", *args)
        self.assertEqual(forward["eligibleDocks"], 2)
        market, = forward["journeys"]
        self.assertEqual((market["dock"], market["readyAtStop"], market["arrival"]),
                         ("market", "24:05:01", "24:15:00"))
        self.assertEqual([leg["trip"] for leg in market["legs"]], ["express"])
        reverse, = self.cli("24:15:00", *args, "--arrive-by", "--needed", "2")["journeys"]
        self.assertEqual((reverse["departOrigin"], reverse["readyAtStop"], reverse["arrival"]),
                         ("23:57:00", "24:08:00", "24:15:00"))

    def test_same_stop_can_finish_on_foot_and_cannot_leave_before_day_start(self):
        args = ["--arrive-by", "--max-meters", "180", "--needed", "2"]
        arrived = self.cli("00:11:00", *args, destination="Market")["journeys"]
        self.assertEqual(len(arrived), 1)
        self.assertEqual((arrived[0]["departOrigin"], arrived[0]["readyAtStop"], arrived[0]["legs"]),
                         ("00:00:00", "00:11:00", []))
        self.assertEqual(self.cli("00:10:59", *args, destination="Market")["journeys"], [])

    def test_bike_return_and_platform_transfer_share_deadline(self):
        flags = ("--max-meters", "100", "--transfer-rules", str(EXAMPLES / "platform-transfer.json"))
        forward = self.cli("23:47:00", *flags, stop_times="platform-night.csv")["journeys"][0]
        reverse, = self.cli("24:12:00", *flags, "--arrive-by", stop_times="platform-night.csv")["journeys"]
        self.assertEqual(forward, reverse)
        self.assertEqual((reverse["dock"], reverse["departOrigin"], reverse["readyAtStop"], reverse["arrival"]),
                         ("ferry", "23:47:00", "23:50:00", "24:12:00"))
        self.assertEqual([(row["from_stop"], row["to_stop"], row["slack_seconds"]) for row in reverse["connections"]],
                         [("Market", "MarketEast", 0)])

    def test_invalid_access_even_if_dock_is_unavailable(self):
        data = json.loads((EXAMPLES / "dock-night.json").read_text(encoding="utf-8"))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            for edit, reason in [({"rideSeconds": True}, "nonnegative integer"), ({"stop": "unknown"}, "unknown timetable"),
                                 ({"id": "ferry"}, "unique"), ({"latitude": 94}, "station search failed")]:
                with self.subTest(edit=edit):
                    bad = {**data, "docks": [{**data["docks"][0], **edit}, *data["docks"][1:]]}
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    self.assertIn(reason, self.cli("23:54:00", "--max-meters", "180", snapshot=path, success=False))

    def test_random_access_times_against_forward_enumeration(self):
        rng = random.Random(971)
        for case in range(120):
            trips = {}
            for index in range(6):
                stops = rng.sample(["A", "B", "C", "D"], 4)
                t = rng.randrange(5, 25)
                calls = []
                for seq, stop in enumerate(stops):
                    t += rng.randrange(1, 5)
                    calls.append(Call(stop, t, t, seq, True, True))
                trips[str(index)] = calls
            docks = [(dict(id=f"d{i}", stop=stop, freeDocks=2, rideSeconds=rng.randrange(6),
                           returnAndWalkSeconds=rng.randrange(6)), float(i + 1))
                     for i, stop in enumerate(("A", "A", "C"))]
            gap, limit = rng.randrange(4), rng.randrange(3)
            for back, instant in [(False, rng.randrange(12)), (True, rng.randrange(20, 45))]:
                expected = []
                for dock, distance in docks:
                    access = dock["rideSeconds"] + dock["returnAndWalkSeconds"]
                    query_time = instant if back else instant + access
                    frontier = enumerate_journeys(trips, dock["stop"], "D", query_time, gap, limit, arrive_by=back)
                    for transfers, score in frontier:
                        if not back or score >= access:
                            expected.append((access - score if back else score, transfers, distance, dock["id"]))
                actual = connect(trips, docks, "D", instant, gap, limit, back)
                keys = [(-seconds(j["departOrigin"]) if back else seconds(j["arrival"]), j["transfers"],
                         j["meters"], j["dock"]) for j in actual]
                self.assertEqual(keys, sorted(expected), (case, back, instant))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radius-dll", type=Path, default=DEFAULT_DLL)
    DockTests.dll = parser.parse_args().radius_dll.resolve()
    unittest.main(argv=[sys.argv[0]], verbosity=2)
