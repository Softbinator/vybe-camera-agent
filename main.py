import logging
import queue
import signal
import sys
import threading

from src.agent_state import AgentState
from src.config_loader import load_config
from src.uploader import Uploader
from src.web_server import WebServer

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
        logger.info("Shutdown signal received, stopping workers…")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # AgentState is created first so the Uploader can read live config from it
    state = AgentState(upload_queue, stop_event)
    state.start_all(config)

    uploader = Uploader(state, upload_queue, stop_event)
    uploader.start()

    web_port = int(config.get("web_port", 5174))
    server = WebServer(state, config_path=config_path, port=web_port)
    server.start()
    logger.info("Web dashboard available at http://localhost:%d", web_port)

    stop_event.wait()

    logger.info("Stopping all camera workers…")
    state.stop_all()

    logger.info("Waiting for upload queue to drain…")
    upload_queue.join()

    uploader.join(timeout=60)
    logger.info("All done. Exiting.")


if __name__ == "__main__":
    main()
