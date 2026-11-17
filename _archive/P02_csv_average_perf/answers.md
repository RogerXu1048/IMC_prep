# P02 参考答案 — CSV 平均价 + 性能工程

> 文件：`csv_average.py`（核心实现，只用标准库）、`solution.py`（Parts A–H）、`test_solution.py`（unittest）。
> 运行：`python3 solution.py`（~45 s），或 `python3 solution.py --parts A,B`；测试：`python3 -m unittest -v test_solution`。
> 下面的数字来自一台 10 核 Apple Silicon 笔记本，你的机器会不同，**重要的是相对关系和解释**。

## 1. 数据里埋了什么（以及怎么发现）

| 问题 | 数量 | 发现方式 / 处理 |
|---|---|---|
| 空 price / `N/A` / 负价 / `0.00` | 300 / 300 / 200 / 50 | `parse_price_cents` 返回 None → reject 并计数 |
| 空 symbol | 30 | reject |
| 多一列（`…,X`）/ 少列 | 100 / 30 | `len(row) != 4` → reject |
| 中间重复的 header（文件拼接） | 3 | `row == header` → skip（不算 bad） |
| 空行 | 20 | skip |
| `" aapl "` / `"Nvda"` 等大小写空格 | 300 | strip + upper，**接受**并计数 |
| 带千分位的 `"1,234.50"`（CSV 引号包住） | 200 | 去逗号，**接受** |

关键：**policy 要说出来**，而且结果要和一个**独立的 oracle**（`make_data.py` 在生成数据时用整数分直接算出的 `expected_ticks.json`）逐个 symbol 精确对上。solution 的 Part A 输出 `EXACT MATCH`。

**埋得最深的坑（Part B）**：`pd.read_csv(..., engine="c", usecols=[...], on_bad_lines="skip")` —— 一旦用了 `usecols`，C engine **不再把多一列的行当成 bad line**，那 100 行会被悄悄算进去，43 个 symbol 的平均价错了（最大误差 3.7e-3）。不用 usecols 的 naive 版本和 pyarrow engine 都是对的。教训：**每一个“优化”都要和 baseline/oracle 对答案**，regression test 能抓住这个 bug，unit test 抓不住。

另一个小坑：naive `read_csv` 会报 `DtypeWarning`：中间重复的 header 把字符串 `"size"` 塞进了 int 列 → 整列变成 object。永远检查 `df.dtypes`。

## 2. 测量结果（解释比数字重要）

