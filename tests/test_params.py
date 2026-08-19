# 修改记录:
#   2026-08-19  Claude  新建：argv 构造与参数白名单的正反例
"""params.py 的正反例。

反例是重点：这一层是本服务唯一的输入闸门，放过一个非法值，
后面就是直接拼进子进程命令行了。
"""
import json
import subprocess
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import params
import schema
from tests.conftest import FAKE_PYTHON, IMPORT_DAILY_SCHEMA


def _argv(**kwargs) -> list[str]:
    return params.build_argv(
        "import_daily", kwargs,
        python=FAKE_PYTHON, program_schema=IMPORT_DAILY_SCHEMA,
    )


# ── argv 构造：正例 ───────────────────────────────────────────────────────────

def test_argv_prefix_is_module_invocation():
    """正例: 必须以 [解释器, -u, -m, 模块] 开头。

    -u 不是可选项——少了它，pipe 的块缓冲会让正常运行被误判为卡死(文档 5.3)。
    """
    argv = _argv(begin="20260817", end="20260817")
    assert argv[:4] == [FAKE_PYTHON, "-u", "-m", "etl.import_daily"]


def test_argv_dates():
    """正例: 日期按短选项拼接"""
    assert _argv(begin="20260817", end="20260818")[4:] == [
        "-b", "20260817", "-e", "20260818",
    ]


def test_argv_codes_list():
    """正例: nargs='+' 的参数展开为「一个选项 + 多个值」"""
    argv = _argv(codes=["600519.SH", "000001.SZ"])
    assert argv[4:] == ["-c", "600519.SH", "000001.SZ"]


def test_argv_code_suffix_normalized_to_upper():
    """正例: 交易所后缀统一大写"""
    assert _argv(codes=["600519.sh"])[4:] == ["-c", "600519.SH"]


def test_argv_code_without_suffix_allowed():
    """正例: 不带后缀的 6 位代码也合法"""
    assert _argv(codes=["600519"])[4:] == ["-c", "600519"]


def test_argv_exchanges_uppercase_input_normalized():
    """正例: type 为 str.lower 时先归一化再比对 choices，用户传 SH 不该被拒"""
    assert _argv(exchanges=["SH", "SZ"])[4:] == ["-x", "sh", "sz"]


def test_argv_store_true_emits_flag_only():
    """正例: store_true 只发选项本身，不带值"""
    assert _argv(print_only=True)[4:] == ["-p"]


def test_argv_store_true_false_is_omitted():
    """正例: store_true 为 False 时整个选项都不出现"""
    assert _argv(print_only=False)[4:] == []


def test_argv_none_value_is_omitted():
    """正例: 值为 None 表示「不传该参数」"""
    assert _argv(begin="20260817", codes=None)[4:] == ["-b", "20260817"]


def test_argv_order_follows_schema_declaration():
    """正例: 拼接顺序跟随自省声明顺序，同样的入参得到同样的 argv(便于比对复现)"""
    a = _argv(source="bstock", begin="20260817", exchanges=["sh"])
    b = _argv(exchanges=["sh"], begin="20260817", source="bstock")
    assert a == b
    assert a[4:] == ["-b", "20260817", "-x", "sh", "-s", "bstock"]


def test_argv_span_at_limit_is_accepted():
    """正例(边界): 跨度恰好等于上限应放行"""
    begin = datetime(2000, 1, 1)
    end = begin + timedelta(days=schema.MAX_SPAN_DAYS - 1)   # 含首尾正好 MAX_SPAN_DAYS 天
    assert params.span_days(f"{begin:%Y%m%d}", f"{end:%Y%m%d}") == schema.MAX_SPAN_DAYS
    argv = _argv(begin=f"{begin:%Y%m%d}", end=f"{end:%Y%m%d}")
    assert argv[4:] == ["-b", f"{begin:%Y%m%d}", "-e", f"{end:%Y%m%d}"]


def test_argv_span_one_over_limit_is_rejected():
    """反例(边界): 只超一天也要拒"""
    begin = datetime(2000, 1, 1)
    end = begin + timedelta(days=schema.MAX_SPAN_DAYS)       # 比上限多一天
    with pytest.raises(params.ParamError, match="超过上限"):
        _argv(begin=f"{begin:%Y%m%d}", end=f"{end:%Y%m%d}")


