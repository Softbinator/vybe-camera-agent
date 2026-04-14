import logging
import os
import queue
import shutil
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent_state import AgentState

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0  # seconds between directory scans


class FileReplayWorker(threading.Thread):
    """Replays pre-recorded .mp4 files as if they were live camera chunks.

    Activated when a camera entry has ``source: file`` in config.yaml.
    Watches ``replay_dir`` for .mp4 files (sorted by name), copies each one
    into ``temp_dir/<label>/`` and enqueues it for the Uploader — exactly as
    CameraWorker does with ffmpeg-produced segments.

    When ``loop: true``, it cycles through all files indefinitely.
    When ``loop: false`` (default), it stops after one pass.

    Example config.yaml entry::

        cameras:
          - label: test-cam
            source: file
            replay_dir: /mock_videos
            loop: true
    """

    def __init__(
        self,
        camera: dict,
        config: dict,
        upload_queue: queue.Queue,
        per_stop_event: threading.Event,
        global_stop_event: threading.Event,
        state: "AgentState",
    ) -> None:
        super().__init__(name=f"replay-{camera['label']}", daemon=True)
        self.label = camera["label"]
        self.replay_dir = camera["replay_dir"]
        self.loop = camera.get("loop", False)
        self.chunk_duration = int(config["chunk_duration_seconds"])
        self.temp_dir = os.path.join(config["temp_dir"], self.label)
        self.upload_queue = upload_queue
        self.per_stop_event = per_stop_event
        self.global_stop_event = global_stop_event
        self.state = state

    def _should_stop(self) -> bool:
        return self.per_stop_event.is_set() or self.global_stop_event.is_set()

    def run(self) -> None:
        os.makedirs(self.temp_dir, exist_ok=True)
        self.state.update_camera_status(self.label, state="connected")

        while not self._should_stop():
            files = self._collect_files()

            if not files:
                logger.info("[%s] replay_dir '%s' is empty — waiting for files", self.label, self.replay_dir)
                self.state.update_camera_status(self.label, state="waiting", last_error="replay_dir is empty")
                self._interruptible_sleep(POLL_INTERVAL * 3)
                continue

            self.state.update_camera_status(self.label, state="connected", last_error=None)
            logger.info("[%s] starting replay pass over %d file(s)", self.label, len(files))

            for src_path in files:
                if self._should_stop():
                    break

                dest_path = os.path.join(self.temp_dir, os.path.basename(src_path))
                try:
                    shutil.copy2(src_path, dest_path)
                except OSError as exc:
                    logger.warning("[%s] could not copy %s: %s", self.label, src_path, exc)
                    continue

                logger.info("[%s] enqueuing replay chunk: %s", self.label, os.path.basename(dest_path))
                self.upload_queue.put({"label": self.label, "path": dest_path})
                self.state.record_chunk_enqueued(self.label)

                # Pace injection so we don't flood the upload queue
                self._interruptible_sleep(self.chunk_duration)

            if not self.loop:
                logger.info("[%s] replay complete (loop=false), stopping", self.label)
                break

            if not self._should_stop():
                logger.info("[%s] looping replay", self.label)

        self.state.update_camera_status(self.label, state="stopped")
        logger.info("[%s] replay worker stopped", self.label)

    def _collect_files(self) -> list[str]:
        """Return sorted list of .mp4 files from replay_dir."""
        try:
            entries = sorted(os.listdir(self.replay_dir))
        except OSError as exc:
            logger.warning("[%s] cannot read replay_dir '%s': %s", self.label, self.replay_dir, exc)
            return []
        return [
            os.path.join(self.replay_dir, e)
            for e in entries
            if e.lower().endswith(".mp4") and os.path.isfile(os.path.join(self.replay_dir, e))
        ]

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in short increments so stop events are noticed promptly."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._should_stop():
            time.sleep(0.5)
