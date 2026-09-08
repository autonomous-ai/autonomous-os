# Review source Orca và gap workflow của Buddy

Review ngày 2026-09-08 từ checkout đầy đủ của `stablyai/orca`, commit
`ba5f708290b72012132fb23a6f016b8fd5601718` (package version 1.4.197). Đây là commit
khác tree cache trước đó `1a8640adb6e86abb342a8025892300b2835f3e8e`; mọi link Orca
bên dưới pin source thực sự đã đọc. Đây là review tĩnh, không khẳng định đã
build/chạy Orca hay Buddy tương đương Orca. Không copy source/asset. Kết quả Buddy
phản ánh worktree hiện tại, gồm thay đổi group/workspace/quota/tab-close và relay
native chưa commit.

## Kết luận chính

Điểm cần học ở Orca là mô hình sở hữu và workflow hằng ngày, không chỉ ba cột.
Repository sở hữu workspace; workspace sở hữu tab group; tab sở hữu terminal leaf
hoặc nội dung khác; mỗi leaf gắn PTY và provider session riêng. Status, attention,
resume và keyboard focus đi theo các định danh này. Buddy đã có project/session
local và native integration chạy được nhưng còn thiếu các tương tác thường dùng.
Đổi màu card hoặc thêm nút rời rạc không giải quyết những gap đó.

## Đối chiếu có source

“Có” nghĩa là có implementation cụ thể, không phải tương đương toàn bộ Orca.
“Một phần” chỉ rõ phần đã có và phần thiếu. “Thiếu” nghĩa là không tìm thấy đường
thực thi tương ứng trong contract/component Buddy đã kiểm tra.

