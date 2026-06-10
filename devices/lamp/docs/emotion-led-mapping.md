# Emotion → LED + Animation Mapping

Source: `os/hal/presets.py` — `EMOTION_PRESETS`

| Emotion | Color (RGB) | Hex | Effect | Speed | Servo Animation |
|---|---|---|---|---|---|
| `curious` | 255, 191, 0 | `#FFBF00` vàng ấm | breathing | 1.0 | curious |
| `happy` | 255, 220, 0 | `#FFDC00` vàng | candle | 1.0 | happy_wiggle |
| `sad` | 80, 80, 200 | `#5050C8` xanh tím | breathing | 0.8 | sad |
| `thinking` | 180, 100, 255 | `#B464FF` tím | pulse | 1.5 | thinking_deep |
| `idle` | 183, 235, 234 | `#B7EBEA` xanh nhẹ | breathing | 0.8 | idle |
| `excited` | 230, 51, 230 | `#E633E6` hồng tím | blink | 2.5 | excited |
| `shy` | 255, 150, 180 | `#FF96B4` hồng | blink | 0.5 | shy |
| `shock` | 255, 255, 255 | `#FFFFFF` trắng | notification_flash | 2.0 | shock |
| `listening` | 51, 121, 230 | `#3379E6` xanh dương | pulse | 1.5 | listening |
| `laugh` | 230, 191, 51 | `#E6BF33` vàng sáng | blink | 1.2 | laugh |
| `confused` | 224, 71, 25 | `#E04719` cam đậm | candle | 0.6 | confused |
| `sleepy` | 60, 40, 120 | `#3C2878` tím đậm | breathing | 0.5 | sleepy |
| `greeting` | 255, 180, 100 | `#FFB464` vàng nhạt | blink | 0.8 | greeting | wake_up | goodbye |
| `acknowledge` | 51, 230, 141 | `#33E68D` xanh lá | blink | 1.0 | acknowledge |
| `stretching` | 245, 240, 230 | `#F5F0E6` xanh lá nhạt | breathing | 0.6 | stretching |
| `music_strong` | 155, 221, 155 | `#9BDD9B` xanh lá nhạt | rainbow | 1.5 | music_rock |
| `music_chill` | 252, 136, 3 | `#FC8803` cam | breathing | 0.5 | music_rock | music_groove | music_jazz | music_waltz |
| `scan` | 36, 184, 224 | `#24B8E0` xanh nhạt | pulse | 2.0 | scanning |
| `nod` | 51, 230, 141 | `#33E68D` xanh lá | blink | 1.0 | nod |
| `headshake` | 230, 51, 51 | `#E63333` đỏ | blink | 1.0 | headshake |

## LED Restore Behavior

- **User đã set color/effect/scene** → sau emotion, restore về màu/scene của user (kèm re-aim nếu là scene)
- **Đèn tắt hoặc chưa set** → emotion LED ở lại sau khi animation xong
- **`shock`** → restore sau 2.0s (notification_flash tự tắt sau ~1.5s)
- **`idle`** → không schedule restore (là ambient resting state)

## Pulse Behavior

Emotion-driven pulse (thinking / listening / scan) chạy trên **nền đen**: wavefront tím/xanh nổi rõ trên strip đen, agent biểu cảm dễ thấy bất kể user đang set màu gì.

Transient pulse (Buddy busy, các driver overlay khác qua `/led/effect` với `transient: true`) thì **overlay trên màu user**: pixel ngoài wavefront giữ màu user, pixel wavefront alpha-blend từ user → emotion. Mục đích: giữ liên tục màu nền user trong khi overlay nhanh.

Source: `os/hal/drivers/rgb/effects.py:pulse()`; emotion path ở `os/hal/app_state.py:_apply_emotion_led_display()` (base đen mặc định), transient path ở `os/hal/routes/led.py:start_led_effect()` (base = `_get_user_base_color()` khi `transient=true`).
