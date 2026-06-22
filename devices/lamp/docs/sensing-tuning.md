# Sensing Threshold Tuning Guide

How to tune Lamp's sensing thresholds on real hardware.
All constants live in `os/hal/config.py` and `os/hal/drivers/voice/voice_service.py`.

## View Logs

SSH into the Pi, then:

```bash
# HAL log (motion, sound, light events all here)
tail -f /var/log/hal/server.log

# Lamp log (confirms event received + forwarded to OpenClaw)
journalctl -fu lamp -f
```

When an event fires you will see two lines — one in each log:

```
# lelamp log
INFO lelamp.service.sensing.sensing_service: [sensing] motion: Small movement detected...

# lamp log
[sensing] received motion event → forwarding to OpenClaw
```

---

## Motion Detection (Activity Recognition)

`MotionPerception` runs Kinetics action recognition (via dlbackend) and emits a
`motion.activity` event with the recognized activity labels.

**File:** `os/hal/config.py`

```python
MOTION_CONFIDENCE_THRESHOLD = 0.3   # min action-recognition confidence to buffer a label
MOTION_FLUSH_S = 10.0               # buffer drain cadence — at most one flush per 10s
MOTION_EVENT_COOLDOWN_S = 360.0     # global floor between motion.activity emissions (6 min)
```

**Emission gates (in order, `motion.py`):**

1. **Flush cadence** — buffered detections are drained at most once per `MOTION_FLUSH_S`.
2. **Presence gate** — no event unless presence == PRESENT.
3. **Global cooldown** — no `motion.activity` more than once per `MOTION_EVENT_COOLDOWN_S`,
   regardless of label changes. Bypassed by a posture nudge (already time-gated by the
   pose window) and by a user change (a new user/session sees a fresh event immediately).
4. **Per-label dedup** — even within cooldown is cleared, the same `(user, label-set)`
   within a 5-min window is dropped. Noisy Kinetics labels flip the set often, so the
   global cooldown above is the dominant gate.

**How to read the log:**

```
INFO hal...motion: [motion] raw actions in window: ['writing', 'typing']
INFO hal...motion: [motion] flushing: Activity detected: writing.
INFO hal...motion: [motion] cooldown drop: ... (last event 42.1s ago < 360s floor)
```

**Tuning:**

| Symptom | Fix |
|---------|-----|
| `motion.activity` fires constantly (every ~10s) | Increase `MOTION_EVENT_COOLDOWN_S` (360 → 600+) — this is the global floor |
| Activity not picked up at all | Decrease `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.2) |
| Spurious activity labels | Increase `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.4) |
| Reaction lags a real activity change | Decrease `MOTION_FLUSH_S` (10 → 5) and/or `MOTION_EVENT_COOLDOWN_S` |

---

## Sound Detection (Sensing)

**File:** `os/hal/config.py`

```python
SOUND_RMS_THRESHOLD = 3000   # RMS level to trigger "loud noise" event
SOUND_SAMPLE_DURATION_S = 0.5 # sample window length
```

**How to read the log:**
The event message includes the actual RMS level:

```
INFO lelamp.service.sensing.sensing_service: [sensing] sound: Loud noise detected (level: 4521)
```

Watch the `level` value during normal ambient conditions vs. when you clap/speak loudly.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Normal speech doesn't trigger event | Decrease `SOUND_RMS_THRESHOLD` (3000 → 1500) |
| Triggers on fan noise / AC hum | Increase `SOUND_RMS_THRESHOLD` (3000 → 5000) |


---

## Voice Wake Word (VAD)

**File:** `os/hal/drivers/voice/voice_service.py` (all env-tunable)

```python
HAL_VAD_THRESHOLD = 3500        # RMS to trigger speech detection (default 3500)
HAL_SILENCE_TIMEOUT = 2.5       # stop STT session after this much silence (s)
HAL_SPEECH_HOLDOFF = 0.2        # min speech duration before opening STT — rejects short clicks (s)
HAL_PRE_ROLL_FRAMES = 8         # rolling lookback frames kept BEFORE VAD trigger (8 × 64ms = 512ms)
HAL_WEBRTCVAD_ENABLED = false   # secondary gate, recommended true for low-threshold setups
HAL_SILERO_ENABLED = false      # tertiary gate (ONNX); webrtcvad usually enough
```

