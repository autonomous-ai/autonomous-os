"""Tuning knobs shared across the tracking package.

Detector-internal knobs (COCO map, endpoints, model paths) live in
detection.py / vit_tracker.py next to the code that uses them; everything
here is either shared between modules or a behavior dial worth finding in
one place.
"""

from hal.config import TRACKING_MAX_DURATION_S

# Vision-pipeline max width (px). The ViT tracker AND the detectors (YuNet /
# local YOLO / remote YOLOWorld) run on a frame downscaled to at most this
# width; every bbox they return is mapped back to ORIGINAL camera coordinates
# before any servo/PID math, so no pixel-tuned constant (PID gains, area/center
# gates, dead zones, feedforward thresholds) needs re-tuning. Camera runs
# 1280x720, so 640 = 0.5x → ¼ the pixels for the ViT crop/resize and detector
# input → faster fast-loop → smoother tracking. Set to 0/None to disable, or a
# width ≥ the camera width for a no-op.
VISION_MAX_WIDTH = 640

# Fast loop target FPS — tracker update on Pi runs ~15-25ms/frame. Servo
# commands are decoupled: the follower wakes at ~33 Hz and coalesces tiny
# setpoint changes, so loop fps no longer directly sets bus-write/click rate;
# it only sets how often the goal/velocity estimate refreshes. 15 gives finer
# velocity estimates and faster reaction on quick targets at ~+50% ViT CPU
# (~20ms × 15 = 0.3 core).
FAST_LOOP_FPS = 15

# Hardware velocity limit for tracking (Feetech STS3215 Goal_Velocity register,
# unit steps/s on a 4096-step revolution → 150 ≈ 13°/s!).
# 0 = unlimited: the SmoothDamp profiles below own the speed envelope entirely.
# The old 150 flattened every software ease curve into a constant ~13°/s crawl
# — motion looked robotic AND fast targets were untrackable. The hardware
# Acceleration ramp (below) still softens each tick.
TRACKING_GOAL_VELOCITY = 0
# Hardware acceleration for tracking (Feetech STS3215 Acceleration register).
# 254 = max (default, snappy). Lower = gentler ramp up/down → less jerk.
# Range: 0-254. ~30 gives smooth glide without being too sluggish.
TRACKING_ACCELERATION = 30

# Camera field-of-view in degrees (horizontal). Used to convert px offset → degrees.
CAMERA_FOV_DEG = 60.0

# Tiered dead zone (per-axis, as fraction of frame).
# Inside ±INNER: true zero — servo rests, PID integral clears (CENTERED).
# INNER→OUTER: "creep band" — a gentle CREEP_GAIN slope drifts the camera
# lazily toward center instead of freezing dead at the boundary. The old hard
# stop at the dead-zone edge produced the start-stop "security camera" feel;
# a human operator never fully freezes, they drift. Beyond OUTER: full error
# (continuous at both boundaries, no output step).
# Yaw outer larger — horizontal jitter is common, small dx not worth a chase.
# Pitch outer smaller — vertical needs finer response for elbow tracking.
DEAD_ZONE_INNER_PCT = 0.02
DEAD_ZONE_YAW_PCT   = 0.07
DEAD_ZONE_PITCH_PCT = 0.05
DEAD_ZONE_CREEP_GAIN = 0.12

# --- Alpha-beta (constant-velocity) filter on the target centroid ---
# Steady-state Kalman for a constant-velocity model. Replaces the plain EMA so
# the servo follows a *predicted, gated* centroid instead of the raw ViT bbox
# center: it smooths jitter, coasts through dropped/garbage frames, and leads a
# moving target to cut lag. ALPHA = position correction (higher = snappier,
# noisier), BETA = velocity correction (higher = faster to track accel, more
# overshoot). GATE_PX rejects a measurement whose residual-from-prediction
# exceeds it — a ViT-bloat teleport or false detection — by coasting on the
# prediction. Because the gate is on residual (not raw jump), sustained fast
# motion is NOT gated (velocity tracks it); only sudden unexplained jumps are.
# LEAD_S projects the centroid forward by this many seconds (velocity
# feedforward) to anticipate motion; 0 = no lead.
AB_ALPHA = 0.6
AB_BETA = 0.2
AB_GATE_PX = 200.0
# Raised 0.12→0.20: with pursuit control the camera pans at the target's
# speed, so a bigger lead buys cinematic "lead room" ahead of the subject.
AB_LEAD_S = 0.20
# Velocity decay applied when a measurement is gated, so a persistent bad lock
# coasts to a stop instead of running away on stale velocity.
AB_GATE_DECAY = 0.7
# Consecutive gated frames after which the filter force-accepts the measurement
# (re-seeds). Stops a genuine fast move from being rejected forever; transient
# ViT-bloat teleports last only 1–2 frames so they're still filtered out.
AB_MAX_GATED_STREAK = 3

