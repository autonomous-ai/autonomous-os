# Gợi Ý Nhạc Chủ Động (Music Suggestion)

> Lamp chủ động gợi ý nhạc phù hợp với mood/trạng thái người dùng — **không auto-play**, chỉ gợi ý bằng giọng nói và chờ xác nhận.

---

## Tổng quan

Tính năng này cho phép Lamp **tự quyết định thời điểm** gợi ý nhạc dựa trên:
- **Mood trigger** — khi mood được log (sad, stressed, tired, excited, happy, bored), agent dùng mood đó để suggest nhạc ngay
- **Activity trigger** — khi camera phát hiện hoạt động tĩnh (ngồi máy tính, đọc sách), agent suggest background music
- **Suggestion history** — lưu lại mỗi lần suggest, user accept/reject, để AI learn pattern

Hoàn toàn **AI-driven** — agent tự quyết định dựa trên SKILL.md instructions. Backend chỉ cung cấp API để lưu/đọc history.

---

## Triggers

### 1. Mood Trigger

```
Agent detect mood signal (camera/voice/telegram)
    ↓
POST /api/mood/log {kind:"signal", ...} → ghi raw signal
    ↓
GET /api/openclaw/mood-history?last=15 → đọc signal + decision gần đây
    ↓
Agent tổng hợp mood (fuse signal mới + history + decision cũ)
    ↓
POST /api/mood/log {kind:"decision", based_on, reasoning} → ghi decision
    ↓
Mood SKILL.md: decision mood thuộc [sad, stressed, tired, excited, happy, bored]?
    ↓ (yes)
Follow Music skill "AI-Driven Music Suggestion"
    ↓
Music skill: GET /api/openclaw/mood-history?kind=decision&last=1 → đọc lại decision mới nhất
    ↓
Agent check:
  ├── Audio đang play? (GET /audio/status) → skip nếu playing
  └── Music suggestion gần đây? (GET /music-suggestion-history) → skip nếu < 30 phút
    ↓ (all pass)
Agent suggest nhạc → POST /api/music-suggestion/log
```

### 2. Activity Trigger (Sedentary) — không cần mood

```
Camera detect "using computer" → [sensing:motion.activity] raw label "using computer"
    ↓
Sensing SKILL.md: raw label thuộc nhóm sedentary → follow Music skill Flow B (sedentary)
    ↓
Agent check:
  ├── Audio đang play? (GET /audio/status) → skip nếu playing
  └── Music suggestion gần đây? (GET /music-suggestion-history) → skip nếu < 30 phút
    ↓ (all pass)
    ↓ KHÔNG check mood — sedentary tự đủ context
    ↓
GET /audio/history?person={name}&last=1 → personalize genre
    ↓
Default: lo-fi, ambient, study beats (override nếu có audio history rõ preference)
Optional: nếu có mood decision fresh → refine genre (tired + sedentary → calm piano)
    ↓
Agent suggest background music → POST /api/music-suggestion/log
```

### Unknown Users

Unknown users (strangers) vẫn được suggest nhạc. Data lưu trong `/root/local/users/unknown/`. Khác biệt duy nhất: chỉ speak qua loa (`[HW:/speak]`), không DM vì không có telegram_id.

### Cooldown

- **30 phút** giữa các lần suggestion
- Agent tự check bằng `GET /api/openclaw/music-suggestion-history?user={name}&last=1`
- Áp dụng cho cả mood trigger và activity trigger

---

## Suggestion History

### Storage

`/root/local/users/{user}/music-suggestions/{YYYY-MM-DD}.jsonl`

Mỗi record:
```json
{"ts":1713359400.5,"seq":1713359400500000000,"hour":14,"trigger":"mood:tired","query":"","message":"How about some calm piano?","status":"pending","user":"gray"}
```

| Field | Ý nghĩa |
|-------|---------|
| `seq` | Unix nanoseconds — unique ID cho mỗi suggestion |
| `trigger` | Nguồn trigger: `mood:tired`, `activity:sedentary` |
| `query` | YouTube search query (empty nếu chỉ text suggestion) |
| `message` | Text suggestion gửi cho user |
| `status` | `pending` → `accepted` / `rejected` / `expired` |

### API

| Endpoint | Method | Mục đích |
|----------|--------|----------|
| `/api/music-suggestion/log` | POST | Agent ghi music suggestion event |
| `/api/music-suggestion/status` | POST | Agent update status (accepted/rejected) |
| `/api/openclaw/music-suggestion-history` | GET | Query history (params: `user`, `date`, `last`) |

### Retention

7 ngày — tự động xóa files cũ hơn.

---

