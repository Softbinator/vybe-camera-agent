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
        # Global recording pause flag. When True, start_all / reload_config still
        # track cameras but don't spawn workers, and pause_all stops any running
        # workers without discarding their config.
        self._recording_paused: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_all(self, config: dict) -> None:
        self._config = config
        # Respect the persisted pause flag so recording stays off across restarts
        # if the operator explicitly paused it.
        self._recording_paused = bool(config.get("recording_paused", False))
        if self._recording_paused:
            logger.info("Recording is paused in config — workers will not be started")
            # Still register status entries so the dashboard shows the cameras.
            for cam in config.get("cameras", []):
                self._register_paused_status(cam)
            return
        for cam in config.get("cameras", []):
            self._start_worker(cam, config)

    def pause_all(self) -> int:
        """Stop every running worker without touching the configured camera list.
        Returns the number of workers stopped."""
        with self._lock:
            labels = list(self._workers.keys())
            self._recording_paused = True
            cam_configs = dict(self._camera_configs)
        for label in labels:
            self._stop_worker(label, timeout=10)
        # Mark status as paused so the UI reflects the reason for 'stopped'.
        with self._lock:
            for label, cam in cam_configs.items():
                st = self._status.get(label)
                if st is not None:
                    st["state"] = "paused"
                    st["pending_credentials"] = bool(cam.get("pending_credentials", False))
        logger.info("Recording paused — stopped %d worker(s)", len(labels))
        return len(labels)

    def resume_all(self) -> int:
        """Restart workers for every configured camera. Returns the number started."""
        with self._lock:
            config = dict(self._config)
            self._recording_paused = False
        started = 0
        for cam in config.get("cameras", []):
            label = cam.get("label")
            with self._lock:
                if label in self._workers:
                    continue
            self._start_worker(cam, config)
            if not bool(cam.get("pending_credentials", False)):
                started += 1
        logger.info("Recording resumed — started %d worker(s)", started)
        return started

    def is_paused(self) -> bool:
        with self._lock:
            return self._recording_paused

    def purge_upload_queue(self) -> int:
        """Drop every pending upload. Returns the number of items discarded."""
        dropped = 0
        while True:
            try:
                self.upload_queue.get_nowait()
            except queue.Empty:
                break
            self.upload_queue.task_done()
            dropped += 1
        logger.warning("Upload queue purged — %d chunk(s) dropped", dropped)
        return dropped

    def _register_paused_status(self, cam_config: dict) -> None:
        """Insert a dashboard status entry for a camera that is not being started."""
        label = cam_config["label"]
        pending = bool(cam_config.get("pending_credentials", False))
        with self._lock:
            self._camera_configs[label] = cam_config
            self._status[label] = {
                "label": label,
                "source": cam_config.get("source", "rtsp"),
                "rtsp_url": cam_config.get("rtsp_url", ""),
                "device": cam_config.get("device", ""),
                "replay_dir": cam_config.get("replay_dir", ""),
                "state": "awaiting_credentials" if pending else "paused",
                "reconnect_attempts": 0,
                "last_chunk_at": None,
                "chunks_enqueued": 0,
                "last_error": None,
                "auto_discovered": bool(cam_config.get("auto_discovered", False)),
                "pending_credentials": pending,
            }

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
            recording_paused = self._recording_paused

        return {
            "queue_depth": self.upload_queue.qsize(),
            "storage_mode": storage_mode,
            "output_dir": output_dir,
            "recording_paused": recording_paused,
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
        pending = bool(cam_config.get("pending_credentials", False))

        initial_status = {
            "label": label,
            "source": source,
            "rtsp_url": cam_config.get("rtsp_url", ""),
            "device": cam_config.get("device", ""),
            "replay_dir": cam_config.get("replay_dir", ""),
            "state": "awaiting_credentials" if pending else "starting",
            "reconnect_attempts": 0,
            "last_chunk_at": None,
            "chunks_enqueued": 0,
            "last_error": None,
            "auto_discovered": bool(cam_config.get("auto_discovered", False)),
            "pending_credentials": pending,
        }

        with self._lock:
            self._stop_events[label] = per_stop
            self._camera_configs[label] = cam_config
            self._status[label] = initial_status

        if pending:
            # Track the camera but don't spawn a worker until credentials are set.
            logger.info("[%s] camera awaiting credentials — worker not started", label)
            return

        with self._lock:
            paused = self._recording_paused
        if paused:
            # Recording is globally paused — track the camera but don't spawn.
            with self._lock:
                if label in self._status:
                    self._status[label]["state"] = "paused"
            logger.info("[%s] recording paused — worker not started", label)
            return

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
