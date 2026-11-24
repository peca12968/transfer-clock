import json
from pathlib import Path
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from router import Call, Timetable, plan, read_calls, read_transfers, seconds, timetable


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"
PLATFORMS = EXAMPLES / "platform-night.csv"
LINK = EXAMPLES / "platform-transfer.json"
ACCESS = EXAMPLES / "step-free-profile.json"
CLOSURE = EXAMPLES / "platform-closure.json"


def step_free_demo():
    scenarios = []
    for name, profile, rules in (("standard", None, LINK), ("step_free", ACCESS, LINK), ("closed", ACCESS, CLOSURE)):
        cases = []
        for reverse, time in ((False, "23:49:00"), (True, "24:23:00")):
            flags = ["--arrive-by"] if reverse else []
            if profile is not None:
                flags.extend(("--step-free", profile))
            result = subprocess.run([sys.executable, ROOT / "router.py", PLATFORMS, "Studio", "Depot", time,
                                     "--transfer-rules", rules, *flags], check=True, capture_output=True, text=True)
            cases.append(json.loads(result.stdout)["journeys"])
        scenarios.append({"scenario": name, "journeys": cases[0], "arrive_by": {"deadline": "24:23:00", "journeys": cases[1]}})
    return scenarios


def exhaustive(trips, rules, start, reverse):
    scores = {}
    pick = max if reverse else min

    def explore(stop, arrived, used, first):
        if stop == "D" and used:
            if not reverse or arrived <= start:
                score = first if reverse else arrived
                scores[used] = pick(scores.get(used, score), score)
            return
        if used == 3:
            return
        for calls in trips.values():
            for index, board in enumerate(calls):
                if not board.pickup:
                    continue
                minimum = rules.get((stop, board.stop), (4 if board.stop == stop else None)) if used else (
                    0 if board.stop == stop else None)
                if minimum is None or board.departure < arrived + minimum:
                    continue
                for end in calls[index + 1:]:
                    if end.dropoff:
                        explore(end.stop, end.arrival, used + 1, first if used else board.departure)

    explore("A", 0 if reverse else start, 0, None)
    result = []
    for used, score in sorted(scores.items()):
        if not result or score != result[-1][1] and pick(result[-1][1], score) == score:
            result.append((used - 1, score))
    return result


