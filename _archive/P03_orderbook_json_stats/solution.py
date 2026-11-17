#!/usr/bin/env python3
"""P03 reference solution: rebuild an L2 book from a JSON stream, then analyse it.

Run:  python3 solution.py        (after python3 make_data.py)

Design choices worth saying out loud in an interview
----------------------------------------------------
* Prices -> integer ticks with round(px * 100). Floats are not safe dict keys
  (100.13 vs 100.13000000000001) and int(px * 100) truncates 100.12999999999 -> 10012.
* Order by SEQUENCE NUMBER, never by timestamp: many messages share a timestamp,
  and the exchange's seq is the authoritative mutation order.
* Small reorder buffer for late-by-a-few messages; if a hole persists, the book is
  marked STALE (not published as tradable) until the next snapshot.
* Updates carry the absolute size, so applying one twice is harmless *only if* no
  other update for that level happened in between - we still drop duplicates by seq.
"""
import heapq
import json
import math
import time
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p03"
PLOTS = ROOT / "plots"
NS_PER_S = 1_000_000_000


def to_tick(px: float) -> int:
    """$ -> integer ticks (tick = $0.01). round(), NOT int(): int() truncates float noise."""
    return int(round(px * 100))


# =========================================================================== #
# Part A - robust loading + sequence integrity
# =========================================================================== #
def part_a_load(path):
    msgs, malformed = [], []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):          # stream line by line: O(1) extra memory per line
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(lineno)
                continue
            if not isinstance(m, dict) or "type" not in m:
                malformed.append(lineno)
                continue
            msgs.append(m)

    types = Counter(m["type"] for m in msgs)

    # sequence integrity, in ARRIVAL order
    seen, dups, out_of_order, max_seq = set(), 0, 0, 0
    for m in msgs:
        s = m.get("seq")
        if s is None:                 # heartbeats carry no seq
            continue
        if s in seen:
            dups += 1
            continue
        seen.add(s)
        if s < max_seq:
            out_of_order += 1
        max_seq = max(max_seq, s)
    missing = sorted(set(range(min(seen), max(seen) + 1)) - seen)

    # price representation problems
    noisy = trunc_wrong = 0
    for m in msgs:
        if "px" in m:
            px = m["px"]
            noisy += px != round(px, 2)
            trunc_wrong += int(px * 100) != to_tick(px)

    print("== Part A: load + integrity ==")
    print(f"lines parsed: {len(msgs):,}   malformed lines: {malformed}")
    print(f"message types: {dict(types)}")
    print(f"seq range {min(seen)}..{max(seen)}  unique={len(seen):,}  duplicates={dups}  "
          f"arrived-out-of-order={out_of_order}")
    print(f"missing seqs (never delivered): {missing}")
    print(f"prices with float noise: {noisy}   of which int(px*100) is WRONG: {trunc_wrong}")
    return msgs, missing


# =========================================================================== #
# Part B - rebuild the book
# =========================================================================== #
class BookSide:
    """tick -> qty hash map + cached best price.

    set():   O(1) average; O(L) rescan only when the best level is deleted
             (L = levels on this side, <= ~50 here).
    best:    O(1).
    top(k):  O(L log k) via heapq (a dict is NOT sorted).
    """

    def __init__(self, is_bid: bool):
        self.is_bid = is_bid
        self.levels = {}
        self.best = None

    def set(self, tick, qty):
        if qty > 0:
            self.levels[tick] = qty
            if self.best is None or (tick > self.best if self.is_bid else tick < self.best):
                self.best = tick
        else:
            self.levels.pop(tick, None)
            if tick == self.best:
                if self.levels:
                    self.best = max(self.levels) if self.is_bid else min(self.levels)
                else:
                    self.best = None

    def reset(self, pairs):
        self.levels = {to_tick(px): q for px, q in pairs if q > 0}
        if self.levels:
            self.best = max(self.levels) if self.is_bid else min(self.levels)
        else:
            self.best = None

    def top(self, k):
        pick = heapq.nlargest if self.is_bid else heapq.nsmallest
        return [(t, self.levels[t]) for t in pick(k, self.levels)]


