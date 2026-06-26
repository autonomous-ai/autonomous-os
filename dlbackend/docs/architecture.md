# Architecture

The backend is three cooperating processes: an nginx front door, the `lbserver`
encrypting load balancer, and one or more `dlserver` model servers.

```
HAL / clients
   │  https / wss
   ▼
┌──────────┐   listen :8899
│  nginx   │   location /         → 127.0.0.1:7999/        (lbserver, plain)
│  :8899   │   location /lelamp/  → 127.0.0.1:7999/hal/    (device traffic)
│          │   location /jupyter/ → 127.0.0.1:8890/jupyter/ (dev only)
└────┬─────┘
     │
     ▼
┌──────────┐   listen :7999
│ lbserver │   RSA + AES-256-GCM terminate
│  :7999   │   round-robin over LB__BACKENDS
└────┬─────┘
     │  plaintext HTTP/WS to a dlserver
     ▼
┌──────────┐   listen :8001
│ dlserver │   perception models + routes
│  :8001   │
└──────────┘
```

## Processes & ports

| Process | Default bind | Launch | Role |
|---------|--------------|--------|------|
| `dlserver` | `0.0.0.0:8001` | `python -m dlserver` | Loads ML models, serves perception endpoints |
| `lbserver` | `0.0.0.0:7999` | `python -m lbserver` | Round-robin proxy + RSA/AES encryption terminator |
| `nginx` | `:8899` (`8899 ssl` for TLS) | `nginx -c nginx.conf` | Public front door, prefix mapping, WS upgrade |

Both `dlserver` and `lbserver` accept `--host`, `--port`, `--log-dir`, `--pid-file`.

## URL prefixes

`dlserver` mounts its routers under two prefixes (see `src/dlserver/app.py`):

| Prefix | Routers | Why |
|--------|---------|-----|
| `/hal/api/dl` | action, facial-emotion, audio (SER + embedder), pose, health | Device-facing perception API |
| `/api/dl` | object detection (WS + HTTP) | Backward-compatible with the GO2 object-detection clients |

`lbserver` mounts:

| Prefix | Route |
|--------|-------|
| `/api/crypto` | `GET /public-key` (PEM) |

`lbserver` proxies everything else through to a backend, prefixing the upstream
path with an internal marker so nginx can route it (`src/lbserver/app.py`).

nginx maps the **external** device prefix `/lelamp/` onto the **internal** `/hal/`
prefix. So a device URL and the dlserver route differ by the front-door rewrite:

```
external (through nginx):  https://<host>:8899/lelamp/api/dl/action-analysis/ws
internal (dlserver route):              /hal/api/dl/action-analysis/ws
```

> `/lelamp/` is a legacy brand prefix carried in `nginx.conf`. Code is the source
> of truth; if/when it is de-branded, update both `nginx.conf` and this table.

## Request lifecycle (device → backend)

1. HAL opens a WS/HTTPS connection to nginx `:8899` under `/lelamp/...` with the
   `X-API-Key` header.
2. nginx rewrites `/lelamp/` → `/hal/` and forwards to `lbserver:7999` (WS upgrade
   preserved).
3. If encryption is enabled, `lbserver` performs the RSA key exchange (WS) or
   decrypts the `CipherHTTPRequest` body (HTTP), then forwards plaintext to a
   `dlserver` chosen round-robin from `LB__BACKENDS`.
4. `dlserver` validates `X-API-Key`, routes to the perception, runs inference, and
   returns the result.
5. `lbserver` re-encrypts the response (if the session is encrypted) on the way back.

For a single-node dev box you can point HAL straight at `dlserver:8001` with
encryption off — `lbserver` and nginx are only needed for fan-out and transport
encryption.

## Health & readiness

`GET /hal/api/dl/health` reports per-model load state, so you can see which
perceptions actually initialized:

```json
{
  "status": "ok",
  "models": {
    "action": true,
    "emotion": true,
    "ser": true,
    "pose": true,
    "audio_embedder": false,
    "object_detectors": {"yoloworld": false, "owlv2": false}
  }
}
```

A perception that is disabled via config (`*__ENABLED=false`) or fails to load its
weights reports `false` here while the rest of the server keeps serving.

## Security hardening

| Layer | Measure |
|-------|---------|
| **Auth** | `X-API-Key` header validated with `secrets.compare_digest` (timing-safe) on every HTTP and WS endpoint |
| **Input limits** | Decoded image size, pixel dimensions, audio size and duration are all capped (see [configuration.md](configuration.md#input-limits)) |
| **Connection limits** | Uvicorn `limit_concurrency=200` caps total concurrent connections |
| **Session cleanup** | WS handlers wrap session lifecycle in `try/finally` — `session.stop()` runs on disconnect or crash |
| **Backpressure** | `InputBatcher` queue is bounded (`batch_size × 100`); `await queue.put()` blocks when full |
| **Error masking** | HTTP 500 responses return `"Internal server error"`, never raw exception details |
| **Proxy safety** | LB WS proxy uses `asyncio.wait(FIRST_COMPLETED)` + task cancellation — no orphaned tasks on disconnect |
| **Download safety** | `ensure_downloaded` warns on plain HTTP URLs (MITM risk); HTTPS recommended |
| **Crypto** | RSA-2048 + AES-256-GCM with per-message random nonces; keys optionally persisted to disk |
</content>
