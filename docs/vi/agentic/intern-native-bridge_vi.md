# Bridge Intern chạy trực tiếp trên thiết bị

`runtimes/intern/bridge` triển khai protocol `0.2.0`, schema `cassi-first.v1`
trong binary Go `os-server` hiện có. Không đổi `runtimes/intern.Service` hoặc
client transport. Chức năng chỉ là soạn văn bản và đề xuất tuyến tiếp nhận;
không thực thi công việc, phần cứng, kênh nhắn tin hoặc MQTT.

## Vòng đời và cấu hình

Chỉ nhánh khởi động Intern đang hoạt động mới tạo provider và bind
`tcp4 127.0.0.1:8765`, trước worker. Cổng bị chiếm gây lỗi; không dùng lại
listener của tiến trình khác. Lỗi bind listener quản trị cũng giải phóng bridge.
Không tạo process Python, Hermes, OpenClaw, installer hoặc systemd unit mới;
`os-server.service` hiện có quản lý toàn bộ tiến trình.

SIGTERM/interrupt hủy worker và yêu cầu provider, cho mỗi listener tối đa năm
giây để đóng HTTP rồi buộc đóng socket còn lại. Lỗi listener dừng vòng đời
Intern. Đổi lựa chọn runtime đã lưu không thay đổi runtime đang chạy; người
vận hành phải khởi động lại. Cấu hình là snapshot lúc khởi động từ
`config/config.json`; bridge không ghi cấu hình, tải model hoặc tìm khóa.

| Field | Mặc định / mục đích |
|---|---|
| `intern_provider` | Rỗng hoặc `ollama`: suy luận cục bộ. Chọn rõ `cerebras` để dùng API Cerebras qua HTTPS. Giá trị khác, kể cả `openai`, gây lỗi khởi động. |
| `intern_ollama_url` | `http://127.0.0.1:11434`; chỉ origin, path rỗng hoặc `/`. Chỉ cho phép `127.0.0.1`, `::1`, `localhost`; chuyển `localhost` thành IPv4 literal trước khi kết nối. |
| `intern_ollama_model` | `qwen3:4b`; người vận hành phải cung cấp model riêng. Không tự pull và không khẳng định model có sẵn. |
| `llm_base_url` | Chỉ dùng khi chọn provider từ xa rõ ràng; HTTPS API base, ví dụ `https://api.cerebras.ai/v1`; thêm `/chat/completions`. |
| `llm_model` | Bắt buộc khi dùng provider từ xa; không đoán model. |
| `llm_api_key` | Khóa hiện có, bắt buộc cho provider từ xa; chỉ gửi bằng bearer header tới endpoint đó, không gửi Ollama. |

Cấu hình LLM chung kế thừa không tự bật cloud. Cấu hình từ xa thiếu/sai gây lỗi
khởi động, không fallback sang Ollama. Từ chối URL có userinfo, query, fragment
hoặc path mã hóa. Không tìm provider/khóa từ môi trường, không proxy discovery.
Chọn Cerebras rõ ràng cho phép bridge dùng các giá trị `llm_base_url`,
`llm_model`, `llm_api_key` hiện có. Model phải hỗ trợ `reasoning_effort:"none"`;
model/tùy chọn không hỗ trợ trả fallback có giới hạn. Ollama lỗi không tự chuyển
sang Cerebras.
Bridge không log cấu hình, prompt, phản hồi provider hoặc lỗi gốc. Lỗi là chuỗi
cố định; phản hồi chứa chính khóa provider bị từ chối.

## Chấp thuận và định tuyến

API quản trị vẫn yêu cầu phân loại rõ ràng cho chính xác văn bản. Client chặn
unknown/restricted/secret trước network. Bridge kiểm tra lại trước định tuyến;
provider kiểm tra lại trước suy luận. Chỉ public/business được tới model.
Nhãn phải do bên gọi đáng tin cậy cung cấp; đây không phải bộ dò dữ liệu riêng
tư theo ngữ nghĩa. Gắn nhãn sai không cấp quyền gửi bí mật. Loopback dành cho
process cục bộ đáng tin cậy, không xác thực người dùng cục bộ khác. Từ chối
Origin, credentials, cookie, forwarded headers, proxy absolute-form và query.