class Rebuilder:
    """Sequence-aware L2 book builder.

    State machine:  WAIT_SNAPSHOT -> LIVE <-> (hole) -> STALE -> (snapshot or hole filled) -> LIVE
    """

    def __init__(self, max_reorder=20, sample_every_ns=250_000_000, depth=20):
        self.bids, self.asks = BookSide(True), BookSide(False)
        self.next_seq = None          # None until the first snapshot
        self.pending = {}             # seq -> msg that arrived ahead of a hole
        self.stale = True
        self.max_reorder = max_reorder
        self.rows = []                # (seq, ts, bid, bid_qty, ask, ask_qty, stale)
        self.trades = []              # (seq, ts, px_tick, qty)
        self.gaps = []                # (first_missing, last_missing) when declared
        self.dups = 0
        self.snap_checks = self.snap_mismatch = 0
        self.crossed = 0
        self.sample_every_ns, self.depth = sample_every_ns, depth
        self.next_sample = None
        self.depth_samples = []       # (ts, [(tick,qty) bids], [(tick,qty) asks])

    # -- public entry point -------------------------------------------------
    def on_message(self, m):
        seq = m.get("seq")
        if seq is None:                                   # heartbeat
            return
        if m["type"] == "snapshot":
            self._on_snapshot(m)
            return
        if self.next_seq is None or seq < self.next_seq or seq in self.pending:
            self.dups += 1                                # duplicate / before first snapshot
            return
        self.pending[seq] = m
        self._drain()
        if self.pending and not self.stale and len(self.pending) > self.max_reorder:
            # the hole at next_seq did not fill within the reorder window -> declare a gap
            self.gaps.append((self.next_seq, min(self.pending) - 1))
            self.stale = True

    # -- internals ------------------------------------------------------------
    def _drain(self):
        while self.next_seq in self.pending:
            m = self.pending.pop(self.next_seq)
            self.stale = False                            # a late message healed the hole
            self._apply(m)
            self._record(m["seq"], m["ts"])
            self.next_seq += 1

    def _apply(self, m):
        if m["type"] == "update":
            side = self.bids if m["side"] == "B" else self.asks
            side.set(to_tick(m["px"]), m["qty"])
        elif m["type"] == "trade":
            # trade prints don't change an L2 book: the following update does
            self.trades.append((m["seq"], m["ts"], to_tick(m["px"]), m["qty"]))

    def _on_snapshot(self, m):
        S = m["seq"]
        if self.next_seq is not None and S < self.next_seq:
            return                                        # old / duplicate snapshot
        if self.next_seq == S and not self.stale:         # free consistency check
            self.snap_checks += 1
            snap_b = {to_tick(p): q for p, q in m["bids"]}
            snap_a = {to_tick(p): q for p, q in m["asks"]}
            self.snap_mismatch += (snap_b != self.bids.levels) or (snap_a != self.asks.levels)
        for s in sorted(self.pending):                    # everything < S is superseded
            if s < S:
                old = self.pending.pop(s)
                if old["type"] == "trade":                # trade prints are still real trades
                    self.trades.append((s, old["ts"], to_tick(old["px"]), old["qty"]))
                self.rows.append((s, old["ts"], np.nan, np.nan, np.nan, np.nan, True))
        self.bids.reset(m["bids"])
        self.asks.reset(m["asks"])
        self.next_seq, self.stale = S + 1, False
        self._record(S, m["ts"])
        self._drain()

    def _record(self, seq, ts):
        bb, ba = self.bids.best, self.asks.best
        if bb is not None and ba is not None and bb >= ba:
            self.crossed += 1
        self.rows.append((seq, ts, bb, self.bids.levels.get(bb), ba, self.asks.levels.get(ba), False))
        if self.next_sample is None:
            self.next_sample = ts
        if ts >= self.next_sample:                        # periodic depth sample for heatmap / top-5
            self.depth_samples.append((ts, self.bids.top(self.depth), self.asks.top(self.depth)))
            while self.next_sample <= ts:
                self.next_sample += self.sample_every_ns


def naive_rebuild(msgs):
    """What happens if you just apply messages in arrival order (no dedup, no reorder, no gap logic)."""
    bids, asks = BookSide(True), BookSide(False)
    last = {}
    for m in msgs:
        if m["type"] == "snapshot":
            bids.reset(m["bids"]); asks.reset(m["asks"])
        elif m["type"] == "update":
            (bids if m["side"] == "B" else asks).set(to_tick(m["px"]), m["qty"])
        if "seq" in m:
            last[m["seq"]] = (bids.best, bids.levels.get(bids.best), asks.best, asks.levels.get(asks.best))
    return last


