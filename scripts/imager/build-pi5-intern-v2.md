# Build V2 image cho Raspberry Pi 5 (Intern V1 hardware)

Guide build một `.img` chứa Autonomous OS V2 stack (openclaw + hermes + codex +
opencode + claudecode + picoclaw + HAL) chạy trên **Raspberry Pi 5** — hardware
mà fleet Intern V1 đang dùng, thay vì OrangePi 4 Pro (target mặc định của
intern-v2).

Không cần sửa `build-pi5.sh` hay `build-orangepi.sh` — script hiện tại đã handle
đầy đủ cặp `TARGET=rpi + RPI_MODEL=5 + DEVICE_TYPE=intern-v2`. README chấm
status "working".

---

## 1. Prerequisites

Chạy trên Mac host:

```bash
# 1. Docker Desktop đã start
docker info >/dev/null 2>&1 && echo "Docker OK" || echo "Start Docker Desktop trước!"

# 2. Disk free ≥ 30 GB
df -h /Users/hoangphuong | tail -1

# 3. Docker memory ≥ 8 GB (Docker Desktop → Settings → Resources → Memory)
docker info --format '{{.MemTotal}}' | awk '{printf "%.1f GB\n", $1/1024/1024/1024}'

# 4. OTA metadata URL prod V2 — cùng URL fleet OrangePi đang dùng
#    Prod (default cho fleet):   https://cdn.autonomous.ai/os/ota/metadata.json
#    Nguồn: scripts/release/tag-release.sh:14 + scripts/release/ota-config.sh
#    Staging (nếu team có): thường thay 'os' → 'os-staging' hoặc như ota-config.sh define

# 5. Tạo thư mục input/ + output/ (bị .gitignore, không có sẵn khi clone fresh)
#    Lần đầu build sẽ fail ở "cp ../provision/software-update input/..." nếu thiếu.
#    Chạy ở path scripts/imager/ (cùng chỗ chạy `make build` — Makefile dùng path tương đối).
cd /Users/hoangphuong/Dropbox/autonomous/interns/autonomous/scripts/imager
mkdir -p input output
```

---

## 2. Command build

```bash
cd /Users/hoangphuong/Dropbox/autonomous/interns/autonomous/scripts/imager

# Pi 5 V1 (case đầu, KHÔNG loa/mic): set PI5_NO_AUDIO=1 để LED không vàng
make build \
  TARGET=rpi \
  RPI_MODEL=5 \
  DEVICE_TYPE=intern-v2 \
  PI5_NO_AUDIO=1 \
  OTA_METADATA_URL="https://cdn.autonomous.ai/os/ota/metadata.json"
```

**Params**:
| Env | Vai trò |
|---|---|
| `TARGET=rpi` | dùng `build-pi5.sh` (Pi builder) thay vì `build-orangepi.sh` |
| `RPI_MODEL=5` | Pi 5 branch — dùng Raspberry Pi OS Lite Trixie (Debian 13) |
| `DEVICE_TYPE=intern-v2` | Pre-bake full V2 stack: openclaw + hermes + codex + opencode + claudecode + picoclaw + HAL |
| `OTA_METADATA_URL` | URL prod V2 metadata → baked vào `/root/config/bootstrap.json` → bootstrap-server tự pull update sau đó |
| `PI5_NO_AUDIO=1` (**optional**) | Chỉ set cho Pi 5 V1 hardware (không có loa/mic). Strip `audio:` khỏi ROBOT.md → healthwatch skip audio check (LED không vàng), HAL skip PortAudio init. Bỏ đi nếu build cho hardware có loa/mic. |

---

## 3. Output

Sau khi build xong (~30-40 phút lần đầu):

```
output/
├── base.img                 # Cache Phase 1 — giữ để build sau nhanh (~4 GB)
├── golden.img               # Raw output (~8 GB) — Makefile tự mv sang tên dưới
└── golden-intern-v2.img     # Final image dùng để flash
```

