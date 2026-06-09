# Mic Lifecycle — Mute/Unmute

Mic mute for privacy — meetings, calls, or just don't want Lamp listening.

## Current State

- Voice pipeline (`VoiceService`) runs always-on: mic → VAD → wake word → STT → OpenClaw
- Sound perception (`SoundPerception`) in sensing loop: mic → RMS → sound events
- Speaker recognition (`SpeakerRecognizer`): at end of every STT session, forwards the buffered WAV to `dlbackend /audio-recognizer/embed` to identify the speaker
- Speech emotion recognition (`SpeechEmotionService`): when speaker ID succeeds, the **same WAV bytes** are forwarded to `dlbackend /api/dl/ser/recognize` on a separate worker thread; results are bucketed and dedup'd per user before firing `speech_emotion.detected` sensing events. See [Speech Emotion Recognition](speech-emotion.md).
- Wake word detection runs inside VoiceService
- ✅ `POST /voice/mute` / `POST /voice/unmute` — stop/restart VoiceService
- ✅ `GET /voice/status` includes `mic_muted` field
- ✅ GPIO17 button: unmute when muted + debounce + TTS confirm
- ✅ Web monitor: Mute/Unmute toggle on Overview
- ✅ Voice skill: HW markers for mute/unmute
- ❌ Sound perception not stopped on mute (still fires sound events)
- ❌ No auto-mute from scene/emotion triggers yet

## Design: Fully Deaf When Muted

When muted, mic is **completely off** — no STT, no wake word, no sound perception. Fully deaf. Saves CPU, guarantees privacy.

### Mute: Voice command (one-way in)

User says "đừng nghe" / "stop listening" / "I'm in a meeting" → STT processes this last command → agent calls `[HW:/voice/mute:{}]` → TTS says "OK, I'll stop listening. Press the button when you need me." → mic off.

This is the **last thing Lamp says** until button press.

### Unmute: Physical button (one-way out)

GPIO17 button press → mic back on. Simple, reliable, no ambiguity.

Button behavior changes based on state:
- **Mic ON + TTS speaking** → click = stop TTS (current behavior)
- **Mic ON + TTS not speaking** → click = stop music if playing
- **Mic OFF (muted)** → click = unmute mic

```
User: "đừng nghe" ──→ [HW:/voice/mute:{}] ──→ mic OFF (deaf)
                                                    │
                                          GPIO17 click ──→ mic ON
```

### Additional unmute triggers

- **Web monitor toggle** — manual enable from UI
- **Telegram command** — remote "unmute" / "start listening"
- **Timer** — "mute for 1 hour" → auto unmute (optional, agent can set cron)

## What Happens When Muted

| Component | State | Why |
|-----------|-------|-----|
| STT | **OFF** | Privacy — no transcription |
| Wake word | **OFF** | Fully deaf — button is the only way back |
| Sound perception | **OFF** | No sound events |
| Speaker recognition | **OFF** | No STT session → no audio buffer to identify |
| Speech emotion | **OFF** | Submission path is downstream of STT — nothing arrives |
| TTS | **ON** | Lamp can still speak (Telegram, cron triggers) |
| Camera/sensing | **Unaffected** | Separate from mic |
| Music | **ON** | Can still play/stop via Telegram or web |

## Interaction with Camera Lifecycle

| Camera | Mic | Use Case |
|--------|-----|----------|
| ON | ON | Normal — full sensing |
| ON | OFF | Meeting mode — sees but doesn't listen |
| OFF | ON | Visual privacy — hears but doesn't see |
| OFF | OFF | Full privacy — only GPIO17 button wakes |

## Auto-Mute Triggers (optional, same pattern as camera)

- **Scene focus/movie** → auto-mute (user focused, don't interrupt)
- **Scene night/sleepy** → auto-mute (sleeping)
- **Scene energize/relax** → auto-unmute

Manual override respected — if user explicitly muted via voice command, auto triggers skip (same as camera `_manual_override`).

## Implementation Plan

### HAL (Python)

1. ✅ **`server.py`**: Endpoints done:
   - `POST /voice/mute` — stop VoiceService, set `_mic_muted = True`
   - `POST /voice/unmute` — restart VoiceService, clear flag
   - `GET /voice/status` — includes `mic_muted` field
   - **TODO**: mute should also stop sound perception

2. ✅ **GPIO17 button handler** (`_on_stop_button`): Done with debounce (500ms):
   - Muted → unmute + TTS "I'm listening!"
   - Not muted + TTS speaking → stop TTS
   - Not muted + no TTS → stop music

3. ❌ **Scene/emotion auto-mute/unmute**: Not implemented yet. Need `_auto_mic_mute()` / `_auto_mic_unmute()` with `_mic_manual_override` flag (same pattern as camera).

4. ❌ **Sound perception stop on mute**: Not implemented. Currently mute only stops VoiceService, sound perception still fires events.

### OpenClaw Skills

5. ✅ **Voice skill**: Done + uploaded. Mute + unmute HW markers, trigger phrases, Telegram/web unmute.

### Web Monitor

6. ✅ **Overview section**: Mute/Unmute toggle + MUTED badge on Mic line.

### Lamp (Go)

7. ✅ **HW marker dispatch**: `[HW:/voice/mute:{}]` and `[HW:/voice/unmute:{}]` — already handled by generic parser.

## Edge Cases

- **Muted + Telegram message**: Works — Telegram doesn't use mic. Agent responds normally.
- **Muted + TTS triggered**: TTS plays — speaker is output, independent of mic.
- **Muted + presence.enter** (camera on): Camera fires presence event, agent responds via TTS. User hears Lamp but Lamp can't hear back. Acceptable — user can click button if they want to talk.
- **Muted + timer unmute**: Agent sets cron "unmute in 1h" before muting → cron fires → `POST /voice/unmute` → mic back on.
- **Double mute**: `POST /voice/mute` when already muted → no-op, return `already_muted`.
- **Button press during TTS + muted**: Unlikely (how did TTS start if muted? → Telegram trigger). If happens: unmute takes priority over stop-TTS.
