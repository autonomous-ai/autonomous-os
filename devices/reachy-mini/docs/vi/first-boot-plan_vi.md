# Reachy Mini Kế Hoạch Khởi Động Lần Đầu

Kế hoạch từng bước cho phiên thử nghiệm thiết bị thật đầu tiên. Chạy một lần
khi máy Wireless về, sau đó cập nhật `runtime.md`, `.env`, và `setup.sh` với
kết quả.

## Giai Đoạn 1: SSH Trinh Sát (Chỉ Đọc)

SSH vào và thu thập thông tin hệ thống. **Chưa thay đổi gì.**

```bash
ssh pollen@reachy-mini.local   # mật khẩu: root
```

**Lối tắt:** [`../recon.sh`](../recon.sh) chạy toàn bộ lệnh trong giai đoạn này
một phát và in ra bảng tóm tắt để điền. Ưu tiên dùng nó thay vì gõ tay từng mục:

```bash
scp devices/reachy-mini/recon.sh pollen@reachy-mini.local:/tmp/
ssh pollen@reachy-mini.local 'bash /tmp/recon.sh' | tee reachy-recon.txt
# thêm --audio-test để chạy luôn loopback mic->loa 3s (bước duy nhất phát ra tiếng)
```

Các mục thủ công bên dưới ghi lại từng probe kiểm tra gì và tại sao.

### 1.1 OS & Kernel

```bash
cat /etc/os-release              # Bookworm hay Bullseye?
uname -a                        # phiên bản kernel, kiến trúc
cat /boot/firmware/config.txt 2>/dev/null || cat /boot/config.txt
df -h                           # dung lượng ổ (16 GB eMMC)
free -h                         # RAM
```

**Tại sao**: xác định package khả dụng, cú pháp dtoverlay, và đường dẫn config
là `/boot/firmware/` hay `/boot/`.

### 1.2 Network Stack

```bash
systemctl is-active NetworkManager
systemctl is-active dhcpcd
systemctl is-active wpa_supplicant
systemctl is-active systemd-networkd
nmcli device status 2>/dev/null || echo "Không có NetworkManager"
ip addr show wlan0
cat /etc/wpa_supplicant/*.conf 2>/dev/null
cat /etc/dhcpcd.conf 2>/dev/null
ls /etc/NetworkManager/system-connections/ 2>/dev/null
```

**Tại sao**: kiểm tra quan trọng nhất. Quyết định `setup.sh` có thể dùng lại
flow dhcpcd/wpa_supplicant hiện tại hay cần đường dẫn NetworkManager. Xem
[recovery_vi.md](recovery_vi.md) để phân tích rủi ro.

**Cây quyết định**:

```
NetworkManager đang chạy?
├── CÓ → viết setup.sh tương thích NM (nmcli cho AP/STA, bỏ hostapd)
│         HOẶC tắt NM và cài dhcpcd stack (rủi ro hơn)
└── KHÔNG → dhcpcd đang chạy?
    ├── CÓ → setup.sh hiện tại hoạt động được
    └── KHÔNG → systemd-networkd? custom? → điều tra
```

### 1.3 Pollen Daemon

```bash
systemctl list-units | grep -i reachy
systemctl list-units | grep -i pollen
systemctl status reachy*
curl -s http://localhost:8000 | head -20
curl -s http://localhost:8000/api 2>/dev/null | head -20
ls /venvs/
ls /restore/venvs/
pip list 2>/dev/null | grep -i reachy
cat /etc/systemd/system/reachy* 2>/dev/null
```

**Tại sao**: cần biết chính xác tên service, port, API surface, và layout venv
để HAL driver và setup.sh không va chạm.

**Ghi lại**:
- [ ] Tên service daemon: `_______________`
- [ ] Port daemon: `_______________`
- [ ] Đường dẫn API gốc: `_______________`
- [ ] Phiên bản Python trong `/venvs/`: `_______________`
- [ ] `/restore/venvs/` tồn tại: có / không

### 1.4 Âm Thanh

```bash
arecord -l                       # liệt kê thiết bị thu (mic array)
aplay -l                        # liệt kê thiết bị phát (loa)
cat /proc/asound/cards
cat /etc/asound.conf 2>/dev/null
# Test nhanh (thu 3 giây, phát lại)
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay -D plughw:1,0 /tmp/test.wav
```

