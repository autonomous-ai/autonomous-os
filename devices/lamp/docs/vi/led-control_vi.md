# LED Control — Tài Liệu

## Phần Cứng

- **64 WS2812 RGB LEDs** — grid 8x5
- Driver: `rpi_ws281x` (Python, HAL owns)
- FastAPI endpoints trên `:5001`

## Endpoints

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/led` | LED strip info (count, available) |
| GET | `/led/color` | Màu hiện tại `{"r", "g", "b"}` |
| POST | `/led/solid` | Fill toàn bộ strip 1 màu |
| POST | `/led/paint` | Set từng pixel (array tối đa 64 items) |
| POST | `/led/off` | Tắt tất cả LED |
| POST | `/led/effect` | Bật effect |
| POST | `/led/effect/stop` | Dừng effect đang chạy |
| POST | `/led/restore` | Repaint LED state mà user đã set (hoặc tắt strip nếu không có) |

### Transient writes

`/led/solid`, `/led/effect`, `/led/off` chấp nhận flag tùy chọn `"transient": true`. Khi bật, call sẽ paint strip nhưng **không** ghi đè user LED state. State đã lưu sẽ được restore khi caller (vd Claude Desktop Buddy) xong việc — qua emotion restore timer tự nhiên, hoặc qua `POST /led/restore`. Pulse effect chạy với `transient: true` cũng overlay trên màu user thay vì nền đen.

## Solid Color

```json
POST /led/solid
{"r": 255, "g": 180, "b": 100}
```

Giá trị RGB 0-255.

## Paint (Per-Pixel)

```json
POST /led/paint
{"pixels": [{"i": 0, "r": 255, "g": 0, "b": 0}, {"i": 1, "r": 0, "g": 255, "b": 0}]}
```

`i` = pixel index (0-63).

## Effects

```json
POST /led/effect
{"effect": "breathing", "r": 255, "g": 100, "b": 50, "speed": 1.0}
```

| Effect | Mô tả | Params |
|--------|-------|--------|
| `breathing` | Sine-wave brightness lên xuống | r, g, b, speed |
| `candle` | Nến lung linh ngẫu nhiên | r, g, b |
| `rainbow` | Xoay hue qua toàn bộ strip | speed |
| `notification_flash` | Flash nhanh 3 lần | r, g, b |
| `pulse` | Pulse đơn từ tâm ra ngoài | r, g, b, speed |

## Lighting Scenes

```json
POST /scene
{"scene": "reading"}
```

Mỗi scene điều khiển **toàn bộ thiết bị ngoại vi** — không chỉ LED mà cả camera, mic, speaker và servo.

Tắt scene: `POST /scene/off` — xoá scene đang active, khôi phục LED idle, bật lại camera/speaker, nhả servo hold.

Scene đang active **sống sót qua các lần restart HAL service** (OTA, deploy, crash): trạng thái được persist vào sidecar theo phiên boot (`/tmp/hal-scene-state.json`, gắn với `boot_id` của kernel) và tự động kích hoạt lại khi HAL chạy trở lại, nên niềm tin của agent ("focus mode đang bật") luôn đồng bộ. Reboot toàn bộ thiết bị thì chủ đích khởi động không có scene. Các lệnh LED transient (`/led/solid`, `/led/off`, `/led/effect` với `"transient": true`, vd hiệu ứng breathing lúc boot) chỉ overlay lên strip mà không thoát scene đang active; chỉ LED override non-transient mới xoá scene.

| Scene | Sáng | Màu (K) | Servo | Camera | Mic | Speaker |
|-------|------|---------|-------|--------|-----|---------|
| `reading` | 80% | 4000K trắng ấm | desk + hold | off | off | off |
| `focus` | 70% | 4200K trung tính ấm | desk + hold | off | off | off |
| `relax` | 40% | 2700K ấm | wall | on | on | on |
| `movie` | 15% | 2400K amber mờ | wall | off | on | off |
| `night` | 5% | 1800K amber đậm | down | off | off | off |
| `energize` | 100% | 5000K ánh sáng ban ngày | up | on | on | on |

### Điều khiển ngoại vi theo scene

Khi kích hoạt scene, `POST /scene` thực hiện theo thứ tự:

1. **LED** — màu đặc = `preset.color × preset.brightness`
2. **Servo aim** — xoay đầu đèn theo hướng preset (desk, wall, up, down)
3. **Servo hold** — nếu `"servo": "hold"`, freeze servo **sau khi** aim xong (aim → hold trong cùng 1 thread). Tự release khi chuyển sang scene không có hold.
4. **Camera** — tự động bật/tắt
5. **Mic** — mute dừng voice pipeline (STT), unmute khởi động lại
6. **Speaker** — mute dừng TTS + nhạc đang phát, unmute bật lại output

### Chặn emotion khi hold mode

Khi servo đang hold (reading/focus), **animation cảm xúc bị chặn** để tránh phân tâm:

- `happy`, `thinking`, `curious`, `sad`, v.v. → servo + LED bị bỏ qua
- `greeting`, `sleepy`, `stretching` → **cho qua** (đây là emotion thay đổi trạng thái: chào, ngủ, thức dậy) — **chỉ áp dụng cho hold do scene preset**

**`/servo/hold` tường minh** (lệnh agent kiểu "nhìn lên tường giữ đó") set `_hold_explicit` và chặn servo với **mọi** emotion, kể cả nhóm scene-change — trước đây `[HW:/emotion:greeting]` đứng cuối reply lợi dụng miễn trừ này, đè pose đã lệnh bằng pose cuối của animation greeting. `/servo/resume` và chuyển scene sẽ xoá cờ.

Nghĩa là khi focus, sensing event vẫn tới OpenClaw nhưng Lamp giữ nguyên trạng thái vật lý — không cử động, LED ổn định.

### Lý do chọn nhiệt độ màu

- **Focus 4200K/70%** (không phải 5000K/100%) — 4000-4300K tối ưu cho tập trung mà không gây mỏi mắt
- **Night 1800K amber đậm** — bước sóng >580nm không ảnh hưởng melatonin
- **Movie mic on** — cho phép điều khiển giọng nói ("pause", "stop") khi xem phim

## Status LED

Xem chi tiết: [status-led_vi.md](status-led_vi.md)

LED phản hồi trạng thái hệ thống (tất cả `breathing` speed 3.0 trừ khi ghi rõ):

| Trạng thái | Màu | RGB |
|-----------|-----|-----|
| Mất internet (Connectivity) | Cam | `(255, 80, 0)` |
| Đang khởi động (Booting) | Xanh dương | `(0, 80, 255)` |
| HAL Down | Tím | `(180, 0, 255)` |
| Agent Down | Cyan | `(0, 200, 200)` |
| Hardware Failure | Vàng | `(255, 255, 0)` |
| OTA đang chạy (bootstrap) | Cam | `(255, 140, 0)` |
| OTA thành công (bootstrap) | Flash xanh lá | `(0, 255, 80)` |
| OTA thất bại (bootstrap) | Đỏ pulse | `(255, 30, 30)` |

Quản lý bởi `internal/statusled/Service` (lamp) và `lib/hal` trực tiếp (bootstrap).

Không còn màu nào hardcode trong Go nữa — trạng thái `internal/statusled`, màu OTA-progress
của bootstrap, và màu trắng setup-needed đều đi qua HAL. OS giữ máy trạng thái (KHI nào hiện)
và gửi *tên trạng thái* xuống HAL (`POST /led/status`: booting/error/ota/connectivity/
hal_down/agent_down/hardware/ready_flash/ota_progress/ota_error/ota_success/setup); HAL tra
màu/effect/speed từ `STATUS_LED_PRESETS`, override per-device qua section `status_led` trong
`presets.json` (xem [DEVICE-SPEC.md § Per-device presets](../../../../contract/DEVICE-SPEC.md#per-device-presets-presetsjson)).
`setup` là solid bền (lưu thành trạng thái hiển thị); còn lại là overlay transient.

### Đèn báo mic đang mute (idle indicator)

`STATUS_LED_PRESETS["mic_muted"]` — đỏ sẫm `(140, 0, 0)` breathing speed 0.8. Key HAL-local
(không có state Go statusled tương ứng): bật bởi `POST /voice/mute`, tắt bởi `POST /voice/unmute`
(`app_state._mic_muted_led`). Đây là **trạng thái nghỉ** của strip khi mic đang mute —
không chặn gì cả:

- Emotion, effect, TTS/music wave, transient overlay vẫn chạy bình thường đè lên. Chạy
  xong thì mọi LED restore (`_restore_user_led`, `POST /led/restore`) lắng về màu đỏ
  thay vì user state — "không có gì xảy ra + đỏ breathing" nghĩa là mic đang mute.
- Lệnh LED explicit của user (non-transient `/led/solid|off|effect`, `/led/paint`)
  dismiss indicator — ý user thắng strip; mic vẫn mute.
- Nhường các lựa chọn ánh sáng chủ đích: user tắt đèn thì strip vẫn tối, scene active
  giữ nguyên ánh sáng chức năng (flag vẫn giữ, thoát scene mà còn mute thì đỏ quay lại
  ở lần restore kế). Các đường scene unmute mic (`/scene` với `mic:"on"`, `/scene/off`)
  cũng clear indicator.
- `_user_led_state` không bao giờ bị đụng — unmute là về lại đúng state user đã lưu.

### Setup-needed solid (lamp)

Khi lamp start và `config.SetUpCompleted == false` (device đang ở AP/provisioning mode), `server/server.go` spawn goroutine background poll `GET /health` của HAL mỗi giây tối đa 30s, khi `health.led == true` thì fire `lelamp.SetSolid(255, 255, 255)` — paint strip trắng solid báo "device ready, vào hotspot đi". Phải poll (không phải call 1 lần) vì cold boot os-server bind :5000 trước HAL :5001. Không dùng status LED state. Blue-breathing booting vẫn show trong lúc init. Xem [setup-flow_vi.md](setup-flow_vi.md#ap-mode).

## Ambient Idle Behaviors

Khi Lamp idle (không có interaction):
- **Breathing LED** — sine-wave brightness. Thở theo màu LED hiện tại; khi chưa có màu nào (vd vừa boot xong), fallback về **trắng ấm dịu `(255, 200, 140)`** (~2700K) speed 0.3, để đèn lúc nghỉ trông như một cái đèn ấm cúng đang bật, không phải màu xanh "thiết bị" lạnh. Nếu user/agent đã đặt màu thì tôn trọng màu đó (breathing dùng màu đó; ambient không đè lên màu đã khóa).

Tự pause khi có interaction, resume sau 60s im lặng.

## LED Trong Emotion

Mỗi emotion preset có LED color riêng:

| Emotion | LED Color |
|---------|-----------|
| curious | Vàng ấm |
| happy | Vàng sáng |
| sad | Xanh dương nhạt |
| thinking | Tím nhẹ |
| idle | Warm white mờ |
| excited | Cam sáng |
| shy | Hồng nhạt |
| shock | Trắng flash |

### Tên emotion không nhận diện được

`POST /emotion` (`os/hal/routes/emotion.py`) không bao giờ từ chối tên emotion khác rỗng. Tên được lowercase/trim; tên nào không có trong `EMOTION_PRESETS` sẽ fallback về `curious` (biểu cảm trung tính, luôn an toàn) kèm log warning — caller là AI agent đôi khi bịa tên emotion, trả 400 sẽ phí lượt mà thiết bị không hiển thị gì. Ngoại lệ: khi thiết bị đang ngủ, tên lạ bị **ignore** (`status: ignored`) thay vì fallback — `curious` là wake emotion, nên fallback sẽ cho tên bịa vượt sleep gate và đánh thức thiết bị. Ngoài trường hợp đó, downstream (servo, LED) dùng emotion đã resolve.

## Override preset theo từng thiết bị

Một thiết bị có thể ghi đè các giá trị emotion/scene/aim này (và kích thước vòng LED) mà
không đổi bảng mặc định dùng chung, qua file `devices/<type>/presets.json`. Đây là cơ chế
nền tảng — xem [DEVICE-SPEC.md § Per-device presets](../../../../contract/DEVICE-SPEC.md#per-device-presets-presetsjson).