Full path final image:
```
/Users/hoangphuong/Dropbox/autonomous/interns/autonomous/scripts/imager/output/golden-intern-v2.img
```

### Watch tiến độ (trong khi build đang chạy)

Mở terminal khác:

```bash
# Xem realtime container đang chạy stage nào
docker ps --filter ancestor=pi-builder

# Log realtime của container
docker logs -f $(docker ps -q --filter ancestor=pi-builder | head -1)

# Hoặc check output/ dir size tăng dần
watch -n 5 'ls -lah /Users/hoangphuong/Dropbox/autonomous/interns/autonomous/scripts/imager/output/'
```

### Console output khi build xong

```
✅ /output/golden.img ready (all QC checks passed)
   Size:     8G (expands to fill SD on first boot)
   User:     system / 12345
   Hostname: intern-v2-xxxx
```

Ngay sau đó Makefile tự `mv golden.img → golden-intern-v2.img`. File ~8 GB, flash
trực tiếp qua balenaEtcher hoặc `make sd-card-flash-raw TARGET=rpi DEVICE_TYPE=intern-v2 DISK=N`.

Lần build lại (giữ `base.img`): **~2-3 phút** — chỉ chạy Phase 2 overlay.

Force rebuild base:
```bash
rm output/base.img
make build TARGET=rpi RPI_MODEL=5 DEVICE_TYPE=intern-v2 OTA_METADATA_URL=...
```

---

## 4. Flash SD card

```bash
# Liệt kê disk
make sd-card-list

# Flash raw (nhanh, skip xz — cần disk N number từ list)
make sd-card-flash-raw TARGET=rpi DEVICE_TYPE=intern-v2 DISK=N
```

Hoặc flash bằng UI: mở **balenaEtcher** hoặc **Raspberry Pi Imager**, chọn file
`output/golden-intern-v2.img`, chọn SD card, Flash.

Yêu cầu SD ≥ 16 GB (image 8 GB, tự expand `/` khi first boot chiếm hết SD).

---

## 5. First boot verify

Cắm SD vào Pi 5 → cắm điện → sau 30-60 giây device lên AP mode.

```bash
# AP SSID: intern-v2-xxxx (xxxx = 4 hex cuối MAC)
# Password AP: xem trong /etc/hostapd/hostapd.conf hoặc default
# Static IP: 192.168.100.1
```

SSH:
```bash
ssh system@192.168.100.1   # password: 12345
```

Verify services chạy:
```bash
systemctl status os-server hal openclaw bootstrap
```

Kỳ vọng:
- `os-server.service` — **active (running)**
- `bootstrap.service` — **active (running)** (poll OTA)
- `openclaw.service` — **active (running)**
- `hal.service` — **active (running)** hoặc **degraded** (nếu không có audio hardware, xem section 6)

Setup device flow: giống OrangePi — vào web autonomous portal → tạo device →
QR/link 192.168.100.1 → connect Wi-Fi → device lên online.

---

## 6. Caveat cho Intern V1 hardware (Pi 5 không loa/mic)

Intern V1 device (Pi 5 case pyramid) **KHÔNG có speaker + mic**.

**Fix**: build với `PI5_NO_AUDIO=1` (xem §2). Khi set:
- `build-pi5.sh` strip line `audio:` khỏi `/opt/devices/intern-v2/ROBOT.md` sau khi fetch device profile
- Code check `device.Has(devType, CapAudio)` → false → healthwatch skip audio component → **LED không vàng**
- HAL skip khởi tạo PortAudio + Gemini realtime voice → không còn error log `sounddevice.PortAudioError` / `Sample format not supported`

Ảnh hưởng thực tế:
- ✅ LLM chat qua Telegram/Slack/Discord — hoạt động (không dùng audio)
- ✅ Web setup + OTA — hoạt động
- ✅ Status LED clean (không hardware warning)
- ❌ Voice chat / wake word — không hoạt động (không có mic)
- ❌ TTS speak — không có tiếng ra (không có loa)

