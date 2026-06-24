# Flow Monitor (tiếng Việt)

Tài liệu đầy đủ bằng tiếng Anh: [`docs/flow-monitor.md`](../flow-monitor.md).

## Tóm tắt

Flow Monitor là lớp quan sát end-to-end cho agent turn: ghi JSONL (`local/flow_events_YYYY-MM-DD.jsonl`), stream SSE tới UI. **Chỉ quan sát** — không đổi hành vi thiết bị hay business logic.

**Run ID từ thiết bị (`chat.send`):** idempotency dùng tiền tố `lamp-chat-*` (trước đây `lamp-sensing-*`). Đó là **mọi** tin gửi qua WebSocket từ thiết bị (sensing POST, wake greeting, …), **không** có nghĩa log đó chỉ là sound/voice — đừng nhầm với Telegram chỉ vì thấy chữ “sensing” trong log cũ.

**Map UUID → `lamp-chat-*`:** Hành vi runId của OpenClaw phụ thuộc version. **5.2** (và một số path 5.4 hiếm) generate UUID mới — thiết bị map UUID → idempotencyKey. **5.4** chủ yếu echo idempotencyKey trực tiếp làm runId — runId đã là device trace, không cần map. Một chat.send có thể tạo cả Phase 1 (echo) lẫn Phase 2 (UUID embedded run) trong burst/drain. SSE handler branch theo `payload.RunID` format: device-format → `RemovePendingChatTraceByRunID` (xoá entry match khỏi queue, không map); UUID → FIFO pop + map. Sau đó `resolveRunID` dùng cho agent stream **và** luồng `chat` để tránh cùng một turn bị hai `run_id` trên Monitor.

**Pending-trace orphan (regression 0.0.465, fix 0.0.468):** Bản trước skip pop khi runId device-format → entry kẹt lại làm orphan → UUID lifecycle kế tiếp pop nhầm → 2 reply khác nhau bị gắn cùng 1 turn (cascade off-by-one ~2 min cho tới khi TTL hết). Fix: dùng `RemovePendingChatTraceByRunID` để xoá entry chính xác thay vì skip.

**Sensing `enter` vs `chat_send`:** Handler gọi `NextChatRunID` + `flow.SetTrace` **trước** `flow.Start` để dòng `enter` trong JSONL cùng `trace_id` với `chat_send`. Trước đây `SetTrace` chỉ chạy sau khi gửi WS nên `enter` còn dính turn trước (turn “ma” / export Pair lệch).

**Log tương quan:** grep `flow correlation` — các `op`: `ws_chat_send`, `hal_agent_out`, `openclaw_uuid_map`, `chat_run_resolve`. Chi tiết bảng trong `docs/flow-monitor.md`.

**Field `type` trong `chat_send`:** event `chat_send` có field `type` = `"user"` (user thật / sensing-driven) hoặc `"system"` (skill watcher, wake greeting). Phân biệt chỉ ở flow event — WS RPC `chat.send` gửi sang OpenClaw giống hệt nhau. Auto-compact **không** sinh `chat_send`; nó gọi RPC `sessions.compact` trực tiếp qua `CompactSession`.

**Đo TTFT / warmup:** Khoảng `lifecycle_start → first thinking/assistant delta` = LLM warmup thực (model reasoning silently trước khi token đầu chảy ra). OS server tính từ marker JSONL `agent_first_token` / `thinking_first_token` (xem dưới) hoặc fallback sang live delta event trong RAM nếu có.

**Stream summary events (re-added 2026-05-19):** Raw `assistant_delta` / `thinking` deltas chỉ ở RAM (monitorBus), KHÔNG ghi JSONL — để tránh ~50–500 dòng/turn. Hậu quả: load lại Flow Monitor cho turn cũ → pipeline rect mất hẳn row streaming. Fix: backend emit 4 flow event nhẹ thay thế:

| Node | Khi nào fire | `data.*` |
|---|---|---|
| `agent_first_token` | Delta `assistant` đầu tiên | `{run_id}` (ts = TTFT moment) |
| `agent_last_token` | `lifecycle.end` drain accumulator | `{run_id, text, chunks, chars}` |
| `thinking_first_token` | Delta `thinking` đầu tiên (chỉ extended thinking) | `{run_id}` |
| `thinking_last_token` | `lifecycle.end` | `{run_id, text, chunks, chars}` |