def test_argv_long_span_allowed_when_chunked():
    """正例: 指定分片后，超长跨度放行"""
    argv = params.build_argv(
        "import_daily", {"begin": "20000101", "end": "20260101"},
        python=FAKE_PYTHON, program_schema=IMPORT_DAILY_SCHEMA,
        allow_long_span=True,
    )
    assert argv[4:] == ["-b", "20000101", "-e", "20260101"]


# ── argv 构造：反例 ───────────────────────────────────────────────────────────

def test_reject_unknown_program():
    """反例: 白名单之外的程序名——本服务不提供任意命令执行"""
    with pytest.raises(params.ParamError, match="未知程序"):
        params.build_argv("rm_rf", {}, python=FAKE_PYTHON)


def test_reject_unknown_parameter_name():
    """反例: 自省结果里没有的参数名一律拒绝，不静默丢弃"""
    with pytest.raises(params.ParamError, match="不接受参数"):
        _argv(nosuch_flag="x")


def test_reject_impossible_date():
    """反例: 20261301 格式对但月份不存在"""
    with pytest.raises(params.ParamError, match="不是有效日期"):
        _argv(begin="20261301")


def test_reject_dashed_date_format():
    """反例: 2026-13-01 这种带分隔符的格式"""
    with pytest.raises(params.ParamError, match="格式非法"):
        _argv(begin="2026-13-01")


def test_reject_short_date():
    """反例: 位数不足"""
    with pytest.raises(params.ParamError, match="格式非法"):
        _argv(begin="202608")


def test_reject_begin_after_end():
    """反例: 起始晚于结束"""
    with pytest.raises(params.ParamError, match="不能晚于"):
        _argv(begin="20260818", end="20260817")


def test_reject_span_over_limit_without_chunk():
    """反例: 跨度超上限且未分片"""
    with pytest.raises(params.ParamError, match="超过上限"):
        _argv(begin="20000101", end="20260101")


def test_reject_sql_injection_in_code():
    """反例(关键): 代码里夹带 SQL——即便全程 shell=False，也不该放进 argv"""
    with pytest.raises(params.ParamError, match="代码非法"):
        _argv(codes=["600519; DROP TABLE STOCK_DAILY"])


def test_reject_shell_metacharacters_in_code():
    """反例: 代码里夹带 shell 元字符"""
    with pytest.raises(params.ParamError, match="代码非法"):
        _argv(codes=["600519 && rm -rf /"])


def test_reject_bad_code_length():
    """反例: 位数不对"""
    with pytest.raises(params.ParamError, match="代码非法"):
        _argv(codes=["60051"])


def test_reject_bad_exchange_suffix():
    """反例: 不存在的交易所后缀"""
    with pytest.raises(params.ParamError, match="代码非法"):
        _argv(codes=["600519.HK"])


def test_reject_invalid_source_enum():
    """反例: 不在 choices 里的数据源"""
    with pytest.raises(params.ParamError, match="取值非法"):
        _argv(source="nosuch")


def test_reject_invalid_exchange_enum():
    """反例: 不在 choices 里的交易所"""
    with pytest.raises(params.ParamError, match="取值非法"):
        _argv(exchanges=["hk"])


def test_reject_non_bool_for_store_true():
    """反例: store_true 参数收到非布尔值"""
    with pytest.raises(params.ParamError, match="应为布尔值"):
        _argv(print_only="yes")


def test_reject_empty_list_for_nargs():
    """反例: 列表参数传空列表——会拼出一个孤零零的 -c"""
    with pytest.raises(params.ParamError, match="不能传空列表"):
        _argv(codes=[])


# ── 自省调用 ──────────────────────────────────────────────────────────────────

