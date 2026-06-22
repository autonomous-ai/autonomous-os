# Hành vi Cảm nhận (Sensing Behavior)

Cách Lamp phản ứng với thế giới xung quanh — triết lý và cơ chế đằng sau từng loại sự kiện cảm nhận.

Lamp là một sinh vật sống. Nó không "xử lý dữ liệu cảm biến" — nó *trải nghiệm* mọi thứ. Tài liệu này mô tả cách trải nghiệm đó được triển khai.

## Tổng quan kiến trúc

```
HAL (Python)          Lamp server (Go)             OpenClaw agent
─────────────────        ─────────────────────        ──────────────
Mic/Camera          →    SensingHandler               LLM
Phát hiện sự kiện        - drop nếu agent bận         - gọi /emotion
Áp dụng tracker logic    - forward lên agent          - gọi /servo
Gửi POST                                              - nói hoặc NO_REPLY
/sensing/event
```

HAL sở hữu logic tracker theo từng type (sound escalation, motion filtering). Go là người gác cổng — drop event nếu agent bận, sau đó forward. Agent quyết định *cách* phản ứng, bị ràng buộc bởi `SOUL.md`.

---

## Âm thanh (Sound)

### Cơ chế hoạt động

HAL bắn một sound event cho mỗi audio sample vượt ngưỡng `SOUND_RMS_THRESHOLD` — có thể nhiều lần mỗi giây. Python-side **sound tracker** (`os/hal/drivers/sensing/perceptions/sound.py`) áp dụng dedup và escalation trước khi forward lên Go. Go chỉ nhận các event đã pass và forward thẳng lên agent.

### Hành vi leo thang (Escalation)

| Giai đoạn | Agent nhận | Phản ứng của agent |
|---|---|---|
| Lần 1 | `... — occurrence 1` | `/emotion shock` (0.8), im lặng |
| Lần 2 | `... — occurrence 2` | `/emotion curious` (0.7), im lặng |
| Lần 3+ | `... — persistent (occurrence 3)` | `/emotion curious` (0.9), nói 1 lần |
| Sau khi nói | Python drop (suppress 3 phút) | Không có gì đến agent |
| Im lặng 2 phút | Window reset | Trở về lần 1 |

Ví dụ: một con chó nghe tiếng động — nó nhìn lên (lần 1), tiếp tục theo dõi (lần 2), rồi sủa một lần nếu tiếng ồn kéo dài (lần 3+). Sau khi sủa thì không sủa tiếp.

### Hằng số (`sound.py`)

```python
_DEDUPE_INTERVAL_S    = 15.0   # tối đa 1 event forwarded mỗi 15s
_WINDOW_DURATION_S    = 120.0  # im lặng lâu hơn thế này thì reset counter
_PERSISTENT_AFTER     = 3      # nói sau bao nhiêu lần
_SUPPRESS_DURATION_S  = 180.0  # suppress sau khi đã nói (3 phút)
```

### Điều chỉnh (Tuning)

| Triệu chứng | Fix |
|---|---|
| Lamp nói quá nhanh | Tăng `_PERSISTENT_AFTER` (3 → 5) |
| Lamp không bao giờ nói dù ồn kéo dài | Giảm `_PERSISTENT_AFTER` (3 → 2) |
| Quá nhiều turn sound trên Flow Monitor | Tăng `_DEDUPE_INTERVAL_S` (15 → 30) |
| Lamp im quá lâu sau khi đã nói | Giảm `_SUPPRESS_DURATION_S` (180 → 60) |
| Lamp phản ứng với tiếng ồn cũ sau khi im lặng | Giảm `_WINDOW_DURATION_S` (120 → 60) |

### Xem trên Flow Monitor

Python đẩy `sound_tracker` events trực tiếp vào monitor bus qua `POST /api/monitor/event`. Chúng hiện trên Flow Monitor cạnh `sensing_input` turn:

```json
{ "action": "silent",    "occurrence": 1 }  // lần 1 hoặc 2 — forwarded, im lặng
{ "action": "persistent","occurrence": 3 }  // lần 3+ — agent sẽ nói
{ "action": "drop" }                        // dedup hoặc suppress — không forward
```

---

## Hiện diện (Presence)

### Vào phòng (`presence.enter`)

Luôn trigger phản ứng đầy đủ — không có ngoại lệ. Agent **phải** làm cả ba:

1. `/emotion greeting` (0.9) với chủ nhà — `/emotion curious` (0.8) với người lạ
2. Với chủ nhà: `/servo/aim {"direction": "user"}` rồi `/servo/track {"target": ["person"]}` — aim xoay camera về phía user trước (~2s), sau đó vision tracker lock vào người và tự bám theo khi user di chuyển trong phòng. Người lạ: `/servo/play {"recording": "scanning"}` (không auto-follow — thận trọng)
3. Nói: chào ấm áp với chủ nhà (gọi tên), thận trọng với người lạ

HAL xử lý cooldown. Nếu event đã đến agent thì đủ thời gian rồi — phản ứng đầy đủ.

#### Quay lại sau khi vắng lâu (chỉ chủ nhà)

Với mỗi `presence.enter` của chủ nhà, sensing handler chèn block `[presence_context: {"last_leave_age_min": N, "current_hour": H}]` vào message trước khi forward sang agent. `last_leave_age_min` được tính từ row `leave` gần nhất trong wellbeing log, quét tối đa 3 ngày gần đây (`wellbeing.LastActionTS`); giá trị `-1` nghĩa là không tìm thấy `leave` nào trong khoảng đó.

`sensing/SKILL.md` đọc block này và **chuyển sang câu chào "quay lại sau khi vắng lâu"** khi cả ba điều kiện đúng:

1. `last_leave_age_min >= 240` (≥4 giờ) — gap ngắn hơn giữ câu chào thường.
2. `current_hour` ngoài `[5, 11)` — khung sáng do route `morning_greeting` của `wellbeing/SKILL.md` quản (fire khi `motion.activity` đầu tiên trong ngày), nên overlay "vắng lâu" ở khung này sẽ chào trùng.
3. `last_leave_age_min != -1` — không có row `leave` thật trước đó thì framing "welcome back" vô nghĩa.

HW marker (emotion `greeting` + servo aim+track) giữ nguyên; chỉ câu thoại đổi — acknowledge gap mà không định lượng (kiểu "lâu nay không thấy", chứ không "đi 5h 17m"). Người lạ không có quan hệ ổn định nên `BuildPresenceContext` bỏ qua user `unknown` và path chào thận trọng hiện tại giữ nguyên.

### Ra khỏi phòng (`presence.leave`)

Agent gọi `/emotion idle` (0.4), fire `/servo/track/stop` để thả follow nếu đang chạy từ `presence.enter` trước đó, và trả lời **NO_REPLY** (im lặng — không TTS). Tránh vòng lặp ồn ào khi người ra vào liên tục. Agent vẫn xử lý event nội bộ để cancel wellbeing crons và ghi daily log.

### Vắng mặt lâu (`presence.away`)

Được gửi tự động bởi `PresenceService` của HAL khi **không phát hiện chuyển động trong 15 phút** (sau khi đã dim đèn ở phút thứ 5). Lúc này đèn đã tắt — agent chỉ cần **thông báo đi ngủ** qua TTS và Telegram.

Agent gọi `/emotion sleepy` (0.8), fire `/servo/track/stop` để thả follow cũ còn sót, và nói lời chúc ngủ ngon ấm áp (ví dụ "Không có ai xung quanh… Lamp đi ngủ đây. Chúc ngủ ngon!"). Đây là hành động cuối cùng trước khi Lamp hoàn toàn idle.

Timeline tự động điều khiển presence:
1. **5 phút không chuyển động** → đèn dim xuống 20% (tự động, không cần agent)
2. **15 phút không chuyển động** → tắt đèn + gửi event `presence.away` → agent thông báo đi ngủ

