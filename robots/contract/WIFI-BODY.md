# Wi-Fi body integration pattern

A robot does not need to run Autonomous OS on its microcontroller. OS and HAL
can run on a computer while the robot firmware owns its hardware. The
Stack-chan integration is an experimental example of this existing driver
pattern, not a new capability or a separate OS API.

[Tiếng Việt](vi/WIFI-BODY_vi.md)

## Host and firmware responsibilities

```text
Agent / OS -> existing HAL routes -> MotionService driver
                                    -> authenticated network connection
                                       -> firmware -> actuators
```

The host loads a device profile, selects the motion driver through
`hal/drivers/motors/factory.py`, and mounts the declared routes. The driver
implements the existing `MotionService` contract, including body ownership,
measured positions, bounded movement, halt, and release. A network body should
not require skills to use a new set of robot-specific HTTP endpoints.

Firmware owns physical interpolation, feedback validity, torque control, and
an independent response to loss of the host or link. Host-side exceptions are
not a physical stop. Specify and verify how firmware cancels motion and holds
or enters its hardware-defined safe state when communication disappears.

## Device and host selection

`DEVICE_TYPE` identifies the robot profile. `HAL_BOARD` identifies the machine
running HAL. For a computer controlling a remote body, a profile can explicitly
declare `boards: [host]` and use `HAL_BOARD=host`. The host board skips local
GPIO peripherals; it does not replace the network motion driver with a mock.
`HAL_SIMULATE=1` is a separate mode that substitutes simulated drivers. Keep it
at `0` when controlling real remote hardware.

A minimal motion integration declares `motion` with its driver and `system`,
and supplies `SAFETY.md` and `SOUL.md`. Keep body-specific HAL environment
settings in the profile's `rootfs/opt/hal/.env`, with credentials supplied
locally. For an unqualified motion-only profile, use an underscore-prefixed
experimental directory rather than claiming capabilities that do not exist.
The Stack-chan example uses `DEVICE_TYPE=stackchan` with
`DEVICES_DIR=<repo>/robots/_experimental`.

## Stack-chan reference

The [host startup guide](../_experimental/stackchan/docs/runtime.md) is the
executable reference. OS and HAL run on the same computer; the current OS HAL
client uses `http://127.0.0.1:5001`. ESP32 firmware initiates an authenticated
WSS connection to HAL at `/stackchan/body/v1` on its separate body listener.
This connection direction belongs to the Stack-chan protocol, not to every
network body. Firmware validates the certificate; both sides share the body
identity and token. Plain WS requires an explicit isolated-development opt-in.

The driver requires advertised timed-move, measured-position, halt/hold and
torque-release capabilities. It enforces the configured speed ceiling against
measured position, renews a controller lease, and verifies arrival instead of
treating a scheduled-command acknowledgement as physical completion. Halt holds
the current pose; release travels to a rest pose and verifies arrival before
requesting torque-off. Firmware clears the lease on halt, so the host must
reacquire it before another move. See the [safety implementation notes](../../docs/safety.md).

The companion project's standalone HTTP bridge is a different entry point.
Pulling this driver does not switch that bridge to full HAL or flash firmware.

## Integration evidence and compatibility

Keep these milestones separate:

- Host tests: profile/env loading, driver selection, no unintended startup
  movement, HTTP-to-transport commands, failure paths and ownership behavior.
- Hardware verification: pinned flashed firmware, authenticated connection,
  calibrated coordinates/rest pose, measured movement, halt, link/process loss,
  lease expiry, recovery and torque behavior on the actual assembly.
- Compatibility: every requirement in [COMPATIBILITY.md](COMPATIBILITY.md),
  supported by the [CTS](cts/README.md). Motion plus a face alone does not meet
  the audio-or-vision requirement. Static tests alone do not qualify hardware.

A merged experimental driver completes host onboarding, not these later
milestones. Add display support through the existing semantic display routes
when a working firmware-backed implementation establishes the needed driver
boundary; do not assume the existing SPI-rendering implementation fits a body
that renders its own expressions.
