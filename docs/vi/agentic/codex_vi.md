# Backend agent Codex

Codex là một trong các **backend agentic có thể hoán đổi** mà os-server chạy
phía sau agent gateway. Bộ não có thể cắm rời (CLAUDE.md): os-server nói chuyện
với backend mà `config.agent_runtime` chọn thông qua một interface duy nhất
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, drain sensing, fan-out Telegram) không cần biết bộ
não nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ. Xem `docs/agentic/hermes.md` + `internal/hermes`.
- **`picoclaw`**: client WebSocket bền tới runtime PicoClaw cục bộ. Xem `docs/agentic/picoclaw.md` + `internal/picoclaw`.
- **`codex`**: **OpenAI Codex CLI** làm bộ não agent của thiết bị, sau một WS bridge cục bộ. Tài liệu này. Code: `os/services/internal/codex/`.

> Code là nguồn chân lý. Tài liệu này mô tả `internal/codex/` đúng như đã triển
> khai; giữ đồng bộ khi thay đổi (EN: `docs/agentic/codex.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · [`hermes_vi.md`](hermes_vi.md) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) · file này (Codex).
>
> **Trạng thái: đã build xong, CHƯA verify trên thiết bị.** Toàn bộ stack
> (install, presync, gatewayd, translator, MCP, reset) compile và có unit-test,
> nhưng chưa có thiết bị nào chạy flow switch end-to-end. Hai điểm phải kiểm
> tra trên thiết bị được đánh dấu sẵn trong code: gatewayd phải listen ở
> `127.0.0.1:18792` với token trong `constants.go`, và ⚠️ campaign-api phải
> serve `{base}/responses` (Codex chỉ nói Responses API — xem §1.2).

## 1. Tổng quan & chọn ra sao

Codex CLI không có chế độ server riêng, nên thiết bị chạy một **WS bridge**
mỏng cục bộ: unit systemd `codex.service` chạy **`os-server codex-gatewayd`** —
bridge được **compile thẳng vào binary os-server** (`internal/codex/gatewayd`,
bản Go port của `bridge.py` tham chiếu; **không có Python trên thiết bị**).
Bridge mở `ws://127.0.0.1:18792/codex/ws/` (bearer token
`autonomous_codex_token`) và spawn **một subprocess mỗi turn**:

```
codex exec --json --dangerously-bypass-approvals-and-sandbox --cd /root/.codex/workspace
```

resume theo thread id lưu trong `/root/.codex/session.json` (`codex exec
resume <id>`). Turn được serialize nghiêm ngặt (queue có buffer + một worker).
Các cờ "nguy hiểm" là cố ý: appliance chạy root không bao giờ được block ở
prompt approval (đi cặp với `approval_policy = "never"` +
`sandbox_mode = "danger-full-access"` trong config.toml, §1.2).

`agent_runtime` trong `config.json` chọn backend; việc phân giải nằm ở
`internal/agent/factory.go` `ProvideGateway()` — `"codex"` →
`codex.ProvideService`, giá trị lạ rơi về OpenClaw. Khi khởi động, banner
`AGENT BACKEND ACTIVE → CODEX` in `ws_url` + `conversation`.

Hằng số kết nối (`internal/codex/constants.go`, không có config theo máy):

