# 修改记录:
#   2026-08-19  Claude  新建：子进程调度 + 心跳监控 + 串行队列 + 状态持久化 + 取消
#   2026-08-19  Claude  新增失败自动重试与批次查询，支撑分片断点续跑
"""ETL 子进程执行器。

设计要点（对应文档 ADR-2/3/6 与第五节）：

* **subprocess 而非进程内调用**：ETL 卡死时必须能 kill 掉它而不拖死本服务。
* **串行队列**：DuckDB 单写者，任何时刻只放行一个 ETL 子进程，新任务排队而非抢锁。
* **心跳判定卡死**：退出码解决不了卡死——进程不退出就没有退出码。reader 线程
  逐行读子进程输出并打时间戳，静默超过 stall_timeout 即判 `stalled`。
* **`stalled` 不是终态**：先报警不动手，由人或模型决定继续等还是杀；
  只有超过 max_runtime 硬上限才自动 terminate → 宽限 → kill。
* **强制无缓冲**：子进程环境固定注入 PYTHONUNBUFFERED=1，配合 argv 里的 `-u`
  形成双保险。少了它，pipe 的块缓冲会攒着不吐，正常运行会被误判为卡死（文档 5.3）。
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import schema

# 子进程被 SIGTERM 之后，等它自己收尾的宽限时间；超时改用 SIGKILL
TERMINATE_GRACE_SECONDS = 5.0
# 监控循环的轮询间隔；只影响卡死判定的时间分辨率，不影响输出实时性（那是 reader 线程的事）
POLL_INTERVAL = 0.2


class JobNotFound(KeyError):
    pass


class Job:
    """一次 ETL 执行。

    可变状态由 Runner 持有的锁保护；`_`前缀字段不落盘。
    """

    def __init__(self, job_id: str, program: str, argv: list[str],
                 stall_timeout: int, max_runtime: int,
                 meta: dict | None = None, retries: int = 0):
        self.job_id = job_id
        self.program = program
        self.argv = list(argv)
        self.stall_timeout = stall_timeout
        self.max_runtime = max_runtime
        self.meta = dict(meta or {})
        self.attempt = 1
        self.max_attempts = max(1, retries + 1)
        self.attempt_history: list[dict] = []

        self.status = schema.STATUS_QUEUED
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.last_line: str = ""
        self.progress: dict | None = None
        self.error: str | None = None
        self.line_count = 0

        self._proc: subprocess.Popen | None = None
        self._start_mono: float | None = None
        self._last_output_mono: float | None = None
        self._cancel_requested = False
        self._cancel_force = False
        self._done = threading.Event()

    # -- 派生量 ------------------------------------------------------------

    def idle_seconds(self) -> float | None:
        """距上次输出多久。未开始或已结束返回 None。"""
        if self._last_output_mono is None or self.status in schema.TERMINAL_STATUSES:
            return None
        return round(time.monotonic() - self._last_output_mono, 1)

    def elapsed_seconds(self) -> float | None:
        if self._start_mono is None:
            return None
        if self.finished_at is not None and self.started_at is not None:
            return round(self.finished_at - self.started_at, 1)
        return round(time.monotonic() - self._start_mono, 1)

    def to_dict(self, queue_position: int | None = None) -> dict:
        return {
            "job_id": self.job_id,
            "program": self.program,
            "status": self.status,
            "exit_code": self.exit_code,
            "exit_hint": (schema.describe_exit_code(self.exit_code)
                          if self.exit_code is not None else None),
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "elapsed_seconds": self.elapsed_seconds(),
            "idle_seconds": self.idle_seconds(),
            "stall_timeout": self.stall_timeout,
            "max_runtime": self.max_runtime,
            "last_line": self.last_line,
            "line_count": self.line_count,
            "progress": self.progress,
            "queue_position": queue_position,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "attempt_history": self.attempt_history,
            "error": self.error,
            "argv": self.argv,
            "meta": self.meta,
        }


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def parse_progress(line: str) -> dict | None:
    """从进度行抽出结构化进度，形如「   已处理: 3400/5400」。

    匹配不上返回 None——日志格式变了不该让整个任务崩掉（文档 十、日志层反例）。
    """
    match = schema.PROGRESS_RE.search(line)
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    percent = round(done * 100.0 / total, 1) if total else None
    return {"done": done, "total": total, "percent": percent}


class Runner:
    """串行执行 ETL 子进程，并持续监控其存活状态。"""

    def __init__(self, jobs_dir: Path | str | None = None,
                 cwd: Path | str | None = None,
                 poll_interval: float = POLL_INTERVAL,
                 terminate_grace: float = TERMINATE_GRACE_SECONDS):
        """jobs_dir / cwd 留空则在用到时从环境变量解析，import 本模块不需要环境就绪。"""
        self._cwd = Path(cwd) if cwd else None
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: list[str] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._poll_interval = poll_interval
        self._terminate_grace = terminate_grace
        self._jobs_dir = Path(jobs_dir) if jobs_dir else None
        self._worker = threading.Thread(target=self._work_loop, name="etl-runner",
                                        daemon=True)
        self._worker.start()

    # -- 目录 --------------------------------------------------------------

    def jobs_dir(self) -> Path:
        path = self._jobs_dir if self._jobs_dir else schema.jobs_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _meta_path(self, job_id: str) -> Path:
        return self.jobs_dir() / f"{job_id}.json"

    def _out_path(self, job_id: str) -> Path:
        return self.jobs_dir() / f"{job_id}.out"

    # -- 提交与查询 --------------------------------------------------------

    def submit(self, program: str, argv: list[str], *,
               stall_timeout: int | None = None,
               max_runtime: int | None = None,
               meta: dict | None = None,
               retries: int = 0) -> str:
        job_id = f"{program}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        job = Job(
            job_id=job_id,
            program=program,
            argv=argv,
            stall_timeout=stall_timeout if stall_timeout is not None
                          else schema.STALL_TIMEOUT_SHORT,
            max_runtime=max_runtime if max_runtime is not None
                        else schema.max_runtime_default(),
            meta=meta,
            retries=retries,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._pending.append(job_id)
        self._persist(job)
        self._queue.put(job_id)
        return job_id

    def _get_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"任务不存在: {job_id}")
        return job

    def get(self, job_id: str) -> dict:
        job = self._get_job(job_id)
        with self._lock:
            position = (self._pending.index(job_id)
                        if job_id in self._pending else None)
            return job.to_dict(queue_position=position)

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """列出任务。`stalled` 置顶——它是最需要人看一眼的状态。"""
        with self._lock:
            jobs = [self._jobs[j] for j in reversed(self._order)]
            pending = list(self._pending)
        if status:
            jobs = [j for j in jobs if j.status == status]
        jobs.sort(key=lambda j: 0 if j.status == schema.STATUS_STALLED else 1)
        return [
            j.to_dict(queue_position=pending.index(j.job_id)
                      if j.job_id in pending else None)
            for j in jobs[:limit]
        ]

    def wait(self, job_id: str, timeout: float) -> dict:
        """内联等待至多 timeout 秒；超时就返回当前状态，交由调用方转后台。"""
        job = self._get_job(job_id)
        job._done.wait(timeout)
        return self.get(job_id)

    def output(self, job_id: str, tail: int = 200,
               only_errors: bool = False) -> dict:
        self._get_job(job_id)
        path = self._out_path(job_id)
        if not path.exists():
            return {"job_id": job_id, "lines": [], "truncated": False,
                    "note": "尚无输出"}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        if only_errors:
            lines = [l for l in lines if "[ERROR]" in l or "[WARNING]" in l]
        selected = lines[-tail:] if tail and tail > 0 else lines
        return {
            "job_id": job_id,
            "lines": selected,
            "total_lines": total,
            "truncated": len(selected) < len(lines),
        }

    def cancel(self, job_id: str, force: bool = False) -> dict:
        """人工取消。先 SIGTERM；force=True 时宽限期后补 SIGKILL。"""
        job = self._get_job(job_id)
        with self._lock:
            if job.status in schema.TERMINAL_STATUSES:
                return job.to_dict()
            job._cancel_requested = True
            job._cancel_force = force
            if job.status == schema.STATUS_QUEUED:
                # 还没轮到它，直接标记取消，工作线程取到时会跳过
                job.status = schema.STATUS_CANCELLED
                job.finished_at = time.time()
                if job_id in self._pending:
                    self._pending.remove(job_id)
                job._done.set()
                self._persist(job)
                return job.to_dict()
            proc = job._proc
        if proc is not None and proc.poll() is None:
            self._stop_process(job, force=force, reason="人工取消")
        return self.get(job_id)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        self._worker.join(timeout)

    # -- 工作线程 ----------------------------------------------------------

    def _work_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                job = self._get_job(job_id)
            except JobNotFound:
                continue
            with self._lock:
                if job_id in self._pending:
                    self._pending.remove(job_id)
                skip = job.status == schema.STATUS_CANCELLED
            if skip:
                continue
            try:
                self._run(job)
            except Exception as e:                      # noqa: BLE001 兜底，工作线程不能死
                with self._lock:
                    job.status = schema.STATUS_FAILED
                    job.error = f"执行器内部错误: {e}"
                    job.finished_at = time.time()
                    job._done.set()
                self._persist(job)

    def _run(self, job: Job) -> None:
        out_path = self._out_path(job.job_id)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        with self._lock:
            job.status = schema.STATUS_RUNNING
            job.started_at = time.time()
            job._start_mono = time.monotonic()
            job._last_output_mono = time.monotonic()
        self._persist(job)

        try:
            proc = subprocess.Popen(
                job.argv,
                cwd=str(self._cwd or schema.spring_dir()),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,          # 行缓冲读取；与 -u / PYTHONUNBUFFERED 配套
                shell=False,        # 绝不拼 shell 字符串
            )
        except OSError as e:
            with self._lock:
                job.status = schema.STATUS_FAILED
                job.error = f"无法启动子进程: {e}"
                job.finished_at = time.time()
                job._done.set()
            self._persist(job)
            return

        with self._lock:
            job._proc = proc

        reader = threading.Thread(
            target=self._read_output, args=(job, proc, out_path),
            name=f"reader-{job.job_id}", daemon=True,
        )
        reader.start()

        self._monitor(job, proc)
        reader.join(timeout=2.0)

        code = proc.poll()
        with self._lock:
            job.exit_code = code
            if job.status in (schema.STATUS_CANCELLED, schema.STATUS_KILLED_STALLED):
                outcome = job.status
            else:
                outcome = schema.status_from_exit_code(code if code is not None else -1)

            # 判定与状态落定必须在同一把锁里一次做完。分开做会留下一个窗口，
            # 让 wait() 或 get_job() 看到一个「其实还要再跑」的终态——
            # 对模型来说那就是任务已经失败了，会据此做出错误决策。
            should_retry = (outcome in schema.RETRYABLE_STATUSES
                            and job.attempt < job.max_attempts)
            if should_retry:
                self._reset_for_retry_locked(job, outcome)
            else:
                job.status = outcome
                job.finished_at = time.time()
                job._done.set()
        self._persist(job)
        if should_retry:
            # 重排到队尾而非插队：串行队列里其他任务不该被一个反复失败的任务饿死；
            # 对分片批次而言，坏的那一段挪到最后重试也更合理。
            self._queue.put(job.job_id)

    def _read_output(self, job: Job, proc: subprocess.Popen, out_path: Path) -> None:
        """逐行读取并打时间戳。这是心跳的唯一来源。"""
        try:
            with out_path.open("a", encoding="utf-8") as sink:
                for line in proc.stdout:            # 行迭代，配合 -u 实现行级实时
                    text = line.rstrip("\n")
                    sink.write(line if line.endswith("\n") else line + "\n")
                    sink.flush()
                    progress = parse_progress(text)
                    with self._lock:
                        job._last_output_mono = time.monotonic()
                        job.last_line = text
                        job.line_count += 1
                        if progress is not None:
                            job.progress = progress
                        # 有输出即视为恢复：stalled 是可回退的非终态
                        if job.status == schema.STATUS_STALLED:
                            job.status = schema.STATUS_RUNNING
        except Exception:                            # noqa: BLE001 读取失败不应拖死任务
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:                        # noqa: BLE001
                pass

    def _monitor(self, job: Job, proc: subprocess.Popen) -> None:
        while proc.poll() is None:
            time.sleep(self._poll_interval)
            now = time.monotonic()
            with self._lock:
                if job._cancel_requested:
                    force = job._cancel_force
                    break_reason = "人工取消"
                    should_stop = True
                    new_status = schema.STATUS_CANCELLED
                else:
                    should_stop = False
                    force = False
                    break_reason = ""
                    new_status = ""
                    elapsed = now - (job._start_mono or now)
                    idle = now - (job._last_output_mono or now)

                    if elapsed > job.max_runtime:
                        should_stop = True
                        force = True
                        break_reason = f"超过 max_runtime={job.max_runtime}s"
                        new_status = schema.STATUS_KILLED_STALLED
                    elif idle > job.stall_timeout and job.status == schema.STATUS_RUNNING:
                        # 只报警不动手：由人或模型决定继续等还是杀
                        job.status = schema.STATUS_STALLED

            if should_stop:
                with self._lock:
                    job.status = new_status
                    job.error = break_reason
                self._persist(job)
                self._stop_process(job, force=force, reason=break_reason)
                return

    def _stop_process(self, job: Job, *, force: bool, reason: str) -> None:
        proc = job._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()                        # 先 SIGTERM，给它收尾的机会
        except Exception:                           # noqa: BLE001
            return
        try:
            proc.wait(timeout=self._terminate_grace)
            return
        except subprocess.TimeoutExpired:
            pass
        if force:
            try:
                proc.kill()                         # SIGTERM 无效时才 SIGKILL
                proc.wait(timeout=self._terminate_grace)
            except Exception:                       # noqa: BLE001
                pass

    def _reset_for_retry_locked(self, job: Job, outcome: str) -> None:
        """记录本次尝试的结果并把任务重置回排队态。调用方必须已持有锁。

        状态直接从「跑完」跳到 queued，中间**不经过终态**——对外永远看不到
        一个还要再跑的任务被标成失败。上一次的结果留在 attempt_history 里。
        """
        job.attempt_history.append({
            "attempt": job.attempt,
            "status": outcome,
            "exit_code": job.exit_code,
            "last_line": job.last_line,
            "error": job.error,
        })
        job.attempt += 1
        job.status = schema.STATUS_QUEUED
        job.exit_code = None
        job.error = None
        job.started_at = None
        job.finished_at = None
        job.last_line = ""
        job.line_count = 0
        job.progress = None
        job._proc = None
        job._start_mono = None
        job._last_output_mono = None
        self._pending.append(job.job_id)

    def batch(self, batch_id: str) -> list[dict]:
        """取出同一批次(分片)的全部任务，按提交顺序。"""
        with self._lock:
            ids = [j for j in self._order
                   if self._jobs[j].meta.get("batch_id") == batch_id]
            pending = list(self._pending)
            return [self._jobs[j].to_dict(
                        queue_position=pending.index(j) if j in pending else None)
                    for j in ids]

    # -- 持久化 ------------------------------------------------------------

    def _persist(self, job: Job) -> None:
        """任务元数据落盘，服务重启后历史仍可查。落盘失败不影响执行。"""
        try:
            path = self._meta_path(job.job_id)
            with self._lock:
                payload = job.to_dict()
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception:                           # noqa: BLE001
            pass


def load_persisted_jobs(jobs_dir: Path | str | None = None) -> list[dict]:
    """读取历史任务记录（本服务重启前的任务）。"""
    path = Path(jobs_dir) if jobs_dir else schema.jobs_dir()
    if not path.is_dir():
        return []
    records: list[dict] = []
    for meta in sorted(path.glob("*.json"), reverse=True):
        try:
            records.append(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:                           # noqa: BLE001 单条坏记录不该拖垮列表
            continue
    return records
