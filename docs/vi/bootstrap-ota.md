# Bootstrap & OTA

## 1. Tổng Quan

Thiết bị chạy **5 thành phần phần mềm** trên board được hỗ trợ (Raspberry Pi 4, Pi 5, hoặc OrangePi). Tất cả được cài đặt qua script setup ban đầu và cập nhật tự động qua OTA worker chạy nền.

| Thành phần | Loại | Cách cài | Service | Đường dẫn |
|---|---|---|---|---|
| **Lamp Server** | Go binary (ARM64) | Tải zip từ OTA | `lamp.service` | `/usr/local/bin/lamp-server` |
| **Bootstrap Server** | Go binary (ARM64) | Tải zip từ OTA | `bootstrap.service` | `/usr/local/bin/bootstrap-server` |
| **Web (Setup SPA)** | React/Vite | Tải zip từ OTA | nginx serve static | `/usr/share/nginx/html/setup/` |
| **OpenClaw** | Node.js package | `npm install -g` | `openclaw.service` | Global npm |
| **HAL** | Python package | Tải zip từ OTA | `lamp-hal.service` | `/opt/hal/` |

### Sơ đồ hệ thống

```
                    ┌──────────────────────────────┐
                    │   OTA Metadata (GCS JSON)     │
                    │                                │
                    │  lamp:    {version, url}     │
                    │  bootstrap: {version, url}     │
                    │  web:       {version, url}     │
                    │  openclaw:  {version}          │
                    │  hal:       {version, url}     │
                    └───────────────┬────────────────┘
                                    │ poll mỗi 5 phút
                                    ▼
┌───────────────────────────────────────────────────────────────────┐
│                    Bootstrap Server (Go, port 8080)               │
│                                                                   │
│  checkLoop() → với mỗi thành phần:                               │
│    1. Phát hiện version hiện tại đang cài                        │
│    2. So sánh với version mục tiêu trong OTA metadata            │
│    3. Nếu khác → applyUpdate()                                   │
│       → tải zip / npm install                                     │
│       → giải nén vào đường dẫn cài đặt                           │
│       → systemctl restart {service}                               │
│    4. Lưu trạng thái vào /root/bootstrap/state.json              │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## 2. OTA Metadata

File JSON duy nhất trên GCS. Tất cả thành phần tham chiếu file này.

> Trong các URL bên dưới, `{BUCKET}` và `{PREFIX}` là bucket + namespace path:
> `GCS_BUCKET` (mặc định `s3-autonomous-upgrade-3`) và `BUCKET_PREFIX` (mặc định
> `os`), đều set trong `scripts/release/ota-config.sh`. Upload scripts đọc từ đó; consumer
> trên device derive cùng path từ `ota_metadata_url` đã provisioning.

**URL**: `https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json`

```json
{
  "lamp": {
    "version": "1.2.3",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/lamp/1.2.3/lamp-1.2.3.zip"
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
    "version": "2026.5.27"
  },
  "hal": {
    "version": "1.0.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/hal/1.0.0/hal-1.0.0.zip"
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
    // OTAKeyHAL sẽ được thêm khi HAL OTA được triển khai
)

type OTAMetadata map[string]OTAComponent

type OTAComponent struct {
    Version string `json:"version"`
    URL     string `json:"url,omitempty"`
}
```

---

## 3. Setup Ban Đầu (`scripts/provision/setup.sh`)

Script chạy **1 lần duy nhất** trên Pi mới. Thực thi tuần tự theo stages.

**Cài nhanh từ CDN:**
```bash
curl -fsSL https://cdn.autonomous.ai/os/install.sh | sudo bash
```

### Tổng quan stages

| Stage | Tên | Mô tả |
|---|---|---|
| -1 | Locale fix | Đảm bảo encoding `C.UTF-8` |
| 0 | Prerequisites | Packages hệ thống, Node.js 22 |
| 0a | WiFi stability | Tắt IPv6, WiFi power saving (RPi5) |
| 0b | Enable SPI | Cho WS2812 LED driver + GC9A01 display |
| 1 | Fetch OTA metadata | Tải metadata.json, trích xuất versions và URLs |
| 1b | Install binaries | Tải + cài lamp-server, bootstrap-server, tạo systemd services |
| 2 | Install OpenClaw | `npm install -g openclaw`, tạo config, systemd service |
| **2b** | **Install HAL** | **Tải + cài HAL Python runtime, tạo systemd service** (MỚI) |
| 3 | Setup nginx | Tải web bundle, cấu hình reverse proxy + captive portal |
| 4 | Setup WiFi AP | Cấu hình hostapd, dnsmasq, bật AP mode cho provisioning |

### Stage 2b: Cài HAL Runtime (MỚI)

```bash
stage_install_hal() {
    echo "=== Stage 2b: Install HAL Runtime ==="

    # 1. Cài Python dependencies hệ thống
    apt-get install -y python3 python3-pip python3-venv

    # 2. Tạo thư mục cài đặt
    mkdir -p /opt/hal

    # 3. Tải từ OTA metadata
    HAL_URL=$(echo "$OTA_JSON" | jq -r '.hal.url')
    HAL_VERSION=$(echo "$OTA_JSON" | jq -r '.hal.version')

    curl -fsSL "$HAL_URL" -o /tmp/hal.zip
    unzip -o /tmp/hal.zip -d /opt/hal/
    rm /tmp/hal.zip

    # 4. Cài Python dependencies trong venv
    python3 -m venv /opt/hal/venv
    /opt/hal/venv/bin/pip install -r /opt/hal/requirements.txt

    # 5. Tạo systemd service
    cat > /etc/systemd/system/lamp-hal.service << 'UNIT'
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
    systemctl enable lamp-hal.service
    systemctl start lamp-hal.service

    echo "HAL $HAL_VERSION installed at /opt/hal/"
}
```

### Systemd Services trên thiết bị

| Service | Lệnh chạy | Port | Ghi chú |
|---|---|---|---|
| `lamp.service` | `/usr/local/bin/lamp-server` | 5000 | HTTP API chính, luôn chạy |
| `bootstrap.service` | `/usr/local/bin/bootstrap-server` | 8080 | OTA worker, poll cập nhật. Expose `POST /force-check` để kích hoạt kiểm tra OTA ngay lập tức |
| `openclaw.service` | `xvfb-run ... openclaw gateway run` | — | AI brain, memory limit 1500M |
| `lamp-hal.service` | `uvicorn hal.server:app --host 127.0.0.1 --port 5001` | 5001 | Hardware drivers (servo, LED, camera, audio) |
| nginx | `nginx` | 80 | Setup SPA + reverse proxy (`/api/` → Lamp 5000, `/hw/` → HAL 5001) |

### Thứ tự khởi động

```
boot
  → lamp.service      (tầng hệ thống, LED boot animation)
  → bootstrap.service   (bắt đầu poll cập nhật)
  → lamp-hal.service          (hardware drivers sẵn sàng)
  → openclaw.service    (AI brain, kết nối lamp qua HTTP)
  → nginx               (web UI cho setup)
```

---

## 4. Bootstrap OTA Worker

### Config (`/root/config/bootstrap.json`)

Bootstrap worker giữ file config riêng, tách khỏi `config.json` của lamp-server,
nhưng nằm cùng thư mục `/root/config/`.

```json
{
  "httpPort": 8080,
  "metadata_url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json",
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
```

`metadata_url` **không có default hardcode** — đây là giá trị tùy theo deployment,
được `setup.sh` (`stage_ota_metadata`) seed vào file này lúc provisioning. File
được load dạng overlay lên các default vận hành (`httpPort` 8080, `poll_interval`
5m, `state_file`), nên file một phần (chỉ có `metadata_url`) vẫn chạy được, và
file thiếu thì dùng default với URL rỗng.

**Đợi-rồi-retry khi chưa provisioning**: nếu `metadata_url` rỗng (device chưa
setup), `Serve()` không khởi động poll loop lẫn healthcheck server. Nó log
`waiting for metadata_url in bootstrap config` và reload
`/root/config/bootstrap.json` mỗi 30s tới khi có URL rồi mới chạy tiếp. Không có
gì silent.

### State (`/root/bootstrap/state.json`)

Lưu version đã cài của mỗi thành phần:

```json
{
  "components": {
    "lamp": "1.2.3",
    "bootstrap": "1.0.5",
    "web": "0.9.0",
    "openclaw": "2026.5.27",
    "hal": "1.0.0"
  }
}
```

### Luồng xử lý chính (`bootstrap/bootstrap.go`)

```
checkLoop():
  1. checkOnce() ngay khi khởi động
  2. Sleep poll_interval (mặc định 5 phút)
  3. Lặp lại

checkOnce():
  1. Tải OTA metadata JSON
  2. Với mỗi key [lamp, bootstrap, web, hal]:
     → reconcile(key, metadata[key])
  GHI CHÚ: OpenClaw OTA tạm thời bị tắt (reconcileOpenClawFromNpm đã comment out)
  3. Lưu state

reconcile(key, target):
  1. Phát hiện version hiện tại đã cài
  2. So sánh với version mục tiêu
  3. Nếu giống → cập nhật state, return
  4. Nếu khác →
     a. Bật LED cam breathing (đang update)
     b. applyUpdate(key, target)
     c. Thành công → flash xanh lá | Thất bại → đỏ pulse
```

### OTA LED Feedback

Bootstrap dùng `lib/hal` để báo trạng thái update qua LED. Xem chi tiết: [status-led_vi.md](status-led_vi.md).

| Giai đoạn | LED |
|----------|-----|
| Đang tải + cài | Cam breathing `(255, 140, 0)` |
| Thành công | Flash xanh lá `(0, 255, 80)` |
| Thất bại | Đỏ pulse `(255, 30, 30)` |

### Phát hiện version hiện tại

| Thành phần | Cách phát hiện |
|---|---|
| `lamp` | Chạy `lamp-server --version`, parse output |
| `bootstrap` | Hằng số compile-time `config.BootstrapVersion` (ldflags) |
| `web` | Đọc file `/usr/share/nginx/html/setup/VERSION` |
| `openclaw` | Chạy `openclaw --version`, trích xuất semver bằng regex |
| `hal` | Chạy `/opt/hal/venv/bin/python -m hal --version` HOẶC đọc `/opt/hal/VERSION` |

### Cách cập nhật từng thành phần

| Thành phần | Các bước |
|---|---|
| `lamp` | Chạy `software-update lamp` (block tối đa 10 phút) |
| `bootstrap` | Spawn detached `software-update bootstrap` (tự cập nhật, sống sót sau restart) |
| `web` | Chạy `software-update web` |
| `openclaw` | ~~Chạy `npm install -g openclaw@{version}` → `systemctl restart openclaw`~~ (tạm thời tắt) |
| `hal` | Chạy `software-update hal` → `systemctl restart lamp-hal` |

---

## 5. Script Cập Nhật (`/usr/local/bin/software-update`)

Bash script được cài bởi setup.sh (và được imager bake sẵn vào image). Bootstrap
worker gọi script này để thực hiện cập nhật.

Script đọc URL metadata OTA từ `metadata_url` trong `/root/config/bootstrap.json`
(biến môi trường `OTA_METADATA_URL` nếu set sẽ override, dùng cho chạy thủ công/debug),
và exit lỗi nếu cả hai đều rỗng — không có URL hardcode.

### Xử lý HAL (MỚI)

```bash
"hal")
    echo "Updating HAL to $VERSION..."

    # Tải
    curl -fsSL "$URL" -o /tmp/hal-update.zip

    # Dừng service trước khi cập nhật
    systemctl stop lamp-hal.service

    # Backup
    cp -r /opt/hal /opt/hal.bak 2>/dev/null || true

    # Giải nén (giữ venv nếu chỉ thay đổi code, hoặc rebuild)
    unzip -o /tmp/hal-update.zip -d /opt/hal/

    # Cài lại dependencies nếu requirements.txt thay đổi
    /opt/hal/venv/bin/pip install -r /opt/hal/requirements.txt --quiet

    # Khởi động lại
    systemctl start lamp-hal.service

    # Dọn dẹp
    rm -f /tmp/hal-update.zip
    rm -rf /opt/hal.bak

    echo "HAL updated to $VERSION"
    ;;
```

---

## 6. HAL Runtime — Nguồn & Tích Hợp

### Chiến lược: Copy code + Track thủ công

Code HAL runtime được **copy** từ project upstream open-source vào mono-repo này, rồi sửa đổi nhiều.

**Tại sao copy, không dùng submodule/subtree:**
- Cần **bỏ** phần LiveKit/OpenAI (thay bằng OpenClaw)
- Cần **thêm** HTTP API server (FastAPI) để Lamp Server bridge đến
- Cần **thêm** DisplayService (GC9A01 eyes + info, không có trong upstream)
- Cần **sửa** services cho phù hợp kiến trúc mới
- Phần overlap chỉ là drivers (~30-40% code upstream), phần còn lại viết lại

**Theo dõi upstream:**
- Nguồn: `https://github.com/humancomputerlab/lelamp_runtime`
- Ghi commit hash upstream vào `os/hal/UPSTREAM.md` khi copy
- Định kỳ check upstream cho driver-level fixes (servo protocol, LED timing, ...)
- Cherry-pick thủ công khi cần
- Bỏ qua thay đổi AI/LiveKit upstream (mình đã thay thế hoàn toàn)

**Các bước thực hiện:**
1. Clone `humancomputerlab/lelamp_runtime` về thư mục tạm
2. Copy driver code (`services/motors.py`, `services/rgb.py`, `services/audio.py`, `services/service_base.py`) vào `os/hal/services/`
3. Xoá toàn bộ code LiveKit, OpenAI, conversation
4. Thêm `os/hal/server.py` — HTTP API server mới (FastAPI)
5. Thêm `os/hal/services/display.py` — DisplayService mới cho GC9A01
6. Tạo `os/hal/UPSTREAM.md` ghi commit hash nguồn và ngày copy
7. Test trên thiết bị với phần cứng thật

### Cấu trúc Mono-repo

HAL nằm trong repo này dưới dạng subfolder Python, cùng với Go và TypeScript:

```
autonomous/
├── os/services/          # Go code (fork từ lobster)
│   ├── cmd/              # Go entrypoints
│   ├── server/           # Go HTTP layer
│   ├── internal/         # Go business logic
│   ├── bootstrap/        # Go OTA worker
│   └── domain/           # Struct dùng chung
├── os/services/web/      # TypeScript/React SPA (copy từ lobster, đổi intern→lamp)
├── os/hal/               # Python hardware drivers (MỚI)
│   ├── __init__.py       # Package init, expose __version__
│   ├── server.py         # HTTP API server (FastAPI) — MỚI, không từ upstream
│   ├── services/
│   │   ├── motors.py     # MotorsService — 5x Feetech servo (từ upstream)
│   │   ├── rgb.py        # RGBService — 64x WS2812 LED (từ upstream)
│   │   ├── audio.py      # Audio — amixer, playback (từ upstream)
│   │   ├── display.py    # DisplayService — GC9A01 LCD (MỚI, không từ upstream)
│   │   └── service_base.py  # Event-driven ServiceBase (từ upstream)
│   ├── config.py         # Runtime config
│   ├── requirements.txt  # Python dependencies
│   ├── VERSION           # Version string
│   └── UPSTREAM.md       # Track commit nguồn từ humancomputerlab/lelamp_runtime
├── resources/
│   └── openclaw-skills/  # SKILL.md files
├── scripts/
│   └── setup.sh
├── go.mod
├── Makefile
└── CLAUDE.md
```

3 ngôn ngữ (Go, Python, TypeScript), 3 folder, 1 repo. Mỗi cái build riêng, quản lý chung.

### HAL OTA Package

Để phân phối qua OTA, HAL được zip từ folder `os/hal/`:

```
hal-{version}.zip
├── hal/                  # Full Python package
├── requirements.txt
└── VERSION
```

### HAL HTTP API (FastAPI trên port 5001)

HAL Python runtime expose HTTP API trên `127.0.0.1:5001`. Lamp Server (Go, port 5000) bridge request từ OpenClaw skills đến API này. Nginx proxy `/hw/*` chỉ cho caller trên cùng máy — client bên ngoài nhận 403. Swagger UI tại `/hw/docs` không truy cập được từ LAN.

```
OpenClaw LLM → curl 127.0.0.1:5000/api/servo → Lamp Server → http://127.0.0.1:5001/servo → HAL Python → Phần cứng
Bên ngoài    → http://<device-ip>/hw/docs    → nginx → 403 Forbidden
```

#### Endpoints (v0.2.0)

| Endpoint | Method | Mô tả |
|---|---|---|
| `/health` | GET | Kiểm tra hardware (servo, led, camera, audio) |
| `/servo` | GET | Recordings hiện có + trạng thái |
| `/servo/play` | POST | Chạy animation theo tên |
| `/led` | GET | Thông tin LED strip |
| `/led/solid` | POST | Đổ 1 màu |
| `/led/paint` | POST | Set màu từng pixel |
| `/led/off` | POST | Tắt tất cả LED |
| `/camera` | GET | Thông tin camera (resolution, availability) |
| `/camera/snapshot` | GET | Chụp 1 frame JPEG |
| `/camera/stream` | GET | MJPEG stream |
| `/audio` | GET | Thông tin audio device (Seeed mic/speaker) |
| `/audio/volume` | GET | Lấy volume hiện tại |
| `/audio/volume` | POST | Set volume (0-100%) |
| `/audio/play-tone` | POST | Phát test tone |
| `/audio/record` | POST | Thu âm từ mic, trả WAV |

---

## 7. Scripts Upload / Publish

### `scripts/release/upload-hal.sh` (MỚI)

```bash
#!/usr/bin/env bash
# Upload HAL runtime lên OTA

set -euo pipefail

VERSION_FILE="VERSION_HAL"
BUCKET="s3-autonomous-upgrade-3"
OTA_PATH="os/ota/hal"
METADATA_PATH="os/ota/metadata.json"

# Tự tăng patch version
CURRENT=$(cat "$VERSION_FILE" 2>/dev/null || echo "0.0.0")
MAJOR=$(echo "$CURRENT" | cut -d. -f1)
MINOR=$(echo "$CURRENT" | cut -d. -f2)
PATCH=$(echo "$CURRENT" | cut -d. -f3)
NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"
echo "$NEW_VERSION" > "$VERSION_FILE"

# Đóng gói
echo "Packaging HAL $NEW_VERSION..."
cd path/to/hal-source
echo "$NEW_VERSION" > VERSION
zip -r "/tmp/hal-${NEW_VERSION}.zip" hal/ requirements.txt VERSION

# Upload zip
gsutil cp "/tmp/hal-${NEW_VERSION}.zip" \
    "gs://${BUCKET}/${OTA_PATH}/${NEW_VERSION}/hal-${NEW_VERSION}.zip"

# Cập nhật metadata
DOWNLOAD_URL="https://storage.googleapis.com/${BUCKET}/${OTA_PATH}/${NEW_VERSION}/hal-${NEW_VERSION}.zip"
gsutil cp "gs://${BUCKET}/${METADATA_PATH}" /tmp/metadata.json
jq --arg v "$NEW_VERSION" --arg u "$DOWNLOAD_URL" \
    '.hal = {"version": $v, "url": $u}' /tmp/metadata.json > /tmp/metadata-updated.json
gsutil cp /tmp/metadata-updated.json "gs://${BUCKET}/${METADATA_PATH}"

echo "HAL $NEW_VERSION published."
```

### Tất cả upload scripts

| Script | Thành phần | Pattern |
|---|---|---|
| `scripts/release/upload-lamp.sh` | Lamp Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-bootstrap.sh` | Bootstrap Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-web.sh` | Web SPA bundle | Build → zip → GCS → update metadata |
| `scripts/release/upload-hal.sh` | HAL Python runtime (MỚI) | Package → zip → GCS → update metadata |
| `scripts/release/upload-setup.sh` | Script setup | Upload lên GCS |
| `scripts/release/upload-setup-ap.sh` | Script setup AP | Upload lên GCS |
| `scripts/release/upload-skills.sh` | OpenClaw skill files | Upload lên GCS |
| `scripts/provision/install.sh` | CDN install shortcut | `curl ... \| sudo bash` trên Pi |
| `scripts/release/tag-release.sh` | Git release tag kèm OTA metadata snapshot | Fetch metadata.json → annotated tag → `git push origin <tag>` |

### `scripts/release/tag-release.sh` — Truy nguồn theo GPL v3 §6

Sau khi các upload component xong (`make upload-lamp upload-hal upload-web ...`), script này neo OTA metadata snapshot vào một git tag duy nhất:

```bash
make tag-release v0.0.8
# → curl https://cdn.autonomous.ai/os/ota/metadata.json
# → git tag -a v0.0.8 -F - (annotation = JSON metadata đẹp)
# → git push origin v0.0.8
```

Người mua chạy `lamp-server --version` trên thiết bị — giá trị lấy từ `git describe --tags --always --dirty` lúc build (`Makefile:VERSION`), nên resolve về tag gần nhất. Họ mở repo public (`github.com/autonomous-ai/lamp`), tìm tag đúng, đọc annotation để xem chính xác version `lamp`/`hal`/`web`/`bootstrap` đã bake vào release đó, rồi checkout commit tương ứng để có source.

Guards trong script: từ chối nếu tag đã tồn tại local hoặc trên remote, từ chối nếu fetch metadata fail hoặc JSON invalid (`set -euo pipefail` + `jq .`). Override qua env: `OTA_METADATA_URL` (mặc định: `https://cdn.autonomous.ai/os/ota/metadata.json`), `TAG_REMOTE` (mặc định: `origin`).

---

## 8. Build & Version

### Go binaries (ldflags)

```makefile
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

LDFLAGS_BOOTSTRAP := -X go-lamp.autonomous.ai/bootstrap/config.BootstrapVersion=$(VERSION)
LDFLAGS_LAMP    := -X go-lamp.autonomous.ai/server/config.LampVersion=$(VERSION)

build-bootstrap:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_BOOTSTRAP)" -o bootstrap-server ./cmd/bootstrap

build-lamp:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_LAMP)" -o lamp-server ./cmd/lamp
```

### HAL (VERSION file)

Version của HAL là file text `VERSION` trong thư mục gốc package. Bootstrap đọc qua file hoặc `python -m hal --version`.

---

## 9. Khác Biệt So Với Lobster

| Khía cạnh | Lobster (gốc) | Autonomous (project này) |
|---|---|---|
| Số thành phần | 4 (lamp, bootstrap, web, openclaw) | **5** (+ hal) |
| OTA keys | lamp, bootstrap, web, openclaw | + **hal** |
| Setup stages | 7 (stage -1 đến 4) | **8** (+ stage 2b: HAL) |
| Systemd services | 4 | **5** (+ lamp-hal.service) |
| Python runtime | Không có | **HAL** tại /opt/hal/ với venv |
| Hardware bridge | Không có | Lamp HTTP → HAL HTTP (localhost proxy) |
| SPI usage | Chỉ LED | LED + **Display (GC9A01)** |

---

## 10. Câu Hỏi Mở

- [x] **HAL source**: Mono-repo. Driver code copy từ `humancomputerlab/lelamp_runtime` vào `os/hal/`, bỏ LiveKit/OpenAI, thêm HTTP API + DisplayService. Track upstream thủ công qua `os/hal/UPSTREAM.md`.
- [x] **HAL HTTP port**: `5001` (Lamp Server là `5000`).
- [x] **Bridge protocol**: HTTP proxy đơn giản. HAL chạy FastAPI trên `127.0.0.1:5001`, Lamp Server proxy từ port 5000.
- [ ] **Python version**: Pin Python 3.11+? Yêu cầu Python hiện tại của HAL?
- [ ] **Đóng gói HAL**: Include venv sẵn? Hay cài deps trên thiết bị? (Pi resources hạn chế cho `pip install`)
- [ ] **Display driver**: DisplayService (GC9A01) — nằm trong HAL Python? Hay module mới?
- [ ] **HAL config**: HAL cần config file riêng? Hay cấu hình qua Lamp Server?

---

> Tài liệu này mô tả toàn bộ hệ thống OTA và bootstrap.
> Xem [architecture-decision.md](architecture-decision.md) cho quyết định kiến trúc.
> Xem [product-vision.md](product-vision.md) cho tầm nhìn sản phẩm.
