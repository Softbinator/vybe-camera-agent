import os
import yaml
from dotenv import load_dotenv


def load_config(config_path: str = "config.yaml") -> dict:
    """Load .env then config.yaml, validate required fields, and return the config dict."""
    load_dotenv()

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError("config.yaml must be a YAML mapping at the top level")

    _validate(config)
    return config


def _validate(config: dict) -> None:
    required_top = ["chunk_duration_seconds", "temp_dir", "rclone_remote", "s3_bucket_path", "cameras"]
    for key in required_top:
        if key not in config:
            raise ValueError(f"Missing required config key: '{key}'")

    if not isinstance(config["chunk_duration_seconds"], (int, float)) or config["chunk_duration_seconds"] <= 0:
        raise ValueError("'chunk_duration_seconds' must be a positive number")

    cameras = config["cameras"]
    if not isinstance(cameras, list) or len(cameras) == 0:
        raise ValueError("'cameras' must be a non-empty list")

    seen_labels = set()
    for i, cam in enumerate(cameras):
        if not isinstance(cam, dict):
            raise ValueError(f"Camera entry {i} must be a mapping")
        for field in ("label", "rtsp_url"):
            if field not in cam:
                raise ValueError(f"Camera entry {i} is missing required field '{field}'")
        label = cam["label"]
        if label in seen_labels:
            raise ValueError(f"Duplicate camera label: '{label}'")
        seen_labels.add(label)
