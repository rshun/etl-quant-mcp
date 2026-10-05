# 修改记录:
#   2026-08-19  Claude  新建：程序注册表、退出码契约、状态机枚举与环境变量出口，
#                       作为本服务与 spring 之间跨仓契约的单一出处
#   2026-08-19  Claude  新增分片枚举、可重试状态集与 check_daily 的 JSON 契约
#   2026-09-12  Claude  跟进 spring：注册 fill_turnover(spring 于 2026-09-10 纳入契约)，
#                       并入 FILL_TARGETS 末位
#   2026-10-04  Claude  跟进 spring 安装包部署：新增 SPRING_BIN_DIR / SPRING_HOME 模式，
#                       直接调用 spring-* 命令；源码模式行为不变
#   2026-10-04  Claude  去掉 SPRING_DIR：cwd 统一为 SPRING_HOME(必填)，
#                       启动方式由 SPRING_BIN_DIR / SPRING_PYTHON 二选一；残留 SPRING_DIR 报错
"""与 spring 的跨仓契约(single source of truth)。

本服务对 ETL 的内部逻辑「零知识」(ADR-5)，只需要三件事：
  1. 怎么启动 —— PROGRAMS 注册表 + tools.describe_cli 自省(见 params.py)
  2. 是否健康 —— 退出码(EXIT_CODE_STATUS) + 心跳(见 runner.py)
  3. 结果在哪看 —— 日志文件(见 logs.py)

PROGRAMS 同时是**安全白名单**：只有在此声明的模块能被启动，
本服务不提供任意命令执行。
"""
import os
import re
from pathlib import Path

# ---- MCP 服务标识 ----
SERVER_NAME = "quant-etl"

# ---- 程序注册表(白名单) ----
# key 既是 describe_cli 的程序名，也是 MCP 侧的程序标识；module 是实际执行的模块路径。
PROGRAMS: dict[str, str] = {
    "adjust":        "etl.adjust",
    "import_daily":  "etl.import_daily",
    "fetch_index":   "etl.fetch_index",
    "fill_volratio": "etl.fill_volratio",
    "update_limit":  "etl.update_limit",
    "fill_shares":   "etl.fill_shares",
    "fill_turnover": "etl.fill_turnover",
}

# 四个补齐类程序日常一起补，Tool 层合并为 etl_fill_indicators，按此顺序串行执行
# (见文档 6.1)。顺序不是随意排的：fill_turnover 由 STOCK_DAILY.volume 除以
# DAILY_BASIC.float_shares 算出，而流通股本正是 fill_shares 回填的，
# 故它必须排在 fill_shares 之后(spring `config/pipeline.yaml` 同样这么声明)。
FILL_TARGETS: tuple[str, ...] = ("fill_volratio", "update_limit",
                                 "fill_shares", "fill_turnover")

# 前三个 target 的参数面完全一致，fill_turnover 多一个 -o/--overwrite
# (默认只补空行，-o 覆盖重算)。合并 Tool 的 overwrite 只对它有意义——
# 传给其他 target 会被 build_argv 以「不接受该参数」拒绝。
FILL_OVERWRITE_TARGET = "fill_turnover"

# spring 的自省出口(ADR-4/7)
DESCRIBE_MODULE = "tools.describe_cli"

# 数据完整性检查工具。它是**只读**的，不占写锁队列，由 params/server 直接内联调用。
# 注意它的退出码是另一套语义(0=完整 / 1=有缺失 / 2=检查出错)，与下面的 EXIT_CODE_STATUS
# **不通用**——调用方一律以其 JSON 输出的 status 字段为准，不解读它的退出码。
CHECK_MODULE = "tools.check_daily"

CHECK_STATUS_COMPLETE = "complete"
CHECK_STATUS_GAPS_FOUND = "gaps_found"
CHECK_STATUS_ERROR = "error"

# ---- 任务状态机(文档 5.1) ----
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_STALLED = "stalled"            # 进程还活着，但静默超过 stall_timeout；非终态
STATUS_KILLED_STALLED = "killed_stalled"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({
    STATUS_SUCCEEDED, STATUS_PARTIAL, STATUS_FAILED,
    STATUS_KILLED_STALLED, STATUS_CANCELLED,
})

# ---- 退出码契约(C1) ----
# 注意 `2` 是 argparse 内建的用法错误码(非法枚举值、未知选项等)，Python 标准库写死，
# 不能解读为「部分成功」——那会把「参数传错了」读成「大体成功」。部分成功码是 `3`。
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3

EXIT_CODE_STATUS: dict[int, str] = {
    EXIT_OK:      STATUS_SUCCEEDED,
    EXIT_FAILED:  STATUS_FAILED,
    EXIT_USAGE:   STATUS_FAILED,
    EXIT_PARTIAL: STATUS_PARTIAL,
}

EXIT_CODE_HINT: dict[int, str] = {
    EXIT_OK:      "成功",
    EXIT_FAILED:  "执行失败，详见日志",
    EXIT_USAGE:   "命令行用法错误(非法参数值或未知选项)，由 argparse 产出",
    EXIT_PARTIAL: "部分成功",
}


