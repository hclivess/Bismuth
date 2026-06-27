# Auto-install — single Bismuth node

One-shot installer that sets up a **full mainnet node as a systemd service** (for devs, exchanges,
service providers). Community-maintained, MIT-licensed.

```bash
# as root, from a clone:
bash auto-install/bis-node-alone-install.sh

# or one-liner:
curl -fsSL https://raw.githubusercontent.com/hclivess/Bismuth/main/auto-install/bis-node-alone-install.sh | sudo bash
```

What it does:

- Tunes the OS (3 GB swap, file limits, sysctl).
- Clones the node from **github.com/hclivess/Bismuth** (branch `main`).
- Creates a **Python 3.11 venv** and installs `requirements-node.txt` (3.11 matches CI and sidesteps the
  `ed25519` sdist build break on Python 3.12, the default on Ubuntu 24.04).
- Installs + enables the **`bismuth-node` systemd service** (`scripts/install-node-service.sh`) — no more
  `screen`/cron; systemd handles restart-on-failure and restart-on-reboot, with a graceful SIGTERM stop.
- The node **downloads the ledger bootstrap automatically** on first start
  (`https://bismuth.cz/ledger.tar.gz`) — initial sync takes a while.

Notes:

- **Ubuntu 22.04 / 24.04 LTS**, run as **root**.
- Override the install location: `BISMUTH_DIR=/opt/bismuth bash auto-install/bis-node-alone-install.sh`.
- The **firewall is not** configured (to avoid locking out SSH). If you use `ufw`, open `5658/tcp`
  (node P2P/socket) and, only if you expose it, `5659/tcp` (REST API).
- Manage the node:
  ```bash
  systemctl status  bismuth-node
  journalctl -u bismuth-node -f      # follow sync
  systemctl restart bismuth-node
  ```
- Prefer a container? A Docker image is maintained separately — see the website's ecosystem directory
  (https://bismuth.cz/).
