# 修改记录:
#   2026-08-19  Claude  新建：Tool 注册、参数闸门与任务管理的正反例
"""server.py 的正反例。

分两类测：
  * **校验路径**用固化的参数 schema 走真实 build_argv，确保非法入参真的被拦下；
  * **接线路径**把 build_argv 换成一个无害的小脚本，用真实 Runner 跑通
    提交 → 等待 → 视图渲染的全过程，确保 Tool 层和执行器是接对的。
"""
import asyncio
import sys
from unittest.mock import patch

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
    "describe_etl_program",
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
    assert live_runner.list() == [], "参数非法时不该提交任务"


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
    assert live_runner.list() == []


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

def test_fill_indicators_defaults_to_all_three(live_runner):
    """正例: 不传 targets 时三项全做，顺序固定"""
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