HAL quản lý việc điều khiển đèn; agent chỉ xử lý thông báo bằng giọng nói. Nếu người dùng quay lại (phát hiện chuyển động), đèn tự phục hồi và event `presence.enter` được kích hoạt.

State machine tự điều khiển này được gate theo capability `presence`: nguồn `on_motion()` duy nhất của nó là vòng people-perception (processor face/motion/emotion), mà HAL chỉ chạy khi device khai báo `presence`. Device **không** có `presence` sẽ khởi tạo state machine ở trạng thái disabled, nên không bao giờ bị timeout sang AWAY một cách sai lệch. Lamp khai báo `presence: required` nên timeline ở trên luôn áp dụng.

---

## Chuyển động (Motion)

Chỉ chuyển động lớn được forward — HAL lọc và không gửi chuyển động nhỏ lên Go.

**Chuyển động lớn**: `/emotion curious` (0.7) + `/servo/play {"recording": "scanning"}` + nói phản ứng tò mò (ví dụ "What was that?", "Whoa, moving so much!"). Có thể kèm ảnh camera để agent thấy ngữ cảnh.

---

## Tư thế (RULA — sampling thầm lặng, gắn vào `motion.activity`)

HAL stream từng frame camera lên dlbackend `/api/dl/pose-estimation/ws` và nhận RULA breakdown từng frame (whole-body score + `risk_level` + `body_scores` + `*_angle` cho `neck / trunk / upper_arm / lower_arm / wrist`, mỗi bên trái/phải). `PosePerception` throttle thành **một sample mỗi `POSE_SAMPLE_INTERVAL_S` (default 60s)** vào tumbling window + JSONL theo ngày tại `/tmp/hal-sensing-snapshots/sensing_pose/samples_YYYY-MM-DD.jsonl`. **Không emit event trực tiếp** — `MotionPerception` check `is_window_complete()` mỗi tick và gắn aggregate vào `motion.activity` kế tiếp khi gate đỏ.

### Tumbling window (lúc nào summary được inject)

Window mở khi **motion flush đầu tiên có label sedentary** — `MotionPerception` gọi `pose.start_window()` ngay lúc thấy bất kỳ label sedentary nào (`using computer`, `writing`, `reading book`, …). Pose samples đã đến trước thời điểm đó (vd user đang đứng/stretching nhưng vẫn present) bị clear, để bad_ratio chỉ phản ánh giai đoạn ngồi. Sau khi mở, window chạy **chỉ theo thời gian**: stay open đủ `POSE_WINDOW_DURATION_S` bất kể user có break sedentary giữa chừng — stretch break không stop clock, chỉ làm bad_ratio honest hơn. Default **600 s = 10 phút** test; đổi 3600 cho production — 1 biến duy nhất, không có nhánh code test/prod.

Khi window complete, `MotionPerception` ra đúng 1 quyết định và **luôn gọi `reset_window()`** — không carry-over samples cũ. Nếu user vẫn sedentary ở flush kế tiếp, `start_window()` mở cycle mới ngay; nếu không, window stay unanchored đến lần sedentary tiếp theo. Detection miss (dlbackend không trả `ergo`, bị che, low confidence) không kéo dài window; chỉ làm giảm số sample trong window.

Sample được tính là **bad** khi **một trong hai**:

- whole-body `risk_level >= 3` (medium/high), **hoặc**
- bất kỳ region đơn nào (bên L **hoặc** R) có sub-score `>= POSE_REGION_HIGH_SUBSCORE` (default `4`)

Vế thứ hai bắt được case "tech neck" (rướn cổ về màn hình) khi RULA tổng vẫn "low" vì lưng+tay OK nhưng riêng cổ rõ ràng tệ.

Inject fire khi **tất cả** điều kiện sau đúng:

- window complete (`time.time() - _window_start_ts >= POSE_WINDOW_DURATION_S`)
- user vẫn sedentary ở flush này (`has_sedentary` True — không nag giữa stretch break)
- samples trong window `>= POSE_WINDOW_MIN_SAMPLES` (default `3`) — noise floor, bỏ qua window mà dlbackend miss quá nhiều frame
- `bad_ratio >= POSE_BAD_RATIO` (default **0.6**)

Không có gate "minimum sedentary streak" riêng và không có "nudge cooldown" riêng — window chính là rhythm. Window-start yêu cầu sedentary nên khi complete, user đã ở máy tính ít nhất `POSE_WINDOW_DURATION_S`; unconditional reset sau mỗi cycle khiến lần fire kế tiếp tự nhiên cách 1 window.

Khi inject fire, cả per-label dedup check **lẫn** global event cooldown (`MOTION_EVENT_COOLDOWN_S`, default 360 s — sàn giới hạn các `motion.activity` thông thường) đều được **bypass** cho event đó: posture nudge là tín hiệu khác hẳn "user vẫn đang dùng máy tính", và mất nó vì dedup hay cooldown sẽ phải chờ thêm cả một window đầy đủ trước khi có cơ hội tiếp.

Lifecycle window:
1. **Open** — `pose.start_window()` gọi từ motion-side khi label sedentary đầu tiên xuất hiện. Clear samples pre-window và anchor `_window_start_ts = now`. Idempotent — call thêm khi window đã mở là no-op.
2. **Reset** — `pose.reset_window()` gọi từ motion-side sau mỗi window complete (fire hay no-fire). Clear samples và unanchor. Window mới chỉ mở khi sedentary lại được observe.
3. **Presence-loss reset** — `pose.py:_check_impl` cũng reset window khi presence drop, để user đi mất giữa cycle không leak samples cũ sang session sau.

### Snapshot bucketed theo từng window

Mỗi sample ghi 1 JPEG có overlay skeleton + nhãn RULA vào **bucket dir của window hiện tại**: `/tmp/hal-sensing-snapshots/sensing_pose/buckets/<window_start_int>/<sample_ts_int>_<score>.jpg`. Tên file kèm ergo score → bucket tự mô tả trên disk, không cần đọc metadata.

Khi `reset_window()` chạy:

- nếu `bad_ratio >= POSE_BAD_RATIO` (window thật sự fire nudge) → bucket được finalize: tạo manifest `bucket.json` (samples + RULA hai bên + danh sách `worst_snapshots`) + marker `.kept`. Bucket kept giữ tới `POSE_BUCKET_KEEP_S` (default **2 ngày**).
- ngược lại → bucket dir bị xoá ngay. Không nudge thì không cần lưu bằng chứng.

Pruner sweep:

- bucket kept cũ hơn `POSE_BUCKET_KEEP_S`, và
- bucket orphan (không có `.kept`) cũ hơn `2 × POSE_WINDOW_DURATION_S` — đây là bucket mà `reset_window` chưa kịp chạy (process bị kill giữa window).

Nếu tổng size vẫn vượt `POSE_SNAPSHOT_MAX_BYTES` (default 50 MB), kept bucket cũ nhất bị drop trước.

Bốn endpoint:

| Endpoint | Trả về |
|---|---|
| `GET /sensing/pose-snapshot` | JPEG mới nhất across tất cả bucket dir (back-compat cho ô preview live trên monitor) |
| `GET /sensing/pose-snapshot/{ts}` | JPEG annotated của sample đó. Scan mọi bucket tìm `<ts>_*.jpg`. 404 khi bucket đã bị prune |
| `GET /sensing/pose-bucket/{bid}` | Manifest `bucket.json` — samples, RULA hai bên, summary, `worst_snapshots[]` |
| `GET /sensing/pose-bucket/{bid}/img/{filename}` | Frame cụ thể từ kept bucket (dùng cho popup Flow Monitor) |

Sensing tab live trên monitor render thumbnail từng row sample (lazy-loaded, click → lightbox). Flow Monitor turn card show 3-tile strip (motion frame + 2 worst pose) + nút "LOAD MORE · pose bucket" → `PoseBucketModal` fetch `bucket.json` và render full table.

### Surface vào `motion.activity`

Khi window fire, `motion.py` append 2 marker vào message:

```
[pose_bucket: <window_start_int>]
[pose_worst: <fn1>,<fn2>,<fn3>]
```

