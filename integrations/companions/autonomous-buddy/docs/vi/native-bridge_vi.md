# Native bridge của Buddy hợp nhất

Autonomous Buddy cài thành một app macOS. Electron quản lý project/session,
process CLI và cửa sổ chính. Executable Swift đi kèm quản lý menu bar, pairing,
Bonjour discovery, WebSocket tới device, quyền macOS và computer executors. Đây
là hai trách nhiệm nội bộ trong cùng sản phẩm. Manager không tự triển khai
screenshot/click/type; Swift không chạy model coding hay chọn context hội thoại.

## Vòng đời process và menu

Electron chạy `Contents/Resources/native/AutonomousBuddy --embedded-helper` bằng
pipe stdin/stdout kế thừa. Không mở socket hay port local để lắng nghe. Mỗi frame
là một JSON object và ký tự xuống dòng. Parent bỏ qua dòng chẩn đoán không phải
JSON của Swift; stderr được đọc riêng. Renderer Electron dùng API preload hữu
hạn, kiểm tra đúng cửa sổ sở hữu, main frame và URL renderer local.

Menu native vẫn hiện trong embedded production mode: pairing, pause, activity
và mục mở manager. Điều khiển permission nằm trong Computer settings. Event menu `open-manager` và `quit` chuyển sang
Electron. Đóng cửa sổ manager trên macOS vẫn giữ app/helper chạy. Thoát app sẽ dọn
process manager và đóng stdin helper; EOF kết thúc Swift và ngắt device. Electron
đợi helper thoát, dùng kill dự phòng sau hai giây. Helper chết bất thường được
hiển thị trong Computer settings; Restart tạo helper mới.

State event chứa trạng thái pairing/connection, host device, pause, permissions,
discovery và tóm tắt command gần đây; không chứa token pairing hay buddy ID.
Các method native thông thường là `status`, `ping`, `pair`, `unpair`, `pause`,
`permissions`, `activity`, `command` và `shutdown`.

## Relay agent qua device đã pair

WebSocket device có xác thực hiện hữu nhận command dạng:

```json
{"id":"ws-command-id","action":"agent.send","params":{"project_id":"p1","session_id":"s1","request_id":"voice-turn-123","prompt":"Continue with tests"},"timeout_ms":10000}
```

Swift route `agent.*` trước computer dispatcher. Command agent mới bị từ chối khi
Buddy pause; Swift standalone không có manager trả lỗi unavailable rõ ràng.
Pause cũng hủy computer execution đang chạy nhưng không dừng session manager đã
nhận. Dùng `agent.stop` sau khi resume, hoặc nút điều khiển session trên desktop,
để dừng agent.

Hai chiều IPC:

```json
{"event":"agent_request","id":"relay-uuid","command":{"id":"ws-command-id","action":"agent.list","params":{}}}
{"id":"ipc-request-uuid","method":"agent_response","params":{"id":"relay-uuid","response":{"id":"ws-command-id","ok":true,"result":{}}}}
```

Swift xác nhận frame thứ hai bằng `result: {"accepted": true}` chỉ khi relay còn
pending, command ID gốc khớp và `ok` là boolean. Response không tồn tại, quá hạn,
trùng hoặc sai ID bị từ chối. Lỗi có dạng
`{ "id": "ws-command-id", "ok": false, "error": "message" }`.

Relay đợi tối đa 10 giây; timeout được truyền vào giới hạn trong 1–10000 ms.
Disconnect/reconnect hủy WebSocket task đang chờ; reply gắn với socket nguồn và
không thể gửi nhầm qua socket thay thế. Việc này hủy chờ, không đảo ngược thao tác
Electron đã nhận. Timeout hoặc mất response do disconnect là kết quả chưa chắc
chắn, không chứng minh session/task chưa được tạo. Swift không tự replay command.

## Voice API phía desktop

Project phải được đăng ký trước trong Buddy. Routing dùng ID rõ ràng, không phụ
thuộc tab UI đang chọn. Follow-up giữ nguyên project/session ID và dùng conversation
ID đã lưu của provider. Session tạo bằng voice dùng root của project đã đăng ký;
session worktree tạo từ desktop cũng có thể được gọi bằng ID.

| Action | Tham số | Kết quả |
| --- | --- | --- |
| `agent.list` | không có | Project, session, workspace và trạng thái khả dụng của provider |
| `agent.create` | `project_id`, `provider` (`codex` hoặc `claude`), `request_id`, `title`, `mode` tùy chọn (`interactive` mặc định hoặc `structured`) | Session vừa tạo |
| `agent.send` | `project_id`, `session_id`, `request_id`, `prompt` | Xác nhận nhận task kèm project/session ID; output đọc riêng |
| `agent.session` | `project_id`, `session_id`, `after_seq` tùy chọn | Session và event giới hạn, `next_seq`, `truncated`, `has_more` |
| `agent.stop` | `project_id`, `session_id` | Session hiện tại sau yêu cầu stop; process có thể thoát bất đồng bộ |

Action không hỗ trợ, project không tồn tại hoặc session khác project đều lỗi.
Prompt phải có nội dung và không quá 100000 đơn vị chuỗi JavaScript. `after_seq`
phải là số nguyên an toàn không âm. `agent.send` không cung cấp phím shell/terminal thô. Prompt coding interactive được hỗ trợ qua kiểm tra readiness bên dưới.

