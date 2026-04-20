#!/usr/bin/env bash
# Push the local webcam (AVFoundation) to the MediaMTX RTSP server at
# rtsp://localhost:8554/webcam.
set -euo pipefail

MTX_URL="${MTX_URL:-rtsp://localhost:8554/webcam}"

echo "==> Available AVFoundation video devices:"
ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | grep -E "AVFoundation video devices|\[AVFoundation" || true

read -r -p "Video device index [0]: " IDX
IDX="${IDX:-0}"
read -r -p "Resolution [1280x720]: " RES
RES="${RES:-1280x720}"
read -r -p "Framerate [30]: " FPS
FPS="${FPS:-30}"

echo "==> Streaming device ${IDX} (${RES}@${FPS}fps) → ${MTX_URL}"
echo "    Ctrl-C to stop."

exec ffmpeg \
  -hide_banner -loglevel warning \
  -f avfoundation -framerate "${FPS}" -video_size "${RES}" -i "${IDX}:none" \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -pix_fmt yuv420p -g "$((FPS * 2))" \
  -f rtsp -rtsp_transport tcp "${MTX_URL}"
