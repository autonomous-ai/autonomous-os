# Đo phản hồi giọng nói (voice metrics)

Đây là một tracker chạy trên ống telemetry của thiết bị — xem
[`docs/vi/telemetry_vi.md`](telemetry_vi.md) để biết về chính cái ống đó, công
tắc bật/tắt, và các luật mà mọi tracker phải theo.

Chỉ **đo**, không đổi hành vi. Không có gì ở đây thay đổi việc thiết bị nói
gì, nói lúc nào hay im lúc nào — nó ghi lại những gì đã xảy ra để tính được
ba con số từ thiết bị thật:

| KPI | Câu hỏi | Mục tiêu |
|-----|---------|----------|
| **KPI-1** | Tỉ lệ lượt thoại đủ điều kiện được phản hồi trong **3 giây** kể từ lúc phát hiện người dùng nói xong | ≥ 95 % |
| **KPI-2** | Tỉ lệ tình huống suppression mà một câu trả lời **cũ** thực sự bị phát ra | < 5 % |
| **KPI-3** | Tỉ lệ lượt tác vụ thoại đủ điều kiện đã chạy xong | ≥ 85 % |

Mục tiêu KPI-1 và KPI-2 đều **tạm thời**. Mọi khoảng thời gian thô đều được lưu, nên
đổi ngưỡng là việc tính lại từ dữ liệu, không phải đo lại thiết bị.

## Code nằm ở đâu

| Lớp | Đường dẫn | Vai trò |
|-----|-----------|---------|
| Tracker ở HAL | `hal/telemetry/voice_metrics.py` | Interaction id, ack/audio cũ, cohort tác vụ và bằng chứng realtime chạy xong |
| Bằng chứng thực thi ở OS | `system/telemetry/voice_task.go` | Biên lifecycle agent và local intent trả về. |
| Reporter offline | `scripts/report_voice_task_metrics.py` | Ghép journal/AA đã lưu và báo cáo KPI-3, không gọi mạng. |
| Ống dẫn ở HAL | `hal/telemetry/client.py` | Dùng chung: log local, hàng đợi có giới hạn, POST nền |
| Nhận ở OS | `system/server/telemetry/delivery/http/handler.go` | `POST /api/telemetry/event` (loopback/LAN, cùng cổng chặn với `/api/sensing/event`) |
| Ống dẫn ở OS | `system/telemetry/telemetry.go` | Field chung, chống trùng, hàng đợi giới hạn, một sender, log local |
| Transport | `system/lib/analytics` | Client Autonomous Analytics `event_tracking` (chung với web/mobile) |

Code voice chỉ bị đụng đúng ở các biên nó vốn sở hữu: điểm kết thúc câu nói,
dispatch turn, playback bắt đầu/kết thúc, và cử chỉ huỷ. Luật, ngưỡng và hình
dạng event đều nằm trong hai package tracking.

## Interaction id

Một interaction = **một lượt người dùng nói**. `voice_metrics.speech_end()` tạo
`interaction_id` (`vi-<16 hex>`) đúng lúc HAL kết luận người dùng đã nói xong,
rồi id đó đi xuyên realtime, POST sensing (gắn với `runId` os-server trả về từ
`/api/sensing/event`), câu trả lời của agent chính (qua `turn_id` của TTS
queue), filler và playback.

`voice_agent_handled` là **thông báo backend về một interaction đã được trả
lời**, không phải interaction mới: nó gắn vào cùng `interaction_id`, nên lượt
do realtime xử lý chỉ sinh đúng một mẫu KPI-1.

Mỗi **đoạn xếp hàng** của câu trả lời stream là một lần phát được đo riêng: lúc
drain, hook được arm lại theo owner của chính đoạn đó, vì nó phát trên stream do
turn nói trước mở ra.

**Chủ sở hữu là tường minh, không đoán.** Mỗi lần phát mang theo owner đã giành
loa: `run:<turn_id>` cho câu trả lời của agent hoặc filler được arm cho turn đó
(os-server truyền run id xuống cùng filler, và truyền ngược qua field `owner`
của `/api/sensing/filler` cho filler chờ của realtime), và `interaction:<id>`
cho giọng native realtime. Nhánh realtime trả lời bằng TTS (không phải audio
native) cũng gắn tag y hệt — lúc đó os-server chưa cấp run id nào, nên chính
interaction id là tag. Audio không ai nhận là `unknown` và **không bao giờ được tính
là đã phản hồi** — đoán "interaction mở mới nhất" chính là cách một filler cũ bị
tính thành phản hồi cho câu lệnh mới. Số đếm đi kèm mọi dòng interaction ở
`unknown_owner_playbacks`.

