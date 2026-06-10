# Chăm Sóc Sức Khỏe Chủ Động (Wellbeing — Hydration + Break)

> Lamp chủ động nhắc uống nước và nghỉ ngơi — AI tự schedule, tự quan sát qua camera, tự học thói quen từng người.

> **⚠️ 2026-04-17 — Storage đã đổi.** File này còn đúng về cơ chế cron + flow agent. Nhưng chi tiết về `wellbeing.md` summary và `wellbeing/YYYY-MM-DD.md` daily log đã **deprecated** — giờ dùng JSONL schema `{ts, seq, hour, action, notes}` mirror mood. Xem `docs/vi/sensing-behavior_vi.md` và `docs/vi/os-server_vi.md` cho đúng endpoint. Endpoint `POST /user/wellbeing/log`, `/summary`, `/today` trên HAL đã bị gỡ — thay bằng `POST http://127.0.0.1:5000/api/wellbeing/log` + `GET /api/openclaw/wellbeing-history` trên Lamp.

---

## Tổng quan

Lamp theo dõi sức khỏe người dùng qua 2 loại nhắc nhở:

| Loại | Mục đích | Default interval | Emotion |
|------|----------|-----------------|---------|
| **Hydration** | Nhắc uống nước | ~45 phút (2700000ms) | `caring` (0.5) |
| **Break** | Nhắc đứng dậy/vươn vai | ~30 phút (1800000ms) | `caring` (0.6) |

Toàn bộ logic nằm trong LLM (OpenClaw agent) — không có hard code timer. AI tự schedule cron jobs và tự quyết định có nhắc hay không.

**Đặc điểm:**
- Per-user: mỗi người quen có thói quen riêng, Lamp nhớ riêng. Người lạ chung vào `"unknown"`.
- AI học từ quan sát: user hay bỏ qua nhắc buổi sáng? hay mệt lúc 15h?
- **Nhắc cho tất cả** — friend và stranger đều được chăm sóc. Stranger dùng `"unknown"` làm tên, share chung 1 bộ cron.
- Cron chỉ tạo khi phát hiện **hoạt động tĩnh** (ngồi xài máy tính, đọc sách, chơi game) qua `motion.activity` — KHÔNG tạo khi `presence.enter`. Tránh tạo/xóa cron liên tục khi người đi qua mà không ngồi xuống.
- Khi cron fire thì cứ nhắc — không cần check presence (cron chỉ active khi có người).

---

## Luồng hoạt động

### 1. Bootstrap — Khi phát hiện hoạt động tĩnh

```
Camera detect face → [sensing:presence.enter]
    ↓
Agent greeting (không tạo cron ở đây)
    ↓
Camera detect motion → [sensing:motion.activity] — "using computer"
    ↓
Agent: hoạt động tĩnh → bắt đầu wellbeing setup (im lặng)
    ↓
Step 1: Đọc wellbeing summary + daily log
  → GET /user/wellbeing/summary?name={name}
  → GET /user/wellbeing/today?name={name}
    ↓
Step 2: cron.list() → check cron đã có chưa
    ↓
Step 3: Schedule 2 cron jobs (nếu chưa có)

  cron.add:
    name: "Wellbeing: {name} hydration"
    schedule: {kind: "every", everyMs: 2700000}  (45 phút)
    sessionTarget: "current", payload.kind: "systemEvent"
    text: "[MUST-SPEAK] Wellbeing hydration check. Remind water... prefix [HW:/broadcast][HW:/emotion]..."

  cron.add:
    name: "Wellbeing: {name} break"
    schedule: {kind: "every", everyMs: 1800000}  (30 phút)
    sessionTarget: "current", payload.kind: "systemEvent"
    text: "[MUST-SPEAK] Wellbeing break check. Suggest stretch... prefix [HW:/broadcast][HW:/emotion]..."
```

**{name}** = tên người quen từ `presence.enter` trước đó, hoặc `"unknown"` nếu là người lạ.

**Kết quả:** 2 cron jobs chạy song song, mỗi cái fire theo interval riêng.

### 2. Cron fire — AI đánh giá và quyết định

