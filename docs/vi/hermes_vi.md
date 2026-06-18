# Hermes — backend agent

Hermes là một trong các **backend agent có thể hoán đổi** mà os-server chạy phía
sau agent gateway. Bộ não là pluggable (CLAUDE.md): os-server nói chuyện với bất
kỳ backend nào `config.agent_runtime` chọn, qua đúng một interface
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, sensing drain, Telegram fan-out) không cần biết não
nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: client HTTP + SSE tới một Hermes API server cục bộ (kiểu OpenAI *Responses API*). Tài liệu này. Code: `os/services/internal/hermes/`.

> Nguồn sự thật là code. Tài liệu này mô tả `internal/hermes/` đúng như đã hiện
> thực; phải đồng bộ khi code đổi (EN: `docs/hermes.md`, VI: file này).

## 1. Được chọn khi nào và như thế nào

`agent_runtime` trong `config.json` chọn backend; resolve nằm ở
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| không set | fallback về `gateway.default` trong `devices/<type>/DEVICE.md`, rồi OpenClaw nếu cái đó cũng trống |
| `"openclaw"` | OpenClaw (mặc định) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| giá trị khác | OpenClaw (log là `FALLBACK — unknown runtime=…`) |

Khi `agent_runtime` không được set trong `config.json`, backend lấy từ
`gateway.default` của thiết bị (`devices/<type>/DEVICE.md`); chỉ dùng OpenClaw nếu
giá trị đó cũng trống. Banner log thêm `source` để biết nguồn nào thắng.

Lúc khởi động, `ProvideGateway` in banner `AGENT BACKEND ACTIVE → HERMES` kèm
`base_url`, `conversation`, `model`, `api_key_set`. **Chưa có config theo từng
máy** cho các giá trị này — chúng là hằng số compile-time trong
`internal/hermes/constants.go`:

| Hằng số | Mặc định | Ý nghĩa |
|---|---|---|
| `BaseURL` | `http://127.0.0.1:8642` | Hermes API server cục bộ |
| `APIKey` | `hermes-api-key` | Bearer cho Hermes |
| `Conversation` | `device-main` | Kênh mà mọi lượt đổ vào |
| `Model` | `hermes-agent` | Model id gửi cho Hermes |

Giả định Hermes đã chạy sẵn trên thiết bị tại `BaseURL` với skills đã provision;
os-server chỉ là client theo từng request.

## 2. Khác gì OpenClaw — và giữ nguyên gì

| | OpenClaw | Hermes |
|---|---|---|
| Transport | một WebSocket bền | HTTP POST + SSE không trạng thái, mỗi lượt |
| Trạng thái kết nối | socket lên/xuống | goroutine poll `/health` (`health.go`) lái `ready`/`connectedAt` |
| Session | chính socket | UUID phía server qua header `X-Hermes-Session-Id` (§3) |
| Pipeline downstream | — | **giống hệt** — Hermes dịch SSE → cùng các frame `domain.WSEvent` |

Vì Hermes phát đúng shape `domain.WSEvent` mà handler OpenClaw
(`server/agent/delivery/http/handler_events.go`) đã tiêu thụ, nên HAL TTS, định
tuyến marker `[HW:/…]`, monitor SSE, sensing drain và Telegram fan-out đều giữ
nguyên. `*hermes.Service` thỏa mãn đầy đủ `domain.AgentGateway` (`Name()`="Hermes",
`IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`/`SetBusy`, `QueuePendingEvent`,
`SendChat*`, `StartWS`, …).

## 3. Mô hình session & conversation

Hermes không có socket, nên "session" nằm phía server:

- Mỗi response mang header `X-Hermes-Session-Id` — một UUID cho mỗi conversation,
  ổn định qua các lần reconnect. `Service.sessionUUID` lưu bóng của nó.
- `Conversation` (`device-main`) là kênh có tên mà mọi lượt đổ vào; tất cả lượt
  chat/sensing/Telegram dùng chung để agent giữ một context.
- `Service.lastResponseID` cache `response.id` mới nhất, dùng để nối lượt (kiểu
  continuation của Responses API).