Nếu quên set `PI5_NO_AUDIO=1` khi build cho hardware không loa/mic, symptoms:
- LED vàng đứng (healthwatch state=hardware)
- os-server log spam mỗi 5s: `WARN hardware component failure audio=false voice=true`
- HAL log spam: `sounddevice.PortAudioError: Error opening OutputStream: Sample format not supported`

Fix on-device (không rebuild image):
```bash
ssh system@<device-ip>
# Repo dùng ROBOT.md, artifact CDN ship dưới tên DEVICE.md — thử cả 2
for f in /opt/devices/intern-v2/ROBOT.md /opt/devices/intern-v2/DEVICE.md; do
  [ -f "$f" ] && sudo sed -i '/^[[:space:]]*audio:/d' "$f"
done
sudo systemctl restart os-server hal
# Verify: sẽ thấy HAL log "Voice service skipped — 'voice' not declared in ROBOT.md"
sudo journalctl -u os-server --since "30 seconds ago" | grep -c "hardware component failure"   # phải = 0
```

Các phần cứng khác trên Pi 5 (Wi-Fi, GPU, USB, mDNS) — driver chuẩn Raspberry
Pi OS đã có sẵn, không cần patch.

---

## 7. Chọn agent runtime mặc định (openclaw / hermes / codex / ...)

Image intern-v2 bake sẵn **6 runtime binaries** (openclaw + hermes + codex +
opencode + claudecode + picoclaw) — chỉ **1 cái active** lúc boot.

### Mặc định

Command build hiện tại (không set `DEFAULT_AGENT`) → fallback về
`robots/intern-v2/ROBOT.md` → **`gateway.default: openclaw`**.

Nguồn: `scripts/imager/build-pi5.sh:1464` (`"openclaw is the default active runtime"`)
+ `build-pi5.sh:2160` (`DEFAULT_AGENT unset → fallback ROBOT.md gateway.default`).

### Build với runtime khác

Set `DEFAULT_AGENT` khi chạy make:

```bash
make build \
  TARGET=rpi \
  RPI_MODEL=5 \
  DEVICE_TYPE=intern-v2 \
  DEFAULT_AGENT=hermes \
  OTA_METADATA_URL="https://cdn.autonomous.ai/os/ota/metadata.json"
```

Values hợp lệ: `openclaw` | `hermes` | `codex` | `opencode` | `claudecode` | `picoclaw`

Giá trị này bake vào `/root/config/f_r_default_agent` — sau F_R (factory reset)
device seed lại đúng runtime này (không tụt về openclaw).

### Side-effect: SSH policy (build-pi5.sh:2172)

Chỉ áp dụng cho `DEVICE_TYPE=intern-v2`:

| DEFAULT_AGENT | SSH lúc first boot |
|---|---|
| `claudecode` (Developer Edition, black case) | **Mở** |
| `openclaw` / `hermes` / `codex` / ... (consumer edition) | **Đóng** |
| unset (fallback ROBOT.md) | **Mở** (không match consumer gate) |

### Switch runtime sau khi flash (không cần rebuild)

Device đã có full 6 binaries — đổi live qua SSH:

```bash
ssh system@192.168.100.1   # 12345
sudo bash -c 'echo hermes > /root/config/f_r_default_agent'
sudo systemctl restart os-server
```

Hoặc qua web setup UI khi lên online (nếu bản đó có UI switch backend).

---

## 8. OTA update sau flash

`bootstrap-server` (đã bake trong image) sẽ:
1. Đọc `metadata_url` từ `/root/config/bootstrap.json` (baked lúc build)
2. Poll URL này mỗi vài phút
3. Pull update mới từ prod fleet → apply tự động

Không cần config thêm gì. Cùng URL prod V2 fleet OrangePi đang dùng → Pi 5
sẽ nhận cùng update stream.

Verify OTA đang chạy:
```bash
sudo cat /root/config/bootstrap.json
journalctl -u bootstrap -n 20
```

---

## 9. Không đụng flow OrangePi

Command mặc định (không truyền `TARGET`) vẫn chạy OrangePi build như cũ:

```bash
make build DEVICE_TYPE=intern-v2 OTA_METADATA_URL=...
# → TARGET=opi (default) → build-orangepi.sh → OrangePi image
```

Chỉ khi truyền **`TARGET=rpi`** thì mới chạy `build-pi5.sh` (Pi 5 branch). Zero
impact tới OrangePi build.

---

## 10. Troubleshoot common

### DNS chết sau khi user setup Wi-Fi (device không call được backend)
Symptom: sau khi user setup Wi-Fi qua web UI, device có IP thật (VD `172.168.20.248`)
+ ping 8.8.8.8 OK, nhưng log os-server/bootstrap/hal đều báo:
```
lookup <domain> on 127.0.0.1:53: connection refused
httpx.ConnectError: [Errno -3] Temporary failure in name resolution
```
Root cause: script `device-sta-mode` tắt dnsmasq (AP mode) rồi start dhcpcd,
nhưng dhcpcd hook `20-resolv.conf` không fire → `/etc/resolv.conf` stuck ở
`nameserver 127.0.0.1` (do AP mode dnsmasq set) → không ai listen port 53.

Đã fix ở `build-pi5.sh:1297` (device-sta-mode heredoc): sau khi dhcpcd nhận IP,
đọc `domain_name_servers` từ lease bằng `dhcpcd -U wlan0` rồi ghi thẳng
`/etc/resolv.conf`, có fallback `1.1.1.1` + `8.8.8.8` nếu router không cấp DNS.

Verify trên device sau flash:
```bash
sudo cat /etc/resolv.conf   # phải thấy DNS thật, KHÔNG có 127.0.0.1
getent hosts campaign-api.autonomous.ai   # phải resolve được
```

### `[FAIL] claude not pre-baked` → `❌ QC FAILED — image may not boot correctly`
`claude.ai/install.sh` đã đổi default landing path — code cũ ở `build-pi5.sh:1540` chỉ probe
`/root/.local/bin/claude`, không có fallback. Đã fix bằng multi-path probe
(`/root/.local/bin/claude`, `/root/.claude/bin/claude`, `/root/.claude/local/claude`, +
`command -v claude` fallback) → copy vào `/usr/local/bin/claude`.

**Quan trọng**: claude pre-bake nằm trong Phase 1 CHROOT_STAGES. Nếu base.img đã stamp
từ lần build fail trước → re-run bình thường skip Phase 1 → QC vẫn fail. Phải xóa cache:
```bash
rm -f output/base.img output/base.img.built-for
```
Rồi mới re-run `make build`. Phase 1 redo (~25-30 min) sẽ áp dụng patch.

### `line 781: unsafe-inline: command not found` + `OPENCLAW_VERSION: unbound variable`
Bug trong outer heredoc `CHROOT_STAGES` (line 781, unquoted → outer shell preprocess):
- **Comment với backtick** (`` `'unsafe-inline'` ``) bị bash hiểu là command substitution → thử execute `'unsafe-inline'` như command
- **Unescaped `${OPENCLAW_VERSION}`** ở dòng `openclaw plugins install` → outer shell try expand, hit `set -u` → unbound var

Đã fix ở `build-pi5.sh:1002` (bỏ backtick trong comment) và `build-pi5.sh:1413-1414` (escape `\${OPENCLAW_VERSION}`).

Rule: mọi ref `${VAR}` bên trong heredoc `CHROOT_STAGES` PHẢI:
- Escape `\${VAR}` nếu var chỉ tồn tại trong chroot (như `OPENCLAW_VERSION`, `SUFFIX_LC`)
- Để nguyên `${VAR}` nếu muốn outer shell inject value (như `DEVICE_TYPE`, `RPI_MODEL`, `MNT`)

Và **KHÔNG dùng backtick trong comment** — dùng single quote `'...'` thay thế.

