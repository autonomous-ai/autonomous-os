# Điều khiển vật lý — Nút GPIO + Touchpad TTP223

Lamp có hai thiết bị input vật lý mà user có thể chạm trực tiếp. Chúng dùng chung thư viện action (`lelamp/service/button_actions.py`) nên cùng một cử chỉ "single click" sẽ hành xử giống nhau dù đến từ nút bấm cơ học hay touchpad cảm ứng.

## Tại sao có hai thiết bị

| Thiết bị | Vai trò | Có ở |
|---|---|---|
| **Nút GPIO** | Một nút bấm cơ. Dùng cho các hành động dứt khoát kể cả destructive (reboot/shutdown). Cảm giác cơ + detect giữ lâu khiến destructive action khó xảy ra do vô tình. | Pi 4/5 và OrangePi sun60 |
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
| **1 chạm** | Stop loa / unmute mic + báo "Mình nghe đây" | Y hệt — fire ~1.2 s sau khi nhả (chi phí decision-window, xem dưới) |
| **2 chạm** (≤ 0.4 s, nút) / (≤ 1.2 s, TTP223) | Bỏ qua (panic-click guard) | Pet response — TTS chọn ngẫu nhiên 1 câu từ pool theo ngôn ngữ |
| **3 chạm** (≤ 0.4 s, nút) | Reboot OS (TTS báo → `sudo reboot`) | n/a — TTP223 dừng ở 2 (chạm thêm bị cooldown nuốt) |
| **Giữ 5 s** | Shutdown OS (TTS báo → release servo → `sudo shutdown -h now`) | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |

Destructive gesture (reboot, shutdown) cố tình chỉ có trên nút GPIO. Hành động phá huỷ cần cử chỉ chủ ý, và nút cơ học cho bằng chứng intent rõ ràng.

## Cắt Lamp giữa câu (barge-in)

Cử chỉ 1 chạm là **cơ chế barge-in chính** của Lamp: chạm đỉnh Lamp (touchpad) hoặc nhấn nút GPIO một lần khi Lamp đang nói → cắt câu TTS đang phát giữa chừng, dừng nhạc, unmute mic để Lamp lắng nghe câu kế. Sau khi cắt, một câu cue "Mình nghe đây" (theo ngôn ngữ) được phát.

