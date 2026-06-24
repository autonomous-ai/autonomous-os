#!/bin/bash
# =============================================================================
# build-orangepi-dev.sh — MINIMAL DEV image for OrangePi 4 Pro v2 (A733/sun60iw2)
# =============================================================================
#
# Stock vendor image + a thin "dev layer" ONLY. No app stack (no lamp-server,
# bootstrap, lelamp, openclaw, nginx, OTA bake). Built for hardware bring-up &
# testing — fast (~5-10 min, no apt / no qemu chroot).
#
# The dev layer turns ON every hardware/development interface:
#   - all GPIO-header bus overlays (spi / i2c / uart / pwm / can) — AUTO-DISCOVERED
#     from the image's own overlay dir so only kernel-shipped names get enabled;
#     SPI3 (WS2812 LED ring) always kept.
#   - permissive dev-node access (spidev / i2c / gpiochip / pwm / ttyS = 0666)
#   - SSH on: password login for the orangepi user (root login disabled)
#   - lamp-ac82 hardware config baked in: /etc/asound.conf (4 audio devices) +
#     the 5 production udev rules (device-servo / device-camera symlinks, stable
#     USB audio card names, T527 SoC perms for dma_heap/cedar/renderD128,
#     PulseAudio ignore, wifi power-save off, Realtek USB-net fix).
#   - first-boot SD-fill resize (self-destructing), same as the golden builder.
#
# Source image: Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z
# Run via Makefile:  make TARGET=opi-dev build   (Docker, --privileged).
# =============================================================================

set -euo pipefail

# ── config ───────────────────────────────────────────────────────────────────
OUT_IMG_SIZE="${DEV_IMG_SIZE:-5G}"          # modest expand; first boot fills the SD
DEV_ROOT_PW="${DEV_ROOT_PW:-12345}"         # root SSH password
DEV_OVERLAYS="${DEV_OVERLAYS:-auto}"        # 'auto' = discover all bus overlays, or a space-separated list
LED_OVERLAY="spi3-cs0-cs1-spidev"           # WS2812 LED ring — always enabled
CONFIG_SRC="${CONFIG_SRC:-}"                # dir with etc/asound.conf + etc/udev/rules.d/*.rules; empty = built-in lamp-ac82
DEV_LABEL="${DEV_LABEL:-}"                  # output suffix, e.g. 'intern' → golden-opi-dev-intern.img.xz
XZ_MEMLIMIT="${XZ_MEMLIMIT:-4GiB}"          # xz compression RAM budget; higher → more threads → faster (was capping at ~25% of RAM = 1 thread)
STOCK="${STOCK:-}"                          # 1 = pure STOCK base: skip the dev layer entirely (device-agnostic; software team flashes it then runs setup.sh)

OPI_FILE_ID="${OPI_FILE_ID:-1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn}"
OPI_FILE_NAME="Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147"

MNT="/mnt/opi"
SRC_7Z="/input/orangepi.7z"
SRC_IMG="/work/${OPI_FILE_NAME}.img"
if [ -n "${STOCK}" ]; then
  OUT_IMG="/output/stock-opi.img"
  MANIFEST="/output/manifest-opi-stock.json"
  VARIANT="stock"; TARGET_NAME="opi-stock"; SSH_ROOT="false"; SSH_PW="false"; OVL_NOTE="none (stock base)"
else
  OUT_IMG="/output/golden-opi-dev${DEV_LABEL:+-${DEV_LABEL}}.img"
  MANIFEST="/output/manifest-opi-dev${DEV_LABEL:+-${DEV_LABEL}}.json"
  VARIANT="minimal-dev"; TARGET_NAME="opi-dev"; SSH_ROOT="false"; SSH_PW="true"; OVL_NOTE="${DEV_OVERLAYS}"
fi

LOOP_DEV=""
PART_LOOP=""

cleanup() {
  set +e
  mountpoint -q "${MNT}" && umount -lf "${MNT}"
  [ -n "${PART_LOOP}" ] && losetup -d "${PART_LOOP}" 2>/dev/null
  [ -n "${LOOP_DEV}" ]  && losetup -d "${LOOP_DEV}"  2>/dev/null
}
trap cleanup EXIT

