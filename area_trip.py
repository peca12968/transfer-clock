"""Compare transit journeys with several access and exit walks in one search."""
import argparse
import csv
import json
from pathlib import Path

from router import buffered_transfers, clock, connection_details, read_calls, reverse_timetable, scan_labels, seconds, timetable
from service_updates import unique_fields


def read_walks(path, table):
    config = json.loads(path.read_text(encoding="utf-8-sig"),
                        object_pairs_hook=lambda fields: unique_fields(fields, "endpoint walk"))
    if not isinstance(config, dict) or config.keys() != {"origin", "destination"}:
        raise ValueError("expected origin and destination walk arrays")

    def load(side):
        rows = config[side]
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{side} must be a nonempty walk array")
        walks = {}
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not {"stop", "seconds"} <= row.keys() <= {"stop", "seconds", "step_free"}:
                raise ValueError(f"{side} walks require stop and seconds, with optional step_free")
            stop, duration, confirmed = row["stop"], row["seconds"], row.get("step_free", False)
            if not isinstance(stop, str) or stop not in table.stops or stop in seen:
                raise ValueError(f"{side}: unknown or repeated stop {stop!r}")
            if type(duration) is not int or duration < 0 or type(confirmed) is not bool:
                raise ValueError(f"{side} walk requires nonnegative integer seconds and boolean step_free")
            seen.add(stop)
            if table.step_free_stops is None or confirmed and stop in table.step_free_stops:
                walks[stop] = duration
        return walks

    return load("origin"), load("destination")


def plan_area(table, access, egress, time, change_seconds, max_transfers, *, arrive_by=False, transfer_buffer=0):
    if min(time, change_seconds, max_transfers) < 0:
        raise ValueError("time, transfer allowance and transfer limit must be nonnegative")
    rules = table.transfers or {}
    minimum = change_seconds if table.step_free_stops is None else None
    required, links = buffered_transfers(minimum, rules, transfer_buffer)
    if not access or not egress:
        return []
    trips = table.trips
    start, boarding_walks, exit_walks = time, access, egress
    if arrive_by:
        trips, links, horizon = reverse_timetable(trips, links, time)
        start, boarding_walks, exit_walks = horizon - time, egress, access
    starts = {stop: start + duration for stop, duration in boarding_walks.items()}

    results = []
    best = None
    for labels in scan_labels(trips, starts, required, max_transfers, links):
        target = min((stop for stop in labels if stop in exit_walks),
                     key=lambda stop: (labels[stop].time + exit_walks[stop], stop), default=None)
        if target is None:
            continue
        label = labels[target]
        score = label.time + exit_walks[target]
        if (arrive_by and score > horizon) or (best is not None and score >= best):
            continue
        best = score
        legs = label.legs()
        departure = time
        if arrive_by:
            legs = [leg.reversed_at(horizon) for leg in reversed(legs)]
            departure = horizon - score
        first, last = legs[0], legs[-1]
        before, after = access[first.origin], egress[last.destination]
        results.append({
            "departure": clock(departure), "arrival": clock(last.arrival + after),
            "access": {"stop": first.origin, "seconds": before, "ready_at": clock(departure + before)},
            "egress": {"stop": last.destination, "seconds": after, "leave_stop_at": clock(last.arrival)},
            "transfers": len(legs) - 1, "legs": [leg.record() for leg in legs],
            "connections": connection_details(legs, rules, minimum, transfer_buffer),
        })
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stop_times", type=Path)
    parser.add_argument("walks", type=Path)
    parser.add_argument("time", help="earliest origin departure or final arrival deadline with --arrive-by")
    parser.add_argument("--arrive-by", action="store_true")
    parser.add_argument("--service-updates", type=Path)
    parser.add_argument("--transfer-rules", type=Path)
    parser.add_argument("--step-free", type=Path)
    parser.add_argument("--change-seconds", type=int, default=120)
    parser.add_argument("--transfer-buffer-seconds", type=int, default=0, help="add time to each permitted transfer")
    parser.add_argument("--max-transfers", type=int, default=2)
    args = parser.parse_args()
    try:
        table = timetable(read_calls(args.stop_times), args.service_updates, args.transfer_rules, step_free=args.step_free)
        access, egress = read_walks(args.walks, table)
        journeys = plan_area(table, access, egress, seconds(args.time), args.change_seconds, args.max_transfers,
                             arrive_by=args.arrive_by, transfer_buffer=args.transfer_buffer_seconds)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps({"journeys": journeys}, indent=2))


if __name__ == "__main__":
    main()
