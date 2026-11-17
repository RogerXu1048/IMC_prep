# P09 参考答案："为什么有些订单要 40 ms？"

> 先自己做完再看。数字来自 `python3 solution.py`（seed 固定，可复现）。

---

## 1. 预设的 findings 以及分析路径

**根因：** s3 的 `tcp_nodelay=0`（Nagle 开着），**再加上**新版本 `oe-2.4.0` 把 NewOrder 拆成 header + body 两次 `send()`，**再加上**交易所那边接收端的 delayed ACK（约 40 ms）。三个条件叠在一起才出事。

| 步骤 | 做什么 | 看到什么 |
|---|---|---|
| A | 读数据时把时间戳列指定为 `Int64` | `exch_ack_ns` 有 189 个空值。默认 `read_csv` 会把这一列变成 float64，epoch ns 约 1.8e18 > 2^53，**精度被静默丢掉 256 ns 量级** |
| B | 分 session、分阶段看 percentiles 和 log 直方图 | s1/s2/s4 的 total p50 约 31–33 µs，p99.9 < 60 µs。**s3 的 p50 是 31 ms，p99 是 41 ms**，而且呈 bimodal：一个约 30 µs 的小峰，加上一根约 40 ms 的尖峰。问题全在 **app→wire** 阶段，wire→ack 四个 session 都一样。全部订单里有 10.46% 超过 1 ms，全部来自 s3 |
| C1 | join `sessions.csv`，按 (session, app_version, tcp_nodelay, write_calls, msg_type) 切片 | s3 的 NewOrder（write_calls=2）有 98.7% 慢；**s4 用同一个 app 版本、同样两次 write，但 tcp_nodelay=1，慢的比例是 0%**。所以"两次 write"本身不是根因，必须和 nodelay=0 组合才出事 |
| C2 | 排除 red herring | s2 内核不同，但完全正常；s1 的 wire→ack 多约 2 µs，原因是 rack R12 更远，跟 40 ms 无关 |
| C3 | 对 s3 里"被 hold 住"的消息（第一个字节到最后一个字节之间隔了 > 1 ms），计算 **header 发出后下一个能捎带 ACK 的 inbound 包**（FILL、HEARTBEAT、更早订单的 ACK）什么时候到 | **body 正好在那个 inbound 包到达后约 1.5 µs 发出**（p99 3 µs）。如果 39 ms 内没有任何 inbound 包，body 就在 40.0–41.4 ms 发出。散点图上 hold = min(到下一个 inbound 的时间, 40 ms)，是一条完美的"折线"，这就是 smoking gun |
| C4 | s3 上 single-write 的消息（Cancel/Modify）也有约 42% 很慢 | 这些慢消息 **100% 和某个被 hold 的 body 在同一个 ns 离开 NIC**，也就是被合并进了同一个 segment：在 send buffer 里排在被 hold 的数据后面（head-of-line blocking） |
| C5 | 次要发现：s3 上没被合并的 single write，按"距上一条消息的间隔"分组 | 间隔 < 10 µs 时，app→first byte 中位数是 32 µs，而 s4 是 3.5 µs。Nagle 会让小包等前一个包的 ACK（约 1 RTT）。**即使 app 不拆包，nodelay=0 也会让 burst 里的订单多等一个 RTT** |
| D | 反事实估计 | 全体 p99 从 40,607 µs 降到 50 µs，p99.9 从 41,320 µs 降到 58 µs；s3 的 p50 从 31,366 µs 降到 31.5 µs |

**为什么是 40 ms：** Linux 的 delayed ACK 最小超时是 `TCP_DELACK_MIN = HZ/25 = 40 ms`。接收方只收到半条消息（只有 header），应用层没有回复，没有数据可以捎带 ACK，于是它等定时器到期再发 ACK。如果这 40 ms 内交易所恰好有别的包发给我们（FILL、HEARTBEAT），ACK 就跟着那个包提前到达，所以分布是"指数分布被 40 ms 截断，再加上 40 ms 处的一根尖峰"。

