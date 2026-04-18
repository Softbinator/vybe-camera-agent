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
from fastapi.responses import HTMLResponse, JSONResponse, Response

from src.preview import capture_snapshot
from src.usb_scanner import scan_usb_devices

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
                 margin-bottom: 24px; gap: 16px; flex-wrap: wrap; }
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
    /* Preview modal */
    .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.8);
                display: none; align-items: center; justify-content: center; z-index: 500; }
    .modal-bg.show { display: flex; }
    .modal { background: var(--surface); border: 1px solid var(--border);
             border-radius: var(--radius); padding: 18px; max-width: 90vw; max-height: 90vh;
             display: flex; flex-direction: column; gap: 12px; }
    .modal header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .modal img { max-width: 80vw; max-height: 70vh; border-radius: 6px;
                 background: #000; object-fit: contain; }
    .modal .err { color: var(--red); font-size: .85rem; }
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
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button id="btn-toggle-recording" class="btn-primary" onclick="toggleRecording()">Loading…</button>
      <button id="btn-purge-queue" class="btn-danger" onclick="purgeQueue()" title="Drop every chunk pending upload — use when the uploader is stuck">Purge queue</button>
    </div>
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

  <!-- Discovered cameras awaiting credentials -->
  <span class="section-title">Discovered Cameras (Awaiting Credentials)</span>
  <div class="grid" id="discovered-grid">
    <div class="card"><p style="color:var(--muted)">No pending cameras.</p></div>
  </div>

  <!-- Add USB camera -->
  <div class="card" style="margin-bottom:24px">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
      <div>
        <div style="font-size:.95rem;font-weight:600">Add USB Camera</div>
        <div style="font-size:.78rem;color:var(--muted)">Enumerate /dev/video* and register one in a click.</div>
      </div>
      <button class="btn-primary" onclick="scanUsb()">Scan USB</button>
    </div>
    <div id="usb-scan-results" style="margin-top:12px"></div>
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
      Drop up to 20 .mp4 files here or click to browse (sorted by filename)
    </div>
    <div id="inject-progress" style="display:none;margin-top:10px;font-size:.85rem;color:var(--muted)"></div>
    <input type="file" id="inject-input" accept=".mp4,video/mp4" multiple onchange="handleFileSelect(this.files)">
  </div>

  <!-- Preview modal -->
  <div class="modal-bg" id="preview-modal" onclick="closePreview(event)">
    <div class="modal" onclick="event.stopPropagation()">
      <header>
        <div style="font-weight:600" id="preview-title">Preview</div>
        <button class="btn-neutral" onclick="closePreview()">Close</button>
      </header>
      <img id="preview-img" alt="camera preview">
      <div class="err" id="preview-err" style="display:none"></div>
      <div style="font-size:.75rem;color:var(--muted)">Snapshot refreshes every 2 s. Not recorded or uploaded.</div>
    </div>
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
  if (s === 'awaiting_credentials') return 'badge-blue';
  if (s === 'paused') return 'badge-yellow';
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
      <div class="row">
        <button class="btn-primary" onclick="openPreview('${escHtml(cam.label)}')">Preview</button>
        <button class="btn-danger"  onclick="restartCamera('${escHtml(cam.label)}')">Restart</button>
      </div>
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

    // Reflect paused flag on the toggle button.
    const recBtn = document.getElementById('btn-toggle-recording');
    if (recBtn) {
      const paused = !!data.recording_paused;
      recBtn.textContent = paused ? 'Resume recording' : 'Pause recording';
      recBtn.className = paused ? 'btn-primary' : 'btn-neutral';
      recBtn.dataset.paused = paused ? '1' : '0';
    }

    const grid = document.getElementById('cameras-grid');
    const activeCams = (data.cameras || []).filter(c => !c.pending_credentials);
    if (activeCams.length) {
      grid.innerHTML = activeCams.map(renderCamera).join('');
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
const MAX_INJECT_FILES = 2000;

async function injectFile(label, file) {
  const fd = new FormData();
  fd.append('label', label);
  fd.append('file', file);
  const r = await fetch(API + `/api/inject?label=${encodeURIComponent(label)}`, {method:'POST', body: fd});
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || 'unknown error');
  return data;
}

async function handleFileSelect(files) {
  if (!files.length) return;
  const label = document.getElementById('inject-label').value.trim();
  if (!label) { showToast('Enter a camera label first', true); return; }

  const fileArr = Array.from(files);
  if (fileArr.length > MAX_INJECT_FILES) {
    showToast(`Select at most ${MAX_INJECT_FILES} files at a time`, true);
    return;
  }

  // Sort lexicographically — chronological for YYYYMMDD_HHMMSS.mp4 filenames
  fileArr.sort((a, b) => a.name.localeCompare(b.name));

  const zone = document.getElementById('inject-zone');
  const progress = document.getElementById('inject-progress');
  const labelInput = document.getElementById('inject-label');
  zone.style.pointerEvents = 'none';
  zone.style.opacity = '0.5';
  labelInput.disabled = true;
  progress.style.display = 'block';

  try {
    for (let i = 0; i < fileArr.length; i++) {
      const file = fileArr[i];
      progress.textContent = `Uploading ${i + 1} / ${fileArr.length} — ${file.name}`;
      await injectFile(label, file);
    }
    showToast(`Injected ${fileArr.length} file(s) → ${label}`);
  } catch(e) {
    showToast('Upload failed: ' + e.message, true);
  } finally {
    zone.style.pointerEvents = '';
    zone.style.opacity = '';
    labelInput.disabled = false;
    progress.style.display = 'none';
    document.getElementById('inject-input').value = '';
  }
}

function handleDrop(e) {
  e.preventDefault();
  document.getElementById('inject-zone').classList.remove('drag');
  handleFileSelect(e.dataTransfer.files);
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
// Discovered cameras (pending credentials)
// ---------------------------------------------------------------------------
function renderDiscovered(cam) {
  const label = escHtml(cam.label);
  return `
    <div class="card">
      <div class="card-title">${label} <span class="badge badge-blue">awaiting credentials</span></div>
      <div class="meta">
        <span>RTSP URL</span><strong style="word-break:break-all">${escHtml(cam.rtsp_url || '')}</strong>
      </div>
      <div class="conn-grid" style="margin-bottom:10px">
        <div class="field"><label>Username</label><input id="cred-user-${label}" type="text" placeholder="admin"></div>
        <div class="field"><label>Password</label><input id="cred-pass-${label}" type="password" placeholder="••••••••"></div>
      </div>
      <div class="row">
        <button class="btn-primary" onclick="saveCredentials('${label}')">Save &amp; Start</button>
        <button class="btn-danger"  onclick="deleteCamera('${label}')">Delete</button>
      </div>
    </div>`;
}
async function fetchDiscovered() {
  try {
    const r = await fetch(API + '/api/discovered');
    if (!r.ok) return;
    const data = await r.json();
    const grid = document.getElementById('discovered-grid');
    if (data.cameras && data.cameras.length) {
      grid.innerHTML = data.cameras.map(renderDiscovered).join('');
    } else {
      grid.innerHTML = `<div class="card"><p style="color:var(--muted)">No pending cameras.</p></div>`;
    }
  } catch(e) { /* ignore */ }
}
async function saveCredentials(label) {
  const username = document.getElementById('cred-user-' + label)?.value.trim() || '';
  const password = document.getElementById('cred-pass-' + label)?.value || '';
  if (!username || !password) { showToast('Username and password required', true); return; }
  try {
    const r = await fetch(API + `/api/cameras/${encodeURIComponent(label)}/credentials`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password}),
    });
    const data = await r.json();
    if (r.ok) { showToast(`Credentials saved for ${label}`); fetchDiscovered(); fetchStatus(); }
    else     { showToast('Error: ' + (data.detail || 'unknown'), true); }
  } catch(e) { showToast('Error: ' + e.message, true); }
}
async function deleteCamera(label) {
  if (!confirm(`Delete camera "${label}"?`)) return;
  try {
    const r = await fetch(API + `/api/cameras/${encodeURIComponent(label)}`, {method:'DELETE'});
    const data = await r.json();
    if (r.ok) { showToast(`Deleted ${label}`); fetchDiscovered(); fetchStatus(); loadConfig(); }
    else     { showToast('Error: ' + (data.detail || 'unknown'), true); }
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// USB scan
// ---------------------------------------------------------------------------
async function scanUsb() {
  const out = document.getElementById('usb-scan-results');
  out.innerHTML = `<p style="color:var(--muted);font-size:.82rem">Scanning…</p>`;
  try {
    const r = await fetch(API + '/api/usb-scan');
    const data = await r.json();
    if (!r.ok) { out.innerHTML = `<p style="color:var(--red)">${escHtml(data.detail || 'scan failed')}</p>`; return; }
    const devs = data.devices || [];
    if (!devs.length) { out.innerHTML = `<p style="color:var(--muted);font-size:.82rem">No video devices found.</p>`; return; }
    out.innerHTML = devs.map(d => {
      const fmts = (d.formats || []).map(f => `${escHtml(f.pixel_format)} (${(f.sizes||[]).slice(0,4).map(escHtml).join(', ')})`).join(' · ');
      const devEsc = escHtml(d.device);
      const defaultLabel = 'usb-' + (d.device || '').replace(/[^a-zA-Z0-9]/g, '-').replace(/^-+/, '');
      return `
        <div style="border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:8px">
          <div style="font-weight:600;font-size:.85rem">${escHtml(d.name)} <span style="color:var(--muted);font-weight:400">${devEsc}</span></div>
          <div style="font-size:.75rem;color:var(--muted);margin-top:4px">${fmts || 'no formats reported'}</div>
          <div class="conn-grid" style="grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px">
            <div class="field"><label>Label</label><input id="usb-label-${devEsc}" type="text" value="${escHtml(defaultLabel)}"></div>
            <div class="field"><label>Framerate</label><input id="usb-fps-${devEsc}" type="number" min="1" max="60" value="30"></div>
            <div class="field"><label>Size</label><input id="usb-size-${devEsc}" type="text" value="1280x720"></div>
          </div>
          <button class="btn-primary" style="margin-top:8px" onclick="addUsbCamera('${devEsc}')">Add</button>
        </div>`;
    }).join('');
  } catch(e) { out.innerHTML = `<p style="color:var(--red)">${escHtml(e.message)}</p>`; }
}

async function addUsbCamera(device) {
  const label = document.getElementById('usb-label-' + device)?.value.trim();
  const fps = Number(document.getElementById('usb-fps-' + device)?.value) || 30;
  const size = document.getElementById('usb-size-' + device)?.value.trim() || '1280x720';
  if (!label) { showToast('Label is required', true); return; }
  try {
    const r = await fetch(API + '/api/cameras', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        label, source: 'v4l2', device, framerate: fps, video_size: size,
      }),
    });
    const data = await r.json();
    if (r.ok) { showToast(`Added ${label}`); fetchStatus(); loadConfig(); }
    else     { showToast('Error: ' + (data.detail || 'unknown'), true); }
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// Recording pause/resume + queue purge
// ---------------------------------------------------------------------------
async function toggleRecording() {
  const btn = document.getElementById('btn-toggle-recording');
  const paused = btn && btn.dataset.paused === '1';
  const endpoint = paused ? '/api/recording/resume' : '/api/recording/pause';
  try {
    const r = await fetch(API + endpoint, {method: 'POST'});
    const data = await r.json();
    if (r.ok) {
      showToast(paused ? 'Recording resumed' : 'Recording paused');
      fetchStatus();
    } else {
      showToast('Error: ' + (data.detail || 'unknown'), true);
    }
  } catch(e) { showToast('Error: ' + e.message, true); }
}
async function purgeQueue() {
  if (!confirm('Drop every chunk currently pending upload? Use this when the uploader is stuck on a failed upload.')) return;
  try {
    const r = await fetch(API + '/api/queue/purge', {method: 'POST'});
    const data = await r.json();
    if (r.ok) { showToast(`Queue purged — dropped ${data.dropped} chunk(s)`); fetchStatus(); }
    else     { showToast('Error: ' + (data.detail || 'unknown'), true); }
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// ---------------------------------------------------------------------------
// Preview modal (camera snapshot)
// ---------------------------------------------------------------------------
let previewTimer = null;
let previewLabel = null;

function openPreview(label) {
  previewLabel = label;
  document.getElementById('preview-title').textContent = 'Preview — ' + label;
  document.getElementById('preview-err').style.display = 'none';
  document.getElementById('preview-img').style.display = '';
  document.getElementById('preview-modal').classList.add('show');
  refreshPreview();
  previewTimer = setInterval(refreshPreview, 2000);
}

function refreshPreview() {
  if (!previewLabel) return;
  const img = document.getElementById('preview-img');
  const err = document.getElementById('preview-err');
  const url = API + `/api/cameras/${encodeURIComponent(previewLabel)}/preview.jpg?ts=${Date.now()}`;
  // Use fetch so we can show the server's error text on failure.
  fetch(url).then(async r => {
    if (!r.ok) {
      const text = await r.text();
      img.style.display = 'none';
      err.style.display = '';
      try { err.textContent = 'Preview failed: ' + (JSON.parse(text).detail || text); }
      catch { err.textContent = 'Preview failed: ' + text; }
      return;
    }
    const blob = await r.blob();
    img.src = URL.createObjectURL(blob);
    img.style.display = '';
    err.style.display = 'none';
  }).catch(e => {
    img.style.display = 'none';
    err.style.display = '';
    err.textContent = 'Preview failed: ' + e.message;
  });
}

function closePreview(e) {
  if (e && e.target !== e.currentTarget) return;
  previewLabel = null;
  if (previewTimer) { clearInterval(previewTimer); previewTimer = null; }
  document.getElementById('preview-modal').classList.remove('show');
  const img = document.getElementById('preview-img');
  if (img.src) URL.revokeObjectURL(img.src);
  img.src = '';
}
// ESC to close preview
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePreview(); });

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
loadConfig();
loadConnection();
fetchStatus();
fetchDiscovered();
setInterval(fetchStatus, 3000);
setInterval(fetchDiscovered, 5000);
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

        # ------------------------------------------------------------------
        # Auto-discovery & USB-scan endpoints
        # ------------------------------------------------------------------

        def _patch_cameras(mutate) -> dict:
            """Read config.yaml, call mutate(cameras: list) -> None, save + reload.
            Returns the new config. Raises HTTPException on validation failure."""
            try:
                with open(config_path) as f:
                    raw_yaml = f.read()
                parsed = yaml.safe_load(raw_yaml) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise HTTPException(status_code=500, detail=f"Could not read config: {exc}")

            cameras = parsed.get("cameras") or []
            mutate(cameras)
            parsed["cameras"] = cameras

            new_yaml = yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False, allow_unicode=True)
            try:
                save_config(config_path, new_yaml)
                new_config = load_config(config_path)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            state.reload_config(new_config)
            return new_config

        @app.get("/api/discovered")
        async def list_discovered():
            cfg = state.get_config()
            pending = [
                {
                    "label": c.get("label"),
                    "rtsp_url": c.get("rtsp_url"),
                    "auto_discovered": bool(c.get("auto_discovered")),
                }
                for c in cfg.get("cameras", [])
                if c.get("pending_credentials")
            ]
            return {"cameras": pending}

        @app.post("/api/cameras/{label}/credentials")
        async def set_camera_credentials(label: str, body: dict):
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if not username:
                raise HTTPException(status_code=400, detail="username is required")
            if not password:
                raise HTTPException(status_code=400, detail="password is required")

            found = {"hit": False}

            def mutate(cameras):
                for cam in cameras:
                    if isinstance(cam, dict) and cam.get("label") == label:
                        cam["rtsp_username"] = username
                        cam["rtsp_password"] = password
                        cam["pending_credentials"] = False
                        found["hit"] = True
                        return
            _patch_cameras(mutate)

            if not found["hit"]:
                raise HTTPException(status_code=404, detail=f"Camera '{label}' not found")
            return {"ok": True, "label": label}

        @app.get("/api/usb-scan")
        async def usb_scan():
            return {"devices": scan_usb_devices()}

        @app.post("/api/cameras")
        async def add_camera(body: dict):
            label = (body.get("label") or "").strip()
            source = body.get("source") or "v4l2"
            if not label:
                raise HTTPException(status_code=400, detail="label is required")
            if source not in ("v4l2", "rtsp", "file"):
                raise HTTPException(status_code=400, detail="source must be v4l2, rtsp or file")

            new_cam: dict = {"label": label, "source": source}
            if source == "v4l2":
                device = (body.get("device") or "").strip()
                if not device:
                    raise HTTPException(status_code=400, detail="device is required for v4l2")
                new_cam["device"] = device
                for opt in ("framerate", "video_size", "input_format"):
                    if body.get(opt):
                        new_cam[opt] = body[opt]
            elif source == "rtsp":
                rtsp_url = (body.get("rtsp_url") or "").strip()
                if not rtsp_url:
                    raise HTTPException(status_code=400, detail="rtsp_url is required for rtsp")
                new_cam["rtsp_url"] = rtsp_url
                if body.get("rtsp_username"):
                    new_cam["rtsp_username"] = body["rtsp_username"]
                if body.get("rtsp_password"):
                    new_cam["rtsp_password"] = body["rtsp_password"]
            else:  # file
                replay_dir = (body.get("replay_dir") or "").strip()
                if not replay_dir:
                    raise HTTPException(status_code=400, detail="replay_dir is required for file")
                new_cam["replay_dir"] = replay_dir

            existing = {c.get("label") for c in state.get_config().get("cameras", []) if isinstance(c, dict)}
            if label in existing:
                raise HTTPException(status_code=409, detail=f"Camera '{label}' already exists")

            def mutate(cameras):
                cameras.append(new_cam)
            _patch_cameras(mutate)

            return {"ok": True, "label": label}

        # ------------------------------------------------------------------
        # Global recording controls (pause/resume all cameras) + queue purge
        # ------------------------------------------------------------------

        def _persist_paused_flag(paused: bool) -> None:
            """Persist `recording_paused` to config.yaml so the state survives a restart."""
            try:
                with open(config_path) as f:
                    parsed = yaml.safe_load(f.read()) or {}
            except (OSError, yaml.YAMLError):
                return
            parsed["recording_paused"] = bool(paused)
            try:
                save_config(config_path, yaml.safe_dump(parsed, sort_keys=False,
                                                        default_flow_style=False,
                                                        allow_unicode=True))
            except OSError as exc:
                logger.warning("could not persist recording_paused: %s", exc)

        @app.get("/api/recording")
        async def get_recording_state():
            return {"paused": state.is_paused()}

        @app.post("/api/recording/pause")
        async def pause_recording():
            stopped = state.pause_all()
            _persist_paused_flag(True)
            return {"ok": True, "paused": True, "workers_stopped": stopped}

        @app.post("/api/recording/resume")
        async def resume_recording():
            started = state.resume_all()
            _persist_paused_flag(False)
            return {"ok": True, "paused": False, "workers_started": started}

        @app.post("/api/queue/purge")
        async def purge_queue():
            dropped = state.purge_upload_queue()
            return {"ok": True, "dropped": dropped}

        # ------------------------------------------------------------------
        # Camera preview — on-demand JPEG snapshot
        # ------------------------------------------------------------------

        @app.get("/api/cameras/{label}/preview.jpg")
        async def preview_snapshot(label: str):
            cfg = state.get_config()
            cam = next((c for c in cfg.get("cameras", [])
                        if isinstance(c, dict) and c.get("label") == label), None)
            if cam is None:
                raise HTTPException(status_code=404, detail=f"Camera '{label}' not found")
            if cam.get("pending_credentials"):
                raise HTTPException(status_code=409, detail="Camera is awaiting credentials")
            try:
                jpeg = capture_snapshot(cam)
            except RuntimeError as exc:
                msg = str(exc)
                # On V4L2, a running capture worker holds /dev/videoN exclusively —
                # translate the raw ffmpeg error into an actionable hint.
                if cam.get("source") == "v4l2" and ("Device or resource busy" in msg or "EBUSY" in msg):
                    msg = (f"{cam.get('device','/dev/videoN')} is busy — "
                           f"pause recording to preview this camera.")
                raise HTTPException(status_code=502, detail=msg)
            return Response(
                content=jpeg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        @app.delete("/api/cameras/{label}")
        async def delete_camera(label: str):
            found = {"hit": False}

            def mutate(cameras):
                for i, cam in enumerate(cameras):
                    if isinstance(cam, dict) and cam.get("label") == label:
                        cameras.pop(i)
                        found["hit"] = True
                        return
            _patch_cameras(mutate)

            if not found["hit"]:
                raise HTTPException(status_code=404, detail=f"Camera '{label}' not found")
            return {"ok": True, "label": label}

        return app
