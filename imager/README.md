# imager — device golden image builder

Produces SD card images that boot OrangePi 4 Pro (or Raspberry Pi 4B / 5) directly
into the device AP/hotspot setup wizard. Flash, insert, power on — no
`scripts/provision/setup.sh` needed on the target.

`DEVICE_TYPE` and `OTA_METADATA_URL` are **required** (no default) — one device
class per golden image; preflight fails if either is unset.

```bash
make build DEVICE_TYPE=lamp OTA_METADATA_URL=…                   # → output/golden-opi-lamp.img.xz (OrangePi, default)
make TARGET=rpi DEVICE_TYPE=lamp OTA_METADATA_URL=… build        # → output/golden-lamp.img (Raspberry Pi 5)
make TARGET=rpi RPI_MODEL=4 DEVICE_TYPE=lamp OTA_METADATA_URL=… build  # → output/golden-lamp.img (Raspberry Pi 4B)
make upload                             # push image + release note to GCS, versioned (auto-cleans output/ on success)
make upload-source                      # mirror input/orangepi.7z → GCS (one-time)
make clean                              # nuclear: wipe output/ entirely (input/ kept)
make clean-all                          # wipe both output/ and input/
```

Lần đầu build ~25–40 phút (download vendor base image, qemu-arm64 chroot apt
install + LeLamp uv sync, OTA backend bake, xz compress). Re-runs nhanh hơn
nhiều — `input/orangepi.7z` được cache, chỉ Phase 3+ re-run.

## Targets

| Board | TARGET | RPI_MODEL | Builder | Output | Status |
|-------|--------|-----------|---------|--------|--------|
| **OrangePi 4 Pro v2 (Allwinner A733)** *default* | `opi` | — | `build-orangepi.sh` | `output/golden-opi-<type>.img.xz` | **working** (verified 2026-05-27) |
| Raspberry Pi 5 | `rpi` | `5` (default) | `build.sh` | `output/golden-<type>.img` | working |
| Raspberry Pi 4B | `rpi` | `4` | `build.sh` | `output/golden-<type>.img` | code-only, untested on HW |

`make TARGET=rpi build` cho Pi 5 path. `make TARGET=rpi RPI_MODEL=4 build` cho Pi 4B. No-arg `make build` mặc định OrangePi.

> The build script always writes a fixed name (`golden-opi.img.xz` / `golden.img`);
> `make build` then renames it to the device-typed `golden[-opi]-<type>.img[.xz]` shown
> above, so 3 device types don't clobber each other. Phase logs below show the raw
> script name (pre-rename).

## OrangePi build flow

