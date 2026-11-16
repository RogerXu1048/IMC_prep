# P07 答案与讲解：Robust Ingestion Pipeline

> 参考实现：`solution.py`（`python3 solution.py`），测试：`python3 -m unittest -v test_solution.py`。
> 第一次运行 `python3 solution.py --bless` 会生成 golden file，之后每次运行都会做 regression 比对。

## 1. 预设的问题与发现路径

| 分区 / 文件 | 问题 | 正确处理 | 状态 |
|---|---|---|---|
| `date=2026-11-04/port=2` | manifest 里有，store 里没有 | 不要 fetch，直接标 MISSING；当天的汇总标为不完整 | MISSING |
| `date=2026-11-05/port=1` | 文件被截断（上传中断），parquet 的 magic bytes 不对 | 解析失败属于 **permanent** 错误，不 retry、不 cache，等上游重新交付 | CORRUPT |
| `date=2026-11-06/port=0` | 只交付了 80% 的行，文件本身能读 | 数据保留，但标 INCOMPLETE；当天汇总标为不完整 | INCOMPLETE |
| `date=2026-11-06/port=9` | 不在 manifest 里（测试 session） | 忽略，并报告 | UNEXPECTED |
| port 3，11-03 起 | **Schema drift**：`qty` 改名为 `quantity`，`price` 变成字符串，`ts` 变成 ISO 字符串 | normalize 到 canonical schema；如果不处理，concat 会得到 object 列，算 VWAP 会出错 | OK（规范化后） |
| port 2 | failover 时 port 1 的约 0.4% fills 被重发，trade_id 相同 | 按 trade_id 去重，保留 owner port 的副本（约 300 行） | — |
| 每个分区 | qty=0、price=-1、side='X' 各若干行 | 放进 quarantine 并记录 reason（约 210 行），不要静默删除 | — |
| 11-02、11-03 port 0 | 6 笔大单价格偏离市场约 10%（erroneous prints） | 偏离 date-symbol median 超过 5% 的行要标出来；corrections 修正后变成 0 笔，VWAP 改变约 15 bps | — |
| `corrections.jsonl` | 包含重复消息、先 CORRECT 后 BUST、3 个不存在的分区 id、指向 MISSING/CORRUPT/INCOMPLETE 分区的修正 | 按 seq 排序、去重、last-wins；无法应用的要分类说明（PENDING 还是 UNKNOWN） | — |

**Flaky store 的行为**：大约 15% 的请求返回 503，3% 会 hang 0.5s 后超时，同时在途请求超过 10 个会返回 429。

参考数字（具体值随随机失败波动）：
- 顺序 fetch 约 2.0s；asyncio x8 约 0.5s（约 4 倍加速）。
- x20 会触发大约 10 次 429。
- warm cache 下跑完整个 pipeline 约 0.1s，只有 CORRUPT 分区会被重新下载，因为失败不进 cache。

## 2. 设计要点（面试时边写边说）

1. **先 inventory 再 load**：比对 listing 和 manifest，在下载任何东西之前就知道 MISSING / UNEXPECTED。
2. **错误分类**：
   - Transient（503、429、timeout）→ retry，用 exponential backoff 加 full jitter，设最大尝试次数。
   - Permanent（404、corrupt、schema error）→ 不 retry，记录下来继续处理别的分区。
   - 自己代码的 bug → 让它 crash，不要用宽泛的 `except Exception` 吞掉。
3. **并发**：
   - `asyncio.Semaphore(8)` 把并发数控制在服务端限制以下。
   - backoff 的 sleep 放在 semaphore **外面**，避免等待时占着并发槽位。
   - parquet 解码用 `asyncio.to_thread`。pyarrow 的 C++ 代码会释放 GIL，所以线程能真正并行。
4. **Cache**：
   - key = (路径, ETag, SCHEMA_VERSION)。
   - 数据文件写完以后再写 `.json` marker 作为 commit 标志，两者都通过 `atomic_path`（写 tmp 文件，再 `os.replace`）。
   - 失败的结果不进 cache。
5. **正确性**：
   - dedup 用 business key（trade_id），不能用整行比较。
   - corrections 的 last-wins 规则保证 idempotent。
   - 汇总表里加 `complete` 列：宁可让下游看到"不完整"，也不要给出一个看起来完整的错误总数。
6. **可观测性**：输出 partition report、quarantine 文件和 summary fingerprint；做 golden regression；画一张非工程师也能看懂的图。

