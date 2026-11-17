"""Unit + integration + regression tests for the P07 pipeline (stdlib unittest, no pytest needed).

Run:  python3 -m unittest -v test_solution.py      (from this folder)
"""
import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import flaky_store as store
import solution as sol


def fills(n=3, port=0, date="2026-11-02", **overrides):
    df = pd.DataFrame({
        "trade_id": [f"T{date.replace('-', '')}-{port}-{i:06d}" for i in range(n)],
        "ts": pd.date_range(f"{date} 14:30", periods=n, freq="s", tz="UTC"),
        "symbol": ["AAPL"] * n,
        "side": ["B", "S"] * (n // 2) + ["B"] * (n % 2),
        "price": [100.0 + i for i in range(n)],
        "qty": [10] * n,
        "venue": ["XNAS"] * n,
        "port": [port] * n,
    })
    for k, v in overrides.items():
        df[k] = v
    return df


def to_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


class TestNormalizeAndValidate(unittest.TestCase):
    def test_v2_format_is_normalized(self):
        v2 = fills().rename(columns={"qty": "quantity"})
        v2["price"] = v2["price"].map(lambda x: f"{x:.2f}")
        v2["ts"] = v2["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        out = sol.normalize_schema(v2)
        self.assertEqual(list(out.columns), sol.CANONICAL)
        self.assertEqual(str(out.ts.dtype), "datetime64[ns, UTC]")
        self.assertEqual(out.price.tolist(), [100.0, 101.0, 102.0])
        pd.testing.assert_series_equal(out.ts, fills().ts.astype("datetime64[ns, UTC]"), check_names=False)

    def test_missing_column_raises(self):
        with self.assertRaises(sol.SchemaError):
            sol.normalize_schema(fills().drop(columns=["venue"]))

    def test_bad_rows_are_quarantined_with_reason(self):
        df = sol.normalize_schema(fills(4, price=[100.0, -1.0, 100.0, 100.0], qty=[10, 10, 0, 10],
                                        side=["B", "S", "B", "X"]))
        good, bad = sol.validate_rows(df)
        self.assertEqual(len(good), 1)
        self.assertEqual(sorted(bad.reason), ["bad_price", "bad_qty", "bad_side"])
        self.assertEqual(str(good.qty.dtype), "int64")


class TestBusinessLogic(unittest.TestCase):
    def test_dedupe_keeps_owner_port(self):
        a = fills(2, port=1)
        resent = a.iloc[[0]].assign(port=2)
        out, n = sol.dedupe(pd.concat([resent, a], ignore_index=True))
        self.assertEqual(n, 1)
        self.assertEqual(out.port.tolist(), [1, 1])

    def test_corrections_last_action_wins_and_unmatched_are_classified(self):
        t = fills(3, port=0).assign(date="2026-11-02")
        parts = {"date=2026-11-02/port=0/fills.parquet": sol.Partition("p", "2026-11-02", 0, status="OK"),
                 "date=2026-11-02/port=1/fills.parquet": sol.Partition("q", "2026-11-02", 1, status="CORRUPT")}
        corr = pd.DataFrame([
            {"seq": 1, "action": "CORRECT", "trade_id": "T20261102-0-000001", "new_price": 99.0},
            {"seq": 2, "action": "CORRECT", "trade_id": "T20261102-0-000002", "new_price": 50.0},
            {"seq": 3, "action": "BUST", "trade_id": "T20261102-0-000002", "new_price": None},  # later bust wins
            {"seq": 4, "action": "CORRECT", "trade_id": "T20261102-0-000001", "new_price": 99.0},  # duplicate msg
            {"seq": 5, "action": "BUST", "trade_id": "T20261102-1-000007", "new_price": None},    # partition corrupt
            {"seq": 6, "action": "BUST", "trade_id": "T20261102-0-999999", "new_price": None},    # unknown trade
            {"seq": 7, "action": "BUST", "trade_id": "T20261102-7-000001", "new_price": None},    # unknown partition
        ])
        out, unmatched, stats = sol.apply_corrections(t, corr, parts)
        self.assertEqual(out.trade_id.tolist(), ["T20261102-0-000000", "T20261102-0-000001"])
        self.assertEqual(out.price.tolist(), [100.0, 99.0])
        self.assertEqual(stats["duplicate_msgs"], 1)
        self.assertEqual(sorted(unmatched.reason), ["PENDING (CORRUPT)", "UNKNOWN_PARTITION", "UNKNOWN_TRADE"])
        out2, _, _ = sol.apply_corrections(out, corr, parts)          # idempotent
        pd.testing.assert_frame_equal(out.reset_index(drop=True), out2.reset_index(drop=True))


class TestRetries(unittest.TestCase):
    def setUp(self):
        self.no_sleep = mock.patch.object(sol, "backoff_delay", lambda attempt: 0)
        self.no_sleep.start()

    def tearDown(self):
        self.no_sleep.stop()

    def test_sync_retries_transient_then_succeeds(self):
        calls = iter([store.TransientStoreError("503"), TimeoutError(), b"ok"])

        def fake(root, rel):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(store, "fetch_bytes_sync", fake):
            self.assertEqual(sol.fetch_sync("root", "x"), (b"ok", 3))

    def test_not_found_is_not_retried(self):
        fake = mock.Mock(side_effect=store.NotFoundError("x"))
        with mock.patch.object(store, "fetch_bytes_sync", fake):
            with self.assertRaises(store.NotFoundError):
                sol.fetch_sync("root", "x")
        self.assertEqual(fake.call_count, 1)

    def test_async_gives_up_after_max_attempts(self):
        async def always_fail(root, rel):
            raise store.TransientStoreError("503")
        with mock.patch.object(store, "fetch_bytes", always_fail):
            with self.assertRaises(store.TransientStoreError):
                asyncio.run(sol.fetch_async("root", "x", asyncio.Semaphore(1), max_attempts=3))


class TestAtomicWrite(unittest.TestCase):
    def test_no_partial_file_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            final = Path(d) / "out.txt"
            with self.assertRaises(RuntimeError):
                with sol.atomic_path(final) as tmp:
                    tmp.write_text("half")
                    raise RuntimeError("crash mid-write")
            self.assertFalse(final.exists())
            self.assertEqual(list(Path(d).iterdir()), [])


class TestEndToEnd(unittest.TestCase):
    """Integration test on a tiny lake: 1 good v1 partition, 1 v2 partition, 1 missing, 1 corrupt."""

    def test_pipeline_on_tiny_lake(self):
        store.configure(seed=1, fail_rate=0.0, timeout_rate=0.0, min_latency=0.0, max_latency=0.0)
        try:
            with tempfile.TemporaryDirectory() as d:
                lake, cache = Path(d) / "lake", Path(d) / "cache"
                p0 = fills(4, port=0)
                v2 = fills(2, port=1).rename(columns={"qty": "quantity"})
                v2["price"] = ["200.00", "202.00"]
                files = {"date=2026-11-02/port=0/fills.parquet": to_bytes(p0),
                         "date=2026-11-02/port=1/fills.parquet": to_bytes(v2),
                         "date=2026-11-02/port=3/fills.parquet": to_bytes(fills(2, port=3))[:50]}
                manifest = {"files": [{"path": k, "rows": 4, "sha256": None} for k in files] +
                            [{"path": "date=2026-11-02/port=2/fills.parquet", "rows": 5, "sha256": None}]}
                manifest["files"][1]["rows"] = 2
                for rel, raw in files.items():
                    (lake / rel).parent.mkdir(parents=True, exist_ok=True)
                    (lake / rel).write_bytes(raw)

                parts, raw = asyncio.run(sol.run_pipeline_async(lake, manifest, cache))
                status = {p.port: p.status for p in parts.values()}
                self.assertEqual(status, {0: "OK", 1: "OK", 2: "MISSING", 3: "CORRUPT"})
                good, _ = sol.validate_rows(raw)
                summary = sol.daily_summary(good, parts)
                row = summary.iloc[0]
                self.assertEqual(row.n_trades, 6)
                self.assertEqual(row.volume, 60)
                self.assertAlmostEqual(row.vwap, (100 + 101 + 102 + 103 + 200 + 202) / 6, places=4)
                self.assertFalse(row.complete)          # port 2 missing, port 3 corrupt

                parts2, raw2 = asyncio.run(sol.run_pipeline_async(lake, manifest, cache))
                self.assertEqual(sum(p.from_cache for p in parts2.values()), 2)
                pd.testing.assert_frame_equal(raw.sort_values("trade_id").reset_index(drop=True),
                                              raw2.sort_values("trade_id").reset_index(drop=True))
        finally:
            store.configure()


class TestRegression(unittest.TestCase):
    def test_summary_matches_golden(self):
        if not sol.GOLDEN.exists():
            self.skipTest("no golden file yet: run `python3 solution.py --bless` once and review it")
        current = sol.RESULTS / "daily_summary.parquet"
        if not current.exists():
            self.skipTest("run solution.py first")
        now = pd.read_csv(io.StringIO(pd.read_parquet(current).to_csv(index=False)))
        gold = pd.read_csv(sol.GOLDEN)
        pd.testing.assert_frame_equal(now, gold)


if __name__ == "__main__":
    unittest.main(verbosity=2)
