# Troubleshooting

How to work out why the stack stopped serving, and which of the several very
different causes you are actually looking at. All paths assume a RunPod master
node; adjust `/workspace` for other hosts.

Run everything from the repo root — several checks read `.env`:

```bash
cd /workspace/autonomous-os/integrations/perception-service
```

> `/workspace/autonomous-os/dlbackend/` is a stale pre-rename copy of the source
> (no Makefile). Never run from it. See
> [migration-dlbackend-rename.md](migration-dlbackend-rename.md).

## Symptom → likely cause

| Symptom | Most likely cause | Jump to |
|---------|-------------------|---------|
| Clients get 502, GPU shows 0 MiB used | dlserver not running at all | [1](#1-what-is-actually-running) |
| Clients get 401 | Wrong/missing `X-API-Key`, service is fine | [1](#1-what-is-actually-running) |
| Service vanished, no error in logs | Container was recreated | [4](#4-was-the-container-recreated) |
| Service restarts every few seconds | Application crash loop | [3](#3-crash-or-group-kill) |
| Process killed with no traceback | Out of memory | [5](#5-out-of-memory) |
| `make` fails on start | Disk full on `/` | [6](#6-disk) |

## 1. What is actually running

```bash
make info                      # dlserver / lbserver / nginx + PIDs
ps -eo pid,lstart,etime,rss,args | grep -E "[d]lserver|[l]bserver|run-with-restart"
ss -lntp | grep -E "8899|8001|7999"
```

`make info` reads the PID files, which can be stale. Trust `ps` and `ss` over it
when they disagree.

Then check whether the stack is actually serving, not just listening:

```bash
API_KEY=$(grep -E '^DL_API_KEY=' .env | head -1 | cut -d= -f2-)
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8899/hal/api/dl/health
```

| Response | Meaning |
|----------|---------|
| `{"status":"ok","models":{…}}` | Healthy, models loaded |
| `401 Invalid or missing API key` | **Service is up** — the auth layer answered. Your key is wrong |
| `502` from nginx | nginx is up, dlserver/lbserver behind it is down |
| Connection refused / empty | Nothing is listening — nginx is down too |

A 401 is a liveness signal, not a failure. Only the `200` body confirms models
are actually loaded, which is why the autostart probe checks for it.

## 2. Reading the logs

Logs are plain text files. No decoding is needed:

```bash
tail -100 /workspace/logs/dlserver/watchdog.log
tail -200 /workspace/logs/dlserver/stderr.log
tail -100 /workspace/logs/lbserver/watchdog.log
tail -50  /workspace/logs/autostart/autostart.log
```

| File | Contents |
|------|----------|
| `<svc>/stdout.log` | server stdout |
| `<svc>/stderr.log` | server stderr (library warnings, tracebacks) |
| `<svc>/watchdog.log` | restart events from `run-with-restart.sh` |
| `<svc>/<svc>.log` | application log (`RotatingFileHandler`, 1 MB × 3) |
| `<svc>/uvicorn.log` | uvicorn error **and** access log (one shared handler) |

Rotation keeps 3 generations as `.1`, `.2`, `.3`:

- `stdout/stderr/watchdog.log` are rotated by `run-with-restart.sh` — once at
  startup, and whenever a file exceeds `MAX_LOG_BYTES` (default 8 MiB, checked
  every `GUARD_INTERVAL` seconds, default 60). The size guard **copies then
  truncates in place**; it must never rename, because the server holds an
  `O_APPEND` fd and would keep writing to the renamed inode.
- `<svc>.log` and `uvicorn.log` are rotated by Python's `RotatingFileHandler`.

> **Historical note.** Before 2026-08-18 these were
> [multilog](https://cr.yp.to/daemontools/multilog.html) *directories* (`stdout/`,
> `stderr/`, `watchdog/`) whose live file was `current`, TAI64N-prefixed and read
> via `tai64nlocal`. Those directories may still exist with old data — decode them
> with `tai64nlocal` as before. They are no longer written to.
>
> multilog was removed because its documented reaction to a write error is to
> *pause and retry forever*, which stops it draining its input pipe. The server on
> the other end then blocks in `pipe_write` with no timeout and no way to run signal
> handlers — and that writer is the asyncio event loop. This froze lbserver on
> 2026-08-10 and dlserver on 2026-08-17. A plain file redirect cannot block the
> writer: a failed write returns an error instead.

## 3. Crash, or group kill?

**The watchdog log answers this, and it is the single most useful check.**
[run-with-restart.sh](../scripts/run-with-restart.sh) writes
`[watchdog] Process exited (code=N), restarting in 5s...` on *every* crash.

```bash
ls -l /workspace/logs/dlserver/watchdog/
cat /workspace/logs/dlserver/watchdog/current | tai64nlocal
```

| What you see | What it means | Next step |
|--------------|---------------|-----------|
| Repeated `Process exited (code=N)` | Application **crash loop**. The watchdog is doing its job | Read `stderr/current` for the traceback |
| `code=137` | SIGKILL — almost always OOM | [5](#5-out-of-memory) |
| `code=1` + traceback | Application error (missing model, port in use, bad config) | Fix the traceback |
| **Log is 0 bytes / empty** | The process **never exited while being watched**. The watchdog died at the same instant | [4](#4-was-the-container-recreated) |

An empty watchdog log across *every* generation is conclusive: the service did
not crash. Something killed the whole tree at once — a container recreate, an
OOM kill of the cgroup, or a manual `kill` of the process group.

## 4. Was the container recreated?

RunPod recreates containers without notice for host maintenance or migration.
This kills the watchdog too, so nothing restarts anything.

```bash
uptime -s                        # NOTE: usually the HOST's boot time, not this container's
ps -o lstart= -p 1               # PID 1 birth = this container's real start time
```

Use `ps -o lstart= -p 1`. Inside Docker, `/proc/uptime` is generally the host
kernel's, so `uptime -s` can be days off from when your container started.

If PID 1 is younger than your process's last log line, the container was
recreated and that is your answer.

Confirm against RunPod's own control plane:

```bash
K=$(tr '\0' '\n' < /proc/1/environ | grep '^RUNPOD_API_KEY=' | cut -d= -f2)
P=$(tr '\0' '\n' < /proc/1/environ | grep '^RUNPOD_POD_ID='  | cut -d= -f2)
curl -s -X POST "https://api.runpod.io/graphql?api_key=$K" \
  -H 'Content-Type: application/json' \
  -d "{\"query\":\"query { pod(input: {podId: \\\"$P\\\"}) { desiredStatus lastStatusChange runtime { uptimeInSeconds } } }\"}"
```

Read the result like this:

- **`lastStatusChange`** — the last *user* action ("Rented by User: …"). If this
  is weeks old, nobody stopped or resumed the pod.
- **`runtime.uptimeInSeconds`** — how long the current runtime has been up.
  Subtract from now to get the restart moment.
- A restart moment with **no matching user action** means RunPod restarted it,
  not you. Nothing on the pod will tell you why; check the console's pod events.

The pod reports `desiredStatus: RUNNING` and keeps billing throughout, and SSH,
Jupyter and nginx all still answer — so nothing looks wrong from outside while
the service is entirely gone. See
[deployment.md#autostart-after-a-container-recreate](deployment.md#autostart-after-a-container-recreate)
for the mechanism that prevents this.

## 5. Out of memory

**`free -h` is misleading inside a container** — it reports the host's RAM (e.g.
503 GiB) while you are limited to a fraction of it. Read the cgroup instead:

```bash
cat /sys/fs/cgroup/memory/memory.limit_in_bytes      # your real ceiling
cat /sys/fs/cgroup/memory/memory.usage_in_bytes      # current
cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes  # high-water mark
cat /sys/fs/cgroup/memory/memory.failcnt             # times the limit was hit
cat /sys/fs/cgroup/memory/memory.oom_control         # oom_kill counter
# cgroup v2: memory.max / memory.current / memory.events
echo $RUNPOD_MEM_GB                                  # RunPod's advertised limit
```

| Reading | Meaning |
|---------|---------|
| `failcnt` > 0 or `oom_kill` > 0 | The limit *was* hit — OOM is your cause |
| `max_usage` close to `limit` | No kill yet, but you are one request away |
| Both counters 0 | No OOM **since this container started** — they reset on recreate |

`dmesg -T | grep -i -E "oom|killed process"` gives host-side confirmation with
timestamps, but is often unreadable from inside an unprivileged container.

The single biggest consumer is dlserver (~16 GB RSS with all models loaded).
Running a second dlserver for testing roughly doubles that — check for stray
instances before blaming a leak:

```bash
ps -eo pid,rss,args --sort=-rss | head -5
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

## 6. Disk

Two filesystems with very different behaviour:

| Mount | Type | Size | Holds |
|-------|------|------|-------|
| `/workspace` | MooseFS network volume | huge | Repo, `.env`, logs, models. **Persists** across container recreates |
| `/` | Container overlay | 50 GB | OS, pip packages, `~/.cache`, `/post_start.sh` |

```bash
df -h / /workspace
du -sh /root/.cache /root/.vscode-server /root/.cursor-server 2>/dev/null
```

`/` is the one that fills up. `make start-runpod-master` runs `install` first,
which pip-installs the CUDA/TensorRT stack ([deployment.md#install](deployment.md#install)) —
if `/` is full, startup fails before anything serves. Deleting large directories
on `/workspace` does **not** help `/`; they are separate disks.

## 7. GPU

```bash
nvidia-smi
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

**0 MiB used means no dlserver is running** — the models are the only thing that
allocates GPU memory. It is a symptom of [1](#1-what-is-actually-running), not a
GPU fault. If `nvidia-smi` itself fails or hangs, the driver or device is the
problem and no restart will help until the pod is recreated.

## Recovering

```bash
cd /workspace/autonomous-os/integrations/perception-service
make stop-runpod-dlserver stop-runpod-lbserver stop-nginx
make start-runpod-master
make info
```

Always stop before starting.

> The stop targets still `rm -f /workspace/logs/*/stderr/lock`. That is now a
> no-op left over from multilog and will be removed — the lock files are not
> recreated. Note the stop targets only kill the PIDs recorded in `/tmp/*.pid`,
> so multilogs or other tree members started by an older build can survive as
> orphans; check with
> `ps -eo pid,ppid,args | grep -aE 'multilog|run-with-restart'` before starting.

Two things that silently produce a *misconfigured* server rather than a failure:

- **Run `make` from the repo root.** `config.py` declares `env_file=".env"` as a
  relative path, so starting from any other directory makes every setting fall
  back to its default.
- **`LB__BACKENDS` and `DL_API_KEY` come from `.env`**, not your shell. If you
  export them by hand, make sure they match `.env` or you will get a stack whose
  load balancer points somewhere unexpected.
