# Đánh giá đăng ký runtime Intern

## Kiểm tra đăng ký an toàn đầy đủ — 2026-09-14

Source OS: `ed3e8b12d3b7890047f9d60bd075f1af89105d35`, PR #406 hiện có.
Source producer Welcome Desk được đọc cục bộ tại commit
`16bb0d3f17083ffc9c9b466ff7470e2e4807b083`: các file
`packages/agents/src/gus/agents/intern_bridge.py`, `intern.py` và
`packages/voice/src/gus/voice/welcome_desk.py`. Đây là kiểm tra source,
không phải xác minh listener hay model đã cài. Client Go vẫn ghim protocol
`0.2.0` / `cassi-first.v1`.

**Kết luận: giữ client chưa đăng ký; không cho chọn `intern`.** Hợp đồng còn
thiếu mang tính quyết định là ai có quyền xác nhận một input cùng context bổ
sung thuộc public/business. Thêm trường chuỗi hoặc kiểm tra loopback không tạo
ra quyền đó. Producer tin `data_class` do caller cung cấp, không xác thực hay
quyết định custody thay OS. Mặc định toàn bộ chat là business, đọc nhãn trong
prompt hoặc tin type `web_chat` đều tự tạo quyền không có căn cứ. Chỉ cho phép
probe public cố định và từ chối mọi input thường cũng không tạo runtime dùng được.

### Bằng chứng source và điều kiện gỡ chặn

Số dòng dưới đây ứng với commit OS trên; tên hàm là khóa tra cứu ổn định.
Không coi các tính năng full-brain tùy chọn là yêu cầu của Welcome Desk văn bản.

| Ranh giới | Bằng chứng source | Điều kiện trước đăng ký |
|---|---|---|
| Trusted admission | `system/server/server.go:450` gắn `sameOriginOrLAN` cho sensing. `system/server/middleware.go:22` kiểm tra vị trí/origin, không xác minh thẩm quyền phân loại input. `SensingEventRequest` tại `system/server/sensing/delivery/http/handler.go:89` không mang class hoặc bằng chứng admission; `PostEvent:211` bind JSON caller. | Xác định producer/policy có thẩm quyền và xác minh assertion trước khi dùng nội dung. Trường JSON `data_class` từ caller bất kỳ không đủ. Chặn unknown/restricted/secret và attachment không hỗ trợ trước log, lưu, bổ sung context hoặc gửi mạng. Integration được kiểm tra chưa xác lập thẩm quyền này. |
| Class qua biến đổi và replay | `PostEvent:231` log văn bản; `:475` ghi ảnh; `:668` queue chuỗi/ảnh/ID. `:793` lấy identity từ request/mood, `:806` tạo message bổ sung, `:834` có thể thêm kết quả Harness. Gateway tại `:905` chỉ nhận text và ID. `runtimes/picoclaw/events.go:99` chụp mood; `:252` dựng lại text khi drain. | Giữ bằng chứng admission bất biến gắn với payload thực qua queue giới hạn. Identity, guard, environment và context Harness bổ sung cần admission riêng. Với Intern, bỏ qua augmentation không hỗ trợ và từ chối ảnh/file trước các thao tác ghi hiện có; không sao chép queue cũ. |
| Completion và tác động | `system/server/server.go:343` truyền event handler chung vào `StartWS`. `system/server/agent/delivery/http/handler_event_agent.go:335` bật busy; `:346` xóa busy khi end/error. `:901` chặn TTS cho web chat nhưng `:936` vẫn gọi `fireHWCallsSync`. `system/server/config_watch.go:handleSetUpCompleteChange` độc lập khởi chạy reconcile, refresh token, schedule, HAL, ambient và healthwatch. | Cần consumer event chỉ văn bản và capability gate trước các tác động startup/config; test bằng bộ đếm tác động được inject. Chặn `[HW:` ở client không thay thế gate toàn OS. TTS suppression không bảo đảm không hành động. |
| Correlation, hủy và readiness | Producer `_safe_run_id` hash ID; `_BridgeState.reserve_run` giữ ID trước inference. `InternBridgeHandler.do_GET` trả readiness tĩnh; `do_POST` chỉ hoàn tất `bridge_request`. Không có API hủy hoặc tra kết quả. `Client.Do` đã kiểm hash và không retry; `ProbeGeneration` dùng request public cố định. | Có thể triển khai correlation ở Go: giữ ID gốc, đúng một kết quả kết thúc cục bộ, xóa busy/marker khi lỗi hoặc shutdown, queue giới hạn. Timeout là kết quả remote chưa xác định, không phải remote đã hủy hay handoff thành công. Generation thật chỉ chứng minh khả năng sinh tại thời điểm đó, không chứng minh session, uptime, hoàn tất nhân viên hoặc provider. HTTP tự nó không phải blocker. |
| Quyền activation | `system/device/runtime.go:updateAgentRuntime` chạy `ensureSwitchRuntime`, materializer và `runSwitchRuntime` trước lưu config; `RestartForAgentRuntime` restart os-server. `runtime_installers.go:materializeInstaller` trả nil nếu thiếu installer; `switch_runtime.sh:install_new` khi đó tải và chạy installer CDN. Switcher điều khiển unit systemd cũ/mới. HTTP yêu cầu readiness; MQTT chỉ xác nhận unit active. | Bridge do bên ngoài giám sát cần đường chọn riêng đã kiểm chứng, bỏ installer/presync/CDN/unit control; xử lý cả chuyển vào và ra Intern, lỗi lưu config và quyền restart. Không thêm installer không có nghĩa an toàn. Không tạo unit giả hoặc dùng `remote` vì nó tạo Hermes. |

