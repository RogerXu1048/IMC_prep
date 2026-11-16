"""P02 tests -- run:  python3 -m unittest -v test_solution     (from this folder)

Layers:
  unit         parse_price_cents / aggregate_rows edge cases (fast, no files)
  chunking     read_chunk never loses or duplicates a line, for every split point
  e2e          ticks_small.csv vs hand-computed expected_small.json
  equivalence  parallel == serial (exact, integer cents)
  regression   full ticks.csv vs the independent oracle written by make_data.py
"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import csv_average as ca  # noqa: E402
import logging
logging.getLogger("csv_average").setLevel(logging.ERROR)   # keep test output clean

DATA = HERE.parent / "data" / "p02"


class TestParsePrice(unittest.TestCase):
    def test_valid(self):
        cases = {"231.45": 23145, "320.5": 32050, "322": 32200, " 1.01 ": 101,
                 "1,234.50": 123450, "0.29": 29, "0.01": 1}
        for text, cents in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ca.parse_price_cents(text), cents)

    def test_invalid(self):
        for text in ["", "   ", "N/A", "abc", "-1.00", "0", "0.00", "nan", "inf", "-inf"]:
            with self.subTest(text=text):
                self.assertIsNone(ca.parse_price_cents(text))


class TestAggregateRows(unittest.TestCase):
    def run_rows(self, rows):
        acc, stats = {}, Counter()
        ca.aggregate_rows(rows, acc, stats, max_logged=0)
        return acc, stats

    def test_normalises_symbol(self):
        acc, stats = self.run_rows([["t", " aapl ", "1.00", "1"], ["t", "AAPL", "3.00", "1"]])
        self.assertEqual(acc, {"AAPL": [400, 2]})
        self.assertEqual(stats["normalised_symbol"], 1)

    def test_rejects(self):
        rows = [[], ["timestamp", "symbol", "price", "size"], ["t", "X", "1", "1", "extra"],
                ["t", "X"], ["t", "", "1.00", "1"], ["t", "X", "N/A", "1"]]
        acc, stats = self.run_rows(rows)
        self.assertEqual(acc, {})
        self.assertEqual((stats["blank"], stats["header"], stats["bad_field_count"],
                          stats["bad_symbol"], stats["bad_price"]), (1, 1, 2, 1, 1))

    def test_empty_input(self):
        self.assertEqual(self.run_rows([]), ({}, Counter()))


class TestChunking(unittest.TestCase):
    def test_every_split_point(self):
        content = b"h,a,b,c\n1,AAA,1.00,1\n22,BB,2.00,2\n333,C,3.00,3\nlast,no,newline,x"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.csv"
            p.write_bytes(content)
            for split in range(len(content) + 1):
                with self.subTest(split=split):
                    a = ca.read_chunk(p, 0, split)
                    b = ca.read_chunk(p, split, len(content))
                    self.assertEqual(a + b, content)          # nothing lost, nothing doubled


class TestEndToEnd(unittest.TestCase):
    def test_small_file(self):
        expected = json.loads((DATA / "expected_small.json").read_text())
        avgs, acc, stats = ca.average_by_symbol(DATA / "ticks_small.csv")
        self.assertEqual(set(avgs), set(expected["averages"]))
        for sym, avg in expected["averages"].items():
            self.assertAlmostEqual(avgs[sym], avg, places=9)
            self.assertEqual(acc[sym], [expected["sum_cents"][sym], expected["counts"][sym]])
        self.assertEqual(stats["accepted"], expected["accepted_rows"])
        rejected = stats["bad_field_count"] + stats["bad_symbol"] + stats["bad_price"]
        self.assertEqual(rejected, expected["rejected_rows"])

    def test_parallel_equals_serial_small(self):
        # tiny chunks => many chunk boundaries inside the small file
        _, serial, _ = ca.average_by_symbol(DATA / "ticks_small.csv")
        _, par, _ = ca.average_by_symbol_parallel(DATA / "ticks_small.csv", workers=2, chunks_per_worker=20)
        self.assertEqual(par, serial)


@unittest.skipUnless((DATA / "ticks.csv").exists(), "run make_data.py first")
class TestRegressionBigFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads((DATA / "expected_ticks.json").read_text())
        _, cls.serial, cls.stats = ca.average_by_symbol(DATA / "ticks.csv")

    def test_matches_oracle(self):
        exp = {s: [self.expected["sum_cents"][s], self.expected["counts"][s]] for s in self.expected["counts"]}
        self.assertEqual(self.serial, exp)
        self.assertEqual(self.stats["accepted"], self.expected["accepted_rows"])

    def test_parallel_equals_serial(self):
        _, par, _ = ca.average_by_symbol_parallel(DATA / "ticks.csv", workers=os.cpu_count())
        self.assertEqual(par, self.serial)


if __name__ == "__main__":
    unittest.main()
