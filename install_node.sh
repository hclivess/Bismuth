#!/usr/bin/env bash
#
# install_node.sh — full Bismuth node installer: dependencies + systemd service.
#
# Installs the system + Python dependencies a full node needs (including the LMDB stores and the
# ed25519 3.12 source-build workaround), then installs, enables, and (optionally) starts the
# `bismuth-node` systemd service pointing at THIS checkout.
#
# Usage:
#   sudo ./install_node.sh                 # install deps + service, start it if not already running
#   sudo ./install_node.sh --no-start      # install deps + service, do not start
#   sudo ./install_node.sh --restart       # also restart the service if it is already running
#   sudo ./install_node.sh --deps-only     # only install dependencies (no service)
#   sudo ./install_node.sh --service-name bismuth-testnet   # custom unit name
#   sudo ./install_node.sh --regnet        # service runs a regnet node (node.py regnet) for testing
#   sudo ./install_node.sh --no-apt        # skip apt (deps already present / non-Debian)
#   sudo ./install_node.sh --tor           # also install Tor (the tor pkg + stem) for tor=managed (doc/38)
#
# Idempotent: safe to re-run. It will NOT restart an already-running node unless --restart is given.
set -euo pipefail

# ---- config / args ---------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"        # the service runs this interpreter; deps install into it
SERVICE_NAME="bismuth-node"
RUN_USER="${SUDO_USER:-root}"               # service User=; default root to match the existing unit
START_MODE="auto"                           # auto | no | restart
DEPS_ONLY=0
DO_APT=1
WITH_TOR=0                                  # --tor: also install the tor binary + stem (doc/38)
NODE_ARGS=""                                # extra args to `node.py` (e.g. "regnet")
DESC="Bismuth node (mainnet)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start)      START_MODE="no" ;;
    --restart)       START_MODE="restart" ;;
    --deps-only)     DEPS_ONLY=1 ;;
    --no-apt)        DO_APT=0 ;;
    --tor)           WITH_TOR=1 ;;
    --service-name)  SERVICE_NAME="$2"; shift ;;
    --regnet)        NODE_ARGS="regnet"; DESC="Bismuth node (regnet)" ;;
    --user)          RUN_USER="$2"; shift ;;
    -h|--help)       grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo) — apt + systemd need it."
command -v "$PYTHON" >/dev/null || die "$PYTHON not found; set PYTHON=/path/to/python3."
log "repo:        $REPO_DIR"
log "python:      $PYTHON ($("$PYTHON" -V 2>&1))"
log "service:     $SERVICE_NAME  (User=$RUN_USER, node.py $NODE_ARGS)"

# ---- 1. system packages ----------------------------------------------------
if [[ $DO_APT -eq 1 ]] && command -v apt-get >/dev/null; then
  log "installing system packages (apt)…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # python + a C toolchain (coincurve / ed25519 may build from source); curl for downloads.
  apt-get install -y -qq \
    python3 python3-pip python3-dev python3-venv \
    build-essential pkg-config libffi-dev libssl-dev libtool automake autoconf \
    git curl ca-certificates >/dev/null
  log "system packages OK"
else
  warn "skipping apt (either --no-apt or not a Debian/Ubuntu system) — ensure a C toolchain + python3-dev exist."
fi

# ---- 2. python dependencies ------------------------------------------------
# Debian/Ubuntu mark the system interpreter externally-managed (PEP 668); the existing service uses the
# system python, so install there with --break-system-packages. (Switch PYTHON to a venv to avoid that.)
# No --upgrade: install only what's missing, so re-running never bumps an already-working node's deps.
PIP=("$PYTHON" -m pip install --break-system-packages)
if "$PYTHON" -m pip install --help 2>&1 | grep -q break-system-packages; then :; else PIP=("$PYTHON" -m pip install); fi

log "installing Python dependencies…"
# Tested versions (the production node): PySocks 1.7.1, pycryptodomex 3.23.0, requests 2.33.0,
# base58 2.1.1, coincurve 21.0.0, cryptography 41.0.7, lmdb 2.2.1, msgpack 1.1.2, dilithium-py 1.4.0.
"${PIP[@]}" \
  PySocks pycryptodomex requests base58 \
  coincurve cryptography \
  lmdb msgpack \
  dilithium-py
log "core + LMDB + PQ deps OK"

# ed25519: REQUIRED (mainnet carries ed25519 txs). Its sdist build is broken on Python 3.12 (bundled
# versioneer uses configparser.SafeConfigParser/readfp, removed in 3.12). Install from a patched source.
if "$PYTHON" -c 'import ed25519' 2>/dev/null; then
  log "ed25519 already installed"
else
  PYVER="$("$PYTHON" -c 'import sys; print("%d%d"%sys.version_info[:2])')"
  if [[ "$PYVER" -ge 312 ]]; then
    log "ed25519: building patched 1.5 source (Python $PYVER needs the versioneer fix)…"
    TMP="$(mktemp -d)"
    "$PYTHON" -m pip download ed25519==1.5 --no-deps --no-binary :all: -d "$TMP" >/dev/null
    tar -xf "$TMP"/ed25519-1.5.tar* -C "$TMP"
    SRC="$TMP/ed25519-1.5"
    # neutralise versioneer: replace its two calls with literals and drop the import.
    sed -i 's/versioneer\.get_version()/"1.5"/g; s/versioneer\.get_cmdclass()/{}/g; /^import versioneer/d' "$SRC/setup.py"
    "$PYTHON" -m pip install --break-system-packages --no-deps "$SRC" 2>/dev/null \
      || "$PYTHON" -m pip install --no-deps "$SRC"
    rm -rf "$TMP"
  else
    "${PIP[@]}" ed25519
  fi
  "$PYTHON" -c 'import ed25519' || die "ed25519 still missing after patched install — a node cannot sync ed25519 blocks."
  log "ed25519 OK"