| Hằng | Mặc định | Ý nghĩa |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18792/codex/ws/` | Endpoint WebSocket của bridge cục bộ |
| `Token` | `autonomous_codex_token` | Bearer token khi connect; bridge đọc cùng giá trị từ `/root/.codex/.env` (`CODEX_WS_TOKEN`, do presync sở hữu) |
| `Conversation` | `device-main` | Chỉ là nhãn — Codex sở hữu thread id của nó (§3) |

## 1.1 Cài đặt (`install.sh`)

Một lần switch `codex.setup` chạy `internal/device/switch_runtime.sh`
(generic), script này materialize các script nhúng của Codex. `install.sh`
(một lần, tự-đủ — chạy thẳng `bash install.sh` cũng cấu hình VÀ khởi động
backend đầy đủ):

1. prerequisites `jq` + `curl`;
2. cài Codex CLI từ **GitHub release đã pin** (`rust-v0.142.5`, asset
   `codex-aarch64-unknown-linux-musl.tar.gz` — musl static, không cần runtime
   deps) vào `/usr/local/bin/codex`; idempotent (bỏ qua khi đúng version đã
   cài);
3. chạy hook presync một lần (`/usr/local/bin/runtime-codex-presync`, được
   os-server materialize TRƯỚC installer — §1.2);
4. ghi + enable **`codex.service`** (`ExecStart=/usr/local/bin/os-server
   codex-gatewayd`, `EnvironmentFile=/root/.codex/.env`, `HOME=/root`,
   `Restart=always`) — bridge không cần materialize gì, nó nằm sẵn trong
   os-server; rồi drop hook `verify` rẻ + offline (`command -v codex` + binary
   os-server tồn tại) để switch-runtime tự-heal.

Tên unit == tên runtime (`codex.service`), nên không cần file khai báo
`os-runtimes/codex/service`. Log install ghi vào `/root/.codex/install.log`
(rootfs bền — `/var/log` là zram volatile trên các board này).

## 1.2 Presync (`presync.sh`) — nhúng, chạy mỗi lần switch + mỗi lần boot

`presync.sh` được nhúng trong os-server và materialize ra
`/usr/local/bin/runtime-codex-presync`. Nó chạy trước mỗi lần codex start
(switch-runtime), một lần cuối install, **và mỗi lần os-server boot /
config-change qua `EnsureOnboarding`** (pattern hermes): `EnsureOnboarding`
hash các file presync sở hữu (`config.toml` + `.env`) quanh lần chạy và chỉ
restart gateway khi có thay đổi thật. Nó sở hữu mọi thứ stateful:

- **§1 MIGRATE** — copy persona/memory/skills một lần từ workspace openclaw,
  chốt bằng marker `/root/.codex/.openclaw-migrated`. Stop openclaw trước
  (retry 3 lần, non-fatal), rồi copy nguyên văn `IDENTITY.md`, `SOUL.md`,
  `KNOWLEDGE.md`, `HEARTBEAT.md`, `MEMORY.md`, `USER.md` **và `AGENTS.md`**
  (Codex đọc `AGENTS.md` natively — slot persona không cần dịch; Go onboarding
  vẫn inject lại khối OS), cộng `memory/` + `skills/` chỉ khi đích chưa có.
  Marker chỉ được ghi sau một lần copy sạch, nên migrate lỗi sẽ thử lại lần
  sau; factory reset xoá `/root/.codex` sẽ xoá marker nên migrate chạy lại ở
  lần switch kế.
- **§2 CONFIG** — regenerate phần đầu của `/root/.codex/config.toml` từ
  config.json. **Cổng auth:** khi `/root/.codex/auth.json` tồn tại (login
  subscription ChatGPT, §9) phần đầu được ghi KHÔNG có `model` /
  `model_provider` / `[model_providers.autonomous]` — codex dùng provider +
  model mặc định built-in (chỉ giữ `approval_policy` + `sandbox_mode`, và vẫn
  giữ nguyên phần đuôi `[mcp_servers`). Ngược lại (chế độ api-key):
  `model` từ `llm_model` (fallback `Auto-AI`),
  `model_provider = "autonomous"` → `[model_providers.autonomous]` với
  `base_url` từ `llm_base_url` chuẩn hoá kết thúc bằng `/v1` (Codex tự append
  `/responses`), `env_key = "OPENAI_API_KEY"`, và **CHỈ
  `wire_api = "responses"`** — wire chat-completions đã bị gỡ upstream ~2/2026
  (⚠️ VERIFY TRÊN THIẾT BỊ: campaign-api phải serve `{base}/responses`). Kèm
  `approval_policy = "never"` + `sandbox_mode = "danger-full-access"`. **Phần
  đuôi từ dòng `[mcp_servers` đầu tiên trở xuống được giữ nguyên văn** —
  `mcp.go` của os-server sở hữu các entry đó (§7), nên hai chủ sở hữu không
  giẫm nhau.
- **§3 ENV** — ghi `/root/.codex/.env` (systemd EnvironmentFile, mode 0600):
  `CODEX_WS_TOKEN` (phải khớp `constants.go` `Token`), `CODEX_PORT=18792`,
  `CODEX_HOME=/root/.codex`, `CODEX_WORKSPACE=/root/.codex/workspace`, và
  `OPENAI_API_KEY` từ `llm_api_key` — **bỏ qua ở chế độ subscription** (API
  key sẽ lấn át/xung đột với auth ChatGPT).

Trên nền lần chạy presync, `EnsureOnboarding` (`onboarding.go`) làm cùng phần
reconcile workspace như các backend khác: seed `KNOWLEDGE.md` từ template nhúng
chỉ khi chưa có, inject các khối OS-managed `<!-- OS DO NOT REMOVE -->` vào
`SOUL.md` / `AGENTS.md` / `HEARTBEAT.md` (gốc OpenClaw, lược phần
chỉ-OpenClaw), capability-gate skills, và restart gateway khi có khối thay đổi.

## 2. Transport & gửi một turn

`client.go` giữ một WebSocket bền tới bridge (khuôn picoclaw: bearer token,
không có handshake pairing, keepalive ping 25s → `pong`, reconnect có backoff,
LED `StateAgentDown` khi rớt). `chat.go` `sendChat` ghi một frame rồi return;
reply về trên read loop:

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>",
  "attachments": [{ "type": "image", "url": "data:image/jpeg;base64,…" }] } }
```

Bridge lưu attachment vào `/root/.codex/attachments` rồi truyền qua
`codex exec -i <path>`. Frame `{"type":"session.new"}` làm bridge bỏ thread id
đã lưu (§4). Codex xử lý một turn mỗi lần và không stream token, nên turn được
correlate bằng một `runID` in-flight duy nhất (pending run id được frame inbound
đầu tiên của turn nhận lấy).

## 3. Dịch event (`translator.go`)

Bridge forward các event JSONL của `codex exec --json` **nguyên văn** (cộng các
frame riêng của nó `bridge.status` / `bridge.error` / `pong`); translator Go
map chúng sang đúng khuôn `domain.WSEvent` mà handler OpenClaw tiêu thụ:

| Event inbound | `domain.WSEvent` phát ra |
|---|---|
| `thread.started` | bắt thread id làm session key + `agent` lifecycle `phase:start` (một lần mỗi turn) |
| `turn.started` | `agent` lifecycle `phase:start` (một lần mỗi turn) |
| `item.started` `command_execution` / `mcp_tool_call` | `agent` tool `phase:start` (`shell` / `server.tool`) |
| `item.completed` `command_execution` / `mcp_tool_call` | tool `phase:end` (phát start trước nếu chưa thấy) |
| `item.completed` `web_search` / `file_change` | cặp tool `phase:start` + `phase:end` |
| `item.completed` `agent_message` | **tích luỹ** — exec mode không có delta stream |
| `item.*` `reasoning` / `todo_list` | *(bỏ qua — status, không phải nội dung)* |
| `turn.completed` | `agent` `stream:assistant` (nguyên câu trả lời trong **một** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` kèm usage — kết thúc turn |
| `turn.failed` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — kết thúc turn |
| `bridge.status` / `pong` | *(log / bỏ qua)* |

Giống PicoClaw, text `agent_message` tích luỹ được đưa ra ở `turn.completed`
dưới dạng một assistant delta duy nhất **trước** `chat.final` /
`lifecycle.end` — trường hợp N=1 của hợp đồng streaming, chính nó cho consumer
chung flush TTS + marker phần cứng `[HW:/…]` tại `lifecycle.end`.

**Usage:** `turn.completed` mang `{input_tokens, cached_input_tokens,
output_tokens}`; translator map `input + cached → InputTokens` (xấp xỉ kích
thước context sống), `output → OutputTokens`, `TotalTokens = in + out`.

## 4. Session

Codex sở hữu session: thread id được bắt từ event `thread.started` và bridge
lưu trong `/root/.codex/session.json`, rồi replay bằng `codex exec resume <id>`
(lịch sử nằm trên đĩa ở `$CODEX_HOME/sessions/` — process thoát ≠ mất session).
`NewSession` gửi frame `session.new` → bridge bỏ thread id → turn kế tiếp là
fresh (best-effort khi socket đang rớt: id cũ resume lỗi thì bridge tự retry
fresh).

Codex **tự auto-compact context của nó** (`model_auto_compact_token_limit`),
nên `ShouldRotateSession` chỉ là **lưới an toàn 150k token** cho thread chạy
hoang — hiếm khi kích hoạt. Theo
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4 "No fake
success", `CompactSession`, `GetConfigJSON` (config của Codex là TOML + secrets
trong `.env` — không có file JSON để lộ ra), `UpdatePrimaryModel`, và
`RefreshModelsConfig` đều trả `domain.ErrNotSupportedByRuntime` — không bao giờ
`nil`. Đây không phải ngõ cụt: caller của `RefreshModelsConfig` rơi về
`EnsureOnboarding`, presync của nó đọc lại `llm_*` từ config.json và hash gate
restart gateway — nên thay đổi llm **được áp dụng live**, chỉ là không qua
method đó.

## 5. Kênh

Codex **không hỗ trợ kênh inbound nào**. Codex CLI không có channel layer riêng
(khác PicoClaw: binary runtime của PicoClaw tự poll Telegram Bot API — presync
của nó bật `channel_list.telegram` trong config riêng của PicoClaw), và
os-server cũng không chạy receive loop Telegram nào. Vì vậy
`SupportedChannels()` trả danh sách rỗng, còn `AddChannel` /
`RefreshChannelConfig` trả `domain.ErrChannelNotSupported` cho **mọi** kênh, kể
cả telegram — no-op success sẽ là giả (không có gì lắng nghe cả). Sau khi
switch, `ChannelReconcile` báo các kênh đã cấu hình trong `unsupported_channels`
của MQTT info uplink và credential vẫn nằm trong config.json (switch về
openclaw thì khôi phục).

Chiều outbound vẫn hoạt động: `TelegramSender` gửi cảnh báo chủ động
(sensing/guard) qua Bot API khi có `config.TelegramBotToken`. `SendToUser*`
nhận chat ID tường minh; `Broadcast` fan-out theo
`/root/.codex/telegram_targets.json` — file do operator tự seed, không có gì tự
ghi vào. Inbound là TODO(codex-telegram) còn mở — xem
`internal/codex/channels.go`. Xem thêm
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md).

## 6. Hooks

Codex không có hooks loader, nên hook `emotion-acknowledge` của OpenClaw được
tái hiện **native bằng Go** (`internal/codex/emotion_ack.go`, mirror
`emotion_ack.go` của hermes): mỗi turn user-visible, sendChat bắn
`{emotion:"thinking"}` sang HAL — cùng prefix skip, cùng intensity, cùng
capability gate (`skills.SupportedHooks`) như handler TS. Hook `turn-gate` đi
kèm cố ý không mirror (sendChat đã đánh dấu turn busy rồi). ⚠️ Giữ lockstep với
`hooks/emotion-acknowledge/handler.ts`.

## 7. Connector MCP (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` (flow MQTT `connector.set`) sửa
`/root/.codex/config.toml` `[mcp_servers.<name>]` qua **go-toml/v2**, ghi atomic
(temp + rename) dưới `mcpMu`, rồi restart gateway để lần `codex exec` kế nhận
server mới. Dịch shape từ entry canonical khuôn OpenClaw: **entry http map
`headers` → `http_headers` và key `type` bị bỏ** (Codex suy transport từ `url`
vs `command`); entry stdio đi qua nguyên vẹn. `RemoveMCPEntry` idempotent
(`removed=false`, không ghi, không restart khi entry vắng). presync chỉ
regenerate phần **đầu** của config và giữ nguyên đuôi `[mcp_servers` (§1.2),
nên các entry sống sót qua mọi lần sync.

## 8. Factory reset (`reset.go`)

`ResetAgent` (do `server/system/factoryreset.go` gọi trên gateway đang active)
không giữ lại gì — config.toml/.env được presync regenerate ở lần switch kế:
**stop** `codex.service` (+ verify inactive, poll 5s), **disable** nó (reboot
mặc định về openclaw), **xoá sạch `/root/.codex`** — config, auth CLI, thread
(`sessions/`), workspace, và marker `.openclaw-migrated` (để presync §1
re-migrate ở lần switch kế) — rồi tạo lại các thư mục baseline `workspace/` +
`attachments/` (Codex không có subcommand onboard; CLI tự tạo lại state dưới
`CODEX_HOME` ở lần chạy đầu).

## 9. Auth — ghi chú phase 2

Phase 1 (hiện tại) xác thực bằng **API key qua campaign-api**:
`OPENAI_API_KEY` = `llm_api_key` trong config.json, `base_url` của provider =
`llm_base_url` (§1.2). Auth theo subscription ChatGPT
(`codex login --device-auth`) **dời sang phase 2** — sẽ dùng chung plumbing
login-pairing với `ClaudeLoginPairer` của nhánh claudecode khi nhánh đó merge.

### Auth subscription (thủ công)

Đã dùng được ngay hôm nay mà không cần flow pairing phase-2: chạy
`codex login --device-auth` trên thiết bị, hoặc copy `~/.codex/auth.json` sẵn
có từ máy khác sang `/root/.codex/auth.json` (`chmod 600`). Presync tự phát
hiện `auth.json` ở mỗi lần chạy (tức mỗi lần boot): bỏ khối provider tuỳ chỉnh
khỏi config.toml và bỏ `OPENAI_API_KEY` khỏi `.env`, nên codex nói chuyện
thẳng với OpenAI bằng provider + model mặc định built-in — chế độ này **né
hoàn toàn blocker 404 `/responses` của campaign-api**. Xoá `auth.json` để quay
về chế độ api-key; việc chuyển đổi tự động ở lần presync kế.
