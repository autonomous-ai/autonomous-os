# Bootstrap & OTA System — AI Lamp

## 1. Overview

The AI Lamp device runs **5 software components** on a Raspberry Pi 4. All components are installed via an initial setup script and kept up-to-date by a background OTA worker.

| Component | Type | Install Method | Service Name | Install Path |
|---|---|---|---|---|
| **Lamp Server** | Go binary (ARM64) | Download zip from OTA | `lamp.service` | `/usr/local/bin/lamp-server` |
| **Bootstrap Server** | Go binary (ARM64) | Download zip from OTA | `bootstrap.service` | `/usr/local/bin/bootstrap-server` |
| **Web (Setup SPA)** | React/Vite bundle | Download zip from OTA | nginx serves static | `/usr/share/nginx/html/setup/` |
| **OpenClaw** | Node.js package | `npm install -g` | `openclaw.service` | Global npm |
| **LeLamp Runtime** | Python package | Download zip from OTA | `lamp-lelamp.service` | `/opt/lelamp/` |

### Architecture Diagram

```
                    ┌──────────────────────────────┐
                    │   OTA Metadata (GCS JSON)     │
                    │                                │
                    │  lamp:      {version, url}     │
                    │  bootstrap: {version, url}     │
                    │  web:       {version, url}     │
                    │  openclaw:  {version}          │
                    │  lelamp:    {version, url}     │
                    └───────────────┬────────────────┘
                                    │ poll every 5m
                                    ▼
┌───────────────────────────────────────────────────────────────────┐
│                    Bootstrap Server (Go, port 8080)               │
│                                                                   │
│  checkLoop() → for each component:                                │
│    1. Detect current installed version                            │
│    2. Compare to OTA metadata target version                      │
│    3. If mismatch → applyUpdate()                                 │
│       → download zip / npm install                                │
│       → extract to install path                                   │
│       → systemctl restart {service}                               │
│    4. Persist state to /root/bootstrap/state.json                 │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## 2. OTA Metadata Format

Single JSON file hosted on GCS. All components reference this file.

**URL**: `https://storage.googleapis.com/{BUCKET}/lamp/ota/metadata.json`

```json
{
  "lamp": {
    "version": "1.2.3",
    "url": "https://storage.googleapis.com/{BUCKET}/lamp/ota/lamp/1.2.3/lamp-1.2.3.zip"
  },
  "bootstrap": {
    "version": "1.0.5",
    "url": "https://storage.googleapis.com/{BUCKET}/lamp/ota/bootstrap/1.0.5/bootstrap-1.0.5.zip"
  },
  "web": {
    "version": "0.9.0",
    "url": "https://storage.googleapis.com/{BUCKET}/lamp/ota/web/0.9.0/setup-0.9.0.zip"
  },
  "openclaw": {
    "version": "2026.3.8"
  },
  "lelamp": {
    "version": "1.0.0",
    "url": "https://storage.googleapis.com/{BUCKET}/lamp/ota/lelamp/1.0.0/lelamp-1.0.0.zip"
  }
}
```

**Domain types** — `domain/ota.go`:

```go
const (
    OTAKeyLamp      = "lamp"
    OTAKeyBootstrap = "bootstrap"
    OTAKeyWeb       = "web"
    OTAKeyOpenClaw  = "openclaw"
    // OTAKeyLeLamp will be added when LeLamp OTA is implemented
)

type OTAMetadata map[string]OTAComponent

type OTAComponent struct {
    Version string `json:"version"`
    URL     string `json:"url,omitempty"`
}
```

---

## 3. Initial Setup (`scripts/setup.sh`)

One-time provisioning script run on a fresh Raspberry Pi. Executes stages sequentially.

**Quick install from CDN:**
```bash
curl -fsSL https://cdn.autonomous.ai/lamp/install.sh | sudo bash
```

### Stage Overview

