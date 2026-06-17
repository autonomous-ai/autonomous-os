#!/bin/bash
# =============================================================================
# build-orangepi.sh — Golden Image Builder for OrangePi 4 Pro v2 (A733/sun60iw2)
# =============================================================================
#
# Source image: Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z
# (vendor "user-built" image from orangepi-xunlong/orangepi-build, hosted on
# Google Drive folder 1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS — there is no public
# mirror; the dev team uploaded it themselves).
#
# Flow:
#   Phase 0  download .7z from Google Drive (cached in /input/)
#   Phase 1  extract .img, expand to OUT_IMG_SIZE, partprobe, resize2fs
#   Phase 2  chroot apt install + write systemd units + helper scripts + configs
#   Phase 3  chroot OTA bake — backend binaries + hal + web UI + buddy
#   Phase 4  install resize-once.service for first-boot SD-fill expand
#   Phase 5  unmount + compress → /output/golden-opi.img.xz
#
# Run via Makefile (Docker container, --privileged for losetup/mount).
# =============================================================================

set -euo pipefail

# ── config ───────────────────────────────────────────────────────────────────
PI_HOSTNAME="autonomous"
PI_TIMEZONE="America/New_York"
USERNAME="system"
PASSWORD="12345"
OUT_IMG_SIZE="${OUT_IMG_SIZE:-14G}"
# OTA metadata URL — per-deployment value, passed in by the Makefile (-e). No
# hardcoded default: fail fast if the caller did not provide one. Baked into the
# image's /root/config/bootstrap.json.
OTA_METADATA_URL="${OTA_METADATA_URL:?OTA_METADATA_URL is required — build via 'make build OTA_METADATA_URL=...'}"
AP_BAND="${AP_BAND:-2.4}"
AP_CHANNEL="${AP_CHANNEL:-}"
COUNTRY_CODE="${COUNTRY_CODE:-US}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.5.27}"
# Device class this golden image is for — bakes devices/<type>/{DEVICE,SOUL}.md
# so one DEVICE_TYPE = one golden image. Forwarded by the Makefile via docker -e.
# REQUIRED, no default — a golden image must declare which device class it is.
DEVICE_TYPE="${DEVICE_TYPE:?DEVICE_TYPE is required — build via 'make build DEVICE_TYPE=...'}"
DEVICES_DIR="${DEVICES_DIR:-/opt/devices}"

# Google Drive file ID for the bookworm server image. Override via env var when
# the dev team rotates the .7z (new Orange Pi release).
OPI_FILE_ID="${OPI_FILE_ID:-1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn}"
OPI_FILE_NAME="Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147"

# Per-device pre-built base image. lamp and intern-v2 ship hardware-team-baked
# .img.xz in input/<device>/. Other device types fall back to Google Drive stock.
case "${DEVICE_TYPE}" in
  lamp)
    DEVICE_BASE_IMG="${DEVICE_BASE_IMG:-/input/lamp/golden-opi-dev.img.xz}"
    ;;
  intern-v2)
    DEVICE_BASE_IMG="${DEVICE_BASE_IMG:-/input/intern-v2/golden-opi-dev.img.xz}"
    ;;
  *)
    DEVICE_BASE_IMG=""
    ;;
esac

MNT="/mnt/opi"
SRC_7Z="/input/orangepi.7z"
SRC_IMG="/work/base-${DEVICE_TYPE}.img"
OUT_DIR="/output/${DEVICE_TYPE}"
OUT_IMG="${OUT_DIR}/golden-opi.img"

LOOP_DEV=""
PART_LOOP=""

