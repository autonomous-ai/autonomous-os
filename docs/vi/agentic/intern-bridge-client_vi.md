# HTTP client cho Intern bridge

`system/lib/internbridge` là Go client **chưa đăng ký runtime**, theo hợp đồng
`0.1.0` của [project-spider-man PR #145](https://github.com/Garman-Unified-Systems/project-spider-man/pull/145),
commit `4f3e3c3606420db7bd4e72c8de3d68d5486f56c5`. Thư viện không triển khai
`AgentGateway`, không có caller production, không cài đặt hay chuyển runtime,
không tích hợp phần cứng.

## Hợp đồng gọi

Import `go.autonomous.ai/os/system/lib/internbridge`, tạo client bằng
`internbridge.New(8765)` và gọi `Do(ctx, Request{...})`. Bridge phải có sẵn trên
cùng máy. Constructor chỉ nhận cổng TCP khác 0; địa chỉ luôn là `127.0.0.1`.
Không dùng DNS, proxy, redirect, cookie jar, authorization hay tìm credentials.

Caller phải cung cấp rõ `Operation` (`route`, `classify`, `generate`) và
`DataClass` (`unknown`, `public`, `business`, `restricted`, `secret`). Phân loại
phải đến từ chính sách caller đáng tin cậy, không lấy từ nội dung hoặc mô hình,
không mặc định thành public/business. Thư viện gửi nguyên data class; không thể
xác minh caller phân loại đúng. Nội dung restricted/secret vẫn được gửi tới
bridge loopback để bridge quyết định custody; caller phải kiểm tra ranh giới
dữ liệu trước khi gọi.

Text phải là UTF-8 hợp lệ, không trống, tối đa 8.000 Unicode code point; JSON
sau mã hóa tối đa 16 KiB. Không cắt nội dung. Run ID tùy chọn phải khớp
`[A-Za-z0-9._-]{1,64}`, không chứa dữ liệu nhạy cảm. ID được cung cấp phải trả về
`run-` cộng 24 ký tự hex đầu của SHA-256; ID do bridge tạo có 32 ký tự hex.

## Giới hạn và kiểm tra

- Mỗi lần gọi tối đa 20 giây, gồm chờ kết nối, header và body. Deadline sớm hơn
  hoặc cancellation của caller được ưu tiên.
- Response tối đa 64 KiB, header tối đa 8 KiB, tối đa bốn kết nối tới host.
  Tắt compression và proxy discovery.
- Chỉ HTTP 200 với version/envelope/status đúng mới thành công. Từ chối field
  lạ/trùng, JSON nối đuôi, giá trị lồng nhau, sai kiểu, thiếu field bắt buộc,
  UTF-8 sai và UTF-16 surrogate không thành cặp; emoji escape hợp lệ được giữ.
- Yêu cầu `executes_actions:false`, `transport_status:accepted`,
  `lifecycle_status:completed`, destination/kind đã biết và run ID tương ứng.
  Nhãn classify chỉ nhận bốn giá trị hợp đồng; draft chứa marker reasoning hoặc
  hardware bị từ chối.
- Không tự retry. Timeout không chứng minh bridge dừng xử lý. Bridge giữ ID
  trước inference, trả 409 nếu lặp; không có API lấy lại kết quả hay hủy từ xa.

## Kết quả và lỗi

Chỉ `routed`, `classified`, `draft` trả `Result`. `routed` chỉ xác định nơi nhận,
không dispatch; draft là văn bản chưa đáng tin, không phải lệnh hay quyền hành động.

Mọi lỗi trả nil result và `*internbridge.Error`. Dùng `errors.Is` với sentinel;
`errors.As` đọc HTTP status khi có. Nội dung lỗi và unwrap chỉ có sentinel cố
định, không chứa text request, output/error server, URL hay lỗi network/parser
gốc. Thư viện không log nội dung.

| Kết quả | Sentinel |
|---|---|
| `custody_hold`, phải không có output | `ErrCustodyHold` |
| `needs_classification` | `ErrNeedsClassification` |
| `needs_input` | `ErrNeedsInput` |
| `service_route` | `ErrServiceRoute` |
| `fallback`, HTTP 500 hợp lệ | `ErrUnavailable` |
| HTTP 400/404/409/413 hợp lệ | `ErrRejected` |
| JSON/envelope/version/status sai, redirect | `ErrProtocol` |
| Response quá lớn | `ErrResponseTooLarge` |
| Deadline/cancel/network | `ErrDeadline`, `ErrCanceled`, `ErrTransport` |
| Input sai | `ErrInvalidRequest` |

`Health`, `Ready`, `BridgeVersion` kiểm tra `/health`, `/ready`, `/version`.
**Ready chỉ xác nhận metadata transport**, không chứng minh model sẵn sàng.
Bridge upstream không probe inference; client không tự tạo uptime.

## Tích hợp chưa thực hiện

Gateway đầy đủ còn cần metadata custody đáng tin trong OS chat, hợp đồng trả
lỗi cho watcher không hỗ trợ và hợp đồng activation/readiness rõ ràng.
Không thêm installer, presync, migration hay phương thức giả thành công.
Tests dùng HTTP server giả cục bộ, không model, credentials hay thiết bị.
Xem [biên bản kiểm chứng](../../receipts/intern-bridge-client-2026-09-14.md).
