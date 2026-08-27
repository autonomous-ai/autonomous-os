# Điều khiển vật lý — Nút GPIO + Touchpad TTP223

Lamp có hai thiết bị input vật lý mà user có thể chạm trực tiếp. Chúng dùng chung thư viện action (`hal/drivers/button_actions.py`) nên cùng một cử chỉ "single click" sẽ hành xử giống nhau dù đến từ nút bấm cơ học hay touchpad cảm ứng.

## Tại sao có hai thiết bị

| Thiết bị | Vai trò | Có ở |
|---|---|---|
| **Nút GPIO** | Một nút bấm cơ. Dùng cho các hành động dứt khoát kể cả destructive (reboot / shutdown / factory-reset). Cảm giác cơ + detect giữ lâu khiến destructive action khó xảy ra do vô tình. | Pi 4/5 và OrangePi sun60 |
| **Touchpad cảm ứng TTP223** | Ba pad chạm xếp như "đầu cún" để vuốt ve + stop/unmute nhẹ. Không có destructive gesture vì FastMode của IC không cho detect giữ lâu tin cậy. | Chỉ OrangePi sun60 (4 Pro / A733) |

## Wiring

| Thiết bị | Pi 4/5 | OrangePi sun60 |
|---|---|---|
| Nút GPIO | gpiochip0 BCM 17 (pull-up, active-LOW) | gpiochip1 line 9 (pull-up, active-LOW) |
| TTP223 | không wire | gpiochip0 line 96 / 98 / 100, **pull-up, active-LOW** (pad nghỉ ở mức HIGH; chạm là edge xuống) |

Cả hai handler đều detect board qua `/proc/device-tree/model`:
- `"sun60iw2"` → OrangePi 4 Pro / A733
- `"raspberry pi 5"` → Pi 5
- `"raspberry pi 4"` → Pi 4
- khác → unknown, cả hai handler bỏ qua không claim GPIO

## Bảng cử chỉ

| Cử chỉ | Nút GPIO | Touchpad TTP223 |
|---|---|---|
| **1 chạm** | Dừng object tracking đang chạy, rồi stop loa / unmute mic + speaker + chime ack (~120 ms ping) — tất cả fire ngay khi nhả nút (không đợi click window); cue "Nghe đây" phát sau khi click window 0.4 s phân giải xong | Tương tự sau khi quyết định tap-vs-pet 1.2 s xong — tracking đang chạy dừng, rồi action mic/loa và cue chạy. Chạm đầu tiên vẫn cắt TTS đang phát và kêu chime ack ngay. |
| **2 chạm** (≤ 0.4 s, nút) / (≤ 1.2 s, TTP223) | Không thêm gì ngoài single-click đã fire ở chạm 1 (panic-click guard) | Pet response. Khi bật `HAL_TOUCH_SWIPE`, hai lần tiếp xúc không rời khỏi một pad là **double tap** → toggle mute mic; khi đó pet nghĩa là một traversal có quay đầu |
| **3 chạm** (≤ 0.4 s, nút) | Reboot OS (TTS báo → `sudo reboot`) | n/a — TTP223 dừng ở 2 (chạm thêm bị cooldown nuốt) |
| **Swipe** qua các pad | n/a | **Chỉ khi bật `HAL_TOUCH_SWIPE`, mặc định tắt.** Một lượt đơn điệu qua cả ba pad → sleep, hoặc wake nếu đang ngủ. Không dùng hướng. |
| **Giữ 2–5 s rồi nhả** | Phát thông báo sleep theo ngôn ngữ, rồi vào `sleepy`: LED tắt, camera/mic/speaker tắt; servo release sau 1 s. Khi đang giữ LED nháy tím sleepy. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 5–10 s rồi nhả** | Shutdown OS (TTS báo → release servo → `sudo shutdown -h now`). LED nháy đỏ khi đã arm. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 10 s+ rồi nhả** | Factory-reset: wipe state thiết bị + reboot vào AP setup (TTS báo → release servo → POST `/api/system/factory-reset` trên OS server). LED đỏ đứng khi đã arm. | n/a |

Gesture giữ chỉ có trên nút GPIO vì nút cơ học cho bằng chứng intent rõ ràng. Mức sleep và các mức destructive **commit khi nhả, không phải khi timer fire lúc đang giữ**. Các mức destructive escalate từ shutdown sang factory-reset sau 10 s (xem "Detect nút GPIO" dưới).

## Cắt Lamp giữa câu (barge-in)

Cử chỉ 1 chạm là **cơ chế barge-in và huỷ attention chính** của Lamp: trước hết nó dừng mọi session object tracking đang chạy; sau đó chạm đỉnh Lamp (touchpad) hoặc nhấn nút GPIO một lần khi Lamp đang nói → cắt câu TTS đang phát giữa chừng, dừng nhạc, unmute mic để Lamp lắng nghe câu kế. Nếu loa đang bị mute bởi user/scene thì cũng được gỡ (trừ khi đang ghi âm enroll giọng) để cue và câu trả lời nghe lại được. Dừng tracking vẫn hoạt động khi hardware mic kill switch đang tắt; nó không wake hoặc unmute mic. Cue "Nghe đây" (theo ngôn ngữ) chỉ phát khi switch cho phép action voice.

