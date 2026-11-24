"""Find earliest arrivals or latest departures for each useful transfer count."""
import argparse
from collections import defaultdict
import csv
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re

from service_updates import read_active_trips, unique_fields


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
    transfers: dict[tuple[str, str], int | None] | None = None
    step_free_stops: frozenset[str] | None = None


def read_transfers(path, stops):
    pairs = json.loads(path.read_text(encoding="utf-8-sig"),
                       object_pairs_hook=lambda fields: unique_fields(fields, "transfer"))
    return parse_transfers(pairs, stops)


def parse_transfers(pairs, stops):
    if not isinstance(pairs, list):
        raise ValueError("expected an array of transfer rules")
    transfers = {}
    for rule in pairs:
        if not isinstance(rule, dict) or set(rule) != {"from_stop", "to_stop", "minimum_seconds"}:
            raise ValueError("expected from_stop, to_stop and minimum_seconds for each transfer")
        origin, destination, minimum = rule["from_stop"], rule["to_stop"], rule["minimum_seconds"]
        if not all(isinstance(stop, str) and stop in stops for stop in (origin, destination)):
            raise ValueError(f"unknown transfer stop: {origin!r} to {destination!r}")
        pair = origin, destination
        if pair in transfers:
            raise ValueError(f"repeated transfer: {origin!r} to {destination!r}")
        if minimum is not None and (type(minimum) is not int or minimum < 0):
            raise ValueError("minimum_seconds must be a nonnegative integer or null")
        transfers[pair] = minimum
    return transfers


def read_step_free(path, stops, trips):
    profile = json.loads(path.read_text(encoding="utf-8-sig"),
                         object_pairs_hook=lambda fields: unique_fields(fields, "step-free profile"))
    if not isinstance(profile, dict) or set(profile) != {"stops", "trips", "transfers"}:
        raise ValueError("step-free profile requires stops, trips and transfers")

    def confirmed(field, known):
        items = profile[field]
        if not isinstance(items, list) or any(not isinstance(item, str) or item not in known for item in items):
            raise ValueError(f"step-free {field} must be a list of known IDs")
        if len(set(items)) != len(items):
            raise ValueError(f"repeated ID in step-free {field}")
        return frozenset(items)

    access_stops = confirmed("stops", stops)
    vehicles = confirmed("trips", trips)
    links = parse_transfers(profile["transfers"], access_stops)
    if any(minimum is None for minimum in links.values()):
        raise ValueError("step-free transfers require a confirmed travel time")
    return access_stops, vehicles, links


def timetable(trips, updates=None, transfer_rules=None, *, step_free=None):
    stops = frozenset(call.stop for calls in trips.values() for call in calls)
    active = read_active_trips(trips, updates) if updates is not None else trips
    transfers = read_transfers(transfer_rules, stops) if transfer_rules is not None else None
    if step_free is None:
        return Timetable(active, stops, transfers)
    access_stops, access_trips, access_links = read_step_free(step_free, stops, trips)
    active = {trip: [call if call.stop in access_stops else replace(call, pickup=False, dropoff=False)
                     for call in calls] for trip, calls in active.items() if trip in access_trips}
    rules = transfers or {}
    links = {pair: max(minimum, rules.get(pair, 0)) for pair, minimum in access_links.items()
             if rules.get(pair, 0) is not None}
    return Timetable(active, stops, links, access_stops)


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


def boarding_times(labels, outgoing, default):
    ready = {}
    for origin, label in labels.items():
        links = {origin: default, **outgoing.get(origin, {})}
        for stop, minimum in links.items():
            if minimum is None:
                continue
            time = label.time + minimum
            if stop not in ready or time < ready[stop][0]:
                ready[stop] = (time, label)
    return ready


