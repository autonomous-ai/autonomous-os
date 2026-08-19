# Vision Tracking — Theo dõi vật thể bằng servo

Lamp có thể theo dõi và hướng theo bất kỳ vật thể nào mà người dùng gọi tên. Một detector tìm vật thể theo tên và seed một ViT tracker, sau đó một vòng lặp vision tốc độ cao bám theo nó real-time, trong khi một servo worker tách rời điều khiển đầu trượt mượt về phía target.

Toàn bộ code tracking nằm trong package `hal/drivers/tracking/`:

| Module | Nội dung |
|--------|----------|
| `tracker_service.py` | `TrackerService` — vòng đời session (start/stop/status/update_bbox) + vòng lặp vision nhanh |
| `constants.py` | Toàn bộ knob tuning (các module khác import với alias `C`) |
| `detection.py` | `ObjectDetector` — chuỗi YuNet face / YOLOv8n local / YOLOWorld remote |
| `vit_tracker.py` | Backend tracker OpenCV (`create_tracker`, `vit_init`, `vit_update`, `get_tracking_score`) |
| `servo_follow.py` | `ServoFollower` — servo goal + thread worker SmoothDamp |
| `filters.py` | `AlphaBetaFilter2D`, `PID`, `smooth_damp`, `soft_deadband` |
| `frame_utils.py` | `downscale`, `scale_bbox` (map tọa độ) |

## Kiến trúc

```
User: "Lamp, follow the cup"
         |
    POST /servo/track {"target": "cup"}
         |
    1. Freeze servos 0.3s → grab a sharp frame
         |
    2. Detect the object (YuNet face | local YOLOv8n | remote YOLOWorld) → bbox
         |
    3. TrackerVit init on the bbox
         |
    4. Two decoupled threads:
         |   a. Vision loop @ FAST_LOOP_FPS (15):
         |        ViT update → alpha-beta centroid filter → soft dead zone
         |        → PID + velocity feedforward → publish an absolute servo goal
         |        (background YOLO re-detect every 1.5s corrects drift)
         |   b. Servo worker: SmoothDamp glide toward the latest goal
         |        (ease-in/ease-out; gộp các thay đổi setpoint rất nhỏ)
         |
    5. Lost / bloated / no-detect / timeout → auto-stop, hold or return to zero
```

Vòng lặp vision không bao giờ block chờ motor di chuyển: nó publish một servo goal *tuyệt đối* rồi chuyển ngay sang frame kế tiếp. Servo worker sở hữu chuyển động vật lý và liên tục ease về phía goal mới nhất. Đây chính là cái giữ cho cả fps của tracker cao lẫn chuyển động đầu mượt.

### Vision downscale, tính toán ở độ phân giải gốc

Camera chạy **1280×720**. Mọi thành phần vision nặng — ViT tracker và cả ba detector — đều chạy trên frame đã downscale xuống `VISION_MAX_WIDTH` (640 px rộng, 0.5× → ¼ số pixel) để tăng tốc. Mỗi bbox chúng tạo ra được map **ngược về tọa độ gốc 1280×720** trước bất kỳ phép tính servo/PID nào (`frame_utils.downscale` / `scale_bbox`, `vit_tracker.vit_init` / `vit_update`, và `detect_object` là transparent). Vì hợp đồng tọa độ ở phía sau luôn là độ phân giải gốc, nên không hằng số nào được tune theo pixel (PID gains, gates, dead zones, ngưỡng feedforward) thay đổi khi hệ số downscale thay đổi. Đặt `VISION_MAX_WIDTH = 0` để tắt.

## Detection

`detect_object(frame, target)` trả về một bbox `(x, y, w, h)` theo tọa độ camera gốc, thử ba đường theo thứ tự:

| Path | Detector | Khi nào | Tốc độ (A523) |
|------|----------|---------|---------------|
| 0 | **YuNet** face detector (`face_detection_yunet_2023mar.onnx`) | target ∈ {`face`, `human face`, `khuôn mặt`, `mặt`} | ~30 ms |
| 1 | **Local YOLOv8n** (COCO classes, `yolov8n.pt`, imgsz=320) | target map tới một COCO class | ~260–770 ms |
| 2 | **Remote YOLOWorld** open-vocab (`{DL_BACKEND_URL}/detect/yoloworld`) | target không thuộc COCO, hoặc local miss (fallback) | ~1.3–2.8 s |

- COCO không có class hand/face, nên `hand`/`face` cố ý rơi xuống YuNet/YOLOWorld thay vì map tới `person` (vốn khóa vào toàn thân).
- Khi local-YOLO miss, code fallback về remote YOLOWorld, **throttle** tối đa một lần mỗi `REMOTE_FALLBACK_MIN_INTERVAL` (2.0 s) để một target thật sự không thể thấy không gọi remote mỗi lần redetect.
- Bộ lọc chất lượng detection: confidence ≥ `DETECT_MIN_CONFIDENCE` (0.15), diện tích nằm giữa `DETECT_MIN_AREA_RATIO` (0.3%) và `DETECT_MAX_AREA_RATIO` (80%) của frame.
- **Chống nhầm vật giống nhau (đường local)** — YOLO local detect **mở** (không filter `classes=`) để các class cạnh tranh còn hiện diện, sau đó: (a) cụm dễ nhầm cell phone / mouse / remote cần conf ≥ 0.35 (`_CONFUSABLE_CONF_FLOOR`) thay vì 0.15 chung; (b) **cross-class disambiguation** — nếu một box class khác đè lên candidate (IoU ≥ 0.5) với confidence *cao hơn*, candidate bị từ chối ("đó chắc là con chuột, không phải cái điện thoại anh hỏi") và code rơi xuống remote fallback. Floor 0.35 chỉ áp cho detect lúc *bắt đầu* session (`strict=True`); redetect giữa session dùng floor chung 0.15 (phone đang phẩy nhanh thường chỉ reconfirm ở conf 0.2–0.3, và các gate reinit đã bảo vệ lock) — cross-class check giữ nguyên cả hai chế độ.

