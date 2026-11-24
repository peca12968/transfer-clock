"""Load service-day trip cancellations and delay offsets."""

from dataclasses import replace
import json


def unique_fields(pairs, kind="service update"):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate {kind} field: {key}")
        result[key] = value
    return result


def delay(value):
    if type(value) is not int or value < 0:
        raise ValueError("delay_seconds must be a nonnegative integer")
    return value


def retime(calls, update):
    offset = delay(update.get("delay_seconds", 0))
    updates = update.get("stop_delays", [])
    if not isinstance(updates, list):
        raise ValueError("stop_delays must be an array")
    sequences = {call.sequence for call in calls}
    offsets = {}
    for item in updates:
        if not isinstance(item, dict) or set(item) != {"stop_sequence", "delay_seconds"}:
            raise ValueError("a stop delay requires stop_sequence and delay_seconds")
        sequence = item["stop_sequence"]
        if type(sequence) is not int or sequence not in sequences or sequence in offsets:
            raise ValueError(f"unknown or repeated stop_sequence: {sequence}")
        offsets[sequence] = delay(item["delay_seconds"])

    adjusted = []
    for call in calls:
        offset = offsets.get(call.sequence, offset)
        current = replace(call, arrival=call.arrival + offset, departure=call.departure + offset)
        if adjusted and current.arrival < adjusted[-1].departure:
            raise ValueError(f"delay makes stop_sequence {call.sequence} arrive before the previous departure")
        adjusted.append(current)
    return adjusted


def read_active_trips(trips, path):
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_fields)
    if not isinstance(data, dict) or set(data) != {"trips"} or not isinstance(data["trips"], list):
        raise ValueError("expected a trips array in the service update file")
    active = trips.copy()
    seen = set()
    for item in data["trips"]:
        if not isinstance(item, dict) or not isinstance(item.get("trip_id"), str):
            raise ValueError("each service update requires a trip_id")
        trip = item["trip_id"]
        if trip not in trips or trip in seen:
            raise ValueError(f"unknown or repeated trip_id: {trip}")
        seen.add(trip)
        fields = set(item) - {"trip_id"}
        if fields == {"cancelled"} and item["cancelled"] is True:
            del active[trip]
        elif fields and fields <= {"delay_seconds", "stop_delays"}:
            try:
                active[trip] = retime(trips[trip], item)
            except ValueError as error:
                raise ValueError(f"trip {trip}: {error}") from error
        else:
            raise ValueError(f"trip {trip}: provide a cancellation or delay fields")
    return active
