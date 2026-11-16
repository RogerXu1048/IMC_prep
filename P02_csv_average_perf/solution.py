#!/usr/bin/env python3
"""P02 reference solution -- per-symbol average price, then a performance deep-dive.

Run:  python3 solution.py              (all parts, ~1-2 min)
      python3 solution.py --parts A,B  (subset)
      python3 solution.py --quick      (use the first 500k rows for everything)

Heavy libraries (pandas/numpy/matplotlib) are imported *inside* the functions that need
them: ProcessPoolExecutor on macOS uses 'spawn', which re-imports this __main__ module in
every worker -- top-level pandas imports would add ~0.5 s of start-up to every worker.
"""
from __future__ import annotations

import argparse
import contextlib
import cProfile
import csv
import io
import json
import logging
import math
import os
import pstats
import queue
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import timeit
import tracemalloc
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import csv_average as ca  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p02"
BIG = DATA / "ticks.csv"
PLOTS = ROOT / "plots"
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def timer(label: str, sink: dict | None = None):
    t0 = time.perf_counter()
    try:
        yield
    finally:                                   # runs even if the block raises
        dt = time.perf_counter() - t0
        if sink is not None:
            sink[label] = dt
        else:
            print(f"  [{label}] {dt:.3f}s")


@contextlib.contextmanager
def prefix_file(src: Path, n_lines: int):
    """Temporary file with the first n_lines of src; deleted on exit."""
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / f"prefix_{n_lines}.csv"
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            for i, line in enumerate(fin):
                if i >= n_lines:
                    break
                fout.write(line)
        yield dst


def oracle_check(acc: dict, expected: dict) -> str:
    exp_sum, exp_cnt = expected["sum_cents"], expected["counts"]
    bad = [s for s in exp_sum if acc.get(s) != [exp_sum[s], exp_cnt[s]]]
    extra = set(acc) - set(exp_sum)
    return "EXACT MATCH" if not bad and not extra else f"MISMATCH on {len(bad)} symbols, {len(extra)} extra"


def compare_floats(avgs: dict, expected: dict) -> str:
    exp = {s: expected["sum_cents"][s] / expected["counts"][s] / 100 for s in expected["sum_cents"]}
    common = set(avgs) & set(exp)
    max_err = max(abs(avgs[s] - exp[s]) for s in common)
    n_wrong = sum(abs(avgs[s] - exp[s]) > 1e-6 for s in common)
    missing = len(set(exp) - set(avgs)); extra = len(set(avgs) - set(exp))
    return (f"max |err|={max_err:.2e}, symbols off by >1e-6: {n_wrong}, "
            f"missing={missing}, extra={extra}")


# ---------------------------------------------------------------------------
# Part A -- pure Python streaming
# ---------------------------------------------------------------------------
def part_a(path: Path, expected: dict | None) -> dict:
    print("\n== Part A: pure-Python streaming (csv module, dict sym -> [sum_cents, count]) ==")
    logging.getLogger("csv_average").setLevel(logging.WARNING)   # show the first few bad rows
    with timer("pure python"):
        avgs, acc, stats = ca.average_by_symbol(path)
    logging.getLogger("csv_average").setLevel(logging.ERROR)
    print("  row stats:", dict(stats))
    print(f"  symbols: {len(avgs)}; sample:", {s: round(avgs[s], 4) for s in ["AAPL", "MSFT", "BKNG", "SPY"]})
    if expected:
        print("  vs independent oracle (integer cents):", oracle_check(acc, expected))
    return avgs


# ---------------------------------------------------------------------------
# Part B -- pandas: naive, optimised, and a subtle trap
# ---------------------------------------------------------------------------
def pandas_naive(path: Path):
    import warnings
    import pandas as pd
    with warnings.catch_warnings():
        # DtypeWarning: 'size' has mixed types because repeated header rows put the string
        # "size" into an int column -> pandas silently falls back to object dtype.
        warnings.simplefilter("ignore", pd.errors.DtypeWarning)
        df = pd.read_csv(path, on_bad_lines="skip")          # all columns, inferred dtypes
    price = pd.to_numeric(df["price"].astype(str).str.replace(",", "", regex=False), errors="coerce")
    sym = df["symbol"].astype(str).str.strip().str.upper()
    ok = price.gt(0) & df["symbol"].notna() & sym.ne("")
    return price[ok].groupby(sym[ok]).mean().to_dict()