Weights được check vào repo (`hal/drivers/tracking/models/`) nên deploy chỉ một lần rsync và Pi không bao giờ cần internet lúc boot để bắt đầu tracking.

## Tracker: TrackerVit

**Model:** `hal/drivers/tracking/models/vittrack.onnx` (đã check vào repo)

| Tính năng | Giá trị |
|-----------|---------|
| Tốc độ | ~15–25 ms/frame trên frame đã downscale |
| Confidence score | `getTrackingScore()` 0.0–1.0 mỗi frame |
| Xử lý scale | Tự động điều chỉnh kích thước bbox |
| Phát hiện mất | Trả về `ok=False` + score thấp khi vật thể biến mất |

**Chuỗi fallback:** TrackerVit → CSRT → KCF → MIL. Chỉ ViT phơi ra confidence score (dùng cho phát hiện ghost-lock); các tracker khác trả về 1.0.

## Servo Control

Tracking điều khiển 4 joint:

- **base_yaw** (ID 1) — pan trái/phải (100 % của yaw)
- **base_pitch** (ID 2) — tilt lên/xuống, 10 % của pitch
- **elbow_pitch** (ID 3) — tilt lên/xuống, 90 % của pitch
- **wrist_pitch** (ID 5) — tilt lên/xuống, 0 %

Pitch được dồn vào elbow (`PITCH_WEIGHT_ELBOW = 0.90`). Thực nghiệm cho thấy chỉ các joint xoay-thuần mới đưa vật thể về giữa; base/wrist chủ yếu tịnh tiến camera (kinematic coupling), nên weight của chúng thấp/bằng không. Chiều dương của motor elbow bị đảo ở phần cứng, nên đóng góp của nó mang `ELBOW_PITCH_SIGN = -1.0`.

### Control law (vision loop → servo goal)

Mỗi frame vòng lặp biến bbox của tracker thành một servo goal tuyệt đối:

1. **Alpha-beta filter trên centroid** (`AlphaBetaFilter2D`) — một Kalman steady-state vận tốc-hằng. Làm mượt jitter, coast qua các frame bị rớt/rác bằng prediction, gate các cú teleport outlier (`AB_GATE_PX`), và phơi ra ước lượng vận tốc. Một velocity lead (`AB_LEAD_S = 0.20 s`) nhắm trước mặt target — "lead room" kiểu điện ảnh.
2. **Dead zone 3 tầng** (`soft_deadband`) — liên tục tại cả hai ranh giới: zero thật bên trong ±`DEAD_ZONE_INNER_PCT` (2%, PID xả và, khi không có lệnh bám theo vận tốc, follower được retarget về pose *hiện tại* để không thể tiếp tục chạy theo goal cũ); **dải creep** tới rìa ngoài (độ dốc `DEAD_ZONE_CREEP_GAIN` = 0.12) để camera lững thững trôi về tâm thay vì đứng khựng — dừng cứng ở đây tạo cảm giác "camera an ninh" đi–dừng–đi; ngoài rìa ngoài là error đầy đủ.
3. **Velocity feedforward chính, PID phụ (smooth pursuit)** — lệnh chủ đạo là feedforward tỉ lệ với vận tốc pixel đo được của target (`VFF_GAIN` = 0.9): camera *khớp tốc độ vật* như mắt người bám mượt, kể cả khi sai số vị trí bằng 0. PID time-aware chống windup (KP cố tình nhỏ: 0.015 yaw / 0.02 pitch) chỉ sửa phần dư vị trí. Vật đang ở giữa nhưng di chuyển thì vẫn pan tiếp (không đóng băng trong dead zone). Output tổng clamp về `PID_OUTPUT_MAX_DEG` (5°).
4. **Hai profile saccade / pursuit** — mô phỏng mắt người: lệch > `SACCADE_OFFSET_FRAC` (22% bề ngang frame) thì follow worker chuyển sang profile **saccade** (`SACCADE_SMOOTH_TIME` 0.20s, `SACCADE_MAX_SPEED_DPS` 100) để lia nhanh về chỗ; lệch nhỏ dùng profile **pursuit** nặng (`SERVO_SMOOTH_TIME` 0.32s, `SERVO_MAX_SPEED_DPS` 55) — quán tính fluid-head. Một profile thỏa hiệp làm cả hai việc đều dở. State log ra `SACCADE` vs `CHASING`.
5. **Publish goal** — joint target tuyệt đối kết quả được giao cho servo worker (non-blocking).

### Servo worker (SmoothDamp follower)

`ServoFollower` (`servo_follow.py`) chạy worker trên thread riêng và liên tục ease các joint về phía goal mới nhất bằng **SmoothDamp** (`smooth_damp`, một follower critically-damped): mỗi joint mang vận tốc riêng, nên mọi cú di chuyển đều tăng tốc mượt và ease-out vào target, và một goal mới đến giữa cú di chuyển sẽ retarget mà không giật restart — chuyển động "film camera" điện ảnh. Worker thức dậy theo nhịp bị giới hạn bởi `SERVO_SUBSTEP_SLEEP` (30 ms), nhưng tính SmoothDamp từ thời gian monotonic thực tế đã trôi qua và cap ở `SERVO_SUBSTEP_MAX_DT_S` (60 ms) sau một lần scheduler/serial bị stall. Nó chỉ gửi một lệnh bus đa-joint khi có ít nhất một servo command đổi ít nhất `SERVO_COMMAND_MIN_DELTA` (0.08), chỉ gộp các thay đổi normalized rất nhỏ; final target luôn được gửi một lần.

