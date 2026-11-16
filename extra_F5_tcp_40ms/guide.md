# F5 Study Guide：40 ms 之谜（order-entry 延迟根因分析）

## 0. 一句话题目
新版本上线后，部分订单从 send() 到交易所 ack 要几十 ms（正常约 30 µs）。用数据找出原因、证明它、量化影响、提出能安全上线的修复。

| 时间 | 做什么 |
|---|---|
| 0:00–0:05 | Clarify：问问题，说出假设 |
| 0:05–0:25 | Step 1：load + 完整性检查 + 分阶段 latency |
| 0:25–0:45 | Step 2：分 session 看分布（表 + ECDF 图） |
| 0:45–1:05 | Step 3：drill down，给出 hypothesis |
| 1:05–1:35 | Step 4：用"下一个 inbound 包"证明机制（核心算法 + 图） |
| 1:35–1:50 | Step 5：impact、counterfactual、修复和验证方案 |
| 1:50–2:00 | 收尾：讲结论 + 网络追问 |

---

## 1. 开场 5 分钟

**要问的问题（English）：**
- "Are all timestamps from the same clock domain? Host send time vs NIC hardware timestamps — are they PTP-synced?"
- "Is `exch_ack_ns` the exchange's application-level ack, or the TCP ACK?"
- "What does 'slow' mean for the desk: is there an SLO, e.g. p99 under 100 µs?"
- "What changed in this morning's release? Which sessions got it?"
- "Are the sessions trading the same instruments and similar volumes?"

**要说出的假设：**
- "I'll assume the clocks are synchronised well enough (sub-µs) that I can subtract host and NIC timestamps."
- "Orders without an ack I'll keep, but exclude from ack-based latency and report separately."
- "I'll treat anything above 1 ms as pathological, since the healthy body is around 30 µs."

---

## Step 1：Load + 完整性检查 + 分阶段 latency

**目标**：一个干净的 orders 表，每行有 app→wire、first→last、wire→ack、total 四个延迟。

**先说什么**：
> "First I'll load the data with the right dtypes and check causality. Then I'll split latency into stages, because that immediately tells me whether the delay is on our host or in the network and exchange."

**决策点与 trade-offs**：
- **时间戳 dtype**：
  - A) 默认 `read_csv`。`exch_ack_ns` 有空值，会变成 float64；epoch ns ≈ 1.8e18 > 2^53，被**静默**四舍五入到 256 ns。
  - B) `dtype={c: "Int64"}`（nullable 整数）→ **选 B**。相减时保持整数，最后才转 float µs。
- **坏数据处理**：违反因果（wire < app）说明采集或时钟坏了，这是"停止分析"级别的问题，所以用 `assert`，而不是悄悄删行。没有 ack 的订单保留，但单独报告（可能是 reject，也可能超出了采集窗口）。
- **为什么要分阶段**：总延迟只告诉你"慢"；分阶段才能告诉你"慢在哪"。这是整道题最重要的一个决定。

**代码骨架**：
```python
TS_COLS = ["app_send_ns", "wire_first_ns", "wire_last_ns", "exch_ack_ns"]
orders = pd.read_csv(DATA / "orders.csv", dtype={c: "Int64" for c in TS_COLS})
inbound = pd.read_csv(DATA / "inbound.csv", dtype={"rx_ns": "int64", "msg_id": "Int64"})
sessions = pd.read_csv(DATA / "sessions.csv")

violations = {
    "wire_first < app_send": int((orders.wire_first_ns < orders.app_send_ns).sum()),
    "wire_last < wire_first": int((orders.wire_last_ns < orders.wire_first_ns).sum()),
    "ack < wire_last": int((orders.exch_ack_ns < orders.wire_last_ns).sum()),
}
assert not any(violations.values()), violations

def us(df, start, end):
    return (df[end] - df[start]).astype("float64") / 1e3

orders["app_to_wire_us"] = us(orders, "app_send_ns", "wire_last_ns")
orders["hold_us"]        = us(orders, "wire_first_ns", "wire_last_ns")
orders["wire_to_ack_us"] = us(orders, "wire_last_ns", "exch_ack_ns")
orders["total_us"]       = us(orders, "app_send_ns", "exch_ack_ns")
```

**自检**：orders 179,573 行，inbound 308,945 行，4 个 session；189 条订单没有 ack；三类违反都是 0。

**可能的追问**：
- *"Why not float64?"*：2^53 ≈ 9e15，epoch ns 远大于它，ulp 是 256 ns；对 µs 级的延迟分析来说误差不可忽略。另一种做法是先减去一个基准时间再转 float。
- *"What if the clocks weren't synced?"*：同一主机上 host 时钟和 NIC 时钟之间的差值可以用最小延迟估计（rolling min）；跨主机的比较就只能比较相对变化。

