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

Zoom được apply **trong capture loop** (`devices/video_capture_device.py::_video_capture_loop`) ngay sau rotate, trước khi set `last_response`. Loop center-crop frame theo `1/zoom` rồi resize về kích thước gốc, nên mọi consumer downstream đều đọc cùng buffer đã zoom:

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

## Edge Cases

- **Guard mode + camera off**: ✅ Done — guard SKILL.md bước 1: `[HW:/camera/enable:{}]` trước khi enable guard. Override manual disable.
- **Face enroll khi camera off**: `/face/enroll` dùng uploaded image, không dùng live camera. Không conflict.
- **Snapshot request khi camera off**: Trả 503 "Camera disabled". Agent xử lý gracefully.
- **Nhiều trigger liên tiếp**: Debounce camera start/stop. `camera_capture.start()` đã handle "already started".
- **Sound spike false positive loop**: Sau auto-on, nếu không detect face trong 30s → auto-off lại.
