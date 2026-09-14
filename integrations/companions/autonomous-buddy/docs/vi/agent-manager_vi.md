# Autonomous Buddy — Desktop agent manager

Autonomous Buddy là một ứng dụng desktop có hai thành phần nội bộ. Electron main process quản lý agent process, Git/file, persistence và workspace React. Helper Swift trong `macos/` giữ pairing, device WebSocket và executor computer-use native. Người dùng chỉ cài và mở một `Autonomous Buddy.app`; helper chạy như process con, không phải app thứ hai cần cài.

Bố cục tham khảo workspace cơ bản của Orca. Implementation được viết mới tại repo này, không sao chép source hay asset Orca. Nếu tái sử dụng trực tiếp về sau phải audit license source/dependency riêng.

## Workspace

- Trái: project, Git worktree và session tương ứng; tìm kiếm, dấu chưa đọc và màn hình Needs attention. Open project dùng hộp chọn thư mục native. Tạo worktree sẽ tạo branch mới và thư mục ngang cấp có tên ghép từ project, branch và hậu tố ngẫu nhiên; không di chuyển công việc hiện tại.
- Giữa: tab session, đổi tên, trạng thái và CLI Codex/Claude/Terminal interactive thật qua xterm cùng `node-pty`. Session structured cũ giữ transcript và ô prompt/follow-up. Preview file và diff cũng mở tại đây. Xem [session interactive](interactive-sessions_vi.md).
- Phải: working changes, nhấn file để xem unified diff, cây file mở rộng và tối đa 30 commit gần nhất. Git status cập nhật mỗi 5 giây khi cửa sổ hiển thị, khi focus hoặc refresh thủ công. Danh sách commit chưa phải đồ thị branch/merge đầy đủ. Preview chỉ đọc, giới hạn file không phải binary tới 1 MiB. Bộ lọc file ở thư mục gốc không tìm toàn project.
- Kéo để đổi độ rộng panel hoặc ẩn sidebar. Cmd/Ctrl+K focus tìm kiếm, Cmd/Ctrl+N mở tạo session, Enter gửi và Shift+Enter xuống dòng.

Thư mục thường cũng có thể làm project mà không cần Git. Tạo worktree cần repository. Xóa project khỏi Buddy chỉ xóa đăng ký cùng session/transcript đã lưu, giữ file và worktree trên đĩa; không cho xóa khi session còn chạy hoặc đang khởi động.

## Model và protocol local

Contract nguồn: [`desktop/src/shared/types.ts`](../../desktop/src/shared/types.ts).

| Record | Trường / vai trò |
| --- | --- |
| `Project` | `id`, `name`, `path` local đã canonicalize |
| `Worktree` | `path`, `branch`, `head`, `primary`, `locked` tùy chọn; đọc từ Git |
| `Session` | `id`, `projectId`, `worktreePath`, `title`, `provider`, `providerSessionId` tùy chọn, `status`, `mode`, `closed`, `interactiveStarted`, `processActive`, `createdAt`, `updatedAt`, `unread` |
| `SessionEvent` | `id`, `sessionId`, `seq` tăng dần trong session, `at`, `type`, `text` |

Provider gồm `codex`, `claude`, `terminal`. Event gồm `prompt`, `output`, `status`, `error`, `result`, `terminal`. Trạng thái gồm `idle`, `running`, `needs_input`, `completed`, `error`, `stopped`. Terminal đang chạy hiển thị **Shell active**, không suy diễn thành coding agent.

Renderer sandbox dùng API preload hữu hạn `window.buddy`. Request/reply qua `ipcRenderer.invoke('buddy:<method>')`; `buddy:update` đẩy snapshot project/session/provider hoặc một session event. `session(id)` lấy lịch sử còn giữ, ghép với live event theo sequence. App không mở HTTP/WebSocket listener local. Command `agent.*` của device đã pair đi qua WebSocket Swift và pipe riêng: create/send có receipt request ID lưu bền, còn `agent.session` hỗ trợ cursor sequence có giới hạn. Xem contract [native bridge](./native-bridge_vi.md). Một session không nhận hai lượt gửi đồng thời; các session khác nhau có thể chạy song song.

IPC kiểm tra cửa sổ/main frame gửi và URL renderer local. Renderer không có Node integration. Workspace session phải thuộc các worktree của project đã đăng ký; duyệt file chặn path traversal và symlink ra ngoài workspace. Boundary này giới hạn API UI, không giới hạn năng lực lệnh người dùng chạy trong shell hoặc agent CLI.

