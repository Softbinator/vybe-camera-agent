#!/usr/bin/env bash
# Push the local webcam (/dev/videoN) to the MediaMTX RTSP server at
# rtsp://localhost:8554/webcam. Run MediaMTX in another terminal first.
set -euo pipefail

MTX_URL="${MTX_URL:-rtsp://localhost:8554/webcam}"

echo "==> Available V4L2 devices:"
if command -v v4l2-ctl >/dev/null 2>&1; then
  v4l2-ctl --list-devices
else
  ls /dev/video* 2>/dev/null || echo "  (no /dev/video* found — plug in a webcam or install v4l-utils)"
fi

read -r -p "Device to stream [/dev/video0]: " DEV
DEV="${DEV:-/dev/video0}"
read -r -p "Resolution [1280x720]: " RES
RES="${RES:-1280x720}"
read -r -p "Framerate [30]: " FPS
FPS="${FPS:-30}"

echo "==> Streaming ${DEV} (${RES}@${FPS}fps) → ${MTX_URL}"
echo "    Ctrl-C to stop."

# -re paces the capture to wall clock so the RTSP stream doesn't outrun
# the receiver. libx264 + zerolatency keeps glass-to-glass latency low.
exec ffmpeg \
  -hide_banner -loglevel warning \
  -f v4l2 -framerate "${FPS}" -video_size "${RES}" -i "${DEV}" \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -pix_fmt yuv420p -g "$((FPS * 2))" \
  -f rtsp -rtsp_transport tcp "${MTX_URL}"
