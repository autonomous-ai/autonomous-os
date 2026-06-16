---
name: input-branching
description: Route voice input from the realtime voice agent pipeline based on message tags.
---

# Input Branching

Voice input passes through a realtime voice agent before reaching you. The agent handles chit-chat directly and delegates everything else. Message tags tell you which path was taken.

## Message Formats

### Delegated with instruction

```
[voice-instruction] Play jazz music on Spotify
[transcript] play some jazz please on spotify
```

- `[voice-instruction]` — the realtime agent's clean summary of what the user wants. **Use this as the primary input.**
- `[transcript]` — raw STT transcript. Often inaccurate — STT is locked to one language while the user may speak another. Treat as noisy supplementary context only, never as the source of truth.

### Delegated without instruction (fallback)

```
turn on the lights
```

No tags. Process as a normal voice event.

### Handled (history entry)

```
[HANDLED] Hey, how's it going?
[REPLY] I'm doing great! How about you?
```

- `[HANDLED]` — the user's original message, already answered via TTS.
- `[REPLY]` — what the realtime agent said. The user already heard it.

This is a **history entry only**. The conversation already happened. You are being notified, not asked to respond.

## Rules

1. **`[voice-instruction]` is the primary input.** When present, use it over `[transcript]`.
2. **`[HANDLED]` → always `NO_REPLY`.** No exceptions. Even if the reply seems wrong or incomplete — the user already heard it. Do not correct, echo, paraphrase, or add to it.
3. **Log context from `[HANDLED]` silently.** If the exchange reveals mood, intent, or information worth tracking (fatigue, stress, preferences), update memory/mood/wellbeing.
4. **Never echo tags.** `[voice-instruction]`, `[transcript]`, `[HANDLED]`, `[REPLY]` are routing metadata, not user-facing text.
5. **No prefix = normal voice event.** Process the message as-is.

## Examples

**Delegated — use instruction, ignore noisy transcript:**
```
[voice-instruction] Set brightness to 50%
[transcript] set the brightness to like half
```
→ Process "Set brightness to 50%". Route to LED skill.

**Delegated — transcript in wrong language (user spoke Vietnamese, STT is English):**
```
[voice-instruction] Turn off the light and play some relaxing music
[transcript] ton of delay and play some relate music
```
→ Process the `[voice-instruction]`. The transcript is gibberish because STT tried to transcribe Vietnamese as English.

**Delegated — transcript garbled by accent/noise:**
```
[voice-instruction] What is the weather like tomorrow
[transcript] what is the wetter like to morrow
```
→ Process the `[voice-instruction]`. The transcript has misheard words.

**Delegated without instruction:**
```
play some jazz music
```
→ Process as-is. Route to music skill.

**Handled — silent:**
```
[HANDLED] What time is it?
[REPLY] It's 3:15 PM.
```
→ `NO_REPLY`.

**Handled — log mood:**
```
[HANDLED] I'm so tired today
[REPLY] That sounds rough. Maybe take a short break?
```
→ Log fatigue via mood/wellbeing. `NO_REPLY`.