## Tích hợp computer-use native

Panel **Computer & device** trong workspace chính hiển thị helper sẵn sàng hay không, device đã pair/kết nối và quyền Accessibility/Screen Recording. Panel có Pair (host tùy chọn và gợi ý device tìm được), Unpair, Pause/Resume khi đã pair, Manage permissions, Activity và Restart computer control khi helper không sẵn sàng. Pair và Activity mở các cửa sổ native hiện có; helper Swift giữ icon menu bar native (không thêm Dock icon), với pairing, pause, Activity, Open Agent Manager và Quit Autonomous Buddy.

Electron main process sở hữu `NativeHelper`. JSONL qua stdin/stdout kế thừa truyền request `{id, method, params}`, reply `{id, result}` hoặc `{id, error}` và snapshot đẩy `{event: "state", state}`. Stderr được đọc để thoát diagnostic. Không có socket/HTTP listener công khai và không trả pairing token cho renderer. Method native gồm `status` (alias `ping`), `pair`, `unpair`, `pause`, `permissions`, `activity`, `command`, `agent_response`, `agent_event`, `shutdown`. `command` chuyển payload `{action, params, timeout_ms}` qua dispatcher/audit Swift hiện có; chưa biến agent session thành vòng suy luận computer-use. `restart` là action vòng đời phía manager, không phải method protocol native. Renderer đi qua boundary IPC preload đã validate.

State gồm pairing, kết nối/lỗi, pause, quyền native, device tìm được và command gần đây. Quyền được kiểm tra thực tế, không suy diễn từ việc đóng gói. Pause hủy công việc native đang chạy; Unpair dùng luồng revoke rồi xóa pairing hiện có. Khi thoát, Electron đóng stdin; helper Swift thoát khi EOF kể cả parent crash, và `shutdown` cũng kết thúc helper. Manager tăng mức dừng sau hai giây nếu helper chưa thoát. Trên macOS, đóng cửa sổ workspace vẫn giữ computer use và agent session chạy. Open Agent Manager trên menu bar mở lại workspace; Quit Autonomous Buddy gửi event menu qua pipe riêng tới Electron để thoát toàn bộ app và helper.

Smoke test native đặt `BUDDY_NATIVE_TEST_MODE=1`: bỏ discovery và reconnect pairing đã lưu, ghi audit vào `/dev/null`, chặn pair/unpair, UI native và prompt quyền. Cờ này không vô hiệu hóa mọi executor command, nên test chỉ được gửi command kiểm tra có kiểm soát. Test không chứng minh kết nối device thật hay agent điều phối computer-use thật.

## Vòng đời agent

Session mới dùng [vòng đời CLI interactive](interactive-sessions_vi.md). Luồng theo từng lượt bên dưới áp dụng cho session **structured** được giữ lại hoặc tạo rõ ràng.

Codex dùng `codex exec` với JSON event, `--dangerously-bypass-approvals-and-sandbox` và `--skip-git-repo-check` để hỗ trợ thư mục project không có Git do người dùng chọn; follow-up dùng `exec resume` cùng thread ID đã lưu. Claude Code dùng print mode với `--dangerously-skip-permissions`, stream JSON và partial message; follow-up truyền `--resume` cùng session ID đã lưu. Prompt đi qua stdin, không qua command shell. CLI dùng tài khoản đã cài trên máy. Cả lượt mới và resume chạy full access, không hỏi quyền CLI theo yêu cầu tường minh của người dùng; không đổi cấu hình CLI toàn cục hay quyền macOS. Xem [agent execution](./agent-execution_vi.md).

Mỗi lượt gửi tạo một CLI process mới, giữ provider conversation ID qua các lượt. Output tới bất đồng bộ sau khi request gửi khởi động process. Hoàn tất cần completion event được nhận diện và exit thành công. Plain text không nhận diện và stderr diagnostic vẫn hiển thị nhưng không chứng minh hoàn tất. Nếu provider trả ID khác thì từ chối đổi conversation; nếu lượt trước chưa từng trả ID thì từ chối follow-up thay vì âm thầm mở conversation mới.

`needs_input` hiện biểu thị Claude trả permission denial trong result, chưa phát hiện mọi câu hỏi và chưa có bridge phê duyệt quyền trực tiếp. Người dùng đọc result rồi gửi follow-up hoặc dùng terminal riêng cho CLI tương tác; terminal đó không tự nối vào conversation đang quản lý. Codex chạy noninteractive cũng chưa có hộp phê duyệt do Buddy cung cấp.

