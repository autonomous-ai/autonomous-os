# Chạy Stack-chan qua HAL trên máy host

Profile thử nghiệm này chạy `hal.server` thật của repository với driver chuyển
động Stack-chan trên máy tính. Profile chỉ khai báo motion và system; chưa là
thiết bị Autonomous tương thích đầy đủ, mục tiêu phát hành OTA hay tích hợp
media. Board `host` không có GPIO hoặc bus servo cục bộ. `HAL_SIMULATE=0`
chọn transport thật tới thân robot từ xa.

## Khởi động HAL

Chạy tại thư mục gốc repository, với môi trường Python HAL và dependencies
đã có ở `hal/.venv`. Cấu hình HAL trên host nằm trong profile tại
`rootfs/opt/hal/.env`, theo cấu trúc các robot khác. Sao chép thành file local
riêng; `cp -n` giữ nguyên cấu hình nếu file đích đã tồn tại:

```bash
mkdir -p "$PWD/.local/stackchan"
cp -n robots/_experimental/stackchan/rootfs/opt/hal/.env "$PWD/.local/stackchan/.env"
chmod 600 "$PWD/.local/stackchan/.env"
```

Sửa `.local/stackchan/.env`: đặt device ID firmware, token riêng dài ít nhất
32 ký tự và đường dẫn đến certificate/key TLS đã có. Không ghi credential thật
vào profile được Git theo dõi. File này cấu hình HAL trên máy tính; ESP32 cần
cấu hình firmware riêng tương ứng. Sau đó chạy HAL với file env này:

```bash
export HAL_USERS_DIR="$PWD/.local/stackchan/users"
export HAL_STRANGERS_DIR="$PWD/.local/stackchan/strangers"
export HAL_LOG_DIR="$PWD/.local/stackchan/logs"
export HAL_STATE_DIR="$PWD/.local/stackchan/state"
export OS_CONFIG_PATH="$PWD/.local/stackchan/config.json"
mkdir -p "$HAL_USERS_DIR" "$HAL_STRANGERS_DIR" "$HAL_LOG_DIR" "$HAL_STATE_DIR"
HAL_BOARD=host DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  HAL_SIMULATE=0 HAL_MODE=developer PYTHONPATH=. \
  hal/.venv/bin/python -m uvicorn hal.server:app \
    --env-file "$PWD/.local/stackchan/.env" --host 127.0.0.1 --port 5001
```

`HAL_MODE=developer` tắt hạn chế truy cập HTTP của HAL. Giữ bind HTTP tường
minh tại `127.0.0.1`; listener cho thân robot là dịch vụ TLS có xác thực riêng.
Các thư mục có quyền ghi phục vụ đường khởi động dùng chung của HAL dù profile
không bật nhận diện khuôn mặt hoặc media. `OS_CONFIG_PATH` tách lần chạy thử
khỏi config OS của thiết bị; file có thể chưa tồn tại khi chỉ thử motion độc
lập. Khi nối với OS cục bộ, đổi biến này tới config thực tế của OS đó.
Không chạy nhiều Uvicorn worker vì
mỗi worker sẽ cùng bind cổng 8765.

Firmware phải chủ động kết nối đến
`wss://<host-reachable-name>:8765/stackchan/body/v1`, xác minh certificate cho
host đó và gửi header `Authorization: Bearer <token>`. Hello phải khớp
`STACKCHAN_DEVICE_ID` và khai báo capability chuyển động theo thời lượng,
đọc vị trí đo được, halt/hold và nhả torque. HAL chờ kết nối; khởi động không
tự động di chuyển thân robot.

Chỉ khi thử plain WS trong mạng cô lập: để trống cả hai giá trị TLS trong file env local, đặt
tường minh `STACKCHAN_BODY_ALLOW_INSECURE_WS=1` trong file đó và dùng URL `ws://` tương ứng
trong firmware. Cấu hình thông thường vẫn dùng TLS.

## Kiểm tra không di chuyển

```bash
curl --fail http://127.0.0.1:5001/device
curl --fail http://127.0.0.1:5001/health
```

`/device` phải trả định danh `stackchan` và motion driver `stackchan`.
`/health` trả `servo: false` khi chưa kết nối và `servo: true` sau handshake
có xác thực. Kết quả này chỉ xác nhận kết nối khả dụng. Không kỳ vọng media.
HAL client Go của OS hiện dùng hằng số `http://127.0.0.1:5001`, nên tích hợp
trực tiếp này cần OS và HAL trên cùng host; client đó không có biến ghi đè
`HAL_BASE_URL`.

