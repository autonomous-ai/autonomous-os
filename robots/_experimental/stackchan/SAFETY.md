---
schema: autonomous.safety.v1
motion:
  max_speed: 10
  stop_always: true
---

# Stack-chan experimental safety bounds

## motion

The 10 degree/second ceiling is a conservative, unqualified development
default, not a measured hardware limit. HAL stretches timed moves against
measured positions, including zero and gravity-rest moves; a required duration
over 60 seconds fails closed. Stop remains available independently of motion
ownership. Remote stop depends on transport delivery; the firmware must also
halt and hold on disconnect or controller-lease expiry.

The current driver accepts `base_yaw.pos` in -30..30 degrees and
`base_pitch.pos` in -15..15 degrees. Pitch uses the legacy 45-degree midpoint:
HAL pitch 0 maps to firmware pitch angle 450 in tenths of a degree. Release
targets yaw 0, pitch -15, measures arrival, then requests torque-off. These
ranges and the head-down rest pose are not physically calibrated for a
particular assembly. Confirm calibration, gravity support, torque behavior,
timed speed, stop, Wi-Fi loss and process-loss behavior before qualification.

### Optional home commissioning

Home commissioning is a separate supervised path, disabled by default with
`STACKCHAN_HOME_COMMISSIONING_ENABLED=0`. `GET /servo/home` performs capability
discovery only and takes no bus lease. `GET /servo/home/position` returns
measured pan/tilt in `calibrated_home_deg_v1` without taking a lease. A move
requires firmware capability `motion.home_degrees.v1` and an explicit
`POST /servo/home/move` request with only a tilt target from 7 to 10 degrees
and a requested duration from 2 to 10 seconds. The motion speed policy may
stretch that request, but the safe transport limit is 60 seconds.

HAL first requires measured pan within ±30 degrees and measured tilt in [0, 5).
The commissioning command contains pitch only, so omitted yaw is torque-off
during the pitch move. A stop or transport loss while below 5
degrees may therefore fail to hold and leave torque off or the session faulted;
use mechanical support and direct supervision. Invalid feedback, reconnect,
yaw drift over 1 degree, timeout and cancellation fail closed. Success requires
measured pitch within 1 degree of target, at least 6 degrees, and at least 1
degree of positive change. HAL then uses `lease.release` to re-energize both
axes, establish a both-axes-held terminal state, and recheck measured position.

This path does not verify calibration, replace the legacy ±15-degree mapping
around the 45-degree midpoint, or authorize later preset moves. A supervised hardware trial on 2026-09-11 produced visible motion,
followed by a transport-failure reboot without final feedback or verified hold.
Commissioning remains unqualified and disabled outside supervised diagnostics.

TLS and a distinct shared body token are required for the normal connection.
The explicit insecure-WS option is restricted to isolated development use.
Host tests and matching firmware source do not qualify physical safety.
