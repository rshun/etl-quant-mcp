# 修改记录:
#   2026-08-19  Claude  新建：日志解析、过滤、摘要与卡死线索的正反例
"""logs.py 的正反例。

日志层最要紧的两件事：
  * **不能崩**——格式变了、文件没了、日志超大，都得给出明确结果而非裸异常；
  * **卡死线索要准**——判据必须与查看时间无关，隔几天复盘同样成立。
"""
from datetime import datetime

import pytest

import logs as logs_mod
from logs import LogNotFound, elapsed_between, parse_line

# 一次「正常跑完」的日志：有完成标志
COMPLETED = """\
01:00:00 [etl.import_daily] [INFO] 获取股票交易明细数据任务启动
01:00:01 [etl.datasource.bstock] [INFO] [baostock插件] 开始获取交易明细数据，共计 300 只股票...
01:00:30 [etl.datasource.bstock] [INFO]    已处理: 100/300
01:01:00 [etl.datasource.bstock] [INFO]    已处理: 200/300
01:01:30 [etl.datasource.bstock] [INFO]    已处理: 300/300
01:01:31 [etl.datasource.bstock] [INFO] 批量采集完成，成功获取 300 条记录
"""

# 一次「停在下载处」的日志：止于进度行，既无完成标志也无报错
STALLED = """\
02:00:00 [etl.adjust] [INFO] 获取股票复权因子任务启动
02:00:01 [etl.datasource.bstock] [INFO] [baostock插件] 开始获取复权因子，共计 5400 只股票...
02:00:40 [etl.datasource.bstock] [INFO]    已处理: 100/5400
02:01:20 [etl.datasource.bstock] [INFO]    已处理: 200/5400
02:09:00 [etl.datasource.bstock] [INFO]    已处理: 300/5400
"""

# 一次「正常失败」的日志：止于 ERROR
FAILED = """\
03:00:00 [etl.import_daily] [INFO] 获取股票交易明细数据任务启动
03:00:01 [etl.util.validators] [ERROR] 错误: begin==end - 提示: 交易日历中无 2027-01-05 的记录
"""


@pytest.fixture
def log_dir(tmp_path):
    return tmp_path


def _write(log_dir, date: str, content: str):
    path = log_dir / f"stockdaily{date}.log"
    path.write_text(content, encoding="utf-8")
    return path


# ── 单行解析 ──────────────────────────────────────────────────────────────────

def test_parse_line_extracts_fields():
    """正例: 解析出时间、模块、级别、正文"""
    got = parse_line("21:32:08 [etl.util.validators] [ERROR] 错误: 日期非法")
    assert got == {"time": "21:32:08", "module": "etl.util.validators",
                   "level": "ERROR", "message": "错误: 日期非法"}


def test_parse_line_keeps_leading_spaces_of_progress():
    """正例: 进度行正文前有缩进，不能被吃掉——它是识别进度行的特征"""
    got = parse_line("01:00:30 [etl.datasource.bstock] [INFO]    已处理: 100/300")
    assert "已处理: 100/300" in got["message"]


def test_parse_line_returns_none_on_mismatch():
    """反例(关键): 格式不匹配返回 None，不抛异常——日志格式变了不该让工具崩掉"""
    assert parse_line("这不是一条标准日志") is None
    assert parse_line("") is None
    assert parse_line("21:32:08 缺少方括号") is None


def test_elapsed_handles_midnight_rollover():
    """反例(边界): 跨零点的时间戳会回绕，间隔不能算成负数"""
    assert elapsed_between("01:00:00", "01:00:30") == 30
    assert elapsed_between("23:59:50", "00:00:10") == 20     # 跨天


# ── 列表 ──────────────────────────────────────────────────────────────────────

def test_list_logs_newest_first(log_dir):
    """正例: 最新日期在前"""
    for d in ("20260817", "20260819", "20260818"):
        _write(log_dir, d, COMPLETED)
    dates = [r["date"] for r in logs_mod.list_logs(log_dir=log_dir)]
    assert dates == ["20260819", "20260818", "20260817"]


