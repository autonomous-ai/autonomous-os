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

All other knobs (model selection, thresholds, crypto, model download) are in
[configuration.md](configuration.md).

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
| `make start-runpod-slave` | Background: nginx + dlserver only (no LB) |
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
  --log-dir PATH            structured logging via multilog:
                              log-dir/stdout/   server stdout
                              log-dir/stderr/   server stderr
                              log-dir/watchdog/ restart events
```

Sending `SIGTERM` to the wrapper gracefully stops the inner process and exits.

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

The `Dockerfile` (CUDA 12.4 + PyTorch + nginx) builds a single-node image:

```bash
docker build -t dlbackend .
docker run --gpus all -e DL_API_KEY=<secret> -p 8899:8899 dlbackend
```

It installs `.[dl,gpu,lb]`, sets `LB__BACKENDS=http://127.0.0.1:8001`, and starts
nginx + lbserver + dlserver (dlserver in the foreground). For multi-node scaling
and auto-restart, prefer the Makefile targets above.

## RunPod notes

- Expose pod port **8899** (the nginx port) — clients reach it at
  `https://<POD_ID>-8899.proxy.runpod.net/`. Device traffic uses the `/lelamp/`
  prefix (e.g. `…/lelamp/api/dl/action-analysis/ws`).
- First model use downloads weights from the public bucket into
  `~/.cache/dlbackend/models` (slower first call). See
  [configuration.md#model-downloading](configuration.md#model-downloading), and
  note the four weights currently missing from the bucket.
- Put models on a persistent RunPod volume (point `MODEL_CACHE_DIR` at it) to avoid
  re-downloading on every pod restart.
</content>