---

## Step 2：分 session 看分布

**目标**：每个 session、每个阶段的 p50/p90/p99/p99.9，外加一张 log-x 的 ECDF 图。

**先说什么**：
> "I'll look at percentiles and an ECDF on a log axis rather than means, because the healthy body is microseconds and the problem is milliseconds — three orders of magnitude apart."

**决策点与 trade-offs**：
- **图的类型**：
  - A) 直方图：要选 bins，log 轴上还要用 `np.logspace` 来分 bins。
  - B) ECDF：不用选参数，每个分位数都能直接读出来，多条线叠在一起也不乱 → **选 B**。
- **为什么不用 mean**：s3 的分布是双峰的，均值约 23 ms，不对应任何一笔真实订单。如果只有 1% 的订单慢，均值几乎不动，但 p99 会暴露问题。
- **画哪两张子图**：总延迟（找出哪个 session 有问题），和 wire→ack（证明网络和交易所那一侧是一样的）。

**代码骨架**：
```python
PCTS = [0.5, 0.9, 0.99, 0.999]
for col in ["app_to_wire_us", "wire_to_ack_us", "total_us"]:
    print(orders.groupby("session")[col].quantile(PCTS).unstack().round(1))

fig, axes = plt.subplots(1, 2, figsize=(12, 4))
for sess, g in orders.groupby("session"):
    for ax, col in zip(axes, ["total_us", "wire_to_ack_us"]):
        x = np.sort(g[col].dropna().to_numpy())
        ax.plot(x, np.arange(1, len(x) + 1) / len(x), label=sess)
for ax in axes:
    ax.set(xscale="log", ylabel="ECDF"); ax.legend()
```

**自检**：
- s1/s2/s4 的 total p50 约 31–33 µs，p99.9 < 60 µs。
- **s3 的 total p50 是 31,366 µs，p99 是 41,181 µs**；s3 有 70.29% 的订单 > 1 ms。
- app→wire 也是 s3 一枝独秀（p50 是 31,333 µs）；**wire→ack 四个 session 几乎一样**（p50 约 27–29 µs）。
- ECDF 上 s3 约 30% 是快单，接着是一段很长的平台，最后一根竖线落在约 40 ms。

**可能的追问**：
- *"What do you conclude from the wire-to-ack plot?"*："The exchange and network legs are identical across sessions, so the delay is on our host, between send() and the last byte leaving the NIC."
- *"Why does s1 have slightly higher wire-to-ack?"*：约多 2 µs，可能是机架更远（R12）。这是 red herring，量级完全不同。

---

## Step 3：Drill down，给出 hypothesis

**目标**：一张切片表，加一句可以被证伪的 hypothesis。

**先说什么**：
> "Now I'll join the session config and slice slow share by configuration and message attributes. I'm looking for a natural control: something identical to s3 except one variable."

**决策点与 trade-offs**：
- **join 的方式**：`merge(..., validate="many_to_one")`。如果 sessions 表里有重复 key，会立刻报错，而不是悄悄把订单复制一份。这是生产代码的习惯。
- **切哪些维度**：session、kernel、app_version、tcp_nodelay、colo_rack、write_calls。维度太多的表没人看得懂，太少又会漏掉控制组。
- **"慢"的定义**：用 app→wire > 1 ms，而不是 total，因为 step 2 已经证明问题在 host 这一侧。

**代码骨架**：
```python
df = orders.merge(sessions, on="session", how="left", validate="many_to_one")
df["slow"] = df.app_to_wire_us > SLOW_US
keys = ["session", "kernel", "app_version", "tcp_nodelay", "colo_rack", "write_calls"]
tab = df.groupby(keys).agg(n=("slow", "size"), slow_pct=("slow", "mean"),
                           p50_app_to_wire=("app_to_wire_us", "median"),
                           p50_wire_to_ack=("wire_to_ack_us", "median"))
```

**自检**（核心表）：
- s3（oe-2.4.0，nodelay=0）且 write_calls=2：**98.69%** 慢，p50 app→wire 40,086 µs。
- s3 且 write_calls=1：41.87% 慢。
- **s4（同样是 oe-2.4.0，同样两次 write，同 kernel、同 rack，但 nodelay=1）：0% 慢**。
- s2 的 kernel 不同，但它没问题，是 red herring。

