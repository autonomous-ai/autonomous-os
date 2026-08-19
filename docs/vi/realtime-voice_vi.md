# Realtime Voice Agent (Trợ lý giọng nói thời gian thực)

Lớp giọng nói speech-to-speech độ trễ thấp, chạy **song song** với pipeline STT
→ agent thông thường. Model realtime xử lý hội thoại tán gẫu trực tiếp (trả lời
âm thanh dưới 1 giây) và **delegate** (chuyển giao) những gì cần đến agent chính
(điều khiển thiết bị, skills, memory, thông tin thời gian thực) về luồng
OS-server.

Code nằm ở `hal/realtime/`; được điều khiển bởi
`hal/drivers/voice/voice_service.py`.

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

Khi model gọi delegate, `stream_output()` **break turn ngay lập tức** sau khi
yield `DelegateSignal` — *không* chờ `turn_complete` của model. Model đã delegate
thì không còn gì để nói nữa, nên drain nốt turn chỉ khiến nó chặn ở timeout
`receive()` (`HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`) — model im suốt cả cửa sổ đó,
cộng thêm ngần ấy giây trễ trước khi agent chính nhìn thấy yêu cầu. Function
result đã được gửi lại model trước khi break; turn còn mở dang dở sẽ được
`flush_output()` của turn kế dọn.

Gemini cũng có thể gửi `generation_complete` trước `turn_complete`: cờ sau bị
trì hoãn trong lúc Gemini giả định client đang phát audio theo thời gian thực.
HAL tự phát câu trả lời đã nhận nên kết thúc consumer turn ngay ở
`generation_complete`, đồng thời nhả commit manual-VAD kế tiếp. Nhờ đó không
còn chờ silent-watchdog vô ích sau khi đã trả lời; `turn_complete` đến muộn sẽ
được bỏ trước lượt sau.

