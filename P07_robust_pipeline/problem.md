# P07 — A Robust, Cached, Concurrent Ingestion Pipeline for Drop-Copy Fills

**Time box: ~60–75 min** · Python stdlib + pandas + pyarrow (asyncio encouraged)

## Context

Every night the post-trade team needs a daily summary of our fills per symbol: number of trades, volume, and VWAP. Fills come from a **drop-copy** service. Each of our 4 order-entry **ports** (exchange sessions) writes one Parquet file per trading date to a remote object store, so the data is partitioned as `date=YYYY-MM-DD/port=N/fills.parquet`.

The current script reads the files one by one and crashes whenever a download fails. When it does finish, it sometimes reports numbers that are quietly wrong. Your job is to build a version that is **correct, fault-tolerant, fast, and re-runnable**, and that tells people exactly what it couldn't do.

Interviewers care more about your reasoning and failure handling than about API trivia. Ask for syntax help whenever you need it.

## Provided infrastructure

`flaky_store.py` (in this folder) simulates the remote store. Treat it as a black box you can't change:

| function | behaviour |
|---|---|
| `list_objects(root) -> list[str]` | relative paths of every object (cheap, reliable) |
| `stat_object(root, rel) -> {"size", "etag"}` | metadata only, like an HTTP HEAD (cheap, reliable) |
| `fetch_bytes_sync(root, rel) -> bytes` | blocking download |
| `await fetch_bytes(root, rel) -> bytes` | asyncio download |

Downloads are slow (tens of ms each) and unreliable. They can raise `TransientStoreError`, `TimeoutError` or `NotFoundError`, and the store may push back if you send it too many requests at once. Read the module docstring for details.

## Data

Run `python3 make_data.py` first. Everything is under `data/p07/lake/` (pass this directory as `root` to the store functions).

- **`date=*/port=*/fills.parquet`**: one row per fill.

| column | type | meaning |
|---|---|---|
| `trade_id` | str | unique fill id, format `T<YYYYMMDD>-<port>-<n>`. This is the business key |
| `ts` | timestamp (UTC) | execution time |
| `symbol` | str | ticker |
| `side` | str | `B` (we bought) / `S` (we sold) |
| `price` | float | execution price |
| `qty` | int | shares |
| `venue` | str | exchange MIC |
| `port` | int | port the record was delivered on |

- **`_manifest.json`** is what the upstream team says it delivered: path, row count and sha256 for each partition.
- **`corrections.jsonl`** holds trade **busts** (`{"seq", "action": "BUST", "trade_id"}`) and **price corrections** (`{"seq", "action": "CORRECT", "trade_id", "new_price"}`) that the exchanges sent after the fact. Apply them in `seq` order.

Assume nothing is clean. The upstream team has been known to change formats without telling anyone.

## Tasks

### Part A: Inventory before you load (≈10 min)
1. Write a **generator** that walks the store listing and yields `(path, date, port)` for each partition file.
2. Compare the listing with the manifest and produce a **partition report**: one row per partition with a status and a detail message. What statuses do you need?

### Part B: Fetching under failure (≈15 min)
1. Implement `fetch_with_retry` for the sync API. Decide which errors are retryable, how long to wait between attempts, and when to give up. Justify each choice.
2. Implement an **asyncio** version that downloads partitions concurrently. Bound the concurrency, and add a client-side timeout.
3. Measure the wall time of sequential vs. concurrent fetching for all partitions. What limits the speed-up? What happens if you allow 20 requests in flight?

### Part C: Parse, normalize, validate (≈10 min)
1. Parse each downloaded partition. Some will fail. Decide which failures are permanent and which aren't, and record them in the partition report instead of crashing.
2. Normalize every partition to **one canonical schema and dtype set**, whatever the producer sent.
3. Validate rows (price, quantity, side). What do you do with invalid rows?
4. Check what you received against what the manifest promised.

### Part D: Caching and persistent results (≈10 min)
1. Add a **persistent on-disk cache** of normalized partitions so a re-run doesn't download or parse again. Define the **cache key** and explain exactly when an entry becomes invalid.
2. Make every write **crash-safe**: a run killed halfway must never leave a file that a later run would trust. A context manager is a natural fit.
3. Show cold vs. warm run times. Should failures be cached?

### Part E: Business logic and the report (≈15 min)
1. Deduplicate fills across ports. What is the key, and which copy do you keep?
2. Apply `corrections.jsonl` and report how many corrections were applied and how many weren't, and **why** each one couldn't be.
3. Produce the daily summary per (date, symbol): `n_trades`, `volume`, `vwap`. Write it to `data/p07/results/` together with the partition report.
4. Some dates are missing data. How do you make sure nobody downstream mistakes a partial total for a complete one?
5. Quantify how much the corrections changed the summary, and look for any fills that look implausible.
6. Make one chart that a non-engineer can use to see the state of the data.

### Part F: Tests (≈10 min, can be discussed rather than fully written)
Write `unittest` tests covering:
- schema normalization
- row validation
- dedup
- corrections, including edge cases
- the retry policy, without real sleeps or real network
- crash-safe writes
- one end-to-end run on a tiny fixture lake in a temp directory
- a regression test against a "golden" summary

## Follow-up questions the interviewer may ask

1. Why not retry every exception? What could go wrong if you retry a non-idempotent operation?
2. Why add **jitter** to exponential backoff? Where should the backoff sleep happen relative to the semaphore?
3. asyncio vs. threads vs. processes: which part of this pipeline is I/O-bound and which is CPU-bound? Does the GIL hurt parquet decoding?
4. What exactly goes into your cache key? Name three ways a cache like this silently serves wrong data.
5. How does `os.replace` make a write atomic? What about readers on another machine (NFS/S3)?
6. A correction arrives for a trade whose partition is missing today. What should happen tomorrow when the partition is re-delivered?
7. The summary job must run idempotently every 15 minutes during the day as data arrives. What changes?
8. How would this look as a streaming job (Kafka → Flink)? Where do exactly-once and idempotent sinks matter?
9. When would you put this data in **Postgres/TimescaleDB** or **MongoDB** instead of Parquet files? How would you partition or shard it? What indexes would you add?
10. How do you schedule, monitor and alert on this pipeline, and how do you backfill a past date?

## 考点（中文）
- **Error handling 设计**：区分 transient 和 permanent（retry 还是不 retry）；exponential backoff 加 jitter；client-side timeout；pipeline 失败时要降级（partial 结果加状态），不能 crash，也不能静默出错。
- **并发模型**：asyncio + Semaphore 限流对应 I/O-bound，`to_thread` / ProcessPool 对应 CPU-bound；要理解 GIL 和服务端 throttling。
- **Caching 与持久化**：cache key（etag + schema version）、invalidation、原子写（tmp + `os.replace`、commit marker），以及 idempotent 重跑。
- **数据正确性**：schema drift、dedup 的 business key、corrections 按 seq 处理（last-wins、重复消息、无法匹配时说明原因），以及 manifest 的 row count / checksum 校验。
- **Context manager / generator / testing**：timer 和 atomic write 用 context manager，listing 用 generator；unit、integration、regression（golden file）三层测试。
- **Cross-team communication**：partition report 和图表要让非工程师一眼看出哪天的数据不完整。