# YOLO background re-detect interval (seconds).
# Local YOLOv8n runs ~300-700ms/call on Allwinner A523. At 500ms interval
# it saturated all CPU cores → camera MJPEG stream stalled.
# 1.5s gives the CPU breathing room while still catching tracker drift.
YOLO_REDETECT_S = 1.5

# How many consecutive tracker-update miss frames before retrying.
# Raised: ViT honestly returns ok=False on transient low-confidence frames.
YOLO_MAX_MISS = 30

# Miss-coast: when ViT misses while the target was MOVING (fast phone wave,
# motion blur), keep panning along the target's last alpha-beta velocity for
# up to this many miss frames before falling back to the search sweep. A fast
# target outruns ViT for a beat; panning ahead re-catches it, while stopping
# dead (old behavior: immediate sweep) guarantees it exits the frame.
MISS_COAST_FRAMES = 6

# Cooldown after servo fire (seconds) — ignore motion detection while camera
# stabilises after a move. Prevents servo shake → fake MOVE → immediate re-fire loop.
SERVO_COOLDOWN_S = 0.10

# Servo-worker wake interval. SmoothDamp uses measured monotonic elapsed time;
# commands smaller than SERVO_COMMAND_MIN_DELTA are coalesced, so a wake does
# not necessarily produce a bus write.
SERVO_SUBSTEP_SLEEP = 0.030
# Clamp a delayed worker iteration so a USB/serial stall cannot be turned into
# one oversized SmoothDamp step when the loop resumes. 60 ms caps one resumed
# command at two nominal 30 ms ticks; normal operation uses measured monotonic
# time, not SERVO_SUBSTEP_SLEEP.
SERVO_SUBSTEP_MAX_DT_S = 0.060
# Coalesce only very small normalized setpoint changes. Keep this below the
# near-centre correction size so the follower retains fine control instead of
# suppressing genuine movement around the dead zone.
SERVO_COMMAND_MIN_DELTA = 0.08

# --- SmoothDamp follower (cinematic ease-in/ease-out) ---
# The servo worker used to step a FIXED number of degrees toward the goal each
# tick, then snap the last step. That makes the commanded velocity a square wave
# (0 → ~50°/s instantly → 0 instantly) every time the goal changes at ~10 Hz —
# the "jerky, not smooth like a film camera" feel. SmoothDamp (Game Programming
# Gems 4 / Unity's Mathf.SmoothDamp) is a critically-damped follower: it carries
# an internal per-joint velocity so every move accelerates smoothly and eases out
# into the target, and when a fresh goal arrives mid-move the velocity carries
# over (no restart jerk). Coalesced commands avoid redundant click/buzz from
# tiny intermediate setpoints.
# SMOOTH_TIME = approximate seconds to reach the target: higher = smoother but
# laggier; tune on-device. MAX_SPEED_DPS caps peak pan speed (deg/s) so a big
# offset can't whip the camera and lose ViT lock (the hardware Goal_Velocity is
# the real ceiling; this keeps the software setpoint tame too).
#
# Two profiles, mirroring how human gaze works: PURSUIT (small error) is heavy
# and slow like a fluid-head film camera — velocity feedforward does the work,
# the follower just adds inertia. SACCADE (error > SACCADE_OFFSET_FRAC of the
# frame) relocates fast with a shorter smooth time, then hands back to pursuit.
# One compromise profile did both badly: snappy enough to catch up = twitchy
# when centered.
SERVO_SMOOTH_TIME   = 0.32   # pursuit
SERVO_MAX_SPEED_DPS = 55.0   # pursuit
SACCADE_SMOOTH_TIME   = 0.20
SACCADE_MAX_SPEED_DPS = 100.0
# Profile switch with hysteresis: enter saccade when the offset exceeds
# SACCADE_OFFSET_FRAC of frame width, drop back to pursuit only below
# SACCADE_EXIT_FRAC. Without the gap, an offset hovering at the boundary
# flip-flopped the speed cap 55↔100 every frame — visible speed wobble.
SACCADE_OFFSET_FRAC = 0.22
SACCADE_EXIT_FRAC = 0.12