Default `create_server` của producer là `http://127.0.0.1:8765`; đây là default
đọc từ source, chưa phải endpoint OS đã kích hoạt. Đăng ký sau phải cố định
endpoint loopback với tên `intern`, Cassi là receptionist đầu tiên, wake word
Gus vẫn xử lý phía producer và không ủy quyền Hermes/OpenClaw. Không truy cập
credentials hay thiết bị để thực hiện đánh giá.

Thiếu session, inference ảnh, skills, cấu hình model hoặc channel không tự nó
buộc phải xây full-brain. Operation không hỗ trợ có thể trả lỗi rõ ràng;
session trống/uptime chưa biết và watcher không hoạt động có context có thể
đáp ứng hợp đồng khi phản ánh đúng thực tế. Không nhúng interface nil, panic,
giả history hoặc trả thành công cho thao tác không làm. Checklist installer/
persona full-brain lịch sử bên dưới **không** yêu cầu cài service Welcome Desk
hay sao chép workspace không được tiêu thụ.

Fallback trong phạm vi hoàn tất khi hai bản audit và proof local được ghi lại;
đăng ký runtime vẫn chờ trusted admission và các gate integration đã kiểm thử.
Xem [proof và giới hạn hiện tại](../../receipts/intern-welcome-desk-audit-2026-09-14.md).

## Đánh giá lại Welcome Desk — 2026-09-14

Kiểm tra tại `0f180391b22446d7fd2e63095b777bd0d34bc809` với client hiện tại
`0.2.0` / `cassi-first.v1`. **Bị chặn: chưa thể chọn `intern`, kể cả sau khi
merge thay đổi tài liệu này.** Phạm vi mới chỉ là Welcome Desk văn bản,
Cassi-first, không thực thi, credentials hay ghi thiết bị. Không cần thêm
tools nhân viên, memory hoặc ảnh; checklist full-brain cũ bên dưới không bắt
buộc Welcome Desk phải có các tính năng đó.

Ba khoảng trống hiện tại vẫn ngăn một adapter nhỏ và an toàn:

1. **Admission:** `AgentGateway.SendChatMessage*` và `QueuePendingEvent` không
   mang data class đáng tin. Caller sensing gửi và replay chuỗi thô
   (`system/server/sensing/delivery/http/handler.go`). `Client.Do` chặn
   unknown/restricted/secret trước mạng. Mặc định mọi chat là public/business
   sẽ phá ranh giới custody; từ chối mọi chat không tạo backend dùng được.
   Cần policy input đáng tin được giữ nguyên qua queue/replay.
2. **Lifecycle và tác động:** `system/server/server.go` khởi chạy event loop
   và watcher. `system/server/config_watch.go` còn gọi migration, reconcile
   MCP/user và restart HAL/thiết lập voice theo điều kiện, bên ngoài adapter.
   Trả unsupported trong adapter không chặn được các tác động đó.
   `handler_event_agent.go` đưa lifecycle vào pipeline thiết bị/TTS/delivery.
   Client đã chặn hardware marker, nhưng `executes_actions:false` không phải
   capability gate toàn OS. Lifecycle chỉ văn bản cần event kết thúc có
   correlation, queue/cancellation có giới hạn và kiểm chứng chặn tác động
   thiết bị/channel. HTTP completion chỉ có phạm vi `bridge_request`.
