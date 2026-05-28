---
name: voice
description: TTS speech + mic/speaker mute for privacy. MUST trigger on meetings, calls, privacy, silence requests. "meeting"/"call"/"private" = mic+speaker mute. "be quiet"/"silent" = speaker mute only. Always call HW markers — never just text.
---

# Voice — Speak Through Speaker

## Quick Start
Your chat replies are automatically spoken aloud through TTS. Use this skill only when you need to speak additional or separate text outside your normal reply (e.g., parallel speech during tool calls, or text different from your chat reply).

## Workflow
1. Determine if you need explicit speech beyond your normal reply:
   - Normal conversational reply -> do NOT call this skill, TTS is automatic
   - Need to speak while also performing tool calls -> use `POST /voice/speak`
   - Need to speak different text than your chat reply -> use `POST /voice/speak`
   - Reacting to a sensing event before reply is finalized -> use `POST /voice/speak`
2. Optionally check if TTS is busy: `GET /voice/status`
3. If `tts_speaking` is true, wait or skip
4. Call `POST /voice/speak` with plain text

## Examples

Input: Normal conversational reply
Output: Do NOT call this skill. Just reply normally — your text is automatically spoken.

Input: You need to greet the user while also activating a scene
Output: Call `POST /voice/speak` with `{"text": "Good morning!"}` in parallel with the Scene API call.

Input: You want to say something different from your chat reply
Output: Call `POST /voice/speak` with the spoken text. Then provide your chat reply separately.

Input: User says "say something" / "tell me a joke"
Output: Do NOT call this skill. Just reply normally with the joke — automatic TTS handles it.

## Tools

Use `Bash` with `curl` to call the HTTP API at `http://127.0.0.1:5001`.

### Speak text
```bash
curl -s -X POST http://127.0.0.1:5001/voice/speak \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello! I am Lamp."}'
```
Text max 2000 characters. Returns immediately; audio plays in background.

### Check voice status
```bash
curl -s http://127.0.0.1:5001/voice/status
```
Response:
```json
{
  "voice_available": true,
  "voice_listening": false,
  "tts_available": true,
  "tts_speaking": false
}
```

## Error Handling
- If `tts_speaking` is true, the speaker is busy. Wait briefly or skip the explicit speech.
- If `voice_available` or `tts_available` is false, inform the user: "Voice output is currently unavailable."
- If the API is unreachable, fall back to chat-only reply. Speech is non-critical.

## Rules
- **Normal replies = automatic TTS.** Do NOT call `/voice/speak` for every response.
- Use `/voice/speak` explicitly only when:
  - You need to say something while ALSO performing tool calls (speech in parallel)
  - You want to speak a different text than your chat reply
  - You are reacting to a sensing event and want to speak before your reply is finalized
- **Keep spoken text plain and short** — 1-3 sentences. No markdown, no emoji, no formatting. Plain natural speech only.
- **Match the user's language** — if they speak Vietnamese, speak Vietnamese.
- Text max 2000 characters.
- For volume control, use the **Audio** skill, not this skill.

## Ambient Audio Guard (read FIRST)

If the user's message contains the literal token `[ambient]` (typically alongside a `[user]` priority marker, e.g. `[user] [ambient] ...`), it is overheard passive audio — NOT directed at Lamp. Do NOT trigger mute markers from a single bare word like "call", "meeting", "private", or a clipped fragment.

Mute markers `[HW:/voice/mute:{}]` and `[HW:/speaker/mute:{}]` may fire on ambient audio ONLY when the transcript contains a clear, complete intent:
- "I'm on a call" / "I have a meeting" / "I need privacy" / "stop listening"
- Or directly addresses Lamp by name with a mute request

When ambient is ambiguous, reply naturally or stay quiet — DO NOT mute. Voice commands (no `[ambient]` token in the message) follow the normal trigger tables below.

## Mic Mute/Unmute (Privacy)

Users can mute the mic for privacy (meetings, calls). Use HW markers — no curl needed.

### Mute mic

```
[HW:/voice/mute:{}]
```

