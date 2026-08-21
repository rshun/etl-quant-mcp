# 修改记录:
#   2026-08-19  Claude  新建：执行器的正反例，含卡死模拟与缓冲回归
"""runner.py 的正反例。

用真实子进程（临时 python 小脚本）而非 mock：本模块要守的恰恰是
「进程真的卡住了会怎样」「输出真的被缓冲了会怎样」，mock 掉就什么都没测到。

两个最关键的用例：
  * test_slow_but_steady_output_stays_running —— 缓冲回归。这条不过，
    整个停顿检测就是错的（文档 5.3）。
  * test_silent_process_becomes_stalled —— 卡死判定本身。
"""
import sys
import time

import pytest

import runner as runner_mod
import schema
from unittest.mock import patch

from runner import Runner, parse_progress

PY = sys.executable


def _script(code: str, *, unbuffered: bool = True) -> list[str]:
    """构造一个假 ETL 子进程。unbuffered=False 用于验证 PYTHONUNBUFFERED 的作用。"""
    argv = [PY]
    if unbuffered:
        argv.append("-u")
    argv += ["-c", code]
    return argv


@pytest.fixture
def run_dir(tmp_path):
    return tmp_path


@pytest.fixture
def runner(run_dir):
    r = Runner(jobs_dir=run_dir, cwd=run_dir, poll_interval=0.05, terminate_grace=1.0)
    yield r
    r.shutdown()


