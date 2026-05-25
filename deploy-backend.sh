#!/bin/bash
# deploy-backend.sh - Pull, install deps, and restart the FastAPI backend
# Project dir = directory of this script. Service name = name of that directory.
# Usage: ./deploy-backend.sh [--port N] [--no-pull] [--no-deps] [--reload]

set -e

# --- Resolve project dir (= dir of this script) and service name (= dir name) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
SERVICE_NAME="$(basename "$PROJECT_DIR")"
VENV_PATH="$PROJECT_DIR/venv"

# --- Defaults ---
PORT=8000
DO_PULL=true
DO_DEPS=true
USE_RELOAD=false

# --- Parse flags ---
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            PORT="$2"
            shift 2
            ;;
        --port=*)
            PORT="${1#*=}"
            shift
            ;;
        --no-pull) DO_PULL=false; shift ;;
        --no-deps) DO_DEPS=false; shift ;;
        --reload)  USE_RELOAD=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--port N] [--no-pull] [--no-deps] [--reload]"
            echo ""
            echo "Options:"
            echo "  --port N     Port the service listens on (default: 8000)"
            echo "  --no-pull    Skip git pull"
            echo "  --no-deps    Skip pip install"
            echo "  --reload     Use systemctl reload instead of restart (zero-downtime)"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Try: $0 --help"
            exit 1
            ;;
    esac
done

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "Invalid port: $PORT"
    exit 1
fi

ENV_FILE="$PROJECT_DIR/.env"
HEALTH_URL="http://127.0.0.1:${PORT}"

# Upsert a KEY=VALUE line in $ENV_FILE, preserving other keys.
update_env_var() {
    local key="$1"
    local value="$2"
    touch "$ENV_FILE"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        local tmp
        tmp=$(mktemp)
        awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k"="v; next} {print}' "$ENV_FILE" > "$tmp"
        mv "$tmp" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
}

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}==>${NC} $1"; }
info() { echo -e "${BLUE}i${NC}  $1"; }
warn() { echo -e "${YELLOW}!${NC}  $1"; }
err()  { echo -e "${RED}✗${NC}  $1"; }

info "Project dir:  $PROJECT_DIR"
info "Service name: $SERVICE_NAME"
info "Port:         $PORT"

# --- Sanity checks ---
cd "$PROJECT_DIR"

if [ ! -d "$VENV_PATH" ]; then
    err "Virtualenv not found at $VENV_PATH"
    echo "Create it with: python3 -m venv venv"
    exit 1
fi

if ! systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    err "systemd service '$SERVICE_NAME' not found"
    echo "Make sure /etc/systemd/system/${SERVICE_NAME}.service exists"
    exit 1
fi

# --- Show current state ---
CURRENT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
info "Current commit: $CURRENT_COMMIT"

# --- Git pull ---
if [ "$DO_PULL" = true ]; then
    log "Pulling latest code..."
    if [ -n "$(git status --porcelain)" ]; then
        warn "Uncommitted changes detected:"
        git status --short
        read -p "Continue anyway? (y/N): " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            err "Aborted."
            exit 1
        fi
    fi
    git pull
    NEW_COMMIT=$(git rev-parse --short HEAD)
    if [ "$CURRENT_COMMIT" = "$NEW_COMMIT" ]; then
        info "Already up to date"
    else
        info "Updated: $CURRENT_COMMIT -> $NEW_COMMIT"
    fi
else
    info "Skipping git pull (--no-pull)"
fi

# --- Activate venv ---
# shellcheck disable=SC1091
source "$VENV_PATH/bin/activate"

# --- Install dependencies ---
if [ "$DO_DEPS" = true ]; then
    if [ -f "requirements.txt" ]; then
        log "Installing dependencies..."
        pip install -q -r requirements.txt
    else
        warn "No requirements.txt found, skipping pip install"
    fi
else
    info "Skipping dependency install (--no-deps)"
fi

# --- Optional: run database migrations if alembic exists ---
if [ -f "alembic.ini" ]; then
    log "Running database migrations..."
    alembic upgrade head
fi

# --- Persist PORT to .env so the systemd unit picks it up on restart ---
log "Setting PORT=$PORT in $ENV_FILE"
update_env_var "PORT" "$PORT"

# --- Restart or reload service ---
if [ "$USE_RELOAD" = true ]; then
    log "Reloading $SERVICE_NAME (zero-downtime)..."
    systemctl reload "$SERVICE_NAME"
else
    log "Restarting $SERVICE_NAME..."
    systemctl restart "$SERVICE_NAME"
fi

# --- Wait briefly and check status ---
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Service is running"
else
    err "Service failed to start!"
    echo ""
    echo "--- Recent logs ---"
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager
    exit 1
fi

# --- Health check ---
log "Health check..."
sleep 1
if curl -sf -o /dev/null -m 5 "$HEALTH_URL"; then
    log "Endpoint responding at $HEALTH_URL"
else
    warn "Health check failed (endpoint may not be ready yet)"
    info "Check logs: journalctl -u $SERVICE_NAME -f"
fi

# --- Summary ---
echo ""
log "Deploy complete!"
echo ""
echo "  Service:  $SERVICE_NAME"
echo "  Commit:   $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  Status:   $(systemctl is-active $SERVICE_NAME)"
echo "  Port:     $PORT"
echo "  Logs:     journalctl -u $SERVICE_NAME -f"
