# Chế độ thực thi agent

Session Codex và Claude do Buddy quản lý chạy full access, không hỏi quyền CLI,
theo yêu cầu tường minh cho workflow này. Áp dụng cho cả hội thoại mới và follow-up
bằng provider session ID đã lưu:

- Codex dùng `codex exec --dangerously-bypass-approvals-and-sandbox`; follow-up
  thêm `resume` và thread ID đã lưu. Giữ JSON output và prompt qua stdin.
- Claude dùng `claude --print --dangerously-skip-permissions`; follow-up thêm
  `--resume` và session ID đã lưu. Giữ stream JSON và partial message.

Session view hiển thị **Full access · no approvals**. Flag chỉ áp dụng cho process
CLI do Buddy khởi chạy; không ghi lại cấu hình CLI toàn cục. Thao tác file/tool
của agent dùng quyền filesystem của người dùng. Quyền macOS Accessibility, Screen
Recording, TCC và pause/pairing của Buddy là các cơ chế riêng, không bị các flag
này bỏ qua. Lỗi provider/tài khoản/OS vẫn có thể dừng lượt; flag không đảm bảo mọi
thao tác đều thành công.

Regression test invocation kiểm tra cả hai provider với và không có resume ID.
Đã kiểm tra CLI help trên máy có hỗ trợ flag. Validation này không khẳng định đã
chạy task model thật có phí.