Tối đa 4 dòng JSONL bonus / turn (thực tế 0–2). Stream name từ OpenClaw vẫn là `"assistant"` ở code level — chỉ JSONL node dùng prefix `agent_` cho khớp các node hiện có (`agent_thinking`, `agent_call`, `agent_response`). State live trong `OpenClawHandler.streamStats`, độc lập với `assistantBuf` (phục vụ TTS flush). Drain ở `lifecycle.end`. Trước đây có `llm_first_token` event đã bị bỏ vì "redundant với pipeline aggregator" — lý do đó sai, aggregator không observe được khi raw deltas không bao giờ tới JSONL.

**Badge `⏱` vs `⚡` trên Turn card:**
- **⏱ total** = `turn.startTime → turn.endTime` (input event → `lifecycle_end` / `tts_send` / `chat_final`) — toàn bộ window server-side. Đây là **server-observed turn duration**.
- **⚡ TTFT** = `turn.startTime → first thinking/assistant_delta` — khớp với timestamp agent bubble trên chat page (lúc user **thấy** reply bắt đầu). Đây là **perceived latency**.
- Khoảng cách ⚡ ↔ ⏱ = tail-streaming các token còn lại + lifecycle close. Reply ngắn → 2 con gần bằng nhau; reply dài → gap rõ rệt.
- Ngưỡng màu: ⏱ green ≤5s / amber ≤15s / red >15s. ⚡ green ≤3s / amber ≤8s / red >8s.
- ⚡ ẩn khi không có LLM stream (local intent match, dropped, queued).

**Khoảng `chat_send → lifecycle_start`** = OpenClaw init (network + load session/context + boot agent), KHÔNG phải LLM. Đo từ `chat_send` (OS server) tới `lifecycle_start` (OpenClaw event đầu tiên).

**Agentic Runtime section trên diagram (2026-05-08 redesign):** 3 node cũ (LLM Start / Thinking / Tool Exec) đã được gộp thành 1 **Event Pipeline rect** chạy giữa Agent Call và Response. Rect hiển thị danh sách events do OpenClaw emit, gộp các delta liên tiếp cùng loại thành 1 dòng tóm tắt (`thinking · 5.2s · 200 chunks · ~4k chars`). Edges ra HW (LED/servo/emotion/audio/lamp_gate) anchor từ cạnh phải pipeline. Aggregation rules + lý do redesign: `docs/debug/flow-monitor-pipeline.md`.

## Sơ đồ Turn Pipeline (SVG)

Component `FlowDiagram` trong `os/services/web/src/pages/Monitor.tsx` vẽ **ba vùng** (màu viền nền):

| Vùng | Màu | Node |
|------|-----|------|
| **OS Server** | Teal | Intent, Local, Cron, Gate |
| **HAL** | Amber | MIC, CAM, EMO, LED, SERVO, TTS |
| **Agentic Runtime** | Blue | Agent, TG In, Tool, Think, Response, TG Out |

### OS Server (hàng trên)

- **Cron** là stage **OS server** (lịch/timer thuộc OS server), **không** nằm trong cụm Agentic Runtime. Trên SVG, Cron cùng hàng với Intent/Local nhưng **`x` trùng cột Agent** để cạnh Cron→Agent là **đường dọc**.

### HAL

- **MIC** và **CAM** là input nodes (hàng trên HAL).
- Output nodes xếp dọc trong 1 cột:
  - **EMO** (`hw_emotion`) — `/emotion` (phối hợp LED + servo + display eyes)
  - **LED** (`hw_led`) — `/led/solid`, `/led/effect`, `/scene`, `/led/off`
  - **SERVO** (`hw_servo`) — `/servo/aim`, `/servo/play`
  - **TTS** (`tts_speak`) — `/voice/speak`, text-to-speech
- Đây là hardware calls trực tiếp từ OpenClaw tools, không qua OS server.
- Đường nối từ LOCAL → output nodes dùng **elbow routing** (gấp khúc bên trái) để tránh cắt qua node trung gian.

### Gate

- **Gate** nằm giữa OpenClaw output và HAL TTS. OS server listen WS events để phối hợp:
  - Tool có `/audio/play` → suppress TTS (không speak chồng nhạc)
  - Tool có `/led/*` → pause ambient breathing (không ghi đè màu agent set)
  - Assistant text accumulate → flush sang TTS khi lifecycle_end

### Agentic Runtime (lưới 3 cột)

- **Cột 1:** Tool + Response (Response dưới Tool).
- **Cột 2:** Agent + Thinking (Think dưới Agent).
- **Cột 3:** Telegram In.
- **Hàng 1:** Agent và TG In cùng hàng.
- **Hàng 2:** Thinking và Tool cùng hàng (Think → Tool).
- **Hàng 3:** Response dưới cột 1.

