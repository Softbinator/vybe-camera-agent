# Dummy RTSP camera — test harness

Turns any laptop (Windows / Linux / macOS) into an RTSP "camera" that
streams its built-in webcam to the camera-agent. Useful to exercise the
RTSP path without real IP cameras.

The laptop plugs into the camera-agent's LAN NIC (the 10.20.0.0/24 side).
It gets a DHCP lease from the agent's dnsmasq, serves RTSP on port 8554,
and — when `lan_discovery.rtsp_port` is set to 8554 — the agent picks it
up automatically.

## 1. On the laptop — download MediaMTX

Grab the right tarball for your OS from <https://github.com/bluenviron/mediamtx/releases>.

- Windows: `mediamtx_vX.Y.Z_windows_amd64.zip` → unzip → you get `mediamtx.exe`.
- Linux x86_64: `mediamtx_vX.Y.Z_linux_amd64.tar.gz` → extract → `mediamtx` binary.
- macOS: `mediamtx_vX.Y.Z_darwin_amd64.tar.gz` (Intel) or `_darwin_arm64.tar.gz` (Apple Silicon).

No install step — it's a single binary.

Put `mediamtx.yml` (the one next to this README) in the same folder as
the binary, then run it:

- Windows: double-click `mediamtx.exe` or run it from PowerShell.
- Linux / macOS: `./mediamtx`

You should see log lines ending in `listener opened on :8554 (RTSP)`.
Windows may prompt you to allow network access — say **yes** for both
private and public networks (the camera-agent is on a private LAN but
Windows sometimes classifies a direct-ethernet link as public).

## 2. Push the webcam feed into MediaMTX

In another terminal / PowerShell window, pick the platform script and run it.
Each script uses ffmpeg (install it first if you don't have it:
`winget install ffmpeg` on Windows, `brew install ffmpeg` on macOS,
`sudo apt install ffmpeg` on Debian/Ubuntu).

- `push-webcam-windows.ps1` — PowerShell, uses `dshow`.
- `push-webcam-linux.sh`   — bash, uses `/dev/videoN`.
- `push-webcam-macos.sh`   — bash, uses `avfoundation`.

Each script first lists the available video devices so you can pick one,
then starts streaming.

Verify on the laptop itself:

```
ffplay rtsp://localhost:8554/webcam
```

You should see your webcam in a window.

## 3. Connect the laptop to the camera-agent

1. Plug an Ethernet cable from the laptop to the camera-agent's LAN NIC.
2. Set the laptop's Ethernet to **DHCP** (default everywhere).
3. Wait ~10 s for the dnsmasq lease. Check the laptop IP:
   - Windows: `ipconfig | findstr IPv4`
   - Linux:   `ip -br addr show`
   - macOS:   `ipconfig getifaddr en0`  (adjust interface)
   - Expect something like `10.20.0.101`.

## 4. Tell the agent about it

Either let auto-discovery do it (preferred) or add it manually.

### Option A — auto-discovery

MediaMTX listens on 8554, but the agent probes 554 by default. Bump the
probe port in `config.yaml`:

```yaml
lan_discovery:
  enabled: true
  rtsp_port: 8554
```

Reload config from the dashboard (Save & Reload on the YAML editor).
Within ~10 s the laptop appears in the "Discovered Cameras" section
as `auto-10-20-0-101`. The `mediamtx.yml` in this folder ships with
credentials `vybe` / `vybe-cam-test` — enter those in the form and hit
**Save & Start**.

### Option B — manual entry

If you don't want to touch `lan_discovery`, open the dashboard's
**Full Configuration** editor and append a camera:

```yaml
cameras:
  - label: laptop-webcam
    source: rtsp
    rtsp_url: rtsp://{USER}:{PASS}@10.20.0.101:8554/webcam
    rtsp_username: vybe
    rtsp_password: vybe-cam-test
```

Save & Reload. The worker starts, **Preview** should show the feed,
and chunks upload as normal.

## 5. Full end-to-end checklist

- `docker compose -f docker-compose.site.yml logs -f agent` on the agent
  shows `ffmpeg started` for `laptop-webcam`.
- Backend `/api/upload/chunk` receives 30 s mp4s.
- Analytics agent processes them through Bedrock.
- The dashboard's **Preview** button shows a live-ish snapshot of your
  laptop webcam.

## Troubleshooting

- **No DHCP lease on the laptop:** verify dnsmasq is running on the agent
  (`docker compose -f docker-compose.site.yml logs dnsmasq`) and that the
  Ethernet cable is plugged into the LAN NIC (the one with static IP
  `10.20.0.1/24`), not the WAN NIC.
- **ffmpeg says `device not found`:** on Windows, `dshow` names are
  exactly as shown in `ffmpeg -list_devices true -f dshow -i dummy`
  (usually something like `HD WebCam` or `Integrated Camera`). The
  script already runs that discovery for you.
- **Agent says `401 Unauthorized`** on the RTSP probe: you typed the
  wrong username/password. The defaults in `mediamtx.yml` here are
  `vybe` / `vybe-cam-test`. Edit both places to change them.
- **Windows Firewall blocks RTSP:** Windows asks the first time
  `mediamtx.exe` runs — allow **private** networks. If you missed it,
  go to Windows Defender Firewall → Allow an app → Add mediamtx.exe.
