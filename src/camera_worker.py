import logging
import os
import queue
import random
import subprocess
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent_state import AgentState

logger = logging.getLogger(__name__)

RECONNECT_BASE = 5      # seconds — first retry delay
RECONNECT_MAX = 300     # seconds — ceiling for exponential backoff


class CameraWorker(threading.Thread):
    """Captures a camera stream with ffmpeg, splits it into fixed-duration chunks,
    and pushes completed chunk paths into the shared upload_queue.

    Supports two input sources:
    - ``rtsp``  : network IP camera via RTSP (default)
    - ``v4l2``  : local USB / V4L2 device (e.g. /dev/video0)

    Reconnects automatically on ffmpeg crash with exponential backoff + jitter.
    Status is reported to AgentState so the web dashboard stays current.
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
        super().__init__(name=f"camera-{camera['label']}", daemon=True)
        self.label = camera["label"]
        self.source = camera.get("source", "rtsp")
        # RTSP fields
        self.rtsp_url = camera.get("rtsp_url", "")
        # V4L2 fields
        self.device = camera.get("device", "/dev/video0")
        self.framerate = camera.get("framerate")        # e.g. 30
        self.video_size = camera.get("video_size")      # e.g. "1920x1080"
        self.input_format = camera.get("input_format")  # e.g. "h264", "mjpeg"
        # Common
        self.chunk_duration = int(config["chunk_duration_seconds"])
        self.output_height = config.get("output_height")
        self.temp_dir = os.path.join(config["temp_dir"], self.label)
        self.upload_queue = upload_queue
        self.per_stop_event = per_stop_event
        self.global_stop_event = global_stop_event
        self.state = state

    def _should_stop(self) -> bool:
        return self.per_stop_event.is_set() or self.global_stop_event.is_set()

    def run(self) -> None:
        os.makedirs(self.temp_dir, exist_ok=True)
        segment_list = os.path.join(self.temp_dir, "segments.txt")
        attempt = 0

        while not self._should_stop():
            self.state.update_camera_status(
                self.label,
                state="reconnecting" if attempt > 0 else "connecting",
                reconnect_attempts=attempt,
            )

            # Truncate the segment list so we only process new entries on reconnect
            open(segment_list, "w").close()
            proc = self._start_ffmpeg(segment_list)
            logger.info("[%s] ffmpeg started (pid %d, attempt %d)", self.label, proc.pid, attempt)

            self.state.update_camera_status(self.label, state="connected", last_error=None)

            watcher = threading.Thread(
                target=self._watch_segments,
                args=(segment_list, proc),
                daemon=True,
            )
            watcher.start()

            # Poll so we can react to per_stop_event while ffmpeg is running
            while proc.poll() is None and not self._should_stop():
                time.sleep(0.5)

            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            watcher.join(timeout=10)

            if self._should_stop():
                break

            exit_code = proc.returncode
            stderr_tail = self._read_stderr_tail(proc)
            error_msg = f"ffmpeg exited (code {exit_code})"
            if stderr_tail:
                error_msg += f": {stderr_tail}"

            attempt += 1
            delay = min(RECONNECT_BASE * (2 ** (attempt - 1)), RECONNECT_MAX)
            # Add ±20% jitter to avoid thundering herd with many cameras
            delay = delay * (0.8 + random.random() * 0.4)

            logger.warning(
                "[%s] %s — reconnecting in %.1fs (attempt %d)",
                self.label, error_msg, delay, attempt,
            )
            self.state.update_camera_status(
                self.label,
                state="reconnecting",
                reconnect_attempts=attempt,
                last_error=error_msg,
            )

            # Sleep in small increments so we react to stop events promptly
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not self._should_stop():
                time.sleep(0.5)

        self.state.update_camera_status(self.label, state="stopped")
        logger.info("[%s] worker stopped", self.label)

    def _start_ffmpeg(self, segment_list: str) -> subprocess.Popen:
        output_pattern = os.path.join(self.temp_dir, "%Y%m%d_%H%M%S.mp4")
        input_opts = self._build_input_opts()
        video_opts = self._build_video_opts()

        cmd = [
            "ffmpeg",
            *input_opts,
            *video_opts,
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

    def _build_input_opts(self) -> list[str]:
        if self.source == "v4l2":
            opts = ["-f", "v4l2"]
            if self.input_format:
                opts += ["-input_format", self.input_format]
            if self.framerate:
                opts += ["-framerate", str(self.framerate)]
            if self.video_size:
                opts += ["-video_size", self.video_size]
            opts += ["-i", self.device]
            return opts
        # rtsp (default)
        return ["-rtsp_transport", "tcp", "-i", self.rtsp_url]

    def _build_video_opts(self) -> list[str]:
        if self.source == "v4l2":
            if self.input_format == "h264":
                # Camera streams native H.264 — stream-copy, zero CPU cost
                return ["-c:v", "copy", "-an"]
            # Raw/MJPEG input — encode to H.264 for reliable mp4 segmenting
            if self.output_height:
                return [
                    "-vf", f"scale=-2:{self.output_height}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-an",
                ]
            return ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an"]

        # rtsp: stream-copy unless a target height is requested
        if self.output_height:
            return [
                "-vf", f"scale=-2:{self.output_height}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac",
            ]
        return ["-c", "copy"]

    def _read_stderr_tail(self, proc: subprocess.Popen) -> str:
        """Return the last non-empty line from ffmpeg's stderr for error reporting."""
        try:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            lines = [l.strip() for l in stderr_bytes.decode(errors="replace").splitlines() if l.strip()]
            return lines[-1] if lines else ""
        except Exception:
            return ""

    def _watch_segments(self, segment_list: str, proc: subprocess.Popen) -> None:
        processed: set = set()
        poll_interval = 1.0

        while proc.poll() is None and not self._should_stop():
            self._enqueue_new_chunks(segment_list, processed, flush=False)
            time.sleep(poll_interval)

        self._enqueue_new_chunks(segment_list, processed, flush=True)

    def _enqueue_new_chunks(self, segment_list: str, processed: set, flush: bool) -> None:
        try:
            with open(segment_list) as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            return

        complete = lines if flush else lines[:-1]

        for path in complete:
            abs_path = path if os.path.isabs(path) else os.path.join(self.temp_dir, path)
            if abs_path in processed:
                continue
            processed.add(abs_path)
            logger.info("[%s] chunk ready: %s", self.label, os.path.basename(abs_path))
            self.upload_queue.put({"label": self.label, "path": abs_path})
            self.state.record_chunk_enqueued(self.label)
