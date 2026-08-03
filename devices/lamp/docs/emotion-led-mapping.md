# Emotion → LED + Animation Mapping

Source: `hal/presets.py` — `EMOTION_PRESETS`

| Emotion | Color (RGB) | Hex | Effect | Speed | Servo Animation |
|---|---|---|---|---|---|
| `curious` | 125, 81, 0 | `#7d5100` vàng ấm | candle | 0.3 | curious |
| `happy` | 105, 80, 10 | `#69500a` vàng | candle | 0.2 | happy_wiggle |
| `sad` | 20, 10, 10 | `#140a0a` đỏ thẫm | breathing | 0.4 | sad |
| `thinking` | 50, 100, 35 | `#326423` xanh chìm | pulse | 0.1 | thinking_deep |
| `idle` | 90, 60, 5 | `#5a3c05` vàng dim | breathing | 0.2 | idle |
| `excited` | 30, 21, 30 | `#1e151e` hồng tím | candle | 0.5 | excited |
| `shy` | 155, 70, 20 | `#9b4614` hồng | breathing | 0.3 | shy |
| `shock` | 255, 255, 255 | `#FFFFFF` trắng | notification_flash | 1.0 | shock |
| `listening` | 51, 121, 230 | `#3379E6` xanh dương | pulse | 0.1 | listening |
| `laugh` | 130, 91, 11 | `#825a0b` vàng sẫm | candle | 0.2 | laugh |
| `confused` | 124, 71, 25 | `#7c4719` cam đậm | candle | 0.2 | confused |
| `sleepy` | 0, 0, 0 | `#000000` đen (tắt) | solid | — | sleepy |
| `greeting` | 255, 180, 100 | `#FFB464` vàng nhạt | breathing | 0.3 | greeting | wake_up | goodbye |
| `acknowledge` | 51, 230, 70 | `#33e645` xanh lá | breathing | 0.5 | acknowledge |
| `stretching` | 145, 140, 30 | `#918b1e` xanh lá nhạt | breathing | 0.6 | stretching |
| `music_strong` | 155, 221, 155 | `#9BDD9B` xanh lá nhạt | rainbow | 1.0 | music_rock |
| `music_chill` | 252, 136, 3 | `#FC8803` cam | breathing | 0.3 | music_rock | music_groove | music_jazz | music_waltz |
| `scan` | 36, 84, 24 | `#245419` xanh nhạt | pulse | 0.3 | scanning |
| `nod` | 107, 73, 13 | `#6b490d` cam đất | breathing | 0.5 | nod |
| `headshake` | 206, 77, 14 | `#ce4e0e` amber | breathing | 0.5 | headshake |

## LED Restore Behavior

- **User đã set color/effect/scene** → sau emotion, restore về màu/scene của user (kèm re-aim nếu là scene)
- **Đèn tắt hoặc chưa set** → emotion LED ở lại sau khi animation xong
- **`shock`** → restore sau 2.0s (notification_flash tự tắt sau ~1.5s)
- **`idle`** → không schedule restore (là ambient resting state)

## Pulse Behavior

Emotion-driven pulse (thinking / listening / scan) chạy trên **nền đen**: wavefront tím/xanh nổi rõ trên strip đen, agent biểu cảm dễ thấy bất kể user đang set màu gì.

Transient pulse (Buddy busy, các driver overlay khác qua `/led/effect` với `transient: true`) thì **overlay trên màu user**: pixel ngoài wavefront giữ màu user, pixel wavefront alpha-blend từ user → emotion. Mục đích: giữ liên tục màu nền user trong khi overlay nhanh.

Source: `hal/drivers/rgb/effects.py:pulse()`; emotion path ở `hal/app_state.py:_apply_emotion_led_display()` (base đen mặc định), transient path ở `hal/routes/led.py:start_led_effect()` (base = `_get_user_base_color()` khi `transient=true`).
