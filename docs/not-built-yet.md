# Not built yet — claim one

The full list behind the README's [Not built yet](../README.md#contribute). Each is a real gap in the tree; open an issue titled with the bullet and say you're on it.

- A skill catalog that reads `skills/` instead of a Go map (`system/skills/skills.go` `Catalog` + `Capability`) — small Go PR, and it makes "one folder per skill" literally true. Bonus: CI publishing the skill feed on merge.
- A mock body so the full stack runs on a laptop: `devices/sim/`. Shortest path we see: Pollen's own simulator — `reachy-mini-daemon --sim` serves the same `:8000` API our `reachy_sdk` driver already speaks (and it is how a Reachy Mini Lite runs, daemon on your laptop) — plus a `sim` board entry so HAL boots off-Pi. Untried. The one we want most.
- An out-of-process motion driver: a `MotionService` that forwards its 23 methods to a local HTTP endpoint, so a vendor SDK under any license can drive a body without living in GPL `hal/` — the driver boundary this repo doesn't have yet; `MediaOwner` → `pollen_daemon` is the precedent.
- Bring-your-own LLM endpoint for the OpenClaw brain (`runtimes/openclaw/service_setup.go` takes the model list from the Autonomous gateway) — and a self-serve gateway key for people who built their own body.
- Reachy Mini head tracking: `hal/drivers/tracking/` → `MotionService` (`/servo/track` still speaks Lamp's joint names).
- Community moves on Reachy: the driver loads only `pollen-robotics/reachy-mini-emotions-library` (`_EMOTES_DATASET`); a `HAL_REACHY_MOVES` list so any `reachy_mini_community_moves` dataset — a move you recorded and pushed to the Hub — plays by name and can back an emotion; plus the sound sidecar, playing each move's `.ogg` through HAL's speaker.
- Board entries without `led`/`button` blocks — `hal/board/board.py` requires both today; a rolling body has neither.
- Route stability in the contract: the HAL paths skills actually call (`/emotion`, `/servo/aim`, `/api/guard/enable` — 111 endpoints today) added to `capabilities.md`'s frozen surface, plus a CTS probe that every documented path still answers. None has been removed since v0.1.0, but that is history, not a rule.
- Unitree Go2-W: a `unitree_go2w` board entry, a `LocomotionService` beside `MotionService` (velocity + a hard stop), `hal/routes/locomotion.py` and a `depth` route, a `/locomotion/stop` probe in the CTS, a `motion.drive` sub-capability so head skills stop matching rolling bodies — plus what Reachy needed too: an installer for Unitree's compute and, if its services hold the camera or mic, a `MediaOwner`. First body that rolls, so open the `LocomotionService` shape as an issue before code — it's the interface every rolling body after this one implements.
- A ROS 2 `MotionService` — subscribe `sensor_msgs/JointState`, drive a `joint_trajectory_controller` (`trajectory_msgs/JointTrajectory` or its `FollowJointTrajectory` action) — so any ros2_control head or arm is a driver, not a rewrite; a wheeled base — `cmd_vel` (`geometry_msgs/Twist`) direct, or a `NavigateToPose` goal handed to Nav2 — waits on `LocomotionService`. Nothing ROS-shaped is in-tree today.
- Run the agent runtimes as an unprivileged user (systemd `User=`, no `dialout`/`gpio`/`video` groups) so the safety gate is a boundary, not just the request path.
- A real `POST /servo/stop` that interrupts a running move or recording and holds, consumed by `motion.stop_always`; recorded animations and the tracking loop under the safety gate; a CTS probe for it.
- A body with a screen, so the `display` skill has a home.
- A Pi 4B image test (`make -C scripts/imager TARGET=rpi RPI_MODEL=4 build` is code-complete, untested on hardware).
- x86 boards: a DMI (`/sys/class/dmi/id/*`) matcher beside the device-tree one in `hal/board/board.py`, so a NUC-carrying robot can be a `boards.json` entry.
- A policy behind the marker — `PolicyService` in `hal/`: `[HW:/policy/run:{"policy":"lerobot/smolvla_base","task":"pick up the mug"}]` starts a LeRobot policy (local `lerobot` inference or its async server) on an arm capability, the safety gate clamps its joint targets, `POST /servo/stop` halts it. First body: an SO-101 declared in `devices/so101/ROBOT.md`. Open the interface as an issue before code.
- A `ReachyMiniApp` wrapper published as a Space tagged `reachy_mini`, so Autonomous OS installs from Pollen's own dashboard app list and stops from there too.
- Lamp's 23 teleop moves exported by `hal/record.py` as a Hub dataset in Pollen's emotion-library format (`autonomous-os/lamp-emotions-library`), so a move recorded on either body plays on both.
- Reachy Mini Lite: the daemon runs on your laptop, so the mock body (`devices/sim/` on `reachy-mini-daemon --sim`) is also the Lite path.
- A measured turn latency — `make latency`: end of speech → first spoken word and → first `[HW:]` POST, p50/p95 over ~20 turns on Lamp with the default brain. Today the code comments say 3–5 s in one place and 8 s time-to-first-token in another.
- A hosted-model table (model, task, open weights yes/no, local swap path) for the voice, face and mood models the gateway serves.
- The contract's reference parsers (`hal/board/device.py`, `hal/safety/policy.py`) sit inside GPL `hal/`, so the Apache-licensed CTS imports GPL code; moving them under `devices/contract/` is a wanted PR.
- A plain setup page for browser setup without the phone app (today `/setup?debug=true&device_id=…`).
- The `Signed OTA` item in full: checksummed, signed release zips with rollback; today `bootstrap/` pulls unsigned zips over HTTPS every 5 min and restarts.