Bảng tọa độ gần đúng và ASCII grid: xem mục *Turn Pipeline* và *Approximate coordinates* trong `docs/flow-monitor.md`.

## File liên quan

| File | Vai trò |
|------|---------|
| `os/services/lib/flow/flow.go` | Emit flow, JSONL, API runID từng event |
| `os/services/server/sensing/delivery/http/handler.go` | Sensing → flow.Start/End |
| `os/services/server/openclaw/delivery/sse/handler.go` | Agent → flow.Log, map runID |
| `os/services/internal/openclaw/service.go` | sendChat / idempotencyKey |
| `os/services/web/src/pages/Monitor.tsx` | `groupIntoTurns`, `FlowDiagram`, v.v. |

**Tải để so sánh:** nút **↓ Bundle** trên Flow Panel tải cùng lúc JSONL tail server, snapshot UI và OpenClaw debug payload (xem bảng *Turns list vs downloaded log* trong `docs/flow-monitor.md`).

### Lấy tin nhắn user từ Telegram

OpenClaw chat stream **không bao giờ broadcast `role:"user"`** — chỉ emit `role:"assistant"`. Để lấy nội dung tin nhắn + tên người gửi, OS server gọi `chat.history` **WebSocket RPC** trên cùng WS connection đang dùng nhận events:

```
→  {"type":"req","id":"history-1","method":"chat.history",
    "params":{"sessionKey":"agent:main:telegram:group:...","limit":20}}

←  {"type":"res","id":"history-1","ok":true,
    "payload":{"messages":[
      {"role":"user","content":[{"type":"text","text":"dừng phát nhạc đi"}],
       "senderLabel":"Leo (158406741)"},
      ...
    ]}}
```

Chi tiết:
- **Async goroutine**: Fetch chạy trong goroutine riêng (gọi đồng bộ trong read loop sẽ deadlock).
- **Pending RPC tracking**: `pendingRPC` map match response về đúng caller qua request ID.
- **Hai phase emit**: `chat_input` đầu tiên fire ngay với placeholder trung tính `[chat]` (chưa có text). Goroutine lấy xong → fire `chat_input` thứ 2 với message + label chọn theo `senderLabel` / prefix message → UI pick event có content.
- **Frontend type upgrade**: emit đầu tiên pin `turn.type = "chat"` (từ summary `[chat]`). Khi emit thứ 2 tới, `groupIntoTurns` chạy lại `isTurnStart` để derive type cụ thể từ message prefix (`emotion.detected` / `speech_emotion.detected` / `voice` / `telegram` / …) và upgrade `turn.type` — **chỉ** khi đang còn ở placeholder `"chat"` (hoặc `"unknown"`), không đè type đã specific. Trước fix này, type bị kẹt ở `"chat"` (label CHAT, icon ❓) vì `refineTurnTypeFromSensingInputs` không nhận `"chat"` là channel type. Prefix `[speech_emotion]` map về `speech_emotion.detected` và được gom vào source `mic` (voice-driven), không phải `cam`, dù label có chữ "emotion".
- **Label routing (emit thứ 2)**: (1) `senderLabel` có → `[telegram:Gray]` (real channel user). (2) `senderLabel` rỗng + message khớp prefix device-internal → `[voice]` / `[emotion]` / `[speech_emotion]` / `[activity]` / `[wellbeing]` / `[music]` / `[sensing]` / `[system]` (sensing/voice event thiết bị đã post qua chat.send, OpenClaw merge vào UUID host turn này qua steer mode). (3) Còn lại → generic `[chat]`. Trước đây mọi UUID channel-turn đều bị gán nhãn theo configured channel (`[telegram]`), nhận nhầm steer-merged self-fire là Telegram.
- **Best-effort**: timeout 3 giây, fail thì giữ nguyên placeholder generic `[chat]` — tốt hơn là gán nhầm vào channel cụ thể.
- **Heartbeat**: Cron 30 phút cũng trigger `lifecycle_start` — last user message sẽ là system prompt, không phải user thật.
- **Token usage**: `chat.history` cũng được gọi lúc `lifecycle_end` để lấy token usage. OpenClaw `lifecycle_end` không có field `usage`. Token nằm trong last `role:"assistant"` message của history response: `usage: {input, output, totalTokens, cacheRead, cacheWrite}`. Emit thành `token_usage` flow event với `source: "chat_history"`.

### NO_REPLY suppression