---

## 2. Follow-up 参考答案

**1. 用 s4 的分布做 counterfactual 为什么合理？什么会让它错？**
s4 和 s3 的 app 版本、内核、rack 都一样，唯一的区别是 `tcp_nodelay`，所以 s4 是一个很好的"同类对照"。可能出错的地方：
- s3 和 s4 的流量模式不同（burst 结构、消息组合）。
- 修复后不再有合并，交易所那边收到的包更多更碎，可能触发对方的限速。
- s3 主机的负载或 CPU 亲和性不同。
- 我们只换了 app→wire 段，假设 wire→ack 不受影响（数据上看四个 session 的 wire→ack 一致，这个假设是合理的）。

**2. 为什么只看平均值不够？**
s3 的分布是 bimodal 的，平均值（约 23 ms）不对应任何真实的订单，而且会把"30% 很快、70% 卡 40 ms"这种结构完全藏起来。更关键的是，如果问题只影响 1% 的订单，平均值几乎不动，但 p99 会暴露它。交易里最关键的往往正是尾部那些订单。

**3. 单次 `send()` 的消息为什么也慢？**
Nagle 规则：只要连接上有**未被 ACK 的数据**，新写入的小数据（< MSS）就留在 send buffer 里，等 ACK 到了或者攒够一个 MSS 再发。当前面一条拆开的消息的 body 被卡住时，后面写进来的 Cancel/Modify 也排在它后面，最后和它一起在 release 时刻作为一个 segment 发出。这是发送缓冲区里的 head-of-line blocking。即使前面没有被 hold 的消息，紧接着发的小消息也要等上一条的 ACK（约 1 RTT，约 27 µs）。

**4. 只能改交易所那边的话怎么办？为什么不能依赖它？**
接收方可以设 `TCP_QUICKACK`（立刻 ACK），或者把 delayed ACK 关掉/调短，问题就会消失。但交易所的网关不归我们控制，它的行为可能随时变化，而且交易所也不会为一个会员去调整。**能在自己这边修的就自己修**：`TCP_NODELAY`，再把每条消息合成一次 write。

**5. 上线前怎么抓到？**
- 回归测试：用真实 TCP 栈（或 loopback 加接收端模拟 delayed ACK）跑 latency benchmark，断言 p99 < 阈值。
- 配置检查：启动时校验 socket options（`getsockopt(TCP_NODELAY)`），配置模板做 lint。
- Canary：新版本先在一个 session 上跑，对比 p99、p99.9 和 >1 ms 的订单数。
- 监控：每个 session 每分钟的 p99 和"慢订单计数"，超过阈值就告警。

**6. Kernel bypass 能避免吗？**
不一定。OpenOnload 这类方案在用户态实现了完整的 TCP，并且**保留 Nagle 和 delayed ACK 的语义**（为了兼容），nodelay 一样要设。如果用 ef_vi 或 DPDK 自己写一个极简的 TCP，作者通常会去掉 Nagle，但那是另一回事。根因是"socket 配置 + 应用写法"，不是内核本身。

**7. pandas：带空值的 int64 epoch-ns 列用 `read_csv` 读会怎样？**
会被推断成 float64。2^53 ≈ 9.0e15，而 epoch ns ≈ 1.8e18，float64 在这个量级的间隔（ulp）是 256 ns，所以**时间戳被静默四舍五入到 256 ns**，相减得到的延迟有几百 ns 误差，还可能出现假的相等或负数。解决：`dtype={"exch_ack_ns": "Int64"}`（nullable），或者先减去一个基准时间再转 float。

**8. 一分钟内发现这类问题的监控怎么做？**
每个 session 维护滚动 1 分钟的 HDR histogram（或者固定的对数 bucket 计数），实时算 p99 和"> 1 ms 的订单数"。告警条件可以是 p99 > 5×基线，或者慢订单数 > N。另外单独监控 `wire_last − wire_first > 100 µs` 这个指标：它直接对应"消息在发送端被拆开、被卡住"，信号很干净。

