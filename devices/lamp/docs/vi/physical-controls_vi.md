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
| **1 chạm** | Stop loa / unmute mic + speaker + chime ack (~120 ms ping) — tất cả fire ngay khi nhả nút (không đợi click window); cue "Nghe đây" phát sau khi click window 0.4 s phân giải xong | Tách y hệt — TTS đang nói bị cắt và chime ack kêu ~0.2 s sau khi nhấc tay (session đầu kết thúc); unmute + cue đợi decision window 1.2 s (chi phí tap-vs-pet, xem dưới) |
| **2 chạm** (≤ 0.4 s, nút) / (≤ 1.2 s, TTP223) | Không thêm gì ngoài single-click đã fire ở chạm 1 (panic-click guard) | Pet response — TTS chọn ngẫu nhiên 1 câu từ pool theo ngôn ngữ |
| **3 chạm** (≤ 0.4 s, nút) | Reboot OS (TTS báo → `sudo reboot`) | n/a — TTP223 dừng ở 2 (chạm thêm bị cooldown nuốt) |
| **Giữ 3–10 s rồi nhả** | Phát thông báo sleep theo ngôn ngữ, rồi vào `sleepy`: LED tắt, camera/mic/speaker tắt; servo release sau 2 s. Khi đang giữ LED nháy tím sleepy. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 10–20 s rồi nhả** | Shutdown OS (TTS báo → release servo → `sudo shutdown -h now`). LED nháy đỏ khi đã arm. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 20 s+ rồi nhả** | Factory-reset: wipe state thiết bị + reboot vào AP setup (TTS báo → release servo → POST `/api/system/factory-reset` trên OS server). LED đỏ đứng khi đã arm. | n/a |

Gesture giữ chỉ có trên nút GPIO vì nút cơ học cho bằng chứng intent rõ ràng. Mức sleep và các mức destructive **commit khi nhả, không phải khi timer fire lúc đang giữ**. Các mức destructive escalate từ shutdown sang factory-reset sau 20 s (xem "Detect nút GPIO" dưới).

## Cắt Lamp giữa câu (barge-in)

Cử chỉ 1 chạm là **cơ chế barge-in chính** của Lamp: chạm đỉnh Lamp (touchpad) hoặc nhấn nút GPIO một lần khi Lamp đang nói → cắt câu TTS đang phát giữa chừng, dừng nhạc, unmute mic để Lamp lắng nghe câu kế. Nếu loa đang bị mute bởi user/scene thì cũng được gỡ (trừ khi đang ghi âm enroll giọng) để cue và câu trả lời nghe lại được. Sau khi cắt, một câu cue "Nghe đây" (theo ngôn ngữ) được phát.

