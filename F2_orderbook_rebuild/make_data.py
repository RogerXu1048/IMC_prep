#!/usr/bin/env python3
"""Generate a synthetic L2 market-data feed (JSON lines) for P03.

A TRUE book is simulated in integer ticks; every change is published as an
absolute-size level update. The delivered file is then made realistic:
duplicates, small re-ordering, one malformed line, one dropped run of seqs,
heartbeats without seq, and float prices with representation noise.

Writes:
  data/p03/feed.jsonl          the feed you must process
  data/p03/truth_bbo.parquet   true BBO after every seq (self-check only)
  data/p03/truth_trades.parquet true aggressor side of every trade (self-check only)
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 20261112
OUT = Path(__file__).resolve().parent.parent / "data" / "p03"
START_NS = int(pd.Timestamp("2026-11-12 14:30:00", tz="UTC").value)
DURATION_S = 30 * 60
SNAP_EVERY = 5000          # snapshot at seq 1, 5001, 10001, ...
MAX_LEVELS = 50
MIN_LEVELS = 8

rng = np.random.default_rng(SEED)


# --------------------------------------------------------------------------- #
# 1) event times: regime-switching Poisson (calm / busy)
# --------------------------------------------------------------------------- #
def event_times():
    times, regimes = [], []
    t, busy = 0.0, False
    regime_end = rng.exponential(25.0)
    while t < DURATION_S:
        rate = 300.0 if busy else 50.0
        t += rng.exponential(1.0 / rate)
        if t >= regime_end:
            busy = not busy
            regime_end = t + rng.exponential(8.0 if busy else 25.0)
        times.append(t)
        regimes.append(busy)
    return np.array(times[:-1]), np.array(regimes[:-1])


# --------------------------------------------------------------------------- #
# 2) the true book
# --------------------------------------------------------------------------- #
bids, asks = {}, {}
for i in range(30):
    bids[9999 - i] = 100 * int(rng.integers(1, 20))
    asks[10001 + i] = 100 * int(rng.integers(1, 20))

messages = []          # dicts in true seq order (without seq yet)
truth_rows = []        # (seq, ts, bid, bid_qty, ask, ask_qty)
truth_trades = []      # (seq, aggressor)
next_seq = 1


def best_bid():
    return max(bids)


def best_ask():
    return min(asks)


def snapshot_msg(ts):
    return {
        "type": "snapshot",
        "ts": ts,
        "bids": [[t, bids[t]] for t in sorted(bids, reverse=True)],
        "asks": [[t, asks[t]] for t in sorted(asks)],
    }


def record_truth(seq, ts):
    bb, ba = best_bid(), best_ask()
    truth_rows.append((seq, ts, bb, bids[bb], ba, asks[ba]))


def maybe_snapshot(ts):
    """Publish a snapshot when due. Must run BEFORE the book is mutated, so that
    snapshot seq S == book after all messages with seq < S."""
    global next_seq
    if next_seq % SNAP_EVERY == 1:
        snap = snapshot_msg(ts)
        snap["seq"] = next_seq
        messages.append(snap)
        record_truth(next_seq, ts)
        next_seq += 1


def emit(msg):
    """Assign the next seq (inserting a snapshot first when it is due)."""
    global next_seq
    maybe_snapshot(msg["ts"])
    msg["seq"] = next_seq
    messages.append(msg)
    next_seq += 1
    return msg["seq"]


def set_level(side, tick, qty, ts):
    maybe_snapshot(ts)
    book = bids if side == "B" else asks
    if qty <= 0:
        book.pop(tick, None)
        qty = 0
    else:
        book[tick] = qty
    seq = emit({"type": "update", "ts": ts, "side": side, "px": tick, "qty": qty})
    record_truth(seq, ts)


def refill_and_cap(ts):
    for side, book in (("B", bids), ("A", asks)):
        while len(book) < MIN_LEVELS:
            deep = (min(book) - 1 - int(rng.integers(0, 3))) if side == "B" else (max(book) + 1 + int(rng.integers(0, 3)))
            set_level(side, deep, 100 * int(rng.integers(2, 15)), ts)
        while len(book) > MAX_LEVELS:
            deep = min(book) if side == "B" else max(book)
            set_level(side, deep, 0, ts)


def simulate():
    times, regimes = event_times()
    for t, busy in zip(times, regimes):
        ts = START_NS + int(t * 1e9)
        bb, ba = best_bid(), best_ask()
        spread = ba - bb
        # liquidity providers step in quickly when the spread is wide
        refill_speed = 0.12 if busy else 0.3   # makers are slower to refill when the tape is busy
        if spread > 1 and rng.random() < min(0.85, refill_speed * (spread - 1)):
            side = "B" if rng.random() < 0.5 else "A"
            k = min(int(rng.geometric(0.6)), spread - 1)
            set_level(side, bb + k if side == "B" else ba - k, 100 * int(rng.integers(1, 6)), ts)
            refill_and_cap(ts + 100)
            continue

        u = rng.random()
        p_add, p_cancel, p_trade = (0.38, 0.35, 0.22) if busy else (0.47, 0.38, 0.08)

        if u < p_add:  # add liquidity at/behind own touch
            side = "B" if rng.random() < 0.5 else "A"
            off = min(int(rng.geometric(0.3)) - 1, 15)
            tick = bb - off if side == "B" else ba + off
            book = bids if side == "B" else asks
            set_level(side, tick, book.get(tick, 0) + 100 * int(rng.integers(1, 6)), ts)

        elif u < p_add + p_cancel:  # cancel part / all of a level near the touch
            side = "B" if rng.random() < 0.5 else "A"
            book = bids if side == "B" else asks
            levels = sorted(book, reverse=(side == "B"))[:10]
            tick = levels[min(int(rng.geometric(0.35)) - 1, len(levels) - 1)]
            # cancel a random fraction of the level (big queues lose more shares)
            new_qty = book[tick] - max(100, int(book[tick] * rng.uniform(0.2, 1.0)) // 100 * 100)
            if new_qty <= 0 and len(book) <= MIN_LEVELS:
                continue
            set_level(side, tick, max(new_qty, 0), ts)

        elif u < p_add + p_cancel + p_trade:  # aggressive order hits the touch
            bq, aq = bids[bb], asks[ba]
            imbalance = (bq - aq) / (bq + aq)
            buy = rng.random() < 0.5 + 0.35 * imbalance
            tick, book, side = (ba, asks, "A") if buy else (bb, bids, "B")
            size = min(book[tick], 100 * int(rng.integers(1, 8)))
            seq = emit({"type": "trade", "ts": ts, "px": tick, "qty": size})
            record_truth(seq, ts)                      # trade msg itself doesn't move the book
            truth_trades.append((seq, "B" if buy else "S"))
            set_level(side, tick, book[tick] - size, ts + 50)

        else:  # new level inside the spread
            if ba - bb > 1:
                side = "B" if rng.random() < 0.5 else "A"
                tick = bb + 1 if side == "B" else ba - 1
                set_level(side, tick, 100 * int(rng.integers(1, 6)), ts)
            else:
                side = "B" if rng.random() < 0.5 else "A"
                tick = bb if side == "B" else ba
                book = bids if side == "B" else asks
                set_level(side, tick, book[tick] + 100 * int(rng.integers(1, 4)), ts)

        refill_and_cap(ts + 100)


# --------------------------------------------------------------------------- #
# 3) delivery: drop a run, reorder, duplicate, heartbeat, malformed, float noise
# --------------------------------------------------------------------------- #
def px_to_json(tick):
    r = rng.random()
    if r < 0.03:
        return tick * 0.01                    # e.g. 100.13000000000001
    if r < 0.05:
        return tick / 100 - 1e-11             # e.g. 100.12999999999 -> int() truncation trap
    return round(tick / 100, 2)


def to_wire(msg):
    m = dict(msg)
    if m["type"] in ("update", "trade"):
        m["px"] = px_to_json(m["px"])
    elif m["type"] == "snapshot":
        m["bids"] = [[round(t / 100, 2), q] for t, q in m["bids"]]
        m["asks"] = [[round(t / 100, 2), q] for t, q in m["asks"]]
    order = ["type", "seq", "ts", "side", "px", "qty", "bids", "asks"]
    return {k: m[k] for k in order if k in m}


def deliver():
    # (a) drop 3 consecutive seqs: a trade that empties the touch + its update + next msg
    by_seq = {m["seq"]: m for m in messages}
    drop_start = None
    for s in range(101_500, 104_000):
        m, nxt = by_seq.get(s), by_seq.get(s + 1)
        if m and nxt and m["type"] == "trade" and nxt["type"] == "update" and nxt["qty"] == 0:
            drop_start = s
            break
    dropped = {drop_start, drop_start + 1, drop_start + 2}
    stream = [m for m in messages if m["seq"] not in dropped]

    # (b) out-of-order: swap ~0.2% messages with a neighbour 1..3 positions later
    n = len(stream)
    protected = lambda i: stream[i]["type"] == "snapshot" or abs(stream[i]["seq"] - drop_start) < 30
    used = set()
    for i in sorted(rng.choice(n - 5, size=int(0.002 * n), replace=False)):
        j = i + int(rng.integers(1, 4))
        if {i, j} & used or protected(i) or protected(j):
            continue
        stream[i], stream[j] = stream[j], stream[i]
        used |= {i, j}

    # (c) duplicates (~0.1%), re-delivered 0..5 positions later
    dup_at = {}
    for i in rng.choice(n - 10, size=int(0.001 * n), replace=False):
        if stream[i]["type"] != "snapshot":
            dup_at.setdefault(i + int(rng.integers(0, 6)), []).append(stream[i])

    lines = []
    next_hb = START_NS + 1_000_000_000
    malformed_at = int(n * 0.37)
    for i, m in enumerate(stream):
        while m["ts"] >= next_hb:               # heartbeats: no seq
            lines.append(json.dumps({"type": "heartbeat", "ts": next_hb}, separators=(",", ":")))
            next_hb += 1_000_000_000
        wire = json.dumps(to_wire(m), separators=(",", ":"))
        if i == malformed_at:
            lines.append(wire[: len(wire) // 2])   # truncated copy (the intact msg follows)
        lines.append(wire)
        for d in dup_at.get(i, []):
            lines.append(json.dumps(to_wire(d), separators=(",", ":")))
    return lines


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    simulate()
    lines = deliver()
    feed_path = OUT / "feed.jsonl"
    feed_path.write_text("\n".join(lines) + "\n")

    tb = pd.DataFrame(truth_rows, columns=["seq", "ts", "bid", "bid_qty", "ask", "ask_qty"])
    tb["ts"] = pd.to_datetime(tb["ts"], unit="ns", utc=True)
    tb["bid"] = tb["bid"] / 100
    tb["ask"] = tb["ask"] / 100
    tb.to_parquet(OUT / "truth_bbo.parquet", index=False)
    tt = pd.DataFrame(truth_trades, columns=["seq", "aggressor"])
    tt.to_parquet(OUT / "truth_trades.parquet", index=False)

    for p in sorted(OUT.iterdir()):
        print(f"{p.relative_to(OUT.parent.parent)}  {p.stat().st_size / 1e6:6.1f} MB")


if __name__ == "__main__":
    main()