## Luồng hoạt động chi tiết

### User confirm → Play music

```
User nói "ừ phát đi" (voice hoặc Telegram reply)
    ↓
Agent:
  1. POST /api/music-suggestion/status → status="accepted"
  2. [HW:/audio/play:{"query":"...","person":"gray"}]
    ↓
Go handler intercept HW markers → POST /audio/play → LeLamp
    ↓
LeLamp: yt-dlp search → ffmpeg → ALSA speaker
```

### User reject → Log rejection

```
User nói "không" hoặc "not now"
    ↓
Agent: POST /api/music-suggestion/status → status="rejected"
```

---

## Các layer và file liên quan

### Go server (Lamp)

| File | Vai trò |
|------|---------|
| `lamp/lib/musicsuggestion/suggestion.go` | Logger JSONL per-user per-day. Log, Query, UpdateStatus, LastSuggestion, Days |
| `lamp/lib/mood/mood.go` | Logger mood events |
| `lamp/server/sensing/delivery/http/handler.go` | PostSuggestionLog/PostSuggestionStatus: API handlers. Motion.activity sedentary nudge agent follow Music skill |
| `lamp/server/openclaw/delivery/sse/handler.go` | SuggestionHistory: GET endpoint |
| `lamp/server/server.go` | Routes: /api/music-suggestion/*, /api/openclaw/music-suggestion-history |

### OpenClaw Skills

| File | Vai trò |
|------|---------|
| `lamp/resources/openclaw-skills/music/SKILL.md` | AI-driven suggestion logic, mood→music mapping, suggestion logging, cooldown check |
| `lamp/resources/openclaw-skills/mood/SKILL.md` | Mood logging → follow Music skill suggestion |
| `lamp/resources/openclaw-skills/sensing/SKILL.md` | Activity groups, sedentary → follow Music skill suggestion |

### LeLamp (Python)

| File | Vai trò |
|------|---------|
| `lelamp/models.py` | FacePersonDetail: includes music_suggestion_days |
| `lelamp/server.py` | /face/owners endpoint: reads music_suggestion_days from JSONL files |

### Frontend (React)

| File | Vai trò |
|------|---------|
| `lamp/web/src/pages/monitor/types.ts` | FaceOwnerDetail: music_suggestion_days field |
| `lamp/web/src/pages/monitor/FaceOwnersSection.tsx` | Hiển thị music_suggestion_days badge + folder tree |

---

## Dữ liệu AI sử dụng

### Music suggestion history (`GET /api/openclaw/music-suggestion-history`)

| Field | Dùng để |
|-------|---------|
| `trigger` | Biết suggestion từ mood hay activity |
| `status` | Learn accept/reject pattern |
| `hour` | Pattern thời gian nào user hay accept |
| `message` | Tránh suggest trùng lặp |

### Audio history (`GET /audio/history?person={name}&last=1`)

| Field | Dùng để |
|-------|---------|
| `query` | Genre/artist signal |
| `duration_s` | Satisfaction: > 180s = enjoyed |
| `stopped_by` | `"end"` = liked, `"user"` < 30s = disliked |

### Learning Rules

- `stopped_by: "end"` + `duration_s` > 180s → suggest similar artist/genre
- `stopped_by: "user"` + `duration_s` < 30s → try different direction
- Multiple `rejected` in suggestion history → reduce frequency / change approach

---

## Speaker conflict

Lamp chỉ có 1 speaker chia sẻ giữa TTS và music:

| Tình huống | Hành vi |
|-----------|---------|
| AI suggest bằng text | TTS nói suggestion → user nghe |
| User confirm → play | suppressTTS → TTS không đè lên nhạc |
| Music đang play + TTS | LeLamp trả 409 — music giữ priority |
| User nói "stop" | `[HW:/audio/stop:{}]` → dừng music |

---

## Monitoring & Debug

### API kiểm tra

```bash
# Suggestion history hôm nay
curl -s "http://<LAMP_IP>:5000/api/openclaw/music-suggestion-history?user=gray&date=$(date +%Y-%m-%d)&last=50"

# Mood history
curl -s "http://<LAMP_IP>:5000/api/openclaw/mood-history?user=gray&date=$(date +%Y-%m-%d)&last=50"

# Audio status
curl -s "http://<LAMP_IP>:5001/audio/status"

# Audio history
curl -s "http://<LAMP_IP>:5001/audio/history?person=gray&last=10"
```

### Web UI

Monitor page → Users section → click vào user → xem `music-suggestions/` folder → click ngày để xem chi tiết.

### Logs

```bash
journalctl -u lamp-server | grep -i "suggestion\|music"
```
