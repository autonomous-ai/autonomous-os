# Bootstrap & OTA

## 1. Tổng Quan

Thiết bị chạy **5 thành phần phần mềm** trên board được hỗ trợ (Raspberry Pi 4, Pi 5, hoặc OrangePi). Tất cả được cài đặt qua script setup ban đầu và cập nhật tự động qua OTA worker chạy nền.

| Thành phần | Loại | Cách cài | Service | Đường dẫn |
|---|---|---|---|---|
| **OS Server** | Go binary (ARM64) | Tải zip từ OTA | `os-server.service` | `/usr/local/bin/os-server` |
| **Bootstrap Server** | Go binary (ARM64) | Tải zip từ OTA | `bootstrap.service` | `/usr/local/bin/bootstrap-server` |
| **Web (Setup SPA)** | React/Vite | Tải zip từ OTA | nginx serve static | `/usr/share/nginx/html/setup/` |
| **OpenClaw** | Node.js package | `npm install -g` | `openclaw.service` | Global npm |
| **HAL** | Python package | Tải zip từ OTA | `hal.service` | `/opt/hal/` |

### Sơ đồ hệ thống

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

Tài liệu publish vẫn giữ component legacy ở top level và thêm envelope Ed25519
`signed` theo `autonomous-ota/v1`. `signed.payload` là JSON base64;
`signed.signature.value` ký đúng bytes payload sau khi decode. Public key 32
byte dạng base64 được provision cục bộ trong `bootstrap.json`, không bao giờ
lấy từ feed. Payload đã decode có dạng:

```json
{
  "os-server": {
    "version": "1.2.3",
    "min_version": "1.2.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/os-server/1.2.3/os-server-1.2.3.zip",
    "sha256": "<64 ký tự hex thường>"
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

### Tạo key ký OTA

Chạy một lần trên máy của release operator:

```bash
make ota-keygen
```

Mặc định lệnh tạo private PEM Ed25519 ở ngoài repo, tại
`~/.config/autonomous/ota/ota-YYYYMMDD.pem`, rồi in ba dòng `export`. Giữ kín
`OTA_SIGNING_PRIVATE_KEY`; dùng nó cùng `OTA_SIGNING_KEY_ID` khi chạy
`make upload-*`. Provision `OTA_SIGNING_PUBLIC_KEY` được in ra cho device mới.
Có thể đổi thư mục hoặc ID:

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
    // Agent-runtime CLIs. Each value is also the runtime name in config.json
    // `agent_runtime` — that equality is how bootstrap updates only the CLI the
    // device actually runs. Hermes is absent on purpose (cannot be pinned).
    OTAKeyCodex      = "codex"
    OTAKeyClaudeCode = "claudecode"
    OTAKeyOpenCode   = "opencode"
    OTAKeyPicoClaw   = "picoclaw"
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

`version` là bản mới nhất; `min_version` là **sàn đã duyệt** mà worker tự động
đẩy cả fleet lên tới. Hai trường tách bạch "đã publish" và "đã auto-push":

- **Auto OTA (bootstrap worker)** chỉ cập nhật thiết bị khi version hiện tại
  **thấp hơn hẳn `min_version`**. Nếu thiếu `min_version` thì mặc định bằng
  `version` (worker bám latest — hành vi cũ).
- **`software-update <key>` thủ công** (qua SSH) bỏ qua `min_version`, luôn cài
  `version` — để test bản mới trên vài thiết bị trước.

Quy trình:

1. `scripts/release/upload-<component>.sh` bump `version` và **giữ nguyên**
   `min_version` (skills/hooks không có `min_version`). Fleet **không** đổi —
   chỉ `version` thay đổi.
2. SSH vào thiết bị, chạy `software-update <key>` → kéo `version`. Test.
3. Ổn? `make promote-<component> [V=<version>]` (vd `make promote-hal`,
   `make promote-os-server V=1.4.0`, `make promote-device DT=lamp`) nâng
   `min_version` (mặc định = `version`). Bootstrap sẽ tự cập nhật mọi thiết bị
   dưới sàn mới ở lần check kế tiếp.

So sánh version theo số trên từng đoạn (`bootstrap.compareVersions`):
`2026.6.10 > 2026.5.9`; bỏ qua hậu tố pre-release/build; version hiện tại rỗng
hoặc không parse được xem là thấp nhất (luôn dưới mọi sàn → cập nhật).

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
| 1b | Install binaries | Tải + cài os-server, bootstrap-server, tạo systemd services |
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

### Systemd Services trên thiết bị

| Service | Lệnh chạy | Port | Ghi chú |
|---|---|---|---|
| `os-server.service` | `/usr/local/bin/os-server` | 5000 | HTTP API chính, luôn chạy |
| `bootstrap.service` | `/usr/local/bin/bootstrap-server` | 8080 | OTA worker, poll cập nhật. Expose `POST /force-check` để kích hoạt kiểm tra OTA ngay lập tức và `GET /security` cho trạng thái tin cậy OTA |
| `openclaw.service` | `xvfb-run ... openclaw gateway run` | — | AI brain, memory limit 1500M |
| `hal.service` | `uvicorn hal.server:app --host 127.0.0.1 --port 5001` | 5001 | Hardware drivers (servo, LED, camera, audio) |
| nginx | `nginx` | 80 | Setup SPA + reverse proxy (`/api/` → OS Server 5000, `/hw/` → HAL 5001) |

### Thứ tự khởi động

```
boot
  → os-server.service   (tầng hệ thống, LED boot animation)
  → bootstrap.service   (bắt đầu poll cập nhật)
  → hal.service          (hardware drivers sẵn sàng)
  → openclaw.service    (AI brain, kết nối os-server qua HTTP)
  → nginx               (web UI cho setup)
