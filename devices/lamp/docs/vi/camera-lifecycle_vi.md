# Camera Lifecycle — Tự động bật/tắt

Camera nên **reactive**: bật khi cần, tắt khi rảnh. Tiết kiệm CPU/RAM, tôn trọng quyền riêng tư.

## Trạng thái hiện tại

- `POST /camera/disable` / `POST /camera/enable` — toggle thủ công từ web monitor
- Camera cấp frame cho sensing: nhận diện khuôn mặt (ONNX InsightFace), pose/motion (ONNX), mức ánh sáng (pixel mean), presence (pixel diff)
- Voice pipeline (mic) chạy độc lập với camera
- Sound perception chạy độc lập với camera

## Thiết kế: Camera On/Off là switch duy nhất

Không thêm abstraction mới. Camera on = full sensing. Camera off = vision sensing dừng, audio sensing vẫn chạy.

### Khi camera TẮT

- `_tick()` bỏ qua mọi vision perception (face, pose, motion, light)
- Sound perception vẫn chạy (dùng mic)
- Wake word detection vẫn chạy (voice_service)
- TTS vẫn hoạt động
- Servo/LED vẫn hoạt động
- Web monitor Camera tab hiện "Disabled" với nút Enable

### Khi camera BẬT

- Mọi perception chạy bình thường
- Face/pose ONNX inference mỗi tick chẵn (optimization có sẵn)

## Trigger tự động TẮT

### 1. Scene: night

Khi `/scene` kích hoạt `night` → tắt camera.
- User đi ngủ, không cần vision
- Sound perception vẫn giữ cho wake word / tiếng động lớn

### 2. Emotion: sleepy

Khi `/emotion` nhận `sleepy` → tắt camera.
- Giống night, agent chủ động đưa đèn vào sleep

### 3. Presence idle timeout

Khi presence chuyển sang `away` (không motion trong away_timeout giây) → tắt camera.
- Không ai trong phòng, không cần chạy vision
- Tiếng động hoặc wake word sẽ bật lại

### 4. Voice command: "đừng nhìn" / "stop watching"

User nói "Lamp, đừng nhìn" / "don't watch me" / "privacy mode" → agent gọi `[HW:/camera/disable:{}]`.
- Yêu cầu riêng tư rõ ràng từ user
- Chỉ voice command hoặc web toggle mới bật lại được

### 5. Scene: focus, reading, movie

Khi `/scene` kích hoạt `focus`, `reading`, hoặc `movie` → tắt camera.
- User đã ngồi đó và đang tập trung, không cần detect thêm
- Presence đã biết từ khi scene kích hoạt
- Tiết kiệm CPU trong session dài
- Camera bật lại khi scene đổi hoặc user đi (detect bằng sound/wake word)

## Trigger tự động BẬT

### 1. Wake word detected

Voice service phát hiện wake word ("Looney", etc.) → bật camera.
- User đang tương tác, có thể cần visual context
- Luôn hoạt động vì mic chạy độc lập

### 2. Sound spike (tiếng ồn lớn)

Sound perception phát hiện RMS vượt ngưỡng khi camera tắt → bật camera.
- Có thể ai đó vào phòng
- Camera bật → face detect → presence.enter nếu tìm thấy người
- Nếu không detect face sau N giây → tắt lại (tránh false positive)

### 3. Scene đổi sang scene active

Khi `/scene` chuyển từ night/sleep sang energize hoặc relax → bật camera.
- User hoặc agent kích hoạt scene ban ngày

### 4. Emotion đổi từ sleepy sang khác

Khi `/emotion` nhận emotion không phải sleepy → bật camera.
- Agent đang tương tác, có thể cần vision

### 5. Morning cron / lịch trình

Cron job vào giờ thức (ví dụ 6:00 AM) → bật camera.
- Sẵn sàng cho morning routine trước khi user nói gì

### 6. Voice command: "nhìn xem" / "look"

User nói "Lamp, nhìn xem" / "look at me" / "camera on" → agent gọi `[HW:/camera/enable:{}]`.
- Yêu cầu rõ ràng từ user

### 7. Telegram/web chat cần visual context

