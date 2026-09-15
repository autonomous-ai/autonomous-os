# Runtime Welcome Desk Intern

`intern` là runtime chỉ xử lý văn bản, do bên ngoài sở hữu và chỉ được kích
hoạt khi chọn rõ ràng. Đây không phải bộ não điều khiển thiết bị; nó không cài
đặt, khởi động, dừng hoặc tự chuyển sang OpenClaw, Hermes hay runtime khác. Khi
chọn hoặc rời `intern`, hệ thống chỉ cập nhật `agent_runtime`; lựa chọn đã lưu
được kích hoạt sau khi người vận hành khởi động lại `os-server`.

Trước khi khởi động lại, gateway đang chạy vẫn xử lý kênh nhắn tin, sensing,
cập nhật cấu hình và đối soát khi khởi động. Các tác động và dấu mốc runtime
này dựa trên gateway đang chạy, không dựa trên lựa chọn đã lưu. Yêu cầu đổi
runtime vẫn kiểm tra riêng lựa chọn đã lưu để yêu cầu người vận hành khởi
động lại ở cả hai chiều.

Khi hoạt động, `os-server` chỉ bind bề mặt HTTP giới hạn vào `127.0.0.1` và
không tạo handler HAL, sensing, MQTT, kênh nhắn tin, skill, firmware hoặc thiết
bị. Tiến trình sở hữu bridge Go native, không cần cài Python. Client runtime
hiện có chỉ gửi yêu cầu đã được chấp thuận đến bridge cố định tại
`http://127.0.0.1:8765/v1/intern`. Nó không đọc hoặc chuyển tiếp thông tin xác
thực, không theo redirect, không dùng proxy, không đính kèm ảnh/lịch sử và
không thực thi tuyến tiếp nhận được trả về.

Xem [cấu hình và giới hạn bridge native](intern-native-bridge_vi.md) để biết
mặc định Ollama cục bộ và cách chọn provider từ xa rõ ràng. Chỉ bridge dùng khóa
provider cho endpoint từ xa đã được cấu hình rõ ràng.

## Chấp thuận và kết quả

Quản trị viên đã xác thực có thể gửi chính xác nội dung văn bản đến
`POST /api/agent/intern/chat` với:

- `operation`: `route`, `reception`, `classify` hoặc `generate`
- `data_class`: `public` hoặc `business`
- `admission`: chuỗi cố định `administrator_classified_exact_text`

Dữ liệu chưa phân loại, hạn chế hoặc bí mật bị từ chối trước khi gọi bridge.
Các field trùng lặp, viết hoa sai, không xác định, quá lớn hoặc JSON không hợp
lệ đều bị từ chối. Phản hồi được chấp nhận chứa `run_id` cục bộ; truy vấn
`GET /api/agent/intern/result/:run_id` để lấy kết quả có giới hạn. Trạng thái
hoàn tất chỉ có nghĩa yêu cầu bridge đã hoàn tất. Kết quả là bản nháp hoặc
metadata tiếp nhận với `executes_actions: false`; hệ thống không thực hiện bàn
giao hay hành động nào.

Runtime không giả lập trạng thái sẵn sàng. Trạng thái này chỉ đúng sau một phản
hồi bản nháp hợp lệ gần đây và hết hạn sau 30 giây. Health của bridge không tự
động chứng minh model đã sẵn sàng.

Mọi đường dẫn khác trả về “runtime không hỗ trợ”. Việc chọn runtime vẫn khả
dụng tại `GET/POST /api/device/agent-runtime`, yêu cầu xác thực quản trị viên,
không thay đổi service và báo cần khởi động lại.
