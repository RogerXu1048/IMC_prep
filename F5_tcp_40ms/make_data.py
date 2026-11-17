"""Generate synthetic order-entry captures for P09 ("the 40 ms mystery").

Writes ../data/p09/orders.csv, ../data/p09/inbound.csv and ../data/p09/sessions.csv.
Deterministic (fixed seed). Don't read this before attempting the problem: the simulation
below is the answer key.
"""
from bisect import bisect_left, insort
from pathlib import Path
import time

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "data" / "p09"
SEED = 9_040
T0 = pd.Timestamp("2026-11-12 14:30:00", tz="UTC").value      # epoch ns of window start
WINDOW_NS = 3600 * 1_000_000_000

SESSIONS = pd.DataFrame([
    # session, host, kernel, app_version, tcp_nodelay, so_sndbuf, colo_rack
    ("s1", "oe-host-01", "4.18.0-513.el8", "oe-2.3.7", 1, 262144, "R12"),
    ("s2", "oe-host-02", "5.14.0-362.el9", "oe-2.3.7", 1, 262144, "R07"),
    ("s3", "oe-host-03", "4.18.0-513.el8", "oe-2.4.0", 0, 262144, "R07"),
    ("s4", "oe-host-04", "4.18.0-513.el8", "oe-2.4.0", 1, 262144, "R07"),
], columns=["session", "host", "kernel", "app_version", "tcp_nodelay", "so_sndbuf", "colo_rack"])

N_MSGS = {"s1": 54_000, "s2": 54_000, "s3": 27_000, "s4": 45_000}
MSG_TYPES = np.array(["NewOrder", "Cancel", "Modify"])
TYPE_P = [0.50, 0.35, 0.15]
TYPE_BYTES = {"NewOrder": (88, 112), "Cancel": (44, 52), "Modify": (64, 80)}
UNSOLICITED_RATE = 8.0             # FILL / drop-copy messages per second per session
DELACK_NS = 40_000_000             # receiver's delayed-ACK timer (Linux TCP_DELACK_MIN)
MIN_RTT_NS = 24_000                # an inbound packet can only ACK data that reached the peer


def send_times(rng, n):
    """Bursty strategy sends (ns since T0): clusters of 1..k orders a few us apart."""
    mean_burst = 2.2
    n_bursts = int(n / mean_burst)
    starts = np.sort(rng.uniform(0, WINDOW_NS - 1e9, n_bursts))
    sizes = rng.geometric(1 / mean_burst, n_bursts)
    burst_of = np.repeat(np.arange(n_bursts), sizes)
    gaps = rng.exponential(8_000, len(burst_of))           # ~8 us between orders in a burst
    first = np.r_[0, np.cumsum(sizes)[:-1]]
    gaps[first] = 0
    csum = np.cumsum(gaps)
    t = starts[burst_of] + csum - np.repeat(csum[first], sizes)
    return np.sort(t)


def unsolicited_inbound(rng):
    """Inbound packets on a session that are not ACKs of our orders: fills + 1s heartbeats."""
    n_fill = rng.poisson(UNSOLICITED_RATE * WINDOW_NS / 1e9)
    fills = rng.uniform(0, WINDOW_NS, n_fill)
    hb = np.arange(rng.uniform(0, 1e9), WINDOW_NS, 1e9)
    times = np.r_[fills, hb]
    kinds = np.r_[np.full(n_fill, "FILL"), np.full(len(hb), "HEARTBEAT")]
    order = np.argsort(times)
    return times[order], kinds[order]


