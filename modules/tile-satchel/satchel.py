"""Extract selected XYZ coordinates from one revision of a remote PMTiles v3 archive."""
import argparse
from collections import defaultdict
from functools import lru_cache
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
from zipfile import ZipFile

from pmtiles.tile import Compression, deserialize_directory, deserialize_header, find_tile, zxy_to_tileid


class DirectURL(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Ranges:
    def __init__(self, url):
        if urlsplit(url).scheme not in ("https", "http"):
            raise ValueError("Use the direct HTTP or HTTPS archive URL")
        self.url = url
        self.etag = None
        self.size = None
        self.requests = self.transferred = 0
        self.opener = build_opener(DirectURL())

    def read(self, offset, length):
        end = offset + length - 1
        if offset < 0 or length <= 0 or (self.size is not None and end >= self.size):
            raise ValueError("Range outside archive")
        headers = {"Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity"}
        if self.etag is not None:
            headers["If-Match"] = self.etag
        with self.opener.open(Request(self.url, headers=headers), timeout=15) as response:
            if response.status != 206 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Expected an unencoded 206 range response")
            tag = response.headers.get("ETag", "")
            if not re.fullmatch(r'"[!#-~\x80-\xff]*"', tag) or (self.etag is not None and tag != self.etag):
                raise ValueError("Missing, weak, or changed ETag")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if not match:
                raise ValueError("Missing or invalid Content-Range")
            start, stop, total = map(int, match.groups())
            if (start, stop) != (offset, end) or end >= total or (self.size is not None and total != self.size):
                raise ValueError("Content-Range does not match pinned archive")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != length:
                raise ValueError("Unexpected Content-Length")
            data = response.read(length + 1)
            if len(data) != length:
                raise ValueError("Range body length mismatch")
        self.etag, self.size = tag, total
        self.requests += 1
        self.transferred += length
        return data


def coordinate(text):
    parts = text.split("/")
    if len(parts) != 3 or any(not part.isascii() or not part.isdigit() for part in parts):
        raise ValueError(f"Expected z/x/y: {text!r}")
    z, x, y = map(int, parts)
    if z > 31 or max(x, y) >= 1 << z:
        raise ValueError(f"Coordinate outside zoom grid: {text!r}")
    return f"{z}/{x}/{y}", zxy_to_tileid(z, x, y)


def select(source, coordinates):
    raw_header = source.read(0, 127)
    if raw_header[:8] != b"PMTiles\x03":
        raise ValueError("Expected PMTiles version 3")
    header = deserialize_header(raw_header)
    if header["internal_compression"] != Compression.GZIP:
        raise ValueError("Expected gzip-compressed directories")

    @lru_cache(maxsize=256)
    def directory(offset, length):
        return deserialize_directory(source.read(offset, length))

    root = directory(header["root_offset"], header["root_length"])
    locations = defaultdict(list)
    missing = []
    for name, tile_id in coordinates:
        entries = root
        for _ in range(4):
            entry = find_tile(entries, tile_id)
            if entry is None:
                missing.append(name)
                break
            section = "tile_data" if entry.run_length else "leaf_directory"
            if entry.offset < 0 or entry.length <= 0 or entry.offset + entry.length > header[section + "_length"]:
                raise ValueError(f"Entry outside {section}")
            span = (header[section + "_offset"] + entry.offset, entry.length)
            if entry.run_length:
                locations[span].append(name)
                break
            entries = directory(*span)
        else:
            raise ValueError("Directory nesting exceeds PMTiles v3 maximum")
    return header, locations, missing


def extract(url, output, names):
    coords = list(dict.fromkeys(coordinate(value) for value in names))
    if not coords:
        raise ValueError("Provide at least one XYZ coordinate")
    source = Ranges(url)
    header, locations, missing = select(source, coords)
    output = Path(output)
    with NamedTemporaryFile(dir=output.parent, delete=False) as staged:
        staged_path = Path(staged.name)
    try:
        tiles = {}
        with ZipFile(staged_path, "w") as bundle:
            for span, addresses in locations.items():
                data = source.read(*span)
                digest = hashlib.sha256(data).hexdigest()
                for name in addresses:
                    bundle.writestr(name + ".tile", data)
                    tiles[name] = digest
            report = {"etag": source.etag, "archive_bytes": source.size, "range_requests": source.requests,
                      "response_body_bytes": source.transferred, "distinct_payloads": len(locations),
                      "tile_type": header["tile_type"].name, "tile_compression": header["tile_compression"].name,
                      "tiles": tiles, "missing": missing}
            bundle.writestr("manifest.json", json.dumps(report, indent=2))
        os.replace(staged_path, output)
    finally:
        staged_path.unlink(missing_ok=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("xyz", nargs="+")
    args = parser.parse_args()
    try:
        print(json.dumps(extract(args.url, args.output, args.xyz), indent=2))
    except (ValueError, OSError, EOFError, HTTPException) as error:
        parser.exit(2, f"{error}\n")
