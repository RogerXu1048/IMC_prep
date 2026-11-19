# F5 Study Guide：40 ms 之谜（order-entry TCP 延迟根因）

## 一句话题目
新版本上线后，部分订单从 `send()` 到交易所 ack 要约 40 ms（正常约 30 µs）。用数据找出原因、证明机制、量化影响，并给出修复和验证方案。

## 2 小时安排
| 时间 | 做什么 |
|---|---|
| 0:00–0:05 | Clarify：问 3 个问题，说出假设（> 1 ms 算坏；无 ack 的单独报告） |
| 0:05–0:30 | Step 1：load + audit + 分阶段 latency |
| 0:30–0:55 | Step 2：分 session 的分位数表 + ECDF 图 |
| 0:55–1:20 | Step 3：join 配置表，找控制组，给 hypothesis |
| 1:20–1:50 | Step 4：`merge_asof` 证明机制 + impact + 修复 |
| 1:50–2:00 | 收尾 2 分钟（结论 + limitations）+ 追问 |

## 开场：3 个 clarifying questions
- "Are the host and NIC timestamps on the same PTP-synced clock, so I can subtract them?"
- "Is `exch_ack_ns` the exchange's application-level ack, not the TCP-level ACK?"
- "What changed in this morning's release, and did every session get it?"

---

## Step 1：Load and audit
**目标**：数据质量计数，加上每单三段 latency：app→wire（我们主机）、wire→ack（网络 + 交易所）、total。
**先说什么**："I'll load the timestamps as exact integers, check causality, and then split latency into stages, so I can tell whether the time is lost on our host or in the network."
**决策与 trade-off**
- `exch_ack_ns` 有空值，默认会读成 float64。epoch ns ≈ 1.8e18 > 2^53，会被静默舍入到 256 ns。所以用 `Int64`，先整数相减，再转 µs。
- 违反因果（比如 wire < app）说明时钟或抓包坏了，用 `assert` 停下，不悄悄删行。无 ack 的订单保留，只从 ack 相关统计里排除。
- 分阶段是整题最关键的决定：total 只告诉你"慢"，分阶段才告诉你"慢在哪"。
**关键代码**
```python
o = pd.read_csv(DATA / "orders.csv", dtype={"exch_ack_ns": "Int64"})
bad = ((o.wire_first_ns < o.app_send_ns) | (o.wire_last_ns < o.wire_first_ns)
       | (o.exch_ack_ns < o.wire_last_ns)).sum()
assert bad == 0 and o.msg_id.is_unique
us = lambda a, b: (o[b] - o[a]).astype("float64") / 1e3     # subtract in int ns, then convert
o["app_to_wire_us"] = us("app_send_ns", "wire_last_ns")     # our host
o["wire_to_ack_us"] = us("wire_last_ns", "exch_ack_ns")     # network + exchange
o["total_us"] = us("app_send_ns", "exch_ack_ns")
```
**自检**：orders 179,573 行；inbound 里 ACK 179,384、FILL 115,161、HEARTBEAT 14,400；no ack 189；duplicate msg_id 0；out-of-order 0。全体中位数：app→wire 4.0 µs，wire→ack 27.6 µs，total 32.7 µs。

## Step 2：Distributions per session
**目标**：每个 session 的 p50/p99（三段）和 > 1 ms 占比，加一张 log-x ECDF。
**先说什么**："I'll compare percentiles and ECDFs on a log axis rather than means, because the healthy body is microseconds and the problem is tens of milliseconds."
**决策与 trade-off**
- ECDF 而不是 histogram：不用选 bins，任何分位数都能直接读，4 条线叠在一起也不乱。用 log x，因为数据跨 4 个数量级。
- 不看 mean：s3 是双峰的（约 30% 快单，其余大多在 40 ms 附近），mean 不对应任何一笔真实订单。
- 两个子图共用 x 轴：左边 total 找出哪个 session 坏了，右边 wire→ack 证明网络和交易所那一段到处一样。
**关键代码**
```python
t = o.groupby("session")[STAGES].quantile([0.5, 0.99]).unstack()
t.columns = [f"{c[:-3]}_p{q * 100:g}" for c, q in t.columns]
t["pct_over_1ms"] = o.groupby("session").total_us.agg(lambda x: x.dropna().gt(SLOW_US).mean() * 100)
for s, g in o.groupby("session"):
    x = np.sort(g[col].dropna())
    ax.plot(x, np.arange(1, len(x) + 1) / len(x), label=s)
ax.xaxis.set_major_formatter("{x:,.0f}")      # plain numbers, not 10^k, on the log axis
```
**自检**：s3 total p50 31,365.7 µs、p99 41,181.2 µs，70.4% > 1 ms；s3 的 app→wire p50 是 31,332.6 µs，时间几乎全在我们主机上。s1/s2/s4 total p99 是 51.1/48.9/49.7 µs，0.0% > 1 ms。wire→ack p50：s1 29.0，s2/s3/s4 都是 27.0 µs。s1 多出的 2 µs 来自 rack R12，是 red herring。

