import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from router import Call, plan, read_calls, seconds


ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "examples" / "night.csv"


def enumerate_journeys(trips, origin, destination, start, gap, limit):
    # Enumerate board/alight pairs and all their continuations, without pruning states.
    earliest = {}

    def visit(stop, time, rides):
        if rides and stop == destination:
            earliest[rides] = min(earliest.get(rides, time), time)
            return
        if rides > limit:
            return
        ready = time + (gap if rides else 0)
        for calls in trips.values():
            for i, board in enumerate(calls):
                if board.stop == stop and board.pickup and board.departure >= ready:
                    for end in calls[i + 1:]:
                        if end.dropoff:
                            visit(end.stop, end.arrival, rides + 1)

    visit(origin, start, 0)
    return [(rides - 1, t) for rides, t in sorted(earliest.items())
            if all(t < earlier for count, earlier in earliest.items() if count < rides)]


class RouteTest(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "router.py"), *map(str, args)],
                              capture_output=True, text=True)

    def test_midnight_transfer_boundary_and_pickup(self):
        for gap, expected, trip in [(120, "24:12:00", "express"), (121, "24:23:00", "shuttle"),
                                   (240, "24:23:00", "shuttle")]:
            p = self.cli(EXAMPLE, "Studio", "Depot", "23:50:00", "--change-seconds", gap)
            self.assertEqual(p.returncode, 0, p.stderr)
            direct, transfer = json.loads(p.stdout)["journeys"]
            self.assertEqual((direct["arrival"], len(direct["legs"])), ("24:31:00", 1))
            self.assertEqual((transfer["arrival"], transfer["legs"][1]["trip"]), (expected, trip))
        missed = self.cli(EXAMPLE, "Studio", "Depot", "23:50:01")
        self.assertEqual(json.loads(missed.stdout)["journeys"], [])

    def test_random_frontiers_and_feasibility_of_every_returned_leg(self):
        rng = random.Random(464)
        for case in range(300):
            trips = {}
            for number in range(6):
                calls = []
                t = rng.randrange(10)
                for sequence in range(4):
                    arrive = t + rng.randrange(4)
                    t = arrive + rng.randrange(3)
                    calls.append(Call(rng.choice("ABCD"), arrive, t, sequence, rng.random() < .85,
                                      rng.random() < .85))
                trips[str(number)] = calls
            stops = sorted({c.stop for calls in trips.values() for c in calls})
            origin, dest = rng.sample(stops, 2)
            start, gap, limit = rng.randrange(6), rng.randrange(4), rng.randrange(4)
            with self.subTest(case=case):
                journeys = plan(trips, origin, dest, start, gap, limit)
                self.assertEqual([(j["transfers"], seconds(j["arrival"])) for j in journeys],
                                 enumerate_journeys(trips, origin, dest, start, gap, limit))
                for journey in journeys:
                    stop, ready = origin, start
                    self.assertEqual(journey["transfers"], len(journey["legs"]) - 1)
                    for leg in journey["legs"]:
                        board = trips[leg["trip"]][leg["board_sequence"]]
                        exit_call = trips[leg["trip"]][leg["exit_sequence"]]
                        self.assertLess(board.sequence, exit_call.sequence)
                        self.assertTrue(board.pickup and exit_call.dropoff)
                        self.assertEqual((stop, leg["origin"]), (board.stop, board.stop))
                        self.assertEqual(exit_call.stop, leg["destination"])
                        self.assertEqual(seconds(leg["departure"]), board.departure)
                        self.assertEqual(seconds(leg["arrival"]), exit_call.arrival)
                        self.assertGreaterEqual(board.departure, ready)
                        stop, ready = exit_call.stop, exit_call.arrival + gap
                    self.assertEqual((stop, ready - gap), (dest, seconds(journey["arrival"])))

    def test_repeated_stop_shuffled_rows_and_zero_time_transfer(self):
        header = "trip_id,stop_id,stop_sequence,arrival_time,departure_time,pickup_type,drop_off_type\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.csv"
            path.write_text(header + "y,A,2,8:00:00,8:00:00,0,0\ny,A,1,8:00:00,8:00:00,1,0\n"
                            "y,B,3,8:00:00,8:00:00,0,0\nx,C,2,8:00:00,8:00:00,0,0\n"
                            "x,B,1,8:00:00,8:00:00,0,0\n", encoding="utf-8-sig")
            trips = read_calls(path)
            journey, = plan(trips, "A", "C", seconds("8:00:00"), 0, 1)
            self.assertEqual([(l["trip"], l["board_sequence"]) for l in journey["legs"]],
                             [("y", 2), ("x", 1)])
            self.assertEqual(plan(trips, "A", "C", seconds("8:00:00"), 0, 0), [])
            self.assertEqual(plan(trips, "A", "A", 0, 0, 0)[0]["legs"], [])

    def test_invalid_inputs(self):
        original = EXAMPLE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.csv"
            for old, new in [("stop_id", "name"), ("23:50:00", "23:60:00"),
                             ("Market,20", "Market,10"), ("24:03:00", "23:20:00"),
                             ("0,1\n", "3,1\n")]:
                path.write_text(original.replace(old, new), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_calls(path)
        for args in [("X", "Depot", "23:00:00"), ("Studio", "Depot", "23:49:00", "--change-seconds", -1)]:
            response = self.cli(EXAMPLE, *args)
            self.assertEqual(response.returncode, 1)
            self.assertEqual(response.stdout, "")


if __name__ == "__main__":
    unittest.main()
