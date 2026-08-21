# 修改记录:
#   2026-08-19  Claude  新建：以固化快照断言「我按契约调用 spring」
"""跨仓契约测试——本仓库这一半。

文档第七节把契约测试拆成两份，两边都不需要对方在场：

| 放哪 | 测什么 |
|---|---|
| spring `tests/unit/test_cli_contract.py` | 「我的 CLI 接口是稳定的」 |
| 本文件 | 「我按契约调用」 |

spring 改了 CLI → spring 那边红；本服务用错参数 → 这边红。
只有端到端才需要凑齐，那部分在 `test_integration.py`（标 integration）。

本文件全程使用固化快照 `fixtures/describe_cli_snapshot.json`，
**不起任何子进程、不需要 spring 在场**。快照与实时自省是否已漂移，
由 `test_integration.py::test_live_introspection_matches_snapshot` 负责。
"""
import json

import pytest

import params
import schema
from tests.conftest import (
    FAKE_PYTHON,
    SNAPSHOT_PATH,
    TODAY_SENTINEL,
    load_snapshot,
    normalize_defaults,
)

SNAPSHOT = load_snapshot()
PROGRAM_IDS = sorted(SNAPSHOT)

# 6 个程序共有的参数面
COMMON_FLAGS = {"begin": "-b", "end": "-e", "codes": "-c", "exchanges": "-x"}
ARGUMENT_KEYS = {"flags", "type", "action", "nargs", "default",
                 "choices", "required", "help"}

# 自省当天作为默认值的三个下载型程序
DATE_DEFAULT_PROGRAMS = ("adjust", "fetch_index", "import_daily")
# begin/end 在 argparse 层默认为 null 的三个补齐型程序
NULL_DEFAULT_PROGRAMS = ("fill_volratio", "update_limit", "fill_shares")


# ── 快照本身 ──────────────────────────────────────────────────────────────────

def test_snapshot_covers_exactly_the_registry():
    """正例: 快照与程序注册表必须一一对应，新增 ETL 不能漏进快照"""
    assert set(SNAPSHOT) == set(schema.PROGRAMS)


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_snapshot_module_matches_registry(name):
    """正例: 快照里的模块路径与注册表一致"""
    assert SNAPSHOT[name]["module"] == schema.PROGRAMS[name]


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_snapshot_argument_shape(name):
    """正例: 每个参数的字段集合固定——params.py 按这个形状解析"""
    for dest, spec in SNAPSHOT[name]["arguments"].items():
        assert set(spec) == ARGUMENT_KEYS, f"{name}.{dest} 字段集合不符"
        assert spec["flags"], f"{name}.{dest} 没有 flags"


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_every_argument_has_help(name):
    """正例(契约 C4): help 是把语义传给模型的唯一免费通道，不得为空"""
    for dest, spec in SNAPSHOT[name]["arguments"].items():
        assert spec["help"], f"{name}.{dest} 缺 help"


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_common_flags_present(name):
    """正例: -b/-e/-c/-x 是通用调用面，少一个本服务的通用逻辑就会断"""
    args = SNAPSHOT[name]["arguments"]
    for dest, short in COMMON_FLAGS.items():
        assert dest in args, f"{name} 缺参数 {dest}"
        assert short in args[dest]["flags"]


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_exchange_choices_consistent(name):
    """正例: 交易所枚举在 6 个程序间一致，本服务才敢用同一份白名单校验"""
    action = SNAPSHOT[name]["arguments"]["exchanges"]
    assert action["choices"] == ["sh", "sz", "bj", "all"]
    assert action["type"] == "str.lower", "本服务依赖它做大小写归一化"


def test_snapshot_is_normalized():
    """反例(关键): 快照里不得留下真实日期——那会让契约测试每天都红"""
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    for name in DATE_DEFAULT_PROGRAMS:
        for dest in ("begin", "end"):
            assert raw[name]["arguments"][dest]["default"] == TODAY_SENTINEL, \
                f"{name}.{dest} 的日期默认值未归一化"


def test_normalize_is_idempotent():
    """正例: 归一化可重复施加，实时自省与快照才好直接比对"""
    assert normalize_defaults(SNAPSHOT) == SNAPSHOT


# ── 两个默认值陷阱（ADR-4）────────────────────────────────────────────────────

@pytest.mark.parametrize("name", DATE_DEFAULT_PROGRAMS)
def test_download_programs_default_to_today(name):
    """正例(陷阱1): 三个下载型的 begin/end 默认值是自省当天，逐日变化。

    钉住它是为了让「哪天有人把默认值改成固定值」这件事被发现——
    那会改变省略参数时的行为。
    """
    for dest in ("begin", "end"):
        assert SNAPSHOT[name]["arguments"][dest]["default"] == TODAY_SENTINEL


