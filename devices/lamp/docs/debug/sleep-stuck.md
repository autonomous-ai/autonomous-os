# Lamp stuck in sleeping — motion.activity (và mọi event) bị suppress

Observed 2026-04-22 trên Pi (test device). Log lelamp lặp hàng phút:

```
[motion] dedup drop: Activity detected: using computer. (same as last send 246.1s ago)
```

→ Flow Monitor không hiện turn `motion.activity` nào mặc dù user đang thao tác máy tính.

## Root cause

Đèn ở `state._sleeping = True` từ lúc Lamp gửi `emotion=sleepy` (09:46:18, sau câu "Sleep tight. See you later.").

Khi `_sleeping` bật, `sensing_service._send_event` suppress tất cả event ngoại trừ `presence.enter`:

- `/opt/hal/drivers/sensing/sensing_service.py:316-319`
- Repo: `os/hal/drivers/sensing/sensing_service.py` (cùng logic)

Đồng thời `/opt/hal/routes/emotion.py:40-44`:

```python
if state._sleeping and req.emotion not in _WAKE_EMOTIONS:
    return {"status": "ignored", ...}
state._sleeping = req.emotion == EMO_SLEEPY
```

`_WAKE_EMOTIONS = {greeting, stretching, sleepy}`. Lamp chỉ POST `thinking/happy/curious/acknowledge` → bị bỏ qua sớm, không flip `_sleeping` về False → kẹt sleep vĩnh viễn cho tới khi có `greeting`/`stretching`.

User nói "No. I wake up." cũng không giúp: event `voice` gửi tới sensing xong bị sleep-gate chặn, không tới Lamp.

## Bug phụ: dedup log gây hiểu nhầm

`motion.py` update `_last_sent_ts` **trước** khi event thực sự đi qua sensing. Nếu sensing suppress (sleep), perception vẫn coi là đã gửi → tiếp tục log `dedup drop` mỗi 10 s, nhìn như đây là nguyên nhân chính.

Tham chiếu: `os/hal/drivers/sensing/perceptions/motion.py:414-425` (sau khi flush message → check dedup window 300 s → set `_last_sent_ts` → gọi `send_event`).

## Evidence — verified 2026-04-22

| Time | Event |
|---|---|
| 09:46:18 | Lamp: "Sleep tight. See you later. [yawn]" + POST `emotion=sleepy` → `_sleeping=True` |
| 09:46:30 | User nói "No. I wake up." → voice event bị sleep-gate chặn |
| 09:46:43 → 10:05:13 | **Kẹt ~19 phút.** Lamp spam `thinking/happy/curious/acknowledge` — tất cả ignored |
| 10:04:14 | `[motion] flushing: Activity detected: reading newspaper` (label khác → pass dedup) nhưng vẫn `sleeping — suppressed motion.activity` → xác nhận dedup không phải bug chính |
| 10:05:13 | lelamp service **restart** (DisplayService/SensingService/VoiceService stop → start) |
| 10:05:24 | Service up, `_sleeping` reset về default False |

→ Đèn thoát sleep **chỉ nhờ service restart**, không phải wake logic. Không có `emotion=greeting/stretching` nào được gửi. Wake path chưa bao giờ hoạt động trong thực tế.

## Fix ideas (chọn sau)

1. **Wake từ voice wake-phrase.** Nhận diện "wake up"/"dậy đi" ở lelamp (trước khi bọc sleep gate) và flip `state._sleeping = False` + gọi `greeting` anim. Không chờ Lamp.
2. **Lamp agent: wake flow chuẩn.** Khi user nói gì đó trong lúc lamp sleeping, agent phải POST `emotion=greeting` trước rồi mới `thinking`. Hiện agent không biết state sleep → cần expose `/state` hoặc gửi `sleeping=true` kèm mỗi event.
3. **Không tính dedup cho event bị suppress.** Cho `SensingService.send_event` trả về `False` khi drop vì sleep, và perception chỉ update `_last_sent_ts` khi `True`. Tránh log nhiễu + cho phép burst ngay khi wake.
4. **Timeout sleep.** `_sleeping` auto tắt sau N phút (ví dụ 30 min) để tránh kẹt vĩnh viễn khi Lamp/agent quên wake.

## Escape hatch (thủ công khi gặp)

```bash
curl -X POST http://127.0.0.1:5000/emotion \
  -H 'Content-Type: application/json' \
  -d '{"emotion":"greeting","intensity":0.7}'
```

Chạy trên Pi để flip `_sleeping=False` ngay.
