#!/bin/bash
# =============================================================================
# build-orangepi.sh — Golden Image Builder for OrangePi (Armbian-based)
# =============================================================================
#
# STATUS: SKELETON — Phase 1 written against Armbian Trixie for OrangePi 4 Pro
#         (RK3399). Chroot Phase 1 + Phase 2 stages are NOT yet shared with
#         imager/build.sh — see TODO_SHARED_STAGES below. Test on real OPi 4
#         Pro hardware before relying on the produced golden-opi.img.
#
# PURPOSE
#   Produces golden-opi.img for OrangePi devices. Sibling of build.sh (which
#   targets Raspberry Pi 5 / RPi OS). Same Btrfs @ + @factory layout, same
#   AP-first-boot flow, same OTA metadata + backend bake.
#
# KEY DIFFERENCES FROM PI BUILD
#   1. Base image:  Armbian Trixie minimal for OPi (not RPi OS)
#   2. Bootloader:  U-Boot + extlinux.conf / armbianEnv.txt (not RPi firmware)
#   3. Boot part:   /boot (ext4 in Armbian) — single root partition layout
#                   is common; need to handle both 1-part and 2-part variants
#   4. SPI enable:  via armbianEnv.txt overlays= (no dtparam=spi=on)
#   5. WiFi country: no raspi-config; set via /etc/default/crda + wpa_supplicant
#   6. AP serial:  device-tree usually empty → ethernet MAC fallback always
#                  fires (already handled in shared device-ap-mode script)
#
# RUN VIA Makefile:  make TARGET=opi build
#
# =============================================================================

set -euo pipefail

# ── config — edit before building ────────────────────────────────────────────
WIFI_COUNTRY="US"
PI_HOSTNAME="autonomous"
PI_TIMEZONE="America/New_York"
USERNAME="system"
PASSWORD="12345"
OUT_IMG_SIZE="8G"
OTA_METADATA_URL="https://storage.googleapis.com/s3-autonomous-upgrade-3/lumi/ota/metadata.json"
AP_BAND="${AP_BAND:-2.4}"
AP_CHANNEL="${AP_CHANNEL:-}"
COUNTRY_CODE="US"

# ── OrangePi-specific base image ─────────────────────────────────────────────
# TODO: Pin to a specific Armbian release once verified. The redirect URL
# returns the current "stable" image for OPi 4 Pro. Armbian publishes images
# under https://www.armbian.com/orange-pi-4-pro/
#
# For OPi 5 / 5 Plus (RK3588) swap the board slug:
#   orangepi5      → OPi 5
#   orangepi5-plus → OPi 5 Plus
ARMBIAN_BOARD="${ARMBIAN_BOARD:-orangepi4-pro}"
ARMBIAN_RELEASE="${ARMBIAN_RELEASE:-trixie}"
ARMBIAN_VARIANT="${ARMBIAN_VARIANT:-minimal}"
ARMBIAN_IMG_URL="${ARMBIAN_IMG_URL:-https://redirect.armbian.com/${ARMBIAN_BOARD}/${ARMBIAN_RELEASE^}_current_${ARMBIAN_VARIANT}}"
# ─────────────────────────────────────────────────────────────────────────────

MNT="/mnt/opi"
SRC_IMG_XZ="/input/armbian.img.xz"
SRC_IMG="/work/armbian.img"
OUT_IMG="/output/golden-opi.img"
BASE_IMG="/output/base-opi.img"
ORIG_ROOT="/mnt/orig_root"
ORIG_BOOT="/mnt/orig_boot"

BTRFS_BUILD_OPTS="defaults,noatime,compress=zstd:1,commit=5"
BTRFS_FSTAB_OPTS="defaults,noatime,compress=zstd:1"

LOOP_DEV="" LOOP_BOOT="" LOOP_ROOT=""
OUT_LOOP_DEV="" OUT_LOOP_BOOT="" OUT_LOOP_ROOT=""

# ── helpers ──────────────────────────────────────────────────────────────────
cleanup() {
  set +e
  for mp in "${MNT}/dev" "${MNT}/sys" "${MNT}/proc" "${MNT}/boot" "${MNT}" "${ORIG_BOOT}" "${ORIG_ROOT}" /mnt/btrfs-top; do
    [ -d "$mp" ] && mountpoint -q "$mp" && umount -lf "$mp" 2>/dev/null
  done
  for dev in $OUT_LOOP_BOOT $OUT_LOOP_ROOT $OUT_LOOP_DEV $LOOP_BOOT $LOOP_ROOT $LOOP_DEV; do
    [ -n "$dev" ] && losetup -d "$dev" 2>/dev/null
  done
}
trap cleanup EXIT