Lần phát bị mute cũng phải có owner resolve được thì mới loại interaction với
`speaker_muted`. Thông báo bị mute không có owner hoặc thuộc lượt khác không
được loại lệnh thoại mới nhất. Mỗi đoạn trong hàng đợi mang metadata phân loại
riêng (câu trả lời/filler/hệ thống), không kế thừa loại của speech mở stream.
Snapshot này không thay đổi hành vi feedback hay ngắt phát.

## Thế nào là "đã phản hồi"

Là frame đầu tiên **thực sự được ghi vào audio stream**
(`TTSService._note_audio_written`, gọi từ mọi đường ghi thật: synth streaming,
drain hàng đợi câu, phát WAV cached, và frame native realtime).

`on_speak_start` **không** phải tín hiệu đó và không dùng cho KPI: đường cached
gọi nó TRƯỚC khi lấy stream lock và trước khi ghi byte nào, nên speech bị stop
hoặc lỗi ở giữa vẫn trông như đã phát. HTTP 200 cũng vậy — `speak_queue()` cố
tình trả `True` cho request nó drop, và `deliverTTS` bên os-server có thể bị
mute sau khi đã nhận text.

Ngay cả lần ghi đầu **cũng không phải thời điểm âm thanh ra loa**: đệm ALSA (và
Bluetooth còn hơn) nằm sau đó. Phép đo là "audio đã được giao cho thiết bị",
không phải "âm đã rời driver".

| Loại playback | Modality ghi lại | Cách phân loại |
|---------------|------------------|----------------|
| `native_realtime` | `spoken_answer_realtime` | ghi từ đường native frame — giọng của chính model realtime |
| `realtime_tts` | `spoken_answer_realtime` | câu trả lời dạng text của realtime được tổng hợp qua TTS, nhận diện bằng metadata playback độc lập với `realtime_feedback` |
| `agent_reply` | `spoken_answer` | `realtime_feedback` (chỉ câu trả lời của agent bật cờ này) |
| `waiting_audio` | `waiting_audio` | speech cached interruptible, tức dead-air filler |
| `system_audio` | `acknowledgement_audio` | các câu cached/hệ thống khác |
| `unknown` | — (không bao giờ là ack) | không ai nhận sở hữu lần phát này |

Tiếng chime xác nhận cử chỉ vật lý không phải phản hồi cho lệnh thoại.
Các lần ghi chime bỏ qua hook đo speech: không kế thừa owner của câu trả lời
đang chờ và không dùng mất hook ghi frame đầu tiên của câu trả lời đó.

**Waiting audio ĐƯỢC tính là phản hồi — đây là quyết định, không phải tình cờ.**
Chỉ số này trả lời câu *"thiết bị có cho người dùng biết là đã nghe thấy
không?"*, chứ không phải *"nó có trả lời không?"*. Câu filler ("một giây nhé")
là một biên nhận thật: người dùng hết phải phân vân không biết đèn có nghe
mình. Nên lượt nào có tiếng đầu tiên là filler thì tính là đã phản hồi, dù câu
trả lời thật tới muộn hơn nhiều.

Phải đọc kèm hệ quả: trên lamp hiện tại **mọi** lần phản hồi đo được đều là
`waiting_audio`, nên chỉ số này đang cho biết đèn nói "tôi nghe rồi" nhanh cỡ
nào, KHÔNG phải nó trả lời nhanh cỡ nào. `ack_modality` được lưu ở mọi dòng
chính là để tách hai thứ đó — tách theo nó trước khi trích một con số, và coi
tỉ lệ `waiting_audio` 100 % là một phát hiện về sản phẩm, không phải điểm tốt.

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
`max_duration`, `stt_error`. Đồng hồ chạy từ chính lúc phát hiện đó (lúc vòng
lặp capture break), **không phải** sau `finalize_session` — ráp transcript, cắt
đuôi im lặng và nhận diện người nói chạy ở giữa, tính vào là đổ oan cho thời
gian phản hồi của thiết bị.

## Event

### `voice_metrics_interaction` — snapshot lượt nói (KPI-1 và KPI-3)

