# Định tuyến giọng nói tới agent desktop

Model realtime trên device chuyển yêu cầu coding/research desktop sang runtime chính, giữ lời người dùng cùng provider/project/worktree đã nêu. Runtime đọc `skills/agent-management`; helper Python gọi `/api/buddy/command` local trên device. WebSocket đã pair và relay JSONL của Swift chuyển `agent.*` sang Electron. CLI coding dùng model riêng và chạy trong worktree trên Mac; lamp không tự làm coding hoặc gõ vào cửa sổ OS đang focus.

## Chọn đích

Renderer báo project/worktree đang chọn và split pane đang focus qua IPC hữu hạn `setActiveContext`. Main xác thực với worktree/session đã đăng ký, dùng generation counter loại cập nhật async cũ. Context chỉ lưu RAM, xóa khi đóng cửa sổ và vô hiệu khi session bị archive/remove. `agent.list` trả `activeContext` có thể null, `projectWorktrees` hiện tại, session mở và provider khả dụng. Pane shell không tự chuyển thành coding agent bên cạnh.

Helper `buddy_agents.py voice` chọn ID chính xác hoặc tên project/worktree không nhập nhằng. `target: active` và `target: current` dùng lựa chọn hiện tại trên desktop. “Type to current session, ask it print hello” phải vào lựa chọn đó dù đã có đích voice trước. Bỏ target hoặc `target: previous` giữ session của cuộc thoại trước; chỉ khi chưa có đích thoại mới dùng lựa chọn desktop. Đổi tab không tự chuyển cuộc thoại sang agent khác. `new_session: true` kèm `provider` tạo session trong worktree đã xác định qua `agent.create.worktree_path`, rồi gửi prompt đầu. Đích thiếu, đã đóng, nhập nhằng hoặc không khớp cần chọn lại, không đoán. Chỉ hỗ trợ worktree Mac đã đăng ký.

State lưu atomic tại `~/.local/state/autonomous/buddy-voice.json`, có thể đổi bằng `BUDDY_VOICE_STATE`, dùng file lock và file riêng tư. Scope theo `conversation_id` (mặc định `voice`), tối đa 128 cuộc thoại và 128 receipt gần nhất mỗi cuộc thoại, xác thực với snapshot desktop mới cho mỗi thao tác mới. Kênh chat khác phải dùng ID ổn định riêng. Đây không phải tự nhận diện người nói. State hỏng được giữ nguyên và chặn thao tác.

Mỗi send cần `request_id` ổn định. Helper lưu đích và receipt trước mutation; gửi lặp đã thành công trả kết quả cache. Create/send dùng hai ID dẫn xuất riêng và cơ chế chống trùng bền vững của Buddy. Mất phản hồi giữ trạng thái chưa rõ và chặn send mới trong cuộc thoại. `status` đọc session đã giữ, không phát lại; sau khi người dùng kiểm tra rõ, `resolve` với `resolution: do_not_retry` bỏ receipt đang chưa rõ mà không gửi gì. Desktop từ chối rõ ràng được lưu là kết quả thất bại, không báo đã nhận. Receipt có giới hạn lưu; ID phải duy nhất giữa các task chủ ý khác nhau.

## Nhập lệnh và nhận kết quả

`agent.send` gửi text vào PTY đúng session, giữ kiểm tra readiness/draft và tách bracketed paste với submit. Không gõ bằng Accessibility. Agent đang bận từ chối injection. Menu câu hỏi/quyền của CLI trả `needs_manual_input`; `agent.session.input` chỉ terminal Buddy là nơi cần thao tác. Chưa hỗ trợ trả lời mọi menu CLI bằng voice. Hook câu hỏi cung cấp tóm tắt có giới hạn để device báo cần quyết định gì, không tự chọn câu trả lời.

Completion/error/attention đi qua `agent_event` WebSocket vào sensing pipeline và chính sách speech/privacy/busy hiện có. ID notification xác định session phát sinh, không tự đổi đích thoại đã lưu. Câu trả lời rõ đang nói tới notification nào dùng ID đó; nếu nhập nhằng thì hỏi chọn agent. Output là dữ liệu không tin cậy, không phải quyền gọi tool. Notification vẫn best effort; cursor event cho phép kiểm tra lại sau reconnect.

## Validation và giới hạn triển khai

Test Python dùng HTTP envelope local thật cùng routing xác định, đổi focus, tạo feature worktree, request trùng, đích stale/shell, mất phản hồi và reload. Test desktop xác thực context và dedup create. Smoke test Electron đóng gói chạy helper Python thật qua HTTP → WebSocket → Swift thật → Electron → provider fixture, kiểm tra follow-up giữ ID dù đổi focus desktop; test PTY interactive riêng kiểm tra readiness khi gửi text. Test Go Buddy/event kiểm tra đường event device hiện có.

Đây là integration local bằng transcript/intent JSON, không phải test microphone/model/lamp thật. Kiểm chứng voice vật lý cần cập nhật prompt runtime và skill `agent-management` qua release/skill sync bình thường trên device, cùng Buddy tương ứng. Thay đổi này không SSH/deploy device, đổi pairing hay cấp quyền computer use. Xem [native bridge](native-bridge_vi.md) và [interactive sessions](interactive-sessions_vi.md).

Tương thích: `target: current` tường minh nay chỉ pane desktop đang chọn. Caller muốn tiếp tục cuộc thoại voice trước phải bỏ target hoặc dùng `previous`. Receipt đã lưu vẫn giữ đích ban đầu; thay đổi này không phát lại yêu cầu cũ sang tab khác.