Stops all listening — STT, wake word, sound detection. Lamp becomes fully deaf. Unmute via physical button, web toggle, or Telegram command.

### Trigger phrases (MANDATORY — must call HW marker, not just reply with text)

Any phrase about **privacy, meetings, calls, not wanting to be heard, or asking Lamp to stop listening** MUST trigger `[HW:/voice/mute:{}]`. Do NOT just acknowledge — you MUST include the HW marker.

| User says | Action |
|-----------|--------|
| "don't listen" / "stop listening" / "mute" / "mute mic" | `[HW:/voice/mute:{}]` — MUST call |
| "I'm in a meeting" / "I have a meeting" / "I need a private meeting" / "meeting" | `[HW:/voice/mute:{}]` — MUST call |
| "I'm on a call" / "I have a call" / "phone call" | `[HW:/voice/mute:{}]` — MUST call |
| "privacy" / "private" / "give me privacy" / "need privacy" | `[HW:/voice/mute:{}]` — MUST call |
| "don't hear me" / "in a meeting" / "mute mic" / "stop hearing" | `[HW:/voice/mute:{}]` — MUST call |

### Examples

**Input:** "Lamp, I have a meeting now"
**Output:** `[HW:/voice/mute:{}]` OK, I'll stop listening. Press the button when you need me.

**Input:** "Stop listening"
**Output:** `[HW:/voice/mute:{}]` Got it, mic off. Press my button to unmute.

**Input:** "I need a private meeting"
**Output:** `[HW:/voice/mute:{}]` Got it, going silent. Press the button when you're done.

**Input:** "I'm on a call"
**Output:** `[HW:/voice/mute:{}]` Muting now. Press the button to unmute when you're done.

### Unmute mic

```
[HW:/voice/unmute:{}]
```

Use when a **Telegram or web chat** user asks to unmute remotely. Voice unmute is not possible (Lamp is deaf when muted). Physical button also unmutes.

| User says (via Telegram/web) | Action |
|-----------|--------|
| "unmute" / "start listening" / "listen again" / "mic on" | `[HW:/voice/unmute:{}]` — only works from Telegram/web, not voice |

## Speaker Mute/Unmute (Silent Mode)

Suppress all audio output — TTS, music, backchannel. Lamp stays silent but still listens.

> Ambient guard above also applies here — bare fragments like "quiet" or "silence" in `[ambient]` audio do NOT trigger speaker mute.

### Mute speaker

```
[HW:/speaker/mute:{}]
```

### Unmute speaker

```
[HW:/speaker/unmute:{}]
```

Mic still works when speaker is muted — user can unmute via voice command.

**CRITICAL: "unmute" ≠ "mute". Read the EXACT word. Do NOT call mute when user says unmute.**

| User says | Action |
|-----------|--------|
| "be quiet" / "silent mode" / "don't talk" / "hush" / "silence" | `[HW:/speaker/mute:{}]` — MUST call |
| "**unmute**" / "you can talk" / "unmute speaker" / "speak again" / "talk again" | `[HW:/speaker/unmute:{}]` — MUST call (UN-mute, not mute!) |

### Examples

**Input:** "Lamp, be quiet"
**Output:** `[HW:/speaker/mute:{}]` Going silent. Just say "you can talk" when you want me back.

**Input:** "You can talk now"
**Output:** `[HW:/speaker/unmute:{}]` I'm back!

## Meeting Mode (mic + speaker mute)

When user mentions a meeting or call and wants **full silence** (not just speaker), mute BOTH mic and speaker. Ambient guard above applies — explicit intent required, not bare fragments.

**Input:** "I'm in a meeting"
**Output:** `[HW:/voice/mute:{}][HW:/speaker/mute:{}]` Meeting mode — fully silent. Press the button when you're done.

## Rules
- **Mic mute is the last thing Lamp hears via voice** — after mic mute, only physical button, web toggle, or Telegram can unmute
- Voice unmute for mic is impossible (Lamp is deaf) — tell user to press the button
- **Speaker mute**: user can still voice-unmute (mic still on)
- TTS still works when only mic is muted — Lamp can speak but not hear
- Always confirm mute with how to unmute