Chuyển sang completed, needs-input hoặc error đánh dấu session chưa đọc; session chỉ đánh dấu đã đọc khi pane đó và cửa sổ đều focus. Nhấn desktop notification mở/focus đúng session. App gửi desktop notification khi chuyển sang các trạng thái này lúc cửa sổ không focus, tùy hỗ trợ/quyền của OS. Notice hoàn tất/cần chú ý cũng đi qua WebSocket Swift đã pair về device. Reconnect phát lại trạng thái kết thúc chưa đọc; delivery là best effort và notice có thể lặp.

Stop gửi signal tới process group trên POSIX và tăng mức dừng nếu process còn sống. Trên macOS, đóng cửa sổ cuối cùng vẫn giữ app và các session chạy qua menu bar. Quit tường minh dừng mọi session và helper native; trên nền tảng khác, đóng cửa sổ cuối cùng cũng thoát app.

## Lưu trạng thái và mở lại

`app.getPath('userData')/manager.json` của Electron lưu schema version 1, đăng ký project, session và lịch sử event có giới hạn. Trên macOS đường dẫn thông thường là `~/Library/Application Support/Autonomous Buddy/manager.json`; biến `BUDDY_DATA_DIR` ghi đè thư mục. Ghi file bằng thay thế atomic. Output lưu debounce 150 ms nên crash đột ngột có thể mất event mới nhất chưa ghi.

Mỗi session giữ tối đa 2.000 event và 2 MiB text (đo bằng độ dài JavaScript string); mỗi event giới hạn 65.536 ký tự. Sequence tiếp tục tăng khi bỏ event cũ. Đây là lịch sử gần đây, không phải bản lưu transcript đầy đủ lâu dài.

Khi mở lại, session đã lưu ở `running`/`needs_input` chuyển thành `stopped`. Không tự khởi động lại process. Follow-up agent có thể resume khi đã lưu provider ID và provider vẫn còn conversation đó. Lịch sử terminal được replay để xem; Restart terminal chạy shell mới trong cùng session/worktree. State hỏng hoặc version không hỗ trợ sẽ báo lỗi thay vì âm thầm bỏ dữ liệu.

## Phát triển và giới hạn

Xem prerequisite và lệnh trong [`desktop/README.md`](../../desktop/README.md): `npm install`, `npm run lint`, `npm test`, `npm run build`, `npm run test:e2e`, rồi `npm start`. Cần Node.js 22.12+, npm và native build tools để rebuild `node-pty` cho Electron; trên macOS dùng Xcode Command Line Tools. `npm start` dùng bản đã build; `npm run dev` build rồi mở app, không có hot reload. Smoke test Electron cần desktop session và bản build sẵn.

Cài và đăng nhập agent CLI riêng. Buddy tìm `codex`/`claude` trong `PATH` được kế thừa; nên mở từ terminal có PATH đó. Có executable không đồng nghĩa đã xác thực tài khoản. Terminal dùng `$SHELL`, dự phòng `/bin/sh`.

Backend test dùng repository tạm và launcher mô phỏng. Smoke test Electron dùng state tạm và response CLI agent mô phỏng, không cần gọi model trả phí. Build/test pass không thay thế kiểm chứng phiên bản CLI đã đăng nhập với provider thật.

Đợt này chưa có adapter OpenCode/provider tùy chỉnh, remote companion, đồ thị Git commit đầy đủ, combined diff/hunk review/edit-save/push, renderer research artifact chuyên biệt hoặc bản release ký số. Electron/React mở đường cho đa nền tảng; validation macOS chưa chứng minh chạy/đóng gói Windows/Linux.

Skill agent-management trên lamp có thể list/create/send/read/stop managed session bằng command của device đã pair. Send/follow-up hỗ trợ session structured và session coding interactive được hook xác minh sẵn sàng; session tạo qua voice mặc định interactive trì hoãn khởi chạy, prompt đầu truyền bằng argv; mode structured rõ ràng vẫn hỗ trợ để tương thích. Xem [session interactive](interactive-sessions_vi.md). Request ID ổn định bảo vệ retry create/send; receipt chưa hoàn tất sau crash từ chối tự replay. Test WebSocket giả xác minh relay mà không truy cập device thật; luồng lamp/CLI thật vẫn cần kiểm chứng end-to-end. Session management độc lập với executor screenshot/click/type. Xem [native bridge](./native-bridge_vi.md) và [review gap từ source Orca](./orca-gap-review_vi.md).

