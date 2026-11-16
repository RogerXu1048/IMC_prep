"""F2: rebuild an L2 order book from a JSON feed, then describe the market. Run make_data.py first."""
import json
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA, PLOTS = ROOT / "data" / "p03", ROOT / "plots"
REORDER_WINDOW = 20                      # msgs we wait for a hole to fill before declaring a gap
BBO_COLS = ["bid", "bid_qty", "ask", "ask_qty"]

def to_tick(px: float) -> int:
    # DECISION: integer ticks as keys. round(), not int(): int(100.12999999999 * 100) = 10012, one tick off
    return round(px * 100)

def step1_load_and_audit(path: Path) -> list[dict]:
    msgs, bad_lines = [], 0
    with open(path) as f:
        for line in f:                   # DECISION: stream line by line; one bad line must not kill the job
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                bad_lines += 1
    seqs = pd.Series([m["seq"] for m in msgs if "seq" in m])   # heartbeats carry no seq
    dup = seqs.duplicated()
    uniq = seqs[~dup]                    # audit in ARRIVAL order, before deciding any policy
    late = (uniq < uniq.cummax().shift(fill_value=0)).sum()
    missing = sorted(set(range(uniq.min(), uniq.max() + 1)) - set(uniq))
    pxs = [m["px"] for m in msgs if "px" in m]
    print("STEP 1 audit")
    print(f"  malformed lines={bad_lines}  types={pd.Series([m.get('type') for m in msgs]).value_counts().to_dict()}")
    print(f"  duplicates={dup.sum()}  arrived late={late}  missing seqs={missing}")
    print(f"  prices with float noise={sum(p != round(p, 2) for p in pxs)}, "
          f"of which int(px*100) is wrong={sum(int(p * 100) != to_tick(p) for p in pxs)}")
    return msgs

class L2Book:
    """Price-level book: tick -> qty per side, plus a cached best price per side."""
    def __init__(self) -> None:
        self.levels: dict[str, dict[int, int]] = {"B": {}, "A": {}}
        self.best: dict[str, int | None] = {"B": None, "A": None}

    def set(self, side: str, tick: int, qty: int) -> None:
        lv, best = self.levels[side], self.best[side]
        if qty > 0:                      # an update carries the NEW absolute size of the level
            lv[tick] = qty
            if best is None or (tick > best if side == "B" else tick < best):
                self.best[side] = tick
        else:
            lv.pop(tick, None)
            if tick == best:             # TRADE-OFF: O(L) rescan only when the best level dies;
                self.best[side] = (max if side == "B" else min)(lv, default=None)  # sorted tree: O(log L) always

    def reset(self, bids: list, asks: list) -> None:
        for side, pairs in (("B", bids), ("A", asks)):
            self.levels[side] = {to_tick(px): q for px, q in pairs if q > 0}
            self.best[side] = (max if side == "B" else min)(self.levels[side], default=None)

    def bbo(self) -> tuple:
        b, a = self.best["B"], self.best["A"]
        return b, self.levels["B"].get(b), a, self.levels["A"].get(a)

