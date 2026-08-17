# Porting a robot to Autonomous OS

The long form of the README's *Your own robot* section — every step with its file paths, sizes and caveats. Read [`devices/contract/ROBOT-SPEC.md`](../devices/contract/ROBOT-SPEC.md) and [`COMPATIBILITY.md`](../devices/contract/COMPATIBILITY.md) alongside it; [`devices/reachy-mini/`](../devices/reachy-mini/) is the worked example.

Three declaration files and whatever drivers your hardware needs, all in-tree. One scope note first: today's motion contract fits one shape — an articulated head (joints, aim, nudge, recordings), which is Lamp and Reachy. A wheeled base or an arm needs a second service beside `MotionService`; the Go2-W port is where that gets designed. Reachy Mini's port is about 1,650 lines: an 868-line `MotionService` wrapping Pollen's SDK, a 125-line media-handover class, a 310-line `rpicam` camera backend, one `boards.json` entry, three declaration files, and a 148-line installer — every piece behind an existing factory.

What's frozen: `autonomous.device.v1` is an ABI — fields are only added, capability names are never removed, a v1 `ROBOT.md` boots on every later v1 runtime. What isn't, yet: the driver protocols a port implements (`MotionService`, `MediaOwner`) are internal and can change between releases — which is why ports live in-tree, where a protocol change carries every driver with it, and not in your repo.

Start with `make new-device NAME=<id>` — it copies [`devices/_template/`](../devices/_template/) (a `ROBOT.md` that declares audio, vision, motion and system, plus a `SOUL.md` stub) into `devices/<id>/`. `make cts` fails until you add the `SAFETY.md` your `motion` declaration requires.

