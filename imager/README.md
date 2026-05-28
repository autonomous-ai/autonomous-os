# imager — Lumi golden image builder

Produces SD card images that boot OrangePi 4 Pro (or Raspberry Pi 5) directly
into the Lumi AP/hotspot setup wizard. Flash, insert, power on — no
`scripts/setup.sh` needed on the target.

```bash
make build                        # → output/golden-opi.img.xz (default: OrangePi)
make sd-card-list                 # find your SD card disk number
make sd-card-flash DISK=N         # decompresses on the fly via `xz | dd`
make upload                       # push image + release note to GCS, versioned (auto-cleans output/ on success)
make upload-source                # mirror input/orangepi.7z → GCS (one-time)
make clean                        # nuclear: wipe output/ entirely (input/ kept)
make clean-all                    # wipe both output/ and input/
```

Lần đầu build ~25–40 phút (download vendor base image, qemu-arm64 chroot apt
install + LeLamp uv sync, OTA backend bake, xz compress). Re-runs nhanh hơn
nhiều — `input/orangepi.7z` được cache, chỉ Phase 3+ re-run.

## Targets

| Board | TARGET | Builder | Output | Status |
|-------|--------|---------|--------|--------|
| **OrangePi 4 Pro v2 (Allwinner A733)** *default* | `opi` | `build-orangepi.sh` | `output/golden-opi.img.xz` | **working** (verified built 2026-05-27) |
| Raspberry Pi 5 (RPi OS Trixie) | `rpi` | `build.sh` | `output/golden.img` | working |

`make TARGET=rpi build` cho Pi 5 path. No-arg `make build` mặc định OrangePi.

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
         - uv (Python pkg mgr for LeLamp)
         - systemd units: lamp, bootstrap, lumi-lelamp, lumi-wifi-power-save, openclaw
         - helper scripts /usr/local/bin/{device-ap-mode, device-sta-mode, connect-wifi, software-update}
           (verbatim copy from production OPi @ 100.111.149.69)
         - configs: hostapd, dnsmasq, dhcpcd, full prod nginx (CSP + WS + captive-portal),
           PulseAudio (WebRTC AEC + anon socket), udev PULSE_IGNORE for I2S codecs,
           /etc/asound.conf (lamp_speaker / lamp_micro1 for ES8389 sndi2s4)
         - mask orangepi-firstrun-config.service (vendor wizard would conflict)
Phase 3  OTA bake from metadata.json:
         - bootstrap-server + lamp-server binaries
         - LeLamp Python app + `uv sync --python 3.12 --extra hardware`
           (with webrtcvad pkg_resources shim for Py 3.12+ where the symbol was removed)
         - Web UI to /usr/share/nginx/html/setup
         - Claude Desktop Buddy BLE plugin (optional, if `claude-desktop-buddy.url` in metadata)
         - Writes /tmp/ota-versions.env (web/lamp/bootstrap/lelamp/buddy versions baked in)
Phase 4  lumi-resize-once.service installed
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
2. `lumi-resize-once.service` runs once: `growpart /dev/mmcblk1 1 && resize2fs
   /dev/mmcblk1p1` → ext4 fills the real SD size. Service self-disables +
   removes itself. Re-flash will reinstall it.
3. Operator runs `sudo device-ap-mode` (or the bootstrap-server triggers it
   when no STA association after a timeout).
4. SSID becomes `Lamp-XXXX` where `XXXX` = last 4 hex chars of the ethernet
   MAC. The board has no device-tree serial; `device-ap-mode`'s fallback
   chain (`/proc/device-tree/serial-number` → `/proc/cpuinfo Serial` →
   `eth0`/`end0` MAC) lands on MAC for OPi.
5. mDNS hostname `lamp-<xxxx>.local` published by `avahi-daemon`.
6. Connect phone/laptop to AP → http://192.168.100.1/ → setup wizard collects
   API keys + home WiFi → `device-sta-mode` switches → device reachable via
   `lamp-xxxx.local` on the home LAN.

## Configuration knobs

All env vars; override at the `make` call.

