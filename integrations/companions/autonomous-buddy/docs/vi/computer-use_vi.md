# Computer use trên Mac đã ghép đôi

Computer use cho phép agent chạy trên thiết bị Autonomous hoàn thành tác vụ trong các ứng dụng trên Mac của người dùng thông qua Buddy. Phạm vi gồm app native, trình duyệt, giao diện tùy biến và tác vụ xuyên app. Agent management, phần quản lý project và phiên CLI local, là chức năng riêng.

## Quyền sở hữu tác vụ và vòng thực thi

```text
Mục tiêu người dùng → agent trên device + skill computer-use
                   → HTTP API OS trên device → WebSocket đã ghép đôi → Buddy Swift trên Mac
                   ← kết quả lệnh / quan sát Accessibility / screenshot
                   → quan sát, thao tác, kiểm chứng, tiếp tục đến kết quả yêu cầu
```

Runtime trên device chịu trách nhiệm suy luận, ngữ cảnh, hỏi thông tin còn thiếu và xác định hoàn thành. Buddy cung cấp thực thi native và quan sát; không tự lập kế hoạch workflow. `skills/computer-use/SKILL.md` chọn lệnh đồng bộ cho mọi tác vụ cần thông tin trả về, thao tác phụ thuộc hoặc kiểm chứng. Mở URL chỉ hoàn thành yêu cầu mở URL; tìm phòng phải tiếp tục lấy tham số, tìm kiếm và đọc kết quả thực tế.

`scripts/buddy.py` trong skill đã cài gọi `http://127.0.0.1:5000/api/buddy/command` trên **device**, kiểm tra cả envelope OS lẫn kết quả Buddy và trả JSON của lệnh khớp ID. Helper tạo ID lệnh duy nhất và không tự retry. `--params-file` nhận file JSON để tránh chèn nội dung người dùng hoặc màn hình vào shell. Timeout helper mặc định 15000 ms, cho phép 500–60000 ms và thêm 10 giây chờ phản hồi HTTP. Lệnh native không có timeout chỉ định dùng mặc định 5000 ms. Đây là thời hạn từng lệnh, không giới hạn toàn bộ tác vụ.

HW marker inline `/buddy/exec/<action>` vẫn dùng được cho thao tác đơn giản, độc lập. Kết quả marker không quay lại vòng suy luận của model. Không dùng marker cho quan sát hoặc chuỗi thao tác phụ thuộc.

## Quan sát native và thao tác theo reference

`get_ui_tree` nhận tùy chọn `app` (tên app đang chạy hoặc bundle ID), `max_nodes` (1–500, mặc định 150) và `max_depth` (1–30, mặc định 12). Khi không có `app`, lệnh quan sát ứng dụng foreground. Kết quả gồm `snapshot_id`, `app`, `bundle_id`, `pid`, `frontmost`, `truncated`, `expires_in_ms` và danh sách phẳng `nodes`.

Node gồm `ref`, `parent_ref` nếu có, `role`, `actions` hỗ trợ, `secure` và các trường có sẵn: `title`, `description`, `value`, `help`, `enabled`, `focused`, `bounds_global_points`. Bounds là `{x,y,width,height}` theo point desktop toàn cục. Mỗi thuộc tính văn bản giới hạn 500 ký tự. Control bảo mật và node con không lộ văn bản/giá trị. Duyệt cây đọc số node con có giới hạn, dùng ngân sách duyệt 3 giây và timeout AX messaging 0.2 giây mỗi element. Cây bị cắt hoặc thiếu nội dung không chứng minh control không tồn tại; cần quan sát khác hoặc screenshot.

`perform_ui_action` yêu cầu `snapshot_id`, `ref` và `ui_action`:

| Action | Hành vi |
|---|---|
| `press` | Gọi AXPress nếu element đã quan sát hỗ trợ. |
| `focus` | Đặt AXFocused nếu element hỗ trợ. |
| `set_value` | Đặt AXValue bằng chuỗi `value`, tối đa 20000 ký tự, nếu hỗ trợ; không áp dụng cho trường bảo mật. |

Reference chỉ tồn tại trong process Buddy. Hết hiệu lực sau 30 giây, quan sát mới, lệnh thay đổi khác, hoặc lần thử thao tác reference sau khi xác thực. Process được quan sát phải vẫn ở foreground. Buddy kiểm tra lại PID, role, title, trạng thái enabled và action hỗ trợ. Quan sát mới sau mỗi action hoặc lỗi. Reference giảm nhầm lẫn nhưng không biến UI đang thay đổi thành giao dịch nguyên tử; agent vẫn phải kiểm chứng kết quả. Đặt giá trị không nhất thiết submit form hoặc kích hoạt mọi sự kiện riêng của app.