log() { echo "==> $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }

# ── prereq check ─────────────────────────────────────────────────────────────
command -v parted >/dev/null || err "parted not found (apt install parted)"
command -v btrfs  >/dev/null || err "btrfs-progs not found"
command -v qemu-aarch64-static >/dev/null || err "qemu-user-static not found"
command -v xz     >/dev/null || err "xz-utils not found"

mkdir -p /input /output /work "${MNT}" "${ORIG_ROOT}" "${ORIG_BOOT}"

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — BASE IMAGE (OPi-specific)
# ─────────────────────────────────────────────────────────────────────────────

if [ -f "${BASE_IMG}" ]; then
  log "Phase 1 skipped — ${BASE_IMG} exists. Delete to force rebuild."
else
  # ── 1. download or use cached Armbian image ────────────────────────────────
  if [ ! -f "${SRC_IMG_XZ}" ]; then
    log "Downloading Armbian image: ${ARMBIAN_IMG_URL}"
    curl -fL -o "${SRC_IMG_XZ}" "${ARMBIAN_IMG_URL}" || err "Armbian image download failed"
  fi
  log "Decompressing source image"
  xz -dkf "${SRC_IMG_XZ}" --stdout > "${SRC_IMG}"

  # ── 2. read source image partition layout ──────────────────────────────────
  # Armbian images for RK3399/RK3588 typically use a single ext4 root partition
  # (no separate FAT boot — bootloader lives in raw sectors before partition 1).
  # Some board flavours ship 2 partitions (FAT boot + root) — detect at runtime.
  LOOP_DEV=$(losetup --find --show "${SRC_IMG}")
  partprobe "${LOOP_DEV}" || true
  PART_COUNT=$(parted -s "${LOOP_DEV}" unit B print 2>/dev/null | grep -cE '^ +[0-9]+ ' || echo 0)
  log "Source image has ${PART_COUNT} partition(s)"

  # TODO_OPI_PARTITION_LAYOUT: handle 1-part vs 2-part Armbian layouts. For
  # OPi 4 Pro Trixie minimal (single ext4 root) the code below should work as-is.
  # For images with a separate FAT boot, we'd need a second LOOP_BOOT setup.
  if [ "${PART_COUNT}" = "1" ]; then
    ROOT_START=$(parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
    LOOP_ROOT=$(losetup --find --show --offset "${ROOT_START}" "${SRC_IMG}")
    LOOP_BOOT=""
  elif [ "${PART_COUNT}" = "2" ]; then
    BOOT_START=$(parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
    BOOT_SIZE=$( parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $4}')
    ROOT_START=$(parted -s "${LOOP_DEV}" unit B print | awk '/^ 2/{gsub(/B/,""); print $2}')
    LOOP_BOOT=$(losetup --find --show --offset "${BOOT_START}" --sizelimit "${BOOT_SIZE}" "${SRC_IMG}")
    LOOP_ROOT=$(losetup --find --show --offset "${ROOT_START}" "${SRC_IMG}")
  else
    err "Unexpected Armbian partition count: ${PART_COUNT}"
  fi

  # ── 3. backup source rootfs (+ boot if present) ────────────────────────────
  log "Backing up source rootfs"
  mount "${LOOP_ROOT}" "${ORIG_ROOT}"
  if [ -n "${LOOP_BOOT}" ]; then
    mount "${LOOP_BOOT}" "${ORIG_BOOT}"
  fi
  rm -rf /work/rootfs /work/boot
  mkdir -p /work/rootfs /work/boot
  rsync -aHAX --numeric-ids "${ORIG_ROOT}/" /work/rootfs/
  if [ -n "${LOOP_BOOT}" ]; then
    rsync -aHAX --numeric-ids "${ORIG_BOOT}/" /work/boot/
    umount "${ORIG_BOOT}"
  fi
  umount "${ORIG_ROOT}"
  losetup -d "${LOOP_ROOT}"; LOOP_ROOT=""
  [ -n "${LOOP_BOOT}" ] && { losetup -d "${LOOP_BOOT}"; LOOP_BOOT=""; }
  losetup -d "${LOOP_DEV}"; LOOP_DEV=""

  # ── 4. create blank output image and partition ──────────────────────────────
  # OPi U-Boot expects bootloader in raw sectors 64..8192 of the SD card. The
  # Armbian image we extracted has those bytes intact in the first ~8MB.
  # We preserve them by dd-copying from source image, then re-partition the
  # rest. This avoids reimplementing U-Boot/SPL/idbloader/uboot.img layout.
  log "Creating blank output image (${OUT_IMG_SIZE})"
  truncate -s "${OUT_IMG_SIZE}" "${BASE_IMG}"

  # Copy raw bootloader region (first 16MB) from source. Armbian's U-Boot lives
  # at sectors 64-16384 (idbloader) and 16384-24576 (uboot.img) on RK3399 —
  # 16MB cushion covers both. TODO_OPI_VERIFY_BOOTLOADER_OFFSETS for OPi 5.
  log "Copying U-Boot bootloader region from source"
  dd if="${SRC_IMG}" of="${BASE_IMG}" bs=1M count=16 conv=notrunc status=progress

  # Create single Btrfs partition starting at 32MB (well clear of bootloader)
  OUT_LOOP_DEV=$(losetup --find --show "${BASE_IMG}")
  parted -s "${OUT_LOOP_DEV}" mklabel msdos
  parted -s "${OUT_LOOP_DEV}" mkpart primary btrfs 32MiB 100%
  partprobe "${OUT_LOOP_DEV}"

  OUT_ROOT_START=$(parted -s "${OUT_LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
  OUT_LOOP_ROOT=$(losetup --find --show --offset "${OUT_ROOT_START}" "${BASE_IMG}")
  OUT_LOOP_BOOT=""

  # ── 5. format Btrfs root + create @ subvolume ──────────────────────────────
  log "Formatting Btrfs root"
  mkfs.btrfs -L lumi-root -f "${OUT_LOOP_ROOT}"
  mount -o "${BTRFS_BUILD_OPTS}" "${OUT_LOOP_ROOT}" "${MNT}"
  btrfs subvolume create "${MNT}/@"
  umount "${MNT}"
  mount -o "${BTRFS_BUILD_OPTS},subvol=@" "${OUT_LOOP_ROOT}" "${MNT}"

  # ── 6. restore rootfs into Btrfs ───────────────────────────────────────────
  log "Restoring rootfs into Btrfs @"
  rsync -aHAX --numeric-ids /work/rootfs/ "${MNT}/"
  # If source had a separate /boot partition, merge it into /boot of the new root.
  if [ -d /work/boot ] && [ "$(ls -A /work/boot 2>/dev/null)" ]; then
    mkdir -p "${MNT}/boot"
    rsync -aHAX --numeric-ids /work/boot/ "${MNT}/boot/"
  fi

  # ── 7. user, ssh, hostname, locale, timezone (shared with Pi build) ────────
  HASH=$(openssl passwd -6 "${PASSWORD}")
  chroot "${MNT}" /usr/sbin/useradd -m -G sudo,audio,video,plugdev,dialout -s /bin/bash "${USERNAME}" 2>/dev/null || true
  chroot "${MNT}" /usr/sbin/chpasswd -e <<<"${USERNAME}:${HASH}" || true
  echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > "${MNT}/etc/sudoers.d/010-${USERNAME}"
  chmod 440 "${MNT}/etc/sudoers.d/010-${USERNAME}"

  touch "${MNT}/etc/ssh/sshd_config.d/.placeholder" 2>/dev/null || true
  ln -sf /lib/systemd/system/ssh.service "${MNT}/etc/systemd/system/multi-user.target.wants/ssh.service" 2>/dev/null || true

  echo "${PI_HOSTNAME}" > "${MNT}/etc/hostname"
  echo "127.0.1.1 ${PI_HOSTNAME}" >> "${MNT}/etc/hosts"
  echo "${PI_TIMEZONE}" > "${MNT}/etc/timezone"
  ln -sf "/usr/share/zoneinfo/${PI_TIMEZONE}" "${MNT}/etc/localtime"

  # ── 8. fstab ────────────────────────────────────────────────────────────────
  ROOT_UUID=$(btrfs filesystem show "${MNT}" | awk '/uuid:/ {print $NF}')
  cat > "${MNT}/etc/fstab" <<EOF
UUID=${ROOT_UUID}  /  btrfs  ${BTRFS_FSTAB_OPTS},subvol=@  0  0
EOF

  # ── 9. armbianEnv.txt — boot args (OPi-specific, NOT cmdline.txt) ──────────
  # Armbian uses /boot/armbianEnv.txt for kernel args. Convert it to point at
  # Btrfs @ subvolume so U-Boot mounts the right rootfs.
  ARMBIAN_ENV="${MNT}/boot/armbianEnv.txt"
  if [ -f "${ARMBIAN_ENV}" ]; then
    # Replace or add rootfstype + rootflags
    sed -i '/^rootfstype=/d;/^rootflags=/d' "${ARMBIAN_ENV}"
    {
      echo "rootfstype=btrfs"
      echo "rootflags=subvol=@,compress=zstd:1"
    } >> "${ARMBIAN_ENV}"
    log "armbianEnv.txt patched for Btrfs @ subvolume"
  else
    log "WARN: ${ARMBIAN_ENV} not present in source image — U-Boot will use defaults"
    # TODO_OPI_BOOT_ENV: investigate alternate boot config (extlinux.conf, boot.scr)
  fi

  # ──────────────────────────────────────────────────────────────────────────
  # PHASE 1 CHROOT STAGES — duplicate of build.sh's chroot block until extracted
  # ──────────────────────────────────────────────────────────────────────────
  #
  # TODO_SHARED_STAGES: the chroot block (apt install + AP setup + PulseAudio +
  # lelamp uv + node + openclaw + systemd units) is currently NOT shared with
  # imager/build.sh. Two options for the refactor:
  #
  #   (a) Extract into imager/lib/chroot-phase1.sh (real bash file, no heredoc
  #       escaping needed). Both build.sh and build-orangepi.sh copy it into
  #       chroot then exec it. Caller passes config via env vars.
  #
  #   (b) Keep duplicate, run a CI diff-check on the chroot blocks to catch
  #       drift.
  #
  # Until then, when porting a fix that touches chroot stages, update BOTH
  # build.sh and build-orangepi.sh. See imager/README.md for the canonical
  # stage list.

  log "Phase 1 chroot stages — SKELETON (chroot block needs porting from build.sh)"

  # Bind-mount for chroot
  cp /usr/bin/qemu-aarch64-static "${MNT}/usr/bin/qemu-aarch64-static"
  mount --bind /proc "${MNT}/proc"
  mount --bind /sys  "${MNT}/sys"
  mount --bind /dev  "${MNT}/dev"
  cp "${MNT}/etc/resolv.conf" "${MNT}/etc/resolv.conf.bak" 2>/dev/null || true
  cp /etc/resolv.conf "${MNT}/etc/resolv.conf"

  # MINIMAL CHROOT — just install packages so the skeleton produces a bootable
  # image. Full stages (AP setup, PulseAudio, lelamp, openclaw, systemd units)
  # must be added before this image is usable for production. Port from
  # build.sh lines 521-1395 — most stages are board-agnostic and apply as-is
  # once apt installs succeed on Armbian.
  chroot "${MNT}" /bin/bash <<'CHROOT_MIN'
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y \
  btrfs-progs parted util-linux \
  hostapd dnsmasq nginx \
  curl jq unzip ca-certificates \
  wpasupplicant dhcpcd5 \
  iproute2 iptables iw rfkill \
  cloud-guest-utils \
  wireless-tools net-tools \
  systemd-sysv \
  xvfb chromium git \
  fake-hwclock \
  libportaudio2 portaudio19-dev pulseaudio pulseaudio-utils ffmpeg \
  alsa-utils libasound2-dev \
  libopenblas0 libgomp1 liblapack3 \
  libgpiod2 \
  python3-dev \
  libsm6 libxext6 libgl1 \
  libjpeg-dev zlib1g-dev libfreetype6-dev libopenjp2-7-dev libtiff-dev \
  openresolv \
  avahi-daemon avahi-utils libnss-mdns \
  bluez 2>&1 | tail -20
apt-get clean
CHROOT_MIN

  mv "${MNT}/etc/resolv.conf.bak" "${MNT}/etc/resolv.conf" 2>/dev/null || true
  umount "${MNT}/dev" "${MNT}/sys" "${MNT}/proc"
  rm -f "${MNT}/usr/bin/qemu-aarch64-static"

  umount "${MNT}"
  losetup -d "${OUT_LOOP_ROOT}"; OUT_LOOP_ROOT=""
  losetup -d "${OUT_LOOP_DEV}"; OUT_LOOP_DEV=""

  log "Phase 1 base image saved: ${BASE_IMG}"
  log "WARNING: chroot stages incomplete — see TODO_SHARED_STAGES."
  log "         Image will boot but lacks AP/lelamp/openclaw services."
fi

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — OVERLAY (copy base→golden, apply OTA + backend binaries + lelamp + web)
# ─────────────────────────────────────────────────────────────────────────────
#
# TODO_SHARED_STAGES: this Phase 2 logic is identical to build.sh Phase 2 and
# the prime candidate for extraction. For now, copy base→golden and exit —
# user fills in Phase 2 after verifying Phase 1 boots on hardware.

log "Phase 2 — copying base-opi.img to golden-opi.img"
cp -f "${BASE_IMG}" "${OUT_IMG}"

log "Build complete: ${OUT_IMG}"
log ""
log "NEXT STEPS:"
log "  1. Flash to SD card:    make sd-card-flash-opi DISK=N"
log "  2. Boot OPi from SD card; verify Armbian boots with Btrfs root"
log "  3. ssh ${USERNAME}@<opi-ip> (password: ${PASSWORD})"
log "  4. Port remaining chroot stages from imager/build.sh into this script,"
log "     then rebuild. See TODO_SHARED_STAGES markers."