## Build và cài local trên macOS

Trong `autonomous-buddy/` hoặc `desktop/`, `make build` compile Electron, rebuild node-pty trong bản sao dependency ở staging, build Swift release rồi đóng gói một app có chữ ký được kiểm tra. Mặc định dùng kiến trúc Node hiện tại; `make build BUDDY_ARCH=x64` build cho Intel, gồm cả helper Swift (`x86_64`). `make dmg` mặc định tạo hai DMG riêng cho `arm64` và `x64`; dùng `BUDDY_ARCHS=x64` để chỉ build Intel. Metadata release theo dõi từng kiến trúc riêng; xem [ký số release](release-signing_vi.md). Executable native nằm ở `Contents/Resources/native/AutonomousBuddy`, resource bundle của SwiftPM nằm cạnh nó. `make install` build lại cả hai thành phần rồi cài `/Applications/Autonomous Buddy.app` qua staging. Trước khi thay thế, installer kiểm tra bundle ID, chữ ký và executable helper; thoát đúng các app cũ theo đường dẫn; lưu bundle cũ vào `~/Library/Application Support/AutonomousBuddy/LegacyBackups/` bằng tên duy nhất có hậu tố `.disabled`. App Swift riêng `/Applications/AutonomousBuddy.app` cũng được lưu ở đó, nên Applications chỉ còn một sản phẩm. Nếu thay thế lỗi thì phục hồi bundle cũ. Dữ liệu pairing và manager được giữ nguyên. `make open` mở app đã cài.

Bundle giữ ID `network.autonomous.ai.buddy.manager` để tiếp tục state manager; pairing store native không thay đổi. Electron chạy helper với `--embedded-helper`, giao tiếp qua pipe riêng của process con, không mở listener mạng local. Helper dừng cùng parent; khi helper không khởi động được, chức năng native báo không sẵn sàng. Quyền Accessibility/Screen Recording vẫn do macOS quản lý và có thể cần cấp lại sau khi đóng gói. Command computer-use từ device tiếp tục do Swift xử lý; `agent.*` chuyển tiếp vào manager bằng project/session ID tường minh.

Mặc định ký ad-hoc nếu không truyền `DEV_ID_APP`; Makefile cha tự tìm Developer ID có sẵn. Build/install không tự notarize. Các target `app-signed`, `dmg`, `dmg-signed`, `notarize` dùng app hợp nhất; recipe phát triển/release Swift cũ được giữ với tiền tố `native-*`. Phân phối Developer ID/notarization cần validation release riêng. Khi mở từ Finder, app bổ sung PATH login shell (timeout 5 giây) và thư mục CLI thông dụng. Đặt `BUDDY_APP_EXECUTABLE` tới executable trong bundle để chạy smoke test Electron trên app đóng gói.

### Split pane và Git review hiện tại

Mỗi tab có cây split đệ quy lưu trong localStorage. Split phải/dưới tạo Terminal N
thật ở root worktree, có focus/input riêng, resize và close pane dừng/lưu trữ
session đích nhưng giữ history. Chưa kế thừa cwd live của shell nguồn hoặc kéo agent
có sẵn giữa pane/tab. Draft prompt structured cũ lưu theo session, giữ qua remount/reopen.

Git panel stage/unstage từng file và commit **toàn bộ index hiện tại** bằng message
đã nhập, gồm cả thay đổi đã staged ngoài Buddy; file unstaged giữ trên đĩa. Chọn
commit mở danh sách file rồi diff từng file so với parent đầu tiên; root commit
so với empty tree. Chưa combined/hunk review, edit/save hay push.

Đã kiểm chứng Codex và Claude thật ở lượt đầu/follow-up với no-approval flags;
quota thật có Codex và cửa sổ Claude 5h/7d/Fable. E2E bundle cơ bản đã pass; E2E
tích hợp split/Git mới còn pending tại lúc cập nhật tài liệu này.

### Biểu tượng app macOS

Biểu tượng Dock/Finder của app đóng gói dùng cùng SF Symbol `lightbulb.fill` với
biểu tượng menu bar khi Buddy đã kết nối: bóng đèn vàng trên nền vuông tối bo góc.
`desktop/scripts/generate-icon.swift` dùng AppKit tạo iconset lúc đóng gói;
`iconutil` tạo `.icns` để truyền vào Electron Packager. Các file icon được tạo nằm
trong `desktop/artifacts/icon/` đã được ignore; không đưa binary icon vào Git.
