# 使用手册

**quant-etl** 提供的全部 13 个 MCP 工具的详细说明。

> English version: [USAGE.md](USAGE.md)
> 安装部署见 [INSTALL.zh-CN.md](INSTALL.zh-CN.md)

---

## 一、总览

| 类别 | 工具 | 是否写数据库 |
|---|---|---|
| **执行** | `etl_import_daily` `etl_adjust` `etl_fetch_index` `etl_fill_indicators` | 是 |
| **任务管理** | `list_jobs` `get_job` `get_job_output` `cancel_job` | 否（`cancel_job` 会终止进程） |
| **日志** | `list_etl_logs` `read_etl_log` `summarize_etl_log` | 否 |
| **校验与自省** | `check_data_gaps` `describe_etl_program` | 否 |

你不需要记住工具名——直接用自然语言描述要做什么，助手会挑工具。本手册的价值在于
让你知道**它能做什么、边界在哪、返回值怎么读**。

### 最常用的三件事

```
昨晚的 ETL 有没有卡死？          → summarize_etl_log
最近三天有没有缺数据？            → check_data_gaps
把 600519 昨天的数据补一下       → etl_import_daily
```

---

## 二、通用概念

读懂这一节，13 个工具就都能看懂了。

### 2.1 任务模型：先等一会儿，等不到就转后台

ETL 动辄跑几十分钟，MCP 单次调用等不了那么久。所以执行类工具都是这样：

1. 提交任务，在 `wait_seconds`（默认 30 秒）内**同步等待**；
2. 等到了就直接返回完整结果；
3. 等不到就返回 `job_id`，任务转入后台继续跑。

拿到 `job_id` 之后用 `get_job` 查进度、`get_job_output` 看输出、`cancel_job` 停掉。

### 2.2 串行队列

**任何时刻只允许一个 ETL 子进程在跑。** 因为 DuckDB 只允许一个写者。
同时提交多个任务不会并发，会排队，`get_job` 的 `queue_position` 告诉你排在第几位。

> 这个队列只管得住本服务启动的任务，**管不到 cron**。夜跑时段手动触发补数
> 会和 cron 抢写锁，尽量避开。

### 2.3 状态机

```
queued → running → ┬→ succeeded       成功
                   ├→ failed          失败
                   ├→ partial         部分成功（协议预留，目前不会出现）
                   ├→ stalled         进程活着，但静默超过阈值 ⚠
                   ├→ killed_stalled  超过 max_runtime 被自动终止
                   └→ cancelled       人工取消
```

**`stalled` 不是终态。** 它表示"进程还活着，但久没输出了"，是**报警而不是结论**：

- 任务可能自己恢复（长区间任务本来进度间隔就长），恢复后自动回到 `running`；
- 也可能真卡死了，这时用 `cancel_job` 停掉，再按 `last_line` 的进度定向重跑。

只有超过 `max_runtime` 硬上限才会被自动终止（变成 `killed_stalled`）。

终态是这五个：`succeeded` `partial` `failed` `killed_stalled` `cancelled`。

### 2.4 停顿阈值

多久没输出算 `stalled`，默认按日期跨度分档：

| 跨度 | 默认阈值 |
|---|---|
| ≤ 7 天 | 90 秒 |
| > 7 天 | 600 秒 |

每次调用都可以用 `stall_timeout` 覆盖。

### 2.5 退出码

| 码 | 状态 | 含义 |
|---|---|---|
| `0` | `succeeded` | 成功 |
| `1` | `failed` | 失败 |
| `2` | `failed` | **命令行用法错误**（Python 标准库写死的，不是"部分成功"） |
| `3` | `partial` | 部分成功（协议预留，spring 侧目前不产出） |
| 其它 | `failed` | 未知退出码一律按失败，不做乐观解读 |

### 2.6 参数格式

| 参数 | 格式 | 例子 |
|---|---|---|
| `begin` / `end` | `YYYYMMDD` 八位数字，必须是真实存在的日期 | `20260817` |
| `codes` | 6 位数字，可带 `.SH` / `.SZ` / `.BJ` 后缀 | `["600519", "000001.SZ"]` |
| `exchanges` | `sh` / `sz` / `bj` / `all`（大小写都接受） | `["sh", "sz"]` |

非法值会**在启动子进程之前**被拒绝，并明确告诉你哪个参数、为什么。

### 2.7 数据源

