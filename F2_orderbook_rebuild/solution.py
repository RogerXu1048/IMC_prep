"""F2: rebuild an L2 order book from a JSON feed, then describe the market. Run make_data.py first."""
import heapq
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
COLS = ["bid", "bid_qty", "ask", "ask_qty"]
ZOOM_S = 3                                     # width of the BBO + trades zoom window (s)
WAITS_MS = [0, 5, 20, 50, 200]                # reorder-buffer budgets to try (step 5)
tick = lambda px: round(px * 100)              # DECISION: round, not int(): int(100.12999999999 * 100) = 10012

def save(ax, name, title, xlabel, ylabel):
    ax.set(title=title, xlabel=xlabel, ylabel=ylabel)
    ax.figure.savefig(PLOTS / f"f2_{name}.png", dpi=110, bbox_inches="tight")
    plt.close(ax.figure)

def load():
    msgs, bad = [], 0
    for line in open(DATA / "feed.jsonl"):
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:           # DECISION: count and skip a bad line, never crash
            bad += 1
    seq = pd.Series([m["seq"] for m in msgs if "seq" in m])        # heartbeats carry no seq
    u = seq.drop_duplicates()                                       # CHECK: audit in ARRIVAL order, before sorting
    px = pd.Series([m["px"] for m in msgs if "px" in m])
    print(f"STEP 1 audit: malformed lines={bad}  types={pd.Series([m['type'] for m in msgs]).value_counts().to_dict()}")
    print(f"  duplicate seqs={seq.duplicated().sum()}  arrived late={(u.cummax() > u).sum()}  "
          f"missing seqs={sorted(set(range(1, seq.max() + 1)) - set(seq))}")
    print(f"  noisy px={(px != px.round(2)).sum()}  int(px*100) wrong={(px * 100).astype(int).ne(tick(px)).sum()}")
    return msgs

def replay(msgs):                              # apply msgs in the order given -> one BBO row per msg
    book, rows, prev, stale = {"B": {}, "A": {}}, [], 0, False
    for m in msgs:
        if m["type"] == "snapshot":            # snapshot seq S = book after all seq < S -> full reset
            book = {"B": {tick(p): q for p, q in m["bids"]}, "A": {tick(p): q for p, q in m["asks"]}}
        elif m["type"] == "update":            # qty = new absolute size, 0 = delete (trades don't touch the book)
            if m["qty"] > 0:
                book[m["side"]][tick(m["px"])] = m["qty"]
            else:
                book[m["side"]].pop(tick(m["px"]), None)
        stale = m["type"] != "snapshot" and (stale or m["seq"] != prev + 1)   # seq gap -> stale until snapshot
        prev, b, a = m["seq"], max(book["B"]), min(book["A"])   # DECISION: max/min is O(levels); ~50 levels, fine
        rows.append((m["seq"], m["ts"], b, book["B"][b], a, book["A"][a], stale))
    return pd.DataFrame(rows, columns=["seq", "ts", *COLS, "stale"]).astype({"ts": "datetime64[ns, UTC]"})