```
Phase 0  Source .7z fetch
         - gdown 'https://drive.google.com/uc?id=$OPI_FILE_ID' → input/orangepi.7z (cached, 734 MB)
         - If Google Drive rate-limits the file (common — "Too many users have viewed
           or downloaded this file recently"), see Troubleshooting → manual download.
Phase 1  Extract + expand
         - 7z e → /work/Orangepi4pro_*.img (~3.8 GB raw ext4)
         - cp → /output/golden-opi.img, truncate to OUT_IMG_SIZE (default 14 GB)
         - growpart loop0 1 → resize partition table to fill expanded image
         - losetup --offset / --sizelimit on partition byte range (bypass kernel
           partition device nodes; Docker on Mac lacks udev so /dev/loopXpY
           never appears)
         - e2fsck + resize2fs → filesystem fills the resized partition
         - mount /mnt/opi
Phase 2  chroot qemu-arm64:
         - apt install (production-matched list: hostapd, dnsmasq, nginx, avahi,
           bluez, pulseaudio, alsa-utils, chromium, xvfb, …)
         - Node.js 22 from NodeSource + `openclaw@$OPENCLAW_VERSION` npm global
         - openclaw onboard --skip-health (creates /root/.openclaw scaffolding)
         - Discord + Slack plugins baked in: openclaw@$OPENCLAW_VERSION
         - uv (Python pkg mgr for LeLamp)
         - systemd units: lamp, bootstrap, hal, openclaw
         - helper scripts /usr/local/bin/{device-ap-mode, device-sta-mode, connect-wifi,
           software-update} synced from scripts/ in this repo
         - configs: hostapd, dnsmasq, dhcpcd, full prod nginx (CSP + WS + captive-portal
           + local-only block for /api/system/exec), PulseAudio (WebRTC AEC + anon socket),
           udev PULSE_IGNORE for I2S codecs, /etc/asound.conf (lamp_speaker / lamp_micro1
           for ES8389 sndi2s4)
         - SPI3 overlay baked into orangepiEnv.txt for WS2812 LED ring (/dev/spidev3.0)
         - mask orangepi-firstrun-config.service (vendor wizard would conflict)
Phase 3  OTA bake from metadata.json:
         - bootstrap-server + os-server binaries
         - LeLamp Python app + `uv sync --python 3.12 --extra hardware`
           (with webrtcvad pkg_resources shim for Py 3.12+ where the symbol was removed)
         - Web UI to /usr/share/nginx/html/setup
         - Claude Desktop Buddy BLE plugin (optional, if `claude-desktop-buddy.url` in metadata)
         - Writes /tmp/ota-versions.env (web/os-server/bootstrap/hal/buddy versions baked in)
Phase 4  resize-once.service installed
         - oneshot, first-boot only, self-destructing
         - growpart + resize2fs to fill the actual SD card (image is 14 GB, SD likely larger)
Phase 5  Finalize
         - Read /tmp/ota-versions.env back out of the image
         - Write /output/manifest-opi.json (build_timestamp, OTA versions, source sha256, …)
         - Unmount, detach loop devices
         - xz -9 --threads=0 /output/golden-opi.img → golden-opi.img.xz (~190 MB)
```

**Typical sizes**: source .7z = 734 MB, extracted .img = 3.8 GB, expanded image
14 GB, final compressed `.img.xz` ≈ 190 MB (xz handles the empty space very
efficiently).

### First boot on the device

1. U-Boot reads `/boot/orangepiEnv.txt` (left intact from vendor image) →
   mounts `/dev/mmcblk1p1` as ext4 root.
2. `resize-once.service` runs once: `growpart /dev/mmcblk1 1 && resize2fs
   /dev/mmcblk1p1` → ext4 fills the real SD size. Service self-disables +
   removes itself. Re-flash will reinstall it.
3. Operator runs `sudo device-ap-mode` (or the bootstrap-server triggers it
   when no STA association after a timeout).
4. SSID becomes `<device_type>-xxxx` (e.g. `lamp-a1b2`) where `xxxx` = last 4 hex
   chars of the ethernet MAC and `<device_type>` is baked at build time. The board
   has no device-tree serial; `device-ap-mode`'s fallback chain
   (`/proc/device-tree/serial-number` → `/proc/cpuinfo Serial` → `eth0`/`end0` MAC)
   lands on MAC for OPi.
5. mDNS hostname `<device_type>-<xxxx>.local` published by `avahi-daemon`.
6. Connect phone/laptop to AP → http://192.168.100.1/ → setup wizard collects
   API keys + home WiFi → `device-sta-mode` switches → device reachable via
   `<device_type>-xxxx.local` on the home LAN.

## Flashing an SD card

**Recommended: Raspberry Pi Imager** (works for both OPi and RPi images)

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Choose OS → "Use custom" → select `output/golden-opi-<type>.img.xz` (or `golden-<type>.img` for RPi)
3. Choose Storage → select your SD card
4. Click Write — Imager handles decompression and verification automatically

> OPi image is `.img.xz` (compressed). Imager decompresses on the fly — no need to extract first.

The `make sd-card-flash DISK=N` Makefile target is also available as a fallback
if Imager is not an option (requires knowing the disk number via `make sd-card-list`).

## Configuration knobs

All env vars; override at the `make` call.

