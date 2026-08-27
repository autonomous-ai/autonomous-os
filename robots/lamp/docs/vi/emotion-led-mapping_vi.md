# Emotion → LED + Animation Mapping

Nguồn: màu trong bảng là màu **lamp** thực sự hiển thị — `robots/lamp/presets.json` (overlay riêng cho device) merge đè lên `EMOTION_PRESETS` trong `hal/presets.py`. Overlay patch được mọi field chứ không riêng `color` — `presets_overlay.py` gọi `base[key].update(fields)`, và lamp dùng đúng cơ chế đó để override `speed` của `listening` và `servo` của `thinking`. Field nào không khai thì giữ giá trị base. Cột `Color source` cho biết màu của dòng đó đến từ `overlay` của lamp hay vẫn là giá trị `base` chưa đụng tới.

| Emotion | Color (RGB) | Hex | Color source | Effect | Speed | Servo Animation |
|---|---|---|---|---|---|---|
| `curious` | 3, 0, 2 | `#030002` tím-hồng dịu | overlay | candle | 0.3 | curious |
| `happy` | 3, 3, 0 | `#030300` vàng dịu | overlay | candle | 0.2 | happy_wiggle |
| `sad` | 3, 0, 0 | `#030000` đỏ dịu | overlay | breathing | 0.4 | sad |
| `thinking` | 3, 0, 2 | `#030002` tím-hồng dịu | overlay | pulse | 0.3 | thinking_deep |
| `idle` | 3, 2, 0 | `#030200` amber dịu | overlay | breathing | 0.2 | idle |
| `excited` | 3, 3, 0 | `#030300` vàng dịu | overlay | candle | 0.5 | excited |
| `shy` | 3, 0, 0 | `#030000` đỏ dịu | overlay | breathing | 0.3 | shy |
| `shock` | 2, 2, 2 | `#020202` trắng dịu | overlay | notification_flash | 1.0 | shock |
| `listening` | 0, 0, 3 | `#000003` xanh dương dịu | overlay | breathing | 1.2 | — (xem ghi chú) |
| `laugh` | 3, 3, 0 | `#030300` vàng dịu | overlay | candle | 0.2 | laugh |
| `confused` | 3, 0, 0 | `#030000` đỏ dịu | overlay | candle | 0.2 | confused |
| `sleepy` | 0, 0, 0 | `#000000` đen (tắt) | base | solid | — | sleepy |
| `greeting` | 3, 0, 3 | `#030003` tím dịu | overlay | breathing | 0.3 | greeting \| wake_up |
| `goodbye` | 3, 0, 3 | `#030003` tím dịu | overlay | breathing | 0.5 | goodbye |
| `caring` | 3, 0, 3 | `#030003` tím dịu | overlay | breathing | 0.4 | nod |
| `acknowledge` | 3, 0, 2 | `#030002` tím-hồng dịu | overlay | breathing | 0.5 | acknowledge |
| `stretching` | 3, 2, 0 | `#030200` amber dịu | overlay | breathing | 0.6 | stretching |
| `music_strong` | 8, 12, 8 | `#080c08` xanh lá nhạt (màu vô tác dụng — xem dưới) | base | rainbow | 1.0 | music_rock |
| `music_chill` | 0, 3, 3 | `#000303` cyan dịu | overlay | breathing | 0.3 | music_rock \| music_groove \| music_jazz \| music_waltz |
| `scan` | 3, 0, 2 | `#030002` tím-hồng dịu | overlay | pulse | 0.3 | scanning |
| `nod` | 3, 2, 0 | `#030200` amber dịu | overlay | breathing | 0.5 | nod |
| `headshake` | 3, 0, 0 | `#030000` đỏ dịu | overlay | breathing | 0.5 | headshake |

Màu của `music_strong` là vô tác dụng: nó chạy effect `rainbow`, mà `rainbow()` trong `hal/drivers/rgb/effects.py` bỏ qua tham số `color` và tự quét trọn vòng hue — nên overlay không buồn set màu cho nó.

## Six hue groups (sáu nhóm màu)