```

---

## 4. Bootstrap OTA Worker

### Config (`/root/config/bootstrap.json`)

Bootstrap worker giữ file config riêng, tách khỏi `config.json` của os-server,
nhưng nằm cùng thư mục `/root/config/`.

```json
{
  "httpPort": 8080,
  "metadata_url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json",
  "signing_public_key": "<Ed25519 public key 32-byte base64>",
  "rollback_versions": {"os-server": "1.2.3"},
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
```

`metadata_url` **không có default hardcode** — đây là giá trị tùy theo deployment,
được `setup.sh` (`stage_ota_metadata`) seed vào file này lúc provisioning. File
được load dạng overlay lên các default vận hành (`httpPort` 8080, `poll_interval`
5m, `state_file`), nên file một phần (chỉ có `metadata_url`) vẫn chạy được, và
file thiếu thì dùng default với URL rỗng.

Khi được truyền vào, `setup.sh` và image builder lưu `OTA_SIGNING_PUBLIC_KEY`
thành `signing_public_key`. Khi đó Bootstrap xác thực envelope trước khi đọc
component; updater được provision xác thực lần nữa và hash mọi ZIP trước khi
giải nén. Khi release operator truyền `OTA_SIGNING_PRIVATE_KEY` và
`OTA_SIGNING_KEY_ID`, release writer ký lại envelope; thiếu chúng thì vẫn giữ
format legacy unsigned.
Với hai binary tự chứa, mỗi lần update giữ
`/root/bootstrap/rollback/<component>.previous`; chạy
`software-update rollback os-server` hoặc `software-update rollback bootstrap`
để khôi phục. Updater ghi version vừa gỡ vào `rollback_versions`; bootstrap chỉ
bỏ qua đúng target đó nên release lỗi không bị cài lại ở lần poll sau. Khi feed
có version khác, OTA của component đó tự tiếp tục. Bản thân rollback không cần
metadata URL hoặc mạng.

Các component cài theo thư mục cũng có cùng hợp đồng recovery. Trước khi update
web, updater dừng nginx, swap bundle đã giải nén hoàn chỉnh từ thư mục staging,
và giữ bundle trước đó tại `/root/bootstrap/rollback/web.previous` cùng trạng
thái active/inactive trước đó của nginx. Sau đó nó đòi hỏi có `index.html`,
`nginx -t` hợp lệ và—khi nginx vốn đang chạy—`GET /` loopback thành công. Nếu
thất bại, updater tự khôi phục bundle và trạng thái service đã lưu. Operator cũng
có thể chạy `software-update rollback web`; version bị loại được ghi vào
`rollback_versions` giống rollback binary.

Với device profile, updater stage ZIP, chỉ dừng `os-server` và `hal` vốn đang
active, rồi giữ profile cũ tại `/root/bootstrap/rollback/device.previous`. Nó
cũng snapshot chính xác các file thuộc `rootfs/` của profile cũ hoặc mới trong
`device.previous.rootfs`; rollback vì vậy khôi phục file bị ghi đè và xoá file
chỉ được profile lỗi thêm vào. Tuning local trong `/opt/hal/.env` vẫn được giữ.
Profile bắt buộc có `ROBOT.md`; mỗi service vốn active phải khởi động lại và trả
về health endpoint loopback. Check lỗi sẽ tự phục hồi profile known-good và trạng
thái service cũ. Dùng `software-update rollback device` khi operator rollback;
version profile bị loại sau đó sẽ bị chặn.

Device không có `signing_public_key` chủ ý ở legacy mode: nó đọc component top
level và chỉ log cảnh báo, không làm OTA lỗi. Đây là compatibility bridge, không
phải bảo đảm trust; pin public key để bật verified OTA.

### Trạng thái bảo mật OTA (operator nhìn thấy được)

Cảnh báo ở trên chỉ là một dòng log, nên một device kẹt ở legacy mode là vô hình
với người vận hành fleet. Cả hai tiến trình đều expose trạng thái đó dưới dạng dữ liệu:

| Endpoint | Do ai phục vụ | Ghi chú |
|----------|---------------|---------|
| `GET http://127.0.0.1:8080/security` | `bootstrap-server` | Nguồn sự thật: worker giữ key đã pin và thực hiện verify. Chỉ loopback. |
| `GET /api/system/ota-security` | `os-server` | Proxy nguyên văn endpoint trên, bọc trong wrapper chuẩn `{"status":1,"data":…}`. Trả `502` khi bootstrap worker không với tới được. |

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

- `mode` là `verified` khi đã provision `signing_public_key`, ngược lại là
  `legacy`. Đây là field duy nhất cần cảnh báo trên toàn fleet.
- `key_fingerprint` là 16 ký tự hex đầu của SHA-256 của key đã pin, để operator
  biết device đang tin **key nào** (và phát hiện device còn kẹt ở key đã xoay
  vòng) mà endpoint không phải phát lại key material. Không có ở legacy mode.
- `artifact_checksums` cho biết SHA-256 từng component có được enforce không. Nó
  đi theo `mode`: digest chỉ có ý nghĩa khi metadata mang nó là xác thực.
- `last_metadata_fetch` là kết quả lần fetch gần nhất, gồm cả lỗi transport
  (`error` được set, `verified` là `false`). Nó vắng mặt cho tới khi lần fetch
  đầu tiên sau restart hoàn tất, nên device chưa từng với tới feed phân biệt
  được với device có feed khoẻ mạnh.

### Cutover sang signed-only

Metadata ký được publish dạng superset: component entry của payload vẫn nằm ở
top level cho các worker đã deploy, còn tài liệu xác thực nằm dưới `signed`. Bản
copy tương thích đó chính là thứ giữ cho legacy mode còn khai thác được, nên
phải gỡ. Lộ trình:

1. **Publish có ký** (đã xong). Release writer thêm envelope `signed` mỗi khi có
   `OTA_SIGNING_PRIVATE_KEY` và `OTA_SIGNING_KEY_ID`. Device bỏ qua nó tới khi
   được pin key.
2. **Provision key.** Device mới nhận `OTA_SIGNING_PUBLIC_KEY` lúc setup; fleet
   hiện có thì ghi vào `/root/config/bootstrap.json`. Không cần redeploy —
   worker đọc key ở lần load config kế tiếp.
3. **Xác nhận toàn fleet.** Poll `GET /api/system/ota-security` và yêu cầu
   `mode == "verified"` cùng đúng `key_fingerprint` trên mọi device. Bước này là
   cổng chặn: còn device nào báo `legacy` thì không đi tiếp.
4. **Cutover.** Publish với `OTA_METADATA_SIGNED_ONLY=1`, cờ này bỏ bản copy top
   level nên tài liệu chỉ còn `signed`. Device chưa migrate sẽ dừng update (và
   kêu to) thay vì update từ nguồn không xác thực — đúng hướng lỗi mong muốn.

Rollback cho bước 4 là publish lại một lần nữa mà không bật cờ; device đã ở
verified mode không bị ảnh hưởng theo hướng nào, vì chúng chỉ đọc `signed`.

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
    "os-server": "1.2.3",
    "bootstrap": "1.0.5",
    "web": "0.9.0",
    "openclaw": "2026.6.10",
    "hal": "1.0.0"
  }
}
```

Việc lưu state là atomic: bootstrap ghi và sync file tạm trong cùng thư mục rồi
mới rename vào vị trí chính thức. Nếu state cũ bị lỗi định dạng (ví dụ do lần ghi
legacy bị gián đoạn), bootstrap giữ lại thành `state.json.corrupt-<timestamp>`,
log cảnh báo và tiếp tục với state rỗng thay vì không khởi động OTA polling.

### Luồng xử lý chính (`bootstrap/bootstrap.go`)

```
checkLoop():
  1. checkOnce() ngay khi khởi động
  2. Sleep poll_interval (mặc định 5 phút)
  3. Lặp lại

