# Bootstrap & OTA System

## 1. Overview

The device runs **5 software components** on a supported board (Raspberry Pi 4, Pi 5, or OrangePi). All components are installed via an initial setup script and kept up-to-date by a background OTA worker.

| Component | Type | Install Method | Service Name | Install Path |
|---|---|---|---|---|
| **OS Server** | Go binary (ARM64) | Download zip from OTA | `os-server.service` | `/usr/local/bin/os-server` |
| **Bootstrap Server** | Go binary (ARM64) | Download zip from OTA | `bootstrap.service` | `/usr/local/bin/bootstrap-server` |
| **Web (Setup SPA)** | React/Vite bundle | Download zip from OTA | nginx serves static | `/usr/share/nginx/html/setup/` |
| **OpenClaw** | Node.js package | `npm install -g` | `openclaw.service` | Global npm |
| **HAL** | Python package | Download zip from OTA | `hal.service` | `/opt/hal/` |

### Architecture Diagram

```
                    ┌──────────────────────────────┐
                    │   OTA Metadata (GCS JSON)     │
                    │                                │
                    │  os-server: {version, url}     │
                    │  bootstrap: {version, url}     │
                    │  web:       {version, url}     │
                    │  openclaw:  {version}          │
                    │  hal:       {version, url}     │
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

> In the URLs below, `{BUCKET}` and `{PREFIX}` are the bucket + path namespace:
> `GCS_BUCKET` (default `s3-autonomous-upgrade-3`) and `BUCKET_PREFIX` (default
> `os`), both set in `scripts/release/ota-config.sh`. Upload scripts read them from there;
> on-device consumers derive the same paths from the provisioned `ota_metadata_url`.

**URL**: `https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json`

The published document retains legacy component entries at the top level and
adds a `signed` `autonomous-ota/v1` Ed25519 envelope. `signed.payload` is
base64-encoded JSON and `signed.signature.value` signs the decoded payload
bytes; the 32-byte base64 public key is provisioned locally in `bootstrap.json`,
never taken from the feed. Its decoded payload has this shape:

```json
{
  "os-server": {
    "version": "1.2.3",
    "min_version": "1.2.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/os-server/1.2.3/os-server-1.2.3.zip",
    "sha256": "<64 lowercase hex characters>"
  },
  "bootstrap": {
    "version": "1.0.5",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/bootstrap/1.0.5/bootstrap-1.0.5.zip"
  },
  "web": {
    "version": "0.9.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/web/0.9.0/setup-0.9.0.zip"
  },
  "openclaw": {
    "version": "2026.6.10"
  },
  "hal": {
    "version": "1.0.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/hal/1.0.0/hal-1.0.0.zip"
  }
}
```

### Generate an OTA signing key

Run this once on the release operator's machine:

```bash
make ota-keygen
```

It writes an Ed25519 private PEM outside the repository by default, at
`~/.config/autonomous/ota/ota-YYYYMMDD.pem`, and prints three export lines. Keep
`OTA_SIGNING_PRIVATE_KEY` private; use it with `OTA_SIGNING_KEY_ID` for
`make upload-*`. Provision the printed `OTA_SIGNING_PUBLIC_KEY` into new devices.
Override the path or ID when needed:

```bash
make ota-keygen OTA_SIGNING_KEY_DIR=/secure/ota-keys OTA_SIGNING_KEY_ID=prod-2026-08
```

**Domain types** — `domain/ota.go`:

```go
const (
    OTAKeyOSServer  = "os-server"
    OTAKeyBootstrap = "bootstrap"
    OTAKeyWeb       = "web"
    OTAKeyOpenClaw  = "openclaw"
    // OTAKeyLeLamp's value is "hal" — the HAL OTA metadata key
)

type OTAMetadata map[string]OTAComponent

