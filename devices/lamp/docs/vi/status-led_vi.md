# Status LED — Đặc Tả

Status LED giúp user nhìn đèn là biết Lamp đang làm gì bên trong.
Không có tín hiệu này, user không phân biệt được Lamp đang khởi động, đang update, đang mất kết nối với AI brain, hay bị lỗi.

## Nguyên Tắc

1. **Nhìn là hiểu** — mỗi trạng thái có màu riêng, không cần đoán.
2. **Không xung đột** — status LED nhường quyền cho scene/emotion do user chọn. Khi trạng thái kết thúc, strip được trả về đúng trạng thái user (hoặc agent) đã set, ambient resume sau khoảng im lặng.
3. **Ưu tiên** — khi nhiều trạng thái active cùng lúc, trạng thái cao nhất thắng.

## Các Trạng Thái

Tất cả các state dùng effect `breathing` speed 3.0 trừ khi ghi rõ. Giá trị RGB lấy từ `internal/statusled/service.go`.

| Trạng thái (hằng số code) | Màu | RGB | Ý nghĩa | Trigger | Tự tắt |
|---|---|---|---|---|---|
| `StateConnectivity` | Cam | `(255, 80, 0)` | **Mất internet** — Wi-Fi kết nối nhưng không có internet | Network monitor: 5 lần ping thất bại liên tiếp (~25s) | Có — khi ping thành công |
| `StateError` | Đỏ | `(255, 0, 0)` | **Lỗi** — Lỗi hệ thống (reserved) | Lỗi nghiêm trọng | Có — khi lỗi được khắc phục |
| `StateOTA` | Xanh lá | `(0, 255, 0)` | **Đang update** — OTA firmware đang chạy (enum dự trữ; bootstrap drive LED OTA trực tiếp qua `lib/hal` — xem "Bootstrap (OTA)" bên dưới) | Bootstrap reconcile phát hiện update | Khởi động lại sau khi update xong |
| `StateBooting` | Xanh dương | `(0, 80, 255)` | **Đang khởi động** — Lamp đang bật | `server.go` lúc startup | Có — khi OpenClaw agent connect và sẵn sàng |
| `StateLeLampDown` | Tím | `(180, 0, 255)` | **HAL Down** — Server phần cứng không phản hồi. Khi HAL đang down LED **tắt hẳn** vì driver LED cũng chết theo; tím breathing chỉ flash ~3s khi phục hồi | `healthwatch` poll HAL `/health` thất bại | Tự tắt 3s sau khi phục hồi |
| `StateAgentDown` | Cyan | `(0, 200, 200)` | **Agent Down** — AI brain mất kết nối | OpenClaw WebSocket ngắt (`internal/openclaw/service_ws.go`) | Có — khi WebSocket reconnect |
| `StateHardware` | Vàng | `(255, 255, 0)` | **Hardware Failure** — servo/LED/audio/voice không healthy qua HAL `/health` | `healthwatch` poll (mỗi 5s); camera và sensing không tính | Có — khi tất cả linh kiện báo OK |

### Ready flash

Sau khi boot xong (Booting clear và không state nào khác active), `statusled.FlashReady()` bắn flash **trắng** ngắn `notification_flash` ~1s để báo agent sẵn sàng nhận lệnh. Sẽ không bắn nếu có status state nào đang active.

### OTA chi tiết (do bootstrap drive)

Bootstrap binary gọi `lib/hal` trực tiếp (không qua `statusled.Service`):

| Giai đoạn | LED | Source |
|---|---|---|
| Đang tải + cài | Cam `(255, 140, 0)` `breathing` speed 0.4 | `bootstrap/bootstrap.go` |
| Thành công | Xanh lá `(0, 255, 80)` `notification_flash` ngắn rồi dừng | `bootstrap/bootstrap.go` |
| Thất bại | Đỏ `(255, 30, 30)` `pulse` speed 1.5 | `bootstrap/bootstrap.go` |

Lưu ý: cam/đỏ OTA của bootstrap dùng RGB và effect parameters hơi khác so với enum trong `statusled.Service` — bootstrap là binary riêng, sở hữu LED trong khi OTA đang chạy.

## Ưu Tiên

Khi nhiều state `statusled.Service` cùng active, state cao nhất được hiển thị:

```
Connectivity (cao nhất) > Error > OTA > Booting > HAL Down > Agent Down > Hardware (thấp nhất)
```

Số ưu tiên (từ map `priority` trong `service.go`):

| Trạng thái | Ưu tiên |
|---|---|
| `StateConnectivity` | 7 (cao nhất) |
| `StateError` | 6 |
| `StateOTA` | 5 |
| `StateBooting` | 4 |
| `StateLeLampDown` | 3 |
| `StateAgentDown` | 2 |
| `StateHardware` | 1 (thấp nhất) |

Ví dụ: nếu Lamp mất internet VÀ agent down, **Mất internet** (cam) thắng vì ưu tiên cao hơn.

LED OTA của bootstrap không qua priority queue — nó chạy khi bootstrap sở hữu strip, thường là lúc lamp đang restart.

## Chi Tiết Hành Vi

### Booting (Xanh dương)
- Activated bởi `server.go` lúc startup, trước khi agent sẵn sàng
- Clear khi OpenClaw agent connect và sẵn sàng nhận lệnh
- Theo sau là flash trắng ngắn `FlashReady` báo "sẵn sàng nghe"

### Connectivity / Mất internet (Cam)
- Network service ping mỗi 5 giây
- Sau 5 lần thất bại liên tiếp (~25 giây), `StateConnectivity` được set
- Tắt ngay khi ping thành công
- Lamp vẫn hoạt động local nhưng cloud features không khả dụng