checkOnce():
  1. Tải OTA metadata JSON
  2. Với mỗi key [os-server, bootstrap, web, hal]:
     → reconcile(key, metadata[key])
  GHI CHÚ: OpenClaw OTA tạm thời bị tắt (reconcileOpenClawFromNpm đã comment out)
  3. Lưu state

reconcile(key, target):
  1. Phát hiện version hiện tại đã cài
  2. Nếu current == "" VÀ component không được cài → bỏ qua im lặng
  3. floor = target.min_version (mặc định target.version nếu rỗng)
  4. Nếu current >= floor → đồng bộ state, return (đã ở/trên sàn duyệt)
  5. Nếu current < floor →
     a. Bật LED cam breathing (đang update)
     b. applyUpdate(key, target)   # cài target.version qua software-update
     c. Thành công → flash xanh lá | Thất bại → đỏ pulse
```

> `software-update <key>` thủ công qua SSH KHÔNG đi qua `reconcile` — nó cài
> thẳng `target.version`, bỏ qua sàn `min_version`.

#### Bước 2 — component thiết bị này không có

Metadata liệt kê mọi component đã publish; không thiết bị nào chạy đủ hết. Reachy
Mini không có `claude-desktop-buddy`; thiết bị chạy runtime khác OpenClaw thì
không có `openclaw`. Với mấy cái đó `detectVersion` trả `""`, mà `""` xếp dưới
mọi sàn — nên nếu không chặn, worker sẽ báo "update available", phát câu thông
báo qua loa, bật LED cam rồi cài thất bại **mỗi lần poll, vĩnh viễn**.

`componentInstalled(key)` hỏi câu thô hơn `detectVersion`: *artifact có tồn tại
không*, chứ không phải *version nào*. Component có mặt nhưng đọc không được vẫn
tính là đã cài, nên OTA vẫn tự chữa được (binary `os-server` hỏng `--version` vẫn
được update); chỉ cái thực sự vắng mặt mới bị bỏ qua.

| Component | Tính là đã cài khi |
|---|---|
| `bootstrap` | luôn luôn (chính là worker đang chạy, để nó tự update được) |
| `os-server` | `os-server` có trong `$PATH` |
| `openclaw` | `openclaw` có trong `$PATH` |
| `web` | tồn tại `/usr/share/nginx/html/setup/` |
| `hal` | tồn tại `/opt/hal/` |
| `claude-desktop-buddy` | tồn tại `/opt/claude-desktop-buddy/` |
| `device` | tồn tại `$DEVICES_DIR/<type>/` (mặc định `/opt/devices`) |

### OTA LED Feedback

Bootstrap dùng `lib/hal` để báo trạng thái update qua LED. Xem chi tiết: [status-led_vi.md](../../robots/lamp/docs/vi/status-led_vi.md).

| Giai đoạn | LED |
|----------|-----|
| Đang tải + cài | Cam breathing `(255, 140, 0)` |
| Thành công | Flash xanh lá `(0, 255, 80)`, rồi khôi phục LED look user đã chọn hoặc ambient resting look nếu chưa có user state |
| Thất bại | Đỏ pulse `(255, 30, 30)` |

### Bất biến mà updater phải giữ (trả giá mới biết)

Một máy mất sạch `/opt/hal`, thư mục staging VÀ bản backup rollback chỉ vì bấm
nút update trên web 2 lần cách nhau 30 giây. Bốn lỗi xếp chồng; cả bốn đã vá
trong `scripts/provision/software-update`, và đều rất dễ tái phạm:

1. **Mỗi lúc chỉ một lần chạy** (`flock` trên `/var/lock/software-update.lock`).
   Mọi nhánh đều publish bằng cách `mv` cây đang chạy sang `<name>.previous`, nên
   lần chạy thứ hai `rm -rf` mất backup duy nhất của lần thứ nhất rồi chết vì
   không còn nguồn. Rate-limit 30 giây/target ở os-server KHÔNG chặn được.
2. **Thiếu thư mục cài đặt là REINSTALL, không phải lỗi.** `mv "$HAL_DIR"` abort
   khi `/opt/hal` vắng nghĩa là lệnh duy nhất có thể cứu máy lại từ chối chạy.
3. **Publish theo đúng mode của cây đang sống** (`publish_mode`). `mktemp -d` cho
   0700; `mv` thư mục đó lên `/usr/share/nginx/html/setup` làm nginx (www-data)
   trả 403 cho mọi request → health check fail → update tự rollback, mãi mãi,
   trên mọi máy.
4. **Virtualenv không relocatable** (`relocate_venv_scripts`). Build `.venv`
   trong staging làm mọi console script mang shebang
   `#!/opt/.hal.new.XXXXXX/.venv/bin/python`; staging biến mất là unit chết
   `203/EXEC`. Không lộ khi kế thừa `.venv` cũ — chỉ cắn đúng lúc cài mới, tức
   đúng đường khôi phục.

