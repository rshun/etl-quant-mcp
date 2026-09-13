# etl-quant-mcp 开发说明与进度

> **文档位置说明**：本文件已迁入 `etl-quant-mcp/docs/` 并纳入版本管理（2026-08-19，M5）。
> spring 侧的 `.gitignore` 忽略 `docs/`，故 spring 只在 README 中保留一段指向本仓库的说明。

| 项 | 值 |
|---|---|
| 新仓库 | `etl-quant-mcp` |
| MCP 服务名 | `quant-etl`（与只读侧 `quant-readonly` 对称） |
| 依赖的数据管道仓库 | `spring`（`/Users/shun/Projects/spring`） |
| 创建日期 | 2026-08-18 |
| 当前状态 | **已交付并部署上线**（S1–S3 / M1–M6）。生产环境 Debian，systemd 常驻。剩余事项见第十五节 |

---

## 一、背景与目标

### 真实问题（已澄清）

夜间 ETL 依赖外部数据源，**故障形态不是崩溃，而是卡死**——程序停在下载处，既不报错也不退出。当前只能人工发现、人工 kill、人工重跑，且重跑时同样可能再次卡住。

这决定了本项目的第一优先级不是「能远程触发 ETL」，而是**「能判断它是在跑还是卡了，并能从卡死点续上」**。

### 目标

MCP 服务端 `quant-etl`，让 Claude 完成闭环：**查日志 → 判定卡死/缺口 → 定向补数 → 复核**。

### 范围

| 需求 | 对应 spring ETL 程序 |
|------|---------------------|
| 1. 指定日期/区间下载复权因子 | `etl.adjust` |
| 2. 指定日期/区间下载股票交易明细 | `etl.import_daily` |
| 3. 指定日期/区间下载指数交易明细 | `etl.fetch_index` |
| 4. 补齐量比 / 涨跌停 / 股本 / 换手率 | `etl.fill_volratio` / `etl.update_limit` / `etl.fill_shares` / `etl.fill_turnover` |
| 5. 查看 `log/stockdailyYYYYMMDD.log` | 新增日志工具 |
| **6. 卡死检测与中断续跑**（澄清后新增，实际最高优先级） | 心跳监控 + 分片 + 缺口检测 |

**明确不做**：任意 SQL 写入、任意命令执行、删表/清库类工具。写入面收敛到上述 7 个既有 ETL 程序
（`fill_turnover` 于 2026-09-12 补入，spring 早在 2026-09-10 已把它纳入契约）。

---

## 二、现状盘点（2026-08-18 实测）

以下均为读代码确认的事实，是设计依据。

### 2.1 影响成败判定

| # | 结论 | 证据 | 影响 |
|---|------|------|------|
| 1 | **6 个 ETL 失败时退出码恒为 0** | `main()` 内均为 `logger.error(...)` + `return`；全项目仅 `tools/check_daily.py:796` 有 `sys.exit(main())` | 致命，必须先修（契约 C1） |
| 2 | **卡死时根本没有退出码** | 进程不退出 | 退出码契约在卡死场景失效，必须补心跳（契约 C1b） |

### 2.2 影响卡死检测

| # | 结论 | 证据 | 影响 |
|---|------|------|------|
| 3 | 批量采集**有超时保护** | `datasource/bstock.py:294 / 386 / 533` 在 `bs.login()` 前调 `socket.setdefaulttimeout(30)` | 这几条路径不会无限阻塞 |
| 4 | ~~另有 3 处 login 无超时保护~~ **判断已推翻（2026-08-19 复核）** | 三处 `bs.login()`（`:36 / 84 / 127`）前确无就近的 `setdefaulttimeout`，但 `socket.setdefaulttimeout()` 是**进程级全局**设置：`:36` 是 `relogin()`，其 7 个调用点（`:329/339/349/436/446/566/576`）全在三个批量函数**内部**，30s 超时早已生效；`:84`(`fetch_sync_calendar`) 与 `:127`(`fetch_stock_info`) 分别只被 `etl/trade_cal.py:79`、`etl/sync_basic.py:72` 调用，**不在 MCP 的 6 个程序范围内** | 对范围内 6 个程序，bstock 网络路径已全部被 30s 超时覆盖，此项**不是根因**。真正裸奔的是 `trade_cal` / `sync_basic`——若它们也在夜跑 cron 里，才是候选（待用户确认 crontab）。另一类超时管不住的卡死：**连接缓慢滴水时每次 recv 都刷新计时器，可事实上永久挂住**，只能靠外部心跳发现（即 C1b + 第五节） |
| 5 | 已有进度日志，**共 3 处**（原文档只记了 1 处） | `:361` `fetch_batch_data`（供 `import_daily`）、`:458` `fetch_adjust_factors`（供 `adjust`）、`:588` `fetch_batch_index`（供 `fetch_index`），均为 `if processed % 100 == 0: logger.info(f"   已处理: {processed}/{total}")` | 现成心跳源，但只按条数、不按时间。**C1b 必须改 3 处**——只改 `:361` 的话，`adjust` 和 `fetch_index` 拿不到 30 秒心跳，而 `adjust` 恰恰是持写锁下载、最不该误判的那个 |
| 6 | 进度日志间隔不稳定 | 每 100 只；单日全市场约 10–50 秒一行，长历史区间可达数分钟一行 | 停顿阈值不能写死，需按任务类型分档 |

### 2.3 影响 kill 安全性

| # | 结论 | 证据 | 影响 |
|---|------|------|------|
| 7 | `import_daily` 下载期间**不持写锁** | `main()` 中先 `module.fetch_batch_data()`，之后才 `dbutil.get_connection(is_read_only=False)` | 下载阶段被 kill 完全无害 |
| 8 | `adjust` 下载期间**持有写锁** | `main()` 中 `get_connection(is_read_only=False)` 在 `fetch_adjust_factors()` **之前** | kill 会中断持锁进程；优先 SIGTERM；spring 侧可延后取连接 |
| 8b | **`fetch_index` 同样持写锁**（2026-08-19 新增实测） | `etl/fetch_index.py:99` 的 `get_connection(is_read_only=False)` 在 `:107` 的 `fetch_batch_index()` **之前**——结构同 `adjust`，非同 `import_daily` | 三个下载型程序里**只有 `import_daily` 下载期间不持锁**；`fetch_index` 的 kill 策略与 S3 改造须与 `adjust` 同档 |
| 9 | ETL 导入为幂等 upsert | README「跨机器搬运」章节 | 重跑安全，支撑断点续跑 |

### 2.4 影响工程结构

| # | 结论 | 证据 | 影响 |
|---|------|------|------|
| 10 | ETL 模块顶层无副作用 | `etl/*.py` 顶层只有 `logger = logging.getLogger(...)` | 自省探针可安全 import |
| 11 | 日志实际路径为**项目根 `log/`** | `util/myutil.py:35` 用 `Path(__file__).parents[1]/"log"`；docstring 却写 `~/log/` | docstring 笔误，spring 侧顺手修 |
| 12 | 日志格式单点固定 | `util/myutil.py:30`：`%(asctime)s [%(name)s] [%(levelname)s] %(message)s`，`datefmt=%H:%M:%S` | 解析可靠 |
| 13 | spring **不使用 `__init__.py`** | `.gitignore` 忽略 `__init__.py`，`git ls-files` 计数为 0（隐式命名空间包） | 新仓库自行决定，但跨仓调用不受影响 |
| 14 | spring 的 `.mcp*` 被 gitignore | `.gitignore:6` | 配置示例只能写进 README |

### 2.5 部署与运行环境（2026-08-19 用户确认）

| # | 结论 | 影响 |
|---|------|------|
| 15 | **生产环境是 Debian Linux**，crontab 与 MCP 都部署在同一台 Debian 上；macOS 只做开发测试 | **ADR-2 的本地 subprocess 模型成立**，无需改远程调用 |
| 16 | Debian 上 `lday` / `tdx` 数据源不可用（依赖 Windows 通达信目录） | 生产上只有 `bstock` 一条路，MCP 参数校验应按平台拒绝另两个 |
| 17 | 夜跑脚本只调 **3 个**程序：`etl.adjust` → `etl.import_daily` → `etl.fetch_index`，经 `quant.sh -m <module>` 包装调用 | `trade_cal` / `sync_basic` **不在夜跑路径**，S3 的超时项因此取消。⚠️ 注意措辞：用户确认的是「不需要加进夜跑」，**不等于永不运行**——两者仍是所有 ETL 的数据前置，见 12 节风险表 |
| 18 | **夜跑脚本不检查退出码**，三条命令顺序执行，无 `set -e`、无 `\|\| exit` | S1 产出的成败信号目前**无人消费**；MCP 上线前夜跑仍是静默失败。修脚本是几行的事，建议尽早做 |
| 19 | `quant.sh` **已审阅：退出码透传正确** | `python3 -m "$PY_MODULE" "$@"` 之后紧跟 `EXIT_CODE=$?`，末尾 `exit $EXIT_CODE`，中间的 `deactivate` 不覆盖退出码。S1 的信号在这一层完好 |
| 20 | Debian 上 venv 在 `/home/rshun/src/venv_stock/`，spring 根目录由夜跑脚本的 `$ROOT_PATH` 决定 | MCP 的 `SPRING_PYTHON` 直接指向该 venv 的 `python3`，**不经过 `quant.sh`** |
| 21 | `quant.sh` 靠 `export PYTHONPATH="$CURRENT_DIR"` 让 `-m` 找到 `etl` 包 | MCP 不需要它——`python -m` 会自动把 CWD 放进 `sys.path[0]`，只要 `cwd=SPRING_DIR` 即可（Mac 上的冒烟已验证） |

