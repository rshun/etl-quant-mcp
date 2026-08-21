# 修改记录:
#   2026-08-19  Claude  新建：FastMCP 入口，注册 ETL 执行、任务管理与自省 Tool
#   2026-08-19  Claude  新增日志类 Tool：list_etl_logs / read_etl_log / summarize_etl_log
#   2026-08-19  Claude  新增分片执行、失败重试与 check_data_gaps
#   2026-08-19  Claude  支持 streamable-http 传输，使客户端与服务端可分属不同用户
"""quant-etl MCP 服务端。

把 spring 的 ETL 程序以 MCP Tool 的形式暴露出来，让模型能完成闭环：
**查日志 → 判定卡死/缺口 → 定向补数 → 复核**。

写入面收敛在 schema.PROGRAMS 白名单内的 6 个既有 ETL 程序，
不提供任意 SQL 写入、任意命令执行、删表清库类工具。

关于「动态注册」的取舍：文档 M2 原写「由 PROGRAMS 动态注册」，实做改为
**显式声明 4 个 Tool 的参数签名**——FastMCP 依函数签名生成 JSON Schema，
显式签名能让模型看到 begin/end/codes/exchanges 的真实类型与默认值，
比一个泛化的 `params: dict` 好用得多。ADR-4「参数增删改自动跟随」的收益
仍然保留，只是落在**校验层**：argv 由 params.py 依据运行时自省结果构造与校验，
spring 改了枚举或删了参数，这里会立刻报错而不是默默拼出一个错误的命令行。
模型需要看当前真实参数时，调 describe_etl_program。

关于传输方式（2026-08-19 补充，对 ADR-2 的澄清）：
ADR-2 决定的是「**怎么调 ETL**」——子进程而非进程内。这与「**客户端怎么连服务端**」
是两条互不相关的轴，早期把两者混为一谈，默认了 stdio。

stdio 模式下服务端由客户端进程拉起，两者必然同用户；而 ETL 需要以数据管道属主
的身份运行（`~/data/quant.db` 的 `~` 按运行用户展开）。当 Claude 客户端与数据
管道分属不同用户时，stdio 就把这个约束传导成了「客户端也必须是管道属主」。

改用 HTTP 后，服务端可作为常驻进程以管道属主身份运行，客户端只需能连上
loopback 端口——**文件系统权限一点都不用动**，客户端能做的事恰好等于
本文件暴露的这些 Tool。ADR-2 的四条理由不受任何影响：ETL 仍是子进程、
仍可 kill、写锁仍随子进程释放。
"""
import json
import os
import subprocess
import sys
import time
import uuid

import logs as logs_mod
import params
import runner as runner_mod
import schema

try:
    from mcp.server.fastmcp import FastMCP
except Exception as e:  # noqa: BLE001
    raise RuntimeError(
        "无法导入 FastMCP，请确认已安装 MCP Python SDK（见 requirements.txt）。"
    ) from e

mcp = FastMCP(schema.SERVER_NAME)

DEFAULT_WAIT_SECONDS = 30

# ---- 传输方式 ----
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "streamable-http"
TRANSPORT_SSE = "sse"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP, TRANSPORT_SSE)

# 默认只绑回环。本服务的授权边界就是「谁能连上这个端口」——
# 它没有鉴权，绑到非回环地址等于把 ETL 写入面开放给整个网段。
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8787

_LOOPBACK_NAMES = frozenset({"localhost", "::1", "[::1]"})


def is_loopback(host: str) -> bool:
    """判断监听地址是否只对本机可见。"""
    text = (host or "").strip().lower()
    if text in _LOOPBACK_NAMES:
        return True
    return text.startswith("127.")