| 程序 | 可选 `source` |
|---|---|
| `etl_import_daily` | `lday` / `bstock` / `tdx`（默认 `bstock`） |
| `etl_fetch_index` | `lday` / `bstock`（默认 `bstock`） |
| `etl_adjust` | 仅 `bstock` |

> `lday` 和 `tdx` 依赖 Windows 上的通达信目录，**Linux 服务器上只有 `bstock` 可用**。

---

## 三、执行类工具

四个工具都支持这套 MCP 层参数：

| 参数 | 默认 | 作用 |
|---|---|---|
| `wait_seconds` | 30 | 同步等待多久，超时转后台 |
| `stall_timeout` | 自动分档 | 静默多久判为 `stalled` |
| `max_runtime` | 7200 | 硬上限，超时自动终止 |
| `retries` | 0 | 失败自动重试次数，上限 5 |
| `chunk` | `none` | `month` / `year` 分片（仅前三个下载型工具有） |

**关于 `retries`**：只对 `failed` 和 `killed_stalled` 重试。不重试 `cancelled`
（那是人的决定），也不重试 `partial`（重跑整段是浪费，应按缺口定向补）。

**关于 `chunk`**：长区间按自然月或自然年切成多段，逐段串行执行。
单段卡死只损失一段——返回值里的 `failed_segments` 会告诉你是哪几段，
配合 `codes` 精确重跑，把"整晚白跑"降级为"丢一段"。
跨度超过 **3650 天**时不分片会被直接拒绝。

---

### `etl_import_daily`

下载股票日线交易明细并入库。

**参数**

| 名称 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `begin` | string | **必填** | 起始日期 `YYYYMMDD` |
| `end` | string | 同 `begin` | 结束日期；省略表示只跑单日 |
| `codes` | array | 全市场 | 指定股票代码 |
| `exchanges` | array | `["all"]` | 指定交易所，`codes` 优先级更高 |
| `source` | string | `bstock` | `lday` / `bstock` / `tdx` |
| `print_only` | bool | `false` | **干跑**：走完整下载流程但不写库 |

**什么时候用**：补某天的日线数据。这是补数闭环里最常用的一个。

**注意**：`print_only=true` 是最安全的试手方式——它真的会去下载，但一个字节都不写库。
第一次用这个服务时建议先干跑一次。

---

### `etl_adjust`

下载复权因子，并稠密化到逐个交易日。

**参数**：同上，但 `source` 只能是 `bstock`，且没有 `print_only`。

**什么时候用**：补复权因子。

**注意**：**即使区间内没有新的复权事件也应该执行**——它同时负责把 `ADJ_FACTOR`
表向前填充到 `end` 日期。所以"这几天没有除权除息，跳过吧"是错的。

---

### `etl_fetch_index`

下载指数日线交易明细并入库。

**参数**：同 `etl_import_daily`，但 `source` 只有 `lday` / `bstock`，无 `print_only`。
`codes` 传的是**指数代码**（如 `["000001"]` 表示上证指数）。

---

### `etl_fill_indicators`

补齐量比 / 涨跌停 / 股本三类衍生指标。

**参数**

| 名称 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `begin` / `end` / `codes` / `exchanges` | | | 同上 |
| `forcerun` | bool | `false` | 非交易日也强制执行 |
| `targets` | array | 全做 | 子集选择，见下 |

`targets` 可选：`fill_volratio`（量比）、`update_limit`（涨跌停）、`fill_shares`（股本）。
**无论你传什么顺序，都会按 `fill_volratio → update_limit → fill_shares` 执行**——
顺序是固定的，避免依赖颠倒。

**前置条件**：三项都依赖当日日线，**必须先跑 `etl_import_daily`**。
`fill_shares` 还额外依赖 `CAPITAL_DETAIL` 表有数据，否则新股会被跳过。

**返回值形状不同**：它为每个 target 建一个独立任务，所以返回的是
`{"targets": [...], "jobs": [...]}` 而不是单个 `job_id`。

---

## 四、任务管理

### `list_jobs`

列出任务，最近的在前，**`stalled` 一律置顶**。

| 参数 | 默认 | 说明 |
|---|---|---|
| `status` | 全部 | 按状态过滤 |
| `limit` | 20 | 返回条数 |

返回里的 `needs_attention` 直接列出需要你看一眼的 `job_id`
（`stalled` / `failed` / `partial` / `killed_stalled`）。

---

### `get_job`

查单个任务的详情。这是**判断卡死的主要工具**。

关键字段：