log() { echo "==> $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }

retry() {
  local cmd="$1" max="${2:-5}" delay="${3:-3}" n=0
  until [ "$n" -ge "$max" ]; do
    eval "$cmd" && return 0
    n=$((n + 1))
    log "retry $n/$max in ${delay}s: $cmd"
    sleep "$delay"
  done
  return 1
}

# ── prereq check (no qemu/chroot needed — we only drop files into the fs) ─────
for bin in 7z losetup parted resize2fs e2fsck gdown xz growpart openssl; do
  command -v "$bin" >/dev/null || err "missing tool: $bin (check Dockerfile)"
done
mkdir -p /input /output /work "${MNT}"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — Source .7z (cached; user usually pre-drops input/orangepi.7z)
# ─────────────────────────────────────────────────────────────────────────────
if [ ! -f "${SRC_7Z}" ]; then
  log "Downloading ${OPI_FILE_NAME}.7z (~734 MB) from Google Drive…"
  retry "gdown 'https://drive.google.com/uc?id=${OPI_FILE_ID}' -O '${SRC_7Z}'" 3 5 \
    || { rm -f "${SRC_7Z}"; err "gdown failed — drop the file at imager/input/orangepi.7z manually (see README)"; }
else
  log "Source .7z cached at ${SRC_7Z}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Extract, expand to OUT_IMG_SIZE, resize fs, mount
# ─────────────────────────────────────────────────────────────────────────────
log "Extracting ${SRC_7Z}…"
rm -f /work/*.img /work/*.sha
7z x -y -o/work "${SRC_7Z}" >/dev/null

EXTRACTED_IMG=$(find /work -maxdepth 2 -name '*.img' -type f | head -1)
[ -n "${EXTRACTED_IMG}" ] || err "no .img found inside .7z"
[ "${EXTRACTED_IMG}" != "${SRC_IMG}" ] && mv -f "${EXTRACTED_IMG}" "${SRC_IMG}"
log "Source image: ${SRC_IMG} ($(du -h "${SRC_IMG}" | cut -f1))"

log "Copying source → ${OUT_IMG} and expanding to ${OUT_IMG_SIZE}…"
cp -f "${SRC_IMG}" "${OUT_IMG}"
truncate -s "${OUT_IMG_SIZE}" "${OUT_IMG}"

LOOP_DEV=$(losetup --find --show "${OUT_IMG}")
sleep 1

log "Resizing partition 1 to fill image…"
growpart "${LOOP_DEV}" 1 || parted -s "${LOOP_DEV}" resizepart 1 100%

# Docker Desktop on Mac ships without udev → /dev/loopXp1 nodes don't appear.
# Attach a second loop device straight at the partition byte range instead.
PART_START=$(parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
PART_SIZE=$( parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $4}')
log "Partition 1: start=${PART_START} size=${PART_SIZE}"
PART_LOOP=$(losetup --find --show --offset "${PART_START}" --sizelimit "${PART_SIZE}" "${OUT_IMG}")
PART="${PART_LOOP}"
[ -b "${PART}" ] || err "partition loop device ${PART} did not appear"

log "Filesystem check + resize…"
e2fsck -fy "${PART}" || true
resize2fs "${PART}"

log "Mounting at ${MNT}…"
mount "${PART}" "${MNT}"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — DEV LAYER (pure file drops into the mounted fs; no chroot)
# Skipped entirely when STOCK=1 → pure vendor OS, device-agnostic base image.
# ─────────────────────────────────────────────────────────────────────────────
if [ -n "${STOCK}" ]; then
  log "Phase 2 — STOCK mode: skipping dev layer (pure vendor OS, device-agnostic)"
else
log "Phase 2 — dev layer"

# ── (a) asound.conf — from CONFIG_SRC if provided, else built-in lamp-ac82 ───
if [ -n "${CONFIG_SRC}" ] && [ -f "${CONFIG_SRC}/etc/asound.conf" ]; then
  log "[dev] /etc/asound.conf (from ${CONFIG_SRC})"
  cp -f "${CONFIG_SRC}/etc/asound.conf" "${MNT}/etc/asound.conf"
else
log "[dev] /etc/asound.conf (built-in lamp-ac82, 4 audio devices)"
cat > "${MNT}/etc/asound.conf" <<'ALSA_EOF'
# Persistent ALSA aliases for lamp-ac82 (Orange Pi 4 Pro, A733 / T527).
# Speaker:  TTGK USB Audio (device_speaker, renamed via udev).
# Micro1:   ES8389 onboard (sndi2s4) for sensing/wake.
# Micro2:   Jieli USB Composite Device (device_micro2, renamed via udev) for STT.
# Micro3:   SunplusIT OPENAICAM built-in mic (device_micro3, renamed via udev).

pcm.device_speaker {
    type plug
    slave.pcm {
        type hw
        card device_speaker
        device 0
    }
}
ctl.device_speaker { type hw card device_speaker }

pcm.device_micro1 {
    type plug
    slave.pcm {
        type hw
        card sndi2s4
        device 0
    }
}
ctl.device_micro1 { type hw card sndi2s4 }

pcm.device_micro2 {
    type plug
    slave.pcm {
        type hw
        card device_micro2
        device 0
    }
}
ctl.device_micro2 { type hw card device_micro2 }

pcm.device_micro3 {
    type plug
    slave.pcm {
        type hw
        card device_micro3
        device 0
    }
}
ctl.device_micro3 { type hw card device_micro3 }

pcm.!default {
    type asym
    playback.pcm "device_speaker"
    capture.pcm "device_micro2"
}
ctl.!default { type hw card sndi2s4 }
ALSA_EOF
fi

# ── (b) device udev rules — from CONFIG_SRC if provided, else built-in ───────
log "[dev] device udev rules"
mkdir -p "${MNT}/etc/udev/rules.d"
if [ -n "${CONFIG_SRC}" ] && ls "${CONFIG_SRC}"/etc/udev/rules.d/*.rules >/dev/null 2>&1; then
  log "[dev] copying device udev rules from ${CONFIG_SRC}:"
  cp -f "${CONFIG_SRC}"/etc/udev/rules.d/*.rules "${MNT}/etc/udev/rules.d/"
  ls -1 "${MNT}/etc/udev/rules.d/" | sed 's/^/      /'
else
log "[dev] writing built-in lamp-ac82 udev rules"
cat > "${MNT}/etc/udev/rules.d/99-lamp-device.rules" <<'UDEV_EOF'
# Camera: SunplusIT OPENAICAM -> video symlink + stable ALSA card name for built-in mic
SUBSYSTEM=="video4linux", ATTRS{idVendor}=="1bcf", ATTRS{idProduct}=="28cc", ATTR{index}=="0", SYMLINK+="device-camera"
SUBSYSTEM=="sound", ACTION=="change", ATTRS{idVendor}=="1bcf", ATTRS{idProduct}=="28cc", ATTR{id}="device_micro3"

# Servo driver: QinHeng CH340
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d3", SYMLINK+="device-servo"

# USB Mic: Jieli Technology -> stable ALSA card name
SUBSYSTEM=="sound", ACTION=="change", ATTRS{idVendor}=="4c4a", ATTRS{idProduct}=="4155", ATTR{id}="device_micro2"

# USB Speaker: TTGK Technology -> stable ALSA card name
SUBSYSTEM=="sound", ACTION=="change", ATTRS{idVendor}=="3302", ATTRS{idProduct}=="1292", ATTR{id}="device_speaker"
UDEV_EOF

cat > "${MNT}/etc/udev/rules.d/99-t527-permissions.rules" <<'UDEV_EOF'
KERNEL=="system", SUBSYSTEM=="dma_heap", GROUP="video", MODE="0660"
KERNEL=="reserved", SUBSYSTEM=="dma_heap", GROUP="video", MODE="0660"
KERNEL=="sunxi_soc_info", MODE="0666"
KERNEL=="renderD128", SUBSYSTEM=="drm", MODE="0666"
KERNEL=="cedar_dev", MODE="0666"
UDEV_EOF

cat > "${MNT}/etc/udev/rules.d/91-pulseaudio-hal-ignore.rules" <<'UDEV_EOF'
# Keep PulseAudio away from all lamp audio devices so HAL can own them.
SUBSYSTEM=="sound", ATTR{id}=="sndi2s4", ENV{PULSE_IGNORE}="1"
SUBSYSTEM=="sound", ATTR{id}=="device_micro2", ENV{PULSE_IGNORE}="1"
SUBSYSTEM=="sound", ATTR{id}=="device_speaker", ENV{PULSE_IGNORE}="1"
SUBSYSTEM=="sound", ATTR{id}=="device_micro3", ENV{PULSE_IGNORE}="1"
UDEV_EOF

cat > "${MNT}/etc/udev/rules.d/10-wifi-disable-powermanagement.rules" <<'UDEV_EOF'
KERNEL=="wlan*", ACTION=="add", RUN+="/sbin/iwconfig wlan0 power off"
UDEV_EOF

cat > "${MNT}/etc/udev/rules.d/50-usb-realtek-net.rules" <<'UDEV_EOF'
# This is used to change the default configuration of Realtek USB ethernet adapters

ACTION!="add", GOTO="usb_realtek_net_end"
SUBSYSTEM!="usb", GOTO="usb_realtek_net_end"
ENV{DEVTYPE}!="usb_device", GOTO="usb_realtek_net_end"

# Modify this to change the default value
ENV{REALTEK_NIC_MODE}="1"

# Realtek
ATTR{idVendor}=="0bda", ATTR{idProduct}=="8156", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="0bda", ATTR{idProduct}=="8155", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="0bda", ATTR{idProduct}=="8153", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="0bda", ATTR{idProduct}=="8152", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"

# Samsung
ATTR{idVendor}=="04e8", ATTR{idProduct}=="a101", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"

# Lenovo
ATTR{idVendor}=="17ef", ATTR{idProduct}=="304f", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="3052", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="3054", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="3057", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="3082", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="7205", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="720a", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="720b", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="720c", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="721e", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="a359", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"
ATTR{idVendor}=="17ef", ATTR{idProduct}=="a387", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"

# TP-LINK
ATTR{idVendor}=="2357", ATTR{idProduct}=="0601", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"

# Nvidia
ATTR{idVendor}=="0955", ATTR{idProduct}=="09ff", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"

# LINKSYS
ATTR{idVendor}=="13b1", ATTR{idProduct}=="0041", ATTR{bConfigurationValue}!="$env{REALTEK_NIC_MODE}", ATTR{bConfigurationValue}="$env{REALTEK_NIC_MODE}"

LABEL="usb_realtek_net_end"
UDEV_EOF
fi

# ── (c) permissive dev-node access for all hardware buses ─────────────────────
log "[dev] /etc/udev/rules.d/99-lamp-dev-interfaces.rules (spidev/i2c/gpiochip/pwm/ttyS = 0666)"
cat > "${MNT}/etc/udev/rules.d/99-lamp-dev-interfaces.rules" <<'UDEV_EOF'
# DEV image: open every GPIO-header bus to non-root for hardware bring-up.
KERNEL=="spidev*",        MODE="0666"
KERNEL=="i2c-[0-9]*",     MODE="0666"
KERNEL=="gpiochip[0-9]*", MODE="0666"
KERNEL=="ttyS[0-9]*",     MODE="0666"
SUBSYSTEM=="pwm",         MODE="0666"
UDEV_EOF

# ── (d) SSH: password login for the orangepi user; root login DISABLED ───────
log "[dev] SSH on (password auth; root login disabled)"
mkdir -p "${MNT}/etc/ssh/sshd_config.d"
cat > "${MNT}/etc/ssh/sshd_config.d/99-lamp-dev.conf" <<'SSH_EOF'
# DEV image: password login for the orangepi user; root SSH login disabled.
PermitRootLogin no
PasswordAuthentication yes
PermitEmptyPasswords no
SSH_EOF

# Enable ssh.service even if the stock image left it disabled (no chroot — symlink).
SSH_UNIT=""
for u in /lib/systemd/system/ssh.service /usr/lib/systemd/system/ssh.service; do
  [ -f "${MNT}${u}" ] && { SSH_UNIT="${u}"; break; }
done
if [ -n "${SSH_UNIT}" ]; then
  mkdir -p "${MNT}/etc/systemd/system/multi-user.target.wants"
  ln -sf "${SSH_UNIT}" "${MNT}/etc/systemd/system/multi-user.target.wants/ssh.service"
  log "[dev] enabled ${SSH_UNIT}"
else
  log "[dev] WARN: ssh.service unit not found in image — SSH may already be socket-activated"
fi

# Root login is disabled (above) and no root password is set — SSH access is via
# the vendor 'orangepi' user (password 'orangepi', has sudo).
log "[dev] root SSH login disabled; access via vendor 'orangepi/orangepi' (sudo)"

# ── (e) enable ALL hardware bus overlays (auto-discovered) ───────────────────
log "[dev] hardware bus overlays"
ENV_TXT="${MNT}/boot/orangepiEnv.txt"
[ -f "${ENV_TXT}" ] || ENV_TXT=$(find "${MNT}/boot" -maxdepth 2 -name 'orangepiEnv.txt' | head -1 || true)

if [ -z "${ENV_TXT}" ] || [ ! -f "${ENV_TXT}" ]; then
  log "[dev] WARN: orangepiEnv.txt not found — skipping overlays"
else
  OVL_DIR=$(find "${MNT}/boot" -type d -name overlay 2>/dev/null | head -1 || true)
  OVERLAYS_TO_ADD="${LED_OVERLAY}"

  if [ "${DEV_OVERLAYS}" != "auto" ]; then
    OVERLAYS_TO_ADD="${LED_OVERLAY} ${DEV_OVERLAYS}"
    log "[dev] explicit DEV_OVERLAYS: ${DEV_OVERLAYS}"
  elif [ -n "${OVL_DIR}" ]; then
    log "[dev] overlay dir: ${OVL_DIR}"
    # Derive board prefix from the known-good LED overlay filename.
    LED_FILE=$(ls "${OVL_DIR}" 2>/dev/null | grep -E "${LED_OVERLAY}\\.dtbo\$" | head -1 || true)
    PREFIX=""
    [ -n "${LED_FILE}" ] && PREFIX="${LED_FILE%${LED_OVERLAY}.dtbo}"
    log "[dev] overlay name prefix: '${PREFIX}'"
    echo "----- available overlays (${OVL_DIR}) -----"
    ls -1 "${OVL_DIR}" 2>/dev/null | sed 's/^/    /'
    echo "-------------------------------------------"
    for f in "${OVL_DIR}"/*.dtbo; do
      [ -e "$f" ] || continue
      b=$(basename "$f"); short="${b%.dtbo}"; short="${short#${PREFIX}}"
      case "${short}" in
        spi*|i2c*|uart*|pwm*|can*) OVERLAYS_TO_ADD="${OVERLAYS_TO_ADD} ${short}" ;;
      esac
    done
  else
    log "[dev] WARN: no overlay dir found — enabling LED overlay only"
  fi

  # Dedupe, preserving order.
  DEDUP=""
  for o in ${OVERLAYS_TO_ADD}; do
    case " ${DEDUP} " in *" ${o} "*) : ;; *) DEDUP="${DEDUP} ${o}" ;; esac
  done
  DEDUP="$(echo "${DEDUP}" | xargs)"

  if grep -q "^overlays=" "${ENV_TXT}"; then
    sed -i "s/^overlays=.*/overlays=${DEDUP}/" "${ENV_TXT}"
  else
    echo "overlays=${DEDUP}" >> "${ENV_TXT}"
  fi
  log "[dev] overlays= ${DEDUP}"
  echo "----- orangepiEnv.txt -----"
  sed 's/^/    /' "${ENV_TXT}"
  echo "---------------------------"
fi

# ── (f) hostname marker (keep /etc/hosts in sync to avoid sudo warnings) ─────
echo "lamp-dev" > "${MNT}/etc/hostname" 2>/dev/null || true
if [ -f "${MNT}/etc/hosts" ]; then
  if grep -q '^127\.0\.1\.1' "${MNT}/etc/hosts"; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1 lamp-dev/' "${MNT}/etc/hosts"
  else
    echo "127.0.1.1 lamp-dev" >> "${MNT}/etc/hosts"
  fi
fi
fi  # ── end Phase 2 dev layer (skipped when STOCK=1) ──

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — first-boot SD-fill resize (self-destructing), same as golden builder
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 4 — lamp-resize-once (first-boot expand)"
cat > "${MNT}/usr/local/bin/lamp-resize-once" <<'RESIZE_EOF'
#!/bin/bash
# Runs ONCE at first boot. Expands root partition + ext4 to fill the SD card,
# then disables itself.
set -uo pipefail
log() { echo "==> $*"; }
ROOT_PART=$(findmnt -n -o SOURCE /)
[ -z "${ROOT_PART}" ] && { echo "ERROR: cannot determine root partition"; exit 1; }
DEV_NAME=$(basename "${ROOT_PART}")
case "${DEV_NAME}" in
  mmcblk*p*|nvme*p*) PARENT="${DEV_NAME%p*}"; PART_NUM="${DEV_NAME##*p}" ;;
  sd?[0-9]*)         PARENT="${DEV_NAME%%[0-9]*}"; PART_NUM="${DEV_NAME##*[a-z]}" ;;
  *) echo "ERROR: unrecognised root device naming ${DEV_NAME}"; exit 1 ;;
