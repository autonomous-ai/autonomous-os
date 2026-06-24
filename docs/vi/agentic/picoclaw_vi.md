# Backend agent PicoClaw

PicoClaw là một trong các **backend agentic có thể hoán đổi** mà os-server chạy
phía sau agent gateway. Bộ não có thể cắm rời (CLAUDE.md): os-server nói chuyện
với backend mà `config.agent_runtime` chọn thông qua một interface duy nhất
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, drain sensing, fan-out Telegram) không cần biết bộ
não nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ. Xem `docs/agentic/hermes.md` + `internal/hermes`.
- **`picoclaw`**: client WebSocket bền tới runtime PicoClaw cục bộ. Tài liệu này. Code: `os/services/internal/picoclaw/`.

> Code là nguồn chân lý. Tài liệu này mô tả `internal/picoclaw/` đúng như đã
> triển khai; giữ đồng bộ khi thay đổi (EN: `docs/agentic/picoclaw.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · [`hermes_vi.md`](hermes_vi.md) (Hermes) ·
> file này (PicoClaw).
>
> **Trạng thái: chỉ-client / chưa hoàn chỉnh.** PicoClaw mới được nối như *client*
> gateway — **chưa có install, presync, migrate persona/memory, import/watch skill,
> onboarding**. Mọi thứ ngoài hot-path WS đều no-op (§7). Coi như chưa đạt parity;
> checklist trong `adding-agent-runtime_vi.md` là danh sách gap nếu sau này nâng nó
> thành backend đầy đủ.

## 1. Khi nào và chọn ra sao

`agent_runtime` trong `config.json` chọn backend; việc phân giải nằm ở
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| `"openclaw"` / để trống | OpenClaw (mặc định; hoặc `gateway.default` từ `DEVICE.md`) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) |
| giá trị khác | OpenClaw (log là `FALLBACK — unknown runtime=…`) |

Khi khởi động, `ProvideGateway` in banner `AGENT BACKEND ACTIVE → PICOCLAW` kèm
`ws_url`, `conversation`, và `source`.

### Onboarding / install nằm ngoài phạm vi

Khác OpenClaw và Hermes, backend này giả định **PicoClaw đã chạy sẵn** trên thiết
bị dưới dạng systemd service và mở sẵn WebSocket. os-server chỉ là **client** —
không có `install.sh`, không đăng ký `runtimereg`, không seed config. Luồng
chuyển runtime `picoclaw.setup` qua MQTT + `switch-runtime` (lật
`config.agent_runtime`) đã tồn tại sẵn (`server/device/delivery/mqtt/`,
`internal/device/switch_runtime.sh`); việc cung cấp chính service PicoClaw được
xử lý ngoài luồng này.

## 2. Hằng số kết nối

**Không có config theo từng máy**; endpoint là hằng số compile-time trong
`internal/picoclaw/constants.go`:

| Hằng | Mặc định | Ý nghĩa |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18790/pico/ws/` | Endpoint WebSocket PicoClaw cục bộ |
| `Token` | `darren_pico_token` | Bearer token gửi trong header `Authorization` khi connect |
| `Conversation` | `device-main` | Nhãn session mặc định cho tới khi server cấp `session_id` |

## 3. Transport

`client.go` giữ **một WebSocket bền** (gorilla/websocket), giống vòng reconnect
của openclaw nhưng đơn giản hơn — PicoClaw **không có handshake challenge /
pairing**, chỉ là bearer token:

1. `StartWS` dial `WSURL` với `Authorization: Bearer <Token>`.
2. Khi connect, trạng thái sẵn sàng bật (`IsReady`/`ConnectedAt`), LED
   `StateAgentDown` được xóa, và lần reconnect (không phải lần đầu) phát TTS
   reconnect i18n.
3. Một goroutine keepalive gửi `{"type":"ping","id":…}` mỗi 25s; PicoClaw đáp
   `pong` (bỏ qua) để làm tươi read deadline 90s.
4. Vòng đọc dịch từng frame đến và đẩy vào `domain.AgentEventHandler` đã đăng ký
   (đồng bộ — an toàn vì `FetchChatHistory` ở đây là no-op, nên handler không bao
   giờ block chờ một WS RPC).
5. Khi rớt: xóa busy + id lượt đang chạy, vẽ `StateAgentDown`, dừng servo
   tracking (chỉ thiết bị có motion), chờ 5s, reconnect.

## 4. Gửi một lượt

`chat.go` `sendChat` ghi một frame và trả về ngay (câu trả lời đến qua vòng đọc):

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>" }, "session_id": "<nếu biết>" }
```

- Lượt có ảnh thêm `payload.attachments: [{ "type": "image", "url": "data:image/jpeg;base64,…" }]` (best-effort; phần text luôn được gửi nên lượt vẫn chạy kể cả khi schema attachment bị bỏ qua).
- Trước khi ghi: đánh dấu busy, lưu `runID` làm **pending run id**, ghi pending chat trace, và phát flow event `chat_input` / `chat_send` (parity với openclaw).

PicoClaw xử lý **mỗi lần một lượt** và không stream token, nên các lượt được liên
kết bằng một `runID` đang chạy duy nhất thay vì id theo từng frame: pending run id
được frame đến đầu tiên của lượt nhận lấy.

## 5. Ánh xạ protocol đến → `domain.WSEvent`

Đây là phần then chốt để Flow Monitor / web-chat render đúng. Chỉ nhìn `type`
**không đủ** — `message.create` / `message.update` phải phân loại theo payload
(`placeholder` / `kind` / `tool_calls` / `content`), theo đúng thứ tự ưu tiên này
(`translator.go` `categorize`):

| Frame đến | Phân loại | `domain.WSEvent` phát ra |
|---|---|---|
| `typing.start` | bắt đầu lượt | `agent` lifecycle `phase:start` (một lần mỗi lượt) |
| `message.create/update`, `placeholder:true` | đang nghĩ | *(không có — trạng thái, không phải nội dung)* |
| `message.create/update`, `kind:"thought"` / `thought:true` | reasoning | *(không có — chỉ là trạng thái)* |
| `message.create`, `kind:"tool_calls"` / có `tool_calls` | gọi tool | `agent` tool `phase:start` + `phase:end` mỗi call |
| `message.create/update`, `content` khác rỗng (không dính các mục trên) | **câu trả lời cuối** | `chat` `state:final role:assistant` **+** `agent` lifecycle `phase:end` (kèm usage) — **kết thúc lượt** |
| `error` | lỗi | `agent` lifecycle `phase:error` — kết thúc lượt |
| `typing.stop` / `message.delete` / `pong` | — | *(bỏ qua)* |

### Lưu ý vòng đời lượt

- **`typing.stop` KHÔNG phải mốc kết thúc lượt.** Nó đến sớm, ngay sau giai đoạn
  nghĩ. Lượt chỉ kết thúc ở frame **final** đầu tiên (hoặc `error`).
- **Lượt không tool:** `typing.start → placeholder → typing.stop → message.update (final)`.
  Final là `message.update` dùng lại `message_id` của placeholder.
- **Lượt có tool:** `placeholder → typing.stop → message.delete (xóa placeholder)
  → message.create kind:"tool_calls" (×N) → message.create (sạch, final)`.
- PicoClaw không phát frame kết quả tool riêng, nên mỗi tool call phát `tool`
  `phase:start` rồi ngay sau là `phase:end` với result rỗng, chỉ để đóng trace.
- `media.create` có trong protocol nhưng server không bao giờ phát — media đi kèm
  trong `message.create` qua `attachments`.

### Cấu trúc tool call

Mỗi phần tử trong `tool_calls` theo kiểu OpenAI: tên + tham số nằm ở
`function.name` và `function.arguments` (là **chuỗi JSON**, không phải object).
Lời dẫn người-đọc-được của agent nằm ở `extra_content.tool_feedback_explanation`
(có thể lẫn ký tự điều khiển ANSI từ input terminal). Translator hiện chuyển tiếp
`name` + `arguments`; explanation chỉ được log chứ không hiển thị (device
`AgentPayload` không có chỗ cho nó).

### Token usage

`context_usage` (chỉ có ở frame final) là kích thước context tích lũy, không phải
input/output theo từng lượt. Ánh xạ thành `TokenUsage{ InputTokens: history_tokens,
TotalTokens: used_tokens }`.

## 6. Session

PicoClaw sở hữu session: `session_id` do server cấp được bắt từ frame đến bất kỳ
và lưu lại (`SetSessionKey`) để `message.send` kế tiếp gửi kèm. `NewSession` chỉ
xóa id cục bộ để lượt kế tiếp bắt đầu session server mới. Không có RPC compact nên
`CompactSession` là no-op.

## 7. Những phần để stub

Mọi thứ không nằm trên hot path của PicoClaw đều là no-op để thỏa interface
`domain.AgentGateway` mà không bịa ra tính năng backend không có: `SetupAgent`,
`AddChannel`, `RefreshChannelConfig`, pairing WhatsApp, `ResetAgent`,
`RestartAgent`, `RefreshModelsConfig`, `EnsureOnboarding`, `FetchChatHistory`,
`GetConfigJSON`, ghi MCP entry, `WatchIdentity`, `UpdateIdentityName`, watcher
skill/model, `UpdatePrimaryModel`. HAL TTS/voice, fan-out Telegram, hàng đợi/drain
sensing-event, và các helper run-marker (guard / broadcast / web-chat / silent /
pose-bucket) đều backend-agnostic và hành xử y hệt backend Hermes.