```
Cron fire (hydration hoặc break) → agent turn mới
    ↓
Agent chạy theo payload instruction:

  Step 1: GET /camera/snapshot → chụp ảnh user
    ↓
  Step 2: GET /presence → user có đang ngồi không?
          → Không present → im lặng, skip
    ↓
  Step 3: AI nhìn ảnh và đánh giá:

  [Hydration cron]
    - User đang cầm ly/chai nước? → không nhắc
    - User vừa mới ngồi xuống? → chưa cần nhắc
    - Không thấy nước, ngồi lâu → nhắc 1 câu ngắn

  [Break cron]
    - User đang vươn vai/đứng dậy? → không nhắc (reset timer thay vì nhắc)
    - User ngồi lâu, tư thế xấu? → nhắc đứng dậy
    - User trông mệt? → nhắc nghỉ
    - User trông ổn, đang tập trung? → không nhắc
    ↓
  Step 4: Quyết định
    A) Nhắc → 1 câu ngắn + [HW:/emotion:{caring}] + [HW:/broadcast:{}]
    B) Im lặng → NO_REPLY (vẫn có emotion marker)
```

### 3. Lifecycle end — TTS + Broadcast Telegram

```
Agent trả lời xong → SSE lifecycle phase="end"
    ↓
Go handler xử lý (giống music suggestion):

  1. flushAssistantText() → lấy text + extract HW markers
     VD: "[HW:/emotion:{caring,0.5}][HW:/broadcast:{}] Uống nước đi nhé!"
    ↓
  2. fireHWCalls():
     → POST /emotion {caring, 0.5} → HAL (LED biểu cảm)
     → /broadcast detected → đánh dấu broadcast
    ↓
  3. Kiểm tra kết quả:
     - NO_REPLY → skip TTS
     - Có text  → TTS nói qua speaker
     - Broadcast → gửi text lên Telegram (user thấy trên điện thoại)
```

### 4. Motion activity — Reset timer thông minh

```
User vươn vai hoặc uống nước → camera detect movement
    ↓
HAL gửi [sensing:motion.activity] kèm snapshot
    ↓
Agent nhìn ảnh → phân tích user đang làm gì:

  User uống nước / cầm ly:
    → cron.list() → tìm "Wellbeing: {name} hydration"
    → cron.remove() → cron.add() lại cùng interval
    → Timer hydration reset về 0

  User vươn vai / đứng dậy / đi lại:
    → cron.list() → tìm "Wellbeing: {name} break"
    → cron.remove() → cron.add() lại cùng interval
    → Timer break reset về 0

  Cả hai hành động:
    → Reset cả 2 cron
```

**Ý nghĩa:** Nếu user tự uống nước, Lamp nhận ra và đặt lại timer — không nhắc thừa.

### 5. Presence leave — Cleanup + ghi notebook

```
User rời khỏi camera → [sensing:presence.leave]
    ↓
Agent xử lý:

  1. cron.list() → cron.remove() cả 2 wellbeing crons
    ↓
  2. Ghi daily log:
     → /root/local/users/{name}/wellbeing/YYYY-MM-DD.md
     → Nội dung: nhắc gì hôm nay, user phản hồi ra sao, quan sát gì
    ↓
  3. Cập nhật summary (nếu phát hiện pattern mới):
     → /root/local/users/{name}/wellbeing.md
     → VD: thêm "hay bỏ qua hydration buổi sáng" hoặc "phản hồi tốt break buổi chiều"
```

**Lưu ý:** Chỉ cancel khi `presence.leave` — KHÔNG cancel khi `presence.away` (dim đèn sau 5 phút không motion).

---

## Dữ liệu per-user

### Cấu trúc folder

```
/root/local/users/{name}/
  ├── wellbeing.md                    ← summary tích lũy
  ├── wellbeing/2026-04-09.md         ← daily log
  ├── wellbeing/2026-04-10.md
  └── mood/2026-04-10.jsonl           ← mood history (có wellbeing events)
```

### wellbeing.md — Summary tích lũy

AI tự viết và cập nhật. Ví dụ:

```markdown
# Alice — Wellbeing Summary

- Hay bỏ qua nhắc uống nước buổi sáng (trước 10h)
- Phản hồi tốt với nhắc break sau 15:00
- Thích reminder nhẹ nhàng, không thích bị nhắc liên tục
- Thường mệt khoảng 14:00-15:00 (post-lunch dip)
- Hay tự vươn vai sau ~40 phút ngồi
```

### wellbeing/YYYY-MM-DD.md — Daily log

AI ghi khi `presence.leave`. Ví dụ:

```markdown
# 2026-04-10

- 09:15 presence.enter
- 09:40 hydration reminder — ignored (no response)
- 10:05 hydration reminder — user grabbed water bottle
- 10:50 break reminder — user stood up and stretched
- 11:35 break reminder — skipped (user looked energized)
- 12:00 presence.leave

Observations: Bỏ qua hydration đầu tiên, phản hồi tốt từ lần 2.
```

