# Realtime Voice Agent (Trợ lý giọng nói thời gian thực)

Lớp giọng nói speech-to-speech độ trễ thấp, chạy **song song** với pipeline STT
→ agent thông thường. Model realtime xử lý hội thoại tán gẫu trực tiếp (trả lời
âm thanh dưới 1 giây) và **delegate** (chuyển giao) những gì cần đến agent chính
(điều khiển thiết bị, skills, memory, thông tin thời gian thực) về luồng
OS-server.

Code nằm ở `os/hal/drivers/realtime/`; được điều khiển bởi
`os/hal/drivers/voice/voice_service.py`.

> **Nguồn chân lý:** doc phản ánh code. Nếu lệch nhau, code đúng.

## Khái niệm: handle vs. delegate

Mỗi lượt nói được stream tới model realtime *cùng lúc* với pipeline STT. Cuối
lượt, model sẽ:

- **Handle** (tự xử lý) — tán gẫu / trả lời nhanh — nói lại qua TTS, không cần
  round-trip tới agent chính, hoặc
- **Delegate** bằng cách gọi tool `delegate_to_main` → dừng output realtime và
  chuyển một dòng tóm tắt yêu cầu tới OS server (→ OpenClaw / Hermes) để xử lý
  phần nặng.

Tool `delegate_to_main` được orchestrator đăng ký tự động (`orchestrator.py`,
`DELEGATE_TOOL`).

## Biểu cảm cảm xúc (fire-and-forget)

Nếu thiết bị khai báo capability `expression`
(`DEVICE.md` → `expression: { routes: [emotion] }`), orchestrator còn đăng ký
thêm tool `express_emotion` (`orchestrator.py`, `EMOTION_TOOL`). Thiết bị không
có "mặt" (vd: chỉ mic + loa) sẽ không có tool này, nên model realtime không thể
set cảm xúc — gating chạy xuyên suốt: `server.py`
(`"expression" in _profile.capabilities`) →
`VoiceService(enable_expression=…)` →
`RealtimeOrchestrator(enable_expression=…)`.

Khác với `delegate_to_main`, `express_emotion` là **fire-and-forget** và là
ngoại lệ duy nhất của quy tắc "tool HOẶC nói" — model gọi nó *song song* với
việc nói. Khi `stream_output()` thấy lời gọi (`_handle_emotion_call`), nó:

1. gọi handler emotion của HAL **in-process** (`_fire_emotion` →
   `routes/emotion.py` `express_emotion`) trong một daemon thread — realtime agent
   chạy ngay trong process HAL nên không cần loopback HTTP / serialize. Nó chạy
   song song với audio đang stream, nên mặt đổi mà không chặn giọng;
2. xác nhận lời gọi bằng `FunctionCallResultInput(trigger_response=False)`, tức
   ghi kết quả vào history **mà không** sinh response thứ hai. Với OpenAI điều
   này bỏ qua `response.create` (`openai_realtime.py`); với Gemini thì tool
   response chỉ để lượt tiếp tục. Độ trễ cộng thêm vào giọng nói ≈ 0.

Model được dặn (`resources/system_prompt*.md`, mục "Expression Exception") không
chờ, không thông báo, không đọc tên cảm xúc thành tiếng. Lưu ý điều này khác
path không-realtime: ở đó agent phát marker text `[HW:/emotion:…]` rồi lớp Go
parse và cắt bỏ — path realtime không bao giờ dùng marker text.

## Các provider

Hai backend thay thế cho nhau, chọn bằng `HAL_REALTIME_PROVIDER`
(`none` | `gemini` | `openai`):

| Provider | Class | Mô hình threading | Model mặc định | Sample rate |
|----------|-------|-------------------|----------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | event loop asyncio riêng trên thread `gemini-io`; thread send/recv submit coroutine qua `run_coroutine_threadsafe` | `gemini-3.1-flash-live-preview` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | thuần đồng bộ; 1 `RealtimeConnection` dùng chung bởi thread send/recv, serialize bằng reentrant lock | `gpt-realtime-2` | 24000 Hz |

Cả hai kế thừa `voice_agent/base.py` `VoiceAgentBase`, định nghĩa contract dựa
trên queue:

- **2 thread mỗi agent**: `_send_loop` rút `_send_queue` → API; `_recv_loop` đọc
  API → `_recv_queue`. Cả hai tự reconnect khi lỗi.
- **Non-blocking**: `append_audio()`, `commit_audio()`, `send()` (đẩy vào queue,
  gate trên `available`).
- **Blocking**: `connect()`, `disconnect()`, `receive()` (generator yield
  `OutputBase` đến khi gặp `TurnDoneEvent`, hoặc khi không có event nào trong
  `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` — mặc định 8 s — để kết thúc lượt im lặng
  và fallback sang main agent mà không bị dead-air dài).
