---
name: speaker-recognizer
description: Self-enroll voices after a clear self-introduction or explicit enrollment request; continue that enrollment, or list, forget, link, and identify voices when requested. Unknown Speaker labels, saved audio, and same-tag history alone do not activate this skill. Handle ordinary requests without identity lookup or a name question.
---

# Speaker Recognizer

Each mic transcript is prefixed `Speaker - Name:` when recognized, or `Unknown Speaker: [voice:voice_N] ... (audio save[d] at <path>...)` otherwise. The audio path is the WAV of whoever spoke this turn — use it (with paths from prior same-tag turns when needed) to enroll on `POST /speaker/enroll`.

**Self-enrollment only** — never enroll one person's voice under another person's name.

## Entry gate

Unknown identity is metadata, not an enrollment request. Without a clear self-introduction, an explicit voice enrollment/management request, or a reply continuing a user-initiated enrollment, handle the actual request directly: no speaker API calls, no enrollment reference reads, no name question, and no replacement acknowledgment. Meaningless fragments follow the device's normal silence rules. Same-tag history alone does not start or resume enrollment; a new unrelated request takes priority.

A name counts only when the speaker identifies themselves, not when addressing the agent ("Mike, open Chrome"), mentioning another person, or appearing in metadata. Preserve any substantive request accompanying a self-introduction; enrollment must not replace it.

## Decision matrix — after the entry gate

| Signals in current turn | Prior same-tag turns? | Action |
|---|---|---|
| `Unknown Speaker:` + path + name + ≥25 words | — | **Enroll now** with current path only. |
| `Unknown Speaker:` + path + name + <25 words | ≥1 prior path same `[voice:N]` | **Enroll now** with all same-tag paths (oldest→newest). |
| `Unknown Speaker:` + path + name + <25 words | none | Ask one follow-up: "say your name + ~25–30 words". |
| Explicit enrollment request + path + NO name | any | Ask for the speaker's own name; request more audio only if needed. |
| Reply continuing enrollment, still no name or insufficient audio | same enrollment | Explain what is missing once; do not loop or enroll under a guessed name. |
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
- **No unsolicited identity questions** — ask only within user-initiated enrollment; combine missing-name and missing-audio guidance in one question. Do not repeatedly interrupt other requests to finish enrollment.
- **Confirm every enroll** AFTER the API returns ok.
- **Don't narrate technical details** — no "base64", "ffmpeg", "POST /speaker/enroll".
- **Never write files directly** — always use the HTTP API.