Cộng thêm một lỗi làm mọi thứ trên khó thấy: **trạng thái service được chụp ở đầu
mỗi lần chạy**, nên lần chạy bắt đầu khi update trước đó đang fail sẽ ghi
"inactive", và mọi update sau khôi phục trung thành trạng thái "chết" đó.
`unit_wanted_active` giờ coi unit còn `enabled` trong systemd là "phải chạy", và
`check_web`/`check_hal` dò đúng thứ unit ĐANG làm thay vì bỏ qua khi snapshot bảo
nó nên tắt (dạng cũ báo thành công trong khi web UI trả 403).

### `POST /force-update/:target` vs `POST /force-check/:target` (bootstrap, loopback)

Hai việc khác nhau, và lẫn lộn chúng chính là thứ làm nút web trông như hỏng:

| Endpoint | Nghĩa | `min_version` |
|---|---|---|
| `force-update/<key>` | Cài `version` đã publish NGAY — đúng thứ `software-update <key>` làm qua SSH | **bỏ qua** (sàn để staging cả fleet, không phải để chặn operator chủ đích) |
| `force-check/<key>` | Chạy lại quyết định TỰ ĐỘNG cho component đó | **tôn trọng** — component đã bằng/cao hơn sàn thì không làm gì |

Nút `update` trong card Versions là `force-update` (qua
`POST /api/system/software-update/:target`). Cả hai dùng chung allowlist target,
và `componentInstalled` vẫn từ chối component thiết bị không có.

### `GET /versions` (bootstrap, loopback)

Trả `{current, target, min_version, update_available, held_by_floor}` cho mọi
component thiết bị THỰC SỰ có (`componentInstalled`), nên mục CLI chính là runtime
nó đang chạy, không có cái nào khác. `held_by_floor` nghĩa là đã publish bản mới
nhưng `min_version` chưa được promote lên — worker sẽ từ chối, nên card Versions
trên web coi component bị giữ là "không có update". os-server proxy thành
`GET /api/system/ota-versions`.

### Phát hiện version hiện tại

