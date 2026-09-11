# Cảm biến môi trường — SEN55

SEN55 đo môi trường: bụi, độ ẩm, nhiệt độ và chỉ số VOC/NOx. HAL cung cấp
capability `environment` tùy chọn, có driver, vòng đời và HTTP snapshot riêng,
cùng tầng với camera và audio. Tích hợp thu nhận dữ liệu, hiển thị chỉ đọc trên web local
và trả snapshot qua MQTT theo yêu cầu, chưa
định nghĩa ngưỡng, thông báo, sensing event gửi OS hay hành vi agent. SEN55
không đo chạm hoặc lực; cảm biến xúc giác cần phần cứng khác.

## Phạm vi hiện tại: chỉ xem dữ liệu

Khi có mẫu đo mới, HAL chỉ cập nhật snapshot gần nhất trong RAM.
HAL **chưa tự gửi dữ liệu vào pipeline sensing/event của OS hoặc cho agent**.
Số đo chưa kích hoạt prompt cho agent, cảnh báo, phản ứng tự động hay lưu lịch sử.

OS server chỉ chuyển tiếp snapshot khi web hoặc client MQTT yêu cầu xem.
Luồng hỏi/đáp này chưa phải tích hợp sensing với OS/agent; phần tích hợp đó
sẽ làm ở giai đoạn sau.

## Đấu dây và lắp đặt

