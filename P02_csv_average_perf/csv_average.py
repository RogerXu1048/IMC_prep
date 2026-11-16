"""Core, stdlib-only implementation for P02 (imported by solution.py and test_solution.py).

Kept free of pandas/numpy on purpose: worker processes started with the 'spawn' method
re-import modules, so a light module = cheap worker start-up.

Bad-row policy (decide it explicitly and write it down -- interviewers care):
  * blank line / repeated header          -> skip, counted separately (not "bad")
  * wrong number of fields                -> reject
  * empty symbol                          -> reject
  * missing / non-numeric / <= 0 price    -> reject
  * " aapl " -> "AAPL"; "1,234.50" -> 1234.50   (normalise, accept, count)
Prices are accumulated as integer cents: exact, and associative (parallel == serial).
"""
from __future__ import annotations

import csv
import io
import logging
import math
import os
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from typing import Iterable

log = logging.getLogger("csv_average")
HEADER = ["timestamp", "symbol", "price", "size"]

# symbol -> [sum_cents, count]   (a list is mutable in place: one dict lookup per row)
Acc = dict[str, list[int]]


def parse_price_cents(text: str) -> int | None:
    """'101.5' -> 10150, '1,234.50' -> 123450, bad/<=0 -> None."""
    text = text.strip()
    if "," in text:
        text = text.replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not (value > 0 and math.isfinite(value)):
        return None
    return round(value * 100)        # round() fixes 0.29*100 = 28.999999999999996


def aggregate_rows(rows: Iterable[list[str]], acc: Acc, stats: Counter,
                   max_logged: int = 3) -> None:
    """Hot loop. Updates acc/stats in place. O(1) work and O(1) extra memory per row."""
    for row in rows:
        if not row:
            stats["blank"] += 1
            continue
        if len(row) != 4:
            stats["bad_field_count"] += 1
            if stats["bad_field_count"] <= max_logged:
                log.warning("bad field count %d: %r", len(row), row)
            continue
        _ts, raw_sym, raw_price, _size = row
        if raw_price == "price" and raw_sym == "symbol":
            stats["header"] += 1
            continue
        sym = raw_sym.strip().upper()
        if not sym:
            stats["bad_symbol"] += 1
            continue
        cents = parse_price_cents(raw_price)
        if cents is None:
            stats["bad_price"] += 1
            if stats["bad_price"] <= max_logged:
                log.warning("bad price %r in row %r", raw_price, row)
            continue
        if sym != raw_sym:
            stats["normalised_symbol"] += 1
        entry = acc.get(sym)
        if entry is None:
            acc[sym] = [cents, 1]
        else:
            entry[0] += cents
            entry[1] += 1
        stats["accepted"] += 1


def averages_from_acc(acc: Acc) -> dict[str, float]:
    return {sym: s / n / 100 for sym, (s, n) in sorted(acc.items())}


def average_by_symbol(path: str | os.PathLike) -> tuple[dict[str, float], Acc, Counter]:
    """Part A: single-threaded streaming version. Memory O(#symbols), not O(#rows)."""
    acc: Acc = {}
    stats: Counter = Counter()
    with open(path, newline="", encoding="utf-8") as f:   # context manager closes on error too
        aggregate_rows(csv.reader(f), acc, stats)
    return averages_from_acc(acc), acc, stats


# ---------------------------------------------------------------------------
# Part E: multi-process map-reduce over byte ranges
# ---------------------------------------------------------------------------
def chunk_offsets(path: str | os.PathLike, n_chunks: int) -> list[tuple[int, int]]:
    size = os.path.getsize(path)
    step = max(1, size // n_chunks)
    starts = list(range(0, size, step))
    return [(s, min(s + step, size)) for s in starts]


def read_chunk(path: str | os.PathLike, start: int, end: int) -> bytes:
    """Return the bytes of every line that STARTS in [start, end).

    A line that straddles `start` belongs to the previous chunk; a line that straddles
    `end` belongs to us. NOTE: only valid if quoted fields never contain newlines.
    """
    with open(path, "rb") as f:
        if start > 0:
            f.seek(start - 1)
            if f.read(1) != b"\n":       # we landed mid-line: skip the rest of it
                f.readline()
        begin = f.tell()
        if begin >= end:
            return b""
        data = f.read(end - begin)
        if data and not data.endswith(b"\n"):
            data += f.readline()         # finish the line that started inside our range
    return data


def _worker(args: tuple[str, int, int]) -> tuple[Acc, Counter]:
    path, start, end = args
    acc: Acc = {}
    stats: Counter = Counter()
    text = read_chunk(path, start, end).decode("utf-8")
    aggregate_rows(csv.reader(io.StringIO(text, newline="")), acc, stats, max_logged=0)
    return acc, stats


def merge(parts: Iterable[tuple[Acc, Counter]]) -> tuple[Acc, Counter]:
    """Reduce step. Sum of ints is associative/commutative => order doesn't matter."""
    total: Acc = {}
    stats: Counter = Counter()
    for acc, st in parts:
        stats.update(st)
        for sym, (s, n) in acc.items():
            entry = total.get(sym)
            if entry is None:
                total[sym] = [s, n]
            else:
                entry[0] += s
                entry[1] += n
    return total, stats


def average_by_symbol_parallel(path: str | os.PathLike, workers: int | None = None,
                               chunks_per_worker: int = 4, executor=None
                               ) -> tuple[dict[str, float], Acc, Counter]:
    workers = workers or os.cpu_count() or 1
    jobs = [(str(path), s, e) for s, e in chunk_offsets(path, workers * chunks_per_worker)]
    if executor is not None:                       # reuse a warm pool (benchmarks)
        acc, stats = merge(executor.map(_worker, jobs))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            acc, stats = merge(pool.map(_worker, jobs))
    return averages_from_acc(acc), acc, stats


# ---------------------------------------------------------------------------
# Part G helper: VWAP with integer arithmetic
# ---------------------------------------------------------------------------
def vwap_by_symbol(path: str | os.PathLike) -> dict[str, float]:
    """sum(price*size)/sum(size); rows with a bad size are skipped for VWAP."""
    notional: dict[str, int] = {}
    volume: dict[str, int] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) != 4 or row[2] == "price":
                continue
            sym = row[1].strip().upper()
            cents = parse_price_cents(row[2])
            if not sym or cents is None:
                continue
            try:
                size = int(row[3])
            except ValueError:
                continue
            if size <= 0:
                continue
            notional[sym] = notional.get(sym, 0) + cents * size
            volume[sym] = volume.get(sym, 0) + size
    return {s: notional[s] / volume[s] / 100 for s in sorted(notional)}
