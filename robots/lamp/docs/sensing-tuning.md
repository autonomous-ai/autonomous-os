# Sensing Threshold Tuning Guide

How to tune Lamp's sensing thresholds on real hardware.
All constants live in `hal/config.py` and `hal/drivers/voice/voice_service.py`.

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

`MotionPerception` runs Kinetics action recognition (via perception-service) and emits a
`motion.activity` event with the recognized activity labels.

**File:** `hal/config.py`

```python
MOTION_CONFIDENCE_THRESHOLD = 0.3    # min action-recognition confidence to buffer a label
MOTION_FLUSH_S = 10.0                # buffer drain cadence — at most one flush per 10s
MOTION_EVENT_COOLDOWN_S = 900.0      # same-class heartbeat floor between emissions (15 min)
MOTION_TRANSITION_MIN_GAP_S = 60.0   # min gap for the class-transition cooldown bypass
```

**Emission gates (in order, `motion.py`):**

1. **Flush cadence** — buffered detections are drained at most once per `MOTION_FLUSH_S`.
2. **Presence gate** — no event unless presence == PRESENT.
3. **Global cooldown** — no `motion.activity` more than once per `MOTION_EVENT_COOLDOWN_S`
   while the **coarse activity class** (the `ACTIVITY_GROUP` set: sedentary/eat/drink/…)
   stays the same. Same-class raw-label flips (`writing → drawing`) stay floored — that's
   noise. Bypassed by: a **class transition** (`computer → eat` is real information,
   emitted as soon as `MOTION_TRANSITION_MIN_GAP_S` has passed, which stops a flickering
   detection from re-opening every-flush spam), a posture nudge (already time-gated by the
   pose window), and a user change (a new user/session sees a fresh event immediately).
4. **Per-label dedup** — even when cooldown is cleared, the same `(user, label-set)`
   within a 5-min window is dropped. Noisy Kinetics labels flip the set often, so the
   global cooldown above is the dominant gate.

**How to read the log:**

```
INFO hal...motion: [motion] raw actions in window: ['writing', 'typing']
INFO hal...motion: [motion] flushing: Activity detected: writing.
INFO hal...motion: [motion] cooldown drop: ... (last event 42.1s ago < 900s floor, class unchanged)
INFO hal...motion: [motion] transition bypass: ['sedentary'] → ['eat'] (last event 312.4s ago)
```

**Tuning:**

| Symptom | Fix |
|---------|-----|
| `motion.activity` fires constantly (every ~10s) | Increase `MOTION_EVENT_COOLDOWN_S` — this is the same-class floor |
| Repeated events from a blinking detection (drink in/out of frame) | Increase `MOTION_TRANSITION_MIN_GAP_S` (60 → 120+) |
| Activity not picked up at all | Decrease `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.2) |
| Spurious activity labels | Increase `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.4) |
| Reaction lags a real activity change | Decrease `MOTION_FLUSH_S` (10 → 5) and/or `MOTION_TRANSITION_MIN_GAP_S` — class changes already bypass the cooldown |

---

## Sound Detection (Sensing)

**File:** `hal/config.py`

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

**File:** `hal/drivers/voice/voice_service.py` (all env-tunable)

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

**File:** `hal/config.py`

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

**File:** `hal/config.py`

```python
FACE_HEIGHT_RATIO_THRESHOLD = 0.10  # Skip faces shorter than 10% of frame height
FACE_MAX_TRUNCATION = 0.05          # Skip faces with >5% of their bbox off-frame
FACE_COOLDOWN_S = 10.0              # Min seconds between face presence events
FACE_OWNER_FORGET_S = 3600.0        # Re-fire presence after N seconds without seeing owner
FACE_STRANGER_FORGET_S = 1800.0     # Same for strangers
```

The height ratio threshold filters out faces that are **too small** relative to the frame — typically distant people or false positives where the face crop is too low-resolution for reliable recognition. Faces whose bounding-box height is under the threshold fraction of the frame height are skipped, before classification.