**Hypothesis（English）**：
> "The new build sends NewOrder as two send() calls. With TCP_NODELAY off, Nagle's algorithm holds the second small segment until the first is ACKed. The exchange delays that ACK, because it has only half a message and nothing to piggy-back the ACK on. So the body waits for the next packet the exchange sends us, or for the 40 ms delayed-ACK timer."

**可能的追问**：
- *"Isn't it just the new version?"*：s4 是同一个版本，但完全没问题。所以"两次 write"本身不足以造成问题，必须再叠加 nodelay=0。
- *"Why are single-write messages on s3 slow too?"*：留到 step 4 去验证（它们排在被 hold 的数据后面）。

---

## Step 4：证明机制（核心算法）

**目标**：如果 hypothesis 是对的，那么 **hold 时间 = min(header 发出后到下一个能捎带 ACK 的 inbound 包的时间, 40 ms)**。这个预测可能失败，所以能用来证伪。

**先说什么**：
> "If I'm right, each held body should leave the NIC right after the next packet the exchange sends us on that session, or at about 40 ms if none arrives. That's a prediction that could fail, so let me test it."

**决策点与 trade-offs**：
- **哪些包可以算"carrier"**：
  - (a) FILL 和 HEARTBEAT 这类 unsolicited 包；
  - (b) **更早**订单的 ACK。
  - 绝不能包含被 hold 那条消息**自己的** ACK，或更晚订单的 ACK。否则每个点都会落在对角线上，变成循环论证。这是面试官最想看你注意到的坑。
- **探测起点**：`wire_first + MIN_RTT`（约 20 µs）。交易所必须先收到 header 才能 ACK 它。
- **匹配算法**：两边都是按时间排好序的数组。
  - A) 对每个 hold，线性扫描所有 inbound 包：O(h·m)，太慢。
  - B) `np.searchsorted` 二分查找：O(h log m)。
  - C) 双指针或 `pd.merge_asof(direction="forward", by="session")`：排序后 O(h + m)。
  - 代码里选 B，最直观；也可以说"merge_asof 是等价的 pandas 写法"。
- **"更早订单的 ack"怎么取**：用 `cummax().shift(1)` 得到之前所有订单里最晚的 ack 时间，O(n)。这是一个近似：严格来说应该取"probe 之后最早的那个更早 ack"，但在 burst 中通常只有一个候选。可以主动说出这个 trade-off。

**代码骨架**：
```python
s3 = df[df.session == "s3"].sort_values("app_send_ns").reset_index(drop=True)
s3["prev_ack_max"] = s3.exch_ack_ns.astype("float64").cummax().shift(1)
held = s3[(s3.write_calls == 2) & (s3.hold_us > SLOW_US)].copy()
probe = held.wire_first_ns.astype("int64").to_numpy() + MIN_RTT_NS
unsol = np.sort(inbound.loc[(inbound.session == "s3") & (inbound.kind != "ACK"), "rx_ns"].to_numpy())

idx = np.searchsorted(unsol, probe)                       # first carrier at/after each probe
next_unsol = np.where(idx < len(unsol), unsol[np.minimum(idx, len(unsol) - 1)], np.inf)
prev_ack = held.prev_ack_max.to_numpy()
next_ack = np.where(prev_ack > probe, prev_ack, np.inf)
held["to_next_ms"] = (np.minimum(next_unsol, next_ack) - (probe - MIN_RTT_NS)) / 1e6
held["hold_ms"] = held.hold_us / 1e3
```

**自检**：
- s3 上被 hold 的拆分消息有 7,738 条（拆分消息共 13,387 条）。
- **28.5%** 被一个 inbound 包放行：body 在该包到达后 **1.54 µs** 发出（p99 3.12 µs）。
- **71.5%** 在 39 ms 内没有任何 inbound 包：hold 时间的 p1/p50/p99 = **40.00 / 40.21 / 41.42 ms**，正是 delayed-ACK 定时器。
- 第二个可证伪检查：s3 上 5,585 条慢的 single-write 消息中，**100.0%** 和某个被 hold 的 body 在**同一个 ns** 离开 NIC。也就是说，它们在 send buffer 里排在被 hold 的数据后面，被合并进了同一个 segment（head-of-line blocking）。
- 图：散点呈完美的"折线"，先沿对角线上升，到 40 ms 变平。

**可能的追问**：
- *"Complexity?"*：排序 O(m log m)，查找 O(h log m)；如果用 merge_asof 或双指针，排序后是线性的。
- *"What if there were no inbound file?"*：仍然可以看 hold 时间的分布：尖峰在 40 ms，下面是截断的指数分布。再结合"只有 nodelay=0 且两次 write"的切片，就是很强的间接证据。最好的办法是抓一次 pcap 直接确认。

