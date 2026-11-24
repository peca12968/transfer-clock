import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from router import Call, plan, read_calls, seconds, timetable


ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "examples" / "night.csv"
DISRUPTION = ROOT / "examples" / "night-disruption.json"


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

    def test_extra_buffer_preserves_stated_same_stop_minimum(self):
        result = self.cli(EXAMPLE, "Studio", "Depot", "23:50:00", "--transfer-buffer-seconds", 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        direct, fallback = json.loads(result.stdout)["journeys"]
        self.assertEqual(direct["connections"], [])
        self.assertEqual((fallback["legs"][-1]["trip"], fallback["arrival"]), ("shuttle", "24:23:00"))
        link, = fallback["connections"]
        self.assertEqual((link["minimum_seconds"], link["buffer_seconds"]), (120, 1))
        self.assertEqual(seconds(link["departure"]) - seconds(link["ready"]), link["slack_seconds"])
        rejected = self.cli(EXAMPLE, "Studio", "Depot", "24:12:00", "--arrive-by", "--transfer-buffer-seconds", 1)
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertEqual(json.loads(rejected.stdout)["journeys"], [])

    def test_service_updates_replan_in_both_directions(self):
        args = ["--service-updates", DISRUPTION]
        forward = self.cli(EXAMPLE, "Studio", "Depot", "23:49:00", *args)
        self.assertEqual(forward.returncode, 0, forward.stderr)
        direct, transfer = json.loads(forward.stdout)["journeys"]
        self.assertEqual(direct["arrival"], "24:33:00")
        self.assertEqual(transfer["arrival"], "24:15:00")
        self.assertEqual([(leg["trip"], leg["departure"], leg["arrival"]) for leg in transfer["legs"]],
                         [("local", "23:50:00", "24:06:00"), ("express", "24:08:00", "24:15:00")])
        missed = self.cli(EXAMPLE, "Studio", "Depot", "23:49:00", "--change-seconds", 121, *args)
        self.assertEqual(json.loads(missed.stdout)["journeys"], [direct])
        reverse = self.cli(EXAMPLE, "Studio", "Depot", "24:15:00", "--arrive-by", *args)
        self.assertEqual(json.loads(reverse.stdout)["journeys"], [{**transfer, "departure": "23:50:00"}])
        early = self.cli(EXAMPLE, "Studio", "Depot", "24:14:59", "--arrive-by", *args)
        self.assertEqual(json.loads(early.stdout)["journeys"], [])

    def test_transfer_demo_matches_cli(self):
        fixture = ROOT / "examples" / "transfer-demo.json"
        demo = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual([(row["scenario"], row["change_seconds"]) for row in demo],
                         [(name, gap) for name in ("scheduled", "disrupted") for gap in (120, 121)])
        for row in demo:
            update = ["--service-updates", DISRUPTION] if row["scenario"] == "disrupted" else []
            query = self.cli(EXAMPLE, "Studio", "Depot", "23:49:00", "--change-seconds", row["change_seconds"], *update)
            self.assertEqual(query.returncode, 0, query.stderr)
            routes = [{"arrival": journey["arrival"], "trips": [leg["trip"] for leg in journey["legs"]]}
                      for journey in json.loads(query.stdout)["journeys"]]
            self.assertEqual(routes, row["routes"])

    def test_propagated_delay_and_all_cancelled_stops(self):
        trips = read_calls(EXAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "updates.json"
            path.write_text(json.dumps({"trips": [{"trip_id": "local", "stop_delays": [
                {"stop_sequence": 20, "delay_seconds": 180}
            ]}]}), encoding="utf-8")
            adjusted = timetable(trips, path).trips["local"]
            self.assertEqual([call.arrival - original.arrival for call, original in zip(adjusted, trips["local"])],
                             [0, 180, 180])
            self.assertEqual([call.departure - original.departure for call, original in zip(adjusted, trips["local"])],
                             [0, 180, 180])
            path.write_text(json.dumps({"trips": [{"trip_id": trip, "cancelled": True} for trip in trips]}),
                            encoding="utf-8")
            cancelled = timetable(trips, path)
            for arrive_by in [False, True]:
                self.assertEqual(plan(cancelled, "Studio", "Depot", 86_400, 120, 2, arrive_by=arrive_by), [])
                self.assertEqual(plan(cancelled, "Studio", "Studio", 86_400, 120, 2, arrive_by=arrive_by)[0]["legs"], [])
                with self.assertRaisesRegex(ValueError, "unknown origin or destination"):
                    plan(cancelled, "Unknown", "Depot", 86_400, 120, 2, arrive_by=arrive_by)

    def test_service_updates_reject_ambiguous_or_impossible_inputs(self):
        trips = read_calls(EXAMPLE)
        cases = [
            ([{"trip_id": "unknown", "cancelled": True}], "unknown or repeated trip_id"),
            ([{"trip_id": "local", "cancelled": True, "delay_seconds": 0}], "cancellation or delay"),
            ([{"trip_id": "local", "delay_seconds": True}], "nonnegative integer"),
            ([{"trip_id": "local", "delay_seconds": -1}], "nonnegative integer"),
            ([{"trip_id": "local", "stop_delays": [{"stop_sequence": 2, "delay_seconds": 1}]}],
             "unknown or repeated stop_sequence"),
            ([{"trip_id": "local", "delay_seconds": 2000, "stop_delays": [
                {"stop_sequence": 30, "delay_seconds": 0}
            ]}], "arrive before the previous departure"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "updates.json"
            for records, error in cases:
                with self.subTest(records=records):
                    path.write_text(json.dumps({"trips": records}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, error):
                        timetable(trips, path)
            path.write_text('{"trips":[],"trips":[]}', encoding="utf-8")
            invalid = self.cli(EXAMPLE, "Studio", "Depot", "23:49:00", "--service-updates", path)
            self.assertEqual((invalid.returncode, invalid.stdout), (1, ""))
            self.assertIn("duplicate service update field", invalid.stderr)

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
