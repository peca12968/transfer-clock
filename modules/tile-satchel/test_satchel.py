import hashlib
from http.client import HTTPException
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from zipfile import ZipFile

from pmtiles.reader import MemorySource, Reader
from pmtiles.tile import tileid_to_zxy
from pmtiles.writer import build_roots_leaves
from demo import archive, exercise, origin
from satchel import coordinate, extract


class SatchelTests(unittest.TestCase):
    def test_deduplicated_extraction(self):
        report = exercise()
        self.assertEqual((len(report["tiles"]), report["distinct_payloads"]), (3, 2))

    def test_leaf_directories_against_local_reference(self):
        with patch("pmtiles.writer.optimize_directories", side_effect=lambda entries, _: build_roots_leaves(entries, 20)):
            data = archive()
        reference = Reader(MemorySource(data))
        self.assertGreater(reference.header()["leaf_directory_length"], 0)
        with TemporaryDirectory() as temp, origin(data) as (url, log):
            output = Path(temp) / "area.zip"
            coords = [tileid_to_zxy(i) for i in (0, 1, 4, 19, 20, 24, 62, 82, 179, 180)]
            names = ["/".join(map(str, xyz)) for xyz in coords]
            report = extract(url, output, [*names, names[0], names[1]])
            with ZipFile(output) as result:
                for name, xyz in zip(names, coords):
                    expected = reference.get(*xyz)
                    if expected is None:
                        self.assertIn(name, report["missing"])
                    else:
                        self.assertEqual(result.read(name + ".tile"), expected)
                        self.assertEqual(report["tiles"][name], hashlib.sha256(expected).hexdigest())
            self.assertIsNone(log[0]["if_match"])
            self.assertTrue(all(r["if_match"] == report["etag"] for r in log[1:]))
            self.assertEqual(len(log), len({r["range"] for r in log}))

    def test_revision_change_during_payloads_keeps_published_file(self):
        data, updated = archive(), archive(2)
        with TemporaryDirectory() as temp:
            output = Path(temp) / "published.zip"
            output.write_bytes(b"prior publication")
            for fault in (None, "ignore-condition"):
                with origin(data, replacement=updated, switch_after=3, fault=fault) as (url, log):
                    with self.assertRaises((HTTPError, ValueError)):
                        extract(url, output, ["1/0/0", "1/1/1"])
                    self.assertEqual(log[-1]["status"], 412 if fault is None else 206)
                self.assertEqual(output.read_bytes(), b"prior publication")
                self.assertEqual(list(output.parent.iterdir()), [output])
            with origin(updated) as (url, _):
                new = extract(url, output, ["1/0/0"])
                self.assertEqual(new["etag"], '"' + hashlib.sha256(updated).hexdigest() + '"')

    def test_range_contract_and_cli(self):
        for name in ("-1/0/0", "1/2/0", "32/0/0", "a/b/c", "1/1"):
            with self.assertRaises(ValueError):
                coordinate(name)
        data = archive()
        with TemporaryDirectory() as temp:
            output = Path(temp) / "bundle.zip"
            for fault in ("weak", "whole-body", "range", "short"):
                with origin(data, fault=fault) as (url, _), self.assertRaises((ValueError, OSError, HTTPException)):
                    extract(url, output, ["1/0/0"])
                self.assertFalse(output.exists())
            with origin(data) as (url, _):
                subprocess.run([sys.executable, Path(__file__).with_name("satchel.py"), url, output, "1/1/0"],
                               check=True, capture_output=True)
                with ZipFile(output) as result:
                    self.assertEqual(result.namelist(), ["manifest.json"])
