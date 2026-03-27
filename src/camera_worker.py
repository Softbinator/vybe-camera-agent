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
        self.output_height = config.get("output_height")  # None means keep original resolution
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

        if self.output_height:
            # Transcode: scale to target height, keeping aspect ratio.
            # -2 ensures width stays divisible by 2 (required by libx264).
            video_opts = [
                "-vf", f"scale=-2:{self.output_height}",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
            ]
        else:
            # No transcoding — stream copy at original resolution.
            video_opts = ["-c", "copy"]

        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
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

    def _watch_segments(self, segment_list: str, proc: subprocess.Popen) -> None:
        """Poll the segment list file and enqueue completed chunks.

        `processed` is local to this watcher session. Since the segment list is
        truncated before each ffmpeg restart, a fresh set is correct per session.
        """
        processed: set = set()
        poll_interval = 1.0

        while proc.poll() is None and not self.stop_event.is_set():
            self._enqueue_new_chunks(segment_list, processed, flush=False)
            time.sleep(poll_interval)

        # Flush any remaining segments after ffmpeg exits
        self._enqueue_new_chunks(segment_list, processed, flush=True)

    def _enqueue_new_chunks(self, segment_list: str, processed: set, flush: bool) -> None:
        try:
            with open(segment_list) as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            return

        # All lines except the last are complete — ffmpeg is still writing the last one.
        # On flush (after ffmpeg exits) all lines are complete.
        complete = lines if flush else lines[:-1]

        for path in complete:
            abs_path = path if os.path.isabs(path) else os.path.join(self.temp_dir, path)
            if abs_path in processed:
                continue
            processed.add(abs_path)
            logger.info("[%s] chunk ready: %s", self.label, os.path.basename(abs_path))
            self.upload_queue.put({"label": self.label, "path": abs_path})