@pytest.mark.parametrize("name", NULL_DEFAULT_PROGRAMS)
def test_fill_programs_have_null_argparse_default(name):
    """正例(陷阱2): 三个补齐型的 begin/end 在 argparse 层默认为 null。

    真实默认（T-1／今天）是 parse_arguments() 里 parse 之后才套用的，自省看不到。
    该语义只由 help 文本承载，所以本服务必须把 help 原样透传给模型。
    """
    for dest in ("begin", "end"):
        spec = SNAPSHOT[name]["arguments"][dest]
        assert spec["default"] is None
        assert "默认" in spec["help"], "真实默认值只能从 help 读出来，它不能没有说明"


# ── argv 构造（按快照）────────────────────────────────────────────────────────

def _argv(name: str, **values) -> list[str]:
    return params.build_argv(name, values, python=FAKE_PYTHON,
                             program_schema=SNAPSHOT[name])


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_argv_prefix_always_unbuffered(name):
    """正例(关键): 每个程序的 argv 都必须带 -u。

    少了它，pipe 的块缓冲会让正常运行被误判为卡死——整个停顿检测就是错的。
    """
    argv = _argv(name, begin="20260817", end="20260817")
    assert argv[:4] == [FAKE_PYTHON, "-u", "-m", schema.PROGRAMS[name]]


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_argv_common_params(name):
    """正例: 通用参数在 6 个程序上构造结果一致"""
    argv = _argv(name, begin="20260817", end="20260818",
                 codes=["600519"], exchanges=["SH"])
    assert argv[4:] == ["-b", "20260817", "-e", "20260818",
                        "-c", "600519", "-x", "sh"]


@pytest.mark.parametrize("name", DATE_DEFAULT_PROGRAMS)
def test_argv_source_enum_enforced(name):
    """反例: 数据源枚举以快照为准，传错立刻拒绝"""
    with pytest.raises(params.ParamError, match="取值非法"):
        _argv(name, begin="20260817", source="nosuch")


@pytest.mark.parametrize("name", NULL_DEFAULT_PROGRAMS)
def test_argv_forcerun_flag(name):
    """正例: 补齐型程序的 -f 开关"""
    assert _argv(name, forcerun=True)[4:] == ["-f"]


def test_argv_import_daily_print_only():
    """正例: 干跑开关是安全冒烟的依据"""
    assert _argv("import_daily", print_only=True)[4:] == ["-p"]


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_argv_rejects_injection(name):
    """反例(安全): 6 个程序一视同仁地拒绝夹带"""
    with pytest.raises(params.ParamError, match="代码非法"):
        _argv(name, begin="20260817", codes=["600519; DROP TABLE"])


# ── 退出码解读 ────────────────────────────────────────────────────────────────

def test_exit_code_contract():
    """正例: 退出码按契约翻译成状态"""
    assert schema.status_from_exit_code(0) == schema.STATUS_SUCCEEDED
    assert schema.status_from_exit_code(1) == schema.STATUS_FAILED
    assert schema.status_from_exit_code(3) == schema.STATUS_PARTIAL


def test_exit_code_2_is_usage_error_not_partial():
    """反例(关键): 2 是 argparse 内建的用法错误，绝不能读成部分成功。

    读错的后果是把「参数传错了」呈现为「大体成功」——一个静默的错误结论。
    """
    assert schema.status_from_exit_code(2) == schema.STATUS_FAILED
    assert "用法错误" in schema.describe_exit_code(2)


@pytest.mark.parametrize("code", [4, 99, -9, -15])
def test_unknown_exit_codes_are_failures(code):
    """反例: 未知退出码一律按失败，不做乐观解读"""
    assert schema.status_from_exit_code(code) == schema.STATUS_FAILED


def test_partial_status_not_produced_by_spring_yet():
    """契约备注: 3(部分成功) 已在协议中预留，但 spring 侧尚无程序产出。

    这条不是在测代码，而是把「协议已定、实现未跟上」这个事实钉在测试里，
    免得日后有人看到映射表就以为 partial 会真的出现。
    """
    assert schema.EXIT_PARTIAL == 3
    assert schema.EXIT_CODE_STATUS[schema.EXIT_PARTIAL] == schema.STATUS_PARTIAL


# ── 其他注册表不变式 ──────────────────────────────────────────────────────────

def test_fill_targets_are_registered_programs():
    """正例: 合并 Tool 的三个 target 都必须在白名单内"""
    assert set(schema.FILL_TARGETS) <= set(schema.PROGRAMS)


