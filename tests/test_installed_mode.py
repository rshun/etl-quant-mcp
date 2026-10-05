# 修改记录:
#   2026-10-04  Claude  新建：spring 安装包部署模式(SPRING_BIN_DIR + SPRING_HOME)的正反例
#   2026-10-04  Claude  去掉 SPRING_DIR：工作目录统一为 SPRING_HOME，
#                       启动方式由 SPRING_BIN_DIR / SPRING_PYTHON 二选一
"""spring 的部署方式与环境变量。

工作目录一律是 SPRING_HOME(spring 的运行目录：config/ log/ csv/ download/)，
启动方式二选一：

| 模式 | 环境变量 | 启动方式 |
|---|---|---|
| 安装包部署 | SPRING_HOME + SPRING_BIN_DIR | `$SPRING_BIN_DIR/spring-xxx` |
| 源码部署 | SPRING_HOME(= spring 检出目录) + SPRING_PYTHON | `python -m etl.xxx` |

源码部署时 spring 的运行目录本来就是项目根目录，所以 SPRING_HOME 就填检出目录。
SPRING_DIR 已不再使用，设置了要报错并指明改法。
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import params
import schema
from tests.conftest import IMPORT_DAILY_SCHEMA

_SUFFIX = ".exe" if os.name == "nt" else ""

ALL_MODULES = [*schema.PROGRAMS.values(), schema.DESCRIBE_MODULE, schema.CHECK_MODULE]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离本机环境：开发机上若真设了这些变量，会让用例测到错误的模式。"""
    for name in ("SPRING_DIR", "SPRING_PYTHON", "SPRING_BIN_DIR",
                 "SPRING_HOME", "SPRING_LOG_DIR"):
        monkeypatch.delenv(name, raising=False)
    params.clear_cache()
    yield
    params.clear_cache()


def _make_home(root: Path) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """一套完整的安装包部署：bin 目录里有全部命令，运行目录里有 config.yaml。"""
    bin_dir = tmp_path / "opt" / "spring" / "bin"
    bin_dir.mkdir(parents=True)
    for module in ALL_MODULES:
        command = bin_dir / (schema.command_name(module) + _SUFFIX)
        command.write_text("#!/bin/sh\n", encoding="utf-8")
        command.chmod(0o755)

    home = _make_home(tmp_path / "srv" / "spring")

    monkeypatch.setenv("SPRING_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("SPRING_HOME", str(home))
    return bin_dir, home


@pytest.fixture
def source(tmp_path, monkeypatch):
    """一套完整的源码部署：SPRING_HOME 是含全部模块文件的 spring 检出目录。"""
    home = _make_home(tmp_path / "spring")
    for module in ALL_MODULES:
        path = home / (module.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    interpreter = tmp_path / "python"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)

    monkeypatch.setenv("SPRING_HOME", str(home))
    monkeypatch.setenv("SPRING_PYTHON", str(interpreter))
    return home, interpreter


def _home_of(mode: str, request) -> Path:
    """installed 夹具返回 (bin_dir, home)，source 夹具返回 (home, interpreter)。"""
    value = request.getfixturevalue(mode)
    return value[1] if mode == "installed" else value[0]


# ── 命令名约定 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("module, expected", [
    ("etl.import_daily", "spring-import-daily"),
    ("etl.adjust", "spring-adjust"),
    ("etl.fill_turnover", "spring-fill-turnover"),
    ("tools.describe_cli", "spring-describe-cli"),
    ("tools.check_daily", "spring-check-daily"),
])
def test_command_name_follows_spring_scripts(module, expected):
    """正例(契约): 命令名 = spring- + 模块末段(下划线换连字符)，
    与 spring pyproject.toml [project.scripts] 的约定一致。"""
    assert schema.command_name(module) == expected


def test_every_module_maps_to_distinct_command():
    """反例(关键): 两个模块映射到同一个命令名，会让白名单里的程序悄悄跑成另一个。"""
    names = [schema.command_name(m) for m in ALL_MODULES]
    assert len(names) == len(set(names))


# ── 命令行构造 ────────────────────────────────────────────────────────────────

def test_build_argv_uses_command_in_installed_mode(installed):
    """正例: 安装包模式直接调用 spring-* 命令，不再经 python -m。"""
    bin_dir, _ = installed
    argv = params.build_argv("import_daily", {"begin": "20260817", "end": "20260817"},
                             program_schema=IMPORT_DAILY_SCHEMA)
    assert argv == [str(bin_dir / ("spring-import-daily" + _SUFFIX)),
                    "-b", "20260817", "-e", "20260817"]


def test_build_argv_uses_module_in_source_mode(source):
    """正例: 源码模式按 [SPRING_PYTHON, -u, -m, 模块] 调用。"""
    _, interpreter = source
    argv = params.build_argv("import_daily", {"begin": "20260817"},
                             program_schema=IMPORT_DAILY_SCHEMA)
    assert argv[:4] == [str(interpreter), "-u", "-m", "etl.import_daily"]


def test_explicit_python_still_means_module_invocation(installed):
    """正例: 显式传 python 时按源码方式调用，供测试与 install.sh 冒烟使用。"""
    argv = params.build_argv("import_daily", {"begin": "20260817"},
                             python="/usr/bin/python3",
                             program_schema=IMPORT_DAILY_SCHEMA)
    assert argv[:4] == ["/usr/bin/python3", "-u", "-m", "etl.import_daily"]


