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

TLS and a distinct shared body token are required for the normal connection.
The explicit insecure-WS option is restricted to isolated development use.
Host tests and matching firmware source do not qualify physical safety.
