# Vision Tracking — Theo dõi vật thể bằng servo

Lamp có thể theo dõi và hướng theo bất kỳ vật thể nào mà người dùng gọi tên. Hai giai đoạn: YOLOWorld API phát hiện vật thể theo tên, TrackerVit bám theo real-time.

## Kiến trúc

```
User: "Lamp, nhìn theo cái ly đi"
         |
    POST /servo/track {"target": "cup"}
         |
    1. YOLOWorld API: frame + "cup" → bbox [x,y,w,h]  (~1-2s, RunPod GPU)
         |
    2. TrackerVit init trên bbox
         |
    3. Vòng lặp tracking @ 7 FPS (cadence move-then-freeze)
         |  lấy frame (servo đứng yên) → TrackerVit update → nudge → đợi servo settle
         |
    4. Vật di chuyển → servo bám theo (yaw + 3 pitch joints)
         |
    5. Confidence < 0.3 trong 5 frame → tự động dừng + giữ servo tại vị trí
```

### Tại sao chọn move-then-freeze (thay vì high-FPS chasing)

Các phiên bản trước chạy loop 20 FPS, gửi nudge mỗi 50ms. Hai vấn đề:

1. **Camera ego-motion blur.** Camera gắn trên lamp đang di chuyển. Lệnh servo gửi nhanh hơn khả năng thực thi của motor → frame bị mờ hoặc lệch so với pose thực tế. Tracker tính bbox từ frame không match với servo hiện tại → nudge overshoot.
2. **Command stacking.** Nudge nhỏ (~0.5°) mỗi 50ms chồng lên nhau nhanh hơn motor có thể đạt target → hunting và giật.

Thiết kế hiện tại: đọc 1 frame, quyết định 1 nudge, gửi lệnh, rồi đợi servo hoàn thành chuyển động vật lý (~80ms) trước khi đọc frame kế tiếp. Mỗi frame đều sắc nét và tọa độ match với pose. Ít lệnh hơn, bước lớn có chủ đích, không hunting.

### Tại sao không có periodic YOLO re-detect

Các phiên bản trước gọi YOLOWorld mỗi 5 giây khi đang tracking để sửa drift. Đã bỏ vì YOLO round-trip mất 1-2 giây, trong đó:

- Servo vẫn di chuyển → bbox trả về ở tọa độ không còn match frame hiện tại.
- Bản thân vật thể có thể đã di chuyển.
- Scene có thể thay đổi bất kỳ.

Dùng bbox đó re-init tracker gây hại nhiều hơn lợi. Drift giờ được xử lý bởi TrackerVit confidence: nếu xuống dưới threshold 5 frame thì dừng hẳn, caller tự re-issue lệnh follow.

### Phát hiện: YOLOWorld API

Phát hiện vật thể open-vocabulary — detect bất kỳ vật nào bằng tên, không giới hạn class cố định.

- **Endpoint:** `{DL_BACKEND_URL}/detect/yoloworld`
- **Auth:** header `x-api-key` từ `DL_API_KEY` config
- **Request:** `{"image_b64": "...", "classes": ["cup"]}`
- **Response:** `[{"class_name": "cup", "xywh": [cx, cy, w, h], "confidence": 0.98}]`
- **Tốc độ:** ~1-2s (RunPod GPU)

Tự động gọi khi `POST /servo/track` không có `bbox`. Có thể truyền bbox thủ công để bỏ qua detection.

### Tracking: TrackerVit

Bám theo vật thể real-time sau khi phát hiện.

## Tracker: TrackerVit

**Model:** `lelamp/service/tracking/vittrack.onnx` (714KB, nằm trong repo)

| Tính năng | Giá trị |
|-----------|---------|
| Tốc độ | ~10-20ms/frame trên Pi 5 |
| Confidence score | 0.0-1.0 mỗi frame |
| Xử lý scale | Tự điều chỉnh kích thước bbox |
| Phát hiện mất | Trả `ok=False` + score thấp khi vật biến mất |

**Fallback chain:** TrackerVit → CSRT (cần opencv-contrib) → KCF → MIL

## Điều khiển Servo

Tracking sử dụng cả 4 servo pitch/yaw:
- **base_yaw** (ID 1) — quay trái/phải (100% yaw)
- **base_pitch** (ID 2) — nghiêng lên/xuống, 55% pitch
- **elbow_pitch** (ID 3) — nghiêng lên/xuống, 30% pitch
- **wrist_pitch** (ID 5) — nghiêng lên/xuống, 15% pitch

Pitch chia đều trên 3 joint cánh tay (base 0.55 / elbow 0.30 / wrist 0.15). Tilt chính ở base, phụ ở elbow, ít ở wrist — giảm nhiễu cơ học và làm đầu đèn *dẫn* motion thay vì 3 joint giật giật cùng lúc.

