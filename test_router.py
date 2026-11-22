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


def enumerate_journeys(trips, origin, destination, start, gap, limit, *, arrive_by=False):
    # Even for an arrival deadline, enumerate every journey in the forward direction.
    best = {}
    choose = max if arrive_by else min

    def visit(stop, time, rides, first):
        if rides and stop == destination:
            if not arrive_by or time <= start:
                score = first if arrive_by else time
                best[rides] = choose(best.get(rides, score), score)
            return
        if rides > limit:
            return
        ready = time + (gap if rides else 0)
        for calls in trips.values():
            for i, board in enumerate(calls):
                if board.stop == stop and board.pickup and board.departure >= ready:
                    for end in calls[i + 1:]:
                        if end.dropoff:
                            visit(end.stop, end.arrival, rides + 1, first if rides else board.departure)

    visit(origin, 0 if arrive_by else start, 0, None)
    frontier = []
    for rides, score in sorted(best.items()):
        if not frontier or (score != frontier[-1][1] and choose(score, frontier[-1][1]) == score):
            frontier.append((rides - 1, score))
    return frontier


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
        missed = self.cli(EXAMPLE, "Studio", "Depot", "23:57:01")
        self.assertEqual(json.loads(missed.stdout)["journeys"], [])

    def test_arrival_deadline_and_transfer_margin_to_the_second(self):
        for deadline, gap, expected in [
            ("24:31:05", 120, [(0, "23:50:00", "24:31:00"), (1, "23:57:00", "24:23:00")]),
            ("24:23:00", 120, [(1, "23:57:00", "24:23:00")]),
            ("24:22:59", 120, [(1, "23:50:00", "24:12:00")]),
            ("24:12:00", 120, [(1, "23:50:00", "24:12:00")]),
            ("24:12:00", 121, []), ("24:11:59", 120, []),
        ]:
            with self.subTest(deadline=deadline, gap=gap):
                command = self.cli(EXAMPLE, "Studio", "Depot", deadline, "--arrive-by", "--change-seconds", gap)
                self.assertEqual(command.returncode, 0, command.stderr)
                journeys = json.loads(command.stdout)["journeys"]
                self.assertEqual([(j["transfers"], j["departure"], j["arrival"]) for j in journeys], expected)

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
            deadline = (case * 7) % 30
            for backwards, query in [(False, start), (True, deadline)]:
                with self.subTest(case=case, arrive_by=backwards):
                    journeys = plan(trips, origin, dest, query, gap, limit, arrive_by=backwards)
                    score = "departure" if backwards else "arrival"
                    self.assertEqual([(j["transfers"], seconds(j[score])) for j in journeys],
                                     enumerate_journeys(trips, origin, dest, query, gap, limit, arrive_by=backwards))
                    for journey in journeys:
                        stop, ready = origin, 0 if backwards else start
                        if backwards:
                            self.assertEqual(journey["departure"], journey["legs"][0]["departure"])
                            self.assertLessEqual(seconds(journey["arrival"]), deadline)
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
            reverse, = plan(trips, "A", "C", seconds("8:00:00"), 0, 1, arrive_by=True)
            self.assertEqual(reverse["legs"], journey["legs"])
            self.assertEqual(plan(trips, "A", "C", seconds("8:00:00"), 0, 0, arrive_by=True), [])
            self.assertEqual(plan(trips, "A", "A", 10, 30, 0, arrive_by=True),
                             [{"arrival": "00:00:10", "departure": "00:00:10", "transfers": 0, "legs": []}])

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
