#!/usr/bin/env python3
"""P08 reference solution: streaming algorithm design on market-data captures.

Run:  python3 solution.py          (after python3 make_data.py)
Tests: python3 -m unittest test_solution.py -v

Every algorithm is written as a plain function/class over iterables so it can be
unit-tested against a brute-force version (see test_solution.py). Only the
standard library is used in the algorithms; matplotlib is used for one figure.
"""
import heapq
import json
import time
from collections import Counter, defaultdict, deque
from operator import itemgetter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p08"
PLOTS = ROOT / "plots"
MS = 1_000_000
SEC = 1_000_000_000


def read_jsonl(path):
    """Generator: one dict per line, O(1) memory regardless of file size."""
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def ts_key(m):
    return m["ts_ns"]


# =========================================================================== #
# Part A - merging k streams
# =========================================================================== #
def merge_sorted_streams(streams, key=ts_key):
    """(i) k-way merge of individually SORTED streams. O(N log k) time, O(k) memory."""
    return heapq.merge(*streams, key=key)


def adjacent_inversions(stream, key=ts_key):
    """How many times the output goes backwards in time (0 for a correct merge)."""
    n, prev = 0, None
    for m in stream:
        k = key(m)
        if prev is not None and k < prev:
            n += 1
        prev = k
    return n


def reorder_bounded(stream, max_delay_ns, late, key=ts_key, stats=None):
    """(ii) Re-sort a stream whose disorder is bounded by max_delay_ns (Flink-style watermark).

    watermark W = max_ts_seen - D. Everything buffered with ts <= W can be emitted, because
    no on-time message can have ts < W any more. A message arriving with ts < W is LATE:
    emitting it would break the order, so it goes to the side output `late`.
    Time O(N log b), memory O(b) where b = msgs in the buffer ~ rate x D.
    """
    heap, seq, max_seen = [], 0, None
    max_buf = 0
    for m in stream:
        ts = key(m)
        if max_seen is not None and ts < max_seen - max_delay_ns:
            late.append(m)
            if stats is not None:
                stats.setdefault("lateness_ns", []).append(max_seen - ts)
            continue
        heapq.heappush(heap, (ts, seq, m))     # seq: stable tie-break, never compare dicts
        seq += 1
        if max_seen is None or ts > max_seen:
            max_seen = ts
        watermark = max_seen - max_delay_ns
        while heap and heap[0][0] <= watermark:
            yield heapq.heappop(heap)[2]
        max_buf = max(max_buf, len(heap))
    while heap:                                 # end of stream: flush
        yield heapq.heappop(heap)[2]
    if stats is not None:
        stats["max_buffer"] = max(stats.get("max_buffer", 0), max_buf)


def merge_ports(paths, max_delay_ns, late, stats=None):
    """Re-sort each port with its own watermark buffer, then k-way merge."""
    per_port = [reorder_bounded(read_jsonl(p), max_delay_ns, late, stats=stats) for p in paths]
    return merge_sorted_streams(per_port)


def part_a(paths):
    print("== Part A: merge 4 ports ==")
    t0 = time.perf_counter()
    naive_inv = adjacent_inversions(merge_sorted_streams([read_jsonl(p) for p in paths]))
    late, stats = [], {}
    merged = list(merge_ports(paths, 2 * MS, late, stats))
    dt = time.perf_counter() - t0
    print(f"(i) heapq.merge assuming sorted ports: {naive_inv:,} times the output goes backwards in time")
    print(f"(ii) watermark reorder (D = 2 ms) + merge: {len(merged):,} msgs, "
          f"inversions = {adjacent_inversions(merged)}, max buffer per port = {stats['max_buffer']} msgs")
    lat_ms = sorted(x / MS for x in stats.get("lateness_ns", []))
    print(f"    late events (side output): {len(late)}  by port: {dict(sorted(Counter(m['port'] for m in late).items()))}; "
          f"behind max_seen by {lat_ms[0]:.1f}..{lat_ms[-1]:.1f} ms")
    print(f"    both passes took {dt:.1f}s")
    return merged, late


