# Remote Hermes — dùng Intern 2 làm frontend voice cho Hermes trên Mac

Intern 2 có thể làm frontend voice/chat cho 1 server Hermes chạy trên Mac của bạn, thay vì chạy agent local trên thiết bị. Thiết bị bắt tiếng nói, Mac chạy "não", câu trả lời được đọc lại trên thiết bị — tất cả qua Wi-Fi LAN.

Cơ chế: tái sử dụng Hermes runtime có sẵn trên thiết bị, chỉ đổi endpoint sang địa chỉ LAN của Mac. Không cần cài phần mềm "remote agent" riêng; 1 setup script nhỏ trên Mac sẽ bật HTTP API server có sẵn của Hermes rồi cho phép LAN truy cập.

---

## Cần chuẩn bị

- **Mac** đã cài Hermes agent CLI (bản [grid](https://grid.dev) chạy ngay; bản cài `pip`/`uv` cũng OK nếu `hermes` có trên `PATH`).
- **Intern 2** chạy OS build có option "Remote (external)" trong **Settings → Runtime**. Ship date: bản OS sau 2026-09-09.
- Mac và Intern 2 cùng **1 mạng Wi-Fi** (hoặc LAN có dây — miễn 2 máy thấy nhau qua IP).
- Không cần account, không cần cloud credential — kết nối là trực tiếp LAN giữa 2 máy.

---

## Cài đặt — 2 bước

### 1. Trên Mac — 1 lệnh

```bash
curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | bash
```

Script sẽ:

1. Tìm chỗ Hermes đã cài.
2. Thêm `aiohttp` vô môi trường Python của Hermes nếu chưa có (không có thì Hermes API server không bind được port).
3. Ghi `API_SERVER_ENABLED=true` + 1 API key random vô `~/.hermes/.env`. Nếu chạy lại lần sau, key cũ giữ nguyên — không rotate key đang dùng.
4. Khởi động (hoặc restart) Hermes gateway bind `0.0.0.0:8642` để thiết bị vào được qua LAN.
5. Print 2 field bạn cần paste vô thiết bị:

```
════════════════════════════════════════════════════════════
  Paste these into your Intern 2 web setup page
  (Settings → Runtime → Remote (external))
════════════════════════════════════════════════════════════

  Hermes URL : http://192.168.1.42:8642
  API Key    : intern2-hermes-a1b2c3d4e5f6a7b8
```

(IP + key thật của bạn sẽ khác. Copy y hệt.)

### 2. Trên Intern 2 — paste vô web UI

1. Mở web setup page của thiết bị trên browser (`http://<device-ip>` cùng Wi-Fi, hoặc setup portal bạn dùng lúc first boot).
2. Vào **Settings → Runtime**.
3. Dropdown "Backend" chọn **Remote (external)**. Hiện 2 field:
   - **Hermes URL** — paste `http://…:8642` từ output script trên Mac.
   - **API Key** — paste token từ cùng output đó.
4. Click **Switch to Remote (external)** rồi xác nhận.
5. Thiết bị restart os-server (~5s). Đợi runtime status hiện **Active**.

Xong. Mở tab Chat của thiết bị và gõ tin nhắn. "Não" giờ chạy trên Mac.

---

## Cách kiểm tra chắc chắn đang chạy qua Mac

3 cách xác nhận, từ dễ → chi tiết:

### (a) Xem kết nối vào

Trên Mac:

```bash
lsof -iTCP:8642 -sTCP:ESTABLISHED -n
```

Sẽ thấy 1 hoặc nhiều connection `ESTABLISHED` với đầu kia là IP thiết bị. Health poll giữ connection warm ngay cả khi không chat gì.

### (b) Tail access log lúc chat

Hermes gateway log mọi HTTP request khi chạy verbose. Setup script bật verbose mặc định; mỗi tin chat = 1 dòng `POST /v1/responses`:

```bash
tail -f /tmp/hermes-gateway.log | grep -E 'aiohttp\.access.*(POST|GET /health)'
```

Gõ tin trong web UI thiết bị. Mỗi tin sẽ print 1 dòng như:

```
INFO aiohttp.access: 192.168.1.10 [09/Sep/2026:14:37:12 +0700] "POST /v1/responses HTTP/1.1" 200 12273
```

Chỉ thấy `GET /health` mà không có `POST /v1/responses` = chat không tới Mac — xem mục "Troubleshoot" bên dưới.

### (c) Tự curl endpoint

Từ máy nào cũng được trong LAN (kể cả Mac):

```bash
curl -H 'Authorization: Bearer YOUR_TOKEN' http://YOUR_MAC_IP:8642/health
```

Kỳ vọng: `{"status": "ok", "platform": "hermes-agent", "version": "…"}`. Timeout hoặc network error = Mac không reach được qua IP/port đó — thường do firewall hoặc IP gõ sai.

---

## Troubleshoot

### Chat báo `agent gateway not connected`
Thiết bị restart runtime nhưng gateway chưa lên, hoặc URL/token sai.

1. Reload trang Runtime — status phải là **Active** (không phải **Starting…** hay error) trong 10s sau khi switch.
2. Curl lại từ thiết bị để verify reach:
   ```bash
   ssh …@<device-ip>   # hoặc dùng web CLI trên thiết bị
   curl -m 5 -H 'Authorization: Bearer YOUR_TOKEN' http://YOUR_MAC_IP:8642/health
   ```
   Kỳ vọng: `{"status": "ok", …}`. Timeout = URL, port, firewall, hoặc LAN routing sai.

### Script báo `Port 8642 already in use`
Hermes gateway cũ chưa tắt. Script cố dừng nó và đợi socket giải phóng, nhưng macOS giữ socket ở `TIME_WAIT` 1 chút. Chạy script lần 2 — thường lần 2 pass, hoặc đợi 30 giây rồi thử lại.

### Script không tìm được Hermes hoặc resolve sai Python
Script walk qua các install location phổ biến (grid, pipx, brew, `~/.hermes/hermes-agent/venv`, …); Hermes cài ngoài các path đó cần chỉ path thủ công.

Tìm Python đang chạy Hermes:

```bash
find $HOME/.hermes $HOME/hermes-agent $HOME/.local -name python -path '*/venv/*' 2>/dev/null
```

Rồi chạy lại với override. **Env var phải nằm BÊN PHẢI pipe** để apply cho `bash`, không phải `curl`:

```bash
curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | \
  HERMES_PY=/full/path/to/venv/bin/python bash
```

Tương tự cho `HERMES_BIN` nếu `hermes` CLI không có trên `PATH`, hoặc CLI auto-detect ra sai bản:

```bash
curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | \
  HERMES_BIN=/full/path/to/hermes bash
```

### Firewall Mac chặn incoming
Trên macOS: **System Settings → Network → Firewall**. Tắt firewall để test, hoặc thêm rule "allow incoming" cho binary `hermes` (CLI ở `~/.grid/tools/hermes-agent/bin/hermes` với bản grid).

### Chat OK nhưng dừng khi Mac sleep
`hermes gateway run` foreground sẽ thoát khi Mac sleep. Cài thành background service để sống qua sleep + reboot:

```bash
hermes gateway install
```

Sau đó chạy setup script lại — nó sẽ thấy launchd service, skip foreground restart, chỉ update env.

---

## Env var override

Setup script honor các env var sau — hầu hết user không cần đụng:

| Variable        | Default            | Vai trò                                                                     |
| --------------- | ------------------ | --------------------------------------------------------------------------- |
| `HERMES_ROOT`   | auto-detect        | Path Hermes cài (thư mục chứa `bin/python`).                                |
| `HERMES_HOST`   | `0.0.0.0`          | Bind address API server. `127.0.0.1` = chỉ Mac local.                       |
| `HERMES_PORT`   | `8642`             | Bind port. Phải match port thiết bị expect (mặc định `8642`).               |
| `HERMES_TOKEN`  | random hoặc cũ     | Force 1 API key cụ thể thay vì tự gen.                                      |

Ví dụ — force 1 token cụ thể để share giữa 3 Mac cùng team:

```bash
HERMES_TOKEN='team-shared-key-2026' curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | bash
```

---

## Lưu ý bảo mật

- API server bind `0.0.0.0` nghĩa là mọi thiết bị trên LAN đều có thể tới. Bearer token là gate duy nhất. **Chọn token mạnh** nếu LAN không tin cậy (café mở, guest Wi-Fi, v.v.) và coi token đó như password.
- Request qua endpoint này sẽ chạy tool call trên Mac với quyền của user account (file access, shell command, mọi thứ Hermes thường làm). Chỉ bind `0.0.0.0` trên network tin cậy.
- API key lưu plaintext trong `~/.hermes/.env`. Ai đọc được file đó là nói chuyện được với Hermes của bạn.
- Muốn cách ly nghiêm ngặt hơn, chạy Hermes trên Mac trong Docker container với `terminal.backend: docker` — Hermes doc có hướng dẫn.

---

## Chuyển ngược về agent local của thiết bị

Trên UI thiết bị, **Settings → Runtime** → chọn backend nào khác (OpenClaw là default). Trang Runtime restart os-server và thiết bị trở lại chạy "não" local.

Hermes trên Mac vẫn chạy; muốn tắt thì chạy `hermes gateway stop` trên Mac.
