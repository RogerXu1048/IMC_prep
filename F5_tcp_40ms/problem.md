# F5: Why do some of our orders take 40 ms?

**Time box: ~2 hours (aim for ~100 lines of core code)** · Python, pandas, matplotlib · Think out loud.

## Context
Our order-entry gateways keep one TCP session per exchange connection. A healthy order goes from "strategy calls `send()`" to "exchange ack received" in about 30 µs. Since this morning's release, some of our orders take ~40 ms to be acknowledged. Find out why, prove it with the data, and size the impact.

## Data
Run `python3 make_data.py` once; it writes `../data/p09/` (one trading hour, four sessions). Timestamps are epoch nanoseconds from PTP-synced host/NIC clocks.

- **`orders.csv`**, one row per order message we sent: `session` (s1..s4), `msg_id`, `msg_type` (NewOrder / Cancel / Modify), `bytes`, `write_calls` (number of `send()` calls used for the message), `app_send_ns` (first `send()`), `wire_first_ns` / `wire_last_ns` (NIC TX time of first / last byte), `exch_ack_ns` (exchange ack received; blank = none).
- **`inbound.csv`**, every message the exchange sent us: `session`, `rx_ns` (NIC RX time), `kind` (ACK / FILL / HEARTBEAT), `msg_id` (for ACK: the order it acknowledges).
- **`sessions.csv`**, one row per session: `session, host, kernel, app_version, tcp_nodelay, so_sndbuf, colo_rack`.

## Steps

**Step 1: Load and audit (~25 min).** Load the three files with sensible dtypes. Count missing values, duplicate ids and impossible timestamp orderings. Compute per-order stage latencies: app→wire (last byte), wire→ack, total.
*Deliverable:* data-quality counts and an orders table with stage latencies.

**Step 2: Distributions per session (~25 min).** Compare the latency distributions of each session, stage by stage.
*Deliverable:* a percentile table per session (p50, p99, % of orders > 1 ms); one figure that shows both the body and the tail; two sentences on what you see.

**Step 3: Drill down (~25 min).** What distinguishes the slow orders from the fast ones? Use the session configuration and the message attributes.
*Deliverable:* one slice table, a one-sentence hypothesis, and what you ruled out and why.

**Step 4: Evidence and impact (~35 min).** Design a check that would *fail* if your hypothesis were wrong, and run it on the slow orders. Then: what share of orders is affected, and what is p99 latency now vs after a fix (state your assumption)? Propose the fix and how you would verify it in production.
*Deliverable:* one figure, 2–3 numbers, a before/after p99 and a short fix + verification plan.

**Wrap-up: 2 minutes, your conclusion + limitations.**

## Follow-ups (asked after your wrap-up)
1. Why is the mean latency per session a misleading summary here?
2. Some single-`send()` messages on the affected session are slow too. Why?
3. TCP vs UDP: why is market data usually UDP multicast, while order entry is TCP?
4. Explain Nagle's algorithm and delayed ACK. If only the exchange side could change, what would help, and why not rely on it?
5. What does kernel bypass (OpenOnload, DPDK) buy you, and would it have avoided this bug? Why trust NIC hardware timestamps over host timestamps?