**Why height and not area.** Area falls off as 1/d² while a linear dimension falls off as 1/d, so an area gate is twice as sensitive for the same change in reach — extending range from 0.8 m to 1.5 m needs a 3.8× change in an area threshold but only a 1.95× change in a height threshold. More importantly, yaw (turning the head, the common case) compresses the bbox **width** while leaving height intact, so an area gate rejected angled faces harder than frontal ones at the same distance — working against the extended-set feature that exists to learn those angled views.

**Truncation.** `FACE_MAX_TRUNCATION` is a separate gate for faces clipped by a frame edge, applied after the height gate and before classification. A clipped face is not a smaller face — it is a face missing features. SCRFD still returns a plausible box (the clipped edge simply runs off-frame, e.g. `[573, -34, 710, 121]`), and the landmark mesh confidently invents the part it cannot see. Measured on a lamp on 2026-09-03: a face cut off above the eyebrows produced eye landmarks hallucinated onto the cheeks at a landmark confidence of **0.90**, an embedding sharing **0.007** similarity with that person's own enrollment photo, and a FRIEND verdict carried entirely by the auto-captured extended bank.

The landmark-in-bbox check inside the aligner does **not** catch this: it clamps the bbox to the frame before comparing, so a landmark can never be "outside" on the very edge that clipped the face. The gate measures the fraction of the bbox **area** falling outside the frame. Rejected faces also never reach `extend_candidates`, so a cut-off view can never be auto-added to a user's extended enrollment set.

**Why 0.05.** Replaying 496 logged frames from a lamp (2026-09-04): every well-recognised frame sat at 0% overflow (median similarity to the enrollment photo 0.66), the 0.1–3% band at 0.56, and the **5–10% band collapsed to 0.32**. One frame clipped by 6.1% minted a spurious `stranger_N` identity, and three later frames then matched *that* identity — so a single clipped frame produced four misidentifications. Dropping the threshold from 0.10 to 0.05 removes all four; the cost is three frames out of 492 that recognise correctly today and are instead skipped.

**What it does not catch.** The gate only sees clipping the detector admits to by returning an off-frame box (e.g. `y1 = -34`). SCRFD sometimes clamps to the edge instead (`y1 = 0` exactly), which measures 0% overflow and passes even though the face is genuinely cut. Treating "bbox touches the frame edge" as clipped was measured and rejected: it would drop 25 frames to catch 2, because 23 edge-touching frames are recognised correctly.

**Reach.** At 640×480 with a ~65° horizontal FOV, 0.10 corresponds to a 48 px face box at roughly 2.2 m. Note the gate is scale-invariant: raising `HAL_CAMERA_WIDTH`/`HEIGHT` does not change which faces pass, but it does raise the pixel quality of the crop handed to the recognizer (EdgeFace warps to 112×112, and SCRFD returns bboxes in original-frame coordinates, so the crop comes from the full-resolution frame). At 1280×720 the same 0.10 yields a 72 px crop instead of 48 px.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Distant people not recognized | Decrease `FACE_HEIGHT_RATIO_THRESHOLD` (0.10 → 0.07) |
| False detections from tiny face-like patches | Increase `FACE_HEIGHT_RATIO_THRESHOLD` (0.10 → 0.15) |
| Recognition flickers / mints new `stranger_N` ids repeatedly | Crop is too small to embed reliably — increase `FACE_HEIGHT_RATIO_THRESHOLD`, or raise camera resolution to 1280×720 |
| Wrong person matched when someone sits close to a frame edge | Face is clipped — decrease `FACE_MAX_TRUNCATION` (0.05 → 0.03), or re-aim the camera so heads stay fully in frame |
| People at the frame edge stop being recognized at all | Increase `FACE_MAX_TRUNCATION` (0.05 → 0.10); check for `FAIL-truncated` folders in the face debug log to see how much was actually cut |
| Presence events fire too often | Increase `FACE_COOLDOWN_S` (10 → 30) |
| Lamp forgets owner too quickly after leaving | Increase `FACE_OWNER_FORGET_S` |

---

## Per-Face Motion Detection

**File:** `hal/config.py`