### Agent Down (Cyan)
- Activated khi OpenClaw WebSocket mất kết nối
- Tắt khi WebSocket reconnect thành công
- Voice command và AI features không khả dụng; LED scene và servo vẫn hoạt động
- TTS thông báo "Brain reconnected!" khi phục hồi

### HAL Down (Tím — hoặc tối/đen)
- Khi HAL crash, LED **tắt hẳn** vì driver LED cũng chết theo
- `healthwatch` poll mỗi 5 giây và theo dõi thời gian down
- Khi phục hồi: tím breathing flash ~3s khi state clear, sau đó LED trở lại bình thường
- TTS thông báo "Hardware recovered!" khi phục hồi
- LED, servo, camera, mic, speaker đều không khả dụng khi HAL down

### Hardware Failure (Vàng)
- Activated khi servo, LED driver, audio, hoặc voice pipeline báo unhealthy qua HAL `/health`
- Per-servo online check qua `lelamp.GetServoStatus()` — bất kỳ servo nào offline cũng trip
- Camera và sensing không tính (có thể tắt theo scene preset)
- Health watcher poll mỗi 5 giây
- Tự tắt khi tất cả linh kiện được giám sát báo OK
- Xem web monitor để biết chi tiết linh kiện nào lỗi

### OTA Update (Xanh lá / Cam / Đỏ — bootstrap)
- Xem "OTA chi tiết (do bootstrap drive)" ở trên
- Thiết bị khởi động lại sau khi update thành công — LED chuyển sang Booting (xanh dương) trên boot mới

### Lỗi (Đỏ — reserved)
- Enum `StateError` được định nghĩa trong `statusled.Service` nhưng hiện tại không được caller nào trong lamp set
- Bootstrap dùng `pulse` đỏ trực tiếp để báo OTA thất bại (không qua `statusled.Service`)

## Kiến Trúc

### Lamp (os-server)

`internal/statusled/Service` quản lý các state active với priority map. Caller `Set` và `Clear` các named state; service apply LED effect cho state có priority cao nhất.

Các caller thực tế (đã verify với code):

```
server.go                    → Set/Clear StateBooting + StateConnectivity + FlashReady
internal/openclaw/service_ws → Set/Clear StateAgentDown
internal/healthwatch/service → Set/Clear StateLeLampDown + StateHardware
```

Service gọi HAL `/led/effect` qua `lib/hal` (shared HTTP client).

### Bootstrap (bootstrap-server)

Bootstrap là binary riêng. Gọi `lib/hal` **trực tiếp** trong hàm `reconcile` (không qua `statusled.Service`):

```
reconcile phát hiện update → lelamp.SetEffect("breathing", 255, 140, 0, 0.4)   // cam
        ↓ cài update...
thành công → lelamp.SetEffect("notification_flash", 0, 255, 80, 1.0)            // xanh lá flash
thất bại   → lelamp.SetEffect("pulse", 255, 30, 30, 1.5)                        // đỏ pulse
```

## Tích Hợp Với Ambient

Ambient service (`internal/ambient`) tự pause khi có interaction event (`chat_send`, `chat_response`, v.v.). Khi `statusled.Service` clear state cuối cùng, nó gọi `lelamp.RestoreLED()` — strip trở về màu/effect mà user (hoặc agent) đã set qua `/led/solid`, `/led/effect`, hoặc `/scene`. Nếu chưa từng có user state, strip clear về off và ambient sẽ resume breathing sau 60s im lặng.

Mọi `statusled.Service` write đều dùng `transient=true` để không ghi đè user LED state — restore-after-animation của emotion sẽ đọc lại đúng màu user, không phải màu status. (Bootstrap gọi `lib/hal` trực tiếp cũng transient.)

## Shared HAL Client

`lib/hal/client.go` — HTTP wrapper dùng chung cho tất cả Go code điều khiển LED:

| Function | Endpoint | Mô tả |
|---|---|---|
| `SetEffect(effect, r, g, b, speed)` | `POST /led/effect` (transient) | Bật effect — không save user LED state |
| `StopEffect()` | `POST /led/effect/stop` | Dừng effect |
| `RestoreLED()` | `POST /led/restore` | Trả strip về user state đã save |
| `SetSolid(r, g, b)` | `POST /led/solid` | Set màu đơn |
| `Off()` | `POST /led/off` | Tắt LED |

Tất cả gọi fire-and-forget, timeout 5s. Nếu hardware không có thì bỏ qua.

## Hoạt Động Bình Thường

Khi không có status state nào active, LED được điều khiển bởi:

1. **Emotion preset** — màu theo cảm xúc của AI agent (xem [emotion-led-mapping.md](../emotion-led-mapping.md))
2. **Scene preset** — scene chiếu sáng do user chọn (reading, focus, relax, v.v.)
3. **Ambient breathing** — breathing nhẹ màu ấm khi idle

Status state **ghi đè** tất cả các LED trên khi active. Khi state tắt, LED tự động quay về hành vi bình thường.

## Trải Nghiệm User

| User thấy | Lamp đang làm gì |
|---|---|
| Xanh dương breathing | Lamp đang khởi động |
| Flash trắng ngắn | Lamp sẵn sàng nghe |
| Cyan breathing | AI brain mất kết nối (Lamp vẫn điều khiển đèn/servo local được) |
| Tím breathing (sau khi tối) | HAL vừa phục hồi sau crash |
| Tối / không LED | HAL crash (driver LED chết) |
| Cam breathing | Mất internet (Lamp offline) |
| Vàng breathing | Có linh kiện hardware không healthy |
| Xanh lá breathing | OTA firmware update đang chạy |
| Flash xanh lá | OTA update xong |
| Đỏ pulse | OTA update thất bại |
| Thở nhẹ ấm (bình thường) | Lamp idle, đang vibe |
