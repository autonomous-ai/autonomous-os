# Session agent interactive

Tab, dòng session ở sidebar và tiêu đề pane chia đôi hiển thị icon riêng kèm tên provider luôn thấy được (Codex, Claude Code hoặc Terminal) trước tên task. Chỉ tên task bị rút gọn; tooltip của tab có provider, tên đầy đủ và trạng thái lượt chạy. Với agent tương tác chưa đặt tên, hook `UserPromptSubmit` đầu tiên của agent chính cung cấp tên task (chuẩn hóa tối đa 70 ký tự); follow-up không thay tên đã có hoặc tên user tự đặt. Nếu provider không gửi nội dung prompt, tên vẫn là New session nhưng luôn hiển thị provider. Cơ chế dùng sự kiện provider, không đoán từ phím gõ terminal.

Session Codex, Claude Code và Terminal mới tạo từ UI desktop khởi chạy tiến trình interactive ngay trong project/worktree đã chọn. Buddy hiển thị CLI thật qua xterm và `node-pty`: phím nhập, output terminal, resize và focus thuộc đúng session đó. Session interactive không có màn chào chat hay ô prompt riêng của Buddy. Một thanh đầu pane duy nhất chứa tên session, trạng thái hiện tại, Stop/Resume, nút chia pane và nút đóng.

## Vòng đời và lịch sử

**Stop** dừng tiến trình do app quản lý nhưng giữ session trên sidebar. **Resume CLI** tiếp tục hội thoại provider đã lưu; **Restart terminal** chạy shell mới trong cùng worktree. Nếu session coding đã dùng nhưng chưa biết ID hội thoại provider, resume từ chối tự tạo hội thoại khác. Terminal đã dừng phát lại output đã lưu, nhưng restart không khôi phục trạng thái tiến trình shell.

Nút **×** của tab/pane và **⌘W** trên tab session dừng và lưu trữ session đích, bỏ khỏi sidebar đang hoạt động mà không xóa lịch sử đã lưu. Cờ lưu trữ được ghi bền vững, nên session đã đóng không tự xuất hiện lại trên sidebar hoặc pane khôi phục sau khi mở lại Buddy. Các session chia pane khác tiếp tục chạy. Đóng session gốc cũng đóng nhóm tab của nó; các session còn lại vẫn mở được từ sidebar. Xóa session rõ ràng là thao tác riêng có xóa lịch sử. Đóng cửa sổ workspace macOS giữ app, helper và các agent khác chạy; Quit dừng tiến trình của app.

Session cũ có lịch sử structured giữ transcript, ô prompt và ID hội thoại provider. Session coding cũ còn trống chuyển sang interactive ở trạng thái đã dừng. Session mới mặc định `mode: interactive`; backend API vẫn hỗ trợ structured. Việc chuyển đổi không bỏ hội thoại. Khôi phục layout loại bỏ session đã lưu trữ. Chỉ pane đang focus tự đánh dấu đã đọc khi cửa sổ có focus.

## Boundary provider và native

Codex interactive chạy CLI với `--dangerously-bypass-approvals-and-sandbox --no-alt-screen`; resume truyền đúng ID hội thoại đã lưu. Claude Code dùng `--dangerously-skip-permissions` cùng `--session-id` được tạo cho hội thoại mới hoặc `--resume` cho hội thoại cũ. Thiết lập full access theo yêu cầu thực thi tường minh của người dùng, không đổi cấu hình CLI toàn cục hay quyền macOS. Trạng thái lượt xử lý và việc tiến trình còn sống là hai thông tin riêng: hoàn tất một lượt chưa chắc CLI đã thoát.

Claude và Codex nhận hook riêng cho session; Buddy không sửa cấu hình provider toàn cục hay trong project. Trust cho hook Codex và project đang chọn được truyền bằng override CLI riêng của tiến trình; marker session lồng nhau được loại khỏi môi trường kế thừa trước khi chạy. `SessionStart` thành ready/idle, gửi prompt và tool thông thường thành running, yêu cầu quyền, `AskUserQuestion` và `request_user_input` thành needs-input, `Stop` thành completed. Claude còn ánh xạ `StopFailure` thành error và `PostCompact` thủ công thành completed. Hook của agent con bị bỏ qua. ID hội thoại từ hook phải khớp ID provider đã lưu; ID khác bị từ chối. `processActive` ghi nhận CLI còn sống độc lập với trạng thái lượt xử lý, nên CLI completed vẫn có thể mở và nhận lượt tiếp theo.

Ngoại trừ task đầu tiên vào CLI Codex trống mô tả dưới đây, `send` chỉ route prompt voice/follow-up vào session coding interactive sau khi hook xác lập ready hoặc completed, tiến trình còn sống và không có bản nhập tay dang dở. Working, needs-input, chưa xác minh sẵn sàng hoặc input thủ công đều chặn việc chèn prompt. Prompt được chuẩn hóa, kiểm tra ký tự điều khiển terminal rồi gửi dạng bracketed paste cùng Enter vào **đúng PTY hiện có**; không tạo tiến trình hay hội thoại mới. Paste và Enter là hai lần ghi riêng, chờ ổn định 500 ms cộng ước lượng thời gian tiếp nhận theo host. Giữ lượt gửi trong thời gian chờ; stop, exit hoặc input tay sẽ hủy Enter và báo prompt có thể đã paste nhưng chưa gửi. Buddy không tự retry trường hợp giao nhận chưa chắc chắn này. Voice `agent.create` mặc định tạo session interactive trì hoãn khởi chạy (idle, `processActive: false`); `send` đầu tiên chạy CLI thật với prompt đầu dưới dạng đối số argv vị trí, không gõ vào stdin của màn onboarding. Các lượt gửi sau dùng readiness được hook xác minh trên cùng PTY. `mode: structured` rõ ràng vẫn được hỗ trợ để tương thích.

Phím terminal đi qua preload API hữu hạn tới đúng session đang chọn. Computer use native vẫn thuộc executor Swift đóng gói cùng app và quyền macOS. Hành vi hook phụ thuộc phiên bản provider đã cài; code và mock test chưa tự chứng minh provider thật hỗ trợ đầy đủ.

Xem [UI workspace](workspace-ui_vi.md), [agent manager](agent-manager_vi.md) và [thực thi agent](agent-execution_vi.md). Đây là code tự triển khai, chưa phải toàn bộ tính năng Orca.


Codex 0.153.4 không phát hook ban đầu khi TUI còn trống. Với task voice đầu tiên, Buddy có thể dừng và mở lại CLI chưa được nhập gì, truyền prompt qua argv, giữ nguyên ID session Buddy và worktree. Chỉ áp dụng khi chưa có ID hội thoại provider, prompt trước đó, thao tác nhập tay hay lifecycle hook. Không áp dụng cho terminal đã dừng, resume, có nhập tay hoặc đang hỏi người dùng. Stop/close hủy bước chuyển đang chờ. Khi đã có hội thoại provider, follow-up giữ đúng PTY đó.

Lỗi readiness phân biệt process chưa chạy/đã dừng, text nháp chưa gửi và trạng thái chưa xác định với menu hỏi thật. Codex `request_user_input` và Claude `AskUserQuestion` đều báo needs-input kèm tóm tắt câu hỏi có giới hạn.