class TransferConnectionTests(unittest.TestCase):
    def test_step_free_cli_skips_unconfirmed_trips_and_respects_closures_in_both_directions(self):
        reports = step_free_demo()
        self.assertEqual(reports, json.loads((EXAMPLES / "step-free-demo.json").read_text(encoding="utf-8")))
        self.assertEqual([report["journeys"][-1]["arrival"] for report in reports], ["24:12:00", "24:23:00", "24:31:00"])
        self.assertEqual([leg["trip"] for leg in reports[1]["journeys"][-1]["legs"]], ["local", "shuttle"])
        self.assertEqual([leg["trip"] for leg in reports[1]["arrive_by"]["journeys"][-1]["legs"]], ["feeder", "shuttle"])
        self.assertEqual(reports[1]["arrive_by"]["journeys"][-1]["departure"], "23:57:00")
        self.assertEqual(reports[2]["arrive_by"]["journeys"], [])

    def test_step_free_permissions_keep_through_rides_and_require_a_confirmed_same_stop_link(self):
        legs = {"first": [Call("A", 1, 1, 1, True, False), Call("B", 2, 2, 2, False, True)],
                "last": [Call("B", 3, 3, 1, True, False), Call("D", 6, 6, 2, False, True)],
                "through": [Call("A", 0, 0, 1, True, False), Call("C", 5, 5, 2, True, True), Call("D", 8, 8, 3, False, True)]}
        profile = {"stops": ["A", "B", "D"], "trips": list(legs), "transfers": []}
        with TemporaryDirectory() as directory:
            access = Path(directory) / "profile.json"
            access.write_text(json.dumps(profile), encoding="utf-8")
            table = timetable(legs, step_free=access)
            self.assertEqual([journey["arrival"] for journey in plan(table, "A", "D", 0, 0, 2)], ["00:00:08"])
            self.assertEqual(plan(table, "A", "C", 0, 0, 2), [])
            self.assertEqual(plan(table, "C", "D", 8, 0, 2, arrive_by=True), [])
            self.assertEqual(plan(table, "A", "D", 6, 0, 2, arrive_by=True), [])
            profile["transfers"] = [{"from_stop": "B", "to_stop": "B", "minimum_seconds": 1}]
            access.write_text(json.dumps(profile), encoding="utf-8")
            table = timetable(legs, step_free=access)
            self.assertEqual(plan(table, "A", "D", 0, 120, 2)[-1]["arrival"], "00:00:06")
            route, = plan(table, "A", "D", 6, 120, 2, arrive_by=True)
            self.assertEqual(route["connections"][0]["slack_seconds"], 0)

    def test_step_free_uses_the_stricter_explicit_minimum_and_respects_trip_updates(self):
        trips = read_calls(PLATFORMS)
        with TemporaryDirectory() as directory:
            rules = Path(directory) / "rules.json"
            rules.write_text(json.dumps([{"from_stop": "Market", "to_stop": "MarketEast", "minimum_seconds": 121}]), encoding="utf-8")
            table = timetable(trips, transfer_rules=rules, step_free=ACCESS)
            route, = plan(table, "Studio", "Depot", seconds("24:23:00"), 120, 2, arrive_by=True)
            self.assertEqual(route["departure"], "23:50:00")
            self.assertEqual(route["connections"][0]["minimum_seconds"], 121)
            delayed = timetable(trips, EXAMPLES / "night-disruption.json", LINK, step_free=ACCESS)
            route, = plan(delayed, "Studio", "Depot", seconds("23:49:00"), 120, 2)
            self.assertEqual(route["arrival"], "24:33:00")
            self.assertEqual(route["connections"], [])

    def test_step_free_profile_rejects_duplicates_and_unverified_link_endpoints(self):
        profile = json.loads(ACCESS.read_text(encoding="utf-8"))
        link = profile["transfers"][0]
        bad_profiles = [
            (dict(profile, trips=["unknown"]), "list of known IDs"),
            (dict(profile, trips=["local", "local"]), "repeated ID"),
            (dict(profile, stops=["Market"]), "unknown transfer stop"),
            (dict(profile, transfers=[dict(link, minimum_seconds=None)]), "confirmed travel time"),
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            for value, message in bad_profiles:
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                    timetable(read_calls(PLATFORMS), step_free=path)

    def test_exact_walk_and_reverse_deadline_with_actual_cli(self):
        for reverse, time in ((False, "23:49:00"), (True, "24:12:00")):
            flags = ["--arrive-by"] if reverse else []
            result = subprocess.run([sys.executable, ROOT / "router.py", PLATFORMS, "Studio", "Depot", time,
                                     "--transfer-rules", LINK, *flags], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            route = json.loads(result.stdout)["journeys"][-1]
            self.assertEqual(route["arrival"], "24:12:00")
            self.assertEqual(route["connections"], [{"from_stop": "Market", "to_stop": "MarketEast",
                "arrival": "24:03:00", "ready": "24:05:00", "departure": "24:05:00", "minimum_seconds": 120,
                "slack_seconds": 0}])
            if reverse:
                self.assertEqual(route["departure"], "23:50:00")

    def test_one_second_and_closure_match_showcase(self):
        table = timetable(read_calls(PLATFORMS), transfer_rules=LINK)
        scenarios = json.loads((EXAMPLES / "platform-demo.json").read_text(encoding="utf-8"))
        results = []
        for minimum, scenario in ((120, "exact"), (121, "missed"), (None, "closed")):
            adjusted = Timetable(table.trips, table.stops, {("Market", "MarketEast"): minimum})
            trips = plan(adjusted, "Studio", "Depot", seconds("23:49:00"), 120, 2)
            deadline = "24:23:00"
            reverse = plan(adjusted, "Studio", "Depot", seconds(deadline), 120, 2, arrive_by=True)
            results.append({"scenario": scenario, "minimum_seconds": minimum, "journeys": trips,
                            "arrive_by": {"deadline": deadline, "journeys": reverse}})
        self.assertEqual(results, scenarios)
        self.assertEqual([row["journeys"][-1]["arrival"] for row in results], ["24:12:00", "24:23:00", "24:31:00"])
        self.assertEqual(results[1]["journeys"][-1]["connections"][0]["slack_seconds"], 239)
        self.assertEqual(results[2]["journeys"][-1]["connections"], [])
        self.assertEqual([row["arrive_by"]["journeys"][-1]["departure"] for row in results[:2]], ["23:57:00", "23:50:00"])
        self.assertEqual(results[2]["arrive_by"]["journeys"], [])

    def test_same_stop_override_and_one_way_between_two_rides(self):
        def segment(a, b, start, end):
            return [Call(a, start, start, 1, True, False), Call(b, end, end, 2, False, True)]
        trips = {"ad": segment("A", "B", 0, 2), "link": segment("C", "D", 5, 9),
                 "da": segment("D", "C", 10, 12), "return": segment("B", "A", 15, 16),
                 "same": segment("B", "D", 3, 20)}
        stops = frozenset("ABCD")
        table = Timetable(trips, stops, {("B", "C"): 3, ("B", "B"): None})
        self.assertEqual(plan(table, "A", "D", 0, 0, 2)[-1]["arrival"], "00:00:09")
        self.assertEqual(plan(table, "D", "A", 10, 0, 2), [])
        self.assertEqual(plan(table, "D", "A", 20, 0, 2, arrive_by=True), [])
        self.assertEqual(plan(table, "B", "C", 0, 0, 2), [])
        closed = Timetable(trips, stops, {("B", "C"): None, ("B", "B"): None})
        self.assertEqual(plan(closed, "A", "D", 0, 0, 2), [])
        self.assertEqual(plan(trips, "A", "D", 0, 0, 2)[0]["arrival"], "00:00:20")

    def test_platform_link_combines_with_delays_and_cancelled_routes(self):
        table = timetable(read_calls(PLATFORMS), EXAMPLES / "night-disruption.json", LINK)
        for reverse, moment in ((False, "23:49:00"), (True, "24:15:00")):
            route = plan(table, "Studio", "Depot", seconds(moment), 120, 2, arrive_by=reverse)[-1]
            self.assertEqual(route["arrival"], "24:15:00")
            self.assertEqual([leg["trip"] for leg in route["legs"]], ["local", "express"])
            self.assertEqual([route["connections"][0][field] for field in ("arrival", "ready", "departure", "slack_seconds")],
                             ["24:06:00", "24:08:00", "24:08:00", 0])

    def test_cross_links_and_same_stop_rules_match_forward_enumeration(self):
        rng = random.Random(620)
        stops = frozenset("ABCD")
        pairs = [(a, b) for a in sorted(stops) for b in sorted(stops)]
        used_walks = 0
        for _ in range(100):
            trips = {}
            for ride in range(6):
                stamp = rng.randrange(26)
                calls = []
                for idx, stop in enumerate(rng.choices(sorted(stops), k=4)):
                    stamp += rng.randrange(1, 6)
                    calls.append(Call(stop, stamp, stamp, idx, rng.random() > .2, rng.random() > .2))
                trips[str(ride)] = calls
            rules = {pair: rng.choice((None, 0, 2, 7)) for pair in rng.sample(pairs, 8)}
            table = Timetable(trips, stops, rules)
            for reverse, time in ((False, 0), (True, 37)):
                trips_for_query = plan(table, "A", "D", time, 4, 2, arrive_by=reverse)
                field = "departure" if reverse else "arrival"
                self.assertEqual([(route["transfers"], seconds(route[field])) for route in trips_for_query],
                                 exhaustive(trips, rules, time, reverse))
                for route in trips_for_query:
                    for connection in route["connections"]:
                        origin, destination = connection["from_stop"], connection["to_stop"]
                        self.assertEqual(connection["minimum_seconds"], rules.get((origin, destination), 4))
                        self.assertGreaterEqual(connection["slack_seconds"], 0)
                        used_walks += origin != destination
        self.assertGreater(used_walks, 0)

    def test_rejects_ambiguous_rules_and_invalid_stops(self):
        valid = {"from_stop": "A", "to_stop": "B", "minimum_seconds": 4}
        invalid = [(json.dumps([dict(valid, minimum_seconds=limit)]), "nonnegative integer")
                   for limit in (True, -1, 1.5)]
        invalid.extend([
            (json.dumps([valid, valid]), "repeated transfer"),
            (json.dumps([dict(valid, to_stop="Z")]), "unknown transfer stop"),
            ('[{"from_stop":"A","from_stop":"B","to_stop":"B","minimum_seconds":1}]', "duplicate transfer field"),
        ])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            for value, match in invalid:
                path.write_text(value, encoding="utf-8")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, match):
                    read_transfers(path, {"A", "B"})


if __name__ == "__main__":
    unittest.main()
