# Cập nhật app

App macOS đã đóng gói kiểm tra cập nhật sau khi mở 30 giây, rồi mỗi 6 giờ. App tải bản mới trong nền và không ép khởi động lại ngay. Bản phát triển và native test mode không gửi request cập nhật qua mạng. Hiện chưa có tùy chọn bật/tắt kiểm tra hoặc tải tự động.

## Kiểm tra và cài đặt

**Check for Updates…** có trong menu bar Buddy và menu ứng dụng Electron ngay cả khi Agent Manager và Settings bị ẩn. Mục này không mở workspace.

| Nhãn menu | Hành vi |
| --- | --- |
| Check for Updates… | Kiểm tra ngay; thao tác thủ công báo đang dùng bản mới nhất hoặc lỗi bằng hộp thoại. |
| Checking for Updates… | Bị vô hiệu hóa trong lúc kiểm tra. |
| Downloading Update… | Bị vô hiệu hóa trong lúc tải. |
| Restart to Update… | Bản tải đã sẵn sàng; chọn Restart hoặc Later trong hộp thoại xác nhận. |
| Restarting… | Bị vô hiệu hóa trong lúc thoát và cài đặt. |

Khi tải tự động xong, app hiện desktop notification; nhấn notification mở xác nhận khởi động lại. Restart dừng session agent và điều khiển device native bằng cùng luồng cleanup khi Quit bình thường, rồi cài và mở lại app. Session không tự chạy lại. **Later** hoãn khởi động lại ngay; bản đã tải vẫn được cài ở lần thoát app bình thường tiếp theo. Pairing và dữ liệu app đã lưu giữ nguyên vị trí hiện có.

Buddy **0.0.21 trở xuống** chưa có updater: cần cài DMG mới thủ công một lần để nhận chức năng này. Việc thêm mã nguồn updater không tự phát hành OTA hoặc đổi version.

## Feed và đóng gói

Electron main process quản lý `autoUpdater` tích hợp sẵn (Squirrel.Mac), dùng feed JSON HTTPS tĩnh tại:

```text
https://storage.googleapis.com/s3-autonomous-upgrade-3/os/ota/autonomous-buddy/<arch>/latest.json
```

`<arch>` là `arm64` hoặc `x64`, khớp kiến trúc app đang chạy. Feed trỏ tới **ZIP** chứa app đã ký số và notarize để cài tự động. File **DMG** và metadata OTA chung theo kiến trúc tiếp tục cung cấp bản tải thủ công; DMG không phải payload tự cập nhật. Version app đóng gói đọc từ `VERSION_AUTONOMOUS_BUDDY`, nên so sánh cập nhật dùng version release thay vì version phát triển trong package desktop. Xem [ký số release](release-signing_vi.md) về xuất bản và thử lại.

Helper Swift embedded nhận request riêng `update_status` với `{label, enabled}` và gửi event menu `check-updates` nằm trong allowlist về Electron. Trạng thái cập nhật được giữ khi dựng lại menu native; không mở API updater cho renderer hay mở Agent Manager.

Unit test, build và smoke test local không chứng minh nâng cấp production thành công. Trước khi phân phối release đầu có updater, cần kiểm tra app đã ký và cài với release mới hơn đã ký trên đúng kiến trúc Mac, gồm khởi động lại, dừng helper và giữ pairing.

## Kiểm chứng local

Chạy riêng hai test runner desktop từ `desktop/`:

```bash
npx vitest run tests/*.test.ts
node --test tests/signing-identity.test.mjs scripts/test-update-feed.mjs scripts/test-update-permissions.mjs
```

Chạy regression test publisher từ thư mục gốc repository:

```bash
python3 scripts/release/tests/test_upload_autonomous_buddy.py
```

Lệnh `npm test` hiện cũng nạp file test Node qua Vitest và có thể lỗi do hai runner bị trộn từ trước; các lệnh tường minh bên trên tách riêng hai runner.

Để chủ động chạy smoke test cài thật bằng Squirrel, trước tiên chạy `make build` trong thư mục Buddy, rồi:

```bash
cd desktop
node tests/update-electron-smoke.mjs
```

Cần macOS và identity ký Developer ID dùng được. Test ký các bản sao app tạm với bundle ID riêng duy nhất, dùng feed test chỉ ở loopback và native test mode, rồi kiểm tra thay thế/mở lại thật cùng file đánh dấu dữ liệu được giữ nguyên. Test không thay Buddy đang cài hay dùng feed production. Lệnh này mô tả cách kiểm chứng, không khẳng định nâng cấp production đã đạt.