class FeedHandler:
    """Sequencer in front of the book: LIVE -> (hole outlives the reorder window) -> STALE -> snapshot -> LIVE."""
    def __init__(self) -> None:
        self.book, self.next_seq, self.pending, self.stale = L2Book(), None, {}, True
        self.rows, self.trades, self.gaps, self.dups, self.snap_mismatch = [], [], [], 0, 0

    def on_message(self, m: dict) -> None:
        seq = m.get("seq")
        if seq is None:
            return
        if m["type"] == "snapshot":
            return self._on_snapshot(m)
        if self.next_seq is None or seq < self.next_seq or seq in self.pending:
            self.dups += 1               # duplicate (or before the first snapshot): drop
            return
        self.pending[seq] = m            # DECISION: order by exchange seq, never by timestamp
        self._drain()
        if self.pending and not self.stale and len(self.pending) > REORDER_WINDOW:
            self.gaps.append(self.next_seq)   # hole did not fill in time: stop vouching for the book
            self.stale = True

    def _drain(self) -> None:
        while self.next_seq in self.pending:
            m = self.pending.pop(self.next_seq)
            self.stale = False
            if m["type"] == "update":
                self.book.set(m["side"], to_tick(m["px"]), m["qty"])
            elif m["type"] == "trade":   # a trade print doesn't change L2; the update after it does
                self.trades.append((m["seq"], m["ts"], to_tick(m["px"]), m["qty"]))
            self._record(m["seq"], m["ts"])
            self.next_seq += 1

    def _on_snapshot(self, m: dict) -> None:
        S = m["seq"]
        if self.next_seq is not None and S < self.next_seq:
            return                       # older than what we already applied
        if self.next_seq == S and not self.stale:   # CHECK: free reconciliation of a live book
            snap = L2Book()
            snap.reset(m["bids"], m["asks"])
            self.snap_mismatch += snap.levels != self.book.levels
        for s in sorted(k for k in self.pending if k < S):   # superseded by the snapshot -> stale rows
            old = self.pending.pop(s)
            if old["type"] == "trade":   # the trade still happened, we just can't say what the book was
                self.trades.append((s, old["ts"], to_tick(old["px"]), old["qty"]))
            self.rows.append((s, old["ts"], None, None, None, None, True))
        self.book.reset(m["bids"], m["asks"])
        self.stale = False
        self._record(S, m["ts"])
        self.next_seq = S + 1
        self._drain()

    def _record(self, seq: int, ts: int) -> None:
        b, bq, a, aq = self.book.bbo()
        assert b is None or a is None or b < a, f"crossed book at seq {seq}"   # CHECK: invariant
        self.rows.append((seq, ts, b, bq, a, aq, False))

def naive_rebuild(msgs: list[dict]) -> pd.DataFrame:   # arrival order, no dedup/reorder/gap logic
    book, last = L2Book(), {}
    for m in msgs:
        if m["type"] == "snapshot":
            book.reset(m["bids"], m["asks"])
        elif m["type"] == "update":
            book.set(m["side"], to_tick(m["px"]), m["qty"])
        if "seq" in m:
            last[m["seq"]] = book.bbo()
    return pd.DataFrame([(s, *v) for s, v in last.items()], columns=["seq", *BBO_COLS])

def match_rate(df: pd.DataFrame, truth: pd.DataFrame) -> float:
    m = df.merge(truth, on="seq", suffixes=("", "_t"))
    return (m[BBO_COLS].to_numpy() == m[[c + "_t" for c in BBO_COLS]].to_numpy()).all(axis=1).mean()