---

## 3. Part F：网络基础参考答案

**1. TCP vs UDP**
- TCP：面向连接，可靠，有序，字节流，有流量控制（rwnd）和拥塞控制（cwnd）。代价是握手、ACK、重传带来的延迟（丢包时 head-of-line blocking），每个连接都有状态，还有 Nagle 和 delayed ACK 这类"为吞吐优化"的机制。
- UDP：无连接，不可靠，不保证顺序，按报文发送，开销极小，延迟可预测。可靠性要由应用层自己做（序号、gap 检测、重传请求）。

**2. Multicast；为什么行情用 UDP multicast，下单用 TCP**
- Multicast：发送方只发一份，交换机和路由器按组（IGMP 加入）复制到所有订阅者。
- 行情是一对多、对延迟敏感、可以容忍偶尔丢包（有 A/B feed 和 recovery），而且发给所有人要**公平**（同时到达）。用 TCP 的话，每个订阅者一个连接，状态和重传会让慢的 consumer 拖累发送方。
- 下单是一对一，要求可靠、有序、每条都必须到（丢一笔订单或撤单都是事故），所以用 TCP（FIX、各交易所的二进制协议）。

**3. 三次握手**
客户端发 SYN(seq=x)，服务端回 SYN-ACK(seq=y, ack=x+1)，客户端再发 ACK(ack=y+1)。双方各自记录初始序号、窗口大小、MSS、窗口缩放和 SACK 等选项；服务端在半连接状态下把连接放在 SYN 队列里。交易会话在开盘前就建好并长期保持，握手不在关键路径上。

**4. Nagle**
目的是避免"小包风暴"（tinygram，比如 telnet 每个字符一个 40 字节头的包）。规则：如果有未 ACK 的数据在途，新的小数据先攒着，直到 ACK 回来或者攒满一个 MSS。它提高带宽效率，代价是**延迟**。

**5. Delayed ACK 以及和 Nagle 的冲突**
接收方不马上回 ACK，而是等一会儿（Linux 最少 40 ms，最多 200 ms），希望能把 ACK 捎带在应用的回复上，或者两个 segment 合并成一个 ACK。冲突的情形：发送方 write-write-read（先写 header，再写 body，然后等回复）。header 发出后，body 被 Nagle 卡住，等 header 的 ACK；接收方只收到半条消息，没有回复可以捎带，于是等 delayed ACK 定时器，结果双方互相等到 40 ms。

**6. `TCP_NODELAY` / `TCP_QUICKACK`**
- `TCP_NODELAY`：**发送方**设置，关闭 Nagle，小包立刻发。低延迟系统的标配。
- `TCP_QUICKACK`：**接收方**设置，立刻发 ACK，退出 delayed-ACK 模式。Linux 上它不是永久生效的，内核可能自动切回，需要在每次 recv 后重新设置。
- 我们能控制的是自己的发送端，所以设 NODELAY；对方的 QUICKACK 我们控制不了。

**7. Slow start / cwnd 对下单会话的影响**
新连接，或者**空闲超过 RTO 后**，cwnd 会被重置成初始窗口（通常 10 个 MSS），之后每个 RTT 指数增长。下单会话流量很小（每条消息约 100 B），平时远到不了 cwnd 的上限，所以基本没影响。但是：(a) 开盘时突然要发一大串订单，或者发大批量报价，可能碰到 cwnd 上限，多出几个 RTT；(b) 丢包后 cwnd 减半，或者 RTO 后回到 1。可以关掉 `tcp_slow_start_after_idle`。