**Khi đang tracking:**
- `_hold_mode = True` — suppress idle animation để tracker sở hữu servo.
- EMA smoothing trên tâm bbox (`EMA_ALPHA = 0.3`) — lọc jitter TrackerVit trước khi đổi sang độ.
- Fallback khi bbox nhảy (`BBOX_JUMP_PX = 120`) — nếu tracker báo tâm dịch >120px/frame, coi là glitch tạm thời, dùng tâm EMA-smoothed thay vì bỏ frame.
- YOLO re-detect định kỳ (`REDETECT_INTERVAL_S = 5.0`) — gọi YOLOWorld mỗi 5s để sửa drift bằng cách re-seed bbox.
- Re-sync vị trí bus mỗi cycle — đọc lại pose thật từ hardware, tránh cộng dồn delta stale khi có motion bên ngoài.

### Chuyển đổi Pixel sang Độ

```
Tâm frame: (320, 240) cho 640x480
Tâm vật thể: tracker bbox đã EMA-smoothed (alpha = 0.3)

dx = cx - 320   (dương = bên phải)
dy = cy - 240   (dương = bên dưới)

yaw_deg   = dx * 0.022   (clamp ±4.5°, bằng 0 nếu |dx| < 12)
pitch_deg = dy * 0.022   (clamp ±4.5°, bằng 0 nếu |dy| < 12)

Adaptive gain: khi |dx| hoặc |dy| > 120px thì nhân gain 1.3x
để bắt kịp nhanh mà không overshoot. Giữ 1.0 khi gần tâm.
```

### Hằng số Tuning

| Hằng số | Giá trị | Mô tả |
|---------|---------|-------|
| `DEG_PER_PX_YAW` | 0.022 | Độ mỗi pixel ngang |
| `DEG_PER_PX_PITCH` | 0.022 | Độ mỗi pixel dọc |
| `DEAD_ZONE_PX` | 12 | Bỏ qua offset nhỏ hơn giá trị này (chống rung) |
| `WAKE_ZONE_PX` | 40 | Khi đã settle, chỉ resume nudge khi object dịch vượt ngưỡng |
| `ADAPTIVE_GAIN_PX` | 120 | Offset vượt ngưỡng → boost gain bắt kịp |
| `ADAPTIVE_GAIN_MULT` | 1.3 | Hệ số nhân gain khi object xa tâm |
| `MAX_NUDGE_DEG` | 4.5 | Độ tối đa mỗi bước (tune cho TRACK_FPS=20) |
| `TRACK_FPS` | 20 | Tần suất vòng lặp tracking (~50ms/cycle) |
| `EMA_ALPHA` | 0.3 | Hệ số smoothing tâm bbox |
| `BBOX_JUMP_PX` | 120 | Ngưỡng bbox jump — fallback sang tâm EMA |
| `REDETECT_INTERVAL_S` | 5.0 | YOLO re-detect định kỳ để sửa drift |
| `CONFIDENCE_THRESHOLD` | 0.3 | Dưới ngưỡng này = "mất" |
| `MAX_LOW_CONFIDENCE_FRAMES` | 5 | Số frame confidence thấp liên tiếp trước khi dừng |
| `PITCH_WEIGHT_BASE/ELBOW/WRIST` | 0.55 / 0.30 / 0.15 | Pitch chia 3 joint |

### Giới hạn vị trí Servo

| Joint | Min | Max |
|-------|-----|-----|
| base_yaw | -135 | 135 |
| base_pitch | -90 | 30 |
| elbow_pitch | -90 | 90 |
| wrist_pitch | -90 | 90 |

## Phát hiện mất Target

TrackerVit cung cấp confidence scoring, khác với MIL/KCF chỉ drift âm thầm.

| Điều kiện | Hành động |
|-----------|-----------|
| `confidence < 0.3` trong 5 frame | Dừng — mất target |
| Bbox > 3x kích thước ban đầu | Dừng — tracker drift/phình |
| Bbox > 50% diện tích frame | Dừng — tracker drift |
| Servo ở limit yaw/pitch + object vẫn lệch > 30% | Dừng — ngoài tầm |
| Tracking > 5 phút | Dừng — timeout tiết kiệm motor/CPU |
| `tracker.update()` trả `ok=False` | Tính là frame confidence thấp |

## API Endpoints

Tất cả dưới `/servo/track`.

### GET /servo/track/targets — Danh sách target gợi ý

```json
{"targets": ["person", "cup", "bottle", "glass", "phone", "laptop", ...]}
```