### `ERROR: cached /output/base.img was built for 'unknown', this build wants 'intern-v2/rpi5'`
Base.img từ lần build fail trước bị stamp lỗi hoặc thiếu stamp file. Fix:
```bash
rm -f output/base.img output/base.img.built-for
```
Rồi re-run `make build` như cũ. Phase 1 sẽ rebuild từ đầu (~10-15 phút thêm).
Lý do stamp check: base.img cache device-type-specific — chạy Pi 4 base cho Pi 5
sẽ hỏng, nên script chặn ngay.

### `E: Unable to locate package libgpiod2` → `make: *** [build] Error 100`
Debian Trixie (Pi 5) đã rename `libgpiod2` → `libgpiod3` (libgpiod 2.x rewrite Rust).
`build-pi5.sh:640` đã patch conditional theo `RPI_MODEL` — nếu vẫn hit, verify:
```bash
grep -A5 "LIBGPIOD_PKG" scripts/imager/build-pi5.sh
```
Phải thấy branch: `RPI_MODEL=5 → libgpiod3`, `else → libgpiod2`.
Nếu Trixie image cũng đổi rename package khác (libportaudio2, libasound2-dev, v.v.),
apply cùng pattern conditional theo `RPI_MODEL`.

### `cp: input/software-update: No such file or directory` → `make: *** [build] Error 1`
Thiếu thư mục `input/` (bị `.gitignore`, không có sẵn khi clone fresh). Fix:
```bash
cd /Users/hoangphuong/Dropbox/autonomous/interns/autonomous/scripts/imager
mkdir -p input output
```
Rồi re-run `make build TARGET=rpi RPI_MODEL=5 ...` như cũ. Docker image `pi-builder`
đã build xong nên lần này sẽ dùng lại cache → nhảy thẳng vô stage `cp` + download raspios.

### Docker OOM
Docker Desktop → Settings → Resources → **Memory ≥ 8 GB** → restart Docker.

### Disk full
```bash
rm output/base.img output/golden.img
docker system prune -a -f   # xóa image + build cache
```

### Build treo giữa chừng
```bash
docker ps                    # tìm container pi-builder
docker logs -f <container_id>
# Ctrl+C thoát; xóa base.img rồi retry
```

### QC FAIL sau build
```bash
# Script xuất log QC step nào fail, thường là missing runtime binary
# Fix: đảm bảo runtime source (openclaw, hermes, etc.) đã ở đúng vị trí trong repo
grep "\[FAIL\]" output/build.log 2>/dev/null
```

### Image build ra bằng `golden.img` không phải `golden-intern-v2.img`
Makefile tự rename ở cuối `build:` target. Nếu không rename, chạy tay:
```bash
mv output/golden.img output/golden-intern-v2.img
```

---

## 11. File tham chiếu

| File | Vai trò |
|---|---|
| `scripts/imager/build-pi5.sh` | Builder Pi 5 (Trixie) — fork từ build.sh + Pi5-specific patches (libgpiod3, resolv.conf sau STA switch, claude installer multi-path). Xóa khi patches merged về build.sh. |
| `scripts/imager/build.sh` | Builder Pi 4 (Bookworm) gốc — KHÔNG đụng, chỉ Pi 4 target chạy file này |
| `scripts/imager/build-orangepi.sh` | Builder OrangePi 4 Pro — KHÔNG đụng vào file này |
| `scripts/imager/Makefile` | Wiring TARGET/RPI_MODEL/DEVICE_TYPE → BUILD_SCRIPT |
| `scripts/imager/README.md` | Doc chính của imager — có bảng target status |
| `scripts/imager/Dockerfile` | Docker build environment (shared cho cả 2 target) |
| `scripts/provision/software-update` | On-device updater script — copied vào image lúc build |

---

## 12. Timeline

| Task | Effort |
|---|---|
| Setup Docker + verify prerequisites | 5-10 phút |
| First build (bao gồm download raspios base) | 30-40 phút |
| Flash SD card + first boot verify | 10-15 phút |
| Test end-to-end (setup device qua web autonomous) | 15-20 phút |
| **Total lần đầu** | **~1.5 giờ** |
| Build lần 2+ (giữ base.img cache) | 3-5 phút |
