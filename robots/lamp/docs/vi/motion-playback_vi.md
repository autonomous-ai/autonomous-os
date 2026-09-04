# Phát chuyển động (Motion Playback)

Recording servo trong `hal/recordings/` được biến thành chuyển động như thế nào.
Source: `hal/drivers/motors/animation_service.py`, còn luật tính nhịp nằm ở
`hal/drivers/motors/recording_timing.py`.

## Nhịp phát

Recording trong `hal/recordings/` là bản ghi teleop ở **20 Hz** (cột `timestamp`, cách nhau 0.05s). Vòng lặp phát trong `hal/drivers/motors/animation_service.py` bước đúng **1 frame mỗi tick** ở `self.fps` (30, `HAL_SERVO_FPS`), nên frame thô sẽ phát sai tốc độ. Vì vậy `_load_recording` resample mọi recording về đúng lưới 1/fps ngay lúc load, còn vòng phát giữ nguyên là một bước duyệt frame đơn giản — không có logic thời gian trong hot path.

Resample làm hai việc:

- **Tôn trọng `timestamp` gốc**, để recording dài đúng bằng lúc ghi. Trước đây bước frame 20 Hz ở nhịp 30 Hz làm mọi animation phát nhanh gấp 1.5×.
- **Giãn những đoạn vượt trần tốc độ** — lấy giá trị nhỏ hơn giữa `SERVO_MAX_DPS` (250 deg/s, `HAL_SERVO_MAX_DPS`; đặt 0 để tắt) và `motion.max_speed` mà body khai báo. STS3215 chỉ đạt tối đa khoảng 270 deg/s, trong khi nhiều recording được ghi vượt xa mức đó; trên Lamp thì mức khai báo 120 deg/s mới là mức có hiệu lực, và nó giãn `laugh` (+20%), `playful`, `headshake`, `acknowledge`. Không recording `music_*` nào bị ảnh hưởng nên các đoạn groove giữ nguyên nhịp.

Ngưỡng này không phải để làm đẹp. Đo trên lamp-0c89 bằng cách đọc `Present_Position` trong lúc phát `greeting`: khi lệnh đòi 554 deg/s, servo bị bỏ lại **55° so với mục tiêu** — nó bão hòa, lết, rồi giật, và đó chính là tiếng ồn nghe được. Việc giãn rất chọn lọc, chỉ chạm vào những đoạn bất khả thi:

| recording | gốc | tốc độ đòi hỏi | sau resample | giãn |
|---|---|---|---|---|
| `greeting` | 2.95s | 554 deg/s | 3.49s | +18% |
| `happy_wiggle` | 7.95s | 476 deg/s | 8.27s | +4% |
| `nod` | 1.95s | 302 deg/s | 2.00s | +2.6% |
| `idle` | 9.95s | 115 deg/s | 9.97s | không |

`idle` vốn đã nằm trong ngưỡng nên không bị giãn chút nào — nhưng nó lại hưởng lợi nhiều nhất, vì vòng lặp không còn tụt xuống 5 Hz khi idle đã settle. Mức giảm đó bước frame lưới fps chậm gấp sáu lần và biến nhịp thở thành năm cú giật thấy rõ mỗi giây; nó tốn về độ mượt nhiều hơn phần CPU tiết kiệm được.

Luật này chạy cả khi không có phần cứng. `MockMotionService` (`HAL_BOARD=sim`, thân máy chạy trên laptop qua `make sim`) import `resample_recording` từ đúng module đó và phát trên cùng lưới 30 Hz, nên một recording tốn đúng bằng thời gian thực như trên thân máy thật. `move_to`/`aim`/`nudge` của nó nội suy theo `duration` được lệnh và chặn cho tới khi tới nơi, y như driver dùng SDK. Thứ simulator vẫn KHÔNG mô hình hoá: quán tính, va chạm, torque thật, và calibration EEPROM riêng của từng con servo.

Hai hệ quả cần biết:

- Thanh ghi chuyển động của servo giữ nguyên default (`Acceleration=254`, `Goal_Velocity=0`). Hướng kẹp tốc độ ngay tại servo thay vì tại trajectory đã được đo và loại bỏ: nó giảm jerk ~33% nhưng đẩy sai số bám từ 55° lên 71°, và chính độ lết đó làm cánh tay chúi vào kẹt cơ khí. Phần mềm biết trước toàn bộ trajectory nên giãn được có kiểm soát; servo thì chỉ biết lết.
- `add_recording()` (đường upload) **xoá cache** chứ không nạp vào. Nó nhận frame đã bị bỏ `timestamp`, nên nạp vào sẽ bỏ qua resample — bản upload phát ở frame rate thô trong khi cùng file đó đọc từ đĩa lại phát đúng.