def status_from_exit_code(code: int) -> str:
    """把子进程退出码翻译成状态；未知退出码一律按失败处理，绝不乐观解读。"""
    return EXIT_CODE_STATUS.get(code, STATUS_FAILED)


def describe_exit_code(code: int) -> str:
    return EXIT_CODE_HINT.get(code, f"未知退出码 {code}，按失败处理")


# ---- 停顿检测阈值(文档 5.4) ----
# spring 的进度日志按「每 100 只或满 30 秒」双条件触发(契约 C1b)，
# 因此 90 秒静默已足够判定异常；长区间任务单只耗时更长，放宽一档。
SHORT_SPAN_DAYS = 7
STALL_TIMEOUT_SHORT = 90
STALL_TIMEOUT_LONG = 600
MAX_RUNTIME_DEFAULT = 7200

# ---- 分片与重试(文档 6.1 的 MCP 层附加参数) ----
CHUNK_NONE = "none"
CHUNK_MONTH = "month"
CHUNK_YEAR = "year"
CHUNK_CHOICES: tuple[str, ...] = (CHUNK_NONE, CHUNK_MONTH, CHUNK_YEAR)

# 哪些终态值得自动重试。
# 不含 cancelled——那是人的决定，自动重跑会推翻它；
# 不含 partial——部分成功重跑整段是浪费，应由调用方按缺口定向补。
RETRYABLE_STATUSES = frozenset({STATUS_FAILED, STATUS_KILLED_STALLED})
MAX_RETRIES = 5

# ---- 参数安全约束(文档 十一) ----
MAX_SPAN_DAYS = 3650
EXCHANGES: tuple[str, ...] = ("SH", "SZ", "BJ")

DATE_RE = re.compile(r"^\d{8}$")
CODE_RE = re.compile(rf"^\d{{6}}(\.({'|'.join(EXCHANGES)}))?$", re.IGNORECASE)
# 进度行形如「   已处理: 3400/5400」
PROGRESS_RE = re.compile(r"已处理:\s*(\d+)\s*/\s*(\d+)")


def default_stall_timeout(span_days: int) -> int:
    """按任务跨度分档给出默认停顿阈值。"""
    return STALL_TIMEOUT_SHORT if span_days <= SHORT_SPAN_DAYS else STALL_TIMEOUT_LONG


# ---- 环境变量出口(ADR-7) ----
# 一律做成函数而非模块级常量：import 本模块不应因环境未配置而失败，
# 否则单元测试与 --help 之类的路径都会被拖死。由 validate_environment() 在启动时统一校验。

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


# 子进程的 cwd 一律是 SPRING_HOME(spring 的运行目录)，启动方式按部署方式二选一：
#   安装包部署 SPRING_HOME + SPRING_BIN_DIR   `$SPRING_BIN_DIR/spring-xxx`
#   源码部署   SPRING_HOME + SPRING_PYTHON    `python -m etl.xxx`
# 源码部署时 spring 的运行目录就是检出目录，SPRING_HOME 填检出目录即可，
# `python -m` 靠 cwd(sys.path[0]) 找到 spring。SPRING_DIR 已不再使用。

def spring_python() -> Path:
    """spring 虚拟环境的解释器(源码部署)。

    直接指向 venv 解释器，不经过 quant.sh 之类的包装脚本——包装脚本提供的
    「激活 venv」与「设 PYTHONPATH」两件事，本服务通过指定解释器 + cwd 已自动满足，
    多包一层 shell 只会多一层退出码传递风险。
    """
    value = _env("SPRING_PYTHON")
    if not value:
        raise RuntimeError(
            "环境变量 SPRING_PYTHON 未设置：源码部署请指定 spring 虚拟环境解释器的绝对路径；"
            "安装包部署请改设 SPRING_BIN_DIR。")
    return Path(value)


def spring_bin_dir() -> Path | None:
    """spring 安装环境的 bin 目录(安装包部署)；未设置表示源码部署。

    用绝对目录而不依赖 PATH：systemd 下的 PATH 与登录 shell 不同，
    且「实际执行哪个程序」不应交给环境变量去决定。
    """
    value = _env("SPRING_BIN_DIR")
    return Path(value) if value else None


def spring_home() -> Path:
    """spring 的运行目录(config/ log/ csv/ download/)，也是 ETL 子进程的 cwd。

    必须显式设置：不设的话 spring 安装包会回落到**运行用户**的 ~/.spring，
    换个用户运行就悄悄读写另一套配置。
    """
    value = _env("SPRING_HOME")
    if not value:
        raise RuntimeError(
            "环境变量 SPRING_HOME 未设置：请指定 spring 运行目录(含 config/config.yaml)的绝对路径；"
            "源码部署填 spring 检出目录。")
    return Path(value)


def command_name(module: str) -> str:
    """模块对应的 spring 命令名：etl.import_daily -> spring-import-daily。

    约定来自 spring pyproject.toml 的 [project.scripts]。命令入口与 `python -m`
    等价——被调用的模块 `__main__` 都只有 `sys.exit(main())`。
    """
    return "spring-" + module.rsplit(".", 1)[-1].replace("_", "-")


