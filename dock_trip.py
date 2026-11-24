"""Plan a bike return and onward transit journey against one station snapshot."""
import argparse
import csv
import json
import math
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from router import clock, plan, read_calls, seconds, timetable


DEFAULT_DLL = Path(__file__).resolve().parent.parent / "return-radius/bin/Release/net10.0/ReturnRadius.dll"


def available_docks(config, stops, needed, max_meters, dll):
    if not isinstance(config, dict) or not isinstance(config.get("docks"), list):
        raise ValueError("expected a docks array")
    origin = config.get("origin")
    if not isinstance(origin, dict) or set(origin) != {"latitude", "longitude"}:
        raise ValueError("origin must contain latitude and longitude")
    stations = []
    access = {}
    fields = ("id", "latitude", "longitude", "freeDocks")
    for dock in config["docks"]:
        if not isinstance(dock, dict) or not all(key in dock for key in fields):
            raise ValueError("dock missing a station field")
        name = dock["id"]
        if not isinstance(name, str) or not name.strip() or name in access:
            raise ValueError("expected unique nonempty dock ids")
        if not isinstance(dock.get("stop"), str) or dock["stop"] not in stops:
            raise ValueError(f"dock {name}: unknown timetable stop")
        for key in ("rideSeconds", "returnAndWalkSeconds"):
            if type(dock.get(key)) is not int or dock[key] < 0:
                raise ValueError(f"dock {name}: {key} must be a nonnegative integer")
        access[name] = dock
        stations.append({key: dock[key] for key in fields})
    if not dll.is_file():
        raise ValueError(f"build Return Radius first or supply --radius-dll: {dll}")
    query = {**origin, "needed": needed, "maxMeters": max_meters, "limit": max(1, len(stations))}
    with TemporaryDirectory() as temp:
        path = Path(temp) / "query.json"
        path.write_text(json.dumps({"stations": stations, "queries": [query]}, allow_nan=False), encoding="utf-8")
        run = subprocess.run(["dotnet", str(dll), str(path)], capture_output=True, encoding="utf-8")
    if run.returncode:
        raise ValueError("station search failed: " + run.stderr.strip())
    matches = json.loads(run.stdout)[0]["matches"]
    return [(access[m["id"]], m["meters"]) for m in matches]


def connect(trips, docks, destination, time, change_seconds, max_transfers, arrive_by):
    journeys = []
    for dock, distance in docks:
        ride = dock["rideSeconds"]
        access = ride + dock["returnAndWalkSeconds"]
        timetable_time = time if arrive_by else time + access
        options = plan(trips, dock["stop"], destination, timetable_time, change_seconds,
                       max_transfers, arrive_by=arrive_by)
        for option in options:
            start = seconds(option["departure"]) - access if arrive_by else time
            if start < 0:
                continue
            journeys.append({
                "dock": dock["id"], "meters": distance, "freeDocks": dock["freeDocks"], "stop": dock["stop"],
                "departOrigin": clock(start), "atDock": clock(start + ride), "readyAtStop": clock(start + access),
                "arrival": option["arrival"], "transfers": option["transfers"], "legs": option["legs"],
            })
    def order(journey):
        score = -seconds(journey["departOrigin"]) if arrive_by else seconds(journey["arrival"])
        return score, journey["transfers"], journey["meters"], journey["dock"]
    return sorted(journeys, key=order)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stop_times", type=Path)
    parser.add_argument("access_snapshot", type=Path)
    parser.add_argument("destination")
    parser.add_argument("time", help="origin departure, or destination deadline with --arrive-by")
    parser.add_argument("--needed", type=int, default=1)
    parser.add_argument("--max-meters", type=float, required=True)
    parser.add_argument("--arrive-by", action="store_true")
    parser.add_argument("--service-updates", type=Path, help="apply delays and cancellations from a JSON file")
    parser.add_argument("--change-seconds", type=int, default=120)
    parser.add_argument("--max-transfers", type=int, default=2)
    parser.add_argument("--radius-dll", type=Path, default=DEFAULT_DLL)
    args = parser.parse_args()
    try:
        time = seconds(args.time)
        if not math.isfinite(args.max_meters) or args.max_meters < 0 or args.needed < 1:
            raise ValueError("expected a nonnegative finite radius and a positive bike count")
        if min(args.change_seconds, args.max_transfers) < 0:
            raise ValueError("transfer allowance and transfer limit must be nonnegative")
        trips = timetable(read_calls(args.stop_times), args.service_updates)
        if args.destination not in trips.stops:
            raise ValueError("unknown destination")
        config = json.loads(args.access_snapshot.read_text(encoding="utf-8-sig"))
        docks = available_docks(config, trips.stops, args.needed, args.max_meters, args.radius_dll)
        journeys = connect(trips, docks, args.destination, time, args.change_seconds, args.max_transfers, args.arrive_by)
    except (ValueError, OSError, csv.Error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps({"eligibleDocks": len(docks), "journeys": journeys}, indent=2))


if __name__ == "__main__":
    main()