| Stage | Name | Description |
|---|---|---|
| -1 | Locale fix | Ensure `C.UTF-8` encoding |
| 0 | Prerequisites | System packages, Node.js 22 |
| 0a | WiFi stability | Disable IPv6, WiFi power saving (RPi5) |
| 0b | Enable SPI | For WS2812 LED driver |
| 1 | Fetch OTA metadata | Download metadata.json, extract versions and URLs |
| 1b | Install binaries | Download + install lamp-server, bootstrap-server, create systemd services |
| 2 | Install OpenClaw | `npm install -g openclaw`, create config, create systemd service |
| **2b** | **Install LeLamp** | **Download + install LeLamp Python runtime, create systemd service** (NEW) |
| 3 | Setup nginx | Download web bundle, configure reverse proxy + captive portal |
| 4 | Setup WiFi AP | Configure hostapd, dnsmasq, start AP mode for provisioning |

### Stage 2b: Install LeLamp Runtime (NEW)

This stage installs the LeLamp Python runtime that provides hardware drivers for servos, LEDs, and audio.

```bash
stage_install_lelamp() {
    echo "=== Stage 2b: Install LeLamp Runtime ==="

    # 1. Install Python dependencies
    apt-get install -y python3 python3-pip python3-venv

    # 2. Create install directory
    mkdir -p /opt/lelamp

    # 3. Download from OTA metadata
    LELAMP_URL=$(echo "$OTA_JSON" | jq -r '.lelamp.url')
    LELAMP_VERSION=$(echo "$OTA_JSON" | jq -r '.lelamp.version')

    curl -fsSL "$LELAMP_URL" -o /tmp/lelamp.zip
    unzip -o /tmp/lelamp.zip -d /opt/lelamp/
    rm /tmp/lelamp.zip

    # 4. Install Python dependencies in venv
    python3 -m venv /opt/lelamp/venv
    /opt/lelamp/venv/bin/pip install -r /opt/lelamp/requirements.txt

    # 5. Create systemd service
    cat > /etc/systemd/system/lamp-lelamp.service << 'UNIT'
[Unit]
Description=LeLamp Python Runtime — Hardware Drivers
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/lelamp
ExecStart=/opt/lelamp/venv/bin/python -m lelamp.server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable lamp-lelamp.service
    systemctl start lamp-lelamp.service

    echo "LeLamp $LELAMP_VERSION installed at /opt/lelamp/"
}
```

### Systemd Services Created by Setup

| Service | ExecStart | Port | Notes |
|---|---|---|---|
| `lamp.service` | `/usr/local/bin/lamp-server` | 5000 | Main HTTP API, always running |
| `bootstrap.service` | `/usr/local/bin/bootstrap-server` | 8080 | OTA worker, polls for updates. Exposes `POST /force-check` to trigger immediate OTA check |
| `openclaw.service` | `xvfb-run ... openclaw gateway run` | — | AI brain, memory limit 1500M |
| `lamp-lelamp.service` | `uvicorn lelamp.server:app --host 127.0.0.1 --port 5001` | 5001 | Hardware drivers (servo, LED, camera, audio) |
| nginx | `nginx` | 80 | Setup SPA + reverse proxy (`/api/` → Lamp 5000, `/hw/` → LeLamp 5001) |

### Service Dependency Order

```
boot
  → lamp.service      (system layer, LED boot animation)
  → bootstrap.service   (starts polling for updates)
  → lamp-lelamp.service      (hardware drivers ready)
  → openclaw.service    (AI brain, connects to lamp via HTTP)
  → nginx               (web UI for setup)
```

---

## 4. Bootstrap OTA Worker

### Config (`config/bootstrap.json`)

```json
{
  "httpPort": 8080,
  "metadata_url": "https://storage.googleapis.com/{BUCKET}/lamp/ota/metadata.json",
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
```

Falls back to defaults if file missing.

### State (`/root/bootstrap/state.json`)