| 字段 | 含义 |
|---|---|
| `status` | 状态机里的状态 |
| `status_hint` | 一句人话解释，`stalled` 时会告诉你有哪两条路 |
| `idle_seconds` | **距上次输出多久**——判断卡死的核心依据 |
| `progress` | 结构化进度 `{done, total, percent}` |
| `last_line` | 最后一行输出，通常是 `已处理: 3400/5400` |
| `elapsed_seconds` | 已运行时长 |
| `queue_position` | 排队位置（`queued` 时） |
| `attempt` / `max_attempts` | 第几次尝试（用了 `retries` 时） |
| `exit_code` / `exit_hint` | 退出码及其解释 |

---

### `get_job_output`

看任务的子进程输出尾部。

| 参数 | 默认 | 说明 |
|---|---|---|
| `job_id` | **必填** | |
| `tail` | 200 | 返回最后多少行 |
| `only_errors` | `false` | 只保留 ERROR / WARNING 行 |

任务失败时先用 `only_errors=true` 看，通常一眼就能定位。

---

### `cancel_job`

终止任务。

| 参数 | 默认 | 说明 |
|---|---|---|
| `job_id` | **必填** | |
| `force` | `false` | `true` 时在宽限期后补 `SIGKILL` |

先发 `SIGTERM`，等一个宽限期；`force=true` 才会强杀。

**为什么可以放心杀**：三个下载型 ETL 在下载阶段都**不持有数据库写锁**，
此时终止是安全的。而下载阶段恰恰是唯一会卡死的阶段。

排队中（`queued`）的任务被取消后不会再执行。

---

## 五、日志

这三个工具与 `get_job` 的心跳**互补**：心跳只看得见本服务启动的任务，
**cron 昨晚跑的那一次只能靠日志复盘**。

### `list_etl_logs`

有哪几天的日志、各多大、什么时候改的。`limit` 默认 30。

日志一天一个文件（`stockdailyYYYYMMDD.log`），当天所有 ETL 程序共用。

---

### `read_etl_log`

读日志尾部并过滤。

| 参数 | 默认 | 说明 |
|---|---|---|
| `date` | 当天 | `YYYYMMDD` |
| `tail` | 200 | 返回行数 |
| `level` | 全部 | 精确匹配 `ERROR` / `WARNING` / `INFO` |
| `module` | 全部 | **子串**匹配，`import_daily` 能匹配 `etl.import_daily` |
| `keyword` | 全部 | 整行子串匹配 |
| `max_bytes` | 200000 | 只读文件尾部这么多字节，超出会标记 `truncated` |

---

### `summarize_etl_log`

**判断某晚是否卡死的主力工具。** 只需一个可选的 `date`。

返回内容：

| 字段 | 含义 |
|---|---|
| `level_counts` | 各级别计数 |
| `modules` | **按模块分组**的明细（一天一个文件多程序共用，不分组会糊成一团） |
| `stall_suspects` | **卡死嫌疑清单** |
| `recent_errors` | 最近的错误样本 |
| `first_line_at` / `last_line_at` / `span_seconds` | 时间跨度 |
| `unparsed_lines` | 解析不了的行数（非 0 说明日志格式变了） |

#### `stall_suspects` 的判据

> **某个模块的最后一行如果是进度行，就是卡死嫌疑。**

因为正常跑完会有「批量采集完成」，正常失败会有 `ERROR`。两者皆无而止于
「已处理: 300/5400」，正是"停在下载处"的形态。

**这个判据与你什么时候看无关**——隔几天复盘同样成立，不像"距今多久没更新"
那种一放就失效。

---

## 六、校验与自省

### `check_data_gaps`

查数据缺口。基于**数据库事实**判定：用 `STOCK_INFO` + `TRADE_CAL` 算出预期记录数，
反查实际落库数。ETL 逻辑怎么变，检查结果都自动跟着变。停牌股票不计入缺失。

| 参数 | 默认 | 说明 |
|---|---|---|
| `begin` | **必填** | |
| `end` | 同 `begin` | |
| `codes` / `exchanges` | 全市场 | 缩小检查范围 |
| `include_index` | `false` | 是否一并校验指数日线 |
| `forcerun` | `false` | 非交易日也检查 |
| `max_detail` | 200 | `missing_codes` 的条数上限 |
| `timeout_seconds` | 300 | 检查超时 |

**返回里看两处**：

