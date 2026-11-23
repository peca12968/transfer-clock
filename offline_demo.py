"""Search near the antimeridian, then download the reported tile window over HTTP."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer


def exercise(satchel):
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(satchel))
    from demo import image, origin

    result, = json.loads(subprocess.check_output(["dotnet", root / "bin/Release/net10.0/ReturnRadius.dll",
                                                  root / "examples/dateline.json"]))
    assert [m["id"] for m in result["matches"]] == ["pier", "island"]
    area = result["map"]
    z = area["zoom"]
    names = [f"{z}/{x % (1 << z)}/{y}" for y in range(area["firstRow"], area["firstRow"] + area["rows"])
             for x in range(area["firstColumn"], area["firstColumn"] + area["columns"])]
    expected = {f"16/{x}/{y}": image(501 + i) for i, (x, y) in
                enumerate([(65535, 32767), (0, 32767), (65535, 32768), (0, 32768)])}
    assert set(names) == set(expected) and len(names) == area["tileCount"] == 4
    stream = io.BytesIO()
    writer = Writer(stream)
    for tile_id in range(64):
        writer.write_tile(tile_id, image(tile_id))
    for name, data in expected.items():
        writer.write_tile(zxy_to_tileid(*map(int, name.split("/"))), data)
    writer.finalize({"tile_type": TileType.PNG, "tile_compression": Compression.NONE}, {"name": "dateline"})
    with TemporaryDirectory() as temp, origin(stream.getvalue()) as (url, log):
        bundle = Path(temp) / "crossing.zip"
        report = json.loads(subprocess.check_output([sys.executable, satchel / "satchel.py", url, bundle, *names]))
        with ZipFile(bundle) as output:
            for name, data in expected.items():
                assert output.read(name + ".tile") == data
        assert report["missing"] == [] and report["range_requests"] == len(log) == 6
        return {"map": area, "stations": result["matches"], "archive_bytes": stream.tell(),
                "response_body_bytes": sum(r["bytes"] for r in log), "requests": len(log)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tile_satchel", type=Path)
    print(json.dumps(exercise(parser.parse_args().tile_satchel.resolve()), indent=2))
