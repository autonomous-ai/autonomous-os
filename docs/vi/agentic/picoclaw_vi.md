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
> **Trạng thái: đạt parity về install; gateway vẫn chỉ-client.** PicoClaw nay đã có
> installer + hook pre-start phía thiết bị (`internal/picoclaw/install.sh` +
> `presync.sh`, được embed và đăng ký qua `install.go` → `runtimereg`), nên một lần
> switch `picoclaw.setup` sẽ cài, cấu hình và khởi động nó giống hermes (§1.1).
> Migrate persona/memory **2 chiều** qua reconciler Go — picoclaw có adapter
> `migrate_persona` (`runtime_picoclaw.go`), nên switch tới/từ nó mang
> SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE cả 2 chiều; **skills** chiều VÀO do `picoclaw
> migrate --workspace-only --force` trong hook presync lo (§1.1).
> Bản thân gateway Go vẫn **chỉ-client**: hầu hết method lifecycle in-process
> (`SetupAgent`, watcher identity …) vẫn no-op (§8) vì provisioning xảy ra ngoài tiến
> trình trong install.sh/presync. Ngoại lệ là `EnsureOnboarding` (`onboarding.go`, giữ
> khối OS-managed trong SOUL/AGENTS/HEARTBEAT cập nhật) và `StartSkillWatcher`
> (`skill_watcher.go`, auto-update skill từ CDN) — đều là thật (§1.1).
> Các gap còn lại (hook emotion-acknowledge, pin queue/steer) được theo dõi theo checklist
> [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) — xem đó trước khi nâng
> PicoClaw lên parity đầy đủ.

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

## 1.1 Cài đặt + provisioning (`install.sh` + `presync.sh`)

Một lần switch `picoclaw.setup` chạy `internal/device/switch_runtime.sh` (generic),
script này materialize các script nhúng của PicoClaw rồi điều phối chúng. Hai script
nằm cạnh backend và được embed + đăng ký trong `install.go`:

| Script | Đường dẫn trên đĩa | Chạy khi |
|---|---|---|
| `install.sh` | `/usr/local/lib/os-runtimes/picoclaw/install.sh` | lần switch đầu / `verify` thất bại |
| `presync.sh` | `/usr/local/bin/runtime-picoclaw-presync` | **trước mỗi lần** picoclaw start (và một lần cuối install) |

**`install.sh`** (một lần):
1. cài `jq` + `yq` + binary `picoclaw` đã pin (GitHub release,
   `picoclaw-linux-arm64`) vào `/usr/local/bin`;
2. `picoclaw onboard` (chỉ khi chưa có `config.json`) tạo `/root/.picoclaw` —
   workspace + `config.json` và `.security.yml` baseline;
3. ghi **`picoclaw.service`** (`ExecStart=/usr/local/bin/picoclaw gateway`,
   `HOME=/root`, `Restart=always`) — `picoclaw gateway` chỉ chạy foreground, nên
   khác hermes (có `gateway install --system`) ta tự bọc nó. Tên unit trùng tên
   runtime nên **không cần** file khai báo `os-runtimes/picoclaw/service`
   (switch-runtime mặc định lấy tên đó);
4. chạy hook presync một lần, rồi drop hook `verify` (`command -v picoclaw`) để
   switch-runtime phát hiện + tự-heal unit mồ côi.

**`presync.sh`** (mỗi lần switch — single owner của config model + channel, nên
tự-heal sau factory reset, giống presync của hermes):
- **§0 migrate** — chốt bằng marker `~/.picoclaw/.openclaw-migrated` (**không** check
  `workspace/skills` rỗng — PicoClaw có sẵn built-in skills nên thư mục đó luôn
  non-empty). Khi marker chưa có và `/root/.openclaw` tồn tại, stop openclaw rồi chạy
  `picoclaw migrate --workspace-only --force` để mang persona/memory/skills từ OpenClaw
  qua. **`--workspace-only`** nghĩa là migrate **không** đụng `config.json` — convert
  `openclaw.json` thành config picoclaw cho ra config hỏng, nên `config.json` giữ bản
  onboard hợp lệ và §1/§2 đắp model/channel/gateway lên trên. Sau đó làm các fixup
  migrate không làm:
  copy `HEARTBEAT.md` + `KNOWLEDGE.md` từ workspace openclaw (KNOWLEDGE.md là living-doc
  learnings của openclaw, seed từ template nhúng rồi append hằng ngày — migrate bỏ qua),
  xoá `AGENT.md` (để PicoClaw chạy đường legacy `AGENTS.md` — chế độ duy nhất đọc
  `IDENTITY.md`), và copy `IDENTITY.md` của openclaw qua (migrate cũng bỏ qua). Cuối
  cùng ghi marker. Factory reset xoá
  `/root/.picoclaw` sẽ xoá marker nên migrate chạy lại; migrate lỗi thì không ghi
  marker và thử lại ở lần switch sau.