def pandas_fast(path: Path, engine: str = "pyarrow"):
    """usecols + category + pyarrow engine + 'aggregate raw keys first, normalise later'."""
    import pandas as pd
    df = pd.read_csv(path, usecols=["symbol", "price"], engine=engine, on_bad_lines="skip",
                     dtype={"symbol": "category", "price": "str"})
    price = pd.to_numeric(df["price"].str.replace(",", "", regex=False), errors="coerce")
    price = price.where(price > 0)                            # <=0 -> NaN -> ignored by sum/count
    # groupby on ~510 raw categories (cheap integer codes), THEN normalise the 510 labels
    g = price.groupby(df["symbol"], observed=True).agg(["sum", "count"])
    g.index = g.index.astype(str).str.strip().str.upper()
    g = g[(g.index != "") & (g.index != "SYMBOL") & (g["count"] > 0)].groupby(level=0).sum()
    return (g["sum"] / g["count"]).to_dict()


def part_b(path: Path, expected: dict | None) -> None:
    print("\n== Part B: pandas versions (compared with the oracle) ==")
    for name, fn in [("pandas naive (C engine, all cols)", pandas_naive),
                     ("pandas fast (pyarrow, usecols, category)", pandas_fast),
                     ("pandas C engine + usecols  (TRAP)", lambda p: pandas_fast(p, engine="c"))]:
        t0 = time.perf_counter()
        avgs = fn(path)
        dt = time.perf_counter() - t0
        verdict = compare_floats(avgs, expected) if expected else ""
        print(f"  {name:<44}{dt:6.2f}s   {verdict}")
    print("  NOTE: with usecols the C engine no longer rejects rows that have an EXTRA column\n"
          "        (on_bad_lines only fires on the columns it parses) -> silently wrong averages.\n"
          "  NOTE: the naive read also raises a DtypeWarning: repeated header rows put the string\n"
          "        'size' into the size column -> object dtype. Always check df.dtypes.")


# ---------------------------------------------------------------------------
# Part C -- benchmark harness: time (repeats) + memory (fresh subprocess per impl)
# ---------------------------------------------------------------------------
IMPLS = {
    "pure_python": lambda p: ca.average_by_symbol(p)[0],
    "pandas_naive": pandas_naive,
    "pandas_fast": pandas_fast,
    "parallel_procs": lambda p: ca.average_by_symbol_parallel(p)[0],
}


def peak_rss_mb(who=resource.RUSAGE_SELF) -> float:
    r = resource.getrusage(who).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3     # bytes on macOS, KB on Linux


def run_mem_probe(impl: str, path: Path) -> str:
    """Child mode: run one impl in a fresh process; print peak RSS (MB) of this process and
    of the largest of its own children (ru_maxrss is per process -- workers are separate)."""
    if impl == "baseline_pandas":
        import pandas  # noqa: F401
    elif impl != "baseline":
        IMPLS[impl](path)
    return f"{peak_rss_mb():.1f} {peak_rss_mb(resource.RUSAGE_CHILDREN):.1f}"


def part_c(path: Path, repeats: int = 3) -> None:
    print("\n== Part C: benchmark (wall time: min/median of repeats; memory: peak RSS of a fresh process) ==")

    def child_rss(impl: str) -> tuple[float, float]:
        out = subprocess.run([sys.executable, __file__, "--mem", impl, "--path", str(path)],
                             capture_output=True, text=True, check=True)
        own, worker = out.stdout.strip().splitlines()[-1].split()
        return float(own), float(worker)

    base, base_pd = child_rss("baseline")[0], child_rss("baseline_pandas")[0]
    worker_peak = 0.0
    rows = []
    for name, fn in IMPLS.items():
        reps = 2 if name in ("pure_python", "pandas_naive") else repeats
        times = []
        for _ in range(reps):
            t0 = time.perf_counter()
            fn(path)
            times.append(time.perf_counter() - t0)
        rss, worker_rss = child_rss(name)
        if name == "parallel_procs":
            worker_peak = worker_rss
        rows.append((name, min(times), statistics.median(times), rss,
                     rss - (base_pd if name.startswith("pandas") else base)))
    print(f"  {'impl':<16}{'min s':>8}{'median s':>10}{'peak RSS MB':>13}{'Δ vs empty proc':>17}")
    for r in rows:
        print(f"  {r[0]:<16}{r[1]:>8.2f}{r[2]:>10.2f}{r[3]:>13.0f}{r[4]:>17.0f}")
    print(f"  (empty interpreter {base:.0f} MB; interpreter + pandas imported {base_pd:.0f} MB; "
          f"each parallel worker peaks at ~{worker_peak:.0f} MB -> total = parent + N x worker)")

    # tracemalloc sees Python-allocator memory only (numpy registers its buffers; the
    # pyarrow memory pool does NOT) -> it under-reports pandas+pyarrow. RSS doesn't lie.
    with prefix_file(path, 500_000) as small:
        for name in ["pure_python", "pandas_fast"]:
            tracemalloc.start()
            t0 = time.perf_counter()
            IMPLS[name](small)
            dt = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(f"  tracemalloc peak on 500k rows, {name:<12}: {peak / 1e6:7.1f} MB  "
                  f"(run took {dt:.2f}s under tracing)")


