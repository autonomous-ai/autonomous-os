# Điều khiển vật lý — Nút GPIO + Touchpad TTP223

Lamp có hai thiết bị input vật lý mà user có thể chạm trực tiếp. Chúng dùng chung thư viện action (`hal/drivers/button_actions.py`) nên cùng một cử chỉ "single click" sẽ hành xử giống nhau dù đến từ nút bấm cơ học hay touchpad cảm ứng.

## Tại sao có hai thiết bị

| Thiết bị | Vai trò | Có ở |
|---|---|---|
| **Nút GPIO** | Một nút bấm cơ. Dùng cho các hành động dứt khoát kể cả destructive (reboot / shutdown / factory-reset). Cảm giác cơ + detect giữ lâu khiến destructive action khó xảy ra do vô tình. | Pi 4/5 và OrangePi sun60 |
| **Touchpad cảm ứng TTP223** | Bốn pad chạm xếp như "đầu cún" để vuốt ve + stop/unmute nhẹ. Không có destructive gesture vì FastMode của IC không cho detect giữ lâu tin cậy. | Chỉ OrangePi sun60 (4 Pro / A733) |

## Wiring

| Thiết bị | Pi 4/5 | OrangePi sun60 |
|---|---|---|
| Nút GPIO | gpiochip0 BCM 17 (pull-up, active-LOW) | gpiochip1 line 9 (pull-up, active-LOW) |
| TTP223 | không wire | gpiochip0 line 96 / 97 / 98 / 99 (đặt tên S1–S4), pull-down, active-HIGH |

Cả hai handler đều detect board qua `/proc/device-tree/model`:
- `"sun60iw2"` → OrangePi 4 Pro / A733
- `"raspberry pi 5"` → Pi 5
- `"raspberry pi 4"` → Pi 4
- khác → unknown, cả hai handler bỏ qua không claim GPIO

## Bảng cử chỉ

| Cử chỉ | Nút GPIO | Touchpad TTP223 |
|---|---|---|
| **1 chạm** | Dừng object tracking đang chạy, rồi stop loa / unmute mic + speaker + chime ack (~120 ms ping) — tất cả fire ngay khi nhả nút (không đợi click window); cue "Nghe đây" phát sau khi click window 0.4 s phân giải xong | Tương tự sau khi quyết định tap-vs-pet 1.2 s xong — tracking đang chạy dừng, rồi action mic/loa và cue chạy. Chạm đầu tiên vẫn cắt TTS đang phát và kêu chime ack ngay. |
| **2 chạm** (≤ 0.4 s, nút) / (≤ 1.2 s, TTP223) | Không thêm gì ngoài single-click đã fire ở chạm 1 (panic-click guard) | Pet response — TTS chọn ngẫu nhiên 1 câu từ pool theo ngôn ngữ |
| **3 chạm** (≤ 0.4 s, nút) | Reboot OS (TTS báo → `sudo reboot`) | n/a — TTP223 dừng ở 2 (chạm thêm bị cooldown nuốt) |
| **Giữ 2–5 s rồi nhả** | Phát thông báo sleep theo ngôn ngữ, rồi vào `sleepy`: LED tắt, camera/mic/speaker tắt; servo release sau 1 s. Khi đang giữ LED nháy tím sleepy. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 5–10 s rồi nhả** | Shutdown OS (TTS báo → release servo → `sudo shutdown -h now`). LED nháy đỏ khi đã arm. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 10 s+ rồi nhả** | Factory-reset: wipe state thiết bị + reboot vào AP setup (TTS báo → release servo → POST `/api/system/factory-reset` trên OS server). LED đỏ đứng khi đã arm. | n/a |

Gesture giữ chỉ có trên nút GPIO vì nút cơ học cho bằng chứng intent rõ ràng. Mức sleep và các mức destructive **commit khi nhả, không phải khi timer fire lúc đang giữ**. Các mức destructive escalate từ shutdown sang factory-reset sau 10 s (xem "Detect nút GPIO" dưới).

## Cắt Lamp giữa câu (barge-in)

Cử chỉ 1 chạm là **cơ chế barge-in và huỷ attention chính** của Lamp: trước hết nó dừng mọi session object tracking đang chạy; sau đó chạm đỉnh Lamp (touchpad) hoặc nhấn nút GPIO một lần khi Lamp đang nói → cắt câu TTS đang phát giữa chừng, dừng nhạc, unmute mic để Lamp lắng nghe câu kế. Nếu loa đang bị mute bởi user/scene thì cũng được gỡ (trừ khi đang ghi âm enroll giọng) để cue và câu trả lời nghe lại được. Dừng tracking vẫn hoạt động khi hardware mic kill switch đang tắt; nó không wake hoặc unmute mic. Cue "Nghe đây" (theo ngôn ngữ) chỉ phát khi switch cho phép action voice.

