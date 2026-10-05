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
# Usage:  ./install.sh --spring-home DIR --spring-bin-dir DIR [options]  (installed package)
#         ./install.sh --spring-home DIR --spring-python PATH [options]  (source checkout)
# Help:   ./install.sh --help
#
set -euo pipefail

# ---------------------------------------------------------------- defaults

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="$SCRIPT_DIR"
VENV_DIR=""                      # defaults to $INSTALL_DIR/.venv
SPRING_HOME=""                   # spring runtime dir (config/ log/ ...); the checkout for source
SPRING_BIN_DIR=""                # installed package: dir holding the spring-* commands
SPRING_PYTHON=""                 # source checkout: spring's virtualenv interpreter
SPRING_LOG_DIR=""                # optional; defaults to <spring-home>/log
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
  --spring-home DIR       spring runtime dir (SPRING_HOME) containing config/config.yaml.
                          For a source checkout, this is the checkout root.

  plus ONE of, matching how spring is deployed:
  --spring-bin-dir DIR    Installed package: directory holding the spring-* commands,
                          e.g. /opt/spring/bin
  --spring-python PATH    Source checkout: Python interpreter of spring's virtualenv.
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
  --log-dir DIR           ETL log directory                (default: <spring-home>/log)
  --max-runtime SECONDS   Hard timeout per job             (default: 7200)
  --stall-timeout SECONDS Silence before a job is 'stalled'(default: auto by date span)
  --python PATH           Python used to build the venv    (default: python3)
  --check                 Validate only; create nothing
  -h, --help              Show this message

EXAMPLES
  # installed package
  ./install.sh \
      --spring-home /srv/spring \
      --spring-bin-dir /opt/spring/bin \
      --port 8787

  # source checkout
  ./install.sh \
      --spring-home /home/rshun/src/spring \
      --spring-python /home/rshun/src/venv_stock/bin/python3 \
      --port 8787

The service must run as the user that owns the ETL data. See docs/INSTALL.md.
USAGE
}

# ---------------------------------------------------------------- arguments

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir)    INSTALL_DIR="$2"; shift 2 ;;
        --venv)           VENV_DIR="$2"; shift 2 ;;
        --spring-dir)     die "--spring-dir is no longer supported. Use --spring-home instead
       (for a source checkout, pass the checkout root)." ;;
        --spring-python)  SPRING_PYTHON="$2"; shift 2 ;;
        --spring-bin-dir) SPRING_BIN_DIR="$2"; shift 2 ;;
        --spring-home)    SPRING_HOME="$2"; shift 2 ;;
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

[ -n "$SPRING_HOME" ] || die "--spring-home is required. Try --help."
# Exactly one launcher: setting both would leave the launch method ambiguous.
if [ -n "$SPRING_BIN_DIR" ] && [ -n "$SPRING_PYTHON" ]; then
    die "Use either --spring-bin-dir (installed package) or --spring-python (source checkout), not both."
elif [ -n "$SPRING_BIN_DIR" ]; then
    MODE="installed"
elif [ -n "$SPRING_PYTHON" ]; then
    MODE="source"
else
    die "One of --spring-bin-dir (installed package) or --spring-python (source checkout) is required.
       Try --help."
fi

# Normalise to absolute paths so the systemd unit is unambiguous.
# Keep the originals: on failure the substitution yields an empty string,
# and the error message must still show what the user actually typed.
_INSTALL_DIR_IN="$INSTALL_DIR"
INSTALL_DIR="$(cd "$_INSTALL_DIR_IN" 2>/dev/null && pwd)" \
    || die "--install-dir does not exist: $_INSTALL_DIR_IN"
_SPRING_HOME_IN="$SPRING_HOME"
SPRING_HOME="$(cd "$_SPRING_HOME_IN" 2>/dev/null && pwd)" \
    || die "--spring-home does not exist or is not readable by $(id -un): $_SPRING_HOME_IN"
if [ "$MODE" = "installed" ]; then
    _SPRING_BIN_DIR_IN="$SPRING_BIN_DIR"
    SPRING_BIN_DIR="$(cd "$_SPRING_BIN_DIR_IN" 2>/dev/null && pwd)" \
        || die "--spring-bin-dir does not exist or is not readable by $(id -un): $_SPRING_BIN_DIR_IN"
