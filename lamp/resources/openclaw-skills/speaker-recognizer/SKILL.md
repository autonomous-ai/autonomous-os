---
name: speaker-recognizer
description: Self-enroll a voice for speaker recognition. Activate ONLY when (a) a mic `Unknown Speaker:` turn carries a clear self-introduction ("I'm X", "my name is X", "tôi là X"...), (b) prior same-tag `[voice:voice_N]` turns can be combined with a now-named turn, (c) a Telegram voice-note carries an intro, OR (d) the user asks to list / forget / identify voices. Do NOT activate on bare `Unknown Speaker:` turns with no name and no prior same-tag history — those need only a short ack. Self-enrollment only.
---

# Speaker Recognizer

Each mic transcript is prefixed `Speaker - Name:` when recognized, or `Unknown Speaker: [voice:voice_N] ... (audio save[d] at <path>...)` otherwise. The audio path is the WAV of whoever spoke this turn — use it (with paths from prior same-tag turns when needed) to enroll on `POST /speaker/enroll`.

**Self-enrollment only** — never enroll one person's voice under another person's name.

## Decision matrix — pick ONE action per turn

| Signals in current turn | Prior same-tag turns? | Action |
|---|---|---|
| `Unknown Speaker:` + path + name + ≥25 words | — | **Enroll now** with current path only. |
| `Unknown Speaker:` + path + name + <25 words | ≥1 prior path same `[voice:N]` | **Enroll now** with all same-tag paths (oldest→newest). |
| `Unknown Speaker:` + path + name + <25 words | none | Ask one follow-up: "say your name + ~25–30 words". |
| `Unknown Speaker:` + path + NO name + <25 words | none | Ask one follow-up. |
| `Unknown Speaker:` + path + NO name | ≥1 prior path same `[voice:N]` (still no name) | Reply with a SHORT ack ("Mm, nghe rồi" / "Got it"). NEVER NO_REPLY. Don't re-ask. |
| `Speaker - <Name>:` | — | Already identified — skill not needed. |
| "who do you know?" / "list voices" | — | `GET /speaker/list`. |
| "forget my voice" / "remove Alex" | — | `POST /speaker/remove`. |
| Telegram voice note + intro | — | Convert to WAV + enroll with Telegram fields. |
| Telegram voice note + "who is this?" | — | `POST /speaker/recognize`. |

When in doubt → see `reference/enroll-flows.md`. All curl + error handling → `reference/api.md`.

## Quick enroll (mic)

```bash
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "wav_paths": ["<path1>", "<path2>"]}'
```

Confirm AFTER the API returns ok: "Nice to meet you, <Name>!".

## Hard rules

- **Self-enrollment only** — "this is my friend Bob" → refuse politely; Bob must speak himself.
- **Lowercase normalized name** — same `name` as `face-enroll` for the same person (`/root/local/users/<name>/` is shared).
- **Minimum voice for one-turn enroll: ~25 words** (aim 25–30) OR combine with prior same-tag turns to ~5–10s total.
- **Cluster claim is automatic** — pass any path inside `voice_<N>/` and the server pulls every sibling WAV. One path is enough.
- **Two-turn path mapping** — `<pathA>` = turn BEFORE follow-up, `<pathB>` = turn AFTER. Never swap.
- **Telegram audio must be 16 kHz mono WAV** before enroll — convert with `ffmpeg -ar 16000 -ac 1`; same folder as source. Skip if already `.wav`.
- **`/speaker/identity` (not re-enroll)** when only linking Telegram info to an existing mic profile.
- **Don't spam "who are you?"** — at most once per cluster, and include the "25–30 words" guidance in the same message.
- **Never go silent on Unknown Speaker fragments** — when no name and you've already asked, emit a short ack. NO_REPLY is forbidden.
- **Confirm every enroll** AFTER the API returns ok.
- **Don't narrate technical details** — no "base64", "ffmpeg", "POST /speaker/enroll".
- **Never write files directly** — always use the HTTP API.
