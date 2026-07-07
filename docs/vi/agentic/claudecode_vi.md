# Backend agent Claude Code

Cách `os-server` điều khiển **Claude Code** (agent CLI của Anthropic) làm
runtime agentic có thể hoán đổi của thiết bị, bên cạnh OpenClaw, Hermes và
PicoClaw. Cơ chế generic (flow switch, install-vs-presync, migration, checklist)
nằm ở [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md); file này là
protocol, layout và các quirk đặc thù claudecode.

> **Trạng thái:** đạt parity đầy đủ với checklist — install nhúng + presync,
> transport bridge WebSocket, adapter migrate persona/memory bằng Go (lossless
> với layout OpenClaw), skills (restore từ CDN + watcher, `.claude/skills`
> native), watch/rename identity, MCP thật (`.mcp.json`), factory reset,
> Telegram + Discord qua **channel plugin native** của Claude Code
> ([code.claude.com/docs/en/channels](https://code.claude.com/docs/en/channels)),
> **Slack inbound do device sở hữu** (HTTP mode, `domain.SlackBridge`),
> và flow **claude.ai OAuth login** (§7b) thay thế cho API key trong
> config.json. Các caveat đã biết được đánh dấu ⚠️ ở §11.

Code: `os/services/internal/claudecode/`.

| Thành phần | Vị trí trên thiết bị |
|------|-----------------|
| Claude Code CLI | `/usr/local/bin/claude` (symlink → `/root/.local/bin/claude`) |
| Bridge (systemd `claudecode.service`) | subcommand `os-server claudecode-gatewayd` (biên dịch sẵn trong `/usr/local/bin/os-server`; code `os/services/internal/claudecode/gatewayd/`) |
| Env khởi chạy (`ANTHROPIC_*`, cờ channel) | `/root/.claudecode/.env` (presync sở hữu) |
| Workspace (cwd của Claude) | `/root/.claudecode/workspace/` |
| Persona / memory | `workspace/{CLAUDE,SOUL,IDENTITY,USER,MEMORY,KNOWLEDGE}.md`, `workspace/memory/*.md` |
| Skills | `workspace/.claude/skills/<name>/` (skill Claude Code native) |
| MCP connector | `workspace/.mcp.json` |
| State resume session | `/root/.claudecode/session.json` |
| Config kênh (telegram / discord) | `/root/.claude/channels/<ch>/{.env,access.json}` |
| Credentials claude.ai OAuth (flow login) | `config.json` `claude_code_oauth_token` + `/root/.claude/.credentials.json` |
| Transcript hội thoại | `/root/.claude/projects/` (nội bộ Claude) |

---

## 1. Chọn + cài đặt

`config.agent_runtime: "claudecode"` (hoặc `gateway.default` trong DEVICE.md)
resolve backend trong `internal/agent/factory.go`. Switch vào/ra đi qua flow
`switch-runtime` generic — không có gì đặc thù claudecode trong switcher.

**`install.sh`** (nhúng, chạy một lần ở lần switch đầu / khi `verify` thất bại):

1. tiền đề: `jq curl`;
2. Claude Code CLI qua installer native chính thức
   (`curl -fsSL https://claude.ai/install.sh | bash` → `~/.local/bin/claude`,
   binary standalone, linux arm64/amd64, không cần Node.js), symlink sang
   `/usr/local/bin/claude`;
3. **bun** + **channel plugin telegram + discord** (best-effort):
   `claude plugin marketplace add anthropics/claude-plugins-official` +
   `claude plugin install {telegram,discord}@claude-plugins-official`. Channel
   plugin là script bun; lỗi ở bước này chỉ vô hiệu các receive loop của
   channel (⚠️ §11);
4. chạy hook presync một lần (bridge + env + sync channel);
5. ghi + start **`claudecode.service`** (tên unit == tên runtime — không cần
   file khai báo service). Unit chạy `os-server claudecode-gatewayd`
   (kèm `EnvironmentFile=-/root/.claudecode/.env`). Thân unit bị duplicate
   trong `gateway_unit.go` (self-heal của `EnsureOnboarding`) — **giữ hai bản
   đồng bộ**;
6. hook verify `/usr/local/lib/os-runtimes/claudecode/verify` =
   `command -v claude` + `/usr/local/bin/os-server` executable (gatewayd nằm
   trong đó; presync tự lành mọi thứ còn lại).

**`presync.sh`** (nhúng; materialize thành
`/usr/local/bin/runtime-claudecode-presync` mỗi lần switch, được switch-runtime
chạy trước khi start, install.sh chạy một lần, và `EnsureOnboarding` chạy trên
**mỗi lần os-server boot / config đổi** — pattern của hermes, nên thiết bị boot
thẳng vào claudecode hoặc sửa `llm_*`/telegram khi đang active sẽ tự lành mà
không cần switch):

- bản thân bridge **không còn được materialize ở đây** — nó nằm trong binary
  os-server dưới dạng subcommand `claudecode-gatewayd`, nên một OTA os-server
  thường sẽ cập nhật nó;
- **§1 SEEDS** — `~/.claude.json` nhận `hasCompletedOnboarding` +
  `bypassPermissionsModeAccepted` (không có TTY để trả lời prompt tương tác);
  `workspace/.claude/settings.json` nhận `enableAllProjectMcpServers: true`
  (tin các entry `.mcp.json` do os-server ghi);
- **§2 ENV** — `/root/.claudecode/.env`, theo một trong hai **auth mode** (§7b):
    - *subscription* (`claude_code_oauth_token` đã set trong config.json, hoặc
      `~/.claude/.credentials.json` có trên đĩa): inject `CLAUDE_CODE_OAUTH_TOKEN`
      và **bỏ toàn bộ biến `ANTHROPIC_*`** — biến API-key đứng trên OAuth trong
      thứ tự ưu tiên credential của Claude Code, nên để chúng lại sẽ âm thầm giữ
      thiết bị trên đường API-key;
    - *api-key* (mặc định): `ANTHROPIC_BASE_URL` ← `llm_base_url` (mặc định
      `https://campaign-api.autonomous.ai/api/v1/ai`, **không có `/v1` ở cuối** —
      Claude gọi `{base}/v1/messages`, cùng endpoint anthropic-messages mà hermes
      dùng), `ANTHROPIC_API_KEY` **và** `ANTHROPIC_AUTH_TOKEN` ← `llm_api_key`
      (phủ cả hai convention x-api-key và Bearer), `ANTHROPIC_MODEL` /
      `ANTHROPIC_SMALL_FAST_MODEL` ← `llm_model` (mặc định `Auto-AI`).

  Cả hai mode đều thêm `DISABLE_AUTOUPDATER=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, và các cờ khởi chạy
  `CLAUDECODE_CHANNELS`;
- **§3 CHANNELS** — xem §7.

## 2. Hằng số wire (`constants.go`)

| Hằng số | Giá trị | Ý nghĩa |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18791/claude/ws/` | endpoint WebSocket của bridge |
| `Token` | `autonomous_claudecode_token` | bearer token khi connect; gatewayd mặc định dùng đúng hằng số này (biên dịch trong os-server) — hai bên PHẢI khớp |
| `Conversation` | `device-main` | chỉ là nhãn; Claude sở hữu session id thật |

## 3. Bridge (`os-server claudecode-gatewayd`)

Claude Code không có server mode, nên systemd unit chạy một gatewayd Go nhỏ
(`internal/claudecode/gatewayd/`, cấu trúc mirror gatewayd của codex — không
còn phụ thuộc python3/websockets), gatewayd này:

- giữ **một process Claude headless bền**:
  `claude --print --verbose --input-format stream-json --output-format
  stream-json --dangerously-skip-permissions`, cwd = workspace, env từ `.env`;
  `--resume <session_id>` khi respawn (session liên tục qua các lần bridge
  restart, state trong `session.json`); `--channels <CLAUDECODE_CHANNELS>` khi
  presync đã cấu hình một channel plugin;
- serve WebSocket (gorilla/websocket), gate bằng bearer token (close code
  `4401` khi token sai); mỗi lúc một client — kết nối mới thay thế kết nối cũ;
- forward **nguyên văn** các event stream-json trên stdout của Claude tới
  client đang kết nối, convert frame `message.send` thành message `user`
  stream-json trên stdin (attachment ảnh data-URL → content block `image`
  base64), trả lời `ping` bằng `pong`;
- restart child khi exit (backoff 5 s); nếu đang có turn in-flight thì phát
  `bridge.error` để os-server đóng run thay vì chờ hết busy TTL; `session.new`
  restart child **không kèm** `--resume`;
- queue các frame `message.send` đến trong lúc child đang down và flush khi
  respawn.

Path, port và token override được qua các biến môi trường `CLAUDECODE_*`
(`CLAUDECODE_WS_TOKEN`, `CLAUDECODE_PORT`, `CLAUDECODE_HOME`,
`CLAUDECODE_WORKSPACE`, `CLAUDECODE_ENV_FILE`, `CLAUDECODE_SESSION_FILE`,
`CLAUDECODE_BIN`, `CLAUDECODE_RESTART_BACKOFF_S`); giá trị mặc định khớp layout
thiết bị ở trên, nên các deployment `/root/.claudecode` hiện có chạy y nguyên.

## 4. Gửi một lượt (`chat.go`)

Shape giống hệt picoclaw: `sendChat` đánh dấu busy + cất pending runID **trước
khi** ghi frame, phát flow event `chat_input`/`chat_send`, và return ngay khi
frame được ghi — reply về trên read loop. Frame outbound:

```json
{"type":"message.send","id":"chat-42","payload":{
  "content":"...","attachments":[{"type":"image","url":"data:image/jpeg;base64,..."}]}}
```

Claude tự serialize các input đang queue, nên mỗi lúc chỉ một turn in-flight và
tương quan pending/current runID đơn lẻ vẫn đúng.

## 5. Event inbound → `domain.WSEvent` (`translator.go`)

Event stream-json của Claude được dịch thành đúng các frame mà handler OpenClaw
tiêu thụ:

| Event Claude | Phát ra |
|---|---|
| `system` (subtype `init`) | — (bắt `session_id`) |
| `assistant` — block `text` | — (cất làm final text dự phòng) |
| `assistant` — block `tool_use` | `lifecycle.start` (một lần) + `tool.start` |
| `user` — block `tool_result` | `tool.end` (text kết quả, match theo `tool_use_id`) |
| `result` subtype `success` | `chat.final` + `lifecycle.end` (+ `usage` theo lượt) |
| `result` subtype `error*` / `is_error` | `lifecycle.error` |
| `bridge.error` | `lifecycle.error` |
| `stream_event` / `pong` / `bridge.status` | bỏ qua |

Các gotcha vòng đời turn:

- **Text cuối cùng là `result.result`**, không phải các block text của
  assistant — text assistant trung gian (giữa các tool call) không bao giờ được
  render, chỉ được cất làm fallback phòng thủ.
- `usage` là **theo từng lượt** (shape API Anthropic: `input_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`) —
  khác `context_usage` cộng dồn của picoclaw.
- **Turn khởi phát từ channel nổi lên trên cùng stdout**: một message Telegram
  được plugin xử lý bên trong session Claude sinh ra các event assistant/result
  không có pending runID — `ensureTurnStarted` cấp một runID mới, nên các turn
  đó vẫn hiện trong Flow Monitor (không cần hook observer, khác hermes).

## 6. Session

Claude sở hữu session: id được bắt từ bất kỳ event nào mang `session_id` và
được bridge persist (`session.json`) cho `--resume`.
`NewSession` gửi `{"type":"session.new"}` (session mới, không resume).
`ShouldRotateSession` **luôn false** và `CompactSession` trả
`domain.ErrNotSupportedByRuntime` — Claude Code tự auto-compact context của
nó, nên một rotation do os-server điều khiển chỉ tổ vứt context đi.

## 7. Kênh — Telegram + Discord qua channel plugin native, Slack do device sở hữu

`SupportedChannels() = [telegram, slack, discord]`. Khác hermes/picoclaw
(receive loop do device sở hữu), loop telegram/discord ở đây là **channel plugin của chính Claude Code**:
bridge khởi chạy `claude --channels
plugin:telegram@claude-plugins-official plugin:discord@claude-plugins-official`
(chỉ những kênh đã cấu hình), mỗi plugin poll Bot API của nó và trả lời qua
chính chat đó, hoàn toàn bên trong session Claude.

- presync ghi `~/.claude/channels/<ch>/.env` (`TELEGRAM_BOT_TOKEN` ←
  `telegram_bot_token`, `DISCORD_BOT_TOKEN` ← `discord_bot_token`) và seed
  `access.json` với `{"dmPolicy":"allowlist","allowFrom":["<owner user id>"]}`
  (`telegram_user_id` / `discord_user_id` — một *snowflake* Discord) — thay cho
  các flow tương tác `/telegram:access pair` / `/discord:access pair`, thứ một
  thiết bị headless không chạy được. Hai plugin dùng chung schema access.json.
  **Owner user id là bắt buộc** cho message chiều vào; chỉ có token thì plugin
  ở lại pairing mode và drop người lạ.
- `AddChannel`/`RefreshChannelConfig` (telegram/discord) → chạy lại presync +
  restart theo hash-diff (`syncChannels` → `EnsureOnboarding`, pattern của
  hermes). Với slack cả hai là no-op success trung thực: bridge do device sở
  hữu đọc `slack_user_id` theo từng event và `slack_bot_token` theo từng call
  Web API — luôn tươi từ config.json — nên chỉ cần persist creds là đủ (không
  presync, không restart bridge). whatsapp → `domain.ErrChannelNotSupported`.
- **Slack do DEVICE SỞ HỮU** (`slack.go` + `slack_sender.go`, mirror của
  `internal/codex/slack.go`): Claude Code không có slack channel plugin
  ("Claude in Slack" là một tính năng cloud riêng, spawn web session từ mention
  `@Claude`, không phải một kênh của thiết bị). Thay vào đó proxy public
  bff-campaign-service nhận delivery từ Slack Events API và fan-out qua MQTT
  tới handler `slack_event` của thiết bị
  (`server/device/delivery/mqtt/slack_event_handler.go`), handler dedup theo
  event_id và type-assert gateway đang active thành `domain.SlackBridge` —
  `ClaudeCodeService` implement interface này, nên event chỉ route vào đây khi
  claudecode đang active. Message được chấp nhận (gate allowlist
  `slack_user_id`; event bot/subtype/user rỗng bị drop) chờ agent rảnh (poll
  500 ms, cap 2 phút) rồi được inject thành một lượt thường với flow source
  `slack`; run được track trong `slackRuns` và **đánh dấu silent** (không TTS),
  đồng thời thêm reaction ack `eyes` lên message của user. Câu trả lời cuối
  (event `result` → `emitFinal`, `translator.go`) được post ngược về đúng
  channel/thread gốc qua `chat.postMessage` (`slack_bot_token`), marker
  `[HW:/...]` + audio tag được strip (`stripForChannel`, `hal.go`); reaction
  ack được xoá khi reply hoặc lỗi. **Không streaming từng phần**
  (`StreamSlackDelta` là no-op — câu trả lời về nguyên khối). Reply thread theo
  thread sẵn có, không có thì thread dưới message của user.
- Outbound `Broadcast`/`SendToUser` (nudge chủ động) đi thẳng tới Telegram Bot
  API (`telegram_sender.go`); `SlackSender` post message chủ động tới kênh
  `slack_user_id` đã cấu hình khi đủ cả hai creds slack. Target store dùng chung
  (`/root/.lumi/telegram_targets.json`) không được plugin populate, nên
  `GetTelegramTargets` fallback về owner id đã cấu hình (`telegram.go`).

## 7b. Auth — claude.ai OAuth login (thay thế cho API key)

Thiết bị có thể xác thực bằng **Claude subscription của chính user** thay vì
`llm_api_key`. Flow này (`internal/claudecode/login.go`, interface tùy chọn
`domain.ClaudeLoginPairer`) mô phỏng flow pairing WhatsApp — stream các
`PairingEvent` — với thêm một chặng: OAuth code đi ngược trở lại flow.

1. `{"cmd":"claudecode_login"}` trên fa_channel khởi động flow: os-server chạy
   `claude setup-token` dưới một pty (`script -qec` — CLI từ chối chạy khi
   không có TTY) và quét output của nó.
2. `{"status":"pairing_url","login_url":"https://claude.ai/oauth/…"}` được
   stream trên fd_channel; user mở URL, authorize, và copy code.
3. `{"cmd":"claudecode_login_code","code":"…"}` đưa code cho CLI đang chờ
   (được ghi vào pty với một `\r` raw-mode).
4. Khi thành công, token dài hạn (`sk-ant-oat01-…`) được persist vào
   config.json (`claude_code_oauth_token`), `EnsureOnboarding` chạy lại presync
   (`.env` ở subscription-mode — xem §1), và bridge restart vào subscription
   auth. Fallback: nếu dòng token không được bắt nhưng CLI đã lưu
   `~/.claude/.credentials.json`, cái đó cũng tính là thành công — presync
   chấp nhận một trong hai tín hiệu.

Runtime không phải claudecode trả lời `claudecode_login` bằng một failure
one-shot ("claude login not supported on … backend"). Chi tiết hợp đồng MQTT:
`docs/vi/mqtt_vi.md` §`claudecode_login`. ⚠️ Thứ tự ưu tiên credential là cái bẫy ở
đây: `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` **đứng trên** OAuth token, đó
là lý do presync ở subscription-mode bỏ hẳn chúng.

## 8. Workspace, persona, skills, MCP

- **`CLAUDE.md`** là file memory Claude Code tự nạp; onboarding sở hữu một khối
  OS-managed (giới hạn bằng marker, ghi chú của owner được giữ bên dưới) chứa
  các **@import** persona (`@SOUL.md @IDENTITY.md @USER.md @MEMORY.md
  @KNOWLEDGE.md` — CLAUDE.md là file duy nhất Claude nạp theo tên) và prompt
  discipline (rule whitelist skills, rule memory, ưu tiên user), phỏng theo
  khối AGENTS.md của picoclaw.
- **Khối SOUL**: cùng cơ chế inject device-soul theo `soul_ref` như
  openclaw/picoclaw.
- **Migrate persona** (`migrate_persona/runtime_claudecode.go`): layout **cố ý
  giống hệt OpenClaw** (IDENTITY.md có slot riêng, MEMORY.md ở gốc workspace,
  KNOWLEDGE.md + `memory/*.md` hằng ngày), nên mọi slot map 1:1 và round-trip
  với các runtime có slot là lossless về cấu trúc. Bản thân `CLAUDE.md` là
  đặc thù runtime và không bao giờ được mang theo.
- **Không có HEARTBEAT.md** — Claude Code không có heartbeat loop nào sẽ đọc
  nó; một quyết định bỏ có ý thức, không phải bỏ sót.
- **Skills** nằm trong `workspace/.claude/skills/` (native, tự-discover). Được
  capability-prune lúc onboarding; **restore từ CDN khi rỗng**
  (`ensureSkills` — bao case factory reset); cập nhật steady-state qua
  `skill_watcher.go` (poll metadata OTA 5 phút, notify qua
  `SendSystemChatMessage`).
- **MCP là thật** (`mcp.go`): `WriteMCPEntry`/`RemoveMCPEntry` upsert
  `mcpServers` trong `workspace/.mcp.json` (entry canonical `{command,args,env}` /
  `{type,url,headers}` pass through nguyên văn) + restart bridge;
  `MCPReconcile` clone connector khi switch runtime qua
  `claudecode.ReadMCPEntries`.
- **Migrate config LLM** (`migrate_config/runtime_claudecode.go`): đọc/ghi
  `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` trong `/root/.claudecode/.env`.

## 9. Gì là thật vs no-op (`stubs.go`)

Thật (ngoài transport): `SetupAgent`/`EnsureOnboarding` (chạy presync + các
khối workspace + restore skills + self-heal unit, restart theo hash-diff),
`ResetAgent`, `RestartAgent`, `WatchIdentity`/`UpdateIdentityName`,
`StartSkillWatcher`, `WriteMCPEntry`/`RemoveMCPEntry`, `NewSession`,
`GetConfigJSON` (settings.json của workspace — không bao giờ là `.env` chứa
secret), `Version` (probe `claude --version`), và surface
`domain.ClaudeLoginPairer` (`StartClaudeLogin`/`SubmitClaudeLoginCode` — §7b).

No-op kèm lý do: `HasWhatsappSession`/`PairWhatsapp` (Baileys chỉ có ở
OpenClaw), `FetchChatHistory` (`TODO(claudecode-history)` — JSONL session
không có API đọc ổn định), `StartModelSync`/`StartPrimaryModelWatch` (model
cố định qua env). `RefreshModelsConfig`/`UpdatePrimaryModel` (model =
`ANTHROPIC_MODEL`, do presync sở hữu) và `CompactSession` (auto-compaction,
§6) trả `domain.ErrNotSupportedByRuntime` để caller fallback sang
`EnsureOnboarding` — presync của nó áp dụng thay đổi model;
`ShouldRotateSession=false`.

## 10. Factory reset (`reset.go`)

Stop + verify `claudecode.service`, disable nó, rồi wipe **dữ liệu user +
creds**: `workspace/`, `.env`, `session.json`, `~/.claude/projects`,
`~/.claude/channels`, `~/.claude/todos`, `~/.claude/history.jsonl`, và
`~/.claude/.credentials.json` (phần login claude.ai — nửa nằm trong
config.json, `claude_code_oauth_token`, bị wipe cùng config). **Giữ lại**
(phần mềm đã cài): claude CLI, bun, `~/.claude/plugins`, `~/.claude.json`
(bản thân bridge nằm trong binary os-server). Mọi thứ bị wipe đều có đường restore chạy sau reset
(presync/EnsureOnboarding dựng lại env + channels từ config.json được nhập
lại; `ensureSkills` tải lại skills; flow login chạy lại cho subscription auth).

## 11. ⚠️ Cần verify trên thiết bị

- **Channels đang là research preview** (Claude Code ≥ 2.1.80): flag/protocol
  `--channels` có thể đổi, và account do org quản lý cần `channelsEnabled`.
  Nếu một plugin đăng ký thất bại, mọi thứ trừ receive loop của kênh đó vẫn
  chạy.
- **Format output của `claude setup-token`**: scanner của login match URL
  OAuth, token `sk-ant-oat01-…`, và các marker thành công dạng text — verify
  với build CLI trên thiết bị (flow degrade thành
  `failure (last output: …)` kèm dòng cuối của CLI khi parse trượt). Các lần
  ghi pty dùng `\r` cho Enter; xác nhận prompt dán code chấp nhận nó.
- **`claude plugin` CLI có sẵn hay không**: install.sh coi việc cài
  marketplace/plugin là best-effort; verify build CLI trên thiết bị hỗ trợ cài
  plugin headless, hoặc cài plugin một lần bằng tay.
- **Tương thích campaign-api**: Claude Code dùng đầy đủ Anthropic Messages API
  (system prompt, vòng lặp tool_use, streaming) trên `ANTHROPIC_BASE_URL` —
  verify proxy pass được các thứ này qua (hermes đã dùng cùng endpoint ở mode
  `anthropic_messages`).
- **First-run headless**: presync seed các flag trong `~/.claude.json`; nếu CLI
  thêm gate tương tác mới, child của bridge có thể exit ngay khi start — check
  `journalctl -u claudecode` và các dòng stderr của `claude` trong log bridge.