Tracks last known installed version per component:

```json
{
  "components": {
    "lamp": "1.2.3",
    "bootstrap": "1.0.5",
    "web": "0.9.0",
    "openclaw": "2026.3.8",
    "lelamp": "1.0.0"
  }
}
```

### Core Loop (`bootstrap/bootstrap.go`)

```
checkLoop():
  1. checkOnce() immediately on startup
  2. Sleep poll_interval (default 5m)
  3. Repeat

checkOnce():
  1. Fetch OTA metadata JSON
  2. For each key [lamp, bootstrap, web, lelamp]:
     → reconcile(key, metadata[key])
  NOTE: OpenClaw OTA is temporarily disabled (reconcileOpenClawFromNpm commented out)
  3. Save state

reconcile(key, target):
  1. Detect current installed version
  2. Compare to target version
  3. If same → update state, return
  4. If different →
     a. Set LED orange breathing (OTA in progress)
     b. applyUpdate(key, target)
     c. Success → green flash | Failure → red pulse
```

### OTA LED Feedback

Bootstrap uses `lib/lelamp` to show update status on LEDs. See [status-led.md](status-led.md) for full spec.

| Phase | LED |
|-------|-----|
| Downloading + installing | Orange breathing `(255, 140, 0)` |
| Success | Green flash `(0, 255, 80)` |
| Failure | Red pulse `(255, 30, 30)` |

### Version Detection Per Component

| Component | How to Detect Current Version |
|---|---|
| `lamp` | Run `lamp-server --version`, parse output |
| `bootstrap` | Compiled-in constant `config.BootstrapVersion` (ldflags) |
| `web` | Read file `/usr/share/nginx/html/setup/VERSION` |
| `openclaw` | Run `openclaw --version`, extract semver with regex |
| `lelamp` | Run `/opt/lelamp/venv/bin/python -m lelamp --version` OR read `/opt/lelamp/VERSION` file |

### Update Application Per Component

| Component | Update Steps |
|---|---|
| `lamp` | Run `software-update lamp` (blocks up to 10 min) |
| `bootstrap` | Spawn detached `software-update bootstrap` (self-update, survives restart) |
| `web` | Run `software-update web` |
| `openclaw` | ~~Run `npm install -g openclaw@{version}` → `systemctl restart openclaw`~~ (temporarily disabled) |
| `lelamp` | Run `software-update lelamp` → `systemctl restart lamp-lelamp` |

---

## 5. Software Update Script (`/usr/local/bin/software-update`)

Bash script installed by setup.sh. Called by bootstrap worker to apply updates.

### LeLamp Case (NEW)

```bash
"lelamp")
    echo "Updating LeLamp to $VERSION..."

    # Download
    curl -fsSL "$URL" -o /tmp/lelamp-update.zip

    # Stop service before updating
    systemctl stop lamp-lelamp.service

    # Backup current
    cp -r /opt/lelamp /opt/lelamp.bak 2>/dev/null || true

    # Extract (preserve venv if only code changed, or rebuild)
    unzip -o /tmp/lelamp-update.zip -d /opt/lelamp/

    # Reinstall dependencies if requirements.txt changed
    /opt/lelamp/venv/bin/pip install -r /opt/lelamp/requirements.txt --quiet

    # Restart
    systemctl start lamp-lelamp.service

    # Cleanup
    rm -f /tmp/lelamp-update.zip
    rm -rf /opt/lelamp.bak

    echo "LeLamp updated to $VERSION"
    ;;
```

---

## 6. LeLamp Runtime — Source & Integration

### Source Strategy: Copy + Track Manually

LeLamp runtime code is **copied** from the upstream open-source project into this mono-repo, then modified heavily.

**Why copy, not submodule/subtree:**
- We need to **remove** LiveKit/OpenAI integration (replaced by OpenClaw)
- We need to **add** HTTP API server (Flask/FastAPI) for Lamp Server to bridge to
- We need to **add** DisplayService (GC9A01 eyes + info, not in original)
- We need to **modify** services to work with our architecture
- The overlap is drivers only (~30-40% of their code), the rest is rewritten