Trạng thái chỉ in-memory (`sessionUUID`, `lastResponseID`, `reqCounter` + các run
tracker guard / broadcast / web_chat / pose-bucket); không gì tồn tại qua lần
restart os-server.

## 4. Giao thức request — `POST /v1/responses`

`client.go` POST một `streamRequest` với `stream: true` rồi đọc luồng SSE:

```jsonc
{
  "model": "hermes-agent",
  "conversation": "device-main",
  "stream": true,
  "instructions": "…",        // text hệ thống/role, optional
  "input": "<text>",           // lượt text thường …
  "title": "…"                 // optional
}
```

Với lượt **vision**, `input` là mảng nhiều phần thay vì chuỗi — Hermes chấp nhận
cả hai dạng:

```jsonc
"input": [{ "role": "user", "content": [
  { "type": "input_text",  "text": "…" },
  { "type": "input_image", "image_url": "data:…" }
]}]
```

## 5. Dịch SSE → `domain.WSEvent`

Bộ tiêu thụ SSE (`client.go`) stream các event `response.*`; `translator.go` map
chúng thành frame `domain.WSEvent` và dispatch qua handler đăng ký bởi `StartWS`
— cùng đường OpenClaw dùng. Vòng đời lượt khớp OpenClaw: `activeTurn` bật true
khi gửi, false khi `response.completed`; kết quả completed mang `response.id`
(cache thành `lastResponseID`) và toàn bộ text assistant cho path send-and-wait.

Marker sensing/pose bị strip trước khi gửi bằng đúng các regex như OpenClaw
(`[snapshot: …]`, `[pose_bucket: …]`, `[pose_worst: …]`) để agent không bao giờ
thấy marker phần cứng nội bộ.

## 6. Trạng thái kết nối & health

Không socket nên liveness phải poll. `health.go` chạy poller `/health` lái
`ready`/`connectedAt`, suy ra `agentStartedAt` từ `/health/detailed.uptime_s` nếu
có, và dùng `hasConnected` để bỏ qua chime TTS "đã reconnect" ở lần poll thành
công đầu tiên. `AgentUptime()` báo uptime tiến trình Hermes, độc lập os-server.

## 7. Trạng thái busy & sensing event chờ

Hợp đồng giống hệt OpenClaw: khi một lượt đang active (`IsBusy`), các sensing
event thụ động bị drop hoặc buffer (`QueuePendingEvent`, last-write-wins theo
loại) và replay khi rảnh, để tín hiệu ambient không cắt ngang lệnh đang chạy.

## 8. Telegram fan-out

`telegram.go` / `telegram_sender.go` định tuyến phản hồi của agent về đúng chat
Telegram gốc. `markTelegramOrigin(runID, chatID)` ghi lượt đến từ đâu, còn
`consumeTelegramOrigin(runID)` đọc lại lúc trả lời, nên một lượt khởi từ Telegram
trả lời đúng chat mà vẫn chảy qua pipeline chung.

## 9. Voice

`hal.go` nối lượt Hermes vào path voice của HAL (TTS lúc speak-end, cùng entry
point `lib/hal` mà OpenClaw dùng), nên tương tác bằng giọng hoạt động như nhau
bất kể backend.

## 10. Vận hành

Hermes được cài bởi `os/services/internal/hermes/install.sh` (đặt cạnh phần hiện
thực của nó). Script này được **embed trong os-server** (`go:embed`, đăng ký qua
`lib/runtimereg`), nên đi kèm + OTA chung với binary; os-server ghi nó ra
`/usr/local/lib/os-runtimes/hermes/install.sh` và switch-runtime chạy bản local
đó — hoàn toàn offline, không cần CDN. (Đường CDN
`${RUNTIMES_BASE_URL}/hermes/install.sh` vẫn là fallback cho backend không
compile vào binary.) Installer kéo Hermes CLI về `/usr/local/bin/hermes`, chạy
`hermes claw migrate` (chỉ skills), seed `~/.hermes/.env`, **chỉ patch `.model`
+ `.custom_providers` trong `config.yaml`** (bằng `yq`, giữ nguyên phần còn lại
CLI đã ghi — không ghi đè cả file), drop hook `runtime-hermes-presync` (§11) và
**chạy hook đó một lần ngay trong install**, rồi cài + start gateway như một
**system service** qua `hermes gateway install --system --run-as-user root` +
`hermes gateway start --system` (unit: **`hermes-gateway.service`**). Vì presync
chạy ngay trong install, một lệnh `bash install.sh` trực tiếp đã được cấu hình
đầy đủ và chạy, không phụ thuộc switch-runtime.

