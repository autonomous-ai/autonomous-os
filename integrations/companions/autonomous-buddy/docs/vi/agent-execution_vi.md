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

## Kiểm tra với tài khoản thật

Từ `desktop/`, chạy tường minh `BUDDY_LIVE_PROVIDER_TEST=1 node tests/provider-live.mjs` để kiểm tra Codex và Claude đã cài với tài khoản thật. Check opt-in tạo project rỗng tạm và hai lượt không dùng tool cho mỗi provider, kiểm tra completion và nhớ context bằng đúng provider session ID đã lưu, rồi xóa state Buddy tạm. Conversation của CLI có thể vẫn được giữ. Check này gọi model thật, không thuộc unit test hoặc Electron test dùng provider giả.

Ngày 2026-09-08, cả hai provider đã qua completion lượt đầu và follow-up đúng session trên Mac này với cờ không hỏi duyệt ở trên.