def test_build_check_argv_uses_command_in_installed_mode(installed):
    """正例: 完整性检查同样走命令，--json 仍恒定在末尾。"""
    bin_dir, _ = installed
    argv = params.build_check_argv({"begin": "20260817", "end": "20260817"})
    assert argv[0] == str(bin_dir / ("spring-check-daily" + _SUFFIX))
    assert "-m" not in argv
    assert argv[-1] == "--json"


@pytest.mark.parametrize("mode", ["installed", "source"])
def test_introspection_runs_in_spring_home(mode, request):
    """正例: 两种模式下子进程 cwd 都是 SPRING_HOME。"""
    home = _home_of(mode, request)
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    fake = MagicMock(stdout=payload, stderr="", returncode=0)
    with patch.object(params.subprocess, "run", return_value=fake) as run:
        params.fetch_program_schema("import_daily")
    assert run.call_args.kwargs["cwd"] == str(home)
    assert run.call_args.kwargs["shell"] is False


def test_introspection_uses_describe_command_in_installed_mode(installed):
    bin_dir, _ = installed
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    fake = MagicMock(stdout=payload, stderr="", returncode=0)
    with patch.object(params.subprocess, "run", return_value=fake) as run:
        params.fetch_program_schema("import_daily")
    assert run.call_args.args[0] == [str(bin_dir / ("spring-describe-cli" + _SUFFIX)),
                                     "import_daily"]


# ── 目录解析 ──────────────────────────────────────────────────────────────────

def test_spring_home_is_required():
    """反例(关键): 不设 SPRING_HOME 时 spring 会回落到运行用户的 ~/.spring，
    读不到真正的配置——必须报错，不能静默换目录。"""
    with pytest.raises(RuntimeError, match="SPRING_HOME"):
        schema.spring_home()


def test_log_dir_defaults_to_spring_home(installed):
    """正例: 与 spring 自身一致——日志写在运行目录的 log/ 下。"""
    _, home = installed
    assert schema.spring_log_dir() == home / "log"
    assert schema.jobs_dir() == home / "log" / "mcp_jobs"


def test_log_dir_explicit_override_wins(installed, monkeypatch, tmp_path):
    monkeypatch.setenv("SPRING_LOG_DIR", str(tmp_path / "elsewhere"))
    assert schema.spring_log_dir() == tmp_path / "elsewhere"


# ── 启动期环境校验：安装包部署 ────────────────────────────────────────────────

def test_validate_installed_passes_when_complete(installed):
    schema.validate_environment()


@pytest.mark.parametrize("module", ALL_MODULES)
def test_validate_installed_reports_missing_command(installed, module):
    """反例(关键): 少任何一个命令都要在启动时报错并点名，
    而不是等第一次调 Tool 才以「No such file」暴露。"""
    bin_dir, _ = installed
    (bin_dir / (schema.command_name(module) + _SUFFIX)).unlink()
    with pytest.raises(RuntimeError, match=schema.command_name(module)):
        schema.validate_environment()


def test_validate_installed_rejects_missing_bin_dir(installed, monkeypatch, tmp_path):
    monkeypatch.setenv("SPRING_BIN_DIR", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="SPRING_BIN_DIR"):
        schema.validate_environment()


# ── 启动期环境校验：源码部署 ──────────────────────────────────────────────────

def test_validate_source_passes_when_complete(source):
    schema.validate_environment()


def test_validate_source_reports_missing_module(source):
    """反例: SPRING_HOME 指向的检出缺模块文件(分支过旧)。"""
    home, _ = source
    (home / "etl" / "import_daily.py").unlink()
    with pytest.raises(RuntimeError, match="import_daily"):
        schema.validate_environment()


# ── 启动期环境校验：两种模式共有 ──────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["installed", "source"])
def test_validate_requires_config(mode, request):
    """反例: 运行目录没有 config.yaml(安装包部署没跑 spring-init，或 SPRING_HOME 指错)。"""
    home = _home_of(mode, request)
    (home / "config" / "config.yaml").unlink()
    with pytest.raises(RuntimeError, match="config.yaml"):
        schema.validate_environment()


@pytest.mark.parametrize("mode", ["installed", "source"])
def test_validate_requires_spring_home(mode, request, monkeypatch):
    request.getfixturevalue(mode)
    monkeypatch.delenv("SPRING_HOME")
    with pytest.raises(RuntimeError, match="SPRING_HOME"):
        schema.validate_environment()


def test_validate_rejects_both_launchers(installed, monkeypatch, tmp_path):
    """反例(关键): 同时设 SPRING_BIN_DIR 与 SPRING_PYTHON 时启动方式有歧义，必须拒绝。"""
    monkeypatch.setenv("SPRING_PYTHON", str(tmp_path / "python"))
    with pytest.raises(RuntimeError, match="SPRING_BIN_DIR.*SPRING_PYTHON"):
        schema.validate_environment()


def test_validate_requires_a_launcher(monkeypatch, tmp_path):
    """反例: 两个都没设，不知道怎么启动 spring。"""
    monkeypatch.setenv("SPRING_HOME", str(_make_home(tmp_path / "home")))
    with pytest.raises(RuntimeError, match="SPRING_BIN_DIR.*SPRING_PYTHON"):
        schema.validate_environment()


@pytest.mark.parametrize("mode", ["installed", "source"])
def test_validate_rejects_legacy_spring_dir(mode, request, monkeypatch, tmp_path):
    """反例(迁移): 旧配置残留 SPRING_DIR 时明确报错并指明改用 SPRING_HOME，
    而不是被静默忽略——否则改了 SPRING_DIR 却不生效，排查方向会被带偏。"""
    request.getfixturevalue(mode)
    monkeypatch.setenv("SPRING_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="SPRING_DIR.*SPRING_HOME"):
        schema.validate_environment()