**Upstream tracking:**
- Source: `https://github.com/humancomputerlab/lelamp_runtime`
- Record the upstream commit hash in `lelamp/UPSTREAM.md` when copying
- Periodically check upstream for driver-level fixes (servo protocol, LED timing, etc.)
- Cherry-pick relevant driver changes manually
- Ignore upstream AI/LiveKit changes (we replaced that entirely)

**Implementation steps:**
1. Clone `humancomputerlab/lelamp_runtime` to a temp directory
2. Copy driver code (`services/motors.py`, `services/rgb.py`, `services/audio.py`, `services/service_base.py`) into `lelamp/services/`
3. Remove all LiveKit, OpenAI, and conversation code
4. Add `lelamp/server.py` — new HTTP API server (FastAPI)
5. Add `lelamp/services/display.py` — new DisplayService for GC9A01
6. Create `lelamp/UPSTREAM.md` with source commit hash and date
7. Test on Pi 4 with actual hardware

### Mono-repo Layout

LeLamp lives inside this repo as a Python subfolder alongside Go and TypeScript:

```
ai-lamp-openclaw/
├── lamp/                 # Go code (forked from lobster)
│   ├── cmd/              # Go entrypoints
│   ├── server/           # Go HTTP layer
│   ├── internal/         # Go business logic
│   ├── bootstrap/        # Go OTA worker
│   └── domain/           # Shared structs
├── web/                  # TypeScript/React SPA (copied from lobster, renamed intern→lamp)
├── lelamp/               # Python hardware drivers (NEW)
│   ├── __init__.py       # Package init, exposes __version__
│   ├── server.py         # HTTP API server (FastAPI) — NEW, not from upstream
│   ├── services/
│   │   ├── motors.py     # MotorsService — 5x Feetech servo (from upstream)
│   │   ├── rgb.py        # RGBService — 64x WS2812 LED (from upstream)
│   │   ├── audio.py      # Audio — amixer, playback (from upstream)
│   │   ├── display.py    # DisplayService — GC9A01 LCD (NEW, not from upstream)
│   │   └── service_base.py  # Event-driven ServiceBase (from upstream)
│   ├── config.py         # Runtime config
│   ├── requirements.txt  # Python dependencies
│   ├── VERSION           # Plain text version string
│   └── UPSTREAM.md       # Tracks source commit from humancomputerlab/lelamp_runtime
├── resources/
│   └── openclaw-skills/  # SKILL.md files
├── scripts/
│   └── setup.sh
├── go.mod
├── Makefile
└── CLAUDE.md
```

3 languages (Go, Python, TypeScript), 3 folders, 1 repo. Each has its own build, but managed together.

### LeLamp OTA Package

For OTA distribution, LeLamp is zipped from the `lelamp/` folder:

```
lelamp-{version}.zip
├── lelamp/               # Full Python package
├── requirements.txt
└── VERSION
```

### LeLamp HTTP API (FastAPI on port 5001)

The LeLamp Python runtime exposes its own HTTP API on `127.0.0.1:5001`. Lamp Server (Go, port 5000) bridges OpenClaw skill requests to this API. Nginx proxies `/hw/*` for same-machine callers only — external clients receive 403. Swagger UI at `/hw/docs` is not accessible from LAN.

```
OpenClaw LLM → curl 127.0.0.1:5000/api/servo → Lamp Server → http://127.0.0.1:5001/servo → LeLamp Python → Hardware
External     → http://<device-ip>/hw/docs    → nginx → 403 Forbidden
```