def transport_settings() -> tuple[str, str, int]:
    """从环境变量解析传输方式与监听地址。

    ETL_MCP_TRANSPORT  stdio(默认) | streamable-http | sse
    ETL_MCP_HOST       默认 127.0.0.1
    ETL_MCP_PORT       默认 8787

    默认仍是 stdio：开发、测试与单机同用户的场景不受任何影响。
    """
    transport = (os.environ.get("ETL_MCP_TRANSPORT") or TRANSPORT_STDIO).strip().lower()
    if transport not in TRANSPORTS:
        raise ValueError(
            f"未知的传输方式 '{transport}'；可选: {', '.join(TRANSPORTS)}"
        )

    host = (os.environ.get("ETL_MCP_HOST") or DEFAULT_HTTP_HOST).strip()

    raw_port = (os.environ.get("ETL_MCP_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_HTTP_PORT
    except ValueError:
        raise ValueError(f"ETL_MCP_PORT 不是合法端口号: '{raw_port}'") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"ETL_MCP_PORT 超出范围: {port}")

    return transport, host, port

_runner: runner_mod.Runner | None = None


def get_runner() -> runner_mod.Runner:
    """懒创建执行器单例——import 本模块不应立刻起线程或读环境。"""
    global _runner
    if _runner is None:
        _runner = runner_mod.Runner()
    return _runner


def reset_runner(instance: runner_mod.Runner | None = None) -> None:
    """供测试注入执行器。"""
    global _runner
    _runner = instance


# ---------------------------------------------------------------- 视图

_STATUS_HINT = {
    schema.STATUS_SUCCEEDED:      "已成功完成",
    schema.STATUS_PARTIAL:        "部分成功——有子项失败，建议查看输出后定向重跑",
    schema.STATUS_FAILED:         "执行失败，请用 get_job_output 查看输出定位原因",
    schema.STATUS_CANCELLED:      "已被人工取消",
    schema.STATUS_KILLED_STALLED: "超过 max_runtime 已被自动终止；可缩小日期区间或指定 codes 后重跑",
}

# 需要人看一眼的状态
_ATTENTION_STATUSES = frozenset({
    schema.STATUS_STALLED, schema.STATUS_FAILED,
    schema.STATUS_PARTIAL, schema.STATUS_KILLED_STALLED,
})


def _status_hint(info: dict) -> str:
    status = info["status"]
    if status == schema.STATUS_QUEUED:
        position = info.get("queue_position")
        return (f"排队中，前面还有 {position} 个任务" if position
                else "排队中，即将开始")
    if status == schema.STATUS_RUNNING:
        progress = info.get("progress")
        if progress:
            return f"运行中：已处理 {progress['done']}/{progress['total']}（{progress['percent']}%）"
        return "运行中"
    if status == schema.STATUS_STALLED:
        return (
            f"⚠ 疑似卡死：已静默 {info.get('idle_seconds')}s，"
            f"超过阈值 {info.get('stall_timeout')}s，但进程仍存活。"
            f"可以继续等（长区间任务本就间隔长），"
            f"也可以 cancel_job 终止后按 last_line 的进度定向重跑。"
        )
    return _STATUS_HINT.get(status, status)


def _view(info: dict) -> dict:
    """给模型看的任务视图：在原始字段上补一句人话和一个是否需要关注的标记。"""
    return {
        **info,
        "status_hint": _status_hint(info),
        "needs_attention": info["status"] in _ATTENTION_STATUSES,
        "finished": info["status"] in schema.TERMINAL_STATUSES,
    }


def _error(message: str, **extra) -> dict:
    """参数类错误返回结构化结果而非抛异常，便于模型自行改正后重试。"""
    return {"ok": False, "error": message, **extra}


# ---------------------------------------------------------------- 执行

def _resolve_stall_timeout(values: dict, override: int | None) -> int:
    if override is not None:
        return override
    # 按日期跨度分档：长区间任务本身进度间隔就长，阈值需放宽
    try:
        span = (params.span_days(values["begin"], values["end"])
                if "begin" in values and "end" in values else 1)
    except params.ParamError:
        span = 1
    return schema.stall_timeout_default() or schema.default_stall_timeout(span)


def _submit_one(program: str, values: dict, *,
                stall_timeout: int | None, max_runtime: int | None,
                retries: int, meta: dict | None = None) -> str:
    argv = params.build_argv(program, values)
    return get_runner().submit(
        program, argv,
        stall_timeout=_resolve_stall_timeout(values, stall_timeout),
        max_runtime=max_runtime,
        retries=retries,
        meta={"params": values, **(meta or {})},
    )


def _launch(program: str, values: dict, *,
            wait_seconds: int,
            stall_timeout: int | None,
            max_runtime: int | None,
            chunk: str = schema.CHUNK_NONE,
            retries: int = 0) -> dict:
    """校验参数 → 构造 argv → 提交 → 内联等待至多 wait_seconds。

    chunk 非 none 时按自然月/年分片，每段一个任务，返回批次结构（含 failed_segments）；
    chunk 为 none 时返回单任务结构。两种形状的第一个字段都是 ok，
    随后分别看 job_id 或 batch_id。
    """
    values = {k: v for k, v in values.items() if v is not None}
    retries = max(0, min(int(retries), schema.MAX_RETRIES))

    if chunk != schema.CHUNK_NONE:
        return _launch_chunked(program, values, chunk=chunk, retries=retries,
                               wait_seconds=wait_seconds,
                               stall_timeout=stall_timeout, max_runtime=max_runtime)

    try:
        job_id = _submit_one(program, values, stall_timeout=stall_timeout,
                             max_runtime=max_runtime, retries=retries)
    except params.ParamError as e:
        return _error(str(e), program=program)
    except params.IntrospectionError as e:
        return _error(f"无法获取 '{program}' 的参数定义：{e}", program=program)

    info = (get_runner().wait(job_id, timeout=wait_seconds) if wait_seconds > 0
            else get_runner().get(job_id))
    result = {"ok": True, **_view(info)}
    if not result["finished"]:
        result["note"] = (
            f"任务仍在进行，已转入后台。用 get_job('{job_id}') 查看进度，"
            f"用 get_job_output('{job_id}') 看输出。"
        )
    return result


def _launch_chunked(program: str, values: dict, *, chunk: str, retries: int,
                    wait_seconds: int, stall_timeout: int | None,
                    max_runtime: int | None) -> dict:
    """分片执行。单段卡死只损失一段，把「整晚白跑」降级为「丢一段」。"""
    if "begin" not in values or "end" not in values:
        return _error("分片执行需要同时指定 begin 与 end", program=program)
    try:
        segments = params.split_range(values["begin"], values["end"], chunk)
    except params.ParamError as e:
        return _error(str(e), program=program)

    batch_id = f"{program}-batch-{uuid.uuid4().hex[:8]}"
    submitted: list[dict] = []
    for index, (seg_begin, seg_end) in enumerate(segments):
        seg_values = {**values, "begin": seg_begin, "end": seg_end}
        try:
            job_id = _submit_one(
                program, seg_values,
                stall_timeout=stall_timeout, max_runtime=max_runtime,
                retries=retries,
                meta={"batch_id": batch_id, "segment_index": index,
                      "segment": [seg_begin, seg_end]},
            )
        except params.ParamError as e:
            # 某一段参数就不合法时立刻停手，已提交的段照常跑完
            return {"ok": False, "error": str(e), "program": program,
                    "batch_id": batch_id, "failed_segment": [seg_begin, seg_end],
                    "submitted": submitted}
        except params.IntrospectionError as e:
            return _error(f"无法获取 '{program}' 的参数定义：{e}", program=program)
        submitted.append({"begin": seg_begin, "end": seg_end, "job_id": job_id})

    # 等待预算是整批共享的，不是每段各给一份
    deadline = time.monotonic() + max(0, wait_seconds)
    for item in submitted:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        get_runner().wait(item["job_id"], timeout=remaining)

    return _batch_view(batch_id, chunk, submitted)


def _batch_view(batch_id: str, chunk: str, submitted: list[dict]) -> dict:
    jobs = [_view(j) for j in get_runner().batch(batch_id)]
    by_id = {j["job_id"]: j for j in jobs}
    segments = []
    failed_segments = []
    for item in submitted:
        info = by_id.get(item["job_id"], {})
        entry = {
            "begin": item["begin"], "end": item["end"],
            "job_id": item["job_id"],
            "status": info.get("status"),
            "attempt": info.get("attempt"),
            "progress": info.get("progress"),
        }
        segments.append(entry)
        if info.get("status") in _ATTENTION_STATUSES:
            failed_segments.append({
                **entry,
                "last_line": info.get("last_line"),
                "error": info.get("error"),
                "status_hint": info.get("status_hint"),
            })

    pending = [s for s in segments if s["status"] not in schema.TERMINAL_STATUSES]
    succeeded = sum(1 for s in segments if s["status"] == schema.STATUS_SUCCEEDED)

    note_parts = []
    if pending:
        note_parts.append(f"{len(pending)} 段仍在后台，用 list_jobs 或 get_job 跟进")
    if failed_segments:
        note_parts.append(
            f"{len(failed_segments)} 段需要关注；可按 last_line 的进度用 codes "
            f"对该段定向重跑，无需整体重来"
        )
    return {
        "ok": True,
        "batch_id": batch_id,
        "chunk": chunk,
        "segment_count": len(segments),
        "succeeded": succeeded,
        "failed_segments": failed_segments,
        "pending_count": len(pending),
        "finished": not pending,
        "segments": segments,
        "note": "；".join(note_parts) or "全部分片成功",
    }


# ---------------------------------------------------------------- ETL 执行类 Tool

@mcp.tool()
def etl_import_daily(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    source: str | None = None,
    print_only: bool = False,
    chunk: str = schema.CHUNK_NONE,
    retries: int = 0,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """下载股票日线交易明细并入库（etl.import_daily）。

    begin/end 为 YYYYMMDD；end 省略时等于 begin（只跑单日）。
    codes 形如 ["600519", "000001.SZ"]，不传则按 exchanges 处理全市场。
    print_only=True 为干跑：只把结果打到屏幕，不写库，适合先验证一遍。
    wait_seconds 内跑完直接返回结果，否则转后台并返回 job_id。
    
    chunk 可设 month / year 按自然月或年分片：每段一个任务串行执行，
    单段卡死只损失一段，返回结构里的 failed_segments 指出哪几段要补。
    超过 3650 天的区间**必须**分片，否则会被拒绝。
    retries 是单段失败(含被判卡死后终止)的自动重试次数，上限 5。
    """
    return _launch("import_daily", {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges,
        "source": source, "print_only": print_only or None,
    }, wait_seconds=wait_seconds, stall_timeout=stall_timeout,
       max_runtime=max_runtime, chunk=chunk, retries=retries)


@mcp.tool()
def etl_adjust(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    source: str | None = None,
    chunk: str = schema.CHUNK_NONE,
    retries: int = 0,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """下载复权因子并稠密化到逐交易日（etl.adjust）。

    即便区间内没有新的复权事件也应执行——它同时负责把 ADJ_FACTOR 向前填充到 end。
    
    chunk 可设 month / year 按自然月或年分片：每段一个任务串行执行，
    单段卡死只损失一段，返回结构里的 failed_segments 指出哪几段要补。
    超过 3650 天的区间**必须**分片，否则会被拒绝。
    retries 是单段失败(含被判卡死后终止)的自动重试次数，上限 5。
    """
    return _launch("adjust", {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges, "source": source,
    }, wait_seconds=wait_seconds, stall_timeout=stall_timeout,
       max_runtime=max_runtime, chunk=chunk, retries=retries)


@mcp.tool()
def etl_fetch_index(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    source: str | None = None,
    chunk: str = schema.CHUNK_NONE,
    retries: int = 0,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """下载指数日线交易明细并入库（etl.fetch_index）。codes 传指数代码，如 ["000001"]。

    chunk 可设 month / year 按自然月或年分片：每段一个任务串行执行，
    单段卡死只损失一段，返回结构里的 failed_segments 指出哪几段要补。
    超过 3650 天的区间**必须**分片，否则会被拒绝。
    retries 是单段失败(含被判卡死后终止)的自动重试次数，上限 5。
    """
    return _launch("fetch_index", {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges, "source": source,
    }, wait_seconds=wait_seconds, stall_timeout=stall_timeout,
       max_runtime=max_runtime, chunk=chunk, retries=retries)


@mcp.tool()
def etl_fill_indicators(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    forcerun: bool = False,
    targets: list[str] | None = None,
    retries: int = 0,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """补齐量比 / 涨跌停 / 股本三类指标（三个程序参数一致，日常一起补）。

    targets 默认全做，顺序为 fill_volratio → update_limit → fill_shares；
    只补其中一两项时传子集。**前置条件**：这三项都依赖当日日线，
    须先跑 etl_import_daily。
    forcerun=True 可在非交易日强制执行。

    每个 target 是一个独立任务，按 targets 顺序串行执行（DuckDB 单写者）。
    """
    selected = list(targets) if targets else list(schema.FILL_TARGETS)
    unknown = [t for t in selected if t not in schema.FILL_TARGETS]
    if unknown:
        return _error(
            f"未知的 targets: {', '.join(unknown)}；"
            f"可选: {', '.join(schema.FILL_TARGETS)}"
        )
    # 保持注册表声明的顺序，避免模型传乱序导致依赖颠倒
    selected = [t for t in schema.FILL_TARGETS if t in selected]

    results = []
    budget = wait_seconds
    for target in selected:
        started = _launch(target, {
            "begin": begin, "end": end if end is not None else begin,
            "codes": codes, "exchanges": exchanges,
            "forcerun": forcerun or None,
        }, wait_seconds=budget, stall_timeout=stall_timeout,
           max_runtime=max_runtime, retries=retries)
        results.append(started)
        if not started.get("ok"):
            return {"ok": False, "error": started["error"],
                    "completed": results[:-1], "failed_target": target}
        # 等待预算在多个任务间共享，避免 3 个 target 把调用方拖到 3 倍超时
        budget = max(0, budget - int(started.get("elapsed_seconds") or 0))

    pending = [r["job_id"] for r in results if not r["finished"]]
    return {
        "ok": True,
        "targets": selected,
        "jobs": results,
        "finished": not pending,
        "note": (f"仍有 {len(pending)} 个任务在后台：{', '.join(pending)}"
                 if pending else "全部完成"),
    }


# ---------------------------------------------------------------- 任务管理 Tool

@mcp.tool()
def list_jobs(status: str | None = None, limit: int = 20) -> dict:
    """列出任务，最近的在前；`stalled` 一律置顶，因为它最需要人看一眼。

    status 可选：queued / running / succeeded / partial / failed /
    stalled / killed_stalled / cancelled。
    """
    if status and status not in _ALL_STATUSES:
        return _error(f"未知状态 '{status}'；可选: {', '.join(sorted(_ALL_STATUSES))}")
    jobs = [_view(j) for j in get_runner().list(status=status, limit=limit)]
    attention = [j["job_id"] for j in jobs if j["needs_attention"]]
    return {
        "ok": True,
        "count": len(jobs),
        "jobs": jobs,
        "needs_attention": attention,
    }


@mcp.tool()
def get_job(job_id: str) -> dict:
    """查看单个任务：状态、退出码、耗时、静默时长、进度、排队位置。

    判断卡死看 idle_seconds 与 status——`stalled` 表示进程还活着但久未输出，
    不是失败，可继续等也可以取消。
    """
    try:
        return {"ok": True, **_view(get_runner().get(job_id))}
    except runner_mod.JobNotFound as e:
        return _error(str(e))


@mcp.tool()
def get_job_output(job_id: str, tail: int = 200, only_errors: bool = False) -> dict:
    """查看任务的子进程输出尾部。only_errors=True 只保留 ERROR / WARNING 行。"""
    try:
        return {"ok": True, **get_runner().output(job_id, tail=tail,
                                                  only_errors=only_errors)}
    except runner_mod.JobNotFound as e:
        return _error(str(e))


@mcp.tool()
def cancel_job(job_id: str, force: bool = False) -> dict:
    """取消任务。先发 SIGTERM；force=True 时在宽限期后补 SIGKILL。

    三个下载型 ETL 在下载阶段都不持有数据库写锁，此时取消是安全的。
    """
    try:
        return {"ok": True, **_view(get_runner().cancel(job_id, force=force))}
    except runner_mod.JobNotFound as e:
        return _error(str(e))


# ---------------------------------------------------------------- 日志类 Tool

@mcp.tool()
def list_etl_logs(limit: int = 30) -> dict:
    """列出已有的 ETL 日志文件（日期、大小、修改时间），最新在前。

    一天一个文件，当天所有 ETL 程序共用。想复盘某晚的运行先用它找到日期。
    """
    try:
        records = logs_mod.list_logs(limit=limit)
    except RuntimeError as e:          # 环境变量未配置
        return _error(str(e))
    return {"ok": True, "count": len(records), "logs": records}


@mcp.tool()
def read_etl_log(
    date: str | None = None,
    tail: int = 200,
    level: str | None = None,
    module: str | None = None,
    keyword: str | None = None,
    max_bytes: int = logs_mod.DEFAULT_MAX_BYTES,
) -> dict:
    """读取某天 ETL 日志的尾部，可按级别 / 模块 / 关键字过滤。

    date 省略为当天（YYYYMMDD）。level 精确匹配（ERROR/WARNING/INFO...），
    module 子串匹配（'import_daily' 可匹配 'etl.import_daily'），keyword 匹配整行。
    只读文件尾部 max_bytes，超出会标记 truncated，避免超大日志灌爆上下文。
    """
    try:
        return {"ok": True, **logs_mod.read_log(
            date, tail=tail, level=level, module=module,
            keyword=keyword, max_bytes=max_bytes)}
    except logs_mod.LogNotFound as e:
        return _error(str(e), hint="用 list_etl_logs 看有哪些日期的日志")
    except (ValueError, RuntimeError) as e:
        return _error(str(e))


@mcp.tool()
def summarize_etl_log(date: str | None = None) -> dict:
    """摘要某天的 ETL 日志：级别计数、按模块分组、错误样本，以及**卡死线索**。

    重点看 `stall_suspects`：某个模块的最后一行如果是进度行（形如「已处理: 3400/5400」），
    说明它停在下载途中就没了下文——正常跑完会有「批量采集完成」，正常失败会有 ERROR，
    两者皆无而止于进度行，就是卡死后被 kill（或至今仍挂着）的典型形态。
    该判据与查看时间无关，隔几天复盘同样成立。

    这与 get_job 的心跳互补：心跳只看得见本服务启动的任务，
    cron 昨晚那一次只能靠日志复盘。
    """
    try:
        return {"ok": True, **logs_mod.summarize_log(date)}
    except logs_mod.LogNotFound as e:
        return _error(str(e), hint="用 list_etl_logs 看有哪些日期的日志")
    except (ValueError, RuntimeError) as e:
        return _error(str(e))


# ---------------------------------------------------------------- 校验类 Tool

CHECK_TIMEOUT_DEFAULT = 300


@mcp.tool()
def check_data_gaps(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    include_index: bool = False,
    forcerun: bool = False,
    max_detail: int = 200,
    timeout_seconds: int = CHECK_TIMEOUT_DEFAULT,
) -> dict:
    """检查指定区间的数据完整性，返回缺失的日期与股票清单（转发 tools.check_daily）。

    基于 DB 事实判定：用 STOCK_INFO + TRADE_CAL 算出预期记录数，反查实际落库数，
    因此 ETL 逻辑怎么变，检查结果都自动跟着变。停牌股票不计入缺失。

    **补数闭环**：summarize_etl_log → check_data_gaps → 用返回的 date/codes
    定向调 etl_* 补 → 再 check_data_gaps 复核。

    返回里看两处：
      * `core.checks[].gap_dates` —— 哪几天缺、缺几条，喂给 begin/end；
      * `core.checks[].missing_codes` —— 具体缺哪些代码，喂给 codes。
        它受 max_detail 约束（默认 200），截断时 missing_codes_truncated 为真，
        完整明细见同一项里的 csv_path。

    以 `status` 判断结果（complete / gaps_found / error），
    **不要**用退出码——check_daily 的退出码是另一套语义。

    本工具只读，不进 ETL 的串行队列，因此不会被正在跑的补数任务挡住；
    但若此刻有写任务持有 DuckDB 写锁，只读连接会打不开，届时会明确报错。
    """
    values = {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges,
        "include_index": include_index or None,
        "forcerun": forcerun or None,
        "json_max_detail": max_detail,
    }
    try:
        argv = params.build_check_argv(values)
    except params.ParamError as e:
        return _error(str(e))
    except RuntimeError as e:            # 环境变量未配置
        return _error(str(e))

    try:
        proc = subprocess.run(
            argv,
            cwd=str(schema.spring_dir()),
            capture_output=True, text=True,
            timeout=timeout_seconds,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return _error(f"完整性检查超时（{timeout_seconds}s）；"
                      f"可缩小日期区间或指定 codes 后重试")
    except OSError as e:
        return _error(f"无法启动检查进程：{e}")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return _error(
            f"检查未返回结果（exit={proc.returncode}）",
            stderr_tail=(proc.stderr or "").strip()[-800:],
            hint=_db_lock_hint(),
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        return _error(f"检查输出不是合法 JSON：{e}",
                      stdout_head=stdout[:400],
                      stderr_tail=(proc.stderr or "").strip()[-800:])

    if payload.get("status") == schema.CHECK_STATUS_ERROR:
        return {"ok": False, "error": payload.get("error", "检查出错"),
                **payload, "hint": _db_lock_hint()}

    has_gaps = payload.get("status") == schema.CHECK_STATUS_GAPS_FOUND
    return {
        "ok": True,
        "has_gaps": has_gaps,
        "summary": (f"发现 {payload['core']['missing_total']} 条缺失"
                    if has_gaps else "核心日线数据完整"),
        **payload,
    }


def _db_lock_hint() -> str | None:
    """DuckDB 单写者：有写任务在跑时只读连接打不开，提示调用方等它结束。"""
    try:
        running = [j["job_id"] for j in get_runner().list(limit=50)
                   if j["status"] in (schema.STATUS_RUNNING, schema.STATUS_STALLED)]
    except Exception:                    # noqa: BLE001 提示信息拿不到不该盖过真实错误
        return None
    if running:
        return (f"当前有写任务在跑（{', '.join(running)}），DuckDB 单写者会挡住只读连接。"
                f"可等它结束或先 cancel_job 再重试。")
    return None


# ---------------------------------------------------------------- 自省 Tool

@mcp.tool()
def describe_etl_program(name: str) -> dict:
    """返回某个 ETL 程序当前的真实命令行参数定义（转发 spring 的 tools.describe_cli）。

    结果直接来自 spring 的 argparse，因此永不过期；help 文本原样透传。

    两个默认值陷阱：
    1. import_daily / adjust / fetch_index 的 begin/end 默认值是**自省当天**，逐日变化；
    2. fill_* 三个程序的 begin/end 在 argparse 层默认为 null，真实默认（T-1／今天）
       是在解析之后才套用的，只能从 help 文本读出来。
    """
    try:
        return {"ok": True, **params.fetch_program_schema(name)}
    except params.ParamError as e:
        return _error(str(e))
    except params.IntrospectionError as e:
        return _error(f"自省失败：{e}")


_ALL_STATUSES = frozenset({
    schema.STATUS_QUEUED, schema.STATUS_RUNNING, schema.STATUS_STALLED,
    *schema.TERMINAL_STATUSES,
})


# ---------------------------------------------------------------- 入口

def main() -> int:
    try:
        schema.validate_environment()
        transport, host, port = transport_settings()
    except (RuntimeError, ValueError) as e:
        print(f"[quant-etl] 启动失败：{e}", file=sys.stderr)
        return 1

    if transport != TRANSPORT_STDIO:
        mcp.settings.host = host
        mcp.settings.port = port
        if not is_loopback(host):
            print(
                f"[quant-etl] ⚠ 监听地址 {host} 不是回环地址。本服务不做鉴权，"
                f"能连上此端口即可触发 ETL 写库。确认这是你要的。",
                file=sys.stderr,
            )
        print(f"[quant-etl] {transport} 监听 {host}:{port}", file=sys.stderr)

    mcp.run(transport=transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