| Variable | Default | Effect |
|----------|---------|--------|
| `TARGET` | `opi` | `opi`, `rpi` — picks builder script + output filename |
| `RPI_MODEL` | `5` | RPi only: `5` = Pi 5 (Trixie/Debian 13), `4` = Pi 4B (Bookworm/Debian 12) |
| `OUT_IMG_SIZE` | `14G` | OPi only: partition size after expansion. |
| `OPI_FILE_ID` | `1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn` | Google Drive file ID for `Orangepi4pro_1.0.6_debian_bookworm_server_*.7z`. Bump when the dev team uploads a new vendor release. |
| `OPENCLAW_VERSION` | `2026.5.27` | npm package version pin. Bump as OpenClaw releases. |
| `OTA_METADATA_URL` | **(required, no default)** | OTA metadata source. Must be passed at build time (`make build OTA_METADATA_URL=...`); build fails in preflight if unset. Baked into the image's `/root/config/bootstrap.json`. |
| `DEVICE_TYPE` | **(required, no default)** | Device class baked into the image (one `DEVICE_TYPE` = one golden image). Selects `devices/<type>/{DEVICE,SOUL}.md`, the staged device profile, and the network identity (`<type>-xxxx` AP SSID + mDNS hostname). Build fails in preflight if unset. |
| `AP_BAND` | `2.4` | `2.4` or `5` — hostapd hw_mode. |
| `AP_CHANNEL` | `6` (2.4 GHz) / `36` (5 GHz) | hostapd channel |
| `COUNTRY_CODE` | `US` | Regulatory domain for wpa_supplicant + hostapd |
| `GCS_BUCKET` | `s3-autonomous-upgrade-3` | (Makefile) target bucket for `make upload` |
| `GCS_PATH` | `os/imager/output` | (Makefile) path inside the bucket |
| `GCS_LEDGER` | `os/imager/RELEASES.md` | (Makefile) path for cumulative append-only release ledger |

## Upload to GCS

After a successful `make build`, push the image + release notes to the team
bucket. Account needs Storage Object Creator on `gs://s3-autonomous-upgrade-3`.

```bash
gcloud auth login                  # if token expired
make upload                        # 3 uploads: image, per-release note, updated RELEASES.md ledger
```

Versioning: `golden-<target>-<type>-<UTC-timestamp>-<git-short-sha>.img.xz`

`make upload` does:

1. Computes sha256 + size of `output/golden-opi-<type>.img.xz`.
2. Reads `output/manifest-opi.json` (written by Phase 5) for OTA versions
   baked in. Degrades gracefully if missing.
3. Generates `output/golden-opi-<type>-<version>.release.txt`.
4. Uploads image + release note to GCS.
5. Prepends new entry to cumulative `RELEASES.md` ledger (newest-first).
6. **Auto-cleans** `output/` after all uploads succeed. Skip with `KEEP_OUTPUT=1 make upload`.

### Mirror the source .7z to GCS (one-shot)

```bash
make upload-source                 # → gs://$GCS_BUCKET/os/imager/source/Orangepi4pro_*.7z
```

## File layout

```
imager/
├── Dockerfile             — Ubuntu 24.04 + qemu-aarch64-static + p7zip-full + e2fsprogs + cloud-guest-utils + gdown
├── Makefile               — build / flash / upload / upload-source targets, dispatches on TARGET
├── build-orangepi.sh      — OrangePi 4 Pro builder (default; ~1200 lines)
├── build.sh               — Raspberry Pi 4B / 5 builder (~2100 lines)
├── lib/                   — RESERVED for shared chroot stages (see lib/README.md)
├── input/                 — cached source images (.7z / .img.xz). gitignored.
├── output/                — built golden images + release notes + manifests. gitignored.
├── .gitignore             — input/ output/ work/
└── README.md              — this file
```

## Sanity checks after first flash

SSH in (`ssh system@<device_type>-xxxx.local`, e.g. `lamp-a1b2.local`, password `12345`) and verify:

```bash
systemctl is-enabled lamp hal openclaw avahi-daemon
ls /usr/local/bin/{os-server,bootstrap-server,device-ap-mode,connect-wifi,software-update}
ls /opt/hal/.venv/bin/uvicorn       # LeLamp uv sync succeeded
openclaw --version                       # OpenClaw npm global installed
ls /etc/asound.conf /etc/udev/rules.d/91-pulseaudio-hal-ignore.rules
findmnt /                                # ext4 root, expanded to full SD
systemctl is-enabled resize-once 2>&1 | grep -q "not found" && echo OK_resize-once-self-destructed
```

