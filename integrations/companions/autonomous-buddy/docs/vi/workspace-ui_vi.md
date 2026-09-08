# UI quản lý workspace agent

Autonomous Buddy gộp workspace agent và điều khiển máy native trong một app. Sidebar project nhóm các agent session và terminal theo từng worktree, hiển thị số lượng, trạng thái đang chạy/cần chú ý, dấu chưa đọc và danh sách session có thể thu gọn. Tìm kiếm khớp tên project, tên branch và tên session. Panel Computer & device vẫn nằm trong cùng sidebar.

## Thao tác workspace

Nhấp chuột phải lên thẻ worktree hoặc dùng nút **…** có nhãn truy cập. Menu hỗ trợ phím mũi tên lên/xuống, Home/End và Escape.

- **Update workspace name** đổi tên hiển thị, giữ nguyên Git branch và đường dẫn.
- **New session** mở lựa chọn provider trong đúng worktree. **New worktree** tạo Git worktree thật qua hộp thoại branch hiện có.
- **Move to status** gán Active, In review hoặc Done. Đây là trạng thái tổ chức; đổi trạng thái không có nghĩa agent đã hoàn thành công việc.
- **Open in** mở workspace đã xác thực trong Finder, Terminal hoặc Visual Studio Code qua các handler main process hữu hạn. App chưa cài sẽ báo lỗi.
- **Copy path**, **Pin/Unpin**, **Mark read/unread** áp dụng cho worktree được chọn. Mark read cũng xóa dấu chưa đọc của các session. Các worktree ghim đứng trước những worktree cùng cấp khác.
- **New group from project** tạo nhóm sidebar có tên và đưa cả project vào nhóm. **Move project to group** chọn nhóm hiện có hoặc No group. Các thao tác này không di chuyển file.
- **Set parent worktree** tổ chức các worktree trong cùng project. Worktree con nằm dưới cha, được thụt lề và có nhãn cha. Chọn No parent để bỏ liên kết. Manager từ chối chu trình. Không rebase hay sửa branch.
- **Sleep workspace** yêu cầu xác nhận trước khi dừng các agent và terminal đang chạy. Giữ session và lịch sử. Không tự động tiếp tục tiến trình đã dừng.
- **Delete worktree** yêu cầu xác nhận trước khi xóa thư mục. Không xóa worktree primary, bị khóa, có thay đổi chưa lưu vào Git hoặc đang chạy session; lệnh Git không dùng force. Giữ branch và lịch sử session.

Mỗi dòng session có menu **…** riêng để đổi tên, dừng hoặc xóa rõ ràng. Xóa session đã dừng cần xác nhận và xóa lịch sử hội thoại, nhưng giữ file project.

Manager lưu metadata workspace gồm `projectId`, `worktreePath`, `pinned`, `status`, `unread`, cùng `displayName` và `parentWorktreePath` tùy chọn. Nhóm project dùng trường `group` tùy chọn của project. Renderer gọi các IPC được cho phép: `updateWorkspace`, `setProjectGroup`, `sleepWorkspace`, `removeWorktree`, `openWorkspace`, `copyWorkspacePath`, `removeSession`; main process kiểm tra filesystem và tiến trình.

## Tab

Mỗi tab session có nút **×** hiển thị. Đóng tab chỉ ẩn tab đó; không dừng agent, không kết thúc terminal và không xóa lịch sử. Đóng tab đang chọn sẽ chuyển sang tab mở liền kề hoặc để worktree trống. Chọn lại session ở sidebar để mở tab. Nhấp đúp tab để đổi tên session.

**⌘W** đóng preview file đang mở trước, nếu không có thì đóng tab session đang chọn. Phím này không đóng session phía sau khi đang mở hộp thoại hoặc menu. Danh sách tab đã đóng và lựa chọn hiện tại được lưu trong tùy chọn UI cục bộ qua lần mở app sau. Việc giữ tiến trình qua lần khởi động app là vấn đề riêng: thoát toàn bộ app vẫn dừng các tiến trình agent do app sở hữu.

## Audit Orca và khác biệt còn lại

Đây là code tự triển khai dựa trên tham khảo [WorktreeContextMenuView](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/sidebar/WorktreeContextMenuView.tsx), [context-menu commands](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/sidebar/use-worktree-context-menu-commands.ts) và [TabBar](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/tab-bar/TabBar.tsx) upstream. Tại revision đã audit, Update của Orca gọi đổi tên; New group from project tạo nhóm rồi gán project vào nhóm. Hai thao tác này không phải pull Git hay tạo worktree.

Chưa đạt toàn bộ tính năng Orca. Buddy hiện dùng bộ trạng thái tổ chức cố định và một menu có cuộn thay vì các menu con mở ngang. Chưa triển khai chọn nhiều mục, kéo thả sắp xếp, chia pane, tab browser/editor, quản trị nhóm độc lập với project, remote/mobile hay tự tiếp tục tiến trình sau Sleep.

Validation: `npm run lint` và `npm run build` trong desktop; smoke test Electron đóng gói được duy trì tại `desktop/tests/electron-smoke.mjs`. Xem [agent-manager_vi.md](agent-manager_vi.md) về boundary native và vòng đời provider/session.

## Thanh usage của provider

Thanh usage phía dưới hiển thị các cửa sổ quota Claude và Codex do `providerUsage(refresh?)` trả về: nhãn cửa sổ, phần trăm đã dùng, thanh mức sử dụng và thời gian đến lần reset nếu có dữ liệu. Di chuột hoặc focus provider bằng bàn phím để xem timestamp reset chính xác và thông tin khả dụng. Khi thiếu dữ liệu hoặc lấy quota lỗi, UI hiển thị Sign in hoặc Not available; không thay quota chưa biết bằng 0%. UI gọi lúc mở và mỗi 60 giây, không chồng request, đồng thời có nút Refresh agent usage để làm mới thủ công. Việc xác thực provider và lấy quota nằm ở main process; API renderer không nhận giá trị credential.
