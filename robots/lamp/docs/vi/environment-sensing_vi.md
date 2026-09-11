# Cảm biến môi trường — SEN55 và SCD41

SEN55 đo môi trường: bụi, độ ẩm, nhiệt độ và chỉ số VOC/NOx. HAL cung cấp
capability `environment` tùy chọn, có driver, vòng đời và HTTP snapshot riêng,
cùng tầng với camera và audio. Tích hợp gồm thu nhận HAL, snapshot chỉ đọc qua
web/MQTT và OS phát hiện thay đổi môi trường kéo dài theo cấu hình để gửi agent.
SEN55 không đo chạm, lực, CO₂, CO hay O₂. Component SCD41 tùy chọn thêm
`co2_ppm` đo thật vào cùng capability; không công bố nhiệt độ/độ ẩm đọc trên
bus của SCD41, nên SEN55 vẫn là nguồn của hai chỉ số đó.

## Phạm vi hiện tại: số đo và sự kiện thay đổi kéo dài

HAL giữ snapshot gần nhất trong RAM. Worker môi trường của OS đọc snapshot
độc lập và gửi sự kiện `environment.update` đủ điều kiện qua
`POST /api/sensing/event`; không gọi agent cho từng mẫu cảm biến.
Skill `environment` diễn giải số đo, tham khảo `wellbeing` để đưa gợi ý phù hợp.
Web và MQTT vẫn chỉ đọc, không kích hoạt lượt agent.
Chưa có kho lịch sử môi trường, dịch vụ hẹn kiểm tra lại, tự điều khiển actuator
hay cảnh báo y tế. Lamp vẫn tắt phần cứng này và comment capability;
worker chỉ chạy khi device khai báo capability.

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
Khai báo chuẩn bị sẵn dùng driver `composite`, `routes: [environment]` và
`required: false`.

Chọn component tại `robots/<device>/environment.json`:

```json
{"components": ["sen55", "scd41"]}
```

Có thể chọn từng component độc lập. Thiếu file này giữ cách chạy cũ chỉ có
SEN55; tên lạ hoặc trùng bị từ chối. Mỗi component được chọn có worker và
cấu hình riêng.

Cấu hình SEN55 thuộc device tại `robots/<device>/sen55.json`, dùng map `boards`
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

Với thời gian mặc định, worker SEN55 đọc mỗi 1 giây và thử lại sau 5 giây khi phần cứng lỗi. Không có dữ liệu
mới quá 30 giây sẽ kích hoạt phục hồi qua cùng luồng thử lại. Khi shutdown,
HAL dừng worker và giải phóng bus. Thu nhận dữ liệu chạy riêng với vòng sensing
camera/microphone. Privacy của camera/microphone và sleep không dừng thu nhận
dữ liệu môi trường. Capability này không thêm chính sách actuator hay giới hạn
`SAFETY.md` mới; nhiệt độ môi trường không phải nhiệt độ SoC.

## Component CO₂ SCD41

`robots/lamp/scd41.json` dùng cùng map `boards`. Entry OrangePi đang tắt,
`bus`, `sda_pin`, `scl_pin` đều `null` chờ hardware xác nhận. Khi bật cần số
bus Linux nguyên không âm; chân header là metadata tùy chọn, không cấu hình
pin-mux. Không sao chép dây hay điện áp nguồn SEN55 khi chưa xác nhận board/
breakout SCD41. SCD41 dùng I2C `0x62`, SEN55 dùng `0x69`, nên có thể chung bus
nếu hardware xác nhận dây, logic, nguồn và cấu hình bus tương thích.

Driver dùng đo định kỳ thông thường (mỗi 5 giây có kết quả mới), kiểm tra
data-ready và CRC, chỉ công bố `co2_ppm`. Mặc định `poll_interval_s: 5`,
`retry_interval_s: 5`, `stale_after_s: 15`, `no_data_timeout_s: 30`. Thay nhịp
poll HAL không thay nhịp đo nội bộ 5 giây này.