## Step 3：Drill down
**目标**：join 配置表，找出慢单和快单的区别，给出一句 hypothesis。
**先说什么**："Now I'll join the session config and slice the slow share by configuration. I'm looking for a control: a session identical to s3 except for one setting."
**决策与 trade-off**
- `merge(validate="many_to_one")`：sessions 表里 key 重复会立刻报错，而不是悄悄把订单复制一份。
- slow 定义用 app→wire > 1 ms，而不是 total：Step 2 已证明网络段一样，而且这样不受无 ack 订单影响。
- kernel 和 colo_rack 也放进 groupby key：每个 session 只有一个值，不增加行数，但 red herring 在表里一眼可见。
**关键代码**
```python
df = o.merge(ses, on="session", how="left", validate="many_to_one")
df["slow"] = df.app_to_wire_us > SLOW_US
tab = df.groupby(["session", "app_version", "kernel", "colo_rack", "tcp_nodelay", "write_calls"]).agg(
    n=("slow", "size"), pct_slow=("slow", "mean"), p50_app_to_wire_us=("app_to_wire_us", "median"))
```
**自检**：s3（oe-2.4.0，nodelay=0）write_calls=2：13,387 单，98.7% 慢，p50 app→wire 40,086.1 µs；write_calls=1：13,338 单，41.9% 慢。**控制组 s4**（同 build、同 kernel、同 rack R07，但 nodelay=1）：22,652 + 22,508 单，0.0% 慢。s2 的 kernel 不同、s1 的 rack 不同，也都是 0.0%，所以是 red herrings。
**Hypothesis**："The new build sends NewOrder with two send() calls. With TCP_NODELAY off, Nagle holds the second segment until the first is ACKed, and the exchange delays that ACK because it has half a message and nothing to piggy-back on. So the body waits for the next packet the exchange sends us, or for the 40 ms delayed-ACK timer."

## Step 4：Evidence and impact
**目标**：可证伪的检验：hold ≈ min(header 发出后到下一个 inbound 包的时间, 40 ms)。再算 impact，给出修复。
**先说什么**："If I'm right, every held body leaves right after the next packet the exchange sends on that session, or at 40 ms if none arrives. That prediction could fail, so let me test it."
**决策与 trade-off**
- carrier 只用 FILL/HEARTBEAT。被 hold 的那条消息自己的 ACK 一定在放行之后，算进去就是循环论证，所有点都会落在对角线上。更早订单的 ACK 在 header 发出前就已到达，因为 Nagle 只在没有未确认数据时才发 header。
- probe = wire_first + 20 µs：交易所收到 header 之前不可能 ACK 它。
- 匹配用 `merge_asof(by="session", direction="forward")`：两边排好序后是 O(n + m)；对每条 hold 线性扫描全部包是 O(n·m)。
- Projection：修复后 s3 应该像 s1/s2/s4（s4 就是同 build 开了 NODELAY），所以用其余 session 的分布作 p99 投影。
**关键代码**
```python
held = s3[(s3.write_calls == 2) & (s3.hold_us > SLOW_US)]           # header sent, body stuck
held = held.assign(probe_ns=held.wire_first_ns + MIN_RTT_NS).sort_values("probe_ns")
carriers = inb.loc[inb.kind != "ACK", ["session", "rx_ns"]].sort_values("rx_ns")
held = pd.merge_asof(held, carriers, left_on="probe_ns", right_on="rx_ns", by="session", direction="forward")
held = held.assign(to_next_ms=(held.rx_ns - held.wire_first_ns) / 1e6, hold_ms=held.hold_us / 1e3)
by_pkt = held.to_next_ms < DELACK_MS - 1
queued = s3[s3.slow & ~s3.msg_id.isin(held.msg_id)]      # slow, but not split-and-held itself
```
**自检**：被 hold 的 body 有 7,738 条。28.5% 在 inbound 包到达后 1.52 µs（median）发出；其余 71.5% 等到定时器，median 40.21 ms。其他慢单 100.0% 和某个 held body 在同一 ns 离开 NIC：它们在 send buffer 里排在后面（head-of-line blocking），这解释了 s3 上 41.9% 的 single-write 慢单。> 1 ms 的订单共 18,784 单（10.5%），p99 从 40,607 µs 降到约 50 µs。
**修复与验证**：s3 设 `TCP_NODELAY=1`，并且每条消息只用一次 `send()`/`writev`。先在 s3 上 canary，以 s4 作对照：看 wire_last − wire_first 回到几 µs、p99 和 > 1 ms 计数；再抓 pcap 确认 body 紧跟 header 发出。

---

## 图怎么讲
**图 (a) `f5_latency_ecdf.png`**：ECDF 一张图同时看 body 和 tail，log x 能放下 4 个数量级；共享 x 轴让人一眼看到网络段只有约 30 µs。
> "On the left, s3 is bimodal: about 30% of its orders are fast and the rest jump to around 40 ms; on the right, the network leg is identical on every session, so the delay is on our host."

