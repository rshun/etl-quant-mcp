#!/usr/bin/env bash
#
# quant-etl MCP server installer.
#
# Prepares a deployment on this machine: validates prerequisites, creates the
# virtual environment, verifies it can reach the spring ETL checkout, and
# generates a systemd unit tailored to your paths and port.
#
# It deliberately does NOT run any command that needs root. The final three
# `sudo` commands are printed for you to review and run yourself.
#
# Usage:  ./install.sh --spring-dir DIR --spring-python PATH [options]
# Help:   ./install.sh --help
#
set -euo pipefail

# ---------------------------------------------------------------- defaults

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="$SCRIPT_DIR"
VENV_DIR=""                      # defaults to $INSTALL_DIR/.venv
SPRING_DIR=""
SPRING_PYTHON=""
SPRING_LOG_DIR=""                # optional; defaults to $SPRING_DIR/log
HOST="127.0.0.1"
PORT="16000"
TRANSPORT="streamable-http"
SERVICE_NAME="quant-etl-mcp"
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
MAX_RUNTIME=""                   # optional; server default is 7200
STALL_TIMEOUT=""                 # optional; server picks a tier by date span
PYTHON_BIN="python3"
CHECK_ONLY=0

MIN_PY_MAJOR=3
MIN_PY_MINOR=10

# ---------------------------------------------------------------- helpers

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
if [ ! -t 1 ]; then RED=""; GREEN=""; YELLOW=""; BOLD=""; OFF=""; fi

step()  { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()   { printf '\n%sERROR:%s %s\n\n' "$RED" "$OFF" "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
quant-etl MCP server installer

REQUIRED
  --spring-dir DIR        Root of the spring ETL checkout (contains etl/ and tools/)
  --spring-python PATH    Python interpreter of spring's virtualenv.
                          Point at the interpreter itself, NOT a wrapper script.

OPTIONAL
  --install-dir DIR       Where this repo lives            (default: this script's directory)
  --venv DIR              Virtualenv location              (default: <install-dir>/.venv)
  --port PORT             TCP port to listen on            (default: 8787)
  --host HOST             Bind address                     (default: 127.0.0.1)
  --transport MODE        stdio | streamable-http | sse    (default: streamable-http)
  --service-name NAME     systemd unit name                (default: quant-etl-mcp)
  --service-user USER     User the service runs as         (default: current user)
  --service-group GROUP   Group the service runs as        (default: current user's group)
  --log-dir DIR           ETL log directory                (default: <spring-dir>/log)
  --max-runtime SECONDS   Hard timeout per job             (default: 7200)
  --stall-timeout SECONDS Silence before a job is 'stalled'(default: auto by date span)
  --python PATH           Python used to build the venv    (default: python3)
  --check                 Validate only; create nothing
  -h, --help              Show this message

EXAMPLE
  ./install.sh \
      --spring-dir /home/rshun/src/spring \
      --spring-python /home/rshun/src/venv_stock/bin/python3 \
      --port 8787

The service must run as the user that owns the ETL data. See INSTALL.md.
USAGE
}

# ---------------------------------------------------------------- arguments

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir)    INSTALL_DIR="$2"; shift 2 ;;
        --venv)           VENV_DIR="$2"; shift 2 ;;
        --spring-dir)     SPRING_DIR="$2"; shift 2 ;;
        --spring-python)  SPRING_PYTHON="$2"; shift 2 ;;
        --log-dir)        SPRING_LOG_DIR="$2"; shift 2 ;;
        --host)           HOST="$2"; shift 2 ;;
        --port)           PORT="$2"; shift 2 ;;
        --transport)      TRANSPORT="$2"; shift 2 ;;
        --service-name)   SERVICE_NAME="$2"; shift 2 ;;
        --service-user)   SERVICE_USER="$2"; shift 2 ;;
        --service-group)  SERVICE_GROUP="$2"; shift 2 ;;
        --max-runtime)    MAX_RUNTIME="$2"; shift 2 ;;
        --stall-timeout)  STALL_TIMEOUT="$2"; shift 2 ;;
        --python)         PYTHON_BIN="$2"; shift 2 ;;
        --check)          CHECK_ONLY=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                die "Unknown option: $1  (try --help)" ;;
    esac
done

[ -n "$SPRING_DIR" ]    || die "--spring-dir is required. Try --help."
[ -n "$SPRING_PYTHON" ] || die "--spring-python is required. Try --help."