**Ghi lại**:
- [ ] Tên ALSA mic: `_______________` (vd: `plughw:2,0`)
- [ ] Tên ALSA loa: `_______________` (vd: `plughw:0,0`)
- [ ] Số kênh mic: `_______________`
- [ ] Sample rate 16 kHz hoạt động: có / không

### 1.5 Camera

```bash
v4l2-ctl --list-devices
ls /dev/video*
# Test nhanh
v4l2-ctl -d /dev/video0 --all 2>/dev/null | head -30
# Nếu dùng libcamera thay V4L2:
libcamera-hello --list-cameras 2>/dev/null
```

**Ghi lại**:
- [ ] Camera device index: `_______________`
- [ ] V4L2 hay libcamera: `_______________`
- [ ] Độ phân giải tối đa: `_______________`

### 1.6 Dịch Vụ & Cổng Đang Dùng

```bash
ss -tlnp                        # tất cả cổng TCP đang lắng nghe
systemctl list-units --type=service --state=running
# Kiểm tra xung đột cổng với dịch vụ của mình
# HAL: 5001, os-server: 8080, nginx: 80
```

**Ghi lại**:
- [ ] Cổng 5001 trống: có / không
- [ ] Cổng 8080 trống: có / không
- [ ] Cổng 80 trống: có / không (nginx?)

### 1.7 Dependency Hệ Thống

```bash
# Kiểm tra build deps cho pygobject/pycairo
dpkg -l | grep -E 'libcairo2-dev|libgirepository|pkg-config'
python3 --version
which uv 2>/dev/null
which pip3
```

### 1.8 Bluetooth

```bash
# Kiểm tra dịch vụ BLE cho recovery
systemctl status bluetooth
hciconfig -a 2>/dev/null
```

## Giai Đoạn 2: Viết Config Dựa Trên Kết Quả

Sau Giai đoạn 1, cập nhật các file này **trên máy dev** (không phải trên Pi):

### 2.1 ALSA Config

Tạo `devices/reachy-mini/rootfs/etc/asound.conf` với tên thiết bị thật:

```
# Template — điền sau khi chạy arecord -l / aplay -l
pcm.device_mic {
    type plug
    slave.pcm "hw:<CARD>,<DEV>"
}

pcm.device_speaker {
    type plug
    slave.pcm "hw:<CARD>,<DEV>"
}
```

### 2.2 HAL .env

Cập nhật `devices/reachy-mini/rootfs/opt/hal/.env` với giá trị thật:

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic       # từ 1.4
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker   # từ 1.4
HAL_CAMERA_INDEX=0                          # từ 1.5
```

### 2.3 setup.sh (Mới, Riêng Cho Reachy)

Viết `devices/reachy-mini/setup.sh` (hoặc sửa `scripts/provision/setup.sh`
chung với phân nhánh `DEVICE_TYPE`). Quyết định thiết kế chính từ trinh sát:

| Quyết định | Nếu NM | Nếu dhcpcd |
|------------|--------|------------|
| AP mode | `nmcli` hotspot hoặc cài hostapd | Flow hostapd hiện tại |
| STA mode | `nmcli con add` | Flow wpa_supplicant hiện tại |
| DNS captive portal | dnsmasq drop-in (giống) | dnsmasq drop-in (giống) |
| Mask service | Không mask NM, cấu hình nó | Mask global wpa_supplicant (giống) |

Bất kể network stack nào, setup.sh phải:

1. **Không bao giờ dừng hoặc restart Pollen daemon** trong quá trình cài
2. **Cài vào venv riêng** (`/opt/hal/.venv/`, không phải `/venvs/`)
3. **Cài system deps**: `libcairo2-dev`, `libgirepository1.0-dev`, `pkg-config`
4. **Không xung đột cổng**: kiểm tra 5001, 8080, 80 trống trước khi bind
5. **Đặt hostname** thành `reachy-mini-<suffix>` không phá mDNS của Pollen
6. **Tạo systemd units** cho `hal.service` và `os-server.service`
7. **Cài nginx** với config captive portal (hoặc bỏ qua nếu Pollen đã chạy nginx)

### 2.4 Kế Hoạch .env HAL Production

File `.env` hiện tại ở `devices/reachy-mini/rootfs/opt/hal/.env` có nhiều
placeholder `TODO(spike)`. Sau Giai đoạn 1, điền giá trị thật.

**Bảng field .env đầy đủ** (field đánh dấu `?` cần dữ liệu từ thiết bị thật):

```bash
# --- Core ---
HAL_MODE=production
HAL_LOG_LEVEL=INFO
DEVICE_TYPE=reachy-mini
DEVICES_DIR=/opt/devices

