# 接入建议：spring 新增的 `sync_suspension` / `sync_limit_pool`

- 提出日期：2026-09-13
- 提出方：spring 侧重构（核对模块引入第三方停牌 / 涨跌停交叉核对）
- 状态：**待实施**——本文档只描述改动建议，未改动本仓库任何代码
- 对应 spring 分支：`dev`（该重构已完成并通过全量测试，未合入 master）

---

## 一、背景：spring 侧发生了什么

spring 新增了两份第三方事实数据，用于交叉核对主表：

| 新表 | 内容 | 用途 |
|---|---|---|
| `SUSPENSION_DAILY` | 每日停牌名单（akshare 东财源） | 交叉核对 `STOCK_DAILY.tradestatus` |
| `LIMIT_POOL_DAILY` | 每日涨跌停股池（涨停 + 跌停，`limit_type` 区分） | 交叉核对 `DAILY_BASIC` 的涨跌停价与标志 |

对应两个新 ETL：

| 程序名 | 模块 | 说明 |
|---|---|---|
| `sync_suspension` | `etl.sync_suspension` | 停牌名单入库 |
| `sync_limit_pool` | `etl.sync_limit_pool` | 涨跌停池入库，`--only up/down/all` |

spring 侧已完成三处契约注册：`tools/describe_cli.py` 的 `PROGRAMS`、`config/pipeline.yaml`、
`tests/unit/test_cli_contract.py` 的 `MODULES`。**即本服务的自省链路已经可用。**

`tools/check_daily.py` 同时新增三个核对项（停牌一致性 / 涨停一致性 / 跌停一致性），
结果出现在 `--json` 的 `warnings.checks[]` 里。

---

## 二、问题：本服务当前**无法启动**这两个 ETL

`schema.PROGRAMS` 是安全白名单（其注释原文）：

> PROGRAMS 同时是**安全白名单**：只有在此声明的模块能被启动，本服务不提供任意命令执行。

两个新程序不在其中，`params.require_known_program()` 会直接拒绝。本服务没有
「按名执行任意程序」的通用入口（这是刻意的设计，不应改变）。

**后果**：`check_data_gaps` 会报出三个新核对项的 `source_missing`（因为两张新表没数据），
但模型无法调用任何 Tool 去补——闭环「check → 定向补 → 复核」在这两项上断在中间。

---

## 三、不需要改的部分（已确认）

这几项已经读过源码确认，**不要重复投入**：

1. **`params.build_argv()` 是通用的**——它运行时调 spring 的 `tools.describe_cli` 自省参数，
   不针对特定程序写死。两个新程序的参数面已能被正确解析。
2. **`_launch(target, values, ...)` 是通用启动器**，不针对特定程序。
3. **`check_data_gaps` 的 JSON 兼容性没问题**。它只读 `payload["status"]` 与
   `payload["core"]["missing_total"]`，其余用 `**payload` 原样透传。spring 侧对
   `warnings.checks[]` 的扩展（每项新增 `status` / `missing_dates`，顶层新增
   `warnings.unchecked`）是纯增量，本服务无需改动即可透传给模型。
4. **`schema.py` 的启动校验会自动覆盖新程序**——它逐个确认 `PROGRAMS` 里每个模块文件
   在 `SPRING_DIR` 下真实存在。加进去之后，若 spring 停在缺少这两个文件的旧分支上，
   服务会在启动时就明确报错，而不是等到第一次调 Tool。

---

## 四、建议改动

### 4.1 `schema.py` — 注册表加两行

```python
PROGRAMS: dict[str, str] = {
    "adjust":          "etl.adjust",
    "import_daily":    "etl.import_daily",
    "fetch_index":     "etl.fetch_index",
    "fill_volratio":   "etl.fill_volratio",
    "update_limit":    "etl.update_limit",
    "fill_shares":     "etl.fill_shares",
    "fill_turnover":   "etl.fill_turnover",
    "sync_suspension": "etl.sync_suspension",   # 新增
    "sync_limit_pool": "etl.sync_limit_pool",   # 新增
}
```

建议同时加一个类似 `FILL_TARGETS` 的常量，声明这两个程序日常一起跑：

```python
# 两份第三方事实数据，日常一起补；彼此无依赖，顺序不重要。
# 与 FILL_TARGETS 不同：那四个有严格先后（fill_turnover 依赖 fill_shares 的流通股本），
# 这两个各自从外部接口取数、不依赖库内任何表（spring pipeline.yaml 里 requires 均为 []）。
MARKET_FLAG_TARGETS: tuple[str, ...] = ("sync_suspension", "sync_limit_pool")
```

### 4.2 `server.py` — 新增一个合并 Tool

建议仿 `etl_fill_indicators` 的形状合并成一个 Tool，而不是开两个：它们总是一起跑，
且都是「补齐当日第三方事实数据」这一件事。

```python
@mcp.tool()
def etl_sync_market_flags(
    begin: str,
    end: str | None = None,
    forcerun: bool = False,
    targets: list[str] | None = None,
    retries: int = 0,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """补齐停牌名单与涨跌停股池两份第三方事实数据（check_daily 的三个核对项依赖它们）。

    targets 默认全做（sync_suspension / sync_limit_pool），两者无依赖、顺序不重要。
    **无前置条件**：都从外部接口取数，不依赖库内任何表，可与日线补数并行安排
    （但 DuckDB 单写者，实际仍串行执行）。

    **必须全量运行**：不要传 codes/exchanges 做部分补数。这两张表是每日全量快照，
    写库按日期整体删除后重插，部分范围运行会让未覆盖的股票从当日快照中消失，
    导致 check_data_gaps 的对应核对项产生整片误报。
    """
```

