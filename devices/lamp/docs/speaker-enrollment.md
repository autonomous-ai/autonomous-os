# Speaker Voice Enrollment — Technical Spec

**Status: IMPLEMENTED** (2026-04)

## Overview

Lamp identifies who is speaking via **WeSpeaker ResNet34** (256-dim embedding, ONNX Runtime). When a speaker is not recognized, HAL saves the audio and optionally nudges the AI agent to enroll the voice. Enrollment is **self-service only** — each person enrolls their own voice.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  HAL (Python, port 5001)                                         │
│                                                                     │
│  VoiceService._stream_session()                                     │
│    ├─ STT transcript ready                                          │
│    ├─ _identify_and_decorate(transcript)                            │
│    │   ├─ audio_buffer → WAV bytes → base64                        │
│    │   ├─ POST /audio-recognizer/embed → dlbackend (RunPod)        │
│    │   │   └─ WeSpeaker ResNet34 ONNX → 256-dim L2-normalized      │
│    │   ├─ Per-chunk voting vs enrolled embeddings                   │
│    │   ├─ Match ≥ 0.7 → "Speaker - Name: transcript"               │
│    │   └─ No match → _format_unknown_speaker()                     │
│    │       ├─ _should_request_enroll() gate                         │
│    │       │   ├─ ≥ 25 words in transcript                          │
│    │       │   └─ ≥ 5s audio duration                               │
│    │       ├─ PASS → "Unknown Speaker: ... (audio save at <path>,   │
│    │       │          auto enroll ...)"                              │
│    │       └─ FAIL → "Unknown Speaker: ..." (no enroll instruction) │
│    └─ POST /api/sensing/event → Lamp (Go)                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Lamp (Go, port 5000)                                               │
│                                                                     │
│  Two paths (both call domain.AppendEnrollNudge):                    │
│                                                                     │
│  1. Direct path (handler.go)                                        │
│     └─ Agent idle → send immediately to OpenClaw                    │
│                                                                     │
│  2. Drain path (service.go)                                         │
│     └─ Agent busy → queue → replay when idle                        │
│                                                                     │
│  AppendEnrollNudge(msg) — domain/voice.go:                          │
│    ├─ Check: contains "Unknown Speaker:" + "audio save at"          │
│    ├─ Cooldown: skip if < 5 min since last nudge                    │
│    └─ Append: "[REQUIRED: Follow speaker-recognizer/SKILL.md ...]"  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  OpenClaw Agent                                                     │
│                                                                     │
│  speaker-recognizer/SKILL.md                                        │
│    ├─ Detects self-introduction ("I'm X", "my name is X")           │
│    ├─ curl POST /speaker/enroll with wav_path + name                │
│    ├─ Two-turn: ask "Who are you?" → enroll with both paths         │
│    └─ Confirm: "Nice to meet you, Name!"                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Anti-Spam Gates

Four layers prevent the agent from repeatedly asking "who are you?":

| Layer | Where | Gate | Purpose |
|-------|-------|------|---------|
| **Audio duration** | HAL `voice_service.py` | `duration_s < SPEAKER_MIN_AUDIO_S` (0.8s) | Skip recognition entirely for very short audio |
| **Enroll instruction** | HAL `_should_request_enroll()` | `≥ 15 words AND ≥ 2s audio` | Don't append full enroll instruction for short utterances (short variant with multi-turn combine hint is still sent) |
| **Lamp-side nudge cooldown** | Lamp `domain/voice.go` | `5 min since last nudge` | Don't inject SKILL.md instruction more than once per 5 min |
| **Per-voiceprint nudge cooldown** | HAL `voice_service.py` | `30 min per voiceprint_hash` (`HAL_ENROLL_NUDGE_COOLDOWN_S`) | Don't repeat "ask user's name" for the same unknown voice cluster; plain `Unknown Speaker:` message sent instead |

## Model & Embedding

| Property | Value |
|----------|-------|
| Model | WeSpeaker ResNet34 (VoxCeleb trained) |
| Embedding dim | 256 |
| Runtime | ONNX Runtime (CPU) on dlbackend (RunPod) |
| Endpoint | `POST {DL_BACKEND_URL}/lelamp/api/dl/audio-recognizer/embed` |
| Auth | `X-API-Key` header |
| Timeout | 15s |

### Recognition Algorithm

1. Audio → preprocess (noise reduce, VAD, HPF, RMS normalize)
2. Extract per-chunk embeddings `[M, 256]`
3. Cosine similarity against all enrolled speaker embeddings
4. Per-chunk voting: each chunk votes for its closest match
5. Winner = most votes (tiebreak by average confidence)
6. `confidence ≥ 0.7` → match; else unknown

### Enrollment Quality