Cả 2 marker đều bị strip trước khi message tới LLM (mirror `[snapshot:]`); agent chỉ thấy `[posture_summary: ...]` ngôn ngữ tự nhiên. Text gốc (kèm marker) được giữ trong `sensing_input` JSONL → Flow Monitor đọc được bucket id mà không cần side channel.

### `/dm` tự đính ảnh (Telegram)

Khi agent quyết định nudge qua `/dm`, SSE handler của Lamp gọi `ConsumePoseBucketRun(runID)` (mirror `ConsumeGuardRun`). Nếu run có bucket stash, các `worst_snapshots` filenames được resolve thành path tuyệt đối dưới `/tmp/hal-sensing-snapshots/sensing_pose/buckets/<bid>/` rồi gửi Telegram qua `sendMediaGroup` — caption nằm trên photo đầu, còn lại hiển thị thành gallery. Agent không biết bất kỳ file path nào; việc đính ảnh hoàn toàn do Lamp quyết định dựa trên việc `motion.activity` gốc có mang bucket hay không.

### Workaround sign góc (tạm thời)

`signed_flexion_angle` ở dlbackend hiện trả về dấu ngược với docstring ("Positive = forward flexion") — user chúi cổ rõ ràng lại ra **góc âm**, không phải dương. HAL negate `upper_arm_angle`, `neck_angle`, `trunk_angle` khi nhận từ dlbackend (`POSE_FLIP_DLBACKEND_ANGLE_SIGN=True`, mặc định bật) để bảng monitor + JSONL khớp thực tế. `lower_arm_angle` unsigned nên bỏ qua. RULA score đã dùng `abs(angle)` nên risk/score không đổi dù theo convention nào. **Revert** bằng cách set flag `False` (hoặc xóa `_flip_signed_angles`) ngay khi dlbackend ship fix upstream.

---

## Ánh sáng (`light.level`)

Thay đổi ánh sáng môi trường được forward khi vượt `LIGHT_CHANGE_THRESHOLD`. Không cần nói — agent điều chỉnh LED hoặc biểu đạt cảm xúc theo ngữ cảnh (ví dụ `/emotion sleepy` khi đèn tắt).

---

## Chế độ canh gác (Guard Mode)

Khi guard mode được bật (`guard_mode: true` trong config), Lamp trở thành **chó canh gác cảnh giác** — phản ứng mạnh mẽ với người lạ và broadcast alert lên Telegram.

### Luồng xử lý
1. Sự kiện `presence.enter` hoặc `motion` đến khi `guard_mode: true`.
2. Go handler gắn tag `[guard-active]` và đánh dấu runID là guard run (kèm snapshot path). Nếu `guard_instruction` có trong config, nó được thêm vào dưới dạng `[guard-instruction: ...]`.
3. Agent xử lý event — emotion **mạnh mẽ** (shock + curious), servo, TTS, cộng thêm custom instruction nếu có (vd: play nhạc, flash LED).

### Cảm xúc Guard Mode (dramatic)

Khi guard mode bật, stranger/motion events trigger cảm xúc **mạnh hơn nhiều** so với sensing thường:

| Guard event | HW markers | Voice |
|---|---|---|
| Stranger detected | `shock` (1.0) → `curious` (0.9) + servo shock | Hoảng sợ, giật mình, nghi ngờ |
| Motion (unknown) | `shock` (0.9) → `curious` (0.8) + servo scanning | Lo lắng, cảnh giác |
| Stranger left | `curious` (0.7) + scanning | Báo cáo đã rời đi, vẫn cảnh giác |
| Owner/friend về | `greeting` (0.9) + servo aim | Chào + kể lại chuyện gì xảy ra + hỏi tắt guard |

**Lời nói cũng phải đầy cảm xúc** — không phải báo cáo khô khan. Agent phải thể hiện sợ hãi, nghi ngờ, run rẩy thật sự.
4. Khi agent response trả về (SSE lifecycle end), Go SSE handler phát hiện guard run.
5. Text tự nhiên của agent + ảnh camera được gửi thẳng qua **Telegram Bot API** (`sendPhoto`) đến tất cả Telegram chat.
6. Delivery 100% đáng tin — bypass hoàn toàn OpenClaw agent.

### Custom guard instruction
Chủ nhà có thể đưa instruction tùy chỉnh khi bật guard mode (vd: "play tiếng rùn rợn khi có người lạ"). Instruction được lưu trong `guard_instruction` trong config và inject vào mỗi guard sensing event dưới dạng `[guard-instruction: ...]`. Agent sẽ thực hiện instruction này bằng các skill có sẵn (music, LED, v.v.).

### Tại sao approach này?
Sau khi thử 6 approaches khác nhau, hybrid này đáng tin nhất:
- **Agent viết message** → tự nhiên, có ngữ cảnh, có tính cách
- **Go side delivery** → Telegram Bot API trực tiếp, đảm bảo gửi, không rủi ro NO_REPLY
- **Agent thực hiện custom guard instruction** → chủ nhà có thể kết hợp guard mode với skill bất kỳ (music, LED, v.v.)

### Quá trình thử nghiệm (2026-04-07)
| # | Approach | Tại sao fail |
|---|----------|--------------|
| 1 | `BroadcastAlert` qua WS `chat.send` RPC | `chat.send` đi qua agent → 2/3 NO_REPLY |
| 2 | Agent-driven qua tag `[guard-active]` | Haiku bỏ qua SKILL instruction (chôn ở dòng 222) |
| 3 | Đưa instruction lên đầu SKILL.md | Haiku vẫn bỏ qua |
| 4 | Go-side template + `BroadcastAlert` | Agent nhận ra `sender: node-host` → ignore. Không có ảnh |
| 5 | Agent-driven + ép buộc trong SOUL.md | Tốt hơn nhưng không 100%. Lỗi token |
| 6 | **Hook agent response + Telegram Bot API** | ✅ Agent viết tự nhiên, Go gửi 100% |

> **Ghi chú:** `BroadcastAlert` (WS RPC) đã bị xóa. Tất cả broadcast giờ dùng `Broadcast()` gửi trực tiếp qua Telegram Bot API.

### Cảnh báo thủ công
Vẫn có thể gửi cảnh báo thủ công qua `POST /api/guard/alert` với message và ảnh tùy chọn. Giờ dùng `Broadcast()` (Bot API trực tiếp) thay vì `BroadcastAlert` cũ.

Trường hợp sử dụng: Lamp hoạt động như trợ lý an ninh nhà. Khi chủ nhà rời đi và bật guard mode, mọi sự hiện diện hoặc chuyển động được báo cáo đến Telegram với message có cảm xúc và nhận biết ngữ cảnh.

---

## Theo dõi người lạ (Stranger Visit Tracking)

HAL (port 5001) theo dõi số lần mỗi stranger đã xuất hiện:

- Mỗi sự kiện `presence.enter` chứa stranger ID (ví dụ `stranger_5`), số lần xuất hiện được tăng lên.
- Stats bao gồm `count`, `first_seen`, và `last_seen` timestamps cho mỗi stranger.
- Lưu trữ tại thư mục data của HAL (giữ qua restart).
- Truy vấn stats qua `GET http://127.0.0.1:5001/face/stranger-stats`.

### Prompt enroll khi quen mặt (familiar-stranger)

Khi visit count của một stranger lần đầu chạm ngưỡng (`_FAMILIAR_VISIT_THRESHOLD = 2`, xem `os/hal/drivers/sensing/perceptions/processors/facerecognizer.py`), HAL:

1. Lưu raw frame hiện tại ra `<STRANGERS_DIR>/snapshots/<stranger_id>_<ts_ms>.jpg`.
2. Thêm hint vào message `presence.enter` đang gửi:
   `(familiar stranger <stranger_id> — seen 2 times, ask user if they want to remember this face; image saved at <path>)`

