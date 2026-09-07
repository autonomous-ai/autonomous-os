# Đo KPI giọng nói (Voice KPI)

Chỉ **đo**, không đổi hành vi. Không có gì ở đây thay đổi việc thiết bị nói
gì, nói lúc nào hay im lúc nào — nó ghi lại những gì đã xảy ra để tính được
hai con số từ thiết bị thật:

| KPI | Câu hỏi | Mục tiêu |
|-----|---------|----------|
| **KPI-1** | Tỉ lệ lượt thoại đủ điều kiện được phản hồi trong **3 giây** kể từ lúc phát hiện người dùng nói xong | ≥ 95 % |
| **KPI-2** | Tỉ lệ tình huống suppression mà một câu trả lời **cũ** thực sự bị phát ra | < 1 % |

## Code nằm ở đâu

| Lớp | Đường dẫn | Vai trò |
|-----|-----------|---------|
| Tracker ở HAL | `hal/tracking/voice_kpi.py` | Toàn bộ phần đo: interaction id, phát hiện ack, phát hiện audio cũ |
| Ống dẫn ở HAL | `hal/tracking/client.py` | Dùng chung: log local, hàng đợi có giới hạn, POST nền |
| Nhận ở OS | `system/server/tracking/delivery/http/handler.go` | `POST /api/tracking/event` (loopback/LAN, cùng cổng chặn với `/api/sensing/event`) |
| Ống dẫn ở OS | `system/tracking/tracking.go` | Field chung, chống trùng, hàng đợi giới hạn, một sender, log local |
| Transport | `system/lib/analytics` | Client Autonomous Analytics `event_tracking` (chung với web/mobile) |

Code voice chỉ bị đụng đúng ở các biên nó vốn sở hữu: điểm kết thúc câu nói,
dispatch turn, playback bắt đầu/kết thúc, và cử chỉ huỷ. Luật, ngưỡng và hình
dạng event đều nằm trong hai package tracking.

## Interaction id

Một interaction = **một lượt người dùng nói**. `voice_kpi.speech_end()` tạo
`interaction_id` (`vi-<16 hex>`) đúng lúc HAL kết luận người dùng đã nói xong,
rồi id đó đi xuyên realtime, POST sensing (gắn với `runId` os-server trả về từ
`/api/sensing/event`), câu trả lời của agent chính (qua `turn_id` của TTS
queue), filler và playback.

`voice_agent_handled` là **thông báo backend về một interaction đã được trả
lời**, không phải interaction mới: nó gắn vào cùng `interaction_id`, nên lượt
do realtime xử lý chỉ sinh đúng một mẫu KPI-1.

## Thế nào là "đã phản hồi"

Là **frame audio đầu tiên** ra loa — hook `on_speak_start` của `TTSService`,
fire ở frame đầu chứ không phải lúc nhận HTTP. `speak_queue()` cố tình trả
`True` cho request bị drop (turn đã bị thay thế), và `deliverTTS` bên os-server
có thể bị mute sau khi đã nhận text — nên HTTP 200, text đã xếp hàng, model đã
sinh chữ, hay `tts_send` đều KHÔNG chứng minh người dùng nghe được gì.

| Loại playback | Modality ghi lại | Cách phân loại |
|---------------|------------------|----------------|
| `native_realtime` | `spoken_answer_realtime` | `TTSService.native_mode` — giọng của chính model realtime |
| `agent_reply` | `spoken_answer` | `realtime_feedback` (chỉ câu trả lời của agent bật cờ này), kèm `latest_queue_turn_id` |
| `waiting_audio` | `waiting_audio` | speech cached interruptible, tức dead-air filler |
| `system_audio` | `acknowledgement_audio` | các câu cached/hệ thống khác |

**Phản hồi bằng hình ảnh KHÔNG được tính.** LED listening đúng là một tín hiệu
người dùng cảm nhận được, nhưng thời điểm nó thật sự bật chưa được đo ở đây;
tính vào mà không đo là thổi phồng KPI-1. Muốn thêm thì phải đo thật
`_set_emotion_local(EMO_LISTENING)` và ghi `ack_modality = "visual_cue"` tường minh.