| Variable | Default | Effect |
|----------|---------|--------|
| `TARGET` | `opi` | `opi` or `rpi` — picks builder script + output filename |
| `OUT_IMG_SIZE` | `14G` | Partition size after expansion. ext4 fills this; xz compresses unused space away. SD card must be ≥ this (lumi-resize-once will expand further on first boot if SD is larger). |
| `OPI_FILE_ID` | `1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn` | Google Drive file ID for `Orangepi4pro_1.0.6_debian_bookworm_server_*.7z`. Bump when the dev team uploads a new vendor release. |
| `OPENCLAW_VERSION` | `2026.5.7` | npm package version pin. Bump as OpenClaw releases. |
| `OTA_METADATA_URL` | `https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/ota/metadata.json` | Backend binaries source. Used by Phase 3 to download `lamp-server`, `bootstrap-server`, `lelamp`, `web`, optional `claude-desktop-buddy`. |
| `AP_BAND` | `2.4` | `2.4` or `5` — hostapd hw_mode. 5 GHz needs chip + regulatory support. |
| `AP_CHANNEL` | `6` (2.4 GHz) / `36` (5 GHz) | hostapd channel |
| `COUNTRY_CODE` | `US` | Regulatory domain for wpa_supplicant + hostapd |
| `GCS_BUCKET` | `s3-autonomous-upgrade-3` | (Makefile) target bucket for `make upload` / `make upload-source` |
| `GCS_PATH` | `lamp/imager/output` | (Makefile) path inside the bucket for built images + per-release notes |
| `GCS_LEDGER` | `lamp/imager/RELEASES.md` | (Makefile) path for cumulative append-only release ledger |

Example — rebuild against a new vendor release uploaded to a different file ID:

```bash
rm input/orangepi.7z output/golden-opi.img output/golden-opi.img.xz
OPI_FILE_ID=NEW_FILE_ID_HERE make build
```

## Upload to GCS

After a successful `make build`, push the image + release notes to the team
bucket. Account needs Storage Object Creator on `gs://s3-autonomous-upgrade-3`.

```bash
gcloud auth login                  # if token expired
make upload                        # 3 uploads: image, per-release note, updated RELEASES.md ledger
```

Versioning: `golden-<target>-<UTC-timestamp>-<git-short-sha>.img.xz`

Example output filename: `golden-opi-20260527-043849-2e4aa75b.img.xz`

`make upload` does:

1. Computes sha256 + size of `output/golden-opi.img.xz`.
2. Reads `output/manifest-opi.json` (written by Phase 5) for OTA versions
   baked in. Degrades gracefully if missing — note will say "OTA versions not
   captured this build".
3. Generates `output/golden-opi-<version>.release.txt` — Markdown release
   note with image metadata + OTA versions + source image identity.
4. Uploads:
   - `gs://$GCS_BUCKET/$GCS_PATH/golden-opi-<version>.img.xz`
   - `gs://$GCS_BUCKET/$GCS_PATH/golden-opi-<version>.release.txt`
5. Downloads existing `gs://$GCS_BUCKET/$GCS_LEDGER` (or creates if missing),
   **prepends** the new release note, re-uploads. Newest-first chronology.
6. **Auto-cleans** `output/golden-opi.img.xz`, the release `.txt`, and
   `manifest-opi.json` after all uploads succeed (saves ~1 GB local disk).
   Skip the cleanup with `KEEP_OUTPUT=1 make upload` if you want to keep the
   artifacts locally (e.g. to flash an SD card right after).

Per-release note format (sample):

```markdown
## golden-opi-20260527-043849-2e4aa75b

- Target:        opi
- Built (UTC):   2026-05-27T04:38:50Z
- Git branch:    main
- Git commit:    2e4aa75bbc489b1c7c0b9da186c6ed013e82760a
- Image size:    192M (192094208 bytes)
- sha256:        242617a805a70dcef79f060c1d6a3deef233ec02828d59d02fb83904472d96bd
- Output name:   golden-opi-20260527-043849-2e4aa75b.img.xz
- Download:      https://storage.googleapis.com/.../golden-opi-20260527-043849-2e4aa75b.img.xz

OTA versions baked at build time:
- web:        1.2.3
- lamp:       0.0.620
- bootstrap:  0.0.10
- lelamp:     1.0.5
- claude-desktop-buddy: 1.0.2

Source image:
- file_id:  1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn
- name:     Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z
- sha256:   …

Build config:
- openclaw: 2026.5.7
- out_size: 14G
- ota_url:  https://storage.googleapis.com/.../metadata.json
```

### Mirror the source .7z to GCS (one-shot)

Permanent escape from Google Drive rate-limits. Once mirrored, future builders
can pull from GCS instead of Drive (TODO: wire `OPI_SOURCE_URL` env into
build-orangepi.sh to prefer the GCS mirror).

```bash
make upload-source                 # → gs://$GCS_BUCKET/lamp/imager/source/Orangepi4pro_*.7z
```

## File layout

```
imager/
├── Dockerfile             — Ubuntu 24.04 + qemu-aarch64-static + p7zip-full + e2fsprogs + cloud-guest-utils + gdown
├── Makefile               — build / flash / upload / upload-source targets, dispatches on TARGET
├── build-orangepi.sh      — OrangePi 4 Pro builder (default; ~750 lines)
├── build.sh               — Raspberry Pi 5 builder (~1990 lines)
├── lib/                   — RESERVED for shared chroot stages (see lib/README.md)
├── input/                 — cached source images (.7z / .img.xz). gitignored.
├── output/                — built golden images + release notes + manifests. gitignored.
├── .gitignore             — input/ output/ work/
└── README.md              — this file
```

