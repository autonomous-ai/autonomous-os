# DL Backend

GPU-accelerated perception backend for Autonomous devices. It runs the
deep-learning models a device's HAL can't run locally (action recognition, facial
and speech emotion, pose + ergonomics, object detection, speaker embedding) and
exposes them over WebSocket and HTTP behind an optional encrypting load balancer.

## Documentation

| Doc | Covers |
|-----|--------|
| [docs/README.md](docs/README.md) | Overview, component map, ports, quick start |
| [docs/architecture.md](docs/architecture.md) | Process topology, ports, URL prefixes, request lifecycle |
| [docs/api.md](docs/api.md) | Every endpoint: method, path, request/response schema, auth |
| [docs/perceptions.md](docs/perceptions.md) | The perception subsystems, models, enums, output types |
| [docs/crypto-and-loadbalancer.md](docs/crypto-and-loadbalancer.md) | `lbserver` round-robin proxy + RSA/AES encryption + nginx |
| [docs/configuration.md](docs/configuration.md) | All environment variables with defaults |

The platform-level overview lives at [`docs/dlbackend.md`](../docs/dlbackend.md)
(and the Vietnamese [`docs/vi/dlbackend_vi.md`](../docs/vi/dlbackend_vi.md)).

## Quick start (single node, no encryption)

```bash
cd dlbackend
# install deps (see pyproject.toml / Dockerfile for the CUDA stack)
export DL_API_KEY=dev-secret
python -m dlserver --host 0.0.0.0 --port 8001
```

```bash
curl -H "X-API-Key: dev-secret" http://localhost:8001/hal/api/dl/health
```

For the full proxied + encrypted stack (nginx → lbserver → dlserver), see
[docs/architecture.md](docs/architecture.md) and
[docs/crypto-and-loadbalancer.md](docs/crypto-and-loadbalancer.md).
</content>