cleanup() {
  set +e
  mountpoint -q "${MNT}/dev"  && umount -lf "${MNT}/dev"
  mountpoint -q "${MNT}/sys"  && umount -lf "${MNT}/sys"
  mountpoint -q "${MNT}/proc" && umount -lf "${MNT}/proc"
  mountpoint -q "${MNT}"      && umount -lf "${MNT}"
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

# ── prereq check ─────────────────────────────────────────────────────────────
for bin in 7z losetup parted resize2fs e2fsck mkfs.ext4 qemu-aarch64-static gdown xz growpart; do
  command -v "$bin" >/dev/null || err "missing tool: $bin (check Dockerfile)"
done
mkdir -p /input /output "${OUT_DIR}" /work "${MNT}"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — Source base image: per-device pre-built or Google Drive stock
# ─────────────────────────────────────────────────────────────────────────────
if [ -n "${DEVICE_BASE_IMG:-}" ]; then
  log "Base image for ${DEVICE_TYPE}: ${DEVICE_BASE_IMG}"
  [ -f "${DEVICE_BASE_IMG}" ] || err "Base image not found: ${DEVICE_BASE_IMG} — place it at imager/input/${DEVICE_TYPE}/"
  log "Decompressing ${DEVICE_BASE_IMG} → ${SRC_IMG}…"
  xz -dkc --threads=0 "${DEVICE_BASE_IMG}" > "${SRC_IMG}"
else
  if [ ! -f "${SRC_7Z}" ]; then
    log "Downloading ${OPI_FILE_NAME}.7z (~734 MB) from Google Drive…"
    if ! retry "gdown 'https://drive.google.com/uc?id=${OPI_FILE_ID}' -O '${SRC_7Z}'" 3 5; then
      rm -f "${SRC_7Z}"
      cat >&2 <<MSG
==============================================================================
gdown failed. Google Drive rate-limits popular files (~"Too many users have
viewed or downloaded this file recently"). The browser bypasses this because
it uses an authenticated session.

MANUAL FIX (one-time per machine):

  1. Open in your browser (authenticated to your Google account):
     https://drive.google.com/uc?id=${OPI_FILE_ID}
     or browse the folder:
     https://drive.google.com/drive/folders/1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS

  2. Click "Download anyway" past the "no virus scan" warning.

  3. Place the downloaded file at:
     $(pwd)/input/orangepi.7z
     (or imager/input/orangepi.7z on the host — the Docker mount sees it there)

  4. Re-run: make build

The .7z file is cached after this — gdown isn't called on subsequent builds.
==============================================================================
MSG
      exit 1
    fi
  else
    log "Source .7z cached at ${SRC_7Z}"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Extract (stock only), expand to OUT_IMG_SIZE, partprobe, resize2fs
# ─────────────────────────────────────────────────────────────────────────────
if [ -z "${DEVICE_BASE_IMG:-}" ]; then
  log "Extracting ${SRC_7Z}…"
  rm -f /work/*.img /work/*.sha
  7z x -y -o/work "${SRC_7Z}" >/dev/null
  EXTRACTED_IMG=$(find /work -maxdepth 2 -name '*.img' -type f | head -1)
  [ -n "${EXTRACTED_IMG}" ] || err "no .img found inside .7z"
  if [ "${EXTRACTED_IMG}" != "${SRC_IMG}" ]; then
    mv -f "${EXTRACTED_IMG}" "${SRC_IMG}"
  fi
fi
log "Source image: ${SRC_IMG} ($(du -h "${SRC_IMG}" | cut -f1))"

log "Copying source → ${OUT_IMG} and expanding to ${OUT_IMG_SIZE}…"
cp -f "${SRC_IMG}" "${OUT_IMG}"
truncate -s "${OUT_IMG_SIZE}" "${OUT_IMG}"

LOOP_DEV=$(losetup --find --show "${OUT_IMG}")
sleep 1

log "Resizing partition 1 to fill image…"
growpart "${LOOP_DEV}" 1 || parted -s "${LOOP_DEV}" resizepart 1 100%

# Docker Desktop on Mac (and minimal containers in general) ship without udev
# so /dev/loopXp1 device nodes don't appear after partition resize. Read the
# new partition byte offset + size via parted, then attach a second loop
# device pointing directly at the partition. Bypasses kernel partition device
# node creation entirely.
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
# Phase 2 — Chroot: apt install, Node, OpenClaw, uv, systemd units, configs
# ─────────────────────────────────────────────────────────────────────────────
log "Setting up chroot…"
cp /usr/bin/qemu-aarch64-static "${MNT}/usr/bin/qemu-aarch64-static"
mount --bind /proc "${MNT}/proc"
mount --bind /sys  "${MNT}/sys"
mount --bind /dev  "${MNT}/dev"
cp -f "${MNT}/etc/resolv.conf" "${MNT}/etc/resolv.conf.bak" 2>/dev/null || true
cp -f /etc/resolv.conf "${MNT}/etc/resolv.conf"

# Suppress debconf interactive prompts during apt installs.
chroot "${MNT}" debconf-set-selections <<'DBCONF' || true
debconf debconf/frontend select Noninteractive
keyboard-configuration keyboard-configuration/layoutcode string us
DBCONF
cat > "${MNT}/etc/apt/apt.conf.d/99-${DEVICE_TYPE}-silent" <<'APT'
Dpkg::Use-Pty "false";
APT

# Pre-seed env passed into chroot heredoc — unquoted heredoc so ${VAR} expands.
chroot "${MNT}" /bin/bash <<CHROOT_STAGES
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export OTA_METADATA_URL="${OTA_METADATA_URL}"
export AP_BAND="${AP_BAND}"
export AP_CHANNEL="${AP_CHANNEL}"
export COUNTRY_CODE="${COUNTRY_CODE}"
export OPENCLAW_VERSION="${OPENCLAW_VERSION}"
export DEVICE_TYPE="${DEVICE_TYPE}"
export DEVICES_DIR="${DEVICES_DIR}"

retry() {
  local cmd="\$1" max="\${2:-5}" delay="\${3:-3}" n=0
  until [ "\$n" -ge "\$max" ]; do
    eval "\$cmd" && return 0
    n=\$((n + 1))
    echo "retry \$n/\$max in \${delay}s: \$cmd"
    sleep "\$delay"
  done
  return 1
}

# ── apt: install OS runtime deps (matches setup.sh + production OPi list) ──
echo "[stage] apt update + install"
apt-get update -qq
apt-get install -y \\
  btrfs-progs \\
  hostapd dnsmasq nginx \\
  curl jq unzip ca-certificates \\
  wpasupplicant dhcpcd5 \\
  iproute2 iptables iw rfkill \\
  cloud-guest-utils \\
  wireless-tools net-tools \\
  systemd-sysv \\
  xvfb xauth chromium chromium-sandbox git \\
  openresolv \\
  fake-hwclock \\
  libportaudio2 portaudio19-dev pulseaudio pulseaudio-utils ffmpeg \\
  alsa-utils libasound2-dev \\
  libopenblas0 libgomp1 liblapack3 \\
  libgpiod2 \\
  python3-dev python3-spidev \\
  libsm6 libxext6 libgl1 \\
  libjpeg-dev zlib1g-dev libfreetype6-dev libopenjp2-7-dev libtiff-dev \\
  avahi-daemon avahi-utils libnss-mdns \\
  bluez openssh-server

# Purge things that would conflict with our AP/STA flow on first boot.
apt-get purge -y --auto-remove network-manager network-manager-gnome 2>/dev/null || true
apt-get clean

# Disable IPv6 — RPi 5 STA-drop workaround; harmless on OrangePi.
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-${DEVICE_TYPE}-wifi.conf <<'SYSCTL'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
SYSCTL

# resolvconf fallback DNS — ensures /etc/resolv.conf is never empty in AP mode.
if [ -f /etc/resolvconf.conf ]; then
  grep -q '^name_servers=' /etc/resolvconf.conf || echo 'name_servers="1.1.1.1 8.8.8.8"' >> /etc/resolvconf.conf
else
  echo 'name_servers="1.1.1.1 8.8.8.8"' > /etc/resolvconf.conf
fi

# ── Node.js 22 + OpenClaw CLI (npm global) ───────────────────────────────────
echo "[stage] Node.js 22 + OpenClaw \${OPENCLAW_VERSION}"
if ! command -v node &>/dev/null || ! node -v 2>/dev/null | grep -qE '^v(2[2-9]|[3-9][0-9])'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
retry "npm install -g openclaw@\${OPENCLAW_VERSION} --omit=optional" 5
openclaw --version || true

# OpenClaw state dir. MUST be /root/.openclaw (with dot) — see openclaw memory
# note: any /root/openclaw mismatch causes WS close 1008 / token_mismatch.
mkdir -p \\
  /root/.openclaw \\
  /root/.openclaw/agents/main/agent \\
  /root/.openclaw/workspace \\
  /root/.openclaw/.cache \\
  /root/.openclaw/.config \\
  /root/.openclaw/.local/share

# Onboard creates default config files. --skip-health since chroot has no
# systemd/network; gateway completes onboarding on first device boot.
HOME=/root \\
OPENCLAW_HOME=/root/.openclaw \\
OPENCLAW_STATE_DIR=/root/.openclaw \\
XDG_CACHE_HOME=/root/.openclaw/.cache \\
XDG_CONFIG_HOME=/root/.openclaw/.config \\
XDG_DATA_HOME=/root/.openclaw/.local/share \\
timeout 60 openclaw onboard --non-interactive --accept-risk --skip-health || \\
  echo "WARN: openclaw onboard timed out (will retry on device first boot)"

# Install external plugins baked into the golden image.
openclaw plugins install @openclaw/discord@${OPENCLAW_VERSION} --force 2>&1 || echo "WARN: discord plugin install failed (non-fatal)"
openclaw plugins install @openclaw/slack@${OPENCLAW_VERSION} --force 2>&1 || echo "WARN: slack plugin install failed (non-fatal)"

# ── uv (Python pkg mgr for HAL) ───────────────────────────────────────────
echo "[stage] uv"
if ! command -v uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="/root/.local/bin:\$PATH"
mkdir -p /opt/hal

# ── systemd units ────────────────────────────────────────────────────────────
echo "[stage] systemd units"

# Unquoted heredoc so \${DEVICE_TYPE}/\${DEVICES_DIR} expand from the chroot env
# (exported above). The unit has no other shell-expandable tokens, so leaving it
# unquoted is safe.
cat > /etc/systemd/system/os-server.service <<UNIT
[Unit]
Description=Autonomous OS Server
After=network-online.target

[Service]
User=root
WorkingDirectory=/root
Environment=DEVICE_TYPE=\${DEVICE_TYPE}
Environment=DEVICES_DIR=\${DEVICES_DIR}
ExecStart=/usr/local/bin/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bootstrap.service <<'UNIT'
[Unit]
Description=Bootstrap Backend
After=network-online.target

[Service]
User=root
ExecStart=/usr/local/bin/bootstrap-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bootstrap

[Install]
WantedBy=multi-user.target
UNIT

# Seed the bootstrap worker config so the OTA metadata URL comes from
# /root/config/bootstrap.json at runtime (single source of truth). The bootstrap
# binary has no compiled-in default and waits until this file provides
# metadata_url — baked here from the build-time OTA_METADATA_URL.
mkdir -p /root/config
cat > /root/config/bootstrap.json <<BSJSON
{
  "httpPort": 8080,
  "metadata_url": "${OTA_METADATA_URL}",
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
BSJSON

cat > /etc/systemd/system/hal.service <<'UNIT'
[Unit]
Description=HAL Hardware Runtime
After=network.target

[Service]
EnvironmentFile=/opt/hal/.env
Type=simple
User=root
WorkingDirectory=/opt/hal
Environment="PYTHONPATH=/opt"
# Anonymous PulseAudio socket (see the default.pa drop-in below) so root-owned
# hal can reach the desktop user's PulseAudio for Bluetooth headset routing.
Environment="PULSE_SERVER=unix:/tmp/pulse-anon-${DEVICE_TYPE}"
ExecStart=/opt/hal/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hal

[Install]
WantedBy=multi-user.target
UNIT

# Default hal env — production-safe defaults. Secrets (GELF, API keys) are
# filled by the device operator via setup wizard; not baked into the image.
cat > /opt/hal/.env <<'ENV'
HAL_MODE=production
HAL_LOG_LEVEL=INFO
HAL_AUDIO_INPUT_ALSA=plug:device_micro2
HAL_AUDIO_SENSING_DEVICE=plug:device_micro1
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker
HAL_VAD_THRESHOLD=1500
HAL_STT_KEEPALIVE=true
HAL_SPEECH_HOLDOFF=0.05
HAL_SOUND_RMS_THRESHOLD=3000
HAL_TTS_SPEED=1.1
HAL_SILERO_ENABLED=false
HAL_WEBRTCVAD_ENABLED=true
HAL_MOTION_ENABLED=true
HAL_EMOTION_ENABLED=true
HAL_POSE_MOTION_ENABLED=false
HAL_MOTION_CONFIDENCE_THRESHOLD=0.4
HAL_EMOTION_CONFIDENCE_THRESHOLD=0.8
HAL_BACKCHANNEL_INTERVAL_S=5
HAL_CAMERA_WIDTH=2560
HAL_CAMERA_HEIGHT=1440
HAL_CAMERA_STREAM_WIDTH=2560
HAL_CAMERA_STREAM_HEIGHT=1440
HAL_CAMERA_INDEX=0
SPEAKER_MATCH_THRESHOLD=0.75
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD=0.75
HAL_DL_ENCRYPTION=true
HAL_DL_ENCRYPTION_REQUIRED=false
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
ENV

# Device profile selector for HAL — appended idempotently so the quoted ENV
# heredoc above stays literal while \${DEVICE_TYPE}/\${DEVICES_DIR} expand here
# from the chroot env.
grep -q "^DEVICE_TYPE=" /opt/hal/.env || echo "DEVICE_TYPE=\${DEVICE_TYPE}" >> /opt/hal/.env
grep -q "^DEVICES_DIR=" /opt/hal/.env || echo "DEVICES_DIR=\${DEVICES_DIR}" >> /opt/hal/.env

# OpenClaw service — env block matches production OPi exactly.
CHROME_PATH=\$(command -v chromium 2>/dev/null || echo /usr/bin/chromium)
OPENCLAW_BIN=\$(command -v openclaw)
cat > /etc/systemd/system/openclaw.service <<UNIT
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/.openclaw
Environment="OPENCLAW_HOME=/root/.openclaw"
Environment="OPENCLAW_STATE_DIR=/root/.openclaw"
Environment="HOME=/root"
Environment="XDG_CACHE_HOME=/root/.openclaw/.cache"
Environment="XDG_CONFIG_HOME=/root/.openclaw/.config"
Environment="XDG_DATA_HOME=/root/.openclaw/.local/share"
Environment="PUPPETEER_EXECUTABLE_PATH=\$CHROME_PATH"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
Environment="CHROME_BIN=\$CHROME_PATH"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1280x800x24" \$OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# wpa_supplicant@wlan0 override → use per-interface config, not global.
mkdir -p /etc/systemd/system/wpa_supplicant@wlan0.service.d
cat > /etc/systemd/system/wpa_supplicant@wlan0.service.d/override.conf <<'OVR'
[Service]
ExecStart=
ExecStart=/sbin/wpa_supplicant -c /etc/wpa_supplicant/wpa_supplicant-wlan0.conf -i wlan0 -D nl80211,wext
Restart=on-failure
RestartSec=5
OVR

# ── helper scripts (verbatim from production OPi @ 100.111.149.69) ───────────
echo "[stage] helper scripts"

cat > /usr/local/bin/device-ap-mode <<'EOFSCRIPT'
#!/bin/bash
set -e
echo "Switching to AP mode..."
for cmd in ip iw systemctl hostapd dnsmasq rfkill; do
  command -v "\$cmd" >/dev/null 2>&1 || { echo "Missing required command: \$cmd"; exit 1; }
done
rfkill unblock wlan 2>/dev/null || true
rfkill unblock wlan0 2>/dev/null || true
systemctl stop wpa_supplicant@wlan0 2>/dev/null || true
systemctl disable wpa_supplicant@wlan0 2>/dev/null || true
systemctl mask wpa_supplicant@wlan0 2>/dev/null || true
killall wpa_supplicant 2>/dev/null || true
systemctl stop dhcpcd 2>/dev/null || true
systemctl disable dhcpcd 2>/dev/null || true
systemctl stop NetworkManager systemd-networkd 2>/dev/null || true
rm -f /var/lib/dhcpcd5/dhcpcd-wlan0 2>/dev/null || true
rm -f /var/lib/dhcpcd/dhcpcd-wlan0 2>/dev/null || true

# SSID suffix from hardware serial (Pi family) or eth MAC (OPi family).
SERIAL=\$(tr -d '\0' </proc/device-tree/serial-number 2>/dev/null || true)
if [ -z "\$SERIAL" ]; then
  SERIAL=\$(awk '/^Serial/ {print \$3}' /proc/cpuinfo 2>/dev/null || true)
fi
if [ -z "\$SERIAL" ]; then
  for iface in eth0 end0; do
    mac=\$(cat "/sys/class/net/\$iface/address" 2>/dev/null | tr -d ':' || true)
    if [ -n "\$mac" ] && [ "\$mac" != "000000000000" ]; then
      SERIAL=\$mac
      break
    fi
  done
fi
SUFFIX=\${SERIAL: -4}
SUFFIX_LC=\$(echo "\$SUFFIX" | tr '[:upper:]' '[:lower:]')
# Network identity is device-type-driven: <device_type>-<suffix>, lowercase.
# DEVICE_TYPE is baked here at image-build time (one DEVICE_TYPE = one golden
# image); the suffix resolves at first boot from the hardware serial / eth MAC.
AP_SSID="${DEVICE_TYPE}-\${SUFFIX_LC}"
[ -f /etc/hostapd/hostapd.conf ] && sed -i "s/^ssid=.*/ssid=\${AP_SSID}/" /etc/hostapd/hostapd.conf

# mDNS <device_type>-<suffix>.local so the setup wizard's AP→.local handoff works.
DEVICE_HOSTNAME="${DEVICE_TYPE}-\${SUFFIX_LC}"
hostnamectl set-hostname "\$DEVICE_HOSTNAME" 2>/dev/null || hostname "\$DEVICE_HOSTNAME" || true
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1 \$DEVICE_HOSTNAME/" /etc/hosts
else
  echo "127.0.1.1 \$DEVICE_HOSTNAME" >> /etc/hosts
fi
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true

REG=\$(grep "^country_code=" /etc/hostapd/hostapd.conf 2>/dev/null | cut -d= -f2)
[ -z "\$REG" ] && REG=US
iw reg set "\$REG" 2>/dev/null || true

ip link set wlan0 down; sleep 1
iw dev wlan0 set type __ap
sleep 1
ip link set wlan0 up; sleep 1
ip addr flush dev wlan0
ip addr add 192.168.100.1/24 dev wlan0

command -v resolvconf >/dev/null 2>&1 && resolvconf -d wlan0.dhcp 2>/dev/null || true

grep -q '^address=/#/' /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf 2>/dev/null || echo 'address=/#/192.168.100.1' >> /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf

systemctl unmask hostapd dnsmasq 2>/dev/null || true
systemctl enable hostapd dnsmasq

systemctl restart nginx 2>/dev/null || true
systemctl restart dnsmasq

systemctl restart hostapd; sleep 2
if ! systemctl is-active --quiet hostapd; then
  echo "hostapd failed. Retrying..."
  systemctl restart hostapd; sleep 2
fi
if ! systemctl is-active --quiet hostapd; then
  echo "ERROR: hostapd still not running"
  journalctl -u hostapd -n 50 --no-pager || true
  exit 1
fi
echo "AP MODE ENABLED  SSID=\$AP_SSID  IP=192.168.100.1"
EOFSCRIPT
chmod +x /usr/local/bin/device-ap-mode

cat > /usr/local/bin/device-sta-mode <<'EOFSCRIPT'
#!/bin/bash
set -e
echo "Switching to STA mode..."
for cmd in ip iw systemctl rfkill; do
  command -v "\$cmd" >/dev/null 2>&1 || { echo "Missing required command: \$cmd"; exit 1; }
done
rfkill unblock wlan 2>/dev/null || true
rfkill unblock wlan0 2>/dev/null || true
systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl disable hostapd dnsmasq 2>/dev/null || true
killall hostapd 2>/dev/null || true
killall dnsmasq 2>/dev/null || true
ip link set wlan0 down 2>/dev/null || true; sleep 1
iw dev wlan0 set type managed
ip link set wlan0 up; sleep 1
ip addr flush dev wlan0
sed -i '/static ip_address=192.168.100.1\\/24/d;/nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
sed -i '/^address=\\/#\\//d' /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf 2>/dev/null || true
systemctl unmask wpa_supplicant@wlan0 2>/dev/null || true
systemctl enable wpa_supplicant@wlan0
systemctl restart wpa_supplicant@wlan0
systemctl enable dhcpcd
systemctl restart dhcpcd
echo "Waiting for IP..."; sleep 5
if ip addr show wlan0 | grep -q "inet "; then
  IP=\$(ip -4 addr show wlan0 | grep inet | awk '{print \$2}')
  echo "Connected. IP address: \$IP"
else
  echo "WARNING: wlan0 did not receive an IP address"
fi
systemctl restart avahi-daemon 2>/dev/null || true
echo "STA MODE ENABLED"
EOFSCRIPT
chmod +x /usr/local/bin/device-sta-mode

cat > /usr/local/bin/connect-wifi <<'EOFSCRIPT'
#!/bin/bash
set -e
WPA_CONF="\${WPA_CONF:-/etc/wpa_supplicant/wpa_supplicant-wlan0.conf}"
COUNTRY="\${COUNTRY:-US}"
[ "\$(id -u)" -ne 0 ] && { echo "Run as root or with sudo."; exit 1; }
if [ \$# -eq 0 ]; then read -r -p "SSID: " SSID; read -r -s -p "Password (empty=open): " PASS; echo ""; [ -z "\$SSID" ] && exit 1
elif [ \$# -eq 1 ]; then SSID="\$1"; PASS=""
else SSID="\$1"; PASS="\$2"; fi
ssid_esc="\${SSID//\\\\/\\\\\\\\}"; ssid_esc="\${ssid_esc//\\"/\\\\\\"}"
psk_esc="\${PASS//\\\\/\\\\\\\\}"; psk_esc="\${psk_esc//\\"/\\\\\\"}"
[ -f "\$WPA_CONF" ] && existing_country=\$(grep -E '^country=' "\$WPA_CONF" 2>/dev/null | head -1 | cut -d= -f2) && [ -n "\$existing_country" ] && COUNTRY="\$existing_country"
mkdir -p "\$(dirname "\$WPA_CONF")"
if [ -z "\$PASS" ]; then
  net_block="network={
	ssid=\\"\${ssid_esc}\\"
	key_mgmt=NONE
	scan_ssid=1
}"
else
  net_block="network={
	ssid=\\"\${ssid_esc}\\"
	psk=\\"\${psk_esc}\\"
	scan_ssid=1
}"
fi
cat >"\$WPA_CONF" <<WPA
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
country=\${COUNTRY}
fast_reauth=1
ap_scan=1
\${net_block}
WPA
chmod 600 "\$WPA_CONF"
/usr/local/bin/device-sta-mode
EOFSCRIPT
chmod +x /usr/local/bin/connect-wifi

cat > /usr/local/bin/software-update <<'EOFSCRIPT'
#!/bin/bash
set -e
# Metadata URL comes from the bootstrap worker config (single source of truth,
# baked at image build). An explicit OTA_METADATA_URL env var still overrides it
# for manual/debug runs. No compiled-in default — abort if neither is set.
BOOTSTRAP_JSON="/root/config/bootstrap.json"
if [ -z "\${OTA_METADATA_URL:-}" ]; then
  OTA_METADATA_URL="\$(jq -r '.metadata_url // empty' "\$BOOTSTRAP_JSON" 2>/dev/null || true)"
fi
if [ -z "\$OTA_METADATA_URL" ]; then
  echo "[software-update] ERROR: no metadata_url in \$BOOTSTRAP_JSON and OTA_METADATA_URL unset"
  exit 1
fi
echo "[software-update] OTA metadata: \$OTA_METADATA_URL"
[ "\$(id -u)" -ne 0 ] && { echo "Run as root."; exit 1; }
[ \$# -ne 1 ] && { echo "Usage: software-update <os-server|openclaw|bootstrap|web|hal|claude-desktop-buddy|device>"; exit 1; }
APP="\$1"
case "\$APP" in
  os-server|openclaw|bootstrap|web|hal|claude-desktop-buddy|device) ;;
  *) echo "Unknown app: \$APP. Use os-server, openclaw, bootstrap, web, hal, claude-desktop-buddy, or device."; exit 1 ;;
esac

METADATA_TMP=\$(mktemp)
ZIP_TMP=""
DIR_TMP=""
trap 'rm -f "\$METADATA_TMP" "\$ZIP_TMP"; rm -rf "\$DIR_TMP"' EXIT
curl -fsSL -H "Cache-Control: no-cache" -H "Pragma: no-cache" -o "\$METADATA_TMP" "\$OTA_METADATA_URL" || { echo "Failed to fetch metadata from \$OTA_METADATA_URL"; exit 1; }
if [ "\$APP" = "device" ]; then
  # Device profile lives nested under devices.<type>; resolve THIS device's type.
  DEVICE_TYPE="\$(grep -E '^DEVICE_TYPE=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"
  [ -z "\$DEVICE_TYPE" ] && DEVICE_TYPE="\$(jq -r '.device_type // empty' /root/config/config.json 2>/dev/null)"
  # No lamp fallback: pulling the lamp profile onto a device whose class can't be
  # resolved would overwrite its persona with the wrong one — refuse instead.
  [ -z "\$DEVICE_TYPE" ] && { echo "ERROR: device_type not found in /opt/hal/.env or /root/config/config.json — cannot resolve device profile"; exit 1; }
  VERSION=\$(jq -r --arg t "\$DEVICE_TYPE" '.devices[\$t].version // empty' "\$METADATA_TMP")
  URL=\$(jq -r --arg t "\$DEVICE_TYPE" '.devices[\$t].url // empty' "\$METADATA_TMP")
else
  META_KEY="\$APP"
  VERSION=\$(jq -r --arg a "\$META_KEY" '.[\$a].version // empty' "\$METADATA_TMP")
  URL=\$(jq -r --arg a "\$META_KEY" '.[\$a].url // empty' "\$METADATA_TMP")
fi
[ -z "\$VERSION" ] && { echo "Metadata has no version for \$APP"; exit 1; }

if [ "\$APP" = "os-server" ]; then
  [ -z "\$URL" ] && { echo "Metadata has no url for os-server"; exit 1; }
  ZIP_TMP=\$(mktemp)
  DIR_TMP=\$(mktemp -d)
  curl -fsSL -H "Cache-Control: no-cache" -o "\$ZIP_TMP" "\$URL" || { echo "Failed to download os-server"; exit 1; }
  unzip -o -q "\$ZIP_TMP" -d "\$DIR_TMP"
  BIN=\$(find "\$DIR_TMP" -type f -executable 2>/dev/null | head -1)
  [ -z "\$BIN" ] && BIN=\$(find "\$DIR_TMP" -type f 2>/dev/null | head -1)
  [ -z "\$BIN" ] || [ ! -f "\$BIN" ] && { echo "No binary in os-server zip"; exit 1; }
  cp -f "\$BIN" /usr/local/bin/os-server
  chmod +x /usr/local/bin/os-server
  systemctl restart os-server
  echo "os-server updated to \$VERSION"
elif [ "\$APP" = "bootstrap" ]; then
  [ -z "\$URL" ] && { echo "Metadata has no url for bootstrap"; exit 1; }
  ZIP_TMP=\$(mktemp)
  DIR_TMP=\$(mktemp -d)
  curl -fsSL -H "Cache-Control: no-cache" -o "\$ZIP_TMP" "\$URL" || { echo "Failed to download bootstrap"; exit 1; }
  unzip -o -q "\$ZIP_TMP" -d "\$DIR_TMP"
  BIN=\$(find "\$DIR_TMP" -type f -executable 2>/dev/null | head -1)
  [ -z "\$BIN" ] && BIN=\$(find "\$DIR_TMP" -type f 2>/dev/null | head -1)
  [ -z "\$BIN" ] || [ ! -f "\$BIN" ] && { echo "No binary in bootstrap zip"; exit 1; }
  cp -f "\$BIN" /usr/local/bin/bootstrap-server
  chmod +x /usr/local/bin/bootstrap-server
  systemctl restart bootstrap
  echo "bootstrap updated to \$VERSION"
elif [ "\$APP" = "openclaw" ]; then
  VER="\${VERSION:-latest}"
  npm install -g "openclaw@\${VER}" || { echo "npm install openclaw failed"; exit 1; }
  openclaw plugins install @openclaw/discord@\${VER} --force 2>&1 || echo "[software-update] WARN: discord plugin install failed (non-fatal)"
  openclaw plugins install @openclaw/slack@\${VER} --force 2>&1 || echo "[software-update] WARN: slack plugin install failed (non-fatal)"
  systemctl restart openclaw
  echo "openclaw updated to \$VER"
elif [ "\$APP" = "web" ]; then
  [ -z "\$URL" ] && { echo "Metadata has no url for web"; exit 1; }
  ZIP_TMP=\$(mktemp)
  DIR_TMP=\$(mktemp -d)
  curl -fsSL -H "Cache-Control: no-cache" -o "\$ZIP_TMP" "\$URL" || { echo "Failed to download web"; exit 1; }
  unzip -o -q "\$ZIP_TMP" -d "\$DIR_TMP"
  echo "\$VERSION" > "\$DIR_TMP/VERSION"
  WEB_ROOT="/usr/share/nginx/html/setup"
  rm -rf "\${WEB_ROOT:?}"/*
  cp -a "\$DIR_TMP"/* "\$WEB_ROOT"
  systemctl restart nginx
  echo "web updated to \$VERSION"
elif [ "\$APP" = "hal" ]; then
  [ -z "\$URL" ] && { echo "Metadata has no url for hal"; exit 1; }
  ZIP_TMP=\$(mktemp)
  curl -fsSL -H "Cache-Control: no-cache" -o "\$ZIP_TMP" "\$URL" || { echo "Failed to download hal"; exit 1; }
  HAL_DIR="/opt/hal"
  unzip -o -q "\$ZIP_TMP" -d "\$HAL_DIR"
  UV_BIN=\$(command -v uv || echo "/root/.local/bin/uv")
  find /root/.cache/uv -name "lerobot.egg-info" -type d 2>/dev/null | xargs rm -rf
  cd "\$HAL_DIR" && "\$UV_BIN" sync --python 3.12 --extra hardware || { echo "uv sync failed"; exit 1; }
  cd /
  systemctl restart hal
  echo "hal updated to \$VERSION"
elif [ "\$APP" = "claude-desktop-buddy" ]; then
  [ -z "\$URL" ] && { echo "Metadata has no url for claude-desktop-buddy"; exit 1; }
  ZIP_TMP=\$(mktemp)
  DIR_TMP=\$(mktemp -d)
  curl -fsSL -H "Cache-Control: no-cache" -o "\$ZIP_TMP" "\$URL" || { echo "Failed to download claude-desktop-buddy"; exit 1; }
  BUDDY_DIR="/opt/claude-desktop-buddy"
  mkdir -p "\$BUDDY_DIR"
  unzip -o -q "\$ZIP_TMP" -d "\$DIR_TMP"
  [ -f "\$DIR_TMP/buddy-plugin" ] && cp -f "\$DIR_TMP/buddy-plugin" "\$BUDDY_DIR/buddy-plugin" && chmod +x "\$BUDDY_DIR/buddy-plugin"
  [ ! -f "/root/config/buddy.json" ] && [ -f "\$DIR_TMP/config/buddy.json" ] && mkdir -p /root/config && cp -f "\$DIR_TMP/config/buddy.json" /root/config/buddy.json
  echo "\$VERSION" > "\$BUDDY_DIR/VERSION_BUDDY"
  systemctl restart claude-desktop-buddy
  echo "claude-desktop-buddy updated to \$VERSION"
elif [ "\$APP" = "device" ]; then
  [ -z "\$URL" ] && { echo "Metadata has no url for devices.\$DEVICE_TYPE"; exit 1; }
  DEVICES_DIR="\$(grep -E '^DEVICES_DIR=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"
  [ -z "\$DEVICES_DIR" ] && DEVICES_DIR="/opt/devices"
  DEST="\$DEVICES_DIR/\$DEVICE_TYPE"
  ZIP_TMP=\$(mktemp)
  curl -fsSL -H "Cache-Control: no-cache" -o "\$ZIP_TMP" "\$URL" || { echo "Failed to download device profile"; exit 1; }
  mkdir -p "\$DEST"
  unzip -o -q "\$ZIP_TMP" -d "\$DEST"
  rm -f "\$ZIP_TMP"
  # Re-apply the device rootfs overlay onto / before services restart.
  [ -d "\$DEST/rootfs" ] && cp -a "\$DEST/rootfs/." /
  systemctl restart os-server 2>/dev/null || true
  systemctl restart hal 2>/dev/null || true
  echo "device profile (\$DEVICE_TYPE) updated to \$VERSION"
fi
EOFSCRIPT
chmod +x /usr/local/bin/software-update

# ── network configs (hostapd, dnsmasq, wpa, dhcpcd) ──────────────────────────
echo "[stage] network configs"

# wpa_supplicant: country-only baseline. connect-wifi overwrites with creds.
mkdir -p /etc/wpa_supplicant
cat > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf <<EOF
country=\${COUNTRY_CODE}
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
EOF
chmod 600 /etc/wpa_supplicant/wpa_supplicant-wlan0.conf

# hostapd: SSID placeholder, device-ap-mode replaces at runtime.
if [ "\${AP_BAND}" = "5" ]; then
  CHANNEL="\${AP_CHANNEL:-36}"
  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${DEVICE_TYPE}-xxxx
hw_mode=a
channel=\$CHANNEL
country_code=\${COUNTRY_CODE}
ieee80211n=1
ieee80211ac=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
else
  CHANNEL="\${AP_CHANNEL:-6}"
  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${DEVICE_TYPE}-xxxx
hw_mode=g
channel=\$CHANNEL
country_code=\${COUNTRY_CODE}
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
fi
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd

mkdir -p /etc/dnsmasq.d
cat > /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf <<'EOF'
interface=wlan0
bind-interfaces
dhcp-range=wlan0,192.168.100.50,192.168.100.150,255.255.255.0,24h
address=/#/192.168.100.1
domain-needed
bogus-priv
no-resolv
EOF
[ -f /etc/dnsmasq.conf ] && sed -i 's/^interface=wlan0/#&/' /etc/dnsmasq.conf || true

systemctl mask wpa_supplicant.service 2>/dev/null || true

# Advertise _autonomous._tcp via mDNS so the Autonomous Buddy (macOS) auto-finds
# this device. Static + device-agnostic: avahi's %h wildcard = the running
# hostname (<device_type>-<suffix>), so one baked file serves every device class.
# Port 80 = the nginx front door the buddy pairs through (/api/buddy/pair/confirm).
mkdir -p /etc/avahi/services
cat > /etc/avahi/services/autonomous.service <<'AVAHI'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_autonomous._tcp</type>
    <port>80</port>
  </service>
</service-group>
AVAHI

# ── nginx config (verbatim from production OPi) ──────────────────────────────
echo "[stage] nginx"
rm -f /etc/nginx/sites-enabled/default
cat > /etc/nginx/sites-enabled/default <<'NGINX'
upstream backend  { server 127.0.0.1:5000; }
upstream hal   { server 127.0.0.1:5001; }
upstream openclaw { server 127.0.0.1:18789; }

server {
  listen 80 default_server;
  root /usr/share/nginx/html/setup;
  index index.html;
  client_max_body_size 20M;

  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'" always;

  location / { try_files \$uri /index.html; }

  location = /api/system/shell {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location = /openapi.json {
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  location = /api/system/exec {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  location = /api/buddy/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location /api/ {
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  location /hw/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://hal/;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /hw;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
  }

  location = /gw {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://openclaw/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
  }

  location /gw/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://openclaw/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
  }

  location = /generate_204       { return 204; }
  location = /hotspot-detect.html { return 204; }
  location = /ncsi.txt            { return 204; }
  location = /connecttest.txt     { return 204; }
}
NGINX
mkdir -p /usr/share/nginx/html/setup
echo '<h1>Device setup — flash the device and reboot.</h1>' > /usr/share/nginx/html/setup/index.html

# ── PulseAudio: WebRTC echo cancel + udev ignore for I2S codecs ──────────────
echo "[stage] PulseAudio"
PULSE_CONF="/etc/pulse/default.pa"
if [ -f "\$PULSE_CONF" ] && ! grep -q "module-echo-cancel" "\$PULSE_CONF"; then
  cat >> "\$PULSE_CONF" <<'PULSE_EOF'

### Echo cancellation (WebRTC AEC)
load-module module-echo-cancel source_name=aec_source sink_name=aec_sink aec_method=webrtc aec_args="analog_gain_control=0 digital_gain_control=0" channels=1
set-default-source aec_source
set-default-sink aec_sink
load-module module-native-protocol-unix auth-anonymous=1 socket=/tmp/pulse-anon-${DEVICE_TYPE}
PULSE_EOF
fi

# 91-pulseaudio-hal-ignore.rules — hardware team bakes udev rules into the base image.
# cat > /etc/udev/rules.d/91-pulseaudio-hal-ignore.rules <<'UDEV_EOF'
# # Keep PulseAudio away from the onboard I2S codecs so hal can own them.
# SUBSYSTEM=="sound", ATTR{id}=="sndi2s4", ENV{PULSE_IGNORE}="1"
# SUBSYSTEM=="sound", ATTR{id}=="wm8960soundcard", ENV{PULSE_IGNORE}="1"
# UDEV_EOF

# ── ALSA ─────────────────────────────────────────────────────────────────────
# /etc/asound.conf is hardware-team-owned and baked into the base image.
# It is NOT shipped in the device profile overlay.

# ── disable conflicting vendor services ──────────────────────────────────────
echo "[stage] mask conflicting vendor services"
systemctl mask orangepi-firstrun-config.service 2>/dev/null || true

# ── enable services (symlink, since chroot has no running systemd) ──────
echo "[stage] enable services"
for unit in os-server bootstrap hal openclaw avahi-daemon bluetooth ssh; do
  systemctl enable "\$unit" 2>/dev/null || true
done

# ── SPI3 overlay for WS2812 RGB LED ring (OrangePi 4 Pro A733) ───────────────
echo "[stage] enable SPI3 overlay for LED ring"
if grep -q "^overlays=" /boot/orangepiEnv.txt 2>/dev/null; then
  sed -i "s/^overlays=.*/& spi3-cs0-cs1-spidev/" /boot/orangepiEnv.txt
else
  echo "overlays=spi3-cs0-cs1-spidev" >> /boot/orangepiEnv.txt
fi

echo "[stage] chroot Phase 2 complete"
CHROOT_STAGES

# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — OTA bake: backend binaries + hal + web UI + buddy
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 3 — OTA bake (OS binaries from metadata.json)"

chroot "${MNT}" /bin/bash <<OVERLAY_STAGES
set -euo pipefail
trap 'echo "OVERLAY ERROR: command failed at line \$LINENO (exit \$?): \$BASH_COMMAND"' ERR
export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:\$PATH"
export DEVICE_TYPE="${DEVICE_TYPE}"
export DEVICES_DIR="${DEVICES_DIR}"

retry() {
  local cmd="\$1" max="\${2:-5}" delay="\${3:-3}" n=0
  until [ "\$n" -ge "\$max" ]; do
    eval "\$cmd" && return 0
    n=\$((n + 1))
    sleep "\$delay"
  done
  return 1
}

install_binary_from_zip() {
  local url="\$1" dest="\$2" name="\$3"
  local ztmp dtmp
  ztmp=\$(mktemp); dtmp=\$(mktemp -d)
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$ztmp' '\$url'" 5
  unzip -o -q "\$ztmp" -d "\$dtmp"; rm -f "\$ztmp"
  local bin
  bin=\$(find "\$dtmp" -type f -executable 2>/dev/null | head -1)
  [ -z "\$bin" ] && bin=\$(find "\$dtmp" -type f 2>/dev/null | head -1)
  [ -z "\$bin" ] && { echo "ERROR: no binary in \$url"; exit 1; }
  cp -f "\$bin" "\$dest"; chmod +x "\$dest"; rm -rf "\$dtmp"
  echo "[overlay] installed \$name → \$dest"
}

echo "[overlay] fetch OTA metadata"
META=\$(mktemp)
retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$META' '${OTA_METADATA_URL}'" 5
WEB_URL=\$(jq -r '.web.url // empty'               "\$META")
OS_SERVER_URL=\$(jq -r '."os-server".url // empty'             "\$META")
BOOTSTRAP_URL=\$(jq -r '.bootstrap.url // empty'   "\$META")
HAL_URL=\$(jq -r '.hal.url // empty'         "\$META")
BUDDY_URL=\$(jq -r '."claude-desktop-buddy".url // empty' "\$META")
DEVICES_URL=\$(jq -r --arg t "\$DEVICE_TYPE" '.devices[\$t].url // empty' "\$META")
WEB_VER=\$(jq -r '.web.version // empty'           "\$META")
OS_SERVER_VER=\$(jq -r '."os-server".version // empty'         "\$META")
BOOTSTRAP_VER=\$(jq -r '.bootstrap.version // empty' "\$META")
HAL_VER=\$(jq -r '.hal.version // empty'     "\$META")
BUDDY_VER=\$(jq -r '."claude-desktop-buddy".version // empty' "\$META")
rm -f "\$META"
[ -z "\$WEB_URL" ] || [ -z "\$OS_SERVER_URL" ] || [ -z "\$BOOTSTRAP_URL" ] && {
  echo "ERROR: OTA metadata missing web.url / os-server.url / bootstrap.url"; exit 1
}
echo "[overlay] web=\$WEB_VER os-server=\$OS_SERVER_VER bootstrap=\$BOOTSTRAP_VER hal=\$HAL_VER buddy=\$BUDDY_VER"

echo "[overlay] backend binaries"
install_binary_from_zip "\$BOOTSTRAP_URL" /usr/local/bin/bootstrap-server "bootstrap"
install_binary_from_zip "\$OS_SERVER_URL"      /usr/local/bin/os-server      "os-server"

echo "[overlay] HAL"
HAL_DIR="/opt/hal"
if [ -n "\$HAL_URL" ]; then
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/hal.zip '\$HAL_URL'" 5
  unzip -o -q /tmp/hal.zip -d "\$HAL_DIR"
  rm -f /tmp/hal.zip
  # If zip nested into subdir, hoist up.
  if [ ! -f "\$HAL_DIR/pyproject.toml" ]; then
    SUBDIR=\$(find "\$HAL_DIR" -maxdepth 2 -name pyproject.toml 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
    [ -n "\$SUBDIR" ] && [ "\$SUBDIR" != "\$HAL_DIR" ] && \\
      { shopt -s dotglob 2>/dev/null || true; mv "\$SUBDIR"/* "\$HAL_DIR"/; shopt -u dotglob 2>/dev/null || true; }
  fi
  find /root/.cache/uv -name 'lerobot.egg-info' -type d 2>/dev/null | xargs -r rm -rf || true
  rm -rf "\$HAL_DIR/.venv"
  cd "\$HAL_DIR"
  uv sync --python 3.12 --extra hardware
  # webrtcvad pkg_resources patch (Py3.12+ killed pkg_resources).
  WEBRTCVAD_PY=\$(find "\$HAL_DIR/.venv" -name "webrtcvad.py" -path "*/site-packages/*" 2>/dev/null | head -1)
  if [ -n "\$WEBRTCVAD_PY" ] && grep -q "import pkg_resources" "\$WEBRTCVAD_PY"; then
    cat > "\$WEBRTCVAD_PY" <<'WEBRTCVAD_EOF'
try:
    import pkg_resources
    __version__ = pkg_resources.get_distribution('webrtcvad').version
except Exception:
    __version__ = '2.0.10'

import _webrtcvad

class Vad(object):
    def __init__(self, mode=None):
        self._vad = _webrtcvad.create()
        _webrtcvad.init(self._vad)
        if mode is not None:
            self.set_mode(mode)
    def set_mode(self, mode):
        _webrtcvad.set_mode(self._vad, mode)
    def is_speech(self, buf, sample_rate, length=None):
        length = length or int(len(buf) / 2)
        if length * 2 > len(buf):
            raise IndexError('buffer has %s frames, but length argument was %s' % (int(len(buf) / 2.0), length))
        return _webrtcvad.process(self._vad, sample_rate, buf, length)

def valid_rate_and_frame_length(rate, frame_length):
    return _webrtcvad.valid_rate_and_frame_length(rate, frame_length)
WEBRTCVAD_EOF
  fi
  cd /
else
  echo "[overlay] WARN: no hal URL — skipping"
fi

echo "[overlay] device profile (\$DEVICE_TYPE)"
# Bake the device profile so one DEVICE_TYPE = one golden image. The chroot's
# root is the image rootfs, so \$DEVICES_DIR/\$DEVICE_TYPE/ is the in-image path
# (same convention as /opt/hal above). No URL → WARN + skip, never fail.
if [ -n "\$DEVICES_URL" ]; then
  DEVICE_PROFILE_DIR="\$DEVICES_DIR/\$DEVICE_TYPE"
  mkdir -p "\$DEVICE_PROFILE_DIR"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/device-profile.zip '\$DEVICES_URL'" 5
  unzip -o -q /tmp/device-profile.zip -d "\$DEVICE_PROFILE_DIR"
  rm -f /tmp/device-profile.zip
  echo "[overlay] device profile baked → \$DEVICE_PROFILE_DIR"
  # Device rootfs overlay: devices/<type>/rootfs/ mirrors the target filesystem.
  # Copy the whole tree onto / for device-specific system config (udev rules, …).
  if [ -d "\$DEVICE_PROFILE_DIR/rootfs" ]; then
    cp -a "\$DEVICE_PROFILE_DIR/rootfs/." /
    echo "[overlay] device rootfs overlay applied"
  else
    echo "[overlay] WARN: device profile has no rootfs/ overlay"
  fi
else
  echo "[overlay] ERROR: no devices.\$DEVICE_TYPE url in OTA metadata — device profile is required (one image = one device type). Run 'make upload-device \$DEVICE_TYPE' before building." >&2
  exit 1
fi

echo "[overlay] web UI"
retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/web.zip '\$WEB_URL'" 5
rm -rf /usr/share/nginx/html/setup/*
unzip -o -q /tmp/web.zip -d /usr/share/nginx/html/setup
rm -f /tmp/web.zip

if [ -n "\$BUDDY_URL" ]; then
  echo "[overlay] Claude Desktop Buddy"
  BUDDY_DIR="/opt/claude-desktop-buddy"
  mkdir -p "\$BUDDY_DIR" /root/config
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/buddy.zip '\$BUDDY_URL'" 5
  unzip -o -q /tmp/buddy.zip -d /tmp/buddy-extract
  rm -f /tmp/buddy.zip
  if [ -f /tmp/buddy-extract/buddy-plugin ]; then
    cp -f /tmp/buddy-extract/buddy-plugin "\$BUDDY_DIR/buddy-plugin"
    chmod +x "\$BUDDY_DIR/buddy-plugin"
  fi
  [ ! -f /root/config/buddy.json ] && [ -f /tmp/buddy-extract/config/buddy.json ] && \\
    cp -f /tmp/buddy-extract/config/buddy.json /root/config/buddy.json
  echo "\$BUDDY_VER" > "\$BUDDY_DIR/VERSION_BUDDY"
  rm -rf /tmp/buddy-extract
  cat > /etc/systemd/system/claude-desktop-buddy.service <<'UNIT'
[Unit]
Description=Claude Desktop Buddy (BLE)
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/claude-desktop-buddy
ExecStart=/opt/claude-desktop-buddy/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-desktop-buddy

[Install]
WantedBy=multi-user.target
UNIT
  systemctl enable claude-desktop-buddy
else
  echo "[overlay] no buddy URL — skipping"
fi

echo "[overlay] Phase 3 complete"

# Persist OTA versions to a file inside the image; host script reads it back
# out after chroot exits to build the manifest. Key=value format so a shell
# 'source' on the host pulls them into variables.
cat > /tmp/ota-versions.env <<MANIFEST
WEB_VER=\${WEB_VER}
OS_SERVER_VER=\${OS_SERVER_VER}
BOOTSTRAP_VER=\${BOOTSTRAP_VER}
HAL_VER=\${HAL_VER}
BUDDY_VER=\${BUDDY_VER}
MANIFEST
OVERLAY_STAGES

# Capture OTA versions for the build manifest before they get wiped by Phase 5.
BAKED_WEB_VER=""; BAKED_OS_SERVER_VER=""; BAKED_BOOTSTRAP_VER=""; BAKED_HAL_VER=""; BAKED_BUDDY_VER=""
if [ -f "${MNT}/tmp/ota-versions.env" ]; then
  # shellcheck disable=SC1090
  . "${MNT}/tmp/ota-versions.env" || true
  BAKED_WEB_VER="${WEB_VER:-}"
  BAKED_OS_SERVER_VER="${OS_SERVER_VER:-}"
  BAKED_BOOTSTRAP_VER="${BOOTSTRAP_VER:-}"
  BAKED_HAL_VER="${HAL_VER:-}"
  BAKED_BUDDY_VER="${BUDDY_VER:-}"
  rm -f "${MNT}/tmp/ota-versions.env"
fi

# Write the build manifest. Makefile `upload` target reads this to populate
# the per-release note with OTA versions actually baked in.
SRC_7Z_SHA=$(sha256sum "${SRC_7Z}" 2>/dev/null | cut -d' ' -f1 || echo unknown)
cat > /output/manifest-opi.json <<MANIFEST_JSON
{
  "build_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "target": "opi",
  "openclaw_version": "${OPENCLAW_VERSION}",
  "out_img_size": "${OUT_IMG_SIZE}",
  "ota_metadata_url": "${OTA_METADATA_URL}",
  "ota_versions": {
    "web": "${BAKED_WEB_VER}",
    "os-server": "${BAKED_OS_SERVER_VER}",
    "bootstrap": "${BAKED_BOOTSTRAP_VER}",
    "hal": "${BAKED_HAL_VER}",
    "claude-desktop-buddy": "${BAKED_BUDDY_VER}"
  },
  "source_image": {
    "file_id": "${OPI_FILE_ID}",
    "name": "${OPI_FILE_NAME}.7z",
    "sha256": "${SRC_7Z_SHA}"
  }
}
MANIFEST_JSON
log "Manifest: /output/manifest-opi.json"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — Install resize-once.service (first-boot SD-fill expand)
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 4 — resize-once (first-boot expand)"

cat > "${MNT}/usr/local/bin/resize-once" <<'RESIZE_EOF'
#!/bin/bash
# Runs ONCE at first boot. Expands root partition + ext4 to fill the SD card,
# then disables itself. Compares root partition device to deduce the parent
# disk + partition number — works for mmcblk1p1 (SD), mmcblk0p1 (eMMC), etc.
set -uo pipefail
log() { echo "==> $*"; }
ROOT_PART=$(findmnt -n -o SOURCE /)
[ -z "${ROOT_PART}" ] && { echo "ERROR: cannot determine root partition"; exit 1; }
log "root partition: ${ROOT_PART}"
# Strip /dev/, then split into disk + part number.
DEV_NAME=$(basename "${ROOT_PART}")
case "${DEV_NAME}" in
  mmcblk*p*|nvme*p*) PARENT="${DEV_NAME%p*}"; PART_NUM="${DEV_NAME##*p}" ;;
  sd?[0-9]*)         PARENT="${DEV_NAME%%[0-9]*}"; PART_NUM="${DEV_NAME##*[a-z]}" ;;
  *) echo "ERROR: unrecognised root device naming ${DEV_NAME}"; exit 1 ;;
esac
DISK="/dev/${PARENT}"
log "parent disk=${DISK} partition=${PART_NUM}"

# growpart needs the partition unmounted-ish; on a mounted root this works
# because growpart only edits the partition table, not data blocks.
growpart "${DISK}" "${PART_NUM}" || { log "growpart already at max — nothing to do"; }
resize2fs "${ROOT_PART}" || { log "WARN resize2fs failed"; }
log "resize complete"

# Self-disable so this service never runs again, even if image is re-cloned.
systemctl disable resize-once.service 2>/dev/null || true
rm -f /etc/systemd/system/resize-once.service
rm -f /etc/systemd/system/multi-user.target.wants/resize-once.service
rm -f /usr/local/bin/resize-once
RESIZE_EOF
chmod +x "${MNT}/usr/local/bin/resize-once"

cat > "${MNT}/etc/systemd/system/resize-once.service" <<'UNIT'
[Unit]
Description=Expand root filesystem to fill SD card on first boot (self-destructing)
ConditionPathExists=/usr/local/bin/resize-once
DefaultDependencies=no
After=local-fs.target systemd-remount-fs.service
Before=basic.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/resize-once

[Install]
WantedBy=multi-user.target
UNIT

# Manually link into wants (systemctl enable inside chroot also works, but we
# already exited the chroot — symlink is the equivalent + no DBus needed).
mkdir -p "${MNT}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/resize-once.service \
  "${MNT}/etc/systemd/system/multi-user.target.wants/resize-once.service"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 — Restore resolv.conf, unmount, compress
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 5 — finalize"

mv -f "${MNT}/etc/resolv.conf.bak" "${MNT}/etc/resolv.conf" 2>/dev/null || true

# Kill any stale chroot processes (apt post-install spawns dbus/sshd sometimes).
for pid in $(lsof -t +D "${MNT}" 2>/dev/null || true); do
  kill -9 "$pid" 2>/dev/null || true
done
fuser -k -M "${MNT}" 2>/dev/null || true
rm -f "${MNT}/run/sshd.pid" "${MNT}/run/dbus/pid" 2>/dev/null || true
rm -rf "${MNT}/run/lock"/* 2>/dev/null || true
rm -f "${MNT}/usr/bin/qemu-aarch64-static"

umount "${MNT}/dev"
umount "${MNT}/sys"
umount "${MNT}/proc"

# Flush + unmount root before xz so the on-disk filesystem is consistent.
sync
umount "${MNT}"
losetup -d "${LOOP_DEV}"; LOOP_DEV=""

log "Compressing ${OUT_IMG} → ${OUT_IMG}.xz (this takes a few minutes)…"
rm -f "${OUT_IMG}.xz"
# -k keeps the original .img alongside the .xz so operator can verify/inspect
# or flash raw before deciding to delete. Manual cleanup: rm -f output/golden-opi.img
xz -9 -k --threads=0 "${OUT_IMG}"

log "DONE: ${OUT_IMG}.xz ($(du -h "${OUT_IMG}.xz" | cut -f1))"
log "Flash:  make sd-card-flash DISK=N    (decompresses on the fly via xz | dd)"