Chuyển động phần cứng khi tracking: ở mỗi lần bắt đầu session, HAL luôn ghi rõ `TRACKING_GOAL_VELOCITY = 0` (unlimited) để xóa giới hạn vận tốc còn sót lại từ mode trước. Vì vậy software profile sở hữu tốc độ; cap cũ 150 steps/s ≈ 13°/s đã làm các đường cong SmoothDamp phẳng thành một chuyển động chậm đều. `TRACKING_ACCELERATION = 30` tạo ramp phần cứng nhẹ nhàng. Lượt trượt về zero bị cap bởi `TRACKING_RETURN_VELOCITY` (200 steps/s); sau đó khôi phục default nhạy bén.

### Sửa drift & quản lý lock

- **Background YOLO re-detect** mỗi `YOLO_REDETECT_S` (1.5 s) trên một worker thread (không bao giờ block fast loop; kết quả gửi qua một queue `maxsize=1`). Bị buộc chạy ngay khi vật thể tiến gần rìa frame (>25 %) hoặc lần CSRT miss đầu tiên.
- **Miss-coast** — tracker miss trong lúc vật đang di chuyển (phẩy nhanh, motion blur) thì loop pan tiếp theo vận tốc alpha-beta cuối cùng tối đa `MISS_COAST_FRAMES` (6) frame miss (state `COAST`) rồi mới rơi về search sweep. Đứng khựng ngay miss đầu tiên là bảo đảm vật nhanh thoát khỏi frame trước khi redetect kịp về.
- **Reinit gating (kiểu SORT/ByteTrack)** — một re-detect chỉ reinit tracker khi nó đã rõ ràng phân kỳ, để tránh churn reinit làm servo quật qua lại:
  - **Area gate** `YOLO_AREA_GATE_MULT` (4.0) — loại một detection có diện tích >4× hoặc <¼ median của 5 cái gần nhất; đừng reinit về nó.
  - **Reinit debounce** `REINIT_COOLDOWN_S` (0.5 s) — rate-limit reinit; chỉ bypass khi lock rõ ràng đã mất (`center_dist > frame_diag × LOST_CENTER_FRAC` = 0.5).
- **Bbox-trust guard (bloat hold)** — khi ViT lock tan thành một box quá khổ thì centroid là rác, nên servo giữ nguyên thay vì đuổi theo nó:
  - `BBOX_FREEZE_RATIO` (1.0) — bbox ≥ diện tích cả frame ⇒ ViT đã tan.
  - `BLOAT_HOLD_MULT` (3.0) — bbox > 3× diện tích lock tin cậy gần nhất ⇒ hold và buộc re-detect.
- **Sàn confidence cho servo** — confidence ViT < `SERVO_MIN_CONF` (0.25) thì giữ servo (`LOW-CONF-HOLD`) kể cả khi detector vẫn đang confirm target; trước đây vùng conf 0.15–0.4 kèm confirm mới là vùng mù khiến servo đuổi theo một lock yếu (thường là ma). Tracker vẫn update và PID chạy lại khi confidence hồi.
- **Detector-gated trust** — nếu không detector nào xác nhận trong `TRUST_TRACKER_S` (2.5 s) và confidence ViT < `TRACKER_TRUST_CONF` (0.4), giữ servo (`WAIT-YOLO`) thay vì đuổi một bóng ma; confidence ViT cao vẫn tiếp tục fire ngay cả khi không có detector confirm mới.
- **Hold là hold thật** — mọi trạng thái hold (`LOW-CONF-HOLD`, `WAIT-YOLO`, `BLOAT-HOLD`, frame bị skip do low-confidence) đều retarget follow worker về pose *hiện tại* (`ServoFollower.hold()`). Trước đây hold chỉ ngừng publish goal mới, worker vẫn glide tiếp về goal cũ — nên arm vẫn "đuổi ma" thêm một nhịp sau khi lock đã hỏng.

### Chuyển đổi Pixel-sang-Degree

```
deg_per_px = CAMERA_FOV_DEG / frame_width          (same on both axes for square pixels)

dx = filtered_lead_x - frame_width/2   (positive = right)
dy = filtered_lead_y - frame_height/2  (positive = below)

yaw_step         = clamp(PID(soft_deadband(dx)) + VFF·vx·deg_per_px·dt,  ±5°)
pitch_correction = clamp(PID(soft_deadband(dy)) + VFF·vy·deg_per_px·dt,  ±5°)
```

### Hằng số tuning