Skill `face-enroll` (phía Lamp) parse hint đó và nói trực tiếp với người trước cam: "I've seen you 2 times now — mind if I remember you? What's your name?" (skill chạy bằng tiếng Anh, agent tự dịch nếu cần). Khi có tên trả về thì gọi `POST /face/enroll` với image path đã lưu. Nếu từ chối, skill chỉ ghi nhận rồi dừng; threshold là one-shot (`count == 2`), nên cùng `stranger_id` đó sẽ KHÔNG bị lelamp prompt lại lần nữa. Count vượt qua 2 không re-fire — lúc đó stranger hoặc đã được enroll (không còn là stranger) hoặc đã chủ động từ chối.

---

## Chăm sóc sức khỏe (Wellbeing — Nhắc uống nước + Nghỉ ngơi, AI-Driven)

Lamp chủ động chăm sóc sức khỏe người dùng bằng cron jobs do AI agent tự quản lý qua OpenClaw. Thay vì timer cứng, agent tự quyết interval dựa trên khoa học và thói quen user.

### Cơ chế hoạt động (event-driven — không cron)

Wellbeing hoạt động **event-driven**. **KHÔNG còn cron wellbeing** nào. Mỗi khi nhận `motion.activity`, agent log hoạt động và đọc lại history gần đây để quyết định có cần nhắc hay không.

**Activity JSONL theo từng user** tại `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` — mỗi dòng là 1 transition:

```jsonc
{"ts": 1776658657.23, "seq": 42, "hour": 11, "action": "sedentary", "notes": ""}
```

`action` values:

| Action | Do ai ghi | Mục đích |
|---|---|---|
| `drink`, `break` | **HAL** (`motion.py` POST `/api/wellbeing/log` ngay trước khi fire `motion.activity`) | Reset point cho nudge timer tương ứng |
| `using computer`, `writing`, `texting`, `reading book`, `reading newspaper`, `drawing`, `playing controller` | **HAL** (`motion.py`, cùng đường đó) | Timeline + nudge phrasing. **KHÔNG** phải reset point. |
| `enter`, `leave` | **HAL** (`FaceRecognizer._post_wellbeing`, gọi từ `_check_impl` khi fresh detection và `_check_leaves` khi forget hết) | Session boundary — mỗi friend có timeline riêng; stranger gộp chung vào 1 timeline `"unknown"` duy nhất qua flag `_any_stranger_logged` (1 enter khi stranger đầu xuất hiện, 1 leave khi stranger cuối cùng forget). |
| `nudge_hydration`, `nudge_break` | Agent (sau khi nhắc) | Ghi lại thời điểm Lamp nhắc — hiện lên timeline. Chỉ agent biết khi nào nó thực sự nói, nên chỉ agent ghi entry này. |

**Dedup nằm ở 2 nơi.**

*Activity dedup (window 5 phút).* `os/hal/drivers/sensing/perceptions/motion.py` giữ `_last_sent_key = (current_user, frozenset(labels))` và `_last_sent_ts`, trong đó `labels` khớp với outbound message (bucket names cho drink/break, raw Kinetics labels cho sedentary). Trước khi gửi `motion.activity` **và trước khi POST các row tới `/api/wellbeing/log`**, nếu key không đổi **và** khoảng cách từ lần gửi cuối chưa vượt `MOTION_DEDUP_WINDOW_S = 300` giây (5 phút) → drop cả chu kỳ. Nên `eating burger → eating cake` gộp thành cùng key `break` và bị drop, còn `writing → drawing` lật key (sedentary giữ raw) nên pass qua.

- Đổi user (owner→owner, owner→unknown, unknown→owner) lật key ngay → event pass qua.
- Stranger khác nhau (`stranger_46` → `stranger_54`) đều collapse về `"unknown"` qua `FaceRecognizer.current_user()` → đổi stranger không phá dedup.
- Sau 5 phút cùng state, event tiếp theo vẫn pass — để Lamp agent "thức dậy" định kỳ chạy threshold check.

*Presence dedup (safety net tại log).* `os/services/lib/wellbeing/wellbeing.go::LogForUser` scan file JSONL của user từ dưới lên để tìm **presence row** gần nhất (enter/leave, bỏ qua activity rows xen giữa). `enter` khi presence cuối đã là `enter` → drop; `leave` khi chưa có session mở → drop. Vì HAL đã fire 1 enter / 1 session thật (per-friend + unknown gộp), layer này chỉ là safety net cho restart / out-of-order edge case, không load-bearing.

**Retention:** 30 ngày. Goroutine trong `wellbeing.Init()` xoá file cũ hàng ngày.

### Khi nhận `motion.activity` — agent làm gì

Tới thời điểm agent thấy event, HAL đã tự log mọi label activity rồi (xem bảng "Do ai ghi" phía trên). Agent chỉ còn đọc history, quyết định nhắc, và nếu nhắc thì log `nudge_*`.

1. **Đọc history gần đây** qua `GET /api/openclaw/wellbeing-history?user={current_user}&last=50`.
2. **Tính delta** từ log, dùng **điểm reset gần nhất** cho mỗi loại:

   ```
   hydration_reset = max(last drink entry, last enter entry, last nudge_hydration entry)
   break_reset     = max(last break entry, last enter entry, last nudge_break entry)
   ```

   Ba điểm reset: hoạt động thực tế (`drink`/`break`), mới vào session (`enter`), hoặc lần nhắc gần nhất (`nudge_*`). Nudge reset là điểm mấu chốt: sau khi Lamp nhắc, delta về 0 → lần nhắc tiếp theo chỉ fire sau 1 threshold window nữa — không cần cooldown constant riêng.
3. **Chọn path** (tối đa 1 phản hồi/turn, reaction ưu tiên hơn nudge — user vừa làm rồi, nudge tiếp sẽ thấy vô duyên):
   - **Reaction** — labels có `drink` hoặc `break` → nói 1–3 câu acknowledge ngắn (kiểu "quao uống nước thứ 3 hôm nay rồi đó", playful/ngạc nhiên, KHÔNG phải lời khuyên). Dùng `count_today` ("lần thứ N hôm nay"), `time_of_day`, và gap delta để biến hoá phrasing. **Không log entry** — row `drink` / `break` đã được HAL ghi sẵn upstream rồi.
   - **Hydration nudge** — else nếu hydration delta ≥ hydration threshold → nhắc uống nước.
   - **Break nudge** — else nếu break delta ≥ break threshold → nhắc nghỉ/stretch.
   - Else (sedentary chưa qua threshold, hoặc chưa có reset nào hôm nay) → `NO_REPLY`.
4. **Sau khi nhắc** (chỉ nudge, không phải reaction), log entry `nudge_hydration` hoặc `nudge_break` — đây là cái reset delta cho window tiếp theo (và hiện lên timeline user).
5. **KHÔNG BAO GIỜ đoán** time-since từ memory — luôn tính từ log.

Reaction path được thêm vào để hành động tích cực không bị im lặng: trước đây user uống nước mà chưa qua threshold thì Lamp `NO_REPLY`, cảm giác như đèn chết. Reaction được nuôi bởi 2 field thêm trong `[wellbeing_context: ...]` — `count_today` (đếm số lần `drink` / `break` hôm nay) và `time_of_day` (`morning` / `noon` / `afternoon` / `evening` / `night`) — để câu thoại có cái cụ thể bám vào mà không tốn thêm tool call. Visual caption (kiểu "chai Lavie xanh") cố ý CHƯA làm — vision pipeline hiện chỉ trả class label, không có free-text mô tả.

### Ngưỡng

Hardcode trong `lamp/resources/openclaw-skills/wellbeing/SKILL.md`:

| Threshold | Giá trị test | Giá trị production |
|---|---|---|
| `HYDRATION_THRESHOLD_MIN` | **5** | 45 |
| `BREAK_THRESHOLD_MIN` | **7** | 30 |

> ⚠ **Release checklist:** trước khi ship, đổi cả 2 ngưỡng về production (45 / 30). Hydration và break cố ý lệch nhau (5 vs 7) để test phân biệt nhánh nào fire.

