# Ghi Chú Runtime Reachy Mini

Đây là runbook riêng cho `devices/reachy-mini`. Tài liệu này chỉ ghi những phần
khác với nền tảng Autonomous chung và thiết bị tham chiếu Lamp.

## Tham Chiếu

Phần nào giống thì tham chiếu, không chép lại:

| Chủ đề | Tài liệu |
|--------|----------|
| Schema `DEVICE.md`, capability mounting, ngữ nghĩa `driver:` | [`devices/contract/DEVICE-SPEC.md`](../../../contract/DEVICE-SPEC.md) |
| Từ vựng capability | [`devices/contract/capabilities.md`](../../../contract/capabilities.md) |
| Layer capability/route/driver của HAL | [`docs/architecture/hal.md`](../../../../docs/architecture/hal.md) |
| Safety engine | [`docs/vi/safety_vi.md`](../../../../docs/vi/safety_vi.md) |
| Setup / AP mode / provisioning | [`docs/vi/setup-flow_vi.md`](../../../../docs/vi/setup-flow_vi.md) |
| Vision tracking của Lamp, hiện vẫn là reference cho phần tracking internals | [`devices/lamp/docs/vi/vision-tracking_vi.md`](../../../lamp/docs/vi/vision-tracking_vi.md) |

Nguồn hardware đã kiểm tra ngày 2026-07-21:

- Pollen / Hugging Face Space: <https://huggingface.co/spaces/pollen-robotics/Reachy_Mini>
- Trang chính thức Reachy Mini: <https://www.reachy-mini.org/>
- Datasheet hardware của Seeed Studio: <https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_hardware/>
- Memory Claude Code của dự án: `reachy-mini-port`

## Profile Này Khai Gì

`DEVICE.md` khai route surface sau:

| Capability | Routes | Required | Ghi chú riêng cho Reachy |
|------------|--------|----------|--------------------------|
| `audio` | `audio`, `speaker`, `voice` | yes | Bản Wireless có mảng 4 mic và loa 5 W |
| `vision` | `camera` | yes | Camera góc rộng nằm trong đầu |
| `motion` | `servo` | yes | `driver: reachy_sdk`; đầu Stewart-platform, body yaw, antenna |
| `expression` | `emotion` | yes | Biểu cảm bằng chuyển động, antenna và giọng nói |
| `sensing` | `sensing` | no | Perception stack optional; gate giống các device khác |
| `presence` | none | no | Chỉ là behavior gate |
| `system` | `system` | yes | HAL system route chung |

Profile này cố ý **không** khai `light`, `display`, `scene`, hoặc `music`.
Các nguồn Pollen/Hugging Face/Seeed hiện liệt kê motion, camera, mảng mic, loa,
compute, IMU, Wi-Fi, pin và antenna có animation, nhưng không liệt kê LED ring
hay màn hình có thể điều khiển như một capability của device. Nếu revision sau
có LED/screen addressable, chỉ thêm capability khi đã có HAL driver và hành vi
safety tương ứng.

## Motion Driver

Reachy chọn motion backend của HAL qua:

```yaml
motion:
  routes: [servo]
  driver: reachy_sdk
  required: true
  safety: SAFETY.md#motion
```

HAL resolve cấu hình này tới `hal/drivers/motors/reachy_service.py` qua
`hal/drivers/motors/factory.py`. Driver implement contract chung
`MotionService`, nên `hal/routes/servo.py` vẫn hardware-neutral.

Driver là client mỏng tới daemon của Pollen:

```bash
REACHY_DAEMON_HOST=localhost
REACHY_DAEMON_PORT=8000
```

Joint keys của Reachy trong HAL dùng độ/mm, dù SDK dùng radian/mét:

| Joint key | Ý nghĩa |
|-----------|---------|
| `head_x.pos`, `head_y.pos`, `head_z.pos` | Dịch chuyển đầu, mm |
| `head_roll.pos`, `head_pitch.pos`, `head_yaw.pos` | Xoay đầu, độ |
| `body_yaw.pos` | Xoay thân, độ |
| `antenna_left.pos`, `antenna_right.pos` | Góc antenna, độ |