**图 (b) `f5_hold_vs_next_inbound.png`**：直接把预测 hold = min(x, 40 ms) 画出来，点要么在 y = x 上，要么在 40 ms 线上。x 轴截在 120 ms，右侧截掉的点也都在 40 ms 线上。
> "Every held body sits either on the diagonal, released by the next inbound packet, or on the 40 ms line, which is exactly the Nagle plus delayed-ACK signature."

## 结论与 limitations（English script，2 分钟）
> "About 10.5% of all orders, 70% of session s3, take around 40 ms instead of 30 µs. The network and exchange leg is identical on every session, so the time is lost on our host, between send() and the wire. The trigger is the new build sending NewOrder in two send() calls on a socket without TCP_NODELAY: Nagle holds the body until the header is ACKed, and the exchange delays that ACK for up to 40 ms. The evidence: held bodies leave about 1.5 µs after the next inbound packet, or at the 40 ms timer, and s4, the same build with NODELAY on, is healthy. Setting TCP_NODELAY and writing each message in one call should bring p99 from about 40.6 ms back to about 50 µs. I'd roll it out as a canary on s3, with s4 as the control.
> Limitations: the mechanism is inferred from timestamps, not seen in a packet capture; this is one hour of data; the projection assumes s3 then behaves like the healthy sessions and nothing else changes; and the 40 ms delayed-ACK timer on the exchange side is assumed, not measured."

## 常见坑
1. 用 float64 读 epoch ns：延迟里混进几百 ns 的误差。
2. 只看 mean，或用线性坐标：看不出 s3 的双峰。
3. 看到 "new release" 就下结论，没用 s4 这个控制组；或者被 s2 的 kernel、s1 的 rack 带偏。
4. 找"下一个 inbound 包"时把被 hold 消息自己的 ACK 也算进去：循环论证，所有点都落在对角线上。
5. 解释不了 s3 上 41.9% 的 single-write 慢单：它们排在 held body 后面，和它在同一 ns 离开 NIC。

## 网络基础（追问速答）
- **TCP vs UDP**：TCP 面向连接、可靠、有序的字节流，有流控和拥塞控制，代价是 ACK、重传和丢包时的 head-of-line blocking。UDP 无连接、按报文发送、开销小，可靠性靠应用层（seq 号、gap 检测、重传请求）。
- **行情用 UDP multicast，下单用 TCP**：行情是一对多，交换机复制给所有订阅者，对大家公平，丢包靠 A/B feed 和 snapshot 恢复。下单是一对一，每条都必须到达且有序（丢一个撤单就是事故）。
- **Nagle**：只要有未被 ACK 的数据在途，新的小数据就先攒着，直到 ACK 回来或攒满一个 MSS。省带宽，费延迟。
- **Delayed ACK**：接收方不立刻回 ACK，希望捎带在自己的回复上；Linux 最少等 40 ms。
- **两者叠加**：write-write-read 模式下，发送方等 ACK，接收方等完整消息，互相等，直到定时器触发。本题正是这样。
- **TCP_NODELAY vs TCP_QUICKACK**：NODELAY 由发送方设置，关掉 Nagle，是低延迟系统的标配。QUICKACK 由接收方设置，而且不是永久生效。我们只控制自己这一侧，所以修自己的 socket。
- **一次 `send()` 的路径**：系统调用 → 拷进 socket send buffer → TCP 根据 Nagle、cwnd、rwnd 决定现在能不能发（本题卡在这一步）→ qdisc、驱动、TX ring → NIC DMA 发出并打 TX 时间戳。
- **Kernel bypass（OpenOnload/ef_vi、DPDK）**：用户态直接读写 NIC ring，busy-poll，零拷贝，延迟约 1 µs、抖动小。代价是独占 CPU 核、tcpdump 等内核工具不可用、绑定厂商。OpenOnload 仍然遵守 TCP 语义（包括 Nagle），所以 bypass 本身修不好这个 bug。
- **NIC 硬件时间戳 + PTP**：NIC 在 MAC/PHY 层打 ns 级时间戳，不受中断和调度影响。PTP 配合硬件时间戳可做到亚 µs 同步，NTP 通常只有 ms 级。所以 host 和 NIC 时间能相减的前提是 PTP。

## 练习方法
1. **第 1 轮**：照本 guide 一步步写，每步对照"自检"的数字，并把"先说什么"读出声。
2. **第 2 轮**：只看 `problem.md` 和函数名（`load`、`step1_stages`、`step2_distributions`、`step3_drilldown`、`step4_evidence_impact`），自己补全函数体。卡住超过 5 分钟再翻 guide。
3. **第 3 轮**：空白文件，设 2 小时计时，全程英文 talk aloud。最后用 2 分钟讲结论和 limitations，再自问 `problem.md` 里的 5 个 follow-ups。
