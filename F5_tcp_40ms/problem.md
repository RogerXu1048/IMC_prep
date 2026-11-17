# F5 — "Why do some of our orders take 40 ms?"

**Time box: ~2 hours** · Python, pandas, numpy, matplotlib · Think out loud; the interviewer helps with syntax.

## Context
Our order-entry gateways keep one TCP session per exchange connection. A healthy order goes from "strategy calls `send()`" to "exchange acknowledgement received" in about 30 µs. Since this morning's release, the desk reports that **some** orders take tens of **milliseconds**. At those latencies we are effectively not in the market. Find out what is going on, prove it with the data, quantify the damage and propose a fix you could safely roll out.

## Data
Run `python3 make_data.py` first. It writes three files to `../data/p09/` (one trading hour, four sessions). All timestamps are epoch nanoseconds from PTP-disciplined host/NIC clocks.

**`orders.csv`**: one row per order message we sent

| column | type | meaning |
|---|---|---|
| `session` | str | order-entry session `s1`..`s4` |
| `msg_id` | int | unique message id |
| `msg_type` | str | NewOrder / Cancel / Modify |
| `bytes` | int | message size |
| `write_calls` | int | number of `send()` calls used for this message |
| `app_send_ns` | int | application's first `send()` call |
| `wire_first_ns`, `wire_last_ns` | int | NIC TX timestamps of the first / last byte |
| `exch_ack_ns` | int, nullable | exchange's application-level ack received (blank = none) |

**`inbound.csv`**: every message the exchange sent us: `session`, `rx_ns` (NIC RX timestamp), `kind` (`ACK` of one of our orders / `FILL` / `HEARTBEAT`), `msg_id` (for `ACK`: the order it acknowledges).

**`sessions.csv`**: per-session configuration: `session, host, kernel, app_version, tcp_nodelay, so_sndbuf, colo_rack`.

## Steps

**Step 1: Load and build stage latencies (≈20 min).** Load the three files with sensible dtypes. Check integrity: missing values, duplicate ids, impossible orderings. Compute per order: app→wire (last byte), first→last byte, wire→ack, total.
*Deliverable:* an orders DataFrame with stage latencies and a short data-quality note.

**Step 2: Compare distributions (≈20 min).** Compare per-session latency distributions for each stage.
*Deliverable:* a percentile table (p50/p90/p99/p99.9) per session and stage; one figure that shows both the body and the tail; two sentences on what you see.

**Step 3: Drill down (≈20 min).** What distinguishes slow orders from fast ones? Use the session configuration and message attributes. State a hypothesis about the mechanism.
*Deliverable:* one slice table and a one-sentence hypothesis.

**Step 4: Prove the mechanism (≈30 min).** Design a check that would **fail** if your hypothesis were wrong. Using the slow orders' timing and the inbound traffic on the same session, test it.
*Deliverable:* one figure and 2–3 numbers that confirm or refute the hypothesis. State the time complexity of how you matched events.

**Step 5: Impact and fix (≈20 min).** How many orders are affected, and what share? What are p50/p99/p99.9 of total latency now, and what would they be after a fix (state your assumptions)? Propose the fix and how you would verify it in production.
*Deliverable:* a before/after table and a short rollout and verification plan.

## Discussion / follow-ups
1. Why is the mean latency per session a misleading summary here?
2. Your counterfactual borrows another session's latencies. Why is that reasonable, and what could make it wrong?
3. Some single-`send()` messages are also slow on the affected session. Why?
4. If only the exchange side could change, what would help? Why can't we rely on that?
5. How would you have caught this before production, and how would you detect it within a minute in production?
6. Would kernel bypass have avoided this bug?
7. TCP vs UDP; why market data is UDP multicast but order entry is TCP; what happens inside `send()`.
