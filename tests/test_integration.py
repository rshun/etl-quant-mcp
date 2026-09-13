# 修改记录:
#   2026-08-19  Claude  新建：需要真实 spring 在场的端到端用例
#   2026-09-13  Claude  日志判据改为「带时间戳的行必须可解析」，不再用 unparsed_lines：
#                       多行消息的续行与空行也计入该计数，会把跑挂了误报成格式漂移
"""端到端集成测试——**需要真实 spring 环境**。

全部标 `integration`，日常用 `pytest -m "not integration"` 跳过。
环境变量缺失时自动 skip，不会因为没配环境就报红。

跑起来需要：

```bash
export SPRING_DIR=/path/to/spring
export SPRING_PYTHON=/path/to/venv/bin/python
pytest -m integration
```

这里只放**必须凑齐两个仓库才能验证**的事情：契约漂移、真实子进程执行、
真实日志解析。纯逻辑一律留在 test_contract / test_params / test_runner 里。
"""
import json
import os
import re
import shutil

import pytest

import logs as logs_mod
import params
import schema
from runner import Runner
from tests.conftest import load_snapshot, normalize_defaults

pytestmark = pytest.mark.integration

SNAPSHOT = load_snapshot()


def _env_ready() -> bool:
    return bool(os.environ.get("SPRING_DIR") and os.environ.get("SPRING_PYTHON"))


pytest_skip = pytest.mark.skipif(
    not _env_ready(),
    reason="需要 SPRING_DIR / SPRING_PYTHON 指向真实 spring 环境",
)


@pytest.fixture(scope="module", autouse=True)
def _require_env():
    if not _env_ready():
        pytest.skip("需要 SPRING_DIR / SPRING_PYTHON 指向真实 spring 环境")


@pytest.fixture
def runner(tmp_path):
    r = Runner(jobs_dir=tmp_path, poll_interval=0.1)
    yield r
    r.shutdown()


# ── 环境与契约漂移 ────────────────────────────────────────────────────────────

def test_environment_validates():
    """正例: 真实环境下 validate_environment 应通过"""
    schema.validate_environment()


def test_live_introspection_matches_snapshot():
    """正例(契约漂移守卫): 实时自省结果必须与固化快照一致。

    这是两个仓库唯一必须凑齐才能验的事：spring 改了 CLI 而本服务没跟上，
    只有这条会红。日期默认值先归一化，否则它每天都会因「今天不是昨天」而红。
    """
    live = {}
    for name in schema.PROGRAMS:
        live[name] = params.fetch_program_schema(name)
    assert normalize_defaults(live) == normalize_defaults(SNAPSHOT), (
        "实时自省与快照不一致——spring 的 CLI 可能变了。"
        "确认变更合理后重新生成 tests/fixtures/describe_cli_snapshot.json"
    )


@pytest.mark.parametrize("name", sorted(schema.PROGRAMS))
def test_describe_cli_exits_zero(name):
    """正例: 自省出口对每个注册程序都可用"""
    schema_payload = params.fetch_program_schema(name, refresh=True)
    assert schema_payload["program"] == name
    assert schema_payload["arguments"]


def test_describe_cli_rejects_unregistered_program():
    """反例: 白名单在真实环境下同样生效"""
    with pytest.raises(params.ParamError):
        params.fetch_program_schema("nosuch_program")


# ── 真实子进程执行 ────────────────────────────────────────────────────────────

def test_dry_run_succeeds_end_to_end(runner):
    """正例(核心): 构造 argv → 起真实子进程 → 读退出码，全链路走通。

    用 -p 干跑：走完整的下载路径但不写库，对真实数据库无副作用。
    """
    argv = params.build_argv("import_daily", {
        "begin": "20260817", "end": "20260817",
        "codes": ["600519"], "print_only": True,
    })
    job_id = runner.submit("import_daily", argv, stall_timeout=120, max_runtime=300)
    info = runner.wait(job_id, timeout=300)

    assert info["status"] == schema.STATUS_SUCCEEDED, info
    assert info["exit_code"] == 0
    assert info["line_count"] > 0, "应捕获到子进程输出"


def test_invalid_date_fails_with_exit_1(runner):
    """反例(核心): 真实 ETL 参数校验失败时退出码为 1，不得静默成功。

    这正是 S1 契约要保证的事——改造之前它返回 0，夜跑失败完全无声。
    """
    argv = params.build_argv("import_daily", {
        "begin": "20270105", "end": "20270105",
    })
    job_id = runner.submit("import_daily", argv, stall_timeout=120, max_runtime=300)
    info = runner.wait(job_id, timeout=300)

    assert info["status"] == schema.STATUS_FAILED
    assert info["exit_code"] == 1