type OTAComponent struct {
    Version    string `json:"version"`
    MinVersion string `json:"min_version,omitempty"`
    URL        string `json:"url,omitempty"`
    SHA256     string `json:"sha256,omitempty"`
}
```

### Staged rollout — `version` vs `min_version`

`version` is the latest build; `min_version` is the **approved floor** the
automatic worker rolls the fleet up to. They decouple "published" from
"auto-pushed":

- **Auto OTA (bootstrap worker)** updates a device only when its current version
  is **strictly below `min_version`**. If `min_version` is absent it defaults to
  `version` (so the worker simply tracks the latest — legacy behavior).
- **Manual `software-update <key>`** (run over SSH) ignores `min_version` and
  always installs `version` — for testing a build on a few devices first.

Workflow:

1. `scripts/release/upload-<component>.sh` bumps `version` and **preserves**
   `min_version` (skills/hooks have no `min_version`). The fleet does **not**
   move — only `version` changed.
2. SSH into a device, run `software-update <key>` → it pulls `version`. Test it.
3. Happy? `make promote-<component> [V=<version>]` (e.g. `make promote-hal`,
   `make promote-os-server V=1.4.0`, `make promote-device DT=lamp`) raises
   `min_version` (default: up to `version`). Bootstrap now auto-updates every
   device below the new floor on its next check.

Version comparison is numeric per dotted segment (`bootstrap.compareVersions`):
`2026.6.10 > 2026.5.9`; pre-release/build suffixes are ignored; an empty or
unparseable current version sorts lowest (always below any floor → updates).

---

## 3. Initial Setup (`scripts/provision/setup.sh`)

One-time provisioning script run on a fresh Raspberry Pi. Executes stages sequentially.

**Quick install from CDN:**
```bash
curl -fsSL https://cdn.autonomous.ai/os/install.sh | sudo bash
```

### Stage Overview

| Stage | Name | Description |
|---|---|---|
| -1 | Locale fix | Ensure `C.UTF-8` encoding |
| 0 | Prerequisites | System packages, Node.js 22 |
| 0a | WiFi stability | Disable IPv6, WiFi power saving (RPi5) |
| 0b | Enable SPI | For WS2812 LED driver |
| 1 | Fetch OTA metadata | Download metadata.json, extract versions and URLs |
| 1b | Install binaries | Download + install os-server, bootstrap-server, create systemd services |
| 2 | Install OpenClaw | `npm install -g openclaw`, create config, create systemd service |
| **2b** | **Install HAL** | **Download + install HAL Python runtime, create systemd service** (NEW) |
| 3 | Setup nginx | Download web bundle, configure reverse proxy + captive portal |
| 4 | Setup WiFi AP | Configure hostapd, dnsmasq, start AP mode for provisioning |

### Stage 2b: Install HAL Runtime (NEW)

This stage installs the HAL Python runtime that provides hardware drivers for servos, LEDs, and audio.

```bash
stage_install_hal() {
    echo "=== Stage 2b: Install HAL Runtime ==="

    # 1. Install Python dependencies
    apt-get install -y python3 python3-pip python3-venv

    # 2. Create install directory
    mkdir -p /opt/hal

    # 3. Download from OTA metadata
    HAL_URL=$(echo "$OTA_JSON" | jq -r '.hal.url')
    HAL_VERSION=$(echo "$OTA_JSON" | jq -r '.hal.version')

    curl -fsSL "$HAL_URL" -o /tmp/hal.zip
    unzip -o /tmp/hal.zip -d /opt/hal/
    rm /tmp/hal.zip

    # 4. Install Python dependencies in venv
    python3 -m venv /opt/hal/venv
    /opt/hal/venv/bin/pip install -r /opt/hal/requirements.txt

    # 5. Create systemd service
    cat > /etc/systemd/system/hal.service << 'UNIT'
[Unit]
Description=HAL Python Runtime — Hardware Drivers
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hal
ExecStart=/opt/hal/venv/bin/python -m hal.server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable hal.service
    systemctl start hal.service

    echo "HAL $HAL_VERSION installed at /opt/hal/"
}
```

### Systemd Services Created by Setup

| Service | ExecStart | Port | Notes |
|---|---|---|---|
| `os-server.service` | `/usr/local/bin/os-server` | 5000 | Main HTTP API, always running |
| `bootstrap.service` | `/usr/local/bin/bootstrap-server` | 8080 | OTA worker, polls for updates. Exposes `POST /force-check` to trigger an immediate OTA check and `GET /security` for the OTA trust posture |
| `openclaw.service` | `xvfb-run ... openclaw gateway run` | — | AI brain, memory limit 1500M |
| `hal.service` | `uvicorn hal.server:app --host 127.0.0.1 --port 5001` | 5001 | Hardware drivers (servo, LED, camera, audio) |
| nginx | `nginx` | 80 | Setup SPA + reverse proxy (`/api/` → OS Server 5000, `/hw/` → HAL 5001) |

### Service Dependency Order

```
boot
  → os-server.service   (system layer, LED boot animation)
  → bootstrap.service   (starts polling for updates)
  → hal.service          (hardware drivers ready)
  → openclaw.service    (AI brain, connects to os-server via HTTP)
  → nginx               (web UI for setup)