# Pitch distribution across 3 joints — the PREFERENCE, not the whole story.
# `servo_follow.distribute_pitch` spends these weights first and then hands
# whatever a saturated joint could not absorb to any joint that still has room,
# so a weight of 0.0 means "not first choice", not "never".
#
# (The comment that used to sit here said "use wrist alone for predictable pitch
# control", which had not matched PITCH_WEIGHT_WRIST = 0.0 for some time. The
# device disagrees with it too — see PITCH_TRAVEL_* below.)
# elbow_pitch still leads, because on a healthy arm it is the joint that
# produces most of the vertical movement — device-measured with base and wrist
# pinned, elbow +1.6 framed the desk and +54.8 framed the ceiling.
#
# It no longer takes almost all of it. The elbow on lamp-ac82 is intermittently
# unresponsive (a hardware fault, not a tuning one): it accepts a goal, reports
# no error, and simply does not move, then works again later. At 0.90 that took
# 90% of every correction with it — device-observed, a 15 deg climb step
# delivering 1.5 deg because only base_pitch's share arrived.
#
# Spreading the remainder over both other joints keeps the loop useful while the
# elbow is out. The landing check already benches a joint that fails to arrive
# and re-routes the next correction, so this is about not depending on it in the
# first place rather than about detecting the fault.
PITCH_WEIGHT_BASE  = 0.20
PITCH_WEIGHT_ELBOW = 0.60
PITCH_WEIGHT_WRIST = 0.20

# Travel each pitch joint actually has, measured on lamp-ac82 2026-08-25 by
# commanding each joint alone and reading the position error `/servo/move`
# reports. Nothing here is a software clamp — `clamped` came back equal to
# `requested` every time; these are where the arm stops.
#
#   base_pitch    -20 .. +30   (-30 stalled at -17.4)
#   elbow_pitch    -5 .. +60   (-15 stalled at  -5.2)
#   wrist_pitch    -35 .. +33  (-50 stalled at -34.8, +40 reached +32.9)
#
# The limits matter because they are wildly asymmetric and the wide MIN/MAX
# below hide it. Looking up drives wrist NEGATIVE, and idle rests it near -32 —
# roughly 2 degrees short of its stop. Gaze used to spend its entire correction
# there, so the servo reported `position error 14.6 deg` and the head never
# moved. Allocating against real travel is what stops that being possible.
#
# Held a margin inside the measured stall so a correction stops just short of
# pushing, rather than stalling the motor against the end of its travel.
# Per-unit, and configuration-dependent (base reached -20 only once elbow was
# high), so treat these as conservative rather than exact.
PITCH_TRAVEL_MIN = {
    "base_pitch.pos":  -18.0,
    "elbow_pitch.pos":  -4.0,
    "wrist_pitch.pos": -33.0,
}
# Measured pan travel, same day and the same way as PITCH_TRAVEL_*. wrist_roll
# reached every target from -59 to +59 cleanly — markedly better behaved than
# any pitch joint, because neither of these two lifts the arm against gravity.
YAW_TRAVEL_MIN = {
    "base_yaw.pos":   -100.0,
    "wrist_roll.pos":  -55.0,
}
YAW_TRAVEL_MAX = {
    "base_yaw.pos":    100.0,
    "wrist_roll.pos":   55.0,
}
PITCH_TRAVEL_MAX = {
    "base_pitch.pos":   30.0,
    "elbow_pitch.pos":  58.0,
    "wrist_pitch.pos":  32.0,
}

# Elbow servo polarity. The elbow_pitch motor's positive direction was reversed
# in hardware (2026-06-19), so a positive pitch_correction now drives the camera
# the opposite way. Flip the elbow contribution by this sign so the camera still
# chases dy in the correct direction. Set back to +1 if the wiring is restored.
ELBOW_PITCH_SIGN = -1.0