Bản thân cổng này là `wakeword` trong `config.json` (Settings → "Require a wake
word before handling speech"). Thiết bị được set up lần đầu lấy giá trị khởi
tạo từ `voice.wakeword` của body trong `robots/<type>/ROBOT.md` — lamp khai báo
`true`; body không khai báo gì thì giữ always-listening. Thiết bị đã provision
từ trước khi có key này vẫn giữ always-listening qua các bản upgrade: os-server
chỉ lấy default từ ROBOT.md khi `config.json` hoàn toàn chưa có key `wakeword`.

Các phrase được chấp nhận là `hello|hey|hi|alo|okay|ok|wake up` + `autonomous`,
device type (`lamp`), hoặc tên agent trong IDENTITY.md — HAL resolve device type
theo env `DEVICE_TYPE` trước rồi mới tới `config.json`, nên danh sách runtime
khớp với danh sách Settings hiển thị.

Một phiên mic là cả một đoạn nói liên tục chứ không phải một câu, nên việc khớp
diễn ra **theo từng câu**: `starts_with_wake_word()`
(`hal/drivers/voice/_internal/speaker_decorate.py`) tách transcript theo `.` `!`
`?` và chấp nhận wake phrase ở **đầu hoặc cuối bất kỳ câu nào**. Xuất hiện ở
giữa câu vẫn bị từ chối — tên thiết bị nằm giữa câu là người ta đang nói *về*
thiết bị ("this lamp is nice"), mở gate ở đó là chen ngang cuộc trò chuyện của
người khác. Vị trí cuối câu được chấp nhận vì gọi tên ở cuối là cách xưng hô rất
tự nhiên ("what time is it, hey lamp?"). Không có luật theo câu thì một lượt như
"What was the score of the Vietnam versus Malaysia match? Hi lamp, can you hear
me?" bị bỏ nguyên lượt và người dùng chỉ nghe thấy im lặng. Hàm vẫn giữ tên
`starts_with_wake_word` vì mọi nơi gọi nó đều hiểu là "lượt này có nói với mình
không?".

Bước xác nhận trên kết quả final chạy trên transcript đã **ghép**, tức bản còn
nguyên dấu câu. `merge_stt_hypothesis()` chỉ giữ token `\w+` nên xoá luôn ranh
giới câu, khiến cả lượt gộp thành một câu duy nhất và rút lại cái gate mà một
partial đã mở đúng. Vì vậy, trước khi bỏ một lượt mà partial đã mở gate, capture
loop kiểm lại `starts_with_wake_word(combined)` trên transcript thật: khớp thì
set `wake_word_confirmed` và log `Wake-word confirmed on assembled transcript`,
chỉ khi lệch thật mới bỏ lượt.

Cả ba tên đều được gửi cho STT làm boost term (`_stt_boost_terms`), vì nghe sai
tên là mất trắng cả lượt — "hi lamp" ra "hi lance" thì gate không bao giờ mở.
Flux nhận chúng dưới dạng param `keyterm` lặp lại, không trọng số; nova-3 cũng
dùng `keyterm`; các model nova cũ hơn dùng `keywords` kèm intensifier `:3`.

Mọi lượt wake-word đã được STT final xác nhận đều đi qua dispatch. Nó mở một
cửa sổ focus follow-up 20 giây (reset sau mỗi lượt được phép), nên câu nói kế
tiếp có thể bỏ wake phrase và được gửi với type `voice_followup`. Follow-up có
cùng độ ưu tiên người dùng như `voice_command` nhưng vẫn quan sát được riêng.
Nếu realtime đã nói, dispatch gửi event đồng bộ `voice_agent_handled` để agent
chính ghi nhớ nhưng im lặng; realtime unavailable, lỗi, timeout hoặc delegate
đi theo đường agent chính bình thường. Dispatch cũng tiêu thụ vision handoff
một-lượt, nên Gemini lỗi tạm thời không thể làm rơi voice command hoặc làm frame
rò sang lượt sau.

### Silero canh đồng hồ im lặng (kết thúc lượt)

Một phiên mic kết thúc khi audio nằm dưới ngưỡng RMS suốt `SILENCE_TIMEOUT_S`.
Chỉ dùng RMS là không đủ trong phòng ồn: tiếng ồn phòng nằm trên
`RMS_THRESHOLD`, nên frame nào cũng refresh đồng hồ, lượt chạy tới hết
`MAX_SESSION_DURATION_S`, và audio gần như toàn tiếng ồn vẫn được đẩy sang STT —
quan sát ngày 18/08/2026 là các phiên dài 8–25 giây trả về
`transcript='(empty)'`. VAD theo năng lượng bỏ sót khoảng một nửa số frame nói
thật trong môi trường đó, và các stack voice production (Pipecat, LiveKit,
Deepgram) đều đặt một neural VAD ở quyết định này.

RMS vẫn giữ vai trò cổng chặn rẻ chạy trước, nhưng đồng hồ im lặng chỉ được
refresh khi Silero cũng xác nhận có tiếng nói. Silero chạy theo **cửa sổ**
(`SILENCE_VAD_WINDOW_FRAMES`) chứ không theo từng frame: nó tốn ~20 ms/frame
trên ARM và LSTM của nó cần hơn một frame 64 ms mới ổn định. Nó dùng instance
Silero **riêng** — cái thứ ba, bên cạnh gate đầu vào và noise guard của realtime
— để state LSTM của các đường kia không bị bẩn, và nó reset state đó ở đầu mỗi
phiên. Nó fail-open: model lỗi thì coi như có tiếng nói, nên thiết bị không bao
giờ cắt lời ai.

`robots/lamp/rootfs/opt/hal/.env` hạ `HAL_MAX_SESSION_DURATION_S` xuống `20`
(default trong code vẫn là `30`); trần đó chỉ chạm tới khi đồng hồ im lặng không
bao giờ hết hạn, mà người nói thật luôn ngừng lâu hơn `SILENCE_TIMEOUT` trong
vòng 20 giây. Cũng file đó trước kia ghi `WAKEWORD_FOLLOWUP_TIMEOUT_S=60` mà
thiếu prefix `HAL_`, nên nó không có tác dụng gì và thiết bị chạy default 20 s;
nay key đã là `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S=60`.

Nếu kết nối provider **ban đầu** lỗi ngay khi HAL khởi động, orchestrator tạo
session mới bằng retry loop nền (thử lại một lần ngay, rồi backoff luỹ thừa từ 2s,
tối đa 60s). Nó tách biệt
với reconnect send/receive của provider, vì các loop đó chưa tồn tại trước khi
`connect()` thành công. Không cần restart HAL hay chờ audio mới; các lượt voice
vẫn fallback xuống agent chính cho tới khi kết nối hồi phục.

## Biểu cảm cảm xúc (fire-and-forget)

Nếu thiết bị khai báo capability `expression`
(`ROBOT.md` → `expression: { routes: [emotion] }`), orchestrator còn đăng ký
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

## Google Search grounding (chỉ Gemini)

Mặc định Gemini Live được cấp sẵn tool **Google Search** built-in
(`HAL_GEMINI_GOOGLE_SEARCH`, mặc định bật; wiring trong `gemini_live.py` như một
`types.Tool(google_search=…)` riêng, đặt cạnh các tool function-declaration). Nhờ
đó model realtime tự trả lời các câu **dữ liệu công khai theo thời gian thực** —
thời tiết, tin tức, thể thao, giá cả, "mấy giờ mặt trời lặn" — bằng cách grounding
ngay trong phiên và tự nói kết quả, thay vì gọi `delegate_to_main` và chịu nguyên
một vòng round-trip xuống main agent. System prompt của Gemini
(`system_prompt_gemini.md`) xếp các lookup công khai này vào mục *Direct Home Run*,
và chỉ chuyển dữ liệu live **thuộc tài khoản/riêng tư** (lịch của user, trạng thái
thiết bị smart-home của họ, tin nhắn của họ) cho `delegate_to_main`.

Đánh đổi:

- **Chỉ Gemini.** OpenAI Realtime không có tool built-in tương đương, nên prompt
  của nó (`system_prompt_openai.md`) vẫn delegate mọi lookup bên ngoài. Qwen Omni
  Realtime cũng vậy — không có search grounding, prompt của nó
  (`system_prompt_qwen.md`) delegate mọi câu dữ liệu thời gian thực.
- **Chi phí.** Grounding tính phí theo mỗi grounded request (cộng thêm token),
  nhưng chỉ phát sinh khi Gemini thực sự quyết định search. Prompt dặn nó *chỉ*
  ground cho dữ kiện công khai/mới thật sự, không ground cho kiến thức chung đã có
  sẵn. So với trước, phần lớn là **dời** chi phí (và latency) khỏi main agent.
- **Chỉ đọc.** Grounding chỉ trả lời câu hỏi, không thực hiện hành động. Nhạc,
  phần cứng, ghi memory, và skill vẫn delegate.

## Thị giác trong phiên — tool `look` (chỉ Gemini)

Khi người dùng hỏi về thứ thiết bị **nhìn thấy** ("cái này là gì?", "tôi đang cầm
gì?", "đọc cái nhãn này", "màu gì đây?"), model realtime trả lời ngay trong phiên
thay vì delegate. Orchestrator đăng ký tool `look` (`orchestrator.py`,
`LOOK_TOOL`) và xử lý trong `_handle_look_call`:

1. Lấy frame camera **nét** **in-process** (`_capture_frame` gọi
   `capture_still` — không qua HTTP loopback; servo bị freeze (cả animation
   loop lẫn servo worker của tracker đều tôn trọng cờ này) và frame chỉ được
   chấp nhận khi timestamp chụp ≥ 0.3s sau lần ghi bus servo cuối, nên motion
   blur không lọt tới model; không thêm độ trễ khi servo vốn đang đứng yên
   hoặc thiết bị không có servo), downscale về `HAL_GEMINI_VISION_MAX_WIDTH`
   (mặc định 768px) để giới hạn token ảnh.
2. Đẩy vào làm **video input** realtime (`ImageInput` → `send_realtime_input(video=…)`),
   rồi **replay turn**: Live API xếp frame gửi giữa-turn vào turn KẾ TIẾP
   (device-proven: flow ack-tool → tiếp-turn cũ khiến mọi câu look trả lời bằng
   ảnh của lần look *trước* — lệch 1 ảnh, delay ack bao nhiêu cũng không cứu),
   nên thay vì ack tool call, orchestrator yield `LookReplaySignal` và
   `run_realtime_turn` gửi lại audio của turn + commit lần nữa trên CÙNG
   session. Frame đang xếp hàng vào đúng turn replay.
3. Turn replay kích hoạt `look` lần nữa, rơi vào reuse guard
   (`VISION_MIN_INTERVAL_S`) và được ack `trigger_response=True` — model trả
   lời bằng frame lúc này đã thật sự nằm trong context.

Plumbing hỗ trợ replay: `receive()` nuốt đúng MỘT `turn_complete` cũ (của turn
bị hủy, về sau replay commit và nếu không sẽ kết thúc rỗng turn replay —
`skip_next_turn_done()`); recycle session idle/turn-cap đang chờ sẽ bị hoãn khi
replay pending (rebuild lúc đó làm mồ côi ảnh vừa gửi); và mọi lần rebuild
session đều reset look reuse guard (ảnh sống trong session — session mới không
có ảnh nào). Chi phí: audio câu hỏi bị tính 2 lần ở turn look; ảnh 1 lần.

Cái này thay cho đường chậm (delegate → main → tìm skill → `/camera/snapshot` →
LLM vision, vài giây) bằng một round-trip ngay trong phiên.

Điều kiện kích hoạt (cần cả ba, nếu không câu hỏi thị giác sẽ rơi về delegate):

- **Capability:** có camera (`app_state.camera_capture` được set). Đây chính là
  capability `vision` ở runtime — `server.py` chỉ tạo `camera_capture` khi
  ROBOT.md khai báo `vision`. Orchestrator đọc đúng một tín hiệu này
  (`_camera_present()`), nên đúng cho mọi đường khởi tạo.
- **Flag:** `HAL_GEMINI_VISION` / `realtime.gemini.vision` (mặc định **bật**).
- **Provider:** chỉ Gemini (luồng inject ảnh → tiếp tục turn đã làm + test cho
  Gemini Live; OpenAI và Qwen vẫn delegate — Qwen Omni qua đường realtime này
  chỉ có text+audio, không có vision trong phiên). System prompt Gemini
  (`system_prompt_gemini.md`) mô tả khi nào gọi `look`.

Chi phí: một frame mỗi lần gọi (kích bằng tool, **không** stream video), nên token
thêm vào là không đáng kể so với audio của turn. Frame 768px ≈ vài trăm token ảnh.
Để chặn model gọi `look` quá nhiều làm tốn token ảnh, `_handle_look_call` chỉ gửi
**tối đa một ảnh mỗi turn** và **không gửi ảnh mới trong vòng
`HAL_GEMINI_VISION_MIN_INTERVAL_S` (mặc định 10s)** kể từ lần gửi trước — các lần
`look` lặp lại sẽ xài lại ảnh đã có trong context.

**Bàn giao frame khi delegate / timeout.** Khi một turn `look` rốt cuộc delegate
hoặc rớt xuống main agent (quan trọng nhất là khi Gemini timeout *giữa* lúc look),
frame mà `look` đã chụp được bàn giao cho main agent để nó trả lời từ đúng ảnh
đó thay vì chụp lại (nhanh hơn, và trả lời đúng khoảnh khắc user chỉ vào).
`_handle_look_call` lưu frame vào `_SNAPSHOT_DIR` và ghi vào
`app_state.realtime_look_frame_path`; `turn_dispatch._take_vision_handoff()`
tiêu thụ nó **một lần mỗi turn** (turn đã handled dùng rồi thì clear luôn để
delegate sau không nhặt phải ảnh cũ) và, khi còn tươi
(`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, mặc định 20s), chèn dòng hint
`[vision-image] <path>` vào message VÀ gửi frame dạng base64 trong field
`image` của sensing POST.
os-server xử lý ảnh theo **gate describe-first** trong `system/vision` (xem
`server/sensing/delivery/http/handler.go`): khi main model đang active KHÔNG
khai image input trong catalog model (trường hợp Auto-AI — attachment thô sẽ
404 tại smart-agent-router: "No endpoints found that support image input"),
frame được `default_image_model` của catalog (qwen — cùng model mà `imageModel`
của openclaw dùng cho ảnh Telegram) tả thành chữ và agent nhận dòng
`[image description] …` — đồng thời hint `[vision-image]` được viết lại để
**bỏ path file**, và **file snapshot cũng bị xoá luôn** (best-effort). Cả
path lẫn file đều không được sống chung với description: snapshot nằm trong
media allow-list của agent nên bất kỳ path nào agent vớ được — hint, hint cũ
trong session history, `ls` thư mục — đều có thể bị `read` thành image block
nằm lì trong session history, làm 404 mọi turn sau mà router rơi vào model
text-only (kể cả turn thuần chữ). Describe được thử 2 lần (20s + 15s, tổng
35s — request treo được retry trên kết nối mới); fail cả hai thì ảnh bị
**bỏ luôn**, file snapshot vẫn bị xoá, và hint được viết lại để agent nói
với user là lần này không nhìn được — tuyệt đối không gửi raw attachment,
vì khi router rơi vào model text-only thì attachment đó đầu độc cả session,
đắt hơn nhiều so với hỏng một turn. Còn khi catalog nói model nhận ảnh,
attachment thô được forward thẳng và hint giữ nguyên path. Gate đọc lại catalog mỗi 30 phút, nên BE flip catalog là
fleet tự chuyển. Gate này cũng cover luôn ảnh upload từ web monitor chat — cả
hai nguồn ảnh hội tụ về một handler. Skill `camera` dặn agent trả lời từ mô
tả/attachment và bỏ qua `/camera/snapshot`. Nếu timeout xảy ra *trước khi* kịp
chụp thì không có gì để bàn giao, agent chụp như bình thường.

## Các provider

Ba backend thay thế cho nhau, chọn bằng `HAL_REALTIME_PROVIDER`
(`none` | `gemini` | `openai` | `qwen`):

| Provider | Class | Mô hình threading | Model mặc định | Sample rate |
|----------|-------|-------------------|----------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | event loop asyncio riêng trên thread `gemini-io`; thread send/recv submit coroutine qua `run_coroutine_threadsafe` | `gemini-2.5-flash-native-audio-preview-12-2025` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | thuần đồng bộ; 1 `RealtimeConnection` dùng chung bởi thread send/recv, serialize bằng reentrant lock | `gpt-realtime-2` | 24000 Hz |
| Qwen Omni Realtime | `voice_agent/qwen_realtime.py` `QwenRealtimeAgent` | thuần đồng bộ; client `websockets.sync.client` thô | `qwen3.5-omni-plus-realtime` | 16000 Hz |

Gemini Live dùng `google-genai` và private asyncio loop của nó do thread
`gemini-io` sở hữu. Teardown đóng/hủy provider receive task trước, rồi mới join
worker; handshake thất bại rollback loop/thread ngay. Nhờ vậy một receive bị
kẹt không sống sót qua session rebuild. Với họ native-audio, HAL gửi websocket
ping mỗi 20 giây nhưng không đặt ping timeout: traffic đi ra giữ đường proxy
sống mà pong bị thiếu không bị hiểu là lỗi client. HAL cũng
recycle Gemini đồng bộ trước khi stream audio nếu lượt trước đã kết thúc quá
`HAL_GEMINI_PRE_TURN_RECYCLE_S` giây, để câu nói sau khoảng nghỉ không rơi vào
socket đã chết vì idle ở proxy.

Mọi provider coi teardown là trạng thái kết thúc: sau khi `disconnect()` đặt
stop signal, worker send/receive không reconnect và cũng không ghi log lỗi
transport trong lúc socket đã đóng đang unwind.

**Qwen Omni Realtime** (Alibaba DashScope / Model Studio) nói **schema event BETA
của OpenAI Realtime** (`session.update`, `input_audio_buffer.append/commit`,
`response.create`, `response.audio.delta`, `response.audio_transcript.delta`,
`response.done`) qua đường WS của DashScope
`wss://<workspace-host>/api-ws/v1/realtime?model=...` với header
`Authorization: Bearer <key>`. Không tái dùng được OpenAI python SDK (SDK nói
schema GA), nên `qwen_realtime.py` là client `websockets.sync.client` thô. Audio
input 16 kHz mono pcm16 base64, output 24 kHz mono pcm16. Luồng turn thủ công
(HAL local VAD): append → commit → `response.create`; `response.create` **bắt
buộc** kèm `response.modalities ["text","audio"]` tường minh, nếu không server
trả lời text-only (verify live 2026-07-06). Model mặc định
`qwen3.5-omni-plus-realtime`: bản legacy `qwen-omni-turbo-realtime` KHÔNG bao
giờ gọi function call và lờ `[TURN CONTEXT]` (device-test 2026-07-06) → hỏng
toàn bộ luồng delegate. Voice: Ethan (mặc định) và Serena trên 3.5-plus;
Cherry/Chelsie chỉ dùng được với turbo (ghép sai → `InvalidParameter` ngay
response đầu); **không** có knob reasoning/thinking (web ẩn selector Reasoning).
Web search built-in (model 3.5) bật qua session `enable_search: true` (knob
`realtime.qwen.search` / `HAL_QWEN_SEARCH`, mặc định bật) — bản qwen của Google
Search grounding bên Gemini. Ràng buộc DashScope: search ("agent mode") KHÔNG
cho đăng ký function tools cùng session, nên khi search bật, delegate chạy qua
giao thức text-marker: agent nối suffix `[TOOL PROTOCOL]` vào instructions,
model trả lời đúng `[DELEGATE] <message>`, recv loop nuốt transcript đó và tổng
hợp FunctionCallOutput `delegate_to_main` y hệt tool call thật (orchestrator
không phân biệt được; `express_emotion` không dùng được ở mode này). Khi search
tắt, function tool (`delegate_to_main`, `express_emotion`) được truyền trong
`session.update` (format beta phẳng) và
`response.function_call_arguments.done` được xử lý. Mỗi turn, dòng token/cost
ghi vào file log riêng `qwen_usage.log` (logger `hal.realtime.usage.qwen`, sinh
đôi với `gemini_usage.log`); bảng giá `_QWEN_RATES` trong `qwen_realtime.py`
($0.27/1M input, $1.07/1M output — Model Studio quốc tế công bố một mức giá
blended duy nhất, chưa công bố tách theo modality; bảng vẫn giữ key theo
modality để drop số tách console-verified vào sau). Audio ≈ 25 token/giây cả
hai chiều (verify: 5.1s audio out = 128 token); usage payload gồm
`input_tokens`/`output_tokens` + `input_tokens_details`/`output_tokens_details`
`{text_tokens, audio_tokens}` + `cached_tokens` top-level.

Cả ba kế thừa `voice_agent/base.py` `VoiceAgentBase`, định nghĩa contract dựa
trên queue:

- **2 thread mỗi agent**: `_send_loop` rút `_send_queue` → API; `_recv_loop` đọc
  API → `_recv_queue`. Cả hai tự reconnect khi lỗi.
- **Fail-fast khi backend lỗi** (cả 2 driver): khi `_recv_loop` gặp lỗi thật
  (Gemini Live: proxy `go_away`, hết quota / resource-exhausted, WS close bất
  thường — tức **không phải** idle close `1000` lành tính; OpenAI: event `error`
  của Realtime API hoặc socket rớt), nó đẩy `TurnDoneEvent` ngay lập tức
  (`_fail_fast_turn`) để `receive()` thoát liền và lượt fallback sang main agent
  **mà không** phải chờ hết `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`. Idle close lành
  tính vẫn reconnect êm (Gemini code `1000`; OpenAI kết thúc vòng lặp event êm,
  không phải lỗi). Chỉ kích hoạt khi đang có lượt chờ output (`_turn_done` clear);
  reconnect vẫn chạy nền để hồi phục session cho lượt sau.
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

## Pricing & log usage

Mỗi turn ghi một dòng token/cost vào log riêng theo provider dưới
`/var/log/hal/` (rotating, 5 MB × 3): `gemini_usage.log` (logger
`hal.realtime.usage`) và `qwen_usage.log` (logger `hal.realtime.usage.qwen`).
OpenAI chỉ log dòng usage thường vào `server.log` (`[realtime] OpenAI usage`),
không ước tính cost. Dòng log mang đủ số token theo từng modality **và** cost
USD ước tính, nên rate có sai thì sau này vẫn tính lại được từ số token đã ghi.

Bảng rate nằm trong code, key `(direction, modality)` tính USD trên 1M token —
`_GEMINI_RATES` trong `voice_agent/gemini_live.py`, `_QWEN_RATES` trong
`voice_agent/qwen_realtime.py`. Model lạ rơi về bảng đắt nhất (cost là trần,
không bao giờ báo thiếu).

| Model | text in | audio in | text out | audio out | audio↔token | Nguồn |
|---|---|---|---|---|---|---|
| `gemini-2.5-flash-native-audio` | $0.50 | $3.00 | $2.00 | $12.00 | 25 tok/s | ai.google.dev pricing (verify 2026-06-29) |
| `gemini-3.1-flash-live` | $0.75 | $3.00 | $4.50 | $12.00 | 25 tok/s | ai.google.dev pricing (verify 2026-06-29) |
| `qwen-omni-turbo-realtime` | $0.27 | $4.44 | $8.89* | $8.89* | 25 tok/s in+out | bill CSV consume-detail (verify 2026-07-06); *output turn có audio bill gộp text+audio (`multi_output_token`); response text-only bill $1.07 (`purein_text_output`) |
| `qwen3.5-omni-flash-realtime` | $0.27 | $4.44 | $8.89* | $8.89* | ~7 tok/s in, ~12.5 tok/s out | bill CSV (verify 2026-07-06): flash bill CÙNG line item rẻ như turbo, kể cả phiên bật search — text-in (phần nặng nhất) rẻ hơn Gemini 3.1 ~2.8x. Search +$0.01/request |
| `qwen3.5-omni-plus-realtime` | $2.10 | $16.50 | $62.00* | $62.00* | ~7 tok/s in, ~12.5 tok/s out | bill CSV consume-detail (verify 2026-07-06); *một line item `omni_audio_output_token` bao cả text+audio của response. Web search tính thêm $0.01/lần search |

Cơ cấu chi phí giống nhau ở mọi provider: `in_text` chiếm áp đảo (system
prompt ~7-10k token + context session tích lũy bị re-bill mỗi turn, phình dần
tới khi session recycle — xem `HAL_REALTIME_SESSION_IDLE_RESET_S` /
`HAL_REALTIME_SESSION_MAX_TURNS`); token audio chỉ là phần lẻ. Gemini tính
thêm phí Google Search theo từng request grounded, ngoài token.

## Orchestrator

`orchestrator.py` `RealtimeOrchestrator` bọc một session agent và là bề mặt duy
nhất mà `voice_service` giao tiếp:

| Method | Mục đích |
|--------|----------|
| `start()` / `stop()` | Dựng agent từ config, connect, summarize memory khi tắt |
| `append_audio(frame)` | Đẩy 1 frame mic (non-blocking) |
| `commit_audio()` | Báo hết câu nói (non-blocking) |
| `stream_output()` | Yield `AudioOutput` / `TextOutput` / `FunctionCallOutput`, hoặc `DelegateSignal` (rồi dừng) |
| `send_text(text)` | Bơm context (turn context, TTS history) dạng user message không tạo response. Gemini Live bỏ qua bước này để tránh va chạm giữa SDK `clientContent` và lượt audio; OpenAI vẫn nhận. |
| `send_function_result(call_id, output)` | Trả kết quả tool về model |
| `save_turn(user, agent)` | Lưu một lượt vào realtime memory |
| `available` / `sample_rate` | Trạng thái sẵn sàng + sample rate của provider |
| `rebuilding` / `wait_until_available()` | Quan sát và chờ ngắn session thay thế vốn đang kết nối, không tự khởi động thêm rebuild |

## Context manager

System prompt, định danh thiết bị, device memory, và skills catalog được lắp ráp
theo agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |
| `picoclaw` | `OpenClawContextManager` (layout giống hệt) | `HAL_PICOCLAW_WORKSPACE_DIR` (`/root/.picoclaw/workspace`) |
| `codex` | `OpenClawContextManager` (layout giống hệt) | `HAL_CODEX_WORKSPACE_DIR` (`/root/.codex/workspace`) |
| `claudecode` | `context_manager/claudecode.py` `ClaudeCodeContextManager` — layout OpenClaw trừ skills, đọc từ `.claude/skills/` (dir native của claude CLI) | `HAL_CLAUDECODE_WORKSPACE_DIR` (`/root/.claudecode/workspace`) |
| `opencode` | `OpenClawContextManager` (layout giống hệt; như codex, skills nằm ở dir ngoài workspace `~/.config/opencode/skills` nên catalog skills theo workspace rỗng — identity + memory vẫn nạp đúng) | `HAL_OPENCODE_WORKSPACE_DIR` (`/root/.opencode/workspace`) |

`ContextManagerBase` (`context_manager/base.py`) lo phần lắp ráp prompt
(`build_instructions`), lưu lượt (`add_turn`), nạp/trim memory, và summarize;
subclass cài `load_device_context`, `load_device_memory`, `load_skills_catalog`,
`summarize_device_memory`. Prompt nền nằm ở `resources/` (`system_prompt.md` +
bản theo provider `system_prompt_openai.md` / `system_prompt_gemini.md` /
`system_prompt_qwen.md`, đăng ký trong `PROVIDER_PROMPT_PATHS` của
context_manager).

### Memory & summarization

Các lượt realtime được append vào file JSONL (`HAL_REALTIME_MEMORY_PATH`, mặc định
`<workspace>/realtime/memory.jsonl`), trim về `HAL_REALTIME_MAX_MEMORY_ENTRIES`
(giữ lại `HAL_REALTIME_MEMORY_TRIM_KEEP`). `RealtimeSummarizer` (`summarizer.py`)
nén device + realtime memory qua **Anthropic Messages API**
(`HAL_REALTIME_SUMMARIZER_MODEL`, mặc định `claude-haiku-4-5-20251001`).
Summarize chạy lúc `start()` (bù phần chưa tóm tắt) và `stop()` (flush). Phần
catch-up ở `start()` chạy trong **thread nền** (sau `connect()`), nên lời gọi
Anthropic không chặn session trở thành `available` — nếu chặn thì một lượt nói
sớm ("hello") ngay sau khi restart sẽ rớt xuống main agent.

## Luồng một lượt (trong `voice_service.py`)

1. **Dựng + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` được tạo;
   `start()` chạy trong daemon thread (`realtime-start`) khi `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` được hook để feed lại text đã nói dạng `[TTS HISTORY]`,
   nhưng **chỉ khi lượt nói đó opt-in** (`TTSService.realtime_feedback`, đặt bởi
   cờ `realtime_feedback` trên `/voice/speak[-queue]`). Chỉ reply thật của agentic
   runtime mới opt-in — os-server gửi qua `hal.SpeakReply` / `hal.SpeakQueueReply`
   (được `SendToHALTTS` / `SendToHALTTSQueue` dùng). Mọi TTS hardcode (dead-air
   filler, ambient mumble, backchannel, thông báo reconnect/health, chitchat
   local) đi qua `hal.Speak` thường và **không bao giờ** được feed lại — nếu không
   model sẽ lặp lại (echo) những câu nó chưa từng sinh ra.
2. **Stream.** Khi session STT đang mở, mỗi frame mic được resample về rate của
   provider và gửi qua `append_audio()` (song song, non-blocking), đồng thời buffer
   vào `rt_audio_buffer`.
   Khi bật STT keepalive tùy chọn, nếu socket STT pre-connect đóng bình thường
   (WS 1000) đúng lúc bắt đầu nói, HAL thay socket trước khi stream tiếp và replay
   toàn bộ pre-roll đúng một lần vào socket mới. Nhờ vậy không mất từ mở đầu; close
   bình thường đã recovery là warning, không phải error.
   Gemini Manual VAD không có lệnh huỷ một activity đã stream. Vì vậy turn
   empty-STT/noise khởi động session sạch thay vì để noise lẫn vào câu người dùng
   kế tiếp. Reconnect này chạy nền: nếu user nói ngay, HAL giữ toàn bộ audio của
   turn mới ở local rồi gửi đúng một lần, đúng thứ tự khi session thay thế sẵn
   sàng. Nếu reconnect chậm/lỗi thì fallback về main agent với transcript STT;
   không làm rớt audio đầu câu hoặc commit nó vào activity cũ.
3. **Bơm turn context + prepass speaker-ID.** `[TURN CONTEXT]` (thời gian, nhắc
   ngôn ngữ trả lời, user hiện tại) được gửi dạng text không tạo response. **User
   hiện tại chính là người nói (VOICE speaker)** được nhận dạng trong lượt này — nó
   **ghi đè** `current_user` suy ra từ khuôn mặt, và rơi về định danh khuôn mặt khi
   không có voice ID (unknown / gate-reject / không có transcript).

   **Thời điểm chạy khác nhau theo mode**, vì voiceprint cần trọn câu nói nên không
   thể tồn tại lúc mới mở session:

   | Mode | Gửi `[TURN CONTEXT]` | Biết người nói? |
   |------|---------------------|-----------------|
   | Always-listening (`wakeword=false`) | lúc **mở** session, trước khi có audio | Không → fallback khuôn mặt, rồi mới sửa |
   | Wake-word / follow-up | sau khi capture xong, khi final xác nhận wake phrase | Có |
   | Deferred (rebuild sau noise-drop) | sau khi capture xong, trên session thay thế | Có |

   Cả hai dòng chạy sau capture còn bị chặn thêm một điều kiện: lượt đó **không**
   phải noise. Chúng chạy sau khi noise guard đã phân loại xong capture, nên một
   lượt STT rỗng mà không phải tiếng nói sẽ không mở gì cả: không `[TURN CONTEXT]`,
   không audio, không session thay thế. Chính việc không gửi mới làm cho đường
   skip-commit trở nên miễn phí — nếu không, toàn bộ buffer của lượt đó đã vào (và
   bị tính tiền trong) một activity đang mở mà ngay bước sau lại vứt đi. Session
   được mở *sớm hơn* trong lúc capture (always-listening) thì đã stream audio rồi
   nên vẫn bị discard như cũ.

   Ở mode always-listening, prepass speaker-ID (`identify_and_decorate`, chạy **một
   lần** cuối session) chỉ giải được người nói *sau khi* context đã gửi đi kèm tên
   từ khuôn mặt. HAL gửi tiếp một correction `[TURN CONTEXT UPDATE]` nêu đúng người
   nói — vẫn **trước** `commit_audio()` nên thuộc cùng một lượt. Bỏ qua khi context
   đã mang đúng tên, hoặc khi lượt đó là noise. Kết quả prepass được xài lại ở hạ
   nguồn — speaker recognition không bao giờ chạy hai lần.

   **Lưu ý Gemini native-audio:** `send_text()` bỏ **toàn bộ** text không tạo
   response trên các model Gemini `*native-audio*` (`gemini_needs_idle_workaround()`),
   vì các message SDK `clientContent(turn_complete=False)` lặp lại va với lượt audio
   sau đó và đóng WS 1011. Trên các model đó cả context lẫn correction đều không tới
   được câu trả lời, và model rơi về định danh còn lưu trong memory của session.
   `gemini-3.1-flash-live` và OpenAI thì nhận cả hai. Mọi lần bỏ đều được log
   (`[realtime->model] DROPPED …`).

   **Điều này không áp dụng cho cấu hình mặc định đang ship.** `REALTIME_GEMINI_MODEL`
   mặc định là `gemini-3.1-flash-live-preview` (`hal/config.py:734`), không phải
   native-audio, nên guard tắt và cả context lẫn correction đều tới được model. Nó chỉ
   bật lại khi ai đó cấu hình một model `*native-audio*`.
4. **Commit.** Cuối session, nếu enabled + `available` + có audio buffer, gọi
   `commit_audio()`. Cue emotion `thinking` fire cùng lúc commit (mặt + servo +
   LED pulse ÉP HIỆN — `thinking` vốn là background emotion có LED nhường
   màu user đã set; cue realtime bypass đúng guard đó, còn user tắt đèn thì vẫn
   tắt) và được clear về `idle` khi có output đầu tiên (câu TTS đầu hoặc frame
   audio native đầu) hoặc khi turn chết không output — trừ khi model đã tự
   express emotion riêng. Lấp khoảng 1-3s latency của model mà trước đây device
   nhìn như đứng hình.

   Cùng lúc commit cũng arm **dead-air filler** (`_WaitFiller`) — nửa phần tiếng
   của chính cue đó. Sau `HAL_REALTIME_FILLER_DELAY_S` (mặc định 1.5s) mà vẫn
   chưa có output nào, HAL gọi `POST /api/sensing/filler` và os-server phát một
   câu filler mở đầu từ cache — pool phrase, ngôn ngữ và WAV cache đều nằm ở
   os-server, nên khoảng chờ realtime và khoảng chờ main agent nghe giống nhau.
   Câu chit-chat bình thường (~1s) không bao giờ chạm timer; lượt model dùng
   Google Search — không ra token nào cho tới khi search xong — thì có. Filler
   phát interruptible nên câu đầu tiên của model cắt ngang nó; mọi đường thoát
   (trả lời, delegate, turn rỗng, exception) đều cancel timer, riêng delegate
   cancel tường minh vì chặng main agent ngay sau đó tự bắn filler của nó. `0`
   để tắt.

   Phát filler là TTS, nên nó dừng pulse thinking và chạy speaking wave. Để
   phần chờ còn lại vẫn có tín hiệu, cue đánh dấu strip là của mình
   (`app_state._thinking_cue_active`): lần restore LED sau TTS vẽ lại pulse
   thinking thay vì rơi về user state. Cờ được bỏ khi cue clear và khi có bất
   kỳ emotion nào khác vào qua `POST /emotion`, nên emotion model tự express
   không bị đè.
5. **Tiêu thụ.** `for output in stream_output()`:
   - `TextOutput` → các câu được flush sang TTS (`speak` / `speak_queue`).
     Nếu `speak` báo busy (TTS khác đang giữ loa non-interruptible, ví dụ
     nudge ambient), câu sẽ fallback sang `speak_queue` để phát sau đó thay
     vì bị mất luôn.
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
lúc import, đổi config phải **restart HAL** mới ăn. Sửa lúc đang chạy thì restart
liền (`restartHAL` trong `system/device/service.go`).

**Chỉ restart khi config thực sự đổi.** os-server *không* restart HAL mỗi lần
os-server restart — làm vậy sẽ rớt voice pipeline vô ích. Thay vào đó nó hash
`config.json` và lưu hash vào `config/.hal_config_hash` mỗi khi (re)start HAL. Lúc
boot (`handleSetUpCompleteChange` trong `server/config_watch.go`) nó chỉ restart HAL
khi hash hiện tại khác snapshot — tức config thật sự đổi trong lúc os-server tắt
(setup mới, OTA đổi config, sửa lúc downtime), hoặc chưa có snapshot (boot đầu). Một
lần os-server restart bình thường với config không đổi sẽ để nguyên HAL đang chạy.
Nếu HAL thật sự chết, `hal.service` (`Restart=always`, `RestartSec=5`) tự hồi độc
lập, nên skip restart là an toàn. Nhánh `restartHAL` cập nhật lại snapshot sau khi
restart HAL, nên đổi lúc-chạy rồi os-server restart không restart hai lần. Hash cả
file (thay vì chỉ tập field HAL đọc) giữ tín hiệu tự-bảo-trì khi tập field HAL đọc
thay đổi; cái giá duy nhất là một lần restart HAL thừa ở boot kế tiếp khi sửa field
chỉ-thuộc-os-server.

### Block `realtime` trong `config.json`

Model ở Go tại `system/server/config/realtime.go`; đọc ở HAL tại
`hal/config.py`. Field chung ở trên; knob theo provider nằm trong sub-object
`gemini` / `openai` / `qwen`, `provider` chọn cái đang active (`none` hoặc vắng →
tắt realtime). `api_key` / `base_url` rỗng → fallback `llm_api_key` /
`llm_base_url` — **trừ qwen**: credential của qwen là của riêng nó
(`realtime.qwen.api_key` / `realtime.qwen.base_url`, Go struct `QwenRealtime`
còn có `model`/`voice`), **cố tình không** fallback về `realtime.api_key`/
`base_url` chung hay credential `llm_*` — qwen nói thẳng với host Alibaba MaaS,
không đi qua proxy `campaign-api`. Set qua `realtime.qwen.*` trong config.json
hoặc qua env trên device (`DASHSCOPE_API_KEY`, `HAL_QWEN_REALTIME_BASE_URL`
trong `/opt/hal/.env`); thiếu cả hai thì WS handshake fail rõ ràng trong log hal.

> **Để `base_url` trống trừ khi có endpoint riêng (không qua proxy).** Khi trống,
> HAL tự suy ra `<llm_base_url>/ws/gemini` (hoặc `/ws/openai`) — đúng suffix WS mà
> proxy `campaign-api` route. Nếu `base_url` bị set bằng `llm_base_url` trần (thiếu
> `/ws/...`), giá trị đó được đưa thẳng vào SDK provider và **404 ngay ở Live
> handshake**. Vì vậy ô "Base URL" trong web Settings chỉ hiển thị *override tường
> minh* (`RealtimeBaseURLOverride`, không phải giá trị đã resolve), để "để trống là
> tự suy ra" luôn trống và mỗi lần Save không vô tình ghi đè URL trần. Quy tắc
> này KHÔNG áp cho qwen: qwen giữ `base_url` riêng trong sub-object của nó và
> không bao giờ suy ra từ `llm_base_url`.

```json
{
  "wakeword": false,
  "realtime": {
    "enabled": true,
    "provider": "gemini",
    "gemini": { "model": "gemini-3.1-flash-live-preview", "voice": "Kore", "thinking_level": "MINIMAL" },
    "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" },
    "qwen": { "api_key": "sk-…", "base_url": "wss://…", "model": "qwen3.5-omni-plus-realtime", "voice": "Ethan" }
  }
}
```

Knob reasoning (`thinking_level` / `reasoning_effort`) default về mức **rẻ nhất**
(`MINIMAL` / `minimal`), không phải mức max của provider — muốn reasoning sâu hơn
thì set tường minh. Qwen **không có** knob reasoning/thinking, nên web ẩn
selector Reasoning khi provider là qwen. Các knob KHÔNG có trong block (turn
detection, session resumption, memory, summarizer) vẫn chỉ theo env/default.

**Filter chống leak CoT.** Trên `gemini-3.1-flash-live-preview` KHÔNG tắt được
thinking: `thinking_level=MINIMAL` lẫn `thinking_budget=0` đều được chấp nhận
nhưng bị bỏ qua (đo `thoughts_token_count` 125–168 trên turn cần suy luận với mọi
config). Bình thường thoughts nằm nội bộ, nhưng trên các turn có
grounding/vision/tool, server thỉnh thoảng đổ nguyên text channel của model —
đoạn lập kế hoạch tiếng Anh ("The user is insisting…", "Phrasing draft:",
"Delivery guidance:") kèm câu trả lời thật — vào `output_audio_transcription`,
trong khi audio của model chỉ chứa câu trả lời sạch. Vì native audio tắt, HAL
đọc transcription → không có guard thì leak bị đọc thành tiếng (tốn ký tự TTS)
và forward vào `[REPLY]`, quay lại context và tự củng cố.
`drivers/voice/_internal/cot_leak_filter.py` chặn leak ở mức câu, trước TTS và
trước khi transcript được forward/lưu, theo 3 tầng: marker TRIGGER (ngôi thứ ba
gắn động từ "the user is/wants…", nhãn planning như "Phrasing draft:") luôn drop
và bật cot-mode cho turn; marker PHỤ ("persona", "system prompt", "emotion
tool", …) chỉ drop khi cot-mode đã bật — câu trả lời hợp lệ nói về chính thiết
bị vẫn an toàn; trong cot-mode drop thêm câu planning tiếng Anh (chỉ với device
không nói tiếng Anh — chữ viết không-Latin như tiếng Việt/Trung/Nhật dùng check
tỉ lệ ASCII, chữ Latin như Pháp/Indo yêu cầu thêm function word tiếng Anh để
answer thật không bị nuốt), draft trong ngoặc kép, mảnh plan vụn, và câu
gần-trùng câu đã giữ (CJK token theo từng ký tự). Check ngôn ngữ bỏ qua các
đoạn nằm trong ngoặc, nên câu planning tiếng Anh nhúng text ngôn-ngữ-trả-lời
trong ngoặc ("The search query 'cách dùng…' didn't yield…") vẫn bị bắt, còn
câu ngôn-ngữ-trả-lời trích dẫn tiếng Anh thì không. Mỗi câu bị drop đều log
`CoT leak dropped`.

Đường agent chính (reply openclaw/hermes nói qua os-server) có bản port Go của
filter này — `system/server/agent/delivery/http/cot_leak_filter.go` (thêm
TRIGGER identifier snake_case cho corpus leak DeepSeek); xem
`docs/vi/flow-monitor_vi.md` § "CoT-leak filter (đường agent)". Harden bên nào
thì nhớ sync bên kia.

### Cấu hình runtime (`hal/config.py` + `config.json`)

Mỗi biến môi trường `HAL_*` ghi đè setting tương ứng; `wakeword` là cờ top-level
trong `config.json`:

| Biến | Mặc định | Ghi chú |
|------|----------|---------|
| `HAL_REALTIME_ENABLED` | `true` | Cổng tổng cho pipeline realtime |
| `wakeword` | `voice.wakeword` trong ROBOT.md khi config còn mới, ngược lại `false` | Cổng wake word top-level trong config file. Khi bật, partial khớp chỉ là tín hiệu tạm: HAL chỉ commit audio buffer sang realtime hoặc forward command sau khi STT **final** xác nhận wake phrase. Transcript được tách thành câu (`.` `!` `?`) và phrase được chấp nhận ở đầu **hoặc cuối** bất kỳ câu nào; xuất hiện giữa câu bị từ chối. Bước xác nhận kiểm lại trên transcript đã ghép mà vẫn còn dấu câu, để bước merge chỉ giữ `\w+` không rút lại cái gate mà một partial đã mở. Các prefix hỗ trợ là `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, `wake up`, áp dụng cho alias chung cố định (`hey autonomous`), device type (`hey lamp`) và tên agent hiện tại (`hey Luna`). Runtime rename chỉ cập nhật alias theo tên agent. Bare name và các prefix khác không mở gate. Một câu bị từ chối sẽ bị bỏ và LED `listening` tạm thời được restore về trạng thái nghỉ bình thường; không bao giờ để hiệu ứng `idle` cố định tiếp tục chạy. Một lượt đã xác nhận mở cửa sổ focus follow-up; lượt trong cửa sổ đó được forward dưới type `voice_followup` mà không cần wake phrase khác. Mọi lượt được phép đều dispatch sang os-server: câu realtime đã nói thành event đồng bộ im lặng `voice_agent_handled`; realtime unavailable, im lặng, lỗi hoặc delegate đi theo đường thường. Nếu realtime tắt, final transcript đã xác nhận đi theo đường os-server thường. Thiếu/`false` giữ nguyên luồng luôn lắng nghe trước gate. Với `config.json` do os-server tạo ra, giá trị khởi tạo lấy từ `voice.wakeword` của body (xem phần Cổng wake word ở trên); config nạp lên mà không có key thì vẫn là `false`. HAL restart sau khi lưu ở local Settings hoặc MQTT `wakeword.gate`. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Số giây idle của cửa sổ focus sau lệnh. Mỗi `voice_command` hoặc `voice_followup` được nhận sẽ refresh cửa sổ. `0` tắt follow-up và buộc mỗi phiên mic phải có wake phrase. Bị bỏ qua khi `wakeword` là false. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Yêu cầu Silero xác nhận có tiếng nói trước khi refresh đồng hồ im lặng kết thúc lượt. RMS vẫn là cổng chặn rẻ chạy trước; đặt `false` để quay về phát hiện im lặng thuần RMS. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Số frame gom lại cho mỗi lần chạy Silero ở bước kiểm đó — Silero tốn ~20 ms/frame trên ARM và LSTM của nó cần hơn một frame 64 ms mới ổn định. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `qwen` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = activity detection thủ công) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Số giây tối đa `receive()` chờ output event kế tiếp trước khi kết thúc lượt im lặng (fallback sang main agent) |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Watchdog im-lặng dùng thay mặc định cho turn có `look` (theo từng turn, qua `extend_recv_timeout()`). Gemini bị ép thinking trên frame dày chữ có thể im >8 s ngay trước khi trả lời — watchdog mặc định giết nhầm mấy turn đó |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Không bao giờ commit turn empty-STT lên model. Giọng thật mà nova-3 miss (câu ngắn) vẫn là voiced nên qua hết guard VAD/Silero, commit audio thô khiến model bịa câu trả lời cho khoảng im lặng (lời chào chung chung, thường kèm tên không ai nói). Khi `true`, mọi turn empty-STT bị bỏ bất kể duration/voicing — im còn hơn trả lời sai. Đặt `false` để quay về đường audio-only gated bằng Silero bên dưới. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Session ngắn hơn ngưỡng này mà không có STT transcript bị coi là nhiễu VAD, không commit lên model. Chỉ xét khi `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Kiểm soát chi phí: khi một turn đến sau ngần này giây im lặng, recycle (rebuild) session **sau** turn đó để turn kế tiếp bỏ phần context mỗi-turn mà provider re-bill trên session sống lâu. Turn sau khoảng nghỉ dài coi như cuộc hội thoại mới; trí nhớ dài hạn vẫn còn nhờ nạp lại `summary.md`. Với Gemini native-audio, bước này bị bỏ qua nếu pre-turn recycle thành công đã làm mới session cho chính idle gap đó. `0` = tắt. Dùng lại đường rebuild của zombie-recovery. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume cùng session Gemini qua reconnect. Mặc định OFF — proxy `campaign-api` không forward đúng resumption handshake nên resume qua nó tạo session zombie (cold reconnect thì chạy được). Chỉ bật khi endpoint hỗ trợ. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `120` | Guard transport cho Gemini: khi lượt nói mới bắt đầu sau ngần này giây idle, rebuild session Gemini **trước khi** stream pre-roll/audio để turn không đụng socket chết vì idle ở proxy/SDK. `0` = tắt. Pre-turn recycle thành công sẽ chặn idle recycle generic sau chính turn đó, nên một idle gap chỉ tạo tối đa một rebuild phục vụ transport/chi phí. |
| `HAL_AGENT_GATEWAY` | `openclaw` | Chọn context manager (cũng đọc từ `agent_runtime` trong config.json) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Key Gemini; fallback về `llm_api_key` |
| `HAL_GEMINI_LIVE_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | |
| `HAL_GEMINI_LIVE_VOICE` | `Kore` | |
| `HAL_GEMINI_LIVE_BASE_URL` | `<llm_base_url>/ws/gemini` | |
| `HAL_GEMINI_THINKING_LEVEL` | `MINIMAL` | `MINIMAL` \| `LOW` \| `MEDIUM` \| `HIGH` — default rẻ (trước là `HIGH`) |
| `HAL_GEMINI_GOOGLE_SEARCH` | `true` | Google Search grounding (chỉ Gemini). Cho model realtime tự trả lời câu dữ liệu công khai theo thời gian thực (thời tiết, tin tức, lookup) ngay trong phiên thay vì delegate. Tính phí theo mỗi grounded request (cộng token); chỉ phát sinh khi Gemini quyết định search. Cũng đặt được qua `realtime.gemini.google_search` trong config.json. |
| `HAL_GEMINI_VISION` | `true` | Tool `look` trong phiên (chỉ Gemini). Cho model realtime chụp một frame camera và trả lời câu hỏi thị giác ("cái này là gì?") ngay trong phiên thay vì delegate. Mặc định bật; chỉ đăng ký khi thiết bị còn có capability `vision`. Cũng đặt được qua `realtime.gemini.vision` trong config.json. |
| `HAL_GEMINI_VISION_MAX_WIDTH` | `768` | Bề rộng tối đa (px) frame được downscale trước khi gửi — giới hạn token ảnh. |
| `HAL_GEMINI_VISION_MIN_INTERVAL_S` | `10` | Chặn chi phí: số giây tối thiểu giữa hai lần **gửi ảnh**. Gọi `look` lặp trong khoảng này (hoặc gọi lần hai trong cùng turn) sẽ xài lại ảnh đã có trong context thay vì gửi ảnh mới. `0` = luôn gửi ảnh mới. |
| `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` | `20` | Tuổi tối đa của frame `look` còn được bàn giao (bằng path) cho main agent khi delegate/timeout fallback để nó xài lại ảnh thay vì chụp lại. `0` tắt guard tuổi (frame vẫn bị clear mỗi turn). |
| `OPENAI_API_KEY` | — | Key OpenAI; fallback về `llm_api_key` |
| `HAL_OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | |
| `HAL_OPENAI_REALTIME_VOICE` | `alloy` | |
| `HAL_OPENAI_REALTIME_BASE_URL` | `<llm_base_url>/ws/openai` | |
| `HAL_OPENAI_REASONING_EFFORT` | `minimal` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — default rẻ (trước là `xhigh`) |
| `DASHSCOPE_API_KEY` | — | Key Qwen (DashScope); **không** fallback về `llm_api_key` — chỉ đọc `realtime.qwen.api_key` khi env trống |
| `HAL_QWEN_REALTIME_BASE_URL` | — | WS host DashScope (`wss://<workspace-host>/api-ws/v1/realtime`); **không** fallback về `llm_base_url` — chỉ đọc `realtime.qwen.base_url` khi env trống |
| `HAL_QWEN_REALTIME_MODEL` | `qwen3.5-omni-plus-realtime` | turbo legacy: không gọi function call, lờ turn context |
| `HAL_QWEN_REALTIME_VOICE` | `Ethan` | 3.5-plus: thêm Serena; chỉ-turbo: Cherry \| Chelsie |
| `HAL_REALTIME_MEMORY_PATH` | `<workspace>/realtime/memory.jsonl` | |
| `HAL_REALTIME_MAX_MEMORY_ENTRIES` / `_TRIM_KEEP` | `1000` / `500` | |
| `HAL_REALTIME_SUMMARIZER_ENABLED` | `true` | |
| `HAL_REALTIME_SUMMARIZER_MODEL` | `claude-haiku-4-5-20251001` | Anthropic Messages API |

## Bản đồ code

| File | Vai trò |
|------|---------|
| `orchestrator.py` | Vòng đời session, tool `delegate_to_main` + `express_emotion` + `look`, stream lượt |
| `voice_agent/base.py` | Agent trừu tượng: contract 2-thread/queue, `receive()` |
| `voice_agent/gemini_live.py` | Provider Gemini Live (IO loop asyncio) |
| `voice_agent/openai_realtime.py` | Provider OpenAI Realtime (sync, connection serialize bằng lock) |
| `voice_agent/qwen_realtime.py` | Provider Qwen Omni Realtime (sync, `websockets.sync.client` thô, schema beta OpenAI qua DashScope; bảng giá `_QWEN_RATES` + log `qwen_usage.log`) |
| `context_manager/{base,openclaw,hermes}.py` | Lắp ráp prompt + memory + skills theo gateway |
| `summarizer.py` | Summarizer memory dựa trên Anthropic |
| `config.py` | Model config provider (`GeminiConfig`, `OpenAIConfig`) |
| `models/`, `enums/` | Kiểu input/output/event, enum provider + gateway |
| `resources/` | System prompt (chung + theo provider) |
| `../voice/voice_service.py` | Tích hợp: stream audio mic, tiêu thụ output, route delegate/handled |