# ---------------------------------------------------------------------------
# Part D -- profiling + micro-benchmarks
# ---------------------------------------------------------------------------
def part_d(path: Path) -> None:
    print("\n== Part D: cProfile of the pure-Python version (500k rows), top 10 by tottime ==")
    with prefix_file(path, 500_000) as small:
        prof = cProfile.Profile()
        prof.enable()
        ca.average_by_symbol(small)
        prof.disable()
    s = io.StringIO()
    pstats.Stats(prof, stream=s).sort_stats("tottime").print_stats(10)
    print("\n".join(line for line in s.getvalue().splitlines()[4:24] if line.strip()))

    print("\n  micro-benchmarks (per call):")
    n = 200_000
    cases = {
        "float('231.45')": lambda: float("231.45"),
        "round(float(s)*100)": lambda: round(float("231.45") * 100),
        "int split cents": lambda: (lambda a, b: int(a) * 100 + int(b))(*"231.45".split(".")),
        "Decimal('231.45')": lambda: Decimal("231.45"),
        "parse_price_cents": lambda: ca.parse_price_cents("231.45"),
        "' aapl '.strip().upper()": lambda: " aapl ".strip().upper(),
    }
    for name, fn in cases.items():
        t = min(timeit.repeat(fn, number=n, repeat=3)) / n
        print(f"    {name:<28}{t * 1e9:7.0f} ns")
    line = "2026-11-12T14:30:00.005791,CMR,201.36,300\n"
    lines = [line] * n
    t_csv = min(timeit.repeat(lambda: list(csv.reader(lines)), number=1, repeat=3)) / n
    t_split = min(timeit.repeat(lambda: [l.rstrip("\n").split(",") for l in lines], number=1, repeat=3)) / n
    print(f"    {'csv.reader per line':<28}{t_csv * 1e9:7.0f} ns")
    print(f"    {'str.split per line':<28}{t_split * 1e9:7.0f} ns   "
          "(faster, but wrong for quoted fields like \"1,234.50\")")


# ---------------------------------------------------------------------------
# Part E -- concurrency: processes, scaling, Amdahl
# ---------------------------------------------------------------------------
def _noop(_):
    return None


def part_e(path: Path) -> None:
    print("\n== Part E: multiprocessing map-reduce over byte-range chunks ==")
    serial = min(timeit.repeat(lambda: ca.average_by_symbol(path), number=1, repeat=2))
    _, acc_serial, _ = ca.average_by_symbol(path)
    n_cpu = os.cpu_count() or 1
    print(f"  serial: {serial:.2f}s   cpu_count={n_cpu}")
    print(f"  {'workers':>8}{'cold s':>9}{'warm s':>9}{'speedup(warm)':>15}{'== serial':>11}")
    results = {}
    for w in sorted({1, 2, 4, 8, n_cpu}):
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=w) as pool:
            list(pool.map(_noop, range(w)))        # forces worker start-up
            startup = time.perf_counter() - t0
            t1 = time.perf_counter()
            _, acc, _ = ca.average_by_symbol_parallel(path, workers=w, executor=pool)
            warm = time.perf_counter() - t1
        results[w] = warm
        print(f"  {w:>8}{startup + warm:>9.2f}{warm:>9.2f}{serial / warm:>15.2f}{str(acc == acc_serial):>11}")
    # Amdahl: speedup(N) = 1 / (s + (1-s)/N)  ->  solve for serial fraction s from the max-N point
    n_max = max(results)
    sp = serial / results[n_max]
    s = (n_max / sp - 1) / (n_max - 1) if n_max > 1 else 1.0
    print(f"  Amdahl estimate from N={n_max}: serial fraction ≈ {s:.1%} "
          f"-> max possible speedup ≈ {1 / s:.0f}x" if s > 0 else "")
    print("  threads would NOT help here: parsing is CPU-bound Python bytecode and the GIL lets\n"
          "  one thread run it at a time (free-threaded 3.13t changes that; asyncio only helps I/O).")