Bảng trên là palette gốc trong `hal/presets.py`. Trên lamp nó bị override, vì ở mức sáng chỉ báo thì palette đó không còn không gian màu dùng được.

Đo trên lamp-0c89 (19/08/2026): 22 emotion dồn vào ba cụm hue, và **12 cái nằm giữa hue 20° và 44°** — caring 20, headshake 20, shy 21, greeting 26, goodbye 26, confused 28, music_chill 34, idle 38, laugh 38, nod 38, curious 40, happy 44. Có cái không chỉ gần nhau mà trùng khít từng byte: `idle` = `laugh` = `nod` = `[12, 8, 1]`, và `greeting` = `goodbye` = `[12, 8, 5]`. Ở peak 12–16 thì mỗi channel chỉ còn 12–16 mức thay vì 255, nên chênh 1–4° hue là mắt thường không phân biệt nổi.

Đây là hệ quả của đợt hạ sáng. Trước 18/08 preset chạy peak cao — greeting/goodbye `[255, 180, 100]`, caring `[255, 160, 120]`, music_chill `[252, 136, 3]`, acknowledge `[51, 230, 70]`, listening `[51, 121, 230]` — ở biên độ đó 22 màu khác nhau đọc rất rõ. User phàn nàn chói ("như flash máy ảnh chiếu vào mặt": greeting chạy full 255 trên cả 64 pixel đúng lúc có người bước tới gần đèn), nên tất cả bị kéo xuống 12–16. Việc đó chữa được chói và làm sập luôn không gian màu.

**Tăng độ sáng trở lại KHÔNG phải là cách chữa.** Với gamma 2.2, hạ peak từ 255 xuống 90 chỉ mất ~40% độ sáng *cảm nhận* mà vẫn giữ 90 mức màu; hạ tiếp 90 → 12 mất thêm ~40% độ sáng cảm nhận nhưng vứt đi gấp 7.5× độ phân giải màu. Gần như toàn bộ lợi ích chống chói đã ăn xong ở bước đầu; bước thứ hai gần như chỉ toàn cái giá phải trả.

Trong bản test chống chói hiện tại, mọi channel RGB trong overlay lamp đều bị cap ở 3. Các mix dưới giữ hue mỗi nhóm gần nhất có thể với số nguyên; `processing` vẫn ở 320° để tránh die xanh lá.

| Group | Hue | RGB | Emotions |
|---|---|---|---|
| negative | 0° đỏ dịu | `[3, 0, 0]` | `sad`, `shy`, `confused`, `headshake` |
| joy | 60° vàng dịu | `[3, 3, 0]` | `happy`, `laugh`, `excited` |
| processing | 320° tím-hồng dịu | `[3, 0, 2]` | `curious`, `thinking`, `scan`, `acknowledge` |
| music | 180° cyan dịu | `[0, 3, 3]` | `music_chill` |
| listening | 240° xanh dương dịu | `[0, 0, 3]` | `listening` |
| social | 300° tím dịu | `[3, 0, 3]` | `greeting`, `goodbye`, `caring` |
| background | 40° amber dịu | `[3, 2, 0]` | `idle`, `nod`, `stretching` |
| alarm | trắng dịu | `[2, 2, 2]` | `shock` |
| sleep | tắt | `[0, 0, 0]` | `sleepy` (giữ nguyên) |

**`processing` nằm ở 320°, không phải 120° xanh lá như slot ban đầu của nó** (25/08/2026, user báo bằng mắt trên lamp-0c89). Xanh lá bị kêu chói ở `[0, 4, 0]`, vốn đã là đáy của tier xanh: `intensity` 0.7 biến nó thành `[0, 2, 0]` khi ra strip, và hạ thêm một bậc là `[0, 1, 0]` — chỗ mà breathing/pulse truncate mỗi frame và chu kỳ giật cấp thấy rõ. Nhóm này giờ được hạ thêm xuống `[3, 0, 2]`, vẫn giữ sắc tím mà không kích die xanh lá. Nguyên nhân nằm ở con die chứ không ở biên độ — die xanh lá của WS2812 sáng hơn die đỏ ở cùng giá trị, đúng cái phát hiện đã đẩy tier xanh xuống 4 trong khi mọi tier khác là 5 — nên *mọi* hue green-dominant, từ 60° tới 180°, đều dính, và hạ số bao nhiêu cũng không thoát. Đưa nhóm này ra khỏi vùng xanh là cách chữa duy nhất mà vẫn giữ được nguyên vòng ring sáng (lối "bật ít pixel đi" đã bị loại — xem `led-control.md`).

