# Runtime Welcome Desk Intern

## Chấp thuận giọng nói — 2026-09-15

Runtime `intern` tùy chỉnh hiện chưa nhận đầu vào từ micro thiết bị hoặc đọc
phản hồi. Khai báo phần cứng `intern-v2` không bật các đường xử lý này trong
runtime. Chat quản trị, sinh văn bản qua bridge và cờ readiness không chứng
minh đường giọng nói đã sẵn sàng.

Văn bản từ micro có endpoint riêng, mặc định bị vô hiệu hóa. Phiên quản trị
đã ký hiện có là thẩm quyền: quản trị viên phải cấp quyền cho phiên và phân
loại chính xác từng bản chép lời. Không có xác thực người nói hoặc bộ phân loại
riêng tư tự động. Wake-word “Gus” không cấp quyền hay xác định phân loại.

Mọi yêu cầu giọng nói cần `Authorization: Bearer <phiên quản trị đã ký>` và
kiểm tra quản trị trực tiếp hiện có (không Origin hoặc query). Bearer khóa
provider và cookie tự đính kèm không cấp quyền giọng nói. Không thêm đăng nhập,
cấp thông tin xác thực hoặc kho xác thực mới.

1. `POST /api/agent/intern/voice/grant` với
   `{"expires_in_seconds":300,"admission":"administrator_grants_voice_session"}`.
   Thời hạn bắt buộc 1–300 giây và không vượt thời hạn phiên đã ký.
2. `POST /api/agent/intern/voice/chat` với cùng phiên và các trường `text`,
   `operation`, `data_class`, `admission` được mô tả bên dưới. Chỉ bản chép lời
   đã được phân loại rõ ràng public/business mới vào hàng đợi.
3. `DELETE /api/agent/intern/voice/grant` thu hồi quyền; bất kỳ phiên quản trị
   đã ký hợp lệ nào cũng có thể thu hồi. Đọc kết quả qua endpoint hiện có.

Chỉ có một quyền trong bộ nhớ cho mỗi vòng đời router. Cấp quyền mới hủy công
việc của quyền cũ. Chỉ lưu dấu vân tay phiên và trạng thái hủy/thời hạn, không
lưu token hay bản chép lời trong quyền. Token khác và router mới không thừa
hưởng quyền. Token phiên không định danh người nói: theo thiết kế quản trị
đơn người dùng hiện có, các phiên cùng thời hạn có token giống nhau. Kết quả
vẫn dùng quyền đọc chung của quản trị viên và thời hạn lưu hiện có.

Hết hạn hoặc thu hồi ngăn công việc đang chờ gọi bridge và hủy chờ HTTP đang
chạy. Nếu đã gửi yêu cầu, kết quả từ xa được đánh dấu chưa biết; không thể hoàn
tác suy luận đã thực hiện. Khởi động lại luôn tắt quyền. Quyền không tự phân
loại đầu vào sau đó, thêm ngữ cảnh/người nói, bật sensing/HAL/TTS hoặc đổi
provider. Chat nhập tay vẫn dùng chấp thuận từng nội dung như trước. Bên gọi
phải gửi văn bản từ micro qua endpoint giọng nói; server không thể suy ra nguồn
vật lý của văn bản do quản trị viên tin cậy gửi. Chưa nối bộ thu micro không tin cậy.

Xem [biên nhận kiểm tra quyền](../../receipts/intern-voice-grant-2026-09-15.md).
[Biên nhận blocker trước](../../receipts/intern-voice-contract-2026-09-14.md)
là bằng chứng lịch sử cho đường micro/TTS thiết bị vẫn chưa được nối.

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