| Hằng số | Giá trị | Mô tả |
|---------|---------|-------|
| `VISION_MAX_WIDTH` | 640 | Chiều rộng downscale cho ViT + detectors (0 = tắt) |
| `FAST_LOOP_FPS` | 15 | Tần số vòng lặp vision |
| `CAMERA_FOV_DEG` | 60 | FOV ngang, cho px→deg |
| `DEAD_ZONE_INNER_PCT` | 0.02 | Dải zero thật (servo nghỉ) |
| `DEAD_ZONE_CREEP_GAIN` | 0.12 | Độ dốc trôi lười trong dải creep |
| `DEAD_ZONE_YAW_PCT` / `_PITCH_PCT` | 0.07 / 0.05 | Soft dead zone theo tỉ lệ frame |
| `PID_YAW_KP` / `PID_PITCH_KP` | 0.015 / 0.02 | PID proportional gains |
| `PID_OUTPUT_MAX_DEG` | 5.0 | Số độ tối đa mỗi lần fire (yaw & pitch kết hợp) |
| `AB_ALPHA` / `AB_BETA` | 0.6 / 0.2 | Alpha-beta position/velocity gains |
| `AB_GATE_PX` | 200 | Loại một cú teleport centroid vượt residual này |
| `AB_LEAD_S` | 0.20 | Velocity lead (nhắm vượt trước target) |
| `VFF_GAIN` | 0.9 | Tỉ lệ vận tốc target được feed forward |
| `VFF_MAX_DT_S` | 0.20 | Cap trên dt mỗi lần fire cho feedforward |
| `VFF_MOVING_MIN_PXS` | 40 | Tốc độ target mà trên đó target ở giữa vẫn tiếp tục pan |
| `SERVO_SMOOTH_TIME` / `SERVO_MAX_SPEED_DPS` | 0.32 / 55 | Profile pursuit (nặng, fluid-head) |
| `SACCADE_SMOOTH_TIME` / `SACCADE_MAX_SPEED_DPS` | 0.20 / 100 | Profile saccade (lia nhanh) |
| `SACCADE_OFFSET_FRAC` / `SACCADE_EXIT_FRAC` | 0.22 / 0.12 | Ngưỡng vào/ra saccade (hysteresis — hết nhấp nháy cap tốc độ ở ranh giới) |
| `SERVO_SUBSTEP_SLEEP` / `SERVO_SUBSTEP_MAX_DT_S` | 0.030 / 0.060 | Chu kỳ worker thức dậy / bước SmoothDamp đo thực tế lớn nhất sau stall |
| `SERVO_COMMAND_MIN_DELTA` | 0.08 | Chỉ gộp các thay đổi normalized rất nhỏ; final target được gửi một lần |
| `TRACKING_GOAL_VELOCITY` | 0 (unlimited) | Được ghi rõ khi session bắt đầu để xóa hardware cap cũ; các profile SmoothDamp sở hữu speed envelope (150 steps/s ≈ 13°/s đã làm mọi đường cong ease thành một chuyển động chậm đều). `TRACKING_RETURN_VELOCITY` (200) chỉ cap lượt trượt về zero |
| `TRACKING_ACCELERATION` | 30 | Ramp gia tốc phần cứng |
| `PITCH_WEIGHT_BASE/ELBOW/WRIST` | 0.10 / 0.90 / 0.0 | Phân bổ pitch qua các joint |
| `ELBOW_PITCH_SIGN` | -1.0 | Chiều elbow (phần cứng đảo) |
| `YOLO_REDETECT_S` | 1.5 | Khoảng thời gian background re-detect |
| `YOLO_AREA_GATE_MULT` | 4.0 | Loại re-detect có diện tích outlier |
| `REINIT_COOLDOWN_S` | 0.5 | Số giây tối thiểu giữa các lần reinit tracker |
| `BBOX_FREEZE_RATIO` | 1.0 | Bbox ≥ frame ⇒ ViT đã tan (hold) |
| `BLOAT_HOLD_MULT` | 3.0 | Bbox > 3× lock tin cậy ⇒ hold |
| `CONFIDENCE_THRESHOLD` | 0.15 | Dưới mức này = frame low-confidence |
| `LOW_CONF_WINDOW` / `LOW_CONF_STOP_COUNT` | 15 / 8 | Cửa sổ trượt: ≥8 frame low trong 15 frame gần nhất → dừng (đếm liên-tiếp cũ bị reset bởi mỗi frame nhấp nháy vượt ngưỡng, khiến ghost lock sống mãi) |
| `SERVO_MIN_CONF` | 0.25 | Sàn confidence để fire servo PID |
| `TRACKER_TRUST_CONF` / `TRUST_TRACKER_S` | 0.4 / 2.5 | Detector-gated trust (xem trên) |
| `YOLO_MAX_MISS` | 30 | Số lần CSRT miss liên tiếp trước khi retry |
| `MAX_TRACK_DURATION_S` | 300 | Timeout tự động dừng (5 phút) |
| `_LOCAL_IMGSZ` | 320 | Kích thước inference local YOLO (640 → 1.3–2.9 s, quá chậm) |

Mọi knob nằm trong `hal/drivers/tracking/constants.py`. (Đường proportional chết `GIMBAL_*` / `EMA_ALPHA` đã bị xoá khi tách package.)

### Giới hạn vị trí servo

| Joint | Min | Max |
|-------|-----|-----|
| base_yaw | -135 | 135 |
| base_pitch | -90 | 30 |
| elbow_pitch | -90 | 90 |
| wrist_pitch | -90 | 90 |

## Điều kiện tự động dừng

| Điều kiện | Hành động |
|-----------|-----------|
| `confidence < 0.15` trong ≥8/15 frame gần nhất (cửa sổ trượt) | Dừng — mất target |
| Bbox co nhỏ dưới `DETECT_MIN_AREA_RATIO` | Dừng — ghost-lock trên một mảnh nhỏ |
| Bbox tràn frame + không detect trong 3 s | Buộc retry, rồi dừng nếu không phục hồi |
| Không detector confirm trong `STOP_NO_YOLO_S` (20 s) | Dừng — ghost tracking |
| CSRT miss `YOLO_MAX_MISS` (30) sau `MAX_TRACKING_RETRIES` (4) | Dừng — vật thể biến mất |
| Thời lượng tracking > 5 phút | Dừng — timeout để tiết kiệm motor/CPU |
| Single-click từ nút GPIO hoặc TTP223 | Dừng — user chủ động huỷ attention |