1. **`devices/<id>/ROBOT.md`** — the body. Front matter declares the board and the capabilities; the OS mounts only what you declare, and refuses to boot on a board you didn't list. `id` must equal the folder name; `type` is free-form (`desk_robot`, `desk_agent`, `mobile_robot` so far); each entry in `boards` is one JSON entry in `hal/board/boards.json` (copy `raspberry_pi_cm4`; the loader wants `led` and `button` blocks even if the body has neither); add `owner: <name>` to a capability if your own daemon holds that device. Keep comments on their own line — the parser does not strip trailing `#` comments. Compute needs 64-bit Linux with systemd on arm64, ~4 GB free (the installer brings its own Python 3.12 via uv), and a `/proc/device-tree/model` string a `boards.json` entry matches.
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
2. **A driver**, if your hardware is new. One class satisfying the 23-method `MotionService` protocol in [`hal/drivers/motors/base.py`](../hal/drivers/motors/base.py) — lifecycle, `move_to`, `aim`, `nudge`, `release`, `hold`, joint names and positions, recordings (the recording methods may no-op on an SDK that isn't animation-based) — plus one line in [`factory.py`](../hal/drivers/motors/factory.py). HAL owns the HTTP routes: a port of today's shape writes drivers, never routes — Reachy's added none. [`reachy_service.py`](../hal/drivers/motors/reachy_service.py) is a worked example wrapping a vendor SDK.
3. **Your software keeps running.** If a daemon you ship already holds the camera or mic, declare `owner: <name>` on that capability and implement the two-method `MediaOwner` protocol ([`hal/drivers/media_owner/base.py`](../hal/drivers/media_owner/base.py), one factory line): HAL calls `release()` before it opens any device and `acquire()` on shutdown. That is how Reachy runs beside Pollen's daemon ([`pollen.py`](../hal/drivers/media_owner/pollen.py), 125 lines).
4. **`SAFETY.md`** — the bounds. Numbers, not prompts: `motion.max_speed`, `light.max_brightness`, `quiet_hours`, `thermal.max_temp_c`. Only the front matter is machine-read — parsed into pure gate functions in [`hal/safety/policy.py`](../hal/safety/policy.py) that run inside the routes they cover, whoever asked: brightness on every LED write, quiet hours on music, `max_speed` on explicit moves (`/servo/move`, `aim`, `nudge`), thermal hysteresis in a background monitor. Not gated yet: recorded animations, the vision-tracking loop (its own 55 deg/s constant), `stop_always`; joint range is clamped by the driver's calibration, not a declared bound — the ledger is [`docs/safety.md`](../docs/safety.md). Undeclared bounds pass through with a warning; a malformed bound or an unknown `autonomous.safety.v<major>` refuses to boot. The OS never invents a limit nobody wrote.
5. **`SOUL.md`** — the self. Plain markdown the agent reads as who it is. Lamp's opens *"You are Lamp — a living being."* and forbids the words "servo", "API" and "LLM" out loud. Copy it, change the body it describes.
6. **Put it on the board.** The one-line installer only ships bodies in our release feed. Until yours is merged: install with `DEVICE_TYPE=lamp` (or `intern-v2` for a mic-and-speaker body), copy `devices/<id>/` to `/opt/devices/<id>`, set `DEVICE_TYPE=<id>` in `/opt/hal/.env` and in `/etc/systemd/system/os-server.service`, restart `hal` and `os-server`. [`devices/reachy-mini/spike-device.sh`](../devices/reachy-mini/spike-device.sh) and [`spike-os.sh`](../devices/reachy-mini/spike-os.sh) are exactly these steps, scripted. Merged, your body gets what Reachy got: the one-liner for everyone, every skill whose capabilities it declares, the six brains, and the setup and monitor UI.
7. **`make cts`** — the compatibility test suite ([`devices/contract/cts/`](../devices/contract/cts/)), Android-style. The static half proves your `ROBOT.md` obeys the [contract](../devices/contract/COMPATIBILITY.md). The runtime half proves the running body matches its own declaration — every declared route mounted and answering, nothing undeclared, `/servo/track/stop` replying (add `ALLOW_MOTION=1` to also prove `/servo/release`; it drops a raised arm). HAL and the daemon listen on the board's loopback only, so tunnel first: `ssh -N -L 5001:127.0.0.1:5001 -L 5000:127.0.0.1:5000 <user>@<body>.local`, then `make cts-runtime TARGET=127.0.0.1`. Passing both halves is what "Autonomous-compatible" means today; three rules of the spec the suite cannot check yet — local setup with no cloud round-trip, no stop routed through the brain, and a true e-stop — are on the list below.

## Run the contract with no hardware

The safety gate is a pure function, and the marker parser has tests that show exactly what it strips:

```bash
python3 -c "from hal.safety.policy import parse_safety, clamp_brightness; p = parse_safety(open('devices/lamp/SAFETY.md').read()); print(clamp_brightness(p, 255))"
go test ./system/server/agent/delivery/http/ -run ExtractHWCalls -v   # needs Go 1.24
```

[`skill-creator`](../skills/skill-creator/) grades whether a skill *triggers* for the right requests, on your laptop. What the marker *does* still needs a body — or the [mock body](https://github.com/autonomous-ai/autonomous-os/issues/200).

## What is frozen, what still moves

**Frozen:** the `autonomous.device.v1` schema (fields are only added) and the capability names in [`capabilities.md`](../devices/contract/capabilities.md) (never removed). **Not frozen yet:** the driver protocols (`MotionService`, `MediaOwner`) and the HAL route paths skills call (`/servo/aim`, `/emotion`) — both can move between releases, which is why ports live in-tree; port against a tag (`v0.1.4`, 2026-08-12). The motion contract is joint-space, proven on a 5–6 DOF head; an arm is the same `MotionService` with more joints (untested in-tree); wheels and legs need `LocomotionService`.

`hal/` is GPL-3.0 — wrapping a permissive vendor SDK is fine (`reachy_service.py` imports Pollen's Apache-2.0 `reachy_mini`, and that is the whole driver); a closed SDK goes out of process ([#204](https://github.com/autonomous-ai/autonomous-os/issues/204)).

What CTS still can't check is listed by name in [`cts/README.md`](../devices/contract/cts/README.md#not-covered-yet) — including rule 6, the immediate deterministic stop: no body in this repo has one yet (`/servo/release` travels to idle before cutting torque), so read that rule as where the contract is going, not as something we pass today. Fixing it is [#201](https://github.com/autonomous-ai/autonomous-os/issues/201).

## Before you write code

Open an issue titled `port: <robot>`. We answer the interface questions there — which `type`, which routes, whether you need `owner:` or `LocomotionService` — and review the PR when both CTS halves are pasted in.
