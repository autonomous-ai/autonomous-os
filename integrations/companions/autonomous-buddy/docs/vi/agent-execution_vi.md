# Chế độ thực thi agent

Session Codex và Claude do Buddy quản lý chạy full access, không hỏi quyền CLI,
theo yêu cầu tường minh cho workflow này. Áp dụng cho cả hội thoại mới và follow-up
bằng provider session ID đã lưu:

- Codex interactive dùng `codex --dangerously-bypass-approvals-and-sandbox --no-alt-screen`; resume truyền đúng thread ID đã lưu.
- Claude interactive dùng `claude --dangerously-skip-permissions` với `--session-id` cho hội thoại mới hoặc `--resume` cho hội thoại cũ.
- Session structured được giữ lại hoặc chọn rõ ràng dùng `codex exec` JSON/stdin hoặc Claude `--print` stream JSON với cùng cờ full access. Chỉ view structured cũ hiển thị **Full access · no approvals**.

Session interactive hiển thị UI terminal thật của provider; xem [session interactive](interactive-sessions_vi.md). Claude vẫn có thể hiện màn trust lần đầu cho thư mục mới; Buddy không bỏ qua màn hình do CLI quản lý này. Đây là bước riêng với quyền tool đã tắt bằng cờ thực thi. Flag chỉ áp dụng cho process CLI do Buddy khởi chạy; không ghi lại cấu hình CLI toàn cục. Thao tác file/tool
của agent dùng quyền filesystem của người dùng. Quyền macOS Accessibility, Screen
Recording, TCC và pause/pairing của Buddy là các cơ chế riêng, không bị các flag
này bỏ qua. Lỗi provider/tài khoản/OS vẫn có thể dừng lượt; flag không đảm bảo mọi
thao tác đều thành công.

Regression test invocation kiểm tra cả hai provider với và không có resume ID.
Đã kiểm tra CLI help trên máy có hỗ trợ flag. Mock test xác minh contract invocation; kiểm chứng provider thật được ghi riêng bên dưới.

## Kiểm tra với tài khoản thật

Từ `desktop/`, chạy tường minh `BUDDY_LIVE_PROVIDER_TEST=1 node tests/provider-live.mjs` để kiểm tra Codex và Claude đã cài với tài khoản thật. Check opt-in tạo project rỗng tạm và hai lượt không dùng tool cho mỗi provider, kiểm tra completion và nhớ context bằng đúng provider session ID đã lưu, rồi xóa state Buddy tạm. Conversation của CLI có thể vẫn được giữ. Check này gọi model thật, không thuộc unit test hoặc Electron test dùng provider giả.

Ngày 2026-09-08, cả hai provider đã qua completion lượt đầu và follow-up đúng session trên Mac này với cờ không hỏi duyệt ở trên.

Đã kiểm chứng session interactive Codex và Claude mới cùng resume đúng ID bằng hook provider thật trên Mac này. Readiness/trạng thái hook và việc tiến trình còn sống được theo dõi riêng; hành vi CLI phụ thuộc phiên bản.
