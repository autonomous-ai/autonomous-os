# Speech Emotion Recognition (SER)

Recognize the **user's** emotion from their voice (not the lamp's). At the end of every mic session (VAD trigger → ~2.5 s silence stop), `VoiceService._submit_speech_emotion_from_session` builds a mono 16 kHz WAV from the session `audio_buffer` and enqueues it on `SpeechEmotionService`, which buffers per-user, dedups by polarity bucket, and fires `speech_emotion.detected` sensing events to Lamp. **SER runs independently of STT** — laughter, sighs, "uh-huh"s, and other non-verbal cues that leave the transcript empty still classify. Speaker recognition is invoked inline to populate the `user` field (falls back to `unknown` when speaker ID is unavailable, the buffer is too short for an embedding, or there's no match); it never gates whether SER runs.

This is the voice-side twin of facial emotion detection (`emotion.detected`). The architecture, polarity bucketing, and dedup window are intentionally symmetric so both modalities land in the same downstream skills (`user-emotion-detection/SKILL.md`, mood logging, music suggestion).

> Not to be confused with **Emotion Expression** (`emotion/SKILL.md`) — that controls the lamp's own emotional output (servo + LED + eyes). SER is about sensing what the *user* feels through speech; expression is how *Lamp* shows its feelings.

**Vietnamese:** [docs/vi/speech-emotion_vi.md](vi/speech-emotion_vi.md)

---

## Architecture

```
voice_service._stream_session(...) finally:                      ← every mic session end
    ├─ trim trailing silence on audio_buffer
    │
    ├─ wake-word split on `combined` → final_text + event_type
    │
    ├─ _identify_and_decorate(final_text, audio_buffer)           ← single speaker recognize per session
    │       → (final_msg, user_name | None)
    │  user = user_name or "unknown"
    │
    ├─ if combined: _send_to_lumi(final_msg, event_type)         ← Lamp message path
    │
    └─ _submit_speech_emotion_from_session(audio_buffer, user)   ← ALWAYS — SER pipeline
            └─ _session_wav_for_ser(buffer) → (wav, duration_s)
            └─ SpeechEmotionService.submit(user, wav, duration_s)
            ▼
SpeechEmotionService.submit(user, wav_bytes, duration_s)   ← non-blocking
    │  queue.put_nowait
    ▼
worker thread (daemon)
    │  Emotion2VecRecognizer.recognize(wav_bytes)
    │     POST {DL_BACKEND_URL}/lelamp/api/dl/ser/recognize
    │     ← { "label": "happy", "confidence": 0.78 }
    ▼
buffer[user].append(Inference)               ← per-user accumulation
    ▲
    │  (flush thread wakes every SPEECH_EMOTION_FLUSH_S)
    ▼
flush:
    ① drop neutral / <unk> / other labels
    ② mode(label) across this user's buffered samples
    ③ bucket = polarity(mode)                ← positive | negative | other
    ④ TTL dedup: key=(user, bucket) over SPEECH_EMOTION_DEDUP_WINDOW_S
    ⑤ POST Lamp /api/sensing/event with type="speech_emotion.detected"
```

LeLamp's voice pipeline **only calls `submit()`**. All HTTP I/O to dlbackend, buffering, bucketing, dedup, retry, and Lamp POST are contained inside the `speech_emotion/` module — they never block the STT path.

---

## File Layout

```
lelamp/service/voice/speech_emotion/
├── __init__.py        # public API: SpeechEmotionService + ABC + engine + result type
├── constants.py       # defaults, label vocabulary, bucket map, event type
├── base.py            # BaseSpeechEmotionRecognizer (ABC), SpeechEmotionResult dataclass
├── emotion2vec.py     # Emotion2VecRecognizer — HTTP wrapper for dlbackend /api/dl/ser/recognize
├── utils.py           # normalize_label, is_neutral, bucket_for, hedge_for, format_message
└── service.py         # SpeechEmotionService — queue + worker + flush + dedup + send-to-lamp
```

Adding a new engine: subclass `BaseSpeechEmotionRecognizer` (one method: `recognize(wav_bytes) -> SpeechEmotionResult | None`) and swap it in via `SpeechEmotionService(recognizer=...)` at construction time. Default factory builds `Emotion2VecRecognizer` from `lelamp.config.SPEECH_EMOTION_API_URL`.

---

## Threading Model

Two daemon threads, started in `SpeechEmotionService.__init__` only when `recognizer.available` is true:

| Thread | Loop | Drains | Produces |
|--------|------|--------|----------|
| `speech-emotion-worker` | `_worker_loop` | submission queue (`queue.Queue`, maxsize 32) | per-user buffer entries |
| `speech-emotion-flush` | `_flush_loop` (wait + tick every `SPEECH_EMOTION_FLUSH_S`) | per-user buffer | `speech_emotion.detected` POSTs to Lamp |

Both threads exit cleanly on `stop()` — the worker is poisoned with a `None` sentinel, the flush thread observes the stop event during its `Event.wait`.

`submit()` is non-blocking by design. If the worker queue is full (32 jobs backlog) the new submission is dropped with a warning — this signals real overload (dlbackend wedged or down) and the caller should not retry. Audio is single-utterance, not streaming, so a one-second drop is acceptable.

---

## Wire Format

### Submission (in-process)

```python
service.submit(
    user="alice",                 # enrolled speaker label, or "unknown" (UNKNOWN_USER_LABEL)
    wav_bytes=b"RIFF....WAVE...", # mono 16 kHz WAV from STT session buffer
    duration_s=2.4,               # length of audio for the MIN_AUDIO_S gate
)
```

### Engine call → dlbackend

```http
POST {DL_BACKEND_URL}/lelamp/api/dl/ser/recognize
Headers:
  X-API-Key: <key>
  Content-Type: application/json
Body:
  {"audio_b64": "<base64 WAV (mono 16 kHz)>", "return_scores": false}
```

Response:

```json
{ "label": "happy", "confidence": 0.9981, "scores": null }
```

Labels (emotion2vec_plus_large, from `/api/dl/ser/labels`):

```
angry, disgusted, fearful, happy, neutral, other, sad, surprised, <unk>
```

### Sensing event → Lamp

```http
POST http://127.0.0.1:5000/api/sensing/event
Content-Type: application/json
```

```json
{
  "type": "speech_emotion.detected",
  "message": "Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; treat as uncertain, do not assume the user is distressed.)",
  "current_user": "alice"
}
```

The raw `Speech emotion detected: <Label>.` prefix is the parser anchor for Lumi-side routing. The parenthetical is a hedge clause to stop the LLM from over-committing on noisy SER reads — same pattern as the facial `Emotion detected: …` message.

Retry policy: 3 attempts with 2 s back-off on `ConnectionError` or HTTP `503`. Other 4xx/5xx are logged and dropped (the sample is gone — we don't retry-storm Lamp).

---

## Polarity Buckets

Bucketing mirrors the facial pipeline so `(user, bucket)` dedup keys are interpretable across modalities. The buckets are:

| Bucket | Labels |
|--------|--------|
| `positive` | happy, surprised |
| `negative` | angry, disgusted, fearful, sad |
| `other` | neutral, other, `<unk>` (these are **dropped before bucketing** — see anti-spam guard #4) |

Why bucket-level dedup, not label-level: emotion2vec on short utterances flips between sad/fearful/angry within the same affective state. Per-label dedup would over-deliver. Per-bucket dedup collapses within-bucket noise (sad ↔ fearful ↔ angry) into one negative event per window; cross-bucket flips (sad → happy) still fire as a genuine mood change.

---

## Anti-Spam Guards

Layered, matched 1-to-1 with the facial emotion processor:

| # | Stage | Drop condition |
|---|-------|----------------|
| 1 | `submit()` | `wav_bytes` empty / `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` |
| 2 | `submit()` | `user` is empty (no subject to attribute emotion to — mirrors face `current_user==""`) |
| 3 | worker | `confidence < CONFIDENCE_THRESHOLD_BY_LABEL[label]` (per-label gate, see Configuration) |
| 4 | flush  | label is `neutral` / `other` / `<unk>` |
| 5 | flush  | `(user, bucket)` was sent less than `SPEECH_EMOTION_DEDUP_WINDOW_S` seconds ago |

Each bucket keeps its own independent TTL entry in `_last_sent_by_key`. Sending a positive event does NOT reset the negative window (and vice versa). Same semantics as facial emotion.

---

## Configuration

All knobs live in `lelamp/config.py` as `SPEECH_EMOTION_*`, overridable via env vars. Defaults mirror `EMOTION_*` so the two modalities behave identically out of the box.

| Constant | Env var | Default | Purpose |
|----------|---------|---------|---------|
| `SPEECH_EMOTION_ENABLED` | `LELAMP_SPEECH_EMOTION_ENABLED` | `true` | Master kill switch |
| `SPEECH_EMOTION_FLUSH_S` | `LELAMP_SPEECH_EMOTION_FLUSH_S` | `10.0` | Buffer drain cadence |
| `SPEECH_EMOTION_DEDUP_WINDOW_S` | `LELAMP_SPEECH_EMOTION_DEDUP_WINDOW_S` | `300.0` | TTL for `(user, bucket)` |
| `SPEECH_EMOTION_MIN_AUDIO_S` | `LELAMP_SPEECH_EMOTION_MIN_AUDIO_S` | `3.0` | Min utterance length |
| `SPEECH_EMOTION_API_TIMEOUT_S` | `LELAMP_SPEECH_EMOTION_API_TIMEOUT_S` | `15` | dlbackend HTTP timeout |
| `DL_SER_ENDPOINT` | `DL_SER_ENDPOINT` | `/lelamp/api/dl/ser/recognize` | Path suffix on `DL_BACKEND_URL` |
| `SPEECH_EMOTION_API_URL` | — | derived | `DL_BACKEND_URL` + `DL_SER_ENDPOINT` |
| `SPEECH_EMOTION_API_KEY` | — | mirrors `DL_API_KEY` | Sent as `X-API-Key` |

Label vocabulary, bucket map, and **per-label confidence thresholds** are declared in `lelamp/service/voice/speech_emotion/constants.py` (not env-overridable — touching these requires a code change). The threshold dict:

```python
# constants.py
CONFIDENCE_THRESHOLD_BY_LABEL: dict[str, float] = {
    "happy":     0.5,
    "surprised": 0.6,
    "sad":       0.6,
    "angry":     0.6,
    "fearful":   0.7,
    "disgusted": 0.7,
}
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5  # fallback for unlisted labels
```

Negative emotions get higher gates to avoid false-positive alarms; happy is loosest because positive misfires are cheap. Lookup goes through `utils.threshold_for(label)` which falls back to `DEFAULT_CONFIDENCE_THRESHOLD` for any unmapped label.

---

## Integration Point

Called from `VoiceService._stream_session`'s `finally` block. Speaker recognize runs **once** per session and its result feeds both the Lamp-message decoration and the SER `user` field:

```python
# In _stream_session finally, after trim:

# 1. Wake-word split (only when transcript exists)
event_type = "voice"
final_text = combined
if combined:
# ... strip wake word; set event_type = "voice_command" if matched

# 2. Single speaker recognize per session
final_msg, se_user = self._identify_and_decorate(final_text, audio_buffer)
user = se_user if se_user else UNKNOWN_USER_LABEL

# 3. Decorate → Lamp (only when STT had text)
if combined:
    self._send_to_lamp(final_msg, event_type=event_type) 

# 4. Submit SER — independent pipeline, reuses speaker result from step 2
self._submit_speech_emotion_from_session(audio_buffer, user=user)
```

`_submit_speech_emotion_from_session` is now a thin submitter (no embedded speaker call):

```python
session_audio = self._session_wav_for_ser(audio_buffer)
if session_audio is None:
    return                                          # buffer empty or < SPEAKER_MIN_AUDIO_S
wav_bytes, duration_s = session_audio
self._speech_emotion.submit(user=user, wav_bytes=wav_bytes, duration_s=duration_s)
```

The previous `_send_best` closure has been inlined into the finally block. SER is still decoupled from STT (fires even when `combined` is empty), and now shares one `/embed` call with the Lamp-decoration path.

### SER user attribution

| Speaker ID outcome | `user` passed to `submit()` |
|--------------------|-----------------------------|
| `match=True` with enrolled name | Speaker label (e.g. `alice`) |
| `match=False` / below threshold (API OK, no `error`) | `unknown` — set directly by `_identify_and_decorate` |
| Recognize skipped or failed (`user_name` is `None`) | `unknown` — `_submit_speech_emotion_from_session` fallback |

So **no match** and **embedding API error** both dedup under the `unknown` key when the session buffer is long enough. SER is never invoked from inside `_identify_and_decorate`.

### When SER is not submitted

- `SPEECH_EMOTION_ENABLED=false` or `SpeechEmotionService` not `available`
- `audio_buffer` empty or shorter than `SPEAKER_MIN_AUDIO_S` (gates `_session_wav_for_ser`)
- `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` (gates `submit()` itself — default 3.0 s)
- `submit()` drops (queue full, empty `user` after normalize, etc.)

`wav_bytes` is built from the STT session `audio_buffer` via `_session_wav_for_ser` (separate from the WAV encoded inside `_identify_and_decorate` for speaker `/embed`, even though they currently happen to share the same buffer contents).

Lazy init in `VoiceService.__init__` mirrors the speaker recognizer pattern: instance is created once, threads start only when the engine reports `available`.

### Cost note: speaker recognize is shared across paths

Speaker recognize fires **once** per mic session. The single `(final_msg, user_name)` result is reused by:

1. The Lamp POST (`_send_to_lumi(final_msg, event_type)`) — when STT had a transcript.
2. The SER submit (`_submit_speech_emotion_from_session(..., user=...)`) — always.

This is the reason the finally block ordering is: wake-word split → `_identify_and_decorate` once → Lamp POST → SER submit. `_submit_speech_emotion_from_session` accepts `user` as an argument now; it no longer issues its own `/embed` request.

---

## Failure Modes

| Failure | Effect | Recovery |
|---------|--------|----------|
| `DL_BACKEND_URL` not configured | `recognizer.available` is False, threads never start, `submit()` is a no-op | Set `llm_base_url` in Lamp config |
| dlbackend down (connection refused) | Worker logs warning, sample dropped, no retry | Next utterance retries automatically |
| dlbackend returns non-200 | Worker logs warning, sample dropped | Same as above |
| Worker queue full | `submit()` logs warning, returns immediately | Indicates backend overload; investigate |
| Lamp sensing endpoint down | 3 retries with 2 s back-off, then sample dropped | Buffer continues filling for next flush |
| `duration_s < MIN_AUDIO_S` | Dropped in `submit()` with debug log | Expected — short utterances aren't worth classifying |

Nothing here blocks the STT path or speaker recognition — SER failures are silent at the user level and visible only in `/var/log/lelamp/server.log`.

---

## Diagnostic Snapshot

`SpeechEmotionService.to_dict()` returns a runtime snapshot for the `/voice/status` (or any future) introspection endpoint:

```json
{
  "type": "speech_emotion",
  "available": true,
  "buffered_users": 2,
  "dedup_keys": 3,
  "queue_size": 0,
  "last_flush_ts": 1715587812.41
}
```

---

## Relationship to Other Pipelines

| Pipeline | Modality | Trigger | Event type | Same skill consumes? |
|----------|----------|---------|------------|----------------------|
| Facial emotion (`emotion.py` perception) | Camera frame → face crop | Every face seen | `emotion.detected` | yes — `user-emotion-detection/SKILL.md` |
| **Speech emotion (this doc)** | Mic → end-of-session WAV | Every mic session with sufficient buffered audio — **independent of STT transcript** | `speech_emotion.detected` | yes — same `user-emotion-detection/SKILL.md` (router accepts both prefixes) |
| Mood synthesis (Mood skill) | — | Any emotion signal | mood `signal` / `decision` rows | — |
| Sound (`sound.py` perception) | Mic RMS | Loud noise | `sound` | dog-bark escalation, separate skill |

Speech emotion shares the polarity vocabulary with facial emotion deliberately. Lamp's sensing handler tags incoming events with `[speech_emotion]` (vs `[emotion]` for face), pre-fetches the same `[emotion_context: ...]` block via `skillcontext.BuildEmotionContext`, and routes to `user-emotion-detection/SKILL.md`. The label-to-mood map covers both vocabularies (`Fear`/`Fearful → stressed`, `Surprise`/`Surprised → excited`, `Disgust`/`Disgusted → frustrated`); the only modality-specific behavior in the skill is `source:"voice"` vs `source:"camera"` on the mood signal log row. Music-suggestion cooldown is shared across modalities so voice cannot bypass a recent camera-driven suggestion, and vice versa.