def earliest_labels(trips, origin, destination, start, change_seconds, max_transfers, transfers=None):
    previous = {origin: Label(start, None, None)}
    if origin == destination:
        return [previous[origin]]
    # Returning to the origin can enable a walk that is available only between rides.
    best = {} if transfers else {origin: start}
    outgoing = defaultdict(dict)
    for (from_stop, to_stop), minimum in (transfers or {}).items():
        outgoing[from_stop][to_stop] = minimum
    journeys = []
    for ride in range(max_transfers + 1):
        current = {}
        ready = boarding_times(previous, outgoing, change_seconds) if ride else {origin: (start, previous[origin])}
        for trip, calls in trips.items():
            boarding = None
            for call in calls:
                if boarding is not None and call.dropoff and (
                        call.stop not in best or call.arrival < best[call.stop]):
                    before, preceding = boarding
                    leg = Leg(trip, before.stop, call.stop, before.sequence, call.sequence,
                              before.departure, call.arrival)
                    current[call.stop] = Label(call.arrival, preceding, leg)
                    best[call.stop] = call.arrival
                if boarding is None and call.pickup and call.stop in ready:
                    time, preceding = ready[call.stop]
                    if time <= call.departure:
                        boarding = call, preceding
        if not current:
            break
        if destination in current:
            journeys.append(current[destination])
        previous = current
    return journeys


def connection_details(legs, transfers, change_seconds):
    connections = []
    for before, after in zip(legs, legs[1:]):
        origin, destination = before.destination, after.origin
        pair = origin, destination
        minimum = transfers.get(pair, change_seconds) if origin == destination else transfers[pair]
        ready = before.arrival + minimum
        connections.append({"from_stop": origin, "to_stop": destination,
                            "arrival": clock(before.arrival), "ready": clock(ready), "departure": clock(after.departure),
                            "minimum_seconds": minimum, "slack_seconds": after.departure - ready})
    return connections


def plan(trips, origin, destination, start, change_seconds, max_transfers, *, arrive_by=False):
    if min(start, change_seconds, max_transfers) < 0:
        raise ValueError("time, transfer allowance and transfer limit must be nonnegative")
    table = trips if isinstance(trips, Timetable) else timetable(trips)
    if origin not in table.stops or destination not in table.stops:
        raise ValueError("unknown origin or destination")
    default_change = change_seconds
    if table.step_free_stops is not None:
        if origin not in table.step_free_stops or destination not in table.step_free_stops:
            return []
        default_change = None
    trips = table.trips
    transfers = table.transfers or {}
    if arrive_by:
        # In the mirrored timetable, the latest feasible departure becomes the earliest arrival.
        last_departure = max((calls[-1].departure for calls in trips.values() if calls), default=start)
        horizon = max(start, last_departure)
        trips = {trip: [c.reversed_at(horizon) for c in reversed(calls)] for trip, calls in trips.items()}
        transfers = {(to_stop, from_stop): minimum for (from_stop, to_stop), minimum in transfers.items()}
        origin, destination = destination, origin
        start = horizon - start
    results = []
    for label in earliest_labels(trips, origin, destination, start, default_change, max_transfers, transfers):
        legs = label.legs()
        if arrive_by:
            legs = [leg.reversed_at(horizon) for leg in reversed(legs)]
            departure = horizon - label.time
            arrival = legs[-1].arrival if legs else departure
            times = {"departure": clock(departure), "arrival": clock(arrival)}
        else:
            times = {"arrival": clock(label.time)}
        journey = {**times, "transfers": max(0, len(legs)-1), "legs": [leg.record() for leg in legs]}
        if table.transfers is not None:
            journey["connections"] = connection_details(legs, table.transfers, default_change)
        results.append(journey)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stop_times", type=Path)
    parser.add_argument("origin")
    parser.add_argument("destination")
    parser.add_argument("time", help="earliest departure, or arrival deadline with --arrive-by")
    parser.add_argument("--arrive-by", action="store_true", help="find the latest departures meeting the deadline")
    parser.add_argument("--service-updates", type=Path, help="apply delays and cancellations from a JSON file")
    parser.add_argument("--transfer-rules", type=Path, help="load directional connections and minimum transfer times")
    parser.add_argument("--step-free", type=Path, help="require confirmed step-free trips, stops and directional transfers")
    parser.add_argument("--change-seconds", type=int, default=120)
    parser.add_argument("--max-transfers", type=int, default=2)
    args = parser.parse_args()
    try:
        trips = timetable(read_calls(args.stop_times), args.service_updates, args.transfer_rules, step_free=args.step_free)
        journeys = plan(trips, args.origin, args.destination, seconds(args.time),
                        args.change_seconds, args.max_transfers, arrive_by=args.arrive_by)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps({"journeys": journeys}, indent=2))


if __name__ == "__main__":
    main()