Agent cần snapshot (camera skill) → tự động bật camera, chụp, tùy chọn giữ bật hoặc tắt sau.

## Manual Override

Web monitor Camera tab toggle luôn hoạt động. Manual disable giữ cho đến khi:
- User bật lại thủ công
- HOẶC voice command bật lại rõ ràng

Manual override KHÔNG bị ghi đè bởi scene/emotion/presence triggers. Chỉ hành động rõ ràng từ user (voice command, web toggle) mới xóa manual override.

## Implementation Plan

### HAL (Python)

1. **`server.py`**: ✅ Done — Đã có `/camera/disable`, `/camera/enable`, `_camera_disabled` flag.

2. **`_camera_manual_override` flag**: ✅ Done — `/camera/disable` set override, `/camera/enable` xóa. `_auto_camera_off()` / `_auto_camera_on()` helpers tôn trọng override.

3. **Scene endpoint** (`/scene`): ✅ Done — Sau khi set scene:
   - `night`, `focus`, `reading`, `movie` → `_auto_camera_off("scene:{name}")`
   - `energize`, `relax` → `_auto_camera_on("scene:{name}")`

4. **Emotion endpoint** (`/emotion`): ✅ Done — preset "camera" field điều khiển:
   - `sleepy` có `"camera": "off"` → `_auto_camera_off("emotion:sleepy")`
   - Emotion khác khi camera đang auto-off → `_auto_camera_on("emotion:{name}")`

5. **Presence service**: ❌ Bỏ qua — camera giữ bật khi away. Tắt sẽ mất auto-greeting (face detect → presence.enter) khi user quay lại.

6. **Sound perception**: ❌ Bỏ qua — các trường hợp camera off (scene/emotion/manual) đều có path re-enable rõ ràng. Sound spike thêm phức tạp mà không cover thêm case mới.

7. **`_tick()` trong sensing_service**: ✅ Đã hoạt động — `frame = None` khi camera stopped, vision perceptions skip. Không cần thay đổi.

### Lamp (Go)

8. **Voice service / wake word**: ❌ Bỏ qua — wake word → agent → emotion preset `"camera": "on"` đã tự bật camera. Không cần enable sớm.

9. **Healthwatch**: ✅ Không cần thay đổi.

### OpenClaw Skills

10. **Camera skill**: ✅ Done — voice/chat toggle + auto-enable trước capture.

### Web Monitor

11. ✅ Done — Camera tab có Enable/Disable toggle.

## Thay đổi Skill cần thiết

### Camera SKILL.md — ✅ Done

- ✅ Description cập nhật với trigger phrases cho toggle
- ✅ Examples cho disable/enable qua `[HW:/camera/disable:{}]` và `[HW:/camera/enable:{}]`
- ✅ Rule auto-enable trước capture
- ✅ Rule: không bao giờ toggle camera chủ động mà không có yêu cầu từ user

### Agent không nên tự ý toggle camera

- Chỉ voice command từ user hoặc system triggers (scene, emotion, presence) mới toggle
- Agent không bao giờ tự quyết định tắt/bật camera mà không có yêu cầu

## Digital Zoom

Zoom phần mềm để tập trung vào vật nhỏ (vd: màn hình laptop đang gọi video call để Lamp đọc được nội dung).

### API

- `POST /camera/zoom` body `{"zoom": <float>}` — set zoom factor, range `1.0` (không zoom) đến `5.0`. Trả về `CameraInfoResponse` đã cập nhật.
- `GET /camera` có field `zoom` chứa factor hiện tại.

### Cơ chế

Zoom được apply **trong capture loop** (`drivers/camera/video_capture_device.py::_video_capture_loop`) ngay sau rotate, trước khi set `last_response`. Loop center-crop frame theo `1/zoom` rồi resize về kích thước gốc, nên mọi consumer downstream đều đọc cùng buffer đã zoom:

| Consumer | Nguồn frame | Thấy zoom? |
|---|---|---|
| `/camera/snapshot` (vision tool) | `camera_capture.last_frame` | ✅ |
| `/camera/stream` (web UI) | `camera_capture.last_frame` | ✅ |
| Sensing orchestrator (face recog, motion, pose, emotion) | `camera_capture.capture()` → `last_response` | ✅ |
| Tracker service | `camera_capture.last_frame` | ✅ |