| Workflow | Bằng chứng và hành vi Orca | Bằng chứng Buddy và gap hiện tại | Ưu tiên |
| --- | --- | --- | --- |
| Tổ chức project/repository | [Repo][repo] có tên/định danh, project group/order, base-ref/path, execution owner; [group actions][groups] chặn catalog async cũ theo host. | **Một phần.** `desktop/src/shared/types.ts:3`, `renderer/WorkspaceSidebar.tsx:170` có path canonical và group dạng chuỗi. Chưa có group ID/order riêng, setup policy theo repo hay host ownership. Local ownership đủ cho MVP hiện tại. | Core: thứ tự/group local dễ đoán. Remote ownership để sau. |
| Vòng đời worktree và phân cấp status | [Worktree][workspace] tách workspace status, pin/unread/archive khỏi Git/host identity; [board grouping][kanban] áp dụng status và manual/recency order. | **Một phần.** `shared/types.ts:57`, `main/manager.ts:197`, `renderer/WorkspaceSidebar.tsx:238` có tên, parent, pin/unread và active/review/done cố định. Có create/delete/sleep (`manager.ts:229`, `:243`, `:297`). Chưa có custom status, archive model, manual order, board. | Core: phân cấp/status rõ ràng, tách trạng thái process agent. Board/custom status tùy chọn. |
| Policy tạo worktree | [Repo][repo] lưu base ref/path, hook và path dùng chung/symlink. | **Một phần.** Buddy tạo branch/worktree ở thư mục ngang cấp và validate worktree hiện hữu. Chưa có chọn base ref, setup command, dùng lại dependency hay trạng thái setup lỗi. | Sau layout/session: chọn branch gốc, hiển thị lỗi setup. |
| Tab và sở hữu nội dung | [Tab model][layout] có group đệ quy, group/worktree/entity ID, preview khác tab cố định, nhiều content type. | **Một phần.** `renderer/App.tsx:54`, `:201`, `:462` lưu tab session đã đóng, mở lại từ sidebar, rename/Cmd+W không stop. File/diff chỉ có một preview tạm; chưa lưu tab order/group, pin hay di chuyển nội dung giữa group. | Core: open/close/reopen/active tab và preview nhất quán trước đổi giao diện. |
| Terminal pane song song | [Terminal layout][terminal] lưu leaf ID ổn định, focus/expand, buffer từng leaf; [split actions][split] split phải/dưới kế thừa cwd thực tế. | **Thiếu.** `renderer/TerminalView.tsx` tạo một xterm cho một session. Resize cột trái/giữa/phải không phải split terminal. Chưa có pane-to-PTY, focus navigation hay close từng pane. | Core: agent/shell cạnh nhau, input/output đúng chủ. |
| Start/follow-up/stop/sleep/resume | [Sleeping record][resume] gắn provider ID, transcript path, launch env và nguồn restore; [hibernation][hibernate] không kill trước khi lưu record resume được. | **Một phần.** `main/manager.ts:365`, `:486`, `main/providers.ts:34` chạy/resume Codex/Claude và stop process group. Sleep stop session; restart đánh dấu stopped và cần follow-up rõ ràng. Scrollback terminal có giới hạn, không phục hồi process sống. | Core: nói rõ sleep/wake/restore; giữ hội thoại trước khi tuyên bố resume. |
| Agent state và input tương tác | [Status][status] phân biệt working/blocked/waiting/done, không suy từ terminal title; [approval card][approval] gửi lựa chọn về đúng PTY; [chat subscriptions][chat] gắn frame transcript vào subscription/session. | **Một phần.** Buddy parse completion/ID có cấu trúc và permission-denial result Claude thành needs_input (`main/providers.ts:175`). Chưa có live question/approval hay provider tool/message structure. User hiện yêu cầu no-approval; [flag Orca][permissions] cũng hỗ trợ mode đó. | Core: waiting/error/completion và follow-up đúng context. UI approval tùy chọn với policy hiện tại, không phải lý do bật lại prompt. |
| Review và hoàn tất thay đổi | [Bulk actions][stage] stage/unstage path đã chọn; [commit area][commit] có action commit/stage/push; [combined diff][diff] tách branch/commit/working và mở/edit file. | **Một phần.** `renderer/GitPanel.tsx:68` mở unified diff working chỉ đọc; `main/git.ts` đọc status/file. Chưa stage/unstage, commit composer, push, edit/save, hunk review, branch compare. | Core: review đồng bộ và stage/commit lựa chọn tường minh. Push là hành động riêng do user yêu cầu. |
| Commit history và kết quả file/research | [History actions][history] mở file commit và diff đơn/gộp; [tab][layout] tách editor/diff/browser/session. | **Một phần.** `renderer/GitPanel.tsx:167` chỉ hiện dòng commit gần đây, chưa inspect file commit. Có preview file/result text nhưng chưa có artifact viewer/editor có cấu trúc. | Core: inspect commit và mở file output. Rich artifact/browser để sau. |
| Unread và attention | [Auto-ack][ack] xác nhận đúng leaf active và timestamp trạng thái; xem một pane không được xóa attention pane khác. | **Một phần.** `main/manager.ts` lưu unread session; `main/index.ts:35` gửi desktop notification; relay gửi notice trạng thái về lamp. Workspace unread là metadata riêng. Chưa có ack theo leaf, click notification tới đúng pane hay phát hiện blocked tổng quát. | Core cùng pane identity; không đánh dấu đã xem công việc chưa thấy. |
| Hiển thị usage | [Usage state][usage] có opt-in scan, summary, daily/model/project breakdown và recent session cho nhiều provider. | **Một phần.** `main/provider-usage.ts:191`, `renderer/ProviderUsageBar.tsx` hiện quota subscription ready/unavailable/error. Không phải token/cost analytics hay phân bổ theo project. | Giữ quota chính xác; analytics tùy chọn. |
| Settings, keyboard, navigation | [Key registry][keys] định nghĩa file/worktree/settings navigation; [pane shortcut][panekeys] có split. [Workflow settings][settings] dùng metadata tìm kiếm chung cho Git/terminal/các mục khác. | **Một phần.** `renderer/App.tsx:153`, `:212` có search/new-session/close và settings đơn giản. Chưa command registry/palette, remap shortcut, quick-open file, MRU tab, focus-next-pane. | Core: phím dễ khám phá cho workspace/tab/pane; settings rộng để sau. |
| Mobile/remote và service sản phẩm | [Repo ownership][repo], [catalog][groups], [package][package] có runtime/SSH ownership và nhiều service tùy chọn. | **Thiếu có chủ đích.** Lamp đã pair điều khiển manager local qua Swift, không phải runtime session multi-host/mobile của Orca. | Sau workflow local hằng ngày. |
| Native computer use | Orca có browser/runtime integration rộng hơn trong [package][package]; không bắt buộc để học workflow trên. | **Có, là boundary nội bộ riêng.** Pairing/permission/command/menu Swift và manager Electron là một app; [native bridge](./native-bridge_vi.md) mô tả relay/lifecycle thật. Chưa chứng minh vòng tự lập kế hoạch từ screenshot. | Giữ Swift executor, không thay chỉ để copy UI. |

## Boundary tái sử dụng và license

[License gốc][license] là MIT, `Copyright (c) 2026 Lovecast Inc.`. Copy module Orca
cần giữ notice và toàn bộ MIT text khi phân phối. Review này không audit mọi
dependency hay cho phép copy toàn asset bundle. [Site notices][notices] ghi Geist
có OFL riêng; MIT gốc không cho phép bỏ notice font/dependency. Checkout còn có
license riêng của mobile audio package và grammar vendor. Cần đọc license từng
file được chọn và transitive imports trước khi tái sử dụng.

