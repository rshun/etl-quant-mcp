# 修改记录:
#   2026-08-19  Claude  新建：Tool 注册、参数闸门与任务管理的正反例
#   2026-09-12  Claude  跟进 spring：fill_turnover 并入 etl_fill_indicators
"""server.py 的正反例。

分两类测：
  * **校验路径**用固化的参数 schema 走真实 build_argv，确保非法入参真的被拦下；
  * **接线路径**把 build_argv 换成一个无害的小脚本，用真实 Runner 跑通
    提交 → 等待 → 视图渲染的全过程，确保 Tool 层和执行器是接对的。
"""
import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

import params
import runner as runner_mod
import schema
import server
from tests.conftest import IMPORT_DAILY_SCHEMA

PY = sys.executable


@pytest.fixture
def live_runner(tmp_path):
    """真实执行器，落盘与工作目录都在 tmp。"""
    r = runner_mod.Runner(jobs_dir=tmp_path, cwd=tmp_path,
                          poll_interval=0.05, terminate_grace=1.0)
    server.reset_runner(r)
    yield r
    r.shutdown()
    server.reset_runner(None)


@pytest.fixture
def fake_schema():
    """让 build_argv 用固化 schema，不需要 spring 在场。"""
    with patch.object(params, "fetch_program_schema", return_value=IMPORT_DAILY_SCHEMA):
        yield


def _harmless(code: str = "print('ok')") -> list[str]:
    return [PY, "-u", "-c", code]


# ── Tool 注册 ─────────────────────────────────────────────────────────────────

EXPECTED_TOOLS = {
    "etl_import_daily", "etl_adjust", "etl_fetch_index", "etl_fill_indicators",
    "list_jobs", "get_job", "get_job_output", "cancel_job",
    "list_etl_logs", "read_etl_log", "summarize_etl_log",
    "check_data_gaps", "describe_etl_program",
}


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_all_expected_tools_registered():
    """正例: 4 个执行类 + 4 个任务管理 + 1 个自省"""
    assert set(_tools()) == EXPECTED_TOOLS


def test_no_arbitrary_execution_tool_exposed():
    """反例(安全): 不得暴露任意命令执行或任意 SQL 写入类工具"""
    names = " ".join(_tools()).lower()
    for forbidden in ("exec", "shell", "command", "sql", "query", "drop", "truncate"):
        assert forbidden not in names, f"暴露了疑似危险工具: {forbidden}"


def test_every_tool_has_description():
    """正例: 每个 Tool 都要有说明——这是模型选对工具的唯一依据"""
    for name, tool in _tools().items():
        assert tool.description and tool.description.strip(), f"{name} 缺少说明"


def test_execution_tools_expose_begin():
    """正例: 4 个执行类 Tool 都以 begin 为必填"""
    for name in ("etl_import_daily", "etl_adjust", "etl_fetch_index",
                 "etl_fill_indicators"):
        schema_ = _tools()[name].inputSchema
        assert "begin" in schema_["properties"]
        assert "begin" in schema_.get("required", [])


# ── 参数闸门（反例为主）──────────────────────────────────────────────────────

def test_invalid_date_returns_structured_error(fake_schema, live_runner):
    """反例: 非法日期应返回结构化错误，而不是抛异常或起子进程"""
    result = server.etl_import_daily(begin="20261301", wait_seconds=0)
    assert result["ok"] is False
    assert "不是有效日期" in result["error"]
    assert live_runner.list_jobs() == [], "参数非法时不该提交任务"


def test_injection_in_codes_rejected(fake_schema, live_runner):
    """反例(关键): 代码里夹带 SQL 必须在提交前就被拦下。

    本用例刻意不配置 SPRING_PYTHON——参数校验必须先于环境解析完成，
    否则模型收到的会是「环境变量未设置」，被引向完全错误的排查方向。
    """
    result = server.etl_import_daily(begin="20260817",
                                     codes=["600519; DROP TABLE STOCK_DAILY"],
                                     wait_seconds=0)
    assert result["ok"] is False
    assert "代码非法" in result["error"]
    assert live_runner.list_jobs() == []


