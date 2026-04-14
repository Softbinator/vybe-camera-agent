"""FastAPI web dashboard for the vybe-camera-agent.

Serves a self-contained HTML dashboard with live status, CodeMirror YAML editor,
connection settings form, per-camera restart, and manual chunk injection.
"""

import logging
import os
import threading
from typing import TYPE_CHECKING

import uvicorn
import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

if TYPE_CHECKING:
    from src.agent_state import AgentState

logger = logging.getLogger(__name__)

# Connection fields that appear in the dedicated settings form (subset of config.yaml)
_CONNECTION_KEYS = [
    "api_base_url",
    "keycloak_url",
    "keycloak_realm",
    "keycloak_client_id",
    "keycloak_client_secret",
    "venue_id",
]

_DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>vybe camera agent</title>

  <!-- CodeMirror 5 — lightweight YAML editor (~150 KB total) -->
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/yaml/yaml.min.js"></script>

  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0f1117; --surface: #1a1d27; --border: #2d3148;
      --text: #e2e8f0; --muted: #718096; --accent: #6366f1;
      --green: #22c55e; --yellow: #eab308; --red: #ef4444; --blue: #3b82f6;
      --radius: 10px; --font: 'Inter', system-ui, sans-serif;
    }
    body { background: var(--bg); color: var(--text); font-family: var(--font);
           min-height: 100vh; padding: 24px; }
    h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 4px; }
    .subtitle { color: var(--muted); font-size: .85rem; margin-bottom: 24px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 16px; margin-bottom: 24px; }
    .card { background: var(--surface); border: 1px solid var(--border);
            border-radius: var(--radius); padding: 18px; }
    .card-title { font-size: .95rem; font-weight: 600; margin-bottom: 12px;
                  display: flex; align-items: center; gap: 8px; }
    .badge { display: inline-flex; align-items: center; gap: 4px;
             padding: 2px 8px; border-radius: 99px; font-size: .73rem;
             font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
    .badge-green  { background: #14532d44; color: var(--green); }
    .badge-yellow { background: #78350f44; color: var(--yellow); }
    .badge-red    { background: #7f1d1d44; color: var(--red); }
    .badge-blue   { background: #1e3a5f44; color: var(--blue); }
    .badge-gray   { background: #1f293744; color: var(--muted); }
    .meta { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px;
            font-size: .82rem; color: var(--muted); margin-bottom: 14px; }
    .meta strong { color: var(--text); }
    .error-box { background: #7f1d1d22; border: 1px solid #7f1d1d; border-radius: 6px;
                 padding: 8px 10px; font-size: .78rem; color: #fca5a5;
                 margin-bottom: 10px; word-break: break-all; }
    button { cursor: pointer; border: none; border-radius: 6px; font-size: .83rem;
             font-weight: 500; padding: 7px 14px; transition: opacity .15s; }
    button:hover { opacity: .8; }
    .btn-primary  { background: var(--accent); color: #fff; }
    .btn-danger   { background: var(--red); color: #fff; }
    .btn-neutral  { background: var(--border); color: var(--text); }
    .queue-bar { background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 14px 18px;
                 display: flex; align-items: center; justify-content: space-between;
                 margin-bottom: 24px; }
    .queue-bar .label { font-size: .85rem; color: var(--muted); }
    .queue-bar .value { font-size: 1.5rem; font-weight: 700; }
    .section-title { display: block; font-size: .8rem; font-weight: 600;
                     text-transform: uppercase; letter-spacing: .07em;
                     color: var(--muted); margin-bottom: 10px; }
    /* CodeMirror overrides */
    .CodeMirror { height: 320px; border-radius: 6px; font-size: .82rem;
                  border: 1px solid var(--border); }
    .CodeMirror-focused { border-color: var(--accent); }
    /* Connection form */
    .conn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    @media (max-width: 680px) { .conn-grid { grid-template-columns: 1fr; } }
    .field { display: flex; flex-direction: column; gap: 4px; }
    .field label { font-size: .78rem; color: var(--muted); }
    .field input { background: #0c0e18; border: 1px solid var(--border);
                   border-radius: 6px; color: var(--text); font-size: .83rem;
                   padding: 7px 10px; outline: none; width: 100%; }
    .field input:focus { border-color: var(--accent); }
    .field input[type=password] { letter-spacing: .1em; }
    .field.full { grid-column: 1 / -1; }
    .row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
    /* Inject */
    .inject-zone { border: 2px dashed var(--border); border-radius: 8px;
                   padding: 20px; text-align: center; color: var(--muted);
                   font-size: .85rem; cursor: pointer; transition: border-color .2s;
                   margin-top: 12px; }
    .inject-zone:hover, .inject-zone.drag { border-color: var(--accent); color: var(--text); }
    #inject-input { display: none; }
    /* Storage mode toggle */
    .mode-toggle { display: flex; gap: 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    .mode-toggle button { background: transparent; color: var(--muted); border-radius: 0;
                          padding: 7px 16px; font-size: .82rem; border: none;
                          border-right: 1px solid var(--border); transition: all .15s; }
    .mode-toggle button:last-child { border-right: none; }
    .mode-toggle button:hover { background: var(--border); color: var(--text); }
    .mode-toggle button.active { background: var(--accent); color: #fff; }
    .divider { border: none; border-top: 1px solid var(--border); margin: 24px 0; }
    .toast { position: fixed; bottom: 24px; right: 24px; background: #1e293b;
             border: 1px solid var(--border); border-radius: 8px; padding: 12px 18px;
             font-size: .85rem; max-width: 340px; opacity: 0;
             transform: translateY(10px); transition: all .25s; z-index: 999; }
    .toast.show { opacity: 1; transform: translateY(0); }
    @media (max-width: 600px) { body { padding: 12px; } }
  </style>
</head>
<body>
  <h1>vybe camera agent</h1>
  <p class="subtitle" id="last-update">Loading…</p>

  <div class="queue-bar">
    <span class="label">Upload queue depth</span>
    <span class="value" id="queue-depth">—</span>
  </div>

  <!-- Storage mode toggle -->
  <div class="card" style="margin-bottom:24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
    <div>
      <div style="font-size:.85rem;font-weight:600;margin-bottom:2px">Storage Mode</div>
      <div style="font-size:.78rem;color:var(--muted)" id="output-dir-label"></div>
    </div>
    <div class="mode-toggle" id="mode-toggle">
      <button id="mode-upload" onclick="setStorageMode('upload')">Upload only</button>
      <button id="mode-both"   onclick="setStorageMode('both')">Upload + Save</button>
      <button id="mode-local"  onclick="setStorageMode('local')">Save locally</button>
    </div>
  </div>

  <!-- Camera status cards -->
  <span class="section-title">Cameras</span>
  <div class="grid" id="cameras-grid">
    <div class="card"><p style="color:var(--muted)">Loading…</p></div>
  </div>

  <hr class="divider">

  <!-- Connection settings -->
  <span class="section-title">Connection Settings</span>
  <div class="card" style="margin-bottom:24px">
    <div class="conn-grid">
      <div class="field full">
        <label>API Base URL</label>
        <input id="conn-api-base-url" type="url" placeholder="http://api:3000">
      </div>
      <div class="field full">
        <label>Venue ID</label>
        <input id="conn-venue-id" type="text" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx">
      </div>
      <div class="field full">
        <label>Keycloak URL</label>
        <input id="conn-keycloak-url" type="url" placeholder="http://keycloak:8080">
      </div>
      <div class="field">
        <label>Realm</label>
        <input id="conn-keycloak-realm" type="text" placeholder="vybe-realm">
      </div>
      <div class="field">
        <label>Client ID</label>
        <input id="conn-keycloak-client-id" type="text" placeholder="camera-agent-…">
      </div>
      <div class="field full">
        <label>Client Secret</label>
        <input id="conn-keycloak-client-secret" type="password" placeholder="••••••••">
      </div>
    </div>
    <div class="row">
      <button class="btn-primary" onclick="saveConnection()">Save &amp; Reconnect</button>
      <button class="btn-neutral" onclick="loadConnection()">Discard</button>
    </div>
  </div>

  <hr class="divider">

  <!-- YAML config editor -->
  <span class="section-title">Full Configuration (config.yaml)</span>
  <div class="card" style="margin-bottom:24px">
    <textarea id="config-editor"></textarea>
    <div class="row">
      <button class="btn-primary" onclick="saveConfig()">Save &amp; Reload</button>
      <button class="btn-neutral" onclick="loadConfig()">Discard Changes</button>
    </div>
  </div>

  <hr class="divider">

  <!-- Manual chunk injection -->
  <span class="section-title">Inject Chunk Manually</span>
  <div class="card" style="margin-bottom:32px">
    <p style="font-size:.85rem;color:var(--muted);margin-bottom:12px">
      Upload an .mp4 file to inject it directly into the upload queue.
      Works even when no cameras are configured.
    </p>
    <div class="conn-grid" style="grid-template-columns: 1fr 1fr">
      <div class="field">
        <label>Camera label (any name)</label>
        <input id="inject-label" type="text" placeholder="entrance">
      </div>
    </div>
    <div class="inject-zone" id="inject-zone"
         onclick="document.getElementById('inject-input').click()"
         ondragover="event.preventDefault();this.classList.add('drag')"
         ondragleave="this.classList.remove('drag')"
         ondrop="handleDrop(event)">
      Drop an .mp4 file here or click to browse
    </div>
    <input type="file" id="inject-input" accept=".mp4,video/mp4" onchange="handleFileSelect(this.files)">
  </div>

  <div class="toast" id="toast"></div>

<script>
const API = '';

// ---------------------------------------------------------------------------
// CodeMirror setup
// ---------------------------------------------------------------------------
const cmEditor = CodeMirror.fromTextArea(document.getElementById('config-editor'), {
  mode: 'yaml',
  theme: 'dracula',
  lineNumbers: true,
  indentUnit: 2,
  tabSize: 2,
  indentWithTabs: false,
  lineWrapping: true,
  extraKeys: { Tab: cm => cm.replaceSelection('  ') },
});

// ---------------------------------------------------------------------------
// Status polling
// ---------------------------------------------------------------------------
function badgeClass(state) {
  if (!state) return 'badge-gray';
  const s = state.toLowerCase();
  if (s === 'connected') return 'badge-green';
  if (s === 'reconnecting' || s === 'connecting') return 'badge-yellow';
  if (s === 'waiting') return 'badge-blue';
  if (s === 'stopped') return 'badge-gray';
  return 'badge-red';
}
function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString();
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function renderCamera(cam) {
  const badge = `<span class="badge ${badgeClass(cam.state)}">${cam.state || 'unknown'}</span>`;
  const src   = cam.source === 'file'
    ? `<span class="badge badge-blue">file replay</span>`
    : cam.source === 'v4l2'
    ? `<span class="badge badge-blue">usb / v4l2</span>`
    : `<span class="badge badge-gray">rtsp</span>`;
  const urlLine = cam.source === 'file'
    ? `<span>Replay dir</span><strong>${escHtml(cam.replay_dir || '—')}</strong>`
    : cam.source === 'v4l2'
    ? `<span>Device</span><strong>${escHtml(cam.device || '—')}</strong>`
    : `<span>RTSP URL</span><strong style="word-break:break-all">${escHtml(cam.rtsp_url || '—')}</strong>`;
  const errorHtml = cam.last_error
    ? `<div class="error-box">${escHtml(cam.last_error)}</div>` : '';
  return `
    <div class="card">
      <div class="card-title">${escHtml(cam.label)} ${badge} ${src}</div>
      <div class="meta">
        <span>Last chunk</span><strong>${fmtTime(cam.last_chunk_at)}</strong>
        <span>Chunks enqueued</span><strong>${cam.chunks_enqueued ?? 0}</strong>
        <span>Reconnects</span><strong>${cam.reconnect_attempts ?? 0}</strong>
        ${urlLine}
      </div>
      ${errorHtml}
      <button class="btn-danger" onclick="restartCamera('${escHtml(cam.label)}')">Restart</button>
    </div>`;
}

async function fetchStatus() {
  try {
    const r = await fetch(API + '/api/status');
    if (!r.ok) return;
    const data = await r.json();
    document.getElementById('queue-depth').textContent = data.queue_depth ?? '—';

    // Update storage mode toggle
    const mode = data.storage_mode || 'upload';
    ['upload','both','local'].forEach(m => {
      document.getElementById('mode-' + m)?.classList.toggle('active', m === mode);
    });
    const outputDir = data.output_dir || '/output';
    const dirLabel = document.getElementById('output-dir-label');
    if (dirLabel) {
      dirLabel.textContent = (mode === 'upload')
        ? 'Chunks are uploaded then deleted'
        : `Chunks saved to ${outputDir}/<label>/` + (mode === 'both' ? ' (also uploaded)' : '');
    }

    const grid = document.getElementById('cameras-grid');
    if (data.cameras && data.cameras.length) {
      grid.innerHTML = data.cameras.map(renderCamera).join('');
    } else {
      grid.innerHTML = `<div class="card"><p style="color:var(--muted)">
        No cameras configured — use the Connection Settings to add cameras via config.yaml,
        or inject chunks manually below.</p></div>`;
    }
    document.getElementById('last-update').textContent =
      'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) { /* silently ignore during startup */ }
}

// ---------------------------------------------------------------------------
// Storage mode toggle
// ---------------------------------------------------------------------------
async function setStorageMode(mode) {
  try {
    const r = await fetch(API + '/api/storage-mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode}),
    });
    const data = await r.json();
    if (r.ok) {
      showToast('Storage mode: ' + mode);
      fetchStatus();
    } else {
      showToast('Error: ' + (data.detail || 'unknown'), true);
    }
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// YAML config editor
// ---------------------------------------------------------------------------
async function loadConfig() {
  try {
    const r = await fetch(API + '/api/config');
    if (!r.ok) { showToast('Failed to load config', true); return; }
    const data = await r.json();
    cmEditor.setValue(data.yaml || '');
    cmEditor.clearHistory();
  } catch(e) { showToast('Error: ' + e.message, true); }
}

async function saveConfig() {
  const rawYaml = cmEditor.getValue();
  try {
    const r = await fetch(API + '/api/config/reload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({yaml: rawYaml}),
    });
    const data = await r.json();
    if (r.ok) {
      showToast('Config saved and reloaded');
      loadConnection();
      fetchStatus();
    } else {
      showToast('Error: ' + (data.detail || 'unknown'), true);
    }
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// Connection settings form
// ---------------------------------------------------------------------------
const CONN_FIELDS = {
  'conn-api-base-url':          'api_base_url',
  'conn-venue-id':              'venue_id',
  'conn-keycloak-url':          'keycloak_url',
  'conn-keycloak-realm':        'keycloak_realm',
  'conn-keycloak-client-id':    'keycloak_client_id',
  'conn-keycloak-client-secret':'keycloak_client_secret',
};

async function loadConnection() {
  try {
    const r = await fetch(API + '/api/connection');
    if (!r.ok) return;
    const data = await r.json();
    for (const [inputId, key] of Object.entries(CONN_FIELDS)) {
      const el = document.getElementById(inputId);
      if (el) el.value = data[key] ?? '';
    }
  } catch(e) { /* ignore */ }
}

async function saveConnection() {
  const payload = {};
  for (const [inputId, key] of Object.entries(CONN_FIELDS)) {
    const el = document.getElementById(inputId);
    if (el) payload[key] = el.value.trim();
  }
  try {
    const r = await fetch(API + '/api/connection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok) {
      showToast('Connection settings saved');
      loadConfig();   // refresh YAML editor to reflect changes
      fetchStatus();
    } else {
      showToast('Error: ' + (data.detail || 'unknown'), true);
    }
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// Per-camera restart
// ---------------------------------------------------------------------------
async function restartCamera(label) {
  try {
    const r = await fetch(API + `/api/cameras/${encodeURIComponent(label)}/restart`, {method:'POST'});
    const data = await r.json();
    showToast(r.ok ? `Restarting ${label}…` : 'Error: ' + (data.detail || 'unknown'), !r.ok);
    if (r.ok) setTimeout(fetchStatus, 1500);
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// Manual chunk injection
// ---------------------------------------------------------------------------
async function injectFile(label, file) {
  const fd = new FormData();
  fd.append('label', label);
  fd.append('file', file);
  try {
    const r = await fetch(API + `/api/inject?label=${encodeURIComponent(label)}`, {method:'POST', body: fd});
    const data = await r.json();
    if (r.ok) showToast(`Injected ${file.name} → ${label}`);
    else showToast('Error: ' + (data.detail || 'unknown'), true);
  } catch(e) { showToast('Error: ' + e.message, true); }
}

function handleFileSelect(files) {
  if (!files.length) return;
  const label = document.getElementById('inject-label').value.trim();
  if (!label) { showToast('Enter a camera label first', true); return; }
  injectFile(label, files[0]);
}

function handleDrop(e) {
  e.preventDefault();
  document.getElementById('inject-zone').classList.remove('drag');
  const files = e.dataTransfer.files;
  if (!files.length) return;
  const label = document.getElementById('inject-label').value.trim();
  if (!label) { showToast('Enter a camera label first', true); return; }
  injectFile(label, files[0]);
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
let toastTimer = null;
function showToast(msg, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.borderColor = isError ? 'var(--red)' : 'var(--green)';
  el.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
loadConfig();
loadConnection();
fetchStatus();
setInterval(fetchStatus, 3000);
</script>
</body>
</html>
"""


def _redact(config: dict) -> dict:
    """Return a copy of config with sensitive fields replaced by '***'."""
    secret_keys = {"keycloak_client_secret"}
    out = {}
    for k, v in config.items():
        if k in secret_keys:
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _redact(v)
        elif isinstance(v, list):
            out[k] = [_redact(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


class WebServer(threading.Thread):
    """Runs a uvicorn/FastAPI server in a daemon thread."""

    def __init__(self, state: "AgentState", config_path: str = "config.yaml", port: int = 5174) -> None:
        super().__init__(name="web-server", daemon=True)
        self.state = state
        self.config_path = config_path
        self.port = port
        self.app = self._build_app()

    def run(self) -> None:
        logger.info("Web dashboard starting on http://0.0.0.0:%d", self.port)
        uvicorn.run(self.app, host="0.0.0.0", port=self.port, log_level="warning")

    def _build_app(self) -> FastAPI:
        from src.config_loader import load_config, save_config

        app = FastAPI(title="vybe-camera-agent", docs_url=None, redoc_url=None)
        state = self.state
        config_path = self.config_path

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def dashboard():
            return _DASHBOARD_HTML

        @app.get("/api/status")
        async def get_status():
            return JSONResponse(state.status())

        @app.get("/api/config")
        async def get_config():
            try:
                with open(config_path) as f:
                    raw_yaml = f.read()
                return {"yaml": raw_yaml, "parsed": _redact(state.get_config())}
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        @app.post("/api/config/reload")
        async def reload_config(body: dict):
            raw_yaml = body.get("yaml", "").strip()
            if not raw_yaml:
                raise HTTPException(status_code=400, detail="yaml field is required")
            try:
                parsed = yaml.safe_load(raw_yaml)
                if not isinstance(parsed, dict):
                    raise HTTPException(status_code=400, detail="YAML must be a mapping")
            except yaml.YAMLError as exc:
                raise HTTPException(status_code=400, detail=f"YAML parse error: {exc}")

            try:
                save_config(config_path, raw_yaml)
                new_config = load_config(config_path)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            state.reload_config(new_config)
            return {"ok": True, "cameras": len(new_config.get("cameras", []))}

        @app.get("/api/connection")
        async def get_connection():
            cfg = state.get_config()
            return {k: cfg.get(k, "") for k in _CONNECTION_KEYS}

        @app.post("/api/connection")
        async def save_connection(body: dict):
            # Validate at least the required fields are present
            missing = [k for k in ("api_base_url", "keycloak_url", "keycloak_realm",
                                   "keycloak_client_id", "venue_id")
                       if not body.get(k, "").strip()]
            if missing:
                raise HTTPException(status_code=400, detail=f"Missing fields: {', '.join(missing)}")

            # Load raw YAML, parse, patch connection keys, re-serialize
            try:
                with open(config_path) as f:
                    raw_yaml = f.read()
                parsed = yaml.safe_load(raw_yaml)
                if not isinstance(parsed, dict):
                    parsed = {}
            except (OSError, yaml.YAMLError) as exc:
                raise HTTPException(status_code=500, detail=f"Could not read config: {exc}")

            for k in _CONNECTION_KEYS:
                if k in body and body[k] != "":
                    parsed[k] = body[k]

            new_yaml = yaml.dump(parsed, default_flow_style=False, allow_unicode=True, sort_keys=False)

            try:
                save_config(config_path, new_yaml)
                new_config = load_config(config_path)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            state.reload_config(new_config)
            return {"ok": True}

        @app.get("/api/storage-mode")
        async def get_storage_mode():
            cfg = state.get_config()
            return {
                "storage_mode": cfg.get("storage_mode", "upload"),
                "output_dir": cfg.get("output_dir", "/output"),
            }

        @app.post("/api/storage-mode")
        async def set_storage_mode(body: dict):
            mode = body.get("mode", "")
            if mode not in ("upload", "local", "both"):
                raise HTTPException(status_code=400, detail="mode must be 'upload', 'local', or 'both'")

            try:
                with open(config_path) as f:
                    raw_yaml = f.read()
                parsed = yaml.safe_load(raw_yaml) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise HTTPException(status_code=500, detail=f"Could not read config: {exc}")

            parsed["storage_mode"] = mode
            new_yaml = yaml.dump(parsed, default_flow_style=False, allow_unicode=True, sort_keys=False)

            try:
                save_config(config_path, new_yaml)
                new_config = load_config(config_path)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            # reload_config diffs cameras only — storage_mode update is picked up
            # by the Uploader reading state.get_config() on the next chunk
            state.reload_config(new_config)
            return {"ok": True, "storage_mode": mode}

        @app.post("/api/cameras/{label}/restart")
        async def restart_camera(label: str):
            ok = state.restart_camera(label)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Camera '{label}' not found")
            return {"ok": True, "label": label}

        @app.post("/api/inject")
        async def inject_chunk(label: str, file: UploadFile = File(...)):
            if not label:
                raise HTTPException(status_code=400, detail="label query parameter is required")

            config = state.get_config()
            dest_dir = os.path.join(config.get("temp_dir", "/tmp/vybe-camera-agent"), label)
            os.makedirs(dest_dir, exist_ok=True)

            filename = os.path.basename(file.filename or "injected.mp4")
            dest_path = os.path.join(dest_dir, filename)

            try:
                contents = await file.read()
                with open(dest_path, "wb") as f_out:
                    f_out.write(contents)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Could not write file: {exc}")

            state.upload_queue.put({"label": label, "path": dest_path})
            state.record_chunk_enqueued(label)
            logger.info("[%s] manually injected chunk: %s (%d bytes)", label, filename, len(contents))

            return {"ok": True, "label": label, "file": filename, "bytes": len(contents)}

        return app