## Screenshot và input

`list_displays` trả ID màn hình, gốc/kích thước theo point toàn cục, kích thước backing pixel và scale. `screenshot` nhận `display_id` đang hoạt động, `scale` từ 0.01 đến 1 (mặc định 1), `return_format` là `path`, `base64` hoặc `both` (mặc định `path`). Kích thước ảnh đầu ra phải từ 1–16384 pixel mỗi trục và không quá 40 triệu pixel. Ảnh JPEG có tên duy nhất trong `~/Library/Application Support/AutonomousBuddy/screenshots/`; Buddy giữ 20 ảnh do chức năng này tạo gần nhất.

Helper trên device yêu cầu base64, kiểm tra/giải mã phản hồi và lưu JPEG duy nhất cùng metadata hình học trên device. Kết quả có `local_image_path`, `metadata_path`; đường dẫn Mac chỉ là `mac_image_path`. Runtime phải tải `local_image_path` bằng tool trả nội dung ảnh thực cho model. JSON hoặc base64 được in ra không tự tạo khả năng nhìn. Với runtime chỉ nhận văn bản, dùng vision phụ trợ bên dưới; nếu cả hai đường đều không có, dùng Accessibility khi đủ hoặc báo rõ thiếu khả năng nhận ảnh. Ảnh trên device được dọn riêng theo tác vụ; cơ chế giữ 20 ảnh trên Mac không dọn file trên device.

Input chuột dùng **point** CGEvent toàn cục, gốc phía trên bên trái và có thể âm ở màn hình phụ. Dùng kích thước mã hóa thực của từng screenshot cùng `image_to_global_points`:

```text
global_x = origin_x + image_x * scale_x
global_y = origin_y + image_y * scale_y
```

Tính cả việc image viewer resize ảnh trước khi dùng tọa độ. Chụp lại và cập nhật hình học sau thay đổi bố trí màn hình. Không ghép ảnh này với transform của ảnh khác.

| Input | Giới hạn xác thực |
|---|---|
| Tọa độ chuột | Số hữu hạn từ -1000000 đến 1000000; từ chối boolean. |
| `click_at` | `button`: left/right/middle; `clicks`: số nguyên 1–3, mặc định 1. |
| `scroll` | `delta_x`/`delta_y` nguyên: -100000 đến 100000; đặt vị trí con trỏ tùy chọn phải có cả `x` và `y`. |
| `drag` | Object point `from`/`to`; `duration_ms`: số nguyên 50–10000, mặc định 300. |
| `type_text` | Tối đa 100000 đơn vị UTF-16; `delay_ms`: số nguyên 0–1000, mặc định 15. |
| `key_combo` | Tối đa sáu phím, đúng một phím được hỗ trợ không phải modifier. |

Gõ, di chuyển mượt, click lặp và kéo đều kiểm tra cancellation giữa các sự kiện. Kéo bị ngắt nhả chuột ở vị trí đã gửi cuối cùng. Input đã gửi vẫn có thể làm app thay đổi.

## Vision phụ trợ cho runtime chỉ nhận văn bản

`POST /api/buddy/observe` chỉ cho gọi local trên device, nhận `question` (bắt buộc, 1–2000 ký tự Unicode), `display_id` uint32 dương tùy chọn và `scale` tùy chọn (0.01–1, mặc định 0.5). Helper cung cấp lệnh `buddy.py observe --question 'What is visible and where is the search field?'`. Endpoint chụp Mac đã ghép đôi một lần với timeout screenshot native 15000 ms, sau đó gọi image model phụ trợ đã cấu hình bằng prompt riêng cho desktop. Chụp và mô tả cùng theo cancellation của caller và ngân sách tổng 80 giây; helper chờ tối đa 90 giây. Không tự retry.

Phản hồi dùng envelope OS chuẩn với `data: {description, screenshot}`. Metadata screenshot giữ kích thước ảnh và transform tọa độ; không có base64. Server kiểm tra ID phản hồi khớp, native thành công, MIME/header JPEG, kích thước khớp metadata, payload ảnh tối đa 12 MiB và giới hạn kích thước hiện có trước khi gửi ảnh tới model đã cấu hình. JSON request giới hạn 16 KiB. Input sai trả HTTP 400; lỗi chụp/vision trả 502, hết thời hạn trả 504, kèm envelope lỗi chuẩn.