def test_invalid_enum_rejected(fake_schema, live_runner):
    """反例: 不在 choices 里的数据源"""
    result = server.etl_import_daily(begin="20260817", source="nosuch", wait_seconds=0)
    assert result["ok"] is False
    assert "取值非法" in result["error"]


def test_begin_after_end_rejected(fake_schema, live_runner):
    """反例: 起始晚于结束"""
    result = server.etl_import_daily(begin="20260818", end="20260817", wait_seconds=0)
    assert result["ok"] is False
    assert "不能晚于" in result["error"]


def test_introspection_failure_surfaces_as_error(live_runner):
    """反例: 自省失败要说清楚，不能让模型以为任务已提交"""
    with patch.object(params, "fetch_program_schema",
                      side_effect=params.IntrospectionError("describe_cli 挂了")):
        result = server.etl_import_daily(begin="20260817", wait_seconds=0)
    assert result["ok"] is False
    assert "无法获取" in result["error"]


# ── 执行接线 ──────────────────────────────────────────────────────────────────

def test_short_job_returns_result_inline(live_runner):
    """正例: wait_seconds 内跑完，直接返回结果而不是 job_id"""
    with patch.object(params, "build_argv", return_value=_harmless()):
        result = server.etl_import_daily(begin="20260817", wait_seconds=15)
    assert result["ok"] is True
    assert result["finished"] is True
    assert result["status"] == schema.STATUS_SUCCEEDED
    assert result["needs_attention"] is False


def test_long_job_goes_background_with_job_id(live_runner):
    """正例: 超过 wait_seconds 转后台，并给出后续该调什么"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import time; time.sleep(5)")):
        result = server.etl_import_daily(begin="20260817", wait_seconds=0)
    assert result["ok"] is True
    assert result["finished"] is False
    assert result["job_id"]
    assert "get_job" in result["note"]
    server.cancel_job(result["job_id"], force=True)


def test_failed_job_flags_attention(live_runner):
    """正例: 失败任务要打上 needs_attention 并给出下一步"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import sys; sys.exit(1)")):
        result = server.etl_import_daily(begin="20260817", wait_seconds=15)
    assert result["status"] == schema.STATUS_FAILED
    assert result["needs_attention"] is True
    assert "get_job_output" in result["status_hint"]


def test_usage_error_exit_code_not_reported_as_partial(live_runner):
    """反例(关键): argparse 的 exit 2 必须呈现为失败，不能让模型以为大体成功了"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import sys; sys.exit(2)")):
        result = server.etl_import_daily(begin="20260817", wait_seconds=15)
    assert result["status"] == schema.STATUS_FAILED
    assert result["needs_attention"] is True


def test_end_defaults_to_begin(fake_schema, live_runner):
    """正例: 省略 end 表示只跑单日"""
    with patch.object(params, "build_argv", wraps=params.build_argv) as spy, \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"), \
         patch.object(runner_mod.Runner, "submit", return_value="fake-id"), \
         patch.object(runner_mod.Runner, "get",
                      return_value={"job_id": "fake-id", "status": schema.STATUS_QUEUED,
                                    "queue_position": 0}):
        server.etl_import_daily(begin="20260817", wait_seconds=0)
    assert spy.call_args.args[1]["end"] == "20260817"


# ── stalled 呈现 ──────────────────────────────────────────────────────────────

def test_stalled_job_hint_explains_options(live_runner):
    """正例(核心): stalled 的提示要说清「进程还活着」并给出两条路"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import time; print('start'); time.sleep(30)")):
        started = server.etl_import_daily(begin="20260817", wait_seconds=0,
                                          stall_timeout=1, max_runtime=60)
    job_id = started["job_id"]

    import time as _t
    deadline = _t.monotonic() + 10
    while _t.monotonic() < deadline:
        info = server.get_job(job_id)
        if info["status"] == schema.STATUS_STALLED:
            break
        _t.sleep(0.05)

    info = server.get_job(job_id)
    assert info["status"] == schema.STATUS_STALLED
    assert info["needs_attention"] is True
    assert "进程仍存活" in info["status_hint"]
    assert "cancel_job" in info["status_hint"]
    server.cancel_job(job_id, force=True)