def _fake_run(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def test_fetch_schema_parses_json():
    """正例: 正常解析 describe_cli 的 JSON 输出"""
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    with patch.object(params.subprocess, "run", return_value=_fake_run(payload)), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        result = params.fetch_program_schema("import_daily", python=FAKE_PYTHON)
    assert result["program"] == "import_daily"
    assert "begin" in result["arguments"]


def test_fetch_schema_is_cached():
    """正例: 第二次取用缓存，不再起子进程"""
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    with patch.object(params.subprocess, "run", return_value=_fake_run(payload)) as run, \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        params.fetch_program_schema("import_daily", python=FAKE_PYTHON)
        params.fetch_program_schema("import_daily", python=FAKE_PYTHON)
    assert run.call_count == 1


def test_fetch_schema_refresh_bypasses_cache():
    """正例: refresh=True 强制重新自省"""
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    with patch.object(params.subprocess, "run", return_value=_fake_run(payload)) as run, \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        params.fetch_program_schema("import_daily", python=FAKE_PYTHON)
        params.fetch_program_schema("import_daily", python=FAKE_PYTHON, refresh=True)
    assert run.call_count == 2


def test_fetch_schema_never_uses_shell():
    """正例(安全): 必须以 argv 列表调用，且 shell=False"""
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    with patch.object(params.subprocess, "run", return_value=_fake_run(payload)) as run, \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        params.fetch_program_schema("import_daily", python=FAKE_PYTHON)
    cmd = run.call_args.args[0]
    assert isinstance(cmd, list)
    assert cmd == [FAKE_PYTHON, "-m", "tools.describe_cli", "import_daily"]
    assert run.call_args.kwargs["shell"] is False


def test_fetch_schema_nonzero_exit_raises():
    """反例: describe_cli 失败必须抛错，不得返回空 schema"""
    with patch.object(params.subprocess, "run",
                      return_value=_fake_run("", returncode=1, stderr="boom")), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        with pytest.raises(params.IntrospectionError, match="失败"):
            params.fetch_program_schema("import_daily", python=FAKE_PYTHON)


def test_fetch_schema_bad_json_raises():
    """反例: 输出不是 JSON"""
    with patch.object(params.subprocess, "run", return_value=_fake_run("not json")), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        with pytest.raises(params.IntrospectionError, match="不是合法 JSON"):
            params.fetch_program_schema("import_daily", python=FAKE_PYTHON)


def test_fetch_schema_missing_arguments_key_raises():
    """反例: JSON 合法但结构不对"""
    with patch.object(params.subprocess, "run", return_value=_fake_run('{"program": "x"}')), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        with pytest.raises(params.IntrospectionError, match="缺少 arguments"):
            params.fetch_program_schema("import_daily", python=FAKE_PYTHON)


def test_fetch_schema_timeout_raises():
    """反例: 自省本身卡住"""
    with patch.object(params.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("cmd", 60)), \
         patch.object(schema, "spring_dir", return_value="/fake/spring"):
        with pytest.raises(params.IntrospectionError, match="超时"):
            params.fetch_program_schema("import_daily", python=FAKE_PYTHON)


def test_fetch_schema_rejects_unknown_program_before_subprocess():
    """反例: 白名单校验发生在起子进程之前"""
    with patch.object(params.subprocess, "run") as run:
        with pytest.raises(params.ParamError):
            params.fetch_program_schema("nosuch", python=FAKE_PYTHON)
    run.assert_not_called()


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def test_span_days_inclusive():
    """正例: 跨度含首尾"""
    assert params.span_days("20260817", "20260817") == 1
    assert params.span_days("20260817", "20260818") == 2


# ── 分片（M4 断点续跑）────────────────────────────────────────────────────────
# 分片在卡死场景价值最大：单段卡死只损失一段，把「整晚白跑」降级为「丢一段」。

def test_split_none_returns_single_segment():
    """正例: 不分片时原样返回一段"""
    assert params.split_range("20260115", "20260420", schema.CHUNK_NONE) == [
        ("20260115", "20260420")]


def test_split_month_aligns_to_calendar_boundaries():
    """正例(核心): 按自然月切，首尾段被原区间裁剪

    对齐自然边界而非固定天数——人和模型补数时都按「某年某月」思考，
    段边界与之一致才好定位与重跑。
    """
    assert params.split_range("20260115", "20260420", schema.CHUNK_MONTH) == [
        ("20260115", "20260131"),
        ("20260201", "20260228"),
        ("20260301", "20260331"),
        ("20260401", "20260420"),
    ]


def test_split_month_handles_leap_february():
    """正例(边界): 闰年 2 月是 29 天"""
    segments = params.split_range("20240201", "20240301", schema.CHUNK_MONTH)
    assert segments[0] == ("20240201", "20240229")


def test_split_year_crosses_year_boundary():
    """正例: 按年切"""
    assert params.split_range("20251201", "20260201", schema.CHUNK_YEAR) == [
        ("20251201", "20251231"),
        ("20260101", "20260201"),
    ]


def test_split_single_day():
    """正例(边界): 单日无论怎么分片都只有一段"""
    for chunk in schema.CHUNK_CHOICES:
        assert params.split_range("20260817", "20260817", chunk) == [
            ("20260817", "20260817")]


def test_split_exact_month_range():
    """正例(边界): 恰好一整月不该被切成两段"""
    assert params.split_range("20260201", "20260228", schema.CHUNK_MONTH) == [
        ("20260201", "20260228")]


def test_split_segments_are_contiguous_and_complete():
    """正例(不变式): 分片必须无缝无重叠地覆盖原区间——漏一天就是漏数据"""
    begin, end = "20240115", "20260420"
    for chunk in (schema.CHUNK_MONTH, schema.CHUNK_YEAR):
        segments = params.split_range(begin, end, chunk)
        assert segments[0][0] == begin
        assert segments[-1][1] == end
        for (_, prev_end), (next_begin, _) in zip(segments, segments[1:]):
            gap = params.parse_date(next_begin, "b") - params.parse_date(prev_end, "e")
            assert gap.days == 1, f"段间不连续: {prev_end} → {next_begin}"


def test_split_rejects_unknown_chunk():
    """反例: 未知分片方式"""
    with pytest.raises(params.ParamError, match="chunk 取值非法"):
        params.split_range("20260101", "20260201", "week")


def test_split_rejects_reversed_range():
    """反例: 起始晚于结束"""
    with pytest.raises(params.ParamError, match="不能晚于"):
        params.split_range("20260201", "20260101", schema.CHUNK_MONTH)


def test_split_rejects_invalid_date():
    """反例: 非法日期"""
    with pytest.raises(params.ParamError, match="不是有效日期"):
        params.split_range("20261301", "20270101", schema.CHUNK_MONTH)


# ── check_daily argv 构造 ─────────────────────────────────────────────────────

def _check_argv(**kwargs) -> list[str]:
    return params.build_check_argv(kwargs, python=FAKE_PYTHON)


def test_check_argv_always_appends_json():
    """正例(关键): 本服务只消费机器可读输出，--json 必须恒定加上"""
    argv = _check_argv(begin="20260817", end="20260817")
    assert argv[:3] == [FAKE_PYTHON, "-m", "tools.check_daily"]
    assert argv[-1] == "--json"


def test_check_argv_renders_all_flags():
    """正例: 各参数正确映射到 flag"""
    argv = _check_argv(begin="20260817", end="20260818", codes=["600519.sh"],
                       exchanges=["SH"], include_index=True, forcerun=True,
                       json_max_detail=50)
    assert argv[3:] == ["-b", "20260817", "-e", "20260818",
                        "-c", "600519.SH", "-x", "sh", "-i", "-f",
                        "--json-max-detail", "50", "--json"]


def test_check_argv_rejects_injection():
    """反例(关键): 校验强度与 ETL 侧一致，不因为是只读工具就放松"""
    with pytest.raises(params.ParamError, match="代码非法"):
        _check_argv(begin="20260817", codes=["600519; DROP TABLE"])


def test_check_argv_rejects_unknown_param():
    """反例: 未知参数名"""
    with pytest.raises(params.ParamError, match="不接受参数"):
        _check_argv(begin="20260817", nosuch=1)


def test_check_argv_rejects_bad_exchange():
    """反例: 非法交易所"""
    with pytest.raises(params.ParamError, match="exchanges 取值非法"):
        _check_argv(begin="20260817", exchanges=["hk"])


def test_check_argv_allows_long_span():
    """正例: 完整性检查是只读的，不受 ETL 的跨度上限约束"""
    argv = _check_argv(begin="20000101", end="20260101")
    assert "-b" in argv