1. Each WAV sample → embedding via dlbackend
2. Filter by consistency threshold `0.7` (cosine similarity between samples)
3. Aggregate remaining embeddings via weighted average
4. Store L2-normalized vector at `/root/local/users/{name}/voice/embedding.npy`

### Voice Cluster Tracking (`voiceprint_hash`)

Every unknown voice is locally clustered so the server can say "this is the same unknown speaker we heard 3 minutes ago" without needing any backend support. Lets the agent combine multiple short utterances into one enroll call.

1. After embedding the query audio, the recognizer aggregates per-chunk embeddings into a single L2-normalized vector.
2. Compare against stored stranger-cluster centroids (cosine similarity).
3. Match ≥ `HAL_VOICE_STRANGER_MATCH_THRESHOLD` (default `0.65`, lower than the 0.7 known-speaker threshold so same voice clusters instead of fragmenting) → reuse existing label `voice_N`.
4. No match → allocate new label `voice_{counter}`, append centroid to on-disk state.
5. Cap at `HAL_MAX_VOICE_STRANGERS` (default `50`) — oldest evicted when exceeded.
6. The assigned hash is:
   - returned on the recognize response as `voiceprint_hash: "voice_N"` (null for known speakers)
   - surfaced in the nudge message as `[voice:voice_N]` tag so the skill can correlate turns
   - used to subdir-group the saved WAV (see Storage)

**Trailing-silence trim**: before the WAV goes to the embedding API, the speaker-ID buffer is truncated at the last speech frame + 200 ms tail. Without this a 3-second utterance ends up as ~5.5 s with ~45% silence, diluting the embedding. Only affects the speaker-ID path — STT still receives the full stream.

## Configuration

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Match threshold | 0.7 | `SPEAKER_MATCH_THRESHOLD` | Min confidence for speaker match |
| Enroll consistency | 0.7 | `SPEAKER_ENROLL_CONSISTENCY_THRESHOLD` | Min cosine similarity between enrollment samples |
| API timeout | 15s | `SPEAKER_EMBEDDING_API_TIMEOUT_S` | HTTP timeout for embedding API |
| Min audio for recognition | 0.8s | `HAL_SPEAKER_MIN_AUDIO_S` | Skip recognition below this |
| Min words for enroll nudge | 15 | Hardcoded in `_should_request_enroll()` | Transcript word count gate |
| Min duration for enroll nudge | 2.0s | Hardcoded in `_should_request_enroll()` | Audio duration gate |
| Lamp nudge cooldown | 5 min | Hardcoded in `domain/voice.go` | Don't re-inject SKILL instruction globally |
| Per-voiceprint nudge cooldown | 30 min | `HAL_ENROLL_NUDGE_COOLDOWN_S` | Don't re-ask name for same voiceprint cluster |
| Voice stranger match threshold | 0.65 | `HAL_VOICE_STRANGER_MATCH_THRESHOLD` | Cosine similarity to cluster unknown voice into existing `voice_N` |
| Max voice strangers | 50 | `HAL_MAX_VOICE_STRANGERS` | Cluster cap; oldest evicted when exceeded |
| Voice strangers dir | `/root/local/voice_strangers` | `HAL_VOICE_STRANGERS_DIR` | Persist cluster embeddings (survives reboot) |
| Speaker recognition enabled | true | `HAL_SPEAKER_RECOGNITION_ENABLED` | Master toggle (default on; gated on the `audio` capability) |

## Storage

```
/root/local/users/{name}/
  metadata.json                      # Shared identity (telegram, display_name)
  voice/
    embedding.npy                    # L2-normalized aggregated vector [256]
    metadata.json                    # num_samples, dim, timestamps
    sample_{origin}_{ts}_{uuid}.wav  # Individual enrollment samples (16kHz mono)

/tmp/hal-unknown-voice/
  incoming_{ts}_{uuid}.wav           # Known-speaker audio (flat)
  voice_{N}/
    incoming_{ts}_{uuid}.wav         # Unknown audio — grouped by voiceprint cluster

/root/local/voice_strangers/
  embeds.npy                         # Stranger cluster centroids [N, 256]
  labels.npy                         # Cluster labels ["voice_1", "voice_2", ...]
  counter.npy                        # Monotonic counter for next new label
```

## API Endpoints (HAL, port 5001)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/speaker/enroll` | Enroll voice from wav_paths + name |
| `POST` | `/speaker/recognize` | Recognize speaker from wav_path |
| `POST` | `/speaker/identity` | Link Telegram identity to existing profile |
| `POST` | `/speaker/remove` | Remove voice profile by name |
| `POST` | `/speaker/reset` | Remove all voice profiles |
| `GET`  | `/speaker/list` | List enrolled speakers |

### Error contract

`/speaker/enroll` distinguishes two failure classes:

| HTTP | When | Skill behavior |
|------|------|----------------|
| `400` | Audio-level reject (too short, silent, VAD found no speech, dlbackend returned 4xx) | Ask user to re-record / speak more clearly |
| `503` | Embedding service unreachable (network, 5xx, malformed response) | Tell user to try again shortly — nothing on disk was modified |

`/speaker/recognize` never fails with 5xx for embedding outages — it returns `200` with `{name: "unknown", error: "<reason>"}` so the skill can gracefully degrade. Only input-level problems (missing WAV, bad base64) return `400`.

## Key Code Locations

| Component | File | Function/Struct |
|-----------|------|-----------------|
| STT → speaker ID | `os/hal/drivers/voice/voice_service.py` | `_identify_and_decorate()` |
| Enroll gate | `os/hal/drivers/voice/voice_service.py` | `_should_request_enroll()` |
| Message formatting | `os/hal/drivers/voice/voice_service.py` | `_format_unknown_speaker()` |
| Speaker recognizer | `os/hal/drivers/voice/speaker_recognizer/speaker_recognizer.py` | `SpeakerRecognizer` |
| Nudge injection + cooldown | `os/services/domain/voice.go` | `AppendEnrollNudge()` |
| Direct event path | `os/services/server/sensing/delivery/http/handler.go` | `PostEvent()` |
| Drain/replay path | `os/services/internal/openclaw/service.go` | `drainPendingEvents()` |
| Agent skill | `lamp/resources/openclaw-skills/speaker-recognizer/SKILL.md` | — |
| Embedding model | `dlbackend/src/core/audio_recognition/audio_recognizer.py` | `ResNet34Recognizer` (default), `EcapaTdnn1024Recognizer`, `CamPPlusRecognizer` — chọn qua env `AUDIO_RECOGNIZER_ENGINE` |
| Embedding endpoint | `dlbackend/src/protocols/htpp/audio_recognizer.py` | `embed_audio()` |
| Config | `os/hal/config.py` | `SPEAKER_*` constants |

## Message Flow Examples

### Short utterance (blocked)
```
User says: "hey" (2 words, 0.9s audio)
→ HAL: skip recognition (< SPEAKER_MIN_AUDIO_S)
→ Message: "hey" (no prefix, no enroll instruction)
```

### Medium utterance (recognized but no enroll nudge)
```
User says: "turn on the lights please" (5 words, 3s audio)
→ HAL: recognize → unknown, _should_request_enroll(5 words, 3s) = false
→ Message: "Unknown Speaker: turn on the lights please"
→ Lamp: no "audio save at" in message → AppendEnrollNudge returns unchanged
→ Agent: responds normally, doesn't ask who user is
```

### Multi-turn combine (same voice cluster)
```
User turn 1: "nice to meet you today. Okay." (5 words)
→ HAL: recognize → unknown, voiceprint_hash=voice_5
→ WAV moved to /tmp/hal-unknown-voice/voice_5/incoming_A.wav
→ Message: "Unknown Speaker: [voice:voice_5] nice to meet you today. Okay. (audio saved at ..._A.wav. Note: audio is too short for single enrollment. If prior turns tagged the same voice_5, combine their saved paths with this one...)"
→ Agent: asks "Could you tell me your name?"

User turn 2: "I'm Alex." (2 words)
→ HAL: voiceprint_hash=voice_5 (same cluster, sim=0.75)
→ WAV moved to /tmp/hal-unknown-voice/voice_5/incoming_B.wav
→ Message: "Unknown Speaker: [voice:voice_5] I'm Alex. (audio saved at ..._B.wav...)"
→ Agent: scans prior turns for same [voice:voice_5] tag → finds path A
→ Agent: POST /speaker/enroll with wav_paths=[path_A, path_B], name="Alex"
→ Agent: "Nice to meet you, Alex!"
```

### Long utterance (full enroll flow)
```
User says: "Hi my name is Leo and I just got home from work..." (30 words, 8s audio)
→ HAL: recognize → unknown, _should_request_enroll(30 words, 8s) = true
→ Message: "Unknown Speaker: Hi my name is Leo... (audio save at /tmp/hal-unknown-voice/incoming_xxx.wav, auto enroll...)"
→ Lamp: AppendEnrollNudge → cooldown OK → append "[REQUIRED: Follow speaker-recognizer/SKILL.md...]"
→ Agent: detects "my name is Leo" → POST /speaker/enroll → "Nice to meet you, Leo!"
```

### Cooldown (blocked)
```
Same unknown speaker, 2 minutes later:
→ HAL: _should_request_enroll = true (long enough)
→ Message has "audio save at"
→ Lamp: AppendEnrollNudge → cooldown NOT elapsed (< 5 min) → skip instruction
→ Agent: sees "Unknown Speaker: ..." without SKILL instruction → responds normally
```