# Normalise to absolute paths so the systemd unit is unambiguous.
# Keep the originals: on failure the substitution yields an empty string,
# and the error message must still show what the user actually typed.
_INSTALL_DIR_IN="$INSTALL_DIR"
_SPRING_DIR_IN="$SPRING_DIR"
INSTALL_DIR="$(cd "$_INSTALL_DIR_IN" 2>/dev/null && pwd)" \
    || die "--install-dir does not exist: $_INSTALL_DIR_IN"
SPRING_DIR="$(cd "$_SPRING_DIR_IN" 2>/dev/null && pwd)" \
    || die "--spring-dir does not exist or is not readable by $(id -un): $_SPRING_DIR_IN"
[ -n "$VENV_DIR" ] || VENV_DIR="$INSTALL_DIR/.venv"

# ---------------------------------------------------------------- checks

step "Checking prerequisites"

if [ "$(id -u)" = "0" ]; then
    warn "Running as root. The service should run as the user that owns the ETL data,"
    warn "not as root. Re-run as that user unless you know why you want root."
fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 \
    || die "Python not found: $PYTHON_BIN
       Install Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} or newer, or pass --python /path/to/python3."

PY_VER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
"$PYTHON_BIN" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" \
    || die "Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ required, found $PY_VER at $(command -v "$PYTHON_BIN").
       The code uses PEP 604 union syntax (X | Y) in annotations."
ok "Python $PY_VER"

"$PYTHON_BIN" -c 'import venv' 2>/dev/null \
    || die "The 'venv' module is unavailable for $PYTHON_BIN.
       On Debian/Ubuntu install it with:  sudo apt install python3-venv
       (This installer does not install system packages for you.)"
ok "venv module available"

[ -f "$INSTALL_DIR/server.py" ] \
    || die "server.py not found in --install-dir: $INSTALL_DIR
       Point --install-dir at the root of the etl-quant-mcp checkout."
[ -f "$INSTALL_DIR/requirements.txt" ] \
    || die "requirements.txt not found in $INSTALL_DIR"
ok "Found this project at $INSTALL_DIR"

# The spring checkout must contain everything the server shells out to.
MISSING=""
for rel in etl/adjust.py etl/import_daily.py etl/fetch_index.py \
           etl/fill_volratio.py etl/update_limit.py etl/fill_shares.py \
           tools/describe_cli.py tools/check_daily.py; do
    [ -f "$SPRING_DIR/$rel" ] || MISSING="$MISSING\n       - $rel"
done
[ -z "$MISSING" ] || die "The spring checkout at $SPRING_DIR is missing:$(printf "$MISSING")

       This usually means the checkout is on an outdated branch.
       Run 'git -C $SPRING_DIR status' and switch to the branch that has these files."
ok "spring checkout looks complete"

[ -x "$SPRING_PYTHON" ] \
    || die "--spring-python is not executable by $(id -un): $SPRING_PYTHON
       Point it at spring's virtualenv interpreter, e.g. .../venv/bin/python3
       Do NOT point it at a wrapper shell script."
ok "spring interpreter is executable"

case "$TRANSPORT" in
    stdio|streamable-http|sse) ok "Transport: $TRANSPORT" ;;
    *) die "--transport must be one of: stdio, streamable-http, sse (got '$TRANSPORT')" ;;
esac

if [ "$TRANSPORT" != "stdio" ]; then
    case "$PORT" in
        ''|*[!0-9]*) die "--port must be a number (got '$PORT')" ;;
    esac
    [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port out of range: $PORT"
    [ "$PORT" -ge 1024 ] || warn "Ports below 1024 require root to bind."

    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; then
        die "Port $PORT is already in use. Pick another with --port."
    fi
    ok "Port $PORT is free"

    case "$HOST" in
        127.*|localhost|::1) ok "Bind address $HOST is loopback-only" ;;
        *) warn "Bind address $HOST is NOT loopback."
           warn "This server has no authentication: anyone who can reach the port"
           warn "can trigger ETL writes. Use 127.0.0.1 unless you have a reason." ;;
    esac
fi

if [ "$CHECK_ONLY" = "1" ]; then
    step "Check-only mode: all prerequisites satisfied, nothing was created."
    exit 0
fi

# ---------------------------------------------------------------- venv

step "Creating the virtual environment"

if [ -x "$VENV_DIR/bin/python" ]; then
    ok "Reusing existing venv at $VENV_DIR"