def test_argparse_usage_error_exits_2(runner):
    """反例(核心): 非法枚举值由 argparse 拒绝，退出码为 2 且必须判为失败。

    钉住它是因为 2 曾被设计为「部分成功」——那会把「参数传错」读成「大体成功」。
    """
    interpreter = str(schema.spring_python())
    argv = [interpreter, "-u", "-m", "etl.import_daily", "-s", "nosuch_source"]
    job_id = runner.submit("import_daily", argv, stall_timeout=60, max_runtime=120)
    info = runner.wait(job_id, timeout=120)

    assert info["exit_code"] == 2
    assert info["status"] == schema.STATUS_FAILED


# ── 完整性检查 ────────────────────────────────────────────────────────────────

def test_check_daily_json_output_is_parseable():
    """正例: check_daily 的 --json 出口返回可解析 JSON 且结构符合约定"""
    import subprocess
    argv = params.build_check_argv({
        "begin": "20260817", "end": "20260817", "codes": ["600519"],
    })
    proc = subprocess.run(argv, cwd=str(schema.spring_dir()),
                          capture_output=True, text=True, timeout=300)
    payload = json.loads(proc.stdout)

    assert payload["tool"] == "check_daily"
    assert payload["status"] in (schema.CHECK_STATUS_COMPLETE,
                                 schema.CHECK_STATUS_GAPS_FOUND,
                                 schema.CHECK_STATUS_ERROR)
    assert "core" in payload or payload["status"] == schema.CHECK_STATUS_ERROR


def test_check_daily_stdout_carries_no_log_lines():
    """反例(关键): --json 时日志必须走 stderr，stdout 只能有 JSON。

    混进日志的话调用方拿到的东西根本解析不了。
    """
    import subprocess
    argv = params.build_check_argv({"begin": "20260817", "end": "20260817"})
    proc = subprocess.run(argv, cwd=str(schema.spring_dir()),
                          capture_output=True, text=True, timeout=300)
    json.loads(proc.stdout)          # 解析不了就直接失败
    assert "[INFO]" not in proc.stdout
    assert "[WARNING]" not in proc.stdout


# ── 日志 ──────────────────────────────────────────────────────────────────────

def test_log_directory_reachable():
    """正例: 日志目录可达（本服务的观测出口之一）"""
    assert schema.spring_log_dir().is_dir(), \
        f"日志目录不存在: {schema.spring_log_dir()}"


# 带时间戳前缀的行 = 一条日志记录的首行。多行消息(如 DuckDB 的异常文本)的续行
# 没有前缀，本就解析不出来，不该算作格式漂移。
_TIMESTAMPED = re.compile(r"^\d{2}:\d{2}:\d{2}\s")


def test_real_log_record_heads_all_parse():
    """正例(关键): 真实日志里每条记录的首行都必须能被解析。

    判据只针对**带 `HH:MM:SS` 前缀的行**——那才是 spring 的日志格式本身。
    解析不了这样的行，才说明格式漂移了，需同步 `logs.LINE_RE`；
    而 summarize_etl_log 的卡死判据完全建立在这些行解析成功之上。

    刻意**不**断言 `unparsed_lines == 0`：多行消息的续行与空行都会计入该计数，
    却与格式无关。一条 DuckDB 的 `Catalog Error` 就能带进四五行续行，
    用那个计数做判据会把「某次跑挂了」误报成「日志格式变了」，
    把排查方向带偏（2026-09-13 真实踩过）。
    """
    available = logs_mod.list_logs(limit=1)
    if not available:
        pytest.skip("日志目录下暂无 stockdaily*.log")

    date = available[0]["date"]
    text = logs_mod.log_path(date).read_text(encoding="utf-8", errors="replace")
    bad = [line for line in text.splitlines()
           if _TIMESTAMPED.match(line) and logs_mod.parse_line(line) is None]
    assert not bad, (
        f"{len(bad)} 行带时间戳却解析不了——spring 的日志格式可能变了，"
        f"需同步 logs.LINE_RE；首例: {bad[0][:120]!r}"
    )

    summary = logs_mod.summarize_log(date)
    assert summary["modules"], "应至少解析出一个模块"
