# Motion Activity Whitelist

Only these Kinect action classes are forwarded to OpenClaw as `motion.activity` events. All others are filtered at HAL level to save tokens.

Chỉ những action classes dưới đây được forward lên OpenClaw dạng `motion.activity`. Còn lại bị filter ở HAL để tiết kiệm token.

HAL does the categorisation before sending. On the `Activity detected:` line:
- Drink actions (listed below) collapse to the bucket name `drink`.
- Break actions (listed below) collapse to the bucket name `break`.
- Sedentary actions are emitted as raw Kinetics labels (no collapsing) so the agent can ground nudge phrasing + music genre in the specific activity.
- Emotional actions are filtered out entirely — they do not appear on `motion.activity`. A dedicated `motion.emotional` event will carry them later.

HAL đã categorize trước khi gửi. Trên dòng `Activity detected:`:
- Action drink (liệt kê dưới) gộp thành bucket name `drink`.
- Action break (liệt kê dưới) gộp thành bucket name `break`.
- Action sedentary giữ raw Kinetics label (không gộp) để agent có context cụ thể cho nudge + music genre.
- Action cảm xúc bị filter hoàn toàn — không xuất hiện trên `motion.activity`. Sẽ có event `motion.emotional` riêng sau.

## drink — reset hydration timer / Reset timer nhắc uống nước

- drinking — uống nước
- drinking beer — uống bia
- drinking shots — uống shot
- tasting beer — nếm bia
- opening bottle — mở chai
- making tea — pha trà

## break — reset break timer / Reset timer nhắc nghỉ (ăn, vận động, tương tác)

- tasting food — nếm đồ ăn
- stretching arm — vươn tay
- stretching leg — vươn chân
- dining — ăn cơm
- eating burger, eating cake, eating carrots, eating chips, eating doughnuts, eating hotdog, eating ice cream, eating spaghetti, eating watermelon
- applauding — vỗ tay (khen)
- clapping — vỗ tay
- celebrating — ăn mừng
- sneezing — hắt xì
- sniffing — hít mũi
- hugging — ôm
- kissing — hôn
- headbanging — lắc đầu theo nhạc
- sticking tongue out — lè lưỡi

## sedentary — create wellbeing crons + trigger Music suggestion / Ngồi yên, tạo wellbeing crons + kích hoạt Music suggestion

- using computer — dùng máy tính
- writing — viết
- texting — nhắn tin
- reading book — đọc sách
- reading newspaper — đọc báo
- drawing — vẽ
- playing controller — chơi game

## emotional — always speak, log mood / Cảm xúc, luôn nói, ghi mood

- laughing — cười
- crying — khóc
- yawning — ngáp
- singing — hát
