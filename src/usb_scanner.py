import glob
import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)


def scan_usb_devices() -> list[dict]:
    """Enumerate available V4L2 video devices.

    Returns a list of {device, name, formats} dicts, one per usable ``/dev/videoN``.
    Uses ``v4l2-ctl --list-devices`` when available; falls back to sysfs names.
    """
    devices = sorted(glob.glob("/dev/video*"), key=_natural_key)
    if not devices:
        return []

    name_by_dev = _names_from_v4l2_ctl()

    result: list[dict] = []
    for dev in devices:
        name = name_by_dev.get(dev) or _name_from_sysfs(dev) or os.path.basename(dev)
        formats = _formats_from_v4l2_ctl(dev)
        # Skip metadata / non-capture devices: they report no pixel formats.
        if not formats:
            continue
        result.append({
            "device": dev,
            "name": name,
            "formats": formats,
        })
    return result


# ---------------------------------------------------------------------------

def _natural_key(dev: str) -> tuple:
    m = re.search(r"(\d+)$", dev)
    return (int(m.group(1)) if m else 0, dev)


def _names_from_v4l2_ctl() -> dict[str, str]:
    """Parse ``v4l2-ctl --list-devices`` output into a {device: friendly-name} dict."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}

    names: dict[str, str] = {}
    current_name: str | None = None
    for line in out.splitlines():
        if not line.strip():
            current_name = None
            continue
        if not line.startswith("\t") and not line.startswith(" "):
            current_name = line.rstrip(":").strip()
            continue
        dev = line.strip()
        if current_name and dev.startswith("/dev/video"):
            names[dev] = current_name
    return names


def _name_from_sysfs(dev: str) -> str | None:
    """Fallback friendly name from /sys/class/video4linux/videoN/name."""
    base = os.path.basename(dev)
    sysfs = f"/sys/class/video4linux/{base}/name"
    try:
        with open(sysfs) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _formats_from_v4l2_ctl(dev: str) -> list[dict]:
    """Return a list of {pixel_format, sizes: [WxH]} supported by the device."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", dev, "--list-formats-ext"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    formats: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        m = re.search(r"'(\w{4})'\s*\(([^)]+)\)", line)
        if m:
            if current:
                formats.append(current)
            current = {"pixel_format": m.group(1), "description": m.group(2), "sizes": []}
            continue
        size = re.search(r"Size:\s*\S+\s+(\d+x\d+)", line)
        if size and current is not None:
            if size.group(1) not in current["sizes"]:
                current["sizes"].append(size.group(1))
    if current:
        formats.append(current)
    return formats