| Field | Ý nghĩa |
|-------|---------|
| `interaction_id`, `run_id`, `event_type`, `route` | Định danh + turn đi đường nào (`handled` / `delegated` / fallback / noise-dropped) |
| `speech_end_method` | Cách phát hiện điểm kết thúc |
| `eligible` | `false` khi có `exclusion_reason` |
| `outcome` | `acknowledged` \| `no_ack` \| `excluded` |
| `exclusion_reason` | `rejected_noise`, `rejected_non_user`, `no_transcript`, `not_addressed`, `speaker_muted`, `interrupted_by_user`. `speaker_muted` được ghi đúng lúc loa **từ chối** phát, không phải đọc cờ mute lúc chốt sổ — nếu không, thiết bị được bật tiếng lại trước khi chốt sẽ trông như thiết bị không thèm trả lời |
| `failure_reason` | `dispatch_failed` — lệnh hợp lệ nhưng **không được phục vụ** (POST không tới nơi). Đây *không* phải exclusion: dòng vẫn eligible và bị tính vào KPI. Lệnh os-server tự trả lời (local intent: âm lượng, LED, giờ) **không** phải lỗi — câu trả lời mang interaction id làm owner và được tính là đã phản hồi. |
| `ack_latency_ms` | Quan sát thô, giữ nguyên bất kể kết luận (`null` khi không có gì phát) |
| `ack_modality`, `ack_kind` | Người dùng thực sự nghe thấy cái gì |
| `answer_latency_ms`, `answer_kind` | Lúc nghe được **câu trả lời** (`agent_reply` / `native_realtime` / `realtime_tts`), khác với biên nhận. `null` = đã báo nghe nhưng chưa từng trả lời trong cửa sổ — đó là phát hiện, không phải thiếu dữ liệu |
| `ack_deadline_ms`, `observe_window_ms` | 3000 / 10000 — ngưỡng (tạm thời) đang áp dụng lúc ghi dòng đó |
| `unknown_owner_playbacks` | Số lần phát không ai nhận — audio bị loại khỏi quyết định ack |
| `amends_event_id`, `amendment_reason` | Có ở dòng **đính chính**: route hoặc exclusion tới sau khi verdict đã gửi |

Cửa sổ quan sát **10 giây**, rộng hơn mục tiêu 3 giây một cách có chủ đích: câu
trả lời muộn được ghi kèm latency thật thay vì gộp thành "không trả lời", nên
sau này đổi ngưỡng vẫn tính lại được từ dữ liệu đã lưu.

**Ghi verdict KHÔNG có nghĩa turn đã kết thúc.** Dòng KPI-1 được ghi ở giây thứ
10; agent chính có thể vẫn đang chạy, và lệnh dừng bấm ở giây 12 vẫn phải tìm
thấy turn đó để suppress. Một turn còn *active* cho tới khi im lặng đủ
`TURN_ACTIVE_TTL_MS` (45 giây) — đồng hồ này được reset mỗi lần turn đó phát ra
tiếng, và không bao giờ hết hạn khi audio của turn đó vẫn đang phát — hoặc cho
tới khi bị loại/bị lỗi. Chỉ khi đó nó mới rời mẫu số KPI-2.
Verdict sai sau đó được sửa bằng dòng đính chính.

### `voice_metrics_suppression` — mỗi biên một event (KPI-2)

| Field | Ý nghĩa |
|-------|---------|
| `suppression_reason` | `explicit_stop` (user click) hoặc `auto_supersede` (realtime đã trả lời câu mới hơn) |
| `interaction_id` | Lượt nói kích hoạt biên này |
| `applicable_interactions` | Có bao nhiêu interaction cũ đang bay bị biên này phủ |
| `old_audio_playing_at_boundary` | Lúc đóng mốc có đang phát audio cũ không |
| `stale_observed` | **Tử số KPI-2** |
| `stale_kind`, `stale_started_after_ms` | Audio loại gì, bắt đầu sau biên bao lâu (audio đang phát sẵn cũng tính) |
| `stale_audible_past_grace_ms` | Audio cũ còn nghe được bao lâu sau khi hết grace |
| `stop_to_silence_ms` | Audio cũ còn kêu bao lâu sau biên — ghi thô, kể cả khi nằm trong grace |
| `grace_ms`, `observe_window_ms` | 2000 / 60000 |
| `observation_complete` | `false` khi cửa sổ đóng lúc turn bị suppress vẫn còn nói được — dòng đó **không** phải bằng chứng suppression sạch |
| `unobserved_interactions` | Còn bao nhiêu turn bị suppress vẫn sống lúc đó |

**Stale = còn nghe được sau grace, dù bắt đầu lúc đó hay chỉ kéo dài sang.**
Mỗi khoảng phát được chấm với mọi biên đang mở, nên audio bắt đầu TRƯỚC biên mà
chạy quá grace vẫn bị tính — bản trước chỉ nhìn lần phát *bắt đầu* sau biên nên
cho qua một câu trả lời vẫn còn nói 3 giây sau lệnh dừng.