def step2_rebuild(msgs: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    fh, t0 = FeedHandler(), time.perf_counter()
    for m in msgs:
        fh.on_message(m)
    rate = len(msgs) / (time.perf_counter() - t0)
    bbo = pd.DataFrame(fh.rows, columns=["seq", "ts", *BBO_COLS, "stale"]).sort_values("seq", ignore_index=True)
    trades = pd.DataFrame(fh.trades, columns=["seq", "ts", "px", "qty"]).sort_values("seq", ignore_index=True)
    for df in (bbo, trades):
        df["ts"] = pd.to_datetime(df["ts"], unit="ns", utc=True)
    truth = pd.read_parquet(DATA / "truth_bbo.parquet")
    truth[["bid", "ask"]] = (truth[["bid", "ask"]] * 100).round()   # truth is in $, we work in ticks
    naive = naive_rebuild(msgs)
    stale = bbo[bbo.stale]
    print(f"STEP 2 rebuild ({rate:,.0f} msgs/s)")
    print(f"  dups dropped={fh.dups}  gap declared at seq {fh.gaps}  snapshot mismatches={fh.snap_mismatch}")
    print(f"  stale rows={len(stale)} (seq {stale.seq.min()}..{stale.seq.max()})")
    print(f"  live rows matching truth: {match_rate(bbo[~bbo.stale], truth):.2%}")
    print(f"  naive arrival-order rebuild: {1 - match_rate(naive, truth):.2%} rows wrong, "
          f"crossed rows={(naive.bid >= naive.ask).sum()}")
    return bbo, trades

def step3_stats(bbo: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    # time weight = how long each BBO state lasted; 0 if the next row is stale (we don't know)
    next_live = ~bbo.stale.shift(-1, fill_value=True)
    bbo = bbo.assign(dur_s=(bbo.ts.shift(-1) - bbo.ts).dt.total_seconds().where(next_live, 0.0))
    live = bbo[~bbo.stale]
    spread = live.ask - live.bid
    # quote rule: compare the price with the mid just before the trade (row at the trade's seq)
    t = trades.merge(bbo[["seq", "bid", "ask"]], on="seq")
    t["sign"] = np.sign(2 * t.px - (t.bid + t.ask))
    tick_rule = np.sign(t.px.diff()).replace(0, np.nan).ffill()
    t["sign"] = t.sign.where(t.sign.fillna(0) != 0, tick_rule)       # at the mid or stale -> tick rule
    truth = pd.read_parquet(DATA / "truth_trades.parquet")
    acc = (t.merge(truth, on="seq").pipe(lambda d: d.sign == np.where(d.aggressor == "B", 1, -1))).mean()
    print("STEP 3 stats")
    print(f"  spread (ticks): event-weighted={spread.mean():.2f}  time-weighted={np.average(spread, weights=live.dur_s):.2f}")
    print(f"  trades={len(t):,}  VWAP=${(t.px * t.qty).sum() / t.qty.sum() / 100:.4f}  sign accuracy={acc:.1%}")
    start = t.ts.dt.floor("1min").value_counts().idxmax()            # busiest minute by trade count
    w, wt = (d[d.ts.between(start, start + pd.Timedelta("1min"))] for d in (live, t))
    fig, ax = plt.subplots(figsize=(11, 4))
    for col in ("bid", "ask"):
        ax.step(w.ts, w[col] / 100, where="post", label=f"best {col}")
    for s, c, name in ((1, "green", "buy-initiated"), (-1, "red", "sell-initiated")):
        ax.scatter(wt.ts[wt.sign == s], wt.px[wt.sign == s] / 100, s=8, c=c, label=name, zorder=3)
    ax.set(title=f"BBO and trades, busiest minute {start:%H:%M} UTC", ylabel="price ($)")
    ax.legend(fontsize=8)
    fig.savefig(PLOTS / "f2_bbo_trades.png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return live

def step4_imbalance(live: pd.DataFrame) -> None:
    move = (live.bid + live.ask).diff().shift(-1)                    # 2*mid change to the next row
    df = pd.DataFrame({
        "imb": (live.bid_qty - live.ask_qty) / (live.bid_qty + live.ask_qty),
        "next": np.sign(move.where(move != 0).bfill()),              # sign of the NEXT non-zero mid move
    }).dropna()
    train, test = df.iloc[: len(df) // 2], df.iloc[len(df) // 2:]
    # DECISION: decile edges from the first half only -> no look-ahead into the test half
    edges = np.unique(np.r_[-np.inf, np.quantile(train.imb, np.linspace(0.1, 0.9, 9)), np.inf])
    table = pd.DataFrame({name: d.groupby(pd.cut(d.imb, edges, labels=False)).next.apply(lambda s: (s > 0).mean())
                          for name, d in (("first_half", train), ("second_half", test))})
    nz = test[test.imb != 0]
    print("STEP 4 imbalance -> P(next mid move is up), by decile")
    print(table.round(2).T.to_string())
    print(f"  out of sample: sign(imbalance) gets the next move right {(np.sign(nz.imb) == nz.next).mean():.1%}")
    fig, ax = plt.subplots(figsize=(7, 4))
    table.plot(ax=ax, marker="o")
    ax.axhline(0.5, color="grey", ls="--")
    ax.set(xlabel="top-of-book imbalance decile (0 = ask-heavy)", ylabel="P(next mid move up)")
    fig.savefig(PLOTS / "f2_imbalance_deciles.png", dpi=110, bbox_inches="tight")
    plt.close(fig)

def main() -> None:
    PLOTS.mkdir(parents=True, exist_ok=True)
    msgs = step1_load_and_audit(DATA / "feed.jsonl")
    bbo, trades = step2_rebuild(msgs)
    live = step3_stats(bbo, trades)
    step4_imbalance(live)

if __name__ == "__main__":
    main()