`automatic_self_calibration: null` giữ cài đặt cảm biến; `true`/`false` đặt
ASC trong RAM trước khi đo. HAL không lưu cài đặt bền vững hay chạy forced
recalibration. Xem điều kiện tiếp xúc không khí của ASC trước khi chọn giá trị;
warm-up OS 60 giây không phải hiệu chuẩn. Xem [datasheet SCD4x](https://sensirion.com/media/documents/48C4B7FB/67FE0194/CD_DS_SCD4x_Datasheet_D1.pdf).
Chưa kiểm chứng dây nối hay số đo SCD41 trên hardware thật.

Log vòng đời HAL dùng key `[sen55]` và `[scd41]`: tắt/khởi động, mẫu hợp lệ
đầu tiên, bắt đầu đo, lỗi thử lại và dừng hiển thị ở INFO (lỗi có thể dùng
WARNING hoặc ERROR). Mỗi mẫu hợp lệ được log ở INFO với timestamp và số đo;
lần poll chưa có mẫu mới log trạng thái chờ và tuổi dữ liệu gần nhất.
`[environment]` ghi component đã chọn/simulation hoặc thiếu capability.

Để theo dõi log trên device, dùng file log hoặc journal systemd:

```sh
tail -F /var/log/hal/server.log | grep --line-buffered -E '\[(environment|sen55|scd41)\]'
journalctl -u hal -f | grep --line-buffered -E '\[(environment|sen55|scd41)\]'
```

Đây là hướng dẫn cho người vận hành; việc ghi tài liệu không thực hiện kết nối
device. Root logging mặc định INFO đã đủ để thấy các dòng này.

## HAL API

Các endpoint thuộc HAL tại port 5001, dùng cơ chế kiểm soát truy cập HAL
hiện có. Chúng khả dụng khi robot nạp route `environment`.

| Endpoint | Hành vi |
|---|---|
| `GET /environment/status` | Trả trạng thái, `last_error`, `sample` gần nhất, `age_s`, `stale` và chẩn đoán từng component, kể cả khi tắt hoặc chưa khả dụng |
| `GET /environment/sample` | Trả snapshot còn mới; HTTP 503 khi chưa khả dụng hoặc đã cũ |
| `GET /health` | Boolean `environment` cho biết có sample còn mới hay không |

Các trạng thái gồm `disabled`, `starting`, `ready`, `error`, `stopped`. Mỗi
entry trong `components` giữ state, enabled, sample, lỗi, tuổi mẫu, bus và
timing riêng. Dữ liệu component không ready hoặc hết hạn không khả dụng;
SEN55 có thanh ghi trạng thái khác zero cũng bị loại khỏi sample tổng hợp.

Nhóm `ready` và không stale khi ít nhất một component đóng góp số đo còn mới.
`partial: true` nghĩa là component khác đang bật nhưng không khả dụng; một
component tắt không tự khiến nhóm partial. Số đo khỏe vẫn dùng được khi sensor
khác lỗi. Không có số đo dùng được thì `sample`, `age_s` của nhóm là `null`,
`stale` là true. `sample` phẳng gồm các trường thuộc component được chọn;
giá trị không khả dụng là `null`.

`sources` ánh xạ chỉ số đến component; `metric_timestamps` giữ Unix timestamp
cho từng chỉ số dùng được. `sample.timestamp` và `age_s` cấp nhóm mô tả dữ liệu
mới nhất, không đại diện mọi chỉ số. Consumer cần kiểm tra trạng thái nguồn
và độ mới từng chỉ số. Bus/timing chỉ có thêm ở cấp cao nhất khi nhóm có một
component. `ready` không chứng nhận warm-up hay hiệu chuẩn cảm biến khí.

Một sample chứa:

| Trường | Ý nghĩa |
|---|---|
| `timestamp` | Unix time tính bằng giây |
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Nồng độ khối lượng bụi, đơn vị µg/m³ |
| `humidity_pct` | Độ ẩm tương đối, % |
| `temperature_c` | Nhiệt độ, °C |
| `voc_index`, `nox_index` | Chỉ số khí không có đơn vị, không phải nồng độ ppm |
| `co2_ppm` | Nồng độ CO₂ đo thật, ppm, chỉ từ SCD41 |

`device_status` của SEN55 nằm trong `components.sen55.sample` để chẩn đoán;
nhóm chỉ có SEN55 vẫn giữ thêm `sample.device_status` để tương thích. Đây
không phải trạng thái chung cho cảm biến CO₂.

Giá trị đo chưa khả dụng được trả bằng JSON `null`; caller không được hiểu
là số không. Phần dưới mô tả phát hiện thay đổi ở OS và diễn giải của agent.

## Hiển thị trên web local

[Device → Sensing](../../../../docs/vi/web-ui_vi.md#58-device--sensing) chỉ hiện
card **Environment** khi device khai báo rõ capability `environment`.
Đang tải hoặc thiếu capability thì không gửi request tới cảm biến. Menu Sensing
không yêu cầu bật debug; hỗ trợ device có `vision`, `environment`
hoặc cả hai, còn card camera vẫn yêu cầu `vision`.

Trình duyệt đọc `GET /api/hardware/environment/status` mỗi 3 giây qua proxy
hardware của OS đã có xác thực, chuyển tới HAL `GET /environment/status`.
Chu kỳ làm mới này độc lập với cấu hình nhịp đọc HAL bên dưới. Card hiển thị
trạng thái, các trường của component có khai báo (tối đa chín số đo gồm CO₂
ppm), nhãn nguồn, thời điểm sample, trạng thái dữ liệu cũ và lỗi.
Giá trị thiếu hoặc cũ hiện `—`; request thất bại được hiển thị rõ để không
trình bày số đo cũ như dữ liệu hiện tại. Lỗi component không che số đo còn tốt.
Bus, thanh ghi trạng thái và timing nằm trong mục kỹ thuật thu gọn theo từng
component ở `status.components`; vẫn hỗ trợ snapshot một sensor kiểu cũ. Đây là màn hình
chỉ đọc, không có ngưỡng tốt/xấu hay lưu lịch sử. Event OS → agent do worker
độc lập bên dưới tạo, không do trình duyệt làm mới.

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

Mỗi entry board nhận các trường thời gian tùy chọn sau (giây). Mặc định bên dưới dành cho SEN55; mặc định SCD41 được nêu ở trên. Giá trị phải là số dương hữu hạn; `stale_after_s` và `no_data_timeout_s` phải lớn hơn `poll_interval_s`. Khởi động lại HAL sau khi sửa. Đây là nhịp đọc của HAL, không thay đổi nhịp đo nội bộ của sensor.

```json
{
  "poll_interval_s": 1.0,
  "retry_interval_s": 5.0,
  "stale_after_s": 5.0,
  "no_data_timeout_s": 30.0
}
```

## Chính sách thay đổi của OS và API cho agent

OS cấu hình diễn giải trong object `environment` cấp cao nhất của
`config/config.json`, qua admin `GET`/`PUT /api/device/config` hiện có.
Cấu hình này tách biệt thời gian HAL trong `sen55.json` và `scd41.json`. Giá trị mặc định:

```json
{
  "environment": {
    "enabled": true,
    "initial_report": true,
    "evaluate_interval_s": 10,
    "sustain_s": 60,
    "cooldown_s": 900,
    "retry_interval_s": 60,
    "max_sample_age_s": 10,
    "metrics": {
      "pm1_0_ug_m3": {"delta": 10, "warmup_s": 60},
      "pm2_5_ug_m3": {"delta": 10, "warmup_s": 60},
      "pm4_0_ug_m3": {"delta": 15, "warmup_s": 60},
      "pm10_ug_m3": {"delta": 15, "warmup_s": 60},
      "temperature_c": {"delta": 2, "warmup_s": 60},
      "humidity_pct": {"delta": 10, "warmup_s": 60},
      "voc_index": {"delta": 50, "warmup_s": 3600},
      "nox_index": {"delta": 20, "warmup_s": 21600},
      "co2_ppm": {"delta": 200, "warmup_s": 60}
    }
  }
}
```

`delta` dùng đơn vị của số đo (độ ẩm dùng điểm phần trăm). Đây là mặc định
phát hiện thay đổi, **không phải giới hạn y tế hay mức chất lượng không khí
tuyệt đối**. Bỏ cả object thì dùng mặc định. Trong object được gửi, trường
cấp cao nhất bị bỏ qua dùng mặc định; gửi `metrics` sẽ thay toàn bộ map chỉ số,
cho phép chỉ theo dõi một nhóm. Mỗi rule cần `delta` dương hữu hạn;
trường bị bỏ trong rule dùng mặc định của chỉ số đó. Trường hoặc map có null
tường minh, map rỗng, trường hoặc chỉ số lạ bị từ chối. Config mới lưu đầy đủ
object mặc định; config cũ thiếu object dùng mặc định mà không tự ghi lại.
Chu kỳ đánh giá, duy trì, retry và tuổi mẫu tối đa phải từ 1–86400 giây;
duy trì và retry không nhỏ hơn chu kỳ đánh giá. Cooldown cho phép 0–604800
giây, warm-up 0–86400 giây. Sửa lúc chạy áp dụng ở tick worker tiếp theo
và reset baseline; không tự bật phần cứng HAL.

Chỉ số đo còn mới, enabled, ready và timestamp tiến lên mới đủ điều kiện.
Cả cờ `stale` của HAL lẫn tuổi mẫu tối đa OS áp dụng cho từng chỉ số và nguồn.
Snapshot tổng hợp dùng `sources`, `components`, `metric_timestamps` để sensor
khỏe không khiến dữ liệu cũ của sensor khác có vẻ mới. SEN55 lỗi chỉ reset
chỉ số của nó, không chặn CO₂ SCD41 còn tốt. Snapshot một sensor kiểu cũ vẫn
dùng timestamp/status cấp cao nhất. Sau khi có số đo hợp lệ liên tục hết warm-up
của từng chỉ số, giá trị đầu tiên tạo baseline phát hiện thay đổi. Mỗi component
HAL có thể trả `continuous_data_s`: thời gian từ mẫu thành công đầu tiên tới
mẫu thành công mới nhất trong đợt thu nhận liên tục. Giá trị là null khi không
hợp lệ, stale hoặc lỗi, và reset khi thu nhận bị gián đoạn. OS dùng thông tin
liên tục này cùng thời gian quan sát hợp lệ local để đáp ứng warm-up khi chỉ
OS restart; HAL cũ thiếu trường này dùng quan sát local. Vẫn kiểm tra độ mới và
tính hợp lệ từng chỉ số; chỉ biết component đã chạy lâu là chưa đủ. Chênh lệch phải đạt `delta` cùng chiều trong `sustain_s`;
giảm dưới mức chênh lệch hoặc đảo chiều sẽ reset thời gian đang chờ.
Chỉ số null reset warm-up/baseline riêng; snapshot không khả dụng hay lỗi đọc
reset số đo của mọi chỉ số. Khoảng gián đoạn lớn cũng reset tính liên tục.
Warm-up là thời gian chờ của OS, không chứng nhận cảm biến đã hiệu chuẩn.

`environment.initial_report` mặc định `true`. Lời chào hệ thống không chờ sensor
hay gọi HAL: chỉ có thể đính kèm snapshot còn mới, đủ warm-up đã cache trong
worker OS bằng JSON `[environment:initial]`. Agent thêm tối đa một câu thực tế
với một hoặc hai số đo vào lời chào bình thường. Cold boot thường chào khi chưa
có dữ liệu môi trường. Sau khi lời chào hoàn tất, OS gửi snapshot đủ điều kiện
đầu tiên một lần qua `environment.update`, với `reason: "initial"` và
`changes: {}`, kể cả chưa có thay đổi đáng kể. Chỉ gửi chỉ số đủ điều kiện;
chỉ số còn warm-up không tạo thêm thông báo ban đầu riêng khi sẵn sàng muộn.
Lời chào thành công có kèm context sẽ tiêu thụ thông báo này; nếu không, nó
vẫn chờ. Dispatch bị từ chối thử lại theo `retry_interval_s`; dispatch được
nhận hoặc xếp queue tiêu thụ thông báo và bắt đầu cooldown chung. Queue là
best-effort, nên hết hạn về sau không tạo lại thông báo ban đầu. Event riêng
vẫn tuân theo capability, policy enabled, sleep, busy và conversation floor.
Trạng thái tồn tại trong tiến trình OS; sensor kết nối lại hoặc sửa config
không tạo lại thông báo đã tiêu thụ. Đặt `initial_report: false` tắt cả dữ liệu
kèm lời chào lẫn update ban đầu, vẫn giữ phát hiện thay đổi kéo dài. Snapshot
ban đầu không chứng minh xu hướng, chẩn đoán sức khỏe hay không khí an toàn.

Event thay đổi đủ điều kiện gồm `[environment:update]` rồi JSON với `observed_at`
(Unix giây), `sample`, `changes` theo tên chỉ số chứa `previous`, `current`,
`previous_at` (Unix giây của baseline), `current_at` (Unix giây của số đo hiện
tại), `source`, `delta` có dấu, và `sustained_s`. Event tổng hợp còn chứa
`sources`, `metric_timestamps`; số đo một sensor kiểu cũ có thể thiếu nguồn.
Body POST chứa event dạng chuỗi JSON trong `message`; bộ định dạng sensing
thêm `[environment:update]` khi chuyển cho agent.
`previous` là baseline ban đầu hoặc sau event
được xác nhận gần nhất của chỉ số đó; `current` là mẫu mới nhất, không phải
trung bình trượt. Cả tăng lẫn giảm đều có thể đủ điều kiện. Dispatch được xác
nhận cập nhật baseline của chỉ số đã gửi và bắt đầu cooldown chung; dispatch
bị từ chối giữ baseline và chờ ít nhất retry interval trước lần thử sau.
Thời lượng event là yêu cầu duy trì theo cấu hình, không phải số đo phơi nhiễm
y tế.

Dispatch kiểm tra capability khai báo rõ, sleep và conversation floor kể cả
khi bật guard mode. Queue lúc agent bận chỉ giữ event môi trường mới nhất,
hết hạn sau 60 giây, kiểm tra lại capability/sleep/policy enabled trước khi phát
lại. Đặt `environment.enabled: false` cũng khiến event handler trả
`dropped_disabled`; chỉ dừng diễn giải tự động, không dừng thu nhận HAL hoặc
đọc status chẩn đoán.
Xác nhận queued là best-effort: event vẫn có thể hết hạn hoặc bị bỏ sau đó;
không bảo đảm người dùng nghe thông báo. Event môi trường tự động không bắt
buộc phản ứng camera, emotion marker hay hành động vật lý.

Tool agent local đọc `GET http://127.0.0.1:5000/api/environment/status`.
Route OS chỉ cho loopback, kiểm tra capability và trả envelope chuẩn
`{"status":1,"data":{...HAL snapshot...},"message":null}`. Route đọc snapshot
chẩn đoán đã cache trong HAL, không ép phần cứng đo ngay; caller phải kiểm tra
readiness, độ mới và từng giá trị null. Thiếu capability trả HTTP 403; lỗi
đọc/định dạng HAL trả HTTP 502. Snapshot disabled/error/stale vẫn có thể là
response chẩn đoán thành công. Browser và MQTT tiếp tục dùng route xác thực
hiện có.

## Skill environment và use case well-being

`skills/environment/SKILL.md` yêu cầu capability và sở hữu diễn giải dữ liệu.
Skill bị loại khi capability thiếu hoặc rỗng, kể cả khi skill cũ vẫn giữ cơ
chế khả dụng dự phòng.
Skill chỉ tham khảo mục environmental-care của `skills/wellbeing/SKILL.md`
để chọn thời điểm và lời nhắc, tránh vòng lặp định tuyến. Hỏi về phòng không
cần quan sát camera, danh tính, log hoạt động hay bộ đếm uống nước.

- **Hỏi về phòng:** đọc status một lần, báo số đo hữu ích; thiếu dữ liệu hoặc
  dữ liệu cũ là chưa biết, không phải không ô nhiễm hay bằng chứng an toàn.
- **Khởi động:** chào ngay; có thể thêm một câu từ số đo đã cache đủ điều kiện.
  Nếu chưa có, snapshot đầu tiên đủ điều kiện có thể tạo update riêng sau lời
  chào, không chào lần nữa hay gọi API cho bản tin này. Bỏ số đo ban đầu đã cũ.
- **Thay đổi kéo dài:** giải thích thay đổi có căn cứ, gợi ý tối đa một hành
  động hữu ích khi phù hợp. Tôn trọng yêu cầu yên tĩnh/ngủ; trả `NO_REPLY`
  nếu không có thông báo hữu ích.
- **Sau một hành động:** so với số đo trước có timestamp thật trong event/hội
  thoại. Báo tốt lên hoặc xấu đi mà không khẳng định quan hệ nhân quả. Thiếu
  baseline thì nói chưa so sánh được. Không hứa hẹn giờ kiểm tra lại, tự polling
  hay bịa kho lịch sử bền vững.

VOC/NOx là chỉ số tương đối, không phải ppm hay nhận diện hóa chất. SEN55
không hỗ trợ kết luận CO₂/O₂. Chỉ dùng SCD41 để nói về CO₂ khi `co2_ppm` đo
thật còn mới; không suy ra CO₂ từ VOC/NOx. Cả hai không đo O₂, CO hay tạo
cảnh báo khói/cháy. PM tức thời không chứng minh đáp ứng hướng dẫn
WHO về phơi nhiễm 24 giờ hoặc cả năm. Xét điều kiện ngoài trời trước khi gợi
ý thông gió, và nhiệt vỏ máy trước khi diễn giải nhiệt độ. Skill không chẩn
đoán sức khỏe, tự bịa ngưỡng, tiếp tục việc không liên quan hay điều khiển máy
lọc/quạt/HVAC nếu thiếu tích hợp khả dụng và quyền từ người dùng.
Không thêm loại log well-being riêng cho môi trường.
