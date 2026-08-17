# Bring your own robot

<img src="media/build-your-own.webp" alt="Printed robot parts laid out on a bench" width="720">

Skills, brains and the app are shared, so a new robot brings only three markdown files and one driver: **ROBOT.md** the body, **SOUL.md** the self, **SAFETY.md** the bounds. Seven steps, laptop to merged. [`devices/reachy-mini/`](../devices/reachy-mini/) is the worked example; read [`ROBOT-SPEC.md`](../devices/contract/ROBOT-SPEC.md) and [`COMPATIBILITY.md`](../devices/contract/COMPATIBILITY.md) alongside this.

**First check the compute.** 64-bit arm64 Linux with systemd, ~4 GB free (the installer brings its own Python 3.12 via uv), and a `/proc/device-tree/model` string that matches an entry in [`boards.json`](../hal/board/boards.json) — a new board is one JSON entry, not a code change.

**And check the shape.** Today's motion contract is joint-space, proven on a 5–6 DOF articulated head — joints, aim, nudge, recordings — which is Lamp and Reachy Mini. An arm is the same `MotionService` with more joints, untested in-tree. Wheels and legs need a second service beside it, `LocomotionService`; the [Go2-W port](https://github.com/autonomous-ai/autonomous-os/issues/205) is where that gets designed.

## 1. Scaffold the folder

```bash
make new-device NAME=my-robot     # copies devices/_template/ into devices/my-robot/
```

The template declares audio, vision, motion and system, and ships a `SOUL.md` stub. `make cts` fails until you add the `SAFETY.md` your `motion` declaration requires.

## 2. Declare the body in `ROBOT.md`

List the board and the [capabilities](../devices/contract/capabilities.md) the robot has. The OS mounts exactly this and nothing else, and refuses to boot on a board you didn't name.

```yaml
---
schema: autonomous.device.v1
id: my-robot
name: My Robot
type: mobile_robot
boards: [raspberry_pi_5]
gateway: { default: openclaw }
capabilities:
  audio:  { routes: [audio, speaker, voice], required: true }
  vision: { routes: [camera], driver: opencv, required: true }
  motion: { routes: [servo], driver: my_sdk, required: true, safety: SAFETY.md#motion }
  system: { routes: [system], required: true }
soul_ref: SOUL.md
safety_ref: SAFETY.md
---
```

Details that bite: `id` must equal the folder name. `type` is free-form (`desk_robot`, `desk_agent`, `mobile_robot` so far). Each entry in `boards` is one JSON entry in [`hal/board/boards.json`](../hal/board/boards.json) — copy `raspberry_pi_cm4`; the loader wants `led` and `button` blocks even if your body has neither. Add `owner: <name>` to a capability if your own daemon holds that device (see step 4). Keep comments on their own line — the parser does not strip trailing `#` comments.

## 3. Write `SOUL.md` and `SAFETY.md`

`SOUL.md` is the self: plain markdown the agent reads as who it is. Lamp's opens *"You are Lamp — a living being."* and forbids the words "servo", "API" and "LLM" out loud. Copy it, change the body it describes.

`SAFETY.md` is the bounds, and it is numbers, not prompts: `motion.max_speed`, `light.max_brightness`, `quiet_hours`, `thermal.max_temp_c`. Only the front matter is machine-read — parsed into pure gate functions in [`hal/safety/policy.py`](../hal/safety/policy.py) that run inside the routes they cover, whoever asked: brightness on every LED write, quiet hours on music, `max_speed` on explicit moves (`/servo/move`, `aim`, `nudge`), thermal hysteresis in a background monitor. Not gated yet: recorded animations, the vision-tracking loop (its own 55 deg/s constant), `stop_always`; joint range is clamped by the driver's calibration, not a declared bound — the ledger is [`docs/safety.md`](safety.md). Undeclared bounds pass through with a warning; a malformed bound or an unknown `autonomous.safety.v<major>` refuses to boot. The OS never invents a limit nobody wrote.

## 4. Add a driver if the hardware is new

