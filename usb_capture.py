#!/usr/bin/env python3
"""Standalone USB / V4L2 camera capture script.

Captures from a V4L2 device (USB webcam, action cam, GoPro in webcam mode, etc.)
and segments the output into fixed-duration .mp4 chunks saved to a local folder.

This script has no dependencies beyond Python 3.10+ and ffmpeg. It does not
require the main agent to be running and does not upload anything — it only
saves files locally. Run it alongside `python main.py` if you want the RTSP
agent and a USB camera running at the same time.

Usage
-----
  python usb_capture.py [options]

  # Quick start — capture from /dev/video0, save to ./output/action-cam/
  python usb_capture.py

  # Specify device, label, chunk duration
  python usb_capture.py --device /dev/video0 --label action-cam --chunk 30

  # Set resolution and framerate
  python usb_capture.py --device /dev/video0 --video-size 1920x1080 --framerate 30

  # Scale output height (re-encodes to 720p)
  python usb_capture.py --device /dev/video0 --height 720

  # List available V4L2 devices
  python usb_capture.py --list-devices

Output
------
Chunks are saved to:  <output-dir>/<label>/<YYYYMMDD_HHMMSS>.mp4

This mirrors the folder structure produced by the main agent so you can use
the web UI's "Inject Chunk Manually" feature to upload the saved files later.
"""

import argparse
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("usb_capture")

RECONNECT_BASE = 5
RECONNECT_MAX = 120


def list_devices() -> None:
    devices = sorted(p for p in (f"/dev/video{i}" for i in range(16)) if os.path.exists(p))
    if not devices:
        print("No V4L2 devices found under /dev/video*")
        return
    print("Available V4L2 devices:")
    for dev in devices:
        # Try to read the device name from sysfs
        idx = dev.replace("/dev/video", "")
        name_path = f"/sys/class/video4linux/video{idx}/name"
        try:
            name = open(name_path).read().strip()
        except OSError:
            name = "(unknown)"
        print(f"  {dev}  —  {name}")


def build_ffmpeg_cmd(
    device: str,
    output_dir: str,
    chunk_duration: int,
    video_size: str | None,
    framerate: int | None,
    height: int | None,
    input_format: str | None,
) -> tuple[list[str], str]:
    os.makedirs(output_dir, exist_ok=True)
    segment_list = os.path.join(output_dir, "segments.txt")
    output_pattern = os.path.join(output_dir, "%Y%m%d_%H%M%S.mp4")

    input_opts = ["-f", "v4l2"]
    if input_format:
        input_opts += ["-input_format", input_format]
    if framerate:
        input_opts += ["-framerate", str(framerate)]
    if video_size:
        input_opts += ["-video_size", video_size]
    input_opts += ["-i", device]

    if input_format == "h264":
        # Camera streams native H.264 — stream-copy directly, zero re-encoding
        video_opts = ["-c:v", "copy", "-an"]
    elif height:
        video_opts = [
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-an",
        ]
    else:
        # V4L2 raw/MJPEG → H.264; -an skips audio (USB cams rarely have usable audio)
        video_opts = ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an"]

    cmd = [
        "ffmpeg", "-y",
        *input_opts,
        *video_opts,
        "-f", "segment",
        "-segment_time", str(chunk_duration),
        "-segment_format", "mp4",
        "-segment_list", segment_list,
        "-segment_list_flags", "+live",
        "-reset_timestamps", "1",
        "-strftime", "1",
        output_pattern,
    ]
    return cmd, segment_list


def watch_segments(segment_list: str, proc: subprocess.Popen, label: str, stop: threading.Event) -> None:
    processed: set[str] = set()
    while proc.poll() is None and not stop.is_set():
        try:
            with open(segment_list) as f:
                lines = [l.strip() for l in f if l.strip()]
            for path in lines[:-1]:
                if path not in processed:
                    processed.add(path)
                    logger.info("[%s] chunk saved: %s", label, os.path.basename(path))
        except OSError:
            pass
        time.sleep(1)

    # Flush any remaining entries after ffmpeg exits
    try:
        with open(segment_list) as f:
            lines = [l.strip() for l in f if l.strip()]
        for path in lines:
            if path not in processed:
                logger.info("[%s] chunk saved: %s", label, os.path.basename(path))
    except OSError:
        pass