---

## 三、架构决策（ADR）

### ADR-1：独立仓库 `etl-quant-mcp`，不放进 spring 【已修订】

- **初版决策**：放在 spring 内，理由是「必须 import `etl.*` / `util.*`」。
- **修订原因**：确定采用纯 subprocess 方案（ADR-2）后，这条理由不成立——唯一残留的 import 需求是 argparse 自省，已由 ADR-4 消除。
- **最终决策**：独立仓库 `etl-quant-mcp`。
- **收益**：spring 不引入 `mcp` 依赖；服务端可独立迭代；与 `quant-mcp` 的剥离方向一致（commit `bb6d214`）。
- **代价**：多配 `SPRING_DIR` / `SPRING_PYTHON`；契约测试需拆两份（见第七节）。

### ADR-2：subprocess 调用，不 in-process 调 `main()`

调用边界就是一行，等价于手敲命令：

```python
subprocess.Popen(
    [SPRING_PYTHON, "-u", "-m", "etl.import_daily", "-b", "20260817", "-e", "20260817"],
    cwd=SPRING_DIR, env={**os.environ, "PYTHONUNBUFFERED": "1"},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
)
```

**理由**：

1. ETL 的 `main()` 无参、内部读 `sys.argv`，进程内调用需篡改全局状态；
2. `configure_etl_logging()` 有 `_configured` 幂等标志，进程内无法按任务隔离日志；
3. **卡死可 kill**——进程内调用会让整个 MCP server 一起挂住，这在本项目是决定性理由；
4. DuckDB 写锁随子进程退出自动释放。

**代价**：每次启动多 1–2 秒 import 时间（akshare/baostock 加载慢），对分钟级任务可忽略。

### ADR-2b：传输方式与调用方式是两条独立的轴 【2026-08-21 新增】

**这一条是对 ADR-2 的澄清，起因是一次真实的设计盲区。**

ADR-2 决定的是「**怎么调 ETL**」——子进程而非进程内。文档此前默认了 stdio 传输，
并把它和 ADR-2 当成同一个决定。**两者其实互不相关**：

| 轴 | 选项 | 由谁决定 |
|---|---|---|
| ETL 调用方式 | 子进程 / 进程内 | ADR-2（子进程） |
| 客户端↔服务端传输 | stdio / streamable-http / sse | **本条（可配置，默认 stdio）** |

HTTP + 子进程完全不违背 ADR-2：ETL 仍是子进程、仍可 kill、写锁仍随子进程退出释放，
四条理由一条不受影响。

**为什么这件事要紧**：stdio 模式下服务端由客户端拉起，两者必然同用户。而 ETL 必须以
数据管道属主的身份运行——`config.yaml` 的 `~/data/quant.db` 按运行用户展开 `~`。
于是 stdio 把「服务端与 ETL 同身份」这个**真实约束**，传导成了「客户端也必须是管道属主」
这个**并不必要的约束**。

生产环境正好撞上：Claude 客户端跑在 `claude` 用户下（还挂着 Telegram、5 条 cron、
另外两个 MCP 服务），ETL 属于 `rshun`，且 `claude` 对 `/home/rshun` 无任何权限。
当时列出的三个方案——迁用户、搬目录、放宽权限——**全都在动同一根轴**（谁跑、文件在哪、
权限多大），因为传输方式被当成了不可动的前提。

改用 HTTP 后：服务端以 `rshun` 常驻，客户端连 `127.0.0.1` 即可，**文件系统权限一行不改**，
两边现有环境都不用动。而且隔离性反而更好——客户端能做的事恰好等于本服务暴露的 Tool，
拿不到 shell、拿不到任意 SQL、也读不到 `quant.db`。

> **教训**：给出多个方案时，先检查它们是不是都在动同一根轴；
> 写下「这做不到」之前，先把前提列出来。当时我恰恰写了「权限隔离在这里拿不到」，
> 而那个结论完全建立在一个没被检验的前提上。

**配置**：`ETL_MCP_TRANSPORT`（默认 `stdio`）/ `ETL_MCP_HOST`（默认 `127.0.0.1`）/
`ETL_MCP_PORT`（默认 `8787`）。本服务**不做鉴权**——「谁能连上这个端口」就是它唯一的
授权边界，因此默认只绑回环，绑到非回环地址会在 stderr 告警。
常驻部署见 `deploy/quant-etl-mcp.service`。

### ADR-3：异步任务模型 + 停顿检测

全市场 `import_daily -b 20000101` 需数十分钟，MCP 客户端单次调用必超时。工具返回 `job_id`，另配任务查询工具。支持 `wait_seconds`（默认 30）内联等待，短任务直接出结果。

**状态机必须区分「跑着」和「卡着」**，详见第五节。

### ADR-4：自省通过子进程 + JSON，不通过 import 【已修订】

spring 侧新增 `tools/describe_cli.py`（约 30 行），把 argparse 定义导出为 JSON：

```bash
python -m tools.describe_cli import_daily     # 单程序
python -m tools.describe_cli --all            # 全部
python -m tools.describe_cli --list           # 只列程序名
```

实际输出结构（比初版设计多一层程序元信息，MCP 需要 `description` 来生成 Tool 文档）：

```json
{
  "program": "import_daily",
  "module": "etl.import_daily",
  "description": "A股历史行情数据入库工具 (支持多源、多代码、指定日期)",
  "arguments": {
    "source": {
      "flags": ["-s", "--source"], "type": "str", "action": "store",
      "nargs": null, "default": "bstock", "choices": ["lday", "bstock", "tdx"],
      "required": false, "help": "指定数据源类型: ..."
    }
  }
}
```

MCP 侧调用它拿参数 schema，**参数增删改自动跟随，MCP 零改动**。这个工具对 spring 自身也有用（`--all` 可直接生成 README 参数表）。

**两个默认值陷阱，MCP 侧必须处理**：

1. `adjust` / `import_daily` / `fetch_index` 的 `-b`/`-e` 默认值是 **`build_parser()` 调用当天**，因此自省输出**逐日变化**。M5 的契约快照测试必须先把日期类默认值归一化，否则测试每天都会红。
2. `fill_volratio` / `update_limit` / `fill_shares` / `fill_turnover` 的 `-b`/`-e` 在 argparse 层默认值是 `null`，**真实默认（T-1／今天）是 `parse_arguments()` 里 parse 之后才套用的**，自省看不到。该语义由 `help` 文本承载（契约 C4），MCP 必须把 `help` 原样透传给模型。

### ADR-5：MCP 对 ETL 内部逻辑「零知识」

MCP 只需知道三件事：**怎么启动**（注册表 + 自省）、**是否健康**（退出码 + 心跳）、**结果在哪看**（日志 + DB 事实）。不内建任何「某程序应该写哪张表」的假设。ETL 内部加多少逻辑都与 MCP 无关。

### ADR-6：单写者串行队列

DuckDB 单写者。任何时刻只允许一个 ETL 子进程，新任务排队而非并发抢锁。

### ADR-7：跨仓边界 = 子进程 + JSON + 文件

spring 对外只暴露三个稳定出口，MCP 只认这三个：

| 出口 | 用途 | 稳定性由谁保证 |
|------|------|--------------|
| `python -m etl.<prog> <args>` + 退出码 | 执行 | spring 的 `test_cli_contract.py` |
| `python -m tools.describe_cli <prog>` | 自省参数 | 同上 |
| `log/stockdailyYYYYMMDD.log` | 观测 | `util/myutil.py` 单点定义 |

MCP 侧环境变量（`.mcp.json` 注入，仿 quant-mcp 写法）：