- **§0.5 onboarding (`onboarding.go`)** — `EnsureOnboarding`, gọi lúc
  boot/config-change như openclaw/hermes, mirror reconcile của openclaw (rút gọn):
  - seed `KNOWLEDGE.md` từ template nhúng (`resources/KNOWLEDGE.md`) **chỉ khi chưa
    có** — bao case fresh device chỉ-picoclaw mà presync §0 không có bản openclaw để
    copy; không bao giờ overwrite;
  - inject khối managed `<!-- OS DO NOT REMOVE -->` vào `SOUL.md`
    (`ensureSoulMDBlock`, soul theo device-type từ `soul_ref` của DEVICE.md; giữ nội
    dung owner dưới `---`), `AGENTS.md` (`ensureAgentsMDBlock`, quy tắc
    skills/memory/priority), và `HEARTBEAT.md` (`ensureHeartbeatMDBlock`, synthesis
    hằng ngày) — mirror openclaw nhưng lược nội dung chỉ-openclaw, giữ các block cập
    nhật qua OTA os-server thường;
  - **capability-gate skills** (`pruneUnsupportedSkills`): xoá thư mục skill device
    không dùng được — skill được giữ nếu được `skills.Supported(caps)` hỗ trợ (gate y
    như openclaw) **hoặc** là built-in của picoclaw (`picoclawBuiltinSkills`:
    `agent-browser`, `github`, `hardware`, `skill-creator`, `summarize`, `tmux`,
    `weather`); còn lại trong `workspace/skills` thì xoá. Fail-open khi DEVICE.md không
    khai cap. Không reload (skill đọc per-turn);
  - khi có block đổi, **restart gateway** (`restartPicoclawGateway` → `systemctl
    restart picoclaw`) để nạp lại file workspace (log+skip nếu không có systemctl).
    Không dùng endpoint `/reload` của gateway — nó cần auth admin mình không có (token
    kênh pico bị từ chối) và chưa chắc re-read workspace markdown; restart thì chắc.
  - các bước đặc thù `openclaw.json` (đăng ký hooks/logging/controlUi) là N/A với
    `config.json` của picoclaw; pin queue/steer là TODO.

Một **skill watcher** riêng (`skill_watcher.go`, chạy lúc boot như openclaw) poll OTA
metadata mỗi 5 phút và tự cập nhật `workspace/skills/<name>` từ CDN khi version của
skill được hỗ trợ thay đổi (gate qua `skills.Supported`), rồi báo agent qua
`SendSystemChatMessage`.
- **§1 cấu trúc** (`jq` trên `config.json`) — `agents.defaults` (provider
  `anthropic-messages`, `model_name "autonomous"`, `restrict_to_workspace:false`,
  `allow_read_outside_workspace:true`), entry `autonomous` trong `model_list`, và
  khung `channel_list`. `channel_list.pico` luôn được bật.
- **§2 động** (secrets lấy từ `/root/config/config.json` cấp **project**, thắng) —
  `model_list[autonomous].api_base` từ `llm_base_url` (PicoClaw cần đuôi `/v1`, khác
  hermes), `.security.yml` `model_list."autonomous:0".api_keys` từ `llm_api_key`,
  bearer token `pico` (phải khớp `constants.go` `Token`), và mỗi kênh non-pico **chỉ
  bật khi có credential**: telegram (`telegram_bot_token` + `telegram_user_id`),
  discord (`discord_bot_token` + `discord_user_id`), slack (`slack_bot_token` +
  `slack_app_token` + `slack_user_id`), whatsapp native (`whatsapp_user_id` →
  `allow_from`, không token, quét QR lần đầu). Secrets nằm trong `.security.yml` dưới
  `channel_list.<ch>.settings`; phần cấu trúc ở `config.json`.