# --- Pollen daemon ---
# Trinh sát 1.3: xác nhận host/port. Hiện đang comment (mặc định
# localhost:8000 trong reachy_service.py). Chỉ bỏ comment nếu daemon
# chạy trên host hoặc port khác.
#REACHY_DAEMON_HOST=localhost          # ? xác nhận daemon trên localhost
#REACHY_DAEMON_PORT=8000              # ? xác nhận port từ `ss -tlnp`

# --- Âm thanh ---
# Trinh sát 1.4: điền từ `arecord -l` / `aplay -l`
HAL_AUDIO_INPUT_ALSA=plug:device_mic  # ? tên ALSA thật sau asound.conf
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker  # ? tên ALSA thật
HAL_VAD_THRESHOLD=500                 # có thể cần chỉnh cho 4-mic array
HAL_SPEECH_HOLDOFF=0.05
HAL_SILENCE_TIMEOUT=3.0
HAL_STT_KEEPALIVE=false
HAL_SILERO_ENABLED=true
HAL_SILERO_THRESHOLD=0.15             # ? chỉnh trên phần cứng thật
HAL_SILERO_CHUNK_SIZE=512
HAL_WEBRTCVAD_ENABLED=true
HAL_WEBRTCVAD_AGGRESSIVENESS=0        # ? có thể cần cao hơn vì servo ồn
HAL_WEBRTCVAD_FRAME_MS=30
HAL_TTS_SPEED=1.1

# --- Camera ---
# Trinh sát 1.5: xác nhận index từ `v4l2-ctl --list-devices`
HAL_CAMERA_INDEX=0                    # ? xác nhận device index
HAL_CAMERA_WIDTH=1280                 # ? xác nhận độ phân giải tối đa
HAL_CAMERA_HEIGHT=720                 # ? khi chia sẻ CPU với daemon
HAL_CAMERA_STREAM_WIDTH=960
HAL_CAMERA_STREAM_HEIGHT=540
HAL_CAMERA_AUTO_EXPOSURE=auto
# LƯU Ý: nếu Pollen OS dùng libcamera thay V4L2, backend OpenCV của HAL
# có thể cần LIBCAMERA_LOG_LEVELS=ERROR hoặc pipeline gstreamer. Kiểm tra 1.5.

# --- Sensing ---
HAL_MOTION_ENABLED=true
HAL_EMOTION_ENABLED=true
HAL_POSE_MOTION_ENABLED=false         # không bật pose tracking đến khi biết CPU budget
HAL_MOTION_CONFIDENCE_THRESHOLD=0.4
HAL_EMOTION_CONFIDENCE_THRESHOLD=0.5
HAL_DL_ENCRYPTION=true
HAL_DL_ENCRYPTION_REQUIRED=false
SPEAKER_MATCH_THRESHOLD=0.7
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD=0.7

# --- Realtime voice ---
HAL_REALTIME_TURN_DETECTION=off
HAL_WARM_MIC=true
HAL_WARM_MIC_ECHO_SKIP_MAX_S=0.1
HAL_ECHO_RMS_FLOOR=300                # ? chỉnh: loa Reachy 5W,
                                      #   có thể cần floor khác so với Lamp 3W

