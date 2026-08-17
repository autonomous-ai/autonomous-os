# HAL — the hardware runtime

The Python FastAPI service that gives a body its API. It boots from the device's
`ROBOT.md`, mounts only the routes that body declares, enforces `SAFETY.md` bounds
in the request path, and hosts the realtime voice agent. Port 5001, loopback in
production; os-server (:5000) proxies it under `/api/hardware/*`.

```bash
(cd hal && uv sync) && make hal-dev   # from the repo root — uvicorn on :5001 with reload
make hal-test                         # pytest hal/test/
make hal-lint                         # broken imports + undefined names
```

| Folder | What lives there |
|---|---|
| `routes/` | the 13 capability routers — `audio`, `camera`, `servo`, `led`, `scene`, `emotion`, `sensing`, `display`, `music`, `voice`, `speaker`, `bluetooth`, `system` |
| `drivers/` | userspace drivers by subsystem — `motors/`, `rgb/`, `camera/`, `voice/` (STT, TTS, VAD), `sensing/`, `tracking/`, `display/`, `media_owner/`, plus GPIO button, TTP223 touch, Bluetooth |
| `board/` | `boards.json` (per-board wiring, matched against `/proc/device-tree/model`) and the declaration-driven mount planner (`device.py`) |
| `safety/` | `policy.py` — `SAFETY.md` parsed into pure gate functions |
| `realtime/` | the realtime voice agent (Gemini Live, OpenAI Realtime, Qwen) that answers small talk and delegates the rest to the brain |
| `recordings/`, `calibration/` | the 32 teleop-recorded servo animations and the per-unit calibration story |

How it fits the rest of the OS: [`docs/architecture/hal.md`](../docs/architecture/hal.md) ·
the frozen contract it serves: [`devices/contract/`](../devices/contract/) ·
adding a driver: [`../README.md#contribute`](../README.md#contribute).

## License

`hal/` is GPL-3.0 ([`LICENSE`](LICENSE)). It descends from
[LeLamp Runtime](https://github.com/humancomputerlab/lelamp_runtime); the inherited files and
the rules for touching them are in [`UPSTREAM.md`](UPSTREAM.md). Everything outside `hal/` in
this repository is Apache-2.0.
