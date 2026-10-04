# 修改记录:
#   2026-10-04  Claude  新建：spring 安装包部署模式(SPRING_BIN_DIR + SPRING_HOME)的正反例
"""spring 安装包部署模式。

spring 改为 wheel 安装后，代码在安装环境的 site-packages 里，每个 ETL / 工具注册为
一个 `spring-*` 命令；配置与运行产出放在运行目录 SPRING_HOME。本服务据此有两种模式：

| 模式 | 环境变量 | 启动方式 | 子进程 cwd |
|---|---|---|---|
| 源码部署 | SPRING_DIR + SPRING_PYTHON | `python -m etl.xxx` | SPRING_DIR |
| 安装包部署 | SPRING_BIN_DIR + SPRING_HOME | `$SPRING_BIN_DIR/spring-xxx` | SPRING_HOME |

源码模式的用例在 test_params / test_contract 里，本文件只测安装包模式及两者的边界。
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


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """一套完整的安装包部署：bin 目录里有全部命令，运行目录里有 config.yaml。"""
    bin_dir = tmp_path / "opt" / "spring" / "bin"
    bin_dir.mkdir(parents=True)
    for module in ALL_MODULES:
        command = bin_dir / (schema.command_name(module) + _SUFFIX)
        command.write_text("#!/bin/sh\n", encoding="utf-8")
        command.chmod(0o755)

    home = tmp_path / "srv" / "spring"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text("", encoding="utf-8")

    monkeypatch.setenv("SPRING_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("SPRING_HOME", str(home))
    return bin_dir, home


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


def test_introspection_uses_command_and_runs_in_spring_home(installed):
    """正例: 自省走 spring-describe-cli，cwd 是运行目录。"""
    bin_dir, home = installed
    payload = json.dumps(IMPORT_DAILY_SCHEMA, ensure_ascii=False)
    fake = MagicMock(stdout=payload, stderr="", returncode=0)
    with patch.object(params.subprocess, "run", return_value=fake) as run:
        params.fetch_program_schema("import_daily")
    assert run.call_args.args[0] == [str(bin_dir / ("spring-describe-cli" + _SUFFIX)),
                                     "import_daily"]
    assert run.call_args.kwargs["cwd"] == str(home)
    assert run.call_args.kwargs["shell"] is False


# ── 目录解析 ──────────────────────────────────────────────────────────────────

def test_workdir_is_spring_home_in_installed_mode(installed):
    _, home = installed
    assert schema.spring_workdir() == home


def test_workdir_requires_spring_home_in_installed_mode(installed, monkeypatch):
    """反例(关键): 不设 SPRING_HOME 时 spring 会回落到运行用户的 ~/.spring，
    读不到真正的配置——必须报错，不能静默换目录。"""
    monkeypatch.delenv("SPRING_HOME")
    with pytest.raises(RuntimeError, match="SPRING_HOME"):
        schema.spring_workdir()


def test_log_dir_defaults_to_spring_home(installed):
    """正例: 与 spring 自身一致——日志写在运行目录的 log/ 下。"""
    _, home = installed
    assert schema.spring_log_dir() == home / "log"
    assert schema.jobs_dir() == home / "log" / "mcp_jobs"


def test_log_dir_explicit_override_wins(installed, monkeypatch, tmp_path):
    monkeypatch.setenv("SPRING_LOG_DIR", str(tmp_path / "elsewhere"))
    assert schema.spring_log_dir() == tmp_path / "elsewhere"


def test_source_mode_log_dir_unchanged(monkeypatch, tmp_path):
    """正例(兼容): 源码部署、未设 SPRING_HOME 时行为与改造前一致。"""
    monkeypatch.setenv("SPRING_DIR", str(tmp_path))
    assert schema.spring_log_dir() == tmp_path / "log"
    assert schema.spring_workdir() == tmp_path


# ── 启动期环境校验 ────────────────────────────────────────────────────────────

def test_validate_installed_passes_when_complete(installed):
    schema.validate_environment()


def test_validate_installed_does_not_need_spring_python(installed):
    """正例: 安装包模式不需要 SPRING_PYTHON——命令自带解释器(shebang)。"""
    assert not os.environ.get("SPRING_PYTHON")
    schema.validate_environment()


@pytest.mark.parametrize("module", ALL_MODULES)
def test_validate_installed_reports_missing_command(installed, module):
    """反例(关键): 少任何一个命令都要在启动时报错并点名，
    而不是等第一次调 Tool 才以「No such file」暴露。"""
    bin_dir, _ = installed
    (bin_dir / (schema.command_name(module) + _SUFFIX)).unlink()
    with pytest.raises(RuntimeError, match=schema.command_name(module)):
        schema.validate_environment()


def test_validate_installed_requires_config(installed):
    """反例: 运行目录没有 config.yaml(没跑 spring-init)。"""
    _, home = installed
    (home / "config" / "config.yaml").unlink()
    with pytest.raises(RuntimeError, match="config.yaml"):
        schema.validate_environment()


def test_validate_installed_requires_spring_home(installed, monkeypatch):
    monkeypatch.delenv("SPRING_HOME")
    with pytest.raises(RuntimeError, match="SPRING_HOME"):
        schema.validate_environment()


def test_validate_installed_rejects_missing_bin_dir(installed, monkeypatch, tmp_path):
    monkeypatch.setenv("SPRING_BIN_DIR", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="SPRING_BIN_DIR"):
        schema.validate_environment()


def test_validate_rejects_both_modes_at_once(installed, monkeypatch, tmp_path):
    """反例(关键): 两种模式的变量同时设置时，cwd 与启动方式都有歧义，必须拒绝。"""
    monkeypatch.setenv("SPRING_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="SPRING_DIR.*SPRING_BIN_DIR"):
        schema.validate_environment()