# --- CPU tuning ---
# RPi CM4: 4 nhân chia sẻ với Pollen daemon (vòng lặp 100 Hz).
# Giữ số thread thấp để không bỏ đói daemon.
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
# ? Sau trinh sát nhiệt: cân nhắc thêm CPU governor hoặc thermal throttle
```

**Câu hỏi chỉnh sửa quan trọng trên phần cứng thật**:

1. **Ngưỡng VAD**: Servo motors của Reachy có thể tạo nhiều tiếng ồn cơ khí
   hơn servo Feetech của Lamp. Có thể cần `HAL_WEBRTCVAD_AGGRESSIVENESS=1`
   hoặc cao hơn.
2. **Echo floor**: Loa 5W so với 3W của Lamp — `HAL_ECHO_RMS_FLOOR` có thể cần
   tăng để tránh tự kích hoạt khi phát TTS.
3. **Camera**: nếu Pi Camera v3 dùng libcamera, `VideoCapture(index)` của OpenCV
   có thể không hoạt động. Cần pipeline gstreamer hoặc tích hợp `picamera2`.
4. **Ngân sách CPU**: Pollen daemon chạy vòng lặp 100 Hz. Sensing của chúng ta
   (phát hiện cảm xúc + chuyển động) tăng tải CPU. Theo dõi bằng `htop` khi
   chạy motion + inference đồng thời. Nếu CPU > 80%, tắt
   `HAL_POSE_MOTION_ENABLED` và giảm độ phân giải camera.

### 2.5 Motion Driver TODOs

Sau Giai đoạn 1, giải quyết các `TODO(spike)` trong `reachy_service.py`:

- [ ] Xác nhận hành vi `wake_up()` / `goto_sleep()`
- [ ] Xác nhận quy ước dấu (yaw dương = trái hay phải?)
- [ ] Test tất cả 28 ánh xạ emotion→HF move
- [ ] Kiểm tra recorded moves có cần truy cập Hugging Face lần đầu không
- [ ] Đo nhiệt độ dưới tải

## Giai Đoạn 3: Deploy & Kiểm Tra

### 3.1 Spike Deploy

```bash
REACHY_HOST=pollen@<IP> bash devices/reachy-mini/spike.sh
```

### 3.2 Smoke Test

```bash
# Health
curl -s http://<IP>:5001/health
curl -s http://<IP>:5001/device

# Motion (thứ tự an toàn)
curl -s http://<IP>:5001/servo/position
curl -s -X POST http://<IP>:5001/servo/aim \
  -H 'content-type: application/json' \
  -d '{"direction":"center","duration":1.0}'
curl -s -X POST http://<IP>:5001/servo/zero
curl -s -X POST http://<IP>:5001/servo/release

# Âm thanh
curl -s -X POST http://<IP>:5001/speaker/play \
  -H 'content-type: application/json' \
  -d '{"text":"Hello, I am Reachy"}'

# Camera
curl -s http://<IP>:5001/camera/snapshot -o /tmp/snap.jpg
open /tmp/snap.jpg
```

### 3.3 Test Production Setup

Chỉ sau khi spike hoạt động:

```bash
ssh pollen@<IP>
DEVICE_TYPE=reachy-mini bash setup.sh   # bản mới
# Khởi động lại
sudo reboot
# Xác nhận AP mode hoạt động
# Kết nối điện thoại vào AP reachy-mini-xxxx
# Hoàn tất setup flow qua captive portal
```

## Giai Đoạn 4: Cập Nhật Tài Liệu

Sau khi mọi thứ hoạt động, cập nhật:

- [ ] `devices/reachy-mini/docs/runtime.md` — điền tất cả TODO(spike)
- [ ] `devices/reachy-mini/docs/vi/runtime_vi.md` — bản tiếng Việt
- [ ] `devices/reachy-mini/docs/recovery.md` — xác nhận lệnh BLE hoạt động
- [ ] `devices/reachy-mini/docs/vi/recovery_vi.md` — bản tiếng Việt
- [ ] `devices/reachy-mini/rootfs/opt/hal/.env` — giá trị thật
- [ ] `devices/reachy-mini/rootfs/etc/asound.conf` — tên ALSA thật
- [ ] `CLAUDE.md` — nếu tạo thêm docs mới

## Giai Đoạn 5: Golden Base Image (Chụp Từ Device) & build-reachy.sh

Việc build image Reachy theo **đúng pattern lamp/intern-v2 đang dùng**: imager
không build trên stock vendor image — mà build trên base image **chụp từ một
device đã biết chạy tốt**. Xem `scripts/imager/README.md` ("Base image — per
device type"). Pollen GitHub release image (`recovery.md` Cấp D) chỉ là **phao
recovery**, KHÔNG phải build base.

### 5.1 Chụp base image TỪ device

Làm việc này **trước khi chạy `setup.sh`**, lúc eMMC còn là Pollen OS nguyên bản
(Phase 1 recon chỉ đọc nên recon trước rồi chụp cũng được). eMMC của CM4 không có
khe SD, nên việc chụp đi qua cùng đường rpiboot USB như khi reflash (`recovery.md`
Cấp D) — nhưng **đọc** thay vì ghi:

```bash
# 1. tắt robot → gạt SW1 sang DOWNLOAD → nối USB2 → chạy rpiboot → bật nguồn
sudo ./rpiboot -d mass-storage-gadget64          # eMMC hiện thành /dev/diskX (macOS) hoặc /dev/sdX (Linux)

