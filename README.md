# etl-quant-mcp

ETL 调度 MCP 服务（`quant-etl`）。把 [spring](../spring) 的 ETL 程序以 MCP Tool 暴露给模型，
让它能完成闭环：**查日志 → 判定卡死/缺口 → 定向补数 → 复核**。

与只读侧 [`quant-mcp`](../quant-mcp)（服务名 `quant-readonly`）对称：那边读数据，这边跑管道。

## 它解决什么问题

夜间 ETL 依赖外部数据源，故障形态**不是崩溃而是卡死**——程序停在下载处，
既不报错也不退出。退出码解决不了这个：进程不退出就没有退出码。

因此本服务的第一能力不是「远程触发 ETL」，而是**判断它在跑还是卡了，并能从卡死点续上**。

## 设计要点

| | |
|---|---|
| **子进程而非进程内调用** | ETL 卡死时必须能 kill 掉它而不拖死本服务 |
| **心跳判定卡死** | reader 线程逐行读输出并打时间戳，静默超阈值即判 `stalled` |
| **`stalled` 不是终态** | 先报警不动手，由人或模型决定继续等还是杀；只有超 `max_runtime` 才自动终止 |
| **强制无缓冲** | 固定注入 `PYTHONUNBUFFERED=1` 并配合 argv 里的 `-u`。少了它，pipe 的块缓冲会让正常运行被误判为卡死 |
| **串行队列** | DuckDB 单写者，任何时刻只放行一个 ETL 子进程 |
| **参数运行时自省** | argv 依据 spring 的 `tools.describe_cli` 构造与校验，spring 改参数本服务零改动 |

## 安装

依赖只有 `mcp` —— 重活都在 spring 的解释器里跑，本仓库不需要 duckdb / pandas / akshare。

```bash
pip install -r requirements.txt
```

## 配置

复制 `.mcp.json.example` 为 `.mcp.json` 并填入实际路径（`.mcp*` 已被 gitignore，各机器本地维护）：

| 环境变量 | 说明 |
|---|---|
| `SPRING_DIR` | spring 项目根目录（子进程的 cwd） |
| `SPRING_PYTHON` | spring 虚拟环境的解释器。**直接指向 venv 的 python，不要指向包装脚本** |
| `SPRING_LOG_DIR` | 可选，默认 `$SPRING_DIR/log` |
| `MAX_RUNTIME_DEFAULT` | 可选，任务硬超时上限，默认 7200 秒 |
| `STALL_TIMEOUT_DEFAULT` | 可选，留空则按日期跨度自动分档 |

配置有误时服务**启动即报错**，不会拖到第一次调用 Tool。

## Tool 一览

**执行类**（均支持 `wait_seconds` 内联等待，超时转后台返回 `job_id`）

| Tool | 底层 |
|---|---|
| `etl_import_daily` | `etl.import_daily` |
| `etl_adjust` | `etl.adjust` |
| `etl_fetch_index` | `etl.fetch_index` |
| `etl_fill_indicators` | `fill_volratio` / `update_limit` / `fill_shares`，按序串行 |

**任务管理**：`list_jobs`（`stalled` 置顶）、`get_job`、`get_job_output`、`cancel_job`

**自省**：`describe_etl_program`

## 退出码契约

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 失败 |
| `2` | **argparse 用法错误**（标准库写死，不可改） |
| `3` | 部分成功（协议已预留，spring 侧尚未产出） |

`2` 不是「部分成功」——那会把「参数传错了」读成「大体成功」。

## 测试

```bash
pytest -q
```

不需要 spring 在场：参数 schema 用固化夹具，子进程用临时 python 小脚本。
需要真实 spring 的用例标了 `integration`。

## 不做什么

任意 SQL 写入、任意命令执行、删表清库。写入面收敛在 `schema.PROGRAMS` 白名单内的 6 个既有 ETL 程序。
