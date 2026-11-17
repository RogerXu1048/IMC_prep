#!/usr/bin/env python3
"""Brute-force cross-checks for every P08 algorithm.

Run from any directory:
    python3 -m unittest "<path>/test_solution.py" -v
or  python3 test_solution.py
"""
import heapq
import random
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solution as S  # noqa: E402


def brute_window_counts(ts, w):
    """count of events in (t - w, t] for each index, O(N^2)."""
    return [sum(1 for j in range(i + 1) if ts[i] - ts[j] < w) for i in range(len(ts))]


class TestMerge(unittest.TestCase):
    def test_heapq_merge_sorted_inputs(self):
        rng = random.Random(1)
        for _ in range(50):
            streams = [sorted(rng.randint(0, 100) for _ in range(rng.randint(0, 20))) for _ in range(rng.randint(1, 5))]
            out = list(S.merge_sorted_streams([[{"ts_ns": t} for t in s] for s in streams]))
            self.assertEqual([m["ts_ns"] for m in out], sorted(t for s in streams for t in s))

    def test_reorder_bounded_matches_definition(self):
        rng = random.Random(2)
        for _ in range(200):
            D = rng.randint(0, 10)
            true_ts = sorted(rng.randint(0, 200) for _ in range(rng.randint(0, 40)))
            # arrival = ts + jitter, occasionally a very late one
            arrivals = sorted(((t + (rng.randint(0, 40) if rng.random() < 0.1 else rng.randint(0, D)), i, t)
                               for i, t in enumerate(true_ts)))
            stream = [{"ts_ns": t, "id": i} for _, i, t in arrivals]
            late = []
            out = list(S.reorder_bounded(stream, D, late))
            # brute-force definition: late iff ts < max(ts seen before it) - D
            exp_late, exp_ok, mx = [], [], None
            for m in stream:
                if mx is not None and m["ts_ns"] < mx - D:
                    exp_late.append(m["id"])
                else:
                    exp_ok.append(m)
                    mx = m["ts_ns"] if mx is None else max(mx, m["ts_ns"])
            self.assertEqual([m["id"] for m in late], exp_late)
            self.assertEqual([m["ts_ns"] for m in out], sorted(m["ts_ns"] for m in exp_ok))
            self.assertEqual(sorted(m["id"] for m in out), sorted(m["id"] for m in exp_ok))


class TestBursts(unittest.TestCase):
    def test_max_in_window(self):
        rng = random.Random(3)
        for _ in range(200):
            ts = sorted(rng.randint(0, 100) for _ in range(rng.randint(1, 50)))
            w = rng.randint(1, 20)
            self.assertEqual(S.max_in_window(ts, w)[0], max(brute_window_counts(ts, w)))

    def test_bursts_above_covers_every_hot_window(self):
        rng = random.Random(4)
        for _ in range(200):
            ts = sorted(rng.randint(0, 200) for _ in range(rng.randint(1, 60)))
            w, thr = rng.randint(1, 15), rng.randint(1, 6)
            bursts = S.bursts_above(ts, w, thr)
            counts = brute_window_counts(ts, w)
            hot_ends = [ts[i] for i, c in enumerate(counts) if c > thr]
            for t in hot_ends:          # every hot window end lies inside a reported burst
                self.assertTrue(any(s <= t <= e for s, e, _ in bursts))
            for s, e, peak in bursts:   # bursts are disjoint, ordered, and really hot
                self.assertGreater(peak, thr)
            for (s1, e1, _), (s2, e2, _) in zip(bursts, bursts[1:]):
                self.assertLess(e1, s2)


