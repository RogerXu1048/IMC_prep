"""F5: why do some of our orders take ~40 ms to be acknowledged?  Run: python3 solution.py"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA, PLOTS = ROOT / "data" / "p09", ROOT / "plots"
SLOW_US = 1_000          # DECISION: a healthy order takes ~30 us, so > 1 ms is clearly broken
DELACK_MS = 40           # LIMITATION: assumed delayed-ACK timer on the exchange side (Linux minimum)
MIN_RTT_NS = 20_000      # DECISION: the exchange cannot ACK our header before it has arrived
STAGES = ["app_to_wire_us", "wire_to_ack_us", "total_us"]


def load():
    # DECISION: exch_ack_ns has blanks -> float64 would round epoch ns (> 2^53) to 256 ns; use Int64
    o = pd.read_csv(DATA / "orders.csv", dtype={"exch_ack_ns": "Int64"})
    inb, ses = pd.read_csv(DATA / "inbound.csv"), pd.read_csv(DATA / "sessions.csv")
    bad = ((o.wire_first_ns < o.app_send_ns) | (o.wire_last_ns < o.wire_first_ns)
           | (o.exch_ack_ns < o.wire_last_ns)).sum()
    print(f"DATA  orders={len(o):,}  sessions={len(ses)}  inbound={inb.kind.value_counts().to_dict()}\n"
          f"DATA  no ack={o.exch_ack_ns.isna().sum()} (kept, excluded from ack stats)  "
          f"duplicate msg_id={o.msg_id.duplicated().sum()}  out-of-order timestamps={bad}")
    # CHECK: broken causality = broken clocks or capture -> stop, don't silently drop rows
    assert bad == 0 and o.msg_id.is_unique
    return o, inb, ses


def step1_stages(o):
    us = lambda a, b: (o[b] - o[a]).astype("float64") / 1e3     # subtract in int ns, then convert
    o["app_to_wire_us"] = us("app_send_ns", "wire_last_ns")     # our host
    o["wire_to_ack_us"] = us("wire_last_ns", "exch_ack_ns")     # network + exchange
    o["total_us"] = us("app_send_ns", "exch_ack_ns")
    o["hold_us"] = us("wire_first_ns", "wire_last_ns")          # first byte -> last byte on the wire
    return o[STAGES].median().round(1).to_dict()


def step2_distributions(o):
    # DECISION: percentiles + log-x ECDF, not means: the body is ~30 us, the problem is ~40 ms
    t = o.groupby("session")[STAGES].quantile([0.5, 0.99]).unstack()
    t.columns = [f"{c[:-3]}_p{q * 100:g}" for c, q in t.columns]
    t["pct_over_1ms"] = o.groupby("session").total_us.agg(lambda x: x.dropna().gt(SLOW_US).mean() * 100)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharex=True, sharey=True, layout="constrained")
    for ax, col, name in zip(axes, ["total_us", "wire_to_ack_us"], ["total: send() -> ack", "network: wire -> ack"]):
        for s, g in o.groupby("session"):
            x = np.sort(g[col].dropna())
            ax.plot(x, np.arange(1, len(x) + 1) / len(x), label=s)
        ax.set(xscale="log", xlabel=f"{name} latency (us, log scale)", ylabel="fraction of orders <= x")
        ax.xaxis.set_major_formatter("{x:,.0f}")      # plain numbers, not 10^k, on the log axis
    axes[0].legend(title="session")
    fig.suptitle(f"Only s3 is slow ({t.pct_over_1ms['s3']:.0f}% of its orders > 1 ms); the network leg is identical")
    fig.savefig(PLOTS / "f5_latency_ecdf.png", dpi=110)
    return t.round(1)


def step3_drilldown(o, ses):
    df = o.merge(ses, on="session", how="left", validate="many_to_one")
    # DECISION: slow = app->wire > 1 ms, since step 2 showed the network leg is the same everywhere
    df["slow"] = df.app_to_wire_us > SLOW_US
    tab = df.groupby(["session", "app_version", "kernel", "colo_rack", "tcp_nodelay", "write_calls"]).agg(
        n=("slow", "size"), pct_slow=("slow", "mean"), p50_app_to_wire_us=("app_to_wire_us", "median"))
    return df, tab.assign(pct_slow=tab.pct_slow * 100).round(1)


def step4_evidence_impact(df, inb):
    s3 = df[df.session == "s3"]
    held = s3[(s3.write_calls == 2) & (s3.hold_us > SLOW_US)]           # header sent, body stuck
    held = held.assign(probe_ns=held.wire_first_ns + MIN_RTT_NS).sort_values("probe_ns")
    # DECISION: carriers = FILL/HEARTBEAT only. The order's own ACK comes after the release
    # (circular), and Nagle only sent the header once all earlier orders were already ACKed.
    carriers = inb.loc[inb.kind != "ACK", ["session", "rx_ns"]].sort_values("rx_ns")
    held = pd.merge_asof(held, carriers, left_on="probe_ns", right_on="rx_ns", by="session", direction="forward")
    held = held.assign(to_next_ms=(held.rx_ns - held.wire_first_ns) / 1e6, hold_ms=held.hold_us / 1e3)
    by_pkt = held.to_next_ms < DELACK_MS - 1
    queued = s3[s3.slow & ~s3.msg_id.isin(held.msg_id)]      # slow, but not split-and-held itself
    fig, ax = plt.subplots(figsize=(7, 5), layout="constrained")
    ax.scatter(held.to_next_ms, held.hold_ms, s=4, alpha=0.3)
    ax.plot([0, DELACK_MS], [0, DELACK_MS], "r--", lw=1, label="hold = time to next inbound packet")
    ax.axhline(DELACK_MS, color="gray", ls=":", label="40 ms delayed-ACK timer")
    ax.set(xlim=(0, 120), ylim=(0, 45), xlabel="header on wire -> next inbound packet (ms, cut at 120)",
           ylabel="body held in our kernel (ms)", title="s3: body leaves at the next inbound packet, else at 40 ms")
    ax.legend(loc="lower right")
    fig.savefig(PLOTS / "f5_hold_vs_next_inbound.png", dpi=110)
    ok = df.dropna(subset=["total_us"])
    # DECISION: projection = fleet without s3, i.e. s3 behaves like s1/s2/s4 after the fix
    return dict(held=len(held), pct_pkt=by_pkt.mean() * 100, timer_ms=held.hold_ms[~by_pkt].median(),
                lag_us=(held.hold_ms - held.to_next_ms)[by_pkt].median() * 1e3,
                pct_same_ns=queued.wire_last_ns.isin(held.wire_last_ns).mean() * 100,
                n_slow=ok.total_us.gt(SLOW_US).sum(), share=ok.total_us.gt(SLOW_US).mean(),
                p99_now=ok.total_us.quantile(0.99), p99_fixed=ok.total_us[ok.session != "s3"].quantile(0.99))


def main():
    PLOTS.mkdir(exist_ok=True)
    o, inb, ses = load()
    print("\nSTEP 1  median latency (us):", step1_stages(o))
    print("\nSTEP 2  latency percentiles by session (us):\n" + step2_distributions(o).to_string())
    df, tab = step3_drilldown(o, ses)
    print("\nSTEP 3  slow = app->wire > 1 ms, by config:\n" + tab.to_string())
    r = step4_evidence_impact(df, inb)
    print(f"\nSTEP 4  {r['held']:,} s3 bodies held > 1 ms: {r['pct_pkt']:.1f}% leave {r['lag_us']:.2f} us (median) "
          f"after an inbound packet, {100 - r['pct_pkt']:.1f}% at the timer (median {r['timer_ms']:.2f} ms)\n"
          f"        other slow s3 msgs leaving at the same ns as a held body: {r['pct_same_ns']:.1f}%\n"
          f"        orders > 1 ms: {r['n_slow']:,} ({r['share']:.1%}); p99 now -> fixed: "
          f"{r['p99_now']:,.0f} -> {r['p99_fixed']:,.0f} us")
    print(f"\nCONCLUSION\n- {r['share']:.1%} of orders, all on s3, take ~40 ms, not ~30 us; the network leg is "
          "the same everywhere, so the time is lost on our host.\n- Cause: oe-2.4.0 sends NewOrder in 2 send() "
          "calls and s3 has TCP_NODELAY off: Nagle holds the body until the exchange's delayed ACK (<= 40 ms). "
          "Control: s4 = same build, NODELAY on, 0% slow; kernel/rack irrelevant.\n- Fix: TCP_NODELAY=1 + one "
          f"send() per message: p99 {r['p99_now']:,.0f} -> ~{r['p99_fixed']:,.0f} us; verify by canary on s3 vs s4.")
    print("\nLIMITATIONS\n- Mechanism is inferred from timestamps, not seen in a pcap (no TCP-level ACKs here).\n"
          "- One hour of data; the projection assumes s3 then looks like s1/s2/s4 and nothing else changes.\n"
          "- The exchange's delayed-ACK timer (40 ms, the Linux default) is assumed, not measured.")


if __name__ == "__main__":
    main()
