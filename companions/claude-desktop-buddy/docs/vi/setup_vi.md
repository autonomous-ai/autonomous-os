# Cài đặt, Triển khai & Khắc phục sự cố

Hướng dẫn build `buddy-plugin`, đưa nó lên thiết bị, cấu hình, ghép cặp (pair) với
Claude Desktop, và khôi phục khi BLE gặp trục trặc.

## Build

Chạy từ **thư mục gốc của repo** (không phải thư mục này):

```bash
make buddy-build
# → cd claude-desktop-buddy && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w" -o buddy-plugin .
```

Lệnh này tạo ra một binary tĩnh `linux/arm64` tên `claude-desktop-buddy/buddy-plugin` cho
mục tiêu Raspberry Pi / OrangePi. Quá trình build dùng bản fork BLE được vendor qua
chỉ thị `replace` trong `go.mod` — không cần thiết lập gì thêm.

> Theo chính sách của repo, binary **không được commit**; phiên bản được tiêm/phân phối qua
> OTA. `VERSION_BUDDY` lưu chuỗi phiên bản hiện tại.

## Triển khai (Deploy)

Có hai con đường, cả hai đều đã được khai báo sẵn trong `scripts/`:

- **Lần đầu / tạo image đầy đủ** — `scripts/setup.sh` cài Buddy như một phần của quá trình
  cấp phát (provisioning) thiết bị.
- **Độc lập / cập nhật** — `scripts/setup-claude-desktop-buddy.sh` (chạy trên Pi
  với quyền root) tải binary từ metadata OTA và cài (lại) service.

Hoặc đẩy thủ công trong quá trình phát triển từ thư mục gốc của repo:

```bash
make upload-claude-desktop-buddy     # scripts/upload-claude-desktop-buddy.sh
```

### Bố cục trên thiết bị

| Path | Nội dung |
|------|------|
| `/opt/claude-desktop-buddy/buddy-plugin` | binary |
| `/opt/claude-desktop-buddy/VERSION_BUDDY` | phiên bản đã cài |
| `/opt/claude-desktop-buddy/chars/` | các thư mục được đẩy từ Claude Desktop |
| `/root/config/buddy.json` | config runtime (không bị ghi đè khi cập nhật) |
| `/var/lib/claude-desktop-buddy/stats.json` | bộ đếm phê duyệt/từ chối trọn vòng đời |
| `/var/log/claude-desktop-buddy.log` | log xoay vòng (2 MB × 10) |
| `/etc/systemd/system/claude-desktop-buddy.service` | unit |

### systemd unit

```ini
[Unit]
Description=Lamp Claude Desktop Buddy (BLE)
After=bluetooth.target lamp.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/claude-desktop-buddy
ExecStart=/opt/claude-desktop-buddy/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
SyslogIdentifier=claude-desktop-buddy
```

Chạy với quyền **root** (cần system D-Bus cho BlueZ agent và các tham số debugfs
để tinh chỉnh advertising). Khởi động sau `bluetooth.target` và `lamp.service`.

```bash
sudo systemctl enable  claude-desktop-buddy
sudo systemctl restart claude-desktop-buddy
sudo systemctl status  claude-desktop-buddy
journalctl -u claude-desktop-buddy -f
```

### OTA

`setup-claude-desktop-buddy.sh` đọc mục `claude-desktop-buddy`
(`version`, `url`) từ JSON metadata OTA
(`https://storage.googleapis.com/.../lamp/ota/metadata.json`) và cài
binary tương ứng. Việc tăng phiên bản tại đó chính là cách phát hành bản cập nhật cho toàn bộ thiết bị.

## Cấu hình

`config/buddy.json` (được triển khai tới `/root/config/buddy.json`). Các flag:
`-config <path>` (mặc định `/root/config/buddy.json`), `-log <path>`.

```json
{
  "enabled": true,
  "device_name": "Claude-lamp-{MAC}",
  "http_port": 5002,
  "lelamp_url": "http://127.0.0.1:5001",
  "lamp_url": "http://127.0.0.1:5000",
  "approval_timeout_sec": 30,
  "narration_lang": "vi"
}
```