Chuỗi end-to-end:
1. `gpio_button.py` / `ttp223.py` detect single click → gọi `single_click_action(source)` trong `button_actions.py`
2. `single_click_action` → `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + thread deferred `_announce_listening()`
3. `stop_tts()` → `tts_service.stop()` set `_stop_event`; mọi blocking loop trong TTS stream (synth, render, playback) check event và abort sạch, không để loa kẹt

### Voice barge-in (tuỳ chọn, mặc định tắt)

Cắt bằng giọng nói — nói trong lúc Lamp đang nói để Lamp dừng và lắng nghe — được gate bởi `LELAMP_BARGE_IN_ENABLED=true` trong `lelamp/.env`. Khi bật, `voice_service._monitor_barge_in()` mở mic capture song song trong lúc TTS phát, tính RMS trên block 256ms, gọi `tts_service.stop()` khi N block liên tiếp vượt `LELAMP_BARGE_IN_RMS_THRESHOLD`. Cùng chuỗi downstream với tap-to-interrupt.

Tại sao tắt mặc định: software-only AEC không khả thi trên hardware này (Speex AEC tích hợp xuống còn ~13-30% reduction dưới TTS multi-chunk streaming). Chỉ với physical separation mic-loa, bleed RMS (1-7500 đo được) và user voice RMS (6-14k đo được) chồng nhau ở zone 7-9k → 1 threshold RMS không discriminate sạch được. Threshold 9000 + 1 frame trigger thiên về 0 false-trigger, đổi lại phải nói lớn để cắt; threshold 6000-7000 thiên ngược lại. Tune theo deployment là không tránh khỏi cho tới khi device có hardware AEC (ví dụ ReSpeaker XVF3800).

Khi bật, tail log để xem `Barge-in monitor session end: max_rms_seen=N` (peak mỗi session) và sự kiện `BARGE-IN: RMS=N`, sau đó set `LELAMP_BARGE_IN_RMS_THRESHOLD` ở giữa bleed-max và voice-min quan sát được. Tap-to-interrupt vẫn active bất kể.

## Detect nút GPIO (`lelamp/service/gpio_button.py`)

Driver đếm edge nút bấm chuẩn:

1. Mỗi falling edge (nhấn) khởi 1 timer long-press 5 s.
2. Mỗi rising edge (nhả) cancel timer. Nếu giữ < 5 s → `click_count += 1` và (re)start click-window timer 0.4 s.
3. Khi click window hết:
   - `count == 1` → `single_click_action`
   - `count == 3` → `triple_click_action`
   - `count == 2` hoặc `>= 4` → bỏ qua (panic-click guard)
4. Nếu timer 5 s fire:
   - Đọc lại pin level (phòng vệ — chống miss release edge mà gây shutdown nhầm khi double-tap chậm)
   - Pin vẫn LOW (vẫn giữ) → fire `long_press_action`
   - Khác → log và bỏ qua

Debounce mỗi edge là 200 ms.

## Detect TTP223 (`lelamp/service/ttp223.py`)

IC TTP223 trên board này chạy ở **FastMode**: output HIGH khi chạm, rồi tự về LOW trong ~50-80 ms dù ngón tay vẫn ở pad. IC chỉ re-trigger khi điện dung thay đổi (ngón tay di chuyển). "Giữ liên tục" là bất khả thi nếu không đổi chân FM của IC sang LowPowerMode (~12 s max touch).

Cross-talk giữa các pad lân cận cũng đáng kể — một lần chạm vật lý fire edge trên 2-4 pad với timing lệch nhau.

Driver bù bằng **mô hình hai tầng**:

### Tầng 1: Session (gap 200 ms)

Bất kỳ edge nào — rising hay falling, pad nào — đều restart timer 200 ms. Khi timer expire (200 ms không edge mới), "session" kết thúc. Một session = một sự kiện chạm logic theo POV user, bất kể bao nhiêu edge vật lý fire bên trong (cross-talk + FastMode auto-LOW).

### Tầng 2: Decision window (1.2 s sau session end)

Sau khi session kết thúc:

1. Nếu **pet cooldown** đang active (head-pat vừa fire gần đây), session bị nuốt im lặng và cooldown được extend. Ngăn `single_click` chen ngang giữa các stroke liên tục.
2. Khác thì increment session count rồi:
   - `count >= 2` → fire `head_pat_action` ngay lập tức, arm pet cooldown 1.5 s
   - `count < 2` → schedule decision timer 1.2 s. Khi timer fire với `count == 1`, fire `single_click_action`.

### Hằng số (`ttp223.py`)

| Hằng số | Giá trị | Lý do |
|---|---|---|
| `SESSION_GAP_S` | 0.2 | Vượt thừa burst cross-talk quan sát được (~30-100 ms) mà không gộp các tap thật sự tách biệt |
| `DECISION_WINDOW_S` | 1.2 | Đo thực tế: pace vuốt của user 0.8-1.2 s mỗi nhịp — đủ rộng để stroke đầu của pet không fire single_click thừa |
| `PET_SESSION_THRESHOLD` | 2 | 2 session liên tiếp trong decision window = pet. Dễ hơn 3 vì mỗi "stroke" trên phần cứng này chỉ tạo 1 session |
| `PET_COOLDOWN_S` | 1.5 | Sau pet fire, session thêm trong 1.5 s extend cooldown chứ không bắt đầu count mới. Vuốt liên tục = 1 pet, rồi im |

## Thư viện action chung (`lelamp/service/button_actions.py`)

Cả ba action sống ở một chỗ để nút GPIO, TTP223, và mọi input tương lai (touchpad, remote) hành xử giống nhau:

| Hàm | Làm gì | Cắt TTS đang phát? |
|---|---|---|
| `single_click_action(source)` | Mic bị mute → unmute. Khác thì stop TTS + stop music. Rồi nói câu "Mình nghe đây" local với retry-on-busy. | Có — gọi `stop_tts()` và bản thân câu cue cũng preempt. |
| `triple_click_action(source)` | Nói "Đang khởi động lại" → đợi 5 s cho clip cached → `sudo reboot`. | Có |
| `long_press_action(source)` | Nói "Đang tắt máy" → đợi 5 s → `release_servos()` (để đèn không slam xuống giữa pose) → `sudo shutdown -h now`. | Có |
| `head_pat_action(source)` | Chọn ngẫu nhiên 1 câu pet local, nói qua `speak_cached` trên daemon thread. **Không cắt**: nếu TTS đang nói, câu pet bị drop im lặng — vuốt giữa câu không được làm Lamp mất lời. | Không |

## Phrase local

Cả 4 action đều local theo `stt_language` từ `config.json` của Lamp. Hằng số ngôn ngữ ở `lelamp/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Fallback về `DEFAULT_LANG` (English) khi ngôn ngữ hiện tại chưa có bản dịch.

### Thông báo an toàn (1 câu/ngôn ngữ)

`reboot`, `shutdown`, và câu cue `listening` dùng phrase nghĩa-đen ("Đang khởi động lại", "Đang tắt máy") ở mọi ngôn ngữ vì user vừa làm cử chỉ destructive và cần xác nhận rõ ràng — đây là thông báo an toàn, không phải khoảnh khắc persona.

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
| `lelamp/service/gpio_button.py` | Handler nút GPIO (cơ học, cả hai board) |
| `lelamp/service/ttp223.py` | Handler touchpad cảm ứng TTP223 (chỉ OrangePi sun60) |
| `lelamp/service/button_actions.py` | Hàm action chung + pool phrase local |
| `lelamp/presets.py` | Hằng số mã ngôn ngữ (`LANG_EN`, v.v.) |
| `lelamp/test_ttp223_probe_orangepi.py` | Probe độc lập để verify mapping line TTP223 |
| `lelamp/test_gpio.py` | Probe độc lập để verify line nút GPIO |

Cả hai handler được spawn trong startup lifespan `lelamp/server.py` — fail thì log nhưng không crash runtime (board không có phần cứng tự skip im lặng).
