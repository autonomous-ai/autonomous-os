# Speaker Lifecycle — Mute/Unmute Audio Output

Suppress all audio output (TTS, music, backchannel) khi user không muốn Lamp phát âm thanh.

## Design

Software flag `_speaker_muted`. TTS/music/backchannel check flag trước khi play → skip. Volume hardware giữ nguyên, unmute → hoạt động lại ngay.

Khác mic mute: mic mute = Lamp không nghe (deaf). Speaker mute = Lamp không nói. Mic vẫn on → user vẫn ra lệnh voice để unmute.

## Mute

- Voice: "im đi" / "silent mode" / "be quiet" → `[HW:/speaker/mute:{}]`
- Telegram/web: remote mute
- Meeting mode: "đang họp" → agent gọi `[HW:/voice/mute:{}]` + `[HW:/speaker/mute:{}]` cùng lúc

## Unmute

- Voice: "nói đi" / "unmute" / "you can talk" → `[HW:/speaker/unmute:{}]` (mic vẫn on nên voice command hoạt động)
- Telegram/web toggle

## What Happens When Muted

- TTS: skip (return immediately)
- Music: skip play
- Backchannel: skip
- LED/servo: unaffected
- Mic/STT: unaffected
- Agent reply: text only (Telegram/web), không TTS

## Implementation

1. `server.py`: `_speaker_muted` flag + `POST /speaker/mute` + `POST /speaker/unmute` + include in `GET /audio/status`
2. `tts/service.py`: check flag before play → return early
3. `music_service.py`: check flag before play → skip
4. Voice skill: add speaker mute/unmute markers + meeting mode trigger phrases
5. Web monitor: toggle button

## Open Questions

- "im đi" (persistent mute) vs "shut up" (stop current TTS) — cần phân biệt trong intent/skill
- Meeting mode: 2 HW markers hay 1 combined `[HW:/meeting/start:{}]`?
