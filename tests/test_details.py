# 修改记录:
#   2026-10-05  Claude  新建：异常明细 CSV 的列出、分页读取、过滤与越界防护
"""details.py 的正反例。

明细层最要紧的两件事：
  * **只读白名单内的文件**——`check_*.csv` 且必须在 csv 目录里，
    不能借它读任意文件，也不能读 import_daily -p 导出的行情 CSV；
  * **不能灌爆上下文**——全市场明细可能上万行，必须分页。
"""
import os

import pytest

import details as details_mod
from details import DetailNotFound


@pytest.fixture
def csv_dir(tmp_path):
    directory = tmp_path / "csv"
    directory.mkdir()
    return directory


def _write(csv_dir, name: str, rows: list[list[str]], *, mtime: float | None = None):
    path = csv_dir / name
    # spring 以 utf-8-sig 落盘（带 BOM，方便 Excel 打开）
    text = "\n".join(",".join(r) for r in rows) + "\n"
    path.write_text(text, encoding="utf-8-sig")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


MISSING_ROWS = [
    ["date", "code", "name"],
    ["2026-09-01", "600519.SH", "贵州茅台"],
    ["2026-09-01", "000681.SZ", "视觉中国"],
    ["2026-09-02", "600519.SH", "贵州茅台"],
]


# ── 列出 ──────────────────────────────────────────────────────────────────────

def test_list_details_newest_first(csv_dir):
    """正例: 按修改时间倒序列出"""
    _write(csv_dir, "check_stockdaily_missing_20260901_20260902.csv", MISSING_ROWS, mtime=1_000)
    _write(csv_dir, "check_adjust_invariant_20260901_20260930.csv", MISSING_ROWS, mtime=2_000)
    got = details_mod.list_details(csv_dir=csv_dir)
    assert [d["name"] for d in got] == [
        "check_adjust_invariant_20260901_20260930.csv",
        "check_stockdaily_missing_20260901_20260902.csv",
    ]
    assert got[0]["size_bytes"] > 0
    assert "modified_at" in got[0]


def test_list_details_excludes_non_check_files(csv_dir):
    """反例(安全): import_daily -p 导出的行情 CSV 不是异常明细，不列出"""
    _write(csv_dir, "600519_bstock_20260901_20260902.csv", MISSING_ROWS)
    _write(csv_dir, "check_isst_null_20260901_20260902.csv", MISSING_ROWS)
    (csv_dir / "check_notes.txt").write_text("x", encoding="utf-8")
    got = details_mod.list_details(csv_dir=csv_dir)
    assert [d["name"] for d in got] == ["check_isst_null_20260901_20260902.csv"]


def test_list_details_keyword_and_limit(csv_dir):
    """正例: 按文件名子串过滤，并受 limit 约束"""
    _write(csv_dir, "check_stockdaily_missing_20260901_20260902.csv", MISSING_ROWS, mtime=1_000)
    _write(csv_dir, "check_index_missing_20260901_20260902.csv", MISSING_ROWS, mtime=2_000)
    _write(csv_dir, "check_isst_null_20260901_20260902.csv", MISSING_ROWS, mtime=3_000)
    got = details_mod.list_details(keyword="missing", csv_dir=csv_dir)
    assert len(got) == 2
    got = details_mod.list_details(keyword="missing", limit=1, csv_dir=csv_dir)
    assert [d["name"] for d in got] == ["check_index_missing_20260901_20260902.csv"]


def test_list_details_missing_dir_returns_empty(tmp_path):
    """反例: csv 目录还没生成（从没跑过核对）时返回空列表，不报错"""
    assert details_mod.list_details(csv_dir=tmp_path / "nope") == []


# ── 读取 ──────────────────────────────────────────────────────────────────────

def test_read_detail_returns_header_and_rows(csv_dir):
    """正例: 表头单列返回，BOM 不混进首列名"""
    _write(csv_dir, "check_stockdaily_missing_20260901_20260902.csv", MISSING_ROWS)
    got = details_mod.read_detail("check_stockdaily_missing_20260901_20260902.csv",
                                  csv_dir=csv_dir)
    assert got["header"] == ["date", "code", "name"]
    assert got["rows"][0] == ["2026-09-01", "600519.SH", "贵州茅台"]
    assert got["total_rows"] == 3
    assert got["matched_rows"] == 3
    assert got["truncated"] is False


def test_read_detail_paginates(csv_dir):
    """正例: offset/limit 分页，截断时标记 truncated"""
    _write(csv_dir, "check_x_20260901_20260902.csv", MISSING_ROWS)
    got = details_mod.read_detail("check_x_20260901_20260902.csv",
                                  offset=1, limit=1, csv_dir=csv_dir)
    assert got["rows"] == [["2026-09-01", "000681.SZ", "视觉中国"]]
    assert got["offset"] == 1
    assert got["truncated"] is True
    assert got["next_offset"] == 2