**How pre-roll works:** Every mic frame goes into a rolling `deque(maxlen=PRE_ROLL_FRAMES)` regardless of VAD state. When VAD finally triggers, the pre-trigger history (frames that fell under `RMS_THRESHOLD` — e.g. quiet stop consonants like "b", "k", "t", "p") gets prepended to the audio stream sent to STT. This eliminates the need for the user to say "Uhm..." as a warmup before their actual phrase.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| First syllable clipped (STT hears "ật đèn" instead of "bật đèn") | Increase `HAL_PRE_ROLL_FRAMES` (8 → 12) or decrease `HAL_VAD_THRESHOLD` (3500 → 1500) |
| Wake word not picked up reliably | Decrease `HAL_VAD_THRESHOLD` (3500 → 1500) + enable `HAL_WEBRTCVAD_ENABLED=true` as safety net |
| Lamp starts listening from ambient noise | Increase `HAL_VAD_THRESHOLD` and/or enable `HAL_WEBRTCVAD_ENABLED=true` |
| Lamp cuts off before you finish speaking | Increase `HAL_SILENCE_TIMEOUT` |
| Stale audio from previous turn bleeds into next session | Already mitigated: `lookback.clear()` fires after each session closes |
| Lamp repeats its own TTS back to OpenClaw (echo loop) | Decrease `ECHO_SIMILARITY_THRESHOLD` (0.55 → 0.45) |

---

## Light Level Detection

**File:** `os/hal/config.py`

```python
LIGHT_LEVEL_INTERVAL_S = 30.0  # check every 30 seconds
LIGHT_CHANGE_THRESHOLD = 30    # min brightness change (0–255) to trigger event
```

**How to read the log:**

```
INFO lelamp.service.sensing.sensing_service: [sensing] light.level: Ambient light decreased significantly (level: 45/255, change: -38)
```

**Tuning:**

| Symptom | Fix |
|---------|-----|
| No event when lights are turned on/off | Decrease `LIGHT_CHANGE_THRESHOLD` (30 → 15) |
| Too sensitive (triggers from lamp dimming slowly) | Increase `LIGHT_CHANGE_THRESHOLD` (30 → 50) |
| Events too frequent | Increase `LIGHT_LEVEL_INTERVAL_S` |

---

## Face Detection

**File:** `os/hal/config.py`

```python
FACE_AREA_RATIO_THRESHOLD = 0.05  # Skip faces larger than 5% of frame area
FACE_COOLDOWN_S = 10.0            # Min seconds between face presence events
FACE_OWNER_FORGET_S = 3600.0      # Re-fire presence after N seconds without seeing owner
FACE_STRANGER_FORGET_S = 1800.0   # Same for strangers
```

The area ratio threshold filters out faces that are **too small** relative to the frame — typically distant people or false positives where the face crop is too low-resolution for reliable recognition. Faces covering less than the threshold fraction of the total frame area are skipped.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Distant people not recognized | Decrease `FACE_AREA_RATIO_THRESHOLD` (0.05 → 0.02) |
| False detections from tiny face-like patches | Increase `FACE_AREA_RATIO_THRESHOLD` (0.05 → 0.1) |
| Presence events fire too often | Increase `FACE_COOLDOWN_S` (10 → 30) |
| Lamp forgets owner too quickly after leaving | Increase `FACE_OWNER_FORGET_S` |

---

## Per-Face Motion Detection

**File:** `os/hal/config.py`

```python
MOTION_PER_FACE_ENABLED = false            # Enable per-face action recognition
MOTION_PER_FACE_DEDUP_WINDOW_S = 300.0     # Per-action dedup window (5 min)
MOTION_PER_FACE_SESSION_TTL_S = 30.0       # Evict face session after this long unseen
MOTION_PER_FACE_MIN_FRAMES = 4             # Min frames before first event fires
```

Per-face motion opens a separate WS session per detected face and runs action recognition on an expanded face crop. Each action is deduped independently per face.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Too many events per person | Increase `MOTION_PER_FACE_DEDUP_WINDOW_S` (300 → 600) |
| Noisy single-frame classifications | Increase `MOTION_PER_FACE_MIN_FRAMES` (4 → 8) |
| Sessions accumulate for briefly-seen faces | Decrease `MOTION_PER_FACE_SESSION_TTL_S` (30 → 15) |
| WS connections pile up in multi-person scenes | Disable with `MOTION_PER_FACE_ENABLED=false` |

