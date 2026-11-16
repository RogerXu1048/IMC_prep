"""P09 reference solution: the 40 ms order-entry mystery.

Run:  python3 solution.py        (after python3 make_data.py)
Figures are written to ../plots/p09_*.png
"""
from pathlib import Path
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p09"
PLOTS = ROOT / "plots"
PLOTS.mkdir(exist_ok=True)

SLOW_US = 1_000           # anything above 1 ms is pathological for order entry
TS_COLS = ["app_send_ns", "wire_first_ns", "wire_last_ns", "exch_ack_ns"]


def pctl(s, ps=(50, 90, 99, 99.9)):
    s = s.dropna()
    return pd.Series({f"p{p:g}": np.percentile(s, p) for p in ps} | {"n": len(s)})


# ----------------------------------------------------------------------------- Part A
def part_a_load():
    print("\n=== Part A: load + stage latencies ===")
    # exch_ack_ns has blanks (no ack). Default read_csv would make it float64, and epoch ns
    # (~1.8e18 > 2^53) cannot be represented exactly in float64 -> silent 256 ns rounding.
    # Read all timestamps as nullable Int64 instead.
    orders = pd.read_csv(DATA / "orders.csv", dtype={c: "Int64" for c in TS_COLS})
    inbound = pd.read_csv(DATA / "inbound.csv", dtype={"rx_ns": "int64", "msg_id": "Int64"})
    sessions = pd.read_csv(DATA / "sessions.csv")

    orders["app_to_wire_us"] = (orders["wire_last_ns"] - orders["app_send_ns"]) / 1e3
    orders["first_to_last_us"] = (orders["wire_last_ns"] - orders["wire_first_ns"]) / 1e3
    orders["wire_to_ack_us"] = (orders["exch_ack_ns"] - orders["wire_last_ns"]) / 1e3
    orders["total_us"] = (orders["exch_ack_ns"] - orders["app_send_ns"]) / 1e3
    orders = orders.astype({c: "float64" for c in
                            ["app_to_wire_us", "first_to_last_us", "wire_to_ack_us", "total_us"]})

    print(f"orders={len(orders):,} inbound={len(inbound):,} sessions={len(sessions)}")
    print("rows per session:", orders["session"].value_counts().sort_index().to_dict())
    print("missing exch_ack (rejected / not acked in capture):",
          int(orders["exch_ack_ns"].isna().sum()))
    bad = {
        "wire_first < app_send": int((orders["wire_first_ns"] < orders["app_send_ns"]).sum()),
        "wire_last < wire_first": int((orders["wire_last_ns"] < orders["wire_first_ns"]).sum()),
        "ack < wire_last": int((orders["exch_ack_ns"] < orders["wire_last_ns"]).sum()),
    }
    print("ordering violations:", bad)
    print("inbound kinds:", inbound["kind"].value_counts().to_dict())
    return orders, inbound, sessions


