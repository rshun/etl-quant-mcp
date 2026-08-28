# Usage Guide

Detailed reference for all 13 MCP tools provided by **quant-etl**.

> 中文版：[USAGE.zh-CN.md](USAGE.zh-CN.md)
> For installation, see [INSTALL.md](INSTALL.md)

---

## 1. Overview

| Category | Tools | Writes to the database? |
|---|---|---|
| **Execution** | `etl_import_daily` `etl_adjust` `etl_fetch_index` `etl_fill_indicators` | Yes |
| **Job management** | `list_jobs` `get_job` `get_job_output` `cancel_job` | No (`cancel_job` terminates a process) |
| **Logs** | `list_etl_logs` `read_etl_log` `summarize_etl_log` | No |
| **Checking & introspection** | `check_data_gaps` `describe_etl_program` | No |

You do not need to memorise tool names — describe what you want in plain
language and the assistant picks the tool. This guide is here so you know
**what is possible, where the limits are, and how to read the results**.

### The three most common tasks

```
Did last night's ETL hang?           → summarize_etl_log
Is any data missing recently?        → check_data_gaps
Re-download 600519 for yesterday     → etl_import_daily
```

---

## 2. Concepts you need first

Understand this section and the 13 tools explain themselves.

### 2.1 Job model: wait briefly, then go to the background

An ETL run can take tens of minutes, far longer than a single MCP call can wait.
So every execution tool behaves like this:

1. Submit the job and **wait synchronously** for up to `wait_seconds` (default 30).
2. If it finishes in time, return the complete result.
3. Otherwise return a `job_id`; the job keeps running in the background.

With a `job_id` you can use `get_job` for progress, `get_job_output` for output,
and `cancel_job` to stop it.

### 2.2 Serial queue

**Only one ETL subprocess runs at a time**, because DuckDB permits only one
writer. Submitting several jobs does not make them run in parallel — they queue,
and `get_job` reports `queue_position`.

> This queue governs only jobs started by this service. It has **no control over
> `cron`**. Avoid triggering writes during the nightly window; the two would
> compete for the same database lock.

### 2.3 State machine

```
queued → running → ┬→ succeeded       success
                   ├→ failed          failure
                   ├→ partial         partial success (reserved; not produced yet)
                   ├→ stalled         process alive but silent past the threshold ⚠
                   ├→ killed_stalled  terminated for exceeding max_runtime
                   └→ cancelled       stopped by a human
```

**`stalled` is not a final state.** It means "the process is alive but has been
quiet for a while" — it is an **alarm, not a verdict**:

- the job may recover on its own (long date ranges naturally have long gaps
  between progress lines), and it returns to `running` automatically;
- or it really has hung, in which case use `cancel_job` and re-run just the
  remaining part, guided by `last_line`.

Only exceeding the `max_runtime` ceiling causes automatic termination
(`killed_stalled`).

The five final states are `succeeded`, `partial`, `failed`, `killed_stalled`,
and `cancelled`.

### 2.4 Stall thresholds

How long silence must last before a job is called `stalled`, chosen by date span:

| Span | Default threshold |
|---|---|
| ≤ 7 days | 90 seconds |
| > 7 days | 600 seconds |

Override per call with `stall_timeout`.

### 2.5 Exit codes

| Code | Status | Meaning |
|---|---|---|
| `0` | `succeeded` | Success |
| `1` | `failed` | Failure |
| `2` | `failed` | **Command-line usage error** — hardcoded in Python's standard library, *not* "partial success" |
| `3` | `partial` | Partial success (reserved; spring does not produce it yet) |
| anything else | `failed` | Unknown codes are treated as failures; never interpreted optimistically |

### 2.6 Parameter formats

| Parameter | Format | Example |
|---|---|---|
| `begin` / `end` | `YYYYMMDD`, must be a real calendar date | `20260817` |
| `codes` | six digits, optional `.SH` / `.SZ` / `.BJ` suffix | `["600519", "000001.SZ"]` |
| `exchanges` | `sh` / `sz` / `bj` / `all` (case-insensitive) | `["sh", "sz"]` |