Lưu ý: một bbox lớn (ví dụ một người lấp đầy frame) **không** phải điều kiện dừng — PID chạy theo centroid, không phải kích thước bbox, nên một vật thể ở gần vẫn track. Khi tracking kết thúc, cánh tay trượt về zero ở tốc độ tracking (không snap).

### Tự động dừng khi mất kết nối gateway/network

Object tracking được điều khiển bởi các cập nhật vision từ xa từ agent/cloud. Khi gateway WebSocket disconnect (mất cloud hoặc internet), thiết bị tự động dừng mọi servo tracking đang chạy — `runtimes/openclaw/service_ws.go` gọi `hal.StopServoTracking()` → HAL `POST /servo/track/stop` (best-effort, được guard bởi `SetUpCompleted`). Nếu không có cập nhật từ xa mới, tracking tiếp tục sẽ cứ nhắm thân về một target cũ mà nó không còn sửa được, nên nó bị dừng như một phản xạ an toàn. Idle animation local vẫn tiếp tục (thiết bị vẫn "sống", không đóng băng) và phục hồi (`/servo/track/stop`, stop/release) vẫn khả dụng. Xem `robots/lamp/SAFETY.md` → `## fail-safe states` (dòng Network/gateway loss, enforced).

## API Endpoints

Tất cả nằm dưới `/servo/track`.

### GET /servo/track/targets — Liệt kê target gợi ý

```json
{"targets": ["person", "cup", "bottle", "glass", "phone", "laptop", ...]}
```

Detection là open-vocabulary qua YOLOWorld (và YuNet cho khuôn mặt) — mọi text đều được, danh sách này chỉ là gợi ý.

### POST /servo/track — Bắt đầu tracking

`target` nhận hoặc một string đơn hoặc một list các label ứng viên. Khi truyền một list, label không rỗng đầu tiên được dùng. Hữu ích khi caller (ví dụ một LLM skill) không chắc label chính xác nào sẽ match.

```json
// Auto-detect, single label
{"target": "cup"}

// Auto-detect, list of candidate labels (preferred from LLM skills)
{"target": ["cup", "mug", "coffee cup"]}

// Manual bbox (skip detection — target is for display only)
{"bbox": [190, 50, 170, 300], "target": "cup"}

// Response
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [190, 50, 170, 300],
  "confidence": 1.0
}
```

### POST /servo/track/stop — Dừng tracking

```json
{"status": "ok", "tracking": false}
```

### GET /servo/track — Kiểm tra trạng thái

```json
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [195, 55, 175, 295],
  "confidence": 0.612
}
```

### POST /servo/track/update — Re-init bbox

Re-init thủ công tracker với một bbox mới mà không dừng session (background YOLO re-detect tự xử lý drift; cái này dành cho caller muốn kiểm soát tường minh).

```json
{"bbox": [250, 160, 75, 95], "target": "cup"}
```

## Luồng End-to-End

### Happy path

```
1. User: "Lamp, follow the cup"
2. Agent calls POST /servo/track {"target": "cup"}
3. HAL internally:
   a. Freezes servos 0.3s and snapshots a sharp frame
   b. Detects "cup" (local YOLOv8n, or remote YOLOWorld) → bbox
   c. TrackerVit init uses the same frame + bbox (coordinates match)
   d. Starts the vision loop + servo worker
4. Servo pans smoothly to follow the cup, background YOLO corrects drift
5. User: "OK stop" → agent calls POST /servo/track/stop
6. Servo glides back to zero
```

### Tự động dừng khi mất

```
1. Object leaves frame or is occluded
2. Confidence TrackerVit ở dưới 0.15 trong phần lớn cửa sổ gần nhất (hoặc lock ViT tan rã)
3. Background YOLO can't re-find it → after the guards trip → auto-stop
4. Arm returns to zero
5. Agent can notify user or re-issue the follow command
```

## Overlay Camera Stream

Khi tracking đang chạy, MJPEG stream (`/camera/stream`) vẽ:
- Bounding box màu xanh quanh vật thể được track
- Label target phía trên box

## Web UI

Camera section hiển thị:
- **Vision Tracking card** — target input, bbox input, các nút Start/Stop/Status
- **Stream badge** — "LIVE" hoặc "TRACKING: {target}"
- **Confidence** — hiện trong panel thông tin tracking
- **Polling** — status refresh mỗi 3 giây

## Dependencies

- `opencv-python>=4.8.0` (đã có trong `pyproject.toml`)
- `ultralytics` — inference local YOLOv8n
- `vittrack.onnx`, `yolov8n.pt`, `face_detection_yunet_2023mar.onnx` — đã check vào `hal/drivers/tracking/models/`
- `requests` (đã có trong project)
- **YOLOWorld API** — DL backend tại `{DL_BACKEND_URL}/detect/yoloworld` (chỉ open-vocab fallback)

## Tương tác với các hệ thống khác

| Hệ thống | Trong khi tracking | Sau khi tracking |
|----------|--------------------|------------------|
| Servo idle animation | Bị chặn (`_hold_mode`) | Tiếp tục |
| `/servo/play` | Bị chặn bởi `_hold_mode` | Tiếp tục |
| Sensing (face, motion) | Tiếp tục — chia sẻ camera | Tiếp tục |
| Camera stream overlay | Vẽ bbox xanh | Stream bình thường |
| TTS | Tiếp tục bình thường | Tiếp tục bình thường |

## Ghi chú hiệu năng