# =========================================================================== #
# Part B - microbursts
# =========================================================================== #
def max_in_window(ts_sorted, window_ns):
    """Max #events in any half-open window (t - w, t]. Two pointers: O(N) time, O(1) memory."""
    best, best_end, left = 0, None, 0
    for right, t in enumerate(ts_sorted):
        while t - ts_sorted[left] >= window_ns:
            left += 1
        if right - left + 1 > best:
            best, best_end = right - left + 1, t
    return best, best_end


def bursts_above(ts_sorted, window_ns, threshold):
    """All maximal periods where some window holds > threshold events, merged.
    Returns [(start_ts, end_ts, peak_count)]. O(N)."""
    out, left = [], 0
    for right, t in enumerate(ts_sorted):
        while t - ts_sorted[left] >= window_ns:
            left += 1
        cnt = right - left + 1
        if cnt > threshold:
            start = ts_sorted[left]
            if out and start <= out[-1][1]:            # overlaps the previous burst -> extend
                s, _, peak = out[-1]
                out[-1] = (s, t, max(peak, cnt))
            else:
                out.append((start, t, cnt))
    return out


def part_b(merged, t0_ns):
    print("\n== Part B: microbursts ==")
    ts = [m["ts_ns"] for m in merged]
    best, end = max_in_window(ts, MS)
    avg_per_ms = len(ts) / ((ts[-1] - ts[0]) / MS)
    print(f"average rate: {avg_per_ms:.2f} msgs per ms;  max in any 1 ms window: {best} "
          f"(ending at t = {(end - t0_ns) / SEC:.4f} s)")
    bursts = bursts_above(ts, MS, 50)
    print(f"windows with > 50 msgs/ms, merged into {len(bursts)} bursts:")
    for s, e, peak in bursts:
        print(f"   t = {(s - t0_ns) / SEC:8.4f}s  duration {(e - s) / 1e3:7.1f} us  peak {peak}/ms")
    return ts


# =========================================================================== #
# Part C - order throttle
# =========================================================================== #
def find_violations(orders, limit=50, window_ns=SEC):
    """orders: list of (ts_ns, session, order_id) sorted by ts (ties in file order).
    An order violates if it is the (limit+1)-th or later order of its session in (t - 1s, t].
    Per-session two pointers: O(N) time, O(N) memory for the per-session lists."""
    by_session = defaultdict(list)
    for o in orders:
        by_session[o[1]].append(o)
    viol = {}
    for sess, lst in by_session.items():
        left, bad = 0, []
        for right, (t, _, oid) in enumerate(lst):
            while t - lst[left][0] >= window_ns:
                left += 1
            if right - left + 1 > limit:
                bad.append((t, oid, right - left + 1))
        viol[sess] = bad
    return viol


class SlidingWindowLimiter:
    """Exact 'at most `limit` accepted sends in any rolling window'.
    Keeps timestamps of accepted sends: allow() is O(1) amortized, memory O(limit)."""

    def __init__(self, limit, window_ns):
        self.limit, self.window = limit, window_ns
        self.sent = deque()

    def allow(self, t):
        while self.sent and t - self.sent[0] >= self.window:
            self.sent.popleft()
        if len(self.sent) < self.limit:
            self.sent.append(t)
            return True
        return False


class TokenBucket:
    """Classic token bucket: O(1) time and memory, but it bounds the LONG-RUN rate,
    not the count in every rolling window (burst = capacity + rate x window)."""

    def __init__(self, capacity, rate_per_s):
        self.capacity, self.rate = capacity, rate_per_s / SEC
        self.tokens, self.last = float(capacity), None

    def allow(self, t):
        if self.last is not None:
            self.tokens = min(self.capacity, self.tokens + (t - self.last) * self.rate)
        self.last = t
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def max_count_rolling(ts_sorted, window_ns=SEC):
    return max_in_window(ts_sorted, window_ns)[0] if ts_sorted else 0


