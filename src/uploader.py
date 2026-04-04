import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1       # seconds
_BACKOFF_MAX = 300      # 5 minutes cap
_TOKEN_REFRESH_MARGIN = 30  # seconds before expiry to refresh token


def _parse_start_time(filename: str) -> str:
    """
    Parse the chunk start time from a filename like '20240101_120000.mp4'.
    Returns an ISO 8601 UTC datetime string. Falls back to current time on parse error.
    """
    basename = os.path.splitext(os.path.basename(filename))[0]
    for fmt in ("%Y%m%d_%H%M%S", "%Y%m%d-%H%M%S", "%Y%m%d_%H%M%S_%f"):
        try:
            dt = datetime.strptime(basename, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    logger.warning("Could not parse start time from filename '%s', using current time", filename)
    return datetime.now(timezone.utc).isoformat()


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
    """Consumes chunks from upload_queue and POSTs them to the vybe-backend API.
    Failed uploads are re-queued with exponential backoff."""

    def __init__(self, config: dict, upload_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(name="uploader", daemon=True)
        self.venue_id = config["venue_id"]
        self.chunk_duration = int(config["chunk_duration_seconds"])
        self.api_base_url = config["api_base_url"].rstrip("/")
        self.upload_url = f"{self.api_base_url}/api/upload/chunk"
        self.upload_queue = upload_queue
        self.stop_event = stop_event
        self._token_cache = _TokenCache(
            keycloak_url=config["keycloak_url"],
            realm=config["keycloak_realm"],
            client_id=config["keycloak_client_id"],
            client_secret=config["keycloak_client_secret"],
        )

    def run(self) -> None:
        logger.info("Uploader started (API: %s)", self.upload_url)
        while not self.stop_event.is_set():
            try:
                item = self.upload_queue.get(timeout=1)
            except queue.Empty:
                continue

            self._upload_with_retry(item)
            self.upload_queue.task_done()

        # Drain remaining items after stop is signalled
        logger.info("Uploader draining remaining queue...")
        while True:
            try:
                item = self.upload_queue.get_nowait()
            except queue.Empty:
                break
            self._upload_with_retry(item)
            self.upload_queue.task_done()

        logger.info("Uploader stopped")

    def _upload_with_retry(self, item: dict) -> None:
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

        filename = os.path.basename(local_path)
        logger.info("[%s] uploading %s → %s", label, filename, self.upload_url)

        try:
            token = self._token_cache.get_token()
            start_time = _parse_start_time(filename)

            with open(local_path, "rb") as f:
                resp = requests.post(
                    self.upload_url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "venueId": self.venue_id,
                        "cameraName": label,
                        "startTime": start_time,
                        "chunkDurationSeconds": str(self.chunk_duration),
                    },
                    files={"file": (filename, f, "video/mp4")},
                    timeout=120,
                )

            if resp.status_code in (200, 201):
                logger.info("[%s] uploaded %s (id: %s)", label, filename, resp.json().get("chunk", {}).get("id", "?"))
                try:
                    os.remove(local_path)
                except OSError as e:
                    logger.warning("[%s] could not delete local chunk %s: %s", label, local_path, e)
            else:
                logger.warning(
                    "[%s] upload failed (attempt %d): HTTP %d — %s",
                    label, attempt + 1, resp.status_code, resp.text[:200],
                )
                item["attempt"] = attempt + 1
                self.upload_queue.put(item)

        except requests.RequestException as exc:
            logger.warning("[%s] upload error (attempt %d): %s", label, attempt + 1, exc)
            item["attempt"] = attempt + 1
            self.upload_queue.put(item)
