# 修改记录:
#   2026-08-19  Claude  新建：自省 spring 的 argparse 定义并据此构造/校验 argv
"""参数自省与 argv 构造。

参数 schema 不写死在本仓库，而是运行时调用 spring 的 `tools.describe_cli` 取回(ADR-4)：
spring 增删改参数后本服务零改动，`help` 文本也原样透传给模型(契约 C4)。

安全边界(文档 十一)：
  * 只接受 schema.PROGRAMS 白名单内的程序，不提供任意命令执行；
  * 一律构造 argv 列表交给 subprocess，**绝不拼 shell 字符串**；
  * 日期、代码、枚举逐项校验，未在自省结果中出现的参数名一律拒绝。

两个默认值陷阱(自省结果本身看不出来，调用方需知道)：
  1. adjust/import_daily/fetch_index 的 -b/-e 默认值是**自省当天**，逐日变化，
     做快照比对前须归一化；
  2. fill_volratio/update_limit/fill_shares 的 -b/-e 在 argparse 层默认为 null，
     真实默认(T-1/今天)是 parse 之后才套用的，语义只在 help 文本里。
"""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import schema

DESCRIBE_TIMEOUT = 60

_SCHEMA_CACHE: dict[str, dict] = {}


class ParamError(ValueError):
    """参数非法。消息直接面向模型，应说清哪个参数、为什么被拒。"""


class IntrospectionError(RuntimeError):
    """调用 describe_cli 失败。"""


# ---------------------------------------------------------------- 自省

def clear_cache() -> None:
    _SCHEMA_CACHE.clear()


