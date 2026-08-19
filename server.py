# 修改记录:
#   2026-08-19  Claude  新建：FastMCP 入口，注册 ETL 执行、任务管理与自省 Tool
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
"""
import sys

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

def _launch(program: str, values: dict, *,
            wait_seconds: int,
            stall_timeout: int | None,
            max_runtime: int | None) -> dict:
    """校验参数 → 构造 argv → 提交 → 内联等待至多 wait_seconds。"""
    values = {k: v for k, v in values.items() if v is not None}
    try:
        argv = params.build_argv(program, values)
    except params.ParamError as e:
        return _error(str(e), program=program)
    except params.IntrospectionError as e:
        return _error(f"无法获取 '{program}' 的参数定义：{e}", program=program)

    if stall_timeout is None:
        # 按日期跨度分档：长区间任务本身进度间隔就长，阈值需放宽
        try:
            span = params.span_days(values["begin"], values["end"]) \
                if "begin" in values and "end" in values else 1
        except params.ParamError:
            span = 1
        stall_timeout = schema.stall_timeout_default() or schema.default_stall_timeout(span)

    job_id = get_runner().submit(
        program, argv,
        stall_timeout=stall_timeout,
        max_runtime=max_runtime,
        meta={"params": values},
    )

    info = (get_runner().wait(job_id, timeout=wait_seconds) if wait_seconds > 0
            else get_runner().get(job_id))
    result = {"ok": True, **_view(info)}
    if not result["finished"]:
        result["note"] = (
            f"任务仍在进行，已转入后台。用 get_job('{job_id}') 查看进度，"
            f"用 get_job_output('{job_id}') 看输出。"
        )
    return result


# ---------------------------------------------------------------- ETL 执行类 Tool

@mcp.tool()
def etl_import_daily(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    source: str | None = None,
    print_only: bool = False,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """下载股票日线交易明细并入库（etl.import_daily）。

    begin/end 为 YYYYMMDD；end 省略时等于 begin（只跑单日）。
    codes 形如 ["600519", "000001.SZ"]，不传则按 exchanges 处理全市场。
    print_only=True 为干跑：只把结果打到屏幕，不写库，适合先验证一遍。
    wait_seconds 内跑完直接返回结果，否则转后台并返回 job_id。
    """
    return _launch("import_daily", {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges,
        "source": source, "print_only": print_only or None,
    }, wait_seconds=wait_seconds, stall_timeout=stall_timeout, max_runtime=max_runtime)


@mcp.tool()
def etl_adjust(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    source: str | None = None,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """下载复权因子并稠密化到逐交易日（etl.adjust）。

    即便区间内没有新的复权事件也应执行——它同时负责把 ADJ_FACTOR 向前填充到 end。
    """
    return _launch("adjust", {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges, "source": source,
    }, wait_seconds=wait_seconds, stall_timeout=stall_timeout, max_runtime=max_runtime)


@mcp.tool()
def etl_fetch_index(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    source: str | None = None,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    stall_timeout: int | None = None,
    max_runtime: int | None = None,
) -> dict:
    """下载指数日线交易明细并入库（etl.fetch_index）。codes 传指数代码，如 ["000001"]。"""
    return _launch("fetch_index", {
        "begin": begin, "end": end if end is not None else begin,
        "codes": codes, "exchanges": exchanges, "source": source,
    }, wait_seconds=wait_seconds, stall_timeout=stall_timeout, max_runtime=max_runtime)


@mcp.tool()
def etl_fill_indicators(
    begin: str,
    end: str | None = None,
    codes: list[str] | None = None,
    exchanges: list[str] | None = None,
    forcerun: bool = False,
    targets: list[str] | None = None,
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
        }, wait_seconds=budget, stall_timeout=stall_timeout, max_runtime=max_runtime)
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
    except RuntimeError as e:
        print(f"[quant-etl] 启动失败：{e}", file=sys.stderr)
        return 1
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
