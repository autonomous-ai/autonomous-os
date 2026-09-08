# Cài đặt desktop

Mở **Autonomous Buddy → Settings…** trong menu app macOS, nhấn **⌘,**, hoặc dùng nút Workspace settings ở sidebar. Settings mở thành trang đầy đủ phủ lên workspace hiện tại. Tiến trình agent, session view, terminal và bản nháp chưa gửi vẫn được giữ bên dưới. **Back to app** hoặc **Escape** trở về workspace. Focus bàn phím nằm trong Settings; workspace bên dưới không nhận tương tác khi trang đang mở.

Điều hướng trái chỉ có các mục hoạt động thật: Appearance, Agents và Computer & device. Tìm kiếm lọc nhãn và mô tả cài đặt thật trong các mục này. Các điều khiển terminal nâng cao khớp truy vấn tự hiện khi tìm kiếm; không có kết quả sẽ có thông báo rõ ràng.

## Appearance

| Cài đặt | Lựa chọn / giới hạn | Mặc định |
| --- | --- | --- |
| Theme | System, Dark, Light | System |
| UI zoom | 75%–150%, bước 5% | 100% |
| IDE font | System default, Sans serif, Monospace | System default |
| Cỡ chữ terminal | 10–24 px, bước 1 | 13 px |
| Font terminal | SF Mono, Menlo, Monaco, monospace | SF Mono |
| Giãn dòng terminal | 1.0–2.0, bước 0.1 | 1.4 |
| Cursor blink | Bật / tắt | Bật |
| Cursor style | Block, Bar, Underline | Block |
| Thanh usage agent | Hiện / ẩn | Hiện |
| Thanh trạng thái workspace | Hiện / ẩn | Hiện |

Theme và font áp dụng cho cả workspace lẫn Settings. Theme System theo thay đổi giao diện macOS. Zoom thay đổi tỷ lệ giao diện Electron. Appearance của terminal cập nhật xterm hiện có và tính lại kích thước mà không chạy lại shell hay xóa scrollback. Font cục bộ không tồn tại sẽ dùng monospace; không tải hay sao chép tài nguyên font từ Orca.

Thay đổi tự lưu qua các API main process `settings`, `updateAppearance`, `onSettings`. Ô số giữ bản nháp khi gõ và lưu khi mất focus hoặc nhấn Enter, chuẩn hóa theo giới hạn và bước hiển thị. Nút cộng/trừ lưu ngay. Điều khiển bị vô hiệu hóa trong lúc tải ban đầu hoặc khi đọc lỗi, tránh thay tùy chọn đã lưu bằng mặc định do tải thất bại. Cập nhật lỗi có thông báo hiển thị.

## Agent và điều khiển máy

Agents hiển thị trạng thái CLI thực tế của Codex, Claude Code và Terminal. Coding session hiện dùng quyền truy cập cục bộ đầy đủ, không có yêu cầu duyệt từng lệnh; panel giải thích hành vi này thay vì đưa ra nút duyệt quyền chưa hoạt động. Có thể gỡ đăng ký project đang chọn sau khi xác nhận. Session đang chạy sẽ ngăn thao tác; gỡ đăng ký sẽ xóa project khỏi manager cùng session và lịch sử đã lưu, nhưng giữ file project trên đĩa.

Computer & device mở panel điều khiển máy hiện có trong cùng app để pairing, quản lý quyền Accessibility/Screen Recording, pause/resume và activity. Các quyền này độc lập với tài khoản CLI agent.

## Audit source và phạm vi

Đã đối chiếu bố cục và workflow với `AppearancePane.tsx`, `AppearanceInterfaceSection.tsx`, `TerminalAppearanceSection.tsx` của Orca trong checkout upstream cục bộ. Buddy dùng code UI tự triển khai. Trang này chưa bao gồm toàn bộ catalog Settings của Orca: chưa cung cấp import theme, gói ngôn ngữ, dò mọi font, cài đặt remote/mobile hay các mục sidebar bổ sung.

Validation gồm `npm run lint`, `npm run build` trong desktop, test settings store và smoke workflow Electron đóng gói. Xem [workspace-ui_vi.md](workspace-ui_vi.md) về workspace và tab.

Menu View có Zoom In (⌘=), Zoom Out (⌘−) và Reset Zoom (⌘0), dùng chung giá trị zoom đã lưu. Nếu file settings không hợp lệ, app giữ nguyên file và hiện lỗi khởi động kèm đường dẫn cùng hướng dẫn khôi phục.
