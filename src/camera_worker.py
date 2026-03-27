import logging
import os
import queue
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

RECONNECT_DELAY = 5  # seconds to wait before reconnecting after ffmpeg crash


class CameraWorker(threading.Thread):
    """Captures an RTSP stream with ffmpeg, splits it into fixed-duration chunks,
    and pushes completed chunk paths into the shared upload_queue."""

    def __init__(self, camera: dict, config: dict, upload_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(name=f"camera-{camera['label']}", daemon=True)
        self.label = camera["label"]
        self.rtsp_url = camera["rtsp_url"]
        self.chunk_duration = int(config["chunk_duration_seconds"])
        self.temp_dir = os.path.join(config["temp_dir"], self.label)
        self.upload_queue = upload_queue
        self.stop_event = stop_event

    def run(self) -> None:
        os.makedirs(self.temp_dir, exist_ok=True)
        segment_list = os.path.join(self.temp_dir, "segments.txt")

        while not self.stop_event.is_set():
            # Truncate the segment list so we only process newly written entries on reconnect
            open(segment_list, "w").close()
            proc = self._start_ffmpeg(segment_list)
            logger.info("[%s] ffmpeg started (pid %d)", self.label, proc.pid)

            watcher = threading.Thread(
                target=self._watch_segments,
                args=(segment_list, proc),
                daemon=True,
            )
            watcher.start()

            proc.wait()
            watcher.join(timeout=10)

            if self.stop_event.is_set():
                break

            logger.warning("[%s] ffmpeg exited (code %d), reconnecting in %ds", self.label, proc.returncode, RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)

        logger.info("[%s] worker stopped", self.label)

    def _start_ffmpeg(self, segment_list: str) -> subprocess.Popen:
        output_pattern = os.path.join(self.temp_dir, "%Y%m%d_%H%M%S.mp4")
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.chunk_duration),
            "-segment_format", "mp4",
            "-segment_list", segment_list,
            "-segment_list_flags", "+live",
            "-reset_timestamps", "1",
            "-strftime", "1",
            output_pattern,
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def _watch_segments(self, segment_list: str, proc: subprocess.Popen) -> None:
        """Poll the segment list file for new entries and enqueue completed chunks."""
        seen = set()
        poll_interval = 1.0

        while proc.poll() is None and not self.stop_event.is_set():
            try:
                with open(segment_list, "r") as f:
                    lines = [line.strip() for line in f if line.strip()]
            except OSError:
                time.sleep(poll_interval)
                continue

            for path in lines:
                if path not in seen:
                    seen.add(path)
                    # The last line is the segment currently being written; skip it
                    # Only enqueue it once the next segment appears (i.e. it's no longer last)
                    pass

            # All lines except the last are complete (ffmpeg has moved on)
            complete = lines[:-1] if len(lines) > 1 else []
            for path in complete:
                if path not in seen or path in seen:
                    # Normalise to absolute path
                    abs_path = path if os.path.isabs(path) else os.path.join(self.temp_dir, path)
                    if abs_path not in getattr(self, "_enqueued", set()):
                        if not hasattr(self, "_enqueued"):
                            self._enqueued = set()
                        self._enqueued.add(abs_path)
                        logger.info("[%s] chunk ready: %s", self.label, os.path.basename(abs_path))
                        self.upload_queue.put({"label": self.label, "path": abs_path})

            time.sleep(poll_interval)

        # After ffmpeg exits, enqueue any remaining completed segments
        try:
            with open(segment_list, "r") as f:
                lines = [line.strip() for line in f if line.strip()]
            if not hasattr(self, "_enqueued"):
                self._enqueued = set()
            for path in lines:
                abs_path = path if os.path.isabs(path) else os.path.join(self.temp_dir, path)
                if abs_path not in self._enqueued and os.path.exists(abs_path):
                    self._enqueued.add(abs_path)
                    logger.info("[%s] final chunk: %s", self.label, os.path.basename(abs_path))
                    self.upload_queue.put({"label": self.label, "path": abs_path})
        except OSError:
            pass
