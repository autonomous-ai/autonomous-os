# Rà soát skills — 2026-09-10

Phạm vi: đủ 28 entrypoint `skills/*/SKILL.md`, đối chiếu reference và implementation khi có phát hiện cụ thể. Đây là review source, không khẳng định đã test mọi runtime/phần cứng. Giữ ngưỡng event, đồng ý của user, định danh, capability và tiêu chí hoàn thành task.

## Đã sửa và kiểm tra local

- `computer-use`: biết Buddy không sẵn sàng thì dừng; chưa biết thì kiểm tra read-only một lần. Giữ kiểm tra quyền/focus/kết quả và đồng bộ vision reference; không thêm inject trạng thái từ runtime.
- `connectors`, `face-enroll`, `servo-control`, `wellbeing`: sửa description YAML lỗi. Rút ngắn description connectors nhưng giữ toàn bộ discovery, bảo vệ credential và xác nhận write trong body. Không đổi API/capability.
- `user-emotion-detection`: thay chỉ dẫn batch POST bash mâu thuẫn bằng log marker đã có. Giữ signal, decision và log của route; không ghi lại bằng shell.
- `wellbeing`: sửa tóm tắt bỏ log cho mọi reaction. Giữ `noted_yawn` để cooldown hoạt động và log morning/sleep/meal theo route; không ghi trùng activity backend đã log. Không đổi ngưỡng/thứ tự ưu tiên.
- `sensing`: nhánh sound ngắn đã commit trước đó; đã thấy occurrence 1 và 3 trên device với tiếng Anh. Không suy rộng sang mọi event/ngôn ngữ.

Kiểm chứng hiện tại: cả 28 entrypoint pass validator frontmatter của repo; `go test ./system/skills/` pass; 19 test Buddy helper pass. Test helper không chứng minh model tuân thủ prompt.

## Phát hiện và hướng xử lý

Đã hoàn tất lượt tuning có giới hạn. Ngoài các sửa đổi phía trên:

- Thống nhất camera/servo capture, template HW thực thi, giữ info display, xác nhận guard-off rõ ràng, bỏ chữ `(POST)` khỏi lời nói và giao stop LED effect cho HAL đã hỗ trợ sẵn.
- Ghi âm lưu WAV vào thư mục tạm riêng, chỉ báo thành công sau HTTP thành công, không retry khi kết quả chưa rõ. Audio action rõ ràng bỏ preflight chung tùy chọn; tăng/giảm âm lượng tương đối vẫn đọc volume hiện tại.
- Mood/music suggestion dùng context đã inject cùng signal mới. Habit dùng wellbeing marker một lần mỗi intent. Voice phân biệt speech tự động, speech bổ sung và privacy mà không đổi hành vi meeting.
- Lịch sử sensing chọn đủ ngày địa phương giao với khoảng thời gian, báo thiếu file thay vì suy ra không có event; sửa nhầm midnight/noon.

Bảng dưới lưu bằng chứng review, gồm cả hành vi chủ động giữ nguyên; không phải danh sách sửa đổi còn dang dở đã hứa trong audit này.

| Skill | Vấn đề / cơ hội | Cần giữ |
| --- | --- | --- |
| `camera`, `servo-control` | Rule cuối và ví dụ compound vẫn dùng raw snapshot cho câu hỏi thị giác, trái `/api/vision/look`; `system/server/vision.go` giải thích rủi ro model text-only đoán ảnh. | Reuse ảnh đã có, aim/hold trước look, vẫn cho xuất raw frame. |
| `led-control` | Endpoint solid/paint đã stop effect; marker stop trước đó là thừa. | Stop riêng, gradient, persistence, brightness. |
| `scene`, `led-control`, `servo-control`, `display` | Template cuối chỉ in nhãn, thiếu HW marker thực thi. | Tham số và ý nghĩa xác nhận hiện tại. |
| `display` | Info rồi eyes-mode ngay khiến text bị xóa; service chuyển mode tức thì. | Không tự nghĩ thêm API delay/duration. |
| `guard` | Bảng trigger cho “I'm back” tắt ngay, workflow trở về yêu cầu xác nhận. | Lệnh guard-off rõ ràng và choreography báo động. |
| `audio` | Ví dụ ghi âm đổ WAV nhị phân vào shell output. | Thời lượng user yêu cầu và xử lý lỗi. |
| `sensing-track` | Query lịch sử gần đây chỉ đọc hôm nay, bỏ sót qua nửa đêm; ví dụ midnight dùng 12pm. | Retention, phân biệt thiếu file. |
| `servo-tracking` | `(POST)` sau marker trong ví dụ có thể bị đọc ra. | Aim/track/stop và JSON. |
| `mood` | Yêu cầu đọc history ngay mâu thuẫn context có sẵn và marker-only. | Tổng hợp gồm signal hiện tại; fallback khi thiếu context. |
| `habit` | Log intent dùng curl dù đã hỗ trợ wellbeing marker. | Một log/turn và posture history đang được dùng. |
| `music-suggestion` | Làm rõ dùng `audio_recent` đã inject thay đọc lại. | Không tự bật nhạc, giữ cooldown. |
| `voice` | Mở đầu chỉ nói explicit speech, thiếu privacy; ví dụ meeting không thống nhất tắt mic hay cả hai. | Không tự chọn semantics privacy mới. |
| `speaker-recognizer` | Đã sửa: yêu cầu thông thường từ giọng chưa nhận diện không kích hoạt hỏi tên hay bắt buộc ack. Hint HAL và nudge OS chung chỉ hướng dẫn enrollment khi tự giới thiệu, yêu cầu đăng ký rõ ràng hoặc trả lời tiếp luồng đó. | Giữ self-enrollment, cùng cluster, đủ audio và quản lý giọng; ưu tiên yêu cầu mới không liên quan. |
| `connectors` | Discover báo connected khi chưa kiểm token và giấu lỗi đọc/parse; bước sau chỉ đọc file riêng dù discovery hỗ trợ generic. | Bảo mật, official host, xác nhận write; test fixture giả trước khi sửa. |
| `harness-use`, `computer-use`, `agent-management` | Tên người không chứng minh target Harness; tag trả lời không phải lựa chọn agent. | Dựa agent thật đã chọn/liệt kê, Buddy độc lập; batch này chưa sửa routing. |

