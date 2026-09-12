# etl-quant-mcp

ETL 调度 MCP 服务（服务名 `quant-etl`）。把 [spring](https://github.com/rshun/spring) 的 ETL 程序以 MCP Tool
暴露给模型，让它能完成闭环：**查日志 → 判定卡死/缺口 → 定向补数 → 复核**。

与只读侧 [quant-mcp](https://github.com/rshun/quant-mcp)（服务名 `quant-readonly`）对称：那边读数据，这边跑管道。
两者共用同一个 DuckDB 库，但本服务是唯一的写入入口。

## 它解决什么问题

夜间 ETL 依赖外部数据源，**故障形态不是崩溃而是卡死**——程序停在下载处，
既不报错也不退出。退出码解决不了这个：进程不退出就没有退出码。

所以本服务的第一能力不是「远程触发 ETL」，而是**判断它在跑还是卡了，并能从卡死点续上**。

## 设计要点

| | |
|---|---|
| **子进程而非进程内调用** | ETL 卡死时必须能 kill 掉它而不拖死本服务 |
| **传输方式可选** | stdio（默认）或 HTTP。HTTP 让客户端与 ETL 分属不同用户成为可能，见下 |
| **心跳判定卡死** | reader 线程逐行读输出并打时间戳，静默超阈值即判 `stalled` |
| **`stalled` 不是终态** | 先报警不动手，由人或模型决定继续等还是杀；只有超 `max_runtime` 才自动终止 |
| **强制无缓冲** | 固定注入 `PYTHONUNBUFFERED=1` 并配合 argv 里的 `-u`。少了它，pipe 的块缓冲会让正常运行被误判为卡死 |
| **串行队列** | DuckDB 单写者，任何时刻只放行一个 ETL 子进程 |
| **参数运行时自省** | argv 依据 spring 的 `tools.describe_cli` 构造与校验，spring 改参数本服务零改动 |
| **分片续跑** | 长区间按自然月/年切段，单段卡死只损失一段 |

## 安装

> 部署到服务器请用 **[`docs/INSTALL.zh-CN.md`](docs/INSTALL.zh-CN.md)**（中文）或 **[`docs/INSTALL.md`](docs/INSTALL.md)**（English）+ `./install.sh`：
> 它会校验前置条件、建 venv、验证跨仓调用、并按你的路径和端口生成 systemd unit。
> 下面是手工安装的说明。

依赖只有 `mcp` —— 重活都在 spring 的解释器里跑，本仓库不需要 duckdb / pandas / akshare。

需要 Python **3.10+**（用到 PEP 604 的 `X | Y` 注解）。实测通过的版本：
**3.11.2**（Debian 12，生产）与 **3.14.3**（macOS，开发）。

> 3.14 起注解才是延迟求值（PEP 649），3.10–3.13 上是**定义时立即求值**。
> 这个差异会让「同作用域内定义了与内置同名的东西、又在注解里用该内置」这类问题
> 在 3.14 上完全不可见，却在旧版本上直接 import 失败。
> `tests/test_contract.py::test_no_builtin_shadowed_in_annotations` 用静态扫描守着这条。

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

## 配置

复制 `.mcp.json.example` 为 `.mcp.json` 并填入实际路径（`.mcp*` 已被 gitignore，各机器本地维护）：

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `SPRING_DIR` | 是 | spring 项目根目录，用作子进程的 cwd |
| `SPRING_PYTHON` | 是 | spring 虚拟环境的解释器。**直接指向 venv 的 python，不要指向包装脚本** |
| `SPRING_LOG_DIR` | 否 | 默认 `$SPRING_DIR/log` |
| `MAX_RUNTIME_DEFAULT` | 否 | 任务硬超时上限，默认 7200 秒 |
| `STALL_TIMEOUT_DEFAULT` | 否 | 留空则按日期跨度自动分档（≤7 天用 90 秒，更长用 600 秒） |

**不要经过 `quant.sh` 之类的包装脚本**：它提供的「激活 venv」与「设 PYTHONPATH」两件事，
本服务通过指定 venv 解释器 + `cwd` 已自动满足（`python -m` 会把 CWD 放进 `sys.path[0]`），
多包一层 shell 只会多一层退出码传递风险。

配置有误时服务**启动即报错**，不会拖到第一次调用 Tool——包括 `SPRING_DIR` 指向的检出
是否真的包含 7 个 ETL 模块、`tools/describe_cli.py` 与 `tools/check_daily.py`
（分支过旧是个真实会踩的坑）。

## 传输方式：stdio 还是 HTTP

默认 **stdio**：服务端由 MCP 客户端拉起，两者同进程树、同用户。单机同用户的场景用这个就够了。

但 stdio 有一个隐含约束：**服务端必然与客户端同用户**。而 ETL 必须以数据管道属主的身份
运行——`config.yaml` 里 `~/data/quant.db` 的 `~` 是按运行用户展开的。所以当 Claude 客户端
与数据管道分属不同用户时（例如 Claude 跑在 `claude` 下、ETL 属于 `rshun`），
stdio 会把这个约束传导成「客户端也必须是管道属主」，只能靠迁移用户或放宽文件权限来化解。

**改用 HTTP 就没有这个问题**：

```
claude 用户  ──HTTP──▶  MCP 服务（rshun 身份常驻）──subprocess──▶  ETL ──▶  quant.db
   ↑                            ↑
只能调 13 个 Tool          文件系统权限完全不用改
```

客户端能做的事**恰好等于**本服务暴露的 Tool——它拿不到 shell、拿不到任意 SQL、
也读不到 `quant.db`。这比放宽文件权限的隔离性更好。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ETL_MCP_TRANSPORT` | `stdio` | `streamable-http` / `sse` |
| `ETL_MCP_HOST` | `127.0.0.1` | **本服务不做鉴权**，绑非回环地址等于把 ETL 写入面开放给整个网段；真要这么做会在 stderr 告警 |
| `ETL_MCP_PORT` | `8787` | |

客户端侧的 `.mcp.json` 相应改成：

```json
{ "mcpServers": { "quant-etl": { "url": "http://127.0.0.1:8787/mcp" } } }
```

常驻部署见 `deploy/quant-etl-mcp.service`（systemd 模板，`User=` 设成管道属主）。

顺带一个好处：常驻之后**任务历史跨客户端会话保留**，重启 Claude 也能用 `list_jobs`
看到今天跑过什么。stdio 模式下服务随客户端进程退出，内存里的任务状态就没了。

## Tool 一览（13 个）

### 执行类

均支持 `wait_seconds` 内联等待（默认 30 秒），超时转后台返回 `job_id`。

| Tool | 底层 |
|---|---|
| `etl_import_daily` | `etl.import_daily` |
| `etl_adjust` | `etl.adjust` |
| `etl_fetch_index` | `etl.fetch_index` |
| `etl_fill_indicators` | `fill_volratio` / `update_limit` / `fill_shares` / `fill_turnover`，按序串行 |

MCP 层附加参数：

| 参数 | 默认 | 作用 |
|---|---|---|
| `wait_seconds` | 30 | 此时间内跑完直接返回结果，否则转后台 |
| `chunk` | `none` | `month` / `year` 分片；超 3650 天的区间必须分片 |
| `retries` | 0 | 单段失败自动重试次数，上限 5 |
| `stall_timeout` | 自动分档 | 静默多久判为 `stalled` |
| `max_runtime` | 7200 | 硬上限，超时自动终止 |

`chunk` 只对三个下载型 Tool 开放——四个 `fill_*` 是纯 SQL、无网络，分片没有意义。

### 任务管理

`list_jobs`（`stalled` 置顶）、`get_job`、`get_job_output`、`cancel_job`

### 日志

`list_etl_logs`、`read_etl_log`、`summarize_etl_log`

`summarize_etl_log` 与 `get_job` 的心跳**互补**：心跳只看得见本服务启动的任务，
cron 昨晚跑的那一次只能靠日志复盘。它的 `stall_suspects` 用一条与查看时间无关的判据——
**某模块的最后一行如果是进度行**，说明它停在下载途中就没了下文（正常跑完有完成标志，
正常失败有 ERROR），因此隔几天复盘同样成立。

### 校验与自省

`check_data_gaps`、`describe_etl_program`

## 补数闭环

```
summarize_etl_log        # 昨晚哪个模块停在进度行了？
      ↓
check_data_gaps          # 到底缺哪几天、哪些股票？
      ↓
etl_import_daily(begin=…, codes=[…])   # 按缺口定向补
      ↓
check_data_gaps          # 复核
```

`check_data_gaps` 是只读的，不进串行队列，因此不会被正在跑的补数任务挡住。
但 DuckDB 单写者——若此刻有写任务持有写锁，只读连接会打不开，届时会明确报错并提示。

## 状态机

```
queued → running → ┬→ succeeded      exit 0
                   ├→ failed         exit 1 或 exit 2
                   ├→ partial        exit 3（协议预留，spring 侧尚未产出）
                   ├→ stalled        进程活着，但静默超过 stall_timeout（非终态，可回到 running）
                   ├→ killed_stalled 超 max_runtime 后被自动终止
                   └→ cancelled      人工取消
```

## 退出码契约

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 失败 |
| `2` | **argparse 用法错误**（Python 标准库写死，不可改） |
| `3` | 部分成功（协议已预留，spring 侧尚未产出） |

`2` 不是「部分成功」——那会把「参数传错了」读成「大体成功」，是个静默的错误结论。

`check_data_gaps` 转发的 `tools.check_daily` 是**另一套**退出码（`0=完整 / 1=有缺失 / 2=检查出错`），
本服务一律以它 JSON 输出里的 `status` 为准，不解读它的退出码。

## 测试

```bash
pytest -m "not integration"      # 296 项，不需要 spring 在场
```

参数 schema 用固化快照，子进程用临时 python 小脚本。

```bash
export SPRING_DIR=/path/to/spring
export SPRING_PYTHON=/path/to/venv/bin/python
pytest -m integration            # 17 项，需要真实 spring
```

集成测试里只放**必须凑齐两个仓库才能验证**的事：契约漂移、真实子进程执行、真实日志解析。

### 契约漂移守卫

`tests/fixtures/describe_cli_snapshot.json` 是 spring CLI 的固化快照。
spring 改了参数而本服务没跟上，`test_live_introspection_matches_snapshot` 会红。

确认变更合理后，重新生成快照：

```bash
python -m tools.describe_cli --all   # 在 spring 目录下运行，再把日期默认值归一化为 <TODAY>
```

**注意两点**：三个下载型程序的 `begin`/`end` 默认值是「自省当天」，逐日变化，
必须归一化；生成时**不要加 `sort_keys`**，参数的声明顺序是契约的一部分
（顶层程序名按字母序排列无妨，要保序的是每个程序内部的参数）。

> Windows 上跑这条要带 `PYTHONUTF8=1`：`subprocess` 的 `text=True` 按系统区域编码
> 解码，GBK 会把 spring 的 UTF-8 中文输出解坏。

## 不做什么

任意 SQL 写入、任意命令执行、删表清库。写入面收敛在 `schema.PROGRAMS` 白名单内的
7 个既有 ETL 程序。所有子进程一律 `subprocess.Popen([...], shell=False)`，绝不拼 shell 字符串。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/INSTALL.zh-CN.md`](docs/INSTALL.zh-CN.md) / [`docs/INSTALL.md`](docs/INSTALL.md) | **安装手册**：前置条件、部署步骤、配置项、故障排查 |
| [`docs/USAGE.zh-CN.md`](docs/USAGE.zh-CN.md) / [`docs/USAGE.md`](docs/USAGE.md) | **使用手册**：13 个工具的逐个说明、典型工作流、常见误区 |
| [`docs/mcp_etl_plan.md`](docs/mcp_etl_plan.md) | **开发记录**：需求背景、架构决策（ADR）、spring 侧契约、卡死检测设计、分阶段任务与变更记录 |
