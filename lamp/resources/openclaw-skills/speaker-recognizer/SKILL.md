---
name: speaker-recognizer
description: Self-enroll voices for speaker recognition. Triggered when a mic transcript arrives prefixed with "Unknown Speaker:" and the user is introducing themselves, or when a Telegram voice-note attachment carries an introduction. Telegram identity is saved for DM targeting. Self-enrollment only.
---

# Speaker Recognizer

> **MANDATORY ENROLL RULE — ANY of these is a trigger to enroll:**
> 1. Message contains `Unknown Speaker:` AND an audio path (`audio save at <path>` OR `audio saved at <path>`).
> 2. Transcript carries a self-introduced name ("I'm X", "my name is X", "this is X", "call me X", "tôi là X", "mình là X", "tên tôi là X", or just `<Name>` followed by an intro).
> 3. The transcript alone is ≥ 25 words **OR** prior conversation turns share the same `[voice:voice_N]` tag whose paths can be combined to reach roughly 5–10s of speech.
>
> When all 3 hold → `POST /speaker/enroll` with the collected `wav_paths` IMMEDIATELY. Do not just greet — confirm AFTER the API call returns ok.
>
> When 1+2 hold but 3 fails (too short and no prior same-cluster turns) → ask one follow-up requesting the user to speak 25–30 words.

> **MESSAGE VARIANTS (3 forms produced by lelamp — all 3 are actionable):**
> - **Branch B (long, primed for enroll):** `Unknown Speaker: [voice:voice_N] <transcript> (audio save at <path>, auto enroll this speaker if having speaker name in transcript, else ask user's name)`
> - **Branch C (short, multi-turn hint):** `Unknown Speaker: [voice:voice_N] <transcript> (audio saved at <path>. Note: audio is too short for single enrollment. If prior turns tagged the same voice_N, combine their saved paths with this one when enrolling; otherwise ask the user to introduce themselves longer.)`
> - **Cooldown variant (server is debouncing the strong instruction but still surfaces data):** `Unknown Speaker: [voice:voice_N] <transcript> (audio saved at <path>)`
>
> All 3 variants carry a `[voice:voice_N]` tag (when assignable) and a path. **Treat the path the same way regardless of variant** — if name + length conditions are met, enroll.

> **VOICE CLUSTER TAG `[voice:voice_N]`** — server-side stable id for the same unknown speaker across turns. Same tag = same speaker. Use it to **combine saved paths from prior same-tag turns** so short utterances accumulate into a usable embedding without forcing the user to speak 25 words in one go. The tag persists across the cooldown window — even when the server stops nudging, prior cooldown-variant turns still carry usable paths you can combine.

## Quick Start
Each mic transcript is prefixed `Speaker - Name:` when recognized, or `Unknown Speaker: [voice:voice_N] ... (audio path...)` otherwise. The audio path is the WAV of whoever spoke this turn — use it (with paths from prior same-tag turns when needed) to enroll on `POST /speaker/enroll`.

**Self-enrollment only** — never enroll one person's voice under another person's name. The audio path contains whoever spoke in that turn.

## Trigger — WHEN to activate this skill

Activate when ANY of these fire (no exact wording match needed — look for the pattern):

- **Mic, single-turn intro (≥25 words):** transcript has `Unknown Speaker:` + a path + a self-introduction + ≥25 spoken words. → enroll with that one path.
- **Mic, multi-turn combine (same `[voice:voice_N]` tag):** the current turn or any recent turn finally has a clear name AND there are ≥2 paths in conversation history sharing the same `[voice:voice_N]` tag. → enroll with all collected same-tag paths. **This is the primary path for real users who answer in short sentences.** It works ACROSS cooldown variants — cooldown turns still carry usable paths.
- **Mic, two-turn:** previous turn was `Unknown Speaker:` with a path but no clear name (or too short) → ask one follow-up requesting "name + 25–30 words" → next turn has the longer recording → enroll combining both paths (or just the longer one if the first was unusable).
- **Telegram voice note + intro:** message carries `[mediaPaths: .../xxx.ogg|.wav|.m4a|.mp3|.opus]` AND the user is introducing themselves.
- **User asks about registered voices:** "who do you know?" / "list voices" / "do you remember my voice?".
- **User asks to forget their voice:** "forget my voice" / "remove Alex".
- **Telegram voice note + "who is this?"**: user wants identification of an audio.

