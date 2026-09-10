# Tích hợp Harness

`system/harness` nối trực tiếp một thiết bị Autonomous với một máy tính Harness đã ghép đôi rõ ràng. Harness Desktop/CLI tìm thiết bị qua dịch vụ mDNS `_autonomous._tcp` đã có, cũng được Autonomous Buddy sử dụng. Harness giữ khóa riêng và dùng giao thức pairing/phiên gốc của `E2eeManager`. Code và khóa Buddy độc lập.

## Ngữ cảnh sản phẩm và trách nhiệm giữa các team

App Harness và phần tích hợp thiết bị của Harness do team Harness phát triển độc lập. Repo này cung cấp phía Autonomous OS và skill `harness-use`; không sở hữu sản phẩm Desktop, runtime agent hay giao thức pairing của Harness. Mục tiêu là để thiết bị chuyển yêu cầu coding/research tới các agent mà Harness đang quản lý trên máy tính của người dùng.

| Bên phụ trách | Repo / code | Trách nhiệm |
|---------------|-------------|-------------|
| Team Autonomous OS | Repo này: `skills/harness-use`, `system/harness`, `system/server/harness.go`, `system/web/src/pages/monitor/HarnessCard.tsx` | Định tuyến voice/skill, giữ agent theo cuộc hội thoại và trạng thái delivery chưa rõ, tạo mã trên thiết bị, trust/phiên phía thiết bị, API nội bộ, OS Monitor và chuyển sự kiện về thiết bị. |
| Team Harness | [autonomous-harness](https://github.com/autonomous-ai/autonomous-harness): `cli/src/lib/autonomous-device`, `cli/src/lib/e2ee`, `cli/src/backendSocket.ts` | Discovery/reconnect phía máy tính, pairing/E2EE gốc, thao tác agent, thương lượng capability, receipt/event và API quản lý CLI. |
| Team Harness | [autonomous-harness-desktop](https://github.com/autonomous-ai/autonomous-harness-desktop): `lib/autonomous_device`, `lib/settings/sections/devices_section.dart` | UI ghép đôi/quản lý qua Harness CLI nội bộ. Desktop không giữ trust của thiết bị hay thực thi skill. |

Đường thực thi: người dùng/voice → `harness-use` → API loopback OS → kết nối trực tiếp đã xác thực → Harness CLI → agent được chọn trên máy tính. Khi người dùng gọi rõ một agent theo tên, OS thêm routing context nội bộ để chọn `harness-use` và loại các skill Buddy, kể cả khi phiên model còn mang chỉ dẫn skill cũ. Yêu cầu rõ “Autonomous Buddy” sẽ ghi đè route Harness và giữ nguyên cho skill Buddy. Agent Harness được nêu tên là đích thực thi: OS yêu cầu skill gửi thẳng nội dung công việc, không gửi yêu cầu hỏi hay liên hệ chính agent đó. Cửa sổ follow-up chỉ là gợi ý; câu nói mơ hồ, không liên quan hoặc không chắc chắn vẫn để main agent xử lý, trừ khi rõ ràng tiếp tục task Harness, trả lời câu hỏi đang mở, hoặc hỏi task đã xong/chưa hay yêu cầu kết quả. Quy tắc này áp dụng cho voice, Web Chat và MQTT Chat. Receipt `send` hoặc `answer` ở `queued`, `delivered`, `started`, `completed` hoặc `rejected` là kết quả đã xác định và kết thúc skill ngay: model không gọi thêm Harness hay shell, gồm `receipt`, `status`, `recap`, `list` hoặc mutation lần hai, mà trả `NO_REPLY`. Chỉ được xem receipt khi `DeliveryUnknown`/không có receipt dùng được hoặc người dùng yêu cầu rõ trạng thái giao; tuyệt đối không tự gửi lại. OS nhận lifecycle event và chuyển kết quả cuối trực tiếp. Với mỗi turn người dùng, skill lưu đích phản hồi cục bộ khi gửi rồi không tạo lời văn từ device agent. Các lifecycle event thật của Harness hiển thị việc đã nhận và đang xử lý trong phản hồi Web Chat đang chờ. Khi nhận `turn.summary` cuối, OS đọc entry mới nhất qua RPC `recap` và ưu tiên `turns[].fullText`, rồi `turn.summary.fullText`, rồi `text` cũ làm câu trả lời cuối. Nếu có `turn.done` đã hoàn tất nhưng thiếu summary, OS đợi ngắn callback thông thường rồi chỉ đọc recap mới nhất tối đa hai lần khi đúng response route vẫn pending. `fullText` là nội dung hoàn chỉnh hướng tới người dùng trong giới hạn đã định; `text` chỉ là bản preview ngắn cho CLI cũ và device card. Voice ghi kết quả trực tiếp này vào realtime history trước các follow-up, và follow-up ngắn sau đó nhận được nó dưới dạng context không đáng tin cậy cho main runtime. Nếu agent mở một câu hỏi có cấu trúc, OS chuyển câu hỏi đó về đúng turn gốc; turn trả lời đã định tuyến sau đó gọi `status`, dùng đúng request ID và các answer key đang mở để trả lời, rồi chuyển kết quả sau cùng về turn tiếp theo. Voice đọc nội dung trực tiếp từ Harness, còn Web Chat hiển thị nó mà không phát TTS. Callback Harness không được đưa thành JSON sensing event nên không thể tạo turn thứ hai hoặc một câu trả lời đã bị device agent sửa lại. Khi người dùng yêu cầu một agent làm việc, kể cả research bằng browser, `harness-use` được ưu tiên; `computer-use` dành cho thao tác UI Mac trực tiếp và Buddy chỉ dùng khi người dùng gọi rõ.

Autonomous Buddy được giữ riêng. Tính năng này không gọi Buddy, không dùng chung khóa pairing hay yêu cầu kết nối Buddy. Tái sử dụng quảng bá mDNS đã có trên thiết bị không đồng nghĩa gộp hai trust store. Giữ tích hợp dùng chung cho thiết bị Autonomous: namespace CLI là `autonomous-device`, không phải `lamp`.

## Nguồn contract và cách phối hợp

Contract CLI của team Harness nằm ở `docs/autonomous-device-integration.md` và `docs/vi/autonomous-device-integration_vi.md` trong repo của họ. Cần đối chiếu với code CLI cùng phiên bản, đặc biệt `command.ts`, `localApi.ts`, handler kết nối trực tiếp/ứng dụng và core/manager `e2ee` gốc. Tài liệu OS này mô tả triển khai trong repo này; không cố định hoặc ghi đè API đang phát triển của team kia. Vector giao thức và bài test liên repo bên dưới là bằng chứng tương thích, không phải đặc tả độc lập để tự nghĩ ra hành vi.

Tích hợp ban đầu được theo dõi tại [OS PR #316](https://github.com/autonomous-ai/autonomous-os/pull/316), [CLI PR #24](https://github.com/autonomous-ai/autonomous-harness/pull/24) và [Desktop PR #8](https://github.com/autonomous-ai/autonomous-harness-desktop/pull/8). Đây là các thay đổi phối hợp, không khẳng định bản phát hành nào đã triển khai. Kiểm tra revision CLI thực sự dùng trong mỗi lần kiểm thử liên thông; mô tả PR và các bản thử nghiệm trước có thể đã cũ.

Khi tiếp tục làm phía OS:

1. Chỉ sửa repo OS này. Agent của team Harness phụ trách hai repo kia; chuyển sai khác contract cụ thể để phối hợp, không âm thầm sửa triển khai của họ.
2. Trước khi đổi lệnh, cấu trúc frame, chiều pairing, ý nghĩa receipt hoặc capability, đối chiếu contract và code hiện tại của bên sở hữu. Ghi revision Harness liên quan và thống nhất thay đổi tương thích với team đó. Thiếu API hoặc lệch phiên bản không phải lý do tự tạo flow, credential backend hay transport mới.
3. Giữ luồng đã thống nhất: thiết bị sinh mã, máy tính tự tìm thiết bị và nhận mã, rồi kết nối trực tiếp. Mật mã pairing/phiên theo Harness; OS không đặt thêm cơ chế riêng. Capability thiếu phải được báo rõ, không lách qua terminal hoặc Buddy.
4. Khi contract được thống nhất thay đổi, cập nhật tài liệu OS cả hai ngôn ngữ và fixture liên quan, rồi chạy test liên repo với revision CLI đó. Test local thành công không chứng minh tương thích với một bản CLI đã cài khác.
5. Sửa repo và kiểm thử local tách biệt với triển khai thiết bị. Chủ thiết bị thực hiện manual test phần cứng/voice; deploy, SSH và restart cần được cho phép rõ ràng. Khi bàn giao triển khai phải bao gồm cập nhật nginx mô tả bên dưới.

## Ghép đôi

1. Trong OS Monitor của thiết bị, bấm **Generate pairing code**. Thiết bị tạo mã ngẫu nhiên mật mã sáu ký tự, có hiệu lực 60 giây.
2. Trên cùng mạng nội bộ, mở Harness Desktop → Settings → Devices, chọn thiết bị Autonomous được tìm thấy rồi nhập mã.
3. Với CLI, chạy `harness autonomous-device discover --json`, sau đó `harness autonomous-device pair --device <discoveryId> --code-stdin`, đưa mã vào stdin.
4. Harness mở WebSocket tới `/api/harness/ws` trên thiết bị được tìm thấy. Sau khi ghép, thiết bị có thể liệt kê và tương tác với agent trên máy tính qua `harness-use`.

Discovery lấy host và port đã quảng bá; đây không phải bằng chứng xác thực thiết bị. Thiết bị không cần ô nhập IP hoặc credential backend. Yêu cầu đăng nhập/khởi động Harness trên Mac vốn có được giữ nguyên; kết nối thiết bị không đi qua backend và hoạt động độc lập với kết nối backend đang mở. Nếu không thấy thiết bị, kiểm tra cùng mạng và mDNS không bị chặn.

Socket trao đổi `machine_select` / `machine_selected` để biết danh tính máy CLI, rồi OS gửi `e2e_pair_intent` gốc với `{pairId,label,role:"device"}`. Mã không được gửi qua socket hay lưu vào trust store. Các vòng CPace gốc dùng `autonomous-e2e-pair|agent:<machineId>|a:adapter|b:device`. `e2e_hello` có chữ ký và `e2e_welcome` mã hóa gốc thiết lập phiên. Đây là tái sử dụng giao thức mật mã Harness qua kết nối trực tiếp, không dùng transport cloud cũ.

Sai mã làm lần ghép thất bại và Desktop hiển thị lỗi. Tạo mã mới trên thiết bị trước khi thử lại. Hoàn tất, thất bại, hủy và hết hạn đều xóa mã. Không ghi đè máy tính đã ghép; cần unpair trước để chọn máy khác. CLI/Desktop có thể giữ các thiết bị đã ghép riêng biệt, mỗi thiết bị có danh tính riêng.

## API và vòng đời OS

- `NewService(configDir, callbacks)` đọc danh tính; `Start(ctx)` quản lý vòng đời.
- `POST /api/harness/pair` có xác thực admin gọi `StartPair(ctx)` không cần machine ID. `GET /api/harness/pair/status` trả mã tạm, `expires_at`, `pairing`, `state` và lỗi tùy chọn với `Cache-Control: no-store`.
- `POST /api/harness/pair/cancel` có xác thực admin vô hiệu hóa lần ghép đang chờ. `DELETE /api/harness` xóa pin và đóng socket.
- `GET /api/harness/status` dành cho admin hoặc caller loopback trực tiếp; không trả mã.
- `GET /api/harness/ws` nhận socket CLI trực tiếp. PAKE và E2EE với khóa đã ghim xác thực route này thay cho HTTP bearer. Từ chối header Origin của trình duyệt. Tối đa bốn socket đầu vào, giới hạn mười giây cho metadata đầu tiên và hai mươi giây cho handshake phiên/ứng dụng.
- `POST /api/harness/request` chỉ cho loopback thực sự, có kiểm tra địa chỉ proxy, để runtime của skill gọi.

Nếu pairing bị ngắt sau khi lưu pin tạm đã xác thực, OS Monitor hiển thị máy tính được giữ lại và nút Unpair thay vì tạo mã xung đột. Nếu không thể lưu việc xóa trust, API trả lỗi và giữ pin cũ trong RAM để trạng thái khớp với đĩa; thử Unpair lại sau khi xử lý lỗi lưu trữ.

CLI chịu trách nhiệm tìm và kết nối lại. OS chờ socket được xác thực; pin đã lưu tồn tại qua khởi động lại. Chỉ thay kết nối hiện có sau khi kết nối mới xác thực và thương lượng ứng dụng thành công. Socket đang hoạt động gửi WebSocket ping mỗi 15 giây và cần pong trong hạn đọc 60 giây.

Danh tính OS và pin của một máy tính lưu tại `configDir/harness/trust.json` (thư mục 0700, file 0600). Ghi nguyên tử và từ chối symlink. Trust file sai định dạng làm khởi tạo thất bại, không âm thầm tạo danh tính mới. Pin đã xác thực ở vòng ba là tạm trong năm phút để phục hồi khi mất PAKE cuối qua `e2e_hello` gốc; thiết lập phiên thành công xác nhận pin. Marker trực tiếp là `harness-device-direct-v1`; pin relay dùng E2EE gốc tương thích được chấp nhận, pin mật mã thử nghiệm trước đó thì không.

## Thao tác agent mã hóa

Sau xác thực, `autonomous_device_request` mã hóa mang `hello` ứng dụng để thương lượng capability và tiếp tục sự kiện. Phản hồi dùng `autonomous_device_result`; sự kiện dùng `autonomous_device_event`. Giữ khóa pairwise/group, miền chữ ký, dẫn xuất khóa, rekey có xác thực và chống replay gốc. Từ chối kết quả ứng dụng plaintext.

Hỗ trợ `agents.list`, `turn.send`, `turn.stop`, `status`, `recap`, `question.answer` và `receipt.get`. Thao tác nhắm agent cần machine ID và agent ID rõ ràng. Duyệt quyền công cụ, nhập terminal thô, shell/file tùy ý và tạo/xóa agent nằm ngoài tích hợp.

Mutation cần idempotency key ổn định. OS gửi một lần và chờ tối đa 30 giây. Timeout/mất kết nối sau gửi trả `DeliveryUnknownError`: tra receipt với cùng key, không tự gửi lại. Tối đa 64 request đang chờ và 128 sự kiện callback trong hàng đợi. Resume dùng `serverInstanceId` và `eventId` dạng số; resync cần đọc lại trạng thái agent. Skill giữ agent được chọn theo cuộc hội thoại và mutation chưa rõ kết quả giữa các lần gọi.

## Nginx và kiểm chứng

Quảng bá mDNS hiện có trỏ cổng 80. Mẫu nginx trong `scripts/provision/setup.sh`, `scripts/imager/build.sh` và `scripts/imager/build-orangepi.sh` có location chính xác `/api/harness/ws`, chuyển tiếp HTTP/1.1 Upgrade với timeout dài. Thiết bị đã cài cần được cập nhật cấu hình nginx này khi triển khai tính năng; chỉ upload binary Go không cập nhật nginx. Updater chuẩn `software-update` phải áp dụng migration nginx idempotent trước rollout Harness cho OS/web, vì image cũ không nhận template provisioner qua component OTA. Kiểm chứng trong repo không deploy hoặc restart thiết bị.

`system/harness/testdata/original-e2ee-protocol.json` được sinh từ E2EE core gốc của Harness. Test bao phủ CPace, chữ ký/khóa phiên gốc, bản ghi mã hóa, rekey, chống replay và vòng đời pairing. `system/server/harness_test.go` kiểm tra tạo mã không cần chọn máy, đọc mã cần xác thực chủ thiết bị, và lệnh agent từ xa/qua proxy bị từ chối. Kiểm tra tương thích liên repo cục bộ dùng manager CLI thật và service Go; không thay thế kiểm tra giọng nói và mạng LAN trên thiết bị vật lý.

Chạy kiểm tra liên repo tùy chọn từ repo OS sau khi cài dependency của checkout CLI:

```sh
go run ./system/harness/testdata/direct_interop.go /absolute/path/to/autonomous-harness/cli
```

Bài kiểm tra quảng bá mDNS tạm trên máy, dùng server WebSocket OS cục bộ và adapter CLI thật nhưng không kết nối backend; kiểm tra sai mã/thử lại, thao tác mã hóa, chống trùng, khởi động/kết nối lại và thu hồi quyền. Cần mạng multicast cục bộ; không kết nối thiết bị vật lý.

### Điều khiển ghép đôi qua MQTT

Các lệnh MQTT data đã xác thực của thiết bị gồm `harness.pair.start`, `harness.status`, `harness.pair.cancel` và `harness.pair.revoke`; phản hồi được publish trên fd channel của thiết bị. WebSocket trực tiếp vẫn là kênh dữ liệu cho máy tính Harness đã ghép đôi.

Khi phát lại hàng đợi Codex, Web/MQTT chat và voice follow-up được bổ sung lại địa chỉ `harness-reply` gốc. Yêu cầu qua hàng đợi giữ cùng run ID cục bộ và channel như khi gửi ngay.

Không poll recap mới nhất ngay sau khi gửi: dữ liệu có thể vẫn thuộc lượt trước và đánh dấu đã giao trước khi kết quả mới tới. Chuyển recap cuối khi nhận `turn.summary`; nếu có `turn.done` nhưng thiếu summary thì dùng fallback có giới hạn đã mô tả ở trên.

Khi Harness phụ trách phản hồi của một run, các sự kiện chat assistant thông thường của đúng run đó được chặn để lời báo đã giao việc hoặc `NO_REPLY` không đóng Web/MQTT chat trước khi kết quả Harness tới. Tin nhắn người dùng và sự kiện lỗi vẫn được chuyển tiếp.

Cả sáu runtime (Codex, OpenClaw, Hermes, PicoClaw, Claude Code và OpenCode) khôi phục địa chỉ trả lời Harness khi phát lại chat trong hàng đợi. Hermes giữ MQTT chat và voice follow-up thành lượt riêng, không gộp với cảm biến nền. Câu tiếng Việt không dấu như “hoi mike agent” được thêm định tuyến agent có tên. Nếu chat kết thúc im lặng mà không gửi yêu cầu Harness, MQTT phát sự kiện final rỗng để mobile ngừng chờ; không hiển thị chuỗi nội bộ `NO_REPLY`.

Kết quả Harness chỉ khớp với run ID thiết bị đã đăng ký. Kết quả không rõ run không được chiếm chat khác đang chờ; task Harness đang chờ không được chặn phản hồi runtime của lượt khác. Kết quả rỗng không đánh dấu đã giao, nên kết quả có nội dung đến sau vẫn hoàn tất được lượt đó.

Lượt Harness đã hoàn tất giữ trạng thái chống lặp để dọn sau 15 phút (dọn khi đăng ký route tiếp theo), chặn final runtime đến sau lifecycle end, progress muộn và việc đăng ký lại route sau kết quả cuối.

Kết quả cuối Harness được ghi vào flow JSONL bằng `harness_response`, giữ run ID thiết bị gốc và `text` đầy đủ. Web Chat dùng sự kiện này khôi phục kết quả đang chờ sau khi SSE ngắt hoặc tải lại trang. Luồng trực tiếp vẫn phát `chat_response` với state `final`.

Callback summary vẫn tra recap khi không có preview. Kết quả rỗng giữ route đang chờ. Sau khi tra recap, chỉ xóa route của đúng run ban đầu; callback từ agent không liên quan không được chiếm chat khác.

Route được khóa bằng run ID cục bộ của thiết bị, không phải agent ID. Một Harness agent có thể có nhiều task người dùng đang chờ; khi sự kiện cũ không có local run ID, OS đưa nó vào route đang chờ lâu nhất của agent đó và giữ nguyên các route mới hơn. Nếu runtime copy cũ riêng phần sequence của response route dạng `device-…-<timestamp>`, OS khôi phục run đúng cùng channel từ flow record trong bộ nhớ có timestamp đó; timestamp khác tuyệt đối không bị đổi. Helper cục bộ lưu receipt đã biết theo response route và từ chối `send` hoặc `answer` thứ hai trên cùng route, ngăn loop receipt/status của model dispatch task hiện tại hai lần. Nếu task mới hoặc task đính chính bị chặn bởi delivery trước, runtime được kiểm receipt đó một lần; khi receipt có trạng thái delivery đã biết, runtime phải gửi task hiện tại trước khi trả `NO_REPLY`.