def spring_command(module: str) -> Path:
    """安装包部署下模块对应命令的绝对路径(Windows 上 pip 生成的是 .exe)。"""
    bin_dir = spring_bin_dir()
    if bin_dir is None:
        raise RuntimeError("环境变量 SPRING_BIN_DIR 未设置：请指定 spring 安装环境 bin 目录的绝对路径。")
    suffix = ".exe" if os.name == "nt" else ""
    return bin_dir / (command_name(module) + suffix)


def spring_log_dir() -> Path:
    """ETL 日志目录：SPRING_LOG_DIR > $SPRING_HOME/log。

    与 spring 自身的日志位置一致(spring 把日志写在运行目录下)，
    否则日志类 Tool 会去一个 spring 从不写入的目录里找文件。
    """
    value = _env("SPRING_LOG_DIR")
    return Path(value) if value else spring_home() / "log"


def jobs_dir() -> Path:
    """任务元数据与输出的落盘目录；放在日志目录下，服务重启后历史仍可查。"""
    return spring_log_dir() / "mcp_jobs"


def stall_timeout_default() -> int | None:
    value = _env("STALL_TIMEOUT_DEFAULT")
    return int(value) if value else None


def max_runtime_default() -> int:
    value = _env("MAX_RUNTIME_DEFAULT")
    return int(value) if value else MAX_RUNTIME_DEFAULT


def validate_environment() -> None:
    """启动时校验环境，配置有误要立刻报错而不是等到第一次调用 Tool。"""
    # 旧配置残留时必须明确报错：静默忽略的话，改了 SPRING_DIR 却不生效，排查方向会被带偏
    if _env("SPRING_DIR"):
        raise RuntimeError(
            "SPRING_DIR 已不再使用，请删除：工作目录改由 SPRING_HOME 指定"
            "(源码部署填 spring 检出目录，安装包部署填 spring 运行目录)。")

    has_bin, has_python = bool(_env("SPRING_BIN_DIR")), bool(_env("SPRING_PYTHON"))
    if has_bin == has_python:
        raise RuntimeError(
            "SPRING_BIN_DIR 与 SPRING_PYTHON 必须且只能设置一个："
            "前者用于安装包部署，后者用于源码部署。")

    home = spring_home()
    if not home.is_dir():
        raise RuntimeError(f"SPRING_HOME 不是目录或不存在: {home}")
    config = home / "config" / "config.yaml"
    if not config.is_file():
        raise RuntimeError(
            f"SPRING_HOME 下缺少配置文件: {config}；安装包部署需先运行 spring-init 并按本机修改配置，"
            f"源码部署请确认 SPRING_HOME 指向 spring 检出目录")

    if has_bin:
        _validate_installed_package()
    else:
        _validate_source_checkout(home)


def _validate_installed_package() -> None:
    bin_dir = spring_bin_dir()
    if not bin_dir.is_dir():
        raise RuntimeError(f"SPRING_BIN_DIR 不是目录或不存在: {bin_dir}")

    entries = [(f"程序 '{program}'", module) for program, module in PROGRAMS.items()]
    entries += [("参数自省出口", DESCRIBE_MODULE), ("数据完整性检查工具", CHECK_MODULE)]
    for label, module in entries:
        command = spring_command(module)
        if not command.is_file():
            raise RuntimeError(
                f"{label} 的命令不存在: {command}；确认 SPRING_BIN_DIR 指向 spring 安装环境的 "
                f"bin 目录，且安装的 spring 版本包含该命令")
        if not os.access(command, os.X_OK):
            raise RuntimeError(f"{label} 的命令不可执行: {command}")


def _validate_source_checkout(root: Path) -> None:
    interpreter = spring_python()
    if not interpreter.is_file():
        raise RuntimeError(f"SPRING_PYTHON 不存在: {interpreter}")
    if not os.access(interpreter, os.X_OK):
        raise RuntimeError(f"SPRING_PYTHON 不可执行: {interpreter}")

    for program, module in PROGRAMS.items():
        relative = Path(module.replace(".", "/") + ".py")
        if not (root / relative).is_file():
            raise RuntimeError(f"程序 '{program}' 的模块文件不存在: {root / relative}")

    # 自省出口与完整性检查工具同样要在启动时确认。
    # 少了这两项检查，spring 若停在缺少它们的旧分支上，要等到第一次调 Tool
    # 才会以「No module named ...」的形式暴露——那时排查方向已经被带偏了。
    for label, module in (("参数自省出口", DESCRIBE_MODULE),
                          ("数据完整性检查工具", CHECK_MODULE)):
        relative = Path(module.replace(".", "/") + ".py")
        if not (root / relative).is_file():
            raise RuntimeError(
                f"{label} '{module}' 的文件不存在: {root / relative}；"
                f"确认 SPRING_HOME 指向的检出包含该文件（分支是否过旧？）"
            )
