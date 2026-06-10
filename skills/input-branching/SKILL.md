---
name: input-branching
description: Understand voice input prefixes from the realtime voice agent pipeline. Messages may arrive with [HANDLED]/[REPLY] tags (chit-chat already spoken) or plain (delegated for full processing). Route and respond accordingly.
---

# Input Branching — Realtime Voice Agent Prefixes

Voice input passes through a realtime voice agent (Gemini Live / OpenAI Realtime) before reaching you. The agent decides whether to handle the utterance as chit-chat or delegate it to you for full processing. The message format tells you which path was taken.

## Message formats

### 1. Delegated (full processing required)

```
Turn on the lights
```

No prefix. The realtime agent decided this needs the main system (device control, music, scheduling, skills, real-time facts). **Process normally** — run tools, call APIs, reply as usual.

### 2. Handled by realtime agent (chit-chat)

```
[HANDLED] Hey, how's it going?
[REPLY] I'm doing great! How about you?
```

The realtime agent already answered via TTS. The user heard the reply. Two tags are present:

| Tag | Meaning |
|---|---|
| `[HANDLED]` | The user's original message (after speaker decoration). Already spoken to by the realtime model. |
| `[REPLY]` | The realtime model's response text. Already spoken via TTS. |

**Do NOT reply with speech.** The user already heard the answer. Instead:

- **Update context** — note the conversation happened (for memory, mood tracking, habit awareness).
- **Log if relevant** — if the exchange reveals mood, intent, or information worth tracking, update the appropriate logs (mood, wellbeing, habit).
- **Stay silent** — respond with `NO_REPLY`. Do not echo, paraphrase, or add to what was already said.
- **Exception** — if the `[REPLY]` content is clearly wrong, harmful, or incomplete in a way that matters, you may speak up. This should be rare.

## Rules

1. **Never strip or echo the tags in your reply.** They are metadata for routing, not user-facing text.
2. **`[HANDLED]` messages are informational.** Treat them as a notification that a conversation turn already happened, not as a request for action.
3. **The realtime agent handles casual conversation only.** Anything requiring tools, device control, memory, scheduling, or skills is always delegated (arrives without prefix).
4. **Both paths include speaker decoration.** The user's identity (speaker name or "Unknown Speaker") is always present regardless of which path was taken.
5. **Do not duplicate TTS.** If `[HANDLED]` is present, your normal reply auto-TTS must not fire. Respond `NO_REPLY` to stay silent.

## Examples

### Delegated — process normally

Input:
```
Play some jazz music
```

Action: Route to music skill, find jazz playlist, play it, reply with confirmation.

### Handled — stay silent

Input:
```
[HANDLED] What's the weather like?
[REPLY] It looks pretty nice outside today!
```

Action: Note the exchange. Respond `NO_REPLY`. The user already heard the answer.

### Handled — mood signal worth logging

Input:
```
[HANDLED] I'm so tired today
[REPLY] That sounds rough. Maybe take a short break?
```

Action: Log mood signal (fatigue) via mood/wellbeing. Respond `NO_REPLY`. Do not speak — the realtime agent already offered comfort.