**Cách chặn spam re-nudge.** Entry `nudge_hydration` / `nudge_break` mà agent log sau khi nhắc cũng tính là reset point cho threshold. Sau khi Lamp nhắc, delta về 0 → lần nhắc tiếp theo cùng loại chỉ fire sau một threshold window nữa (45 min cho hydration, 30 min cho break trong production).

```
10:45  hydration overdue → nhắc 💧 + log nudge_hydration → hydration delta = 0
10:50  wake-up → delta = 5 min < 45 → SKIP
11:20  wake-up → delta = 35 min < 45 → SKIP
11:30  wake-up → delta = 45 min ≥ 45 → nhắc 💧 lại (user vẫn chưa uống)
```

Nếu user uống hoặc nghỉ trước window tiếp theo, entry `drink`/`break` tự reset delta → không cần nhắc nữa.

### User attribution — `[context: current_user=X]`

Sensing handler inject tag `[context: current_user=X]` vào mọi message `motion.activity`. `X` là **friend có session_start mới nhất** trong số các friend còn trong forget window (xem `FaceRecognizer.current_user()`), hoặc `"unknown"` khi face **chỉ** thấy stranger (không có friend nào còn present). Quan trọng: nếu có friend còn chưa bị "forget", `current_user()` trả về friend đó kể cả khi event `presence.enter` vừa rồi là của stranger — stranger-flicker không đá friend khỏi session.

Chọn theo `session_start` (thời điểm re-enter sau leave gần nhất) chứ không phải `last_seen`, để trường hợp 2 friend cùng present liên tục (Chloe 18:00, An 18:30) luôn chọn friend enter mới nhất (An) — deterministic, không phụ thuộc thứ tự dict.

**Nguồn duy nhất nằm ở HAL.** `sensing_service._send_event` đính kèm `face_recognizer.current_user()` vào mọi payload gửi đi dưới field `current_user`. Lamp sensing handler đọc thẳng `req.CurrentUser`, không parse lại từ message text nữa — khép lại lớp bug: `presence.enter` chỉ-có-stranger bắn khi friend vẫn còn present từng khiến `mood.CurrentUser()` của Lamp bị downgrade về `"unknown"`.

Caller ngoài (web UI, skill) có thể query cùng giá trị qua `GET http://127.0.0.1:5001/face/current-user` → `{"current_user": "<name>"}`. Đây là endpoint riêng; **không** parse ra từ `/face/cooldowns` (endpoint đó chỉ phục vụ debug view friend/stranger cooldown).

Các skill Wellbeing, Mood, Music đều bắt buộc dùng đúng giá trị này cho field `user` trong API call — **cấm** suy luận từ memory, KNOWLEDGE.md, chat history, hay `senderLabel`.

Cùng với `[context: current_user=X]`, handler còn inject thêm `[user_info: {"name","is_friend","telegram_id","telegram_username"}]` (build bởi `os/services/lib/skillcontext/BuildUserContext`, fetch từ lelamp `/user/info`). Skill phải đọc `telegram_id` từ block này — **cấm** `curl /user/info`. Block bị bỏ khi fetch fail hoặc `current_user` là `unknown`; SKILL.md vẫn giữ fallback path.

### Marker presence do HAL tự ghi

`FaceRecognizer._post_wellbeing` của HAL ghi thẳng row `enter` / `leave` qua `POST /api/wellbeing/log` — agent không tham gia, sensing handler của Lamp cũng không ghi nữa.

- **Per-friend:** mỗi friend có timeline riêng. Fresh friend detection (sau gap > `FACE_OWNER_FORGET_S`) → `{"action": "enter", "user": "<name>"}`. Friend bị forget trong `_check_leaves` → `{"action": "leave", "user": "<name>"}`. Chloe enter khi Leo còn present chỉ ghi `chloe: enter` — không đụng timeline của Leo.
- **Stranger (gộp về `"unknown"`):** gate bởi flag `_any_stranger_logged`. Stranger đầu tiên → `unknown: enter`. Flag giữ true khi còn bất kỳ stranger nào trong forget window, nên stranger_37 → stranger_38 → stranger_52 không sinh thêm row. Khi `_check_leaves` drop stranger cuối → `unknown: leave`.

Kết quả: mọi enter có matching leave trên cùng timeline, attribution trong mỗi timeline chỉ phản ánh event thuộc về user đó.

**Hai flow, hai rule khác nhau.** Quan trọng là presence row và activity row attribute theo nguyên tắc khác:

- **Enter/leave row** = per-presence: mỗi friend có timeline riêng, stranger gộp chung 1 timeline `unknown`, **không** xét `current_user()` — friend mới enter không đá friend đang present ra, stranger xuất hiện cạnh friend cũng không downgrade friend.
- **Activity row** (drink/break/sedentary) = dùng `current_user()` với friend priority — Chloe + stranger cùng visible → activity vào mỗi Chloe timeline vì cô là effective user.

Ví dụ — Chloe và Stranger_X overlap:

| Time | Event | Chloe timeline | Unknown timeline |
|---|---|---|---|
| 18:00 | Chloe fresh detected | `chloe: enter` | — |
| 18:15 | Stranger_X fresh detected | — | `unknown: enter` |
| 18:20 | `motion.activity` (using computer) — `current_user()=chloe` | `chloe: using computer` | — |
| 18:45 | Stranger cuối forget | — | `unknown: leave` |
| 19:00 | `motion.activity` (writing) — `current_user()=chloe` | `chloe: writing` | — |
| 20:00 | Chloe forget | `chloe: leave` | — |

Timeline Chloe đầy đủ session + activities; timeline unknown ghi lại "có stranger ghé qua 18:15–18:45" nhưng không có activity row, vì Chloe là effective user suốt khoảng đó. 2 flow không conflict — chúng trả lời 2 câu hỏi khác nhau.

### Ưu tiên: Skills > Knowledge > History

AGENTS.md quy định thứ tự ưu tiên: **SKILL.md luôn override KNOWLEDGE.md và conversation history**. Điều này rất quan trọng vì agent tự tích lũy "kinh nghiệm" vào KNOWLEDGE.md qua heartbeat, và những ghi chú này có thể chứa rules sai xung đột với skills do developer duy trì. Nếu agent phát hiện xung đột, nó phải cập nhật KNOWLEDGE.md cho khớp với skill, không phải ngược lại.

### Khi `presence.leave` / `presence.away`

Backend ghi marker `leave` vào log. Không có gì khác để làm — **không có cron để cancel**. Directive yêu cầu agent im lặng (`NO_REPLY`).

Agent dùng ảnh camera để đánh giá — KHÔNG phải lúc nào cũng nói. Tránh spam user khi họ trông ổn.

### Gợi ý nhạc (AI-Driven)

Gợi ý nhạc **không còn** được kích hoạt bởi timer cứng. Thay vào đó, AI agent **tự schedule** music check qua OpenClaw cron jobs và **tự học** thói quen user theo thời gian:

- **Tự schedule:** Khi phát hiện **hoạt động tĩnh đầu tiên** trong `motion.activity` (không phải `presence.enter`), AI tạo cron job (mặc định: mỗi 20 phút / 1200000ms, `sessionTarget: "current"`, `payload.kind: "systemEvent"`). AI tự điều chỉnh interval dựa trên phản hồi của user.
- **Quyết định dựa trên dữ liệu:** Trước khi gợi ý, AI query:
  - `GET /audio/status` — nhạc đang phát chưa?
  - `GET /api/openclaw/mood-history` — mood mới nhất để chọn genre
  - `GET /audio/history?person={name}` — lịch sử nghe nhạc per-user (genre ưa thích, thời lượng, mức độ hài lòng)
- **Vòng lặp học:** AI so sánh thời điểm gợi ý với `music.play` events trong mood history. Gợi ý được chấp nhận → củng cố timing/genre; bị từ chối → điều chỉnh schedule.
- **Cá nhân hóa:** Theo thời gian, AI học được khi nào user thích nghe nhạc, thể loại nào, nghe bao lâu — và điều chỉnh gợi ý cho phù hợp.