#### Endpoints (v0.2.0)

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Hardware availability (servo, led, camera, audio) |
| `/servo` | GET | Available recordings + current state |
| `/servo/play` | POST | Play animation by name |
| `/led` | GET | LED strip info |
| `/led/solid` | POST | Fill with single color |
| `/led/paint` | POST | Set per-pixel colors |
| `/led/off` | POST | Turn off all LEDs |
| `/camera` | GET | Camera info (resolution, availability) |
| `/camera/snapshot` | GET | Capture single JPEG frame |
| `/camera/stream` | GET | MJPEG stream |
| `/audio` | GET | Audio device info (Seeed mic/speaker) |
| `/audio/volume` | GET | Get current volume |
| `/audio/volume` | POST | Set volume (0-100%) |
| `/audio/play-tone` | POST | Play test tone |
| `/audio/record` | POST | Record from mic, return WAV |

---

## 7. Upload / Publish Scripts

### `scripts/upload-lelamp.sh` (NEW)

```bash
#!/usr/bin/env bash
# Upload LeLamp runtime to OTA

set -euo pipefail

VERSION_FILE="VERSION_LELAMP"
BUCKET="s3-autonomous-upgrade-3"
OTA_PATH="lamp/ota/lelamp"
METADATA_PATH="lamp/ota/metadata.json"

# Auto-increment patch version
CURRENT=$(cat "$VERSION_FILE" 2>/dev/null || echo "0.0.0")
MAJOR=$(echo "$CURRENT" | cut -d. -f1)
MINOR=$(echo "$CURRENT" | cut -d. -f2)
PATCH=$(echo "$CURRENT" | cut -d. -f3)
NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"
echo "$NEW_VERSION" > "$VERSION_FILE"

# Package
echo "Packaging LeLamp $NEW_VERSION..."
cd path/to/lelamp-source
echo "$NEW_VERSION" > VERSION
zip -r "/tmp/lelamp-${NEW_VERSION}.zip" lelamp/ requirements.txt VERSION

# Upload zip
gsutil cp "/tmp/lelamp-${NEW_VERSION}.zip" \
    "gs://${BUCKET}/${OTA_PATH}/${NEW_VERSION}/lelamp-${NEW_VERSION}.zip"

# Update metadata
DOWNLOAD_URL="https://storage.googleapis.com/${BUCKET}/${OTA_PATH}/${NEW_VERSION}/lelamp-${NEW_VERSION}.zip"
gsutil cp "gs://${BUCKET}/${METADATA_PATH}" /tmp/metadata.json
jq --arg v "$NEW_VERSION" --arg u "$DOWNLOAD_URL" \
    '.lelamp = {"version": $v, "url": $u}' /tmp/metadata.json > /tmp/metadata-updated.json
gsutil cp /tmp/metadata-updated.json "gs://${BUCKET}/${METADATA_PATH}"

echo "LeLamp $NEW_VERSION published."
```

### All Upload Scripts

| Script | Component | Pattern |
|---|---|---|
| `scripts/upload-lamp.sh` | Lamp Server binary | Build → zip → GCS → update metadata |
| `scripts/upload-bootstrap.sh` | Bootstrap Server binary | Build → zip → GCS → update metadata |
| `scripts/upload-web.sh` | Web SPA bundle | Build → zip → GCS → update metadata |
| `scripts/upload-lelamp.sh` | LeLamp Python runtime (NEW) | Package → zip → GCS → update metadata |
| `scripts/upload-setup.sh` | Setup script | Upload to GCS |
| `scripts/upload-setup-ap.sh` | AP setup script | Upload to GCS |
| `scripts/upload-skills.sh` | OpenClaw skill files | Upload to GCS |
| `scripts/install.sh` | CDN install shortcut | `curl ... \| sudo bash` on Pi |
| `scripts/tag-release.sh` | Git release tag with OTA metadata snapshot | Fetch metadata.json → annotated tag → `git push origin <tag>` |

### `scripts/tag-release.sh` — GPL v3 §6 traceability

