# Status LED — Đặc Tả

Status LED giúp user nhìn đèn là biết Lamp đang làm gì bên trong.
Không có tín hiệu này, user không phân biệt được Lamp đang khởi động, đang update, đang mất kết nối với AI brain, hay bị lỗi.

## Nguyên Tắc

1. **Nhìn là hiểu** — mỗi trạng thái có màu riêng, không cần đoán.
2. **Không xung đột** — status LED nhường quyền cho scene/emotion do user chọn. Khi trạng thái kết thúc, strip được trả về đúng trạng thái user (hoặc agent) đã set, ambient resume sau khoảng im lặng.
3. **Ưu tiên** — khi nhiều trạng thái active cùng lúc, trạng thái cao nhất thắng.

## Các Trạng Thái

Tất cả các state dùng effect `breathing` trừ khi ghi rõ. Bảng base chạy nó ở speed 3.0, nhưng trên lamp sáu cue sống lâu — `ota`, `booting`, `connectivity`, `hal_down`, `agent_down` và `hardware` — thở ở speed 0.6 qua overlay. Giá trị RGB gốc nằm trong `STATUS_LED_PRESETS` của `hal/presets.py` (phía Go chỉ gửi *tên* state), nhưng trên lamp chúng **đã bị override** bởi khối `status_led` trong `robots/lamp/presets.json` — overlay per-device được merge lúc boot bởi `hal/board/presets_overlay.py`. Overlay patch cả `color` lẫn `speed` (speed cho sáu cue nói trên); tên effect vẫn lấy từ bảng base, và robot khác giữ nguyên giá trị base. Các giá trị vẫn được tune về luminance thấp và cân bằng theo hue để cue sáng lâu không bị chói.

| Trạng thái (hằng số code) | Màu | RGB | Ý nghĩa | Trigger | Tự tắt |
|---|---|---|---|---|---|
| `StateConnectivity` | Cam | `(5, 2, 0)` | **Mất internet** — Wi-Fi kết nối nhưng không có internet | Network monitor: 5 lần ping thất bại liên tiếp (~25s) | Có — khi ping thành công |
| `StateError` | Đỏ | `(5, 0, 0)` | **Lỗi** — Lỗi hệ thống (reserved) | Lỗi nghiêm trọng | Có — khi lỗi được khắc phục |
| `StateOTA` | Xanh lá | `(0, 4, 0)` | **Đang update** — OTA firmware đang chạy (enum dự trữ; bootstrap drive LED OTA trực tiếp qua `lib/hal` — xem "Bootstrap (OTA)" bên dưới) | Bootstrap reconcile phát hiện update | Khởi động lại sau khi update xong |
| `StateBooting` | Xanh dương | `(0, 2, 5)` | **Đang khởi động** — Lamp đang bật | `server.go` lúc startup | Có — khi OpenClaw agent connect và sẵn sàng |
| `StateLeLampDown` | Tím | `(4, 0, 5)` | **HAL Down** — Server phần cứng không phản hồi. Khi HAL đang down LED **tắt hẳn** vì driver LED cũng chết theo; tím breathing chỉ flash ~3s khi phục hồi | `healthwatch` poll HAL `/health` thất bại | Tự tắt 3s sau khi phục hồi |
| `StateAgentDown` | Cyan | `(0, 4, 4)` | **Agent Down** — AI brain mất kết nối | OpenClaw WebSocket ngắt (`runtimes/openclaw/service_ws.go`) | Có — khi WebSocket reconnect |
| `StateHardware` | Vàng | `(4, 4, 0)` | **Hardware Failure** — servo/LED/audio/voice không healthy qua HAL `/health` | `healthwatch` poll (mỗi 5s); camera và sensing không tính | Có — khi tất cả linh kiện báo OK |

### Độ sáng (overlay của lamp)