- `available` ⇔ websocket/session đã connect (`_connected`).

### An toàn connection của OpenAI

Agent OpenAI dùng chung 1 `RealtimeConnection` giữa thread send và recv. Mọi
thao tác ghi vào connection, việc swap connection khi reconnect, và teardown đều
chạy dưới reentrant lock (`_conn_lock`); vòng lặp recv blocking dài chạy **ngoài**
lock trên một snapshot của connection để send audio không bị starve giữa lượt.
Reconnect là idempotent (re-check `_connected` trong lock) và `_drop_connection()`
chỉ null connection nếu nó vẫn là connection hiện tại — nên 2 thread không thể
tear down / dựng lại connection của nhau.

## Orchestrator

`orchestrator.py` `RealtimeOrchestrator` bọc một session agent và là bề mặt duy
nhất mà `voice_service` giao tiếp:

| Method | Mục đích |
|--------|----------|
| `start()` / `stop()` | Dựng agent từ config, connect, summarize memory khi tắt |
| `append_audio(frame)` | Đẩy 1 frame mic (non-blocking) |
| `commit_audio()` | Báo hết câu nói (non-blocking) |
| `stream_output()` | Yield `AudioOutput` / `TextOutput` / `FunctionCallOutput`, hoặc `DelegateSignal` (rồi dừng) |
| `send_text(text)` | Bơm context (turn context, TTS history) dạng user message không tạo response |
| `send_function_result(call_id, output)` | Trả kết quả tool về model |
| `save_turn(user, agent)` | Lưu một lượt vào realtime memory |
| `available` / `sample_rate` | Trạng thái sẵn sàng + sample rate của provider |

## Context manager

System prompt, định danh thiết bị, device memory, và skills catalog được lắp ráp
theo agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |

`ContextManagerBase` (`context_manager/base.py`) lo phần lắp ráp prompt
(`build_instructions`), lưu lượt (`add_turn`), nạp/trim memory, và summarize;
subclass cài `load_device_context`, `load_device_memory`, `load_skills_catalog`,
`summarize_device_memory`. Prompt nền nằm ở `resources/` (`system_prompt.md` +
bản theo provider `system_prompt_openai.md` / `system_prompt_gemini.md`).

### Memory & summarization

Các lượt realtime được append vào file JSONL (`HAL_REALTIME_MEMORY_PATH`, mặc định
`<workspace>/realtime/memory.jsonl`), trim về `HAL_REALTIME_MAX_MEMORY_ENTRIES`
(giữ lại `HAL_REALTIME_MEMORY_TRIM_KEEP`). `RealtimeSummarizer` (`summarizer.py`)
nén device + realtime memory qua **Anthropic Messages API**
(`HAL_REALTIME_SUMMARIZER_MODEL`, mặc định `claude-haiku-4-5-20251001`).
Summarize chạy lúc `start()` (bù phần chưa tóm tắt) và `stop()` (flush).

## Luồng một lượt (trong `voice_service.py`)