### Trade-off

Zoom > 1 thu hẹp field of view của **tất cả** consumer:

- ✅ Mặt người trên bề mặt nhỏ (màn hình laptop) sẽ đủ to để InsightFace detect được → presence.enter có thể trigger từ người trên Meet call.
- ✅ Vision tool snapshot đọc rõ nội dung trên màn hình.
- ❌ Người/vật ngoài vùng center crop sẽ vô hình với face recog / motion / pose / tracker.
- ❌ Đang tracking có thể mất target nếu nó di chuyển ra ngoài vùng crop.

Coi zoom > 1 là **chế độ tạm thời** cho 1 subject cụ thể. Reset về `1.0` (nút Reset trên web UI hoặc `POST /camera/zoom {"zoom": 1.0}`) khi xong để sensing trở lại bình thường.

### Lưu trữ

State zoom nằm trên instance device (`LocalVideoCaptureDevice.zoom`). Không persist — reset về `1.0` khi server restart. Không auto-reset khi disable/enable camera.

### Web UI

Monitor → Camera tab → card Live Stream có slider Zoom (1.0×–5.0×, step 0.1, debounce 200 ms POST) kèm nút Reset. Giá trị slider chuyển màu vàng khi đang zoom để cảnh báo FOV bị thu hẹp.

## Exposure & Frame Rate

Auto-exposure của camera USB kéo dài thời gian tích sáng khi thiếu sáng (~60ms), giới hạn tốc độ nhả frame ở **~16fps tại mọi độ phân giải** — đây là giới hạn exposure clock, không phải băng thông USB (720p và 4K đều kẹt 16fps). Ghim **manual** exposure tránh được throttle này, nhưng manual + gain cao đẩy ISP của camera vào trạng thái bất ổn làm loạn màu (frame posterize xanh lá/hồng) và kẹt nguyên phiên capture — đã gặp trên nhiều thiết bị với gain 255 và gain 192. Vì vậy HAL mặc định **auto** exposure; chỉ chuyển sang manual khi frame rate ổn định quan trọng hơn độ sáng thích ứng, và giữ gain ≤ ~144.

### Config (env, đọc bởi `config.py`)

| Biến | Default | Ý nghĩa |
|---|---|---|
| `HAL_CAMERA_AUTO_EXPOSURE` | `auto` | `auto` dùng auto-exposure thích ứng của camera (default; sáng/thích ứng nhưng throttle fps khi thiếu sáng). `manual` ghim exposure theo các giá trị bên dưới — rủi ro loạn màu ISP khi gain cao. |
| `HAL_CAMERA_EXPOSURE` | `330` | Thời gian exposure manual, V4L2 `exposure_absolute` ×100µs: `200`=20ms (30fps), `330`=33ms (trần ≈30fps), `500`=50ms (≈20fps). |
| `HAL_CAMERA_GAIN` | `96` | Gain cảm biến (tùy camera, vd 0–255). Tăng sáng không tốn fps nhưng thêm noise; trên ~144 rủi ro loạn màu ISP. |
| `HAL_CAMERA_BRIGHTNESS` | _(không set)_ | Offset brightness (tùy camera, vd -64..64). Nâng sáng digital. |

Default áp dụng kể cả khi `.env` không có entry nào. Muốn ghim frame rate trên một thiết bị thì set `HAL_CAMERA_AUTO_EXPOSURE=manual` per device — fallback manual (330 / 96) là bộ giá trị đã verify màu ổn định; default cũ (`manual` / 500 / 255) là combo độc đã biết.

### Cơ chế

`_apply_camera_controls()` (`drivers/camera/video_capture_device.py`) chạy sau khi set độ phân giải lúc open **và mỗi lần reopen device** — open mới reset camera về default, nếu không áp lại thì manual exposure sẽ âm thầm mất và FPS throttle quay lại. Map sang V4L2/UVC controls qua OpenCV: `CAP_PROP_AUTO_EXPOSURE` (1=manual, 3=auto), `CAP_PROP_EXPOSURE`, `CAP_PROP_GAIN`, `CAP_PROP_BRIGHTNESS`.