Voice `agent.create` mặc định interactive trì hoãn khởi chạy: session idle chưa có tiến trình. `agent.send` đầu tiên chạy CLI với prompt là đối số argv vị trí. Lượt sau paste vào cùng PTY chỉ khi hook provider xác minh ready/completed và không có input thủ công dang dở; working, needs-input, đã dừng hoặc chưa xác minh đều từ chối chèn prompt. `mode: structured` rõ ràng giữ luồng tương thích theo lượt. `agent.list` loại session đã lưu trữ; đóng session desktop ghi bền trạng thái này để session không xuất hiện lại sau restart. Xem [session interactive](interactive-sessions_vi.md).

Create/send cần `request_id` ổn định gồm 8–128 chữ cái, chữ số, gạch dưới hoặc gạch
ngang. Manager fingerprint tham số action đã chuẩn hóa và lưu receipt trong
`manager.json` trước khi gọi thao tác. Request đồng thời khớp nhau dùng chung kết
quả pending; receipt hoàn tất trả lại kết quả hoặc lỗi trước đó, kể cả sau restart.
Dùng lại ID với tham số khác sẽ lỗi. Receipt chưa hoàn tất sau crash mang trạng
thái không chắc chắn và từ chối tự chạy lại: kiểm tra session trước khi chọn ID
mới. Storage thay file atomic, không phải transaction chung giữa filesystem và
process CLI. Tối đa 10000 receipt, không tự xóa cũ; khi đầy, ID mới bị từ chối để
giữ các bản ghi chống chạy trùng trước đó.

Storage session giữ tối đa 2000 event và 2 Mi đơn vị chuỗi JavaScript của text;
mỗi event chỉ giữ tối đa 65536 đơn vị cuối. Mỗi trang `agent.session` trả tối đa
100 event và khoảng 128 KiB dữ liệu event serialize; text mỗi event trả về giới
hạn 8192 đơn vị đầu. `truncated` báo khoảng trống trước history còn lưu, không báo
text bị cắt. Tăng `after_seq` thành `next_seq` khi `has_more` còn true. Đây là
transcript có giới hạn, không phải archive log đầy đủ.

## Event hoàn tất và cần chú ý

Electron gửi method native `agent_event` với params envelope:

```json
{"type":"agent_event","project_id":"p1","session_id":"s1","seq":17,"status":"needs_input","title":"Review tests","summary":"A decision is needed"}
```

Swift chỉ chuyển tiếp tới device đang connected và xác nhận
`result: {"sent": true|false}`. `sent` nghĩa là đã đưa vào lệnh ghi WebSocket,
không phải ACK từ device hay xác nhận đã đọc thông báo thành tiếng. Manager phát
notice `completed`, `needs_input`, `error`, với summary tối đa 1200 đơn vị chuỗi.
Khi reconnect, manager phát lại session unread ở các trạng thái này. Notice có
thể lặp; bên nhận nên chống trùng bằng session ID và sequence. Output chi tiết
đọc qua `agent.session`; IPC native không phải database session thứ hai.

## Validation cách ly

Swift test kiểm tra correlation, reply ngược thứ tự, cancellation, timeout,
pause, standalone unavailable và kiểm tra URL loopback. Chỉ dành cho E2E bundle,
`BUDDY_NATIVE_TEST_MODE=1` tắt menu/discovery native, reconnect pairing thật, UI
pairing và permission prompt. Audit ghi vào `/dev/null`. Chế độ này không mô
phỏng computer executor.

Trong mode đó, `BUDDY_TEST_DEVICE_URL=ws://127.0.0.1:<port>/api/buddy/ws` kết nối
mock server bằng buddy ID `test-buddy` và Bearer token `test-token`, không đọc/lưu
credential pairing thật. Có thể bỏ path. Chỉ chấp nhận IPv4 loopback rõ ràng,
`ws`, port hợp lệ, không credential, query hay fragment. Override không có tác
dụng ngoài test mode.

## Các mục quyền macOS sau khi chuyển sang app hợp nhất

Bundle hợp nhất là `Autonomous Buddy.app`, bundle ID
`network.autonomous.ai.buddy.manager`. Helper Swift bên trong là executable thường
ở `Contents/Resources/native/AutonomousBuddy`, ký bằng identifier
`network.autonomous.ai.buddy`; đây không phải app thứ hai cần cài. macOS có thể giữ
mục Accessibility của app Swift standalone cũ sau khi bundle đó đã được chuyển
sang backup. Vì vậy, nhiều dòng quyền không chứng minh có hai app Buddy đang chạy.

Kiểm tra Accessibility và Screen Recording thực thi trong helper Swift hiện tại.
Trạng thái helper báo mới là bằng chứng về quyền hiệu lực. Quan hệ đóng gói và
signing identifier không tự chứng minh TCC kế thừa quyền parent hoặc việc xóa mục
quyền cũ sẽ không ảnh hưởng. Build ad-hoc local có designated requirement dựa trên
code hash; build lại có thể cần cấp quyền lại.
