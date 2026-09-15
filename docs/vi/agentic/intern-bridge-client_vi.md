# HTTP client cho Intern bridge

`system/lib/internbridge` là Go client **chưa đăng ký runtime**, theo hợp đồng
`0.2.0`, schema `cassi-first.v1` của [project-spider-man PR #148](https://github.com/Garman-Unified-Systems/project-spider-man/pull/148),
commit `753b661087ee2ae1e4718babc2da618740cb52fa`. Từ chối envelope `0.1.0`,
kể cả đổi version nhưng giữ cấu trúc cũ. Thư viện không triển khai
`AgentGateway`, không có caller production, không cài đặt hay chuyển runtime,
không tích hợp phần cứng.

## Hợp đồng gọi

Import `go.autonomous.ai/os/system/lib/internbridge`, tạo client bằng
`internbridge.New(8765)` và gọi `Do(ctx, Request{...})`. Bridge phải có sẵn trên
cùng máy. Constructor chỉ nhận cổng TCP khác 0; địa chỉ luôn là `127.0.0.1`.
Không dùng DNS, proxy, redirect, cookie jar, authorization hay tìm credentials.

Caller phải cung cấp rõ `Operation` (`route`, `reception`, `classify`, `generate`) và
`DataClass` (`unknown`, `public`, `business`, `restricted`, `secret`). Phân loại
phải đến từ chính sách caller đáng tin cậy, không lấy từ nội dung hoặc mô hình,
không mặc định thành public/business. Với request hợp lệ, unknown trả
`ErrNeedsClassification`, restricted/secret trả `ErrCustodyHold` trước mọi
request mạng, kể cả `route`; HTTP status bằng 0 và result bằng nil. Chỉ
public/business được gửi với nhãn nguyên vẹn. Thư viện không thể xác minh caller
phân loại đúng; nhãn sai không cấp quyền vượt ranh giới dữ liệu.

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
  lạ/trùng, JSON nối đuôi, giá trị lồng nhau tùy ý, sai kiểu, thiếu field bắt buộc,
  UTF-8 sai và UTF-16 surrogate không thành cặp; emoji escape hợp lệ được giữ.
  Chỉ object `reception_route` gồm đúng sáu field scalar được phép lồng nhau.
- Yêu cầu `executes_actions:false`, `transport_status:accepted`,
  `lifecycle_status:completed`, `lifecycle_scope:bridge_request`, destination/kind
  đã biết và run ID tương ứng. Nhãn classify chỉ nhận bốn giá trị hợp đồng;
  output tối đa 4.096 Unicode code point; từ chối marker `<think` hoặc `[HW:`
  ở mọi status.
- Không tự retry. Timeout không chứng minh bridge dừng xử lý. Bridge giữ ID
  trước inference, trả 409 nếu lặp; không có API lấy lại kết quả hay hủy từ xa.

## Kết quả và lỗi

Chỉ `reception_route` (cho `Route`/`Reception`), `classified`, `draft` trả `Result`.
Reception chỉ đề xuất tuyến; draft là văn bản chưa đáng tin, không phải lệnh
hay quyền hành động. Không đồng nghĩa nhân viên đã hoàn thành công việc.

`Result.Destination` luôn là `cassi@mama`, nơi tiếp nhận đầu tiên.
`RequestedDestination` và `Kind` mô tả tuyến đề xuất: `orchestration@gus`,
`rex@dru`, `melvil@lab`, `cassi@mama` (persona); `pam@gus`, `mcavoy`,
`smart-home` (service). `news` là intent, không phải destination hay node.
Tuyến yêu cầu Cassi hoặc smart-home phải là custody hold; chỉ có envelope
tiếp nhận Cassi không có nghĩa dữ liệu đang bị giữ.

`Result.ReceptionRoute` có sáu field có kiểu: `FirstDestination`, `Handoff`,
`Intent`, `Status`, `Executed`, `NextStep`. Nơi đầu tiên phải là Cassi,
status `reception_route`, executed false, next step `safe_escalation`.
Handoff là null trên wire (chuỗi rỗng trong struct) hoặc đúng một destination
được phép trùng `RequestedDestination`, không phải Cassi. Intent unknown và
custody hold bắt buộc null handoff. Từ chối array, field lạ/trùng và object
handoff lồng nhau. Intent hợp lệ: `orchestration`, `engineering`, `service`,
`library`, `reception`, `smart-home`, `news`, `unknown`, `address_or_default`.
Đây chỉ là metadata, không phải API thiết bị hay hành động.

Service yêu cầu `service_route`, trả `ErrServiceRoute` với nil result; client
không theo handoff. Generate không thành công với reception-only. Hold phải
bỏ hoàn toàn `output` (kể cả null cũng bị từ chối), không trả result.
Completed chỉ mô tả request HTTP, không chứng minh reception/handoff/service
đã thực thi.

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
| HTTP 502 invalid-harness envelope hợp lệ | `ErrProtocol` |
| Response quá lớn | `ErrResponseTooLarge` |
| Deadline/cancel/network | `ErrDeadline`, `ErrCanceled`, `ErrTransport` |
| Input sai | `ErrInvalidRequest` |

`Health`, `Ready`, `BridgeVersion` kiểm tra `/health`, `/ready`, `/version`.
**Ready chỉ xác nhận metadata transport**, không chứng minh model sẵn sàng.
Bridge upstream không probe inference; client không tự tạo uptime.

`ProbeGeneration(ctx)` kiểm tra `Ready`, rồi gửi prompt public cố định
`Write a brief greeting.` bằng `generate`. Chỉ draft hợp lệ mới thành công;
fallback, chỉ định tuyến, response sai và lỗi transport vẫn là lỗi. Hai bước
chia sẻ deadline tối đa 20 giây hoặc deadline sớm hơn của caller. Không retry,
cache readiness, nội dung người dùng hay run ID tái sử dụng. Mỗi lần gọi tiêu
thụ inference (tối đa 256 token sinh với bridge đã ghim); caller phải giới hạn
tần suất. Đây không phải health check chạy nền tự động.

Thành công chỉ chứng minh một response generation tại thời điểm đó, không
chứng minh `AgentGateway` sẵn sàng, danh tính/locality của model, uptime hay
xác thực listener. `LocalModel` trong bridge đã ghim cưỡng chế routing local;
protocol 0.2.0 không có chứng thực provider. Harness upstream còn hỗ trợ
provider tùy chọn nên transport thành công không chứng minh inference local.

## Tích hợp chưa thực hiện

Gateway đầy đủ còn bị chặn theo [kiểm toán hợp đồng runtime](intern-runtime-contract_vi.md):
custody xuyên suốt, ảnh/session/persona/skill và activation/configuration có
chủ sở hữu. Watcher có thể triển khai bằng Go; chữ ký không trả lỗi tự nó
không phải lý do đổi interface chung. Không đăng ký installer, presync,
migration hay runtime có thể chọn trong slice này.
Tests dùng HTTP server giả cục bộ, không model, credentials hay thiết bị.
Xem [biên bản Cassi-first](../../receipts/intern-runtime-contract-2026-09-14.md)
và [biên bản 0.1.0 trước đây](../../receipts/intern-bridge-client-2026-09-14.md).