Được hỗ trợ qua các endpoint `/servo` chung:

- pose/readiness: `/servo`, `/servo/position`, `/servo/status`
- chuyển động: `/servo/move`, `/servo/aim`, `/servo/nudge`
- recovery/mode: `/servo/zero`, `/servo/hold`, `/servo/release`, `/servo/resume`
- expression moves: `/servo/play` khi recorded-move library của Reachy sẵn sàng

Khác biệt đã biết so với Lamp:

- Upload CSV servo recording là concept của Feetech/Lamp; `add_recording` của
  Reachy hiện no-op cho tới khi quyết định uploaded moves có cần cho body này
  hay không.
- Idle/ambient motion do daemon hoặc recorded-move library quản lý, không phải
  event loop Feetech.
- `/servo/track` chưa production-ready cho Reachy. `tracker_service` chung vẫn
  chạm vào internals kiểu Lamp/Feetech và cần chuyển sang accessor của
  `MotionService` trước.

## Safety Delta

Machine bounds hiện tại trong `SAFETY.md`:

```yaml
motion:
  max_speed: 60
  stop_always: true
```

Safety layer chung của HAL kéo dài duration để giữ `max_speed`. `stop`, `zero`,
`hold`, và `release` vẫn là hành động recovery deterministic.

Chưa thêm block `thermal` cho tới khi đo được thermal profile của Raspberry Pi
trên bản Wireless thật.

## Checklist Khởi Động Kiểm Tra

1. Kiểm tra static profile:

   ```bash
   python3 -m unittest devices.contract.cts.test_compatibility
   ```

2. Cài dependency Reachy chỉ trên robot:

   ```bash
   cd hal
   uv sync --extra reachy
   ```

   Giữ `reachy` tách khỏi extra `hardware` chung. Reachy SDK kéo các dependency
   Linux GUI/media như pygobject/pycairo, không nên ép vào image Lamp.

3. Boot HAL với profile Reachy:

   ```bash
   DEVICE_TYPE=reachy-mini DEVICES_DIR=/opt/devices uv run uvicorn hal.server:app --host 0.0.0.0 --port 5001
   ```

4. Xác nhận mounted routes:

   ```bash
   curl -s http://localhost:5001/device
   curl -s http://localhost:5001/health
   ```

   Khi các driver required đều sẵn sàng, expected routes là `audio`, `camera`,
   `emotion`, `servo`, `speaker`, `system`, `voice`. `led` và `display` phải
   vắng mặt.

5. Verify motion theo thứ tự an toàn:

   ```bash
   curl -s http://localhost:5001/servo/position
   curl -s -X POST http://localhost:5001/servo/aim \
     -H 'content-type: application/json' \
     -d '{"direction":"center","duration":1.0}'
   curl -s -X POST http://localhost:5001/servo/nudge \
     -H 'content-type: application/json' \
     -d '{"yaw":5,"pitch":0,"duration":1.0}'
   curl -s -X POST http://localhost:5001/servo/zero
   curl -s -X POST http://localhost:5001/servo/release
   ```

## TODO Khi Spike Hardware

Cập nhật tài liệu này sau session chạy trên máy thật đầu tiên:

- board id thực tế của bản Wireless (`DEVICE.md` hiện cho phép
  `raspberry_pi_4` và `raspberry_pi_5`)
- camera device id/name và resolution mặc định dùng được
- microphone ALSA device name và hành vi echo cancellation
- sign convention cho `head_yaw.pos`, `head_pitch.pos`, và thứ tự antenna
- `wake_up` / `goto_sleep` có phát âm thanh không khi dùng `media_backend="no_media"`
- first-run behavior của `pollen-robotics/reachy-mini-emotions-library`
- thermal limits trước khi bật `SAFETY.md` `thermal`