**Dữ liệu AI sử dụng để học thói quen:**

| Câu hỏi | Nguồn dữ liệu |
|----------|----------------|
| User ngồi vào bàn mấy giờ? | `presence.enter` events → field `hour` |
| Ngồi bao lâu thì muốn nghe nhạc? | Khoảng cách giữa `presence.enter` và `music.play` |
| Nghe thể loại gì? | `audio/history` → fields `query`, `title` |
| Nghe bao lâu thì tắt? | `audio/history` → field `duration_s` |
| Thời điểm nào thích nghe nhạc nhất? | `music.play` events → field `hour` |

Xem skill Music (`resources/openclaw-skills/music/SKILL.md`) để biết chi tiết implementation.

### Chăm sóc chủ động (piggyback trên sensing events)

Ngoài nhắc nhở theo lịch, agent được khuyến khích **chú ý** khi nhận bất kỳ event nào có user visible (presence.enter, motion.activity). Dựa vào giờ, thời gian ngồi, và hình ảnh → agent có thể chủ động nhắc ăn, nghỉ, hoặc hỏi thăm. Một câu ngắn, chỉ khi tự nhiên. Không bắt buộc nhưng khuyến khích.

Ví dụ: "Ăn sáng chưa?" khi presence.enter sáng sớm, "Trưa rồi, ăn gì chưa?" khi motion.activity lúc 12:20, "Khuya rồi đó..." khi motion.activity khuya.

### Speak và broadcast markers

Hai control marker cho turn channel-origin:

| Marker | Tác dụng | Khi nào dùng |
|---|---|---|
| `[HW:/speak:{}]` | Force TTS trên loa. Không ảnh hưởng Telegram. | Proactive crons (wellbeing, music) chạy trong Telegram/channel session để nhắc phát qua loa. Thường kèm `[HW:/dm:{"telegram_id":"..."}]` để DM đúng 1 người. |
| `[HW:/broadcast:{}]` | Force TTS **và** fan-out text tới tất cả Telegram chat. | Chỉ dành cho guard mode alert. Không dùng cho wellbeing/music — sẽ notify mọi chat, không phải chỉ người được nhắc. |

Mặc định turn channel-origin (Telegram, webchat) suppress TTS loa vì reply đi qua channel message. `/speak` override suppression đó mà không kèm fan-out.

**Cron-fire tự force TTS.** Khi OpenClaw emit `event:"cron"` với `action:"started"`, Lamp cache `sessionKey` và `lifecycle_start` kế tiếp trên session đó trong vòng 10 s sẽ bị mark là cron fire — `isChannelRun` bị override thành `false` nên loa lamp tự nói mà không cần `[HW:/speak]` trong reply. Marker vẫn hữu ích như defense-in-depth fallback nếu cron event bị drop (`dropIfSlow: true` ở phía OpenClaw).

### Mood history per-user

Mood history lưu per-user tại `/root/local/users/{name}/mood/YYYY-MM-DD.jsonl` (30 ngày retention). Hệ thống tracking ai đang ngồi qua `presence.enter` (face recognition) và log mood events vào thư mục user đó.

#### Nguồn mood

| Source | Cách hoạt động |
|---|---|
| **Camera** (`source: "camera"`) | `motion.activity` detect action cảm xúc (laughing, crying, yawning, singing) → Emotion Detection skill trigger → agent log mood |
| **Conversation** (`source: "conversation"`) | Agent detect mood theo 2 cách: (1) **single message** — explicit ("I'm tired") hoặc implied ("work is killing me" → stressed); (2) **conversation flow** — sau khi nói chuyện một lúc, đọc vibe tổng thể (tone shift, reply ngắn cộc lốc, topic lặp lại, năng lượng tăng/giảm). Agent tin trực giác và mạnh dạn suy luận: chỉ cần một gợi ý nhỏ là đủ, log nhầm còn hơn bỏ sót. Hoạt động trên mọi channel (Telegram, voice, web). |

#### Voice mood nudge

Voice events (`voice_command`, `voice`) kèm nudge `[MANDATORY: Follow Mood skill — log mood now.]` trong message gửi lên agent, cộng `[Current user: {name}]` khi face recognition biết ai đang ngồi.

#### Định dạng lưu trữ

JSONL (mỗi dòng 1 JSON object) — chọn thay vì JSON array vì:
- **Append**: O(1) — ghi thêm 1 dòng (không cần đọc-parse-ghi lại cả file)
- **Crash-safe**: tệ nhất mất 1 dòng (array có thể corrupt cả file)
- **Đọc N cuối**: `Query()` đọc tất cả rồi slice — đủ nhanh cho file daily (vài chục entry)

Mỗi row có field `kind` — hoặc raw `signal` từ 1 nguồn (camera/voice/telegram),
hoặc `decision` do agent tổng hợp từ các signal gần đây + decision trước đó.
Server không bao giờ tự fuse — Mood skill chịu trách nhiệm ghi cả 2 row mỗi lần
phát hiện mood.

```bash
# Ghi raw signal (agent gọi mỗi lần camera/voice/telegram báo mood)
POST /api/mood/log  {"kind":"signal","mood":"happy","source":"camera","trigger":"laughing"}

# Ghi decision sau khi đọc history và tổng hợp
POST /api/mood/log  {"kind":"decision","mood":"happy","based_on":"3 signals last 20min","reasoning":"laughing reinforces previous happy decision"}

# Đọc tất cả kind cho 1 ngày (agent dùng để re-analyze)
GET /api/openclaw/mood-history?user=gray&date=2026-04-09&last=100

# Đọc decision mới nhất (Music/Wellbeing dùng để biết "current mood")
GET /api/openclaw/mood-history?user=gray&kind=decision&last=1
```

Signal row: `{"ts":...,"seq":1,"hour":10,"kind":"signal","mood":"happy","source":"camera","trigger":"laughing"}`
Decision row: `{"ts":...,"seq":2,"hour":10,"kind":"decision","mood":"happy","source":"agent","based_on":"...","reasoning":"..."}`

### Nhận diện cross-channel

Agent liên kết tên face recognition với Telegram username bằng cách quan sát timing và context (ví dụ: "gray" đang ngồi và "@GrayDev" nhắn Telegram cùng lúc). Mapping được lưu vào USER.md (cho friend) hoặc notes trong folder user. Agent hỏi xác nhận nếu chưa chắc.

---

## Phân tích Motion Activity (khi đang có mặt)

Khi user đang ở trạng thái PRESENT và camera phát hiện chuyển động, hệ thống gửi event `motion.activity` thay vì `motion`. Hệ thống gửi tên action đã detect (không kèm ảnh — tên action đủ để agent suy luận).

### Cách hoạt động

`MotionPerception` buffer snapshots và action names, flush theo interval (`MOTION_FLUSH_S`). Khi flush, check `PresenceService.state`:
- **PRESENT** → gửi 1 event `motion.activity` duy nhất. Format message:
  - `Activity detected: <labels>.` — HAL đã categorize: physical actions gộp thành bucket (`drink`, `break`); sedentary activities giữ raw Kinetics label (`using computer`, `writing`, `texting`, `reading book`, `reading newspaper`, `drawing`, `playing controller`). Agent log từng label nguyên văn — không map gì thêm ở phía agent.
  - Emotional X3D actions (`laughing`, `crying`, `yawning`, `singing`) **cố ý bị drop** ở đây. Một event type riêng `motion.emotional` sẽ được thêm sau; cho đến khi đó, detection emotional bị bỏ qua im lặng. `motion.activity` giữ thuần vật lý.
  - Không gửi ảnh — tiết kiệm tokens. **Không** yêu cầu nhận diện friend.
- **Còn lại** → event bị **skip** (log, không gửi). Lamp chỉ expect `motion.activity` — plain `motion` từ X3D/pose không có handler và lãng phí agent tokens.

Ví dụ message:
```
Activity detected: drink, using computer.
Activity detected: break.
Activity detected: writing, reading book.
```