3. **Selection và quyền quản lý:** `system/domain/device.go:AgentRuntimes`
   không có `intern`; API switch từ chối. Tự ghi `agent_runtime: "intern"`
   lại dẫn tới **fallback OpenClaw** trong
   `system/agent/factory.go:resolveRuntime`, không phải cách kích hoạt Intern.
   Chỉ thêm tên vào list sẽ đi qua `system/device/runtime.go` và
   `runtime_installers.go`: ghi script, cài đặt/điều khiển service rồi lưu
   config. Bridge loopback do bên ngoài quản lý cần activation rõ ràng, được
   kiểm chứng và chặn các tác động này. `remote` chọn Hermes nên không phù hợp.

Không thêm adapter, factory case, config field, installer hoặc default.
`internbridge.New(port)` vẫn là constructor thư viện: port khác 0, host cố
định `127.0.0.1`, không override URL/proxy/redirect/credential. Port ví dụ
`8765` chưa phải default runtime đăng ký. Bản triển khai sau phải dùng tên
`intern`, config loopback cố định có chủ sở hữu, không ủy quyền Hermes/OpenClaw.
Tính năng tùy chọn không hỗ trợ có thể trả lỗi rõ ràng; không nhúng interface
nil, panic, bỏ ảnh âm thầm hoặc giả thành công session/readiness.

Bước tiếp theo: xác lập trusted admission và hợp đồng capability/activation
chỉ văn bản; sau đó chứng minh selection, correlation request/result, lỗi
offline/protocol/custody và không có tác động thiết bị/channel trước đăng ký
interface đầy đủ. Dừng tại ranh giới adapter lớn hơn theo yêu cầu, không cần
truy cập thiết bị để review. Xem
[bằng chứng local](../../receipts/intern-welcome-desk-audit-2026-09-14.md).

## Đánh giá full-brain trước đó

