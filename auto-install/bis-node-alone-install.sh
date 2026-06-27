#!/usr/bin/env bash
#
# Bismuth — single-node auto-installer (mainnet). Community-maintained. MIT license.
#
# Sets up a full Bismuth node as a systemd service on a fresh Ubuntu 22.04 / 24.04 server:
#   * system tuning (swap, file limits, sysctl)
#   * clones the node from github.com/hclivess/Bismuth (branch main)
#   * a Python 3.11 venv with the node requirements — 3.11 matches CI and avoids the ed25519 sdist
#     build break on 3.12 (Ubuntu 24.04's default python)
#   * installs + enables the systemd service via scripts/install-node-service.sh
#   * the node downloads the ledger bootstrap automatically on first start (bismuth.cz/ledger.tar.gz),
#     so there is no fragile snapshot step here
#
# Usage (root):   bash auto-install/bis-node-alone-install.sh
# One-liner:      curl -fsSL https://raw.githubusercontent.com/hclivess/Bismuth/main/auto-install/bis-node-alone-install.sh | sudo bash
# Override dir:   BISMUTH_DIR=/opt/bismuth bash auto-install/bis-node-alone-install.sh
#
set -euo pipefail

VERSION="0.2.0"
REPO_URL="${BISMUTH_REPO:-https://github.com/hclivess/Bismuth.git}"
REPO_BRANCH="${BISMUTH_BRANCH:-main}"
INSTALL_DIR="${BISMUTH_DIR:-/opt/bismuth}"
PYVER="3.11"                       # matches CI; ed25519's sdist build is broken on 3.12

log() { echo -e "\n\033[1;36m== $* ==\033[0m"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

create_swap() {
	log "Swap"
	if [ -f /swapfile ] || swapon --show 2>/dev/null | grep -q .; then
		echo "Swap already present — skipping."
	else
		fallocate -l 3G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
		grep -q '/swapfile' /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
		echo "3G swap activated."
	fi
}

config_os() {
	log "OS tuning (file limits + sysctl)"
	grep -q "root soft nofile 65535" /etc/security/limits.conf || {
		echo "root soft nofile 65535" >> /etc/security/limits.conf
		echo "root hard nofile 65535" >> /etc/security/limits.conf
	}
	grep -q "fs.file-max = 100000"        /etc/sysctl.conf || echo "fs.file-max = 100000"        >> /etc/sysctl.conf
	grep -q "vm.swappiness = 10"          /etc/sysctl.conf || echo "vm.swappiness = 10"          >> /etc/sysctl.conf
	grep -q "vm.vfs_cache_pressure = 50"  /etc/sysctl.conf || echo "vm.vfs_cache_pressure = 50"  >> /etc/sysctl.conf
	sysctl -p || true
}

install_dependencies() {
	log "APT dependencies"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -y
	apt-get install -y software-properties-common ca-certificates curl git unzip sqlite3 pigz build-essential
	# Python 3.11 via deadsnakes only if the distro doesn't already ship it (22.04/24.04 default to 3.10/3.12).
	if ! command -v "python${PYVER}" >/dev/null 2>&1; then
		add-apt-repository -y ppa:deadsnakes/ppa
		apt-get update -y
	fi
	apt-get install -y "python${PYVER}" "python${PYVER}-venv" "python${PYVER}-dev"
}

fetch_node() {
	log "Fetch node — $REPO_URL @ $REPO_BRANCH -> $INSTALL_DIR"
	if [ -d "$INSTALL_DIR/.git" ]; then
		echo "Existing checkout found; updating."
		git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH"
		git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"
	else
		mkdir -p "$(dirname "$INSTALL_DIR")"
		git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
	fi
}

setup_venv() {
	log "Python ${PYVER} venv + node requirements"
	"python${PYVER}" -m venv "$INSTALL_DIR/venv"
	"$INSTALL_DIR/venv/bin/pip" install -q -U pip wheel setuptools
	"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements-node.txt"
	echo "Requirements installed into $INSTALL_DIR/venv"
}

install_service() {
	log "systemd service (bismuth-node)"
	# install-node-service.sh auto-detects the repo dir and prefers $INSTALL_DIR/venv/bin/python.
	bash "$INSTALL_DIR/scripts/install-node-service.sh"
}

create_swap
config_os
install_dependencies
fetch_node
setup_venv
install_service

log "Done (installer v${VERSION})"
cat <<EOF

Bismuth node installed at: ${INSTALL_DIR}   (python ${PYVER} venv)
It runs as the systemd service 'bismuth-node' and downloads the ledger bootstrap
(https://bismuth.cz/ledger.tar.gz) automatically on first start — the initial sync takes a while.

  systemctl status  bismuth-node
  journalctl -u bismuth-node -f      # follow sync progress
  systemctl restart bismuth-node     # graceful

Firewall is NOT configured (so we don't risk locking out SSH). If you run ufw, open:
  ufw allow 5658/tcp     # node P2P / command socket
  ufw allow 5659/tcp     # REST API (only if you expose it publicly)
EOF