```

---

## 4. Bootstrap OTA Worker

### Config (`/root/config/bootstrap.json`)

The bootstrap worker keeps its own config file, separate from os-server's
`config.json`, but it lives in the same `/root/config/` directory.

```json
{
  "httpPort": 8080,
  "metadata_url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json",
  "signing_public_key": "<base64 32-byte Ed25519 public key>",
  "rollback_versions": {"os-server": "1.2.3"},
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
```

`metadata_url` has **no compiled-in default** — it is a per-deployment value
seeded into this file by `setup.sh` (`stage_ota_metadata`) at provisioning. The
file is loaded as an overlay on operational defaults (`httpPort` 8080,
`poll_interval` 5m, `state_file`), so a partial file (just `metadata_url`) works
and a missing file yields defaults with an empty URL.

When supplied, `setup.sh` and golden-image builders persist
`OTA_SIGNING_PUBLIC_KEY` as `signing_public_key`. Bootstrap then verifies the
envelope before reading any component; the provisioned updater verifies it
again and hashes every ZIP before extraction. When a release operator supplies
`OTA_SIGNING_PRIVATE_KEY` and `OTA_SIGNING_KEY_ID`, release writers re-sign the
envelope; without them they retain the legacy unsigned format. For the two self-contained
binaries, each update retains `/root/bootstrap/rollback/<component>.previous`;
run `software-update rollback os-server` or `software-update rollback bootstrap`
to restore it. The updater records the version it removed in
`rollback_versions`; bootstrap then skips only that exact target, so the failed
release is not reinstalled on the next poll. Publishing a different version
automatically resumes OTA for that component. Rollback itself does not need the
metadata URL or network access.

Directory installs have the same recovery contract. Before a web update, the
updater stops nginx, swaps the fully unpacked staged bundle into place, and
retains the previous bundle at `/root/bootstrap/rollback/web.previous` together
with nginx's prior active/inactive state. It then requires `index.html`, a valid
`nginx -t`, and—when nginx had been running—a successful loopback `GET /`.
Failure automatically restores the saved bundle and service state. An operator
can also run `software-update rollback web`; the rejected version is recorded in
`rollback_versions` just like a binary rollback.

For a device profile, the updater stages the ZIP, stops only the `os-server` and
`hal` services that were active, and retains the old profile at
`/root/bootstrap/rollback/device.previous`. It also snapshots the exact files
covered by the old or new `rootfs/` overlay in `device.previous.rootfs`; rollback
therefore restores overwritten files and removes files introduced only by the
rejected profile. Local `/opt/hal/.env` tuning remains preserved. The profile
must contain `ROBOT.md`; each service that was active must recover and answer
its loopback health endpoint. A failed check restores the known-good profile and
its prior service state automatically. Use `software-update rollback device` for
an operator rollback; the rejected device-profile version is then blocked.

Devices without `signing_public_key` deliberately remain in legacy mode: they
read the top-level entries and emit a warning rather than failing OTA. This is a
compatibility bridge, not a trust guarantee; pin the public key to opt in to
verified OTA.

### OTA security posture (operator-visible)

The warning above is a log line, so a device silently stuck in legacy mode is
invisible to a fleet operator. Both processes expose the posture as data:

| Endpoint | Served by | Notes |
|----------|-----------|-------|
| `GET http://127.0.0.1:8080/security` | `bootstrap-server` | Source of truth: the worker holds the pinned key and performs verification. Loopback only. |
| `GET /api/system/ota-security` | `os-server` | Proxies the above verbatim inside the standard `{"status":1,"data":…}` wrapper. `502` when the bootstrap worker is unreachable. |

```json
{
  "mode": "verified",
  "metadata_format": "autonomous-ota/v1",
  "key_fingerprint": "9f2c1ab30d4e5f60",
  "artifact_checksums": true,
  "last_metadata_fetch": {
    "at": "2026-08-24T09:12:03Z",
    "verified": true
  }
}
```

- `mode` is `verified` when `signing_public_key` is provisioned, `legacy`
  otherwise. This is the single field to alert on across the fleet.
- `key_fingerprint` is the first 16 hex characters of the SHA-256 of the pinned
  key, so operators can confirm *which* key a device trusts (and spot devices
  left on a rotated-out key) without the endpoint echoing key material. Absent
  in legacy mode.
- `artifact_checksums` reports whether per-component SHA-256 digests are
  enforced. It follows `mode`: digests are only meaningful when the metadata
  carrying them is authentic.
- `last_metadata_fetch` is the outcome of the most recent fetch, including
  transport failures (`error` is set, `verified` is `false`). It is absent until
  the first fetch completes after a restart, so a device that has never reached
  its feed is distinguishable from one whose feed is healthy.

### Signed-only cutover

Signed metadata is published as a superset: the payload's component entries stay
at the top level for already-deployed workers, with the authenticated document
under `signed`. That compatibility copy is what keeps legacy mode exploitable,
so it has to be retired. The rollout:

1. **Publish signed** (done). Release writers add the `signed` envelope whenever
   `OTA_SIGNING_PRIVATE_KEY` and `OTA_SIGNING_KEY_ID` are set. Devices ignore it
   until a key is pinned.
2. **Provision keys.** New devices get `OTA_SIGNING_PUBLIC_KEY` at setup; the
   existing fleet gets it written into `/root/config/bootstrap.json`. No
   redeploy is needed — the worker reads the key on the next config load.
3. **Confirm the fleet.** Poll `GET /api/system/ota-security` and require
   `mode == "verified"` with the expected `key_fingerprint` on every device.
   This step is the gate: do not proceed while any device reports `legacy`.
4. **Cut over.** Publish with `OTA_METADATA_SIGNED_ONLY=1`, which drops the
   top-level copy so the document contains only `signed`. An unmigrated device
   then stops updating (loudly) instead of updating from an unauthenticated
   source — the intended failure direction.

Rollback for step 4 is publishing once more without the flag; devices already in
verified mode are unaffected either way, since they read only `signed`.

**Wait-then-retry when unprovisioned**: if `metadata_url` is empty (device not
set up yet), `Serve()` does not start the poll loop or healthcheck server. It logs
`waiting for metadata_url in bootstrap config` and reloads
`/root/config/bootstrap.json` every 30s until a URL appears, then proceeds.
Nothing is silent.

### State (`/root/bootstrap/state.json`)

Tracks last known installed version per component:

```json
{
  "components": {
    "os-server": "1.2.3",
    "bootstrap": "1.0.5",
    "web": "0.9.0",
    "openclaw": "2026.6.10",
    "hal": "1.0.0"
  }
}
```

State saves are atomic: bootstrap writes and syncs a temporary file in the same
directory before renaming it into place. If an older state file is malformed
(for example, after an interrupted legacy write), bootstrap preserves it as
`state.json.corrupt-<timestamp>`, logs a warning, and resumes with empty state
instead of failing to start OTA polling.

### Core Loop (`bootstrap/bootstrap.go`)

```
checkLoop():
  1. checkOnce() immediately on startup
  2. Sleep poll_interval (default 5m)
  3. Repeat

checkOnce():
  1. Fetch OTA metadata JSON
  2. For each key [os-server, bootstrap, web, hal]:
     → reconcile(key, metadata[key])
  NOTE: OpenClaw OTA is temporarily disabled (reconcileOpenClawFromNpm commented out)
  3. Save state

reconcile(key, target):
  1. Detect current installed version
  2. If current == "" AND the component is not installed → skip silently
  3. floor = target.min_version (default target.version if empty)
  4. If current >= floor → sync state, return (at/above approved floor)
  5. If current < floor →
     a. Set LED orange breathing (OTA in progress)
     b. applyUpdate(key, target)   # installs target.version via software-update
     c. Success → green flash | Failure → red pulse
```

> Manual `software-update <key>` over SSH does NOT pass through `reconcile` — it
> installs `target.version` directly, bypassing the `min_version` floor.

#### Step 2 — components this device does not have

Metadata lists every published component; no device runs all of them. A Reachy
Mini has no `claude-desktop-buddy`; a device on a non-OpenClaw runtime has no
`openclaw`. For those `detectVersion` returns `""`, which sorts below every
floor — so without this gate the worker reports "update available", speaks the
update cue, lights the OTA LED and fails the install **every poll, forever**.

`componentInstalled(key)` asks a coarser question than `detectVersion`: *is the
artifact here at all*, not *which version*. A component that is present but
unreadable still counts as installed, so OTA self-repair keeps working (an
`os-server` whose `--version` is broken is still updated); only a genuinely
absent one is skipped.

| Component | Counts as installed when |
|---|---|
| `bootstrap` | always (it is the running worker, so it can self-update) |
| `os-server` | `os-server` is on `$PATH` |
| `openclaw` | `openclaw` is on `$PATH` |
| `web` | `/usr/share/nginx/html/setup/` exists |
| `hal` | `/opt/hal/` exists |
| `claude-desktop-buddy` | `/opt/claude-desktop-buddy/` exists |
| `device` | `$DEVICES_DIR/<type>/` exists (default `/opt/devices`) |

### OTA LED Feedback

Bootstrap uses `lib/hal` to show update status on LEDs. See [status-led.md](../robots/lamp/docs/status-led.md) for full spec.

| Phase | LED |
|-------|-----|
| Downloading + installing | Orange breathing `(255, 140, 0)` |
| Success | Green flash `(0, 255, 80)`, then restore the user-selected LED look or the ambient resting look when none exists |
| Failure | Red pulse `(255, 30, 30)` |

### Version Detection Per Component

| Component | How to Detect Current Version |
|---|---|
| `os-server` | Run `os-server --version`, parse output |
| `bootstrap` | Compiled-in constant `config.BootstrapVersion` (ldflags) |
| `web` | Read file `/usr/share/nginx/html/setup/VERSION` |
| `openclaw` | Run `openclaw --version`, extract semver with regex |
| `hal` | Run `/opt/hal/venv/bin/python -m hal --version` OR read `/opt/hal/VERSION` file |

### Update Application Per Component

| Component | Update Steps |
|---|---|
| `os-server` | Run `software-update os-server` (blocks up to 10 min) |
| `bootstrap` | Spawn detached `software-update bootstrap` (self-update, survives restart) |
| `web` | Run `software-update web` |
| `openclaw` | ~~Run `npm install -g openclaw@{version}` → `systemctl restart openclaw`~~ (temporarily disabled) |
| `hal` | Run `software-update hal` → `systemctl restart hal` |

---

## 5. Software Update Script (`/usr/local/bin/software-update`)

Bash script installed by setup.sh (and baked into the image by the imager).
Called by bootstrap worker to apply updates.

It reads the OTA metadata URL from `metadata_url` in `/root/config/bootstrap.json`
(an explicit `OTA_METADATA_URL` env var overrides it for manual/debug runs), and
aborts with an error if neither is set — no compiled-in URL.

### HAL Case

```bash
"hal")
    # Preserve the complete runtime and prior service state.
    systemctl stop hal
    mv /opt/hal /root/bootstrap/rollback/hal.previous

    # Build the candidate in a sibling directory. .env, venv, and uv cache
    # are copied from the retained runtime before uv sync.
    unzip -q "$ZIP" -d /opt/.hal.new
    cp -a /root/bootstrap/rollback/hal.previous/{.env,.venv,.uv-cache} /opt/.hal.new/
    (cd /opt/.hal.new && uv sync --python 3.12 --extra hardware)
    mv /opt/.hal.new /opt/hal

    systemctl restart hal
    curl -fsS http://127.0.0.1:5001/health
    # Any staging or health failure restores hal.previous and its old state.
    # Operators can also run: software-update rollback hal
    ;;
```

---

## 6. HAL Runtime — Source & Integration

### Source Strategy: Copy + Track Manually

HAL runtime code is **copied** from the upstream open-source project into this mono-repo, then modified heavily.

**Why copy, not submodule/subtree:**
- We need to **remove** LiveKit/OpenAI integration (replaced by OpenClaw)
- We need to **add** HTTP API server (Flask/FastAPI) for OS Server to bridge to
- We need to **add** DisplayService (GC9A01 eyes + info, not in original)
- We need to **modify** services to work with our architecture
- The overlap is drivers only (~30-40% of their code), the rest is rewritten

**Upstream tracking:**
- Source: `https://github.com/humancomputerlab/lelamp_runtime`
- Record the upstream commit hash in `hal/UPSTREAM.md` when copying
- Periodically check upstream for driver-level fixes (servo protocol, LED timing, etc.)
- Cherry-pick relevant driver changes manually
- Ignore upstream AI/LiveKit changes (we replaced that entirely)

**Implementation steps:**
1. Clone `humancomputerlab/lelamp_runtime` to a temp directory
2. Copy driver code (`services/motors.py`, `services/rgb.py`, `services/audio.py`, `services/service_base.py`) into `hal/services/`
3. Remove all LiveKit, OpenAI, and conversation code
4. Add `hal/server.py` — new HTTP API server (FastAPI)
5. Add `hal/services/display.py` — new DisplayService for GC9A01
6. Create `hal/UPSTREAM.md` with source commit hash and date
7. Test on device with actual hardware

### Mono-repo Layout

HAL lives inside this repo as a Python subfolder alongside Go and TypeScript:

```
autonomous/
├── system/          # Go code (forked from lobster)
│   ├── cmd/              # Go entrypoints
│   ├── server/           # Go HTTP layer
│   ├── internal/         # Go business logic
│   ├── bootstrap/        # Go OTA worker
│   └── domain/           # Shared structs
├── system/web/      # TypeScript/React SPA (copied from lobster, renamed intern→lamp)
├── hal/               # Python hardware drivers (NEW)
│   ├── __init__.py       # Package init, exposes __version__
│   ├── server.py         # HTTP API server (FastAPI) — NEW, not from upstream
│   ├── system/
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

### HAL OTA Package

For OTA distribution, HAL is zipped from the `hal/` folder:

```
hal-{version}.zip
├── hal/                  # Full Python package
├── requirements.txt
└── VERSION
```

### HAL HTTP API (FastAPI on port 5001)

The HAL Python runtime exposes its own HTTP API on `127.0.0.1:5001`. OS Server (Go, port 5000) bridges OpenClaw skill requests to this API. Nginx proxies `/hw/*` for same-machine callers only — external clients receive 403. Swagger UI at `/hw/docs` is not accessible from LAN.

```
OpenClaw LLM → curl 127.0.0.1:5000/api/servo → OS Server → http://127.0.0.1:5001/servo → HAL Python → Hardware
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

### `scripts/release/upload-hal.sh` (NEW)

```bash
#!/usr/bin/env bash
# Upload HAL runtime to OTA

set -euo pipefail

VERSION_FILE="VERSION_HAL"
BUCKET="s3-autonomous-upgrade-3"
OTA_PATH="os/ota/hal"
METADATA_PATH="os/ota/metadata.json"

# Auto-increment patch version
CURRENT=$(cat "$VERSION_FILE" 2>/dev/null || echo "0.0.0")
MAJOR=$(echo "$CURRENT" | cut -d. -f1)
MINOR=$(echo "$CURRENT" | cut -d. -f2)
PATCH=$(echo "$CURRENT" | cut -d. -f3)
NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"
echo "$NEW_VERSION" > "$VERSION_FILE"

# Package
echo "Packaging HAL $NEW_VERSION..."
cd path/to/hal-source
echo "$NEW_VERSION" > VERSION
zip -r "/tmp/hal-${NEW_VERSION}.zip" hal/ requirements.txt VERSION

# Upload zip
gsutil cp "/tmp/hal-${NEW_VERSION}.zip" \
    "gs://${BUCKET}/${OTA_PATH}/${NEW_VERSION}/hal-${NEW_VERSION}.zip"

# Update metadata
DOWNLOAD_URL="https://storage.googleapis.com/${BUCKET}/${OTA_PATH}/${NEW_VERSION}/hal-${NEW_VERSION}.zip"
gsutil cp "gs://${BUCKET}/${METADATA_PATH}" /tmp/metadata.json
jq --arg v "$NEW_VERSION" --arg u "$DOWNLOAD_URL" \
    '.hal = {"version": $v, "url": $u}' /tmp/metadata.json > /tmp/metadata-updated.json
gsutil cp /tmp/metadata-updated.json "gs://${BUCKET}/${METADATA_PATH}"

echo "HAL $NEW_VERSION published."
```

### All Upload Scripts

| Script | Component | Pattern |
|---|---|---|
| `scripts/release/upload-os-server.sh` | OS Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-bootstrap.sh` | Bootstrap Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-web.sh` | Web SPA bundle | Build → zip → GCS → update metadata |
| `scripts/release/upload-hal.sh` | HAL Python runtime (NEW) | Package → zip → GCS → update metadata |
| `scripts/release/upload-setup.sh` | Setup script | Upload to GCS |
| `scripts/release/upload-setup-ap.sh` | AP setup script | Upload to GCS |
| `scripts/release/upload-skills.sh` | OpenClaw skill files | Upload to GCS |
| `scripts/provision/install.sh` | CDN install shortcut | `curl ... \| sudo bash` on Pi |
| `scripts/release/tag-release.sh` | Git release tag with OTA metadata snapshot | Fetch metadata.json → annotated tag → `git push origin <tag>` |

### `scripts/release/tag-release.sh` — GPL v3 §6 traceability

After component uploads succeed (`make upload-os-server upload-hal upload-web ...`), this script anchors the resulting OTA metadata snapshot to a single git tag:

```bash
make tag-release v0.0.8
# → curl https://cdn.autonomous.ai/os/ota/metadata.json
# → git tag -a v0.0.8 -F - (annotation = pretty-printed metadata JSON)
# → git push origin v0.0.8
```

Buyers run `os-server --version` on the device — value comes from `git describe --tags --always --dirty` at build time (`Makefile:VERSION`), so it resolves to the closest tag. They then open the public repo (`github.com/autonomous-ai/autonomous-os`), find the matching tag, read the annotation for the exact `os-server`/`hal`/`web`/`bootstrap` versions baked at release time, and checkout that commit for corresponding source.

Guards in the script: refuses if tag already exists locally or on remote, refuses if metadata fetch fails or JSON is invalid (`set -euo pipefail` + `jq .`). Overrides via env vars: `OTA_METADATA_URL` (default: `https://cdn.autonomous.ai/os/ota/metadata.json`), `TAG_REMOTE` (default: `origin`).

---

## 8. Build & Version Injection

### Go Binaries (ldflags)

```makefile
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Go symbol stays config.LampVersion (internal, not part of deploy identity).
LDFLAGS_OS   := -X go.autonomous.ai/os/system/server/config.OSVersion=$(VERSION)
LDFLAGS_BOOT := -X go.autonomous.ai/os/system/bootstrap/config.BootstrapVersion=$(VERSION)

os-build-bootstrap:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap

os-build:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_OS)" -o os-server ./cmd/os-server
```

### HAL (VERSION file)

HAL version is a plain text `VERSION` file in the package root. Read by bootstrap via file or `python -m hal --version`.

---

## 9. Key Differences from Lobster

| Aspect | Lobster (original) | Autonomous (this project) |
|---|---|---|
| Components | 4 (lamp, bootstrap, web, openclaw) | **5** (+ hal) |
| OTA keys | lamp, bootstrap, web, openclaw | + **hal** |
| Setup stages | 7 (stages -1 to 4) | **8** (+ stage 2b: HAL) |
| Systemd services | 4 | **5** (+ hal.service) |
| Python runtime | None | **HAL** at /opt/hal/ with venv |
| Hardware bridge | N/A | OS server HTTP → HAL HTTP (localhost proxy) |
| SPI usage | LED only | LED + **Display (GC9A01)** |

---

## 10. Open Questions

- [x] **HAL source**: Mono-repo. Driver code copied from `humancomputerlab/lelamp_runtime` into `hal/`, with LiveKit/OpenAI removed and HTTP API + DisplayService added. Upstream tracked manually via `hal/UPSTREAM.md`.
- [x] **HAL HTTP port**: `5001` (OS Server is `5000`).
- [x] **Bridge protocol**: Simple HTTP proxy. HAL runs FastAPI on `127.0.0.1:5001`, OS Server proxies from port 5000.
- [x] **Python version**: Pinned to Python 3.12+ (`pyproject.toml`, `.python-version`, `setup.sh` uses `uv sync --python 3.12`).
- [x] **HAL packaging**: On-device venv via `uv sync --python 3.12 --extra hardware` at `/opt/hal/.venv`. OTA preserves venv, reinstalls only on requirements change.
- [x] **Display driver**: DisplayService (GC9A01) is part of HAL Python at `hal/service/display/display_service.py`.
- [x] **HAL config**: Environment variable-based (`config.py` reads from env vars). `.env` file support via `python-dotenv`. No separate config file needed.

---

*This document describes the full OTA and bootstrap system. For architecture decisions, see [architecture-decision.md](../robots/lamp/docs/architecture-decision.md). For product vision, see [product-vision.md](../robots/lamp/docs/product-vision.md).*