**注意这个 Tool 刻意不暴露 `codes` / `exchanges` 参数**——理由见 4.4。

实现可直接复用 `etl_fill_indicators` 的循环骨架（`_launch` + 共享 `wait_seconds` 预算 +
失败即中断并返回 `completed` / `failed_target`）。

### 4.3 `server.py` — `check_data_gaps` 的 docstring 补一句

现在的 docstring 只引导模型看 `core.checks[].gap_dates` 和 `missing_codes`。
spring 侧新增的三个核对项在 `warnings.checks[]` 里，模型不会主动去看。建议补充：

```
`warnings.checks[]` 里另有三项第三方交叉核对（停牌核对 / 涨停核对 / 跌停核对）：
  * status 为 source_missing / partial 表示对应的第三方表当日无数据，
    需要先调 etl_sync_market_flags 补齐，missing_dates 给出具体缺哪几天；
  * status 为 mismatch 表示库内数据与第三方源不一致，明细见该项的 csv_path。
告警类不影响顶层 status 与退出码。
```

### 4.4 关于**不暴露 `codes` / `exchanges`** 的理由（重要）

spring 侧这两个 ETL 的写库是「按日期整体删除后重插」（避免每日快照的成员变化留下幽灵行）。
`DELETE` 是无条件全日删除、`INSERT` 只插过滤后剩下的行，所以：

> 昨天全量入库 18 条 → 今天为排查某只股跑 `-c 600519` → 当日 18 条被删、只剩 1 条
> → 核对侧看到「表里有数据」→ 把其余 17 只全报成差异，**一整片误报**

spring 侧已在这两个 ETL 里加了对应的 WARNING，但 MCP 层最好从**参数面上就不给这个口子**，
把 `-c` / `-x` 留给人工排查用命令行。

---

## 五、测试影响

### 5.1 必须重新生成契约快照

`tests/fixtures/describe_cli_snapshot.json` 需要补入两个新程序，否则
`test_contract.py::test_snapshot_covers_exactly_the_registry` 会红：

```python
assert set(SNAPSHOT) == set(schema.PROGRAMS)
```

生成方式参照该仓库既有做法（快照来自 spring 的 `python -m tools.describe_cli --all`）。

### 5.2 `test_contract.py` 的两个常量要分类

两个新程序的 `-b/-e` **默认值是自省当天**（与 `adjust` / `fetch_index` / `import_daily` 同类），
不是 argparse 层的 `null`。所以：

```python
# 自省当天作为默认值 —— 新增两个归这一类
DATE_DEFAULT_PROGRAMS = ("adjust", "fetch_index", "import_daily",
                         "sync_suspension", "sync_limit_pool")

# begin/end 在 argparse 层默认为 null 的四个补齐型程序 —— 不变
NULL_DEFAULT_PROGRAMS = ("fill_volratio", "update_limit", "fill_shares",
                         "fill_turnover")
```

这一点已用实际输出确认：

```json
{
  "program": "sync_suspension",
  "module": "etl.sync_suspension",
  "description": "A股停牌名单入库工具 (支持指定日期区间)",
  "arguments": {
    "begin": { "flags": ["-b", "--begin"], "default": "20260913", ... },
    ...
  }
}
```

`default` 是具体日期而非 `null`，会随自省日期变化（`conftest.py` 的
`TODAY_SENTINEL` / `normalize_defaults` 就是为这个准备的）。

### 5.3 两个程序的参数面

两者一致，均有 `-b/-e/-c/-x/-s/-f`（spring 侧契约测试强制的公共参数面），
`sync_limit_pool` 另有 `--only {up,down,all}`。`-s/--source` 的 choices 目前只有
`akstock`，default `akstock`。

### 5.4 文档第一节的「明确不做」需要更新

`docs/mcp_etl_plan.md` 里写着：

> 写入面收敛到上述 7 个既有 ETL 程序（`fill_turnover` 于 2026-09-12 补入……）

接入后变成 9 个，该处措辞需同步。

---

## 六、spring 侧需要知道的前置条件

实施前请确认 spring 那边：

1. **已跑过 `python -m etl.init_db`** 建出两张新表（`CREATE TABLE IF NOT EXISTS`，幂等，
   不影响既有表）。没建表的话，`check_daily` 会因查不到表而退出码 2。
2. spring 当前在 `dev` 分支，这次重构**尚未合入 master**。若本服务的 `SPRING_DIR`
   指向的工作区停在 master，`schema.py` 的启动校验会报「模块文件不存在」。

---

## 七、只读侧（quant-mcp）：无需改动

顺带确认过 `quant-mcp`：它的 `list_tables` 是从 `information_schema.tables` 实时枚举的，
不是白名单，所以两张新表**自动可见、可查询**。`src/quant_mcp/schema.py` 的 `TABLES`
只是契约测试要遍历的对象清单，不拦访问。

可选：把两张表加进 `TABLES` 以纳入契约测试覆盖——属于「契约硬化」范畴，不影响使用。

---

## 八、改动规模小结

| 文件 | 改动 |
|---|---|
| `schema.py` | `PROGRAMS` 加 2 行；建议新增 `MARKET_FLAG_TARGETS` |
| `server.py` | 新增 1 个 Tool；`check_data_gaps` docstring 补一段 |
| `tests/fixtures/describe_cli_snapshot.json` | 重新生成 |
| `tests/test_contract.py` | `DATE_DEFAULT_PROGRAMS` 加 2 项 |
| `docs/mcp_etl_plan.md` | 「7 个既有 ETL 程序」→ 9 个 |

底层机制（`_launch` / `params.build_argv` / 启动校验 / JSON 透传）全部无需改动。