def fetch_program_schema(program: str, *, refresh: bool = False,
                         python: str | Path | None = None) -> dict:
    """取回单个程序的参数 schema，结果缓存在进程内。

    缓存是安全的：spring 改了 CLI 需要重启本服务，而这本来就是部署动作。
    需要强制刷新时传 refresh=True。
    """
    require_known_program(program)
    if not refresh and program in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[program]

    interpreter = str(python) if python else str(schema.spring_python())
    cmd = [interpreter, "-m", schema.DESCRIBE_MODULE, program]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(schema.spring_dir()),
            capture_output=True,
            text=True,
            timeout=DESCRIBE_TIMEOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise IntrospectionError(f"自省 '{program}' 超时({DESCRIBE_TIMEOUT}s)") from e
    except OSError as e:
        raise IntrospectionError(f"无法启动自省进程: {e}") from e

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise IntrospectionError(
            f"自省 '{program}' 失败(exit={proc.returncode}): {detail[:500]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise IntrospectionError(f"自省 '{program}' 的输出不是合法 JSON: {e}") from e

    if not isinstance(payload, dict) or "arguments" not in payload:
        raise IntrospectionError(f"自省 '{program}' 的输出缺少 arguments 字段")

    _SCHEMA_CACHE[program] = payload
    return payload


def require_known_program(program: str) -> str:
    """白名单校验：只有注册表里的程序能被启动。"""
    if program not in schema.PROGRAMS:
        known = ", ".join(sorted(schema.PROGRAMS))
        raise ParamError(f"未知程序 '{program}'；可用: {known}")
    return schema.PROGRAMS[program]


# ---------------------------------------------------------------- 单项校验

def parse_date(value: Any, field: str) -> datetime:
    """日期必须是 YYYYMMDD 且真实存在（20261301 这类要被拒）。"""
    text = str(value).strip()
    if not schema.DATE_RE.match(text):
        raise ParamError(f"{field} 格式非法: '{text}'，应为 8 位数字 YYYYMMDD")
    try:
        return datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise ParamError(f"{field} 不是有效日期: '{text}'") from None


def normalize_code(value: Any) -> str:
    """股票/指数代码；交易所后缀统一大写。"""
    text = str(value).strip()
    if not schema.CODE_RE.match(text):
        raise ParamError(
            f"代码非法: '{text}'，应为 6 位数字，可带 .SH/.SZ/.BJ 后缀"
        )
    if "." in text:
        number, exchange = text.split(".", 1)
        return f"{number}.{exchange.upper()}"
    return text


def _coerce_choice(value: Any, spec: dict, field: str) -> str:
    """按自省结果里的 type 归一化后再比对 choices。

    exchanges 的 argparse type 是 str.lower、choices 是小写，
    因此必须先归一化再校验，否则用户传 'SH' 会被误判为非法。
    """
    text = str(value).strip()
    if spec.get("type") == "str.lower":
        text = text.lower()
    choices = spec.get("choices")
    if choices is not None and text not in choices:
        raise ParamError(f"{field} 取值非法: '{value}'；可选: {', '.join(map(str, choices))}")
    return text


# ---------------------------------------------------------------- argv 构造

def build_argv(program: str, params: dict[str, Any] | None = None, *,
               python: str | Path | None = None,
               allow_long_span: bool = False,
               program_schema: dict | None = None) -> list[str]:
    """把参数字典构造成完整 argv。

    Parameters
    ----------
    params
        键必须是自省结果中的 dest 名(begin/end/codes/exchanges/source/...)，
        未出现在自省结果中的键一律拒绝。值为 None 表示「不传该参数」。
    allow_long_span
        跨度超过 MAX_SPAN_DAYS 时是否放行；调用方指定了 chunk 分片才应传 True。
    program_schema
        允许注入自省结果，便于测试与批量构造时复用。

    Returns
    -------
    完整 argv，形如 [python, "-u", "-m", "etl.import_daily", "-b", "20260817", ...]。
    `-u` 与调用方设置的 PYTHONUNBUFFERED 共同保证行级实时输出——
    少了它，pipe 的块缓冲会让正常运行被误判为卡死(文档 5.3)。
    """
    module = require_known_program(program)
    params = dict(params or {})
    spec_map = (program_schema or fetch_program_schema(program, python=python))["arguments"]

    unknown = [k for k in params if k not in spec_map]
    if unknown:
        raise ParamError(
            f"程序 '{program}' 不接受参数: {', '.join(sorted(unknown))}；"
            f"可用: {', '.join(sorted(spec_map))}"
        )

    _validate_date_range(params, allow_long_span=allow_long_span)

    # 先把所有参数校验并渲染完，**再**去解析解释器路径。
    # 顺序很重要：环境没配好不该盖过「参数写错了」这个更具体的错误，
    # 否则模型收到的是「SPRING_PYTHON 未设置」，会往完全错误的方向排查。
    # 按自省结果的声明顺序拼接，保证同样的参数得到同样的 argv(便于比对与复现)。
    rendered: list[str] = []
    for dest, spec in spec_map.items():
        if dest not in params:
            continue
        value = params[dest]
        if value is None:
            continue
        rendered.extend(_render_argument(dest, value, spec))

    interpreter = str(python) if python else str(schema.spring_python())
    return [interpreter, "-u", "-m", module, *rendered]


def _validate_date_range(params: dict[str, Any], *, allow_long_span: bool) -> None:
    begin_raw = params.get("begin")
    end_raw = params.get("end")
    begin = parse_date(begin_raw, "begin") if begin_raw is not None else None
    end = parse_date(end_raw, "end") if end_raw is not None else None

    if begin is None or end is None:
        return

    if begin > end:
        raise ParamError(
            f"begin 不能晚于 end: begin={begin:%Y%m%d}, end={end:%Y%m%d}"
        )

    span_days = (end - begin).days + 1
    if span_days > schema.MAX_SPAN_DAYS and not allow_long_span:
        raise ParamError(
            f"日期跨度 {span_days} 天超过上限 {schema.MAX_SPAN_DAYS} 天；"
            f"请指定 chunk 分片后重试"
        )


def _render_argument(dest: str, value: Any, spec: dict) -> list[str]:
    flags = spec.get("flags") or []
    if not flags:
        raise ParamError(f"参数 '{dest}' 没有可用的命令行选项")
    flag = flags[0]
    action = spec.get("action")

    if action in ("store_true", "store_false"):
        if not isinstance(value, bool):
            raise ParamError(f"{dest} 应为布尔值，收到 {type(value).__name__}")
        return [flag] if value else []

    if spec.get("nargs") == "+":
        values = value if isinstance(value, (list, tuple)) else [value]
        if not values:
            raise ParamError(f"{dest} 为列表参数，不能传空列表")
        rendered = [_render_scalar(dest, v, spec) for v in values]
        return [flag, *rendered]

    return [flag, _render_scalar(dest, value, spec)]


def _render_scalar(dest: str, value: Any, spec: dict) -> str:
    if dest == "codes":
        return normalize_code(value)
    if dest in ("begin", "end"):
        return f"{parse_date(value, dest):%Y%m%d}"
    if spec.get("choices") is not None:
        return _coerce_choice(value, spec, dest)
    text = str(value).strip()
    if not text:
        raise ParamError(f"{dest} 不能为空")
    return text


def span_days(begin: Any, end: Any) -> int:
    """给定日期区间的天数(含首尾)，供调用方挑选 stall_timeout 档位。"""
    return (parse_date(end, "end") - parse_date(begin, "begin")).days + 1