def part_b_rebuild(msgs, missing):
    rb = Rebuilder()
    t0 = time.perf_counter()
    for m in msgs:
        rb.on_message(m)
    elapsed = time.perf_counter() - t0

    bbo = pd.DataFrame(rb.rows, columns=["seq", "ts", "bid", "bid_qty", "ask", "ask_qty", "stale"])
    bbo = bbo.sort_values("seq").reset_index(drop=True)
    bbo["ts"] = pd.to_datetime(bbo["ts"], unit="ns", utc=True)

    truth = pd.read_parquet(DATA / "truth_bbo.parquet")
    truth["bid_t"] = (truth["bid"] * 100).round().astype("int64")
    truth["ask_t"] = (truth["ask"] * 100).round().astype("int64")
    cmp = bbo.merge(truth[["seq", "bid_t", "bid_qty", "ask_t", "ask_qty"]], on="seq", suffixes=("", "_true"))
    live = cmp[~cmp["stale"]]
    agree = ((live["bid"] == live["bid_t"]) & (live["ask"] == live["ask_t"])
             & (live["bid_qty"] == live["bid_qty_true"]) & (live["ask_qty"] == live["ask_qty_true"]))

    # naive comparison
    naive = naive_rebuild(msgs)
    nv = pd.DataFrame([(s, *v) for s, v in naive.items()], columns=["seq", "bid", "bid_qty", "ask", "ask_qty"])
    nv = nv.merge(truth[["seq", "bid_t", "bid_qty", "ask_t", "ask_qty"]], on="seq", suffixes=("", "_true"))
    naive_bad = ~((nv["bid"] == nv["bid_t"]) & (nv["ask"] == nv["ask_t"])
                  & (nv["bid_qty"] == nv["bid_qty_true"]) & (nv["ask_qty"] == nv["ask_qty_true"]))
    naive_crossed = int((nv["bid"] >= nv["ask"]).sum())

    print("\n== Part B: rebuild ==")
    print(f"processed {len(msgs):,} msgs in {elapsed:.2f}s  ->  {len(msgs) / elapsed:,.0f} msgs/s (pure Python)")
    print(f"duplicates/old dropped: {rb.dups}   gaps declared: {rb.gaps}   (true missing seqs: {missing})")
    print(f"snapshot reconciliations: {rb.snap_checks}, mismatches: {rb.snap_mismatch}   crossed rows: {rb.crossed}")
    stale_rows = int(bbo["stale"].sum())
    print(f"BBO rows: {len(bbo):,}   stale rows: {stale_rows:,} "
          f"(seq {int(bbo.loc[bbo.stale, 'seq'].min())}..{int(bbo.loc[bbo.stale, 'seq'].max())})")
    print(f"agreement with truth on LIVE rows: {agree.mean():.4%} of {len(live):,}")
    print(f"naive (arrival-order) rebuild: {naive_bad.mean():.3%} of seqs wrong "
          f"({int(naive_bad.sum()):,} rows), crossed rows: {naive_crossed}")
    return rb, bbo


# =========================================================================== #
# Part C - statistics
# =========================================================================== #
def classify_trades(rb, bbo):
    tr = pd.DataFrame(rb.trades, columns=["seq", "ts", "px", "qty"]).drop_duplicates("seq").sort_values("seq")
    tr["ts"] = pd.to_datetime(tr["ts"], unit="ns", utc=True)
    # The BBO row AT the trade's seq is the book just before the trade's update => the prevailing quote.
    tr = tr.merge(bbo[["seq", "bid", "ask", "stale"]], on="seq", how="left")
    mid2 = tr["bid"] + tr["ask"]                      # 2*mid in ticks: avoids .5 tick floats
    quote_sign = np.where(2 * tr["px"] > mid2, 1, np.where(2 * tr["px"] < mid2, -1, 0))
    quote_sign = np.where(tr["stale"].fillna(True).astype(bool), 0, quote_sign)

    # tick rule: compare with previous trade price; zero tick -> carry the last non-zero sign
    dpx = tr["px"].diff()
    tick_sign = pd.Series(np.sign(dpx)).replace(0, np.nan).ffill().fillna(1).to_numpy()
    tr["sign_tick"] = tick_sign.astype(int)
    tr["sign"] = np.where(quote_sign != 0, quote_sign, tick_sign).astype(int)   # quote rule, tick fallback
    tr["used_fallback"] = quote_sign == 0
    return tr


