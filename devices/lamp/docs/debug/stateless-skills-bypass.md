# Stateless skills — bypass OpenClaw session

## Bối cảnh

Đặt câu hỏi: wellbeing và music-suggestion có thể tạo session mới mỗi lần fire không, để khỏi bleed vào voice session?

## Hiện trạng

- `motion.activity` (wellbeing) và `emotion.detected` (music-suggestion) fire từ HAL → Lamp `/sensing` POST → `SendChatMessageWithRun` (`lamp/internal/openclaw/service_chat.go:88`) → OpenClaw `chat.send` RPC.
- `chat.send` luôn route vào `sessionKey` hiện tại của Lamp (`service_chat.go:125-128`) → dùng chung session với voice.
- Wellbeing và music-suggestion **không phải cron**; là event-driven, fire trong cùng session voice.

## Phương án "tạo session mới" — issue gì?

1. **`IsBusy` global** (`service_events.go:42-55`) — flag duy nhất, không phân session. Voice fire → mọi sensing bị queue/drop. Tách session nhưng giữ flag global → vẫn tuần tự.
2. **HW contention** — 1 loa, 1 servo, 1 LED. Voice + nudge LLM song song sẽ đè TTS, stomp servo, emotion marker cuối thắng.
3. **Lifecycle session** — phải track key, recover sau reconnect, tự gọi `sessions.new` trước khi compact (~80k token).
4. **First-fire chậm** — session mới load SOUL + KNOWLEDGE + ~20 skills, +2–5s/fire.
5. **Flow Monitor UI** — hiện assume 1 session, phải sửa để render nhiều lane.

→ Muốn song song thật cần 3 việc: session split + per-session `IsBusy` + HW arbiter ở lelamp. Chỉ làm bước 1 = không khác hôm nay.

## Insight then-key

**Mấy skill reactive đa số không cần history.**

| Loại | Cần OpenClaw session? |
|---|---|
| Voice / Telegram chat | Có — multi-turn, cần history |
| Wellbeing, music-suggestion, user-emotion-detection, mood, sensing-react, guard-react | Không — stateless, đọc JSONL của riêng mình |

Session = persistent context store. Skill stateless gửi vào session nào cũng lãng phí.

## Phương án đề xuất

**Bypass OpenClaw hoàn toàn cho skill stateless:**

- Lamp gọi LLM one-shot trực tiếp (GPT 5.5 — model đang dùng; hoặc Haiku/Sonnet tùy chọn).
- System prompt = SKILL.md content + event context.
- Output = `<say>...</say>` → đẩy thẳng speak-queue ở lelamp.

Speak-queue ở lelamp: voice ưu tiên cao, nudge thấp. Voice đang nói → nudge vào hàng đợi, voice xong thì speak.

## Lợi thế

- Không đụng `IsBusy`, không đụng session.
- ~500ms–1s/fire (one-shot, không load skills/SOUL/KNOWLEDGE).
- Không bleed vào voice memory.
- Không bị compaction nuốt SKILL rules (vấn đề `project_openclaw_compaction_summary_risk`).
- Không cần per-session-busy flag, không cần Flow Monitor multi-lane.

OpenClaw chỉ lo voice/chat — đúng việc của nó. Lamp + Haiku xử lý reactive.

## Skill ứng viên di chuyển

- `wellbeing` — đã đọc JSONL, decision deterministic, chỉ LLM phrase 1 câu.
- `music-suggestion` — cooldown + mood-history JSONL, chọn genre table/habit, suggest 1 câu.
- `user-emotion-detection` — log mood signal + decision dựa trên emotion event.
- `mood` — log dựa trên event, decision từ mood-history JSONL.
- `sensing` (presence/sound/light reactions) — phản ứng emotion marker + 1 câu, ko cần history.
- `guard` reactions — boolean state + event, ko cần history.

## Skill phải giữ ở OpenClaw

- `voice` — multi-turn conversation.
- Telegram chat — cùng nguyên do.
- Bất cứ flow nào reference "lúc nãy bạn nói…".