## Firmware và qualification

[Repo tích hợp Stack-chan riêng tại revision đã đối chiếu](https://github.com/glifocat/stackchan-autonomous/tree/9a5209596b97c257b6b2c1f6ff6bec91c44f8112)
chứa firmware patch và HTTP bridge độc lập. Pull repository OS này không đổi
bridge đó sang HAL, không cấu hình hay flash ESP32 và không cài service. Khi
thử đường này, cấu hình đích kết nối firmware tới body listener của HAL.

Firmware đã đối chiếu trả `completed` với state `scheduled` khi nhận chuyển
động theo thời lượng; HAL gia hạn controller lease trong lúc nội suy.
`motion.halt` xóa lease, nên HAL acquire lại trước chuyển động tiếp theo.
Source tương thích không xác định firmware thực tế đã flash lên từng thân máy.

Profile giới hạn tốc độ ở mức thử nghiệm chưa qualification là 10 độ/giây.
Driver cho phép yaw ±30, pitch ±15 độ quanh midpoint pitch legacy 45 độ;
gravity-rest nhắm yaw 0, pitch -15. Đây chưa là hiệu chuẩn vật lý. Đọc
[SAFETY.md](../../SAFETY.md) trước khi qualification chuyển động có giám sát.
Test startup và protocol trên host không xác nhận CTS đầy đủ hay an toàn vật lý.

### Commissioning home tùy chọn

Commissioning home là capability riêng, cần giám sát và mặc định bị tắt
(`STACKCHAN_HOME_COMMISSIONING_ENABLED=0`). `GET /servo/home` chỉ báo firmware
đang kết nối có quảng bá `motion.home_degrees.v1` hay không; endpoint này
không acquire lease và không chạm vào bus. `GET /servo/home/position` đọc vị
trí đo được trong frame rõ ràng `calibrated_home_deg_v1`, cũng không acquire
lease.

Khi được bật, `POST /servo/home/move` chỉ nhận target `tilt` từ 7 đến 10 độ và
duration yêu cầu từ 2 đến 10 giây. Safety policy có thể kéo dài duration đó,
tối đa 60 giây ở transport driver. Firmware phải quảng bá
`motion.home_degrees.v1`. Trước khi chuyển động, HAL yêu cầu pan đo được nằm
trong ±30 độ và tilt đo được nằm trong [0, 5). Lệnh chỉ gửi pitch và bỏ qua
yaw; torque yaw sẽ tắt trong lúc pitch chuyển động. Phải giữ hỗ trợ cơ khí và
giám sát thân robot, nhất là khi dưới 5 độ, vì stop hoặc
mất transport có thể không giữ được vị trí, khiến torque tắt hoặc session lỗi.

HAL fail-closed khi feedback không hợp lệ, reconnect, yaw lệch quá 1 độ,
timeout hoặc bị cancel. Chỉ báo thành công khi pitch đo được nằm trong sai số
1 độ so với target, ít nhất 6 độ và đã tăng dương ít nhất 1 độ. Sau đó
`lease.release` mới cấp torque lại cho cả hai trục, thiết lập trạng thái cuối
giữ cả hai trục, rồi HAL đọc lại vị trí đo được. Flow này không xác minh calibration, không thay thế mapping
midpoint legacy ±15 độ và không cho phép các preset move tiếp theo. Trong lần thử có giám sát ngày 2026-09-11, robot đã chuyển động rồi khởi
động lại do lỗi transport; không có feedback cuối hoặc xác nhận giữ vị trí.
Commissioning chưa được qualification và phải tắt ngoài các lần chẩn đoán có giám sát.

## Chạy OS trên cùng host

Giữ HAL chạy trong terminal đầu tiên. Trong terminal khác, dùng target phát
triển OS hiện có với cùng thư mục profile và thư mục state riêng:

```bash
make os-dev DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  OS_STATE_DIR="$PWD/.local/stackchan/os"
```

Trỏ `OS_CONFIG_PATH` của HAL tới `$PWD/.local/stackchan/os/config/config.json`
trước khi khởi động HAL để dùng chung cấu hình này. Target OS mặc định dùng
runtime Codex; cài/cấu hình runtime đã chọn và chạy gateway theo
[hướng dẫn phát triển simulator hiện có](../../../../../docs/vi/simulator_vi.md).
Bước Stack-chan thay cho bước HAL `make sim`: không chạy thêm HAL thứ hai hoặc
bridge riêng trên cổng 5001. Profile không cài runtime agent hay firmware ESP32.