**Cửa sổ dài, và quan sát dở dang thì phải nói ra.** Câu trả lời của agent chính
có thể tới hàng chục giây sau biên, nên mỗi suppression được theo dõi 60 giây.
Nếu hết cửa sổ mà turn bị suppress vẫn nói được, dòng đó ghi
`observation_complete = false` thay vì "không có stale".
Nếu đã quan sát thấy audio stale thì vẫn là lỗi đã xác nhận, dù quan sát chưa
hoàn tất; quan sát thêm không thể xoá lần phát đó.

**Grace được ghi rõ nguồn, không bịa cho KPI đẹp.** `TTSService.stop()` set stop
event và đánh thức drain queue; worker vẫn phải nhả lock. Comment trong
`hal/drivers/voice/tts/service.py` ghi lại ca xấu đo trên lamp-0c89 mất tới ~4 s
(bình thường dưới 1 s) — chính là lý do bản vá đánh-thức-queue tồn tại.
`STALE_GRACE_MS = 2000` là lựa chọn báo cáo; `stop_to_silence_ms` được lưu thô ở
mọi biên nên lựa chọn đó luôn kiểm tra được và đổi được mà không phải đo lại thiết bị.

Cả hai biên đều đóng mốc **ở HAL**, nơi cả hai phát sinh: cử chỉ huỷ là button
action của HAL, còn `voice_agent_handled` do chính turn dispatcher của HAL gửi đi.

**Chỉ ghi nhận biên khi os-server THẬT SỰ áp dụng.** Response của
`/api/sensing/event` cho post `voice_agent_handled` mang theo `speechSuppressed`
— chính câu trả lời của os-server cho "tôi có lấy loa khỏi turn cũ không". Khi
`OS_REALTIME_SUPERSEDES_MAIN_REPLY` tắt (mặc định) thì không có gì bị suppress,
POST lỗi cũng vậy; cả hai đều không được ghi, vì mẫu số đầy những tình huống
chưa từng suppress gì sẽ làm KPI-2 đẹp miễn phí.

**Mẫu số chỉ đếm turn còn active.** Interaction đã bị loại hoặc đã đóng thì
không thể phát ra câu trả lời cũ, nên không tính là thứ mà biên phải suppress.

## Định nghĩa KPI

**KPI-1 — phản hồi trong 3 giây**

- Mẫu số: các dòng `voice_metrics_interaction` có `eligible = true`, sau khi áp
  dụng đính chính (dòng nào có bản `amends_event_id` cho cùng `interaction_id`
  thì bị bản đính chính thay thế).
- Tử số: trong đó `outcome = 'acknowledged'` và `ack_latency_ms <= 3000`.
- Loại trừ (vẫn báo cáo, không bao giờ vứt): turn bị noise guard loại, turn
  model từ chối vì không phải người dùng, transcript rỗng, câu không nói với
  thiết bị (không wake word / ngoài cửa sổ follow-up), và loa đang mute.
- **Lỗi thì GIỮ LẠI.** Lệnh hợp lệ mà thiết bị không phục vụ được (POST sang
  os-server không tới — `failure_reason = 'dispatch_failed'`) vẫn *eligible* và
  tính là `no_ack`. Loại nó ra là thổi phồng tỉ lệ thành công bằng đúng những
  ca người dùng thấy tệ nhất. Lượt chậm cũng vẫn nằm trong mẫu số.

**KPI-2 — câu trả lời cũ thực sự bị phát**

- Mẫu số: các dòng `voice_metrics_suppression` có `applicable_interactions > 0`
  **và** (`observation_complete = true` **hoặc** `stale_observed = true`). Lần
  phát stale đã xác nhận vẫn nằm trong cả tử số và mẫu số, kể cả khi turn bị
  suppress còn active lúc cửa sổ quan sát đóng. Chỉ dòng quan sát dở dang có
  `stale_observed = false` mới báo riêng như coverage loss — không được gộp
  vào phần "đạt".