def test_list_logs_respects_limit(log_dir):
    """正例: 数量上限"""
    for d in ("20260817", "20260818", "20260819"):
        _write(log_dir, d, COMPLETED)
    assert len(logs_mod.list_logs(limit=2, log_dir=log_dir)) == 2


def test_list_logs_on_missing_dir_returns_empty(log_dir):
    """反例: 目录不存在返回空列表，不抛异常"""
    assert logs_mod.list_logs(log_dir=log_dir / "nope") == []


def test_list_logs_ignores_unrelated_files(log_dir):
    """反例: 非 stockdaily 前缀的文件不该混进来"""
    _write(log_dir, "20260819", COMPLETED)
    (log_dir / "other.log").write_text("x", encoding="utf-8")
    assert [r["date"] for r in logs_mod.list_logs(log_dir=log_dir)] == ["20260819"]


# ── 读取与过滤 ────────────────────────────────────────────────────────────────

def test_read_log_tail(log_dir):
    """正例: 只取尾部若干行"""
    _write(log_dir, "20260819", COMPLETED)
    result = logs_mod.read_log("20260819", tail=2, log_dir=log_dir)
    assert len(result["lines"]) == 2
    assert "批量采集完成" in result["lines"][-1]


def test_read_log_filter_by_level(log_dir):
    """正例: 按级别精确过滤"""
    _write(log_dir, "20260819", COMPLETED + FAILED)
    result = logs_mod.read_log("20260819", level="ERROR", log_dir=log_dir)
    assert result["matched_lines"] == 1
    assert "[ERROR]" in result["lines"][0]


def test_read_log_filter_by_module_substring(log_dir):
    """正例: 模块名子串匹配，'import_daily' 应能匹配 'etl.import_daily'"""
    _write(log_dir, "20260819", COMPLETED + FAILED)
    result = logs_mod.read_log("20260819", module="import_daily", log_dir=log_dir)
    assert result["matched_lines"] == 2
    assert all("import_daily" in l for l in result["lines"])


def test_read_log_filter_by_keyword(log_dir):
    """正例: 关键字匹配整行"""
    _write(log_dir, "20260819", COMPLETED)
    result = logs_mod.read_log("20260819", keyword="已处理", log_dir=log_dir)
    assert result["matched_lines"] == 3


def test_read_log_accepts_dashed_date(log_dir):
    """正例: 带横线的日期也接受"""
    _write(log_dir, "20260819", COMPLETED)
    assert logs_mod.read_log("2026-08-19", log_dir=log_dir)["date"] == "20260819"


def test_read_log_missing_file_raises_clear_error(log_dir):
    """反例(关键): 文件不存在要明确报错，不抛裸异常"""
    with pytest.raises(LogNotFound, match="日志文件不存在"):
        logs_mod.read_log("20990101", log_dir=log_dir)


def test_read_log_invalid_date_rejected(log_dir):
    """反例: 日期格式非法"""
    with pytest.raises(ValueError, match="日期格式非法"):
        logs_mod.read_log("2026-13", log_dir=log_dir)


def test_read_log_truncates_huge_file(log_dir):
    """反例(关键): 超大日志受 max_bytes 截断并标记 truncated，不灌爆上下文"""
    big = "".join(f"01:00:{i%60:02d} [etl.x] [INFO] 第 {i} 行内容填充填充填充\n"
                  for i in range(5000))
    _write(log_dir, "20260819", big)
    result = logs_mod.read_log("20260819", tail=10000, max_bytes=2000, log_dir=log_dir)
    assert result["byte_truncated"] is True
    assert result["truncated"] is True
    assert result["scanned_lines"] < 5000


def test_read_log_drops_partial_first_line_when_truncated(log_dir):
    """反例: 字节截断会切开某一行，那半行必须丢弃而不是当成正常行"""
    big = "".join(f"01:00:00 [etl.x] [INFO] 行{i:05d}\n" for i in range(500))
    _write(log_dir, "20260819", big)
    result = logs_mod.read_log("20260819", tail=10000, max_bytes=1000, log_dir=log_dir)
    assert all(parse_line(l) is not None for l in result["lines"]), \
        "截断处的半行没有被丢弃"


