# Deployment

How to run the DL backend on a GPU box (RunPod or any CUDA host), from a single
dev process up to a multi-GPU round-robin cluster. Everything here is driven by the
`Makefile` — the targets are the source of truth; this doc explains them.

## Topology & ports

```
            public                  master node                    extra GPU nodes
  client ──────────▶ nginx :8899 ──▶ lbserver :7999 ──┬──▶ dlserver :8001  (local)
                     (TLS optional)   round-robin       ├──▶ slave node #1  (LB__BACKENDS)
                                                        └──▶ slave node #2  (LB__BACKENDS)
```

| Port | Process | Notes |
|------|---------|-------|
| `8899` | nginx | Public entry (HTTP, or `8899 ssl`). Maps `/lelamp/`→`/hal/`, upgrades WS |
| `7999` | lbserver (master) **or** dlserver (slave) | On a slave node nginx proxies straight to the dlserver |
| `8001` | dlserver (master, behind LB) | Local backend the master's LB fans out to |
| `8890` | jupyter | Optional, dev convenience |

`nginx.conf` always proxies `:8899 → 127.0.0.1:7999`. On a **master** node `7999` is
the load balancer; on a **slave** node `7999` is a bare dlserver.

## Install

`pyproject.toml` declares the dependencies (there is no `requirements.txt`).

```bash
make install        # auto-detects CUDA: GPU → onnxruntime-gpu + TensorRT, else CPU
make install-gpu    # force onnxruntime-gpu (+ make install-tensorrt)
make install-cpu    # force CPU onnxruntime
make install-lb     # load-balancer deps only (for a lbserver-only node)
make install-dev    # CPU + dev/test tooling
```

`make install-tensorrt` installs TensorRT 10.8 (cu12) + cuDNN 9 and appends the
matching `LD_LIBRARY_PATH` to `~/.bashrc`. If it fails it falls back to CUDA/CPU.

## Required configuration

| Env | Required | Purpose |
|-----|----------|---------|
| `DL_API_KEY` | **yes** | Shared `X-API-Key`; both servers refuse to start without it |
| `LB__BACKENDS` | master only | Comma-separated dlserver URLs the LB round-robins over |

All other knobs (model selection, thresholds, crypto, input limits, model download)
are in [configuration.md](configuration.md).

