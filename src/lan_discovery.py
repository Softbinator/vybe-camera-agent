import logging
import os
import socket
import threading
import time
from typing import TYPE_CHECKING

import yaml

from src.config_loader import save_config

if TYPE_CHECKING:
    from src.agent_state import AgentState

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
SOCKET_CONNECT_TIMEOUT = 2.0
RTSP_PROBE_TIMEOUT = 3.0


class LanDiscovery(threading.Thread):
    """Watches the dnsmasq leases file and auto-registers new devices as pending cameras.

    Any device that gets a DHCP lease on the camera LAN is probed on the configured
    RTSP port. If it answers to an RTSP OPTIONS request on one of the common paths,
    a camera entry with ``pending_credentials: true`` is appended to config.yaml and
    the agent state is reloaded. The user then sets the RTSP user/password in the
    web dashboard.
    """

    def __init__(
        self,
        config_path: str,
        state: "AgentState",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="lan-discovery", daemon=True)
        self.config_path = config_path
        self.state = state
        self.stop_event = stop_event

    def run(self) -> None:
        config = self.state.get_config()
        lan = config.get("lan_discovery", {})
        if not lan.get("enabled"):
            logger.info("lan_discovery disabled — thread exiting")
            return

        leases_file = lan.get("leases_file", "/var/lib/dnsmasq/dnsmasq.leases")
        logger.info("lan_discovery started — watching %s", leases_file)

        # Seed seen-set with IPs already in config so we never re-discover
        # cameras the user added manually or that were discovered in a prior run.
        seen_ips = self._collect_known_ips(config)

        while not self.stop_event.is_set():
            try:
                self._tick(leases_file, seen_ips)
            except Exception:
                logger.exception("lan_discovery tick failed")
            self.stop_event.wait(POLL_INTERVAL_SECONDS)

        logger.info("lan_discovery stopped")

    # ------------------------------------------------------------------

    def _tick(self, leases_file: str, seen_ips: set) -> None:
        leases = self._read_leases(leases_file)
        if not leases:
            return

        new_ips = [ip for (_mac, ip, _name) in leases if ip not in seen_ips]
        if not new_ips:
            return

        config = self.state.get_config()
        lan = config.get("lan_discovery", {})
        port = int(lan.get("rtsp_port", 554))
        probe_paths = list(lan.get("probe_paths", ["/"]))

        added_any = False
        for ip in new_ips:
            seen_ips.add(ip)
            path = self._probe_rtsp(ip, port, probe_paths)
            if path is None:
                logger.info("lan_discovery: %s did not respond to RTSP on port %d — skipping", ip, port)
                continue
            logger.info("lan_discovery: RTSP camera found at %s (path=%s) — adding as pending", ip, path)
            self._append_camera(ip, port, path)
            added_any = True

        if added_any:
            new_config = self.state.get_config()  # re-read after write
            try:
                self.state.reload_config(new_config)
            except Exception:
                logger.exception("lan_discovery: reload_config failed")

    # ------------------------------------------------------------------

    @staticmethod
    def _read_leases(path: str) -> list[tuple[str, str, str]]:
        """Parse the dnsmasq leases file. Returns list of (mac, ip, hostname)."""
        if not os.path.exists(path):
            return []
        leases: list[tuple[str, str, str]] = []
        try:
            with open(path) as f:
                for line in f:
                    parts = line.strip().split()
                    # dnsmasq format: <expiry> <mac> <ip> <hostname> <client-id>
                    if len(parts) >= 4:
                        leases.append((parts[1], parts[2], parts[3]))
        except OSError:
            return []
        return leases

    @staticmethod
    def _collect_known_ips(config: dict) -> set:
        """Extract IPs already referenced in cameras[].rtsp_url so we skip re-discovery."""
        ips: set = set()
        for cam in config.get("cameras", []):
            url = cam.get("rtsp_url", "") or ""
            # crude extraction: pull the first dotted-quad we see
            for token in url.replace("/", " ").replace("@", " ").replace(":", " ").split():
                if token.count(".") == 3 and all(p.isdigit() for p in token.split(".")):
                    ips.add(token)
                    break
        return ips

    @staticmethod
    def _probe_rtsp(ip: str, port: int, paths: list[str]) -> str | None:
        """Return the first path that answers RTSP, or None if none do."""
        # Quick TCP check first to avoid long probe timeouts on non-camera devices
        try:
            with socket.create_connection((ip, port), timeout=SOCKET_CONNECT_TIMEOUT):
                pass
        except OSError:
            return None

        for path in paths:
            if LanDiscovery._rtsp_options(ip, port, path):
                return path
        return None

    @staticmethod
    def _rtsp_options(ip: str, port: int, path: str) -> bool:
        url = f"rtsp://{ip}:{port}{path}"
        req = (
            f"OPTIONS {url} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: vybe-camera-agent\r\n"
            f"\r\n"
        ).encode()
        try:
            with socket.create_connection((ip, port), timeout=RTSP_PROBE_TIMEOUT) as s:
                s.sendall(req)
                data = s.recv(512)
        except OSError:
            return False
        if not data:
            return False
        first = data.split(b"\r\n", 1)[0]
        # 200 = open (no auth or anonymous), 401 = auth required (still a real camera)
        return first.startswith(b"RTSP/1.0 200") or first.startswith(b"RTSP/1.0 401")

    # ------------------------------------------------------------------

    def _append_camera(self, ip: str, port: int, path: str) -> None:
        """Read config.yaml, append a pending-credentials camera entry, write back."""
        try:
            with open(self.config_path) as f:
                raw = yaml.safe_load(f) or {}
        except OSError:
            logger.exception("lan_discovery: cannot read %s", self.config_path)
            return

        cameras = raw.get("cameras") or []
        label = f"auto-{ip.replace('.', '-')}"
        if any(isinstance(c, dict) and c.get("label") == label for c in cameras):
            return

        cameras.append({
            "label": label,
            "source": "rtsp",
            "rtsp_url": f"rtsp://{{USER}}:{{PASS}}@{ip}:{port}{path}",
            "auto_discovered": True,
            "pending_credentials": True,
        })
        raw["cameras"] = cameras

        save_config(self.config_path, yaml.safe_dump(raw, sort_keys=False))