def test_list_jobs_collects_attention_ids(live_runner):
    """正例: 列表要单独汇总需要关注的任务 id"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import sys; sys.exit(1)")):
        failed = server.etl_import_daily(begin="20260817", wait_seconds=15)
    listing = server.list_jobs()
    assert listing["ok"] is True
    assert failed["job_id"] in listing["needs_attention"]


def test_list_jobs_rejects_unknown_status(live_runner):
    """反例: 未知状态名要明确报错并列出可选值"""
    result = server.list_jobs(status="nosuch")
    assert result["ok"] is False
    assert "未知状态" in result["error"]


# ── 任务管理：未知 id 一律返回结构化错误 ──────────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda: server.get_job("nosuch"),
    lambda: server.get_job_output("nosuch"),
    lambda: server.cancel_job("nosuch"),
])
def test_unknown_job_id_returns_error_not_exception(live_runner, call):
    """反例: 未知任务 id 不该抛裸异常"""
    result = call()
    assert result["ok"] is False
    assert "任务不存在" in result["error"]


def test_cancel_running_job(live_runner):
    """正例: 取消运行中的任务"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import time; print('x'); time.sleep(30)")):
        started = server.etl_import_daily(begin="20260817", wait_seconds=0,
                                          stall_timeout=30, max_runtime=60)
    import time as _t
    deadline = _t.monotonic() + 10
    while _t.monotonic() < deadline:
        if server.get_job(started["job_id"])["status"] == schema.STATUS_RUNNING:
            break
        _t.sleep(0.05)
    result = server.cancel_job(started["job_id"], force=True)
    assert result["ok"] is True


def test_job_output_tail(live_runner):
    """正例: 输出按尾部返回"""
    code = "\n".join(f"print('line{i}')" for i in range(10))
    with patch.object(params, "build_argv", return_value=_harmless(code)):
        started = server.etl_import_daily(begin="20260817", wait_seconds=15)
    out = server.get_job_output(started["job_id"], tail=3)
    assert out["ok"] is True
    assert out["lines"] == ["line7", "line8", "line9"]


# ── fill_indicators ───────────────────────────────────────────────────────────

def test_fill_indicators_defaults_to_all_targets(live_runner):
    """正例: 不传 targets 时四项全做，顺序固定"""
    with patch.object(params, "build_argv", return_value=_harmless()):
        result = server.etl_fill_indicators(begin="20260817", wait_seconds=15)
    assert result["ok"] is True
    assert result["targets"] == list(schema.FILL_TARGETS)
    assert result["finished"] is True


def test_fill_indicators_normalizes_target_order(live_runner):
    """正例(关键): 模型传乱序时按注册表顺序纠正，避免依赖颠倒"""
    with patch.object(params, "build_argv", return_value=_harmless()):
        result = server.etl_fill_indicators(
            begin="20260817", targets=["fill_shares", "fill_volratio"],
            wait_seconds=15)
    assert result["targets"] == ["fill_volratio", "fill_shares"]