Khi wake word đang bật, cú click cũng **được tính như một wake event**: `single_click_action` gọi `voice_service.grant_wakeword_focus(source)`, mở đúng cửa sổ follow-up focus (`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, mặc định 20 s) mà câu wake phrase mở ra. Không có nó thì thiết bị nói "Nghe đây" rồi lại bỏ câu trả lời của user vì thiếu wake phrase. Cửa sổ được kiểm tra lại ở thời điểm dispatch, không chỉ latch lúc mở mic session, nên click giữa lúc session đang chạy vẫn authorize câu user đang nói. No-op khi wake word tắt (mọi câu đã dispatch sẵn) hoặc timeout follow-up = 0.

### Presence enter và quay về phía đèn — trigger wake

Wake gate có **bốn** cửa vào: wake phrase nói ra, single click, một người mới đã nhận diện, và quay về phía đèn trước khi nói. Một `presence.enter` có identity đã enrolled sẽ mở đúng cửa sổ follow-up focus qua `SensingService`, nên người đã nhận diện có thể nói “hello, Leo” mà không cần gọi wake phrase trước. Event chỉ có stranger vẫn được Agent nhìn thấy nhưng mặc định không mở voice focus; họ vẫn có thể dùng wake phrase, click hoặc gaze. Đặt `HAL_PRESENCE_WAKE_STRANGERS=true` cho deployment ưu tiên guest, nơi stranger xuất hiện trong khung có thể bắt đầu hội thoại. Focus chỉ được grant sau khi event presence đã qua cooldown bình thường; nó không tự unmute hoặc tự khởi động mic đang không sẵn sàng.

**Quay mặt về phía đèn rồi nói** cũng mở cùng cửa sổ đó (`hal/drivers/tracking/gaze.py`), qua `voice_service.grant_wakeword_focus(source)` giống presence enter và cú click — mọi thứ phía sau gate không đổi.

Lý do nằm ở hình dạng sản phẩm chứ không phải sở thích. Đèn bàn nằm cách user một cánh tay và trong tầm nhìn cả ngày, nên lặp wake phrase vài chục lần một ngày nghe như đang ra lệnh cho một thiết bị, còn bấm nút thì như đang vận hành máy. Giữa hai người, tín hiệu không phải hai thứ đó: người ta **quay về phía nhau rồi nói**. Các sản phẩm phổ biến hoá "hey <name>" đều không có camera và đặt ở đầu kia phòng, nên không so sánh trực tiếp được.

Hai đặc tính quyết định cách implement:

* **Người ta quay TRƯỚC khi nói, không bao giờ sau.** Bình thường tiếng nói chỉ kích hoạt việc watcher **đọc ngược** ring buffer (`HAL_GAZE_BUFFER_S`, mặc định 4 s) — đúng mô hình pre-roll lookback của mic để không mất âm đầu câu. Có một nhánh recovery: nếu lần đọc này có ít hơn hai mẫu mặt dùng được, VAD yêu cầu watcher khôi phục pose user đã nhớ mà không chặn việc thu audio. Trước khi dispatch **chính transcript đó**, gaze được kiểm tra thêm một lần. Đầu đã đo được là đang quay đi sẽ không vào nhánh này, nên tiếng nói nghe ké vẫn không làm lamp quay về ai đó rồi mở gate.
* **Có mặt người KHÔNG phải tín hiệu.** User ngồi cạnh đèn cả ngày nên "phát hiện có người" gần như luôn đúng và không lọc được gì; "phát hiện có mặt" cũng chỉ hơn chút — mặt quay về màn hình vẫn detect ra. Gate đặt trên **hướng đầu**, đủ chặt để loại tư thế rất thường gặp: nói chuyện với đồng nghiệp trong khi thân vẫn hướng về bàn.

Head yaw suy ra từ 5 landmark mà `YuNet` vốn đã trả về (`detect_face_with_landmarks` trong `detection.py`): độ lệch của mũi so với trung điểm hai mắt, đo **dọc theo đường nối hai mắt** và chuẩn hoá bằng nửa khoảng cách hai mắt, chính là `sin(yaw)` dưới phép chiếu pinhole. Đo dọc đường nối mắt thay vì theo trục x của ảnh là thứ giữ cho đầu **nghiêng** (chống tay lên má) không bị đọc thành đầu quay. Không load thêm model nào, không chạy thêm inference nào; ở `HAL_GAZE_SAMPLE_FPS` (mặc định 6) chi phí là số lẻ trên CPU 8 nhân — đo thật chứ không suy đoán: CPU idle 69.2% xuống 68.8% khi watcher chạy.

Landmark nằm ngoài khung không phải là một phép đo. `YuNet` trả về đủ 5 điểm cho một khuôn mặt bị mép khung cắt hệt như cho khuôn mặt nằm trọn trong khung, và những điểm bị cắt quay về với toạ độ ngoài khung — đo thật trên máy, user ngồi thẳng trước đèn còn camera thì ngắm quá thấp: box `[264, -1, 162, 92]`, hai mắt ở `y = -3.0` và `y = -1.3`. Đưa vào công thức yaw, các toạ độ đó đẩy tỉ số mũi vượt 1, chỗ mà lệnh clamp biến "không đo được" thành đúng `90.0` — không phân biệt được với một khuôn mặt nghiêng thật, và bị đếm là một phiếu **chống** hướng về đèn. Đó chính là lý do user đang nhìn thẳng vào đèn lại cho ra `trail=[90,90,90,90]` và bị từ chối. Nên mẫu nào có mắt hoặc mũi rơi ra ngoài khung sẽ được ghi là **không đo được** — không bỏ phiếu theo chiều nào, giống hệt frame không thấy mặt. Khoé miệng bị cắt thì bỏ qua — góc quay không bao giờ đọc tới chúng.

Trước tất cả những thứ trên, các dòng detector có bbox không phải số hữu hạn bị loại thẳng. YuNet có thể trả về toạ độ vô cực cho một khuôn mặt đang rời khung — quan sát thật trên máy khi đang tracking, `bbox_area` 1.9%, conf 0.29 — và `int()` trên nó ném `OverflowError`, giết luôn thread detect của tracker giữa phiên. Vô cực không phải là "mặt rất to", nó là detector nói rằng không có gì dùng được; nên bỏ dòng đó đi và để đường "frame này không thấy mặt" vốn có xử lý tiếp. Bộ lọc chạy **trước** bước chọn mặt to nhất / gần tâm nhất, vì chiều rộng vô cực thắng mọi cuộc so diện tích và sẽ che mất một khuôn mặt hoàn toàn dùng được.

Khi trong khung có nhiều mặt, mặt được tính là mặt **gần tâm khung nhất** trong số những mặt cao ít nhất `HAL_GAZE_MIN_FACE_PX` — không phải mặt to nhất. Lấy mặt to nhất tức là trao gate cho bất kỳ ai ghé vào gần hơn, và người đó là user chỉ theo thông lệ; chính hướng ngắm của đèn mới là tiên nghiệm tốt hơn cho câu hỏi nó đang chĩa vào mặt nào. Khi chỉ có một mặt đạt ngưỡng thì hai luật cho cùng kết quả, nên thay đổi này chỉ có tác dụng khi thực sự có người thứ hai chung bàn. Nếu không ai qua ngưỡng kích thước thì vẫn trả về mặt to nhất, để mẫu vẫn ghi nhận là có người. Lưu ý đường tracking chỉ lấy bbox (`_detect_face_yunet`, dùng cho object follow) vẫn giữ chính sách mặt-to-nhất của riêng nó — hai bên độc lập.

| Env var | Mặc định | Chỉnh cái gì |
|---|---|---|
| `HAL_GAZE_WAKE` | `false` | Công tắc tổng cho **toàn bộ watcher**, không chỉ riêng cửa gaze: `start()` thoát ngay khi tắt, nên canh giữa theo chiều dọc, leo tìm mặt, xoay ngang, repoint và quét tự động (xem `vision-tracking_vi.md`) cũng không chạy. Tắt vẫn giữ cửa wake phrase, click và presence enter. Image của lamp đặt `true`. |
| `HAL_PRESENCE_WAKE_STRANGERS` | `false` | Cho `presence.enter` chỉ có stranger mở voice focus. Để tắt nếu guest phải dùng tín hiệu nói ra, chạm hoặc gaze. |
| `HAL_GAZE_SHADOW` | `true` | Chỉ log quyết định, không mở gate. Không tốn gì — không turn nào mở nên không tốn LLM hay TTS. |
| `HAL_GAZE_MAX_YAW_DEG` | 25 | Nón chấp nhận ở giữa khung. |
| `HAL_GAZE_EDGE_CONE_SCALE` | 1.8 | Nón nới rộng bao nhiêu ở rìa khung, nơi barrel distortion thổi phồng góc. |
| `HAL_GAZE_MIN_FACE_PX` | 48 | Chiều cao mặt tối thiểu **tính bằng pixel của khung đã thu nhỏ** — watcher nhận diện trên `frame_utils.downscale(frame)`, hàm này kẹp chiều rộng về `VISION_MAX_WIDTH` (640), nên ở 1280×720 ngưỡng này là 96 px trên ảnh gốc, còn ở 640 hoặc nhỏ hơn thì là 48 px trên cả hai. Dưới ngưỡng này landmark chỉ cách nhau vài pixel, góc tính ra là số học trên sai số làm tròn, nên mẫu đó không được bỏ phiếu. Khác `LOOK_AIM_MIN_FACE_HEIGHT_FRAC` vốn là tỉ lệ nên miễn nhiễm, giá trị này âm thầm gấp đôi hoặc giảm nửa nếu chế độ camera đổi. |
| `HAL_GAZE_WINDOW_S` | 1.5 | Cửa sổ bằng chứng, kết thúc tại thời điểm bắt đầu nói. |
| `HAL_GAZE_MIN_FACING_RATIO` | 0.6 | Tỉ lệ mẫu trong cửa sổ phải thấy đầu hướng về đèn. Là TỈ LỆ, không phải chuỗi liên tục — yaw từng mẫu nhiễu thật. |
| `HAL_GAZE_MIN_SAMPLES` | 2 | Dưới mức này không đủ bằng chứng để kết luận theo chiều nào. Vòng lặp thực tế chỉ đạt ~2 mẫu/s dù cấu hình bao nhiêu — nó bị chặn bởi việc lấy frame và chạy detector — nên để 3 là loại oan cả user mà mọi tầng khác đều đồng ý là đang nhìn đèn. Dòng log `[gaze] sampling at N/s` đếm số mẫu THỰC SỰ ghi được, và báo riêng số frame bị chặn trước khi kịp đo (đang chờ servo ổn định, hoặc detector đang bị một lệnh `look` giữ). Đếm số lần thử thay vì số mẫu từng báo 5.7/s trong khi buffer không có gì mới hơn cửa sổ 1.5 s — tức dưới 1 mẫu/s bằng chứng thật. |
| `HAL_GAZE_SAMPLE_FPS` | 6 | Tần suất lấy mẫu. Cử chỉ thì chậm, nhưng quyết định là một cuộc bỏ phiếu và chỉ mẫu đo được mới tính — ở 3 fps cửa sổ thường chỉ còn một mẫu dùng được, từ chối cả user đang nhìn thẳng vào đèn. |
| `HAL_GAZE_BUFFER_S` | 4.0 | Lịch sử yaw giữ lại. Phải lớn hơn `WINDOW_S` để phần đọc ngược nhìn đủ xa về trước. Đã có lúc phải gấp đôi, vì một phép kiểm tra transition nay đã bị gỡ bỏ; giữ 4.0 vì thêm một giây không tốn gì và `trail=` đọc dễ hơn khi có nhiều lịch sử phía sau. |
| `HAL_GAZE_WAKE_FOCUS_S` | 10 | Cửa sổ follow-up mà một lần wake bằng *gaze* mở ra, ngắn hơn 20 s của wake phrase hay click. Một cái liếc mắt đòi hỏi ít hơn một hành động có chủ ý. Bị chặn trên bởi `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, không bao giờ vượt qua. |
| `HAL_GAZE_COOLDOWN_S` | 5 | Khoảng cách tối thiểu giữa hai lần gaze mở gate. |
| `HAL_GAZE_REPOINT` | `true` | Quay về bearing đã nhớ khi lâu không thấy ai. |
| `HAL_GAZE_REPOINT_AFTER_S` | 12 | Phải vắng mặt bao lâu mới quay. Recovery do voice kích hoạt khi không có evidence sẽ bỏ qua khoảng chờ này, nhưng không bỏ qua cooldown di chuyển. |
| `HAL_GAZE_REPOINT_COOLDOWN_S` | 60 | Tối đa một lần quay trong khoảng này, kể cả recovery do voice kích hoạt. |
| `HAL_GAZE_REPOINT_MIN_CONFIDENCE` | 0.2 | Dưới confidence này thì bearing không đáng để quay. Khớp với ngưỡng của chính look-aim: ở 0.5 watcher từ chối đúng những bearing mà aim và search vẫn đang dùng bình thường — một bearing đủ tốt để ngắm cho một turn hội thoại đang chạy thì cũng đủ tốt để quay đầu về phía đó giữa hai turn. |
| `HAL_GAZE_REPOINT_SKIP_IF_FACE_S` | 3 | Từ chối reacquire do speech kích hoạt nếu vừa thấy mặt trong khoảng này. Sau khi leo tìm đã thấy mặt user *cao hơn* bearing, tuân theo bearing nghĩa là quay ngược xuống nhìn vào chỗ không có ai. |
| `HAL_GAZE_WELL_FRAMED_EDGE` | 0.6 | Mặt được lệch khỏi tâm khung bao nhiêu mà vẫn tính là "có người ở đây, không cần quay". Mặt sát rìa là mặt sắp ra khỏi khung; coi nó là đã vào khung tử tế chính là thứ khiến bộ đếm vắng mặt reset mãi mãi trong khi user trôi dần ra khỏi tầm nhìn — đo được ở edge 0,71–0,75 mà đèn vẫn từ chối repoint. |

Image của lamp chủ động override `HAL_GAZE_MAX_YAW_DEG` thành **60°**. Đây là calibration riêng cho thiết bị, không phải mặc định chung: trên lamp-0c89, YuNet đo user đang nhìn thẳng vào camera qua kính thành 55,7–59,1°. Ngưỡng tối thiểu hai mẫu hợp lệ và phiếu bầu 60% vẫn giữ nguyên, nên một frame đơn lẻ vẫn không đủ để mở gate.

Hai tham số trong đó là **đo ra**, không phải chọn. `MIN_FACE_PX` có vì probe trên thiết bị bắt được ba đồng nghiệp ở nền cỡ 8–18 px cho ra yaw 49 / 20 / 29 — nhiễu thuần — bên cạnh người dùng ngồi tại bàn cỡ 78 px với yaw 90 hoàn toàn đúng; hai nhóm không chồng lấn nên ngưỡng này xoá cả một lớp rác chứ không phải chỉnh cho vừa. `MIN_FACING_RATIO` có vì trail của một người ngồi yên đọc ra `[10,15,8,25,36,1,-,90]`, mức dao động mà không cái đầu nào làm được, nên mọi luật đòi MỌI mẫu phải đạt đều sẽ loại oan họ.

Lâu không thấy ai mà đèn tự quay: đó là `REPOINT`. Trước đây nó là thứ **duy nhất** trong watcher động vào thân đèn; giờ thì không còn — canh giữa theo chiều dọc, leo tìm mặt, xoay ngang và quét tự động đều động vào thân đèn, và tất cả được mô tả trong `vision-tracking_vi.md` chứ không phải ở đây, vì chúng nói về việc *đưa user vào khung hình* chứ không phải về việc mở gate. Recording idle là một vòng lặp các pose tuyệt đối, đảo `base_pitch` khoảng 17° mỗi chu kỳ, nên dù đặt đèn ở đâu thì idle cũng kéo camera về pose ghi sẵn của chính nó — trên bàn làm việc thì đó là bàn phím. Đặt pose đã nhớ một lần sẽ bị vòng lặp kế tiếp ghi đè; muốn đậu đúng ở bearing thì phải offset toàn bộ playback theo bearing, việc đó thuộc về motion playback chứ không thuộc tính năng này. Nên đèn làm điều mà con người làm: không thấy ai có thể đang nói với mình thì quay về chỗ người đó hay ngồi, một lần, rồi chờ.

Shadow mode tồn tại chính để một buổi chạy cạnh user thật cho ra số liệu (`[gaze] speech: yaw=… facing=…%/…% -> WOULD_WAKE`) đủ để chốt các ngưỡng trên.

**Thực sự phải đúng những gì thì gaze mới arm.** `HAL_GAZE_WAKE` tự gọi mình là công tắc tổng, và nó là điều kiện cần chứ không đủ — có bốn điều kiện, và ba trong số đó nằm ở chỗ khác chứ không phải bảng gaze:

| # | Điều kiện | Nằm ở đâu |
|---|---|---|
| 1 | `LOOK_AIM_ENABLED` | biến môi trường `HAL_LOOK_AIM` — cả watcher lẫn bearing sampler đều khởi động *bên trong* khối look-aim (`hal/server.py:816`) |
| 2 | có camera trong mount plan | khai báo thiết bị — `"camera" in _plan.mounted` |
| 3 | wake word đang bật | **`config.json` của os-server, key `wakeword`** — đọc qua `_os_cfg_get("wakeword", False)`. **Không có** biến môi trường `HAL_WAKEWORD_ENABLED`; đặt nó ra cũng không có tác dụng gì |
| 4 | `HAL_GAZE_WAKE` | bảng gaze ở trên |

Tắt look-aim là điều kiện dễ bất ngờ nhất: nó âm thầm tắt luôn cửa wake thứ ba *và* bộ học bearing thụ động, mà không chỗ nào trong hai thứ đó nhắc tới look-aim. Nếu watcher không chạy mà bảng trông vẫn đúng, hãy kiểm tra 1–3 trước khi nghi 4 — dòng log cần tìm là `[gaze] not starting: wake word disabled, nothing to gate`.

Suy biến sạch theo cả hai chiều. Máy **không có camera** thì gaze lẫn presence enter từ camera đều không thể arm, còn cửa wake phrase và click vẫn nguyên vẹn — không cần cấu hình riêng. Khi wake word **tắt** thì watcher không khởi động luôn: không có wake word thì mọi câu đã dispatch sẵn, không còn gate nào để mở, chạy tiếp chỉ tốn CPU để quyết định một thứ vô nghĩa. Một mẫu gaze cũng bị bỏ qua khi đầu đang **đổi chỗ**, khi camera bị tắt vì quyền riêng tư, và khi detector lock đang do một `look` đang chạy giữ.

**Đổi chỗ, chứ không phải chỉ đang ghi servo.** Có hai trạng thái ghi servo liên tục mà không đưa đầu đi đâu cả: vòng idle đang thở, và một phiên tracking đang bám mặt user. Coi hai thứ đó là "đang di chuyển" thì `last_servo_write` không bao giờ cũ và gần như mọi frame đều bị từ chối — đo thật, idle: ghi được 0.3 mẫu/s trên 4.9/s bị chặn; đo thật, tracking: 0.7/s trên 4.5/s, từ chối một user ở yaw 0.9° với mặt 130px ngay giữa khung chỉ vì cửa sổ có 1 mẫu thay vì 2. Tracking là trường hợp quan trọng nhất: đó chính là lúc đèn đang bám theo mặt user, nên từ chối nhận ra người ta đang nói với nó đúng lúc đó là khoảnh khắc trông hỏng nhất có thể — vì vậy test settle không được phép biến thành `_tracking_active` qua cửa sau. Cả hai đều là chỉnh nhỏ liên tục, góc yaw sống sót qua chúng. Dòng `[gaze] sampling at N/s; blocked: …` tách số frame bị chặn theo từng lý do, vì hai cổng đó sửa ở hai chỗ khác nhau.

Chuỗi end-to-end:
1. `gpio_button.py` / `ttp223.py` detect single click → gọi `single_click_action(source)` trong `button_actions.py`
2. `single_click_action` → `_cancel_agent_speech()` (thread fire-and-forget) + `tracker_service.stop()` nếu đang tracking + `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + thread deferred `_announce_listening()`
2a. `_cancel_agent_speech()` → `POST /api/agent/speech/cancel` lên OS server. Cần vì `stop_tts()` chỉ bịt được thứ HAL đang giữ: câu đang phát cộng hàng đợi đã pre-synth. OS server đẩy câu trả lời theo từng câu, nên không có call này thì thiết bị im đúng một câu rồi nói tiếp. OS server bịt miệng mọi turn đang chạy (xem `docs/os-server.md`) nhưng vẫn cho turn bắt đầu sau cú click nói — nên user chạm xong nói câu mới được ngay kể cả khi còn backlog turn cũ đang chạy nốt. Turn không bị abort, chỉ là không được nói. Chạy trên thread riêng và fire ở cả hai nhánh (unmute mic và stop loa), vì kiểu gì cú chạm cũng có nghĩa là user đang giành lượt nói.
2b. `state.note_music_cancel()` → đóng dấu watermark huỷ nhạc ở phía HAL, và `audio_stop()` chạy ở **cả hai** nhánh (unmute mic và stop loa), không chỉ nhánh stop loa. Cần vì cancel ở OS server chỉ tác động lên TTS: turn bị huỷ vẫn chạy tiếp và tool call nhạc còn treo của nó vẫn tới `POST /audio/play` ngay sau đó, nơi một thread `music-play` mới tự `_stop_event.clear()` — nên một cú stop tại một thời điểm luôn thua cuộc đua này, và user nghe đúng bài nhạc mình vừa huỷ sau khi `yt-dlp` resolve xong (1–5 s). Trong lúc watermark còn tươi (`app_state.MUSIC_CANCEL_GUARD_S`, 3 s), `/audio/play` trả `{"status": "suppressed"}` thay vì phát. Cửa sổ được chọn đủ phủ tool call đang bay nhưng vẫn dưới sàn của một yêu cầu mới thật sự (nói → STT → LLM → tool không bao giờ dưới ~3 s), nên "chạm xong xin bài hát" vẫn chạy bình thường.
3. `stop_tts()` → `tts_service.stop()` set `_stop_event`; mọi blocking loop trong TTS stream (synth, render, playback) check event và abort sạch, không để loa kẹt

### Voice barge-in (tắt trong profile lamp)

Cắt bằng giọng nói — nói trong lúc Lamp đang nói để Lamp dừng và lắng nghe — theo `HAL_BARGE_IN_ENABLED`, vốn mặc định bằng `HAL_AEC_ENABLED` — trong code là `false`. Profile lamp bật AEC nhưng chủ động đặt barge-in là `false`: mic USB đang dùng ở quá gần loa so với tuning khử vọng hiện tại. Cắt lời bằng chạm vẫn hoạt động.

Khi barge-in được bật, đường đang chạy là vòng **warm mic**, không phải `_monitor_barge_in()`. Với `HAL_WARM_MIC=true` (mặc định), `arecord` vẫn mở suốt lúc phát và vòng capture rút rồi bỏ frame; barge-in được phát hiện ngay ở đó, trên chính frame 64 ms của vòng lặp, khi `HAL_BARGE_IN_WARM_FRAMES` frame liên tiếp vượt `HAL_BARGE_IN_RMS_THRESHOLD` **và** Silero đồng ý đó là tiếng nói **và** `aec.uncancelled()` xác nhận frame đó thật sự đã được khử. `_monitor_barge_in()` (256 ms blocks, chỉ xét mức) là đường cũ và không thể tới được khi warm mic bật — `HAL_BARGE_IN_BLOCK_MS` và `HAL_BARGE_IN_TRIGGER_FRAMES` chỉ định cỡ cho đường đó. Chuỗi downstream giống tap-to-interrupt.

**Hai mức vẫn chồng nhau, và không threshold nào tách được.** Đo trên `lamp-ee17` (loa 25 %, `HAL_AEC_DELAY_MS=205`) với gate đặt tạm ở 30000 để không gì kích được, ba lượt trả lời đầy đủ trong phòng im lặng đạt đỉnh **9804 / 6510 / 7849** — đó là trần vọng âm. Một lần cắt lời thật đã xác nhận trên cùng máy đo được **8027**, tức *thấp hơn* trần đó. Vậy nên threshold dưới trần sẽ tự cắt lời mình (ở 4500 nó kích ở 5530 / 6446 / 6637 / 7749, hai lần chuyển chính lời Lamp thành lượt của người dùng), còn threshold trên trần sẽ bỏ sót những lần cắt lời nói nhỏ. Muốn tách được cần phép thử envelope-decorrelation — vọng âm bám theo envelope đầu xa, con người thì không — hiện chưa làm. Mặc định 5000 cố ý thiên về việc bắt được giọng nói bình thường; nâng dần lên 11000 để đổi theo hướng ngược lại.

Đừng kỳ vọng cổng Silero loại được giọng của chính Lamp: vọng âm *là* tiếng nói, và nó đạt 0.50, 0.75 và 1.00 ở các sự kiện khác nhau, trong khi các lần cắt lời thật đạt 0.08, 0.88 và 1.00. Nó loại tiếng động lớn không phải giọng nói (đập cửa, chìa khoá, ho); phần còn lại do mức RMS lo.

Để đặc tả một deployment mới: đặt tạm `HAL_BARGE_IN_RMS_THRESHOLD` ở 30000, không nói gì, và đọc dòng `drain peak RMS=… , longest run N frames` mà mỗi lượt trả lời ghi ra. Tap-to-interrupt vẫn active bất kể.

## Detect nút GPIO (`hal/drivers/gpio_button.py`)

Driver đếm edge nơi **mọi destructive action commit ở rising edge (nhả) dựa trên thời lượng giữ** — không timer nào fire lúc đang giữ. Đây chính là cái cho phép user huỷ giữa chừng (nhả trước ngưỡng) hoặc escalate (giữ tiếp quá 10 s).

1. **Falling edge (nhấn):** ghi `press_start` (đồng hồ monotonic) và spawn thread hold-LED watcher (mỗi lần nhấn 1 thread, có stop `Event` riêng). Không arm timer action nào.
2. **Rising edge (nhả):** dừng LED watcher, tính `held = now − press_start`, scrub click đang chờ cho mọi hold từ 2 s trở lên, rồi chốt LED feedback (đỏ đứng cho shutdown/factory reset). Sau đó nó truyền duration vào `hold_release_action(held, source)` off-thread. Mapping action này chọn:
   - `held >= 10 s` (`FACTORY_RESET_DURATION`) → `factory_reset_action`.
   - `held >= 5 s` (`LONG_PRESS_DURATION`) → `shutdown_action`.
   - `held >= 2 s` (`SLEEP_HOLD_DURATION`) → `sleep_action`, hàm gọi pipeline emotion `sleepy` chuẩn.
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

Ba màu này là preset chứ không phải hằng nhúng cứng trong driver: `BUTTON_LED_PRESETS` trong `hal/presets.py` (`sleep_warn` / `shutdown_warn` / `factory_reset`), device override được qua section `button_led` của `robots/<id>/presets.json` giống mọi bảng LED khác. Driver giữ phần staging — lúc nào nháy, lúc nào để đứng — và đọc màu ngay lúc paint, vì overlay merge bảng tại chỗ lúc boot.

Debounce mỗi edge là 200 ms (tick nhấn và nhả track độc lập để tap nhanh không bị drop trong khi bounce lặp của cùng một edge bị lọc).

## Detect TTP223 (`hal/drivers/ttp223.py`)

IC TTP223 trên board này chạy ở **FastMode**: output HIGH khi chạm, rồi tự về LOW trong ~50-80 ms dù ngón tay vẫn ở pad. IC chỉ re-trigger khi điện dung thay đổi (ngón tay di chuyển). "Giữ liên tục" là bất khả thi nếu không đổi chân FM của IC sang LowPowerMode (~12 s max touch).

Cross-talk giữa các pad lân cận cũng đáng kể — một lần chạm vật lý fire edge trên 2-3 pad với timing lệch nhau (con số 2-4 có từ trước khi dời chân pad, lúc còn wire bốn pad).

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

### Tầng 3: Phân loại traversal (`HAL_TOUCH_SWIPE`, mặc định TẮT)

**Mặc định tắt.** Đợt đo có nhãn lẽ ra phải cho phép tính năng này vẫn chưa chạy, nên khi không đặt cờ, driver hành xử đúng như bản hai-cử-chỉ ở trên.

Khi bật, driver giữ lại thứ tự các pad được chạm lần đầu — các lần lặp liên tiếp bị gộp, vì FastMode kích lại dưới một ngón tay đứng yên không phải là một cú di chuyển — rồi phân loại dựa trên việc thứ tự đó có **quay đầu** hay không. Đảo chiều, chứ không phải thời gian, mới là yếu tố phân biệt: swipe là một lượt đơn điệu, còn vuốt ve thì quay đầu ít nhất một lần. Điều đó cũng có nghĩa là đảo chiều quyết định được ngay khi nó xảy ra, nên **pet giữ được đường phản hồi nhanh**, chỉ các trường hợp nhập nhằng mới phải chờ hết cửa sổ quyết định.

Thứ tự phân giải, khớp cái nào trước thì thắng:

1. **SWIPE** — đủ cả ba pad, đơn điệu, mọi khoảng liền kề ≥ `HAL_TOUCH_SWIPE_MIN_GAP_MS`. Thứ tự quan trọng: kiểm tra trước để một cú swipe đã phân giải không bao giờ bắn thêm một tap. → sleep, hoặc wake nếu đang ngủ.
2. **PET** — có đảo chiều (bắn ngay tại cuối session), hoặc ≥2 lần tiếp xúc trải trên ≥2 pad mà không có đảo chiều rõ ràng (phương án dự phòng theo số đếm, để một cú vuốt nhiễu không bị câm).
3. **DOUBLE TAP** — ≥2 lần tiếp xúc không rời khỏi một pad. → toggle mute mic.
4. **TAP** — mọi trường hợp còn lại.

**Một điểm đụng độ cần biết.** "Hai lần tiếp xúc, cùng một pad" đồng thời là chữ ký của double tap và chữ ký của phương án dự phòng pet theo số đếm, và hai cái đó không phân biệt được ở mức tín hiệu. Double tap thắng. Nên một cú vuốt nhiễu tới mức chỉ ghi nhận được một pad sẽ **mute mic thay vì cười khúc khích**. Đó là cái giá đã chấp nhận khi gán hành động cho double tap; điều kiện `≥2 pad` ở luật 2 là thứ giữ trường hợp phổ biến hơn ở lại phía pet.

| Biến env | Mặc định | Điều chỉnh |
|---|---|---|
| `HAL_TOUCH_SWIPE` | `false` | Công tắc chính cho luật 1–3. Tắt thì khôi phục đúng hành vi hai-cử-chỉ. |
| `HAL_TOUCH_SWIPE_MIN_GAP_MS` | 40 | Khoảng cách tối thiểu giữa hai pad liền kề để một traversal được tính là có chủ đích thay vì cross-talk. **Tạm thời** — dữ liệu từ orange-lamp (n=36, chưa gán nhãn) cho thấy delta giữa các pad nằm trên một phân bố liên tục 7–345 ms không có khoảng trống, nên chưa thể đặt giá trị này từ bằng chứng. Cần hiệu chỉnh lại từ một đợt chạy có nhãn; `HAL_TOUCH_DEBUG` ghi lại chính các delta dùng để đo. |

`boards.json` có thêm trường tùy chọn `axis` trong mục `touch` — các line theo thứ tự vật lý trái sang phải, ví dụ `"axis": [96, 100, 98]`. Hiện tại nó **vắng mặt**: thứ tự line không phải thứ tự không gian trên board này, và chỉ một đợt chạy có nhãn nhấn từng pad một mới xác định được. Khi vắng, phân loại lùi về thứ tự line khai báo. Một axis sai chỉ làm sai **hướng** swipe, thứ mà driver cố ý không dùng — quay đầu vẫn là quay đầu bất kể pad được đánh số theo chiều nào.


### Hằng số (`ttp223.py`)

| Hằng số | Giá trị | Lý do |
|---|---|---|
| `SESSION_GAP_S` | 0.2 | Vượt thừa burst cross-talk quan sát được (~30-100 ms) mà không gộp các tap thật sự tách biệt |
| `DECISION_WINDOW_S` | 1.2 | Đo thực tế: pace vuốt của user 0.8-1.2 s mỗi nhịp — đủ rộng để stroke đầu của pet không fire single_click thừa |
| `PET_SESSION_THRESHOLD` | 2 | 2 session liên tiếp trong decision window = pet. Dễ hơn 3 vì mỗi "stroke" trên phần cứng này chỉ tạo 1 session |
| `PET_COOLDOWN_S` | 1.5 | Sau pet fire, session thêm trong 1.5 s extend cooldown chứ không bắt đầu count mới. Vuốt liên tục = 1 pet, rồi im |

### Trace lại chuyện thực sự đã xảy ra (`HAL_TOUCH_DEBUG`)

Hai trong bốn dòng log quyết định ở trên là `logger.debug` nên không bao giờ xuất hiện ở mức `HAL_LOG_LEVEL=INFO` đang ship, còn `_on_edge` thì vứt bỏ thông tin pad nào đã bắn trước khi có gì kịp ghi lại. Vì vậy khi một cú chạm làm sai, bình thường không có cách nào biết được là pad bắn nhầm, tầng session gộp sai, hay action làm điều gì đó ngoài dự tính.

`hal/drivers/touch_debug.py` bịt khoảng trống đó. **Mặc định TẮT** — khi không đặt env, nó tốn đúng một phép kiểm tra boolean đã cache cho mỗi edge, không mở file nào và không tạo thread nào. Đặt `HAL_TOUCH_DEBUG=1` trong `/opt/hal/.env` rồi restart HAL để bật.

Nó ghi một file JSON cho mỗi cử chỉ đã phân giải, đặt tên `<timestamp>_<ACTION>.json` để một phân loại sai nhìn thấy được ngay từ `ls` (`20260827-114032_TAP.json`, `..._PET.json`, `..._IGNORED-pet_cooldown.json`, `..._IGNORED-settle.json`). Mỗi file chứa bốn tầng: `edges` (line nào, mức nào, lúc nào, và có bị chốt chặn `SETTLE_S` chặn không), `sessions` (tầng 200 ms đã gộp chúng ra sao, kèm `primary_pad`, `adjacent_deltas_ms` và `span_ms` của từng lần tiếp xúc), `traversal` (chuỗi pad trải qua nhiều session và số `reversals`) và `action` (cái gì đã chạy, trên trạng thái thiết bị nào). Ngoài ra còn một dòng tóm tắt `TOUCH-TRACE` ghi ở mức INFO.

Nó cố ý không bao giờ log vào journald: HAL log nhiều đến mức cửa sổ journal của `hal.service` chỉ tính bằng phút, nên một bản trace để ở đó sẽ bị đẩy mất trước khi kịp đọc.

| Biến env | Mặc định | Điều chỉnh |
|---|---|---|
| `HAL_TOUCH_DEBUG` | `false` | Công tắc chính. Tắt = mọi điểm vào đều là no-op. |
| `HAL_TOUCH_DEBUG_DIR` | `touch_logs/` cạnh module | Thư mục output. Rơi về thư mục tạm nếu cây mã chỉ đọc. |
| `HAL_TOUCH_DEBUG_MAX_ENTRIES` | 200 | Giới hạn số file, cũ nhất bị dọn ở mỗi lần ghi. 0 = không giới hạn. |
| `HAL_TOUCH_DEBUG_PADS` | _(không đặt)_ | Map line→nhãn, ví dụ `96=S1,98=S2,100=S4`. Không đặt thì pad được đặt tên theo số line — các tên S lịch sử không đi theo thứ tự line sau hai lần dời chân, nên driver không đoán chúng. |


## Thư viện action chung (`hal/drivers/button_actions.py`)

Các action sống ở một chỗ để nút GPIO, TTP223, và mọi input tương lai (touchpad, remote) hành xử giống nhau:

| Hàm | Làm gì | Cắt TTS đang phát? |
|---|---|---|
| `single_click_action(source)` | Dừng object tracking đang chạy. Sau đó gỡ mute loa do user/scene (bỏ qua khi `_enrolling`). Đóng dấu watermark hủy nhạc và dừng nhạc — ở **cả hai** nhánh, để một cú click luôn dập được thứ ồn nhất trong phòng. Rồi nếu mic bị mute → unmute; ngược lại thì stop TTS. Rồi mở cửa sổ follow-up wake word (no-op khi wake word tắt) và nói câu "Nghe đây" local với retry-on-busy. Tracking vẫn dừng khi hardware mic kill switch đang tắt; action voice vẫn bị chặn. | Có — gọi `stop_tts()` và bản thân câu cue cũng preempt. |
| `triple_click_action(source)` | Chỉ map gesture: gọi `reboot_action(source)`. | Có |
| `reboot_action(source)` | Nói "Đang khởi động lại" → đợi 5 s cho clip cached → `reboot_os()` (`sudo reboot`). | Có |
| `sleep_action(source)` | Phát thông báo sleep theo ngôn ngữ, rồi gọi `sleepy`: LED tắt, camera/mic/speaker tắt, rồi release servo sau 1 s. | Có — pipeline sleepy dừng TTS/nhạc đang phát sau thông báo. |
| `hold_release_action(held, source)` | Mapping signal hold: chọn sleep, shutdown hoặc factory reset theo duration lúc nhả. | Tuỳ action được chọn |
| `shutdown_action(source)` | Nói "Đang tắt máy" → đợi 5 s → `release_servos()` (để đèn không slam xuống giữa pose) → `shutdown_os()` (`sudo shutdown -h now`). | Có |
| `factory_reset_action(source)` | Nói "Đang khôi phục cài đặt gốc. Đang khởi động lại" → `release_servos()` → POST `/api/system/factory-reset` trên OS server (server lo phần wipe + reboot, xem dưới). | Có |
| `swipe_action(source)` | Toggle sleep/wake: đánh thức nếu `_sleeping`, ngược lại gọi `sleep_action`. Không dựa vào hướng — một cú swipe "sai chiều" sẽ không làm gì mà cũng không có phản hồi giải thích vì sao. | Tùy action được chọn |
| `mic_toggle_action(source)` | Toggle mute mic cho một double tap đã phân giải. Từ chối khi công tắc mic phần cứng đang tắt hoặc đang ghi âm enroll giọng. Đèn mic-muted là xác nhận duy nhất — mic đã mute thì không có ack bằng âm thanh. | Không |
| `head_pat_action(source)` | Chọn ngẫu nhiên 1 câu pet local, nói qua `speak_cached` trên daemon thread. **Không cắt**: nếu TTS vẫn busy thì câu pet bị drop im lặng. Thực tế trên TTP223, session chạm đầu tiên đã cắt lời đang nói và phát tiếng ack chime (`_ack_first_session`) nên tới lúc pet fire thì TTS thường rảnh và câu giggle phát được. | Không |

### Factory-reset: wipe những gì

`factory_reset_action` chỉ **báo + uỷ quyền** — phần reset thật nằm ở OS server (`system/server/system/factoryreset.go`), gọi được từ thiết bị qua loopback không cần Bearer token (authoritative nhờ hiện diện vật lý: giữ 10 s có chủ ý). `POST /api/system/factory-reset` là reset **mềm** (wipe state, không reflash — kernel / package OS / binary / `.venv` HAL không bị đụng):

1. Wipe state của agent backend đang chạy (OpenClaw hoặc Hermes, auto-detect từ `config.json` `agent_runtime`).
2. Wipe các path state của thiết bị: `/root/config` (config.json — API key, channel token, MQTT creds), `/root/local/users` + `/root/local/strangers` (enrollment khuôn mặt/giọng), `/var/lib/hal/snapshots` (snapshot camera), và `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (WiFi nhà → ép vào AP mode lần boot kế).
3. Reboot. Thiết bị lên lại ở AP mode `<device_type>-XXXX` với setup wizard mới (~30 s).

Reset là **single-flight** + cooldown 5 phút (`FactoryResetMinInterval`) dùng chung cho mọi trigger (giữ GPIO, HTTP, MQTT) — circuit breaker chống caller chạy loạn và lặp do vô tình.

## Persist mute/disable qua HAL restart

**Sleep cũng persist theo cách này** (`/tmp/hal-sleep-state.json`). Nó cùng loại
với các switch người dùng thấy được: ai đó — hoặc một scene ban đêm — đã cho
thiết bị ngủ, và restart HAL không được phép huỷ điều đó. OTA thì restart HAL,
nên trước khi có sidecar này, một lần update lúc 3 giờ sáng là thiết bị tỉnh dậy:
đèn sáng lại, mic nghe lại, sensing hết bị gate. Sidecar này còn mang theo mute mic/loa **do chính sleep sở hữu** — chúng cố ý không nằm trong sidecar mic/speaker để lúc thức trả switch về đúng lựa chọn của user, mà hệ quả trước đây là restart xong máy nghe lại được và một turn agent còn đang bay vẫn nói thành tiếng. `POST /emotion` ghi cờ mỗi lần
nó đổi, và lifespan trong `server.py` express lại `sleepy` sau khi driver đã lên,
để thiết bị TRÔNG vẫn đang ngủ chứ không phải boot vào look nghỉ với cái cờ được
set âm thầm. Driver chuyển động cũng được yêu cầu khởi động **không** kèm chuỗi
thức dậy (`start(skip_wake=True)`): startup pose là một cú move 5 giây rồi tới
idle loop, nên sửa sau nghĩa là con lamp đang ngủ vẫn đứng dậy, cử động, rồi mới
nằm xuống lại. Khôi phục cờ ngay lúc import — trước khi driver start — chính là
thứ cho phép BỎ QUA thay vì hoàn tác. Reboot cả máy thì vẫn tỉnh như cũ.


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
| `hal/test_ttp223_probe_orangepi.py` | Probe pad độc lập (ioctl thuần stdlib, không cần gpiod). `info` đọc trạng thái line khi HAL vẫn chạy; `watch` map pad→line và cần dừng `hal.service`. Line lấy từ board profile. |
| `hal/test_gpio.py` | Probe độc lập để verify line nút GPIO |

Cả hai handler được spawn trong startup lifespan `hal/server.py` — fail thì log nhưng không crash runtime (board không có phần cứng tự skip im lặng).
