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

## Ổn định

Tốc độ không phải cách duy nhất một recording làm hại thân máy. Từng khớp có thể
nằm trong dải của nó mà *tổ hợp* lại đẩy trọng tâm ra ngoài đế — đo trên lamp-0c89
ngày 4/9/2026, một CSV của bên thứ ba với mọi khớp đều trong dải đã làm lật con
máy nằm nghiêng ([#271]).

Không giới hạn per-joint nào diễn đạt được chuyện này, nên `recording_stability.py`
dựng lại trọng tâm toàn thân cho từng frame và từ chối recording nào vươn xa khỏi
trục đế quá mức thân máy cho phép. Kiểm tra chạy bên trong `resample_recording`, nên
thân máy thật và simulator từ chối cùng một clip.

Không có gì trong phần kiểm tra này là riêng của lamp. Nó cần hai **khai báo theo
từng thân máy**, thiếu một trong hai là gate nằm im:

| thứ gì | ở đâu | lamp |
|---|---|---|
| ngưỡng | `SAFETY.md` `motion.max_cog_offset_mm` | 22 mm |
| hình học | `ROBOT.md` `urdf_ref` → URDF trong thư mục device | `urdf/lamp.urdf` |

Presence-driven như mọi bound khác trong `robots/contract/SAFETY-SPEC.md`: thân máy
không khai gì thì không bị giới hạn, còn thân máy khai ngưỡng nhưng không có
`urdf_ref` dùng được sẽ ghi log rằng không chấm được tư thế rồi cho qua, chứ không
fail-closed. Reachy Mini hiện không khai cái nào nên không bị ảnh hưởng.

Ngưỡng này được kẹp giữa hai mốc đo được, không phải chọn bừa:

| | trọng tâm lệch khỏi trục đế (đỉnh) |
|---|---|
| clip đã làm lật đèn | 31,6 mm |
| `confused.csv` — recording nặng nhất đang ship | 17,7 mm |
| 28 recording còn lại | ≤ 17,6 mm |

Cả 29 recording đang ship đều qua với khoảng dư thoải mái. Thứ quyết định là tay
vươn ra trước: `base_yaw` xoay cánh tay quanh đúng cái trục dùng để đo nên không
đóng góp gì, còn `elbow_pitch` ảnh hưởng mạnh nhất (frame tệ nhất của clip làm lật
có tay gập ra trước ở `elbow_pitch` 53,8° trong khi `base_pitch` mới 6,9° — các giá
trị cực trị của từng khớp trong clip đó không bao giờ xảy ra cùng một frame).

Mọi lần nạp đều được ghi log: `INFO` kèm mức đỉnh và frame xảy ra, `WARNING` khi
clip qua được nhưng đã vượt 85% ngưỡng, và `ERROR` kèm nguyên tư thế vi phạm khi bị
từ chối — một lần từ chối phải giải thích được chỉ bằng journal. Recording bị từ
chối sẽ bị đường nạp thông thường bỏ qua, nên hỏng ở mức "thiếu một animation",
không phải crash.

Frame nào có khớp không nằm trong URDF của thân máy sẽ bị **bỏ qua và ghi log rõ là
bỏ qua** chứ không chấm điểm: mọi tên khớp lạ sẽ được đọc thành 0° và trả về một con
số dễ chịu cho tư thế chưa từng được đánh giá — một lần "pass" giả còn tệ hơn là
không kiểm tra.

Cho thân máy khác dùng gate này chỉ tốn hai khai báo, không phải code: ship URDF của
nó và tự suy ra ngưỡng từ thư viện animation của chính nó (clip rộng nhất cộng dư
địa). Đừng chép lại 22 mm — đó là milimét của hình học và khối lượng *của thân máy
này*, không phải hằng số chung.

`urdf/lamp.urdf` chỉ có động học và khối lượng; repo này không ship mesh nên phần
visual và collision đã được bỏ. Nó theo gói device profile lên máy qua
`make upload-device lamp`. Khối lượng từng link là số
ước lượng và các gốc inertial trong URDF đều bằng 0, nên khối lượng mỗi link được
đặt ngay tại gốc của nó: con số milimét tuyệt đối là xấp xỉ, còn thứ hạng giữa các
clip thì không. Phải tính lại hằng số này nếu phân bố khối lượng của thân máy thay đổi.

[#271]: https://github.com/autonomous-ai/autonomous-os/issues/271