esac
DISK="/dev/${PARENT}"
log "parent disk=${DISK} partition=${PART_NUM}"
growpart "${DISK}" "${PART_NUM}" || log "growpart already at max"
resize2fs "${ROOT_PART}" || log "WARN resize2fs failed"
systemctl disable lamp-resize-once.service 2>/dev/null || true
rm -f /etc/systemd/system/lamp-resize-once.service
rm -f /etc/systemd/system/multi-user.target.wants/lamp-resize-once.service
rm -f /usr/local/bin/lamp-resize-once
RESIZE_EOF
chmod +x "${MNT}/usr/local/bin/lamp-resize-once"

cat > "${MNT}/etc/systemd/system/lamp-resize-once.service" <<'UNIT'
[Unit]
Description=Expand root filesystem to fill SD card on first boot (self-destructing)
ConditionPathExists=/usr/local/bin/lamp-resize-once
DefaultDependencies=no
After=local-fs.target systemd-remount-fs.service
Before=basic.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/lamp-resize-once

[Install]
WantedBy=multi-user.target
UNIT
mkdir -p "${MNT}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/lamp-resize-once.service \
  "${MNT}/etc/systemd/system/multi-user.target.wants/lamp-resize-once.service"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 — manifest, unmount, compress
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 5 — finalize"
SRC_7Z_SHA=$(sha256sum "${SRC_7Z}" 2>/dev/null | cut -d' ' -f1 || echo unknown)
cat > "${MANIFEST}" <<MANIFEST_JSON
{
  "build_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "target": "${TARGET_NAME}",
  "variant": "${VARIANT}",
  "label": "${DEV_LABEL}",
  "config_src": "${CONFIG_SRC}",
  "out_img_size": "${OUT_IMG_SIZE}",
  "dev_overlays": "${OVL_NOTE}",
  "ssh": { "root_login": ${SSH_ROOT}, "password_auth": ${SSH_PW} },
  "app_stack": false,
  "source_image": {
    "file_id": "${OPI_FILE_ID}",
    "name": "${OPI_FILE_NAME}.7z",
    "sha256": "${SRC_7Z_SHA}"
  }
}
MANIFEST_JSON
log "Manifest: ${MANIFEST}"

sync
umount "${MNT}"
losetup -d "${PART_LOOP}"; PART_LOOP=""
losetup -d "${LOOP_DEV}";  LOOP_DEV=""

log "Compressing ${OUT_IMG} → ${OUT_IMG}.xz (xz -9 -T0, memlimit ${XZ_MEMLIMIT})…"
rm -f "${OUT_IMG}.xz"
xz -9 -k --threads=0 --memlimit-compress="${XZ_MEMLIMIT}" "${OUT_IMG}"

log "DONE: ${OUT_IMG}.xz ($(du -h "${OUT_IMG}.xz" | cut -f1))"
log "Flash:  make TARGET=${TARGET_NAME} ${DEV_LABEL:+DEV_LABEL=${DEV_LABEL} }sd-card-flash DISK=N"
