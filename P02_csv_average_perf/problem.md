# P02 — "Average Price per Stock": A Performance-Engineering Deep Dive

**Time box: ~60 min** (Parts A–C are the core; D–H are the discussion the interviewer will
steer into) · Python; pandas/numpy allowed; stdlib `unittest` for tests

## Context

This starts as the simplest possible task — and that is the point. A performance engineer is
judged less on *writing* the program than on how they prove it is correct, measure it, find
where the time and memory go, and make it faster without breaking it. The interviewer will
keep asking "how do you know?" and "what would you do next?".

The loop we expect you to follow:
**correctness proof → benchmark → micro-benchmark → change the implementation → repeat.**

## Data

Run `python3 make_data.py` first. It writes to `data/p02/`:

| file | contents |
|---|---|
| `ticks.csv` | ~3M rows, ~125 MB, one trading day of trades for ~500 symbols |
| `ticks_small.csv` | ~20 hand-written rows, including edge cases |
| `expected_small.json` | the exact expected result for `ticks_small.csv` (and the bad-row policy used to produce it) |
| `expected_ticks.json` | per-symbol `sum_cents` / `counts` for `ticks.csv` (an independent oracle for regression tests) |

Schema of the CSVs:

| column | type | meaning |
|---|---|---|
| `timestamp` | ISO-8601 string, µs | trade time (UTC) |
| `symbol` | string | ticker |
| `price` | decimal string, 2 dp | trade price in USD |
| `size` | int | shares traded |

The big file was produced by concatenating exports from several systems. Assume it is not
perfectly clean; inspect it before trusting it.

## Tasks

**Part A — Correct first (≈15 min).** Write `average_by_symbol(path)` in plain Python (no
pandas) that reads the CSV and returns the average price per symbol. Decide and *state*
your policy for bad rows (skip? fail? normalise?) and make the program report what it
skipped. What data structure holds the state, and how much memory does it need as a function
of the file size?

**Part B — pandas (≈10 min).** Write the same thing in pandas. Then make it fast (think about
which columns, which dtypes, which parser). Verify that every version produces the same
answer as Part A. Do they?

**Part C — Benchmark (≈10 min).** Build a small benchmark harness and produce a table: wall
time (several repeats — which statistic do you report?) and peak memory for each
implementation. Explain any surprises in the memory numbers. How do you measure the memory of
code that allocates outside the Python allocator?

**Part D — Where does the time go? (≈10 min).** Profile the pure-Python version. Which lines /
functions are hot? Micro-benchmark the candidates for the hot spot (e.g. different ways to
parse a price). What does the profiler itself cost, and what can it not see?

**Part E — Concurrency (≈10 min).** Make it use all cores. Explain your choice between
threads, processes and asyncio. Measure the speed-up for 1, 2, 4, 8, … workers and explain
the shape of the curve. Prove the parallel result equals the serial result.

**Part F — Tests.** Write `unittest` tests: unit tests for the parsing edge cases, an
end-to-end test on `ticks_small.csv`, a parallel == serial test, and a regression test on the
full file. Which test would have caught the bugs you found in Part B?

**Part G — Extensions.** (1) Volume-weighted average price (VWAP) per symbol.
(2) Average price per symbol per minute. (3) Your averages are sums of millions of floats:
how accurate are they? Compare several ways of summing.

**Part H — A consumer appears.** Now the parsed ticks must be handed to another component
(a consumer thread) as they are parsed. Implement producer → queue → consumer. Measure
throughput and *handoff latency* (enqueue → dequeue). How do batch size and the Python
runtime affect them?

## Follow-up questions the interviewer may ask

1. Which in-memory data structure did you pick and why? What changes if you need the median instead of the mean? Or 50,000 symbols? Or the data doesn't fit in memory?
2. What is the "modern" way to manage resources and handle errors in Python? (files, temp files, exceptions, cleanup)
3. Unit vs end-to-end vs regression tests: what does each one catch? Where does the "expected" answer come from?
4. For a trading system, which performance metrics would you report — latency? throughput? handoff latency? Which percentile?
5. How do you find hotspots in time *and* in memory allocations? Which tools, and how do they work under the hood (deterministic vs sampling vs hardware-trace profilers)?
6. Why don't threads speed this up? When would they? What does Amdahl's law predict for your measurements?
7. Your laptop has 10 cores — why isn't the speed-up 10×?
8. Why should prices be stored as integer cents (or ticks) rather than floats?
9. What would you do differently if this ran continuously on a live feed instead of a file?

## 考点（中文）

- **正确性优先**：明确 bad-row policy，独立的 oracle，pandas 的“优化版”可能悄悄变错（必须和 baseline 对答案）。
- **测量方法论**：benchmark 要 repeat、报 min/median；内存要区分 tracemalloc 和 RSS；profiler 的开销和盲区（sampling vs instrumentation vs hardware trace）。
- **Python 性能模型**：GIL、进程 vs 线程 vs asyncio、spawn 的启动成本、Amdahl、数据结构选择（dict of [sum,count] 是 O(#symbols) 内存）。
- **数值正确性**：float 累加误差、integer cents、`math.fsum`。
- **系统思维**：producer/consumer、batching 的吞吐 vs 延迟 tradeoff、handoff latency、back-pressure。