Invalid values are rejected **before any subprocess starts**, with a message
naming the parameter and the reason.

### 2.7 Data sources

| Program | Available `source` |
|---|---|
| `etl_import_daily` | `lday` / `bstock` / `tdx` (default `bstock`) |
| `etl_fetch_index` | `lday` / `bstock` (default `bstock`) |
| `etl_adjust` | `bstock` only |

> `lday` and `tdx` require a Windows TDX installation directory. **On a Linux
> server only `bstock` is usable.**

---

## 3. Execution tools

All four accept this common set of MCP-level parameters:

| Parameter | Default | Purpose |
|---|---|---|
| `wait_seconds` | 30 | How long to wait synchronously before going to the background |
| `stall_timeout` | auto by span | Silence before the job is reported `stalled` |
| `max_runtime` | 7200 | Hard ceiling; the job is terminated past it |
| `retries` | 0 | Automatic retries on failure, capped at 5 |
| `chunk` | `none` | `month` / `year` splitting (the three download tools only) |

**About `retries`:** only `failed` and `killed_stalled` are retried. Not
`cancelled` — that was a human decision. Not `partial` — re-running the whole
range is wasteful; target the gaps instead.

**About `chunk`:** a long range is split on calendar month or year boundaries and
the segments run one after another. A hang then costs you one segment instead of
the whole night: `failed_segments` in the result tells you which ones to redo.
Ranges longer than **3650 days** are rejected unless you chunk them.

---

### `etl_import_daily`

Download daily stock quotes into the database.

**Parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `begin` | string | **required** | Start date, `YYYYMMDD` |
| `end` | string | same as `begin` | Omit to run a single day |
| `codes` | array | whole market | Specific stock codes |
| `exchanges` | array | `["all"]` | Exchange filter; `codes` takes precedence |
| `source` | string | `bstock` | `lday` / `bstock` / `tdx` |
| `print_only` | bool | `false` | **Dry run**: performs the full download, writes nothing |

**When to use it:** filling in a missing day of quotes. This is the workhorse of
the gap-filling loop.

**Note:** `print_only=true` is the safest way to try this service. It genuinely
downloads but does not write a single byte to the database. Use it first.

---

### `etl_adjust`

Download adjustment factors and expand them to every trading day.

**Parameters:** as above, except `source` can only be `bstock` and there is no
`print_only`.

**Note:** **run it even when no adjustment events occurred in the range.** It is
also responsible for forward-filling the `ADJ_FACTOR` table up to `end`. "No
dividends this week, skip it" is the wrong instinct.

---

### `etl_fetch_index`

Download index quotes into the database.

**Parameters:** as `etl_import_daily`, but `source` is `lday` / `bstock` only and
there is no `print_only`. `codes` takes **index** codes, e.g. `["000001"]` for
the SSE Composite.

---

### `etl_fill_indicators`

Fill three families of derived indicators.

**Parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `begin` / `end` / `codes` / `exchanges` | | | As above |
| `forcerun` | bool | `false` | Run even on a non-trading day |
| `targets` | array | all three | Subset selection, see below |

`targets` may contain `fill_volratio`, `update_limit`, and `fill_shares`.
**Whatever order you pass, execution is always
`fill_volratio → update_limit → fill_shares`** — the order is fixed so
dependencies cannot be inverted.

**Prerequisite:** all three depend on that day's quotes, so
**`etl_import_daily` must run first**. `fill_shares` additionally needs data in
the `CAPITAL_DETAIL` table, or newly listed stocks are skipped.

**Different result shape:** it creates one job per target, so it returns
`{"targets": [...], "jobs": [...]}` rather than a single `job_id`.

---

## 4. Job management

### `list_jobs`

Lists jobs, newest first, with **`stalled` always at the top**.

| Parameter | Default | Notes |
|---|---|---|
| `status` | all | Filter by state |
| `limit` | 20 | How many to return |

The result includes `needs_attention`, a plain list of job ids worth looking at
(`stalled`, `failed`, `partial`, `killed_stalled`).

---

### `get_job`

Details for one job. This is the **primary tool for judging a hang**.

Key fields:

| Field | Meaning |
|---|---|
| `status` | State from the state machine |
| `status_hint` | One sentence in plain language; for `stalled` it spells out your two options |
| `idle_seconds` | **Seconds since the last output** — the core evidence for a hang |
| `progress` | Structured progress `{done, total, percent}` |
| `last_line` | Last line of output, usually `已处理: 3400/5400` |
| `elapsed_seconds` | Time spent so far |
| `queue_position` | Position in the queue while `queued` |
| `attempt` / `max_attempts` | Which attempt this is, when `retries` is used |
| `exit_code` / `exit_hint` | Exit code and its interpretation |

---

### `get_job_output`

Tail of a job's subprocess output.

| Parameter | Default | Notes |
|---|---|---|
| `job_id` | **required** | |
| `tail` | 200 | Lines to return |
| `only_errors` | `false` | Keep only ERROR / WARNING lines |

When a job fails, start with `only_errors=true`; it usually pinpoints the cause
immediately.

---

### `cancel_job`

Terminate a job.

| Parameter | Default | Notes |
|---|---|---|
| `job_id` | **required** | |
| `force` | `false` | Send `SIGKILL` after the grace period |

`SIGTERM` is sent first, followed by a grace period; only `force=true` escalates.

**Why cancelling is safe:** the three download programs hold **no database write
lock during the download phase**, and the download phase is precisely the one
that can hang.

A job cancelled while still `queued` never runs.

---

## 5. Logs

These three tools **complement** the heartbeat in `get_job`: the heartbeat only
sees jobs this service started, so **last night's `cron` run can only be
reconstructed from the logs**.

### `list_etl_logs`

Which days have logs, their sizes and timestamps. `limit` defaults to 30.

There is one file per day (`stockdailyYYYYMMDD.log`), shared by every ETL program
that ran that day.

---

### `read_etl_log`

Read and filter the tail of a log.

| Parameter | Default | Notes |
|---|---|---|
| `date` | today | `YYYYMMDD` |
| `tail` | 200 | Lines to return |
| `level` | all | Exact match on `ERROR` / `WARNING` / `INFO` |
| `module` | all | **Substring** match; `import_daily` matches `etl.import_daily` |
| `keyword` | all | Substring match on the whole line |
| `max_bytes` | 200000 | Only this many bytes from the end are read; excess is flagged `truncated` |

---

### `summarize_etl_log`

**The main tool for deciding whether a night's run hung.** Takes one optional
`date`.

Returns:

| Field | Meaning |
|---|---|
| `level_counts` | Counts per log level |
| `modules` | Breakdown **grouped by module** (one file per day is shared by several programs; without grouping it is unreadable) |
| `stall_suspects` | **Modules suspected of hanging** |
| `recent_errors` | Sample of recent errors |
| `first_line_at` / `last_line_at` / `span_seconds` | Time span |
| `unparsed_lines` | Lines that could not be parsed; non-zero means the log format changed |

#### How `stall_suspects` is decided

> **If a module's last log line is a progress line, it is a suspect.**

A normal completion ends with a "batch collection finished" line; a normal
failure ends with an `ERROR`. Neither present, and the last thing written is
`已处理: 300/5400` — that is exactly what "stopped mid-download" looks like.

**This test does not depend on when you look.** It still holds days later, unlike
"how long since the last update", which decays immediately.

---

## 6. Checking and introspection

### `check_data_gaps`

Find missing data. The verdict is based on **database facts**: expected record
counts are computed from `STOCK_INFO` and `TRADE_CAL`, then compared against what
is actually stored. However the ETL logic changes, this check follows. Suspended
stocks are not counted as missing.

| Parameter | Default | Notes |
|---|---|---|
| `begin` | **required** | |
| `end` | same as `begin` | |
| `codes` / `exchanges` | whole market | Narrow the scope |
| `include_index` | `false` | Also check index quotes |
| `forcerun` | `false` | Check even on a non-trading day |
| `max_detail` | 200 | Cap on `missing_codes` entries |
| `timeout_seconds` | 300 | Check timeout |

**Two places to look in the result:**

- `core.checks[].gap_dates` — which days, and how many rows short → feed into `begin`/`end`
- `core.checks[].missing_codes` — which codes exactly → feed into `codes`