```json
{
  "mcpServers": {
    "quant-etl": {
      "command": "${ETL_MCP_PYTHON}",
      "args": ["${ETL_MCP_DIR}/server.py"],
      "env": {
        "SPRING_DIR": "<spring 项目根目录>",
        "SPRING_PYTHON": "<venv 解释器绝对路径>",
        "STALL_TIMEOUT_DEFAULT": "180",
        "MAX_RUNTIME_DEFAULT": "7200",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

`SPRING_LOG_DIR` 默认取 `$SPRING_DIR/log`，可覆盖。

两套环境的实际取值：

| | `SPRING_DIR` | `SPRING_PYTHON` |
|---|---|---|
| **生产 (Debian)** | 夜跑脚本的 `$ROOT_PATH` | `/home/rshun/src/venv_stock/bin/python3` |
| **开发 (macOS)** | `/Users/shun/Projects/spring` | `/Users/shun/Projects/.venv_quant/bin/python` |

**不经过 `quant.sh`**：它的职责是激活 venv + 设 `PYTHONPATH`，而 MCP 直接指定 venv 解释器、并以 `cwd=SPRING_DIR` 启动子进程，这两件事都自动满足（`python -m` 会把 CWD 放进 `sys.path[0]`）。多包一层 shell 只会多一层退出码传递风险。

---

## 四、ETL 侧契约

这五条是「ETL 变化时 MCP 不失效」的前提，由 spring 侧保证。

### C1 退出码 = 成败信号 【S1 已落地，2026-08-19】

```python
def main() -> int:
    ...
    except Exception as e:
        logger.error(f"执行过程中发生未预期的错误: {e}")
        return 1
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
```

约定（**2026-08-19 修订，原 `2 = 部分成功` 与 argparse 冲突**）：

| 退出码 | 含义 | 产出方 |
|---|---|---|
| `0` | 成功 | `main()` |
| `1` | 失败 | `main()` |
| `2` | **argparse 用法错误**（非法枚举值、未知选项等） | argparse 内建，改不掉 |
| `3` | 部分成功（如 5400 只中 3 只失败） | 尚未实现，见下 |

> **为什么改**：argparse 遇到非法参数会 `sys.exit(2)`，这是 Python 标准库写死的行为。实测 `python -m etl.import_daily -s nosuch` → `exit=2`。若沿用原约定，MCP 会把「参数传错了」读成「部分成功」——一个**静默的错误结论**，正是 S1 要消灭的那类问题。由于 `2 = 部分成功` 当时尚未被任何程序产出，改起来零成本。
>
> **实现现状**：7 个程序已全部返回 `0`/`1`（`fill_turnover` 于 2026-09-10 由 spring 补入契约），`2` 由 argparse 自动产出。**`3` 尚未有任何程序产出**——`fetch_batch_data` 不回报每只股票的成败，做不出可信判定。MCP 侧应把 `3` 当作「协议已预留、暂不出现」处理。

立此契约后，ETL 内部新增任何逻辑分支，只要遵守「失败返回非 0」，MCP 自动判对成败。

### C1b 心跳 = 存活信号 【当前部分满足，必做】

**退出码解决不了卡死**——进程不退出就没有退出码。补充契约：

> **长耗时 ETL 必须周期性向 stdout 输出进度，间隔按「条数或时间」双条件触发。**

`datasource/bstock.py` 的 **3 处**进度日志（`:361` / `:458` / `:588`，分别服务 `import_daily` / `adjust` / `fetch_index`）现为纯条数触发（每 100 只），均改为：

```python
now = time.monotonic()
if processed % 100 == 0 or now - last_progress_at >= 30:
    logger.info(f"   已处理: {processed}/{total}")
    last_progress_at = now
```

有了 30 秒稳定心跳，MCP 的停顿阈值才能从「按任务分档的 180–600 秒」收紧到统一 90 秒，卡死发现速度提升数倍。

### C2 结果判定基于 DB 事实

`check_data_gaps` 沿用 `tools/check_daily` 的做法：用 `STOCK_INFO` + `TRADE_CAL` 算预期记录数，反查实际落库数。ETL 逻辑改了 → 落库事实变了 → 检查结果自动跟着变。

### C3 依赖顺序声明在 spring 侧

新增 `config/pipeline.yaml`（初版落在 `etl/pipeline.yaml`，spring 于 2026-09-10 前后移入 `config/`），
改 ETL 的人顺手维护，MCP 读它做拓扑排序：

```yaml
import_daily:  {requires: []}
fetch_index:   {requires: []}
adjust:        {requires: [import_daily, sync_capital]}
fill_volratio: {requires: [import_daily]}
update_limit:  {requires: [import_daily]}
fill_shares:   {requires: [import_daily, sync_capital]}
fill_turnover: {requires: [import_daily, fill_shares]}
```

> `adjust` 的 `requires` 于 2026-09-10 由空变为 `[import_daily, sync_capital]`：默认源改为 `local`
> 之后，复权因子由 `CAPITAL_DETAIL` 除权事件 + `STOCK_DAILY` 收盘价本地自算，不再是纯下载。

### C4 新逻辑的语义写进 argparse `help=`

`help` 文本经 `describe_cli` 原样透传给模型，是语义传递的唯一免费通道。新增 `--xxx` 模式时，把「什么时候该用」写进 help，而不是只写进 README。

---

## 五、卡死检测（本项目核心）

### 5.1 状态机

```text
queued → running → ┬→ succeeded      exit 0
                   ├→ failed         exit 1 或 exit 2(argparse 用法错误)
                   ├→ partial        exit 3（尚未实现）
                   ├→ stalled        进程活着，但 idle_seconds > stall_timeout
                   ├→ killed_stalled 超 max_runtime 后被自动终止
                   └→ cancelled      人工取消
```

**`stalled` 是可查询状态，不是终态**——先报警不动手，由人或模型决定继续等还是杀。只有超过 `max_runtime` 硬上限才自动 terminate → 5s → kill。

### 5.2 实现

reader 线程逐行读子进程 stdout，每行打时间戳：

```python
for line in proc.stdout:                 # 行迭代，配合 -u 实现行级实时
    job.last_output_at = time.monotonic()
    job.last_line = line.rstrip()
    job.progress = _parse_progress(line)  # 从「已处理: 3400/5400」抽出
    out_file.write(line)