Ở mode `auto`, control được chủ động set về 3 (aperture-priority) mỗi lần open, chứ không bỏ mặc: camera UVC giữ nguyên manual exposure/gain qua các lần restart HAL, nên trạng thái manual sót lại từ cấu hình cũ sẽ sống dai qua cả việc đổi `.env` sang `auto`. **Gain** manual sót lại thì không bị reset (default tùy camera, auto-exposure tự bù); nếu đổi sang auto rồi mà màu vẫn sai, xóa một lần bằng `v4l2-ctl -d /dev/video0 --set-ctrl gain=<default>`. Lưu ý đổi `.env` chỉ có hiệu lực sau `systemctl restart hal` — process đang chạy vẫn giữ env lúc nó start.

### Trade-off

Frame rate vs độ sáng là trade-off vật lý cứng trong phòng tối: exposure tối đa vẫn giữ được 30fps là ~33ms (`HAL_CAMERA_EXPOSURE=330`); ảnh sáng hơn cần exposure dài hơn (ít fps) hoặc gain cao hơn (nhiễu hơn, và trên ~144 rủi ro loạn màu). Stream endpoint bị cap riêng bởi `HAL_CAMERA_STREAM_FPS` (default 10), nên live view trên monitor không phản ánh tốc độ capture thật.

## Chọn thiết bị (Device Selection)

Mặc định camera mở theo index: `HAL_CAMERA_INDEX` (default `0`) → `/dev/video0`, kèm fallback scan (symlink udev `/dev/cam`, rồi quét index 0–5). Index trần dễ vỡ — cắm thêm USB device khác hoặc thứ tự enumerate lúc boot đổi là `/dev/video<N>` xáo trộn.

`HAL_CAMERA_NAME` (tuỳ chọn) chọn camera theo **tên phần cứng**, giống cách audio chọn device (`resolve_camera_device_id()` trong `drivers/camera/video_capture_device.py`). Giá trị là substring không phân biệt hoa thường của tên thiết bị v4l2 (ví dụ `OPENAICAM`). Thứ tự resolve:

1. **Symlink capture `/dev/v4l/by-id`** (`...-video-index0`) có tên chứa needle — trả về chính đường symlink, nên các lần reopen sau vẫn bám đúng thiết bị kể cả khi kernel đánh số lại `/dev/video<N>` sau replug hay USB power-cycle.
2. **Scan tên sysfs** — match `/sys/class/video4linux/video<N>/name` (N nhỏ nhất trước), bỏ qua node metadata anh em của UVC (cùng tên, thuộc tính `index` khác 0, không capture được).
3. **Fallback về index cũ** kèm warning khi không match gì (camera rớt hoặc đổi tên).

Không set `HAL_CAMERA_NAME` thì hành vi index cũ giữ nguyên 100%.

## Khôi phục lỗi (Failure Recovery)

Capture loop (`drivers/camera/video_capture_device.py`) tự khôi phục 2 dạng lỗi thiết bị, đều bằng cách release rồi mở lại device V4L2 qua `_reopen_with_backoff()` (retry backoff lũy tiến 1s→30s, không bao giờ thoát loop vĩnh viễn khi HAL còn chạy; MJPEG, độ phân giải và exposure được áp lại sau mỗi lần reopen):