Đây là bằng chứng hình ảnh do model mô tả, không phải agent văn bản tự nhìn trực tiếp và không thực thi thao tác. Hỏi tập trung về control/văn bản nhìn thấy, yêu cầu tâm theo pixel ảnh khi cần; đổi tọa độ bằng transform screenshot trả về. Nêu rõ bất định và quan sát lại sau thao tác. Screenshot được gửi tới vision provider đã cấu hình trên device, dùng cùng catalog/cấu hình model phụ trợ với chức năng mô tả ảnh hiện có. Prompt không gọi đây là ảnh camera của device.

## Quyền, đồng thời và khôi phục

`desktop_info` là preflight chỉ đọc, trả `protocol_version: 2`, `paused`, `accessibility`, `screen_recording`, `frontmost_app`, capabilities và tối đa 100 app đang chạy với `pid`, `name`, `bundle_id`, `active`; `apps_truncated` cho biết có mục bị lược bỏ. Lệnh không bật prompt quyền. Lệnh hoạt động khi paused nhưng vẫn từ chối công việc chồng lấn bằng busy.

Cấp quyền Accessibility cho app Buddy đang chạy để dùng AX và bàn phím/chuột, cùng quyền Screen Recording để chụp màn hình trong macOS System Settings → Privacy & Security. Cần ghép đôi và WebSocket đang kết nối. Thiếu quyền, Buddy mất kết nối, Buddy tạm dừng và AX không có nội dung là các kết quả khác nhau; lặp input không sửa được việc thiếu quyền.

Dispatcher native cho phép một lệnh thông thường đang chạy và từ chối lệnh chồng lấn bằng lỗi busy. `cancel_command` với `{"id":"<active-command-id>"}` được xử lý trong khi lệnh đó chạy. Kết quả `cancel_requested` nghĩa là đã yêu cầu dừng lệnh đang chạy khớp ID, không có nghĩa hoàn tác tác động trước đó. Pause local hủy tác vụ đang chạy và từ chối lệnh thông thường mới. Resume không tự tiếp tục tác vụ người dùng cũ.

OS tuần tự hóa ghi WebSocket. Phản hồi khớp với connection và request gốc; reconnect làm tác vụ đang chờ thất bại thay vì cho connection thay thế trả lời request cũ. HTTP bị hủy/timeout dẫn đến cố gắng hủy lệnh native tương ứng. Cancellation mang tính hợp tác; lời gọi OS đồng bộ có thể cần thời gian để trả về.

Timeout, mất kết nối hoặc cancellation có thể để lại tác động một phần. Không tự retry thao tác có hệ quả. Quan sát lại app, xác định điều đã xảy ra rồi mới chọn bước tiếp. Phản hồi native thành công nghĩa là lệnh đã thực thi; không chứng minh thành công theo mục tiêu như kết quả tìm kiếm đúng, nội dung đã lưu hoặc toàn bộ workflow đã hoàn thành.

## Checklist nghiệm thu thủ công

Đây là kịch bản nghiệm thu, **không phải bản ghi đã chạy live thành công**. Dùng file thử có thể xóa và thao tác được cho phép; ghi runtime/model, build Buddy, trạng thái quyền, hình học màn hình, lệnh, quan sát và bằng chứng cuối.

- **App native:** tạo/sửa ghi chú hoặc tài liệu thử; quan sát UI bằng AX, chọn control theo ngữ cảnh và kiểm chứng nội dung cuối cùng cùng vị trí lưu.
- **Trình duyệt:** mở trang tìm kiếm được yêu cầu, lấy điểm đến/ngày hoặc tham số tương đương còn thiếu, submit tìm kiếm thật và báo kết quả đã đọc. Chỉ mở trang là chưa đạt.
- **Xuyên app:** đọc dữ liệu mẫu đã biết trong app thứ nhất, chuyển tóm tắt sang app khác, quay lại khi câu trả lời làm thay đổi yêu cầu và kiểm chứng nội dung đích.
- **UI tùy biến:** thao tác canvas hoặc control không có AX hữu ích bằng screenshot thực và input con trỏ; kiểm chứng kết quả nhìn thấy.
- **Nhiều màn hình/Retina:** lặp thao tác với ảnh scale đầy đủ và giảm trên màn hình chính/phụ, gồm gốc âm; kiểm chứng đúng control nhận input.
- **Reference cũ:** đổi focus, chờ quá 30 giây hoặc dùng lại snapshot đã tiêu thụ; kiểm tra từ chối và khôi phục bằng quan sát mới.
- **Quyền/mất kết nối:** thu hồi quyền hoặc ngắt Buddy; kiểm tra lỗi cụ thể và không báo thành công tưởng tượng. Kết nối lại rồi quan sát trước khi tiếp tục.
- **Cancellation:** dừng gõ/kéo dài bằng cancel và Pause local; xác nhận không kẹt nút chuột, quan sát tác động một phần và không retry mù.
- **Đồng thời:** gửi lệnh chồng lấn; kiểm tra busy và input không xen kẽ. Reconnect khi đang chờ lệnh và kiểm tra phản hồi cũ không thể đáp ứng request mới.
- **Hoàn thành toàn tác vụ:** đưa vào câu hỏi làm rõ, tải chậm và dialog bất ngờ. Kiểm tra agent giữ mục tiêu, đổi cách sau lỗi lặp và báo hoàn thành có bằng chứng hoặc blocker còn lại cụ thể.