> Tên unit: gateway chạy dưới `hermes-gateway.service`. Installer khai báo tên
> này trong `/usr/local/lib/os-runtimes/hermes/service` để `switch-runtime`
> enable đúng unit (§11); `reset_hermes.go` nhắm tới cùng unit đó.

`hermes claw migrate` **không** mang model config qua, nên hook presync sync
`llm_*` của thiết bị từ `config.json` vào config Hermes — một lần trong install,
và mỗi lần switch sau đó:

| `config.json` | → | Hermes |
|---|---|---|
| `llm_model` | → | `config.yaml` `.model.default` |
| `llm_base_url` | → | `config.yaml` `.custom_providers[0].base_url` |
| `llm_api_key` | → | `.env` `AUTONOMOUS_API_KEY` |

`.env` `API_SERVER_KEY` phải bằng `constants.go` `APIKey` (`hermes-api-key`) nếu
không mọi lượt sẽ 401. Hermes phải listen tại `127.0.0.1:8642` để khớp `BaseURL`.

Để trỏ tới Hermes endpoint / key / model khác ở hiện tại, sửa
`internal/hermes/constants.go` rồi build lại (việc cho phép cấu hình theo từng máy
là phần làm sau).

## 11. Switch backend lúc runtime

Bạn không sửa tay `config.json`. Ba trigger — **MQTT** `agent_runtime.set`
(`{"runtime":"hermes"}`), **HTTP** `POST /api/device/agent-runtime`, và section
**web** Settings → *Runtime* — đều dồn vào một hàm,
`device.Service.UpdateAgentRuntime` (`internal/device/service.go`). Nó validate
runtime, lưu `config.agent_runtime`, rồi chạy switcher trong một transient unit
systemd riêng (`systemd-run`, để cú restart os-server ở cuối không giết nó giữa
chừng):

```
switch-runtime <new> <old>
```

`switch-runtime` **generic, không biết backend cụ thể** — nó được embed trong
os-server (`internal/device/switch_runtime.sh` qua `go:embed`) và ghi ra
`/usr/local/bin/switch-runtime` khi cần, nên được version + OTA chung với binary
và **không cần đụng imager/setup.sh bao giờ**. Với backend đích `X` nó:

1. phân giải tên unit của `X` (mặc định `X.service`, hoặc tên mà installer khai
   báo trong `/usr/local/lib/os-runtimes/X/service` — hermes → `hermes-gateway`)
   và đảm bảo nó tồn tại, nếu chưa thì chạy installer của `X` — ưu tiên bản embed
   ở `/usr/local/lib/os-runtimes/X/install.sh`, không có thì
   `curl ${RUNTIMES_BASE_URL}/X/install.sh | bash` (openclaw được bỏ qua —
   `openclaw.service` đã bake bởi setup.sh);
2. chạy hook tùy chọn `/usr/local/bin/runtime-X-presync` (của hermes sync `llm_*`,
   theo §10);
3. `systemctl enable --now <X-unit>` + `disable --now <old-unit>`;
4. `systemctl restart os-server`, để `factory.go` resolve lại gateway.

Xác nhận đã switch qua banner `AGENT BACKEND ACTIVE → …` mới + một lần poll
`/health` khỏe trong log.

**Thêm backend mới** (picoclaw, claudecode, …) vì vậy chỉ là một `install.sh`
cạnh phần hiện thực của backend (`internal/<name>/install.sh`), `go:embed` +
đăng ký vào `lib/runtimereg` từ `init()` của package (phải tạo `<name>.service`,
tùy chọn drop `runtime-<name>-presync`), cộng một entry trong
`domain.AgentRuntimes` để validate + dropdown web. Backend mới vốn đã cần một
gateway client ở `internal/<name>` và một case trong `factory.go`, nên việc embed
installer không thêm coupling mới — và không đụng imager, switcher, hay CDN.