`dlserver` enforces a connection cap of 200 concurrent connections (uvicorn
`limit_concurrency`). Each WebSocket connection creates a GPU-backed session, so
this prevents memory exhaustion from connection floods. Input payloads are also
size-capped — see [configuration.md#input-limits](configuration.md#input-limits).

## Single node — dev (foreground)

Run one server directly, no nginx/LB, no encryption:

```bash
export DL_API_KEY=dev-secret
make start            # dlserver on :8001  (alias: make start-dlserver)
# or, separately:
make start-lbserver   # lbserver on :7999
```

Hit it: `curl -H "X-API-Key: dev-secret" http://localhost:8001/hal/api/dl/health`.

## Single node — full stack (master)

Brings up nginx + dlserver + lbserver as background, auto-restarting processes:

```bash
export DL_API_KEY=<secret>
export LB__BACKENDS=http://127.0.0.1:8001   # the local dlserver
make start-runpod-master        # plain HTTP on :8899
make start-runpod-master-ssl    # HTTPS on :8899 (self-signed cert auto-generated)
```

This is `start-nginx[-ssl]` + `start-runpod-dlserver` + `start-runpod-lbserver`.
Both servers run under `scripts/run-with-restart.sh`, a watchdog that restarts the
process on crash after a 5s cooldown.

## Scaling across GPUs (master + slaves)

To spread load over more GPUs, add **slave** nodes — each runs a standalone
dlserver behind its own nginx — and list their public URLs in the master's
`LB__BACKENDS`. The master's lbserver round-robins across all backends (local +
slaves) for both HTTP and WebSocket. See
[crypto-and-loadbalancer.md](crypto-and-loadbalancer.md#scaling-topology) for how
the proxy distributes traffic.

On each **slave** GPU node:

```bash
export DL_API_KEY=<same secret as master>
make start-runpod-slave        # nginx :8899 → dlserver :7999   (no LB on this node)
make start-runpod-slave-ssl    # same, HTTPS
```

Slave nodes run under the same `run-with-restart.sh` watchdog as the master, so
they auto-restart on exit and get the same log rotation and size cap. `make
stop-runpod-dlserver` stops a slave node too.

On the **master**, point the LB at the local dlserver plus every slave's public
endpoint, then start the master stack:

```bash
export LB__BACKENDS="http://127.0.0.1:8001,https://<slave1-host>:8899,https://<slave2-host>:8899"
make start-runpod-master       # (or -master-ssl)
```

`DL_API_KEY` must match across all nodes. Slaves do not run a load balancer; they
are pure model servers.

## TLS / SSL

```bash
make gen-ssl-cert     # self-signed cert+key at $SSL_DIR (default /workspace/ssl)
```

The `*-ssl` targets call this automatically. Override `SSL_DIR`, `SSL_CERT`,
`SSL_KEY` to use real certificates. `nginx-ssl.conf` listens on `8899 ssl`.

## Process management

### Makefile targets

Starting:

Default ports: dlserver=`8001`, lbserver=`7999`, nginx=`8899`, jupyter=`8890`.
Override with `DLSERVER_PORT`, `LBSERVER_PORT`, `JUPYTER_PORT` make variables.

| Target | What it does |
|--------|-------------|
| `make start` | Foreground dlserver on `:8001` (alias: `make start-dlserver`) |
| `make start-lbserver` | Foreground lbserver on `:7999` |
| `make start-runpod-master` | Background: nginx + dlserver + lbserver (HTTP) |
| `make start-runpod-master-ssl` | Same with self-signed TLS |
| `make start-runpod-slave` | Background: nginx + dlserver only (no LB), with auto-restart watchdog |
| `make start-runpod-slave-ssl` | Same with TLS |
| `make start-runpod-dlserver` | Background dlserver with auto-restart watchdog |
| `make start-runpod-lbserver` | Background lbserver with auto-restart watchdog |
| `make start-nginx` | Start nginx (HTTP) |
| `make start-nginx-ssl` | Start nginx (HTTPS, auto-generates self-signed cert) |
| `make start-jupyter` | Jupyter Lab on `:8890` |

Stopping:

| Target | What it does |
|--------|-------------|
| `make stop-runpod-dlserver` | Kill dlserver + its watchdog wrapper |
| `make stop-runpod-lbserver` | Kill lbserver + its watchdog wrapper |
| `make stop-nginx` | Stop nginx |

Status:

```bash
make info    # port layout + running/stopped state of each process
```

### Testing

```bash
make test              # all API + local tests
make test-local        # all local tests (no remote server needed)
make test-api          # all remote API tests (needs DL_BACKEND_URL)
make test-action-local # single subsystem
make test-benchmark    # HTTP + WS + mixed stress tests
```

All test targets use `-` (continue on error) so a single test failure does not
block later suites.

### Watchdog (`scripts/run-with-restart.sh`)

Background targets (`start-runpod-*`) wrap the server process in a watchdog
script that restarts it on crash:

```
run-with-restart.sh [OPTIONS] -- COMMAND [ARGS...]

  --pid-file PATH           inner process PID (for stop targets)
  --wrapper-pid-file PATH   watchdog's own PID
  --cooldown SECONDS        wait between restarts (default: 5)
  --log-dir PATH            plain-file logging (never a pipe -- a blocked pipe
                            can freeze the server):
                              log-dir/stdout.log    server stdout
                              log-dir/stderr.log    server stderr
                              log-dir/watchdog.log  restart events
                            Rotated to .1/.2/.3 at startup and whenever a file
                            exceeds MAX_LOG_BYTES (default 8 MiB, checked every
                            GUARD_INTERVAL seconds, default 60).
```

Sending `SIGTERM` to the wrapper gracefully stops the inner process and exits.

### Timeout ladder

Deadlines shrink as you go inward, so the innermost layer gives up first and every
layer above it reports an attributable failure rather than inventing its own:

| Layer | Setting | Value |
|-------|---------|-------|
| HAL (device) | client read timeout | 10-15s |
| nginx | `proxy_read_timeout` (`nginx.conf`) | 45s |
| nginx | `proxy_connect_timeout` | 5s |
| lbserver | `lb.http_timeout` (`config.py`) | 30s |
| lbserver | `lb.ws_open_timeout` | 30s |

**Never set two adjacent layers to the same value.** nginx and lbserver were both
120s, which made the winner nondeterministic: a hung backend surfaced as `504`
sometimes and `500` other times. lbserver must expire first so it can log which
backend timed out and return `504`.

HAL's 10-15s sits *inside* the whole chain, so the device still gives up before
nginx does. Raising it above 45s would make failures attributable end to end, but
costs realtime responsiveness -- that is a device-side decision, deployed by OTA.

### PID files and logs

| File | Purpose |
|------|---------|
| `/tmp/dlserver.pid` | dlserver process PID |
| `/tmp/dlserver-wrapper.pid` | dlserver watchdog PID |
| `/tmp/lbserver.pid` | lbserver process PID |
| `/tmp/lbserver-wrapper.pid` | lbserver watchdog PID |
| `/tmp/nginx.pid` | nginx master process PID |
| `/workspace/logs/dlserver/` | dlserver stdout/stderr/watchdog logs |
| `/workspace/logs/lbserver/` | lbserver stdout/stderr/watchdog logs |
| `/workspace/logs/jupyter/` | Jupyter Lab logs |

Optional: `make start-jupyter` runs Jupyter Lab on `:8890`, reachable at
`https://<host>:8899/jupyter/`.

## Docker

The `Dockerfile` (CUDA 12.4 + PyTorch + nginx) builds a single image that runs
as either **master** or **slave** via the `ROLE` env var.

```bash
docker build -t perception-service .
```

### Single node (master only)

```bash
docker compose up                # or:
docker run --gpus all -e DL_API_KEY=<secret> -p 8899:8899 perception-service
```

Runs nginx :8899 → lbserver :7999 → dlserver :8001.

### Multi-GPU (master + slaves on separate machines)

On each **slave** GPU machine:

```bash
docker run --gpus all \
  -e DL_API_KEY=<same-as-master> \
  -e ROLE=slave \
  -v model-cache:/workspace/models \
  -p 8899:8899 \
  perception-service
```

Or with compose: `DL_API_KEY=<secret> docker compose -f docker-compose.slave.yml up -d`

Slaves run nginx :8899 → dlserver :7999 (no load balancer).

On the **master**, point `LB__BACKENDS` at the local dlserver plus every slave:

```bash
docker run --gpus all \
  -e DL_API_KEY=<secret> \
  -e ROLE=master \
  -e LB__BACKENDS="http://127.0.0.1:8001,https://<slave1>:8899,https://<slave2>:8899" \
  -v model-cache:/workspace/models \
  -p 8899:8899 \
  perception-service
```

Or with compose: set `LB__BACKENDS` in `.env` and run `docker compose up -d`.

### Compose files

| File | Role | What it runs |
|------|------|-------------|
| `docker-compose.yml` | Master | nginx + lbserver + dlserver |
| `docker-compose.slave.yml` | Slave | nginx + dlserver (no LB) |

Model weights are stored on a named volume (`model-cache` → `/workspace/models`)
so they persist across container restarts. First start downloads from CDN.

## RunPod notes

- Expose pod port **8899** (the nginx port) — clients reach it at
  `https://<POD_ID>-8899.proxy.runpod.net/`. Device traffic uses the `/lelamp/`
  prefix (e.g. `…/lelamp/api/dl/action-analysis/ws`).
- First model use downloads weights from the public bucket into
  `~/.cache/perception-service/models` (slower first call). See
  [configuration.md#model-downloading](configuration.md#model-downloading), and
  note the four weights currently missing from the bucket.
- Put models on a persistent RunPod volume (point `MODEL_CACHE_DIR` at it) to avoid
  re-downloading on every pod restart.

### Autostart after a container recreate

RunPod recreates pod containers without notice (host maintenance, migration).
That kills the whole process tree — **including the watchdog**, so nothing
restarts the stack. The stock `runpod/pytorch` template only starts nginx, SSH
and Jupyter, so the pod comes back looking healthy (`RUNNING`, SSH answers,
nginx answers on `:8899`) while dlserver and lbserver are simply absent. Left
alone, this is a silent outage that lasts until someone notices 502s.

Two layers cover two different failures:

| Layer | Handles | Mechanism |
|-------|---------|-----------|
| Watchdog | dlserver/lbserver **crashes** | [run-with-restart.sh](../scripts/run-with-restart.sh) restarts the process in 5s |
| Autostart | **container recreates** | [runpod-autostart.sh](../scripts/runpod-autostart.sh), triggered on boot |

The watchdog cannot cover a recreate — it dies with the process it watches.

**Install (once per pod).** RunPod's `/start.sh` already runs `/post_start.sh`
on every container start, so no template change is needed:

```bash
cat > /post_start.sh <<'EOF'
#!/usr/bin/env bash
# /start.sh runs under `set -e` and calls this synchronously before its final
# `sleep infinity`. A non-zero exit would abort it and kill PID 1; a blocking
# call would stall pod startup. So: detach, and always return 0.
setsid nohup bash \
    /workspace/autonomous-os/integrations/perception-service/scripts/runpod-autostart.sh \
    </dev/null >/dev/null 2>&1 &
exit 0
EOF
chmod +x /post_start.sh
```

On each container start the script then waits for the `/workspace` MooseFS mount
and the GPU, exits early if dlserver is already up (safe to re-run by hand),
`cd`s to the repo root, runs `make start-runpod-master`, and polls
`/hal/api/dl/health` until it returns 200 — so the log records whether models
actually loaded, not just that `make` returned.

```bash
tail -f /workspace/logs/autostart/autostart.log
```

**Two things that must not change.** `/post_start.sh` has to exit 0 immediately,
for the reason in its comment. And `runpod-autostart.sh` has to `cd` to the repo
root before running `make`, because `config.py` declares `env_file=".env"` as a
*relative* path — started elsewhere, every setting silently falls back to its
default and the stack comes up misconfigured rather than failing.

**Limitation.** `/post_start.sh` lives on the container overlay `/`, which
survives a recreate on the same host but not a migration to a different machine
(the overlay is rebuilt from the image). `runpod-autostart.sh` is on `/workspace`
and always survives. If the pod is ever migrated, re-run the `cat` above. For
full durability, set the same command as the pod template's **Container Start
Command** in the RunPod console, which lives in RunPod's control plane.

For diagnosing an outage after the fact, see
[troubleshooting.md](troubleshooting.md).
</content>