# ----------------------------------------------------------------------------- Part B
def part_b_distributions(orders):
    print("\n=== Part B: distributions per session ===")
    for col in ["app_to_wire_us", "wire_to_ack_us", "total_us"]:
        print(f"\n{col}:")
        print(orders.groupby("session")[col].apply(pctl).unstack().round(1).to_string())
    slow = orders.groupby("session")["total_us"].apply(lambda s: (s > SLOW_US).mean() * 100)
    print("\n% orders slower than 1 ms by session:", slow.round(2).to_dict())
    print(f"% of ALL orders slower than 1 ms: {(orders['total_us'] > SLOW_US).mean() * 100:.2f}%")

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    bins = np.logspace(1, np.log10(orders["total_us"].max() * 1.1), 150)
    for sess, g in orders.groupby("session"):
        axes[0].hist(g["total_us"].dropna(), bins=bins, histtype="step", label=sess)
        x = np.sort(g["total_us"].dropna().to_numpy())
        axes[1].plot(x, np.arange(1, len(x) + 1) / len(x), label=sess)
    axes[0].set(xscale="log", yscale="log", xlabel="app send -> exchange ack (us, log)",
                ylabel="count", title="Total latency: one session is bimodal")
    axes[1].set(xscale="log", xlabel="app send -> exchange ack (us, log)", ylabel="ECDF",
                title="ECDF by session")
    for ax in axes:
        ax.axvline(40_000, ls=":", color="gray")
        ax.legend()
        ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(PLOTS / "p09_latency_by_session.png", dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------- Part C
def part_c_drilldown(orders, inbound, sessions):
    print("\n=== Part C: what distinguishes the slow orders? ===")
    df = orders.merge(sessions, on="session", how="left")
    df["slow"] = df["app_to_wire_us"] > SLOW_US

    # 1. Slicing by configuration: slow share and median send-side delay.
    keys = ["session", "kernel", "app_version", "tcp_nodelay", "colo_rack", "write_calls",
            "msg_type"]
    tab = df.groupby(keys).agg(n=("slow", "size"), slow_pct=("slow", "mean"),
                               p50_app_to_wire_us=("app_to_wire_us", "median"),
                               p50_wire_to_ack_us=("wire_to_ack_us", "median"))
    tab["slow_pct"] *= 100
    print(tab.round(2).to_string())
    print("-> s4 runs the same app version and also splits NewOrder into 2 writes, yet is fast;"
          "\n   only s3 (tcp_nodelay=0) is slow. s2's different kernel and s1's farther rack are"
          "\n   red herrings (s1 shows ~+2 us wire->ack, nothing like 40 ms).")

    s3 = df[df["session"] == "s3"].sort_values("app_send_ns").reset_index(drop=True)
    s3_in = inbound[inbound["session"] == "s3"].sort_values("rx_ns")

    # 2. Hold initiators: split messages whose last byte left long after the first byte.
    hold = s3[(s3["write_calls"] == 2) & (s3["first_to_last_us"] > SLOW_US)].copy()
    split = s3[s3["write_calls"] == 2]
    print(f"\ns3 split messages held > 1 ms between first and last byte ('hold initiators'): "
          f"{len(hold):,} of {len(split):,}; another "
          f"{(split['slow'] & (split['first_to_last_us'] <= SLOW_US)).sum():,} were queued "
          "behind an earlier hold (whole message left at the release instant)")

    # 3. Smoking gun: the body is released when the NEXT inbound packet arrives (it carries
    #    the TCP ACK for the header), or after ~40 ms (receiver's delayed-ACK timer).
    #    Candidate ACK carriers are (a) unsolicited packets (fills, heartbeats) and
    #    (b) exchange acks of messages sent BEFORE this header. Careful: the exchange's ack of
    #    the held message itself (or of anything sent after it) arrives right after the
    #    release -- including it makes every point sit on the diagonal (a circular argument).
    #    The exchange can only ACK the header after receiving it, so ignore packets arriving
    #    less than ~one RTT (20 us) after the header left.
    s3["prev_ack_max"] = (s3["exch_ack_ns"].astype("float64").cummax().shift(1))
    hold = hold.merge(s3[["msg_id", "prev_ack_max"]], on="msg_id", how="left")
    hold["probe_ns"] = hold["wire_first_ns"].astype("int64") + 20_000
    unsolicited = s3_in.loc[s3_in["kind"] != "ACK", ["rx_ns", "kind"]]
    nxt = pd.merge_asof(hold.sort_values("probe_ns"), unsolicited,
                        left_on="probe_ns", right_on="rx_ns", direction="forward")
    prev_ack = nxt["prev_ack_max"].where(nxt["prev_ack_max"] > nxt["probe_ns"])
    use_ack = prev_ack < nxt["rx_ns"]
    nxt["kind"] = nxt["kind"].mask(use_ack, "ACK of earlier order")
    nxt["next_in_ns"] = nxt["rx_ns"].astype("float64").mask(use_ack, prev_ack)
    wire_first = nxt["wire_first_ns"].astype("int64")
    nxt["to_next_inbound_us"] = (nxt["next_in_ns"] - wire_first) / 1e3
    nxt["hold_us"] = nxt["first_to_last_us"]
    early = nxt["to_next_inbound_us"] < 39_000
    gap_after = nxt.loc[early, "hold_us"] - nxt.loc[early, "to_next_inbound_us"]
    print(f"Holds where an inbound packet arrived within 39 ms: {early.mean() * 100:.1f}%; "
          f"body left {gap_after.median():.2f} us after that packet (p99 "
          f"{gap_after.quantile(.99):.2f} us) -- released by the piggy-backed ACK")
    late = nxt.loc[~early, "hold_us"] / 1e3
    print(f"Holds with NO inbound packet within 39 ms: {(~early).mean() * 100:.1f}%; hold time "
          f"p1/p50/p99 = {late.quantile(.01):.2f}/{late.median():.2f}/{late.quantile(.99):.2f} ms "
          "-- released by the receiver's 40 ms delayed-ACK timer")
    print("Kind of inbound packet that released early holds:",
          nxt.loc[early, "kind"].value_counts().to_dict())

    fig, ax = plt.subplots(figsize=(7.5, 6))
    ax.scatter(nxt["to_next_inbound_us"] / 1e3, nxt["hold_us"] / 1e3, s=3, alpha=0.3)
    ax.plot([0, 40], [0, 40], "r--", lw=1, label="hold = time to next inbound packet")
    ax.axhline(40, color="gray", ls=":", label="40 ms delayed-ACK timer")
    ax.set(xlim=(0, 120), ylim=(0, 45), xlabel="time from header on wire to next inbound packet (ms)",
           ylabel="time body was held (ms)", title="s3: body waits for the next ACK-carrying packet")
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(PLOTS / "p09_hold_vs_next_inbound.png", dpi=110)
    plt.close(fig)

    # 4. Collateral damage: single-write messages queued behind a held body (coalescing).
    single_slow = s3[(s3["write_calls"] == 1) & s3["slow"]]
    released = set(hold["wire_last_ns"].astype("int64"))
    same_release = single_slow["wire_last_ns"].astype("int64").isin(released).mean()
    print(f"\ns3 single-write messages that are slow: {len(single_slow):,} "
          f"({len(single_slow) / (s3['write_calls'] == 1).sum() * 100:.1f}% of s3 single writes); "
          f"{same_release * 100:.1f}% left the NIC at exactly the same ns as a held body "
          "-> coalesced into the same segment (head-of-line blocking in the send buffer)")

    # 5. Nagle even without the split: back-to-back small sends wait for the previous ACK.
    s3["gap_prev_us"] = s3["app_send_ns"].astype("int64").diff() / 1e3
    s3["app_to_first_us"] = (s3["wire_first_ns"] - s3["app_send_ns"]).astype("float64") / 1e3
    fast_single = s3[(s3["write_calls"] == 1) & ~s3["slow"]]
    by_gap = fast_single.groupby(pd.cut(fast_single["gap_prev_us"], [0, 10, 20, 30, 50, 1e12]),
                                 observed=True)["app_to_first_us"].median()
    print("s3 single writes (not coalesced): median app->first byte by gap since previous send:")
    print(by_gap.round(1).to_string())
    s4 = df[(df["session"] == "s4") & (df["write_calls"] == 1)].sort_values("app_send_ns")
    s4_gap = s4["app_send_ns"].astype("int64").diff() / 1e3
    s4_first = (s4["wire_first_ns"] - s4["app_send_ns"]).astype("float64") / 1e3
    print(f"   compare s4 (nodelay=1), gap<10us: median {s4_first[s4_gap < 10].median():.1f} us "
          "-> on s3 a small send waits for the previous send's ACK (~1 RTT)")
    return df


# ----------------------------------------------------------------------------- Part D
def part_d_impact(df):
    print("\n=== Part D: impact and projected fix ===")
    total = df["total_us"]
    print(f"Orders > 1 ms: {(total > SLOW_US).sum():,} of {total.notna().sum():,} "
          f"({(total > SLOW_US).mean() * 100:.2f}%); all on s3: "
          f"{df.loc[total > SLOW_US, 'session'].eq('s3').all()}")

    # Counterfactual: give each s3 message the send-side delay of a random s4 message of the
    # same type (same app version, tcp_nodelay=1) and keep s3's own network leg.
    rng = np.random.default_rng(1)
    fixed = df.copy()
    for mtype, g in df[df["session"] == "s3"].groupby("msg_type"):
        donor = df.loc[(df["session"] == "s4") & (df["msg_type"] == mtype), "app_to_wire_us"].dropna()
        fixed.loc[g.index, "app_to_wire_us"] = rng.choice(donor.to_numpy(), len(g))
    fixed["total_us"] = fixed["app_to_wire_us"] + fixed["wire_to_ack_us"]

    rows = {}
    for name, frame in (("now", df), ("projected fix", fixed)):
        rows[(name, "all sessions")] = pctl(frame["total_us"])
        rows[(name, "s3 only")] = pctl(frame.loc[frame["session"] == "s3", "total_us"])
    print(pd.DataFrame(rows).T.round(1).to_string())


# ----------------------------------------------------------------------------- Part E
def part_e_recommendation():
    print("\n=== Part E: fix + verification ===")
    print("Root cause: Nagle's algorithm (tcp_nodelay=0 on s3) x split write (oe-2.4.0 sends\n"
          "header and body in two send() calls) x receiver delayed ACK (~40 ms).")
    print("Fix: setsockopt(TCP_NODELAY, 1) on s3 (and in the config template), AND build each\n"
          "message in one buffer (or writev) so it is one send(). Either removes the 40 ms; only\n"
          "TCP_NODELAY also removes the ~1-RTT wait of back-to-back small sends seen above.")
    print("Verify: canary on s3, compare p99/p99.9 + count of >1 ms orders vs s4; pcap shows the\n"
          "second segment leaving immediately; `ss -tni` shows nodelay; strace counts send() calls.")


def main():
    t0 = time.time()
    orders, inbound, sessions = part_a_load()
    part_b_distributions(orders)
    df = part_c_drilldown(orders, inbound, sessions)
    part_d_impact(df)
    part_e_recommendation()
    print(f"\nTotal runtime {time.time() - t0:.1f}s; plots in {PLOTS}")


if __name__ == "__main__":
    main()
