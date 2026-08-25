# Backend agent Codex

Codex là một trong các **backend agentic có thể hoán đổi** mà os-server chạy
phía sau agent gateway. Bộ não có thể cắm rời (CLAUDE.md): os-server nói chuyện
với backend mà `config.agent_runtime` chọn thông qua một interface duy nhất
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, drain sensing, fan-out Telegram) không cần biết bộ
não nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ. Xem `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: client WebSocket bền tới runtime PicoClaw cục bộ. Xem `docs/agentic/picoclaw.md` + `runtimes/picoclaw`.
- **`codex`**: **OpenAI Codex CLI** làm bộ não agent của thiết bị, sau một WS bridge cục bộ. Tài liệu này. Code: `runtimes/codex/`.

> Code là nguồn chân lý. Tài liệu này mô tả `runtimes/codex/` đúng như đã triển
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
bridge được **compile thẳng vào binary os-server** (`runtimes/codex/gatewayd`,
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
`system/agent/factory.go` `ProvideGateway()` — `"codex"` →
`codex.ProvideService`, giá trị lạ rơi về OpenClaw. Khi khởi động, banner
`AGENT BACKEND ACTIVE → CODEX` in `ws_url` + `conversation`.

Giá trị kết nối (`runtimes/codex/constants.go`). Cả ba đường dẫn thiết bị dưới
đây được resolve một lần lúc khởi động process, từ CÙNG các biến môi trường mà
gatewayd và `presync.sh` đọc (`system/lib/syspath`); không set env thì ra đúng
mặc định của thiết bị, nên board không bị ảnh hưởng:

| Giá trị | Mặc định | Env | Ý nghĩa |
|---|---|---|---|
| `WSURL` | `ws://127.0.0.1:18792/codex/ws/` | `CODEX_PORT` | Endpoint WebSocket của bridge cục bộ |
| `Token` | `autonomous_codex_token` | `CODEX_WS_TOKEN` | Bearer token khi connect; bridge đọc cùng giá trị từ `$CODEX_HOME/.env` (do presync sở hữu) |
| `codexHome` | `/root/.codex` | `CODEX_HOME` | State dir mà mọi đường dẫn codex khác dẫn xuất ra — workspace, skills, sessions, config.toml, `.env`, các file state telegram |
| `Conversation` | `device-main` | — | Chỉ là nhãn — Codex sở hữu thread id của nó (§3) |