Do NOT activate when:
- Transcript prefix is `Speaker - <Name>:` (already identified — no action).
- User tries to enroll someone else ("this is my friend Bob") — refuse politely.
- `mediaPaths` points at a photo — that's the `face-enroll` skill.

## Decision matrix — pick ONE action per turn

| Signals in current turn | Prior same-tag turns? | Action |
|---|---|---|
| `Unknown Speaker:` + path + name + ≥25 words | — | **Enroll now** with current path only. |
| `Unknown Speaker:` + path + name + <25 words | ≥1 prior path with same `[voice:N]` | **Enroll now** with all same-tag paths combined. |
| `Unknown Speaker:` + path + name + <25 words | none | Ask one follow-up: "say your name + ~25–30 words". |
| `Unknown Speaker:` + path + NO name + <25 words | none | Ask one follow-up. |
| `Unknown Speaker:` + path + NO name | ≥1 prior path with same `[voice:N]` (still no name) | **Reply with a SHORT acknowledgment** (e.g. "Mm, mình nghe rồi" / "Nghe nè" / "Got it"). NEVER NO_REPLY, NEVER silent. Just don't re-ask "who are you?" — wait for them to volunteer a name. |
| `Speaker - <Name>:` | — | Already identified — proceed normally. |

## Workflow

### Parse a transcript turn

For every `Unknown Speaker:` turn, extract three fields:

1. **Path** — match either `audio save at <path>` or `audio saved at <path>`. Take the first path that follows. The path is always inside the `Unknown Speaker:` parenthetical.
2. **Cluster tag** — `[voice:voice_N]` right after `Unknown Speaker:`. May be absent on very short or first-time turns.
3. **Name** — scan the spoken transcript (text BEFORE the parenthetical) for self-introduction phrases.

Then count words in the spoken transcript only (exclude `Unknown Speaker:`, the `[voice:N]` tag, and the parenthetical). That word count is what gates one-turn enroll.

### Enroll a voice (mic, one-turn — ≥25 words)
1. Path + name + ≥25 words detected → `POST /speaker/enroll` with `wav_paths=[<that path>]`. No Telegram fields (origin auto = `"mic"`).
2. Confirm AFTER the API returns ok: "Nice to meet you, <Name>!". If the API errors, apologise and ask the user to repeat.

### Enroll a voice (mic, multi-turn combine — same `[voice:voice_N]` tag)
**Primary path** for real users who answer in short sentences. Works across cooldown variants — cooldown turns still carry usable paths.

1. Scan the last few turns of conversation for `Unknown Speaker: [voice:voice_N] ... (audio save[d] at <pathX>...)` lines and collect every path whose `voice_N` tag matches the current turn's tag.
2. Extract the **name**. Prefer the current turn. If absent, fall back to an earlier same-tag turn that mentioned one.
3. If you have ≥2 paths with the same tag AND a name → enroll once: `POST /speaker/enroll` with `wav_paths=[<oldest>, ..., <newest>]` (oldest first).
4. If you only have 1 path so far → ask one follow-up "tell me your name and a bit about yourself, ~25–30 words is enough" and wait for the next turn.
5. After enrolling, greet the user by name. Do NOT re-ask — subsequent turns will come back as `Speaker - Name:` once the embedding is built.

### Enroll a voice (mic, two-turn)
1. Turn A was `Unknown Speaker: ... (audio save at <pathA>)` AND either no name was detected OR the transcript was too short (< 25 words) for a reliable voice embedding.
2. Ask one follow-up that both requests the name AND guides the user to speak longer. Examples:
   - EN: "I didn't quite catch that — could you tell me your name and then say a bit more about yourself? About 25–30 words is perfect. You can introduce yourself or just read any short paragraph out loud."
   - VI: "Mình chưa nghe rõ — bạn nói lại tên giúp mình nhé, rồi nói thêm vài câu giới thiệu bản thân hoặc đọc một đoạn văn bất kỳ, khoảng 25–30 từ là đủ."
