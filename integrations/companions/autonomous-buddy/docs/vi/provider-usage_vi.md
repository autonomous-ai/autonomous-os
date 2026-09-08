# Usage của provider

Footer desktop hiển thị usage subscription do Claude và Codex trả về. Mỗi window có dữ liệu hiển thị phần trăm đã dùng và thời gian tới reset; không biến dữ liệu thiếu thành 0 giả. Footer đọc khi mount và mỗi 60 giây, có refresh thủ công, không chạy chồng request.

## Contract

Preload đã validate cung cấp `providerUsage(refresh?: boolean)`, trả `ProviderUsage[]`: `provider` (`claude` hoặc `codex`), `state` (`ready`, `unavailable`, `error`), `message` tùy chọn, `windows` (`label`, `usedPercent`, `resetsAt` tùy chọn), và `updatedAt`. Timestamp là Unix milliseconds. Backend `ProviderUsageService.read(force)` cache 60 giây và gộp request đồng thời; `dispose()` hủy default probe đang chạy. Backend hiện trả `unavailable` cùng danh sách window rỗng khi lỗi. Frontend cũng xử lý state error.

## Nguồn và boundary

- **Codex:** chạy CLI đã cài bằng `app-server`, initialize protocol stdio riêng rồi chỉ gọi `account/rateLimits/read`. Không tạo model turn. Chọn bucket `codex` nếu có, nếu không dùng `rateLimits`; label primary/secondary lấy từ duration provider khai báo. Timeout 10 giây, tổng stdout tối đa 1 MiB. Shutdown đóng stdin, gửi SIGTERM, tăng lên SIGKILL sau 500 milliseconds nếu cần.
- **Claude:** đọc Keychain item macOS `Claude Code-credentials` hiện có, thêm hash config directory nếu đặt `CLAUDE_CONFIG_DIR`. Keychain timeout 3 giây; bị từ chối thì unavailable. Ngoài macOS, reader có thể đọc `.credentials.json` hiện có trong Claude config directory, giới hạn 64 KiB. Reader này không chứng minh app đã hỗ trợ đóng gói Windows/Linux. Không refresh, khôi phục, ghi lại hoặc trả credential ra UI.
- Claude GET chỉ đọc tới endpoint cố định `https://api.anthropic.com/api/oauth/usage` với bearer token hiện có và OAuth beta header. Từ chối redirect. Toàn probe có abort signal 10 giây; response tối đa 64 KiB. Đây là compatibility integration, không phải API public được bảo đảm ổn định; thay đổi provider hoặc loại account không hỗ trợ có thể khiến usage unavailable.
- Claude lấy window `five_hour`, `seven_day` thật. Entry `weekly_scoped` tùy chọn trong `limits` dùng model display name và percent, gồm Fable. Chúng được ưu tiên hơn alias Fable theo thứ tự `fable_weekly`, `fable_seven_day`, `seven_day_fable`. Bỏ qua `fable` đơn lẻ vì duration không rõ. Percent phải hữu hạn, trong khoảng 0–100. Thiếu field thì giữ thiếu; reset nhận chuỗi ISO hoặc epoch seconds, gồm phần thập phân, rồi chuyển thành milliseconds nguyên.

Credential thô, provider body và CLI diagnostic không xuất hiện trong lỗi trả về. Không suy quota subscription từ token count hoặc chi phí USD. `BUDDY_NATIVE_TEST_MODE=1` dùng fixture xác định trong IPC parent thay vì probe account thật. Unit test inject fixture credential/fetch/CLI, không cần model call trả phí. Test pass không thay thế kiểm tra quyền truy cập account và compatibility provider.

## Tham khảo

- [OpenAI Codex App Server](https://learn.chatgpt.com/docs/app-server): initialization và account rate-limit RPC.
- [Claude Code status line](https://code.claude.com/docs/en/statusline): contract chính thức cho usage/reset có cấu trúc.
- Orca audit tại `ba5f708290b72012132fb23a6f016b8fd5601718`: [OAuth request](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/rate-limits/claude-oauth-usage-request.ts), [credential lookup](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/claude-accounts/keychain.ts), [Fable fixtures](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/rate-limits/claude-fetcher-fable-usage.test.ts). Adapter Buddy được viết mới; các nguồn này dùng để xác định compatibility contract.