def test_fill_indicators_overwrite_only_applies_to_turnover(live_runner):
    """正例(关键): overwrite 只带给 fill_turnover。

    -o/--overwrite 只有 fill_turnover 有。透传给别的 target 会被 build_argv
    拒绝，整个补数链就断在第一个 target 上——所以这里按 target 判断。
    """
    with patch.object(params, "build_argv", return_value=_harmless()) as build:
        result = server.etl_fill_indicators(
            begin="20260817", overwrite=True, wait_seconds=15)
    assert result["ok"] is True
    passed = {call.args[0]: call.args[1] for call in build.call_args_list}
    assert passed[schema.FILL_OVERWRITE_TARGET]["overwrite"] is True
    for target in schema.FILL_TARGETS:
        if target != schema.FILL_OVERWRITE_TARGET:
            assert "overwrite" not in passed[target], f"{target} 不该收到 overwrite"


def test_fill_indicators_omits_overwrite_when_off(live_runner):
    """反例: overwrite=False 时连 fill_turnover 也不带该键（默认仅补空行）"""
    with patch.object(params, "build_argv", return_value=_harmless()) as build:
        server.etl_fill_indicators(begin="20260817", wait_seconds=15)
    passed = {call.args[0]: call.args[1] for call in build.call_args_list}
    assert "overwrite" not in passed[schema.FILL_OVERWRITE_TARGET]


def test_fill_indicators_rejects_unknown_target(live_runner):
    """反例: 未知 target"""
    result = server.etl_fill_indicators(begin="20260817", targets=["nosuch"],
                                        wait_seconds=0)
    assert result["ok"] is False
    assert "未知的 targets" in result["error"]


def test_fill_indicators_stops_on_param_error(live_runner):
    """反例: 某个 target 参数校验失败时立即停下并说明是哪个"""
    with patch.object(params, "fetch_program_schema", return_value=IMPORT_DAILY_SCHEMA):
        result = server.etl_fill_indicators(begin="20261301", wait_seconds=0)
    assert result["ok"] is False
    assert result["failed_target"] == "fill_volratio"


# ── 自省 Tool ─────────────────────────────────────────────────────────────────

def test_describe_program_returns_arguments(live_runner):
    """正例: 转发自省结果"""
    with patch.object(params, "fetch_program_schema", return_value=IMPORT_DAILY_SCHEMA):
        result = server.describe_etl_program("import_daily")
    assert result["ok"] is True
    assert "begin" in result["arguments"]


def test_describe_unknown_program_rejected(live_runner):
    """反例: 白名单之外的程序名"""
    result = server.describe_etl_program("rm_rf")
    assert result["ok"] is False
    assert "未知程序" in result["error"]


# ── 日志类 Tool ───────────────────────────────────────────────────────────────

_STALLED_LOG = """\
02:00:00 [etl.adjust] [INFO] 获取股票复权因子任务启动
02:00:40 [etl.datasource.bstock] [INFO]    已处理: 100/5400
02:09:00 [etl.datasource.bstock] [INFO]    已处理: 300/5400
"""


@pytest.fixture
def etl_log_dir(tmp_path, monkeypatch):
    """把日志目录指向临时目录，不碰真实 spring。"""
    directory = tmp_path / "log"
    directory.mkdir()
    monkeypatch.setenv("SPRING_LOG_DIR", str(directory))
    return directory


def test_list_etl_logs(etl_log_dir):
    """正例: 列出日志文件"""
    (etl_log_dir / "stockdaily20260819.log").write_text(_STALLED_LOG, encoding="utf-8")
    result = server.list_etl_logs()
    assert result["ok"] is True
    assert [l["date"] for l in result["logs"]] == ["20260819"]


def test_read_etl_log_with_filter(etl_log_dir):
    """正例: 按关键字过滤"""
    (etl_log_dir / "stockdaily20260819.log").write_text(_STALLED_LOG, encoding="utf-8")
    result = server.read_etl_log(date="20260819", keyword="已处理")
    assert result["ok"] is True
    assert result["matched_lines"] == 2


def test_read_etl_log_missing_file_returns_hint(etl_log_dir):
    """反例: 文件不存在时返回结构化错误并提示下一步，不抛裸异常"""
    result = server.read_etl_log(date="20990101")
    assert result["ok"] is False
    assert "日志文件不存在" in result["error"]
    assert "list_etl_logs" in result["hint"]