| impl | 时间 | peak RSS | 说明 |
|---|---|---|---|
| pure Python streaming | ~1.7 s（~1.75M rows/s） | 24 MB（≈空解释器 22 MB） | 内存 O(#symbols)，跟文件大小无关 |
| pandas naive | ~1.9–2.3 s | ~915 MB | 读所有列、全部变成字符串对象；内存 ≈ 7× 文件大小 |
| pandas fast（pyarrow + usecols + category） | ~0.53 s | ~690 MB | pyarrow 多线程解析；但还是把整列装进内存 |
| multiprocessing（8 workers，warm pool） | ~0.35–0.5 s | 父进程 25 MB + 每个 worker ~65 MB | ~3.5–4.7× speed-up |

- **tracemalloc 的盲区**：500k 行上 pandas_fast 的 tracemalloc peak 只有 ~53 MB，而 RSS 增加了几百 MB —— pyarrow 用自己的 memory pool（jemalloc/mimalloc），不走 Python allocator，tracemalloc 看不见；numpy 会注册自己的 buffer 所以能看见。所以：**Python 对象分配看 tracemalloc，进程真实内存看 RSS（`resource.getrusage().ru_maxrss`，macOS 是 bytes、Linux 是 KB），每个实现在一个新进程里测**（peak 是单调的，同一进程里测多个实现会互相污染）。
- **为什么报 min 和 median**：min 最接近“没有干扰时的真实成本”（噪声只会让它变慢），适合比较实现；median 代表典型情况；生产延迟则要看 p99。至少 repeat 3 次，并区分 cold（第一次：磁盘缓存、import、进程启动）和 warm。

## 3. 六个问题的参考答案

### Q1. 用什么数据结构存内存里的数据？
- 只要平均值：`dict[str, [sum_cents, count]]`。每行 O(1) 平均（hash 查找 + 原地加），总内存 O(S)（S = symbol 数），**不需要存行**。用 list 而不是 tuple 是为了原地更新，少一次 dict 写入。用 integer cents：精确、可结合（并行 reduce 结果与顺序无关）。
- 如果要 median / percentile：mean 可以流式算，median 不行——需要存每个 symbol 的所有价格（O(N)），或者用近似：固定 tick 的价格 histogram（价格是离散 tick，`Counter[price_ticks]`，内存 O(不同价位数)），或 t-digest/P² 等 streaming quantile。
- 如果 50,000 个 symbol：dict 仍然没问题（几 MB）。如果是几亿个 key 才考虑把 symbol 映射成整数 id，用 numpy 数组 `np.bincount(ids, weights=cents)` —— 这也是 pandas category/groupby 的底层思路。
- 数据装不进内存：streaming（本题已经是）、分块（`chunksize=`）、按 symbol 分区后各自处理、或者列式格式（parquet）只读需要的列。
- 如果要存行：列式（numpy/arrow 数组，每列连续内存，向量化友好）>> list of dicts（每行一个 dict 约几百字节）。`@dataclass(slots=True)` 能把 per-object 内存降低 ~40%。

### Q2. 现代 Python 的内存管理和错误处理
- **资源**：一律 `with`（context manager）—— 文件、锁、临时目录（`tempfile.TemporaryDirectory`）、进程池（`with ProcessPoolExecutor()`）。异常时也会关闭/清理。自己写用 `@contextlib.contextmanager` + `try/finally`（solution 的 `timer`、`prefix_file`）；多个动态资源用 `contextlib.ExitStack`。全局状态改了要在 `finally` 里恢复（Part H 的 `sys.setswitchinterval`）。
- **内存**：CPython 主要靠引用计数（对象没人引用立刻释放）+ 分代 GC 处理循环引用。写法上：用 generator/iterator 流式处理而不是 `readlines()`；避免中间大列表；大数据用 numpy/arrow 的连续 buffer；`__slots__`；需要时 `del` 大对象（但不要到处 `gc.collect()`）。
- **错误处理**：对“预期中的脏数据”不要抛异常终止整个任务——按 policy 计数、记录前几条样例（`logging`，不要 print 几千行），最后汇报；对“程序 bug/不可恢复的错误”让它抛出（fail fast）。`try` 块要窄（只包 `float(text)`），捕获具体异常（`ValueError`），不要 `except Exception: pass`。可选：严格模式（bad rows 超过阈值就失败）。

### Q3. 怎么验证正确性：unit + e2e（+ regression + equivalence）
- **Unit**：纯函数的边界情况——`parse_price_cents` 的 `"320.5"`、`"322"`、`"1,234.50"`、`"0.29"`（浮点陷阱：0.29*100 = 28.999…）、`"nan"`、`"inf"`、空串、负数；`aggregate_rows` 的空行、header、少列多列、空 symbol、空输入。用 `subTest` 做表驱动。
- **Chunking 的性质测试**：对一个小文件的**每一个**切分点，`read_chunk(0,k) + read_chunk(k,end) == 原文件`（不丢、不重）。这就是 property-based testing 的思路（装了 Hypothesis 可以随机生成文件）。
- **E2E**：小文件 + 手算的期望结果（`expected_small.json`），覆盖所有 policy 分支。
- **Equivalence**：parallel == serial（整数分 → 可以要求**完全相等**，float 的话只能用容差，而且并行的加法顺序不同结果会变）。
- **Regression / golden**：全量文件 vs 独立 oracle。oracle 最好来自**不同的实现**（这里是生成器用 numpy 直接算），否则只是在测“和上次一样”。Part B 的 usecols bug 只有这一层能抓到。

### Q4. 交易场景关心哪些性能指标？
- **Latency**（每条消息从到达到处理完）：看分布——p50、p99、p99.9、max，而不是 mean；还有 **jitter**（方差/尾部）。交易里最在乎尾部，因为最慢的时候往往是行情最剧烈、机会最多的时候。
- **Throughput**（rows/s、msgs/s）：尤其是**峰值**（开盘、新闻时的 burst）下能否跟上；跟不上就会排队 → 延迟爆炸。
- **Handoff latency**（producer → consumer）：线程/进程间传递一条消息的时间——队列锁、唤醒（futex/condition variable）、cache line 在核之间迁移、Python 里还有 GIL。Part H 的测量：batch=256 时 handoff p50 ≈ 3.8 ms ≈ GIL switch interval 5 ms 的量级；把 switch interval 调到 0.1 ms，handoff 降到 ~95 µs。batch=1 时 consumer 跟不上，队列满，延迟变成排队延迟（Little's law：等待时间 ≈ 队列深度 / 吞吐）。
- **Batching 的 tradeoff**：batch 越大吞吐越高（每行分摊的锁/唤醒成本越少），但每行要等 batch 填满，延迟越大（“oldest-row age”随 batch 线性增长）。低延迟系统一般用小 batch 或“有多少拿多少”的 drain 策略 + busy-polling + 无锁 ring buffer（如 LMAX Disruptor）。
- 其他：资源效率（CPU/内存/每核吞吐）、启动时间（冷启动）、队列深度（back-pressure 的信号）、丢包/丢消息率。

### Q5. 怎么找热点（时间 + 内存分配）
- **时间**：
  - `cProfile`（deterministic/instrumenting，每次函数调用/返回都记录）：精确调用次数，但开销大（这里同样 500k 行从 ~0.3 s 变成 ~0.75 s，慢 2–3×；tracemalloc 更夸张，~6×），会**扭曲**小函数多的代码，看不到行级别。结果：`aggregate_rows` 自身 0.40 s、`parse_price_cents` 0.14 s 自身 + 内部 `strip`/`round`/`float` 等内建调用。
  - `line_profiler`：逐行计时，定位到具体哪一行。
  - **Sampling profiler**（`py-spy`、`scalene`、Linux `perf`）：每隔固定时间（如 100 Hz–10 kHz）中断/读取调用栈，统计哪个栈出现得多。开销低（py-spy 是外部进程读目标进程内存，不用改代码，生产可用），能生成 flame graph；但它是统计结果，短函数可能采样不到，看不到调用次数。
  - **Hardware-trace / PMU**：`perf stat`/`perf record` 用 CPU 的 performance counters（cycles、instructions、cache-misses、branch-misses）；Intel PT（Processor Trace）记录**每一个**分支，可以重建完整的执行轨迹，开销很低但数据量巨大、解码复杂，只在 Intel CPU 上。适合 C/C++ 热路径的 ns 级分析；对 Python 来说你会看到的大多是解释器的 `_PyEval_EvalFrameDefault`（3.12+ 有 `-X perf` 让 perf 能看到 Python 函数名）。
  - **Micro-benchmark**（`timeit`）：定位到热点后，比较候选写法：`float()` 33 ns、`round(float*100)` 71 ns、`Decimal()` 64 ns、手写 `split('.')` + `int` 200 ns（反而更慢——Python 层的操作越多越慢）；`csv.reader` 190 ns/行 vs `str.split` 118 ns/行（快但不能处理引号里的逗号——**正确性优先**）。注意 micro-benchmark 的陷阱：常量折叠、缓存过热、不代表真实数据分布。
- **内存**：`tracemalloc`（Python allocator 的分配，能按行号 snapshot 对比，找泄漏）；`memray`（记录所有分配包括 C 扩展，flame graph）；`resource.getrusage` / `psutil` 看 RSS；`scalene` 同时给 CPU + 内存 + 拷贝量。记住 tracemalloc 看不见 pyarrow 的 memory pool。

### Q6. 怎么并发
- 这个任务是 **CPU-bound 的 Python 字节码**（解析、dict 更新）→ CPython 的 GIL 让同一时刻只有一个线程执行字节码 → **线程没用**（甚至更慢，因为切换和锁争用）。线程有用的情况：I/O-bound（网络/磁盘等待时会释放 GIL）、或者计算在释放 GIL 的 C 扩展里（numpy、pyarrow 的多线程 CSV reader 就是这样做的）。Python 3.13t（free-threaded，无 GIL 构建）改变了这一点，但生态还在适配。
- **asyncio**：单线程协作式并发，只适合大量并发 I/O（如同时从 50 个 URL/端口拉数据），对 CPU-bound 毫无帮助。
- **multiprocessing**（solution 的做法）：按**字节范围**把文件切成 `workers × 4` 块（多于 worker 数，用于负载均衡），每块对齐到行边界（规则：一行属于它**开始**所在的块）→ 每个 worker 返回小的 partial `{sym: [sum, count]}`（只有 ~500 个 key，序列化成本极小）→ 父进程 reduce。这是标准的 **map-reduce**；因为 integer 加法可结合可交换，结果和 serial 完全一致（test 证明）。注意：如果 CSV 的引号字段里有换行，按字节切分会出错。
- 结果（多次运行有波动）：1→2→4→8→10 workers ≈ 0.8×、1.8–1.9×、3.2–3.4×、4.4–4.7×、3.7–4.9×；8 → 10 基本不再提升。

**为什么不是 10×？（Amdahl 等）**
- Amdahl：speed-up(N) = 1 / (s + (1−s)/N)。从 N=10 的测量反推 serial fraction ≈ 10–20%（随运行波动）→ 上限 ≈ 5–9×。串行部分：进程启动（spawn 要重新 import 模块——所以 `csv_average.py` 只依赖标准库，`solution.py` 把 pandas 放在函数里 import）、切块、结果 pickle 回传、reduce、父进程的 `ProcessPoolExecutor` 调度。
- 1 个 worker 比 serial 还慢（0.8×）：要把整块读成 bytes、decode、`StringIO` 再解析，多了一次拷贝。
- Apple Silicon 的 10 核里有 efficiency cores（慢核）——最后一个慢核上的 chunk 拖累整体（straggler）；更多更小的 chunk 能缓解。
- 内存带宽 / 磁盘读取（这里文件在 page cache 里，所以主要是 CPU）。
- 还可以：用 pyarrow（C++ 多线程）直接读 → 0.53 s 单进程；或者 pyarrow + 多进程按文件分区。

## 4. Correctness proof → Benchmarking → Micro-benchmarking → Implementation 循环
1. **Correctness proof**：先写最简单、明显正确的版本（Part A）+ oracle + 测试。这是之后所有优化的“标准答案”。
2. **Benchmark**（端到端）：在真实大小的数据上测 wall time 和内存，repeat，冷/热区分，记录环境（机器、Python 版本、数据）。得到 baseline：1.7 s、24 MB。
3. **Profile → Micro-benchmark**：找出热点（`aggregate_rows` 循环本身 + 价格解析），针对热点比较候选实现（`float` vs `Decimal` vs 手写解析、`csv` vs `split`）。
4. **Implementation**：只改热点，改完**先跑测试**（Part B 的 trap 说明“快但错”很容易发生），再 benchmark 确认真的变快了（而且不是噪声——看多次 repeat 的分布）。
5. 重复，直到达到目标或者收益递减（Amdahl）。每一步都要能回答：“快了多少？怎么知道还是对的？瓶颈现在在哪？”

## 5. Part G：数值精度
AAPL 574,433 个价格的平均（精确值用 integer cents 算）：
- 朴素 `s += v` 循环：误差 ~3e-12（误差随 n 线性累积，O(n·ε)）。
- Python 3.12 的内建 `sum()`：对 float 使用 **Neumaier 补偿求和**（3.12 新特性），误差 ~1e-14（只剩最后除法的舍入）。
- `math.fsum`：正确舍入的精确和；`np.sum`：pairwise summation，误差 O(log n·ε)，这里也 ~1e-14。
- float32：误差 2.6e-6 —— 一个价格在 $232 时已经是 0.0003 cent 级别，单精度对金额完全不够。
- 结论：价格用**整数 tick/cent**（精确、可结合、比较时没有 `0.1+0.2 != 0.3` 问题），只在输出时转成 float/Decimal。

VWAP 用整数：`sum(cents * size) / sum(size)`，Python int 不会溢出（numpy int64 在极端情况下可能溢出，要注意）。按分钟平均：ISO 时间戳的前 16 个字符就是分钟，`str.slice(0, 16)` 比 `pd.to_datetime` 快得多。

## 6. Follow-up 补充
- **Q7（10 核不是 10×）** 见上面的 Amdahl 部分。
- **Q8（integer cents）** 见第 5 节。另外：交易所价格本来就是整数 tick（如 $0.01 或期权的 $0.05），用 tick 表示还能直接当数组下标。
- **Q9（live feed 而不是文件）**：同样的 O(1) per message 更新（mean 可增量更新）；关注每条消息的延迟而不是总时间；不能再“读完再算”；并行要按 symbol 分区（同一 symbol 的消息必须由同一个 worker 按顺序处理）；需要 back-pressure、监控队列深度、处理断线/重连/重复消息。

## 7. 常见错误 / 扣分点
- 直接 `pd.read_csv(path).groupby('symbol').price.mean()` —— 会因为多一列的行直接报错，或者 price 列变成字符串，`mean()` 报错/结果错，且不报告丢了什么。
- 只用 float 存价格、并行后结果和 serial 差几个 ulp 就判定“测试失败”或者干脆不测。
- 在同一个进程里先后测多个实现的 peak RSS（peak 只增不减）。
- benchmark 只跑一次；把 cProfile 下的时间当成真实时间。
- 说“用 threading 并行”而不提 GIL；或者说“用 asyncio 加速”。
- 按字节切块时没有处理行边界（丢行或重复行）。
- 不知道 `on_bad_lines` + `usecols` 的坑没关系，但**必须**有能抓住这类问题的测试。

## 8. English speak-aloud scripts

**Opening:**
> "I'll start with the simplest correct version: a streaming loop with the csv module and a dictionary from symbol to a running sum and count, so memory is proportional to the number of symbols, not the number of rows. I'll keep prices as integer cents so the sums are exact. First I need a policy for bad rows — I'll skip and count them, normalise the symbol case, and log a few examples — and I'll check the result against an independent oracle before I optimise anything."

**Benchmark & profiling:**
> "The baseline is 1.7 seconds and 24 megabytes. The profile says the time is in the per-row loop and the price parsing, so I micro-benchmarked the alternatives: float parsing is about 30 nanoseconds and the rest is interpreter overhead. Pandas with the pyarrow engine is three times faster but uses 700 megabytes, so it's a memory-for-speed trade-off. One of my pandas variants was faster but wrong — the C engine with usecols silently accepts rows with an extra column — and only the regression test against the oracle caught it."

**Concurrency:**
> "Threads won't help because the work is CPU-bound Python bytecode and the GIL serialises it. I split the file into byte ranges aligned to line boundaries, have each process return a tiny partial sum and count per symbol, and reduce them — a map-reduce. Because the sums are integers the parallel result is exactly equal to the serial one. I get about four to five times speed-up on eight workers; from Amdahl's law that implies roughly ten to twenty percent serial overhead — process start-up, chunk setup and the reduce — plus stragglers on the efficiency cores."

## 9. 复杂度
- Serial：时间 O(N)（N = 行数），额外内存 O(S)。
- Parallel：时间 O(N/P + S·C)（C = chunk 数，reduce 成本），内存每个 worker O(chunk 大小 + S)（我们把整块读进内存；可以改成流式读 chunk 降到 O(S)）。
- pandas：时间 O(N)，但内存 O(N)（整列物化），groupby 是 hash 分组 O(N)。
- Producer/consumer：每行 O(1)，队列有界 → 内存 O(maxsize × batch)。