---

## Step 5：Impact + 修复 + 验证

**目标**：一张修复前后的对照表，加一个上线和验证计划。

**先说什么**：
> "To size the damage I'll build a counterfactual: give each s3 order the send-side delay of a comparable s4 order — same build, Nagle off — and keep s3's own network leg."

**决策点与 trade-offs**：
- **怎么构造 counterfactual**：
  - A) 直接把 s3 的值删掉。这会改变样本组成，不对。
  - B) 从 s4 里按相同 msg_type 抽样 app→wire 的值替换 s3 的 → **选 B**。
  - 假设：s3 和 s4 的流量模式可比，修复后网络那一段不变（step 2 已经证明四个 session 的 wire→ack 一样）。
- **修复方案**：`TCP_NODELAY=1`（我们这边能控制）**加上**把每条消息合成一次 `send()` 或 `writev`。两者都能消除 40 ms，但只有 NODELAY 能同时消除 burst 里小包互相等一个 RTT 的问题。
- **验证方式**：先在 s3 上 canary，以 s4 作对照，比较 p99、p99.9 和 >1 ms 订单数；用 pcap 确认第二个 segment 立刻发出；`ss -tni` 确认 nodelay 生效；`strace -c` 数 send() 调用次数。

**代码骨架**：
```python
rng = np.random.default_rng(1)
fixed = df.copy()
for mtype, g in df[df.session == "s3"].groupby("msg_type"):
    donor = df.loc[(df.session == "s4") & (df.msg_type == mtype), "app_to_wire_us"].dropna()
    fixed.loc[g.index, "app_to_wire_us"] = rng.choice(donor.to_numpy(), len(g))
fixed["total_us"] = fixed.app_to_wire_us + fixed.wire_to_ack_us
```

**自检**：
- 超过 1 ms 的订单有 **18,784 / 179,384 = 10.46%**，全部来自 s3。
- 全部订单的 p99 从 **40,607 µs → 50.0 µs**，p99.9 从 **41,319.5 µs → 58.3 µs**。
- s3 的 p50 从 **31,365.7 µs → 31.5 µs**。

**可能的追问**：
- *"Only the exchange could change — what then?"*：接收方设 `TCP_QUICKACK` 或关闭 delayed ACK 就能解决，但交易所不归我们管，也不会为一家会员单独调整。能在自己这边修的就自己修。
- *"How do you catch it within a minute?"*：每个 session 维护一个滚动的 HDR histogram，看 p99 和 >1 ms 的计数；同时单独监控 `wire_last − wire_first > 100 µs`，这个信号直接对应"被拆开并卡住"。
- *"Before production?"*：启动时用 getsockopt 校验 socket options；在真实 TCP 栈上跑 latency regression benchmark；按 canary 发布。

---

## 收尾：如何讲结论（English script）

> "About 10% of all orders — 70% of session s3 — took around 40 ms instead of 30 µs. The network and exchange legs are identical across sessions, so the delay is on our host, between send() and the wire. The trigger is the new build splitting NewOrder into two send() calls on a socket without TCP_NODELAY. Nagle holds the body until the header is ACKed, and the exchange delays that ACK for up to 40 ms. The proof: every held body leaves about 1.5 µs after the next inbound packet, or at 40 ms if none arrives, and s4, same build with NODELAY on, is perfectly healthy. Setting TCP_NODELAY and writing each message in one call should bring the overall p99 from 40 ms back to about 50 µs. I'd roll it out as a canary on s3 and confirm with a packet capture."

**有更多时间的话**：
- 抓 pcap，直接看 TCP 层的 ACK 时序；
- 量化 burst 内小包因为 Nagle 多等一个 RTT 的影响（s3 上 gap < 10 µs 的 single write 比 s4 慢约 28 µs）；
- 估算这 40 ms 损失了多少成交、多少钱。

---

## 常见坑
1. 用 float64 读 epoch ns，延迟里混进几百 ns 的误差。
2. 只看 mean，或者用线性坐标画图，看不出双峰结构。
3. 不拆阶段就下结论"网络慢"或"s3 主机坏了"。
4. 看到"新版本"就说是 bug，没有用 s4 这个天然对照组；或者被 s2 的 kernel、s1 的 rack 带偏。
5. 找"下一个 inbound 包"时把被 hold 消息自己的 ACK 也算进去，论证变成循环。
6. 对每个 hold 线性扫描全部 inbound 包，O(h·m)，大数据下跑不动。
7. 修复只说"关 Nagle"，没有说上线和验证方案，也没提合并成一次 write。
8. 忘了 single-write 消息也会慢，没能解释它们（排在 hold 后面，被一起合并发出）。

