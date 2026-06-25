"""Tor / onion-routing manager (doc/38).

Owns the node's entire Tor lifecycle behind one object, replacing the old hardcoded
``setproxy(SOCKS5,"127.0.0.1",9050)`` bolt-on. Three modes (from the tri-state ``tor`` config):

  * ``off``       — clearnet (default). This module is never imported; ``node.tor_manager`` stays None.
  * ``external``  — route outbound through an ALREADY-RUNNING tor daemon's SOCKS proxy (backward-compatible
                    with the old ``tor=True``: defaults to 127.0.0.1:9050, now overridable). Inbound
                    listener suppressed, exactly as before.
  * ``managed``   — Bismuth launches and OWNS a tor child process via ``stem`` (no manual torrc/daemon):
                    auto SOCKS + control ports, cookie auth, bootstrap gating, an EPHEMERAL v3 hidden
                    service for inbound, and ``take_ownership`` so tor dies with the node.

HONEST SCOPE: there is no daemon-free Tor. Onion routing is a property of the live Tor network, so the
``tor`` C binary remains a HARD runtime requirement for every non-off mode. "managed" only removes the
manual torrc / HiddenServiceDir / separate-daemon burden — it does NOT remove tor itself. ``stem`` is an
OPTIONAL dependency, imported lazily only here and only when a tor mode is active; a clearnet node never
touches it. On any failure (stem missing, tor binary missing, launch/bootstrap timeout) the manager either
falls back to clearnet (``tor_required=False``, default) with a single loud warning, or fails fast
(``tor_required=True``) so a privacy-critical operator is never silently deanonymized — never a hang, never
a retry-spin.
"""
import os
import re
import time

# v3 onion = 56 base32 chars (a-z2-7) + ".onion"  (v2 was removed from the Tor network in 2021)
ONION_V3_RE = re.compile(r"^[a-z2-7]{56}\.onion$")

DEFAULT_SOCKS_HOST = "127.0.0.1"
DEFAULT_SOCKS_PORT = 9050
_BOOTSTRAP_TIMEOUT = 120     # seconds to wait for tor to reach 100% before giving up
_LAUNCH_TIMEOUT = 90


def is_onion_v3(host):
    """True iff `host` is a syntactically valid v3 .onion address."""
    return bool(ONION_V3_RE.match(host.strip().lower())) if isinstance(host, str) else False