# ---------------------------------------------------------------------------
# Part F -- run the unittest suite
# ---------------------------------------------------------------------------
def part_f() -> None:
    import unittest
    print("\n== Part F: tests (python3 -m unittest -v test_solution) ==")
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent), pattern="test_solution.py")
    res = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    print(f"  ran {res.testsRun} tests, failures={len(res.failures)}, errors={len(res.errors)}")


# ---------------------------------------------------------------------------
# Part G -- VWAP, per-minute averages, numerical accuracy
# ---------------------------------------------------------------------------
def part_g(path: Path) -> None:
    import numpy as np
    import pandas as pd
    print("\n== Part G: VWAP, per-minute average, floating-point accuracy ==")
    with timer("VWAP pure python"):
        vwap = ca.vwap_by_symbol(path)
    print("  VWAP sample:", {s: round(vwap[s], 4) for s in ["AAPL", "BKNG", "SPY"]})

    df = pd.read_csv(path, engine="pyarrow", on_bad_lines="skip", dtype=str)
    df["px"] = pd.to_numeric(df["price"].str.replace(",", "", regex=False), errors="coerce")
    df["sym"] = df["symbol"].str.strip().str.upper()
    df = df[(df.px > 0) & df.sym.notna() & (df.sym != "") & (df.sym != "SYMBOL")]
    # ISO strings: the first 16 chars ARE the minute -> no datetime parsing needed (much faster)
    df["minute"] = df["timestamp"].str.slice(0, 16)
    per_min = df.groupby(["sym", "minute"], observed=True)["px"].mean()
    aapl = per_min.loc["AAPL"]
    print(f"  per-minute AAPL averages: {len(aapl)} minutes, first 3:",
          {k: round(v, 3) for k, v in aapl.head(3).items()})
    PLOTS.mkdir(exist_ok=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(pd.to_datetime(aapl.index), aapl.values)
    ax.set_title("AAPL per-minute average price"); ax.set_ylabel("$")
    fig.tight_layout(); fig.savefig(PLOTS / "p02_aapl_per_minute.png", dpi=110); plt.close(fig)

    # accuracy: average of AAPL prices with different summation strategies
    px = df.loc[df.sym == "AAPL", "px"].to_numpy()
    exact = Decimal(int(np.round(px * 100).astype(np.int64).sum())) / Decimal(100 * len(px))
    naive = 0.0
    for v in px.tolist():
        naive += v                                 # sequential: error grows ~ O(n * eps)
    variants = {"naive += loop": naive / len(px),
                "builtin sum() (3.12: Neumaier)": sum(px.tolist()) / len(px),
                "math.fsum (exact rounding)": math.fsum(px.tolist()) / len(px),
                "np.sum (pairwise)": float(np.sum(px)) / len(px),
                "float32 np.sum": float(np.sum(px.astype(np.float32))) / len(px)}
    print(f"  AAPL n={len(px):,}: exact mean (integer cents) = {exact}")
    for k, v in variants.items():
        print(f"    {k:<32} error = {float(Decimal(v) - exact):+.3e}")


# ---------------------------------------------------------------------------
# Part H -- producer / consumer: handoff latency vs batching
# ---------------------------------------------------------------------------
def _run_pipeline(lines: list[str], batch_size: int) -> dict:
    """Producer parses CSV lines -> (sym, cents) and enqueues batches; consumer aggregates.
    Producer does the heavy work, so the queue is usually near-empty and we measure the
    *handoff* (wake-up + GIL acquisition), not time spent waiting behind a full queue."""
    q: queue.Queue = queue.Queue(maxsize=256)           # bounded -> back-pressure, bounded memory
    handoff_ns: list[int] = []                          # enqueue -> dequeue
    age_ns: list[int] = []                              # first row of batch parsed -> dequeue
    depth: list[int] = []
    acc: dict[str, list[int]] = {}

    def producer():
        batch, t_first = [], 0
        for row in csv.reader(lines):
            if len(row) != 4 or row[2] == "price":
                continue
            cents = ca.parse_price_cents(row[2])
            sym = row[1].strip().upper()
            if cents is None or not sym:
                continue
            if not batch:
                t_first = time.perf_counter_ns()
            batch.append((sym, cents))
            if len(batch) == batch_size:
                q.put((t_first, time.perf_counter_ns(), batch))
                batch = []
        if batch:
            q.put((t_first, time.perf_counter_ns(), batch))
        q.put(None)                                     # sentinel: end of stream

    def consumer():
        while (item := q.get()) is not None:
            now = time.perf_counter_ns()
            t_first, t_enq, batch = item
            handoff_ns.append(now - t_enq)
            age_ns.append(now - t_first)
            depth.append(q.qsize())
            for sym, cents in batch:
                e = acc.get(sym)
                if e is None:
                    acc[sym] = [cents, 1]
                else:
                    e[0] += cents
                    e[1] += 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=producer), threading.Thread(target=consumer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.perf_counter() - t0
    pct = lambda xs, q_: sorted(xs)[min(len(xs) - 1, int(q_ * len(xs)))] / 1e3
    return {"rows_per_s": len(lines) / dt, "handoff_p50": pct(handoff_ns, 0.5),
            "handoff_p99": pct(handoff_ns, 0.99), "age_p50": pct(age_ns, 0.5),
            "age_p99": pct(age_ns, 0.99), "max_depth": max(depth)}


def part_h(path: Path, n_rows: int = 300_000) -> None:
    print("\n== Part H: producer -> queue -> consumer; handoff latency vs batch size ==")
    with open(path, newline="") as f:
        lines = [line for _, line in zip(range(n_rows), f)]
    default_si = sys.getswitchinterval()
    print(f"  GIL switch interval = {default_si * 1e3:.0f} ms;  latencies in us")
    print(f"  {'batch':>6}{'switch':>8}{'rows/s':>12}{'handoff p50':>13}{'p99':>9}"
          f"{'oldest-row age p50':>20}{'p99':>9}{'max qdepth':>12}")
    configs = [(1, default_si), (16, default_si), (256, default_si), (4096, default_si),
               (1, 0.0001), (256, 0.0001)]
    try:
        for batch_size, si in configs:
            sys.setswitchinterval(si)
            r = _run_pipeline(lines, batch_size)
            print(f"  {batch_size:>6}{si * 1e3:>6.1f}ms{r['rows_per_s']:>12,.0f}{r['handoff_p50']:>13.1f}"
                  f"{r['handoff_p99']:>9.1f}{r['age_p50']:>20.1f}{r['age_p99']:>9.1f}{r['max_depth']:>12}")
    finally:
        sys.setswitchinterval(default_si)               # always restore global state
    print("  - batch=1: per-row put/get + wake-up costs more than the producer's parsing, so the\n"
          "    consumer is the bottleneck, the queue sits at maxsize and latency = queueing delay\n"
          "    (Little's law: wait ≈ depth / throughput).")
    print("  - batching raises throughput (fewer lock/wake-ups per row) but each row waits for its\n"
          "    batch to fill: 'oldest-row age' grows ~linearly with batch size.\n"
          "  - two Python threads share one GIL: a busy producer holds it until the switch interval\n"
          "    expires, so the consumer's wake-up (handoff) is bounded by that interval, not by the queue.")


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="A,B,C,D,E,F,G,H")
    ap.add_argument("--quick", action="store_true", help="use the first 500k rows")
    ap.add_argument("--mem", help=argparse.SUPPRESS)
    ap.add_argument("--path", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.mem:                                           # child mode for Part C
        print(run_mem_probe(args.mem, Path(args.path)))
        return

    expected = json.loads((DATA / "expected_ticks.json").read_text())
    parts = set(args.parts.upper().split(","))
    t0 = time.perf_counter()
    with contextlib.ExitStack() as stack:
        path = BIG
        if args.quick:
            path = stack.enter_context(prefix_file(BIG, 500_000))
            expected = None                                # oracle is for the full file
        print(f"input: {path} ({path.stat().st_size / 1e6:.0f} MB)")
        if "A" in parts: part_a(path, expected)
        if "B" in parts: part_b(path, expected)
        if "C" in parts: part_c(path)
        if "D" in parts: part_d(path)
        if "E" in parts: part_e(path)
        if "F" in parts: part_f()
        if "G" in parts: part_g(path)
        if "H" in parts: part_h(path)
    print(f"\ntotal runtime {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