Mix `[3, 0, 2]` tối hơn đưa processing nghiêng về tím mà vẫn giữ ràng buộc không dùng green. Peak đỏ giảm 40% so với mix `[5, 0, 2]` trước đó.

Ba điểm là cố ý:

1. **Mọi màu đều có ít nhất một channel bằng 0** — bão hoà tối đa. Ở peak 12–16 đây là bắt buộc: màu pha loãng kiểu `[12, 8, 1]` mất hết cái làm nên chính nó, trong khi `[0, 12, 0]` vẫn đọc ra xanh lá không lẫn đi đâu được dù mờ tới mấy. Riêng `processing`, channel bằng 0 đó là *xanh lá*, vì một lý do thứ hai chồng lên chuyện bão hoà: giữ xanh lá ở 0 chính là thứ giữ cho con die xanh sáng chói kia tắt. Đỏ và xanh dương trong nhóm đó pha thoải mái — luật này nói về con die, không phải về việc phải đơn sắc.
2. **`idle` / `nod` / `stretching` dùng `[3, 2, 0]`.** `idle` là trạng thái lamp giữ lâu nhất, nên vẫn cần dịu hơn cue vàng đầy đủ.
3. **Trong cùng một nhóm, emotion phân biệt nhau bằng effect + speed chứ không bằng màu** — ví dụ nhóm joy: `happy` candle 0.2, `laugh` candle 0.2, `excited` candle 0.5. Mắt phân biệt nhịp tốt hơn nhiều so với phân biệt 4° hue.

Nói thẳng cái đánh đổi: 22 emotion giờ dùng chung 6 màu. Nhìn màu chỉ đọc ra **nhóm**, không đọc ra emotion cụ thể. Chấp nhận được, vì cái nó thay thế là trạng thái nhìn màu chẳng đọc ra gì cả.

Ngày 24/08/2026 peak bị hạ ba lượt trong cùng một ngày, mỗi lượt đều đo bằng mắt trên lamp-0c89. Lượt 1 chia đôi toàn bộ (16/12/8 → 8/6/4) vì mức cũ vẫn bị báo là chói. Lượt 2 chỉ hạ nhóm green-dominant 6 → 4: xanh ở peak 6 vẫn bị báo chói trong khi đỏ ở peak 8 nhìn ổn — die xanh của WS2812 sáng hơn die đỏ ở cùng giá trị, nhiều hơn mức luật 12-vs-16 bù được. Lượt 3 hạ nốt nhóm low-green 8 → 5. Mọi lượt đều scale tỉ lệ nên sáu hue không đổi, chỉ biên độ giảm. Cần nhớ thêm: emotion còn bị nhân `intensity` (mặc định 0.7) trước khi ra strip, nên `listening` khai `[0, 0, 5]` thực tế chỉ còn `[0, 0, 3]`, `idle` `[4, 2, 0]` còn `[2, 1, 0]` — bằng hoặc dưới sàn peak 8 ghi trong `hal/presets.py`. Ở mức đó `breathing`/`pulse` truncate mỗi frame (`int(c * brightness)`) nên cả chu kỳ chỉ còn rất ít mức sáng; thứ cần soi tiếp là giật cấp, không phải chói.

Ghi chú kỹ thuật:

- **`shock` được override dù không thuộc 6 nhóm hue.** Nó dùng `[2, 2, 2]`, giữ cue báo động trắng dưới cap test.
- **`music_strong` cố tình không có** trong bảng override. Nó dùng effect `rainbow`, mà `rainbow()` trong `hal/drivers/rgb/effects.py` bỏ qua hoàn toàn tham số `color` — nó tự quét trọn vòng hue. Gán màu cho nó là vô nghĩa.
- **Bảng này nằm trong `robots/lamp/presets.json`**, overlay riêng cho device, merge từng field lúc boot qua `hal/board/presets_overlay.py` — `hal/presets.py` *không* bị sửa. Nên các robot khác (reachy, intern) vẫn giữ palette gốc, và muốn trả lamp về palette gốc thì chỉ cần xoá section `emotion` trong file JSON đó.
- **Đừng nhầm `EMO_IDLE` với `AMBIENT_RESTING_LED`.** Cái sau là `[0, 0, 0]` (quyết định sản phẩm 30/07/2026: strip lúc nghỉ thì tắt hẳn); `EMO_IDLE` là một emotion agent chủ động phát ra và vẫn có màu.

## `listening` không có servo

Đây là preset duy nhất để `"servo": None` — chỉ LED, đèn đứng yên. `listening` chạy đúng lúc user đang nói, tiếng servo cộng rung thân máy lọt thẳng vào mic và làm bẩn STT.

`thinking` thì **có** servo (`thinking_deep`), nhưng vẫn là ca đặc biệt ở phía LED: hook emotion-ack bắn nó ở **mỗi** message preprocessed, nên LED của nó nằm sau `_BACKGROUND_EMOTIONS` guard trong `hal/app_state.py` để cả cuộc hội thoại không bị sơn xanh lá liên tục.

`listening.csv` vẫn giữ trong `hal/recordings/` dù không emotion nào map tới: `/servo/play` vẫn gọi tay được, và Reachy vẫn map nó (`hal/drivers/motors/reachy_service.py`).

Đường code chịu `servo: None` bình thường — `hal/routes/emotion.py` bỏ qua nhánh play và `POST /emotion` trả `"servo": null`, còn `listening` không schedule LED restore nào cả.

### `servo: None` một mình KHÔNG làm đèn đứng yên

Không phát recording mới không có nghĩa là đèn im. Idle loop chạy từ lúc boot và lặp vô hạn (`_continue_playback` trong `hal/drivers/motors/animation_service.py`), mà `idle.csv` không hề nhẹ — mỗi vòng 10s nó quét wrist_roll ~32°, wrist_pitch ~26°, base_pitch ~17°. Tệ hơn: emotion vừa chạy xong sẽ **interpolate ngược về idle** trong vài giây, nên cú vung to nhất rơi đúng lúc user đang nói.

Nên với emotion `servo: None` (hôm nay chỉ có `listening`), route gọi `svc.halt()`: drop recording đang chạy, ghim pose hiện tại, torque vẫn ON. Không cần un-halt tường minh — emotion/`/servo/play` kế tiếp gọi `_begin_motion()` và tự xoá cờ.

Hai chốt chặn kèm theo:

- **Music được miễn**: đang phát nhạc thì groove quan trọng hơn, cue listening không được dừng nhảy.
- **Auto-resume idle sau 10s** (`STILL_IDLE_RESUME_SECONDS` trong `hal/routes/emotion.py`): nếu turn không sinh ra emotion nào (LLM lỗi, im lặng sau partial đầu), body tự trở lại idle thay vì đứng chết ở tư thế dở. Bất kỳ `POST /emotion` nào cũng huỷ timer này. Safety net 8s của `voice_service` chỉ dọn LED, không đụng servo — nên timer này là thứ duy nhất lo phần thân.
- **Clear thinking khi câu trả lời nói xong** (`_clear_thinking_after_reply` trong `hal/app_state.py`, gọi từ `_on_tts_speak_end`): phần chờ kết thúc khi máy ngừng nói, nên đó là lúc tắt mặt `thinking` — không phải chờ agent nhả emotion marker. Có gate `tts_service.realtime_feedback` (cờ chỉ reply của runtime mới set), nên filler và system notice phát *trong lúc* chờ không kết thúc cue.
- **Auto-reset thinking sau 25s** (`HAL_EMOTION_THINKING_RESET_S`, `0` = tắt) — lưới cho các turn không hề nói. `thinking` là mặt duy nhất không có ai tự tắt — nó được bật khi bắt đầu chờ và bị thay bởi emotion mà câu trả lời express, nên turn chết trước khi express (realtime exception, delegate mà agent trả lời không kèm marker) để nó cháy mãi, không có input người dùng thì không thoát ra được. Sau ngần ấy giây thinking LIÊN TỤC, route bỏ `_thinking_cue_active`, express `idle` và restore LED user state. Bất kỳ emotion nào khác huỷ timer; `thinking` mới arm lại. Con số này là đo, không phải đoán: trên máy, realtime clear cue trong 0.4-8.6s và turn delegate chạy 6-22s.