def part_c(path, t0_ns):
    print("\n== Part C: throttle (<= 50 orders per rolling 1 s, per session) ==")
    orders = [(o["ts_ns"], o["session"], o["order_id"]) for o in read_jsonl(path)]
    viol = find_violations(orders)
    by_session = defaultdict(list)
    for t, s, _ in orders:
        by_session[s].append(t)
    for sess in sorted(by_session):
        bad = viol[sess]
        peak = max_count_rolling(by_session[sess])
        first = f"first = {bad[0][1]} at t = {(bad[0][0] - t0_ns) / SEC:.4f}s" if bad else "none"
        print(f"  {sess}: {len(by_session[sess]):5d} orders, peak in 1 s = {peak:3d}, violations = {len(bad):3d}; {first}")

    print("  replaying each session through limiters (rejected orders are not sent):")
    configs = [("sliding log (50 / 1s)", lambda: SlidingWindowLimiter(50, SEC)),
               ("token bucket cap=50, 50/s", lambda: TokenBucket(50, 50)),
               ("token bucket cap=25, 25/s", lambda: TokenBucket(25, 25))]
    for name, make in configs:
        rejected, worst = 0, 0
        for sess, ts in by_session.items():
            lim = make()
            accepted = [t for t in ts if lim.allow(t)]
            rejected += len(ts) - len(accepted)
            worst = max(worst, max_count_rolling(accepted))
        verdict = "OK" if worst <= 50 else "STILL VIOLATES"
        print(f"   {name:28s} rejected {rejected:3d}, worst accepted count in any 1 s = {worst:3d} -> {verdict}")


# =========================================================================== #
# Part D - rolling quantiles
# =========================================================================== #
class FenwickQuantile:
    """Counts of integer values 0..max_value in a Binary Indexed Tree.
    add: O(log B); kth smallest: O(log B) via binary lifting; memory O(B)."""

    def __init__(self, max_value):
        self.n = max_value + 1
        self.tree = [0] * (self.n + 1)
        self.top = 1 << (self.n.bit_length() - 1)   # highest power of two <= n

    def add(self, value, delta):
        i, n, tree = value + 1, self.n, self.tree
        while i <= n:
            tree[i] += delta
            i += i & -i

    def kth(self, k):
        """Smallest value v with count(<= v) >= k (k is 1-based)."""
        pos, step, tree, n = 0, self.top, self.tree, self.n
        while step:
            nxt = pos + step
            if nxt <= n and tree[nxt] < k:
                pos = nxt
                k -= tree[nxt]
            step >>= 1
        return pos        # index pos+1 in the tree == value pos


