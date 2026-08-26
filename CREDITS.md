# Credits

Autonomous OS is assembled from other people's work. This is what a robot running it
actually carries, and where each piece came from. Licenses are stated only where the
tree proves them — a `LICENSE` file, a header, or the upstream's own published terms.
Where we could not verify one from here, we say so; check upstream before you ship.

## The runtime this grew out of

- **[LeLamp Runtime](https://github.com/humancomputerlab/lelamp_runtime)** — the motion
  and RGB service core, the `follower/` package and the teleop recordings in `hal/`.
  Commit `ee23699`, copied 2026-03-25. What we took, ignored and changed is listed file
  by file in [`hal/UPSTREAM.md`](hal/UPSTREAM.md).

## Models that run on the robot

- **[TEN-VAD](https://github.com/TEN-framework/ten-vad)** (Apache-2.0) — voice activity
  detection. `assets/ten-vad.onnx` is the original FP32 model, copied verbatim; the
  feature front-end is reimplemented in numpy in `hal/drivers/voice/ten_vad_lite/`.
- **[Silero VAD](https://github.com/snakers4/silero-vad)** (MIT) — `silero_vad.onnx`,
  the fallback detector.
- **[Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics)** (**AGPL-3.0**) —
  person detection for tracking. Both the package and the `yolov8n.pt` weights are
  AGPL; if you build a product on this path, read that license first.
- **[OpenCV Zoo](https://github.com/opencv/opencv_zoo)** — YuNet face detection and the
  ViT tracker (`face_detection_yunet_2023mar.onnx`, `vittrack.onnx`). Each model in that
  zoo carries its own license; not verified in-tree.
- **[LeRobot](https://github.com/huggingface/lerobot)** (Apache-2.0) — the servo bus our
  Feetech arms talk through.
- Face and voice identity: **SCRFD / InsightFace**, **EdgeFace**, **MediaPipe FaceMesh**,
  **RTMPose**, **WeSpeaker**. Inference code is ported into
  `hal/drivers/sensing/perceptions/` and `hal/drivers/voice/speaker_recognizer/`; weights
  are fetched at install time. Licenses not verified in-tree — check each upstream.
- Perception service extras (`integrations/perception-service/`): **emotion2vec** /
  FunASR, **POSTER V2** (whose `vit.py` comes from **[timm](https://github.com/huggingface/pytorch-image-models)**,
  Apache-2.0), **EmoAffectNet**, **OWLv2**, **UniFormerV2**, **TCPFormer**,
  **YOLO-World**, **X3D**.

## Brains we run, and do not ship

Each is installed on the robot from its own publisher; none is vendored here.

- **[OpenClaw](https://github.com/openclaw/openclaw)** and its Slack, Discord and
  WhatsApp connectors — the default brain.
- **[Hermes](https://nousresearch.com)** — Nous Research.
- **[Codex CLI](https://github.com/openai/codex)** (Apache-2.0) — OpenAI.
- **[Claude Code](https://claude.com/claude-code)** — Anthropic, proprietary.
- **[opencode](https://opencode.ai)**.
- **PicoClaw** — ours, in a separate repo.

## Bodies

- **[Pollen Robotics](https://github.com/pollen-robotics)** — the `reachy_mini` SDK and
  the emotions library. Our Reachy Mini driver is a wrapper around them and nothing else.
- **Raspberry Pi OS**, **OrangePi Debian** — the kernels we run on and do not ship.

## Code copied into this tree

- **[tinygo.org/x/bluetooth](https://github.com/tinygo-org/bluetooth)** (BSD-3-Clause) —
  forked under `integrations/companions/claude-desktop-buddy/third_party/bluetooth/` to
  add BlueZ secure read/write flags. LICENSE kept alongside.
- **[Anthropic Agent Skills](https://github.com/anthropics/skills)** (Apache-2.0) —
  `skills/skill-creator/` is theirs, with its `LICENSE.txt`.
- **[yq](https://github.com/mikefarah/yq)** (MIT) — downloaded by the installer.

## Everything else

The ordinary dependency list, credited by being in the manifests rather than here: Gin,
gorilla/websocket, discordgo, paho, wire, fsnotify, go-yaml and the Go standard extras
(`go.mod`); FastAPI, uvicorn, numpy, scipy, onnxruntime, OpenCV, Pillow, pydantic,
sounddevice, webrtcvad, LiveKit Agents and the vendor API clients — OpenAI, Google
GenAI, Anthropic, Deepgram, ElevenLabs, Qwen (`hal/pyproject.toml`); React, Vite,
Tailwind, Radix, framer-motion, chart.js and xterm (`system/web/package.json`).

## Speech the robot makes itself

Piper is the on-device TTS option. **None of it ships in the image** — the operator
turns it on in Settings → Voice and the device fetches the engine and each voice from
upstream at that moment. That is a deliberate boundary, not an accident of packaging:
what a user downloads for themselves, we are not distributing.

- **[Piper](https://github.com/rhasspy/piper)** (MIT) — the synthesis engine. Archived
  by its owner on 2025-10-07; the release tarballs and the voice repo both still work,
  and the voice repo was still being updated as of 2026-08. Development moved to
  **[piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)**, which is **GPL-3.0**.
- The engine tarball carries **espeak-ng** (**GPL-3.0**), **onnxruntime** (MIT) and
  `piper_phonemize` (MIT). The MIT badge on Piper covers Piper's own code — the bundle
  around it does not inherit it. Anyone who decides to ship this inside the image
  becomes a distributor of GPL-3.0 and owes the source offer and, for a consumer
  device, a look at GPLv3 §6. Read that license first.
- HAL runs the binary as a **separate process** over a pipe
  (`hal/drivers/voice/tts/piper.py`), never as a linked library. Keep it that way.

Voices are listed with their licence in
[`hal/drivers/voice/tts/piper_catalog.py`](hal/drivers/voice/tts/piper_catalog.py),
which is the enforcement point: a voice is offered only after someone read its dataset
terms. Adding one is a licensing decision.

- Nothing owed: `en_US-ljspeech`, `en_US-kristin` (public domain); `es_ES-davefx`,
  `de_DE-thorsten`, `pt_BR-faber` (CC0).
- **Attribution owed** — credited here, which is the whole point of this file:
  - **`vi_VN-vais1000`** — VAIS-1000 Vietnamese Speech Synthesis Corpus,
    [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  - **`en_US-libritts_r`** — LibriTTS-R, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  - **`fr_FR-siwis`** — SIWIS French Speech Synthesis Database,
    [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Deliberately **not** offered, and why, so nobody re-adds them: `en_US-lessac`
  (Blizzard 2013 — a research licence granted by hand, per organisation);
  `en_US-hfc_female`, `en_US-hfc_male`, `en_US-ryan`, `hi_IN-priyamvada`,
  `vi_VN-vivos` (CC BY-**NC**-SA — no commercial use); `vi_VN-25hours`,
  `ru_RU-irina`, `zh_CN-huayan` (licence unknown).

Something missing or miscredited? Open an issue — we would rather fix it than owe it.