def test_read_etl_log_invalid_date_returns_error(etl_log_dir):
    """反例: 日期格式非法"""
    result = server.read_etl_log(date="2026-13")
    assert result["ok"] is False
    assert "日期格式非法" in result["error"]


def test_summarize_etl_log_surfaces_stall_suspect(etl_log_dir):
    """正例(核心): 摘要要能指出「停在下载处」的模块"""
    (etl_log_dir / "stockdaily20260819.log").write_text(_STALLED_LOG, encoding="utf-8")
    result = server.summarize_etl_log(date="20260819")
    assert result["ok"] is True
    assert [s["module"] for s in result["stall_suspects"]] == ["etl.datasource.bstock"]
    assert result["stall_suspects"][0]["progress"]["done"] == 300


def test_summarize_etl_log_missing_file_returns_hint(etl_log_dir):
    """反例: 文件不存在"""
    result = server.summarize_etl_log(date="20990101")
    assert result["ok"] is False
    assert "list_etl_logs" in result["hint"]


# ── 分片执行（M4）────────────────────────────────────────────────────────────

def test_chunked_run_returns_batch_shape(live_runner):
    """正例(核心): 分片返回批次结构，逐段可见"""
    with patch.object(params, "build_argv", return_value=_harmless()):
        result = server.etl_import_daily(begin="20260115", end="20260320",
                                         chunk="month", wait_seconds=30)
    assert result["ok"] is True
    assert result["batch_id"]
    assert result["chunk"] == "month"
    assert result["segment_count"] == 3
    assert [(s["begin"], s["end"]) for s in result["segments"]] == [
        ("20260115", "20260131"), ("20260201", "20260228"), ("20260301", "20260320")]
    assert result["succeeded"] == 3
    assert result["failed_segments"] == []
    assert result["finished"] is True


def test_chunk_none_keeps_single_job_shape(live_runner):
    """正例: 不分片时保持单任务结构，常见调用不被批次结构复杂化"""
    with patch.object(params, "build_argv", return_value=_harmless()):
        result = server.etl_import_daily(begin="20260115", end="20260320",
                                         wait_seconds=15)
    assert "job_id" in result
    assert "batch_id" not in result