Helper trên device tạo thư mục task riêng và trả `capture_dir`; dùng lại qua `--output-dir` khi chụp tiếp. Mỗi thư mục giữ tối đa 50 cặp ảnh/metadata thuộc helper, khác với giới hạn 20 ảnh trên Mac. Thư mục task bị bỏ dở cần được dọn chủ động.

## Mở ứng dụng và focus

`open_app` yêu cầu activate một lần và trả `pid`, `bundle_id`, `app`, `activation_requested`, `frontmost`, `requires_observation`. `open_url` trả `opened`, `browser`, các cờ activation/observation và metadata app/focus khi xác định được handler đang chạy. `frontmost` mô tả process foreground lúc trả lời, không đảm bảo cửa sổ đích đang hiện trên màn hình được chụp. App có thể còn tải, có dialog hoặc người dùng đã chuyển app. Quan sát cửa sổ/màn hình đích trước khi gõ.

Chọn trình duyệt tường minh hỗ trợ alias Chrome, Safari, Firefox, Arc, Edge và Brave. Trình duyệt được yêu cầu không được hỗ trợ hoặc chưa cài sẽ báo lỗi; Buddy không âm thầm thay bằng trình duyệt mặc định. Khi không có `browser` hoặc dùng `browser: "default"`, Buddy dùng handler URL đã đăng ký và kiểm tra macOS có chấp nhận yêu cầu mở hay không. Bị từ chối là lỗi. `opened: true` xác nhận yêu cầu được chấp nhận, không xác nhận trang đã sẵn sàng hoặc workflow đã hoàn thành. Mở app không liên tục giành lại focus từ người dùng.

### Lời nói tự nhiên, câu tiếp nối và nhiều màn hình

Người dùng có thể nói “Mở Airbnb tìm chỗ ở Đà Nẵng giúp mình” mà không cần nhắc Buddy hay công cụ. Skill computer-use giữ toàn bộ mục tiêu tìm kiếm và thông tin còn thiếu qua câu trả lời ngắn như “cuối tuần này, hai người”. Skill giữ địa điểm và số khách đã biết, xác định ngày tương đối từ ngày hiện tại/múi giờ đáng tin cậy, đồng thời hỏi ngày nhận và trả phòng cụ thể nếu “cuối tuần” còn mơ hồ. Câu sửa đổi cập nhật tác vụ đang chờ; yêu cầu dừng hủy tác vụ. Cách xử lý này cũng áp dụng cho app native: “ghi vào Notes” tạo và kiểm tra ghi chú, còn “đổi tên nó” chỉ trỏ đến đối tượng đã xác định trước đó khi tham chiếu rõ ràng. Thao tác Finder tác động lên Mac, không phải hệ thống file của device.

Trước khi đọc app đích bằng hình ảnh, agent liệt kê màn hình. `is_main` đánh dấu màn hình chính, không xác định cửa sổ của app đang hoạt động nằm ở đâu. Nếu có, `bounds_global_points` của cửa sổ từ Accessibility được đối chiếu với hình chữ nhật của từng màn hình. Nếu không, agent kiểm tra mỗi màn hình có khả năng chứa app một lần bằng `display_id` cụ thể cho đến khi tìm được mục tiêu. Agent giữ ID màn hình đó và phép chuyển tọa độ của ảnh mới nhất, rồi tìm lại nếu mục tiêu biến mất hoặc bố trí màn hình thay đổi. App khác xuất hiện trên màn hình chính không chứng minh app được yêu cầu mở thất bại. Agent không di chuyển cửa sổ chỉ để dễ quan sát.

