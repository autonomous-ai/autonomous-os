# Safety Engine (Bộ máy An toàn)

Safety engine thực thi các **giới hạn (bounds)** trong `SAFETY.md` của thiết bị một
cách **tất định, ngay trong runtime**, *dưới* tầng agent. Đây là cơ chế hiện thực
nguyên tắc số một trong `contract/SAFETY-SPEC.md`: *an toàn nằm dưới bộ não.* Agent
yêu cầu hành động; engine quyết định — trên **mọi** yêu cầu, bất kể ai phát ra —
phần cứng có được phép thực thi không và trong giới hạn nào.

> **Trạng thái:** Slice 1 (trần độ sáng) đã **được hiện thực và thực thi** —
> `os/hal/safety/policy.py` + gate LED trong `rgb_service.py`, nạp từ
> `devices/lamp/SAFETY.md`. Các slice sau tái dùng cùng loader + gate. Mỗi dòng bảng
> dưới được đánh dấu đã-thực-thi / dự-trữ. Tài liệu này bám theo code, không ngược lại.

## Vì sao cần engine, không phải nhắc (prompt) agent

Định tuyến an toàn qua mô hình ngôn ngữ là không đáng tin — nó có thể bị thuyết phục
bỏ qua việc từ chối, có thể bịa ra giới hạn, và **không** đảm bảo được một hành động
đã *không* xảy ra. Guard mode trong codebase này đã từng được dựng lại để gửi cảnh báo
một cách tất định cũng vì lý do đó. Safety engine tổng quát hóa điều này: **runtime**,
không phải gateway, là điểm duy nhất clamp/chặn/dừng.

## Kiến trúc

Ba tầng, soi gương tầng thiết bị (`DEVICE.md` → capability → route → driver):

```
SAFETY.md front matter        các giới hạn đã khai (contract máy đọc; theo capability group)
        │  resolve qua DEVICE.md safety_ref (path hoặc http), parse lúc boot
        ▼
os/hal/safety/policy.py        SafetyPolicy thuần + gate functions (không IO, unit-testable)
        │  clamp_brightness(requested) -> min(requested, ceiling)   [slice 1]
        ▼
HAL capability routes          gọi gate TRƯỚC khi actuate (led, sau này servo/music)
        │  tất định, in-process, agent không bypass được
        ▼
phần cứng
```

- **`SAFETY.md` front matter** — các bound, keyed theo capability group. Schema + bảng
  field: `contract/SAFETY-SPEC.md`.
- **`os/hal/safety/policy.py`** — loader thuần (parse front-matter bằng regex,
  dependency-free, cùng kỷ luật với `os/hal/board/device.py`) tạo ra `SafetyPolicy`
  có kiểu, kèm các gate function thuần. Không phần cứng, không tác dụng phụ về đồng hồ,
  unit-testable hoàn toàn off-hardware.
- **Các route** — mỗi capability route hỏi gate tại điểm actuate. Route LED clamp độ
  sáng; sau này route servo clamp speed/accel và đảm bảo `stop`. Vì gate là một lời gọi
  hàm nằm trên đường yêu cầu, không có đường nào vòng qua nó.

Policy được nạp một lần lúc boot (cùng `DeviceProfile`) và lộ ra qua endpoint thiết bị
để các bound khai báo quan sát được: `GET /device` đã trả `safety_ref`; các bound
resolve cũng hiện ở đó.

## Ngữ nghĩa fail-safe

Theo mức tới hạn của từng capability (quy tắc đầy đủ ở `contract/SAFETY-SPEC.md`):

| Capability | Bound vắng / không nạp được | Lý do |
|------------|------------------------------|-------|
| light, audio | pass-through (chỉ log) | đèn dịu/loa im không phải mối nguy; đừng bịa giới hạn |
| motion | **fail-closed** (từ chối actuate) | chuyển động với giới hạn chưa biết là lỗi phần cứng |

`SAFETY.md` là tùy chọn. Tag `schema` được validate như của `DEVICE.md` — `schema`
thiếu hoặc major lạ sẽ abort boot thay vì thực thi một ABI không đọc được.

## Lộ trình slice

| Slice | Phạm vi | Gate | Thực thi ở đâu | Trạng thái |
|-------|---------|------|----------------|-----------|
| 1 | trần `light.max_brightness` | `clamp_brightness` / `clamp_color` | gate LED (`rgb_service` `_handle_solid`/`_handle_paint`) | **đã thực thi (v1)** |
| 2 | `quiet_hours` (light + audio) | `active_max_brightness` (theo giờ) + `audio_quiet_now` | gate LED + route music | **đã thực thi (v1)** |
| 3 | `motion.max_speed`/`max_accel`, `stop_always` (fail-closed) | `clamp_motion`, đảm bảo stop | route servo | dự trữ |
| 4 | trạng thái fail-safe (mất mạng → giữ pose, lỗi board → tắt capability) | state gate | lifespan + routes | dự trữ |

Mỗi slice thêm field vào `SafetyPolicy` và gate function rồi nối một/nhiều route;
loader và contract front-matter **không** đổi hình dạng giữa các slice (chỉ thêm field
— ABI `autonomous.safety.v1`).

## Kiểm chứng việc thực thi (verify enforcement)

Một bound chỉ thực sự tồn tại nếu bạn *chứng minh* được nó giữ vững **và** agent không
lách qua được. Mỗi slice được kiểm ở ba mức; bound chưa "xong" cho tới khi cả ba pass.
(Khác với `devices/lamp/docs/security-test.md` — cái đó về security mạng/kiểm soát truy
cập: port, RCE, CORS — không phải giới hạn actuation.)