### Mood history (wellbeing events)

`mood.go` log 2 event types liên quan:

| Event | Khi nào | Ý nghĩa |
|-------|---------|---------|
| `wellbeing.hydration` | Sensing event từ HAL (legacy, hiếm) | Timer cũ fire |
| `wellbeing.break` | Sensing event từ HAL (legacy, hiếm) | Timer cũ fire |
| `mood.assessed` | Sau mỗi agent turn | AI đã evaluate — có `emotion`, `response`, `no_reply` |

**Lưu ý:** Với flow hiện tại (AI-driven cron), wellbeing cron turns không đi qua sensing handler → không tạo `wellbeing.hydration` / `wellbeing.break` event trong mood history. Mood assessment vẫn được log nếu có `mood.TrackRun`.

---

## Các layer và file liên quan

### Go server (Lamp)

| File | Vai trò |
|------|---------|
| `lamp/server/openclaw/delivery/sse/handler.go` | Xử lý lifecycle end: fire HW calls (emotion), broadcast qua Telegram, suppress TTS nếu cần |
| `lamp/lib/mood/mood.go` | Log `wellbeing.hydration`, `wellbeing.break` events. `IsMoodEvent()` whitelist cả 2 type |

### OpenClaw Skills

| File | Vai trò |
|------|---------|
| `lamp/resources/openclaw-skills/sensing/SKILL.md` | Toàn bộ wellbeing logic: bootstrap crons, science reference, principles, presence.enter/leave workflow, motion activity reset |
| `lamp/internal/openclaw/resources/SOUL.md` | Định nghĩa user folder structure (wellbeing.md, wellbeing/YYYY-MM-DD.md) |

### HAL (Python)

| File | Vai trò |
|------|---------|
| `os/hal/drivers/sensing/perceptions/wellbeing.py` | **Stub/no-op** — class placeholder, tất cả logic đã chuyển sang AI cron |
| `os/hal/drivers/sensing/sensing_service.py` | Instantiate WellbeingPerception (no-op) trong sensing pipeline |

### Frontend (React)

| File | Vai trò |
|------|---------|
| `lamp/web/src/pages/monitor/FlowSection/types.ts` | Icon mapping: `wellbeing.hydration` → 💧, `wellbeing.break` → 🧘 |
| `lamp/web/src/pages/monitor/FlowSection/index.tsx` | Hiển thị wellbeing events trong flow diagram |

---

## Science reference (mặc định lần đầu)

AI dùng bảng này khi chưa có dữ liệu học từ user:

| Chủ đề | Khuyến nghị | Nguồn |
|--------|------------|-------|
| Uống nước | 200-250 ml mỗi 20-30 phút khi ngồi | Mayo Clinic, EFSA |
| Nghỉ ngồi | Đứng dậy/vươn vai mỗi 45-60 phút | WHO sedentary behavior guidelines |
| Mệt tư thế | Dấu hiệu xuất hiện sau 30-50 phút ngồi tĩnh | Cornell University ergonomics |
| Mỏi mắt (20-20-20) | Mỗi 20 phút, nhìn xa 6m trong 20 giây | American Academy of Ophthalmology |
| Giờ mệt cao điểm | Thường 13:00-15:00 (post-lunch dip) | Circadian rhythm research |

Sau vài session, AI sẽ override bằng dữ liệu thực từ wellbeing notebook.

---

## Cách test

### Điều kiện tiên quyết

- Lamp Go server đang chạy (port 5000)
- HAL đang chạy (port 5001) với camera hoạt động
- OpenClaw agent connected
- Camera thấy được user (cho presence detection + snapshot)

### Test 1: Bootstrap cron khi presence.enter

**Mục tiêu:** Verify AI tạo 2 wellbeing cron jobs khi owner ngồi vào.

**Bước thực hiện:**
1. Ngồi trước camera → chờ `presence.enter`
2. Chờ agent greeting xong
3. Kiểm tra agent đã tạo cron

**Kết quả mong đợi:**
- Agent đọc wellbeing.md (nếu có)
- Agent tạo 2 cron jobs: `"Wellbeing: {name} hydration"` và `"Wellbeing: {name} break"`
- Không thông báo việc tạo cron (im lặng)

**Verify trên Flow Monitor:**
- Mở web UI → Monitor page
- Xem flow: `sensing_input` (presence.enter) → agent tool calls (cron.list, cron.add)

### Test 2: Hydration cron fire → nhắc uống nước