---

## 网络基础速答（Step 5 之后的追问）
- **TCP vs UDP**：
  - TCP 面向连接、可靠、有序、字节流，有流控和拥塞控制。代价是握手、ACK、重传，丢包时会有 head-of-line blocking。
  - UDP 无连接、不可靠、按报文发送，开销小、延迟可预测，可靠性由应用层自己做（seq 号、gap 检测、重传请求）。
- **为什么行情用 UDP multicast，下单用 TCP**：
  - 行情是一对多，发送方发一份，由交换机复制给所有订阅者，对所有人公平；能容忍丢包（有 A/B feed 和 snapshot 恢复）。
  - 下单是一对一，每条都必须到达且有序（丢一个撤单就是事故），所以用 TCP。
- **A/B feed**：同一份行情走两条物理独立的路径，seq 相同；feed handler 对每个 seq 取先到的那份。两路都缺的话，等一个短超时后请求重传，或用 snapshot 恢复，期间把 book 标为 stale。
- **Nagle**：只要有未被 ACK 的数据在途，新的小数据就先攒着，直到 ACK 回来或攒满一个 MSS。省带宽，费延迟。
- **Delayed ACK**：接收方不立刻回 ACK（Linux 最少 40 ms），希望把 ACK 捎带在应用回复上。和 Nagle 叠加时，在 write-write-read 模式下双方互相等待。
- **TCP_NODELAY / TCP_QUICKACK**：
  - NODELAY 由**发送方**设置，关闭 Nagle，是低延迟系统的标配。
  - QUICKACK 由**接收方**设置，立刻 ACK；它不是永久生效的，每次 recv 后要重新设置。我们只能控制自己这一侧。
- **Slow start / cwnd**：新连接或空闲超过 RTO 后 cwnd 会重置。下单流量小，平时碰不到上限；开盘时一串大 burst 可能多出几个 RTT。可以关闭 `tcp_slow_start_after_idle`。
- **重传 / RTO**：
  - RTO 最小 200 ms，之后指数退避，对交易是灾难。
  - Fast retransmit（3 个重复 ACK）约 1 RTT，SACK 能指出具体缺哪些段。
  - 丢包之后到达的所有数据都要等重传完成才能交给应用。
- **Head-of-line blocking**：前面的卡住，后面的走不了。例子：TCP 丢包、send buffer 里被 hold 的数据（本题）、单线程事件循环。办法：多个独立的流、UDP 加应用层排序。
- **Kernel bypass（OpenOnload/ef_vi、DPDK）**：
  - 用户态直接读写 NIC 的 ring，靠轮询而不是中断，零拷贝，延迟能降到约 1 µs，抖动也小。
  - 代价：要独占 CPU 核做 busy-poll，tcpdump 等内核工具用不了，依赖特定厂商，调试更难。
  - 注意：OpenOnload 为了兼容**仍然保留 Nagle 语义**，所以 bypass 不能修好本题的 bug。
- **硬件时间戳与 PTP**：
  - NIC 在 MAC/PHY 层打 ns 级时间戳，不受中断和调度影响。
  - PTP（IEEE 1588）配合硬件时间戳和支持 PTP 的交换机，精度可以到几十 ns 到亚 µs；NTP 通常只有 ms 级。
- **一次 `send()` 的完整路径**：
  1. 系统调用，进入内核；
  2. 数据拷贝进 socket 发送缓冲区；
  3. TCP 层根据 Nagle、cwnd、rwnd 决定**现在能不能发**（本题的问题就出在这一步）；
  4. 加 TCP/IP 头，经过 qdisc 和驱动，放进 TX ring；
  5. NIC 通过 DMA 读出数据，序列化到线上，此时打硬件 TX 时间戳；
  6. 数据留在重传队列里，直到收到 ACK 才释放。

---

## 练习方法
1. **第 1 轮**：照着本 guide 一步一步写，每一步都对照"自检"里的数字，并把"先说什么"读出声。
2. **第 2 轮**：只看 `problem.md`，加上 5 个函数名（`step1_load`、`step2_distributions`、`step3_drilldown`、`step4_hold_vs_next_inbound`、`step5_impact`），自己补全函数体。卡住超过 5 分钟再翻 guide。
3. **第 3 轮**：空白文件，设 2 小时计时器，全程用英文 talk aloud。重点练三件事：开场的 clarify、step 3 的 hypothesis 表述、step 4 的"可证伪"论证和循环论证陷阱。结束时用 1 分钟讲完收尾 script。