## 3. Follow-up questions 参考答案

**Q1. 为什么不能对所有异常都 retry？**
- Permanent 错误（404、数据损坏、schema 错）重试多少次结果都一样，只会浪费时间、拖慢整个 job。
- 把自己的 bug（KeyError、TypeError）也 retry 会掩盖问题。
- **非幂等操作**（比如"下单"、"追加写一行"）如果请求其实成功了、只是响应丢了，retry 会造成重复执行，例如重复下单。要 retry，必须先让操作幂等：用 idempotency key 或 client order id，让服务端去重。

**Q2. 为什么要 jitter？backoff 放在哪里？**
- 不加 jitter 的话，所有 client 会在同一时刻一起重试（thundering herd），把刚恢复的服务端再次打垮。Full jitter 是 `uniform(0, base·2^n)`，能把重试在时间上打散。
- sleep 要放在 semaphore 外面。否则等待中的任务占着并发槽位，真正能发出去的请求就变少了。

**Q3. asyncio / threads / processes？**
- 下载是 I/O-bound：asyncio（单线程、大量并发，开销小）或 threads 都可以。
- parquet 解码和 pandas 转换是 CPU-bound。pyarrow 的 C++ 部分会释放 GIL，所以 `to_thread` 有效；纯 Python 的 CPU 计算会被 GIL 限制，要用 `ProcessPoolExecutor`，代价是数据要 pickle 跨进程传递。
- 加速的上限由三点决定：服务端限流（429）、单个请求的延迟分布（长尾的那个请求决定 gather 什么时候完成）、本地 CPU 解析时间。

**Q4. Cache key 和三种"静默返回错误数据"的情况？**
key = 对象路径 + ETag（内容版本）+ SCHEMA_VERSION（代码版本）。容易出错的三种情况：
- (a) key 里只有路径，上游重新交付了文件，cache 还是旧的；
- (b) 改了 normalize 逻辑却没有 bump version；
- (c) 进程崩溃时写了一半的文件被下次当成有效 cache。

其他的坑：TTL cache 在数据更正之后过期得不够快；cache 了失败的结果；多个进程同时写同一个 key（需要原子 rename 或者加锁）。

**Q5. `os.replace` 为什么是原子的？**
在 POSIX 上，同一文件系统内的 `rename(2)` 是原子操作，读者要么看到旧文件，要么看到新文件，不会看到一半。tmp 文件必须和目标在**同一个目录或文件系统**里，跨文件系统会退化成 copy。NFS 上的语义更弱；S3 这类对象存储单个对象的 PUT 是原子的，但没有"目录"层面的原子性，所以常见做法是先写 `_SUCCESS` 或 manifest 作为 commit 标志，或者用 Delta、Iceberg 这类 table format 提供事务。

**Q6. Correction 指向一个今天缺失的分区，明天重新交付了怎么办？**
无法应用的 correction 不能丢，要持久化到 pending 表里。下次 pipeline 运行时，因为 corrections 是按 seq 全量重放、并且 last-wins，只要分区到了，重放就会自然生效。分区的 ETag 变了以后 cache 自动失效，汇总重算，`complete` 也会变成 True。这就是 idempotent 设计的好处：重跑一遍就能修好，不需要专门写补数据的逻辑。

**Q7. 白天每 15 分钟增量跑一次？**
- 分区的写入可能还没完成，要等上游的 commit marker 或 manifest 更新后再读。
- 汇总可以用增量方式（按分区维护 sum(px·qty)、sum(qty)、count，然后合并），也可以对当天全量重算，数据量小的时候全量更简单。
- 输出带上 as-of 时间戳和 `complete` 标记。
- 写结果时保证原子性，并且一次只有一个 job 在写（用 lock 或调度器保证串行）。

**Q8. 改成 Kafka → Flink 流处理？**
- 每个 port 对应一个 topic 或 partition，key = trade_id，保证同一笔成交的所有事件有序。
- 去重用 Flink 的 keyed state 加 TTL。corrections 作为另一条流，按 trade_id 做 connect / join。
- 按事件时间用 watermark 决定什么时候输出"日汇总"，迟到的数据走 side output 或者触发结果更新。
- Exactly-once = Kafka 的 offset 和 Flink 的 checkpoint 一起提交，sink 用两阶段提交（2PC）或者**幂等 sink**（按主键 upsert）。实践中用幂等 sink 最简单，也最可靠。

