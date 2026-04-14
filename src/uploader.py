import logging
import os
import queue
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from src.agent_state import AgentState

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1       # seconds
_BACKOFF_MAX = 300      # 5 minutes cap
_TOKEN_REFRESH_MARGIN = 30  # seconds before expiry to refresh token


def _parse_start_time(filename: str) -> str:
    """Parse the chunk start time from a filename like '20240101_120000.mp4'.
    Returns an ISO 8601 UTC datetime string. Falls back to current time on parse error.
    """
    basename = os.path.splitext(os.path.basename(filename))[0]
    for fmt in ("%Y%m%d_%H%M%S", "%Y%m%d-%H%M%S", "%Y%m%d_%H%M%S_%f"):
        try:
            dt = datetime.strptime(basename, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    logger.warning("Could not parse start time from filename '%s', using current time", filename)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TokenCache:
    """Thread-safe Keycloak client-credentials token cache."""

    def __init__(self, keycloak_url: str, realm: str, client_id: str, client_secret: str):
        self._token_url = f"{keycloak_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._expires_at - _TOKEN_REFRESH_MARGIN:
                return self._access_token
            self._refresh()
            return self._access_token  # type: ignore[return-value]

    def _refresh(self) -> None:
        resp = requests.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.monotonic() + data.get("expires_in", 300)
        logger.debug("Keycloak token refreshed, expires in %ds", data.get("expires_in", 300))


class Uploader(threading.Thread):
    """Consumes chunks from upload_queue and handles them according to storage_mode:

    - ``upload``  : POST to backend API, then delete local temp file (default)
    - ``local``   : move to output_dir/<label>/, no API call
    - ``both``    : POST to backend API, then move to output_dir/<label>/ instead of deleting

    All live config (API URL, Keycloak credentials, storage mode) is read from
    AgentState on every chunk so that connection-settings changes take effect
    immediately without restarting the process.
    """

    def __init__(self, state: "AgentState", upload_queue: queue.Queue, stop_event: threading.Event) -> None:
        super().__init__(name="uploader", daemon=True)
        self.state = state
        self.upload_queue = upload_queue
        self.stop_event = stop_event
        # Token cache is rebuilt lazily whenever Keycloak settings change
        self._token_cache: _TokenCache | None = None
        self._token_cache_key: tuple = ()

    def run(self) -> None:
        logger.info("Uploader started")
        while not self.stop_event.is_set():
            try:
                item = self.upload_queue.get(timeout=1)
            except queue.Empty:
                continue
            self._process(item)
            self.upload_queue.task_done()

        logger.info("Uploader draining remaining queue…")
        while True:
            try:
                item = self.upload_queue.get_nowait()
            except queue.Empty:
                break
            self._process(item)
            self.upload_queue.task_done()

        logger.info("Uploader stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a valid Bearer token, rebuilding the cache if Keycloak config changed."""
        cfg = self.state.get_config()
        key = (
            cfg.get("keycloak_url", ""),
            cfg.get("keycloak_realm", ""),
            cfg.get("keycloak_client_id", ""),
            cfg.get("keycloak_client_secret", ""),
        )
        if self._token_cache is None or self._token_cache_key != key:
            logger.info("Keycloak config changed — rebuilding token cache")
            self._token_cache = _TokenCache(
                keycloak_url=cfg["keycloak_url"],
                realm=cfg["keycloak_realm"],
                client_id=cfg["keycloak_client_id"],
                client_secret=cfg["keycloak_client_secret"],
            )
            self._token_cache_key = key
        return self._token_cache.get_token()

    def _save_locally(self, local_path: str, label: str, output_dir: str) -> bool:
        """Move *local_path* into *output_dir*/<label>/. Returns True on success."""
        dest_dir = os.path.join(output_dir, label)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(local_path))
            shutil.move(local_path, dest)
            logger.info("[%s] saved locally → %s", label, dest)
            return True
        except OSError as exc:
            logger.warning("[%s] could not save locally: %s", label, exc)
            return False

    def _process(self, item: dict) -> None:
        """Dispatch to the correct handler based on current storage_mode."""
        cfg = self.state.get_config()
        storage_mode = cfg.get("storage_mode", "upload")

        if storage_mode == "local":
            self._handle_local(item, cfg)
        else:
            self._upload_with_retry(item, cfg, also_save_locally=(storage_mode == "both"))

    def _handle_local(self, item: dict, cfg: dict) -> None:
        label = item["label"]
        local_path = item["path"]
        if not os.path.exists(local_path):
            logger.debug("[%s] chunk not found, skipping: %s", label, os.path.basename(local_path))
            return
        output_dir = cfg.get("output_dir", "/output")
        self._save_locally(local_path, label, output_dir)

    def _upload_with_retry(self, item: dict, cfg: dict, also_save_locally: bool = False) -> None:
        label = item["label"]
        local_path = item["path"]
        attempt = item.get("attempt", 0)

        if not os.path.exists(local_path):
            logger.debug("[%s] chunk not found, skipping: %s", label, os.path.basename(local_path))
            return

        if attempt > 0:
            delay = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)
            logger.info("[%s] retry %d — waiting %ds before upload", label, attempt, delay)
            time.sleep(delay)

        # Re-read live config values so switching API URL / credentials takes effect
        api_base_url = cfg.get("api_base_url", "").rstrip("/")
        upload_url = f"{api_base_url}/api/upload/chunk"
        venue_id = cfg.get("venue_id", "")
        chunk_duration = int(cfg.get("chunk_duration_seconds", 30))
        output_dir = cfg.get("output_dir", "/output")

        filename = os.path.basename(local_path)
        logger.info("[%s] uploading %s → %s", label, filename, upload_url)

        try:
            token = self._get_token()
            start_time = _parse_start_time(filename)

            with open(local_path, "rb") as f:
                resp = requests.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "venueId": venue_id,
                        "cameraName": label,
                        "startTime": start_time,
                        "chunkDurationSeconds": str(chunk_duration),
                    },
                    files={"file": (filename, f, "video/mp4")},
                    timeout=120,
                )

            if resp.status_code in (200, 201):
                chunk_id = resp.json().get("chunk", {}).get("id", "?")
                logger.info("[%s] uploaded %s (id: %s)", label, filename, chunk_id)
                if also_save_locally:
                    self._save_locally(local_path, label, output_dir)
                else:
                    try:
                        os.remove(local_path)
                    except OSError as exc:
                        logger.warning("[%s] could not delete temp chunk %s: %s", label, local_path, exc)
            else:
                logger.warning(
                    "[%s] upload failed (attempt %d): HTTP %d — %s",
                    label, attempt + 1, resp.status_code, resp.text[:2000],
                )
                item["attempt"] = attempt + 1
                self.upload_queue.put(item)

        except requests.RequestException as exc:
            logger.warning("[%s] upload error (attempt %d): %s", label, attempt + 1, exc)
            item["attempt"] = attempt + 1
            self.upload_queue.put(item)