Các ca đánh giá lời nói tự nhiên trong `skills/computer-use/evals/natural-voice.json` bao gồm câu tiếp nối tìm chỗ ở, sửa yêu cầu, Notes, Finder, tóm tắt xuyên app, hủy tác vụ và tình huống ba màn hình: Buddy ở màn hình chính 1, kết quả Chrome ở màn hình 4, app khác ở màn hình 5. Các ID này thuộc tình huống kiểm thử, không phải bố trí cố định. Việc định nghĩa ca đánh giá và kiểm tra cú pháp không chứng minh đã đạt kiểm thử trên device thật.

Yêu cầu ngắn mở hoặc thao tác website/app nhắm tới Mac đã pair ngay cả khi người dùng không nói “Mac” hay “máy tính”. Mô tả kích hoạt skill bao gồm các câu nói này. Browser cài trên device không có desktop không thay thế máy tính của người dùng; việc chỉ tra cứu thông tin vẫn là luồng riêng.

Skill hướng dẫn agent đối chiếu tham số tìm kiếm hoặc nhập chữ với lời người dùng đã giữ lại trước khi gửi, gồm địa danh và nội dung đọc để ghi; lệnh thành công với địa điểm bị thay thế vẫn là tác vụ thất bại.

## Mở file và thư mục trên Mac

`open_path` giải quyết `path` filesystem trên Mac. Dùng `{"path":"~/Downloads"}` để mở Downloads mà không cần biết username Mac. Chấp nhận đường dẫn tuyệt đối, `~` hoặc bắt đầu bằng `~/`; từ chối đường dẫn tương đối, `~otheruser`, chuỗi URL, ký tự NUL, đích không tồn tại và đường dẫn quá 16384 byte UTF-8. Mở rộng home dùng thư mục home của process Mac, không dùng home trên device. Hỗ trợ file/thư mục đã tồn tại; executor không tạo hoặc sửa nội dung filesystem.

`mode` tùy chọn là `open` (mặc định) hoặc `reveal`. Mở dùng app đã đăng ký; `app` tùy chọn chọn app đã cài theo tên hoặc bundle ID, ví dụ `{"path":"~/Documents/draft.txt","app":"TextEdit"}`. App chỉ định không có sẽ báo lỗi, không fallback. `{"path":"~/Downloads/report.pdf","mode":"reveal"}` yêu cầu Finder chọn item; không kết hợp `app` với `reveal`. Các thao tác dùng NSWorkspace, không cần Accessibility hoặc AppleScript.

Kết quả có `path` đã giải quyết, `is_directory`, `mode`, `opened` hoặc `reveal_requested` cùng metadata activation/observation. Reveal là yêu cầu vì API Finder không trả xác nhận theo mục tiêu. Mở file có thể khởi chạy app liên kết; kiểm chứng cửa sổ/nội dung đích trước khi báo hoàn thành hoặc gõ. `desktop_info.capabilities` công bố `open_path` trên build Buddy hỗ trợ.

## Giới hạn input bàn phím theo ứng dụng

Trên build công bố `target_app_input` trong `desktop_info.capabilities`, `type_text` và `key_combo` nhận `app` tùy chọn (tên hoặc bundle ID). Với workflow nhắm app cụ thể, cần dùng trường này: `{"keys":["cmd","n"],"app":"Notes"}` hoặc `{"text":"Draft summary","app":"com.apple.Notes"}`. Buddy từ chối trước khi gửi input nếu app đó không ở foreground. Buddy giữ process ID foreground và kiểm tra lại ngay trước từng cặp key-down/key-up hoàn chỉnh; đang gõ sẽ dừng nếu focus đổi giữa các ký tự. Không tự activate hoặc chuyển cửa sổ để giành lại focus.

Cặp phím được gửi không có suspension ở giữa, nên cancellation không chủ động để phím bị giữ. Input đã gửi không thể hoàn tác; kiểm tra foreground không khóa focus OS nguyên tử hoặc xác định đúng ô nhập trong cùng app. Kiểm chứng cửa sổ/ô nhập trước và quan sát tác động một phần sau lỗi focus; không retry mù toàn bộ văn bản. Bỏ `app` giữ tương thích cho yêu cầu tường minh gõ vào ô đang focus. Build cũ có thể bỏ qua tham số lạ, nên kiểm tra capability trước khi dựa vào cơ chế này.