def part_c_stats(rb, bbo):
    live = bbo[~bbo["stale"]].copy()
    live["spread"] = live["ask"] - live["bid"]
    live["mid"] = (live["ask"] + live["bid"]) / 200     # dollars

    # time weight = how long this BBO state was in force (until the next row);
    # the row before a stale stretch gets weight 0 because we don't know what happened next
    nxt_ts = bbo["ts"].shift(-1)
    nxt_stale = bbo["stale"].shift(-1).fillna(True).astype(bool)
    dur = (nxt_ts - bbo["ts"]).dt.total_seconds().where(~nxt_stale, 0.0).fillna(0.0)
    live["dur"] = dur[~bbo["stale"]]
    ev_mean = live["spread"].mean()
    tw_mean = np.average(live["spread"], weights=live["dur"])
    ev_dist = live["spread"].value_counts(normalize=True).sort_index()
    tw_dist = live.groupby("spread")["dur"].sum() / live["dur"].sum()

    # per-minute activity and realised volatility (1s-sampled mid, log returns)
    mid_1s = live.set_index("ts")["mid"].resample("1s").last().ffill()
    r = np.log(mid_1s).diff().dropna()
    rv_bps = (r.pow(2).resample("1min").sum() ** 0.5) * 1e4
    msgs_per_min = bbo.set_index("ts")["seq"].resample("1min").count()

    tr = classify_trades(rb, bbo)
    truth = pd.read_parquet(DATA / "truth_trades.parquet")
    tr = tr.merge(truth, on="seq", how="left")
    true_sign = np.where(tr["aggressor"] == "B", 1, -1)
    acc_combined = (tr["sign"] == true_sign).mean()
    acc_tick = (tr["sign_tick"] == true_sign).mean()

    per_min = tr.set_index("ts").resample("1min").agg(trades=("qty", "size"), volume=("qty", "sum"))
    vwap = (tr["px"] * tr["qty"]).sum() / tr["qty"].sum() / 100

    live["imb1"] = (live["bid_qty"] - live["ask_qty"]) / (live["bid_qty"] + live["ask_qty"])
    imb5 = []
    for ts, b, a in rb.depth_samples:
        bq, aq = sum(q for _, q in b[:5]), sum(q for _, q in a[:5])
        imb5.append((pd.Timestamp(ts, unit="ns", tz="UTC"), (bq - aq) / (bq + aq)))
    imb5 = pd.DataFrame(imb5, columns=["ts", "imb5"])

    print("\n== Part C: statistics ==")
    print(f"spread (ticks)  event-weighted mean = {ev_mean:.3f}   TIME-weighted mean = {tw_mean:.3f}")
    print("  distribution (ticks: event% / time%):",
          ", ".join(f"{int(k)}: {ev_dist.get(k, 0):.1%}/{tw_dist.get(k, 0):.1%}" for k in range(1, 6)))
    print(f"corr(messages per minute, realised vol per minute) = {np.corrcoef(msgs_per_min.reindex(rv_bps.index).fillna(0), rv_bps)[0, 1]:.2f}")
    print(f"realised vol per minute (bps): median {rv_bps.median():.1f}, max {rv_bps.max():.1f}")
    print(f"trades: {len(tr):,}  volume: {tr['qty'].sum():,}  VWAP: ${vwap:.4f}")
    print(f"trades/min: median {per_min['trades'].median():.0f}, max {per_min['trades'].max()}")
    print(f"trade sign accuracy: quote rule (+tick fallback on {int(tr['used_fallback'].sum())} stale trades) "
          f"= {acc_combined:.2%};  tick rule alone = {acc_tick:.2%}")
    print(f"imbalance top-1: mean {live['imb1'].mean():+.3f};  top-5 (250ms samples): mean {imb5['imb5'].mean():+.3f}, "
          f"corr(top1, top5) at sample times = "
          f"{pd.merge_asof(imb5, live[['ts', 'imb1']], on='ts')[['imb1', 'imb5']].corr().iloc[0, 1]:.2f}")
    return live, tr, ev_dist, tw_dist