## Sanity checks after first flash

SSH in (`ssh system@lamp-xxxx.local`, password `12345` until rotated by the
setup wizard) and verify:

```bash
systemctl is-enabled lamp lumi-lelamp lumi-wifi-power-save openclaw avahi-daemon
ls /usr/local/bin/{lamp-server,bootstrap-server,device-ap-mode,connect-wifi,software-update}
ls /opt/lelamp/.venv/bin/uvicorn       # LeLamp uv sync succeeded
openclaw --version                       # OpenClaw npm global installed
ls /etc/asound.conf /etc/udev/rules.d/91-pulseaudio-lelamp-ignore.rules
findmnt /                                # ext4 root, expanded to full SD
systemctl is-enabled lumi-resize-once 2>&1 | grep -q "not found" && echo OK_resize-once-self-destructed
```

## Maintenance — Pi vs OPi drift

Chroot stage logic is currently **duplicated** between `build.sh` (Pi) and
`build-orangepi.sh` (OPi). When you change something inside either script's
chroot block (apt list, helper script, systemd unit, nginx config), mirror
in the other if the change is board-agnostic.

Planned refactor: extract a sourceable `imager/lib/chroot-stages.sh` that
both builders source. Blocked on first verified OPi golden image on real
hardware (so the refactor has a working baseline). See `lib/README.md` for
the design sketch.

## Source image notes (OPi)

The base `.7z` is the **vendor "user-built" image** referenced by
`/etc/orangepi-release` on the production device:

```
BOARD=orangepi4pro
BOARDFAMILY=sun60iw2
BUILD_REPOSITORY_URL=https://github.com/orangepi-xunlong/orangepi-build
BUILD_REPOSITORY_COMMIT=70abbec-dirty
DISTRIBUTION_CODENAME=bookworm
VERSION=1.0.6
IMAGE_TYPE=user-built
```

`-dirty` suffix = dev team applied local patches to `orangepi-build` before
generating the image. Patches not public. If we ever need to rebuild from
source, ask the dev team for the patch set + exact `./build.sh` invocation.

The Drive folder also has variants we don't use:

- `Orangepi4pro_1.0.6_debian_bookworm_desktop_xfce_*.7z` (Xfce desktop — bloated)
- `Orangepi4pro_1.0.6_debian_bullseye_server_*.7z` (Debian 11, EOL)
- `Orangepi4pro_1.0.6_debian_bullseye_desktop_xfce_*.7z`

We pin **bookworm_server** because that's what production runs (confirmed via
`/etc/os-release` on 100.111.149.69).

### Why not re-compress the source to shrink it?

Tested 2026-05-27 — extracting the vendor `.7z` and re-compressing the inner
`.img` with `xz -9 --threads=0` shrinks 735 MB → 638 MB (~13 % reduction).
Not worth a refactor:

- Source download is one-time-per-machine (gdown caches `input/orangepi.7z`).
- 13 % saves ~100 MB on a one-shot upload to the GCS mirror.
- Vendor `.7z` (LZMA solid) is already near-optimal for already-allocated
  ext4 data. The extra gain comes from xz seeing zero runs the 7z solid
  block obscures.
- A bigger win (~50 %) would need `zerofree` to zero unused ext4 blocks
  before xz — adds a mount + tool dep + 5–10 min build time. Skipped.

Keep the canonical source as `.7z`. `make upload-source` mirrors it as-is.

## Troubleshooting

### Docker on Mac: losetup / udev quirks

`--privileged + losetup` is flaky on Docker Desktop. If `losetup: cannot find
unused loop device` appears, switch to OrbStack (`brew install orbstack && orb
start`) or run the build on a Linux host.

Docker Desktop also ships without udev, so `partprobe` doesn't auto-create
`/dev/loopXp1` after partition resize. `build-orangepi.sh` works around this
by attaching a **second** loop device with `--offset/--sizelimit` directly at
the partition byte range (bypasses kernel partition-device-node creation
entirely). No action needed — already handled.

### SSL `docker-credential-desktop` not in PATH

Known Docker Desktop config issue. Fix once:

```bash
jq 'del(.credsStore)' ~/.docker/config.json > /tmp/c && mv /tmp/c ~/.docker/config.json
```

### gdown: Google Drive "Quota exceeded" / "Too many users"

Google Drive rate-limits popular files at the **file level** (not IP). When
this hits, gdown + raw curl both fail. Browser session sometimes works
because it authenticates to your Google account.

