# Lamp Buddy — sign + notarize cho production

Doc handover cho dev sẽ làm Apple Developer enrolment. Sau khi setup 1 lần xong, mỗi release chỉ cần:

```bash
cd lumi-buddy
export DEV_ID_APP="Developer ID Application: <Your Org> (<TEAMID>)"
export NOTARY_PROFILE=lamp-notary
make dmg-signed
```

Output `dist/LumiBuddy-<version>.dmg` được sign + notarize + staple — user mount, drag app vào Applications, double-click, macOS mở luôn không có cảnh báo Gatekeeper hay phải right-click → Open.

Đường ad-hoc `make dmg` cũ vẫn dùng được cho local build và share nội bộ; doc này chỉ cover đường production.

## Khác biệt vs build ad-hoc

| | Ad-hoc (`make dmg`) | Production (`make dmg-signed`) |
|---|---|---|
| Signing identity | Không (`-` placeholder) | Developer ID Application cert từ Apple |
| Hardened runtime | Off | **On** (`--options runtime`, Apple bắt buộc) |
| Secure timestamp | Không | **Có** (`--timestamp`) |
| Apple notarize | Không | **Có** (notarytool submit + wait) |
| Stapler ticket | Không | **Có** (`stapler staple`) — verify offline được |
| UX lần đầu mở | Right-click → Open Anyway | Double-click bình thường |
| TCC reset khi rebuild | Mỗi build (cdhash đổi) | Ổn định qua các bản (cert identifier ổn định) |
| User permission grants | Re-grant Accessibility + Screen Recording mỗi release | Grant 1 lần, giữ qua các release |

TCC ổn định là win lớn nhất — không có nó, mỗi lần tester nhận build mới phải bấm lại ~3 dialog permission.

## Setup 1 lần (dev giữ cert làm)

### 1. Đăng ký Apple Developer Program

`https://developer.apple.com/programs/enroll/` — $99/năm. Cá nhân hay tổ chức đều OK; tổ chức tốt hơn vì cert không bị buộc vào 1 Apple ID cụ thể.

Sau khi enrol xong, ghi nhớ **Team ID** (chuỗi 10 ký tự alphanumeric, ví dụ `ABCDE12345`) — xem ở header trang Apple Developer account, cần dùng bên dưới.

### 2. Tạo cert Developer ID Application

Cách thẳng nhất là qua Xcode (download Xcode nếu chưa có):

1. Xcode → **Settings → Accounts** → "+" → đăng nhập với Apple ID đã enrol.
2. Chọn team → **Manage Certificates…**.
3. "+" → **Developer ID Application**. Xcode tự gen CSR + download `.cer` + cài private key vào login Keychain trong 1 bước.

Đường thủ công (không có Xcode) — chỉ khi không dùng được Xcode:

1. Keychain Access → **Certificate Assistant → Request a Certificate From a Certificate Authority…** → lưu CSR.
2. `https://developer.apple.com/account/resources/certificates` → "+" → **Developer ID Application** → upload CSR → download `.cer`.
3. Double-click `.cer` → cài vào login Keychain; private key đã tạo cùng CSR ở bước 1.

Verify cert dùng được:

```bash
security find-identity -v -p codesigning
```

Tìm dòng `Developer ID Application: <Your Org> (<TEAMID>)`. Chuỗi đầy đủ trong dấu nháy là cái dùng làm `DEV_ID_APP` khi gọi `make`.

### 3. Setup credentials cho notarytool

Notarize chạy với **app-specific password** do Apple cấp, không phải password iCloud. Tạo 1 lần:

1. `https://account.apple.com/account/manage` → Sign-In and Security → **App-Specific Passwords** → Generate (đặt nhãn `lamp-buddy-notary` hoặc tương tự).
2. Copy password (định dạng `xxxx-xxxx-xxxx-xxxx`).
3. Lưu bộ 3 (Apple ID, Team ID, app-specific password) vào Keychain để `notarytool` đọc không phải prompt:

```bash
xcrun notarytool store-credentials lamp-notary \
  --apple-id "apple-id-cua-ban@example.com" \
  --team-id "ABCDE12345" \
  --password "xxxx-xxxx-xxxx-xxxx"
```

`lamp-notary` là alias profile — pass nó qua `NOTARY_PROFILE` khi `make`. Đặt tên gì cũng được, miễn nhất quán.

Smoke test:

```bash
xcrun notarytool history --keychain-profile lamp-notary
```

History rỗng OK — nghĩa là auth chạy đúng.

## Flow mỗi release