- **`read()` fail** — USB autosuspend hoặc lỗi V4L2 thoáng qua làm `read()` trả `ret=False`. Retry 1 lần sau 1s, rồi reopen.
- **ISP đóng băng** — camera cứ nhả lại **cùng một buffer** với `ret=True` (đã gặp trên UVC cam khi dùng manual exposure/gain), nên nhánh recovery `read()`-fail không bao giờ kích hoạt trong khi mọi consumer (realtime look, sensing, tracking, snapshot) âm thầm xử lý cảnh cũ. Watchdog so sánh chữ ký frame đã subsample; frame byte-identical liên tục 10s (`_FREEZE_REOPEN_S`) không thể đến từ sensor thật → reopen. Log: `Camera frozen — identical frames for Ns, reopening device`.
- **ISP loạn màu (color corruption)** — cùng bệnh ISP kẹt nhưng frame vẫn **thay đổi**, chỉ có chroma là rác: vùng xanh lá bão hoà cực đại posterize + mảng magenta/hồng bổ túc, trong khi mọi v4l2 control đều đúng (đã bắt được mẫu sống trên cam SunplusIT ngay sau một chu kỳ close/open). Freeze watchdog mù với mode này, nên có watchdog thứ hai check frame subsample trong HSV (throttle ~1 lần/giây): frame bị coi là corrupt khi xanh lá bão hoà cao phủ ≥10% (`_COLOR_GREEN_FRAC`) **và** magenta ≥0.8% (`_COLOR_MAGENTA_FRAC`) ở saturation ≥100 (`_COLOR_SAT_MIN`, value ≥60). Đòi cả hai họ hue bổ túc xuất hiện cùng lúc chính là chốt chặn false-positive — tường xanh, cây cối, hay LED của chính lamp hắt màu chỉ có 1 hue. Corruption phải liên tục 30s (`_COLOR_CORRUPT_REOPEN_S`; 1 frame sạch là reset) mới kích recovery như freeze. Ngưỡng calibrate từ ảnh corrupt thật (green 0.19 / magenta 0.012) vs cảnh sạch (0.000 / 0.000). Log: `Camera color corruption — posterized green/magenta frames for Ns, reopening device`.

### ISP kẹt sâu → leo thang USB power-cycle

Đôi khi ISP kẹt **sâu** hơn mức reopen V4L2 chữa được: frame trở lại vẫn posterize xanh/hồng hoặc freeze lại ngay sau reopen, dù mọi v4l2 control đều đúng (auto_exposure=3, gain hợp lý). Đã quan sát trên UVC cam SunplusIT (`1bcf:28cc`); cách chữa duy nhất đã verify (không cần reboot) là power-cycle cổng USB.

Cả hai watchdog ISP (freeze và loạn màu) dùng chung một thang leo qua `_recover_isp_fault()`:

- **Trigger** — ≥3 lần reopen do ISP fault (`_ISP_FAULT_ESCALATE_COUNT`) trong cửa sổ trượt 10 phút (`_ISP_FAULT_WINDOW_S`, 600s). Reopen do `read()`-fail **không** được tính.
- **Resolve USB path** — động, không hardcode: đi ngược chuỗi parent sysfs từ `/sys/class/video4linux/video<N>/device` tới node có `idVendor` (chính là USB device), lấy basename (ví dụ `1-1`). Nếu camera không phải USB (ví dụ sensor CSI) → bỏ qua leo thang, log lý do và giữ nguyên đường reopen thường.
- **Power-cycle** — ghi bus path vào `/sys/bus/usb/drivers/usb/unbind`, đợi ~3s (`_USB_REBIND_DELAY_S`), ghi vào `.../bind` (HAL chạy root), rồi đợi tối đa 15s (`_USB_DEVNODE_TIMEOUT_S`) cho `/dev/video<N>` enumerate lại trước khi trả quyền cho `_reopen_with_backoff()`. Best-effort: lỗi sysfs nào cũng chỉ log rồi fallback về reopen thường.
- **Cooldown** — tối đa 1 lần power-cycle mỗi 10 phút (`_USB_POWER_CYCLE_COOLDOWN_S`); camera chết hẳn không được kéo loop unbind/bind vô hạn. Trong thời gian cooldown, fault vẫn đi đường reopen thường.
- **Log** — `Camera USB power-cycle (ISP deep-stuck: N ISP-fault reopens in Ws)`.

## Edge Cases

- **Guard mode + camera off**: ✅ Done — guard SKILL.md bước 1: `[HW:/camera/enable:{}]` trước khi enable guard. Override manual disable.
- **Face enroll khi camera off**: `/face/enroll` dùng uploaded image, không dùng live camera. Không conflict.
- **Snapshot request khi camera off**: Trả 503 "Camera disabled". Agent xử lý gracefully.
- **Nhiều trigger liên tiếp**: Debounce camera start/stop. `camera_capture.start()` đã handle "already started".
- **Sound spike false positive loop**: Sau auto-on, nếu không detect face trong 30s → auto-off lại.
