import logging
import queue
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.camera_worker import CameraWorker
    from src.file_replay_worker import FileReplayWorker

logger = logging.getLogger(__name__)

WorkerType = "CameraWorker | FileReplayWorker"


class AgentState:
    """Central registry for all camera workers and their runtime status.

    Thread-safe: all mutations go through a single lock so the web server
    and worker threads can read/write concurrently without races.
    """

    def __init__(self, upload_queue: queue.Queue, global_stop_event: threading.Event) -> None:
        self.upload_queue = upload_queue
        self.global_stop_event = global_stop_event

        self._lock = threading.Lock()
        self._workers: dict[str, WorkerType] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._camera_configs: dict[str, dict] = {}
        self._status: dict[str, dict] = {}
        self._config: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_all(self, config: dict) -> None:
        self._config = config
        for cam in config.get("cameras", []):
            self._start_worker(cam, config)

    def stop_all(self) -> None:
        with self._lock:
            labels = list(self._workers.keys())

        for label in labels:
            self._stop_worker(label, timeout=15)

    def restart_camera(self, label: str) -> bool:
        """Stop and restart a single camera worker. Returns False if label unknown."""
        with self._lock:
            if label not in self._workers:
                return False
            cam_config = self._camera_configs.get(label)
            config = self._config

        if cam_config is None:
            return False

        logger.info("[%s] restart requested via web UI", label)
        self._stop_worker(label, timeout=15)
        self._start_worker(cam_config, config)
        return True

    def reload_config(self, new_config: dict) -> None:
        """Diff new_config against the running workers and apply changes hot."""
        old_labels = set(self._camera_configs.keys())
        new_cameras = {cam["label"]: cam for cam in new_config["cameras"]}
        new_labels = set(new_cameras.keys())

        # Stop removed cameras
        for label in old_labels - new_labels:
            logger.info("[%s] camera removed by config reload — stopping", label)
            self._stop_worker(label, timeout=15)

        # Restart changed cameras
        for label in old_labels & new_labels:
            old_cam = self._camera_configs[label]
            new_cam = new_cameras[label]
            if old_cam != new_cam:
                logger.info("[%s] camera config changed — restarting", label)
                self._stop_worker(label, timeout=15)
                self._start_worker(new_cam, new_config)

        # Start new cameras
        for label in new_labels - old_labels:
            logger.info("[%s] new camera added by config reload — starting", label)
            self._start_worker(new_cameras[label], new_config)

        with self._lock:
            self._config = new_config

    # ------------------------------------------------------------------
    # Status API (called by web server)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            cameras = [dict(s) for s in self._status.values()]
            storage_mode = self._config.get("storage_mode", "upload")
            output_dir = self._config.get("output_dir", "/output")

        return {
            "queue_depth": self.upload_queue.qsize(),
            "storage_mode": storage_mode,
            "output_dir": output_dir,
            "cameras": cameras,
        }

    def update_camera_status(self, label: str, **kwargs) -> None:
        """Called by worker threads to push status updates."""
        with self._lock:
            if label in self._status:
                self._status[label].update(kwargs)

    def get_config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def get_storage_mode(self) -> str:
        with self._lock:
            return self._config.get("storage_mode", "upload")

    def get_output_dir(self) -> str:
        with self._lock:
            return self._config.get("output_dir", "/output")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_worker(self, cam_config: dict, config: dict) -> None:
        from src.camera_worker import CameraWorker
        from src.file_replay_worker import FileReplayWorker

        label = cam_config["label"]
        per_stop = threading.Event()
        source = cam_config.get("source", "rtsp")

        initial_status = {
            "label": label,
            "source": source,
            "rtsp_url": cam_config.get("rtsp_url", ""),
            "device": cam_config.get("device", ""),
            "replay_dir": cam_config.get("replay_dir", ""),
            "state": "starting",
            "reconnect_attempts": 0,
            "last_chunk_at": None,
            "chunks_enqueued": 0,
            "last_error": None,
        }

        with self._lock:
            self._stop_events[label] = per_stop
            self._camera_configs[label] = cam_config
            self._status[label] = initial_status

        if source == "file":
            worker = FileReplayWorker(cam_config, config, self.upload_queue, per_stop, self.global_stop_event, self)
        else:
            worker = CameraWorker(cam_config, config, self.upload_queue, per_stop, self.global_stop_event, self)

        with self._lock:
            self._workers[label] = worker

        worker.start()

    def _stop_worker(self, label: str, timeout: int = 15) -> None:
        with self._lock:
            stop_event = self._stop_events.get(label)
            worker = self._workers.get(label)

        if stop_event:
            stop_event.set()

        if worker and worker.is_alive():
            worker.join(timeout=timeout)

        with self._lock:
            self._workers.pop(label, None)
            self._stop_events.pop(label, None)
            self._camera_configs.pop(label, None)
            if label in self._status:
                self._status[label]["state"] = "stopped"

    def record_chunk_enqueued(self, label: str) -> None:
        with self._lock:
            if label in self._status:
                self._status[label]["chunks_enqueued"] += 1
                self._status[label]["last_chunk_at"] = datetime.now(timezone.utc).isoformat()