| Thành phần | Cách phát hiện |
|---|---|
| `os-server` | Chạy `os-server --version`, parse output |
| `bootstrap` | Hằng số compile-time `config.BootstrapVersion` (ldflags) |
| `web` | Đọc file `/usr/share/nginx/html/setup/VERSION` |
| `openclaw` | Chạy `openclaw --version`, trích xuất semver bằng regex |
| `hal` | Chạy `/opt/hal/venv/bin/python -m hal --version` HOẶC đọc `/opt/hal/VERSION` |
| `codex` / `claudecode` / `opencode` | Chạy `<cli> --version`, lấy semver ở dòng đầu (`cliSemver`) |
| `picoclaw` | Đọc `/usr/local/lib/os-runtimes/picoclaw/installed-version` — output `version` của nó không có semver |
| `hermes` | — không auto-update (xem bên dưới) |

### Cách cập nhật từng thành phần

| Thành phần | Các bước |
|---|---|
| `os-server` | Chạy `software-update os-server` (block tối đa 10 phút) |
| `bootstrap` | Spawn detached `software-update bootstrap` (tự cập nhật, sống sót sau restart) |
| `web` | Chạy `software-update web` |
| `openclaw` | ~~Chạy `npm install -g openclaw@{version}` → `systemctl restart openclaw`~~ (tạm thời tắt) |
| `hal` | Chạy `software-update hal` → `systemctl restart hal` |
| `codex` / `claudecode` / `opencode` / `picoclaw` | Chạy `software-update <key>` — CHỈ trên thiết bị có `agent_runtime` đúng bằng runtime đó |
| `hermes` | Không nằm trong loop: `hermes update` không pin được, nên một `min_version` nó không bao giờ đạt sẽ kích lại mỗi vòng poll. Chỉ chạy tay qua SSH. |

**Vì sao CLI của agent gate theo `agent_runtime` chứ không theo binary:**
`scripts/imager/build-orangepi.sh` bake CLI của MỌI agent lên mọi image lamp /
intern-v2 bất kể `DEFAULT_AGENT`, nên `inPath("codex")` vẫn đúng trên máy đang
chạy Hermes. Gate theo sự hiện diện của binary sẽ khiến mỗi vòng poll phát TTS
"thiết bị đang cập nhật", chuyển LED cam, tải một CLI thiết bị không dùng, rồi
restart một unit không tồn tại — lặp mãi mãi. Nên `componentInstalled` so key với
`agent_runtime` trong `/root/config/config.json`; đọc không được hoặc rỗng nghĩa
là "không phải runtime này" (bỏ qua) — hướng an toàn.

**Cửa thứ hai** chặn hướng còn lại: `updaterSupports(key)` đọc
`/usr/local/bin/software-update` và đòi đúng branch guard
(`[ "$APP" = "<key>" ]`) phải có mặt. `software-update` chỉ vào máy qua imager
hoặc `setup.sh` — KHÔNG bao giờ qua OTA — nên máy provision trước khi có các key
này sẽ giữ mãi bản updater trả `Unknown app: codex`. Không có cửa này thì mỗi
vòng poll (5 phút) máy đó sẽ nói "thiết bị đang cập nhật", thở cam, fail lúc
apply rồi kẹt LED đỏ (nhánh lỗi không gọi `restoreLED`). Có nó thì máy cũ đơn
giản là không nhận update CLI — kết cục duy nhất chúng có thể có — và im lặng.
Khớp theo branch guard chứ không theo key trần: key còn xuất hiện trong comment
và trong usage string của bản updater không hề implement nó.

**Chữa máy đang dùng updater cũ.** `make upload-setup` còn publish bản raw tại
`{CDN}/software-update`, nên một lệnh SSH là đủ đưa máy ngoài thực địa lên bản
hiện tại:

```bash
sudo curl -fsSL https://cdn.autonomous.ai/os/software-update -o /tmp/su \
  && sudo bash -n /tmp/su \
  && sudo install -m 0755 /tmp/su /usr/local/bin/software-update
```

`bash -n` trước `install` chính là điểm mấu chốt: tải dở dang không được phép đè
lên một updater đang chạy tốt. Sau đó máy sẽ nhận update CLI ở vòng poll kế
tiếp — không cần restart, bootstrap đọc lại file mỗi vòng. OpenClaw giữ nguyên kiểm
tra `inPath`: nó cài bằng npm theo từng máy chứ không bake sẵn, và provisioning
cũ có thể chưa set `agent_runtime`.

---

## 5. Script Cập Nhật (`/usr/local/bin/software-update`)

Bash script được cài bởi setup.sh (và được imager bake sẵn vào image). Bootstrap
worker gọi script này để thực hiện cập nhật.

Script đọc URL metadata OTA từ `metadata_url` trong `/root/config/bootstrap.json`
(biến môi trường `OTA_METADATA_URL` nếu set sẽ override, dùng cho chạy thủ công/debug),
và exit lỗi nếu cả hai đều rỗng — không có URL hardcode.

### Xử lý HAL

```bash
"hal")
    # Giữ nguyên toàn bộ runtime và trạng thái service trước đó.
    systemctl stop hal
    mv /opt/hal /root/bootstrap/rollback/hal.previous

    # Build candidate ở thư mục kề. .env, venv và uv cache được copy từ
    # runtime đã giữ trước khi chạy uv sync.
    unzip -q "$ZIP" -d /opt/.hal.new
    cp -a /root/bootstrap/rollback/hal.previous/{.env,.venv,.uv-cache} /opt/.hal.new/
    (cd /opt/.hal.new && uv sync --python 3.12 --extra hardware)
    mv /opt/.hal.new /opt/hal

    systemctl restart hal
    curl -fsS http://127.0.0.1:5001/health
    # Lỗi staging hoặc health sẽ khôi phục hal.previous và trạng thái cũ.
    # Operator cũng có thể chạy: software-update rollback hal
    ;;
```