Kiểm tra ngày 2026-09-14 trên nhánh `codex/gus-runtime-contract-20260914`,
base `8254e8a14`, [PR #406](https://github.com/autonomous-ai/autonomous-os/pull/406),
toàn bộ interface `system/domain/agent.go` và
[hướng dẫn runtime](adding-agent-runtime_vi.md). Producer được kiểm tra tại
project-spider-man commit `4f3e3c3606420db7bd4e72c8de3d68d5486f56c5`,
`packages/agents/src/gus/agents/{intern,intern_bridge}.py`.

Cập nhật tương thích: client hiện ghim PR #148 commit
`753b661087ee2ae1e4718babc2da618740cb52fa`, version `0.2.0`, schema
`cassi-first.v1`; xem [hợp đồng client hiện tại](intern-bridge-client_vi.md).
Phần dưới ghi lại kiểm toán đăng ký trước đó. Metadata reception mới không
thêm thực thi nhân viên, session, truy cập thiết bị hay đăng ký gateway.
Provider tùy chọn upstream cũng có nghĩa probe không chứng minh inference local.

## Kết luận

Có thể tái sử dụng transport, nhưng bridge hiện tại chưa đáp ứng một runtime
Intern đầy đủ. `InternHarness.__call__` chỉ định tuyến/phân loại/soạn văn bản
từng lượt, không memory nhân viên, tools hay thực thi hành động. Request chỉ
có text, operation, data class và run ID. Đăng ký factory ngay sẽ đưa tính năng
thiếu ra cho caller thật. Đây là thiếu hợp đồng/triển khai, không phải thiếu
quyền truy cập thiết bị.

Slice này thêm hai tiền đề trong `system/lib/internbridge`: chặn
unknown/restricted/secret trước mạng cho mọi operation và
`Client.ProbeGeneration` dùng prompt public cố định, deadline chung, lỗi rõ
ràng. Không tuyên bố đăng ký runtime.

## Các nhóm hợp đồng đã kiểm tra

[Bản kiểm toán tiếng Anh](../../agentic/intern-runtime-contract.md) liệt kê
đầy đủ tên phương thức theo từng nhóm. Kết quả tương ứng:

| Nhóm / file | Thiếu gì hoặc có thể triển khai thế nào |
|---|---|
| Text/slash: `SendChatMessage`, `SendSystemChatMessage`, các biến thể `WithRun` | Chữ ký không mang custody đáng tin. `system/server/sensing/delivery/http/handler.go` gửi chuỗi và queue ảnh. Cần policy theo input, bảo toàn khi queue/replay; unknown phải bị chặn. Không suy ra class từ prompt/channel/model. Slash command cần ngữ nghĩa riêng. |
| Ảnh: `SendChatMessageWithImages`, `SendChatMessageWithImagesAndRun`, `SendSlashCommandWithImagesAndRun` | `_validate_request` trong `intern_bridge.py` không có attachments; `LocalModel.complete` trong `intern.py` chỉ gửi text. Phải bổ sung producer/model và custody ảnh; không bỏ ảnh âm thầm. Trả unsupported là trung thực nhưng chưa đạt core contract. |
| Event, busy, queue, run ID, tất cả marker và pending trace | Có thể triển khai trong Go với state đồng bộ, queue giới hạn và lifecycle thật. HTTP completion không tự trở thành OS event. Bridge hash run ID; cần giữ mapping trace. Timeout không chứng minh remote đã hủy và không được retry tự động. |
| `Name`, `Version`, `IsReady`, `ConnectedAt`, `AgentUptime` | `/ready` là metadata tĩnh. Probe generation mới chỉ tăng bằng chứng tại thời điểm gọi. Cần state readiness thật; uptime giữ 0 khi chưa biết theo interface. |
| Session/history/rotation/compaction | Producer không có session/history/context. Lưu key cục bộ không tạo hội thoại model. Cần storage có chủ sở hữu, consumption, rotation và reset. Compaction/history không hỗ trợ có thể trả `ErrNotSupportedByRuntime`, không giả thành công. |
| Setup/onboarding/reset/restart/config | Producer CLI chỉ nhận host/port; chưa có workspace/config device có thể quản lý. Cần installation/config thật, không xóa thư mục đoán hoặc báo restart giả. |
| Identity, skill watcher và cả tám skill methods | Producer không đọc persona/skill files. Cần slot thật được tiêu thụ rồi dùng helper `system/skills` và capability gate. Copy file đơn thuần là dữ liệu chết. Watcher không trả lỗi không tự nó là blocker. |
| Model sync/update/watch/refresh | `LocalModel` đã ghim chỉ gọi IPv4 loopback port 1234, model mặc định `qwen/qwen3-14b`, không proxy/redirect. CLI không nhận model config; response bỏ provider/usage. Cần config local-only thực sự được đọc và proof model availability, không cloud fallback. Unsupported update chỉ hợp lệ khi onboarding/presync thật sự áp dụng config. |
| MCP | Không có consumer tools/MCP. Nếu chủ ý loại trừ, trả `ErrNotSupportedByRuntime`; không lưu config vô dụng rồi báo thành công. |
| Channels và delivery | Có thể công bố không hỗ trợ channel; mutation/delivery phải trả lỗi và pairing phải emit failure. Token/targets/session trống chỉ đúng với capability đó; không cần nhập credentials. |
| HAL/TTS/volume/voice | Có thể dùng helper HAL hiện có, giữ phân biệt accepted và played. Không gọi phần cứng trong công việc này. |

## Điều kiện đăng ký và activation

Sau khi hợp đồng trên tồn tại, thêm constant/list vào `system/domain/device.go`,
factory/transport vào `system/agent/factory.go`, và `runtimes/intern.Service`
đầy đủ với compile-time assertion.

`runtimes/intern/install.go` nhúng `install.sh` cần nguồn package đã ghim,
cài đặt tái lập được, service unit và verify executable nhẹ. `presync.sh`
nhúng phải khôi phục config/skills mỗi lần switch qua
`runtimereg.Register`/`RegisterPresync`. `system/device/runtime_installers.go`
và `switch_runtime.sh` đã có cơ chế; không cần switcher mới.
`RegisterReadiness` chỉ chạy khi caller yêu cầu. Activation tương lai phải yêu
cầu readiness, không chỉ systemd active; tính đến retry loop trước khi dùng
probe tiêu thụ inference.

Chỉ thêm `system/agent/migrate_persona/runtime_intern.go` và đăng ký trong
`migrator.go` khi runtime thực sự tiêu thụ slot persona/memory tương ứng.
Cần `read`, `write`, `personaPaths`, `userProfilePath`, kiểm tra round-trip và
reset/re-presync. Thư mục đoán sẽ tạo migration thành công giả và mất dữ liệu.
Hook cần gắn vào turn thật của OS và capability gate chung.

## Giới hạn proof

HTTP server giả chứng minh zero request cho dữ liệu bị giữ, positive control
cho dữ liệu được phép, phân biệt generation/metadata, lỗi giới hạn/an toàn,
không retry/cache thành công. Không chứng minh model đã cài, provider thật,
lifecycle runtime đang chạy hay readiness thiết bị.
Xem [biên bản kiểm chứng](../../receipts/intern-runtime-contract-2026-09-14.md).