Xác định chiều connector theo [datasheet SEN5x của Sensirion, mục 4
và 6](https://sensirion.com/media/documents/6791EFA0/62A1F68F/Sensirion_Datasheet_Environmental_Node_SEN5x.pdf),
không dựa vào màu dây. Đầu cáp tương thích là JST GHR-06V-S.

| Chân SEN55 | Tín hiệu | Kết nối |
|---|---|---|
| 1 | VDD | Nguồn 5 V |
| 2 | GND | GND chung với host |
| 3 | SDA | Dữ liệu I2C của host |
| 4 | SCL | Clock I2C của host |
| 5 | SEL | Nối GND trước khi cấp nguồn |
| 6 | NC | Để trống |

Ghi nhận từ bên hardware do chủ device cung cấp: **header phía OrangePi** dùng
**chân vật lý 3 là SDA**, **chân vật lý 5 là SCL**. Đây là vị trí chân header
host, không phải số chân connector SEN55: nối OrangePi pin 3 với SEN55 pin 3
(SDA), OrangePi pin 5 với SEN55 pin 4 (SCL). Số bus Linux `/dev/i2c-N` và cấu
hình pin-mux vẫn cần xác nhận; chưa kiểm chứng trên device và ghi chú này
không bật thu nhận dữ liệu.

`sen55.json` ghi vị trí chân vật lý phía host bằng `sda_pin: 3` và `scl_pin: 5`.
Đây là metadata dây nối; driver dùng `bus`, không tự cấu hình pin-mux GPIO
từ hai trường này.

SDA/SCL hỗ trợ logic 3,3 V. Dùng điện trở kéo lên 3,3 V với host 3,3 V;
5 V cấp nguồn cảm biến, không cấp cho GPIO host. Địa chỉ I2C là `0x69`,
tốc độ bus tối đa 100 kHz. Cần xác định board thực tế, sơ đồ chân header,
pin multiplexing, bus khả dụng và khả năng cấp nguồn trước khi đấu dây.
HAL không chọn chân header hay cấu hình tốc độ bus. Khi lắp, giữ thông
thoáng cửa hút/xả khí và tránh nhiệt từ host.

Tích hợp này chưa được kiểm chứng dây nối, cấu hình I2C trên board hay
số đo với SEN55 thật.

## Bật trong HAL

Dòng khai báo tùy chọn `environment` trong `ROBOT.md` của Lamp đang được comment.
HAL chưa mount API hoặc đọc cấu hình dây nối cho đến khi bỏ comment dòng này.
Khai báo chuẩn bị sẵn dùng driver `sen55`, `routes: [environment]` và
`required: false`.

Cấu hình thuộc device tại `robots/<device>/sen55.json`, dùng map `boards`
như `mpr121.json`. Board mục tiêu là OrangePi (`orangepi_sun60`); Lamp có entry tắt
(`{"enabled": false}`) cho board này và chưa giả định bus. Thiếu file hoặc
entry của board đang chọn thì cảm biến tắt. Entry tắt có thể bỏ `bus` hoặc để `null` khi chưa biết bus.
Khi bật, `bus` phải là số nguyên không âm.
Cấu hình sai, kể cả trường không được hỗ trợ, bị từ chối khi khởi động.

Để bật sau khi xác nhận dây nối, bỏ comment capability trong `ROBOT.md`,
sửa entry đúng board rồi khởi động lại HAL. Mẫu sau có placeholder,
chưa phải JSON có thể nạp trực tiếp:

```text
{
  "boards": {
    "<actual board ID>": {"enabled": true, "bus": <actual bus number>}
  }
}
```

Thay board ID bằng board được nhận diện và placeholder bus bằng số bus Linux
thực tế, là số nguyên không âm. Không có bus mặc định. Bật interface I2C của
kernel, cấu hình pin multiplexing và tốc độ bus theo board đó; tích hợp này
đã chọn OrangePi nhưng vẫn cần xác nhận dây nối và bus thực tế. HAL truy cập `/dev/i2c-N` bằng thư viện
chuẩn Python; process cần quyền mở thiết bị này. Không cần thêm package I2C
Python. Chế độ mô phỏng không bao giờ truy cập phần cứng, kể cả entry đã bật.

Với thời gian mặc định, worker đọc mỗi 1 giây và thử lại sau 5 giây khi phần cứng lỗi. Không có dữ liệu
mới quá 30 giây sẽ kích hoạt phục hồi qua cùng luồng thử lại. Khi shutdown,
HAL dừng worker và giải phóng bus. Thu nhận dữ liệu chạy riêng với vòng sensing
camera/microphone. Privacy của camera/microphone và sleep không dừng thu nhận
dữ liệu môi trường. Capability này không thêm chính sách actuator hay giới hạn
`SAFETY.md` mới; nhiệt độ môi trường không phải nhiệt độ SoC.

## HAL API

Các endpoint thuộc HAL tại port 5001, dùng cơ chế kiểm soát truy cập HAL
hiện có. Chúng khả dụng khi robot nạp route `environment`.

| Endpoint | Hành vi |
|---|---|
| `GET /environment/status` | Trả trạng thái, `last_error`, `sample` gần nhất, `age_s`, `stale` và cấu hình `timing`, kể cả khi tắt hoặc chưa khả dụng |
| `GET /environment/sample` | Trả snapshot còn mới; HTTP 503 khi chưa khả dụng hoặc đã cũ |
| `GET /health` | Boolean `environment` cho biết có sample còn mới hay không |

Các trạng thái gồm `disabled`, `starting`, `ready`, `error`, `stopped`.
Sample quá `stale_after_s` (mặc định 5 giây) là stale; mọi trạng thái khác `ready` cũng là stale,
bao gồm `error`, `disabled`, `stopped`. Khi chưa có sample, `sample` và `age_s` là
`null`, `stale` là true. Sample được giữ trong status phục vụ chẩn đoán;
caller phải kiểm tra độ mới. `ready` nghĩa là đã có dữ liệu, không khẳng
định cảm biến khí đã hoàn thành warm-up hay thích nghi.

Một sample chứa:

| Trường | Ý nghĩa |
|---|---|
| `timestamp` | Unix time tính bằng giây |
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Nồng độ khối lượng bụi, đơn vị µg/m³ |
| `humidity_pct` | Độ ẩm tương đối, % |
| `temperature_c` | Nhiệt độ, °C |
| `voc_index`, `nox_index` | Chỉ số khí không có đơn vị, không phải nồng độ ppm |
| `device_status` | Bitmask thanh ghi trạng thái cảm biến |

Giá trị đo chưa khả dụng được trả bằng JSON `null`; caller không được hiểu
là số không. Số đo và trạng thái để việc diễn giải cho bước tích hợp OS/agent
sau này.

## Hiển thị trên web local

[Device → Sensing](../../../../docs/vi/web-ui_vi.md#58-device--sensing) chỉ hiện
card **Environment · SEN55** khi device khai báo rõ capability `environment`.
Đang tải hoặc thiếu capability thì không gửi request tới cảm biến. Menu Sensing
không yêu cầu bật debug; hỗ trợ device có `vision`, `environment`
hoặc cả hai, còn card camera vẫn yêu cầu `vision`.

Trình duyệt đọc `GET /api/hardware/environment/status` mỗi 3 giây qua proxy
hardware của OS đã có xác thực, chuyển tới HAL `GET /environment/status`.
Chu kỳ làm mới này độc lập với cấu hình nhịp đọc HAL bên dưới. Card hiển thị
trạng thái, tám số đo, thời điểm sample, trạng thái dữ liệu cũ và lỗi.
Giá trị thiếu hoặc cũ hiện `—`; request thất bại được hiển thị rõ để không
trình bày số đo cũ như dữ liệu hiện tại. Bus, thanh ghi trạng thái cảm biến và
cấu hình thời gian nằm trong mục kỹ thuật thu gọn mặc định; các mốc thời gian
lấy từ `status.timing`. Đây là màn hình
chỉ đọc, không có ngưỡng tốt/xấu, lưu lịch sử hay event OS → agent.

Card vẫn ẩn với capability đang comment của Lamp. Nếu khai báo capability
nhưng giữ `enabled: false`, card hiển thị trạng thái đã tắt.

## Đọc qua MQTT

Mobile/backend gửi `{"cmd":"data","kind":"environment.status","data":{}}`
trên `fa_channel`; OS trả `MQTTDataResponse` trên `fd_channel`, cùng `kind`,
`status: "success"` và snapshot HAL trong `data`. Handler yêu cầu capability
`environment` được khai báo, không yêu cầu model SEN55. HAL local có timeout
5 giây. Sensor đang tắt, lỗi hoặc dữ liệu cũ vẫn là snapshot hợp lệ: caller phải
kiểm tra `state`, `stale`, `sample`, `last_error` trong `data`.

Thiếu capability trả `status: "failure"`,
`error: "environment capability not declared"`. Lỗi kết nối HAL, HTTP khác 200
hoặc status JSON không hợp lệ cũng trả failure. Lamp hiện vẫn comment capability
nên trả lỗi thiếu capability. Đây là request/reply, không stream hay event tự
động, không gọi agent. Xem [giao thức MQTT](../../../../docs/vi/mqtt_vi.md)
để biết payload và quy tắc phản hồi.

## Cấu hình thời gian

Mỗi entry board nhận các trường thời gian tùy chọn sau (giây). Bỏ qua trường nào thì dùng mặc định tương ứng. Giá trị phải là số dương hữu hạn; `stale_after_s` và `no_data_timeout_s` phải lớn hơn `poll_interval_s`. Khởi động lại HAL sau khi sửa. Đây là nhịp đọc của HAL, không thay đổi nhịp đo nội bộ của sensor.

```json
{
  "poll_interval_s": 1.0,
  "retry_interval_s": 5.0,
  "stale_after_s": 5.0,
  "no_data_timeout_s": 30.0
}
```
