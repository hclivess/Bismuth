#!/usr/bin/env bash
#
# install-node-service.sh — install the Bismuth node as a systemd service (mainnet).
#
# No more screen/nohup. After this, the node is a normal service:
#   systemctl status  bismuth-node     # is it up / synced
#   systemctl stop    bismuth-node     # GRACEFUL stop (SIGTERM -> drains db_lock -> clean ledger)
#   systemctl restart bismuth-node     # graceful restart (survives reboots — it's enabled)
#   journalctl -u bismuth-node -f      # follow the logs
#
# It auto-detects the repo dir, python, and user; gracefully stops any node already running on :5658
# (e.g. a manual nohup), writes the unit, and enables+starts it. Idempotent — safe to re-run.
#
# Usage:  sudo bash scripts/install-node-service.sh
#
set -euo pipefail

SERVICE="bismuth-node"
UNIT="/etc/systemd/system/${SERVICE}.service"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo bash scripts/install-node-service.sh)" >&2; exit 1; }

# Repo = parent of this script's directory (scripts/ lives inside the repo).
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# Prefer a repo-local venv (created by auto-install/bis-node-alone-install.sh) so the service uses the
# pinned interpreter + deps; fall back to the system python3.
if [ -x "$REPO_DIR/venv/bin/python" ]; then PYTHON="$REPO_DIR/venv/bin/python"; else PYTHON="$(command -v python3 || true)"; fi
RUN_USER="${SUDO_USER:-root}"

[ -f "$REPO_DIR/node.py" ] || { echo "ERROR: node.py not found in $REPO_DIR" >&2; exit 1; }
[ -n "$PYTHON" ]           || { echo "ERROR: python3 not found on PATH" >&2; exit 1; }

echo "Repo:    $REPO_DIR"
echo "Python:  $PYTHON"
echo "User:    $RUN_USER"
echo "Service: $UNIT"
echo

# --- gracefully stop any node already bound to :5658 (manual nohup, screen, or an old service) --------
PID="$(ss -tlnp 2>/dev/null | grep ':5658 ' | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -n "${PID:-}" ]; then
  echo "Stopping the running node (pid $PID) gracefully via the 'stop' command..."
  "$PYTHON" -c "
import sys, socket
sys.path.insert(0, '$REPO_DIR')
import connections
s = socket.socket(); s.settimeout(10); s.connect(('127.0.0.1', 5658))
connections.send(s, 'stop')
try: s.close()
except Exception: pass
print('  stop sent')
" || echo "  (stop command failed; will fall back to kill)"
  for _ in $(seq 1 70); do ss -tlnp 2>/dev/null | grep -q ':5658 ' || break; sleep 1; done
  if ss -tlnp 2>/dev/null | grep -q ':5658 '; then
    echo "  still up after 70s, sending SIGTERM to $PID"; kill "$PID" 2>/dev/null || true; sleep 3
  fi
  echo "  node stopped."
fi

# --- write the unit -----------------------------------------------------------------------------------
cat > "$UNIT" <<UNITEOF
[Unit]
Description=Bismuth node (mainnet)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
# Always boot mainnet: ignore a leftover regnet test override (config_custom.txt). See options.py.
Environment=BISMUTH_IGNORE_CONFIG_CUSTOM=1
ExecStart=${PYTHON} node.py
Restart=on-failure
RestartSec=10
# Graceful shutdown: SIGTERM -> node._graceful_stop -> finishes the in-flight block + drains db_lock,
# so ledger.db/hyper.db stay consistent (no cross-integrity rollback on the next boot). Give it room.
KillSignal=SIGTERM
TimeoutStopSec=180
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE}

[Install]
WantedBy=multi-user.target
UNITEOF

echo "Wrote $UNIT"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

echo
echo "Waiting for the node to come up..."
sleep 5
systemctl --no-pager --full status "$SERVICE" 2>/dev/null | sed -n '1,10p' || true
echo
echo "Installed. The node now runs as a service and restarts on reboot. No more screens:"
echo "  systemctl status  $SERVICE"
echo "  systemctl stop    $SERVICE     # graceful"
echo "  systemctl restart $SERVICE"
echo "  journalctl -u $SERVICE -f"