### Flow wellbeing nudge (event-driven)

Agent đọc dòng `Activity detected:`, split theo dấu phẩy, rồi POST từng label nguyên văn vào field `action` — HAL đã categorize sẵn nên agent không map gì.

1. **Log** từng label qua `POST /api/wellbeing/log` với `{action, notes:"", user}` — một entry/label. HAL đã dedup trên outbound label set nên agent không phải check.
2. **Đọc history** qua `GET /api/openclaw/wellbeing-history?user={name}&last=50`.
3. **Tính delta** so với reset point gần nhất cho mỗi loại (xem Wellbeing SKILL Step 3).
4. **Quyết định nudge** theo Wellbeing SKILL Step 4 — tối đa 1 hydration hoặc break nudge mỗi turn.
5. **Phản hồi**: 1 câu chăm sóc ngắn nếu có nudge/suggestion, không thì `NO_REPLY`.

### Hành vi Agent

| Event | Emotion | Voice |
|---|---|---|
| `motion.activity` | `curious` (0.4) | CÓ (nhận xét caring có context) hoặc NO_REPLY (ngồi yên) |

### Per-Face Motion Activity (MotionPerFacePerception)

Tùy chọn thay thế cho `MotionPerception` — chạy nhận diện hành động **riêng cho từng khuôn mặt** thay vì toàn bộ frame. Bật qua `HAL_MOTION_PER_FACE_ENABLED=true` (mặc định `false`).

- Mở rộng bbox khuôn mặt (1x lên, 2x trái/phải/xuống) để lấy phần thân trên + tay.
- Mỗi `face_id` có WS session riêng với backend action recognition.
- Person detection luôn **tắt** trên các session này.
- Dedup theo từng action riêng biệt cho mỗi face (mặc định 5 phút).
- Session cần tối thiểu 4 frame trước khi gửi event đầu tiên.
- Session bị xóa sau 30 giây không thấy face đó.

| Config | Env var | Mặc định | Mô tả |
|---|---|---|---|
| `MOTION_PER_FACE_ENABLED` | `HAL_MOTION_PER_FACE_ENABLED` | `false` | Bật nhận diện hành động per-face |
| `MOTION_PER_FACE_DEDUP_WINDOW_S` | `HAL_MOTION_PER_FACE_DEDUP_WINDOW_S` | `300` | Cửa sổ dedup per-action (5 phút) |
| `MOTION_PER_FACE_SESSION_TTL_S` | `HAL_MOTION_PER_FACE_SESSION_TTL_S` | `30` | Xóa session sau bao lâu không thấy face |
| `MOTION_PER_FACE_MIN_FRAMES` | `HAL_MOTION_PER_FACE_MIN_FRAMES` | `4` | Số frame tối thiểu trước khi gửi event |

Định dạng message: `Activity detected (gray): using computer, writing.`

---

## Nhận diện cảm xúc người dùng — User Emotion (UC-M1) ✅

Lamp nhận diện trạng thái cảm xúc **của người dùng** qua ba kênh:

1. **Biểu cảm khuôn mặt** (chính) — event `emotion.detected` từ `os/hal/drivers/sensing/perceptions/emotion.py`. Dùng emotion classifier chuyên dụng chạy trên dlbackend tự host qua WebSocket. Nhận diện 7 cảm xúc: Angry, Disgust, Fear, Happy, Sad, Surprise, Neutral. Ngưỡng confidence cấu hình được (`EMOTION_CONFIDENCE_THRESHOLD`).
2. **Cảm xúc giọng nói** (phụ) — event `speech_emotion.detected` từ `os/hal/drivers/voice/speech_emotion/`. Chạy ở cuối mỗi phiên STT đã nhận diện được speaker, cùng WAV bytes đã dùng cho speaker recognition. Dùng `emotion2vec_plus_large` trên dlbackend qua HTTP. Xem [Speech Emotion Recognition](../speech-emotion.md) cho pipeline đầy đủ.
3. **Body action** (cấp 3) — emotional X3D actions từ action recognition **cố ý bị loại** khỏi `motion.activity` (giờ thuần vật lý: sedentary/drink/break). Một event type `motion.emotional` riêng đang được lên kế hoạch.

> **Đừng nhầm lẫn với Emotion Expression** (`emotion/SKILL.md`) — cái đó điều khiển cảm xúc đầu ra của Lamp (servo + LED + eyes). Emotion Detection là cảm nhận *user* đang cảm thấy gì; Emotion Expression là cách *Lamp* thể hiện cảm xúc của chính nó.

### Event `emotion.detected`

Được HAL fire khi dlbackend emotion classifier nhận diện biểu cảm khuôn mặt vượt ngưỡng confidence. Format message:

```
Emotion detected: <Label>. (weak camera cue; confidence=<0.00-1.00>; bucket=<positive|negative|other>; treat as uncertain, <hedge theo bucket>.)
```

Ví dụ thực tế:

```
Emotion detected: Fear. (weak camera cue; confidence=0.62; bucket=negative; treat as uncertain, do not assume the user is distressed.)
Emotion detected: Happy. (weak camera cue; confidence=0.78; bucket=positive; treat as uncertain, do not over-celebrate.)
```

Prefix `Emotion detected: <Label>.` được giữ nguyên để parser của `user-emotion-detection/SKILL.md` và mood mapping Fear→stressed / Sad→sad vẫn chạy như cũ. Phần ngoặc đơn phía sau là để LLM không over-commit khi FER read nhiễu (bug từng gặp: Fear → "Oh hello there again"). Hedge theo bucket: `negative` → "do not assume the user is distressed"; `positive` → "do not over-celebrate"; `other` → "do not over-react".

**Dedup theo polarity bucket** (`EMOTION_BUCKETS` trong `os/hal/drivers/sensing/perceptions/processors/emotion.py`) gộp các label chi tiết thành `positive` / `negative` / `other` và dedup theo `(current_user, bucket)` trong window 5 phút. Nhiễu trong cùng bucket (Fear↔Sad↔Anger) gộp thành 1 event/window; flip giữa hai bucket (Fear→Happy) vẫn fire như mood change thật. Confidence trong message được average **chỉ trên các lần xuất hiện của dominant label** — confidence của label khác không pha loãng.

Sensing handler (`handler.go`) route `emotion.detected` events tới agent. Khi agent đang bận, events được queue và replay khi agent rảnh.

### Agent behavior

`user-emotion-detection/SKILL.md` xử lý `emotion.detected` events:

1. Map facial emotion → mood signal (vd: Happy → happy, Sad → sad, Angry → frustrated, Fear → stressed) và log `signal` row qua `POST /api/mood/log`
2. Chọn 1 response route từ bảng 3 row (first match wins):
   - **#1 `audio_playing == true`** → LED ack + `NO_REPLY` (đang có nhạc, không nói chồng lên)
   - **#2 `suggestion_worthy == true` AND decision fresh AND `last_suggestion_age_min ∉ [0, 7)`** → **music** — 1 câu nhẹ kèm genre, xem `music-suggestion/SKILL.md`
   - **#3 còn lại** → **checkin** — 1 phản ứng người ngắn. Xem `user-emotion-detection/reference/checkin.md` cho example theo raw FER label (Sad/Fear/Angry/Disgust/Happy/Surprise), mỗi label có 3 style: Ask/Comfort/Invite. Examples chỉ là gợi ý — agent tự improvise mỗi turn.
3. **Cooldown chỉ chặn music, không chặn checkin.** Khi cooldown 7 phút còn hiệu lực, row #2 fail vế thứ ba và event rơi xuống checkin (row #3). Agent vẫn hỏi "có chuyện gì" — chỉ không suggest nhạc 2 lần liên tiếp. `NO_REPLY` chỉ xảy ra ở row #1 (đang phát nhạc).
4. **Không bao giờ chào trên emotion event.** `emotion.detected` không phải presence/arrival event — `sensing/SKILL.md` cấm openers như `hello`, `welcome back`, mọi câu chứa `again`. Greeting chỉ dành cho `presence.enter`.