**8. 丢包检测与重传**
- RTO：超时重传，Linux 最小 200 ms，之后指数退避，对交易来说是灾难性的。
- Fast retransmit：收到 3 个重复 ACK 就立刻重传（约 1 RTT）。SACK 告诉发送方具体缺哪些段。另外还有 TLP（tail loss probe）、RACK。
- 对延迟的影响：丢包后的所有数据都要等重传完成才能交给应用（head-of-line blocking）。所以交易网络要求零丢包：要有足够的 buffer、做流量整形、不能过载。

**9. Head-of-line blocking**
排在前面的东西卡住了，后面的即使已经准备好也走不了。例子：TCP 一个段丢了，后面已经到达的段不能交给应用；send buffer 里被 Nagle 卡住的数据，挡住了后面的消息（本题的 C4）；交换机输出队列满了；单线程的事件循环里一个慢请求挡住了后面所有的请求。办法：用多个独立的流（多个 session 或 multicast group）、UDP 加应用层排序、优先级队列。

**10. A/B feeds**
同一份行情从两条物理独立的路径各发一份，seq 相同。feed handler 对每个 seq 取先到的那份，后到的那份丢掉；如果某个 seq 两条线都缺，就等一个很短的超时（参考两条线到达时间差的分布），然后发 retransmission request，或者用 snapshot 恢复，期间把 book 标为 stale。见 P04。

**11. Kernel bypass**
- 内核网络栈的开销包括：系统调用、上下文切换、中断、socket buffer 拷贝、协议栈的通用逻辑、调度抖动。一次 recv 往返大约要几 µs。
- Bypass 的做法是用户态直接访问 NIC 的 ring buffer（DMA），轮询而不靠中断，零拷贝。OpenOnload 是 Solarflare/AMD 的方案，提供兼容 socket API 的用户态 TCP/UDP，基本不用改代码；ef_vi 是更底层的帧级 API；DPDK 是通用的用户态驱动框架。延迟可以降到 1 µs 左右，抖动也大幅减少。
- 代价：要独占 CPU 核去 busy-poll（耗电、占核），用不了内核的工具（tcpdump、iptables、部分 `ss` 信息），专用硬件和驱动会锁定厂商，调试更难，安全隔离也更弱。

**12. 硬件时间戳与 PTP**
- 硬件时间戳：NIC 在包经过 MAC/PHY 时用自己的时钟打戳（ns 级），不受中断和调度影响。软件时间戳是内核或应用打的戳，带有 µs 级的随机误差。
- PTP（IEEE 1588）：grandmaster（GPS 授时）和从时钟之间交换 Sync/Follow_up/Delay_req/Delay_resp 报文，测出 offset 和路径延迟（假设路径对称）。配合硬件时间戳和支持 PTP 的交换机（boundary/transparent clock），精度可以到几十 ns 到亚 µs。NTP 一般只有 ms 级。监管上也有要求，比如 MiFID II RTS 25 规定高频交易的时钟要做到 100 µs 精度。

**13. MTU 与分片**
以太网 MTU 通常是 1500 B。IP 分片后只要丢一片，整个报文就作废；重组需要缓存、增加延迟；有的防火墙会丢分片包；在 UDP multicast 下不可靠，代价很大。所以交易协议都把报文设计得 < MTU（行情包通常几百字节），TCP 用 MSS 和 PMTU discovery 避免分片。

**14. Interrupt coalescing vs busy polling**
- Interrupt coalescing：NIC 攒够 N 个包或者等 T µs 才发一次中断，降低 CPU 开销，提高吞吐，但每个包多了最多 T 的延迟和抖动。
- Busy polling（`SO_BUSY_POLL`，或者 kernel bypass 下的自旋）：一个核一直轮询 RX ring，包一到就处理，延迟最低、抖动最小，代价是这个核 100% 被占用，耗电。
- 低延迟交易的典型做法：关掉中断合并（`ethtool -C rx-usecs 0`），或者直接 busy-poll，配合 CPU isolation（isolcpus）、IRQ 亲和性绑定、关闭节能 C-state。

