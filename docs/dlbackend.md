# DL Backend

GPU-accelerated perception backend for Autonomous devices. It runs the
deep-learning models a device's HAL can't run locally (action recognition, facial
and speech emotion, pose + ergonomics, object detection, speaker embedding) and
exposes them over WebSocket and HTTP behind an optional encrypting load balancer.

This page is the **platform overview** — what the backend is and where it sits.
The **full reference lives next to the code** in
[`dlbackend/docs/`](../dlbackend/docs/); schemas, model lists, endpoints and env
vars stay there, not here.

| Want… | Read |
|-------|------|
| Process topology, ports, URL prefixes, request lifecycle | [`dlbackend/docs/architecture.md`](../dlbackend/docs/architecture.md) |
| Every endpoint with request/response schemas | [`dlbackend/docs/api.md`](../dlbackend/docs/api.md) |
| Perception models, enums, output types | [`dlbackend/docs/perceptions.md`](../dlbackend/docs/perceptions.md) |
| Load balancer + RSA/AES encryption + nginx | [`dlbackend/docs/crypto-and-loadbalancer.md`](../dlbackend/docs/crypto-and-loadbalancer.md) |
| Deploy: install, GPU scaling, RunPod, Docker, TLS | [`dlbackend/docs/deployment.md`](../dlbackend/docs/deployment.md) |
| All environment variables with defaults | [`dlbackend/docs/configuration.md`](../dlbackend/docs/configuration.md) |

## Architecture at a glance

Two FastAPI processes behind an nginx front door:

```
HAL / clients
   │  https / wss  :8899
   ▼
┌─────────┐   /lelamp/ → /hal/      ┌──────────┐  round-robin   ┌──────────┐
│  nginx  │ ─────────────────────▶ │ lbserver │ ─────────────▶ │ dlserver │
│  :8899  │     (WS upgrade)        │  :7999   │  decrypt →      │  :8001   │
└─────────┘                         └──────────┘   plaintext     └──────────┘
```

- **`dlserver`** (`:8001`) — loads the ML models, serves the perception endpoints.
- **`lbserver`** (`:7999`) — round-robin proxy over one or more `dlserver`
  instances; terminates RSA+AES encryption so `dlserver` stays plaintext.
- **`nginx`** (`:8899`) — public front door; maps the device-facing `/lelamp/`
  prefix onto the internal `/hal/` prefix and upgrades WebSockets.

For single-node dev you can hit `dlserver:8001` directly with encryption off.

## What it provides

Perception subsystems exposed to devices: action recognition, facial emotion,
speech emotion (SER), pose estimation (with RULA ergonomics), object detection and
speaker embedding. Face and person detection run internally to feed those
pipelines. Requests from concurrent sessions are batched before GPU dispatch —
tune `BATCH_SIZE` and `BATCH_TIMEOUT` per model. Model choices and outputs:
[`dlbackend/docs/perceptions.md`](../dlbackend/docs/perceptions.md).

## Using it from a device

HAL is the main client. Point it at the backend with `DL_BACKEND_URL` and the
shared `DL_API_KEY` (sent as `X-API-Key`), and optionally enable client-side
encryption. Sensing streams camera frames to the action/pose/emotion endpoints;
voice posts end-of-utterance audio to the speech-emotion endpoint. Exact endpoints
and payloads are in [`dlbackend/docs/api.md`](../dlbackend/docs/api.md); every
config knob is in
[`dlbackend/docs/configuration.md`](../dlbackend/docs/configuration.md).

## Deployment

`dlbackend/` ships a `Dockerfile` (CUDA + nginx), `nginx.conf` / `nginx-ssl.conf`
and a `Makefile`; the two processes launch via `python -m dlserver` and
`python -m lbserver`. Install, single-node vs master/slave GPU scaling, RunPod,
Docker and TLS: [`dlbackend/docs/deployment.md`](../dlbackend/docs/deployment.md).

---

> **Doc-sync:** this page is an overview only. When code changes, update the
> detailed docs under [`dlbackend/docs/`](../dlbackend/docs/) **and** the
> Vietnamese overview [`docs/vi/dlbackend_vi.md`](vi/dlbackend_vi.md). Code is the
> source of truth.
</content>