---

## Speech Emotion Recognition (SER)

**Files:** `os/hal/config.py`, `os/hal/drivers/voice/voice_service.py` (`_submit_speech_emotion_from_session`, `_identify_and_decorate`, `_session_wav_for_ser`) — see also [Speech Emotion Recognition](speech-emotion.md) for the full architecture. **Vietnamese:** [docs/vi/sensing-tuning_vi.md](vi/sensing-tuning_vi.md) (SER section), [speech-emotion_vi.md](vi/speech-emotion_vi.md).

**Voice integration (session end, transcript-independent):** in the `finally` block of every mic session (VAD trigger → ~2.5 s silence), `_stream_session` runs `_identify_and_decorate(final_text, audio_buffer)` **once** to resolve both `final_msg` (for the Lamp POST when STT had text) and `user_name` (for the SER submit). The result is passed to `_submit_speech_emotion_from_session(audio_buffer, user=...)`, which builds the WAV and calls `SpeechEmotionService.submit`. Unknown / no-match speakers still enqueue SER under the shared `unknown` dedup key when audio is long enough.

```python
SPEECH_EMOTION_ENABLED = True
SPEECH_EMOTION_FLUSH_S = 10.0               # Per-user buffer drain cadence
SPEECH_EMOTION_DEDUP_WINDOW_S = 300.0       # (user, bucket) TTL — 5 min
SPEECH_EMOTION_MIN_AUDIO_S = 3.0            # Skip utterances shorter than this (hal.config default)
SPEECH_EMOTION_API_TIMEOUT_S = 15           # dlbackend HTTP timeout
DL_SER_ENDPOINT = "/lelamp/api/dl/ser/recognize"
```

Per-label confidence thresholds are **not** in `config.py` — they live in `os/hal/drivers/voice/speech_emotion/constants.py` as `CONFIDENCE_THRESHOLD_BY_LABEL` (and `DEFAULT_CONFIDENCE_THRESHOLD` for unmapped labels). Negative emotions are gated higher than positive ones to suppress false-positive alarms:

```python
# constants.py
CONFIDENCE_THRESHOLD_BY_LABEL = {
    "happy":     0.5,
    "surprised": 0.6,
    "sad":       0.6,
    "angry":     0.6,
    "fearful":   0.7,
    "disgusted": 0.7,
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
```

Tune by editing the dict directly — no env override.

**How to read the log:**

The service tags every line `[speech_emotion]`:

```
INFO lelamp.voice.speech_emotion: [speech_emotion] buffered: alice -> sad (0.72, 2.40s)
INFO lelamp.voice.speech_emotion: [speech_emotion] flushing alice: Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; ...) (mode of sad, fearful, sad)
INFO lelamp.voice.speech_emotion: [speech_emotion] sent to Lamp: Speech emotion detected: Sad. ...
INFO lelamp.voice.speech_emotion: [speech_emotion] dedup drop: angry bucket=negative (key seen 87.4s ago)
```

The `flushing` line shows the raw label list — that's the mode-over-samples that produced the dominant label.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Same-bucket events fire too often | Increase `SPEECH_EMOTION_DEDUP_WINDOW_S` (300 → 600) |
| Single-utterance noisy reads slip through | Raise the offending label's entry in `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) — e.g. nudge `"sad": 0.6 → 0.7`. Bump `DEFAULT_CONFIDENCE_THRESHOLD` only if the noise is across the board |
| Short "yeah" / "ok" utterances flagged | Increase `SPEECH_EMOTION_MIN_AUDIO_S` (3.0 → 4.0) |
| Mood lag — Lamp too slow to react after a real shift | Decrease `SPEECH_EMOTION_FLUSH_S` (10 → 5) |
| Worker queue full warnings in log | Investigate dlbackend latency; raising queue size is not enough — backlog means something downstream is wedged |
| Too many `speech_emotion.detected` for strangers | Expected: unknown speakers use `user="unknown"`; tighten the per-label entry in `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) or dedup window — do not disable SER solely because Lamp transcript says `Unknown Speaker:` |

---

## Apply Changes

After editing `os/hal/config.py` or `voice_service.py` on the Pi:

```bash
sudo systemctl restart lelamp
tail -f /var/log/hal/server.log
```

No reboot needed — just restart the service.