Ứng viên *tách logic*, không phải drop-in: cây tab/layout immutable, stable pane
ID, helper order/status, keybinding definition và regression case tương ứng.
Ngay `tab-types.ts` đã import agent/execution-host/AI-Vault riêng của Orca; nên tách
contract nhỏ do Buddy sở hữu, không kéo cả type graph. Không đánh dấu module nào
“dùng trực tiếp nguyên trạng” khi chưa audit dependency đó.

Component source-control/pane phụ thuộc `useAppStore` Zustand, routing
`@/runtime/*`, connection ownership, translation, telemetry, Monaco và preload
Orca. [Manifest][package] có node-pty/xterm cùng SSH, filesystem watcher, Claude
SDK, browser automation, speech model và service client; build script còn build
CLI/runtime. Tái sử dụng nguyên pane kéo theo nhiều thứ hơn React markup. Không
clone cả app hay nhận remote runtime chỉ để có tab local.

Giữ ba boundary kiểm thử riêng: (1) manager local với project/workspace/session/
pane identity lưu bền; (2) React view dùng manager API hữu hạn rõ ràng; (3) executor
computer native theo OS và device transport. Helper Windows/Linux sau này thay
boundary 3, không thay model session.

## Thứ tự triển khai và bằng chứng nghiệm thu

1. **Nền workflow thành một slice thống nhất:** model tab/content/pane lưu bền,
   split/focus/close/reopen, cwd đúng và session binding ổn định. Nghiệm thu: hai
   agent và shell cùng workspace, stream độc lập, input không lạc pane, close
   không kill pane bên cạnh; reopen/restart giữ identity và báo đúng thứ đã stop.
2. **Khép vòng task:** running/waiting/completed/error đáng tin, follow-up đúng
   provider session, stop/sleep/wake và unread/notification chính xác. Tôn trọng
   policy no-approval đã yêu cầu. Nghiệm thu: concurrent turn, cancel,
   restart/reconnect và CLI đã đăng nhập; mock test xanh riêng không đủ.
3. **Khép review local:** chọn file đổi, combined diff, stage/unstage, commit
   draft/result, inspect file/diff commit. Nghiệm thu repo tạm có
   staged/unstaged/untracked/rename; chỉ path đã chọn thay đổi, lỗi hiển thị,
   không tự push.
4. **Polish từ action model chung:** palette/navigation, thứ tự local lưu bền,
   setup/settings cần thiết; quota vẫn phải đúng. Custom board, remote/mobile,
   analytics và rich artifact để sau khi phục vụ workflow thực tế đã kiểm chứng.

Các ưu tiên này là đề xuất, không chứng minh gap đã được đóng. Audit không sửa
production. Validation gồm đọc source đã pin và đối chiếu contract/component
Buddy; không cài dependency, build hay chạy Orca.

[repo]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/repo-types.ts#L42
[workspace]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/worktree/types.ts#L52
[groups]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/store/project-groups/project-group-catalog-actions.ts#L16
[kanban]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/sidebar/workspace-kanban-worktree-groups.ts#L22
[layout]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/tab-types.ts#L8
[terminal]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/terminal-tab-types.ts#L60
[split]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/terminal-pane/use-terminal-pane-split-actions.ts#L39
[resume]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/agent-session-resume.ts#L45
[hibernate]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/store/terminals/terminal-pane-hibernation.ts#L47
[status]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/agent-status-types.ts#L25
[approval]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/native-chat/NativeChatApprovalCard.tsx#L5
[chat]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/preload/api/native-chat-api.ts#L16
[ack]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/hooks/useAutoAckViewedAgent.ts#L23
[stage]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/right-sidebar/source-control/commit/use-bulk-actions.ts#L26
[commit]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/right-sidebar/source-control/commit/commit-area.tsx#L30
[history]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/right-sidebar/source-control/sync/use-git-history-commit-actions.ts#L41
[diff]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/editor/combined-diff/review-controls/use-combined-diff-section-actions.ts#L27
[keys]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/keybindings/definitions-core-1.ts#L6
[panekeys]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/keybindings/definitions-core-4.ts#L22
[usage]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/store/slices/usage-provider-slices.ts#L19
[settings]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/hooks/settings-navigation-workflow-sections.ts#L28
[permissions]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/tui-agent-permissions.ts#L7
[license]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/LICENSE#L1
[notices]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/docs/site/THIRD_PARTY_NOTICES.md#L3
[package]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/package.json#L1
