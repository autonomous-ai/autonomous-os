# Architecture Overview — Autonomous

## 3-Layer Architecture

```
Agentic Runtime (AI/LLM) → OS Server (Go, :5000) → HAL (Python, :5001) → Hardware
```

| Layer | Language | Port | Role |
|-------|----------|------|------|
| Agentic Runtime | Go | WS | AI brain, LLM, SKILL.md, memory, channels |
| OS Server | Go | 5000 | System (network, OTA, MQTT, reset), sensing event routing, local intent |
| HAL | Python | 5001 | Hardware drivers (servo, LED, camera, audio, display), FastAPI |

## Project Directory

```
system/
├── cmd/os-server/main.go              — OS Server entry point
├── cmd/bootstrap/main.go         — OTA bootstrap worker
├── server/
│   ├── server.go                 — Gin HTTP server, route setup
│   ├── config/                   — JSON config management
│   ├── health/delivery/http/     — Health, system info, dashboard
│   ├── network/delivery/http/    — WiFi scan, connect
│   ├── device/delivery/          — Setup (HTTP + MQTT handlers)
│   ├── sensing/delivery/http/    — Sensing event → intent match / agent gateway
│   └── openclaw/delivery/sse/    — Agent gateway status, SSE events
├── agent/  ambient/  beclient/  buddy/  device/  healthwatch/
├── intent/  monitor/  network/  skills/  statusled/  vision/
│                                 — System managers, one folder per diagram chip
├── lib/mqtt/                     — MQTT client (Eclipse Paho autopaho)
├── domain/                       — Shared structs
├── bootstrap/                    — OTA worker
└── web/                          — React 19 + Vite + Tailwind CSS 4 SPA

runtimes/                   — Swappable brains: openclaw/ hermes/ picoclaw/ codex/ claudecode/ opencode/

hal/
├── server.py                     — FastAPI server
├── config.py                     — Runtime constants (sensing thresholds, timeouts, URLs)
├── board/                        — Device profiles, board pin maps, and overlays
├── drivers/                      — Hardware services (camera, motors, RGB, sensing, voice, display)
├── routes/                       — FastAPI capability route modules
├── safety/                       — Parsed safety policy and deterministic gates
├── realtime/                     — Realtime voice agent and context managers
├── server_support/               — Shared HTTP/security support
└── pyproject.toml                — Python dependencies (opencv-python, insightface)

robots/                          — Per-device configs and overlays
  contract/                       — Shared API contracts (+ cts/ compliance suite)
skills/                           — Built-in SKILL.md files for agent runtime, including
                                    skill-creator for owner-authored skills
integrations/                     — Off-device: companions/, chat-bridges/, perception-service/
```

## Principles

- **Hardware is a plugin** — plug in and it works, unplug and it's skipped
- **System layer runs WITHOUT the runtime** — device always responds
- **Code is the source of truth** — docs reflect code
- **HAL is the hardware driver** — no AI logic
- **SKILL.md native** — no MCP, LLM reads skills and calls curl directly
- **Owners can create skills** — the built-in `skill-creator` guides an owner
  through drafting, testing, and packaging a skill for the Autonomous Skill Store.

## Lamp Simulator on a Laptop

> **Setup and run instructions live in [simulator.md](simulator.md)** — prerequisites,
> the config file and its example, the four `make` targets, and troubleshooting.
> This section covers what the simulator *is*.

`make sim` boots the production `lamp` declaration on a laptop. It keeps the
normal HAL routes and safety gates, but substitutes virtual motion, LED,
camera, microphone, speaker, voice and sensing services; it never opens a
servo bus, macOS camera/mic, GPIO, or sends GELF logs. Startup prints clickable
local links for the HAL docs and, for the default Lamp body,
`http://127.0.0.1:5001/simulator`. The latter has an orbitable view of the
checked-in Lamp CAD assembly plus live five-joint values, recording playback,
and LED-effect controls using the same `/servo/*` and `/led/*` endpoints a
skill uses. Drag to orbit, scroll to zoom, and double-click to reset the
camera. The state panel names the posture mode holding the body (`zero`,
`hold`) next to the live joints, and the Motor control button that owns it is lit: an
animation pressed while the motor is held is reported as ignored with that
reason, not as a tick, because `/servo/play` answers `"ignored"` rather than
`"ok"` for a play it dropped. Its CAD motion preview responds to the live joint
values, including
recording playback; a control switches to the untouched static assembly for
comparison. The repository does not include the mechanical joint hierarchy,
pivots, axes, or calibrated CAD zero offsets, so this visual response is not a
claim that a rendered `down` or `right` pose is physically correct.

This is an interface simulator, not a physics model: it has no mass or
collision model, while shipped recording CSVs replay their timing in memory.
Virtual camera/audio content is deterministic. To boot a minimal contract test
body instead, use `make sim DEVICE_TYPE=sim`. That body declares `motion` and
`system` and nothing else, which is how we prove HAL mounts exactly what a
`ROBOT.md` declares and no more — lamp cannot show that, because lamp declares
everything. See `robots/sim/ROBOT.md`.