```bash
cd lumi-buddy

# Lưu vào shell rc 1 lần, hoặc export mỗi session.
export DEV_ID_APP="Developer ID Application: Autonomous Inc (ABCDE12345)"
export NOTARY_PROFILE=lamp-notary

# Tuỳ chọn: bump VERSION trong Makefile nếu là release mới.

make dmg-signed
```

Make target chạy theo thứ tự:

1. `swift build -c release` — binary production.
2. Gen app icon nếu chưa có (`make icon` chain — SF Symbol placeholder trừ khi thay bằng PNG design thật).
3. Bundle `dist/LumiBuddy.app` với icon + Info.plist.
4. `codesign` app với Developer ID, hardened runtime, secure timestamp.
5. `hdiutil create` DMG (layout drag-to-Applications).
6. `codesign` DMG với Developer ID.
7. `xcrun notarytool submit … --wait` — upload lên Apple, block 1-5 phút chờ verdict.
8. `xcrun stapler staple` — embed ticket notarize vào DMG để Gatekeeper verify offline.
9. `spctl --assess` — Gatekeeper dry-run local, in `accepted` nếu pass.

Kết quả là `dist/LumiBuddy-<version>.dmg`. Gửi file này.

## Verify build trước khi ship

```bash
# 1. Chữ ký app hợp lệ.
codesign --verify --deep --strict --verbose=2 dist/LumiBuddy.app

# 2. Gatekeeper accept app.
spctl --assess --type execute --verbose=4 dist/LumiBuddy.app
#   expected: "accepted source=Developer ID notarized"

# 3. DMG có ticket được staple.
xcrun stapler validate dist/LumiBuddy-<version>.dmg
#   expected: "The validate action worked!"

# 4. Gatekeeper dry-run thật trên DMG.
spctl --assess --type open --context context:primary-signature --verbose=4 dist/LumiBuddy-<version>.dmg
#   expected: "accepted source=Notarized Developer ID"
```

Cả 4 phải pass trước khi upload đi đâu.

## Lỗi hay gặp

**`errSecInternalComponent` khi codesign.** Private key thiếu hoặc bị lock. Mở Keychain Access → login → tìm "Developer ID Application" → confirm có private key sibling. Nếu chỉ có cert, bạn import `.cer` ở máy khác máy gen CSR — làm lại bước 2 trên máy này.

**`Hardened Runtime is not enabled` trong notarytool log.** Thiếu flag `--options runtime`. Makefile target đã set; nếu bạn chạy `codesign` thủ công, re-sign với `--options runtime`.

**`The signature does not include a secure timestamp`.** Thiếu flag `--timestamp`. Tương tự — Makefile có sẵn; codesign thủ công cần thêm.

**Notarize status `Invalid` log có `disallowed-entitlement`.** Bạn dùng entitlement Apple không cho phép với Developer ID distribution. Buddy hiện không set entitlement nên không nên gặp. Nếu gặp, lấy full log:

```bash
xcrun notarytool log <submission-id> --keychain-profile lamp-notary
```

…và check entitlement nào bị reject.

**Notarize status `Accepted` nhưng Gatekeeper vẫn cảnh báo trên máy user.** DMG chưa staple. Hoặc chạy lại `make notarize` trên DMG hiện có (re-staple), hoặc build lại với `make dmg-signed`.

**User báo "app is damaged".** Thường là quarantine xattr đã set mà staple ticket thiếu/invalid. Bảo user chạy `xattr -d com.apple.quarantine /Applications/LumiBuddy.app` 1 lần; fix triệt để là ship DMG đã staple.

## Khi nào cần re-notarize

Phải notarize lại mọi lần binary .app hoặc DMG thay đổi — tức là mỗi release. Notarize bind với chính xác bits đó; không thể "chuyển" ticket từ build này sang build khác. Makefile xử lý tự động qua full chain.

Staple offline-capable nên user cài lần đầu khi offline vẫn được trải nghiệm không cảnh báo miễn DMG có ticket nhúng sẵn.

## Phần doc này CỐ Ý KHÔNG cover

- **Phát hành qua Mac App Store.** Cert khác (`Apple Distribution`), App Sandbox bắt buộc, flow submission riêng qua App Store Connect. Out of scope.
- **Sparkle / auto-update.** Buddy hiện không có update channel; release thủ công bằng drop DMG. Add Sparkle sau nếu release cadence tăng.
- **CI signing.** Làm được (GitHub Actions với cert + notarytool keychain profile encrypt làm secrets), nhưng handoff hiện tại giả định 1 dev sign local. Setup CI khi build cadence justify.
