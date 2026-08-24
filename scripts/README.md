# Scripts

Provisioning, release, and maintenance tooling. Run release targets via `make`
from the repo root — the Makefile targets are thin wrappers around these scripts.

| Folder | Role |
|--------|------|
| [`provision/`](provision/) | Runs **on the device**: first-boot setup (AP/STA, nginx, systemd units, HAL, agent runtime) and standalone component installers |
| [`release/`](release/) | Runs **on a developer machine**: builds/zips each component, uploads to GCS, patches OTA `metadata.json`, promotes fleet rollouts |
| [`maintenance/`](maintenance/) | Runs **on an existing device**: one-off idempotent patches for fleets provisioned before a fix landed in `setup.sh` |
| [`imager/`](imager/) | Runs **on a build machine**: builds the flashable SBC golden image (OrangePi/RPi) — own Dockerfile + Makefile, uploads to GCS `os/imager/` |

## provision/

- **`install.sh`** — the curl-pipe entry point (`DEVICE_TYPE=lamp curl -fsSL .../os/install.sh | sudo bash`). Sets the default `OTA_METADATA_URL`, requires `DEVICE_TYPE` (no default — the OS image is device-agnostic), downloads the latest `setup.sh` from the CDN and runs it. Use it on a fresh board or when re-provisioning; the imager bakes a first-boot path that ends up here too.
- **`setup.sh`** — the full provisioner (published to the CDN by `make upload-setup`). Stages, in order: locale fix → apt prerequisites (hostapd, dnsmasq, nginx, dhcpcd, avahi, Node 22, resolv.conf repair) → IPv6 disable → SPI enable → fetch OTA metadata + seed `metadata_url` into `/root/config/bootstrap.json` → install `bootstrap-server` + `os-server` binaries and systemd units → HAL into `/opt/hal` (uv + Python 3.12 venv, PulseAudio AEC config, `hal.service`) → device profile (`ROBOT.md`/`SOUL.md`/`SAFETY.md` into `$DEVICES_DIR/<type>`) → Claude Desktop Buddy → OpenClaw (npm global install, gateway service, capability-gated skill seed) → nginx (setup web SPA, `/api` proxy, WS routes, security headers) → AP setup (hostapd + dnsmasq, SSID/hostname `<device_type>-<serial-suffix>`, avahi mDNS, and writes the on-device helpers `device-ap-mode`, `device-sta-mode`, `connect-wifi`, `software-update`). Ends in AP mode and reboots.

  Each stage runs through a `run_stage` wrapper that catches failures and keeps
  going: even if app stages (hal/buddy/openclaw) fail, `stage_ap` is still
  reached so the device stays reachable over WiFi for re-provisioning. Failed
  stages are listed in the final summary.
- **`software-update`** — the on-device OTA updater installed at `/usr/local/bin/software-update`, and the **single source of truth** for it. Every fleet gets this one file: the imagers `install -m 0755` it from `/input` (staged by `imager/Makefile` before `docker run`, because the Docker build context is `imager/` and cannot reach this directory), and `release/upload-setup.sh` inlines it into `setup.sh` between the `>>> BEGIN software-update (generated)` markers at publish time. The `setup.sh` copy in git is a placeholder that exits 1 — assembly happens at release, never in git, so it cannot go stale. Edit it here and nowhere else.
- **`setup-ap.sh`** — AP-only subset of `setup.sh` (hostapd + dnsmasq + the mode-switch helpers), no app installs. Published via `make upload-setup-ap`.
- **`install-openclaw.sh`** — standalone OpenClaw (re)install on an already-provisioned device.
- **`setup-claude-desktop-buddy.sh`** — standalone Buddy plugin install; requires `setup.sh` to have run (reads `metadata_url` from `/root/config/bootstrap.json`).

## release/ — the OTA pipeline

All scripts source **`ota-config.sh`** (single edit point: `GCS_BUCKET=s3-autonomous-upgrade-3`, `BUCKET_PREFIX=os`, repo paths; env overrides win).

