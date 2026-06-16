# DL Backend

GPU-accelerated perception backend for Autonomous devices. It runs deep-learning
models that a device's HAL cannot run locally (action recognition, facial / speech
emotion, pose + ergonomics, object detection, speaker embedding) and exposes them
over WebSocket and HTTP behind an optional encrypting load balancer.

> These docs describe the **current** code under `dlbackend/src`. The repo-root
> `docs/dlbackend.md` and the top-level `dlbackend/README.md` predate the
> `dlserver` / `lbserver` refactor and are stale — prefer this folder.

## Doc index

| Doc | Covers |
|-----|--------|
| [architecture.md](architecture.md) | Process topology, ports, URL prefixes, request lifecycle |
| [api.md](api.md) | Every endpoint: method, path, request/response schema, auth |
| [perceptions.md](perceptions.md) | The 8 perception subsystems, models, enums, output types |
| [crypto-and-loadbalancer.md](crypto-and-loadbalancer.md) | `lbserver` round-robin proxy + RSA/AES encryption + nginx |
| [configuration.md](configuration.md) | All environment variables with defaults |
| [configuration.md#model-downloading](configuration.md#model-downloading) | How model weights auto-download from the public bucket + per-model URLs |

## What it does

The backend is split into two FastAPI processes plus an nginx front door:

- **`dlserver`** (`:8001`) — runs the ML models and serves perception endpoints.
- **`lbserver`** (`:7999`) — round-robin proxy in front of one or more `dlserver`
  instances; terminates RSA+AES encryption so `dlserver` stays plaintext.
- **`nginx`** (`:8899`) — public TLS/HTTP front door; maps the device-facing
  `/lelamp/` prefix onto the internal `/hal/` prefix and upgrades WebSockets.

```
Device (HAL) / clients
        │  wss/https  :8899
        ▼
   ┌─────────┐   /lelamp/ → /hal/      ┌──────────┐  round-robin   ┌──────────┐
   │  nginx  │ ─────────────────────▶ │ lbserver │ ─────────────▶ │ dlserver │
   │  :8899  │     (WS upgrade)        │  :7999   │  (decrypt →     │  :8001   │
   └─────────┘                         └──────────┘   plaintext)    └──────────┘
                                       RSA+AES-GCM                  perception models
```

For a single-node dev setup you can talk to `dlserver` directly and skip
`lbserver`/nginx (no encryption, no proxy).

## Perception subsystems

| Subsystem | Transport | Default model | Output |
|-----------|-----------|---------------|--------|
| Action recognition | WS | X3D | Kinetics action classes + confidence |
| Facial emotion | WS + HTTP | POSTER V2 | Emotion + confidence (± valence/arousal) |
| Speech emotion (SER) | HTTP | emotion2vec | 9-class emotion + confidence |
| Pose estimation | WS | RTMPose (2D) + TCPFormer (3D) | 2D/3D keypoints + RULA ergonomics |
| Object detection | WS + HTTP | per-detector (opt-in) | Open-vocabulary boxes |
| Audio embedder | HTTP | WeSpeaker ResNet34 | Speaker embedding vector |
| Face detection | internal | YuNet | Face boxes (feeds emotion/pose) |
| Person detection | internal | YOLO | Person crop (feeds action) |

See [perceptions.md](perceptions.md) for model files, input sizes and enums.

## Quick start (single node, no encryption)

```bash
cd dlbackend
# install deps (see pyproject.toml / Dockerfile for the CUDA stack)
export DL_API_KEY=dev-secret
python -m dlserver --host 0.0.0.0 --port 8001
```

On first use each perception auto-downloads its weights from the public bucket
(`CDN_BASE`, default `https://storage.googleapis.com/autonomous-models`) into
`~/.cache/dlbackend/models`, so the first call to a model is slower. Weights are
not committed to the repo. See
[configuration.md#model-downloading](configuration.md#model-downloading) for the
per-model URLs and how to self-host or override them.

Then hit the health check:

```bash
curl -H "X-API-Key: dev-secret" http://localhost:8001/hal/api/dl/health
```

For the full proxied + encrypted stack (nginx → lbserver → dlserver), see
[architecture.md](architecture.md) and [crypto-and-loadbalancer.md](crypto-and-loadbalancer.md).

## Authentication

All endpoints require the `X-API-Key` header matching the `DL_API_KEY` env var
(validated on the HTTP request or the WebSocket handshake). `dlserver` raises at
startup if `DL_API_KEY` is unset. See [api.md](api.md#authentication).
</content>
</invoke>