# 2. unmount các phân vùng tự mount trước
sudo diskutil unmountDisk /dev/diskX             # macOS
# sudo umount /media/$USER/bootfs /media/$USER/rootfs   # Linux

# 3. đọc raw NGUYÊN đĩa (kèm bảng phân vùng) và nén luôn
sudo dd if=/dev/rdiskX bs=8m | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz    # macOS (chú ý rdiskX)
# sudo dd if=/dev/sdX bs=8M | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz     # Linux
```

Lưu thành base của imager theo layout lamp/intern:
`scripts/imager/input/reachy-mini/golden-reachy-dev.img.xz`. Tuỳ chọn mirror lên
CDN Autonomous như các base khác:
`gs://s3-autonomous-upgrade-3/os/imager/base/golden-reachy-dev.img.xz`.

### 5.2 Recover device TỪ base đã chụp

Cùng đường rpiboot USB như 5.1, ghi image đã chụp ngược lại. Hai cách:

```bash
# --- Cách A: dd (đơn giản nhất — chụp bằng dd thì restore bằng dd y hệt) ---
# tắt → SW1 DOWNLOAD → USB2 → rpiboot → bật nguồn → unmount (như 5.1)
xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/rdiskX bs=8m    # macOS
# xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/sdX bs=8M     # Linux

# --- Cách B: bmaptool (nhanh hơn, sparse — tạo .bmap 1 lần rồi flash) ---
xz -dc reachy-mini-base-v<pollen-ver>.img.xz > reachy-base.img
bmaptool create -o reachy-base.bmap reachy-base.img
sudo bmaptool copy reachy-base.img --bmap reachy-base.bmap /dev/rdiskX
```

Rồi khôi phục boot bình thường: tắt nguồn → gạt switch về **DEBUG** → rút USB →
bật nguồn. Kiểm tra bằng `reachyminios_check` (phải in `Image validation PASSED`)
và xác nhận robot cử động đúng (check calibration, xem 5.3).

### 5.3 Vì sao chụp, không tải Pollen release

- eMMC lúc ship có thể chứa driver, config, hoặc first-boot state mà release
  generic thiếu — chụp đảm bảo base khớp đúng phần cứng + daemon con máy đang chạy.
- Cùng lý do lamp/intern-v2 dùng image của hardware team thay vì stock `.7z`.
- **Cảnh báo — per-unit state**: dump eMMC của 1 con có thể chứa calibration riêng
  (servo/IMU offset) hoặc identity. Restore về **chính con đó** thì luôn an toàn.
  Trước khi tái sử dụng image đã chụp làm base cho **con khác**, phải verify cái gì
  là per-unit rồi strip/tạo lại — câu hỏi recon: sau khi flash image đã chụp sang
  một con *khác*, `reachyminios_check` có PASS **và** robot có cử động đúng không?

### 5.4 build-reachy.sh (imager target tương lai — chưa viết)

Sẽ mirror các phase của `build-orangepi.sh`, chỉnh lại:

| build-orangepi.sh | build-reachy.sh khác biệt |
|---|---|
| Phase 0 base: `gdown` stock `.7z` | giải nén `input/reachy-mini/golden-reachy-dev.img.xz` (chụp ở 5.1) |
| Phase 2 chroot apt: hostapd/dnsmasq/dhcpcd | **gated vào recon 1.2** — NetworkManager vs dhcpcd quyết package mạng |
| Phase 2: bake nguyên stack OS | **cài chồng — không bao giờ xoá Pollen daemon** |
| Flash: SD card qua Imager | **rpiboot + bmaptool vào eMMC** — không có khe SD |
| Phase 3 OTA bake | như cũ: os-server, bootstrap, HAL, device profile overlay |

Chờ: recon 1.2 (network stack) + câu hỏi per-unit-state (5.3). Chỉ đáng build khi
ship NHIỀU con Reachy; với 1 con dev, `spike.sh` + `setup.sh`-cài-chồng là đủ.

## Tài Liệu Tham Khảo

- [Pollen OS build system](https://github.com/pollen-robotics/reachy-mini-os)
- [Reachy Mini SDK](https://github.com/pollen-robotics/reachy_mini)
- [Thông số phần cứng](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/hardware)
- [Hướng dẫn reflash](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
- [BLE reset](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)
- [Autonomous setup.sh](../../../scripts/provision/setup.sh)
- [Autonomous imager](../../../scripts/imager/README.md)
