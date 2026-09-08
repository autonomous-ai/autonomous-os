# Review source Orca và gap workflow của Buddy

Review ngày 2026-09-08 từ checkout đầy đủ của `stablyai/orca`, commit
`ba5f708290b72012132fb23a6f016b8fd5601718` (package version 1.4.197). Đây là commit
khác tree cache trước đó `1a8640adb6e86abb342a8025892300b2835f3e8e`; mọi link Orca
bên dưới pin source thực sự đã đọc. Đây là review tĩnh, không khẳng định đã
build/chạy Orca hay Buddy tương đương Orca. Không copy source/asset. Kết quả Buddy
phản ánh worktree hiện tại, gồm group/workspace/quota/tab-close, relay native và split/Git review mới.
Cây split/Git đã đọc từ source; E2E tích hợp mới còn pending tại lúc cập nhật,
không lấy E2E bundle cơ bản đã pass để khẳng định phần mới đã pass.

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
| Tổ chức project/repository | [Repo][repo] có tên/định danh, project group/order, base-ref/path, execution owner; [group actions][groups] chặn catalog async cũ theo host. | **Một phần.** `desktop/src/shared/types.ts`, `renderer/WorkspaceSidebar.tsx` có path canonical và group dạng chuỗi. Chưa có group ID/order riêng, setup policy theo repo hay host ownership. Local ownership đủ cho MVP hiện tại. | Core: thứ tự/group local dễ đoán. Remote ownership để sau. |
| Vòng đời worktree và phân cấp status | [Worktree][workspace] tách workspace status, pin/unread/archive khỏi Git/host identity; [board grouping][kanban] áp dụng status và manual/recency order. | **Một phần.** `shared/types.ts`, `main/manager.ts`, `renderer/WorkspaceSidebar.tsx` có tên, parent, pin/unread và active/review/done cố định. Có create/delete/sleep (`manager.ts`). Chưa có custom status, archive model, manual order, board. | Core: phân cấp/status rõ ràng, tách trạng thái process agent. Board/custom status tùy chọn. |
| Policy tạo worktree | [Repo][repo] lưu base ref/path, hook và path dùng chung/symlink. | **Một phần.** Buddy tạo branch/worktree ở thư mục ngang cấp và validate worktree hiện hữu. Chưa có chọn base ref, setup command, dùng lại dependency hay trạng thái setup lỗi. | Sau layout/session: chọn branch gốc, hiển thị lỗi setup. |
| Tab và sở hữu nội dung | [Tab model][layout] có group đệ quy, group/worktree/entity ID, preview khác tab cố định, nhiều content type. | **Một phần.** `renderer/App.tsx` lưu tab session đã đóng, mở lại từ sidebar, rename/Cmd+W không stop. File/diff chỉ có một preview tạm; có layout pane lưu lại theo tab; chưa lưu tab order/group, pin hay di chuyển nội dung giữa group. | Core: open/close/reopen/active tab và preview nhất quán trước đổi giao diện. |
| Terminal pane song song | [Terminal layout][terminal] lưu leaf ID ổn định, focus/expand, buffer từng leaf; [split actions][split] split phải/dưới kế thừa cwd thực tế. | **Một phần.** `desktop/src/renderer/SessionWorkspace.tsx`, `split-layout.ts` đã có cây split đệ quy keyed lưu lại, tạo Terminal N thật bên phải/dưới, focus độc lập, resize và close pane không stop session. Split bắt đầu ở root worktree, chưa kế thừa cwd hiện tại của shell nguồn. Chưa đặt agent có sẵn vào pane tùy ý, chuyển pane giữa tab hay shortcut đổi focus. | Core: kiểm chứng input isolation/restore, sau đó đặt agent có sẵn và cwd thực tế. |
| Start/follow-up/stop/sleep/resume | [Sleeping record][resume] gắn provider ID, transcript path, launch env và nguồn restore; [hibernation][hibernate] không kill trước khi lưu record resume được. | **Một phần.** `main/manager.ts`, `main/providers.ts` chạy/resume Codex/Claude và stop process group. Sleep stop session; restart đánh dấu stopped và cần follow-up rõ ràng. Scrollback terminal có giới hạn, không phục hồi process sống. | Core: nói rõ sleep/wake/restore; giữ hội thoại trước khi tuyên bố resume. |
| Agent state và input tương tác | [Status][status] phân biệt working/blocked/waiting/done, không suy từ terminal title; [approval card][approval] gửi lựa chọn về đúng PTY; [chat subscriptions][chat] gắn frame transcript vào subscription/session. | **Một phần.** Buddy parse completion/ID có cấu trúc và permission-denial result Claude thành needs_input (`main/providers.ts`). Chưa có live question/approval hay provider tool/message structure. User hiện yêu cầu no-approval; [flag Orca][permissions] cũng hỗ trợ mode đó. | Core: waiting/error/completion và follow-up đúng context. UI approval tùy chọn với policy hiện tại, không phải lý do bật lại prompt. |
| Review và hoàn tất thay đổi | [Bulk actions][stage] stage/unstage path đã chọn; [commit area][commit] có action commit/stage/push; [combined diff][diff] tách branch/commit/working và mở/edit file. | **Một phần.** `desktop/src/renderer/GitPanel.tsx`, `main/git.ts` đã stage/unstage từng file và commit toàn bộ index hiện tại bằng message tường minh; working file vẫn giữ trên đĩa. Preview chỉ đọc; chưa combined/branch diff, hunk action, edit/save hay push. Commit bao gồm cả file đã staged từ trước, không chỉ file stage bằng Buddy. | Core: xác minh tác động index chính xác; combined review tiếp theo. Push vẫn là scope riêng tường minh. |
| Commit history và kết quả file/research | [History actions][history] mở file commit và diff đơn/gộp; [tab][layout] tách editor/diff/browser/session. | **Một phần.** `desktop/src/renderer/GitPanel.tsx` đã mở commit thành danh sách file đổi và diff từng file so với parent đầu tiên (empty tree nếu root commit). Chưa full graph, combined commit review hay structured research artifact viewer/editor. | Đã có inspect cơ bản; kiểm chứng rename/root commit. Rich artifact/browser để sau. |
| Unread và attention | [Auto-ack][ack] xác nhận đúng leaf active và timestamp trạng thái; xem một pane không được xóa attention pane khác. | **Một phần.** `desktop/src/renderer/SessionView.tsx` chỉ mark read khi pane và document cùng focus. Notification `main/index.ts` mở/focus đúng session, relay gửi notice về lamp. Workspace unread vẫn riêng; chưa có blocked detector tổng quát hay ack timestamp/retained leaf như Orca. | Đã có pane-aware ack và mở session từ notice; kiểm chứng concurrent completion. |
| Hiển thị usage | [Usage state][usage] có opt-in scan, summary, daily/model/project breakdown và recent session cho nhiều provider. | **Một phần.** `main/provider-usage.ts`, `renderer/ProviderUsageBar.tsx` hiện quota subscription ready/unavailable/error. Không phải token/cost analytics hay phân bổ theo project. | Giữ quota chính xác; analytics tùy chọn. |
| Settings, keyboard, navigation | [Key registry][keys] định nghĩa file/worktree/settings navigation; [pane shortcut][panekeys] có split. [Workflow settings][settings] dùng metadata tìm kiếm chung cho Git/terminal/các mục khác. | **Một phần.** `renderer/App.tsx` có search/new-session/close và settings đơn giản. Chưa command registry/palette, remap shortcut, quick-open file, MRU tab, focus-next-pane. | Core: phím dễ khám phá cho workspace/tab/pane; settings rộng để sau. |
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

Thứ tự trên là roadmap gốc; split cơ bản, stage/commit và inspect commit đã có
source triển khai như matrix cập nhật. Phần kế tiếp là validation các luồng mới
và đóng gap còn lại, không viết lại phần đã có. Các ưu tiên không chứng minh
E2E tích hợp split/Git đã pass. Audit không sửa
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