### Xử lý Codex

Codex CLI là binary musl tĩnh publish trên GitHub releases, nên khác mọi thành
phần khác: không có artifact nào của mình nằm trên GCS. Metadata chỉ chứa
`codex.version` (không `url`/`sha256`), URL release được ghép từ chính version
đó. Metadata lưu **semver trần** (`0.149.1`) vì đó là thứ `codex --version` in
ra; tiền tố tag upstream (`rust-v`) được ghép lại lúc tải. Giữ đoạn tải này
đồng bộ với `runtimes/codex/install.sh` và `scripts/imager/build-orangepi.sh`.

```bash
"codex")
    curl -fsSL https://github.com/openai/codex/releases/download/rust-v${VERSION}/codex-aarch64-unknown-linux-musl.tar.gz
    tar -xzf …            # bước giải nén CHÍNH LÀ kiểm tra toàn vẹn (artifact upstream không có sha256)
    install -D -m 0755 /usr/local/bin/codex /root/bootstrap/rollback/codex.previous
    install -m 0755 …     /usr/local/bin/codex
    codex --version       # abort nếu binary mới không chạy được
    # codex.service chỉ tồn tại sau khi đã switch runtime sang codex; mọi image
    # lamp/intern-v2 đều bake sẵn BINARY bất kể runtime, nên restart là có điều
    # kiện. Operator có thể rollback: software-update rollback codex
    systemctl restart codex
    ;;
```

Publish version bằng `make upload-codex <semver-trần>`, thả cho fleet bằng
`make promote-codex`.

Chỉ binary bị đụng: `config.toml`, `.env` và persona vẫn do presync hook sở hữu,
nên update không thể ghi đè cấu hình Codex của thiết bị.

### Xử lý Claude Code

Khác Codex, Claude Code **không** phải binary mình tự đặt: installer của
Anthropic sở hữu `/root/.local/share/claude/versions/<ver>` và trỏ
`/root/.local/bin/claude` vào đó, còn `/usr/local/bin/claude` chỉ là symlink của
mình. Nên update = chạy lại installer với version đã publish (installer nhận
version qua tham số vị trí: `install.sh [stable|latest|VERSION]`).

```bash
"claudecode")
    HOME=/root curl -fsSL https://claude.ai/install.sh | bash -s -- "$VERSION"
    ln -sf /root/.local/bin/claude /usr/local/bin/claude   # installer có thể trỏ lại symlink của nó
    claude --version                                       # abort nếu không chạy được
    systemctl restart claudecode                           # có điều kiện: unit chỉ tồn tại khi runtime đang active
    ;;
```

Cố ý **không có backup `.previous` / rollback target**: installer vẫn giữ thư mục
`versions/<ver>` cũ, nên muốn lùi thì publish version cũ rồi chạy
`software-update claudecode`, chứ không phải khôi phục file mình lưu. Publish
bằng `make upload-claudecode <semver-trần>`, thả bằng `make promote-claudecode`.

### Xử lý OpenCode

OpenCode dùng installer chính chủ (arch detection + giải nén là việc của
upstream); mình chỉ pin version và ép thư mục cài. Khớp với
`runtimes/opencode/install.sh` và bake trong imager — giữ cả ba đồng bộ.

```bash
"opencode")
    install -D -m 0755 /usr/local/bin/opencode /root/bootstrap/rollback/opencode.previous
    curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin bash -s -- --version "$VERSION"
    # …rồi bước copy dự phòng y như install.sh, phòng khi installer bỏ qua
    # OPENCODE_INSTALL_DIR và dùng ~/.opencode/bin.
    opencode --version
    systemctl restart opencode      # có điều kiện: unit chỉ tồn tại khi runtime đang active
    ;;
```

`OPENCODE_INSTALL_DIR` PHẢI đứng trước `bash`, không phải `curl` — trong pipeline
`VAR=x curl … | bash` biến chỉ bind vào `curl`. Vì đây đúng là binary ở đường dẫn
cố định nên nó CÓ backup `.previous`: `software-update rollback opencode` chạy
được, khác claudecode. Publish bằng `make upload-opencode <semver-trần>`, thả
bằng `make promote-opencode`.

### Xử lý Hermes (chỉ SSH — KHÔNG pin được nên không bao giờ auto-apply)

Hermes cài kiểu git; `runtimes/hermes/install.sh` ghi
`/usr/local/lib/hermes-agent/.install_method=git` chính là để updater upstream
nhận ra. Updater đó **không nhận version đích** — `hermes update` luôn nhảy lên
HEAD của upstream. Nên `hermes.version` publish ra chỉ quyết định *khi nào* fleet
update (qua `min_version`), không quyết định *bản nào*.

```bash
"hermes")
    hermes update                       # upstream không có tham số version
    hermes --version                    # abort nếu không chạy được
    # Version thực tế != version metadata → CẢNH BÁO, không fail: trên thiết bị
    # không có gì pin được nó.
    systemctl restart hermes-gateway    # unit là hermes-gateway.service, không phải hermes.service
    ;;
```