1. **Dựng + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` được tạo;
   `start()` chạy trong daemon thread (`realtime-start`) khi `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` được hook để feed lại text đã nói dạng `[TTS HISTORY]`.
2. **Stream.** Khi session STT đang mở, mỗi frame mic được resample về rate của
   provider và gửi qua `append_audio()` (song song, non-blocking), đồng thời buffer
   vào `rt_audio_buffer`.
3. **Commit.** Cuối session, nếu enabled + `available` + có audio buffer, bơm
   `[TURN CONTEXT]` của lượt (thời gian, user hiện tại) rồi gọi `commit_audio()`.
4. **Tiêu thụ.** `for output in stream_output()`:
   - `TextOutput` → các câu được flush sang TTS (`speak` / `speak_queue`).
   - `DelegateSignal` → dừng; chuyển `[voice-instruction] …` + transcript tới OS
     server với `event_type` gốc.
   - Ngược lại lượt đã được xử lý cục bộ → báo OS server `voice_agent_handled`
     (để OpenClaw trả `NO_REPLY`, bỏ filler dead-air), và lưu lượt vào realtime memory.

## Cấu hình

Realtime agent được cấu hình từ **block `realtime` trong `config.json`** của thiết
bị (các knob hướng người vận hành), với biến môi trường `HAL_*` của HAL là override
cho dev và default built-in là sàn. Thứ tự ưu tiên mỗi knob:

```
biến HAL_*  >  block "realtime" trong config.json  >  default built-in
```

os-server **seed** block này vào `config.json` lúc start lần đầu — và khi upgrade
nếu thiếu — nên file luôn có realtime config sửa được. HAL **tự đọc** trực tiếp
(giống `llm_api_key` / `stt_language`), không push xuống. Vì HAL đọc `config.json`
lúc import, đổi config phải **restart HAL** mới ăn.

### Block `realtime` trong `config.json`

Model ở Go tại `os/services/server/config/realtime.go`; đọc ở HAL tại
`os/hal/config.py`. Field chung ở trên; knob theo provider nằm trong sub-object
`gemini` / `openai`, `provider` chọn cái đang active (`none` hoặc vắng → tắt
realtime). `api_key` / `base_url` rỗng → fallback `llm_api_key` / `llm_base_url`.

```json
"realtime": {
  "enabled": true,
  "provider": "gemini",
  "gemini": { "model": "gemini-3.1-flash-live-preview", "voice": "Kore", "thinking_level": "MINIMAL" },
  "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" }
}
```

Knob reasoning (`thinking_level` / `reasoning_effort`) default về mức **rẻ nhất**
(`MINIMAL` / `minimal`), không phải mức max của provider — muốn reasoning sâu hơn
thì set tường minh. Các knob KHÔNG có trong block (turn detection, session
resumption, memory, summarizer) vẫn chỉ theo env/default.

### Biến môi trường (`os/hal/config.py`)

Mỗi knob có thể bị `HAL_*` env override (thắng block, và là đường cho dev-box):

| Biến | Mặc định | Ghi chú |
|------|----------|---------|
| `HAL_REALTIME_ENABLED` | `true` | Cổng tổng cho pipeline realtime |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = activity detection thủ công) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Số giây tối đa `receive()` chờ output event kế tiếp trước khi kết thúc lượt im lặng (fallback sang main agent) |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Session ngắn hơn ngưỡng này mà không có STT transcript bị coi là nhiễu VAD, không commit lên model |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume cùng session Gemini qua reconnect. Mặc định OFF — proxy `campaign-api` không forward đúng resumption handshake nên resume qua nó tạo session zombie (cold reconnect thì chạy được). Chỉ bật khi endpoint hỗ trợ. |
| `HAL_AGENT_GATEWAY` | `openclaw` | Chọn context manager (cũng đọc từ `agent_runtime` trong config.json) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Key Gemini; fallback về `llm_api_key` |
| `HAL_GEMINI_LIVE_MODEL` | `gemini-3.1-flash-live-preview` | |
| `HAL_GEMINI_LIVE_VOICE` | `Kore` | |
| `HAL_GEMINI_LIVE_BASE_URL` | `<llm_base_url>/ws/gemini` | |
| `HAL_GEMINI_THINKING_LEVEL` | `MINIMAL` | `MINIMAL` \| `LOW` \| `MEDIUM` \| `HIGH` — default rẻ (trước là `HIGH`) |
| `OPENAI_API_KEY` | — | Key OpenAI; fallback về `llm_api_key` |
| `HAL_OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | |
| `HAL_OPENAI_REALTIME_VOICE` | `alloy` | |
| `HAL_OPENAI_REALTIME_BASE_URL` | `<llm_base_url>/ws/openai` | |
| `HAL_OPENAI_REASONING_EFFORT` | `minimal` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — default rẻ (trước là `xhigh`) |
| `HAL_REALTIME_MEMORY_PATH` | `<workspace>/realtime/memory.jsonl` | |
| `HAL_REALTIME_MAX_MEMORY_ENTRIES` / `_TRIM_KEEP` | `1000` / `500` | |
| `HAL_REALTIME_SUMMARIZER_ENABLED` | `true` | |
| `HAL_REALTIME_SUMMARIZER_MODEL` | `claude-haiku-4-5-20251001` | Anthropic Messages API |

## Bản đồ code

| File | Vai trò |
|------|---------|
| `orchestrator.py` | Vòng đời session, tool `delegate_to_main` + `express_emotion`, stream lượt |
| `voice_agent/base.py` | Agent trừu tượng: contract 2-thread/queue, `receive()` |
| `voice_agent/gemini_live.py` | Provider Gemini Live (IO loop asyncio) |
| `voice_agent/openai_realtime.py` | Provider OpenAI Realtime (sync, connection serialize bằng lock) |
| `context_manager/{base,openclaw,hermes}.py` | Lắp ráp prompt + memory + skills theo gateway |
| `summarizer.py` | Summarizer memory dựa trên Anthropic |
| `config.py` | Model config provider (`GeminiConfig`, `OpenAIConfig`) |
| `models/`, `enums/` | Kiểu input/output/event, enum provider + gateway |
| `resources/` | System prompt (chung + theo provider) |
| `../voice/voice_service.py` | Tích hợp: stream audio mic, tiêu thụ output, route delegate/handled |