OpenClaw agent trả `NO_REPLY` (hoặc dạng cắt ngắn `NO`, `NO_RE`, `NO_...`) khi quyết định không cần trả lời — thường cho passive sensing events (sound, motion). `isAgentNoReply()` trong `handler.go` suppress: không phát TTS, không hiện output. Match: `"NO"` chính xác, hoặc bắt đầu bằng `"NO_"` / `"NO_RE"` (case-insensitive).

### Output text & first-sentence streaming

Web (chat + flow Output) đọc text reply từ event `tts_send`, ưu tiên `data.full_text` (toàn bộ reply) rồi fallback `data.text`. Khi câu đầu được stream sớm tới TTS giữa turn (event `tts_stream_send`, gửi trước để giảm latency), `data.text` chỉ chứa **phần còn lại** (câu 1 đã bị cắt để không phát 2 lần), còn `data.full_text` mới giữ câu 1 + phần còn lại. Web không đọc `tts_stream_send`, nên thiếu `full_text` thì câu 1 sẽ không hiện. `data.streamed_len` là byte offset nơi phần còn lại bắt đầu.

### TTS suppress event

Khi `SendToHalTTS` thật sự bị skip (loa không phát), OS server emit `tts_suppressed` thay vì `tts_send`. Field `data.reason` discriminate: `channel_run` (real Telegram user turn — detect qua runID có prefix `tg-` OS server tự sinh trong `session.message` handler, hoặc `channelRuns` map mark từ chat.history fallback; reply đi qua OpenClaw session fan-out thay vì loa thiết bị), `music_playing` (audio đang chiếm loa), `already_spoken` (built-in tts tool đã route trước), `web_chat` (Flow Monitor chat — reply chỉ hiện trên web UI). UI hiển thị 🔇 ở Gate column thay vì 🔊 — tránh case trước đây log nói "TTS" nhưng loa im. Classifier chỉ dùng positive evidence: UUID runs từ OpenClaw steer-mode self-fire, cron fire, heartbeat KHÔNG bị coi là `channel_run` và VẪN phát loa.

### Cron-fire auto-force TTS

Khi OpenClaw emit `event:"cron"` với `action:"started"` (xem `src/cron/service/state.ts` của OpenClaw), OS server cache `sessionKey` → mark `lifecycle_start` kế tiếp trên session đó (trong vòng 10 s) là cron fire → `isChannelRun` bị override thành `false` để loa thiết bị tự nói mà không cần marker `[HW:/speak]`. Marker vẫn giữ trong skill làm defense-in-depth fallback nếu cron event bị drop (`dropIfSlow: true` ở phía OpenClaw).

### Pose bucket trên Turn card

Với `motion.activity` mà window pose vừa fire, turn card hiển thị:

```
IN   <input text>
[snapshot strip — tối đa 3 thumbnail: 1 motion + 2 worst pose]
[🪑 LOAD MORE · pose bucket <id> · N worst]
OUT  🔊 <output text>
```

- Strip được extract từ marker `[snapshot:]` + `[pose_bucket:]` / `[pose_worst:]` trong `sensing_input`. Click thumbnail mở lightbox inline (giống cũ).
- Nút **LOAD MORE** mở `PoseBucketModal` → fetch `/api/hardware/sensing/pose-bucket/<id>` (proxy về lelamp) → render bảng từng sample (monospace + cột joint giống Sensing tab). Row có filename trong `worst_snapshots` được highlight (viền đỏ + ⭐) để xem nhanh khung tệ nhất.
- Khi /dm fire, OS server tự đính các worst snapshot vào Telegram qua `sendMediaGroup` — caption nằm trên ảnh đầu tiên, agent không cần biết file path. Xem `devices/lamp/docs/sensing-behavior.md` mục "/dm auto-attach".

### Clip audio debug trên Turn card

Với `speech_emotion.detected`, turn card hiển thị một player `<audio controls>` click-to-play gắn nhãn `🎙 debug` cho mỗi audio URL, để nghe chính xác clip đã tạo ra emotion được detect.