> Note: the vendor OrangePi image also has an `orangepi` user (password `orangepi`) that is not removed by the builder. Both `system/12345` and `orangepi/orangepi` work for SSH.

## Maintenance — Pi vs OPi drift

Chroot stage logic is currently **duplicated** between `build.sh` (RPi) and
`build-orangepi.sh` (OPi). When you change something inside either script's
chroot block (apt list, helper script, systemd unit, nginx config), mirror
in the other if the change is board-agnostic.

Planned refactor: extract a sourceable `imager/lib/chroot-stages.sh` that
both builders source. See `lib/README.md` for the design sketch.

## Source image notes (OPi)

The base `.7z` is the **vendor "user-built" image**:

```
BOARD=orangepi4pro
BOARDFAMILY=sun60iw2
DISTRIBUTION_CODENAME=bookworm
VERSION=1.0.6
IMAGE_TYPE=user-built
```

## Troubleshooting

### Docker on Mac: losetup / udev quirks

`--privileged + losetup` is flaky on Docker Desktop. If `losetup: cannot find
unused loop device` appears, switch to OrbStack (`brew install orbstack && orb
start`) or run the build on a Linux host.

### SSL `docker-credential-desktop` not in PATH

```bash
jq 'del(.credsStore)' ~/.docker/config.json > /tmp/c && mv /tmp/c ~/.docker/config.json
```

### gdown: Google Drive "Quota exceeded" / "Too many users"

**Quickest fix — "Add shortcut to My Drive" trick:**

1. Open https://drive.google.com/drive/folders/1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS
2. Right-click `Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z` → **Add shortcut to Drive** → My Drive
3. Download from your copy (new file ID, fresh quota)
4. Drop at `imager/input/orangepi.7z` and re-run `make build`

**Permanent fix:** mirror to GCS once via `make upload-source`, then update
`build-orangepi.sh` to pull from GCS instead of GDrive (TODO — wire `OPI_SOURCE_URL`).

### `make upload` warning about parallel composite uploads

The "ERROR: Cannot check..." message is a **warning**, not a fatal error — upload still works.
To silence it: `gcloud config set storage/parallel_composite_upload_enabled False`

### Final image fails to boot OPi

Check the U-Boot bootloader region survived:

```bash
xz -dc output/golden-opi-<type>.img.xz | head -c 16M | hexdump -C | head -20
```

Expected: non-zero bytes near offsets 0x2000 (SPL header) and 0x20000 (U-Boot proper).

## Recent changes

**2026-06-08** — Raspberry Pi 4B support:
- `RPI_MODEL` env var added to `build.sh` (`4` = Pi 4B Bookworm, `5` = Pi 5 Trixie, default 5)
- Pi 4 uses separate cache file `input/raspios-bookworm.img.xz` to avoid collision with Pi 5 cache
- `stage_rpi5_wifi_stability` (IPv6 disable) now guarded by `if [ "$RPI_MODEL" = "5" ]`

**2026-06-06** — nginx local-only block for `/api/system/exec`:
- Blocks external access to exec endpoint, loopback-only

**2026-06-05** — helper scripts synced from repo:
- `device-ap-mode`, `device-sta-mode`, sysctl fixes now sourced from `scripts/` in this repo
  (previously copied verbatim from a production device)

**2026-06-04** — `software-update` full implementation:
- Replaced stub with full implementation covering all components:
  lamp, bootstrap, hal, openclaw, web, claude-desktop-buddy
- Discord + Slack plugins reinstalled on openclaw update

**2026-06-03** — SPI3 overlay baked in:
- `spi3-cs0-cs1-spidev` overlay baked into `orangepiEnv.txt` so `/dev/spidev3.0`
  (WS2812B LED ring) is available immediately after flash — no manual overlay step needed

**2026-06-02** — Discord + Slack plugins:
- `@openclaw/discord` and `@openclaw/slack` plugins installed at build time (matching `OPENCLAW_VERSION`)

**2026-05-27** — OPi builder verified end-to-end on Mac/Docker:
- `OUT_IMG_SIZE` default raised to `14G`
- Loop device handling switched to `losetup --offset/--sizelimit`
- Phase 5 writes `output/manifest-opi.json`
- New Makefile targets: `make upload`, `make upload-source`
