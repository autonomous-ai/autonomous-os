"""Recording playback timing — the one place that decides when a frame plays.

Both motion drivers replay the same shipped CSVs: the SDK-backed
``AnimationService`` on a physical body, and ``MockMotionService`` on a laptop.
If they disagreed about timing the simulator would be lying about the thing it
exists to show, so the stretch-and-resample rule lives here and both call it.

See ``robots/lamp/docs/motion-playback.md`` for the measurements behind the
speed ceiling.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hal.motion.timing")

# Peak joint speed the STS3215 can actually deliver, in degrees/second.
# Measured on device: recordings commanding >500 deg/s leave the servo 55 deg
# behind its goal — it saturates, lags, then snaps, which is the audible
# grinding. Recordings are resampled so no segment exceeds this; segments that
# would are stretched in time instead. Set HAL_SERVO_MAX_DPS=0 to disable
# stretching and play recordings at their authored speed.
SERVO_MAX_DPS = float(os.environ.get("HAL_SERVO_MAX_DPS", "250"))

def effective_max_dps(policy: Any = None) -> float:
    """The speed ceiling a replay must respect: hardware limit, then policy.

    ``SERVO_MAX_DPS`` is what the servo can physically deliver; SAFETY.md's
    ``motion.max_speed`` is what the body promises it will not exceed. A replay
    is bound by whichever is lower, so a declared ceiling covers recordings the
    same way it already covers move/aim/nudge (#219).
    """
    if SERVO_MAX_DPS <= 0 or policy is None:
        return SERVO_MAX_DPS
    from hal.safety.policy import cap_speed_dps

    return cap_speed_dps(policy, SERVO_MAX_DPS)


# Recordings are authored at ~20 Hz but a playback loop steps one frame per
# tick at its own fps, so raw frames play at the wrong wall-clock speed. Frames
# are resampled onto that grid at load time; this is the CSV column that
# carries the authored timing.
RECORDING_TIME_COLUMN = "timestamp"


def stretch_timeline(
    times: List[float], frames: List[Dict[str, float]], policy: Any = None
) -> List[float]:
    """Widen the gaps that demand more joint speed than the servo can deliver.

    Returns a new, still-monotonic time axis. Only over-speed segments grow;
    everything else keeps its authored timing, so a recording slows down
    exactly where it was impossible and nowhere else.
    """
    max_dps = effective_max_dps(policy)
    if max_dps <= 0:
        return times

    out = [times[0]]
    for i in range(1, len(frames)):
        authored_dt = max(times[i] - times[i - 1], 1e-3)
        peak_delta = max(
            (abs(frames[i][j] - frames[i - 1][j]) for j in frames[i]),
            default=0.0,
        )
        needed_dt = peak_delta / max_dps
        out.append(out[-1] + max(authored_dt, needed_dt))
    return out


def resample_recording(
    times: List[float],
    frames: List[Dict[str, float]],
    name: str,
    fps: float,
    policy: Any = None,
) -> List[Dict[str, float]]:
    """Put frames on a playback loop's own 1/fps grid.

    The loop steps exactly one frame per tick, so a list sampled at fps plays at
    real time by construction — no timing logic in the hot path.
    """
    stretched = stretch_timeline(times, frames, policy)
    duration = stretched[-1] - stretched[0]
    if duration <= 0:
        return frames

    joints = list(frames[0].keys())
    step = 1.0 / fps
    total = max(1, int(round(duration / step)))

    out: List[Dict[str, float]] = []
    src = 0
    for k in range(total + 1):
        t = stretched[0] + min(k * step, duration)
        # stretched[] is monotonic and t only advances, so this walk is O(n).
        while src < len(stretched) - 2 and stretched[src + 1] < t:
            src += 1
        span = stretched[src + 1] - stretched[src]
        p = 0.0 if span <= 0 else (t - stretched[src]) / span
        p = max(0.0, min(1.0, p))
        a, b = frames[src], frames[src + 1]
        out.append({j: a[j] + (b[j] - a[j]) * p for j in joints})

    authored = times[-1] - times[0]
    max_dps = effective_max_dps(policy)
    if max_dps > 0 and duration > authored * 1.01:
        logger.info(
            "recording %r stretched %.2fs -> %.2fs to stay under %.0f deg/s",
            name, authored, duration, max_dps,
        )
    return out
