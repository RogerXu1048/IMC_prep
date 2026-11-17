# P09 — "Why do some of our orders take 40 ms?"

**Time box: ~60 min** (Parts A–E ~45 min data work, Part F ~15 min discussion) · Python,
pandas, numpy, matplotlib allowed · Think out loud.

## Context

Our order-entry gateways keep a TCP session to the exchange. A healthy order goes from
"strategy calls `send()`" to "exchange acknowledgement received" in about 30 µs. Since this
morning's release, the trading desk reports that **some** orders take tens of
*milliseconds*. At those latencies we are effectively not in the market, so this costs money.

We capture every order at three points: when the application calls `send()`, when the
first and last byte leave the NIC (hardware TX timestamp), and when the exchange's
application-level acknowledgement arrives. We also capture every inbound packet the
exchange sends on each session. Find out what is going on, prove it with the data, and
propose a fix.

## Data

Run `python3 make_data.py` first. It writes three files to `../data/p09/`, covering one
trading hour (14:30–15:30 UTC) and four order-entry sessions.

**`orders.csv`**: one row per order message we sent

| column | type | meaning |
|---|---|---|
| `session` | str | order-entry session `s1`..`s4` |
| `msg_id` | int | unique message id |
| `msg_type` | str | NewOrder / Cancel / Modify |
| `bytes` | int | message size in bytes |
| `write_calls` | int | number of `send()` calls the application used for this message |
| `app_send_ns` | int | when the application made its first `send()` call (epoch ns, host clock) |
| `wire_first_ns` | int | NIC TX timestamp of the message's first byte (epoch ns) |
| `wire_last_ns` | int | NIC TX timestamp of the message's last byte (epoch ns) |
| `exch_ack_ns` | int, nullable | when the exchange's acknowledgement for this message arrived (epoch ns); blank if none was received |

**`inbound.csv`**: every application message the exchange sent us

| column | type | meaning |
|---|---|---|
| `session` | str | session |
| `rx_ns` | int | NIC RX timestamp (epoch ns) |
| `kind` | str | `ACK` (ack of one of our orders), `FILL`, `HEARTBEAT` |
| `msg_id` | int, nullable | for `ACK`: the order it acknowledges |

**`sessions.csv`**: configuration of each session

| column | meaning |
|---|---|
| `session`, `host` | session and host it runs on |
| `kernel` | Linux kernel version |
| `app_version` | order-entry gateway build |
| `tcp_nodelay` | socket option value (1 = set) |
| `so_sndbuf` | socket send buffer size |
| `colo_rack` | rack in the co-location facility |

All timestamps come from the same PTP-disciplined host and NIC clocks.

## Tasks

### Part A: Load and build stage latencies (8 min)
Load the files and compute, per order: application→wire (last byte), wire→ack, and total.
Check for missing values and impossible orderings.
**Deliverable:** a DataFrame with the stage latencies and a short data-quality note.

### Part B: Look at the distributions (8 min)
Compare the latency distributions across sessions, per stage. Choose plots that make
both the body and the tail visible.
**Deliverable:** a percentile table per session and stage, one figure, and what you notice.

### Part C: Drill down (15 min)
What distinguishes the slow orders from the fast ones? Use the session configuration, the
message attributes, the timing relative to neighbouring messages on the same session, and the
inbound traffic. Form a hypothesis about the mechanism and back it with evidence **from the
data**. Look for a check that would fail if your hypothesis were wrong.
**Deliverable:** a hypothesis plus 2–3 pieces of quantitative evidence (a table and/or a plot).

### Part D: Impact (7 min)
How many orders are affected, and what share of all orders? What are the p50 / p99 / p99.9 of
total latency now, and what would they be after a fix? State the assumptions behind your
counterfactual.
**Deliverable:** a before/after table.

### Part E: Fix and verification (7 min)
Propose a fix (or fixes). How would you verify it in production without risking the business,
and which tools would show the mechanism directly on the host?
**Deliverable:** a short written plan.

### Part F: Networking fundamentals (discussion, ~15 min)
See the question list at the end of this handout. **Finish Parts A–E before you read it**,
because some of the questions hint at the mechanism.

## Follow-up questions the interviewer may ask
1. Your counterfactual borrows another session's latencies. Why is that reasonable? What could make it wrong?
2. Why is it not enough to look at the average latency per session?
3. If messages written with a single `send()` were also slow on an affected session, what could explain it?
4. If you could only change the exchange side, what would help? Why can't we rely on that?
5. How would you have caught this before production (tests, canaries, latency SLO alerts)?
6. Would kernel bypass have avoided this bug? Explain.
7. Pandas detail: what happens to an int64 epoch-ns column with blanks when you read it with `read_csv`?
8. How would you build a monitor that detects this class of problem within a minute?

## Part F question list (read after Parts A–E)
Expect to be asked several of these:
1. TCP vs UDP: what does each guarantee, and what does each cost?
2. What is multicast? Why does exchange market data use UDP multicast while order entry uses TCP?
3. Walk through the TCP three-way handshake. What state does each side keep?
4. What is Nagle's algorithm, and what problem was it designed to solve?
5. What is delayed ACK? Why does it interact badly with Nagle?
6. What do `TCP_NODELAY` and `TCP_QUICKACK` do? Which side controls each?
7. Slow start and the congestion window: does it matter for a long-lived, low-volume
   order-entry session? When can it?
8. How does TCP detect loss and retransmit (RTO, fast retransmit)? What does that do to latency?
9. What is head-of-line blocking? Where does it show up in TCP and in our systems?
10. What are A/B feeds and how does a feed handler arbitrate them?
11. What is kernel bypass (e.g. OpenOnload/ef_vi, DPDK)? Why does it cut latency? What do you give up?
12. Hardware vs software timestamps; what PTP does; how accurate it can be.
13. MTU and fragmentation: why do trading systems avoid fragmented packets?
14. Interrupt coalescing vs busy polling: what is the trade-off?
15. Trace one `send()` end to end: syscall → socket buffer → TCP/IP → qdisc/driver → NIC DMA → wire.

## 考点（中文）
- 分阶段拆 latency（app→wire→ack），看分布而不是 mean；log 坐标、ECDF、bimodal
- 系统化 drill-down：按配置、消息属性、邻近消息、inbound 流量切片，找"能证伪的证据"，排除 red herring
- TCP 协议栈的细节怎么影响延迟（socket options、ACK 机制、发送缓冲区）
- 反事实估计 (counterfactual) 的假设，修复方案，生产验证（canary / pcap / ss / strace）
- 网络基础问答：TCP vs UDP、multicast、A/B feed、kernel bypass、PTP、硬件时间戳
- pandas 细节：nullable Int64、epoch ns 用 float64 会丢精度
