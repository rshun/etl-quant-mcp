# 修改记录:
#   2026-08-19  Claude  新建：ETL 日志的反向读取、过滤与摘要
#   2026-09-13  Claude  补记：多行消息的续行会计入 unparsed_lines，不代表格式漂移
"""ETL 日志读取与摘要。

日志由 spring 的 `util/myutil.py:configure_etl_logging()` 单点定义，格式固定为：

    HH:MM:SS [logger.name] [LEVEL] message

两个必须知道的事实：

1. **时间戳只有时分秒，日期在文件名里**（`stockdailyYYYYMMDD.log`）。
   因此跨零点的运行（23:50 启动、00:10 结束）会把两段时间写进**同一个**文件，
   且时间戳会回绕。本模块计算间隔时按「负数即跨天」补 24 小时。
2. **一天一个文件，当天所有 ETL 程序共用**。所以摘要必须按模块（logger 名）
   分组，否则不同程序的日志会糊在一起。
3. **一条记录可能跨多行**。异常文本（如 DuckDB 的 `Catalog Error`）会带出若干
   续行，它们没有 `HH:MM:SS` 前缀，`parse_line` 一律返回 None 并计入
   `unparsed_lines`。所以 **`unparsed_lines` 非零不等于日志格式变了**——
   判断格式是否漂移，要看**带时间戳前缀的行**是否还能解析
   （见 `tests/test_integration.py::test_real_log_record_heads_all_parse`）。

这一层与 runner 的心跳互补：心跳只看得见本服务自己启动的任务，
看不到 cron 昨晚跑的那一次——那一次只能靠日志复盘。
"""
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import schema

LOG_PREFIX = "stockdaily"
LOG_GLOB = f"{LOG_PREFIX}*.log"
DEFAULT_MAX_BYTES = 200_000
DEFAULT_TAIL = 200

# HH:MM:SS [模块] [级别] 正文
LINE_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})\s+\[(?P<module>[^\]]+)\]\s+\[(?P<level>[A-Z]+)\]\s?(?P<message>.*)$"
)

# 批量采集正常收尾的标志，用来区分「跑完了」与「停在下载处」
COMPLETION_MARKERS = ("批量采集完成", "任务启动", "已成功写入数据库", "已执行每日数据补齐")

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class LogNotFound(FileNotFoundError):
    """日志文件不存在。明确报错，不抛裸异常。"""


def parse_line(line: str) -> dict | None:
    """解析单行日志。格式不匹配返回 None——日志格式变了不该让整个工具崩掉。"""
    match = LINE_RE.match(line.rstrip("\n"))
    if not match:
        return None
    return {
        "time": match.group("time"),
        "module": match.group("module"),
        "level": match.group("level"),
        "message": match.group("message"),
    }


