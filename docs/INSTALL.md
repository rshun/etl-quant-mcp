# Installation Guide

**quant-etl** — an MCP server that lets an AI assistant run and monitor the
ETL pipeline of the [spring](https://github.com/rshun/spring) project.

This guide assumes no prior knowledge of the project. Follow it top to bottom
and you will end up with a working deployment.

> 中文版：[INSTALL.zh-CN.md](INSTALL.zh-CN.md)

---

## 1. What this software does

`spring` is a data pipeline that downloads Chinese A-share market data every
night and stores it in a DuckDB database file. It runs unattended from `cron`.

Its characteristic failure is **not a crash — it is a hang**. The program stops
mid-download and neither reports an error nor exits. Because it never exits,
there is no exit code to inspect; a human has to notice, kill it, and re-run it.

`quant-etl` is a small service that sits next to `spring` and exposes it to an
AI assistant as a set of tools, so the assistant can:

- read last night's logs and judge whether a run hung,
- find which trading days or stocks are missing data,
- re-download exactly the missing pieces,
- watch a running job and stop it if it stalls.

It never runs arbitrary commands or arbitrary SQL. It can only start the six ETL
programs that are named in its allow-list.

---

## 2. How the pieces fit together

```
  ┌────────────────┐        ┌──────────────────┐        ┌──────────────┐
  │  AI assistant  │  HTTP  │   quant-etl      │  spawns│  spring ETL  │
  │  (MCP client)  │───────▶│   MCP server     │───────▶│  programs    │
  └────────────────┘        └──────────────────┘        └──────┬───────┘
      any user                runs as the ETL                  │ writes
                              data owner                       ▼
                                                        ┌──────────────┐
                                                        │  quant.db    │
                                                        └──────────────┘
```

Two transports are supported:

| Transport | When to use |
|---|---|
| **`stdio`** (default) | The AI client and the ETL data belong to the **same** operating-system user. The client launches the server itself; nothing needs to run in the background. |
| **`streamable-http`** | The AI client runs as a **different** user than the ETL data owner. The server runs in the background as the data owner; the client connects over a local port and needs no filesystem access at all. |

If you are unsure, use `streamable-http`. It works in both situations, and it is
what the rest of this guide assumes.

---

## 3. Why the service user matters

**Read this section even if you skip everything else.**

`spring` stores its database path in a configuration file, typically written as
`~/data/quant.db`. The `~` is expanded **by whichever user runs the program**.

That means:

| Process runs as | Database actually written |
|---|---|
| `rshun` (the data owner) | `/home/rshun/data/quant.db` ← the real one |
| any other user `X` | `/home/X/data/quant.db` ← **a different, empty file** |

If the service runs as the wrong user, **nothing appears to go wrong**. DuckDB
creates the missing file, the ETL runs to completion, the exit code is 0, the
logs look normal — and the data silently goes into the wrong place. You may not
notice for weeks.

> **Rule: the service must run as the same user that `cron` uses to run the ETL.**
> Throughout this guide that user is called `rshun`; substitute your own.

This is also precisely why the HTTP transport exists. Your AI client may run as
a completely different user — that is fine, because the client only speaks HTTP.
Only the *server* has to be the data owner.

---

## 4. Prerequisites

Run every command in this section **as the ETL data owner** (`rshun`).

### 4.1 A working `spring` checkout

```bash
ls /home/rshun/src/spring/tools/describe_cli.py
```

If this file is missing, your checkout is on an outdated branch. Update it:

```bash
git -C /home/rshun/src/spring status
git -C /home/rshun/src/spring pull
```

> `config/config.yaml` is tracked by git but its contents are machine-specific.
> If `git pull` reports a conflict there, resolve it by hand and make sure the
> key `progress_heartbeat_seconds` survives — the ETL fails without it.

### 4.2 `spring`'s Python interpreter

```bash
/home/rshun/src/venv_stock/bin/python3 -V
```

You need the path to the **interpreter itself**, not to a wrapper shell script.
If you do not know where it is, look at how `cron` invokes the ETL; the wrapper
script it uses will name the virtualenv.

### 4.3 Python 3.10 or newer for this service

```bash
python3 -V
```

Verified versions: **3.11.2** (Debian 12) and **3.14.3** (macOS). The code needs
3.10+ for modern type-annotation syntax.

If the `venv` module is missing, Debian and Ubuntu need an extra package:

```bash
sudo apt install python3-venv
```

### 4.4 This repository

```bash
git clone https://github.com/rshun/etl-quant-mcp.git /home/rshun/src/etl-quant-mcp
```

---

## 5. Installation

### 5.1 Run the installer

```bash
cd /home/rshun/src/etl-quant-mcp
./install.sh \
    --spring-dir    /home/rshun/src/spring \
    --spring-python /home/rshun/src/venv_stock/bin/python3
```

The installer:

1. checks every prerequisite and stops with a specific message if one is missing,
2. creates a virtual environment at `.venv` and installs the single dependency,
3. starts the server's own self-check and asks `spring` to describe one of its
   programs — proving the two projects can actually talk to each other,
4. writes a systemd unit tailored to your paths into
   `deploy/quant-etl-mcp.service.generated`.

It deliberately **does not** run anything that requires `root`.

To validate your setup without creating anything:

```bash
./install.sh --check --spring-dir ... --spring-python ...
```

### 5.2 Common options

| Option | Default | Notes |
|---|---|---|
| `--port PORT` | `8787` | Change it if something already uses that port. Nothing depends on this number; the client URL just has to match. |
| `--host HOST` | `127.0.0.1` | Leave it. See [Security](#10-security-notes). |
| `--service-user USER` | current user | Must be the ETL data owner. |
| `--service-name NAME` | `quant-etl-mcp` | The systemd unit name. |
| `--venv DIR` | `<repo>/.venv` | |
| `--log-dir DIR` | `<spring-dir>/log` | Where the ETL writes its logs. |
| `--transport MODE` | `streamable-http` | Use `stdio` only if client and server are the same user. |

Run `./install.sh --help` for the full list.

### 5.3 Install the service

Review the generated unit, then run the three commands the installer printed:

```bash
cat deploy/quant-etl-mcp.service.generated
sudo cp deploy/quant-etl-mcp.service.generated /etc/systemd/system/quant-etl-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now quant-etl-mcp
```

---

## 6. Verify the service

### 6.1 It is running

```bash
systemctl status quant-etl-mcp --no-pager
```

Expect `active (running)`.

### 6.2 It is listening on the right address

```bash
ss -ltn | grep -w 8787
```

Expect `127.0.0.1:8787`.

> If you see `0.0.0.0:8787`, **stop and fix the configuration**. That means the
> service is reachable from your whole network, and it has no authentication.

### 6.3 It started cleanly

```bash
journalctl -u quant-etl-mcp -n 20 --no-pager
```

Expect two lines like:

```
[quant-etl] streamable-http 监听 127.0.0.1:8787
INFO:     Uvicorn running on http://127.0.0.1:8787
```

The first line is the server confirming its transport and bind address.
Runtime messages from the server are written in Chinese — see the note at the
start of [Troubleshooting](#9-troubleshooting).

### 6.4 The client user can reach it

Switch to the user that runs your AI client and run:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

Expect `200`.

---

## 7. Connect your AI client

Run this **as the client user**, which may be different from the service user:

```bash
claude mcp add --transport http quant-etl http://127.0.0.1:8787/mcp
```

Check the exact flag names first with `claude mcp add --help`; they vary between
versions. If you prefer to configure it by hand, add this to your MCP client
configuration:

```json
{
  "mcpServers": {
    "quant-etl": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```

Confirm it registered:

```bash
claude mcp list
```

### 7.1 First command to try

Ask the assistant:

> Use summarize_etl_log to check yesterday's ETL log.

This tool only reads log files. It is the safest way to confirm the whole chain
works, and it is also the single most useful thing this service does: it reports
`stall_suspects` — modules whose last log line was a progress line, which is the
signature of a run that hung mid-download.

---

## 8. Configuration reference

All configuration is done through environment variables, which the systemd unit
sets. To change one, edit `/etc/systemd/system/quant-etl-mcp.service`, then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart quant-etl-mcp
```

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `SPRING_DIR` | yes | — | Root of the spring checkout. Used as the working directory for ETL subprocesses. |
| `SPRING_PYTHON` | yes | — | spring's virtualenv interpreter. Must be the interpreter, not a wrapper script. |
| `SPRING_LOG_DIR` | no | `$SPRING_DIR/log` | Where ETL logs live. Job records are stored in `mcp_jobs/` underneath it. |
| `ETL_MCP_TRANSPORT` | no | `stdio` | `stdio`, `streamable-http`, or `sse`. |
| `ETL_MCP_HOST` | no | `127.0.0.1` | Bind address. Non-loopback values print a warning. |
| `ETL_MCP_PORT` | no | `8787` | Listening port. |
| `MAX_RUNTIME_DEFAULT` | no | `7200` | Seconds before a job is killed as runaway. |
| `STALL_TIMEOUT_DEFAULT` | no | auto | Seconds of silence before a job is reported `stalled`. Left unset, the server picks 90 s for short date ranges and 600 s for long ones. |

If a required variable is wrong, **the service fails to start** and says exactly
what is wrong. It does not wait until the first tool call.

### 8.1 Do not point `SPRING_PYTHON` at a wrapper script

Projects often provide a shell wrapper that activates the virtualenv and sets
`PYTHONPATH`. This service does not need it: it names the interpreter directly
and sets the working directory itself. Adding a wrapper only introduces another
layer through which exit codes must survive.

---

## 9. Troubleshooting

Every message below is quoted exactly as this software prints it. The installer
speaks English, but the **server's own runtime messages are in Chinese** — this
guide reproduces them verbatim so you can match them against what you see, with
an English explanation beneath each.

A general rule: configuration mistakes make the service **fail to start** with a
specific message. If `systemctl status` shows the service running, the
configuration is sound.

### `参数自省出口 'tools.describe_cli' 的文件不存在`

*("The parameter-introspection entry point `tools.describe_cli` does not exist")*

Your `spring` checkout does not contain that file, almost always because it is
on an old branch.

```bash
git -C /home/rshun/src/spring status
git -C /home/rshun/src/spring pull
```

### `环境变量 SPRING_DIR 未设置` / `SPRING_PYTHON 不存在`

*("Environment variable SPRING_DIR is not set" / "SPRING_PYTHON does not exist")*

The systemd unit is missing or has a wrong `Environment=` line. Check with:

```bash
systemctl cat quant-etl-mcp
```

### "No module named tools.describe_cli"

Same cause as the first entry — an outdated `spring` checkout. Newer versions of
this service catch it at startup with a clearer message.

### `Address already in use`

Another process holds the port. Choose a different one:

```bash
sudo sed -i 's/ETL_MCP_PORT=8787/ETL_MCP_PORT=18787/' /etc/systemd/system/quant-etl-mcp.service
sudo systemctl daemon-reload && sudo systemctl restart quant-etl-mcp
```

Remember to update the client URL to match.

### The service starts but tools return database errors

DuckDB allows only one writer. If `cron` is running the nightly ETL right now,
a read-only connection cannot be opened. Wait for it to finish, or check what is
running with the `list_jobs` tool.

### A job is reported `stalled`

That is the feature working, not a failure. `stalled` means the process is still
alive but has produced no output for longer than the threshold. It is **not** a
final state — the job may recover on its own. You can wait, or use `cancel_job`
to stop it and then re-run just the missing part.

### Tools appear but the assistant cannot call them

Some clients maintain an allow-list of permitted tools. Consult your client's
documentation for how to permit `quant-etl`.

---

## 10. Security notes

**This service has no authentication.** Whoever can connect to the port can
trigger ETL runs that write to the production database.

The protections that do exist:

- It binds to `127.0.0.1` by default, so only processes on the same machine can
  reach it.
- It can only start the six ETL programs in its allow-list. There is no tool for
  running arbitrary commands or arbitrary SQL.
- Every parameter is validated before a subprocess is started: dates must be
  real calendar dates, stock codes must match a strict pattern, and enumerated
  values must come from spring's own current definitions.
- Subprocesses are always started from an argument list, never by composing a
  shell command string.

If you bind to anything other than a loopback address, you are exposing ETL
write access to everyone who can route to that address. The service prints a
warning when you do, but it will not stop you.

---

## 11. Updating

```bash
cd /home/rshun/src/etl-quant-mcp
git pull
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart quant-etl-mcp
```

If the update changes the systemd unit template, re-run `install.sh` with the
same options and copy the regenerated unit into place.

---

## 12. Uninstalling

```bash
sudo systemctl disable --now quant-etl-mcp
sudo rm /etc/systemd/system/quant-etl-mcp.service
sudo systemctl daemon-reload
```

Then remove the client configuration, and delete the checkout directory if you
no longer want it. Nothing else on the system is modified by this software —
it creates no databases, no users, and no files outside its own directory and
`$SPRING_LOG_DIR/mcp_jobs/`.

---

## 13. Tool reference

Thirteen tools are exposed.

### Read-only

| Tool | Purpose |
|---|---|
| `summarize_etl_log` | Per-module summary of one day's log, including `stall_suspects`. |
| `read_etl_log` | Tail of a log, filterable by level, module, or keyword. |
| `list_etl_logs` | Which days have logs, with sizes and timestamps. |
| `check_data_gaps` | Which trading days and stocks are missing data. |
| `describe_etl_program` | The current command-line parameters of one ETL program. |
| `list_jobs` | All jobs, with stalled ones first. |
| `get_job` | One job: status, progress, seconds since last output. |
| `get_job_output` | Tail of a job's output. |

### State-changing

| Tool | Purpose |
|---|---|
| `cancel_job` | Stop a running job. |
| `etl_import_daily` | Download daily stock quotes. |
| `etl_adjust` | Download adjustment factors. |
| `etl_fetch_index` | Download index quotes. |
| `etl_fill_indicators` | Fill derived indicators. |

The four `etl_*` tools accept `print_only` or a small date range, which makes
them safe to try: `print_only` performs the full download without writing
anything to the database.

Jobs are queued and run **one at a time**, because DuckDB permits only one
writer.

---

## 14. Getting more detail

`../README.md` covers the design. `mcp_etl_plan.md` is the full development
record: requirements, architecture decisions, the contract with `spring`, and
the reasoning behind the hang-detection design.
