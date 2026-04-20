# Push the local webcam (DirectShow) to the MediaMTX RTSP server at
# rtsp://localhost:8554/webcam. Run MediaMTX in another PowerShell window first.
#
# Usage:
#   .\push-webcam-windows.ps1
#   .\push-webcam-windows.ps1 -DeviceName "HD Pro Webcam C920"
param(
    [string]$DeviceName = "",
    [string]$Resolution = "1280x720",
    [int]$Framerate    = 30,
    [string]$MtxUrl    = "rtsp://localhost:8554/webcam"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Error "ffmpeg not found on PATH. Install it first: winget install Gyan.FFmpeg"
    exit 1
}

Write-Host "==> Available DirectShow video devices:"
# ffmpeg prints the device list to stderr even on success.
$listing = & ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1
$listing | Select-String -Pattern 'dshow.*video|dshow.*"' | ForEach-Object { Write-Host "    $_" }

if (-not $DeviceName) {
    $DeviceName = Read-Host "Video device name (exactly as shown above, e.g. 'HD Pro Webcam C920')"
}

Write-Host "==> Streaming '$DeviceName' ($Resolution @ $Framerate fps) -> $MtxUrl"
Write-Host "    Ctrl-C to stop."

$gop = $Framerate * 2
& ffmpeg `
    -hide_banner -loglevel warning `
    -f dshow -framerate $Framerate -video_size $Resolution `
    -i "video=$DeviceName" `
    -c:v libx264 -preset veryfast -tune zerolatency `
    -pix_fmt yuv420p -g $gop `
    -f rtsp -rtsp_transport tcp $MtxUrl