fi
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

if [ "$MODE" = "source" ]; then
    # The spring checkout must contain everything the server shells out to.
    MISSING=""
    for rel in etl/adjust.py etl/import_daily.py etl/fetch_index.py \
               etl/fill_volratio.py etl/update_limit.py etl/fill_shares.py \
               tools/describe_cli.py tools/check_daily.py; do
        [ -f "$SPRING_HOME/$rel" ] || MISSING="$MISSING\n       - $rel"
    done
    [ -z "$MISSING" ] || die "The spring checkout at $SPRING_HOME is missing:$(printf "$MISSING")

       For a source checkout, --spring-home must be the checkout root.
       If it is, the checkout is probably on an outdated branch:
       run 'git -C $SPRING_HOME status' and switch to the branch that has these files."
    ok "spring checkout looks complete"

    [ -x "$SPRING_PYTHON" ] \
        || die "--spring-python is not executable by $(id -un): $SPRING_PYTHON
       Point it at spring's virtualenv interpreter, e.g. .../venv/bin/python3
       Do NOT point it at a wrapper shell script."
    ok "spring interpreter is executable"
else
    # Every command the server shells out to must exist and be executable.
    MISSING=""
    for cmd in spring-adjust spring-import-daily spring-fetch-index \
               spring-fill-volratio spring-update-limit spring-fill-shares \
               spring-fill-turnover spring-describe-cli spring-check-daily; do
        [ -x "$SPRING_BIN_DIR/$cmd" ] || MISSING="$MISSING\n       - $cmd"
    done
    [ -z "$MISSING" ] || die "Missing or not executable by $(id -un) in $SPRING_BIN_DIR:$(printf "$MISSING")

       Point --spring-bin-dir at the bin/ of the environment spring was installed into,
       and make sure the installed spring version provides these commands."
    ok "spring commands found in $SPRING_BIN_DIR"
fi

if [ ! -f "$SPRING_HOME/config/config.yaml" ]; then
    if [ "$MODE" = "installed" ]; then
        die "No config/config.yaml under --spring-home: $SPRING_HOME
       Run 'SPRING_HOME=$SPRING_HOME $SPRING_BIN_DIR/spring-init' once, then edit the config."
    else
        die "No config/config.yaml under --spring-home: $SPRING_HOME
       For a source checkout, --spring-home must be the checkout root."
    fi
fi
ok "spring runtime dir looks complete"

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

SMOKE_ENV=("SPRING_HOME=$SPRING_HOME")
if [ "$MODE" = "installed" ]; then
    SMOKE_ENV+=("SPRING_BIN_DIR=$SPRING_BIN_DIR")
else
    SMOKE_ENV+=("SPRING_PYTHON=$SPRING_PYTHON")
fi
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
    if [ "$MODE" = "source" ]; then
        die "Could not introspect spring's CLI.
       Try manually:  cd $SPRING_HOME && $SPRING_PYTHON -m tools.describe_cli --list"
    else
        die "Could not introspect spring's CLI.
       Try manually:  SPRING_HOME=$SPRING_HOME $SPRING_BIN_DIR/spring-describe-cli --list"
    fi
fi

# ---------------------------------------------------------------- systemd unit

step "Generating the systemd unit"

UNIT_OUT="$INSTALL_DIR/deploy/$SERVICE_NAME.service.generated"
mkdir -p "$INSTALL_DIR/deploy"

{
    cat <<UNIT
# Generated by install.sh on $(date '+%Y-%m-%d %H:%M:%S')
#
# The service user must be able to write spring's database and runtime dirs.
# If spring's config writes the database path with ~, it expands per user, so a
# different user silently writes to a different database file.
# See docs/INSTALL.md, "Why the service user matters".

[Unit]
Description=quant-etl MCP server (ETL scheduling for spring)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/server.py

UNIT
    echo "Environment=SPRING_HOME=$SPRING_HOME"
    if [ "$MODE" = "installed" ]; then
        echo "Environment=SPRING_BIN_DIR=$SPRING_BIN_DIR"
    else
        echo "Environment=SPRING_PYTHON=$SPRING_PYTHON"
    fi
    cat <<UNIT
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

echo "Full documentation: $INSTALL_DIR/docs/INSTALL.md  (中文: docs/INSTALL.zh-CN.md)"
echo