Ngày 24/08/2026 độ sáng status cue trên lamp được chỉnh bằng mắt trên lamp-0c89, qua ba lượt. Lượt 1 hạ đúng một nửa so với base (12 với các cue nhiều xanh lá, 16 với các cue ít xanh lá). Lượt 2 hạ tiếp riêng nhóm xanh 6 → 4, vì xanh ở mức 6 vẫn chói trong khi đỏ ở mức 8 nhìn đã ổn — die xanh của WS2812 sáng hơn die đỏ ở cùng giá trị nhiều hơn mức mà luật 12-vs-16 bù được. Lượt 3 hạ nhóm low-green 8 → 5. Mọi lượt đều scale tỉ lệ trên cả ba kênh nên hue không đổi, chỉ luminance giảm. Nửa còn lại của cách sửa là **nhịp** chứ không phải mức: mấy cue này chạy `breathing` speed 3.0 — nhanh nhất trong file — và thở nhanh mà kéo dài thì mắt đọc thành "đang nhấp nháy" chứ không phải ánh sáng (đúng vết `listening` hồi 21/08, hạ màu không ăn thua), nên chúng được hạ về 0.6. `setup` được hạ ngày 25/08/2026, 16 → `(3, 3, 3)`, nhìn bằng mắt lần lượt ở 8, rồi 5, rồi 3. Nó đứng ngoài cả ba lượt trên, và lý do cuối cùng nó phải xuống THẤP HƠN các cue khác chứ không cao hơn là vì nó là cue `solid` duy nhất: sáng cả 32 pixel cùng lúc nên tổng flux cao hơn nhiều so với con số peak — đúng cái bẫy đã ghi cho bảng scene, nơi chấm điểm một look sáng nguyên vòng bằng peak đã ra quá sáng ở lượt đầu. Peak 3 vẫn nhìn thấy từ xa vì 32 pixel sáng là một nguồn lớn, và đó mới là thứ onboarding cần. Sàn truncate mỗi frame nói dưới đây cũng không áp cho nó, vì `solid` không scale theo frame. `mic_muted` thì **cố ý** không hạ: là đèn báo privacy, phải đọc được trong phòng sáng. Lưu ý sàn peak ~8 ghi trong `hal/presets.py`: dưới mức đó `breathing`/`pulse` bị truncate mỗi frame (`int(c * brightness)` trong `hal/drivers/rgb/effects.py`) nên một chu kỳ chỉ còn rất ít mức sáng và strip có thể thấy giật cấp — đó là thứ cần soi bằng mắt trên máy thật. Sau các lượt hạ này, `error` và `mic_muted` trùng màu `(5, 0, 0)`; phân biệt bằng hình dạng (pulse vs breathing) — đúng cách bảng base vẫn dùng.

### Ready flash

Sau khi boot xong (Booting clear và không state nào khác active), `statusled.FlashReady()` bắn flash **trắng** `(4, 4, 4)` ngắn `notification_flash` ~1s để báo agent sẵn sàng nhận lệnh. Sẽ không bắn nếu có status state nào đang active.

### OTA chi tiết (do bootstrap drive)

Bootstrap binary gọi `lib/hal` trực tiếp (không qua `statusled.Service`):

| Giai đoạn | LED | Source |
|---|---|---|
| Đang tải + cài | Cam `(5, 2, 0)` `breathing` speed 0.4 | `bootstrap/bootstrap.go` |
| Thành công | Xanh lá `(0, 4, 1)` `notification_flash` ngắn rồi dừng | `bootstrap/bootstrap.go` |
| Thất bại | Đỏ `(5, 1, 1)` `pulse` speed 1.5 | `bootstrap/bootstrap.go` |

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

`system/statusled/Service` quản lý các state active với priority map. Caller `Set` và `Clear` các named state; service apply LED effect cho state có priority cao nhất.

Các caller thực tế (đã verify với code):

```
server.go                    → Set/Clear StateBooting + StateConnectivity + FlashReady
runtimes/openclaw/service_ws → Set/Clear StateAgentDown
system/healthwatch/service → Set/Clear StateLeLampDown + StateHardware
```

Service gọi HAL `/led/effect` qua `lib/hal` (shared HTTP client).

### Bootstrap (bootstrap-server)

Bootstrap là binary riêng. Gọi `lib/hal` **trực tiếp** trong hàm `reconcile` (không qua `statusled.Service`):

```
reconcile phát hiện update → lelamp.SetEffect("breathing", 16, 8, 0, 0.4)   // cam
        ↓ cài update...
thành công → lelamp.SetEffect("notification_flash", 0, 255, 80, 1.0)            // xanh lá flash
thất bại   → lelamp.SetEffect("pulse", 16, 2, 2, 1.5)                        // đỏ pulse
```

## Tích Hợp Với Ambient

Ambient service (`system/ambient`) tự pause khi có interaction event (`chat_send`, `chat_response`, v.v.). Khi `statusled.Service` clear state cuối cùng, nó gọi `lelamp.RestoreLED()` — strip trở về màu/effect mà user (hoặc agent) đã set qua `/led/solid`, `/led/effect`, hoặc `/scene`. Nếu chưa từng có user state, strip clear về off và ambient sẽ resume breathing sau 60s im lặng.

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

1. **Emotion preset** — màu theo cảm xúc của AI agent (xem [emotion-led-mapping_vi.md](emotion-led-mapping_vi.md))
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
