#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# NMEA Web Forwarder — Installer v0.6-release
# =============================================================================
# Fresh-install only.  Creates a dedicated system user, copies application
# files, installs the systemd unit (hardened), and starts the service.
# Idempotent — safe to re-run for upgrades.
# =============================================================================

APP_DIR="/opt/nmea-web"
DATA_DIR="/var/nmea"
SVC_USER="nmea-web"
SVC_NAME="nmea-web"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; }
info() { echo -e "${CYAN}[*]${NC} $*"; }

[[ $EUID -eq 0 ]] || { err "Please run as root (sudo)."; exit 1; }

# ---------------------------------------------------------------------------
# 1.  Pre-flight checks
# ---------------------------------------------------------------------------
info "Pre-flight checks..."

if ! command -v python3 &>/dev/null; then
    err "python3 is required."
    exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
    err "Python 3.9 or newer is required."
    exit 1
}
say "Python 3.9+ found."

# Ensure venv + pip are available (Ubuntu ships python3-venv separately)
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq python3-venv
    else
        err "python3-venv is required (installed via your package manager)."
        exit 1
    fi
    say "Installed python3-venv."
fi

# ---------------------------------------------------------------------------
# 2.  System user
# ---------------------------------------------------------------------------
info "Setting up system user..."

if ! getent group "$SVC_USER" &>/dev/null; then
    groupadd --system "$SVC_USER"
    say "Created group: $SVC_USER"
fi

if ! id "$SVC_USER" &>/dev/null 2>&1; then
    useradd --system -g "$SVC_USER" -s /usr/sbin/nologin "$SVC_USER"
    say "Created user: $SVC_USER"
fi

# ---------------------------------------------------------------------------
# 3.  Directories
# ---------------------------------------------------------------------------
info "Creating directories..."

mkdir -p "$APP_DIR"
mkdir -p "$DATA_DIR"

chown -R "${SVC_USER}:${SVC_USER}" "$APP_DIR"
chown "${SVC_USER}:${SVC_USER}" "$DATA_DIR"
chmod 750 "$APP_DIR" "$DATA_DIR"

# ---------------------------------------------------------------------------
# 4.  Application files
# ---------------------------------------------------------------------------
info "Installing application files..."

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for f in main.py engine.py database.py models.py requirements.txt; do
    cp "$SCRIPT_DIR/app/$f" "$APP_DIR/"
done

mkdir -p "$APP_DIR/templates"
cp "$SCRIPT_DIR/app/templates/index.html" "$APP_DIR/templates/"

mkdir -p "$APP_DIR/static"

# byte-compile + cleanup stale cache
find "$APP_DIR" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
python3 -m compileall -q "$APP_DIR" 2>/dev/null || true

chown -R "${SVC_USER}:${SVC_USER}" "$APP_DIR"

# ---------------------------------------------------------------------------
# 5.  Python virtualenv + dependencies
# ---------------------------------------------------------------------------
info "Setting up Python virtual environment..."

VENV_DIR="$APP_DIR/.venv"

if [ -d "$VENV_DIR/bin" ]; then
    say "Reusing existing virtualenv at $VENV_DIR"
else
    python3 -m venv --clear "$VENV_DIR"
    say "Virtualenv created at $VENV_DIR"
fi

# Bootstrap pip inside the venv (ensurepip may be missing on minimal installs)
"$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null || true
"$VENV_DIR/bin/python3" -m pip install -r "$APP_DIR/requirements.txt" --quiet 2>&1 | tail -2
say "Python dependencies installed."

chown -R "${SVC_USER}:${SVC_USER}" "$VENV_DIR"

# ---------------------------------------------------------------------------
# 6.  Systemd unit
# ---------------------------------------------------------------------------
info "Installing systemd service..."

cp "$SCRIPT_DIR/conf/nmea-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SVC_NAME.service" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 7.  Logrotate
# ---------------------------------------------------------------------------
info "Installing logrotate config..."

cp "$SCRIPT_DIR/conf/nmea-web.logrotate" /etc/logrotate.d/nmea-web
chmod 644 /etc/logrotate.d/nmea-web

# ---------------------------------------------------------------------------
# 8.  Start or restart
# ---------------------------------------------------------------------------
info "Starting service..."

if systemctl is-active --quiet "$SVC_NAME.service" 2>/dev/null; then
    say "Restarting $SVC_NAME..."
    systemctl restart "$SVC_NAME.service"
else
    say "Starting $SVC_NAME..."
    systemctl start "$SVC_NAME.service"
fi

sleep 2

# ---------------------------------------------------------------------------
# 9.  Done
# ---------------------------------------------------------------------------
if systemctl is-active --quiet "$SVC_NAME.service" 2>/dev/null; then
    echo ""
    say "============================================================"
    say " nmea-web installed and running"
    say "============================================================"
    echo ""
    info "Dashboard  : http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
    info "Auth       : admin / admin"
    info "Logs       : journalctl -u $SVC_NAME -f"
    info "Data       : $DATA_DIR"
    echo ""
else
    echo ""
    err "Service failed to start. Diagnostics:"
    info "  journalctl -u $SVC_NAME --no-pager -n 40"
    echo ""
    exit 1
fi
