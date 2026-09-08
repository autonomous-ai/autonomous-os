# Tích hợp Harness

`system/harness` nối trực tiếp một thiết bị Autonomous với một máy tính Harness đã ghép đôi rõ ràng. Harness Desktop/CLI tìm thiết bị qua dịch vụ mDNS `_autonomous._tcp` đã có, cũng được Autonomous Buddy sử dụng. Harness giữ khóa riêng và dùng giao thức pairing/phiên gốc của `E2eeManager`. Code và khóa Buddy độc lập.

## Ghép đôi

1. Trong OS Monitor của thiết bị, bấm **Generate pairing code**. Thiết bị tạo mã ngẫu nhiên mật mã sáu ký tự, có hiệu lực 60 giây.
2. Trên cùng mạng nội bộ, mở Harness Desktop → Settings → Devices, chọn thiết bị Autonomous được tìm thấy rồi nhập mã.
3. Với CLI, chạy `harness autonomous-device discover --json`, sau đó `harness autonomous-device pair --device <discoveryId> --code-stdin`, đưa mã vào stdin.
4. Harness mở WebSocket tới `/api/harness/ws` trên thiết bị được tìm thấy. Sau khi ghép, thiết bị có thể liệt kê và tương tác với agent trên máy tính qua `harness-use`.

Discovery lấy host và port đã quảng bá; đây không phải bằng chứng xác thực thiết bị. Thiết bị không cần ô nhập IP hoặc credential backend. Yêu cầu đăng nhập/khởi động Harness trên Mac vốn có được giữ nguyên; kết nối thiết bị không đi qua backend và hoạt động độc lập với kết nối backend đang mở. Nếu không thấy thiết bị, kiểm tra cùng mạng và mDNS không bị chặn.

Socket trao đổi `machine_select` / `machine_selected` để biết danh tính máy CLI, rồi OS gửi `e2e_pair_intent` gốc với `{pairId,label,role:"device"}`. Mã không được gửi qua socket hay lưu vào trust store. Các vòng CPace gốc dùng `autonomous-e2e-pair|agent:<machineId>|a:adapter|b:device`. `e2e_hello` có chữ ký và `e2e_welcome` mã hóa gốc thiết lập phiên. Đây là tái sử dụng giao thức mật mã Harness qua kết nối trực tiếp, không dùng transport cloud cũ.

Sai mã làm lần ghép thất bại và Desktop hiển thị lỗi. Tạo mã mới trên thiết bị trước khi thử lại. Hoàn tất, thất bại, hủy và hết hạn đều xóa mã. Không ghi đè máy tính đã ghép; cần unpair trước để chọn máy khác. CLI/Desktop có thể giữ các thiết bị đã ghép riêng biệt, mỗi thiết bị có danh tính riêng.

## API và vòng đời OS

- `NewService(configDir, callbacks)` đọc danh tính; `Start(ctx)` quản lý vòng đời.
- `POST /api/harness/pair` có xác thực admin gọi `StartPair(ctx)` không cần machine ID. `GET /api/harness/pair/status` trả mã tạm, `expires_at`, `pairing`, `state` và lỗi tùy chọn với `Cache-Control: no-store`.
- `POST /api/harness/pair/cancel` có xác thực admin vô hiệu hóa lần ghép đang chờ. `DELETE /api/harness` xóa pin và đóng socket.
- `GET /api/harness/status` dành cho admin hoặc caller loopback trực tiếp; không trả mã.
- `GET /api/harness/ws` nhận socket CLI trực tiếp. PAKE và E2EE với khóa đã ghim xác thực route này thay cho HTTP bearer. Từ chối header Origin của trình duyệt. Tối đa bốn socket đầu vào, giới hạn mười giây cho metadata đầu tiên và hai mươi giây cho handshake phiên/ứng dụng.
- `POST /api/harness/request` chỉ cho loopback thực sự, có kiểm tra địa chỉ proxy, để runtime của skill gọi.

Nếu pairing bị ngắt sau khi lưu pin tạm đã xác thực, OS Monitor hiển thị máy tính được giữ lại và nút Unpair thay vì tạo mã xung đột. Nếu không thể lưu việc xóa trust, API trả lỗi và giữ pin cũ trong RAM để trạng thái khớp với đĩa; thử Unpair lại sau khi xử lý lỗi lưu trữ.