## Đồng hồ

Mọi khoảng thời gian đều là hiệu hai lần đọc `time.monotonic()` **trong cùng
tiến trình HAL**. Không bao giờ trừ clock HAL cho clock os-server. `event_timestamp`
(giây) của analytics client không dùng để đo latency — mọi khoảng thời gian là
field mili-giây riêng.

`speech_end_method` ghi cách xác định điểm kết thúc, vì đó là **phát hiện**,
không phải sự thật âm học: `silence_clock`, `tts_started`, `music_started`,
`max_duration`, `stt_error`.

## Event

### `voice_kpi_interaction` — mỗi lượt nói một event (KPI-1)

| Field | Ý nghĩa |
|-------|---------|
| `interaction_id`, `run_id`, `event_type`, `route` | Định danh + turn đi đường nào (`handled` / `delegated` / fallback / noise-dropped) |
| `speech_end_method` | Cách phát hiện điểm kết thúc |
| `eligible` | `false` khi có `exclusion_reason` |
| `outcome` | `acknowledged` \| `no_ack` \| `excluded` |
| `exclusion_reason` | `rejected_noise`, `rejected_non_user`, `no_transcript`, `not_addressed`, `speaker_muted`, `interrupted_by_user` |
| `ack_latency_ms` | Quan sát thô, giữ nguyên bất kể kết luận (`null` khi không có gì phát) |
| `ack_modality`, `ack_kind` | Người dùng thực sự nghe thấy cái gì |
| `ack_deadline_ms`, `observe_window_ms` | 3000 / 10000 — ngưỡng đang áp dụng lúc ghi dòng đó |

Cửa sổ quan sát **10 giây**, rộng hơn mục tiêu 3 giây một cách có chủ đích: câu
trả lời muộn được ghi kèm latency thật thay vì gộp thành "không trả lời", nên
sau này đổi ngưỡng vẫn tính lại được từ dữ liệu đã lưu.

### `voice_kpi_suppression` — mỗi biên một event (KPI-2)

| Field | Ý nghĩa |
|-------|---------|
| `suppression_reason` | `explicit_stop` (user click) hoặc `auto_supersede` (realtime đã trả lời câu mới hơn) |
| `interaction_id` | Lượt nói kích hoạt biên này |
| `applicable_interactions` | Có bao nhiêu interaction cũ đang bay bị biên này phủ |
| `old_audio_playing_at_boundary` | Lúc đóng mốc có đang phát audio cũ không |
| `stale_observed` | **Tử số KPI-2** |
| `stale_kind`, `stale_started_after_ms` | Audio loại gì, bắt đầu sau biên bao lâu |
| `stop_to_silence_ms` | Audio cũ còn kêu bao lâu sau biên — ghi thô, kể cả khi nằm trong grace |
| `grace_ms`, `observe_window_ms` | 2000 / 3000 |

**Grace được ghi rõ nguồn, không bịa cho KPI đẹp.** `TTSService.stop()` set stop
event và đánh thức drain queue; worker vẫn phải nhả lock. Comment trong
`hal/drivers/voice/tts/service.py` ghi lại ca xấu đo trên lamp-0c89 mất tới ~4 s
(bình thường dưới 1 s) — chính là lý do bản vá đánh-thức-queue tồn tại.
`STALE_GRACE_MS = 2000` là lựa chọn báo cáo; `stop_to_silence_ms` được lưu thô ở
mọi biên nên lựa chọn đó luôn kiểm tra được và đổi được mà không phải đo lại thiết bị.

Cả hai biên đều đóng mốc **ở HAL**, nơi cả hai phát sinh: cử chỉ huỷ là button
action của HAL, còn `voice_agent_handled` do chính turn dispatcher của HAL gửi đi.

## Định nghĩa KPI

**KPI-1 — phản hồi trong 3 giây**

- Mẫu số: các dòng `voice_kpi_interaction` có `eligible = true`.
- Tử số: trong đó `outcome = 'acknowledged'` và `ack_latency_ms <= 3000`.
- Loại trừ (vẫn báo cáo, không bao giờ vứt): turn bị noise guard loại, turn
  model từ chối vì không phải người dùng, transcript rỗng, câu không nói với
  thiết bị (không wake word / ngoài cửa sổ follow-up), và loa đang mute. Lượt
  chậm hoặc lỗi mà đủ điều kiện thì **vẫn nằm trong mẫu số**.