```

`get_job` 返回字段：

| 字段 | 含义 |
|---|---|
| `status` | 上述状态机 |
| `idle_seconds` | 距上次输出多久 |
| `last_line` | 最后一行日志，通常是 `已处理: 3400/5400` |
| `progress` | 结构化进度 `{done: 3400, total: 5400}` |
| `elapsed_seconds` | 已运行时长 |
| `queue_position` | 排队位置（`queued` 时） |

### 5.3 必须注意的坑：缓冲

子进程 stdout 重定向到 pipe 时是**块缓冲**（4–8KB），日志会攒着不吐，导致正常运行被误判为卡死。**必须双保险**：

- 命令行加 `-u`
- 环境变量 `PYTHONUNBUFFERED=1`

**这一条不做，整个停顿检测就是错的。** 需在 `test_mcp_runner.py` 中用一个「慢速输出」的假子进程做回归测试。

### 5.4 阈值分档

| 任务类型 | `stall_timeout` 默认 | 说明 |
|---|---|---|
| 单日/短区间（≤ 7 天） | 180s | C1b 落地后可收紧到 90s |
| 长区间（> 7 天） | 600s | 每 100 只的间隔本身就长 |
| `max_runtime` 硬上限 | 7200s | 可按 job 覆盖 |

三者均可由工具参数覆盖。

### 5.5 kill 安全性

| 程序 | 下载期间持写锁？ | kill 策略 |
|---|---|---|
| `import_daily` | 否 | 下载阶段可放心 kill |
| `fetch_index` | 否（S3 已修正） | 下载阶段可放心 kill |
| `adjust` | 否（S3 已修正） | 下载阶段可放心 kill |
| `fill_*` | 纯 SQL，无网络，基本不会卡 | 一般无需 kill |

### 5.6 与分片的配合

`chunk` 分片在卡死场景价值最大：按月切 12 段，单段卡死只损失一段；杀掉后 `last_line` 指出处理到第几只，配合 `codes` 精确重跑。**卡死从「整晚白跑」降级为「丢一段」。**

---

## 六、Tool 清单

### 6.1 ETL 执行类

| Tool | 底层 | ETL 参数 |
|------|------|---------|
| `etl_adjust` | `etl.adjust` | begin, end, codes, exchanges(sh/sz/bj/all), source(local/bstock) |
| `etl_import_daily` | `etl.import_daily` | + source(lday/bstock/tdx), print_only |
| `etl_fetch_index` | `etl.fetch_index` | + source(lday/bstock) |
| `etl_fill_indicators` | fill_volratio / update_limit / fill_shares / fill_turnover | begin, end, codes, exchanges, forcerun, overwrite(仅 fill_turnover), **targets**（多选，按序串行） |

需求 4 的四项合并为一个工具：日常一起补，拆开只会让模型多轮调用。前三项参数完全一致（`-b -e -c -x -f`）；
`fill_turnover` 于 2026-09-12 补入（当初列为「可选再挂」），它多一个 `-o/--overwrite`，
合并 Tool 的 `overwrite` 只透传给它——带给别的 target 会被 `build_argv` 拒绝。
执行顺序 `fill_volratio → update_limit → fill_shares → fill_turnover`：换手率由日线成交量除以
`DAILY_BASIC.float_shares` 算出，而流通股本正是 `fill_shares` 回填的，颠倒顺序会算出一片空值。

**MCP 层附加参数**（非 ETL 参数）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `wait_seconds` | 30 | 此时间内跑完直接返回摘要，否则转后台返回 `job_id` |
| `chunk` | `none` | `none`/`month`/`year`，长区间分片，失败段可单独重跑 |
| `retries` | 0 | 单段失败自动重试次数 |
| `stall_timeout` | 见 5.4 | 静默多久判为 `stalled` |
| `max_runtime` | 7200 | 硬上限，超时自动终止 |

### 6.2 任务管理类

| Tool | 作用 |
|------|------|
| `list_jobs(status?, limit)` | 列出任务，`stalled` 优先置顶 |
| `get_job(job_id)` | 状态、退出码、耗时、`idle_seconds`、`progress`、失败分片、排队位置 |
| `get_job_output(job_id, tail=200, only_errors=false)` | 子进程输出尾部 |
| `cancel_job(job_id, force=false)` | SIGTERM →（force 时）5s 后 SIGKILL |

任务元数据落 `$SPRING_LOG_DIR/mcp_jobs/<job_id>.json`，输出落 `<job_id>.out`，**MCP server 重启后历史仍可查**。

### 6.3 日志类（需求 5）

| Tool | 作用 |
|------|------|
| `list_etl_logs(limit=30)` | 列出 `log/stockdaily*.log`（日期、大小、修改时间） |
| `read_etl_log(date?, tail=200, level?, module?, keyword?, max_bytes=200000)` | 反向读尾部，按 LEVEL / 模块 / 关键字过滤 |
| `summarize_etl_log(date?)` | ERROR/WARNING 计数、按模块分组、错误摘要、**最后一条进度行与其时间戳**（判断当晚是否卡死） |

### 6.4 自省与校验类

| Tool | 作用 |
|------|------|
| `describe_etl_program(name)` | 转发 `tools.describe_cli`，实时返回当前参数（永不过期） |
| `check_data_gaps(begin, end, exchanges?, codes?, include_index?)` | 转发 `tools.check_daily`，返回缺失日期/股票清单 |

**补数闭环**：`summarize_etl_log` → `check_data_gaps` → 用 `codes` + 精确日期定向补 → 再 check。

---

## 七、仓库分工与防漂移

契约测试拆两份，**两边都不需要对方在场**：

| 放哪 | 测什么 | 举例 |
|---|---|---|
| spring `tests/unit/test_cli_contract.py` | 「我的 CLI 接口是稳定的」 | 7 个程序都有 `-b -e -c -x`；失败退出码为 1；`describe_cli` 输出结构合法 |
| etl-quant-mcp `tests/test_contract.py` | 「我按契约调用」 | 用固化的 JSON 快照做 fixture，测 argv 构造与退出码解读 |

spring 改 CLI → spring 测试红；MCP 用错参数 → MCP 测试红。只有端到端时才需凑齐（标 `integration`，靠 `SPRING_DIR` 环境变量）。

---

## 八、目录结构

### spring 侧新增

```text
spring/
├── tools/describe_cli.py         # 新增：argparse → JSON 自省出口
├── config/pipeline.yaml          # 新增：程序依赖声明（C3，原 etl/pipeline.yaml）
└── tests/unit/test_cli_contract.py   # 新增：CLI 契约自测
```

### etl-quant-mcp 仓库

```text
etl-quant-mcp/
├── server.py            # FastMCP 入口，由注册表动态注册 Tool
├── runner.py            # 子进程调度 + 心跳监控 + 串行队列 + 状态持久化
├── params.py            # 自省结果缓存 + argv 构造 + 白名单校验
├── logs.py              # 日志读取/过滤/摘要
├── schema.py            # 程序注册表、枚举、常量、服务名
├── pyproject.toml       # 仿 quant-mcp
├── requirements.txt     # mcp==1.25.0（不需要 duckdb/pandas，一切走子进程）
├── .mcp.json            # 配置示例
├── README.md
├── docs/mcp_etl_plan.md # 本文件迁入
└── tests/
    ├── conftest.py
    ├── test_params.py    # argv 构造 + 白名单，正/反例
    ├── test_runner.py    # mock 子进程：正常/失败/卡死/缓冲，正/反例
    ├── test_logs.py      # 日志解析/过滤，正/反例
    ├── test_contract.py  # 契约快照
    └── test_integration.py  # 需 SPRING_DIR，标 integration