`gap_dates` is always complete (each entry is just a few numbers).
`missing_codes` is capped by `max_detail`; when truncated,
`missing_codes_truncated` is true and the full detail is in the `csv_path` of the
same entry.

**Judge the outcome by `status`** (`complete` / `gaps_found` / `error`), never by
the exit code — the underlying tool uses a different exit-code convention.

**This tool is read-only and does not enter the serial queue**, so a running
backfill will not block it. But DuckDB allows only one writer: if a write job
currently holds the lock, the read-only connection cannot be opened, and the tool
says so explicitly.

---

### `describe_etl_program`

Returns the **current, real** command-line definition of one ETL program.
`name` is required and must be one of `adjust`, `import_daily`, `fetch_index`,
`fill_volratio`, `update_limit`, `fill_shares`.

The result comes straight from spring's own argument parser, so it **can never go
stale**, and `help` text is passed through verbatim.

**Two default-value traps:**

1. For `import_daily`, `adjust`, and `fetch_index`, the `begin`/`end` defaults are
   **the day you asked**, so they change daily.
2. For the three `fill_*` programs, `begin`/`end` default to `null` at the parser
   level; the real defaults (T-1 / today) are applied after parsing, so they are
   **only discoverable from the `help` text**.

---

## 7. Typical workflows

### 7.1 Morning review

```
1. summarize_etl_log            Did any module stop on a progress line?
2. check_data_gaps              Which days and stocks are actually missing?
3. etl_import_daily(codes=…)    Fill exactly those gaps
4. check_data_gaps              Confirm
```

This is what the service is for. The first two steps are read-only and safe to
run any time.

### 7.2 Handling a hang

```
1. get_job                      Is status stalled? Check idle_seconds and progress
2. (judge)                      Long ranges have long gaps — is progress still moving?
3. cancel_job(force=true)       Terminate once you are sure
4. etl_import_daily(codes=…)    Re-run from where last_line left off
```

### 7.3 Large historical backfill

```
etl_import_daily(begin=20200101, end=20261231, chunk="year", retries=1)
```

Split on calendar years, run sequentially, retry a failed segment once. The
returned `failed_segments` names what still needs attention. Without chunking,
anything over 3650 days is rejected outright.

### 7.4 First time trying the service

```
etl_import_daily(begin=<a trading day>, codes=["600519"], print_only=true)
```

A dry run: the full download path, nothing written, back in a couple of seconds.

---

## 8. Limits and boundaries

### Things it cannot do, by design

- **No arbitrary command execution** — only the six allow-listed ETL programs
- **No arbitrary SQL** — there is no query tool (use `quant-mcp` for read-only queries)
- **No dropping or truncating tables**

### Things you have to watch yourself

- **Do not trigger write tools during the nightly window** — they would compete
  with `cron` for the DuckDB write lock. The serial queue cannot see `cron`.
- **On Linux only the `bstock` source works**; `lday` and `tdx` need a Windows
  TDX directory.
- **`etl_fill_indicators` depends on `etl_import_daily`** — wrong order fills in
  nothing useful.
- **The service has no authentication**: whoever reaches the port can trigger
  writes. See section 10 of [INSTALL.md](INSTALL.md).

### About data safety

ETL writes are **idempotent upserts**, so re-running never corrupts data. The
worst case is wasted time and a held lock, not damaged records. That makes the
cost of a mistake considerably lower than it first appears.

---

## 9. Common misconceptions

| Misconception | Reality |
|---|---|
| `stalled` means the job failed | No. It is an **alarm**; the process is alive and may recover. |
| Exit code 2 means partial success | No. 2 is a **command-line usage error**, which is a failure. |
| Skip `etl_adjust` when there were no dividends | Run it. It also forward-fills `ADJ_FACTOR` up to `end`. |
| Submitting several jobs makes them finish sooner | No. One writer only; everything queues. |
| Use `check_data_gaps`'s exit code | Don't. Use the `status` field in the result. |
| `summarize_etl_log` shows jobs this service ran | It does not. It reads the ETL's own log files. Use `list_jobs` for this service's jobs. |
