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

## Demo tầm chuyển động — tính ra, không thu sẵn

`POST /servo/demo` (`hal/drivers/motors/range_demo.py`) trình diễn một vòng dạo
qua tầm chuyển động kèm lời thoại: *"hết cỡ bên trái"* ngay khi đế quay sang
trái, rồi sang phải, rồi ngẩng lên và cúi xuống, rồi về chỗ cũ. Không phát hiện
gì và không báo cáo gì — chuyển động cùng lời nói chính là toàn bộ sản phẩm.

Đây là chuyển động duy nhất trong tài liệu này cố ý **không** phải một bản thu,
và lý do nằm ở chính lỗi mà nó thay thế. Khi được yêu cầu trình diễn tầm hoạt
động, trước đây đèn trả lời bằng emotion `scan`: `hal/recordings/scanning.csv`,
360 frame trong 17.95 s, `base_yaw.pos` trải từ **−31.3…+23.1 — tức 54.4° trên
một tầm 270°** — mà lại thuyết minh là *"Tôi sẽ quét trọn tầm của mình… xoay một
vòng đây!"*. Một bản thu không thể đúng theo cấu trúc được. Nó là chuyển động tay
của ai đó bị đóng băng tại thời điểm thu, không có gì trong file nói nó với tới
đâu, và nó sai một cách âm thầm. Waypoint đọc từ `C.YAW_MIN` / `C.YAW_MAX` thì
đúng theo cấu trúc, vẫn đúng khi giới hạn thay đổi, và port sang hằng số của một
robot khác mà không tốn gì.

**Yaw chạy tới giới hạn thật; pitch thì cố ý không.** `WRIST_PITCH_MIN` /
`WRIST_PITCH_MAX` khai báo ±90° còn cánh tay thì không có chừng đó — đo trên
thiết bị lamp-ac82, `wrist_pitch` lên tới −89.55 khi ngẩng (bị chặn bởi soft
limit chứ không phải bởi khớp) và chỉ tới −16.61 khi cúi mà không hề khựng. Dải
khai báo không phải một phép đo, nên các chặng pitch chỉ đi `PITCH_LOOK_DEG`
(25°) tính từ seed pose, có kẹp biên: đúng bằng độ lệch mà vòng nhìn của pha quét
vẫn đi mỗi lần chạy. Tầm đã được kiểm chứng hơn tầm chỉ được khai báo. Các câu
thoại bám theo sự phân đôi đó — pool của yaw nói *"hết cỡ bên trái"*, còn pool
của pitch chỉ nói *"lên như vầy nè"*.

**Đế được tăng tốc rồi trả lại.** `DEMO_YAW_SPEED` (1200 ≈ 80°/s) được ghi vào
`base_yaw` cho màn trình diễn, y như pha quét vẫn làm và cùng một lý do: nếu
không đụng tới, khớp này chỉ chạy ~14°/s, nên một chặng 135° mất ~9 s và câu nói
mô tả nó kết thúc trong khi đèn vẫn còn đang xoay. Được khôi phục trong `finally`,
vì một mức giới hạn bị bỏ quên sẽ theo demo đi ra ngoài và bóp chậm cả idle lẫn
mọi emotion.

**Lời nói do HAL định thời, còn câu chữ thuộc về os-server.** Mỗi chặng gọi
`aim._say(pool)` → `POST /api/sensing/filler` → các pool `demo_*` trong
`system/lib/i18n/fillers.go`. Không cần lượt LLM nào và không tốn token. Một
marker `[HW:...]` không làm được việc này: marker bắn trước TTS, nên một demo
thuyết minh bằng marker sẽ mô tả màn trình diễn đã xong từ đời nào. Câu nói đi
TRƯỚC chặng của nó (nói, rồi mới di chuyển) còn chặng kế tiếp thì chờ
`_wait_until_still` — `move_and_hold` trả về khi đã gửi xong frame chứ không phải
khi servo đã tới nơi, nên không có bước chờ đó thì kịch bản vượt mặt thân máy chỉ
sau hai chặng.

**Các cổng chặn.** Từ chối khi thiết bị đang ngủ và khi motion service đang bị
suppress; từ chối một demo thứ hai chồng lên demo đang chạy. `start()` trả về
ngay và màn trình diễn chạy trên thread riêng — agent đi tới đây qua marker
`[HW:/servo/demo:{}]`, mà `fireHWCall` chỉ cho một POST phần cứng năm giây trong
khi demo dài ~20 s. Nút vật lý abort nó cùng với pha ngắm và pha quét
(`button_actions._stop_active_tracking`): một cú nhấn chỉ dừng cánh tay mà không
dừng lời thoại sẽ để lại một cái đèn đang mô tả những chặng nó không còn thực
hiện nữa. Một demo bị abort sẽ quay về đúng tư thế lúc bắt đầu.