**15. 一次 `send()` 端到端**
1. 应用调用 `send()`，进入**系统调用**（用户态切到内核态）。
2. 数据从用户 buffer **拷贝**进 socket 的发送缓冲区（skb）。
3. **TCP 层**决定现在能不能发：要看 Nagle、cwnd、对方的 rwnd、发送窗口；能发的话切成 segment，加 TCP 头（seq/ack），记录重传队列。
4. **IP 层**加 IP 头，查路由，做 netfilter。
5. 经过 **qdisc**（流量控制队列）进入**驱动**，驱动把描述符放进 NIC 的 TX ring。
6. **NIC** 通过 **DMA** 从内存读出数据，做校验和 offload，MAC/PHY 把它**序列化到线上**（10G 下 100 B 约 80 ns），这时打硬件 TX 时间戳。
7. 发完后 NIC 发中断（或者等轮询），驱动回收描述符。数据还留在重传队列里，直到收到 ACK 才释放。
本题的问题发生在第 3 步：Nagle 让数据停在了发送缓冲区里。

---

## 4. 常见错误 / 扣分点
1. 只看平均值，或者用线性坐标画图，看不到 bimodal 的结构。
2. 看到 s3 慢就直接说"s3 主机有问题"或"网络有问题"，没把延迟拆成阶段（app→wire 和 wire→ack），所以没注意到 wire→ack 四个 session 一模一样。
3. 看到 s3 是新版本，就下结论"新版本的 bug"，没有利用 s4（同版本、nodelay=1）这个天然对照；或者被 s2 的内核版本、s1 的 rack 带偏。
4. 计算"下一个 inbound 包"时，把被 hold 那条消息**自己的 ACK** 也算进去，结果每个点都落在对角线上，论证变成了循环论证。
5. 修复只提一个"关掉 Nagle"，没说怎么在生产里安全验证（canary、对照组、pcap），也没考虑把消息合成一次 write。
6. `read_csv` 让带空值的时间戳变成 float64，延迟里混进几百 ns 的误差。

---

## 5. Speak-aloud scripts (English)

**Framing the drill-down:**
> "First I'll split latency into stages: app to wire, and wire to exchange ack. That tells me
> whether the problem is on our host or in the network or exchange. Wire-to-ack is identical
> on all four sessions, about 27 µs. All the damage is between send() and the last byte
> leaving the NIC, and only on s3. So the problem is in our host's send path, not the network."

**Using the natural control:**
> "s3 and s4 run the same app version, and both split NewOrder into two writes. The only
> config difference is TCP_NODELAY. s4 is perfectly healthy, so the split write alone isn't
> the bug. It's the split write combined with Nagle. My hypothesis: Nagle holds the body until
> the header is ACKed, and the exchange delays that ACK because it only has half a message."

**The falsifiable check:**
> "If that's right, the body should leave the NIC right after the next packet that could
> carry an ACK arrives, or at about 40 ms if nothing arrives. That's exactly what we see.
> Holds end about 1.5 µs after the next fill or heartbeat, and the rest cluster at 40.0 to
> 41.4 ms. The fix is TCP_NODELAY plus writing each message in one call. I'd roll it out on
> s3 as a canary, and confirm with a pcap that the second segment now leaves immediately."

---

## 6. 复杂度
- 读数据和各阶段延迟：O(n)。groupby 切片：O(n)（hash）。
- "下一个 inbound 包"：`merge_asof` 要求两边都有序，排序 O((n+m) log(n+m))，合并是线性的。不用 merge_asof 的话，对每个 hold 用 `searchsorted` 二分，O(h log m)。
- 合并判定（release 时刻相同）：用集合做 membership，O(n)。
- 反事实重采样：O(n)。
- 生成器里的 Nagle 模拟：每条消息做一次二分加一次 `insort`，总共 O(n log n + n·k)，insort 的平移代价对 2.7 万条来说可以忽略。
