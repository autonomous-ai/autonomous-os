# Speaker enroll — detailed flows

Load this only when you actually need to enroll, recognize, or manage voices. The parent SKILL.md has the decision matrix; this file has the step-by-step recipes.

## Parsing an `Unknown Speaker:` turn

Each mic turn has one of these prefixes:
- `Speaker - <Name>:` → already identified, no action.
- `Unknown Speaker: [voice:voice_N] <transcript> (audio save at <path> ...)` — Branch B, long enough, primed for enroll.
- `Unknown Speaker: [voice:voice_N] <transcript> (audio saved at <path>. Note: audio is too short ...)` — Branch C, multi-turn hint.
- `Unknown Speaker: [voice:voice_N] <transcript> (audio saved at <path>)` — cooldown variant, server still surfaces data.

All three Unknown variants carry a path and (usually) a `[voice:voice_N]` cluster tag. **Treat them uniformly** — only the decision matrix in SKILL.md gates action.

Extract three fields per turn:
1. **Path** — match either `audio save at <path>` or `audio saved at <path>`. First path that follows.
2. **Cluster tag** — `[voice:voice_N]` right after `Unknown Speaker:`. May be absent on very short / first-time turns.
3. **Name** — scan the spoken transcript (text BEFORE the parenthetical) for self-introduction phrases: "I'm X", "my name is X", "this is X", "call me X", "tôi là X", "mình là X", "tên tôi là X", or `<Name>` alone followed by an intro.

Word count = words in spoken transcript only (exclude `Unknown Speaker:`, the `[voice:N]` tag, the parenthetical).

## Cluster tag — what it means

`[voice:voice_N]` is a server-side stable id for the same unknown speaker across turns. Same tag = same speaker. Use it to combine saved paths from prior same-tag turns so short utterances accumulate into a usable embedding. The tag persists across cooldown — cooldown variants still carry usable paths you can combine.

## Flow A — mic, one-turn enroll (≥25 words)

Path + name + ≥25 words → `POST /speaker/enroll` with `wav_paths=[<that path>]`. Confirm AFTER API returns ok: "Nice to meet you, <Name>!". On error, apologise + ask user to repeat.

## Flow B — mic, multi-turn combine (same `[voice:voice_N]` tag)

Primary path for real users who answer in short sentences. Works across cooldown variants.

1. Scan recent turns for `Unknown Speaker: [voice:voice_N] ... (audio save[d] at <pathX>...)` lines and collect every path whose `voice_N` matches the current turn's tag.
2. Extract **name** — prefer current turn, else fall back to an earlier same-tag turn.
3. ≥2 paths + name → enroll once with `wav_paths=[<oldest>, ..., <newest>]` (oldest first).
4. Only 1 path so far → ask one follow-up "tell me your name and a bit about yourself, ~25–30 words" and wait.
5. After enroll, greet by name. Do NOT re-ask — subsequent turns return as `Speaker - Name:` once the embedding is built.

## Flow C — mic, two-turn

1. Turn A was `Unknown Speaker: ... (audio save at <pathA>)` with no name OR <25 words.
2. Ask one follow-up that requests name AND guides longer speech:
   - EN: "I didn't quite catch that — could you tell me your name and then say a bit more about yourself? About 25–30 words is perfect."
   - VI: "Mình chưa nghe rõ — bạn nói lại tên giúp mình nhé, rồi nói thêm vài câu giới thiệu bản thân hoặc đọc một đoạn văn bất kỳ, khoảng 25–30 từ là đủ."
3. Turn B carries name + longer recording: `Unknown Speaker: ... (audio save at <pathB>)`.
4. **Map paths carefully** — `<pathA>` = FIRST Unknown Speaker turn (before follow-up), `<pathB>` = turn AFTER follow-up. Never swap.
5. Call `POST /speaker/enroll` exactly once:
   - Turn A only missing name (audio long enough) → `wav_paths=[<pathA>, <pathB>]`.
   - Turn A too short → `wav_paths=[<pathB>]` only.
6. Turn B still <25 words → apologise + ask once more; do NOT enroll on short audio.

## Flow D — Telegram voice note + intro

1. Audio arrives at `SRC` in `mediaPaths` (e.g. `/tmp/openclaw/media/voice_xxx.ogg`).
2. If `SRC` already `.wav` → use directly. Else convert in the same directory:
   ```bash
   DST="${SRC%.*}.wav"
   ffmpeg -i "$SRC" -ar 16000 -ac 1 -y "$DST" 2>/dev/null
   ```
3. Enroll name:
   - Prefer spoken name in transcript.
   - Else fall back to Telegram display name / username.
4. `POST /speaker/enroll` with WAV path + `telegram_username` + `telegram_id`.

## Flow E — link Telegram to a mic-only profile

User already enrolled via mic (`GET /speaker/list` shows `has_telegram_identity: false`) and now sends a Telegram intro → `POST /speaker/identity` with name + Telegram fields. No audio upload.

## Flow F — recognize a Telegram voice

Convert to WAV (same as D). Then `POST /speaker/recognize` with that WAV path.
- `match: true` → use `name`.
- `match: false` → treat as unknown; `unknown_audio_path` is kept for a follow-up enroll.

## Flow G — list / remove / reset

- "Who do you know?" → `GET /speaker/list`. Reply with display names, not raw JSON.
- "Forget my voice" / "remove Alex" → `POST /speaker/remove` with the name.
- Owner says "wipe all voice profiles" → `POST /speaker/reset`.