YOLOWorld là open-vocabulary — bất kỳ text nào cũng được, danh sách chỉ là gợi ý.

### POST /servo/track — Bắt đầu tracking

`target` chấp nhận chuỗi đơn hoặc list nhiều candidate label. Khi truyền list, YOLOWorld đánh giá tất cả label và chọn detection có confidence cao nhất. Hữu ích khi caller (ví dụ LLM skill) không chắc label chính xác.

```json
// Tự detect, 1 label
{"target": "cup"}

// Tự detect, list candidate label (ưu tiên từ LLM skill)
{"target": ["cup", "mug", "ly cà phê"]}

// Bbox thủ công (bỏ qua detection — target chỉ dùng để hiển thị)
{"bbox": [190, 50, 170, 300], "target": "cup"}

// Response
{"status": "ok", "tracking": true, "target": "cup | mug | ly cà phê", "bbox": [190, 50, 170, 300], "confidence": 1.0}
```

### POST /servo/track/stop — Dừng tracking

```json
{"status": "ok", "tracking": false}
```

### GET /servo/track — Kiểm tra trạng thái

```json
{"status": "ok", "tracking": true, "target": "ly nước", "bbox": [195, 55, 175, 295], "confidence": 0.612}
```

### POST /servo/track/update — Khởi tạo lại bbox

Re-init thủ công tracker với bbox mới mà không dừng phiên tracking.

```json
{"bbox": [250, 160, 75, 95], "target": "ly nước"}
```

Lưu ý: không có re-detect YOLO định kỳ tự động — caller tự quyết định khi nào re-init. Xem "Tại sao không có periodic YOLO re-detect" ở trên.

## Luồng End-to-End

### Trường hợp thành công

```
1. User: "Lamp, nhìn theo cái ly"
2. Agent gọi POST /servo/track {"target": "cup"}
3. HAL nội bộ:
   a. Snapshot 1 frame và giữ lại
   b. Gửi frame đó cho YOLOWorld API → lấy bbox (~1-2s)
   c. TrackerVit init dùng *cùng* frame + bbox (tọa độ match)
   d. Bắt đầu vòng lặp move-then-freeze
4. Servo bám theo ly nước real-time (confidence ~0.5-0.7)
5. User: "Thôi đi" → agent gọi POST /servo/track/stop
6. Servo giữ tại vị trí hiện tại (không snap về idle)
```

### Tự dừng khi mất

```
1. Vật rời khỏi frame hoặc bị che
2. TrackerVit confidence giảm dưới 0.3
3. Sau 5 frame confidence thấp liên tiếp → tự dừng
4. Servo giữ tại vị trí cuối (không snap về idle)
5. Agent có thể thông báo user hoặc tự re-detect
```

## Camera Stream Overlay

Khi tracking, MJPEG stream (`/camera/stream`) vẽ thêm:
- Khung xanh lá bao quanh vật thể
- Tên target phía trên khung

## Web UI

Camera section hiển thị:
- **Vision Tracking card** — input target, input bbox, nút Start/Stop/Status
- **Stream badge** — "LIVE" hoặc "TRACKING: {target}"
- **Confidence** — hiện trong panel thông tin tracking
- **Polling** — trạng thái refresh mỗi 3 giây

## Phụ thuộc

- `opencv-python>=4.8.0` (đã có trong `pyproject.toml`)
- `vittrack.onnx` — nằm trong repo tại `lelamp/service/tracking/vittrack.onnx`
- `requests` (đã có trong project)
- **YOLOWorld API** — RunPod DL backend tại `DL_BACKEND_URL/detect/yoloworld`

## Tương tác với các hệ thống khác

| Hệ thống | Khi đang tracking | Sau tracking |
|----------|-------------------|--------------|
| Servo idle animation | Bị chặn (`_hold_mode`) | Tiếp tục |
| `/servo/play` | Bị chặn bởi `_hold_mode` | Tiếp tục |
| Sensing (face, motion) | Tiếp tục — chia sẻ camera | Tiếp tục |
| Camera stream overlay | Vẽ bbox xanh lá | Stream bình thường |
| TTS | Tiếp tục bình thường | Tiếp tục bình thường |

## Bước tiếp theo

- **OpenClaw skill** — `track/SKILL.md` để agent gọi tracking bằng giọng nói
- ~~**Re-detect định kỳ**~~ — đã thử, rollback. Round-trip YOLO 1-2s desync với chuyển động servo (xem "Tại sao không có periodic YOLO re-detect" ở trên)
- **PID control** — servo phản hồi mượt hơn thay vì chỉ proportional
- **Nhiều vật thể** — track nhiều vật, chuyển đổi giữa chúng