fi

# ---- 2b. optional Tor for tor=managed (doc/38) ----------------------------
# We do NOT vendor a tor binary in the repo (platform matrix + you must get tor's security updates from
# your distro). Instead --tor installs the distro 'tor' package + the 'stem' controller lib. The node
# stays clearnet by default; the operator opts in by setting tor=managed in config.toml/config.txt.
if [[ $WITH_TOR -eq 1 ]]; then
  log "installing Tor (managed onion routing, doc/38)…"
  if command -v tor >/dev/null; then
    log "tor binary already present ($(command -v tor))"
  elif [[ $DO_APT -eq 1 ]] && command -v apt-get >/dev/null; then
    apt-get install -y -qq tor >/dev/null 2>&1 && log "tor installed (apt)" || warn "apt could not install tor"
  elif command -v dnf  >/dev/null; then
    dnf install -y -q tor >/dev/null 2>&1 && log "tor installed (dnf)" || warn "dnf could not install tor"
  elif command -v yum  >/dev/null; then
    yum install -y -q tor >/dev/null 2>&1 && log "tor installed (yum)" || warn "yum could not install tor"
  else
    warn "no supported package manager detected — install the 'tor' package manually."
  fi
  "${PIP[@]}" stem >/dev/null 2>&1 && log "stem (tor controller lib) installed" || warn "could not pip-install stem"
  if command -v tor >/dev/null; then
    log "Tor ready — set tor=managed (and tor_onion=True for inbound) in config.toml/config.txt (doc/38)."
  else
    warn "tor binary not found — managed mode will fall back to clearnet until tor is installed."
  fi
fi

# ---- 3. sanity: every import a full node needs actually resolves -----------
log "verifying imports…"
"$PYTHON" - <<'PYCHK' || die "a required module failed to import (see above)."
import importlib, sys
mods = ["coincurve","ed25519","lmdb","msgpack","base58","Cryptodome","socks","requests","cryptography"]
bad = []
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append("%s (%s)" % (m, e))
if bad:
    print("MISSING:", "; ".join(bad)); sys.exit(1)
print("  all required modules import OK")
PYCHK

if [[ $DEPS_ONLY -eq 1 ]]; then log "deps-only: done."; exit 0; fi

# ---- 4. config + runtime dirs ---------------------------------------------
cd "$REPO_DIR"
mkdir -p static
if [[ ! -f config.txt ]]; then
  if [[ -f config_custom.txt && -n "$NODE_ARGS" ]]; then
    warn "no config.txt; node will fall back to options.py defaults (regnet uses config_custom.txt)."
  else
    warn "no config.txt found in $REPO_DIR — the node will boot on options.py defaults. Create config.txt for mainnet."
  fi
fi

# ---- 5. systemd unit -------------------------------------------------------
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
log "writing $UNIT"
# BISMUTH_IGNORE_CONFIG_CUSTOM=1 for mainnet so a leftover regnet config_custom.txt can't hijack the boot.
IGNORE_CUSTOM="Environment=BISMUTH_IGNORE_CONFIG_CUSTOM=1"
[[ -n "$NODE_ARGS" ]] && IGNORE_CUSTOM="# (regnet: config_custom.txt IS honored)"
cat > "$UNIT" <<UNIT
[Unit]
Description=${DESC}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
${IGNORE_CUSTOM}
ExecStart=${PYTHON} node.py ${NODE_ARGS}
Restart=on-failure
RestartSec=10
# Graceful shutdown: SIGTERM -> node._graceful_stop finishes the in-flight block + drains the db lock,
# so ledger.db/hyper.db stay consistent (no cross-integrity rollback on the next boot). Give it room.
KillSignal=SIGTERM
TimeoutStopSec=180
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
log "service installed + enabled (starts on boot)"

# ---- 6. start / restart ----------------------------------------------------
ACTIVE="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
case "$START_MODE" in
  no)      log "not starting (--no-start). Start with: systemctl start $SERVICE_NAME" ;;
  restart) log "restarting $SERVICE_NAME…"; systemctl restart "$SERVICE_NAME" ;;
  auto)
    if [[ "$ACTIVE" == "active" ]]; then
      warn "$SERVICE_NAME is already running — left untouched (use --restart to apply changes now)."
    else
      log "starting $SERVICE_NAME…"; systemctl start "$SERVICE_NAME"
    fi ;;
esac

# ---- 7. report -------------------------------------------------------------
sleep 1 || true
echo
systemctl --no-pager --lines=0 status "$SERVICE_NAME" 2>/dev/null | head -4 || true
echo
log "done. Useful commands:"
echo "    journalctl -u $SERVICE_NAME -f                 # follow logs"
echo "    systemctl {status,restart,stop} $SERVICE_NAME"
echo "    curl -s http://127.0.0.1:5659/api/status       # node height / peers (if rest_api=True)"