```

**注意**：`etl-quant-mcp` 不需要 duckdb / pandas / akshare —— 所有重活都在 spring 的解释器里跑。依赖只有 `mcp`，这是分离方案的一个额外收益。

---

## 九、分阶段任务与进度

状态图例：`[ ]` 未开始 `[~]` 进行中 `[x]` 已完成 `[!]` 阻塞

### S1 spring 契约化 【前置，独立于 MCP 也值得做】

分支：`feature/etl-contract`

- [x] `etl/adjust.py` main() 返回状态码 + `sys.exit(main())`
- [x] `etl/import_daily.py` 同上
- [x] `etl/fetch_index.py` 同上
- [x] `etl/fill_volratio.py` 同上
- [x] `etl/update_limit.py` 同上
- [x] `etl/fill_shares.py` 同上
- [x] 各文件顶部补「修改记录」注释块
- [x] 测试：正例——正常执行返回 0（`tests/unit/test_etl_exit_codes.py`）
- [x] 测试：反例——mock 数据源抛异常，断言返回 1（同上）
- [x] **pytest 跑通**——`tests/unit/test_etl_exit_codes.py` 37 passed；全量 `pytest -m "not integration"` 350 passed / 4 skipped，无回归
- [x] **端到端冒烟**：非法日期 / `begin > end` → `exit=1` 且日志有明确错误行；`-c 600519 -p` 干跑 → `exit=0`（真实取到 baostock 数据）

**S1 采用的退出码语义**（部分成功码本轮不产出，见下）：

| 分支 | 退出码 |
|---|---|
| 参数校验失败 / 候选代码为空 / 数据源缺方法 / `ImportError` / 通用 `Exception` | `1` |
| `print_only` 干跑完成 / 正常完成 | `0` |

> **部分成功码本轮未实现**：`fetch_batch_data` 目前不回报每只股票的成败，做不出可信的部分成功判定。要落地需先改数据源返回值，属 S2 之后的范围。另注意 `2` 已被 argparse 占用（见契约 C1 的修订说明），部分成功码为 `3`。

> 收益：当前 cron 夜跑失败是静默的，这一步让失败可被发现，不依赖 MCP。

### S2 spring 自省与心跳

分支：`feature/etl-introspect`

- [x] 6 个 ETL 拆出 `build_parser()`，`parse_arguments()` 改为 `return build_parser().parse_args()`
      （三个 `fill_*` 的日期默认值后处理保留在 `parse_arguments()` 内）
- [x] 新增 `tools/describe_cli.py`（单程序 + `--all` + `--list`，自身也遵守 C1 退出码）
- [x] `datasource/bstock.py` 进度日志改「条数或时间」双条件（C1b）——**3 处全改**：`fetch_batch_data` / `fetch_adjust_factors` / `fetch_batch_index`
- [x] 心跳间隔改为可配：`config/config.yaml` 新增 `baostock.progress_heartbeat_seconds: 30`
- [x] 新增 `etl/pipeline.yaml`（C3）
- [x] 新增 `tests/unit/test_cli_contract.py`（55 项）
- [x] 各文件顶部补「修改记录」注释块
- [x] **验证**：全量 `pytest -m "not integration"` → 405 passed / 4 skipped，无回归
- [x] **变异测试**：把心跳退回纯条数触发 → 2 项红；漏掉计时器重置 → 1 项红。测试确认有效，非空跑

### S3 spring 健壮性【已完成，范围较原计划收窄】

分支：`feature/etl-timeout`

> 2026-08-19 复核推翻了「无超时 login 是卡死根因」（见 2.2 结论 4）；用户随后确认 `trade_cal` / `sync_basic` 无需定期运行（结论 17）。**超时相关的两项因此取消**——它们唯一的受益者不运行。本阶段只保留写锁时机这一项，而它因结论 8b 反而比原计划更重要。

- [x] ~~`datasource/bstock.py:84 / 127` 补 `socket.setdefaulttimeout(30)`~~ **取消**：唯一调用者 `trade_cal` / `sync_basic` 不定期运行；`:36` 的 `relogin()` 本就受全局超时覆盖
- [x] ~~确认 crontab~~ **已确认**：夜跑只有 adjust / import_daily / fetch_index
- [x] `etl/adjust.py` 的 `get_connection()` 延后到 `fetch_adjust_factors()` 之后，下载期间不持写锁
- [x] **`etl/fetch_index.py` 同上**
- [x] `util/myutil.py` 修正 `configure_etl_logging` docstring 的 `~/log/` 笔误
- [x] 复核 akshare / tdx 路径：`web.py:99`、`tdx_offline.py:72` 的 `requests.get` 均已带 `timeout`，无裸奔点
- [x] 新增 `tests/unit/test_etl_write_lock_timing.py`（9 项，含调用顺序与源码位置双重断言）
- [x] **验证**：全量 414 passed / 4 skipped；变异测试——把写连接挪回下载前 → 3 项红

> **成果**：三个下载型 ETL 现已统一为「先下载、后取写连接」。下载阶段（唯一会卡死的阶段）不再持有任何写锁，卡死时可放心 kill，5.5 的 kill 策略表随之简化。

### M1 MCP 骨架

仓库：`etl-quant-mcp`

- [x] 仓库初始化（pyproject / requirements / LICENSE / .gitignore）
- [x] `schema.py`：程序注册表 `PROGRAMS`、服务名、状态机、退出码契约、环境变量出口
- [x] `params.py`：调用 `describe_cli` 取 schema + 缓存 + argv 构造 + 白名单校验
- [x] `runner.py`：子进程执行（`-u` + `PYTHONUNBUFFERED`）、reader 线程心跳、串行队列、状态持久化、取消
- [x] `tests/test_params.py`（38 项正反例）
- [x] `tests/test_runner.py`（31 项）：**含卡死模拟与缓冲回归**
- [x] **验证**：69 passed；对真实 spring 跑通自省 → argv 构造 → 子进程执行 → 退出码解读全链路

**M1 实测的端到端结果**：

| 场景 | 结果 |
|---|---|
| 干跑 `-c 600519 -p` | `succeeded` / exit 0 / 捕获 20 行输出 / 1.0s |
| 2027 日期（日历无记录） | `failed` / exit 1 / `last_line` 为 S3 改进后的可执行提示 |

**变异测试（确认关键用例非空跑）**：

| 注入的缺陷 | 结果 |
|---|---|
| 去掉子进程的 `PYTHONUNBUFFERED` | `test_slow_but_steady_output_stays_running` 红——持续输出的任务真的被误判为 `stalled`，复现了 5.3 警告的场景 |
| 把 `exit 2` 映射成 `partial` | `test_exit_2_is_failed_not_partial` 红 |

**M1 期间做的两处设计决定**：

1. `schema.py` 的环境变量一律做成**函数**而非模块级常量——`import schema` 不应因环境未配置而失败，否则单元测试和 `--help` 之类的路径都会被拖死。改由 `validate_environment()` 在服务启动时统一校验（满足文档第十节「`SPRING_DIR` 不存在 → 启动即明确报错」）。
2. `Runner` 的 `cwd` 是**显式构造参数**，不从 `jobs_dir` 推断。初版把两者耦合当测试开关，是错的。

### M2 Tool 接入

- [x] `server.py`：FastMCP 入口，4 个 ETL 执行类 Tool
- [x] 4 个任务管理 Tool（含 `stalled` 呈现）
- [x] `describe_etl_program`
- [x] `.mcp.json.example`（`.gitignore` 的 `.mcp*` 需加 `!.mcp.json.example` 例外，否则示例也进不了库）
- [x] `README.md`（本属 M5，但 `pyproject.toml` 的 `readme` 字段指向它，缺了会导致打包失败，故提前建）
- [x] `tests/test_server.py`（28 项正反例）
- [x] 冒烟：单日单只股票跑通
- [x] **验证**：97 passed（M1 的 69 + M2 的 28）

**共注册 9 个 Tool**：

| 类别 | Tool |
|---|---|
| 执行 | `etl_import_daily` / `etl_adjust` / `etl_fetch_index` / `etl_fill_indicators` |
| 任务管理 | `list_jobs` / `get_job` / `get_job_output` / `cancel_job` |
| 自省 | `describe_etl_program` |

**M2 冒烟实测**（对真实 spring）：

| 步骤 | 结果 |
|---|---|
| `describe_etl_program('import_daily')` | 返回 6 个参数的实时定义 |
| `etl_import_daily(begin=20260817, codes=['600519'], print_only=True)` | `succeeded` / exit 0 / 0.8s |
| `get_job` | `line_count=20`, `finished=True` |
| `get_job_output(tail=3)` | 正确返回输出尾部 |
| 真实失败（2027 日期） | `failed` / `needs_attention=True` / `last_line` 为 S3 的可执行提示 |
| 参数注入（`600519; DROP TABLE`） | 提交前即被拦下，返回结构化错误，**未起子进程** |

#### M2 期间的两个决定

**1. 「动态注册」改为显式 Tool 签名（偏离原计划）**

原计划写「由 `PROGRAMS` 动态注册」。实做改为显式声明 4 个 Tool 的参数签名，理由是
FastMCP 依函数签名生成 JSON Schema——显式签名让模型看得到 `begin`/`end`/`codes`/`exchanges`
的真实类型与默认值，远好过一个泛化的 `params: dict`。

ADR-4「参数增删改自动跟随」的收益并未丢失，只是落点从**注册层**移到了**校验层**：
argv 仍由 `params.py` 依据运行时自省结果构造与校验，spring 改了枚举或删了参数，
这里会立刻报错而不是默默拼出一个错误的命令行；模型要看当前真实参数就调 `describe_etl_program`。

**2. 修了 `params.build_argv` 的一个校验顺序缺陷**

原实现在解析 `SPRING_PYTHON` **之后**才校验代码与枚举，导致参数写错时模型收到的是
「环境变量 SPRING_PYTHON 未设置」，会被引向完全错误的排查方向。改为**先校验完所有参数，
再解析环境**。`test_injection_in_codes_rejected` 刻意不配置该环境变量，正是为了钉住这个顺序。

> 这个缺陷是写 Tool 层测试时才暴露的——M1 的单元测试全都显式传了 `python=` 参数，
> 恰好绕过了这条路径。

### M3 日志能力（需求 5）

- [x] `logs.py`：反向读尾部 + 正则过滤
- [x] `list_etl_logs` / `read_etl_log` / `summarize_etl_log`
- [x] `tests/test_logs.py`（29 项）+ `tests/test_server.py` 补 6 项 Tool 层用例
- [x] **验证**：132 passed；对真实日志跑通（265 行、9 个模块、0 未解析行）

**卡死判据（M3 的核心设计）**

`summarize_etl_log` 的 `stall_suspects` 用一条**与查看时间无关**的判据：

> **某模块的最后一行如果是进度行，就是卡死嫌疑。**
> 正常跑完会有「批量采集完成」，正常失败会有 ERROR。两者皆无而止于
> 「已处理: 300/5400」，正是「停在下载处」的形态。

时间无关很重要——隔几天复盘同样成立，不像「距今多久没更新」那种判据一放就失效。

变异测试确认它两个方向都有牙：

| 注入的缺陷 | 结果 |
|---|---|
| 永不上报卡死（漏判） | 2 项红 |
| 所有模块都上报（误判） | 3 项红——其中两项专门钉住「正常跑完」与「正常失败」不得误报 |

**M3 期间处理的两个日志格式事实**

1. **时间戳只有 `HH:MM:SS`，日期在文件名里**。跨零点的运行（23:50 启动、00:10 结束）
   会把两段写进同一个文件且时间戳回绕，因此所有间隔计算都按「负数即跨天」补 24 小时。
   有专门的边界用例钉住。
2. **一天一个文件，当天所有 ETL 程序共用**。所以摘要必须按模块（logger 名）分组，
   否则不同程序的日志糊成一团。实测当天日志里有 9 个模块。

> `summarize_etl_log` 与 `get_job` 的心跳**互补而非重复**：心跳只看得见本服务
> 启动的任务，cron 昨晚跑的那一次只能靠日志复盘。

### M4 断点续跑闭环（需求 6）

- [x] `chunk` 分片执行 + 失败区间收集（`params.split_range` 按自然月/年切）
- [x] `retries` 重试（上限 5，仅对 `failed` / `killed_stalled` 生效）
- [x] `check_data_gaps` 转发（消费 S4 补的 `check_daily --json`）
- [x] 端到端演练（见下）
- [x] **验证**：174 passed（M3 的 132 + M4 的 42），连跑 3 次稳定

**分片设计**：切分对齐**自然月/年边界**而非固定天数——补数时人和模型都按「某年某月」
思考，段边界与之一致才好定位与重跑。有不变式测试钉住「分片必须无缝无重叠地覆盖原区间」，
漏一天就是漏数据。超过 `MAX_SPAN_DAYS` 的区间不分片仍然拒绝，分片后逐段校验即可放行。

**重试范围**：只重试 `failed` 与 `killed_stalled`。不含 `cancelled`——那是人的决定，
自动重跑会推翻它；不含 `partial`——重跑整段是浪费，应按缺口定向补。

#### M4 揪出的一个竞态（重要）

写重试时踩到的：任务失败后若还要重试，原实现会**先把终态落定、放开锁、落盘，
再重新加锁重置**。这中间有个约 1ms 的窗口，`get_job` 或 `wait` 在那一刻看到的是
`failed`——模型据此认为任务已结束，会去做「定向重跑」，而执行器自己正要重跑，
于是同一段跑了两遍。

修法是把**判定与状态落定放进同一把锁一次做完**：要重试的任务状态直接从「跑完」
跳到 `queued`，中间不经过终态，上次结果留在 `attempt_history`。

> 这个 bug 是全量跑测试时偶发红才暴露的（单跑该文件always绿）。
> 更值得记的是**第一版回归测试是无效的**：它靠轮询采样，而真实窗口只有落盘那一瞬，
> 轮询几乎必然错过——变异测试连试 3 次都抓不到。改成挂钩 `_persist` 采样后才变成
> 确定性守卫（变异 3/3 红）。轮询式的并发测试很容易是「看起来测了」。

#### 端到端演练（真实 ETL + 真实网络）

无法真的拔网线，改用 **`SIGSTOP` 冻结子进程**——从外部看，被冻结的进程与挂死的进程
完全一致：活着、不退出、不产出任何输出。

| 步骤 | 实测结果 |
|---|---|
| 启动整个沪市的 `import_daily`（干跑不写库） | 正常下载，progress 到 `100/2310` |
| `SIGSTOP` 冻结 | `proc.poll()` 为 None，进程存活 |
| 停顿检测 | **7.9s 判为 `stalled`**（阈值 8s），`idle_seconds=8.3`，`needs_attention=True` |
| 进度是否保留 | 是，`progress` 停在 `100/2310`，`last_line` 为该进度行 |
| `cancel_job(force=True)` | 进程终止，`returncode=-15`，状态 `cancelled` |
| 定向重跑单只 | `succeeded` / exit 0 / 0.4s |

> 一处与预期不符，如实记录：预期被 `SIGSTOP` 的进程只有 `SIGKILL` 才终止得掉，
> 实测 `SIGTERM`（-15）就够了。结果达成，但机制与预判不同，未深究 macOS 的信号语义
> ——**生产在 Debian，那边行为可能不同，真要较真需在 Debian 上复测**。
>
> 本演练证明的是「进程无输出 → 判定 → 终止 → 定向重跑」这条链路可用；
> 它**没有**证明真实断网时 baostock 的具体行为（比如缓慢滴水导致 recv 超时永不触发）。
> 那需要在 Debian 上做真实的断网演练。

### M5 交付

- [x] `tests/test_contract.py` 契约快照（75 项）+ `tests/fixtures/describe_cli_snapshot.json`
- [x] `tests/test_integration.py`（16 项，标 `integration`，环境缺失自动 skip）
- [x] README（配置、13 个 Tool、补数闭环、退出码契约、与 quant-mcp 的关系）
- [x] spring README 增补：核心特性一条 + 「写入侧：ETL 调度」小节（含三个稳定出口表）
- [x] 本文件迁入并纳入版本管理（`.gitignore` 里那条 `docs/` 是从 spring 抄来的，已去掉）
- [x] **验证**：254 passed（不含 integration）+ 16 passed（对真实 spring）

#### 契约快照的两个坑

1. **日期默认值必须归一化。** 三个下载型程序的 `begin`/`end` 默认值是「自省当天」，
   原样入快照的话，契约测试**每天都会红**。快照里统一替换为 `<TODAY>` 哨兵，
   比对前对实时结果施加同样的归一化（`conftest.normalize_defaults`）。
   有专门的用例钉住「快照里不得留下真实日期」。

2. **生成快照不能加 `sort_keys`。** 第一版加了，结果参数顺序被打乱成字典序
   （`begin/codes/end/...`），而 `build_argv` 依声明顺序拼接 argv——
   argv 随之变形，6 个 `test_argv_common_params` 全红。
   参数的声明顺序是契约的一部分，快照必须忠实保留。已加
   `test_snapshot_preserves_declaration_order` 钉住这一点。

#### 测试分工最终形态

| 文件 | 项数 | 需要 spring | 测什么 |
|---|---|---|---|
| `test_params.py` | 54 | 否 | argv 构造、参数闸门、分片切分 |
| `test_runner.py` | 42 | 否 | 子进程执行、心跳、卡死、重试、串行队列 |
| `test_logs.py` | 29 | 否 | 日志解析、过滤、摘要、卡死判据 |
| `test_server.py` | 49 | 否 | Tool 注册、编排、错误呈现 |
| `test_contract.py` | 75 | 否 | 「我按契约调用」——全程用固化快照 |
| `test_integration.py` | 16 | **是** | 契约漂移、真实子进程、真实日志 |

集成测试里只放**必须凑齐两个仓库才能验证**的事，纯逻辑一律不放进去。

### M6 部署上线【2026-08-22 完成】

- [x] `deploy/quant-etl-mcp.service`：systemd 常驻单元
- [x] 生产部署到 Debian，以数据管道属主 `rshun` 运行；客户端对其文件系统零访问
- [x] 传输层 `streamable-http`，监听 `127.0.0.1:8787`
- [x] **修复 `Runner.list` 遮蔽内置 `list`**（`76a30e2`）——生产 Python 3.11.2 上同类注解 `-> list[dict]` 解析到该方法并抛 `TypeError`，模块 import 失败、服务起不来
- [x] 新增 AST 静态守卫 `test_no_builtin_shadowed_in_annotations`
- [x] **修复终止过程中对外呈现终态**（`7aed503`）——SIGTERM → 宽限期 → SIGKILL 最长数秒，其间 `get_job` 显示「已结束」，会诱使调用方与执行器的重试撞车
- [x] 记录实测通过的 Python 版本：生产 3.11.2 / 开发 3.14.3
- [x] venv 约定改为项目内 `.venv`
- [x] 用户确认部署成功（2026-08-22）
- [ ] 夜跑脚本 `fetchData()` 仍不检查退出码（见 15.2）——MCP 侧已能自判，但 cron 链路仍是静默失败

> **本阶段最大的教训：开发机与生产机的 Python 版本差异会掩盖真实缺陷。**
> 3.14 的 PEP 649 延迟求值让 `Runner.list` 的遮蔽问题在开发机上完全不可见，
> 测试全绿、服务却在生产起不来。这类问题**在新版本上跑测试永远发现不了**，
> 只能靠静态检查——这正是新增 AST 守卫而非普通用例的原因。

---

## 十、测试策略

遵循 spring 的 CLAUDE.md 三层规范，**每项新开发均需正、反例**。

### 反例清单（必须覆盖）

**参数层**

- 非法日期 `2026-13-01` / `20261301` → 拒绝
- 非法代码 `600519; DROP TABLE` → 拒绝
- 非法 `source` / `exchanges` 枚举 → 拒绝
- `begin > end` → 拒绝
- 跨度超 `MAX_SPAN_DAYS` 且未指定 `chunk` → 拒绝

**执行层**

- 子进程返回 1 → `failed`，**不得报成功**
- **子进程返回 2（argparse 用法错误）→ `failed`，绝不得读成 `partial`**
- 子进程返回 3 → `partial`（该码目前无程序产出，测试用假子进程构造）
- **子进程长时间无输出 → `stalled`，且不得误报成功或失败**
- **子进程输出很慢但持续 → 必须保持 `running`，不得误判卡死**（缓冲回归）
- 子进程超 `max_runtime` → `killed_stalled`，且进程确实已终止
- SIGTERM 无效的子进程 → force 后 SIGKILL 生效
- `SPRING_DIR` 不存在 / `SPRING_PYTHON` 不可执行 → 启动即明确报错

**日志层**

- 日志文件不存在 → 明确报错，不抛裸异常
- 日志行格式不匹配 → 返回 `None`，不崩
- 超大日志 → 受 `max_bytes` 截断并标记 `truncated`

---

## 十一、安全约束（红线相关）

- 一律 `subprocess.Popen([...], shell=False)`，**绝不拼 shell 字符串**
- 日期强制 `^\d{8}$`；代码强制 `^\d{6}(\.(SH|SZ|BJ))?$`；枚举白名单
- 只暴露 `schema.PROGRAMS` 中写死的程序，**不提供任意命令执行**
- `SPRING_PYTHON` / `SPRING_DIR` 只从环境变量读，不接受 Tool 参数注入
- 跨度上限 `MAX_SPAN_DAYS`（默认 3650），超限要求显式 `chunk`
- 日志返回有 `max_bytes` 上限，防止灌爆上下文
- 新仓库依赖仅 `mcp==1.25.0`；spring 侧不新增任何依赖

---

## 十二、风险与注意

| 风险 | 说明 | 缓解 |
|------|------|------|
| **卡死误判** | pipe 块缓冲导致正常任务被判 `stalled` | 强制 `-u` + `PYTHONUNBUFFERED`；专项回归测试 |
| **卡死漏判** | 长区间任务进度日志间隔天然很长 | 阈值分档；推动 C1b 落地后统一收紧 |
| **DuckDB 写锁冲突** | `quant-readonly` 与写入服务同连 `~/data/quant.db` | 启动前探测并给明确提示；引导用 `config.yaml` 的 `db_active: test` |
| **kill `adjust` 时持锁** | 下载期间就持有写连接 | 优先 SIGTERM；推动 S3 延后取连接 |
| **跨仓版本漂移** | spring 改 CLI，MCP 不知情 | 契约测试拆两份（第七节） |
| `fill_shares` 前置依赖 | 需 `CAPITAL_DETAIL` 有数据，否则新股跳过 | `pipeline.yaml` 声明；`sync_capital` 无日期参数，**暂不纳入 MCP** |
| **TRADE_CAL 耗尽（定时炸弹）** | 开发库的日历止于 **2026-12-31**，`2027-01-01` 起无记录。`dbutil.check_is_trading_day` 查不到即返回 `False`，而三个夜跑程序的校验器都含 `v_single_day_must_be_trading_day`，夜跑又恰是 `BEGIN=END=今天` → **2027-01-01 起每晚三个程序全部失败**（生产库需另行确认） | 年底节假日公布后跑 `python -m etl.trade_cal -b 20000101`（`-e` 默认次年末）。S1 之后该故障会以 `exit=1` 暴露，不再静默 |
| TRADE_CAL 缺记录的报错有误导性 | 提示为「为非交易日（休市）」，真实原因「日历表中不存在该日期」只在一条 WARNING 里 | 建议 `check_is_trading_day` 区分「查无记录」与「休市」两种情形（约 3 行改动，**未做，待确认**） |
| STOCK_INFO 不更新 → 新股静默漏采 | README 写明 `sync_basic` 应「每天运行」，但它不在夜跑脚本里。`get_candidate_codes` 从 `STOCK_INFO` 取候选，该表不更新则新上市股票永远不进下载范围，且**不报错** | 需用户确认实际维护方式；不纳入 MCP（无补数语义），但应写进运维说明 |
| 跨平台数据源 | `lday`/`tdx` 依赖 Windows 通达信目录，macOS 只能 `bstock` | 参数校验阶段按平台拒绝 |
| ETL 语义变化 | 参数没变、行为变了 | 无法自动感知，靠 spring 自身测试 + 文档 |
| `.mcp.json` 不入库 | spring 的 `.gitignore` 忽略 `.mcp*` | README 给示例，各机器本地维护 |

---

## 十三、验证方式

```bash
# 1. 分支确认（不得在 master 开发）
git rev-parse --abbrev-ref HEAD
```

```bash
# 2. spring 侧测试（不走网络）
pytest -m "not integration"
```

```bash
# 3. S1 验证：失败时退出码应为非 0
python -m etl.import_daily -b 20261301 ; echo "exit=$?"
```

```bash
# 4. S2 验证：自省出口可用
python -m tools.describe_cli import_daily
```

```bash
# 5. 手动冒烟：干跑，不写库
python -m etl.import_daily -b 20260817 -e 20260817 -c 600519 -p
```

```bash
# 6. MCP 协议层冒烟（stdio）
python server.py
```

**闭环冒烟顺序**（在 Claude 里）：`etl_import_daily`（单日单股）→ `get_job` → `read_etl_log` → `check_data_gaps`。

**卡死演练**：跑一个长区间任务，中途断网，观察 `get_job` 是否在 `stall_timeout` 后转 `stalled` 并给出 `last_line` 进度。

**回滚**：MCP 为独立仓库，删除即可；S1/S2/S3 对 spring 的改动均在独立分支，未合并前不影响 master。

---

## 十五、待确认与后续缺口

### 15.1 ~~`quant.sh` 是否透传退出码~~【已解决，2026-08-19】

已审阅，**写法正确，无需改动**：

```bash
python3 -m "$PY_MODULE" "$@"
EXIT_CODE=$?          # 紧接着取，中间无其他命令
...
if command -v deactivate >/dev/null 2>&1; then deactivate; fi
exit $EXIT_CODE       # 原样返回
```

`deactivate` 在 `EXIT_CODE` 已捕获之后执行，不会覆盖。S1 的信号在这一层完好。

**ADR-7 出口定义随之确定**：MCP 调 `python -m etl.<prog>`，`SPRING_PYTHON` 指向 venv 解释器，**不经过 `quant.sh`**（理由见 ADR-7 的环境取值表）。M1 的阻塞解除。

### 15.2 夜跑脚本不检查退出码【仍未解决】

```bash
fetchData()
{
cd $ROOT_PATH
$SCRIPT_PATH/quant.sh -m etl.adjust -b $BEGIN -e $END
$SCRIPT_PATH/quant.sh -m etl.import_daily  -b $BEGIN -e $END
$SCRIPT_PATH/quant.sh -m etl.fetch_index  -b $BEGIN -e $END
}
```

三条命令均未检查退出码，也无 `set -e`。

**信号链的断点在这里，不在 `quant.sh`**：ETL 返回 1 → `quant.sh` 正确透传 1 → `fetchData()` 丢弃。
前两条命令的成败被完全吞掉，`fetchData()` 的返回值只反映最后一条。

不阻塞 MCP（MCP 会自己判退出码），但在 MCP 上线前，夜跑仍是静默失败。最小修法：

```bash
fetchData()
{
  cd "$ROOT_PATH" || return 1
  local rc=0
  for m in etl.adjust etl.import_daily etl.fetch_index; do
    if ! "$SCRIPT_PATH/quant.sh" -m "$m" -b "$BEGIN" -e "$END"; then
      echo "[ETL] $m 失败 (exit=$?)" >&2
      rc=1
    fi
  done
  return $rc
}
```

（保持「失败也继续跑完剩余程序」的原有行为，只是把成败记下来并汇总返回。若希望一失败就中止，把 `rc=1` 换成 `return 1`。）

### 15.3 ~~`tools/check_daily` 的两个缺口~~【已解决，2026-08-19】

**缺口 1（无机器可读输出）已补**：新增 `-j/--json`，把结果以 JSON 输出到 stdout。

`_check_table` 原本算出了全部所需结构却只返回一个计数，改为返回：

```json
{"label": "日线数据", "table": "STOCK_DAILY", "missing": 3,
 "gap_dates":    [{"date": "2026-08-17", "expected": 2, "actual": 1, "missing": 1}],
 "missing_codes": [{"date": "2026-08-17", "code": "000001.SZ", "name": "..."}],
 "csv_path": "/path/to/check_stockdaily_missing_....csv"}