- Đường dẫn clip trên Pi tới qua field `audio` (tùy chọn) trong body `POST /api/sensing/event`. `os/services/server/sensing/delivery/http/handler.go` chuyển basename của path thành URL servable (`audioURLForPath` → `/api/sensing/audio/<file>.wav`) và lưu vào `Detail` của monitor event ở key `audio` — **chỉ URL basename, không bao giờ là raw path**.
- Frontend `turnIO()` (`helpers.ts`) rút các URL này vào `audioUrls` từ `detail.audio` của event `sensing_input`; `TurnBadge.tsx` render player.
- **Đây là affordance CHỈ-ĐỂ-DEBUG — audio KHÔNG BAO GIỜ gửi cho LLM.** Path nằm trong field JSON riêng, không nằm trong text tin nhắn chat, nên tự nhiên bị loại khỏi những gì agent thấy — giống cách snapshot `motion.activity` được hiện trên UI nhưng strip trước khi tới LLM.
- **Route**: `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`) serve file `.wav` theo basename từ `/var/lib/hal/speech-emotion` hoặc `/tmp/hal-speech-emotion`, với validation basename nghiêm ngặt — tên phải kết thúc `.wav` và không chứa `/`, `\`, hay `..` (nếu không → `404`).

### Tool call display

- Chỉ hiện tool events phase `"start"` (có args). Phase `update`/`result` không có args nên bỏ qua.
- Hiện full curl command từ `args.command` (OpenClaw agent tự generate).
- Mỗi tool entry có nút 📋 copy riêng — click copy curl command.
- OpenClaw gửi tool name ở `data.name` (không phải `data.tool`), args là object `data.args` (e.g. `{"command":"curl ..."}`).

Chi tiết run ID, `runIDMap`, stitching turn, edge case: đọc bản tiếng Anh.

## Compaction summary inspector

Session agent auto-compact khi context vượt ~80k tokens. Mỗi lần compact ghi 1 record `type:"compaction"` vào `/root/.openclaw/agents/main/sessions/<sessionId>.jsonl`, chứa field `summary` dạng text — text này được **chèn đầu mỗi turn kế tiếp** cho đến lần compact sau. Rule bị copy/generalize nhầm vào summary có thể đè SKILL.md (summary nằm trước trong prompt, đóng vai trò "context đã chốt").

**UI:** header Flow Monitor có nút `📋 Summary`. Click → fetch + render modal show: `timestamp`, `tokensBefore`, `summaryChars`, `compactionCount`, `readFiles` (file nào được đọc vào compaction prompt), và toàn văn `summary`.

**Endpoint:** `GET /api/openclaw/compaction-latest?session=<key>` (mặc định `agent:main:main`). Response format: `{status:1, data:{found, sessionFile, timestamp, tokensBefore, summary, details:{readFiles}, ...}}`.

Dùng khi agent viện rule mà grep không thấy trong bất kỳ `skills/**/SKILL.md` — gần như 100% nguồn là compaction summary, không phải skill đang load. Handler: `os/services/server/openclaw/delivery/sse/handler_api_compaction.go`.

## Issue đang mở

### OpenClaw built-in `tts` tool bypass speaker HAL (ĐÃ FIX)
Agent gọi `tts` built-in tool của OpenClaw thay vì trả assistant text. OpenClaw generate audio phía server (`"Generated audio reply."`) nhưng không route tới speaker HAL (`/voice/speak`). Agent trả `NO_REPLY` → OS server không có text → im lặng.
- **Nguyên nhân**: OpenClaw cung cấp `tts` tool khi `tools.profile = "full"`. Sensing SKILL.md hướng dẫn gọi `/voice/speak`, agent map nhầm sang built-in `tts` tool thay vì `curl` tới HAL.
- **Fix**: (1) Deny `tts` tool qua `tools.deny: ["tts"]` trong config (`service.go`). `tools.disabled` KHÔNG hợp lệ — dùng `tools.deny` (deny thắng `tools.profile`). (2) Intercept fallback trong handler.go: nếu agent vẫn gọi `tts` tool, extract text và route sang `SendToHalTTS()`. (3) Cập nhật sensing SKILL.md và SOUL.md — agent trả text bình thường, OS server pipeline tự TTS qua HAL.
- **Trạng thái**: Đã fix v0.0.138.

### OpenClaw không thấy `tool_call` dù có action
Đã gặp nhiều turn (nhất là Telegram): user yêu cầu action (ví dụ đổi màu đèn), kết quả OUT/TTS xác nhận đã đổi, nhưng flow/debug không có `tool_call`.

- **Ảnh hưởng**: node `TOOL` có thể không sáng dù nhìn như đã có action.
- **Trạng thái hiện tại**: đã bật raw dump full-stream (`source: "openclaw_raw"`), nhưng vẫn có run không thấy payload `stream:"tool"`.
- **Chưa chốt**: có thể OpenClaw chạy nhánh nội bộ không emit tool stream, hoặc action chỉ được suy ra từ assistant text mà không có tool invocation tường minh.