| Script | Component / artifact | GCS destination (`gs://$GCS_BUCKET/…`) |
|--------|----------------------|----------------------------------------|
| `upload-os-server.sh` | `os-server` binary (zip) | `os/ota/os-server/<ver>.zip` |
| `upload-bootstrap.sh` | `bootstrap-server` binary (zip) | `os/ota/bootstrap/<ver>.zip` |
| `upload-hal.sh` | `hal` source tree (zip) | `os/ota/hal/<ver>.zip` |
| `upload-web.sh` | web `dist/` bundle (zip) | `os/ota/web/<ver>.zip` |
| `upload-device.sh <type>` | one device profile: DEVICE/SOUL/SAFETY.md + rootfs overlay (zip) | `os/ota/devices/<type>/<ver>.zip` |
| `upload-claude-desktop-buddy.sh` | `buddy-plugin` linux/arm64 (zip) | `os/ota/claude-desktop-buddy/<ver>.zip` |
| `upload-autonomous-buddy.sh` | macOS companion DMG | `os/ota/autonomous-buddy/<ver>.dmg` |
| `upload-autonomous-chat.sh` | chat-bridge hook binary (zip) | `os/ota/autonomous-chat/<ver>.zip` |
| `upload-twitch-irc.sh` | Twitch IRC hook binary (zip) | `os/ota/twitch-irc/<ver>.zip` |
| `upload-skills.sh` | per-skill deterministic zips (content-hash version, `.skill-hashes` cache skips unchanged). Also runs from CI on every merge to main that touches `skills/` — [`publish-skills.yml`](../.github/workflows/publish-skills.yml) | `os/skills/<name>.zip` |
| `upload-hooks.sh` | OpenClaw hooks (`HOOK.md`, `handler.ts`) | `os/hooks/<hook>/` |
| `upload-openclaw.sh <ver>` | metadata-only: bumps `openclaw.version` (device installs from npm) | `os/ota/metadata.json` only |
| `upload-setup.sh` / `upload-setup-ap.sh` | the provision scripts themselves | `os/setup.sh`, `os/setup-ap.sh` |
| `upload-cad.sh` | CAD file → Mega.nz share link (no make target, no metadata) | — |
| `promote-ota.sh` | bumps a component's `min_version` rollout floor | `os/ota/metadata.json` |
| `tag-release.sh <tag>` | annotated git tag embedding the current metadata snapshot (GPL v3 §6 traceability) | — |

**Metadata mechanics.** Every upload script: fetch `gs://…/os/ota/metadata.json`
→ patch **only its own component key** (`{version, url, updated_at}`, preserving
any existing `min_version`) → upload the zip and metadata back with
`Cache-Control: no-cache, no-store, must-revalidate` so devices never see stale
data. `upload-device.sh` merges into the nested `devices.<type>` entry so
independent device teams never clobber each other.

**VERSION auto-bump.** Each upload script auto-increments the patch component of
its version file (`system/VERSION_OS_SERVER`, `hal/VERSION_HAL`,
`system/VERSION_WEB`, `robots/<type>/VERSION`, …) and injects it into the
build (Go binaries via `make … VERSION=x.y.z` → ldflags). Never hand-edit these
for a release, and never commit built binaries.

**Upload ≠ rollout.** The device's `bootstrap` worker (`system/bootstrap/`)
polls `metadata.json` (default every 5m) and auto-applies a component only when
its current version is strictly **below the floor** = `min_version` (falling
back to `version` when unset). Uploading bumps `version` but preserves
`min_version`, so a new build sits in staging: anyone can pull it manually with
`software-update <component>` over SSH (which ignores `min_version`), but the
fleet only moves when you run `promote-ota.sh` / `make promote-<component>` —
that is the explicit rollout decision.

![OTA release flow](ota-flow.svg)

## maintenance/

| Script | Fleet patch |
|--------|-------------|
| `add-network.sh` | Bench helper: adds a hardcoded WiFi network via `nmcli` |
| `migrate-openclaw-path.sh` | Rewrites `openclaw.service` from `/root/openclaw` → `/root/.openclaw`, restarts |
| `patch-nginx-gw.sh` | Adds the `upstream openclaw` + `/gw/` proxy location to existing nginx config |
| `patch-nginx-hw-timeout.sh` | Adds 300s proxy timeouts to `/hw/` (long-running enroll endpoints 504'd at 60s) |
| `patch-security.sh` | One-shot hardening: HAL bind to 127.0.0.1, loopback-only `/hw/` and `/api/system/exec`, etc. Idempotent; paste into `/monitor#cli` after OTA-ing hal + os-server |

## Conventions

- Run releases via make from the repo root: `make upload-os-server`, `upload-bootstrap`, `upload-hal`, `upload-web`, `upload-skills`, `upload-hooks`, `upload-claude-desktop-buddy`, `upload-autonomous-buddy`, `upload-autonomous-chat`, `upload-twitch-irc`, `upload-setup`, `upload-setup-ap`, `upload-device <type>`, `upload-openclaw <version>`, `upload-all`, `promote-<component>` / `promote-device DT=<type>` (optional `V=<ver>`), `tag-release <tag>`.
- `upload-all` bundles the core components but deliberately excludes `upload-openclaw` and `upload-device` (explicit, per-type decisions).
- Version files are owned by the upload scripts — do not hand-edit them for releases; do not commit binary artifacts (versions are injected at build time via ldflags).