Mọi kết quả đều có đích đầu tiên `cassi@mama`, `executes_actions:false`,
`executed:false`, `next_step:safe_escalation`, scope `bridge_request`. Đây là
metadata Cassi-first, không kết nối tới Mama.

| Tên/intent đầu câu sau wake `Gus` tùy chọn | Đích đề xuất | Kết quả |
|---|---|---|
| Gus / orchestration | `orchestration@gus` | Route persona hoặc draft/classification |
| Rex / engineering | `rex@dru` | Route persona hoặc draft/classification |
| Melvil / curator / library | `melvil@lab` | Route persona hoặc draft/classification |
| PAM / service | `pam@gus` | `service_route`, không thực thi |
| Cassi / casi / cassandra / reception | `cassi@mama` | `custody_hold`, không có output, handoff null |
| news / briefing / daily briefing / morning briefing | `mcavoy@lab` | `service_route`, chờ xem xét, không lấy tin |
| notification / alarm / reminder | `pam@gus` | `service_route`, chờ xem xét, không gửi hoặc lập lịch |
| smart-home / smart home | `smart-home` | `custody_hold`, không có output, handoff null |

Hỗ trợ `hey`, `ok`, `okay`, `hey_gus`; chỉ bỏ tối đa một wake công ty và một
địa chỉ nội bộ khi định tuyến. Tên nội bộ rõ ràng vẫn tạo đề xuất xác định trước;
`Gus` đơn lẻ nhận diện công ty. Không đệ quy/fan-out. `route` không gọi model.
Recognizer bị giới hạn và xác định trước: news/headline(s)/briefing
(gồm daily/morning) đề xuất `mcavoy@lab`; notify/notification, alarm và
remind/reminder đề xuất `pam@gus`, kể cả khi `DataClass` là unknown. Bất kỳ
smart-home, device, light, scene, fan, Hue, Nanoleaf, Kasa hoặc home-control nào
(kể cả service trộn với home) luôn `custody_hold`, không handoff và không gọi
model. Với `route`, văn bản chưa xác định nhận intent unknown và handoff null.
`classify` chỉ trả `question`, `draft`, `action`, `unknown`; nhãn không cấp
quyền hành động. Model nhận văn bản đã chấp thuận nguyên vẹn và một system
instruction cố định, không lịch sử, ảnh, tệp, ngữ cảnh kênh hoặc bộ nhớ nhân viên.

### Reception bằng ngôn ngữ thông thường

Với `reception`, văn bản public/business chưa xác định, có hoặc không có wake
`Gus`, được gọi phân loại **tối đa một lần** qua provider Ollama cục bộ hiện có.
Tên nội bộ rõ ràng, tuyến service xác định trước và custody hold không gọi model.
Cụm trộn content/PAM (kể cả bắt đầu bằng alias service) trả yêu cầu làm rõ mà
không suy luận. Wake công ty không có nội dung trả `needs_input`. Không tự suy
ra hoặc nâng quyền nhãn dữ liệu của bên gọi.

Chỉ nhận các nhãn `orchestration`, `engineering`, `service`, `notification`,
`alarm`, `reminder`, `library`, `news`, `briefing`, `smart-home`, `reception`,
`unknown`, tương ứng hợp đồng nhãn reception Python có giới hạn. Sau khi bỏ
khoảng trắng đầu/cuối phải còn đúng một nhãn; chặn JSON, danh sách, địa chỉ
persona, lời giải thích và nhãn lạ. Chỉ dẫn cố định yêu cầu `unknown` khi mơ hồ
hoặc có nhiều đích. Nhãn ánh xạ tới đích hiện có ở trên, tối đa một đề xuất.
Nhãn `smart-home`/`reception` trả `custody_hold`, không output hoặc handoff.
Chặn từ khóa home và custody do bên gọi khai báo vẫn chạy trước suy luận.

Fixture provider giả lập phân loại “Give me a rundown to start the day” thành
`briefing`, “What happened in the world today?” thành `news`, “Wake me at seven”
thành `alarm`, “Let me know when the report is ready” thành `notification`.
Đây là kiểm thử hợp đồng, không đo độ chính xác của model thật.