# =========================================================================== #
# Part D - plots
# =========================================================================== #
def part_d_plots(rb, bbo, live, tr, ev_dist, tw_dist):
    PLOTS.mkdir(parents=True, exist_ok=True)

    # 1) busiest minute: BBO step plot + trades coloured by inferred side
    busiest = bbo.set_index("ts")["seq"].resample("1min").count().idxmax()
    w = live[(live["ts"] >= busiest) & (live["ts"] < busiest + pd.Timedelta("1min"))]
    wt = tr[(tr["ts"] >= busiest) & (tr["ts"] < busiest + pd.Timedelta("1min"))]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.step(w["ts"], w["bid"] / 100, where="post", lw=0.8, color="tab:blue", label="best bid")
    ax.step(w["ts"], w["ask"] / 100, where="post", lw=0.8, color="tab:orange", label="best ask")
    for s, c, lab in ((1, "tab:green", "buy-initiated"), (-1, "tab:red", "sell-initiated")):
        x = wt[wt["sign"] == s]
        ax.scatter(x["ts"], x["px"] / 100, s=6, color=c, label=lab, zorder=3)
    ax.set(title=f"BBO and trades, busiest minute ({busiest:%H:%M} UTC)", ylabel="price ($)")
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout(); fig.savefig(PLOTS / "p03_bbo_trades_window.png", dpi=120); plt.close(fig)

    # 2) spread distribution: event- vs time-weighted
    ks = np.arange(1, 9)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(ks - 0.2, [ev_dist.get(k, 0) for k in ks], width=0.4, label="event-weighted")
    ax.bar(ks + 0.2, [tw_dist.get(k, 0) for k in ks], width=0.4, label="time-weighted")
    ax.set(xlabel="spread (ticks)", ylabel="share", title="Spread distribution")
    ax.legend(); fig.tight_layout(); fig.savefig(PLOTS / "p03_spread_hist.png", dpi=120); plt.close(fig)

    # 3) depth heatmap: price level x time, +bid qty / -ask qty, 3 minutes from the busiest minute
    t0 = busiest.value
    samples = [s for s in rb.depth_samples if t0 <= s[0] < t0 + 180 * NS_PER_S]
    mids = [(b[0][0] + a[0][0]) / 2 for _, b, a in samples]
    lo, hi = int(min(mids)) - 15, int(max(mids)) + 15
    grid = np.zeros((hi - lo + 1, len(samples)))
    for j, (_, b, a) in enumerate(samples):
        for tick, q in b:
            if lo <= tick <= hi:
                grid[tick - lo, j] = q
        for tick, q in a:
            if lo <= tick <= hi:
                grid[tick - lo, j] = -q
    vmax = np.quantile(np.abs(grid[grid != 0]), 0.98)
    fig, ax = plt.subplots(figsize=(12, 5))
    secs = np.array([(s[0] - t0) / NS_PER_S for s in samples])
    im = ax.imshow(grid, aspect="auto", origin="lower", cmap="RdBu", vmin=-vmax, vmax=vmax,
                   extent=[secs[0], secs[-1], (lo - 0.5) / 100, (hi + 0.5) / 100])
    ax.plot(secs, np.array(mids) / 100, color="k", lw=0.8, label="mid")
    fig.colorbar(im, ax=ax, label="qty (blue = bid, red = ask)")
    ax.set(xlabel=f"seconds after {busiest:%H:%M:%S}", ylabel="price ($)", title="Depth heatmap (top 20 levels)")
    ax.legend(loc="upper left"); fig.tight_layout(); fig.savefig(PLOTS / "p03_depth_heatmap.png", dpi=120); plt.close(fig)