def rebuild(msgs):
    # DECISION: the whole file is on disk, so dedupe by seq and SORT by seq (not ts: many msgs share a ts)
    ordered = sorted({m["seq"]: m for m in msgs if "seq" in m}.values(), key=lambda m: m["seq"])
    bbo = replay(ordered)
    bbo["t"] = (bbo.ts - bbo.ts[0].floor("min")).dt.total_seconds()     # seconds after the 14:30 UTC open
    truth = pd.read_parquet(DATA / "truth_bbo.parquet").set_index("seq")[COLS] * [100, 1, 100, 1]   # $ -> ticks
    wrong = lambda df: df.set_index("seq")[COLS].ne(truth.loc[df.seq].round()).any(axis=1).mean()
    live, st = bbo[~bbo.stale], bbo[bbo.stale]
    assert (live.bid < live.ask).all()                                  # CHECK: a live book is never crossed
    naive = replay([m for m in msgs if "seq" in m]).drop_duplicates("seq", keep="last")   # arrival order, no logic
    rate = bbo.groupby(bbo.t // 1 / 60).size()                          # messages per second, x in minutes
    ax = rate.plot(figsize=(10, 3.5), lw=0.8)
    ax.axvspan(st.t.min() / 60, st.t.max() / 60, color="tab:red", alpha=0.3)
    save(ax, "feed_health", f"Feed is bursty (median {rate.median():.0f}, peak {rate.max()} msgs/s); red = book "
         f"stale {st.t.max() - st.t.min():.0f} s after a seq gap", "minutes after 14:30 UTC open", "messages / second")
    trades = pd.DataFrame([(m["seq"], tick(m["px"])) for m in ordered if m["type"] == "trade"], columns=["seq", "px"])
    return bbo, trades, 1 - wrong(live), wrong(naive), (naive.bid >= naive.ask).sum(), st.t.max() - st.t.min()

def market(bbo, trades):
    live = bbo.assign(dur_s=bbo.t.diff().shift(-1).fillna(0),            # dur_s = how long this BBO state lasted
                      spread=bbo.ask - bbo.bid)[~bbo.stale]
    tr = trades.merge(live[["seq", "t", "bid", "ask"]], on="seq")       # DECISION: row at trade seq = book BEFORE it
    tr["side"] = np.sign(2 * tr.px - tr.bid - tr.ask)                   # quote rule: above mid = buyer-initiated
    truth = pd.read_parquet(DATA / "truth_trades.parquet")
    acc = tr.merge(truth, on="seq").pipe(lambda d: d.side == d.aggressor.map({"B": 1, "S": -1})).mean()
    start = (tr.t // ZOOM_S * ZOOM_S).value_counts().idxmax()           # busiest window by trade count
    w, wt = (d[d.t.between(start, start + ZOOM_S)] for d in (live, tr))
    ax = (w.set_index(w.t - start)[["bid", "ask"]] / 100).plot(drawstyle="steps-post", figsize=(10, 4))
    for s, mk, name in ((1, "^", "buyer-initiated trade"), (-1, "v", "seller-initiated trade")):
        ax.scatter(wt.t[wt.side == s] - start, wt.px[wt.side == s] / 100, marker=mk, s=25, c="k", label=name, zorder=3)
    ax.legend(fontsize=8, loc="upper left")
    save(ax, "bbo_trades", f"Busiest {ZOOM_S} s: buys print at the ask, sells at the bid",
         f"seconds after {w.ts.iloc[0]:%H:%M:%S} UTC", "price ($)")
    s = live.spread.clip(upper=5).astype(str).replace("5", "5+")
    pct = pd.DataFrame({"% of book updates": s.value_counts(normalize=True),
                        "% of time": live.dur_s.groupby(s).sum() / live.dur_s.sum()}).sort_index() * 100
    ax = pct.plot.bar(rot=0, figsize=(7, 4), color=["#b4b3ad", "tab:blue"])
    save(ax, "spread_dist", f"1-tick spread {pct.iloc[0, 1]:.0f}% of the time but only {pct.iloc[0, 0]:.0f}% "
         "of updates: wide spreads close fast", "spread (ticks)", "share (%)")
    return live, live.spread.mean(), np.average(live.spread, weights=live.dur_s), len(tr), acc

def imbalance(live):
    nxt = (live.bid + live.ask).diff().shift(-1).replace(0, np.nan).bfill()   # DECISION: NEXT non-zero mid move
    imb = (live.bid_qty - live.ask_qty) / (live.bid_qty + live.ask_qty)
    df = pd.DataFrame({"imb": imb, "up": nxt > 0})[nxt.notna()]
    train, test = df.iloc[: len(df) // 2], df.iloc[len(df) // 2:]
    edges = np.r_[-1, np.quantile(train.imb, np.linspace(0.1, 0.9, 9)), 1]   # DECISION: deciles from 1st half only
    p_up = lambda d: d.groupby(pd.cut(d.imb, edges, labels=False, include_lowest=True)).up.mean() * 100
    table = pd.DataFrame({"first half (deciles fit here)": p_up(train), "second half (out of sample)": p_up(test)})
    ax = table.plot(marker="o", figsize=(7, 4), xticks=range(10))
    ax.axhline(50, color="grey", ls="--", lw=1)
    save(ax, "imbalance_deciles", "Bid-heavy books tick up next, and it holds out of sample",
         "top-of-book imbalance decile (0 = ask-heavy, 9 = bid-heavy)", "P(next mid move is up) (%)")
    return table, ((test.imb > 0) == test.up)[test.imb != 0].mean()

def live_order(msgs, wait_ms):
    """STEP 5 live mode: we can't sort a file we don't have yet. Release msgs in seq order, hold early ones in a
    heap, and give up on a missing seq once the oldest held msg has waited wait_ms (clock = newest ts seen)."""
    # Production: NIC thread -> lock-free SPSC ring -> this loop on one pinned thread per book (single writer, no locks)
    heap, out, held, nxt, now = [], [], [], 1, 0         # timeout is checked on each arrival (live: also a timer)
    for j, m in enumerate(m for m in msgs if "seq" in m):
        now = max(now, m["ts"])
        if m["seq"] >= nxt:                                   # else: duplicate, or too late (we gave up on it)
            heapq.heappush(heap, (m["seq"], j, now, m))       # j (arrival no.) breaks ties between duplicates
        while heap and (heap[0][0] <= nxt or now - min(h[2] for h in heap) >= wait_ms * 1e6):   # heap stays tiny
            seq, i, t0, m2 = heapq.heappop(heap)
            if seq >= nxt:                                    # seq > nxt: timed out, skip the gap -> book goes stale
                out.append(m2), held.append((j > i, (now - t0) / 1e6))   # (had to wait?, how long in ms)
                nxt = seq + 1
    return out, np.array(held)

def live_mode(msgs, bbo):
    ref, rows, speed = bbo.set_index("seq")[COLS], [], []     # the offline book, verified vs truth in step 2
    for w in WAITS_MS:
        t = time.perf_counter()
        out, held = live_order(msgs, w)
        b = replay(out)
        speed.append(len(out) / (time.perf_counter() - t))    # buffer + book, one thread, pure Python
        lv = b[~b.stale]
        assert not lv.set_index("seq")[COLS].ne(ref.loc[lv.seq]).any(axis=None)   # CHECK: stale, but never wrong
        stale_s = b.ts.diff().shift(-1).dt.total_seconds()[b.stale].sum()
        rows.append((w, stale_s, held[:, 0].mean() * 100, held[:, 1].max()))
    table = pd.DataFrame(rows, columns=["wait_ms", "stale_s", "held_%", "max_hold_ms"]).set_index("wait_ms")
    pick = table.index[table.stale_s <= table.stale_s.min() + 1][0]   # smallest budget that is as good as offline
    return table, pick, min(speed), min(speed) / bbo.groupby(bbo.t // 1).size().max()

def main():
    PLOTS.mkdir(parents=True, exist_ok=True)
    msgs = load()
    bbo, trades, ok, bad, crossed, stale_s = rebuild(msgs)
    print(f"STEP 2 rebuild: stale rows={bbo.stale.sum():,}  live rows matching truth={ok:.2%}\n"
          f"  naive arrival-order replay: {bad:.2%} rows wrong, {crossed:,} crossed")
    live, ev, tw, n, acc = market(bbo, trades)
    print(f"STEP 3 market: spread event-weighted={ev:.2f} ticks, time-weighted={tw:.2f} ticks\n"
          f"  quote rule vs truth_trades: {acc:.1%} right on {n:,} live trades")
    table, oos = imbalance(live)
    print(f"STEP 4 imbalance: P(next mid move up) by decile, %\n{table.round().astype(int).T.to_string()}\n"
          f"  out of sample, sign(imbalance) calls the next move {oos:.1%}")
    buf, pick, speed, headroom = live_mode(msgs, bbo)
    print(f"STEP 5 live: reorder-buffer budget vs staleness vs added delay\n{buf.round(2).to_string()}\n"
          f"  smallest budget as good as offline: {pick} ms | {speed:,.0f} msgs/s = {headroom:,.0f}x the peak second")
    print("CONCLUSION",
          f"- Book = exchange BBO on {ok:.2%} of live rows; naive arrival order: {bad:.2%} wrong, {crossed:,} crossed",
          f"- Spread {tw:.2f} ticks per unit time vs {ev:.2f} per update: wide spreads come in bursts and close fast",
          f"- Imbalance calls the next mid move {oos:.0%} of the time out of sample (coin flip = 50%)",
          f"- Live: a {pick} ms reorder buffer is as good as sorting ({buf['held_%'].iloc[-1]:.1f}% of msgs wait); 0 ms "
          f"leaves the book stale {buf.stale_s.iloc[0]:,.0f} of 1,800 s", sep="\n")
    print("LIMITATIONS",
          f"- A real gap still costs {stale_s:.0f} s until the next snapshot (ask for a retransmit); "
          f"the {pick} ms budget is fit on one session",
          "- Quote rule is exact only because every trade prints at the touch; real feeds have hidden/midpoint fills",
          "- Next-move direction is not P&L (spread, latency, queue position); one synthetic 30-min session", sep="\n")

if __name__ == "__main__":
    main()