Chuỗi end-to-end:
1. `gpio_button.py` / `ttp223.py` detect single click → gọi `single_click_action(source)` trong `button_actions.py`
2. `single_click_action` → `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + thread deferred `_announce_listening()`
3. `stop_tts()` → `tts_service.stop()` set `_stop_event`; mọi blocking loop trong TTS stream (synth, render, playback) check event và abort sạch, không để loa kẹt

### Voice barge-in (tuỳ chọn, mặc định tắt)

Cắt bằng giọng nói — nói trong lúc Lamp đang nói để Lamp dừng và lắng nghe — được gate bởi `HAL_BARGE_IN_ENABLED=true` trong `hal/.env`. Khi bật, `voice_service._monitor_barge_in()` mở mic capture song song trong lúc TTS phát, tính RMS trên block 256ms, gọi `tts_service.stop()` khi N block liên tiếp vượt `HAL_BARGE_IN_RMS_THRESHOLD`. Cùng chuỗi downstream với tap-to-interrupt.

Tại sao tắt mặc định: software-only AEC không khả thi trên hardware này (Speex AEC tích hợp xuống còn ~13-30% reduction dưới TTS multi-chunk streaming). Chỉ với physical separation mic-loa, bleed RMS (1-7500 đo được) và user voice RMS (6-14k đo được) chồng nhau ở zone 7-9k → 1 threshold RMS không discriminate sạch được. Threshold 9000 + 1 frame trigger thiên về 0 false-trigger, đổi lại phải nói lớn để cắt; threshold 6000-7000 thiên ngược lại. Tune theo deployment là không tránh khỏi cho tới khi device có hardware AEC (ví dụ ReSpeaker XVF3800).

Khi bật, tail log để xem `Barge-in monitor session end: max_rms_seen=N` (peak mỗi session) và sự kiện `BARGE-IN: RMS=N`, sau đó set `HAL_BARGE_IN_RMS_THRESHOLD` ở giữa bleed-max và voice-min quan sát được. Tap-to-interrupt vẫn active bất kể.

## Detect nút GPIO (`hal/drivers/gpio_button.py`)

Driver đếm edge nơi **mọi destructive action commit ở rising edge (nhả) dựa trên thời lượng giữ** — không timer nào fire lúc đang giữ. Đây chính là cái cho phép user huỷ giữa chừng (nhả trước ngưỡng) hoặc escalate (giữ tiếp quá 20 s).

1. **Falling edge (nhấn):** ghi `press_start` (đồng hồ monotonic) và spawn thread hold-LED watcher (mỗi lần nhấn 1 thread, có stop `Event` riêng). Không arm timer action nào.
2. **Rising edge (nhả):** dừng LED watcher, tính `held = now − press_start` rồi rẽ nhánh:
   - `held >= 20 s` (`FACTORY_RESET_DURATION`) → scrub mọi click đang chờ, khoá LED đỏ đứng, chạy `factory_reset_action` off-thread.
   - `held >= 10 s` (`LONG_PRESS_DURATION`) → scrub click đang chờ, freeze LED đỏ, chạy `long_press_action` (shutdown) off-thread.
   - `held >= 3 s` (`SLEEP_HOLD_DURATION`) → scrub click đang chờ, chạy `sleep_action` off-thread; nó gọi pipeline emotion `sleepy` chuẩn.
   - khác (tap ngắn) → `click_count += 1` và (re)start click-window timer 0.4 s. Ở tap **đầu tiên** của chuỗi, phần im lặng của `single_click_action` (`announce=False`) fire ngay off-thread — nó không phá huỷ ("cho tôi nói"), nên không cần đợi window. Cue nói được hoãn lại để không nói đè lên chuỗi triple-click đang bấm dở.
3. Khi click window hết:
   - `count == 3` → `triple_click_action` (không cue — chỉ announce reboot)
   - count khác → `announce_listening_cue` phát cue "Nghe đây" đã hoãn, đúng 1 lần mỗi chuỗi; `count == 2` / `>= 4` log thêm ignored (panic-click guard — floor-grab đã chạy ở tap 1, không gì phá huỷ fire)

Release edge không có press khớp (press bị debounce nuốt) thì bỏ qua — `press_start` có thể là cũ, hành động theo nó có thể fire destructive action trên timestamp cũ vài phút. Destructive action chạy trên daemon thread riêng vì callback `lgpio` phải return ngay, nếu không các edge sau sẽ dồn hàng.

### LED feedback khi giữ

Thread watcher poll thời lượng giữ và đẩy LED RGB ở priority HIGH (preempt emotion hiện tại) để user thấy đã arm tới đâu trước khi nhả:

| Thời gian giữ | LED | Ý nghĩa |
|---|---|---|
| < 3 s | giữ nguyên | một tap ngắn |
| 3–10 s | tím sleepy, nháy 2 Hz | đã arm sleepy; nhả ra sẽ vào sleep (LED sau đó tắt) |
| 10–20 s | đỏ, nháy 2 Hz | đã arm shutdown — nhả bây giờ là tắt máy |
| 20 s+ | đỏ, đứng | đã arm factory-reset — nhả bây giờ là wipe + reboot |

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
| `single_click_action(source)` | Gỡ mute loa do user/scene (bỏ qua khi `_enrolling`). Mic bị mute → unmute. Khác thì stop TTS + stop music. Rồi nói câu "Nghe đây" local với retry-on-busy. | Có — gọi `stop_tts()` và bản thân câu cue cũng preempt. |
| `triple_click_action(source)` | Nói "Đang khởi động lại" → đợi 5 s cho clip cached → `sudo reboot`. | Có |
| `sleep_action(source)` | Phát thông báo sleep theo ngôn ngữ, rồi gọi `sleepy`: LED tắt, camera/mic/speaker tắt, rồi release servo sau 2 s. | Có — pipeline sleepy dừng TTS/nhạc đang phát sau thông báo. |
| `long_press_action(source)` | Nói "Đang tắt máy" → đợi 5 s → `release_servos()` (để đèn không slam xuống giữa pose) → `sudo shutdown -h now`. | Có |
| `factory_reset_action(source)` | Nói "Đang khôi phục cài đặt gốc. Đang khởi động lại" → `release_servos()` → POST `/api/system/factory-reset` trên OS server (server lo phần wipe + reboot, xem dưới). | Có |
| `head_pat_action(source)` | Chọn ngẫu nhiên 1 câu pet local, nói qua `speak_cached` trên daemon thread. **Không cắt**: nếu TTS vẫn busy thì câu pet bị drop im lặng. Thực tế trên TTP223, session chạm đầu tiên đã cắt lời đang nói (`_grab_floor_if_speaking`) nên tới lúc pet fire thì TTS thường rảnh và câu giggle phát được. | Không |

### Factory-reset: wipe những gì

`factory_reset_action` chỉ **báo + uỷ quyền** — phần reset thật nằm ở OS server (`system/server/system/factoryreset.go`), gọi được từ thiết bị qua loopback không cần Bearer token (authoritative nhờ hiện diện vật lý: giữ 20 s có chủ ý). `POST /api/system/factory-reset` là reset **mềm** (wipe state, không reflash — kernel / package OS / binary / `.venv` HAL không bị đụng):

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