def _seconds(hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 3600 + m * 60 + s


def elapsed_between(earlier: str, later: str) -> int:
    """两个 HH:MM:SS 之间的秒数；结果为负视为跨零点，补一天。"""
    delta = _seconds(later) - _seconds(earlier)
    return delta + 86400 if delta < 0 else delta


def resolve_date(date: str | None) -> str:
    """日期归一为 YYYYMMDD；None 表示当天。"""
    if date is None:
        return datetime.now().strftime("%Y%m%d")
    text = str(date).strip().replace("-", "")
    if not schema.DATE_RE.match(text):
        raise ValueError(f"日期格式非法: '{date}'，应为 YYYYMMDD")
    return text


def log_path(date: str | None = None, log_dir: Path | None = None) -> Path:
    directory = Path(log_dir) if log_dir else schema.spring_log_dir()
    return directory / f"{LOG_PREFIX}{resolve_date(date)}.log"


def list_logs(limit: int = 30, log_dir: Path | None = None) -> list[dict]:
    """列出已有日志文件，最新在前。"""
    directory = Path(log_dir) if log_dir else schema.spring_log_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob(LOG_GLOB):
        stem = path.stem[len(LOG_PREFIX):]
        stat = path.stat()
        records.append({
            "date": stem,
            "path": str(path),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    records.sort(key=lambda r: r["date"], reverse=True)
    return records[:limit]


def _read_tail_bytes(path: Path, max_bytes: int) -> tuple[list[str], bool]:
    """只读文件尾部 max_bytes，避免把超大日志灌进上下文。

    返回 (行列表, 是否发生了字节级截断)。截断时丢弃第一行——
    它多半是被从中间切开的半行。
    """
    size = path.stat().st_size
    truncated = size > max_bytes
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - max_bytes)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    return lines, truncated


def read_log(date: str | None = None, *, tail: int = DEFAULT_TAIL,
             level: str | None = None, module: str | None = None,
             keyword: str | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
             log_dir: Path | None = None) -> dict:
    """反向读取日志尾部并按条件过滤。

    level  精确匹配级别（ERROR / WARNING / INFO ...）
    module 子串匹配模块名（如 'import_daily' 可匹配 'etl.import_daily'）
    keyword 子串匹配整行
    """
    path = log_path(date, log_dir)
    if not path.exists():
        raise LogNotFound(f"日志文件不存在: {path}")

    lines, byte_truncated = _read_tail_bytes(path, max_bytes)
    total_read = len(lines)

    if level:
        wanted = level.upper()
        lines = [l for l in lines
                 if (p := parse_line(l)) is not None and p["level"] == wanted]
    if module:
        lines = [l for l in lines
                 if (p := parse_line(l)) is not None and module in p["module"]]
    if keyword:
        lines = [l for l in lines if keyword in l]

    matched = len(lines)
    selected = lines[-tail:] if tail and tail > 0 else lines

    return {
        "date": resolve_date(date),
        "path": str(path),
        "lines": selected,
        "matched_lines": matched,
        "scanned_lines": total_read,
        "truncated": byte_truncated or len(selected) < matched,
        "byte_truncated": byte_truncated,
        "max_bytes": max_bytes,
    }


def _progress_of(parsed: dict) -> dict | None:
    match = schema.PROGRESS_RE.search(parsed["message"])
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    return {
        "done": done,
        "total": total,
        "percent": round(done * 100.0 / total, 1) if total else None,
        "at": parsed["time"],
    }


def summarize_log(date: str | None = None, *,
                  max_bytes: int = DEFAULT_MAX_BYTES,
                  error_samples: int = 5,
                  log_dir: Path | None = None) -> dict:
    """当天日志摘要：级别计数、按模块分组的错误、以及**卡死线索**。

    卡死线索的判据是时间无关的，因此什么时候复盘都成立：
    **某个模块的最后一行如果是进度行，说明它停在下载途中就没了下文**——
    正常跑完会有「批量采集完成」，正常失败会有 ERROR。两者皆无而止于进度行，
    就是卡死后被 kill（或至今仍挂着）的典型形态。
    """
    path = log_path(date, log_dir)
    if not path.exists():
        raise LogNotFound(f"日志文件不存在: {path}")

    lines, byte_truncated = _read_tail_bytes(path, max_bytes)

    level_counts: Counter = Counter()
    module_levels: dict[str, Counter] = defaultdict(Counter)
    module_last: dict[str, dict] = {}
    module_last_progress: dict[str, dict] = {}
    module_max_gap: dict[str, int] = {}
    errors: list[dict] = []
    unparsed = 0
    first_at = last_at = None

    for line in lines:
        parsed = parse_line(line)
        if parsed is None:
            unparsed += 1
            continue

        module, level, at = parsed["module"], parsed["level"], parsed["time"]
        level_counts[level] += 1
        module_levels[module][level] += 1
        first_at = first_at or at
        last_at = at

        if level in ("ERROR", "CRITICAL", "WARNING"):
            errors.append({"time": at, "module": module,
                           "level": level, "message": parsed["message"]})

        progress = _progress_of(parsed)
        if progress is not None:
            previous = module_last_progress.get(module)
            if previous is not None:
                gap = elapsed_between(previous["at"], at)
                module_max_gap[module] = max(module_max_gap.get(module, 0), gap)
            module_last_progress[module] = progress

        module_last[module] = parsed

    modules = []
    stall_suspects = []
    for module, last in sorted(module_last.items()):
        last_progress = module_last_progress.get(module)
        ends_on_progress = _progress_of(last) is not None
        completed = any(marker in last["message"] for marker in COMPLETION_MARKERS)
        modules.append({
            "module": module,
            "levels": dict(module_levels[module]),
            "last_line_at": last["time"],
            "last_message": last["message"][:200],
            "last_progress": last_progress,
            "max_progress_gap_seconds": module_max_gap.get(module),
            "ends_on_progress_line": ends_on_progress,
            "ended_with_completion_marker": completed,
        })
        if ends_on_progress:
            stall_suspects.append({
                "module": module,
                "stopped_at": last["time"],
                "progress": last_progress,
                "reason": "该模块的最后一行是进度行——既无完成标志也无报错，"
                          "符合「停在下载处」的形态",
            })

    return {
        "date": resolve_date(date),
        "path": str(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "size_bytes": path.stat().st_size,
        "level_counts": dict(level_counts),
        "error_count": level_counts.get("ERROR", 0) + level_counts.get("CRITICAL", 0),
        "warning_count": level_counts.get("WARNING", 0),
        "first_line_at": first_at,
        "last_line_at": last_at,
        "span_seconds": (elapsed_between(first_at, last_at)
                         if first_at and last_at else None),
        "modules": modules,
        "recent_errors": errors[-error_samples:] if error_samples else [],
        "stall_suspects": stall_suspects,
        "unparsed_lines": unparsed,
        "truncated": byte_truncated,
    }