def test_read_log_unparsable_lines_do_not_break_filtering(log_dir):
    """反例: 混入非标准行时，过滤仍应正常工作"""
    _write(log_dir, "20260819", COMPLETED + "这是一行垃圾\n" + FAILED)
    result = logs_mod.read_log("20260819", level="ERROR", log_dir=log_dir)
    assert result["matched_lines"] == 1


# ── 摘要 ──────────────────────────────────────────────────────────────────────

def test_summarize_counts_levels(log_dir):
    """正例: 级别计数"""
    _write(log_dir, "20260819", COMPLETED + FAILED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    assert s["error_count"] == 1
    assert s["level_counts"]["INFO"] == 7


def test_summarize_groups_by_module(log_dir):
    """正例: 按模块分组——一天一个文件，多个程序共用，不分组会糊成一团"""
    _write(log_dir, "20260819", COMPLETED + FAILED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    names = {m["module"] for m in s["modules"]}
    assert names == {"etl.import_daily", "etl.datasource.bstock", "etl.util.validators"}


def test_summarize_reports_last_progress(log_dir):
    """正例(核心): 报出最后一条进度行及其时间戳"""
    _write(log_dir, "20260819", STALLED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    bstock = next(m for m in s["modules"] if m["module"] == "etl.datasource.bstock")
    assert bstock["last_progress"] == {"done": 300, "total": 5400,
                                       "percent": 5.6, "at": "02:09:00"}


def test_summarize_flags_stall_when_log_ends_on_progress(log_dir):
    """正例(核心): 止于进度行 → 判为卡死嫌疑"""
    _write(log_dir, "20260819", STALLED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    suspects = {x["module"] for x in s["stall_suspects"]}
    assert "etl.datasource.bstock" in suspects


def test_summarize_no_stall_when_completed(log_dir):
    """反例(关键): 正常跑完不得误报卡死"""
    _write(log_dir, "20260819", COMPLETED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    assert s["stall_suspects"] == []


def test_summarize_no_stall_when_failed_with_error(log_dir):
    """反例(关键): 正常失败（止于 ERROR）也不是卡死，两者处置方式不同"""
    _write(log_dir, "20260819", FAILED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    assert s["stall_suspects"] == []


def test_summarize_reports_max_progress_gap(log_dir):
    """正例: 报出进度行之间的最大间隔——能看出中途卡过又恢复的情况"""
    _write(log_dir, "20260819", STALLED)
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    bstock = next(m for m in s["modules"] if m["module"] == "etl.datasource.bstock")
    assert bstock["max_progress_gap_seconds"] == 460   # 02:01:20 → 02:09:00


def test_summarize_span_across_midnight(log_dir):
    """反例(边界): 跨零点的运行，时间跨度不能算成负数"""
    _write(log_dir, "20260819",
           "23:50:00 [etl.import_daily] [INFO] 任务启动\n"
           "00:10:00 [etl.import_daily] [INFO] 批量采集完成\n")
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    assert s["span_seconds"] == 1200


def test_summarize_counts_unparsed_lines(log_dir):
    """反例: 非标准行被计数而非静默丢弃，格式漂移能被发现"""
    _write(log_dir, "20260819", COMPLETED + "垃圾行1\n垃圾行2\n")
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    assert s["unparsed_lines"] == 2


def test_summarize_missing_file_raises_clear_error(log_dir):
    """反例: 文件不存在要明确报错"""
    with pytest.raises(LogNotFound, match="日志文件不存在"):
        logs_mod.summarize_log("20990101", log_dir=log_dir)


def test_summarize_empty_file_does_not_crash(log_dir):
    """反例(边界): 空日志不该崩"""
    _write(log_dir, "20260819", "")
    s = logs_mod.summarize_log("20260819", log_dir=log_dir)
    assert s["level_counts"] == {}
    assert s["modules"] == []
    assert s["span_seconds"] is None