# Maximum tracking duration (seconds) — auto-stop to save motor/CPU. The
# per-device value comes from HAL_TRACKING_MAX_DURATION_S at HAL startup.
MAX_TRACK_DURATION_S = TRACKING_MAX_DURATION_S

# Yaw distribution across the two joints that PAN the camera.
#
# Device-measured 2026-08-25 by pinning every other joint and capturing:
#   base_yaw   -24 -> face at the far right of frame;  +24 -> centre-left
#   wrist_roll -34 -> face at the far right of frame;  +34 -> left
# So INCREASING either joint pans the camera right, and a face on the right
# (dx > 0) is corrected by increasing both. Same sign, no ELBOW_PITCH_SIGN
# equivalent needed.
#
# base_yaw leads for two reasons beyond its larger travel: turning the base is
# the gesture people read as "it looked at me", and `user_bearing` stores the
# bearing AS base_yaw — so aiming mostly with the wrist would leave the
# remembered bearing describing a pose the lamp never actually held.
# wrist_roll assists, and picks up whatever a saturated yaw cannot take.
#
# Unlike the pitch joints these two are on nearly the same scale — 12.0 vs 11.5
# encoder counts per normalised unit — so treating their contributions as 1:1
# is sound here in a way it is not for pitch.
YAW_WEIGHT_BASE = 0.75
YAW_WEIGHT_ROLL = 0.25

# Servo position limits (degrees).
YAW_MIN, YAW_MAX = -135.0, 135.0
WRIST_ROLL_MIN, WRIST_ROLL_MAX = -90.0, 90.0
BASE_PITCH_MIN, BASE_PITCH_MAX = -90.0, 30.0
ELBOW_PITCH_MIN, ELBOW_PITCH_MAX = -90.0, 90.0
WRIST_PITCH_MIN, WRIST_PITCH_MAX = -90.0, 90.0

# Detection quality filters (applied by every detector AND by the loop's
# ghost-lock sliver / bloat checks).
DETECT_MIN_AREA_RATIO = 0.003
DETECT_MAX_AREA_RATIO = 0.80
DETECT_MIN_CONFIDENCE = 0.15  # lowered to catch phone at angles/back-facing

# Bbox-trust guard (ViT bloat protection).
# ViT can dissolve its lock into a box that overflows the whole frame — it stops
# tracking the object and "tracks" everything. Driving the servo off that
# bloated centroid causes jitter when the object is still and useless chase when
# it moves. We only treat a bbox as garbage when it MEETS OR EXCEEDS the full
# frame area: nothing real can be bigger than the frame, so this never freezes a
# legitimately large object (e.g. a person standing close fills 80–90% and must
# still track). On garbage we HOLD the servo and let YOLO/retry relock.
BBOX_FREEZE_RATIO = 1.0   # bbox area ≥ this fraction of frame ⇒ ViT dissolved
# Relative bloat: the frame-overflow check above misses the common failure where
# ViT balloons to 20–45% of the frame (still < 1 frame) while the real target is
# a small face (~3%). Its centroid then wanders ±200px/frame and the servo
# whipsaws (the "jerky, never centers" symptom). Hold the servo whenever the live
# bbox is more than this multiple of the last YOLO-trusted lock area. A genuinely
# large/approaching object is confirmed by YOLO, which updates the trusted
# baseline, so this never freezes a legitimately large target.
BLOAT_HOLD_MULT = 3.0

# Detection gating + reinit debounce (SORT/ByteTrack-style outlier rejection).
# Reject a YOLO/YuNet box whose area is more than this multiple off the recent
# median — it's almost certainly a false detection (background object, the
# second face, scale glitch). Don't reinit the tracker to it.
YOLO_AREA_GATE_MULT = 4.0
# Min seconds between tracker reinits, so a noisy detector can't reinit every
# frame. Bypassed when the tracker is clearly lost (see LOST_CENTER_FRAC).
REINIT_COOLDOWN_S = 0.5
# Detection-tracker center distance beyond this fraction of the frame diagonal
# means the lock is genuinely lost → reinit immediately, ignoring the cooldown.
LOST_CENTER_FRAC = 0.5