**Mục tiêu:** Verify AI nhắc uống nước khi cron fire.

**Bước thực hiện:**
1. Ngồi trước camera (presence = present)
2. Không cầm ly/chai nước
3. Chờ hydration cron fire (theo interval đã set)

**Kết quả mong đợi:**
- Agent chụp ảnh camera
- Agent check presence
- Agent nhìn ảnh → không thấy nước → nhắc 1 câu ngắn
- TTS nói qua speaker (emotion: caring)
- Telegram nhận được cùng text (broadcast)

**Verify:**
```bash
# Xem agent log
journalctl -u os-server | grep -i "caring\|hydration\|water"
```

### Test 3: Hydration cron fire → user đang uống → skip

**Mục tiêu:** Verify AI không nhắc khi user đang uống nước.

**Bước thực hiện:**
1. Cầm ly/chai nước trước camera
2. Chờ hydration cron fire

**Kết quả mong đợi:**
- Agent chụp ảnh → thấy user cầm nước → NO_REPLY
- Không TTS, không broadcast
- Flow Monitor hiển thị `[no reply]`

### Test 4: Break cron fire → nhắc nghỉ

**Mục tiêu:** Verify AI nhắc đứng dậy khi ngồi lâu.

**Bước thực hiện:**
1. Ngồi trước camera liên tục
2. Chờ break cron fire

**Kết quả mong đợi:**
- Agent chụp ảnh → đánh giá tư thế/mệt mỏi
- Nếu ngồi lâu/tư thế xấu → nhắc 1 câu (emotion: caring 0.6)
- Nếu user trông ổn → có thể NO_REPLY

### Test 5: Motion activity → reset hydration timer

**Mục tiêu:** Verify cron reset khi user tự uống nước.

**Bước thực hiện:**
1. Wellbeing crons đang chạy
2. Cầm ly nước uống trước camera → trigger `motion.activity`
3. Quan sát agent response

**Kết quả mong đợi:**
- Agent nhận `[sensing:motion.activity]` kèm snapshot
- Agent nhìn ảnh → thấy user uống nước
- Agent gọi cron.remove → cron.add lại hydration cron (reset timer)
- Hydration timer bắt đầu lại từ 0

**Verify:**
- Flow Monitor: xem tool calls cron.remove + cron.add cho hydration cron

### Test 6: Motion activity → reset break timer

**Mục tiêu:** Verify cron reset khi user tự vươn vai.

**Bước thực hiện:**
1. Wellbeing crons đang chạy
2. Đứng dậy/vươn vai trước camera → trigger `motion.activity`

**Kết quả mong đợi:**
- Agent nhìn ảnh → thấy user vươn vai/đứng dậy
- Agent reset break cron (remove + add lại)

### Test 7: Presence leave → cleanup + ghi notebook

**Mục tiêu:** Verify cron bị cancel và notebook được cập nhật khi user rời đi.

**Bước thực hiện:**
1. Wellbeing crons đang chạy
2. Rời khỏi camera → chờ `presence.leave`

**Kết quả mong đợi:**
- Agent cancel cả 2 wellbeing cron jobs
- Agent ghi daily log tại `/root/local/users/{name}/wellbeing/YYYY-MM-DD.md`
- Agent cập nhật summary `wellbeing.md` nếu phát hiện pattern mới

**Verify (trên Pi):**
```bash
# Xem daily log
cat /root/local/users/<name>/wellbeing/$(date +%Y-%m-%d).md

# Xem summary
cat /root/local/users/<name>/wellbeing.md
```

### Test 8: Stranger không nhận wellbeing

**Mục tiêu:** Verify stranger không được schedule cron.

**Bước thực hiện:**
1. Để người lạ (chưa enroll face) ngồi trước camera
2. Chờ `presence.enter` (stranger detected)

**Kết quả mong đợi:**
- Agent greeting bình thường (emotion: curious, cautious)
- KHÔNG tạo wellbeing cron jobs
- KHÔNG đọc/ghi wellbeing notebook

### Test 9: User nói "đừng nhắc nữa"

**Mục tiêu:** Verify AI tôn trọng user preference.

**Bước thực hiện:**
1. Wellbeing crons đang chạy
2. Nói "đừng nhắc tôi uống nước nữa"

**Kết quả mong đợi:**
- Agent ghi vào wellbeing.md: "user không muốn nhắc hydration"
- Agent remove hydration cron
- Các lần presence.enter sau: không schedule hydration cron (đọc notebook trước)