Khi wake word đang bật, cú click cũng **được tính như một wake event**: `single_click_action` gọi `voice_service.grant_wakeword_focus(source)`, mở đúng cửa sổ follow-up focus (`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, mặc định 20 s) mà câu wake phrase mở ra. Không có nó thì thiết bị nói "Nghe đây" rồi lại bỏ câu trả lời của user vì thiếu wake phrase. Cửa sổ được kiểm tra lại ở thời điểm dispatch, không chỉ latch lúc mở mic session, nên click giữa lúc session đang chạy vẫn authorize câu user đang nói. No-op khi wake word tắt (mọi câu đã dispatch sẵn) hoặc timeout follow-up = 0.

### Quay về phía đèn — trigger wake thứ ba

Wake gate có **ba** cửa vào, không phải hai. Bên cạnh wake phrase nói ra và single click, **quay mặt về phía đèn rồi nói** cũng mở đúng cửa sổ đó (`hal/drivers/tracking/gaze.py`), qua đúng seam `voice_service.grant_wakeword_focus(source)` mà cú click dùng — mọi thứ phía sau gate không đổi.

Lý do nằm ở hình dạng sản phẩm chứ không phải sở thích. Đèn bàn nằm cách user một cánh tay và trong tầm nhìn cả ngày, nên lặp wake phrase vài chục lần một ngày nghe như đang ra lệnh cho một thiết bị, còn bấm nút thì như đang vận hành máy. Giữa hai người, tín hiệu không phải hai thứ đó: người ta **quay về phía nhau rồi nói**. Các sản phẩm phổ biến hoá "hey <name>" đều không có camera và đặt ở đầu kia phòng, nên không so sánh trực tiếp được.

Hai đặc tính quyết định cách implement:

* **Người ta quay TRƯỚC khi nói, không bao giờ sau.** Chờ mic báo rồi mới nhìn thì đã muộn — và tệ hơn là không bao giờ thấy được **sự chuyển** từ nhìn chỗ khác sang nhìn đèn, vốn là toàn bộ tín hiệu. Nên watcher lấy mẫu liên tục vào một ring buffer (`HAL_GAZE_BUFFER_S`, mặc định 3 s), còn tiếng nói chỉ kích hoạt việc **đọc ngược** buffer đó. Đây đúng mô hình mic đang dùng với pre-roll lookback của nó, thứ tồn tại để không mất âm đầu câu.
* **Có mặt người KHÔNG phải tín hiệu.** User ngồi cạnh đèn cả ngày nên "phát hiện có người" gần như luôn đúng và không lọc được gì; "phát hiện có mặt" cũng chỉ hơn chút — mặt quay về màn hình vẫn detect ra. Gate đặt trên **hướng đầu**, đủ chặt để loại tư thế rất thường gặp: nói chuyện với đồng nghiệp trong khi thân vẫn hướng về bàn.

Head yaw suy ra từ 5 landmark mà `YuNet` vốn đã trả về (`detect_face_with_landmarks` trong `detection.py`): độ lệch của mũi so với trung điểm hai mắt, đo **dọc theo đường nối hai mắt** và chuẩn hoá bằng nửa khoảng cách hai mắt, chính là `sin(yaw)` dưới phép chiếu pinhole. Đo dọc đường nối mắt thay vì theo trục x của ảnh là thứ giữ cho đầu **nghiêng** (chống tay lên má) không bị đọc thành đầu quay. Không load thêm model nào, không chạy thêm inference nào; ở `HAL_GAZE_SAMPLE_FPS` (mặc định 6) chi phí là số lẻ trên CPU 8 nhân — đo thật chứ không suy đoán: CPU idle 69.2% xuống 68.8% khi watcher chạy.

Landmark nằm ngoài khung không phải là một phép đo. `YuNet` trả về đủ 5 điểm cho một khuôn mặt bị mép khung cắt hệt như cho khuôn mặt nằm trọn trong khung, và những điểm bị cắt quay về với toạ độ ngoài khung — đo thật trên máy, user ngồi thẳng trước đèn còn camera thì ngắm quá thấp: box `[264, -1, 162, 92]`, hai mắt ở `y = -3.0` và `y = -1.3`. Đưa vào công thức yaw, các toạ độ đó đẩy tỉ số mũi vượt 1, chỗ mà lệnh clamp biến "không đo được" thành đúng `90.0` — không phân biệt được với một khuôn mặt nghiêng thật, và bị đếm là một phiếu **chống** hướng về đèn. Đó chính là lý do user đang nhìn thẳng vào đèn lại cho ra `trail=[90,90,90,90]` và bị từ chối. Nên mẫu nào có mắt hoặc mũi rơi ra ngoài khung sẽ được ghi là **không đo được** (không bỏ phiếu theo chiều nào, giống hệt frame không thấy mặt); bbox của nó vẫn nuôi phần chỉnh ngắm theo chiều dọc, thứ mà khung hình lệch như vậy đang cần. Khoé miệng bị cắt thì bỏ qua — góc quay không bao giờ đọc tới chúng.

Trước tất cả những thứ trên, các dòng detector có bbox không phải số hữu hạn bị loại thẳng. YuNet có thể trả về toạ độ vô cực cho một khuôn mặt đang rời khung — quan sát thật trên máy khi đang tracking, `bbox_area` 1.9%, conf 0.29 — và `int()` trên nó ném `OverflowError`, giết luôn thread detect của tracker giữa phiên. Vô cực không phải là "mặt rất to", nó là detector nói rằng không có gì dùng được; nên bỏ dòng đó đi và để đường "frame này không thấy mặt" vốn có xử lý tiếp. Bộ lọc chạy **trước** bước chọn mặt to nhất / gần tâm nhất, vì chiều rộng vô cực thắng mọi cuộc so diện tích và sẽ che mất một khuôn mặt hoàn toàn dùng được.

Khi trong khung có nhiều mặt, mặt được tính là mặt **gần tâm khung nhất** trong số những mặt cao ít nhất `HAL_GAZE_MIN_FACE_PX` — không phải mặt to nhất. Lấy mặt to nhất tức là trao gate cho bất kỳ ai ghé vào gần hơn, và người đó là user chỉ theo thông lệ; chính hướng ngắm của đèn mới là tiên nghiệm tốt hơn cho câu hỏi nó đang chĩa vào mặt nào. Khi chỉ có một mặt đạt ngưỡng thì hai luật cho cùng kết quả, nên thay đổi này chỉ có tác dụng khi thực sự có người thứ hai chung bàn. Nếu không ai qua ngưỡng kích thước thì vẫn trả về mặt to nhất: bbox đó còn nuôi phần chỉnh ngắm theo chiều dọc (`HAL_GAZE_REPOINT`), mà việc chỉnh lại cần nhất đúng lúc mọi mặt đều quá nhỏ để đo. Lưu ý đường tracking chỉ lấy bbox (`_detect_face_yunet`, dùng cho object follow) vẫn giữ chính sách mặt-to-nhất của riêng nó — hai bên độc lập.

| Env var | Mặc định | Chỉnh cái gì |
|---|---|---|
| `HAL_GAZE_WAKE` | `false` | Công tắc tổng. Tắt = chỉ còn hai cửa như hiện nay. |
| `HAL_GAZE_SHADOW` | `true` | Chỉ log quyết định, không mở gate. Không tốn gì — không turn nào mở nên không tốn LLM hay TTS. |
| `HAL_GAZE_MAX_YAW_DEG` | 25 | Nón chấp nhận ở giữa khung. |
| `HAL_GAZE_EDGE_CONE_SCALE` | 1.8 | Nón nới rộng bao nhiêu ở rìa khung, nơi barrel distortion thổi phồng góc. |
| `HAL_GAZE_MIN_FACE_PX` | 48 | Chiều cao mặt tối thiểu **tính bằng pixel**. Dưới ngưỡng này landmark chỉ cách nhau vài pixel, góc tính ra là số học trên sai số làm tròn, nên mẫu đó không được bỏ phiếu. |
| `HAL_GAZE_WINDOW_S` | 1.5 | Cửa sổ bằng chứng, kết thúc tại thời điểm bắt đầu nói. |
| `HAL_GAZE_MIN_FACING_RATIO` | 0.6 | Tỉ lệ mẫu trong cửa sổ phải thấy đầu hướng về đèn. Là TỈ LỆ, không phải chuỗi liên tục — yaw từng mẫu nhiễu thật. |
| `HAL_GAZE_MIN_SAMPLES` | 2 | Dưới mức này không đủ bằng chứng để kết luận theo chiều nào. Vòng lặp thực tế chỉ đạt ~2 mẫu/s dù cấu hình bao nhiêu — nó bị chặn bởi việc lấy frame và chạy detector — nên để 3 là loại oan cả user mà mọi tầng khác đều đồng ý là đang nhìn đèn. Dòng log `[gaze] sampling at N/s` đếm số mẫu THỰC SỰ ghi được, và báo riêng số frame bị chặn trước khi kịp đo (đang chờ servo ổn định, hoặc detector đang bị một lệnh `look` giữ). Đếm số lần thử thay vì số mẫu từng báo 5.7/s trong khi buffer không có gì mới hơn cửa sổ 1.5 s — tức dưới 1 mẫu/s bằng chứng thật. |
| `HAL_GAZE_SAMPLE_FPS` | 6 | Tần suất lấy mẫu. Cử chỉ thì chậm, nhưng quyết định là một cuộc bỏ phiếu và chỉ mẫu đo được mới tính — ở 3 fps cửa sổ thường chỉ còn một mẫu dùng được, từ chối cả user đang nhìn thẳng vào đèn. |
| `HAL_GAZE_BUFFER_S` | 3.0 | Lịch sử yaw giữ lại. Phải lớn hơn `WINDOW_S`. |
| `HAL_GAZE_COOLDOWN_S` | 5 | Khoảng cách tối thiểu giữa hai lần gaze mở gate. |
| `HAL_GAZE_REPOINT` | `true` | Quay về bearing đã nhớ khi lâu không thấy ai. |
| `HAL_GAZE_REPOINT_AFTER_S` | 45 | Phải vắng mặt bao lâu mới quay. |
| `HAL_GAZE_REPOINT_COOLDOWN_S` | 300 | Tối đa một lần quay trong khoảng này. |
| `HAL_GAZE_REPOINT_MIN_CONFIDENCE` | 0.5 | Dưới confidence này thì bearing không đáng để quay. |
| `HAL_GAZE_PITCH` | `true` | Nâng/hạ `wrist_pitch` để khuôn mặt nằm trong khung thay vì ở phía trên khung. Đặt trên bàn thì đèn khởi điểm chĩa vào bàn phím, và không có cú chỉnh trái-phải nào cứu được chuyện đó. Trước đây tắt vì mỗi bước chỉnh đều bị xoá giữa chừng; sửa xong thì vòng lặp hội tụ — đo được 45% → 21% → 16% chiều cao khung, mỗi bước xuất phát đúng chỗ bước trước để lại. Vẫn là vòng hở trên cánh tay hai khâu ghép nhau, nên `MAX_STEP_DEG` và `MAX_BLIND_STEPS` mới là thứ bó nó. |
| `HAL_GAZE_PITCH_DEG_PER_FRAME` | 45 | Số độ `wrist_pitch` cho trọn một chiều cao khung. Là hạt giống, không phải hiệu chuẩn: sau mỗi bước nó đo lại. |
| `HAL_GAZE_PITCH_MAX_STEP_DEG` | 15 | Bước chỉnh lớn nhất một lần. |
| `HAL_GAZE_PITCH_DEAD_ZONE_FRAC` | 0.15 | Lệch dưới mức này thì để yên. |
| `HAL_GAZE_PITCH_COOLDOWN_S` | 4 | Khoảng cách tối thiểu giữa hai lần chỉnh. |
| `HAL_GAZE_PITCH_MAX_BLIND_STEPS` | 0 | Số lần được chỉnh dựa trên suy đoán từ thân người thay vì mặt thật, trước khi phải có một khuôn mặt xác nhận hướng. |
| `HAL_GAZE_IDLE_ANCHOR` | `true` | Dời tâm vòng idle về tư thế đã nhìn thấy mặt, để idle không kéo ngược cú chỉnh. |

Hai tham số trong đó là **đo ra**, không phải chọn. `MIN_FACE_PX` có vì probe trên thiết bị bắt được ba đồng nghiệp ở nền cỡ 8–18 px cho ra yaw 49 / 20 / 29 — nhiễu thuần — bên cạnh người dùng ngồi tại bàn cỡ 78 px với yaw 90 hoàn toàn đúng; hai nhóm không chồng lấn nên ngưỡng này xoá cả một lớp rác chứ không phải chỉnh cho vừa. `MIN_FACING_RATIO` có vì trail của một người ngồi yên đọc ra `[10,15,8,25,36,1,-,90]`, mức dao động mà không cái đầu nào làm được, nên mọi luật đòi MỌI mẫu phải đạt đều sẽ loại oan họ.

Lâu không thấy ai mà đèn tự quay: đó là `REPOINT`, và là thứ **duy nhất** trong watcher động vào thân đèn. Recording idle là một vòng lặp các pose tuyệt đối, đảo `base_pitch` khoảng 17° mỗi chu kỳ, nên dù đặt đèn ở đâu thì idle cũng kéo camera về pose ghi sẵn của chính nó — trên bàn làm việc thì đó là bàn phím. Đặt pose đã nhớ một lần sẽ bị vòng lặp kế tiếp ghi đè; muốn đậu đúng ở bearing thì phải offset toàn bộ playback theo bearing, việc đó thuộc về motion playback chứ không thuộc tính năng này. Nên đèn làm điều mà con người làm: không thấy ai có thể đang nói với mình thì quay về chỗ người đó hay ngồi, một lần, rồi chờ.

Shadow mode tồn tại chính để một buổi chạy cạnh user thật cho ra số liệu (`[gaze] speech: yaw=… facing=…%/…% -> WOULD_WAKE`) đủ để chốt các ngưỡng trên.

Suy biến sạch theo cả hai chiều. Máy **không có camera** thì watcher không bao giờ arm, hai cửa kia nguyên vẹn — không cần cấu hình riêng. Khi `HAL_WAKEWORD_ENABLED` **false** thì watcher không khởi động luôn: không có wake word thì mọi câu đã dispatch sẵn, không còn gate nào để mở, chạy tiếp chỉ tốn CPU để quyết định một thứ vô nghĩa. Một mẫu gaze cũng bị bỏ qua khi đầu đang **đổi chỗ**, khi camera bị tắt vì quyền riêng tư, và khi detector lock đang do một `look` đang chạy giữ.

**Đổi chỗ, chứ không phải chỉ đang ghi servo.** Có hai trạng thái ghi servo liên tục mà không đưa đầu đi đâu cả: vòng idle đang thở, và một phiên tracking đang bám mặt user. Coi hai thứ đó là "đang di chuyển" thì `last_servo_write` không bao giờ cũ và gần như mọi frame đều bị từ chối — đo thật, idle: ghi được 0.3 mẫu/s trên 4.9/s bị chặn; đo thật, tracking: 0.7/s trên 4.5/s, từ chối một user ở yaw 0.9° với mặt 130px ngay giữa khung chỉ vì cửa sổ có 1 mẫu thay vì 2. Tracking là trường hợp quan trọng nhất: đó chính là lúc đèn đang bám theo mặt user, nên từ chối nhận ra người ta đang nói với nó đúng lúc đó là khoảnh khắc trông hỏng nhất có thể — vì vậy test settle không được phép biến thành `_tracking_active` qua cửa sau. Cả hai đều là chỉnh nhỏ liên tục, góc yaw sống sót qua chúng. Dòng `[gaze] sampling at N/s; blocked: …` tách số frame bị chặn theo từng lý do, vì hai cổng đó sửa ở hai chỗ khác nhau.

Chuỗi end-to-end:
1. `gpio_button.py` / `ttp223.py` detect single click → gọi `single_click_action(source)` trong `button_actions.py`
2. `single_click_action` → `_cancel_agent_speech()` (thread fire-and-forget) + `tracker_service.stop()` nếu đang tracking + `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + thread deferred `_announce_listening()`
2a. `_cancel_agent_speech()` → `POST /api/agent/speech/cancel` lên OS server. Cần vì `stop_tts()` chỉ bịt được thứ HAL đang giữ: câu đang phát cộng hàng đợi đã pre-synth. OS server đẩy câu trả lời theo từng câu, nên không có call này thì thiết bị im đúng một câu rồi nói tiếp. OS server bịt miệng mọi turn đang chạy (xem `docs/os-server.md`) nhưng vẫn cho turn bắt đầu sau cú click nói — nên user chạm xong nói câu mới được ngay kể cả khi còn backlog turn cũ đang chạy nốt. Turn không bị abort, chỉ là không được nói. Chạy trên thread riêng và fire ở cả hai nhánh (unmute mic và stop loa), vì kiểu gì cú chạm cũng có nghĩa là user đang giành lượt nói.
2b. `state.note_music_cancel()` → đóng dấu watermark huỷ nhạc ở phía HAL, và `audio_stop()` chạy ở **cả hai** nhánh (unmute mic và stop loa), không chỉ nhánh stop loa. Cần vì cancel ở OS server chỉ tác động lên TTS: turn bị huỷ vẫn chạy tiếp và tool call nhạc còn treo của nó vẫn tới `POST /audio/play` ngay sau đó, nơi một thread `music-play` mới tự `_stop_event.clear()` — nên một cú stop tại một thời điểm luôn thua cuộc đua này, và user nghe đúng bài nhạc mình vừa huỷ sau khi `yt-dlp` resolve xong (1–5 s). Trong lúc watermark còn tươi (`app_state.MUSIC_CANCEL_GUARD_S`, 3 s), `/audio/play` trả `{"status": "suppressed"}` thay vì phát. Cửa sổ được chọn đủ phủ tool call đang bay nhưng vẫn dưới sàn của một yêu cầu mới thật sự (nói → STT → LLM → tool không bao giờ dưới ~3 s), nên "chạm xong xin bài hát" vẫn chạy bình thường.
3. `stop_tts()` → `tts_service.stop()` set `_stop_event`; mọi blocking loop trong TTS stream (synth, render, playback) check event và abort sạch, không để loa kẹt

### Voice barge-in (tuỳ chọn, mặc định tắt)

Cắt bằng giọng nói — nói trong lúc Lamp đang nói để Lamp dừng và lắng nghe — được gate bởi `HAL_BARGE_IN_ENABLED=true` trong `hal/.env`. Khi bật, `voice_service._monitor_barge_in()` mở mic capture song song trong lúc TTS phát, tính RMS trên block 256ms, gọi `tts_service.stop()` khi N block liên tiếp vượt `HAL_BARGE_IN_RMS_THRESHOLD`. Cùng chuỗi downstream với tap-to-interrupt.

Tại sao tắt mặc định: software-only AEC không khả thi trên hardware này (Speex AEC tích hợp xuống còn ~13-30% reduction dưới TTS multi-chunk streaming). Chỉ với physical separation mic-loa, bleed RMS (1-7500 đo được) và user voice RMS (6-14k đo được) chồng nhau ở zone 7-9k → 1 threshold RMS không discriminate sạch được. Threshold 9000 + 1 frame trigger thiên về 0 false-trigger, đổi lại phải nói lớn để cắt; threshold 6000-7000 thiên ngược lại. Tune theo deployment là không tránh khỏi cho tới khi device có hardware AEC (ví dụ ReSpeaker XVF3800).

Khi bật, tail log để xem `Barge-in monitor session end: max_rms_seen=N` (peak mỗi session) và sự kiện `BARGE-IN: RMS=N`, sau đó set `HAL_BARGE_IN_RMS_THRESHOLD` ở giữa bleed-max và voice-min quan sát được. Tap-to-interrupt vẫn active bất kể.

## Detect nút GPIO (`hal/drivers/gpio_button.py`)

Driver đếm edge nơi **mọi destructive action commit ở rising edge (nhả) dựa trên thời lượng giữ** — không timer nào fire lúc đang giữ. Đây chính là cái cho phép user huỷ giữa chừng (nhả trước ngưỡng) hoặc escalate (giữ tiếp quá 10 s).

1. **Falling edge (nhấn):** ghi `press_start` (đồng hồ monotonic) và spawn thread hold-LED watcher (mỗi lần nhấn 1 thread, có stop `Event` riêng). Không arm timer action nào.
2. **Rising edge (nhả):** dừng LED watcher, tính `held = now − press_start` rồi rẽ nhánh:
   - `held >= 10 s` (`FACTORY_RESET_DURATION`) → scrub mọi click đang chờ, khoá LED đỏ đứng, chạy `factory_reset_action` off-thread.
   - `held >= 5 s` (`LONG_PRESS_DURATION`) → scrub click đang chờ, freeze LED đỏ, chạy `long_press_action` (shutdown) off-thread.
   - `held >= 2 s` (`SLEEP_HOLD_DURATION`) → scrub click đang chờ, chạy `sleep_action` off-thread; nó gọi pipeline emotion `sleepy` chuẩn.
   - khác (tap ngắn) → `click_count += 1` và (re)start click-window timer 0.4 s. Ở tap **đầu tiên** của chuỗi, phần im lặng của `single_click_action` (`announce=False`) fire ngay off-thread — nó không phá huỷ ("cho tôi nói"), nên không cần đợi window. Cue nói được hoãn lại để không nói đè lên chuỗi triple-click đang bấm dở.
3. Khi click window hết:
   - `count == 3` → `triple_click_action` (không cue — chỉ announce reboot)
   - count khác → `announce_listening_cue` phát cue "Nghe đây" đã hoãn, đúng 1 lần mỗi chuỗi; `count == 2` / `>= 4` log thêm ignored (panic-click guard — floor-grab đã chạy ở tap 1, không gì phá huỷ fire)

Release edge không có press khớp (press bị debounce nuốt) thì bỏ qua — `press_start` có thể là cũ, hành động theo nó có thể fire destructive action trên timestamp cũ vài phút. Destructive action chạy trên daemon thread riêng vì callback `lgpio` phải return ngay, nếu không các edge sau sẽ dồn hàng.

### LED feedback khi giữ

Thread watcher poll thời lượng giữ và đẩy LED RGB ở priority HIGH (preempt emotion hiện tại) để user thấy đã arm tới đâu trước khi nhả:

| Thời gian giữ | LED | Ý nghĩa |
|---|---|---|
| < 2 s | giữ nguyên | một tap ngắn |
| 2–5 s | tím sleepy, nháy 2 Hz | đã arm sleepy; nhả ra sẽ vào sleep (LED sau đó tắt) |
| 5–10 s | đỏ, nháy 2 Hz | đã arm shutdown — nhả bây giờ là tắt máy |
| 10 s+ | đỏ, đứng | đã arm factory-reset — nhả bây giờ là wipe + reboot |

Màu tím nhận diện mức sleep; đỏ nháy vs đỏ đứng phân biệt shutdown với factory-reset. LED là no-op im lặng khi RGB service không có (máy dev) — nút vẫn hoạt động.

Debounce mỗi edge là 200 ms (tick nhấn và nhả track độc lập để tap nhanh không bị drop trong khi bounce lặp của cùng một edge bị lọc).

## Detect TTP223 (`hal/drivers/ttp223.py`)

IC TTP223 trên board này chạy ở **FastMode**: output HIGH khi chạm, rồi tự về LOW trong ~50-80 ms dù ngón tay vẫn ở pad. IC chỉ re-trigger khi điện dung thay đổi (ngón tay di chuyển). "Giữ liên tục" là bất khả thi nếu không đổi chân FM của IC sang LowPowerMode (~12 s max touch).

Cross-talk giữa các pad lân cận cũng đáng kể — một lần chạm vật lý fire edge trên 2-4 pad với timing lệch nhau.

Driver bù bằng **mô hình hai tầng**:

### Tầng 1: Session (gap 200 ms)

Bất kỳ edge nào — rising hay falling, pad nào — đều restart timer 200 ms. Khi timer expire (200 ms không edge mới), "session" kết thúc. Một session = một sự kiện chạm logic theo POV user, bất kể bao nhiêu edge vật lý fire bên trong (cross-talk + FastMode auto-LOW).

### Tầng 2: Decision window (1.2 s sau session end)

Sau khi session kết thúc:

1. Nếu **pet cooldown** đang active (head-pat vừa fire gần đây), session bị nuốt im lặng và cooldown được extend. Ngăn `single_click` chen ngang giữa các stroke liên tục.
2. Ngược lại tăng session count. Ở session **đầu tiên** của chuỗi (`_ack_first_session`): nếu TTS đang nói giữa chừng → cắt lời ngay lập tức, rồi chime ack kêu (trung tính với gesture — hợp lệ cho cả tap lẫn nhịp vuốt đầu của pet). Chỉ cắt TTS + chime — nhạc, unmute và cue vẫn đợi phân giải. Trade-off có chủ đích: vuốt đầu Lamp lúc nó đang nói giờ sẽ cắt lời nó (câu giggle pet theo sau) — đổi lấy tap-to-interrupt tức thời.
3. Rồi phân giải:
   - `count >= 2` → fire `head_pat_action` ngay lập tức, arm pet cooldown 1.5 s
   - `count < 2` → schedule decision timer 1.2 s. Khi timer fire với `count == 1`, fire `single_click_action`.

### Hằng số (`ttp223.py`)

| Hằng số | Giá trị | Lý do |
|---|---|---|
| `SESSION_GAP_S` | 0.2 | Vượt thừa burst cross-talk quan sát được (~30-100 ms) mà không gộp các tap thật sự tách biệt |
| `DECISION_WINDOW_S` | 1.2 | Đo thực tế: pace vuốt của user 0.8-1.2 s mỗi nhịp — đủ rộng để stroke đầu của pet không fire single_click thừa |
| `PET_SESSION_THRESHOLD` | 2 | 2 session liên tiếp trong decision window = pet. Dễ hơn 3 vì mỗi "stroke" trên phần cứng này chỉ tạo 1 session |
| `PET_COOLDOWN_S` | 1.5 | Sau pet fire, session thêm trong 1.5 s extend cooldown chứ không bắt đầu count mới. Vuốt liên tục = 1 pet, rồi im |

## Thư viện action chung (`hal/drivers/button_actions.py`)

Các action sống ở một chỗ để nút GPIO, TTP223, và mọi input tương lai (touchpad, remote) hành xử giống nhau:

| Hàm | Làm gì | Cắt TTS đang phát? |
|---|---|---|
| `single_click_action(source)` | Dừng object tracking đang chạy. Sau đó gỡ mute loa do user/scene (bỏ qua khi `_enrolling`). Mic bị mute → unmute. Khác thì stop TTS + stop music. Rồi mở cửa sổ follow-up wake word (no-op khi wake word tắt) và nói câu "Nghe đây" local với retry-on-busy. Tracking vẫn dừng khi hardware mic kill switch đang tắt; action voice vẫn bị chặn. | Có — gọi `stop_tts()` và bản thân câu cue cũng preempt. |
| `triple_click_action(source)` | Nói "Đang khởi động lại" → đợi 5 s cho clip cached → `sudo reboot`. | Có |
| `sleep_action(source)` | Phát thông báo sleep theo ngôn ngữ, rồi gọi `sleepy`: LED tắt, camera/mic/speaker tắt, rồi release servo sau 1 s. | Có — pipeline sleepy dừng TTS/nhạc đang phát sau thông báo. |
| `long_press_action(source)` | Nói "Đang tắt máy" → đợi 5 s → `release_servos()` (để đèn không slam xuống giữa pose) → `sudo shutdown -h now`. | Có |
| `factory_reset_action(source)` | Nói "Đang khôi phục cài đặt gốc. Đang khởi động lại" → `release_servos()` → POST `/api/system/factory-reset` trên OS server (server lo phần wipe + reboot, xem dưới). | Có |
| `head_pat_action(source)` | Chọn ngẫu nhiên 1 câu pet local, nói qua `speak_cached` trên daemon thread. **Không cắt**: nếu TTS vẫn busy thì câu pet bị drop im lặng. Thực tế trên TTP223, session chạm đầu tiên đã cắt lời đang nói (`_grab_floor_if_speaking`) nên tới lúc pet fire thì TTS thường rảnh và câu giggle phát được. | Không |

### Factory-reset: wipe những gì

`factory_reset_action` chỉ **báo + uỷ quyền** — phần reset thật nằm ở OS server (`system/server/system/factoryreset.go`), gọi được từ thiết bị qua loopback không cần Bearer token (authoritative nhờ hiện diện vật lý: giữ 10 s có chủ ý). `POST /api/system/factory-reset` là reset **mềm** (wipe state, không reflash — kernel / package OS / binary / `.venv` HAL không bị đụng):

1. Wipe state của agent backend đang chạy (OpenClaw hoặc Hermes, auto-detect từ `config.json` `agent_runtime`).
2. Wipe các path state của thiết bị: `/root/config` (config.json — API key, channel token, MQTT creds), `/root/local/users` + `/root/local/strangers` (enrollment khuôn mặt/giọng), `/var/lib/hal/snapshots` (snapshot camera), và `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (WiFi nhà → ép vào AP mode lần boot kế).
3. Reboot. Thiết bị lên lại ở AP mode `<device_type>-XXXX` với setup wizard mới (~30 s).

Reset là **single-flight** + cooldown 5 phút (`FactoryResetMinInterval`) dùng chung cho mọi trigger (giữ GPIO, HTTP, MQTT) — circuit breaker chống caller chạy loạn và lặp do vô tình.

## Persist mute/disable qua HAL restart

Mic mute, speaker mute và camera disable mỗi cái persist vào một sidecar
boot-scoped riêng — `/tmp/hal-mic-state.json`, `/tmp/hal-speaker-state.json`,
`/tmp/hal-camera-state.json` (cùng pattern `boot_id` với sidecar LED/scene) —
nên HAL restart (OTA, deploy, đổi config) không còn âm thầm unmute mic, mở lại
speaker hay bật lại camera. Mọi route flip switch đều persist (`/voice/mute|unmute`,
`/speaker/mute|unmute`, `/camera/disable|enable`, scene đổi mic/speaker,
`_auto_camera_on/off`); gesture nút/touchpad đi qua đúng các route đó. Khi
restore: `start_voice` tạo voice pipeline nhưng không mở mic, lifespan trong
`server.py` không start camera capture và vẽ lại đèn báo mic-muted, còn cờ
speaker không cần bước apply (TTS check lúc speak). Reboot nguyên máy thì bắt
đầu fresh (Intern v2 Pro có công tắc gạt tự apply lại). Mute speaker transient
của record-enroll chủ đích KHÔNG persist.

## Phrase local

Thông báo của các action đều local theo `stt_language` từ `config.json` của Lamp. Hằng số ngôn ngữ ở `hal/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Fallback về `DEFAULT_LANG` (English) khi ngôn ngữ hiện tại chưa có bản dịch.

### Thông báo an toàn (1 câu/ngôn ngữ)

`reboot`, `shutdown`, `factory-reset`, và câu cue `listening` dùng phrase nghĩa-đen ("Đang khởi động lại", "Đang tắt máy", "Đang khôi phục cài đặt gốc. Đang khởi động lại") ở mọi ngôn ngữ vì user vừa làm cử chỉ destructive và cần xác nhận rõ ràng — đây là thông báo an toàn, không phải khoảnh khắc persona.

### Phrase pet (15 câu/ngôn ngữ, random)

Phrase pet chọn ngẫu nhiên từ pool 15 câu mỗi ngôn ngữ để Lamp không nói robot khi bị vuốt liên tục. Tone phản ánh tính cách Lamp (AI companion + smart light + expressive robot, "như pet/friend"):

- Nhột / cười nhỏ: "Hihi, nhột quá!" / "Hehe, that tickles!"
- Pet-like kêu rừ rừ: "Mình kêu rừ rừ nè!" / "I'm purring." / "我咕噜咕噜啦！"
- Light-themed (Lamp = luminous): "Mình sáng cả lên rồi nè!" / "You light me up."
- Tim ấm: "Tim mình ấm lên!" / "My heart's glowing."
- Xin thêm: "Vuốt nữa đi mà!" / "More, please!"
- Khen người vuốt: "Mình mê cái này lắm!" / "You're the best."
- Eo nũng: "Vuốt nhẹ thôi nha~" / "Stop it, you!"

Phrase cố tình ngắn — chúng fire giữa lúc vuốt nên cần cảm giác responsive.

## File

| Đường dẫn | Mục đích |
|---|---|
| `hal/drivers/gpio_button.py` | Handler nút GPIO (cơ học, cả hai board) |
| `hal/drivers/ttp223.py` | Handler touchpad cảm ứng TTP223 (chỉ OrangePi sun60) |
| `hal/drivers/button_actions.py` | Hàm action chung + pool phrase local |
| `hal/presets.py` | Hằng số mã ngôn ngữ (`LANG_EN`, v.v.) |
| `hal/test_ttp223_probe_orangepi.py` | Probe độc lập để verify mapping line TTP223 |
| `hal/test_gpio.py` | Probe độc lập để verify line nút GPIO |

Cả hai handler được spawn trong startup lifespan `hal/server.py` — fail thì log nhưng không crash runtime (board không có phần cứng tự skip im lặng).
