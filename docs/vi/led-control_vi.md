# LED Control — Tài Liệu

## Phần Cứng

- **64 WS2812 RGB LEDs** — grid 8x5
- Driver: `rpi_ws281x` (Python, LeLamp owns)
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
- `greeting`, `sleepy`, `stretching` → **cho qua** (đây là emotion thay đổi trạng thái: chào, ngủ, thức dậy)

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
| LeLamp Down | Tím | `(180, 0, 255)` |
| Agent Down | Cyan | `(0, 200, 200)` |
| Hardware Failure | Vàng | `(255, 255, 0)` |
| OTA đang chạy (bootstrap) | Cam | `(255, 140, 0)` |
| OTA thành công (bootstrap) | Flash xanh lá | `(0, 255, 80)` |
| OTA thất bại (bootstrap) | Đỏ pulse | `(255, 30, 30)` |

Quản lý bởi `internal/statusled/Service` (lamp) và `lib/lelamp` trực tiếp (bootstrap).

### Setup-needed solid (lamp)

Khi lamp start và `config.SetUpCompleted == false` (device đang ở AP/provisioning mode), `server/server.go` spawn goroutine background poll `GET /health` của LeLamp mỗi giây tối đa 30s, khi `health.led == true` thì fire `lelamp.SetSolid(255, 255, 255)` — paint strip trắng solid báo "device ready, vào hotspot đi". Phải poll (không phải call 1 lần) vì cold boot lamp-server bind :5000 trước LeLamp :5001. Không dùng status LED state. Blue-breathing booting vẫn show trong lúc init. Xem [setup-flow_vi.md](setup-flow_vi.md#ap-mode).

## Ambient Idle Behaviors

Khi Lamp idle (không có interaction):
- **Breathing LED** — sine-wave brightness, palette warm

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
