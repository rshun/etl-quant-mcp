# 安装手册

**quant-etl** —— 一个 MCP 服务端，让 AI 助手能够驱动并监控
[spring](https://github.com/rshun/spring) 项目的 ETL 数据管道。

本手册假设你对本项目一无所知。从头照做即可完成部署。

> English version: [INSTALL.md](INSTALL.md)

---

## 一、这个软件解决什么问题

`spring` 是一套 A 股数据管道，每晚由 `cron` 自动下载行情数据并写入一个
DuckDB 数据库文件。

它典型的故障形态**不是崩溃，而是卡死**：程序停在下载途中，既不报错也不退出。
因为进程不退出，就没有退出码可供判断——只能靠人发现、人杀掉、人重跑。

`quant-etl` 就是装在 `spring` 旁边的一个小服务，把它以工具的形式暴露给 AI 助手，
使助手能够：

- 读昨晚的日志，判断某次运行是不是卡死了；
- 查出哪些交易日、哪些股票缺数据；
- 只把缺失的那部分定向补回来；
- 盯着正在跑的任务，卡住时把它停掉。

它**不提供任意命令执行，也不提供任意 SQL**。能启动的只有白名单里写死的 7 个 ETL 程序。

---

## 二、各部分如何配合

```
  ┌────────────────┐        ┌──────────────────┐        ┌──────────────┐
  │   AI 助手      │  HTTP  │   quant-etl      │  子进程│  spring 的   │
  │  (MCP 客户端)  │───────▶│   MCP 服务端     │───────▶│  ETL 程序    │
  └────────────────┘        └──────────────────┘        └──────┬───────┘
      任意用户                 以 ETL 数据属主                  │ 写入
                               的身份运行                       ▼
                                                        ┌──────────────┐
                                                        │  quant.db    │
                                                        └──────────────┘
```

支持两种传输方式：

| 传输方式 | 什么时候用 |
|---|---|
| **`stdio`**（默认） | AI 客户端和 ETL 数据属于**同一个**操作系统用户。服务端由客户端自己拉起，不需要常驻进程。 |
| **`streamable-http`** | AI 客户端与 ETL 数据属主是**不同用户**。服务端以数据属主身份常驻，客户端通过本机端口连接，**完全不需要文件系统权限**。 |

拿不准就用 `streamable-http`——两种情况它都适用，本手册后续也按它来写。

---

## 三、为什么"服务以哪个用户运行"至关重要

**其它章节都可以跳，这一节请务必读完。**

`spring` 的配置文件里数据库路径通常写作 `~/data/quant.db`，而这个 `~`
是**按运行该程序的用户**展开的。

也就是说：

| 进程以谁的身份运行 | 实际写入的数据库 |
|---|---|
| `rshun`（数据属主） | `/home/rshun/data/quant.db` ← 真正的那个 |
| 其它任何用户 `X` | `/home/X/data/quant.db` ← **另一个空文件** |

最糟糕的是：**这种情况下不会有任何报错**。DuckDB 见文件不存在就新建一个，
ETL 照常跑完、退出码 0、日志一切正常——数据默默进了错误的文件。
你可能几周后才会发现。

> **铁律：服务必须以 `cron` 跑 ETL 时用的同一个用户运行。**
> 本手册中该用户称作 `rshun`，请替换成你自己的。

这也正是 HTTP 传输存在的理由。你的 AI 客户端可以是完全不同的用户——没关系，
因为客户端只说 HTTP。**只有服务端必须是数据属主。**

---

## 四、前置条件

本节所有命令都**以 ETL 数据属主（`rshun`）的身份**执行。

### 4.1 一份可用的 spring 检出

```bash
ls /home/rshun/src/spring/tools/describe_cli.py
```

文件不存在说明检出停在旧分支上，更新它：

```bash
git -C /home/rshun/src/spring status
git -C /home/rshun/src/spring pull
```

> `config/config.yaml` 是入库文件，但内容与机器相关。
> 如果 `git pull` 在这个文件上产生冲突，**手工解决**，并确认
> `progress_heartbeat_seconds` 这个键还在——缺了它 ETL 会直接崩。

### 4.2 spring 的 Python 解释器

```bash
/home/rshun/src/venv_stock/bin/python3 -V
```

你需要的是**解释器本身的路径**，不是包装用的 shell 脚本。
不知道在哪的话，看 `cron` 是怎么调 ETL 的，它用的包装脚本里会写明虚拟环境位置。

### 4.3 本服务需要 Python 3.10 及以上

```bash
python3 -V
```

已实测通过的版本：**3.11.2**（Debian 12）与 **3.14.3**（macOS）。
代码用到了 PEP 604 的 `X | Y` 类型注解语法，所以下限是 3.10。

如果缺 `venv` 模块，Debian / Ubuntu 需要装一个额外的包：

```bash
sudo apt install python3-venv
```

### 4.4 本仓库

```bash
git clone https://github.com/rshun/etl-quant-mcp.git /home/rshun/src/etl-quant-mcp
```

---

## 五、安装

### 5.1 运行安装脚本

```bash
cd /home/rshun/src/etl-quant-mcp
./install.sh \
    --spring-dir    /home/rshun/src/spring \
    --spring-python /home/rshun/src/venv_stock/bin/python3
```

脚本会做四件事：

1. 逐项校验前置条件，缺任何一项都**明确指出是哪一项**并停下；
2. 在 `.venv` 建虚拟环境并装唯一的那个依赖；
3. **跑真实冒烟**——调服务自带的环境校验，再让 spring 自省一个程序，
   证明两个仓库之间真的能通话；
4. 按你的路径和端口生成 systemd unit，写到
   `deploy/quant-etl-mcp.service.generated`。

它**刻意不执行任何需要 root 的命令**。

只想校验环境、不创建任何东西：

```bash
./install.sh --check --spring-dir ... --spring-python ...
```

### 5.2 常用选项

| 选项 | 默认值 | 说明 |
|---|---|---|
| `--port PORT` | `8787` | 端口被占用时换一个。这个数字没有任何特殊性，只要客户端 URL 对得上即可。 |
| `--host HOST` | `127.0.0.1` | 保持默认。理由见[第十节](#十安全说明)。 |
| `--service-user USER` | 当前用户 | 必须是 ETL 数据属主。 |
| `--service-name NAME` | `quant-etl-mcp` | systemd 服务名。 |
| `--venv DIR` | `<仓库>/.venv` | |
| `--log-dir DIR` | `<spring 目录>/log` | ETL 日志所在目录。 |
| `--transport MODE` | `streamable-http` | 只有客户端与服务端同用户时才用 `stdio`。 |

完整选项见 `./install.sh --help`。

### 5.3 装成系统服务

先看一眼生成的 unit，确认无误再执行脚本打印的那三条命令：

```bash
cat deploy/quant-etl-mcp.service.generated
sudo cp deploy/quant-etl-mcp.service.generated /etc/systemd/system/quant-etl-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now quant-etl-mcp
```

---

## 六、验证服务

### 6.1 服务在跑

```bash
systemctl status quant-etl-mcp --no-pager
```

期望 `active (running)`。

### 6.2 监听地址正确

```bash
ss -ltn | grep -w 8787
```

期望 `127.0.0.1:8787`。

> 如果显示 `0.0.0.0:8787`，**停下来改配置**。那意味着整个网段都能连到它，
> 而它没有任何鉴权。

### 6.3 启动日志正常

```bash
journalctl -u quant-etl-mcp -n 20 --no-pager
```

期望有这两行：

```
[quant-etl] streamable-http 监听 127.0.0.1:8787
INFO:     Uvicorn running on http://127.0.0.1:8787
```

第一行是服务在确认自己的传输方式和绑定地址。

### 6.4 客户端用户连得上

**切换到运行 AI 客户端的那个用户**，执行：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

期望返回 `200`。

顺便确认另一半也成立——客户端用户**依然碰不到数据属主的文件**：

```bash
ls /home/rshun/src/spring
```

期望是「权限不够」。能调工具但访问不了文件系统，这正是这套方案要达到的效果。

---

## 七、接入 AI 客户端

**以客户端用户的身份**执行（它可以和服务用户不同）：

```bash
claude mcp add --transport http quant-etl http://127.0.0.1:8787/mcp
```

各版本参数名可能不同，先用 `claude mcp add --help` 确认。想手工配置的话，
在 MCP 客户端的配置里加：

```json
{
  "mcpServers": {
    "quant-etl": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```

确认注册成功：

```bash
claude mcp list
```

### 7.1 第一个该试的命令

对助手说：

> 用 summarize_etl_log 看一下昨天的 ETL 日志

这个工具只读日志文件，是验证整条链路最安全的方式，同时也是本服务最有价值的能力：
它会报出 `stall_suspects`——**最后一行是进度行的模块**，那正是"下载途中卡死"的特征。

---

## 八、配置项参考

所有配置都通过环境变量，由 systemd unit 设置。要改某一项，编辑
`/etc/systemd/system/quant-etl-mcp.service`，然后：

```bash
sudo systemctl daemon-reload && sudo systemctl restart quant-etl-mcp
```

| 变量 | 必填 | 默认值 | 含义 |
|---|---|---|---|
| `SPRING_DIR` | 是 | — | spring 检出的根目录，用作 ETL 子进程的工作目录。 |
| `SPRING_PYTHON` | 是 | — | spring 虚拟环境的解释器。必须是解释器本身，不能是包装脚本。 |
| `SPRING_LOG_DIR` | 否 | `$SPRING_DIR/log` | ETL 日志所在目录。任务记录存在其下的 `mcp_jobs/`。 |
| `ETL_MCP_TRANSPORT` | 否 | `stdio` | `stdio` / `streamable-http` / `sse`。 |
| `ETL_MCP_HOST` | 否 | `127.0.0.1` | 绑定地址。非回环地址会打印告警。 |
| `ETL_MCP_PORT` | 否 | `8787` | 监听端口。 |
| `MAX_RUNTIME_DEFAULT` | 否 | `7200` | 任务被判为跑飞并强制终止的秒数上限。 |
| `STALL_TIMEOUT_DEFAULT` | 否 | 自动 | 静默多少秒判为 `stalled`。不设的话，服务按日期跨度分档：短区间 90 秒，长区间 600 秒。 |

必填项配错时，**服务会启动失败并明确说明哪里不对**，不会拖到第一次调用工具才暴露。

### 8.1 不要把 `SPRING_PYTHON` 指向包装脚本

项目通常会提供一个 shell 包装脚本，负责激活虚拟环境和设置 `PYTHONPATH`。
本服务不需要它：它直接指定解释器，并自己设置工作目录（`python -m` 会把工作目录
放进 `sys.path[0]`）。多包一层只会多一层"退出码能否正确传出"的风险。

---

## 九、故障排查

下面每条报错都是本软件真实会打印的原文。

一条通用规律：**配置错误一定表现为服务启动失败**并给出具体信息。
只要 `systemctl status` 显示服务在跑，配置就是对的。

### `参数自省出口 'tools.describe_cli' 的文件不存在`

spring 检出里没有这个文件，几乎总是因为分支过旧：

```bash
git -C /home/rshun/src/spring status
git -C /home/rshun/src/spring pull
```

### `环境变量 SPRING_DIR 未设置` / `SPRING_PYTHON 不存在`

systemd unit 缺失，或者 `Environment=` 那几行写错了。检查：

```bash
systemctl cat quant-etl-mcp
```

### `No module named tools.describe_cli`

与第一条同因——spring 检出过旧。新版本的本服务会在启动时就拦下并给出更清楚的提示。

### `Address already in use`

端口被别的进程占了，换一个：

```bash
sudo sed -i 's/ETL_MCP_PORT=8787/ETL_MCP_PORT=18787/' /etc/systemd/system/quant-etl-mcp.service
sudo systemctl daemon-reload && sudo systemctl restart quant-etl-mcp
```

记得同步修改客户端的 URL。

### 服务能起来，但工具返回数据库相关错误

DuckDB 只允许一个写者。如果此刻 `cron` 正在跑夜间 ETL，只读连接也打不开。
等它跑完，或者用 `list_jobs` 工具看看当前有什么在跑。

### 某个任务被报为 `stalled`

**这是功能在正常工作，不是故障。** `stalled` 表示进程还活着但超过阈值没有输出。
它**不是终态**——任务可能自己恢复。你可以继续等，也可以用 `cancel_job` 停掉它，
然后只把缺的那部分重跑。

### 工具出现了，但助手调不动

有些客户端维护着一份"允许调用的工具"白名单。查阅你的客户端文档，
了解如何放行 `quant-etl`。

---

## 十、安全说明

**本服务没有鉴权。** 谁能连上这个端口，谁就能触发写生产数据库的 ETL。

实际存在的保护有这些：

- 默认绑 `127.0.0.1`，只有本机进程能连；
- 只能启动白名单里的 7 个 ETL 程序，**不提供任意命令执行，也不提供任意 SQL**；
- 每个参数在起子进程之前都会校验：日期必须是真实存在的日历日，股票代码必须匹配
  严格的模式，枚举值必须来自 spring 当前的真实定义；
- 子进程一律以参数列表启动，**绝不拼接 shell 命令字符串**。

如果你把它绑到回环以外的地址，就等于把"写生产库"这个能力开放给所有能路由到该地址的人。
服务会打印告警，但不会阻止你。

---

## 十一、升级

```bash
cd /home/rshun/src/etl-quant-mcp
git pull
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart quant-etl-mcp
```

如果这次升级改动了 systemd unit 模板，用同样的参数重新跑一次 `install.sh`，
再把新生成的 unit 复制到位。

---

## 十二、卸载

```bash
sudo systemctl disable --now quant-etl-mcp
sudo rm /etc/systemd/system/quant-etl-mcp.service
sudo systemctl daemon-reload
```

然后删掉客户端配置，不再需要的话把检出目录也删掉。

本软件不修改系统上任何其它东西——它不创建数据库、不创建用户，
除了自己的目录和 `$SPRING_LOG_DIR/mcp_jobs/` 之外不写任何文件。

---

## 十三、工具清单

共暴露 13 个工具。

### 只读类

| 工具 | 作用 |
|---|---|
| `summarize_etl_log` | 按模块汇总某天的日志，含 `stall_suspects`（卡死嫌疑）。 |
| `read_etl_log` | 读日志尾部，可按级别、模块、关键字过滤。 |
| `list_etl_logs` | 有哪几天的日志，各多大、什么时候改的。 |
| `check_data_gaps` | 哪些交易日、哪些股票缺数据。 |
| `describe_etl_program` | 某个 ETL 程序当前真实的命令行参数。 |
| `list_jobs` | 列出所有任务，`stalled` 置顶。 |
| `get_job` | 单个任务的状态、进度、距上次输出多久。 |
| `get_job_output` | 某个任务的输出尾部。 |

### 会改变状态的

| 工具 | 作用 |
|---|---|
| `cancel_job` | 终止一个正在跑的任务。 |
| `etl_import_daily` | 下载股票日线。 |
| `etl_adjust` | 下载复权因子。 |
| `etl_fetch_index` | 下载指数日线。 |
| `etl_fill_indicators` | 补齐衍生指标。 |

四个 `etl_*` 都接受 `print_only` 参数或很小的日期区间，因此可以放心试：
`print_only` 会走完整的下载流程但**不写任何数据**。

任务是**排队串行执行**的，因为 DuckDB 只允许一个写者。

---

## 十四、了解更多

`../README.md` 讲设计思路。`mcp_etl_plan.md` 是完整的开发记录：
需求背景、架构决策（ADR）、与 spring 之间的契约、以及卡死检测的设计推演。
