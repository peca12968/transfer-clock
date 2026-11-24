"""Generate an actual PNG tile archive and serve it over loopback HTTP."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import random
import re
import struct
from tempfile import TemporaryDirectory
from threading import Thread
from zipfile import ZipFile
import zlib

from pmtiles.tile import Compression, TileType
from pmtiles.writer import Writer
from satchel import extract


def image(seed):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = random.Random(seed)
    raster = b"".join(b"\0" + pixels.randbytes(48) for _ in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raster)) + chunk(b"IEND", b""))


def archive(revision=1):
    stream = io.BytesIO()
    writer = Writer(stream)
    for tile_id in [1, 2, 3, *range(20, 180)]:
        seed = 1 if tile_id == 2 else tile_id
        writer.write_tile(tile_id, image(revision * 1000 + seed))
    writer.finalize({"tile_type": TileType.PNG, "tile_compression": Compression.NONE}, {"name": "sample"})
    return stream.getvalue()


@contextmanager
def origin(data, *, switch_after=None, replacement=None, fault=None):
    log = []
    versions = [(data, '"' + hashlib.sha256(data).hexdigest() + '"')]
    if replacement is not None:
        versions.append((replacement, '"' + hashlib.sha256(replacement).hexdigest() + '"'))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            changed = switch_after is not None and len(log) >= switch_after
            content, tag = versions[int(changed)]
            requested = self.headers.get("Range", "")
            condition = self.headers.get("If-Match")
            row = {"range": requested, "if_match": condition, "bytes": 0}
            log.append(row)
            if condition and condition != tag and fault != "ignore-condition":
                row["status"] = 412
                self.send_error(412)
                return
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            ranged = match and self.headers.get("If-Range", tag) == tag and fault != "whole-body"
            if ranged:
                start = int(match[1])
                end = min(int(match[2]) if match[2] else len(content) - 1, len(content) - 1)
                if start > end:
                    row["status"] = 416
                    self.send_error(416)
                    return
                body = content[start:end + 1]
                status = 206
            else:
                body, status = content, 200
            row.update(status=status, bytes=len(body))
            self.send_response(status)
            self.send_header("ETag", ("W/" if fault == "weak" else "") + tag)
            if ranged:
                self.send_header("Content-Range", f"bytes {start + (fault == 'range')}-{end}/{len(content)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body[:-1] if fault == "short" else body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/sample.pmtiles", log
        finally:
            server.shutdown()
            thread.join()


def exercise():
    data = archive()
    with TemporaryDirectory() as temp, origin(data) as (url, log):
        destination = Path(temp) / "selection.zip"
        report = extract(url, destination, ["1/0/0", "1/0/1", "1/1/1", "1/1/0"])
        assert report["response_body_bytes"] == sum(r["bytes"] for r in log) < len(data)
        assert report["range_requests"] == len(log) == 4
        with ZipFile(destination) as result:
            assert result.read("1/0/0.tile") == result.read("1/0/1.tile") == image(1001)
            assert result.read("1/1/1.tile") == image(1003)
            assert json.loads(result.read("manifest.json")) == report
            assert report["missing"] == ["1/1/0"]
        return report


if __name__ == "__main__":
    print(json.dumps(exercise(), indent=2))