def nearest_rank(pct, n):
    """1-based rank for the pct-th percentile (nearest-rank, integer maths, no float surprises)."""
    return max(1, (pct * n + 99) // 100)


def rolling_quantiles_fenwick(values, window, pcts=(50, 99), max_value=32_767):
    fw, win = FenwickQuantile(max_value), deque()
    for v in values:
        v = min(v, max_value)                       # clamp the extreme tail into the last bucket
        fw.add(v, 1)
        win.append(v)
        if len(win) > window:
            fw.add(win.popleft(), -1)
        n = len(win)
        yield tuple(fw.kth(nearest_rank(p, n)) for p in pcts)


def rolling_quantiles_bruteforce(values, window, pcts=(50, 99)):
    win = deque()
    for v in values:
        win.append(v)
        if len(win) > window:
            win.popleft()
        s = sorted(win)                             # O(N log N) per message
        yield tuple(s[nearest_rank(p, len(s)) - 1] for p in pcts)


class SlidingMedian:
    """Two heaps + lazy deletion (LeetCode 480 style). Lower median.
    add/remove: O(log N) amortized; median: O(1)."""

    def __init__(self):
        self.small, self.large = [], []      # small = max-heap (negated) of the lower half
        self.n_small = self.n_large = 0      # live sizes (heaps may hold dead entries)
        self.dead = Counter()

    def _prune_small(self):
        while self.small and self.dead[-self.small[0]]:
            self.dead[-self.small[0]] -= 1
            heapq.heappop(self.small)

    def _prune_large(self):
        while self.large and self.dead[self.large[0]]:
            self.dead[self.large[0]] -= 1
            heapq.heappop(self.large)

    def _rebalance(self):                     # invariant: n_small == n_large or n_large + 1
        if self.n_small > self.n_large + 1:
            heapq.heappush(self.large, -heapq.heappop(self.small))
            self.n_small -= 1; self.n_large += 1
            self._prune_small()
        elif self.n_small < self.n_large:
            heapq.heappush(self.small, -heapq.heappop(self.large))
            self.n_large -= 1; self.n_small += 1
            self._prune_large()

    def add(self, v):
        if not self.small or v <= -self.small[0]:
            heapq.heappush(self.small, -v); self.n_small += 1
        else:
            heapq.heappush(self.large, v); self.n_large += 1
        self._rebalance()

    def remove(self, v):
        self.dead[v] += 1
        if v <= -self.small[0]:
            self.n_small -= 1
            if v == -self.small[0]:
                self._prune_small()
        else:
            self.n_large -= 1
            if self.large and v == self.large[0]:
                self._prune_large()
        self._rebalance()

    def median(self):
        return -self.small[0]


def rolling_median_heaps(values, window):
    sm, win = SlidingMedian(), deque()
    for v in values:
        sm.add(v)
        win.append(v)
        if len(win) > window:
            sm.remove(win.popleft())
        yield sm.median()


def part_d(merged, t0_ns):
    print("\n== Part D: rolling p50 / p99 of lat_us over the last 1000 messages ==")
    lat = [m["lat_us"] for m in merged]
    t = time.perf_counter()
    fw = list(rolling_quantiles_fenwick(lat, 1000))
    t_fw = time.perf_counter() - t

    t = time.perf_counter()
    med = list(rolling_median_heaps(lat, 1000))
    t_heap = time.perf_counter() - t
    assert med == [p50 for p50, _ in fw], "two-heap median disagrees with Fenwick p50"

    n_bf = 20_000
    t = time.perf_counter()
    bf = list(rolling_quantiles_bruteforce(lat[:n_bf], 1000))
    t_bf = time.perf_counter() - t
    assert bf == fw[:n_bf], "Fenwick disagrees with brute force"

    print(f"Fenwick (p50+p99): {len(lat) / t_fw:,.0f} msgs/s   two-heap median: {len(lat) / t_heap:,.0f} msgs/s   "
          f"brute-force sort: {n_bf / t_bf:,.0f} msgs/s  (all three agree)")

    # Where is latency degraded? A single rolling p99 over 1000 samples is noisy (it is set by
    # ~10 tail samples), so summarise per second and look for SUSTAINED elevation.
    per_sec = defaultdict(list)
    for m, (p50, p99) in zip(merged, fw):
        per_sec[(m["ts_ns"] - t0_ns) // SEC].append((p50, p99))
    sec_p50 = {s: sorted(v)[len(v) // 2][0] for s, v in per_sec.items()}
    sec_p99 = {s: sorted(x[1] for x in v)[len(v) // 2] for s, v in per_sec.items()}
    base50 = sorted(sec_p50.values())[len(sec_p50) // 2]
    base99 = sorted(sec_p99.values())[len(sec_p99) // 2]
    elevated = sorted(s for s, v in sec_p50.items() if v > 2 * base50)
    runs, start = [], None                   # contiguous runs of elevated seconds
    for i, s in enumerate(elevated):
        if start is None:
            start = s
        if i + 1 == len(elevated) or elevated[i + 1] != s + 1:
            runs.append((start, s))
            start = None
    spikes99 = sum(1 for s, v in sec_p99.items() if v > 2 * base99 and sec_p50[s] <= 2 * base50)
    print(f"typical rolling p50 = {base50} us, p99 = {base99} us (per-second medians)")
    print(f"sustained degradation (rolling p50 > 2x typical): {[(f'{a}s', f'{b + 1}s') for a, b in runs if b - a >= 2]}; "
          f"peak rolling p99 = {max(x[1] for x in fw)} us")
    print(f"isolated seconds where only p99 jumps (tail noise from ~10 samples): {spikes99}")
    return fw


# =========================================================================== #
# Part E - heavy hitters
# =========================================================================== #
class SlidingTopK:
    """Exact counts over the last `window_ns`: deque of (ts, sym) + Counter.
    add: O(1) amortized; top(k): O(S log k) with S distinct symbols in the window."""

    def __init__(self, window_ns):
        self.window, self.events, self.counts = window_ns, deque(), Counter()

    def add(self, ts, sym):
        self.events.append((ts, sym))
        self.counts[sym] += 1
        while self.events and ts - self.events[0][0] >= self.window:
            _, old = self.events.popleft()
            self.counts[old] -= 1
            if self.counts[old] == 0:
                del self.counts[old]

    def top(self, k):
        return heapq.nlargest(k, self.counts.items(), key=itemgetter(1))


def misra_gries(stream, k):
    """Keep at most k counters. Every item with true frequency > N/(k+1) survives,
    and estimate >= true - N/(k+1). O(k) memory, O(1) amortized per item."""
    counters = {}
    for x in stream:
        if x in counters:
            counters[x] += 1
        elif len(counters) < k:
            counters[x] = 1
        else:
            for key in list(counters):      # decrement-all step (amortized O(1): pays for earlier increments)
                counters[key] -= 1
                if counters[key] == 0:
                    del counters[key]
    return counters


def part_e(merged):
    print("\n== Part E: heavy hitters ==")
    exact = Counter(m["sym"] for m in merged)
    win = SlidingTopK(60 * SEC)
    for m in merged:
        win.add(m["ts_ns"], m["sym"])
    print(f"global top-5:        {exact.most_common(5)}")
    print(f"last-60s top-5:      {win.top(5)}")
    newcomers = [s for s, _ in win.top(5) if s not in dict(exact.most_common(5))]
    if newcomers:
        print(f"  -> in the last minute {newcomers} broke into the top 5 "
              f"(global rank of {newcomers[0]}: {[s for s, _ in exact.most_common()].index(newcomers[0]) + 1})")

    k = 20
    mg = misra_gries((m["sym"] for m in merged), k)
    n = len(merged)
    top_mg = sorted(mg.items(), key=itemgetter(1), reverse=True)[:5]
    max_err = max(exact[s] - c for s, c in mg.items())
    print(f"Misra-Gries k={k}: top-5 by estimate {[s for s, _ in top_mg]} "
          f"(exact top-5 {[s for s, _ in exact.most_common(5)]}); max undercount {max_err:,} <= N/(k+1) = {n // (k + 1):,}")


def plot(merged, fw, t0_ns):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    PLOTS.mkdir(parents=True, exist_ok=True)
    secs = [(m["ts_ns"] - t0_ns) / SEC for m in merged]
    per_ms = Counter((m["ts_ns"] - t0_ns) // MS for m in merged)
    xs = sorted(per_ms)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
    a1.plot(secs[::20], [x[1] for x in fw][::20], lw=0.8, label="rolling p99")
    a1.plot(secs[::20], [x[0] for x in fw][::20], lw=0.8, label="rolling p50")
    a1.set(ylabel="lat (us)", yscale="log", title="Rolling latency quantiles (last 1000 msgs) and per-ms message counts")
    a1.legend(loc="upper left")
    a2.plot([x / 1000 for x in xs], [per_ms[x] for x in xs], lw=0.6, color="tab:gray")
    a2.axhline(50, color="tab:red", ls="--", lw=0.8, label="50 msgs/ms")
    a2.set(xlabel="seconds since 14:30:00 UTC", ylabel="msgs per 1 ms bucket", yscale="log")
    a2.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(PLOTS / "p08_rolling_latency_and_bursts.png", dpi=120)
    plt.close(fig)


def main():
    t_start = time.perf_counter()
    paths = sorted(DATA.glob("port_*.jsonl"))
    merged, _late = part_a(paths)
    t0_ns = merged[0]["ts_ns"] // SEC * SEC
    part_b(merged, t0_ns)
    part_c(DATA / "orders.jsonl", t0_ns)
    fw = part_d(merged, t0_ns)
    part_e(merged)
    plot(merged, fw, t0_ns)
    print(f"\ntotal runtime {time.perf_counter() - t_start:.1f}s; plot -> {PLOTS / 'p08_rolling_latency_and_bursts.png'}")


if __name__ == "__main__":
    main()
