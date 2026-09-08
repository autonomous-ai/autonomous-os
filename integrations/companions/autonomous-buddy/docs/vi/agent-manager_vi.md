# Autonomous Buddy — Desktop agent manager

App tùy chọn `desktop/` quản lý project, worktree và agent session trên máy. Electron main process sở hữu agent process, truy cập Git/file và persistence; React hiển thị workspace. App Swift trong `macos/` tiếp tục giữ pairing, device WebSocket và các executor computer-use native. Đợt này chưa nối hai app với nhau.

Bố cục tham khảo workspace cơ bản của Orca. Implementation được viết mới tại repo này, không sao chép source hay asset Orca. Nếu tái sử dụng trực tiếp về sau phải audit license source/dependency riêng.

## Workspace

- Trái: project, Git worktree và session tương ứng; tìm kiếm, dấu chưa đọc và màn hình Needs attention. Open project dùng hộp chọn thư mục native. Tạo worktree sẽ tạo branch mới và thư mục ngang cấp có tên ghép từ project, branch và hậu tố ngẫu nhiên; không di chuyển công việc hiện tại.
- Giữa: tab session, đổi tên, trạng thái, transcript streaming, ô prompt/follow-up và Stop. Terminal dùng xterm với shell `node-pty` thật, thay đổi kích thước theo panel. Preview file và diff cũng mở tại đây.
- Phải: working changes, nhấn file để xem unified diff, cây file mở rộng và tối đa 30 commit gần nhất. Git status cập nhật mỗi 5 giây khi cửa sổ hiển thị, khi focus hoặc refresh thủ công. Danh sách commit chưa phải đồ thị branch/merge đầy đủ. Preview chỉ đọc, giới hạn file không phải binary tới 1 MiB. Bộ lọc file ở thư mục gốc không tìm toàn project.
- Kéo để đổi độ rộng panel hoặc ẩn sidebar. Cmd/Ctrl+K focus tìm kiếm, Cmd/Ctrl+N mở tạo session, Enter gửi và Shift+Enter xuống dòng.

Thư mục thường cũng có thể làm project mà không cần Git. Tạo worktree cần repository. Xóa project khỏi Buddy chỉ xóa đăng ký cùng session/transcript đã lưu, giữ file và worktree trên đĩa; không cho xóa khi session còn chạy hoặc đang khởi động.

## Model và protocol local

Contract nguồn: [`desktop/src/shared/types.ts`](../../desktop/src/shared/types.ts).

| Record | Trường / vai trò |
| --- | --- |
| `Project` | `id`, `name`, `path` local đã canonicalize |
| `Worktree` | `path`, `branch`, `head`, `primary`, `locked` tùy chọn; đọc từ Git |
| `Session` | `id`, `projectId`, `worktreePath`, `title`, `provider`, `providerSessionId` tùy chọn, `status`, `createdAt`, `updatedAt`, `unread` |
| `SessionEvent` | `id`, `sessionId`, `seq` tăng dần trong session, `at`, `type`, `text` |

Provider gồm `codex`, `claude`, `terminal`. Event gồm `prompt`, `output`, `status`, `error`, `result`, `terminal`. Trạng thái gồm `idle`, `running`, `needs_input`, `completed`, `error`, `stopped`. Terminal đang chạy hiển thị **Shell active**, không suy diễn thành coding agent.

Renderer sandbox dùng API preload hữu hạn `window.buddy`. Request/reply qua `ipcRenderer.invoke('buddy:<method>')`; `buddy:update` đẩy snapshot project/session/provider hoặc một session event. `session(id)` lấy lịch sử còn giữ, ghép với live event theo sequence. Đợt này chưa có HTTP/WebSocket listener, API cursor remote, turn ID hay chống gửi trùng theo request ID. Một session không nhận hai lượt gửi đồng thời; các session khác nhau có thể chạy song song.

IPC kiểm tra cửa sổ/main frame gửi và URL renderer local. Renderer không có Node integration. Workspace session phải thuộc các worktree của project đã đăng ký; duyệt file chặn path traversal và symlink ra ngoài workspace. Boundary này giới hạn API UI, không giới hạn năng lực lệnh người dùng chạy trong shell hoặc agent CLI.

## Vòng đời agent

Codex dùng `codex exec` với JSON event, `sandbox_mode="workspace-write"` và `--skip-git-repo-check` để hỗ trợ thư mục project không có Git do người dùng chọn; follow-up dùng `exec resume` cùng thread ID đã lưu. Claude Code dùng print mode, stream JSON và partial message; follow-up truyền `--resume` cùng session ID đã lưu. Prompt đi qua stdin, không qua command shell. CLI dùng cấu hình và tài khoản đã cài riêng trên máy.

Mỗi lượt gửi tạo một CLI process mới, giữ provider conversation ID qua các lượt. Output tới bất đồng bộ sau khi request gửi khởi động process. Hoàn tất cần completion event được nhận diện và exit thành công. Plain text không nhận diện và stderr diagnostic vẫn hiển thị nhưng không chứng minh hoàn tất. Nếu provider trả ID khác thì từ chối đổi conversation; nếu lượt trước chưa từng trả ID thì từ chối follow-up thay vì âm thầm mở conversation mới.

