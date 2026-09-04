# Motion Playback

How servo recordings in `hal/recordings/` become motion. Source:
`hal/drivers/motors/animation_service.py`, with the timing rule itself in
`hal/drivers/motors/recording_timing.py`.

## Timing

Recordings in `hal/recordings/` are teleop captures authored at **20 Hz** (`timestamp` column, 0.05s apart). The playback loop in `hal/drivers/motors/animation_service.py` steps exactly **one frame per tick** at `self.fps` (30, `HAL_SERVO_FPS`), so raw frames would play at the wrong speed. `_load_recording` therefore resamples every recording onto the loop's own 1/fps grid at load time, and playback itself stays a plain frame-per-tick walk — no timing logic in the hot path.

Resampling does two things:

- **Honors the authored `timestamp`**, so a recording lasts as long as it was recorded to last. Stepping 20 Hz frames at 30 Hz previously played every animation 1.5× too fast.
- **Stretches segments that exceed the speed ceiling** — the lower of `SERVO_MAX_DPS` (250 deg/s, `HAL_SERVO_MAX_DPS`; set to 0 to disable) and the body's declared `motion.max_speed`. The STS3215 tops out around 270 deg/s, and several recordings were authored well past that; on Lamp the declared 120 deg/s is the binding one, and it stretches `laugh` (+20%), `playful`, `headshake` and `acknowledge`. No `music_*` recording is affected, so grooves keep their timing.

The cap is not cosmetic. Measured on lamp-0c89 by sampling `Present_Position` while playing `greeting`: commanding 554 deg/s left the servo **55° behind its goal** — it saturates, lags, then snaps, which is the audible grinding. Stretching is surgical, applied only to the segments that were impossible:

| recording | authored | peak demanded | after resample | stretch |
|---|---|---|---|---|
| `greeting` | 2.95s | 554 deg/s | 3.49s | +18% |
| `happy_wiggle` | 7.95s | 476 deg/s | 8.27s | +4% |
| `nod` | 1.95s | 302 deg/s | 2.00s | +2.6% |
| `idle` | 9.95s | 115 deg/s | 9.97s | none |

`idle` is already within budget, so it is not stretched at all — but it gained the most, because the loop no longer drops to 5 Hz once idle settles. That reduction stepped fps-grid frames six times too slowly and delivered breathing as five visible jerks per second; it cost more in smoothness than it saved in CPU.

The same rule runs off-hardware. `MockMotionService` (`HAL_BOARD=sim`, the `make sim` laptop body) imports `resample_recording` from the same module and replays on the same 30 Hz grid, so a recording takes the same wall-clock time in the simulator as on a body. Its `move_to`/`aim`/`nudge` interpolate over the commanded `duration` and block until arrival, as the SDK-backed driver does. What the simulator still does not model: inertia, collision, torque, and per-unit EEPROM calibration.

Two consequences worth knowing:

- Servo motion registers are left at their defaults (`Acceleration=254`, `Goal_Velocity=0`). Capping speed in the servo instead of in the trajectory was measured and rejected: it cut jerk ~33% but pushed tracking error from 55° to 71°, and the resulting lag walked the arm into a mechanical jam. Software knows the whole trajectory ahead of time and can stretch it under control; the servo can only lag.
- `add_recording()` (upload path) **invalidates** the cache rather than filling it. It receives frames already stripped of `timestamp`, so caching them would bypass resampling — the uploaded copy would play at raw frame rate while the identical file read from disk played correctly.