Tên unit lấy đúng theo khai báo trong `/usr/local/lib/os-runtimes/hermes/service`
(`hermes-gateway`), và lần chạy báo về version Hermes THỰC SỰ đạt được, không
phải version yêu cầu. Không có backup `.previous`: giống claudecode, bản cài do
tool upstream sở hữu chứ không phải file mình copy.

### Xử lý PicoClaw (ca cá biệt về version)

PicoClaw là binary trần từ GitHub releases của CHÍNH MÌNH (không tarball, khác
codex). `version` publish ra là **TAG** release (`v0.3.1-fixvision`), không phải
semver: `picoclaw version` in ra một chuỗi build không liên quan
(`nightly-44-g1959045c-dirty`), nên không thể suy ngược tag từ binary. Vì vậy
bản update ghi tag đã cài vào
`/usr/local/lib/os-runtimes/picoclaw/installed-version` — mọi chỗ kiểm version
phải đọc stamp này, không phải `picoclaw version`.

```bash
"picoclaw")
    curl -fsSL https://github.com/autonomous-ai/picoclaw/releases/download/${VERSION}/picoclaw-linux-arm64
    picoclaw --no-color version         # chạy TỪ THƯ MỤC TẠM trước: cổng kiểm tra toàn vẹn duy nhất
    install -D -m 0755 /usr/local/bin/picoclaw /root/bootstrap/rollback/picoclaw.previous
    install -m 0755 …                   /usr/local/bin/picoclaw
    echo "$VERSION" > /usr/local/lib/os-runtimes/picoclaw/installed-version
    systemctl restart picoclaw          # có điều kiện: unit chỉ tồn tại khi runtime đang active
    ;;
```

`make upload-picoclaw <release-tag>` kiểm tra tag có thật asset
`picoclaw-linux-arm64` trước khi publish — gõ sai tag nếu không sẽ chỉ lộ ra
dưới dạng OTA fail trên mọi thiết bị đang poll. Rollback dùng được
(`software-update rollback picoclaw`).

---

## 6. HAL Runtime — Nguồn & Tích Hợp

### Chiến lược: Copy code + Track thủ công

Code HAL runtime được **copy** từ project upstream open-source vào mono-repo này, rồi sửa đổi nhiều.

**Tại sao copy, không dùng submodule/subtree:**
- Cần **bỏ** phần LiveKit/OpenAI (thay bằng OpenClaw)
- Cần **thêm** HTTP API server (FastAPI) để OS Server bridge đến
- Cần **thêm** DisplayService (GC9A01 eyes + info, không có trong upstream)
- Cần **sửa** services cho phù hợp kiến trúc mới
- Phần overlap chỉ là drivers (~30-40% code upstream), phần còn lại viết lại

**Theo dõi upstream:**
- Nguồn: `https://github.com/humancomputerlab/lelamp_runtime`
- Ghi commit hash upstream vào `hal/UPSTREAM.md` khi copy
- Định kỳ check upstream cho driver-level fixes (servo protocol, LED timing, ...)
- Cherry-pick thủ công khi cần
- Bỏ qua thay đổi AI/LiveKit upstream (mình đã thay thế hoàn toàn)

**Các bước thực hiện:**
1. Clone `humancomputerlab/lelamp_runtime` về thư mục tạm
2. Copy driver code (`services/motors.py`, `services/rgb.py`, `services/audio.py`, `services/service_base.py`) vào `hal/services/`
3. Xoá toàn bộ code LiveKit, OpenAI, conversation
4. Thêm `hal/server.py` — HTTP API server mới (FastAPI)
5. Thêm `hal/services/display.py` — DisplayService mới cho GC9A01
6. Tạo `hal/UPSTREAM.md` ghi commit hash nguồn và ngày copy
7. Test trên thiết bị với phần cứng thật

### Cấu trúc Mono-repo

HAL nằm trong repo này dưới dạng subfolder Python, cùng với Go và TypeScript:

```
autonomous/
├── system/          # Go code (fork từ lobster)
│   ├── cmd/              # Go entrypoints
│   ├── server/           # Go HTTP layer
│   ├── internal/         # Go business logic
│   ├── bootstrap/        # Go OTA worker
│   └── domain/           # Struct dùng chung
├── system/web/      # TypeScript/React SPA (copy từ lobster, đổi intern→lamp)
├── hal/               # Python hardware drivers (MỚI)
│   ├── __init__.py       # Package init, expose __version__
│   ├── server.py         # HTTP API server (FastAPI) — MỚI, không từ upstream
│   ├── system/
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

Để phân phối qua OTA, HAL được zip từ folder `hal/`:

```
hal-{version}.zip
├── hal/                  # Full Python package
├── requirements.txt
└── VERSION
```

### HAL HTTP API (FastAPI trên port 5001)

HAL Python runtime expose HTTP API trên `127.0.0.1:5001`. OS Server (Go, port 5000) bridge request từ OpenClaw skills đến API này. Nginx proxy `/hw/*` chỉ cho caller trên cùng máy — client bên ngoài nhận 403. Swagger UI tại `/hw/docs` không truy cập được từ LAN.

```
OpenClaw LLM → curl 127.0.0.1:5000/api/servo → OS Server → http://127.0.0.1:5001/servo → HAL Python → Phần cứng
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
| `scripts/release/upload-os-server.sh` | OS Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-bootstrap.sh` | Bootstrap Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-web.sh` | Web SPA bundle | Build → zip → GCS → update metadata |
| `scripts/release/upload-hal.sh` | HAL Python runtime (MỚI) | Package → zip → GCS → update metadata |
| `scripts/release/upload-setup.sh` | Script setup | Upload lên GCS |
| `scripts/release/upload-setup-ap.sh` | Script setup AP | Upload lên GCS |
| `scripts/release/upload-skills.sh` | OpenClaw skill files | Upload lên GCS |
| `scripts/release/upload-openclaw.sh` | Version OpenClaw npm | Chỉ metadata (device chạy `npm install -g`) |
| `scripts/release/upload-codex.sh` | Version Codex CLI | Chỉ metadata (device tải tarball GitHub release) |
| `scripts/release/upload-claudecode.sh` | Version Claude Code CLI | Chỉ metadata (device chạy installer Anthropic) |
| `scripts/release/upload-opencode.sh` | Version OpenCode CLI | Chỉ metadata (device chạy installer opencode.ai) |
| `scripts/release/upload-picoclaw.sh` | TAG release PicoClaw | Chỉ metadata (device tải asset GitHub); kiểm tra tag có thật |
| `scripts/release/upload-hermes.sh` | Version Hermes (chỉ SSH, không pin được) | Chỉ metadata (device chạy `hermes update`) |
| `scripts/provision/install.sh` | CDN install shortcut | `curl ... \| sudo bash` trên Pi |
| `scripts/release/tag-release.sh` | Git release tag kèm OTA metadata snapshot | Fetch metadata.json → annotated tag → `git push origin <tag>` |

### `scripts/release/tag-release.sh` — Truy nguồn theo GPL v3 §6

Sau khi các upload component xong (`make upload-os-server upload-hal upload-web ...`), script này neo OTA metadata snapshot vào một git tag duy nhất:

```bash
make tag-release v0.0.8
# → curl https://cdn.autonomous.ai/os/ota/metadata.json
# → git tag -a v0.0.8 -F - (annotation = JSON metadata đẹp)
# → git push origin v0.0.8
```

Người mua chạy `os-server --version` trên thiết bị — giá trị lấy từ `git describe --tags --always --dirty` lúc build (`Makefile:VERSION`), nên resolve về tag gần nhất. Họ mở repo public (`github.com/autonomous-ai/autonomous-os`), tìm tag đúng, đọc annotation để xem chính xác version `os-server`/`hal`/`web`/`bootstrap` đã bake vào release đó, rồi checkout commit tương ứng để có source.

Guards trong script: từ chối nếu tag đã tồn tại local hoặc trên remote, từ chối nếu fetch metadata fail hoặc JSON invalid (`set -euo pipefail` + `jq .`). Override qua env: `OTA_METADATA_URL` (mặc định: `https://cdn.autonomous.ai/os/ota/metadata.json`), `TAG_REMOTE` (mặc định: `origin`).

---

## 8. Build & Version

### Go binaries (ldflags)

```makefile
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Go symbol giữ config.LampVersion (nội bộ, không thuộc deploy identity).
LDFLAGS_OS   := -X go.autonomous.ai/os/system/server/config.OSVersion=$(VERSION)
LDFLAGS_BOOT := -X go.autonomous.ai/os/system/bootstrap/config.BootstrapVersion=$(VERSION)

os-build-bootstrap:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap

os-build:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_OS)" -o os-server ./cmd/os-server
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
| Systemd services | 4 | **5** (+ hal.service) |
| Python runtime | Không có | **HAL** tại /opt/hal/ với venv |
| Hardware bridge | Không có | OS server HTTP → HAL HTTP (localhost proxy) |
| SPI usage | Chỉ LED | LED + **Display (GC9A01)** |

---

## 10. Câu Hỏi Mở

- [x] **HAL source**: Mono-repo. Driver code copy từ `humancomputerlab/lelamp_runtime` vào `hal/`, bỏ LiveKit/OpenAI, thêm HTTP API + DisplayService. Track upstream thủ công qua `hal/UPSTREAM.md`.
- [x] **HAL HTTP port**: `5001` (OS Server là `5000`).
- [x] **Bridge protocol**: HTTP proxy đơn giản. HAL chạy FastAPI trên `127.0.0.1:5001`, OS Server proxy từ port 5000.
- [ ] **Python version**: Pin Python 3.11+? Yêu cầu Python hiện tại của HAL?
- [ ] **Đóng gói HAL**: Include venv sẵn? Hay cài deps trên thiết bị? (Pi resources hạn chế cho `pip install`)
- [ ] **Display driver**: DisplayService (GC9A01) — nằm trong HAL Python? Hay module mới?
- [ ] **HAL config**: HAL cần config file riêng? Hay cấu hình qua OS Server?

---

> Tài liệu này mô tả toàn bộ hệ thống OTA và bootstrap.
> Xem [architecture-decision.md](../../robots/lamp/docs/vi/architecture-decision.md) cho quyết định kiến trúc.
> Xem [product-vision.md](../../robots/lamp/docs/vi/product-vision.md) cho tầm nhìn sản phẩm.
