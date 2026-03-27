import logging
import os
import queue
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1       # seconds
_BACKOFF_MAX = 300      # 5 minutes cap


class Uploader(threading.Thread):
    """Consumes chunks from upload_queue and uploads them via rclone.
    Failed uploads are re-queued with exponential backoff."""

    def __init__(self, config: dict, upload_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(name="uploader", daemon=True)
        self.remote = config["rclone_remote"]
        self.bucket_path = config["s3_bucket_path"].rstrip("/")
        self.upload_queue = upload_queue
        self.stop_event = stop_event

    def run(self) -> None:
        logger.info("Uploader started")
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
            logger.warning("[%s] chunk no longer exists, skipping: %s", label, local_path)
            return

        filename = os.path.basename(local_path)
        destination = f"{self.remote}:{self.bucket_path}/{label}/{filename}"

        if attempt > 0:
            delay = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)
            logger.info("[%s] retry %d — waiting %ds before upload", label, attempt, delay)
            time.sleep(delay)

        logger.info("[%s] uploading %s → %s", label, filename, destination)
        try:
            result = subprocess.run(
                ["rclone", "copyto", local_path, destination],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("[%s] uploaded %s", label, filename)
                try:
                    os.remove(local_path)
                except OSError as e:
                    logger.warning("[%s] could not delete local chunk %s: %s", label, local_path, e)
            else:
                logger.warning(
                    "[%s] rclone failed (attempt %d): %s",
                    label, attempt + 1, result.stderr.strip()
                )
                item["attempt"] = attempt + 1
                self.upload_queue.put(item)
        except FileNotFoundError:
            logger.error("rclone not found — ensure it is installed and on PATH")
            item["attempt"] = attempt + 1
            self.upload_queue.put(item)
