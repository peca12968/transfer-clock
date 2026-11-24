import json
from pathlib import Path
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from area_trip import plan_area, read_walks
from router import Call, Timetable, read_calls, seconds, timetable


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"
TABLE = EXAMPLES / "area-night.csv"
WALKS = EXAMPLES / "area-walks.json"
LINKS = EXAMPLES / "platform-transfer.json"
CONFIRMED = EXAMPLES / "area-step-free.json"


def cli(time, *, reverse=False, step_free=False, extra=()):
    flags = ["--arrive-by"] if reverse else []
    if step_free:
        flags.extend(("--step-free", CONFIRMED))
    return subprocess.run([sys.executable, ROOT / "area_trip.py", TABLE, WALKS, time,
                           "--transfer-rules", LINKS, *flags, *extra], capture_output=True, text=True, timeout=15)


def exhaustive(table, access, egress, time, minimum, limit, *, reverse):
    """Enumerate forward rides even when verifying the latest departure."""
    choose = max if reverse else min
    scores = {}

    def explore(stop, arrival, rides, first_departure):
        if rides and stop in egress:
            end = arrival + egress[stop]
            if not reverse or end <= time:
                score = first_departure if reverse else end
                scores[rides] = choose(scores.get(rides, score), score)
        if rides > limit:
            return
        for calls in table.trips.values():
            for index, board in enumerate(calls):
                gap = 0 if stop == board.stop else None
                if rides:
                    gap = table.transfers.get((stop, board.stop), minimum if stop == board.stop else None)
                if not board.pickup or gap is None or board.departure < arrival + gap:
                    continue
                departure = first_departure if rides else board.departure - access[stop]
                for end in calls[index + 1:]:
                    if end.dropoff and (not reverse or end.arrival <= time):
                        explore(end.stop, end.arrival, rides + 1, departure)

    for origin, walk in access.items():
        explore(origin, walk if reverse else time + walk, 0, None)
    frontier = []
    for rides, score in sorted(scores.items()):
        if not frontier or score != frontier[-1][1] and choose(score, frontier[-1][1]) == score:
            frontier.append((rides - 1, score))
    return frontier


class AreaTripTests(unittest.TestCase):
    def test_published_door_to_door_cases_match_cli(self):
        rows = json.loads((EXAMPLES / "area-demo.json").read_text(encoding="utf-8"))
        self.assertEqual([row["scenario"] for row in rows], ["departure", "tight", "later", "step_free"])
        for row in rows:
            result = cli(row["time"], reverse=row["arrive_by"], step_free=row["step_free"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["journeys"], row["journeys"])
        outbound, tight, later, confirmed = [row["journeys"] for row in rows]
        self.assertEqual([(route["transfers"], route["arrival"]) for route in outbound], [(0, "24:15:00"), (1, "24:14:00")])
        direct, express = outbound
        self.assertEqual(direct["egress"], {"stop": "DepotEast", "seconds": 300, "leave_stop_at": "24:10:00"})
        self.assertEqual(express["access"], {"stop": "Studio", "seconds": 60, "ready_at": "23:50:00"})
        self.assertEqual(express["connections"][0]["slack_seconds"], 0)
        self.assertEqual([(routes[0]["departure"], routes[0]["access"]["stop"]) for routes in (tight, later, confirmed)],
                         [("23:49:00", "Studio"), ("23:50:00", "Avenue"), ("23:49:00", "Studio")])

    def test_reverse_deadline_excludes_pre_service_day_and_one_second_miss(self):
        table = timetable({"ride": [Call("A", 3, 3, 1, True, False), Call("B", 9, 9, 2, False, True)]})
        self.assertEqual(plan_area(table, {"A": 4}, {"B": 2}, 11, 0, 2, arrive_by=True), [])
        self.assertEqual(plan_area(table, {"A": 3}, {"B": 2}, 10, 0, 2, arrive_by=True), [])
        journey, = plan_area(table, {"A": 3}, {"B": 2}, 11, 0, 2, arrive_by=True)
        self.assertEqual((journey["departure"], journey["arrival"]), ("00:00:00", "00:00:11"))

    def test_endpoint_walks_combine_with_delays_and_cancellation(self):
        with TemporaryDirectory() as temp:
            updates = Path(temp) / "updates.json"
            updates.write_text(json.dumps({"trips": [
                {"trip_id": "direct", "cancelled": True}, {"trip_id": "express", "delay_seconds": 60},
            ]}), encoding="utf-8")
            extra = ("--service-updates", updates)
            too_early = cli("24:14:59", reverse=True, extra=extra)
            on_time = cli("24:15:00", reverse=True, extra=extra)
        self.assertEqual(too_early.returncode, 0, too_early.stderr)
        self.assertEqual(on_time.returncode, 0, on_time.stderr)
        self.assertEqual(json.loads(too_early.stdout)["journeys"], [])
        journey, = json.loads(on_time.stdout)["journeys"]
        self.assertEqual((journey["departure"], journey["arrival"]), ("23:49:00", "24:15:00"))
        self.assertEqual([leg["trip"] for leg in journey["legs"]], ["local", "express"])
        self.assertEqual(journey["connections"][0]["slack_seconds"], 60)

    def test_walk_validation_and_explicit_access_confirmation(self):
        table = timetable(read_calls(TABLE), step_free=CONFIRMED)
        origins, destinations = read_walks(WALKS, table)
        self.assertEqual(origins, {"Studio": 60})
        self.assertEqual(destinations, {"Depot": 120, "DepotEast": 300})
        with TemporaryDirectory() as temp:
            path = Path(temp) / "walks.json"
            path.write_text('{"origin":[{"stop":"Studio","seconds":0,"seconds":5}],"destination":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "repeated|duplicate"):
                read_walks(path, table)
            for walks in ([{"stop": "Typos", "seconds": 0}], [{"stop": "Studio", "seconds": True}],
                          [{"stop": "Studio", "seconds": -1}], [{"stop": "Studio", "seconds": 1, "step_free": 1}],
                          [{"stop": "Avenue", "seconds": 3}] * 2):
                path.write_text(json.dumps({"origin": walks, "destination": [{"stop": "Depot", "seconds": 0}]}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_walks(path, table)
        self.assertEqual(plan_area(table, {}, destinations, 0, 0, 2, arrive_by=True), [])

    def test_forward_and_reverse_match_exhaustive_search(self):
        rng = random.Random(389)
        stops = "ABCDE"
        for example in range(120):
            trips = {}
            for i in range(7):
                when = rng.randrange(34)
                calls = []
                for index in range(rng.randint(2, 4)):
                    when += rng.randrange(1, 9)
                    calls.append(Call(rng.choice(stops), when, when, index, rng.random() > .2, rng.random() > .2))
                trips[str(i)] = calls
            links = {(a, b): rng.choice((0, 1, 7, None)) for a in stops for b in stops if rng.random() < .22}
            table = Timetable(trips, frozenset(stops), links)
            access = {stop: rng.randrange(12) for stop in rng.sample(stops, 2)}
            egress = {stop: rng.randrange(12) for stop in rng.sample(stops, 2)}
            for reverse, time, field in ((False, 4, "arrival"), (True, 38, "departure")):
                with self.subTest(example=example, reverse=reverse):
                    actual = plan_area(table, access, egress, time, 3, 2, arrive_by=reverse)
                    scores = [(route["transfers"], seconds(route[field])) for route in actual]
                    self.assertEqual(scores, exhaustive(table, access, egress, time, 3, 2, reverse=reverse))