def run(
    device: str,
    label: str,
    output_dir: str,
    chunk_duration: int,
    video_size: str | None,
    framerate: int | None,
    height: int | None,
    input_format: str | None,
) -> None:
    stop = threading.Event()

    def shutdown(sig, frame):
        logger.info("Stopping…")
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    camera_dir = os.path.join(output_dir, label)
    logger.info("USB capture started")
    logger.info("  device   : %s", device)
    logger.info("  label    : %s", label)
    logger.info("  output   : %s", camera_dir)
    logger.info("  chunk    : %ds", chunk_duration)
    if video_size:
        logger.info("  size     : %s", video_size)
    if framerate:
        logger.info("  framerate: %d fps", framerate)
    if input_format:
        logger.info("  input fmt: %s%s", input_format,
                    " (stream copy, no re-encoding)" if input_format == "h264" else "")
    if height:
        logger.info("  height   : %dpx (re-encoded)", height)

    attempt = 0
    while not stop.is_set():
        # Clear segment list before each (re)start
        seg_list_path = os.path.join(camera_dir, "segments.txt")
        os.makedirs(camera_dir, exist_ok=True)
        open(seg_list_path, "w").close()

        cmd, segment_list = build_ffmpeg_cmd(device, camera_dir, chunk_duration, video_size, framerate, height, input_format)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        logger.info("[%s] ffmpeg started (pid %d, attempt %d)", label, proc.pid, attempt)

        watcher = threading.Thread(
            target=watch_segments, args=(segment_list, proc, label, stop), daemon=True
        )
        watcher.start()

        while proc.poll() is None and not stop.is_set():
            time.sleep(0.5)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        watcher.join(timeout=5)

        if stop.is_set():
            break

        try:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            lines = [l.strip() for l in stderr_bytes.decode(errors="replace").splitlines() if l.strip()]
            error = lines[-1] if lines else f"exit code {proc.returncode}"
        except Exception:
            error = f"exit code {proc.returncode}"

        attempt += 1
        delay = min(RECONNECT_BASE * (2 ** (attempt - 1)), RECONNECT_MAX)
        delay *= 0.8 + random.random() * 0.4
        logger.warning("[%s] ffmpeg exited: %s — reconnecting in %.1fs (attempt %d)", label, error, delay, attempt)

        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not stop.is_set():
            time.sleep(0.5)

    logger.info("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture from a USB/V4L2 camera to local .mp4 chunks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--device", default="/dev/video0",
                        help="V4L2 device path (default: /dev/video0)")
    parser.add_argument("--label", default="action-cam",
                        help="Camera label — used as the output subfolder name (default: action-cam)")
    parser.add_argument("--output", default="./output",
                        help="Base output directory (default: ./output)")
    parser.add_argument("--chunk", type=int, default=30,
                        help="Chunk duration in seconds (default: 30)")
    parser.add_argument("--video-size", default=None,
                        help="Capture resolution, e.g. 1920x1080 or 1280x720")
    parser.add_argument("--framerate", type=int, default=None,
                        help="Capture framerate, e.g. 30")
    parser.add_argument("--height", type=int, default=None,
                        help="Scale output to this height in pixels (re-encodes, e.g. 720)")
    parser.add_argument("--input-format", default=None,
                        help="V4L2 pixel format: h264 (stream copy, best for Osmo/GoPro), "
                             "mjpeg, yuyv422, etc. Omit to let ffmpeg auto-detect.")
    parser.add_argument("--list-devices", action="store_true",
                        help="List available V4L2 devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    if not os.path.exists(args.device):
        logger.error("Device not found: %s  (run with --list-devices to see available devices)", args.device)
        sys.exit(1)

    run(
        device=args.device,
        label=args.label,
        output_dir=args.output,
        chunk_duration=args.chunk,
        video_size=args.video_size,
        framerate=args.framerate,
        height=args.height,
        input_format=args.input_format,
    )


if __name__ == "__main__":
    main()
