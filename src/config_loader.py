import os
import re
import yaml
from dotenv import load_dotenv


_ENV_VAR_RE = re.compile(r'^\$\{(\w+)\}$')


def _expand_env(value):
    """Expand a single ${VAR} placeholder or return value as-is."""
    if not isinstance(value, str):
        return value
    m = _ENV_VAR_RE.match(value.strip())
    if m:
        return os.environ.get(m.group(1), "")
    return value


def _expand_config(obj):
    """Recursively expand all ${VAR} placeholders in a config dict/list."""
    if isinstance(obj, dict):
        return {k: _expand_config(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_config(item) for item in obj]
    return _expand_env(obj)


def load_config(config_path: str = "config.yaml") -> dict:
    """Load .env then config.yaml, expand env vars, validate, and return config dict."""
    load_dotenv()

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("config.yaml must be a YAML mapping at the top level")

    config = _expand_config(raw)
    _validate(config)
    return config


def save_config(config_path: str, raw_yaml: str) -> None:
    """Write raw YAML text to config_path.

    Uses direct write (no atomic rename) so it works with Docker bind-mounted files,
    where os.replace() would fail with EBUSY due to cross-device filesystem boundaries.
    """
    with open(config_path, "w") as f:
        f.write(raw_yaml)


def _validate(config: dict) -> None:
    required_top = [
        "chunk_duration_seconds",
        "temp_dir",
        "venue_id",
        "api_base_url",
        "keycloak_url",
        "keycloak_realm",
        "keycloak_client_id",
        "keycloak_client_secret",
    ]
    for key in required_top:
        if key not in config or config[key] == "":
            raise ValueError(f"Missing required config key: '{key}'")

    if not isinstance(config["chunk_duration_seconds"], (int, float)) or config["chunk_duration_seconds"] <= 0:
        raise ValueError("'chunk_duration_seconds' must be a positive number")

    config.setdefault("cameras", [])
    config.setdefault("storage_mode", "upload")
    config.setdefault("output_dir", "/output")
    config.setdefault("lan_discovery", {})
    config.setdefault("recording_paused", False)

    cameras = config["cameras"]
    if not isinstance(cameras, list):
        raise ValueError("'cameras' must be a list")

    storage_mode = config["storage_mode"]
    if storage_mode not in ("upload", "local", "both"):
        raise ValueError(f"'storage_mode' must be 'upload', 'local', or 'both', got '{storage_mode}'")

    lan = config["lan_discovery"]
    if not isinstance(lan, dict):
        raise ValueError("'lan_discovery' must be a mapping")
    lan.setdefault("enabled", False)
    lan.setdefault("leases_file", "/var/lib/dnsmasq/dnsmasq.leases")
    lan.setdefault("rtsp_port", 554)
    lan.setdefault("probe_paths", [
        "/stream1",
        "/Streaming/Channels/101",
        "/h264Preview_01_main",
        "/live",
        "/",
    ])

    seen_labels = set()
    for i, cam in enumerate(cameras):
        if not isinstance(cam, dict):
            raise ValueError(f"Camera entry {i} must be a mapping")
        if "label" not in cam or cam["label"] == "":
            raise ValueError(f"Camera entry {i} is missing required field 'label'")

        label = cam["label"]
        if label in seen_labels:
            raise ValueError(f"Duplicate camera label: '{label}'")
        seen_labels.add(label)

        source = cam.get("source", "rtsp")
        if source not in ("rtsp", "file", "v4l2"):
            raise ValueError(f"Camera '{label}': source must be 'rtsp', 'v4l2', or 'file', got '{source}'")

        cam.setdefault("auto_discovered", False)
        cam.setdefault("pending_credentials", False)

        if source == "rtsp":
            if "rtsp_url" not in cam or cam["rtsp_url"] == "":
                raise ValueError(f"Camera '{label}' (source=rtsp) is missing required field 'rtsp_url'")
        elif source == "v4l2":
            if "device" not in cam or cam["device"] == "":
                raise ValueError(f"Camera '{label}' (source=v4l2) is missing required field 'device'")
        elif source == "file":
            if "replay_dir" not in cam or cam["replay_dir"] == "":
                raise ValueError(f"Camera '{label}' (source=file) is missing required field 'replay_dir'")


def resolve_rtsp_url(cam: dict) -> str:
    """Return the final RTSP URL for a camera, substituting {USER}/{PASS} placeholders
    from cam['rtsp_username']/cam['rtsp_password'] when present."""
    url = cam.get("rtsp_url", "")
    user = cam.get("rtsp_username")
    password = cam.get("rtsp_password")
    if user is not None:
        url = url.replace("{USER}", str(user))
    if password is not None:
        url = url.replace("{PASS}", str(password))
    return url