Đo trên lamp-0c89: sau `listening`, 5 góc servo đứng nguyên ở T+2s / T+5s / T+8s, tới ~T+13s idle chạy lại; `happy` gửi giữa lúc halt thì phát bình thường.

## Ngân sách độ sáng (peak budget)

Emotion LED là **chỉ báo**, không phải chiếu sáng — dùng chung ngân sách với `STATUS_LED_PRESETS`:

- hue thiên xanh lá (xanh lá / vàng / cyan / trắng) → peak channel **4**
- ít hoặc không có xanh lá (đỏ / tím / cam / xanh dương) → peak channel **5**

Mỗi màu được hạ bằng cách **scale tỉ lệ RGB gốc** xuống tier tương ứng, nên hue của từng emotion giữ y như trước. Hạ sáng phải làm bằng scale, không phải chọn màu mới — hue là thứ agent muốn nói, độ sáng chỉ là nói to hay nhỏ.

Gate `light.max_brightness` (lamp: 120) chỉ scale peak **LÊN** tới trần chứ không làm dim, nên hạ sáng phải làm ngay trong preset. Chỉnh xong phải nhìn bằng mắt trên device thật.

`listening` dùng breathing chứ không phải pulse: nó sáng suốt lúc user nói, mà khoảng tối giữa các nhịp của pulse đọc thành cảnh báo khi kéo dài.

Nếu sau này dùng `blink`: `blink()` map speed 1.0 → **~3 Hz** (`hal/drivers/rgb/effects.py`), đủ nhanh để nhức mắt. Giữ ≤ 0.5 (~1.5 Hz trở xuống).

## `candle` đổi độ sáng, không bao giờ đổi hue

`candle()` (`hal/drivers/rgb/effects.py`) cho mỗi pixel một mức flicker riêng trong `[CANDLE_FLICKER_MIN, 1.0]` (`CANDLE_FLICKER_MIN = 0.8`) và scale **cả ba channel bằng đúng một mức đó**: `tuple(min(255, int(c * level)) for c in color)`. Mọi pixel cùng một màu, chỉ khác độ sáng — cả strip đọc ra như một ngọn lửa thở không đều, thay vì một mớ màu khác nhau.

Đây đúng là luật của chính các preset: hạ sáng bằng scale, không bao giờ bằng cách chọn màu mới. Hue là thứ agent muốn nói.

Trước đây nó phá luật này. Bản cũ xử lý từng channel riêng — `r = color[0]*flicker + random.randint(0, 20)`, `g = color[1]*flicker*random.uniform(0.6, 0.9)`, `b = color[2]*flicker*0.3` — còn sống được khi màu emotion còn sáng, nhưng không sống nổi sau khi hạ xuống mức chỉ báo với peak chỉ 12–16. Cộng `+20` vào đỏ khi đó là **lớn hơn cả chính màu đó**. Đo trên lamp (19/08/2026): `happy`, khai báo `[12, 9, 1]` ở hue 44° vàng, vẽ ra pixel trải từ `(9, 5, 0)` tới `(26, 4, 0)` — đỏ lên tới 2.2× giá trị khai báo của chính nó, hue sập về 5–20°, nên mắt thấy một mảng cam lốm đốm thay vì vàng đều. `excited` `[12, 8, 12]` (hồng tím, hue 300°) tệ nhất: xanh dương bị nhân ×0.3 trong khi đỏ bị thổi lên, và ra thành cam. Sau khi sửa, hue đo được giữ đúng: happy 36–48°, curious 36–43°, excited đúng 300°.