Provider vắng mặt/lỗi, output sai hoặc mơ hồ và `unknown` trả `reception_route`,
intent `unknown`, handoff null và câu cố định “Could you clarify?”. Đề xuất hợp
lệ cũng dùng câu cố định yêu cầu xem xét; output phân loại thô, reasoning và lỗi
provider không trở thành lời trả cho người dùng. Giữ mọi kiểm tra final-answer,
tỷ lệ token, timeout, kích thước phản hồi và giới hạn đồng thời. Reception không
gọi generation lần thứ hai.

Chọn Cerebras rõ ràng giữ hành vi `generate`/`classify` hiện có. Phần tăng thêm
này chỉ phân loại reception cục bộ: reception chưa xác định dưới Cerebras trả
yêu cầu làm rõ, không network I/O hoặc đổi provider. Giữ protocol `0.2.0`, schema
`cassi-first.v1`, metadata Cassi-first, `executed:false`,
`next_step:safe_escalation`; không thêm dispatch hoặc thực thi.

## Giới hạn và kết quả

- Một lời gọi provider đồng thời; yêu cầu model khác nhận unavailable, không xếp
  hàng. Không retry, redirect, proxy, cookie, đổi provider hoặc fan-out.
- Ollama `/api/chat`: `stream:false`, `think:false` ở cấp cao nhất,
  `keep_alive:5m`, `num_predict:256`, temperature 0. Provider từ xa dùng
  `/chat/completions`, `max_completion_tokens:256`, `reasoning_effort:none`, temperature 0.
- Deadline provider 15 giây kể cả đọc body; hủy/deadline sớm hơn được ưu tiên.
  Connect/TLS timeout 3 giây; header tối đa 8 KiB, body 64 KiB, một kết nối.
  Xác minh TLS được bật.
- Chỉ chấp nhận assistant content hoàn tất, không streaming. Chặn reasoning,
  tool payload, field message lạ, truncation, marker think/analysis/tool/HW,
  JSON key trùng, Unicode lỗi, tài liệu JSON thừa. Output tối đa 4.096 code point.
  Bộ lọc bao gồm thẻ đóng XML/ngoặc, tiêu đề và nhãn code fence reasoning,
  liên kết phần cứng như `[label](HW:/led/off)`, thẻ `<say>` và sentinel runtime.
  Từ chối toàn bộ phản hồi, không cắt bỏ một phần để biến thành thành công.
  Vẫn hỗ trợ văn bản cuối và Markdown thông thường; kiểm tra marker không chứng
  minh không có reasoning bằng văn bản không đánh dấu.
  Ollama phải báo 1–256 token và không quá bốn lần số code point output sau khi
  bỏ khoảng trắng đầu/cuối; phép
  kiểm tra thận trọng có thể chặn câu ngắn hợp lệ, không chứng minh nội bộ model.
- Sai cấu hình gây lỗi khởi động. Với `generate`/`classify`, provider vắng mặt, timeout, từ chối, tùy chọn
  không hỗ trợ hoặc output không an toàn trả `fallback` cố định / `ErrUnavailable`;
  không lộ lỗi provider.
- Input tối đa 8.000 code point / 16 KiB JSON; chặn field trùng/lạ/sai hoa-thường,
  null và Unicode lỗi.
- Run ID cung cấp được băm SHA-256, giữ trước suy luận, replay trả 409. Giữ tối
  đa 4.096 ID suốt đời process; đầy thì unavailable thay vì xóa ID cũ. Restart
  xóa ledger; không bảo đảm exactly-once bền vững. ID bỏ trống dùng 128 bit ngẫu
  nhiên và không được lưu.

`GET /health`, `/ready`, `/version` chỉ chứng minh metadata transport, không gọi
model. Gateway chỉ ready sau draft hợp lệ và hết hạn sau 30 giây. Hủy HTTP
cục bộ không chứng minh provider từ xa dừng tính toán hoặc tính phí.

Tham khảo API [Ollama](https://docs.ollama.com/api/chat),
[Cerebras](https://inference-docs.cerebras.ai/api-reference/chat-completions) và
[biên bản kiểm chứng](../../receipts/intern-native-bridge-2026-09-14.md).
Model/provider không hỗ trợ yêu cầu final-answer có giới hạn sẽ thất bại an toàn.
Xem [biên bản reception ngôn ngữ thông thường](../../receipts/intern-reception-classification-2026-09-15.md).