Log của gateway xác nhận cấu hình khi boot (`Gateway started on 127.0.0.1:18790`,
health ở `/health` `/ready` `/reload`, `Channels enabled: [pico]`). Cảnh báo
`SECURITY: Channel allows EVERYONE (allow_from is empty) channel=pico` là bình
thường: `pico` là gateway native cục bộ của thiết bị và cố tình không có `allow_from`.

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

## 7. Khả năng kênh (channel capability)

PicoClaw **chỉ chạy telegram**. Vòng nhận Telegram do **thiết bị sở hữu** (điều
khiển bởi `config.TelegramBotToken`), và PicoClaw không có delivery slack/discord
riêng. Ba phương thức kênh trong `internal/picoclaw/channels.go` mã hóa điều này
một cách trung thực:

| Phương thức | telegram | slack / discord / whatsapp |
|---|---|---|
| `SupportedChannels()` | trả về `[telegram]` (mục duy nhất) | — |
| `AddChannel(…)` | **no-op thành công** trung thực — telegram do thiết bị sở hữu, nên không có gì để ghi vào runtime | trả về `domain.ErrChannelNotSupported` |
| `RefreshChannelConfig(…)` | `("", nil)` — no-op thành công (không cần re-apply runtime) | trả về `domain.ErrChannelNotSupported` |

Đây là một phần của **mô hình capability generic toàn repo**: mọi runtime khai báo
`SupportedChannels()` và trả về `domain.ErrChannelNotSupported` (chuỗi
`"channel_not_supported"`) cho các kênh nó không chạy được, thay cho no-op im lặng
kiểu cũ. Hành vi not-supported dùng chung và `ChannelReconcile` sau khi switch được
mô tả trong [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) — xem ở đó
thay vì lặp lại tại đây.

**Khi switch TỪ openclaw → picoclaw:** nếu openclaw đã cấu hình slack/discord, các
kênh đó trở thành không hỗ trợ dưới PicoClaw. Sau khi switch, `ChannelReconcile`
báo cáo chúng trong trường `unsupported_channels` của uplink MQTT info
(`domain.MQTTInfoResponse`), và creds của chúng **vẫn nằm trong `config.json`** —
switch ngược lại openclaw sẽ khôi phục chúng.

## 8. Những phần để stub

Mọi thứ không nằm trên hot path của PicoClaw đều là no-op để thỏa interface
`domain.AgentGateway` mà không bịa ra tính năng backend không có: `SetupAgent`,
pairing WhatsApp, `ResetAgent`, `RestartAgent`, `RefreshModelsConfig`,
`FetchChatHistory`, `GetConfigJSON`, ghi MCP entry, `WatchIdentity`,
`UpdateIdentityName`, watcher model (`StartModelSync`/`StartPrimaryModelWatch`),
`UpdatePrimaryModel`. (`AddChannel` / `RefreshChannelConfig` KHÔNG phải stub — trả
`domain.ErrChannelNotSupported` cho kênh không hỗ trợ, xem §7; `EnsureOnboarding`
(§1.1) và `StartSkillWatcher` (auto-update skill, §1.1) là thật.) HAL TTS/voice, fan-out
Telegram, hàng đợi/drain sensing-event, và các helper run-marker (guard / broadcast /
web-chat / silent / pose-bucket) đều backend-agnostic và hành xử y hệt backend Hermes.

Những phần này no-op **có chủ đích**: PicoClaw được provisioning ngoài tiến trình
bởi `install.sh` + `presync.sh` (§1.1), không phải bằng các lời gọi gateway
in-process. Cài đặt, cấu hình model/channel, và migrate persona đều diễn ra trong các
script đó trong luồng `switch-runtime`. Ngoại lệ duy nhất là **`EnsureOnboarding`**
(`onboarding.go`) — nó là thật: inject khối OS-managed vào `workspace/AGENTS.md` lúc
boot/config-change (§1.1), đúng hợp đồng như openclaw.