After component uploads succeed (`make upload-lamp upload-lelamp upload-web ...`), this script anchors the resulting OTA metadata snapshot to a single git tag:

```bash
make tag-release v0.0.8
# → curl https://cdn.autonomous.ai/lamp/ota/metadata.json
# → git tag -a v0.0.8 -F - (annotation = pretty-printed metadata JSON)
# → git push origin v0.0.8
```

Buyers run `lamp-server --version` on the device — value comes from `git describe --tags --always --dirty` at build time (`Makefile:VERSION`), so it resolves to the closest tag. They then open the public repo (`github.com/autonomous-ai/lamp`), find the matching tag, read the annotation for the exact `lamp`/`lelamp`/`web`/`bootstrap` versions baked at release time, and checkout that commit for corresponding source.

Guards in the script: refuses if tag already exists locally or on remote, refuses if metadata fetch fails or JSON is invalid (`set -euo pipefail` + `jq .`). Overrides via env vars: `OTA_METADATA_URL` (default: `https://cdn.autonomous.ai/lamp/ota/metadata.json`), `TAG_REMOTE` (default: `origin`).

---

## 8. Build & Version Injection

### Go Binaries (ldflags)

```makefile
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

LDFLAGS_BOOTSTRAP := -X go-lamp.autonomous.ai/bootstrap/config.BootstrapVersion=$(VERSION)
LDFLAGS_LAMP    := -X go-lamp.autonomous.ai/server/config.LampVersion=$(VERSION)

build-bootstrap:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_BOOTSTRAP)" -o bootstrap-server ./cmd/bootstrap

build-lamp:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_LAMP)" -o lamp-server ./cmd/lamp
```

### LeLamp (VERSION file)

LeLamp version is a plain text `VERSION` file in the package root. Read by bootstrap via file or `python -m lelamp --version`.

---

## 9. Key Differences from Lobster

| Aspect | Lobster (original) | AI Lamp (this project) |
|---|---|---|
| Components | 4 (lamp, bootstrap, web, openclaw) | **5** (+ lelamp) |
| OTA keys | lamp, bootstrap, web, openclaw | + **lelamp** |
| Setup stages | 7 (stages -1 to 4) | **8** (+ stage 2b: LeLamp) |
| Systemd services | 4 | **5** (+ lamp-lelamp.service) |
| Python runtime | None | **LeLamp** at /opt/lelamp/ with venv |
| Hardware bridge | N/A | Lamp HTTP → LeLamp HTTP (localhost proxy) |
| SPI usage | LED only | LED + **Display (GC9A01)** |

---

## 10. Open Questions

- [x] **LeLamp source**: Mono-repo. Driver code copied from `humancomputerlab/lelamp_runtime` into `lelamp/`, with LiveKit/OpenAI removed and HTTP API + DisplayService added. Upstream tracked manually via `lelamp/UPSTREAM.md`.
- [x] **LeLamp HTTP port**: `5001` (Lamp Server is `5000`).
- [x] **Bridge protocol**: Simple HTTP proxy. LeLamp runs FastAPI on `127.0.0.1:5001`, Lamp Server proxies from port 5000.
- [x] **Python version**: Pinned to Python 3.12+ (`pyproject.toml`, `.python-version`, `setup.sh` uses `uv sync --python 3.12`).
- [x] **LeLamp packaging**: On-device venv via `uv sync --python 3.12 --extra hardware` at `/opt/lelamp/.venv`. OTA preserves venv, reinstalls only on requirements change.
- [x] **Display driver**: DisplayService (GC9A01) is part of LeLamp Python at `lelamp/service/display/display_service.py`.
- [x] **LeLamp config**: Environment variable-based (`config.py` reads from env vars). `.env` file support via `python-dotenv`. No separate config file needed.

---

*This document describes the full OTA and bootstrap system. For architecture decisions, see [architecture-decision.md](architecture-decision.md). For product vision, see [product-vision.md](product-vision.md).*