For a manual Mac media check, use `make sim SIM_MEDIA=host`. This explicitly
opens the host camera, microphone and speaker: the simulator page shows its
camera stream, **Play test tone** uses the speaker, and **Record 3 seconds**
captures a WAV for playback. The default `SIM_MEDIA=virtual` remains
permission-free and deterministic for tests.

Host mode never hard-fails. Each subsystem is probed at boot — the webcam is
opened and read once, the microphone records a few milliseconds — and whichever
one is missing, busy or permission-denied falls back to its virtual device with
a logged `[sim-media]` line. `GET /simulator/state` reports the outcome per
subsystem (`media_camera`, `media_audio`, `media_reasons`, with `media` being
"host" only when both are), and the simulator page prints the same reason and
disables the tone/record buttons whenever audio is virtual. On macOS the two
permissions live under System Settings > Privacy & Security > Camera and
Microphone, and must be granted to the terminal app running HAL.

The camera in host mode uses a simulation-only driver
(`hal/drivers/camera/host_capture_device.py`, registered as `host`) that opens
the webcam through the platform's native OpenCV backend — AVFoundation on
macOS, where the production V4L2 path and its USB power-cycle healing do not
exist. Production bodies still resolve their `driver:` from ROBOT.md.

Host mode also runs the **real voice pipeline**: entry VAD, Silero, STT, the
realtime agent (Gemini Live), wake word, and the `[turn] route=…` dispatch that
forwards to os-server — the same code a board runs, not a stub. The gate is
`state.simulation_audio`, so the two decisions can never drift apart: a laptop
using its own microphone gets the pipeline that microphone exists for, and a
macOS permission denial flips the flag back and lands on the inert
`VirtualVoiceService` with a logged reason rather than a real pipeline reading a
dead device. `SIM_MEDIA=virtual` keeps the stub, so tests stay silent and offline.

Credentials come from the config.json HAL shares with os-server, exactly as on a
board — point `OS_CONFIG_PATH` at the `make os-dev` state dir and one file feeds
both processes. `llm_api_key` alone covers LLM, `AutonomousSTT`, ElevenLabs TTS,
image description, **and** Gemini Live (whose key falls back to it and whose
endpoint is `llm_base_url` + `/ws/gemini`); `deepgram_api_key` is optional and
only swaps the STT provider.

Music plays too. `MusicService` streams yt-dlp → ffmpeg → **aplay** (ALSA), or
**paplay** when a Bluetooth sink is active — neither exists on macOS, so both
routes failed at `Popen` and the device apologised out loud with "Sorry, I can't
play that right now". macOS gets a third route: ffmpeg's own AudioToolbox output
device, chosen over `ffplay` because ffmpeg is already a hard dependency here and
`ffplay` is not in every build. `SIM_MEDIA=virtual` keeps the same pipeline into
a null sink — the search and decode still run, the laptop just stays quiet.

Two HAL paths are device-absolute and must move for a laptop, both already
env-driven and set by the `sim` target: `HAL_SNAPSHOT_DIR` (where
`GET /camera/snapshot?save=true` writes — it has to sit under the agent
runtime's own home, `/root/.codex/media/hal-snapshots` on a board, or the agent
cannot read the frame back and os-server cannot serve its thumbnail) and
`HAL_SNAPSHOT_PERSIST_DIR` (`/var/lib/hal/snapshots` is root-only).

## Voice Pipeline

```
Mic (always on) → Local VAD (RMS energy, free)
    → Speech detected → Connect Deepgram STT
        → "hey lamp, turn off light" → voice_command → local intent → execute
        → "hey wanna grab lunch?" → voice (ambient) → OpenClaw
    → Silence 3s → Disconnect Deepgram
```

## Sensing Flow

```
HAL sensing loop (every 2s) → Read 1 camera frame, run all detectors:
    ├─ Motion detection (frame diff) → event if >8% pixels changed
    ├─ Face recognition (InsightFace buffalo_sc) → friend/stranger classification
    │     → presence.enter (annotated JPEG with colored bboxes: green=friend, red=stranger)
    │     → presence.leave (3 consecutive ticks without face)
    ├─ Light level (mean brightness, every 30s) → event if change >30/255
    └─ Sound detection (mic RMS) → event if > threshold

Event has image? (large motion, face enter) → encode frame full-resolution JPEG q85
Face enter image: original frame annotated with bounding boxes + labels

POST /api/sensing/event {type, message, image?}
    → OS server (Go):
        1. Voice event + local intent match? → execute directly (~50ms)
        2. No match → forward to OpenClaw:
           - Has image → SendChatMessageWithImage (text + vision content block)
           - No image → SendChatMessage (text only)
        3. OpenClaw AI sees image + reads context → decides action → calls SKILL API
```

Cooldowns to protect LLM costs: motion/sound 60s, presence 10s, light.level 30s.