Emotion bị ảnh hưởng: `curious`, `happy`, `excited`, `laugh`, `confused` — năm cái dùng candle. `breathing` và `pulse` chưa bao giờ dính bug này vì chúng luôn chỉ scale theo tỉ lệ (`int(c * brightness)`).

### …và nó không còn nhấp nháy gắt nữa

Cùng đợt sửa đó còn xử lý một vấn đề thứ hai, độc lập, trong `candle`: trước đây nó bốc một mức ngẫu nhiên mới cho mỗi pixel **mỗi frame**, ở nhịp `0.05/speed` — 4 Hz với `happy`/`laugh`/`confused`, 10 Hz với `excited` — nhảy tuỳ ý trong khoảng 0.4 đến 1.0 của màu gốc. Đó là độ sâu điều biến 60% ở 4–10 Hz, trong khi IEEE 1789-2015 xếp **mọi** flicker dưới 90 Hz vào vùng rủi ro cao (ở 100 Hz giới hạn đã là 1.6%). Dải 3–70 Hz chính là dải bị gắn với đau đầu và khó chịu thị giác, và trên một chiếc đèn bàn chiếu thẳng vào mặt thì nó bị phản ánh là gây chóng mặt.

Hai thay đổi kéo nó về trong khuyến nghị mà không đổi bản chất effect:

- **Refresh cố định 30 Hz kèm nội suy** (`CANDLE_REFRESH_HZ = 30`). Mỗi pixel giờ đi dần về mức đích thay vì nhảy phắt tới — `speed` quyết định nó đi nhanh cỡ nào, chứ không phải nó dịch chuyển tức thời bao nhiêu lần mỗi giây. Mắt bắt vào các bước nhảy; một đường dốc mượt cùng chu kỳ đọc ra là chuyển động, không phải nhấp nháy.
- **Nâng sàn 0.4 → 0.8**, kéo độ sâu điều biến từ 60% xuống 20%.

Đo trên lamp sau đó: `happy` refresh 29.2 Hz ở mức điều biến 9.1%, `excited` 29.1 Hz ở 18.2%, và nhịp flicker cảm nhận được rơi vào khoảng 0.85 Hz (speed 0.2) đến 2.25 Hz (`excited`, speed 0.5) — dưới mốc 3 Hz nơi dải khó chịu bắt đầu. Hệ số `speed * 0.6` trong phần tiệm cận chính là thứ giữ `excited` ở đó; nâng nó lên là đẩy preset sôi nổi nhất trở lại vào dải đó.

`CANDLE_FLICKER_MIN` và `CANDLE_REFRESH_HZ` là ngưỡng an toàn thị giác, không phải tuỳ chọn thẩm mỹ. Đừng hạ cái nào xuống nếu chưa đọc lại chuẩn.

## LED Restore Behavior

- **User đã set color/effect/scene** → sau emotion, restore về màu/scene của user (kèm re-aim nếu là scene)
- **Đèn tắt hoặc chưa set** → emotion LED ở lại sau khi animation xong
- **`shock`** → restore sau 2.0s (notification_flash tự tắt sau ~1.5s)
- **`idle`** → không schedule restore (là ambient resting state)

## Pulse Behavior

Emotion-driven pulse (thinking / listening / scan) chạy trên **nền đen**: wavefront tím/xanh nổi rõ trên strip đen, agent biểu cảm dễ thấy bất kể user đang set màu gì.

Transient pulse (Buddy busy, các driver overlay khác qua `/led/effect` với `transient: true`) thì **overlay trên màu user**: pixel ngoài wavefront giữ màu user, pixel wavefront alpha-blend từ user → emotion. Mục đích: giữ liên tục màu nền user trong khi overlay nhanh.

Source: `hal/drivers/rgb/effects.py:pulse()`; emotion path ở `hal/app_state.py:_apply_emotion_led_display()` (base đen mặc định), transient path ở `hal/routes/led.py:start_led_effect()` (base = `_get_user_base_color()` khi `transient=true`).