**Quickest fix — "Add shortcut to My Drive" trick:**

1. Open https://drive.google.com/drive/folders/1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS
2. Right-click `Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z` → **Add shortcut to Drive** → My Drive
3. Open My Drive → download the file from your copy (new file ID, fresh quota)
4. Drop the downloaded file at `imager/input/orangepi.7z` (rename if needed)
5. Re-run `make build` — script sees the cached file and skips Phase 0 download

**Permanent fix:** mirror to GCS once via `make upload-source`, then update
`build-orangepi.sh` to pull from `gs://s3-autonomous-upgrade-3/lamp/imager/source/`
instead of GDrive (TODO — wire `OPI_SOURCE_URL`).

### `make upload` warning about parallel composite uploads

```
ERROR: Cannot check if the destination bucket is compatible for running
parallel composite uploads as the user does not permission to perform GET
operation on the bucket. The operation will be performed without parallel
composite upload feature and hence might perform relatively slower.
```

Misleading "ERROR" prefix — this is a **warning**. Means the account doesn't
have `storage.buckets.get` IAM permission, so gcloud falls back from
4-stream parallel composite upload to single-stream. Upload still works, just
slower (single-stream ~10 MB/s for our 190 MB images = ~20 sec, acceptable).

To silence the warning:

```bash
gcloud config set storage/parallel_composite_upload_enabled False
```

### Final image fails to boot OPi

Check the U-Boot bootloader region (first ~16 MB of the image) survived. The
build script never touches these sectors — they should be byte-identical to
the source `.7z`'s `.img`.

```bash
xz -dc output/golden-opi.img.xz | head -c 16M | hexdump -C | head -20
```

Expected: non-zero bytes near offsets 0x2000 (SPL header) and 0x20000 (U-Boot
proper). If everything's zero past offset 0x200, the bootloader was clobbered.

## Recent changes

**2026-05-27** — OPi builder verified end-to-end on Mac/Docker:

- `OUT_IMG_SIZE` default raised to `14G` (6G was too small for LeLamp uv sync —
  torchvision + ultralytics + polars + livekit fill ~4 GB).
- Loop device handling switched from `partprobe + /dev/loopXp1` to `losetup
  --offset/--sizelimit` (Docker Desktop on Mac has no udev, so the partition
  device nodes never appeared even with `--partscan`).
- Phase 5 now writes `output/manifest-opi.json` with OTA versions baked in,
  source `.7z` sha256, build timestamp, `OPENCLAW_VERSION` pin, etc.
- New Makefile targets: `make upload` (image + per-release note + cumulative
  `RELEASES.md` ledger to GCS, version-stamped), `make upload-source` (mirror
  vendor `.7z` to GCS to escape GDrive rate limits permanently).
- gdown invocation: switched from `--id` (removed in gdown 5.x) and `--fuzzy`
  (not in apt-shipped gdown) to plain URL form `https://drive.google.com/uc?id=$ID`
  which works on all gdown versions.
- Clear manual-fallback error when GDrive rate-limits.

**2026-05-26** — Full rewrite of the OPi builder:

- Switched base image from Armbian (wrong assumption) to vendor Orange Pi
  Bookworm 1.0.6 .7z (matches `/etc/orangepi-release` on production OPi).
- Dropped Btrfs `@`/`@factory` subvolume scheme — production runs plain ext4
  single-partition. Replaced @factory factory-reset with `lumi-resize-once`
  first-boot expand.
- Bootloader handling: now relies on vendor image's pre-baked U-Boot in raw
  sectors. No more `armbianEnv.txt` manipulation; `orangepiEnv.txt` is left
  intact.
- chroot stages now mirror production OPi exactly: ES8389 ALSA aliases,
  vendor service masks (`orangepi-firstrun-config.service`), OpenClaw with
  the production env block (XDG_*, PUPPETEER paths), production nginx config
  with CSP + WebSocket proxies + captive-portal returns.
- `make build` is one command end-to-end.

**Earlier (Pi 5 only)** — ported from `scripts/setup.sh`: openresolv +
`name_servers="1.1.1.1 8.8.8.8"` fallback (Pi-only — OPi vendor image doesn't
use openresolv), avahi `lamp-<suffix>.local` mDNS, PulseAudio udev ignore for
`sndi2s4` + `wm8960soundcard`, webrtcvad Py3.12+ patch, MAC-based SSID
fallback for non-Pi boards in `device-ap-mode`, Pi Imager `wpa.conf` cleanup,
`AP_BAND=5` knob, `stage_buddy` (Claude Desktop Buddy BLE plugin) gated on
OTA `claude-desktop-buddy.url`.
