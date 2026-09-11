# Mẫu tích hợp thân robot qua Wi-Fi

Robot không cần chạy Autonomous OS trên vi điều khiển. OS và HAL có thể chạy
trên máy tính, còn firmware robot quản lý phần cứng. Stack-chan là ví dụ thử
nghiệm của mẫu driver hiện có, không phải capability mới hay API OS riêng.

[English](../WIFI-BODY.md)

## Trách nhiệm của host và firmware

```text
Agent / OS -> route HAL hiện có -> driver MotionService
                                  -> kết nối mạng có xác thực
                                     -> firmware -> cơ cấu chấp hành
```

Host nạp profile, chọn driver qua `hal/drivers/motors/factory.py` và mount các
route được khai báo. Driver thực hiện contract `MotionService` hiện có, gồm
quyền sở hữu thân máy, vị trí đo được, chuyển động có giới hạn, halt và release.
Robot qua mạng không nên buộc skill dùng một bộ HTTP endpoint riêng theo robot.

Firmware quản lý nội suy vật lý, tính hợp lệ của feedback, torque và phản ứng
độc lập khi mất host hoặc đường truyền. Exception trên host không phải thao tác
dừng vật lý. Cần xác định và kiểm chứng cách firmware hủy chuyển động, giữ vị
trí hoặc chuyển sang trạng thái an toàn của phần cứng khi mất liên lạc.

## Chọn device và host

`DEVICE_TYPE` xác định profile robot. `HAL_BOARD` xác định máy chạy HAL. Máy
tính điều khiển robot từ xa có thể được profile khai báo `boards: [host]` và
chọn bằng `HAL_BOARD=host`. Board host bỏ qua thiết bị GPIO cục bộ; nó không
thay driver mạng bằng mock. `HAL_SIMULATE=1` là chế độ riêng thay driver bằng
mô phỏng. Giữ giá trị `0` khi điều khiển phần cứng từ xa thật.

Tích hợp chuyển động tối thiểu khai báo `motion` cùng driver và `system`, kèm
`SAFETY.md` và `SOUL.md`. Cấu hình HAL riêng của robot nằm trong
`rootfs/opt/hal/.env` thuộc profile; credential được cung cấp cục bộ. Với profile
chỉ có motion chưa qualification, dùng thư mục thử nghiệm có tiền tố gạch dưới,
không nhận vơ capability chưa tồn tại. Ví dụ Stack-chan dùng
`DEVICE_TYPE=stackchan`, `DEVICES_DIR=<repo>/robots/_experimental`.

## Tham chiếu Stack-chan

[Hướng dẫn khởi động host](../../_experimental/stackchan/docs/vi/runtime_vi.md)
là tham chiếu có thể chạy. OS và HAL ở cùng máy tính; HAL client hiện tại của
OS dùng `http://127.0.0.1:5001`. Firmware ESP32 chủ động kết nối WSS có xác thực
tới `/stackchan/body/v1` trên listener riêng của HAL. Chiều kết nối này thuộc
protocol Stack-chan, không bắt buộc với mọi robot qua mạng. Firmware xác minh
certificate; hai bên dùng cùng định danh và token. Plain WS cần bật tường minh
cho thử nghiệm trong mạng cô lập.

Driver yêu cầu firmware công bố capability timed-move, measured-position,
halt/hold và torque-release. Nó áp dụng giới hạn tốc độ theo vị trí đo được,
gia hạn controller lease và xác minh tới đích thay vì coi ACK đã lập lịch là
hoàn tất vật lý. Halt giữ tư thế hiện tại; release đi tới tư thế rest, xác minh
đến nơi rồi mới yêu cầu nhả torque. Firmware xóa lease khi halt, nên host phải
acquire lại trước chuyển động mới. Xem [ghi chú triển khai an toàn](../../../docs/vi/safety_vi.md).

HTTP bridge độc lập của repo companion là entry point khác. Pull driver này
không tự chuyển bridge đó sang HAL đầy đủ hay flash firmware.

## Bằng chứng tích hợp và compatibility

Phân biệt các mốc:

- Test host: nạp profile/env, chọn driver, không tự chuyển động lúc startup,
  lệnh HTTP tới transport, đường lỗi và hành vi ownership.
- Kiểm chứng phần cứng: pin firmware đã flash, kết nối có xác thực, hiệu chuẩn
  tọa độ/rest, chuyển động đo được, halt, mất kết nối/process, hết hạn lease,
  phục hồi và torque trên cụm phần cứng thật.
- Compatibility: toàn bộ yêu cầu trong [COMPATIBILITY.md](../COMPATIBILITY.md),
  được hỗ trợ bởi [CTS](../cts/README.md). Motion cùng khuôn mặt chưa đáp ứng
  yêu cầu audio hoặc vision. Test tĩnh không tự qualification phần cứng.

Merge driver thử nghiệm hoàn thành onboarding host, chưa hoàn thành các mốc
sau. Bổ sung display qua route semantic hiện có khi một implementation chạy
được với firmware xác định ranh giới driver cần thiết; không mặc định
implementation render qua SPI phù hợp với robot tự vẽ biểu cảm.