- `core.checks[].gap_dates` —— 哪几天缺、缺几条 → 喂给 `begin`/`end`
- `core.checks[].missing_codes` —— 具体缺哪些代码 → 喂给 `codes`

`gap_dates` 总是完整的（每项只是几个数字）；`missing_codes` 受 `max_detail`
约束，截断时 `missing_codes_truncated` 为真，完整明细见同一项里的 `csv_path`。

**以 `status` 判断结果**（`complete` / `gaps_found` / `error`），
不要用退出码——它转发的工具是另一套退出码语义。

**它是只读的，不进串行队列**，所以不会被正在跑的补数任务挡住。但 DuckDB 单写者——
如果此刻有写任务持有写锁，只读连接会打不开，届时会明确报错并提示。

---

### `describe_etl_program`

返回某个 ETL 程序**当前真实的**命令行参数定义。`name` 必填，取值是六个程序名之一：
`adjust` / `import_daily` / `fetch_index` / `fill_volratio` / `update_limit` / `fill_shares`。

结果直接来自 spring 的 argparse，**永不过期**；`help` 文本原样透传。

**两个默认值陷阱**：

1. `import_daily` / `adjust` / `fetch_index` 的 `begin`/`end` 默认值是**自省当天**，逐日变化；
2. 三个 `fill_*` 的 `begin`/`end` 在 argparse 层默认为 `null`，真实默认（T-1／今天）
   是解析之后才套用的，**只能从 `help` 文本读出来**。

---

## 七、典型工作流

### 7.1 早晨复盘

```
1. summarize_etl_log            昨晚有没有模块停在进度行上？
2. check_data_gaps              到底缺哪几天、哪些股票？
3. etl_import_daily(codes=…)    按缺口定向补
4. check_data_gaps              复核
```

这是本服务的核心用法。前两步都是只读的，可以放心跑。

### 7.2 卡死处置

```
1. get_job                      status 是 stalled？看 idle_seconds 和 progress
2. （判断）                      长区间任务本来间隔就长——先看 progress 有没有在动
3. cancel_job(force=true)       确认卡死后终止
4. etl_import_daily(codes=…)    按 last_line 的进度定向重跑
```

### 7.3 长区间回补

```
etl_import_daily(begin=20200101, end=20261231, chunk="year", retries=1)
```

按自然年切段串行执行，单段失败自动重试一次。返回的 `failed_segments`
指出哪几段还要补。不分片的话超过 3650 天会被直接拒绝。

### 7.4 第一次试手

```
etl_import_daily(begin=<某个交易日>, codes=["600519"], print_only=true)
```

干跑，走完整下载流程但不写库，两秒返回。

---

## 八、边界与限制

### 做不到的事（设计如此）

- **不能执行任意命令**，只能启动白名单里的 6 个 ETL 程序
- **不能执行任意 SQL**，没有查询工具（只读查询请用 `quant-mcp`）
- **不能删表清库**

### 需要你自己注意的

- **夜跑时段别手动触发写入类工具**——会和 cron 抢 DuckDB 写锁。串行队列管不到 cron
- **Linux 上只有 `bstock` 数据源可用**，`lday` / `tdx` 依赖 Windows 通达信目录
- **`etl_fill_indicators` 依赖 `etl_import_daily`**，顺序错了会补出空数据
- **服务本身没有鉴权**，能连上端口就能触发写库（详见 [INSTALL.zh-CN.md](INSTALL.zh-CN.md) 第十节）

### 数据安全

ETL 的入库是**幂等 upsert**，重跑不会损坏数据。所以最坏情况是"浪费时间、占住写锁"，
不是"数据被写坏"。这一点让误操作的代价比看起来小得多。

---

## 九、常见误区

| 误区 | 实际情况 |
|---|---|
| `stalled` 表示任务失败了 | 不是。它是**报警**，进程还活着，可能自己恢复 |
| 退出码 2 表示部分成功 | 不是。2 是**命令行用法错误**，属于失败 |
| 区间内没有除权就不用跑 `etl_adjust` | 要跑。它还负责把 `ADJ_FACTOR` 向前填充到 `end` |
| 提交多个任务会并发加快 | 不会。DuckDB 单写者，一律排队串行 |
| 可以用 `check_data_gaps` 的退出码判断 | 不要。用返回值里的 `status` 字段 |
| `summarize_etl_log` 能看到本服务跑的任务 | 看不到，它读的是 ETL 自己的日志文件。本服务的任务用 `list_jobs` |