# Ghost-lock detection via tracker confidence (ViT only).
# Sliding window, NOT a consecutive-frame counter: ViT confidence on a dying
# lock flickers around the threshold (0.12 → 0.16 → 0.13 …), and a consecutive
# counter reset by every single above-threshold frame let ghost locks survive
# indefinitely. Stop when LOW_CONF_STOP_COUNT of the last LOW_CONF_WINDOW
# frames are below CONFIDENCE_THRESHOLD.
CONFIDENCE_THRESHOLD = 0.15
LOW_CONF_WINDOW = 15
LOW_CONF_STOP_COUNT = 8
# Floor for firing the servo PID at all, even while the detector still
# confirms the target. Without it, conf 0.15–0.4 with a fresh detector confirm
# was a blind zone that kept the servo chasing a barely-held lock. Below this
# → hold servo (tracker keeps updating; PID resumes when confidence recovers).
SERVO_MIN_CONF = 0.25
# When the detector (YuNet / YOLO) hasn't confirmed for TRUST_TRACKER_S, fall
# back to ViT's own confidence: if it's above this threshold, keep firing PID
# (the tracker still has a solid lock — common when face moves fast and YuNet
# misses a few frames). Below this → freeze servo, wait for detector.
TRACKER_TRUST_CONF = 0.4
# Detector-gated trust window (seconds) — a FLOOR, not the value used. The
# loop sizes the real window from the detector's measured latency
# (YOLO_REDETECT_S + 2 x latency + TRUST_MARGIN_S), because the same loop is
# served by detectors three orders of magnitude apart: YuNet at ~30ms, local
# YOLO at ~0.4s, the remote open-vocab model at ~0.55s median (up to ~2s). A
# single constant is
# only ever correct for one of them, and 2.5 was correct for the fastest — so
# every slower target sat in WAIT-YOLO with the object plainly in frame.
# This floor still applies when the detector is fast, keeping face behaviour
# exactly as it was.
TRUST_TRACKER_S = 2.5
# Slack on top of one redetect cycle, so ordinary scheduling jitter (a frame
# that arrived late, a detect thread that started a beat behind) does not read
# as a missed confirm.
TRUST_MARGIN_S = 0.5
# No detector confirm at all for this long → the lock is a ghost, stop.
STOP_NO_YOLO_S = 20.0

# PID gains for servo control (industry pattern: PyImageSearch face tracking).
# KP lowered again (0.025→0.015 yaw, 0.03→0.02 pitch) as part of the pursuit
# rework: velocity feedforward (VFF_GAIN below) is now the primary command —
# the camera *matches the target's speed* like human smooth pursuit — and the
# PID only trims residual position error. A large P term on top of full
# feedforward double-counts the error and overshoots.
PID_YAW_KP, PID_YAW_KI, PID_YAW_KD = 0.015, 0.002, 0.002
PID_PITCH_KP, PID_PITCH_KI, PID_PITCH_KD = 0.02, 0.002, 0.0025
PID_OUTPUT_MAX_DEG = 5.0
PID_INTEGRAL_MAX = 30.0

# --- Velocity feedforward (constant-velocity cinematic pan) ---
# The position PID only reacts to accumulated error, so a target moving at a
# steady speed is always chased in catch-up bursts (the "follows in jerks, not a
# smooth pan" feel). The alpha-beta filter already estimates the target's pixel
# velocity (vx_f, vy_f); feed a fraction of it straight to the servo as a rate
# command so the camera pans AT the target's speed even at zero position error.
# The PID then only has to correct the residual. 0 = off (pure position PID).
# Raised 0.6→0.9: velocity matching is now the dominant command (smooth
# pursuit); KP was lowered in step so the sum doesn't overshoot.
VFF_GAIN = 0.9
# Cap on the per-fire dt used to turn the feedforward rate (deg/s) into a
# per-fire step (deg) — a long gap between fires can't inject a huge lurch.
VFF_MAX_DT_S = 0.20
# Target pixel-speed above which a position-centered target is still "moving" →
# keep panning on feedforward instead of freezing in the dead zone.
VFF_MOVING_MIN_PXS = 40.0