def _wait_terminal(runner: Runner, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = runner.get(job_id)
        if info["status"] in schema.TERMINAL_STATUSES:
            return info
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态: {runner.get(job_id)}")


def _wait_status(runner: Runner, job_id: str, status: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = runner.get(job_id)
        if info["status"] == status:
            return info
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内变为 {status}: {runner.get(job_id)}")


# ── 进度解析 ──────────────────────────────────────────────────────────────────

def test_parse_progress_extracts_counts():
    """正例: 从 spring 的进度行抽出结构化进度"""
    assert parse_progress("21:00:00 [etl] [INFO]    已处理: 3400/5400") == {
        "done": 3400, "total": 5400, "percent": 63.0,
    }


def test_parse_progress_returns_none_on_mismatch():
    """反例: 格式不匹配返回 None，不抛异常——日志格式变了不该拖垮任务"""
    assert parse_progress("普通日志行") is None
    assert parse_progress("") is None


def test_parse_progress_zero_total_does_not_divide_by_zero():
    """反例(边界): total 为 0 不该崩"""
    assert parse_progress("已处理: 0/0") == {"done": 0, "total": 0, "percent": None}


# ── 退出码 → 状态 ─────────────────────────────────────────────────────────────

def test_exit_0_is_succeeded(runner):
    """正例: 退出码 0 → succeeded"""
    job_id = runner.submit("fake", _script("print('done')"))
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_SUCCEEDED
    assert info["exit_code"] == 0


def test_exit_1_is_failed(runner):
    """反例(关键): 退出码 1 → failed，**绝不得报成功**"""
    job_id = runner.submit("fake", _script("import sys; print('boom'); sys.exit(1)"))
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_FAILED
    assert info["exit_code"] == 1


def test_exit_2_is_failed_not_partial(runner):
    """反例(关键): 退出码 2 是 argparse 用法错误，必须判 failed，绝不能读成 partial"""
    job_id = runner.submit("fake", _script("import sys; sys.exit(2)"))
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_FAILED
    assert "用法错误" in info["exit_hint"]


def test_exit_3_is_partial(runner):
    """正例: 退出码 3 → partial"""
    job_id = runner.submit("fake", _script("import sys; sys.exit(3)"))
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_PARTIAL


def test_unknown_exit_code_is_failed(runner):
    """反例: 未知退出码一律按失败，不做乐观解读"""
    job_id = runner.submit("fake", _script("import sys; sys.exit(42)"))
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_FAILED


def test_unstartable_command_fails_clearly(runner):
    """反例: 可执行文件不存在 → failed 且给出明确原因，不抛裸异常"""
    job_id = runner.submit("fake", ["/nonexistent/python", "-c", "pass"])
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_FAILED
    assert "无法启动子进程" in (info["error"] or "")


# ── 心跳与卡死 ────────────────────────────────────────────────────────────────

def test_silent_process_becomes_stalled(runner):
    """正例(核心): 进程活着但长时间无输出 → stalled"""
    code = "import time; print('start'); time.sleep(30)"
    job_id = runner.submit("fake", _script(code), stall_timeout=1, max_runtime=60)
    info = _wait_status(runner, job_id, schema.STATUS_STALLED, timeout=10)
    assert info["idle_seconds"] >= 1
    assert info["last_line"] == "start"
    runner.cancel(job_id, force=True)


def test_stalled_is_not_terminal_and_recovers(runner):
    """正例(核心): stalled 是可回退的非终态——输出恢复后应回到 running"""
    code = ("import time\n"
            "print('start')\n"
            "time.sleep(2)\n"
            "print('   已处理: 50/100')\n"
            "time.sleep(5)\n")
    job_id = runner.submit("fake", _script(code), stall_timeout=1, max_runtime=60)
    _wait_status(runner, job_id, schema.STATUS_STALLED, timeout=10)
    info = _wait_status(runner, job_id, schema.STATUS_RUNNING, timeout=10)
    assert info["progress"] == {"done": 50, "total": 100, "percent": 50.0}
    runner.cancel(job_id, force=True)


def test_slow_but_steady_output_stays_running(runner):
    """反例(缓冲回归，最关键的一条): 输出很慢但持续，必须保持 running，不得误判卡死。

    脚本刻意**不加 -u**：这条能过，证明执行器注入的 PYTHONUNBUFFERED=1 真的生效了。
    若两道保险都失效，pipe 的块缓冲会攒着不吐，这个任务会被误判为 stalled。
    """
    code = ("import time\n"
            "for i in range(12):\n"
            "    print(f'   已处理: {i*10}/120')\n"
            "    time.sleep(0.2)\n")
    job_id = runner.submit("fake", _script(code, unbuffered=False),
                           stall_timeout=1, max_runtime=60)

    saw_stalled = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        info = runner.get(job_id)
        if info["status"] == schema.STATUS_STALLED:
            saw_stalled = True
            break
        if info["status"] in schema.TERMINAL_STATUSES:
            break
        time.sleep(0.05)

    info = _wait_terminal(runner, job_id)
    assert not saw_stalled, "持续输出的任务被误判为卡死——缓冲保险失效了"
    assert info["status"] == schema.STATUS_SUCCEEDED
    assert info["line_count"] == 12


def test_progress_tracked_from_output(runner):
    """正例: 进度从输出流中持续更新"""
    code = ("print('   已处理: 100/500')\n"
            "print('   已处理: 500/500')\n")
    job_id = runner.submit("fake", _script(code))
    _wait_terminal(runner, job_id)
    assert runner.get(job_id)["progress"] == {"done": 500, "total": 500, "percent": 100.0}


# ── 超时终止 ──────────────────────────────────────────────────────────────────

def test_max_runtime_kills_and_marks_killed_stalled(runner):
    """正例: 超过硬上限自动终止，状态为 killed_stalled，且进程确已死"""
    code = "import time; print('start'); time.sleep(60)"
    job_id = runner.submit("fake", _script(code), stall_timeout=30, max_runtime=1)
    info = _wait_terminal(runner, job_id, timeout=15)
    assert info["status"] == schema.STATUS_KILLED_STALLED
    assert "max_runtime" in (info["error"] or "")


def test_sigterm_resistant_process_is_killed(runner):
    """反例: 子进程忽略 SIGTERM 时，宽限期后必须 SIGKILL 生效"""
    code = ("import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ignoring sigterm')\n"
            "time.sleep(60)\n")
    job_id = runner.submit("fake", _script(code), stall_timeout=30, max_runtime=1)
    info = _wait_terminal(runner, job_id, timeout=20)
    assert info["status"] == schema.STATUS_KILLED_STALLED


# ── 取消 ──────────────────────────────────────────────────────────────────────

def test_cancel_running_job(runner):
    """正例: 取消运行中的任务"""
    code = "import time; print('start'); time.sleep(30)"
    job_id = runner.submit("fake", _script(code), stall_timeout=30, max_runtime=60)
    _wait_status(runner, job_id, schema.STATUS_RUNNING, timeout=10)
    runner.cancel(job_id, force=True)
    info = _wait_terminal(runner, job_id, timeout=15)
    assert info["status"] == schema.STATUS_CANCELLED


def test_cancel_unknown_job_raises(runner):
    """反例: 取消不存在的任务要明确报错"""
    with pytest.raises(runner_mod.JobNotFound):
        runner.cancel("nosuch-job")


def test_cancel_finished_job_is_noop(runner):
    """反例: 取消已结束的任务不该改写它的结果"""
    job_id = runner.submit("fake", _script("print('ok')"))
    _wait_terminal(runner, job_id)
    info = runner.cancel(job_id)
    assert info["status"] == schema.STATUS_SUCCEEDED


# ── 串行队列（ADR-6：DuckDB 单写者）──────────────────────────────────────────

def test_jobs_run_serially(runner):
    """正例(核心): 任何时刻只允许一个子进程，新任务排队而非并发抢锁"""
    code = ("import time, os\n"
            "p = os.environ['CONCURRENCY_PROBE']\n"
            "open(p, 'a').write('S')\n"
            "time.sleep(0.4)\n"
            "open(p, 'a').write('E')\n")
    probe = runner.jobs_dir() / "probe.txt"
    import os
    os.environ["CONCURRENCY_PROBE"] = str(probe)
    try:
        ids = [runner.submit("fake", _script(code)) for _ in range(3)]
        for job_id in ids:
            _wait_terminal(runner, job_id, timeout=30)
    finally:
        os.environ.pop("CONCURRENCY_PROBE", None)

    # 串行执行的话，标记必然是严格的 SESESE；若并发会出现 SS
    assert probe.read_text() == "SESESE"


def test_queued_job_reports_queue_position(runner):
    """正例: 排队中的任务能报出自己的位置"""
    blocker = runner.submit("fake", _script("import time; time.sleep(1.5)"))
    queued = runner.submit("fake", _script("print('later')"))
    info = runner.get(queued)
    assert info["status"] == schema.STATUS_QUEUED
    assert info["queue_position"] == 0
    _wait_terminal(runner, blocker, timeout=20)
    _wait_terminal(runner, queued, timeout=20)


def test_cancel_queued_job_never_starts_it(runner):
    """正例: 取消排队中的任务，它不该再被执行"""
    marker = runner.jobs_dir() / "should_not_exist.txt"
    blocker = runner.submit("fake", _script("import time; time.sleep(1.0)"))
    queued = runner.submit("fake", _script(f"open({str(marker)!r}, 'w').write('ran')"))
    assert runner.cancel(queued)["status"] == schema.STATUS_CANCELLED
    _wait_terminal(runner, blocker, timeout=20)
    time.sleep(0.5)
    assert not marker.exists(), "已取消的排队任务仍被执行了"


# ── 输出与持久化 ──────────────────────────────────────────────────────────────

def test_output_returns_tail(runner):
    """正例: 输出按尾部返回"""
    code = "\n".join(f"print('line{i}')" for i in range(20))
    job_id = runner.submit("fake", _script(code))
    _wait_terminal(runner, job_id)
    result = runner.output(job_id, tail=5)
    assert result["lines"] == [f"line{i}" for i in range(15, 20)]
    assert result["total_lines"] == 20


def test_output_only_errors_filter(runner):
    """正例: 只看错误行"""
    code = ("print('21:00:00 [etl] [INFO] 正常')\n"
            "print('21:00:01 [etl] [ERROR] 出错了')\n")
    job_id = runner.submit("fake", _script(code))
    _wait_terminal(runner, job_id)
    lines = runner.output(job_id, only_errors=True)["lines"]
    assert len(lines) == 1 and "[ERROR]" in lines[0]


def test_output_of_unknown_job_raises(runner):
    """反例: 未知任务要明确报错"""
    with pytest.raises(runner_mod.JobNotFound):
        runner.output("nosuch-job")


def test_job_metadata_persisted(run_dir, runner):
    """正例: 任务元数据落盘，服务重启后历史仍可查"""
    job_id = runner.submit("fake", _script("print('ok')"))
    _wait_terminal(runner, job_id)
    records = runner_mod.load_persisted_jobs(run_dir)
    assert any(r["job_id"] == job_id and r["status"] == schema.STATUS_SUCCEEDED
               for r in records)


def test_load_persisted_jobs_skips_corrupt_file(run_dir, runner):
    """反例: 单条坏记录不该拖垮整个列表"""
    job_id = runner.submit("fake", _script("print('ok')"))
    _wait_terminal(runner, job_id)
    (run_dir / "broken.json").write_text("{ not json", encoding="utf-8")
    records = runner_mod.load_persisted_jobs(run_dir)
    assert any(r["job_id"] == job_id for r in records)


# ── 列表 ──────────────────────────────────────────────────────────────────────

def test_list_puts_stalled_first(runner):
    """正例: stalled 置顶——它是最需要人看一眼的状态"""
    ok = runner.submit("fake", _script("print('ok')"))
    _wait_terminal(runner, ok)
    stuck = runner.submit("fake", _script("import time; print('s'); time.sleep(30)"),
                          stall_timeout=1, max_runtime=60)
    _wait_status(runner, stuck, schema.STATUS_STALLED, timeout=10)
    assert runner.list_jobs()[0]["job_id"] == stuck
    runner.cancel(stuck, force=True)


def test_list_filters_by_status(runner):
    """正例: 按状态过滤"""
    job_id = runner.submit("fake", _script("print('ok')"))
    _wait_terminal(runner, job_id)
    assert [j["job_id"] for j in runner.list_jobs(status=schema.STATUS_SUCCEEDED)] == [job_id]
    assert runner.list_jobs(status=schema.STATUS_STALLED) == []


def test_get_unknown_job_raises(runner):
    """反例: 未知任务 id"""
    with pytest.raises(runner_mod.JobNotFound):
        runner.get("nosuch-job")


# ── 内联等待 ──────────────────────────────────────────────────────────────────

def test_wait_returns_result_for_short_job(runner):
    """正例: 短任务在 wait_seconds 内跑完，直接出结果"""
    job_id = runner.submit("fake", _script("print('quick')"))
    info = runner.wait(job_id, timeout=15)
    assert info["status"] == schema.STATUS_SUCCEEDED


def test_wait_times_out_for_long_job(runner):
    """正例: 长任务超时返回当前状态，交由调用方转后台"""
    job_id = runner.submit("fake", _script("import time; print('x'); time.sleep(10)"),
                           stall_timeout=30, max_runtime=60)
    info = runner.wait(job_id, timeout=0.5)
    assert info["status"] not in schema.TERMINAL_STATUSES
    runner.cancel(job_id, force=True)


# ── 失败重试（M4）────────────────────────────────────────────────────────────
# retries 是分片续跑的一半：单段失败自动再试，省掉一轮人工往返。

def test_no_retry_by_default(runner):
    """正例: 默认不重试，只跑一次"""
    job_id = runner.submit("fake", _script("import sys; sys.exit(1)"))
    info = _wait_terminal(runner, job_id)
    assert info["status"] == schema.STATUS_FAILED
    assert info["attempt"] == 1
    assert info["attempt_history"] == []


def test_retry_exhausts_all_attempts(runner):
    """正例: 始终失败时跑满 retries+1 次，并留下每次的记录"""
    job_id = runner.submit("fake", _script("import sys; sys.exit(1)"), retries=2)
    info = _wait_terminal(runner, job_id, timeout=30)
    assert info["status"] == schema.STATUS_FAILED
    assert info["attempt"] == 3
    assert info["max_attempts"] == 3
    assert [h["attempt"] for h in info["attempt_history"]] == [1, 2]
    assert all(h["exit_code"] == 1 for h in info["attempt_history"])


def test_retry_succeeds_on_second_attempt(runner, run_dir):
    """正例(核心): 第一次失败、第二次成功 → 最终状态是 succeeded"""
    marker = run_dir / "attempt.txt"
    code = (f"import os, sys\n"
            f"p = {str(marker)!r}\n"
            f"n = len(open(p).read()) if os.path.exists(p) else 0\n"
            f"open(p, 'a').write('x')\n"
            f"print(f'attempt {{n+1}}')\n"
            f"sys.exit(1 if n == 0 else 0)\n")
    job_id = runner.submit("fake", _script(code), retries=1)
    info = _wait_terminal(runner, job_id, timeout=30)
    assert info["status"] == schema.STATUS_SUCCEEDED
    assert info["attempt"] == 2
    assert len(info["attempt_history"]) == 1


def test_wait_does_not_return_between_retries(runner):
    """反例(关键): wait 不得在重试排队的空隙里返回，把还要再跑的任务报成最终失败"""
    code = "import sys, time; time.sleep(0.2); sys.exit(1)"
    job_id = runner.submit("fake", _script(code), retries=2)
    info = runner.wait(job_id, timeout=30)
    assert info["status"] in schema.TERMINAL_STATUSES
    assert info["attempt"] == 3, f"wait 提前返回了，attempt={info['attempt']}"


def test_cancelled_job_is_not_retried(runner):
    """反例(关键): 人工取消是人的决定，不得被自动重试推翻"""
    code = "import time; print('x'); time.sleep(30)"
    job_id = runner.submit("fake", _script(code), retries=3,
                           stall_timeout=30, max_runtime=60)
    _wait_status(runner, job_id, schema.STATUS_RUNNING, timeout=10)
    runner.cancel(job_id, force=True)
    info = _wait_terminal(runner, job_id, timeout=15)
    assert info["status"] == schema.STATUS_CANCELLED
    assert info["attempt"] == 1


def test_killed_stalled_job_is_retried(runner):
    """正例: 卡死后被终止属于可重试——这正是断点续跑要覆盖的场景"""
    code = "import time; print('start'); time.sleep(30)"
    job_id = runner.submit("fake", _script(code), retries=1,
                           stall_timeout=30, max_runtime=1)
    info = _wait_terminal(runner, job_id, timeout=30)
    assert info["status"] == schema.STATUS_KILLED_STALLED
    assert info["attempt"] == 2, "被判卡死终止后应重试一次"


def test_retry_resets_progress_state(runner):
    """正例: 重试时进度与行数要归零，不能把上一次的残留混进来"""
    code = ("import sys\n"
            "print('   已处理: 7/10')\n"
            "sys.exit(1)\n")
    job_id = runner.submit("fake", _script(code), retries=1)
    info = _wait_terminal(runner, job_id, timeout=30)
    assert info["attempt"] == 2
    assert info["line_count"] == 1, "行数应只统计最后一次尝试"


# ── 批次查询 ──────────────────────────────────────────────────────────────────

def test_batch_returns_jobs_in_submit_order(runner):
    """正例: 同批次任务按提交顺序返回，便于对上分片区间"""
    ids = [runner.submit("fake", _script("print('ok')"),
                         meta={"batch_id": "B1", "segment_index": i})
           for i in range(3)]
    for job_id in ids:
        _wait_terminal(runner, job_id, timeout=30)
    assert [j["job_id"] for j in runner.batch("B1")] == ids


def test_batch_ignores_other_batches(runner):
    """反例: 不同批次不得串味"""
    a = runner.submit("fake", _script("print('a')"), meta={"batch_id": "B1"})
    b = runner.submit("fake", _script("print('b')"), meta={"batch_id": "B2"})
    for job_id in (a, b):
        _wait_terminal(runner, job_id, timeout=30)
    assert [j["job_id"] for j in runner.batch("B1")] == [a]


def test_batch_unknown_id_returns_empty(runner):
    """反例: 未知批次返回空列表，不抛异常"""
    assert runner.batch("nosuch") == []


def test_retrying_job_never_publishes_terminal_status(run_dir):
    """反例(关键竞态): 还要重试的任务，任何时刻都不得对外呈现为终态。

    暴露了的话，模型调 get_job 看到 failed 就以为结束了，据此去「定向重跑」——
    而执行器自己正要重跑，于是跑了两遍。

    这里挂钩 _persist 来采样而不是轮询：真实窗口只有落盘那一瞬(约 1ms)，
    轮询几乎必然错过，那样的测试等于没测。_persist 同时也是落盘记录的写入点，
    因此这条断言连带覆盖了「重启后从 JSON 读到的历史状态也不能是假终态」。
    """
    samples: list[tuple[int, str]] = []
    original = Runner._persist

    def spy(self, job):
        samples.append((job.attempt, job.status))
        return original(self, job)

    with patch.object(Runner, "_persist", spy):
        r = Runner(jobs_dir=run_dir, cwd=run_dir, poll_interval=0.05,
                   terminate_grace=1.0)
        try:
            job_id = r.submit("fake", _script("import sys; sys.exit(1)"), retries=2)
            info = _wait_terminal(r, job_id, timeout=30)
        finally:
            r.shutdown()

    assert info["attempt"] == 3
    premature = [(a, st) for a, st in samples
                 if a < 3 and st in schema.TERMINAL_STATUSES]
    assert not premature, f"还要重试却已呈现为终态: {premature}"
    assert (3, schema.STATUS_FAILED) in samples, "最终失败状态应被落盘"


def test_status_during_termination_is_not_terminal_when_retry_pending(run_dir):
    """反例(关键竞态·另一半): 「正在终止」的那段时间不得对外呈现为终态。

    终止流程是 SIGTERM → 等宽限期 → SIGKILL，最长可达数秒。若监控线程在发起终止
    **之前**就把 job.status 写成 killed_stalled，这几秒里 get_job 看到的就是终态——
    而此刻可能还要重试。调用方据此会以为任务结束、自行去补数，与执行器的重试撞车。

    这里挂钩 _stop_process 采样，因为那正是窗口所在；轮询版本只能靠撞，
    实测约 40% 漏判（`test_killed_stalled_job_is_retried` 曾因此偶发红）。
    """
    samples: list[tuple[int, str]] = []
    original = Runner._stop_process

    def spy(self, job, *, force, reason):
        with self._lock:
            samples.append((job.attempt, job.status))
        return original(self, job, force=force, reason=reason)

    with patch.object(Runner, "_stop_process", spy):
        r = Runner(jobs_dir=run_dir, cwd=run_dir, poll_interval=0.05,
                   terminate_grace=1.0)
        try:
            job_id = r.submit(
                "fake", _script("import time; print('start'); time.sleep(30)"),
                retries=1, stall_timeout=30, max_runtime=1)
            info = _wait_terminal(r, job_id, timeout=40)
        finally:
            r.shutdown()

    assert info["status"] == schema.STATUS_KILLED_STALLED
    assert info["attempt"] == 2, "超硬上限被终止属于可重试，应跑满两次"
    assert samples, "应至少发起过一次终止"

    premature = [(a, st) for a, st in samples
                 if a < 2 and st in schema.TERMINAL_STATUSES]
    assert not premature, f"终止过程中过早呈现终态: {premature}"