Chỉ cần set `CODEX_HOME` là dời được cả backend, ở cả phía client lẫn
`codex-gatewayd` (gatewayd lấy nó làm gốc cho mặc định từng file). Đó chính là
cách `make os-dev` / `make codex-dev` chạy binary được ship ở ngoài thiết bị —
xem [os-server_vi.md § Chạy off-device](../os-server_vi.md#chạy-off-device-laptop).

## 1.1 Cài đặt (`install.sh`)

Một lần switch `codex.setup` chạy `system/device/switch_runtime.sh`
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
reconcile workspace như các backend khác: seed `KNOWLEDGE.md` **và `AGENTS.md`**
từ template nhúng chỉ khi chưa có, inject các khối OS-managed `<!-- OS DO NOT REMOVE -->` vào
`SOUL.md` / `AGENTS.md` / `HEARTBEAT.md` (gốc OpenClaw, lược phần
chỉ-OpenClaw), refresh khối AGENTS.md **toàn cục** ở tầng user
(`ensureUserAgentsMDBlock`, xem bên dưới), và capability-gate skills. Thay đổi
chỉ-markdown không bao giờ restart gateway — mỗi `codex exec` đọc lại
workspace; chỉ presync đổi config hoặc self-heal unit mới restart.

**Vì sao phải seed `AGENTS.md`.** Codex không có lệnh `setup` để sinh lại
`AGENTS.md` nền như openclaw, nên một **thiết bị chỉ chạy codex** — chưa từng
chạy openclaw, khiến presync §1 không có gì để migrate — hoàn toàn không có file
này. Mà `AGENTS.md` lại là file duy nhất codex tự nạp, nên không có file nghĩa là
không có khối OS **và không có persona**: agent tự giới thiệu là "Codex".
`EnsureOnboarding` giờ seed `runtimes/codex/resources/AGENTS.md` (một bản nền
ngắn, mang tiêu đề `Your Workspace` mà bộ inject khối bám vào) qua
`seedFileIfAbsent` — hàm này **không bao giờ ghi đè**, nên thiết bị đã migrate từ
openclaw vẫn giữ nguyên file của nó.

**Khối persona inline (AGENTS.md).** Codex chỉ tự nạp DUY NHẤT `AGENTS.md` vào
context; chỉ dẫn "Session Startup" bảo đọc `SOUL.md`/`IDENTITY.md` là tự
nguyện, và với turn ngắn model bỏ qua (đã xác minh trên thiết bị: "bạn tên gì"
→ "Tôi là Codex"). OpenClaw/Hermes inject soul vào system prompt ở tầng
runtime; codex không có tầng đó, nên `ensurePersonaInlineBlock` inline persona
THẲNG VÀO `AGENTS.md`: khối
`<!-- OS PERSONA INLINE — DO NOT EDIT (generated from SOUL.md + IDENTITY.md) -->`
… `<!-- /OS PERSONA INLINE -->` được upsert idempotent ở NGAY ĐẦU file (trên
khối OS mandatory), gồm phần mở đầu "Who you are" bắt buộc, tên agent parse từ
`IDENTITY.md` (`- **Name:** …`), và nguyên văn `SOUL.md` vừa được reconcile
(cắt tối đa 20 000 byte kèm ghi chú truncate để `AGENTS.md` nằm dưới trần
32 KiB project-doc của codex). Khối được dựng lại sau `ensureSoulMDBlock` mỗi
lần `EnsureOnboarding` và ngay sau khi đổi tên (`UpdateIdentityName`), nên
turn kế tiếp thấy tên mới luôn; `SOUL.md` biến mất thì khối bị gỡ. Ghi atomic
(tmp+rename), và chỉ ghi khi byte thực sự khác.

### Skills — discovery native `$CODEX_HOME/skills` (`codexSkillsDir`)

Skills của thiết bị nằm ở **`/root/.codex/skills/<name>/SKILL.md`** — thư mục
discovery **native** của codex-cli (`$CODEX_HOME/skills` trên 0.142.x). Codex tự
phát hiện mọi `<name>/SKILL.md` có YAML frontmatter hợp lệ ở đây, trong **mọi**
phiên bất kể cwd, và liệt kê trong picker skill `@` tương tác. Điều này giống fix
của claudecode (skills chuyển sang thư mục native `~/.claude/skills` của Claude
Code). KHÔNG đặt ở `workspace/skills` — codex không bao giờ quét nó; thiết bị để
skills ở đó sẽ có picker `@` rỗng và không nạp skill native. Mọi nơi tạo skill đều
trỏ tới `codexSkillsDir`: `presync.sh` §1 (migrate từ openclaw → `$CODEX_DIR/skills`),
`skill_watcher.go` (tải CDN + thông điệp `notifySkillChanges`), và
`pruneUnsupportedSkills` (capability gate). `migrateSkillsToCodexHome` nâng bất kỳ
`workspace/skills` cũ do os-server đời trước để lại vào thư mục native rồi xoá bản
workspace (idempotent); factory reset xoá toàn bộ `/root/.codex`, nên bộ skills
được migrate lại từ openclaw ở lần `EnsureOnboarding` kế tiếp.

**AGENTS.md toàn cục — luật cấp thiết bị cho phiên coding.** `AGENTS.md` trong
workspace chỉ tới được phiên **device-chat**: gatewayd chạy
`codex exec --cd /root/.codex/workspace` (`gatewayd/turn.go`), nên vòng quét
AGENTS.md từ repo-root→cwd của codex tìm thấy nó. Một **phiên coding Telegram**
(`telegram_coding.go`, mục "Telegram remote coding-sessions") chạy
`codex exec --cd <folder>` ở thư mục tuỳ ý (`/root`, `/root/myapp`, …), vòng
quét không bao giờ chạm file trong workspace. Codex còn nạp thêm một file
user-instructions **toàn cục**, `$CODEX_HOME/AGENTS.md` = `/root/.codex/AGENTS.md`,
trong **mọi** phiên bất kể cwd (codex-rs `CodexHomeUserInstructionsProvider`,
merge trước vòng quét project). `ensureUserAgentsMDBlock` (`codexUserAgentsMD`)
inject một khối OS ở đó — cùng kỷ luật marker như file workspace — mang luật
**Connectors (BẮT BUỘC)** và ghi chú rằng skills của thiết bị nằm ở đường dẫn
**tuyệt đối** `/root/.codex/skills/<name>/SKILL.md`. Không có nó, phiên coding
không biết connectors của thiết bị tồn tại và báo chủ máy rằng Gmail/Calendar
"chưa kết nối". Như một fallback đọc-theo-path (độc lập với discovery native), khối
`AGENTS.md` workspace và `notifySkillChanges` đều dẫn skill bằng đường dẫn
**tuyệt đối** đó, không bao giờ dùng `skills/…` tương đối — vốn sẽ resolve dưới
`<folder>` của phiên coding. Không cần restart gateway (đọc lại mỗi turn); factory
reset xoá toàn bộ `/root/.codex`, nên file toàn cục được dựng lại ở lần
`EnsureOnboarding` kế tiếp.

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
| `item.completed` `agent_message` | **giữ làm câu trả lời** — exec mode không có delta stream. Cái mới đẩy cái trước xuống `stream:thinking` (xem *Preamble* bên dưới) |
| `item.*` `reasoning` / `todo_list` | *(bỏ qua — status, không phải nội dung)* |
| `turn.completed` | `agent` `stream:assistant` (nguyên câu trả lời trong **một** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` kèm usage — kết thúc turn |
| `turn.failed` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — kết thúc turn |
| `bridge.status` / `pong` | *(log / bỏ qua)* |

Giống PicoClaw, text `agent_message` tích luỹ được đưa ra ở `turn.completed`
dưới dạng một assistant delta duy nhất **trước** `chat.final` /
`lifecycle.end` — trường hợp N=1 của hợp đồng streaming, chính nó cho consumer
chung flush TTS + marker phần cứng `[HW:/…]` tại `lifecycle.end`.

**Preamble.** Codex exec tự thuật trước khi gọi tool, thành một item
`agent_message` riêng ("Using the sensing skill for this presence event.",
"Posture summary is present, so this is the posture-nudge route."). Gộp hết
`agent_message` lại là đọc cả chuỗi tự thuật đó ra loa — đúng lỗi leak thấy ở
`presence.enter` và nudge `motion.activity`. Nên chỉ `agent_message` **cuối
cùng** của turn mới là câu trả lời: mỗi cái trước đó bị đẩy xuống
`stream:thinking` (chỉ vào Flow Monitor, không bao giờ ra TTS hay reply kênh)
ngay khi có cái mới hơn chứng minh nó không phải câu trả lời. Ngoại lệ: message
không phải cuối mà mang marker `[HW:/…]` là hành động phần cứng thật nên vẫn
giữ trong reply. Chỉnh prompt không chặn preamble một cách đáng tin — đây mới là
chỗ cưỡng chế.

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
nên `ShouldRotateSession` chỉ là **lưới an toàn 250k token** cho thread chạy
hoang — hiếm khi kích hoạt. Nó tính trên kích thước **context** đang sống —
`input_tokens + cached_input_tokens` của `turn.completed` gần nhất, được
translator lưu vào `lastContextTokens` — chứ không phải `totalTokens` mà handler
chung truyền vào (số đó cộng cả output của lượt này, là khối lượng turn chứ
không phải context). Tự đọc usage frame của mình giúp thay đổi này nằm gọn
trong codex: các backend khác không bị đụng tới.

Lưới này từng là 150k và tính trên `totalTokens` của handler cho tới
24/8/2026, khi thiết bị cho thấy nó kích hoạt trên turn bình thường chứ không
phải turn chạy hoang — 3 trong 8 turn sensing liên tiếp trên lamp-0c89 vượt
ngưỡng (context 153k / 170k). Mỗi lần rotate là mất thread, thread mới lại
shell-đọc lại toàn bộ `SKILL.md` (6 lần gọi, ~60s), đẩy context vượt ngưỡng
ngay lập tức: một vòng lặp rotate. Lưới an toàn phải nằm **trên** mức mà
compaction của codex ổn định lại, không phải nằm trong đó. Theo
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4 "No fake
success", `CompactSession`, `GetConfigJSON` (config của Codex là TOML + secrets
trong `.env` — không có file JSON để lộ ra), `UpdatePrimaryModel`, và
`RefreshModelsConfig` đều trả `domain.ErrNotSupportedByRuntime` — không bao giờ
`nil`. Đây không phải ngõ cụt: caller của `RefreshModelsConfig` rơi về
`EnsureOnboarding`, presync của nó đọc lại `llm_*` từ config.json và hash gate
restart gateway — nên thay đổi llm **được áp dụng live**, chỉ là không qua
method đó.

## 5. Kênh

### Telegram (receive loop device-owned)

Telegram dưới Codex là **device-owned**. Codex CLI không có channel layer riêng
(khác PicoClaw: binary runtime của PicoClaw tự poll Telegram Bot API — presync
của nó bật `channel_list.telegram` trong config riêng của PicoClaw), nên
os-server tự chạy receive loop inbound: `runtimes/codex/telegram_poll.go`, một
goroutine khởi động từ `StartWS` (nằm ngoài vòng reconnect nên sống qua các lần
WS rớt). Vì loop nằm trong lifecycle của service codex, nó chỉ chạy **khi codex
là runtime đang active** — không bao giờ tranh `getUpdates` với poller của
openclaw/hermes (Telegram trả 409 khi có poller song song).

Loop long-poll `getUpdates` (cửa sổ 50 s, client timeout 70 s) và đọc
`config.TelegramBotToken` / `TelegramUserID` **mới ở mỗi vòng lặp** — lưu hay
xoay credential không cần restart; khi token rỗng thì kiểm tra lại mỗi 30 s,
lỗi HTTP/mạng thì backoff 5 s. Một tin nhắn chỉ được chấp nhận khi là tin
**text** không rỗng, trong chat **private**, và `from.id` bằng
`TelegramUserID` (so sánh chuỗi sau `strconv`); mọi thứ khác bị bỏ qua ở mức
debug nhưng offset vẫn tiến (không re-deliver). Offset kế tiếp được ghi
nguyên tử (temp + rename) vào `/root/.codex/telegram_offset.json`, và chat id
của mỗi tin được chấp nhận được upsert vào
`/root/.codex/telegram_targets.json` để chiều outbound `Broadcast` (cảnh báo
chủ động sensing/guard) tới đúng chat.

Mỗi tin được chấp nhận chờ agent rảnh (`IsBusy`, poll 500 ms, tôn trọng ctx)
rồi được inject qua `sendChat` với flow source `telegram`, nên `chat_input` /
`chat_send` phát như bình thường và Flow Monitor thấy rõ nguồn gốc. Văn bản
turn được inject có prefix metadata người gửi — format chính xác
`[telegram] Message from <FirstName LastName> (@username) [id:<numeric>]:\n<text>`,
do `tgUser.label()` dựng (phần `(@username)` bị bỏ khi không có, tên fallback
về `unknown`) — để agent biết ai đang nói và trên kênh nào, mirror hành vi
telegram plugin của openclaw. Run được
đánh dấu **silent** (reply không được đọc qua TTS) và theo dõi trong
`telegramRuns`; tại `turn.completed`, `emitFinal` consume tracker và DM văn
bản cuối về đúng chat gốc, sau khi strip marker phần cứng `[HW:/...]` và audio
tag TTS (`[laugh]`, `[sigh]`, …) — `stripForChannel` trong `hal.go`, mirror
`hwMarkerRe` phía downstream và whitelist audio-tag của HAL. Khi `turn.failed`,
tracker được consume mà không DM để map không leak.

Trong lúc turn chạy, một goroutine `telegramTypingKeeper` giữ chỉ báo
"đang nhập…" của Telegram: ngay sau khi turn được inject, nó bắn Bot API
`sendChatAction(typing)` lập tức rồi lặp lại mỗi 4 s (chỉ báo tự hết hạn sau
~5 s) cho tới khi run được consume — reply đã DM qua `emitFinal` hoặc turn
lỗi qua `handleError` — và bị chặn trần bởi `telegramTypingLifetime` = 10 phút
để một turn kẹt không thể làm chat "đang nhập…" mãi mãi. Việc gửi là
best-effort (lỗi chỉ log ở mức debug rồi bỏ qua).

### Slack (đường proxy HTTP-mode)

Slack cũng là **device-owned**, qua đường proxy HTTP-mode (mô phỏng bridge
của hermes, `runtimes/hermes/slack.go`): proxy công khai bff-campaign-service
nhận các delivery Slack Events API và fan-out qua MQTT tới handler
`slack_event` của thiết bị
(`server/device/delivery/mqtt/slack_event_handler.go`), handler dedup theo
`event_id` (LRU trong bộ nhớ, TTL 5 phút) rồi type-assert gateway đang active
sang `domain.SlackBridge`. `CodexService` implement bridge đó
(`runtimes/codex/slack.go`), nên event chỉ được route về đây **khi codex là
runtime đang active** — không sửa gì code dispatch phía server, cùng cách nối
dây với hermes. Không dùng Socket Mode; thiết bị không bao giờ mở WebSocket
tới Slack.

Xử lý event mirror hermes: `url_verification` echo lại challenge; chỉ event
`message` / `app_mention` từ user thật mới qua (tin của bot, event có subtype
và event không có user bị bỏ qua — loop guard); mention bot ở đầu bị strip; và
khi `config.SlackUserID` được set, chỉ user đó được phép tạo turn (rỗng = mở,
workspace/app đã tự giới hạn phạm vi). Reply đi vào thread sẵn có nếu tin nhắn
nằm trong thread, ngược lại thread dưới tin của user (`thread_ts` fallback =
`ts` của tin).

Tin được chấp nhận sẽ được inject bất đồng bộ (goroutine): chờ agent rảnh
(`IsBusy`, poll 500 ms, chặn trần 2 phút — quá trần thì tin bị bỏ, vì MQTT
handler đã ack và Slack sẽ không gửi lại), rồi đi qua `sendChat` với flow
source `slack` và prefix metadata người gửi
`[slack] Message from <@U…> [channel:C…]:\n<text>`. Run được đánh dấu
**silent** (không TTS) và theo dõi trong `slackRuns` (runID →
channel/thread_ts/ts của tin); việc nhận tin được xác nhận bằng **reaction
eyes** trên tin của user (best-effort). Tại `turn.completed`, `emitFinal`
consume tracker và post reply — đã làm sạch bằng `stripForChannel`, như
telegram — về đúng channel/thread qua `chat.postMessage`
(`config.SlackBotToken`), đồng thời xoá reaction eyes; khi `turn.failed`,
tracker được consume và chỉ xoá reaction (không reply). Khác hermes,
**không có progressive streaming** (`chat.startStream`/`appendStream`) và
không có status "…is typing" của assistant — codex exec trả reply nguyên
khối, nên `StreamSlackDelta` là no-op và văn bản cuối chỉ được post một lần;
`DeliverSlackReply` (do shared agent handler gọi) là safety net
consume-nếu-còn, bình thường là no-op vì `emitFinal` đã consume tracker đồng
bộ trước khi dispatch các event lifecycle.

Yêu cầu (config.json): `slack_bot_token` (`chat.postMessage` + reactions) và
tuỳ chọn `slack_user_id` (allowlist + đích `Broadcast` chủ động của channel
sender `SlackSender`). `slack_signing_secret` của HTTP mode do proxy công khai
tiêu thụ, không phải trên thiết bị — codex, như bridge của hermes, tin đường
MQTT đã được xác thực.

### Discord (phiên gateway bot device-owned)

Discord cũng là **device-owned**: nó yêu cầu một phiên bot Gateway WebSocket
(không có API nhận kiểu long-poll), nên os-server chạy phiên đó qua
[discordgo](https://github.com/bwmarrin/discordgo)
(`runtimes/codex/discord.go`). Như loop telegram, phiên được khởi động từ
`StartWS` (`go s.startDiscordBot(ctx)`) và sống trong vòng đời của service
codex — nó chạy **chỉ khi codex là runtime đang active**, nên không bao giờ
tranh phiên bot với runtime khác trên cùng token. Loop đọc
`config.DiscordBotToken` **mới trên mỗi lần thử kết nối** (rỗng → kiểm tra
lại mỗi 30 s; open lỗi → back off 15 s); mở xong thì discordgo tự xử lý
reconnect gateway, và phiên được đóng khi ctx của gateway kết thúc. Intents:
direct messages + guild messages + message content.

Tin chỉ được chấp nhận nếu không phải từ bot (loop guard, bao cả tin của
chính bot), id người gửi bằng `config.DiscordUserID` (**allowlist là bắt
buộc** — rỗng thì từ chối tất cả, vì ai cũng có thể chung guild với bot), và
là **DM** hoặc tin trong `config.DiscordGuildID` có **@mention bot** (mirror
hành vi phổ biến của plugin openclaw: guild yêu cầu @mention, DM thì không —
bản thân mention bị strip khỏi văn bản turn). Mọi thứ khác bị bỏ qua ở mức
debug.

Mỗi tin được chấp nhận chờ agent rảnh (`IsBusy`, poll 500 ms, tôn trọng
ctx), rồi được inject qua `sendChat` với flow source `discord` và prefix
metadata người gửi
`[discord] Message from <Username> [id:<id>]:\n<text>`. Run được đánh dấu
**silent** (không TTS) và theo dõi trong `discordRuns` (runID → channel id);
trong lúc turn chạy, một goroutine `discordTypingKeeper` giữ **chỉ báo đang
nhập native** của Discord (`ChannelTyping` lập tức rồi mỗi 8 s — chỉ báo kéo
dài ~10 s — chặn trần 10 phút). Tại `turn.completed`, `emitFinal` consume
tracker và post reply — đã làm sạch bằng `stripForChannel`, như telegram — về
đúng channel qua `ChannelMessageSend`, **chia khúc theo giới hạn cứng 2000 ký
tự mỗi tin của Discord** (ưu tiên cắt ở ranh giới xuống dòng khi có thể); khi
`turn.failed`, tracker được consume mà không reply nên map không thể leak.
Bên gửi reply dùng handle phiên có mutex bảo vệ trên service (phiên nil → log
+ bỏ).

Yêu cầu (config.json): `discord_bot_token`, `discord_user_id` (allowlist), và
`discord_guild_id` khi cần mention trong guild hoạt động (setup chỉ dùng DM
có thể để trống).

### Channel API

`SupportedChannels()` trả `["telegram", "slack", "discord"]`. `AddChannel` /
`RefreshChannelConfig` cho cả ba đều là no-op success trung thực — mọi
consumer đọc credential mới từ config.json mỗi lần dùng (loop telegram mỗi
vòng lặp, bridge Slack mỗi event / lần gọi Web API, bot discord mỗi lần thử
kết nối / mỗi tin), nên không có gì phía agent để ghi và không cần restart.
`AddChannel(discord)` kiểm tra thêm rằng `discord_bot_token` và
`discord_user_id` phải có mặt (đường nhận không thể hoạt động nếu thiếu —
chấp nhận sẽ là fake success). Whatsapp không có đường nhận và trả
`domain.ErrChannelNotSupported`. Xem thêm
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md).

### Coding từ xa qua Telegram (`telegram_coding.go`, `coding_sessions.go`)

Mirror `runtimes/claudecode/telegram_coding.go` 1:1 — một chat Telegram có thể
**gắn vào thread `codex` interactive của một folder và code tiếp từ điện thoại**,
nhiều folder mỗi folder một thread. Tách biệt với lượt persona device-main.

- **Khám phá** (`coding_sessions.go`): codex lưu mỗi thread thành "rollout" JSONL
  ở `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
  (`/root/.codex/sessions`). `allCodingSessions` quét cây đó; **thread id
  (`payload.id`) và cwd (`payload.cwd`) lấy từ record `session_meta` đầu tiên**;
  danh sách hiện **3 prompt người dùng gần nhất** (mới nhất trước, bỏ khối
  `<environment_context>` tổng hợp). Dedupe theo thread id (rollout mới nhất thắng).
  Khác claude, codex resume **theo thread-id toàn cục**: `codex exec --cd <dir>
  resume <id>` đặt cwd độc lập, không cần khớp cwd gốc (đã verify device —
  resume thread cũ echo lại id; lỗi 404 trong test đó là do endpoint
  campaign-api `/responses` chưa có, KHÔNG phải cơ chế resume).
- **Lệnh** (chặn trong `handleTelegramUpdate` trước khi inject device-main):
  `/resume` (giống CLI codex — không tham số thì liệt kê folder, `/resume <n>`
  chọn theo số, `/resume <folder>` chọn mới nhất) · alias `/sessions` (liệt kê) +
  `/use <n|folder>` (chọn) · `/sessions <folder>` (mọi thread trong 1 folder) ·
  `/new <folder>` · `/here` · `/device` · `/help`. Chat chưa chọn và không phải
  lệnh thì rơi xuống device-main như cũ.
- **Mô hình hand-off.** Mỗi lượt spawn một `codex exec --json
  --dangerously-bypass-approvals-and-sandbox --cd <folder> [resume <thread>]
  <prompt>` (thứ tự cờ quan trọng — `--cd` bị từ chối sau `resume` nên phải đứng
  trước). Reply là các item `agent_message` gộp lại, parse từ JSONL
  (`parseCodexResult`: `thread.started`→id, `item.completed`/`agent_message`→
  text, `turn.completed`→xong), DM chunk ở 4000 ký tự. Env exec **mirror
  `turnEnv` của gatewayd** (env tiến trình + cặp `.env` presync + `HOME=/root` +
  `CODEX_HOME=/root/.codex`), nên codex dùng đúng `config.toml` + auth — chạy
  được **cả 2** auth mode (`OPENAI_API_KEY` qua `/responses`, hoặc `auth.json`
  subscription ChatGPT) mà không phải sửa runner.
- **Bảo vệ.** Mutex theo folder tuần tự hoá các lượt; lượt quét `/proc`
  (`procHoldsFolder`) từ chối chạy khi còn TUI `codex` interactive giữ folder
  (2 writer làm hỏng rollout). Chỉ `telegram_user_id` allowlist chạm được; coding
  chạy `--dangerously-bypass-approvals-and-sandbox` nên allowlist là ranh giới
  bảo mật.
- **Trạng thái.** Lựa chọn mỗi chat persist vào `/root/.codex/telegram_coding.json`
  (sống qua restart). Chạy thẳng trong os-server (mỗi lượt một `codex exec`
  riêng), độc lập với child thường trú của gatewayd.

## 6. Hooks

Codex không có hooks loader, nên hook `emotion-acknowledge` của OpenClaw được
tái hiện **native bằng Go** (`runtimes/codex/emotion_ack.go`, mirror
`emotion_ack.go` của hermes): mỗi turn user-visible, sendChat bắn
`{emotion:"thinking"}` sang HAL — cùng prefix skip, cùng intensity, cùng
capability gate (`skills.SupportedHooks`) như handler TS. Hook `turn-gate` đi
kèm cố ý không mirror (sendChat đã đánh dấu turn busy rồi). ⚠️ Giữ lockstep với
`runtimes/openclaw/hooks/emotion-acknowledge/handler.ts`.

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