class TestThrottle(unittest.TestCase):
    def test_find_violations(self):
        rng = random.Random(5)
        for _ in range(100):
            orders = sorted((rng.randint(0, 50), rng.choice("XY"), f"o{i}") for i in range(rng.randint(0, 60)))
            orders = [(t, s, f"o{i}") for i, (t, s, _) in enumerate(orders)]
            limit, w = rng.randint(1, 5), rng.randint(1, 10)
            got = S.find_violations(orders, limit, w)
            for sess in "XY":
                lst = [o for o in orders if o[1] == sess]
                exp = [o[2] for i, o in enumerate(lst)
                       if sum(1 for j in range(i + 1) if o[0] - lst[j][0] < w) > limit]
                self.assertEqual([oid for _, oid, _ in got.get(sess, [])], exp)

    def test_sliding_limiter_is_safe_and_greedy(self):
        rng = random.Random(6)
        for _ in range(100):
            ts = sorted(rng.randint(0, 100) for _ in range(rng.randint(0, 80)))
            limit, w = rng.randint(1, 6), rng.randint(1, 15)
            lim = S.SlidingWindowLimiter(limit, w)
            accepted = []
            for t in ts:
                ok = lim.allow(t)
                in_window = sum(1 for a in accepted if t - a < w)
                self.assertEqual(ok, in_window < limit)     # accept exactly when it is safe
                if ok:
                    accepted.append(t)
            if accepted:
                self.assertLessEqual(S.max_in_window(accepted, w)[0], limit)

    def test_token_bucket_long_run_rate(self):
        tb = S.TokenBucket(capacity=5, rate_per_s=10)
        sends = [i * 1_000_000 for i in range(10_000)]        # 1000/s for 10 s
        accepted = sum(tb.allow(t) for t in sends)
        self.assertLessEqual(accepted, 5 + 10 * 10 + 1)


class TestQuantiles(unittest.TestCase):
    def test_fenwick_kth(self):
        rng = random.Random(7)
        for _ in range(100):
            vals = [rng.randint(0, 63) for _ in range(rng.randint(1, 60))]
            fw = S.FenwickQuantile(63)
            for v in vals:
                fw.add(v, 1)
            s = sorted(vals)
            for k in range(1, len(vals) + 1):
                self.assertEqual(fw.kth(k), s[k - 1])

    def test_rolling_fenwick_vs_bruteforce(self):
        rng = random.Random(8)
        for _ in range(30):
            vals = [int(rng.lognormvariate(3, 1)) for _ in range(rng.randint(1, 300))]
            w = rng.randint(1, 50)
            got = list(S.rolling_quantiles_fenwick(vals, w, pcts=(1, 50, 90, 99, 100), max_value=10**6))
            exp = list(S.rolling_quantiles_bruteforce(vals, w, pcts=(1, 50, 90, 99, 100)))
            self.assertEqual(got, exp)

    def test_two_heap_median(self):
        rng = random.Random(9)
        for _ in range(100):
            vals = [rng.randint(0, 20) for _ in range(rng.randint(1, 200))]   # many duplicates on purpose
            w = rng.randint(1, 30)
            got = list(S.rolling_median_heaps(vals, w))
            exp = [p50 for (p50,) in S.rolling_quantiles_bruteforce(vals, w, pcts=(50,))]
            self.assertEqual(got, exp)


class TestHeavyHitters(unittest.TestCase):
    def test_sliding_topk_counts(self):
        rng = random.Random(10)
        for _ in range(50):
            events = sorted((rng.randint(0, 300), rng.choice("abcdef")) for _ in range(rng.randint(1, 100)))
            w = rng.randint(1, 60)
            tk = S.SlidingTopK(w)
            for i, (t, s) in enumerate(events):
                tk.add(t, s)
                # brute force: events added so far whose ts is within (t - w, t]
                brute = Counter(s2 for t2, s2 in events[: i + 1] if t - t2 < w)
                self.assertEqual(+tk.counts, +brute)
                k = rng.randint(1, 4)
                self.assertEqual(sorted(c for _, c in tk.top(k)), sorted(brute.values())[-k:])

    def test_misra_gries_guarantee(self):
        rng = random.Random(11)
        for _ in range(50):
            k = rng.randint(1, 8)
            stream = [min(int(rng.paretovariate(1.2)), 30) for _ in range(rng.randint(1, 500))]
            mg = S.misra_gries(stream, k)
            exact = Counter(stream)
            n = len(stream)
            self.assertLessEqual(len(mg), k)
            for x, c in exact.items():
                est = mg.get(x, 0)
                self.assertLessEqual(est, c)                    # never over-counts
                self.assertGreaterEqual(est, c - n / (k + 1))   # bounded under-count
                if c > n / (k + 1):
                    self.assertIn(x, mg)                        # true heavy hitters survive


if __name__ == "__main__":
    unittest.main(verbosity=2)