**Q9. Postgres/TimescaleDB、MongoDB 还是 Parquet？**
- **Parquet + 对象存储**：列式存储，压缩比高，适合大批量扫描分析（research、回测），append 很便宜，但不适合按行更新或点查。
- **Postgres/TimescaleDB**：需要事务、按 trade_id 点查或更新（corrections 正适合）、复杂 join、并发读写的时候用。TimescaleDB 的 hypertable 按时间自动分 chunk。索引建在 `(trade_id)` 唯一键和 `(symbol, ts)` 上；按 date 做 partition 方便归档和删除过期数据。
- **MongoDB**：schema 灵活，适合文档型、字段经常变化的数据，比如各交易所格式不同的原始消息。但聚合分析和事务不如 PG，用来存 fills 这种结构固定、需要做精确汇总的数据并不合适。
- **Partition 和 shard 的区别**：partition 是在一个系统里把数据按 date 切开，方便 pruning；shard 是把数据分到多台机器上，例如按 symbol hash，用来扩展写入吞吐。shard key 要避免热点（比如 SPY 一个 symbol 的成交量远超其他）。

**Q10. 调度、监控、回补？**
- 用 Airflow 或 cron 调度，每个 date 是一个任务，并且任务必须幂等。
- 监控：job 是否成功、运行时长、每天各状态的分区数、quarantine 行数、pending correction 数、数据新鲜度（最新的分区是哪天）。
- 告警：MISSING 或 CORRUPT 分区超过阈值时 page 上游团队。
- 回补（backfill）：指定 date 范围重跑。因为有 cache 和幂等写入，回补是安全的。

## 4. 常见错误 / 扣分点
- 用一个 `try/except Exception: pass` 包住整个 loop。坏分区被静默跳过，汇总少了一个 port 也没人知道。
- 无限 retry 或者不加 timeout，一个 hang 住的请求就能卡住整个 job。
- asyncio 里调用阻塞的 `time.sleep` 或 `fetch_bytes_sync`，事件循环被卡死，并发等于没有。
- 并发数不设上限，`gather` 一万个请求，被服务端 429 限流。
- 把 schema drift 的分区直接 concat，得到 object 类型的列，`price*qty` 变成字符串重复或者报错。
- dedup 用整行 `drop_duplicates`。重发的那份 port 字段不同，所以去不掉。
- corrections 没有按 seq 排序，或者对同一个 trade 的多次修正只应用了第一次。
- 有分区缺失却仍然输出"完整的"日汇总，没有 `complete` 标记。
- cache key 里没有内容版本，或者直接写目标文件，崩溃后留下写了一半的文件。

## 5. English speak-aloud scripts

**Error policy:**
> "I split failures into three buckets. Transient ones, like 503s, throttling and timeouts, get retried with exponential backoff and jitter, up to a cap. Permanent ones, like a missing object, a corrupt file or an unknown schema, are recorded in the partition report and skipped. Bugs in my own code should crash loudly. The pipeline always finishes and always says which partitions it couldn't use."

**Caching:**
> "The cache key is the object path, its ETag and a schema version that I bump whenever I change the parsing logic. If either the data or the code changes, the entry is invalid. Writes go to a temp file and are renamed into place, and the JSON marker is written last, so a crash never leaves a half-written entry that a later run would trust. I don't cache failures, so a re-delivery is picked up automatically."

**Communicating the result:**
> "Two of the five days are complete. The 4th is missing port 2. The 5th has a corrupt file on port 1. The 6th only delivered 80% of port 0. The summary has a `complete` flag, so downstream users can't mistake a partial total for a real one. 30 corrections are pending until those partitions are re-delivered; re-running the pipeline will pick them up."

## 6. 复杂度
- Inventory：O(P)，P 为分区数。
- Fetch：wall time 约 `ceil(P/C) × 单请求延迟 × (1 + retry 率)`，C 是并发数。
- 解析与 normalize：O(N)，N 为行数。
- Dedup：排序 O(N log N)，用 hash 去重是 O(N)。
- Corrections：O(N + K)，K 是 correction 数，用 `isin` 和 map（hash）。
- 汇总 groupby：O(N)。
- 内存：当前是全量 O(N)。如果数据大到放不下，可以逐分区计算 partial aggregates（sum、volume、count），最后再合并（map-reduce）。
