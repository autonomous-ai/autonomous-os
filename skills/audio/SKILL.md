---
name: audio
description: Low-level speaker and microphone hardware control — adjust volume, play test tones, record raw audio. Do NOT use for TTS/speech (that is the Voice skill).
---

# Audio Control

## Quick Start
Control the device's speaker and microphone hardware directly. Use this for volume adjustments, test tones, and raw audio recording. This is LOW-LEVEL hardware control only.

## Workflow
1. Determine the user's audio hardware need:
   - Volume adjustment -> use `POST /audio/volume`
   - Check current volume -> use `GET /audio/volume`
   - Diagnostics / test -> use `POST /audio/play-tone`
   - Raw recording -> use `POST /audio/record`
2. Optionally check device availability first: `GET /audio`
3. Execute the appropriate API call
4. Confirm the action to the user

## Examples

Input: "Louder please" / "Turn it up"
Output: Check current volume with `GET /audio/volume`, then increase by ~15 with `POST /audio/volume`. Confirm: "Volume set to 85%."

Input: "Set volume to 50%"
Output: Call `POST /audio/volume` with `{"volume": 50}`. Confirm: "Volume set to 50%."

Input: "Mute" / "Too loud"
Output: Call `POST /audio/volume` with `{"volume": 0}`. Confirm: "Muted."

Input: "I can't hear you"
Output: Check current volume with `GET /audio/volume`, then increase it. Confirm with the new level.

Input: "Say something" / "Tell me a joke"
Output: Do NOT use this skill. Just reply normally — your voice pipeline handles TTS automatically.

## Tools

Use `Bash` with `curl` to call the HTTP API at `http://127.0.0.1:5001`.

### Check audio devices
```bash
curl -s http://127.0.0.1:5001/audio
```
Response:
```json
{
  "output_device": 0,
  "input_device": 1,
  "available": true
}
```

### Set volume
```bash
curl -s -X POST http://127.0.0.1:5001/audio/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 70}'
```
Volume range: 0 (mute) to 100 (max).

### Get current volume
```bash
curl -s http://127.0.0.1:5001/audio/volume
```
Response: `{"control": "Speaker", "volume": 70}`

### Play test tone
```bash
curl -s -X POST "http://127.0.0.1:5001/audio/play-tone?frequency=440&duration_ms=500"
```
Plays a sine wave. Use for audio testing only. Keep it short (< 1 second).

### Record audio
```bash
curl -s -X POST "http://127.0.0.1:5001/audio/record?duration_ms=3000"
```
Records from the microphone and returns a WAV file.

## Error Handling
- If `GET /audio` returns `"available": false`, inform the user: "The speaker/microphone is not connected right now."
- If the API is unreachable, inform the user: "I couldn't access the audio hardware. The service may be unavailable."
- If the user requests a volume outside 0-100, clamp to the valid range.

## Rules
- Default volume is usually 70%. Adjust based on user preference.
- **Audio = volume knob, raw mic recording, test beeps.** No AI speech processing.
- **Voice = AI-powered speech (TTS/STT).** Uses Audio hardware underneath but is a separate skill.
- When the user says "I can't hear you" or "too loud", adjust volume via this skill.
- Do NOT use this skill for TTS or speech output — that is handled by the **Voice** skill and the automatic voice pipeline.

## Output Template
```
[Audio] {action} — {details}
```
Examples:
- `[Audio] Volume set — 70%`
- `[Audio] Volume set — muted (0%)`
- `[Audio] Test tone played — 440Hz, 500ms`
- `[Audio] Recording captured — 3000ms`