- Sàn CPU của fast-loop trên Allwinner A523 là chi phí ViT inference + detector; frame downscale (`VISION_MAX_WIDTH`) và local imgsz=320 là các đòn bẩy chính.
- Độ mượt chuyển động đến từ servo worker tách rời + SmoothDamp + velocity feedforward; alpha-beta filter + reinit gating giữ cho bản thân goal ổn định để follower không đuổi theo nhiễu.
- Vật thể nhỏ/xa (ví dụ một cái ly ở đầu phòng) có thể vượt độ phân giải của cả detector local lẫn remote — đây là giới hạn perception, không phải bug điều khiển.
</content>
</invoke>

---

## Look-aim — ngắm đầu trước khi một câu hỏi thị giác chụp ảnh

Tách biệt với phần theo dõi vật thể ở trên, và do một trigger khác kích hoạt.

Tool `look` của realtime **không có tham số**: nó chụp đúng thứ đầu đang hướng tới
(`orchestrator.py` — *"the model just signals intent to look; the device grabs the current frame"*).
Nên một câu hỏi thị giác — *"tôi đang cầm gì đây?"* — có thể được trả lời rất tự tin từ một tấm ảnh
bức tường. `hal/drivers/tracking/aim.py` căn giữa đối tượng trước.

| | |
|---|---|
| **Trigger** | tool `look` được gọi — **không phải** hội thoại thông thường |
| **Phạm vi** | chỉ yaw |
| **Ngân sách** | `HAL_LOOK_AIM_DEADLINE_S` (0.8 s); hết hạn thì chụp từ đúng chỗ nó tới được |
| **Tắt** | `HAL_LOOK_AIM=false` |

Hội thoại thông thường không đổi: thân máy vẫn đứng yên suốt pha lắng nghe và suy nghĩ như trước.
Chỉ một lời gọi `look` mới giải phóng nó, vì đó đúng là khoảnh khắc thiết bị được yêu cầu tường minh
là hãy nhìn vào một vật.

**Thân máy được sở hữu trong suốt cả lượt look.** Từ lúc pha ngắm bắt đầu cho tới khi màn trập đóng,
`servo_ownership()` bật đúng cái khóa `_tracking_active` mà vision tracker vẫn dùng, khóa này chặn
**toàn bộ** animation servo của emotion (`routes/emotion.py`) và khiến vòng animation bỏ luôn bản ghi
đang phát dở.

Đây không phải phần đánh bóng cho đẹp. Các preset emotion phát những tư thế **đã ghi sẵn**, tuyệt đối
trên mọi khớp — kể cả `wrist_roll` — nên chỉ cần một cái rơi vào giữa pha ngắm và lần chụp là đầu bị
đặt lại hoàn toàn, và khung hình cho ra đúng chỗ animation đã đỗ lại chứ không phải người dùng. Một
phản ứng "tò mò" rơi vào giữa câu hỏi là đủ để chụp lên trần nhà. `nudge()` chiếm quyền được một
animation *đang* phát, nhưng không chặn được cái được gọi *sau đó* — mà đó lại đúng là khoảng thời gian
lần chụp nằm trong.

Giá trị khóa trước đó được khôi phục chứ không bị xóa, nên một lượt look không bao giờ kết thúc một
phiên bám vật thể đang chạy thật.

**Vì sao chỉ yaw.** Quy ước dấu của yaw được chép từ quy ước đã kiểm chứng thực nghiệm của tracker
(`dx>0` → `base_yaw` tăng). `AnimationService.nudge()` điều khiển `base_pitch`, trong khi tracker phân
bổ pitch trên base/elbow/wrist — nên dấu của pitch **chưa** được kiểm chứng trên đường này, và pitch
đảo dấu là lỗi codebase này đã từng mắc một lần (xem `servo_follow.command_pid`).

**Thứ tự ưu tiên:**

1. **Thấy người** → căn giữa. Ưu tiên khung bao người hơn khung bao mặt: vật giơ lên hay che mất mặt
   nhưng hiếm khi che cả người, và lấy khung cả người thì bao gồm luôn thứ họ đang cầm.
2. **Không thấy gì, nhưng vài giây trước vừa xác nhận có người ở đúng tư thế này** → **giữ nguyên và
   chụp.** Coi việc biến mất là *bị che khuất, không phải vắng mặt* — đó đúng là hình ảnh của một vật
   giơ lên dưới góc nhìn của bộ phát hiện, và quay đi lúc đó là bỏ rơi đúng thứ người dùng vừa hỏi.
3. **Không thấy gì và gần đây cũng không thấy** → bước dần về phía bearing đã ghi nhớ, và phát hiện
   lại sau **mỗi** bước. Mỗi bước bị giới hạn bởi `BEARING_STEP_DEG`, nhỏ hơn hẳn FOV của camera, vì
   `nudge()` chặn luồng: một cú quay mù lớn có thể đi thẳng qua người đang đứng giữa đường rồi tới một
   chỗ trống với đầy tự tin.
4. **Hết hạn chót** → chụp từ đúng chỗ đầu tới được. Ở đây không bao giờ quét.

Mọi chuyển động đều đi qua `nudge()`, nên `max_speed` trong `SAFETY.md` sẽ kéo dãn thời gian di chuyển
chứ không bị bỏ qua để kịp hạn chót. Một cú click đơn trên nút vật lý sẽ hủy nó
(`button_actions.py`), vì cử chỉ đó nghĩa là "dừng lại và chú ý vào tôi".

### Quét tìm kiếm — chỉ khi được yêu cầu, không bao giờ nội tuyến

Khác với look-aim, và cố ý nằm ngoài đường chụp. Pha ngắm chạy trong một lượt hội thoại đang diễn ra
với hạn chót; một pha quét mất vài giây, đúng là khoảng lặng chết mà thiết kế đó sinh ra để tránh. Nên
pha quét chỉ được vào ở nơi có thể thong thả:

- người dùng yêu cầu thẳng — *"bạn đang ở đâu?"*, *"tìm tôi được không?"* (`skills/servo-control`)
- họ đồng ý với đề nghị sau một lần nhìn thất bại — *"Tôi không thấy nó. Bạn có muốn tôi quay quanh tìm thử không?"*

`POST /servo/search` — quét và dừng ngay ở đối tượng đầu tiên nhìn thấy.

**Thứ tự mới là mấu chốt.** Các điểm dừng được gieo mầm từ bearing đã ghi nhớ rồi lan dần ra hai bên
(`seed`, `seed±45°`, `seed±90°`, …) thay vì quét từ trái sang phải, để nơi khả dĩ nhất được kiểm tra
trước. Đó chính là thứ thường biến một pha quét vài giây thành chỉ một điểm dừng.

Mỗi bước là `STEP_DEG` (45°), cố ý **nhỏ hơn FOV của camera** để các ô chồng lấn — bước bằng đúng cả
FOV sẽ để lại khe hở, nơi một người đứng vắt giữa hai ô bị cả hai ô bỏ sót. Các điểm dừng bị kẹp trong
tầm cơ khí ±135°, và đầu được cho `SETTLE_S` để hết rung trước mỗi lần đọc khung hình, vì đầu đang
chuyển động cho ảnh nhòe và bộ phát hiện sẽ bỏ sót thứ đang nằm ngay trong tầm nhìn.

Bị hủy bởi nút bấm vật lý giống như pha ngắm, và không bao giờ quét khi camera đang tắt — một pha quét
là rất nhiều chuyển động lộ liễu để thực hiện khi người dùng vừa yêu cầu thiết bị đừng nhìn.

> Chưa làm: tín hiệu LED trong lúc quét. Trạng thái LED transient nằm sau các request model của route,
> nên điều khiển nó từ đây sẽ phải đi vòng qua HTTP loopback (điều codebase này tránh) hoặc nhân bản
> phần bookkeeping khôi phục — và một tín hiệu không khôi phục được sẽ làm kẹt LED của lamp. Đáng làm
> cho tử tế thay vì làm nửa vời.

### Nói trong lúc đang tìm

Một chiếc lamp lặng lẽ xoay đi giữa câu hỏi trông như bị hỏng. Một chiếc vừa xoay vừa nói *"bạn đang ở
đâu?"* thì đọc ra là đang cố giúp.

os-server sở hữu các câu, phần phân giải ngôn ngữ và cache WAV (`system/lib/i18n/fillers.go`, các pool
`look_searching` / `look_found` / `look_capturing`); HAL chỉ quyết định **khi nào**, qua
`POST /api/sensing/filler` với `{"pool": "..."}`.

| Trạng thái | Khi nào | Mặc định |
|---|---|---|
| `look_searching` | bước đầu tiên về phía bearing đã ghi nhớ | **bật** (`HAL_LOOK_AIM_SPEAK`) |
| `look_found` | có người xuất hiện **sau khi** đã thông báo đang tìm | bật (cùng cờ) |
| `look_capturing` | pha ngắm có việc phải làm trước khi bấm máy | bật (`HAL_LOOK_AIM_SPEAK_CAPTURE`) |

Phần chặn quan trọng hơn bản thân các câu nói. **Không nói gì khi đối tượng đã nằm giữa sẵn** — lần chụp
đó xong trong vài trăm mili giây, nên mọi câu ở đây đều có điều kiện là pha ngắm thực sự đã phải di
chuyển. *"Bạn đây rồi"* chỉ phát ra như phần kết của một lần tìm đã được thông báo, không bao giờ đứng
một mình. Trạng thái đang tìm chỉ thông báo **một lần**, không phải mỗi bước. Còn câu lúc chụp chỉ phát
khi pha ngắm có việc phải làm — đó là thứ giữ cho nó không mở đầu mọi câu hỏi thị giác.

Một lần chụp nhanh, im lặng và đúng vốn đã là kết quả tốt — lời nói chỉ dành cho những khoảnh khắc người
dùng thực sự phải chờ.

### Bearing người dùng đã ghi nhớ

`hal/drivers/tracking/user_bearing.py` gộp các lần nhìn thấy **ở vùng giữa** thành một ước lượng suy
giảm duy nhất tại `/var/lib/hal/user_bearing.json` (`HAL_USER_BEARING_PATH`). Một góc duy nhất, không
phải histogram — lamp chỉ bao giờ cần một hướng để quay tới.

Chỉ những lần nhìn thấy trong phạm vi **2%** quanh tâm khung hình mới được ghi, chặt hơn cả dung sai
căn khung của chính pha ngắm. Đó là cố ý: ở tâm khung hình thì vị trí servo **chính là** bearing, nên
không có phép quy đổi pixel→góc nào, và do đó không phụ thuộc vào hằng số FOV của camera (đang mâu
thuẫn — 60° trong `constants.py` so với 78° trong BOM phần cứng) hay vào mô hình chiếu của ống kính.

Các góc được lấy trung bình **tuyến tính, không phải theo vòng tròn**: `base_yaw` là dải servo bị chặn
±135° và không quấn vòng, nên trung bình theo vòng tròn sẽ sai ở hai đầu dải.

Giá trị lệch (outlier) bị làm giảm ảnh hưởng chứ không bị nhận thẳng — một người đi ngang qua phòng
không được phép lật ngược ước lượng — nhưng `OUTLIER_STREAK` lần lệch liên tiếp sẽ được coi là dời chỗ
thật và được nhận nguyên vẹn.