Cả 2 route share chung 1 cooldown: music log qua `POST /api/music-suggestion/log` với `trigger:"<genre>:<mood>"` (mood bucket); checkin log cùng endpoint với `trigger:"checkin:<emotion>"` (raw FER label). `last_suggestion_age_min` phản ánh cả 2 kênh nên music suggestion mới sẽ im lặng nhánh music trong 7 phút, nhưng checkin vẫn fire. Checkin phrasing keyed theo raw emotion (không phải mood) — mỗi FER label có 3 style: Ask / Comfort / Invite. Xem `reference/checkin.md`. Output checkin luôn prefix `[HW:/emotion:{"emotion":"caring","intensity":0.5}]`.

### Mood pipeline

- **Mood history** (agent ghi): Mỗi signal, Mood skill ghi 1 row raw `signal`, rồi đọc history gần đây và ghi 1 row `decision` tổng hợp (vd: `{"kind":"decision","mood":"happy","based_on":"...","reasoning":"..."}`).
- Mood decisions trigger downstream skills: `music-suggestion` (nhạc chủ động), `wellbeing` (nhắc uống nước/nghỉ).

Xem `user-emotion-detection/SKILL.md` để biết rules phản hồi đầy đủ.

### Event `speech_emotion.detected`

Được HAL fire ở cuối mỗi phiên STT đã nhận diện được speaker, sau khi WAV bytes (cùng bytes đã dùng cho speaker `/embed`) được forward sang `dlbackend /api/dl/ser/recognize` (emotion2vec_plus_large). Buffering, aggregation theo từng user, dedup theo polarity bucket, và POST sang Lamp đều nằm trong `os/hal/drivers/voice/speech_emotion/SpeechEmotionService` — `voice_service.py` chỉ gọi `submit(user, wav, duration)`. Format message giống pipeline khuôn mặt:

```
Speech emotion detected: <Label>. (weak voice cue; confidence=<0.00-1.00>; bucket=<positive|negative|other>; treat as uncertain, <hedge theo bucket>.)
```

Ví dụ thực tế:

```
Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; treat as uncertain, do not assume the user is distressed.)
Speech emotion detected: Happy. (weak voice cue; confidence=0.84; bucket=positive; treat as uncertain, do not over-celebrate.)
```

Labels (từ emotion2vec_plus_large): `angry`, `disgusted`, `fearful`, `happy`, `neutral`, `other`, `sad`, `surprised`, `<unk>`. Neutral / other / `<unk>` bị drop trước khi bucket — cùng quy tắc với face Neutral.

**Anti-spam guards** (mirror 1-1 với pipeline khuôn mặt):

1. Audio quá ngắn (`duration_s < SPEECH_EMOTION_MIN_AUDIO_S`) drop ở `submit()`.
2. Unknown speaker (`match=false` hoặc `name=="unknown"`) drop — không có subject để gán cảm xúc.
3. Confidence thấp (`< CONFIDENCE_THRESHOLD_BY_LABEL[label]`, fallback `DEFAULT_CONFIDENCE_THRESHOLD` — đều ở `speech_emotion/constants.py`) bị worker drop.
4. Neutral labels drop ở flush.
5. TTL dedup `(user, bucket)` trong `SPEECH_EMOTION_DEDUP_WINDOW_S` (mặc định 5 phút). Mỗi bucket có timer riêng — gửi event positive KHÔNG reset window của negative.

Payload event gửi kèm `current_user` rõ ràng nên sensing handler Lamp không phải tra cứu lại.

### Agent behavior (chung với face emotion)

`speech_emotion.detected` route qua **cùng skill** với `emotion.detected` — `user-emotion-detection/SKILL.md`. Sensing handler gắn prefix `[speech_emotion]` (vs `[emotion]` cho face), pre-fetch cùng block `[emotion_context: ...]`, rồi forward tới agent. Skill:

1. Parse prefix để chọn `source` trên mood signal log (`"voice"` cho `[speech_emotion]`, `"camera"` cho `[emotion]`).
2. Map label qua bảng chung: `Happy → happy`, `Sad → sad`, `Angry → frustrated`, `Fear`/`Fearful → stressed`, `Surprise`/`Surprised → excited`, `Disgust`/`Disgusted → frustrated`, `Neutral → normal`. Biến thể voice (`Fearful`, `Surprised`, `Disgusted`) bucket giống hệt counterpart face.
3. Chạy cùng router `music / checkin / action / silent` trên cùng các field của `[emotion_context: ...]` (`audio_playing`, `suggestion_worthy`, `last_suggestion_age_min`, `is_decision_stale`).
4. Share 1 cooldown music-suggestion duy nhất giữa 2 modality — voice không thể bypass cooldown mà camera vừa set, và ngược lại.

Hedge `(weak voice cue; ...)` trong message là tín hiệu cho model nghiêng về Comfort/Invite ở nhánh checkin thay vì Ask khi voice-only negative read, vì emotion2vec trên utterance ngắn nhiễu hơn face FER. Xem `user-emotion-detection/SKILL.md` cho rules đầy đủ.

Xem [Speech Emotion Recognition](../speech-emotion.md) cho kiến trúc đầy đủ, threading model, configuration, và failure mode.

---

## Lưu trữ Snapshot (hai tầng)

Các sensing event có kèm camera frame (`motion`, `presence.enter`, `presence.leave`, `motion.activity`, `emotion.detected`, `music.mood`) lưu snapshot ở hai nơi.

| Tầng | Đường dẫn | Rotation | Giữ qua reboot |
|------|-----------|----------|-----------------|
| **Tmp buffer** | `/tmp/hal-sensing-snapshots/sensing_<prefix>/` | Theo số lượng (tối đa 50 file) | Không |
| **Persistent** | `/var/lib/hal/snapshots/sensing_<prefix>/` | TTL (72h) + dung lượng (tối đa 50 MB) | Có |

Mỗi loại event ghi vào subdir riêng (`sensing_<prefix>`, ví dụ `sensing_presence/`, `sensing_motion_activity/`, `sensing_emotion/`). Tên file là `<ms>.jpg`. Snapshot được lưu vào tmp trước, rồi copy sang persistent dir. Đường dẫn persistent được ghi trong event message (`[snapshot: /var/lib/hal/snapshots/sensing_<prefix>/<ms>.jpg]`) để agent có thể xem lại — kể cả sau khi thiết bị reboot. Monitor phục vụ ảnh qua `GET /api/sensing/snapshot/<category>/<name>`.

Các hằng số cấu hình nằm trong `os/hal/config.py`:
- `SNAPSHOT_TMP_MAX_COUNT` — số file tối đa trong tmp (mặc định 50)
- `SNAPSHOT_PERSIST_TTL_S` — TTL file persistent tính bằng giây (mặc định 72h)
- `SNAPSHOT_PERSIST_MAX_BYTES` — dung lượng tối đa thư mục persistent (mặc định 50 MB)

---

## Quy tắc chung (tất cả event type)

- **Passive sensing events** (`[sensing:*]`) bị drop nếu agent đang bận xử lý turn khác.
- **Voice events** luôn pass through — người dùng đang chủ động nói chuyện. Voice messages kèm mood scan nudge (`[MANDATORY: Follow Mood skill — log mood now.]`) để agent nhớ detect mood từ conversation flow.
- Prefix `[sensing:type]` trong message là cách agent biết đây là ambient event, không phải message từ người dùng.
- **Pre-turn `thinking` emotion**: Hook `emotion-acknowledge` tự fire `POST /emotion {thinking, 0.7}` server-side ở event `message:preprocessed` cho mọi non-sensing message — agent không cần gọi. Hook skip sensing events vì mỗi type đã có emotion đầu tiên riêng.
- **Image pruning echo**: OpenClaw strip image payload cũ khỏi conversation history để tiết kiệm token. Model nhỏ (Haiku) có thể echo marker dưới dạng `[image description removed]` trong response. `SOUL.md` hướng dẫn agent không được echo các marker này.