- Tử số: trong đó `stale_observed = true`.
- Báo cáo `explicit_stop` và `auto_supersede` **riêng**: hai chính sách khác
  nhau. Supersede tự động chỉ xảy ra khi os-server bật
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1`. **Mặc định trong code là TẮT và thay
  đổi này không đụng tới default** — nhưng phải đọc `.env` của chính body trước
  khi kết luận: lamp xuất xưởng đã **BẬT** sẵn
  (`robots/lamp/rootfs/opt/hal/.env`), nên lamp có sinh mẫu `auto_supersede`.
  Body nào tắt thì mẫu số rỗng và lát cắt đó là **N/A**, không phải 100 %.
- Suppression đúng (`observation_complete = true`, `stale_observed = false`) là **đạt**, không phải "mất câu
  trả lời". Hành động phần cứng của turn bị auto-supersede vẫn hợp lệ theo
  thiết kế; chỉ speech và filler bị bỏ.
- Không có mẫu đủ điều kiện ⇒ **N/A**. Không bao giờ báo 0 % hay 100 % từ tập rỗng.

## KPI-3: chạy xong, không đánh giá làm đúng

“Successfully complete” nghĩa là **tác vụ đã chạy xong, không ghi nhận lỗi kết thúc thực thi**. Agent vẫn có thể trả
lời sai hoặc làm hành động không hiệu quả mà đạt chỉ số thực thi này. KPI không
đánh giá đúng yêu cầu user, kết quả tool, tác động vật lý hay phát hết câu trả lời.

Cohort là lượt nói do HAL ghi nhận; riêng run backend không tạo mẫu thoại.
`voice_metrics_interaction` bổ sung các field độc lập:

| Field | Ý nghĩa |
|-------|---------|
| `task_schema_version` | `1`; dòng cũ chưa có instrumentation tác vụ được báo coverage, không tính là thất bại |
| `task_started_at_ms` | Unix milliseconds lúc phát hiện nói xong; chọn cohort, không dùng tính latency giữa hai process |
| `task_revision` | Revision snapshot tăng dần; lấy lớn nhất theo thiết bị + interaction, kể cả bind run-id muộn |
| `task_eligible` | Độc lập với eligibility ack: mute hoặc ngắt speech không loại tác vụ thực thi |
| `task_eligibility_known` | Đã biết routing/exclusion chưa; báo riêng coverage chưa rõ |
| `task_exclusion_reason` | Loại noise, non-user, transcript rỗng hoặc không hướng tới thiết bị; dispatch lỗi vẫn eligible |

`voice_metrics_task_execution` chứa `schema_version=1`, `run_id`,
`interaction_id`, `outcome` (`completed`, `failed`, `unknown`), `evidence`,
`execution_at_ms` (Unix milliseconds), và boolean `error`. Không chứa nội dung
lỗi, transcript hay kết quả tool.

| Evidence | Outcome và ý nghĩa |
|----------|-------------------|
| `lifecycle_end` | `completed`: runtime xác nhận kết thúc, không có `data.aborted=true` hay `data.error` khác rỗng |
| `lifecycle_end_error` | `failed`: end có aborted hoặc error vẫn là lỗi; không tự suy diễn từ `stopReason` |
| `lifecycle_error`, `lifecycle_end_error`, `chat_error`, `local_intent_error` | `failed`: lỗi thực thi; local intent ghi nhận lỗi API hành động HAL |
| `lifecycle_error_recovered` | `unknown`: recover chưa chứng minh chạy xong; cần end tường minh |
| `local_intent_returned` | `completed`: handler local intent chạy xong, không ghi nhận lỗi hành động HAL; không chứng minh đúng ý user hay tác động vật lý |
| `realtime_turn_done` | `completed`: provider phát TurnDoneEvent và HAL trả về turn đã handled |

Ghép bằng chứng bằng **thiết bị + run_id** hoặc **thiết bị + interaction_id**.
OS cũng ghi run không phải thoại: không tự đưa chúng vào mẫu số. Với
`route=realtime_handled`, chỉ chấp nhận `realtime_turn_done`; bỏ run backend
đồng bộ bộ nhớ. Bất kỳ evidence kết thúc `failed` đã ghép được tới as-of đều giữ nguyên lỗi:
`lifecycle_end` tới sau không xoá được, vì có thể chỉ là cleanup. Nếu không có
failed thì lấy execution timestamp mới nhất không vượt as-of; nếu trùng thời
gian ưu tiên completed hơn unknown. Lỗi recovered là `unknown`, nên end thật
tới sau có thể xác nhận completion. Đây là lỗi kết thúc thực thi, không phải
mọi lần gọi tool: lỗi tool được xử lý có thể recover mà không thành terminal
failure. Dispatch lỗi không có execution evidence được tính failed. `Result.ExecutionFailed`
ghi nhận lỗi API HAL của local intent vốn trước đây chỉ được log; handler trả
về sau lỗi này phát `local_intent_error`, không phát completion.

Horizon báo cáo mặc định tạm thời, có thể cấu hình, là **1800 giây**: mẫu số gồm lượt eligible có
`task_started_at_ms <= as_of_ms - 1800000`. Lượt mới hơn báo riêng là
`fresh_pending_turns`, kể cả đã xong. Lượt đủ tuổi nhưng failed, unknown hoặc
incomplete **vẫn ở mẫu số**; eligibility chưa rõ được gắn cờ thay vì âm thầm bỏ.
Tử số là lượt đủ tuổi có evidence được chọn là `completed`. So tỉ lệ chưa làm
tròn với **85%**; mẫu số rỗng là **N/A** (`kpi3_pct`, `meets_target` bằng JSON `null`).

Đây là horizon để báo cáo, **không phải timeout thực thi**. Cửa sổ ack 10 giây
và TTL speech-active 45 giây đều không kết thúc tác vụ. Completion muộn được
đếm khi chạy lại báo cáo với evidence mới. Phải lấy execution tới thời điểm
as-of, không cắt kết quả theo thời điểm nói cuối của cohort.

## Runbook cho agent: query và report KPI-3

Trong phiên truy cập device đã được phép, xuất cả hai service kèm hostname:

```bash
journalctl -u hal -u os-server --since '7 days ago' -o json --no-pager > /tmp/voice-task-journal.jsonl
```

Copy file về máy bằng quyền truy cập device đã có, rồi chạy từ repo root
(hoặc pipe export vào stdin):

```bash
python3 scripts/report_voice_task_metrics.py voice-task-journal.jsonl > voice-task-report.json
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --now-ms 1789088400000 --settle-seconds 1800 > voice-task-report.json
```

Reporter lọc observation trước khi chọn revision và loss counter, dùng
`__REALTIME_TIMESTAMP` của journal (microseconds), `event_timestamp` của AA
(seconds), hoặc `observed_at_ms` của dòng chuẩn hóa (milliseconds). Dòng sau
cutoff được đếm trong `future_events_excluded`. Export thiếu timestamp vẫn đọc
được nhưng tăng `missing_observation_timestamp_events`; không thể bảo đảm
snapshot lịch sử chính xác, nên phải dùng export đã cắt tại thời điểm cần báo
cáo. CLI đọc JSONL từng dòng và chỉ giữ trạng thái metric, không nạp toàn bộ
journal vào RAM.

`--now-ms` cố định thời điểm as-of Unix milliseconds để tái lập; dùng cutoff
thật của export. `--device <hostname>` bổ sung identity khi dòng thiếu thiết
bị; **không phải bộ lọc thiết bị**. Hỗ trợ journal JSONL (cả ANSI màu và
`MESSAGE` dạng mảng byte của journald) và AA JSONL chứa
`event_name`, `data.user_pseudo_id`, `data.event_params` dạng key/value. Giữ
amendment và execution trong export. Reporter nhóm theo device, khử bản trùng
HAL/OS bằng task revision. ID bắt đầu `vi-smoke-` mặc định bị loại;
`--include-synthetic --settle-seconds 0` chỉ để kiểm tra instrumentation,
không dùng công bố KPI sản phẩm.

Báo interval, as-of, horizon, nguồn (log local hay AA), từng device và tổng
`completed_turns / eligible_mature_turns`, phần trăm, `meets_target`. Kèm
`failed_turns`, `unknown_turns`, `incomplete_turns`, `fresh_pending_turns`,
`ineligible_turns`, số legacy/thiếu start bị loại và eligibility chưa rõ.
Ghi rõ **đã chạy xong; chưa đánh giá làm đúng**. Báo coverage malformed/thiếu
identity và counter mất telemetry; log local có thể chứa event AA bị thiếu.
Reporter lấy max theo device cho `hal_dropped_total`, `hal_failed_total`,
`telemetry_dropped_total`, `telemetry_failed_total`, `unknown_owner_playbacks`,
rồi cộng max các device ở aggregate. Đây là tín hiệu coverage, không phải tổng
mất mát chính xác qua restart/interaction; xem mốc reset trước khi diễn giải.

Dòng INFO `[telemetry] delivered` cho `voice_metrics_*` chứng minh HTTP gửi AA
thành công. `[telemetry] event` chỉ chứng minh ghi local. Cả hai chưa chứng
minh query thấy row trong kho. Muốn xác minh warehouse phải có quyền đọc và
query/export thấy `event_id` cùng interaction/run tương ứng; credential ghi
AA không tự cung cấp API đọc.

## Truy vấn kho dữ liệu

Giả định schema (ghi rõ vì schema kho AA không nằm trong repo này): event vào
bảng `event_tracking` với `event_name`, `event_timestamp`, và `event_params` là
mảng `{key, value}` với `value` lưu dạng chuỗi — đúng hình dạng thiết bị này
gửi lên và web/mobile đang gửi. Nếu kho của bạn trải params thành cột thì sửa
lại hàm trích xuất.

Xem các truy vấn SQL đầy đủ (KPI-1, KPI-2, KPI-3, coverage) trong bản tiếng Anh:
[`docs/voice-metrics.md`](../voice-metrics.md#warehouse-queries).

SQL KPI-3 trong bản Anh thực hiện trích params, lấy revision mới nhất theo
thiết bị + interaction, ghép execution bằng run/interaction, chặn memory-sync
realtime, giữ mọi lỗi terminal và chọn evidence mới nhất khi không có lỗi,
tách fresh/coverage và so ngưỡng 85% chưa làm
tròn. Output realtime dở dang không có `TurnDoneEvent` vẫn incomplete. Tổng KPI
phải lấy tổng completed chia tổng eligible, không lấy trung bình phần trăm.
Reporter không có CLI start-window: file đầu vào quyết định interval; giữ
amendment và execution tail. Dòng legacy thiếu start cần giới hạn khoảng
export để coverage có nghĩa. Identity device/interaction trống phải báo riêng,
không gộp thành device giả. Counter mất telemetry là số cộng dồn theo process,
có thể reset khi restart: xem maxima và mốc reset, không cộng từng event.

## Log local trên thiết bị

Mọi event được ghi log **trước khi** gửi, và lỗi gửi cũng được ghi — bản trên
kho mới là bản có thể thiếu.

```bash
journalctl -u hal -f | grep '\[telemetry\]'        # HAL: mọi event + lỗi POST
journalctl -u hal -f | grep '\[voice-metrics\]'       # quyết định ack / biên / stale
journalctl -u os-server -f | grep '\[telemetry\]'  # os-server: đã gửi, đã drop, đã lỗi
```

Ghép các dòng bằng `interaction_id`, không dựa vào vị trí gần nhau.
`Session END` và dòng `[turn] route=` đều có id này; verdict xuất hiện sau
timer quan sát 10 giây nên có thể xen giữa log của session tiếp theo.
JSON telemetry local của HAL cũng có `event_id` để ghép amendment với event
gốc ngay cả khi chưa bật gửi analytics.

```bash
journalctl -u hal -o cat --no-pager | grep -F 'vi-<interaction-id>'
```

`eligible=false` và `ack_latency_ms=null` là mẫu bị loại; xem
`exclusion_reason`. `eligible=true` và ack null là chưa ghi nhận acknowledge
trong cửa sổ quan sát. Có transcript không đồng nghĩa với đủ điều kiện: cổng
wake-word hoặc model realtime vẫn có thể loại lượt không hướng tới thiết bị.
Transcript chỉ nằm trong log voice local sẵn có, không được thêm vào payload
telemetry.

## Cấu hình

Cả hai giá trị nằm trong `/opt/hal/.env` của body, os-server load lúc khởi
động — đổi kho dữ liệu cho một thiết bị không cần build lại:

| Key | Ý nghĩa |
|-----|---------|
| `AUTONOMOUS_ANALYTICS_URL` | Nơi bắn event — **đồng thời là công tắc bật/tắt**. Rỗng (mặc định) = không có gì rời khỏi thiết bị. Không có endpoint mặc định trong code. |
| `AUTONOMOUS_ANALYTICS_ID` | Key authorization của AA. Thiếu ⇒ báo lỗi gửi rõ ràng (`[telemetry] delivery failed`). |

Cả hai tiến trình (HAL và os-server) đọc cùng một key, đọc mỗi lần gọi — điền
vào rồi restart service là đủ, không cần build lại. Event vẫn ghi log local
trong mọi trường hợp; URL rỗng chỉ bỏ chặng gửi mạng.

Thứ tự phân giải: env của process → `/opt/hal/.env`. Chặng HAL→os-server
(`http://127.0.0.1:5000/api/telemetry/event`) vẫn là hằng loopback như mọi lời
gọi HAL→OS khác.