class TorManager:
    def __init__(self, node):
        self.node = node
        self.log = node.logger.app_log
        self.mode = getattr(node, "tor_mode", "off")
        self._proxy = None            # (host, port) once known, else None
        self._onion = None            # our published .onion, else None
        self._controller = None
        self._tor_process = None
        self._ready = False
        self._failed = False

    # --- lifecycle ----------------------------------------------------------
    def launch(self):
        """Bring the configured mode up. Returns True if outbound tor is usable (or clearnet fallback was
        taken), raises only when tor_required and tor could not be brought up."""
        if self.mode == "off":
            return True
        if self.mode == "external":
            # Trust an already-running daemon; just record where its SOCKS proxy is.
            self._proxy = (getattr(self.node, "tor_socks_host", DEFAULT_SOCKS_HOST),
                           int(getattr(self.node, "tor_socks_port", DEFAULT_SOCKS_PORT)))
            self._ready = True
            self.log.warning("Status: Tor external mode — routing outbound via SOCKS %s:%d "
                             "(inbound listener suppressed)" % self._proxy)
            return True
        if self.mode == "managed":
            return self._launch_managed()
        # unknown mode -> treat as off (defensive; options coercion should prevent this)
        self.log.warning("Status: unknown tor mode %r — running clearnet" % (self.mode,))
        self.mode = "off"
        return True

    def _fail(self, msg):
        """Common failure handling: fail-fast if tor_required, else fall back to clearnet."""
        self._failed = True
        self._proxy = None
        self._ready = False
        if getattr(self.node, "tor_required", False):
            raise RuntimeError("tor_required=True but Tor could not be brought up: " + msg)
        self.log.warning("Status: Tor unavailable (%s) — FALLING BACK TO CLEARNET "
                         "(set tor_required=True to fail instead)" % msg)
        return True   # node continues on clearnet

    def _launch_managed(self):
        try:
            import stem.process
            from stem.control import Controller
            from stem import Signal
        except ImportError:
            return self._fail("the 'stem' package is not installed (pip install stem)")

        data_dir = getattr(self.node, "tor_data_dir", "") or os.path.join("static", "tor_data")
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as e:
            return self._fail("cannot create tor data dir %s: %s" % (data_dir, e))

        cfg = {
            "SocksPort": "auto",          # avoid colliding with a system tor on 9050
            "ControlPort": "auto",
            "CookieAuthentication": "1",
            "DataDirectory": data_dir,
        }
        tor_binary = getattr(self.node, "tor_binary", "") or None

        def _boot(line):
            if "Bootstrapped " in line:
                self.log.warning("Status: tor %s" % line.strip().split("Bootstrapped ", 1)[1][:40])

        try:
            self._tor_process = stem.process.launch_tor_with_config(
                config=cfg, tor_cmd=tor_binary or "tor", take_ownership=True,
                init_msg_handler=_boot, timeout=_LAUNCH_TIMEOUT)
        except OSError as e:
            return self._fail("could not launch tor binary: %s" % e)

        try:
            ctrl_port = self._read_control_port(data_dir)
            self._controller = Controller.from_port(port=ctrl_port)
            self._controller.authenticate()
        except Exception as e:                          # noqa: BLE001 - any stem/control error -> fallback
            self.stop()
            return self._fail("could not authenticate to tor control port: %s" % e)

        if not self._await_bootstrap():
            self.stop()
            return self._fail("tor did not finish bootstrapping within %ds" % _BOOTSTRAP_TIMEOUT)

        # derive the real SOCKS port tor chose (SocksPort=auto)
        try:
            from stem import Listener
            socks = self._controller.get_listeners(Listener.SOCKS)
            self._proxy = (socks[0][0], int(socks[0][1])) if socks else (DEFAULT_SOCKS_HOST, DEFAULT_SOCKS_PORT)
        except Exception as e:                          # noqa: BLE001
            self.stop()
            return self._fail("could not read tor SOCKS port: %s" % e)

        if getattr(self.node, "tor_onion", False):
            self._publish_onion()

        self._ready = True
        self.log.warning("Status: Tor managed mode ready — SOCKS %s:%d%s"
                         % (self._proxy[0], self._proxy[1],
                            (" onion=%s" % self._onion) if self._onion else ""))
        return True

    def _read_control_port(self, data_dir):
        """ControlPort=auto writes the chosen port to <data_dir>/control_port (or '+__ControlPort')."""
        for name in ("control_port",):
            p = os.path.join(data_dir, name)
            if os.path.exists(p):
                with open(p) as f:
                    txt = f.read().strip()
                # format may be "PORT=127.0.0.1:NNNN" or just "NNNN"
                return int(txt.rsplit(":", 1)[-1].split("=")[-1])
        raise RuntimeError("tor did not report its control port")

    def _await_bootstrap(self):
        deadline = time.time() + _BOOTSTRAP_TIMEOUT
        while time.time() < deadline:
            try:
                phase = self._controller.get_info("status/bootstrap-phase")
            except Exception:                            # noqa: BLE001
                phase = ""
            if "PROGRESS=100" in phase or "TAG=done" in phase:
                return True
            time.sleep(1)
        return False

    def _publish_onion(self):
        """Create an EPHEMERAL v3 hidden service mapping <onion>:node.port -> 127.0.0.1:node.port. The
        ED25519 key is persisted (0600) so the .onion is stable across restarts, unless tor_onion_key is
        empty (throwaway address)."""
        key_path = getattr(self.node, "tor_onion_key", "") or ""
        key_type, key_content = "NEW", "ED25519-V3"
        if key_path and os.path.exists(key_path):
            try:
                with open(key_path) as f:
                    saved = f.read().strip()
                key_type, key_content = saved.split(":", 1)
            except Exception:                            # noqa: BLE001
                key_type, key_content = "NEW", "ED25519-V3"
        try:
            port = int(getattr(self.node, "port", 5658))
            resp = self._controller.create_ephemeral_hidden_service(
                {port: "127.0.0.1:%d" % port}, key_type=key_type, key_content=key_content,
                await_publication=True)
            self._onion = resp.service_id + ".onion"
            if key_path and resp.private_key:            # persist on first creation
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write("%s:%s" % (resp.private_key_type, resp.private_key))
        except Exception as e:                            # noqa: BLE001 - onion is best-effort
            self.log.warning("Status: could not publish onion service: %s" % e)
            self._onion = None

    def stop(self):
        try:
            if self._controller is not None:
                self._controller.close()
        except Exception:                                 # noqa: BLE001
            pass
        try:
            if self._tor_process is not None:
                self._tor_process.kill()
        except Exception:                                 # noqa: BLE001
            pass
        self._controller = None
        self._tor_process = None
        self._ready = False

    # --- query surface (consumed by worker.py / peers_storage.py / node.py) -------------------------
    def get_proxy(self):
        """(host, port) SOCKS proxy to route an outbound dial through, or None for clearnet/not-ready."""
        return self._proxy if self._ready else None

    def is_ready(self):
        return self._ready

    def onion_address(self):
        return self._onion

    def signal_newnym(self):
        """Request a fresh set of circuits (rate-limited by tor)."""
        if self._controller is None:
            return False
        try:
            from stem import Signal
            wait = self._controller.get_newnym_wait()
            if wait > 0:
                return False
            self._controller.signal(Signal.NEWNYM)
            return True
        except Exception:                                 # noqa: BLE001
            return False