1. **Unit (gate thuần, off-hardware).** Gate là hàm thuần nên giới hạn của nó là một
   bảng test: yêu cầu trên trần thì clamp về trần, yêu cầu dưới trần thì giữ nguyên,
   bound vắng thì hành xử theo quy tắc fail-safe. Chạy trong CI không cần thiết bị.
2. **Runtime (trên thiết bị, qua route thật).** Phát yêu cầu actuate qua HTTP và quan
   sát giá trị *thực tế đẩy ra phần cứng*, không phải giá trị đã xin. Bound khai báo
   cũng quan sát được ở `GET /device`, nên test khẳng định *request vs trần vs output
   quan sát* khớp nhau.
3. **Bypass audit (kiểm tra tới hạn của an toàn).** Xác nhận **không** có đường nào tới
   actuator né được gate — phát cùng hành động qua **mọi** route có thể điều khiển nó
   (đường agent, route trực tiếp, mọi endpoint raw/cấp thấp) và xác nhận mỗi đường đều
   bị clamp. Bound thực thi trên một đường nhưng chạm được qua đường khác = chưa thực thi.

### Slice 1 — trần độ sáng (checklist)

- [x] **Unit:** `clamp_brightness(255)` với `max_brightness: 180` → `180`;
      `clamp_brightness(120)` → `120`; không khai → giữ nguyên (pass-through).
      `clamp_color` scale giữ hue (trắng→180,180,180; đỏ→180,0,0). Schema
      thiếu/sai/major-lạ fail-loud. (`os/hal/test/test_safety.py`, 21 test.)
- [x] **Runtime:** đã verify trên Lamp thật — `GET /device` trả
      `"safety": {"light": {"max_brightness": 180}}`; `POST /led/solid` trắng đầy (255)
      đọc lại `[180,180,180]`, `[100,50,0]` giữ nguyên, `[255,0,0]` → `[180,0,0]`.
- [x] **Bypass audit:** gate nằm ở `rgb_service` `_handle_solid` / `_handle_paint` —
      chokepoint duy nhất mọi lệnh ghi pixel đi qua. Mọi caller (route LED, effects,
      app_state, scene, gpio_button, presence, smooth_animation, main) đều dùng
      `dispatch(RGB_CMD_*)` → 2 handler đó; chỉ còn `_driver` ghi trực tiếp bên trong
      chúng (sau clamp) + `clear()` (màu đen). Grep xác nhận không đường nào né.
- [x] **Tất định:** `clamp_color` thuần, không hỏi caller — clamp giống hệt dù từ agent, Web UI hay `curl` thô —
      gate không quan tâm ai xin.

### Slice 2 — quiet hours (checklist)

Quiet hours thêm chiều **thời gian**: trong khung giờ trong ngày, trần LED giảm và
nhạc bị chặn (giờ wall-clock local; máy chạy cả ngày nên đây là giờ thực, không
phải "tắt ban đêm"). Gate đọc đồng hồ mỗi request → đổi ngay ở ranh giới, không cần
restart.

- [x] **Unit (clock inject):** `in_window` xử lý wrap nửa đêm (22:00→07:00 đúng lúc
      23:00 và 06:00, sai lúc 12:00, loại trừ 07:00); `active_max_brightness` trả trần
      giảm (40) trong khung, base (180) ngoài khung; `clamp_color((255,255,255),
      now=23:00)` → `(40,40,40)`, `now=12:00` → `(180,180,180)`; `audio_quiet_now`
      true trong khung, false ngoài / khi không có policy. (`os/hal/test/test_safety.py`.)
- [x] **Runtime:** `GET /device` báo `safety.light.quiet_hours` +
      `safety.audio.quiet_hours`. Đặt khung trùng giờ hiện tại → vòng LED kẹp xuống
      trần giảm và `POST /audio/play` trả `{"status":"suppressed"}`; ngoài khung thì
      bình thường.
- [x] **Bypass audit:** trần quiet LED đi chung chokepoint `rgb_service`
      `_handle_solid`/`_handle_paint` như slice 1 (không thêm đường). Chặn nhạc nằm ở
      `/audio/play` — route duy nhất khởi động audio tùy ý (TTS cố ý miễn trừ).
- [x] **Tất định:** kiểm tra khung giờ thuần khi truyền `now`; chỗ duy nhất không
      thuần là đọc đồng hồ hệ thống, cô lập trong `policy._now()` để test inject giờ.

Các slice sau mở rộng checklist này (motion: khẳng định fail-closed khi bound vắng,
và `stop` chiếm quyền trước một lệnh move đang chạy).

## Quan hệ với enforcement hardcode rời rạc hiện có

Một số hành vi an toàn đã tồn tại, hardcode và rải rác: `motion.stop()` trong service
motors/animation, clamp vị trí cơ học của lerobot, config scale độ sáng LED. Engine
**không** gỡ hết một lúc — nó *tập trung hóa* chúng vào policy khai báo từng slice một,
để các bound trở thành **dữ liệu thiết bị khai** thay vì hằng số chôn trong driver.
Slice 1 giới thiệu engine bằng một bound **chưa** từng được thực thi (trần độ sáng độc
lập với agent), chứng minh đường đi đầu-cuối trước khi di trú các mảnh đã có.