CLI chịu trách nhiệm tìm và kết nối lại. OS chờ socket được xác thực; pin đã lưu tồn tại qua khởi động lại. Chỉ thay kết nối hiện có sau khi kết nối mới xác thực và thương lượng ứng dụng thành công. Socket đang hoạt động gửi WebSocket ping mỗi 15 giây và cần pong trong hạn đọc 60 giây.

Danh tính OS và pin của một máy tính lưu tại `configDir/harness/trust.json` (thư mục 0700, file 0600). Ghi nguyên tử và từ chối symlink. Trust file sai định dạng làm khởi tạo thất bại, không âm thầm tạo danh tính mới. Pin đã xác thực ở vòng ba là tạm trong năm phút để phục hồi khi mất PAKE cuối qua `e2e_hello` gốc; thiết lập phiên thành công xác nhận pin. Marker trực tiếp là `harness-device-direct-v1`; pin relay dùng E2EE gốc tương thích được chấp nhận, pin mật mã thử nghiệm trước đó thì không.

## Thao tác agent mã hóa

Sau xác thực, `autonomous_device_request` mã hóa mang `hello` ứng dụng để thương lượng capability và tiếp tục sự kiện. Phản hồi dùng `autonomous_device_result`; sự kiện dùng `autonomous_device_event`. Giữ khóa pairwise/group, miền chữ ký, dẫn xuất khóa, rekey có xác thực và chống replay gốc. Từ chối kết quả ứng dụng plaintext.

Hỗ trợ `agents.list`, `turn.send`, `turn.stop`, `status`, `recap`, `question.answer` và `receipt.get`. Thao tác nhắm agent cần machine ID và agent ID rõ ràng. Duyệt quyền công cụ, nhập terminal thô, shell/file tùy ý và tạo/xóa agent nằm ngoài tích hợp.

Mutation cần idempotency key ổn định. OS gửi một lần và chờ tối đa 30 giây. Timeout/mất kết nối sau gửi trả `DeliveryUnknownError`: tra receipt với cùng key, không tự gửi lại. Tối đa 64 request đang chờ và 128 sự kiện callback trong hàng đợi. Resume dùng `serverInstanceId` và `eventId` dạng số; resync cần đọc lại trạng thái agent. Skill giữ agent được chọn theo cuộc hội thoại và mutation chưa rõ kết quả giữa các lần gọi.

## Nginx và kiểm chứng

Quảng bá mDNS hiện có trỏ cổng 80. Mẫu nginx trong `scripts/provision/setup.sh`, `scripts/imager/build.sh` và `scripts/imager/build-orangepi.sh` có location chính xác `/api/harness/ws`, chuyển tiếp HTTP/1.1 Upgrade với timeout dài. Thiết bị đã cài cần được cập nhật cấu hình nginx này khi triển khai tính năng; chỉ upload binary Go không cập nhật nginx. Kiểm chứng trong repo không deploy hoặc restart thiết bị.

`system/harness/testdata/original-e2ee-protocol.json` được sinh từ E2EE core gốc của Harness. Test bao phủ CPace, chữ ký/khóa phiên gốc, bản ghi mã hóa, rekey, chống replay và vòng đời pairing. `system/server/harness_test.go` kiểm tra tạo mã không cần chọn máy, đọc mã cần xác thực chủ thiết bị, và lệnh agent từ xa/qua proxy bị từ chối. Kiểm tra tương thích liên repo cục bộ dùng manager CLI thật và service Go; không thay thế kiểm tra giọng nói và mạng LAN trên thiết bị vật lý.

Chạy kiểm tra liên repo tùy chọn từ repo OS sau khi cài dependency của checkout CLI:

```sh
go run ./system/harness/testdata/direct_interop.go /absolute/path/to/autonomous-harness/cli
```

Bài kiểm tra quảng bá mDNS tạm trên máy, dùng server WebSocket OS cục bộ và adapter CLI thật nhưng không kết nối backend; kiểm tra sai mã/thử lại, thao tác mã hóa, chống trùng, khởi động/kết nối lại và thu hồi quyền. Cần mạng multicast cục bộ; không kết nối thiết bị vật lý.
