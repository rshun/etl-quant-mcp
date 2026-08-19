# 修改记录:
#   2026-08-19  Claude  新建：参数 schema 夹具与假子进程脚本工厂
"""测试夹具。

本仓库的单元测试**不需要 spring 在场**：参数 schema 用固化夹具，
子进程用现造的 python 小脚本。需要真实 spring 的用例一律标 integration。
"""
import pytest

# import_daily 的自省结果快照（结构与 tools/describe_cli.py 的输出一致）。
# 日期类默认值故意写成固定值：真实输出里它是「自省当天」，逐日变化，不能直接入夹具。
IMPORT_DAILY_SCHEMA = {
    "program": "import_daily",
    "module": "etl.import_daily",
    "description": "A股历史行情数据入库工具 (支持多源、多代码、指定日期)",
    "arguments": {
        "begin": {
            "flags": ["-b", "--begin"], "type": "str", "action": "store",
            "nargs": None, "default": "20260819", "choices": None,
            "required": False, "help": "指定交易日期 (格式: YYYYMMDD)，默认为当天",
        },
        "end": {
            "flags": ["-e", "--end"], "type": "str", "action": "store",
            "nargs": None, "default": "20260819", "choices": None,
            "required": False, "help": "指定交易日期 (格式: YYYYMMDD)，默认为当天",
        },
        "codes": {
            "flags": ["-c", "--codes"], "type": None, "action": "store",
            "nargs": "+", "default": None, "choices": None,
            "required": False, "help": "指定股票代码列表",
        },
        "exchanges": {
            "flags": ["-x", "--exchanges"], "type": "str.lower", "action": "store",
            "nargs": "+", "default": ["all"], "choices": ["sh", "sz", "bj", "all"],
            "required": False, "help": "指定交易所范围",
        },
        "source": {
            "flags": ["-s", "--source"], "type": "str", "action": "store",
            "nargs": None, "default": "bstock", "choices": ["lday", "bstock", "tdx"],
            "required": False, "help": "指定数据源类型",
        },
        "print_only": {
            "flags": ["-p", "--print-only"], "type": None, "action": "store_true",
            "nargs": 0, "default": False, "choices": None,
            "required": False, "help": "仅输出到屏幕，不写入数据库",
        },
    },
}

FAKE_PYTHON = "/usr/bin/python3"


@pytest.fixture
def import_daily_schema() -> dict:
    return IMPORT_DAILY_SCHEMA


@pytest.fixture(autouse=True)
def _clear_params_cache():
    """每个用例都从干净的自省缓存开始，避免用例间串味。"""
    import params
    params.clear_cache()
    yield
    params.clear_cache()
