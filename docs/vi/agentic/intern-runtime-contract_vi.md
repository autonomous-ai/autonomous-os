# Đánh giá đăng ký runtime Intern

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
