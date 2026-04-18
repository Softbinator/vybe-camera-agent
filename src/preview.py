"""Single-frame JPEG snapshots for the dashboard's camera preview button.

We intentionally avoid a full live MJPEG/HLS stream here:
- For V4L2 cameras, most drivers allow only a single reader on /dev/videoN, so
  a persistent preview would conflict with the recording worker.
- For RTSP, multiple TCP readers generally work, but they consume bandwidth on
  the site uplink. Snapshots let the UI poll on demand only while the preview
  modal is open.

The browser can refresh an <img src="/api/cameras/LABEL/preview.jpg?ts=..."> on a
timer to approximate video. ~1-2 fps is plenty to verify that the camera feed
is alive and the framing is correct.
"""

import logging
import os
import subprocess

from src.config_loader import resolve_rtsp_url

logger = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = 10  # seconds — fail fast if ffmpeg hangs


def capture_snapshot(camera: dict) -> bytes:
    """Return a JPEG byte string for a single frame of *camera*.

    Raises RuntimeError if ffmpeg fails or times out. Caller should surface
    the message so the UI can show 'preview failed' without crashing.
    """
    source = camera.get("source", "rtsp")
    input_opts = _build_input_opts(camera, source)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        *input_opts,
        "-frames:v", "1",
        "-q:v", "5",                # JPEG quality 1-31 (lower = better). 5 is a reasonable balance.
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-",                        # stdout
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SNAPSHOT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg snapshot timed out after {SNAPSHOT_TIMEOUT}s") from exc

    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"").decode(errors="replace").strip().splitlines()[-1:]
        msg = tail[0] if tail else f"ffmpeg exited {proc.returncode}"
        raise RuntimeError(msg)
    return proc.stdout


def _build_input_opts(camera: dict, source: str) -> list[str]:
    if source == "v4l2":
        device = camera.get("device", "/dev/video0")
        opts = ["-f", "v4l2"]
        if camera.get("input_format"):
            opts += ["-input_format", str(camera["input_format"])]
        if camera.get("video_size"):
            opts += ["-video_size", str(camera["video_size"])]
        opts += ["-i", device]
        return opts
    if source == "file":
        replay_dir = camera.get("replay_dir", "")
        candidate = _first_file_in(replay_dir)
        if not candidate:
            raise RuntimeError(f"no files in replay_dir: {replay_dir}")
        return ["-i", candidate]
    # rtsp
    url = resolve_rtsp_url(camera)
    return ["-rtsp_transport", "tcp", "-i", url]


def _first_file_in(directory: str) -> str | None:
    if not directory or not os.path.isdir(directory):
        return None
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in names:
        full = os.path.join(directory, name)
        if os.path.isfile(full):
            return full
    return None