One class satisfying the 23-method `MotionService` protocol in [`hal/drivers/motors/base.py`](../hal/drivers/motors/base.py) — lifecycle, `move_to`, `aim`, `nudge`, `release`, `hold`, joint names and positions, recordings (the recording methods may no-op on an SDK that isn't animation-based) — plus one line in [`factory.py`](../hal/drivers/motors/factory.py). HAL owns the HTTP routes: a port of today's shape writes drivers, never routes — Reachy's added none. [`reachy_service.py`](../hal/drivers/motors/reachy_service.py) is a worked example wrapping a vendor SDK.

**Your own software keeps running.** If a daemon you ship already holds the camera or mic, declare `owner: <name>` on that capability and implement the two-method `MediaOwner` protocol ([`hal/drivers/media_owner/base.py`](../hal/drivers/media_owner/base.py), one factory line): HAL calls `release()` before it opens any device and `acquire()` on shutdown. That is how Reachy runs beside Pollen's daemon ([`pollen.py`](../hal/drivers/media_owner/pollen.py), 125 lines).

## 5. Put it on the board and run the test suite

The one-line installer only ships bodies in our release feed. Until yours is merged: install with `DEVICE_TYPE=lamp` (or `intern-v2` for a mic-and-speaker body), copy `devices/<id>/` to `/opt/devices/<id>`, set `DEVICE_TYPE=<id>` in `/opt/hal/.env` and in `/etc/systemd/system/os-server.service`, then restart `hal` and `os-server`. [`spike-device.sh`](../devices/reachy-mini/spike-device.sh) and [`spike-os.sh`](../devices/reachy-mini/spike-os.sh) are exactly these steps, scripted.

```bash
make cts                          # on your laptop: ROBOT.md against the contract
make cts-runtime TARGET=<ip>      # against the robot: every declared route answering
```

CTS is [`devices/contract/cts/`](../devices/contract/cts/), Android-style. The static half proves your `ROBOT.md` obeys the [contract](../devices/contract/COMPATIBILITY.md). The runtime half proves the running body matches its own declaration — every declared route mounted and answering, nothing undeclared, `/servo/track/stop` replying (add `ALLOW_MOTION=1` to also prove `/servo/release`; it drops a raised arm). HAL and the daemon listen on the board's loopback only, so tunnel first:

```bash
ssh -N -L 5001:127.0.0.1:5001 -L 5000:127.0.0.1:5000 <user>@<body>.local
make cts-runtime TARGET=127.0.0.1
```

*Autonomous-compatible* means passing these two, not our opinion of your robot: [`COMPATIBILITY.md`](../devices/contract/COMPATIBILITY.md) is 16 numbered rules and the suite checks them. No fee, no contract, no sign-off from us.

**Without hardware**, two pieces still run on a laptop:

```bash
python3 -c "from hal.safety.policy import parse_safety, clamp_brightness; p = parse_safety(open('devices/lamp/SAFETY.md').read()); print(clamp_brightness(p, 255))"
go test ./system/server/agent/delivery/http/ -run ExtractHWCalls -v   # needs Go 1.24
```

[`skill-creator`](../skills/skill-creator/) grades whether a skill *triggers* for the right requests, also on your laptop. What the marker *does* still needs a body — or the [mock body](https://github.com/autonomous-ai/autonomous-os/issues/200).

## 6. Teach it a skill

A skill is one folder with one `SKILL.md`, and it acts by writing markers the OS turns into motion:

```markdown
---
name: morning-wave
description: When someone says good morning, greet them by name and wave.
---
1. Reply with `[HW:/emotion:{"emotion":"greeting","intensity":0.9}]` — the arm waves, the ring warms up.
2. Say good morning, using their name if you know their face. One sentence.
```

```bash
make push-skill SKILL=./my-skill TARGET=pi@my-robot.local   # live on the next conversation, no reboot
```

It is the same `SKILL.md` OpenClaw and Claude skills use, so the ones you have work as they are. Writing one, the marker grammar, and shipping a skill to every robot: [`skills/README.md`](../skills/README.md).

## 7. Open the PR

Start with an issue titled `port: <robot>` — we answer the interface questions there before you write code (which `type`, which routes, whether you need `owner:` or `LocomotionService`), and review the PR once both CTS halves are pasted in.

The day it merges your robot is a product: a one-line installer for your customers, every skill its hardware supports, six brains, the app's Add-robot flow, OTA and a live monitor — plus every skill written from then on. Reachy Mini got all of it for ~2,900 lines over two weeks (2026-07-21 → 08-04) — an 868-line motion driver, a 125-line media-handover class, a 310-line camera backend, 1,875 lines of installer and unit scripts, 189 lines of declarations — with no change to Pollen's stack.

## What is frozen, what still moves

**Frozen:** the `autonomous.device.v1` schema (fields are only added) and the capability names in [`capabilities.md`](../devices/contract/capabilities.md) (never removed). A v1 `ROBOT.md` boots on every later v1 runtime. **Not frozen yet:** the driver protocols (`MotionService`, `MediaOwner`) and the HAL route paths skills call (`/servo/aim`, `/emotion`) — both can move between releases, which is why ports live in-tree, where a protocol change carries every driver with it; port against a tag (`v0.1.4`, 2026-08-12).

`hal/` is GPL-3.0 — wrapping a permissive vendor SDK is fine (`reachy_service.py` imports Pollen's Apache-2.0 `reachy_mini`, and that is the whole driver); a closed SDK goes out of process ([#204](https://github.com/autonomous-ai/autonomous-os/issues/204)).

What CTS still can't check is listed by name in [`cts/README.md`](../devices/contract/cts/README.md#not-covered-yet) — including rule 6, the immediate deterministic stop: no body in this repo has one yet (`/servo/release` travels to idle before cutting torque), so read that rule as where the contract is going, not as something we pass today. Fixing it is [#201](https://github.com/autonomous-ai/autonomous-os/issues/201).

Copy from a finished one: [Lamp](../devices/lamp/), [Intern](../devices/intern-v2/), [Reachy Mini](../devices/reachy-mini/), [Go2-W](../devices/unitree-go2w/).