def test_read_detail_last_page_has_no_next(csv_dir):
    """正例: 最后一页 next_offset 为 None"""
    _write(csv_dir, "check_x_20260901_20260902.csv", MISSING_ROWS)
    got = details_mod.read_detail("check_x_20260901_20260902.csv",
                                  offset=2, limit=10, csv_dir=csv_dir)
    assert len(got["rows"]) == 1
    assert got["truncated"] is False
    assert got["next_offset"] is None


def test_read_detail_keyword_filters_rows(csv_dir):
    """正例: keyword 按整行子串过滤（典型用法：按股票代码筛）"""
    _write(csv_dir, "check_x_20260901_20260902.csv", MISSING_ROWS)
    got = details_mod.read_detail("check_x_20260901_20260902.csv",
                                  keyword="600519", csv_dir=csv_dir)
    assert got["matched_rows"] == 2
    assert got["total_rows"] == 3
    assert all("600519.SH" in r for r in got["rows"])


def test_read_detail_handles_quoted_commas(csv_dir):
    """正例: 字段内含逗号时按 CSV 规则解析，不按逗号硬切"""
    path = csv_dir / "check_x_20260901_20260902.csv"
    path.write_text('code,reason\n600519.SH,"缺 pb, shares"\n', encoding="utf-8-sig")
    got = details_mod.read_detail(path.name, csv_dir=csv_dir)
    assert got["rows"] == [["600519.SH", "缺 pb, shares"]]


def test_read_detail_empty_file(csv_dir):
    """反例: 空文件给出空表头与零行，不抛异常"""
    (csv_dir / "check_x_20260901_20260902.csv").write_bytes(b"")
    got = details_mod.read_detail("check_x_20260901_20260902.csv", csv_dir=csv_dir)
    assert got["header"] == []
    assert got["rows"] == []
    assert got["total_rows"] == 0


def test_read_detail_accepts_csv_path_inside_dir(csv_dir):
    """正例: 可直接传 check_data_gaps 返回的 csv_path（绝对路径）"""
    path = _write(csv_dir, "check_x_20260901_20260902.csv", MISSING_ROWS)
    got = details_mod.read_detail(str(path), csv_dir=csv_dir)
    assert got["name"] == "check_x_20260901_20260902.csv"
    assert got["total_rows"] == 3


def test_read_detail_missing_file(csv_dir):
    """反例: 文件不存在抛 DetailNotFound"""
    with pytest.raises(DetailNotFound):
        details_mod.read_detail("check_nope_20260901_20260902.csv", csv_dir=csv_dir)


@pytest.mark.parametrize("name", [
    "600519_bstock_20260901_20260902.csv",   # 行情导出，不是明细
    "check_x.txt",                            # 扩展名不对
    "../config/config.yaml",                  # 路径穿越
    "..\\check_x.csv",                        # Windows 风格穿越
    "",
])
def test_read_detail_rejects_names_outside_whitelist(csv_dir, name):
    """反例(安全): 白名单外的文件名一律拒绝"""
    with pytest.raises(ValueError):
        details_mod.read_detail(name, csv_dir=csv_dir)


def test_read_detail_rejects_path_outside_dir(csv_dir, tmp_path):
    """反例(安全): 绝对路径即便文件名合规，不在 csv 目录下也拒绝"""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    path = _write(outside, "check_x_20260901_20260902.csv", MISSING_ROWS)
    with pytest.raises(ValueError):
        details_mod.read_detail(str(path), csv_dir=csv_dir)


@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"limit": 0}, {"limit": -5}])
def test_read_detail_rejects_bad_paging(csv_dir, kwargs):
    """反例: 分页参数非法"""
    _write(csv_dir, "check_x_20260901_20260902.csv", MISSING_ROWS)
    with pytest.raises(ValueError):
        details_mod.read_detail("check_x_20260901_20260902.csv", csv_dir=csv_dir, **kwargs)


def test_read_detail_caps_limit(csv_dir):
    """正例: limit 超上限时按上限截，避免一次灌爆上下文"""
    rows = [["code"]] + [[f"{i:06d}.SZ"] for i in range(details_mod.MAX_LIMIT + 10)]
    _write(csv_dir, "check_x_20260901_20260902.csv", rows)
    got = details_mod.read_detail("check_x_20260901_20260902.csv",
                                  limit=details_mod.MAX_LIMIT + 10, csv_dir=csv_dir)
    assert len(got["rows"]) == details_mod.MAX_LIMIT
    assert got["limit"] == details_mod.MAX_LIMIT
    assert got["truncated"] is True