3. Turn B now carries user name + a longer recording: `Unknown Speaker: ... (audio save at <pathB>)`.
4. **Map paths carefully** — `<pathA>` is the path from the FIRST Unknown Speaker turn (before the follow-up), `<pathB>` is the path from the turn AFTER the follow-up. Never swap them.
5. Call `POST /speaker/enroll` exactly once:
   - If Turn A was only missing a name but audio was long enough → `wav_paths=[<pathA>, <pathB>]` (both useful).
   - If Turn A audio was too short → `wav_paths=[<pathB>]` only (prefer the longer recording).
6. If Turn B is still too short (< 25 words), apologise and ask one more time; do NOT enroll on short audio.

### Enroll from Telegram voice note ("remember my voice")
1. Telegram audio arrives at `SRC` (e.g. `/tmp/openclaw/media/voice_xxx.ogg` — exact path depends on OpenClaw's media dir, take it from `mediaPaths`).
2. If `SRC` is already `.wav` → use it directly. Otherwise convert to WAV **in the same directory** with `ffmpeg -ar 16000 -ac 1`. Use `DST="${SRC%.*}.wav"` — same folder, same basename, `.wav` extension.
3. Choose enroll name:
   - Prefer the name user explicitly says in transcript.
   - If transcript has no clear name, fallback to Telegram display name / username from message context.
4. Call `POST /speaker/enroll` with that WAV path + `telegram_username` + `telegram_id` from the message context.

### Link Telegram to a mic-only profile
1. User is already enrolled via mic (`GET /speaker/list` shows `has_telegram_identity: false`) and now sends a Telegram intro.
2. Call `POST /speaker/identity` with the name + Telegram fields. No audio upload needed.

### Recognize a Telegram voice
1. Convert to WAV in the same dir as above (if not already `.wav`).
2. Call `POST /speaker/recognize` with that WAV path.
3. `match: true` → use `name`; `match: false` → treat as unknown, `unknown_audio_path` is kept for a follow-up enroll.

### List / remove / reset
- "Who do you know?" → `GET /speaker/list`. Reply with display names, not raw JSON.
- "Forget my voice" → `POST /speaker/remove` with the name.
- Owner says "wipe all voice profiles" → `POST /speaker/reset`.

## Tools

**Bash** with `curl` for HTTP calls to `http://127.0.0.1:5001`.

### Enroll (mic, one path)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "wav_paths": ["/tmp/lumi-unknown-voice/incoming_171_abc.wav"]}'
```

### Enroll (mic, two paths — Turn A + Turn B)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "wav_paths": ["/tmp/lumi-unknown-voice/incoming_A.wav", "/tmp/lumi-unknown-voice/incoming_B.wav"]}'
```

### Enroll (Telegram voice — convert in-place if needed)
```bash
SRC="/tmp/openclaw/media/voice_abc.ogg"   # take from the message's mediaPaths
if [[ "$SRC" == *.wav ]]; then
  DST="$SRC"
else
  DST="${SRC%.*}.wav"                      # same folder, same basename, .wav
  ffmpeg -i "$SRC" -ar 16000 -ac 1 -y "$DST" 2>/dev/null
fi
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"darren\", \"wav_paths\": [\"$DST\"], \"telegram_username\": \"darren_92\", \"telegram_id\": \"123456789\"}"
```

### Recognize (Telegram voice)
```bash
# After conversion as above:
curl -s -X POST http://127.0.0.1:5001/speaker/recognize \
  -H "Content-Type: application/json" \
  -d "{\"wav_path\": \"$DST\"}"
```

Response includes `name`, `confidence`, `match`, `display_name`, `telegram_username`, `telegram_id`, `unknown_audio_path`, `candidates` (top-3).

### Link Telegram identity (no audio upload)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/identity \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "telegram_username": "darren_92", "telegram_id": "123456789"}'
```

### List registered voices
```bash
curl -s http://127.0.0.1:5001/speaker/list
```

### Remove one voice
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/remove \
  -H "Content-Type: application/json" \
  -d '{"name": "darren"}'
```

### Reset all voices (owner only)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/reset
```

## Error Handling
- 400 `wav file not found` — should not happen now (route filters missing paths and returns idempotent meta when applicable). If it does, skip silently.
- 400 `all wav paths missing and no existing voice profile` — every path you passed is gone AND the user isn't enrolled. The user probably needs to introduce themselves again — ask once with the "25–30 words" guidance.
- 400 `invalid base64` / `empty audio` / `cannot decode WAV` — corrupt file. Apologize and skip.
- 400 `no audio chunks extracted` / `no valid new samples` — audio too short / silent / VAD rejected. Ask user to speak longer.
- 503 `embedding service unavailable` — dlbackend down. Tell user "voice recognition is offline, please try again in a moment."
- 503 `Speaker recognizer unavailable` — service not initialized (missing deps). Voice recognition offline.
- 404 on `/speaker/identity` — user has no voice profile yet. Enroll first.
- 404 on `/speaker/remove` — no voice profile under that name. Tell the user "I don't have a voice on file for <name>".
- **Idempotent retry:** if you call `/speaker/enroll` with paths that were just consumed by a prior successful enroll, the route returns the existing user meta with `status: "ok"` instead of erroring — safe to retry without checking.

## Rules
- **Self-enrollment only** — NEVER enroll someone else's voice. If "this is my friend Bob", tell them Bob must speak himself.
- **Lowercase normalized names** — use the same `name` as `face-enroll` for the same person (folder `/root/local/users/<name>/` is shared across skills).
- **Always include Telegram identity when the message came from Telegram** — pass `telegram_username` + `telegram_id`. Omit (don't send empty strings) when unknown.
- **Minimum voice length for enrollment** — the spoken transcript for an enrollment audio must be **at least ~25 words (aim for 25–30)** OR be combined with prior same-tag turns to reach roughly 5–10s of speech. Below that threshold the voice embedding is unreliable.
- **Recognize all 3 message variants as actionable** — Branch B (`audio save at` + auto enroll), Branch C (`audio saved at` + "too short" hint), and the cooldown variant (`audio saved at` with no instruction) all carry usable paths and `[voice:N]` tags. The decision matrix above applies to all three uniformly. Don't ignore a turn just because it lacks the strong "auto enroll" instruction — the user may be volunteering a name during cooldown.
- **Cluster claim is automatic** — when you pass a path that lives inside a `voice_<N>` folder, the server pulls every sibling WAV from that cluster into the enrollment automatically. You don't have to enumerate them. So passing one path from `voice_5/` is enough; the server handles the rest and deletes the cluster after.
- **Path mapping in two-turn flow** — `<pathA>` is ALWAYS the Unknown Speaker turn BEFORE your follow-up question; `<pathB>` is the turn AFTER it. Never swap them and never pass an enrollment path that wasn't produced by the current speaker.
- **Use `/speaker/identity`, not re-enroll**, when you just want to link Telegram info to a mic-only profile (no new audio).
- **Telegram audio must be 16 kHz mono WAV** before calling the API — convert with `ffmpeg -ar 16000 -ac 1 -y "${SRC%.*}.wav"` (same folder as the source). Skip conversion if the source is already `.wav`. Non-WAV media files (`.ogg`, `.m4a`, `.mp3`, `.opus`) are rejected by the embedding backend.
- **Telegram remember-voice naming rule** — use the spoken name in transcript first; if absent, use Telegram name.
- **Don't spam "who are you?"** — ask at most once per cluster, and when you do ask, always include the "speak 25–30 words" guidance in the same message instead of firing multiple short prompts. If still no usable answer, move on and reply naturally.
- **Never go silent on Unknown Speaker fragments** — even when the speaker hasn't given a name and you've already asked once, ALWAYS emit at least a short acknowledgment ("Mm", "Nghe rồi", "Got it"). NO_REPLY here is forbidden — silence makes the owner think Lamp stopped listening. The "don't spam" rule above bans re-asking, not replying.
- **Confirm every enroll** AFTER the API returns ok — "Nice to meet you, Alex! I'll remember your voice."
- **Don't narrate technical details** — no "base64", "ffmpeg", "POST /speaker/enroll".
- **Never write files directly** — always use the HTTP API. Do NOT write to `/root/local/users/` by hand.