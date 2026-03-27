import logging
import queue
import signal
import sys
import threading

from src.camera_worker import CameraWorker
from src.config_loader import load_config
from src.uploader import Uploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    config_path = "config.yaml"
    logger.info("Loading config from %s", config_path)
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    upload_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    def shutdown(_signum, _frame):
        logger.info("Shutdown signal received, stopping workers...")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    uploader = Uploader(config, upload_queue, stop_event)
    uploader.start()

    workers = []
    for camera in config["cameras"]:
        worker = CameraWorker(camera, config, upload_queue, stop_event)
        worker.start()
        workers.append(worker)
        logger.info("Started worker for camera: %s", camera["label"])

    # Block main thread until stop is signalled
    stop_event.wait()

    logger.info("Waiting for camera workers to finish...")
    for worker in workers:
        worker.join(timeout=30)

    logger.info("Waiting for upload queue to drain...")
    upload_queue.join()

    uploader.join(timeout=60)
    logger.info("All done. Exiting.")


if __name__ == "__main__":
    main()
