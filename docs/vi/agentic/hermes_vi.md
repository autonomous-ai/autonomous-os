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
> thực; phải đồng bộ khi code đổi (EN: `docs/agentic/hermes.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · file này (Hermes) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) (PicoClaw). Cơ chế switch/install/migration
> generic nằm ở file đầu; protocol đặc thù từng backend nằm ở các file kia.

## 1. Được chọn khi nào và như thế nào

`agent_runtime` trong `config.json` chọn backend; resolve nằm ở
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| không set | fallback về `gateway.default` trong `devices/<type>/DEVICE.md`, rồi OpenClaw nếu cái đó cũng trống |
| `"openclaw"` | OpenClaw (mặc định) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) — client WebSocket bền; giả định service PicoClaw đã chạy sẵn. Xem `docs/agentic/picoclaw.md` + `internal/picoclaw`. |
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

## 8. Channel (Telegram/Slack/Discord) — hiển thị inbound + fan-out

hermes gateway **sở hữu I/O của Telegram/Discord/WhatsApp**: nó tự poll các nền
tảng đó bằng token mà `presync` sync vào `~/.hermes/.env`, chạy turn, rồi reply
thẳng về chat. (**Slack là ngoại lệ** trên fleet này — app chạy ở chế độ
HTTP/Events, không phải Socket Mode, nên os-server bridge nó; xem mục Slack dưới
đây.) os-server không nằm trên đường channel của gateway, nên — khác OpenClaw (đẩy
WS event `session.message`) — một lượt channel do gateway xử lý sẽ KHÔNG hiện trong
Flow Monitor. Gateway cũng không có broadcast turn cross-platform để subscribe;
seam duy nhất là hệ thống **hook** của nó.

Vì vậy os-server cài một hook cho gateway, `os-server-observer`
(`internal/hermes/hooks/os-server-observer/{HOOK.yaml,handler.py}`, được
`ensureObserverHook` materialize vào `~/.hermes/hooks/` mỗi lần boot — xem §10).
Hook fire ở `agent:start` / `agent:end` cho **mọi** platform và POST lượt đó tới
endpoint loopback `POST /api/agent/channel-turn` (`handler_channel_turn.go`),
nơi emit đúng các flow event như một turn bình thường:

- `agent:start` → `chat_input` (source `channel`, kèm `sender` + `channel`) cùng
  `lifecycle_start`.
- `agent:end` → `lifecycle_end` cùng `tts_suppressed` mang text phản hồi (reply đi
  về channel chứ không ra loa thiết bị — cùng node mà đường channel của OpenClaw
  dùng, để web turn render được), hoặc `no_reply` cho lượt rỗng / `NO_REPLY`.

Hai event dùng chung một `run_id`, tương quan qua `session_id`. Handler
channel-agnostic (dựa vào field `platform`) và **skip** lượt `api_server` / `cli`
— đó là các call `/v1/responses` của chính os-server, đã được `sendChat` log rồi;
emit lại sẽ nhân đôi các turn khởi từ thiết bị. (Các lượt qua bridge Slack dưới đây
được lái qua `/v1/responses`, nên đã được `sendChat` log và cũng bị hook skip — không
đếm trùng.)

Gửi outbound (chủ động) — `Broadcast` / `SendToUser` trong `telegram.go` /
`telegram_sender.go` — đi thẳng tới Telegram Bot API cho các cảnh báo do thiết bị
khởi tạo, dùng bot token và danh sách chat trong `telegramTargetsFile`.

### Slack — bridge HTTP-mode (cho runtime chỉ-Socket-Mode)

`domain.SlackBridge` (`os/services/domain/slack_bridge.go`) là một **cơ chế
generic**, không riêng cho hermes: nó là interface cho **bất kỳ** runtime nào mà
hỗ trợ Slack native **chỉ là Socket Mode** (hiện tại: hermes là ví dụ duy nhất) và
do đó **không có webhook HTTP Slack local** để nhận event. Với một runtime như
vậy, chính os-server trở thành **Slack frontend kiểu HTTP-mode** — nó parse event,
chạy lượt, và post phản hồi qua Bot API. OpenClaw và picoclaw tự phục vụ webhook
HTTP Slack của chúng (webhook local `127.0.0.1:18789/slack/events` của OpenClaw),
nên chúng **không** implement `SlackBridge` và giữ nguyên path POST webhook local
hiện có.

**Yêu cầu Slack app.** Slack app phải **bật "Agents & AI Apps"** cùng các scope
**`assistant:write`** (trạng thái typing của assistant), **`chat:write`** (stream +
post), và **`im:history`** (đọc DM). Thiếu `assistant:write` thì trạng thái typing
bị bỏ qua âm thầm (best-effort) nhưng văn bản vẫn stream qua `chat:write`.

**Inbound** — Slack → proxy bff-campaign-service → MQTT `slack_event` → device.
`server/device/delivery/mqtt/slack_event_handler.go` (`forwardSlackHTTP`)
type-assert gateway đang hoạt động sang `domain.SlackBridge`. Khi khớp (hermes) nó
gọi `HandleInboundSlack`; khi không khớp (openclaw, picoclaw) nó giữ nguyên path
POST webhook local hiện có.

`internal/hermes/slack.go` `HandleInboundSlack` / `parseSlackInbound` decode JSON
Slack Events (challenge `url_verification` — phòng thủ, proxy public mới là nơi
thực sự sở hữu kiểm tra Request URL của Slack; `event_callback` với `event.type`
`message`/`app_mention`). Nó bỏ qua tin nhắn bot (`bot_id`), event `subtype`
(sửa/join), và event `user` rỗng (chống loop); áp cổng allowed-user qua
`config.SlackUserID` (rỗng = mở); strip mention `<@Uxxx>` ở đầu; và bắt `channel`,
`thread_ts`, `ts` của tin nhắn user, cùng `team_id`. Với một tin nhắn user thật, nó:

1. ghi origin Slack (channel + `thread_ts` + `ts` của tin nhắn user) vào map
   `slackRunOrigin`;
2. đặt trạng thái assistant thành **"...is typing"** qua
   **`assistant.threads.setStatus`** (`setSlackAssistantStatus`, best-effort, async
   — cần `assistant:write`);
3. đăng ký một session stream **lazy** (`startSlackStreamSession`) — chưa call
   Slack, chỉ là một goroutine theo-run sẵn sàng stream;
4. gửi lượt qua `SendChatMessageWithRun`;
5. thêm một reaction 👀 (`eyes`) vào tin nhắn của user (`setSlackReaction`, hằng số
   `slackAckReaction = "eyes"`, async).

**Streaming.** Phản hồi được render bằng API streaming native của Slack — chỉ báo
"…is typing" thật cùng văn bản chảy dần vào. Trong lượt, handler SSE của agent
(`server/agent/delivery/http/handler_event_agent.go`) feed văn bản phản hồi **đã
được dọn (cleaned) tích lũy** (`cleanedSlackStreamText` trong `handler_state.go`,
hàm này strip các HW marker và hoãn khi gặp marker `[HW:` còn dở / bất kỳ wrapper
`<say>` nào / `NO_REPLY` / `HEARTBEAT_OK`) vào `StreamSlackDelta` ở mỗi delta. Một
goroutine theo-run riêng (`slack_stream.go`) mở stream **lazy** ngay khi có nội
dung đầu tiên qua **`chat.startStream`** — gieo (seed) chính văn bản đầu đó dưới
dạng một chunk `markdown_text`, nên bubble **không bao giờ rỗng** — rồi append phần
đuôi mới qua **`chat.appendStream`** (các chunk `markdown_text`), throttle ~650 ms
(flush đầu tiên là tức thì qua một kick). Nó chỉ append phần đuôi mới (chưa-append),
theo thứ tự, nên vòng lặp delta SSE không bao giờ block vào một call HTTP Slack.
`chat.startStream` nhận `channel`, `thread_ts` (bắt buộc — trả lời trong thread
đang có, nếu không thì thread dưới tin nhắn của user), và `recipient_team_id` (bắt
buộc với channel, lấy từ `team_id` của event).

**Finalize phản hồi** — `handler_event_agent.go` gọi `DeliverSlackReply(runID,
text)` (`internal/hermes/slack.go`) cho runID đã hoàn tất. Hàm này tiêu thụ origin,
**xóa trạng thái assistant** (`setSlackAssistantStatus` với `""`), gỡ reaction 👀,
rồi `finishSlackStream` làm một **flush cuối + `chat.stopStream`** (việc này cũng
xóa chỉ báo typing và đánh dấu tin nhắn hoàn tất). Khi stream chưa từng mở (không
có nội dung tới nó, hoặc `startStream` cứ thất bại), nó fallback về một
`chat.postMessage` đơn (`PostSlackReply`). Các call Web API đều đi qua helper
generic `slackAPI` trong `internal/hermes/slack_sender.go`.

**Chặn TTS (cả hai nửa của lượt).** Một lượt xuất phát từ Slack không bao giờ tới
loa thiết bị; việc chặn được áp tại **hai** điểm qua peek **không tiêu thụ**
`IsSlackOriginRun(runID)` (để cả hai chạy trước khi `DeliverSlackReply` tiêu thụ
origin lúc reply): stream câu đầu giữa lượt (`canStreamSentenceTTS` trong
`server/agent/delivery/http/handler_text.go`) **và** phần còn lại cuối
(`isChannelRun`, đặt từ `isSlackRun`, trong `handler_event_agent.go`). Phản hồi đi
tới Slack, không tới loa.

**Các method Bot API dùng.** `chat.startStream` / `chat.appendStream` /
`chat.stopStream` (phản hồi streaming), `assistant.threads.setStatus` (trạng thái
typing), `reactions.add` / `reactions.remove` (ack 👀), `chat.postMessage`
(fallback + proactive).

**Outbound / proactive** — một `SlackSender` (`domain.ChannelSender`, trong
`slack_sender.go`) post tin nhắn sensing/broadcast tới `config.SlackUserID` qua
`chat.postMessage`; nó được nối vào danh sách `channels` của hermes trong
`internal/hermes/service.go` cùng với `TelegramSender`.

**`.env`** — `SLACK_BOT_TOKEN` (đồng bộ từ `config.json` bởi hook presync) là cái
bridge dùng cho mọi call Bot API. `SLACK_APP_TOKEN` không liên quan tới bridge HTTP
(nó chỉ dùng cho Socket Mode native) nhưng vô hại.

**Giới hạn phạm vi v1.** Bridge bỏ qua re-verify chữ ký request của Slack (path
qua MQTT broker đã được device xác thực và proxy đã verify chữ ký rồi) và hoãn
slash command (`slack_command`). Bản thân phản hồi chỉ là văn bản và ảnh đính kèm
bị bỏ trên path proactive.

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
compile vào binary.) Installer **cài Hermes CLI theo từng stage** (tải installer
upstream về một file tạm rồi chạy `bash <installer> --stage <name>
--non-interactive` cho lần lượt `prerequisites repository venv python-deps path
config`), **cố tình bỏ qua stage `node-deps`**: stage đó chạy `npm install` các
native module của browser-tool (node-gyp) bị treo vô hạn trên board ARM, mà một
voice lamp không bao giờ dùng browser tools (gateway thuần Python nên không cần).
Vì vậy nó **không** còn chạy bản monolithic `curl | bash --skip-setup`. Sau vòng
lặp stage, installer ghi `git` vào `/usr/local/lib/hermes-agent/.install_method`
để một lệnh `hermes update` về sau nhận ra đây là git install. Tiếp đó installer
dừng `openclaw` (để import skills không tranh chấp state đang chạy của nó), seed
các key `API_SERVER_*` trong `~/.hermes/.env`, rồi **giao toàn bộ phần config.yaml
+ skills cho hook presync** (gọi inline), và cuối cùng cài + start gateway như một
**system service** qua `hermes gateway install --system --run-as-user root` +
`hermes gateway start --system` (unit: **`hermes-gateway.service`**). Vì presync
lo phần config + skills (xem dưới) và installer chạy nó inline, một lệnh
`bash install.sh` trực tiếp đã được cấu hình đầy đủ và chạy.

Installer tee toàn bộ stdout+stderr vào `$HERMES_LOG`, mặc định là
**`/root/.hermes/install.log`** (rootfs bền), **không** phải `/var/log/hermes/…`:
trên các board này `/var/log` là một mount zram (log2ram) dễ bay khi reboot →
sẽ mất log install đúng lúc cần. Theo dõi trực tiếp bằng
`tail -f /root/.hermes/install.log`, hoặc override đường dẫn qua env
`HERMES_LOG=…` trước khi gọi.

> Tên unit: gateway chạy dưới `hermes-gateway.service`. Installer khai báo tên
> này trong `/usr/local/lib/os-runtimes/hermes/service` để `switch-runtime`
> enable đúng unit (§11); `reset_hermes.go` nhắm tới cùng unit đó.

### Unit gateway được tự-vá (pre-bake trong image + backstop runtime)

`IsReady()` và cổng gác của device-setup (`WaitForAgentReady`,
`internal/device/service.go`) đều chờ HTTP `/health` của gateway
(`127.0.0.1:8642`). Việc đó cần **unit `hermes-gateway.service` tồn tại** — chỉ có
binary `hermes` trên `PATH` (`hermes --version` chạy được) là **chưa đủ**. Unit
bình thường do `install.sh` tạo ở lần switch hermes đầu, nhưng device có thể tới
hermes mà *không* đi qua đường đó — ví dụ operator sửa tay `agent_runtime` trong
`config.json` thành `hermes` sau factory reset. Không có unit thì gateway không bao
giờ chạy, `WaitForAgentReady` time-out, `SetUpCompleted` vẫn `false`, device về AP
mode, và triệu chứng nhìn giống "**WiFi không kết nối được**" dù WiFi thực ra đã
associate thành công. Hai lớp khắc phục:

- **A — pre-bake trong image** (`imager/build-orangepi.sh`): ngay sau khi pre-bake
  binary Hermes CLI, image chạy `hermes gateway install --system` để ghi file unit,
  rồi `systemctl disable hermes-gateway` để nó **không** auto-start lúc boot
  (OpenClaw là runtime active mặc định; enable cả hai sẽ chạy 2 agent). Best-effort
  — chroot lúc build không có systemd đang chạy, nên nếu CLI không tạo được unit ở
  đó thì lớp B cài lúc runtime.
- **B — backstop runtime** (`ensureGatewayUnit`, `internal/hermes/gateway.go`, gọi
  từ `EnsureOnboarding`): khi unit vắng (`systemctl cat hermes-gateway` fail), nó
  chạy `hermes gateway install --system` theo nhu cầu và khai lại file
  `service`/`verify` cho switch-runtime. Nhanh — binary + venv đã pre-bake, nên chỉ
  ghi unit (không git clone / `uv sync`). `EnsureOnboarding` sau đó **`systemctl
  enable`** unit (factory reset disable nó — `reset_hermes.go` bước 4, "SetupAgent
  re-enables" — và unit vừa cài cũng chưa enable cho boot) và (re)start nó khi config
  đổi, khi unit vừa được cài, **hoặc** khi unit có nhưng không active (crash /
  disabled).

### Hook presync làm chủ `config.yaml` + skills

Model config trong `config.yaml` và skills openclaw-imported do **hook presync**
(`internal/hermes/presync.sh`) làm chủ, **không** phải `install.sh`. **os-server
materialize hook ra `/usr/local/bin/runtime-hermes-presync` mỗi switch**
(`materializePresync`, đăng ký qua `runtimereg.RegisterPresync`), nên OTA os-server
thường cũng refresh nó trên disk — khác với bản `install.sh` ghi một lần mà
`switch-runtime` skip ở switch sau (*activation gap*; xem
`docs/vi/agentic/adding-agent-runtime_vi.md` §3).

**Hook cũng chạy mỗi lần os-server boot VÀ lúc setup ban đầu**, không chỉ khi switch
— đều qua `EnsureOnboarding` (`internal/hermes/onboarding.go`), chạy `PresyncScript`
embed và restart `hermes-gateway` **chỉ khi** config thật sự đổi (guard content-hash
— không restart loop). Hash phát hiện thay đổi bao trùm **cả** `config.yaml` **và**
`.env` (`hermesEnvFile` = `/root/.hermes/.env`), nên một thay đổi chỉ-token-kênh (chỉ
đụng `.env`, vd thêm Slack live) cũng restart gateway — để Hermes server nhận kênh
mới:

- **Boot:** startup-sequence gọi `EnsureOnboarding`. Khắc ngách: device **boot thẳng
  vào Hermes** (`DEVICE.md gateway.default: hermes`, hoặc imager pre-install) chưa
  từng switch từ OpenClaw, hoặc `llm_*` đổi khi Hermes đang chạy, sẽ giữ `config.yaml`
  cũ không lấy `llm_api_key`/`base_url` thật từ `config.json`.
- **Setup:** `SetupAgent` (cũng trong `onboarding.go`) chỉ gọi `EnsureOnboarding`.
  Được vì **Hermes provision từ `config.json`, không từ `SetupRequest`** (khác
  OpenClaw, `SetupAgent` của nó viết `openclaw.json` thẳng từ request — nên OpenClaw
  cần *hai* hàm riêng, Hermes *một*). Device setup flow lưu `config.json` **trước**
  khi gọi `SetupAgent` (`internal/device/service.go` — call được cố ý đặt sau
  `config.Save()`), nên presync materialize `config.yaml`/`.env` từ key vừa nhập ngay
  lập tức thay vì chờ boot kế.

Cho Hermes khả năng self-heal config giống OpenClaw (`ensureAgentDefaults` +
`StartModelSync`), tái dùng đúng script presync thay vì viết lại sync trong Go. (Xoay
`llm_*` live qua `PUT /api/device/config` không reboot thì vẫn chờ boot kế — trigger
theo config-change là follow-up khả dĩ.)

Hook chạy ngay trước khi gateway start (lúc switch và boot, và inline lúc install),
làm 3 việc theo thứ tự:

1. **Restore skills** — khi `~/.hermes/skills/openclaw-imports` rỗng (cài đầu HOẶC
   sau factory-reset wipe), chạy `hermes claw migrate` (nó **copy** skills openclaw,
   không transform). Guard theo thư mục rỗng để switch thường là no-op (không
   re-import churn). `claw migrate` cũng đụng SOUL/MEMORY, nhưng vô hại: migrate
   persona Go (§12) chạy sau ghi đè sạch, chỉ skills trụ lại.
2. **Đảm bảo structure `config.yaml`** (idempotent — tự lành sau khi
   `hermes setup --reset` xoá trắng). Coerce `model: ''` bị reset về map, rồi khẳng định:
   - `.model.provider = custom:autonomous`
   - `.model.default = "Auto-AI"` — alias model campaign-api **cố định**. os-server
     gửi model request cố định (`constants.go` `Model`) mỗi lượt, nên **không** lấy
     từ `llm_model` (đó là model chính của OpenClaw, không liên quan Hermes).
   - `.custom_providers[0]` → `name: autonomous`, `key_env: AUTONOMOUS_API_KEY`,
     `api_mode: anthropic_messages`, `base_url` (mặc định campaign-api, override dưới).
3. **Sync giá trị theo máy** từ `config.json` (chỉ field khác rỗng, nên kênh chưa
   cấu hình giữ nguyên):

| `config.json` | → | Hermes |
|---|---|---|
| `llm_base_url` | → | `config.yaml` `.custom_providers[0].base_url` |
| `llm_api_key` | → | `.env` `AUTONOMOUS_API_KEY` |
| `telegram_bot_token` | → | `.env` `TELEGRAM_BOT_TOKEN` |
| `telegram_user_id` | → | `.env` `TELEGRAM_ALLOWED_USERS` |
| `slack_bot_token` / `slack_app_token` / `slack_user_id` | → | `.env` `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_ALLOWED_USERS` |
| `discord_bot_token` / `discord_guild_id` / `discord_user_id` | → | `.env` `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` / `DISCORD_ALLOWED_USERS` |
| `whatsapp_user_id` | → | `.env` `WHATSAPP_ALLOWED_USERS` |

`.env` `API_SERVER_KEY` phải bằng `constants.go` `APIKey` (`hermes-api-key`) nếu
không mọi lượt sẽ 401. Hermes phải listen tại `127.0.0.1:8642` để khớp `BaseURL`.

Để trỏ tới Hermes endpoint / key / model khác ở hiện tại, sửa
`internal/hermes/constants.go` rồi build lại (việc cho phép cấu hình theo từng máy
là phần làm sau).

### Capability kênh & add/refresh live

Hermes là một **channel runtime hạng nhất** trong luồng capability generic
(`internal/hermes/channels.go`). Hermes Agent giao **telegram / slack / discord**
natively ngay trong server của nó — một kênh được bật khi token của nó có mặt trong
`~/.hermes/.env` (Slack dùng Socket Mode → `SLACK_APP_TOKEN`), mà bảng map `.env` ở
§10 phía trên đã đổ từ `config.json`. os-server **không chạy receive loop kênh** của
riêng mình; việc duy nhất của nó là đặt creds vào `.env` rồi bounce gateway.

- **`SupportedChannels()`** trả `[telegram, slack, discord]`. **WhatsApp KHÔNG được
  hỗ trợ trên Hermes** (pairing Baileys chỉ có ở OpenClaw) → `AddChannel` /
  `RefreshChannelConfig` cho `whatsapp` trả `domain.ErrChannelNotSupported` (gate
  capability qua `domain.ChannelSupported`).
- **`AddChannel` và `RefreshChannelConfig` không còn là no-op.** Trước kia là stub
  `return nil` im lặng; giờ chúng re-sync `~/.hermes/.env` từ `config.json` bằng cách
  tái dùng primitive presync và restart `hermes-gateway` **chỉ khi config đổi**. Cơ
  chế: `syncChannelsEnv()` → `EnsureOnboarding()` → `runPresync()` (upsert các biến
  kênh trong `.env`) → hash-diff `config.yaml`+`.env` → `restartHermesGateway()`. Cả
  hai đều quy về "re-sync `.env` + restart-if-changed" nên dùng chung một code path.
- **Persist-then-apply.** Lớp device (`internal/device/service.go` `AddChannel`)
  gate capability trước, rồi persist creds kênh vào `config.json` **trước khi** gọi
  `AddChannel` của gateway, để presync đọc lại `config.json` và thấy token mới. Một
  apply fail tạm thời để creds đã persist (chiều phục hồi được — presync lúc boot /
  `ChannelReconcile` re-apply lại).
- **Switch runtime vs add live.** Khi **switch vào Hermes**, hook presync đã chạy
  trước khi gateway start nên slack/discord tự mang qua; code mới khắc ngách
  add/refresh **live** (thêm kênh khi đang chạy trên Hermes). `ChannelReconcile` lúc
  khởi động (`internal/agent/channel_reconcile.go`) cũng re-apply kênh sau switch,
  nhưng với Hermes nó thực chất là **no-op** — presync đã sync `.env` rồi nên hash-diff
  không thấy đổi và bỏ qua restart. Nó cũng ghi WhatsApp là không hỗ trợ
  (`ChannelsUnsupported`) cho info uplink, để creds đó lại cho lần switch về OpenClaw.

### MCP connectors (`mcp_servers` trong `config.yaml`)

Các remote-MCP connector (Notion, Linear, Asana, GitHub, Ahrefs, …) được nối bởi
luồng MQTT `connector.set` của backend là công dân hạng nhất trên Hermes:
`WriteMCPEntry`/`RemoveMCPEntry` (`internal/hermes/mcp.go`) upsert/xoá
`mcp_servers.<name>` trong `~/.hermes/config.yaml` và restart `hermes-gateway`,
mirror `internal/openclaw/mcp.go` (cái này sửa `mcp.servers` trong `openclaw.json`).

Connector writer giao cho gateway một entry chuẩn, shape kiểu OpenClaw —
`{type:"http", url, headers}` cho hosted MCP, hoặc `{command, args, env}` cho stdio.
`toHermesMCPEntry` dịch nó sang schema `mcp_servers` của Hermes: Hermes suy ra
transport từ việc có `url` hay `command`, nên discriminator `type` chỉ-OpenClaw bị
bỏ và `enabled: true` được khẳng định. Hook presync chỉ sửa
`.model`/`.custom_providers`/`.env` (qua `yq`) và để nguyên `mcp_servers`, nên hai
chủ sở hữu của `config.yaml` không đụng nhau; read-modify-write được tuần tự hoá
dưới `HermesService.mcpMu`. Như với OpenClaw, `config.yaml` phải đã tồn tại
(connector được cấu hình sau onboarding) — một `hermes setup --reset` xoá sạch
`mcp_servers` cùng với phần còn lại, và `connector.set` kế tiếp đẩy lại.

**Clone khi switch runtime.** `MCPReconcile` (`internal/agent/mcp_reconcile.go`)
mirror `ChannelReconcile`: gate bởi `config.MCPAppliedRuntime`, nó fire một lần
trong startup-sequence khi quan sát thấy switch, đọc MCP entry của runtime **trước
đó** thẳng từ config trên-disk của nó (`mcp.servers` trong `openclaw.json` ↔
`mcp_servers` trong `config.yaml`, normalize mỗi cái về shape chuẩn), và đẩy lại
chúng qua `WriteMCPEntry` của gateway giờ-đang-hoạt-động. Mỗi entry tự đủ (header
auth mang token inline), nên clone là phép copy config→config thuần — không có cơ
chế token-file/refresh. Một lỗi clone để marker chưa-tiến nên boot kế thử lại (không
chiều switch nào xoá config của runtime kia).

## 11. Switch backend lúc runtime

Cơ chế switch là **generic** (không biết backend cụ thể) và được mô tả đầy đủ ở
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §2–§3: ba trigger (MQTT
`hermes.setup`, HTTP `POST /api/device/agent-runtime {"runtime":"hermes"}`, web
Settings → *Runtime*) dồn vào `device.Service.UpdateAgentRuntime`, chạy
`switch-runtime <new> <old>` dưới `systemd-run --wait` và **chỉ ghi
`config.agent_runtime` sau khi exit 0 sạch** (nên crash giữa chừng resolve về
backend cũ vẫn đang cài). Các điểm đặc thù Hermes mà switcher generic dựa vào:

- **Tên unit** `hermes-gateway.service` (không phải `hermes.service`) — khai trong
  `/usr/local/lib/os-runtimes/hermes/service` để `switch-runtime` enable đúng unit;
  `reset_hermes.go` nhắm cùng unit.
- **Verify hook** `/usr/local/lib/os-runtimes/hermes/verify` chạy `command -v
  hermes` (check CLI rẻ). Cố tình **không** check structure config — config tự lành
  qua presync (§10), nên verify fail sẽ ép full reinstall vô ích.
- **Presync** `runtime-hermes-presync` chạy trước khi gateway start (§10).
- Ack MQTT `hermes.setup` phản ánh **kết quả thật** (success chỉ sau khi switch
  thành công; ngược lại failure kèm lý do rollback), vì `UpdateAgentRuntime` block
  chờ exit code của switcher.

Xác nhận đã switch qua banner `AGENT BACKEND ACTIVE → HERMES` + một lần poll
`/health` khỏe.

## 12. Persona, memory & skills mang qua khi switch

Switch openclaw→hermes chạy một migration persona Go
(`internal/agent/migrate_persona/openclaw_to_hermes.go`) lúc os-server boot —
**tách biệt với `claw migrate`**. Nó mang vào `~/.hermes/`:

- **SOUL.md** (rebrand) — và vì Hermes không có slot IDENTITY.md riêng, inline các
  field IDENTITY đã điền của owner thành block `## Your identity card` để tên tùy
  chỉnh (vd "Ngân") sống sót. `UpdateIdentityName` (đổi tên thiết bị) sửa block đó;
  `WatchIdentity` (`internal/hermes/identity.go`) poll SOUL.md và khi tên đổi thì
  đẩy wake words mới sang HAL + `i18n.SetDeviceName` — mirror `WatchIdentity` của
  OpenClaw, chỉ khác là watch SOUL.md thay vì IDENTITY.md.
- **MEMORY.md + daily `memory/*.md` + KNOWLEDGE.md** → merge vào `memories/MEMORY.md`.
  Hermes chỉ load `MEMORY.md` + `USER.md` **theo tên** (không glob `memories/*.md`),
  nên KNOWLEDGE được fold vào thay vì giữ thành file riêng bị bỏ qua.
- **USER.md** → `memories/USER.md`.

Copy soul dùng `Overwrite=true` (switch lấy persona của runtime nguồn; backup
trước). Chiều ngược hermes→openclaw **strip identity card khỏi SOUL VÀ restore các
field của nó về `IDENTITY.md` của OpenClaw** (`restoreIdentityCard`, nghịch đảo của
inline) — nên tên đặt dưới Hermes sống sót cả chiều về, không chỉ chiều đi.
**Skills** được giữ tươi dưới Hermes nhờ
`internal/hermes/skill_watcher.go` — auto-update từ CDN vào `skills/openclaw-imports`,
gate theo capability, mirror watcher OpenClaw (engine chung ở
`internal/skills/skillzip.go`).

**MCP connector cũng được mang qua** — các remote-MCP server đã cấu hình được clone
config→config bởi `MCPReconcile` ở cùng boot switch đó (xem §10, *MCP connectors*),
nên một thiết bị đã nối Notion/Linear dưới OpenClaw vẫn giữ chúng dưới Hermes (và
ngược lại).

### Round-trip không mất nội dung nhưng một-chiều về cấu trúc (đặc thù Hermes)

Persona, tên, user profile, và **nội dung** memory sống sót openclaw→hermes→openclaw
không mất. Asymmetry **cấu trúc** duy nhất là hệ quả của việc Hermes chỉ load
`MEMORY.md` + `USER.md` theo tên (không có slot `KNOWLEDGE.md`, không có daily-memory):

- Chiều xuôi **gộp** `KNOWLEDGE.md` + daily `memory/*.md` của OpenClaw **VÀO** một
  `MEMORY.md` của Hermes. Chiều về các entry đó đã merge sẵn nên đổ hết vào
  `MEMORY.md` của OpenClaw — **không tách lại** thành `KNOWLEDGE.md` hay file theo
  ngày. Không mất dữ liệu; cấu trúc bị làm phẳng.

Đây là đặc thù mô hình bộ nhớ Hermes — backend *có* slot đó sẽ map 1:1 và round-trip
sạch. (Xem quy tắc fold-vs-move ở [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4.)

> **Thêm backend khác** là công thức generic — xem
> [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) cho hợp đồng
> `AgentGateway`, mẫu install/presync, migration, skills, hooks, reset, và checklist
> đầy đủ.