`needs_input` hiện biểu thị Claude trả permission denial trong result, chưa phát hiện mọi câu hỏi và chưa có bridge phê duyệt quyền trực tiếp. Người dùng đọc result rồi gửi follow-up hoặc dùng terminal riêng cho CLI tương tác; terminal đó không tự nối vào conversation đang quản lý. Codex chạy noninteractive cũng chưa có hộp phê duyệt do Buddy cung cấp.

Chuyển sang completed, needs-input hoặc error đánh dấu session chưa đọc; mở session sẽ đánh dấu đã đọc. App gửi desktop notification khi chuyển sang các trạng thái này lúc cửa sổ không focus, tùy hỗ trợ/quyền của OS. Chưa gửi thông báo về lamp.

Stop gửi signal tới process group trên POSIX và tăng mức dừng nếu process còn sống. Đóng cửa sổ cuối cùng sẽ thoát app và dừng mọi session đang quản lý. Chưa hỗ trợ tiếp tục chạy nền sau khi đóng app.

## Lưu trạng thái và mở lại

`app.getPath('userData')/manager.json` của Electron lưu schema version 1, đăng ký project, session và lịch sử event có giới hạn. Trên macOS đường dẫn thông thường là `~/Library/Application Support/Autonomous Buddy/manager.json`; biến `BUDDY_DATA_DIR` ghi đè thư mục. Ghi file bằng thay thế atomic. Output lưu debounce 150 ms nên crash đột ngột có thể mất event mới nhất chưa ghi.

Mỗi session giữ tối đa 2.000 event và 2 MiB text (đo bằng độ dài JavaScript string); mỗi event giới hạn 65.536 ký tự. Sequence tiếp tục tăng khi bỏ event cũ. Đây là lịch sử gần đây, không phải bản lưu transcript đầy đủ lâu dài.

Khi mở lại, session đã lưu ở `running`/`needs_input` chuyển thành `stopped`. Không tự khởi động lại process. Follow-up agent có thể resume khi đã lưu provider ID và provider vẫn còn conversation đó. Lịch sử terminal được replay để xem; muốn shell mới cần tạo terminal session mới. State hỏng hoặc version không hỗ trợ sẽ báo lỗi thay vì âm thầm bỏ dữ liệu.

## Phát triển và giới hạn

Xem prerequisite và lệnh trong [`desktop/README.md`](../../desktop/README.md): `npm install`, `npm run lint`, `npm test`, `npm run build`, `npm run test:e2e`, rồi `npm start`. Cần Node.js 22.12+, npm và native build tools để rebuild `node-pty` cho Electron; trên macOS dùng Xcode Command Line Tools. `npm start` dùng bản đã build; `npm run dev` build rồi mở app, không có hot reload. Smoke test Electron cần desktop session và bản build sẵn.

Cài và đăng nhập agent CLI riêng. Buddy tìm `codex`/`claude` trong `PATH` được kế thừa; nên mở từ terminal có PATH đó. Có executable không đồng nghĩa đã xác thực tài khoản. Terminal dùng `$SHELL`, dự phòng `/bin/sh`.

Backend test dùng repository tạm và launcher mô phỏng. Smoke test Electron dùng state tạm và response CLI agent mô phỏng, không cần gọi model trả phí. Build/test pass không thay thế kiểm chứng phiên bản CLI đã đăng nhập với provider thật.

Đợt này chưa có adapter OpenCode/provider tùy chỉnh, Swift IPC, voice routing từ lamp, remote companion, đồ thị Git commit đầy đủ, UI stage/commit/push, renderer research artifact chuyên biệt hoặc bản release ký số. Electron/React mở đường cho đa nền tảng; validation macOS chưa chứng minh chạy/đóng gói Windows/Linux.

Bước tích hợp tiếp theo nên truyền project/session ID tường minh từ voice routing của lamp qua boundary local có xác thực giữa Swift và manager, rồi trả status/event về. Quản lý session tiếp tục độc lập với executor screenshot/click/type.

## Build và cài local trên macOS

Trong `desktop/`, chạy `make build` để compile, rebuild node-pty, đóng gói theo kiến trúc Mac hiện tại và kiểm tra chữ ký ad-hoc. `make install` cài bundle đã kiểm tra vào `/Applications/Autonomous Buddy.app` qua thư mục staging, giữ riêng companion Swift `AutonomousBuddy.app`. `make open` mở app. Bản này chưa notarize để phân phối. Khi mở từ Finder, app bổ sung PATH của login shell (timeout 5 giây) và thư mục CLI thông dụng vào PATH hiện có để tìm agent đã cài. Đặt `BUDDY_APP_EXECUTABLE` tới executable trong bundle để chạy cùng bộ smoke test Electron trên app đóng gói.