# =========================================================================== #
# Part E - does top-of-book imbalance predict the next mid move?
# =========================================================================== #
def part_e_predict(live):
    df = live[["ts", "mid", "imb1"]].reset_index(drop=True)
    step = df["mid"].diff().shift(-1)                       # change from this row to the next
    df["next_move"] = np.sign(step.where(step != 0).bfill())  # sign of the NEXT non-zero mid change
    # also: mid change over the next 1 second, in ticks (merge_asof forward in time)
    fut = df[["ts", "mid"]].rename(columns={"ts": "ts_f", "mid": "mid_f"})
    df["ts_target"] = df["ts"] + pd.Timedelta("1s")
    df = pd.merge_asof(df.sort_values("ts_target"), fut, left_on="ts_target", right_on="ts_f",
                       direction="backward").sort_values("ts")
    df["dmid_1s_ticks"] = (df["mid_f"] - df["mid"]) * 100
    df = df.dropna(subset=["next_move"])

    split = df["ts"].iloc[len(df) // 2]
    train, test = df[df["ts"] < split], df[df["ts"] >= split]
    edges = np.unique(np.quantile(train["imb1"], np.linspace(0, 1, 11)))
    edges[0], edges[-1] = -np.inf, np.inf                    # bins fitted on TRAIN only
    out = {}
    for name, part in (("in-sample (1st half)", train), ("out-of-sample (2nd half)", test)):
        dec = pd.cut(part["imb1"], edges, labels=False)
        out[name] = part.groupby(dec).agg(p_up=("next_move", lambda s: (s > 0).mean()),
                                          dmid_1s=("dmid_1s_ticks", "mean"), n=("imb1", "size"))
    nz = test[test["imb1"] != 0]
    hit = (np.sign(nz["imb1"]) == nz["next_move"]).mean()
    corr = np.corrcoef(test["imb1"], test["next_move"])[0, 1]

    print("\n== Part E: imbalance -> next mid move ==")
    table = pd.concat(out, axis=1)
    print(table.round(3).to_string())
    print(f"OOS: sign(imbalance) predicts the direction of the next mid move {hit:.1%} of the time "
          f"(coin flip = 50%); corr(imb, next_move) = {corr:.3f}")

    fig, ax = plt.subplots(figsize=(7, 4))
    for name, t in out.items():
        ax.plot(t.index + 1, t["p_up"], marker="o", label=name)
    ax.axhline(0.5, color="grey", ls="--", lw=0.8)
    ax.set(xlabel="top-1 imbalance decile (1 = ask-heavy, 10 = bid-heavy)", ylabel="P(next mid move is up)",
           title="Imbalance predicts the next mid move")
    ax.legend(); fig.tight_layout(); fig.savefig(PLOTS / "p03_imbalance_vs_future_mid.png", dpi=120); plt.close(fig)


# =========================================================================== #
# Part F - performance
# =========================================================================== #
def part_f_perf(msgs):
    rb = Rebuilder(sample_every_ns=10**18)      # no depth sampling: measure the hot path only
    t0 = time.perf_counter()
    for m in msgs:
        rb.on_message(m)
    dt = time.perf_counter() - t0

    # micro-benchmark: rescanning the best price with max(dict) on every message vs. cached best
    levels = {10000 - i: 100 for i in range(50)}
    n = 200_000
    t1 = time.perf_counter()
    for _ in range(n):
        max(levels)
    t_scan = (time.perf_counter() - t1) / n * 1e9

    path = DATA / "feed.jsonl"
    t2 = time.perf_counter()
    with open(path) as f:
        for line in f:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                pass
    t_parse = time.perf_counter() - t2

    print("\n== Part F: performance ==")
    print(f"book update hot path: {len(msgs) / dt:,.0f} msgs/s  ({dt / len(msgs) * 1e6:.2f} us/msg)")
    print(f"json.loads alone: {len(msgs) / t_parse:,.0f} msgs/s  -> parsing is a big share of end-to-end cost")
    print(f"max(dict of 50 levels) = {t_scan:.0f} ns per call; cached best = O(1) and only rescans when the best level dies")


def main():
    msgs, missing = part_a_load(DATA / "feed.jsonl")
    rb, bbo = part_b_rebuild(msgs, missing)
    live, tr, ev_dist, tw_dist = part_c_stats(rb, bbo)
    part_d_plots(rb, bbo, live, tr, ev_dist, tw_dist)
    part_e_predict(live)
    part_f_perf(msgs)
    print(f"\nplots written to {PLOTS}/p03_*.png")


if __name__ == "__main__":
    main()