### Test 10: Multi-user — 2 người khác nhau

**Mục tiêu:** Verify mỗi người có wellbeing riêng.

**Bước thực hiện:**
1. User A (Alice) ngồi vào → wellbeing crons tạo cho Alice
2. Alice rời đi → crons cancel, notebook ghi
3. User B (Bob) ngồi vào → wellbeing crons tạo cho Bob

**Kết quả mong đợi:**
- Alice có folder `/root/local/users/alice/wellbeing/`
- Bob có folder `/root/local/users/bob/wellbeing/`
- Cron jobs tên khác nhau: `"Wellbeing: alice hydration"` vs `"Wellbeing: bob hydration"`
- Thói quen Alice không ảnh hưởng Bob

---

## Monitoring & Debug

### Flow Monitor (Web UI)

| Event type | Icon | Ý nghĩa |
|-----------|------|---------|
| `sensing_input` (presence.enter) | 👤 | User đến, trigger bootstrap |
| `sensing_input` (motion.activity) | 🏃 | User cử động, có thể reset timer |
| `wellbeing.hydration` | 💧 | Hydration event (legacy) |
| `wellbeing.break` | 🧘 | Break event (legacy) |
| `hw_emotion` | 😊 | Emotion marker fired (caring) |
| `no_reply` | — | Agent quyết định không nhắc |

### Logs quan trọng

```bash
# Xem wellbeing-related logs
journalctl -u os-server | grep -i "wellbeing\|caring\|broadcast\|cron"
```

| Log message | Ý nghĩa |
|------------|---------|
| `HW marker fired /emotion` | Emotion caring đã gửi đến HAL |
| `broadcast run response to channels` | Nhắc nhở đã gửi lên Telegram |
| `agent replied NO_REPLY` | AI quyết định không nhắc lần này |

### Kiểm tra cron jobs đang chạy

Không có API trực tiếp từ Go server để list cron. Cron được quản lý bởi OpenClaw. Kiểm tra qua agent log hoặc xem cron.list output trong Flow Monitor tool calls.

### Kiểm tra wellbeing data trên Pi

```bash
# List users
ls /root/local/users/

# Xem summary
cat /root/local/users/<name>/wellbeing.md

# Xem daily logs
ls /root/local/users/<name>/wellbeing/
cat /root/local/users/<name>/wellbeing/$(date +%Y-%m-%d).md

# Xem mood history có wellbeing events
curl -s "http://127.0.0.1:5000/api/openclaw/mood-history?date=$(date +%Y-%m-%d)&last=200" | jq '.data.events[] | select(.event | startswith("wellbeing"))'
```

---

## So sánh với hệ thống cũ

| Khía cạnh | Cũ (timer Python) | Hiện tại (AI-driven cron) |
|-----------|-------------------|---------------------------|
| Trigger | `WellbeingPerception` timer cứng | OpenClaw cron, AI tự schedule |
| Interval | Config qua env var, cần restart | AI tự quyết, thay đổi runtime |
| Đánh giá | Không — luôn fire | AI chụp ảnh, nhìn user, quyết định |
| Per-user | Không | Có — mỗi người folder riêng |
| Học thói quen | Không | Có — wellbeing.md + daily logs |
| Reset timer | Không | Có — motion.activity reset khi user tự uống/vươn vai |
| Code | `os/hal/drivers/sensing/perceptions/wellbeing.py` (active) | Stub no-op, logic trong SKILL.md |

---

## Hạn chế hiện tại

1. **Toàn bộ logic nằm trong LLM** — Nếu AI quên tạo cron, quên ghi notebook, hoặc bỏ qua instruction → không có fallback. Go server không có wellbeing-specific logic.

2. **Interval trong SKILL.md là `<interval_ms>` placeholder** — Không có default number cụ thể trong cron.add template (khác với music SKILL đã fix). AI tự chọn dựa trên "principles" text. Có thể dẫn đến interval không consistent giữa các session.

3. **Camera-dependent** — AI cần snapshot để đánh giá. Nếu camera lỗi hoặc snapshot tối → AI có thể quyết định sai (nhắc khi không cần hoặc ngược lại).

4. **Notebook quality phụ thuộc LLM** — Summary và daily log do AI tự viết. Chất lượng ghi chú có thể khác nhau giữa các model (Opus vs Haiku).

5. **Cron cleanup phụ thuộc presence.leave** — Nếu device reboot giữa session (không có presence.leave), cron cũ có thể còn sót. Bootstrap step có cleanup nhưng phụ thuộc AI nhớ chạy.