def test_failed_segments_are_collected(live_runner):
    """正例(核心): 失败的段被单独收集，附带 last_line 供定向重跑"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import sys; print('   已处理: 30/100'); sys.exit(1)")):
        result = server.etl_import_daily(begin="20260115", end="20260228",
                                         chunk="month", wait_seconds=30)
    assert result["ok"] is True
    assert len(result["failed_segments"]) == 2
    first = result["failed_segments"][0]
    assert first["begin"] == "20260115"
    assert "已处理: 30/100" in first["last_line"]
    assert "定向重跑" in result["note"]


def test_chunked_run_allows_span_over_limit(live_runner):
    """正例(关键): 超过 3650 天的区间分片后放行——分片正是为此存在"""
    with patch.object(params, "build_argv", return_value=_harmless()):
        result = server.etl_import_daily(begin="20000101", end="20260101",
                                         chunk="year", wait_seconds=60)
    assert result["ok"] is True
    assert result["segment_count"] == 27


def test_unchunked_span_over_limit_still_rejected(fake_schema, live_runner):
    """反例: 不分片时超长跨度仍必须拒绝"""
    result = server.etl_import_daily(begin="20000101", end="20260101", wait_seconds=0)
    assert result["ok"] is False
    assert "超过上限" in result["error"]


def test_invalid_chunk_rejected(live_runner):
    """反例: 未知分片方式"""
    result = server.etl_import_daily(begin="20260101", end="20260201",
                                     chunk="week", wait_seconds=0)
    assert result["ok"] is False
    assert "chunk 取值非法" in result["error"]


def test_retries_capped(live_runner):
    """反例: retries 被夹到上限，防止一个坏段无限重试占住串行队列"""
    with patch.object(params, "build_argv", return_value=_harmless()), \
         patch.object(runner_mod.Runner, "submit", return_value="fake") as submit, \
         patch.object(runner_mod.Runner, "get",
                      return_value={"job_id": "fake", "status": schema.STATUS_QUEUED,
                                    "queue_position": 0}):
        server.etl_import_daily(begin="20260817", retries=999, wait_seconds=0)
    assert submit.call_args.kwargs["retries"] == schema.MAX_RETRIES


# ── check_data_gaps（M4）─────────────────────────────────────────────────────

_GAPS_PAYLOAD = {
    "tool": "check_daily",
    "status": "gaps_found",
    "exit_code": 1,
    "params": {"begin": "2026-08-17", "end": "2026-08-17"},
    "core": {
        "missing_total": 2,
        "checks": [{
            "label": "日线数据", "table": "STOCK_DAILY", "missing": 2,
            "gap_dates": [{"date": "2026-08-17", "expected": 2, "actual": 0, "missing": 2}],
            "missing_codes": [{"date": "2026-08-17", "code": "600519.SH", "name": "贵州茅台"}],
            "missing_codes_truncated": False, "missing_codes_total": 1,
            "csv_path": "/tmp/x.csv",
        }],
        "detail_truncated": False, "json_max_detail": 200,
    },
    "warnings": {"total": 0, "checks": []},
    "error": None,
}


def _fake_proc(stdout="", returncode=0, stderr=""):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def test_check_data_gaps_reports_gaps(live_runner):
    """正例(核心): 返回缺失日期与代码，正是补数闭环的输入"""
    import json as json_mod
    with patch.object(server.subprocess, "run",
                      return_value=_fake_proc(json_mod.dumps(_GAPS_PAYLOAD))), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        result = server.check_data_gaps(begin="20260817")
    assert result["ok"] is True
    assert result["has_gaps"] is True
    assert "2 条缺失" in result["summary"]
    check = result["core"]["checks"][0]
    assert check["gap_dates"][0]["date"] == "2026-08-17"
    assert check["missing_codes"][0]["code"] == "600519.SH"


def test_check_data_gaps_complete(live_runner):
    """正例: 数据完整"""
    import json as json_mod
    payload = {**_GAPS_PAYLOAD, "status": "complete",
               "core": {**_GAPS_PAYLOAD["core"], "missing_total": 0}}
    with patch.object(server.subprocess, "run",
                      return_value=_fake_proc(json_mod.dumps(payload))), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        result = server.check_data_gaps(begin="20260817")
    assert result["ok"] is True
    assert result["has_gaps"] is False


def test_check_data_gaps_never_uses_shell(live_runner):
    """正例(安全): 必须以 argv 列表调用且 shell=False"""
    import json as json_mod
    with patch.object(server.subprocess, "run",
                      return_value=_fake_proc(json_mod.dumps(_GAPS_PAYLOAD))) as run, \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        server.check_data_gaps(begin="20260817")
    argv = run.call_args.args[0]
    assert isinstance(argv, list)
    assert argv[-1] == "--json"
    assert run.call_args.kwargs["shell"] is False


def test_check_data_gaps_rejects_injection_before_subprocess(live_runner):
    """反例(关键): 非法代码在起子进程之前就被拦下"""
    with patch.object(server.subprocess, "run") as run:
        result = server.check_data_gaps(begin="20260817",
                                        codes=["600519; DROP TABLE"])
    assert result["ok"] is False
    assert "代码非法" in result["error"]
    run.assert_not_called()


def test_check_data_gaps_surfaces_check_error(live_runner):
    """反例: 检查工具自己报错时如实上报"""
    import json as json_mod
    payload = {"tool": "check_daily", "status": "error", "exit_code": 2,
               "error": "参数校验失败，详见 stderr 日志"}
    with patch.object(server.subprocess, "run",
                      return_value=_fake_proc(json_mod.dumps(payload), returncode=2)), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        result = server.check_data_gaps(begin="20260822")
    assert result["ok"] is False
    assert "参数校验失败" in result["error"]


def test_check_data_gaps_handles_non_json_output(live_runner):
    """反例: 输出不是 JSON 时给出可诊断的信息，不抛裸异常"""
    with patch.object(server.subprocess, "run",
                      return_value=_fake_proc("Traceback...", returncode=1)), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        result = server.check_data_gaps(begin="20260817")
    assert result["ok"] is False
    assert "不是合法 JSON" in result["error"]
    assert "Traceback" in result["stdout_head"]


def test_check_data_gaps_empty_output_hints_db_lock(live_runner):
    """反例(关键): 空输出时提示写锁——DuckDB 单写者会挡住只读连接"""
    with patch.object(params, "build_argv",
                      return_value=_harmless("import time; print('x'); time.sleep(30)")):
        started = server.etl_import_daily(begin="20260817", wait_seconds=0,
                                          stall_timeout=30, max_runtime=60)
    import time as _t
    deadline = _t.monotonic() + 10
    while _t.monotonic() < deadline:
        if server.get_job(started["job_id"])["status"] == schema.STATUS_RUNNING:
            break
        _t.sleep(0.05)

    with patch.object(server.subprocess, "run",
                      return_value=_fake_proc("", returncode=1, stderr="Conflicting lock")), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        result = server.check_data_gaps(begin="20260817")
    assert result["ok"] is False
    assert "写任务在跑" in (result["hint"] or "")
    server.cancel_job(started["job_id"], force=True)


def test_check_data_gaps_timeout(live_runner):
    """反例: 检查超时给出可执行建议"""
    import subprocess as sp
    with patch.object(server.subprocess, "run",
                      side_effect=sp.TimeoutExpired("cmd", 300)), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"), \
         patch.object(schema, "spring_python", return_value="/usr/bin/python3"):
        result = server.check_data_gaps(begin="20260817", timeout_seconds=300)
    assert result["ok"] is False
    assert "超时" in result["error"]


# ── 传输方式（客户端与服务端可分属不同用户）────────────────────────────────────
# ADR-2 定的是「ETL 用子进程调」，与「客户端怎么连服务端」是两条独立的轴。
# stdio 下服务端由客户端拉起、必然同用户；HTTP 下服务端可以常驻在数据管道
# 属主名下，客户端只要连得上端口即可，文件系统权限一点不用动。

@pytest.fixture
def clean_transport_env(monkeypatch):
    for key in ("ETL_MCP_TRANSPORT", "ETL_MCP_HOST", "ETL_MCP_PORT"):
        monkeypatch.delenv(key, raising=False)


def test_transport_defaults_to_stdio(clean_transport_env):
    """正例(关键): 默认仍是 stdio——已有的开发与单机用法不受任何影响"""
    transport, host, port = server.transport_settings()
    assert transport == server.TRANSPORT_STDIO
    assert host == "127.0.0.1"
    assert port == server.DEFAULT_HTTP_PORT


def test_transport_http_opt_in(clean_transport_env, monkeypatch):
    """正例: 显式设置才切到 HTTP"""
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "streamable-http")
    assert server.transport_settings()[0] == server.TRANSPORT_HTTP


def test_transport_accepts_sse(clean_transport_env, monkeypatch):
    """正例: sse 也支持（较老的客户端可能只认它）"""
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "SSE")
    assert server.transport_settings()[0] == server.TRANSPORT_SSE


def test_host_and_port_overridable(clean_transport_env, monkeypatch):
    """正例: 地址与端口可覆盖"""
    monkeypatch.setenv("ETL_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("ETL_MCP_PORT", "9999")
    _, host, port = server.transport_settings()
    assert (host, port) == ("0.0.0.0", 9999)


def test_unknown_transport_rejected(clean_transport_env, monkeypatch):
    """反例: 未知传输方式必须拒绝，不能悄悄退回默认值"""
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "websocket")
    with pytest.raises(ValueError, match="未知的传输方式"):
        server.transport_settings()


@pytest.mark.parametrize("bad", ["abc", "0", "70000", "-1"])
def test_invalid_port_rejected(clean_transport_env, monkeypatch, bad):
    """反例: 非法端口必须拒绝"""
    monkeypatch.setenv("ETL_MCP_PORT", bad)
    with pytest.raises(ValueError):
        server.transport_settings()


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.0.0.53", True), ("localhost", True),
    ("::1", True), ("LOCALHOST", True),
    ("0.0.0.0", False), ("192.168.1.5", False), ("", False),
])
def test_loopback_detection(host, expected):
    """正例/反例: 回环判定——它决定要不要发出暴露面告警"""
    assert server.is_loopback(host) is expected


def test_main_rejects_bad_transport_before_starting(clean_transport_env, monkeypatch):
    """反例: 配置错误应在启动时就返回 1，不进入 run()"""
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "nope")
    with patch.object(schema, "validate_environment"), \
         patch.object(server.mcp, "run") as run:
        assert server.main() == 1
    run.assert_not_called()


def test_main_stdio_does_not_touch_network_settings(clean_transport_env):
    """正例: stdio 模式不应改动监听设置"""
    before = (server.mcp.settings.host, server.mcp.settings.port)
    with patch.object(schema, "validate_environment"), \
         patch.object(server.mcp, "run") as run:
        assert server.main() == 0
    assert (server.mcp.settings.host, server.mcp.settings.port) == before
    run.assert_called_once_with(transport=server.TRANSPORT_STDIO)


def test_main_http_applies_host_and_port(clean_transport_env, monkeypatch):
    """正例(核心): HTTP 模式把地址端口装进 FastMCP 后再启动"""
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("ETL_MCP_PORT", "8899")
    original = (server.mcp.settings.host, server.mcp.settings.port)
    try:
        with patch.object(schema, "validate_environment"), \
             patch.object(server.mcp, "run") as run:
            assert server.main() == 0
        assert server.mcp.settings.host == "127.0.0.1"
        assert server.mcp.settings.port == 8899
        run.assert_called_once_with(transport=server.TRANSPORT_HTTP)
    finally:
        server.mcp.settings.host, server.mcp.settings.port = original


def test_main_warns_when_binding_non_loopback(clean_transport_env, monkeypatch, capsys):
    """正例(安全关键): 绑非回环地址必须告警。

    本服务不做鉴权，「谁能连上这个端口」就是它唯一的授权边界。
    绑到 0.0.0.0 等于把 ETL 写入面开放给整个网段，这件事必须说出来。
    """
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("ETL_MCP_HOST", "0.0.0.0")
    original = (server.mcp.settings.host, server.mcp.settings.port)
    try:
        with patch.object(schema, "validate_environment"), \
             patch.object(server.mcp, "run"):
            server.main()
        stderr = capsys.readouterr().err
        assert "不是回环地址" in stderr
        assert "不做鉴权" in stderr
    finally:
        server.mcp.settings.host, server.mcp.settings.port = original


def test_main_does_not_warn_on_loopback(clean_transport_env, monkeypatch, capsys):
    """反例: 绑回环时不该发告警，否则告警会被当噪音忽略"""
    monkeypatch.setenv("ETL_MCP_TRANSPORT", "streamable-http")
    original = (server.mcp.settings.host, server.mcp.settings.port)
    try:
        with patch.object(schema, "validate_environment"), \
             patch.object(server.mcp, "run"):
            server.main()
        assert "不是回环地址" not in capsys.readouterr().err
    finally:
        server.mcp.settings.host, server.mcp.settings.port = original