**Được tiêu thụ bởi ưu tiên 3 của pha ngắm** (ở trên) khi độ tin cậy vượt `MIN_BEARING_CONFIDENCE`.
Hãy mở ra kiểm tra phép tính và dấu:

```bash
cat /var/lib/hal/user_bearing.json
```

`bearing_deg` phải hội tụ về gần nơi người dùng thực sự ngồi. Một ước lượng nằm **đối xứng gương qua
0** nghĩa là dấu yaw bị đảo — đây là kiểu hỏng mà file này dễ dính nhất, vì nó chạy vòng hở và không
có gì sửa sai cho nó.

### Phát hiện lamp đã bị dời chỗ

Bearing được lưu theo hệ quy chiếu **gắn với lamp**, nên nhấc lamp lên hay xoay nó trên bàn là nó sai
ngay lập tức — trong khi file vẫn trông hoàn toàn hợp lệ.

Không có gì trên thiết bị này quan sát được điều đó một cách trực tiếp:

| Cách tiếp cận | Vì sao không được |
|---|---|
| IMU / cảm biến gia tốc | không được lắp — không có trong BOM lẫn trong HAL |
| Phản hồi servo | `base_yaw` đo vị trí đầu so với **đế**. Xoay cả chiếc lamp làm thay đổi thế giới chứ không làm thay đổi khớp. |
| Dựa vào lần khởi động lại | yếu ở cả hai chiều — lamp reboot mà không bị dời, và bị dời mà không reboot |

Nên nó được **suy ra từ các dự đoán sai**: khi ưu tiên 3 của pha ngắm quay tới bearing đã ghi nhớ mà
không thấy ai, đó là một lần trượt. `PREDICTION_MISS_LIMIT` lần trượt sẽ hủy ước lượng, và nó tự dựng
lại từ các lần nhìn thấy mới.

Ba lớp bảo vệ giúp sinh hoạt bình thường không bị hiểu nhầm thành dời chỗ:

- **Một lần trượt là chưa đủ** — người dùng có thể chỉ đang ra ngoài.
- **Một lần trúng xóa sạch chuỗi trượt**, nên những lần vắng mặt lẻ tẻ không bao giờ cộng dồn.
- **Các lần trượt phải xảy ra gần nhau** (`MISS_STREAK_WINDOW_S`). Lamp bị dời sẽ trượt ở mọi lần thử
  kể từ lúc bị dời; còn người dùng thỉnh thoảng ở phòng khác thì tạo ra các lần trượt rời rạc trải dài
  hàng tuần. Không có cửa sổ thời gian thì sau đủ lâu hai thứ đó trông y hệt nhau.

Và một lần trượt chỉ được tính khi lamp **thực sự đã nhìn và không thấy gì**. Camera đang tắt, không
lấy được khung hình, hết hạn chót, bị nút bấm hủy, và cả trường hợp giữ nguyên vì bị che khuất — tất cả
đều thoát ra mà không chấm điểm; đặc biệt chế độ riêng tư tuyệt đối không được phép xóa dần nơi người
dùng hay ngồi.

Cách này tự chữa lành cho **mọi** nguyên nhân (lamp bị dời, đồ đạc sắp xếp lại, người dùng đổi bàn) mà
không bao giờ cần biết nguyên nhân nào đã xảy ra.

#### Phải dời bao nhiêu thì mới cần tới cơ chế phát hiện

Phần lớn các lần dời chỗ không bao giờ chạm tới cơ chế đếm trượt ở trên, vì ước lượng tự sửa lấy:

| Lamp bị xoay | Cái gì sửa nó |
|---|---|
| **nhỏ hơn nửa FOV camera** (~30°) | người dùng **vẫn nằm trong khung hình** ở bearing cũ, nên pha ngắm vẫn tìm thấy và căn giữa được — và chính lần nhìn thấy đó ghi lại góc yaw mới đúng. EMA thông thường kéo ước lượng theo. **Không có gì phát hiện ra việc dời chỗ; và cũng không cần.** |
| tới `OUTLIER_DEG` (45°) | không thấy được từ bearing cũ, nhưng tìm thấy trong lúc bước dần về phía đó. Lần nhìn thấy vẫn dưới ngưỡng outlier nên được nhận với trọng số đầy đủ. |
| vượt `OUTLIER_DEG` | các lần nhìn thấy trông như outlier, nên `OUTLIER_STREAK` lần liên tiếp sẽ được nhận là một lần dời chỗ. |
| xa tới mức không bao giờ tìm thấy người dùng | chuỗi trượt sẽ hủy ước lượng và nó dựng lại từ đầu. |

Nên toàn bộ cơ chế ở trên chỉ dành cho trường hợp **cuối cùng**. Một chiếc lamp bị xê dịch nhẹ trên bàn
được xử lý bởi chính quy tắc cập nhật của ước lượng — đó cũng là lý do trường hợp nhỏ lại là trường hợp
êm nhất: lamp không bao giờ biết là nó đã bị dời, và cũng không cần biết.

**Xem và đặt lại:**

```bash
curl 127.0.0.1:5001/servo/bearing              # {"known":true,"bearing_deg":-18.5,...}
curl -X POST 127.0.0.1:5001/servo/bearing/reset
```

Lệnh đặt lại cũng được nối với giọng nói qua `skills/servo-control` — *"tôi đã dời bạn đi"*, *"bạn
đang ở chỗ mới"*. Việc phát hiện tự động cần vài lần thất bại mới hành động, điều đó đúng để tránh báo
động giả nhưng chậm khi người dùng vốn đã BIẾT là lamp bị dời.
