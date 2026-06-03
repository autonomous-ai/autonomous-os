# Speaker API — curl + error handling

HTTP at `http://127.0.0.1:5001`. Load only when actually calling the API.

## Enroll (mic, one path)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "wav_paths": ["/tmp/lamp-unknown-voice/incoming_171_abc.wav"]}'
```

## Enroll (mic, multiple paths — same `[voice:N]` combined)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "wav_paths": ["/tmp/lamp-unknown-voice/incoming_A.wav", "/tmp/lamp-unknown-voice/incoming_B.wav"]}'
```

## Enroll (Telegram — convert in-place if needed)
```bash
SRC="/tmp/openclaw/media/voice_abc.ogg"   # from mediaPaths
if [[ "$SRC" == *.wav ]]; then
  DST="$SRC"
else
  DST="${SRC%.*}.wav"
  ffmpeg -i "$SRC" -ar 16000 -ac 1 -y "$DST" 2>/dev/null
fi
curl -s -X POST http://127.0.0.1:5001/speaker/enroll \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"darren\", \"wav_paths\": [\"$DST\"], \"telegram_username\": \"darren_92\", \"telegram_id\": \"123456789\"}"
```

## Recognize (Telegram voice)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/recognize \
  -H "Content-Type: application/json" \
  -d "{\"wav_path\": \"$DST\"}"
```
Response: `name`, `confidence`, `match`, `display_name`, `telegram_username`, `telegram_id`, `unknown_audio_path`, `candidates` (top-3).

## Link Telegram identity (no audio upload)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/identity \
  -H "Content-Type: application/json" \
  -d '{"name": "darren", "telegram_username": "darren_92", "telegram_id": "123456789"}'
```

## List registered voices
```bash
curl -s http://127.0.0.1:5001/speaker/list
```

## Remove one voice
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/remove \
  -H "Content-Type: application/json" \
  -d '{"name": "darren"}'
```

## Reset all voices (owner only)
```bash
curl -s -X POST http://127.0.0.1:5001/speaker/reset
```

## Errors

- **400 `wav file not found`** — route filters missing paths and returns idempotent meta when applicable. If you see it, skip silently.
- **400 `all wav paths missing and no existing voice profile`** — every path is gone AND user not enrolled. Ask once with the "25–30 words" guidance.
- **400 `invalid base64` / `empty audio` / `cannot decode WAV`** — corrupt file. Apologize + skip.
- **400 `no audio chunks extracted` / `no valid new samples`** — too short / silent / VAD rejected. Ask user to speak longer.
- **503 `embedding service unavailable`** — dlbackend down. Tell user "voice recognition is offline, please try again in a moment."
- **503 `Speaker recognizer unavailable`** — service not initialized (missing deps).
- **404 on `/speaker/identity`** — no voice profile yet; enroll first.
- **404 on `/speaker/remove`** — no profile under that name; "I don't have a voice on file for <name>".
- **Idempotent retry** — if you call `/speaker/enroll` with paths just consumed by a prior successful enroll, the route returns existing user meta with `status: "ok"` instead of erroring. Safe to retry.