```python
MOTION_PER_FACE_ENABLED = false            # Enable per-face action recognition
MOTION_PER_FACE_DEDUP_WINDOW_S = 300.0     # Per-action dedup window (5 min)
MOTION_PER_FACE_SESSION_TTL_S = 30.0       # Evict face session after this long unseen
MOTION_PER_FACE_MIN_FRAMES = 4             # Min frames before first event fires
```

Per-face motion opens a separate WS session per detected face and runs action recognition on an expanded face crop. Each action is deduped independently per face. On top of the per-face dedup, ONE global cooldown floor is shared across all faces — same semantics and same knobs as regular motion (`MOTION_EVENT_COOLDOWN_S` same-class floor, `MOTION_TRANSITION_MIN_GAP_S` min gap on the class-transition bypass, floor cleared on a real user change) — so N faces in frame still produce at most one same-class `motion.activity` per cooldown, not N.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Too many events per person | Increase `MOTION_PER_FACE_DEDUP_WINDOW_S` (300 → 600) |
| Too many events across people | Increase `MOTION_EVENT_COOLDOWN_S` — the global floor is shared with regular motion |
| Noisy single-frame classifications | Increase `MOTION_PER_FACE_MIN_FRAMES` (4 → 8) |
| Sessions accumulate for briefly-seen faces | Decrease `MOTION_PER_FACE_SESSION_TTL_S` (30 → 15) |
| WS connections pile up in multi-person scenes | Disable with `MOTION_PER_FACE_ENABLED=false` |

---

## Speech Emotion Recognition (SER)

**Files:** `hal/config.py`, `hal/drivers/voice/voice_service.py` (`_submit_speech_emotion_from_session`, `_identify_and_decorate`, `_session_wav_for_ser`) — see also [Speech Emotion Recognition](../../../docs/speech-emotion.md) for the full architecture. **Vietnamese:** [docs/vi/sensing-tuning_vi.md](vi/sensing-tuning_vi.md) (SER section), [speech-emotion_vi.md](../../../docs/vi/speech-emotion_vi.md).

**Voice integration (session end, transcript-independent):** in the `finally` block of every mic session (VAD trigger → ~2.5 s silence), `_stream_session` runs `_identify_and_decorate(final_text, audio_buffer)` **once** to resolve both `final_msg` (for the Lamp POST when STT had text) and `user_name` (for the SER submit). The result is passed to `_submit_speech_emotion_from_session(audio_buffer, user=...)`, which builds the WAV and calls `SpeechEmotionService.submit`. Unknown / no-match speakers still enqueue SER under the shared `unknown` dedup key when audio is long enough.

```python
SPEECH_EMOTION_ENABLED = True
SPEECH_EMOTION_FLUSH_S = 10.0               # Per-user buffer drain cadence
SPEECH_EMOTION_DEDUP_WINDOW_S = 300.0       # (user, bucket) TTL — 5 min
SPEECH_EMOTION_MIN_AUDIO_S = 3.0            # Skip utterances shorter than this (hal.config default)
SPEECH_EMOTION_API_TIMEOUT_S = 15           # perception-service HTTP timeout
DL_SER_ENDPOINT = "/lelamp/api/dl/ser/recognize"
```

Per-label confidence thresholds are **not** in `config.py` — they live in `hal/drivers/voice/speech_emotion/constants.py` as `CONFIDENCE_THRESHOLD_BY_LABEL` (and `DEFAULT_CONFIDENCE_THRESHOLD` for unmapped labels). Negative emotions are gated higher than positive ones to suppress false-positive alarms:

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
| Worker queue full warnings in log | Investigate perception-service latency; raising queue size is not enough — backlog means something downstream is wedged |
| Too many `speech_emotion.detected` for strangers | Expected: unknown speakers use `user="unknown"`; tighten the per-label entry in `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) or dedup window — do not disable SER solely because Lamp transcript says `Unknown Speaker:` |

---

## Apply Changes

After editing `hal/config.py` or `voice_service.py` on the Pi:

```bash
sudo systemctl restart lelamp
tail -f /var/log/hal/server.log
```

No reboot needed — just restart the service.