def test_fill_targets_order_is_dependency_safe():
    """正例: 三者顺序固定，避免依赖颠倒"""
    assert schema.FILL_TARGETS == ("fill_volratio", "update_limit", "fill_shares")


def test_check_module_not_in_write_whitelist():
    """反例(安全关键): check_daily 是只读工具，绝不能混进写入白名单。

    PROGRAMS 是「能被当作 ETL 启动」的边界，混进只读工具会让这条边界失去意义。
    """
    assert schema.CHECK_MODULE not in schema.PROGRAMS.values()
    assert "check_daily" not in schema.PROGRAMS


@pytest.mark.parametrize("name", PROGRAM_IDS)
def test_snapshot_preserves_declaration_order(name):
    """正例(关键): 快照必须保留 argparse 的声明顺序，不能按字典序排。

    build_argv 依声明顺序拼接 argv，同样的入参才会得到同样的命令行（便于比对与复现）。
    用 sort_keys 生成快照会把顺序打乱成 begin/codes/end/...，argv 随之变形。
    这条就是为了让那种「顺手加个 sort_keys」被立刻发现。
    """
    dests = list(SNAPSHOT[name]["arguments"])
    assert dests[:4] == ["begin", "end", "codes", "exchanges"], \
        f"{name} 的参数顺序被改动过: {dests}"


# ── 启动期环境校验 ────────────────────────────────────────────────────────────

def test_validate_environment_checks_introspection_entrypoint(tmp_path, monkeypatch):
    """反例(关键): spring 检出缺少 describe_cli 时必须在启动时报错。

    少了这条检查，spring 若停在旧分支上，故障会推迟到第一次调 Tool，
    表现为「No module named tools.describe_cli」——排查方向会被带偏到
    PYTHONPATH / cwd 上去，而真实原因是检出的分支不对。
    """
    root = tmp_path / "spring"
    for module in schema.PROGRAMS.values():
        path = root / (module.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    interpreter = tmp_path / "python"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)

    monkeypatch.setenv("SPRING_DIR", str(root))
    monkeypatch.setenv("SPRING_PYTHON", str(interpreter))

    with pytest.raises(RuntimeError, match="describe_cli"):
        schema.validate_environment()

    # 补上自省出口后，还缺 check_daily
    describe = root / "tools" / "describe_cli.py"
    describe.parent.mkdir(parents=True, exist_ok=True)
    describe.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="check_daily"):
        schema.validate_environment()

    # 两个都齐了才通过
    (root / "tools" / "check_daily.py").write_text("", encoding="utf-8")
    schema.validate_environment()


# ── 跨 Python 版本可移植性 ────────────────────────────────────────────────────

def test_no_builtin_shadowed_in_annotations():
    """反例(关键): 同一作用域内不得「定义了与内置同名的东西，又在注解里用那个内置」。

    Python 3.14 起注解才是延迟求值（PEP 649）；3.10–3.13 上注解在**定义时立即求值**，
    此时类体里的 `def list(...)` 会遮蔽内置 `list`，同类中任何 `-> list[dict]`
    都会抛 `TypeError: 'function' object is not subscriptable`——**整个模块直接 import 失败**。

    这个 bug 在 3.14 上完全不可见，靠跑测试发现不了，只能靠静态检查。
    `pyproject.toml` 声明 requires-python >= 3.10，这条就是那句声明的守卫。
    （真实案例：Runner.list 曾因此让服务在 Debian 上起不来。）
    """
    import ast
    import builtins
    from pathlib import Path

    builtin_names = set(dir(builtins))
    root = Path(__file__).resolve().parents[1]

    def defined_here(body) -> set[str]:
        names = set()
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    def annotated_here(body) -> set[str]:
        used = set()
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            annotations = [a.annotation for a in node.args.args + node.args.kwonlyargs
                           if a.annotation]
            if node.returns:
                annotations.append(node.returns)
            for annotation in annotations:
                used.update(n.id for n in ast.walk(annotation) if isinstance(n, ast.Name))
        return used

    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 有 `from __future__ import annotations` 的模块注解恒为惰性，可豁免
        if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names) for n in tree.body):
            continue
        scopes = [("<module>", tree.body)]
        scopes += [(n.name, n.body) for n in tree.body if isinstance(n, ast.ClassDef)]
        for scope, body in scopes:
            for name in sorted(defined_here(body) & builtin_names & annotated_here(body)):
                offenders.append(f"{path.name}:{scope} 定义了 '{name}'，"
                                 f"同作用域注解又用到内置 '{name}'")

    assert not offenders, "存在会在 Python < 3.14 上 import 失败的名字遮蔽:\n  " + \
                          "\n  ".join(offenders)