```

`gap_dates` 始终完整（每项只是几个数字，跨十年也紧凑，且它正是「哪几天要补」的答案）；
`missing_codes` 受 `--json-max-detail`（默认 200）约束并标记 `missing_codes_truncated`，
完整明细始终以 CSV 落盘。全市场缺一天就是 5000+ 条，不设限会灌爆调用方上下文。

**一个必须做对的细节**：`configure_etl_logging()` 默认把日志打到 **stdout**，
不处理的话 JSON 会和日志混在一起、根本无法解析。因此给它加了 `console_stream` 参数，
`--json` 时传 `sys.stderr`。变异测试已确认：去掉这一步，stdout 立刻变成
`JSONDecodeError: Extra data`。

**缺口 2（退出码语义不同）的处置：不改退出码，改用 JSON 的 `status` 字段。**

`check_daily` 的 `0=完整 / 1=有缺失 / 2=检查出错` 有其自身合理性（`1` 是「查到了缺失」
这个**发现**，不是「失败」），强行对齐 ETL 契约反而失真。因此：

> **MCP 侧一律以 JSON 的 `status` 为准（`complete` / `gaps_found` / `error`），
> 不解读 `check_daily` 的退出码。** 这样也顺带绕开了它的 `2` 与 argparse `2` 撞车的问题。

参数校验失败与运行时异常两条路径也都会吐 JSON，调用方不会拿到空 stdout。

### 15.3b 顺带修复：`check_daily` 的日志从未进过 ETL 日志文件

做上面这件事时发现的既有缺陷：`tools/check_daily.py` 的 logger 名是 `tools.check_daily`，
**不在 `etl` 之下**，因此 `configure_etl_logging()` 配的 FileHandler 完全管不到它——
它的日志从来没进过 `stockdailyYYYYMMDD.log`（实测该文件里 `[tools.` 出现 0 次）。
同目录的 `export_etl_tables` / `import_etl_tables` 本就用 `etl.tools.*`，只有它是例外。

已按同目录约定改为 `etl.tools.check_daily`。这直接影响 MCP 的「查日志 → 判定」闭环：
改之前 `summarize_etl_log` 根本看不见完整性检查的结论。

### 15.4 四个 `fill_*` 的调度方式未知

夜跑脚本里没有 `fill_volratio` / `update_limit` / `fill_shares` / `fill_turnover`。
它们是在同一脚本的其他函数、另一条 crontab，还是根本不自动跑？这影响 `pipeline.yaml` 依赖声明的准确性。

### 15.5 `adjust` 的数据前置 `sync_capital` 不在 MCP 范围内【2026-09-12 新增】

spring 于 2026-09-10 把 `adjust` 的默认源改为 `local`——复权因子由 `CAPITAL_DETAIL` 的除权事件
加 `STOCK_DAILY` 收盘价本地自算，`requires` 随之变为 `[import_daily, sync_capital]`。

而 `sync_capital` 因为**没有日期参数**，当初被判定为「暂不纳入 MCP」（见第九节风险表）。
后果是：`CAPITAL_DETAIL` 落后时 `etl_adjust` 会失败或漏算，**而模型没有任何 Tool 能自己补上**，
只能提示人工去 spring 侧跑一次。

同一时间 `adjust` 还加了运行前预检：`ADJ_FACTOR` 有漏跑 / 上市日起未稠密化 / 区间内部空洞时，
以退出码 1 退出并在日志里给出该用哪个 `-b` 回填。这条已写进 `etl_adjust` 的 Tool 说明，
模型应先用 `get_job_output` 读出那条命令，而不是盲目重跑。

待定：是否给 `sync_capital` 开一个无日期参数的 Tool，或在 `etl_adjust` 失败时自动附带提示。

### 15.6 `sync_suspension` / `sync_limit_pool` 未纳入白名单【2026-09-13 新增】

spring 新增两张第三方事实表（`SUSPENSION_DAILY` / `LIMIT_POOL_DAILY`）与对应的两个 ETL，
并已完成 `describe_cli` / `config/pipeline.yaml` / `test_cli_contract.py` 三处契约注册
——**自省链路对本服务已经可用**，`describe_cli --list` 现在返回 9 个程序。

同时 `check_daily` 新增三个核对项（停牌 / 涨停 / 跌停一致性），结果落在 `--json` 的
`warnings.checks[]`。

缺口：`schema.PROGRAMS` 是安全白名单，两个新程序不在其中即无法启动。于是
`check_data_gaps` 会报出这三项的 `source_missing`，**而模型没有任何 Tool 能补**
——闭环「check → 定向补 → 复核」在这两项上断在中间。

本轮只在 `check_data_gaps` 的 Tool 说明与使用手册里写明「这三项补不了、别试」，
避免模型空转；接入方案见 [`proposal_sync_market_flags.md`](proposal_sync_market_flags.md)，
状态为**待实施**。注意文档不得抢跑：在白名单真正加上之前，USAGE / README / INSTALL
的程序清单与数量口径一律保持 7 个，否则模型会照着文档去调一个必然被拒的 Tool。

---

## 十四、变更记录

| 日期 | 修改人 | 说明 |
|------|--------|------|
| 2026-08-18 | Claude | 初版：需求拆解、架构决策、Tool 清单、分阶段任务 |
| 2026-08-18 | Claude | 补充 ETL 契约 C1–C4 与 P0.1 阶段；实测发现退出码恒为 0 |
| 2026-08-18 | Claude | **重大修订**：改为独立仓库 `etl-quant-mcp`；自省改走子进程 JSON（ADR-4/7）；契约测试拆两份 |
| 2026-08-18 | Claude | **重大修订**：澄清故障形态为「卡死」而非崩溃；新增 C1b 心跳契约、第五节卡死检测、S3 健壮性阶段；任务编号改为 S/M 双仓库体系 |
| 2026-08-19 | Claude | 复核第二节 14 条实测结论：推翻结论 4「无超时 login 是根因」；结论 5 补为 3 处进度日志；新增结论 8b「`fetch_index` 同样持写锁」；修正两处行号与 `SPRING_PYTHON` 路径；S3 重新定位 |
| 2026-08-19 | Claude | **S1 完成并验证**：6 个 ETL 的 `main()` 返回退出码并由 `sys.exit()` 传出；新增 `tests/unit/test_etl_exit_codes.py`（37 项）；全量 350 passed；端到端冒烟正反例均符合预期；明确 `2`(部分成功) 本轮不产出 |
| 2026-08-19 | Claude | **S2 完成并验证**：6 个 ETL 拆出 `build_parser()`；新增 `tools/describe_cli.py`、`etl/pipeline.yaml`、`tests/unit/test_cli_contract.py`（55 项）；C1b 心跳 3 处全改并做可配；全量 405 passed |
| 2026-08-19 | Claude | **契约修正**：argparse 用法错误固定 `exit=2`，与原约定的 `2 = 部分成功` 冲突，部分成功码改为 `3`；同步状态机与反例清单 |
| 2026-08-19 | Claude | **S3 完成**（范围收窄）：`adjust`/`fetch_index` 写连接延后到下载之后；取消两项超时改造（唯一调用者不运行）；修 myutil docstring 笔误；新增 `tests/unit/test_etl_write_lock_timing.py`（9 项）；全量 414 passed |
| 2026-08-19 | Claude | 新增 2.5 部署环境事实（Debian、夜跑只调 3 个程序、脚本不检查退出码）与第十五节待确认事项；`quant.sh` 是否透传退出码列为 M1 阻塞项 |
| 2026-08-19 | Claude | **M1 阻塞解除**：审阅 `quant.sh`，退出码透传正确；确定 MCP 直调 `python -m etl.<prog>` 不经包装脚本；补 ADR-7 两套环境取值表 |
| 2026-08-19 | Claude | 风险表新增 3 条：TRADE_CAL 于 2027-01-01 耗尽将导致夜跑全失败、非交易日报错有误导性、STOCK_INFO 不更新致新股静默漏采 |
| 2026-08-19 | Claude | spring 侧补 `get_trading_day_status`，区分「休市」与「日历无记录」，后者给出可执行提示；`check_is_trading_day` 保留为薄封装 |
| 2026-08-19 | Claude | **M1 完成并验证**：新建 `schema.py`/`params.py`/`runner.py` 与 69 项正反例；对真实 spring 跑通全链路；变异测试确认缓冲回归与退出码映射两条关键用例有效 |
| 2026-08-19 | Claude | **M2 完成并验证**：`server.py` 注册 9 个 Tool，新增 `tests/test_server.py`（28 项）、`README.md`、`.mcp.json.example`；97 passed；冒烟跑通单日单股 |
| 2026-08-19 | Claude | M2 偏离原计划：Tool 改为显式签名而非动态注册（理由见 M2 小节）；修复 `build_argv` 校验顺序缺陷（环境错误会盖过参数错误） |
| 2026-08-19 | Claude | **M3 完成并验证**：新增 `logs.py` 与 3 个日志 Tool（共 12 个 Tool）；`tests/test_logs.py`（29 项）；132 passed；变异测试确认卡死判据漏判/误判两个方向均被覆盖 |
| 2026-08-19 | Claude | spring 侧补 `check_daily --json` 机器可读出口（M4 前置）：`_check_table` 改返回结构化结果，`configure_etl_logging` 支持指定控制台流；新增 19 项测试，全量 438 passed |
| 2026-08-19 | Claude | 修复 `check_daily` logger 命名：原名 `tools.check_daily` 不在 `etl` 之下，日志从未进过 ETL 日志文件，`summarize_etl_log` 因此看不见完整性检查结论 |
| 2026-08-19 | Claude | **M4 完成并验证**：分片执行 + 失败区间收集、失败重试、`check_data_gaps`（共 13 个 Tool）；174 passed；SIGSTOP 冻结演练实测 7.9s 判定卡死 |
| 2026-08-19 | Claude | 修复重试竞态：还要重试的任务曾短暂对外呈现为终态，会诱使模型重复补数；判定与状态落定改为同锁一次完成，回归测试由轮询改为挂钩 `_persist` 才具备确定性 |
| 2026-08-19 | Claude | **M5 完成**：契约快照（75 项）与集成测试（16 项）；README 补齐；spring README 增补写入侧小节；本文件纳入版本管理。254 + 16 passed |
| 2026-08-21 | Claude | **新增 ADR-2b**：支持 streamable-http 传输，使客户端与 ETL 可分属不同用户，无需改动任何文件系统权限。真实客户端连通验证通过（13 个 Tool、经 HTTP 跑完一次 ETL）。277 passed |
| 2026-08-21 | Claude | `validate_environment` 补查 `describe_cli` 与 `check_daily`：spring 检出停在旧分支时，原先要等到第一次调 Tool 才以「No module named」暴露，排查方向会被带偏 |
| 2026-08-21 | Claude | **修复生产起不来**：`Runner.list` 遮蔽内置 `list`，Python < 3.14 上同类注解 `-> list[dict]` 解析到该方法而抛 `TypeError`；改名 `list_jobs` 并新增 AST 静态守卫（此类缺陷在 3.14 上跑测试无法发现） |
| 2026-08-21 | Claude | **修复终止竞态**：`_monitor` 在发起终止前即写终态，SIGTERM→宽限期→SIGKILL 期间对外呈现「已结束」，与待重试状态冲突；改为只记录终止意图，终态由 `_run` 原子块统一落定 |
| 2026-08-22 | Claude | **M6 部署上线**：systemd 常驻（streamable-http，127.0.0.1:8787，以管道属主运行），用户确认部署成功；头部状态改为「已交付并部署上线」；清理 S3 小节两行遗留未勾选重复项 |
| 2026-09-12 | Claude | **跟进 spring 变更**：`fill_turnover` 并入 `etl_fill_indicators`（新增只对它生效的 `overwrite`）；`etl_adjust` 暴露 `densify` 并改写说明（默认源 `bstock`→`local`、`bstock` 已废弃、新增运行前预检）；重新生成契约快照；`pipeline.yaml` 路径更正为 `config/`；新增 15.5。296 passed，跨仓漂移守卫 9 passed |
| 2026-09-13 | Claude | **跟进 spring 变更**：spring 移除 `--densify`，同步撤掉 `etl_adjust` 的 `densify` 形参并重生成快照；`check_data_gaps` 的 Tool 说明与 USAGE 中英补 `warnings` 一节（三个新核对项会报 `source_missing` 且本服务补不了）；日志判据由 `unparsed_lines == 0` 改为「带时间戳的行必须可解析」——多行异常的续行会计入前者，曾把「跑挂了」误报成「日志格式变了」；新增 15.6。296 passed，integration 13 passed |