| Key | Mặc định | Ý nghĩa |
|-----|---------|---------|
| `enabled` | `true` | nếu `false`, tiến trình ghi log rồi thoát ngay lập tức |
| `device_name` | `Claude-lamp-{MAC}` | tên BLE được quảng bá; `{MAC}` → 4 ký tự hex cuối lấy từ `/api/system/network` của Lamp, viết thường để khớp với `lamp-xxxx.local` |
| `http_port` | `5002` | cổng HTTP API cục bộ của Buddy |
| `lelamp_url` | `http://127.0.0.1:5001` | base URL của runtime phần cứng LeLamp |
| `lamp_url` | `http://127.0.0.1:5000` | base URL của Lamp Go API (bus monitor/sensing, tra cứu MAC) |
| `narration_lang` | `vi` | ngôn ngữ thuyết minh TTS (`en`/`vi`; không xác định → tiếng Anh) |

> **Các key bị bỏ qua / không tác dụng:** `led_mapping` (nếu có) **không được đọc** — nó không nằm
> trong struct `Config`; hành vi LED được hardcode trong `bridge.go`. `approval_timeout_sec`
> được parse nhưng **không dùng** — hiện chưa có timeout phê duyệt phía server.

Thiếu file config → dùng giá trị mặc định (kèm một dòng log). Lỗi parse → dùng mặc định.

## Ghép cặp với Claude Desktop

1. Đảm bảo service đang chạy và đang advertising
   (`journalctl -u claude-desktop-buddy -f` hiển thị `BLE advertising started`).
2. Trong bộ chọn **Hardware Buddy** của Claude Desktop, chọn thiết bị theo
   tên được quảng bá (`Claude-lamp-xxxx`).
3. Nếu được yêu cầu passkey, đọc nó từ journal:

   ```
   [agent] PAIRING PASSKEY for <device>: 123456 (entered 0/6)
   ```

   Thiết bị là một BlueZ agent kiểu **DisplayOnly** (nó không có màn hình riêng), nên
   nó ghi log passkey 6 chữ số; nhập số đó vào ô nhập của Claude Desktop.

> Liên kết hiện đang **không được mã hóa** (các flag GATT secure-only bị loại bỏ tại
> runtime vì client Mac không tự động khởi tạo SMP). `status.sec` báo
> `false`. Bonding sẽ được bật lại khi Desktop khởi tạo handshake.

## Khắc phục sự cố

### Không phát hiện được thiết bị / dừng advertising

Khởi động lại đồng thời stack Bluetooth và service:

```bash
sudo systemctl stop claude-desktop-buddy
sudo systemctl restart bluetooth
sleep 3
sudo bluetoothctl power on
sudo systemctl start claude-desktop-buddy
```

### Phát hiện chậm (Mac hiếm khi thấy thiết bị)

Journal hiển thị `WARN: bluetooth debugfs not available — using BlueZ default
1280ms advertising`. Advertising quá chậm so với cửa sổ quét (scan window) của macOS. debugfs phải
được mount và tiến trình phải chạy với quyền root để `tuneAdvIntervals()` hạ
interval xuống 100–200 ms. Kiểm tra xem `/sys/kernel/debug/bluetooth/hci*` có tồn tại không.

### "No response" trong bảng Hardware Buddy

Thường là do sai khớp về permission của characteristic / bonding. Bản build hiện tại
cố tình loại bỏ các flag secure-only để các characteristic có thể truy cập được qua
liên kết không mã hóa — nếu bạn bật lại chúng, Desktop phải hoàn tất bonding LE Secure
Connections trước, nếu không các thao tác đọc/ghi sẽ thất bại với lỗi "No response".

### Tin nhắn BLE bị rớt / hỏng

Các log như `dropped N-byte BLE message (truncated)` thỉnh thoảng xuất hiện là bình thường —
Write-Without-Response không có ACK nên BlueZ rớt gói khi tải cao. Một vài heartbeat đơn lẻ
vô hại; nhưng nếu rớt giữa chừng một lần truyền thì việc đẩy thư mục sẽ bị hủy (phải gửi lại).

### MAC phân giải thành `unk`

`WARN: failed to fetch mac ...` hoặc `mac is empty` nghĩa là `/api/system/network` của Lamp
không truy cập được/chưa sẵn sàng (Buddy thử lại ~15 lần / mỗi 2 s). Thiết bị
vẫn chạy, advertising với tên `Claude-lamp-unk`. Hãy đảm bảo `lamp.service` đang hoạt động.

### Các lệnh kiểm tra hữu ích

```bash
curl -s http://127.0.0.1:5002/health    # ble_advertising + uptime
curl -s http://127.0.0.1:5002/status    # state, connected, pending_prompt
bluetoothctl show                       # adapter powered + discoverable
```
