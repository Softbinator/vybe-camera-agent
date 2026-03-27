# vybe-camera-agent

Captures RTSP camera streams, splits them into fixed-duration chunks, and uploads them to S3 (or any S3-compatible provider) using [rclone](https://rclone.org/).

- Multiple cameras run concurrently, each in its own thread
- Uses **ffmpeg** for RTSP capture and segmentation
- Uses **rclone** for uploads — swap storage providers without changing code
- Failed uploads are retried with exponential backoff; chunks are kept locally until confirmed uploaded
- Configured via a YAML file (cameras) and a `.env` file (credentials)

---

## Prerequisites

The following tools must be installed and available on your `PATH`:

- **Python 3.12+** — [python.org](https://www.python.org/downloads/)
- **ffmpeg** — [ffmpeg.org](https://ffmpeg.org/download.html)
- **rclone** — [rclone.org/install](https://rclone.org/install/)

Verify they are available:

```bash
python --version
ffmpeg -version
rclone --version
```

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd vybe-camera-agent
```

### 2. Create and activate a virtual environment

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (Command Prompt):**
```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in your storage credentials. The variables follow rclone's environment variable convention — the remote name prefix must match `rclone_remote` in `config.yaml` (uppercased):

```env
RCLONE_CONFIG_S3VYBE_TYPE=s3
RCLONE_CONFIG_S3VYBE_PROVIDER=AWS
RCLONE_CONFIG_S3VYBE_ACCESS_KEY_ID=YOUR_KEY
RCLONE_CONFIG_S3VYBE_SECRET_ACCESS_KEY=YOUR_SECRET
RCLONE_CONFIG_S3VYBE_REGION=eu-central-1
```

### 5. Configure cameras

Edit `config.yaml`:

```yaml
chunk_duration_seconds: 60        # Length of each recorded segment
temp_dir: /tmp/vybe-camera-agent  # Local staging directory for chunks
rclone_remote: s3vybe             # Must match the prefix in your .env (lowercased)
s3_bucket_path: my-bucket/recordings

cameras:
  - label: entrance
    rtsp_url: rtsp://admin:password@192.168.1.10:554/stream1
  - label: parking
    rtsp_url: rtsp://admin:password@192.168.1.11:554/stream1
```

Uploaded files will be stored at:
```
<rclone_remote>:<s3_bucket_path>/<camera_label>/<timestamp>.mp4
```

---

## Running

```bash
python main.py
```

Stop with `Ctrl+C` — the agent will finish the current upload queue before exiting.

---

## Project structure

```
vybe-camera-agent/
├── main.py                    # Entry point
├── config.yaml                # Camera list and global settings
├── .env.example               # Credential template (copy to .env)
├── requirements.txt
├── src/
│   ├── config_loader.py       # Loads and validates config.yaml + .env
│   ├── camera_worker.py       # Per-camera ffmpeg thread with auto-reconnect
│   └── uploader.py            # rclone upload with exponential backoff retry
├── Dockerfile
├── docker-compose.yml
└── deploy/
    ├── systemd/               # Linux auto-start (systemd service)
    ├── windows/               # Windows auto-start (Task Scheduler XML)
    └── macos/                 # macOS auto-start (launchd plist)
```

---

## Docker deployment

Build and start:

```bash
docker compose up --build
```

Run in the background:

```bash
docker compose up --build -d
```

The container uses `restart: unless-stopped`, so it will automatically restart after a system reboot as long as the Docker daemon is running.

---

## Auto-start on boot

### Linux (systemd)

```bash
sudo cp deploy/systemd/vybe-camera-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vybe-camera-agent
sudo systemctl start vybe-camera-agent

# View logs
sudo journalctl -u vybe-camera-agent -f
```

### Windows (Task Scheduler)

Run as Administrator:

```bat
schtasks /create /xml deploy\windows\task-scheduler.xml /tn "Vybe Camera Agent"
```

Or open Task Scheduler → Action → Import Task → select the XML file.

Update the `WorkingDirectory` inside the XML to match your installation path before importing.

### macOS (launchd)

```bash
cp deploy/macos/com.vybe.camera-agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.vybe.camera-agent.plist

# View logs
tail -f /tmp/vybe-camera-agent.stdout.log
tail -f /tmp/vybe-camera-agent.stderr.log
```