else
    "$PYTHON_BIN" -m venv "$VENV_DIR" || die "Failed to create venv at $VENV_DIR"
    ok "Created $VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
    || die "Dependency installation failed. Re-run without --quiet to see why:
       $VENV_DIR/bin/pip install -r $INSTALL_DIR/requirements.txt"
ok "Dependencies installed (only 'mcp'; the heavy work runs in spring's interpreter)"

# ---------------------------------------------------------------- smoke test

step "Verifying the server can start"

SMOKE_ENV=(
    "SPRING_DIR=$SPRING_DIR"
    "SPRING_PYTHON=$SPRING_PYTHON"
)
[ -n "$SPRING_LOG_DIR" ] && SMOKE_ENV+=("SPRING_LOG_DIR=$SPRING_LOG_DIR")

if ! env "${SMOKE_ENV[@]}" "$VENV_DIR/bin/python" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR')
import schema
schema.validate_environment()
import server
print('  tools exposed:', len(server.mcp._tool_manager.list_tools()))
" 2>/tmp/quant-etl-smoke.$$; then
    printf '%s\n' "$(cat /tmp/quant-etl-smoke.$$)" >&2
    die "The server failed its own environment check (see the error above)."
fi
ok "Environment check passed"

# Ask spring to describe one program: proves the cross-repo call path works.
if env "${SMOKE_ENV[@]}" "$VENV_DIR/bin/python" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR')
import params
params.fetch_program_schema('import_daily')
" >/dev/null 2>&1; then
    ok "Introspection of spring's CLI succeeded"
else
    die "Could not introspect spring's CLI.
       Try manually:  cd $SPRING_DIR && $SPRING_PYTHON -m tools.describe_cli --list"
fi

# ---------------------------------------------------------------- systemd unit

step "Generating the systemd unit"

UNIT_OUT="$INSTALL_DIR/deploy/$SERVICE_NAME.service.generated"
mkdir -p "$INSTALL_DIR/deploy"

{
    cat <<UNIT
# Generated by install.sh on $(date '+%Y-%m-%d %H:%M:%S')
#
# The service MUST run as the user that owns the ETL data. spring resolves its
# database path with ~ expansion, so a different user silently writes to a
# different database file. See INSTALL.md, "Why the service user matters".

[Unit]
Description=quant-etl MCP server (ETL scheduling for spring)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/server.py

Environment=SPRING_DIR=$SPRING_DIR
Environment=SPRING_PYTHON=$SPRING_PYTHON
Environment=ETL_MCP_TRANSPORT=$TRANSPORT
Environment=ETL_MCP_HOST=$HOST
Environment=ETL_MCP_PORT=$PORT
UNIT
    [ -n "$SPRING_LOG_DIR" ] && echo "Environment=SPRING_LOG_DIR=$SPRING_LOG_DIR"
    [ -n "$MAX_RUNTIME" ]    && echo "Environment=MAX_RUNTIME_DEFAULT=$MAX_RUNTIME"
    [ -n "$STALL_TIMEOUT" ]  && echo "Environment=STALL_TIMEOUT_DEFAULT=$STALL_TIMEOUT"
    cat <<'UNIT'

Restart=on-failure
RestartSec=5

# The service reads and writes spring's directory and database, so ProtectHome
# cannot be used. These are the hardening options that remain compatible.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
} > "$UNIT_OUT"

ok "Wrote $UNIT_OUT"

# ---------------------------------------------------------------- next steps

CLIENT_URL="http://$HOST:$PORT/mcp"

cat <<NEXT

${BOLD}Installation prepared successfully.${OFF}

The three commands below need root, so this installer did not run them.
Review the generated unit first, then run them yourself:

  ${BOLD}sudo cp $UNIT_OUT /etc/systemd/system/$SERVICE_NAME.service${OFF}
  ${BOLD}sudo systemctl daemon-reload${OFF}
  ${BOLD}sudo systemctl enable --now $SERVICE_NAME${OFF}

Then verify:

  systemctl status $SERVICE_NAME --no-pager
  ss -ltn | grep -w $PORT          # expect $HOST:$PORT
  journalctl -u $SERVICE_NAME -n 20 --no-pager

NEXT

if [ "$TRANSPORT" != "stdio" ]; then
cat <<NEXT
Point your MCP client at:

  $CLIENT_URL

With the Claude Code CLI (run as the *client* user, which may differ from
$SERVICE_USER — that is the whole point of the HTTP transport):

  claude mcp add --transport http quant-etl $CLIENT_URL

NEXT
fi

echo "Full documentation: $INSTALL_DIR/INSTALL.md"
echo