## Đã review, chưa chọn thay đổi hành vi

- `emotion`: marker và auto-sync mắt đã tránh gọi trùng.
- `music`: playback/stop trực tiếp và discovery khi yêu cầu mơ hồ hợp lý.
- `input-branching`: voice đã handled im lặng nhưng vẫn giữ bookkeeping cần thiết.
- `face-enroll`: reference từng flow giữ đúng định danh/xác nhận; chỉ sửa metadata.
- `claude-buddy`: protocol approval legacy độc lập; không gộp theo tên Buddy.
- `faq`: hướng dẫn thông tin, không cấp quyền sửa settings; proxy Vite khớp config.
- `skill-creator`: evaluation cần nhiều bước có chủ ý; không áp dụng điểm dừng kiểu sound.

## Chủ động giữ nguyên / quyết định cho follow-up

- Giữ implementation discovery connectors trong lượt tuning này. Lỗi báo connected/giấu lỗi và đọc generic storage cần fix riêng với credential fixture giả; thay authentication trong lúc chỉnh prompt vượt quá mục tiêu giữ hành vi.
- Giữ meeting tắt mic hay cả hai chờ quyết định sản phẩm. Enrollment nay cần ý định từ user; giọng chưa nhận diện không ghi đè yêu cầu thông thường hay quy tắc im lặng với fragment vô nghĩa. Hướng dẫn HAL/OS chung áp dụng xuyên runtime, không đổi ngưỡng nhận diện hay API. Test local kiểm tra giữ metadata, hướng dẫn có điều kiện và cooldown; chưa chứng minh mức cải thiện latency của model.
- Giữ routing Harness: tên riêng chưa chứng minh agent. Gate Buddy độc lập; `harness-reply` không chọn target.
- Giữ semantics cooldown posture/check-in: reference mâu thuẫn với router/window; không đặt ngưỡng mới hoặc xóa posture history đang dùng.

## Kiểm chứng cuối và giới hạn

- Cả 28 frontmatter pass `python3 skills/skill-creator/scripts/quick_validate.py <skill>`; `git diff --check` pass.
- `go test ./system/skills/ ./system/server/agent/delivery/http/ ./system/lib/flow/ ./system/skillcontext/...` pass. Các package flow/skillcontext báo không có test, không coi đó là coverage hành vi.
- `python3 -m unittest discover -s skills/computer-use/tests -q`: 19 test pass (có một resource warning Python sẵn có); không sửa helper code.
- 15 block Bash audio/sensing-track pass `bash -n`. Recipe ghi âm chạy với HTTP mock local: giữ nguyên WAV khi thành công, HTTP 503 không báo thành công, mỗi ca chỉ một request.
- Recipe history GNU-date chỉ kiểm cú pháp, chưa chạy trên Linux; không khẳng định latency/lịch sử thật. Chưa test model/phần cứng trên device cho batch này. Test sound trước chỉ có phạm vi đã nêu.

Review hoàn tất ở cấp repo, với các quyết định giữ nguyên được ghi rõ. Không commit, deploy hoặc gọi dịch vụ ngoài cho batch tuning này. Hoàn thành goal nghĩa là hoàn tất review source và sửa đổi đã kiểm chứng, không phải đã xử lý mọi bug sản phẩm được phát hiện riêng.