**KPI-2 — câu trả lời cũ thực sự bị phát**

- Mẫu số: các dòng `voice_kpi_suppression`.
- Tử số: trong đó `stale_observed = true`.
- Báo cáo `explicit_stop` và `auto_supersede` **riêng**: hai chính sách khác
  nhau. Supersede tự động chỉ xảy ra khi os-server bật
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1`; **mặc định TẮT và thay đổi này không
  đụng tới default**, nên trên body mặc định mẫu số `auto_supersede` sẽ rỗng và
  lát cắt KPI đó là **N/A**, không phải 100 %.
- Suppression đúng (`stale_observed = false`) là **đạt**, không phải "mất câu
  trả lời". Hành động phần cứng của turn bị auto-supersede vẫn hợp lệ theo
  thiết kế; chỉ speech và filler bị bỏ.
- Không có mẫu đủ điều kiện ⇒ **N/A**. Không bao giờ báo 0 % hay 100 % từ tập rỗng.

## Truy vấn kho dữ liệu

Giả định schema (ghi rõ vì schema kho AA không nằm trong repo này): event vào
bảng `event_tracking` với `event_name`, `event_timestamp`, và `event_params` là
mảng `{key, value}` với `value` lưu dạng chuỗi — đúng hình dạng thiết bị này
gửi lên và web/mobile đang gửi. Nếu kho của bạn trải params thành cột thì sửa
lại hàm trích xuất.

Xem các truy vấn SQL đầy đủ (KPI-1, KPI-2, coverage) trong bản tiếng Anh:
[`docs/voice-kpi.md`](../voice-kpi.md#warehouse-queries).

## Log local trên thiết bị

Mọi event được ghi log **trước khi** gửi, và lỗi gửi cũng được ghi — bản trên
kho mới là bản có thể thiếu.

```bash
journalctl -u hal -f | grep '\[tracking\]'        # HAL: mọi event + lỗi POST
journalctl -u hal -f | grep '\[voice-kpi\]'       # quyết định ack / biên / stale
journalctl -u os-server -f | grep '\[tracking\]'  # os-server: đã gửi, đã drop, đã lỗi
```

## Riêng tư

Không bao giờ gửi transcript, text câu trả lời, audio thô hay credential. Chỉ
id, outcome trong danh sách cố định, và các khoảng thời gian. `user_pseudo_id`
là hostname thiết bị, `platform` là `device` (xem `system/lib/analytics`).

## Giới hạn

- **Điểm kết thúc là phát hiện, không phải âm học.** Latency tính từ silence
  clock / STT final, không phải từ lúc sóng âm thật sự dứt.
- **Chưa đo phản hồi bằng hình ảnh** (xem trên).
- **`auto_supersede` rỗng trên body mặc định** — `OS_REALTIME_SUPERSEDES_MAIN_REPLY`
  mặc định TẮT và không bị thay đổi.
- **Phát hiện stale ở mức playback, không ở mức mẫu audio.** Nó thấy audio bắt
  đầu hoặc còn chạy sau biên; không nói được bao nhiêu mili-giây audio cũ đã
  lọt tai ngoài `stop_to_silence_ms`.
- **Filler và giọng realtime được quy về interaction mở gần nhất**, không phải
  bằng id tường minh — hai đường đó không mang turn id. Câu trả lời của agent
  thì quy đúng theo run id.
- **State chỉ nằm trong RAM.** HAL restart thì mất các interaction còn trong
  cửa sổ quan sát; những mẫu đó thiếu chứ không sai.
- **Mất mát khi gửi được báo cáo, không giấu**: `tracking_dropped_total`,
  `tracking_failed_total`, `hal_dropped_total`, `hal_failed_total` đi kèm mọi event.

## Lệnh kiểm chứng

```bash
go build ./...                                   # os-server + package tracking
go test ./system/tracking/ ./system/server/tracking/...
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_voice_kpi.py -q
```