def simulate_session(rng, session, cfg, msg_id_start):
    n = N_MSGS[session]
    t = send_times(rng, n)
    n = len(t)
    mtype = MSG_TYPES[rng.choice(3, n, p=TYPE_P)]
    nbytes = np.array([rng.integers(*TYPE_BYTES[m]) for m in mtype])
    two_writes = (cfg.app_version == "oe-2.4.0") & (mtype == "NewOrder")
    write_calls = np.where(two_writes, 2, 1)
    stack = rng.lognormal(np.log(3_500), 0.35, n)                 # syscall + TCP/IP + driver
    rtt = rng.lognormal(np.log(27_000), 0.22, n) + (2_000 if cfg.colo_rack == "R12" else 0)
    second_syscall = rng.lognormal(np.log(1_300), 0.3, n)
    react = rng.lognormal(np.log(1_500), 0.3, n)                   # kernel reacts to an ACK
    unsol_t, unsol_kind = unsolicited_inbound(rng)

    wire_first = np.empty(n)
    wire_last = np.empty(n)
    exch_ack = np.empty(n)

    if cfg.tcp_nodelay == 1:
        # Every send() goes straight to the wire.
        wire_first[:] = t + stack
        wire_last[:] = wire_first + np.where(two_writes, second_syscall, nbytes * 0.8)
        exch_ack[:] = wire_last + rtt
    else:
        # Nagle: a small segment may not be sent while previously sent data is un-ACKed.
        acks_sorted = []                     # arrival times of exchange packets (ACK carriers)
        inflight_ack = -np.inf               # when the data currently in flight gets ACKed
        hold_until = -np.inf                 # body of a split message waiting for an ACK

        def next_inbound_after(x):
            cands = []
            i = np.searchsorted(unsol_t, x)
            if i < len(unsol_t):
                cands.append(unsol_t[i])
            j = bisect_left(acks_sorted, x)
            if j < len(acks_sorted):
                cands.append(acks_sorted[j])
            return min(cands) if cands else np.inf

        for i in range(n):
            ready = t[i] + stack[i]
            if ready < hold_until:
                # queued in the socket buffer behind the held body -> coalesced into the
                # segment that is released when the ACK finally arrives
                wire_first[i] = wire_last[i] = hold_until
                exch_ack[i] = wire_last[i] + rtt[i]
                insort(acks_sorted, exch_ack[i])
                inflight_ack = max(inflight_ack, exch_ack[i])
                continue
            first = ready if ready >= inflight_ack else inflight_ack + react[i]
            if write_calls[i] == 1:
                wire_first[i] = wire_last[i] = first
            else:
                # header goes out; body is held until the header is ACKed. The receiver has
                # an incomplete message, so it has nothing to say -> delayed ACK, unless some
                # other packet it sends us (ACK of an earlier order, a fill) carries the ACK.
                piggyback = next_inbound_after(first + MIN_RTT_NS)
                timer = first + DELACK_NS + rng.exponential(300_000)
                release = min(piggyback, timer) + react[i]
                wire_first[i] = first
                wire_last[i] = release
                hold_until = release
            exch_ack[i] = wire_last[i] + rtt[i]
            insort(acks_sorted, exch_ack[i])
            inflight_ack = max(inflight_ack, exch_ack[i])

    orders = pd.DataFrame({
        "session": session,
        "msg_id": np.arange(msg_id_start, msg_id_start + n),
        "msg_type": mtype,
        "bytes": nbytes,
        "write_calls": write_calls,
        "app_send_ns": T0 + np.round(t).astype(np.int64),
        "wire_first_ns": T0 + np.round(wire_first).astype(np.int64),
        "wire_last_ns": T0 + np.round(wire_last).astype(np.int64),
        "exch_ack_ns": T0 + np.round(exch_ack).astype(np.int64),
    })
    # a few orders were rejected / never acknowledged inside the capture
    lost = rng.random(n) < 0.001
    orders["exch_ack_ns"] = orders["exch_ack_ns"].astype("Int64").mask(lost)

    inbound = pd.concat([
        pd.DataFrame({"session": session, "rx_ns": orders.loc[~lost, "exch_ack_ns"].astype(np.int64),
                      "kind": "ACK", "msg_id": orders.loc[~lost, "msg_id"]}),
        pd.DataFrame({"session": session, "rx_ns": T0 + np.round(unsol_t).astype(np.int64),
                      "kind": unsol_kind, "msg_id": pd.NA}),
    ], ignore_index=True)
    return orders, inbound


def main():
    t_start = time.time()
    rng = np.random.default_rng(SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    all_orders, all_inbound = [], []
    msg_id = 1_000_000
    for cfg in SESSIONS.itertuples(index=False):
        o, ib = simulate_session(rng, cfg.session, cfg, msg_id)
        msg_id += len(o) + 100_000
        all_orders.append(o)
        all_inbound.append(ib)
    orders = pd.concat(all_orders).sort_values("app_send_ns", kind="stable")
    inbound = pd.concat(all_inbound).sort_values("rx_ns", kind="stable")
    inbound["msg_id"] = inbound["msg_id"].astype("Int64")

    files = {"orders.csv": orders, "inbound.csv": inbound, "sessions.csv": SESSIONS}
    for name, df in files.items():
        path = OUT / name
        df.to_csv(path, index=False)
        print(f"wrote {path.relative_to(OUT.parent.parent)}  rows={len(df):,}  "
              f"size={path.stat().st_size / 1e6:.1f} MB")
    print(f"done in {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
