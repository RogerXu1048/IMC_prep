"""F5: why do some of our orders take 40 ms? Root-cause analysis of the order-entry send path.

Run: python3 make_data.py && python3 solution.py   (figures -> ../plots/f5_*.png)
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA, PLOTS = ROOT / "data" / "p09", ROOT / "plots"
TS_COLS = ["app_send_ns", "wire_first_ns", "wire_last_ns", "exch_ack_ns"]
SLOW_US = 1_000          # DECISION: healthy order entry is ~30 us; anything > 1 ms is pathological
MIN_RTT_NS = 20_000      # the exchange cannot ACK our header before it has arrived there
DELACK_MS = 40           # Linux receiver delayed-ACK minimum (TCP_DELACK_MIN)
PCTS = [0.5, 0.9, 0.99, 0.999]


def us(df: pd.DataFrame, start: str, end: str) -> pd.Series:
    return (df[end] - df[start]).astype("float64") / 1e3


def step1_load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # DECISION: epoch-ns columns as nullable Int64. With blanks, read_csv would infer float64 and
    # 1.8e18 > 2^53 -> timestamps silently rounded to 256 ns. Subtract in int, convert at the end.
    orders = pd.read_csv(DATA / "orders.csv", dtype={c: "Int64" for c in TS_COLS})
    inbound = pd.read_csv(DATA / "inbound.csv", dtype={"rx_ns": "int64", "msg_id": "Int64"})
    sessions = pd.read_csv(DATA / "sessions.csv")

    # CHECK: causality. If any of these fire, the capture or the clocks are broken -> stop.
    violations = {
        "wire_first < app_send": int((orders.wire_first_ns < orders.app_send_ns).sum()),
        "wire_last < wire_first": int((orders.wire_last_ns < orders.wire_first_ns).sum()),
        "ack < wire_last": int((orders.exch_ack_ns < orders.wire_last_ns).sum()),
    }
    assert not any(violations.values()), violations
    assert orders.msg_id.is_unique

    # Split the latency into stages: on our host (app->wire) vs network+exchange (wire->ack).
    orders["app_to_wire_us"] = us(orders, "app_send_ns", "wire_last_ns")
    orders["hold_us"] = us(orders, "wire_first_ns", "wire_last_ns")   # first byte -> last byte
    orders["wire_to_ack_us"] = us(orders, "wire_last_ns", "exch_ack_ns")
    orders["total_us"] = us(orders, "app_send_ns", "exch_ack_ns")
    print(f"orders={len(orders):,}  inbound={len(inbound):,}  sessions={len(sessions)}")
    print(f"no exchange ack in capture: {orders.exch_ack_ns.isna().sum()} (kept, excluded from ack stats)")
    print(f"ordering violations: {violations}")
    return orders, inbound, sessions


def step2_distributions(orders: pd.DataFrame) -> None:
    # DECISION: percentiles + ECDF on a log axis, never just the mean: the body (~30 us) and a
    # possible tail (ms) differ by 3 orders of magnitude.
    for col in ["app_to_wire_us", "wire_to_ack_us", "total_us"]:
        print(f"{col} percentiles by session:")
        print(orders.groupby("session")[col].quantile(PCTS).unstack().round(1).to_string())
    slow_pct = (orders.total_us > SLOW_US).groupby(orders.session).mean().mul(100).round(2)
    print(f"% orders > 1 ms by session: {slow_pct.to_dict()}")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for (sess, g) in orders.groupby("session"):
        for ax, col in zip(axes, ["total_us", "wire_to_ack_us"]):
            x = np.sort(g[col].dropna().to_numpy())
            ax.plot(x, np.arange(1, len(x) + 1) / len(x), label=sess)
    for ax, title in zip(axes, ["app send -> exchange ack", "wire -> exchange ack"]):
        ax.set(xscale="log", xlabel=f"{title} (us, log)", ylabel="ECDF", title=title)
        ax.legend()
        ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(PLOTS / "f5_latency_ecdf.png", dpi=110)
    plt.close(fig)


def step3_drilldown(orders: pd.DataFrame, sessions: pd.DataFrame) -> pd.DataFrame:
    df = orders.merge(sessions, on="session", how="left", validate="many_to_one")
    df["slow"] = df.app_to_wire_us > SLOW_US
    keys = ["session", "kernel", "app_version", "tcp_nodelay", "colo_rack", "write_calls"]
    tab = df.groupby(keys).agg(n=("slow", "size"), slow_pct=("slow", "mean"),
                               p50_app_to_wire=("app_to_wire_us", "median"),
                               p50_wire_to_ack=("wire_to_ack_us", "median"))
    tab["slow_pct"] *= 100
    print(tab.round(2).to_string())
    # DECISION: s4 is the natural control (same app version, same 2-write split, same kernel and
    # rack as s3) -> the only difference left is tcp_nodelay.
    print("hypothesis: Nagle (tcp_nodelay=0) holds the 2nd write until the 1st is ACKed; the exchange")
    print("            delays that ACK (half a message, nothing to piggy-back on) -> up to 40 ms")
    return df


def step4_hold_vs_next_inbound(df: pd.DataFrame, inbound: pd.DataFrame) -> pd.DataFrame:
    s3 = df[df.session == "s3"].sort_values("app_send_ns").reset_index(drop=True)
    # Candidate ACK carriers: (a) unsolicited packets (fills, heartbeats);
    # (b) exchange acks of EARLIER orders. TRADE-OFF: including the held order's own ack (or later
    # ones) would put every point on the diagonal -- a circular argument.
    s3["prev_ack_max"] = s3.exch_ack_ns.astype("float64").cummax().shift(1)
    held = s3[(s3.write_calls == 2) & (s3.hold_us > SLOW_US)].copy()
    probe = held.wire_first_ns.astype("int64").to_numpy() + MIN_RTT_NS
    unsol = np.sort(inbound.loc[(inbound.session == "s3") & (inbound.kind != "ACK"), "rx_ns"].to_numpy())

    # The algorithm: both arrays are sorted -> binary search (or a two-pointer sweep) finds the
    # first carrier after each probe in O(h log m). pd.merge_asof(direction="forward") is equivalent.
    idx = np.searchsorted(unsol, probe, side="left")
    next_unsol = np.where(idx < len(unsol), unsol[np.minimum(idx, len(unsol) - 1)], np.inf)
    prev_ack = held.prev_ack_max.to_numpy()
    next_ack = np.where(prev_ack > probe, prev_ack, np.inf)
    held["to_next_ms"] = (np.minimum(next_unsol, next_ack) - (probe - MIN_RTT_NS)) / 1e6
    held["hold_ms"] = held.hold_us / 1e3

    early = held.to_next_ms < DELACK_MS - 1
    lag_us = (held.hold_ms - held.to_next_ms)[early] * 1e3
    late = held.hold_ms[~early]
    print(f"held split messages on s3: {len(held):,} of {(s3.write_calls == 2).sum():,}")
    print(f"released by an inbound packet: {early.mean():.1%}, body left {lag_us.median():.2f} us after it "
          f"(p99 {lag_us.quantile(.99):.2f} us)")
    print(f"no packet within 39 ms: {(~early).mean():.1%}, hold p1/p50/p99 = "
          f"{late.quantile(.01):.2f}/{late.median():.2f}/{late.quantile(.99):.2f} ms (delayed-ACK timer)")

    # CHECK (falsifiable): slow single-write messages should leave at the same ns as a held body
    single_slow = s3[(s3.write_calls == 1) & (s3.app_to_wire_us > SLOW_US)]
    coalesced = single_slow.wire_last_ns.isin(held.wire_last_ns).mean()
    print(f"slow single-write msgs on s3: {len(single_slow):,}; {coalesced:.1%} left with a held body "
          "(queued behind it in the send buffer)")

    fig, ax = plt.subplots(figsize=(7, 5.5))
    ax.scatter(held.to_next_ms, held.hold_ms, s=3, alpha=0.3)
    ax.plot([0, DELACK_MS], [0, DELACK_MS], "r--", lw=1, label="hold = time to next inbound packet")
    ax.axhline(DELACK_MS, color="gray", ls=":", label="40 ms delayed-ACK timer")
    ax.set(xlim=(0, 120), ylim=(0, 45), xlabel="header on wire -> next inbound packet (ms)",
           ylabel="body held (ms)", title="s3: body waits for the next ACK-carrying packet")
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(PLOTS / "f5_hold_vs_next_inbound.png", dpi=110)
    plt.close(fig)
    return held


def step5_impact(df: pd.DataFrame) -> None:
    slow = df.total_us > SLOW_US
    print(f"orders > 1 ms: {slow.sum():,} of {df.total_us.notna().sum():,} ({slow.mean():.2%}); "
          f"all on s3: {df.loc[slow, 'session'].eq('s3').all()}")
    # DECISION: counterfactual = give each s3 order the app->wire delay of a random s4 order of the
    # same type (s4 = same build with TCP_NODELAY on); keep s3's own network leg.
    # TRADE-OFF: assumes s3 and s4 traffic patterns are comparable and the network leg is unchanged.
    rng = np.random.default_rng(1)
    fixed = df.copy()
    for mtype, g in df[df.session == "s3"].groupby("msg_type"):
        donor = df.loc[(df.session == "s4") & (df.msg_type == mtype), "app_to_wire_us"].dropna()
        fixed.loc[g.index, "app_to_wire_us"] = rng.choice(donor.to_numpy(), len(g))
    fixed["total_us"] = fixed.app_to_wire_us + fixed.wire_to_ack_us
    rows = {}
    for name, frame in [("now", df), ("after fix", fixed)]:
        rows[(name, "all")] = frame.total_us.quantile([0.5, 0.99, 0.999])
        rows[(name, "s3")] = frame.loc[frame.session == "s3", "total_us"].quantile([0.5, 0.99, 0.999])
    print(pd.DataFrame(rows).T.round(1).rename(columns=lambda q: f"p{q * 100:g}").to_string())
    print("fix: setsockopt(TCP_NODELAY, 1) on s3 + write each message with ONE send()/writev")
    print("verify: canary on s3 vs s4; pcap shows 2nd segment immediately; `ss -tni`; strace send() count")


def main() -> None:
    PLOTS.mkdir(exist_ok=True)
    print("== step 1: load + stage latencies")
    orders, inbound, sessions = step1_load()
    print("\n== step 2: distributions by session")
    step2_distributions(orders)
    print("\n== step 3: what distinguishes the slow orders?")
    df = step3_drilldown(orders, sessions)
    print("\n== step 4: hold time vs next inbound packet (s3)")
    step4_hold_vs_next_inbound(df, inbound)
    print("\n== step 5: impact + fix")
    step5_impact(df)


if __name__ == "__main__":
    main()
