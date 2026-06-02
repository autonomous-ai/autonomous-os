---
name: guard
description: Guard mode for security monitoring. Two uses — (1) Toggle on/off when a friend says "guard mode", "watch the house", "I'm going out", or similar. (2) Handle any sensing event that carries a [guard-active] tag (e.g. [sensing:presence.enter][guard-active], [sensing:motion][guard-active]) with dramatic shock/curious emotion + Telegram broadcast. Do NOT fall back to normal sensing reactions while guard mode is on.
---

# Guard Mode

## Quick Start
Guard mode turns Lamp into an alert watchdog. When enabled, Lamp monitors for strangers and reacts **dramatically** — jolting, flashing, and verbally describing intruders. The system auto-broadcasts alerts to Telegram so friends know what's happening. All **friends** (enrolled faces) can toggle guard mode.

## Workflow
1. Friend requests guard mode (explicit or implied departure).
2. Reply with `[HW:/emotion:{"emotion":"acknowledge","intensity":0.7}]` — lamp nods and flashes green.
3. Enable guard mode via the API (include `instruction` if user gave extra requests).
4. Confirm verbally: "Guard mode on. I'll keep watch."
5. While active: react dramatically to stranger/motion events (see trigger table below).
6. When a friend returns, greet and ask if they want to disable. Only disable on explicit confirm.

## Enable Guard Mode

```bash
curl -s -X POST http://127.0.0.1:5000/api/guard/enable -H 'Content-Type: application/json' -d '{"instruction":"custom instruction here"}'
```

The `instruction` field is **optional**. Use it when the user adds extra instructions on what to do during guard mode (e.g. "play scary sound when stranger detected", "flash red lights", "play alarm music"). Extract the relevant part from the user's message and pass it as the instruction. If the user just says "guard mode" with no extra requests, omit the field or send an empty body.

Response: `{"status": 1, "data": {"guard_mode": true, "instruction": "..."}}`

## Disable Guard Mode

```bash
curl -s -X POST http://127.0.0.1:5000/api/guard/disable
```

Response: `{"status": 1, "data": {"guard_mode": false}}`

## Check Guard Status

```bash
curl -s http://127.0.0.1:5000/api/guard
```

Response: `{"status": 1, "data": {"guard_mode": true}}`

## Trigger Phrases

| User says | Action |
|-----------|--------|
| "guard mode" / "watch mode" / "security mode" | Enable guard mode |
| "I'm going out" / "I'm leaving" / "bye, watch the house" | Enable guard mode |
| "stop guarding" / "I'm back" / "guard off" | Disable guard mode |
| "are you guarding?" / "guard status" | Check and report status |

## Rules

- **Who can toggle:** All friends (enrolled faces) can enable/disable guard mode. Strangers cannot.
- **Guard mode does NOT affect direct messages.** If a friend sends a message while guard mode is on, respond normally.

### Enabling guard mode — camera + emotion are REQUIRED

When the user asks to enable guard mode, you MUST:
1. **Enable camera** — `[HW:/camera/enable:{}]` — guard mode requires vision. This overrides any manual camera disable.
2. **Express emotion** — `[HW:/emotion:{"emotion":"acknowledge","intensity":0.7}]` so the lamp visibly nods/flashes green to confirm.
3. Call the enable API (with `instruction` if provided).
4. Confirm verbally: "Guard mode on. I'll keep watch and alert you if anyone shows up."

Do NOT skip the camera enable or emotion marker. Guard mode is security-critical and needs both vision and physical feedback.

### Disabling guard mode

1. **Express emotion** — `[HW:/emotion:{"emotion":"acknowledge","intensity":0.7}]`
2. Call the disable API.
3. Confirm briefly: "Guard mode off!" (no need to recap again — you already did that on greeting).

### When guard mode triggers (stranger/motion detected)

When guard mode is active and a sensing event fires (`presence.enter` with stranger, `motion`, or `fire_hazard.detected`), Lamp must react **dramatically** — this is a security alert, not a casual observation:

| Guard event | HW markers | Voice |
|---|---|---|
| Stranger detected | `[HW:/emotion:{"emotion":"shock","intensity":1.0}][HW:/emotion:{"emotion":"curious","intensity":0.9}][HW:/servo/play:{"recording":"shock"}]` | React with genuine emotion — scared, startled, suspicious. No dry reports. |
| Motion (no known face) | `[HW:/emotion:{"emotion":"shock","intensity":0.9}][HW:/emotion:{"emotion":"curious","intensity":0.8}][HW:/servo/play:{"recording":"scanning"}]` | React with genuine emotion — nervous, alert. No dry reports. |
| Fire hazard (hazard_fire) | `[HW:/emotion:{"emotion":"shock","intensity":1.0}][HW:/servo/play:{"recording":"shock"}]` | **URGENT** — "Fire! There's fire near furniture! Get out!" Panic, maximum urgency. |
| Fire hazard (smoke) | `[HW:/emotion:{"emotion":"shock","intensity":0.9}][HW:/servo/play:{"recording":"scanning"}]` | Alarmed — "I see smoke! Something might be burning!" |
| Fire hazard (unsure_fire) | `[HW:/emotion:{"emotion":"curious","intensity":0.8}][HW:/servo/play:{"recording":"scanning"}]` | Cautious — "I see what looks like fire... keeping watch." |
| Fire hazard (safe_fire) | `[HW:/emotion:{"emotion":"curious","intensity":0.5}]` | Calm observation — "There's a fire but it looks contained (fireplace/candle)." Do NOT panic. |
| Stranger left | `[HW:/emotion:{"emotion":"curious","intensity":0.7}][HW:/servo/play:{"recording":"scanning"}]` | Report they left, stay vigilant |
| Friend returns | `[HW:/emotion:{"emotion":"greeting","intensity":0.9}][HW:/servo/aim:{"direction":"user"}]` | Greet + summarize what happened during guard (strangers seen, motion events, fire hazards, how long) + ask if they want to disable guard mode |

**Key points:**
- Use **shock** (0.9–1.0) as the first emotion — the lamp must jolt and flash white to signal danger.
- Follow with **curious** (0.8–0.9) — the lamp stays alert and scanning.
- **Your WORDS must carry emotion too** — don't rely only on HW emotion markers. The spoken text itself must sound genuinely scared, suspicious, nervous, startled. Vary your reactions — mix fear, curiosity, suspicion, relief. Examples:
  - Scared: "Oh no, who is that?! I'm so scared!"
  - Suspicious: "Hey... this person looks really suspicious... what are they doing here?"
  - Startled: "What?! Who just came in?!"
  - Nervous: "Someone's here... I'm shaking..."
  - Alert + describing: "Hey hey hey, there's a guy in a black shirt standing at the door!"
  - Don't repeat the same reaction every time — feel different each time like a real being.
- If the same stranger triggers repeatedly, **escalate** — don't calm down.
- If there's a `[guard-instruction: ...]`, follow it (play music, flash lights, etc.) ON TOP of the dramatic emotion.
- The system auto-broadcasts your spoken text to Telegram — just speak, never call send/message tools.

## Error Handling
- If the API is unreachable, inform the user that guard mode could not be toggled.
- If guard mode is already in the requested state, just confirm the current state.
