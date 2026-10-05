# 修改记录:
#   2026-10-05  Claude  新建：spring 核对类异常明细 CSV 的列出与分页读取
"""异常明细读取。

spring 的核对工具（`tools.check_daily`、`tools.check_adjust`、`util/checker`）
把完整明细以 CSV 落到 `$SPRING_HOME/csv/`，JSON 输出里只带有限条数 + `csv_path`。
HTTP 部署下客户端与本服务分属不同用户，模型拿到 `csv_path` 也读不了——
本模块就是那条路径的只读出口。

三个必须知道的事实：

1. **只开放 `check_*.csv`**。同一目录里还有 `import_daily -p` 导出的逐股行情
   （`{symbol}_{tag}_{begin}_{end}.csv`），那是数据导出不是异常明细，不在此列。
   文件名白名单 + 父目录必须正好是 csv 目录，两道闸一起防路径穿越。
2. **spring 以 `utf-8-sig` 落盘**（带 BOM，方便 Excel）。按普通 utf-8 读会让
   BOM 混进首列表头。
3. **全市场明细可能上万行**，所以必须分页，且单页有硬上限。读取是流式的，
   内存里只留当前页。
"""
import csv
import re
from datetime import datetime
from pathlib import Path

import schema

DETAIL_GLOB = "check_*.csv"
DETAIL_NAME_RE = re.compile(r"^check_[A-Za-z0-9_.-]+\.csv$")
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


class DetailNotFound(FileNotFoundError):
    """明细文件不存在。明确报错，不抛裸异常。"""


def _directory(csv_dir: Path | None) -> Path:
    return Path(csv_dir) if csv_dir else schema.spring_csv_dir()


def resolve_detail(name: str, csv_dir: Path | None = None) -> Path:
    """把裸文件名或 check_data_gaps 返回的 csv_path 解析为 csv 目录内的路径。

    文件名必须符合 `check_*.csv`；传了目录部分时，其父目录必须正好是 csv 目录。
    任一不满足抛 ValueError——不能借这个出口读任意文件。
    """
    directory = _directory(csv_dir)
    text = str(name or "").strip()
    # 两种分隔符都按目录处理，避免 Windows 风格的 ..\\ 在 POSIX 上被当成文件名
    filename = re.split(r"[\\/]", text)[-1]
    if not DETAIL_NAME_RE.match(filename):
        raise ValueError(f"非法的明细文件名: '{name}'，只允许 csv 目录下的 check_*.csv")
    if filename != text:
        given_parent = Path(text).parent.resolve()
        if given_parent != directory.resolve():
            raise ValueError(f"明细文件必须位于 {directory}，拒绝读取: '{name}'")
    return directory / filename


def list_details(limit: int = 30, keyword: str | None = None,
                 csv_dir: Path | None = None) -> list[dict]:
    """列出明细文件，最新修改在前。keyword 按文件名子串过滤。"""
    directory = _directory(csv_dir)
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob(DETAIL_GLOB):
        if not path.is_file() or (keyword and keyword not in path.name):
            continue
        stat = path.stat()
        records.append({
            "name": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "_mtime": stat.st_mtime,
        })
    records.sort(key=lambda r: (r["_mtime"], r["name"]), reverse=True)
    for record in records:
        del record["_mtime"]
    return records[:limit]


def read_detail(name: str, *, offset: int = 0, limit: int = DEFAULT_LIMIT,
                keyword: str | None = None, csv_dir: Path | None = None) -> dict:
    """分页读取一个明细文件。

    keyword 按整行子串过滤（典型用法：传股票代码）；offset/limit 作用于过滤后的行。
    limit 超过 MAX_LIMIT 时按上限截，避免一次灌爆上下文。
    """
    if offset < 0:
        raise ValueError(f"offset 不能为负: {offset}")
    if limit <= 0:
        raise ValueError(f"limit 必须为正: {limit}")
    limit = min(limit, MAX_LIMIT)

    path = resolve_detail(name, csv_dir)
    if not path.is_file():
        raise DetailNotFound(f"明细文件不存在: {path}")

    header: list[str] = []
    rows: list[list[str]] = []
    total = matched = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        for row in reader:
            total += 1
            if keyword and keyword not in ",".join(row):
                continue
            if offset <= matched < offset + limit:
                rows.append(row)
            matched += 1

    end = offset + len(rows)
    has_more = end < matched
    return {
        "name": path.name,
        "path": str(path),
        "header": header,
        "rows": rows,
        "offset": offset,
        "limit": limit,
        "total_rows": total,
        "matched_rows": matched,
        "truncated": has_more,
        "next_offset": end if has_more else None,
    }
