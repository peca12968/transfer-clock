"""Find earliest arrivals or latest departures for each useful transfer count."""
import argparse
from collections import defaultdict
import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from service_updates import read_active_trips


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+:[0-5][0-9]:[0-5][0-9]", value):
        raise ValueError(f"invalid service-day time {value!r}")
    h, m, s = map(int, value.split(":"))
    return 3600 * h + 60 * m + s


def clock(value):
    return f"{value // 3600:02}:{value // 60 % 60:02}:{value % 60:02}"


@dataclass(frozen=True)
class Call:
    stop: str
    arrival: int
    departure: int
    sequence: int
    pickup: bool
    dropoff: bool

    def reversed_at(self, horizon):
        return Call(self.stop, horizon - self.departure, horizon - self.arrival,
                    self.sequence, self.dropoff, self.pickup)


def allowed(value):
    if value not in ("", "0", "1"):
        raise ValueError("pickup and drop-off must be regular (0), unavailable (1), or blank")
    return value != "1"


def read_calls(path):
    trips = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream, strict=True)
        required = {"trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"}
        if not required.issubset(rows.fieldnames or []) or len(set(rows.fieldnames)) != len(rows.fieldnames):
            raise ValueError("invalid or missing stop_times columns")
        for row in rows:
            try:
                if None in row or None in row.values():
                    raise ValueError("wrong column count")
                if not row["trip_id"].strip() or not row["stop_id"].strip():
                    raise ValueError("empty trip_id or stop_id")
                if not re.fullmatch(r"[0-9]+", row["stop_sequence"]):
                    raise ValueError("invalid stop_sequence")
                call = Call(row["stop_id"], seconds(row["arrival_time"]), seconds(row["departure_time"]),
                            int(row["stop_sequence"]), allowed(row.get("pickup_type", "")),
                            allowed(row.get("drop_off_type", "")))
                if call.departure < call.arrival:
                    raise ValueError("departure precedes arrival")
                trips[row["trip_id"]].append(call)
            except ValueError as error:
                raise ValueError(f"line {rows.line_num}: {error}") from error
    for trip, calls in trips.items():
        calls.sort(key=lambda c: c.sequence)
        for a, b in zip(calls, calls[1:]):
            if a.sequence == b.sequence or a.departure > b.arrival:
                raise ValueError(f"trip {trip}: repeated sequence or reversed time")
    return dict(sorted(trips.items()))


@dataclass(frozen=True)
class Timetable:
    trips: dict[str, list[Call]]
    stops: frozenset[str]


def timetable(trips, updates=None):
    stops = frozenset(call.stop for calls in trips.values() for call in calls)
    active = read_active_trips(trips, updates) if updates is not None else trips
    return Timetable(active, stops)


@dataclass(frozen=True)
class Leg:
    trip: str
    origin: str
    destination: str
    board_sequence: int
    exit_sequence: int
    departure: int
    arrival: int

    def reversed_at(self, horizon):
        return Leg(self.trip, self.destination, self.origin, self.exit_sequence, self.board_sequence,
                   horizon - self.arrival, horizon - self.departure)

    def record(self):
        return {**asdict(self), "departure": clock(self.departure), "arrival": clock(self.arrival)}


@dataclass(frozen=True)
class Label:
    time: int
    previous: "Label | None"
    leg: Leg | None

    def legs(self):
        legs = []
        label = self
        while label.leg is not None:
            legs.append(label.leg)
            label = label.previous
        return legs[::-1]


def earliest_labels(trips, origin, destination, start, change_seconds, max_transfers):
    previous = {origin: Label(start, None, None)}
    if origin == destination:
        return [previous[origin]]
    best = {origin: start}
    journeys = []
    for ride in range(max_transfers + 1):
        current = {}
        allowance = change_seconds if ride else 0
        for trip, calls in trips.items():
            boarding = None
            for call in calls:
                if boarding is not None and call.dropoff and (
                        call.stop not in best or call.arrival < best[call.stop]):
                    leg = Leg(trip, boarding.stop, call.stop, boarding.sequence, call.sequence,
                              boarding.departure, call.arrival)
                    current[call.stop] = Label(call.arrival, previous[boarding.stop], leg)
                    best[call.stop] = call.arrival
                if boarding is None and call.pickup and call.stop in previous:
                    if previous[call.stop].time + allowance <= call.departure:
                        boarding = call
        if not current:
            break
        if destination in current:
            journeys.append(current[destination])
        previous = current
    return journeys


def plan(trips, origin, destination, start, change_seconds, max_transfers, *, arrive_by=False):
    if min(start, change_seconds, max_transfers) < 0:
        raise ValueError("time, transfer allowance and transfer limit must be nonnegative")
    table = trips if isinstance(trips, Timetable) else timetable(trips)
    if origin not in table.stops or destination not in table.stops:
        raise ValueError("unknown origin or destination")
    trips = table.trips
    if arrive_by:
        # In the mirrored timetable, the latest feasible departure becomes the earliest arrival.
        last_departure = max((calls[-1].departure for calls in trips.values() if calls), default=start)
        horizon = max(start, last_departure)
        trips = {trip: [c.reversed_at(horizon) for c in reversed(calls)] for trip, calls in trips.items()}
        origin, destination = destination, origin
        start = horizon - start
    results = []
    for label in earliest_labels(trips, origin, destination, start, change_seconds, max_transfers):
        legs = label.legs()
        if arrive_by:
            legs = [leg.reversed_at(horizon) for leg in reversed(legs)]
            departure = horizon - label.time
            arrival = legs[-1].arrival if legs else departure
            times = {"departure": clock(departure), "arrival": clock(arrival)}
        else:
            times = {"arrival": clock(label.time)}
        results.append({**times, "transfers": max(0, len(legs)-1), "legs": [leg.record() for leg in legs]})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stop_times", type=Path)
    parser.add_argument("origin")
    parser.add_argument("destination")
    parser.add_argument("time", help="earliest departure, or arrival deadline with --arrive-by")
    parser.add_argument("--arrive-by", action="store_true", help="find the latest departures meeting the deadline")
    parser.add_argument("--service-updates", type=Path, help="apply delays and cancellations from a JSON file")
    parser.add_argument("--change-seconds", type=int, default=120)
    parser.add_argument("--max-transfers", type=int, default=2)
    args = parser.parse_args()
    try:
        trips = timetable(read_calls(args.stop_times), args.service_updates)
        journeys = plan(trips, args.origin, args.destination, seconds(args.time),
                        args.change_seconds, args.max_transfers, arrive_by=args.arrive_by)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps({"journeys": journeys}, indent=2))


if __name__ == "__main__":
    main()