## Riêng tư

Không bao giờ gửi transcript, text câu trả lời, audio thô hay credential. Chỉ
id, outcome trong danh sách cố định, và các khoảng thời gian. `user_pseudo_id`
là hostname thiết bị, `platform` là `device` (xem `system/lib/analytics`).

## Giới hạn

- **Điểm kết thúc là phát hiện, không phải âm học.** Latency tính từ silence
  clock / STT final, không phải từ lúc sóng âm thật sự dứt.
- **Chưa đo phản hồi bằng hình ảnh** (xem trên).
- **`auto_supersede` tuỳ theo body.** Mặc định trong code của
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY` là TẮT (không đổi ở đây), nhưng lamp tự
  bật trong `.env` của nó; body nào tắt thì lát cắt đó không có mẫu nào.
- **Phát hiện stale ở mức playback, không ở mức mẫu audio.** Nó chấm các khoảng
  phát vượt mốc grace; không nói được còn bao nhiêu mili-giây PCM nằm trong đệm
  phần cứng.
- **Audio đã giao cho driver, không phải âm trong phòng.** Mốc ack là lần ghi
  stream đầu tiên; đệm ALSA/Bluetooth sau đó không đo được.
- **Playback không ai nhận thì để không quy chủ, có chủ đích.** Nó được đếm
  (`unknown_owner_playbacks`) và loại khỏi quyết định ack, thay vì đoán.
- **Cửa sổ 60 giây vẫn có thể hết sớm.** Những dòng đó mang
  `observation_complete = false`. Lần phát stale đã xác nhận vẫn tính là lỗi;
  chỉ dòng chưa quan sát thấy stale mới là coverage loss.
- **State chỉ nằm trong RAM.** HAL restart thì mất các interaction còn trong
  cửa sổ quan sát; những mẫu đó thiếu chứ không sai.
- **Tracker giữ tối đa 32 interaction.** Cái cũ bị đẩy ra trước hạn ghi verdict
  sẽ được báo cáo sớm, gắn cờ `eviction = 'tracker_capacity'`, chứ không bị bỏ
  im — cửa sổ quan sát của nó bị cắt ngắn.
- **Mất mát khi gửi được báo cáo, không giấu**: `telemetry_dropped_total`,
  `telemetry_failed_total`, `hal_dropped_total`, `hal_failed_total` đi kèm mọi event.

## Lệnh kiểm chứng

```bash
go build ./...                                   # os-server + package tracking
go test ./system/telemetry/ ./system/server/telemetry/...
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_voice_metrics.py -q
```

## Kiểm chứng device — 2026-09-11

Đã kiểm chứng trên `lamp-0c89` (`172.168.20.169`), version
`0.1.88-voice-task`, bằng deploy được cho phép và curl smoke request.
Không lưu transcript hoặc toàn bộ journal thiết bị vào repo.

| Quan sát | Định danh và kết quả |
|----------|---------------------|
| Local intent hỏi giờ giả lập | `vi-smoke-c5e84236c9` → `local_intent_returned`, event `vte-RAUD26IPOSIDNMCK3LF4F3PRVJ` |
| Tác vụ tính toán delegate giả lập | `vi-smoke-09bf638baa` → run `device-chat-4-1789090404539` → `lifecycle_end` completed, event `vte-CQ54WS3EVMVLQRQPSP6FPYCITP`; AA delivered lúc 08:33:29 +07 |
| Turn realtime tự nhiên | `vi-85eff37e57264e94` → `realtime_turn_done`, event `task-rt-vi-85eff37e57264e94`; AA delivered lúc 08:34:11 +07 |

Report offline chỉ lấy mẫu giả lập đạt **2/2** với
`--include-synthetic --settle-seconds 0`: chỉ kiểm tra instrumentation, không
chứng minh sản phẩm đạt 85%. Snapshot 08:35 +07 với horizon chuẩn 1800 giây
cho **N/A**, hai lượt fresh pending. Đã xác minh HTTP gửi AA thành công;
chưa thực hiện query đọc warehouse.

Bản rollback trên device: `/tmp/os-server-before-voice-task` và
`/tmp/voice-task-hal-before.tgz`; đây không phải file trong repo.
Kiểm tra local gồm test Go tập trung cho domain, intent, telemetry và handler,
75 test HAL tập trung, 20 unittest reporter. HAL lint dùng test venv tạm dưới
`/tmp` có pyflakes. Không công bố toàn bộ HAL suite xanh:
`test_gemini_generation_complete.py:52` có lỗi baseline nhận `InterruptedOutput`
nhưng chờ `TextOutput`, đã tái hiện cả trên HEAD.

Xem [các lệnh kiểm chứng đầy đủ trong bản Anh](../voice-metrics.md#device-validation--2026-09-11):
focused Go test, 75 HAL pytest, 20 reporter unittest, HAL lint qua venv có
pyflakes và build Linux ARM64 gắn version `0.1.88-voice-task`. Binary ARM64 cuối
đã được cài lên device; os-server restart và active.
