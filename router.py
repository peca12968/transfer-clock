"""Find the earliest arrivals for each useful transfer count on one service day."""
import argparse
from collections import defaultdict
import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re


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
class Leg:
    trip: str
    origin: str
    destination: str
    board_sequence: int
    exit_sequence: int
    departure: str
    arrival: str


@dataclass(frozen=True)
class Label:
    time: int
    previous: "Label | None"
    leg: Leg | None

    def journey(self):
        legs = []
        label = self
        while label.leg is not None:
            legs.append(asdict(label.leg))
            label = label.previous
        return {"arrival": clock(self.time), "transfers": max(0, len(legs) - 1), "legs": legs[::-1]}


def plan(trips, origin, destination, start, change_seconds, max_transfers):
    if min(start, change_seconds, max_transfers) < 0:
        raise ValueError("time, transfer allowance and transfer limit must be nonnegative")
    stops = {call.stop for calls in trips.values() for call in calls}
    if origin not in stops or destination not in stops:
        raise ValueError("unknown origin or destination")
    previous = {origin: Label(start, None, None)}
    if origin == destination:
        return [previous[origin].journey()]
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
                              clock(boarding.departure), clock(call.arrival))
                    current[call.stop] = Label(call.arrival, previous[boarding.stop], leg)
                    best[call.stop] = call.arrival
                if boarding is None and call.pickup and call.stop in previous:
                    if previous[call.stop].time + allowance <= call.departure:
                        boarding = call
        if not current:
            break
        if destination in current:
            journeys.append(current[destination].journey())
        previous = current
    return journeys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stop_times", type=Path)
    parser.add_argument("origin")
    parser.add_argument("destination")
    parser.add_argument("start")
    parser.add_argument("--change-seconds", type=int, default=120)
    parser.add_argument("--max-transfers", type=int, default=2)
    args = parser.parse_args()
    try:
        trips = read_calls(args.stop_times)
        journeys = plan(trips, args.origin, args.destination, seconds(args.start),
                        args.change_seconds, args.max_transfers)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps({"journeys": journeys}, indent=2))


if __name__ == "__main__":
    main()
